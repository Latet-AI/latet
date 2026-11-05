"""How an exception names the body members it admits (the request-coverage plan's S-M7, R-42 and R-100; P2-6).

A body selector is an exact JSON pointer (RFC 6901) typed by the value it names: ``/x_context/team`` of kind
``string`` names that one member and nothing beside or below it. Its only wildcard is an array index, a whole segment
``*``, which matches an element of an array and never an object's member; there is no key wildcard, no regex, no
parent object (a selector names a string, a number, a boolean or a null, never a container whose members would come
with it). At most 256 characters and 32 segments; a segment is printable text, ``~0`` and ``~1`` its only escapes.
A selector never names a credential member: a segment whose words say key, token, secret, password, authorization,
cookie, signature and the like is refused when the selector is made, whatever the body it would meet.

The body walk (``body_coverage``) consults selectors only where the profile's closed declaration would refuse: an
undeclared member of a readable holder, outside every provider item. A member the profile declares keeps its own
treatment (a model id, a control, an opaque value, an image), and a holder the profile refuses by its kind (an unknown
block or item type, a provider item, an inline image, remote media) is refused before any selector is read. What a
selector admits is checked by the final check, keys and values alike, never masked, and the decision names the
exception that admitted it (``exception_rule``), so that a decision the rules in force do not list is never minted.

R-100: an exception records the body revision of its profile when it is approved. A later revision lists the
components it changed, as pointers; the exception carries over only if none of them is on a selector's path (the
member itself, a holder or an ancestor, or a level below). Otherwise, or for a revision this version does not have,
it is suspended and needs approval again: nothing widens by an upgrade."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

# The grammar itself is below the settings and the gateway (``selector_grammar``), so that the settings hold an
# exception's body components to it without importing the gateway; its names are this module's too.
from latet.selector_grammar import BodySelector as BodySelector
from latet.selector_grammar import SelectorRefused as SelectorRefused
from latet.selector_grammar import pointer_segments as _segments
from latet.selector_grammar import selectable as selectable

MAX_SELECTORS = 64  # per exception record (the plan's finite budgets: Policy)
MAX_EXCEPTIONS = 128  # exception records per identity, so at most that many for one route
_EXCEPTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_INDEX = re.compile(r"0|[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class BodyGrant:
    """One exception's body selectors: its internal id (never its description) and 1 to 64 distinct selectors."""

    exception: str
    selectors: tuple[BodySelector, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.exception, str) or not _EXCEPTION.fullmatch(self.exception):
            raise SelectorRefused("exception id")
        if (
            type(self.selectors) is not tuple
            or not 1 <= len(self.selectors) <= MAX_SELECTORS
            or not all(type(one) is BodySelector for one in self.selectors)
            or len(set(self.selectors)) != len(self.selectors)
        ):
            raise SelectorRefused("selectors")


def exception_rule(exception: str) -> str:
    """The rule a decision names when an exception admitted part of its body: the rules in force must list it, or the
    decision is not minted (``outcome``), so that a revoked exception stops at once and a Forward hides none."""
    return "exception " + exception


@dataclass(slots=True, eq=False, repr=False)
class _Node:
    keys: dict[str, _Node] = field(default_factory=dict)
    index: _Node | None = None  # the array index wildcard
    ends: dict[str, set[str]] = field(default_factory=dict)  # a selector ends here: its kind, its exceptions
    through: set[str] = field(default_factory=set)  # the exceptions whose selectors reach or pass this node

    def child(self, segment: str | None) -> _Node:
        if segment is None:
            if self.index is None:
                self.index = _Node()
            return self.index
        return self.keys.setdefault(segment, _Node())

    def continues(self) -> bool:
        return bool(self.keys) or self.index is not None


@dataclass(frozen=True, slots=True, repr=False)
class Admitted:
    """What an admitted member gives the final check (its keys and its values, as text) and the exceptions that
    named it."""

    texts: tuple[str, ...]
    exceptions: frozenset[str]


def _kind(value: Any) -> str | None:
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return "number"
    return None


class BodyGrants:
    """The body selectors of the exceptions that apply to one request's exact route, as one immutable trie.

    Made once for the rules in force and shared by the requests they decide. ``admits`` answers for one undeclared
    member, found by the body walk at its concrete path (object keys and array indexes from the body's top)."""

    __slots__ = ("_root",)

    def __init__(self, grants: Iterable[BodyGrant] = ()) -> None:
        root = _Node()
        given = tuple(grants)
        if len(given) > MAX_EXCEPTIONS or len({one.exception for one in given}) != len(given):
            raise SelectorRefused("exceptions")
        for one in given:
            if type(one) is not BodyGrant:
                raise SelectorRefused("selectors")
            for chosen in one.selectors:
                node = root
                for segment in chosen.segments:
                    node = node.child(segment)
                    node.through.add(one.exception)
                node.ends.setdefault(chosen.kind, set()).add(one.exception)
        self._root = root

    def __bool__(self) -> bool:
        return self._root.continues()

    def admits(self, path: tuple[str | int, ...], value: Any) -> Admitted | None:
        nodes = [self._root]
        for step in path:
            nodes = _step(nodes, step)
            if not nodes:
                return None
        texts: list[str] = []
        used: set[str] = set()
        if not _cover(value, nodes, texts, used):
            return None
        return Admitted(tuple(texts), frozenset(used))


def _step(nodes: list[_Node], step: str | int) -> list[_Node]:
    out: dict[int, _Node] = {}
    for node in nodes:
        if isinstance(step, str):
            found = node.keys.get(step)
            if found is not None:
                out[id(found)] = found
        else:
            if node.index is not None:
                out[id(node.index)] = node.index
            found = node.keys.get(str(step))
            if found is not None:
                out[id(found)] = found
    return list(out.values())


def _cover(value: Any, nodes: list[_Node], texts: list[str], used: set[str]) -> bool:
    """Every key and value of ``value`` is named by a selector reaching ``nodes``, with the kind it names."""
    if isinstance(value, dict):
        through = [node for node in nodes if node.keys]
        if not through:
            return False  # no selector goes on into an object here: no parent object, no deeper level
        if not value:
            used.update(*(node.through for node in through))
        for key, child in value.items():
            below = _step(through, key)
            if not below:
                return False  # a sibling key no selector names
            texts.append(key)
            if not _cover(child, below, texts, used):
                return False
        return True
    if isinstance(value, list):
        through = [node for node in nodes if node.index is not None or any(_INDEX.fullmatch(k) for k in node.keys)]
        if not through:
            return False
        if not value:
            used.update(*(node.through for node in through))
        for index, child in enumerate(value):
            below = _step(through, index)
            if not below or not _cover(child, below, texts, used):
                return False
        return True
    kind = _kind(value)
    named = [node.ends[kind] for node in nodes if kind is not None and kind in node.ends]
    if not named:
        return False
    used.update(*named)
    if kind == "string":
        texts.append(value)
    elif kind == "number":
        texts.append(str(value))
    return True


@dataclass(frozen=True, slots=True)
class BodyRevision:
    """One revision of a protocol's body declaration and the components it changed, as pointers (``*`` any index)."""

    id: str
    changed: tuple[str, ...] = ()
    segments: tuple[tuple[str | None, ...], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.changed) is not tuple:
            raise SelectorRefused("revision")
        # A change may name a credential member: it suspends what it reaches, it admits nothing.
        object.__setattr__(self, "segments", tuple(_segments(one, credentials=False) for one in self.changed))


# A WebSocket's cancel frame is read by a closed declaration of its own (``body_coverage.cancel_fields``), which no
# exception reaches: its revision names what its proofs are read by alone.
_PROTOCOLS = ("anthropic", "chat", "responses", "embeddings", "gemini", "ws-cancel")
# Every protocol's body declarations, oldest first, as this version ships them; the id is the one its proofs carry
# (``BodyProof.profile``). A revision that changes what a member is, or declares one, lists its pointer.
HISTORY: Mapping[str, tuple[BodyRevision, ...]] = MappingProxyType(
    {protocol: (BodyRevision(f"{protocol}.body.1"),) for protocol in _PROTOCOLS}
)


def revision(protocol: str) -> str:
    """The body revision this version reads ``protocol``'s requests by: an exception approved now records it."""
    return HISTORY[protocol][-1].id


def _overlaps(one: tuple[str | None, ...], other: tuple[str | None, ...]) -> bool:
    for a, b in zip(one, other, strict=False):
        if a == b:
            continue
        if a is None and b is not None and _INDEX.fullmatch(b):
            continue
        if b is None and a is not None and _INDEX.fullmatch(a):
            continue
        return False
    return True  # one is the other, or on its path


def carries_over(
    selectors: Iterable[BodySelector],
    protocol: str,
    approved: str,
    *,
    history: Mapping[str, tuple[BodyRevision, ...]] = HISTORY,
) -> bool:
    """R-100: whether an exception approved under body revision ``approved`` of ``protocol`` (its route's) still names
    the same components now.

    True only if every revision since ``approved`` left each selector's components alone: no changed pointer is the
    selected member, one of its holders or ancestors, or below it. A revision this version does not have (a later one
    after a downgrade, another protocol's, a malformed id) keeps nothing."""
    chain = history.get(protocol, ())
    ids = [one.id for one in chain]
    if approved not in ids:
        return False
    later = chain[ids.index(approved) + 1 :]
    chosen = [one.segments for one in selectors]
    return not any(_overlaps(changed, segments) for step in later for changed in step.segments for segments in chosen)
