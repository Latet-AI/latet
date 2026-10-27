"""0.9.10's fakes that never run out, in an API key's vault: a key's ``StoredVault`` holds few of its values in memory,
so it is the store (``PgVaultStore``) that refuses a fake another value has, and each value's wider forms are drawn
once the narrower ones are taken there, within the 80 fakes a store may refuse for one value (``vault._REFUSED_MOST``).
The three forms 0.9.10 found running out (Task 10's F1): public IPv4 addresses, one-word and first.last mailbox
addresses at a public mail provider."""

import ipaddress
import random
import string
from collections import Counter

import pytest

from latet.engine import vault as engine_vault
from latet.engine.stored import StoredVault
from latet.engine.types import EntityClass
from latet.engine.vault import FakeTaken
from latet.server.vault import PgVaultStore

from ..conftest import ROOT_KEY

pytestmark = pytest.mark.server

WORKING = 100  # few of the vault's values in memory: the store's refusals keep its fakes apart


def _words(n: int, seed: int, make) -> list[str]:
    """``n`` distinct values made by ``make`` from a generator of random words of 4 to 9 letters (seeded), as
    tests/unit/test_surrogates.py makes them."""
    rng = random.Random(seed)

    def word() -> str:
        return "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(4, 9)))

    found: dict[str, None] = {}
    while len(found) < n:
        found[make(word)] = None
    return list(found)


def _public_ipv4(n: int) -> list[str]:
    rng, found = random.Random(7), {}
    while len(found) < n:
        address = ipaddress.IPv4Address(rng.getrandbits(32))
        if address.is_global and not address.is_multicast:
            found[str(address)] = None
    return list(found)


CASES = {
    "public-ipv4": ("IP_ADDRESS", EntityClass.PII, lambda: _public_ipv4(2_000)),
    "one-word-gmail": ("EMAIL", EntityClass.PII, lambda: _words(1_000, 1, lambda word: f"{word()}@gmail.com")),
    "first-last-gmail": (
        "EMAIL",
        EntityClass.PII,
        lambda: _words(5_000, 2, lambda word: f"{word()}.{word()}@gmail.com"),
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_a_keys_vault_never_runs_out_of_fakes_and_its_store_keeps_each_one_apart(server, case):
    etype, eclass, values = CASES[case]
    values = values()
    key, _ = server.identities.add_key("Billing CI")
    store = PgVaultStore(server.db, key.id, ROOT_KEY)
    refused: Counter[str] = Counter()  # by value: the fakes the store refused it

    def save(entity):
        try:
            return store.save_entity(entity)
        except FakeTaken:
            refused[entity.id] += 1
            raise

    vault = StoredVault(ROOT_KEY, store, working=WORKING, on_insert=save)
    fakes = [vault.intern(etype, eclass, value).surrogate for value in values]  # a MaskingError fails the test
    assert len(set(fakes)) == len(values) and not set(fakes) & set(values)
    assert vault.count <= WORKING + WORKING // 10 and store.count_entities() == len(values)
    assert (
        refused and max(refused.values()) < engine_vault._REFUSED_MOST
    )  # the store did refuse: memory alone could not
    if etype == "IP_ADDRESS":
        assert not any(ipaddress.ip_address(fake).is_global for fake in fakes)


def test_a_store_that_refuses_every_fake_is_asked_no_more_than_80_times_by_a_keys_vault(server):
    """A store that refuses every fake (each refusal a round trip to PostgreSQL): a key's vault asks it 80 times at
    most for one value, whatever its wider forms hold, and fails the value then."""
    key, _ = server.identities.add_key("Billing CI")
    store = PgVaultStore(server.db, key.id, ROOT_KEY)
    asked: list[str] = []

    def refuse(entity):
        asked.append(entity.surrogate)
        raise FakeTaken(f"the fake made for a {entity.etype} is another value's")

    vault = StoredVault(ROOT_KEY, store, working=WORKING, on_insert=refuse)
    with pytest.raises(engine_vault.MaskingError, match="no collision-free surrogate"):
        vault.intern("IP_ADDRESS", EntityClass.PII, "8.8.4.4")
    assert 16 <= len(asked) <= engine_vault._REFUSED_MOST == 80
