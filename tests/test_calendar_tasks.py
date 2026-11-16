# ruff: noqa: F811
"""Tasks in a task list (#12): what the plugin keeps of a task it did not write."""

from __future__ import annotations

import pytest
from postulo.plugins.api import complete_reminder

from postulo_dav.sync import DavSync
from tests.test_dav import (  # noqa: F401 - fixtures are used by name
    BASE,
    CONFIG,
    company,
    connection,
    listed,
    made,
    make_reminder,
    only_task,
    server,
    user,
    with_tasks,
)

pytestmark = pytest.mark.django_db


def test_a_language_parameter_on_a_task_made_elsewhere_survives_a_push(listed, connection, user):
    reminder = make_reminder(user)
    full = with_tasks(connection)
    DavSync().sync(connection, full)
    href, body = only_task(listed)
    phone = body.replace(
        "END:VTODO",
        "DESCRIPTION;LANGUAGE=pt-BR:Ligar ao Cave\r\nLOCATION;LANGUAGE=pt-BR:Lisboa\r\n"
        "CATEGORIES;LANGUAGE=pt-BR:trabalho\r\nEND:VTODO",
    )
    listed.add(href, phone)
    DavSync().sync(connection, full)  # the phone's version is read, nothing differs
    complete_reminder(reminder, actor="")
    assert DavSync().sync(connection, full).pushed == 1
    written = only_task(listed)[1]
    assert "STATUS:COMPLETED" in written
    for kept in (
        "DESCRIPTION;LANGUAGE=pt-BR:Ligar ao Cave",
        "LOCATION;LANGUAGE=pt-BR:Lisboa",
        "CATEGORIES;LANGUAGE=pt-BR:trabalho",
    ):
        assert kept in written
