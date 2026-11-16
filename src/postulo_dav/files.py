"""Plain files and plain folders over WebDAV, for the store (#5) and the backup destination (#6).

:mod:`postulo_dav.dav` speaks to address books and calendars. A store keeps *files* in a
folder, and a backup destination keeps one big file in one, so what they need is the client
learning a few more things, on top of the same rules (#13, #33): the HTTP client is Postulo's
own, only ``https`` carries a password, and an address the server names is followed only on
the host the person typed. This module adds them as a subclass of :class:`~postulo_dav.dav.Server`
and not as a second client, so every request still passes the one guard that holds it to the
chosen folder:

* bytes, streamed, in both directions of the word: a file is sent from an open handle and
  never read into memory whole;
* a plain ``MKCOL``, which makes a folder and nothing else;
* plain folders in listings, with size and ETag, at Depth 1 (RFC 4918 §9.1 lets a server
  refuse infinity);
* ``DAV:quota-available-bytes``, asked for by name (RFC 4331 §3);
* ``MOVE`` with ``Overwrite: F`` (RFC 4918 §10.6), for Nextcloud's chunked upload.

A request that writes is held to the folder the connection was given, and a ``Destination``
is held to it as well.
"""

from __future__ import annotations

import datetime as dt
import email.utils
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import httpx
from django.utils.translation import gettext as _
from postulo.plugins.api import BodyRefused, deadline_in, read_body, until

from . import dav
from .dav import (
    DAV,
    READ_WRITE,
    SMALL_LIMIT,
    TOTAL_SECONDS,
    AuthenticationRefused,
    DavError,
    Server,
    _explain,
    _refusal,
)

#: What a listing of a folder asks for.
_ENTRIES = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/><d:resourcetype/>'
    "<d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>"
)
_QUOTA = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:quota-available-bytes/></d:prop></d:propfind>'
)

#: How long one upload may take in all, and how long a plain request may: a document is at
#: most twenty megabytes and a render far less, an archive of everybody's data can be
#: gigabytes on a home uplink, and the core's own lease on a send is four hours.
DOCUMENT_SECONDS = 15 * 60.0
ARCHIVE_SECONDS = 3 * 60 * 60.0


class FolderFull(DavError):
    """The server says the folder has no room (507, RFC 4918 §11.5)."""


@dataclass(frozen=True)
class Entry:
    """One thing in a folder, as a listing names it."""

    #: The canonical address, on the host that was typed.
    href: str
    #: The last part of the address, decoded.
    name: str
    folder: bool
    etag: str = ""
    size: int | None = None
    modified: dt.datetime | None = None

    @property
    def path(self) -> str:
        return urlsplit(self.href).path


def child(folder: str, name: str) -> str:
    """The address of ``name`` directly in ``folder``, the name escaped as one path segment."""
    return folder.rstrip("/") + "/" + quote(name, safe="")


class FolderServer(Server):
    """A DAV server used as a place to keep files, held to one folder."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        client: httpx.Client,
        upload_client: httpx.Client | None = None,
    ):
        super().__init__(base_url, username, password, client=client)
        #: The client uploads go through, whose request deadline is as long as an upload
        #: may take: the ordinary one is held to a minute, which suits a listing.
        self.upload_client = upload_client or client

    # ------------------------------------------------------------ the guard

    def _held(self, method: str, url: str, *, discovery: bool) -> None:
        """The base guard, plus the two verbs a folder needs: ``MKCOL`` and ``MOVE``.

        They write, so they are held to a collection chosen with read and write, exactly as
        ``PUT`` and ``DELETE`` are: a folder is made, or a file moved, only inside one.
        """
        if method in ("MKCOL", "MOVE") and not discovery:
            path = quote(unquote(urlsplit(url).path), safe="/")
            if ".." not in path.split("/"):
                wanted = path.rstrip("/") + "/"
                for root, mode in self.scopes.items():
                    if wanted.startswith(root) and mode == READ_WRITE:
                        return
            raise DavError(_("A request outside the chosen collections was not sent."))
        super()._held(method, url, discovery=discovery)

    # -------------------------------------------------------------- reading

    def entries(self, folder: str) -> list[Entry]:
        """What is directly in ``folder``, files and folders, at Depth 1.

        Only what is under the folder, on the host that was typed: a listing that names the
        folder itself, its parent, another host, or a path that climbs out is not followed.
        """
        tree = self.propfind(folder, _ENTRIES, "1")
        return self._entries_of(tree, folder, depth=1)

    def entry(self, href: str) -> Entry | None:
        """The one file or folder at ``href``, or ``None`` when the server has nothing there."""
        try:
            tree = self.propfind(href, _ENTRIES, "0")
        except DavError as error:
            if error.status in (404, 410):
                return None
            raise
        found = self._entries_of(tree, href, depth=0)
        return found[0] if found else None

    def _entries_of(self, tree, folder: str, *, depth: int) -> list[Entry]:
        root = urlsplit(self.canonical(folder)).path.rstrip("/") + "/"
        found: list[Entry] = []
        for response in tree.iter(f"{{{DAV}}}response"):
            href_node = response.find(f"{{{DAV}}}href")
            if href_node is None or not href_node.text:
                continue
            try:
                href = self.canonical(href_node.text.strip())
            except DavError:
                continue  # another host's
            path = urlsplit(href).path
            if ".." in path.split("/"):
                continue
            is_root = path.rstrip("/") + "/" == root
            if depth == 1 and (is_root or not path.startswith(root)):
                continue  # the folder itself, or somewhere that is not in it
            if depth == 0 and not is_root:
                continue
            types = response.find(f".//{{{DAV}}}resourcetype")
            folder_node = types is not None and types.find(f"{{{DAV}}}collection") is not None
            etag_node = response.find(f".//{{{DAV}}}getetag")
            length_node = response.find(f".//{{{DAV}}}getcontentlength")
            size = None
            if length_node is not None and (length_node.text or "").strip().isdecimal():
                size = int(length_node.text.strip())
            modified = None
            modified_node = response.find(f".//{{{DAV}}}getlastmodified")
            if modified_node is not None and modified_node.text:
                try:
                    modified = email.utils.parsedate_to_datetime(modified_node.text.strip())
                except (TypeError, ValueError):
                    modified = None
            found.append(
                Entry(
                    href=href,
                    name=unquote(path.rstrip("/").rsplit("/", 1)[-1]),
                    folder=folder_node,
                    etag=(etag_node.text or "").strip() if etag_node is not None else "",
                    size=size,
                    modified=modified,
                )
            )
        return found

    def free_space(self, folder: str) -> int | None:
        """The bytes the account may still store in ``folder``, or ``None`` when not known.

        ``DAV:quota-available-bytes`` is asked for by name (RFC 4331 §3). A server that does
        not say, refuses the question, or answers with Nextcloud's negative numbers (not
        computed, unknown, unlimited) leaves it unknown, which means go ahead. A refused
        password is the one thing that is not swallowed.
        """
        try:
            tree = self.propfind(folder, _QUOTA, "0")
        except AuthenticationRefused:
            raise
        except DavError:
            return None
        node = tree.find(f".//{{{DAV}}}quota-available-bytes")
        text = (node.text or "").strip() if node is not None else ""
        if not text.lstrip("-").isdecimal():
            return None
        value = int(text)
        return value if value >= 0 else None

    def download(self, href: str, limit: int) -> bytes:
        """The bytes of the file at ``href``, refusing one over ``limit``."""
        response, body = self._send("GET", href, limit=limit)
        if response.status_code != 200:
            raise _refusal(response)
        return body

    # -------------------------------------------------------------- writing

    def make_folder(self, href: str) -> None:
        """Make one folder. One that is already there is not an error; a missing parent is.

        RFC 4918 §9.3.1: 405 says it exists, 409 that a parent is missing, and so is never
        made behind the person's back: the caller makes folders one level at a time.
        """
        response = self.request("MKCOL", href)
        if response.status_code in (200, 201, 405):
            return
        if response.status_code == 409:
            raise DavError(
                _("The folder %(path)s could not be made: the folder above it is not there.")
                % {"path": unquote(urlsplit(href).path)},
                status=409,
            )
        raise _refusal(response)

    def make_path(self, root: str, parts: list[str]) -> str:
        """Make each folder in ``parts`` below ``root``, one at a time; the last one's address."""
        current = root.rstrip("/") + "/"
        for part in parts:
            current = child(current, part) + "/"
            self.make_folder(current)
        return current

    def upload(
        self,
        href: str,
        content,
        size: int,
        *,
        seconds: float,
        headers: dict[str, str] | None = None,
    ) -> str:
        """``PUT`` a stream to ``href`` without overwriting; the new ETag, or ``""``.

        ``content`` is bytes or an iterator of them, sent as it is read, and ``size`` is what
        it adds up to, declared so the server may refuse it early. A redirect is not followed
        (a body that was streamed cannot be sent twice). Answers a person can act on: 412 is
        :class:`~postulo_dav.dav.Conflict`, 507 :class:`FolderFull`.
        """
        url = self.absolute(href)
        self._held("PUT", url, discovery=False)
        deadline = deadline_in(seconds)
        sent = {"Content-Type": "application/octet-stream", "If-None-Match": "*"}
        sent.update(headers or {})
        sent["Content-Length"] = str(size)
        with (
            until(deadline),
            self.upload_client.stream(
                "PUT", url, auth=self.auth, follow_redirects=False, content=content, headers=sent
            ) as response,
        ):
            try:
                read_body(response, limit=SMALL_LIMIT, deadline=deadline)
            except BodyRefused:
                pass  # what the server wrote beside its status is not needed
        return self._upload_outcome(response)

    @staticmethod
    def _upload_outcome(response: httpx.Response) -> str:
        status = response.status_code
        if status in (200, 201, 204):
            return response.headers.get("ETag", "").strip()
        if status == 412:
            raise dav.Conflict(_explain(response), status=412)
        if status == 507:
            raise FolderFull(
                _("The folder is full: the server has no room for the file."), status=507
            )
        if 300 <= status < 400:
            raise DavError(
                _("The server redirected an upload, which is not followed."), status=status
            )
        raise _refusal(response)

    def move(self, source: str, destination: str, *, headers: dict[str, str] | None = None) -> None:
        """``MOVE`` without overwriting (``Overwrite: F``, RFC 4918 §10.6).

        The destination is held to the chosen folder as a write there is. A 412 is
        :class:`~postulo_dav.dav.Conflict`: something is already at the destination.
        """
        target = self.absolute(destination)
        self._held("PUT", target, discovery=False)
        response = self.request(
            "MOVE", source, headers={"Destination": target, "Overwrite": "F", **(headers or {})}
        )
        if response.status_code == 412:
            raise dav.Conflict(_explain(response), status=412)
        if response.status_code == 507:
            raise FolderFull(
                _("The folder is full: the server has no room for the file."), status=507
            )
        if response.status_code not in (200, 201, 204):
            raise _refusal(response)


def connect_folder(config: dict, folder: str | None = None, *, seconds: float) -> tuple:
    """Two HTTP clients under Postulo's policy, the server on top, and the folder allowed.

    ``seconds`` is how long one upload may take. Returns ``(clients, server)`` where
    ``clients`` is a context manager's worth of two clients: close both, or use
    :func:`opened`.
    """
    ordinary = dav.postulo_client(total=TOTAL_SECONDS, follow_redirects=False)
    long = dav.postulo_client(total=seconds, follow_redirects=False)
    server = FolderServer(
        str(config.get("url", "")),
        str(config.get("username", "")),
        str(config.get("password", "")),
        client=ordinary,
        upload_client=long,
    )
    server.allow(folder or server.base, READ_WRITE)
    return (ordinary, long), server


class opened:  # noqa: N801 - a context manager, read as a function
    """``with opened(config, seconds=...) as server:`` closes both clients on the way out."""

    def __init__(self, config: dict, folder: str | None = None, *, seconds: float):
        self._clients, self.server = connect_folder(config, folder, seconds=seconds)

    def __enter__(self) -> FolderServer:
        return self.server

    def __exit__(self, *exc) -> None:
        for client in self._clients:
            client.close()


def size_text(count: int) -> str:
    """A size in bytes as a person reads it: ``1.5 MB``, binary units, no sentence."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - the loop always returns
