"""The WebDAV client: discovery, listing, reading, writing, through Postulo's HTTP client.

CardDAV (RFC 6352) and CalDAV (RFC 4791) are WebDAV with two collection types and a few
properties. What this needs is: find the person's principal from the address they typed,
``/.well-known/carddav`` and ``/.well-known/caldav`` (RFC 6764), from it the address-book
home and the calendar home, list the collections there, and then the four verbs on a
collection — list with ETags, GET, PUT with ``If-Match``, DELETE with ``If-Match``. Nothing
else.

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
    '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:displayname/></d:prop></d:propfind>'
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


@dataclass(frozen=True)
class Collection:
    href: str
    name: str
    kind: str  # ADDRESSBOOK or CALENDAR


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

    def _send(
        self, method: str, href: str, *, limit: int, **kwargs
    ) -> tuple[httpx.Response, bytes]:
        """One request, its answer read to ``limit`` bytes and to a deadline, and no further.

        Redirects are followed here and not by the client, so that each hop is held to the
        configured origin like any other address the server names.
        """
        url = self.absolute(href)
        deadline = deadline_in(TOTAL_SECONDS)
        for _hop in range(MAX_REDIRECTS + 1):
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
                    raise DavError(
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

    def propfind(self, href: str, body: str, depth: str) -> ElementTree:
        response, answer = self._send(
            "PROPFIND",
            href,
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
                tree = self.propfind(start, _PRINCIPAL, "0")
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

    def home(self, principal: str, kind: str) -> str:
        tree = self.propfind(principal, _HOMES, "0")
        tag = (
            f"{{{CARD}}}addressbook-home-set"
            if kind == ADDRESSBOOK
            else f"{{{CAL}}}calendar-home-set"
        )
        for node in tree.iter(tag):
            inner = node.find(f"{{{DAV}}}href")
            if inner is not None and inner.text:
                return self.absolute(inner.text.strip())
        if kind == ADDRESSBOOK:
            raise DavError(_("The server does not name an address book home."))
        raise DavError(_("The server does not name a calendar home."))

    def collections(self, home: str) -> list[Collection]:
        tree = self.propfind(home, _COLLECTIONS, "1")
        found: list[Collection] = []
        for response in tree.iter(f"{{{DAV}}}response"):
            href_node = response.find(f"{{{DAV}}}href")
            if href_node is None or not href_node.text:
                continue
            types = response.find(f".//{{{DAV}}}resourcetype")
            if types is None:
                continue
            if types.find(f"{{{CARD}}}addressbook") is not None:
                kind = ADDRESSBOOK
            elif types.find(f"{{{CAL}}}calendar") is not None:
                kind = CALENDAR
            else:
                continue
            name_node = response.find(f".//{{{DAV}}}displayname")
            name = (name_node.text or "").strip() if name_node is not None else ""
            try:
                href = self.absolute(href_node.text.strip())
            except DavError:
                continue  # another host's: not a collection this connection may use
            found.append(Collection(href, name, kind))
        return found

    def find_collection(self, home: str, name: str, kind: str) -> Collection | None:
        """The collection called ``name`` under ``home``, or ``None``. Reads only."""
        for collection in self.collections(home):
            if collection.kind == kind and collection.name.strip().lower() == name.strip().lower():
                return collection
        return None

    def create_collection(self, home: str, name: str, kind: str) -> Collection:
        """Make the collection called ``name`` under ``home``. Writes to the server."""
        slug = "postulo-" + ("contacts" if kind == ADDRESSBOOK else "interviews")
        href = urljoin(home.rstrip("/") + "/", slug + "/")
        if kind == ADDRESSBOOK:
            response = self.request(
                "MKCOL",
                href,
                content=_MKADDRESSBOOK.format(name=_xml(name)).encode(),
                headers={"Content-Type": "application/xml; charset=utf-8"},
            )
        else:
            response = self.request(
                "MKCALENDAR",
                href,
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
            # Some servers answer without one; ask, so the next write can be conditional.
            new_etag = self.etags(href.rsplit("/", 1)[0] + "/").get(self.canonical(href), "")
        return new_etag

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
