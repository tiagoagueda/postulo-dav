"""The WebDAV client: discovery, listing, reading, writing, through Postulo's HTTP client.

CardDAV (RFC 6352) and CalDAV (RFC 4791) are WebDAV with two collection types and a few
properties. What this needs is: find the person's principal from ``/.well-known/carddav``
and ``/.well-known/caldav`` (RFC 6764), from it the address-book home and the calendar
home, list the collections there, create one when it is missing, and then the four verbs
on a collection — list with ETags, GET, PUT with ``If-Match``, DELETE. Nothing else.

Every request goes through ``postulo.plugins.http.client()``, so the instance's
destination policy, timeouts and user agent apply; a server on the LAN needs the
operator's ``POSTULO_CONNECTIONS_ALLOW_PRIVATE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from defusedxml import ElementTree
from postulo.plugins import http

DAV = "DAV:"
CARD = "urn:ietf:params:xml:ns:carddav"
CAL = "urn:ietf:params:xml:ns:caldav"

ADDRESSBOOK = "addressbook"
CALENDAR = "calendar"

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
    """The server answered, and the answer was no. The message is for the person."""


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
    return f"HTTP {response.status_code} for {response.request.method} {response.request.url.path}"


class Server:
    """One DAV server, one account."""

    def __init__(self, base_url: str, username: str, password: str, *, client: httpx.Client):
        self.base = base_url.rstrip("/") + "/"
        self.client = client
        self.auth = httpx.BasicAuth(username, password)

    # ------------------------------------------------------------- plumbing

    def absolute(self, href: str) -> str:
        return urljoin(self.base, href)

    def request(self, method: str, href: str, **kwargs) -> httpx.Response:
        return self.client.request(method, self.absolute(href), auth=self.auth, **kwargs)

    def propfind(self, href: str, body: str, depth: str) -> ElementTree:
        response = self.request(
            "PROPFIND",
            href,
            content=body.encode(),
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        if response.status_code not in (207, 200):
            raise DavError(_explain(response))
        try:
            return ElementTree.fromstring(response.content)
        except ElementTree.ParseError as error:
            raise DavError(f"unreadable answer from the server: {error}") from error

    # ------------------------------------------------------------ discovery

    def principal(self, well_known: str) -> str:
        """The person's principal URL, from ``/.well-known/carddav`` or ``caldav``."""
        for start in (f"/.well-known/{well_known}", "/"):
            try:
                tree = self.propfind(start, _PRINCIPAL, "0")
            except DavError:
                continue
            for href in tree.iter(f"{{{DAV}}}current-user-principal"):
                inner = href.find(f"{{{DAV}}}href")
                if inner is not None and inner.text:
                    return self.absolute(inner.text.strip())
        raise DavError(f"the server names no principal for {well_known}")

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
        raise DavError(f"the server names no {kind} home")

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
            found.append(Collection(self.absolute(href_node.text.strip()), name, kind))
        return found

    def ensure_collection(self, home: str, name: str, kind: str) -> Collection:
        """The collection called ``name`` under ``home``, created when missing."""
        for collection in self.collections(home):
            if collection.name.strip().lower() == name.strip().lower():
                return collection
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
            raise DavError(_explain(response))
        return Collection(href, name, kind)

    # ------------------------------------------------------------ resources

    def etags(self, collection: str) -> dict[str, str]:
        """Every resource in the collection, as href → ETag."""
        tree = self.propfind(collection, _ETAGS, "1")
        found: dict[str, str] = {}
        collection_path = urlsplit(collection).path.rstrip("/")
        for response in tree.iter(f"{{{DAV}}}response"):
            href_node = response.find(f"{{{DAV}}}href")
            if href_node is None or not href_node.text:
                continue
            href = self.absolute(href_node.text.strip())
            if unquote(urlsplit(href).path.rstrip("/")) == unquote(collection_path):
                continue  # the collection itself
            types = response.find(f".//{{{DAV}}}resourcetype")
            if types is not None and types.find(f"{{{DAV}}}collection") is not None:
                continue
            etag_node = response.find(f".//{{{DAV}}}getetag")
            found[href] = (etag_node.text or "").strip() if etag_node is not None else ""
        return found

    def get(self, href: str) -> Resource:
        response = self.request("GET", href)
        if response.status_code != 200:
            raise DavError(_explain(response))
        return Resource(response.text, response.headers.get("ETag", "").strip())

    def put(self, href: str, text: str, content_type: str, *, etag: str = "") -> str:
        """Write a resource. A blank ``etag`` means "create; refuse to overwrite"."""
        headers = {"Content-Type": content_type}
        headers["If-Match" if etag else "If-None-Match"] = etag or "*"
        response = self.request("PUT", href, content=text.encode("utf-8"), headers=headers)
        if response.status_code == 412:
            raise Conflict(_explain(response))
        if response.status_code not in (200, 201, 204):
            raise DavError(_explain(response))
        new_etag = response.headers.get("ETag", "").strip()
        if not new_etag:
            # Some servers answer without one; ask, so the next write can be conditional.
            new_etag = self.etags(href.rsplit("/", 1)[0] + "/").get(href, "")
        return new_etag

    def delete(self, href: str, *, etag: str = "") -> None:
        headers = {"If-Match": etag} if etag else {}
        response = self.request("DELETE", href, headers=headers)
        if response.status_code == 412:
            raise Conflict(_explain(response))
        if response.status_code not in (200, 202, 204, 404):
            raise DavError(_explain(response))


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def connect(config: dict) -> tuple[httpx.Client, Server]:
    """An HTTP client under Postulo's policy, and the server on top of it."""
    client = http.client()
    server = Server(
        str(config.get("url", "")),
        str(config.get("username", "")),
        str(config.get("password", "")),
        client=client,
    )
    return client, server
