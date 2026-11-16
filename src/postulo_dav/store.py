"""A WebDAV folder as a store: a copy of each document handed over, kept where the person says (#5).

Postulo's core offers a document to every store the person has connected, once it has been
*handed over* (sent with an application, never a PDF exported on its own), and keeps its own
file as the one that counts: what is put here is a **copy**. Deleting the document in Postulo
leaves the copy where it is, because the core calls neither ``delete`` nor ``browse``.

The connection is its own (a connection has one plugin and one kind): the address of the
folder, as its full URL (RFC 6764 defines well-known addresses for CardDAV and CalDAV only,
and on Nextcloud it is ``/remote.php/dav/files/<user>/<folder>/``), a user name and a password.
The folder has to exist; what is below it is made as it is needed, one level at a time, since
``MKCOL`` answers 409 while a parent is missing (RFC 4918 §9.3.1).

* **Where a file goes** comes from the document's metadata, each part made safe for a path:
  by company, ``<company>/<role>/<day> <filename>`` (``<kind>/`` without an application), or
  flat, a descriptive name in the folder itself. The day is when it was sent, else when it was
  made.
* **Create, never overwrite.** ``PUT`` with ``If-None-Match: *``. A 412 reads the file there
  back: the same size and SHA-256 is a retry after a lost reply and counts as sent; anything
  else gets a numbered name.
* **Answers a person can act on.** A full folder (507) is a failure the scheduler retries; a
  refused password (401) ends the connection, with the reason, so it does not fail for every
  document after.
* **Held to the folder.** Every request is under it, on the host that was typed, through
  :class:`~postulo_dav.files.FolderServer`; an address a listing names elsewhere is not
  followed. A credential that opens only one folder is the better way to keep that promise,
  and the README says how.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from postulo.plugins.api import (
    ConnectionUnusable,
    DocumentMetadata,
    ExternalRef,
    FieldSpec,
    TestResult,
)

from . import __version__
from .dav import AuthenticationRefused, Conflict, DavError
from .files import DOCUMENT_SECONDS, FolderFull, child, opened, size_text

COMPANY, FLAT = "company", "flat"

#: What a name part may not hold: controls, and what some file system or server refuses.
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
#: How long a folder's name may be, and a whole file's.
SEGMENT_LIMIT = 80
NAME_LIMIT = 100
#: How many numbered names are tried before giving up on a name that keeps clashing.
MAX_NAMES = 50
CHUNK = 1 << 20


def segment(text: str, limit: int = SEGMENT_LIMIT) -> str:
    """One part of a path, safe on a server and in a file manager, never empty.

    Reserved characters become spaces, runs of space one, and leading and trailing dots and
    spaces go (which is also what makes ``..`` impossible). A part that was nothing but
    those is an underscore.
    """
    text = unicodedata.normalize("NFC", text or "")
    text = _UNSAFE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .") or "_"


def _file_name(filename: str, limit: int = NAME_LIMIT) -> str:
    base = re.split(r"[\\/]", filename or "")[-1].strip() or "document"
    suffix = PurePosixPath(base).suffix
    if len(suffix) > 10:
        suffix = ""
    suffix = _UNSAFE.sub("", suffix).strip()
    stem = base[: len(base) - len(PurePosixPath(base).suffix)] if suffix else base
    return segment(stem, limit - len(suffix)) + suffix


def _numbered(name: str, number: int) -> str:
    suffix = PurePosixPath(name).suffix
    if len(suffix) > 10:
        suffix = ""
    stem = name[: len(name) - len(suffix)]
    return f"{stem} ({number}){suffix}"


def layout_path(metadata: DocumentMetadata, layout: str) -> tuple[list[str], str]:
    """The folders below the chosen one, and the file's name, for ``metadata``."""
    day = metadata.sent_on or metadata.created_on or metadata.created_at.date()
    filename = _file_name(metadata.filename or metadata.title)
    kind = segment(metadata.kind_label or metadata.kind, 40)
    if layout == FLAT:
        parts = [segment(value, 40) for value in (metadata.company, metadata.role) if value.strip()]
        described = " - ".join([*parts, kind, filename])
        return [], f"{day.isoformat()} {described}"
    if metadata.company.strip():
        folders = [segment(metadata.company)]
        if metadata.role.strip():
            folders.append(segment(metadata.role))
        return folders, f"{day.isoformat()} {filename}"
    return [kind], f"{day.isoformat()} {filename}"


def _stream(handle):
    handle.seek(0)
    while chunk := handle.read(CHUNK):
        yield chunk


def _measure(handle, metadata: DocumentMetadata) -> tuple[int, str]:
    """The file's size, and its SHA-256: the recorded one where there is one."""
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(0)
    if metadata.checksum:
        return size, metadata.checksum.lower()
    digest = hashlib.sha256()
    for chunk in _stream(handle):
        digest.update(chunk)
    return size, digest.hexdigest()


class DavStore:
    name = "webdav"
    version = __version__
    kind = "store"
    label = gettext_lazy("WebDAV folder")

    def config_fields(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "url",
                str(_("Folder address")),
                type="url",
                help=str(
                    _(
                        "The folder's full address, which has to exist. On Nextcloud: "
                        "https://cloud.example.org/remote.php/dav/files/USER/FOLDER/. "
                        "Postulo makes the folders below it as it needs them and never writes "
                        "outside it."
                    )
                ),
            ),
            FieldSpec("username", str(_("Username")), type="text"),
            FieldSpec(
                "password",
                str(_("Password or app password")),
                type="password",
                secret=True,
                help=str(
                    _(
                        "Stored encrypted. Most servers cannot narrow a password to one folder; "
                        "a Nextcloud public share that allows uploads, or a Hetzner Storage Box "
                        "sub-account, can."
                    )
                ),
            ),
            FieldSpec(
                "layout",
                str(_("How files are arranged")),
                type="choice",
                choices=(
                    (COMPANY, str(_("By company and role, each in its own folder"))),
                    (FLAT, str(_("All in the folder, with descriptive names"))),
                ),
                default=COMPANY,
                required=False,
            ),
        ]

    def summary(self, config: dict) -> str:
        return str(config.get("url", "")).rstrip("/")

    def validate(self, config: dict) -> dict[str, list[str]]:
        """The sync's rule: a password goes only over https, or to a machine on the LAN."""
        from .sync import DavSync

        return DavSync().validate(config)

    # ------------------------------------------------------------------ test

    def test(self, config: dict) -> TestResult:
        """Read the folder and ask how much room is left. Writes nothing (#2)."""
        try:
            with opened(config, seconds=DOCUMENT_SECONDS) as server:
                if server.entry(server.base) is None:
                    return TestResult(False, self._missing(server.base))
                found = server.entries(server.base)
                free = server.free_space(server.base)
        except DavError as error:
            return TestResult(False, str(error))
        except Exception as error:  # noqa: BLE001 - the sentence is the point
            return TestResult(False, f"{type(error).__name__}: {error}")
        message = _("The folder %(path)s is there, with %(count)d items in it.") % {
            "path": unquote(urlsplit(server.base).path),
            "count": len(found),
        }
        if free is not None:
            message += " " + _("There is room for %(size)s more.") % {"size": size_text(free)}
        return TestResult(True, message)

    @staticmethod
    def _missing(base: str) -> str:
        return _(
            "The folder %(path)s is not there on the server. Make it there first: Postulo makes "
            "only the folders below it."
        ) % {"path": unquote(urlsplit(base).path)}

    # ------------------------------------------------------------------- put

    def put(self, document, file, metadata: DocumentMetadata, config: dict, user) -> ExternalRef:
        parts, name = layout_path(metadata, config.get("layout") or COMPANY)
        size, digest = _measure(file, metadata)
        try:
            with opened(config, seconds=DOCUMENT_SECONDS) as server:
                if server.entry(server.base) is None:
                    raise DavError(self._missing(server.base), status=404)
                folder = server.make_path(server.base, parts)
                for number in range(1, MAX_NAMES + 1):
                    href = child(folder, name if number == 1 else _numbered(name, number))
                    try:
                        server.upload(
                            href,
                            _stream(file),
                            size,
                            seconds=DOCUMENT_SECONDS,
                            headers={
                                "Content-Type": metadata.content_type or "application/octet-stream"
                            },
                        )
                    except Conflict:
                        if self._is_the_same(server, href, size, digest):
                            return self._ref(server, href)  # a retry after a lost reply
                        continue
                    return self._ref(server, href)
        except AuthenticationRefused as refused:
            if refused.status == 401:
                raise ConnectionUnusable(str(refused)) from refused
            raise
        except FolderFull:
            raise
        raise DavError(
            _("Too many files already have the name %(name)s in that folder.") % {"name": name}
        )

    @staticmethod
    def _ref(server, href: str) -> ExternalRef:
        return ExternalRef(store=DavStore.name, id=urlsplit(server.canonical(href)).path)

    @staticmethod
    def _is_the_same(server, href: str, size: int, digest: str) -> bool:
        """Whether the file already at ``href`` is the one being sent: size, then SHA-256."""
        there = server.entry(href)
        if there is None or there.folder or there.size != size:
            return False
        return hashlib.sha256(server.download(href, size + 1)).hexdigest() == digest
