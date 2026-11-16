"""A WebDAV folder as a backup destination: each verified backup, kept off the machine (#6).

The core writes a verified archive, encrypts it to the operator's age recipient (this plugin
sends what it is handed and encrypts nothing itself), and hands it to the destination staff
chose under *Server settings → Backups* (postulo/postulo#325). The destination belongs to the
instance and to nobody's account, so the folder, the user name and the password are staff's,
and the credential opens a folder of archives: the README says why it should be confined to
that folder and used for nothing else.

* **Room first.** ``DAV:quota-available-bytes``, asked by name (RFC 4331 §3): below the
  archive's size, it fails before a byte is sent, saying how much is needed. Nextcloud's
  -1, -2 and -3 (not computed, unknown, unlimited) and a server that says nothing mean go
  ahead; a 507 in the middle of the upload is a full folder.
* **Upload without overwriting.** Streamed from the file, never read into memory, under the
  archive's own name, with ``If-None-Match: *``. On a retry, a 412 over a file of the archive's
  size means the reply was lost and counts as sent; over a file of any other size it is an
  error, and the file is left as it is.
* **Large archives on Nextcloud** (a folder under ``/remote.php/dav/files/``, and above
  :data:`CHUNK_THRESHOLD`): chunked upload v2. ``MKCOL`` an upload under
  ``/remote.php/dav/uploads/<user>/``, chunks numbered 1 to 10000 of :data:`CHUNK_SIZE`, then
  ``MOVE <id>/.file`` to the target with ``Overwrite: F`` (RFC 4918 §10.6). Every step names the
  ``Destination`` and ``OC-Total-Length`` so the server may refuse early. The server drops an
  unfinished upload after a day; a retry starts a new one, and a failure here removes its own.
  Elsewhere, one ``PUT``. None of this was tried against a real Nextcloud.
* **Confirmed** before it counts: the file in the folder must show the archive's size. The
  reference is the file's address (the core has no place to keep an ETag, so a delete reads
  the current one and sends it as ``If-Match``).
* **Retention there** goes through ``list`` and ``delete``: only archives of the shape this
  destination puts there (``postulo-backup-<stamp>.tar.gz.age``) are listed, and only one of
  them directly in the folder is ever deleted; whatever else is in it is never touched. How
  many to keep is the core's setting.
* **Held to the folder** (and, while chunking, Nextcloud's uploads area): every request is,
  and an address a listing names elsewhere is not followed.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import uuid
from urllib.parse import unquote, urlsplit

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from postulo.plugins.api import (
    BackupArchive,
    BackupManifest,
    BackupRef,
    FieldSpec,
    TestResult,
)

from . import __version__
from .dav import READ_WRITE, Conflict, DavError, _refusal
from .files import ARCHIVE_SECONDS, DOCUMENT_SECONDS, child, opened, size_text

#: The name of an archive, as the core writes it and this destination keeps it: the core's
#: ``postulo-backup-<stamp>.tar.gz`` and ``.age`` after it for the encryption.
ARCHIVE_NAME = re.compile(r"postulo-backup-[0-9A-Za-z_-]+\.tar\.gz\.age")
#: An archive above this goes up in chunks, where the server is Nextcloud: 100 MiB, under what
#: a stock PHP and reverse proxy take in one request.
CHUNK_THRESHOLD = 100 * 1024 * 1024
#: One chunk: Nextcloud takes 5 MB to 5 GB, and at most 10000 of them.
CHUNK_SIZE = 50 * 1024 * 1024
MAX_CHUNKS = 10000
READ_SIZE = 1 << 20
_NEXTCLOUD = re.compile(r"^(?P<prefix>.*?/remote\.php/dav)/files/(?P<user>[^/]+)/")


def _slice(handle, length: int):
    """``length`` bytes of ``handle``, a megabyte at a time, as a request body."""
    left = length
    while left > 0:
        part = handle.read(min(READ_SIZE, left))
        if not part:
            return
        left -= len(part)
        yield part


def uploads_area(base: str) -> str | None:
    """Nextcloud's uploads area for the account ``base`` is a folder of, or ``None``."""
    parts = urlsplit(base)
    found = _NEXTCLOUD.match(parts.path)
    if found is None:
        return None
    return f"{parts.scheme}://{parts.netloc}{found['prefix']}/uploads/{found['user']}/"


class DavBackup:
    name = "webdav-backup"
    version = __version__
    kind = "backup-destination"
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
                        "Use a folder for the archives alone."
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
                        "Stored encrypted. It opens a folder of every account's backups, and "
                        "deleting is how old ones are removed: use one that reaches only this "
                        "folder, such as a Hetzner Storage Box sub-account or an account made "
                        "for backups, and nothing else."
                    )
                ),
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
        """Read the folder and ask how much room is left. Writes nothing, so the first backup
        is what proves permission to write (#2)."""
        try:
            with opened(config, seconds=DOCUMENT_SECONDS) as server:
                if server.entry(server.base) is None:
                    return TestResult(False, self._missing(server.base))
                found = [e for e in server.entries(server.base) if ARCHIVE_NAME.fullmatch(e.name)]
                free = server.free_space(server.base)
        except DavError as error:
            return TestResult(False, str(error))
        except Exception as error:  # noqa: BLE001 - the sentence is the point
            return TestResult(False, f"{type(error).__name__}: {error}")
        message = _("The folder %(path)s is there, with %(count)d of Postulo's backups in it.") % {
            "path": unquote(urlsplit(server.base).path),
            "count": len(found),
        }
        if free is not None:
            message += " " + _("There is room for %(size)s more.") % {"size": size_text(free)}
        return TestResult(True, message)

    @staticmethod
    def _missing(base: str) -> str:
        return _(
            "The folder %(path)s is not there on the server. Make it there first: Postulo does "
            "not make the folder the backups are kept in."
        ) % {"path": unquote(urlsplit(base).path)}

    # ------------------------------------------------------------------ send

    def send(self, archive: BackupArchive, manifest: BackupManifest, config: dict) -> BackupRef:
        started = time.monotonic()
        with opened(config, seconds=ARCHIVE_SECONDS) as server:
            base = server.base
            if server.entry(base) is None:
                raise DavError(self._missing(base), status=404)
            free = server.free_space(base)
            if free is not None and free < archive.size:
                raise DavError(
                    _(
                        "The folder has room for %(free)s and the backup needs %(needed)s, so "
                        "nothing was sent."
                    )
                    % {"free": size_text(free), "needed": size_text(archive.size)}
                )
            href = child(base, archive.name)
            area = uploads_area(base) if archive.size > CHUNK_THRESHOLD else None
            try:
                if area is not None:
                    server.allow(area, READ_WRITE)
                    self._chunked(server, archive, href, area, started)
                else:
                    server.upload(
                        href, archive.chunks(READ_SIZE), archive.size, seconds=ARCHIVE_SECONDS
                    )
            except Conflict:
                # Already there: a retry after a reply that was lost, or somebody else's file.
                there = server.entry(href)
                if there is None or there.folder or there.size != archive.size:
                    raise DavError(
                        _(
                            "A different file is already called %(name)s in the folder, and it "
                            "was not overwritten."
                        )
                        % {"name": archive.name}
                    ) from None
            self._confirm(server, href, archive)
            return BackupRef(
                id=urlsplit(server.canonical(href)).path,
                label=archive.name,
                created_at=dt.datetime.now(dt.UTC),
            )

    def _chunked(self, server, archive: BackupArchive, href: str, area: str, started: float):
        """Nextcloud's chunked upload v2 (``MKCOL``, numbered ``PUT``\\ s, ``MOVE``)."""
        destination = server.absolute(href)
        headers = {"Destination": destination, "OC-Total-Length": str(archive.size)}
        folder = child(area, f"postulo-{uuid.uuid4().hex}") + "/"
        made = server.request("MKCOL", folder, headers=headers)
        if made.status_code not in (200, 201):
            raise _refusal(made)
        size = min(max(CHUNK_SIZE, -(-archive.size // MAX_CHUNKS)), 5 * 1024**3)
        try:
            with archive.open() as handle:
                for number, offset in enumerate(range(0, archive.size, size), start=1):
                    left = ARCHIVE_SECONDS - (time.monotonic() - started)
                    if left <= 0:
                        raise DavError(_("The time for this upload ran out."))
                    length = min(size, archive.size - offset)
                    server.upload(
                        child(folder, f"{number:05d}"),
                        _slice(handle, length),
                        length,
                        seconds=left,
                        headers=headers,
                    )
            server.move(folder + ".file", href, headers={"OC-Total-Length": str(archive.size)})
        except Exception:
            try:  # what is unfinished goes after a day anyway; this only tidies sooner
                server.request("DELETE", folder)
            except DavError:
                pass
            raise

    @staticmethod
    def _confirm(server, href: str, archive: BackupArchive) -> None:
        """The file in the folder must show the archive's size, or it was not sent."""
        there = server.entry(href)
        if there is None or there.folder:
            raise DavError(_("The backup is not in the folder after the upload."))
        if there.size is None:
            raise DavError(_("The server does not say how big the file is, so it cannot confirm."))
        if there.size != archive.size:
            raise DavError(
                _(
                    "The folder holds %(found)d bytes of it and the backup is %(size)d: "
                    "not confirmed."
                )
                % {"found": there.size, "size": archive.size}
            )

    # ----------------------------------------------------------- list, delete

    def list(self, config: dict) -> list[BackupRef]:
        """Every archive of the shape this destination puts there, directly in the folder."""
        with opened(config, seconds=DOCUMENT_SECONDS) as server:
            return [
                BackupRef(id=entry.path, label=entry.name, created_at=entry.modified)
                for entry in server.entries(server.base)
                if not entry.folder and ARCHIVE_NAME.fullmatch(entry.name)
            ]

    def _current_etag(self, server, href: str) -> str | None:
        """The ETag the file has now, or ``None`` when it is not there."""
        there = server.entry(href)
        return None if there is None else there.etag

    def delete(self, reference: BackupRef, config: dict) -> None:
        """Remove one archive ``list`` returned: only one directly in the folder, and of the
        shape this destination writes, with ``If-Match`` the ETag it has at that moment."""
        with opened(config, seconds=DOCUMENT_SECONDS) as server:
            href = server.absolute(str(reference.id))  # never another host
            root = urlsplit(server.canonical(server.base)).path.rstrip("/") + "/"
            path = urlsplit(server.canonical(href)).path
            name = unquote(path.rsplit("/", 1)[-1])
            if (
                not server.within(server.base, href)
                or path.rsplit("/", 1)[0] + "/" != root
                or not ARCHIVE_NAME.fullmatch(name)
            ):
                raise DavError(
                    _(
                        "%(name)s is not one of the backups this destination keeps, so it was "
                        "not deleted."
                    )
                    % {"name": name}
                )
            etag = self._current_etag(server, href)
            if etag is None:
                return  # already gone
            server.delete(href, etag=etag)
