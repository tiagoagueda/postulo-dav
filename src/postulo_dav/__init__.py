"""postulo-dav: contacts to CardDAV and interviews to CalDAV, in both directions.

One connection — a server, a username, an app password — and Postulo keeps the
recruiters you talk to in an address book called *Postulo* and the interviews you have
coming in a calendar called *Postulo*, on a server that speaks the standards on one host
and takes a password: Nextcloud, Radicale, Baïkal, SOGo. The README's *Tried with* says
what was really tried; a provider that keeps its collections on another host than the one
typed (iCloud, Fastmail's two servers) is not followed there (#13). Only those two
collections are in scope, ever: a personal address book holds hundreds of people with
nothing to do with a job hunt, and "sync everything" would import a family into a job
tracker.

Changes travel both ways. What is edited here is pushed on the next run; what is edited
on the phone is pulled. When both sides changed, the later change wins and the losing
version is kept as a note. A record deleted on the other side is never deleted here.

Every string a person reads is translated by this package itself, from ``locale/`` next to
this file; Postulo never carries a plugin's words.
"""

from importlib import metadata

#: One version, kept in `pyproject.toml` and read back from the installed distribution.
#: Written twice it drifts, and it had: `0.1.0` here against `0.3.0` there, which the
#: connections page, the `PRODID` of every card and event, and the plugin registry all
#: repeated (#2). The fallback is for a source tree that was never installed.
try:
    __version__ = metadata.version("postulo-dav")
except metadata.PackageNotFoundError:  # pragma: no cover - only outside an installation
    __version__ = "0.0.0+unknown"
