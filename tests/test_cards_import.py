# ruff: noqa: F811 - the fixtures of test_dav are imported by name and used by argument
"""Importing one card: a company's numbers, address and notes, a contact's rows (#9)."""

from __future__ import annotations

import pytest
from postulo.core.memo import forget_decisions
from postulo.plugins.api import SyncLink, addresses_for, numbers_for

from .test_dav import (  # noqa: F401
    BASE,
    BOOK,
    company,
    connection,
    made,
    server,
    user,
)


@pytest.fixture(autouse=True)
def fresh_decisions():
    forget_decisions()
    yield
    forget_decisions()


def import_card(connection, uid, kind, into=None, pair=False):
    from postulo.plugins.testing import create_by_hand

    return create_by_hand(
        connection, collection=f"{BASE}{BOOK}", uid=uid, kind=kind, into=into, pair=pair
    )


ACME = (
    "BEGIN:VCARD\r\nVERSION:4.0\r\nUID:acme-1\r\nKIND:org\r\nFN:Acme Corp\r\n"
    "URL:https://acme.example.org\r\nNOTE:A good place\r\n"
    "TEL:+351 213 456 789\r\nEMAIL:hello@acme.example.org\r\n"
    "ADR;TYPE=work:;;1 Rua Nova;Lisboa;;1000-001;Portugal\r\nEND:VCARD\r\n"
)


def test_a_company_card_gives_name_website_notes_place_number_and_address(made, connection, user):
    made.add(BOOK + "acme.vcf", ACME)
    made.log.clear()
    company = import_card(connection, "acme-1", "company")
    assert company.name == "Acme Corp" and company.website == "https://acme.example.org"
    assert company.location.startswith("Lisboa")  # the core words it: "Lisboa, Portugal"
    assert "A good place" in company.note_for(user)
    assert "hello@acme.example.org" in company.note_for(user)  # a company has no email
    assert [n.number.replace(" ", "") for n in numbers_for(company, user)] == ["+351213456789"]
    (address,) = addresses_for(company, user)
    assert (address.street, address.postcode, address.municipality, address.country) == (
        "1 Rua Nova",
        "1000-001",
        "Lisboa",
        "PT",
    )
    assert not [m for m, _p in made.log if m not in ("PROPFIND", "GET", "REPORT")]
    assert not SyncLink.objects.filter(connection=connection)


def test_a_card_fills_only_the_blanks_of_a_company_and_adds_what_it_lacks(
    made, connection, company, user
):
    from postulo.plugins.api import update_company

    update_company(
        company, actor=user, website="https://mine.example.org", location="Porto", notes="Mine"
    )
    made.add(BOOK + "acme.vcf", ACME)
    filled = import_card(connection, "acme-1", "company", into=company)
    assert filled.pk == company.pk and filled.name == "Aperture Science"
    assert filled.website == "https://mine.example.org" and filled.location == "Porto"
    assert filled.note_for(user).startswith("Mine") and "A good place" not in filled.note_for(user)
    assert len(numbers_for(filled, user)) == 1
    # Its own place stays an address of its own, and the card's is added beside it.
    assert {a.municipality for a in addresses_for(filled, user)} == {"Porto", "Lisboa"}
    again = import_card(connection, "acme-1", "company", into=filled)
    assert len(numbers_for(again, user)) == 1, "a second import adds nothing twice"
    assert again.note_for(user).count("hello@acme.example.org") == 1


def test_a_contact_card_brings_its_numbers_addresses_links_and_department(made, connection):
    made.add(
        BOOK + "g.vcf",
        "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:g-1\r\nFN:Gordon Freeman\r\nORG:Black Mesa;Research\r\n"
        "TEL;TYPE=cell:+351 912 345 678\r\nTEL;TYPE=work:+351 213 456 789\r\n"
        "URL:https://www.linkedin.com/in/gordon\r\nURL:https://gordon.example.org\r\n"
        "ADR;TYPE=home:;;1 Mesa Rd;Mesa;NM;87001;US\r\nEND:VCARD\r\n",
    )
    gordon = import_card(connection, "g-1", "contact")
    assert gordon.company.name == "Black Mesa" and gordon.department.name == "Research"
    owner = gordon.owner
    assert len(numbers_for(gordon, owner)) == 2
    (address,) = addresses_for(gordon, owner)
    assert address.postcode == "87001"
    from postulo.plugins.api import links_for

    assert {row.url for row in links_for(gordon, owner)} == {
        "https://www.linkedin.com/in/gordon",
        "https://gordon.example.org",
    }
