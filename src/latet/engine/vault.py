"""The vault: one entry per sensitive value, mapping it to its surrogate (and back).

Surrogates are derived deterministically from a per-install key, so the vault is effectively the
cache of a function plus rare collision counters. Entries can be persisted through ``on_insert``.
``on_insert`` keeps a new entity and answers None, or the entity of the same value another process
kept first (used in its place); it raises FakeTaken when the fake is another value's. ``on_replace``
keeps a new fake in place of an entry's old one, at load (``Vault.__init__``), and answers as
``on_insert`` does.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import partial

from latet.engine.detectors.ids import ID_TYPES
from latet.engine.escapes import TAIL, ends_with_escape
from latet.engine.known import ValueMatchers, value_chars
from latet.engine.modules import AGAIN, kept
from latet.engine.surrogates.data.names import NAME_EDGES, PUBLIC_MAIL_DOMAINS
from latet.engine.surrogates.generators import (
    GENERATORS,
    Drbg,
    email_domain,
    gen_company,
    gen_data,
    gen_email,
    gen_secret,
    shape,
)
from latet.engine.types import EntityClass

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 16
_MAX_GROWTH = 4  # characters a value of a structure's fake may gain when its shape has no free one left
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
        return hmac.new(self._k_id, f"{etype}\x00{original}".encode(), hashlib.sha256).hexdigest()[:20]

    def by_surrogate(self, surrogate: str) -> Entity | None:
        return self._by_surrogate.get(surrogate)

    def by_original(self, original: str) -> Entity | None:
        return self._by_original.get(original)

    def get(self, entity_id: str) -> Entity | None:
        return self._by_id.get(entity_id)

    def entities(self) -> list[Entity]:
        with self._lock:
            return list(self._by_key.values())

    @property
    def count(self) -> int:
        """How many values it knows, without copying them (a server weighs every space by it)."""
        return len(self._by_key)

    def is_surrogate(self, value: str) -> bool:
        return value in self._by_surrogate

    @property
    def matchers(self) -> ValueMatchers:
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
            first = self._by_original.get(entity.original)
            if first is None or first is known:  # another type's entity of the same value stays: its sighting was first
                self._by_original[entity.original] = entity
            self._by_id[entity.id] = entity
            if known is not None:
                if self._by_key.get((known.etype, known.original)) is known:
                    self._keyed((known.etype, known.original), None)
                if self._by_surrogate.get(known.surrogate) is known:
                    del self._by_surrogate[known.surrogate]
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
    ) -> Entity:
        """The entity for ``original``, creating it (and its surrogate) on first sight.

        ``context`` is the text being masked: a candidate surrogate that already occurs in it would
        make the restore ambiguous, so it is rejected. ``rule`` is the detector rule that found it,
        kept for the dashboard (like the type, the first sighting's wins), as it is kept in clear
        (``modules.kept``: a field's name in it, which can be a value, is named by its kind). ``generate``
        makes the fake for a type of the user's own recognizers (it follows the rule's shape and checksum).
        A candidate that holds ``original``, whatever the case, is rejected as one equal to it (``_shows``).
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
            domain_surrogate = None
            if etype == "EMAIL":
                domain = email_domain(original)
                if domain and domain.lower() not in PUBLIC_MAIL_DOMAINS:
                    dom = self.intern("DOMAIN", EntityClass.PII, domain.lower(), context, rule)
                    domain_surrogate = dom.surrogate
            whole = restored_whole(etype, eclass)
            # A value of a structure whose shape has no free fake left (hundreds of 3-digit ids in one
            # output) gets a longer one: a fake that fails the masking would stop the request.
            growth = _MAX_GROWTH if eclass == EntityClass.STRUCT else 0
            tries = ((grow, counter) for grow in range(growth + 1) for counter in range(_MAX_ATTEMPTS))
            for grow, counter in tries:
                candidate = self._generate(etype, eclass, original, counter, domain_surrogate, generate, grow)
                if (
                    _shows(candidate, original)
                    or candidate in self._by_surrogate
                    or candidate in self._by_original
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
                )
                if self._on_insert is not None:
                    try:
                        first = self._on_insert(entity)
                    except FakeTaken:
                        continue  # another process gave this fake to another value: the next counter
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
        growth = _MAX_GROWTH if entity.eclass == EntityClass.STRUCT else 0
        for grow, counter in ((grow, counter) for grow in range(growth + 1) for counter in range(_MAX_ATTEMPTS)):
            candidate = self._generate(
                entity.etype, entity.eclass, entity.original, counter, domain_surrogate, None, grow
            )
            if _shows(candidate, entity.original) or candidate in self._by_surrogate or candidate in self._by_original:
                continue
            drawn = replace(entity, surrogate=candidate, counter=counter)
            try:
                kept = keep(entity, drawn)
            except FakeTaken:
                continue  # another process gave this fake to another value: the next counter
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
        if self._by_original.get(entity.original) is entity:
            self._by_original[entity.original] = updated
        self._by_id[entity.id] = updated
        if self._on_rule is not None:
            self._on_rule(updated)
        return updated

    def _generate(
        self,
        etype: str,
        eclass: EntityClass,
        original: str,
        counter: int,
        domain_surrogate: str | None,
        generate: Callable[[str, Drbg], str] | None = None,
        grow: int = 0,
    ) -> str:
        value = normalize(etype, original)
        if len(value) > SEED_LONGEST:  # its digest: the generator hashes the seed again for every 32 bytes it draws
            value = "sha256:" + hashlib.sha256(value.encode()).hexdigest()
        seed = f"{etype}\x00{value}\x00{counter}".encode()
        if grow:  # the shape of a value of a structure, `grow` characters longer: its last one again
            return gen_data(original + original[-1] * grow, Drbg(self._k_sur, seed + f"\x00grow{grow}".encode()))
        rng = Drbg(self._k_sur, seed)
        generator = generate or GENERATORS.get(etype)
        if etype == "EMAIL":
            domain_of = (lambda _d: domain_surrogate) if domain_surrogate else None
            generator = partial(gen_email, domain_surrogate=domain_of)
        if generator is not None:
            try:
                return generator(original, rng)
            except (ValueError, AssertionError, IndexError):  # no fake of its kind (every name would show it)
                return shape(original, Drbg(self._k_sur, seed + b"\x00shape"))
        if eclass == EntityClass.STRUCT:
            return gen_data(original, rng)
        if eclass == EntityClass.SECRET:
            return shape(original, rng) if etype in _SHAPE_SECRETS else gen_secret(original, rng)
        if eclass == EntityClass.CUSTOM and original.replace(" ", "").isalpha():
            return gen_company(original, rng)
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
        self._touch((entity.etype, entity.original))
        self._keyed((entity.etype, entity.original), entity)
        self._by_surrogate[entity.surrogate] = entity
        self._by_original.setdefault(entity.original, entity)
        self._by_id[entity.id] = entity
        self._moved()

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
        if self._by_original.get(entity.original) is entity:
            del self._by_original[entity.original]
