# ruff: noqa: F811
"""Finding the collections (#13): what a run asks the server before it does any work."""

from __future__ import annotations

import pytest
from postulo.applications.services import schedule_interview
from postulo.plugins import registry, syncing

from postulo_dav.dav import ADDRESSBOOK, CALENDAR, DavError
from postulo_dav.sync import DavSync
from tests.test_dav import (  # noqa: F401 - fixtures are used by name
    CONFIG,
    application,
    cards,
    choose_hrefs,
    company,
    connection,
    events,
    made,
    next_week,
    recruiter,
    server,
    sync,
    user,
)

pytestmark = pytest.mark.django_db

NO_BOOKS = "The server does not name an address book home."
NO_CALENDARS = "The server does not name a calendar home."


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


# ------------------------------------------- a server with one kind of collection (#70)


@pytest.fixture
def calendars_only(made):
    """A server that keeps calendars and names no address-book home."""
    made.homes = (CALENDAR,)
    return made


def test_look_on_the_server_offers_the_calendars_where_there_are_no_address_books(
    calendars_only,
):
    found = DavSync().discover(CONFIG)["collections"]
    assert {(option.group, option.label) for option in found} == {
        ("Calendars", "Personal"),
        ("Calendars", "Postulo"),
    }
    assert not [m for m, _p in calendars_only.log if m != "PROPFIND"]


def test_a_connection_that_chose_only_a_calendar_there_syncs_with_no_error(
    calendars_only, connection, recruiter, application
):
    schedule_interview(application, kind="phone", starts_at=next_week())
    offered = DavSync().discover(CONFIG)["collections"]
    chosen = {option.value: "readwrite" for option in offered if option.label == "Postulo"}
    full = choose_hrefs(connection, chosen)
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.pushed == 1, report.summary()
    assert len(events(calendars_only)) == 1 and not cards(calendars_only)


def test_a_connection_by_name_syncs_the_calendar_and_notes_the_missing_address_books(
    calendars_only, connection, recruiter, application
):
    schedule_interview(application, kind="phone", starts_at=next_week())
    report = sync(connection)
    # An error on every run put the connection on the failing wait although its calendar
    # half had synced. It is a note now, and the run is one that worked.
    assert report.error == "" and report.pushed == 1, report.summary()
    assert NO_BOOKS in report.summary()
    assert len(events(calendars_only)) == 1 and not cards(calendars_only)
    registry.plugins("sync", refresh=True)
    assert syncing.run_syncs() == (1, 0)
    connection.refresh_from_db()
    assert connection.last_error == ""


def test_test_finds_the_calendar_and_says_there_are_no_address_books(calendars_only):
    result = DavSync().test(CONFIG)
    assert result.ok, result.message
    assert result.message == f"Found the calendar “Postulo” (read and write). {NO_BOOKS}"


def test_a_server_with_contacts_only_syncs_them_and_notes_the_missing_calendars(
    made, connection, recruiter, application
):
    made.homes = (ADDRESSBOOK,)
    schedule_interview(application, kind="phone", starts_at=next_week())
    found = DavSync().discover(CONFIG)["collections"]
    assert {option.group for option in found} == {"Address books"}
    report = sync(connection)
    assert report.error == "" and report.pushed == 1, report.summary()
    assert NO_CALENDARS in report.summary()
    assert len(cards(made)) == 1 and not events(made)


def test_a_server_with_neither_home_is_the_error_it_was(made, connection, recruiter):
    made.homes = ()
    with pytest.raises(DavError, match="address book home"):
        DavSync().discover(CONFIG)
    result = DavSync().test(CONFIG)
    assert not result.ok and result.message == NO_BOOKS
    assert sync(connection).error == f"{NO_BOOKS} {NO_CALENDARS}"
    assert not cards(made) and not events(made)
