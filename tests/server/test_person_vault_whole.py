"""A person's space on a Latet server whose vault is small enough to be held whole (``stored.WHOLE_VALUES``, the
owner's ruling of 2026-10-31): every value in memory from its build and its texts masked with no lookup of PostgreSQL
for each, a value kept or forgotten on another instance seen at its next call, every value weighed with its space,
and the vault on its store as any other once it holds more than its bound."""

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from latet.engine import stored as stored_vaults
from latet.engine.stored import StoredVault
from latet.engine.types import EntityClass
from latet.engine.vault import Entity
from latet.server import spaces as spaces_module
from latet.server import vault as pg
from latet.server.app import create_server_app

from .support import URL, listening
from .test_key_vault import upstream  # noqa: F401 - the stub model every space of a test reaches
from .test_person_vault import (
    NAME,
    NAME_FAKE,
    WORKING,
    _chat,
    _emails,
    _fake,
    _keep,
    _leaves,
    _runtime,
    _store,
    _token_of,
    _tool_output,
    _working,
)

pytestmark = pytest.mark.server

OTHER = "Clotilde Squarcialupi"  # another customer no detector of the tests reads
OTHER_FAKE = "Benedetta Moretti"


def _name(value: str, fake: str, n: int) -> Entity:
    return Entity(f"n{n:019d}", "PERSON", EntityClass.NER, value, fake, 0, time.time() - 3600 + n)


def _lookups(monkeypatch) -> list[str]:
    """Each lookup by words any space's store is asked from now on (``PgVaultStore.bounded_words``, ``with_words``)."""
    asked: list[str] = []
    for name in ("bounded_words", "with_words"):
        real = getattr(pg.PgVaultStore, name)

        def spied(self, *args, real=real, name=name, **kwargs):
            asked.append(name)
            return real(self, *args, **kwargs)

        monkeypatch.setattr(pg.PgVaultStore, name, spied)
    return asked


def test_a_small_persons_space_is_held_whole_and_masks_through_the_gateway_with_no_lookup(
    server,
    upstream,  # noqa: F811
    monkeypatch,
):
    """Its working set of 100 or not, a vault of 301 values is held whole: a name kept long ago is masked in a tool's
    output and in what the person writes, restored in the answer, and PostgreSQL is asked for no text's words."""
    _working(server)
    person, token = _token_of(server, "Emily Carter")
    _keep(server, person, [_name(NAME, NAME_FAKE, 0), *_emails(300)])
    asked = _lookups(monkeypatch)
    text = f"2026-10-24 10:02:11 ticket 4411 closed, customer {NAME}, rating 5"
    with TestClient(create_server_app(server), base_url=URL) as client:
        before = len(upstream.received)
        _chat(client, token, _tool_output(text))
        said = _chat(client, token, [{"role": "user", "content": f"write to {NAME} about the invoice"}])
        sent = b"".join(upstream.received[before:]).decode()
        vault = server.spaces._spaces[person.id].runtime.built.vault
        assert isinstance(vault, StoredVault) and vault.holds_all and vault.count == 301
    assert NAME not in sent and "Lanzavecchia" not in sent and sent.count(NAME_FAKE) == 2
    assert NAME in said and NAME_FAKE not in said
    assert asked == []


async def test_a_persons_space_held_whole_leaves_it_as_its_vault_grows_past_its_bound(server, monkeypatch):
    _working(server)
    monkeypatch.setattr(stored_vaults, "WHOLE_VALUES", 305)
    person, _ = server.identities.add_person("Emily Carter")
    _keep(server, person, [_name(NAME, NAME_FAKE, 0), *_emails(300)])
    async with listening(server):
        runtime = await _runtime(server, person)
        vault = runtime.built.vault
        assert isinstance(vault, StoredVault) and vault.holds_all
        new = " ".join(f"new.customer{n}@gmail.com" for n in range(10))
        await _leaves(runtime, f"invited {new}")
        assert not vault.holds_all and vault.count <= WORKING  # its working set once the call ended
        asked = _lookups(monkeypatch)
        assert await _leaves(runtime, f"customer {NAME} called") == f"customer {NAME_FAKE} called"
        assert asked  # out of memory now: found in PostgreSQL by its words


async def test_a_persons_space_past_its_bound_is_on_its_store(server, monkeypatch):
    _working(server)
    monkeypatch.setattr(stored_vaults, "WHOLE_VALUES", 200)
    person, _ = server.identities.add_person("Emily Carter")
    _keep(server, person, _emails(300))
    async with listening(server):
        vault = (await _runtime(server, person)).built.vault
        assert isinstance(vault, StoredVault) and not vault.holds_all and vault.count <= WORKING


async def test_two_instances_see_a_value_kept_and_one_forgotten_by_the_other_in_a_space_held_whole(instances):
    """Kept and forgotten in PostgreSQL with no message of either (as a message lost): the other instance's next call
    reads what changed, the new value by its name, the forgotten one in a whole read of the vault, as a vault on its
    store sees them."""
    a, b = instances
    _working(a, b)
    person, _ = a.identities.add_person("Emily Carter")
    _keep(a, person, _emails(300))
    async with listening(a, b):
        first, second = await _runtime(a, person), await _runtime(b, person)
        assert first.built.vault.holds_all and second.built.vault.holds_all
        other = _name(OTHER, OTHER_FAKE, 1)
        _keep(a, person, [other])  # kept elsewhere
        assert second.built.vault.in_memory(other.id) is None
        assert await _leaves(second, f"customer {OTHER} called") == f"customer {OTHER_FAKE} called"
        assert second.built.vault.in_memory(other.id) is not None
        store = _store(a, person)
        try:
            await asyncio.to_thread(store.forget_entity, other.id)  # forgotten elsewhere
        finally:
            store.close()
        # A text of its own: the one masked before is kept as it left (a lost message leaves the masked texts until a
        # catch-up, ``Spaces._resync``, as on a vault on its store; a forget's message drops them, ``spaces._forget``).
        assert await _leaves(second, f"{OTHER} wrote again") == f"{OTHER} wrote again"
        vault = second.built.vault
        assert vault.in_memory(other.id) is None and vault.holds_all and vault.count == 300
        restored, count = await asyncio.to_thread(second.built.engine.unmask, f"call {OTHER_FAKE}")
        assert (restored, count) == (f"call {OTHER_FAKE}", 0)


async def test_a_persons_space_held_whole_weighs_every_value(server):
    _working(server)
    person, _ = server.identities.add_person("Emily Carter")
    entities = _emails(300)
    _keep(server, person, entities)
    async with listening(server):
        runtime = await _runtime(server, person)
        vault = runtime.built.vault
        assert vault.holds_all and vault.count == 300
        chars = sum(max(len(e.original), len(e.surrogate)) for e in entities)
        values = spaces_module._PER_VALUE * 300 + spaces_module._PER_CHAR * chars
        assert spaces_module._values(vault) == values
        assert spaces_module.space_size(runtime) >= values
        assert server.spaces._spaces[person.id].size >= values  # weighed so as it was built
        assert _fake(0) in await _leaves(runtime, f"login by {entities[0].original}")
