"""A request is never refused for its size where the previous release masked it, on PostgreSQL with the index of
prints (the owner's ruling of 2026-10-29, Task 8's fix round 6; the Task 9 review's C1): a conversation that holds a
lockfile (``uv.lock``: 542 KB and some 196,000 lookup words, nearly all of them the interior windows of its hashes), a
mebibyte of hex in one run, a document of 150,000 distinct words; a person's space and an API key's, every later turn
of the conversation too (each request carries the whole conversation again). ``TooManyCandidates`` (422) stays where
0.10.2 had it, for an API key alone."""

import random
import secrets
from dataclasses import replace

import pytest

from latet.bridge.apps import Apps
from latet.engine import anchors, lookup
from latet.engine.lookup import LookupBudget
from latet.engine.rehydrate import restore_text, restores_for
from latet.engine.stored import StoredVault
from latet.engine.types import EntityClass, Policy
from latet.engine.vault import Vault
from latet.gateway.app import OnServer
from latet.server import vault as pg

from ..conftest import ROOT_KEY
from ..unit.test_call_joined import _mediator_over
from ..unit.test_interior_lookup import DATA, masker
from .test_key_vault import APPS_API, _bearer, _calling, _masked

pytestmark = pytest.mark.server

BIANCHI = "Laura.Bianchi@nordest.it"
TOKEN = "Kq7mZ2xW9pL4vR8tN3bY"  # an API key's secret, found by its word
SYSTEM = "You are a coding assistant. Answer briefly."


def lockfile(hashes: int = 1_850, seed: int = 11) -> str:
    """A lockfile shaped as ``uv.lock`` is: packages, each with its sdist and wheels, each a URL whose path holds 60 hex
    digits and a sha256 of 64, whose interior positions are as many lookup words. This repository's is 542,493
    characters, 18,787 words 0.10.x read and 195,923 lookup words in all; this one, 540,800, 18,447 and 196,198. A
    maintainer's address and a token are in it."""
    rng = random.Random(f"lockfile:{seed}")
    lines = ["version = 1", "revision = 2", 'requires-python = ">=3.13"', ""]
    written = 0
    package = 0
    while written < hashes:
        name = f"package-{package:04d}"
        version = f"{1 + package % 9}.{package % 13}.{package % 7}"
        lines += ["[[package]]", f'name = "{name}"', f'version = "{version}"']
        lines.append('source = { registry = "https://pypi.org/simple" }')
        digest = rng.getrandbits(256).to_bytes(32, "big").hex()
        lines.append(
            f'sdist = {{ url = "https://files.pythonhosted.org/packages/{digest[:2]}/{digest[2:4]}/{digest[4:]}/'
            f'{name}-{version}.tar.gz", hash = "sha256:{digest}", size = {rng.randint(10_000, 900_000)} }}'
        )
        written += 1
        lines.append("wheels = [")
        for tag in ("py3-none-any", "cp313-cp313-win_amd64", "cp313-cp313-manylinux_2_17_x86_64"):
            digest, path = (rng.getrandbits(256).to_bytes(32, "big").hex() for _ in range(2))
            lines.append(
                f'    {{ url = "https://files.pythonhosted.org/packages/{path[:2]}/{path[2:4]}/{path[4:]}/{name}-'
                f'{version}-{tag}.whl", hash = "sha256:{digest}", size = {rng.randint(10_000, 900_000)} }},'
            )
            written += 1
        lines += ["]", ""]
        if package == 40:
            lines += [f'# maintainer = "{BIANCHI}"', f'# token = "{TOKEN}"', ""]
        package += 1
    return "\n".join(lines)


def _turns(tool_output: str) -> list[list[str]]:
    """Three requests of one conversation, each with the whole conversation so far (as an agent sends it)."""
    first = [SYSTEM, f"Please check the lockfile; questions to {BIANCHI}", tool_output]
    second = [*first, "The lockfile pins 875 packages.", "Which of them pull a native wheel?"]
    third = [*second, "Three of them do.", f"Thanks. Write to {BIANCHI} with the list."]
    return [first, second, third]


@pytest.fixture
def stores(server):
    """A person's store and an API key's, each with its index of prints built, and the values they hold."""
    person, _ = server.identities.add_person("Emily Carter")
    key, _ = server.identities.add_key("Lockfile CI")
    source = Vault(ROOT_KEY)
    values = [
        source.intern("EMAIL", EntityClass.PII, BIANCHI),
        source.intern("API_KEY", EntityClass.SECRET, TOKEN),
    ]
    made = {}
    for name, identity in (("person", person), ("key", key)):
        store = pg.PgVaultStore(server.db, identity.id, ROOT_KEY, index=True)
        for entity in values:
            assert store.save_entity(entity) is None
        assert store.build_index()
        made[name] = store
    try:
        yield source, made
    finally:
        for store in made.values():
            store.close()


def _person(store):
    return StoredVault(ROOT_KEY, store, working=5000, held_at_most=1000, glued_all=True, whole_past_bounds=True)


def _key(store):
    return StoredVault(ROOT_KEY, store, working=5000, held_at_most=1000)


def _gateway_turn(vault, texts):
    """One request of a person's conversation, as the gateway masks it: one call, the words of every text asked
    first, then each text masked (``Mediator``: ``holding``, ``prefetch``, the masker)."""
    reader = masker(vault, Policy.MASK)
    with vault.holding():
        vault.prefetch(texts)
        return [reader.mask(text, DATA).text for text in texts]


def _restored(vault, texts):
    out = []
    with vault.holding():
        vault.prefetch_fakes(texts)
        for text in texts:
            out.append(restore_text(text, restores_for(vault.candidates_in(text), parts=False, whole_vault=True))[0])
    return out


def test_the_lockfile_is_past_what_the_windows_may_ask():
    plain, windows = anchors.words_and_windows(lockfile(), dotted=True, interior=LookupBudget().interior)
    assert len(plain) + len(windows) > 190_000 and len(windows) > lookup.LOOKUP_WORD_ROLES
    assert len(plain) < lookup.LOOKUP_WORD_ROLES
    assert 500_000 < len(lockfile()) < 800_000


def test_a_persons_conversation_holding_a_lockfile_is_masked_every_turn_as_0_10_0_masked_it(stores):
    """The Task 9 review's no-refusal oracle: each turn masked as 0.10.0's whole ``Vault`` masks it, nothing refused."""
    source, made = stores
    vault = _person(made["person"])
    for texts in _turns(lockfile()):
        done = _gateway_turn(vault, texts)
        assert done == [masker(source, Policy.MASK).mask(text, DATA).text for text in texts]
        assert all(BIANCHI not in text and TOKEN not in text for text in done)
        assert _restored(vault, done) == texts


def test_a_persons_vault_held_whole_masks_the_lockfile_conversation_every_turn_as_0_10_0_masked_it(stores, monkeypatch):
    """The same oracle on a person's vault small enough to be held whole (the owner's ruling of 2026-10-31): past every
    bound of the lookups, memory answers, nothing is refused, nothing read whole, and PostgreSQL is asked no text's
    words."""
    source, made = stores
    store = made["person"]
    vault = StoredVault(
        ROOT_KEY, store, working=5000, held_at_most=1000, glued_all=True, whole_past_bounds=True, whole_at_most=35_000
    )
    assert vault.holds_all
    asked: list[str] = []
    for name in ("bounded_words", "with_words", "whole_entities"):
        real = getattr(store, name)
        monkeypatch.setattr(store, name, lambda *a, real=real, name=name, **k: asked.append(name) or real(*a, **k))
    for texts in _turns(lockfile()):
        done = _gateway_turn(vault, texts)
        assert done == [masker(source, Policy.MASK).mask(text, DATA).text for text in texts]
        assert all(BIANCHI not in text and TOKEN not in text for text in done)
        assert _restored(vault, done) == texts
    assert asked == [] and vault.whole_count == 0


def test_an_api_keys_calls_holding_a_lockfile_are_masked_and_restored_every_turn(stores):
    _source, made = stores
    vault = _key(made["key"])
    apps = Apps(_mediator_over(vault), OnServer("https://latet.invalid", "k_round6", kind="key"))
    for texts in _turns(lockfile()):
        _, out, _, _ = apps._mask(texts, DATA)
        masked = [item["text"] for item in out]
        assert all(BIANCHI not in text and TOKEN not in text for text in masked)
        assert _restored(vault, masked) == texts


@pytest.mark.parametrize("who", ["person", "key"])
def test_a_mebibyte_of_hex_in_one_run_is_masked(stores, who):
    source, made = stores
    vault = _person(made[who]) if who == "person" else _key(made[who])
    run = secrets.token_hex(1 << 19)  # 1 MiB of hex, one run: a million interior positions and more
    texts = [f"dump: {run}", f"questions to {BIANCHI}, key {TOKEN}"]
    done = _gateway_turn(vault, texts)
    assert done[0] == texts[0]
    assert BIANCHI not in done[1] and TOKEN not in done[1]
    if who == "person":
        assert done == [masker(source, Policy.MASK).mask(text, DATA).text for text in texts]
    assert _restored(vault, done) == texts


def test_a_document_of_150000_distinct_words_is_masked_for_a_person_from_the_whole_vault(stores, monkeypatch):
    source, made = stores
    store = made["person"]
    vault = _person(store)
    rng = random.Random("distinct")
    words = [f"w{n:06d}{rng.choice('abcdefghijklmnopqrstuvwxyz')}" for n in range(150_000)]
    lines = [" ".join(words[at : at + 12]) + "." for at in range(0, len(words), 12)]
    lines.insert(len(lines) // 2, f"Questions to {BIANCHI}; the deploy key is {TOKEN}.")
    document = "\n".join(lines)
    wholes = []
    load, read = store.load_entities, store.whole_entities

    def loaded():
        wholes.append("load_entities")
        return load()

    def whole():
        wholes.append("whole_entities")
        return read()

    monkeypatch.setattr(store, "load_entities", loaded)
    monkeypatch.setattr(store, "whole_entities", whole)
    reached, snapshots = [], []
    shared = vault._whole_read

    def whole_read():
        reached.append(True)
        return shared()

    monkeypatch.setattr(vault, "_whole_read", whole_read)
    for _turn in range(2):
        [done] = _gateway_turn(vault, [document])
        assert done == masker(source, Policy.MASK).mask(document, DATA).text
        assert BIANCHI not in done and TOKEN not in done
        snapshots.append(vault._shared_whole)
    # Past the word roles, each request was masked from the whole vault, none refused (the review of the merge of Task
    # 9, M3: each reached it): read once through the reader that changes nothing, and its snapshot shared by the next
    # request, brought up to what the store's log named since (Task 9's shared whole read: the same base, no read).
    assert len(reached) == 2
    assert snapshots[0] is not None and snapshots[1] is not None and snapshots[1].base is snapshots[0].base
    assert wholes == ["whole_entities"]


async def test_too_many_candidates_stays_where_0_10_2_had_it_and_the_lookup_bounds_refuse_nothing(server, monkeypatch):
    """An API key's texts whose words name more values than ``CANDIDATES_AT_MOST`` are refused with 422, as in
    0.10.2; past Task 8's own bounds (candidates, characters, bytes, word roles, windows) they are masked."""
    _, secret = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        roles = ("info", "sales", "admin", "support", "office")  # mailboxes of roles: found by their domain's name
        await _masked(client, secret, [f"write to {role}@lanzavecchia.it" for role in roles])
        body = {"texts": ["ask lanzavecchia about the renewal"]}
        for ceiling in (
            "LOOKUP_CANDIDATES",
            "LOOKUP_CHARACTERS",
            "LOOKUP_BYTES",
            "LOOKUP_WORD_ROLES",
            "INTERIOR_WINDOWS",
        ):
            with monkeypatch.context() as limited:
                limited.setattr(lookup, ceiling, 1)
                for operation in ("mask", "scan"):
                    answer = await client.post(f"{APPS_API}/{operation}", json=body, headers=_bearer(secret))
                    assert answer.status_code == 200, (ceiling, operation, answer.text)
        monkeypatch.setattr(pg, "CANDIDATES_AT_MOST", 3)
        for operation in ("mask", "scan"):
            answer = await client.post(f"{APPS_API}/{operation}", json=body, headers=_bearer(secret))
            assert answer.status_code == 422, operation
            assert answer.json() == {
                "error": "the texts of this call hold the words of more than 3 values or fakes of this vault, more than "
                "one call may weigh: nothing of it was masked or restored"
            }
            assert "lanzavecchia" not in answer.text


def test_a_persons_whole_vault_read_past_a_bound_is_read_again_once_the_call_sees_a_change(stores, monkeypatch):
    """A refresh inside a call (another instance's change seen) drops what the call read, its whole vault read past a
    bound too: a fake given anew elsewhere meanwhile is restored as it is now, never as the vault was read before."""
    _, made = stores
    store = made["person"]
    remote = pg.PgVaultStore(store.db, store.identity, ROOT_KEY)
    try:
        vault = _person(store)
        old = store.by_original(BIANCHI)
        assert old is not None
        monkeypatch.setattr(lookup, "LOOKUP_CANDIDATES", 1)
        with vault.holding():
            vault.prefetch([f"questions to {BIANCHI}, key {TOKEN}"])  # past the bound: the whole vault read
            assert vault._call().lookup.past and vault._call().whole is not None
            anew = replace(old, surrogate="g.ferri@posta-test.example", counter=1)
            assert remote.replace_fake(old, anew) is None
            assert vault.refresh()
            text = f"write to {anew.surrogate}"
            vault.prefetch_fakes([text])
            restored = restore_text(text, restores_for(vault.candidates_in(text), parts=False, whole_vault=True))
        assert restored[0] == f"write to {BIANCHI}"
    finally:
        remote.close()
