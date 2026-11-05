"""Finite request-local projections for duplicate confidentiality admission."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

from latet.bridge.api import Bridge, _ProtectedRequest
from latet.engine.response_guard import safe_text
from latet.engine.vault import Vault
from latet.gateway import body_coverage, coverage
from latet.gateway.body_coverage import PROTECTED_LAUNCH, BodyRefused, Ledger, declared_body_safe
from latet.gateway.mediate import _projected_request_safe
from latet.protocols.base import BodyScan

from .test_request_launch_confidentiality import _SHAPE
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _sinks

_SHORT = "latet_lk_notes"
_ESCAPED = "%6C" + _SHAPE[1:]


@pytest.mark.parametrize("protected", [False, True], ids=["ordinary-combinations", "protected-combination"])
def test_shadow_projection_combines_discriminator_and_payload(protected: bool) -> None:
    key = r"\u0025" + _ESCAPED[1:] if protected else _SHORT
    argument = '{"' + key + '":"ordinary"}'
    assert safe_text(argument), "the plain D6 role must admit the complete serialized unit"
    raw = (
        '{"model":"m","input":[{"type":"function_call","type":"message","name":"query","call_id":"call_1",'
        '"arguments":' + json.dumps(argument) + ',"arguments":"{}","content":"ordinary"}]}'
    ).encode()
    assert len(raw) < 1024
    ledger = Ledger()
    ledger.preflight(raw)
    stop = len(ledger.raw)
    readings = []
    for body in ledger.shadow_projections(raw, stop):
        readings.append(_projected_request_safe(body, "responses", OPENAI, ledger, None))
        del body
    assert readings == [True, True, True, not protected]
    assert ledger.shadow_work == 4 * stop and len(ledger.raw) == stop


def _wrapped(content: str) -> str:
    inner = {"messages": [{"role": "user", "content": content}]}
    return base64.b64encode(json.dumps(inner).encode()).decode()


@pytest.mark.parametrize("case", ["ordinary", "protected", "malformed"])
def test_shadow_projection_counted_source_regions(case: str) -> None:
    losing = "invalid-base64!" if case == "malformed" else _wrapped(_ESCAPED if case == "protected" else _SHORT)
    raw = ('{"input":{"invokeModel":{"body":"' + losing + '","body":"' + _wrapped("ordinary") + '"}}}').encode()
    assert len(raw) < 1024
    ledger = Ledger()
    ledger.preflight(raw)
    stop = len(ledger.raw)
    ledger.embedded(base64.b64decode(_wrapped("ordinary"), validate=True))
    original = tuple(ledger.raw)
    root = ledger.links[(-1, ("root", 0))]
    readings = []
    refused = False
    try:
        for body in ledger.shadow_projections(raw, stop):
            readings.append(_projected_request_safe(body, "anthropic", OPENAI, ledger, "bedrock-count"))
            del body
    except BodyRefused as error:
        refused = error.code == "body-shadow wrapper"
    assert readings == ([True] if case == "malformed" else [True, case == "ordinary"])
    assert refused is (case == "malformed")
    assert tuple(ledger.raw) == original and ledger.links[(-1, ("root", 0))] == root


def test_shadow_projection_charges_before_copy_or_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b'{"input":"ordinary","input":"ordinary winner"}'
    ledger = Ledger(replace(coverage.LIMITS, occurrences=9))
    ledger.preflight(raw)
    assert len(ledger.raw) == 5

    def forbidden_decode(*args: object, **kwargs: object) -> None:
        pytest.fail("projection decode began without a full work charge")

    monkeypatch.setattr(body_coverage.json, "loads", forbidden_decode)
    with pytest.raises(BodyRefused) as caught:
        next(ledger.shadow_projections(raw, len(ledger.raw)))
    assert caught.value.code == "coverage-limit" and ledger.shadow_work == 0


def _repeated(repeats: int, value: bytes = b'"ordinary"') -> bytes:
    """A Responses body whose ``input`` is given ``repeats`` + 1 times: one shadow group of as many choices."""
    return b'{"model":"m","input":' + value + (b',"input":' + value) * repeats + b"}"


def _projected(raw: bytes, ledger: Ledger) -> tuple[list[bool], list[int]]:
    """Every projection of ``raw`` read by the closed declaration, and the ledger's retained bytes as each is read."""
    ledger.preflight(raw)
    readings, during = [], []
    for body in ledger.shadow_projections(raw, len(ledger.raw)):
        during.append(ledger.retained)
        readings.append(_projected_request_safe(body, "responses", OPENAI, ledger, None))
        del body
    return readings, during


def test_a_repeated_key_costs_the_ledger_bytes_linear_in_its_copies() -> None:
    """Each projection's materialization is reserved before it is made and released once it is read: the ledger
    retains its entries, links and shadow index, and one live projection at most, never every projection made
    (it grew by about 3.5 KB more for each further copy, and refused a short key given about 100 times)."""
    after, peaks = [], []
    for repeats in range(1, 65):
        raw = _repeated(repeats)
        ledger = Ledger()
        readings, during = _projected(raw, ledger)
        assert readings == [True] * (repeats + 1)  # every combination still materialized and read
        stop = len(ledger.raw)
        assert ledger.shadow_work == (repeats + 1) * stop  # the work counted on, cumulative
        reservation = 768 * stop + 12 * len(raw)
        assert max(during) - ledger.retained <= reservation  # one live projection reserved at a time
        after.append(ledger.retained)
        peaks.append(max(during))
    steps = {later - earlier for earlier, later in pairwise(after)}
    assert len(steps) == 1 and steps.pop() <= 3200, steps
    growth = [later - earlier for earlier, later in pairwise(peaks)]
    assert max(growth) - min(growth) <= 64, growth  # the peak is linear too: one reservation on top


def test_a_hundred_copies_of_a_short_key_pass_in_about_linear_memory() -> None:
    import tracemalloc

    def traced(repeats: int) -> tuple[int, int]:
        raw = _repeated(repeats)
        ledger = Ledger()
        tracemalloc.start()
        try:
            readings, _ = _projected(raw, ledger)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert readings == [True] * (repeats + 1)
        return peak, ledger.retained

    (half, half_retained), (whole, whole_retained) = traced(50), traced(100)
    assert whole_retained < 400_000 and whole_retained < 2.2 * half_retained
    assert whole < 2.5 * half, (half, whole)


def test_a_pathological_repetition_ends_in_the_work_limit() -> None:
    """The work of reading every projection is still counted, cumulatively, against the occurrence limit: a key given
    hundreds of times ends in the fixed coverage limit, refused from its work, its bytes far within their limit."""
    raw = _repeated(300)
    ledger = Ledger()
    with pytest.raises(BodyRefused) as caught:
        _projected(raw, ledger)
    assert caught.value.code == "coverage-limit"
    stop = len(ledger.raw)
    used = ledger.envelope + len(ledger.raw) + ledger.declared_occurrences + ledger.shadow_work
    assert used + stop > coverage.LIMITS.occurrences
    assert ledger.retained + 768 * stop + 12 * len(raw) < coverage.LIMITS.ledger_bytes // 8


def test_one_projection_past_the_remaining_bytes_is_refused_before_it_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _repeated(2)
    probe = Ledger()
    probe.preflight(raw)
    stop = len(probe.raw)
    floor = probe.retained + 256 * stop  # its entries and the shadow index
    reservation = 768 * stop + 12 * len(raw)
    fits = Ledger(replace(coverage.LIMITS, ledger_bytes=floor + reservation))
    assert _projected(raw, fits)[0] == [True, True, True] and fits.retained == floor
    short = Ledger(replace(coverage.LIMITS, ledger_bytes=floor + reservation - 1))
    short.preflight(raw)

    def forbidden_decode(*args: object, **kwargs: object) -> None:
        pytest.fail("a projection was materialized past the ledger's bytes")

    monkeypatch.setattr(body_coverage.json, "loads", forbidden_decode)
    with pytest.raises(BodyRefused) as caught:
        next(short.shadow_projections(raw, stop))
    assert caught.value.code == "coverage-limit" and short.shadow_work == 0


@pytest.mark.parametrize("at", ["first", "middle", "second-to-last"])
def test_a_launch_key_in_any_copy_not_read_is_refused(at: str) -> None:
    copies = [b'"ordinary"'] * 9
    index = {"first": 0, "middle": 4, "second-to-last": 7}[at]
    copies[index] = json.dumps(_ESCAPED).encode()
    raw = b'{"model":"m",' + b",".join(b'"input":' + copy for copy in copies) + b"}"
    ledger = Ledger()
    readings, _ = _projected(raw, ledger)
    assert readings.count(False) == 1 and len(readings) == 9


def _large(copies: int, size: int = 1300 * 1024) -> bytes:
    """A chat request with one large text and a short top-level key given ``copies`` times (review M-2's probe)."""
    text = ("lorem ipsum dolor sit amet " * (size // 27 + 1))[:size]
    repeated = ",".join(['"temperature":1'] * copies)
    return ('{"model":"gpt-4o","messages":[{"role":"user","content":"' + text + '"}],' + repeated + "}").encode()


def _read_all(raw: bytes, ledger: Ledger, protocol: str | None = None) -> tuple[list[bool], str | None]:
    """Every projection of ``raw`` read (by the closed declaration when ``protocol`` is given), and the code of the
    refusal that stopped them, if one did."""
    ledger.preflight(raw)
    readings: list[bool] = []
    try:
        for body in ledger.shadow_projections(raw, len(ledger.raw)):
            readings.append(_projected_request_safe(body, protocol, OPENAI, ledger, None) if protocol else True)
            del body
    except BodyRefused as error:
        return readings, error.code
    return readings, None


def test_the_bytes_read_across_projections_are_bounded() -> None:
    """Review M-2: each projection materializes and reads the whole body, so the body bytes all of a request's
    projections read are counted, cumulatively, against a fixed ceiling (``shadow_bytes``), before each is made: a
    large body with a short key given hundreds of times ends in the fixed coverage limit after a few projections,
    where it passed after 215 readings of 1.3 MiB (1.7 s under the interpreter's lock)."""
    import time

    raw = _large(215)
    ledger = Ledger()
    started = time.perf_counter()
    readings, refused = _read_all(raw, ledger)
    assert refused == "coverage-limit" and time.perf_counter() - started < 1.0
    assert len(readings) == coverage.LIMITS.shadow_bytes // len(raw)
    assert ledger.shadow_bytes == len(readings) * len(raw) <= coverage.LIMITS.shadow_bytes
    used = ledger.envelope + len(ledger.raw) + ledger.declared_occurrences + ledger.shadow_work
    assert used + len(ledger.raw) <= coverage.LIMITS.occurrences  # refused for its bytes, its work within its limit


@pytest.mark.parametrize("beyond", [False, True])
def test_the_projected_bytes_edge_is_exact_and_precedes_any_copy(monkeypatch: pytest.MonkeyPatch, beyond: bool) -> None:
    raw = _repeated(2)  # three projections of the whole body
    ledger = Ledger(replace(coverage.LIMITS, shadow_bytes=3 * len(raw) - int(beyond)))
    real = Ledger.shadow_projections
    decode = body_coverage.json.loads

    def watched(self: Ledger, data: bytes, stop: int):  # type: ignore[no-untyped-def]
        for count, body in enumerate(real(self, data, stop), 1):
            yield body
            if count == 2 and beyond:  # the third is refused before anything of it is copied or decoded
                monkeypatch.setattr(body_coverage.json, "loads", lambda *a, **k: pytest.fail("decoded past the edge"))
        monkeypatch.setattr(body_coverage.json, "loads", decode)

    monkeypatch.setattr(Ledger, "shadow_projections", watched)
    readings, refused = _read_all(raw, ledger, "responses")
    monkeypatch.setattr(body_coverage.json, "loads", decode)
    assert readings == [True] * (2 if beyond else 3)
    assert refused == ("coverage-limit" if beyond else None)
    assert ledger.shadow_bytes == len(readings) * len(raw)


def test_a_large_body_with_a_key_given_many_times_gets_the_fixed_422() -> None:
    from .test_request_coverage import gateway

    with gateway() as (client, transport, _clients, _mediator):
        client.headers.clear()
        response = client.post(
            "/openai/v1/chat/completions", content=_large(215), headers={"content-type": "application/json"}
        )
    assert response.status_code == 422 and not transport.sent
    assert "request coverage limit exceeded" in response.text


@pytest.mark.parametrize(("repeats", "sent"), [(100, True), (300, False)])
def test_a_key_given_many_times_is_sent_until_the_work_limit(repeats: int, sent: bool) -> None:
    from .test_request_coverage import gateway

    raw = b'{"model":"gpt-4o","prompt":"x"' + b',"prompt":"x"' * repeats + b"}"
    with gateway() as (client, transport, _clients, _mediator):
        client.headers.clear()
        response = client.post("/openai/v1/completions", content=raw, headers={"content-type": "application/json"})
    if sent:
        assert response.status_code == 200 and len(transport.sent) == 1
        assert json.loads(transport.sent[-1].content)["prompt"] == "x"
    else:
        assert response.status_code == 422 and not transport.sent
        assert "request coverage limit exceeded" in response.text


@pytest.mark.parametrize("protected", [False, True], ids=["ordinary-d6", "protected-d6"])
def test_declaration_screens_unknown_d6_text(protected: bool) -> None:
    text = _ESCAPED if protected else _SHORT
    scan = BodyScan()
    assert declared_body_safe({text: ["ordinary"]}, "chat", OPENAI, scan, Ledger()) is not protected
    assert declared_body_safe({"extension": [text]}, "chat", OPENAI, scan, Ledger()) is not protected


@pytest.mark.parametrize("protected", [False, True], ids=["ordinary-echo", "protected-echo"])
def test_bridge_mask_screens_non_string_echo_before_prefetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected: bool
) -> None:
    text = {"entry": [_ESCAPED if protected else _SHORT]}
    data = {"session": "shadow-echo", "items": [{"text": text}]}
    entered = False
    prefetch = Vault.prefetch

    def observe(vault, values):
        nonlocal entered
        entered = True
        return prefetch(vault, values)

    monkeypatch.setattr(Vault, "prefetch", observe)
    with _sinks(tmp_path) as (mediator, recorder, _):
        bridge = Bridge(mediator, "bridge-fixture-token", recorder)
        if protected:
            with pytest.raises(_ProtectedRequest, match=PROTECTED_LAUNCH):
                bridge._mask(data)
        else:
            _, out, _, _, _ = bridge._mask(data)
            assert len(out) == 1 and out[0].get("text") is text and out[0].get("count") == 0
    assert entered is not protected
