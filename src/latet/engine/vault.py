"""The vault: one entry per sensitive value, mapping it to its surrogate (and back).

Surrogates are derived deterministically from a per-install key, so the vault is effectively the
cache of a function plus rare collision counters. Entries can be persisted through ``on_insert``.
``on_insert`` keeps a new entity and answers None, or the entity of the same value another process
kept first (used in its place); it raises FakeTaken when the fake is another value's. ``on_replace``
keeps a new fake in place of an entry's old one, at load (``Vault.__init__``), and answers as
``on_insert`` does.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from functools import partial

from latet.engine.detectors.ids import ID_TYPES
from latet.engine.escapes import TAIL, ends_with_escape, plus_as_space, readings, written_like
from latet.engine.known import Matchers, ValueMatchers, value_chars
from latet.engine.modules import AGAIN, kept
from latet.engine.surrogates.data.names import NAME_EDGES, PUBLIC_MAIL_DOMAINS
from latet.engine.surrogates.generators import (
    GENERATORS,
    WIDE,
    WIDER,
    Drbg,
    email_domain,
    every_ip,
    gen_company,
    gen_data,
    gen_email,
    gen_secret,
    gen_short_secret,
    shape,
)
from latet.engine.types import SECRET_FAKE_SHORTEST, SHORT_FAKE_LONGEST, EntityClass

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 16  # fakes drawn in each form of a value's (``Vault._draws``) before the next, wider one
_MAX_GROWTH = 4  # characters a value of a structure's fake may gain when its shape has no free one left
# Fakes a store may refuse for one value (FakeTaken: another process gave them first), each a round trip to it on a
# server: past them the value fails as it did when it had 16 fakes to try (80, those of a value of a structure). The
# vault's own strings are passed over before the store is asked, so a full space costs no round trip.
_REFUSED_MOST = (_MAX_GROWTH + 1) * 16
# Values changed and not yet read by the vault's detectors that are kept track of one by one: past it, they read every
# value again (a catch-up of a whole vault on a server, with no masking meanwhile).
_TOUCHED_KEPT = 16_384
_SHAPE_SECRETS = {"PASSWORD", "PRIVATE_KEY", "URL_PASSWORD"}
# The longest value whose fake is drawn from a seed of the whole of it (``Vault._generate``): a longer one's seed holds
# a digest of it, so that its fake is drawn in time linear in its length. A PEM key of RSA 4096 bits, a certificate, a
# token keep the fakes they always had.
SEED_LONGEST = 4096
# Entity types whose surrogates are ordinary words: they are restored only as whole words.
_WORDLIKE = {"PERSON", "USERNAME", "CLIENT", "COMPANY", "ORG", "CUSTOM", "PROJECT", "STREET_ADDRESS", "CITY"}


def restored_whole(etype: str, eclass: EntityClass) -> bool:
    """A surrogate of this kind is restored only where it stands as a whole word."""
    return etype in _WORDLIKE or eclass in (EntityClass.CUSTOM, EntityClass.NER, EntityClass.LLM, EntityClass.STRUCT)


def short_fake(entity: Entity) -> bool:
    """Whether ``entity``'s fake is a secret's of ``SHORT_FAKE_LONGEST`` characters or fewer: one 0.10.0 gave a short
    secret (of its shape, kept for ever, R7), or one a recognizer of yours gives in its rule's shape. A text that never
    held the secret can hold it by chance inside an id or a number (`5821` in `order_id=9958213`). The one decision for
    the entity, of its fake as written, for its restores and for the detectors of its fake (``known._Part``)."""
    return entity.eclass == EntityClass.SECRET and len(entity.surrogate) <= SHORT_FAKE_LONGEST


def fake_restored_whole(entity: Entity, whole_vault: bool = False) -> bool:
    """Whether ``entity``'s fake is restored only where it stands as a whole word (no letter, digit or underscore
    next to it): a fake of a word (``restored_whole``); and, where the fakes of the whole vault are restored
    (``whole_vault``: ``Engine.unmask``, which reads every fake of the vault for every text), a short one
    (``short_fake``), which a text can hold by chance inside an id or a number. A conversation's restore, of the fakes
    it was shown alone, restores a short fake inside a word too, as 0.10.0 did: a secret glued to letters (`xhunter2y`)
    was masked there, and comes back there, as every other fake does."""
    return restored_whole(entity.etype, entity.eclass) or (whole_vault and short_fake(entity))


def _shows(candidate: str, original: str) -> bool:
    """Whether a fake shows its value: it is it, or holds it, whatever the case (``emily37`` for ``emily``). Such a fake
    leaves the value in the text the model reads, and the final check finds it there and stops the request (a computer
    named ``host`` and its ``host-1a2b3c``): it is drawn again, whatever made it (a recognizer's, a country's)."""
    return original.casefold() in candidate.casefold()


def _occurs(candidate: str, text: str, whole: bool) -> bool:
    """``candidate`` is in ``text``: as a whole word, the way a restore would find it, when ``whole``."""
    if not whole:
        return candidate in text
    word = str.isalnum
    at = text.find(candidate)
    while at >= 0:
        end = at + len(candidate)
        before = at == 0 or not (word(text[at - 1]) or text[at - 1] == "_")
        before = before or ends_with_escape(text[max(0, at - TAIL) : at])
        after = end == len(text) or not (word(text[end]) or text[end] == "_")
        if before and after:
            return True
        at = text.find(candidate, at + 1)
    return False


def _in_form(generate: Callable[[str, Drbg, int], str], wide: int) -> Callable[[str, Drbg], str]:
    """``generate``, drawing its form ``wide`` (``generators.WIDE``)."""
    return lambda original, rng: generate(original, rng, wide)


class MaskingError(RuntimeError):
    """No collision-free surrogate could be produced: the caller must fail closed."""


class FakeTaken(Exception):
    """The fake made for a value is already another value's in the store (another process gave it first): the vault
    tries its next counter."""


class EntryGone(Exception):
    """The value's entry is no longer in the store (another process forgot it meanwhile): the vault forgets it too."""


@dataclass(frozen=True, slots=True)
class Entity:
    id: str
    etype: str
    eclass: EntityClass
    original: str
    surrogate: str
    counter: int
    created: float
    rule: str | None = None  # the detector rule of its first sighting (None: recorded before rules were kept)
    # How its value was found: where the text's escapes read as what they stand for (True, ``Span.value``: the decoded
    # reading), or as it is written (False); None: kept before Latet recorded it (0.9.9 and earlier). A column of the
    # store's, `decoded`, in clear as the rule is.
    decoded: bool | None = None


def written_in_words(etype: str, eclass: EntityClass) -> bool:
    """A value of this kind is written in words, a space between two (a name, a term, a street): a `+` in it, kept as
    a form's value writes it (`Mario+Rossi`), is a space. In any other value it is a sign: a phone's `+39`, a
    sub-address's `+crm`, a token's (``escapes.readings``). Only for an entry kept before Latet recorded how its value
    was found (``written_in_escapes``)."""
    return eclass != EntityClass.STRUCT and restored_whole(etype, eclass)


ESCAPE_SIGNS = ("%", "+", "\\", "&")  # what a value written in escapes holds one of, at least


def written_in_escapes(entity: Entity) -> bool | None:
    """Whether ``entity``'s value is kept as written in escapes, its fake given the forms and the reading of such a
    value (``fake_reading``, ``restores_for``): None where it is not, else whether a `+` in it is a form's space (the
    ``words`` of ``escapes.readings``). From how the value was found (``Entity.decoded``): found as it is written, it
    reads as it is written; found where escapes read as what they stand for, its `+` were a form's spaces where nothing
    else of it is an escape (``escapes.plus_as_space``: `333+1234567`), or in a value written in words, as 0.9.9 read
    them (`Ren\\u00e9+Dupont` in a link held in a JSON string). An entry kept before that was recorded keeps 0.9.9's
    rule: a `+` is a form's space in a value written in words alone (``written_in_words``)."""
    if entity.decoded is False or not any(sign in entity.original for sign in ESCAPE_SIGNS):
        return None
    words = written_in_words(entity.etype, entity.eclass)
    if entity.decoded is None:
        return words
    return plus_as_space(entity.original) or words


def fake_reading(entity: Entity) -> str | None:
    """How the fake of a value kept as written in escapes reads, where it reads otherwise (`Nicola Greco` for
    `Nicola%20Greco`): kept apart from every other value and fake as a fake is (Ruling 099-R2), sent back in a later
    text as a fake is (an echo), and given back as the value reads (``restores_for``). None for a value found as it is
    written, and for one that reads as it is written (``written_in_escapes``)."""
    words = written_in_escapes(entity)
    if words is None:
        return None
    read = readings(entity.original, entity.surrogate, words=words)
    return read[0] if read is not None and read[0] != entity.surrogate else None


def _kept(entity: Entity) -> Entity:
    """``entity`` with its rule as the vault keeps it (``modules.kept``): one kept before with a field's name in it, by
    the field's kind. A name the name model found before it left quotes out (`'Francesca Solieri`, from a string
    console.table printed) is known without them: found with it, the quote went into the fake (`Marta Galli'`)."""
    rule = kept(entity.rule)
    original = entity.original
    if entity.eclass == EntityClass.NER:
        original = original.strip(NAME_EDGES) or original
    return (
        entity if (rule, original) == (entity.rule, entity.original) else replace(entity, rule=rule, original=original)
    )


def derive_keys(root: bytes) -> dict[str, bytes]:
    return {name: hmac.new(root, name.encode(), hashlib.sha256).digest() for name in ("id", "surrogate", "cache")}


def normalize(etype: str, value: str) -> str:
    if etype in ("EMAIL", "DOMAIN", "HOSTNAME"):
        return value.lower()
    if etype == "PHONE":
        return "".join(c for c in value if c.isdigit())
    if etype in ("IBAN", "CREDIT_CARD", "VAT_NUMBER") or etype in ID_TYPES:
        return "".join(c for c in value if c.isalnum()).upper()
    return value


class Vault:
    def __init__(
        self,
        root_key: bytes,
        entities: Iterable[Entity] = (),
        on_insert: Callable[[Entity], Entity | None] | None = None,
        on_rule: Callable[[Entity], None] | None = None,
        on_learn: Callable[[Entity], None] | None = None,
        on_replace: Callable[[Entity, Entity], Entity | None] | None = None,
    ) -> None:
        keys = derive_keys(root_key)
        self._k_id = keys["id"]
        self._k_sur = keys["surrogate"]
        self.cache_key = keys["cache"]
        self._lock = threading.RLock()
        self._by_key: dict[tuple[str, str], Entity] = {}
        self._by_surrogate: dict[str, Entity] = {}
        self._by_reading: dict[str, Entity] = {}  # how a fake written in escapes reads (``fake_reading``)
        self._by_original: dict[str, Entity] = {}
        self._by_id: dict[str, Entity] = {}
        self._on_insert = on_insert
        self._on_rule = on_rule
        self._on_learn = on_learn
        self.version = 0
        # Told each time the version moves (a value kept, learnt or forgotten), under the lock, from the thread that
        # moved it: the Live Reviewer's previews of the texts still waiting are made again from it
        # (``Mediator.live_changed``), which must return at once.
        self.on_change: Callable[[], None] | None = None
        # Why it lacks a value another process keeps, which ``on_learn`` refused (``learn``): every text masked or
        # checked through it is refused with these words from then on (``Masker``), since only its own detector would
        # find that value. None: it lacks none.
        self.lacking: str | None = None
        # The characters its values' detectors hold of them, each value's or its fake's, the longer (``value_chars``):
        # counted as each value comes and goes, where the detectors count them at their next reading. A server weighs a
        # value's length by it as the value is kept (``server.spaces``).
        self.chars = 0
        # The values changed since the detectors of its values last read them (``take_changes``), or every one.
        self._touched: dict[tuple[str, str], None] = {}
        self._touched_all = True
        # The detectors of its values and of their fakes (``latet.engine.known``): made by the first reading and shared
        # by every masker of this vault, a project's included, so that a process holding many maskers (an identity's
        # space on a server, one for each project) holds one pair, made again in part as the vault changes.
        self._matchers: ValueMatchers | None = None
        for e in entities:
            self._index(_kept(e))
        if on_replace is not None:
            # A fake kept before the vault refused one that shows its value (`emily59` for `emily`) leaves the value in
            # the text the model reads: drawn again at load, and kept in the store in its place.
            for entity in [e for e in self._by_key.values() if _shows(e.surrogate, e.original)]:
                self._redraw(entity, on_replace)

    # --- lookups ---------------------------------------------------------------------------------

    def entity_id(self, etype: str, original: str) -> str:
        """The id of ``original``'s entry, a keyed hash of its type and value, in the vault's generation
        (``_generation``): the first, 0, adds nothing, so that every id made before generations were is made again as
        it was; a later one (a value forgotten by itself and met again, Task 11b) gives another, so that nothing kept
        under the old id (a conversation's, a history's) names the value met again (Ruling 11b-R1). The generation
        leads, after a character no type begins with: never the text of generation 0's for another value."""
        generation = self._generation()
        named = f"\x00generation\x00{generation}\x00{etype}\x00{original}" if generation else f"{etype}\x00{original}"
        return hmac.new(self._k_id, named.encode(), hashlib.sha256).hexdigest()[:20]

    def by_surrogate(self, surrogate: str, as_read: bool = True) -> Entity | None:
        """The entity of a fake, or (``as_read``) of how a fake written in escapes reads (`Nicola Greco` for
        `Nicola%20Greco`). A store keeps one value per fake as written: what another process kept is compared so."""
        found = self._by_surrogate.get(surrogate)
        return found if found is not None or not as_read else self._by_reading.get(surrogate)

    def by_original(self, original: str) -> Entity | None:
        return self._by_original.get(original)

    def get(self, entity_id: str) -> Entity | None:
        return self._by_id.get(entity_id)

    def in_memory(self, entity_id: str) -> Entity | None:
        """The entity this vault holds in memory under ``entity_id``, its store never asked (an API key's vault, which
        holds its working set alone, asks its store in ``get``)."""
        return self._by_id.get(entity_id)

    def forgot_any(self, ids: Sequence[str]) -> bool:
        """Whether a value of the entries ``ids`` (those a text kept as it left names) was forgotten since, so that the
        text is masked anew rather than sent with an old fake (Task 11b, Ruling 11b-R1): never here. A person's Forget
        leaves a conversation's texts as they left (review 11b's N2); an API key's vault, whose forgets move it to a new
        generation of fakes and entries, says so (``engine.stored.StoredVault``)."""
        return False

    def entities(self) -> list[Entity]:
        with self._lock:
            return list(self._by_key.values())

    def candidates_in(self, text: str) -> list[Entity]:
        """The entities whose fake may be in ``text``, which restoring it from the whole vault reads
        (``Engine.unmask``): here every one the vault holds, whatever the text. A vault that keeps most of its values
        elsewhere overrides it to narrow them to the text's, and the engine then makes their restores for each text."""
        return self.entities()

    def prefetch(self, texts: Sequence[str]) -> None:
        """Bring into memory the values ``texts`` may hold, before they are masked, so that its detectors
        (``matchers``) find them as they find every value in memory: here nothing, as every value is in memory. A vault
        that keeps most of its values elsewhere (``engine.stored.StoredVault``) asks for them there, once for all the
        texts."""

    def weigh(self, texts: Sequence[str]) -> None:
        """Bring into memory the values ``texts`` hold among those this thread's call found already (``prefetch``),
        with no other call to a store: a text kept in another form than its request writes it. Here nothing, as every
        value is in memory."""

    def hold(self, ids: Iterable[str], key: str | None = None) -> object | None:
        """Bring the entities of ``ids`` into memory and use them now, those a conversation was shown, so that its
        answer, restored on the event loop where nothing waits on a store, finds each one there; with ``key``, keep
        them there until ``release(key, held)``, ``held`` what this returns: here nothing, as every entity is in
        memory."""
        return None

    def release(self, key: str, held: object | None) -> None:
        """What one ``hold`` kept in memory under ``key`` (``held``, its answer) may go again: here nothing."""

    def fakes_out_of_memory(self, text: str) -> list[Entity]:
        """The entities whose fake ``text`` holds and that its memory does not (its detectors of fakes cannot find
        them): here none, as every fake is in memory."""
        return []

    def prefetch_fakes(self, texts: Sequence[str]) -> None:
        """Ask once, for all of ``texts``, for the fakes they may hold, which ``candidates_in`` then gives for each of
        them while this thread's call lasts (``holding``): here nothing, as every fake is in memory."""

    @contextlib.contextmanager
    def holding(self, *, scoped: bool = False) -> Iterator[None]:
        """While it is open, what this thread brings into memory (``prefetch``) or keeps stays there: a call holds the
        values it found until it is done with them. Here nothing is ever let go. ``scoped``: a call that brings every
        value its texts hold into memory first (an applications call), which a vault that keeps most of its values
        elsewhere masks with the detectors of those alone (``engine.stored``); here every value is read, as always."""
        yield

    def whole_vault_call(self) -> bool:
        """Whether this thread's call is an applications call (``holding(scoped=True)``), whose texts an unmask of the
        whole vault restores (``Engine.unmask``): its masking takes a short fake (``short_fake``) for a fake only where
        it stands as a whole word, as that unmask restores it (``known.Matched.echo_inside``). Never here: a person's
        vault serves conversations alone."""
        return False

    def holds_since(self, version: int) -> bool:
        """Whether every value it learnt or kept since ``version`` is still in its memory, so that its detectors alone
        find each one in a text checked at that version (``Engine.mask_text``'s cache): here always. A vault that lets
        values go (``engine.stored.StoredVault``) answers no once it let some go since."""
        return True

    def kept_since(self, version: int) -> bool:
        """Whether every fake it gave since ``version`` is still its own, so that a text masked at that version may be
        sent again as it was (``Engine.mask_text``'s cache): here always. A vault whose call can fail to keep the values
        it made (``engine.stored.StoredVault``: an applications call's, kept together) answers no once one did: a text
        masked then may hold a fake of theirs, which no unmask would find."""
        return True

    def displaced_by(self, entity: Entity) -> list[Entity]:
        """The entities its memory holds under ``entity``'s fake or value with another id, which knowing ``entity``
        puts out (a value forgotten elsewhere, its fake or its value given anew since): in memory alone, as a store
        keeps one value for each fake and one entity for each value (``server.spaces``)."""
        found = (self._by_surrogate.get(entity.surrogate), self._by_original.get(entity.original))
        return list({other.id: other for other in found if other is not None and other.id != entity.id}.values())

    @property
    def count(self) -> int:
        """How many values it knows, without copying them (a server weighs every space by it)."""
        return len(self._by_key)

    def is_surrogate(self, value: str) -> bool:
        """Whether ``value`` is a fake: one as written, or how a fake written in escapes reads, unless that reading is
        a value the vault knows too (a real Nicola Greco learnt after `Nicola%20Greco` went out): the value wins, and
        is masked and checked as one."""
        return value in self._by_surrogate or (value in self._by_reading and value not in self._by_original)

    def a_value_over_a_reading(self, forms: Iterable[str]) -> bool:
        """Whether ``forms`` (how a text found as a fake is written and how it reads) are no fake as written, and one is
        how a fake written in escapes reads and a value the vault knows too: the value wins (``is_surrogate``)."""
        forms = list(forms)
        if any(form in self._by_surrogate for form in forms):
            return False
        return any(form in self._by_reading and form in self._by_original for form in forms)

    def _taken(self, candidate: str) -> bool:
        """Whether ``candidate`` is a value the vault knows, a fake, or how a fake written in escapes reads: a fake
        drawn so would leave a real name in the text, or bring another value back."""
        return candidate in self._by_surrogate or candidate in self._by_original or candidate in self._by_reading

    def _read_as(self, entity: Entity, kept: bool) -> None:
        """How ``entity``'s fake reads, where it is written in escapes and reads otherwise: indexed (``kept``) or
        not."""
        reading = fake_reading(entity)
        if reading is None:
            return
        if kept:
            self._by_reading[reading] = entity
        elif self._by_reading.get(reading) is entity:
            del self._by_reading[reading]

    @property
    def matchers(self) -> Matchers:
        """The detectors of its values and of their fakes, one for the vault (``latet.engine.known``)."""
        found = self._matchers
        if found is None:
            with self._lock:
                if self._matchers is None:
                    self._matchers = ValueMatchers(self)
                found = self._matchers
        return found

    def take_changes(
        self, *, whole: bool = False
    ) -> tuple[int, list[Entity] | None, dict[tuple[str, str], Entity | None]]:
        """For its detectors (``ValueMatchers``, the one reader): in one step under its lock, its version and either
        every entity it holds (the first reading, ``whole``, or more values changed than kept track of) or, for each
        value changed since the last call, its entity now (None: gone). What changes after is in the next call's."""
        with self._lock:
            touched, self._touched = self._touched, {}
            if whole or self._touched_all:
                self._touched_all = False
                return self.version, list(self._by_key.values()), {}
            return self.version, None, {key: self._by_key.get(key) for key in touched}

    def _touch(self, key: tuple[str, str]) -> None:
        """``key``'s entity changed (under the lock): its detectors read it again. Past ``_TOUCHED_KEPT`` values changed
        and not read yet, every value is read again instead."""
        if not self._touched_all:
            self._touched[key] = None
            if len(self._touched) > _TOUCHED_KEPT:
                self._touched_all, self._touched = True, {}

    def forget(self, entity_id: str) -> Entity | None:
        """Drop an entity: its surrogate is no longer restored, and its original no longer known."""
        with self._lock:
            entity = self._by_id.get(entity_id)
            if entity is None:
                return None
            self._unindex(entity)
            self._moved()
            return entity

    def learn(self, entity: Entity) -> None:
        """An entity another process kept (another instance of a Latet server): known here from now on, as if it were
        made here. Nothing is saved: the store has it already. It takes the place of what this vault knew under the same
        id (a rule learned elsewhere, a value kept again with another fake), in memory only, so that the next sighting
        finds, masks and restores the new one alone.

        A value it does not know goes through ``on_learn`` first. One refused there (a ``MaskingError``, its words
        naming no value: on a server, its space past its share of the memory) is not learnt and the refusal raised: the
        vault lacks it from then on (``lacking``), and its version moves, so that a text checked clean before is checked
        again."""
        entity = _kept(entity)
        with self._lock:
            known = self._by_id.get(entity.id)
            if known == entity:
                return
            if known is None and self._on_learn is not None:
                try:
                    self._on_learn(entity)
                except MaskingError as refused:
                    self.lacking = str(refused)
                    self._moved()
                    raise
            # The lookups take no lock (a response restored, a text masked in another thread): each index goes from the
            # old entity to the new one in one step, so the value is never unknown in between. What only the old one
            # still holds (its fake, when the new one has another) goes last.
            self._touch((entity.etype, entity.original))
            if known is not None:
                self._touch((known.etype, known.original))
            self._keyed((entity.etype, entity.original), entity)
            self._by_surrogate[entity.surrogate] = entity
            self._read_as(entity, True)
            first = self._by_original.get(entity.original)
            if first is None or first is known:  # another type's entity of the same value stays: its sighting was first
                self._by_original[entity.original] = entity
            self._by_id[entity.id] = entity
            if known is not None:
                if self._by_key.get((known.etype, known.original)) is known:
                    self._keyed((known.etype, known.original), None)
                if self._by_surrogate.get(known.surrogate) is known:
                    del self._by_surrogate[known.surrogate]
                self._read_as(known, False)
                if self._by_original.get(known.original) is known:
                    del self._by_original[known.original]
            self._moved()

    # --- interning -------------------------------------------------------------------------------

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
        """The entity for ``original``, creating it (and its surrogate) on first sight.

        ``context`` is the text being masked: a candidate surrogate that already occurs in it would
        make the restore ambiguous, so it is rejected. ``rule`` is the detector rule that found it,
        kept for the dashboard (like the type, the first sighting's wins), as it is kept in clear
        (``modules.kept``: a field's name in it, which can be a value, is named by its kind). ``generate``
        makes the fake for a type of the user's own recognizers (it follows the rule's shape and checksum).
        A candidate that holds ``original``, whatever the case, is rejected as one equal to it (``_shows``).
        ``reads``: what ``original`` reads, where it is written in escapes (`Niccol%C3%B2%20Ferri`): the fake is drawn
        from it (a name for a name, an address for an address), and written in the escapes ``original`` is written
        in where they are part of the syntax (``escapes.written_like``), so that a URL stays one.
        """
        rule = kept(rule)
        with self._lock:
            # One surrogate per value: the type of its first sighting wins, so a value detected
            # as two types (an ASSIGNED_SECRET here, a PASSWORD there) is masked the same everywhere.
            found = self._by_original.get(original)
            if found is not None:
                if found.rule is None and rule and rule not in AGAIN:
                    found = self._learn_rule(found, rule)
                return found
            reads = reads if reads is not None and reads != original else None
            domain_surrogate = None
            if etype == "EMAIL":
                domain = email_domain(reads or original)
                if domain and domain.lower() not in PUBLIC_MAIL_DOMAINS:
                    dom = self.intern("DOMAIN", EntityClass.PII, domain.lower(), context, rule)
                    domain_surrogate = dom.surrogate
            whole = restored_whole(etype, eclass)
            refused = 0  # fakes the store refused: another process's
            for counter, candidate in self._draws(etype, eclass, original, domain_surrogate, generate, reads):
                if reads is not None:
                    # As it reads, the fake is none of the values the vault knows nor of their fakes, nor in the text,
                    # as any fake is none: the model would read a real name, or write one that comes back as another
                    # value (the reviewer named in the same text as the fake reads).
                    if (
                        _shows(candidate, reads)
                        or self._taken(candidate)
                        or (context and _occurs(candidate, context, whole))
                    ):
                        continue
                    candidate = written_like(candidate, original, reads)
                if (
                    _shows(candidate, original)
                    or self._taken(candidate)
                    or (context and _occurs(candidate, context, whole))
                ):
                    continue
                entity = Entity(
                    id=self.entity_id(etype, original),
                    etype=etype,
                    eclass=eclass,
                    original=original,
                    surrogate=candidate,
                    counter=counter,
                    created=time.time(),
                    rule=rule,
                    decoded=reads is not None,
                )
                if self._on_insert is not None:
                    try:
                        first = self._on_insert(entity)
                    except FakeTaken:
                        refused += 1  # another process gave this fake to another value: the next counter
                        if refused >= _REFUSED_MOST:
                            break
                        continue
                    if first is not None:
                        entity = first  # the same value, kept first by another process
                self._index(entity)
                return entity
            raise MaskingError(f"no collision-free surrogate for a {etype}")

    def _redraw(self, entity: Entity, keep: Callable[[Entity, Entity], Entity | None]) -> None:
        """``entity``, whose fake shows its value, given the fake its value gets now, as ``intern`` draws it (a fake
        shown by none of the vault's strings, a value's or a fake's, nor showing its own), kept by ``keep`` in place of
        the old one: None when kept so, or the entry as another process changed it first, used in its place; FakeTaken
        when the store knows that fake already, and the next counter is tried; EntryGone when another process forgot
        the value meanwhile, and the vault forgets it too. Its id stays (the same value), so that the texts and the
        history that name it find it again. When no fake can be drawn, the old one stays, and the log says so by the
        entry's id."""
        domain_surrogate = None
        if entity.etype == "EMAIL":
            domain = email_domain(entity.original)
            if domain and domain.lower() not in PUBLIC_MAIL_DOMAINS:
                domain_surrogate = self.intern("DOMAIN", EntityClass.PII, domain.lower(), rule=entity.rule).surrogate
        refused = 0
        for counter, candidate in self._draws(entity.etype, entity.eclass, entity.original, domain_surrogate, None):
            if _shows(candidate, entity.original) or self._taken(candidate):
                continue
            drawn = replace(entity, surrogate=candidate, counter=counter)
            try:
                kept = keep(entity, drawn)
            except FakeTaken:
                refused += 1  # another process gave this fake to another value: the next counter
                if refused >= _REFUSED_MOST:
                    break
                continue
            except EntryGone:
                self._unindex(entity)  # forgotten by another process: known here no more
                return
            self._unindex(entity)
            self._index(kept if kept is not None else drawn)
            return
        log.warning("vault entry %s keeps a fake that shows its value: no other could be drawn", entity.id)

    def _learn_rule(self, entity: Entity, rule: str) -> Entity:
        """A value from a vault older than the rules: keep the rule it is found by now.

        The version stays: what is masked and restored does not change, only what the dashboard says.
        """
        updated = replace(entity, rule=rule)
        if self._by_key.get((entity.etype, entity.original)) is entity:
            self._keyed((entity.etype, entity.original), updated)
        if self._by_surrogate.get(entity.surrogate) is entity:
            self._by_surrogate[entity.surrogate] = updated
        if (reading := fake_reading(entity)) is not None and self._by_reading.get(reading) is entity:
            self._by_reading[reading] = updated
        if self._by_original.get(entity.original) is entity:
            self._by_original[entity.original] = updated
        self._by_id[entity.id] = updated
        if self._on_rule is not None:
            self._on_rule(updated)
        return updated

    def _draws(
        self,
        etype: str,
        eclass: EntityClass,
        original: str,
        domain_surrogate: str | None,
        generate: Callable[[str, Drbg], str] | None,
        reads: str | None = None,
    ) -> Iterator[tuple[int, str]]:
        """The fakes ``original`` may get, in the order they are tried, each with the counter it is kept with:
        ``_MAX_ATTEMPTS`` of each of its forms, the first the one Latet always drew (counters 0 to 15: a value that
        finds a free fake there gets the fake it always got), then the wider ones, so that a vault never runs out of
        fakes while a wider form has one. A value of a structure whose shape has no free fake left (hundreds of 3-digit
        ids in one output) gets a longer one, a character at a time. Words drawn from lists (names, companies, towns)
        go through each wider form of their generator (``generators.WIDER``), then the value's own shape, as a value no
        generator can fake gets. An IPv4 address goes to the benchmarking block too, then to every address of its
        blocks in turn (``every_ip``). A recognizer of yours (``generate``) has its one form."""

        def draw(counter: int, grow: int = 0, wide: int = 0) -> str:
            return self._generate(etype, eclass, original, counter, domain_surrogate, generate, grow, reads, wide)

        if eclass == EntityClass.STRUCT:
            for grow in range(_MAX_GROWTH + 1):
                for counter in range(_MAX_ATTEMPTS):
                    yield counter, draw(counter, grow)
            return
        drawn = reads if reads is not None else original
        mine = generate is None
        company = etype not in GENERATORS and eclass == EntityClass.CUSTOM and drawn.replace(" ", "").isalpha()
        words = mine and (etype in WIDER or company)
        ip = mine and etype == "IP_ADDRESS"
        forms = WIDER.get(etype, WIDER["COMPANY"]) if words else 1 if ip else 0
        counter = 0
        for wide in range(forms + 1):
            for _ in range(_MAX_ATTEMPTS):
                yield counter, draw(counter, wide=wide)
                counter += 1
        if words:
            for _ in range(_MAX_ATTEMPTS):
                yield counter, shape(drawn, Drbg(self._k_sur, self._seed(etype, original, counter) + b"\x00shape"))
                counter += 1
        elif ip:
            for address in every_ip(drawn, Drbg(self._k_sur, self._seed(etype, original, counter))):
                yield counter, address
                counter += 1

    def _seed(self, etype: str, original: str, counter: int) -> bytes:
        """What the fake of ``original`` (as written) is drawn from at ``counter``, in the vault's generation
        (``_generation``): the first, 0, adds nothing, so that every fake drawn before generations were is drawn
        again as it was."""
        value = normalize(etype, original)
        if len(value) > SEED_LONGEST:  # its digest: the generator hashes the seed again for every 32 bytes it draws
            value = "sha256:" + hashlib.sha256(value.encode()).hexdigest()
        generation = self._generation()
        if generation:
            return f"{etype}\x00{value}\x00{counter}\x00generation\x00{generation}".encode()
        return f"{etype}\x00{value}\x00{counter}".encode()

    def _generation(self) -> int:
        """The generation the vault draws its new fakes in: 0 here. An API key's vault draws in its store's, which each
        forget of values by themselves moves (``engine.stored.StoredVault``, Task 11b), so that a value forgotten and
        met again gets a new fake, not the one the texts masked before hold."""
        return 0

    def _generate(
        self,
        etype: str,
        eclass: EntityClass,
        original: str,
        counter: int,
        domain_surrogate: str | None,
        generate: Callable[[str, Drbg], str] | None = None,
        grow: int = 0,
        reads: str | None = None,
        wide: int = 0,
    ) -> str:
        """A fake for ``original``, drawn from it, or from what it reads (``reads``) where it is written in escapes (the
        seed is the value as written: each written form has a fake of its own), in its generator's form ``wide``
        (``_draws``)."""
        seed = self._seed(etype, original, counter)
        original = reads if reads is not None else original
        if grow:  # the shape of a value of a structure, `grow` characters longer: its last one again
            return gen_data(original + original[-1] * grow, Drbg(self._k_sur, seed + f"\x00grow{grow}".encode()))
        rng = Drbg(self._k_sur, seed)
        generator = generate or GENERATORS.get(etype)
        if etype == "EMAIL":
            domain_of = (lambda _d: domain_surrogate) if domain_surrogate else None
            generator = partial(gen_email, domain_surrogate=domain_of, wide=wide)
        elif wide and generate is None and etype in WIDE:
            generator = _in_form(WIDE[etype], wide)
        if generator is not None:
            try:
                return generator(original, rng)
            except (ValueError, AssertionError, IndexError):  # no fake of its kind (every name would show it)
                return shape(original, Drbg(self._k_sur, seed + b"\x00shape"))
        if eclass == EntityClass.STRUCT:
            return gen_data(original, rng)
        if eclass == EntityClass.SECRET:
            if len(original) < SECRET_FAKE_SHORTEST:  # 0.10.2: a fake no id or number holds by chance
                return gen_short_secret(original, rng)
            return shape(original, rng) if etype in _SHAPE_SECRETS else gen_secret(original, rng)
        if eclass == EntityClass.CUSTOM and original.replace(" ", "").isalpha():
            try:
                return gen_company(original, rng, wide)
            except IndexError:  # every company's word would show it
                return shape(original, Drbg(self._k_sur, seed + b"\x00shape"))
        return shape(original, rng)

    def _keyed(self, key: tuple[str, str], entity: Entity | None) -> None:
        """``key``'s entity set in its place (None: taken out), counted in ``chars`` (under the lock)."""
        old = self._by_key.get(key)
        if old is not None:
            self.chars -= value_chars(old)
        if entity is None:
            self._by_key.pop(key, None)
        else:
            self._by_key[key] = entity
            self.chars += value_chars(entity)

    def _index(self, entity: Entity) -> None:
        self._place(entity)
        self._moved()

    def _place(self, entity: Entity) -> None:
        """``entity`` in every index (under the lock), the version left as it is: ``_index`` moves it."""
        self._touch((entity.etype, entity.original))
        self._keyed((entity.etype, entity.original), entity)
        self._by_surrogate[entity.surrogate] = entity
        self._read_as(entity, True)
        self._by_original.setdefault(entity.original, entity)
        self._by_id[entity.id] = entity

    def _moved(self) -> None:
        """The version moved (under the lock), and ``on_change`` told."""
        self.version += 1
        if self.on_change is not None:
            self.on_change()

    def _unindex(self, entity: Entity) -> None:
        """Take an entity out of every index, in memory only: nothing reaches the store."""
        self._touch((entity.etype, entity.original))
        self._by_id.pop(entity.id, None)
        self._keyed((entity.etype, entity.original), None)
        if self._by_surrogate.get(entity.surrogate) is entity:
            del self._by_surrogate[entity.surrogate]
        self._read_as(entity, False)
        if self._by_original.get(entity.original) is entity:
            del self._by_original[entity.original]
