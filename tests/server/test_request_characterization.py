"""The characterization of the requests on a server (P1-1 of `notes/server-request-coverage-plan.md`): the table of
``tests/request_coverage.py`` through the server's app (its gate, then the space of an API key, on PostgreSQL and
Valkey: ``tests/server/support.py`` and the ``instances`` fixture), row after row in one cell, each row's observation
compared exactly with Phase 2's baseline and held to what the RC plan intends of it on 0.11 (the computer module's
``INTENDED``, one code path on both machines, and ``ON_A_SERVER``); and what documents 0.10 from its golden:
RC-REC-11's leak rows at 0.10's counts. It documents behaviour: what a server passes on as it came is recorded so,
never called safe."""

import os
from pathlib import Path

import pytest

from ..request_coverage import (
    TABLE_0_10,
    expected,
    holds_no_value,
    leak_counts,
    load_baseline,
    load_golden,
    observe_machine,
    phase_1_differences,
    run_case,
    server_cell,
    table_for,
)
from ..request_recorder import RecordingWSUpstream
from ..unit.test_request_characterization import _compare, _report, documented

pytestmark = pytest.mark.server


async def test_the_table_on_a_server(instances):
    baseline = load_baseline()
    upstream = RecordingWSUpstream().start()
    try:
        async with server_cell(instances, upstream) as cell:
            observations = [(case, await run_case(cell, case)) for case in table_for("server")]
    finally:
        upstream.stop()
    assert not holds_no_value([o for _, o in observations])
    differing = _compare("server", observations, baseline)
    assert not differing, _report(differing)
    documented("server", {case.id: o for case, o in observations})


@pytest.mark.parametrize("row", [case.id for case in TABLE_0_10 if case.leak is not None])
def test_the_leak_rows_held_on_a_servers_vault_in_0_10(row):
    """On a server the leak was the vault's: what 0.10's golden recorded of each leak row left a conversation held (an
    API key's ``StoredVault``) after the request was over; Phase 1 released it, and changed no other count but the
    releases, the renewer and the answer left open, never the holds taken."""
    golden = load_golden()
    case = next(case for case in TABLE_0_10 if case.id == row)
    today = golden["table"]["server"][row]
    assert today["held"] >= 1
    changed = phase_1_differences(case, today)
    assert "held" in changed and set(changed) <= {"release", "renewer_running", "held", "unclosed"}
    assert leak_counts(today)["hold"] == (case.after_phase_1 or {})["hold"]


async def _observe_the_server(instances):
    """``scripts/request_characterization.py record|replay``: the server's table and its two cells of generated cases
    (``LATET_RC_SEED``, ``LATET_RC_COUNT``), written to ``LATET_RC_OUT``/server.json. Each cell its own identity
    (an API key) on the same two instances."""
    upstream = RecordingWSUpstream().start()
    try:
        out = Path(os.environ["LATET_RC_OUT"]) / "server.json"
        seed, count = int(os.environ["LATET_RC_SEED"]), int(os.environ["LATET_RC_COUNT"])
        await observe_machine(lambda: server_cell(instances, upstream), "server", seed, count, out)
    finally:
        upstream.stop()


if os.environ.get("LATET_RC_OUT"):  # collected for the script alone: never a skip (CI fails a server test skipped)
    test_observe_the_server_for_the_script = _observe_the_server


def test_every_server_row_has_a_golden():
    """0.10's golden covers every server row of 0.10's table (Phase 1's evidence)."""
    golden = load_golden()
    assert [
        case.id for case in TABLE_0_10 if "server" in case.machines and expected(golden, "server", case) is None
    ] == []


def test_every_server_row_has_a_baseline():
    baseline = load_baseline()
    assert [case.id for case in table_for("server") if expected(baseline, "server", case) is None] == []
