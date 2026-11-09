"""An API key's vault on a Latet server: its newest values in memory, every other one in PostgreSQL (R7).

A key's space masks logs and documents for applications, and its vault grows by millions of values. Each instance of
the server keeps in memory only its working set: the ``working`` values it used last (``WORKING_VALUES``, the server's
``key_working_values``), and the values whose value or fake has no anchor word (``engine.anchors``), which no text's
words find, held for good; and the newest secrets that no word of a text finds glued to letters or digits
(``held_glued``), ``held_at_most`` of them (``HELD_SECRETS``, the server's ``key_held_secrets``, Ruling 10b-R3), held
for good too (an older one is found in the store by its word or by an edge, including inside a longer ASCII run).
With ``glued_all`` (a person's space, the plan's S4), every value or fake of one run that no outer word finds glued on
both sides (``glued_both``) is held for good as well, whatever its age, and every secret held glued, not only the newest
``held_at_most`` (Ruling T9-S3): what no word finds stays where 0.10.0's whole vault held it. Every other value is in
the store alone (``KeyVaultStore``, PostgreSQL's ``PgVaultStore``), and found there by the words of the texts as they
come:

- Masking (``for_text``, the plan's S4, where Ruling 10b-R1 read the detectors of the whole working set for every
  text): each text is read for the values and fakes its own words find (``words_in``, read once a call for each text),
  in memory, among those its call asked the store for at once (``prefetch``: the values whose word is one of its texts'
  words, those a text holds brought into memory, Ruling T8a), and in the store for a word the call never asked for (not
  a word of a fake memory holds: a masked text holds its fakes where its values were); and for the few the vault's
  detectors (``Vault.matchers``) hold because no word of a text finds them glued (``matched``:
  held for good, or glued on both sides, ``unfound_glued``), while they are in memory. A value found once is masked for
  ever, whatever its age. A store that does not answer fails the call (``VaultUnread``): a text is never masked without
  its known values.
- Restoring (``candidates_in``): the fakes a text may hold are those its words find in memory, those memory holds
  that no word finds glued whose fake the text holds (``_HeldForms``), and those of the store whose word is one of its
  words, asked for once for all the texts of a call (``prefetch_fakes``). A fake of any age is restored. A
  conversation's values are brought back into memory as its request is masked (``hold``), so that its answer, restored
  on the event loop, finds them there.
- Lookups (``by_original``, ``by_surrogate``, ``get``, ``is_surrogate``): memory first, then the store; what they find
  joins the working set. A string the store says is no fake is remembered as such (``MISSES_KEPT`` at most), until the
  vault issues or learns it as one. A string that holds a live launch key is looked up as a fake in memory alone,
  never asked of the store nor remembered (``by_surrogate``): no fake has a key's shape.

Past ``working`` values, the least recently used ``EVICT_SHARE`` of them leave memory (as ``forget`` takes a value out
of it, the store left as it is): never a value held for good, nor one that a call still under way found or kept, nor
one kept or learnt while a call under way was open (``holding``: the call asked for its words before it was kept). The
version moves when what the detectors hold changes (a value kept, learnt or forgotten; one they hold placed in memory
or let go), and when a value kept after the vault was made is found that it never heard of (another instance's, whose
message was lost), not when a value joins memory for its lookups: a text the engine keeps masked is checked again by
its words, never vouched for by memory alone (``holds_since``).

The event loop serves every identity of the instance and never waits on the store: on its thread (any thread that runs
one) a lookup answers from memory alone, a text is read for memory's values alone, and ``prefetch`` refuses.

A call's lookups are bounded (``engine.lookup``), and past a bound read as the release before read, never refusing
(the owner's ruling of 2026-10-29, Task 8's fix round 6): the interior windows skipped or dropped, an API key's words
asked as 0.10.2 asked them (``KeyVaultStore.with_words``, ``TooManyCandidates`` where it was), a person's whole vault
read for the call (``whole_past_bounds``).

A person's vault of ``whole_at_most`` values or fewer (``WHOLE_VALUES``, the owner's ruling of 2026-10-31) is held whole
(``holds_all``): every value of its store in memory from its build, none let go, so that each text is read from memory
alone, as if every lookup of the store had found what memory holds, with no lookup of the store for each text (they
cost more than they saved in a small vault: 0.69x 0.10.0 at 20,000 values). It stays current as a vault on its store
does: each call asks the store first what changed (``refresh``), and takes in by their ids the entries the store's log
names (those out of memory too), or reads the whole vault again where the log names nothing (a forget, a barrier). A
call begun while it held every value reads memory alone to its end, and nothing leaves memory while one is open. Once
it holds more than ``whole_at_most`` values it is a vault on its store as any other, its working set cut back as its
last such call ends."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import heapq
import itertools
import re
import secrets
import sys
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, ClassVar

from latet.engine import lookup
from latet.engine.anchors import address_word, anchor, dotted, edges, glued_unfound, words_and_windows
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import SCALED_PER_BYTE, KnownValuesDetector, forms, long_enough
from latet.engine.escapes import blanked, decoded, escapes, read_escaped
from latet.engine.known import Matched, Matchers, ValueMatchers, text_detectors
from latet.engine.lookup import LOOKUP_BATCH, LookupBudget, LookupLimitExceeded, TooMany
from latet.engine.rehydrate import restores_for
from latet.engine.response_guard import safe_text
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, MaskingError, Vault, _kept, fake_reading, restored_whole

if TYPE_CHECKING:
    from latet.engine.known import TextValues
    from latet.engine.surrogates.generators import Drbg
    from latet.state.protocols import KeyVaultStore, Role, Together


# The values a key's space holds in memory: about 8 to 10 MiB at 1.5 to 2 KiB a value with its share of the detectors
# (R7). More is no faster (Task 10, Ruling T10b): a call asks the store for every word of its texts whatever memory
# holds, and each value in memory is one more that the detectors read every text for. On a vault of 200,000 values,
# 500 log lines a call, mask ran at 351 lines a second with 1,000 values in memory, 281 with 5,000, 201 with 20,000 and
# 94 with 100,000. What a key's conversations hold for their answers fits in a working set (``hold``): 5,000 leaves
# them room that 1,000 would not. The server's setting key_working_values, 1,000 to 1,000,000.
WORKING_VALUES = 5_000
# The secrets a key's space holds in memory for good because no word of a text finds them glued (``held_glued``), at
# most: the newest; an older one is in the store alone, found by its words where nothing is glued to it (Ruling
# 10b-R3). A log with a token in each line makes one a line: unbounded, they filled the working set and every build.
# Each is read in every text as any value in memory is: with 1,000 held beside 4,000 other values, mask ran as with
# 5,000 values and none held (1,041 and 1,131 lines a second, an applications call of 500 lines, one thread). The
# server's setting key_held_secrets, 0 to 1,000,000.
HELD_SECRETS = 1_000
# The share of the working set that leaves memory at once once it is full: the least recently used tenth, so that the
# detectors are made again once every so many new values, not at each.
EVICT_SHARE = 0.1
MISSES_KEPT = 10_000  # strings the store said are no fake, remembered (the oldest forgotten first)
# How long a conversation's values stay held for its answer at most (``hold``): an answer that never ends (its
# connection lost before Latet heard it end) holds them no longer than this.
HOLD_SECONDS = 600.0
_GONE_KEPT = 10_000  # values forgotten, remembered so that a reading begun before is not brought back into memory
# The words of the entries its calls' lookups find, worked out once (``StoredVault._both_words``, Task 8's fix round 7):
# at a million values a call's words find some 1,500 entries, most of them again in the next call, and each call worked
# out every one's words anew (11 ms a call). As many entries as memory holds (``working`` and ``held_at_most``) times
# this, at most, the least recently used let go first; 0: none kept.
WORDS_MEMO_SHARE = 1
# An entry of the memo besides its id, its digest and its words, which are weighed by their sizes: its tuple, its place
# in the dict and in the order of use, its weight.
_MEMO_ENTRY_BYTES = 200
_MEMO_DIGEST = 16  # the bytes of the keyed digest an entry is checked against (``StoredVault._made_of``)
# What a key's parts, and a person's (Task 9), may take past the ordinary RE2 budget at most (``SCALED_PER_BYTE``'s,
# ``StoredVault.detectors_bytes``; Task 8's fix round 9, the owner's ruling: a request is never refused for its size
# where the release before masked it): a server gives each space an eighth of its share (``spaces.DETECTORS_SHARE``),
# and this is that of its default budget, 2,048 MiB. Parts past it keep the ordinary budget: slower, exact, never
# refused.
DETECTORS_EXTRA_MOST = 128 << 20
# What a person's space asks its store for of the secrets held glued (``glued_all``): every one, whatever its age, as
# 0.10.0's whole vault held them (Ruling T9-S3); an API key's holds its newest ``held_at_most``.
_EVERY_HELD = 2**31 - 1
# Whole reads of a person's vault past a bound of a call (``StoredVault._whole``) the process makes at once: each
# space makes one at a time and shares it (the Task 8 review's I-1), and past this many spaces wait their turn.
WHOLE_READS_AT_ONCE = 2
DELTA_AT_MOST = 5_000  # entries changed since a whole read's base at most, past which the base is made again (r3)
_WHOLE_READS = threading.BoundedSemaphore(WHOLE_READS_AT_ONCE)
# The values a person's vault holds whole at most (``StoredVault.whole_at_most``, ``holds_all``; the owner's ruling of
# 2026-10-31, measured in notes/server-performance-small-vaults.md): where its lookups would make it slower than
# 0.10.0's whole vault. Held whole, it masks a tool's output at about the same speed whatever its size (850-930 lines a
# second from 5,000 to 50,000 values on the bench computer); on its store it masks 340-410 whatever its size, and
# 0.10.0's vault falls as it grows (1,250 at 5,000, 550 at 20,000, 400 at 30,000, 350 at 35,000, 310 at 40,000): on
# its store a vault is about as fast as 0.10.0's at 30,000 values (0.97x), faster from 35,000 (1.07x, 1.26x at
# 40,000). Held whole it costs about 1.6 KB a value (about 58 MB at 35,000, where 0.10.0's took about 90 MB); on its
# store, its working set alone (about 17 MB whatever its size). An API key's vault is never held whole (0): its
# detectors would hold every secret no word finds glued, where on its store they hold those of its working set.
WHOLE_VALUES = 35_000


class _Base:
    """The detector of a whole read's base entities (``_Weighing``), made once and shared by the snapshots made from it,
    under one lock of its own (the re-review's r4: two snapshots never both make it)."""

    def __init__(self, entities: list[Entity], weighing: _Weighing | None = None) -> None:
        self.entities = entities
        self.lock = threading.Lock()
        self._weighing = weighing

    def weighing(self) -> _Weighing:
        with self.lock:
            if self._weighing is None:
                self._weighing = _Weighing(self.entities)
            return self._weighing


class _Whole:
    """A person's whole vault, read once past a bound (``StoredVault._whole_read``), an immutable snapshot shared by
    reference by every call that reads past one (never copied into a call, the re-review's R-I1): its entities by id,
    each by its words, how many characters they weigh, and the detectors a text is weighed against them with, a base
    made once (``_Base``) and a small one of the entries changed since (``delta``), the base's matches of those left
    out. Its place in the store's log of changes (``seq``): every change told after it is applied to it before a call
    reads it (``StoredVault._whole_read``), never one before. ``updated`` makes the next snapshot from the entries a
    change named; past ``EVICT_SHARE`` of the base (or ``DELTA_AT_MOST``) the base is made again in the background
    (``wants_base``), the old one and the delta serving meanwhile. The base and the delta weigh a text's joins
    (which entities a text holds, ``held_in``) as a fresh detector would mask it, not always with the same joins: an
    entry added can leave a base value cut where a fresh detector would cut another (the re-review's r1); the values
    are found by their words all the same (``by_word``), so what is masked is the same."""

    def __init__(
        self,
        key: object,
        seq: int | None,
        entities: Iterable[Entity],
        *,
        base: _Base | None = None,
        delta: dict[str, Entity] | None = None,
        by_word: dict[str, tuple[Entity, ...]] | None = None,
    ) -> None:
        self.key = key
        self.seq = seq
        self.by_id: dict[str, Entity] = {entity.id: entity for entity in entities}
        self.chars = sum(max(len(entity.original), len(entity.surrogate)) for entity in self.by_id.values())
        self.base = base if base is not None else _Base(list(self.by_id.values()))
        self.delta = delta or {}
        if by_word is None:
            found: dict[str, list[Entity]] = {}
            for entity in self.by_id.values():
                for word in _words_of_both(entity):
                    found.setdefault(word, []).append(entity)
            by_word = {word: tuple(entities) for word, entities in found.items()}
        self.by_word = by_word
        self._delta_weighing: _Weighing | None = None
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self.by_id)

    @property
    def wants_base(self) -> bool:
        """Whether its changes since its base passed what a delta should hold: a base made anew, in the background."""
        return len(self.delta) > min(DELTA_AT_MOST, max(1, int(len(self.base.entities) * EVICT_SHARE)))

    def held_in(self, texts: Iterable[str]) -> list[Entity]:
        """The entities ``texts`` hold, as the vault's detectors would find them: the base's, but those changed since,
        and the changed ones' own."""
        texts = list(texts)
        base = self.base.weighing()
        with self._lock:
            if self.delta and self._delta_weighing is None:
                self._delta_weighing = _Weighing(self.delta.values())
            delta = self._delta_weighing
        found = [entity for entity in base.held_in(texts) if entity.id not in self.delta and entity.id in self.by_id]
        return found + (delta.held_in(texts) if delta is not None else [])

    def updated(self, key: object, seq: int, changed: Iterable[Entity]) -> _Whole:
        """The next snapshot, at ``seq``: ``changed`` (the entries the changes since named, as the store holds them now)
        in place of what this one held of them, or added; its base kept (``wants_base``: made again elsewhere)."""
        named = {entity.id: _kept(entity) for entity in changed}
        if not named:
            return _Whole(key, seq, self.by_id.values(), base=self.base, delta=self.delta, by_word=self.by_word)
        by_id = {**self.by_id, **named}
        by_word = dict(self.by_word)
        for entity_id, entity in named.items():
            old = self.by_id.get(entity_id)
            for word in _words_of_both(old) if old is not None else ():
                rest = tuple(e for e in by_word.get(word, ()) if e.id != entity_id)
                if rest:
                    by_word[word] = rest
                else:
                    by_word.pop(word, None)
            for word in _words_of_both(entity):
                by_word[word] = (*by_word.get(word, ()), entity)
        delta = {**self.delta, **named}
        return _Whole(key, seq, by_id.values(), base=self.base, delta=delta, by_word=by_word)

    def rebased(self, base: _Base) -> _Whole:
        """This snapshot over ``base`` (made in the background from an earlier snapshot's entities): the entries that
        differ from it its delta."""
        made = {entity.id: entity for entity in base.entities}
        delta = {i: e for i, e in self.by_id.items() if made.get(i) is not e}
        return _Whole(self.key, self.seq, self.by_id.values(), base=base, delta=delta, by_word=self.by_word)

    def own(self, shared: _Whole | None) -> int:
        """The values this snapshot holds of its own beside ``shared`` (another snapshot of the same vault): all of them
        when they share no base, else its delta alone (the re-review's r5)."""
        if shared is None or shared is self:
            return len(self)
        return len(self.delta) if shared.base is self.base else len(self)


# The values it placed in memory last, remembered as heard of (``_heard_of``): one kept after the vault was made, found
# in the store and not among these, is another instance's whose message never came, and moves the version as one learnt.
_HEARD_KEPT = 10_000
# How far another instance's clock may run behind this one's: a value its ``created`` says was kept up to this long
# before the vault was made may have been kept after it, and is heard of only once placed here. A day: an hour let a
# clock two hours behind leave a value never heard of unheeded (the review of Task 7); what a day costs is one version
# move, at its first join, for each value kept elsewhere in the day before the vault was made.
_CLOCKS_APART = 86_400.0
_WINDOW = 6  # the characters of a held fake's form a text is looked up by (``_HeldForms``): its last six
UNREAD = "the vault's store did not answer: no text is masked or restored without the values it keeps; try again"
ON_THE_LOOP = "an API key's vault is not read on the event loop: its texts are masked in a thread"


class VaultUnread(MaskingError):
    """The store did not answer when asked for the values or fakes a text may hold: nothing is masked or restored
    without them. Its words name no value; its cause is the store's error."""


def _miss(string: str, as_read: bool) -> str:
    """How a string the store said is no fake is remembered (``StoredVault.by_surrogate``): asked as it reads too, as
    it is; asked as written alone, apart (no string of a text starts with NUL's mark)."""
    return string if as_read else "\x00" + string


def found_inside(entity: Entity, role: Role) -> bool:
    """Whether a vault that holds ``entity`` finds its value (``original``) or its fake (``surrogate``) inside a word: a
    value its detectors find there (a secret's, ``KnownValuesDetector``), a fake a restore finds there (one that is no
    word, ``vault.restored_whole``)."""
    if role == "original":
        return entity.eclass == EntityClass.SECRET
    return not restored_whole(entity.etype, entity.eclass)


# The kinds of fake found by their dotted word (``anchors.dotted``, Task 10's F3): an address by its local part, the
# others whole.
_DOTTED_KINDS = frozenset({"EMAIL", "IP_ADDRESS", "DOMAIN", "HOSTNAME"})
_PARTS = re.compile(r"[._-]")  # a dotted word of two runs or more


def fake_word(entity: Entity) -> str | None:
    """The word a store finds ``entity``'s fake by (version 6, Task 10's F3): a fake shaped as an address, an IPv4
    address or a domain by its dotted word (``anchors.dotted``: the IPv4 address and the domain whole), which few fakes
    share, where its anchor is shared by thousands (the domain of the fakes at one company, `example`, an IPv4
    address's first number); an address by its local part with its domain's name (version 8, ``anchors.address_word``:
    `lily.harris@liberty-d05e`), as thousands of fakes at as many companies share a local part in a vault of a
    million; another by its anchor. None for a fake with neither."""
    fake = entity.surrogate
    word = None
    if entity.etype == "EMAIL":
        word = address_word(fake)
    elif entity.etype in _DOTTED_KINDS:
        word = dotted(fake)
    return word if word is not None else anchor(fake)


def value_word(entity: Entity) -> str | None:
    """The word a store finds ``entity``'s value by (version 7, Task 10's F4 and F5): an address that names a person
    by its local part (`maria.rossi`, `mrossi84`: two runs or more, or digits), an IPv4 address and a domain whole
    (``anchors.dotted``), where its anchor is its company's name or its first number, which hundreds or thousands of
    values share; a role's mailbox (`info@`) by its anchor, its domain's name, which it shares with fewer than `info`;
    another by its anchor. None for a value with neither."""
    value = entity.original
    word = None
    if entity.etype == "EMAIL":
        local, at, _ = value.rpartition("@")
        word = dotted(local) if at else None
        if word is not None and not (_PARTS.search(word) or any(c.isdigit() for c in word)):
            word = None  # one run of letters: a role's mailbox, `info`, `sales`
    elif entity.etype in _DOTTED_KINDS:
        word = dotted(value)
    return word if word is not None else anchor(value)


def word_of(entity: Entity, role: Role) -> str | None:
    """The word a store finds ``entity``'s value (``original``: ``value_word``) or fake (``surrogate``: ``fake_word``)
    by."""
    return value_word(entity) if role == "original" else fake_word(entity)


def held_glued(entity: Entity) -> bool:
    """Whether a vault holds ``entity`` in memory for good, one of its newest ``held_at_most`` (``HELD_SECRETS``,
    Ruling 10b-R3), because its value is found inside a word (a secret, of the length the detector of known values
    matches again) where no word of a text may find it glued (``anchors.glued_unfound``: one run of ``ONE_RUN_HELD``
    letters or fewer, or an anchor of ``EDGE`` letters or fewer at its edge; version 8, review 10b's I1). Its fake is
    held with it; a store loads the newest of them apart (``load_held``)."""
    value = entity.original
    return found_inside(entity, "original") and long_enough(value, entity.etype) and glued_unfound(value)


def edges_of(entity: Entity, role: Role) -> tuple[str | None, str | None]:
    """The edges (``anchors.edges``: its head and its tail, Ruling T10h) a store finds ``entity``'s value or fake by
    as well: where the vault finds it inside a word (``found_inside``) and its word is its anchor. A secret held glued
    (``held_glued``) keeps them too: past what a space holds of those (``HELD_SECRETS``), one of seven letters or more
    is found glued by them, including on both sides through bounded interior query windows."""
    string = getattr(entity, role)
    if not found_inside(entity, role) or word_of(entity, role) != anchor(string):
        return None, None
    return edges(string)


def words_of(entity: Entity, role: Role) -> tuple[str, ...]:
    """Every word a store finds ``entity``'s value (``original``) or fake (``surrogate``) by: its word (``word_of``),
    and its edges (``edges_of``), so that a text that glues a letter or a digit to it finds it as a vault that holds it
    does. None for a string without a word."""
    word = word_of(entity, role)
    if word is None:
        return ()
    return (word, *(edge for edge in edges_of(entity, role) if edge is not None))


def glued_both(entity: Entity, role: Role) -> bool:
    """Whether a vault finds ``entity``'s value (``original``) or fake (``surrogate``) inside a word (``found_inside``)
    where it is one run of plain letters and digits (its anchor begins and ends it) longer than ``EDGE``: glued to
    letters or digits on both sides, its letters are inside a run of the text, and no word of the text finds it
    (``anchors.glued_unfound``), where glued on one side its head or its tail does (``edges_of``). A store keeps a head
    and a tail row of that role for it (``KeyVaultStore.load_glued``). A token of forty letters and digits, an IBAN's
    or a card number's fake (Review Focus 3)."""
    head, tail = edges_of(entity, role)
    return head is not None and tail is not None


def unfound_glued(entity: Entity) -> bool:
    """Whether a text that glues letters or digits to ``entity``'s value or fake, where the vault's detectors find it,
    may hold it where none of its words finds it: one glued on both sides (``glued_both``); or a secret, whose value and
    fake the detectors find inside a word, of a short anchor at its edge or of one run of ``ONE_RUN_HELD`` letters or
    fewer (``anchors.glued_unfound``: those held glued for good past ``held_at_most`` of them, 0.10.0's short fakes of
    short secrets), which the detectors of the whole working set found while they were in memory."""
    if glued_both(entity, "original") or glued_both(entity, "surrogate"):
        return True
    return entity.eclass == EntityClass.SECRET and (glued_unfound(entity.original) or glued_unfound(entity.surrogate))


def _held_for_good(entity: Entity) -> bool:
    """Whether a person's vault (``glued_all``) holds ``entity`` for good: no word finds its value or its fake (no
    anchor), or none finds it glued (``glued_both``, ``held_glued``), as ``StoredVault._noted`` decides."""
    if value_word(entity) is None or fake_word(entity) is None:
        return True
    return glued_both(entity, "original") or glued_both(entity, "surrogate") or held_glued(entity)


def _words_of_both(entity: Entity) -> frozenset[str]:
    """Every word a store finds ``entity``'s value or fake by (``words_of``, both roles)."""
    return frozenset((*words_of(entity, "original"), *words_of(entity, "surrogate")))


def _unseen() -> None:
    """A reading that brings nothing into memory (``Matched.seen``): memory's alone."""


def _charged(budget: LookupBudget, entity: Entity) -> None:
    """``entity`` charged to ``budget`` as one more candidate selected (for a text, or for a weighing); past a bound the
    call reads as the release before read (``LookupBudget.relax``), never refusing."""
    try:
        budget.entity(entity)
    except LookupLimitExceeded:
        budget.relax()


def on_a_loop() -> bool:
    """Whether an event loop runs in this thread: there, nothing waits on the store."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@dataclass(slots=True)
class _Call:
    """What one thread's call holds while it is open (``StoredVault.holding``): how deep it is nested, how many values
    were forgotten when it began, and when it began (the vault's clock: a value kept or learnt since stays in memory
    until it is done); each of its texts' words, read once (``words_in``); the words it asked the store for values and
    the values those words found, by each of their words (each weighed against every text of the call, the detector of
    them made once, and the texts weighed against them as they are now); each text's reading (``for_text``) at the
    vault's version; the entities, by id, it found or kept, which stay in memory until it is done; and the fakes its
    texts may hold (``prefetch_fakes``), by the anchor of each, with the
    words they were asked for by. Whether it is an applications call (``scoped``), whether its texts' readings ask the
    store for a word it never asked for (``asks``: not the final check of texts a call masked), whether it asked for
    texts' words first (``prefetched``), and the changes it keeps together in the store (``together``) with the ids of
    the entities they are of (``kept``)."""

    depth: int = 0
    since: int = 0
    began: int = 0
    words: dict[str, frozenset[str]] = field(default_factory=dict)
    asked: set[str] = field(default_factory=set)
    candidates: dict[str, Entity] = field(default_factory=dict)
    by_word: dict[str, list[Entity]] = field(default_factory=dict)
    weighing: _Weighing | None = None
    weighed: set[str] = field(default_factory=set)
    read: dict[str, tuple[int, Matched]] = field(default_factory=dict)
    held: set[str] = field(default_factory=set)
    fakes: dict[str, list[Entity]] = field(default_factory=dict)
    fakes_asked: set[str] = field(default_factory=set)
    scoped: bool = False
    asks: bool = True
    prefetched: bool = False
    together: Together | None = None
    kept: set[str] = field(default_factory=set)
    lookup: LookupBudget = field(default_factory=LookupBudget)
    observed: object = None  # store freshness seen by this call, independent of other calls/vaults
    # Past a bound of a person's call (``StoredVault.whole_past_bounds``): every value of the store, by each of its
    # words (``_whole``), read once for the call.
    whole: _Whole | None = None  # a person's whole vault past a bound, shared by reference (``StoredVault._whole``)
    # Whether memory held every value of the store as it began (``StoredVault.holds_all``): its texts are read from
    # memory alone to its end, and nothing leaves memory while it is open (``StoredVault._alone``).
    alone: bool = False


class _Weighing:
    """Which of some entities a text holds, as the vault's detectors would find them: their values (``fakes`` False:
    those the detectors of values read, a value of a structure's left out) or their fakes, found in the text and in its
    reading with the letters of its escapes blanked out (``read_escaped``'s two readings), and in both once the escapes
    read as what they stand for (``escapes.decoded``, the masker's third reading: a JSON document written in ASCII,
    whose values are masked once its strings are read, and kept so; a value written in HTML's references or a log's
    bytes). A string some entity holds as a secret is found inside a word, as the detectors find a secret; a string is
    found in every form the detectors find it in, its accents composed or decomposed (``terms.forms``); and a fake
    written in escapes is found as it reads too (``vault.fake_reading``: `Giulia Martini` for `Giulia%20Martini`)."""

    def __init__(self, entities: Iterable[Entity], *, fakes: bool = False) -> None:
        self._by: dict[str, list[Entity]] = {}
        for entity in entities:
            if fakes or entity.eclass != EntityClass.STRUCT:
                self._by.setdefault(entity.surrogate if fakes else entity.original, []).append(entity)
            if fakes and (reading := fake_reading(entity)) is not None:  # a fake in escapes, as it reads (0.9.9)
                self._by.setdefault(reading, []).append(entity)
        rule = "echo" if fakes else "known-values"
        found = ((string, *self._kind(string)) for string in self._by)
        self._detector = KnownValuesDetector(found, rule=rule, bounded=True)
        # A match is the string as some text writes it, which the detector finds in each of its forms: each form maps
        # back to the entities of every string it is a form of.
        self._written: dict[str, list[Entity]] = {}
        for string, held in self._by.items():
            for form in forms(string):
                self._written.setdefault(form, []).extend(held)

    def _kind(self, string: str) -> tuple[str, EntityClass]:
        """How ``string`` is looked for: as a secret (inside a word too) when one of its entities is one."""
        entities = self._by[string]
        chosen = next((e for e in entities if e.eclass == EntityClass.SECRET), entities[0])
        return chosen.etype, chosen.eclass

    def held_in(self, texts: Iterable[str]) -> list[Entity]:
        """The entities whose value (or fake) one of ``texts`` holds."""
        if self._detector.empty:
            return []
        found: dict[str, Entity] = {}
        context = DetectContext()
        for text in texts:
            if not text:
                continue
            for reading in _readings(text):
                for span in self._detector.detect(reading, context):
                    for entity in self._written.get(reading[span.start : span.end], ()):
                        found[entity.id] = entity
        return list(found.values())


def _readings(text: str) -> list[str]:
    """``text`` as ``_Weighing`` reads it: as it is, with the letters of its escapes blanked out, and both again where
    its escapes read as what they stand for, as the masker reads them (``escapes.decoded``: JSON's, a log's bytes,
    git's octal ones, a URL's quote, HTML's references; half of a character alone as U+FFFD, which the detectors can
    encode)."""
    found = [text]
    reading = decoded(text)
    if reading is not None and reading.text != text:
        found.append(reading.text)
    for reading in list(found):
        places = escapes(reading)
        if places:
            found.append(blanked(reading, places))
    return found


class _HeldForms:
    """The forms a restore reads the fakes of the values held for good in (``rehydrate.restores_for``, a whole vault's:
    no parts of a name), by their last ``_WINDOW`` characters (a shorter one whole), each with its entity's id: a
    text's candidates among those values are the ones whose fake it holds in one of those forms
    (``StoredVault.candidates_in``), found in one reading of the text. Before (the final review's I1), each of them was
    a candidate of every text and had its restores made again for each: an unmask of 500 log lines ran at 185 lines a
    second with 1,000 secrets held glued, against 6,713 with none. A form a text does not hold restores nothing in it,
    so what a text is restored to is as it was.

    Fakes of one kind share their ending (every address's ends in `.example`), so a window of the text finds the
    lengths of the forms that end with it, and each length's slice of the text is looked up whole (the security
    re-review's R-M1: checking every form under the window, 2,000 held short addresses made a megabyte of `example `
    take 28 s). A text costs its characters times the window lengths held (six at most), and each window it holds
    the lengths of the forms that end with it, however many forms share them.

    Changed under the vault's lock (``add``, ``drop``); read on any thread without it, as the vault's indexes are: what
    a reader gets (a window's lengths, a form's ids, the window lengths) is replaced whole, never changed in place."""

    def __init__(self) -> None:
        self._forms: dict[str, tuple[str, ...]] = {}  # each held entity's forms, by its id
        self._by_form: dict[str, tuple[str, ...]] = {}  # each form, with the ids of the entities it is a form of
        self._ending: dict[str, tuple[int, ...]] = {}  # each window, with the lengths of the forms that end with it
        self._kept: Counter[tuple[str, int]] = Counter()  # how many forms each window has of each length
        self._counts: Counter[int] = Counter()  # how many forms have a window of each length
        self._lengths: tuple[int, ...] = ()  # those lengths, the windows a text is read for

    def add(self, entity: Entity) -> None:
        """``entity``'s forms, in place of those it had (a fake given anew)."""
        self.drop(entity.id)
        forms = tuple(dict.fromkeys(restore.pattern for restore in restores_for([entity], parts=False)))
        self._forms[entity.id] = forms
        for form in forms:
            window = form[max(0, len(form) - _WINDOW) :]
            self._by_form[form] = (*self._by_form.get(form, ()), entity.id)
            if not self._kept[window, len(form)]:
                self._ending[window] = tuple(sorted((*self._ending.get(window, ()), len(form))))
            self._kept[window, len(form)] += 1
            self._counts[len(window)] += 1
        self._lengths = tuple(sorted(length for length, count in self._counts.items() if count > 0))

    def drop(self, entity_id: str) -> None:
        """The forms of ``entity_id``, no longer held for good."""
        forms = self._forms.pop(entity_id, None)
        if not forms:
            return
        for form in forms:
            window = form[max(0, len(form) - _WINDOW) :]
            rest = tuple(found for found in self._by_form.get(form, ()) if found != entity_id)
            if rest:
                self._by_form[form] = rest
            else:
                self._by_form.pop(form, None)
            self._kept[window, len(form)] -= 1
            if self._kept[window, len(form)] <= 0:
                del self._kept[window, len(form)]
                lengths = tuple(length for length in self._ending.get(window, ()) if length != len(form))
                if lengths:
                    self._ending[window] = lengths
                else:
                    self._ending.pop(window, None)
            self._counts[len(window)] -= 1
        self._lengths = tuple(sorted(length for length, count in self._counts.items() if count > 0))

    def held_in(self, text: str) -> set[str]:
        """The ids of the entities whose fake ``text`` holds, in one of the forms a restore reads."""
        found: set[str] = set()
        ending, by_form = self._ending.get, self._by_form.get
        for size in self._lengths:
            for i in range(len(text) - size + 1):
                lengths = ending(text[i : i + size])
                if lengths is None:
                    continue
                end = i + size
                for length in lengths:  # each form that ends here, by its whole slice
                    if length <= end and (ids := by_form(text[end - length : end])) is not None:
                        found.update(ids)
        return found


@dataclass(slots=True)
class _Hold:
    """What a conversation holds in memory for its answers (``StoredVault.hold``): the ids, how many answers are
    awaited, and until when at most."""

    ids: set[str] = field(default_factory=set)
    count: int = 0
    until: float = 0.0


class _TextMatchers:
    """A stored vault's matchers, an API key's or a person's (``StoredVault.matchers``): its detectors hold the values
    ``StoredVault.matched`` names (``ValueMatchers``, over what ``StoredVault.take_changes`` gives of them); a text is
    read for those and for the values and fakes its own words find (``StoredVault.read_for``). Every text is read for
    its detectors, whose values (secrets of one run, of many that start every way) need an automaton of about 520 to
    730 bytes for each byte of their pattern: they are given ``SCALED_PER_BYTE`` (Task 8's fix round 8), and what that
    adds is weighed with the space (``extra_bytes``, ``StoredVault.detectors_bytes``)."""

    def __init__(self, vault: StoredVault) -> None:
        self._vault = vault
        self._parts = ValueMatchers(vault, per_byte=SCALED_PER_BYTE, extra_most=vault.detectors_extra_most)

    @property
    def chars(self) -> int:
        """``ValueMatchers.chars``: those its detectors hold."""
        return self._parts.chars

    @property
    def extra_bytes(self) -> int:
        """What its detectors' RE2 alternations may use past the ordinary budget, in bytes, as last read
        (``ValueMatchers.extra_bytes``)."""
        return self._parts.extra_bytes

    def current(self) -> Matched:
        """The values and fakes its detectors hold, whatever the text."""
        return self._parts.current()

    def for_text(self, text: str, *, pieces: Iterable[str] | None = None) -> Matched:
        """``Matchers.for_text``'s (``StoredVault.read_for``)."""
        return self._vault.read_for(text, self.current(), pieces)


class StoredVault(Vault):
    """An API key's vault: its working set in memory, every value in its store (the module's words)."""

    reads_store = True  # its prefetch asks its store: ``Vault.prefetch`` screens what it is handed first

    # Every caller that masks a text, or checks a masking of it it keeps, in a call asks for the text's words first
    # (``prefetch``: ``Engine.mask_text``, ``Mediator``, ``Apps``, the plugin's bridge, the final check of a text it
    # only checks): a reading then leaves unasked the words of the fakes memory holds (``_asked_now``), which a masked
    # text holds where its values were, and which the text's own found as they were asked. The tests set this
    # (``tests/conftest.py``): a reading that leaves such a word unasked in a call that asked for no text's words fails
    # (the review of Task 7's M4).
    prefetched_first: ClassVar[bool] = False

    def __init__(
        self,
        key: bytes,
        store: KeyVaultStore,
        *,
        working: int = WORKING_VALUES,
        held_at_most: int = HELD_SECRETS,
        glued_all: bool = False,
        whole_past_bounds: bool = False,
        detectors_extra_most: int = DETECTORS_EXTRA_MOST,
        whole_at_most: int = 0,
        counted: int | None = None,
        on_insert: Callable[[Entity], Entity | None] | None = None,
        on_rule: Callable[[Entity], None] | None = None,
        on_learn: Callable[[Entity], None] | None = None,
        on_replace: Callable[[Entity, Entity], Entity | None] | None = None,
    ) -> None:
        """Its values held for good and its ``working`` newest, from ``store``. A value it keeps goes through
        ``on_insert`` (the store's ``save_entity`` when none is given: a value kept nowhere would be lost once it left
        memory), the rule of one through ``on_rule`` (the store's too), as ``Vault``'s; in an applications call that
        keeps them together (``holding``), each value it keeps noted as the call's (a rule is kept at once). Its
        ``held_at_most`` newest secrets held glued (``held_glued``) are held for good, loaded with the rest; with
        ``glued_all`` (a person's space), every value or fake of its store glued on both sides where no word finds it
        (``glued_both``, ``KeyVaultStore.load_glued``) too, never let go. Past a bound of its call's lookup
        (``engine.lookup``) it reads as the release before read, never refusing: its texts' words as 0.10.2 asked them
        (``KeyVaultStore.with_words``, an API key's), or, with ``whole_past_bounds`` (a person's space, which 0.10.0
        held whole), the store's whole vault for the call (``_whole``). ``detectors_extra_most``: what its detectors may
        take past the ordinary RE2 budget at most (``detectors_bytes``). ``whole_at_most``: a store of that many values
        or fewer is held whole (``holds_all``, a person's: ``WHOLE_VALUES``); 0, never (an API key's). ``counted``: how
        many values the store holds, counted just before (``count_entities``: not counted again). A value learnt from
        another instance that ``on_learn`` refuses (its space past its share) while it is held whole lets go of all but
        its working set first, where no call reads memory alone (``_let_go_of_whole``), and is tried once more."""
        self.whole_at_most = whole_at_most
        self._holds_all = False  # every value of the store in memory (``holds_all``), from the build below
        self._rereading = 0  # the values a whole read again holds while it runs (``whole_count``)
        self.whole_past_bounds = whole_past_bounds
        self.detectors_extra_most = detectors_extra_most
        self._shared_whole: _Whole | None = None  # the whole vault read past a bound, shared (``_whole``)
        self._whole_lock = threading.Lock()  # one whole read of this vault at a time
        self._whole_epoch = 0  # moved by each forget here: the shared whole read is read whole again after one
        self._rebasing = False  # a base of the shared whole read being made in the background
        self._building: _Base | None = None  # that base, weighed while it is made (``whole_count``)
        self._store = store
        insert = on_insert if on_insert is not None else store.save_entity

        def inserted(entity: Entity) -> Entity | None:
            first = insert(entity)
            if first is None:  # kept as given: the call's own, until it commits
                self._noted_in_call(entity.id)
                # New to the store: a call under way asked for its words before, and holds it until it is done.
                self._kept_at[entity.id] = next(self._clock)
            return first

        self.working = working
        self.held_at_most = held_at_most
        self.glued_all = glued_all
        self._glued: OrderedDict[str, None] = OrderedDict()  # the secrets held glued, the newest last (``_noted``)
        self._clock = itertools.count(1)
        self._used: dict[str, int] = {}  # each entity in memory, by id: when it was used last
        self._for_good: set[str] = set()  # the entities no word of a text finds (``_noted``): never let go
        self._held_forms = _HeldForms()  # the forms of the fakes no word finds glued, in a text (``candidates_in``)
        self._matched: set[str] = set()  # the entities in memory its detectors hold (``matched``), by id
        # Each word of the values and fakes in memory (``words_of``), with the ids of those it is a word of, replaced
        # whole under the lock and read without it (``read_for``); and each entity's words, as they were indexed.
        self._by_word: dict[str, tuple[str, ...]] = {}
        self._words_held: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {}
        self._fake_words: Counter[str] = Counter()  # the words of the fakes in memory: how many have each
        self._kept_at: dict[str, int] = {}  # each value kept or learnt in memory, by id: when (the clock)
        self._built = time.time()  # a value kept after it was made, never placed here, it never heard of (``_join``)
        # The words of the entries its calls found (``_both_words``), by id, each with a digest of what it was worked
        # out of, keyed by this vault's own key (``_made_of``), and what it weighs; and what they weigh together, with
        # the space (``words_bytes``).
        self._words_memo: OrderedDict[str, tuple[bytes, frozenset[str], int]] = OrderedDict()
        self._words_memo_lock = threading.Lock()
        self._memo_key = secrets.token_bytes(32)
        self.words_bytes = 0
        self._heard: OrderedDict[str, None] = OrderedDict()  # the values it placed in memory last (``_HEARD_KEPT``)
        self._texts: _TextMatchers | None = None
        self._calls: dict[int, _Call] = {}  # the calls under way, by their thread (each thread changes its own alone)
        self._misses: OrderedDict[str, None] = OrderedDict()  # strings the store said are no fake
        self._misses_lock = threading.Lock()  # held for no store call: the event loop may take it
        self._placed = 0  # entities placed in memory so far: a miss read while one was placed is not remembered
        # How far the store's tellings of what changed by name were taken in (``refresh``): read before its freshness,
        # and both before memory is read below, so that a change after either is told after it.
        self._named_seen: int = getattr(store, "named_seq", 0)
        self._observed_store = getattr(store, "freshness", None)
        self._gone: OrderedDict[str, int] = OrderedDict()  # values forgotten, by id: when (``_forgets``)
        self._forgets = 0
        self._resynced = 0  # a full resync revokes even in-flight reads of ids that were out of memory
        self._undone = 0  # the version once it last undid what a call did not keep (``_undo``)
        self._stuck = 0  # past the working set by values it may not let go: tried again past this many
        self._left_at = 0  # when it last stopped holding every value (``_left_whole``: the clock), 0 never
        self._resyncing = threading.Lock()  # one resync at a time (``resync``)
        self._sessions: dict[str, _Hold] = {}  # what conversations hold for their answers (``hold``)
        self._sessions_lock = threading.Lock()  # held for no store call: the event loop releases
        held = {e.id: e for e in store.load_unanchored()}
        if glued_all:
            for e in store.load_glued():  # the oldest first
                held.setdefault(e.id, e)
        load_held = getattr(store, "load_held", None)  # a store that keeps the secrets held glued apart
        wanted = self._held_wanted
        for e in reversed(load_held(wanted) if load_held is not None and wanted else []):
            held.setdefault(e.id, e)  # the oldest first, so that the newest are kept (``_noted``)
        recent, whole = self._recent(store, working, counted)
        for e in reversed(recent):  # the oldest first: the newest is the one used last
            held.setdefault(e.id, e)
        super().__init__(
            key,
            held.values(),
            on_insert=inserted,
            on_rule=on_rule if on_rule is not None else store.save_rule,
            on_learn=self._learning(on_learn),
            on_replace=on_replace,
        )
        with self._lock:
            self._holds_all = whole and len(self._by_key) <= whole_at_most
            self._trim()

    def _recent(self, store: KeyVaultStore, working: int, counted: int | None) -> tuple[list[Entity], bool]:
        """The values it holds from its build besides those held for good, the newest first: every one, and True, when
        the store holds ``whole_at_most`` or fewer (counted first, ``counted`` where its builder just did, so that a
        large vault reads its working set alone), else its ``working`` newest."""
        if self.whole_at_most > 0 and counted is None:
            counted = store.count_entities()
        if self.whole_at_most > 0 and counted is not None and counted <= self.whole_at_most:
            found = store.load_recent(self.whole_at_most + 1)
            if len(found) <= self.whole_at_most:
                return found, True
            return found[:working], False  # grown past it since it was counted
        return store.load_recent(working), False

    def _learning(self, on_learn: Callable[[Entity], None] | None) -> Callable[[Entity], None] | None:
        """``on_learn`` (a value another instance kept, ``Vault.learn``), or None. Refused while it is held whole (its
        space past its share, ``runtime.NewValues.learn``), it lets go of all but its working set
        (``_let_go_of_whole``) and is asked once more: a vault held whole near its share learns where its working set
        alone fits, where its whole memory would have it refuse every text until its space is made again (the review
        of 2026-11-01, M2). Refused again, or with a call open, the refusal stands."""
        if on_learn is None:
            return None

        def learning(entity: Entity) -> None:
            try:
                on_learn(entity)
            except MaskingError:
                if not self._let_go_of_whole():
                    raise
                on_learn(entity)

        return learning

    def _let_go_of_whole(self) -> bool:
        """Held whole no longer, memory cut back to its working set (under the lock), where no call is open: a call
        begun while it held every value reads memory alone to its end, and nothing leaves memory while one begun before
        is open (``_trim``). Whether it let any value go."""
        if not self._holds_all or self._calls:
            return False
        before = len(self._by_key)
        self._left_whole()
        self._stuck = 0
        self._trim()
        return len(self._by_key) < before

    # --- what it holds -----------------------------------------------------------------------------

    @property
    def _held_wanted(self) -> int:
        """How many of the secrets held glued (``held_glued``) it asks its store for: every one with ``glued_all`` (a
        person's space, Ruling T9-S3), else its newest ``held_at_most``."""
        return _EVERY_HELD if self.glued_all else self.held_at_most

    @property
    def holds_all(self) -> bool:
        """Whether it holds every value of its store in memory (``whole_at_most``, the module's words): read whole from
        its build while the store held that many or fewer, until it holds more."""
        return self._holds_all

    def _alone(self, call: _Call | None) -> bool:
        """Whether memory alone answers what ``call`` asks of the store (outside a call: a reading of now): memory held
        every value of the store as it began (``_Call.alone``), and has let none go since (``_trim``)."""
        return call.alone if call is not None else self._holds_all

    def _held_by(self, roles: Role | tuple[Role, ...], words: Iterable[str]) -> list[Entity]:
        """What its store answers ``with_words(roles, words)``, from memory alone (``holds_all``): the entities in
        memory one of whose words of ``roles`` (``words_of``, a value's or a fake's) is one of ``words``."""
        wanted = (roles,) if isinstance(roles, str) else roles
        fakes, values = "surrogate" in wanted, "original" in wanted
        by_word, by_id, held = self._by_word, self._by_id, self._words_held
        found: dict[str, Entity] = {}
        for word in words:
            for entity_id in by_word.get(word, ()):
                entity, kept = by_id.get(entity_id), held.get(entity_id)
                if entity is None or kept is None or entity_id in found:
                    continue
                # A word of an entity is its value's, its fake's, or both (``_index_words``).
                of_fake = word in kept[1]
                if (fakes and of_fake) or (values and (not of_fake or word in words_of(entity, "original"))):
                    found[entity_id] = entity
        return list(found.values())

    def entities(self) -> list[Entity]:
        """The working set: the values in memory, not the whole vault, which its store alone holds."""
        return super().entities()

    def holds_since(self, version: int) -> bool:
        """Always: memory never vouches for a text alone, as each check of one reads its words (``read_for``), which
        find a value out of memory in the store."""
        return True

    def kept_since(self, version: int) -> bool:
        return version >= self._undone

    @property
    def detectors_bytes(self) -> int:
        """What its detectors' RE2 alternations were granted past the ordinary budget (``_TextMatchers.extra_bytes``),
        weighed with its space: about a megabyte for each thousand bytes of the secrets they hold and of their fakes, 0
        before they are made, never more than ``detectors_extra_most``."""
        texts = self._texts
        return texts.extra_bytes if texts is not None else 0

    @property
    def matchers(self) -> Matchers:
        """Its detectors of the values it holds whatever the text (``matched``), and each text's reading of the values
        its words find (``read_for``)."""
        found = self._texts
        if found is None:
            with self._lock:
                if self._texts is None:
                    self._texts = _TextMatchers(self)
                found = self._texts
        return found

    def matched(self, entity: Entity) -> bool:
        """Whether its detectors hold ``entity`` while it is in memory, as every text is read for those: held for good
        (no word finds it, or it is held glued), or a secret no word of a text finds glued to letters or digits
        (``unfound_glued``: glued on both sides, ``glued_both``, or of a short anchor at its edge), which the detectors
        find inside a word, its value and its fake. Another is found by the words of each text that holds it: a fake
        of personal data no word finds glued (an IBAN's) the detectors find only as a whole word, where its word finds
        it too, and its restores read it glued (``_HeldForms``, the review of Task 7's M3)."""
        return entity.id in self._for_good or (entity.eclass == EntityClass.SECRET and unfound_glued(entity))

    def take_changes(
        self, *, whole: bool = False
    ) -> tuple[int, list[Entity] | None, dict[tuple[str, str], Entity | None]]:
        """``Vault.take_changes``'s, for its detectors (``matchers``), narrowed to the entities they hold
        (``matched``, as each was noted in memory): one changed that they do not hold is answered as gone from them."""
        with self._lock:
            version, everything, changed = super().take_changes(whole=whole)
            if everything is not None:
                return version, [e for e in everything if e.id in self._matched], {}
            held = self._matched
            return version, None, {k: e if e is not None and e.id in held else None for k, e in changed.items()}

    @contextlib.contextmanager
    def holding(self, *, scoped: bool = False, memory_only: bool = False) -> Iterator[None]:
        """While it is open, what this thread brings into memory (``prefetch``, the lookups) or keeps stays there, out
        of reach of the values let go, and the words it asked the store for are not asked again: a call (the texts of
        one request of the applications API, one text the engine masks) holds its values until it is done, and those
        kept or learnt while it is open (another thread's, whose words it asked for before). Nested, it is one call;
        the working set is cut back as the outermost closes. The outermost asks the store first whether values kept
        elsewhere were taken in since the last call (``KeyVaultStore.current``, off the event loop): the version moves
        when they were, and a text masked before is read again. ``scoped`` (the outermost's: an applications call): the
        words of its texts are asked for the space's fakes with their values (``prefetch``), and the values it keeps
        are kept together where the store can (its ``together``, Ruling T10i: one transaction and one commit for the
        call, where each value cost its own), committed as it closes. Each of its texts is read for the values and fakes
        its words find and for those the vault's detectors hold (``read_for``, the plan's S4); ``memory_only`` (the
        final check of texts a call masked) uses memory alone unless the store observes other instances' commits:
        that final check refreshes and queries again under the same call budget. A call that ends in an error, or whose
        commit fails, keeps none of them, and memory
        forgets them (``_undo``): no text leaves masked with a fake the store does not keep. A commit that fails fails
        the call (``VaultUnread``)."""
        # No lock to open or close one, but to cut the working set back: a thread's call is its own entry, and a text
        # masked on one thread waits for no value another keeps meanwhile.
        thread = threading.get_ident()
        call = self._calls.get(thread)
        if call is None:
            budget = LookupBudget()
            self.refresh(budget=budget)
            call = self._calls[thread] = _Call(
                since=self._forgets,
                began=next(self._clock),
                asks=not memory_only or self._observed_store is not None,
                lookup=budget,
            )
            # Read once the call is among those under way (``_trim``: a vault that let go of its whole memory since
            # this call began lets nothing go until it ends), and once what changed elsewhere was taken in.
            call.alone = self._holds_all
            call.observed = self._observed_store
            if scoped:
                call.scoped = True
                together = getattr(self._store, "together", None)
                if together is not None:
                    call.together = together()
        elif memory_only and self.rechecks_store:
            self.refresh()  # a nested final verifier observes commits since the outer call began
        call.depth += 1
        failed = True
        try:
            yield
            call.lookup.check(0, 0)  # a caught inner refusal still rejects and rolls back the whole operation
            failed = False
        finally:
            call.depth -= 1
            if call.depth == 0:
                try:
                    if call.together is not None:
                        self._ended(call, failed)
                finally:
                    del self._calls[thread]
                    if len(self._by_key) > self.working and not self._holds_all:  # held whole: nothing to let go
                        with self._lock:
                            self._stuck = 0  # what it held may go now
                            self._trim()

    def _call(self) -> _Call | None:
        """This thread's call under way, if any."""
        return self._calls.get(threading.get_ident())

    def refresh(self, *, budget: LookupBudget | None = None) -> bool:
        """Observe commits before this check, even inside an open call and after background catch-up.

        Revoke both positive and negative answers. A call retains its original cumulative LookupBudget and
        generated words; only lookup/matching answers become stale. Commits after this check belong to the next
        check, rather than an unbounded retry loop under continuous writers.
        """
        current = getattr(self._store, "current", None)
        if current is None or on_a_loop():
            return False
        changed = self._asked(current)
        observed = getattr(self._store, "freshness", None)
        if observed is not None:
            changed = observed != self._observed_store
        call = self._call()
        call_changed = call is not None and observed is not None and call.observed != observed
        if changed:
            self._asked(lambda: self._caught_up(observed, budget))
        if call is not None and (changed or call_changed):
            call.asked.clear()
            call.candidates.clear()
            call.by_word.clear()
            call.whole = None  # a person's whole vault, read again past a bound as it is now
            call.weighing = None
            call.weighed.clear()
            call.read.clear()
            call.fakes.clear()
            call.fakes_asked.clear()
            call.asks = True
            call.since = self._forgets
            call.observed = observed
            # Held whole no longer (past its bound since the call began): what changed elsewhere was taken in as a vault
            # on its store takes it in (by its words, not into memory), so the call's texts are read through the store
            # from now on (the review of the change of 2026-10-31); nothing it read leaves memory before it ends.
            call.alone = call.alone and self._holds_all
        return changed or call_changed

    def _caught_up(self, observed: object, budget: LookupBudget | None) -> None:
        """What changed elsewhere since this vault last confirmed its memory, taken in (``resync``), one call at a time
        (``_resyncing``) and once (the review of 2026-11-01, I3): the calls that saw the same change wait for the first
        one's resync, then find the freshness they saw taken in, or nothing told past what it confirmed, and read
        nothing. What changed by name is read after the freshness it saw move (told with its move, and read only once
        the move is done): the resync confirms those alone, or the whole memory where the store does not know them by
        name (``PgVaultStore.named_after``: a forget, a barrier, a count read with no index). The freshness and how far
        it confirmed are set under the same lock, so that the next call to wait sees them."""
        named_after = getattr(self._store, "named_after", None)
        with self._resyncing:
            if observed is not None and observed == self._observed_store:
                return  # taken in by another call's resync meanwhile
            told, only = named_after(self._named_seen) if named_after is not None else (self._named_seen, None)
            if only is not None and not only and told == self._named_seen:
                self._observed_store = observed  # nothing told past what memory confirmed: nothing to read
                return
            self._resynced_now(False, budget, only)
            self._observed_store = observed
            self._named_seen = max(self._named_seen, told)

    def whole_vault_call(self) -> bool:
        """``Vault.whole_vault_call``'s: this thread's call is an applications call (``holding(scoped=True)``)."""
        call = self._call()
        return call is not None and call.scoped

    @property
    def rechecks_store(self) -> bool:
        """A store with independently observed commits must also be queried by the final verifier."""
        return self._observed_store is not None

    def _noted_in_call(self, entity_id: str) -> None:
        """The value of ``entity_id``, kept as given: the store's once this thread's call that
        keeps its changes together commits: undone in memory if it does not (``_undo``)."""
        call = self._calls.get(threading.get_ident())
        if call is not None and call.together is not None and not getattr(call.together, "at_once", False):
            call.kept.add(entity_id)  # (at once: the instance had no slot for it, and what it kept is kept, review N4)

    def _ended(self, call: _Call, failed: bool) -> None:
        """``call``'s changes kept together: committed, unless it ``failed`` (rolled back, its error raised as it is);
        a commit that fails rolls them back, and raises VaultUnread. Either way, what was not kept leaves memory."""
        together = call.together
        assert together is not None
        if failed:
            together.rollback()
            self._undo(call)
            return
        try:
            together.commit()
        except Exception as exc:
            self._undo(call)
            raise VaultUnread(UNREAD) from exc

    def _undo(self, call: _Call) -> None:
        """The entities ``call`` kept in the store, out of memory (their changes were rolled back): read
        again from the store as a text needs them, as it has them. No text masked before is sent again as it was
        masked (``kept_since``: it may hold a fake of theirs)."""
        with self._lock:
            undone = [entity for entity_id in call.kept if (entity := self._by_id.get(entity_id)) is not None]
            self._forget_words(call.kept)
            for entity in undone:
                self._unindex(entity)
            if undone:
                self._moved()
                self._undone = self.version

    # --- masking -----------------------------------------------------------------------------------

    def _prefetch(self, texts: Sequence[str]) -> None:
        """The values ``texts`` hold, brought into memory through bounded store batches: those whose anchor is one of
        their words (``words_in``, each text's read once a call), but for the words this call asked for already, and
        of those the ones a text holds alone, as the vault's detectors would find them (Ruling T8a): the namesakes of a
        value stay out, and the working set is not flushed by a word many values share. Within a call, every value its
        words found is weighed against each text it masks (a value of a JSON document, read from the request's text),
        with no other call to the store, and kept with the call for its texts' readings (``read_for``); a text weighed
        against them as they are now is not weighed again. An applications call (``scoped``) asks for the fakes of the
        space too, in the same statement (review 10b's d): a text's reading finds one it holds, as a value of its own
        (the spec: an already masked text is not recognised), and brings its entity into memory (``Matched.seen``).
        Raises VaultUnread when the store does not answer, and MaskingError on the event loop: no text is masked without
        its known values. Held whole (``_alone``), nothing: memory holds every value its texts' words would find, and
        their readings find them there."""
        call = self._call()
        if self._alone(call):
            if call is not None:
                call.prefetched = True
            return
        budget = call.lookup if call is not None else LookupBudget()
        words: set[str] = set()
        for text in texts:
            if text:
                words |= self._words(text, call, budget)
        new = words - call.asked if call is not None else words
        found: list[Entity] = []
        since = self._forgets
        if new:
            if on_a_loop():
                raise MaskingError(ON_THE_LOOP)
            roles = self._roles(call)
            found, answered = self._lookup(roles, new, budget, call)
            new = answered
        if call is None:
            for entity in found:
                _charged(budget, entity)
            held = _Weighing(found).held_in(texts)
        else:
            call.prefetched = True
            call.asked |= new
            if found:
                self._candidates(call, found)
            texts = [text for text in texts if text and text not in call.weighed]
            if not texts or not (call.candidates or call.whole is not None):
                return
            held, since = self._weighed(call, texts), call.since
            call.weighed.update(texts)
        if not held:
            return
        with self._lock:
            self._join(held, since)
            self._trim()

    @staticmethod
    def _roles(call: _Call | None) -> Role | tuple[Role, ...]:
        """What a call asks the store for by its words: an applications call the values and the fakes of the space (one
        statement for both), another the values."""
        return ("original", "surrogate") if call is not None and call.scoped else "original"

    def _words(
        self, text: str, call: _Call | None, budget: LookupBudget, pieces: Iterable[str] | None = None
    ) -> frozenset[str]:
        """``text``'s words (``words_in``), read once a call: kept with ``call``. ``pieces``: the texts ``text`` is
        joined of (``Masker.joined``), each read where its escapes read on its own, a run of letters ending at the
        blank line between two: their words, each text's read once."""
        if call is not None and (kept := call.words.get(text)) is not None:
            return kept
        if pieces is not None:
            found = frozenset().union(*(self._words(piece, call, budget) for piece in pieces))
        else:
            plain, windows = words_and_windows(text, dotted=True, interior=budget.admit_windows)
            budget.plain |= plain
            found = frozenset(plain | windows)
        if call is not None:
            call.words[text] = found
        return found

    def _lookup(
        self, roles: Role | tuple[Role, ...], words: Iterable[str], budget: LookupBudget, call: _Call | None = None
    ) -> tuple[list[Entity], set[str]]:
        """The entities whose word (of ``roles``) is one of ``words``, and the words answered. Within the call's bounds
        (``engine.lookup``) the fast lookup, each statement bounded by the store itself (a production store charges
        rows before opening them; a simple in-memory one returns entities, charged here): the words and their interior
        windows at once. Past a bound it reads as the release before, never refusing: windows past theirs (their share
        of the word roles, or the candidates, characters or bytes of the lookup that holds them) are dropped whole, the
        words 0.10.x read (``LookupBudget.plain``) asked alone, and the windows left unanswered, so that a later text
        whose word one of them is asks for it; past a bound of those words, every word is read as 0.10.2 asked it (an
        API key's, whose ``TooManyCandidates`` stays where it was) or from the store's whole vault (a person's,
        ``whole_past_bounds``: ``_past``). Held whole (``_alone``), memory answers every word as the store would
        (``_held_by``), whatever the bounds: no lookup, nor a whole read past a bound."""
        wanted = set(words)
        if self._alone(call):
            found = self._held_by(roles, wanted)
            # Outside a call nothing holds memory back: still held whole once read, none was let go meanwhile.
            if call is not None or self._holds_all:
                return found, wanted
        count = 1 if isinstance(roles, str) else len(roles)
        if budget.past:
            return self._past(roles, wanted, budget, call)
        plain = {word for word in wanted if word in budget.plain}
        if len(plain) < len(wanted) and not budget.windows_off:
            # The words and their windows in one lookup, charged only once its whole answer fits; past a bound, or
            # the windows past their share of the word roles, the windows are dropped whole and the words asked alone.
            if budget.words + len(wanted) * count <= lookup.LOOKUP_WORD_ROLES:
                trial = copy.copy(budget)
                trial.words += len(wanted) * count
                try:
                    found = self._asked_bounded(roles, wanted, trial)
                except (LookupLimitExceeded, TooMany):
                    pass
                else:
                    budget.words, budget.candidates = trial.words, trial.candidates
                    budget.characters, budget.bytes = trial.characters, trial.bytes
                    return found, wanted
            budget.windows_off = True
        if budget.words + len(plain) * count > lookup.LOOKUP_WORD_ROLES:
            budget.relax()
            return self._past(roles, wanted, budget, call)
        budget.words += len(plain) * count
        try:
            found = self._asked_bounded(roles, plain, budget)
        except (LookupLimitExceeded, TooMany) as exc:
            if isinstance(exc, TooMany) and not self.whole_past_bounds:
                raise  # 0.10.2's refusal of an API key's call (422), where it had it
            budget.relax()
            return self._past(roles, wanted, budget, call)
        return found, plain

    def _asked_bounded(self, roles: Role | tuple[Role, ...], words: set[str], budget: LookupBudget) -> list[Entity]:
        """The fast lookup of ``words``, each answer bounded (``_lookup``): raises LookupLimitExceeded past a bound."""
        wanted = sorted(words)
        count = 1 if isinstance(roles, str) else len(roles)
        bounded = getattr(self._store, "bounded_words", None)
        found: list[Entity] = []
        # A store that bounds each statement it sends itself (``lookup_batch`` None: PostgreSQL's, ``WORDS_AT_ONCE``
        # words or prints a statement) is asked once for all of them: one connection and one round trip where the
        # words' prints the index holds fit one statement, rather than one each LOOKUP_BATCH words.
        batch = getattr(self._store, "lookup_batch", LOOKUP_BATCH)
        size = max(1, len(wanted)) if bounded is not None and batch is None else max(1, LOOKUP_BATCH // count)
        for at in range(0, len(wanted), size):
            part = wanted[at : at + size]
            if bounded is not None:
                rows = self._asked(partial(bounded, roles, part, budget))
            else:
                rows = self._asked(partial(self._store.with_words, roles, part))
                for entity in rows:
                    budget.entity(entity)
            found.extend(_kept(entity) for entity in rows)
        return found

    def _past(
        self, roles: Role | tuple[Role, ...], words: set[str], budget: LookupBudget, call: _Call | None
    ) -> tuple[list[Entity], set[str]]:
        """``words`` read as the release before read them, past a bound of the call (``_lookup``), and the words
        answered: a person's from the store's whole vault (``_whole``: windows too, which it answers as every other
        word), an API key's as 0.10.2 asked them (``KeyVaultStore.with_words``: past ``WORDS_ASKED_AT_MOST`` words, the
        store's whole index; past ``CANDIDATES_AT_MOST`` entries, ``TooManyCandidates``), its windows left out and
        unanswered (0.10.2 had none: a later text whose word one of them is asks for it)."""
        if self.whole_past_bounds:
            whole = self._whole(call)
            found: dict[str, Entity] = {}
            for word in words:
                for entity in whole.get(word, ()):
                    found.setdefault(entity.id, entity)
            return list(found.values()), words
        plain = {word for word in words if word in budget.plain}
        if not plain:
            return [], plain
        asked = sorted(plain)
        return [_kept(entity) for entity in self._asked(partial(self._store.with_words, roles, asked))], plain

    def _whole(self, call: _Call | None) -> dict[str, tuple[Entity, ...]]:
        """Every value of the store (``whole_entities``, else ``load_entities``: bounded by the vault's limit, as 0.10.0
        held a person's vault whole), by each of its words (``_words_of_both``), the snapshot shared by the calls
        (``_whole_read``); in a call, held by reference (``call.whole``): each of its texts is weighed against the
        snapshot's detectors (``_weighed``) and read for its values (``_found_by``) with no other lookup. None of its
        entities is copied into the call's candidates, nor worked out through the words memo (``_candidates``): its
        words are the snapshot's own. Not on the event loop."""
        if call is not None and call.whole is not None:
            return call.whole.by_word
        if on_a_loop():
            raise MaskingError(ON_THE_LOOP)
        shared = self._whole_read()
        if call is not None:
            call.whole = shared  # by reference: weighed while the call holds it (``whole_count``)
            call.weighed.clear()  # the texts weighed so far weighed again against it
        return shared.by_word

    def _whole_read(self) -> _Whole:
        """The whole vault as the store holds it now, shared by the calls that read past a bound (the Task 8 review's
        I-1): one read of this vault at a time, the others waiting for it, ``WHOLE_READS_AT_ONCE`` in the process,
        through the store's reader that changes nothing (``whole_entities``). A snapshot has its place in the store's
        log of changes (``seq``, read before its whole read): each call past a bound applies the changes told since
        that place, the entries they name read again by their ids (the re-review's R2-C1: a snapshot never claims a
        change it did not apply, and nothing is done for it on calls that do not read past a bound, r2); where the log
        names nothing (a forget elsewhere, a barrier, a log that no longer reaches back) or this vault forgot a value,
        it is read whole again. A store with no log: read again whole once what it holds moved (``freshness``), or at
        every call where it does not say."""
        named_after = getattr(self._store, "named_after", None)
        with self._whole_lock:
            shared = self._shared_whole
            if shared is not None and shared.key == self._whole_epoch and named_after is not None:
                assert shared.seq is not None
                told, names = named_after(shared.seq)
                if names is not None:
                    if told != shared.seq:
                        ids = sorted(names)
                        budget = LookupBudget()
                        bounded = getattr(self._store, "bounded_ids", None)
                        fresh = self._bounded_or_whole(bounded, budget, lambda: self._store.by_ids(ids), ids)
                        shared = self._shared_whole = shared.updated(shared.key, told, fresh)
                    self._rebase_later(shared)
                    return shared
            elif shared is not None and named_after is None and shared.seq is None:
                # A store with no log that cannot say what it holds either (no ``freshness``) is read whole again at
                # each call past a bound, as Task 8's round 6 read it: a snapshot kept would miss a value kept since.
                freshness = getattr(self._store, "freshness", None)
                if freshness is not None and shared.key == (freshness, self._whole_epoch):
                    return shared
            self._shared_whole = None
            seq = getattr(self._store, "named_seq", None) if named_after is not None else None  # before the read
            key = (
                self._whole_epoch
                if named_after is not None
                else (getattr(self._store, "freshness", None), self._whole_epoch)
            )
            reader = getattr(self._store, "whole_entities", None)
            with _WHOLE_READS:
                entities = [_kept(entity) for entity in self._asked(reader or self._store.load_entities)]
            shared = self._shared_whole = _Whole(key, seq, entities)
            return shared

    def _rebase_later(self, shared: _Whole) -> None:
        """``shared``'s base made again in a thread of its own once its changes passed what a delta holds
        (``_Whole.wants_base``): the old base and the delta serve meanwhile, no request waits for it (the re-review's
        r3), and the snapshot shared then is given it while it still stands on the base this one was made beside
        (round 4's q1: else the snapshot was read whole again meanwhile, and the base made here is let go). Weighed
        while it is made (``_building``), one of the process's ``WHOLE_READS_AT_ONCE`` (q2)."""
        if not shared.wants_base or self._rebasing:
            return
        self._rebasing = True
        base = self._building = _Base(list(shared.by_id.values()))
        old = shared.base

        def made() -> None:
            try:
                with _WHOLE_READS:
                    base.weighing()
                with self._whole_lock:
                    current = self._shared_whole
                    if current is not None and current.base is old:
                        self._shared_whole = current.rebased(base)
            finally:
                self._building = None
                self._rebasing = False

        threading.Thread(target=made, name="latet-whole-base", daemon=True).start()

    @property
    def whole_count(self) -> int:
        """The values of the whole vault it holds read past a bound (``_whole_read``): the snapshot shared now, and what
        an older one a call under way still holds has of its own beside it (the re-review's R-I1 and r5); none when it
        holds none."""
        shared = self._shared_whole
        building = self._building
        made = len(building.entities) if building is not None else 0  # the base being made (q2)
        made += self._rereading  # a vault held whole read whole again, while it runs (``resync``)
        return made + sum(whole.own(shared) for whole in self._wholes())

    @property
    def whole_tables(self) -> int:
        """The entries of the lookup tables of the older snapshots calls under way still hold (their own ``by_id`` and
        ``by_word``, which ``whole_count`` leaves out where they share their values: round 4's q2)."""
        shared = self._shared_whole
        return sum(len(whole.by_id) + len(whole.by_word) for whole in self._wholes() if whole is not shared)

    @property
    def whole_chars(self) -> int:
        """The characters of those values (``whole_count``), the longer of each value and its fake."""
        shared = self._shared_whole
        return sum(whole.chars if whole.own(shared) == len(whole) else 0 for whole in self._wholes())

    def _wholes(self) -> list[_Whole]:
        """The snapshots of the whole vault it holds: the shared one and those calls under way hold, each once."""
        found = {id(whole): whole for call in list(self._calls.values()) if (whole := call.whole) is not None}
        shared = self._shared_whole
        if shared is not None:
            found[id(shared)] = shared
        return list(found.values())

    def _candidates(self, call: _Call, found: Iterable[Entity]) -> None:
        """``found`` (what the store answered the call's words), kept with ``call``, each by its words
        (``_both_words``): the texts weighed so far are weighed again against them. But what the call's words found in
        the whole vault it reads past a bound (``call.whole``, the snapshot's own entities): its texts are read for
        those through the snapshot (``_found_by``) and weighed by its detectors (``_weighed``), so no copy of them is
        kept with the call, nor a detector of the call's own made of them, which the space never weighed, nor their
        words worked out through the memo, which a whole vault would empty of the entries the calls find again (the
        review of the merge of Task 9, I1)."""
        whole = call.whole.by_id if call.whole is not None else None
        added = False
        for entity in found:
            if entity.id in call.candidates or (whole is not None and whole.get(entity.id) is entity):
                continue
            added = True
            call.candidates[entity.id] = entity
            for word in self._both_words(entity, call.since):
                call.by_word.setdefault(word, []).append(entity)
        if added:  # the same candidates weigh a text the same: nothing to weigh again
            call.weighing = None
            call.weighed.clear()

    def _both_words(self, entity: Entity, since: int | None = None) -> frozenset[str]:
        """``_words_of_both(entity)``, kept for the next call that finds the entry (``WORDS_MEMO_SHARE``). Those words
        are a function of the entry's value, fake, type and class alone, with no side effect (``words_of``: its word,
        ``value_word`` or ``fake_word``, and its edges, ``edges_of``, of the strings and of ``found_inside``'s type and
        class, through ``anchors``' pure functions and constant tables): kept by the entry's id with a digest of those
        four (``_made_of``), and worked out anew for an entry that differs in any of them, never answered with an older
        entry's. ``since``: when the call that asks began (``_Call.since``).

        What the memo holds, and for how long (Task 8's fix round 8, the review of round 7's I-1):

        - What an entry is checked against is not its value and fake but a digest of them, keyed by this vault's own
          key, drawn at random when it is made and kept in this process alone: no one can check a guessed value
          against it without that key.
        - Its words, in folded case, are parts of them (an anchor, an address's local part with its domain's name, a
          secret's first and last ``EDGE`` letters), or the whole string where that is its word (a domain, an IPv4
          address, one run of letters and digits). They stay until the least recently used goes past as many as
          memory holds, or the entry is forgotten or changed: at once when this instance forgets it or hears that it
          was forgotten or changed (``_forget_words``: ``forget``, ``forgotten``, a resync's entries changed, a call
          undone), and at the next whole resync when it is not told (a forget on another instance whose message was
          lost: every change the store does not name is read as a whole resync, which empties the memo, ``resync``).
        - Nothing more from a call that found the entry before either: an entry forgotten or changed here since
          (``_gone``) is not kept, nor one found by a call begun before the last whole resync (``since``), whose
          lookups may hold an entry forgotten elsewhere meanwhile. Both are told before the memo is emptied of them,
          and read under its lock.

        What it holds is weighed with the space (``words_bytes``): each entry's id, digest and words, and the entry
        itself (``_MEMO_ENTRY_BYTES``)."""
        kept = WORDS_MEMO_SHARE * (self.working + self.held_at_most)
        if kept <= 0:
            return _words_of_both(entity)
        made_of = self._made_of(entity)
        with self._words_memo_lock:
            hit = self._words_memo.get(entity.id)
            if hit is not None and hit[0] == made_of:
                self._words_memo.move_to_end(entity.id)
                return hit[1]
        words = _words_of_both(entity)
        weight = _MEMO_ENTRY_BYTES + sys.getsizeof(entity.id) + sys.getsizeof(made_of) + sys.getsizeof(words)
        weight += sum(map(sys.getsizeof, words))
        with self._words_memo_lock:
            older = self._words_memo.pop(entity.id, None)
            if older is not None:
                self.words_bytes -= older[2]
            if entity.id in self._gone or (since is not None and since < self._resynced):
                return words  # the call's own: forgotten or changed since it was found, or found before a whole resync
            self._words_memo[entity.id] = (made_of, words, weight)
            self.words_bytes += weight
            while len(self._words_memo) > kept:
                _, (_, _, dropped) = self._words_memo.popitem(last=False)
                self.words_bytes -= dropped
        return words

    def _made_of(self, entity: Entity) -> bytes:
        """A digest of what ``entity``'s words are worked out of (its value, fake, type and class), keyed by this
        vault's own key (``_memo_key``): what the memo checks an entry against, in place of the strings themselves.
        Each part is written with its length, so that no two entries' parts run into one another."""
        digest = hashlib.blake2b(key=self._memo_key, digest_size=_MEMO_DIGEST)
        for part in (entity.original, entity.surrogate, entity.etype, str(int(entity.eclass))):
            data = part.encode("utf-8", "surrogatepass")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        return digest.digest()

    def _forget_words(self, ids: Iterable[str]) -> None:
        """The words kept of ``ids`` (``_both_words``) let go, as their values leave the vault."""
        with self._words_memo_lock:
            for entity_id in ids:
                older = self._words_memo.pop(entity_id, None)
                if older is not None:
                    self.words_bytes -= older[2]

    def _forget_all_words(self) -> None:
        """Every word the memo keeps let go (``_both_words``): a whole resync's, which may follow a forget whose message
        was lost, of an entry the memo holds that memory does not."""
        with self._words_memo_lock:
            self._words_memo.clear()
            self.words_bytes = 0

    def read_for(self, text: str, parts: Matched, pieces: Iterable[str] | None = None, *, ask: bool = True) -> Matched:
        """What ``text`` is read for (``Matchers.for_text``, the plan's S4): its detectors (``parts``: the values it
        holds whatever the text, ``matched``), and the values and fakes whose word is one of the text's words
        (``words_in``, ``pieces``: those of the texts it is joined of), looked for in the text alone (``TextValues``):
        those in memory, those this thread's call asked the store for (``prefetch``), and those of the words the call
        never asked for, asked of the store now, once for the call (but a word of a fake memory holds: a masked text
        holds its fakes, whose values were looked for by the words of the text they were masked in, and the check of
        each text of a call, ``settle``, asks the store nothing again). Outside a call (the final check of a request
        whose call is over, which asked for its texts' words) and on the event loop, memory's alone, as the detectors of
        the whole working set read them before. Kept with the call for the text at the vault's version. Its ``seen``
        brings into memory those the text held (Ruling T8a). Raises VaultUnread when the store does not answer. ``ask``
        off: the store is not asked, nor anything kept or brought into memory (an unmask's reading of memory's fakes,
        ``candidates_in``)."""
        call = self._call()
        budget = call.lookup if call is not None else LookupBudget()
        return self._read_for(text, parts, call, budget, pieces, ask=ask)

    def _read_for(
        self,
        text: str,
        parts: Matched,
        call: _Call | None,
        budget: LookupBudget,
        pieces: Iterable[str] | None = None,
        *,
        ask: bool = True,
    ) -> Matched:
        """A reading sharing the budget of its public operation, including a standalone fake lookup."""
        if ask and call is not None and (kept := call.read.get(text)) is not None and kept[0] == parts.version:
            return kept[1]
        words = self._words(text, call, budget, pieces)
        asking = ask and call is not None and call.asks and not on_a_loop()
        if asking and call is not None:
            self._asked_now(call, words, text)  # kept with the call, found by their words below
        found = self._found_by(words, call, budget)
        detectors = text_detectors(found.values(), text)
        since = call.since if call is not None else self._forgets
        seen = self._seen_by(detectors, found, since) if asking and found else _unseen
        known, echo, inside = detectors
        held = Matched(
            parts.version, parts.known.plus(known), parts.echo.plus(echo), parts.echo_inside.plus(inside), seen
        )
        if ask and call is not None:
            call.read[text] = (parts.version, held)
        return held

    def _found_by(self, words: Iterable[str], call: _Call | None, budget: LookupBudget) -> dict[str, Entity]:
        """The entities whose value or fake has one of ``words`` among its words: in memory, and among those ``call``
        asked the store for."""
        found: dict[str, Entity] = {}
        by_word, by_id = self._by_word, self._by_id
        for word in words:
            for entity_id in by_word.get(word, ()):
                if (entity := by_id.get(entity_id)) is not None:
                    if entity_id not in found:
                        _charged(budget, entity)
                    found[entity_id] = entity
        if call is not None and call.by_word:
            for word in words:
                for entity in call.by_word.get(word, ()):
                    if entity.id not in found:
                        _charged(budget, entity)
                    found.setdefault(entity.id, entity)
        if call is not None and call.whole is not None:  # a person's whole vault past a bound, by reference
            for word in words:
                for entity in call.whole.by_word.get(word, ()):
                    if entity.id not in found:
                        _charged(budget, entity)
                    found.setdefault(entity.id, entity)
        return found

    def _weighed(self, call: _Call, texts: Sequence[str]) -> list[Entity]:
        """What ``texts`` hold among the call's candidates (``prefetch``: its own, the detector of them made once for
        the call) and the whole vault it reads past a bound (``call.whole``, its detectors the snapshot's, shared)."""
        held: list[Entity] = []
        if call.candidates:
            if call.weighing is None:
                for entity in call.candidates.values():
                    _charged(call.lookup, entity)
                call.weighing = _Weighing(call.candidates.values())
            held = call.weighing.held_in(texts)
        if call.whole is not None:
            seen = {entity.id for entity in held}
            held += [entity for entity in call.whole.held_in(texts) if entity.id not in seen]
        return held

    def _asked_now(self, call: _Call, words: frozenset[str], text: str) -> None:
        """Those of ``words`` (``text``'s) ``call`` never asked for, asked of the store now, what it answers kept with
        the call: but the words of the fakes memory holds (``_fake_words``), which a masked text holds where its values
        were, and which the text's own words found, asked for first (``prefetched_first``). Not on the event loop. None
        in a call begun with every value in memory (``_alone``): memory found them already. None for a text that holds
        a live launch key (N-2): the store is never asked about its words, not even by their keyed prints, as
        ``prefetch`` never hands it on; it is masked by what memory holds, the key by its own detector
        (``memory_alone``). The key is never looked up as a fake (``by_surrogate``, ``by_reading``: memory alone) nor
        remembered as no fake; masked, it is kept as a value like any other (R-103), and its keep, like any value's,
        writes and finds it by its own keyed hash (``original_key``). Where the masking keeps fakes
        (``keep_surrogates``, from the model) and where a text is checked for values in clear, each other value found
        is asked whether it is a fake by its own keyed hash alone (``is_surrogate``), as in any text, which keeps a
        resumed conversation's old fakes as fakes."""
        if call.alone or self.memory_alone(text):
            return
        fakes = self._fake_words
        unasked = words - call.asked
        new = {word for word in unasked if not fakes.get(word)}
        if self.prefetched_first and len(new) < len(unasked) and not call.prefetched:
            raise AssertionError("a text read in a call that asked for no text's words first (prefetch)")
        if not new:
            return
        roles = self._roles(call)
        found, answered = self._lookup(roles, new, call.lookup, call)
        call.asked |= answered
        self._candidates(call, found)

    def _seen_by(self, detectors: tuple[TextValues, ...], found: dict[str, Entity], since: int) -> Callable[[], None]:
        """What a text's reading brings into memory once it is over (``Matched.seen``): the entities of ``found`` whose
        value or fake ``detectors`` found in it (as the text writes it: a form of the string, or how a fake in escapes
        reads), those not there yet, as ``prefetch`` brings them; each once."""

        def seen() -> None:
            strings: set[str] = set()
            for detector in detectors:
                strings |= detector.found
                detector.found.clear()
            if not strings:
                return
            held: dict[str, Entity] = {}
            for entity in found.values():
                if entity.id in held or entity.id in self._by_id:
                    continue
                for string in (entity.original, entity.surrogate, fake_reading(entity)):
                    if string is not None and any(form in strings for form in forms(string)):
                        held[entity.id] = entity
                        break
            if held:
                with self._lock:
                    self._join(held.values(), since)
                    self._trim()

        return seen

    def weigh(self, texts: Sequence[str]) -> None:
        """The values ``texts`` hold among those this thread's call found already (``prefetch``), brought into memory
        with no call to the store: a text its conversation sent before, kept in another form than the request writes
        it (a JSON document's values written again with their letters), weighed before memory is asked whether it holds
        a value in clear (``Mediator._leaves_as_it_left``)."""
        call = self._call()
        if call is None or not (call.candidates or call.whole is not None):
            return
        held = self._weighed(call, texts)
        if held:
            with self._lock:
                self._join(held, call.since)
                self._trim()

    def intern(
        self,
        etype: str,
        eclass: EntityClass,
        original: str,
        context: str = "",
        rule: str | None = None,
        generate: Callable[[str, Drbg], str] | None = None,
        reads: str | None = None,
    ) -> Entity:
        """``Vault.intern``'s: a value it does not hold is kept through ``on_insert``, whose store answers with the
        entity it keeps already for that value, out of memory as it may be (the same fake as ever), and refuses a fake
        that is another value's or another fake. The entity is used now, and held by this thread's call; and, new to
        the store, by every call under way (``holding``), which asked for its words before it was kept (one used again
        is no call's but its own: the review of Task 7's M5)."""
        with self._lock:
            entity = super().intern(etype, eclass, original, context, rule, generate, reads)
            self._use(entity)
            self._trim()
            return entity

    def learn(self, entity: Entity) -> None:
        """``Vault.learn``'s; the value held by every call under way (``holding``), which asked for its words before."""
        with self._lock:
            super().learn(entity)
            held = self._by_id.get(entity.id)
            if held is not None:
                self._index_words(held)  # in place of the words it had under that id, if any (I2)
                self._noted(held)
                self._kept_at[held.id] = next(self._clock)
            self._grown()  # past its bound by values another instance kept, as by its own (``_place``)
            self._trim()

    def forget(self, entity_id: str) -> Entity | None:
        """``Vault.forget``'s, in memory; and a value out of memory is looked for in the store, so that whoever
        forgets it there (``Runtime.forget``) does (not on the event loop). A reading of the store begun before is not
        brought back into memory."""
        with self._lock:
            self._forgets += 1
            self._whole_epoch += 1  # the shared whole read holds it: read whole again
            self._gone[entity_id] = self._forgets
            while len(self._gone) > _GONE_KEPT:
                self._gone.popitem(last=False)
            entity = super().forget(entity_id)
        self._forget_words([entity_id])
        if entity is not None or on_a_loop():
            return entity
        found = self._store.by_ids([entity_id])
        return _kept(found[0]) if found else None

    def _generation(self) -> int:
        """Its store's generation (``KeyVaultStore.generation``): moved by each forget of values by themselves (Task
        11b), read and written under this vault's lock alone (its draws, and the store's refusal of a value drawn in an
        older one), so that a value is kept in the generation it was drawn in."""
        return getattr(self._store, "generation", 0)

    def forgotten(self, ids: Iterable[str], generation: int | None = None) -> int:
        """The values its store forgot already (an API key's values forgotten by themselves, Task 11b:
        ``PgVaultStore.forget_values``), out of memory in one step under the lock, with no call to the store: those it
        held, as ``forget`` takes them out, the version moved once, and every one of them, held or not, remembered as
        forgotten, so that a reading of the store begun before does not bring it back. ``generation``: the one the
        forget moved the store to, which its new fakes are drawn in from now on (review 11b's M1: not a refused value
        first), under the same lock as its draws. How many it held. Its caller has the engine forget the texts it masked
        before (``Engine.rules_changed``): one may hold a fake of one of them, held in memory or not."""
        held = 0
        with self._lock:
            if generation is not None and generation > self._generation():
                self._store.generation = generation
            self._whole_epoch += 1  # the shared whole read may hold them: read whole again
            for entity_id in dict.fromkeys(ids):
                self._forgets += 1
                self._gone[entity_id] = self._forgets
                self._forget_words([entity_id])
                entity = self._by_id.get(entity_id)
                if entity is not None:
                    self._unindex(entity)
                    held += 1
            while len(self._gone) > _GONE_KEPT:
                self._gone.popitem(last=False)
            if held:
                self._moved()
        return held

    # --- restoring ---------------------------------------------------------------------------------

    def _prefetch_fakes(self, texts: Sequence[str]) -> None:
        """The fakes ``texts`` may hold, asked for in bounded store batches (those whose anchor is one of their words)
        and kept with this thread's call, open (``holding``), for ``candidates_in`` to give each text its own: an
        unmask shares those batches across all its texts. Not brought into memory. Raises VaultUnread when the store
        does not answer, and MaskingError on the event loop."""
        call = self._call()
        if call is None:
            return  # nothing to keep them with: each text asks for its own
        words: set[str] = set()
        for text in texts:
            if text:
                words |= self._words(text, call, call.lookup)
        words -= call.fakes_asked
        if not words:
            return
        if on_a_loop():
            raise MaskingError(ON_THE_LOOP)
        found, answered = self._lookup("surrogate", words, call.lookup, call)
        for entity in found:
            kept = _kept(entity)
            for word in words_of(kept, "surrogate"):  # its anchor, and its edges where it is restored inside a word
                call.fakes.setdefault(word, []).append(kept)
        call.fakes_asked |= answered

    def memory_alone(self, text: str) -> bool:
        """Whether what ``text`` holds is read from memory alone, the store asked nothing of its words: on the event
        loop, where nothing waits on a store, and for a text that holds a live launch key (N-2, P2-6a's re-review: the
        store is never asked about its words, not even by their keyed prints, as ``Vault.prefetch`` and
        ``prefetch_fakes`` never hand one on), whatever reads it: masking (``_asked_now``), restoring
        (``candidates_in``) and adopting its fakes alike, those memory does not hold (``fakes_out_of_memory``) and
        each whole one its detectors of fakes find (``Engine.adopt``'s ``by_surrogate``, ``memory_only``: the minors
        after 0.11, item 11). The one test of the rule (``Vault.memory_alone``), of a string as of a text: a string
        that holds a key is never looked up as a fake, not even by its keyed hash (``by_surrogate``, ``by_reading``:
        memory alone), nor remembered as no fake. Masked, the key is kept as a value like any other (R-103): its keep,
        like any value's, writes and finds it by its own keyed hash (``original_key``). Where the masking of such a text
        keeps fakes (``keep_surrogates``, from the model) and where it is checked for values in clear, each other value
        found is asked whether it is a fake, by that value's keyed hash alone (``is_surrogate``), as in any text, so
        that a resumed conversation's old fake stays the fake the model saw; a value it then keeps in the store, with
        keyed hashes of its own."""
        return on_a_loop() or not safe_text(text)

    def fakes_out_of_memory(self, text: str) -> list[Entity]:
        """The entities whose fake ``text`` holds, as its detectors of fakes would find them, among those of the store
        (from what this thread's call asked for, ``prefetch_fakes``, else in one call): brought into memory, so that a
        conversation resumed with fakes of any age adopts them (``Engine.adopt``). Memory's alone on the event loop and
        for a text that holds a live launch key (``memory_alone``): none."""
        if self.memory_alone(text):
            return []
        call = self._call()
        budget = call.lookup if call is not None else LookupBudget()
        words = set(self._words(text, call, budget))
        found: dict[str, Entity] = {}
        if call is not None and call.fakes_asked:
            for word in words & call.fakes_asked:
                found.update((e.id, e) for e in call.fakes.get(word, ()))
            words -= call.fakes_asked
        if words:
            found.update((e.id, e) for e in self._lookup("surrogate", words, budget, call)[0])
        for entity in found.values():
            _charged(budget, entity)
        held = _Weighing(found.values(), fakes=True).held_in([text])
        if held:
            since = call.since if call is not None else self._forgets
            with self._lock:
                self._join(held, since)
                self._trim()
        return held

    def candidates_in(self, text: str) -> list[Entity]:
        """The entities whose fake may be in ``text`` (``Engine.unmask``), each once: those memory holds whose fake its
        words find (``read_for``'s, none asked of the store: an unmask of the whole vault reads the fakes, ``echo``),
        those its detectors hold whose fake the text holds (``_HeldForms``: no word of the text finds them, glued to
        letters or without a word), and those of the store whose fake's word is one of the text's words (not brought
        into memory: a restore reads them once), from what this thread's call asked for already (``prefetch_fakes``),
        else in one call to the store. In a call that asked, memory is not read for its fakes (Task 10b): every fake
        memory holds is the store's too, found by its words as the store's are. On the event loop, and for a text that
        holds a live launch key (``memory_alone``), memory's alone, read whatever the call asked. Raises VaultUnread
        when the store does not answer."""
        found: dict[str, Entity] = {}
        call = self._call()
        budget = call.lookup if call is not None else LookupBudget()
        alone = self.memory_alone(text)
        words = self._words(text, call, budget)
        if call is None or not call.fakes_asked or alone:
            echo = self._read_for(text, self.matchers.current(), call, budget, ask=False).echo
            for span in read_escaped(text, lambda t: list(echo.detect(t, DetectContext()))):
                # As written, and as it reads; a fake, or how a fake written in escapes reads (0.9.9: `Giulia Martini`).
                for form in dict.fromkeys((text[span.start : span.end], span.of(text))):
                    entity = self._by_surrogate.get(form) or self._by_reading.get(form)
                    if entity is not None:
                        if entity.id not in found:
                            _charged(budget, entity)
                        found[entity.id] = entity
        for entity_id in self._held_forms.held_in(text):  # those its detectors hold whose fake the text holds
            entity = self._by_id.get(entity_id)
            if entity is not None:
                if entity_id not in found:
                    _charged(budget, entity)
                found.setdefault(entity_id, entity)
        if alone:
            return list(found.values())
        words = set(words)
        if call is not None and call.fakes_asked:
            for word in words & call.fakes_asked:
                for entity in call.fakes.get(word, ()):
                    if entity.id not in found:
                        _charged(budget, entity)
                    found.setdefault(entity.id, entity)
            words -= call.fakes_asked
        if words:
            for entity in self._lookup("surrogate", words, budget, call)[0]:
                if entity.id not in found:
                    _charged(budget, entity)
                found.setdefault(entity.id, _kept(entity))
        return list(found.values())

    # --- lookups -----------------------------------------------------------------------------------

    def by_surrogate(self, surrogate: str, as_read: bool = True, memory_only: bool = False) -> Entity | None:
        """``Vault.by_surrogate``'s: memory first, then the store, which keeps each fake as written and how each fake
        written in escapes reads (``fake_reading``), each found by the string's keyed hash: two probes at most, whatever
        shares a word with it. A string the store says is neither is remembered so, apart for the two questions.
        ``memory_only`` (a string of a text ``memory_alone`` reads from memory alone, ``Engine.adopt``'s), and a string
        that holds a live launch key whoever asks (``memory_alone`` of the string: the masking's check of each value it
        finds, ``is_surrogate``, and of a text for values in clear, ``fake_or_let_through``; Task 5's review, root's
        ruling): memory's answer, the string never looked up in the store, not even by its keyed hash, and no miss
        remembered, so the key is never remembered as no fake. No fake that ever left Latet has a key's shape (a
        stand-in has 32 characters after the prefix, never 43, and every text that leaves is screened for one), so the
        store's answer could name none. Memory is made current first as for any string (``refresh``, which asks the
        store what changed, nothing of the string)."""
        if self._call() is None and not on_a_loop():
            self.refresh()
        found = self._held(self._by_surrogate.get(surrogate))
        if found is None and as_read:
            found = self._held(self._by_reading.get(surrogate))
        miss = _miss(surrogate, as_read)
        if found is not None or memory_only or self._missed(miss) or self.memory_alone(surrogate):
            return found
        may_be_fake = getattr(self._store, "may_be_fake", None)
        if may_be_fake is not None and not self._asked(lambda: may_be_fake(surrogate)):
            return None
        return self._fetched(
            lambda: self._store.by_surrogate(surrogate) or (self._store.by_reading(surrogate) if as_read else None),
            miss=miss,
        )

    def by_original(self, original: str) -> Entity | None:
        found = self._held(self._by_original.get(original))
        if found is not None or on_a_loop():
            return found
        return self._fetched(lambda: self._store.by_original(original))

    def get(self, entity_id: str) -> Entity | None:
        found = self._held(self._by_id.get(entity_id))
        if found is not None or on_a_loop():
            return found
        return self._fetched(lambda: next(iter(self._store.by_ids([entity_id])), None))

    def is_surrogate(self, value: str) -> bool:
        """``Vault.is_surrogate``'s, memory first, then the store: a fake as written, or as it reads unless that
        reading is a value the vault knows too (the value wins)."""
        found = self.by_surrogate(value)
        return found is not None and (found.surrogate == value or self.by_original(value) is None)

    def forgot_any(self, ids: Sequence[str]) -> bool:
        """``Vault.forgot_any``'s for an API key's vault, whose values forgotten by themselves take their entries with
        them (Task 11b): whether one of ``ids`` is no longer held, memory first, those out of it asked of the store in
        one call and brought back (``hold``), what this instance heard of a forget or not. Not on the event loop."""
        missing = [entity_id for entity_id in ids if self.in_memory(entity_id) is None]
        if not missing:
            return False
        self.hold(missing)
        if not getattr(self._store, "forgets_values", True):  # a person's: Forget leaves the texts as they left (N2)
            return False
        return any(self.in_memory(entity_id) is None for entity_id in missing)

    def hold(self, ids: Iterable[str], key: str | None = None) -> object | None:
        """The entities of ``ids`` used now, and those out of memory brought back from the store in one call (not on
        the event loop): a conversation's, before its answer is restored on the event loop, which finds them in memory
        alone (the version moves once they are back: restores made without them are made again). With ``key`` (the
        conversation's) they stay in memory while its answers are awaited: each hold under a
        key is counted, and the key's ids held until as many releases (its answers ended), or ``HOLD_SECONDS`` after
        the last hold, whichever comes first: the other calls of the key, while the model answers, let none of them go
        (Review Important 1). What all conversations hold is a working set at most: past it, the holds made longest ago
        lapse first; and a hold whose time is over is dropped here, at the next hold. A hold whose store call fails
        (``VaultUnread``) raises uncounted, since no release pairs it: the key's ids it added stay held for as long as
        the key's other holds, counted before it, are.

        Returns what the hold was counted in, for its release to hand back (``release(key, held)``): a request whose
        hold lapsed, and whose conversation held anew since, takes nothing of the new hold when it ends. None without
        ``key``."""
        ids = list(ids)
        hold = None
        if key is not None:
            with self._sessions_lock:
                now = time.monotonic()
                self._lapsed(now)
                hold = self._sessions.get(key)
                if hold is None:
                    hold = self._sessions[key] = _Hold()
                hold.ids.update(ids)
                hold.count += 1
                hold.until = now + HOLD_SECONDS
                self._bounded(key)
        missing = [i for i in ids if self._held(self._by_id.get(i)) is None]
        if not missing or on_a_loop():
            return hold
        since = self._forgets
        try:
            found = self._asked(lambda: self._store.by_ids(missing))
        except BaseException:
            if key is not None:
                self.release(key, hold)  # uncounted: no release would pair it
            raise
        with self._lock:
            # Brought back, the version moves: a conversation's restores made while they were out of memory
            # (``Engine.restores``, kept by the version) are made again with them.
            version = self.version
            self._join(found, since)
            if self.version == version and any(entity.id in self._by_id for entity in found):
                self._moved()
            self._trim()
        return hold

    def renew(self, key: str) -> bool:
        """An answer under ``key`` still being restored (``Engine.restores``, at each part of it, on the event
        loop): its hold, while an answer is awaited (its count), lasts ``HOLD_SECONDS`` from now, so that an answer
        longer than that keeps the values it restores (Task 9's I1, T9-S4); an API key's too (fix round 3). False once
        its store is closed (its space closed): whoever renews it stops (``forward.renewing``)."""
        if getattr(self._store, "closed", False):
            return False
        with self._sessions_lock:
            hold = self._sessions.get(key)
            if hold is not None and hold.count > 0:
                hold.until = time.monotonic() + HOLD_SECONDS
        return True

    def reacquire(self, ids: Iterable[str], key: str, held: object | None) -> object | None:
        """The values of ``ids`` held again under ``key`` for an answer that waited (a Live Reviewer's decision, in the
        thread that applies it, never the event loop): ``held``, the request's hold, renewed and given them where it is
        still the key's; else a hold of its own, counted once, which the request's release pairs from then on (its
        answer). Those out of memory are brought back from the store in one read, as ``hold`` brings them."""
        ids = list(ids)
        with self._sessions_lock:
            hold = self._sessions.get(key)
            live = hold is not None and hold is held
            if live and hold is not None:
                hold.ids.update(ids)
                hold.until = time.monotonic() + HOLD_SECONDS
        if not live:
            return self.hold(ids, key)  # (outside the lock, which ``hold`` takes)
        self.hold(ids)  # those out of memory back, under no key: the request's hold keeps them
        return held

    def release(self, key: str, held: object | None) -> None:
        """One answer awaited under ``key`` ended, ``held`` what its hold was counted in (``hold``'s answer): once every
        one has, what was held under it may go again, at the next cut of the working set (the event loop releases: its
        lock is held for no store call). Nothing is taken when ``held`` lapsed or was cut meanwhile, which took its
        count with it: another hold under the key since is another request's, and keeps its own."""
        with self._sessions_lock:
            hold = self._sessions.get(key)
            if hold is None or hold is not held:
                return
            hold.count -= 1
            if hold.count <= 0:
                del self._sessions[key]

    def _lapsed(self, now: float) -> None:
        """The holds whose time is over, dropped (under ``_sessions_lock``)."""
        for key in [key for key, hold in self._sessions.items() if hold.until <= now]:
            del self._sessions[key]

    def _bounded(self, newest: str) -> None:
        """The holds made longest ago dropped until what they all hold is a working set at most, ``newest`` kept
        whatever it holds (under ``_sessions_lock``)."""
        held = sum(len(hold.ids) for hold in self._sessions.values())
        for key in sorted(self._sessions, key=lambda key: self._sessions[key].until):
            if held <= self.working:
                break
            # A person's hold whose answer is awaited is never dropped for another's (Task 9's I1): its values are
            # weighed with the space (its memory share), as its whole vault was.
            if key != newest and not (self.glued_all and self._sessions[key].count > 0):
                held -= len(self._sessions.pop(key).ids)

    def _held(self, entity: Entity | None) -> Entity | None:
        """``entity``, found in memory, used now (no lock: the event loop finds values too)."""
        if entity is not None:
            self._used[entity.id] = next(self._clock)
        return entity

    def _missed(self, surrogate: str) -> bool:
        with self._misses_lock:
            return surrogate in self._misses

    def _fetched(self, ask: Callable[[], Entity | None], miss: str | None = None) -> Entity | None:
        """What the store answers ``ask``, brought into memory; None for none (``miss``: a string it says is no fake,
        remembered unless an entity was placed in memory meanwhile, which may be its)."""
        since, placed = self._forgets, self._placed
        entity = ask()
        if entity is None:
            if miss is not None:
                with self._misses_lock:
                    if self._placed == placed:
                        self._misses[miss] = None
                        while len(self._misses) > MISSES_KEPT:
                            self._misses.popitem(last=False)
            return None
        with self._lock:
            self._join([entity], since)
            self._trim()
            return self._by_id.get(entity.id)

    # --- the working set ---------------------------------------------------------------------------

    def resync(
        self, *, invalidate: bool = True, budget: LookupBudget | None = None, only: Iterable[str] | None = None
    ) -> int:
        """The working set as the store has it now (a catch-up of the space, after a change it never heard): each of
        its entities the store no longer has, or holds otherwise (a fake given anew), leaves memory, to be found again
        as it is when a text needs it; one held for good is given the store's; the values without anchor kept elsewhere
        meanwhile are read again, and the newest ``held_at_most`` secrets held glued (``load_held``, review 10b's R4),
        and with ``glued_all`` those glued on both sides (``load_glued``), as a build reads them. The working set is
        asked for, never the whole vault. ``only``: the entries the store named as changed since this vault last
        confirmed its memory (``PgVaultStore.named_after``), those of the working set alone asked for again; every
        change of an entry stamps all of its rows, so a catch-up names it, and an entry deleted is never among them
        (its forget makes the store know nothing by name, and ``only`` None). The version moves: a text masked before
        is checked again, by its words, as the change never heard may be in any. A whole one confirms every change the
        store told before its barrier (its own barrier's too, ``named_seq`` read after it): the next refresh does not
        confirm them again. How many changed.

        Held whole (``holds_all``), memory stays every value of the store: the entries named are read by their ids,
        those out of memory too, and taken in, one changed given the store's; where nothing names them, the whole vault
        is read again (``whole_at_most`` and one more, the newest first), what memory holds that it no longer has let go
        and what it lacks taken in; past ``whole_at_most`` it is a vault on its store from then on (``_left_whole``),
        and this resync one of its working set.

        One resync of a vault at a time (``_resyncing``): two calls' refreshes at once each have theirs whole, so that
        neither drops what the other read (a resync revokes the reads begun before it, ``_resynced``), which no later
        telling names again (the review of the change of 2026-10-31: a value kept elsewhere was lost from a vault held
        whole, as one held for good could be from a vault on its store)."""
        with self._resyncing:
            return self._resynced_now(invalidate, budget, only)

    def _resynced_now(self, invalidate: bool, budget: LookupBudget | None, only: Iterable[str] | None) -> int:
        """``resync``'s, under ``_resyncing``; what a whole read again holds weighed while it runs (``_rereading``)."""
        try:
            return self._resynced_body(invalidate, budget, only)
        finally:
            self._rereading = 0

    def _resynced_body(self, invalidate: bool, budget: LookupBudget | None, only: Iterable[str] | None) -> int:
        """``_resynced_now``'s."""
        barrier = getattr(self._store, "invalidate", None)
        if invalidate and barrier is not None:
            barrier()
        # Read before the barrier below: every telling up to it has its fence up (``PgVaultStore._telling``), and every
        # read joined from now on began after them.
        told = getattr(self._store, "named_seq", None) if only is None else None
        with self._lock:
            self._forgets += 1
            self._resynced = self._forgets
        if only is None:
            # After ``_resynced``: a call begun before it keeps no more words (``_both_words``).
            self._forget_all_words()
        with self._misses_lock:
            self._misses.clear()
            self._placed += 1  # a negative exact lookup begun before this resync cannot be remembered later
        ids = list(self._by_id) if only is None else [i for i in dict.fromkeys(only) if i in self._by_id]
        bounded = getattr(self._store, "bounded_ids", None)
        call = self._call()
        budget = call.lookup if call is not None else budget if budget is not None else LookupBudget()
        # Each read bounded by the call's budget while it lasts; past a bound read as 0.10.x read it, never refusing.
        found: list[Entity] = []  # nothing it holds was named, when ``only`` names none of them
        alone, left = self._holds_all, False
        since = self._forgets  # held whole, read first: a value forgotten here while the store is read is not taken in
        # Before the reads: an entry kept or learnt since (``_kept_at``) is newer than what they find (the review of
        # 2026-11-01, M3), and stays as memory holds it.
        began = next(self._clock)
        if alone:
            if only is None:
                # Read whole again (a change named by nothing): weighed with the space while it runs, about memory's
                # size, and one of the process's ``WHOLE_READS_AT_ONCE``, as a whole read past a bound (M1).
                self._rereading = len(self._by_key)
                with _WHOLE_READS:
                    found = self._store.load_recent(self.whole_at_most + 1)
                if len(found) > self.whole_at_most:  # grown past it, with nothing told: on its store from now on
                    with self._lock:
                        if self._holds_all:
                            self._left_whole()
                    alone, found, left = False, [], True
            else:
                named = list(dict.fromkeys(only))
                if named:
                    found = self._bounded_or_whole(bounded, budget, lambda: self._store.by_ids(named), named)
        if not alone:
            if only is None or ids:
                found = self._bounded_or_whole(bounded, budget, lambda: self._store.by_ids(ids), ids)
            since = self._forgets
        now = {e.id: _kept(e) for e in found}
        if alone:
            unanchored, glued, both = [], [], []  # among what was read: the store's whole vault, or what it named
        elif only is not None and self.glued_all:
            # A person's (Task 9's I2): every value it holds for good, whatever its age, would be read whole after
            # each commit. Those kept or changed since are among the entries named: read those alone, by their ids, and
            # those of them no word finds held (``_noted``); the whole set is read by a whole resync alone.
            named = [i for i in dict.fromkeys(only) if i not in self._by_id]
            fresh = self._bounded_or_whole(bounded, budget, lambda: self._store.by_ids(named), named) if named else []
            unanchored, glued, both = [e for e in fresh if _held_for_good(e)], [], []
        else:
            unanchored, glued, both = self._bounded_or_whole(
                getattr(self._store, "bounded_retained", None),
                budget,
                self._retained_whole,
                self._held_wanted,
                self.glued_all,
            )
        with self._lock:
            kept_at = self._kept_at
            changed = [
                e
                for i in ids
                if (e := self._by_id.get(i)) is not None and now.get(i) != e and kept_at.get(i, 0) <= began
            ]
            for entity in changed:
                self._forgets += 1
                self._gone[entity.id] = self._forgets
                self._forget_words([entity.id])
                fresh = now.get(entity.id)
                if fresh is not None and (entity.id in self._for_good or alone):
                    self._replaced(entity, fresh)  # in place: never out of memory meanwhile (I2)
                else:
                    self._unindex(entity)
            while len(self._gone) > _GONE_KEPT:
                self._gone.popitem(last=False)
            if alone:  # held whole: what the store holds that memory lacks, taken in (the oldest first)
                self._join([e for e in reversed(found) if e.id not in self._by_id], since)
            # The values without anchor kept elsewhere meanwhile: no text's word finds them, so they are read again; and
            # the newest secrets held glued, the oldest first, so that the newest are those kept (``_noted``).
            self._join([e for e in unanchored if e.id not in self._by_id], since)
            self._join([e for e in both if e.id not in self._by_id], since)
            self._join([e for e in reversed(glued) if e.id not in self._by_id], since)
            # A change never heard can be in any text masked before (Review Critical 1): each is checked again.
            self._moved()
            if told is not None:
                self._named_seen = max(self._named_seen, told)
            if left:
                self._trim()  # its working set, now that it no longer holds every value
        return len(changed)

    def _bounded_or_whole[T](
        self, bounded: Callable[..., T] | None, budget: LookupBudget, whole: Callable[[], T], *args: object
    ) -> T:
        """``bounded(*args, budget)``, a store's read bounded by the call's budget; with no such read, or past a bound
        (``LookupLimitExceeded``, or a budget already past one), ``whole()``: the read as 0.10.x made it, never
        refusing (``engine.lookup``)."""
        if bounded is not None and not budget.past:
            try:
                return bounded(*args, budget)
            except LookupLimitExceeded:
                budget.relax()
        return whole()

    def _retained_whole(self) -> tuple[list[Entity], list[Entity], list[Entity]]:
        """The values a resync reads again whatever memory holds, as 0.10.x read them: those without anchor, the
        newest ``held_at_most`` secrets held glued, and with ``glued_all`` those glued on both sides."""
        unanchored = self._store.load_unanchored()
        load_held = getattr(self._store, "load_held", None)
        glued = load_held(self._held_wanted) if load_held is not None and self._held_wanted else []
        both = self._store.load_glued() if self.glued_all else []
        return unanchored, glued, both

    def _join(self, entities: Iterable[Entity], since: int) -> None:
        """``entities``, read from the store, in memory (under the lock): one held already is used now; one forgotten
        here since the reading began (``since``) stays out; one whose fake or value memory holds under another id puts
        that one out of memory, as a value another instance kept does (the store keeps one value for each fake and one
        entity for each value). Each is held by this thread's call. The version moves once, where what the detectors
        hold changed (one they hold placed, ``matched``; one put out), or where one is a value it never heard of
        (``_heard_of``: kept after it was made, by another instance whose message was lost), as one learnt moves it: a
        text masked before is checked again. A value that only joins memory for its lookups moves none (the plan's
        S4)."""
        if since < self._resynced:
            return
        moved = False
        for entity in entities:
            entity = _kept(entity)
            held = self._by_id.get(entity.id)
            if held is None:
                if self._gone.get(entity.id, 0) > since:
                    continue
                others = self.displaced_by(entity)
                for other in others:
                    self._unindex(other)
                unheard = not self._heard_of(entity)
                self._place(entity)
                held = entity
                moved = moved or bool(others) or unheard or entity.id in self._matched
            self._use(held)
        if moved:
            self._moved()

    def _heard_of(self, entity: Entity) -> bool:
        """Whether this vault heard of ``entity``: kept before it was made (``created``, another instance's clock up to
        ``_CLOCKS_APART`` behind), then any text its words found it in found it; or one it placed in memory since, among
        the last ``_HEARD_KEPT`` (kept or learnt here, read from the store before). Another was kept elsewhere after it
        was made, and its message never came."""
        return entity.created < self._built - _CLOCKS_APART or entity.id in self._heard

    def _use(self, entity: Entity) -> None:
        """``entity`` used now, and held by this thread's call (under the lock)."""
        self._used[entity.id] = next(self._clock)
        call = self._call()
        if call is not None:
            call.held.add(entity.id)

    def _noted(self, entity: Entity) -> None:
        """What it keeps of ``entity``, in memory now (under the lock): used now, held for good when no word of a text
        may find it in the store (a value or a fake of no word, which a store loads with the vault's newest,
        ``load_unanchored``; with ``glued_all``, one glued on both sides, ``glued_both``; or a secret held glued,
        ``held_glued``, the newest ``held_at_most`` alone), the forms of its fake held where its detectors hold it
        (``matched``), heard of, and its fake no miss."""
        self._use(entity)
        if value_word(entity) is None or fake_word(entity) is None:
            self._for_good.add(entity.id)
        elif self.glued_all and (
            glued_both(entity, "original") or glued_both(entity, "surrogate") or held_glued(entity)
        ):  # a person's space: every one no word finds glued, as its whole vault held them (Ruling T9-S3)
            self._for_good.add(entity.id)  # whatever its age, never let go
            self._glued.pop(entity.id, None)
        elif held_glued(entity):  # held for good, the newest ``held_at_most`` alone (Ruling 10b-R3)
            self._glued[entity.id] = None
            self._glued.move_to_end(entity.id)
            self._for_good.add(entity.id)
            while len(self._glued) > self.held_at_most:
                older, _ = self._glued.popitem(last=False)
                self._for_good.discard(older)  # in memory as any value, let go when the working set is full
        else:
            self._for_good.discard(entity.id)
        # What it is and what it is kept as: held by the detectors while in memory (``matched``), its fake's forms
        # held for its restores where no word finds it glued (``unfound_glued``).
        if self.matched(entity):
            self._matched.add(entity.id)
        else:
            self._matched.discard(entity.id)
        if entity.id in self._matched or unfound_glued(entity):
            self._held_forms.add(entity)
        else:
            self._held_forms.drop(entity.id)
        self._heard[entity.id] = None
        self._heard.move_to_end(entity.id)
        while len(self._heard) > _HEARD_KEPT:
            self._heard.popitem(last=False)
        self._placed += 1
        reading = fake_reading(entity)
        with self._misses_lock:
            for string in (entity.surrogate, _miss(entity.surrogate, False), reading):
                if string is not None:
                    self._misses.pop(string, None)

    def _place(self, entity: Entity) -> None:
        super()._place(entity)
        self._index_words(entity)
        self._noted(entity)
        self._grown()

    def _replaced(self, old: Entity, new: Entity) -> None:
        """``new``, the store's version of an entry memory holds as ``old`` (the same id), in its place in every index
        (under the lock): each index goes from the old to the new in one step (``Vault._swapped``, its words in place,
        ``_index_words``), so that a reading that takes no lock never finds it missing (the review of 2026-11-01, I2: a
        call begun whole reads memory alone)."""
        self._swapped(old, new)
        self._index_words(new)
        self._noted(new)

    def _grown(self) -> None:
        """Held whole no longer once it holds more than ``whole_at_most`` values (under the lock: ``_left_whole``)."""
        if self._holds_all and len(self._by_key) > self.whole_at_most:
            self._left_whole()

    def _left_whole(self) -> None:
        """No longer every value of its store in memory (``holds_all``), past ``whole_at_most`` values (under the
        lock): a vault on its store from now on, whose lookups ask the store; nothing leaves memory while a call begun
        before is open (``_trim``), which reads memory alone to its end (``_Call.alone``)."""
        self._holds_all = False
        self._left_at = next(self._clock)

    def _unindex(self, entity: Entity) -> None:
        super()._unindex(entity)
        self._unindex_words(entity.id)
        self._used.pop(entity.id, None)
        self._kept_at.pop(entity.id, None)
        self._for_good.discard(entity.id)
        self._matched.discard(entity.id)
        self._held_forms.drop(entity.id)
        self._glued.pop(entity.id, None)

    def _index_words(self, entity: Entity) -> None:
        """``entity``'s words (``words_of``), each naming it in memory (under the lock): the ids of a word replaced
        whole, as a text's reading takes them without the lock (``read_for``). In place of the words it had (an entry
        changed, the same id): it is added to its new words first, then taken out of those it no longer has, never out
        of one it keeps, so that a reading never finds it under none of its words (the review of 2026-11-01, I2)."""
        old_words, old_fakes = self._words_held.get(entity.id, (frozenset(), ()))
        fakes = words_of(entity, "surrogate")
        words = frozenset((*words_of(entity, "original"), *fakes))
        for word in words:
            ids = self._by_word.get(word, ())
            if entity.id not in ids:
                self._by_word[word] = (*ids, entity.id)
        self._words_held[entity.id] = (words, fakes)
        self._fake_words.update(fakes)
        for word in old_words - words:
            rest = tuple(found for found in self._by_word.get(word, ()) if found != entity.id)
            if rest:
                self._by_word[word] = rest
            else:
                self._by_word.pop(word, None)
        self._fake_words.subtract(old_fakes)
        for word in old_fakes:
            if self._fake_words[word] <= 0:
                del self._fake_words[word]

    def _unindex_words(self, entity_id: str) -> None:
        """The words of ``entity_id`` no longer naming it in memory (under the lock)."""
        words, fakes = self._words_held.pop(entity_id, (frozenset(), ()))
        for word in words:
            rest = tuple(found for found in self._by_word.get(word, ()) if found != entity_id)
            if rest:
                self._by_word[word] = rest
            else:
                self._by_word.pop(word, None)
        for word in fakes:
            self._fake_words[word] -= 1
            if self._fake_words[word] <= 0:
                del self._fake_words[word]

    def _trim(self) -> None:
        """Past ``working`` values in memory, the least recently used leave it (under the lock) until a share of it
        (``EVICT_SHARE``) is free again: never one held for good, nor one a call under way holds, nor one kept or
        learnt since the oldest call under way began (it asked for its words before). The version moves where one its
        detectors hold left (``matched``); a value that leaves memory moves none, as every text is checked by its words
        (``holds_since``). None while it holds every value of its store (``holds_all``), nor while a call begun before
        it stopped is open (``_left_whole``): it reads memory alone to its end (``_Call.alone``)."""
        if len(self._by_key) <= max(self.working, self._stuck) or self._holds_all:
            return
        calls = list(self._calls.values())
        if any(call.began < self._left_at for call in calls):
            return  # tried again as each call ends (``holding``), or at the next value kept
        share = max(1, int(self.working * EVICT_SHARE))
        target = self.working - share
        kept = set(self._for_good).union(*(call.held for call in calls), *self._sessions_held())
        if calls:  # kept or learnt while a call is open, after it asked for its words: held until it is done
            began = min(call.began for call in calls)
            kept.update(entity_id for entity_id, at in list(self._kept_at.items()) if at > began)
        dropped = False
        while len(self._by_key) > target:
            # The least recently used it may let go, as many as are past the target: a few thousand of the working set.
            items = [(stamp, i) for i, stamp in list(self._used.items()) if i not in kept]
            order = heapq.nsmallest(len(self._by_key) - target, items)
            if not order:
                break
            for _, entity_id in order:
                kept.add(entity_id)  # tried: not again in the next round
                entity = self._by_id.get(entity_id)
                if entity is None:
                    self._used.pop(entity_id, None)  # used meanwhile on another thread, gone already
                    continue
                dropped = dropped or entity_id in self._matched
                self._unindex(entity)
        # Still past it: the rest is held, by calls under way or for good. Not tried again at each value they keep,
        # but once a call closes, or a share more is in memory.
        self._stuck = len(self._by_key) + share if len(self._by_key) > target else 0
        if dropped:
            self._moved()

    def _sessions_held(self) -> list[frozenset[str]]:
        """The ids conversations hold until their answers end (``hold``), those whose time ran out dropped, in one step
        under ``_sessions_lock``."""
        with self._sessions_lock:
            self._lapsed(time.monotonic())
            return [frozenset(hold.ids) for hold in self._sessions.values()]

    def _asked[T](self, ask: Callable[[], T]) -> T:
        """What the store answers ``ask``; VaultUnread, with the store's error as its cause, when it does not answer.
        A refusal in Latet's own words (a ``MaskingError``: a call that would read too many values) is raised as it
        is."""
        try:
            return ask()
        except MaskingError:
            raise
        except Exception as exc:
            raise VaultUnread(UNREAD) from exc
