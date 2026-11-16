"""Cards paired with contacts, departments, extra numbers and addresses, and the person's own (#8).

Against the in-memory server of ``test_dav``; the cards are made the way a phone makes them.
"""

# ruff: noqa: F811 - the fixtures of test_dav are imported by name and used by argument
from __future__ import annotations

import pytest
from postulo.core.memo import forget_decisions
from postulo.jobs.models import Company, Contact
from postulo.plugins import pairing
from postulo.plugins.api import (
    AddressRow,
    SyncLink,
    addresses_for,
    numbers_for,
    update_contact,
)

from postulo_dav import text
from postulo_dav.sync import DavSync

from .test_dav import (  # noqa: F401
    BASE,
    BOOK,
    application,
    cards,
    company,
    connection,
    made,
    phone_card,
    recruiter,
    server,
    sync,
    user,
)


@pytest.fixture(autouse=True)
def fresh_decisions():
    forget_decisions()
    yield
    forget_decisions()


def structure(user, on: bool) -> None:
    from postulo.plugins.models import PluginPolicy

    PluginPolicy.objects.update_or_create(
        plugin="employer-structure",
        person=user,
        defaults={"state": PluginPolicy.State.AVAILABLE if on else PluginPolicy.State.FORCED_OFF},
    )
    forget_decisions()


def mobile_card(uid: str, name: str) -> str:
    """A card as a phone makes it, with numbers a country would really give out."""
    return (
        phone_card(uid, name)
        .replace("+1 555 0100", "+351 912 345 678")
        .replace("+1 555 0199", "+351 213 456 789")
        .replace(
            "ADR;TYPE=home:;;1 Mesa Rd;Mesa;;;US", "ADR;TYPE=home:;;1 Mesa Rd;Mesa;NM;87001;US"
        )
    )


def card_body(made) -> str:
    (body,) = [b for h, b in cards(made).items() if h.endswith("phone.vcf")]
    return body


# --------------------------------------------------------------------- departments


def test_the_orgs_unit_is_the_contacts_department_and_survives_a_push(made, connection):
    made.enforce_uid = True
    made.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    assert sync(connection).pulled == 1
    contact = Contact.objects.get(name="Gordon Freeman")
    assert contact.company.name == "Black Mesa"
    assert contact.department.name == "Research"
    contact.role = "Director"
    contact.save()
    assert sync(connection).pushed == 1
    assert "ORG:Black Mesa;Research" in card_body(made)
    assert sync(connection).summary() == "nothing to do"


def test_a_department_chosen_here_becomes_the_unit_on_the_card(made, connection):
    from postulo.plugins.api import get_or_create_department

    made.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    sync(connection)
    contact = Contact.objects.get(name="Gordon Freeman")
    legal = get_or_create_department(contact.company, "Legal", actor=contact.owner)
    update_contact(contact, actor=contact.owner, department=legal)
    assert sync(connection).pushed == 1
    assert "ORG:Black Mesa;Legal" in card_body(made)
    assert "Research" not in card_body(made)


def test_a_company_changed_on_the_phone_leaves_the_old_companys_department(made, connection):
    made.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    sync(connection)
    body = card_body(made).replace("ORG:Black Mesa;Research", "ORG:Aperture")
    made.add(BOOK + "phone.vcf", body)
    report = sync(connection)
    contact = Contact.objects.get(name="Gordon Freeman")
    assert report.error == "" and contact.company.name == "Aperture"
    assert contact.department_id is None  # a pair the core refuses is never made


def test_a_postulo_card_for_a_contact_in_a_department_says_so(made, connection, recruiter):
    from postulo.plugins.api import get_or_create_department

    legal = get_or_create_department(recruiter.company, "Legal", actor=recruiter.owner)
    update_contact(recruiter, actor=recruiter.owner, department=legal)
    sync(connection)
    (body,) = cards(made).values()
    assert "ORG:Aperture Science;Legal" in body


def test_while_departments_are_off_nothing_of_one_travels(made, connection, user):
    structure(user, False)
    made.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    sync(connection)
    contact = Contact.objects.get(name="Gordon Freeman")
    assert contact.department_id is None
    contact.role = "Director"
    contact.save()
    sync(connection)
    assert "ORG:Black Mesa;Research" in card_body(made)  # the phone's unit is left as it is
    assert sync(connection).summary() == "nothing to do"


# ------------------------------------------------------ what the phone keeps and Postulo adds


def test_a_language_parameter_survives_a_push_that_changes_the_property(made, connection):
    made.enforce_uid = True
    made.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    sync(connection)
    contact = Contact.objects.get(name="Gordon Freeman")
    update_contact(contact, actor=contact.owner, name="Gordon M. Freeman")
    assert sync(connection).pushed == 1
    assert "FN;LANGUAGE=pt-BR:Gordon M. Freeman" in card_body(made)


def test_the_numbers_and_the_address_of_a_card_come_in_and_are_not_lost(made, connection):
    made.enforce_uid = True
    made.add(BOOK + "phone.vcf", mobile_card("phone-uid-1", "Gordon Freeman"))
    sync(connection)
    contact = Contact.objects.get(name="Gordon Freeman")
    assert sorted(n.number.replace(" ", "") for n in numbers_for(contact, contact.owner)) == [
        "+351213456789",
        "+351912345678",
    ]
    (address,) = addresses_for(contact, contact.owner)
    assert (address.street, address.postcode, address.country) == ("1 Mesa Rd", "87001", "US")
    # Nothing changed here: nothing is pushed, and nothing the phone keeps is touched.
    assert sync(connection).summary() == "nothing to do"
    body = card_body(made)
    assert "BDAY:19700101" in body and "X-VENDOR:keep me" in body


def test_a_second_address_and_number_made_here_are_added_to_the_card(made, connection):
    from postulo.plugins.api import PhoneRow, save_phone_numbers, save_postal_addresses

    made.enforce_uid = True
    made.add(BOOK + "phone.vcf", mobile_card("phone-uid-1", "Gordon Freeman"))
    sync(connection)
    contact = Contact.objects.get(name="Gordon Freeman")
    owner = contact.owner
    save_phone_numbers(
        contact, [*numbers_for(contact, owner), PhoneRow("+351 211 234 567", "work")], actor=owner
    )
    save_postal_addresses(
        contact,
        [
            *addresses_for(contact, owner),
            AddressRow("2 Rua", "1000-001", "Lisboa", "", "PT", "work"),
        ],
        actor=owner,
    )
    report = sync(connection)
    assert report.error == "" and report.pushed == 1
    body = card_body(made)
    assert "TEL;TYPE=voice:+351211234567" in body
    assert "ADR;TYPE=work:;;2 Rua;Lisboa;;1000-001;Portugal" in body
    # What was there stays, once.
    assert body.count("+351 213 456 789") == 1 and body.count("1 Mesa Rd") == 1
    assert "PHOTO;ENCODING=b;TYPE=JPEG:AAAA" in body and "VERSION:3.0" in body
    assert sync(connection).summary() == "nothing to do"


def test_a_postulo_card_carries_the_extra_numbers_and_addresses_in_the_books_version(
    made, connection, recruiter
):
    from postulo.plugins.api import PhoneRow, save_phone_numbers, save_postal_addresses

    made.collections[BOOK]["versions"] = ["3.0"]
    owner = recruiter.owner
    save_phone_numbers(
        recruiter,
        [PhoneRow("+351 912 345 678", "mobile"), PhoneRow("+351 211 234 567", "work")],
        actor=owner,
    )
    save_postal_addresses(
        recruiter,
        [AddressRow("1 Test St", "78701", "Austin", "TX", "US", "work", "", True)],
        actor=owner,
    )
    sync(connection)
    (body,) = cards(made).values()
    assert "VERSION:3.0" in body and "+351211234567" in body
    assert "ADR;TYPE=work:;;1 Test St;Austin;TX;78701;" in body
    assert "CC=" not in body and "PREF=" not in body
    assert text.text_to_card(body).addresses[0].municipality == "Austin"


# --------------------------------------------------------------------- the person's own card


def test_the_card_the_server_names_as_the_persons_own_is_the_one_offered_as_you(made, connection):
    made.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Gordon Freeman"))
    made.add(BOOK + "me.vcf", phone_card("me-1", "Ana Silva"))
    handler = made.handler

    def handler_with_address(request):
        body = request.content.decode() if request.content else ""
        if request.method == "PROPFIND" and "principal-address" in body:
            import httpx

            return httpx.Response(
                207,
                headers={"Content-Type": "application/xml"},
                content=(
                    '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
                    "<d:response><d:href>/dav/principals/ana/</d:href><d:propstat><d:prop>"
                    f"<c:principal-address><d:href>{BOOK}me.vcf</d:href></c:principal-address>"
                    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                    "</d:multistatus>"
                ),
            )
        return handler(request)

    made.handler = handler_with_address
    offered = {
        i.uid: i.kinds for i in DavSync().remote_items(connection.full_config, kind="contact")
    }
    assert "you" in offered["me-1"] and "you" not in offered["phone-uid-1"]
    assert [i.uid for i in DavSync().remote_items(connection.full_config, kind="you")] == ["me-1"]


def test_a_paired_own_card_is_patched_one_way_and_never_blanked(made, connection, user):
    made.enforce_uid = True
    user.first_name, user.last_name = "Ana", "Silva"
    user.save()
    made.add(BOOK + "phone.vcf", phone_card("phone-uid-1", "Ana S."))
    (item,) = DavSync().remote_items(
        connection.full_config, kind="you", key=(f"{BASE}{BOOK}", "phone-uid-1")
    )
    pairing.pair(connection, item, user)
    connection.config = {**connection.config, "send_own_card": True, "send_contacts": "none"}
    connection.save()
    report = DavSync().sync(connection, connection.full_config)
    assert report.error == "" and report.pushed == 1
    body = card_body(made)
    assert "FN;LANGUAGE=pt-BR:Ana Silva" in body and "EMAIL;TYPE=home:ana@example.org" in body
    for kept in (
        "UID:phone-uid-1",
        "BDAY:19700101",
        "ADR;TYPE=home:;;1 Mesa Rd;Mesa;;;US",
        "TITLE:Physicist",  # Postulo has no headline: a blank never replaces the phone's
        "ORG:Black Mesa;Research",
        "VERSION:3.0",
    ):
        assert kept in body
    # A change on the phone is not read back into the profile.
    made.add(
        BOOK + "phone.vcf", body.replace("TEL;TYPE=cell:+1 555 0100", "TEL;TYPE=cell:+1 555 9")
    )
    DavSync().sync(connection, connection.full_config)
    user.refresh_from_db()
    assert (user.first_name, user.email) == ("Ana", "ana@example.org")
    assert Contact.objects.count() == 0
    assert SyncLink.for_record(connection, user).uid == "phone-uid-1"
    assert Company.objects.count() == 0
