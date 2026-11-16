# ruff: noqa: F811
"""Whole days on the calendar (#11): deadlines, closing dates and answer dates, one way."""

from __future__ import annotations

import datetime as dt
import re

import pytest
from django.utils import timezone
from postulo.plugins.api import __all__ as surface

from postulo_dav.sync import WHOLE_DAY_HREF, DavSync
from tests.test_dav import (  # noqa: F401 - fixtures are used by name
    CALENDAR_PATH,
    CONFIG,
    application,
    company,
    connection,
    events,
    made,
    server,
    user,
    with_config,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def public_address(settings):
    settings.POSTULO_PUBLIC_URL = "https://postulo.example.org"


def days(fake):
    return {h: b for h, b in events(fake).items() if WHOLE_DAY_HREF.search(h)}


def with_days(connection, **extra):
    return with_config(connection, whole_days=True, **extra)


def test_the_core_gives_the_names_the_plugin_takes():
    assert {"calendar_items", "day_lines"} <= set(surface)


def test_no_whole_day_is_written_unless_the_switch_is_on(made, connection, application):
    application.deadline = timezone.localdate() + dt.timedelta(days=5)
    application.save()
    assert DavSync().sync(connection, connection.full_config).error == ""
    assert not days(made)


def test_a_deadline_goes_out_as_an_all_day_event_and_a_second_run_does_nothing(
    made, connection, application
):
    day = timezone.localdate() + dt.timedelta(days=5)
    application.deadline = day
    application.save()
    full = with_days(connection)
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.pushed == 1
    ((href, body),) = days(made).items()
    assert href.startswith(CALENDAR_PATH)
    assert f"DTSTART;VALUE=DATE:{day:%Y%m%d}" in body
    assert f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}" in body
    assert "TRANSP:TRANSPARENT" in body and "Deadline:" in body
    made.log.clear()
    assert DavSync().sync(connection, full).summary() == "nothing to do"
    assert not [m for m, _p in made.log if m in ("PUT", "DELETE")]


def test_a_change_here_is_written_and_one_made_on_the_phone_is_written_over(
    made, connection, application
):
    application.deadline = timezone.localdate() + dt.timedelta(days=5)
    application.save()
    full = with_days(connection)
    DavSync().sync(connection, full)
    ((href, body),) = days(made).items()
    made.add(href, body.replace("SUMMARY:", "SUMMARY:Moved on the phone ").replace("Deadline", "x"))
    assert DavSync().sync(connection, full).pushed == 1
    assert "Moved on the phone" not in made.resources[href][0]
    later = timezone.localdate() + dt.timedelta(days=9)
    application.deadline = later
    application.save()
    DavSync().sync(connection, full)
    assert f"DTSTART;VALUE=DATE:{later:%Y%m%d}" in made.resources[href][0]


def test_a_whole_day_that_is_gone_is_deleted_with_if_match_and_nothing_else_is(
    made, connection, application
):
    application.deadline = timezone.localdate() + dt.timedelta(days=5)
    application.save()
    full = with_days(connection)
    DavSync().sync(connection, full)
    mine = CALENDAR_PATH + "phone-1.ics"
    made.add(
        mine,
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:phone-1\r\nDTSTART;VALUE=DATE:20991201\r\n"
        "SUMMARY:Mine\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
    )
    application.deadline = None
    application.save()
    made.delete_conditions.clear()
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.removed == 1
    assert not days(made) and mine in made.resources
    assert made.delete_conditions and all(made.delete_conditions)


def test_a_whole_day_is_never_offered_for_pairing_nor_taken_for_an_interview(
    made, connection, application
):
    application.deadline = timezone.localdate() + dt.timedelta(days=5)
    application.save()
    DavSync().sync(connection, with_days(connection))
    ((_href, body),) = days(made).items()
    assert re.search(r"UID:[0-9a-f-]{36}@postulo", body)
    items = DavSync().remote_items(with_days(connection), "", "interview", None)
    assert items == []


def test_read_only_writes_no_whole_day(made, connection, application):
    application.deadline = timezone.localdate() + dt.timedelta(days=5)
    application.save()
    assert DavSync().sync(connection, with_days(connection, read_only=True)).error == ""
    assert not days(made)


def test_without_a_public_address_the_run_says_why_and_deletes_nothing(
    made, connection, application, settings
):
    application.deadline = timezone.localdate() + dt.timedelta(days=5)
    application.save()
    full = with_days(connection)
    DavSync().sync(connection, full)
    settings.POSTULO_PUBLIC_URL = ""
    made.log.clear()
    report = DavSync().sync(connection, full)
    assert report.error == "" and any("public address" in str(n) for n in report.notes)
    assert days(made) and not [m for m, _p in made.log if m in ("PUT", "DELETE")]
