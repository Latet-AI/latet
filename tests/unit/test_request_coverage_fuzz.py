"""The request fuzz of P2-8 (``scripts/request_coverage_fuzz.py``): its sixteen cells, a generator that is seeded and
bounded, a judge that catches each kind of violation it names, and a small fixed-seed smoke of each computer cell (the
server's in ``tests/server/test_request_coverage_fuzz.py``). The full run (16 x 5,000) is root's, scheduled apart."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import request_coverage_fuzz as rcf  # noqa: E402

from ..request_coverage import EMAIL, computer_cell  # noqa: E402
from ..request_recorder import RecordingWSUpstream, SentRequest  # noqa: E402

SEED = 20261104
SMOKE = 12  # cases of each cell in the smoke
COMPUTER_CELLS = [cell for cell in rcf.CELLS if cell.startswith("computer-")]


# --- the cells and the generator ------------------------------------------------------------------------------------


def test_sixteen_cells_one_for_each_machine_transport_policy_and_history():
    assert len(rcf.CELLS) == len(set(rcf.CELLS)) == 16
    seen = {rcf.parts(cell) for cell in rcf.CELLS}
    assert seen == {
        (m, t, p, h) for m in rcf.MACHINES for t in rcf.TRANSPORTS for p in rcf.POLICIES for h in rcf.HISTORIES
    }
    with pytest.raises(ValueError, match="not a cell of the fuzz"):
        rcf.parts("computer-http-loose-history-on")


@pytest.mark.parametrize("cell", ["computer-http-exception-history-on", "server-ws-strict-history-off"])
def test_the_generator_is_seeded_by_the_seed_and_the_cell_alone(cell):
    first = [case.digest() for case in rcf.generate(SEED, 40, cell)]
    assert first == [case.digest() for case in rcf.generate(SEED, 40, cell)]
    assert first != [case.digest() for case in rcf.generate(SEED + 1, 40, cell)]
    assert (
        rcf.corpus(SEED, 40, [cell])["cells"][cell]["sha256"] == rcf.corpus(SEED, 40, [cell])["cells"][cell]["sha256"]
    )


@pytest.mark.parametrize("cell", rcf.CELLS)
def test_every_case_is_within_the_bounds(cell):
    cases = rcf.generate(SEED, 300, cell)
    assert len(cases) == 300 and all(rcf.within(case) for case in cases)
    for case in cases:
        payloads = [len(case.body), *(len(rcf._bytes(f)) for f in case.frames)]
        assert max(payloads) <= 64 * 1024 and case.depth <= 16 and case.nodes <= 256 and rcf.events_of(case) <= 16


def test_the_bounds_hold_at_their_edge_and_refuse_one_past_it():
    base = rcf.generate(SEED, 1, "computer-http-strict-history-on")[0]
    edge = dataclasses.replace(base, body=b"x" * 65536, depth=16, nodes=256)
    assert rcf.within(edge)
    for past in (
        dataclasses.replace(edge, body=b"x" * 65537),
        dataclasses.replace(edge, depth=17),
        dataclasses.replace(edge, nodes=257),
        dataclasses.replace(edge, events=("review",) * 15 + ("request", "retry")),
    ):
        assert not rcf.within(past)
    assert rcf.within(dataclasses.replace(edge, events=("review",) * 15 + ("request",)))  # 16 events
    frames = dataclasses.replace(edge, body=b"", frames=("{}",) * 8, events=("request", "retry"))
    assert rcf.events_of(frames) == 16 and rcf.within(frames)


@pytest.mark.parametrize("policy", ["strict", "exception"])
@pytest.mark.parametrize("transport", ["http", "ws"])
def test_the_cases_draw_every_mutation_and_state_sequence(transport, policy):
    cases = rcf.generate(SEED, 800, f"computer-{transport}-{policy}-history-on")
    events = {event for case in cases for event in case.events}
    wanted = {"request", "retry", "cancel", "review"} | (
        {"approve", "amend", "revoke", "expire"} if policy == "exception" else set()
    )
    assert wanted <= events
    if policy == "strict":
        assert not events & {"approve", "amend", "revoke", "expire"}  # nothing is ever in force
    mutations = {m for case in cases for m in case.mutations}
    pool = set(rcf._WS_MUTATIONS if transport == "ws" else rcf._HTTP_MUTATIONS)
    assert pool <= mutations
    kinds = {p.kind for case in cases for p in case.plants}
    assert kinds == {"secret", "canary"}


def test_the_corpus_names_kinds_never_a_value():
    found = rcf.corpus(SEED, 60, ["computer-http-exception-history-on", "server-ws-strict-history-on"])
    text = json.dumps(found)
    cases = rcf.generate(SEED, 60, "computer-http-exception-history-on")
    values = {p.value for case in cases for p in case.plants}
    assert values and not any(value in text for value in values)
    assert found["bounds"] == {"payload": 65536, "depth": 16, "nodes": 256, "events": 16}
    part = found["cells"]["computer-http-exception-history-on"]
    assert part["generated"] == 60 and len(part["cases"]) == 60


def test_the_run_refuses_a_full_count_unless_asked_and_batches_one_machine_at_a_time(monkeypatch, tmp_path):
    monkeypatch.delenv("LATET_RCF_FULL", raising=False)
    assert rcf.main(["run", "--seed", "1", "--count", "101", "--out", str(tmp_path)]) == 2
    batches = rcf._batches(list(rcf.CELLS), 2)
    assert len(batches) == 8 and all(len({rcf.parts(c)[0] for c in batch}) == 1 for batch in batches)
    assert sorted(c for batch in batches for c in batch) == sorted(rcf.CELLS)


# --- the judge: each assertion catches what it names -----------------------------------------------------------------


async def _one(tmp_path, cell: str, case: rcf.FuzzCase) -> rcf.CaseResult:
    upstream = RecordingWSUpstream().start()
    try:
        async with rcf.fuzz_cell(cell, lambda: computer_cell(tmp_path, upstream), 1) as made:
            return await rcf.run_case(made, case, 0)
    finally:
        upstream.stop()


def _case(cell: str, **fields) -> rcf.FuzzCase:
    """A case of the exception's route: a chat whose text holds ``EMAIL``, with what ``fields`` change."""
    body = json.dumps({"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"write to {EMAIL}"}]})
    base = rcf.FuzzCase(
        "x",
        cell,
        "llm-chat",
        "POST",
        "/llm/v1/chat/completions",
        (("authorization", "Bearer sk-rc-test"), ("content-type", "application/json")),
        body.encode(),
        (),
        ("request",),
        (),
        (rcf.Plant("secret", "slot", EMAIL),),
        (EMAIL,),
        6,
        4,
    )
    return dataclasses.replace(base, **fields)


def _rejudge(result: rcf.CaseResult, steps=None, logs=None, case=None, history="on") -> dict[str, list[str]]:
    found, _ = rcf.judge(case or result.case, steps or result.steps, logs or result.logs, history, "computer")
    return found


CELL = "computer-http-exception-history-on"


async def test_a_clean_case_passes_every_assertion(tmp_path):
    canary = "rcfk0000000000aa"
    case = _case(
        CELL,
        headers=(*_case(CELL).headers, ("x-rcf-team", canary)),
        plants=(rcf.Plant("secret", "slot", EMAIL), rcf.Plant("canary", "header:x-rcf-team", canary)),
    )
    result = await _one(tmp_path, CELL, case)
    [step] = result.steps
    assert {name: why for name, why in result.violations.items() if why} == {}
    assert all(result.asserted[name] for name in rcf.ASSERTIONS)
    # The exception was exercised: the canary left in its own header alone, as a Pass naming the exception.
    assert rcf._excepted_out(case, step) and step.witness.facts[0][1] == "Pass" and step.witness.facts[0][5]
    assert step.records and "exception" in step.records[0].summary["passes"]


async def test_each_assertion_catches_its_violation(tmp_path):
    canary = "rcfk0000000000bb"
    case = _case(
        CELL,
        headers=(*_case(CELL).headers, ("x-rcf-team", canary)),
        plants=(rcf.Plant("secret", "slot", EMAIL), rcf.Plant("canary", "header:x-rcf-team", canary)),
    )
    result = await _one(tmp_path, CELL, case)
    [step] = result.steps
    [sent] = step.sent

    def tampered(**fields) -> list[rcf.Step]:
        return [dataclasses.replace(step, **fields)]

    def sent_as(**fields) -> list[SentRequest]:
        return [dataclasses.replace(sent, **fields)]

    # coverage: a decision without its envelope proof
    [authorized] = step.witness.authorized
    unproved = types.SimpleNamespace(
        decision=dataclasses.replace(authorized.decision, envelope_proof=None),
        attempt=authorized.attempt,
        credentials=authorized.credentials,
        payload=authorized.payload,
    )
    witness = dataclasses.replace(step.witness, authorized=[unproved])
    assert _rejudge(result, tampered(witness=witness))["coverage"]
    # ... and a body proof of another body (the same bytes, not the very ones the request carried)
    proof = authorized.decision.proof if hasattr(authorized.decision, "proof") else authorized.decision.body_proof
    broken = dataclasses.replace(proof, raw=bytes(bytearray(proof.raw)))
    field = "proof" if hasattr(authorized.decision, "proof") else "body_proof"
    unproved = types.SimpleNamespace(
        decision=dataclasses.replace(authorized.decision, **{field: broken}),
        attempt=authorized.attempt,
        credentials=authorized.credentials,
        payload=authorized.payload,
    )
    witness = dataclasses.replace(step.witness, authorized=[unproved])
    assert _rejudge(result, tampered(witness=witness))["coverage"]
    # authority: a request that reached the provider without the transport's token, and one more than was sent
    assert _rejudge(result, tampered(witness=dataclasses.replace(step.witness, tokens=[False])))["authority"]
    assert _rejudge(result, tampered(sent=[sent, sent]))["authority"]
    # bytes: what reached the provider is not what was frozen (its content, its headers)
    assert _rejudge(result, tampered(sent=sent_as(headers=sent.headers[:-1])))["bytes"]
    assert _rejudge(result, tampered(sent=sent_as(content=sent.content + b" ")))["bytes"]
    # canary: the same canary in a header no exception names, and in clear with no exception in force
    moved = (*sent.headers, (b"x-rcf-other", canary.encode()))
    assert _rejudge(result, tampered(sent=sent_as(headers=moved)))["canary"]
    assert _rejudge(result, tampered(permitted=frozenset()))["canary"]
    # masking: an original readable upstream, plainly or as a JSON escape
    assert _rejudge(result, tampered(sent=sent_as(content=sent.content + EMAIL.encode())))["masking"]
    escaped = "".join(f"\\u{ord(c):04x}" for c in EMAIL)
    assert _rejudge(result, tampered(sent=sent_as(content=b'{"m": "' + escaped.encode() + b'"}')))["masking"]
    # restoration: a fake left in the answer; the original not restored; two decisions for one request
    fake = step.fakes[EMAIL]
    assert _rejudge(result, tampered(body=step.body + fake.encode()))["restoration"]
    assert _rejudge(result, tampered(body=step.body.replace(EMAIL.encode(), b"someone")))["restoration"]
    facts = step.witness.facts
    assert _rejudge(result, tampered(witness=dataclasses.replace(step.witness, facts=facts * 2)))["restoration"]
    # sinks: a credential in a log line, a record with the history off
    assert _rejudge(result, logs=["latet.x WARNING key sk-rc-test"])["sinks"]
    assert _rejudge(result, history="off")["sinks"]
    # ownership: a hold not released, an answer left open, a peak past the bound
    assert _rejudge(result, tampered(seam={"hold": 2, "release": 1}))["ownership"]
    assert _rejudge(result, tampered(answers=(1, 0)))["ownership"]
    assert _rejudge(result, tampered(peak=rcf.MEMORY_PEAK + 1))["ownership"]


async def test_a_revoked_exception_lets_nothing_through_any_more(tmp_path):
    canary = "rcfk0000000000cc"
    case = _case(
        CELL,
        headers=(*_case(CELL).headers, ("x-rcf-team", canary)),
        events=("revoke", "request"),
        plants=(rcf.Plant("canary", "header:x-rcf-team", canary),),
    )
    result = await _one(tmp_path, CELL, case)
    [step] = result.steps
    assert step.permitted == frozenset() and step.blocked_answer and not step.sent
    assert {name: why for name, why in result.violations.items() if why} == {}


# --- D6 (plan, approved 2026-10-29): on a verify-only route an unknown string member is checked text, and passes ----

STRICT = "computer-http-strict-history-on"


def _bedrock(member: str, value, plant: rcf.Plant) -> rcf.FuzzCase:
    """Bedrock's signed call (verify-only: checked, never rewritten), its body given one member no profile names."""
    template = rcf.TEMPLATE["bedrock-signed"]
    doc = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 64,
           "messages": [{"role": "user", "content": "hello there"}], member: value}  # fmt: skip
    return rcf.FuzzCase(
        "d6", STRICT, "bedrock-signed", "POST", template.path, template.headers, json.dumps(doc).encode(), (),
        ("request",), ("field-top",), (plant,), (), 6, 3,
    )  # fmt: skip


def _left_with(step: rcf.Step, member: str, value) -> list[rcf.Step]:
    """``step`` as if what reached the provider had also carried ``member`` (a leak the judge must name)."""
    sent = step.sent[-1]
    doc = json.loads(sent.content)
    doc[member] = value
    return [dataclasses.replace(step, sent=[dataclasses.replace(sent, content=json.dumps(doc).encode())])]


async def test_a_string_canary_in_an_unknown_member_of_a_verify_only_route_passes_as_checked_only(tmp_path):
    """Ruling (a): a string canary, no secret in it, in an unknown member of Bedrock's call leaves under D6's Pass
    "checked only": the judge accepts it (the full fuzz on 2e3cea71 flagged 36 such steps)."""
    canary = "rcfk0000000000d6"
    result = await _one(tmp_path, STRICT, _bedrock("x_rcf_d6d6", canary, rcf.Plant("canary", "body:/x_rcf", canary)))
    step = result.steps[0]
    [decision] = {id(a.decision): a.decision for a in step.witness.authorized}.values()
    assert type(decision).__name__ == "Pass" and "checked only" in decision.evidence.rules
    assert any(canary in s.content.decode() for s in step.sent)  # it left, byte for byte, as D6 says
    assert {name: why for name, why in result.violations.items() if why} == {}
    # Under any other decision (a Pass by another rule, say) the same canary is a violation.
    from latet.gateway.outcome import PolicyEvidence

    other = dataclasses.replace(decision, evidence=PolicyEvidence(("sent before in this conversation",), "x"))
    authorized = [types.SimpleNamespace(decision=other, attempt=a.attempt, credentials=a.credentials,
                                        payload=a.payload) for a in step.witness.authorized]  # fmt: skip
    moved = [dataclasses.replace(step, witness=dataclasses.replace(step.witness, authorized=authorized))]
    assert _rejudge(result, moved)["canary"]


async def test_a_non_string_canary_in_that_member_is_refused_and_a_leak_of_one_is_named(tmp_path):
    """Ruling (b): the fuzz plants an object or an array holding a canary there too; the product refuses it, and one
    that left all the same (under the very Pass a string canary leaves by) is a violation."""
    drawn = [m for cell in rcf.CELLS if cell.startswith("computer-http") for case in rcf.generate(SEED, 1500, cell)
             if case.template == "bedrock-signed" for m in case.mutations]  # fmt: skip
    assert "field-object" in drawn and "secret-member" in drawn
    canary = "rcfk0000000000ob"
    refused = await _one(tmp_path, STRICT, _bedrock("x_rcf_obj", {"note": canary},
                                                    rcf.Plant("canary", "body:/x_rcf_obj", canary)))  # fmt: skip
    assert refused.steps[0].blocked_answer and not refused.steps[0].sent
    assert {name: why for name, why in refused.violations.items() if why} == {}
    string = "rcfk0000000000s6"
    (tmp_path / "pass").mkdir()
    passed = await _one(tmp_path / "pass", STRICT, _bedrock("x_rcf_d6d6", string,
                                                            rcf.Plant("canary", "body:/x_rcf", string)))  # fmt: skip
    for value in ({"note": canary}, [{"note": canary}], [[canary]]):
        case = dataclasses.replace(
            passed.case, plants=(*passed.case.plants, rcf.Plant("canary", "body:/x_rcf_obj", canary))
        )
        assert _rejudge(passed, _left_with(passed.steps[0], "x_rcf_obj", value), case=case)["canary"]


async def test_a_real_secret_in_that_member_is_still_a_violation(tmp_path):
    """Ruling (a)'s other half: a secret written in an unknown string member is refused (Bedrock's call is checked,
    never rewritten), and one that left all the same is a violation, D6 or not."""
    from ..request_coverage import EMAIL

    secret = rcf.Plant("secret", "body:/x_rcf_secret", EMAIL)
    refused = await _one(tmp_path, STRICT, _bedrock("x_rcf_secret", f"write to {EMAIL}", secret))
    assert refused.steps[0].blocked_answer and not refused.steps[0].sent
    assert {name: why for name, why in refused.violations.items() if why} == {}
    string = "rcfk0000000000s7"
    (tmp_path / "pass").mkdir()
    passed = await _one(tmp_path / "pass", STRICT, _bedrock("x_rcf_d6d6", string,
                                                            rcf.Plant("canary", "body:/x_rcf", string)))  # fmt: skip
    case = dataclasses.replace(passed.case, plants=(*passed.case.plants, secret))
    found = _rejudge(passed, _left_with(passed.steps[0], "x_rcf_secret", f"write to {EMAIL}"), case=case)
    assert found["masking"]
    # Sent at all, the secret masked or not: a verify-only call is never rewritten, so it was not refused.
    assert _rejudge(passed, passed.steps, case=case)["masking"]
    # A canary that holds a secret is no D6 canary: its leak is named too.
    held = f"{string}-{EMAIL}"
    case = dataclasses.replace(passed.case, plants=(rcf.Plant("canary", "body:/x_rcf", held),))
    assert _rejudge(passed, _left_with(passed.steps[0], "x_rcf_d6d6", held), case=case)["canary"]


# --- the smoke: each computer cell, a fixed seed ---------------------------------------------------------------------


@pytest.mark.parametrize("cell", COMPUTER_CELLS)
async def test_the_smoke_of_each_computer_cell(tmp_path, cell):
    upstream = RecordingWSUpstream().start()
    try:
        async with rcf.fuzz_cell(cell, lambda: computer_cell(tmp_path, upstream), 4) as made:
            report = await rcf.fuzz(made, SEED, SMOKE)
    finally:
        upstream.stop()
    found = report.as_json()
    assert found["finished"] and found["executed"] == found["generated"] == SMOKE, found
    assert found["violated"] == {} and found["errors"] == {}, found["samples"]
    assert {"coverage", "authority", "bytes", "masking", "restoration", "sinks", "ownership"} <= set(found["asserted"])
    assert found["outcomes"].get("sent", 0) >= 1 and found["outcomes"].get("blocked", 0) >= 1, found["outcomes"]


async def _fuzz_the_computer(tmp_path):
    """``scripts/request_coverage_fuzz.py run``: the computer's cells of ``LATET_RCF_CELLS``, each in a fresh folder."""
    upstream = RecordingWSUpstream().start()
    made = [0]

    def fresh(cell):
        made[0] += 1
        folder = tmp_path / f"cell{made[0]}"
        folder.mkdir()
        return computer_cell(folder, upstream)

    try:
        await rcf.run_machine_cells(fresh)
    finally:
        upstream.stop()


if os.environ.get("LATET_RCF_OUT"):  # collected for the script alone
    test_fuzz_the_computer_for_the_script = _fuzz_the_computer
