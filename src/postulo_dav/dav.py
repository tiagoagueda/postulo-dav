"""The WebDAV client: discovery, listing, reading, writing, through Postulo's HTTP client.

CardDAV (RFC 6352) and CalDAV (RFC 4791) are WebDAV with two collection types and a few
properties. What this needs is: find the person's principal from the address they typed,
``/.well-known/carddav`` and ``/.well-known/caldav`` (RFC 6764), from it the address-book
home and the calendar home, list the collections there, and then the four verbs on a
collection — list with ETags, read (GET, or a multiget REPORT for many at once), PUT with
``If-Match``, DELETE with ``If-Match``. Nothing else.

Looking a collection up and creating one are two methods on purpose, never one:
:meth:`Server.find_collection` reads, :meth:`Server.create_collection` writes, and the
caller says which it is doing. *Test* and read-only mode only ever read, so pressing a
button labelled *Test* cannot leave an address book behind on somebody's server (#2).

Every request goes through ``postulo.plugins.api.client()``, so the instance's
destination policy, timeouts and user agent apply; a server on the LAN needs the
operator's ``POSTULO_CONNECTIONS_ALLOW_PRIVATE``. Three more rules are this module's own,
because the server is somebody else's and so is whatever it writes into a collection:

* an address the server names is followed only on the host the person typed (#13, #33);
* an answer is read to a size and a deadline, never whole (#33);
* two spellings of one address are one address (#19).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from defusedxml import ElementTree
from django.utils.translation import gettext as _
from postulo.plugins.api import BodyRefused, deadline_in, read_body, until
from postulo.plugins.api import client as postulo_client

DAV = "DAV:"
CARD = "urn:ietf:params:xml:ns:carddav"
CAL = "urn:ietf:params:xml:ns:caldav"

ADDRESSBOOK = "addressbook"
CALENDAR = "calendar"

#: Every request is held to this many seconds in all, its redirects and the reading of its
#: answer included (#33): `timeout` bounds each wait, not their sum.
TOTAL_SECONDS = 60.0
#: How much of an answer is read. A listing of a big address book is long; one card or one
#: event is not, and nothing a person keeps in a contact comes near a quarter of a megabyte.
LISTING_LIMIT = 2 * 1024 * 1024
RESOURCE_LIMIT = 256 * 1024
#: What a PUT, a DELETE or a MKCOL answers: a status, and at most a short explanation.
SMALL_LIMIT = 64 * 1024
#: A server that redirects more than this is looping, or sending us somewhere it should not.
MAX_REDIRECTS = 5
#: How many cards or events one multiget REPORT asks for, and how much of its answer is
#: read. A batch whose answer is over the limit is asked again in two halves (#45).
MULTIGET_BATCH = 50
MULTIGET_LIMIT = 4 * 1024 * 1024

_PRINCIPAL = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)
_HOMES = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav" '
    'xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:prop>'
    "<c:addressbook-home-set/><cal:calendar-home-set/></d:prop></d:propfind>"
)
_COLLECTIONS = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:prop>'
    "<d:resourcetype/><d:displayname/><cal:supported-calendar-component-set/>"
    "<d:current-user-privilege-set/></d:prop></d:propfind>"
)
_ETAGS = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/><d:resourcetype/></d:prop></d:propfind>'
)
_MKADDRESSBOOK = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:mkcol xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"><d:set><d:prop>'
    "<d:resourcetype><d:collection/><c:addressbook/></d:resourcetype>"
    "<d:displayname>{name}</d:displayname></d:prop></d:set></d:mkcol>"
)
_MKCALENDAR = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
    "<d:displayname>{name}</d:displayname>"
    '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
    "</c:supported-calendar-component-set></d:prop></d:set></c:mkcalendar>"
)
#: The two multiget reports, and the property each carries the resource's text in.
_MULTIGET = {
    ADDRESSBOOK: (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:addressbook-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><d:getetag/><c:address-data/></d:prop>{hrefs}</c:addressbook-multiget>",
        f"{{{CARD}}}address-data",
    ),
    CALENDAR: (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>{hrefs}</c:calendar-multiget>",
        f"{{{CAL}}}calendar-data",
    ),
}


_QUERY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
    '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
    '<c:time-range start="{start}" end="{end}"/>'
    "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
)


_CARD_QUERY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<c:addressbook-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><d:getetag/><c:address-data/></d:prop>{filter}"
    "<c:limit><c:nresults>{limit}</c:nresults></c:limit></c:addressbook-query>"
)


class DavError(Exception):
    """The server answered, and the answer was no. The message is for the person.

    ``status`` is the HTTP status when there was one, so a caller need not read the words.
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthenticationRefused(DavError):
    """The server did not accept the user name and password (401 or 403)."""


class Conflict(DavError):
    """The record changed on the server since it was last read (412)."""


class Ambiguous(DavError):
    """Two collections answer to one name (#13)."""


class TooLarge(DavError):
    """The answer was over its size limit, or too slow, and was not read."""


#: What each mode lets a request be (#13). Reading includes the listing and the multiget
#: REPORT, which only read; the two writes are the only verbs that change a resource.
READ, READ_WRITE = "read", "readwrite"
_READING = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"})
_WRITING = frozenset({"PUT", "DELETE"})
#: What a request that is not held to a chosen collection may be: finding the account and
#: its collections, and making the one the person asked to have made.
_DISCOVERY = frozenset({"PROPFIND", "MKCOL", "MKCALENDAR"})


@dataclass(frozen=True)
class Collection:
    href: str
    name: str
    kind: str  # ADDRESSBOOK or CALENDAR
    #: The component types a calendar accepts (RFC 4791 §5.2.3); empty when it says nothing,
    #: which means all of them.
    components: tuple[str, ...] = ()
    #: What the account may do here (RFC 3744 §5.4), or ``None`` when the server does not say.
    privileges: frozenset[str] | None = None

    @property
    def accepts_events(self) -> bool:
        return self.kind == CALENDAR and (not self.components or "VEVENT" in self.components)

    @property
    def writable(self) -> bool | None:
        """Whether the account may add and change resources here; ``None`` if not told.

        Only ``bind`` with ``write-content`` counts (RFC 3744 §3.9, §3.4), or ``write`` and
        ``all``, which contain them. ``write-properties`` is not enough: Nextcloud grants it
        on a share that is read only.
        """
        if self.privileges is None:
            return None
        names = self.privileges
        return bool({"all", "write"} & names or {"bind", "write-content"} <= names)


@dataclass(frozen=True)
class Resource:
    text: str
    etag: str


def _explain(response: httpx.Response) -> str:
    return _("The server answered HTTP %(status)d for %(method)s %(path)s.") % {
        "status": response.status_code,
        "method": response.request.method,
        "path": response.request.url.path,
    }


def _refusal(response: httpx.Response) -> DavError:
    """The error a status stands for: a refused password is a sentence of its own (#28)."""
    if response.status_code in (401, 403):
        return AuthenticationRefused(
            _("The server refused the user name or password."), status=response.status_code
        )
    return DavError(_explain(response), status=response.status_code)


def _text(response: httpx.Response, body: bytes) -> str:
    try:
        return body.decode(response.charset_encoding or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class Server:
    """One DAV server, one account."""

    def __init__(self, base_url: str, username: str, password: str, *, client: httpx.Client):
        self.base = base_url.rstrip("/") + "/"
        self.client = client
        self.auth = httpx.BasicAuth(username, password)
        parts = urlsplit(self.base)
        self.origin = (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port)
        #: The collections this connection is held to, as the path of each with a slash at
        #: the end, and the mode it may be used in (#13). Nothing is chosen until
        #: :meth:`allow` says so, and a request outside what is chosen is not sent.
        self.scopes: dict[str, str] = {}

    # ------------------------------------------------------------- plumbing

    def absolute(self, href: str) -> str:
        """``href`` against the configured address, and never anywhere but that origin.

        The credentials go with every request, so an address the *server* hands back (a
        principal, a home, an entry in a listing, a redirect) is followed only where the
        person pointed this connection: the same scheme, host and port. A server, or
        whoever can write to a collection it serves, that names another host gets none of
        the password (#13, #33).
        """
        url = urljoin(self.base, href)
        parts = urlsplit(url)
        if (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port) != self.origin:
            raise DavError(
                _(
                    "The server pointed to an address outside its own (%(host)s); it was not "
                    "followed."
                )
                % {"host": parts.hostname or ""}
            )
        return url

    def canonical(self, href: str) -> str:
        """One spelling of an address, so that two spellings of one resource compare equal.

        A server may list a resource with its reserved characters escaped (Radicale writes
        ``%40`` for the ``@`` of an interview's UID) where the address was stored as it was
        written. The path is decoded and encoded again with one fixed set of characters left
        alone, and the query and fragment, which no resource here has, are dropped (#19).
        """
        parts = urlsplit(self.absolute(href))
        path = quote(unquote(parts.path), safe="/")
        return f"{parts.scheme}://{parts.netloc}{path}"

    def _root(self, href: str) -> str:
        path = quote(unquote(urlsplit(self.absolute(href)).path), safe="/")
        return path.rstrip("/") + "/"

    def allow(self, href: str, mode: str) -> None:
        """Hold this connection's requests to ``href``, in ``mode``: READ or READ_WRITE."""
        self.scopes[self._root(href)] = READ_WRITE if mode == READ_WRITE else READ

    def within(self, collection: str, href: str) -> bool:
        """Whether ``href`` is ``collection`` or something under it, on this server."""
        try:
            root, path = (
                self._root(collection),
                quote(unquote(urlsplit(self.absolute(href)).path), safe="/"),
            )
        except DavError:
            return False
        return ".." not in path.split("/") and (path.rstrip("/") + "/").startswith(root)

    def _held(self, method: str, url: str, *, discovery: bool) -> None:
        """Refuse a request this connection was not given: the one guard (#13).

        Every request passes here, each hop of a redirect included, before it is sent. Only
        discovery (a PROPFIND, or making the collection that was asked for) is exempt; the
        rest must be under a chosen collection, with a verb its mode allows. The origin was
        held by :meth:`absolute` already.
        """
        if discovery:
            if method in _DISCOVERY:
                return
            raise DavError(_("A request outside the chosen collections was not sent."))
        path = quote(unquote(urlsplit(url).path), safe="/")
        if ".." not in path.split("/"):
            wanted = path.rstrip("/") + "/"
            for root, mode in self.scopes.items():
                if wanted.startswith(root):
                    allowed = _READING | _WRITING if mode == READ_WRITE else _READING
                    if method in allowed:
                        return
                    raise DavError(
                        _("This collection is read only here, so a %(method)s was not sent.")
                        % {"method": method}
                    )
        raise DavError(_("A request outside the chosen collections was not sent."))

    def _send(
        self, method: str, href: str, *, limit: int, discovery: bool = False, **kwargs
    ) -> tuple[httpx.Response, bytes]:
        """One request, its answer read to ``limit`` bytes and to a deadline, and no further.

        Redirects are followed here and not by the client, so that each hop is held to the
        configured origin like any other address the server names.
        """
        url = self.absolute(href)
        deadline = deadline_in(TOTAL_SECONDS)
        for _hop in range(MAX_REDIRECTS + 1):
            self._held(method, url, discovery=discovery)
            with (
                until(deadline),
                self.client.stream(
                    method, url, auth=self.auth, follow_redirects=False, **kwargs
                ) as response,
            ):
                location = response.headers.get("Location")
                if response.status_code in (301, 302, 303, 307, 308) and location:
                    url = self.absolute(urljoin(url, location))
                    if response.status_code == 303:
                        method, kwargs = "GET", {}
                    continue
                try:
                    body = read_body(response, limit=limit, deadline=deadline)
                except BodyRefused as error:
                    raise TooLarge(
                        _(
                            "The server's answer for %(path)s was too large or too slow, "
                            "and was not read."
                        )
                        % {"path": urlsplit(url).path},
                    ) from error
                return response, body
        raise DavError(
            _("The server redirected %(path)s too many times.") % {"path": urlsplit(url).path}
        )

    def request(self, method: str, href: str, **kwargs) -> httpx.Response:
        """A request whose answer is small: a status and perhaps a sentence."""
        response, _body = self._send(method, href, limit=SMALL_LIMIT, **kwargs)
        return response

    def propfind(self, href: str, body: str, depth: str, *, discovery: bool = False) -> ElementTree:
        response, answer = self._send(
            "PROPFIND",
            href,
            discovery=discovery,
            limit=LISTING_LIMIT,
            content=body.encode(),
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        if response.status_code not in (207, 200):
            raise _refusal(response)
        try:
            return ElementTree.fromstring(answer)
        except ElementTree.ParseError as error:
            raise DavError(
                _("The server's answer could not be read: %(error)s") % {"error": error}
            ) from error

    # ------------------------------------------------------------ discovery

    def principal(self, well_known: str) -> str:
        """The person's principal URL.

        The address as it was typed is asked first: a DAV server under a path
        (``/dav.php/``, ``/SOGo/dav/``) answers there and may answer nowhere else (#23).
        Then ``/.well-known/carddav`` or ``caldav`` (RFC 6764), then the host's root. A
        refused password ends the search at once: it is the answer, wherever it came from
        (#28).
        """
        for start in ("", f"/.well-known/{well_known}", "/"):
            try:
                tree = self.propfind(start, _PRINCIPAL, "0", discovery=True)
            except AuthenticationRefused:
                raise
            except DavError:
                continue
            for href in tree.iter(f"{{{DAV}}}current-user-principal"):
                inner = href.find(f"{{{DAV}}}href")
                if inner is not None and inner.text:
                    return self.absolute(inner.text.strip())
        raise DavError(
            _("The server does not say where the %(kind)s account is.") % {"kind": well_known}
        )

    def homes(self, principal: str, kind: str) -> list[str]:
        """Every home set the principal names for ``kind``, first the server's first."""
        tree = self.propfind(principal, _HOMES, "0", discovery=True)
        tag = (
            f"{{{CARD}}}addressbook-home-set"
            if kind == ADDRESSBOOK
            else f"{{{CAL}}}calendar-home-set"
        )
        found: list[str] = []
        for node in tree.iter(tag):
            for inner in node.findall(f"{{{DAV}}}href"):
                if not inner.text:
                    continue
                try:
                    home = self.absolute(inner.text.strip())
                except DavError:
                    continue  # another host's: not a home this connection may use
                if home not in found:
                    found.append(home)
        if found:
            return found
        if kind == ADDRESSBOOK:
            raise DavError(_("The server does not name an address book home."))
        raise DavError(_("The server does not name a calendar home."))

    def home(self, principal: str, kind: str) -> str:
        return self.homes(principal, kind)[0]

    @staticmethod
    def _collection_of(response, href: str) -> Collection | None:
        types = response.find(f".//{{{DAV}}}resourcetype")
        if types is None:
            return None
        if types.find(f"{{{CARD}}}addressbook") is not None:
            kind = ADDRESSBOOK
        elif types.find(f"{{{CAL}}}calendar") is not None:
            kind = CALENDAR
        else:
            return None
        name_node = response.find(f".//{{{DAV}}}displayname")
        name = (name_node.text or "").strip() if name_node is not None else ""
        components = tuple(
            (comp.get("name") or "").upper()
            for comp in response.findall(
                f".//{{{CAL}}}supported-calendar-component-set/{{{CAL}}}comp"
            )
        )
        privileges = None
        privilege_set = response.find(f".//{{{DAV}}}current-user-privilege-set")
        if privilege_set is not None:
            privileges = frozenset(
                child.tag.rsplit("}", 1)[-1]
                for privilege in privilege_set.findall(f"{{{DAV}}}privilege")
                for child in privilege
            )
        return Collection(href, name, kind, components, privileges)

    def collections(self, home: str) -> list[Collection]:
        tree = self.propfind(home, _COLLECTIONS, "1", discovery=True)
        found: list[Collection] = []
        for response in tree.iter(f"{{{DAV}}}response"):
            href_node = response.find(f"{{{DAV}}}href")
            if href_node is None or not href_node.text:
                continue
            try:
                href = self.absolute(href_node.text.strip())
            except DavError:
                continue  # another host's: not a collection this connection may use
            collection = self._collection_of(response, href)
            if collection is not None:
                found.append(collection)
        return found

    def describe(self, href: str) -> Collection | None:
        """The one collection at ``href``, asked about as discovery, or ``None`` if gone.

        Asked before the collection is allowed, since what it is decides whether it is
        used at all. ``None`` when the server has nothing there, or something that is not an address
        book or a calendar: a chosen collection that vanished is reported, never made again.
        """
        try:
            tree = self.propfind(href, _COLLECTIONS, "0", discovery=True)
        except DavError as error:
            if error.status in (404, 410):
                return None
            raise
        for response in tree.iter(f"{{{DAV}}}response"):
            return self._collection_of(response, self.absolute(href))
        return None

    def find_collection(self, home: str, name: str, kind: str) -> Collection | None:
        """The collection called ``name`` under ``home``, or ``None``. Reads only.

        A name two collections share is refused rather than guessed at: a name is not an
        identity, and the first of two is somebody else's as often as it is the one meant (#13).
        """
        found = [
            c
            for c in self.collections(home)
            if c.kind == kind and c.name.strip().lower() == name.strip().lower()
        ]
        if len(found) > 1:
            raise Ambiguous(
                _(
                    "More than one collection is called “%(name)s”. Choose one of them, by "
                    "its address, instead of by name."
                )
                % {"name": name}
            )
        return found[0] if found else None

    def create_collection(self, home: str, name: str, kind: str) -> Collection:
        """Make the collection called ``name`` under ``home``. Writes to the server."""
        slug = "postulo-" + ("contacts" if kind == ADDRESSBOOK else "interviews")
        href = urljoin(home.rstrip("/") + "/", slug + "/")
        if kind == ADDRESSBOOK:
            response = self.request(
                "MKCOL",
                href,
                discovery=True,
                content=_MKADDRESSBOOK.format(name=_xml(name)).encode(),
                headers={"Content-Type": "application/xml; charset=utf-8"},
            )
        else:
            response = self.request(
                "MKCALENDAR",
                href,
                discovery=True,
                content=_MKCALENDAR.format(name=_xml(name)).encode(),
                headers={"Content-Type": "application/xml; charset=utf-8"},
            )
        if response.status_code not in (200, 201):
            raise _refusal(response)
        return Collection(href, name, kind)

    # ------------------------------------------------------------ resources

    def etags(self, collection: str) -> dict[str, str]:
        """Every resource in the collection, as canonical href → ETag.

        Only what is under the collection, on the host that was typed: a listing that names
        anything else (the collection's parent, another host, a path elsewhere) is not
        followed, and nothing is fetched, adopted, written or deleted there (#13).
        """
        tree = self.propfind(collection, _ETAGS, "1")
        return self._listed(tree, collection)

    def _listed(self, tree, collection: str) -> dict[str, str]:
        """The resources a multistatus names under ``collection``, as canonical href → ETag."""
        found: dict[str, str] = {}
        root = urlsplit(self.canonical(collection)).path.rstrip("/") + "/"
        for response in tree.iter(f"{{{DAV}}}response"):
            href_node = response.find(f"{{{DAV}}}href")
            if href_node is None or not href_node.text:
                continue
            try:
                href = self.canonical(href_node.text.strip())
            except DavError:
                continue
            path = urlsplit(href).path
            if path.rstrip("/") + "/" == root or not path.startswith(root):
                continue  # the collection itself, or somewhere that is not in it
            if ".." in path.split("/"):
                continue  # a path that climbs out of the collection once it is resolved
            types = response.find(f".//{{{DAV}}}resourcetype")
            if types is not None and types.find(f"{{{DAV}}}collection") is not None:
                continue
            etag_node = response.find(f".//{{{DAV}}}getetag")
            found[href] = (etag_node.text or "").strip() if etag_node is not None else ""
        return found

    def multiget(self, collection: str, hrefs, kind: str) -> dict[str, Resource]:
        """Several resources of ``collection`` at once, as canonical href → Resource.

        One ``addressbook-multiget`` (RFC 6352 §8.7) or ``calendar-multiget`` (RFC 4791
        §7.9) ``REPORT`` per :data:`MULTIGET_BATCH` addresses, where a ``GET`` each made a
        request per card or event (#45); both reports are mandatory, so every server
        answers them. An answer refused for its size (cards with photos) is asked again in
        two halves. Only what was asked for is taken from the answer, on the host that was
        typed; whatever the server leaves out — a 404, a resource over
        :data:`RESOURCE_LIMIT`, a batch too large even alone, a report it refuses — is
        left out here too, and the caller reads it with :meth:`get`, which says why.
        """
        wanted = list(dict.fromkeys(self.canonical(href) for href in hrefs))
        batches = [
            wanted[start : start + MULTIGET_BATCH]
            for start in range(0, len(wanted), MULTIGET_BATCH)
        ]
        found: dict[str, Resource] = {}
        while batches:
            batch = batches.pop(0)
            try:
                found.update(self._multiget(collection, batch, kind))
            except TooLarge:
                if len(batch) > 1:
                    middle = len(batch) // 2
                    batches[:0] = [batch[:middle], batch[middle:]]
            except AuthenticationRefused:
                raise
            except DavError:
                break  # the report is refused: what is left is read one by one
        return found

    def _multiget(self, collection: str, batch: list[str], kind: str) -> dict[str, Resource]:
        template, data_tag = _MULTIGET[kind]
        body = template.format(
            hrefs="".join(f"<d:href>{_xml(urlsplit(href).path)}</d:href>" for href in batch)
        )
        response, answer = self._send(
            "REPORT",
            collection,
            limit=MULTIGET_LIMIT,
            content=body.encode(),
            headers={"Content-Type": "application/xml; charset=utf-8"},
        )
        if response.status_code != 207:
            raise _refusal(response)
        try:
            tree = ElementTree.fromstring(answer)
        except ElementTree.ParseError as error:
            raise DavError(
                _("The server's answer could not be read: %(error)s") % {"error": error}
            ) from error
        asked = set(batch)
        found: dict[str, Resource] = {}
        for node in tree.iter(f"{{{DAV}}}response"):
            href_node = node.find(f"{{{DAV}}}href")
            if href_node is None or not href_node.text:
                continue
            try:
                href = self.canonical(href_node.text.strip())
            except DavError:
                continue  # another host's
            if href not in asked:
                continue  # only what was asked for, which the listing held to the collection
            for propstat in node.findall(f"{{{DAV}}}propstat"):
                status = propstat.find(f"{{{DAV}}}status")
                if status is not None and " 200 " not in f"{status.text or ''} ":
                    continue
                data = propstat.find(f".//{data_tag}")
                if data is None or not data.text:
                    continue
                if len(data.text.encode("utf-8")) > RESOURCE_LIMIT:
                    continue  # get() refuses it with a sentence
                etag_node = propstat.find(f".//{{{DAV}}}getetag")
                etag = (etag_node.text or "").strip() if etag_node is not None else ""
                found[href] = Resource(data.text, etag)
        return found

    def query_events(self, collection: str, start: str, end: str) -> dict[str, Resource]:
        """The events of ``collection`` that fall in a time range, as canonical href → Resource.

        One ``calendar-query`` REPORT with a ``time-range`` (RFC 4791 §7.8, ``start`` and
        ``end`` as UTC stamps like ``20261201T000000Z``), so the server narrows the answer
        and nothing is listed whole. Only what is under the collection is taken from it.
        An answer over its size is an error, not a part of one.
        """
        response, answer = self._send(
            "REPORT",
            collection,
            limit=MULTIGET_LIMIT,
            content=_QUERY.format(start=start, end=end).encode(),
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        )
        if response.status_code != 207:
            raise _refusal(response)
        try:
            tree = ElementTree.fromstring(answer)
        except ElementTree.ParseError as error:
            raise DavError(
                _("The server's answer could not be read: %(error)s") % {"error": error}
            ) from error
        found: dict[str, Resource] = {}
        for node in tree.iter(f"{{{DAV}}}response"):
            href_node = node.find(f"{{{DAV}}}href")
            if href_node is None or not href_node.text:
                continue
            try:
                href = self.canonical(href_node.text.strip())
            except DavError:
                continue  # another host's
            if not self.within(collection, href):
                continue
            for propstat in node.findall(f"{{{DAV}}}propstat"):
                status = propstat.find(f"{{{DAV}}}status")
                if status is not None and " 200 " not in f"{status.text or ''} ":
                    continue
                data = propstat.find(f".//{{{CAL}}}calendar-data")
                if data is None or not data.text:
                    continue
                if len(data.text.encode("utf-8")) > RESOURCE_LIMIT:
                    continue
                etag_node = propstat.find(f".//{{{DAV}}}getetag")
                etag = (etag_node.text or "").strip() if etag_node is not None else ""
                found[href] = Resource(data.text, etag)
        return found

    def query_cards(
        self, collection: str, search: str = "", limit: int = 100
    ) -> dict[str, Resource]:
        """The cards of ``collection`` whose name contains ``search``, as href → Resource.

        One ``addressbook-query`` REPORT (RFC 6352 §8.6) with a text match on ``FN`` and a
        limit, so the server narrows the answer and a whole address book is never read to
        find one card. An empty search is the first ``limit`` cards. Only what is under the
        collection is taken from the answer.
        """
        match = ""
        if search.strip():
            match = (
                '<c:filter><c:prop-filter name="FN"><c:text-match collation="i;unicode-casemap" '
                f'match-type="contains">{_xml(search.strip())}</c:text-match></c:prop-filter>'
                "</c:filter>"
            )
        response, answer = self._send(
            "REPORT",
            collection,
            limit=MULTIGET_LIMIT,
            content=_CARD_QUERY.format(filter=match, limit=int(limit)).encode(),
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        )
        if response.status_code != 207:
            raise _refusal(response)
        try:
            tree = ElementTree.fromstring(answer)
        except ElementTree.ParseError as error:
            raise DavError(
                _("The server's answer could not be read: %(error)s") % {"error": error}
            ) from error
        found: dict[str, Resource] = {}
        for node in tree.iter(f"{{{DAV}}}response"):
            href_node = node.find(f"{{{DAV}}}href")
            if href_node is None or not href_node.text:
                continue
            try:
                href = self.canonical(href_node.text.strip())
            except DavError:
                continue
            if not self.within(collection, href):
                continue
            for propstat in node.findall(f"{{{DAV}}}propstat"):
                status = propstat.find(f"{{{DAV}}}status")
                if status is not None and " 200 " not in f"{status.text or ''} ":
                    continue
                data = propstat.find(f".//{{{CARD}}}address-data")
                if data is None or not data.text or len(data.text.encode()) > RESOURCE_LIMIT:
                    continue
                etag_node = propstat.find(f".//{{{DAV}}}getetag")
                etag = (etag_node.text or "").strip() if etag_node is not None else ""
                found[href] = Resource(data.text, etag)
        return found

    def get(self, href: str) -> Resource:
        response, body = self._send("GET", href, limit=RESOURCE_LIMIT)
        if response.status_code != 200:
            raise _refusal(response)
        return Resource(_text(response, body), response.headers.get("ETag", "").strip())

    def put(self, href: str, text: str, content_type: str, *, etag: str = "") -> str:
        """Write a resource. A blank ``etag`` means "create; refuse to overwrite"."""
        headers = {"Content-Type": content_type}
        headers["If-Match" if etag else "If-None-Match"] = etag or "*"
        response = self.request("PUT", href, content=text.encode("utf-8"), headers=headers)
        if response.status_code == 412:
            raise Conflict(_explain(response), status=412)
        if response.status_code not in (200, 201, 204):
            raise _refusal(response)
        new_etag = response.headers.get("ETag", "").strip()
        if not new_etag:
            # A server that stored something other than the bytes it was sent must not
            # answer with an ETag (RFC 4791 §5.3.4, RFC 6352 §6.3.2.3), and SabreDAV drops
            # it whenever it repairs a body. Ask for this one resource, so the next write
            # can be conditional: one small request, not a listing of the whole collection
            # for every write (#46). No ETag there either leaves it blank, and the next
            # run reads the resource once and settles it.
            new_etag = self.etag(href)
        return new_etag

    def etag(self, href: str) -> str:
        """The ETag of the one resource at ``href``, or ``""`` when the server gives none."""
        collection = href.rsplit("/", 1)[0] + "/"
        tree = self.propfind(href, _ETAGS, "0")
        return self._listed(tree, collection).get(self.canonical(href), "")

    def delete(self, href: str, *, etag: str = "") -> None:
        headers = {"If-Match": etag} if etag else {}
        response = self.request("DELETE", href, headers=headers)
        if response.status_code == 412:
            raise Conflict(_explain(response), status=412)
        if response.status_code not in (200, 202, 204, 404):
            raise _refusal(response)


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


#: Names that only ever mean a machine on the person's own network.
LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".intranet", ".home.arpa")


def is_private_host(host: str) -> bool:
    """A guess, made without asking the network, at whether ``host`` is somebody's own.

    Deliberately syntactic. It is asked one question only — *may a password go over plain
    http to this?* — and the answer has to be no for anything that looks like it is out on
    the internet. A name that looks public but resolves to a box in the hallway is refused;
    the address or the local name of that box is not. ``check_destination`` remains the
    authority on whether the instance may dial an address at all.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return bool(address.is_private or address.is_loopback or address.is_link_local)
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return True
    return "." not in host  # a bare name: a Compose service, a machine on the LAN


def connect(config: dict) -> tuple[httpx.Client, Server]:
    """An HTTP client under Postulo's policy, and the server on top of it.

    Redirects are not followed by the client: the server follows them itself, one hop at a
    time, and keeps each on the host the person typed. ``total`` holds every request, its
    waits added up, to a minute (#33).
    """
    client = postulo_client(total=TOTAL_SECONDS, follow_redirects=False)
    server = Server(
        str(config.get("url", "")),
        str(config.get("username", "")),
        str(config.get("password", "")),
        client=client,
    )
    return client, server
