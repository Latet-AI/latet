"""Bounded checks of the actual Server launch admission owners."""

import asyncio
import threading
from sys import getsizeof
from types import SimpleNamespace
from typing import cast

import pytest

from latet.config import ProjectEntry
from latet.gateway.launch_keys import DIRECTORY_BYTES, HOLD_BYTES, LaunchRefused
from latet.server.app import Server
from latet.server.launch_binding import BindingCodec
from latet.server.launch_controls import LaunchHoldAdmission
from latet.server.launch_mutations import MUTATION_BYTES, MUTATIONS, LaunchMutations, OpenOutcome, OpenResultCell
from latet.server.launch_open import _binding, _OpenHandoff, _OpenPayload, _run_open
from latet.server.launch_records import LaunchUnavailable, Operation
from latet.server.launches import LaunchAuthority, LaunchCapacity, Publication, ReservationUncertain, Ticket
from latet.server.machines import submit_store
from latet.server.spaces import PAST_SHARE, Space, Spaces, _AuditFence


def test_launch_budget_precharge_rolls_back_and_space_cache_does_not_own_tickets():
    ledger = LaunchMutations()
    holds = LaunchHoldAdmission()
    spaces = Spaces(cast(Server, object()), launch_holds=holds, launch_mutations=ledger)
    holds.bind_budget(spaces.launch_fits)
    space = cast(Space, SimpleNamespace(identity=SimpleNamespace(id="u_aaaaaaaaaa"), size=16384))
    replacement = cast(Space, SimpleNamespace(identity=space.identity, size=32768))
    base = spaces._retained_total()
    try:
        spaces._publish_space(space)
        assert spaces._retained_total() == base + space.size
        spaces._publish_space(replacement)
        assert spaces._retained_total() == base + replacement.size
        spaces.budget = spaces._retained_total() + HOLD_BYTES - 1
        assert holds.reserve() is None and holds.count == 0
        assert ledger.reserve(spaces.launch_fits) is None
        assert spaces._retained_total() == base + replacement.size

        spaces.budget = spaces._retained_total() + MUTATION_BYTES
        ticket = ledger.reserve(spaces.launch_fits)
        assert ticket is not None
        assert spaces._retained_total() == base + replacement.size + MUTATION_BYTES
        vault, echoes = threading.RLock(), threading.RLock()

        def share(added):
            assert added == 0 and ledger.issuer_bytes(space.identity.id) == MUTATION_BYTES
            return None

        with vault:
            assert not ticket.attach(space.identity.id, _AuditFence((vault, echoes), share))
        locked, release = threading.Event(), threading.Event()

        def contend():
            with vault:
                locked.set()
                release.wait(3)

        thread = threading.Thread(target=contend)
        thread.start()
        try:
            assert locked.wait(3)
            assert not ticket.attach(space.identity.id, _AuditFence((vault, echoes), share))
        finally:
            release.set()
            thread.join(3)
        assert not thread.is_alive()
        assert ledger.issuer_bytes(space.identity.id) == 0
        assert not ticket.attach(space.identity.id, _AuditFence((), share))
        assert not ticket.attach(space.identity.id, _AuditFence((vault, echoes), lambda _added: PAST_SHARE))
        assert ticket.attach(space.identity.id, _AuditFence((vault, echoes), share))
        assert spaces._retained_total() == base + replacement.size + MUTATION_BYTES
        assert spaces._remove_space(space.identity.id) is replacement
        assert spaces._retained_total() == base + MUTATION_BYTES
        assert ledger.issuer_bytes(space.identity.id) == MUTATION_BYTES
        ticket.end_ingress()
        assert spaces._retained_total() == base and ledger.issuer_bytes(space.identity.id) == 0
    finally:
        spaces.shutdown()

    tickets = [ledger.reserve(lambda: True) for _ in range(MUTATIONS)]
    assert all(ticket is not None for ticket in tickets)
    assert ledger.reserve(lambda: True) is None
    empty_issuers = getsizeof(ledger._issuers)
    for index, ticket in enumerate(tickets):
        assert ticket is not None
        assert ticket.attach(f"issuer{index}", lambda _added: None)
    directory = getsizeof(ledger) + getsizeof(ledger._issuers) + getsizeof(ledger._slots) + getsizeof(ledger._free)
    assert directory <= DIRECTORY_BYTES
    for ticket in tickets:
        assert ticket is not None
        ticket.end_ingress()
    assert getsizeof(ledger._issuers) == empty_issuers
    ledger.require_drained()


async def test_actual_store_running_and_queued_cancellation_keep_one_charge_until_consumed():
    ledger = LaunchMutations()
    issuer = "u_aaaaaaaaaa"
    baseline = ledger.retained_bytes
    running, queued = ledger.reserve(lambda: True), ledger.reserve(lambda: True)
    assert running is not None and queued is not None
    for index, ticket in enumerate((running, queued)):
        assert ticket.attach(issuer, lambda _added: None)
        assert ticket.identify(bytes([index]) * 32)
    loop = asyncio.get_running_loop()
    all_running = asyncio.Event()
    release = threading.Event()
    workers_started = 0

    def started():
        nonlocal workers_started
        workers_started += 1
        if workers_started == 4:
            all_running.set()

    def blocker(result):
        loop.call_soon_threadsafe(started)
        assert release.wait(3), "the test must release the bounded STORE barriers"
        result.outcome = OpenOutcome("unavailable")

    results = [OpenResultCell() for _ in range(4)]
    running.submit(blocker, results[0], results[0], not_started=OpenOutcome("unavailable"))
    assert running.actual is not None
    workers = [running.actual, *(submit_store(blocker, result) for result in results[1:])]
    waiting = []
    try:
        await asyncio.wait_for(all_running.wait(), 3)

        def unresolved(result):
            result.outcome = OpenOutcome("unavailable", uncertain=True)

        outcome = OpenResultCell()
        queued.submit(unresolved, outcome, outcome, not_started=OpenOutcome("unavailable"))
        future = queued.actual
        assert future is not None
        workers.append(future)
        assert workers[0].running() and not future.running() and not future.done()
        observed = [asyncio.Event(), asyncio.Event()]

        async def wait(ticket, ready):
            ready.set()
            return await ticket.wait()

        waiting = [
            asyncio.create_task(wait(ticket, ready)) for ticket, ready in zip((running, queued), observed, strict=True)
        ]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in observed)), 3)
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        running.end_ingress()
        queued.end_ingress()
        assert not future.cancelled() and ledger.retained_bytes == baseline + 2 * MUTATION_BYTES
        with pytest.raises(RuntimeError, match="have not drained"):
            ledger.require_drained()
    finally:
        release.set()
        running.end_ingress()
        queued.end_ingress()
        views = [asyncio.shield(asyncio.wrap_future(future)) for future in workers]
        await asyncio.wait_for(asyncio.gather(*views, return_exceptions=True), 3)
        await ledger.drain()
    assert ledger.retained_bytes == baseline + MUTATION_BYTES
    assert ledger.issuer_bytes(issuer) == MUTATION_BYTES
    assert queued.actual is None and queued.outcome is not None and queued.outcome.uncertain
    assert outcome.outcome is None and results[0].outcome is None


def test_protected_open_selects_current_canonical_root_without_host_paths():
    entries = [ProjectEntry(path="/Repo"), ProjectEntry(path="/Repo/work"), ProjectEntry(path="/repo")]
    data = {"folder": "/Repo/work/child", "root": None, "flavor": "posix"}
    selected = _binding(data, entries)
    assert (selected.root, selected.source) == ("/Repo/work", "server")
    fallback = _binding({**data, "root": "/Repo"}, [])
    assert (fallback.root, fallback.source) == ("/Repo", "client")
    with pytest.raises(LaunchRefused, match="launch-unregistered"):
        _binding(data, [])
    with pytest.raises(LaunchRefused, match="launch-invalid"):
        _binding({**data, "root": "/Other"}, entries)
    # An entry in a form a launch cannot compare is left out, never fatal to the others (P2-6, I-7).
    skipped = _binding(data, [*entries, ProjectEntry(path="relative")])
    assert (skipped.root, skipped.source) == ("/Repo/work", "server")
    with pytest.raises(LaunchRefused, match="launch-invalid"):
        _binding(data, [ProjectEntry(path=r"C:\Repo"), ProjectEntry(path=r"C:\repo")])
    with pytest.raises(LaunchRefused, match="launch-invalid"):
        _binding(data, entries * 67)


def test_protected_open_worker_consumes_handoff_and_keeps_only_the_typed_outcome(monkeypatch):
    authority = object.__new__(LaunchAuthority)
    issuer = "u_aaaaaaaaaa"
    locator, launch_id = b"l" * 32, b"i" * 16
    publication = Publication(
        Ticket(issuer, locator, launch_id, b"e" * 16, b"w" * 16, b"o" * 16, Operation.OPEN, 1, 0, 0, None),
        None,
    )
    binding = _binding({"folder": "/root/child", "root": "/root", "flavor": "posix"}, [])
    payload = _OpenPayload(authority, BindingCodec(b"r" * 32), issuer, locator, launch_id, b"v" * 32, binding)
    failures = (
        (LaunchCapacity(), "capacity", None, False),
        (LaunchUnavailable(), "unavailable", None, False),
        (ReservationUncertain(publication), "unavailable", publication, False),
        (RuntimeError("private backend failure"), "unavailable", None, True),
    )
    for failure, status, retained, uncertain in failures:

        def fail(*_args, failure=failure):
            raise failure

        monkeypatch.setattr(LaunchAuthority, "reserve_open", fail)
        result = OpenResultCell()
        handoff = _OpenHandoff(payload, result)
        assert _run_open(handoff) is None and handoff.payload is None
        outcome = result.take()
        assert outcome is not None
        assert (outcome.status, outcome.uncertain) == (status, uncertain)
        assert outcome.receipt is None and outcome.retained is retained and result.outcome is None
