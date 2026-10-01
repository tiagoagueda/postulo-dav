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
| Server address | The server's root, e.g. `https://cloud.example.org`. Discovery (`/.well-known/carddav` and `caldav`) finds the rest. It must be `https://`: the password goes over the wire, and only a machine on your own network may be reached over plain `http://`. |
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
the call link, the preparation notes and the application's link in the description, the
people you are meeting as attendees — informational only: Postulo never sends invitations —
and an alarm from the interview's reminder. Interviews from the last ninety days onwards
are in the calendar. An event created on the phone stays on the phone: nothing links it to
an application, and a job tracker must not invent one. Recurring events are ignored.

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
`src/postulo_dav/locale/`. Postulo never translates a plugin's strings; a catalogue for
every language Postulo offers is in place; fill one and run `uv run postulo-messages
compile`. `uv run postulo-messages extract` refreshes them from the source, and `uv run
pytest -m release` holds the twenty-four European Union catalogues complete before a
release — a gate the tag job in CI runs, and every other run skips.

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
