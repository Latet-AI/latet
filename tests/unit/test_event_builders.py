"""What Community code records as events: the schema's fields only, a code where Latet wrote a text, the identity a
server's space is for, and never a value, a text, a fake, a path or a session key."""

import inspect
import json
import random
import re
import string
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from latet.bridge.api import HISTORY_REASON
from latet.events import (
    _TOKEN_ID,
    _TYPE,
    CODE,
    EVENT_TYPES,
    FIELDS,
    STOP_CODES,
    apps_event,
    code_of,
    exchange_event,
    make_event,
    name_of,
    stop_code,
    type_code,
)
from latet.gateway import forward, mediate
from latet.gateway.approvals import EXPIRED, REJECTED
from latet.gateway.body_coverage import BodyRefused
from latet.gateway.forward import _SETTINGS_MOVED, too_large
from latet.gateway.mediate import (
    _NOT_AN_OBJECT,
    _NOT_MESSAGES,
    _TWICE,
    _UNREAD_FRAME,
    Mediator,
    _unread_body,
    _unread_fields,
)
from latet.gateway.outcome import BLOCK_CODES
from latet.gateway.recorder import Recorder
from latet.protocols.codec import DecodedTooLarge
from latet.server.identities import TOKEN_ID
from latet.server.tokens import KEY_PREFIX, TOKEN_PREFIX, new_id, new_secret

T0 = 1_790_000_000.0  # 2026-09-21T14:13:20Z
SUMMARY = {
    "ts": T0,
    "session": "cc:5f1c0d2e-session",
    "agent": "claude-code",
    "path": "/v1/messages",
    "host": "api.anthropic.com",
    "flags": [],
    "new_by_type": {"EMAIL": 2, "GITHUB_PAT": 1, "PHONE": 0},
    "block_reason": None,
    "project": {"id": "3f9a0c1d2e4b", "name": "billing-api"},
}


def test_an_event_has_the_fields_of_the_interfaces_events_section():
    assert FIELDS[:4] == ("v", "id", "ts", "type") and FIELDS[-1] == "chain"
    assert set(FIELDS) == {
        "v", "id", "ts", "type", "identity", "kind", "group", "agent", "project", "masked", "stopped", "reason",
        "outcome", "by", "rule", "chain",
    }  # fmt: skip
    assert {"exchange", "stopped", "license", "admin", "approval", "change", "apps"} <= set(EVENT_TYPES)
    assert tuple(make_event("admin")) == tuple(make_event("apps")) == FIELDS


def test_the_chain_of_an_admin_event_is_two_hashes_and_nothing_else():
    head = "a" * 64
    assert make_event("admin", chain={"prev": "", "hash": head})["chain"] == {"prev": "", "hash": head}
    assert make_event("admin", chain={"prev": head, "hash": "b" * 64})["chain"]["prev"] == head
    for chain in ({"prev": "", "hash": "zz"}, {"hash": head}, {"prev": "", "hash": head, "value": "x"}, "a" * 64):
        with pytest.raises(ValueError, match="chain"):
            make_event("admin", chain=chain)


@pytest.mark.parametrize(
    "chain",
    [
        {"prev": "", "hash": ""},  # only the very first entry has no previous hash, and every entry has its own
        {"prev": "", "hash": "a" * 63},
        {"prev": "", "hash": "a" * 65},
        {"prev": "", "hash": "A" * 64},  # lowercase hexadecimal, as the log writes it
        {"prev": "", "hash": "g" * 64},
        {"prev": "", "hash": "a" * 64 + chr(10)},  # a line end is no digit
        {"prev": "a" * 63, "hash": "b" * 64},
        {"prev": "a" * 65, "hash": "b" * 64},
        {"prev": "a" * 64 + chr(10), "hash": "b" * 64},
        {"prev": " " + "a" * 63, "hash": "b" * 64},
        {"prev": None, "hash": "b" * 64},
        {"prev": 0, "hash": "b" * 64},
        {"prev": "", "hash": None},
        {"prev": "", "hash": 7},
        {"prev": b"", "hash": "b" * 64},
        {"prev": "", "hash": ("b" * 64,)},
        {"previous": "", "hash": "b" * 64},
        {},
        [],
        ["a" * 64, "b" * 64],
        ("", "a" * 64),
        ["prev", "hash"],  # the right names, but no mapping
        ("prev", "hash"),
        {"prev", "hash"},
        5,
        True,
        "",
    ],
)
def test_a_chain_in_any_other_shape_is_refused(chain):
    with pytest.raises(ValueError, match="chain") as refused:
        make_event("admin", chain=chain)
    assert "a" * 63 not in str(refused.value)  # it says the shape it wants, never what it was given


def test_the_chain_is_copied_and_the_events_without_one_have_none():
    given = {"prev": "", "hash": "c" * 64}
    event = make_event("admin", chain=given)
    given["hash"] = "d" * 64
    assert event["chain"] == {"prev": "", "hash": "c" * 64} and event["chain"] is not given
    assert make_event("admin")["chain"] is None and make_event("exchange", chain=None)["chain"] is None


def test_code_is_the_pattern_every_code_of_an_event_is_checked_with():
    assert CODE.fullmatch("route-bypass") and CODE.fullmatch("key.controller.added") and CODE.fullmatch("t_k3yfortest")
    assert not CODE.fullmatch("Route") and not CODE.fullmatch("a b") and not CODE.fullmatch("-a")
    assert all(CODE.fullmatch(code) for code in STOP_CODES)
    with pytest.raises(ValueError, match="reason is a code"):
        make_event("stopped", reason="Blocked")


def test_an_applications_api_call_is_its_counts_its_key_and_its_tokens_id():
    event = apps_event("mask", identity="u_billingci2", token="t_k3yfortest", masked={"EMAIL": 2, "IBAN": 0},
                       stopped=1, clock=lambda: T0)  # fmt: skip
    assert (event["type"], event["identity"], event["kind"], event["agent"]) == ("apps", "u_billingci2", "key", "apps")
    assert (event["outcome"], event["rule"], event["masked"], event["stopped"]) == (
        "mask",
        "t_k3yfortest",
        {"EMAIL": 2},
        1,
    )
    assert apps_event("scan", identity="u_billingci2", token=None, masked={})["rule"] is None


def test_an_applications_api_events_fields_are_the_schemas_and_its_time_is_the_calls():
    event = apps_event("scan", identity="u_billingci2", token=new_id("t_"), masked={}, clock=lambda: T0)
    assert tuple(event) == FIELDS and event["ts"] == "2026-09-21T14:13:20Z"
    assert (event["masked"], event["stopped"], event["reason"], event["group"], event["project"]) == (
        {},
        0,
        None,
        None,
        None,
    )
    assert (event["by"], event["chain"]) == (None, None)
    assert apps_event("scan", identity="u_billingci2", token="", masked={})["rule"] is None


def test_an_applications_api_event_names_its_token_by_its_id_never_by_its_secret():
    for _ in range(50):
        token = new_id("t_")
        assert apps_event("mask", identity="u_billingci2", token=token, masked={})["rule"] == token
    for prefix in (KEY_PREFIX, TOKEN_PREFIX):
        secret = new_secret(prefix)
        with pytest.raises(ValueError, match="token's id") as refused:
            apps_event("mask", identity="u_billingci2", token=secret, masked={"EMAIL": 1})
        assert secret not in str(refused.value) and secret[len(prefix) : len(prefix) + 12] not in str(refused.value)
    for token in ("t_", "t_k3yfortes", "t_k3yfortest1", "T_k3yfortest", "t_K3YFORTEST", "u_k3yfortest", "t_k3yforte-t",
                  "t_k3yfortest ", "t_k3yfortest" + chr(10), "t_k3yfor1est"):  # fmt: skip
        with pytest.raises(ValueError, match="token's id"):
            apps_event("scan", identity="u_billingci2", token=token, masked={})


@pytest.mark.parametrize("operation", ["unmask", "forget", "Mask", "mask ", "", "scan, mask", "emily.carter@acme.com"])
def test_only_a_scan_and_a_mask_are_applications_api_events(operation):
    with pytest.raises(ValueError, match="scan or mask"):
        apps_event(operation, identity="u_billingci2", token=None, masked={})


def test_an_applications_api_event_is_for_a_key_with_counts_only():
    for identity in ("Emily Carter", "emily.carter@acme.com", "", "g_payments", "latet_key_x"):
        with pytest.raises(ValueError, match="identity"):
            apps_event("mask", identity=identity, token=None, masked={})
    with pytest.raises(ValueError, match="stopped is a count"):
        apps_event("mask", identity="u_billingci2", token=None, masked={}, stopped=-1)
    with pytest.raises(ValueError, match="invalid literal"):  # a count that is no number
        apps_event("mask", identity="u_billingci2", token=None, masked={"EMAIL": "two"})
    # A type is a label of the settings (a term's type) or Latet's own: one an event cannot name is made one (M10 holds).
    assert apps_event("mask", identity="u_billingci2", token=None, masked={"client name": 2})["masked"] == {
        "CLIENT_NAME": 2
    }
    event = apps_event("mask", identity="u_billingci2", token=None, masked={"EMAIL": 2, "IBAN": -1, "PHONE": 0})
    assert event["masked"] == {"EMAIL": 2}


def _blocked(reason, *flags, **more):
    return {"flags": ["blocked", *flags], "block_reason": reason} | more


def _reviewer_failed(why):
    """The words the gateway writes when the local reviewer could not read a text (``Mediator.failure``: it reads
    nothing of its object)."""
    return cast(Any, Mediator).failure(None, SimpleNamespace(on_failure="stop"), ["user_text"], why)


# The refusals of what Latet does not read, as the gateway words them (never copied here: a reword of the gateway
# changes these, and a stop_code that no longer knows it fails its test instead of turning into "other").
UNREAD_REQUESTS = [
    _unread_body("POST"),
    _unread_body("GET"),
    _NOT_AN_OBJECT,
    _unread_fields(["image"]),
    _unread_fields(["audio", "file"]),
    _UNREAD_FRAME.format(what="a frame that is not text"),
    # 0.11's refusals of a part of a request no request profile names, or that Latet could not account for, by their
    # words: a body's member or resource, and the coverage's limit.
    str(BodyRefused("message", unknown=True)),
    str(BodyRefused("model resource")),
    str(BodyRefused("tools", limit=True)),
]
# 0.11's refusals said by their code alone (``outcome.BLOCK_CODES``, which the recorder hands an event's summary): an
# envelope's, a proof's, evidence's.
UNCOVERED_CODES = [
    "envelope-header-unknown",
    "envelope-route",
    "envelope-query-unknown",
    "proof-incomplete",
    "evidence",
]
# The words of three more of 0.11's refusals (literals where the gateway writes them: checked there below): a value in
# clear in a header or a query parameter, and a decision the transport refused to send for its coverage limit or for
# evidence that expired.
SENSITIVE_ENVELOPE = "request envelope refused (envelope-sensitive)"
COVERAGE_LIMIT = "request envelope refused (coverage-limit)"
EVIDENCE_EXPIRED = "opaque evidence expired; send the request again"
STOPS = [
    ({"flags": ["blocked", "route-bypass"]}, "route-bypass"),
    ({"flags": ["blocked", "outbound:WebSearch"], "path": "/plugin/tool-call"}, "outbound-tool"),
    (_blocked(HISTORY_REASON, "leak", "history"), "history"),
    (_blocked(f"Latet did not send this request: {REJECTED}"), "live-reviewer"),
    (_blocked(f"Latet did not send this request: {EXPIRED}"), "live-reviewer"),
    (_blocked(_reviewer_failed("unreachable")), "local-reviewer"),
    (_blocked(_reviewer_failed("timeout"), "leak"), "local-reviewer"),
    (_blocked(_reviewer_failed("suspect")), "local-reviewer"),
    (_blocked("the local reviewer stopped this request: the user text must not leave this machine even masked"),
     "local-reviewer"),
    ({"flags": ["blocked", "auto-mode-review", "leak"]}, "auto-mode-review"),
    (_blocked("sensitive values would leave in clear: EMAIL[21]", "leak"), "leak"),
    (_blocked("the AWS login ended", "error", signed="aws-session"), "aws-signing"),
    (_blocked("the AWS login ended", "leak", "error", signed="aws-session"), "aws-signing"),
    (_blocked("unreadable request body (JSONDecodeError)"), "unreadable"),
    (_blocked("unreadable request body (JSONDecodeError)", "leak"), "unreadable"),
    (_blocked(str(BodyRefused("shape")), block_code="refused"), "unreadable"),  # 0.11's: a body it cannot read
    (_blocked(_TWICE), "unreadable"),
    (_blocked(_TWICE, "leak"), "unreadable"),
    (_blocked(_NOT_MESSAGES), "not-messages"),
    (_blocked(_NOT_MESSAGES, "leak"), "not-messages"),
    (_blocked("Latet cannot read this token count (not a request it knows), so it does not send it"), "not-messages"),
    (_blocked("masking failed"), "masking-failed"),
    (_blocked("masking failed: the vault is full"), "masking-failed"),
    (_blocked("masking failed: the vault is full", "leak"), "masking-failed"),
    *[(_blocked(words), "unread-request") for words in UNREAD_REQUESTS],
    *[(_blocked(words, "leak"), "unread-request") for words in UNREAD_REQUESTS],
    (_blocked(_unread_fields(["image"]), "uninspected:image"), "unread-request"),
    *[({"flags": ["blocked"], "block_code": code}, "unread-request") for code in UNCOVERED_CODES],
    *[({"flags": ["blocked", "leak"], "block_code": code}, "unread-request") for code in UNCOVERED_CODES],
    # The merge review's M3: a value in clear in a header or a query parameter is a leak, never a part unread...
    (_blocked(SENSITIVE_ENVELOPE, block_code="envelope-sensitive"), "leak"),
    ({"flags": ["blocked"], "block_code": "envelope-sensitive"}, "leak"),
    # ...and a signed Bedrock request the transport refused for its coverage limit or for evidence that expired is
    # stopped for that, by its code, or by the error it was recorded with after an attempt, never for its AWS login.
    (_blocked(COVERAGE_LIMIT, "error", signed="aws-session", block_code="coverage-limit"), "unread-request"),
    (_blocked(EVIDENCE_EXPIRED, "error", signed="aws-session", block_code="evidence"), "unread-request"),
    (_blocked(COVERAGE_LIMIT, "error", signed="aws-session") | {"error": "coverage-limit"}, "unread-request"),
    (_blocked(EVIDENCE_EXPIRED, "error", signed="aws-session") | {"error": "opaque-expired"}, "unread-request"),
    # 0.11.1's refusal of a request whose settings changed while it was prepared (a save, a shared change, a sync of the
    # terms, after its reviews): a reason of its own, by its code, never `other` nor a part unread, and not hidden by a
    # leak in the same request, by its being Claude Code's auto mode reviewer's, nor by its AWS login.
    (_blocked(_SETTINGS_MOVED, block_code="settings-changed"), "settings-changed"),
    (_blocked(_SETTINGS_MOVED, "leak", block_code="settings-changed"), "settings-changed"),
    (_blocked(_SETTINGS_MOVED, "auto-mode-review", block_code="settings-changed"), "settings-changed"),
    (_blocked(_SETTINGS_MOVED, "error", signed="aws-session", block_code="settings-changed"), "settings-changed"),
    (_blocked(too_large(DecodedTooLarge(1024 * 1024))), "too-large"),
    (_blocked(too_large(DecodedTooLarge(2000)), "leak"), "too-large"),
    (_blocked("requests with images are not sent", "uninspected:image"), "unread"),
    (_blocked("a reason no code knows"), "other"),
    ({"flags": ["blocked"]}, "other"),
    ({}, "other"),
]  # fmt: skip


@pytest.mark.parametrize(("summary", "code"), STOPS)
def test_why_a_request_was_stopped_is_a_code(summary, code):
    assert stop_code(summary) == code and code in STOP_CODES


@pytest.mark.parametrize(
    "words",
    [
        "Latet did not send this request: {",
        "the local reviewer stopped this request",
        "unreadable request body (",
        "Latet cannot read this token count",
        "masking failed: {",
    ],
)
def test_the_gateway_still_writes_the_words_that_stop_code_reads_where_they_are_not_a_constant(words):
    assert words in inspect.getsource(mediate)


def test_the_gateway_still_writes_the_words_of_the_refusals_these_tests_name():
    assert SENSITIVE_ENVELOPE in inspect.getsource(mediate)
    assert COVERAGE_LIMIT in inspect.getsource(forward) and EVIDENCE_EXPIRED in inspect.getsource(forward)
    # The error a refusal after an attempt is recorded with (``Forwarder._not_sent``): its own code, which
    # ``_UNSENT_ERRORS`` reads, but for one whose settings changed (0.12.0), recorded with ``settings-changed``.
    sent = inspect.getsource(forward.Forwarder._not_sent)
    assert "said = code if moved else refusal.code" in sent and "error: str | None = said" in sent
    # The refusal of a request whose settings changed while it was prepared: its code, which the recorder hands an
    # event's summary as it is one of the gateway's codes, and its words.
    assert '"settings-changed", _SETTINGS_MOVED' in inspect.getsource(forward) and "settings-changed" in BLOCK_CODES


def test_the_event_summary_carries_the_error_an_exchange_was_recorded_with():
    """The recorder hands an event's summary the error code it records (a signed request the transport refused after
    an attempt, which has no refusal code of its own then), read for the stop's code alone, never copied."""
    exchange = SimpleNamespace(
        started=T0, headers={}, path="/bedrock/us-east-1/model/x/invoke", signed="aws-session",
        block_reason=COVERAGE_LIMIT, block_code=None, leaks=[], uninspected=[], images=[], degraded=[],
        protocol="anthropic", mode="mask", profile="bedrock.invoke.1", review=False, off=False,
        restores=SimpleNamespace(project=None), withheld=False, history=False, outbound=[], bypass=False,
    )  # fmt: skip
    summary = Recorder._event_summary(cast(Any, None), cast(Any, exchange), {}, "coverage-limit")
    assert summary["error"] == "coverage-limit" and stop_code(summary) == "unread-request"
    event = exchange_event(summary)
    assert event["reason"] == "unread-request" and "coverage-limit" not in json.dumps(event)


def test_a_request_refused_because_its_settings_changed_is_stopped_for_that():
    """The merge review of 0.11.1's M1: the exchange the gateway records for a request whose settings (a sync of the
    terms, on Enterprise) changed after its reviews, a leak found in it as well, is a `stopped` event for
    `settings-changed`, its words read, never copied; before, it was `other`."""
    exchange = SimpleNamespace(
        started=T0, headers={}, path="/openai/v1/chat/completions", signed=None,
        block_reason=_SETTINGS_MOVED, block_code="settings-changed", leaks=[SimpleNamespace(allowed=False)],
        uninspected=[], images=[], degraded=[], protocol="openai", mode="mask", profile=None, review=False, off=False,
        restores=SimpleNamespace(project=None), withheld=False, history=False, outbound=[], bypass=False,
    )  # fmt: skip
    event = exchange_event(Recorder._event_summary(cast(Any, None), cast(Any, exchange), {}))
    assert (event["type"], event["stopped"], event["reason"]) == ("stopped", 1, "settings-changed")
    assert "send it again" not in json.dumps(event)


ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = ("", "it/", "de/", "fr/", "es/")  # the documentation's: English at the root
# A reason's words on the dashboard's Reports page: `REASONS` of `dashboard/src/ee/lib/reports.ts`, a code to its key.
_REASON_WORDS = re.compile(r"^\s*'?([a-z-]+)'?: tk\('ee\.reports\.reasons\.(\w+)'\),$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\| `([a-z-]+)` \|")


def test_every_stop_code_has_its_words_on_reports_and_its_row_in_the_documentation():
    """Each `reason` a `stopped` event can carry has words of its own on the dashboard's Reports page, in its five
    languages, and a row in the documentation's table of why a request was stopped (`enterprise/reports`), in five
    languages: a code added here is never shown there as "Another reason", nor missing from the table (the merge review
    of 0.11.1's M1, whose `settings-changed` was in neither)."""
    source = (ROOT / "dashboard" / "src" / "ee" / "lib" / "reports.ts").read_text(encoding="utf-8")
    words = dict(_REASON_WORDS.findall(source))
    assert set(words) == set(STOP_CODES) - {"other"}  # one the page does not know is "Other"
    for language in ("en", "it", "de", "fr", "es"):
        locale = json.loads((ROOT / "dashboard" / "src" / "locales" / f"{language}.json").read_text(encoding="utf-8"))
        said = locale["ee"]["reports"]["reasons"]
        assert all(said.get(key) for key in [*words.values(), "other"]), language
    for language in LANGUAGES:
        page = ROOT / "docs" / "src" / "content" / "docs" / f"{language}enterprise" / "reports.mdx"
        lines = page.read_text(encoding="utf-8").splitlines()
        start = end = next(i for i, line in enumerate(lines) if line.startswith("| `leak` |"))
        while lines[start - 1].startswith("|"):
            start -= 1
        while end + 1 < len(lines) and lines[end + 1].startswith("|"):
            end += 1
        rows = [_TABLE_ROW.match(line) for line in lines[start + 2 : end + 1]]  # its heading and rule left out
        assert sorted(row.group(1) for row in rows if row) == sorted(STOP_CODES) and all(rows), page


def test_every_stop_code_is_one_a_request_can_get_and_the_first_that_holds_wins():
    assert {code for _, code in STOPS} == set(STOP_CODES) and len(STOP_CODES) == len(set(STOP_CODES))
    assert STOP_CODES[-1] == "other" and STOP_CODES.index("leak") > STOP_CODES.index("masking-failed")
    assert STOP_CODES.index("leak") > STOP_CODES.index("too-large")
    settings = STOP_CODES.index("settings-changed")
    assert settings < min(STOP_CODES.index(code) for code in ("auto-mode-review", "aws-signing", "unread-request"))
    # Signed and stopped for its size: its size, never its AWS login (the forwarder records no error code with it).
    assert stop_code(_blocked(too_large(DecodedTooLarge(2000)), signed="aws-session")) == "too-large"
    assert stop_code({"flags": ["leak", "route-bypass"], "block_reason": "masking failed"}) == "route-bypass"
    assert stop_code({"flags": ["leak"], "block_reason": "the local reviewer could not read it"}) == "local-reviewer"
    assert stop_code({"flags": ["error"], "signed": None}) == "other"  # an error with nothing signed is no AWS signing
    assert stop_code({"flags": ["leak"], "signed": "aws-session"}) == "leak"
    # A leak found in the request does not hide why it was stopped when Latet said why in words of its own.
    assert stop_code(_blocked("the AWS login ended", "leak", "error", signed="aws-session")) == "aws-signing"
    assert stop_code(_blocked("masking failed: x", "leak", "uninspected:image")) == "masking-failed"


def test_an_exchange_is_its_counts_agent_project_and_identity_at_its_own_time():
    event = exchange_event(SUMMARY, identity="u_emilycarte", kind="person")
    assert (event["type"], event["ts"], event["identity"], event["kind"]) == (
        "exchange", "2026-09-21T14:13:20Z", "u_emilycarte", "person",
    )  # fmt: skip
    assert (event["agent"], event["project"], event["stopped"], event["group"]) == (
        "claude-code",
        "billing-api",
        0,
        None,
    )
    assert event["masked"] == {"EMAIL": 2, "GITHUB_PAT": 1} and event["reason"] is None
    assert tuple(event) == FIELDS
    text = json.dumps(event)
    assert "cc:5f1c0d2e-session" not in text and "/v1/messages" not in text and "api.anthropic.com" not in text
    assert "3f9a0c1d2e4b" not in text  # the project's id is not its name


def test_an_api_keys_exchange_says_so_and_a_computers_names_nobody():
    assert exchange_event(SUMMARY, identity="u_billingci2", kind="key")["kind"] == "key"
    alone = exchange_event(SUMMARY, kind="person")
    assert (alone["identity"], alone["kind"]) == (None, None)


def test_a_stopped_request_says_why_with_a_code_never_with_its_text():
    stopped = SUMMARY | {
        "flags": ["blocked", "leak"],
        "block_reason": "sensitive values would leave in clear: EMAIL[21]",
    }
    event = exchange_event(stopped)
    assert (event["type"], event["stopped"], event["reason"]) == ("stopped", 1, "leak")
    assert "EMAIL[21]" not in json.dumps(event)


def test_an_exchange_not_counted_says_so_and_has_no_counts():
    event = exchange_event({"ts": T0, "flags": [], "agent": "codex", "uncounted": True})
    assert (event["type"], event["masked"], event["reason"]) == ("exchange", {}, "uncounted")


def test_a_stopped_request_not_counted_keeps_its_stop_code():
    event = exchange_event({"ts": T0, "flags": ["blocked", "leak"], "agent": "codex", "uncounted": True})
    assert (event["type"], event["masked"], event["reason"], event["stopped"]) == ("stopped", {}, "leak", 1)


def test_an_exchange_counts_what_was_masked_by_type_and_leaves_the_zeros_out():
    event = exchange_event({"ts": T0, "flags": [], "new_by_type": {"IBAN": 3, "EMAIL": 0, "PHONE": -2}})
    assert event["masked"] == {"IBAN": 3}
    assert exchange_event({"ts": T0, "flags": [], "new_by_type": None})["masked"] == {}
    assert exchange_event({"ts": T0})["masked"] == {} and exchange_event({"ts": T0})["type"] == "exchange"


def test_what_an_exchange_names_that_is_not_a_code_or_a_name_is_made_one_never_copied():
    odd = SUMMARY | {"agent": "Goose Agent v2!", "project": {"id": "x", "name": "Billing API" + chr(10)}}
    event = exchange_event(odd)
    assert (event["agent"], event["project"]) == ("gooseagentv2", "Billing API")
    nameless = SUMMARY | {"agent": None, "project": None}
    event = exchange_event(nameless)
    assert (event["agent"], event["project"]) == (None, None)
    root = SUMMARY | {"project": {"id": "x", "name": "C:" + chr(0x5C)}}  # a drive's root has no folder name
    assert exchange_event(root)["project"] is None


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("EMAIL", "EMAIL"),
        ("GITHUB_PAT", "GITHUB_PAT"),
        ("CLIENT-NAME", "CLIENT_NAME"),
        ("PROJECT NAME", "PROJECT_NAME"),
        ("client name", "CLIENT_NAME"),
        ("KUNDE_\N{LATIN CAPITAL LETTER A WITH DIAERESIS}", "KUNDE_A"),
        ("\N{LATIN CAPITAL LETTER E WITH ACUTE}QUIPE", "EQUIPE"),
        ("\N{CYRILLIC CAPITAL LETTER KA}\N{CYRILLIC CAPITAL LETTER A}", "CUSTOM"),  # nothing Latin is left
        ("\N{CJK UNIFIED IDEOGRAPH-5BA2}\N{CJK UNIFIED IDEOGRAPH-6237}", "CUSTOM"),
        ("--", "CUSTOM"),
        ("", "CUSTOM"),
        ("_PROJECT_", "PROJECT"),
        ("A -- B", "A_B"),
        ("2FA CODE", "T_2FA_CODE"),
        ("A" * 80, "A" * 64),
        ("9" * 80, "T_" + "9" * 62),
        ("A" * 63 + "-B", "A" * 63),
    ],
)
def test_a_type_of_value_is_made_one_an_event_can_name(label, code):
    """A term's type is the label a person gave it (``terms``' ``type``, uppercased): one an event cannot name is made
    one, so that no builder refuses the exchange or the call that masked it."""
    assert type_code(label) == code
    assert make_event("exchange", masked={code: 1})["masked"] == {code: 1}


def test_types_made_the_same_add_up_and_no_builder_refuses_a_type():
    summary = SUMMARY | {"new_by_type": {"CLIENT-NAME": 2, "CLIENT NAME": 1, "PROJECT NAME": 0, "EMAIL": 1}}
    assert exchange_event(summary)["masked"] == {"CLIENT_NAME": 3, "EMAIL": 1}
    masked = {"CLIENT-NAME": 1, "client name": 2, "KUNDE_\N{LATIN CAPITAL LETTER A WITH DIAERESIS}": 1}
    event = apps_event("mask", identity="u_billingcix", token="t_k3yfortest", masked=masked)
    assert event["masked"] == {"CLIENT_NAME": 3, "KUNDE_A": 1}


def test_whatever_type_a_term_is_given_its_code_is_one_an_event_takes():
    rng = random.Random(13)
    alphabet = string.printable + "\N{LATIN SMALL LETTER E WITH ACUTE}\N{CYRILLIC SMALL LETTER A}\N{SNOWMAN}"
    alphabet += "\N{LATIN SMALL LETTER SHARP S}\N{FULLWIDTH LATIN CAPITAL LETTER A}\N{ZERO WIDTH SPACE}"
    for _ in range(2000):
        label = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        code = type_code(label)
        assert _TYPE.fullmatch(code), label
        assert type_code(code) == code  # a code is its own


def test_the_events_of_an_exchange_are_one_each_with_an_id_of_their_own():
    assert exchange_event(SUMMARY)["id"] != exchange_event(SUMMARY)["id"]


def test_a_name_from_outside_becomes_a_code_or_a_printable_name():
    assert code_of("Goose Agent v2!") == "gooseagentv2" and code_of("  ") is None and code_of(None) is None
    assert code_of("admin:3F9A") == "admin:3f9a" and code_of("key.controller.added") == "key.controller.added"
    assert name_of("billing" + chr(0) + "-api" + chr(10)) == "billing-api" and name_of("") is None
    assert len(name_of("x" * 500) or "") == 200


def test_a_token_id_is_what_the_server_makes_one():
    assert _TOKEN_ID.pattern == TOKEN_ID.pattern  # the server's own (latet.server.identities)


def test_a_negative_limit_leaves_nothing_it_does_not_count_from_the_end():
    assert code_of("abcdef", -1) is None and code_of("abcdef", -100) is None
    assert name_of("abcdef", -1) is None and name_of("abcdef", -100) is None


def test_a_code_always_begins_with_a_letter_or_a_digit_and_fits_what_an_event_takes():
    assert code_of("_private") == "private" and code_of("..:-x") == "x" and code_of("--") is None
    assert code_of("a" * 500) == "a" * 40 and code_of("a" * 500, 80) == "a" * 80 and code_of("a" * 500, 500) == "a" * 80
    assert code_of("abc", 0) is None and code_of(0) is None and code_of(False) is None
    assert code_of("Emily Carter" + chr(0x2028) + "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}") == "emilycarteri"
    assert make_event("license", reason=code_of("_.x:y-"))["reason"] == "x:y-"


def test_whatever_is_given_to_code_of_comes_out_as_a_code_an_event_takes_or_nothing():
    rng = random.Random(7)
    alphabet = string.printable + "\N{LATIN SMALL LETTER E WITH ACUTE}\N{CYRILLIC SMALL LETTER A}\N{SNOWMAN}\x00\x1f"
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        for limit in (1, 5, 40, 80):
            code = code_of(text, limit)
            assert code is None or (CODE.fullmatch(code) and len(code) <= limit)
            if code is not None:
                assert make_event("license", outcome=code)["outcome"] == code


@pytest.mark.parametrize(
    "name", ["src/billing-api", "C:/Users/emily", "billing" + chr(0x5C) + "api", "/", "C:" + chr(0x5C)]
)
def test_a_name_with_a_separator_of_a_path_is_a_path_and_names_nothing(name):
    assert name_of(name) is None


def test_whatever_is_given_to_name_of_comes_out_as_a_name_an_event_takes_or_nothing():
    rng = random.Random(11)
    alphabet = (
        string.printable + "\N{LATIN SMALL LETTER E WITH ACUTE}\N{CYRILLIC SMALL LETTER A}\N{SNOWMAN}\x00\x1f\x7f"
    )
    alphabet += "\N{RIGHT-TO-LEFT OVERRIDE}\N{NO-BREAK SPACE}\N{LINE SEPARATOR}\N{ZERO WIDTH SPACE}"
    plain = alphabet.replace("/", "").replace(chr(0x5C), "")  # half the texts hold no separator, so they keep a name
    for _ in range(2000):
        letters = rng.choice((alphabet, plain))
        text = "".join(rng.choice(letters) for _ in range(rng.randint(0, 260)))
        name = name_of(text)
        assert name is None or (0 < len(name) <= 200 and name == name.strip())
        if name is not None:
            assert make_event("exchange", project=name)["project"] == name
    assert name_of("x" * 500, 500) == "x" * 200  # an event takes 200 at most
    assert (
        name_of("billing-api", 7) == "billing"
        and name_of("  billing  api", 9) == "billing"
        and name_of("ab", 0) is None
    )
    assert name_of("  Billing API  ") == "Billing API" and name_of(None) is None and name_of(chr(10) + chr(0)) is None
