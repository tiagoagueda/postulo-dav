"""The sync, against a DAV server that lives in memory, and inside a real Postulo."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlsplit

import httpx
import pytest
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
from postulo.plugins.base import SyncPlugin
from postulo.plugins.models import Connection, SyncLink

from postulo_dav import dav as dav_module
from postulo_dav import text
from postulo_dav.dav import ADDRESSBOOK, CALENDAR, Conflict, Server
from postulo_dav.sync import DavSync, card_for, event_text

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

    def _etag(self) -> str:
        self.counter += 1
        return f'"v{self.counter}"'

    def add(self, href: str, body: str) -> str:
        etag = self._etag()
        self.resources[href] = (body, etag)
        return etag

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        method = request.method
        self.log.append((method, path))
        if request.headers.get("Authorization") != httpx.BasicAuth("ana", "app-pw")._auth_header:
            return httpx.Response(401)
        body = request.content.decode()

        if method == "PROPFIND":
            depth = request.headers.get("Depth", "0")
            if path in ("/.well-known/carddav", "/.well-known/caldav"):
                return httpx.Response(301, headers={"Location": f"{BASE}/dav/"})
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
                return _multistatus(rows)
            return httpx.Response(404)

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
            etag = self._etag()
            self.resources[path] = (body, etag)
            return httpx.Response(201 if current is None else 204, headers={"ETag": etag})

        if method == "DELETE":
            self.resources.pop(path, None)
            return httpx.Response(204)

        return httpx.Response(405)


def _multistatus(rows) -> httpx.Response:
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
    xml.append("</d:multistatus>")
    return httpx.Response(
        207, content="".join(xml).encode(), headers={"Content-Type": "application/xml"}
    )


@pytest.fixture
def server(monkeypatch):
    fake = FakeDav()

    def fake_client(**kwargs):
        kwargs.pop("event_hooks", None)
        kwargs.setdefault("timeout", 10)
        kwargs.setdefault("follow_redirects", True)
        return httpx.Client(transport=httpx.MockTransport(fake.handler), **kwargs)

    monkeypatch.setattr(dav_module.http, "client", fake_client)
    return fake


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
    assert all_day.starts_at == dt.datetime(2026, 9, 10, tzinfo=dt.UTC)


def test_long_lines_fold_and_unfold():
    card = text.Card(uid="u", name="A" * 200)
    rendered = text.card_to_text(card, prodid="p", rev=timezone.now())
    assert all(len(line.encode()) <= 75 for line in rendered.split("\r\n"))
    assert text.text_to_card(rendered).name == "A" * 200


# ------------------------------------------------------------------- discovery


def test_discovery_finds_the_homes_and_creates_the_collections(server):
    plugin = DavSync()
    assert isinstance(plugin, SyncPlugin)
    result = plugin.test(CONFIG)
    assert result.ok and "“Postulo”" in result.message
    assert "/dav/addressbooks/ana/postulo-contacts/" in server.collections
    assert "/dav/calendars/ana/postulo-interviews/" in server.collections
    assert server.collections["/dav/addressbooks/ana/postulo-contacts/"]["name"] == "Postulo"
    result = plugin.test(CONFIG)
    assert (
        result.ok and len([c for c in server.collections.values() if c["name"] == "Postulo"]) == 2
    )


def test_a_wrong_password_is_a_sentence(server):
    result = DavSync().test({**CONFIG, "password": "no"})
    assert not result.ok and "principal" in result.message


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
    assert "Changed on the phone on 2020-01-01" in recruiter.notes and "Janitor" in recruiter.notes
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
    assert "Changed here on" in recruiter.notes and "Boss" in recruiter.notes


def test_a_deletion_on_the_phone_never_deletes_here(server, connection, recruiter):
    DavSync().sync(connection, connection.full_config)
    server.resources.clear()
    report = DavSync().sync(connection, connection.full_config)
    assert report.removed == 1 and "removed from the address book" in report.summary()
    assert Contact.objects.filter(pk=recruiter.pk).exists()
    assert SyncLink.for_record(connection, recruiter).remote_gone is True
    report = DavSync().sync(connection, connection.full_config)
    assert report.summary() == "nothing to do", "a record removed on purpose is not pushed back"


def test_a_deletion_here_takes_the_twin_with_it(server, connection, recruiter):
    DavSync().sync(connection, connection.full_config)
    recruiter.delete()
    report = DavSync().sync(connection, connection.full_config)
    assert report.removed == 1 and cards(server) == {} and SyncLink.objects.count() == 0


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
    assert "ATTENDEE;CN=Cave Johnson;ROLE=REQ-PARTICIPANT:mailto:cave@aperture.test" in body
    assert "BEGIN:VALARM" in body and "TRIGGER:-PT1440M" in body
    assert body == event_text(interview)
    assert SyncLink.for_record(connection, interview).remote_href.endswith(
        f"/postulo-interviews/{interview.uid}.ics"
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


def test_read_only_pulls_but_never_writes(server, connection, recruiter, application):
    schedule_interview(application, kind="video", starts_at=next_week())
    connection.config["read_only"] = True
    connection.save()
    report = DavSync().sync(connection, connection.full_config)
    assert report.pushed == 0 and report.skipped == 2
    assert not [m for m, _p in server.log if m in ("PUT", "DELETE")]
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
    with translation.override("fr-fr"):
        assert str(plugin.label) == "CardDAV et CalDAV"
        assert plugin.config_fields()[3].label == "Carnet d'adresses"
    with translation.override("pt-pt"):
        assert str(plugin.label) == "CardDAV e CalDAV"
    with translation.override("en-gb"):
        assert str(plugin.label) == "CardDAV and CalDAV"
