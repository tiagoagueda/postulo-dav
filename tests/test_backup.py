"""The WebDAV backup destination (#6): each verified instance backup, uploaded to a folder."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from postulo.plugins import registry
from postulo.plugins.api import (
    BackupArchive,
    BackupDestinationPlugin,
    BackupManifest,
    BackupRef,
)
from postulo.plugins.testing import assert_is_a_backup_destination

from postulo_dav import backup as backup_module
from postulo_dav import dav as dav_module
from postulo_dav.backup import DavBackup
from tests.fakefiles import BASE, CONFIG, ROOT, UPLOADS, FakeFiles

NAME = "postulo-backup-20261101-030000.tar.gz.age"
PAYLOAD = b"age-encryption.org/v1\n" + bytes(range(256)) * 200


@pytest.fixture
def fake(monkeypatch):
    fake = FakeFiles()

    def fake_client(**kwargs):
        kwargs.pop("event_hooks", None)
        kwargs.pop("total", None)
        kwargs.setdefault("timeout", 10)
        kwargs.setdefault("follow_redirects", True)
        return httpx.Client(transport=httpx.MockTransport(fake.handler), **kwargs)

    monkeypatch.setattr(dav_module, "postulo_client", fake_client)
    return fake


def archive(tmp_path, name=NAME, data=PAYLOAD) -> BackupArchive:
    path = tmp_path / name
    path.write_bytes(data)
    return BackupArchive(name=name, size=len(data), _path=path)


MANIFEST = BackupManifest(
    archive=NAME.removesuffix(".age"),
    created_at=dt.datetime(2026, 11, 1, 3, 0, tzinfo=dt.UTC),
    version="0.5.0",
    engine="sqlite",
    instance="Postulo",
)


def send(tmp_path, config=None, **kwargs):
    return DavBackup().send(archive(tmp_path, **kwargs), MANIFEST, config or CONFIG)


# ------------------------------------------------------------------- the plugin


def test_it_is_a_backup_destination_registered_under_its_entry_point():
    plugin = DavBackup()
    assert isinstance(plugin, BackupDestinationPlugin)
    assert plugin.kind == "backup-destination"
    assert "webdav-backup" in {item.name for item in registry.plugins("backup-destination")}


def test_it_keeps_the_contract_the_core_publishes(fake, tmp_path):
    assert_is_a_backup_destination(DavBackup(), CONFIG, tmp_path=tmp_path)


def test_it_asks_for_an_address_a_user_and_a_secret_password():
    fields = {spec.name: spec for spec in DavBackup().config_fields()}
    assert set(fields) == {"url", "username", "password"}
    assert fields["password"].secret


def test_a_password_will_not_go_over_plain_http():
    assert "url" in DavBackup().validate({**CONFIG, "url": "http://cloud.example.org/dav/"})
    assert DavBackup().validate(CONFIG) == {}


# ------------------------------------------------------------------------- send


def test_an_archive_is_uploaded_whole_under_its_own_name_without_overwriting(fake, tmp_path):
    ref = send(tmp_path)
    assert fake.stored(NAME) == PAYLOAD
    assert isinstance(ref, BackupRef)
    assert ref.id == ROOT + NAME
    assert ref.label == NAME
    puts = [h for h in fake.headers if h.get("if-none-match")]
    assert len(puts) == 1 and puts[0]["if-none-match"] == "*"
    assert puts[0]["content-length"] == str(len(PAYLOAD))
    assert not [1 for m, _p in fake.log if m in ("MKCOL", "MOVE")]


def test_it_is_streamed_and_never_read_into_memory(fake, tmp_path):
    seen = []
    handed = archive(tmp_path)

    class Counting(BackupArchive):
        def chunks(self, size: int = 1 << 20):
            for part in super().chunks(1000):
                seen.append(len(part))
                yield part

    DavBackup().send(
        Counting(name=handed.name, size=handed.size, _path=handed._path), MANIFEST, CONFIG
    )
    assert max(seen) <= 1000 and len(seen) > 1
    assert fake.stored(NAME) == PAYLOAD


def test_room_is_asked_first_and_too_little_sends_nothing(fake, tmp_path):
    fake.quota = len(PAYLOAD) - 1
    with pytest.raises(Exception, match=r"needs? ") as info:
        send(tmp_path)
    assert "KB" in str(info.value)
    assert not [m for m, _p in fake.log if m in ("PUT", "MKCOL", "MOVE")]
    assert fake.stored(NAME) is None


@pytest.mark.parametrize("quota", [None, -1, -2, -3, len(PAYLOAD), 10**12])
def test_a_quota_that_is_unknown_unlimited_or_enough_means_go_ahead(fake, tmp_path, quota):
    fake.quota = quota
    send(tmp_path)
    assert fake.stored(NAME) == PAYLOAD


def test_a_full_folder_in_the_middle_of_the_upload_says_so(fake, tmp_path):
    fake.full = True
    with pytest.raises(Exception, match="full"):
        send(tmp_path)


def test_a_retry_after_a_lost_reply_leaves_one_archive(fake, tmp_path):
    fake.lose_reply = 1
    with pytest.raises(Exception, match="500"):
        send(tmp_path)
    ref = send(tmp_path)
    assert ref.id == ROOT + NAME
    assert list(fake.under(ROOT)) == [ROOT + NAME]


def test_a_different_file_under_the_name_is_never_overwritten(fake, tmp_path):
    fake.add_file(ROOT + NAME, b"something else")
    with pytest.raises(Exception, match="different"):
        send(tmp_path)
    assert fake.stored(NAME) == b"something else"


def test_it_is_confirmed_by_size_before_it_counts_as_sent(fake, tmp_path):
    fake.shorten = 3
    with pytest.raises(Exception, match=str(len(PAYLOAD) - 3)):
        send(tmp_path)


def test_a_server_that_does_not_say_the_size_cannot_confirm(fake, tmp_path):
    fake.no_length = True
    with pytest.raises(Exception, match="confirm"):
        send(tmp_path)


def test_a_missing_folder_is_not_made_up(fake, tmp_path):
    with pytest.raises(Exception, match="not there"):
        send(tmp_path, {**CONFIG, "url": BASE + "/remote.php/dav/files/ana/Nowhere/"})
    assert not [1 for m, _p in fake.log if m == "MKCOL"]


def test_a_refused_password_is_a_sentence(fake, tmp_path):
    with pytest.raises(Exception, match="password"):
        send(tmp_path, {**CONFIG, "password": "wrong"})


# ----------------------------------------------------------------- chunked upload


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(backup_module, "CHUNK_THRESHOLD", 1000)
    monkeypatch.setattr(backup_module, "CHUNK_SIZE", 1500)


def test_a_large_archive_on_nextcloud_goes_up_in_chunks_and_is_moved_into_place(
    fake, tmp_path, small_chunks
):
    ref = send(tmp_path)
    assert fake.stored(NAME) == PAYLOAD
    assert ref.id == ROOT + NAME
    assert fake.largest_put == 0  # no plain PUT of the whole: the chunks were not stored as files
    methods = [(m, p) for m, p in fake.log if p.startswith(UPLOADS)]
    assert methods[0][0] == "MKCOL"
    chunks = [p for m, p in methods if m == "PUT"]
    assert len(chunks) == -(-len(PAYLOAD) // 1500)
    assert [p.rsplit("/", 1)[1] for p in chunks] == [f"{n:05d}" for n in range(1, len(chunks) + 1)]
    assert methods[-1] == ("MOVE", methods[0][1] + ".file")
    move = next(h for (m, p), h in zip(fake.log, fake.headers, strict=True) if m == "MOVE")
    assert move["overwrite"] == "F"
    assert move["destination"] == BASE + ROOT + NAME
    assert move["oc-total-length"] == str(len(PAYLOAD))
    # Every step names the destination and the total, for an early refusal.
    for (m, p), headers in zip(fake.log, fake.headers, strict=True):
        if p.startswith(UPLOADS) and m in ("MKCOL", "PUT", "MOVE"):
            assert headers["destination"] == BASE + ROOT + NAME
            assert headers["oc-total-length"] == str(len(PAYLOAD))
    assert not fake.chunks  # the upload's own folder is gone


def test_a_chunked_retry_over_the_same_archive_counts_as_sent(fake, tmp_path, small_chunks):
    fake.add_file(ROOT + NAME, PAYLOAD)
    ref = send(tmp_path)
    assert ref.id == ROOT + NAME
    assert not fake.chunks


def test_a_chunked_clash_with_another_file_is_refused_and_cleaned_up(fake, tmp_path, small_chunks):
    fake.add_file(ROOT + NAME, b"x" * 10)
    with pytest.raises(Exception, match="different"):
        send(tmp_path)
    assert fake.stored(NAME) == b"x" * 10
    assert not fake.chunks


def test_a_chunk_that_fails_leaves_no_half_upload(fake, tmp_path, small_chunks):
    fake.full = True
    with pytest.raises(Exception, match="full"):
        send(tmp_path)
    assert not fake.chunks
    assert fake.stored(NAME) is None


def test_below_the_threshold_it_is_one_put(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(backup_module, "CHUNK_THRESHOLD", len(PAYLOAD) + 1)
    send(tmp_path)
    assert not [p for _m, p in fake.log if p.startswith(UPLOADS)]


def test_off_nextcloud_it_is_one_put_however_large(fake, tmp_path, small_chunks):
    fake.folders |= {"/dav/", "/dav/backups/"}
    config = {**CONFIG, "url": BASE + "/dav/backups/"}
    send(tmp_path, config)
    assert fake.files["/dav/backups/" + NAME][0] == PAYLOAD
    assert not [p for _m, p in fake.log if p.startswith(UPLOADS)]


# --------------------------------------------------------------- list and delete


def populate(fake):
    names = [
        "postulo-backup-20261101-030000.tar.gz.age",
        "postulo-backup-20261102-030000.tar.gz.age",
    ]
    for name in names:
        fake.add_file(ROOT + name, b"data " + name.encode())
    fake.add_file(ROOT + "holiday.jpg", b"jpeg")
    fake.add_file(ROOT + "postulo-backup-20261101-030000.tar.gz", b"unencrypted?!")
    fake.folders.add(ROOT + "postulo-backup-20261103-030000.tar.gz.age/")
    return names


def test_list_is_only_the_archives_this_destination_would_have_put_there(fake):
    names = populate(fake)
    fake.foreign = ["https://evil.example.net/postulo-backup-20261109-030000.tar.gz.age"]
    refs = DavBackup().list(CONFIG)
    assert sorted(ref.label for ref in refs) == names
    assert {ref.id for ref in refs} == {ROOT + name for name in names}
    assert all(ref.created_at is not None for ref in refs)
    assert set(fake.hosts) == {"cloud.example.org"}


def test_delete_removes_one_archive_conditionally_and_touches_nothing_else(fake):
    names = populate(fake)
    before = {p for p in fake.files}
    ref = next(r for r in DavBackup().list(CONFIG) if r.label == names[0])
    etag = fake.files[ref.id][1]
    DavBackup().delete(ref, CONFIG)
    assert before - set(fake.files) == {ROOT + names[0]}
    index = [m for m, _p in fake.log].index("DELETE")
    assert fake.headers[index]["if-match"] == etag
    assert [r.label for r in DavBackup().list(CONFIG)] == [names[1]]


def test_delete_never_touches_a_file_that_is_not_an_archive(fake):
    populate(fake)
    for bad in (ROOT + "holiday.jpg", "/remote.php/dav/files/ana/Other/x", ROOT + "sub/a"):
        with pytest.raises(Exception, match="not one of"):
            DavBackup().delete(BackupRef(id=bad), CONFIG)
    assert "holiday.jpg" in " ".join(fake.files)
    assert not [m for m, _p in fake.log if m == "DELETE"]


def test_delete_refuses_an_address_on_another_host(fake):
    with pytest.raises(Exception, match="outside"):
        DavBackup().delete(BackupRef(id="https://evil.example.net" + ROOT + NAME), CONFIG)
    assert set(fake.hosts) <= {"cloud.example.org"}


def test_delete_of_a_file_that_changed_since_it_was_looked_at_is_refused(fake, monkeypatch):
    populate(fake)
    ref = DavBackup().list(CONFIG)[0]
    monkeypatch.setattr(DavBackup, "_current_etag", lambda self, server, href: '"stale"')
    with pytest.raises(Exception, match="412"):
        DavBackup().delete(ref, CONFIG)
    assert ref.id in fake.files


def test_delete_of_one_that_is_already_gone_is_fine(fake):
    DavBackup().delete(BackupRef(id=ROOT + NAME), CONFIG)


# --------------------------------------------------------------------- the scope


def test_every_request_stays_in_the_folder_or_the_uploads_area(fake, tmp_path, small_chunks):
    send(tmp_path)
    DavBackup().list(CONFIG)
    for _method, path in fake.log:
        assert path.startswith((ROOT, UPLOADS)), path
    assert set(fake.hosts) == {"cloud.example.org"}


# ------------------------------------------------------------------------- test


def test_test_reads_the_folder_and_the_room_and_writes_nothing(fake):
    fake.quota = 3 * 1024**3
    result = DavBackup().test(CONFIG)
    assert result.ok
    assert "3.0 GB" in result.message
    assert {m for m, _p in fake.log} == {"PROPFIND"}


def test_test_says_so_when_the_folder_is_not_there(fake):
    result = DavBackup().test({**CONFIG, "url": BASE + "/remote.php/dav/files/ana/Nowhere/"})
    assert not result.ok
    assert "Nowhere" in result.message


def test_test_says_a_wrong_password_in_a_sentence(fake):
    result = DavBackup().test({**CONFIG, "password": "wrong"})
    assert not result.ok
    assert "password" in result.message


def test_the_names_the_destination_needs_are_on_the_surface():
    from postulo.plugins import api

    for name in ("BackupDestinationPlugin", "BackupArchive", "BackupManifest", "BackupRef"):
        assert name in api.__all__, name
