"""The sync: contacts ↔ the *Postulo* address book, interviews ↔ the *Postulo* calendar.

One run does, for each of the two collections:

1. **List** the collection: every resource with its ETag.
2. **Push** each local record — a contact, an upcoming or recent interview — that has no
   twin yet, or whose content changed since it was last pushed. A write is conditional
   on the ETag last seen, so a change made on the phone in the meantime is noticed rather
   than overwritten: the later change wins, and the losing version is kept as a note.
3. **Pull** each twin whose ETag changed since it was last seen, and apply it locally.
   Interview times move through Postulo's own service, so the timeline says so.
4. **Notice deletions**. A twin that vanished from the collection is *unlinked and
   flagged* — the local record stays, and the report says what happened. A record
   deleted here takes its twin with it, but only the twin it was told about: the DELETE
   carries ``If-Match``, so a card or an event edited on the phone since the last sync
   survives the deletion and becomes a note instead (#2).
5. **Adopt** what appeared in the collection from elsewhere: a contact created on the
   phone becomes a contact here, under the company its ORG names or a holding company.
   An event created on the phone is left alone with a note: nothing links it to an
   application, and a job tracker must not invent one.

Recurring events are ignored: interviews do not recur. Attendees are informational; no
invitation is ever sent. Read-only mode does steps 1, 3, 4 and 5 and never writes — and
*never writes* includes the collections themselves: a person who asked for read only, and
a person who merely pressed *Test*, gets no address book and no calendar made for them on
their server. Both look the collections up and report what is not there (#2).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from urllib.parse import urljoin, urlsplit

from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

# What this plugin imports from Postulo, and what that costs.
#
# `postulo.plugins.api` is the surface: those names are a promise kept across a minor
# release. Everything marked #229 below is past it — reached on purpose, because a two-way
# sync of records needs the records themselves, the services that move them so the timeline
# says so, and the link table that remembers which twin is whose, and none of that is on
# the surface yet. postulo/postulo#229 proposes `SyncLink` plus a small sync facade; when it
# lands these move to `postulo.plugins.api` and the markers go with them. Until then this is
# what the plugin risks on a core refactor, counted here rather than buried in the file.
from postulo.applications import ical  # #229
from postulo.applications.models import Interview, InterviewOutcome  # #229
from postulo.applications.services import (  # #229
    get_or_create_company,
    record_event,
    reschedule_interview,
    settle_interview,
)
from postulo.core import phone_numbers, web_links  # #229
from postulo.jobs.models import Contact  # #229
from postulo.plugins.api import (
    DestinationRefused,
    FieldSpec,
    SyncReport,
    TestResult,
    check_destination,
)
from postulo.plugins.models import SyncLink  # #229 — SyncLink is the whole point of it

from . import __version__, dav, text
from .dav import ADDRESSBOOK, CALENDAR, Conflict, DavError, Server

PRODID = f"-//Postulo//postulo-dav {__version__}//EN"
ACTOR = "dav"
#: Interviews older than this are left out of the calendar: it shows what is coming.
HISTORY = dt.timedelta(days=90)
#: The name of the company a contact created on the phone lands on when its card has no ORG.
HOLDING_COMPANY = gettext_lazy("(no company)")
#: What follows "the address book X is not on the server" when writing is allowed.
FIRST_SYNC_TAIL = gettext_lazy("The first sync will make what is missing.")
#: ...and when it is not. Read only means read only, collections included (#2).
READ_ONLY_TAIL = gettext_lazy(
    "Read only is on, so nothing is made: create it on the server, or name one that is "
    "already there."
)


def digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


#: A card whose twin was deleted here, but which somebody had edited in the meantime.
CARD_KEPT = gettext_lazy(
    "A contact deleted here had been changed on the phone since the last sync. The card was "
    "left in the address book, and comes back as a contact on the next run."
)
#: The same for an event. Nothing adopts a foreign event, so this one simply stays put.
EVENT_KEPT = gettext_lazy(
    "An interview deleted here had been changed on the phone since the last sync. The event "
    "was left on the calendar."
)


def drop_twin(server: Server, link, report: SyncReport, kept: str) -> bool:
    """Delete the twin of a record deleted here. ``True`` if the server actually dropped it.

    The DELETE carries ``If-Match`` with the ETag this connection last saw, so it only ever
    destroys the version it was told about. A `412` means the card or the event was edited
    on the phone after that: somebody's writing, which a deletion on this side is not
    entitled to throw away. The resource is left where it is and the run says so (#2).

    Either way the link goes: it named a record that no longer exists. A card left behind is
    adopted as a contact on the next run — the deletion is undone, visibly, in the direction
    that loses nothing — and an event left behind is foreign from then on, like any other.
    """
    try:
        server.delete(link.remote_href, etag=link.etag)
    except Conflict:
        report.notes.append(str(kept))
        return False
    return True


# ------------------------------------------------------------------ contacts


def card_for(contact: Contact) -> text.Card:
    # A card carries one number and one address on the web; a contact holds several of
    # each since Postulo 0.3, with one of each kind marked as the one to use. The card
    # gets the primaries, which is what "the number" and "the LinkedIn" mean there now.
    number = phone_numbers.primary_for(contact)
    profile = web_links.primary_for(contact, web_links.Kind.SOCIAL)
    return text.Card(
        uid=f"contact-{contact.pk}-{contact.owner_id}@postulo",
        name=contact.name,
        org=contact.company.name if contact.company_id else "",
        title=contact.role,
        email=contact.email,
        phone=number.number if number else "",
        url=profile.url if profile else "",
        note=contact.notes,
    )


def apply_card(contact: Contact, card: text.Card) -> None:
    contact.name = card.name or contact.name
    contact.role = card.title[:200]
    contact.email = card.email[:254]
    contact.notes = card.note
    if card.org and (not contact.company_id or contact.company.name.lower() != card.org.lower()):
        contact.company = get_or_create_company(contact.owner, card.org)
    contact.save()
    # The number and the profile are rows of their own, written once the contact has a
    # key to hang them on. Only the primary of each is touched: the rest of what the
    # person keeps here is not the card's to lose. A number some other row on the instance
    # already holds is left alone rather than failing the sync over one field of a card.
    number = card.phone[:40].strip()
    primary = phone_numbers.primary_for(contact)
    if not number or not phone_numbers.taken_elsewhere(
        number, exclude_pk=primary.pk if primary else None
    ):
        phone_numbers.save_only_number(contact, contact.owner, number)
    web_links.save_only_link(contact, contact.owner, web_links.Kind.SOCIAL, card.url[:500])


# ---------------------------------------------------------------- interviews


def event_text(interview: Interview) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
    event = ical.event_lines(interview)
    reminder = interview.reminder
    if reminder is not None and not reminder.is_done and reminder.due_at < interview.starts_at:
        lead = interview.starts_at - reminder.due_at
        minutes = int(lead.total_seconds() // 60)
        event = event[:-1] + [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{ical.escape(reminder.summary)}",
            f"TRIGGER:-PT{minutes}M",
            "END:VALARM",
            "END:VEVENT",
        ]
    return text.render(lines + event + ["END:VCALENDAR"])


def interview_source(interview: Interview) -> str:
    """What a push is a function of: the parts of an interview a calendar can change."""
    return text.Event(
        starts_at=interview.starts_at,
        ends_at=interview.ends_at,
        location=interview.location,
        description=interview.notes,
        status=ical.STATUS_OF[interview.outcome],
    ).digest_source()


def apply_event(interview: Interview, event: text.Event, report: SyncReport) -> None:
    if event.starts_at and event.ends_at:
        reschedule_interview(
            interview, starts_at=event.starts_at, ends_at=event.ends_at, actor=ACTOR
        )
    changed = []
    if event.location != interview.location:
        interview.location = event.location[:500]
        changed.append("location")
    if (
        event.description
        and event.description != interview.notes
        and not event.description.startswith(interview.notes)
    ):
        interview.notes = event.description
        changed.append("notes")
    if changed:
        interview.save(update_fields=[*changed, "updated_at"])
    if event.status == "CANCELLED" and interview.outcome == InterviewOutcome.SCHEDULED:
        settle_interview(
            interview,
            InterviewOutcome.CANCELLED,
            note=str(_("Cancelled on the calendar.")),
            actor=ACTOR,
        )


# ---------------------------------------------------------------- the plugin


class DavSync:
    name = "dav"
    version = __version__
    kind = "sync"
    label = gettext_lazy("CardDAV and CalDAV")

    def config_fields(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "url",
                str(_("Server address")),
                type="url",
                help=str(
                    _(
                        "The server's root, e.g. https://cloud.example.org. Postulo finds the "
                        "address book and calendar homes from there."
                    )
                ),
            ),
            FieldSpec("username", str(_("Username")), type="text"),
            FieldSpec(
                "password",
                str(_("Password or app password")),
                type="password",
                secret=True,
                help=str(_("An app password where the server offers one. Stored encrypted.")),
            ),
            FieldSpec(
                "addressbook",
                str(_("Address book")),
                type="text",
                default="Postulo",
                help=str(
                    _(
                        "Made on the server by the first sync if it is not there, unless read "
                        "only is on. Only this address book is touched."
                    )
                ),
            ),
            FieldSpec(
                "calendar",
                str(_("Calendar")),
                type="text",
                default="Postulo",
                help=str(
                    _(
                        "Made on the server by the first sync if it is not there, unless read "
                        "only is on. Only this calendar is touched."
                    )
                ),
            ),
            FieldSpec(
                "read_only",
                str(_("Read only")),
                type="boolean",
                required=False,
                default=False,
                help=str(
                    _(
                        "Never write to the server — not even to make the address book or the "
                        "calendar. Contacts and interviews still come in; nothing goes out."
                    )
                ),
            ),
        ]

    def summary(self, config: dict) -> str:
        parts = [str(config.get("url", "")).rstrip("/")]
        parts.append(
            f"{config.get('addressbook') or 'Postulo'} · {config.get('calendar') or 'Postulo'}"
        )
        if config.get("read_only"):
            parts.append(str(_("read only")))
        return " · ".join(part for part in parts if part)

    # -------------------------------------------------------------- test

    def validate(self, config: dict) -> dict[str, list[str]]:
        """Look at what was typed before anything is saved. No password over plain http.

        Basic authentication puts the password on the wire; over ``http://`` it is on the
        wire in the clear, and a job hunt's address book is not the thing to learn that on.
        The exception is the machine down the hall — a bare name, a ``.local``, a private
        address — where there is no network in between and the operator has said such
        addresses may be reached at all. Everything else must be ``https://``.
        """
        url = str(config.get("url", "")).strip()
        if not url:
            return {}
        parts = urlsplit(url)
        scheme, host = parts.scheme.lower(), parts.hostname or ""
        if scheme == "https":
            return {}
        if scheme != "http":
            return {
                "url": [
                    str(_("The address must start with https:// — for example %(example)s."))
                    % {"example": "https://cloud.example.org"}
                ]
            }
        if not dav.is_private_host(host):
            return {
                "url": [
                    str(
                        _(
                            "Over http:// the password goes across the network in the clear, and "
                            "“%(host)s” is not an address on your own network. Use https://."
                        )
                    )
                    % {"host": host}
                ]
            }
        # A machine on the person's own network, over http, is a decision the *operator*
        # made once. Asking here is also the cheapest way to ask it: where private
        # destinations are allowed the answer comes back without resolving anything.
        try:
            check_destination(url)
        except DestinationRefused as refused:
            return {"url": [str(refused)]}
        return {}

    def test(self, config: dict) -> TestResult:
        """Sign in and look. *Test* reads: it never makes a collection (#2)."""
        read_only = bool(config.get("read_only"))
        try:
            client, server = dav.connect(config)
            with client:
                found = {
                    kind: self._collection(server, config, kind, create=False)
                    for kind in (ADDRESSBOOK, CALENDAR)
                }
        except (DavError, Exception) as error:  # noqa: BLE001 - the sentence is the point
            return TestResult(False, f"{type(error).__name__}: {error}")
        missing = [kind for kind, collection in found.items() if collection is None]
        if not missing:
            return TestResult(
                True,
                str(_("Found the address book “%(book)s” and the calendar “%(calendar)s”."))
                % {
                    "book": found[ADDRESSBOOK].name,
                    "calendar": found[CALENDAR].name,
                },
            )
        sentences = [self._missing_sentence(config, kind) for kind in missing]
        # Signing in worked, which is what *Test* is for; what is missing is only a
        # problem the sync cannot solve when it is forbidden to write.
        sentences.append(str(READ_ONLY_TAIL if read_only else FIRST_SYNC_TAIL))
        return TestResult(not read_only, " ".join(sentences))

    def _name(self, config: dict, kind: str) -> str:
        return str(config.get("addressbook" if kind == ADDRESSBOOK else "calendar") or "Postulo")

    def _missing_sentence(self, config: dict, kind: str) -> str:
        name = self._name(config, kind)
        if kind == ADDRESSBOOK:
            return str(_("The address book “%(name)s” is not on the server.")) % {"name": name}
        return str(_("The calendar “%(name)s” is not on the server.")) % {"name": name}

    def _collection(
        self, server: Server, config: dict, kind: str, *, create: bool
    ) -> dav.Collection | None:
        """The collection to work in. ``create=False`` looks, and never makes one."""
        well_known = "carddav" if kind == ADDRESSBOOK else "caldav"
        principal = server.principal(well_known)
        home = server.home(principal, kind)
        name = self._name(config, kind)
        found = server.find_collection(home, name, kind)
        if found is not None or not create:
            return found
        return server.create_collection(home, name, kind)

    # -------------------------------------------------------------- sync

    def sync(self, connection, config: dict) -> SyncReport:
        report = SyncReport()
        read_only = bool(config.get("read_only"))
        client, server = dav.connect(config)
        with client:
            # In read-only mode a missing collection is reported, not made: half a sync
            # still runs, and the person is told which half did not.
            book = self._collection(server, config, ADDRESSBOOK, create=not read_only)
            if book is None:
                report.notes.append(
                    f"{self._missing_sentence(config, ADDRESSBOOK)} {READ_ONLY_TAIL}"
                )
            else:
                self._sync_contacts(server, book, connection, report, read_only=read_only)
            calendar = self._collection(server, config, CALENDAR, create=not read_only)
            if calendar is None:
                report.notes.append(f"{self._missing_sentence(config, CALENDAR)} {READ_ONLY_TAIL}")
            else:
                self._sync_interviews(server, calendar, connection, report, read_only=read_only)
        return report

    # ---------------------------------------------------------- contacts

    def _sync_contacts(self, server, book, connection, report, *, read_only: bool) -> None:
        now = timezone.now()
        remote = server.etags(book.href)
        # Links flagged remote_gone are kept in the map: they say "the person removed
        # this from the address book", and the record must not be pushed back.
        links = {link.object_id: link for link in SyncLink.of_model(connection, Contact)}
        contacts = {
            c.pk: c for c in Contact.objects.for_user(connection.owner).select_related("company")
        }
        linked_hrefs = {link.remote_href for link in links.values()}

        # Local records: push new and changed ones; notice twins that vanished.
        for pk, contact in contacts.items():
            link = links.get(pk)
            if link is not None and link.remote_gone:
                continue  # removed from the address book on purpose; leave it removed
            card = card_for(contact)
            source = digest(card.digest_source())
            if link is None:
                if read_only:
                    report.skipped += 1
                    continue
                href = urljoin(book.href, f"{uuid.uuid4()}.vcf")
                etag = server.put(
                    href,
                    text.card_to_text(card, prodid=PRODID, rev=now),
                    "text/vcard; charset=utf-8",
                )
                SyncLink.bind(
                    connection,
                    contact,
                    remote_href=href,
                    uid=card.uid,
                    etag=etag,
                    local_hash=source,
                    last_synced_at=now,
                )
                report.pushed += 1
                continue
            if link.remote_href not in remote:
                link.remote_gone = True
                link.save(update_fields=["remote_gone", "updated_at"])
                report.removed += 1
                report.notes.append(
                    str(_("“%(name)s” was removed from the address book; kept here."))
                    % {"name": contact.name}
                )
                continue
            remote_changed = remote[link.remote_href] != link.etag
            local_changed = link.local_hash != source
            if remote_changed:
                resource = server.get(link.remote_href)
                incoming = text.text_to_card(resource.text)
                if local_changed and (incoming.rev or now) < contact.updated_at:
                    # Both changed and ours is later: ours wins, theirs is kept as a note.
                    contact.notes = (contact.notes + "\n\n" if contact.notes else "") + str(
                        _("Changed on the phone on %(when)s: %(text)s")
                    ) % {
                        "when": (incoming.rev or now).date().isoformat(),
                        "text": incoming.digest_source().replace("\n", " · "),
                    }
                    contact.save(update_fields=["notes", "updated_at"])
                    card = card_for(contact)
                    source = digest(card.digest_source())
                    link.etag = resource.etag
                    local_changed = True
                else:
                    if local_changed:
                        losing = card.digest_source().replace("\n", " · ")
                        incoming.note = (incoming.note + "\n\n" if incoming.note else "") + str(
                            _("Changed here on %(when)s: %(text)s")
                        ) % {"when": contact.updated_at.date().isoformat(), "text": losing}
                    apply_card(contact, incoming)
                    link.etag = resource.etag
                    link.local_hash = digest(card_for(contact).digest_source())
                    link.last_synced_at = now
                    link.save()
                    report.pulled += 1
                    continue
            if local_changed:
                if read_only:
                    report.skipped += 1
                    continue
                try:
                    etag = server.put(
                        link.remote_href,
                        text.card_to_text(card, prodid=PRODID, rev=now),
                        "text/vcard; charset=utf-8",
                        etag=link.etag,
                    )
                except Conflict:
                    report.notes.append(
                        str(_("“%(name)s” changed on both sides at once; next run decides."))
                        % {"name": contact.name}
                    )
                    continue
                link.etag, link.local_hash, link.last_synced_at = etag, source, now
                link.save()
                report.pushed += 1

        # Links whose contact is gone here: take the twin with it — unless the twin moved.
        for link in list(links.values()):
            if link.object_id not in contacts:
                if not read_only and link.remote_href in remote:
                    if drop_twin(server, link, report, CARD_KEPT):
                        report.removed += 1
                link.delete()

        # Cards that appeared on the other side: adopt them.
        for href in remote:
            if href in linked_hrefs:
                continue
            resource = server.get(href)
            card = text.text_to_card(resource.text)
            if not card.name:
                report.skipped += 1
                continue
            company = get_or_create_company(connection.owner, card.org or str(HOLDING_COMPANY))
            contact = Contact.objects.create(
                owner=connection.owner, company=company, name=card.name[:200]
            )
            apply_card(contact, card)
            SyncLink.bind(
                connection,
                contact,
                remote_href=href,
                uid=card.uid,
                etag=resource.etag,
                local_hash=digest(card_for(contact).digest_source()),
                last_synced_at=now,
            )
            report.pulled += 1

    # -------------------------------------------------------- interviews

    def _sync_interviews(self, server, calendar, connection, report, *, read_only: bool) -> None:
        now = timezone.now()
        remote = server.etags(calendar.href)
        links = {link.object_id: link for link in SyncLink.of_model(connection, Interview)}
        interviews = {
            i.pk: i
            for i in Interview.objects.for_user(connection.owner)
            .filter(starts_at__gte=now - HISTORY)
            .select_related("application__posting__company", "reminder")
            .prefetch_related("contacts")
        }
        linked_hrefs = {link.remote_href for link in links.values()}

        for pk, interview in interviews.items():
            link = links.get(pk)
            if link is not None and link.remote_gone:
                continue  # removed from the calendar on purpose; leave it removed
            source = digest(interview_source(interview))
            if link is None:
                if read_only:
                    report.skipped += 1
                    continue
                href = urljoin(calendar.href, f"{interview.uid}.ics")
                etag = server.put(href, event_text(interview), "text/calendar; charset=utf-8")
                SyncLink.bind(
                    connection,
                    interview,
                    remote_href=href,
                    uid=interview.uid,
                    etag=etag,
                    local_hash=source,
                    last_synced_at=now,
                )
                report.pushed += 1
                continue
            if link.remote_href not in remote:
                link.remote_gone = True
                link.save(update_fields=["remote_gone", "updated_at"])
                report.removed += 1
                record_event(
                    interview.application,
                    summary=str(
                        _("%(kind)s removed from the calendar on another device; kept here.")
                    )
                    % {"kind": interview.get_kind_display()},
                    actor=ACTOR,
                )
                continue
            remote_changed = remote[link.remote_href] != link.etag
            local_changed = link.local_hash != source
            if remote_changed:
                resource = server.get(link.remote_href)
                events = [e for e in text.text_to_events(resource.text) if not e.recurring]
                incoming = events[0] if events else None
                if incoming is None:
                    report.skipped += 1
                    continue
                if local_changed and (incoming.last_modified or now) < interview.updated_at:
                    record_event(
                        interview.application,
                        summary=str(_("Changed on the phone, but changed here later; kept ours.")),
                        body=incoming.digest_source().replace("\n", " · "),
                        actor=ACTOR,
                    )
                    link.etag = resource.etag
                else:
                    if local_changed:
                        record_event(
                            interview.application,
                            summary=str(
                                _("Changed here, but changed on the phone later; took theirs.")
                            ),
                            body=interview_source(interview).replace("\n", " · "),
                            actor=ACTOR,
                        )
                    apply_event(interview, incoming, report)
                    interview.refresh_from_db()
                    link.etag = resource.etag
                    link.local_hash = digest(interview_source(interview))
                    link.last_synced_at = now
                    link.save()
                    report.pulled += 1
                    continue
            if local_changed:
                if read_only:
                    report.skipped += 1
                    continue
                try:
                    etag = server.put(
                        link.remote_href,
                        event_text(interview),
                        "text/calendar; charset=utf-8",
                        etag=link.etag,
                    )
                except Conflict:
                    report.notes.append(
                        str(_("An interview changed on both sides at once; next run decides."))
                    )
                    continue
                link.etag, link.local_hash, link.last_synced_at = etag, source, now
                link.save()
                report.pushed += 1

        for link in list(links.values()):
            if link.object_id not in interviews:
                still_exists = Interview.objects.filter(pk=link.object_id).exists()
                if still_exists:
                    continue  # merely too old for the window; leave the calendar alone
                if not read_only and link.remote_href in remote:
                    if drop_twin(server, link, report, EVENT_KEPT):
                        report.removed += 1
                link.delete()

        foreign = [href for href in remote if href not in linked_hrefs]
        if foreign:
            report.skipped += len(foreign)
            report.notes.append(
                str(_("%(count)d events on the calendar are not from Postulo and were left alone."))
                % {"count": len(foreign)}
            )
