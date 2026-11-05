"""The request fuzz of P2-8 (``scripts/request_coverage_fuzz.py``) on a server: a small fixed-seed smoke of each of its
eight server cells (HTTP and WebSocket, strict and with a precise exception, the history on and off), through the
server's app (its gate, then an API key's space, on PostgreSQL and Valkey: the ``instances`` fixture), and the node
``scripts/request_coverage_fuzz.py run`` asks for the server's cells. The full run (5,000 a cell) is root's."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import request_coverage_fuzz as rcf  # noqa: E402

from ..request_coverage import server_cell  # noqa: E402
from ..request_recorder import RecordingWSUpstream  # noqa: E402

pytestmark = pytest.mark.server
SEED = 20261104
SMOKE = 10
SERVER_CELLS = [cell for cell in rcf.CELLS if cell.startswith("server-")]


@pytest.mark.parametrize("cell", SERVER_CELLS)
async def test_the_smoke_of_each_server_cell(instances, cell):
    upstream = RecordingWSUpstream().start()
    try:
        async with rcf.fuzz_cell(cell, lambda: server_cell(instances, upstream), 4) as made:
            report = await rcf.fuzz(made, SEED, SMOKE)
    finally:
        upstream.stop()
    found = report.as_json()
    assert found["finished"] and found["executed"] == found["generated"] == SMOKE, found
    assert found["violated"] == {} and found["errors"] == {}, found["samples"]
    assert {"coverage", "authority", "bytes", "masking", "restoration", "sinks", "ownership"} <= set(found["asserted"])
    assert found["outcomes"].get("sent", 0) >= 1 and found["outcomes"].get("blocked", 0) >= 1, found["outcomes"]


async def _fuzz_the_server(instances):
    """``scripts/request_coverage_fuzz.py run``: the server's cells of ``LATET_RCF_CELLS``, each an identity of its own
    (an API key) on the same two instances."""
    upstream = RecordingWSUpstream().start()
    try:
        await rcf.run_machine_cells(lambda cell: server_cell(instances, upstream))
    finally:
        upstream.stop()


if os.environ.get("LATET_RCF_OUT"):  # collected for the script alone: never a skip (CI fails a server test skipped)
    test_fuzz_the_server_for_the_script = _fuzz_the_server
