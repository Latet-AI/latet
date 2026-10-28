"""An API key's vault on a Latet server: its newest values in memory, every other one in PostgreSQL (R7).

A key's space masks logs and documents for applications, and its vault grows by millions of values. Each instance of
the server keeps in memory only its working set: the ``working`` values it used last (``WORKING_VALUES``, the server's
``key_working_values``), and the values whose value or fake has no anchor word (``engine.anchors``), which no text's
words find, held for good; and the newest secrets that no word of a text finds glued to letters or digits
(``held_glued``), ``held_at_most`` of them (``HELD_SECRETS``, the server's ``key_held_secrets``, Ruling 10b-R3), held
for good too (an older one is found in the store by its word where nothing is glued to it, or by an edge where one side
is). Every other value is in the store alone (``KeyVaultStore``, PostgreSQL's ``PgVaultStore``), and found there by
the words of the texts as they come:

- Masking (``prefetch``): before the texts of a call are masked, the store is asked, once for all of them, for the
  values whose anchor is one of their words, and those join the working set, where the vault's detectors
  (``Vault.matchers``) find them as they find any value in memory. A value found once is masked for ever, whatever its
  age. A store that does not answer fails the call (``VaultUnread``): a text is never masked without its known values.
- Restoring (``candidates_in``): the fakes a text may hold are those of the working set its detectors of fakes find,
  those held for good, and those of the store whose anchor is one of its words, asked for once for all the texts of a
  call (``prefetch_fakes``). A fake of any age is restored. A conversation's values are brought back into memory as
  its request is masked (``hold``), so that its answer, restored on the event loop, finds them there.
- Lookups (``by_original``, ``by_surrogate``, ``get``, ``is_surrogate``): memory first, then the store; what they find
  joins the working set. A string the store says is no fake is remembered as such (``MISSES_KEPT`` at most), until the
  vault issues or learns it as one.

Past ``working`` values, the least recently used ``EVICT_SHARE`` of them leave memory (as ``forget`` takes a value out
of it, the store left as it is), and the version moves: never a value held for good, nor one that a call still under
way found or kept (``holding``). A text its engine keeps masked held no value in clear the vault knew then; once values
have left memory since, memory alone no longer vouches for it (``holds_since``), and the store is asked again.

The event loop serves every identity of the instance and never waits on the store: on its thread (any thread that runs
one) a lookup answers from memory alone, and ``prefetch`` refuses."""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import re
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from latet.engine.anchors import address_word, anchor, dotted, edges, glued_unfound, words_in
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import KnownValuesDetector, forms, long_enough
from latet.engine.escapes import blanked, decoded, escapes, read_escaped
from latet.engine.rehydrate import restores_for
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, MaskingError, Vault, _kept, fake_reading, restored_whole

if TYPE_CHECKING:
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
    is found glued on one side by them."""
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
    were forgotten when it began, the words it asked the store for values and the values those words found (each
    weighed against every text of the call, the detector of them made once), the entities, by id, it found or kept,
    which stay in memory until it is done, and the fakes its texts may hold (``prefetch_fakes``), by the anchor of each,
    with the words they were asked for by. Whether it is an applications call (``scoped``), and the changes it keeps
    together in the store (``together``) with the ids of the entities they are of (``kept``)."""

    depth: int = 0
    since: int = 0
    asked: set[str] = field(default_factory=set)
    candidates: dict[str, Entity] = field(default_factory=dict)
    weighing: _Weighing | None = None
    held: set[str] = field(default_factory=set)
    fakes: dict[str, list[Entity]] = field(default_factory=dict)
    fakes_asked: set[str] = field(default_factory=set)
    scoped: bool = False
    together: Together | None = None
    kept: set[str] = field(default_factory=set)


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


class StoredVault(Vault):
    """An API key's vault: its working set in memory, every value in its store (the module's words)."""

    def __init__(
        self,
        key: bytes,
        store: KeyVaultStore,
        *,
        working: int = WORKING_VALUES,
        held_at_most: int = HELD_SECRETS,
        on_insert: Callable[[Entity], Entity | None] | None = None,
        on_rule: Callable[[Entity], None] | None = None,
        on_learn: Callable[[Entity], None] | None = None,
        on_replace: Callable[[Entity, Entity], Entity | None] | None = None,
    ) -> None:
        """Its values held for good and its ``working`` newest, from ``store``. A value it keeps goes through
        ``on_insert`` (the store's ``save_entity`` when none is given: a value kept nowhere would be lost once it left
        memory), the rule of one through ``on_rule`` (the store's too), as ``Vault``'s; in an applications call that
        keeps them together (``holding``), each value it keeps noted as the call's (a rule is kept at once). Its
        ``held_at_most`` newest secrets held glued (``held_glued``) are held for good, loaded with the rest."""
        self._store = store
        insert = on_insert if on_insert is not None else store.save_entity

        def inserted(entity: Entity) -> Entity | None:
            first = insert(entity)
            if first is None:  # kept as given: the call's own, until it commits
                self._noted_in_call(entity.id)
            return first

        self.working = working
        self.held_at_most = held_at_most
        self._glued: OrderedDict[str, None] = OrderedDict()  # the secrets held glued, the newest last (``_noted``)
        self._clock = itertools.count(1)
        self._used: dict[str, int] = {}  # each entity in memory, by id: when it was used last
        self._for_good: set[str] = set()  # the entities no word of a text finds (``_noted``): never let go
        self._held_forms = _HeldForms()  # the forms their fakes take in a text (``candidates_in``)
        self._calls: dict[int, _Call] = {}  # the calls under way, by their thread (each thread changes its own alone)
        self._misses: OrderedDict[str, None] = OrderedDict()  # strings the store said are no fake
        self._misses_lock = threading.Lock()  # held for no store call: the event loop may take it
        self._placed = 0  # entities placed in memory so far: a miss read while one was placed is not remembered
        self._gone: OrderedDict[str, int] = OrderedDict()  # values forgotten, by id: when (``_forgets``)
        self._forgets = 0
        self._let_go = 0  # the version once it last let values go
        self._undone = 0  # the version once it last undid what a call did not keep (``_undo``)
        self._stuck = 0  # past the working set by values it may not let go: tried again past this many
        self._sessions: dict[str, _Hold] = {}  # what conversations hold for their answers (``hold``)
        self._sessions_lock = threading.Lock()  # held for no store call: the event loop releases
        held = {e.id: e for e in store.load_unanchored()}
        load_held = getattr(store, "load_held", None)  # a store that keeps the secrets held glued apart
        for e in reversed(load_held(held_at_most) if load_held is not None and held_at_most else []):
            held.setdefault(e.id, e)  # the oldest first, so that the newest are kept (``_noted``)
        for e in reversed(store.load_recent(working)):  # the oldest first: the newest is the one used last
            held.setdefault(e.id, e)
        super().__init__(
            key,
            held.values(),
            on_insert=inserted,
            on_rule=on_rule if on_rule is not None else store.save_rule,
            on_learn=on_learn,
            on_replace=on_replace,
        )
        with self._lock:
            self._trim()

    # --- what it holds -----------------------------------------------------------------------------

    def entities(self) -> list[Entity]:
        """The working set: the values in memory, not the whole vault, which its store alone holds."""
        return super().entities()

    def holds_since(self, version: int) -> bool:
        return version >= self._let_go

    def kept_since(self, version: int) -> bool:
        return version >= self._undone

    @contextlib.contextmanager
    def holding(self, *, scoped: bool = False) -> Iterator[None]:
        """While it is open, what this thread brings into memory (``prefetch``, the lookups) or keeps stays there, out
        of reach of the values let go, and the words it asked the store for are not asked again: a call (the texts of
        one request of the applications API, one text the engine masks) holds its values until it is done. Nested, it
        is one call; the working set is cut back as the outermost closes. ``scoped`` (the outermost's: an applications
        call): the fakes of the space its texts hold are brought in with their values (``prefetch``), and the values it
        keeps are kept together where the store can (its ``together``, Ruling T10i: one transaction and one commit for
        the call, where each value cost its own), committed as it closes. Its texts are masked with the detectors of the
        whole working set, as every text is (Ruling 10b-R1): what its words found and what it keeps are in memory
        too, and a value memory holds that no word of a text finds in the store (a secret glued to letters on both
        sides) is masked all the same. A call that ends in an error,
        or whose commit fails, keeps none of them, and memory forgets them (``_undo``): no text leaves masked with a
        fake the store does not keep. A commit that fails fails the call (``VaultUnread``)."""
        # No lock to open or close one, but to cut the working set back: a thread's call is its own entry, and a text
        # masked on one thread waits for no value another keeps meanwhile.
        thread = threading.get_ident()
        call = self._calls.get(thread)
        if call is None:
            call = self._calls[thread] = _Call(since=self._forgets)
            if scoped:
                call.scoped = True
                together = getattr(self._store, "together", None)
                if together is not None:
                    call.together = together()
        call.depth += 1
        failed = True
        try:
            yield
            failed = False
        finally:
            call.depth -= 1
            if call.depth == 0:
                try:
                    if call.together is not None:
                        self._ended(call, failed)
                finally:
                    del self._calls[thread]
                    if len(self._by_key) > self.working:
                        with self._lock:
                            self._stuck = 0  # what it held may go now
                            self._trim()

    def _call(self) -> _Call | None:
        """This thread's call under way, if any."""
        return self._calls.get(threading.get_ident())

    def whole_vault_call(self) -> bool:
        """``Vault.whole_vault_call``'s: this thread's call is an applications call (``holding(scoped=True)``)."""
        call = self._call()
        return call is not None and call.scoped

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
        masked (``kept_since``: it may hold a fake of theirs), and memory vouches for none (``holds_since``)."""
        with self._lock:
            undone = [entity for entity_id in call.kept if (entity := self._by_id.get(entity_id)) is not None]
            for entity in undone:
                self._unindex(entity)
            if undone:
                self._moved()
                self._let_go = self._undone = self.version

    # --- masking -----------------------------------------------------------------------------------

    def prefetch(self, texts: Sequence[str]) -> None:
        """The values ``texts`` hold, brought into memory in one call to the store: those whose anchor is one of their
        words (``words_in``), but for the words this thread's call asked for already, and of those the ones a text holds
        alone, as the vault's detectors would find them (Ruling T8a): the namesakes of a value stay out, and the working
        set is not flushed by a word many values share. Within a call, every value its words found is weighed against
        each text it masks (a value of a JSON document, read from the request's text), with no other call to the store.
        Raises VaultUnread when the store does not answer, and MaskingError on the event loop: no text is masked without
        its known values."""
        words: set[str] = set()
        for text in texts:
            if text:
                words |= words_in(text, dotted=True)
        call = self._call()
        new = words - call.asked if call is not None else words
        found: list[Entity] = []
        since = self._forgets
        # An applications call brings the fakes of the space its texts hold too: one lookup for both (review 10b's d).
        both = call is not None and call.scoped
        if new:
            if on_a_loop():
                raise MaskingError(ON_THE_LOOP)
            roles: Role | tuple[Role, ...] = ("original", "surrogate") if both else "original"
            found = [_kept(e) for e in self._asked(lambda: self._store.with_words(roles, new))]
        if call is None:
            held = _Weighing(found).held_in(texts)
        else:
            call.asked |= new
            if found:
                call.candidates.update((e.id, e) for e in found)
                call.weighing = None
            if call.weighing is None:
                call.weighing = _Weighing(call.candidates.values())
            held, since = call.weighing.held_in(texts), call.since
            if both and found:
                held += self._fakes_held(found, texts)
        if not held:
            return
        with self._lock:
            self._join(held, since)
            self._trim()

    def _fakes_held(self, found: list[Entity], texts: Sequence[str]) -> list[Entity]:
        """The fakes of the vault ``texts`` hold, among ``found`` (what the store found by the texts' words, as values
        or as fakes), as the detectors of fakes would find them: an applications call masks a fake of the space as a
        value of its own (the spec: an already masked text is not recognised), which the detectors of memory's fakes
        find, so it brings them in as it brings its values, whatever their age."""
        return _Weighing(found, fakes=True).held_in(texts)

    def weigh(self, texts: Sequence[str]) -> None:
        """The values ``texts`` hold among those this thread's call found already (``prefetch``), brought into memory
        with no call to the store: a text its conversation sent before, kept in another form than the request writes
        it (a JSON document's values written again with their letters), weighed before memory is asked whether it holds
        a value in clear (``Mediator._leaves_as_it_left``)."""
        call = self._call()
        if call is None or not call.candidates:
            return
        if call.weighing is None:
            call.weighing = _Weighing(call.candidates.values())
        held = call.weighing.held_in(texts)
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
        that is another value's or another fake. The entity is used now, and held by this thread's call."""
        with self._lock:
            entity = super().intern(etype, eclass, original, context, rule, generate, reads)
            self._use(entity)
            self._trim()
            return entity

    def learn(self, entity: Entity) -> None:
        with self._lock:
            super().learn(entity)
            held = self._by_id.get(entity.id)
            if held is not None:
                self._noted(held)
            self._trim()

    def forget(self, entity_id: str) -> Entity | None:
        """``Vault.forget``'s, in memory; and a value out of memory is looked for in the store, so that whoever
        forgets it there (``Runtime.forget``) does (not on the event loop). A reading of the store begun before is not
        brought back into memory."""
        with self._lock:
            self._forgets += 1
            self._gone[entity_id] = self._forgets
            while len(self._gone) > _GONE_KEPT:
                self._gone.popitem(last=False)
            entity = super().forget(entity_id)
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
            for entity_id in dict.fromkeys(ids):
                self._forgets += 1
                self._gone[entity_id] = self._forgets
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

    def prefetch_fakes(self, texts: Sequence[str]) -> None:
        """The fakes ``texts`` may hold, asked for in one call to the store (those whose anchor is one of their words)
        and kept with this thread's call, open (``holding``), for ``candidates_in`` to give each text its own: an
        unmask of a thousand texts asks once. Not brought into memory. Raises VaultUnread when the store does not
        answer, and MaskingError on the event loop."""
        call = self._call()
        if call is None:
            return  # nothing to keep them with: each text asks for its own
        words: set[str] = set()
        for text in texts:
            if text:
                words |= words_in(text, dotted=True)
        words -= call.fakes_asked
        if not words:
            return
        if on_a_loop():
            raise MaskingError(ON_THE_LOOP)
        for entity in self._asked(lambda: self._store.with_words("surrogate", words)):
            kept = _kept(entity)
            for word in words_of(kept, "surrogate"):  # its anchor, and its edges where it is restored inside a word
                call.fakes.setdefault(word, []).append(kept)
        call.fakes_asked |= words

    def fakes_out_of_memory(self, text: str) -> list[Entity]:
        """The entities whose fake ``text`` holds, as its detectors of fakes would find them, among those of the store
        (from what this thread's call asked for, ``prefetch_fakes``, else in one call): brought into memory, so that a
        conversation resumed with fakes of any age adopts them (``Engine.adopt``). Memory's alone on the event loop:
        none."""
        if on_a_loop():
            return []
        words = words_in(text, dotted=True)
        call = self._call()
        found: dict[str, Entity] = {}
        if call is not None and call.fakes_asked:
            for word in words & call.fakes_asked:
                found.update((e.id, e) for e in call.fakes.get(word, ()))
            words -= call.fakes_asked
        if words:
            found.update((e.id, _kept(e)) for e in self._asked(lambda: self._store.with_words("surrogate", words)))
        held = _Weighing(found.values(), fakes=True).held_in([text])
        if held:
            since = call.since if call is not None else self._forgets
            with self._lock:
                self._join(held, since)
                self._trim()
        return held

    def candidates_in(self, text: str) -> list[Entity]:
        """The entities whose fake may be in ``text`` (``Engine.unmask``), each once: those of the working set whose
        fake its detectors of fakes find, those held for good whose fake the text holds (``_HeldForms``: no word of
        the text finds them, glued to letters or without a word), and those of
        the store whose fake's word is one of the text's words (not brought into memory: a restore reads them once),
        from what this thread's call asked for already (``prefetch_fakes``), else in one call to the store. In a call
        that asked, the working set is not read for its fakes (Task 10b): every fake memory holds is the store's too,
        found by its words as the store's are, and reading each text for every fake of the working set was half of an
        unmask's time. On the event loop, memory's alone. Raises VaultUnread when the store does not answer."""
        found: dict[str, Entity] = {}
        call = self._call()
        if call is None or not call.fakes_asked or on_a_loop():
            echo = self.matchers.current().echo
            for span in read_escaped(text, lambda t: list(echo.detect(t, DetectContext()))):
                # As written, and as it reads; a fake, or how a fake written in escapes reads (0.9.9: `Giulia Martini`).
                for form in dict.fromkeys((text[span.start : span.end], span.of(text))):
                    entity = self._by_surrogate.get(form) or self._by_reading.get(form)
                    if entity is not None:
                        found[entity.id] = entity
        for entity_id in self._held_forms.held_in(text):  # those held for good whose fake the text holds
            entity = self._by_id.get(entity_id)
            if entity is not None:
                found.setdefault(entity_id, entity)
        if on_a_loop():
            return list(found.values())
        words = words_in(text, dotted=True)
        if call is not None and call.fakes_asked:
            for word in words & call.fakes_asked:
                for entity in call.fakes.get(word, ()):
                    found.setdefault(entity.id, entity)
            words -= call.fakes_asked
        if words:
            for entity in self._asked(lambda: self._store.with_words("surrogate", words)):
                found.setdefault(entity.id, _kept(entity))
        return list(found.values())

    # --- lookups -----------------------------------------------------------------------------------

    def by_surrogate(self, surrogate: str, as_read: bool = True) -> Entity | None:
        """``Vault.by_surrogate``'s: memory first, then the store, which keeps each fake as written and how each fake
        written in escapes reads (``fake_reading``), each found by the string's keyed hash: two probes at most, whatever
        shares a word with it. A string the store says is neither is remembered so, apart for the two questions."""
        found = self._held(self._by_surrogate.get(surrogate))
        if found is None and as_read:
            found = self._held(self._by_reading.get(surrogate))
        miss = _miss(surrogate, as_read)
        if found is not None or on_a_loop() or self._missed(miss):
            return found
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
        return any(self.in_memory(entity_id) is None for entity_id in missing)

    def hold(self, ids: Iterable[str], key: str | None = None) -> object | None:
        """The entities of ``ids`` used now, and those out of memory brought back from the store in one call (not on
        the event loop): a conversation's, before its answer is restored on the event loop, which finds them in memory
        alone. With ``key`` (the conversation's) they stay in memory while its answers are awaited: each hold under a
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
            self._join(found, since)
            self._trim()
        return hold

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
            if key != newest:
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
            if miss is not None and self._placed == placed:
                with self._misses_lock:
                    self._misses[miss] = None
                    while len(self._misses) > MISSES_KEPT:
                        self._misses.popitem(last=False)
            return None
        with self._lock:
            self._join([entity], since)
            self._trim()
            return self._by_id.get(entity.id)

    # --- the working set ---------------------------------------------------------------------------

    def resync(self) -> int:
        """The working set as the store has it now (a catch-up of the space, after a change it never heard): each of
        its entities the store no longer has, or holds otherwise (a fake given anew), leaves memory, to be found again
        as it is when a text needs it; one held for good is given the store's; the values without anchor kept elsewhere
        meanwhile are read again, and the newest ``held_at_most`` secrets held glued (``load_held``, review 10b's R4),
        as a build reads them. The working set is asked for, never the whole vault. Memory then vouches for no text
        masked before (``holds_since``): the change never heard may be in any. How many changed."""
        ids = list(self._by_id)
        now = {e.id: _kept(e) for e in self._store.by_ids(ids)}
        since = self._forgets
        unanchored = self._store.load_unanchored()
        load_held = getattr(self._store, "load_held", None)  # a store that keeps the secrets held glued apart
        glued = load_held(self.held_at_most) if load_held is not None and self.held_at_most else []
        with self._lock:
            changed = [e for i in ids if (e := self._by_id.get(i)) is not None and now.get(i) != e]
            for entity in changed:
                for_good = entity.id in self._for_good
                self._unindex(entity)
                fresh = now.get(entity.id)
                if fresh is not None and for_good:
                    self._place(fresh)
            # The values without anchor kept elsewhere meanwhile: no text's word finds them, so they are read again; and
            # the newest secrets held glued, the oldest first, so that the newest are those kept (``_noted``).
            self._join([e for e in unanchored if e.id not in self._by_id], since)
            self._join([e for e in reversed(glued) if e.id not in self._by_id], since)
            # A change never heard can be in any text masked before (Review Critical 1): memory no longer vouches for
            # one, and each is asked for again at its next use, as after values were let go.
            self._moved()
            self._let_go = self.version
        return len(changed)

    def _join(self, entities: Iterable[Entity], since: int) -> None:
        """``entities``, read from the store, in memory (under the lock): one held already is used now; one forgotten
        here since the reading began (``since``) stays out; one whose fake or value memory holds under another id puts
        that one out of memory, as a value another instance kept does (the store keeps one value for each fake and one
        entity for each value). Each is held by this thread's call. The version moves once."""
        moved = False
        for entity in entities:
            entity = _kept(entity)
            held = self._by_id.get(entity.id)
            if held is None:
                if self._gone.get(entity.id, 0) > since:
                    continue
                for other in self.displaced_by(entity):
                    self._unindex(other)
                self._place(entity)
                held, moved = entity, True
            self._use(held)
        if moved:
            self._moved()

    def _use(self, entity: Entity) -> None:
        """``entity`` used now, and held by this thread's call (under the lock)."""
        self._used[entity.id] = next(self._clock)
        call = self._call()
        if call is not None:
            call.held.add(entity.id)

    def _noted(self, entity: Entity) -> None:
        """What it keeps of ``entity``, in memory now (under the lock): used now, held for good when no word of a text
        may find it in the store (a value or a fake of no word, which a store loads with the vault's newest,
        ``load_unanchored``; or a secret held glued, ``held_glued``, the newest ``held_at_most`` alone), and its fake
        no miss."""
        self._use(entity)
        if value_word(entity) is None or fake_word(entity) is None:
            self._for_good.add(entity.id)
        elif held_glued(entity):  # held for good, the newest ``held_at_most`` alone (Ruling 10b-R3)
            self._glued[entity.id] = None
            self._glued.move_to_end(entity.id)
            self._for_good.add(entity.id)
            while len(self._glued) > self.held_at_most:
                older, _ = self._glued.popitem(last=False)
                self._for_good.discard(older)  # in memory as any value, let go when the working set is full
                self._held_forms.drop(older)
        else:
            self._for_good.discard(entity.id)
        if entity.id in self._for_good:
            self._held_forms.add(entity)
        else:
            self._held_forms.drop(entity.id)
        self._placed += 1
        reading = fake_reading(entity)
        with self._misses_lock:
            for string in (entity.surrogate, _miss(entity.surrogate, False), reading):
                if string is not None:
                    self._misses.pop(string, None)

    def _place(self, entity: Entity) -> None:
        super()._place(entity)
        self._noted(entity)

    def _unindex(self, entity: Entity) -> None:
        super()._unindex(entity)
        self._used.pop(entity.id, None)
        self._for_good.discard(entity.id)
        self._held_forms.drop(entity.id)
        self._glued.pop(entity.id, None)

    def _trim(self) -> None:
        """Past ``working`` values in memory, the least recently used leave it (under the lock) until a share of it
        (``EVICT_SHARE``) is free again: never one held for good, nor one a call under way holds. The version moves,
        and what was masked before it can no longer be vouched for by memory alone (``holds_since``)."""
        if len(self._by_key) <= max(self.working, self._stuck):
            return
        share = max(1, int(self.working * EVICT_SHARE))
        target = self.working - share
        kept = set(self._for_good).union(*(call.held for call in list(self._calls.values())), *self._sessions_held())
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
                self._unindex(entity)
                dropped = True
        # Still past it: the rest is held, by calls under way or for good. Not tried again at each value they keep,
        # but once a call closes, or a share more is in memory.
        self._stuck = len(self._by_key) + share if len(self._by_key) > target else 0
        if dropped:
            self._moved()
            self._let_go = self.version

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
