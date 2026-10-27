"""An API key's own limit of values in its vault: an administrator sets it (PATCH, 1,000 to 100,000,000 values; null the
server's), above or below the server's, and it reaches the key's space on every instance within the beat; a full vault
refuses the call that would add a value to it (507) and lets nothing of the call out, in clear or masked; the status
warns at ninety per cent of each identity's own limit. A person has none of their own."""

import asyncio
import contextlib
import logging
import time

import httpx
import pytest

from latet.bridge.apps import APPS_API
from latet.engine.types import EntityClass
from latet.engine.vault import Entity
from latet.server.app import create_server_app
from latet.server.master import root_key
from latet.server.vault import PgVaultStore, vault_usage

from .support import URL, captured_logs, listening, until

pytestmark = pytest.mark.server

ADMIN = "/_latet/server/v1/admin"
CANARY = "mario.rossi@acme.it"
WHERE = ("10.0.3.9", 40000)
FULL = "this API key's vault is full: its limit is {} values; latet server keys limit raises it"


def _email(n: int) -> str:
    """A mail provider's address: no domain of its own to keep, so one value in the vault."""
    return f"customer.number{n:05d}@gmail.com"


def _texts(start: int, stop: int) -> list[str]:
    return [f"login by {_email(n)}" for n in range(start, stop)]


def _fill(server, key, count: int) -> None:
    """``count`` values in the key's vault, sealed with its root key and kept as its space keeps them (``_email(0)`` to
    ``_email(count - 1)``, each with a fake of its own), without a request: a vault of a thousand values a call at a
    time takes fourteen seconds."""
    store = PgVaultStore(server.db, key.id, root_key(server.db, server.master, key.id), limit=count + 1)
    for n in range(count):
        fake = f"fake.number{n:05d}@posta.example"
        assert (
            store.save_entity(Entity(f"e{n:019d}", "EMAIL", EntityClass.PII, _email(n), fake, 0, time.time())) is None
        )


@contextlib.asynccontextmanager
async def _calling(*servers):
    """A client for each instance, its ear on Valkey while it lasts, its spaces closed after."""
    async with listening(*servers), contextlib.AsyncExitStack() as stack:
        clients = []
        for server in servers:
            transport = httpx.ASGITransport(app=create_server_app(server), client=WHERE)
            clients.append(await stack.enter_async_context(httpx.AsyncClient(transport=transport, base_url=URL)))
        yield clients[0] if len(clients) == 1 else clients


def _administrator(server):
    return {"x-latet-admin": server.admin.issue("first administrator")}


async def _mask(client, secret, texts):
    return await client.post(
        f"{APPS_API}/mask", json={"texts": texts, "kind": "prose"}, headers={"authorization": f"Bearer {secret}"}
    )


async def _limit(client, administrator, identity_id, limit):
    return await client.patch(f"{ADMIN}/identities/{identity_id}", json={"vault_limit": limit}, headers=administrator)


async def _read(client, administrator, identity_id):
    answer = await client.get(f"{ADMIN}/identities/{identity_id}", headers=administrator)
    assert answer.status_code == 200
    return answer.json()["identity"]


def _counted(server, identity_id, count: int) -> None:
    """``count`` rows in the identity's vault, to be counted: nothing opens them, and nothing here opens its space."""
    with server.db.connection() as conn:
        conn.execute(
            "INSERT INTO vault_entities (identity_id, id, etype, eclass, original, original_key, surrogate,"
            " surrogate_key, counter, created) SELECT %s, 'e' || lpad(n::text, 19, '0'), 'EMAIL', 0, ''::bytea,"
            " convert_to('o' || n, 'UTF8'), ''::bytea, convert_to('s' || n, 'UTF8'), 0, 0"
            " FROM generate_series(1, %s) AS n",
            (identity_id, count),
        )


def _in_the_vault(server, identity_id) -> int:
    with server.db.connection() as conn:
        return conn.execute("SELECT count(*) FROM vault_entities WHERE identity_id = %s", (identity_id,)).fetchone()[0]


def _room_of(server, identity_id) -> int:
    """The limit the key's space keeps to on this instance."""
    return server.spaces._spaces[identity_id].parts.vault.limit


async def test_a_key_limit_above_the_servers(server):
    server.settings = server.settings.model_copy(update={"vault_limit": 1000})  # the least a setting takes
    key, secret = server.identities.add_key("Billing CI")
    plain, plain_secret = server.identities.add_key("Support bot")
    _fill(server, plain, 1000)  # a key without a limit of its own holds what the server allows
    async with _calling(server) as client:
        administrator = _administrator(server)
        changed = await _limit(client, administrator, key.id, 2000)
        assert changed.status_code == 200 and changed.json()["identity"]["vault_limit"] == 2000
        # 1,500 values masked: the key's own limit, not the server's.
        assert (await _mask(client, secret, _texts(0, 1000))).status_code == 200
        assert (await _mask(client, secret, _texts(1000, 1500))).status_code == 200
        # The same traffic on a key without one stops at the server's.
        assert (await _mask(client, plain_secret, _texts(1000, 1500))).status_code == 507
        mine = await _read(client, administrator, key.id)
        theirs = await _read(client, administrator, plain.id)
    assert (mine["vault_limit"], mine["vault_values"]) == (2000, 1500)
    assert (theirs["vault_limit"], theirs["vault_values"]) == (None, 1000)


async def test_a_full_vault_answers_507_and_nothing_in_clear(server):
    key, secret = server.identities.add_key("Billing CI")
    _fill(server, key, 1000)
    with captured_logs(level="debug") as logs:
        async with _calling(server) as client:
            administrator = _administrator(server)
            assert (await _limit(client, administrator, key.id, 1000)).status_code == 200  # as many as it holds
            full = await _mask(client, secret, [f"write to {CANARY}", f"login by {_email(1)}"])
            known = await _mask(client, secret, [f"login by {_email(7)}"])  # a value it knows needs no room
    assert full.status_code == 507
    assert full.json() == {"error": FULL.format(1000)}  # the words of R9, and no text of the call
    assert CANARY not in full.text and _email(1) not in full.text
    assert known.status_code == 200 and "fake.number00007@posta.example" in known.text and _email(7) not in known.text
    assert _in_the_vault(server, key.id) == 1000  # the value that did not fit was not kept
    assert CANARY not in logs.getvalue()


async def test_a_limit_below_what_the_vault_holds_is_accepted_and_refuses_new_values(server):
    key, secret = server.identities.add_key("Billing CI")
    _fill(server, key, 1100)
    async with _calling(server) as client:
        administrator = _administrator(server)
        opened = await _mask(client, secret, _texts(1000, 1001))  # its space is open, at the server's limit
        assert opened.status_code == 200
        lowered = await _limit(client, administrator, key.id, 1000)
        assert lowered.status_code == 200  # not refused: the values are kept, the vault takes no more
        assert (lowered.json()["identity"]["vault_limit"], lowered.json()["identity"]["vault_values"]) == (1000, 1100)
        await until(lambda: _room_of(server, key.id) == 1000)
        refused = await _mask(client, secret, [f"write to {CANARY}"])
        known = await _mask(client, secret, [f"login by {_email(1000)}"])
        assert refused.status_code == 507 and refused.json() == {"error": FULL.format(1000)}
        assert known.status_code == 200  # every value it holds is still masked
        mine = await _read(client, administrator, key.id)
        assert (mine["vault_limit"], mine["vault_values"]) == (1000, 1100)  # more values than its limit: GET says so
        # The server's limit again: the vault has room.
        assert (await _limit(client, administrator, key.id, None)).json()["identity"]["vault_limit"] is None
        await until(lambda: _room_of(server, key.id) == server.settings.vault_limit)
        assert (await _mask(client, secret, [f"write to {CANARY}"])).status_code == 200


async def test_the_limit_reaches_both_instances(instances):
    a, b = instances
    key, secret = a.identities.add_key("Billing CI")
    async with _calling(a, b) as (first, second):
        administrator = _administrator(a)
        assert (await _mask(first, secret, ["hello"])).status_code == 200  # both spaces built before the change
        assert (await _mask(second, secret, ["hello"])).status_code == 200
        assert _room_of(a, key.id) == _room_of(b, key.id) == a.settings.vault_limit
        assert (await _limit(first, administrator, key.id, 5000)).status_code == 200
        await until(lambda: _room_of(a, key.id) == _room_of(b, key.id) == 5000, 10)
        assert (await _read(second, administrator, key.id))["vault_limit"] == 5000
        assert (await _limit(second, administrator, key.id, None)).status_code == 200
        await until(lambda: _room_of(a, key.id) == _room_of(b, key.id) == a.settings.vault_limit, 10)


async def test_a_limit_whose_announcement_was_lost_reaches_the_space_at_the_next_check(instances, monkeypatch):
    a, b = instances
    key, secret = a.identities.add_key("Billing CI")
    publish = a.bus.publish

    def lost(channel, message):  # Valkey takes no announcement of an identity: no instance hears the change
        if channel == "identities":
            raise OSError("the connection was lost")
        publish(channel, message)

    async with _calling(a, b) as (first, second):
        administrator = _administrator(a)
        await _mask(second, secret, ["hello"])
        monkeypatch.setattr(a.bus, "publish", lost)
        assert (await _limit(first, administrator, key.id, 5000)).status_code == 200
        await asyncio.sleep(0.3)  # a moment for what might be heard
        assert _room_of(b, key.id) == b.settings.vault_limit  # nobody heard it
        await b.check()
        await until(lambda: _room_of(b, key.id) == 5000, 10)


async def test_the_status_warns_at_ninety_per_cent_of_the_keys_own_limit(server):
    server.settings = server.settings.model_copy(update={"vault_limit": 10_000})
    named = {
        name: server.identities.add_key(name)[0].id for name in ("Over", "Near", "Under", "Roomy", "Default", "Few")
    }
    held = {"Over": 1200, "Near": 900, "Under": 899, "Roomy": 900, "Default": 9000, "Few": 8999}
    own = {"Over": 1000, "Near": 1000, "Under": 1000, "Roomy": 100_000}
    for name, count in held.items():
        _counted(server, named[name], count)
    async with _calling(server) as client:
        administrator = _administrator(server)
        for name, limit in own.items():
            assert (await _limit(client, administrator, named[name], limit)).status_code == 200

        async def near():
            status = (await client.get(f"{ADMIN}/status", headers=administrator)).json()["vault"]
            assert status["limit"] == 10_000
            return [(e["id"], e["values"], e["limit"]) for e in status["near"]]

        # Each against its own limit, the fullest first: the one over its limit, then the two at ninety per cent (the
        # more values first), and none of those under ninety, or far from a limit of its own that is higher.
        assert await near() == [
            (named["Over"], 1200, 1000),
            (named["Default"], 9000, 10_000),
            (named["Near"], 900, 1000),
        ]
        # The server's limit again for "Near": 900 of 10,000 is far from it.
        assert (await _limit(client, administrator, named["Near"], None)).status_code == 200
        assert await near() == [(named["Over"], 1200, 1000), (named["Default"], 9000, 10_000)]


def test_ninety_per_cent_of_a_limit_is_counted_exactly(server):
    """In binary, 0.7 of 1,290 is 902.9999999999999: the share is taken in decimals, for the server's limit and a key's."""
    plain, _ = server.identities.add_key("Billing CI")
    own, _ = server.identities.add_key("Support bot")
    other, _ = server.identities.add_key("Audit bot")
    server.identities.limit_vault(own.id, 1290)
    for key, count in ((plain, 902), (own, 903), (other, 902)):
        _counted(server, key.id, count)
    server.identities.limit_vault(other.id, 1290)
    assert vault_usage(server.db, 1290, share=0.7) == [(own.id, 903, 0, 1290)]  # 903 is the least: 902 is not near
    # 0.7 of 1,288 is 901.6: 902 is near, and the fuller share of its limit comes first.
    assert vault_usage(server.db, 1288, share=0.7) == [(plain.id, 902, 0, 1288), (own.id, 903, 0, 1290)]


def _retired(server, identity_id, count: int) -> None:
    """``count`` fakes retired in the identity's vault (forgotten values': rows of no entry, kept as long as the vault)."""
    with server.db.connection() as conn:
        conn.execute(
            "INSERT INTO vault_strings (identity_id, key, role) SELECT %s, convert_to('r' || n, 'UTF8'), 'retired'"
            " FROM generate_series(1, %s) AS n",
            (identity_id, count),
        )


async def test_retired_fakes_are_weighed_with_the_values_where_the_status_and_the_metric_say_how_full_a_vault_is(
    server,
):
    """The security re-review's R-L1: a forgotten value's fake stays retired as long as the vault, outside vault_limit,
    so a key that masks and forgets in a loop grows it for ever. The status and ``latet_vault_near_limit`` weigh the
    retired fakes with the values (they are no room in the vault: the limit counts values alone), and the status says
    how many of them each vault near its limit holds."""
    looping, _ = server.identities.add_key("Masks and forgets")
    plain, _ = server.identities.add_key("Plain")
    server.identities.limit_vault(looping.id, 1000)
    server.identities.limit_vault(plain.id, 1000)
    _counted(server, looping.id, 100)
    _retired(server, looping.id, 850)
    _counted(server, plain.id, 899)
    assert vault_usage(server.db, 10_000) == [(looping.id, 100, 850, 1000)]
    async with _calling(server) as client:
        status = (await client.get(f"{ADMIN}/status", headers=_administrator(server))).json()["vault"]
    assert status["near"] == [{"id": looping.id, "values": 100, "retired": 850, "limit": 1000}]


def test_the_limit_the_store_of_identities_takes_is_a_whole_number_in_range(server):
    key, _ = server.identities.add_key("Billing CI")
    emily, _ = server.identities.add_person("Emily Carter")
    for given in (999, 100_000_001, 5000.0, "5000", True):
        with pytest.raises(ValueError, match="1000 to 100000000"):
            server.identities.limit_vault(key.id, given)
    assert server.identities.limit_vault(key.id, 5000) is True
    assert server.identities.limit_vault(key.id, 5000) is False  # nothing changed, nothing told
    assert server.identities.limit_vault(emily.id, 5000) is False  # a person's vault is the server's
    assert server.identities.limit_vault("u_nobodyever", 5000) is False
    assert server.identities.limit_vault(key.id, None) is True
    assert server.identities.current(key.id).vault_limit is None
    assert server.identities.current(emily.id).vault_limit is None


# --- the administration: what a limit may be, who has one, and what is recorded -----------------------------------


@pytest.mark.parametrize(
    "given",
    [999, 0, -1000, 100_000_001, 1.5, 5000.0, "5000", True, False, [5000], {"n": 5000}],
    ids=repr,
)
async def test_a_limit_outside_its_range_is_refused_and_nothing_changes(server, given):
    key, _ = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        administrator = _administrator(server)
        refused = await _limit(client, administrator, key.id, given)
        assert refused.status_code == 400 and "1000" in refused.json()["error"]
        assert "100000000" in refused.json()["error"]  # the range, in its words
        assert (await _read(client, administrator, key.id))["vault_limit"] is None
    assert server.audit.query(action="key.updated") == []


@pytest.mark.parametrize("given", [1000, 100_000_000])
async def test_the_ends_of_the_range_are_limits(server, given):
    key, _ = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        answer = await _limit(client, _administrator(server), key.id, given)
    assert answer.status_code == 200 and answer.json()["identity"]["vault_limit"] == given


@pytest.mark.parametrize("given", [5000, None])
async def test_a_person_has_no_limit_of_their_own(server, given):
    emily, _ = server.identities.add_person("Emily Carter")
    async with _calling(server) as client:
        administrator = _administrator(server)
        refused = await _limit(client, administrator, emily.id, given)
        assert refused.status_code == 409 and "API key" in refused.json()["error"]
        person = await _read(client, administrator, emily.id)
    assert person["vault_limit"] is None and person["vault_values"] == 0
    assert server.identities.current(emily.id).vault_limit is None
    assert server.audit.query(action="person.updated") == []


async def test_the_change_is_recorded_with_the_limit_and_without_a_trace_when_it_changes_nothing(server):
    key, _ = server.identities.add_key("Billing CI")
    async with _calling(server) as client:
        administrator = _administrator(server)
        for given in (5000, 5000, None, None, 7000):
            assert (await _limit(client, administrator, key.id, given)).status_code == 200
        both = await client.patch(
            f"{ADMIN}/identities/{key.id}", json={"name": "Billing", "vault_limit": 8000}, headers=administrator
        )
        assert both.status_code == 200
    changes = [
        (e.details["vault_limit"], e.details["fields"]) for e in reversed(server.audit.query(action="key.updated"))
    ]
    assert changes == [
        (5000, ["vault_limit"]),
        (None, ["vault_limit"]),
        (7000, ["vault_limit"]),
        (8000, ["name", "vault_limit"]),
    ]  # the two that changed nothing are recorded as such: identity.change
    assert all(e.target == key.id for e in server.audit.query(action="key.updated"))
    nothing = [e for e in server.audit.query(action="identity.change") if e.details["status"] == 200]
    assert len(nothing) == 2


async def test_a_limit_and_its_entry_are_kept_together_or_not_at_all(server, monkeypatch, caplog):
    key, _ = server.identities.add_key("Billing CI")
    kept = server.audit.record

    def refused(actor, action, *args, conn=None, **kwargs):
        if conn is not None:  # an entry written last in its change's transaction
            raise RuntimeError("the log is gone")
        return kept(actor, action, *args, **kwargs)

    async with _calling(server) as client:
        administrator = _administrator(server)
        monkeypatch.setattr(server.audit, "record", refused)
        with caplog.at_level(logging.ERROR):
            failed = await _limit(client, administrator, key.id, 5000)
        monkeypatch.setattr(server.audit, "record", kept)
    assert failed.status_code == 500 and "identity" not in failed.json()
    assert server.identities.current(key.id).vault_limit is None


async def test_the_listing_and_the_one_identity_say_the_limit_and_only_the_one_the_values(server):
    key, _ = server.identities.add_key("Billing CI")
    server.identities.add_key("Support bot")
    async with _calling(server) as client:
        administrator = _administrator(server)
        await _limit(client, administrator, key.id, 3000)
        listed = (await client.get(f"{ADMIN}/identities?kind=key", headers=administrator)).json()["identities"]
        one = (await client.get(f"{ADMIN}/identities/{key.id}", headers=administrator)).json()
    assert sorted(i["vault_limit"] or 0 for i in listed) == [0, 3000]
    assert all("vault_values" not in i for i in listed)  # a count of every vault is no part of a listing
    assert one["identity"]["vault_limit"] == 3000 and one["identity"]["vault_values"] == 0
