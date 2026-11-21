"""The roles of Latet Enterprise on a server, and what each may do (notes/enterprise-edition.md, "Roles").

| Role       | Sees                                                      | Decides                            |
|------------|-----------------------------------------------------------|------------------------------------|
| user       | their own data, and the API keys they control             | their settings; a controlled key's |
| group lead | their groups' reports                                     | nothing                            |
| security   | every report                                              | policy, terms, approvals, SIEM     |
| auditor    | reports, the policy's history and the administration log  | nothing                            |
| admin      | people, groups and API keys                               | sign-in, roles, license, API keys  |

A role is given to a person or to a group (ee/people.py). Controlling an API key is no role: it is a link between the
key and people or groups, which ee/keys.py reads.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Role(StrEnum):
    USER = "user"
    GROUP_LEAD = "group-lead"
    SECURITY = "security"
    AUDITOR = "auditor"
    ADMIN = "admin"


ACTIONS: Final[Mapping[str, frozenset[Role]]] = MappingProxyType(
    {
        # The reports of the groups one is in: group leads. Security and auditors see every group's (reports.all).
        "reports.group": frozenset({Role.GROUP_LEAD, Role.SECURITY, Role.AUDITOR}),
        "reports.all": frozenset({Role.SECURITY, Role.AUDITOR}),
        # The administration log: security and admin read it as well as auditors, by design (the interfaces text).
        "audit.read": frozenset({Role.AUDITOR, Role.SECURITY, Role.ADMIN}),
        "policy.read": frozenset({Role.SECURITY, Role.AUDITOR}),
        "policy.edit": frozenset({Role.SECURITY}),
        # E6's synced terms: the Term sync page, its sources and their secrets. 2026-11-06 (E6's [D1]): no rules for
        # everyone beside them; the policy is the company's (policy.edit), and synced terms reach every space beside the
        # settings.
        "terms.manage": frozenset({Role.SECURITY}),
        "approvals.decide": frozenset({Role.SECURITY}),
        "siem.edit": frozenset({Role.SECURITY}),
        "people.manage": frozenset({Role.ADMIN}),
        "keys.manage": frozenset({Role.ADMIN}),
        "sign-in.edit": frozenset({Role.ADMIN}),
        "license.manage": frozenset({Role.ADMIN}),
    }
)

# The dashboard's Enterprise pages (enterprise-interfaces.md, page ids) and the action that shows each one.
PAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "license": "license.manage",
        "policy": "policy.read",
        "people": "people.manage",
        "sign-in": "sign-in.edit",
        "reports": "reports.group",
        "audit": "audit.read",
        "siem": "reports.all",  # E4: the SIEM outputs, read by the security role and auditors (M13)
        "company-approvals": "approvals.decide",
        "term-sync": "terms.manage",
    }
)

# The roles given by hand, to a person or a group: everyone has the user role.
GRANTED: Final = (Role.GROUP_LEAD, Role.SECURITY, Role.AUDITOR, Role.ADMIN)


def can(roles: Collection[Role], action: str) -> bool:
    """Whether someone with ``roles`` may do ``action``. An action Latet does not know is allowed to nobody."""
    allowed = ACTIONS.get(action)
    return allowed is not None and any(role in allowed for role in roles)
