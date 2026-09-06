# postulo-dav

**Company contacts in a CardDAV address book and interviews in a CalDAV calendar, in both
directions — on Nextcloud, Radicale, Baïkal, SOGo, Fastmail, iCloud and any server that
speaks the standards.**

What you want on the phone is the recruiters and the interviews. What you do not want is
your family imported into a job tracker. So this plugin for
[Postulo](https://source.tiagoagueda.com/postulo/postulo) works with two dedicated
collections, an address book called *Postulo* and a calendar called *Postulo*, created on
the server if missing, and touches nothing else.

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
| Server address | The server's root, e.g. `https://cloud.example.org`. Discovery (`/.well-known/carddav` and `caldav`) finds the rest. |
| Username, password | An app password where the server offers one. Stored encrypted. |
| Address book, Calendar | Both default to *Postulo*; created if missing. |
| Read only | Contacts and interviews still come in; nothing goes out. |
| Run | Every 15 minutes to once a day. The scheduler honours it; *Sync now* runs at once. |

Press *Test*: it finds or creates both collections and says so. Then subscribe the phone
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
takes its twin with it.

## Translations

French and Portuguese ship with the package, in `src/postulo_dav/locale/`. Postulo never
translates a plugin's strings; to add a language, copy a catalogue, translate it, and run
`python scripts/compile_messages.py`.

## Develop

```sh
uv sync        # brings in Postulo itself from its repository, for the tests
uv run pytest
uv run ruff check .
```

The tests run against a DAV server that lives in memory — discovery, two homes,
collections, ETags, conditional writes — and inside a real Postulo through its registry
and scheduler. No request leaves the machine.

## Licence

AGPL-3.0-or-later, like Postulo.
