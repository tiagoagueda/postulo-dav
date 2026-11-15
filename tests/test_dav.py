"""The sync, against a DAV server that lives in memory, and inside a real Postulo."""

from __future__ import annotations

import datetime as dt
import re
from html import escape
from importlib import metadata
from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest
from django.db import connection as db_connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from postulo.applications.models import (
    Application,
    ApplicationEvent,
    Interview,
    InterviewOutcome,
    Status,
)
from postulo.applications.services import schedule_interview
from postulo.jobs.models import Company, Contact, JobPosting
from postulo.plugins import registry, syncing
from postulo.plugins.api import (
    LinkKind,
    primary_phone_number,
    save_phone_number,
    save_web_link,
)
from postulo.plugins.base import SyncPlugin
from postulo.plugins.models import Connection, SyncLink

from postulo_dav import dav as dav_module
from postulo_dav import text
from postulo_dav.dav import ADDRESSBOOK, CALENDAR, Conflict, Server
from postulo_dav.sync import HISTORY, PRODID, DavSync, card_for, event_text

pytestmark = pytest.mark.django_db

BASE = "https://cloud.example.org"
CONFIG = {
    "url": BASE,
    "username": "ana",
    "password": "app-pw",
    "addressbook": "Postulo",
    "calendar": "Postulo",
    "read_only": False,
}


class FakeDav:
    """Enough of a CardDAV/CalDAV server: discovery, two homes, collections, resources."""

    def __init__(self) -> None:
        self.collections: dict[str, dict] = {
            "/dav/addressbooks/ana/contacts/": {"name": "Contacts", "kind": ADDRESSBOOK},
            "/dav/calendars/ana/personal/": {"name": "Personal", "kind": CALENDAR},
        }
        self.resources: dict[str, tuple[str, str]] = {}  # href → (text, etag)
        self.counter = 0
        self.log: list[tuple[str, str]] = []
        #: The ``If-Match`` of every DELETE, so a test can see that one was conditional.
        self.delete_conditions: list[str | None] = []
        #: Radicale lists a resource with its reserved characters escaped (``@`` as ``%40``).
        self.quote_listing = False
        #: RFC 4791 §5.3.2.1 and RFC 6352 §6.3.2.1: a PUT that changes a resource's UID, or
        #: reuses one already in the collection, is refused.
        self.enforce_uid = False
        #: The host of every request, so a test can see where the credentials went.
        self.hosts: list[str] = []
        #: Where ``/.well-known/…`` points; a hostile server points it somewhere else.
        self.well_known_location = f"{BASE}/dav/"
        #: Rows a hostile listing adds to the contacts collection: other hosts, other paths.
        self.foreign_hrefs: list[str] = []
        #: A server that refuses a calendar object carrying a METHOD (RFC 4791 §4.1).
        self.reject_method = False
        #: A server that rewrote what it was sent answers a PUT without an ETag (RFC 4791
        #: §5.3.4, RFC 6352 §6.3.2.3), as SabreDAV does whenever it repairs a body.
        self.put_etag = True
        #: The path and the Depth of every PROPFIND, so a test can count the listings.
        self.propfinds: list[tuple[str, str]] = []
        #: The multiget REPORTs (RFC 6352 §8.7, RFC 4791 §7.9); a server that refuses
        #: them answers 405, which no conforming server does.
        self.multiget = True
        #: How many hrefs each multiget REPORT asked for.
        self.multigets: list[int] = []

    def _etag(self) -> str:
        self.counter += 1
        return f'"v{self.counter}"'

    def add(self, href: str, body: str) -> str:
        etag = self._etag()
        self.resources[href] = (body, etag)
        return etag

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = self._handle(request)
        if self.quote_listing and response.status_code == 207:
            body = re.sub(
                r"<d:href>(.*?)</d:href>",
                lambda m: "<d:href>" + quote(unquote(m.group(1)), safe="/") + "</d:href>",
                response.text,
            )
            response = httpx.Response(207, content=body.encode(), headers=response.headers)
        return response

    @staticmethod
    def _uid(body: str) -> str:
        found = re.search(r"^UID:([^\r\n]*)", body, re.MULTILINE)
        return found.group(1) if found else ""

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(urlsplit(str(request.url)).path)
        method = request.method
        self.log.append((method, path))
        self.hosts.append(request.url.host)
        if request.headers.get("Authorization") != httpx.BasicAuth("ana", "app-pw")._auth_header:
            return httpx.Response(401)
        body = request.content.decode()

        if method == "PROPFIND":
            depth = request.headers.get("Depth", "0")
            self.propfinds.append((path, depth))
            if path in self.resources and depth == "0":
                etag = self.resources[path][1]
                return _multistatus([(path, f"<d:getetag>{etag}</d:getetag><d:resourcetype/>")])
            if path in ("/.well-known/carddav", "/.well-known/caldav"):
                return httpx.Response(301, headers={"Location": self.well_known_location})
            if path == "/dav/" and "current-user-principal" in body:
                return _multistatus(
                    [
                        (
                            "/dav/",
                            "<d:current-user-principal><d:href>/dav/principals/ana/</d:href></d:current-user-principal>",
                        )
                    ]
                )
            if path == "/dav/principals/ana/":
                return _multistatus(
                    [
                        (
                            "/dav/principals/ana/",
                            "<c:addressbook-home-set><d:href>/dav/addressbooks/ana/</d:href></c:addressbook-home-set>"
                            "<cal:calendar-home-set><d:href>/dav/calendars/ana/</d:href></cal:calendar-home-set>",
                        )
                    ]
                )
            if path in ("/dav/addressbooks/ana/", "/dav/calendars/ana/") and depth == "1":
                rows = [(path, "<d:resourcetype><d:collection/></d:resourcetype>")]
                for href, meta in self.collections.items():
                    if href.startswith(path):
                        kind = (
                            "<c:addressbook/>" if meta["kind"] == ADDRESSBOOK else "<cal:calendar/>"
                        )
                        rows.append(
                            (
                                href,
                                f"<d:resourcetype><d:collection/>{kind}</d:resourcetype><d:displayname>{meta['name']}</d:displayname>",
                            )
                        )
                return _multistatus(rows)
            if path in self.collections and depth == "1":
                rows = [(path, "<d:resourcetype><d:collection/></d:resourcetype>")]
                for href, (_text, etag) in self.resources.items():
                    if href.startswith(path):
                        rows.append((href, f"<d:getetag>{etag}</d:getetag><d:resourcetype/>"))
                rows += [
                    (href, '<d:getetag>"foreign"</d:getetag><d:resourcetype/>')
                    for href in self.foreign_hrefs
                ]
                return _multistatus(rows)
            return httpx.Response(404)

        if method == "REPORT":
            if not self.multiget or "-multiget" not in body:
                return httpx.Response(405)
            data = "c:address-data" if "addressbook-multiget" in body else "cal:calendar-data"
            asked = [unquote(href) for href in re.findall(r"<d:href>(.*?)</d:href>", body)]
            self.multigets.append(len(asked))
            rows, missing = [], []
            for href in asked:
                if href in self.resources and href.startswith(path):
                    text_, etag = self.resources[href]
                    rows.append(
                        (href, f"<d:getetag>{etag}</d:getetag><{data}>{escape(text_)}</{data}>")
                    )
                else:
                    missing.append(href)
            return _multistatus(rows, missing)

        if method in ("MKCOL", "MKCALENDAR"):
            name = re.search(r"<d:displayname>(.*?)</d:displayname>", body).group(1)
            self.collections[path] = {
                "name": name,
                "kind": ADDRESSBOOK if method == "MKCOL" else CALENDAR,
            }
            return httpx.Response(201)

        if method == "GET":
            if path not in self.resources:
                return httpx.Response(404)
            text_, etag = self.resources[path]
            return httpx.Response(200, text=text_, headers={"ETag": etag})

        if method == "PUT":
            current = self.resources.get(path)
            if request.headers.get("If-None-Match") == "*" and current is not None:
                return httpx.Response(412)
            if "If-Match" in request.headers and (
                current is None or current[1] != request.headers["If-Match"]
            ):
                return httpx.Response(412)
            if self.reject_method and "\nMETHOD:" in body.replace("\r\n", "\n"):
                return httpx.Response(403)
            if self.enforce_uid:
                uid = self._uid(body)
                folder = path.rsplit("/", 1)[0] + "/"
                if current is not None and self._uid(current[0]) != uid:
                    return httpx.Response(403)  # no-uid-conflict: the UID may not change
                if any(
                    other != path and other.startswith(folder) and self._uid(text_) == uid
                    for other, (text_, _etag) in self.resources.items()
                ):
                    return httpx.Response(409)  # no-uid-conflict: the UID is already there
            etag = self._etag()
            self.resources[path] = (body, etag)
            headers = {"ETag": etag} if self.put_etag else {}
            return httpx.Response(201 if current is None else 204, headers=headers)

        if method == "DELETE":
            self.delete_conditions.append(request.headers.get("If-Match"))
            current = self.resources.get(path)
            if current is None:
                return httpx.Response(404)
            if "If-Match" in request.headers and current[1] != request.headers["If-Match"]:
                return httpx.Response(412)
            del self.resources[path]
            return httpx.Response(204)

        return httpx.Response(405)


def _multistatus(rows, missing=()) -> httpx.Response:
    xml = [
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:carddav" '
        'xmlns:cal="urn:ietf:params:xml:ns:caldav">'
    ]
    for href, props in rows:
        xml.append(
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    for href in missing:
        xml.append(
            f"<d:response><d:href>{href}</d:href>"
            "<d:status>HTTP/1.1 404 Not Found</d:status></d:response>"
        )
    xml.append("</d:multistatus>")
    return httpx.Response(
        207, content="".join(xml).encode(), headers={"Content-Type": "application/xml"}
    )


@pytest.fixture
def server(monkeypatch):
    fake = FakeDav()

    def fake_client(**kwargs):
        kwargs.pop("event_hooks", None)
        kwargs.pop("total", None)
        kwargs.setdefault("timeout", 10)
        kwargs.setdefault("follow_redirects", True)
        return httpx.Client(transport=httpx.MockTransport(fake.handler), **kwargs)

    monkeypatch.setattr(dav_module, "postulo_client", fake_client)
    return fake


@pytest.fixture
def made(server):
    """The two Postulo collections already on the server, as a first sync would leave them."""
    server.collections["/dav/addressbooks/ana/postulo-contacts/"] = {
        "name": "Postulo",
        "kind": ADDRESSBOOK,
    }
    server.collections["/dav/calendars/ana/postulo-interviews/"] = {
        "name": "Postulo",
        "kind": CALENDAR,
    }
    return server


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="ana@example.org", password="pw" * 8)  # noqa: S106


@pytest.fixture
def connection(user):
    connection = Connection(
        owner=user,
        kind="sync",
        plugin="dav",
        label="Cloud",
        config={k: v for k, v in CONFIG.items() if k != "password"},
    )
    connection.secrets = {"password": "app-pw"}
    connection.save()
    return connection


@pytest.fixture
def company(user):
    return Company.objects.create(owner=user, name="Aperture Science")


@pytest.fixture
def recruiter(user, company):
    from postulo.core import phone_numbers

    contact = Contact.objects.create(
        owner=user,
        company=company,
        name="Cave Johnson",
        role="CEO",
        email="cave@aperture.test",
    )
    phone_numbers.save_only_number(contact, user, "+1 555 0100")
    return contact


@pytest.fixture
def application(user, company):
    posting = JobPosting.objects.create(owner=user, company=company, title="Test Engineer")
    return Application.objects.create(owner=user, posting=posting, status=Status.APPLIED)


def next_week(hour=10) -> dt.datetime:
    return (timezone.now() + dt.timedelta(days=7)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def cards(fake: FakeDav) -> dict[str, str]:
    return {href: body for href, (body, _e) in fake.resources.items() if href.endswith(".vcf")}


def events(fake: FakeDav) -> dict[str, str]:
    return {href: body for href, (body, _e) in fake.resources.items() if href.endswith(".ics")}


# ---------------------------------------------------------------------- text


def test_a_card_round_trips():
    card = text.Card(
        uid="u1",
        name="Cave Johnson",
        org="Aperture Science",
        title="CEO",
        email="cave@aperture.test",
        phone="+1 555 0100",
        url="https://linkedin.com/in/cave",
        note="Loud;\nlikes lemons, combustible",
    )
    rendered = text.card_to_text(
        card, prodid="-//test//EN", rev=dt.datetime(2026, 9, 6, 10, tzinfo=dt.UTC)
    )
    assert "FN:Cave Johnson" in rendered and "N:Johnson;Cave;;;" in rendered
    assert "NOTE:Loud\\;\\nlikes lemons\\, combustible" in rendered
    back = text.text_to_card(rendered)
    assert back == text.Card(**{**card.__dict__, "rev": dt.datetime(2026, 9, 6, 10, tzinfo=dt.UTC)})


def test_a_card_from_a_phone_is_understood():
    phone = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\n"
        "N:Freeman;Gordon;;Dr.;\r\nORG:Black Mesa;Research\r\n"
        "item1.EMAIL;type=INTERNET;type=pref:gordon@bm.test\r\n"
        "TEL;type=CELL:+1 555 0199\r\nitem2.URL:https://example.org/gordon\r\n"
        "UID:ABC-123\r\nEND:VCARD\r\n"
    )
    card = text.text_to_card(phone)
    assert card.name == "Gordon Freeman" and card.org == "Black Mesa"
    assert card.email == "gordon@bm.test" and card.phone == "+1 555 0199"
    assert card.url == "https://example.org/gordon" and card.uid == "ABC-123"


def test_an_event_is_read_with_its_zone_and_recurrence_noticed():
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:e1\r\n"
        "DTSTART;TZID=Europe/Paris:20260910T140000\r\n"
        "DTEND;TZID=Europe/Paris:20260910T150000\r\n"
        "SUMMARY:Interview\r\nLOCATION:Room 4\r\n"
        "LAST-MODIFIED:20260901T100000Z\r\nRRULE:FREQ=WEEKLY\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    (event,) = text.text_to_events(ics)
    assert event.starts_at == dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)
    assert event.ends_at == dt.datetime(2026, 9, 10, 13, 0, tzinfo=dt.UTC)
    assert event.location == "Room 4" and event.recurring and event.last_modified is not None
    (all_day,) = text.text_to_events("BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260910\r\nEND:VEVENT")
    # A whole day is not an instant: it is not read as midnight UTC, which moved interviews (#24).
    assert all_day.starts_at is None and all_day.problem == "date"


def test_long_lines_fold_and_unfold():
    card = text.Card(uid="u", name="A" * 200)
    rendered = text.card_to_text(card, prodid="p", rev=timezone.now())
    assert all(len(line.encode()) <= 75 for line in rendered.split("\r\n"))
    assert text.text_to_card(rendered).name == "A" * 200


# ------------------------------------------------------------------- discovery


def test_test_looks_and_never_makes_a_collection(server):
    plugin = DavSync()
    assert isinstance(plugin, SyncPlugin)
    before = dict(server.collections)
    result = plugin.test(CONFIG)
    assert result.ok, "signing in and discovery worked, which is what Test asks"
    assert "The address book “Postulo” is not on the server." in result.message
    assert "The calendar “Postulo” is not on the server." in result.message
    assert "first sync" in result.message
    assert server.collections == before, "pressing Test creates nothing on somebody's server"
    assert not [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR")]


def test_test_in_read_only_says_nothing_will_be_made(server):
    result = DavSync().test({**CONFIG, "read_only": True})
    assert not result.ok, "read only cannot make what is missing, so the sync cannot run"
    assert "Read only is on" in result.message
    assert not [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR")]


def test_test_finds_the_collections_once_they_are_there(made):
    result = DavSync().test(CONFIG)
    assert result.ok
    assert result.message == "Found the address book “Postulo” and the calendar “Postulo”."


def test_the_first_sync_makes_the_collections_and_the_second_does_not(server, connection):
    DavSync().sync(connection, connection.full_config)
    assert server.collections["/dav/addressbooks/ana/postulo-contacts/"]["name"] == "Postulo"
    assert server.collections["/dav/calendars/ana/postulo-interviews/"]["name"] == "Postulo"
    made = [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR")]
    DavSync().sync(connection, connection.full_config)
    assert [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR")] == made


def test_read_only_reports_a_missing_collection_rather_than_making_it(
    server, connection, recruiter
):
    connection.config["read_only"] = True
    connection.save()
    report = DavSync().sync(connection, connection.full_config)
    assert not [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR", "PUT")]
    assert "The address book “Postulo” is not on the server." in report.summary()
    assert "The calendar “Postulo” is not on the server." in report.summary()
    assert "Read only is on" in report.summary()


def test_a_wrong_password_is_a_sentence(server, user):
    result = DavSync().test({**CONFIG, "password": "no"})
    assert not result.ok
    assert result.message == "The server refused the user name or password."
    assert "principal" not in result.message and "DavError" not in result.message
    # ...and a sync says the same, in a report of its own, so the core adds no class name.
    connection = Connection(owner=user, kind="sync", plugin="dav", label="Cloud", config={})
    report = DavSync().sync(connection, {**CONFIG, "password": "no"})
    assert report.error == "The server refused the user name or password."


def test_the_server_helper_writes_conditionally(server):
    with httpx.Client(transport=httpx.MockTransport(server.handler)) as client:
        dav = Server(BASE, "ana", "app-pw", client=client)
        etag = dav.put("/dav/x/a.vcf", "BEGIN:VCARD\r\nEND:VCARD", "text/vcard")
        with pytest.raises(Conflict):
            dav.put("/dav/x/a.vcf", "again", "text/vcard")
        with pytest.raises(Conflict):
            dav.put("/dav/x/a.vcf", "again", "text/vcard", etag='"stale"')
        assert dav.put("/dav/x/a.vcf", "again", "text/vcard", etag=etag) != etag


# ------------------------------------------------------------------ contacts


def test_contacts_are_pushed_once_and_then_only_when_changed(server, connection, recruiter):
    report = DavSync().sync(connection, connection.full_config)
    assert report.pushed == 1 and report.pulled == 0
    (body,) = cards(server).values()
    assert "FN:Cave Johnson" in body and "ORG:Aperture Science" in body and "TITLE:CEO" in body
    link = SyncLink.for_record(connection, recruiter)
    assert link.remote_href.startswith(f"{BASE}/dav/addressbooks/ana/postulo-contacts/")

    assert DavSync().sync(connection, connection.full_config).summary() == "nothing to do"

    recruiter.role = "Founder"
    recruiter.save()
    report = DavSync().sync(connection, connection.full_config)
    assert report.pushed == 1
    (body,) = cards(server).values()
    assert "TITLE:Founder" in body
    assert [m for m, p in server.log if m == "PUT"] == ["PUT", "PUT"]


def test_a_change_on_the_phone_is_pulled(server, connection, recruiter):
    DavSync().sync(connection, connection.full_config)
    (href,) = cards(server)
    body, _ = server.resources[href]
    server.resources[href] = (
        body.replace("TEL;TYPE=work,voice:+1 555 0100", "TEL:+1 555 0111").replace(
            "ORG:Aperture Science", "ORG:Aperture Labs"
        ),
        '"phone-1"',
    )
    report = DavSync().sync(connection, connection.full_config)
    assert report.pulled == 1
    recruiter.refresh_from_db()
    from postulo.core import phone_numbers

    assert phone_numbers.primary_for(recruiter).number == "+1 555 0111"
    assert recruiter.company.name == "Aperture Labs", (
        "a new ORG becomes a company of the person's own"
    )


def test_when_both_changed_the_later_wins_and_the_loser_is_a_note(server, connection, recruiter):
    DavSync().sync(connection, connection.full_config)
    (href,) = cards(server)
    body, _ = server.resources[href]
    old_rev = "REV:20200101T000000Z"
    server.resources[href] = (
        re.sub(r"REV:\S+", old_rev, body).replace("TITLE:CEO", "TITLE:Janitor"),
        '"phone-2"',
    )
    recruiter.role = "Chairman"
    recruiter.save()
    report = DavSync().sync(connection, connection.full_config)
    recruiter.refresh_from_db()
    assert recruiter.role == "Chairman", "ours is later"
    note = recruiter.note_for(recruiter.owner)
    assert "Changed on the phone on 2020-01-01" in note and "Janitor" in note
    assert report.pushed == 1
    assert "TITLE:Chairman" in server.resources[href][0]

    # The other way round: the phone's change is later than ours.
    body, _ = server.resources[href]
    server.resources[href] = (
        re.sub(r"REV:\S+", "REV:20990101T000000Z", body).replace(
            "TITLE:Chairman", "TITLE:Emeritus"
        ),
        '"phone-3"',
    )
    Contact.objects.filter(pk=recruiter.pk).update(
        role="Boss", updated_at=timezone.now() - dt.timedelta(days=1)
    )
    report = DavSync().sync(connection, connection.full_config)
    recruiter.refresh_from_db()
    assert recruiter.role == "Emeritus" and report.pulled == 1
    note = recruiter.note_for(recruiter.owner)
    assert "Changed here on" in note and "Boss" in note


def test_a_deletion_on_the_phone_never_deletes_here(server, connection, recruiter):
    DavSync().sync(connection, connection.full_config)
    server.resources.clear()
    report = DavSync().sync(connection, connection.full_config)
    assert report.removed == 1 and "removed from the address book" in report.summary()
    assert Contact.objects.filter(pk=recruiter.pk).exists()
    assert SyncLink.for_record(connection, recruiter).remote_gone is True
    report = DavSync().sync(connection, connection.full_config)
    assert report.summary() == "nothing to do", "a record removed on purpose is not pushed back"


def test_an_unpaired_contact_is_neither_written_nor_pulled_nor_adopted(
    server, connection, recruiter
):
    DavSync().sync(connection, connection.full_config)
    (href,) = cards(server)
    SyncLink.unbind(connection, recruiter)
    recruiter.role = "Founder"
    recruiter.save()
    body, _ = server.resources[href]
    server.resources[href] = (body.replace("ORG:Aperture Science", "ORG:Aperture Labs"), '"p-2"')
    puts = [m for m, p in server.log if m == "PUT"]

    report = DavSync().sync(connection, connection.full_config)

    assert [m for m, p in server.log if m == "PUT"] == puts, "nothing was written to the twin"
    assert report.pushed == 0 and report.pulled == 0
    recruiter.refresh_from_db()
    assert recruiter.company.name == "Aperture Science", "nothing came in from the twin"
    assert Contact.objects.count() == 1, "the card was not adopted as a second contact"
    assert SyncLink.for_record(connection, recruiter).paired is False


def test_an_unpaired_contact_is_not_paired_again_when_its_card_goes(server, connection, recruiter):
    DavSync().sync(connection, connection.full_config)
    SyncLink.unbind(connection, recruiter)
    server.resources.clear()
    report = DavSync().sync(connection, connection.full_config)
    assert report.pushed == 0 and cards(server) == {}, "an unpaired contact is not pushed anew"
    assert SyncLink.for_record(connection, recruiter).paired is False


def test_an_unpaired_contact_deleted_here_leaves_its_card_alone(server, connection, recruiter):
    DavSync().sync(connection, connection.full_config)
    SyncLink.unbind(connection, recruiter)
    recruiter.delete()
    DavSync().sync(connection, connection.full_config)
    assert server.delete_conditions == [], "the card of an unpaired contact is not deleted"
    assert SyncLink.objects.count() == 0


def test_an_unpaired_interview_is_not_written(server, connection, application):
    interview = schedule_interview(application, kind="video", starts_at=next_week())
    DavSync().sync(connection, connection.full_config)
    SyncLink.unbind(connection, interview)
    puts = [m for m, p in server.log if m == "PUT"]
    interview.notes = "Bring a laptop"
    interview.save()
    server.resources.clear()
    report = DavSync().sync(connection, connection.full_config)
    assert [m for m, p in server.log if m == "PUT"] == puts
    assert report.pushed == 0 and events(server) == {}
    assert SyncLink.for_record(connection, interview).paired is False


def test_a_deletion_here_takes_the_twin_with_it(server, connection, recruiter):
    DavSync().sync(connection, connection.full_config)
    (href,) = cards(server)
    etag = server.resources[href][1]
    recruiter.delete()
    report = DavSync().sync(connection, connection.full_config)
    assert report.removed == 1 and cards(server) == {} and SyncLink.objects.count() == 0
    assert server.delete_conditions == [etag], "the DELETE named the version it was destroying"


def test_a_card_changed_on_the_phone_survives_a_deletion_here(server, connection, recruiter):
    DavSync().sync(connection, connection.full_config)
    (href,) = cards(server)
    body, _etag = server.resources[href]
    # Edited on the phone after the last sync: a new ETag the connection has never seen.
    server.resources[href] = (body.replace("TITLE:CEO", "TITLE:Janitor"), '"phone-1"')
    recruiter.delete()
    report = DavSync().sync(connection, connection.full_config)
    assert report.removed == 0, "412, so nothing was destroyed"
    assert "left in the address book" in report.summary()
    assert "TITLE:Janitor" in server.resources[href][0]
    assert SyncLink.objects.count() == 0, "the link named a contact that no longer exists"

    # The card is unlinked and still there, so the next run adopts it, visibly.
    report = DavSync().sync(connection, connection.full_config)
    assert report.pulled == 1
    assert Contact.objects.get(name="Cave Johnson").role == "Janitor"


def test_an_event_changed_on_the_phone_survives_a_deletion_here(server, connection, application):
    interview = schedule_interview(application, kind="panel", starts_at=next_week())
    DavSync().sync(connection, connection.full_config)
    (href,) = events(server)
    body, _etag = server.resources[href]
    server.resources[href] = (body, '"phone-9"')
    interview.delete()
    report = DavSync().sync(connection, connection.full_config)
    assert report.removed == 0 and "left on the calendar" in report.summary()
    assert href in server.resources, "an interview deleted here does not erase somebody's edit"
    assert SyncLink.objects.count() == 0

    # Nothing adopts a foreign event, so from now on it is simply left alone.
    report = DavSync().sync(connection, connection.full_config)
    assert report.skipped == 1 and "not from Postulo" in report.summary()


def test_a_password_will_not_go_over_plain_http(settings):
    plugin = DavSync()
    assert plugin.validate({**CONFIG, "url": "https://cloud.example.org"}) == {}
    assert (
        "in the clear" in plugin.validate({**CONFIG, "url": "http://cloud.example.org"})["url"][0]
    )
    assert plugin.validate({**CONFIG, "url": "ftp://cloud.example.org"})["url"]
    assert plugin.validate({**CONFIG, "url": ""}) == {}

    # The machine down the hall, where the operator has allowed private destinations.
    settings.POSTULO_CONNECTIONS_ALLOW_PRIVATE = True
    assert plugin.validate({**CONFIG, "url": "http://192.168.1.4:8080"}) == {}
    assert plugin.validate({**CONFIG, "url": "http://nextcloud/"}) == {}
    assert plugin.validate({**CONFIG, "url": "http://cloud.home.arpa"}) == {}
    settings.POSTULO_CONNECTIONS_ALLOW_PRIVATE = False
    assert plugin.validate({**CONFIG, "url": "http://192.168.1.4:8080"})["url"]


def test_a_card_created_on_the_phone_is_adopted(server, connection, user):
    DavSync().sync(connection, connection.full_config)
    server.add(
        "/dav/addressbooks/ana/postulo-contacts/phone.vcf",
        "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:p1\r\n"
        "FN:Gordon Freeman\r\nEMAIL:gordon@bm.test\r\nEND:VCARD\r\n",
    )
    server.add(
        "/dav/addressbooks/ana/postulo-contacts/phone2.vcf",
        "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:p2\r\n"
        "FN:Barney Calhoun\r\nORG:Black Mesa\r\nEND:VCARD\r\n",
    )
    report = DavSync().sync(connection, connection.full_config)
    assert report.pulled == 2
    gordon = Contact.objects.get(name="Gordon Freeman")
    assert (
        gordon.owner == user
        and gordon.company.name == "(no company)"
        and gordon.email == "gordon@bm.test"
    )
    assert Contact.objects.get(name="Barney Calhoun").company.name == "Black Mesa"
    assert SyncLink.of_model(connection, Contact).count() == 2
    assert DavSync().sync(connection, connection.full_config).summary() == "nothing to do"


# ---------------------------------------------------------------- interviews


def test_interviews_are_pushed_as_events_with_an_alarm(server, connection, application, recruiter):
    interview = schedule_interview(
        application, kind="video", starts_at=next_week(), contacts=[recruiter]
    )
    report = DavSync().sync(connection, connection.full_config)
    assert report.pushed == 2, "one contact, one interview"
    (body,) = events(server).values()
    assert (
        f"UID:{interview.uid}" in body
        and "SUMMARY:Video call: Test Engineer at Aperture Science" in body
    )
    # Since postulo/postulo#963 an event names nobody as an ATTENDEE (a calendar program may
    # offer to mail an attendee), so the people met are text in the description; and the
    # document says it is published, not an invitation.
    assert "ATTENDEE" not in body and "mailto:" not in body
    assert "METHOD:PUBLISH" in body
    assert "With: Cave Johnson (CEO)" in body
    assert "BEGIN:VALARM" in body and "TRIGGER:-P1D" in body

    # DTSTAMP is the moment of writing, so it is the one line two renderings may disagree on.
    def without_stamp(document: str) -> str:
        return re.sub(r"DTSTAMP:\S+", "", document)

    assert without_stamp(body) == without_stamp(event_text(interview))
    assert SyncLink.for_record(connection, interview).remote_href.endswith(
        f"/postulo-interviews/{interview.uid.split('@')[0]}.ics"
    )


def test_moving_an_interview_on_the_phone_moves_it_here_through_the_timeline(
    server, connection, application
):
    interview = schedule_interview(application, kind="onsite", starts_at=next_week())
    DavSync().sync(connection, connection.full_config)
    (href,) = events(server)
    body, _ = server.resources[href]
    later = next_week(hour=15)
    body = re.sub(r"DTSTART:\S+", f"DTSTART:{text.stamp(later)}", body)
    body = re.sub(r"DTEND:\S+", f"DTEND:{text.stamp(later + dt.timedelta(hours=1))}", body)
    body = (
        body.replace("END:VEVENT", "LOCATION:Building 7\r\nEND:VEVENT")
        .replace("LAST-MODIFIED:", "LAST-MODIFIED:2099")
        .replace("LAST-MODIFIED:2099", "LAST-MODIFIED:")
    )
    body = re.sub(r"LAST-MODIFIED:\S+", "LAST-MODIFIED:20990101T000000Z", body)
    server.resources[href] = (body, '"phone-9"')
    report = DavSync().sync(connection, connection.full_config)
    assert report.pulled == 1
    interview.refresh_from_db()
    assert interview.starts_at == later and interview.location == "Building 7"
    assert ApplicationEvent.objects.filter(
        application=application, summary__contains="moved from"
    ).exists()
    assert ApplicationEvent.objects.filter(application=application, actor="dav").exists()
    assert interview.reminder.due_at == later - dt.timedelta(days=1)


def test_cancelling_on_the_phone_settles_the_interview(server, connection, application):
    interview = schedule_interview(application, kind="phone", starts_at=next_week())
    DavSync().sync(connection, connection.full_config)
    (href,) = events(server)
    body, _ = server.resources[href]
    body = body.replace("STATUS:CONFIRMED", "STATUS:CANCELLED")
    body = re.sub(r"LAST-MODIFIED:\S+", "LAST-MODIFIED:20990101T000000Z", body)
    server.resources[href] = (body, '"phone-10"')
    DavSync().sync(connection, connection.full_config)
    interview.refresh_from_db()
    assert interview.outcome == InterviewOutcome.CANCELLED
    assert ApplicationEvent.objects.filter(
        application=application, kind="interview_cancelled"
    ).exists()


def test_a_removed_event_is_flagged_on_the_timeline_and_kept(server, connection, application):
    interview = schedule_interview(application, kind="panel", starts_at=next_week())
    DavSync().sync(connection, connection.full_config)
    server.resources = {h: v for h, v in server.resources.items() if not h.endswith(".ics")}
    report = DavSync().sync(connection, connection.full_config)
    assert report.removed == 1
    assert Interview.objects.filter(pk=interview.pk).exists()
    assert ApplicationEvent.objects.filter(
        application=application, summary__contains="removed from the calendar"
    ).exists()


def test_events_from_elsewhere_and_recurring_ones_are_left_alone(server, connection, application):
    DavSync().sync(connection, connection.full_config)
    server.add(
        "/dav/calendars/ana/postulo-interviews/dentist.ics",
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:d\r\nDTSTART:20260910T090000Z\r\nDTEND:20260910T100000Z\r\nSUMMARY:Dentist\r\nRRULE:FREQ=YEARLY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
    )
    report = DavSync().sync(connection, connection.full_config)
    assert report.skipped == 1 and "not from Postulo" in report.summary()
    assert Interview.objects.count() == 0


def test_read_only_pulls_but_never_writes(made, connection, recruiter, application):
    server = made
    schedule_interview(application, kind="video", starts_at=next_week())
    connection.config["read_only"] = True
    connection.save()
    report = DavSync().sync(connection, connection.full_config)
    assert report.pushed == 0 and report.skipped == 2
    assert not [m for m, _p in server.log if m in ("PUT", "DELETE", "MKCOL", "MKCALENDAR")]
    server.add(
        "/dav/addressbooks/ana/postulo-contacts/phone.vcf",
        "BEGIN:VCARD\r\nFN:Alyx Vance\r\nEND:VCARD\r\n",
    )
    report = DavSync().sync(connection, connection.full_config)
    assert report.pulled == 1 and Contact.objects.filter(name="Alyx Vance").exists()


# --------------------------------------------------------------- in Postulo


def test_postulo_runs_it_from_the_scheduler_and_shows_the_report(server, connection, recruiter):
    registry.plugins("sync", refresh=True)
    assert registry.find_plugin("sync", "dav") is not None
    assert syncing.run_syncs() == (1, 0)
    connection.refresh_from_db()
    assert connection.last_summary == "1 pushed" and connection.last_error == ""
    assert card_for(recruiter).name == "Cave Johnson"


def test_the_plugin_speaks_french_and_portuguese_by_itself():
    from django.utils import translation

    registry.plugins("sync", refresh=True)
    plugin = registry.find_plugin("sync", "dav")
    with translation.override("fr-FR"):
        assert str(plugin.label) == "CardDAV et CalDAV"
        assert plugin.config_fields()[3].label == "Carnet d'adresses"
    with translation.override("pt-PT"):
        assert str(plugin.label) == "CardDAV e CalDAV"
    with translation.override("en-GB"):
        assert str(plugin.label) == "CardDAV and CalDAV"


def test_the_version_is_the_one_the_package_was_built_with():
    import postulo_dav

    assert postulo_dav.__version__ == metadata.version("postulo-dav")
    assert DavSync().version == postulo_dav.__version__
    assert PRODID.endswith(f"postulo-dav {postulo_dav.__version__}//EN")


# =============================================================================
# The review of 2026-09-29 and the security review of 2026-10-16
# =============================================================================

BOOK = "/dav/addressbooks/ana/postulo-contacts/"
CALENDAR_PATH = "/dav/calendars/ana/postulo-interviews/"


def vcard(uid: str, name: str, **properties: str) -> str:
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{uid}", f"FN:{name}"]
    lines += [f"{key.replace('_', '-')}:{value}" for key, value in properties.items()]
    return "\r\n".join([*lines, "END:VCARD"]) + "\r\n"


def new_connection(user, label="Again") -> Connection:
    connection = Connection(
        owner=user,
        kind="sync",
        plugin="dav",
        label=label,
        config={k: v for k, v in CONFIG.items() if k != "password"},
    )
    connection.secrets = {"password": "app-pw"}
    connection.save()
    return connection


def sync(connection):
    return DavSync().sync(connection, connection.full_config)


# ------------------------------------------------------------------ the writer (#27)


def test_a_card_is_written_with_no_property_nobody_set():
    card = text.Card(
        uid="u1",
        name="Ana\rORG:Injected",
        org="Co\r\nTITLE:Boss",
        title="x\ry",
        email="a@b.test\nTITLE:Injected",
        phone="1\r2",
        url="https://x.test/\nNOTE:Injected",
        note="first\rTITLE:Injected\r\nmore\x00end\x7f",
    )
    rendered = text.card_to_text(card, prodid="p", rev=dt.datetime(2026, 9, 6, 10, tzinfo=dt.UTC))
    assert "\r" not in rendered.replace("\r\n", ""), "no lone carriage return is left"
    names = sorted(prop.name for prop in text.properties(rendered))
    assert names == sorted(
        "BEGIN VERSION PRODID UID FN N ORG TITLE EMAIL TEL URL NOTE REV END".split()
    ), "every property is one somebody set"
    back = text.text_to_card(rendered)
    assert back.name == "Ana\nORG:Injected" and back.org == "Co\nTITLE:Boss"
    assert back.title == "x\ny" and back.phone == "1\n2"
    assert back.email == "a@b.testTITLE:Injected" and back.url == "https://x.test/NOTE:Injected"
    assert back.note == "first\nTITLE:Injected\nmoreend"


def test_the_writers_rule_is_the_one_postulos_own_writer_keeps():
    from postulo.applications import ical

    for sample in ("a\rb", "a\r\nb", "a\nb", "a\x00b\x1fc\x7fd", "plain", "tab\there"):
        assert text.clean(sample) == ical.clean(sample)
        assert text.without_controls(sample) == ical.without_controls(sample)
    assert text.CONTROLS == ical.CONTROLS


# ------------------------------------------------------------ the readers (#25 #24 #34 #11)


def test_a_rev_is_read_in_every_form_a_vcard_gives_it():
    ten = dt.datetime(2026, 9, 6, 10, tzinfo=dt.UTC)
    for value in (
        "20260906T100000Z",
        "2026-09-06T10:00:00Z",
        "2026-09-06T12:00:00+02:00",
        "20260906T120000+0200",
        "2026-09-06T10:00:00.250Z",
    ):
        assert text.parse_stamp(value) == ten, value
    for bad in ("", "garbage", "2026-09-06", "20261301T000000Z", "20260906T250000Z"):
        assert text.parse_stamp(bad) is None, bad


def test_an_older_extended_rev_loses_to_a_later_edit_here(server, connection, recruiter):
    sync(connection)
    (href,) = cards(server)
    body, _ = server.resources[href]
    server.resources[href] = (
        re.sub(r"REV:\S+", "REV:2020-01-01T00:00:00Z", body).replace("TITLE:CEO", "TITLE:Janitor"),
        '"phone-2"',
    )
    recruiter.role = "Chairman"
    recruiter.save()
    sync(connection)
    recruiter.refresh_from_db()
    assert recruiter.role == "Chairman", "the phone's change is the older, and loses"


def test_a_card_that_gives_no_time_wins_and_says_it_was_chosen_blind(server, connection, recruiter):
    sync(connection)
    (href,) = cards(server)
    body, _ = server.resources[href]
    server.resources[href] = (
        re.sub(r"REV:\S+", "REV:garbage", body).replace("TITLE:CEO", "TITLE:Janitor"),
        '"phone-2"',
    )
    recruiter.role = "Chairman"
    recruiter.save()
    report = sync(connection)
    recruiter.refresh_from_db()
    assert recruiter.role == "Janitor"
    assert "gives no time" in report.summary()


def test_a_windows_zone_name_resolves_to_the_right_instant():
    summer = text.Property("DTSTART", "20260910T140000", {"TZID": "W. Europe Standard Time"})
    assert text.parse_moment(summer) == dt.datetime(2026, 9, 10, 12, tzinfo=dt.UTC)
    winter = text.Property("DTSTART", "20261210T080000", {"TZID": "Pacific Standard Time"})
    assert text.parse_moment(winter) == dt.datetime(2026, 12, 10, 16, tzinfo=dt.UTC)


def test_every_windows_zone_names_a_zone_that_exists():
    from zoneinfo import ZoneInfo

    missing = []
    for windows, iana in text.WINDOWS_ZONES.items():
        try:
            ZoneInfo(iana)
        except Exception:  # noqa: BLE001 - the list of what is wrong is the point
            missing.append((windows, iana))
    assert missing == []


def test_a_time_that_is_not_an_instant_is_not_read_as_utc():
    assert text.read_moment(text.Property("DTSTART", "20260910T140000")) == (None, "floating")
    assert text.read_moment(text.Property("DTSTART", "20260910", {"VALUE": "DATE"})) == (
        None,
        "date",
    )
    assert text.read_moment(
        text.Property("DTSTART", "20260910T140000", {"TZID": "Nowhere/Land"})
    ) == (None, "zone:Nowhere/Land")
    assert text.read_moment(text.Property("DTSTART", "20260931T250000Z")) == (None, "unreadable")


def test_a_zone_the_document_defines_itself_is_used_when_it_can_be_understood():
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VTIMEZONE\r\nTZID:Customized Zone\r\nX-LIC-LOCATION:Europe/Paris\r\n"
        "BEGIN:STANDARD\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nDTSTART:19701025T030000\r\n"
        "END:STANDARD\r\nEND:VTIMEZONE\r\n"
        "BEGIN:VTIMEZONE\r\nTZID:Fixed\r\n"
        "BEGIN:STANDARD\r\nTZOFFSETFROM:+0530\r\nTZOFFSETTO:+0530\r\nDTSTART:19700101T000000\r\n"
        "END:STANDARD\r\nEND:VTIMEZONE\r\n"
        "BEGIN:VEVENT\r\nUID:e\r\n"
        "DTSTART;TZID=Customized Zone:20260910T140000\r\n"
        "DTEND;TZID=Fixed:20260910T150000\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    (event,) = text.text_to_events(ics)
    assert event.starts_at == dt.datetime(2026, 9, 10, 12, tzinfo=dt.UTC)
    assert event.ends_at == dt.datetime(2026, 9, 10, 9, 30, tzinfo=dt.UTC)
    assert event.problem == ""


def test_an_alarms_text_does_not_replace_the_events_own_description():
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:e\r\nDESCRIPTION:Bring the portfolio\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Interview tomorrow\r\nTRIGGER:-P1D\r\n"
        "END:VALARM\r\nSUMMARY:Panel\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    (event,) = text.text_to_events(ics)
    assert event.description == "Bring the portfolio" and event.summary == "Panel"
    only_an_alarm = ics.replace("DESCRIPTION:Bring the portfolio\r\n", "")
    assert text.text_to_events(only_an_alarm)[0].description == ""


def test_a_duration_ends_an_event_that_has_no_end():
    ics = "BEGIN:VEVENT\r\nDTSTART:20260910T100000Z\r\nDURATION:PT1H30M\r\nEND:VEVENT\r\n"
    (event,) = text.text_to_events(ics)
    assert event.ends_at == dt.datetime(2026, 9, 10, 11, 30, tzinfo=dt.UTC)


# ------------------------------------------------------------------- the pull (#24 #34)


def _push_one_interview(server, connection, application, kind="onsite"):
    interview = schedule_interview(application, kind=kind, starts_at=next_week())
    sync(connection)
    (href,) = events(server)
    return interview, href, server.resources[href][0]


@pytest.mark.parametrize(
    ("start", "why"),
    [
        ("DTSTART;TZID=Nowhere/Land:20260910T140000", "time zone"),
        ("DTSTART:20260910T140000", "no time zone"),
        ("DTSTART;VALUE=DATE:20260910", "all-day"),
    ],
)
def test_an_event_whose_time_is_not_an_instant_does_not_move_the_interview(
    server, connection, application, start, why
):
    interview, href, body = _push_one_interview(server, connection, application)
    end = (
        start.replace("DTSTART", "DTEND")
        .replace("140000", "150000")
        .replace("20260910", "20260911")
    )
    body = re.sub(r"DTSTART:\S+", start, body)
    body = re.sub(r"DTEND:\S+", end, body)
    server.resources[href] = (body, '"phone-1"')
    before = Interview.objects.get(pk=interview.pk).starts_at
    report = sync(connection)
    interview.refresh_from_db()
    assert interview.starts_at == before, "an interview is not moved on a guess"
    assert "could not be used" in report.summary() and why in report.summary()
    assert not ApplicationEvent.objects.filter(application=application, summary__contains="moved")


def test_an_event_that_ends_before_it_starts_does_not_move_the_interview(
    server, connection, application
):
    interview, href, body = _push_one_interview(server, connection, application)
    start = interview.starts_at
    body = re.sub(r"DTSTART:\S+", f"DTSTART:{text.stamp(start + dt.timedelta(hours=2))}", body)
    body = re.sub(r"DTEND:\S+", f"DTEND:{text.stamp(start)}", body)
    server.resources[href] = (body, '"phone-1"')
    report = sync(connection)
    interview.refresh_from_db()
    assert interview.starts_at == start
    assert "ends before it starts" in report.summary()


def test_one_bad_resource_is_a_note_and_the_run_goes_on(made, connection, application):
    server = made
    schedule_interview(application, kind="phone", starts_at=next_week())
    server.add(BOOK + "long-uid.vcf", vcard("U" * 300, "Long Uid"))
    server.add(BOOK + "odd-rev.vcf", vcard("odd", "Odd Rev", REV="20261301T000000Z"))
    server.add(BOOK + "fine.vcf", vcard("fine", "Fine Person"))
    report = sync(connection)
    assert report.error == ""
    assert {c.name for c in Contact.objects.all()} == {"Odd Rev", "Fine Person"}
    assert "Long Uid" in report.summary() and "too long" in report.summary()
    assert len(events(server)) == 1, "the interview was pushed in the same run"


# --------------------------------------------------------------- the database (#20)


def test_a_card_with_a_name_and_a_company_over_the_limit_is_adopted_once(made, connection, user):
    server = made
    server.add(BOOK + "long.vcf", vcard("long", "N" * 250, ORG="O" * 250))
    report = sync(connection)
    assert report.error == "" and report.pulled == 1
    (contact,) = Contact.objects.all()
    assert len(contact.name) <= 200 and len(contact.company.name) <= 200
    assert SyncLink.of_model(connection, Contact).count() == 1
    assert sync(connection).summary() == "nothing to do"


def test_a_card_that_cannot_be_applied_leaves_no_contact_and_ends_nothing(
    made, connection, application, monkeypatch
):
    from django.core.exceptions import ValidationError

    from postulo_dav import sync as sync_module

    server = made
    schedule_interview(application, kind="phone", starts_at=next_week())
    server.add(BOOK + "bad.vcf", vcard("bad", "Bad Card"))
    server.add(BOOK + "good.vcf", vcard("good", "Good Card"))
    real = sync_module.apply_card

    def apply(contact, card, *args, **kwargs):
        if card.name == "Bad Card":
            contact.save()
            raise ValidationError("no")
        return real(contact, card, *args, **kwargs)

    monkeypatch.setattr(sync_module, "apply_card", apply)
    report = sync(connection)
    assert not Contact.objects.filter(name="Bad Card").exists(), "no half-made contact is left"
    assert Contact.objects.filter(name="Good Card").exists()
    assert "Bad Card" in report.summary() and report.skipped == 1
    assert len(events(server)) == 1, "the interviews half ran in the same run"


def test_one_half_failing_is_reported_and_the_other_still_runs(
    made, connection, application, monkeypatch
):
    server = made
    schedule_interview(application, kind="phone", starts_at=next_week())

    def broken(self, run, book):
        raise RuntimeError("the contacts half broke")

    monkeypatch.setattr(DavSync, "_sync_contacts", broken)
    report = sync(connection)
    assert "the contacts half broke" in report.error
    assert len(events(server)) == 1


# ------------------------------------------------------------------ conflicts (#21)


def test_read_only_records_a_lost_contact_conflict_once(server, connection, recruiter, user):
    sync(connection)
    (href,) = cards(server)
    body, _ = server.resources[href]
    server.resources[href] = (
        re.sub(r"REV:\S+", "REV:20200101T000000Z", body).replace("TITLE:CEO", "TITLE:Janitor"),
        '"phone-2"',
    )
    recruiter.role = "Chairman"
    recruiter.save()
    connection.config["read_only"] = True
    connection.save()
    for _run in range(3):
        sync(connection)
    note = Contact.objects.get(pk=recruiter.pk).note_for(user)
    assert note.count("Changed on the phone on") == 1


def test_read_only_records_a_lost_interview_conflict_once(server, connection, application):
    interview, href, body = _push_one_interview(server, connection, application)
    body = body.replace("END:VEVENT", "LOCATION:Phone room\r\nEND:VEVENT")
    body = re.sub(r"LAST-MODIFIED:\S+", "LAST-MODIFIED:20200101T000000Z", body)
    server.resources[href] = (body, '"phone-2"')
    Interview.objects.filter(pk=interview.pk).update(
        location="Room 9", updated_at=timezone.now() + dt.timedelta(minutes=1)
    )
    connection.config["read_only"] = True
    connection.save()
    for _run in range(3):
        sync(connection)
    assert (
        ApplicationEvent.objects.filter(
            application=application, summary__contains="kept ours"
        ).count()
        == 1
    )


# -------------------------------------------------------------- what the phone typed (#22)


def test_notes_typed_on_the_phone_are_pulled_and_survive_a_push(server, connection, application):
    interview, href, body = _push_one_interview(server, connection, application)
    server.resources[href] = (
        body.replace("END:VEVENT", "DESCRIPTION:Bring the portfolio\r\nEND:VEVENT"),
        '"phone-1"',
    )
    assert sync(connection).pulled == 1
    interview.refresh_from_db()
    assert interview.notes == "Bring the portfolio"

    Interview.objects.filter(pk=interview.pk).update(location="Room 9")
    assert sync(connection).pushed == 1
    (event,) = text.text_to_events(server.resources[href][0])
    assert "Bring the portfolio" in event.description, "the push did not erase what was typed"
    assert event.location == "Room 9"
    assert "BEGIN:VALARM" in server.resources[href][0], "the alarm is still Postulo's"


def test_notes_extended_on_the_phone_are_pulled(server, connection, application):
    interview, href, body = _push_one_interview(server, connection, application)
    Interview.objects.filter(pk=interview.pk).update(notes="Bring CV")
    server.resources[href] = (
        body.replace("END:VEVENT", "DESCRIPTION:Bring CV and the portfolio\r\nEND:VEVENT"),
        '"phone-1"',
    )
    sync(connection)
    interview.refresh_from_db()
    assert interview.notes == "Bring CV and the portfolio"
    sync(connection)
    interview.refresh_from_db()
    assert interview.notes == "Bring CV and the portfolio", "the same text is never added twice"


def test_postulos_own_lines_are_not_taken_for_notes(
    server, connection, application, recruiter, settings
):
    settings.POSTULO_PUBLIC_URL = "https://postulo.example.org"
    interview = schedule_interview(
        application, kind="video", starts_at=next_week(), contacts=[recruiter]
    )
    sync(connection)
    (href,) = events(server)
    body, _ = server.resources[href]
    assert "With: Cave Johnson (CEO)" in body
    assert f"https://postulo.example.org{application.get_absolute_url()}" in body
    server.resources[href] = (body, '"phone-1"')  # touched, not changed
    sync(connection)
    interview.refresh_from_db()
    assert interview.notes == ""


def test_the_event_carries_no_link_when_the_instance_does_not_know_its_address(
    server, connection, application, settings
):
    settings.POSTULO_PUBLIC_URL = ""
    schedule_interview(application, kind="video", starts_at=next_week())
    sync(connection)
    (body,) = events(server).values()
    assert "http" not in body and "URL:" not in body


def test_a_server_that_refuses_a_method_is_written_to_without_one(server, connection, application):
    server.reject_method = True
    schedule_interview(application, kind="video", starts_at=next_week())
    report = sync(connection)
    assert report.pushed == 1 and report.error == ""
    (body,) = events(server).values()
    assert "METHOD" not in body
    assert [m for m, _p in server.log].count("PUT") == 2


def test_a_write_answered_without_an_etag_asks_for_that_resource_alone(
    made, connection, user, company, application
):
    server = made
    server.put_etag = False
    for number in range(3):
        Contact.objects.create(owner=user, company=company, name=f"Person {number}")
    schedule_interview(application, kind="phone", starts_at=next_week())
    report = sync(connection)
    assert report.pushed == 4 and report.error == ""
    # One listing of each collection, the run's own. Asking the collection again for each
    # write's ETag made it 1 + 3 listings of the address book and 1 + 1 of the calendar
    # (#46); each write now costs one Depth 0 PROPFIND of the resource it wrote.
    listings = [path for path, depth in server.propfinds if depth == "1"]
    assert listings.count(BOOK) == 1 and listings.count(CALENDAR_PATH) == 1
    asked = [
        path for path, depth in server.propfinds if depth == "0" and path.endswith((".vcf", ".ics"))
    ]
    assert len(asked) == 4
    # The ETags were remembered, so the next run reads and writes nothing.
    for link in SyncLink.objects.filter(connection=connection):
        assert link.etag == server.resources[unquote(urlsplit(link.remote_href).path)][1]
    seen = len(server.log)
    sync(connection)
    assert not [m for m, _p in server.log[seen:] if m in ("GET", "PUT")]


# ------------------------------------------------------------------ multiget (#45)


def foreign_event(uid: str) -> str:
    return (
        f"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:20260910T090000Z\r\n"
        "DTEND:20260910T100000Z\r\nSUMMARY:Dentist & co\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def test_a_first_run_reads_the_address_book_in_a_few_reports(made, connection):
    server = made
    for number in range(120):
        server.add(f"{BOOK}card-{number}.vcf", vcard(f"u{number}", f"Person {number} & co"))
    report = sync(connection)
    assert report.pulled == 120 and report.error == ""
    assert Contact.objects.filter(name="Person 7 & co").exists()
    # A GET per card made it 120 requests; three REPORTs of at most fifty now.
    assert not [p for m, p in server.log if m == "GET"]
    assert server.multigets == [50, 50, 20]


def test_a_card_changed_on_the_phone_is_read_through_a_report(server, connection, recruiter):
    sync(connection)
    (href,) = cards(server)
    body, _etag = server.resources[href]
    server.resources[href] = (body.replace("TITLE:CEO", "TITLE:Founder"), '"phone-1"')
    seen = len(server.log)
    report = sync(connection)
    assert report.pulled == 1
    recruiter.refresh_from_db()
    assert recruiter.role == "Founder"
    assert [m for m, _p in server.log[seen:] if m in ("GET", "REPORT")] == ["REPORT"]
    assert SyncLink.for_record(connection, recruiter).etag == '"phone-1"'


def test_read_only_reads_the_foreign_events_in_reports_on_every_run(made, connection, application):
    server = made
    schedule_interview(application, kind="phone", starts_at=next_week())
    for number in range(60):
        server.add(f"{CALENDAR_PATH}dentist-{number}.ics", foreign_event(f"d{number}"))
    connection.config["read_only"] = True
    connection.save()
    for _run in range(2):
        seen, asked = len(server.log), len(server.multigets)
        report = sync(connection)
        assert "60 events on the calendar are not from Postulo" in report.summary()
        # Nothing is pushed in read-only mode, so the foreign events are read on every run:
        # 60 GETs a run before, two REPORTs now.
        assert not [m for m, _p in server.log[seen:] if m in ("GET", "PUT")]
        assert server.multigets[asked:] == [50, 10]


def test_a_report_too_large_is_asked_again_in_halves(made, connection, monkeypatch):
    server = made
    for number in range(8):
        server.add(f"{BOOK}card-{number}.vcf", vcard(f"u{number}", f"Person {number}"))
    one = len(
        _multistatus(
            [("x", "<c:address-data>" + vcard("u0", "Person 0") + "</c:address-data>")]
        ).content
    )
    monkeypatch.setattr(dav_module, "MULTIGET_LIMIT", 3 * one)
    report = sync(connection)
    assert report.pulled == 8 and report.error == ""
    assert not [p for m, p in server.log if m == "GET"]
    assert server.multigets == [8, 4, 2, 2, 4, 2, 2]


def test_a_server_that_refuses_the_report_is_read_one_card_at_a_time(made, connection):
    server = made
    server.multiget = False
    for number in range(3):
        server.add(f"{BOOK}card-{number}.vcf", vcard(f"u{number}", f"Person {number}"))
    report = sync(connection)
    assert report.pulled == 3 and report.error == ""
    assert len([p for m, p in server.log if m == "GET"]) == 3


# ------------------------------------------------------------------ queries (#47)


def queries_of_an_idle_run(connection) -> int:
    with CaptureQueriesContext(db_connection) as queries:
        report = sync(connection)
    assert report.error == "" and report.pushed == report.pulled == 0
    return len(queries)


def test_an_idle_run_asks_nothing_per_old_interview(made, connection, application):
    def add_old_interviews(count: int) -> None:
        for _number in range(count):
            schedule_interview(application, kind="phone", starts_at=next_week())
        sync(connection)
        # Older than the window: no longer synced, but their links and events are kept.
        Interview.objects.update(starts_at=timezone.now() - 2 * HISTORY)
        sync(connection)

    add_old_interviews(1)
    one = queries_of_an_idle_run(connection)
    add_old_interviews(4)
    five = queries_of_an_idle_run(connection)
    assert SyncLink.objects.filter(connection=connection).count() == 5
    assert len(events(made)) == 5
    # One `exists()` per old interview made it 4 more; one query for all of them now.
    assert five == one


def test_an_idle_run_asks_nothing_per_unchanged_contact(made, connection, user, company):
    made_so_far = []

    def add_contacts(count: int) -> None:
        for _number in range(count):
            number = len(made_so_far)
            contact = Contact.objects.create(owner=user, company=company, name=f"P {number}")
            save_phone_number(contact, user, f"+1 555 01{number:02d}")
            save_web_link(contact, user, LinkKind.SOCIAL, f"https://example.org/in/p{number}")
            made_so_far.append(contact)
        sync(connection)
        sync(connection)

    add_contacts(2)
    two = queries_of_an_idle_run(connection)
    add_contacts(3)
    five = queries_of_an_idle_run(connection)
    assert len(cards(made)) == 5
    assert all("TEL" in body and "URL" in body for body in cards(made).values())
    # Each unchanged contact cost a SAVEPOINT and a RELEASE, and two queries for its
    # primary number and link. The savepoint is gone; the two queries go too with a core
    # that reads the primaries from the rows prefetched (postulo/postulo#1171).
    contact = Contact.objects.prefetch_related("phone_numbers").get(pk=made_so_far[0].pk)
    with CaptureQueriesContext(db_connection) as asked:
        primary_phone_number(contact)
    per_contact = 0 if len(asked) == 0 else 2
    assert five - two == 3 * per_contact


# ----------------------------------------------------------- numbers and links (#26 #32)


def test_a_number_held_elsewhere_is_a_note_and_the_card_stays_changed(
    server, connection, recruiter, user, monkeypatch
):
    from postulo.core import phone_numbers

    from postulo_dav import sync as sync_module

    # A number somebody holds as their own on the instance: the core's answer is a yes, and
    # which rows it counts is the core's business, so the answer is given here.
    monkeypatch.setattr(
        sync_module, "phone_number_is_taken", lambda number, **kwargs: number == "+1 555 0111"
    )
    sync(connection)
    (href,) = cards(server)
    body, _ = server.resources[href]
    server.resources[href] = (
        body.replace("TEL;TYPE=work,voice:+1 555 0100", "TEL:+1 555 0111"),
        '"phone-1"',
    )
    report = sync(connection)
    assert "already held" in report.summary()
    assert phone_numbers.primary_for(recruiter).number == "+1 555 0100"
    assert sync(connection).summary() != "nothing to do", "the card is still seen as changed"

    recruiter.role = "Chairman"
    recruiter.save()
    sync(connection)
    note = Contact.objects.get(pk=recruiter.pk).note_for(user)
    assert "+1 555 0111" in note, "the phone's number was not overwritten without a word"


def test_a_card_without_a_number_leaves_the_number_here_alone(server, connection, recruiter):
    from postulo.core import phone_numbers

    sync(connection)
    (href,) = cards(server)
    body, _ = server.resources[href]
    body = re.sub(r"TEL[^\r\n]*\r\n", "", body).replace("TITLE:CEO", "TITLE:Founder")
    server.resources[href] = (body, '"phone-1"')
    sync(connection)
    recruiter.refresh_from_db()
    assert recruiter.role == "Founder"
    assert phone_numbers.primary_for(recruiter).number == "+1 555 0100"


def test_a_card_without_a_web_address_leaves_the_link_here_alone(server, connection, recruiter):
    from postulo.core import web_links

    web_links.save_only_link(
        recruiter, recruiter.owner, LinkKind.SOCIAL, "https://example.org/in/c"
    )
    sync(connection)
    (href,) = cards(server)
    body, _ = server.resources[href]
    body = re.sub(r"URL[^\r\n]*\r\n", "", body).replace("TITLE:CEO", "TITLE:Founder")
    server.resources[href] = (body, '"phone-1"')
    sync(connection)
    assert web_links.primary_for(recruiter, LinkKind.SOCIAL).url == "https://example.org/in/c"


def test_a_web_address_that_is_not_a_web_address_is_not_taken(made, connection):
    server = made
    server.add(BOOK + "a.vcf", vcard("a", "Plain Url", URL="javascript:alert(1)"))
    server.add(
        BOOK + "b.vcf",
        vcard("b", "Grouped Url", **{"item1.URL": "data:text/html,<b>x</b>"}),
    )
    server.add(BOOK + "c.vcf", vcard("c", "Good Url", URL="https://example.org/good"))
    report = sync(connection)
    assert report.error == ""
    assert "not a web address" in report.summary()
    for name in ("Plain Url", "Grouped Url"):
        assert Contact.objects.get(name=name).web_links.count() == 0
    assert [link.url for link in Contact.objects.get(name="Good Url").web_links.all()] == [
        "https://example.org/good"
    ]


# --------------------------------------------------------------- notes per reader (#30)


def test_a_contacts_note_is_the_connection_owners_own(server, connection, recruiter, user):
    recruiter.write_note(user, "likes lemons")
    sync(connection)
    (body,) = cards(server).values()
    assert "NOTE:likes lemons" in body
    (href,) = cards(server)
    server.resources[href] = (body.replace("likes lemons", "likes oranges"), '"phone-1"')
    sync(connection)
    assert Contact.objects.get(pk=recruiter.pk).note_for(user) == "likes oranges"


def test_another_persons_contact_is_not_synced(server, connection, recruiter, company):
    from django.contrib.auth import get_user_model

    stranger = get_user_model().objects.create_user(email="bo@example.org", password="pw" * 8)  # noqa: S106
    Contact.objects.create(owner=stranger, name="Somebody Else")
    sync(connection)
    assert len(cards(server)) == 1 and "Somebody Else" not in "".join(cards(server).values())


# -------------------------------------------------------------------- the address (#19)


def test_a_server_that_escapes_its_hrefs_does_not_make_every_event_vanish(
    server, connection, application
):
    server.quote_listing = True
    schedule_interview(application, kind="panel", starts_at=next_week())
    assert sync(connection).pushed == 1
    link = SyncLink.of_model(connection, Interview).get()
    assert sync(connection).summary() == "nothing to do"
    link.refresh_from_db()
    assert not link.remote_gone
    assert not ApplicationEvent.objects.filter(
        application=application, summary__contains="removed from the calendar"
    )


def test_a_link_stored_with_a_literal_at_sign_still_matches_an_escaped_listing(
    server, connection, application
):
    server.quote_listing = True
    interview = schedule_interview(application, kind="panel", starts_at=next_week())
    sync(connection)
    link = SyncLink.of_model(connection, Interview).get()
    legacy = f"{BASE}{CALENDAR_PATH}{interview.uid}.ics"
    (old,) = events(server)
    server.resources[legacy.removeprefix(BASE)] = server.resources.pop(old)
    link.remote_href = legacy
    link.save()
    assert sync(connection).summary() == "nothing to do"
    link.refresh_from_db()
    assert not link.remote_gone


def test_an_interview_is_named_for_the_part_of_its_uid_with_no_reserved_character(
    server, connection, application
):
    interview = schedule_interview(application, kind="panel", starts_at=next_week())
    sync(connection)
    (href,) = events(server)
    assert "@" not in href and href.endswith(f"{interview.uid.split('@')[0]}.ics")


# ----------------------------------------------------------------- discovery (#23 #28)


class UnderAPath:
    """A DAV server installed under ``/baikal/``: nothing answers at the root of the host."""

    def __init__(self, fake: FakeDav) -> None:
        self.fake = fake
        self.root = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/baikal/"):
            self.root.append(path)
            return httpx.Response(404)
        inner = httpx.Request(
            request.method,
            request.url.copy_with(path=path.removeprefix("/baikal")),
            headers=request.headers,
            content=request.content,
        )
        response = self.fake.handler(inner)
        if response.status_code == 207:
            body = response.text.replace("<d:href>/", "<d:href>/baikal/")
            response = httpx.Response(207, content=body.encode(), headers=response.headers)
        return response


def test_a_server_under_a_path_connects_from_the_address_typed(monkeypatch):
    fake = FakeDav()
    fake.collections["/dav/addressbooks/ana/postulo-contacts/"] = {
        "name": "Postulo",
        "kind": ADDRESSBOOK,
    }
    fake.collections["/dav/calendars/ana/postulo-interviews/"] = {
        "name": "Postulo",
        "kind": CALENDAR,
    }
    under = UnderAPath(fake)

    def client(**kwargs):
        kwargs.pop("event_hooks", None)
        kwargs.pop("total", None)
        return httpx.Client(transport=httpx.MockTransport(under.handler), **kwargs)

    monkeypatch.setattr(dav_module, "postulo_client", client)
    result = DavSync().test({**CONFIG, "url": f"{BASE}/baikal/dav/"})
    assert result.ok, result.message
    assert result.message == "Found the address book “Postulo” and the calendar “Postulo”."
    assert under.root == [], "nothing was asked at the root of the host"


# ------------------------------------------------- what the server sends (#13 #33)


def test_a_card_over_the_limit_is_skipped_and_the_next_is_read(made, connection):
    server = made
    server.add(BOOK + "a-huge.vcf", vcard("h", "Huge Person", NOTE="x" * dav_module.RESOURCE_LIMIT))
    server.add(BOOK + "b-small.vcf", vcard("s", "Small Person"))
    report = sync(connection)
    assert Contact.objects.filter(name="Small Person").exists()
    assert not Contact.objects.filter(name="Huge Person").exists()
    assert "a-huge.vcf" in report.summary() and "too large" in report.summary()


def test_a_listing_over_the_limit_is_reported_and_the_other_half_still_runs(
    made, connection, application, monkeypatch
):
    server = made
    for number in range(40):
        server.add(f"{BOOK}card-{number}.vcf", vcard(f"u{number}", f"Person {number}"))
    schedule_interview(application, kind="phone", starts_at=next_week())
    monkeypatch.setattr(dav_module, "LISTING_LIMIT", 2000)
    report = sync(connection)
    assert "too large" in report.error
    assert Contact.objects.count() == 0
    assert len(events(server)) == 1


def test_the_password_goes_only_where_the_person_pointed_it(made, connection):
    server = made
    server.foreign_hrefs = [
        "https://evil.example/dav/addressbooks/ana/postulo-contacts/x.vcf",
        "/dav/addressbooks/ana/other/y.vcf",
        "/dav/addressbooks/ana/postulo-contacts/../other/z.vcf",
    ]
    report = sync(connection)
    assert report.error == ""
    assert set(server.hosts) == {"cloud.example.org"}
    assert not [p for _m, p in server.log if "other" in p or "evil" in p or "foreign" in p]
    assert Contact.objects.count() == 0


def test_a_redirect_to_another_host_is_not_followed(server, connection):
    server.well_known_location = "https://evil.example/dav/"
    report = sync(connection)
    assert report.error, "discovery fails rather than going there"
    assert "evil.example" not in server.hosts


def test_the_client_is_held_to_a_total_and_follows_no_redirect_itself(monkeypatch):
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return httpx.Client()

    monkeypatch.setattr(dav_module, "postulo_client", spy)
    client, _server = dav_module.connect(CONFIG)
    client.close()
    assert seen == {"total": 60.0, "follow_redirects": False}


# ---------------------------------------------------------------- pairing by UID (#7)


def test_an_adopted_card_keeps_its_uid_when_it_is_edited_here(made, connection, user):
    server = made
    server.enforce_uid = True
    server.add(BOOK + "phone.vcf", vcard("phone-uid-1", "Gordon Freeman", EMAIL="g@bm.test"))
    assert sync(connection).pulled == 1
    contact = Contact.objects.get(name="Gordon Freeman")
    contact.role = "Physicist"
    contact.save()
    report = sync(connection)
    assert report.error == "" and report.pushed == 1
    body, _ = server.resources[BOOK + "phone.vcf"]
    assert "UID:phone-uid-1" in body and "TITLE:Physicist" in body


def test_a_card_moved_to_a_new_address_stays_paired(server, connection, recruiter):
    server.enforce_uid = True
    sync(connection)
    (href,) = cards(server)
    server.resources[BOOK + "renamed.vcf"] = server.resources.pop(href)
    report = sync(connection)
    assert report.error == "" and Contact.objects.count() == 1
    link = SyncLink.for_record(connection, recruiter)
    assert link.remote_href.endswith("renamed.vcf") and not link.remote_gone
    assert "moved to another address" in report.summary()
    recruiter.role = "Chairman"
    recruiter.save()
    assert sync(connection).pushed == 1
    assert "TITLE:Chairman" in server.resources[BOOK + "renamed.vcf"][0]


def test_a_flagged_card_that_comes_back_is_paired_again(server, connection, recruiter):
    sync(connection)
    (href,) = cards(server)
    saved = server.resources.pop(href)
    sync(connection)
    assert SyncLink.for_record(connection, recruiter).remote_gone
    server.resources[BOOK + "back.vcf"] = saved
    sync(connection)
    link = SyncLink.for_record(connection, recruiter)
    assert not link.remote_gone and link.remote_href.endswith("back.vcf")
    assert Contact.objects.count() == 1


def test_deleting_and_adding_a_connection_again_changes_no_counts(
    server, connection, recruiter, application, user
):
    server.enforce_uid = True
    server.add(BOOK + "phone.vcf", vcard("phone-uid-1", "Gordon Freeman", EMAIL="g@bm.test"))
    schedule_interview(application, kind="onsite", starts_at=next_week())
    first = sync(connection)
    assert first.error == ""
    contacts, cards_before, events_before = (
        Contact.objects.count(),
        len(cards(server)),
        len(events(server)),
    )

    connection.delete()
    assert SyncLink.objects.count() == 0
    second = sync(new_connection(user))
    assert second.error == "", second.error
    assert Contact.objects.count() == contacts
    assert len(cards(server)) == cards_before and len(events(server)) == events_before
    assert SyncLink.objects.count() == contacts + 1
    assert sync(SyncLink.objects.first().connection).summary() == "nothing to do"


def test_an_interview_whose_event_is_already_there_is_paired_not_pushed_again(
    server, connection, application, user
):
    server.enforce_uid = True
    interview = schedule_interview(application, kind="onsite", starts_at=next_week())
    sync(connection)
    connection.delete()
    report = sync(new_connection(user))
    assert report.error == "" and len(events(server)) == 1
    assert (
        SyncLink.of_model(SyncLink.objects.get().connection, Interview).get().uid == interview.uid
    )


def test_a_server_that_refuses_one_put_is_a_note_not_a_failed_run(
    server, connection, recruiter, application, user
):
    server.enforce_uid = True
    schedule_interview(application, kind="onsite", starts_at=next_week())
    # A card already in the collection under the UID the contact would be given.
    server.add(
        BOOK + "squatter.vcf", vcard(f"contact-{recruiter.pk}-{user.pk}@postulo", "Squatter")
    )
    report = sync(connection)
    assert report.error == ""
    assert len(events(server)) == 1, "the interviews half ran"


def test_an_interview_with_notes_and_a_reminder_moved_on_the_phone_keeps_its_notes(
    server, connection, application
):
    interview = schedule_interview(application, kind="onsite", starts_at=next_week())
    Interview.objects.filter(pk=interview.pk).update(notes="Ask about the on-call rota")
    sync(connection)
    (href,) = events(server)
    body, _ = server.resources[href]
    assert "BEGIN:VALARM" in body and "Ask about the on-call rota" not in body
    later = next_week(hour=15)
    body = re.sub(r"DTSTART:\S+", f"DTSTART:{text.stamp(later)}", body)
    body = re.sub(r"DTEND:\S+", f"DTEND:{text.stamp(later + dt.timedelta(hours=1))}", body)
    body = re.sub(r"LAST-MODIFIED:\S+", "LAST-MODIFIED:20990101T000000Z", body)
    server.resources[href] = (body, '"phone-9"')
    sync(connection)
    interview.refresh_from_db()
    assert interview.starts_at == later
    assert interview.notes == "Ask about the on-call rota", "the alarm's text did not replace them"
