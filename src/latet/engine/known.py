"""The vault's values and their fakes as detectors: what every masker of a vault (a project's included) and the final
check find again wherever a value appears in clear, or a fake comes back.

RE2 compiles an alternation of values all at once and holds the GIL while it does, a second at 100,000 secrets: on a
server every identity's event loop waits meanwhile. So the values are held in parts (``_Part``), each compiled on its
own: a new value goes to the fresh part, at most ``FRESH_VALUES`` values and ``FRESH_CHARS`` characters, compiled again
at each new value; full, the fresh part is folded into the last of the others, at most ``PART_VALUES`` values (one
alternation for each kind of value, never a word index) and ``PART_CHARS`` characters, compiled once more. One new
value costs a compile of the fresh part, and every ``FRESH_VALUES`` values one of a part: a bounded compile, whatever
the vault holds, and a bounded cost per value. A value longer than the fresh part takes goes with the other long ones
(a whole JSON string of strict data mode's), in a part compiled again at each of them until it is full; one longer than
a part takes a part of its own, compiled once. So the parts are as many as the vault's characters make, never one for
each long value, and a scan of a text makes that many searches. A value forgotten, or another instance's that takes the
place of one under its id, has its part compiled again. Each part is compiled under an RE2 budget sized to it
(``bounded``): many are held, and RE2 compiles them faster so.

A reading of the values (``ValueMatchers.current``) takes what the vault changed since the reading before, in one step
under the vault's lock (``Vault.take_changes``: the version, and for each value changed meanwhile what the vault holds
of it now), applies it to the parts and compiles each part that changed, then answers the parts whole, at that version:
no value the vault knew at that version is missing from them, nor between two readings. A value the vault keeps
meanwhile is in the next reading. Readings take one lock: a second waits for the first, whose compiles are bounded each,
with a pause between two when it compiles many parts in a row (``LET_GO``), so that the event loop takes its turn. One
that fails (no memory) has every value read again by the next."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import WORD_INDEX_FROM, KnownValuesDetector
from latet.engine.types import EntityClass, Span

if TYPE_CHECKING:
    from latet.engine.vault import Entity, Vault

FRESH_VALUES = 128  # the part new values go to, compiled again at each one: a few milliseconds
# The pause after each compile of a reading that compiles several parts in a row, every value read (a space's first
# reading, more values changed between two readings than kept track of). A pause of no time at all gives the event
# loop no turn on Windows (its thread takes the GIL back before the loop's wakes); one millisecond does, and it costs a
# reading of 100,000 values a few tenths of a second, in its own thread.
LET_GO = 0.001
FRESH_CHARS = 8 * 1024
PART_VALUES = WORD_INDEX_FROM  # a full part: tens of milliseconds to compile, its values matched as a short list is
PART_CHARS = 96 * 1024

_Key = tuple[str, str]  # a value as the vault keys it: its type and the value


def value_chars(entity: Entity) -> int:
    """What an entity puts in a pattern: its value or its fake, the longer (``Vault.chars`` counts them)."""
    return max(len(entity.original), len(entity.surrogate))


class _Part:
    """Some of the vault's entities, and the detectors of their values and of their fakes, made once it changed."""

    __slots__ = ("chars", "made", "members")

    def __init__(self) -> None:
        self.members: dict[_Key, Entity] = {}
        self.chars = 0
        self.made: tuple[KnownValuesDetector, KnownValuesDetector] | None = None

    def add(self, key: _Key, entity: Entity) -> None:
        self.members[key] = entity
        self.chars += value_chars(entity)
        self.made = None

    def drop(self, key: _Key) -> None:
        self.chars -= value_chars(self.members.pop(key))
        self.made = None

    def detectors(self, pause: bool = False) -> tuple[KnownValuesDetector, KnownValuesDetector]:
        """Its two detectors, compiled now when it changed: two calls to RE2, each holding the GIL for its whole
        compile. ``pause``: one of many parts compiled in a row (a first reading, a reading of every value again), each
        compile then followed by a pause (``LET_GO``), so that the event loop takes its turn between two."""
        if self.made is None:
            entities = list(self.members.values())
            # A value masked by strict data mode stays in its structure: `admin` in a table is not `admin` in code.
            known = KnownValuesDetector(
                ((e.original, e.etype, e.eclass) for e in entities if e.eclass != EntityClass.STRUCT), bounded=True
            )
            if pause:
                time.sleep(LET_GO)
            from latet.engine.vault import fake_reading  # the vault imports this module

            # A fake, and how a fake written in escapes reads (`Nicola Greco` for `Nicola%20Greco`): either sent back
            # in a later text is masked as a value of its own, so that the model's answer comes back as it was sent.
            fakes = [(e.surrogate, e.etype, e.eclass) for e in entities]
            fakes += [(reading, e.etype, e.eclass) for e in entities if (reading := fake_reading(e)) is not None]
            echo = KnownValuesDetector(fakes, rule="echo", bounded=True)
            if pause:
                time.sleep(LET_GO)
            self.made = (known, echo)
        return self.made


class PartedValues:
    """A detector of values held in parts: what each part finds, the text encoded once for all of them."""

    id = "known-values"
    mandatory = True

    def __init__(self, parts: tuple[KnownValuesDetector, ...]) -> None:
        self._parts = parts

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if not self._parts:
            return
        data = text.encode()
        for part in self._parts:
            yield from part.spans(text, data)


@dataclass(frozen=True, slots=True)
class Matched:
    """The vault's values (``known``) and fakes (``echo``) as they were at its ``version``."""

    version: int
    known: PartedValues
    echo: PartedValues


_MATCHED = ("original", "surrogate", "etype", "eclass")  # what the detectors read of an entity


def _same(one: Entity, other: Entity) -> bool:
    """Whether two entities are matched alike (the rule each was found by is the dashboard's, not the matchers')."""
    return all(getattr(one, name) == getattr(other, name) for name in _MATCHED)


class ValueMatchers:
    """A vault's values and fakes, in parts (the module's words). One for each vault (``Vault.matchers``): every masker
    of it reads through it."""

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._lock = threading.Lock()
        self._where: dict[_Key, _Part] = {}  # the part each entity is in
        self._full: list[_Part] = []
        self._fresh = _Part()
        self._long = _Part()  # values longer than the fresh part takes, gathered until they fill a part
        self._held: Matched | None = None
        self._whole = True  # every value read again at the next reading (none read yet, or one failed half way)
        self._chars = 0  # what its parts hold (``chars``)

    @property
    def chars(self) -> int:
        """The characters its parts hold, as last read: each value's or its fake's, the longer (``value_chars``), what
        their patterns take memory in proportion to. It lags the vault until the next reading: a server weighs each
        value's length as it is kept, by the vault's own count of the same (``Vault.chars``)."""
        return self._chars

    def current(self) -> Matched:
        """The values and fakes as the vault holds them now (made again where it changed since the last reading)."""
        held = self._held
        if held is not None and held.version == self._vault.version:
            return held
        with self._lock:
            held = self._held
            if held is not None and held.version == self._vault.version:
                return held
            try:
                version, everything, changed = self._vault.take_changes(whole=self._whole)
                if everything is not None:
                    self._restart(everything)
                else:
                    self._apply(changed)
                parts = [part for part in (*self._full, self._fresh, self._long) if part.members]
                pause = sum(part.made is None for part in parts) > 1  # many compiles in a row: a turn between two
                made = [part.detectors(pause) for part in parts]
            except BaseException:
                self._whole = True  # what was taken may be applied in part: the next reading reads every value
                raise
            self._whole = False
            self._held = Matched(
                version,
                PartedValues(tuple(known for known, _ in made if not known.empty)),
                PartedValues(tuple(echo for _, echo in made if not echo.empty)),
            )
            return self._held

    def _restart(self, entities: list[Entity]) -> None:
        """Every entity laid in parts afresh, each full."""
        self._where, self._full, self._fresh, self._long = {}, [], _Part(), _Part()
        self._chars = 0
        part = _Part()
        for entity in entities:
            key = (entity.etype, entity.original)
            if part.members and (len(part.members) >= PART_VALUES or part.chars + value_chars(entity) > PART_CHARS):
                self._full.append(part)
                part = _Part()
            part.add(key, entity)
            self._chars += value_chars(entity)
            self._where[key] = part
        if part.members:
            self._full.append(part)

    def _apply(self, changed: dict[_Key, Entity | None]) -> None:
        """What the vault holds now of each value it changed: a value gone leaves its part, one new or another in its
        place goes to the fresh part (or with the long ones)."""
        for key, entity in changed.items():
            part = self._where.get(key)
            if part is not None:
                if entity is not None and _same(part.members[key], entity):
                    continue
                self._chars -= value_chars(part.members[key])
                part.drop(key)
                del self._where[key]
                if not part.members and part is not self._fresh and part is not self._long:
                    self._full.remove(part)
            if entity is not None:
                self._place(key, entity)

    def _place(self, key: _Key, entity: Entity) -> None:
        chars = value_chars(entity)
        self._chars += chars
        if chars > PART_CHARS:  # longer than a part: one of its own, compiled once
            part = _Part()
            part.add(key, entity)
            self._where[key] = part
            self._full.append(part)
            return
        if chars > FRESH_CHARS:  # too long to be compiled again at each new value: with the other long ones
            long = self._long
            if long.members and (long.chars + chars > PART_CHARS or len(long.members) >= PART_VALUES):
                self._full.append(long)
                long = self._long = _Part()
            long.add(key, entity)
            self._where[key] = long
            return
        fresh = self._fresh
        fresh.add(key, entity)
        self._where[key] = fresh
        if len(fresh.members) >= FRESH_VALUES or fresh.chars >= FRESH_CHARS:
            self._fold()

    def _fold(self) -> None:
        """The fresh part, full, into the last part when that has room for it, else a part of its own; a new fresh
        part."""
        fresh, last = self._fresh, self._full[-1] if self._full else None
        if (
            last is not None
            and len(last.members) + len(fresh.members) <= PART_VALUES
            and last.chars + fresh.chars <= PART_CHARS
        ):
            for key, entity in fresh.members.items():
                last.add(key, entity)
                self._where[key] = last
        else:
            self._full.append(fresh)
        self._fresh = _Part()
