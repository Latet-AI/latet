"""The role table: each role may do what notes/enterprise-edition.md says, and nothing more (ee/roles.py)."""

import pytest

from latet.ee.roles import ACTIONS, GRANTED, PAGES, Role, can

TABLE = {
    Role.USER: set(),
    Role.GROUP_LEAD: {"reports.group"},
    Role.SECURITY: {
        "reports.group",
        "reports.all",
        "audit.read",
        "policy.read",
        "policy.edit",
        "terms.manage",
        "approvals.decide",
        "siem.edit",
    },
    Role.AUDITOR: {"reports.group", "reports.all", "audit.read", "policy.read"},
    Role.ADMIN: {"audit.read", "people.manage", "keys.manage", "sign-in.edit", "license.manage"},
}


@pytest.mark.parametrize("role", list(Role))
def test_each_role_may_do_what_the_table_says_and_nothing_more(role):
    assert {action for action in ACTIONS if can({role}, action)} == TABLE[role]


def test_roles_add_up():
    assert can({Role.USER, Role.AUDITOR}, "policy.read")
    assert not can({Role.USER, Role.AUDITOR}, "policy.edit")


def test_an_action_latet_does_not_know_is_allowed_to_nobody():
    assert not can(set(Role), "everything")


def test_every_page_of_the_interfaces_is_shown_by_an_action_of_the_table():
    assert set(PAGES) == {
        "license",
        "policy",
        "people",
        "sign-in",
        "reports",
        "audit",
        "siem",
        "company-approvals",
        "term-sync",
    }
    assert set(PAGES.values()) <= set(ACTIONS)


def test_each_page_is_shown_by_the_action_the_interfaces_name():
    assert dict(PAGES) == {
        "license": "license.manage",
        "policy": "policy.read",
        "people": "people.manage",
        "sign-in": "sign-in.edit",
        "reports": "reports.group",
        "audit": "audit.read",
        "siem": "reports.all",  # M13: the SIEM outputs, a page of their own
        "company-approvals": "approvals.decide",
        "term-sync": "terms.manage",
    }


def test_every_action_is_held_by_at_least_one_role():
    assert all(ACTIONS[action] for action in ACTIONS)


def test_the_tables_cannot_be_changed():
    with pytest.raises(TypeError):
        ACTIONS["policy.edit"] = frozenset(Role)  # type: ignore[index]
    with pytest.raises(TypeError):
        PAGES["policy"] = "people.manage"  # type: ignore[index]


def test_the_role_names_are_those_of_the_interfaces_and_the_user_role_is_never_given():
    assert [role.value for role in Role] == ["user", "group-lead", "security", "auditor", "admin"]
    assert set(GRANTED) == set(Role) - {Role.USER}
