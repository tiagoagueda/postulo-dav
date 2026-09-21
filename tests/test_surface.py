"""This plugin imports `postulo.plugins.api`, and nothing else from Postulo.

`postulo.plugins.api` is a promise: those names keep working across a minor release, and a
change to one is a deprecation entry before it is a removal. `postulo.plugins.base`,
`postulo.core`, `postulo.applications` and the rest are this month's internals and may be
renamed in a patch release without a word — so a plugin that imports them is a plugin that
breaks on somebody else's Tuesday, with nobody warned.

This repository had no such check, and it showed: a two-way sync needs the records
themselves, the calls that move them so the timeline says so, and the link table that
remembers which twin is whose, and none of that was on the surface, so `sync.py` carried
nine imports past it and a paragraph counting what they risked. postulo/postulo#229 put
them on the surface and published the check; this file is the check, imported rather than
copied, so that it improves when the core's does.

Only `src/` is measured. The tests are not shipped and are entitled to reach into Postulo
to set a world up, which is a different promise from the one the plugin makes at run time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from postulo.plugins.testing import SURFACE, assert_imports_only_the_surface, imports_of

PACKAGE = "postulo_dav"
SOURCE = Path(__file__).resolve().parents[1] / "src" / PACKAGE


def test_there_is_something_to_check():
    """A sanity check on the check: an empty package would make everything below pass."""
    assert len(list(SOURCE.rglob("*.py"))) >= 4


def test_the_plugin_imports_only_the_surface():
    assert_imports_only_the_surface(SOURCE, package=PACKAGE)


def test_the_surface_is_what_this_plugin_actually_uses():
    """The other half: the surface is imported, rather than merely not reached past."""
    assert imports_of(SOURCE, package=PACKAGE) == {SURFACE}


@pytest.mark.parametrize(
    "name",
    [
        "Contact",
        "Interview",
        "InterviewOutcome",
        "LinkKind",
        "SyncLink",
        "calendar_status",
        "event_lines",
        "get_or_create_company",
        "phone_number_is_taken",
        "primary_phone_number",
        "primary_web_link",
        "record_event",
        "reschedule_interview",
        "save_phone_number",
        "save_web_link",
        "settle_interview",
    ],
)
def test_every_name_this_sync_needs_is_promised(name: str):
    """Not merely importable: on `__all__`, which is what the promise is made about.

    A name that resolves today because it happens to be reachable is not a name that keeps
    working across a release. These sixteen are why this plugin could not be written against
    the surface before postulo/postulo#229.
    """
    from postulo.plugins import api

    assert name in api.__all__, name
