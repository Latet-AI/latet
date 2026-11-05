"""P2-3 authoritative opaque metadata across actual Valkey connections and server instances."""

from __future__ import annotations

import hashlib
import json

import pytest
from redis.exceptions import OutOfMemoryError

from latet.gateway.provenance import ECHO_RULE, EchoCheck, EchoScope, OpaqueCapture
from latet.gateway.routes import Upstream
from latet.server.live import (
    ECHO_STORED_BASE,
    ECHO_STORED_ENTRY,
    ECHO_STORED_IDENTITY,
    GIVEN_TTL,
    ValkeyEchoes,
)

from ..conftest import ROOT_KEY
from .support import listening

pytestmark = pytest.mark.server


def members(count):
    return tuple(hashlib.sha256(str(index).encode()).digest() for index in range(count))


def test_provenance_is_authoritative_across_connections_restart_and_expiry(buses):
    clock = [100.0]
    a = ValkeyEchoes(buses[0], "u_opaque", ROOT_KEY, clock=lambda: clock[0])
    b = ValkeyEchoes(buses[1], "u_opaque", ROOT_KEY, clock=lambda: clock[0])
    first = members(1)
    assert a.keep(b"c" * 32, first)
    assert b.read(first)[0] == set(first)
    a.close()
    restarted = ValkeyEchoes(buses[0], "u_opaque", ROOT_KEY, clock=lambda: clock[0])
    assert restarted.read(first)[0] == set(first)
    clock[0] += GIVEN_TTL + 1
    assert b.read(first)[0] == set()  # front cannot grant authority after authoritative expiry
    assert not buses[0].client.exists(*a._keys)
    for owner in (b, restarted):
        owner.close()
        assert owner.retained_bytes == 0


def test_a_refused_valkey_write_records_nothing_and_counters_remain_exact(bus):
    one, two, three = members(3)
    owner = ValkeyEchoes(bus, "u_opaque", ROOT_KEY, members=2)
    assert owner.keep(b"c" * 32, (one, two))
    expected = ECHO_STORED_BASE + ECHO_STORED_IDENTITY + 2 * ECHO_STORED_ENTRY
    assert int(bus.client.get(owner._keys[3])) == expected
    assert not owner.keep(b"c" * 32, (three,))
    assert owner.read((one, two))[0] == {one, two}
    assert owner.read((three,))[0] == set()
    assert int(bus.client.get(owner._keys[3])) == expected
    owner.close()
    assert owner.retained_bytes == 0


def test_global_and_per_identity_byte_caps_charge_bookkeeping_and_refuse_one_beyond(bus):
    one, two = members(2)
    exact = ECHO_STORED_BASE + ECHO_STORED_IDENTITY + ECHO_STORED_ENTRY
    owner = ValkeyEchoes(bus, "u_one", ROOT_KEY, size=ECHO_STORED_IDENTITY + ECHO_STORED_ENTRY, total=exact)
    assert owner.keep(b"c" * 32, (one,))
    assert int(bus.client.get(owner._keys[3])) == exact
    assert not owner.keep(b"c" * 32, (two,))
    other = ValkeyEchoes(bus, "u_two", b"b" * 32, total=exact)
    assert not other.keep(b"c" * 32, (one,))
    assert other.read((one,))[0] == set()
    assert bus.client.hlen(owner._keys[0]) == 1
    owner.close()
    other.close()


def test_wrong_key_type_refuses_atomic_batch_without_record_or_counter_subset(bus):
    owner = ValkeyEchoes(bus, "u_opaque", ROOT_KEY)
    bus.client.set(owner._keys[1], b"wrong-type")
    assert not owner.keep(b"c" * 32, members(2))
    assert not bus.client.exists(owner._keys[0], owner._keys[2], owner._keys[3])
    assert owner.retained_bytes == 0
    owner.close()


def test_oom_refusal_and_ambiguous_commit_never_publish_front_permission(bus, monkeypatch):
    owner = ValkeyEchoes(bus, "u_opaque", ROOT_KEY)
    original = owner._call

    def refused(mode, values):
        raise OutOfMemoryError("synthetic backend noeviction refusal")

    monkeypatch.setattr(owner, "_call", refused)
    assert not owner.keep(b"c" * 32, members(1))
    assert owner.retained_bytes == 0
    assert owner.read(members(1))[0] == set()
    assert not bus.client.exists(*owner._keys)

    def ambiguous(mode, values):
        original(mode, values)
        raise TimeoutError("commit completed but reply was lost")

    monkeypatch.setattr(owner, "_call", ambiguous)
    assert not owner.keep(b"c" * 32, members(1))
    assert owner.retained_bytes == 0
    monkeypatch.setattr(owner, "_call", original)
    assert owner.read(members(1))[0] == set(members(1))
    owner.close()


def test_a_failed_provenance_read_falls_to_the_rule_or_blocks(bus, monkeypatch):
    owner = ValkeyEchoes(bus, "u_opaque", ROOT_KEY)

    def failed(*args):
        raise ConnectionError("synthetic read failure")

    monkeypatch.setattr(owner, "_call", failed)
    encrypted = {"input": [{"type": "reasoning", "encrypted_content": "YWJj"}]}
    fixed = EchoCheck(ROOT_KEY, owner, EchoScope(("fixed",), "responses", True), "session", encrypted)
    assert list(fixed.rules(encrypted).values()) == ["provider encrypted field (1)"]
    company = EchoCheck(ROOT_KEY, owner, EchoScope(("company",), "responses", False), "session", encrypted)
    assert company.rules(encrypted) == {}
    image = {"input": [{"type": "image_generation_call", "result": "YWJj"}]}
    assert EchoCheck(ROOT_KEY, owner, EchoScope(("fixed",), "responses", True), "session", image).rules(image) == {}
    owner.close()


def test_provenance_keys_hold_keyed_hashes_alone(bus):
    canary = "OpaqueCanarySecretNeverStoredWithOriginalOrFake"
    body = {"input": [{"type": "image_generation_call", "result": canary}]}
    owner = ValkeyEchoes(bus, "u_opaque", ROOT_KEY)
    check = EchoCheck(
        ROOT_KEY, owner, EchoScope(("destination-canary", "profile"), "responses"), "conversation-canary", body
    )
    captured = OpaqueCapture(check, owner)
    captured.body({"output": body["input"]})
    assert captured.commit()
    raw = b"".join(bus.client.dump(key) or b"" for key in owner._keys)
    for value in (canary, "destination-canary", "conversation-canary"):
        assert value.encode() not in raw
    fields = bus.client.hkeys(owner._keys[0])
    assert fields and all(len(value) == 129 and value[64:65] == b":" for value in fields)
    assert all(float(value) > 0 for value in bus.client.hvals(owner._keys[0]))
    owner.close()


async def test_an_echo_seen_by_one_instance_passes_on_another(instances, monkeypatch):
    a, b = instances
    person, _ = a.identities.add_person("Opaque Example")
    route = Upstream("company", "/company", "https://company.example.invalid")
    headers = {"session-id": "opaque-conversation", "content-type": "application/json"}
    original = {"model": "gpt-4o", "input": "hello"}
    echoed = {"model": "gpt-4o", "input": [{"type": "image_generation_call", "id": "ig_1", "result": "YWJj"}]}
    async with listening(a, b):
        first, second = await a.spaces.runtime(person), await b.spaces.runtime(person)
        monkeypatch.setattr(a.bus, "publish", lambda *args, **kwargs: None)
        prepared = await first.mediator.run(
            first.mediator.prepare, route, "POST", "/company/v1/responses", headers, json.dumps(original).encode()
        )
        captured = first.mediator.opaque_capture(prepared)
        assert captured is not None
        await first.mediator.run(captured.body, {"output": echoed["input"]})
        assert await first.mediator.run(captured.commit)
        replay = await second.mediator.run(
            second.mediator.prepare, route, "POST", "/company/v1/responses", headers, json.dumps(echoed).encode()
        )
        assert replay.block_reason is None and ECHO_RULE in replay.body_rules
        for item in (prepared, replay):
            if item.lease is not None:
                item.lease.release()


def test_native_metadata_memory_is_covered_including_duplicate_index_keys(bus):
    owner = ValkeyEchoes(bus, "u_memory", ROOT_KEY)
    samples = []
    for count in (1, 8, 64, 256):
        assert owner.keep(b"c" * 32, members(count))
        actual = sum(bus.client.memory_usage(key) or 0 for key in owner._keys)
        charged = int(bus.client.get(owner._keys[3]))
        samples.append({"entries": count, "native_bytes": actual, "charged": charged})
        assert actual <= charged
    print(json.dumps({"native_opaque_metadata": samples}))
    owner.close()


@pytest.mark.parametrize("corruption", ["total", "orphan-identity"])
def test_corrupt_numeric_counter_refuses_without_partial_mutation(bus, corruption):
    owner = ValkeyEchoes(bus, "u_corrupt", ROOT_KEY)
    if corruption == "total":
        assert owner.keep(b"c" * 32, members(1))
        bus.client.set(owner._keys[3], "0")
    else:
        bus.client.hset(
            owner._keys[2],
            mapping={owner._owner + ":c": "1", owner._owner + ":b": str(ECHO_STORED_ENTRY + ECHO_STORED_IDENTITY)},
        )
    before = [bus.client.dump(key) for key in owner._keys]
    assert not owner.keep(b"c" * 32, members(2))
    assert owner.read(members(1))[0] == set()
    assert [bus.client.dump(key) for key in owner._keys] == before
    owner.close()


def test_expired_storage_stays_charged_until_bounded_pruning(bus):
    tick = [100.0]
    owner = ValkeyEchoes(bus, "u_expired", ROOT_KEY, clock=lambda: tick[0])
    values = members(1030)
    assert owner.keep(b"c" * 32, values)
    total = int(bus.client.get(owner._keys[3]))
    tick[0] += GIVEN_TTL + 1
    new = hashlib.sha256(b"new").digest()
    assert not owner.keep(b"c" * 32, (new,))  # beyond the bounded oldest-expiry batch, no mutation
    assert int(bus.client.get(owner._keys[3])) == total
    assert owner.read((new,))[0] == set()
    assert bus.client.hlen(owner._keys[0]) == 6
    assert int(bus.client.get(owner._keys[3])) == ECHO_STORED_BASE + ECHO_STORED_IDENTITY + 6 * ECHO_STORED_ENTRY
    assert owner.read(values[-6:])[0] == set()
    assert not bus.client.exists(*owner._keys)
    owner.close()
