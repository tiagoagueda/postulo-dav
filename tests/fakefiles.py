"""A WebDAV server that keeps plain files and plain folders in memory.

Enough for the store (#5) and the backup destination (#6): folders and files, ``MKCOL``
that answers 405 and 409 as RFC 4918 §9.3.1 says, a ``PUT`` that may refuse to overwrite,
``MOVE`` with ``Overwrite: F``, ``DELETE`` with ``If-Match``, the quota, Nextcloud's chunked
upload area, and a few ways to go wrong: a full folder, a reply that was lost, a listing
that names an address elsewhere.
"""

from __future__ import annotations

import hashlib
from urllib.parse import unquote, urlsplit

import httpx

BASE = "https://cloud.example.org"
ROOT = "/remote.php/dav/files/ana/Postulo/"
UPLOADS = "/remote.php/dav/uploads/ana/"
PASSWORD = "app-pw"  # noqa: S105

CONFIG = {
    "url": BASE + ROOT,
    "username": "ana",
    "password": PASSWORD,
}


class FakeFiles:
    def __init__(self) -> None:
        self.folders: set[str] = {
            "/",
            "/remote.php/dav/files/ana/",
            ROOT,
            UPLOADS,
        }
        self.files: dict[str, tuple[bytes, str]] = {}
        self.counter = 0
        #: Every request: method and path.
        self.log: list[tuple[str, str]] = []
        self.hosts: list[str] = []
        #: The headers of every request, in order, for a test that reads them.
        self.headers: list[dict[str, str]] = []
        #: ``None`` says nothing (the property is left out); otherwise the bytes still free.
        self.quota: int | str | None = None
        #: Answer every PUT 507.
        self.full = False
        #: Store the next PUT and answer 500, as a reply lost on the way back does.
        self.lose_reply = 0
        #: Answer every request 401.
        self.refuse = False
        #: Answer a write 403.
        self.forbid_writes = False
        #: Addresses a hostile listing adds.
        self.foreign: list[str] = []
        #: The size of the biggest single PUT that was accepted, to see that chunking happened.
        self.largest_put = 0
        #: A server that does not send getcontentlength.
        self.no_length = False
        #: Chunked uploads in progress: id → {name: bytes}
        self.chunks: dict[str, dict[str, bytes]] = {}

    def _etag(self) -> str:
        self.counter += 1
        return f'"e{self.counter}"'

    def add_file(self, path: str, data: bytes) -> None:
        self.files[path] = (data, self._etag())

    def stored(self, name: str) -> bytes | None:
        found = self.files.get(ROOT + name)
        return found[0] if found else None

    def under(self, folder: str) -> dict[str, bytes]:
        return {p: d for p, (d, _e) in self.files.items() if p.startswith(folder)}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(urlsplit(str(request.url)).path)
        method = request.method
        self.log.append((method, path))
        self.hosts.append(request.url.host)
        self.headers.append(dict(request.headers))
        if (
            self.refuse
            or request.headers.get("Authorization") != httpx.BasicAuth("ana", PASSWORD)._auth_header
        ):
            return httpx.Response(401)
        body = request.read()

        if method == "PROPFIND":
            depth = request.headers.get("Depth", "0")
            if path in self.files:
                return _multistatus([(path, self._props(path))])
            if path not in self.folders:
                return httpx.Response(404)
            if "quota-available-bytes" in body.decode():
                extra = (
                    ""
                    if self.quota is None
                    else f"<d:quota-available-bytes>{self.quota}</d:quota-available-bytes>"
                )
                return _multistatus([(path, extra)])
            rows = [(path, self._props(path))]
            if depth == "1":
                for folder in self.folders:
                    if folder != path and folder.startswith(path) and self._direct(path, folder):
                        rows.append((folder, self._props(folder)))
                for name in self.files:
                    if name.startswith(path) and self._direct(path, name):
                        rows.append((name, self._props(name)))
                rows += [
                    (href, '<d:getetag>"x"</d:getetag><d:resourcetype/>') for href in self.foreign
                ]
            return _multistatus(rows)

        if method in ("MKCOL", "PUT", "MOVE", "DELETE") and self.forbid_writes:
            return httpx.Response(403)

        if method == "MKCOL":
            if path in self.folders or path in self.files:
                return httpx.Response(405)
            parent = path.rstrip("/").rsplit("/", 1)[0] + "/"
            if parent not in self.folders:
                return httpx.Response(409)
            self.folders.add(path)
            if path.startswith(UPLOADS):
                self.chunks[path] = {}
            return httpx.Response(201)

        if method == "GET":
            if path not in self.files:
                return httpx.Response(404)
            data, etag = self.files[path]
            return httpx.Response(200, content=data, headers={"ETag": etag})

        if method == "PUT":
            parent = path.rsplit("/", 1)[0] + "/"
            if self.full:
                return httpx.Response(507)
            if parent not in self.folders:
                return httpx.Response(409)
            if parent in self.chunks:
                self.chunks[parent][path.rsplit("/", 1)[1]] = body
                return httpx.Response(201)
            if request.headers.get("If-None-Match") == "*" and path in self.files:
                return httpx.Response(412)
            etag = self._etag()
            self.files[path] = (body, etag)
            self.largest_put = max(self.largest_put, len(body))
            if self.lose_reply:
                self.lose_reply -= 1
                return httpx.Response(500)
            return httpx.Response(201, headers={"ETag": etag})

        if method == "MOVE":
            target = unquote(urlsplit(request.headers["Destination"]).path)
            if path.endswith("/.file"):
                folder = path.rsplit("/", 1)[0] + "/"
                if folder not in self.chunks:
                    return httpx.Response(404)
                if request.headers.get("Overwrite") == "F" and target in self.files:
                    return httpx.Response(412)
                parts = self.chunks.pop(folder)
                self.folders.discard(folder)
                data = b"".join(parts[name] for name in sorted(parts))
                self.files[target] = (data, self._etag())
                return httpx.Response(201)
            return httpx.Response(405)

        if method == "DELETE":
            if path in self.folders:
                self.folders.discard(path)
                self.chunks.pop(path, None)
                return httpx.Response(204)
            current = self.files.get(path)
            if current is None:
                return httpx.Response(404)
            if "If-Match" in request.headers and request.headers["If-Match"] != current[1]:
                return httpx.Response(412)
            del self.files[path]
            return httpx.Response(204)

        return httpx.Response(405)

    @staticmethod
    def _direct(folder: str, path: str) -> bool:
        rest = path[len(folder) :].strip("/")
        return bool(rest) and "/" not in rest

    def _props(self, path: str) -> str:
        if path in self.folders:
            return "<d:resourcetype><d:collection/></d:resourcetype>"
        data, etag = self.files[path]
        length = "" if self.no_length else f"<d:getcontentlength>{len(data)}</d:getcontentlength>"
        return (
            f"<d:getetag>{etag}</d:getetag><d:resourcetype/>{length}"
            "<d:getlastmodified>Mon, 02 Nov 2026 03:00:00 GMT</d:getlastmodified>"
        )


def _multistatus(rows) -> httpx.Response:
    xml = ['<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">']
    for href, props in rows:
        xml.append(
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    xml.append("</d:multistatus>")
    return httpx.Response(
        207, content="".join(xml).encode(), headers={"Content-Type": "application/xml"}
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
