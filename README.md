# postulo-dav

**Company contacts in a CardDAV address book and interviews in a CalDAV calendar, in both
directions — on Nextcloud, Radicale, Baïkal, SOGo, Fastmail, iCloud and any server that
speaks the standards.**

What you want on the phone is the recruiters and the interviews. What you do not want is
your family imported into a job tracker. So this plugin for
[Postulo](https://source.tiagoagueda.com/postulo/postulo) works with two dedicated
collections, an address book called *Postulo* and a calendar called *Postulo*, made on the
server by the first sync if they are not there, and touches nothing else. Nothing else
makes them: not *Test*, and not a connection set to read only.

## Install

Into the environment of a running Postulo:

```sh
uv pip install git+https://source.tiagoagueda.com/postulo/postulo-dav.git
```

Restart Postulo. In a container, bake it into the image — see *Plugins in the image* on
[Installing Postulo](https://source.tiagoagueda.com/postulo/postulo/wiki/Installing-Postulo).
A server on your own network needs the operator's
`POSTULO_CONNECTIONS_ALLOW_PRIVATE=true`.

## Use

Under **Settings → Connections → Add a connection**, choose *CardDAV and CalDAV*:

| Field | What it is |
| --- | --- |
| Server address | The server's root, e.g. `https://cloud.example.org`, or the address of its DAV service where it lives under a path, e.g. `https://example.org/dav.php/`: that address is asked first, then `/.well-known/carddav` and `caldav`, then the root. It must be `https://`: the password goes over the wire, and only a machine on your own network may be reached over plain `http://`. |
| Username, password | An app password where the server offers one. Stored encrypted. |
| Collections | Press *Look on the server*, then choose one address book, one calendar and one task list from the list, each *Read only* or *Read and write*. They are kept by their address, so renaming one on the server changes nothing. A share the account may not write (it must hold `bind` and `write-content`; `write-properties` alone is what Nextcloud grants on a read-only share) offers only *Read only*; a server that does not say is offered writing, with a note. One of each kind per connection: add another connection for a second. A task list is a calendar the server says takes tasks (`VTODO`) and not events, which is how a phone tells one: a calendar that takes both is a calendar here, and cannot hold the tasks. |
| Address book, Calendar | Used only while no collection is chosen above, so a connection made before that keeps working. Both default to *Postulo*: a name or an address; made by the first sync if they are not there, never in read-only mode and never when it is an address. The whole collection syncs, and a name two collections share is refused. |
| Task list | Used only while no collection is chosen above, and blank by default: **no task list is kept unless one is named or chosen**. A name or an address; a name that is not there is made by the first sync (as *postulo-tasks*, for tasks only), never in read-only mode, never by *Test* and never when it is an address. A server that keeps no tasks gets a note in the report and the rest of the run goes on. |
| Deadlines and dates | Off by default. On, application deadlines, listing closing dates and offer answer-by dates are written to the calendar as all-day events, one way (see *Whole days* below). |
| Tasks about applications | Off by default; needs a task list. On, each application also has up to two tasks of Postulo's own in it (see below). |
| Send contacts | *All of them* (the default, and what every connection did before) or *None*. Cards already in the address book stay, nothing more is sent to them, and the run says how many were left. |
| Send companies | Off by default. On, each company you own gets a card of its own: `KIND:org` in a vCard 4 book, `X-ABShowAs:COMPANY` (Apple's property, untried here) in a book whose `supported-address-data` lists no 4.0. Name, website, numbers and postal addresses travel both ways (what one side has and the other lacks is added, nothing is removed); your own note on the company goes out one way as the card's `NOTE`, and is never read back. A company has no email to send. A company deleted here takes the card Postulo wrote with it; switching this off leaves the cards and sends nothing more. Departments are the `ORG` unit on their contacts' cards (see below), not cards of their own. |
| Send my own card | Off by default. Writes your name, sign-in address, headline, primary number, first link and primary address to a new card Postulo makes in the chosen book, one way: a change on the phone is written over and never reaches your profile. Pair a card of your own instead (the *Pairing* page, *you*) and only the properties Postulo owns are patched, and never blanked. |
| Read only | Contacts, interviews and reminders still come in; nothing goes out — not even a new address book or task list. |
| Run | Every 15 minutes to once a day. The scheduler honours it; *Sync now* runs at once. |

Press *Test*: it signs in and says which collections it found, by name and address, and in
what mode. It makes neither — the first sync does that. Then subscribe the phone
to the *Postulo* address book and calendar the way you would to any other.

## What travels

**Contacts ↔ vCard.** Name, company and department (`ORG:Company;Unit`), role (`TITLE`),
email, phone, LinkedIn (`URL`) and notes; further numbers (`TEL`), further web addresses
(`URL`) and postal addresses (`ADR`) as well. The card is written in the version the address
book lists in `supported-address-data`: 4.0, or 3.0 where 4.0 is not listed (RFC 6352
§6.2.2), without the 4.0-only `ADR` parameters. The department is the `ORG` unit only while
*Employer structure* is on for you: while it is off, nothing of a department is read or
written, and a unit on a card made elsewhere is left as it is. A contact created on the
phone in the *Postulo* address book becomes a contact here, under the company its `ORG` names
or a *(no company)* holding company until you assign one; its unit becomes its department.
Numbers, web addresses and postal addresses from a card are **added** to the contact and
none is ever removed (the core's rules still apply: a number another record holds, or an
address its country's rules refuse, is a note in the run's report); the ones you add here
are added to the card. A pull that moves a contact to another company leaves the old
company's department behind.

**Interviews ↔ VEVENT.** Start, end, *Kind: Role at Company* as the summary, the place or
the call link, the people you are meeting and the application's link (when the instance
knows its own address, `POSTULO_PUBLIC_URL`) in the description, and an alarm from the
interview's reminder. Your preparation notes are not written to the calendar, and nobody
is named as an attendee: a calendar can be shared, and many programs offer to mail an
attendee (postulo/postulo#963). The file says `METHOD:PUBLISH`, so that nothing takes it
for an invitation; a server that refuses a calendar object carrying a `METHOD` (RFC 4791
§4.1) is written to without one. What you type in the description on the phone comes back
as the interview's notes, and a later push keeps it. Interviews from the last ninety days onwards
are in the calendar. An event created on the phone stays on the phone: nothing links it to
an application, and a job tracker must not invent one. Recurring events are ignored.

**Reminders ↔ VTODO, in a task list of their own (#12).** A calendar made for events cannot hold tasks
(RFC 4791 §5.2.3), so tasks live in a task list: chosen like the other collections, or named
in *Task list*. Each reminder is a task with its summary, `DUE` (UTC), `STATUS` and
`COMPLETED`, and `PERCENT-COMPLETE` with them. From the phone an edit changes the reminder, a
moved `DUE` postpones it (a day-long `DUE` is that day at nine, your time), a tick completes it
at the moment of the tick, an untick reopens it, and `CANCELLED` completes it; each through
Postulo's own services, so the reminder says the plugin changed it. Only those properties are
ever written, into the task as it is on the server, so a description, categories, alarms of
its own and anything else the phone keeps survive every push, and an alarm's text is never
read as the task's. Reminders from the last ninety days onwards go out, done ones included. An
interview's reminder is its event's alarm already, and gets no task while interviews go to a
calendar. A task made in the list Postulo made (*postulo-tasks*) becomes a reminder, if it has a
due time and is not done; in any other list it is left alone and counted, and the person pairs
it by hand on the *Pairing* page (which also makes a reminder from one), so that an errand never
becomes a reminder by accident. A task deleted on the phone flags its link and the reminder
stays; a reminder deleted here deletes a task Postulo made and cancels one the person's own
phone made. By their documentation, and untried against any real server
here, Nextcloud, Radicale, Fastmail and DAVx5's task apps carry tasks; Google's CalDAV and
upgraded iCloud Reminders do not.

**An application's tasks (#12).** With *Tasks about applications* on, an application has two
tasks that Postulo owns, each paired by the application's link in a slot of its own
(`apply`, `reply`; the application's slot-less link stays its event's). *Apply to ROLE at
COMPANY* exists while the application is a draft: its `DUE` is the deadline (a date), it is
`COMPLETED` at `applied_at` and `CANCELLED` if the application was withdrawn unsent. *Hear back
from COMPANY about ROLE* exists once it is sent: `COMPLETED` when it is accepted or rejected,
`CANCELLED` when withdrawn or ghosted, and an offer, which is no ending, leaves it open.
`PRIORITY` follows the application's, inverted as RFC 5545 §3.8.1.9 has it (high 1, normal 5,
low 9), and `REFID` (RFC 9253) is the same on all of an application's tasks. What the
application says is written; what is done to these tasks on the phone is undone on the next
run (a tick cannot say which ending it was) — with one exception. **A tick on *Apply* is
filed as a suggestion** (*Marked as applied in the task list*, the moment of the tick as
`occurred_at`), never a status changed behind your back; accepting it moves the application
to *Applied* and stamps `applied_at` with that moment. While it waits the tick stays; if you
decline it, the next run unticks the task. A tick is one suggestion by task and moment, so
the same tick is never filed twice, even after it was declined. A reminder about an
application carries its `REFID` and sits under the application's open task by `RELATED-TO`
(RFC 5545 §3.8.4.5), which some task apps draw as a subtask (Nextcloud Tasks does, by its
documentation; the rest untried); and a task made in Postulo's own list under one of these
becomes a reminder about that application. Reminders made on the phone are read, never the
other way round: moving a reminder to another application on the phone does nothing here.
"An update" (a timeline entry) is not a task: a reminder to chase one already is.

**Pairing an event made elsewhere with an interview.** On the connection's *Pairing* page
a person pairs an event from the chosen calendar (found with a `calendar-query` over the
interviews' window and a year ahead; repeating events and Postulo's own are not offered)
with one of their interviews. Postulo's own event for that interview is then deleted, with
`If-Match`, so the meeting is not shown twice. The event keeps its `UID` and is patched,
never rebuilt: only its times, place and a cancellation are exchanged, both ways. Its
summary, description, link, attendees, alarms and any parameter such as `LANGUAGE` stay as
written, and its description is never taken for the notes. An event with an `ORGANIZER` is a
scheduling object (RFC 6638 §3) and is read, never written. Deleting the interview unpairs
it; the event stays.

An event can instead be paired with an **application**. The next sync makes an interview
for it through Postulo's own service, so it has its timeline entry and its reminder, and
pairs that interview with the event as above. The kind is *Other* (nothing on the Pairing
page asks for one; change it on the interview), and an interview deleted here is made again
while the event stays paired with the application, so unpair the application to stop that.
Events that belong to no application (an appointment with the employment office) cannot be
paired yet: the core's Pairing page has no record kind for an appointment. Whole-day events
that are on the Calendar page are written to the calendar by a switch of their own, below.

**Whole days (#11).** With *Deadlines and dates* on (off by default), the whole days of the
Calendar page go to the chosen calendar as all-day events (`TRANSP:TRANSPARENT`, so a month
of deadlines does not make you look busy): the deadline of each application, the closing
date of each listing and the date an offer must be answered by, from ninety days ago and a
year on. They come from the core's own reading of the page (`calendar_items`), under the
names it gives them, so they are the same events the feed writes. It is one way, and no
record is paired with them: each is written to `day-<uuid>.ics`, written again when it
differs from what Postulo says now (a change made on the phone is written over), drawn
`CANCELLED` once it is over (an application already sent, a listing already decided), and
deleted, with `If-Match`, once it no longer exists here or leaves the window. Only an
address of that form is ever rewritten or deleted, so an event you made is never touched.
Nothing is written in read-only mode. The core leaves the whole days out when the instance
has no public address (`POSTULO_PUBLIC_URL`), because their names are made from it; the run
says so and deletes nothing. They are never offered for pairing. Reminders are not whole
days: they are tasks, above.

**Pairing a card made elsewhere with a contact, a company or yourself.** On the connection's
*Pairing* page a person finds a card of the chosen address book (one `addressbook-query` on
its name, never the whole book) and pairs it with a contact, a company, or *you*. A card
marked `KIND:org` (or Apple's `X-ABShowAs:COMPANY`) is offered as a company only. *You* is the
card the server names as yours (`CARDDAV:principal-address`, RFC 6352 §7.1.2, which few
servers give); where the server names none, any card may be paired as yours. A card made
elsewhere, including one adopted from the phone, is patched, never rebuilt: only the name,
company and unit (the `ORG` components), title, first email, first number and web address
are replaced, numbers, web addresses and postal addresses it lacks are added, and its UID,
version, `KIND`, photo, birthday, other addresses and numbers, `X-` properties and
parameters such as `LANGUAGE` stay as written. Its `NOTE` is not exchanged either way: your
notes are private and gather conflict records. Deleting the contact unpairs the card and
leaves it; only a card Postulo wrote is deleted. A company paired with a card keeps name,
website, numbers and addresses in step both ways, never blanking a property the card has
and the company lacks. **Your own card**, once paired and with *Send my own card* on, is
written one way: your name, sign-in address, headline, first number and first link replace
the card's, only to give a property a value (a blank here never blanks the phone's), and
nothing on it is ever read back into your profile, so a phone cannot change your sign-in
email or a number you verified.

**Importing one card.** From the *Pairing* page a card of the chosen address book can be
copied once into Postulo, into a **new company**, an **existing company**, an **existing
contact** or a **new contact**. For a company: the name (the card's `FN` for a company's
card, else its first `ORG` component), website, `NOTE` as its notes, the locality and country
of an `ADR` as its location, every `ADR` as a postal address and every `TEL` as a number; a
company has no email, so the card's email, and a number the core would not keep, go into its
notes under a line naming the card, and nothing is lost silently. An existing company or
contact is only given what it lacks (its blanks are filled, a number or address is added
beside the ones it has, nothing is overwritten and nothing merges). For a contact: name,
title, one email, the primary number and web link, further numbers, web addresses and postal
addresses; the `ORG` unit becomes its department where employer structure is on, and a card
with no `ORG` gives a contact with no company. Records are written through Postulo's own
services. An import alone writes nothing to the server; pairing it afterwards is the
person's choice. Logos and photos are not imported (a logo needs a call the core does not
offer), and the differences between a card and an existing company are not listed: the
*Pairing* page has no place to show them.

**Pairing.** A card or an event is paired with its record by its `UID`, with the
address as a cache. Postulo never changes a `UID` it did not write, so a card created on the
phone keeps its own; a card moved to another address on the server is found again by it; and
a connection deleted and added again pairs with what is already there instead of making
everything twice. A server that lists its addresses with escaped characters (Radicale
writes `%40` for `@`) is read the same as one that does not.

**What the server may send.** The password goes only to the host in the address you typed:
an address in a listing, a redirect or a principal that names another host is not followed,
and nothing outside the chosen collection is read or written: one guard holds every request,
a redirect hop included, to the collections you chose and to the verbs their mode allows
(only finding the account and its collections, and making a requested collection, are
exempt). A run finds the account and its homes once, whichever halves need them, and a connection that
chose its collections by address looks up nothing but those. A collection that vanished from the server is reported, never made again. A record
paired with something outside the choice is left alone and counted. The modes are Postulo's
word, not the server's lock: an app password cannot be narrowed to one collection on
Nextcloud, Fastmail or iCloud. For real confinement use an account that receives only the
shares you want, Radicale's rights file, or a sub-account on the storage. An answer is read to a size (2 MB
for a listing, 256 KB for a card or an event) and to a minute in all; a card that is too
large, too slow or not readable is skipped with a note, and the next one is read. So is one
whose date, number, address or name does not fit. A time zone Postulo cannot resolve, a
floating time and an all-day event never move an interview.

**Both directions.** What you edit here is pushed on the next run; what you edit on the
phone is pulled. Moving an interview on the phone moves it here through Postulo's own
service, so the timeline says so and the reminder follows. Cancelling it on the phone
settles it here.

**Conflicts.** When both sides changed since the last run, the later change wins and the
losing version is kept — as a line in the contact's notes, or as a timeline entry on the
application.

**Deletions.** A record deleted on the phone is *never* deleted here: it is unlinked and
flagged, the report says so, and an interview gets a timeline entry. A record deleted here
takes its twin with it — but only the version of the twin the last sync saw. The delete
carries `If-Match`, so a card or an event you edited on the phone in the meantime is not
destroyed by a deletion on this side: it stays where it is and the run says so. A card left
behind that way comes back as a contact on the next run; an event left behind is simply one
more event that is not Postulo's.

### Documents and backups: files in a WebDAV folder

The same package carries two more plugins, for plain files in one folder rather than for
address books and calendars. Neither syncs anything: each puts files into a folder you made,
under its address as the full URL (on Nextcloud,
`https://cloud.example.org/remote.php/dav/files/<user>/<folder>/`), over `https://` only. The
folder must exist; Postulo makes what is below it for documents and never makes the folder
itself.

**The store, *WebDAV folder* (#5), one person's copies.** Under **Settings → Connections**,
choose *WebDAV folder*. Each document you hand over, sent with an application and not a PDF
exported on its own, is **copied** to the folder; Postulo's own file stays the one that counts,
and deleting the document here leaves the copy there. Files are arranged *by company* (the
default: `<company>/<role>/<day> <filename>`, dated by when it was sent, else when it was
made, and `<kind>/` for one without an application) or *flat* (`<day> <company> - <role> -
<kind> - <filename>` in the folder). Each part is made safe for a path. A file is **never
overwritten**: if the name is taken by a different file, it gets a number, and if the file is
there already, byte for byte, a retry after a lost reply is counted as sent. A full folder is
reported and tried again; a refused password switches the connection off, with the reason.

**The backup destination, *WebDAV folder* (#6), the instance's off-site copy.** Chosen by staff
under *Server settings → Backups*, not by a person. Each verified backup, already encrypted by
Postulo to your age recipient, is uploaded under its own name, never over a file. The free
space (`quota-available-bytes`) is asked first, so a folder without room receives nothing and
says how much is needed. On Nextcloud, an archive over 100 MiB goes up in chunks (upload
v2) and is moved into place; elsewhere it is one `PUT`. Neither has been tried against a real
Nextcloud. It counts as sent when the file in the folder shows the archive's size. Keeping the
newest *N* works through the folder's listing: only files named
`postulo-backup-<stamp>.tar.gz.age` are listed, only one of them is ever deleted, and each
delete carries `If-Match` with the ETag the file has at that moment. Anything else in the
folder is left alone. Postulo sends what the core hands it: it encrypts nothing itself.

**Both are held to their folder**, and a listing that names an address elsewhere is not
followed. A password that can reach only that folder keeps the promise at the server, which
this cannot: a Nextcloud app password cannot be narrowed to a folder (it can only lose file
access altogether) and a Fastmail one only by protocol. What can: for the store, WebDAV access
to a Nextcloud public share that allows uploads (the share token as the user name; an
administrator has to allow it), or a Hetzner Storage Box sub-account, which reaches only its
own sub-directory. For the backups, one of those, or a Nextcloud account made for them and
used for nothing else: the password opens a folder of everybody's data, and it can delete
every off-site archive. Neither was tried against a real server.

## Translations

French, European Portuguese and Brazilian Portuguese ship with the package, in
`src/postulo_dav/locale/`. Postulo never translates a plugin's strings, and nobody translates this one in a commit:
every translation is made in Weblate, at
[translate.tiagoagueda.com](https://translate.tiagoagueda.com/projects/postulo/), which drafts
new strings by machine and sends what speakers save back as a pull request from its
`weblate` branch. A commit runs `uv run postulo-messages extract`, which leaves an empty slot
for a new string in every catalogue, and `uv run postulo-messages guard` in CI refuses one
that writes a translation (postulo/postulo#706). The `.mo` files are not committed:
`uv run postulo-messages compile` writes them, before the tests and before `uv build`.
`uv run pytest -m release` holds the languages the paired core release selects complete before
a release — a gate the tag job in CI runs, and every other run skips.

A language code here is a BCP 47 tag in its canonical form, as everywhere in Postulo: the
language in lower case, a script with a capital, a region in capitals (`fr-FR`, `pt-BR`,
`sr-Cyrl`). The directories under `locale/` are the one exception: `fr_FR` and `sr_Cyrl`
are gettext's names for a catalogue, not codes, and `postulo-messages` derives them from
the tags, so nobody types one. Nothing this plugin writes on a card or an event names a
language today. Where one is written it is such a tag, which is what the `LANGUAGE`
parameter of a vCard (RFC 6350 §5.1) and of a calendar entry (RFC 5545 §3.2.10) takes.
The `EN` at the end of the product identifier is not one: it belongs to the identifier's
own format and stays as it is.

## Develop

```sh
uv sync        # brings in the Postulo this plugin is released beside, for the tests
uv run pytest
uv run ruff check .
```

The tests run against a DAV server that lives in memory — discovery, two homes,
collections, ETags, conditional writes and conditional deletes — and inside a real Postulo
through its registry and scheduler. No request leaves the machine. One of them reads this
package's own source and lists every name it takes from Postulo: `postulo.plugins.api` is
the surface, and everything else is written down so the reach past it stays countable
(postulo/postulo#229).

To work against a Postulo checkout beside this one rather than the pinned one:

```sh
uv pip install -e ../postulo
uv run --no-sync pytest
```

## Licence

Copyright (C) 2026 Postulo contributors. AGPL-3.0-or-later, like Postulo.
