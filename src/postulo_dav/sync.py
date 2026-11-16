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
from dataclasses import dataclass, field, replace
from urllib.parse import urljoin, urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q
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
    AddressRow,
    AlreadyPaired,
    Application,
    Company,
    Contact,
    DestinationRefused,
    DiscoveredOption,
    FieldSpec,
    Interview,
    InterviewKind,
    InterviewOutcome,
    InterviewRefused,
    LinkKind,
    LinkRow,
    PhoneRow,
    Reminder,
    RemoteItem,
    Suggestion,
    SyncLink,
    SyncReport,
    TestResult,
    VCardAddress,
    WriteRefused,
    addresses_for,
    calendar_status,
    change_reminder,
    check_destination,
    complete_reminder,
    create_contact,
    create_reminder,
    event_lines,
    get_or_create_company,
    get_or_create_department,
    links_for,
    numbers_for,
    person_card,
    phone_number_is_taken,
    postpone_reminder,
    primary_phone_number,
    primary_web_link,
    record_event,
    reopen_reminder,
    reschedule_interview,
    save_phone_number,
    save_phone_numbers,
    save_postal_addresses,
    save_web_link,
    save_web_links,
    schedule_interview,
    settle_interview,
    suggest,
    update_company,
    update_contact,
)

from . import __version__, dav, text
from .dav import (
    ADDRESSBOOK,
    CALENDAR,
    OWN_TASK_LIST,
    READ,
    READ_WRITE,
    TASKS,
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
#: ...and for a company's card Postulo wrote (#10).
COMPANY_CARD_UID = re.compile(r"^company-\d+-\d+@postulo$")


#: A UID Postulo minted for an interview: ``<uuid>@postulo``. Any other UID on a paired event
#: is somebody else's, and the event is patched and never rebuilt (#11).
POSTULO_UID = re.compile(r"@postulo$")
#: How far ahead the events offered for pairing reach.
PAIRING_AHEAD = dt.timedelta(days=365)
#: How many tasks of the list are read to offer for pairing.
PAIRING_TASKS = 200


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


def card_made_elsewhere(link, contact, owner) -> bool:
    """Whether the card of ``link`` was made by somebody else: its UID is not Postulo's (#8).

    Such a card is patched, never rebuilt, and its ``NOTE`` is not exchanged: Postulo's notes
    are private and gather conflict records, so a pushed one would expose them and a pulled
    one would replace them.
    """
    uid = link.uid if link is not None else ""
    return bool(uid) and uid != f"contact-{contact.pk}-{owner.pk}@postulo"


def card_hash(card: text.Card, elsewhere: bool) -> str:
    """What a push of ``card`` is a function of: without the note for a card made elsewhere."""
    return digest((replace(card, note="") if elsewhere else card).digest_source())


def card_changes(card: text.Card, current: text.Card) -> dict[str, str]:
    """The properties of ``card`` that differ from the card on the server, as for `patch_card`."""
    wanted = {
        "FN": (card.name, current.name),
        "ORG": (card.org, current.org),
        "TITLE": (card.title, current.title),
        "EMAIL": (card.email, current.email),
        "TEL": (card.phone, current.phone),
        "URL": (card.url, current.url),
    }
    same = {"TEL": text.digits, "URL": text.web_key}
    changed = {
        name: new
        for name, (new, old) in wanted.items()
        if (same.get(name, str)(new) != same.get(name, str)(old))
    }
    # The unit: written when this side has one that differs, and cleared only when the
    # company changed (the old unit was the old company's). A card with a unit this side
    # does not know is left as it is (#8).
    if card.sync_unit and (card.org or current.org) and card.unit != current.unit:
        if card.unit or "ORG" in changed:
            changed["UNIT"] = card.unit
    return changed


def card_additions(card: text.Card, current: text.Card, changes: dict[str, str]) -> list[str]:
    """The lines of ``card`` the card on the server lacks: numbers, addresses, web addresses.

    Added beside what is there and never replacing anything (#8): what the phone keeps and
    this side does not know stays, and a number or address already on the card is not
    written twice. ``changes`` is what `card_changes` found, since a first number or web
    address that is replaced is not one the card still has.
    """
    have_numbers = {text.digits(n) for n in current.more_phones}
    have_numbers.add(text.digits(card.phone if "TEL" in changes else current.phone))
    have_urls = {text.web_key(u) for u in current.more_urls}
    have_urls.add(text.web_key(card.url if "URL" in changes else current.url))
    have_addresses = {text.address_key(a) for a in current.addresses}
    lines: list[str] = []
    for number in card.more_phones:
        if text.digits(number) not in have_numbers:
            have_numbers.add(text.digits(number))
            lines.append(f"TEL;TYPE=voice:{text.escape(number)}")
    for url in card.more_urls:
        if text.web_key(url) not in have_urls:
            have_urls.add(text.web_key(url))
            lines.append(f"URL:{text.without_controls(url)}")
    missing = []
    for address in card.addresses:
        if text.address_key(address) not in have_addresses:
            have_addresses.add(text.address_key(address))
            missing.append(address)
    return lines + text.address_lines(missing, current.version or "4.0")


def digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


#: A card whose twin was deleted here, but which somebody had edited in the meantime.
CARD_KEPT = gettext_lazy(
    "A contact deleted here had been changed on the phone since the last sync. The card was "
    "left in the address book, and comes back as a contact on the next run."
)
#: ...and for a task of the task list (#12).
TASK_KEPT = gettext_lazy(
    "A reminder deleted here had been changed on the phone since the last sync. The task was "
    "left in the task list."
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
    #: What goes out, per kind of record (#10): contacts by default, as ever; companies and the
    #: person's own card only when switched on.
    send_contacts: bool = True
    send_companies: bool = False
    send_own_card: bool = False
    #: The vCard version the address book takes, for the cards Postulo writes.
    vcard: str = "4.0"
    #: Records left unsent because their kind is switched off, for the report.
    unsent: int = 0
    #: Whether an application gets its apply and hear-back tasks (#12).
    milestones: bool = False
    #: Whether interviews go to a calendar this run: an interview's reminder is then its
    #: event's alarm already, and gets no task of its own (#12).
    events_on: bool = True

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


def _what(kind: str) -> str:
    """The collection of ``kind`` in words, for a sentence."""
    return {ADDRESSBOOK: _("address book"), TASKS: _("task list")}.get(kind, _("calendar"))


def _is_href(name: str) -> bool:
    """Whether what was typed for a collection is its address and not its name."""
    return name.startswith(("/", "https://", "http://"))


def _tail(href: str) -> str:
    return urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]


# ------------------------------------------------------------------ contacts


class _NoProbe(Exception):
    """Raised to roll the probe of `structure_on` back."""


def structure_on(owner) -> bool:
    """Whether departments are offered to ``owner`` (the core's *employer structure*).

    The core keeps the answer off the plugin surface, and says it in the one place a plugin
    can ask it: making a department is refused while the feature is off. So a company and a
    department are made inside a transaction that is always rolled back, and the refusal,
    or the lack of one, is the answer.
    """
    try:
        with transaction.atomic():
            company = get_or_create_company(owner, f"postulo-dav probe {uuid.uuid4().hex}")
            get_or_create_department(company, "probe", actor=owner, via=ACTOR)
            raise _NoProbe
    except _NoProbe:
        return True
    except WriteRefused:
        return False


#: What the card's address kinds are, for an address row of the core to be told on it.
_ADDRESS_KINDS = ("home", "work", "postal")


def card_for(contact: Contact, reader=None, uid: str = "", units: bool = False) -> text.Card:
    """The card for ``contact`` as ``reader`` sees it: their own note, their own numbers.

    The note is each person's own since postulo/postulo#896: read for nobody, it raises. A
    queryset that read the contact with ``with_notes`` has set it already; otherwise it is
    asked for. ``uid`` is the identifier the card already has on the server, and is never
    changed: a server may refuse a write that does (RFC 6352 §5.1, #7). ``units`` is whether
    the person is offered departments: only then is the contact's department the card's
    ``ORG`` unit (#8).
    """
    reader = reader or contact.owner
    # A card carries a first number and a first address on the web, which are the primaries;
    # the rest of the contact's numbers and links, and its postal addresses, follow them.
    number = primary_phone_number(contact)
    profile = primary_web_link(contact, LinkKind.SOCIAL)
    note = contact.notes if "notes" in contact.__dict__ else contact.note_for(reader)
    first = number.number if number else ""
    web = profile.url if profile else ""
    return text.Card(
        uid=uid or f"contact-{contact.pk}-{reader.pk}@postulo",
        name=contact.name,
        org=contact.company.name if contact.company_id else "",
        title=contact.role,
        email=contact.email,
        phone=first,
        url=web,
        note=note,
        unit=contact.department.name if units and contact.department_id else "",
        sync_unit=units,
        more_phones=tuple(
            row.number for row in extra_numbers(contact, reader) if row.number != first
        ),
        more_urls=tuple(
            row.url
            for row in extra_links(contact, reader)
            if text.web_key(row.url) != text.web_key(web)
        ),
        addresses=tuple(
            VCardAddress(
                street=row.street,
                postcode=row.postcode,
                municipality=row.municipality,
                region=row.region,
                country=row.country,
                kind=row.kind if row.kind in _ADDRESS_KINDS else "",
                primary=row.is_primary,
            )
            for row in contact.postal_addresses.all()
        ),
    )


def extra_numbers(contact: Contact, reader) -> tuple:
    """The contact's numbers as ``reader`` is shown them, asked for only when there is more
    than one: a contact with a single number costs no question here (#47)."""
    return numbers_for(contact, reader) if len(contact.phone_numbers.all()) > 1 else ()


def extra_links(contact: Contact, reader) -> tuple:
    """The same for the web addresses: all of them, when there is more than the one."""
    return links_for(contact, reader) if len(contact.web_links.all()) > 1 else ()


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


def apply_card(
    contact: Contact, card: text.Card, owner, report: SyncReport | None = None, units: bool = False
) -> bool:
    """Write a card onto the contact. ``False`` if some of it could not be taken.

    Only what the card says is written: a card without a ``TEL`` leaves the number here
    alone, and one without a ``URL`` the link, because this plugin never deletes what the
    core keeps (#26). A number another record holds is a note, never a failed run. It goes
    through the core's services with this plugin as the actor (#9): the contact, its
    department (the ``ORG`` unit, while the person is offered departments, ``units``), and
    its numbers, web addresses and postal addresses, which the card adds to and never
    shortens.
    """
    notes = report.notes if report is not None else []
    changes: dict = {
        "name": (card.name or contact.name)[:NAME_LENGTH],
        "role": card.title[:200],
        "email": card.email[:254],
    }
    company = contact.company if contact.company_id else None
    if card.org and (company is None or company.name.lower() != card.org.lower()):
        company = get_or_create_company(owner, card.org[:NAME_LENGTH])
        changes["company"] = company
    if units and card.unit and company is not None:
        try:
            changes["department"] = get_or_create_department(
                company, card.unit[:120], actor=owner, via=ACTOR
            )
        except WriteRefused:
            pass  # departments are switched off: nothing of one is written
    complete = True
    try:
        update_contact(contact, actor=owner, via=ACTOR, notes=card.note, **changes)
    except WriteRefused as refused:
        # The one thing a card may say that the core will not keep is its email: the rest
        # is taken, and the person is told.
        changes.pop("email")
        complete = False
        notes.append(
            _("“%(name)s”: the email on the card was not taken: %(why)s")
            % {"name": contact.name, "why": refused}
        )
        update_contact(contact, actor=owner, via=ACTOR, notes=card.note, **changes)
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
    add_rows(contact, card, owner, notes)
    return complete


class _Kept(Exception):
    """Raised to roll back a rows call that would have dropped a row the person has."""


def _added(holder, owner, existing, fresh, saver, notes, what: str) -> None:
    """Make ``holder``'s rows ``existing`` plus ``fresh`` with ``saver``, and never fewer.

    The core's rows calls make the rows they are handed the holder's rows, so a row that its
    rules would now refuse (a number kept from before they were made) would be dropped with
    the rest. That is never the card's to do: such a call is rolled back and told as a note.
    A new row that is refused is a note, and the rest is saved.
    """
    try:
        with transaction.atomic():
            saved = saver(holder, [*existing, *fresh], actor=owner, via=ACTOR)
            if any(refused.row in existing for refused in saved.refused):
                raise _Kept
    except _Kept:
        notes.append(
            _("“%(name)s”: %(what)s on the card were not taken, so that none kept here is lost.")
            % {"name": getattr(holder, "name", ""), "what": what}
        )
        return
    for refused in saved.refused:
        notes.append(
            _("“%(name)s”: a number, address or web address on the card was not taken: %(why)s")
            % {"name": getattr(holder, "name", ""), "why": refused.reason}
        )


def add_rows(holder, card: text.Card, owner, notes: list[str]) -> None:
    """Give ``holder`` the numbers, web addresses and postal addresses ``card`` has and it lacks.

    A row is added and no row is removed: what the person keeps here is not the card's to
    lose. A row the core's rules refuse (a number another record holds, an address against
    its country's rules) is a note and the rest is saved.
    """
    existing = tuple(numbers_for(holder, owner))
    have = {text.digits(r.number) for r in existing}
    fresh = [PhoneRow(n[:40].strip()) for n in card.more_phones if text.digits(n) not in have]
    if fresh:
        _added(holder, owner, existing, fresh, save_phone_numbers, notes, _("numbers"))
    existing = tuple(links_for(holder, owner))
    have = {text.web_key(r.url) for r in existing}
    fresh = [
        LinkRow(web_address(u)[:500], "website")
        for u in card.more_urls
        if text.web_key(u) not in have and web_address(u)
    ]
    if fresh:
        _added(holder, owner, existing, fresh, save_web_links, notes, _("web addresses"))
    existing = tuple(addresses_for(holder, owner))

    def key(a) -> str:
        return text.address_key(
            VCardAddress(a.street, a.postcode, a.municipality, a.region, a.country)
        )

    have = {key(a) for a in existing}
    fresh = [
        AddressRow(a.street, a.postcode, a.municipality, a.region, a.country, a.kind)
        for a in card.addresses
        if key(a) not in have
    ]
    if fresh:
        _added(holder, owner, existing, fresh, save_postal_addresses, notes, _("addresses"))


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


# --------------------------------------------------------------------- tasks


#: The two tasks an application may have, as slots of its link (#326): the one to send it, and
#: the one to hear back about it.
APPLY, REPLY = "apply", "reply"
#: Where an application stands once it has been sent and nothing has ended it: the statuses a
#: hear-back task is open in. An offer is not an ending, and leaves it open (#12).
SENT_OPEN = frozenset(
    {"applied", "acknowledged", "screening", "interviewing", "assessment", "offer"}
)
#: Postulo's priorities (low, normal, high) as RFC 5545 §3.8.1.9 numbers them: 1 is the
#: highest and 9 the lowest, so the order is inverted.
PRIORITIES = {1: 9, 2: 5, 3: 1}
#: The UID of an application's milestone task, which Postulo makes: ``application-<pk>-<owner>-
#: <slot>@postulo``. A task made on the phone *under* one is a reminder about that application.
MILESTONE_UID = re.compile(r"^application-(\d+)-(\d+)-(apply|reply)@postulo$")


def application_ref(owner_pk: int, application_pk: int) -> str:
    """What the tasks of one application share: ``REFID``, RFC 9253 (#12)."""
    return f"application-{application_pk}-{owner_pk}@postulo"


def milestone_uid(owner_pk: int, application_pk: int, slot: str) -> str:
    return f"application-{application_pk}-{owner_pk}-{slot}@postulo"


def milestone_todo(application: Application, slot: str, uid: str, owner_pk: int) -> text.Todo:
    """The task an application's milestone is, from the application as it stands (#12).

    *Apply* is due its deadline, done at ``applied_at``, cancelled if the application was
    withdrawn unsent. *Hear back* is done when the application is accepted or rejected and
    cancelled when it is withdrawn or ghosted; an offer, which is no ending, leaves it open.
    """
    posting = application.posting
    values = {"role": posting.title, "company": posting.company.name}
    status, completed, due_day = "NEEDS-ACTION", None, None
    if slot == APPLY:
        summary = _("Apply to %(role)s at %(company)s") % values
        due_day = application.deadline
        if application.applied_at is not None:
            status, completed = "COMPLETED", application.applied_at
        elif application.status == "withdrawn":
            status = "CANCELLED"
    else:
        summary = _("Hear back from %(company)s about %(role)s") % values
        if application.status in ("accepted", "rejected"):
            status = "COMPLETED"
            completed = application.closed_at or application.updated_at
        elif application.status in ("withdrawn", "ghosted"):
            status = "CANCELLED"
    return text.Todo(
        uid=uid,
        summary=summary[:250],
        due_day=due_day,
        status=status,
        completed=completed,
        priority=PRIORITIES.get(application.priority, 5),
        refid=application_ref(owner_pk, application.pk),
    )


def milestone_moment(application: Application, slot: str, now: dt.datetime) -> dt.datetime:
    """When a finished milestone was finished: what the ninety days are counted from."""
    if slot == APPLY:
        return application.applied_at or application.updated_at
    return application.closed_at or application.updated_at


def reminder_todo(reminder: Reminder, uid: str, refid: str = "", parent: str = "") -> text.Todo:
    """The task a reminder is: what to do, by when, and whether it is done (#12).

    A reminder about an application carries its ``REFID`` and sits under its open milestone
    task by ``RELATED-TO`` (RFC 5545 §3.8.4.5), which Nextcloud Tasks draws as a subtask.
    """
    done = reminder.done_at
    return text.Todo(
        uid=uid,
        summary=reminder.summary,
        due=reminder.due_at,
        status="COMPLETED" if done else "NEEDS-ACTION",
        completed=done,
        refid=refid,
        parent=parent,
    )


def task_hash(todo: text.Todo) -> str:
    """What a push of ``todo`` is a function of."""
    return digest(todo.digest_source())


def same_due(todo: text.Todo, wanted: dt.datetime | None) -> bool:
    """Whether the task is due when ``wanted`` says: a day-long ``DUE`` is that day, any hour."""
    if todo.due is not None:
        return wanted is not None and text.stamp(todo.due) == text.stamp(wanted)
    if todo.due_day is not None:
        return wanted is not None and timezone.localtime(wanted).date() == todo.due_day
    return wanted is None


def same_task(theirs: text.Todo, ours: text.Todo) -> bool:
    """Whether two tasks say the same of what Postulo keeps: summary, due time, done or not."""
    return (
        theirs.summary == ours.summary and same_due(theirs, ours.due) and theirs.done == ours.done
    )


def state_of(todo: text.Todo) -> str:
    """Open, completed or cancelled: the three things a task's status says to Postulo."""
    return "CANCELLED" if todo.status == "CANCELLED" else "COMPLETED" if todo.done else "OPEN"


def task_changes(ours: text.Todo, theirs: text.Todo, *, strict: bool = False) -> set[str]:
    """The properties to write into the task on the server so that it says what ``ours`` says.

    Only what differs, and only what Postulo owns (#12). A task the phone *cancelled* and a
    reminder done are the same fact, so a reminder's status is left as the phone wrote it;
    ``strict`` is for the tasks Postulo owns outright, which say which ending it was.
    """
    names: set[str] = set()
    if ours.summary != theirs.summary:
        names.add("SUMMARY")
    if ours.due is not None:
        if not same_due(theirs, ours.due):
            names.add("DUE")
    elif ours.due_day is not None:
        if theirs.due_day != ours.due_day:
            names.add("DUE")
    elif strict and (theirs.due is not None or theirs.due_day is not None):
        names.add("DUE")
    if (state_of(ours) != state_of(theirs)) if strict else (ours.done != theirs.done):
        names.add("STATUS")
    elif (
        ours.done
        and theirs.status != "CANCELLED"
        and (ours.completed and text.stamp(ours.completed))
        != (theirs.completed and text.stamp(theirs.completed))
    ):
        names.add("COMPLETED")
    if ours.priority is not None and ours.priority != theirs.priority:
        names.add("PRIORITY")
    if ours.refid and ours.refid != theirs.refid:
        names.add("REFID")
    if ours.parent != theirs.parent and (ours.parent or strict or theirs.parent):
        names.add("RELATED-TO")
    return names


def apply_todo(reminder: Reminder, todo: text.Todo, report: SyncReport, now: dt.datetime) -> None:
    """Take from the task what a task list may change in a reminder (#12).

    Through the core's services, so the reminder says who changed it: an edit changes what
    it says, a moved ``DUE`` postpones it, a tick completes it at the moment of the tick, an
    untick reopens it, and ``CANCELLED`` completes it.
    """
    if todo.summary and todo.summary != reminder.summary:
        change_reminder(reminder, summary=todo.summary, actor=ACTOR)
    if todo.done and not reminder.is_done:
        at = todo.completed or todo.last_modified or now
        complete_reminder(reminder, at=min(at, now), actor=ACTOR)
    elif not todo.done and reminder.is_done:
        reopen_reminder(reminder, actor=ACTOR)
    if not same_due(todo, reminder.due_at):
        if todo.due is not None:
            postpone_reminder(reminder, todo.due, actor=ACTOR)
        elif todo.due_day is not None:
            postpone_reminder(reminder, todo.due_day, actor=ACTOR)
        elif todo.problem:
            report.notes.append(
                _(
                    "The time of the task “%(name)s” could not be used (%(why)s), so the "
                    "reminder was not moved."
                )
                % {"name": reminder.summary[:120], "why": _problem(todo)}
            )


def _problem(event) -> str:
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
                "tasklist",
                str(_("Task list")),
                type="text",
                required=False,
                default="",
                help=str(
                    _(
                        "Used only while no collection is chosen below. A name or an address "
                        "of a task list to keep your reminders in; blank, and none is used. A "
                        "name that is not there is made by the first sync, unless read only is "
                        "on. Needs a server that keeps tasks."
                    )
                ),
            ),
            FieldSpec(
                "milestone_tasks",
                str(_("Tasks about applications")),
                type="boolean",
                required=False,
                default=False,
                help=str(
                    _(
                        "Also keep two tasks for each application in the task list: one to "
                        "apply, which ticking sends to you as a suggestion, and one to hear "
                        "back, which closes when the application ends. Reminders about an "
                        "application sit under its open task."
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
                        "Press Look on the server, then choose one address book, one calendar "
                        "and one task list. Only those are read or written, and a mode set "
                        "here is Postulo's word, not the server's lock: an app password cannot "
                        "be narrowed to one collection. Whole collections sync."
                    )
                ),
            ),
            FieldSpec(
                "send_contacts",
                str(_("Send contacts")),
                type="choice",
                required=False,
                default="all",
                choices=(("all", str(_("All of them"))), ("none", str(_("None")))),
                help=str(
                    _(
                        "Which contacts get a card in the address book. Cards already there "
                        "stay, and nothing more is sent to them while this is off."
                    )
                ),
            ),
            FieldSpec(
                "send_companies",
                str(_("Send companies")),
                type="boolean",
                required=False,
                default=False,
                help=str(
                    _(
                        "Write each company as a card of its own (a company card, with its "
                        "name and website), kept in step with the company."
                    )
                ),
            ),
            FieldSpec(
                "send_own_card",
                str(_("Send my own card")),
                type="boolean",
                required=False,
                default=False,
                help=str(
                    _(
                        "Write your own details to a card of its own, one way: a change on the "
                        "phone never reaches your profile."
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
                        "Never write to the server — not even to make the address book, the "
                        "calendar or the task list. Contacts, interviews and reminders still "
                        "come in; nothing goes out."
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
                kinds = [ADDRESSBOOK, CALENDAR] + ([TASKS] if self._name(config, TASKS) else [])
                found = {
                    kind: self._collection(server, config, kind, create=False) for kind in kinds
                }
        except DavError as error:
            # Already a sentence for the person, in their language: no class name in front.
            return TestResult(False, str(error))
        except Exception as error:  # noqa: BLE001 - the sentence is the point
            return TestResult(False, f"{type(error).__name__}: {error}")
        missing = [kind for kind, collection in found.items() if collection is None]
        if not missing:
            message = str(
                _(
                    "Found the address book “%(book)s” (%(book_href)s) and the calendar "
                    "“%(calendar)s” (%(calendar_href)s)."
                )
            ) % {
                "book": found[ADDRESSBOOK].name,
                "book_href": urlsplit(found[ADDRESSBOOK].href).path,
                "calendar": found[CALENDAR].name,
                "calendar_href": urlsplit(found[CALENDAR].href).path,
            }
            if TASKS in found:
                message += " " + str(
                    _("Found the task list “%(name)s” (%(href)s).")
                    % {
                        "name": found[TASKS].name or _tail(found[TASKS].href),
                        "href": urlsplit(found[TASKS].href).path,
                    }
                )
            return TestResult(True, message)
        sentences = [self._missing_sentence(config, kind) for kind in missing]
        # Signing in worked, which is what *Test* is for; what is missing is only a
        # problem the sync cannot solve when it is forbidden to write.
        if any(not _is_href(self._name(config, kind)) for kind in missing):
            sentences.append(str(READ_ONLY_TAIL if read_only else FIRST_SYNC_TAIL))
        return TestResult(not read_only, " ".join(sentences))

    def _tested_choice(self, chosen: dict, notes: list[str]) -> TestResult:
        sentences = []
        for kind, (collection, mode) in chosen.items():
            what = _what(kind)
            how = _("read only") if mode == READ else _("read and write")
            sentences.append(
                str(_("Found the %(what)s “%(name)s” (%(how)s)."))
                % {"what": what, "name": collection.name or _tail(collection.href), "how": how}
            )
        if not chosen:
            sentences.append(str(_("None of the chosen collections is on the server.")))
        return TestResult(bool(chosen), " ".join([*sentences, *notes]))

    def _name(self, config: dict, kind: str) -> str:
        """What names the collection of ``kind``; a task list is named by nothing, by default."""
        if kind == TASKS:
            return str(config.get("tasklist") or "").strip()
        return str(config.get("addressbook" if kind == ADDRESSBOOK else "calendar") or "Postulo")

    def _missing_sentence(self, config: dict, kind: str) -> str:
        name = self._name(config, kind)
        if kind == ADDRESSBOOK:
            return str(_("The address book “%(name)s” is not on the server.")) % {"name": name}
        if kind == TASKS:
            return str(_("The task list “%(name)s” is not on the server.")) % {"name": name}
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
            if found is not None and not (
                found.kind == CALENDAR and found.accepts_tasks
                if kind == TASKS
                else found.is_a(kind)
            ):
                raise DavError(
                    _("“%(name)s” is not an address book.") % {"name": name}
                    if kind == ADDRESSBOOK
                    else _("“%(name)s” is not a task list.") % {"name": name}
                    if kind == TASKS
                    else _("“%(name)s” is not a calendar.") % {"name": name}
                )
        else:
            well_known = "carddav" if kind == ADDRESSBOOK else "caldav"
            principal = server.principal(well_known)
            home = server.home(principal, kind)
            found = server.find_collection(home, name, kind)
            if found is None and create:
                try:
                    found = server.create_collection(home, name, kind)
                except AuthenticationRefused as refused:
                    # A 403 to making a task list is the server saying it keeps no tasks, and
                    # not that the password is wrong: the rest of the run is not refused (#12).
                    if kind == TASKS and refused.status == 403:
                        raise DavError(
                            _("The server would not make a task list, so it may not keep tasks."),
                            status=403,
                        ) from refused
                    raise
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
        found: dict[str, list[tuple[dav.Collection, str]]] = {
            ADDRESSBOOK: [],
            CALENDAR: [],
            TASKS: [],
        }
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
            kind = TASKS if collection.is_task_list else collection.kind
            # A task list is held to like any other collection: one guard, one more (#12).
            if kind != TASKS and collection.kind == CALENDAR and not collection.accepts_events:
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
            found[kind].append((collection, mode))
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
                    % {"what": _what(kind)}
                )
            elif entries:
                collection, mode = entries[0]
                server.allow(collection.href, mode)
                chosen[kind] = (collection, mode)
        return chosen

    def discover(self, config: dict) -> dict[str, list[DiscoveredOption]]:
        """What the server has to choose from: each address book, calendar and task list."""
        options: dict[str, DiscoveredOption] = {}
        groups = {
            ADDRESSBOOK: str(_("Address books")),
            CALENDAR: str(_("Calendars")),
            TASKS: str(_("Task lists")),
        }
        client, server = dav.connect(config)
        with client:
            for home_kind in (ADDRESSBOOK, CALENDAR):
                principal = server.principal("carddav" if home_kind == ADDRESSBOOK else "caldav")
                for home in server.homes(principal, home_kind):
                    for collection in server.collections(home):
                        # A calendar that takes events is a calendar, one that takes tasks and
                        # not events is a task list (#12), and neither is the other.
                        if collection.kind != home_kind:
                            continue
                        if collection.is_task_list:
                            kind = TASKS
                        elif home_kind == CALENDAR and not collection.accepts_events:
                            continue
                        else:
                            kind = home_kind
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
                                group=groups[kind],
                                modes=(READ,) if writable is False else (),
                                note=note,
                            ),
                        )
        return {"collections": list(options.values())}

    # ------------------------------------------------------------ pairing

    def _make_interviews(self, run: Run, calendar, remote: dict[str, str]) -> None:
        """Make the interview for each event a person paired with an application (#11).

        An event is paired with an application on the *Pairing* page; the interview is
        made here, through `schedule_interview` so that it has its timeline entry and its
        reminder, and is then paired with the same event like any other: kept in step by
        patching, never rebuilt. The interview's own link carries no collection, since the
        application's link holds the event's key. The kind is *Other* until the person
        changes it on the interview: nothing on the pairing page asks for one.
        """
        server, connection, report, canon = run.server, run.connection, run.report, run.canon
        # Unpaired ones count: a person who unpaired an interview did not ask for a new one.
        have = {link.uid for link in SyncLink.of_model(connection, Interview) if link.uid}
        for link in SyncLink.of_model(connection, Application):
            if link.slot or not link.paired or link.remote_gone or link.uid in have:
                continue
            href = canon(link.remote_href) if link.remote_href else ""
            if not href or not server.within(calendar.href, href):
                continue  # not on this calendar: not this run's to read
            if href not in remote:
                continue  # the event is gone: the pairing waits, and nothing is made
            application = link.target
            if application is None:
                continue

            def make(link=link, href=href, application=application) -> None:
                resource = server.get(href)
                events = [e for e in text.text_to_events(resource.text) if not e.recurring]
                event = next((e for e in events if e.uid == link.uid), None)
                if event is None or not event.starts_at or not event.ends_at:
                    report.skipped += 1
                    report.notes.append(
                        _(
                            "“%(name)s” could not be made into an interview: its time "
                            "cannot be read."
                        )
                        % {"name": (event.summary if event else href)[:120]}
                    )
                    return
                try:
                    interview = schedule_interview(
                        application,
                        kind=InterviewKind.OTHER,
                        starts_at=event.starts_at,
                        ends_at=event.ends_at if event.ends_at > event.starts_at else None,
                        location=event.location[:500],
                        actor=ACTOR,
                    )
                except InterviewRefused as refused:
                    report.skipped += 1
                    report.notes.append(
                        _("“%(name)s” could not be made into an interview: %(why)s")
                        % {"name": event.summary[:120], "why": refused}
                    )
                    return
                SyncLink.bind(
                    connection,
                    interview,
                    remote_href=href,
                    uid=link.uid,
                    etag=resource.etag,
                    local_hash=digest(core_source(interview)),
                    last_synced_at=run.now,
                )
                report.pulled += 1

            run.each(link.uid, make)

    def fill_record(self, connection, config: dict, item: RemoteItem, kind: str, into=None):
        """Make a company or a contact from one card, or fill one of the person's own (#9).

        One card, started by the person on the Pairing page, read from the chosen address
        book now; nothing is written to the server. Written through the core's services with
        the plugin as the actor: a new record is made from the card, and an existing one is
        only given what it lacks (its blanks), never overwritten. Postulo pairs the two
        afterwards unless the person asked for a one-off import.
        """
        if kind == "reminder":
            return self._reminder_from(connection, config, item, into)
        client, server = dav.connect(config)
        with client:
            chosen = self._resolve(server, config, [])
            if chosen is not None:
                picked = chosen.get(ADDRESSBOOK)
                book = picked[0] if picked else None
            else:
                book = self._collection(server, config, ADDRESSBOOK, create=False)
            if book is None or not server.within(book.href, item.href):
                raise DavError(_("That card is not in the chosen address book."))
            card = text.text_to_card(server.get(item.href).text)
        if card.uid != item.uid:
            raise DavError(_("That card has changed since it was listed."))
        owner = connection.owner
        if kind == "company":
            return self._company_from(owner, card, into)
        if kind == "contact":
            return self._contact_from(owner, card, into)
        raise DavError(_("A card cannot be made into that."))

    def _task_list(self, server: Server, config: dict) -> dav.Collection | None:
        """The task list chosen, or named, and allowed for this run; ``None`` if there is none."""
        chosen = self._resolve(server, config, [])
        if chosen is not None:
            picked = chosen.get(TASKS)
            return picked[0] if picked else None
        if not self._name(config, TASKS):
            return None
        return self._collection(server, config, TASKS, create=False)

    def _reminder_from(self, connection, config: dict, item: RemoteItem, into):
        """Make a reminder from one task of the chosen task list, or give ``into`` back (#12).

        Nothing is written to the server. A reminder has a summary and a due time already, so
        there is nothing to fill in one that exists: it is returned as it is, and paired.
        """
        client, server = dav.connect(config)
        with client:
            tasks = self._task_list(server, config)
            if tasks is None or not server.within(tasks.href, item.href):
                raise DavError(_("That task is not in the chosen task list."))
            found = [t for t in text.text_to_todos(server.get(item.href).text) if t.uid == item.uid]
        if not found:
            raise DavError(_("That task has changed since it was listed."))
        if into is not None:
            return into
        todo = found[0]
        if todo.due is None and todo.due_day is None:
            raise DavError(_("The task has no due time to make a reminder of."))
        return create_reminder(
            connection.owner, summary=todo.summary, due=todo.due or todo.due_day, actor=ACTOR
        )

    def _company_from(self, owner, card: text.Card, into):
        """A company from a card: its name, website and, as notes, the numbers it carries.

        The name is the card's ``FN`` for a company's card, else the first ``ORG`` component.
        Matching an existing company is the person's call: what is filled is ``into``, and a
        new company is made only when none was given, by the name the core would match
        (case aside). A card's numbers and emails, which a company has no place for yet
        (postulo/postulo#329), go into its notes, under a line naming the card.
        """
        name = (card.name if card.kind == "org" else card.org or card.name)[:NAME_LENGTH]
        if not name.strip():
            raise DavError(_("The card has no name to make a company of."))
        company = into or get_or_create_company(owner, name, strict=True)
        changes: dict = {}
        site = web_address(card.url)
        if site and not company.website:
            changes["website"] = site
        extras = [p for p in (card.phone, card.email) if p]
        if extras:
            note = company.note_for(owner)
            line = _("From the card “%(name)s”: %(details)s") % {
                "name": name,
                "details": ", ".join(extras),
            }
            if line not in note:
                changes["notes"] = f"{note}\n\n{line}".strip()
        if changes:
            update_company(company, actor=owner, via=ACTOR, **changes)
            company.refresh_from_db()
        return company

    def _contact_from(self, owner, card: text.Card, into):
        """A contact from a card: what adoption takes, and the ``ORG`` unit as the department.

        A card with no ``ORG`` gives a contact with no company. An existing contact is only
        given what it lacks.
        """
        if not card.name.strip():
            raise DavError(_("The card has no name to make a contact of."))
        company = into.company if into is not None else None
        if card.org and company is None:
            company = get_or_create_company(owner, card.org[:NAME_LENGTH], strict=True)
        department = None
        if card.unit and company is not None:
            try:
                department = get_or_create_department(
                    company, card.unit[:NAME_LENGTH], actor=owner, via=ACTOR
                )
            except WriteRefused:
                department = None  # employer structure is off: no department is written
        if into is None:
            contact = create_contact(
                company,
                actor=owner,
                via=ACTOR,
                name=card.name[:NAME_LENGTH],
                role=card.title[:200],
                email=card.email[:254],
                department=department,
            )
        else:
            contact = into
            changes = {}
            if not contact.role and card.title:
                changes["role"] = card.title[:200]
            if not contact.email and card.email:
                changes["email"] = card.email[:254]
            if contact.company_id is None and company is not None:
                changes["company"] = company
            if contact.department_id is None and department is not None:
                changes["department"] = department
            if changes:
                update_contact(contact, actor=owner, via=ACTOR, **changes)
        number = card.phone[:40].strip()
        if number and primary_phone_number(contact) is None:
            if not phone_number_is_taken(number, exclude_pk=None, asked_by=owner):
                save_phone_number(contact, owner, number)
        url = web_address(card.url)[:500]
        if url and primary_web_link(contact, LinkKind.SOCIAL) is None:
            try:
                save_web_link(contact, owner, LinkKind.SOCIAL, url)
            except (ValidationError, ValueError):
                pass  # an address the core will not keep is left out, as adoption does
        contact.refresh_from_db()
        return contact

    def remote_items(
        self, config: dict, search: str = "", kind: str = "", key: tuple[str, str] | None = None
    ) -> list[RemoteItem]:
        """What a person may pair by hand: the chosen address book's cards and calendar's events."""
        items: list[RemoteItem] = []
        if kind in ("", "interview", "application"):
            items += self._event_items(config, search, kind, key)
        if kind in ("", "contact", "company", "you"):
            items += self._card_items(config, search, kind, key)
        if kind in ("", "reminder"):
            items += self._task_items(config, search, key)
        return items

    def _task_items(
        self, config: dict, search: str, key: tuple[str, str] | None
    ) -> list[RemoteItem]:
        """Tasks of the chosen task list a person may pair with a reminder (#12).

        Asked live, and nothing is written. A task Postulo made is paired already and is not
        offered, nor is a repeating one: a reminder is one nudge, not a series.
        """
        client, server = dav.connect(config)
        with client:
            tasks = self._task_list(server, config)
            if tasks is None:
                return []
            where = server.canonical(tasks.href)
            if key is not None and server.canonical(key[0]) != where:
                return []
            listed = server.etags(tasks.href)
            found = server.multiget(tasks.href, list(listed)[:PAIRING_TASKS], CALENDAR)
            words = search.strip().casefold()
            items: list[RemoteItem] = []
            for href, resource in found.items():
                for todo in text.text_to_todos(resource.text):
                    if todo.recurring or not todo.uid or POSTULO_UID.search(todo.uid):
                        continue
                    if key is not None and todo.uid != key[1]:
                        continue
                    if words and words not in todo.summary.casefold():
                        continue
                    due = todo.due.astimezone(timezone.get_current_timezone()) if todo.due else None
                    detail = (
                        due.strftime("%Y-%m-%d %H:%M")
                        if due
                        else todo.due_day.isoformat()
                        if todo.due_day
                        else ""
                    )
                    items.append(
                        RemoteItem(
                            collection=where,
                            uid=todo.uid,
                            label=todo.summary or str(_("(no title)")),
                            collection_label=tasks.name or _tail(tasks.href),
                            href=href,
                            detail=detail,
                            kinds=("reminder",),
                        )
                    )
            return items

    def _card_items(
        self, config: dict, search: str, kind: str, key: tuple[str, str] | None
    ) -> list[RemoteItem]:
        """Cards of the chosen address book a person may pair with a contact or a company (#8).

        One ``addressbook-query`` on the card's name, limited, never a whole book. A card
        Postulo made is paired already and is not offered; a company's card
        (``KIND:org``) is offered as a company, any other as a contact.
        """
        client, server = dav.connect(config)
        with client:
            chosen = self._resolve(server, config, [])
            if chosen is not None:
                picked = chosen.get(ADDRESSBOOK)
                book = picked[0] if picked else None
            else:
                book = self._collection(server, config, ADDRESSBOOK, create=False)
            if book is None:
                return []
            where = server.canonical(book.href)
            if key is not None and server.canonical(key[0]) != where:
                return []
            # The card the server names as the person's own is the one offered as *you*;
            # where it names none, any card may be (RFC 6352 §7.1.2, #8).
            try:
                own = server.principal_address(server.principal("carddav"))
            except DavError:
                own = ""
            own = server.canonical(own) if own else ""
            items = []
            for href, resource in server.query_cards(book.href, search).items():
                card = text.text_to_card(resource.text)
                if not card.uid or not (card.name or card.org) or POSTULO_CARD_UID.match(card.uid):
                    continue
                if key is not None and card.uid != key[1]:
                    continue
                kinds = ("company",) if card.kind == "org" else ("contact", "company")
                if card.kind != "org" and (not own or server.canonical(href) == own):
                    kinds += ("you",)
                if kind and kind not in kinds:
                    continue
                items.append(
                    RemoteItem(
                        collection=where,
                        uid=card.uid,
                        label=card.name or card.org,
                        collection_label=book.name or _tail(book.href),
                        href=href,
                        detail=" · ".join(p for p in (card.org, card.title) if p),
                        kinds=kinds,
                    )
                )
            return items

    def _event_items(
        self, config: dict, search: str, kind: str, key: tuple[str, str] | None
    ) -> list[RemoteItem]:
        """Events a person may pair with an interview, from the chosen calendar (#11).

        Asked live, and answered from one ``calendar-query`` with a time range (RFC 4791
        §7.8): the interviews' own window, and a year ahead. Events Postulo made, repeating
        events and events without a UID are not offered: the first are paired already, and an
        interview is one meeting. Nothing is written.
        """
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
                                kinds=("interview", "application"),
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
            run = Run(
                server,
                connection,
                report,
                read_only,
                send_contacts=config.get("send_contacts") != "none",
                send_companies=bool(config.get("send_companies")),
                send_own_card=bool(config.get("send_own_card")),
                milestones=bool(config.get("milestone_tasks")),
            )
            try:
                chosen = self._resolve(server, config, report.notes)
                run.events_on = chosen is None or CALENDAR in chosen
            except AuthenticationRefused as error:
                report.error = str(error)
                return report
            except httpx.HTTPError as error:
                report.error = f"{type(error).__name__}: {error}"
                return report
            for kind, half in (
                (ADDRESSBOOK, self._sync_contacts),
                (CALENDAR, self._sync_interviews),
                (TASKS, self._sync_tasks),
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
                        if kind == TASKS and not self._name(config, TASKS):
                            continue  # no task list is named: none is kept (#12)
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
                    if kind == TASKS:
                        # A server that keeps no tasks is a note, not a failed run: the
                        # contacts and the interviews have synced, and say so (#12).
                        report.notes.append(
                            _("Reminders were not synced: the task list could not be used. %(why)s")
                            % {"why": error}
                        )
                    else:
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
        run.vcard = book.vcard
        remote = server.etags(book.href)
        # Links flagged remote_gone are kept in the map: they say "the person removed
        # this from the address book", and the record must not be pushed back.
        links = {link.object_id: link for link in SyncLink.of_model(connection, Contact)}
        held = run.held(links, book)
        # A department is the card's ``ORG`` unit only while the person is offered them (#8).
        units = structure_on(owner)

        def cf(contact: Contact, uid: str = "") -> text.Card:
            return card_for(contact, owner, uid, units=units)

        # The numbers and the links are prefetched for the card's primaries: two queries a
        # run, where a core that reads the primary from prefetched rows (postulo/postulo#1171)
        # asked two per contact (#47). An older core still asks per contact, as before.
        contacts = {
            c.pk: c
            for c in Contact.objects.visible_to(owner)
            .with_notes(owner)
            .select_related("company", "department")
            .prefetch_related("phone_numbers", "web_links", "postal_addresses")
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

        # A card paired with a company is that company's, and is never adopted as a contact.
        companies = {
            canon(link.remote_href)
            for model in (Company, type(owner))
            for link in SyncLink.of_model(connection, model)
            if link.remote_href
        }

        def linked() -> set[str]:
            return {
                canon(link.remote_href) for link in links.values() if link.remote_href
            } | companies

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
            same = digest(card.digest_source()) == digest(cf(contact, link.uid).digest_source())
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
            mine = digest(cf(contact, uid).digest_source())
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

        def patch(contact: Contact, link, card: text.Card) -> str:
            """Write what changed here into a card somebody else made; its new ETag (#8).

            Only the properties Postulo owns are replaced, in the card as it is now, so
            that its UID, version, kind, photo, address and the rest survive.
            """
            resource = server.get(link.remote_href)
            current = text.text_to_card(resource.text)
            changes = card_changes(card, current)
            additions = card_additions(card, current, changes)
            if not changes and not additions:
                return resource.etag
            patched = text.patch_card(resource.text, link.uid, changes, rev=now, add=additions)
            if patched is None:
                raise DavError(_("The card could not be read, so it was not changed."))
            return server.put(
                link.remote_href,
                patched,
                "text/vcard; charset=utf-8",
                etag=link.etag or resource.etag,
            )

        # Local records: push new and changed ones; notice twins that vanished.
        def one(contact: Contact) -> None:
            link = links.get(contact.pk)
            if contact.pk in held:
                return  # paired with something outside the chosen address book
            if link is not None and (link.remote_gone or not link.paired):
                return  # removed from the address book on purpose, or unpaired: leave it
            card = cf(contact, link.uid if link else "")
            other = card_made_elsewhere(link, contact, owner)
            source = card_hash(card, other)
            if link is None:
                if read_only or not run.send_contacts:
                    report.skipped += 1
                    run.unsent += 0 if read_only else 1
                    return
                href = urljoin(book.href, f"{uuid.uuid4()}.vcf")
                etag = server.put(
                    href,
                    text.card_to_text(card, prodid=PRODID, rev=now, version=run.vcard),
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
                if local_changed and card_hash(incoming, other) == source:
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
                    card = cf(contact, link.uid)
                    source = card_hash(card, other)
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
                    if other:
                        incoming.note = card.note  # a card made elsewhere: no notes exchanged
                    complete = apply_card(contact, incoming, owner, report, units)
                    if complete:
                        link.etag = resource.etag
                    # else the card is still seen as changed, and the note repeats until the
                    # two sides agree; a later edit here meets the conflict rule above (#26).
                    link.local_hash = card_hash(cf(contact, link.uid), other)
                    link.last_synced_at = now
                    link.uid = link.uid or card.uid
                    link.save()
                    report.pulled += 1
                    return
            if local_changed:
                if read_only or not run.send_contacts:
                    report.skipped += 1
                    run.unsent += 0 if read_only else 1
                    return
                try:
                    if other:
                        etag = patch(contact, link, card)
                    else:
                        etag = server.put(
                            link.remote_href,
                            text.card_to_text(card, prodid=PRODID, rev=now, version=run.vcard),
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
                card = cf(contact, link.uid)
            except TOLERATED:
                return False
            return link.local_hash == card_hash(card, card_made_elsewhere(link, contact, owner))

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
            if (
                link.paired
                and not read_only
                and POSTULO_CARD_UID.match(
                    link.uid or ""
                )  # a card somebody else made is unpaired, never deleted (#8)
                and canon(link.remote_href) in remote
            ):
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
                contact = create_contact(
                    company, actor=owner, via=ACTOR, name=card.name[:NAME_LENGTH]
                )
                apply_card(contact, card, owner, report, units)
                SyncLink.bind(
                    connection,
                    contact,
                    remote_href=href,
                    uid=card.uid,
                    etag=resource.etag,
                    local_hash=card_hash(
                        cf(contact, card.uid), not POSTULO_CARD_UID.match(card.uid or "")
                    ),
                    last_synced_at=now,
                )
                report.pulled += 1

            run.each(card.name, adopt)

        self._sync_company_cards(run, book, remote)
        self._send_own_card(run, book, remote)
        if run.unsent:
            report.notes.append(
                _("%(count)d records were not sent, because sending that kind is switched off.")
                % {"count": run.unsent}
            )

    def _sync_company_cards(self, run: Run, book, remote: dict[str, str]) -> None:
        """Keep each company a person paired with a card in step: name and website (#8).

        Both ways, as for a contact; the card is patched and never rebuilt, and its ``NOTE``
        is not exchanged. The company's other details wait for what the core gives a company
        (postulo/postulo#329). Nothing is made here: a card becomes a company's twin only by
        being paired, and a pairing leaves unpaired links alone.
        """
        server, connection, report, canon = run.server, run.connection, run.report, run.canon
        now, read_only = run.now, run.read_only
        links = {link.object_id: link for link in SyncLink.of_model(connection, Company)}
        held = run.held(links, book)
        own_companies = Company.objects.for_user(run.owner).filter(owner=run.owner)

        def make(company) -> None:
            uid = f"company-{company.pk}-{run.owner.pk}@postulo"
            href = urljoin(book.href, f"{uuid.uuid4()}.vcf")
            card = text.Card(uid=uid, name=company.name, org=company.name, url=company.website)
            etag = server.put(
                href,
                text.card_to_text(card, prodid=PRODID, rev=now, version=run.vcard, org=True),
                "text/vcard; charset=utf-8",
            )
            links[company.pk] = SyncLink.bind(
                connection,
                company,
                remote_href=canon(href),
                uid=uid,
                etag=etag,
                local_hash=digest(f"{company.name}\n{company.website}"),
                last_synced_at=now,
            )
            remote[canon(href)] = etag
            report.pushed += 1

        # Companies go out as cards of their own when that is switched on; when it is off,
        # the cards and their links stay and nothing more is pushed (#10).
        for company in own_companies:
            if company.pk in links:
                continue
            if read_only or not run.send_companies:
                continue
            run.each(company.name, lambda company=company: make(company))

        def one(link) -> None:
            company = link.target
            if company is None:
                return
            href = canon(link.remote_href)
            if href not in remote:
                link.remote_gone = True
                link.save(update_fields=["remote_gone", "updated_at"])
                report.removed += 1
                report.notes.append(
                    _("“%(name)s” was removed from the address book; kept here.")
                    % {"name": company.name}
                )
                return
            card = text.Card(name=company.name, org=company.name, url=company.website)
            source = digest(f"{company.name}\n{company.website}")
            local_changed = link.local_hash != source
            resource = None
            if remote[href] != link.etag:
                resource = server.get(link.remote_href)
                incoming = text.text_to_card(resource.text)
                name = incoming.name or incoming.org
                same = (name, web_address(incoming.url)) == (company.name, company.website)
                ours_later = (
                    local_changed
                    and incoming.rev is not None
                    and getattr(company, "updated_at", None) is not None
                    and incoming.rev < company.updated_at
                )
                if same or not ours_later:
                    changes = {}
                    if name:
                        changes["name"] = name
                    if incoming.url.strip():
                        changes["website"] = web_address(incoming.url)
                    if changes and not same:
                        try:
                            update_company(company, actor=run.owner, via=ACTOR, **changes)
                        except WriteRefused as refused:
                            report.skipped += 1
                            report.notes.append(
                                _("“%(name)s” was not changed from the card: %(why)s")
                                % {"name": company.name, "why": refused}
                            )
                            link.etag = resource.etag
                            link.save(update_fields=["etag", "updated_at"])
                            return
                        company.refresh_from_db()
                    link.etag = resource.etag
                    link.local_hash = digest(f"{company.name}\n{company.website}")
                    link.last_synced_at = now
                    link.save()
                    if not same:
                        report.pulled += 1
                    return
                link.etag = resource.etag
            if local_changed:
                ours = bool(COMPANY_CARD_UID.match(link.uid or ""))
                if read_only or (ours and not run.send_companies):
                    report.skipped += 1
                    run.unsent += 0 if read_only else 1
                    return
                resource = resource or server.get(link.remote_href)
                changes = card_changes(card, text.text_to_card(resource.text))
                etag = resource.etag
                if changes:
                    patched = text.patch_card(resource.text, link.uid, changes, rev=now)
                    if patched is None:
                        raise DavError(_("The card could not be read, so it was not changed."))
                    try:
                        etag = server.put(
                            link.remote_href,
                            patched,
                            "text/vcard; charset=utf-8",
                            etag=resource.etag,
                        )
                    except Conflict:
                        report.notes.append(
                            _("“%(name)s” changed on both sides at once; next run decides.")
                            % {"name": company.name}
                        )
                        return
                    report.pushed += 1
                link.etag, link.local_hash, link.last_synced_at = etag, source, now
                link.save()

        for pk, link in links.items():
            if not link.paired or link.remote_gone or pk in held:
                continue
            run.each(str(link.uid)[:60], lambda link=link: one(link))

        # A company deleted here takes its twin with it only when Postulo wrote that card; a
        # card somebody else made is unpaired and left (#8, #10).
        gone = {
            link.object_id: link
            for link in links.values()
            if link.target is None and link.object_id not in held
        }
        for link in gone.values():
            if (
                link.paired
                and not read_only
                and COMPANY_CARD_UID.match(link.uid or "")
                and canon(link.remote_href) in remote
            ):
                try:
                    if drop_twin(server, link, report, CARD_KEPT):
                        report.removed += 1
                except DavError as error:
                    run.skip(_tail(link.remote_href), error)
                    continue
            link.delete()

    def _send_own_card(self, run: Run, book, remote: dict[str, str]) -> None:
        """Write the person's own details to a card of their own, one way (#10).

        A new card Postulo makes in the chosen book, from the core's `person_card`: name,
        sign-in address, headline, the primary number and the first link. It is never read
        back into the profile, and a change made on the phone is overwritten by the next one
        made here. Nothing is sent while it is switched off, and the card stays.
        """
        server, connection, report, canon = run.server, run.connection, run.report, run.canon
        owner, now = run.owner, run.now
        link = SyncLink.for_record(connection, owner)
        if link is not None and not link.paired:
            return
        if not run.send_own_card:
            return
        if run.read_only:
            report.skipped += 1
            return
        person = person_card(owner)
        name = f"{person.given_name} {person.family_name}".strip()
        if not name:
            report.notes.append(_("Your own card was not sent: your profile has no name."))
            return
        number = next((n.number for n in person.numbers if n.is_primary), "") or next(
            (n.number for n in person.numbers), ""
        )
        card = text.Card(
            uid=f"you-{owner.pk}@postulo",
            name=name,
            title=person.headline,
            email=person.email,
            phone=number,
            url=next((link_row.url for link_row in person.links), ""),
        )
        source = digest(card.digest_source())
        names = (person.given_name, person.family_name)
        body = text.card_to_text(card, prodid=PRODID, rev=now, version=run.vcard, own_name=names)

        def write() -> None:
            nonlocal link
            if link is None:
                href = urljoin(book.href, f"{uuid.uuid4()}.vcf")
                etag = server.put(href, body, "text/vcard; charset=utf-8")
                SyncLink.bind(
                    connection,
                    owner,
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
                report.notes.append(_("Your own card was removed from the address book; kept."))
                return
            if link.remote_gone or (link.local_hash == source and remote[href] == link.etag):
                return
            if link.uid != card.uid:
                # A card the person paired as their own, made elsewhere: patched, never
                # rebuilt, in the properties Postulo owns, and only to give them a value:
                # what Postulo has nothing for stays as it is. Never read back.
                if link.local_hash == source:
                    return
                resource = server.get(link.remote_href)
                changes = {
                    name: value
                    for name, value in card_changes(card, text.text_to_card(resource.text)).items()
                    if name in ("FN", "TITLE", "EMAIL", "TEL", "URL") and value
                }
                etag = resource.etag
                if changes:
                    patched = text.patch_card(resource.text, link.uid, changes, rev=now)
                    if patched is None:
                        raise DavError(_("The card could not be read, so it was not changed."))
                    try:
                        etag = server.put(
                            link.remote_href,
                            patched,
                            "text/vcard; charset=utf-8",
                            etag=resource.etag,
                        )
                    except Conflict:
                        return
                    report.pushed += 1
                link.etag, link.local_hash, link.last_synced_at = etag, source, now
                link.save()
                return
            try:
                # One way: what is on the card now, a change made on the phone included, is
                # replaced; the ETag just listed keeps the write conditional.
                etag = server.put(
                    link.remote_href, body, "text/vcard; charset=utf-8", etag=remote[href]
                )
            except Conflict:
                return  # changed again since it was listed: the next run writes over it
            link.etag, link.local_hash, link.last_synced_at = etag, source, now
            link.save()
            report.pushed += 1

        run.each(_("your own card"), write)

    # -------------------------------------------------------- interviews

    def _sync_interviews(self, run: Run, calendar) -> None:
        server, connection, report = run.server, run.connection, run.report
        owner, now, read_only, canon = run.owner, run.now, run.read_only, run.canon
        remote = server.etags(calendar.href)
        self._make_interviews(run, calendar, remote)
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

    # ------------------------------------------------------------- tasks

    def _sync_tasks(self, run: Run, tasks) -> None:
        """Reminders and applications' milestones ↔ the task list, both ways (#12).

        A reminder is a task: ``SUMMARY``, ``DUE``, ``STATUS`` and ``COMPLETED`` are exchanged
        and nothing else is written, so what the phone keeps in a task (its description, its
        categories, its own alarms) survives every push, as an event somebody else made does
        (#11). A task is paired with its reminder by list and UID, its address a cache. Only
        the person's own reminders are kept: an interview's is its event's alarm already while
        events are on. A task made in a list Postulo made becomes a reminder; in any other list
        a person pairs it by hand, so that an errand never becomes one by accident.

        An application may have two more, when that is switched on: *Apply* (its deadline, its
        ``applied_at``) and *Hear back*. Postulo owns them: what the application says is
        written, and a tick on the phone does not stick, but for one *Apply* tick, which is
        filed as a suggestion for the person to accept.
        """
        server, connection, report = run.server, run.connection, run.report
        owner, now, read_only, canon = run.owner, run.now, run.read_only, run.canon
        remote = server.etags(tasks.href)
        own_list = _tail(tasks.href) == OWN_TASK_LIST
        where = canon(tasks.href)
        task_links = [link for link in SyncLink.of_model(connection, Reminder)]
        links = {link.object_id: link for link in task_links if not link.slot}
        held = run.held(links, tasks)
        mlinks = {
            (link.object_id, link.slot): link
            for link in SyncLink.of_model(connection, Application)
            if link.slot in (APPLY, REPLY)
        }
        mheld = run.held(mlinks, tasks)
        window = Reminder.objects.for_user(owner).filter(retired_at__isnull=True)
        window = window.filter(Q(done_at__isnull=True) | Q(done_at__gte=now - HISTORY))
        if run.events_on:
            window = window.exclude(interview__isnull=False)
        reminders = {r.pk: r for r in window}

        # The applications' milestones, and so which task each application's reminders sit
        # under: the one still open.
        milestones: dict[tuple[int, str], tuple[Application, text.Todo]] = {}
        if run.milestones or mlinks:
            applications = Application.objects.filter(owner=owner).select_related(
                "posting__company"
            )
            for application in applications:
                for slot in (APPLY, REPLY):
                    link = mlinks.get((application.pk, slot))
                    if (application.pk, slot) in mheld:
                        continue
                    wanted = run.milestones and (
                        (
                            slot == APPLY
                            and application.status == "draft"
                            and not application.applied_at
                        )
                        or (slot == REPLY and application.status in SENT_OPEN)
                    )
                    if link is None and not wanted:
                        continue
                    uid = (
                        link.uid
                        if link is not None
                        else milestone_uid(owner.pk, application.pk, slot)
                    )
                    ours = milestone_todo(application, slot, uid, owner.pk)
                    if ours.done and milestone_moment(application, slot, now) < now - HISTORY:
                        continue  # long finished: left where it is, as an old interview is
                    milestones[(application.pk, slot)] = (application, ours)
        opens = {pk: ours.uid for (pk, _slot), (_a, ours) in milestones.items() if not ours.done}

        def todo_of(reminder: Reminder, uid: str) -> text.Todo:
            if not reminder.application_id:
                return reminder_todo(reminder, uid)
            return reminder_todo(
                reminder,
                uid,
                refid=application_ref(owner.pk, reminder.application_id),
                parent=opens.get(reminder.application_id, ""),
            )

        cache: dict[str, tuple[dav.Resource, list[text.Todo]]] = {}
        fetched: dict[str, dav.Resource] = {}

        def read(href: str):
            key = canon(href)
            if key not in cache:
                resource = fetched.pop(key, None) or server.get(href)
                cache[key] = (
                    resource,
                    [t for t in text.text_to_todos(resource.text) if not t.recurring],
                )
            return cache[key]

        def linked() -> set[str]:
            return {
                canon(link.remote_href)
                for link in [*links.values(), *mlinks.values()]
                if link.remote_href
            }

        def strangers() -> list[str]:
            claimed = linked()
            return [h for h in remote if h in cache and h not in claimed]

        # What no link claims is read when something might be theirs: any task in the list
        # Postulo made (it is where tasks are adopted from), a reminder with no twin yet, or
        # a twin gone from its address.
        every = [(pk, link) for pk, link in links.items() if pk in reminders]
        every += [(key, mlinks[key]) for key in milestones if key in mlinks]
        wanted = (
            own_list
            or any(pk not in links for pk in reminders)
            or any(key not in mlinks for key in milestones)
            or any(
                canon(link.remote_href) not in remote or link.remote_gone
                for _key, link in every
                if link.paired and _key not in held and _key not in mheld
            )
        )
        unclaimed = [h for h in remote if h not in linked()] if wanted else []
        moved = [
            canon(link.remote_href)
            for _key, link in every
            if link.paired
            and link.remote_href
            and not link.remote_gone
            and remote.get(canon(link.remote_href), link.etag) != link.etag
        ]
        fetched.update(server.multiget(tasks.href, unclaimed + moved, CALENDAR))
        for href in unclaimed:
            try:
                read(href)
            except DavError as error:
                run.skip(_tail(href), error)

        def pair(item, ours: text.Todo, twin: str, slot: str, existing) -> None:
            """Pair ``item`` (a reminder or an application) with the task already there."""
            found = next((h for h in strangers() if any(t.uid == twin for t in cache[h][1])), None)
            if found is None or len(found) > HREF_LENGTH:
                return
            resource, todos = cache[found]
            todo = next(t for t in todos if t.uid == twin)
            same = same_task(todo, ours)
            try:
                bound = SyncLink.bind(
                    connection,
                    item,
                    slot=slot,
                    collection=where,
                    remote_href=found,
                    uid=twin,
                    etag=resource.etag if same else "",
                    local_hash=task_hash(ours) if same else "",
                    last_synced_at=now if same else None,
                    remote_gone=False,
                )
            except AlreadyPaired:
                return  # that task is another record's by now: leave both alone
            if slot:
                mlinks[(item.pk, slot)] = bound
            else:
                links[item.pk] = bound
            if existing is not None:
                report.notes.append(_("A task moved to another address; still paired."))

        # Pair: a twin that moved is found again by its UID, and a record whose task is
        # already there (a connection deleted and added again) is paired, not pushed twice.
        for pk, reminder in reminders.items():
            link = links.get(pk)
            if link is not None and (not link.paired or pk in held):
                continue
            if link is not None and canon(link.remote_href) in remote and not link.remote_gone:
                continue
            twin = link.uid if link is not None and made_elsewhere(link, reminder) else reminder.uid
            pair(reminder, todo_of(reminder, twin), twin, "", link)
        for (pk, slot), (application, ours) in milestones.items():
            link = mlinks.get((pk, slot))
            if link is not None and not link.paired:
                continue
            if link is not None and canon(link.remote_href) in remote and not link.remote_gone:
                continue
            pair(application, ours, ours.uid, slot, link)

        def put_task(ours: text.Todo, href: str, etag: str, known, *, strict: bool = False) -> str:
            """Write the task: new whole, or the properties Postulo owns into the one there."""
            if not etag:
                return server.put(
                    href,
                    text.todo_to_text(ours, prodid=PRODID, now=now),
                    "text/calendar; charset=utf-8",
                )
            current = known or server.get(href)
            theirs = next((t for t in text.text_to_todos(current.text) if t.uid == ours.uid), None)
            if theirs is None:
                raise DavError(_("The task could not be read, so it was not changed."))
            names = task_changes(ours, theirs, strict=strict)
            if not names:
                return current.etag
            patched = text.patch_todo(current.text, ours.uid, names, ours, modified=now)
            if patched is None:
                raise DavError(_("The task could not be read, so it was not changed."))
            return server.put(href, patched, "text/calendar; charset=utf-8", etag=current.etag)

        def one(reminder: Reminder) -> None:
            pk = reminder.pk
            link = links.get(pk)
            if pk in held:
                return  # paired with something outside the chosen task list
            if link is not None and (link.remote_gone or not link.paired):
                return  # removed from the list on purpose, or unpaired: leave it
            uid = link.uid if link is not None else reminder.uid
            ours = todo_of(reminder, uid)
            source = task_hash(ours)
            if link is None:
                if read_only:
                    report.skipped += 1
                    return
                href = urljoin(tasks.href, f"{reminder.uid.split('@')[0]}.ics")
                etag = put_task(ours, href, "", None)
                links[pk] = SyncLink.bind(
                    connection,
                    reminder,
                    collection=where,
                    remote_href=canon(href),
                    uid=uid,
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
                    _(
                        "The task for “%(name)s” was removed from the task list; the reminder "
                        "was kept."
                    )
                    % {"name": reminder.summary[:120]}
                )
                return
            remote_changed = remote[href] != link.etag
            local_changed = link.local_hash != source
            known = None
            if remote_changed:
                resource, todos = read(link.remote_href)
                incoming = next((t for t in todos if t.uid == uid), None) or (
                    todos[0] if todos else None
                )
                if incoming is None:
                    report.skipped += 1
                    return
                known = resource
                if local_changed and same_task(incoming, ours):
                    # Both sides already say the same: nothing to merge, only to remember.
                    link.etag, link.local_hash, link.last_synced_at = resource.etag, source, now
                    link.save()
                    return
                if local_changed and (incoming.last_modified or now) < reminder.updated_at:
                    report.notes.append(
                        _("“%(name)s” changed on the phone, but changed here later; kept ours.")
                        % {"name": reminder.summary[:120]}
                    )
                    link.etag = resource.etag
                    link.save(update_fields=["etag", "updated_at"])
                else:
                    if local_changed:
                        report.notes.append(
                            _(
                                "“%(name)s” changed here, but changed on the phone later; took "
                                "theirs."
                            )
                            % {"name": reminder.summary[:120]}
                        )
                    apply_todo(reminder, incoming, report, now)
                    reminder.refresh_from_db()
                    link.etag = resource.etag
                    link.local_hash = task_hash(todo_of(reminder, uid))
                    link.last_synced_at = now
                    link.save()
                    report.pulled += 1
                    return
            if local_changed:
                if read_only:
                    report.skipped += 1
                    return
                try:
                    etag = put_task(ours, link.remote_href, link.etag, known)
                except Conflict:
                    report.notes.append(
                        _("A reminder changed on both sides at once; next run decides.")
                    )
                    return
                link.etag, link.local_hash, link.last_synced_at = etag, source, now
                link.save()
                report.pushed += 1

        for reminder in reminders.values():
            run.each(reminder.summary, lambda reminder=reminder: one(reminder))

        def tick(application: Application, link, incoming: text.Todo, resource) -> bool:
            """A tick on an *Apply* task is a suggestion, never a status changed behind the
            person's back. One suggestion per tick: the id is the task and the moment it
            was ticked, so a tick undone and made again is a new one, and one already declined
            is not filed again (the core never files one ``external_id`` twice). ``True`` when
            this tick was declined, and the task is to be unticked."""
            moment = incoming.completed or incoming.last_modified
            marker = text.stamp(moment) if moment else resource.etag
            suggestion, _new = suggest(
                owner,
                source=ACTOR,
                external_id=f"{link.uid}:{marker}",
                application=application,
                summary=str(_("Marked as applied in the task list")),
                occurred_at=min(moment, now) if moment else now,
                suggested_status="applied",
            )
            return suggestion.status == "declined"

        def mone(application: Application, slot: str, ours: text.Todo) -> None:
            link = mlinks.get((application.pk, slot))
            if link is not None and (link.remote_gone or not link.paired):
                return  # removed from the list on purpose, or unpaired: leave it
            source = task_hash(ours)
            if link is None:
                if read_only:
                    report.skipped += 1
                    return
                href = urljoin(tasks.href, f"{ours.uid.split('@')[0]}.ics")
                etag = put_task(ours, href, "", None)
                mlinks[(application.pk, slot)] = SyncLink.bind(
                    connection,
                    application,
                    slot=slot,
                    collection=where,
                    remote_href=canon(href),
                    uid=ours.uid,
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
                    _("The task “%(name)s” was removed from the task list; it is not made again.")
                    % {"name": ours.summary[:120]}
                )
                return
            known = None
            names: set[str] = set()
            if remote[href] != link.etag:
                resource, todos = read(link.remote_href)
                incoming = next((t for t in todos if t.uid == ours.uid), None) or (
                    todos[0] if todos else None
                )
                if incoming is None:
                    report.skipped += 1
                    return
                known = resource
                if slot == APPLY and incoming.done and not ours.done:
                    if not tick(application, link, incoming, resource):
                        link.etag, link.last_synced_at = resource.etag, now
                        link.save(update_fields=["etag", "last_synced_at", "updated_at"])
                        report.pulled += 1
                        return
                # Anything else somebody did to the task is undone: the application says it.
                names = task_changes(ours, incoming, strict=True)
                if not names:
                    link.etag, link.local_hash, link.last_synced_at = resource.etag, source, now
                    link.save()
                    return
            if names or link.local_hash != source:
                if read_only:
                    report.skipped += 1
                    return
                try:
                    etag = put_task(ours, link.remote_href, link.etag, known, strict=True)
                except Conflict:
                    report.notes.append(
                        _("A task changed on both sides at once; next run decides.")
                    )
                    return
                link.etag, link.local_hash, link.last_synced_at = etag, source, now
                link.save()
                report.pushed += 1

        for (_pk, slot), (application, ours) in milestones.items():
            run.each(ours.summary, lambda a=application, s=slot, t=ours: mone(a, s, t))

        # An *Apply* tick the person declined is unticked on the next run, once.
        if not read_only:
            declined = {
                external_id.rsplit(":", 1)[0]: reviewed_at
                for external_id, reviewed_at in Suggestion.objects.filter(
                    owner=owner,
                    source=ACTOR,
                    status="declined",
                    external_id__startswith="application-",
                ).values_list("external_id", "reviewed_at")
                if reviewed_at is not None
            }
            for (_pk, slot), (application, ours) in milestones.items():
                link = mlinks.get((application.pk, slot))
                if slot != APPLY or link is None or ours.done or not link.paired:
                    continue
                when = declined.get(link.uid)
                if when is None or when <= (
                    link.last_synced_at or dt.datetime.min.replace(tzinfo=dt.UTC)
                ):
                    continue

                def untick(application=application, ours=ours, link=link) -> None:
                    try:
                        link.etag = put_task(ours, link.remote_href, link.etag, None, strict=True)
                    except Conflict:
                        return  # changed again since: the next run looks at it
                    link.last_synced_at = now
                    link.save()
                    report.pushed += 1

                run.each(ours.summary, untick)

        # Links whose record is gone: the task goes with it when Postulo made it, and is
        # cancelled when the person's own phone did (#12). One further than the window: a
        # reminder merely too old for it is left alone, as an interview is.
        def forget(links_of, model, current) -> None:
            unseen = [link.object_id for link in links_of if link.object_id not in current]
            existing = set(model.objects.filter(pk__in=unseen).values_list("pk", flat=True))
            for link in links_of:
                if link.object_id in current or link.object_id in existing:
                    continue
                if link.paired and not read_only and canon(link.remote_href) in remote:
                    try:
                        if not made_elsewhere(link):
                            if drop_twin(server, link, report, TASK_KEPT):
                                report.removed += 1
                        else:
                            self._cancel_task(run, link)
                    except DavError as error:
                        run.skip(_tail(link.remote_href), error)
                        continue
                link.delete()

        forget(list(links.values()), Reminder, reminders)
        forget(list(mlinks.values()), Application, {pk for pk, _slot in milestones})

        # Tasks that appeared in the list from elsewhere: a reminder each, in a list Postulo
        # made, about the application whose task it was made under; left alone, and counted,
        # in any other (#12).
        left = 0
        for href in [h for h in remote if h not in linked()]:
            if not own_list or href not in cache:
                left += 1
                continue
            resource, todos = cache[href]
            todo = todos[0] if todos else None
            if (
                todo is None
                or not todo.uid
                or len(href) > HREF_LENGTH
                or len(todo.uid) > UID_LENGTH
            ):
                left += 1
                continue
            if todo.done:
                left += 1
                continue
            if todo.due is None and todo.due_day is None:
                report.skipped += 1
                report.notes.append(
                    _("The task “%(name)s” has no due time, so it was not made a reminder.")
                    % {"name": todo.summary[:120]}
                )
                continue

            def adopt(href=href, resource=resource, todo=todo) -> None:
                parent = MILESTONE_UID.match(todo.parent)
                about = None
                if parent and int(parent.group(2)) == owner.pk:
                    about = Application.objects.filter(owner=owner, pk=int(parent.group(1))).first()
                reminder = create_reminder(
                    owner,
                    summary=todo.summary,
                    due=todo.due or todo.due_day,
                    application=about,
                    actor=ACTOR,
                )
                SyncLink.bind(
                    connection,
                    reminder,
                    collection=where,
                    remote_href=href,
                    uid=todo.uid,
                    etag=resource.etag,
                    local_hash=task_hash(todo_of(reminder, todo.uid)),
                    last_synced_at=now,
                )
                report.pulled += 1

            run.each(todo.summary or href, adopt)
        if left:
            report.skipped += left
            report.notes.append(
                ngettext(
                    "%(count)d task in the task list is not from Postulo and was left alone.",
                    "%(count)d tasks in the task list are not from Postulo and were left alone.",
                    left,
                )
                % {"count": left}
            )

    def _cancel_task(self, run: Run, link) -> None:
        """Mark a task somebody else made as cancelled, once its reminder is deleted here."""
        server = run.server
        resource = server.get(link.remote_href)
        patched = text.patch_todo(
            resource.text,
            link.uid,
            {"STATUS"},
            text.Todo(status="CANCELLED"),
            modified=run.now,
        )
        if patched is None:
            return
        try:
            server.put(
                link.remote_href, patched, "text/calendar; charset=utf-8", etag=resource.etag
            )
        except Conflict:
            run.report.notes.append(str(TASK_KEPT))
