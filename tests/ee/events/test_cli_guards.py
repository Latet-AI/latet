"""Adversarial CLI inputs: tampered markers, protocol omissions and exact export bytes."""

import json

import pytest

from .test_cli import _run, _verified


def _gaps(**changes):
    return {"recorded": [[1, 3, 3]], "first_kept": 1, "unrecorded": [], "unchecked": [], **changes}


ENTRY = {"v": 1, "type": "admin", "id": "a" * 32, "outcome": "person.added"}
MARKER = {**ENTRY, "outcome": "siem.gap", "reason": "audit:1-3", "stopped": 3}


def _wrap(event, wrapper):
    if wrapper == "hec":
        return {"event": event}
    if wrapper == "splunk":
        return {"result": {"_raw": json.dumps(event)}}
    if wrapper == "elastic":
        return {"_source": event}
    if wrapper == "hits":
        return {"hits": {"hits": [{"_source": event}]}}
    return event


@pytest.mark.parametrize("wrapper", ["plain", "hec", "splunk", "elastic", "hits"])
@pytest.mark.parametrize("mutation", ["v", "type", "id", "string", "float", "bool"])
def test_tampered_markers_are_never_filtered_out(monkeypatch, tmp_path, wrapper, mutation):
    marker = dict(MARKER)
    if mutation in ("v", "type", "id"):
        del marker[mutation]
        marker["reason"] = "audit:7-9"
    else:
        marker["stopped"] = {"string": "3", "float": 3.0, "bool": True}[mutation]
    path = tmp_path / "copy.json"
    path.write_text(json.dumps([ENTRY, _wrap(marker, wrapper)]), encoding="utf-8")
    monkeypatch.setattr("latet.cli.server.admin_call", _verified(_gaps()))
    done = _run("log", "verify", "--siem", str(path))
    assert done.exit_code == 1, done.output
    assert "not Latet's" in done.output


@pytest.mark.parametrize("field", ["recorded", "first_kept", "unrecorded", "unchecked"])
@pytest.mark.parametrize("bad", ["missing", None, True, {}, "secret-canary"])
def test_incomplete_gap_protocol_is_never_success(monkeypatch, tmp_path, field, bad):
    gaps = _gaps()
    if bad == "missing":
        del gaps[field]
    else:
        gaps[field] = bad
    monkeypatch.setattr("latet.cli.server.admin_call", _verified(gaps))
    path = tmp_path / "copy.json"
    path.write_text(json.dumps([ENTRY, MARKER]), encoding="utf-8")
    done = _run("log", "verify", "--siem", str(path))
    assert done.exit_code == 1, done.output
    assert "not checked" in done.output and "secret-canary" not in done.output


@pytest.mark.parametrize(("recorded", "phrase"), [([[1, 3, 3]], "1 in copy.json"), ([], "cannot be checked")])
def test_retained_attestation_wins_over_marker_age(monkeypatch, tmp_path, recorded, phrase):
    monkeypatch.setattr("latet.cli.server.admin_call", _verified(_gaps(recorded=recorded, first_kept=41)))
    path = tmp_path / "copy.json"
    path.write_text(json.dumps([MARKER]), encoding="utf-8")
    done = _run("log", "verify", "--siem", str(path))
    assert done.exit_code == 0, done.output
    assert phrase in done.output


@pytest.mark.parametrize(
    "payload", ["[" * 2000 + "]" * 2000, "not-json secret-canary", '{"event":'], ids=["deep", "invalid", "partial"]
)
def test_unreadable_copy_has_fixed_failure(monkeypatch, tmp_path, payload):
    monkeypatch.setattr("latet.cli.server.admin_call", _verified(_gaps()))
    path = tmp_path / "copy.json"
    path.write_text(payload, encoding="utf-8")
    done = _run("log", "verify", "--siem", str(path))
    assert done.exit_code == 2, done.output
    assert "cannot be read" in done.output and "secret-canary" not in done.output


def test_export_keeps_bytes_and_ignores_server_filename(monkeypatch, tmp_path):
    from latet.ee.events import cli

    content = chr(0xFEFF) + "name,count\r\n'=formula caf\N{LATIN SMALL LETTER E WITH ACUTE},2\r\n"
    monkeypatch.setattr(cli, "admin_call", lambda *a, **k: {"file": content, "name": "../escape.csv"})
    path = tmp_path / "chosen.csv"
    done = _run("reports", "export", "-o", str(path))
    assert done.exit_code == 0, done.output
    assert path.read_bytes() == content.encode("utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["chosen.csv"]


@pytest.mark.parametrize("content", [None, 2, {}, ["secret-canary"]])
def test_export_requires_string_before_creating_file(monkeypatch, tmp_path, content):
    from latet.ee.events import cli

    monkeypatch.setattr(cli, "admin_call", lambda *a, **k: {"file": content})
    path = tmp_path / "chosen.csv"
    done = _run("reports", "export", "-o", str(path))
    assert done.exit_code == 2 and not path.exists()
    assert "secret-canary" not in done.output


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_export_never_overwrites_existing_destination(monkeypatch, tmp_path, kind):
    from latet.ee.events import cli

    monkeypatch.setattr(cli, "admin_call", lambda *a, **k: {"file": "new"})
    path = tmp_path / "chosen"
    if kind == "file":
        path.write_bytes(b"original")
    else:
        path.mkdir()
    done = _run("reports", "export", "-o", str(path))
    assert done.exit_code == 2, done.output
    if kind == "file":
        assert path.read_bytes() == b"original"


def test_show_escapes_the_complete_changed_destination_hint(monkeypatch):
    from latet.ee.events import cli

    name = "collector" + chr(27) + "[31m" + chr(0x202E)
    row = {
        "name": name,
        "kind": "webhook",
        "url": "https://example.com",
        "secret_destination_changed": True,
        "held": True,
        "last_error": "refused",
        "queued": 2,
        "dropped": 0,
    }
    monkeypatch.setattr(cli, "admin_call", lambda *a, **k: {"outputs": [row]})
    done = _run("siem", "show")
    assert done.exit_code == 0, done.output
    assert chr(27) not in done.output and chr(0x202E) not in done.output
    assert "held" in done.output and "siem skip" in done.output


@pytest.mark.parametrize(
    ("first", "last", "count", "dropped"), [(7, 7, 1, 0), (1, 3, 3, 0), (None, None, 0, 4), (1, 3, 3, 4)]
)
def test_skip_reports_protected_range_and_dropped_events(monkeypatch, first, last, count, dropped):
    from latet.ee.events import cli

    monkeypatch.setattr(
        cli, "admin_call", lambda *a, **k: {"from": first, "to": last, "count": count, "dropped": dropped}
    )
    done = _run("siem", "skip", "collector")
    assert done.exit_code == 0, done.output
    assert f"{dropped} non-admin events dropped" in done.output
    assert ("administration log" in done.output) is (first is not None)
    assert "None" not in done.output


def test_held_event_run_fails_status_and_suggests_skip():
    from datetime import date

    from latet.ee.events.checks import check_events
    from latet.ee.events.store import MemoryEvents
    from latet.ee.license import License, Standing, State
    from latet.ee.settings import EnterpriseSettings, SiemOutput, SiemSettings

    store = MemoryEvents()
    store.failed("collector", "events refused", at=1, retry_at=2, held_to=9)
    lic = License(
        "L-2027-0001", "Acme Corp", 300, frozenset({"reports", "siem"}), date(2026, 11, 1), date(2027, 10, 31)
    )
    standing = Standing(State.VALID, lic, None, 200, None, None, ())
    settings = EnterpriseSettings(
        siem=SiemSettings(outputs=[SiemOutput(name="collector", kind="webhook", url="https://example.com")])
    )
    lines = check_events(queue=store, settings=settings, standing=standing, secret_state=lambda _: (True, False))
    [line] = [line for line in lines if line.title == "SIEM collector"]
    assert line.status == "fail" and "siem skip collector" in line.detail


@pytest.fixture
def real_admin(monkeypatch):
    import httpx

    from latet.cli.agent import MARKERS
    from latet.server.client import ServerClient

    for names in MARKERS.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AGENT", raising=False)
    monkeypatch.setenv("LATET_SERVER_URL", "https://latet.acme.lan")
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", "latet_adm_" + "r" * 43)
    calls = []

    def exchange(self, method, address, sent, body, limit, end, receive):
        path = address.split("/_latet/server/v1/", 1)[1]
        calls.append((method, path, {"body": body}))
        if path == "admin/ee/groups":
            return httpx.Response(200, json={"groups": [{"name": "Payments", "id": "g_paymentsaa"}]})
        return httpx.Response(200, json={"file": "count\r\n1\r\n"})

    # Keep request's status/refusal handling real; replace the single network exchange underneath it.
    monkeypatch.setattr(ServerClient, "_once", exchange)
    return calls


@pytest.mark.parametrize(
    "command",
    [
        ("reports", "settings", "--people", "named"),
        ("siem", "skip", "collector"),
        ("siem", "secret", "collector", "--remove"),
        ("siem", "test", "collector"),
    ],
)
def test_real_admin_helper_refuses_agent_mutations(real_admin, monkeypatch, command):
    monkeypatch.setenv("CLAUDECODE", "1")
    done = _run(*command)
    assert done.exit_code == 2 and "run it yourself" in done.output
    assert real_admin == []


def test_real_admin_helper_encodes_query_without_changing_path(real_admin):
    from urllib.parse import parse_qs, urlsplit

    value = "billing &other=?/# caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    done = _run("reports", "export", "--project", value)
    assert done.exit_code == 0, done.output
    [(method, path, _)] = real_admin
    assert method == "GET" and urlsplit(path).path == "admin/ee/reports/export"
    assert parse_qs(urlsplit(path).query) == {"format": ["csv"], "project": [value]}


def test_report_resolves_group_name_before_exporting(real_admin):
    from urllib.parse import parse_qs, urlsplit

    done = _run("reports", "export", "--group", "PAYMENTS")
    assert done.exit_code == 0, done.output
    assert real_admin[0][:2] == ("GET", "admin/ee/groups")
    assert parse_qs(urlsplit(real_admin[1][1]).query)["group"] == ["g_paymentsaa"]


def test_real_admin_refused_token_never_writes_export(real_admin, monkeypatch, tmp_path):
    import httpx

    from latet.server.client import ServerClient

    monkeypatch.setattr(
        ServerClient, "_once", lambda *a, **k: httpx.Response(401, json={"error": "administration token refused"})
    )
    path = tmp_path / "absent.csv"
    done = _run("reports", "export", "-o", str(path))
    assert done.exit_code == 2 and not path.exists()
    assert "administration token refused" in done.output
    assert "latet_adm_" not in done.output


@pytest.mark.parametrize(
    "gaps",
    [
        {"error": "secret-canary"},
        {"recorded": [[True, 3, 3]], "first_kept": 1, "unrecorded": [], "unchecked": []},
        _gaps(unrecorded=[{}]),
        _gaps(unchecked=["secret-canary"]),
    ],
)
def test_bad_gap_elements_fail_without_raw_payload(monkeypatch, gaps):
    monkeypatch.setattr("latet.cli.server.admin_call", _verified(gaps))
    done = _run("log", "verify")
    assert done.exit_code == 1, done.output
    assert "not checked" in done.output and "secret-canary" not in done.output


def test_export_write_error_is_fixed(monkeypatch, tmp_path):
    from pathlib import Path

    from latet.ee.events import cli

    monkeypatch.setattr(cli, "admin_call", lambda *a, **k: {"file": "counts"})

    def denied(*args, **kwargs):
        raise OSError("secret-canary")

    monkeypatch.setattr(Path, "open", denied)
    done = _run("reports", "export", "-o", str(tmp_path / "out.csv"))
    assert done.exit_code == 2 and "cannot be written" in done.output
    assert "secret-canary" not in done.output


def test_export_refuses_a_symbolic_link(monkeypatch, tmp_path):
    from latet.ee.events import cli

    monkeypatch.setattr(cli, "admin_call", lambda *a, **k: {"file": "new"})
    target, path = tmp_path / "target", tmp_path / "link"
    target.write_bytes(b"original")
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links require privileges on this platform")
    done = _run("reports", "export", "-o", str(path))
    assert done.exit_code == 2 and target.read_bytes() == b"original"


def test_exclusive_export_handles_link_refusal_without_platform_privileges(monkeypatch, tmp_path):
    """Windows may deny creating a symlink; exercise the actual exclusive-open refusal path on every platform."""
    import errno
    from pathlib import Path

    from latet.ee.events import cli

    monkeypatch.setattr(cli, "admin_call", lambda *a, **k: {"file": "new"})
    target = tmp_path / "target"
    target.write_bytes(b"original")
    link = tmp_path / "link"
    original_open = Path.open

    def open_link(path, mode="r", *args, **kwargs):
        if path == link:
            if mode == "xb":
                raise FileExistsError(errno.EEXIST, "secret-canary")
            return original_open(target, mode, *args, **kwargs)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_link)
    done = _run("reports", "export", "-o", str(link))
    assert done.exit_code == 2 and "cannot be written" in done.output
    assert "secret-canary" not in done.output and target.read_bytes() == b"original"


def test_export_refuses_a_special_destination(monkeypatch, tmp_path):
    import os

    from latet.ee.events import cli

    monkeypatch.setattr(cli, "admin_call", lambda *a, **k: {"file": "new"})
    path = tmp_path / "NUL" if os.name == "nt" else tmp_path / "pipe"
    if os.name != "nt":
        os.mkfifo(path)
    done = _run("reports", "export", "-o", str(path))
    assert done.exit_code == 2, done.output


def test_a_broken_chain_cannot_become_success_from_valid_markers(monkeypatch, tmp_path):
    answer = _verified(_gaps())("GET", "log/verify") | {"ok": False, "broken_at": 4}
    monkeypatch.setattr("latet.cli.server.admin_call", lambda *a, **k: answer)
    path = tmp_path / "copy.json"
    path.write_text(json.dumps([MARKER]), encoding="utf-8")
    done = _run("log", "verify", "--siem", str(path))
    assert done.exit_code == 1 and "broken at entry 4" in done.output


@pytest.mark.parametrize("rule", ["license-paused", "license-resumed"])
def test_pause_and_resume_markers_still_need_attestation(monkeypatch, tmp_path, rule):
    path = tmp_path / "copy.json"
    path.write_text(json.dumps([MARKER | {"rule": rule}]), encoding="utf-8")
    monkeypatch.setattr("latet.cli.server.admin_call", _verified(_gaps(recorded=[])))
    assert _run("log", "verify", "--siem", str(path)).exit_code == 1
    monkeypatch.setattr("latet.cli.server.admin_call", _verified(_gaps()))
    assert _run("log", "verify", "--siem", str(path)).exit_code == 0
