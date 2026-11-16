# ruff: noqa: F811 - the fixtures of test_dav are imported by name and used by argument
"""What goes out to the address book, per kind of record (#10).

The switches themselves (contacts all or none, companies, the own card, a kind switched off
leaving its cards) are in ``test_dav``; this holds what a company's and the person's own card
carry, and how a company's card made elsewhere is patched.
"""

from __future__ import annotations

import pytest
from postulo.core.memo import forget_decisions
from postulo.plugins import pairing
from postulo.plugins.api import (
    AddressRow,
    PhoneRow,
    numbers_for,
    save_phone_numbers,
    save_postal_addresses,
    update_company,
)

from postulo_dav.sync import DavSync

from .test_dav import (  # noqa: F401
    BASE,
    BOOK,
    cards,
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


def with_config(connection, **extra):
    connection.config = {**connection.config, **extra}
    connection.save()
    return connection.full_config


def dressed(company, user):
    save_phone_numbers(company, [PhoneRow("+351 213 456 789", "work")], actor=user)
    save_postal_addresses(
        company, [AddressRow("1 Rua Nova", "1000-001", "Lisboa", "", "PT", "work")], actor=user
    )
    update_company(company, actor=user, website="https://aperture.example.org", notes="My view")


def test_a_company_card_carries_its_numbers_addresses_and_note(made, connection, company, user):
    dressed(company, user)
    full = with_config(connection, send_companies=True, send_contacts="none")
    report = DavSync().sync(connection, full)
    assert report.error == "" and report.pushed == 1
    (body,) = cards(made).values()
    for line in (
        "KIND:org",
        "FN:Aperture Science",
        "URL:https://aperture.example.org",
        "TEL;TYPE=work,voice:+351213456789",
        "ADR;TYPE=work;PREF=1;CC=PT:;;1 Rua Nova;Lisboa;;1000-001;Portugal",
        "NOTE:My view",
    ):
        assert line in body, line
    assert DavSync().sync(connection, full).summary() == "nothing to do"


def test_a_company_card_in_a_vcard_3_book_has_no_vcard_4_parameters(
    made, connection, company, user
):
    made.collections[BOOK]["versions"] = ["3.0"]
    dressed(company, user)
    DavSync().sync(connection, with_config(connection, send_companies=True, send_contacts="none"))
    (body,) = cards(made).values()
    assert "X-ABShowAs:COMPANY" in body and "ADR;TYPE=work:;;1 Rua Nova" in body
    assert "CC=" not in body and "PREF=" not in body and "KIND:org" not in body


def test_a_changed_note_goes_out_to_a_card_postulo_wrote_and_is_never_read_back(
    made, connection, company, user
):
    dressed(company, user)
    full = with_config(connection, send_companies=True, send_contacts="none")
    DavSync().sync(connection, full)
    update_company(company, actor=user, notes="A new view")
    assert DavSync().sync(connection, full).pushed == 1
    (href,) = cards(made)
    assert "NOTE:A new view" in made.resources[href][0]
    made.add(href, made.resources[href][0].replace("A new view", "Typed on the phone"))
    DavSync().sync(connection, full)
    assert "Typed on the phone" not in company.note_for(user)


def company_item(connection, uid):
    (item,) = DavSync().remote_items(
        connection.full_config, kind="company", key=(f"{BASE}{BOOK}", uid)
    )
    return item


ELSEWHERE = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:acme-1\r\nX-ABShowAs:COMPANY\r\nFN:Aperture Labs\r\n"
    "ORG:Aperture Labs\r\nTITLE:Keep me\r\nEMAIL:keep@aperture.example.org\r\n"
    "TEL;TYPE=work:+351 211 234 567\r\nNOTE:Their note\r\nEND:VCARD\r\n"
)


def test_a_company_card_made_elsewhere_keeps_its_own_properties_when_the_company_changes(
    made, connection, company, user
):
    made.enforce_uid = True
    made.add(BOOK + "acme.vcf", ELSEWHERE)
    pairing.pair(connection, company_item(connection, "acme-1"), company)
    report = DavSync().sync(connection, with_config(connection, send_contacts="none"))
    assert report.error == "" and report.pulled == 1
    company.refresh_from_db()
    assert company.name == "Aperture Labs"
    # The number on the card is the company's now.
    assert [n.number.replace(" ", "") for n in numbers_for(company, user)] == ["+351211234567"]
    update_company(company, actor=user, website="https://labs.example.org")
    save_phone_numbers(
        company,
        [*numbers_for(company, user), PhoneRow("+351 213 456 789", "work")],
        actor=user,
    )
    report = DavSync().sync(connection, connection.full_config)
    assert report.error == "" and report.pushed == 1
    body, _etag = made.resources[BOOK + "acme.vcf"]
    for kept in (
        "UID:acme-1",
        "VERSION:3.0",
        "X-ABShowAs:COMPANY",
        "TITLE:Keep me",
        "EMAIL:keep@aperture.example.org",
        "TEL;TYPE=work:+351 211 234 567",
        "NOTE:Their note",
        "URL:https://labs.example.org",
        "TEL;TYPE=voice:+351213456789",
    ):
        assert kept in body, kept
    assert "My view" not in body
    assert DavSync().sync(connection, connection.full_config).summary() == "nothing to do"


def test_the_own_card_carries_the_primary_address_and_still_is_never_read_back(
    made, connection, user
):
    from postulo.core.formsets import give_owner
    from postulo.core.models import PostalAddress

    user.first_name, user.last_name = "Ana", "Silva"
    user.save()
    address = PostalAddress(
        holder=user.profile,
        kind="home",
        street="5 Rua Velha",
        postcode="1000-002",
        municipality="Lisboa",
        country="PT",
        is_primary=True,
    )
    give_owner(address, user.profile)
    address.save()
    full = with_config(connection, send_own_card=True, send_contacts="none")
    assert DavSync().sync(connection, full).pushed == 1
    (body,) = cards(made).values()
    assert "ADR;TYPE=home;PREF=1;CC=PT:;;5 Rua Velha;Lisboa;;1000-002;Portugal" in body
    assert DavSync().sync(connection, full).summary() == "nothing to do"
