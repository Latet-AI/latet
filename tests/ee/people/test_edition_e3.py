"""The edition's own E3 lines without PostgreSQL (R17, R30): /me and the license's standing never raise when the
people's roles cannot be read, and then name no role, no action and no page a role decides, nor the headcount."""

import logging

from latet.ee.access import Access
from latet.state import IdentityRef

from ..editions import memory_edition
from .server_support import NOW

EMILY = IdentityRef("u_emilycartr", "Emily Carter", "person")


class Unanswered:
    """People whose store does not answer (PostgreSQL or Valkey down): its words name an address."""

    def roles_of(self, identity_id):
        raise ConnectionError("connection to 10.0.0.5 refused")

    def account(self, identity_id):
        raise ConnectionError("connection to 10.0.0.5 refused")

    def close(self):
        """The edition closes it first (E3's release gates, fix round 2)."""


def test_me_answers_when_the_peoples_roles_cannot_be_read(monkeypatch, tmp_path, licensed, caplog):
    edition, store, source = memory_edition(monkeypatch, tmp_path, clock=lambda: NOW, count_users=lambda: 120)
    try:
        store.write(licensed.make())  # every feature, roles among them: the roles decide, and cannot be read
        source.said = {"sources": [], "problems": [{"level": "fail", "text": "unreadable"}]}  # the Policy page weighed
        edition.people = Unanswered()  # type: ignore[assignment]
        edition.access = Access(edition.people, edition.licensing)  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger="latet.ee.edition"):
            me = edition.me(EMILY)
        assert (me["roles"], me["actions"], me["pages"]) == ([], [], [])  # only the pages PAGES does not name: none
        assert me["license"]["users_in_use"] is None
        told = edition.told(edition.standing(), EMILY.id)
        assert told.users_in_use is None and told.over_since is None
        assert "ConnectionError" in caplog.text and "10.0.0.5" not in caplog.text
        assert "linked" not in me  # whether they are linked to the provider cannot be read either: nothing is said
    finally:
        edition.close()


def test_me_says_whether_a_person_is_linked_to_the_provider_and_nothing_for_anyone_else(
    monkeypatch, tmp_path, licensed
):
    from types import SimpleNamespace

    class People:
        """Emily's account holds a subject; Daniel's holds none; Priya has none at all."""

        def roles_of(self, identity_id):
            return frozenset()

        def account(self, identity_id):
            return {
                "u_emilycartr": SimpleNamespace(subject="https://login.acme.test 5d1e"),
                "u_danielokaf": SimpleNamespace(subject=None),
            }.get(identity_id)

        def close(self):
            """The edition closes it first."""

    edition, store, source = memory_edition(monkeypatch, tmp_path, clock=lambda: NOW, count_users=lambda: 3)
    try:
        store.write(licensed.make())
        source.said = {"sources": [], "problems": []}
        assert "linked" not in edition.me(EMILY)  # the tests' in-memory edition has no people: nothing to say
        edition.people = People()  # type: ignore[assignment]
        edition.access = Access(edition.people, edition.licensing)  # type: ignore[arg-type]
        assert edition.me(EMILY)["linked"] is True
        assert edition.me(IdentityRef("u_danielokaf", "Daniel Okafor", "person"))["linked"] is False
        assert edition.me(IdentityRef("u_priyashahx", "Priya Shah", "person"))["linked"] is False  # no account row yet
        assert "linked" not in edition.me(IdentityRef("u_billingcix", "Billing CI", "key"))  # a key has no account
        assert "linked" not in edition.me(None)
        assert "https://login.acme.test" not in str(edition.me(EMILY))  # never what links them
    finally:
        edition.close()


def test_me_reads_no_link_while_the_license_serves_no_sso(monkeypatch, tmp_path, licensed):
    """No sign-in to link without sso: /me says nothing of a link and reads nothing for it, at each visit."""
    from types import SimpleNamespace

    class People:
        def __init__(self):
            self.asked: list[str] = []

        def roles_of(self, identity_id):
            return frozenset()

        def account(self, identity_id):
            self.asked.append(identity_id)
            return SimpleNamespace(subject=None)

        def close(self):
            """The edition closes it first."""

    edition, store, source = memory_edition(monkeypatch, tmp_path, clock=lambda: NOW, count_users=lambda: 3)
    try:
        store.write(licensed.make(features=["roles", "locked-rules"]))
        source.said = {"sources": [], "problems": []}
        people = People()
        edition.people = people  # type: ignore[assignment]
        edition.access = Access(people, edition.licensing)  # type: ignore[arg-type]
        assert "linked" not in edition.me(EMILY) and people.asked == []
        store.write(licensed.make())  # every feature, sso among them
        assert edition.me(EMILY)["linked"] is False and people.asked == [EMILY.id]
    finally:
        edition.close()


def test_me_reads_the_persons_roles_once(monkeypatch, tmp_path, licensed):
    from latet.ee.roles import Role

    class People:
        def __init__(self):
            self.asked: list[str] = []

        def roles_of(self, identity_id):
            self.asked.append(identity_id)
            return frozenset({Role.USER, Role.ADMIN, Role.SECURITY})

    edition, store, source = memory_edition(monkeypatch, tmp_path, clock=lambda: NOW, count_users=lambda: 120)
    try:
        store.write(licensed.make())
        source.said = {"sources": [], "problems": []}
        people = People()
        edition.access = Access(people, edition.licensing)  # type: ignore[arg-type]
        me = edition.me(EMILY)
        assert people.asked == [EMILY.id]  # roles, actions, the Policy page, the pages and the headcount from one read
        assert me["roles"] == ["admin", "security", "user"] and "policy.edit" in me["actions"]
        assert {"license", "policy", "people", "sign-in"} <= set(me["pages"]) and me["license"]["users_in_use"] == 120
        edition.me(EMILY)
        assert people.asked == [EMILY.id, EMILY.id]  # read again at the next /me: nothing kept between them
    finally:
        edition.close()


def test_the_licenses_administrators_are_told_its_headcount_and_nobody_else(monkeypatch, tmp_path, licensed):
    from latet.ee.roles import Role

    class People:
        def roles_of(self, identity_id):
            return frozenset({Role.USER, Role.ADMIN}) if identity_id == EMILY.id else frozenset({Role.USER})

    edition, store, _ = memory_edition(monkeypatch, tmp_path, clock=lambda: NOW, count_users=lambda: 120)
    try:
        store.write(licensed.make())
        edition.access = Access(People(), edition.licensing)  # type: ignore[arg-type]
        assert edition.told(edition.standing(), EMILY.id).users_in_use == 120
        assert edition.told(edition.standing(), "u_danielokaf").users_in_use is None
        assert edition.told(edition.standing()).users_in_use is None  # no identity: as anyone is told it
        assert edition.me(EMILY)["license"]["users_in_use"] == 120
    finally:
        edition.close()
