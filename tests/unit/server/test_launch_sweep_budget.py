"""Offline budget checks for authenticated sweep discovery candidates."""

import asyncio
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import AsyncExitStack, ExitStack, contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from starlette.types import ASGIApp

from latet.config import Config, IdentityConfig
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig
from latet.gateway.launch_keys import DIRECTORY_BYTES
from latet.gateway.mediate import Mediator
from latet.gateway.provenance import LocalEchoes
from latet.projects import Projects
from latet.runtime import Runtime
from latet.server import machines
from latet.server.app import Server
from latet.server.backends import SpaceBackends
from latet.server.identities import Identity
from latet.server.launch_mutations import MUTATION_BYTES, LaunchMutations, MutationAdmission
from latet.server.launch_records import Entry, Operation, Reason, State
from latet.server.launch_sweep import LaunchSweep
from latet.server.launches import LaunchAuthority
from latet.server.spaces import PAST_SHARE, SPACE_SHARE, Space, Spaces, _AuditFence, space_size
from latet.state.protocols import VaultStore

if TYPE_CHECKING:
    from latet.gateway.request_audit import _NativeRLock

_ISSUER = "u_aaaaaaaaaa"
_SEED = b"s" * 16
_TARGET = b"t" * 16
_LOCATOR = b"l" * 32


@contextmanager
def _spaces() -> Iterator[tuple[Spaces, LaunchMutations]]:
    ledger = LaunchMutations()
    spaces = Spaces(cast(Server, object()), launch_mutations=ledger)
    with ExitStack() as cleanup:
        cleanup.callback(spaces._executor.shutdown, wait=True)
        cleanup.callback(spaces._index_executor.shutdown, wait=True)
        cleanup.callback(spaces.shutdown)
        yield spaces, ledger


@contextmanager
def _probe(spaces: Spaces, ledger: LaunchMutations) -> Iterator[MutationAdmission]:
    ticket = ledger.reserve(spaces.launch_fits, background=True)
    assert ticket is not None
    try:
        yield ticket
    finally:
        ticket.end_ingress()


def _claim(ticket: MutationAdmission, spaces: Spaces, target: bytes = _TARGET) -> bool:
    return ticket.claim_detached(_ISSUER, target, partial(spaces.launch_background_fits, _ISSUER))


def _assert_unattached(ticket: MutationAdmission) -> None:
    assert ticket.issuer is None and ticket.target is None and not ticket.claimed
    assert ticket.owner._slots[ticket.slot] is ticket


def test_sweep_background_absent_space_precharge_and_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    with _spaces() as (spaces, ledger):
        limit = int(2 * MUTATION_BYTES / SPACE_SHARE)
        global_charge = DIRECTORY_BYTES + 2 * MUTATION_BYTES
        spaces.budget = limit
        with _probe(spaces, ledger) as seed:
            assert _claim(seed, spaces, _SEED)
            original_fits = spaces.launch_fits
            original_issuer_bytes = LaunchMutations.issuer_bytes
            seen: list[tuple[int, int, bool]] = []
            issuer_reads: list[str] = []

            def launch_fits() -> bool:
                fits = original_fits()
                seen.append((spaces._retained_total(), original_issuer_bytes(ledger, _ISSUER), fits))
                return fits

            def issuer_bytes(owner: LaunchMutations, issuer: str) -> int:
                issuer_reads.append(issuer)
                return original_issuer_bytes(owner, issuer)

            def unexpected_fence(_space: Space) -> _AuditFence:
                pytest.fail("absent issuer attempted to take a loaded Space fence")

            monkeypatch.setattr(spaces, "launch_fits", launch_fits)
            monkeypatch.setattr(spaces, "_request_fence", unexpected_fence)
            monkeypatch.setattr(LaunchMutations, "issuer_bytes", issuer_bytes)
            for budget, global_fits, admitted in (
                (global_charge - 1, False, False),
                (limit - 1, True, False),
                (limit, True, True),
            ):
                spaces.budget = limit
                seen.clear()
                issuer_reads.clear()
                with _probe(spaces, ledger) as ticket:
                    spaces.budget = budget
                    assert _claim(ticket, spaces) is admitted
                    assert seen == [
                        (global_charge, MUTATION_BYTES, True),
                        (global_charge, 2 * MUTATION_BYTES, global_fits),
                    ]
                    assert issuer_reads == ([_ISSUER] if global_fits else [])
                    assert spaces._retained_total() == global_charge
                    if admitted:
                        assert ticket.claimed and ticket.issuer == _ISSUER and ticket.target == _TARGET
                        assert original_issuer_bytes(ledger, _ISSUER) == 2 * MUTATION_BYTES
                    else:
                        _assert_unattached(ticket)
                        assert original_issuer_bytes(ledger, _ISSUER) == MUTATION_BYTES
                assert ledger._slots[ticket.slot] is None
                assert spaces._retained_total() == DIRECTORY_BYTES + MUTATION_BYTES
                assert original_issuer_bytes(ledger, _ISSUER) == MUTATION_BYTES
                assert seed.claimed and seed.target == _SEED
        assert spaces._retained_total() == DIRECTORY_BYTES
        assert original_issuer_bytes(ledger, _ISSUER) == 0
        assert not spaces._spaces and not spaces._building and not spaces._missed


def _contend_project(projects: Projects, vault: Vault, echoes: LocalEchoes, attempt: Callable[[], None]) -> None:
    entered, release = threading.Event(), threading.Event()
    timed_out = False

    def hold() -> None:
        nonlocal timed_out
        with projects._lock:
            entered.set()
            timed_out = not release.wait(3)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(3)
        attempt()
        assert holder.is_alive()
        assert not cast("_NativeRLock", vault._lock)._is_owned()
        assert not cast("_NativeRLock", echoes._lock)._is_owned()
    finally:
        release.set()
        holder.join(3)
        assert not holder.is_alive()
    assert not timed_out


async def test_sweep_background_loaded_space_fences_and_charges_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _spaces() as (spaces, ledger), ExitStack() as cleanup:
        config = Config(ner="off", ocr="off", history="off", identity=IdentityConfig(enabled=False))
        vault = Vault(b"v" * 32)
        built = build(config, vault)
        mediator = Mediator(built.engine, built.verifier)
        cleanup.callback(mediator.executor.shutdown, wait=True)
        cleanup.callback(mediator.bridge_executor.shutdown, wait=True)
        initialization = cleanup.enter_context(ExitStack())
        initialization.callback(mediator.close)
        projects = Projects(config, vault, files=False)
        identity = Identity(_ISSUER, "test", None, "person", False, 0, None, "local", None)
        runtime = Runtime(
            config,
            built,
            cast(VaultStore, SimpleNamespace(index_bytes=0, close=lambda: None)),
            GatewayConfig(mediator=mediator),
            tmp_path,
            identity=identity.ref(),
            projects=projects,
        )
        initialization.pop_all()  # Runtime now owns mediator.close.
        cleanup.callback(runtime.close)
        echoes = mediator.echoes
        assert type(echoes) is LocalEchoes
        async with AsyncExitStack() as stack:
            space = Space(identity, runtime, cast(ASGIApp, None), cast(SpaceBackends, None), stack, 0)
            spaces._bind_request_budget(space)
            spaces._weighed(space)
            spaces._publish_space(space)
            try:
                weight = space_size(runtime)
                assert space.size == weight and space.extra == 0
                global_charge = DIRECTORY_BYTES + weight + MUTATION_BYTES
                limit = int((weight + MUTATION_BYTES) / SPACE_SHARE)
                assert global_charge <= limit - 1
                locks = vault._lock, projects._lock, echoes._lock
                fences: list[tuple[object, ...]] = []
                checks: list[tuple[int, int, int, tuple[bool, ...], str | None]] = []
                original_fence = spaces._request_fence
                original_share = spaces._retained_past_share

                def request_fence(loaded: Space) -> _AuditFence:
                    assert loaded is space
                    fence = original_fence(loaded)
                    fences.append(fence.locks)
                    return fence

                def retained_share(loaded: Space, added: int) -> str | None:
                    assert loaded is space
                    result = original_share(loaded, added)
                    checks.append(
                        (
                            added,
                            ledger.issuer_bytes(_ISSUER),
                            spaces._retained_total(),
                            tuple(cast("_NativeRLock", lock)._is_owned() for lock in locks),
                            result,
                        )
                    )
                    return result

                monkeypatch.setattr(spaces, "_request_fence", request_fence)
                monkeypatch.setattr(spaces, "_retained_past_share", retained_share)

                def attempt(admitted: bool) -> None:
                    with _probe(spaces, ledger) as ticket:
                        assert spaces._retained_total() == global_charge
                        assert _claim(ticket, spaces) is admitted
                        assert spaces._retained_total() == global_charge and space.size == weight
                        if admitted:
                            assert ticket.claimed and ticket.issuer == _ISSUER and ticket.target == _TARGET
                            assert ledger.issuer_bytes(_ISSUER) == MUTATION_BYTES
                        else:
                            _assert_unattached(ticket)
                            assert ledger.issuer_bytes(_ISSUER) == 0
                    assert ledger._slots[ticket.slot] is None
                    assert ledger.issuer_bytes(_ISSUER) == 0
                    assert spaces._retained_total() == DIRECTORY_BYTES + weight

                spaces.budget = limit - 1
                attempt(False)
                refused = (0, MUTATION_BYTES, global_charge, (True, True, True), PAST_SHARE)
                assert checks == [refused]
                spaces.budget = limit
                _contend_project(projects, vault, echoes, partial(attempt, False))
                assert checks == [refused]
                attempt(True)
                assert checks == [refused, (0, MUTATION_BYTES, global_charge, (True, True, True), None)]
                assert fences == [locks, locks, locks]
            finally:
                removed = spaces._remove_space(_ISSUER)
            assert removed is space
        assert spaces._retained_total() == DIRECTORY_BYTES and ledger.issuer_bytes(_ISSUER) == 0
        assert not spaces._spaces and not spaces._building and not spaces._missed


class _DiscoveryAuthority:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.bad_arguments = 0

    def __getattr__(self, name: str) -> None:
        self.calls[name] += 1
        raise AssertionError(f"unexpected authority call: {name}")

    def expired_locators(self, cursor: bytes | None, limit: int) -> tuple[bytes, ...]:
        self.calls["expired_locators"] += 1
        self.bad_arguments += (cursor, limit) != (None, 16)
        return (_LOCATOR,)

    def _snapshot(self, locator: bytes) -> tuple[Entry, int]:
        self.calls["snapshot"] += 1
        self.bad_arguments += locator != _LOCATOR
        return (
            Entry(
                locator=_LOCATOR,
                launch_id=_TARGET,
                issuer=_ISSUER,
                epoch=b"e" * 16,
                revision=1,
                generation=2,
                op_seq=0,
                state=State.LIVE,
                deadline_us=1,
                binding_reserved=True,
                binding_digest=b"d" * 32,
                published_token=b"p" * 16,
                published_blob_digest=b"b" * 32,
                terminal_reason=Reason.NONE,
                disclosure_possible=True,
                exit_confirmed=False,
                op_kind=Operation.NONE,
                op_id=None,
                op_owner=None,
                op_base_gen=None,
                op_target_gen=None,
                op_payload_digest=None,
            ),
            2,
        )


async def test_sweep_discovery_loses_foreground_canonical_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    with _spaces() as (spaces, ledger):
        spaces.budget = int(2 * MUTATION_BYTES / SPACE_SHARE)
        authority = _DiscoveryAuthority()
        sweep = LaunchSweep()
        work: list[Future[None]] = []
        submit_attempts = 0
        checks = 0
        claims: list[tuple[MutationAdmission, str, bytes, bool]] = []
        original_submit = machines.submit_store
        original_check = spaces.launch_background_fits
        original_claim = MutationAdmission.claim_detached

        def submit(fn: Callable[[object], None], argument: object) -> Future[None]:
            nonlocal submit_attempts
            submit_attempts += 1
            assert submit_attempts == 1
            actual = original_submit(fn, argument)
            work.append(actual)
            return actual

        def check(issuer: str) -> bool:
            nonlocal checks
            checks += 1
            return original_check(issuer)

        def claim(ticket: MutationAdmission, issuer: str, launch_id: bytes, fits: Callable[[], bool]) -> bool:
            accepted = original_claim(ticket, issuer, launch_id, fits)
            claims.append((ticket, issuer, launch_id, accepted))
            return accepted

        def foreground_share(added: int) -> str | None:
            assert added == 0
            return None if original_check(_ISSUER) else PAST_SHARE

        monkeypatch.setattr(machines, "submit_store", submit)
        monkeypatch.setattr(spaces, "launch_background_fits", check)
        monkeypatch.setattr(MutationAdmission, "claim_detached", claim)
        foreground = None
        try:
            async with asyncio.timeout(10):
                sweep.tick(cast(LaunchAuthority, authority), spaces)
                candidate = sweep._active
                assert candidate is not None and len(work) == 1 and candidate.actual is work[0]
                _assert_unattached(candidate)
                foreground = ledger.reserve(spaces.launch_fits)
                assert foreground is not None
                assert foreground.attach(_ISSUER, foreground_share) and foreground.identify(_TARGET)
                assert candidate.actual is work[0] and sweep._active is candidate
                assert ledger.issuer_bytes(_ISSUER) == MUTATION_BYTES
                assert spaces._retained_total() == DIRECTORY_BYTES + 2 * MUTATION_BYTES

                await asyncio.shield(asyncio.wrap_future(work[0]))
                await candidate.wait()
                assert sweep._pg == _LOCATOR and sweep._active is None and candidate.actual is None
                assert ledger._slots[candidate.slot] is None and candidate.outcome is None
                assert ledger._slots[foreground.slot] is foreground and foreground.claimed and foreground.ingress
                assert foreground.issuer == _ISSUER and foreground.target == _TARGET
                assert ledger.issuer_bytes(_ISSUER) == MUTATION_BYTES
                assert spaces._retained_total() == DIRECTORY_BYTES + MUTATION_BYTES
                assert claims == [(candidate, _ISSUER, _TARGET, False)] and checks == 0
                assert authority.calls == {"expired_locators": 1, "snapshot": 1} and authority.bad_arguments == 0
                assert submit_attempts == len(work) == 1
        finally:
            if foreground is not None:
                foreground.end_ingress()
            ledger.stop()
            async with asyncio.timeout(15):
                try:
                    for actual in work:
                        await asyncio.shield(asyncio.wrap_future(actual))
                finally:
                    await ledger.drain()
        assert spaces._retained_total() == DIRECTORY_BYTES and ledger.issuer_bytes(_ISSUER) == 0
        assert not spaces._spaces and not spaces._building and not spaces._missed
