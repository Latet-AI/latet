"""The measurement harness of P2-8 (``scripts/request_coverage_bench.py``): frozen fixtures for every case of the
measurement bullet, the bar judged as the plan says it (refusals apart, never faster), and a tiny in-process run of
its computer path, its ledger and its finalizer measurements. The full measurements are root's, scheduled apart."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import request_coverage_bench as bench  # noqa: E402

# The plan's measurement bullet, case by case (RC-REC-3's read and RC-REC-2's Valkey calls are ``micro``'s; the
# finalizer and the repeated key's ledger their own commands).
BULLET = {
    "small-warm",
    "small-cold",
    "bodyless-get",
    "large-json",
    "many-slots",
    "media-withheld",
    "cached-500-plus-1",
    "cached-one-alone",
    "signed-retry",
    "ws-turn",
    "stored-key-working0",
    "index-on",
    "index-off",
    "envelope-key-candidates",
    "envelope-person-whole",
}


def test_the_fixtures_are_frozen_and_hold_every_case_of_the_bullet(tmp_path):
    doc = bench.fixtures()
    assert doc == bench.fixtures() and doc["sha256"] == bench._digest(doc["cases"])
    assert {case["id"] for case in doc["cases"]} >= BULLET
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(doc), "utf-8")
    assert bench.load_fixtures(path)["sha256"] == doc["sha256"]
    changed = json.loads(path.read_text("utf-8"))
    changed["cases"][0]["path"] = "/llm/v1/other"
    path.write_text(json.dumps(changed), "utf-8")
    with pytest.raises(SystemExit):
        bench.load_fixtures(path)  # never measured on bytes other than those frozen


def _measured(label, rows, sha="f" * 64):
    cases = [
        {
            "id": case_id,
            "sent": sent,
            "refused": refused,
            "bodyless": case_id == "bodyless-get",
            "wall_ms": {"p50": p50},
            "cpu_ms": {"p50": None},
            "ledger_peak_bytes": None,
            "retained_request_bytes": None,
        }
        for case_id, sent, refused, p50 in rows
    ]
    return {"label": label, "source_pin": label, "fixtures_sha256": sha, "machines": {"computer": {"cases": cases}}}


def _compare(tmp_path, base, rc):
    (tmp_path / "base.json").write_text(json.dumps(base), "utf-8")
    (tmp_path / "rc.json").write_text(json.dumps(rc), "utf-8")
    out = tmp_path / "verdict.json"
    code = bench.compare(argparse.Namespace(base=tmp_path / "base.json", rc=tmp_path / "rc.json", out=out))
    return code, {row["id"]: row for row in json.loads(out.read_text("utf-8"))["rows"]}


def test_the_bar_is_five_percent_of_the_base_and_two_ms_for_a_bodyless_get(tmp_path):
    base = _measured("task8", [("small-warm", 10, 0, 10.0), ("bodyless-get", 10, 0, 50.0)])
    within = _measured("rc", [("small-warm", 10, 0, 10.5), ("bodyless-get", 10, 0, 51.9)])
    code, rows = _compare(tmp_path, base, within)
    assert code == 0 and all(row["verdict"] == "within the bar" for row in rows.values())
    past = _measured("rc", [("small-warm", 10, 0, 10.6), ("bodyless-get", 10, 0, 52.1)])  # 6 %; 2.1 ms (4.2 %)
    code, rows = _compare(tmp_path, base, past)
    assert code == 1 and {row["verdict"] for row in rows.values()} == {"past the bar: root rules"}


def test_a_case_refused_on_rc_is_never_faster_and_other_fixtures_are_never_compared(tmp_path):
    base = _measured("task8", [("small-warm", 10, 0, 10.0)])
    refused = _measured("rc", [("small-warm", 0, 10, None)])
    code, rows = _compare(tmp_path, base, refused)
    assert code == 1 and rows["small-warm"]["verdict"] == "not comparable: refused on RC"
    with pytest.raises(SystemExit):
        _compare(tmp_path, base, _measured("rc", [("small-warm", 10, 0, 9.0)], sha="e" * 64))


def test_the_provider_says_back_the_last_user_text_and_expires_a_first_aws_token():
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hello there"}]}).encode()
    status, _, answer = bench.provider_answer("POST", "/v1/chat/completions", {}, body)
    assert status == 200 and "You said: hello there" in answer.decode()
    status, headers, _ = bench.provider_answer("POST", "/model/x/invoke", {"x-amz-security-token": "expired-t"}, b"{}")
    assert status == 403 and headers["x-amzn-errortype"] == "ExpiredTokenException"


async def test_a_tiny_run_of_the_computer_cases(tmp_path):
    doc = bench.fixtures()
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(doc), "utf-8")
    config = {
        "fixtures": str(path),
        "folder": str(tmp_path / "run"),
        "machine": "computer",
        "rounds": 1,
        "warm": 1,
        "requests": 2,
        "cases": ["bodyless-get", "small-warm", "signed-retry", "ws-turn", "media-withheld"],
    }
    found = await bench.child_measure(config)
    rows = {case["id"]: case for case in found["cases"]}
    assert found["fixtures_sha256"] == doc["sha256"] and set(rows) == set(config["cases"])
    for case_id in ("bodyless-get", "small-warm", "signed-retry", "ws-turn"):
        assert rows[case_id]["sent"] == 2 and rows[case_id]["refused"] == 0, rows[case_id]
        assert rows[case_id]["wall_ms"]["p50"] > 0 and rows[case_id]["traced_peak_bytes"] > 0
    assert rows["small-warm"]["ledger_peak_bytes"] > 0 and rows["small-warm"]["retained_request_bytes"] is not None
    # Without text recognition models an image is refused on every head: counted apart, never a speed.
    assert rows["media-withheld"]["sent"] == 0 and rows["media-withheld"]["refused"] == 2


async def test_the_ledger_a_repeated_key_costs_and_the_finalizer_wait_are_measured(tmp_path):
    found = await bench.child_ledger({"folder": str(tmp_path / "ledger")})
    rows = {(r["text"], r["repeated"]): r for r in found["rows"]}
    assert all(r["status"] == 200 and r["ledger_bytes"] for r in rows.values())
    assert rows[("short", 1)]["ledger_bytes"] > rows[("short", 0)]["ledger_bytes"]
    assert found["bytes_per_repeated_key"]["short"]["n"] == 8
    waited = await bench.child_finalizer({"folder": str(tmp_path / "finalizer"), "rounds": 1})
    for name in ("whole", "stream"):
        part = waited["finalizer"][name]
        assert part["alone_ms"]["n"] == 1 and part["during_masking_ms"]["n"] == 1 and part["added_p50_ms"] is not None
