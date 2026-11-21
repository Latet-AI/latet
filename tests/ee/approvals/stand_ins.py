"""Stand-ins for central approvals' tests (Latet Enterprise): who is who on a server, its spaces, and the edition of a
server's spaces with a real ``CentralApprovals`` behind its hooks."""

from __future__ import annotations

import secrets
from typing import Any

from latet.ee.roles import Role
from latet.state import IdentityRef

from ...central import Company

_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"


class FakeDirectory:
    """``Directory`` from dictionaries: people, API keys, their roles, groups and controllers (``conn`` ignored)."""

    def __init__(self) -> None:
        self.everyone: dict[str, IdentityRef] = {}
        self.disabled: set[str] = set()
        self.granted: dict[str, set[Role]] = {}
        self.controlling: dict[str, list[str]] = {}  # an API key's id: the ids of the people who control it
        self.groups: dict[str, str] = {}  # an identity's id: the name of its first group

    def add(self, name: str, *, kind: str = "person", roles: tuple[Role, ...] = (), group: str | None = None) -> Any:
        identity = IdentityRef("u_" + "".join(secrets.choice(_BASE32) for _ in range(10)), name, kind)  # pyright: ignore[reportArgumentType]
        self.everyone[identity.id] = identity
        self.granted[identity.id] = set(roles)
        if group is not None:
            self.groups[identity.id] = group
        return identity

    def identity(self, identity_id: str) -> IdentityRef | None:
        return None if identity_id in self.disabled else self.everyone.get(identity_id)

    def holding(self, role: Role, *, conn: Any = None) -> list[IdentityRef]:
        return [i for i in self.everyone.values() if i.kind == "person" and i.id not in self.disabled
                and role in self.granted.get(i.id, set())]  # fmt: skip

    def roles(self, identity_id: str, *, conn: Any = None) -> frozenset[Role] | None:
        found = self.identity(identity_id)
        if found is None or found.kind != "person":
            return None  # deciders are enabled people
        return frozenset({Role.USER, *self.granted.get(identity_id, set())})

    def controllers(self, key_id: str, *, conn: Any = None) -> list[IdentityRef]:
        found = [self.identity(person) for person in self.controlling.get(key_id, [])]
        return [person for person in found if person is not None]

    def controlled(self, identity_id: str) -> list[IdentityRef]:
        return [self.everyone[key] for key, people in self.controlling.items() if identity_id in people]

    def group_of(self, identity_id: str) -> str | None:
        return self.groups.get(identity_id)


class Spaces:
    """The spaces of a server as central approvals reach them: each identity's runtime, by its id."""

    def __init__(self) -> None:
        self.runtimes: dict[str, Any] = {}
        self.fresh: list[bool] = []  # whether each reach asked for the settings read again (R41)

    async def __call__(self, identity_id: str, run: Any, fresh: bool = False) -> Any:
        self.fresh.append(fresh)
        return self.runtimes.get(identity_id)


class Served(Company):
    """The edition of a server's spaces with a real ``CentralApprovals`` behind its hooks, as Latet Enterprise wires
    it (Task 7)."""

    def changes_go_to(self, identity: Any) -> Any:
        return self.queue.changes_go_to(identity)

    def live_review_goes_to(self, identity: Any) -> Any:
        return self.queue.live_review_goes_to(identity)
