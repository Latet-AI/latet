"""What a token of an API key may do: ``scan`` (say what a text holds), ``mask`` (replace it with fakes), ``unmask``
(put the real values back) and ``forget`` (take values out of the key's vault, Task 11b), each a permission of its own.
Masking says what it found, so ``mask`` brings ``scan``; nothing brings ``forget``, which is given by name alone.

A token's row keeps its permissions in ``tokens.permissions``: NULL for scan, mask and unmask (``DEFAULT``), as every
token made before permissions existed (a person's computer, an API key's first token), so that those rows are what they
were and none of them may forget; a token added to an API key keeps its own, scan, mask and unmask too (Ruling T13c):
the names, sorted and joined by commas (``stored``), one of eleven forms, which the column's CHECK allows and no other.
The row's keyed tag covers that text, under a label of its own (``identities._GRANT_TAG``), so that a writer of the
database can neither widen a token's permissions (to ``forget``, say) nor narrow them, nor make an added token one that
serves agents: a row changed so finds no one.

A lookup of a token answers its ``Grant``: the identity, the token's id, what it may do, and whether it serves agents
(a NULL row). The gate asks it of every call. An agent's paths (an address, which calls a model through Latet, and the
plugin's bridge) restore a key's fakes with no entry of the administration log for each restore, so they serve only a
token that serves agents (Ruling T13b): a token added to a key acts through the applications API alone, where each
unmask is logged. An address then takes ``mask`` and ``unmask`` (``GATEWAY``); each of the bridge's calls what
``bridge_needs`` says, a call it does not name the three of ``DEFAULT``. Nothing here reads or writes anything."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from latet.server.identities import Identity

__all__ = [
    "DEFAULT",
    "GATEWAY",
    "PERMISSIONS",
    "Grant",
    "Permission",
    "bridge_needs",
    "loaded",
    "normalized",
    "stored",
]

Permission = Literal["scan", "mask", "unmask", "forget"]
PERMISSIONS: Final[tuple[Permission, ...]] = ("scan", "mask", "unmask", "forget")  # in the order a refusal names them
# What a token without permissions of its own may do (NULL in its row): scan, mask and unmask, as every token made
# before permissions existed. Never ``forget``: a token that may take values out of a vault is given it by name.
DEFAULT: Final[frozenset[Permission]] = frozenset(("scan", "mask", "unmask"))
# What calling a model through Latet takes: it masks what goes and restores what comes back.
GATEWAY: Final[frozenset[Permission]] = frozenset(("mask", "unmask"))
# The words of a refusal: the names allowed, never what was given.
_REFUSED: Final = f"a token's permissions are one or more of {', '.join(PERMISSIONS)}"
# The plugin's bridge, under an identity's address, and what each of its calls needs of the token that makes it, by
# the path after ``_ENGINE`` (``bridge_needs``): the calls that tell the plugin whether it is set up need nothing; those
# that mask, check what leaves or wait on a decision about it need ``mask``; those that restore real values, or add
# fakes to a session so that a later call restores them, need ``unmask``. The AWS sessions (``aws-sessions`` and below)
# hold the credentials a model is called with, as the gateway does: ``mask`` and ``unmask``. No call of the bridge
# forgets a value.
_ENGINE: Final = "/_latet/engine/v1/"
_AWS_SESSIONS: Final = "aws-sessions"
_MASK: Final[frozenset[Permission]] = frozenset(("mask",))
_UNMASK: Final[frozenset[Permission]] = frozenset(("unmask",))
_BRIDGE_NEEDS: Final[dict[str, frozenset[Permission]]] = {
    "ping": frozenset(),
    "route-check": frozenset(),
    "mask": _MASK,
    "verify": _MASK,
    "outbound": _MASK,
    "instructions": _MASK,
    "approvals/wait": _MASK,
    "rehydrate": _UNMASK,
    "scope": _UNMASK,
}


def normalized(given: Iterable[str]) -> frozenset[Permission]:
    """The permissions ``given`` names, ``mask`` bringing ``scan`` (and nothing bringing ``forget``). Raises ValueError,
    naming the names allowed and quoting nothing given, for a name that is not one of them (``MASK`` is not ``mask``)
    and for none at all."""
    chosen: set[Permission] = set()
    for name in given:
        if name not in PERMISSIONS:
            raise ValueError(_REFUSED)
        chosen.add(name)
    if not chosen:
        raise ValueError(_REFUSED)
    if "mask" in chosen:
        chosen.add("scan")
    return frozenset(chosen)


def stored(permissions: frozenset[Permission]) -> str:
    """What the row of a token added to an API key keeps of ``permissions``, as ``normalized`` gives them: their names,
    sorted and joined by commas, scan, mask and unmask too (``mask,scan,unmask``, Ruling T13c: NULL is the row of a
    token that serves agents alone, which ``Identities`` writes as such). Raises ValueError for a set ``normalized``
    does not give (none, a name not Latet's, ``mask`` without ``scan``): a row never holds what ``loaded`` would
    refuse."""
    if permissions != normalized(permissions):
        raise ValueError("a token's permissions are written as Latet normalizes them: mask brings scan")
    return ",".join(sorted(permissions))


def _forms() -> dict[str, frozenset[Permission]]:
    """Every form ``stored`` makes, with the permissions it stands for: what ``loaded`` reads besides NULL."""
    found: dict[str, frozenset[Permission]] = {}
    for size in range(1, len(PERMISSIONS) + 1):
        for chosen in itertools.combinations(PERMISSIONS, size):
            granted = normalized(chosen)
            found[stored(granted)] = granted
    return found


_FORMS: Final = _forms()


def loaded(column: str | None) -> frozenset[Permission]:
    """The permissions a token's row keeps (``stored``'s form): ``DEFAULT`` for NULL. Raises ValueError, quoting
    nothing of it (a writer of the database can put anything there), for a text ``stored`` does not make."""
    if column is None:
        return DEFAULT
    found = _FORMS.get(column)
    if found is None:
        raise ValueError("a token's permissions are not in a form Latet stores")
    return found


def bridge_needs(path: str) -> frozenset[Permission]:
    """What a call of the plugin's bridge needs of its token, by ``path`` as the gate reads it after an identity's
    address (``/_latet/engine/v1/rehydrate``): nothing for ``ping`` and ``route-check``; ``mask`` for ``mask``,
    ``verify``, ``outbound``, ``instructions`` and ``approvals/wait``; ``unmask`` for ``rehydrate`` and ``scope``;
    ``mask`` and ``unmask`` for ``aws-sessions`` and everything under it. Any other path, under the bridge or not, needs
    scan, mask and unmask (``DEFAULT``, what a token without permissions of its own holds): a call this list does not
    name is refused to a token with less, never let through. The gate asks it of a token that serves agents alone, a
    token added to a key being refused every call of the bridge before (Ruling T13b): a second guard."""
    if not path.startswith(_ENGINE):
        return DEFAULT
    call = path.removeprefix(_ENGINE)
    if call == _AWS_SESSIONS or call.startswith(f"{_AWS_SESSIONS}/"):
        return GATEWAY
    return _BRIDGE_NEEDS.get(call, DEFAULT)


@dataclass(frozen=True, slots=True)
class Grant:
    """What a token found by a lookup grants: its identity, the token's id, what it may do, and whether it serves agents
    (``agents``). Printed, it names its identity by its id (``Identity``'s ``repr``) and says nothing of the token's
    secret, which it does not hold."""

    identity: Identity
    token_id: str
    permissions: frozenset[Permission]
    # Whether it serves agents: its row keeps no permissions of its own (NULL), as a person's computer's token, an API
    # key's first and every token made before permissions existed. A token added to a key keeps its own, all three too
    # (Ruling T13c), and acts through the applications API alone (Ruling T13b). Never so unless a lookup says it.
    agents: bool = False

    def allows(self, *needed: Permission) -> bool:
        """Whether the token holds every permission of ``needed`` (what needs none is allowed)."""
        return self.permissions.issuperset(needed)

    def lacking(self, *needed: Permission) -> Permission | None:
        """The first permission of ``needed``, in the order ``PERMISSIONS`` names them, that the token does not hold:
        what a refusal names. None when it holds them all (what needs none, too)."""
        return next((name for name in PERMISSIONS if name in needed and name not in self.permissions), None)

    @property
    def gateway(self) -> bool:
        """Whether the token may call a model through Latet, which masks what goes and restores what comes back, with no
        entry of the administration log for each restore: it serves agents (``agents``) and holds ``mask`` and
        ``unmask``. Its address serves it then, and the plugin's bridge too."""
        return self.agents and self.permissions >= GATEWAY
