"""P2-8's drift fixtures on a computer (`tests/request_qualification.py`'s ``DRIFT``; the server's in
`tests/server/test_request_profile_qualification.py`): for each request family a routed agent uses, one added
top-level body member, one added nested member, one added header and one added query name, each with its documented
outcome, through the computer's actual ingress to the recording transport (S1-S4):

- a member no profile names, of control values, is read by the new rule and sent, the history naming its pass
  (`new-member`, 0.15.0); where new members are refused (`unknown_members: refuse`) a rewritten request refuses it,
  saying where it is (`body-unknown-member`: "unknown member in top-level", "in message", "in message content
  block"...), nothing sent; a Gemini part's member too (root's ruling 6);
- a request checked only (Claude Code, Bedrock, Vertex) lets a new member through when its value is a text or a list
  of texts, which the final check reads (D6), in either mode; an object by the new rule, or refused where new members
  are; a text that carries a sensitive value, never;
- a header or a query parameter no profile names is refused naming it, never its value (D4, root's ruling 5), over HTTP
  and at a WebSocket's handshake (closed 1008), and the history records the refusal by its category;
- a WebSocket frame's unknown member is read by the new rule, or, where new members are refused, refused in an error
  frame, nothing of the turn sent (no exception reaches a frame, F-2);
- a removable header of the family's SDK is taken off, the request sent.

No provider is called."""

from __future__ import annotations

import pytest

from ..request_coverage import computer_cell, run_case
from ..request_qualification import DRIFT, apply_resources, problems, recorded_passes
from ..request_recorder import RecordingWSUpstream


@pytest.fixture(params=["check", "refuse"])
def unknown_members(request) -> str:
    """A computer reading new members by the new rule, and one refusing them as before it."""
    return request.param


@pytest.fixture
async def cell(tmp_path, unknown_members):
    upstream = RecordingWSUpstream().start()
    try:
        async with computer_cell(tmp_path, upstream, unknown_members=unknown_members) as made:
            await apply_resources(made)
            yield made
    finally:
        upstream.stop()


def test_every_family_drifts_in_each_place():
    for family, rows in DRIFT.items():
        assert {"top", "nested", "header", "query"} <= {fixture.kind for fixture in rows}, family


@pytest.mark.parametrize("family", sorted(DRIFT))
async def test_each_familys_drift_has_its_documented_outcome_on_a_computer(cell, unknown_members, family):
    found = {}
    for fixture in DRIFT[family]:
        if "computer" in fixture.case.machines:
            observation = await run_case(cell, fixture.case)
            expect = fixture.expected(unknown_members)
            if wrong := problems(cell, observation, expect, await recorded_passes(cell)):
                found[fixture.case.id] = wrong
    assert not found, found


def test_every_added_body_member_has_its_outcome_where_new_members_are_refused():
    """Each drift the new rule reads (``READ``) says what it gave before it: a refusal naming the path's category."""
    for family, rows in DRIFT.items():
        for fixture in rows:
            if fixture.expect.passes == "new-member" or fixture.expect.frames_up == 1:
                assert fixture.strict is not None and fixture.strict.sends == 0, (family, fixture.case.id)
