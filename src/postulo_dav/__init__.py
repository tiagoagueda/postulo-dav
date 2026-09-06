"""postulo-dav: contacts to CardDAV and interviews to CalDAV, in both directions.

One connection — a server, a username, an app password — and Postulo keeps the
recruiters you talk to in an address book called *Postulo* and the interviews you have
coming in a calendar called *Postulo*, on whatever server speaks the standards:
Nextcloud, Radicale, Baïkal, SOGo, Fastmail, iCloud. Only those two collections are in
scope, ever: a personal address book holds hundreds of people with nothing to do with a
job hunt, and "sync everything" would import a family into a job tracker.

Changes travel both ways. What is edited here is pushed on the next run; what is edited
on the phone is pulled. When both sides changed, the later change wins and the losing
version is kept as a note. A record deleted on the other side is never deleted here.

Every string a person reads is translated by this package itself, from ``locale/`` next to
this file; Postulo never carries a plugin's words.
"""

__version__ = "0.1.0"
