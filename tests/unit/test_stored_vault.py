"""An API key's vault on a Latet server (``engine.stored.StoredVault``): its newest values in memory, every other one in
the store, found there by the words of the texts it masks and unmasks. A value found once is masked for ever, whatever
its age (R7); a fake of any age is restored; nothing waits on the store on the event loop; and a store that does not
answer masks nothing."""

import asyncio
import contextlib
import itertools
import json
import threading
import time
import unicodedata
from collections.abc import AsyncIterator, Callable
from urllib.parse import quote, unquote, unquote_plus

import httpx
import pytest

from latet.config import Config
from latet.engine import stored
from latet.engine.anchors import anchor
from latet.engine.build import build, build_masker
from latet.engine.known import ValueMatchers
from latet.engine.pipeline import Engine, Session
from latet.engine.stored import EVICT_SHARE, WORKING_VALUES, StoredVault
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, FakeTaken, MaskingError, Vault, fake_reading
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream
from latet.server.settings import KEY_WORKING_VALUES, ServerSettings

from ..conftest import ROOT_KEY

NAME = "Ferruccio Lanzavecchia"  # a person the name model found once: its anchor is lanzavecchia
ACCENTED = "Niccol\N{LATIN SMALL LETTER O WITH GRAVE} Ferri"  # composed (NFC), as a keyboard writes it


class _Together:
    """What one thread keeps while it keeps its values together (``_Store.together``, as ``PgVaultStore.together``
    keeps them in one transaction): in the store once it commits, nowhere when it rolls back or its commit fails."""

    def __init__(self, store: "_Store") -> None:
        self.store = store
        self.rows: dict[str, Entity] = {}
        # The instance had no slot free for it (``PgVaultStore``'s ``Database.together``): each value kept at once.
        self.at_once = store.no_slot

    def commit(self) -> None:
        store = self.store
        with store._lock:
            store._together.pop(threading.get_ident(), None)
            if store.refuse_commit:
                raise ConnectionError("the commit did not reach the store")
            store.rows.update(self.rows)
            store.count += len(self.rows)
            store.commits += 1

    def rollback(self) -> None:
        with self.store._lock:
            self.store._together.pop(threading.get_ident(), None)


class _Store:
    """A key's vault kept in a dict, as PostgreSQL keeps it (``server.vault.PgVaultStore``): one entity per value, one
    value per fake (a fake that is a value or another fake, or reads as one, is refused), the values and fakes found
    by their anchors, safe across threads. It notes each lookup it is asked, and does not answer while ``down``. A
    thread's values kept together (``together``) are its own until they commit, as a transaction's are: its own lookups
    find them, no other thread's."""

    def __init__(self) -> None:
        self.rows: dict[str, Entity] = {}  # by id, the oldest first
        self.calls: list[str] = []
        self.down = False
        self.refuse_commit = False  # a commit of values kept together fails
        self.no_slot = False  # a call that asks to keep its values together keeps each at once
        self.commits = 0  # the values kept together, committed
        self.unreadable = 0
        self.count = 0
        self._lock = threading.RLock()
        self._together: dict[int, _Together] = {}

    def _asked(self, name: str) -> None:
        self.calls.append(name)
        if self.down:
            raise ConnectionError("the store does not answer")

    def _all(self) -> list[Entity]:
        with self._lock:
            together = self._together.get(threading.get_ident())
            return [*self.rows.values(), *(together.rows.values() if together is not None else ())]

    def together(self) -> _Together:
        with self._lock:
            together = _Together(self)
            if not together.at_once:
                self._together[threading.get_ident()] = together
            return together

    # --- VaultStore --------------------------------------------------------------------------------

    def load_entities(self) -> list[Entity]:
        return self._all()

    def load_entity(self, entity_id: str) -> Entity | None:
        self._asked("load_entity")
        return self.rows.get(entity_id)

    def save_entity(self, e: Entity) -> Entity | None:
        with self._lock:
            rows = self._all()
            for kept in rows:
                if kept.id == e.id or kept.original == e.original:
                    return kept
            known = {s for kept in rows for s in (kept.original, kept.surrogate, fake_reading(kept))}
            if e.surrogate in known or (fake_reading(e) or e.surrogate) in known:  # as written, and as it reads
                raise FakeTaken("taken")
            together = self._together.get(threading.get_ident())
            if together is not None:
                together.rows[e.id] = e
                return None
            self.rows[e.id] = e
            self.count += 1
            return None

    def save_rule(self, e: Entity) -> None:
        """At once, whether the thread keeps its values together or not, as ``PgVaultStore.save_rule`` keeps it (review
        10b's M7): an entry another connection sees, a value its call has not committed yet left as it is."""
        with self._lock:
            if e.id in self.rows and self.rows[e.id].rule is None:
                self.rows[e.id] = e

    def replace_fake(self, old: Entity, new: Entity) -> Entity | None:
        with self._lock:
            self.rows[old.id] = new
        return None

    def forget_entity(self, entity_id: str) -> None:
        with self._lock:
            self.rows.pop(entity_id, None)

    def load_review(self, key: bytes) -> None:
        return None

    def save_review(self, key: bytes, answer: dict) -> None:
        pass

    def forget_reviews(self) -> None:
        pass

    def load_image_decision(self, key: str) -> None:
        return None

    def save_image_decision(self, key: str, decision: dict) -> None:
        pass

    def close(self) -> None:
        pass

    # --- KeyVaultStore -----------------------------------------------------------------------------

    def count_entities(self) -> int:
        return len(self.rows)

    def load_recent(self, n: int) -> list[Entity]:
        return self._all()[::-1][:n]

    def load_unanchored(self) -> list[Entity]:
        return [e for e in self._all() if stored.value_word(e) is None or stored.fake_word(e) is None]

    def load_held(self, n: int) -> list[Entity]:
        """The ``n`` newest values held glued (``stored.held_glued``), the newest first."""
        return [e for e in self._all() if stored.held_glued(e)][::-1][:n]

    def by_original(self, value: str) -> Entity | None:
        self._asked("by_original")
        return next((e for e in self._all() if e.original == value), None)

    def by_surrogate(self, fake: str) -> Entity | None:
        self._asked("by_surrogate")
        return next((e for e in self._all() if e.surrogate == fake), None)

    def by_ids(self, ids) -> list[Entity]:
        self._asked("by_ids")
        with self._lock:
            rows = {e.id: e for e in self._all()}
            return [rows[i] for i in dict.fromkeys(ids) if i in rows]

    def by_reading(self, reading: str) -> Entity | None:
        self._asked("by_reading")
        return next((e for e in self._all() if fake_reading(e) == reading), None)

    def with_words(self, role, words) -> list[Entity]:
        roles = (role,) if isinstance(role, str) else tuple(role)
        self._asked(f"with_words:{'+'.join(roles)}")
        asked = set(words)
        return [e for e in self._all() if any(asked.intersection(stored.words_of(e, r)) for r in roles)]

    def backfill_words(self) -> int:
        return 0


def _email(n: int) -> str:
    return f"customer.number{n:05d}@gmail.com"  # a mail provider's: no domain of its own to keep


def _vault(store: _Store, working: int = 100) -> StoredVault:
    return StoredVault(ROOT_KEY, store, working=working, on_insert=store.save_entity, on_rule=store.save_rule)


def _engine(vault: StoredVault) -> Engine:
    masker, _ = build_masker(Config(profile="balanced", ner="off"), vault)
    return Engine(vault, masker)


def _many(vault: StoredVault, count: int = 500, start: int = 0) -> None:
    """``count`` other values kept after the ones before, so that those leave the vault's memory."""
    for n in range(start, start + count):
        vault.intern("EMAIL", EntityClass.PII, _email(n))


def _held(vault: StoredVault, entity: Entity) -> bool:
    return entity.id in {e.id for e in vault.entities()}


def test_the_defaults():
    assert WORKING_VALUES == 5_000 and EVICT_SHARE == 0.1  # Task 10's measurements (Ruling T10b)
    # The server's setting, named in its settings as well so that they are read without the engine.
    assert ServerSettings.model_fields["key_working_values"].default == WORKING_VALUES == KEY_WORKING_VALUES


def test_holds_at_most_its_working_set():
    store = _Store()
    vault = _vault(store)
    _many(vault)
    assert vault.count <= 100 and len(vault.entities()) == vault.count
    assert vault.chars == sum(max(len(e.original), len(e.surrogate)) for e in vault.entities())
    assert len(store.rows) == 500  # every value kept in the store


def test_a_value_out_of_memory_keeps_its_fake():
    store = _Store()
    vault = _vault(store)
    first = vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@gmail.com")
    _many(vault)
    assert not _held(vault, first)
    again = vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@gmail.com")
    assert again.surrogate == first.surrogate and again.id == first.id
    assert [e.original for e in store.rows.values()].count("laura.bianchi@gmail.com") == 1


def test_a_name_found_long_ago_is_still_masked():
    """R7: a name the name model found once, out of memory since, in a tool's output, where the name model does not
    read: the known values the store keeps find it, with its first fake."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    person = vault.intern("PERSON", EntityClass.NER, NAME, rule="ner")
    _many(vault)
    assert not _held(vault, person)
    done = engine.mask_text(f"ticket 4411 closed by {NAME} at 10:02", SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert done.text == f"ticket 4411 closed by {person.surrogate} at 10:02"
    assert [r.entity_id for r in done.replacements] == [person.id]


def test_one_store_call_for_a_whole_batch():
    store = _Store()
    vault = _vault(store)
    _many(vault)
    store.calls.clear()
    vault.prefetch([f"line {n}: {_email(n)} logged in" for n in range(1000)])
    assert store.calls == ["with_words:original"]


def test_a_value_prefetched_is_held_while_its_call_lasts():
    """A call's values stay in memory until it is over, however many values it keeps meanwhile; then the working set
    is cut back."""
    store = _Store()
    vault = _vault(store)
    old = vault.intern("PERSON", EntityClass.NER, NAME, rule="ner")
    _many(vault)
    with vault.holding():
        vault.prefetch([f"seen: {NAME}"])
        assert _held(vault, old)
        _many(vault, start=1000)
        assert _held(vault, old)  # found by the call: not let go before it is over
    assert vault.count <= 100


def test_unmask_finds_old_fakes_in_every_form():
    """Review Focus 3: a fake out of memory, upper case, inside a URL, twice, next to a full stop; a person's whole fake."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    email = vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@nordest.it")
    person = vault.intern("PERSON", EntityClass.NER, NAME, rule="ner")
    _many(vault)
    assert not _held(vault, email) and not _held(vault, person)
    fake = email.surrogate
    text = (
        f"TO {fake.upper()}, see https://crm.example/find?q={quote(fake, safe='')}, "
        f"cc {fake} and {fake}. Signed {person.surrogate}."
    )
    restored, count = engine.unmask(text)
    original = email.original
    assert restored == (
        f"TO {original.upper()}, see https://crm.example/find?q={quote(original, safe='')}, "
        f"cc {original} and {original}. Signed {NAME}."
    )
    assert count == 2


# Latet's credentials of the main line's fixes (latet-tokens), and a secret-named pair in the middle of a log line.
CODE = "latet_code_Qm7xK2pL9vR4tW8yZ3nB6cD1fG5hJ0sA"  # 32 characters after its prefix (24 bytes)
SCIM = "latet_scim_" + "Hk3Lm8Np2Qr7St1Uv6Wx0Yz4Ab9Cd5Ef3Gh8Ij2Kl7M"  # 43 after it
BRIDGE = "latet_brg_" + "Pq4Rs9Tu3Vw8Xy2Za7Bc1De6Fg0Hi5Jk9Lm3No8Pq2R"
LOG_LINE = "2026-10-26T12:00:01Z INFO login ok user=ada password=Winter2024x session started"


def test_the_main_lines_credentials_and_a_pair_in_a_line_are_masked_and_restored_out_of_memory():
    """The final review's M1: the formats the main line's fixes mask (`latet_code_`, `latet_scim_`, `latet_brg_`, and a
    secret-named pair in the middle of a line), masked by an applications call, pushed out of the working set by other
    values, then masked again with the fakes they had and restored from the store by another instance, as any value."""
    store = _Store()
    vault = _vault(store)
    texts = [f"code {CODE} given", f"scim token {SCIM}", f"bridge {BRIDGE} ok", LOG_LINE]
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        first = [_engine(vault).mask_text(t, SlotKind.TOOL_RESULT, Session("t", kept=False)) for t in texts]
    kept = {e.original: e for e in store.rows.values()}
    assert {CODE, SCIM, BRIDGE, "Winter2024x"} <= set(kept)
    assert all(v not in done.text for v in (CODE, SCIM, BRIDGE, "Winter2024x") for done in first)
    for token, prefix in ((CODE, "latet_code_"), (SCIM, "latet_scim_"), (BRIDGE, "latet_brg_")):
        assert kept[token].surrogate.startswith(prefix) and len(kept[token].surrogate) == len(token)
    _many(vault, 600)
    for token in (CODE, SCIM, BRIDGE):
        assert not _held(vault, kept[token])  # out of memory: in the store alone
    later = _vault(store)  # another instance, built after
    with later.holding(scoped=True):
        later.prefetch(texts)
        again = [_engine(later).mask_text(t, SlotKind.TOOL_RESULT, Session("t", kept=False)) for t in texts]
    assert [done.text for done in again] == [done.text for done in first]  # the fakes they had
    elsewhere = _vault(store)
    with elsewhere.holding(scoped=True):
        masked = [done.text for done in first]
        elsewhere.prefetch_fakes(masked)
        restored = [_engine(elsewhere).unmask(t) for t in masked]
    assert [text for text, _ in restored] == texts and [count for _, count in restored] == [1, 1, 1, 1]


def _namesakes(store: _Store, count: int = 200) -> list[Entity]:
    """``count`` people of one surname, kept by another instance: one anchor for all of them."""
    elsewhere = _vault(store)
    return [elsewhere.intern("PERSON", EntityClass.NER, f"Nome{n} Lanzavecchia", rule="ner") for n in range(count)]


def test_only_the_values_a_text_holds_join_memory():
    """Ruling T8a (b): a text's words find every value of its anchor, two hundred namesakes; the one the text holds
    joins memory, and the others leave the working set as it was."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    people = _namesakes(store)
    before = vault.count
    done = engine.mask_text("ticket closed by Nome7 Lanzavecchia", SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert done.text == f"ticket closed by {people[7].surrogate}"
    assert vault.count == before + 1 and _held(vault, people[7]) and not _held(vault, people[8])


@pytest.mark.parametrize("where", ["another instance", "evicted"])
@pytest.mark.parametrize(("kept", "written"), [("NFC", "NFD"), ("NFD", "NFC")], ids=["written-nfd", "kept-nfd"])
def test_a_value_written_in_another_unicode_form_is_masked(kept, written, where):
    """Review round 3: a name with an accent, kept composed and written decomposed (as macOS writes a file's name), or
    the other way round, out of memory (kept by another instance, or let go since): the detectors of known values find
    either form, and so does the weighing of what a text holds; it joins memory and is masked, as it was when every
    value was in memory."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    keeper = _vault(store) if where == "another instance" else vault
    person = keeper.intern("PERSON", EntityClass.NER, unicodedata.normalize(kept, ACCENTED), rule="ner")
    if where == "evicted":
        _many(vault)
    assert not _held(vault, person)
    text = f"ticket closed by {unicodedata.normalize(written, ACCENTED)}"
    done = engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert _held(vault, person)
    assert ACCENTED not in unicodedata.normalize("NFC", done.text) and done.text.startswith("ticket closed by ")


def test_a_calls_later_text_finds_the_values_its_first_words_found():
    """A text masked within a call whose words were asked for already (a value of a JSON document, read from the
    request's text): the values those words found are weighed against it too, with no other call to the store."""
    store = _Store()
    vault = _vault(store)
    people = _namesakes(store)
    with vault.holding():
        vault.prefetch(['{"note": "Nome9 or Nome10? the Lanzavecchia family"}'])  # its words, not the name
        assert not _held(vault, people[9])
        store.calls.clear()
        vault.prefetch(["by Nome9 Lanzavecchia"])  # a later text of the call, of the same words
        assert _held(vault, people[9])
        assert store.calls == []


def test_a_resumed_conversations_fake_out_of_memory_is_adopted():
    """Review Minor 6: a conversation resumed with fakes in its texts (the plugin's) has them restored in its answers,
    those that left memory too."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    email = vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@gmail.com")
    _many(vault, start=1000)
    assert not _held(vault, email)
    session = Session("resumed", kept=False)
    engine.mask_text(f"as before, write to {email.surrogate}", SlotKind.USER_TEXT, session, keep_surrogates=True)
    assert email.id in session.entity_ids


@pytest.mark.parametrize("written", ["NFC", "NFD"])
def test_a_resumed_conversations_accented_fake_is_adopted_in_either_form(written):
    """Review round 3: a fake with an accent, out of memory, written composed or decomposed in a resumed conversation's
    text: adopted either way, its answers restored."""
    store = _Store()
    person = Entity("e" + "6" * 19, "PERSON", EntityClass.NER, NAME, ACCENTED, 0, 0.0, "ner")
    store.rows[person.id] = person
    vault = _vault(store)
    engine = _engine(vault)
    _many(vault, start=1000)
    assert not _held(vault, person)
    session = Session("resumed", kept=False)
    text = f"as before, ask {unicodedata.normalize(written, ACCENTED)}"
    engine.mask_text(text, SlotKind.USER_TEXT, session, keep_surrogates=True)
    assert person.id in session.entity_ids


def _tool_output_sent_twice(document: str, value: str, etype: str, eclass: EntityClass) -> tuple[str, str, Entity]:
    """A conversation's request with ``document`` as a tool's output, sent twice; between the two, another instance
    keeps ``value``, which this one never hears of: the tool output as each request carried it, and the entity."""
    store = _Store()
    vault = _vault(store)
    built = build(Config(profile="balanced", ner="off"), vault)
    mediator = Mediator(built.engine, built.verifier)
    call = {"id": "c1", "type": "function", "function": {"name": "ticket", "arguments": "{}"}}
    messages = [
        {"role": "user", "content": "look up the ticket"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": document},
    ]
    raw = json.dumps({"model": "m", "messages": messages}).encode()
    upstream = Upstream("openai", "/openai", "http://127.0.0.1:1")

    def sent() -> str:
        prepared = mediator.prepare(upstream, "POST", "/openai/v1/chat/completions", {"authorization": "Bearer t"}, raw)
        return json.loads(prepared.body)["messages"][2]["content"]

    first = sent()
    entity = _vault(store).intern(etype, eclass, value, rule="ner")
    vault.intern("EMAIL", EntityClass.PII, "other.one@acme.it")  # the version moves here
    return first, sent(), entity


@pytest.mark.parametrize(
    ("ascii_only", "form"),
    [(False, "NFC"), (True, "NFC"), (False, "NFD"), (True, "NFD")],
    ids=["letters", "escapes", "decomposed-letters", "decomposed-escapes"],
)
def test_a_kept_json_output_holds_no_value_kept_elsewhere_since(ascii_only, form):
    """Review round 2: a tool's JSON output its conversation sends again, a name in it kept by another instance since:
    the kept form (written again with its letters) holds the name, the request's text holds it as JSON escapes; it
    leaves masked, as it did when every value was in memory. Round 3: its accent decomposed too."""
    written = unicodedata.normalize(form, ACCENTED)
    document = json.dumps({"email": "mario.rossi@acme.it", "note": f"closed by {written}"}, ensure_ascii=ascii_only)
    first, second, person = _tool_output_sent_twice(document, ACCENTED, "PERSON", EntityClass.NER)
    assert written in json.loads(first)["note"]  # unknown then
    note = json.loads(second)["note"]
    assert ACCENTED not in unicodedata.normalize("NFC", note) and note.startswith("closed by ")
    if form == "NFC":
        assert person.surrogate in second


def test_a_kept_text_is_weighed_in_the_form_it_was_kept():
    """A value written in the request in a form no reading of the text puts back (a slash JSON escapes, `\\/`), and
    in the kept form as it is (the document written again, an address masked in it): weighed against the kept text, it
    joins memory and the kept text is masked again."""
    value = "ACME/Northwind"
    document = json.dumps({"account": value, "email": "mario.rossi@acme.it"}).replace("/", "\\/")
    first, second, account = _tool_output_sent_twice(document, value, "CUSTOMER", EntityClass.CUSTOM)
    assert json.loads(first)["account"] == value and value in first  # unknown then, and kept with its slash
    assert value not in second and account.surrogate in second


def test_a_string_one_entity_holds_as_a_secret_is_weighed_inside_a_word():
    """Review round 2, nit: a string held as a secret by one entity and as another kind by another is looked for as the
    detectors look for a secret, inside a word too."""
    plain = Entity("e" + "3" * 19, "CUSTOMER", EntityClass.CUSTOM, "zq7KeyMaterial", "fake-a", 0, 0.0, None)
    secret = Entity("e" + "4" * 19, "PASSWORD", EntityClass.SECRET, "zq7KeyMaterial", "fake-b", 0, 0.0, None)
    held = stored._Weighing([plain, secret]).held_in(["token=abczq7KeyMaterialdef"])
    assert {e.id for e in held} == {plain.id, secret.id}


def test_a_gateway_request_asks_the_store_once():
    """Review Minor 2: the texts of a request (its messages, the keys of a tool call's input) are asked for at once, and
    a value kept elsewhere is masked in every one of them."""
    store = _Store()
    vault = _vault(store)
    built = build(Config(profile="balanced", ner="off"), vault)
    mediator = Mediator(built.engine, built.verifier)
    customer = _vault(store).intern("CUSTOMER", EntityClass.CUSTOM, NAME, rule="recognizers:customer")
    messages = [{"role": "user", "content": f"note {n}"} for n in range(5)] + [
        {"role": "user", "content": f"to {NAME}"}
    ]
    raw = json.dumps({"model": "m", "messages": messages}).encode()
    store.calls.clear()
    upstream = Upstream("openai", "/openai", "http://127.0.0.1:1")
    prepared = mediator.prepare(upstream, "POST", "/openai/v1/chat/completions", {"authorization": "Bearer t"}, raw)
    assert [call for call in store.calls if call.startswith("with_words")] == ["with_words:original"]
    assert NAME not in prepared.body.decode() and customer.surrogate in prepared.body.decode()


def test_one_store_call_for_a_whole_unmask():
    """The fakes of a call's texts are asked for once for all of them, as their values are (R7)."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    fakes = [vault.intern("EMAIL", EntityClass.PII, _email(n)).surrogate for n in range(300)]
    _many(vault, start=1000)
    store.calls.clear()
    with vault.holding():
        vault.prefetch_fakes([f"line {n}: {fake}" for n, fake in enumerate(fakes)])
        restored = [engine.unmask(f"line {n}: {fake}") for n, fake in enumerate(fakes)]
    assert restored == [(f"line {n}: {_email(n)}", 1) for n in range(300)]
    assert store.calls == ["with_words:surrogate"]


def test_unmask_reaches_no_other_vault():
    ours, theirs = _Store(), _Store()
    engine = _engine(_vault(ours))
    other = StoredVault(b"\x09" * 32, theirs, working=100, on_insert=theirs.save_entity)
    their_fake = other.intern("EMAIL", EntityClass.PII, _email(7)).surrogate
    assert engine.unmask(f"from {their_fake}") == (f"from {their_fake}", 0)


def test_a_value_without_anchor_is_always_found():
    """A value or a fake with no anchor word is found by no text's words: held in memory for good."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    badge = vault.intern("BADGE", EntityClass.CUSTOM, "x9-k2")
    assert anchor(badge.original) is None
    _many(vault)
    assert _held(vault, badge)
    done = engine.mask_text("badge x9-k2 used at gate 4", SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert done.text == f"badge {badge.surrogate} used at gate 4"
    assert engine.unmask(done.text) == ("badge x9-k2 used at gate 4", 1)
    # So too after the vault is made again from its store: the values without anchor are loaded with the newest.
    again = _vault(store)
    assert _held(again, badge)


def test_a_new_vault_holds_the_newest_values():
    store = _Store()
    _many(_vault(store))
    vault = _vault(store)
    held = {e.original for e in vault.entities()}
    assert len(held) == 100 and _email(499) in held and _email(0) not in held


def test_lookups_reach_the_store_and_bring_what_they_find_into_memory():
    store = _Store()
    vault = _vault(store)
    email = vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@gmail.com")
    _many(vault)
    assert not _held(vault, email)
    assert vault.by_surrogate(email.surrogate) == email and _held(vault, email)
    _many(vault, start=1000)
    assert vault.get(email.id) == email and _held(vault, email)
    _many(vault, start=2000)
    assert vault.by_original(email.original) == email
    _many(vault, start=3000)
    assert vault.is_surrogate(email.surrogate) and not vault.is_surrogate(email.original)


def test_a_string_found_to_be_no_fake_is_asked_once():
    store = _Store()
    vault = _vault(store)
    fake = "nobody.at.all@gmail.com"
    store.calls.clear()
    assert not vault.is_surrogate(fake)
    assert not vault.is_surrogate(fake)
    # As written, and as a fake in escapes reads: one probe each by the string's keyed hash, never the fakes that share
    # a word with it (Review Important 2).
    assert store.calls == ["by_surrogate", "by_reading"]
    # Until the vault learns it as a fake (kept by another instance): out of memory since, it is asked for again.
    entity = Entity("e" + "1" * 19, "EMAIL", EntityClass.PII, "real.person@gmail.com", fake, 0, 0.0, None)
    store.save_entity(entity)
    vault.learn(entity)
    _many(vault)
    assert not _held(vault, entity)
    assert vault.is_surrogate(fake)


def test_no_store_call_on_the_event_loop():
    store = _Store()
    vault = _vault(store)
    email = vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@gmail.com")
    _many(vault)
    store.calls.clear()

    async def looking() -> tuple[Entity | None, Entity | None]:
        on_the_loop = vault.by_surrogate(email.surrogate)
        assert store.calls == []  # nothing asked on the loop's thread
        return on_the_loop, await asyncio.to_thread(vault.by_surrogate, email.surrogate)

    on_the_loop, in_a_thread = asyncio.run(looking())
    assert on_the_loop is None and in_a_thread == email
    assert store.calls == ["by_surrogate"]


def test_a_request_only_checked_is_stopped_by_a_value_found_long_ago():
    """The final check of a request Latet does not rewrite (Claude Code's) finds a value found once, out of memory
    since, as it finds every value: asked for in the store with the request's words."""
    store = _Store()
    vault = _vault(store)
    verifier = build(Config(profile="balanced", ner="off"), vault).verifier
    customer = vault.intern("CUSTOMER", EntityClass.CUSTOM, NAME, rule="recognizers:customer")
    _many(vault)
    assert not _held(vault, customer)
    result = verifier.verify([(f"please call {NAME} today", SlotKind.USER_TEXT, False)], "s")
    assert [(leak.etype, leak.blocks) for leak in result.leaks] == [("CUSTOMER", True)]


def test_a_conversations_values_are_held_before_its_answer_is_restored():
    """A conversation's values that left memory are brought back as its request is masked, so that its answer,
    restored on the event loop where nothing waits on the store, finds them."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    session = Session("conversation", kept=False)
    first = engine.mask_text(f"write to {_email(1)}", SlotKind.USER_TEXT, session).text
    _many(vault, start=1000)

    async def restored() -> str:
        return "".join(r.replacement for r in engine.restores(session, json_mode=False) if r.pattern in first)

    assert asyncio.run(restored()) == ""  # out of memory: the event loop does not find it
    engine.hold(session)  # in the thread that masks
    assert _email(1) in asyncio.run(restored())


def test_a_store_that_does_not_answer_masks_nothing():
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    store.down = True
    with pytest.raises(MaskingError):
        engine.mask_text(f"write to {_email(1)}", SlotKind.USER_TEXT, Session("t", kept=False))
    with pytest.raises(MaskingError):
        engine.unmask("from someone")


def test_a_text_masked_before_is_checked_again_once_values_left_memory():
    """A text the engine keeps masked held no value in clear that the vault knew then. Once values have left memory
    since, memory alone cannot vouch for it: the store is asked for the values it may hold, and one kept since (on
    another instance, say) is masked."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    text = f"ticket 4411 closed by {NAME}"
    session = Session("t", kept=False)
    assert engine.mask_text(text, SlotKind.TOOL_RESULT, session).text == text  # nothing known in it yet
    elsewhere = _vault(store)  # another instance keeps the name, which this one never hears of
    person = elsewhere.intern("PERSON", EntityClass.NER, NAME, rule="ner")
    _many(vault)  # values come and go here meanwhile
    assert engine.mask_text(text, SlotKind.TOOL_RESULT, session).text == f"ticket 4411 closed by {person.surrogate}"


def test_values_are_let_go_a_share_at_a_time():
    store = _Store()
    vault = _vault(store)
    _many(vault, 100)
    version = vault.version
    vault.intern("EMAIL", EntityClass.PII, _email(100))  # the 101st: a tenth of the working set goes
    assert vault.count == 100 - int(100 * EVICT_SHARE)
    assert vault.version > version and not vault.holds_since(version)
    after = vault.version
    vault.intern("EMAIL", EntityClass.PII, _email(101))
    assert vault.holds_since(after)  # nothing let go since


def test_a_catch_up_has_every_text_masked_before_it_checked_again():
    """Review Critical 1: an announcement lost, the catch-up cannot say which cached text holds the value another
    instance kept meanwhile, nor did memory let anything go: memory no longer vouches for any text masked before it."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    for n in range(20):  # well within the working set: nothing is let go
        vault.intern("EMAIL", EntityClass.PII, _email(n))
    text = f"ticket 4411 closed by {NAME}"
    session = Session("t", kept=False)
    assert engine.mask_text(text, SlotKind.TOOL_RESULT, session).text == text
    elsewhere = _vault(store)  # another instance keeps the name and a value without anchor; this one never hears
    person = elsewhere.intern("PERSON", EntityClass.NER, NAME, rule="ner")
    badge = elsewhere.intern("BADGE", EntityClass.CUSTOM, "x9-k2")
    vault.resync()
    vault.intern("EMAIL", EntityClass.PII, _email(999))  # it works on: the version moves, nothing is let go
    assert engine.mask_text(text, SlotKind.TOOL_RESULT, session).text == f"ticket 4411 closed by {person.surrogate}"
    assert _held(vault, badge)  # no text's word finds it: the catch-up reads the values without anchor again


def test_a_conversation_held_for_its_answer_stays_in_memory_until_the_answer_ends(monkeypatch):
    """Review Important 1: while the model answers, the calls of the same key keep values; the conversation's are held
    until its answer is restored (on the event loop, from memory alone), then let go as any other, and a hold whose
    answer never ends lapses."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    session = Session("conversation", kept=False)
    first = engine.mask_text(f"write to {_email(1)}", SlotKind.USER_TEXT, session).text

    async def restored() -> str:
        return "".join(r.replacement for r in engine.restores(session, json_mode=False) if r.pattern in first)

    held = engine.hold(session)  # its request leaves
    other = threading.Thread(target=_many, args=(vault, 100, 1000))  # an application's call on the same key
    other.start()
    other.join()
    assert _email(1) in asyncio.run(restored())
    engine.release(session, held)  # its answer ended
    _many(vault, 100, 2000)
    assert asyncio.run(restored()) == ""
    monkeypatch.setattr(stored, "HOLD_SECONDS", 0.0)  # an answer that never ends holds its values no longer than this
    engine.hold(session)
    _many(vault, 100, 3000)
    assert asyncio.run(restored()) == ""


def test_what_conversations_hold_is_bounded_by_the_working_set():
    """Review round 2: forty conversations of a key answered at once, fifty values each, on a working set of a hundred:
    what they hold for their answers is a working set at most, the oldest holds lapsing first."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    sessions = []
    for c in range(40):
        session = Session(f"conversation-{c}", kept=False)
        for n in range(50):
            engine.mask_text(f"write to c{c:02d}.person{n:03d}@acme.it", SlotKind.USER_TEXT, session)
        engine.hold(session)
        sessions.append(session)
    assert vault.count <= 2 * vault.working
    held = {i for session in sessions[-2:] for i in session.entity_ids}
    assert held <= {e.id for e in vault.entities()}  # the newest holds stand


def test_holds_whose_answers_never_end_lapse(monkeypatch):
    """A hold whose answer never ends (a request stopped after it was masked, a WebSocket turn) is dropped once its time
    is over, at the next hold, whether or not the working set is full."""
    monkeypatch.setattr(stored, "HOLD_SECONDS", 0.0)
    vault = _vault(_Store(), working=1000)
    engine = _engine(vault)
    for c in range(300):
        session = Session(f"ws-{c}", kept=False)
        engine.mask_text(f"write to c{c:03d}@acme.it", SlotKind.USER_TEXT, session)
        engine.hold(session)
    assert len(vault._sessions) <= 1


def test_two_requests_of_one_conversation_are_held_until_both_answers_end():
    """Review round 2: the first answer of two that one conversation awaits at once lets nothing of the second go."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    session = Session("conversation", kept=False)
    first = engine.mask_text(f"write to {_email(1)}", SlotKind.USER_TEXT, session).text

    async def restored() -> str:
        return "".join(r.replacement for r in engine.restores(session, json_mode=False) if r.pattern in first)

    first_held = engine.hold(session)  # its first request leaves
    second_held = engine.hold(session)  # and its second, at once
    engine.release(session, first_held)  # the first answer ends
    _many(vault, 100, 1000)
    assert _email(1) in asyncio.run(restored())
    engine.release(session, second_held)  # the second
    _many(vault, 100, 2000)
    assert asyncio.run(restored()) == ""


def _spied(vault: StoredVault) -> list[str]:
    """The conversations ``vault`` is asked to hold for their answers, in the order asked."""
    keys: list[str] = []
    hold = vault.hold

    def spy(ids, key=None):
        if key is not None:
            keys.append(key)
        return hold(ids, key)

    vault.hold = spy  # type: ignore[method-assign]
    return keys


@contextlib.asynccontextmanager
async def _gateway(
    vault: StoredVault, provider: Callable[[httpx.Request], httpx.Response]
) -> AsyncIterator[httpx.AsyncClient]:
    """A gateway over ``vault`` whose provider answers with ``provider``, and a client of it."""
    built = build(Config(profile="balanced", ner="off"), vault)
    config = GatewayConfig(
        port=5283,
        upstreams=[Upstream("openai", "/openai", "http://127.0.0.1:1")],
        mediator=Mediator(built.engine, built.verifier),
        client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client,
    ):
        yield client


def _answered(request: httpx.Request) -> httpx.Response:
    body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}).encode()
    return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))


async def _chat(client: httpx.AsyncClient, messages: list[dict]) -> httpx.Response:
    chat = {"model": "m", "messages": messages}
    return await client.post("/openai/v1/chat/completions", json=chat, headers={"authorization": "Bearer t"})


@pytest.mark.parametrize("ends", ["answered", "refused", "unreachable", "streamed", "blocked"])
async def test_a_conversation_is_let_go_however_its_request_ends(ends):
    """Review round 2: what a key's vault holds for a conversation's answer is let go however the request ends: an
    answer restored whole or streamed, one the provider refused, a provider that did not answer; round 3: a request
    stopped once masked (a card number written as a JSON number, which the balanced profile does not mask)."""
    vault = _vault(_Store())
    keys = _spied(vault)

    def provider(request: httpx.Request) -> httpx.Response:
        if ends == "unreachable":
            raise httpx.ConnectError("no route to the provider")
        status, kind, body = 200, "application/json", b""
        if ends == "refused":
            status, body = 500, json.dumps({"error": {"message": "overloaded"}}).encode()
        elif ends == "streamed":
            chunk = {"choices": [{"index": 0, "delta": {"content": "ok"}}]}
            kind, body = "text/event-stream", f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        else:
            body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}).encode()
        return httpx.Response(status, headers={"content-type": kind}, stream=httpx.ByteStream(body))

    messages = [{"role": "user", "content": f"write to {_email(1)}"}]
    if ends == "blocked":
        call = {"id": "c1", "type": "function", "function": {"name": "query", "arguments": "{}"}}
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"customer": "c-101", "card": 4111111111111111}'},
        ]
    async with _gateway(vault, provider) as client:
        answer = await _chat(client, messages)
    assert answer.status_code == {"refused": 500, "unreachable": 502, "blocked": 400}.get(ends, 200)
    assert keys and vault._sessions == {}  # it held, and was let go


async def test_a_request_that_never_held_lets_no_other_request_go():
    """Review round 3: a request whose masking failed before its conversation's vault held anything (the store did not
    answer) lets go nothing of what another request of the conversation, its answer still awaited, holds."""
    store = _Store()
    vault = _vault(store)
    keys = _spied(vault)
    messages = [{"role": "user", "content": f"write to {_email(1)}"}]
    async with _gateway(vault, _answered) as client:
        assert (await _chat(client, messages)).status_code == 200
        conversation = keys[-1]
        vault.hold(["e" + "7" * 19], conversation)  # another request of the conversation, its answer awaited
        store.down = True
        refused = await _chat(client, messages)
        store.down = False
    assert refused.status_code == 400 and keys == [conversation, conversation]  # it never held
    assert vault._sessions[conversation].count == 1


def test_an_older_requests_release_takes_nothing_of_a_newer_hold(monkeypatch):
    """Task 8b's review, minor 5: a request whose hold lapsed (its answer slower than ``HOLD_SECONDS``) and whose
    conversation held anew since (its next request) ends: its release takes nothing of the new hold, whose request's
    answer is still awaited. Each release hands back the hold its own request counted in."""
    vault = _vault(_Store())
    email = vault.intern("EMAIL", EntityClass.PII, _email(1))
    monkeypatch.setattr(stored, "HOLD_SECONDS", 0.0)
    older = vault.hold([email.id], "conversation")  # a request whose answer is slow: its hold lapses
    monkeypatch.setattr(stored, "HOLD_SECONDS", 600.0)
    newer = vault.hold([email.id], "conversation")  # the next request: the lapsed hold dropped, a new one counted
    assert older is not None and newer is not None and older is not newer
    vault.release("conversation", older)  # the slow answer ends at last
    assert vault._sessions["conversation"] is newer and newer.count == 1
    vault.release("conversation", newer)
    assert vault._sessions == {}


def test_a_hold_whose_store_call_fails_is_not_counted():
    """Task 8's review: a hold whose values out of memory the store does not bring back (it does not answer) raises and
    leaves no count behind, which no release would pair (the conversation's values pinned until the hold lapsed); a
    conversation held already keeps its own count."""
    store = _Store()
    vault = _vault(store)
    first = vault.intern("EMAIL", EntityClass.PII, _email(1))
    second = vault.intern("EMAIL", EntityClass.PII, _email(2))
    _many(vault, 500, 1000)  # both out of memory
    store.down = True
    with pytest.raises(MaskingError):
        vault.hold([first.id], "conversation")  # a request: the store does not answer its hold
    assert vault._sessions == {}
    store.down = False
    held = vault.hold([first.id], "conversation")  # the next, its answer awaited
    store.down = True
    with pytest.raises(MaskingError):
        vault.hold([second.id], "conversation")  # another of the conversation, the store down again
    store.down = False
    assert vault._sessions["conversation"].count == 1
    vault.release("conversation", held)  # the answer awaited ends: nothing is held any more
    assert vault._sessions == {}


def test_a_count_put_off_is_made_once_its_second_is_over(monkeypatch):
    """Review Minor 4: a key's vault is counted at most once a second as values another instance kept are heard; the
    last of them is counted all the same, once the second is over, though no other value follows it."""
    from latet.server import spaces

    monkeypatch.setattr(spaces, "RECOUNT_SECONDS", 0.2)
    store = _Store()
    vault = _vault(store)
    spaces._recount(vault, store)
    assert store.count == 0
    store.save_entity(Entity("e" + "2" * 19, "EMAIL", EntityClass.PII, _email(2), "a.b@gmail.com", 0, 0.0, None))
    store.count = 0  # kept by another instance: this one's count did not move
    spaces._recount(vault, store)  # within the second: put off
    assert store.count == 0
    deadline = time.monotonic() + 5
    while store.count != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.count == 1


def test_resync_drops_what_the_store_no_longer_has():
    store = _Store()
    vault = _vault(store)
    gone = vault.intern("EMAIL", EntityClass.PII, _email(1))
    kept = vault.intern("EMAIL", EntityClass.PII, _email(2))
    store.forget_entity(gone.id)  # forgotten on another instance, never heard of here
    store.calls.clear()
    vault.resync()
    assert not _held(vault, gone) and _held(vault, kept)
    assert store.calls == ["by_ids"]  # the working set asked for, never the whole vault


def test_threads_mask_and_let_go_at_once():
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    errors: list[BaseException] = []

    def work(first: int) -> None:
        try:
            for n in range(first, first + 150):
                done = engine.mask_text(f"from {_email(n)}", SlotKind.USER_TEXT, Session(f"s{first}", kept=False))
                assert _email(n) not in done.text
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n * 1000,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert vault.count <= 100 and len(store.rows) == 600


# --- 0.9.9 and 0.9.10 in a key's vault: values written in escapes, fakes as they read ----------------------------

ZOE = "Zo\N{LATIN SMALL LETTER E WITH DIAERESIS} Li"  # no ASCII run of three: its anchor is `zoe`


def _plainly(person: Entity, text: str) -> str:
    """``text`` masked by a vault that holds ``person`` in memory, as every vault of a computer holds its values."""
    masker, _ = build_masker(Config(profile="balanced", ner="off"), Vault(ROOT_KEY, [person]))
    return masker.mask(text, SlotKind.TOOL_RESULT).text


@pytest.mark.parametrize(
    "written",
    [
        "Zo\\u00eb Li",
        "Zo%C3%AB Li",
        "Zo&euml; Li",
        "Zo&#235; Li",
        "Zo&#xEB; Li",
        "Zo\\303\\253 Li",
        "Zo\\U000000eb Li",
    ],
    ids=["json", "url", "html-name", "html-number", "html-hex", "git-octal", "python-U"],
)
def test_a_value_out_of_memory_written_in_escapes_is_masked_as_a_vault_that_holds_it_masks_it(written):
    """0.9.9: a value the vault knows, written in escapes where no JSON document is read (HTML's references, git's octal
    bytes, Python's long escape, a URL's quote, JSON's), is found where the escapes read as what they stand for. Out of
    a key's memory, its anchor (`zoe`) is a word of that reading, and the weighing reads it there too: the text is
    masked as a vault that holds the value masks it."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    person = _vault(store).intern("PERSON", EntityClass.NER, ZOE, rule="ner")
    assert anchor(ZOE) == "zoe" and not _held(vault, person)
    text = f"ticket closed by {written} today"
    done = engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert written not in done.text and done.text == _plainly(person, text)


def test_half_a_character_written_alone_is_read_as_the_masker_reads_it():
    """The fuzz of 0.9.9: `\\udd8a` alone (half of a character, with no other half) beside a value out of memory is read
    as the masker reads it (U+FFFD), never as a lone surrogate, which the detectors cannot encode: the value is masked,
    as a vault that holds it masks it."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    person = _vault(store).intern("PERSON", EntityClass.NER, NAME, rule="ner")
    text = f"note \\udd8a closed by {NAME}"
    done = engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert NAME not in done.text and done.text == _plainly(person, text)


def _in_a_link(store: _Store) -> Entity:
    """A name kept as a link writes it (0.9.9, Ruling 099-R1), by another instance: its fake in the same escapes, which
    reads otherwise (`%20` as a space), its longest run glued to an escape (`20martini`)."""
    person = Entity("e" + "7" * 19, "PERSON", EntityClass.NER, "Mario%20Rossi", "Giulia%20Martini", 0, 0.0, "ner", True)
    assert store.save_entity(person) is None
    _many(_vault(store))  # kept after it: a vault made now holds these, the newest
    return person


def _as_read(person: Entity) -> str:
    reading = fake_reading(person)
    assert reading is not None and reading != person.surrogate
    return reading


def _plain_unmask(person: Entity, text: str) -> tuple[str, int]:
    """``text`` restored by a vault that holds ``person`` in memory."""
    vault = Vault(ROOT_KEY, [person])
    masker, _ = build_masker(Config(profile="balanced", ner="off"), vault)
    return Engine(vault, masker).unmask(text)


@pytest.mark.parametrize("call", [False, True], ids=["alone", "in-a-call"])
def test_a_fake_kept_in_escapes_is_restored_out_of_memory_as_it_is_written_and_as_it_reads(call):
    """0.9.9 (Ruling 099-R2): a model writes a link's fake as it reads (`Giulia Martini`), or in a link's other forms;
    out of a key's memory, the fake's anchor is a word of every one (version 3: no run an escape glues letters to), and
    each is restored as a vault that holds it restores it."""
    store = _Store()
    person = _in_a_link(store)
    reading = _as_read(person)
    for text in (
        f"{reading} reviewed it",
        f"see /find?q={person.surrogate}",
        f"see /find?q={reading.replace(' ', '+')}",
    ):
        engine = _engine(_vault(store))
        assert not _held(engine.vault, person)  # type: ignore[arg-type]
        if call:
            with engine.vault.holding():
                engine.vault.prefetch_fakes([text])
                found = engine.unmask(text)
        else:
            found = engine.unmask(text)
        assert found == _plain_unmask(person, text) and found[1] == 1, text


def test_a_fake_as_it_reads_is_restored_from_memory_on_the_event_loop():
    """On the event loop a key's vault restores from memory alone: a fake held there is found as it reads too."""
    store = _Store()
    person = _in_a_link(store)
    vault = _vault(store)
    engine = _engine(vault)
    vault.hold([person.id])
    text = f"{_as_read(person)} reviewed it"

    async def on_the_loop() -> tuple[str, int]:
        return engine.unmask(text)

    assert asyncio.run(on_the_loop()) == _plain_unmask(person, text)


def test_a_resumed_conversation_adopts_a_fake_out_of_memory_as_it_reads():
    store = _Store()
    person = _in_a_link(store)
    vault = _vault(store)
    engine = _engine(vault)
    session = Session("resumed", kept=False)
    engine.mask_text(f"as before, ask {_as_read(person)}", SlotKind.USER_TEXT, session, keep_surrogates=True)
    assert person.id in session.entity_ids


def test_a_fake_as_it_reads_is_a_fake_out_of_memory_until_a_value_of_that_name_is_known():
    """``by_surrogate`` and ``is_surrogate`` answer for a fake as it reads as a vault holding it does, out of a key's
    memory too: a fake (as written, or as it reads by default), and no fake once a real value of that name is known
    (the value wins, Ruling 099 I-1)."""
    store = _Store()
    person = _in_a_link(store)
    reading = _as_read(person)
    vault = _vault(store)
    assert not _held(vault, person)
    assert vault.by_surrogate(reading, as_read=False) is None
    assert vault.by_surrogate(reading) == person
    assert _vault(store).is_surrogate(reading)
    _vault(store).intern("PERSON", EntityClass.NER, reading, rule="ner")  # a real person of that name, elsewhere
    assert not _vault(store).is_surrogate(reading)


@pytest.mark.parametrize("decoded", [True, False, None], ids=["found-where-escapes-read", "as-written", "before"])
@pytest.mark.parametrize(
    ("etype", "eclass", "value", "fake"),
    [
        ("PERSON", EntityClass.NER, "Mario%20Rossi", "Giulia%20Martini"),
        ("PHONE", EntityClass.PII, "333+1234567", "356+5297396"),
        ("PHONE", EntityClass.PII, "%2B393331112222", "%2B393331234567"),  # every run glued to an escape of its own
        ("PASSWORD", EntityClass.SECRET, "p%40ss%2Fw0rd%2BZq9", "s%40fy%2Ft1lb%2BMd3"),
    ],
    ids=["name-in-a-link", "phone-in-a-form", "phone-in-a-link", "password-in-a-link"],
)
def test_how_a_value_was_found_decides_its_fakes_reading_out_of_memory_as_in_memory(
    decoded, etype, eclass, value, fake
):
    """0.9.10's `decoded` column, in a key's vault: an entry found where escapes read (TRUE) has its fake's reading, one
    found as it is written (FALSE) none, one kept before the column (NULL) 0.9.9's rule (a `+` a space in a name
    alone); out of memory, its fake as written and as it might read is restored as a vault holding it restores it, a
    fake whose every run starts at one of its own escapes too (`%2B39...`, Review Important 1)."""
    entry = Entity("e" + "8" * 19, etype, eclass, value, fake, 0, 0.0, None, decoded)
    store = _Store()
    assert store.save_entity(entry) is None
    _many(_vault(store))
    for text in (f"call {fake} now", f"call {unquote_plus(fake)} now", f"call {unquote(fake)} now"):
        engine = _engine(_vault(store))
        assert not _held(engine.vault, entry)  # type: ignore[arg-type]
        assert engine.unmask(text) == _plain_unmask(entry, text), (decoded, text)


SECRET = "tok_9fQ2xL7pZ4"  # its anchor, 9fq2xl7pz4, ends it: a letter after it is glued to that run
IBAN = "IT60X0542811101000000123456"  # one run: glued to a name after it, `IT60...456D'Angelo`


@pytest.mark.parametrize(
    "text",
    [
        "  D%27Angelotok_9fQ2xL7pZ4acme.it AnnaRossin",
        "Niccoltok_9fQ2xL7pZ4n&n",
        "RSSMRA85T10A562S&#39;tok_9fQ2xL7pZ4uD'Angelo",
        "\u00f2n tok_9fQ2xL7pZ44539148803436467",
        '{"key": "tok_9fQ2xL7pZ4\nnext"}',
    ],
)
def test_a_secret_out_of_memory_glued_to_letters_or_digits_is_masked_as_a_vault_that_holds_it_masks_it(text):
    """Ruling T10h: the vault's detectors find a secret inside a word; out of a key's memory, a word of the text finds
    it there too (its anchor's head, ``anchors.edges``), and the text is masked as a vault that holds it masks it."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    secret = _vault(store).intern("API_KEY", EntityClass.SECRET, SECRET)
    assert not _held(vault, secret)
    done = engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert SECRET not in done.text and done.text == _plainly(secret, text)


@pytest.mark.parametrize("call", [False, True], ids=["alone", "in-a-call"])
@pytest.mark.parametrize(
    ("before", "after"),
    [("pay ", "D'Angelo"), ("=", "D%27Angelo%2CMaria"), ("\u00f2 ", "7 now"), ("@", "x"), ("pay x", " now")],
)
def test_a_fake_out_of_memory_glued_to_letters_or_digits_is_restored_as_a_vault_that_holds_it_restores_it(
    call, before, after
):
    """Ruling T10h: a fake that is no word (an IBAN's) is restored inside a word, by a vault that holds it; out of a
    key's memory, a word of the text finds it there too (its anchor's head or tail), and the text is restored as a vault
    that holds it restores it."""
    store = _Store()
    iban = _vault(store).intern("IBAN", EntityClass.PII, IBAN, rule="pii:iban")
    _many(_vault(store))  # kept after it: a vault made now holds those
    text = f"{before}{iban.surrogate}{after}"
    engine = _engine(_vault(store))
    assert not _held(engine.vault, iban)  # type: ignore[arg-type]
    if call:
        with engine.vault.holding():
            engine.vault.prefetch_fakes([text])
            found = engine.unmask(text)
    else:
        found = engine.unmask(text)
    assert found == _plain_unmask(iban, text) and found[1] == 1, text


def test_a_store_finds_a_value_or_a_fake_by_its_edges_only_where_a_vault_finds_it_inside_a_word():
    """Ruling T10h: what a store finds an entry's value or fake by (``stored.words_of``): its anchor, and its edges
    where the vault's detectors find the value inside a word (a secret) or a restore finds the fake there (no word)."""
    secret = Entity("e1", "API_KEY", EntityClass.SECRET, SECRET, "jqb_9jI0bA3tA3", 0, 0.0, None)
    iban = Entity("e2", "IBAN", EntityClass.PII, IBAN, "IT84W7093766632581800787511", 0, 0.0, None)
    person = Entity("e3", "PERSON", EntityClass.NER, "Massimiliano Ferri", "Bartolomeo Conti", 0, 0.0, None)
    assert stored.words_of(secret, "original") == ("9fq2xl7pz4", "9fq2xl")
    assert stored.words_of(secret, "surrogate") == ("9ji0ba3ta3", "9ji0ba")
    assert stored.words_of(iban, "original") == ("it60x0542811101000000123456",)  # found whole by the detectors
    assert stored.words_of(iban, "surrogate") == ("it84w7093766632581800787511", "it84w7", "787511")
    assert stored.words_of(person, "original") == ("massimiliano",)  # a name: whole, value and fake
    assert stored.words_of(person, "surrogate") == ("bartolomeo",)


def test_a_fake_shaped_as_an_address_an_ipv4_address_or_a_domain_is_found_by_a_word_of_its_own():
    """Task 10's F3: what a store finds a fake by (``stored.words_of``): an address's local part with its domain's name
    (version 8, ``anchors.address_word``), an IPv4 address and a domain whole (``anchors.dotted``), so that a text
    naming one opens it and few others, not the thousands of fakes that share its domain, `example`, its local part or
    the first number of an address; other fakes by their anchor and edges."""
    address = Entity(
        "e1", "EMAIL", EntityClass.PII, "maria.rossi@nordest.it", "Lily.Harris@liberty-d05e.example", 0, 0.0, None
    )
    ip = Entity("e2", "IP_ADDRESS", EntityClass.PII, "151.12.3.4", "198.51.100.7", 0, 0.0, None)
    domain = Entity("e3", "DOMAIN", EntityClass.PII, "nordest.it", "liberty-d05e.example", 0, 0.0, None)
    odd = Entity("e4", "EMAIL", EntityClass.PII, "m+x@nordest.it", "lily+news@liberty-d05e.example", 0, 0.0, None)
    assert stored.words_of(address, "surrogate") == ("lily.harris@liberty-d05e",)
    assert stored.words_of(ip, "surrogate") == ("198.51.100.7",)
    assert stored.words_of(domain, "surrogate") == ("liberty-d05e.example",)
    assert stored.words_of(odd, "surrogate") == ("liberty",)  # its anchor: not shaped as the others are
    assert stored.words_of(address, "original") == ("maria.rossi",)  # a value names a person: its local part (v7)


def test_an_unmask_opens_the_fakes_a_text_names_not_those_that_share_a_domain_with_them():
    """Task 10's F3: 2,000 fakes at one company and 300 IPv4 fakes share their domain, `example` and their first number;
    an unmask of a line naming one of each opens those two."""
    store = _Store()
    keeper = _vault(store, working=1_000)
    people = [keeper.intern("EMAIL", EntityClass.PII, f"person{n:04d}.surname@nordest.it") for n in range(2_000)]
    hosts = [keeper.intern("IP_ADDRESS", EntityClass.PII, f"151.12.{n // 250}.{n % 250 + 1}") for n in range(300)]
    assert len({e.surrogate.rsplit("@", 1)[1] for e in people}) == 1
    vault = _vault(store, working=1_000)
    text = f"login failed for {people[7].surrogate} from {hosts[9].surrogate}: invalid password"
    with vault.holding():
        vault.prefetch_fakes([text])
        found = {e.id for e in vault.candidates_in(text)}
    assert {people[7].id, hosts[9].id} <= found and len(found) <= 6


def test_a_value_shaped_as_a_persons_address_an_ipv4_address_or_a_domain_is_found_by_a_word_of_its_own():
    """Task 10's F4 and F5: a value is found by its address's local part where it names a person (two parts or more, or
    digits: `maria.rossi`, `mrossi84`), an IPv4 address and a domain whole, where its anchor is its company's name or
    an address's first number, which the values at that company (hundreds) or thousands of addresses share; a mailbox
    of a role (`info`) by its anchor, its domain's name, which it shares with fewer than `info` would."""

    def value(etype: str, original: str) -> Entity:
        return Entity("e1", etype, EntityClass.PII, original, "x", 0, 0.0, None)

    assert stored.words_of(value("EMAIL", "Maria.Rossi@nordestlogistica.it"), "original") == ("maria.rossi",)
    assert stored.words_of(value("EMAIL", "mrossi84@nordestlogistica.it"), "original") == ("mrossi84",)
    assert stored.words_of(value("EMAIL", "info@nordestlogistica.it"), "original") == ("nordestlogistica",)
    assert stored.words_of(value("IP_ADDRESS", "151.12.3.4"), "original") == ("151.12.3.4",)
    assert stored.words_of(value("DOMAIN", "nordestlogistica.it"), "original") == ("nordestlogistica.it",)
    assert stored.words_of(value("PERSON", "Maria Rossi"), "original") == ("maria",)


def test_a_mask_opens_the_values_a_text_names_not_their_colleagues():
    """Task 10's F5: 2,000 addresses at one company and 300 IPv4 addresses that share a first number; a line naming one
    of each opens those two, and is masked as a vault holding every one of them masks it."""
    store = _Store()
    keeper = _vault(store, working=1_000)
    people = [
        keeper.intern("EMAIL", EntityClass.PII, f"person{n:04d}.surname@nordestlogistica.it") for n in range(2_000)
    ]
    hosts = [keeper.intern("IP_ADDRESS", EntityClass.PII, f"151.12.{n // 250}.{n % 250 + 1}") for n in range(300)]
    vault = _vault(store, working=1_000)
    text = f"login failed for {people[7].original} from {hosts[9].original}: invalid password"
    asked: list[int] = []
    with_words = store.with_words
    store.with_words = lambda role, words: asked.append(len(found := with_words(role, words))) or found
    engine = _engine(vault)
    done = engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert people[7].original not in done.text and hosts[9].original not in done.text
    assert people[7].surrogate in done.text and hosts[9].surrogate in done.text
    assert sum(asked) <= 4, asked


def test_an_unmask_in_a_call_finds_memorys_fakes_by_their_words_as_the_stores():
    """Task 10b: in a call whose fakes were asked for at once (``prefetch_fakes``), every fake memory holds is the
    store's too, and found by its words as the store's are: no text is read with the detector of memory's fakes, which
    read each text for every one of the working set (half of an unmask's time on a vault of 200,000 values). The texts
    are restored as a vault holding every value restores them; a fake with no word (held for good) is restored too."""
    store = _Store()
    vault = _vault(store, working=1_000)
    engine = _engine(vault)
    held = [vault.intern("EMAIL", EntityClass.PII, _email(n)) for n in range(300)]  # in memory
    wordless = vault.intern("USERNAME", EntityClass.PII, "x9-k2")  # no anchor: held for good, found by no word
    assert stored.words_of(wordless, "original") == () and _held(vault, wordless)
    texts = [f"line {n}: {e.surrogate}" for n, e in enumerate(held)] + [f"user {wordless.surrogate} left"]
    read: list[str] = []
    current = vault.matchers.current
    vault.matchers.current = lambda: read.append("echo") or current()  # type: ignore[method-assign]
    with vault.holding():
        vault.prefetch_fakes(texts)
        restored = [engine.unmask(text) for text in texts]
    plain = Vault(ROOT_KEY, [*held, wordless])
    masker, _ = build_masker(Config(profile="balanced", ner="off"), plain)
    assert restored == [Engine(plain, masker).unmask(text) for text in texts]
    assert all(count == 1 for _, count in restored) and read == []


def test_an_applications_call_masks_with_the_detectors_of_the_whole_working_set():
    """Ruling 10b-R1 (it replaced the call's own detectors of 783ea89b): a call that brought its texts' values into
    memory at once (``holding(scoped=True)``, then ``prefetch``) reads each text with the detectors of the whole working
    set, as every text is read, so that it never masks less than the gateway. The texts are masked as a vault holding
    every value masks them: a name and a secret memory holds, which no detector finds anew; a value kept by an earlier
    text of the call; a value without anchor."""
    store = _Store()
    vault = _vault(store, working=1_000)
    others = [vault.intern("EMAIL", EntityClass.PII, _email(n)) for n in range(500)]  # in memory, in no text
    name = vault.intern("PERSON", EntityClass.NER, NAME, rule="ner")
    secret = vault.intern("API_KEY", EntityClass.SECRET, "tok_9fQ2xL7pZ4")
    badge = vault.intern("BADGE", EntityClass.CUSTOM, "x9-k2")  # no anchor: held for good
    texts = [
        f"{NAME} logged in with tok_9fQ2xL7pZ4",
        "new user ugo.neri84@gmail.com created",
        "ugo.neri84@gmail.com wrote to Ferruccio, badge x9-k2",
        f"{NAME} wrote to ugo.neri84@gmail.com",
    ]
    engine = _engine(vault)
    read: list[str] = []
    whole = ValueMatchers.current
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        ValueMatchers.current = lambda self: read.append("whole") or whole(self)  # type: ignore[method-assign]
        try:
            masked = [engine.mask_text(t, SlotKind.TOOL_RESULT, Session("t", kept=False)).text for t in texts]
        finally:
            ValueMatchers.current = whole  # type: ignore[method-assign]
    plain = Vault(ROOT_KEY, [*others, name, secret, badge])
    masker, _ = build_masker(Config(profile="balanced", ner="off"), plain)
    plainly = Engine(plain, masker)
    assert masked == [plainly.mask_text(t, SlotKind.TOOL_RESULT, Session("t", kept=False)).text for t in texts]
    assert NAME not in masked[0] and "tok_9fQ2xL7pZ4" not in masked[0] and "x9-k2" not in masked[2]
    assert read  # the whole working set's detectors


@pytest.mark.parametrize("in_memory", [True, False], ids=["in-memory", "out-of-memory"])
def test_an_applications_call_masks_a_fake_of_the_space_as_a_value_whatever_its_age(in_memory):
    """Already masked text is not recognised (the spec): a fake the space made is a value like any other, found as the
    vault's detectors of fakes find it (an address at a domain kept for examples, which no detector of addresses takes
    for one). An applications call brings the fakes its texts hold into its own detectors as it brings their values
    (``prefetch``), so that a fake of any age is masked again, in memory or not."""
    store = _Store()
    keeper = _vault(store)
    person = keeper.intern("EMAIL", EntityClass.PII, "maria.rossi@nordest.it")
    assert person.surrogate.endswith(".example")
    if not in_memory:
        _many(keeper)
    vault = _vault(store)
    assert _held(vault, person) == in_memory
    text = f"to {person.surrogate}"
    engine = _engine(vault)
    with vault.holding(scoped=True):
        vault.prefetch([text])
        done = engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert person.surrogate not in done.text and done.text.startswith("to ")


# --- an applications call masks no less than the gateway (Ruling 10b-R1, review I1, M3, M4) ------------------------

# Known secrets glued to letters or digits where no word of the text is theirs: a one-run token glued on both sides, a
# password glued on both sides, an anchor of fewer than six letters glued on one side or both.
GLUED = [
    ("5f3a9c1d2b4e6f70a1b2", "ref x5f3a9c1d2b4e6f70a1b2y end"),
    ("hunter2", "ref xhunter2y end"),
    ("ab12", "ref ab12y end"),
    ("ab12", "ref xab12 end"),
    ("ab12", "ref xab12y end"),
]


def _masked_three_ways(vault: StoredVault, kept: list[Entity], text: str) -> tuple[str, str, str]:
    """``text`` masked in an applications call, by the gateway's path on the same vault (a fresh engine: no cache),
    and by a plain vault holding ``kept``."""
    with vault.holding(scoped=True):
        vault.prefetch([text])
        call = _engine(vault).mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False)).text
    gateway = _engine(vault).mask_text(text, SlotKind.TOOL_RESULT, Session("g", kept=False)).text
    plain = Vault(ROOT_KEY, kept)
    masker, _ = build_masker(Config(profile="balanced", ner="off"), plain)
    return call, gateway, Engine(plain, masker).mask_text(text, SlotKind.TOOL_RESULT, Session("p", kept=False)).text


@pytest.mark.parametrize(("secret", "text"), GLUED)
def test_an_applications_call_masks_a_secret_memory_holds_as_the_gateway_masks_it(secret, text):
    """Ruling 10b-R1: a call's detectors add to what memory holds, never replace it. A secret the vault holds in memory
    is found inside a word, as a plain vault and the gateway's path find it, though no word of the text finds it in
    the store."""
    store = _Store()
    vault = _vault(store, working=1_000)
    kept = vault.intern("API_KEY", EntityClass.SECRET, secret)
    call, gateway, plain = _masked_three_ways(vault, [kept], text)
    assert secret not in call and call == gateway == plain


def test_a_value_kept_on_another_thread_while_an_applications_call_runs_is_masked_in_it():
    """Review M4: a value kept by another thread (the gateway's, or one heard from another instance) after the call
    brought its texts' values in is in memory, and the call masks it, as the gateway's path does."""
    store = _Store()
    vault = _vault(store, working=1_000)
    engine = _engine(vault)
    text = f"ticket closed by {NAME}"
    with vault.holding(scoped=True):
        vault.prefetch([text])  # nothing known of it yet
        elsewhere = threading.Thread(target=vault.intern, args=("PERSON", EntityClass.NER, NAME, "", "ner"))
        elsewhere.start()
        elsewhere.join()
        done = engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert NAME not in done.text


def test_a_value_held_for_good_in_place_of_another_during_an_applications_call_is_masked_in_it():
    """Review M3: one value held for good forgotten and another kept on another thread, during a call: the call masks
    the new one, whatever the count of what memory holds for good."""
    store = _Store()
    vault = _vault(store, working=1_000)
    engine = _engine(vault)
    old = vault.intern("BADGE", EntityClass.CUSTOM, "x9-k2")  # no anchor: held for good
    with vault.holding(scoped=True):
        vault.prefetch(["warm up"])
        engine.mask_text("warm up", SlotKind.TOOL_RESULT, Session("t", kept=False))
        vault.forget(old.id)
        elsewhere = threading.Thread(target=vault.intern, args=("BADGE", EntityClass.CUSTOM, "q7-z4"))
        elsewhere.start()
        elsewhere.join()
        done = engine.mask_text("badge q7-z4 used", SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert "q7-z4" not in done.text


def test_an_unmask_opens_the_address_fake_a_text_names_not_those_that_share_its_local_part():
    """Version 8: in a vault of a million values, thousands of address fakes at as many companies share a local part
    (`lily.harris`), and a call of 500 lines opened 20,594 fakes: an address's fake is found by its local part with its
    domain's name, which a text naming it holds."""
    store = _Store()
    shared = [
        Entity(
            f"e{n:04d}", "EMAIL", EntityClass.PII, f"p{n:04d}.x@c{n}.it", f"lily.harris@c{n:04d}.example", 0, 0.0, None
        )
        for n in range(300)
    ]
    for entity in shared:
        store.save_entity(entity)
    vault = _vault(store)
    text = f"login failed for {shared[7].surrogate}: invalid password"
    opened: list[int] = []
    asked = store.with_words
    store.with_words = lambda role, words: opened.append(len(found := asked(role, words))) or found  # type: ignore[method-assign]
    with vault.holding():
        vault.prefetch_fakes([text])
        found = {e.id for e in vault.candidates_in(text)}
    assert shared[7].id in found and sum(opened) == 1


# Out of memory (version 8): a secret whose glued form no word of a text holds is held in memory for good.
GLUED_HELD = [*[case for case in GLUED if len(case[0]) <= 16], ("tok_ab12", "id tok_ab12x end")]


@pytest.mark.parametrize(("secret", "text"), GLUED_HELD)
def test_a_secret_no_word_finds_glued_is_held_for_good_and_masked_glued_on_every_instance(secret, text):
    """Review I1, out of memory: a secret that is one run of ``ONE_RUN_HELD`` letters or fewer, or whose anchor of six
    letters or fewer begins or ends it, glued to letters or digits, is found by no word of the text: a vault holds it
    in memory for good (``_for_good``), as one without an anchor, so that an instance built later masks it
    glued, in an applications call and on the gateway's path, as a plain vault does."""
    store = _Store()
    keeper = _vault(store)
    kept = keeper.intern("API_KEY", EntityClass.SECRET, secret)
    _many(keeper)
    assert _held(keeper, kept) and kept.id in keeper._for_good
    vault = _vault(store)  # another instance, built after
    assert _held(vault, kept)
    call, gateway, plain = _masked_three_ways(vault, [kept], text)
    assert secret not in call and call == gateway == plain


def _token(n: int) -> str:
    """A token of a log line (`session=`): 16 hex digits, one run, which a vault holds glued (``held_glued``)."""
    return f"{(n * 0x9E3779B97F4A7C15) % (1 << 64):016x}"


def test_what_is_held_for_good_glued_is_bounded_and_the_newest_kept():
    """Ruling 10b-R3 (review N1): a log with a token in each line made each token a value held in memory for good, on
    every instance, read in every text: without bound. What is held glued is bounded (``held_at_most``, the server's
    ``key_held_secrets``), the newest kept; an older one stays in the store, found by its words where nothing is glued
    to it, and the working set keeps its bound."""
    store = _Store()
    vault = StoredVault(ROOT_KEY, store, working=1_000, held_at_most=200, on_insert=store.save_entity)
    tokens = [vault.intern("GENERIC_API_KEY", EntityClass.SECRET, _token(n)) for n in range(3_000)]
    assert all(stored.held_glued(e) for e in tokens)
    assert vault.count <= 1_000
    assert [e.id for e in tokens if e.id in vault._for_good] == [e.id for e in tokens[-200:]]
    later = StoredVault(ROOT_KEY, store, working=100, held_at_most=200, on_insert=store.save_entity)
    assert {e.id for e in tokens[-200:]} == later._for_good and later.count <= 200
    old = tokens[0]
    assert not _held(later, old)
    text = f"GET /orders 200 session={old.original} took 12 ms"
    with later.holding(scoped=True):
        later.prefetch([text])
        done = _engine(later).mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert old.original not in done.text and old.surrogate in done.text


def test_a_catch_up_reads_the_newest_secrets_held_glued_again():
    """Review 10b's R4: a short secret another instance kept while this one missed the change is held glued here once
    its space catches up (``resync``), the newest ``held_at_most`` of them as a build holds them, so that a text that
    glues letters to it on both sides is masked here as there, not only after the space's next build."""
    store = _Store()
    vault = StoredVault(ROOT_KEY, store, working=100, held_at_most=3, on_insert=store.save_entity)
    elsewhere = StoredVault(ROOT_KEY, store, working=100, held_at_most=3, on_insert=store.save_entity)
    secrets = [elsewhere.intern("GENERIC_API_KEY", EntityClass.SECRET, _token(n)) for n in range(5)]
    assert all(stored.held_glued(e) and not _held(vault, e) for e in secrets)
    vault.resync()
    assert {e.id for e in secrets if _held(vault, e)} == {e.id for e in secrets[-3:]} == vault._for_good
    newest = secrets[-1]
    text = f"GET /orders 200 session=zz{newest.original}qq took 12 ms"  # glued on both sides: no word of it finds it
    with vault.holding(scoped=True):
        vault.prefetch([text])
        done = _engine(vault).mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert newest.original not in done.text and newest.surrogate in done.text


def test_ten_thousand_tokens_in_the_store_keep_a_space_within_its_working_set():
    """Review N1: a vault of 10,000 tokens held glued, built: the newest ``held_at_most`` in memory, never all."""
    store = _Store()
    for n in range(10_000):
        e = Entity(f"s{n:019d}", "GENERIC_API_KEY", EntityClass.SECRET, _token(n), _token(n + 77_777), 0, 1.0, None)
        store.rows[e.id] = e
    vault = StoredVault(ROOT_KEY, store, working=1_000, held_at_most=300, on_insert=store.save_entity)
    assert len(vault._for_good) == 300 and vault.count <= 1_000


@pytest.mark.parametrize("call", [True, False], ids=["an applications call", "no call"])
def test_a_value_held_for_good_is_a_candidate_only_of_a_text_that_holds_its_fake(call):
    """The final review's I1: an unmask read every value held in memory for good for every text, the secrets held glued
    among them (1,000 by default), and made their restores again for each. One is a candidate of a text only where the
    text holds a form a restore reads its fake in: glued to letters on both sides, apart, quoted for a URL, a fake of
    no word; the others are not, and the text is restored as before."""
    store = _Store()
    vault = StoredVault(ROOT_KEY, store, working=1_000, held_at_most=200, on_insert=store.save_entity)
    engine = _engine(vault)
    tokens = [vault.intern("GENERIC_API_KEY", EntityClass.SECRET, _token(n)) for n in range(200)]
    password = vault.intern("PASSWORD", EntityClass.SECRET, "p@ss/w0rd")  # held glued, its fake quoted in a URL
    wordless = vault.intern("PASSWORD", EntityClass.SECRET, "ab cd")  # no word: held for good
    held = [*tokens[1:], password, wordless]  # the oldest token let go: 200 held glued at most
    assert {e.id for e in held} == vault._for_good and quote(password.surrogate, safe="") != password.surrogate
    glued, apart = tokens[3], tokens[150]
    text = (
        f"GET /orders 200 session=zz{glued.surrogate}qq next {apart.surrogate} "
        f"see https://crm.example/find?q={quote(password.surrogate, safe='')} pin {wordless.surrogate}"
    )
    with vault.holding(scoped=True) if call else contextlib.nullcontext():
        if call:
            vault.prefetch_fakes([text])
        found = {e.id for e in vault.candidates_in(text)}
        restored, count = engine.unmask(text)
    assert {e.id for e in held} & found == {glued.id, apart.id, password.id, wordless.id}
    assert restored == (
        f"GET /orders 200 session=zz{glued.original}qq next {apart.original} "
        f"see https://crm.example/find?q={quote(password.original, safe='')} pin {wordless.original}"
    )
    assert count == 4


@pytest.mark.timing
def test_an_unmask_with_a_full_set_of_secrets_held_glued_runs_about_as_fast_as_with_none():
    """The final review's I1, measured: an applications call's unmask of 500 log lines, each with a fake of a token
    (`session=`), with the default 1,000 secrets held glued and with none held: 185 and 6,713 lines a second before (36
    times slower), now within twice."""

    def unmask_seconds(held: int) -> float:
        store = _Store()
        vault = StoredVault(ROOT_KEY, store, working=WORKING_VALUES, on_insert=store.save_entity)
        engine = _engine(vault)
        # The token of every line when no other is kept: one run of 10, itself held glued (the only one held then).
        token = vault.intern("GENERIC_API_KEY", EntityClass.SECRET, "Zq81vT5mWx")
        tokens = [vault.intern("GENERIC_API_KEY", EntityClass.SECRET, _token(n)) for n in range(held)] or [token]
        assert len(vault._for_good) == (held or 1) and held in (0, stored.HELD_SECRETS)
        mails = [vault.intern("EMAIL", EntityClass.PII, _email(n)) for n in range(500)]
        lines = [
            f"2026-10-26T12:00:01Z INFO order {n} shipped to {mails[n].surrogate} "
            f"session={tokens[n % len(tokens)].surrogate} ok"
            for n in range(500)
        ]
        took = float("inf")
        for _ in range(3):  # the fastest of three: a shared runner may pause any one of them
            started = time.perf_counter()
            with vault.holding(scoped=True):
                vault.prefetch_fakes(lines)
                restored = [engine.unmask(line) for line in lines]
            took = min(took, time.perf_counter() - started)
        assert [count for _, count in restored] == [2] * 500
        return took

    none, full = unmask_seconds(0), unmask_seconds(stored.HELD_SECRETS)
    assert full < 2 * none + 0.2, f"{full:.2f} s with 1,000 secrets held glued, {none:.2f} s with none"


def _short_addresses(vault: StoredVault, count: int) -> list[Entity]:
    """``count`` addresses of two letters at a one-letter domain (`ab@c.co`): no word of three, so each is held for
    good, and every fake of theirs ends in `.example`, as every address's fake does."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    made = [
        vault.intern("EMAIL", EntityClass.PII, f"{a}{b}@{c}.co")
        for a, b, c in itertools.islice(itertools.product(letters, letters, letters), count)
    ]
    assert {e.id for e in made} <= vault._for_good and all(e.surrogate.endswith(".example") for e in made)
    return made


def test_a_held_fake_sharing_its_ending_with_others_is_found_by_its_whole_form():
    """The security re-review's R-M1: the held fakes are found by their last six characters, which thousands share
    (`xample`), and each found by its whole form, whatever its neighbours: a text holds the ones it names, in every
    form a restore reads (as written, in capitals, quoted for a URL), and none of the others."""
    store = _Store()
    vault = StoredVault(ROOT_KEY, store, working=5_000, on_insert=store.save_entity)
    made = _short_addresses(vault, 300)
    named = made[7], made[150]
    text = (
        f"example to {named[0].surrogate.upper()} and {quote(named[1].surrogate, safe='')}, "
        + "example " * 50
        + f"cc {named[1].surrogate}."
    )
    assert vault._held_forms.held_in(text) == {named[0].id, named[1].id}


@pytest.mark.timing
def test_a_text_full_of_the_ending_thousands_of_held_fakes_share_is_read_in_linear_time():
    """The security re-review's R-M1: every held form under the ending a text's window found was checked, so 2,000 held
    short addresses (4,000 forms under `xample`) made 100 KB of `example ` take 2.84 s, 28 s a MB. Each ending's forms
    are now grouped by length, and the text's slice of each length looked up whole: 1 MB in less than a second."""
    store = _Store()
    vault = StoredVault(ROOT_KEY, store, working=5_000, on_insert=store.save_entity)
    _short_addresses(vault, 2_000)
    text = "example " * (1024 * 1024 // 8)
    took = float("inf")
    for _ in range(3):  # the fastest of three: a shared runner may pause any one of them
        started = time.perf_counter()
        assert vault._held_forms.held_in(text) == set()
        took = min(took, time.perf_counter() - started)
    assert took < 1.0, f"1 MB of 'example ' with 2,000 short addresses held: {took:.2f} s"


def test_an_applications_call_asks_the_store_once_for_the_values_and_the_fakes_of_its_words():
    """Review 10b's (d): a call's texts' words are asked once, for the values and the fakes they find, where they were
    asked twice, once for each (``prefetch`` and ``_fakes_held``)."""
    store = _Store()
    vault = _vault(store)
    _many(vault)
    store.calls.clear()
    with vault.holding(scoped=True):
        vault.prefetch([f"line {n}: {_email(n)} logged in" for n in range(100)])
    assert store.calls == ["with_words:original+surrogate"]


def test_a_call_that_found_no_slot_and_fails_forgets_none_of_what_it_kept():
    """Review N4: a call whose instance had no slot free keeps each value at once: if it then fails, what it kept is
    the store's, and memory forgets none of it, nor masks every text again."""
    store = _Store()
    store.no_slot = True
    vault = _vault(store)
    version = vault.version

    def call() -> None:
        with vault.holding(scoped=True):
            vault.intern("EMAIL", EntityClass.PII, _email(1))
            raise RuntimeError("the call failed")

    with pytest.raises(RuntimeError):
        call()
    kept = vault.by_original(_email(1))
    assert kept is not None and store.rows[kept.id] == kept and _held(vault, kept) and vault.kept_since(version)


# --- an applications call's new values, kept together (Task 10b, Ruling T10i) ----------------------------------------

CALL = [f"line {n}: {_email(n)} logged in from badge x9-k{n}" for n in range(30)]


def test_an_applications_calls_new_values_are_kept_together_once_it_ends():
    """A key's new values cost a transaction and a commit each, about half of a new key's mask: an applications call
    (``holding(scoped=True)``) keeps its own in one (the store's ``together``), committed as the call ends; what another
    call kept before is used as it is. A call of the gateway (``holding()``) keeps each one at once, as before."""
    store = _Store()
    vault = _vault(store, working=1_000)
    engine = _engine(vault)
    before = vault.intern("EMAIL", EntityClass.PII, _email(0))  # kept at once: no call keeps it together
    assert store.rows == {before.id: before} and store.commits == 0
    with vault.holding(scoped=True):
        vault.prefetch(CALL)
        masked = [engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False)).text for text in CALL]
        assert list(store.rows) == [before.id] and store.commits == 0  # nothing kept before the call ends
    assert store.commits == 1 and len(store.rows) == 1 + 29
    assert all(_email(n) not in text for n, text in enumerate(masked)) and before.surrogate in masked[0]
    with vault.holding():  # the gateway's
        gateway = vault.intern("EMAIL", EntityClass.PII, _email(100))
        assert gateway.id in store.rows
    assert store.commits == 1


@pytest.mark.parametrize("failing", ["call", "commit"])
def test_an_applications_call_that_fails_keeps_none_of_its_new_values_and_forgets_them(failing):
    """A call that ends in an error, or whose values the store does not commit, keeps none of them, and memory forgets
    them, so that no text is ever masked with a fake the store does not keep (its unmask would find nothing): a commit
    that fails fails the call (``VaultUnread``), and memory no longer vouches for a text masked before. The next call
    keeps them, with the fakes they would have had."""
    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    old = vault.intern("EMAIL", EntityClass.PII, _email(0))
    version = vault.version
    text = f"{_email(0)} wrote to {_email(1)}"
    seen: list[str] = []

    def call() -> None:
        with vault.holding(scoped=True):
            vault.prefetch([text])
            seen.append(engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False)).text)
            if failing == "call":
                raise RuntimeError("the call failed")
            store.refuse_commit = True

    with pytest.raises(RuntimeError if failing == "call" else MaskingError):
        call()
    store.refuse_commit = False
    [masked] = seen
    new = vault.entity_id("EMAIL", _email(1))
    assert old.surrogate in masked and _email(1) not in masked
    assert list(store.rows) == [old.id] and store.commits == 0
    assert new not in {e.id for e in vault.entities()} and vault.by_original(_email(1)) is None
    # The rule of a value kept before, learnt in the call, is kept at once, as the store keeps a rule (review M7): the
    # value stays in memory as it is.
    assert _held(vault, old) and store.rows[old.id].rule is not None
    found = vault.by_original(_email(0))
    assert found is not None and (found.id, found.surrogate, found.rule) == (
        old.id,
        old.surrogate,
        store.rows[old.id].rule,
    )
    assert not vault.holds_since(version) and not vault.kept_since(version)
    # Masked again, not as it was masked in the call that failed (its fake kept nowhere): its value kept now, with the
    # fake it would have had.
    with vault.holding(scoped=True):
        vault.prefetch([text])
        again = engine.mask_text(text, SlotKind.TOOL_RESULT, Session("t", kept=False)).text
    assert again == masked and store.commits == 1 and store.rows[new].surrogate in again


def test_values_the_store_forgot_leave_memory_at_once_and_a_reading_begun_before_does_not_bring_them_back():
    """Task 11b: the values its store forgot (``PgVaultStore.forget_values``) leave memory in one step, with no call to
    the store, whether memory held them or not; the version moves once; and a reading of the store that began before
    (another thread's call that found them there) does not put them back."""
    store = _Store()
    vault = _vault(store)
    vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@gmail.com")
    gone = vault.intern("PERSON", EntityClass.NER, NAME, rule="ner")
    _many(vault)
    held = vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@gmail.com")  # used again now: in memory
    assert _held(vault, held) and not _held(vault, gone)
    since = vault._forgets  # what a reading that began now remembers
    version = vault.version
    for entity_id in (held.id, gone.id):
        store.forget_entity(entity_id)
    store.calls.clear()
    assert vault.forgotten([held.id, gone.id, held.id]) == 1  # how many it held
    assert store.calls == [] and vault.version == version + 1
    assert not _held(vault, held) and vault.by_original("laura.bianchi@gmail.com") is None
    with vault._lock:
        vault._join([held, gone], since)  # a reading begun before they were forgotten, ending now
    assert not _held(vault, held) and not _held(vault, gone)
    assert vault.forgotten([]) == 0 and vault.version == version + 1


def test_new_fakes_are_drawn_in_the_stores_generation():
    """Task 11b: a vault draws a value's fake from the value, in its store's generation (moved by each forget of values
    by themselves): generation 0 draws the fakes Latet always drew, a later one others, the same on every instance in
    the same generation, so that a value forgotten and met again gets a new fake; and a new id (Ruling 11b-R1), so
    that nothing kept under the old one (a conversation's, the history's) names the value met again."""
    value = "laura.bianchi@gmail.com"
    plain = Vault(ROOT_KEY).intern("EMAIL", EntityClass.PII, value)
    store = _Store()
    vault = _vault(store)
    first = vault.intern("EMAIL", EntityClass.PII, value)
    assert (first.surrogate, first.id) == (plain.surrogate, plain.id)  # as it always was
    store.forget_entity(first.id)
    vault.forgotten([first.id], 1)  # the generation the forget moved the store to: drawn in at once (review 11b's M1)
    assert store.generation == 1
    vault.forgotten([], 0)  # an older one, heard late: never back
    assert store.generation == 1
    again = vault.intern("EMAIL", EntityClass.PII, value)
    assert again.surrogate != first.surrogate and again.id != first.id
    elsewhere = _Store()
    elsewhere.generation = 1
    there = _vault(elsewhere).intern("EMAIL", EntityClass.PII, value)
    assert (there.surrogate, there.id) == (again.surrogate, again.id)  # the same on every instance
    elsewhere = _Store()
    elsewhere.generation = 2
    assert _vault(elsewhere).intern("EMAIL", EntityClass.PII, value).id not in (first.id, again.id)


def test_a_value_kept_again_with_another_fake_elsewhere_drops_the_masked_texts():
    """Task 11b: a value forgotten on one instance and kept again there, with a fake of the new generation, before this
    instance heard of the forget (it finds the entry there still, by the id the value gives, and drops nothing): learnt
    in place of the one it held, its masked texts are dropped too (``spaces._learned`` says so), as they may hold the old
    fake. The same entry learnt again drops nothing."""
    import dataclasses

    from latet.server.spaces import _learned

    store = _Store()
    vault = _vault(store)
    held = vault.intern("EMAIL", EntityClass.PII, "laura.bianchi@gmail.com")
    assert _learned(vault, held) is False  # the same: nothing to drop
    again = dataclasses.replace(held, surrogate="giada.ferri@gmail.example", counter=0)
    store.rows[held.id] = again
    assert _learned(vault, again) is True
    assert vault.by_original("laura.bianchi@gmail.com").surrogate == "giada.ferri@gmail.example"
    assert vault.by_surrogate(held.surrogate) is None
