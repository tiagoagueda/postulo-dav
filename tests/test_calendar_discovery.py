# ruff: noqa: F811
"""Finding the collections (#13): what a run asks the server before it does any work."""

from __future__ import annotations

import pytest

from postulo_dav.sync import DavSync
from tests.test_dav import (  # noqa: F401 - fixtures are used by name
    application,
    company,
    connection,
    made,
    recruiter,
    server,
    user,
)

pytestmark = pytest.mark.django_db


def test_a_run_by_name_asks_for_the_principal_and_the_homes_once(made, connection, recruiter):
    full = connection.full_config
    assert DavSync().sync(connection, full).error == ""
    made.propfinds.clear()
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.summary() == "nothing to do"
    # Each half used to ask for the address as typed and for the homes itself; now the
    # address as typed (``/``) and the homes (of the principal) are asked once per run (#13).
    assert made.propfinds.count(("/", "0")) == 1, made.propfinds
    assert made.propfinds.count(("/dav/principals/ana/", "0")) == 1, made.propfinds
    assert len(made.propfinds) <= 10, made.propfinds
