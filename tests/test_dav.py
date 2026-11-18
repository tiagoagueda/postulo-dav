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
    Reminder,
    Status,
)
from postulo.applications.services import schedule_interview
from postulo.jobs.models import Company, Contact, JobPosting
from postulo.plugins import registry, syncing
from postulo.plugins.api import (
    LinkKind,
    primary_phone_number,
    reschedule_interview,
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
        #: The collection of every calendar-query REPORT (RFC 4791 §7.8).
        self.queries: list[str] = []
        #: A server that refuses to make a task list (a calendar for ``VTODO``).
        self.refuse_tasks = False
        #: A server that writes its own addresses in full, as ``https://host:443/…`` (#70):
        #: what goes before the path of every address a multistatus names. Empty, and they
        #: are paths, as most servers write them.
        self.authority = ""
        #: The homes the principal names. A server with calendars only names one (#70).
        self.homes: tuple[str, ...] = (ADDRESSBOOK, CALENDAR)

    @staticmethod
    def _collection_props(meta: dict) -> str:
        """What a listing says of a collection: its type, its name, and optionally what the
        account may do there (``privileges``) and which components it takes (``components``)."""
        kind = "<c:addressbook/>" if meta["kind"] == ADDRESSBOOK else "<cal:calendar/>"
        props = (
            f"<d:resourcetype><d:collection/>{kind}</d:resourcetype>"
            f"<d:displayname>{meta['name']}</d:displayname>"
        )
        if "components" in meta:
            comps = "".join(f'<cal:comp name="{c}"/>' for c in meta["components"])
            props += f"<cal:supported-calendar-component-set>{comps}</cal:supported-calendar-component-set>"  # noqa: E501
        if "versions" in meta:
            kinds = "".join(
                f'<c:address-data-type content-type="text/vcard" version="{v}"/>'
                for v in meta["versions"]
            )
            props += f"<c:supported-address-data>{kinds}</c:supported-address-data>"
        if "privileges" in meta:
            privs = "".join(
                f"<d:privilege><d:{name}/></d:privilege>" for name in meta["privileges"]
            )
            props += f"<d:current-user-privilege-set>{privs}</d:current-user-privilege-set>"
        return props

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
        if self.authority and response.status_code == 207:
            body = response.text.replace("<d:href>/", f"<d:href>{self.authority}/")
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
                homes = {
                    ADDRESSBOOK: "<c:addressbook-home-set><d:href>/dav/addressbooks/ana/</d:href></c:addressbook-home-set>",  # noqa: E501
                    CALENDAR: "<cal:calendar-home-set><d:href>/dav/calendars/ana/</d:href></cal:calendar-home-set>",  # noqa: E501
                }
                return _multistatus(
                    [("/dav/principals/ana/", "".join(homes[kind] for kind in self.homes))]
                )
            if path in ("/dav/addressbooks/ana/", "/dav/calendars/ana/") and depth == "1":
                rows = [(path, "<d:resourcetype><d:collection/></d:resourcetype>")]
                for href, meta in self.collections.items():
                    if href.startswith(path):
                        rows.append((href, self._collection_props(meta)))
                return _multistatus(rows)
            if path in self.collections and depth == "0":
                return _multistatus([(path, self._collection_props(self.collections[path]))])
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
            if "addressbook-query" in body:
                self.queries.append(path)
                found = re.search(r'match-type="contains">(.*?)<', body)
                needle = found.group(1).lower() if found else ""
                rows = [
                    (
                        href,
                        f"<d:getetag>{etag}</d:getetag>"
                        f"<c:address-data>{escape(text_)}</c:address-data>",
                    )
                    for href, (text_, etag) in self.resources.items()
                    if href.startswith(path)
                    and href.endswith(".vcf")
                    and needle in (re.search(r"^FN:(.*)$", text_, re.M) or [""] * 2)[1].lower()
                ]
                return _multistatus(rows)
            if "calendar-query" in body:
                self.queries.append(path)
                rows = [
                    (
                        href,
                        f"<d:getetag>{etag}</d:getetag>"
                        f"<cal:calendar-data>{escape(text_)}</cal:calendar-data>",
                    )
                    for href, (text_, etag) in self.resources.items()
                    if href.startswith(path) and href.endswith(".ics")
                ]
                return _multistatus(rows)
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
            if self.refuse_tasks and "VTODO" in body:
                return httpx.Response(403)  # a server that keeps no tasks
            self.collections[path] = {
                "name": name,
                "kind": ADDRESSBOOK if method == "MKCOL" else CALENDAR,
            }
            if method == "MKCALENDAR":
                self.collections[path]["components"] = re.findall(r'<c:comp name="(\w+)"/>', body)
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
    assert result.message == (
        "Found the address book “Postulo” (/dav/addressbooks/ana/postulo-contacts/) "
        "and the calendar “Postulo” (/dav/calendars/ana/postulo-interviews/)."
    )


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
        dav.allow("/dav/x/", dav_module.READ_WRITE)
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
    assert result.message == (
        "Found the address book “Postulo” (/baikal/dav/addressbooks/ana/postulo-contacts/) "
        "and the calendar “Postulo” (/baikal/dav/calendars/ana/postulo-interviews/)."
    )
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


# ------------------------------------------------ a default port is the same host (#70)

WITH_PORT = "https://cloud.example.org:443"


@pytest.fixture
def ported(server):
    """A server that writes every address of its own in full, default port included."""
    server.authority = WITH_PORT
    server.well_known_location = f"{WITH_PORT}/dav/"
    return server


def test_a_server_that_writes_its_default_port_is_found_and_synced(
    ported, connection, recruiter, application
):
    schedule_interview(application, kind="phone", starts_at=next_week())
    found = DavSync().discover(CONFIG)["collections"]
    assert {option.value for option in found} == {f"{BASE}{CONTACTS}", f"{BASE}{PERSONAL}"}
    result = DavSync().test(CONFIG)
    assert result.ok, result.message
    report = sync(connection)
    assert report.error == "" and report.pushed == 2, report.summary()
    assert len(cards(ported)) == 1 and len(events(ported)) == 1
    # What is kept carries no port: it is the same address whoever wrote it.
    kept = [link.remote_href for link in SyncLink.objects.filter(connection=connection)]
    assert len(kept) == 2 and all(href.startswith(f"{BASE}/dav/") for href in kept), kept
    assert sync(connection).summary() == "nothing to do"
    assert set(ported.hosts) == {"cloud.example.org"}


def test_a_collection_chosen_under_the_port_is_the_one_without_it(made, connection, recruiter):
    made.authority = WITH_PORT
    full = choose_hrefs(connection, {f"{WITH_PORT}{BOOK}": "readwrite"})
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.pushed == 1, report.summary()
    assert SyncLink.for_record(connection, recruiter).remote_href.startswith(f"{BASE}{BOOK}")
    assert DavSync().sync(connection, full).summary() == "nothing to do"


def test_the_canonical_address_carries_no_default_port(server):
    with httpx.Client(transport=httpx.MockTransport(server.handler)) as client:
        for typed in (BASE, WITH_PORT, f"{WITH_PORT}/dav/"):
            dav = Server(typed, "ana", "app-pw", client=client)
            one = f"{BASE}{BOOK}a%40b.vcf"
            assert dav.canonical(f"{WITH_PORT}{BOOK}a@b.vcf") == one
            assert dav.canonical(f"{BASE}{BOOK}a%40b.vcf") == one
            assert dav.canonical(f"{BOOK}a@b.vcf") == one
            assert dav.absolute(f"{WITH_PORT}{BOOK}?x=1") == f"{BASE}{BOOK}?x=1"
        plain = Server("http://nas.local:80/dav", "ana", "app-pw", client=client)
        assert plain.canonical("/dav/x") == "http://nas.local/dav/x"
        # A port that is not the scheme's own is part of the address, and stays.
        other = Server("https://cloud.example.org:8443", "ana", "app-pw", client=client)
        assert other.canonical("/dav/x") == "https://cloud.example.org:8443/dav/x"
        with pytest.raises(dav_module.DavError):
            other.absolute(f"{WITH_PORT}/dav/x")


def test_an_address_typed_with_its_default_port_syncs_and_keeps_none(
    made, connection, recruiter, company
):
    connection.config = {**connection.config, "url": WITH_PORT}
    connection.save()
    # Two links from before: one kept under the address as it was typed, one elsewhere.
    under = f"{WITH_PORT}{BOOK}"
    SyncLink.bind(connection, company, collection=under, uid="org-1", slot="was")
    SyncLink.bind(connection, company, collection="https://other.example/book/", uid="org-2")
    report = sync(connection)
    assert report.error == "" and report.pushed == 1, report.summary()
    assert SyncLink.for_record(connection, recruiter).remote_href.startswith(f"{BASE}{BOOK}")
    # The collection is compared as text by Postulo, so it is rewritten; another server's is not.
    assert SyncLink.for_record(connection, company, slot="was").collection == f"{BASE}{BOOK}"
    assert SyncLink.for_record(connection, company).collection == "https://other.example/book/"
    assert sync(connection).summary() == "nothing to do"


def test_another_port_is_still_another_host(made, connection):
    server = made
    server.foreign_hrefs = [
        f"https://cloud.example.org:8443{BOOK}a.vcf",
        f"http://cloud.example.org:443{BOOK}b.vcf",
        f"https://evil.example:443{BOOK}c.vcf",
        f"https://cloud.example.org:port{BOOK}d.vcf",
        f"https://cloud.example.org:99999{BOOK}e.vcf",
    ]
    report = sync(connection)
    assert report.error == "", report.error
    assert set(server.hosts) == {"cloud.example.org"}
    assert not [p for _m, p in server.log if p.endswith(".vcf")], server.log
    assert Contact.objects.count() == 0


def test_a_redirect_to_another_port_is_not_followed(server, connection):
    server.well_known_location = "https://cloud.example.org:8443/dav/"
    report = sync(connection)
    assert report.error, "discovery fails rather than going there"
    assert ("PROPFIND", "/dav/") not in server.log


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


# ------------------------------------- collections chosen by address, with a mode (#13)

CONTACTS = "/dav/addressbooks/ana/contacts/"
PERSONAL = "/dav/calendars/ana/personal/"
DISCOVERY_PATHS = (
    "/",
    "/dav/",
    "/dav/principals/ana/",
    "/dav/addressbooks/ana/",
    "/dav/calendars/ana/",
    "/.well-known/carddav",
    "/.well-known/caldav",
)


def choose_hrefs(connection, chosen: dict[str, str]) -> dict:
    connection.config = {**connection.config, "collections": chosen}
    connection.save()
    return connection.full_config


def requests_under(server: FakeDav, *roots: str) -> list[tuple[str, str]]:
    return [(m, p) for m, p in server.log if p.startswith(roots)]


def test_a_chosen_collection_is_used_by_address_and_nothing_else_is_asked(
    made, connection, recruiter, application
):
    server = made
    server.add(CONTACTS + "mine.vcf", vcard("personal-1", "Somebody Personal"))
    server.add(PERSONAL + "private.ics", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    schedule_interview(application, kind="phone", starts_at=next_week())
    # The names typed match the person's own book and calendar: they must not be taken.
    config = {**CONFIG, "addressbook": "Contacts", "calendar": "Personal"}
    connection.config = {**config, "collections": {}}
    full = choose_hrefs(connection, {BOOK: "readwrite", CALENDAR_PATH: "readwrite"})
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.pushed == 2
    assert not Contact.objects.filter(name="Somebody Personal").exists()
    assert set(server.hosts) == {"cloud.example.org"}
    # The homes are not listed, and nothing is asked outside the two collections.
    assert not [(m, p) for m, p in server.log if p in DISCOVERY_PATHS]
    assert all(p.startswith((BOOK, CALENDAR_PATH)) for _m, p in server.log), server.log
    assert server.resources[PERSONAL + "private.ics"]
    assert not requests_under(server, CONTACTS, PERSONAL)


def test_hostile_hrefs_in_a_chosen_collection_get_no_request(made, connection, recruiter):
    server = made
    server.foreign_hrefs = [
        "https://evil.example/dav/addressbooks/ana/postulo-contacts/x.vcf",
        "http://cloud.example.org/dav/addressbooks/ana/postulo-contacts/y.vcf",
        "/dav/addressbooks/ana/contacts/z.vcf",
        "/dav/addressbooks/ana/postulo-contacts/../contacts/w.vcf",
        "/dav/addressbooks/ana/postulo-contacts-evil/v.vcf",
    ]
    full = choose_hrefs(connection, {BOOK: "readwrite"})
    report = DavSync().sync(connection, full)
    assert report.error == ""
    assert set(server.hosts) == {"cloud.example.org"}
    assert all(p.startswith(BOOK) for _m, p in server.log), server.log
    assert Contact.objects.count() == 1  # the recruiter, and nothing adopted from a stranger


def test_the_guard_sends_nothing_outside_the_chosen_collection_or_the_mode(server):
    with httpx.Client(transport=httpx.MockTransport(server.handler)) as client:
        dav = Server(BASE, "ana", "app-pw", client=client)
        dav.allow(BOOK, dav_module.READ)
        for method, path in (
            ("PUT", BOOK + "a.vcf"),
            ("DELETE", BOOK + "a.vcf"),
            ("MKCOL", BOOK + "b/"),
            ("MKCALENDAR", BOOK + "b/"),
            ("GET", CONTACTS + "a.vcf"),
            ("GET", BOOK + "../contacts/a.vcf"),
            ("GET", "/dav/addressbooks/ana/postulo-contacts-evil/a.vcf"),
            ("PROPPATCH", BOOK),
        ):
            with pytest.raises(dav_module.DavError):
                dav.request(method, path)
        assert server.log == []
        assert dav.request("GET", BOOK + "a.vcf").status_code == 404
        assert server.log == [("GET", BOOK + "a.vcf")]


def test_a_collection_read_only_gets_no_write_and_no_mkcol(made, connection, recruiter):
    server = made
    server.add(BOOK + "phone.vcf", vcard("phone-1", "Gordon Freeman"))
    full = choose_hrefs(connection, {BOOK: "read"})
    report = DavSync().sync(connection, full)
    assert report.error == ""
    assert Contact.objects.filter(name="Gordon Freeman").exists()  # read: it comes in
    assert report.pushed == 0
    assert not [m for m, _p in server.log if m in ("PUT", "DELETE", "MKCOL", "MKCALENDAR")]


def test_a_calendar_with_no_card_book_chosen_syncs_only_that_half(made, connection, recruiter):
    server = made
    full = choose_hrefs(connection, {CALENDAR_PATH: "readwrite"})
    DavSync().sync(connection, full)
    assert not requests_under(server, "/dav/addressbooks/")
    assert not cards(server)


def test_a_server_side_rename_changes_nothing(made, connection, recruiter):
    server = made
    full = choose_hrefs(connection, {BOOK: "readwrite"})
    assert DavSync().sync(connection, full).pushed == 1
    server.collections[BOOK]["name"] = "Renamed by somebody"
    server.log.clear()
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.summary() == "nothing to do"
    assert not [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR")]
    assert not SyncLink.for_record(connection, recruiter).remote_gone


def test_a_collection_that_vanished_is_reported_and_not_made_again(made, connection, recruiter):
    server = made
    full = choose_hrefs(connection, {BOOK: "readwrite"})
    DavSync().sync(connection, full)
    del server.collections[BOOK]
    for href in list(server.resources):
        del server.resources[href]
    server.log.clear()
    report = DavSync().sync(connection, full)
    assert "no longer on the server" in report.summary()
    assert not [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR", "PUT", "DELETE")]
    assert not SyncLink.for_record(connection, recruiter).remote_gone  # nothing was concluded


def test_a_share_that_is_read_only_is_not_offered_writing(made, connection):
    server = made
    # Nextcloud grants write-properties on a read-only share (bugzilla 2051561).
    server.collections[BOOK]["privileges"] = ["read", "write-properties"]
    server.collections[CALENDAR_PATH]["privileges"] = ["read", "bind", "write-content"]
    found = DavSync().discover(CONFIG)["collections"]
    by_value = {option.value: option for option in found}
    assert by_value[f"{BASE}{BOOK}"].modes == (dav_module.READ,)
    assert by_value[f"{BASE}{BOOK}"].note
    assert by_value[f"{BASE}{CALENDAR_PATH}"].modes == ()
    assert not [m for m, _p in server.log if m not in ("PROPFIND",)]


def test_writing_chosen_for_a_read_only_collection_is_lowered_to_reading(
    made, connection, recruiter
):
    server = made
    server.collections[BOOK]["privileges"] = ["read", "write-properties"]
    full = choose_hrefs(connection, {f"{BASE}{BOOK}": "readwrite"})
    report = DavSync().sync(connection, full)
    assert "read only" in report.summary()
    assert not [m for m, _p in server.log if m in ("PUT", "DELETE")]


def test_discovery_lists_task_lists_apart_from_the_calendars(made, connection):
    server = made
    server.collections["/dav/calendars/ana/tasks/"] = {
        "name": "Tasks",
        "kind": CALENDAR,
        "components": ["VTODO"],
    }
    found = DavSync().discover(CONFIG)["collections"]
    labels = {(option.group, option.label) for option in found}
    assert ("Address books", "Contacts") in labels and ("Calendars", "Personal") in labels
    assert ("Task lists", "Tasks") in labels and ("Calendars", "Tasks") not in labels
    assert len({option.value for option in found}) == len(found)


def test_two_collections_of_a_kind_chosen_are_refused_rather_than_guessed(made, connection):
    server = made
    full = choose_hrefs(connection, {BOOK: "readwrite", CONTACTS: "readwrite"})
    report = DavSync().sync(connection, full)
    assert "More than one address book" in report.summary()
    assert not cards(server)
    assert not requests_under(server, BOOK + "x", CONTACTS + "x")


def test_pairings_outside_a_new_choice_are_left_alone_and_counted(
    made, connection, recruiter, application
):
    server = made
    schedule_interview(application, kind="phone", starts_at=next_week())
    DavSync().sync(connection, connection.full_config)  # the first, by name
    other = "/dav/addressbooks/ana/other-book/"
    server.collections[other] = {"name": "Other", "kind": ADDRESSBOOK}
    server.log.clear()
    full = choose_hrefs(connection, {other: "readwrite"})
    report = DavSync().sync(connection, full)
    assert report.error == "" and "outside the chosen collection" in report.summary()
    assert report.skipped == 1 and report.removed == 0 and report.pushed == 0
    assert not SyncLink.for_record(connection, recruiter).remote_gone
    assert not [m for m, _p in server.log if m in ("PUT", "DELETE", "MKCOL")]
    assert not requests_under(server, BOOK)


def test_a_name_two_collections_share_is_refused_at_test_and_at_each_run(server, connection):
    server.collections["/dav/addressbooks/ana/second/"] = {"name": "Contacts", "kind": ADDRESSBOOK}
    config = {**CONFIG, "addressbook": "Contacts"}
    result = DavSync().test(config)
    assert not result.ok and "More than one collection" in result.message
    report = DavSync().sync(connection, {**connection.full_config, "addressbook": "Contacts"})
    assert "More than one collection" in report.error
    assert not [m for m, _p in server.log if m in ("PUT", "DELETE", "MKCOL")]


def test_an_address_is_accepted_for_a_name_and_never_made(made, connection, recruiter):
    server = made
    config = {**connection.full_config, "addressbook": BOOK, "calendar": "/dav/calendars/ana/gone/"}
    report = DavSync().sync(connection, config)
    assert report.pushed == 1
    assert "is not on the server" in report.summary()
    assert not [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR")]


def test_test_names_what_it_found_by_address(made, connection):
    full = choose_hrefs(connection, {BOOK: "read", CALENDAR_PATH: "readwrite"})
    result = DavSync().test(full)
    assert result.ok
    assert "address book" in result.message and "read only" in result.message
    assert "calendar" in result.message and "read and write" in result.message


def test_the_form_has_a_discovered_field_with_two_modes():
    (spec,) = [f for f in DavSync().config_fields() if f.name == "collections"]
    assert spec.type == "discovered" and [m for m, _l in spec.modes] == ["read", "readwrite"]


# ------------------------------- pairing an event made elsewhere with an interview (#11)


def phone_event(uid: str, when: dt.datetime, *, organizer: bool = False, extra: str = "") -> str:
    start = when.astimezone(dt.UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Phone//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTART:{start:%Y%m%dT%H%M%SZ}",
        f"DTEND:{start + dt.timedelta(hours=1):%Y%m%dT%H%M%SZ}",
        "SUMMARY;LANGUAGE=pt-BR:Conversa com a Aperture",
        "LOCATION;LANGUAGE=pt-BR:Lisboa",
        "DESCRIPTION:Bring the portfolio",
        "URL:https://meet.example.org/abc",
        "ATTENDEE;CN=Cave:mailto:cave@example.org",
        "X-VENDOR-THING:keep me",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Ring ring",
        "TRIGGER:-PT15M",
        "END:VALARM",
    ]
    if organizer:
        lines.append("ORGANIZER:mailto:boss@example.org")
    lines += [extra] if extra else []
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def pair_with_phone_event(connection, interview, uid="phone-ev-1@example.org"):
    from postulo.plugins import pairing

    (item,) = DavSync().remote_items(connection.full_config, key=(f"{BASE}{CALENDAR_PATH}", uid))
    return pairing.pair(connection, item, interview)


@pytest.fixture
def phone(made, connection, application):
    """An interview, and an event the phone made on the same calendar, not yet paired."""
    interview = schedule_interview(application, kind="video", starts_at=next_week(9))
    interview.notes = "My own preparation"
    interview.save()
    href = CALENDAR_PATH + "phone.ics"
    made.add(href, phone_event("phone-ev-1@example.org", next_week(14)))
    made.href = href
    return interview


def test_events_from_elsewhere_are_offered_for_pairing_and_postulos_own_are_not(
    made, connection, application, phone
):
    DavSync().sync(connection, connection.full_config)  # Postulo's own event is there now
    found = DavSync().remote_items(connection.full_config)
    assert [(i.uid, i.label, i.kinds) for i in found] == [
        ("phone-ev-1@example.org", "Conversa com a Aperture", ("interview", "application"))
    ]
    assert found[0].href == f"{BASE}{made.href}" and found[0].collection == f"{BASE}{CALENDAR_PATH}"
    assert DavSync().remote_items(connection.full_config, search="aperture")
    assert not DavSync().remote_items(connection.full_config, search="nothing like it")
    assert not DavSync().remote_items(connection.full_config, kind="contact")
    assert [p for p in made.queries if p == CALENDAR_PATH] == [
        CALENDAR_PATH
    ] * 3  # a calendar-query each, never a full listing


def test_a_paired_event_keeps_its_uid_and_everything_postulo_does_not_own(made, connection, phone):
    made.enforce_uid = True
    DavSync().sync(connection, connection.full_config)
    pair_with_phone_event(connection, phone)
    report = DavSync().sync(connection, connection.full_config)
    assert report.error == ""
    phone.refresh_from_db()
    assert phone.starts_at == next_week(14)  # the event's time came in
    assert phone.notes == "My own preparation"  # its description is not the interview's notes
    # Postulo's own event for it is gone, so that the meeting is not shown twice.
    assert [h for h in events(made)] == [made.href]

    made.log.clear()
    reschedule_interview(phone, starts_at=next_week(16), ends_at=next_week(17), actor="test")
    phone.location = "Porto"
    phone.save()
    report = DavSync().sync(connection, connection.full_config)
    assert report.error == "" and report.pushed == 1
    body, _etag = made.resources[made.href]
    assert "UID:phone-ev-1@example.org" in body
    assert f"DTSTART:{next_week(16).astimezone(dt.UTC):%Y%m%dT%H%M%SZ}" in body
    assert "LOCATION:Porto" in body
    # Everything Postulo does not own comes back as it was, parameters included.
    for kept in (
        "SUMMARY;LANGUAGE=pt-BR:Conversa com a Aperture",
        "DESCRIPTION:Bring the portfolio",
        "URL:https://meet.example.org/abc",
        "ATTENDEE;CN=Cave:mailto:cave@example.org",
        "X-VENDOR-THING:keep me",
        "TRIGGER:-PT15M",
        "DESCRIPTION:Ring ring",
    ):
        assert kept in body
    assert "METHOD" not in body and "@postulo" not in body
    assert [m for m, _p in made.log if m == "PUT"] == ["PUT"]


def test_a_language_parameter_on_a_paired_event_survives_a_push(made, connection, phone):
    DavSync().sync(connection, connection.full_config)
    pair_with_phone_event(connection, phone)
    DavSync().sync(connection, connection.full_config)
    reschedule_interview(phone, starts_at=next_week(18), ends_at=next_week(19), actor="test")
    DavSync().sync(connection, connection.full_config)
    body, _etag = made.resources[made.href]
    assert "LOCATION;LANGUAGE=pt-BR:Lisboa" in body  # a place nobody changed is not rewritten
    assert "SUMMARY;LANGUAGE=pt-BR:Conversa com a Aperture" in body


def test_a_paired_event_moved_on_the_phone_moves_the_interview_and_not_its_notes(
    made, connection, phone
):
    DavSync().sync(connection, connection.full_config)
    pair_with_phone_event(connection, phone)
    DavSync().sync(connection, connection.full_config)
    body, _etag = made.resources[made.href]
    moved = (
        body.replace(
            f"DTSTART:{next_week(14).astimezone(dt.UTC):%Y%m%dT%H%M%SZ}",
            f"DTSTART:{next_week(15).astimezone(dt.UTC):%Y%m%dT%H%M%SZ}",
        )
        .replace(
            f"DTEND:{next_week(15).astimezone(dt.UTC):%Y%m%dT%H%M%SZ}",
            f"DTEND:{next_week(16).astimezone(dt.UTC):%Y%m%dT%H%M%SZ}",
        )
        .replace("Bring the portfolio", "Something else entirely")
    )
    made.add(made.href, moved)
    report = DavSync().sync(connection, connection.full_config)
    phone.refresh_from_db()
    assert report.pulled == 1 and phone.starts_at == next_week(15)
    assert phone.notes == "My own preparation"


def test_an_event_with_an_organizer_is_never_written_to(made, connection, phone):
    made.add(made.href, phone_event("phone-ev-1@example.org", next_week(14), organizer=True))
    DavSync().sync(connection, connection.full_config)
    pair_with_phone_event(connection, phone)
    DavSync().sync(connection, connection.full_config)
    reschedule_interview(phone, starts_at=next_week(16), ends_at=next_week(17), actor="test")
    made.log.clear()
    report = DavSync().sync(connection, connection.full_config)
    assert "organizer" in report.summary()
    assert not [m for m, p in made.log if m in ("PUT", "DELETE") and p == made.href]
    again = DavSync().sync(connection, connection.full_config)
    assert "organizer" not in again.summary()  # said once, not on every run


def test_deleting_the_interview_unpairs_it_and_leaves_the_event(made, connection, phone):
    DavSync().sync(connection, connection.full_config)
    pair_with_phone_event(connection, phone)
    DavSync().sync(connection, connection.full_config)
    phone.delete()
    made.log.clear()
    DavSync().sync(connection, connection.full_config)
    assert made.href in made.resources
    assert not [m for m, _p in made.log if m == "DELETE"]
    assert not SyncLink.objects.filter(connection=connection, uid="phone-ev-1@example.org")


def test_a_read_only_calendar_pairs_and_pulls_but_never_writes(made, connection, phone):
    full = choose_hrefs(connection, {CALENDAR_PATH: "read"})
    pair_with_phone_event(connection, phone)
    DavSync().sync(connection, full)
    phone.refresh_from_db()
    assert phone.starts_at == next_week(14)
    reschedule_interview(phone, starts_at=next_week(16), ends_at=next_week(17), actor="test")
    made.log.clear()
    DavSync().sync(connection, full)
    assert not [m for m, _p in made.log if m in ("PUT", "DELETE")]


def test_the_patch_changes_only_what_it_is_given_and_nothing_else():
    source = phone_event("x@example.org", next_week(14))
    patched = text.patch_event(source, "x@example.org", location="", modified=next_week(1))
    assert "LOCATION" not in patched
    assert (
        text.patch_event(source, "other@example.org", location="x", modified=next_week(1)) is None
    )
    # An alarm's own DESCRIPTION and TRIGGER are not touched, and the UID is not either.
    assert "TRIGGER:-PT15M" in patched and "UID:x@example.org" in patched
    assert text.text_to_events(patched)[0].description == "Bring the portfolio"


# ------------------------- an event paired with an application becomes its interview (#11)


def pair_with_application(connection, application, uid="phone-ev-1@example.org"):
    from postulo.plugins import pairing

    (item,) = DavSync().remote_items(
        connection.full_config, kind="application", key=(f"{BASE}{CALENDAR_PATH}", uid)
    )
    assert "application" in item.kinds
    return pairing.pair(connection, item, application)


def test_an_event_paired_with_an_application_becomes_an_interview_kept_in_step(
    made, connection, application
):
    made.enforce_uid = True
    href = CALENDAR_PATH + "phone.ics"
    made.add(href, phone_event("phone-ev-1@example.org", next_week(14)))
    pair_with_application(connection, application)
    report = DavSync().sync(connection, connection.full_config)
    assert report.error == "" and report.pulled == 1
    interview = Interview.objects.get(application=application)
    assert interview.starts_at == next_week(14) and interview.location == "Lisboa"
    assert interview.kind == "other" and interview.reminder_id
    # A second run makes nothing more, and writes nothing to the event.
    made.log.clear()
    DavSync().sync(connection, connection.full_config)
    assert Interview.objects.filter(application=application).count() == 1
    assert not [m for m, _p in made.log if m in ("PUT", "DELETE")]
    # Moved here: the event is patched, UID and the rest kept.
    reschedule_interview(interview, starts_at=next_week(16), ends_at=next_week(17), actor="test")
    DavSync().sync(connection, connection.full_config)
    body, _etag = made.resources[href]
    assert "UID:phone-ev-1@example.org" in body and "SUMMARY;LANGUAGE=pt-BR:" in body
    assert f"DTSTART:{next_week(16).astimezone(dt.UTC):%Y%m%dT%H%M%SZ}" in body
    assert Interview.objects.filter(application=application).count() == 1


def test_an_unpaired_application_link_makes_no_interview(made, connection, application):
    made.add(CALENDAR_PATH + "phone.ics", phone_event("phone-ev-1@example.org", next_week(14)))
    from postulo.plugins import pairing

    pairing.unpair(pair_with_application(connection, application))
    DavSync().sync(connection, connection.full_config)
    assert not Interview.objects.filter(application=application).exists()


def test_an_event_with_no_readable_time_is_noted_and_makes_nothing(made, connection, application):
    body = re.sub(
        r"DT(START|END):\d{8}T\d{6}Z",
        r"DT\1;VALUE=DATE:20261201",
        phone_event("phone-ev-1@example.org", next_week(14)),
    )
    made.add(CALENDAR_PATH + "phone.ics", body)
    made.add(CALENDAR_PATH + "ok.ics", phone_event("ok@example.org", next_week(9)))
    pair_with_application(connection, application, uid="phone-ev-1@example.org")
    report = DavSync().sync(connection, connection.full_config)
    assert "could not be made into an interview" in report.summary()
    assert not Interview.objects.exists()


# ------------------------------- pairing cards with contacts and companies (#8)


def phone_card(uid: str, name: str, **extra: str) -> str:
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"UID:{uid}",
        f"FN;LANGUAGE=pt-BR:{name}",
        "N:Freeman;Gordon;;;",
        "ORG:Black Mesa;Research",
        "TITLE:Physicist",
        "EMAIL;TYPE=home:gordon@bm.test",
        "TEL;TYPE=cell:+1 555 0100",
        "TEL;TYPE=home:+1 555 0199",
        "ADR;TYPE=home:;;1 Mesa Rd;Mesa;;;US",
        "BDAY:19700101",
        "PHOTO;ENCODING=b;TYPE=JPEG:AAAA",
        "X-VENDOR:keep me",
        "NOTE:Their private note",
    ]
    lines += [f"{k}:{v}" for k, v in extra.items()]
    return "\r\n".join([*lines, "END:VCARD"]) + "\r\n"


def test_the_card_patch_changes_only_what_is_given():
    source = phone_card("p-1", "Gordon Freeman")
    patched = text.patch_card(
        source, "p-1", {"TITLE": "Director", "ORG": "Aperture", "TEL": ""}, rev=next_week(1)
    )
    assert "TITLE:Director" in patched and "ORG:Aperture;Research" in patched
    assert "+1 555 0100" not in patched and "+1 555 0199" in patched  # the first number only
    for kept in (
        "UID:p-1",
        "VERSION:3.0",
        "FN;LANGUAGE=pt-BR:Gordon Freeman",
        "N:Freeman;Gordon;;;",
        "ADR;TYPE=home:;;1 Mesa Rd;Mesa;;;US",
        "BDAY:19700101",
        "PHOTO;ENCODING=b;TYPE=JPEG:AAAA",
        "X-VENDOR:keep me",
        "NOTE:Their private note",
        "EMAIL;TYPE=home:gordon@bm.test",
    ):
        assert kept in patched
    assert text.patch_card(source, "other", {"TITLE": "x"}, rev=next_week(1)) is None


def test_a_card_made_elsewhere_is_patched_when_the_contact_changes_here(made, connection):
    server = made
    server.enforce_uid = True
    server.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    assert sync(connection).pulled == 1
    contact = Contact.objects.get(name="Gordon Freeman")
    contact.role = "Director"
    contact.save()
    report = sync(connection)
    assert report.error == "" and report.pushed == 1
    body, _etag = server.resources[BOOK + "phone.vcf"]
    assert "TITLE:Director" in body and "UID:phone-uid-1" in body
    for kept in (
        "FN;LANGUAGE=pt-BR:Gordon Freeman",
        "ADR;TYPE=home:;;1 Mesa Rd;Mesa;;;US",
        "BDAY:19700101",
        "PHOTO;ENCODING=b;TYPE=JPEG:AAAA",
        "X-VENDOR:keep me",
        "TEL;TYPE=home:+1 555 0199",
        "NOTE:Their private note",
        "VERSION:3.0",
        "ORG:Black Mesa;Research",
    ):
        assert kept in body
    assert "VERSION:4.0" not in body


def test_notes_are_not_exchanged_with_a_card_made_elsewhere(made, connection):
    server = made
    server.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    sync(connection)
    contact = Contact.objects.get(name="Gordon Freeman")
    contact.write_note(contact.owner, "My private view of Gordon")
    contact.role = "Director"
    contact.save()
    sync(connection)
    body, _etag = server.resources[BOOK + "phone.vcf"]
    assert "My private view" not in body and "NOTE:Their private note" in body
    body = body.replace("Their private note", "Changed on the phone").replace(
        "TITLE:Director", "TITLE:Chief"
    )
    server.add(BOOK + "phone.vcf", body)
    sync(connection)
    contact = Contact.objects.get(name="Gordon Freeman")
    assert contact.role == "Chief"
    assert "My private view of Gordon" in contact.note_for(contact.owner)
    assert "Changed on the phone" not in contact.note_for(contact.owner)


def test_deleting_a_contact_unpairs_a_card_made_elsewhere_and_leaves_it(made, connection):
    server = made
    server.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    sync(connection)
    Contact.objects.get(name="Gordon Freeman").delete()
    server.log.clear()
    sync(connection)
    assert BOOK + "phone.vcf" in server.resources
    assert not [m for m, _p in server.log if m == "DELETE"]
    assert not SyncLink.objects.filter(connection=connection, uid="phone-uid-1")


def test_cards_are_offered_by_a_query_and_postulos_own_are_not(made, connection, recruiter):
    server = made
    sync(connection)  # the recruiter's own card is on the book now
    server.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    server.add(
        BOOK + "acme.vcf",
        "BEGIN:VCARD\r\nVERSION:4.0\r\nUID:acme-1\r\nKIND:org\r\nFN:Acme\r\nEND:VCARD\r\n",
    )
    found = DavSync().remote_items(connection.full_config, kind="contact")
    # Where the server names no card as the person's own, any card may be offered as *you*.
    assert [(i.uid, i.kinds) for i in found] == [("phone-uid-1", ("contact", "company", "you"))]
    assert sorted(
        i.uid for i in DavSync().remote_items(connection.full_config, kind="company")
    ) == ["acme-1", "phone-uid-1"]
    assert [i.uid for i in DavSync().remote_items(connection.full_config, search="acme")] == [
        "acme-1"
    ]
    assert BOOK in server.queries


def test_a_company_paired_with_a_card_keeps_name_and_website_in_step(made, connection, company):
    from postulo.plugins import pairing

    server = made
    server.enforce_uid = True
    server.add(
        BOOK + "acme.vcf",
        "BEGIN:VCARD\r\nVERSION:4.0\r\nUID:acme-1\r\nKIND:org\r\nFN:Aperture Labs\r\n"
        "URL:https://aperture.example.org\r\nADR:;;1 Rd;Town;;;PT\r\nNOTE:theirs\r\nEND:VCARD\r\n",
    )
    (item,) = DavSync().remote_items(
        connection.full_config, kind="company", key=(f"{BASE}{BOOK}", "acme-1")
    )
    pairing.pair(connection, item, company)
    report = sync(connection)
    company.refresh_from_db()
    assert report.error == "" and company.name == "Aperture Labs"
    assert company.website == "https://aperture.example.org"
    # Changed here: the card is patched, KIND, address and note kept.
    from postulo.plugins.api import update_company

    update_company(
        company, actor=company.owner, name="Aperture Science", website="https://as.example.org"
    )
    server.log.clear()
    report = sync(connection)
    assert report.pushed == 1, server.log
    body, _etag = server.resources[BOOK + "acme.vcf"]
    assert "FN:Aperture Science" in body and "URL:https://as.example.org" in body
    for kept in ("UID:acme-1", "KIND:org", "ADR:;;1 Rd;Town;;;PT", "NOTE:theirs", "VERSION:4.0"):
        assert kept in body
    assert sync(connection).summary() == "nothing to do"


# ------------------------------- importing one card (#9)


def import_card(connection, uid, kind, into=None, pair=False):
    from postulo.plugins.testing import create_by_hand

    return create_by_hand(
        connection, collection=f"{BASE}{BOOK}", uid=uid, kind=kind, into=into, pair=pair
    )


def test_a_company_card_becomes_a_new_company_and_writes_nothing_to_the_server(
    made, connection, user
):
    from postulo.jobs.models import Company

    made.add(
        BOOK + "acme.vcf",
        "BEGIN:VCARD\r\nVERSION:4.0\r\nUID:acme-1\r\nKIND:org\r\nFN:Acme Corp\r\n"
        "ORG:Acme Corp\r\nURL:https://acme.example.org\r\nTEL:+351 210 000 000\r\n"
        "EMAIL:hello@acme.example.org\r\nEND:VCARD\r\n",
    )
    made.log.clear()
    company = import_card(connection, "acme-1", "company")
    assert Company.objects.get(name="Acme Corp") == company
    assert company.website == "https://acme.example.org"
    from postulo.plugins.api import numbers_for

    note = company.note_for(user)
    # The number is a number of the company's now; only the email, which it has no place
    # for, goes into the notes.
    assert "hello@acme.example.org" in note
    assert [n.number.replace(" ", "") for n in numbers_for(company, user)] == ["+351210000000"]
    assert not [m for m, _p in made.log if m not in ("PROPFIND", "GET", "REPORT")]
    assert not SyncLink.objects.filter(connection=connection)  # a one-off: nothing paired


def test_a_card_fills_only_the_blanks_of_an_existing_company(made, connection, company):
    company.website = "https://mine.example.org"
    company.save()
    made.add(
        BOOK + "acme.vcf",
        "BEGIN:VCARD\r\nVERSION:4.0\r\nUID:acme-1\r\nKIND:org\r\nFN:Different Name\r\n"
        "URL:https://theirs.example.org\r\nEND:VCARD\r\n",
    )
    filled = import_card(connection, "acme-1", "company", into=company, pair=True)
    assert filled.pk == company.pk and filled.name == "Aperture Science"
    assert filled.website == "https://mine.example.org"
    assert SyncLink.for_record(connection, company).uid == "acme-1"


def test_a_card_becomes_a_contact_with_its_unit_as_department_or_no_company(made, connection, user):
    made.add(
        BOOK + "g.vcf",
        "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:g-1\r\nFN:Gordon Freeman\r\nORG:Black Mesa;Research\r\n"
        "TITLE:Physicist\r\nEMAIL:g@bm.test\r\nTEL:+351 910 000 001\r\nEND:VCARD\r\n",
    )
    made.add(
        BOOK + "h.vcf",
        "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:h-1\r\nFN:Alone Person\r\nEND:VCARD\r\n",
    )
    gordon = import_card(connection, "g-1", "contact")
    assert gordon.name == "Gordon Freeman" and gordon.company.name == "Black Mesa"
    assert gordon.role == "Physicist" and gordon.email == "g@bm.test"
    assert primary_phone_number(gordon).number
    alone = import_card(connection, "h-1", "contact")
    assert alone.company_id is None  # not the holding company
    assert Contact.objects.filter(owner=user).count() == 2


def test_a_card_outside_the_chosen_book_cannot_be_imported(made, connection):
    made.add("/dav/addressbooks/ana/contacts/x.vcf", vcard("x-1", "Somebody Personal"))
    from postulo.plugins.testing import create_by_hand

    full = choose_hrefs(connection, {BOOK: "read"})
    assert full
    with pytest.raises(Exception):  # noqa: B017,PT011 - the core's refusal wraps the plugin's
        create_by_hand(
            connection, collection=f"{BASE}{BOOK}", uid="x-1", kind="contact", pair=False
        )
    assert not Contact.objects.filter(name="Somebody Personal").exists()


# ------------------------------------- choosing what goes out (#10)


def with_config(connection, **extra):
    connection.config = {**connection.config, **extra}
    connection.save()
    return connection.full_config


def test_an_existing_connection_sends_all_its_contacts_and_nothing_more(
    made, connection, recruiter, company
):
    report = sync(connection)
    assert report.pushed == 1
    assert [b for b in cards(made).values() if "KIND:org" in b or "X-ABShowAs" in b] == []


def test_contacts_can_be_switched_off_and_the_run_says_how_many_were_left(
    made, connection, recruiter
):
    full = with_config(connection, send_contacts="none")
    report = DavSync().sync(connection, full)
    assert report.pushed == 0 and "1 records were not sent" in report.summary()
    assert not cards(made)


def test_switching_contacts_off_leaves_the_cards_and_adopts_none_of_them(
    made, connection, recruiter
):
    sync(connection)
    (href,) = cards(made)
    recruiter.role = "Founder"
    recruiter.save()
    full = with_config(connection, send_contacts="none")
    made.log.clear()
    report = DavSync().sync(connection, full)
    assert href in made.resources and "TITLE:Founder" not in made.resources[href][0]
    assert not [m for m, _p in made.log if m in ("PUT", "DELETE")]
    assert Contact.objects.count() == 1 and "not sent" in report.summary()


def test_a_company_goes_out_as_a_kind_org_card_in_a_vcard_4_book(made, connection, company):
    full = with_config(connection, send_companies=True, send_contacts="none")
    company.website = "https://aperture.example.org"
    company.save()
    report = DavSync().sync(connection, full)
    assert report.pushed == 1
    (body,) = cards(made).values()
    assert "VERSION:4.0" in body and "KIND:org" in body and "FN:Aperture Science" in body
    assert "URL:https://aperture.example.org" in body and "\nN:" not in body
    assert "X-ABShowAs" not in body
    assert DavSync().sync(connection, full).summary() == "nothing to do"
    # A card Postulo made for a company that is deleted here goes with it.
    company.delete()
    DavSync().sync(connection, full)
    assert not cards(made)


def test_a_book_that_lists_only_vcard_3_gets_vcard_3_and_x_abshowas(made, connection, company):
    made.collections[BOOK]["versions"] = ["3.0"]
    full = with_config(connection, send_companies=True, send_contacts="none")
    DavSync().sync(connection, full)
    (body,) = cards(made).values()
    assert "VERSION:3.0" in body and "X-ABShowAs:COMPANY" in body and "KIND:org" not in body


def test_switching_companies_off_leaves_their_cards_and_pushes_nothing_more(
    made, connection, company
):
    full = with_config(connection, send_companies=True, send_contacts="none")
    DavSync().sync(connection, full)
    off = with_config(connection, send_companies=False, send_contacts="none")
    company.website = "https://changed.example.org"
    company.save()
    made.log.clear()
    report = DavSync().sync(connection, off)
    assert len(cards(made)) == 1 and "changed.example.org" not in next(iter(cards(made).values()))
    assert not [m for m, _p in made.log if m in ("PUT", "DELETE")]
    assert "not sent" in report.summary()


def test_the_own_card_is_written_one_way_and_never_read_back(made, connection, user):
    user.first_name, user.last_name = "Ana", "Silva"
    user.save()
    full = with_config(connection, send_own_card=True, send_contacts="none")
    report = DavSync().sync(connection, full)
    assert report.pushed == 1
    (href, (body, _etag)) = (
        next(iter(cards(made).items()))[0],
        made.resources[next(iter(cards(made)))],
    )
    assert (
        "FN:Ana Silva" in body
        and "N:Silva;Ana;;;" in body
        and "EMAIL;TYPE=work:ana@example.org" in body
    )
    # Changed on the phone: it is overwritten here, and nothing reaches the profile.
    made.add(href, body.replace("Ana Silva", "Somebody Else"))
    DavSync().sync(connection, full)
    user.refresh_from_db()
    assert user.first_name == "Ana" and user.email == "ana@example.org"
    assert Contact.objects.count() == 0  # never adopted as a contact
    assert "FN:Ana Silva" in made.resources[href][0]


def test_the_own_card_is_off_by_default(made, connection, user):
    user.first_name = "Ana"
    user.save()
    sync(connection)
    assert not cards(made)


# ---------------------------------------------- reminders as tasks, in a task list (#12)

TASKS_PATH = "/dav/calendars/ana/postulo-tasks/"
PHONE_TASKS = "/dav/calendars/ana/phone-tasks/"


def tasks(fake: FakeDav, root: str = TASKS_PATH) -> dict[str, str]:
    return {h: b for h, (b, _e) in fake.resources.items() if h.startswith(root)}


@pytest.fixture
def listed(made):
    """A task list of Postulo's own already on the server, and the connection told its name."""
    made.collections[TASKS_PATH] = {
        "name": "Postulo Tasks",
        "kind": CALENDAR,
        "components": ["VTODO"],
    }
    return made


def with_tasks(connection, name="Postulo Tasks", **extra):
    return with_config(connection, tasklist=name, **extra)


def make_reminder(user, summary="Ring Cave", days=2, **kwargs):
    from postulo.plugins.api import create_reminder

    return create_reminder(
        user, summary=summary, due=timezone.now() + dt.timedelta(days=days), **kwargs
    )


def task_text(uid, summary="Buy lemons", due="20991201T100000Z", extra=""):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\n"
        f"UID:{uid}\r\nDTSTAMP:20260101T000000Z\r\nSUMMARY:{summary}\r\n"
        + (f"DUE:{due}\r\n" if due else "")
        + extra
        + "END:VTODO\r\nEND:VCALENDAR\r\n"
    )


def only_task(fake, root=TASKS_PATH):
    ((href, body),) = tasks(fake, root).items()
    return href, body


def test_a_task_is_read_in_the_component_it_belongs_to_and_an_alarm_is_not_its_text():
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:t1\r\nSUMMARY:Ring Cave\r\n"
        "DUE:20261201T100000Z\r\nSTATUS:NEEDS-ACTION\r\nPRIORITY:1\r\nREFID:app-1\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nSUMMARY:Alarm summary\r\n"
        "DESCRIPTION:Alarm text\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"
        "RELATED-TO:parent-1\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )
    (todo,) = text.text_to_todos(ics)
    assert todo.summary == "Ring Cave" and todo.parent == "parent-1"
    assert todo.due == dt.datetime(2026, 12, 1, 10, tzinfo=dt.UTC)
    assert todo.priority == 1 and todo.refid == "app-1" and not todo.done
    (day,) = text.text_to_todos(
        task_text("t2", due="").replace("END:VTODO", "DUE;VALUE=DATE:20261201\r\nEND:VTODO")
    )
    assert day.due is None and day.due_day == dt.date(2026, 12, 1)


def test_a_task_is_patched_in_what_postulo_owns_and_nothing_else():
    phone = task_text(
        "t1",
        extra=(
            "DESCRIPTION:Mine, not Postulo's\r\nCATEGORIES:work\r\nLOCATION:Lisbon\r\n"
            "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Alarm text\r\nTRIGGER:-PT15M\r\n"
            "END:VALARM\r\nSTATUS:NEEDS-ACTION\r\n"
        ),
    )
    now = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    done = text.Todo(uid="t1", summary="Buy lemons", status="COMPLETED", completed=now)
    patched = text.patch_todo(phone, "t1", {"STATUS"}, done, modified=now)
    for kept in ("DESCRIPTION:Mine, not Postulo's", "CATEGORIES:work", "LOCATION:Lisbon"):
        assert kept in patched
    assert "DESCRIPTION:Alarm text" in patched and "BEGIN:VALARM" in patched
    assert "STATUS:COMPLETED" in patched and "COMPLETED:20261101T000000Z" in patched
    assert "PERCENT-COMPLETE:100" in patched and "NEEDS-ACTION" not in patched
    assert "DUE:20991201T100000Z" in patched and "SUMMARY:Buy lemons" in patched
    assert text.patch_todo(phone, "someone-else", {"STATUS"}, done, modified=now) is None


def test_a_reminder_goes_out_as_a_task_and_a_second_run_does_nothing(listed, connection, user):
    reminder = make_reminder(user)
    full = with_tasks(connection)
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.pushed == 1
    href, body = only_task(listed)
    assert f"UID:{reminder.uid}" in body and "SUMMARY:Ring Cave" in body
    assert "STATUS:NEEDS-ACTION" in body and f"DUE:{text.stamp(reminder.due_at)}" in body
    link = SyncLink.for_record(connection, reminder)
    assert link.uid == reminder.uid and link.collection == f"{BASE}{TASKS_PATH}"
    listed.log.clear()
    assert DavSync().sync(connection, full).summary() == "nothing to do"
    assert not [m for m, _p in listed.log if m in ("PUT", "DELETE")]


def test_no_task_list_is_kept_unless_one_is_named_or_chosen(listed, connection, user):
    make_reminder(user)
    report = sync(connection)
    assert report.error == "" and not tasks(listed)
    assert not requests_under(listed, TASKS_PATH)


def test_an_edit_on_the_phone_changes_the_reminder(listed, connection, user):
    from postulo.applications.services import reopen_reminder  # noqa: F401

    reminder = make_reminder(user)
    full = with_tasks(connection)
    DavSync().sync(connection, full)
    href, body = only_task(listed)
    later = timezone.now().replace(microsecond=0) + dt.timedelta(days=5)
    listed.add(
        href,
        body.replace("Ring Cave", "Ring Cave Johnson").replace(
            f"DUE:{text.stamp(reminder.due_at)}", f"DUE:{text.stamp(later)}"
        ),
    )
    report = DavSync().sync(connection, full)
    assert report.pulled == 1 and report.error == ""
    reminder.refresh_from_db()
    assert reminder.summary == "Ring Cave Johnson" and reminder.due_at == later
    assert reminder.actor == "dav"
    assert DavSync().sync(connection, full).summary() == "nothing to do"


def test_a_tick_completes_it_at_its_moment_an_untick_reopens_and_cancelled_completes(
    listed, connection, user
):
    reminder = make_reminder(user)
    full = with_tasks(connection)
    DavSync().sync(connection, full)
    href, body = only_task(listed)
    moment = (timezone.now() - dt.timedelta(hours=3)).replace(microsecond=0)
    ticked = body.replace(
        "STATUS:NEEDS-ACTION",
        f"STATUS:COMPLETED\r\nCOMPLETED:{text.stamp(moment)}\r\nPERCENT-COMPLETE:100",
    )
    listed.add(href, ticked)
    DavSync().sync(connection, full)
    reminder.refresh_from_db()
    assert reminder.done_at == moment
    # Unticked on the phone: outstanding again.
    listed.add(href, body)
    DavSync().sync(connection, full)
    reminder.refresh_from_db()
    assert reminder.done_at is None
    # Cancelled on the phone: completed, and not written back as anything else.
    listed.add(href, body.replace("STATUS:NEEDS-ACTION", "STATUS:CANCELLED"))
    DavSync().sync(connection, full)
    reminder.refresh_from_db()
    assert reminder.done_at is not None
    listed.log.clear()
    assert DavSync().sync(connection, full).summary() == "nothing to do"
    assert "STATUS:CANCELLED" in listed.resources[href][0]


def test_a_reminder_done_here_ticks_its_task_and_keeps_what_the_phone_added(
    listed, connection, user
):
    from postulo.plugins.api import complete_reminder

    reminder = make_reminder(user)
    full = with_tasks(connection)
    DavSync().sync(connection, full)
    href, body = only_task(listed)
    phone = body.replace(
        "END:VTODO",
        "DESCRIPTION:Mine\r\nCATEGORIES:phone\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\n"
        "DESCRIPTION:Ring!\r\nTRIGGER:-PT5M\r\nEND:VALARM\r\nEND:VTODO",
    )
    listed.add(href, phone)
    DavSync().sync(connection, full)  # the phone's version is read, nothing differs
    complete_reminder(reminder, actor="")
    report = DavSync().sync(connection, full)
    assert report.pushed == 1
    written = listed.resources[href][0]
    assert "STATUS:COMPLETED" in written and "COMPLETED:" in written
    assert "DESCRIPTION:Mine" in written and "CATEGORIES:phone" in written
    assert "DESCRIPTION:Ring!" in written and "BEGIN:VALARM" in written


def test_a_task_deleted_on_the_phone_flags_its_link_and_the_reminder_stays(
    listed, connection, user
):
    reminder = make_reminder(user)
    full = with_tasks(connection)
    DavSync().sync(connection, full)
    href, _body = only_task(listed)
    del listed.resources[href]
    report = DavSync().sync(connection, full)
    assert report.removed == 1 and "reminder was kept" in report.summary()
    assert SyncLink.for_record(connection, reminder).remote_gone
    assert Reminder.objects.filter(pk=reminder.pk).exists()
    listed.log.clear()
    DavSync().sync(connection, full)
    assert not tasks(listed), "a flagged link is not pushed back"


def test_a_reminder_deleted_here_takes_its_task_and_cancels_one_the_phone_made(
    listed, connection, user
):
    ours = make_reminder(user, "Ours")
    full = with_tasks(connection)
    DavSync().sync(connection, full)
    listed.add(TASKS_PATH + "lemons.ics", task_text("lemons-1"))
    DavSync().sync(connection, full)  # adopted: the list is Postulo's own
    theirs = Reminder.objects.get(summary="Buy lemons")
    ours.delete()
    theirs.delete()
    DavSync().sync(connection, full)
    assert list(tasks(listed)) == [TASKS_PATH + "lemons.ics"]
    assert "STATUS:CANCELLED" in listed.resources[TASKS_PATH + "lemons.ics"][0]


def test_a_task_made_in_postulos_own_list_becomes_a_reminder(listed, connection, user):
    listed.add(TASKS_PATH + "lemons.ics", task_text("lemons-1", due="20991201T100000Z"))
    listed.add(TASKS_PATH + "nodue.ics", task_text("nodue-1", "Think about it", due=""))
    listed.add(TASKS_PATH + "done.ics", task_text("done-1", "Old", extra="STATUS:COMPLETED\r\n"))
    full = with_tasks(connection)
    report = DavSync().sync(connection, full)
    reminder = Reminder.objects.get(summary="Buy lemons")
    assert reminder.due_at == dt.datetime(2099, 12, 1, 10, tzinfo=dt.UTC)
    assert reminder.actor == "dav" and reminder.source == ""
    assert "no due time" in report.summary()
    assert not Reminder.objects.filter(summary__in=["Think about it", "Old"]).exists()
    link = SyncLink.for_record(connection, reminder)
    assert link.uid == "lemons-1" and link.collection == f"{BASE}{TASKS_PATH}"
    DavSync().sync(connection, full)
    assert Reminder.objects.filter(owner=user).count() == 1, "adopted once, not on every run"


def test_a_task_in_any_other_list_is_left_alone_and_counted(made, connection, user):
    made.collections[PHONE_TASKS] = {"name": "Phone", "kind": CALENDAR, "components": ["VTODO"]}
    made.add(PHONE_TASKS + "errand.ics", task_text("errand-1", "Buy bread"))
    full = with_tasks(connection, "Phone")
    report = DavSync().sync(connection, full)
    assert report.error == "" and not Reminder.objects.filter(summary="Buy bread").exists()
    assert "not from Postulo and was left alone" in report.summary()
    assert not [m for m, _p in made.log if m in ("PUT", "DELETE")]


def test_the_first_sync_makes_the_task_list_but_test_and_read_only_never_do(server, connection):
    config = {**CONFIG, "tasklist": "Postulo Tasks"}
    result = DavSync().test(config)
    assert result.ok and "The task list “Postulo Tasks” is not on the server." in result.message
    assert not [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR")]
    only = {**connection.full_config, "tasklist": "Postulo Tasks", "read_only": True}
    report = DavSync().sync(connection, only)
    assert "The task list “Postulo Tasks” is not on the server." in report.summary()
    assert not [m for m, _p in server.log if m in ("MKCOL", "MKCALENDAR", "PUT")]
    DavSync().sync(connection, {**connection.full_config, "tasklist": "Postulo Tasks"})
    made_list = server.collections[TASKS_PATH]
    assert made_list["components"] == ["VTODO"]


def test_a_server_that_keeps_no_tasks_is_a_note_and_the_rest_still_syncs(
    server, connection, recruiter, user
):
    server.refuse_tasks = True
    make_reminder(user)
    report = DavSync().sync(connection, with_tasks(connection))
    assert report.error == "" and "Reminders were not synced" in report.summary()
    assert cards(server), "the contacts went out all the same"


def test_an_interviews_reminder_gets_no_task_while_events_are_on(
    listed, connection, user, application
):
    schedule_interview(application, kind="phone", starts_at=next_week())
    make_reminder(user, "Plain one")
    full = with_tasks(connection)
    DavSync().sync(connection, full)
    (body,) = tasks(listed).values()
    assert "SUMMARY:Plain one" in body
    # With no calendar chosen but a task list, the interview's reminder is nobody's alarm:
    chosen = choose_hrefs(connection, {TASKS_PATH: "readwrite"})
    DavSync().sync(connection, chosen)
    assert len(tasks(listed)) == 2


def test_a_task_list_is_chosen_like_a_calendar_and_held_to_like_one(listed, connection, user):
    make_reminder(user)
    listed.add(PHONE_TASKS + "x.ics", task_text("x-1"))
    full = choose_hrefs(connection, {TASKS_PATH: "readwrite"})
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.pushed == 1
    assert all(p.startswith(TASKS_PATH) for _m, p in listed.log), listed.log
    assert not [(m, p) for m, p in listed.log if p in DISCOVERY_PATHS]
    # Read only: tasks come in, nothing goes out.
    listed.add(TASKS_PATH + "lemons.ics", task_text("lemons-1"))
    listed.log.clear()
    read = choose_hrefs(connection, {TASKS_PATH: "read"})
    report = DavSync().sync(connection, read)
    assert not [m for m, _p in listed.log if m in ("PUT", "DELETE", "MKCALENDAR")]
    assert Reminder.objects.filter(summary="Buy lemons").exists()


def test_two_task_lists_chosen_are_refused_rather_than_guessed(listed, connection):
    listed.collections[PHONE_TASKS] = {"name": "Phone", "kind": CALENDAR, "components": ["VTODO"]}
    report = DavSync().sync(
        connection, choose_hrefs(connection, {TASKS_PATH: "readwrite", PHONE_TASKS: "readwrite"})
    )
    assert "More than one task list" in report.summary()
    assert not requests_under(listed, TASKS_PATH + "x", PHONE_TASKS + "x")


def test_a_task_is_offered_for_pairing_and_made_a_reminder_by_hand(made, connection, user):
    from postulo.plugins.testing import create_by_hand, pair_by_hand

    made.collections[PHONE_TASKS] = {"name": "Phone", "kind": CALENDAR, "components": ["VTODO"]}
    made.add(PHONE_TASKS + "errand.ics", task_text("errand-1", "Buy bread"))
    made.add(PHONE_TASKS + "ours.ics", task_text("abc@postulo", "Postulo's own"))
    made.add(PHONE_TASKS + "nodue.ics", task_text("nodue-1", "Whenever", due=""))
    full = with_tasks(connection, "Phone")
    items = DavSync().remote_items(full, kind="reminder")
    assert [(i.uid, i.kinds) for i in items if i.uid != "nodue-1"] == [("errand-1", ("reminder",))]
    assert DavSync().remote_items(full, search="bread")[0].label == "Buy bread"
    reminder = create_by_hand(
        connection, collection=f"{BASE}{PHONE_TASKS}", uid="errand-1", kind="reminder"
    )
    assert reminder.summary == "Buy bread" and reminder.actor == "dav"
    assert SyncLink.for_record(connection, reminder).uid == "errand-1"
    mine = make_reminder(user, "Mine")
    pair_by_hand(connection, mine, collection=f"{BASE}{PHONE_TASKS}", uid="nodue-1")
    report = DavSync().sync(connection, full)
    assert report.error == ""
    mine.refresh_from_db()
    assert mine.summary == "Whenever", "the phone gave no time, so its version was the later one"
    from postulo.plugins.api import change_reminder

    change_reminder(mine, summary="Mine again", actor="")
    DavSync().sync(connection, full)
    written = made.resources[PHONE_TASKS + "nodue.ics"][0]
    assert "UID:nodue-1" in written and "SUMMARY:Mine again" in written
    # A task without a due time cannot be made a reminder.
    made.add(PHONE_TASKS + "nodue2.ics", task_text("nodue-2", "Someday", due=""))
    with pytest.raises(Exception):  # noqa: B017,PT011
        create_by_hand(
            connection,
            collection=f"{BASE}{PHONE_TASKS}",
            uid="nodue-2",
            kind="reminder",
            pair=False,
        )


# --------------------------------- an application's apply and hear-back tasks (#12)


@pytest.fixture
def draft(user, company):
    posting = JobPosting.objects.create(owner=user, company=company, title="Test Engineer")
    return Application.objects.create(
        owner=user,
        posting=posting,
        status=Status.DRAFT,
        priority=3,
        deadline=dt.date(2099, 6, 30),
    )


def with_milestones(connection, **extra):
    return with_tasks(connection, milestone_tasks=True, **extra)


def milestone(fake, which: str, root=TASKS_PATH) -> str:
    (body,) = [b for h, b in tasks(fake, root).items() if h.endswith(f"-{which}.ics")]
    return body


def test_a_draft_has_an_apply_task_with_its_deadline_and_priority(listed, connection, draft):
    full = with_milestones(connection)
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.pushed == 1
    body = milestone(listed, "apply")
    assert "SUMMARY:Apply to Test Engineer at Aperture Science" in body
    assert "DUE;VALUE=DATE:20990630" in body and "PRIORITY:1" in body  # high is 1 (RFC 5545)
    assert f"REFID:application-{draft.pk}-{draft.owner_id}@postulo" in body
    assert "STATUS:NEEDS-ACTION" in body
    link = SyncLink.for_record(connection, draft, "apply")
    assert link.uid == f"application-{draft.pk}-{draft.owner_id}-apply@postulo"
    assert DavSync().sync(connection, full).summary() == "nothing to do"


def test_no_application_task_is_made_unless_it_is_switched_on(listed, connection, draft):
    DavSync().sync(connection, with_tasks(connection))
    assert not tasks(listed)


def test_a_tick_on_apply_files_one_suggestion_and_accepting_stamps_the_moment(
    listed, connection, draft, user
):
    from postulo.applications.models import Suggestion
    from postulo.applications.suggestions import accept

    full = with_milestones(connection)
    DavSync().sync(connection, full)
    href = next(h for h in tasks(listed) if h.endswith("-apply.ics"))
    body = listed.resources[href][0]
    moment = (timezone.now() - dt.timedelta(days=1)).replace(microsecond=0)
    ticked = body.replace(
        "STATUS:NEEDS-ACTION", f"STATUS:COMPLETED\r\nCOMPLETED:{text.stamp(moment)}"
    )
    listed.add(href, ticked)
    for _run in range(3):
        DavSync().sync(connection, full)
    (suggestion,) = Suggestion.objects.filter(owner=user)
    assert suggestion.suggested_status == "applied" and suggestion.application == draft
    assert suggestion.occurred_at == moment and suggestion.source == "dav"
    draft.refresh_from_db()
    assert draft.status == Status.DRAFT, "nothing is changed behind the person's back"
    assert "STATUS:COMPLETED" in listed.resources[href][0], "the tick stays while it waits"
    accept(suggestion)
    draft.refresh_from_db()
    assert draft.status == Status.APPLIED and draft.applied_at == moment
    DavSync().sync(connection, full)
    assert f"COMPLETED:{text.stamp(moment)}" in listed.resources[href][0]
    # Reply now exists, and nothing is left to do.
    assert "SUMMARY:Hear back from Aperture Science about Test Engineer" in milestone(
        listed, "reply"
    )
    assert DavSync().sync(connection, full).summary() == "nothing to do"


def test_a_declined_tick_is_unticked_on_the_next_run_and_not_filed_again(
    listed, connection, draft, user
):
    from postulo.applications.models import Suggestion
    from postulo.applications.suggestions import decline

    full = with_milestones(connection)
    DavSync().sync(connection, full)
    href = next(h for h in tasks(listed) if h.endswith("-apply.ics"))
    body = listed.resources[href][0]
    moment = text.stamp(timezone.now() - dt.timedelta(hours=2))
    ticked = body.replace("STATUS:NEEDS-ACTION", f"STATUS:COMPLETED\r\nCOMPLETED:{moment}")
    listed.add(href, ticked)
    DavSync().sync(connection, full)
    decline(Suggestion.objects.get(owner=user))
    DavSync().sync(connection, full)
    assert "STATUS:NEEDS-ACTION" in listed.resources[href][0]
    assert "COMPLETED:" not in listed.resources[href][0]
    # The same tick made again is the same suggestion, which is not filed twice and is
    # unticked again; a tick at another moment is a new one.
    listed.add(href, ticked)
    DavSync().sync(connection, full)
    assert Suggestion.objects.filter(owner=user).count() == 1
    assert "STATUS:NEEDS-ACTION" in listed.resources[href][0]
    later = text.stamp(timezone.now() - dt.timedelta(hours=1))
    listed.add(href, body.replace("STATUS:NEEDS-ACTION", f"STATUS:COMPLETED\r\nCOMPLETED:{later}"))
    DavSync().sync(connection, full)
    assert Suggestion.objects.filter(owner=user).count() == 2


def test_hear_back_closes_as_the_application_ends_and_a_tick_does_not_stick(
    listed, connection, application, user
):
    from postulo.applications.services import change_status

    full = with_milestones(connection)
    DavSync().sync(connection, full)  # an application that was sent: hear back, no apply
    href = next(h for h in tasks(listed) if h.endswith("-reply.ics"))
    assert not [h for h in tasks(listed) if h.endswith("-apply.ics")]
    body = listed.resources[href][0]
    assert "STATUS:NEEDS-ACTION" in body and "PRIORITY:5" in body
    # Ticked on the phone: the application did not end, so the next push undoes it.
    listed.add(href, body.replace("STATUS:NEEDS-ACTION", "STATUS:COMPLETED"))
    DavSync().sync(connection, full)
    assert "STATUS:NEEDS-ACTION" in listed.resources[href][0]
    # An offer is not an ending.
    change_status(application, "offer", actor="")
    DavSync().sync(connection, full)
    assert "STATUS:NEEDS-ACTION" in listed.resources[href][0]
    change_status(application, "rejected", actor="")
    DavSync().sync(connection, full)
    assert "STATUS:COMPLETED" in listed.resources[href][0]
    # An ending the phone cannot tell from another: pushed one way.
    listed.add(href, listed.resources[href][0].replace("STATUS:COMPLETED", "STATUS:CANCELLED"))
    DavSync().sync(connection, full)
    assert "STATUS:COMPLETED" in listed.resources[href][0]
    assert DavSync().sync(connection, full).summary() == "nothing to do"


def test_hear_back_is_cancelled_when_ghosted_and_apply_when_withdrawn_unsent(
    listed, connection, application, draft
):
    from postulo.applications.services import change_status

    full = with_milestones(connection)
    DavSync().sync(connection, full)
    change_status(application, "ghosted", actor="")
    change_status(draft, "withdrawn", actor="")
    DavSync().sync(connection, full)
    assert "STATUS:CANCELLED" in milestone(listed, "reply")
    apply_body = [b for h, b in tasks(listed).items() if h.endswith("-apply.ics")][0]
    assert "STATUS:CANCELLED" in apply_body and "COMPLETED:" not in apply_body


def test_reminders_about_an_application_sit_under_its_open_task(listed, connection, draft, user):
    reminder = make_reminder(user, "Chase them", application=draft)
    full = with_milestones(connection)
    DavSync().sync(connection, full)
    ref = f"application-{draft.pk}-{user.pk}@postulo"
    apply_uid = f"application-{draft.pk}-{user.pk}-apply@postulo"
    body = next(b for h, b in tasks(listed).items() if reminder.uid.split("@")[0] in h)
    assert f"REFID:{ref}" in body and f"RELATED-TO:{apply_uid}" in body
    # Sent: its open task is now hear-back, and the reminder moves under it.
    from postulo.applications.services import change_status

    change_status(draft, "applied", actor="")
    DavSync().sync(connection, full)
    body = next(b for h, b in tasks(listed).items() if reminder.uid.split("@")[0] in h)
    assert f"RELATED-TO:{apply_uid.replace('-apply', '-reply')}" in body
    assert "RELATED-TO:" + apply_uid not in body


def test_a_task_made_under_an_applications_task_is_a_reminder_about_it(
    listed, connection, draft, user
):
    full = with_milestones(connection)
    DavSync().sync(connection, full)
    apply_uid = f"application-{draft.pk}-{user.pk}-apply@postulo"
    listed.add(
        TASKS_PATH + "under.ics",
        task_text("under-1", "Update my CV", extra=f"RELATED-TO:{apply_uid}\r\n"),
    )
    DavSync().sync(connection, full)
    reminder = Reminder.objects.get(summary="Update my CV")
    assert reminder.application == draft
    # Somebody else's application is not one: the task is a reminder about none.
    listed.add(
        TASKS_PATH + "other.ics",
        task_text(
            "other-1", "Stranger", extra=f"RELATED-TO:application-{draft.pk}-9999-apply@postulo\r\n"
        ),
    )
    DavSync().sync(connection, full)
    assert Reminder.objects.get(summary="Stranger").application is None


def test_a_task_deleted_on_the_phone_is_not_made_again_and_a_deleted_application_takes_its_links(
    listed, connection, draft
):
    full = with_milestones(connection)
    DavSync().sync(connection, full)
    href = next(iter(tasks(listed)))
    del listed.resources[href]
    report = DavSync().sync(connection, full)
    assert report.removed == 1 and "not made again" in report.summary()
    assert SyncLink.for_record(connection, draft, "apply").remote_gone
    assert not tasks(listed), "it is not made again"
    draft.delete()
    DavSync().sync(connection, full)
    assert not SyncLink.objects.filter(connection=connection, slot="apply")


def test_a_deleted_application_takes_the_tasks_postulo_made(listed, connection, draft):
    full = with_milestones(connection)
    DavSync().sync(connection, full)
    assert len(tasks(listed)) == 1
    draft.delete()
    DavSync().sync(connection, full)
    assert not tasks(listed) and not SyncLink.objects.filter(connection=connection, slot="apply")


def test_an_applications_tasks_and_its_event_pairing_do_not_meet(listed, connection, application):
    """A link in a slot is a task's; the application's own (slot-less) link is its event's."""
    listed.add(CALENDAR_PATH + "phone.ics", phone_event("phone-ev-1@example.org", next_week(14)))
    pair_with_application(connection, application)
    full = with_milestones(connection)
    report = DavSync().sync(connection, full)
    assert report.error == ""
    assert Interview.objects.filter(application=application).count() == 1
    assert SyncLink.objects.filter(connection=connection, slot="reply").count() == 1
    assert SyncLink.for_record(connection, application).uid == "phone-ev-1@example.org"
    DavSync().sync(connection, full)
    assert Interview.objects.filter(application=application).count() == 1
    assert len(tasks(listed)) == 1, "the interview's reminder is its event's alarm, no task"
