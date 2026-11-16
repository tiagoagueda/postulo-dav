"""The WebDAV store (#5): a copy of each document handed over, in the person's own folder."""

from __future__ import annotations

import datetime as dt
import io

import httpx
import pytest
from postulo.plugins import registry
from postulo.plugins.api import ConnectionUnusable, DocumentMetadata, StorePlugin

from postulo_dav import dav as dav_module
from postulo_dav import store as store_module
from postulo_dav.store import DavStore, layout_path
from tests.fakefiles import BASE, CONFIG, ROOT, FakeFiles, sha256

PDF = b"%PDF-1.7 a curriculum vitae " * 40
WHEN = dt.datetime(2026, 11, 3, 23, 30, tzinfo=dt.UTC)


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


def metadata(**changes) -> DocumentMetadata:
    values = {
        "kind": "cv",
        "kind_label": "Curriculum vitae",
        "origin": "render",
        "title": "My CV",
        "filename": "cv-ana.pdf",
        "content_type": "application/pdf",
        "created_at": WHEN,
        "checksum": sha256(PDF),
        "size": len(PDF),
        "company": "Acme",
        "role": "Data engineer",
        "sent_on": dt.date(2026, 11, 5),
        "created_on": dt.date(2026, 11, 3),
    }
    values.update(changes)
    return DocumentMetadata(**values)


def put(config=None, meta=None, data=PDF):
    return DavStore().put(None, io.BytesIO(data), meta or metadata(), config or CONFIG, None)


# ------------------------------------------------------------------- the plugin


def test_it_is_a_store_and_is_registered_under_its_entry_point():
    store = DavStore()
    assert isinstance(store, StorePlugin)
    assert store.kind == "store"
    assert store.name == "webdav"
    names = {plugin.name for plugin in registry.plugins("store")}
    assert "webdav" in names


def test_it_asks_for_an_address_a_user_and_a_secret_password():
    fields = {spec.name: spec for spec in DavStore().config_fields()}
    assert {"url", "username", "password", "layout"} <= set(fields)
    assert fields["password"].secret
    assert fields["layout"].default == "company"


def test_a_password_will_not_go_over_plain_http():
    problems = DavStore().validate({**CONFIG, "url": "http://cloud.example.org/dav/"})
    assert "url" in problems
    assert DavStore().validate(CONFIG) == {}


def test_a_summary_masks_the_password():
    assert DavStore().summary(CONFIG).startswith(BASE)
    assert "app-pw" not in DavStore().summary(CONFIG)


# ----------------------------------------------------------------- where it goes


def test_by_company_the_file_lands_under_company_role_and_the_day_it_was_sent():
    assert layout_path(metadata(), "company") == (
        ["Acme", "Data engineer"],
        "2026-11-05 cv-ana.pdf",
    )


def test_without_an_application_the_folder_is_the_kind_and_the_day_is_the_creation_day():
    meta = metadata(company="", role="", sent_on=None)
    assert layout_path(meta, "company") == (["Curriculum vitae"], "2026-11-03 cv-ana.pdf")


def test_a_company_without_a_role_has_one_level():
    assert layout_path(metadata(role=""), "company")[0] == ["Acme"]


def test_flat_has_no_folders_and_a_descriptive_name():
    parts, name = layout_path(metadata(), "flat")
    assert parts == []
    assert name == "2026-11-05 Acme - Data engineer - Curriculum vitae - cv-ana.pdf"


def test_a_part_is_made_safe_for_a_path():
    meta = metadata(company="../../etc/passwd", role='a/b\\c:d*?"<>|\x00e', filename="../x.pdf")
    parts, name = layout_path(meta, "company")
    for part in [*parts, name]:
        assert "/" not in part and "\\" not in part and "\x00" not in part
        assert part not in ("", ".", "..")
        assert not part.startswith(".")
    assert name.endswith("x.pdf")


def test_a_very_long_name_is_cut_and_keeps_its_extension():
    parts, name = layout_path(metadata(company="C" * 500, filename="d" * 500 + ".pdf"), "company")
    assert len(parts[0]) <= 80
    assert len(name) <= 150
    assert name.endswith(".pdf")


# ------------------------------------------------------------------- the copy


def test_a_document_lands_in_the_folder_and_the_folders_are_made_one_level_at_a_time(fake):
    ref = put()
    assert fake.stored("Acme/Data engineer/2026-11-05 cv-ana.pdf") == PDF
    assert ref.store == "webdav"
    assert ref.id.endswith("/Acme/Data%20engineer/2026-11-05%20cv-ana.pdf")
    assert ref.id.startswith(ROOT)
    made = [path for method, path in fake.log if method == "MKCOL"]
    assert made == [ROOT + "Acme/", ROOT + "Acme/Data engineer/"]


def test_a_second_document_makes_no_folder_that_is_there(fake):
    put()
    fake.log.clear()
    put(meta=metadata(filename="letter.pdf"))
    # Asked again and answered 405, which is "it is there": not an error, and nothing new made.
    assert len([1 for m, _p in fake.log if m == "MKCOL"]) == 2
    assert len(fake.folders) == len(set(fake.folders))
    assert len(fake.under(ROOT)) == 2


def test_flat_puts_the_file_straight_in_the_folder(fake):
    put({**CONFIG, "layout": "flat"})
    assert fake.stored("2026-11-05 Acme - Data engineer - Curriculum vitae - cv-ana.pdf") == PDF
    assert not [1 for m, _p in fake.log if m == "MKCOL"]


def test_it_never_overwrites(fake):
    put()
    assert any(h.get("if-none-match") == "*" for h in fake.headers if h.get("content-length"))


def test_a_clash_with_another_file_gets_a_suffix(fake):
    name = "Acme/Data engineer/2026-11-05 cv-ana.pdf"
    put()
    fake.files[ROOT + name] = (b"somebody else's", '"mine"')
    ref = put()
    assert fake.stored(name) == b"somebody else's"
    assert fake.stored("Acme/Data engineer/2026-11-05 cv-ana (2).pdf") == PDF
    assert "%282%29" in ref.id


def test_a_retry_after_a_lost_reply_leaves_one_file(fake):
    fake.lose_reply = 1
    with pytest.raises(Exception, match="500"):
        put()
    ref = put()  # the retry: the file is there, byte for byte
    assert len(fake.under(ROOT)) == 1
    assert ref.id.endswith("2026-11-05%20cv-ana.pdf")


def test_a_retry_whose_file_differs_is_not_taken_for_the_same(fake):
    fake.lose_reply = 1
    with pytest.raises(Exception, match="500"):
        put(data=b"a" * 100, meta=metadata(checksum=sha256(b"a" * 100), size=100))
    put()  # another document with the same name
    assert len(fake.under(ROOT)) == 2


def test_a_full_folder_is_reported_and_retried(fake):
    fake.full = True
    with pytest.raises(Exception, match="full") as info:
        put()
    assert not isinstance(info.value, ConnectionUnusable)


def test_a_refused_password_switches_the_connection_off(fake):
    with pytest.raises(ConnectionUnusable):
        put({**CONFIG, "password": "wrong"})


def test_a_server_that_says_401_at_the_upload_does_too(fake):
    fake.refuse = True
    with pytest.raises(ConnectionUnusable):
        put()


def test_a_forbidden_write_is_an_ordinary_failure(fake):
    fake.forbid_writes = True
    with pytest.raises(Exception) as info:
        put()
    assert not isinstance(info.value, ConnectionUnusable)


def test_a_missing_chosen_folder_is_not_made_up(fake):
    with pytest.raises(Exception, match="not there"):
        put({**CONFIG, "url": BASE + "/remote.php/dav/files/ana/Nowhere/"})
    assert not [1 for m, _p in fake.log if m == "MKCOL"]


# ------------------------------------------------------------------ the scope


def test_every_request_stays_in_the_folder_and_on_the_host(fake):
    put()
    assert set(fake.hosts) == {"cloud.example.org"}
    for _method, path in fake.log:
        assert path.startswith(ROOT), path


def test_a_listing_that_names_an_address_elsewhere_is_not_followed(fake):
    fake.foreign = ["https://evil.example.net/x.pdf", "/remote.php/dav/files/ana/Other/y.pdf"]
    put()
    assert set(fake.hosts) == {"cloud.example.org"}
    assert not [p for _m, p in fake.log if p.startswith("/remote.php/dav/files/ana/Other")]


def test_a_filename_cannot_climb_out_of_the_folder(fake):
    put(meta=metadata(filename="../../../secret.pdf", company="", role=""))
    for _method, path in fake.log:
        assert ".." not in path.split("/")
        assert path.startswith(ROOT)


# ------------------------------------------------------------------------ test


def test_test_reads_and_writes_nothing(fake):
    fake.quota = 5 * 1024 * 1024
    result = DavStore().test(CONFIG)
    assert result.ok
    assert "5.0 MB" in result.message
    assert {m for m, _p in fake.log} <= {"PROPFIND"}


def test_test_says_so_when_the_folder_is_not_there(fake):
    result = DavStore().test({**CONFIG, "url": BASE + "/remote.php/dav/files/ana/Nowhere/"})
    assert not result.ok
    assert "Nowhere" in result.message


def test_test_says_a_wrong_password_in_a_sentence(fake):
    result = DavStore().test({**CONFIG, "password": "wrong"})
    assert not result.ok
    assert "password" in result.message


def test_the_store_module_has_a_store_in_it():
    assert store_module.DavStore.name == "webdav"


def test_the_names_the_store_needs_are_on_the_surface():
    from postulo.plugins import api

    for name in ("StorePlugin", "DocumentMetadata", "ExternalRef", "ConnectionUnusable"):
        assert name in api.__all__, name
