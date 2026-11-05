"""The history's public summary (P2-5's safe view) as the dashboard and the tutorial read it, by root's ruling of
2026-11-03 and its second round: a rewritten exchange says `rewrite`; every stopped one carries the blocked flag and
its fixed category code (``BLOCK_CODES``), its words staying in the full history's private view; the session is the
opaque keyed conversation key; the host is a provider's public endpoint, any other `custom` (keyed in the private
view, as a destination of one's own is); the path is the route template the envelope admitted; the verified fact, the
signing as a boolean, the model a provider's grammar admits, the entity types of Latet's own detectors. Never a value,
a body, a header, a query value, a profile's or a destination's name, whatever the producer is handed and whatever an
old row holds."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import pytest

import latet.gateway.request_profiles as request_profiles
from latet.engine.types import SlotKind
from latet.gateway.mediate import REWRITE, VERIFY, Prepared
from latet.gateway.outcome import BLOCK_CODES
from latet.gateway.recorder import Outcome, Recorder
from latet.protocols.base import SlotRef
from latet.storage.exchanges import ExchangeRecord, ExchangeStore, safe_summary

from ..conftest import ROOT_KEY

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
BODY = "P25-SUMMARY-BODY-CANARY-58213"
HEADER = "P25-SUMMARY-HEADER-CANARY-90417"
QUERY = "P25-SUMMARY-QUERY-CANARY-33178"
SESSION = "P25-SUMMARY-SESSION-CANARY-27741"
REASON = f"sensitive values would leave in clear: GITHUB_PAT[40] {BODY}"
CANARIES = (GH_PAT, BODY, HEADER, QUERY, SESSION, "GITHUB_PAT[40]", "would leave in clear", "sk-")
TEMPLATE = "/{destination}/v1/messages"
OPAQUE = re.compile(r"hg:l1:[0-9a-f]{64}:[0-9a-f]{64}")


def _texts() -> list[str]:
    return [f"deploy with {GH_PAT} and {BODY}"]


def _outcome(
    prepared: Prepared,
    *,
    path: str = TEMPLATE,
    block_code: str | None = None,
    upstream: str = "anthropic",
    host: str = "api.anthropic.com",
) -> Outcome:
    stopped = bool(prepared.block_reason or block_code)
    outcome = Outcome(
        {"user-agent": f"claude-cli/2.1.291 {HEADER}", "authorization": "Bearer sk-ant-" + HEADER},
        upstream,
        path,
        "http",
        time.time(),
        prepared,
        400 if stopped else 200,
        request_headers=[("authorization", "Bearer sk-ant-" + HEADER), ("x-canary", HEADER)],
        host=host,
    )
    if not stopped:
        outcome.forwarded = outcome.started + 0.004
    outcome.block_code = block_code
    return outcome


def _prepared(mode: str, reason: str | None = None) -> Prepared:
    texts = _texts()
    return Prepared(
        mode,
        "anthropic",
        json.dumps(texts).encode(),
        None,
        document=texts,
        pending=[(SlotRef(texts, 0, SlotKind.USER_TEXT), [], False)],
        block_reason=reason,
        model="claude-sonnet-4-5",
    )


def _recorded(tmp_path: Path, history: str, outcome: Outcome) -> tuple[ExchangeStore, dict, dict, dict]:
    """What the producer published live, the list row and the detail of ``outcome`` recorded through the real
    recorder into the real store."""
    store = ExchangeStore(tmp_path / f"{history}.db", ROOT_KEY)
    recorder = Recorder(store, history=history)

    async def scenario() -> dict:
        queue = recorder.broadcaster.subscribe()
        recorder.finish(outcome)
        return await asyncio.wait_for(queue.get(), timeout=10)

    try:
        live = asyncio.run(scenario())
        assert recorder.drain(timeout=10)
    finally:
        recorder.close()
    [row] = store.list()[0]
    detail = store.detail(row["id"])
    assert detail is not None
    return store, live, row, detail


@pytest.mark.parametrize("history", ["full", "metadata"])
def test_a_rewritten_exchange_says_rewrite(tmp_path, history):
    store, live, row, detail = _recorded(tmp_path, history, _outcome(_prepared(REWRITE)))
    try:
        for summary in (live, row, detail["summary"]):
            assert summary["mode"] == "rewrite", "a rewritten exchange never reads passthrough"
            assert summary["protocol"] == "anthropic" and summary["path"] == TEMPLATE
            assert summary["host"] == "api.anthropic.com"
            assert summary["session"] == store.history_group("unscoped") and OPAQUE.fullmatch(summary["session"])
            assert summary["block_reason"] is None and "blocked" not in summary["flags"]
            assert summary["verified"] is True, "rewritten with nothing left in clear: the verified fact"
            assert summary["model"] == "claude-sonnet-4-5" and summary["signed"] is False
            assert type(summary["overhead_ms"]) is int
    finally:
        store.close()


@pytest.mark.parametrize("history", ["full", "metadata"])
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, "refused"),  # 0.10's preparation refused it: its words alone, as the plugin's checks are recorded too
        ("refused", "refused"),
        ("masking-failed", "masking-failed"),
        ("too-large", "too-large"),
        ("credentials", "credentials"),
        ("envelope-sensitive", "envelope-sensitive"),
        ("P25-UNKNOWN-CODE", "refused"),  # never a producer's text: a code outside the catalog is the generic one
    ],
)
def test_a_blocked_exchange_carries_its_category(tmp_path, history, code, expected):
    store, live, row, detail = _recorded(tmp_path, history, _outcome(_prepared(VERIFY, REASON), block_code=code))
    try:
        for summary in (live, row, detail["summary"]):
            assert summary["block_reason"] == expected and expected in BLOCK_CODES
            assert "blocked" in summary["flags"] and summary["mode"] == "verify"
            assert summary["path"] == TEMPLATE and summary["host"] == "api.anthropic.com"
            assert summary["verified"] is False and summary["overhead_ms"] is None  # never sent
        private = detail["sensitive"]
        if history == "full":  # the words told the agent stay in the separately sealed private view
            assert private is not None and private["block_reason"] == REASON
        else:
            assert private is None
    finally:
        store.close()


@pytest.mark.parametrize("history", ["full", "metadata"])
def test_the_summary_never_carries_a_value_a_header_a_query_or_the_body(tmp_path, history):
    prepared = _prepared(VERIFY, REASON)
    outcome = _outcome(prepared, path=f"/anthropic/v1/messages/{SESSION}?key={QUERY}", block_code="refused")
    outcome.host = f"user:{HEADER}@api.anthropic.com"
    store, live, row, detail = _recorded(tmp_path, history, outcome)
    try:
        (clear,) = store._db.execute("SELECT summary FROM exchanges").fetchone()
        public = json.dumps([live, row, detail["summary"], clear])
        assert not [canary for canary in CANARIES if canary in public]
        for summary in (live, row, detail["summary"]):
            assert summary["path"] == "/gateway", "a path outside the admitted templates is shown by its area"
            assert summary["host"] == "custom", "a host that is no provider's public endpoint is not shown"
            assert summary["block_reason"] == "refused"
    finally:
        store.close()


@pytest.mark.parametrize("history", ["full", "metadata"])
@pytest.mark.parametrize("code", ["proof-incomplete", "evidence", "coverage-limit", "egress-refused"])
def test_every_block_the_final_decision_makes_carries_the_blocked_flag_and_its_category(tmp_path, history, code):
    """A refusal its arena has no words for (a proof that leaves a part unaccounted for, evidence the rules do not
    admit, the coverage limit): stopped all the same, by its code."""
    store, live, row, detail = _recorded(tmp_path, history, _outcome(_prepared(VERIFY), block_code=code))
    try:
        for summary in (live, row, detail["summary"]):
            assert "blocked" in summary["flags"] and summary["block_reason"] == code
            assert summary["verified"] is False and summary["overhead_ms"] is None
    finally:
        store.close()


@pytest.mark.parametrize("history", ["full", "metadata"])
def test_a_destination_of_ones_own_is_custom_and_keyed_in_the_private_view(tmp_path, history):
    name, host = "acme-llm", "llm.acme-internal.example:8443"
    store, live, row, detail = _recorded(tmp_path, history, _outcome(_prepared(REWRITE), upstream=name, host=host))
    try:
        (clear,) = store._db.execute("SELECT summary FROM exchanges").fetchone()
        for summary in (live, row, detail["summary"]):
            assert summary["upstream"] == "custom" and summary["host"] == "custom"
        everything = json.dumps([live, row, detail, clear])
        assert name not in everything and "acme-internal" not in everything
        private = detail["sensitive"]
        if history == "full":  # keyed there too, as the store's own index keys the destination's name
            assert private["upstream"].startswith("dest:v1:") and private["host"].startswith("host:v1:")
        else:
            assert private is None
    finally:
        store.close()


@pytest.mark.parametrize("history", ["full", "metadata"])
def test_a_stopped_values_type_is_shown_and_its_preview_is_private(tmp_path, history):
    from latet.engine.types import EntityClass
    from latet.engine.verifier import Leak

    leak = Leak("GITHUB_PAT", EntityClass.SECRET, "gitleaks:github-pat", SlotKind.USER_TEXT, "GITHUB_PAT[40]", True)
    prepared = _prepared(VERIFY, REASON)
    prepared.leaks = [leak]
    store, _live, row, detail = _recorded(tmp_path, history, _outcome(prepared, block_code="refused"))
    try:
        [shown] = detail["leaks"]
        assert shown["type"] == "GITHUB_PAT" and shown["kind"] == "user_text"
        if history == "metadata":
            assert "preview" not in shown, "the preview is in the full history's private view alone"
        assert "GITHUB_PAT[40]" not in json.dumps(row)
    finally:
        store.close()


# --- the projection itself, for any row it is handed (a producer's, or an old one's) ------------------------------


def _templates() -> set[str]:
    """Every route template request_profiles writes literally (f-strings expanded below by hand)."""
    source = Path(request_profiles.__file__).read_text("utf-8")
    found = set(re.findall(r'"(/[^"(\[\\?*+^$|]*\{[a-z-]+\}[^"(\[\\?*+^$|]*)"', source))
    found.discard("/{destination}/{unsupported-route}")  # a refusal's, never recorded
    return {template for template in found if "{{" not in template}  # f-strings: expanded in _EXPANDED


_EXPANDED = {
    "/{destination}/v1/messages",
    "/{destination}/v1/messages/count_tokens",
    "/{destination}/v1/chat/completions",
    "/{destination}/v1/completions",
    "/{destination}/v1/responses",
    "/{destination}/v1/embeddings",
    "/{destination}/chat/completions",
    "/{destination}/completions",
    "/{destination}/embeddings",
    "/{destination}/responses",
    "/backend-api/codex/models",
    "/backend-api/codex/responses",
    "/plugin/route-check",
    "/plugin/tool-call",
    "/plugin/final-check",
}


def test_every_admitted_route_template_and_plugin_check_is_shown_as_it_is():
    templates = _templates()
    assert len(templates) >= 14, "the scan of request_profiles' templates found too few"
    for path in sorted(templates | _EXPANDED):
        assert safe_summary({"path": path})["path"] == path


@pytest.mark.parametrize(
    "path",
    [
        "/anthropic/v1/messages?beta=" + QUERY,
        "/u/" + SESSION + "/openai/v1/chat/completions",
        "/vertex/v1/projects/" + SESSION + "/locations/us-east5/publishers/anthropic/models/m:rawPredict",
        "/{destination}/v1/models/" + QUERY,
        "/{destination}/{unsupported-route}",
        "/plugin/" + SESSION,
        "/" + BODY,
        "/bedrock/us-east-1/model/us.anthropic.claude-opus-5-5/invoke",  # an old row's raw path: its area alone
    ],
)
def test_any_other_path_is_shown_by_its_area_alone(path):
    shown = safe_summary({"path": path})["path"]
    assert shown in ("/gateway", "/other") and QUERY not in shown and SESSION not in shown


@pytest.mark.parametrize("code", sorted(BLOCK_CODES))
def test_every_block_category_is_shown(code):
    assert safe_summary({"block_reason": code})["block_reason"] == code


@pytest.mark.parametrize(
    "reason",
    [REASON, "masking failed: no fake is free", "request envelope refused (envelope-sensitive)", 7, None, ""],
)
def test_an_old_rows_words_are_never_shown_as_a_category(reason):
    assert safe_summary({"block_reason": reason})["block_reason"] is None


@pytest.mark.parametrize(
    "host",
    [
        "api.anthropic.com",
        "api.openai.com",
        "chatgpt.com",
        "openrouter.ai",
        "generativelanguage.googleapis.com",
        "bedrock-runtime.eu-west-1.amazonaws.com",
        "bedrock.us-east-1.amazonaws.com",
        "us-east5-aiplatform.googleapis.com",
        "aiplatform.googleapis.com",
        "aiplatform.eu.rep.googleapis.com",
    ],
)
def test_a_providers_public_endpoint_is_shown(host):
    assert safe_summary({"host": host})["host"] == host


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:37479",
        "localhost:8080",
        "[::1]:5283",
        "llm.acme.com",
        "bedrock-runtime.mars-north-9.amazonaws.com",
        "api.anthropic.com.evil.example",
        f"user:{HEADER}@llm.acme.com",
        "P25_PRIVATE_HOST_1",
        "llm.acme.com/" + QUERY,
        "x" * 300,
        "custom",
    ],
)
def test_any_other_host_is_custom(host):
    assert safe_summary({"host": host})["host"] == "custom"


@pytest.mark.parametrize("host", [None, "", 5, ["api.anthropic.com"]])
def test_no_host_is_none(host):
    assert safe_summary({"host": host})["host"] is None


@pytest.mark.parametrize(
    ("signed", "expected"),
    [("aws-profile:work", True), ("aws-session", True), (True, True), (None, False), (False, False), ("work", False)],
)
def test_signed_is_a_boolean_and_never_the_profiles_name(signed, expected):
    shown = safe_summary({"signed": signed})
    assert shown["signed"] is expected and "work" not in json.dumps(shown)


@pytest.mark.parametrize(
    "model",
    ["claude-sonnet-4-5", "gpt-4.1-mini", "gpt-5.2-codex", "us.anthropic.claude-opus-5-5", "gemini-2.5-pro"],
)
def test_a_model_a_providers_grammar_admits_is_shown(model):
    assert safe_summary({"model": model})["model"] == model


@pytest.mark.parametrize(
    "model", ["anthropic/claude-sonnet-4.5", "openai/gpt-4o", "google/gemini-2.5-pro", "openrouter/auto"]
)
def test_an_openrouter_vendor_id_its_grammar_admits_is_shown(model):
    """OpenRouter's body model is read by OpenRouter's grammar (P2-8a, ruling 6): its history shows that model too."""
    assert safe_summary({"model": model})["model"] == model


@pytest.mark.parametrize(
    "model",
    [
        "m",
        BODY,
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/x",
        "my-deployment",
        "meta-llama/llama-3.1-70b-instruct",
        5,
        None,
    ],
)
def test_any_other_model_is_not(model):
    assert safe_summary({"model": model})["model"] is None


@pytest.mark.parametrize(("verified", "expected"), [(True, True), (False, False), (None, False), ("yes", False)])
def test_the_verified_fact_is_carried(verified, expected):
    assert safe_summary({"verified": verified})["verified"] is expected


def test_a_time_not_known_is_none():
    assert safe_summary({})["overhead_ms"] is None and safe_summary({"overhead_ms": None})["overhead_ms"] is None
    assert (
        safe_summary({"overhead_ms": 7})["overhead_ms"] == 7 and safe_summary({"latency_ms": 120})["latency_ms"] == 120
    )
    assert (
        safe_summary({"latency_ms": -1})["latency_ms"] is None
        and safe_summary({"latency_ms": "9"})["latency_ms"] is None
    )


def test_the_detectors_types_are_categories_and_an_owners_own_type_is_other():
    counts = {"GENERIC_API_KEY": 1, "GITHUB_PAT": 2, "DOMAIN": 1, "EMAIL": 3, "PROJECT_FALCON": 4, BODY: 1}
    shown = safe_summary({"by_type": counts, "new_by_type": counts})
    for key in ("by_type", "new_by_type"):
        assert shown[key] == {"GENERIC_API_KEY": 1, "GITHUB_PAT": 2, "DOMAIN": 1, "EMAIL": 3, "OTHER": 5}


@pytest.mark.parametrize("mode", ["rewrite", "verify", "passthrough"])
def test_each_mode_is_shown_as_it_is(mode):
    assert safe_summary({"mode": mode})["mode"] == mode


@pytest.mark.parametrize("mode", ["mask", "auto", "P25_PRIVATE_MODE", None])
def test_an_unknown_mode_claims_no_protection(mode):
    assert safe_summary({"mode": mode})["mode"] == "passthrough"


@pytest.mark.parametrize("protocol", ["anthropic", "chat", "responses", "embeddings", "gemini"])
def test_each_protocol_the_gateway_reads_is_shown(protocol):
    assert safe_summary({"protocol": protocol})["protocol"] == protocol


def test_an_old_row_keeps_its_words_out_of_the_summary(tmp_path):
    """A row written before this summary (0.10's: its reason, raw path and host in clear) reads with its category
    unknown, its path by its area, its host as a host; its words only in the authorized private view."""
    store = ExchangeStore(tmp_path / "old.db", ROOT_KEY)
    try:
        exchange_id = store.record(ExchangeRecord({"ts": 1_800_000_000.0, "session": "owned", "flags": []}))
        old = {
            "id": exchange_id,
            "ts": 1_800_000_000.0,
            "session": "owned",
            "flags": ["blocked", "leak"],
            "mode": "verify",
            "verified": True,  # an older Latet's claim: not carried as this record's fact
            "host": "api.anthropic.com",
            "path": f"/anthropic/v1/messages?beta={QUERY}",
            "block_reason": REASON,
        }
        store._db.execute("UPDATE exchanges SET summary = ? WHERE id = ?", (json.dumps(old), exchange_id))
        [row] = store.list()[0]
        assert row["block_reason"] is None and row["path"] == "/gateway" and row["host"] == "api.anthropic.com"
        assert row["mode"] == "verify" and "blocked" in row["flags"]
        assert row["verified"] is False and row["coverage"] == "unknown"
        assert not [canary for canary in CANARIES if canary in json.dumps(row)]
    finally:
        store.close()
