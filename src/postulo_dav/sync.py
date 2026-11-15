"""The sync: contacts ↔ the *Postulo* address book, interviews ↔ the *Postulo* calendar.

One run does, for each of the two collections:

1. **List** the collection: every resource with its ETag.
2. **Pair** what lost its pairing: a twin that moved to another address is found again by its
   ``UID``, and a record whose twin is already there (a connection deleted and added again) is
   paired with it instead of being pushed a second time (#7).
3. **Push** each local record — a contact, an upcoming or recent interview — that has no
   twin yet, or whose content changed since it was last pushed. A write is conditional
   on the ETag last seen, so a change made on the phone in the meantime is noticed rather
   than overwritten: the later change wins, and the losing version is kept as a note.
4. **Pull** each twin whose ETag changed since it was last seen, and apply it locally.
   Interview times move through Postulo's own service, so the timeline says so.
5. **Notice deletions**. A twin that vanished from the collection is *unlinked and
   flagged* — the local record stays, and the report says what happened. A record
   deleted here takes its twin with it, but only the twin it was told about: the DELETE
   carries ``If-Match``, so a card or an event edited on the phone since the last sync
   survives the deletion and becomes a note instead (#2).
6. **Adopt** what appeared in the collection from elsewhere: a contact created on the
   phone becomes a contact here, under the company its ORG names or a holding company.
   An event created on the phone is left alone with a note: nothing links it to an
   application, and a job tracker must not invent one.

One card, or one event, that cannot be read or saved costs that one and not the run: it is
a note and a count, and the next resource is tried. The contacts and the interviews are
two halves of a run, and one failing does not stop the other (#20, #34).

Recurring events are ignored: interviews do not recur. Postulo writes no attendee and no
preparation notes into the calendar (postulo/postulo#963); what the person types in an
event's description on the phone is theirs, and comes back as the interview's notes without
the push ever overwriting it. Read-only mode does steps 1, 2, 4, 5 and 6 and never writes —
and *never writes* includes the collections themselves: a person who asked for read only,
and a person who merely pressed *Test*, gets no address book and no calendar made for them
on their server. Both look the collections up and report what is not there (#2).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext

# One import, and it is a promise. Everything this plugin needs from Postulo is on
# `postulo.plugins.api` and stays working across a minor release; anything else in
# `postulo` is this month's internals. That was not true when this file was written — a
# two-way sync needs the records themselves, the calls that move them so the timeline says
# so, and the link table that remembers which twin is whose, and none of it was on the
# surface — so the block below carried nine imports and a paragraph counting what they
# risked. postulo/postulo#229 put them on it, and `tests/test_surface.py` holds this file
# to the line.
from postulo.plugins.api import (
    AlreadyPaired,
    Contact,
    DestinationRefused,
    DiscoveredOption,
    FieldSpec,
    Interview,
    InterviewOutcome,
    LinkKind,
    RemoteItem,
    SyncLink,
    SyncReport,
    TestResult,
    calendar_status,
    check_destination,
    event_lines,
    get_or_create_company,
    phone_number_is_taken,
    primary_phone_number,
    primary_web_link,
    record_event,
    reschedule_interview,
    save_phone_number,
    save_web_link,
    settle_interview,
)

from . import __version__, dav, text
from .dav import (
    ADDRESSBOOK,
    CALENDAR,
    READ,
    READ_WRITE,
    AuthenticationRefused,
    Conflict,
    DavError,
    Server,
)

logger = logging.getLogger(__name__)

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
#: The columns of a link, which a longer address or identifier would overflow (#34).
HREF_LENGTH = 500
UID_LENGTH = 200
#: Contact.name and Company.name.
NAME_LENGTH = 200

#: What one resource may fail with without ending the run: the server refusing it, or the
#: database refusing what it said. The first is a sentence; the second is not worth one.
TOLERATED = (DavError, DatabaseError, ValidationError, ValueError, AlreadyPaired)

#: A UID Postulo itself wrote for a contact: ``contact-<pk>-<owner>@postulo``.
POSTULO_CARD_UID = re.compile(r"^contact-\d+-\d+@postulo$")


#: A UID Postulo minted for an interview: ``<uuid>@postulo``. Any other UID on a paired event
#: is somebody else's, and the event is patched and never rebuilt (#11).
POSTULO_UID = re.compile(r"@postulo$")
#: How far ahead the events offered for pairing reach.
PAIRING_AHEAD = dt.timedelta(days=365)


def made_elsewhere(link, interview=None) -> bool:
    """Whether the twin of ``link`` is an event somebody else made (#11).

    Postulo's own events carry the interview's UID. A pairing the person made by hand with an
    event from the phone or a colleague carries that event's own, which is kept as it is: a
    write to such an event is a patch of the properties Postulo owns, never a rebuilt event
    (RFC 4791 §5.3.2.1 forbids changing its UID), and deleting the interview unpairs it
    rather than delete an event Postulo did not make.
    """
    uid = link.uid or ""
    if interview is not None:
        return bool(uid) and uid != interview.uid
    return bool(uid) and not POSTULO_UID.search(uid)


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


@dataclass
class Run:
    """What one half of a sync shares: the server, the connection, the report, the clock."""

    server: Server
    connection: object
    report: SyncReport
    read_only: bool
    now: dt.datetime = field(default_factory=timezone.now)

    @property
    def owner(self):
        return self.connection.owner

    def canon(self, href: str) -> str:
        return self.server.canonical(href)

    def held(self, links: dict, collection) -> set[int]:
        """The records paired with something outside ``collection``: not this run's to touch.

        A connection pointed at another collection than the one it first used leaves such
        links behind. Their twins are neither pushed to, nor deleted, nor taken for
        removed, and the report counts them (#13).
        """
        outside = {
            pk
            for pk, link in links.items()
            if link.remote_href and not self.server.within(collection.href, link.remote_href)
        }
        if outside:
            self.report.skipped += len(outside)
            self.report.notes.append(
                _(
                    "%(count)d records are paired with something outside the chosen "
                    "collection and were left alone."
                )
                % {"count": len(outside)}
            )
        return outside

    def skip(self, name: str, error: BaseException) -> None:
        """One resource that could not be handled: counted, named, and the run goes on."""
        self.report.skipped += 1
        reason = str(error) if isinstance(error, DavError) else _("it could not be saved here")
        self.report.notes.append(
            _("“%(name)s” was skipped: %(reason)s") % {"name": name[:120], "reason": reason}
        )

    def each(self, name: str, work) -> None:
        """Do ``work`` for one resource, all or nothing, and survive it failing (#20, #34)."""
        try:
            with transaction.atomic():
                work()
        except TOLERATED as error:
            self.skip(name, error)


def _is_href(name: str) -> bool:
    """Whether what was typed for a collection is its address and not its name."""
    return name.startswith(("/", "https://", "http://"))


def _tail(href: str) -> str:
    return urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]


# ------------------------------------------------------------------ contacts


def card_for(contact: Contact, reader=None, uid: str = "") -> text.Card:
    """The card for ``contact`` as ``reader`` sees it: their own note, their own numbers.

    The note is each person's own since postulo/postulo#896: read for nobody, it raises. A
    queryset that read the contact with ``with_notes`` has set it already; otherwise it is
    asked for. ``uid`` is the identifier the card already has on the server, and is never
    changed: a server may refuse a write that does (RFC 6352 §5.1, #7).
    """
    reader = reader or contact.owner
    # A card carries one number and one address on the web; a contact holds several of
    # each since Postulo 0.3, with one of each kind marked as the one to use. The card
    # gets the primaries, which is what "the number" and "the LinkedIn" mean there now.
    number = primary_phone_number(contact)
    profile = primary_web_link(contact, LinkKind.SOCIAL)
    note = contact.notes if "notes" in contact.__dict__ else contact.note_for(reader)
    return text.Card(
        uid=uid or f"contact-{contact.pk}-{reader.pk}@postulo",
        name=contact.name,
        org=contact.company.name if contact.company_id else "",
        title=contact.role,
        email=contact.email,
        phone=number.number if number else "",
        url=profile.url if profile else "",
        note=note,
    )


def web_address(url: str) -> str:
    """``url`` if it is a complete http(s) address, else nothing.

    A card's ``URL`` is whatever somebody typed, and the page that shows a contact writes it
    into an ``href``. ``addresses.web_address`` in Postulo is not on the plugin surface, so
    the two-line rule is kept here (#32).
    """
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme.lower() in ("http", "https") and parts.netloc and not text.CONTROLS & set(url):
        return url
    return ""


def apply_card(contact: Contact, card: text.Card, owner, report: SyncReport | None = None) -> bool:
    """Write a card onto the contact. ``False`` if some of it could not be taken.

    Only what the card says is written: a card without a ``TEL`` leaves the number here
    alone, and one without a ``URL`` the link, because this plugin never deletes what the
    core keeps (#26). A number another record holds is a note, never a failed run.
    """
    notes = report.notes if report is not None else []
    contact.name = (card.name or contact.name)[:NAME_LENGTH]
    contact.role = card.title[:200]
    contact.email = card.email[:254]
    if card.org and (not contact.company_id or contact.company.name.lower() != card.org.lower()):
        contact.company = get_or_create_company(owner, card.org[:NAME_LENGTH])
    contact.save()
    contact.write_note(owner, card.note)
    complete = True
    # The number and the profile are rows of their own, written once the contact has a
    # key to hang them on. Only the primary of each is touched: the rest of what the
    # person keeps here is not the card's to lose.
    number = card.phone[:40].strip()
    if number:
        primary = primary_phone_number(contact)
        if phone_number_is_taken(
            number, exclude_pk=primary.pk if primary else None, asked_by=owner
        ):
            complete = False
            notes.append(
                _(
                    "“%(name)s”: the number on the card is already held by another record "
                    "here, so it was not taken."
                )
                % {"name": contact.name}
            )
        else:
            try:
                with transaction.atomic():
                    save_phone_number(contact, owner, number)
            except (ValidationError, ValueError):
                complete = False
                notes.append(
                    _("“%(name)s”: the number on the card was not taken.") % {"name": contact.name}
                )
    if card.url.strip():
        url = web_address(card.url)[:500]
        if not url:
            notes.append(
                _(
                    "“%(name)s”: the web address on the card is not a web address, so it was "
                    "not taken."
                )
                % {"name": contact.name}
            )
        else:
            try:
                with transaction.atomic():
                    save_web_link(contact, owner, LinkKind.SOCIAL, url)
            except (ValidationError, ValueError):
                notes.append(
                    _("“%(name)s”: the web address on the card was not taken.")
                    % {"name": contact.name}
                )
    return complete


# ---------------------------------------------------------------- interviews


def application_url(interview: Interview) -> str:
    """The application's page, absolute, when the instance knows its own address (#11)."""
    base = str(getattr(settings, "POSTULO_PUBLIC_URL", "") or "").rstrip("/")
    return f"{base}{interview.application.get_absolute_url()}" if base else ""


def _description_of(lines: list[str]) -> tuple[int | None, str]:
    """Where the event's own DESCRIPTION is in ``lines`` (an alarm's is not), and its text."""
    depth = 0
    for index, line in enumerate(lines):
        upper = line.upper()
        if upper.startswith("BEGIN:") and upper != "BEGIN:VEVENT":
            depth += 1
        elif upper.startswith("END:") and upper != "END:VEVENT":
            depth -= 1
        elif depth == 0 and upper.startswith("DESCRIPTION"):
            prop = text.parse_line(line)
            if prop is not None and prop.name == "DESCRIPTION":
                return index, text.unescape(prop.value)
    return None, ""


def postulo_block(interview: Interview) -> str:
    """The description Postulo itself writes: who is met, and the application's link."""
    return _description_of(event_lines(interview, url=application_url(interview)))[1]


def free_text(description: str, block: str) -> str:
    """What a person wrote in an event's description: all of it but Postulo's own lines."""
    ours = [line for line in block.split("\n") if line]
    kept = []
    for line in description.replace("\r\n", "\n").split("\n"):
        if line in ours:
            ours.remove(line)
        else:
            kept.append(line)
    return "\n".join(kept).strip()


def event_text(interview: Interview, free: str = "", *, method: bool = True) -> str:
    """The whole iCalendar document for one interview, alarm included.

    `alarm=True` is what writing into somebody's *own* calendar means: the reminder is
    theirs and they set it in Postulo, so their phone should ring for it. The feed Postulo
    serves carries no alarm, because a subscription that rings is a decision for whoever
    subscribes. Assembling the VALARM here was this plugin's job until the core took it
    (postulo/postulo#229).

    The event carries no attendee and no notes (postulo/postulo#963), and the document says
    ``METHOD:PUBLISH`` so that a calendar program does not take it for an invitation. What
    a person wrote in the description on the phone is ``free``, and stays above Postulo's
    own lines.
    """
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
    if method:
        lines.append("METHOD:PUBLISH")
    body = event_lines(interview, url=application_url(interview), alarm=True)
    if free:
        index, ours = _description_of(body)
        description = "DESCRIPTION:" + text.escape("\n".join(part for part in (free, ours) if part))
        if index is None:
            body.insert(len(body) - 1, description)
        else:
            body[index] = description
    return text.render(lines + body + ["END:VCALENDAR"])


def core_source(interview: Interview) -> str:
    """The parts of an interview a calendar can change, as one string."""
    return text.Event(
        starts_at=interview.starts_at,
        ends_at=interview.ends_at,
        location=interview.location,
        status=calendar_status(interview),
    ).digest_source()


def interview_source(interview: Interview) -> str:
    """What a push is a function of: those parts, and the lines Postulo writes itself."""
    return core_source(interview) + "\n" + postulo_block(interview)


def event_source(event: text.Event) -> str:
    """The same, as the other side holds it: the notes and Postulo's lines are no part."""
    return text.Event(
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        location=event.location,
        status=event.status or "CONFIRMED",
    ).digest_source()


def _problem(event: text.Event) -> str:
    kind, _sep, zone = event.problem.partition(":")
    if kind == "floating":
        return _("it has no time zone")
    if kind == "date":
        return _("it is an all-day event")
    if kind == "zone":
        return _("its time zone “%(zone)s” is not known here") % {"zone": zone}
    return _("its time cannot be read")


def apply_event(
    interview: Interview,
    event: text.Event,
    report: SyncReport,
    block: str = "",
    *,
    notes: bool = True,
) -> None:
    """Take from the event what a calendar may change; ``notes=False`` leaves the notes alone.

    An event somebody else made exchanges times, place and cancellation only: its
    description is theirs, and is never taken for the interview's notes (#11).
    """
    kind = interview.get_kind_display()
    if event.starts_at and event.ends_at:
        if event.ends_at > event.starts_at:
            reschedule_interview(
                interview, starts_at=event.starts_at, ends_at=event.ends_at, actor=ACTOR
            )
        else:
            report.notes.append(
                _("The %(kind)s on the calendar ends before it starts, so it was not moved.")
                % {"kind": kind}
            )
    elif event.problem:
        # A time that is not an instant is not a time to move an interview to (#24).
        report.notes.append(
            _(
                "The time of the %(kind)s on the calendar could not be used (%(why)s), so it "
                "was not moved."
            )
            % {"kind": kind, "why": _problem(event)}
        )
    changed = []
    if event.location != interview.location:
        interview.location = event.location[:500]
        changed.append("location")
    written = free_text(event.description, block) if notes else ""
    if written and written not in interview.notes:
        # What was typed on the phone is the person's own: added to the notes, or taking
        # the place of the notes it extends, and the same text is never added twice.
        if interview.notes and interview.notes in written:
            interview.notes = written
        elif interview.notes:
            interview.notes = f"{interview.notes}\n\n{written}"
        else:
            interview.notes = written
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
                        "The server's address, e.g. https://cloud.example.org, or the address "
                        "of its DAV service where it lives under a path, e.g. "
                        "https://example.org/dav.php/. Postulo finds the address book and "
                        "calendar homes from there."
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
                required=False,
                help=str(
                    _(
                        "Used only while no collection is chosen below. A name or an address "
                        "of an address book; one that is not there is made by the first sync, "
                        "unless read only is on. The whole address book syncs, and a name two "
                        "address books share is refused."
                    )
                ),
            ),
            FieldSpec(
                "calendar",
                str(_("Calendar")),
                type="text",
                default="Postulo",
                required=False,
                help=str(
                    _(
                        "Used only while no collection is chosen below. A name or an address "
                        "of a calendar; one that is not there is made by the first sync, "
                        "unless read only is on. The whole calendar syncs, and a name two "
                        "calendars share is refused."
                    )
                ),
            ),
            FieldSpec(
                "collections",
                str(_("Collections")),
                type="discovered",
                required=False,
                modes=((READ, str(_("Read only"))), (READ_WRITE, str(_("Read and write")))),
                help=str(
                    _(
                        "Press Look on the server, then choose one address book and one "
                        "calendar. Only those are read or written, and a mode set here is "
                        "Postulo's word, not the server's lock: an app password cannot be "
                        "narrowed to one collection. Whole collections sync."
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
        if self._chosen(config):
            parts.append(
                ngettext(
                    "%(count)d collection chosen",
                    "%(count)d collections chosen",
                    len(self._chosen(config)),
                )
                % {"count": len(self._chosen(config))}
            )
        else:
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
        notes: list[str] = []
        try:
            client, server = dav.connect(config)
            with client:
                chosen = self._resolve(server, config, notes)
                if chosen is not None:
                    return self._tested_choice(chosen, notes)
                found = {
                    kind: self._collection(server, config, kind, create=False)
                    for kind in (ADDRESSBOOK, CALENDAR)
                }
        except DavError as error:
            # Already a sentence for the person, in their language: no class name in front.
            return TestResult(False, str(error))
        except Exception as error:  # noqa: BLE001 - the sentence is the point
            return TestResult(False, f"{type(error).__name__}: {error}")
        missing = [kind for kind, collection in found.items() if collection is None]
        if not missing:
            return TestResult(
                True,
                str(
                    _(
                        "Found the address book “%(book)s” (%(book_href)s) and the calendar "
                        "“%(calendar)s” (%(calendar_href)s)."
                    )
                )
                % {
                    "book": found[ADDRESSBOOK].name,
                    "book_href": urlsplit(found[ADDRESSBOOK].href).path,
                    "calendar": found[CALENDAR].name,
                    "calendar_href": urlsplit(found[CALENDAR].href).path,
                },
            )
        sentences = [self._missing_sentence(config, kind) for kind in missing]
        # Signing in worked, which is what *Test* is for; what is missing is only a
        # problem the sync cannot solve when it is forbidden to write.
        if any(not _is_href(self._name(config, kind)) for kind in missing):
            sentences.append(str(READ_ONLY_TAIL if read_only else FIRST_SYNC_TAIL))
        return TestResult(not read_only, " ".join(sentences))

    def _tested_choice(self, chosen: dict, notes: list[str]) -> TestResult:
        sentences = []
        for kind, (collection, mode) in chosen.items():
            what = _("address book") if kind == ADDRESSBOOK else _("calendar")
            how = _("read only") if mode == READ else _("read and write")
            sentences.append(
                str(_("Found the %(what)s “%(name)s” (%(how)s)."))
                % {"what": what, "name": collection.name or _tail(collection.href), "how": how}
            )
        if not chosen:
            sentences.append(str(_("None of the chosen collections is on the server.")))
        return TestResult(bool(chosen), " ".join([*sentences, *notes]))

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
        """The collection a name, or an address, stands for. ``create=False`` never makes one.

        What a connection that chooses no collection does, as it always has; it is held to
        the one it found, in the mode read only sets. An address is looked up and never
        made: it names a collection that is there or is not.
        """
        mode = READ if config.get("read_only") else READ_WRITE
        name = self._name(config, kind)
        if _is_href(name):
            found = server.describe(name)
            if found is not None and found.kind != kind:
                raise DavError(
                    _("“%(name)s” is not an address book.") % {"name": name}
                    if kind == ADDRESSBOOK
                    else _("“%(name)s” is not a calendar.") % {"name": name}
                )
        else:
            well_known = "carddav" if kind == ADDRESSBOOK else "caldav"
            principal = server.principal(well_known)
            home = server.home(principal, kind)
            found = server.find_collection(home, name, kind)
            if found is None and create:
                found = server.create_collection(home, name, kind)
        if found is not None:
            server.allow(found.href, mode)
        return found

    # ------------------------------------------------------------ choosing

    @staticmethod
    def _chosen(config: dict) -> dict[str, str]:
        """The collections the person chose, as address → mode; empty when none were."""
        value = config.get("collections")
        if not isinstance(value, dict):
            return {}
        return {
            str(href): (READ_WRITE if mode == READ_WRITE else READ)
            for href, mode in value.items()
            if href and mode
        }

    def _resolve(
        self, server: Server, config: dict, notes: list[str]
    ) -> dict[str, tuple[dav.Collection, str]] | None:
        """What was chosen, looked up by address: kind → (collection, mode); ``None`` if none.

        Each address is asked about once. One that is no longer on the server is reported
        and never made again. A mode of *read and write* the server does not allow is
        lowered to *read*, with a word. Only the collections kept are allowed, so no
        request goes to any other. One address book and one calendar are used at most.
        """
        wanted = self._chosen(config)
        if not wanted:
            return None
        read_only = bool(config.get("read_only"))
        found: dict[str, list[tuple[dav.Collection, str]]] = {ADDRESSBOOK: [], CALENDAR: []}
        for href, mode in wanted.items():
            try:
                collection = server.describe(href)
            except AuthenticationRefused:
                raise
            except DavError as error:
                notes.append(
                    str(_("“%(href)s” could not be read: %(error)s"))
                    % {"href": href, "error": error}
                )
                continue
            if collection is None:
                notes.append(
                    str(
                        _(
                            "The collection “%(href)s” is no longer on the server. It was "
                            "not made again; choose another, or let it go."
                        )
                    )
                    % {"href": href}
                )
                continue
            if collection.kind == CALENDAR and not collection.accepts_events:
                notes.append(
                    str(_("The calendar “%(name)s” does not take events, so it was not used."))
                    % {"name": collection.name or _tail(collection.href)}
                )
                continue
            if mode == READ_WRITE and (read_only or collection.writable is False):
                if not read_only:
                    notes.append(
                        str(_("The server does not let you write to “%(name)s”: read only."))
                        % {"name": collection.name or _tail(collection.href)}
                    )
                mode = READ
            found[collection.kind].append((collection, mode))
        chosen: dict[str, tuple[dav.Collection, str]] = {}
        for kind, entries in found.items():
            if len(entries) > 1:
                notes.append(
                    str(
                        _(
                            "More than one %(what)s is chosen, and a connection syncs one: "
                            "none was used. Add a connection for each, or let the others go."
                        )
                    )
                    % {"what": _("address book") if kind == ADDRESSBOOK else _("calendar")}
                )
            elif entries:
                collection, mode = entries[0]
                server.allow(collection.href, mode)
                chosen[kind] = (collection, mode)
        return chosen

    def discover(self, config: dict) -> dict[str, list[DiscoveredOption]]:
        """What the server has to choose from: every address book and calendar of every home."""
        options: dict[str, DiscoveredOption] = {}
        client, server = dav.connect(config)
        with client:
            for kind, group in (
                (ADDRESSBOOK, str(_("Address books"))),
                (CALENDAR, str(_("Calendars"))),
            ):
                principal = server.principal("carddav" if kind == ADDRESSBOOK else "caldav")
                for home in server.homes(principal, kind):
                    for collection in server.collections(home):
                        if collection.kind != kind or (
                            kind == CALENDAR and not collection.accepts_events
                        ):
                            continue
                        writable = collection.writable
                        note = ""
                        if writable is False:
                            note = str(_("Read only on the server"))
                        elif writable is None:
                            note = str(_("The server does not say whether it may be written"))
                        options.setdefault(
                            collection.href,
                            DiscoveredOption(
                                collection.href,
                                collection.name or _tail(collection.href),
                                group=group,
                                modes=(READ,) if writable is False else (),
                                note=note,
                            ),
                        )
        return {"collections": list(options.values())}

    # ------------------------------------------------------------ pairing

    def remote_items(
        self, config: dict, search: str = "", kind: str = "", key: tuple[str, str] | None = None
    ) -> list[RemoteItem]:
        """Events a person may pair with an interview, from the chosen calendar (#11).

        Asked live, and answered from one ``calendar-query`` with a time range (RFC 4791
        §7.8): the interviews' own window, and a year ahead. Events Postulo made, repeating
        events and events without a UID are not offered: the first are paired already, and an
        interview is one meeting. Nothing is written.
        """
        if kind not in ("", "interview"):
            return []
        client, server = dav.connect(config)
        with client:
            notes: list[str] = []
            chosen = self._resolve(server, config, notes)
            if chosen is not None:
                picked = chosen.get(CALENDAR)
                collection = picked[0] if picked else None
            else:
                collection = self._collection(server, config, CALENDAR, create=False)
            if collection is None:
                return []
            now = timezone.now()
            found = server.query_events(
                collection.href, text.stamp(now - HISTORY), text.stamp(now + PAIRING_AHEAD)
            )
            where = server.canonical(collection.href)
            if key is not None and server.canonical(key[0]) != where:
                return []
            words = search.strip().casefold()
            items: list[tuple[dt.datetime, RemoteItem]] = []
            for href, resource in found.items():
                for event in text.text_to_events(resource.text):
                    if event.recurring or not event.uid or POSTULO_UID.search(event.uid):
                        continue
                    if key is not None and event.uid != key[1]:
                        continue
                    if words and words not in f"{event.summary} {event.location}".casefold():
                        continue
                    when = (
                        event.starts_at.astimezone(timezone.get_current_timezone())
                        if event.starts_at
                        else None
                    )
                    detail = " · ".join(
                        part
                        for part in (
                            when.strftime("%Y-%m-%d %H:%M") if when else "",
                            event.location,
                        )
                        if part
                    )
                    items.append(
                        (
                            event.starts_at or now,
                            RemoteItem(
                                collection=where,
                                uid=event.uid,
                                label=event.summary or str(_("(no title)")),
                                collection_label=collection.name or _tail(collection.href),
                                href=href,
                                detail=detail,
                                kinds=("interview",),
                            ),
                        )
                    )
            return [item for _when, item in sorted(items, key=lambda pair: pair[0])]

    # -------------------------------------------------------------- sync

    def sync(self, connection, config: dict) -> SyncReport:
        report = SyncReport()
        read_only = bool(config.get("read_only"))
        errors: list[str] = []
        client, server = dav.connect(config)
        with client:
            run = Run(server, connection, report, read_only)
            try:
                chosen = self._resolve(server, config, report.notes)
            except AuthenticationRefused as error:
                report.error = str(error)
                return report
            except httpx.HTTPError as error:
                report.error = f"{type(error).__name__}: {error}"
                return report
            for kind, half in (
                (ADDRESSBOOK, self._sync_contacts),
                (CALENDAR, self._sync_interviews),
            ):
                # Each half on its own: a server with no calendar home still gets the
                # contacts, and one failing says so without stopping the other (#20).
                try:
                    if chosen is not None:
                        # What was chosen is all there is: a kind with nothing chosen is
                        # not synced, and nothing is looked up or made by name.
                        if kind not in chosen:
                            continue
                        collection, mode = chosen[kind]
                        run.read_only = mode == READ
                    else:
                        # In read-only mode a missing collection is reported, not made:
                        # half a sync still runs, and the person is told which half did not.
                        collection = self._collection(server, config, kind, create=not read_only)
                        run.read_only = read_only
                        if collection is None:
                            tail = (
                                "" if _is_href(self._name(config, kind)) else f" {READ_ONLY_TAIL}"
                            )
                            report.notes.append(
                                f"{self._missing_sentence(config, kind)}{tail if read_only else ''}"
                            )
                            continue
                    half(run, collection)
                except AuthenticationRefused as error:
                    errors.append(str(error))
                    break  # the other half would be refused the same way
                except DavError as error:
                    errors.append(str(error))
                except httpx.HTTPError as error:
                    errors.append(f"{type(error).__name__}: {error}")
                    break  # the server is not answering; asking again would only wait twice
                except Exception as error:
                    logger.exception("The %s half of a DAV sync failed", kind)
                    errors.append(f"{type(error).__name__}: {error}")
        if errors:
            report.error = " ".join(dict.fromkeys(errors))
        return report

    # ---------------------------------------------------------- contacts

    def _sync_contacts(self, run: Run, book) -> None:
        server, connection, report = run.server, run.connection, run.report
        owner, now, read_only, canon = run.owner, run.now, run.read_only, run.canon
        remote = server.etags(book.href)
        # Links flagged remote_gone are kept in the map: they say "the person removed
        # this from the address book", and the record must not be pushed back.
        links = {link.object_id: link for link in SyncLink.of_model(connection, Contact)}
        held = run.held(links, book)
        # The numbers and the links are prefetched for the card's primaries: two queries a
        # run, where a core that reads the primary from prefetched rows (postulo/postulo#1171)
        # asked two per contact (#47). An older core still asks per contact, as before.
        contacts = {
            c.pk: c
            for c in Contact.objects.visible_to(owner)
            .with_notes(owner)
            .select_related("company")
            .prefetch_related("phone_numbers", "web_links")
        }
        # A contact may be a group's, or shared to read: what comes in from the phone is
        # applied only to those the person may change (postulo/postulo#896).
        writable = set(Contact.objects.visible_to(owner, write=True).values_list("pk", flat=True))
        cache: dict[str, tuple[dav.Resource, text.Card]] = {}
        fetched: dict[str, dav.Resource] = {}

        def read(href: str):
            key = canon(href)
            if key not in cache:
                resource = fetched.pop(key, None) or server.get(href)
                cache[key] = (resource, text.text_to_card(resource.text))
            return cache[key]

        def linked() -> set[str]:
            return {canon(link.remote_href) for link in links.values() if link.remote_href}

        def strangers() -> list[str]:
            """The cards read that no link claims: adopted below, unless paired first."""
            claimed = linked()
            return [h for h in remote if h in cache and h not in claimed]

        # Every card this run will read, asked for in a few multiget REPORTs rather than a
        # GET each (#45): those no link claims, and the twins whose ETag moved.
        unclaimed = [h for h in remote if h not in linked()]
        moved = [
            canon(link.remote_href)
            for link in links.values()
            if link.remote_href
            and link.object_id in contacts
            and link.paired
            and not link.remote_gone
            and remote.get(canon(link.remote_href), link.etag) != link.etag
        ]
        fetched.update(server.multiget(book.href, unclaimed + moved, ADDRESSBOOK))

        # Read what no link claims: it is what a moved twin and an old pairing are found in,
        # and what is adopted below.
        for href in unclaimed:
            try:
                read(href)
            except DavError as error:
                run.skip(_tail(href), error)

        # Pair: a twin that moved is found again by its UID, never by its address (#7).
        for link in list(links.values()):
            contact = contacts.get(link.object_id)
            if contact is None or not link.uid or not link.paired or link.object_id in held:
                continue
            if canon(link.remote_href) in remote and not link.remote_gone:
                continue
            found = next((h for h in strangers() if cache[h][1].uid == link.uid), None)
            if found is None or len(found) > HREF_LENGTH:
                continue
            resource, card = cache[found]
            same = digest(card.digest_source()) == digest(
                card_for(contact, owner, link.uid).digest_source()
            )
            link.remote_href, link.remote_gone = found, False
            link.etag = resource.etag if same else ""
            link.save(update_fields=["remote_href", "remote_gone", "etag", "updated_at"])
            report.notes.append(
                _("“%(name)s” moved to another address on the server; still paired.")
                % {"name": contact.name}
            )

        # Pair: a contact with no twin yet whose twin is already there, as after a connection
        # was deleted and added again. Postulo's own UID counts only with the same name, since
        # after an export and an import its number may name somebody else; a card from the
        # phone is matched on an email address that exactly one contact and one card share.
        lonely = [c for pk, c in contacts.items() if pk not in links]
        emails: dict[str, int] = {}
        for contact in lonely:
            if contact.email:
                key = contact.email.strip().lower()
                emails[key] = emails.get(key, 0) + 1
        for contact in lonely:
            default_uid = f"contact-{contact.pk}-{owner.pk}@postulo"
            name = contact.name.strip().lower()
            found = next(
                (
                    h
                    for h in strangers()
                    if cache[h][1].uid == default_uid and cache[h][1].name.strip().lower() == name
                ),
                None,
            )
            key = contact.email.strip().lower()
            if found is None and key and emails.get(key) == 1:
                matches = [
                    h
                    for h in strangers()
                    if cache[h][1].email.strip().lower() == key
                    and not POSTULO_CARD_UID.match(cache[h][1].uid)
                ]
                if len(matches) == 1:
                    found = matches[0]
            if found is None or len(found) > HREF_LENGTH:
                continue
            resource, card = cache[found]
            uid = card.uid if card.uid and len(card.uid) <= UID_LENGTH else default_uid
            mine = digest(card_for(contact, owner, uid).digest_source())
            same = digest(card.digest_source()) == mine
            try:
                links[contact.pk] = SyncLink.bind(
                    connection,
                    contact,
                    remote_href=found,
                    uid=uid,
                    etag=resource.etag if same else "",
                    local_hash=mine if same else "",
                    last_synced_at=now if same else None,
                )
            except AlreadyPaired:
                continue  # that card is another contact's by now: leave both alone

        # Local records: push new and changed ones; notice twins that vanished.
        def one(contact: Contact) -> None:
            link = links.get(contact.pk)
            if contact.pk in held:
                return  # paired with something outside the chosen address book
            if link is not None and (link.remote_gone or not link.paired):
                return  # removed from the address book on purpose, or unpaired: leave it
            card = card_for(contact, owner, link.uid if link else "")
            source = digest(card.digest_source())
            if link is None:
                if read_only:
                    report.skipped += 1
                    return
                href = urljoin(book.href, f"{uuid.uuid4()}.vcf")
                etag = server.put(
                    href,
                    text.card_to_text(card, prodid=PRODID, rev=now),
                    "text/vcard; charset=utf-8",
                )
                links[contact.pk] = SyncLink.bind(
                    connection,
                    contact,
                    remote_href=canon(href),
                    uid=card.uid,
                    etag=etag,
                    local_hash=source,
                    last_synced_at=now,
                )
                report.pushed += 1
                return
            href = canon(link.remote_href)
            if href not in remote:
                link.remote_gone = True
                link.save(update_fields=["remote_gone", "updated_at"])
                report.removed += 1
                report.notes.append(
                    str(_("“%(name)s” was removed from the address book; kept here."))
                    % {"name": contact.name}
                )
                return
            remote_changed = remote[href] != link.etag
            local_changed = link.local_hash != source
            if remote_changed:
                resource, incoming = read(link.remote_href)
                if local_changed and digest(incoming.digest_source()) == source:
                    # Both sides already say the same: nothing to merge, only to remember.
                    link.etag, link.local_hash, link.last_synced_at = resource.etag, source, now
                    link.uid = link.uid or card.uid
                    link.save()
                    return
                if contact.pk not in writable:
                    link.etag = resource.etag
                    link.save(update_fields=["etag", "updated_at"])
                    report.skipped += 1
                    report.notes.append(
                        _(
                            "“%(name)s” is shared with you to read only; the change on the "
                            "phone was not applied."
                        )
                        % {"name": contact.name}
                    )
                    return
                if local_changed and (incoming.rev or now) < contact.updated_at:
                    # Both changed and ours is later: ours wins, theirs is kept as a note.
                    contact.write_note(
                        owner,
                        (contact.notes + "\n\n" if contact.notes else "")
                        + _("Changed on the phone on %(when)s: %(text)s")
                        % {
                            "when": (incoming.rev or now).date().isoformat(),
                            "text": incoming.digest_source().replace("\n", " · "),
                        },
                    )
                    # The version just recorded is remembered at once, whatever happens to
                    # the push: a skipped or failed one must not record it again (#21).
                    link.etag = resource.etag
                    link.save(update_fields=["etag", "updated_at"])
                    card = card_for(contact, owner, link.uid)
                    source = digest(card.digest_source())
                    local_changed = True
                else:
                    if local_changed:
                        losing = card.digest_source().replace("\n", " · ")
                        incoming.note = (incoming.note + "\n\n" if incoming.note else "") + str(
                            _("Changed here on %(when)s: %(text)s")
                        ) % {"when": contact.updated_at.date().isoformat(), "text": losing}
                        if incoming.rev is None:
                            # No time on the card to weigh against ours: the phone's wins,
                            # and the person is told it was chosen blind (#25).
                            report.notes.append(
                                _(
                                    "“%(name)s” changed on both sides, and the card from the "
                                    "phone gives no time: the phone's version was taken."
                                )
                                % {"name": contact.name}
                            )
                    complete = apply_card(contact, incoming, owner, report)
                    if complete:
                        link.etag = resource.etag
                    # else the card is still seen as changed, and the note repeats until the
                    # two sides agree; a later edit here meets the conflict rule above (#26).
                    link.local_hash = digest(card_for(contact, owner, link.uid).digest_source())
                    link.last_synced_at = now
                    link.uid = link.uid or card.uid
                    link.save()
                    report.pulled += 1
                    return
            if local_changed:
                if read_only:
                    report.skipped += 1
                    return
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
                    return
                link.etag, link.local_hash, link.last_synced_at = etag, source, now
                link.uid = link.uid or card.uid
                link.save()
                report.pushed += 1

        def idle(contact: Contact) -> bool:
            """Nothing to do for this contact: its twin and its card are as last synced.

            Asked before :meth:`Run.each`, so an unchanged contact costs no savepoint (#47).
            Anything that cannot be decided here is left to ``one``, which says why.
            """
            link = links.get(contact.pk)
            if link is None:
                return False
            if link.remote_gone or not link.paired or contact.pk in held:
                return True
            try:
                href = canon(link.remote_href)
                if remote.get(href) != link.etag:
                    return False
                card = card_for(contact, owner, link.uid)
            except TOLERATED:
                return False
            return link.local_hash == digest(card.digest_source())

        for contact in contacts.values():
            if not idle(contact):
                run.each(contact.name, lambda contact=contact: one(contact))

        # Links whose contact is gone here: take the twin with it — unless the twin moved.
        # Whether each one still exists is asked once for all of them, not once each (#47).
        unseen = [link.object_id for link in links.values() if link.object_id not in contacts]
        existing = set(Contact.objects.filter(pk__in=unseen).values_list("pk", flat=True))
        for link in list(links.values()):
            if link.object_id in contacts:
                continue
            if link.object_id in existing:
                continue  # no longer visible to this person, but not deleted: not ours to drop
            if link.paired and not read_only and canon(link.remote_href) in remote:
                try:
                    if drop_twin(server, link, report, CARD_KEPT):
                        report.removed += 1
                except DavError as error:
                    run.skip(_tail(link.remote_href), error)
                    continue
            link.delete()

        # Cards that appeared on the other side: adopt them.
        for href in strangers():
            resource, card = cache[href]
            if not card.name:
                report.skipped += 1
                continue
            if len(href) > HREF_LENGTH or len(card.uid) > UID_LENGTH:
                # A longer address or identifier does not fit the link, and a cut one would
                # name another resource: the card is left alone, with a word (#34).
                run.skip(card.name, DavError(_("its address or identifier is too long")))
                continue

            def adopt(href=href, resource=resource, card=card) -> None:
                company = get_or_create_company(
                    owner, (card.org or str(HOLDING_COMPANY))[:NAME_LENGTH]
                )
                contact = Contact.objects.create(
                    owner=owner, company=company, name=card.name[:NAME_LENGTH]
                )
                apply_card(contact, card, owner, report)
                SyncLink.bind(
                    connection,
                    contact,
                    remote_href=href,
                    uid=card.uid,
                    etag=resource.etag,
                    local_hash=digest(card_for(contact, owner, card.uid).digest_source()),
                    last_synced_at=now,
                )
                report.pulled += 1

            run.each(card.name, adopt)

    # -------------------------------------------------------- interviews

    def _sync_interviews(self, run: Run, calendar) -> None:
        server, connection, report = run.server, run.connection, run.report
        owner, now, read_only, canon = run.owner, run.now, run.read_only, run.canon
        remote = server.etags(calendar.href)
        links = {link.object_id: link for link in SyncLink.of_model(connection, Interview)}
        held = run.held(links, calendar)
        interviews = {
            i.pk: i
            for i in Interview.objects.for_user(owner)
            .filter(starts_at__gte=now - HISTORY)
            .select_related("application__posting__company", "reminder")
            .prefetch_related("contacts")
        }
        cache: dict[str, tuple[dav.Resource, list[text.Event]]] = {}
        fetched: dict[str, dav.Resource] = {}

        def read(href: str):
            key = canon(href)
            if key not in cache:
                resource = fetched.pop(key, None) or server.get(href)
                cache[key] = (
                    resource,
                    [e for e in text.text_to_events(resource.text) if not e.recurring],
                )
            return cache[key]

        def linked() -> set[str]:
            return {canon(link.remote_href) for link in links.values() if link.remote_href}

        def strangers() -> list[str]:
            claimed = linked()
            return [h for h in remote if h in cache and h not in claimed]

        # The events no link claims are read only when something might be theirs: an
        # interview with no twin yet, or one whose twin has gone from its address.
        wanted = any(pk not in links for pk in interviews) or any(
            canon(link.remote_href) not in remote or link.remote_gone
            for pk, link in links.items()
            if pk in interviews and link.paired and pk not in held
        )
        # Every event this run will read, asked for in a few multiget REPORTs rather than a
        # GET each (#45): those no link claims when they are wanted, and the twins whose
        # ETag moved. In read-only mode the foreign events are still read on every run, as
        # nothing is ever pushed to settle `wanted`, but as a REPORT per fifty.
        unclaimed = [h for h in remote if h not in linked()] if wanted else []
        moved = [
            canon(link.remote_href)
            for pk, link in links.items()
            if pk in interviews
            and link.paired
            and link.remote_href
            and not link.remote_gone
            and remote.get(canon(link.remote_href), link.etag) != link.etag
        ]
        fetched.update(server.multiget(calendar.href, unclaimed + moved, CALENDAR))
        if wanted:
            for href in unclaimed:
                try:
                    read(href)
                except DavError as error:
                    run.skip(_tail(href), error)

        # Pair: a twin that moved is found again by its UID; an interview with no twin yet
        # whose event is already there is paired with it instead of being pushed again (#7).
        for pk, interview in interviews.items():
            link = links.get(pk)
            if link is not None and (not link.paired or pk in held):
                continue  # unpaired on purpose, or paired outside the chosen calendar
            if link is not None and canon(link.remote_href) in remote and not link.remote_gone:
                continue
            twin = (
                link.uid if link is not None and made_elsewhere(link, interview) else interview.uid
            )
            found = next((h for h in strangers() if any(e.uid == twin for e in cache[h][1])), None)
            if found is None or len(found) > HREF_LENGTH:
                continue
            resource, events = cache[found]
            event = next(e for e in events if e.uid == twin)
            same = event_source(event) == core_source(interview)
            try:
                links[pk] = SyncLink.bind(
                    connection,
                    interview,
                    remote_href=found,
                    uid=twin,
                    etag=resource.etag if same else "",
                    local_hash=(
                        digest(
                            core_source(interview)
                            if twin != interview.uid
                            else interview_source(interview)
                        )
                        if same
                        else ""
                    ),
                    last_synced_at=now if same else None,
                    remote_gone=False,
                )
            except AlreadyPaired:
                continue  # that event is another interview's by now: leave both alone
            if link is not None:
                report.notes.append(
                    _("An interview moved to another address on the calendar; still paired.")
                )

        def push(interview: Interview, href: str, *, known: dav.Resource | None, etag: str) -> str:
            """Write the event, keeping what was typed in its description on the phone."""
            free = ""
            if etag:
                current = known or server.get(href)
                events = [e for e in text.text_to_events(current.text) if not e.recurring]
                if events:
                    free = free_text(events[0].description, postulo_block(interview))
            # RFC 4791 §4.1 asks a calendar object resource to carry no METHOD; some servers
            # refuse one that does. The first try says PUBLISH, so that nothing takes the
            # event for an invitation; a refusal is tried again without it.
            try:
                return server.put(
                    href,
                    event_text(interview, free),
                    "text/calendar; charset=utf-8",
                    etag=etag,
                )
            except Conflict:
                raise
            except DavError as error:
                if error.status not in (400, 403, 415):
                    raise
            return server.put(
                href,
                event_text(interview, free, method=False),
                "text/calendar; charset=utf-8",
                etag=etag,
            )

        def patch(interview: Interview, link, known: dav.Resource | None) -> str | None:
            """Write what changed here into an event somebody else made; its new ETag.

            Only the times, the place and a cancellation are written, into the event as it is
            now, so that everything else in it survives. ``None`` when nothing may be written:
            an event with an ORGANIZER is a scheduling object and is never written to (#11).
            """
            resource = known or server.get(link.remote_href)
            events = [e for e in text.text_to_events(resource.text) if not e.recurring]
            event = next((e for e in events if e.uid == link.uid), None)
            if event is None:
                report.skipped += 1
                return None
            if event.organizer:
                report.notes.append(
                    _(
                        "The %(kind)s is paired with an event that has an organizer, so it is "
                        "read from the calendar and never written to it."
                    )
                    % {"kind": interview.get_kind_display()}
                )
                return None
            changes: dict = {}
            ends = interview.ends_at or interview.starts_at + dt.timedelta(hours=1)
            if (
                event.starts_at
                and event.ends_at
                and (interview.starts_at != event.starts_at or ends != event.ends_at)
            ):
                changes.update(starts_at=interview.starts_at, ends_at=ends)
            if interview.location != event.location:
                changes["location"] = interview.location
            if calendar_status(interview) == "CANCELLED" and event.status != "CANCELLED":
                changes["status"] = "CANCELLED"
            if not changes:
                return resource.etag
            patched = text.patch_event(resource.text, link.uid, modified=now, **changes)
            if patched is None:
                report.skipped += 1
                return None
            return server.put(
                link.remote_href, patched, "text/calendar; charset=utf-8", etag=resource.etag
            )

        def drop_own(interview: Interview, link) -> None:
            """Take Postulo's own event for an interview off the calendar, once the interview is
            paired with another, so that the meeting is not shown twice (#11).

            Its address is the one Postulo names its events by, and only an address no link
            claims is touched; the DELETE carries ``If-Match``.
            """
            own = canon(urljoin(calendar.href, f"{interview.uid.split('@')[0]}.ics"))
            if own == canon(link.remote_href) or own not in remote or own in linked():
                return
            try:
                server.delete(own, etag=remote[own])
            except Conflict:
                return  # edited since it was listed: left, and looked at on the next run
            del remote[own]
            report.removed += 1

        def one(interview: Interview) -> None:
            pk = interview.pk
            link = links.get(pk)
            if pk in held:
                return  # paired with something outside the chosen calendar
            if link is not None and (link.remote_gone or not link.paired):
                return  # removed from the calendar on purpose, or unpaired: leave it
            other = link is not None and made_elsewhere(link, interview)
            # What is compared, for an event somebody else made, is only what a calendar
            # may change: the notes and Postulo's own lines are not exchanged with it.
            source = digest(core_source(interview) if other else interview_source(interview))
            if other and not read_only:
                drop_own(interview, link)
            if link is None:
                if read_only:
                    report.skipped += 1
                    return
                # The resource is named for the UID's own part, which has no reserved
                # character in it, so that no server writes the address two ways (#19).
                href = urljoin(calendar.href, f"{interview.uid.split('@')[0]}.ics")
                etag = push(interview, href, known=None, etag="")
                links[pk] = SyncLink.bind(
                    connection,
                    interview,
                    remote_href=canon(href),
                    uid=interview.uid,
                    etag=etag,
                    local_hash=source,
                    last_synced_at=now,
                )
                report.pushed += 1
                return
            href = canon(link.remote_href)
            if href not in remote:
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
                return
            remote_changed = remote[href] != link.etag
            local_changed = link.local_hash != source
            known = None
            if remote_changed:
                resource, events = read(link.remote_href)
                twin = link.uid if other else interview.uid
                incoming = next((e for e in events if e.uid == twin), None) or (
                    events[0] if events else None
                )
                if incoming is None:
                    report.skipped += 1
                    return
                known = resource
                if local_changed and event_source(incoming) == core_source(interview):
                    # Both sides already say the same: nothing to merge, only to remember.
                    link.etag, link.local_hash, link.last_synced_at = resource.etag, source, now
                    link.save()
                    return
                if local_changed and (incoming.last_modified or now) < interview.updated_at:
                    record_event(
                        interview.application,
                        summary=str(_("Changed on the phone, but changed here later; kept ours.")),
                        body=event_source(incoming).replace("\n", " · "),
                        actor=ACTOR,
                    )
                    # Remembered at once: a push that is skipped or fails must not record
                    # the same conflict on every run (#21).
                    link.etag = resource.etag
                    link.save(update_fields=["etag", "updated_at"])
                else:
                    if local_changed:
                        record_event(
                            interview.application,
                            summary=str(
                                _("Changed here, but changed on the phone later; took theirs.")
                            ),
                            body=core_source(interview).replace("\n", " · "),
                            actor=ACTOR,
                        )
                    apply_event(
                        interview, incoming, report, postulo_block(interview), notes=not other
                    )
                    interview.refresh_from_db()
                    link.etag = resource.etag
                    link.local_hash = digest(
                        core_source(interview) if other else interview_source(interview)
                    )
                    link.last_synced_at = now
                    link.save()
                    report.pulled += 1
                    return
            if local_changed:
                if read_only:
                    report.skipped += 1
                    return
                try:
                    if other:
                        etag = patch(interview, link, known)
                        if etag is None:  # nothing may be written there: remembered, not repeated
                            link.local_hash = source
                            link.save(update_fields=["local_hash", "updated_at"])
                            return
                    else:
                        etag = push(interview, link.remote_href, known=known, etag=link.etag)
                except Conflict:
                    report.notes.append(
                        str(_("An interview changed on both sides at once; next run decides."))
                    )
                    return
                link.etag, link.local_hash, link.last_synced_at = etag, source, now
                link.save()
                report.pushed += 1

        for interview in interviews.values():
            run.each(interview.get_kind_display(), lambda interview=interview: one(interview))

        # The links of interviews outside the window grow with the person's whole history:
        # whether each one still exists is asked once for all of them, not once each (#47).
        unseen = [link.object_id for link in links.values() if link.object_id not in interviews]
        existing = set(Interview.objects.filter(pk__in=unseen).values_list("pk", flat=True))
        for link in list(links.values()):
            if link.object_id not in interviews:
                if link.object_id in existing:
                    continue  # merely too old for the window; leave the calendar alone
                if (
                    link.paired
                    and not read_only
                    and not made_elsewhere(link)
                    and canon(link.remote_href) in remote
                ):
                    try:
                        if drop_twin(server, link, report, EVENT_KEPT):
                            report.removed += 1
                    except DavError as error:
                        run.skip(_tail(link.remote_href), error)
                        continue
                link.delete()

        claimed = linked()
        foreign = [href for href in remote if href not in claimed]
        if foreign:
            report.skipped += len(foreign)
            report.notes.append(
                str(_("%(count)d events on the calendar are not from Postulo and were left alone."))
                % {"count": len(foreign)}
            )
