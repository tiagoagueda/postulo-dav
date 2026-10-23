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
| Address book, Calendar | Both default to *Postulo*; made by the first sync if they are not there, never in read-only mode. |
| Read only | Contacts and interviews still come in; nothing goes out — not even a new address book. |
| Run | Every 15 minutes to once a day. The scheduler honours it; *Sync now* runs at once. |

Press *Test*: it signs in, discovers the two homes and says which of the two collections
are already there. It makes neither — the first sync does that. Then subscribe the phone
to the *Postulo* address book and calendar the way you would to any other.

## What travels

**Contacts ↔ vCard 4.0.** Name, company (`ORG`), role (`TITLE`), email, phone, LinkedIn
(`URL`) and notes. A contact created on the phone in the *Postulo* address book becomes a
contact here, under the company its `ORG` names or a *(no company)* holding company until
you assign one.

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

**Pairing.** A card or an event is paired with its record by its `UID`, with the
address as a cache. Postulo never changes a `UID` it did not write, so a card created on the
phone keeps its own; a card moved to another address on the server is found again by it; and
a connection deleted and added again pairs with what is already there instead of making
everything twice. A server that lists its addresses with escaped characters (Radicale
writes `%40` for `@`) is read the same as one that does not.

**What the server may send.** The password goes only to the host in the address you typed:
an address in a listing, a redirect or a principal that names another host is not followed,
and nothing outside the collection is read or written. An answer is read to a size (2 MB
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

AGPL-3.0-or-later, like Postulo.
