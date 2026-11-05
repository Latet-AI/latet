"""P2-8's negative and drift fixtures on a server (`tests/request_qualification.py`; a computer's in
`tests/agents/test_request_profile_compatibility.py` and `tests/unit/test_request_profile_drift.py`): the same cases
through the server's app (its gate, then the space of an API key, on PostgreSQL and Valkey) to the recording transport,
each with the outcome a computer gives, one code path on both machines (R-116). A case that needs an owner-listed
resource (a Vertex project, a Gemini cache) runs on a computer alone: an API key's settings come from the
administration, which these cells do not drive. No provider is called."""

from __future__ import annotations

import pytest

from ..request_coverage import run_case, server_cell
from ..request_qualification import DRIFT, NEGATIVE, fixtures, problems
from ..request_recorder import RecordingWSUpstream

pytestmark = pytest.mark.server


async def _run(instances, table):
    found: dict[str, list[str]] = {}
    upstream = RecordingWSUpstream().start()
    try:
        async with server_cell(instances, upstream) as cell:
            for family, fixture in fixtures(table, "server"):
                observation = await run_case(cell, fixture.case)
                if wrong := problems(cell, observation, fixture.expect):
                    found[f"{family}/{fixture.case.id}"] = wrong
    finally:
        upstream.stop()
    return found


async def test_the_negative_fixtures_on_a_server(instances):
    found = await _run(instances, NEGATIVE)
    assert not found, found


async def test_the_drift_fixtures_on_a_server(instances):
    found = await _run(instances, DRIFT)
    assert not found, found
