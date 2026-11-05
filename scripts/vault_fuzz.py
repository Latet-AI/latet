"""The differential fuzz of an API key's vault (the server performance plan's S10; until 0.10.0 the scratchpad's
``keyfuzz2.py``, its configurations and seed unchanged, its values and pieces those 0.10.1's fuzz added to them too,
and the integration review's assignments, `password=` and `api_key: `, read by the assignment detector besides):
seeded texts glued from pieces that write the values in escapes, quotes and runs of letters, masked and restored twice
in each configuration, once over a plain ``Vault`` holding the values (a fresh one each text), once over an API key's
``StoredVault`` whose store holds them and whose memory holds ``--memory`` of them (0: none but those held for good;
``--glued-all``: and every one no word finds glued on both sides, as a person's space holds them, ``glued_all``), a
vault made anew over the store before the text is masked, and again before it is restored. Its values include those of
one run that no word finds glued on both sides (the server performance plan's S4), which an API key's vault holds while
they are among its recent values. Masked as a key's space masks (``prefetch`` then the masker, in one call: the
gateway's ``holding()``, a text a call), restored as it restores
(``prefetch_fakes``, then the candidates of the text, ``restores_for(parts=False, whole_vault=True)``, as
``Engine.unmask``). Both vaults hold the short secrets 0.10.0 kept with fakes of their shape too (``OLD_VALUES``,
0.10.1's fuzz). With ``--path apps`` the texts are masked in applications calls as ``Apps._mask`` masks one
(``holding(scoped=True)``, ``prefetch``, the call's lines read joined, ``Masker.joined``, then ``settle``), the plain
vault masking as an applications call masks (``Vault.whole_vault_call``): calls of one to ``CALL_MOST`` texts by the
seed (``calls_of``), the texts with a line break among the others, as a tool's output and as text people wrote in turn,
each side's vaults made anew for each call; and each call masked a third time, over the plain vault with its texts each
alone (``Masker.joins`` off, 0.10.0's way). For each configuration, the texts where the key's vault masks a position the
plain one does not mask (``less``), where the plain one restores exactly and the key's does not (``restore-lost``, plain
and as a JSON string), where a fake written as it reads is restored otherwise (``read-differs``, of ``read-texts``),
errors, and where it masks more (``more``); with memory 0, a text masked or restored less only where a value or a
fake no word finds stands glued on both sides (``let-go``, ``let_go``: an API key keeps those among its recent values
alone, as 0.10.0 did), apart, never failing the run; on the apps path,
where the call read joined masks less than its texts each alone (``joined-less``), or does not restore what they
restore (``joined-restore-lost``, plain or as a JSON string), and where it masks more (``joined-more``).

    uv run python scripts/vault_fuzz.py one OUT.json COUNT CONFIG [--pg] [--path gateway|apps] [--memory N]
        [--glued-all] [--tight] [--whole N|cross]
    uv run python scripts/vault_fuzz.py all --out DIR --count 5000 [--pg] [--index on|off] [--jobs 8] [--glued-all]
        [--tight] [--whole N|cross]

``--glued-all``: the key's vault is a person's (``glued_all``, and past a bound of a call's lookup the store's whole
vault read, ``whole_past_bounds``: Task 8's fix round 6). ``--tight``: the bounds of a call's lookup lowered
(``TIGHT``), so that most calls read past one (their windows skipped or dropped, their words read as 0.10.2 read them,
or a person's whole vault): no text is refused, and none masked or restored less than the plain vault's. ``--whole N``:
the key's vault held whole up to ``N`` values (``whole_at_most``, as a person's space holds a vault of
``stored.WHOLE_VALUES`` values or fewer: every value in memory, none asked of the store for a text), so that the fuzz's
vaults, of a few dozen values, are below the bound; ``--whole cross``, each vault's bound the values its store holds as
it is made (``CROSS``, counted then, asserted held whole): held whole as it is made, and on its store from the first
value it keeps, which is asserted too; 0, the default: on its store whatever its size, as a vault past the bound is.

``one``: one configuration, its counts printed and written to ``OUT.json`` (its cases to ``OUT-CONFIG.json``). ``all``:
the eight configurations on both paths with memory 0 and 1,000, a process each, ``--jobs`` at once (``--glued-all``:
every run's vault a person's, Task 9); a line for each run, and exit 1 when any run has a
``less``, a ``restore-lost``, a ``json-restore-lost``, a ``joined-less``, a ``joined-restore-lost`` or an error, or did
not finish.

``--pg``: the store is PostgreSQL's ``PgVaultStore`` on the test services (``LATET_TEST_DATABASE_URL``), an identity of
its own for each text (each call) in a schema of the run's own (dropped after); else ``MemoryStore``, which keeps the
vault as PostgreSQL keeps it (one entity per value, one value per fake, found by their words and edges). ``--index``:
whether the store finds the words through its index of prints (``off``: the SQL path), from the plan's Task 8 on."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from latet.bridge.apps import Apps
from latet.bridge.review import MASKED
from latet.engine.anchors import EDGE
from latet.engine.detectors.pii import PiiDetector
from latet.engine.detectors.structural import AssignmentDetector
from latet.engine.detectors.terms import IdentityDetector, LocalIdentity, Term, TermsDetector
from latet.engine.masker import AllowList, Masker
from latet.engine.pipeline import Engine
from latet.engine.rehydrate import restore_text, restores_for
from latet.engine.stored import StoredVault, edges_of, glued_both, held_glued, word_of
from latet.engine.types import EntityClass, Policy, SlotKind
from latet.engine.vault import Entity, FakeTaken, Vault, fake_reading
from latet.engine.verifier import Verifier
from latet.gateway.app import OnServer
from latet.gateway.mediate import Mediator

if TYPE_CHECKING:
    from latet.state.protocols import Role

HERE = Path(__file__).resolve().parent
ROOT_KEY = b"\x01" * 32
SEED = 9917
BS = chr(92)
O = "\N{LATIN SMALL LETTER O WITH GRAVE}"  # noqa: E741
LONG_TOKEN = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"  # forty letters and digits, one run (S4)
VALUES = [
    ("PERSON", EntityClass.NER, "Niccol" + O + " Ferri"),
    ("PERSON", EntityClass.NER, "Mario Rossi"),
    ("PERSON", EntityClass.NER, "Anna Maria"),
    ("PERSON", EntityClass.NER, "Maria Rossi"),
    ("PERSON", EntityClass.NER, "Rossi Bianchi"),
    ("PERSON", EntityClass.NER, "Maria D'Angelo"),
    ("PERSON", EntityClass.NER, "mario.rossi"),
    ("API_KEY", EntityClass.SECRET, "tok_9fQ2xL7pZ4"),
    ("EMAIL", EntityClass.PII, "mario.rossi@acme.it"),
    # Fix round 1 (review 10b's I1): known secrets of 3 to 8 characters, of one run or with a short anchor at an edge.
    ("PASSWORD", EntityClass.SECRET, "x9q"),
    ("PASSWORD", EntityClass.SECRET, "ab12"),
    ("PASSWORD", EntityClass.SECRET, "pw5Kz"),
    ("PASSWORD", EntityClass.SECRET, "hunter2"),
    ("API_KEY", EntityClass.SECRET, "Zt8kQ2pW"),
    ("API_KEY", EntityClass.SECRET, "tok_ab12"),
    # 0.10.1's fix round 1 (review I1): short secrets with a sign at an edge or inside, glued on both sides.
    ("PASSWORD", EntityClass.SECRET, "Summer2024!"),
    ("PASSWORD", EntityClass.SECRET, "abcdefg-h"),
    # The server performance plan's S4 (Task 7): secrets of one run longer than a vault holds glued for good, which no
    # word of a text finds glued on both sides (``stored.glued_both``): held while among the recent values, or always.
    ("API_KEY", EntityClass.SECRET, LONG_TOKEN),
    ("API_KEY", EntityClass.SECRET, "Kq7mZ2xW9pL4vR8tN3bY"),
]
# 0.10.1's fix round 1 (review C1, M6): short secrets a vault kept in 0.10.0, with fakes of their shape, whose values
# the pieces put inside longer words and numbers; their fakes are pieces too.
OLD_VALUES = (("PASSWORD", "postgres"), ("PASSWORD", "1234"), ("PASSWORD", "admin1"))


def _old_entities() -> list[Entity]:
    from latet.engine.surrogates.generators import Drbg, shape
    from latet.engine.vault import derive_keys

    vault = Vault(ROOT_KEY)
    key = derive_keys(ROOT_KEY)["surrogate"]
    return [
        Entity(
            vault.entity_id(etype, value), etype, EntityClass.SECRET, value,
            shape(value, Drbg(key, f"{etype}\x00{value}\x000".encode())), 0, 0.0, "assignment:password",
        )
        for etype, value in OLD_VALUES
    ]  # fmt: skip


OLD_ENTITIES = _old_entities()
# The pieces 0.10.0's texts were drawn from (``keyfuzz2.py``'s): the spans 0.10.0 found are kept for those texts
# (``scripts/spans_golden.py``, ``fuzz_texts(5000, PIECES_0_10_0)``), which 0.10.1's pieces would change.
PIECES_0_10_0 = [
    "Niccol", BS + "u00f2", O, " Ferri", "Ferri", "%20", "%40", "%C3%B2", "+", "=", "&", "?q=", "&#242;", "&ograve;",
    BS, BS * 2, BS * 3, BS * 4, "n", "u", "x", "c3", BS + "xb2", BS + "303", BS + "262", "mario.rossi", "@acme.it",
    "acme.it", "Mario", "Rossi", "Anna", "Maria", "Bianchi", " ", " ", " ", "\n", '"', "'", BS + "'", BS + '"', "D",
    "D'Angelo", "D%27Angelo", "&#39;", "tok_9fQ2xL7pZ4", "IT60X0542811101000000123456", "4539148803436467",
    "RSSMRA85T10A562S", "+393331234567", "Acme Corp", "Acme", "marc", "luca.neri%40acme.it", "ops@acme.it",
    BS + "ud83e", BS + "udd8a", "%3D", "%2B", "%2C", ".", ",", "-", "_", "&#64;", "&amp;", BS + "n", BS + "u0020",
    "x9q", "ab12", "pw5Kz", "hunter2", "Zt8kQ2pW", "tok_ab12", "q", "7", "Zz",
]  # fmt: skip
# With 0.10.1's: its short secrets, the longer words and numbers its old ones sit inside, and their old fakes.
PIECES_0_10_1 = [
    *PIECES_0_10_0,
    "Summer2024!", "abcdefg-h", "postgresql", "postgres", "9912343", "1234", "admin12", "admin1",
    *(e.surrogate for e in OLD_ENTITIES),
]  # fmt: skip
PIECES = [
    *PIECES_0_10_1,
    # The integration review: a secret the assignment detector finds (`password=0542811101 `) inside an IBAN of another
    # text, which the vault then keeps as a secret's group; and its name in front of the other values.
    "password=0542811101 ", "password=", "api_key: ", "0542811101",
    # The plan's S4: secrets of one run that no word finds glued on both sides (the pieces glue letters to them).
    LONG_TOKEN, "Kq7mZ2xW9pL4vR8tN3bY",
]  # fmt: skip
CONFIGS: dict[str, tuple[Any, ...]] = {
    "none": ({}, ()),
    "email-allow": ({"EMAIL": Policy.ALLOW}, ()),
    "person-phone-allow": ({"PERSON": Policy.ALLOW, "PHONE": Policy.ALLOW}, ()),
    "domain-allow": ({"DOMAIN": Policy.ALLOW}, ()),
    "fiscal-redact": ({"IT_FISCAL_CODE": Policy.REDACT}, ()),
    "allow-address": ({}, ("mario.rossi@acme.it",)),
    "allow-name": ({}, ("Mario Rossi",)),
    "email-allow-domain-term": ({"EMAIL": Policy.ALLOW}, (), (Term("acme.it", "DOMAIN"),)),
}
PATHS = ("gateway", "apps")
MEMORIES = (0, 1000)
CALL_MOST = 16  # the texts of an applications call on the apps path: one to this many, by the seed
COUNTERS = (
    *("texts", "calls", "less", "more", "restore-lost", "json-restore-lost", "let-go", "read-differs", "read-texts"),
    *("joined-less", "joined-more", "joined-restore-lost", "errors"),
)
# Any of them above 0: a run that is not clean.
FAILING = ("less", "restore-lost", "json-restore-lost", "joined-less", "joined-restore-lost", "errors")
_SCHEMA = "latet_t_keyfuzz_"


def fuzz_texts(count: int, pieces: Sequence[str] = PIECES) -> list[str]:
    """The fuzz's ``count`` texts: from 1 to 15 of ``pieces`` each, drawn by the seed 9917 (the first texts are the
    same whatever the count; ``PIECES_0_10_0``: 0.10.0's texts)."""
    rng = random.Random(SEED)
    return ["".join(rng.choice(pieces) for _ in range(rng.randrange(1, 16))) for _ in range(count)]


def calls_of(texts: Sequence[str]) -> list[list[str]]:
    """``texts`` in applications calls, in order: one to ``CALL_MOST`` texts each, by a seed of their own (the same
    calls for the same texts)."""
    rng = random.Random(SEED + 1)
    calls: list[list[str]] = []
    at = 0
    while at < len(texts):
        size = rng.randrange(1, CALL_MOST + 1)
        calls.append(list(texts[at : at + size]))
        at += size
    return calls


class MemoryStore:
    """A key's vault as PostgreSQL keeps it (``PgVaultStore``), in memory: one entity per value, one value per fake (a
    fake that is a value or another fake refused), the values and fakes found by their words as the store finds them
    (``stored.word_of``, and by an edge for a word of ``EDGE`` letters, ``stored.edges_of``), the values of a string
    without a word loaded for good, and the newest secrets held glued (``stored.held_glued``) loaded apart."""

    def __init__(self) -> None:
        self.rows: dict[str, Entity] = {}
        self.unreadable = 0
        self.count = 0
        self._lock = threading.RLock()

    def _all(self) -> list[Entity]:
        with self._lock:
            return list(self.rows.values())

    def load_entities(self) -> list[Entity]:
        """Every value, the oldest first: a person's whole vault, read past a bound of a call (Task 8's fix round 6)."""
        return self._all()

    @staticmethod
    def _found_by(e: Entity, role: Role, words: set[str], edged: set[str]) -> bool:
        """Whether ``words`` (``edged``: those of ``EDGE`` letters) find ``e``'s value or fake, as ``with_words``."""
        if word_of(e, role) in words:
            return True
        return any(edge is not None and edge in edged for edge in edges_of(e, role))

    def save_entity(self, e: Entity) -> Entity | None:
        with self._lock:
            for kept in self.rows.values():
                if kept.id == e.id or kept.original == e.original:
                    return kept
            known = {s for kept in self.rows.values() for s in (kept.original, kept.surrogate, fake_reading(kept))}
            if e.surrogate in known or (fake_reading(e) or e.surrogate) in known:
                raise FakeTaken("taken")
            self.rows[e.id] = e
            self.count += 1
            return None

    def save_rule(self, e: Entity) -> None:
        with self._lock:
            if e.id in self.rows and self.rows[e.id].rule is None:
                self.rows[e.id] = e

    def load_unanchored(self) -> list[Entity]:
        return [e for e in self._all() if word_of(e, "original") is None or word_of(e, "surrogate") is None]

    def load_held(self, n: int) -> list[Entity]:
        return [e for e in self._all()[::-1] if held_glued(e)][:n]

    def load_glued(self) -> list[Entity]:
        """The entries with a head and a tail of one role (``stored.glued_both``), the oldest first."""
        return [e for e in self._all() if glued_both(e, "original") or glued_both(e, "surrogate")]

    def current(self) -> bool:
        """Values kept elsewhere taken in since the last call: none (one store, every lookup reads it)."""
        return False

    def load_recent(self, n: int) -> list[Entity]:
        return self._all()[::-1][:n]

    def count_entities(self) -> int:
        """How many values it holds: whether a vault is held whole (``--whole``) is decided by it first."""
        return len(self._all())

    def by_original(self, value: str) -> Entity | None:
        return next((e for e in self._all() if e.original == value), None)

    def by_surrogate(self, fake: str) -> Entity | None:
        return next((e for e in self._all() if e.surrogate == fake), None)

    def by_ids(self, ids: Iterable[str]) -> list[Entity]:
        with self._lock:
            return [self.rows[i] for i in dict.fromkeys(ids) if i in self.rows]

    def by_reading(self, reading: str) -> Entity | None:
        return next((e for e in self._all() if fake_reading(e) == reading), None)

    def with_words(self, role: Role | tuple[Role, ...], words: Iterable[str]) -> list[Entity]:
        asked = set(words)
        edged = {word for word in asked if len(word) == EDGE}
        roles: tuple[Role, ...] = (role,) if isinstance(role, str) else role
        return [e for e in self._all() if any(self._found_by(e, r, asked, edged) for r in roles)]


class PgStores:
    """An identity of its own for each text, in a schema of the run's own on the test PostgreSQL (``schema``: the one
    ``all`` named for this run, which it drops itself should the run not end), dropped as it closes. ``index``: whether
    each store finds words through its index of prints (from the plan's Task 8 on; the SQL path before)."""

    def __init__(self, schema: str | None = None, index: bool = True) -> None:
        from latet.server.db import Database, migrate
        from latet.server.master import MasterKey

        self.schema = schema or _SCHEMA + secrets.token_hex(4)
        self.index = index
        self.db = Database(os.environ["LATET_TEST_DATABASE_URL"], schema=self.schema, max_size=2)
        try:
            migrate(self.db, master=MasterKey([b"latet synthetic vault fuzz key!!"]))
        except BaseException as error:
            try:
                self.db.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("fuzz constructor and connection cleanup failed", [error, cleanup]) from None
            raise
        self.n = 0

    def new(self) -> Any:
        from latet.server.vault import PgVaultStore

        self.n += 1
        identity = f"k_fuzz{self.n:06d}"
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO identities (id, kind, name, created) VALUES (%s, 'key', %s, 0)", (identity, identity)
            )
        store = PgVaultStore(self.db, identity, ROOT_KEY, index=self.index)
        try:
            if self.index and not store.build_index():
                raise RuntimeError("the fuzz's indexed store did not become ready")
        except BaseException as error:
            try:
                store.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("fuzz store construction and close failed", [error, cleanup]) from None
            raise
        return store

    def close(self, *, drop: bool = True) -> None:
        self.db.close()
        if drop:
            drop_schema(self.schema)


def drop_schema(schema: str) -> None:
    """A schema of this fuzz's own (named ``latet_t_keyfuzz_…``), dropped from the test PostgreSQL."""
    import psycopg
    from psycopg import sql

    if not schema.startswith(_SCHEMA):
        raise ValueError("not a schema of the fuzz")
    with psycopg.connect(os.environ["LATET_TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


# --- one text, both ways (keyfuzz2.py's run, the path and the memory given rather than read from the command line) ---


def _masker(vault: Vault, policies: dict, allowed: tuple, terms: tuple) -> Masker:
    detectors = [
        PiiDetector(),
        AssignmentDetector(),  # values assigned to sensitive names (the integration review)
        TermsDetector([Term("Acme Corp", "CLIENT"), Term("Rossi Bianchi", "CLIENT"), *terms]),
        IdentityDetector(LocalIdentity(username="marc", full_name="Marco Ferretti")),
    ]
    return Masker(vault, detectors, policies=policies, allow=AllowList(values=set(allowed)))


def _masked(vault: Vault, text: str, policies: dict, allowed: tuple, terms: tuple, apps: bool):
    masker = _masker(vault, policies, allowed, terms)
    with vault.holding(scoped=apps):  # an applications call's, or the gateway's
        vault.prefetch([text])
        done = masker.mask(text, SlotKind.TOOL_RESULT)
    positions = sorted({i for r in done.replacements if r.policy != Policy.ALLOW for i in range(r.start, r.end)})
    redacted = any(r.policy == Policy.REDACT for r in done.replacements)
    return done.text, positions, redacted


def _restored(vault: Vault, masked: str) -> tuple[str, str]:
    """``masked`` restored as a key's space restores it, plain and as a JSON string (each its own call)."""
    as_json = json.dumps(masked, ensure_ascii=False)
    out = []
    for text, json_mode in ((masked, False), (as_json, True)):
        with vault.holding():
            vault.prefetch_fakes([text])
            back, _ = restore_text(
                text, restores_for(vault.candidates_in(text), json_mode=json_mode, parts=False, whole_vault=True)
            )
        out.append(back)
    return out[0], out[1]


def _as_read(vault: Vault, masked: str) -> str:
    """``masked`` with each fake written in escapes the vault holds as it reads (`Giulia Martini`), as a model may
    write it back."""
    for entity in vault.entities():
        reading = fake_reading(entity)
        if reading is not None:
            masked = masked.replace(entity.surrogate, reading)
    return masked


def _restored_read(vault: Vault, text: str) -> str:
    with vault.holding():
        vault.prefetch_fakes([text])
        back, _ = restore_text(text, restores_for(vault.candidates_in(text), parts=False, whole_vault=True))
    return back


def _plain_vault(apps: bool) -> Vault:
    """A plain vault holding the fuzz's values, the old short secrets with their 0.10.0 fakes among them; ``apps``:
    masking as an applications call masks (``Vault.whole_vault_call``), which a plain vault never is."""
    vault = Vault(ROOT_KEY, OLD_ENTITIES)
    if apps:
        vault.whole_vault_call = lambda: True  # type: ignore[method-assign]
    for etype, eclass, value in VALUES:
        vault.intern(etype, eclass, value)
    return vault


def _old_kept(store: Any) -> None:
    """The old short secrets in ``store``, with their fakes, as 0.10.0 kept them."""
    for entity in OLD_ENTITIES:
        store.save_entity(entity)


def run_plain(text: str, policies: dict, allowed: tuple, terms: tuple = (), apps: bool = False):
    vault = _plain_vault(apps)
    masked, positions, redacted = _masked(vault, text, policies, allowed, terms, apps)
    back, back_json = _restored(vault, masked)
    read = _as_read(vault, masked)
    exact, exact_json = redacted or back == text, redacted or json.loads(back_json) == text
    return positions, exact, exact_json, masked, read, _restored_read(vault, read)


def run_key(
    store: Any,
    text: str,
    policies: dict,
    allowed: tuple,
    terms: tuple = (),
    as_read: str = "",
    apps: bool = False,
    memory: int = 0,
    glued: bool = False,
):
    _old_kept(store)
    keeper = StoredVault(ROOT_KEY, store, working=0)
    for etype, eclass, value in VALUES:
        keeper.intern(etype, eclass, value)
    # Every value in memory (1000), or none but those held for good (0); ``glued``: and every one no word finds glued
    # on both sides, whatever its age (a person's space, ``glued_all``).
    vault = _stored(store, memory, glued)
    masked, positions, redacted = _masked(vault, text, policies, allowed, terms, apps)
    _crossed(vault, store)
    later = _stored(store, memory, glued)  # the unmask, the same
    back, back_json = _restored(later, masked)
    # The plain vault's text as it reads, restored by a vault made anew, its memory the same (until Task 7, none: a
    # value glued on both sides that memory holds is restored where no word finds it).
    back_read = _restored_read(_stored(store, memory, glued), as_read)
    exact, exact_json = redacted or back == text, redacted or json.loads(back_json) == text
    return positions, exact, exact_json, masked, as_read, back_read, back, json.loads(back_json), store.load_glued()


# --- a call of many texts, as the applications API masks one (``--path apps``) ---------------------------------------


def _apps_call(
    vault: Vault, texts: Sequence[str], kind: SlotKind, policies: dict, allowed: tuple, terms: tuple, joined: bool
) -> list[tuple[str, list[int], bool]]:
    """``texts`` masked as one applications call, by ``Apps._mask`` itself (``holding(scoped=True)``, ``prefetch``,
    the call's lines read joined, ``mask_value`` as one text each, then ``settle``) over ``vault``; ``joined`` off: each
    text alone (``Masker.joins``). Each text's masked text, the positions masked, and whether a value was redacted."""
    masker = _masker(vault, policies, allowed, terms)
    masker.joins = joined
    mediator = Mediator(Engine(vault, masker), Verifier(masker))
    _, out, _, _ = Apps(mediator, OnServer("https://fuzz.invalid", "u_fuzzkey", kind="key"))._mask(list(texts), kind)
    done = []
    for entry in out:
        masked = entry[MASKED]
        positions = sorted({i for r in masked.replacements if r.policy != Policy.ALLOW for i in range(r.start, r.end)})
        done.append((masked.text, positions, any(r.policy == Policy.REDACT for r in masked.replacements)))
    return done


def run_plain_call(
    texts: Sequence[str], kind: SlotKind, policies: dict, allowed: tuple, terms: tuple = (), joined: bool = True
) -> list[tuple[Any, ...]]:
    """``run_plain`` for a call of ``texts``, one plain vault holding the values for the whole call, masking as an
    applications call masks."""
    vault = _plain_vault(apps=True)
    rows = []
    for text, (masked, positions, redacted) in zip(
        texts, _apps_call(vault, texts, kind, policies, allowed, terms, joined), strict=True
    ):
        back, back_json = _restored(vault, masked)
        read = _as_read(vault, masked)
        exact, exact_json = redacted or back == text, redacted or json.loads(back_json) == text
        rows.append((positions, exact, exact_json, masked, read, _restored_read(vault, read)))
    return rows


def run_key_call(
    store: Any,
    texts: Sequence[str],
    kind: SlotKind,
    policies: dict,
    allowed: tuple,
    terms: tuple = (),
    as_read: Sequence[str] = (),
    memory: int = 0,
    glued: bool = False,
) -> list[tuple[Any, ...]]:
    """``run_key`` for a call of ``texts``, over one store for the whole call."""
    _old_kept(store)
    keeper = StoredVault(ROOT_KEY, store, working=0)
    for etype, eclass, value in VALUES:
        keeper.intern(etype, eclass, value)
    vault = _stored(store, memory, glued)
    masked_call = _apps_call(vault, texts, kind, policies, allowed, terms, True)
    _crossed(vault, store)
    later = _stored(store, memory, glued)  # the unmask, the same
    rows = []
    glued_values = store.load_glued()
    for text, (masked, positions, redacted), read in zip(texts, masked_call, as_read, strict=True):
        back, back_json = _restored(later, masked)
        # The plain vault's text as it reads, restored by a vault made anew, its memory the same.
        back_read = _restored_read(_stored(store, memory, glued), read)
        exact, exact_json = redacted or back == text, redacted or json.loads(back_json) == text
        rows.append((positions, exact, exact_json, masked, read, back_read, back, json.loads(back_json), glued_values))
    return rows


def _short(plain: tuple, key: tuple) -> tuple[list[int], bool, bool]:
    """Where the key's vault masked ``plain``'s text less than the plain vault, and whether it restored it less (plain,
    and as a JSON string)."""
    return sorted(set(plain[0]) - set(key[0])), plain[1] and not key[1], plain[2] and not key[2]


_RUN_CHARACTER = re.compile("[A-Za-z0-9]")  # what a run of a text's words is made of (``anchors.WORD``)


def _glued_at(text: str, string: str) -> list[tuple[int, int]]:
    """Where ``string`` stands in ``text`` with a letter or a digit glued on both sides, inside a run of the text's:
    no word of the text finds it there (``stored.glued_both``)."""
    # Equal anchors at both ends can satisfy glued_both even across a separator, for values and fakes alike.
    if not string.isascii() or not string.isalnum():
        return []
    found = []
    at = text.find(string)
    while at >= 0:
        end = at + len(string)
        if at > 0 and end < len(text) and _RUN_CHARACTER.match(text[at - 1]) and _RUN_CHARACTER.match(text[end]):
            found.append((at, end))
        at = text.find(string, at + 1)
    return found


def _within_glued_words(text: str, runs: Iterable[tuple[int, int]], places: list[tuple[int, int]]) -> bool:
    """Each loss overlaps a glued occurrence and stays within its maximal ASCII letter-and-digit run."""
    bounds = []
    for start, end in places:
        left, right = start, end
        while left > 0 and _RUN_CHARACTER.match(text[left - 1]):
            left -= 1
        while right < len(text) and _RUN_CHARACTER.match(text[right]):
            right += 1
        bounds.append((start, end, left, right))
    return all(
        any(
            left <= begin and end_run <= right and start < end_run and begin < end for start, end, left, right in bounds
        )
        for begin, end_run in runs
    )


# The glued fakes a lost restore may have left in a text, at most, whose subsets ``_restored_but`` tries.
_LEFT_AT_MOST = 12


def _restored_but(text: str, restored: str, fakes: list[tuple[str, str]]) -> bool:
    """Whether ``restored`` is ``text`` but for some fakes no word finds glued on both sides left in it, glued so:
    giving some of those occurrences their values (each fake's, ``fakes``) makes it ``text``. The occurrences a text
    holds of a fake of its own (the fuzz's pieces hold old fakes) are tried left as they are too."""
    left = sorted(
        {(start, end, value) for fake, value in fakes for start, end in _glued_at(restored, fake)}, reverse=True
    )
    if not left or len(left) > _LEFT_AT_MOST:
        return False
    for chosen in range(1, 1 << len(left)):
        made, last = restored, len(restored) + 1
        for k, (start, end, value) in enumerate(left):  # from the right: the places before stay where they are
            if chosen >> k & 1:
                if end > last:  # overlapping occurrences: not both
                    break
                made, last = made[:start] + value + made[end:], start
        else:
            if made == text:
                return True
    return False


def let_go(text: str, less: list[int], lost: bool, lost_json: bool, key: tuple) -> bool:
    """Whether what a key's vault whose memory holds none (``--memory 0``) masked or restored less than the plain
    vault is all a value or a fake no word finds glued on both sides (``stored.glued_both``, ``key[8]``: its store's,
    ``load_glued``), excluding the secrets held even without working memory (``held_glued``). An API key keeps the
    others among its recent values alone (the plan's S4), as 0.10.0 did: each run of positions masked less overlaps
    such a glued occurrence and stays within the maximal ASCII letter-and-digit run around it (the group the value
    names, masked whole). A restore that lost something (``key[6]``, as a JSON string
    ``key[7]``) is the text but for such fakes left in it, glued so (``_restored_but``)."""
    glued = [e for e in key[8] if not held_glued(e)]
    values = [e.original for e in glued if glued_both(e, "original")]
    places = [place for value in values for place in _glued_at(text, value)]
    runs: list[list[int]] = []
    for i in less:
        if runs and runs[-1][1] == i:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])
    if not _within_glued_words(text, [(begin, end) for begin, end in runs], places):
        return False
    fakes = [(e.surrogate, e.original) for e in glued if glued_both(e, "surrogate")]
    for restored, was_lost in ((key[6], lost), (key[7], lost_json)):
        if was_lost and not _restored_but(text, restored, fakes):
            return False
    return bool(less or lost or lost_json)


def _tally(
    counts: dict[str, int],
    cases: list[dict],
    text: str,
    plain: tuple,
    key: tuple,
    memory_none: bool = False,
) -> None:
    """What the key's vault did with ``text`` against the plain vault, counted, and a case where they differ.
    ``memory_none``: a vault whose memory holds none of the values (``--memory 0``, an API key's that let every one
    go): what it masks or restores less only for a value glued on both sides that no word finds (``let_go``) is
    counted apart (``let-go``), never as less, and its case kept; anything else it loses fails the run."""
    less, lost, lost_json = _short(plain, key)
    if memory_none and let_go(text, less, lost, lost_json, key):
        counts["let-go"] += 1
        cases.append({"text": text, "let-go": True, "less": less, "lost": lost, "lost_json": lost_json})
        less, lost, lost_json = [], False, False
    more = sorted(set(key[0]) - set(plain[0]))
    counts["less"] += bool(less)
    counts["more"] += bool(more)
    counts["restore-lost"] += lost
    counts["json-restore-lost"] += lost_json
    # The fakes as they read, where both masked the text alike: restored as the plain vault restores them.
    read_differs = plain[3] == key[3] and plain[4] != plain[3] and plain[5] != key[5]
    counts["read-texts"] += plain[3] == key[3] and plain[4] != plain[3]
    counts["read-differs"] += read_differs
    if read_differs:
        cases.append({"text": text, "read": plain[4], "plain_back": plain[5], "key_back": key[5]})
    if less or lost or lost_json:
        cases.append({"text": text, "less": less, "lost": lost, "lost_json": lost_json, "key_masked": key[3]})


def _tally_joined(counts: dict[str, int], cases: list[dict], text: str, joined: tuple, alone: tuple) -> None:
    """What the call read joined did with ``text`` against its texts each alone (the plain vault both times)."""
    less = sorted(set(alone[0]) - set(joined[0]))
    more = sorted(set(joined[0]) - set(alone[0]))
    lost = (alone[1] and not joined[1]) or (alone[2] and not joined[2])
    counts["joined-less"] += bool(less)
    counts["joined-more"] += bool(more)
    counts["joined-restore-lost"] += lost
    if less or more or lost:
        cases.append({"text": text, "joined-less": less, "joined-more": more, "joined-lost": lost, "joined": joined[3]})


def run_config(
    name: str,
    texts: Sequence[str],
    *,
    path: str = "gateway",
    memory: int = 0,
    new_store: Callable[[], Any] = MemoryStore,
    glued: bool = False,
) -> tuple[dict[str, int], list[dict]]:
    """Every text of ``texts`` masked and restored both ways in the configuration ``name`` (on the apps path, in calls,
    ``calls_of``, and a third way: the plain vault's with each text alone): the counts, and the cases that are not alike
    (those of the plain vault's own errors too, which are not counted against the key's)."""
    policies, allowed, *terms = CONFIGS[name]
    counts = dict.fromkeys(COUNTERS, 0)
    cases: list[dict] = []
    if path == "apps":
        for number, call in enumerate(calls_of(texts)):
            _apps_config(
                call, KINDS[number % len(KINDS)], policies, allowed, terms, memory, new_store, counts, cases, glued
            )
        return counts, cases
    for text in texts:
        counts["texts"] += 1
        try:
            plain = run_plain(text, policies, allowed, *terms)
        except Exception as exc:  # the plain vault's own: not counted against the key's
            cases.append({"text": text, "plain-error": f"{type(exc).__name__}: {str(exc)[:80]}"})
            continue
        store = new_store()
        try:
            key = run_key(
                store, text, policies, allowed, *(terms or [()]), as_read=plain[4], memory=memory, glued=glued
            )
        except Exception as exc:  # a probe: the error is part of the result
            counts["errors"] += 1
            cases.append({"text": text, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
            continue
        _tally(counts, cases, text, plain, key, memory == 0 and not glued)
    return counts, cases


KINDS = (SlotKind.TOOL_RESULT, SlotKind.USER_TEXT)  # a call's texts on the apps path: data, then prose, in turn


def _apps_config(
    call: list[str],
    kind: SlotKind,
    policies: dict,
    allowed: tuple,
    terms: list,
    memory: int,
    new_store: Callable[[], Any],
    counts: dict[str, int],
    cases: list[dict],
    glued: bool = False,
) -> None:
    """One call of the apps path: masked by the plain vault read joined and with each text alone, and by the key's
    vault read joined; counted text by text."""
    counts["texts"] += len(call)
    counts["calls"] += 1
    try:
        alone = run_plain_call(call, kind, policies, allowed, *(terms or [()]), joined=False)
    except Exception as exc:  # the plain vault's own, each text alone (0.10.0's way): not counted against the call
        cases.append({"texts": call, "plain-error": f"{type(exc).__name__}: {str(exc)[:80]}"})
        return
    try:
        plain = run_plain_call(call, kind, policies, allowed, *(terms or [()]))
    except Exception as exc:  # the call read joined: the code under test, its failure counted
        counts["errors"] += len(call)
        cases.append({"texts": call, "joined-error": f"{type(exc).__name__}: {str(exc)[:120]}"})
        return
    store = new_store()
    try:
        key = run_key_call(
            store,
            call,
            kind,
            policies,
            allowed,
            *(terms or [()]),
            as_read=[p[4] for p in plain],
            memory=memory,
            glued=glued,
        )
    except Exception as exc:  # a probe: the error is part of the result
        counts["errors"] += len(call)
        cases.append({"texts": call, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
        return
    for text, joined_plain, alone_plain, keyed in zip(call, plain, alone, key, strict=True):
        _tally(counts, cases, text, joined_plain, keyed, memory == 0 and not glued)
        _tally_joined(counts, cases, text, joined_plain, alone_plain)


# The values the key's vault is held whole up to (``--whole``, ``StoredVault.whole_at_most``): 0, on its store;
# ``CROSS``, the values its store holds as it is made, so that it is held whole until it keeps one.
CROSS = -1
WHOLE_AT_MOST = 0


def _stored(store: Any, memory: int, glued: bool) -> StoredVault:
    """The key's vault over ``store``: ``memory`` values in memory; with ``glued`` a person's (``glued_all``, and the
    store's whole vault read past a bound of a call, ``whole_past_bounds``); held whole up to ``WHOLE_AT_MOST``
    values (``CROSS``: its store's count as it is made, and held whole then, asserted)."""
    whole = store.count_entities() if WHOLE_AT_MOST == CROSS else WHOLE_AT_MOST
    vault = StoredVault(ROOT_KEY, store, working=memory, glued_all=glued, whole_past_bounds=glued, whole_at_most=whole)
    if WHOLE_AT_MOST == CROSS and not vault.holds_all:
        raise AssertionError("a vault made at its store's count is not held whole")
    return vault


def _crossed(vault: StoredVault, store: Any) -> None:
    """With ``CROSS``: a vault whose store grew past its count as it was made (a value it kept) is held whole no
    longer, asserted."""
    if WHOLE_AT_MOST == CROSS and store.count_entities() > vault.whole_at_most and vault.holds_all:
        raise AssertionError("a vault past its bound is still held whole")


def clean(counts: dict[str, int]) -> bool:
    return not any(counts.get(name, 0) for name in FAILING)


# --- the commands ---------------------------------------------------------------------------------------------------


# The bounds of a call's lookup with ``--tight`` (``engine.lookup``'s): low enough that most fuzz calls read past one.
TIGHT = {
    "INTERIOR_WINDOWS": 60,
    "LOOKUP_WORD_ROLES": 120,
    "LOOKUP_CANDIDATES": 12,
    "LOOKUP_CHARACTERS": 1_500,
    "LOOKUP_BYTES": 3_000,
}


def one(args: argparse.Namespace) -> int:
    global WHOLE_AT_MOST
    WHOLE_AT_MOST = args.whole
    if args.tight:
        from latet.engine import lookup

        for name, bound in TIGHT.items():
            setattr(lookup, name, bound)
    texts = fuzz_texts(args.count)
    if args.texts is not None:  # the texts of a file (a JSON list) instead
        texts = json.loads(Path(args.texts).read_text("utf-8"))
    stores = PgStores(args.schema, args.index == "on") if args.pg else None
    try:
        counts, cases = run_config(
            args.config,
            texts,
            path=args.path,
            memory=args.memory,
            new_store=stores.new if stores is not None else MemoryStore,
            glued=args.glued_all,
        )
    finally:
        if stores is not None:
            stores.close()
    print(args.config, counts, flush=True)
    out = Path(args.out)
    _cases_file(out, args.config).write_text(json.dumps(cases[:500], ensure_ascii=False, indent=1), "utf-8")
    out.write_text(json.dumps({"summary": {args.config: counts}}, ensure_ascii=False, indent=1), "utf-8")
    return 0 if clean(counts) else 1


@dataclass
class _Run:
    """One run of ``all``: its path, memory and configuration, its process, and what it found."""

    path: str
    memory: int
    config: str
    out: Path
    schema: str = ""
    process: subprocess.Popen | None = None
    started: float = 0.0
    seconds: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    failed: str = ""

    @property
    def name(self) -> str:
        return f"{self.path}-m{self.memory}-{self.config}"

    def line(self) -> str:
        head = f"{self.path:<7} memory {self.memory:>4}  {self.config:<24}"
        if self.failed:
            return f"{head} FAILED: {self.failed} ({self.seconds:.0f} s)"
        said = ", ".join(f"{name} {self.counts.get(name, 0)}" for name in COUNTERS)
        return f"{head} {said} ({self.seconds:.0f} s){'' if clean(self.counts) else '  NOT CLEAN'}"


def _cases_file(out: Path, config: str) -> Path:
    """Where ``one`` writes the cases of ``config`` beside its counts in ``out``."""
    return out.with_name(f"{out.stem}-{config}{out.suffix}")


def _start(run: _Run, args: argparse.Namespace) -> None:
    # What an earlier run into the same folder left goes first: a run that ends without writing its own (an error
    # before its counts exits 1, as a run that is not clean does) has no summary to be read as clean.
    run.out.unlink(missing_ok=True)
    _cases_file(run.out, run.config).unlink(missing_ok=True)
    command = [sys.executable, str(HERE / "vault_fuzz.py"), "one", str(run.out), str(args.count), run.config]
    command += ["--path", run.path, "--memory", str(run.memory), "--index", args.index]
    command += ["--glued-all"] if args.glued_all else []
    command += ["--tight"] if args.tight else []
    command += ["--whole", "cross" if args.whole == CROSS else str(args.whole)]
    if args.pg:
        run.schema = _SCHEMA + secrets.token_hex(4)
        command += ["--pg", "--schema", run.schema]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    folder = run.out.parent
    with open(folder / f"{run.name}.log", "wb") as log:
        run.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
    run.started = time.monotonic()


def _finish(run: _Run, killed: str = "") -> None:
    process = run.process
    assert process is not None
    run.seconds = time.monotonic() - run.started
    if killed:
        process.kill()
        process.wait()
        run.failed = killed
    elif process.returncode not in (0, 1):
        run.failed = f"exit {process.returncode} (see {run.name}.log)"
    if not run.failed:
        try:
            run.counts = json.loads(run.out.read_text("utf-8"))["summary"][run.config]
        except (OSError, ValueError, KeyError):
            run.failed = f"no summary (see {run.name}.log)"
    if run.schema and (killed or run.failed):  # a run that did not end drops nothing itself
        with contextlib.suppress(Exception):
            drop_schema(run.schema)


def all_runs(args: argparse.Namespace) -> int:
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    runs = [
        _Run(path, memory, config, folder / f"{path}-m{memory}-{config}.json")
        for path in PATHS
        for memory in MEMORIES
        for config in CONFIGS
    ]
    print(
        f"{len(runs)} runs of {args.count} texts, {args.jobs} at once; store "
        f"{'PostgreSQL, index ' + args.index if args.pg else 'in memory'}"
        f"{'; a person' if args.glued_all else ''}{'; tight bounds' if args.tight else ''}"
        f"{'; held whole as made, until it keeps a value' if args.whole == CROSS else ''}"
        f"{f'; held whole up to {args.whole}' if args.whole > 0 else ''}; out {folder}",
        flush=True,
    )
    waiting, running, done = list(runs), [], []
    began = time.monotonic()
    try:
        while waiting or running:
            while waiting and len(running) < args.jobs:
                run = waiting.pop(0)
                _start(run, args)
                running.append(run)
            time.sleep(0.5)
            for run in list(running):
                assert run.process is not None
                over = time.monotonic() - run.started > args.timeout
                if run.process.poll() is None and not over:
                    continue
                _finish(run, f"past {args.timeout:.0f} s, stopped" if run.process.poll() is None else "")
                running.remove(run)
                done.append(run)
                print(run.line(), flush=True)
    finally:
        for run in running:  # interrupted: nothing left behind
            _finish(run, "interrupted")
    failed = [run for run in done if run.failed or not clean(run.counts)]
    summary = {run.name: {"counts": run.counts, "failed": run.failed, "seconds": round(run.seconds)} for run in done}
    (folder / "summary.json").write_text(json.dumps(summary, indent=1), "utf-8")
    took = time.monotonic() - began
    if failed:
        print(f"NOT CLEAN: {len(failed)} of {len(runs)} runs ({', '.join(r.name for r in failed)}); {took:.0f} s")
        return 1
    print(f"clean: {len(runs)} runs, less 0, restore-lost 0, json-restore-lost 0, errors 0; {took:.0f} s")
    return 0


def _whole(given: str) -> int:
    """``--whole``'s value: a count of values, or ``cross`` (``CROSS``)."""
    if given == "cross":
        return CROSS
    count = int(given)
    if count < 0:
        raise argparse.ArgumentTypeError("a count of values, 0 or more, or cross")
    return count


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    commands = found.add_subparsers(dest="command", required=True)
    single = commands.add_parser("one", help="one configuration, in this process")
    single.add_argument("out", help="where its counts go (a .json file); its cases go beside it")
    single.add_argument("count", type=int, help="how many fuzz texts")
    single.add_argument("config", choices=list(CONFIGS))
    single.add_argument("--path", choices=PATHS, default="gateway")
    single.add_argument("--memory", type=int, default=0, help="the key's vault's working set (0: none)")
    single.add_argument(
        "--glued-all",
        action="store_true",
        help="the key's vault keeps every value glued on both sides whatever its age (a person's space)",
    )
    single.add_argument("--texts", help="a JSON list of texts, in place of the fuzz's")
    single.add_argument("--schema", help=argparse.SUPPRESS)  # named by `all`, which drops it should the run not end
    every = commands.add_parser("all", help="every configuration, path and memory, a process each")
    every.add_argument("--out", required=True, help="a folder for each run's counts, cases and output")
    every.add_argument("--count", type=int, default=5000)
    every.add_argument("--jobs", type=int, default=8, help="runs at once")
    every.add_argument("--timeout", type=float, default=4 * 3600, help="seconds a run may take")
    every.add_argument("--glued-all", action="store_true", help="every run's vault a person's (see ``one``)")
    for command in (single, every):
        command.add_argument("--pg", action="store_true", help="the store on the test PostgreSQL")
        command.add_argument("--index", choices=("on", "off"), default="on", help="the store's index (Task 8)")
        command.add_argument("--tight", action="store_true", help="the bounds of a call's lookup lowered (TIGHT)")
        command.add_argument(
            "--whole",
            type=_whole,
            default=0,
            help="the key's vault held whole up to this many values (0: on its store; cross: its count as made)",
        )
    return found


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.pg and not os.environ.get("LATET_TEST_DATABASE_URL"):
        raise SystemExit("--pg: LATET_TEST_DATABASE_URL is not set (`uv run python scripts/server-services.py env`)")
    return one(args) if args.command == "one" else all_runs(args)


if __name__ == "__main__":
    sys.exit(main())
