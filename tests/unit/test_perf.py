"""Overhead budgets (generous, so slow CI runners pass; scripts/bench.py prints the real numbers).

Targets from the plan: an incremental turn on a ~1 MB conversation under 30 ms p50 locally, and
streaming rewrite under 1 ms per event.
"""

import json
import statistics
import time

import pytest

from latet.config import Config
from latet.engine.build import build
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import IdentityDetector, LocalIdentity, TermsDetector
from latet.engine.vault import Vault
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream

from ..conftest import ROOT_KEY
from .test_terms_bench import bench  # scripts/terms_bench.py

pytestmark = pytest.mark.timing  # every test here is a budget of time

UPSTREAM = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
HEADERS = {"x-api-key": "test", "content-type": "application/json"}
CODE = "def handler(event, context):\n    value = compute(event['x']) + 1\n    return {'ok': True, 'v': value}\n"


def conversation(turns: int, extra: int = 0) -> dict:
    messages = []
    for i in range(turns):
        file_body = CODE * 230 + f"API_KEY=sk_live_{i:04d}Zq8kLm2pXw9Ab7Cd5Ef3Gh1Jk\nowner{i}@acme.it\n"
        messages.append(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": file_body}]}
        )
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"Read file {i}, all good."}]})
    for j in range(extra):
        messages.append({"role": "user", "content": f"new question {j}: " + CODE * 100})
    return {"model": "x", "system": "You are a helpful agent. " * 250, "messages": messages, "stream": True}


def _mediator() -> Mediator:
    built = build(Config(), Vault(ROOT_KEY))
    return Mediator(built.engine, built.verifier)


def test_incremental_turn_overhead():
    mediator = _mediator()
    base = conversation(40)
    raw = json.dumps(base).encode()
    assert len(raw) > 900_000
    t0 = time.perf_counter()
    mediator.prepare(UPSTREAM, "POST", "/anthropic/v1/messages", HEADERS, raw)
    cold = time.perf_counter() - t0
    warm = []
    for k in range(1, 6):
        body = conversation(40, extra=k)
        raw = json.dumps(body).encode()
        t0 = time.perf_counter()
        prepared = mediator.prepare(UPSTREAM, "POST", "/anthropic/v1/messages", HEADERS, raw)
        warm.append(time.perf_counter() - t0)
        assert b"Zq8kLm2pXw9" not in prepared.body
    p50 = statistics.median(warm)
    print(f"\ncold 1 MB: {cold * 1000:.0f} ms; incremental p50: {p50 * 1000:.1f} ms")
    assert p50 < 0.25  # generous for CI; ~30 ms locally


def test_stream_rewrite_per_event_cost():
    from latet.engine.rehydrate import restores_for
    from latet.engine.types import EntityClass
    from latet.protocols.rewrite import AnthropicStream, RestoreContext

    vault = Vault(ROOT_KEY)
    entities = [vault.intern("ASSIGNED_SECRET", EntityClass.SECRET, f"secret-value-{i:03d}-xyz") for i in range(200)]
    ctx = RestoreContext(restores_for(entities), restores_for(entities, json_mode=True))
    stream = AnthropicStream(ctx)
    stream.on_event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    text = " ".join(e.surrogate for e in entities) * 3
    pieces = [text[i : i + 20] for i in range(0, len(text), 20)]
    t0 = time.perf_counter()
    for piece in pieces:
        stream.on_event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}})
    per_event = (time.perf_counter() - t0) / len(pieces)
    print(f"\nper event: {per_event * 1e6:.0f} µs over {len(pieces)} events")
    assert per_event < 0.002  # generous for CI; well under 1 ms locally


def test_fifty_thousand_terms_are_matched_within_the_budget():
    terms = bench.make_terms(50_000)
    texts = [bench.make_text(100_000, [t.value for t in terms[::1000]], seed) for seed in range(3)]
    t0 = time.perf_counter()
    detector = TermsDetector(terms)
    built = time.perf_counter() - t0
    t0 = time.perf_counter()
    found = [list(detector.detect(text, DetectContext())) for text in texts]
    per_text = (time.perf_counter() - t0) / len(texts)
    print(f"\n50,000 terms: built in {built:.2f} s, {per_text * 1000:.1f} ms per 100 KB")
    assert all(found)
    assert built < 10.0  # generous for CI; about 0.2 s locally (one RE2 alternation did not build at all)
    assert per_text < 0.25  # generous for CI; about 16 ms locally (2.8 s with 10,000 terms before)


def test_a_text_full_of_terms_is_matched_within_the_budget():
    # A git log names its author in every commit: each name found must not cost a pass over the whole text.
    entry = "commit 4edc1f4\nAuthor: Emily Carter <emily.carter@acme.com>\nDate:   Mon Oct 12 2026\n\n    Fix the parser\n\n"
    log = (entry * (100_000 // len(entry) + 1))[:100_000]
    detector = IdentityDetector(LocalIdentity(full_name="Emily Carter", emails=("emily.carter@acme.com",)))
    t0 = time.perf_counter()
    found = list(detector.detect(log, DetectContext()))
    took = time.perf_counter() - t0
    print(f"\n{len(found)} terms in a 100 KB git log: {took * 1000:.0f} ms")
    assert len(found) > 2500  # the name, the address and its domain in each of about 950 commits
    assert took < 0.25  # generous for CI; about 0.8 s when RE2's binding encoded the whole text for each name found
