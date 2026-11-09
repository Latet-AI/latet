"""P2-3 retained weighting through real Runtime/Spaces, without opening any backend namespace."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import psycopg
import pytest
import redis
from psycopg import sql

from latet.config import Config, IdentityConfig
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig
from latet.gateway.approvals import Batch
from latet.gateway.mediate import Mediator
from latet.gateway.provenance import (
    ECHO_CONVERSATION_BYTES,
    ECHO_ENTRY_BYTES,
    EchoCheck,
    EchoScope,
    LocalEchoes,
    OpaqueCapture,
)
from latet.gateway.request_audit import (
    BASE_BYTES,
    EVENT_RESERVE_BYTES,
    RESERVATION_BYTES,
    STATUS_BYTES,
    RequestAudit,
    SafeDecisionRecord,
    new_exception_reference,
)
from latet.projects import Projects
from latet.runtime import Runtime
from latet.server.backends import SpaceBackends
from latet.server.bus import redis_url
from latet.server.identities import Identity
from latet.server.live import ValkeyEchoes
from latet.server.spaces import Space, Spaces, _AuditFence, space_size
from latet.state.protocols import VaultStore

from ..conftest import ROOT_KEY
from .support import DATABASE, VALKEY, listening, open_test_server, scanned_once, until

if TYPE_CHECKING:
    from latet.server.app import Server

pytestmark = pytest.mark.server


@pytest.fixture
def retained_runtimes(tmp_path):
    """Real runtime/engine/mediator owners; only storage and future audit contribution are inert boundaries."""
    made = []
    for index in range(2):
        config = Config(identity=IdentityConfig(enabled=False))
        built = build(config, Vault(ROOT_KEY))
        mediator = Mediator(built.engine, built.verifier)
        runtime = Runtime(
            config=config,
            built=built,
            store=cast(VaultStore, SimpleNamespace(index_bytes=0)),
            gateway=GatewayConfig(port=5283, mediator=mediator),
            data=tmp_path / str(index),
        )
        # P2-5's future audit owner is the sole synthetic input. No fake weighting function is used.
        runtime.request_audit = SimpleNamespace(retained_bytes=0)
        made.append(runtime)
    try:
        yield made
    finally:
        for runtime in made:
            runtime.mediator.close()


def test_request_retained_owner_growth_changes_actual_space_size(retained_runtimes):
    """Omitting retained state from _fixed permits unbounded growth outside the memory budget."""
    runtime = retained_runtimes[0]
    baseline = space_size(runtime)
    runtime.request_audit.retained_bytes = 6144
    assert space_size(runtime) - baseline == 6144, "owned request state was omitted from the space weight"
    runtime.request_audit.retained_bytes = 0
    assert space_size(runtime) == baseline


def test_opaque_provenance_counts_toward_each_space_and_total_budget(retained_runtimes):
    """Each runtime's retained-owner input must reach the actual per-space and aggregate readings once."""
    spaces = Spaces(cast("Server", SimpleNamespace()))
    try:
        for index, runtime in enumerate(retained_runtimes):
            identity = cast(Identity, SimpleNamespace(id=f"u_test{index}"))
            space = Space(identity, runtime, None, cast(SpaceBackends, None), AsyncExitStack(), 0)
            spaces._spaces[identity.id] = space
            spaces._weighed(space)
        before = [space.size for space in spaces._spaces.values()]
        retained_runtimes[0].request_audit.retained_bytes = 2048
        retained_runtimes[1].request_audit.retained_bytes = 8192
        for space in spaces._spaces.values():
            spaces._weighed(space)
        after = [space.size for space in spaces._spaces.values()]
        assert [after[i] - before[i] for i in range(2)] == [2048, 8192]
        assert sum(after) - sum(before) == 10240
        for runtime in retained_runtimes:
            runtime.request_audit.retained_bytes = 0
        for space in spaces._spaces.values():
            spaces._weighed(space)
        assert [space.size for space in spaces._spaces.values()] == before
    finally:
        spaces._executor.shutdown(wait=True)
        spaces._index_executor.shutdown(wait=True)


def test_actual_registry_growth_replacement_and_close_are_weighed_once(retained_runtimes):
    runtime = retained_runtimes[0]
    store = runtime.mediator.echoes
    baseline = space_size(runtime)
    member = hashlib.sha256(b"value").digest()
    expected = ECHO_ENTRY_BYTES + ECHO_CONVERSATION_BYTES
    assert store.keep(b"c" * 32, (member,))
    assert runtime.request_retained_bytes == expected
    assert space_size(runtime) == baseline + expected
    assert store.keep(b"c" * 32, (member,))
    assert space_size(runtime) == baseline + expected
    store.ceiling = lambda added: "full" if added else None
    assert not store.keep(b"d" * 32, (hashlib.sha256(b"other").digest(),))
    assert space_size(runtime) == baseline + expected
    store.close()
    assert space_size(runtime) == baseline


def test_actual_receipts_enter_each_runtime_and_aggregate_once(retained_runtimes):
    spaces = Spaces(cast("Server", SimpleNamespace()))
    receipts = []
    try:
        for index, runtime in enumerate(retained_runtimes):
            store = runtime.mediator.echoes
            scope = EchoScope(("company",), "responses")
            body = {"input": [{"type": "image_generation_call", "result": "opaque"}]}
            capture = OpaqueCapture(EchoCheck(ROOT_KEY, store, scope, "s", {}), store)
            capture.body({"output": body["input"]})
            assert capture.commit()
            identity = cast(Identity, SimpleNamespace(id=f"receipt{index}"))
            space = Space(identity, runtime, None, cast(SpaceBackends, None), AsyncExitStack(), 0)
            spaces._spaces[identity.id] = space
            spaces._weighed(space)
            baseline = space.size
            runtime.retained_ceiling = lambda added: "full"
            refused = EchoCheck(ROOT_KEY, store, scope, "s", body)
            assert not refused.found and space_size(runtime) == baseline
            runtime.retained_ceiling = None
            receipt = EchoCheck(ROOT_KEY, store, scope, "s", body)
            receipts.append(receipt)
            assert receipt.found
            assert space_size(runtime) - baseline == 2 * ECHO_ENTRY_BYTES
            spaces._weighed(space)
            assert space.size - baseline == 2 * ECHO_ENTRY_BYTES
        assert sum(runtime.request_retained_bytes for runtime in retained_runtimes) == 8 * ECHO_ENTRY_BYTES
        for receipt in receipts:
            receipt.close()
            receipt.close()
        for space in spaces._spaces.values():
            spaces._weighed(space)
            assert space.retained == 2 * ECHO_ENTRY_BYTES
            space.runtime.mediator.echoes.close()
            spaces._weighed(space)
            assert space.retained == 0
    finally:
        for receipt in receipts:
            receipt.close()
        spaces._executor.shutdown(wait=True)
        spaces._index_executor.shutdown(wait=True)


async def test_real_space_retained_ceiling_sees_unweighed_reservations_and_excludes_detector_extra(server):
    identity, _ = server.identities.add_person("Opaque Budget")
    async with listening(server):
        runtime = await server.spaces.runtime(identity)
        space = server.spaces._spaces[identity.id]
        server.spaces._weighed(space)
        baseline = space.size - space.extra
        server.spaces.budget = 2 * (baseline + 2048)
        owner = runtime.mediator.echoes
        assert owner.reserve(2048)
        assert not owner.reserve(1)  # includes growth since the frozen weighing
        assert runtime.request_audit.retained_bytes == BASE_BYTES == 4096
        assert owner.retained_bytes == 2048
        assert runtime.request_retained_bytes == BASE_BYTES + 2048
        assert server.spaces._retained_past_share(space, 1) is not None
        owner.release(2048)
        assert server.spaces._retained_past_share(space, 2048) is None
        server.spaces._weighed(space)
        assert owner.retained_bytes == 0 and runtime.request_audit.retained_bytes == BASE_BYTES
        assert space.retained == BASE_BYTES and space.size - space.extra == baseline


@pytest.fixture
def budget_namespaces(request):
    schema = "latet_t_" + secrets.token_hex(5)
    prefix = "latet_t_" + secrets.token_hex(5) + ":"
    with psycopg.connect(DATABASE, connect_timeout=5) as connection:
        if connection.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone() is not None:
            raise RuntimeError("budget schema was not absent before ownership")
    with redis.Redis.from_url(redis_url(VALKEY), socket_connect_timeout=5, socket_timeout=5) as client:
        if next(client.scan_iter(match=prefix + "*", count=100), None) is not None:
            raise RuntimeError("budget prefix was not absent before ownership")
    request.node.user_properties.append(("p26a.before_absent", (schema, prefix)))
    return schema, prefix


async def _wait_budget_owners(tasks, *, allow_cancelled=False):
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=15)
        if pending or any(
            (task.cancelled() and not allow_cancelled) or (not task.cancelled() and task.exception() is not None)
            for task in done
        ):
            raise RuntimeError("budget owner drain was not proved")


@asynccontextmanager
async def _observed_budget_server(namespaces, tmp_path, request, monkeypatch, names):
    schema, prefix = namespaces
    data = tmp_path / "legacy-budget-models"
    data.mkdir()
    server = open_test_server(schema, prefix, data=data)
    work, workers, closing = [], set(), set()
    original_submit = ThreadPoolExecutor.submit
    original_close_later = server.spaces._close_later
    loop_thread = threading.get_ident()

    def submit(executor, fn, /, *args, **kwargs):
        if threading.get_ident() != loop_thread:
            return original_submit(executor, fn, *args, **kwargs)
        if len(work) == 64:
            raise RuntimeError("unexpected budget work inventory")
        actual = original_submit(executor, fn, *args, **kwargs)
        work.append(actual)
        return actual

    def close_later(spaces, words=None):
        workers.update(space.worker for space in spaces if space.worker is not None)
        original_close_later(spaces, words)
        closing.update(server.spaces._closing)

    with monkeypatch.context() as observe:
        observe.setattr(ThreadPoolExecutor, "submit", submit)
        observe.setattr(server.spaces, "_close_later", close_later)
        try:
            assert (server.settings.database_pool, server.settings.valkey_connections) == (10, 100)
            identities = tuple(server.identities.add_person(name)[0] for name in names)
            async with listening(server):
                try:
                    yield server, identities
                finally:
                    await _wait_budget_owners(set(server.spaces._building.values()))
                    await _wait_budget_owners(set(server.spaces._index_tasks))
        finally:
            await _wait_budget_owners(closing | server.spaces._closing)
            await _wait_budget_owners(workers, allow_cancelled=True)
            await _wait_budget_owners({asyncio.wrap_future(actual) for actual in work})
            await server.launch_mutations.drain()
            if server.spaces._spaces or server.spaces._building or server.spaces._closing or server._ear is not None:
                raise RuntimeError("budget Server still owns live work after drain")
            server.close()
            with redis.Redis.from_url(redis_url(VALKEY), socket_connect_timeout=5, socket_timeout=5) as client:
                keys = scanned_once(client.scan_iter(match=prefix + "*", count=100), 65)
                if len(keys) > 64 or any(not key.startswith(prefix.encode("ascii")) for key in keys):
                    raise RuntimeError("unexpected budget prefix inventory")
                if keys:
                    client.delete(*keys)
            with psycopg.connect(DATABASE, autocommit=True, connect_timeout=5) as connection:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            request.node.user_properties.append(("p26a.drained", namespaces))


@pytest.mark.parametrize("protected", ["busy", "approval"])
async def test_retained_growth_evicts_eligible_space_but_protects_held_owner(
    budget_namespaces, protected, monkeypatch, request, tmp_path
):
    from latet.server import spaces as spaces_module

    monkeypatch.setattr(spaces_module, "SPACE_SHARE", 1.0)
    names = ("Opaque First", "Opaque Second")
    async with _observed_budget_server(budget_namespaces, tmp_path, request, monkeypatch, names) as (
        server,
        (first, second),
    ):
        one = await server.spaces.runtime(first)
        two = await server.spaces.runtime(second)
        assert (one.config.ner, one.config.ocr, two.config.ner, two.config.ocr) == ("auto",) * 4
        spaces = server.spaces
        a, b = spaces._spaces[first.id], spaces._spaces[second.id]
        for space in (a, b):
            spaces._weighed(space)
        shared = server.launch_mutations.retained_bytes + server.launch_holds.retained_bytes
        baseline = a.size + b.size + shared
        assert spaces._retained_total() == baseline
        assert one.mediator.echoes.reserve(8192)
        spaces.budget = baseline + 4096
        assert baseline < spaces.budget < shared + space_size(one) + space_size(two)
        if protected == "busy":
            a.busy += 1
        else:
            monkeypatch.setattr(type(a.parts.approvals), "holding", property(lambda self: self is a.parts.approvals))
        server.spaces._fit(keep=None)
        assert first.id in server.spaces._spaces and second.id not in server.spaces._spaces
        await until(lambda: two.mediator.echoes.retained_bytes == 0)
        one.mediator.echoes.release(8192)
        if protected == "busy":
            a.busy -= 1
        assert one.mediator.echoes.retained_bytes == 0
        assert one.request_audit.retained_bytes == BASE_BYTES == 4096
        assert one.request_retained_bytes == BASE_BYTES


@pytest.fixture
def cb_real_local_spaces(tmp_path):
    """Both real owners, a fresh fixture; the old inert fixture stays unchanged."""
    spaces = Spaces(cast("Server", SimpleNamespace()))
    made = []
    try:
        for number in range(2):
            config = Config(ner="off", identity=IdentityConfig(enabled=False))
            built = build(config, Vault(ROOT_KEY))
            mediator = Mediator(built.engine, built.verifier)
            storage = SimpleNamespace(index_bytes=0, close=lambda: None)
            runtime = Runtime(
                config, built, storage, GatewayConfig(port=5283, mediator=mediator), tmp_path / ("cb-" + str(number))
            )
            runtime.projects = Projects(config, built.vault, files=False)
            identity = cast(Identity, SimpleNamespace(id="u_" + ("a" if number == 0 else "b") * 10))
            parts = cast(SpaceBackends, SimpleNamespace(approvals=SimpleNamespace(holding=False)))
            space = Space(identity, runtime, None, parts, AsyncExitStack(), 0)
            spaces._spaces[identity.id] = space
            made.append((runtime, space))
            spaces._bind_request_budget(space)
            spaces._weighed(space)
        yield spaces, made
    finally:
        for runtime, _space in made:
            runtime.close()
            runtime.mediator.executor.shutdown(wait=True)
            runtime.mediator.bridge_executor.shutdown(wait=True)
        spaces._executor.shutdown(wait=True)
        spaces._index_executor.shutdown(wait=True)


def test_audit_status_and_provenance_share_the_existing_space_budget(cb_real_local_spaces):
    """Two actual Audit bases and LocalEchoes contributions enter each real weighing once."""
    spaces, made = cb_real_local_spaces
    leases = []
    before = [space.size for _runtime, space in made]
    audit_growth = EVENT_RESERVE_BYTES + RESERVATION_BYTES + STATUS_BYTES
    echo_growth = ECHO_ENTRY_BYTES + ECHO_CONVERSATION_BYTES
    try:
        for number, (runtime, space) in enumerate(made):
            audit, echoes = runtime.request_audit, runtime.mediator.echoes
            assert type(audit) is RequestAudit and type(echoes) is LocalEchoes
            assert type(audit._ceiling) is _AuditFence
            assert runtime.gateway.request_audit is audit
            assert audit.retained_bytes == runtime.request_retained_bytes == space.retained == BASE_BYTES == 4096
            lease = audit.reserve_exception(new_exception_reference(1, 1))
            assert lease is not None
            leases.append((audit, lease))
            conversation = hashlib.sha256(("cb-conversation-" + str(number)).encode()).digest()
            member = hashlib.sha256(("cb-member-" + str(number)).encode()).digest()
            assert echoes.keep(conversation, (member,))
            assert echoes.keep(conversation, (member,)), "replacement must not double the provenance weight"
            assert audit.retained_bytes == BASE_BYTES + audit_growth
            assert echoes.retained_bytes == echo_growth
            assert runtime.request_retained_bytes == BASE_BYTES + audit_growth + echo_growth
            assert space.retained == BASE_BYTES, "this reading intentionally predates both admissions"
            assert space_size(runtime) - before[number] == audit_growth + echo_growth
        assert sum(runtime.request_retained_bytes for runtime, _space in made) == 2 * (
            BASE_BYTES + audit_growth + echo_growth
        )
        for _ in range(2):
            for runtime, space in made:
                spaces._weighed(space)
                assert space.retained == runtime.request_retained_bytes
        assert sum(space.size for _runtime, space in made) - sum(before) == 2 * (audit_growth + echo_growth)
        for runtime, space in made:
            runtime.mediator.echoes.close()
            assert runtime.request_retained_bytes == runtime.request_audit.retained_bytes
            spaces._weighed(space)
        for audit, lease in leases:
            assert audit.abandon(lease)
            assert audit.retained_bytes == BASE_BYTES + STATUS_BYTES, "ended reservation is gone; live status remains"
            audit.close()
            assert audit.retained_bytes == 0
        for runtime, space in made:
            spaces._weighed(space)
            assert runtime.request_retained_bytes == space.retained == 0
    finally:
        for audit, lease in leases:
            audit.abandon(lease)


@pytest.mark.parametrize("order", ["audit-first", "provenance-first"])
async def test_combined_owners_reject_growth_against_unweighed_real_space(server, order):
    """Either owner's admission must see the other's unweighed reservation at the third ceiling."""
    identity, _code = server.identities.add_person("P25 combined unweighed")
    async with listening(server):
        runtime = await server.spaces.runtime(identity)
        spaces, space = server.spaces, server.spaces._spaces[identity.id]
        audit, echoes = runtime.request_audit, runtime.mediator.echoes
        assert type(audit) is RequestAudit and type(echoes) is ValkeyEchoes
        assert type(echoes._front) is LocalEchoes and type(audit._ceiling) is _AuditFence
        spaces._weighed(space)
        cached, base = space.size, space.size - space.extra
        assert runtime.request_retained_bytes == space.retained == BASE_BYTES
        audit_growth = EVENT_RESERVE_BYTES + RESERVATION_BYTES + STATUS_BYTES
        echo_growth = 2048
        spaces.budget = 2 * (base + audit_growth + echo_growth)  # unchanged SPACE_SHARE=1/2
        reference = new_exception_reference(1, 1)
        lease, reserved = None, 0
        try:
            if order == "audit-first":
                lease = audit.reserve_exception(reference)
                assert lease is not None
                assert echoes.reserve(echo_growth)
                reserved = echo_growth
            else:
                assert echoes.reserve(echo_growth)
                reserved = echo_growth
                lease = audit.reserve_exception(reference)
                assert lease is not None
            assert space.size == cached and space.retained == BASE_BYTES
            assert runtime.request_retained_bytes == BASE_BYTES + audit_growth + echo_growth
            assert space_size(runtime) - cached == audit_growth + echo_growth
            before = (audit.active, len(audit._events), runtime.request_retained_bytes)
            assert not echoes.reserve(1), "the combined live owners already fill the real per-space share"
            assert audit.reserve_exception(reference) is None
            assert audit._reserve_projection(1) is None
            assert (audit.active, len(audit._events), runtime.request_retained_bytes) == before
            spaces._weighed(space)
            assert space.retained == runtime.request_retained_bytes
            assert space.size - cached == audit_growth + echo_growth
        finally:
            if lease is not None:
                audit.abandon(lease)
            if reserved:
                echoes.release(reserved)


@pytest.mark.parametrize("protected", ["busy", "approval", "active-audit"])
async def test_combined_growth_preserves_real_holds_then_evicts_only_eligible_owner(
    budget_namespaces, protected, monkeypatch, request, tmp_path
):
    """Default share policy protects the actual hold; after it ends the grown owner becomes eligible."""
    from latet.server import spaces as spaces_module

    names = ("P25 combined protected", "P25 combined neighbor")
    async with _observed_budget_server(budget_namespaces, tmp_path, request, monkeypatch, names) as (
        server,
        (first, second),
    ):
        one = await server.spaces.runtime(first)
        two = await server.spaces.runtime(second)
        assert (one.config.ner, one.config.ocr, two.config.ner, two.config.ocr) == ("auto",) * 4
        spaces = server.spaces
        a, b = spaces._spaces[first.id], spaces._spaces[second.id]
        audit, echoes = one.request_audit, one.mediator.echoes
        assert type(audit) is RequestAudit and type(echoes) is ValkeyEchoes
        assert spaces_module.SPACE_SHARE == 0.5, "no share/cap override is part of this witness"
        lease, reserved, busy, held_batch = None, 0, False, None
        try:
            if protected == "active-audit":
                lease = audit.reserve_exception(new_exception_reference(1, 1))
                assert lease is not None and audit.protects_idle
            else:
                assert audit.record(SafeDecisionRecord(source=1, outcome=1, coverage=1, attempts=0, answered=0))
                assert not audit.protects_idle
                if protected == "busy":
                    a.busy += 1
                    busy = True
                else:
                    held_batch = Batch("cb-approval", None, "other", [])
                    await a.parts.approvals.store(held_batch)
                    assert a.parts.approvals.holding and not held_batch.future.done()
            # Real owner growth and real weighting: no injected retained field or weighing function.
            for space in (a, b):
                spaces._weighed(space)
            shared = server.launch_mutations.retained_bytes + server.launch_holds.retained_bytes
            assert spaces._retained_total() == a.size + b.size + shared
            margin = 8192
            share = max(shared + b.size + a.extra + margin, a.size - a.extra + margin)
            budget = 2 * share
            without_protection = a.size - audit.retained_bytes - a.parts.approvals.held_bytes
            growth = max(
                32768,
                budget - shared - without_protection - b.size + margin,
                share - (without_protection - a.extra) + margin,
            )
            assert growth <= echoes._front.limit
            assert echoes.reserve(growth)
            reserved = growth
            spaces.budget = budget
            assert a.size + b.size + shared < budget < shared + space_size(one) + space_size(two)
            assert space_size(one) - a.extra > share
            assert space_size(two) - b.extra <= share
            assert shared + b.size + a.extra + share <= budget
            spaces._fit(keep=None)
            assert first.id in spaces._spaces and second.id in spaces._spaces
            assert not a.closed and not b.closed
            # The protected oversized owner counts at its share; it must not evict its smaller neighbor.
            if busy:
                a.busy -= 1
                busy = False
            if held_batch is not None:
                keeper = a.parts.approvals._keeper
                a.parts.approvals.close()
                if keeper is not None:
                    await _wait_budget_owners({keeper}, allow_cancelled=True)
                assert not a.parts.approvals.holding and held_batch.future.done()
            if lease is not None:
                assert audit.abandon(lease)
                lease = None
            assert not audit.protects_idle
            assert space_size(one) - a.extra > share
            assert shared + space_size(one) + space_size(two) > budget
            spaces._fit(keep=None)
            assert first.id not in spaces._spaces and second.id in spaces._spaces
            await _wait_budget_owners(set(spaces._closing))
            assert a.closed and audit.retained_bytes == 0
            assert one.request_retained_bytes == reserved, "live provenance reservation survives actual owner close"
            echoes.release(reserved)
            reserved = 0
            assert one.request_retained_bytes == 0 and not b.closed
        finally:
            if busy:
                a.busy -= 1
            if lease is not None:
                audit.abandon(lease)
            if held_batch is not None:
                keeper = a.parts.approvals._keeper
                a.parts.approvals.close()
                if keeper is not None:
                    await _wait_budget_owners({keeper}, allow_cancelled=True)
            if reserved:
                echoes.release(reserved)


@pytest.mark.parametrize("first_release", ["audit", "provenance"])
async def test_real_space_close_keeps_both_actual_owners_until_each_borrow_ends(server, first_release):
    """Actual Valkey-backed provenance receipt plus mandatory Audit lease outlive explicit Space close."""
    identity, _code = server.identities.add_person("P25 combined close")
    async with listening(server):
        runtime = await server.spaces.runtime(identity)
        audit, echoes = runtime.request_audit, runtime.mediator.echoes
        assert type(audit) is RequestAudit and type(echoes) is ValkeyEchoes
        scope = EchoScope(("company",), "responses")
        field = {"type": "image_generation_call", "result": "opaque"}
        key = runtime.built.vault.cache_key
        seed = EchoCheck(key, echoes, scope, "cb-lifetime", {})
        capture = OpaqueCapture(seed, echoes)
        capture.body({"output": [field]})
        receipt, lease = None, None
        try:
            loop = asyncio.get_running_loop()
            assert await loop.run_in_executor(runtime.mediator.executor, capture.commit)
            seed.close()
            receipt = await loop.run_in_executor(
                runtime.mediator.executor,
                lambda: EchoCheck(key, echoes, scope, "cb-lifetime", {"input": [field]}),
            )
            assert len(receipt.found) == 1
            receipt_charge = 2 * ECHO_ENTRY_BYTES
            lease = audit.reserve_exception(new_exception_reference(1, 1))
            assert lease is not None
            audit_charge = BASE_BYTES + STATUS_BYTES + EVENT_RESERVE_BYTES + RESERVATION_BYTES
            assert (
                runtime.request_retained_bytes
                == audit_charge + ECHO_ENTRY_BYTES + ECHO_CONVERSATION_BYTES + receipt_charge
            )
            await server.spaces.drop(identity.id)
            assert identity.id not in server.spaces._spaces
            assert audit.retained_bytes == audit_charge
            assert echoes.retained_bytes == receipt_charge
            assert runtime.request_retained_bytes == audit_charge + receipt_charge
            assert audit.reserve_exception(new_exception_reference(1, 1)) is None and not echoes.reserve(1)
            if first_release == "audit":
                assert audit.abandon(lease)
                lease = None
                assert runtime.request_retained_bytes == receipt_charge
                receipt.close()
            else:
                receipt.close()
                assert runtime.request_retained_bytes == audit_charge
                assert audit.abandon(lease)
                lease = None
            receipt.close()  # genuine receipt close is idempotent
            assert runtime.request_retained_bytes == 0
        finally:
            capture.close()
            seed.close()
            if receipt is not None:
                receipt.close()
            if lease is not None:
                audit.abandon(lease)
            runtime.mediator.executor.shutdown(wait=True)
