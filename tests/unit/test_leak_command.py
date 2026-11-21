"""`latet leak`: the command's output, its JSON and its exit codes, with the sources, the scan and Latet's own answers
faked at their interfaces. No value is ever in a report, so none is in these tests but the made-up hints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers the commands
from latet.cli.main import app
from latet.leak import known as known_module
from latet.leak import scan as scan_module
from latet.leak import sources
from latet.leak.known import CONNECTED, NO_KEY, NO_VAULT, UNREADABLE_KEY, UNREADABLE_VAULT, NoAnswer, NothingHere
from latet.leak.scan import Finding, Occurrences, Report
from latet.leak.sources import Skipped

NONE = Occurrences(0, None, None)


def day(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def occ(
    sessions: int,
    first: str,
    last: str,
    confirmed: int = 0,
    agents: tuple[str, ...] = ("claude",),
    folders: tuple[str, ...] = ("C:/work/app",),
) -> Occurrences:
    return Occurrences(sessions, day(first), day(last), confirmed, frozenset(agents), folders)


def finding(**kw: Any) -> Finding:
    base: dict[str, Any] = {
        "etype": "GITHUB_TOKEN",
        "eclass": "SECRET",
        "hint": "ghp_… (40 characters)",
        "agents": frozenset({"claude"}),
        "folders": ("C:/work/app",),
        "in_clear": NONE,
        "replaced": NONE,
        "redacted": NONE,
    }
    base.update(kw)
    return Finding(**base)


class FakeKnown:
    """Latet's answers, as far as the command asks them itself: the earliest date Latet has on this computer."""

    def __init__(self, began: datetime | None = None) -> None:
        self.began = began

    def latet_since(self) -> datetime | None:
        return self.began


class Fake:
    def __init__(self) -> None:
        self.report = Report(sessions={"claude": 3, "codex": 1})
        self.known: FakeKnown | None = FakeKnown(day("2026-10-10"))
        self.unknown = NoAnswer(CONNECTED)  # why there are no answers, where ``known`` is None
        self.read_args: dict[str, Any] = {}
        self.scan_args: dict[str, Any] = {}
        self.events: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def read(agents=None, extra=(), since=None, home=None):  # type: ignore[no-untyped-def]
            self.events.append("read")
            self.read_args = {"agents": agents, "extra": list(extra), "since": since}
            if agents:
                for a in agents:
                    if a not in sources.AGENTS:
                        raise ValueError(f"unknown agent: {a}")
            return iter(())

        def scan(texts, config, personal=False, known=None, progress=None):  # type: ignore[no-untyped-def]
            list(texts)
            self.scan_args = {"personal": personal, "known": known}
            if progress is not None:
                for n in (1, 2, 3):
                    progress("claude", n)
                progress("codex", 1)
            return self.report

        def answer():  # type: ignore[no-untyped-def]
            if self.known is not None:
                return self.known, None
            return (NothingHere() if self.unknown.why == NO_VAULT else None), self.unknown

        monkeypatch.setattr(sources, "read", read)
        monkeypatch.setattr(sources, "remove_old_copies", lambda: self.events.append("cleaned"))
        monkeypatch.setattr(scan_module, "scan", scan)
        monkeypatch.setattr(known_module, "answer", answer)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Fake:
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path))
    f = Fake()
    f.install(monkeypatch)
    return f


def run(*args: str):
    return CliRunner().invoke(app, ["leak", *args])


def test_three_lists_and_the_advice(fake: Fake) -> None:
    fake.report.findings = [
        finding(in_clear=occ(4, "2026-10-02", "2026-10-09"), replaced=occ(2, "2026-10-12", "2026-11-14", 1)),
        finding(
            etype="ANTHROPIC_KEY",
            hint="sk-ant-api03-… (108 characters)",
            agents=frozenset({"claude", "codex"}),
            replaced=occ(6, "2026-10-12", "2026-11-14", 5, agents=("claude", "codex")),
        ),
        finding(etype="DB_PASSWORD", hint="Db password (20 characters)", redacted=occ(2, "2026-11-01", "2026-11-02")),
    ]
    result = run()
    out = result.output
    assert result.exit_code == 1
    assert "Latet read 3 Claude Code sessions and 1 Codex session." in out
    assert "1 secret in clear (no sign that Latet replaced it: it may have reached the provider):" in out
    assert "ghp_… (40 characters)" in out
    assert "Claude Code, 4 sessions in C:/work/app" in out
    assert "2026-10-02 to 2026-10-09" in out
    assert "2 secrets Latet replaced, or may have (its fake left instead where the session went through Latet):" in out
    assert "5 confirmed by Latet's history, 1 if the session went through Latet" in out
    assert "Claude Code and Codex, 6 sessions" in out
    assert "1 secret your settings redact" in out
    assert "may have sent a marker in its place" in out
    assert "settings may have been different then" in out
    assert "Rotate each secret in clear at its vendor: what reached a model provider may stay in its logs." in out
    assert "Do not resume those sessions" in out
    assert fake.scan_args["known"] is fake.known


def test_a_secret_in_two_lists_appears_in_each_with_its_count(fake: Fake) -> None:
    fake.report.findings = [
        finding(in_clear=occ(4, "2026-10-02", "2026-10-09"), replaced=occ(2, "2026-10-12", "2026-11-14", 2))
    ]
    out = run().output
    assert out.count("ghp_… (40 characters)") == 2
    assert "4 sessions" in out
    assert "2 sessions" in out


def test_no_advice_to_rotate_when_nothing_is_in_clear(fake: Fake) -> None:
    fake.report.findings = [finding(replaced=occ(1, "2026-10-12", "2026-10-12", 1))]
    out = run().output
    assert "Rotate" not in out
    assert "need nothing" in out


def test_without_latet_answers_it_says_so_and_lists_the_secrets_in_clear(fake: Fake) -> None:
    fake.known = None
    fake.report.findings = [finding(in_clear=occ(3, "2026-10-02", "2026-10-09"))]
    out = run().output
    assert "could not tell from here which" in out
    assert "connected to a Latet server" in out
    assert "1 secret in clear" in out
    assert fake.scan_args["known"] is None


def test_nothing_found_exits_zero(fake: Fake) -> None:
    result = run()
    assert result.exit_code == 0
    assert "No secrets found" in result.output


def test_nothing_read_exits_two(fake: Fake) -> None:
    fake.report = Report(skipped=[Skipped("C:/x/a.json", "not JSON")])
    result = run()
    assert result.exit_code == 2
    assert "nothing" in result.output.lower()
    assert "C:/x/a.json: not JSON" in result.output


def test_skipped_one_line_each_and_not_an_error(fake: Fake) -> None:
    fake.report.skipped = [Skipped("C:/x/a.jsonl", "a line is not JSON"), Skipped("b.db", "locked")]
    result = run()
    assert result.exit_code == 0
    assert "2 things could not be read:" in result.output
    assert "  C:/x/a.jsonl: a line is not JSON" in result.output
    assert "  b.db: locked" in result.output


def test_what_could_not_be_read_is_said_once(fake: Fake) -> None:
    """``unread`` counts what ``skipped`` lists: one line says how many, and the lines below it what and why."""
    fake.report.skipped = [Skipped("C:/x/a.jsonl", "a line is not JSON"), Skipped("b.db", "locked")]
    fake.report.unread = 2
    out = run().output
    assert out.count("could not be read") == 1 and "2 things could not be read:" in out
    assert "record" not in out and "unread" not in out


def test_personal_data_counted_by_type(fake: Fake) -> None:
    fake.report.personal = {"EMAIL": 12, "PHONE": 1}
    result = run("--personal")
    assert fake.scan_args["personal"] is True
    assert "Email: 12" in result.output
    assert "Phone: 1" in result.output
    assert result.exit_code == 1
    run()
    assert fake.scan_args["personal"] is False


def test_agent_since_and_path_reach_the_sources(fake: Fake, tmp_path: Path) -> None:
    run("--agent", "claude", "--agent", "codex", "--since", "2026-10-01", "--path", str(tmp_path))
    assert fake.read_args["agents"] == ["claude", "codex"]
    assert fake.read_args["extra"] == [tmp_path]
    assert fake.read_args["since"].date().isoformat() == "2026-10-01"


def test_unknown_agent_names_the_agents(fake: Fake) -> None:
    result = run("--agent", "nope")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "nope" in result.stderr
    for name in sources.AGENTS:
        assert name in result.stderr


def test_bad_since(fake: Fake) -> None:
    result = run("--since", "yesterday-ish")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "2026-10-01" in result.stderr


def test_json_has_the_fields_and_no_value(fake: Fake) -> None:
    fake.report.personal = {"EMAIL": 2}
    fake.report.skipped = [Skipped("b.db", "locked")]
    fake.report.unread = 1
    fake.report.findings = [
        finding(in_clear=occ(4, "2026-10-02", "2026-10-09"), replaced=occ(2, "2026-10-12", "2026-11-14", 1))
    ]
    result = run("--json", "--personal")
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["sessions"] == {"claude": 3, "codex": 1}
    f = data["findings"][0]
    assert f["type"] == "GITHUB_TOKEN"
    assert f["types"] == ["GITHUB_TOKEN"]
    assert f["class"] == "SECRET"
    assert f["hint"] == "ghp_… (40 characters)"
    assert f["agents"] == ["claude"]
    assert f["folders"] == ["C:/work/app"]
    assert f["in_clear"] == {
        "sessions": 4,
        "first": "2026-10-02T00:00:00+00:00",
        "last": "2026-10-09T00:00:00+00:00",
        "confirmed": 0,
        "agents": ["claude"],
        "folders": ["C:/work/app"],
    }
    assert f["replaced"]["confirmed"] == 1
    assert f["redacted"] == {"sessions": 0, "first": None, "last": None, "confirmed": 0, "agents": [], "folders": []}
    assert data["personal"] == {"EMAIL": 2}
    assert data["skipped"] == [{"where": "b.db", "why": "locked"}]
    assert data["unread"] == 1
    assert data["latet_answers"] is True


def test_without_latet_answers_a_redacted_secret_is_listed_as_redacted_as_the_notice_says(fake: Fake) -> None:
    fake.known = None
    fake.report.findings = [
        finding(in_clear=occ(3, "2026-10-02", "2026-10-09")),
        finding(etype="DB_PASSWORD", hint="Db password (20 characters)", redacted=occ(2, "2026-11-01", "2026-11-02")),
    ]
    out = run().output
    assert "Every secret below is listed as in clear" not in out
    assert (
        "secrets your settings redact, or Latet redacts as too long to keep, are listed as redacted, every other as "
        "in clear"
    ) in out
    assert "1 secret in clear" in out and "1 secret your settings redact" in out


def test_in_clear_says_from_when_latet_can_tell(fake: Fake) -> None:
    """In clear means Latet has no sign it replaced them, which it can tell only from the earliest date it keeps here:
    the header does not say they reached the provider for certain, and a line says from when Latet can tell."""
    fake.report.findings = [finding(in_clear=occ(4, "2026-10-02", "2026-10-09"))]
    out = run().output
    assert "1 secret in clear (no sign that Latet replaced it: it may have reached the provider):" in out
    assert "Latet can tell only from 2026-10-10 on" in out
    assert "may have gone through Latet all the same" in out
    fake.known = FakeKnown(None)
    assert "keep no date yet" in run().output
    fake.known = None
    assert "can tell only from" not in run().output


def test_the_replaced_header_says_may_where_a_session_is_not_confirmed(fake: Fake) -> None:
    fake.report.findings = [finding(replaced=occ(2, "2026-10-12", "2026-11-14", 2))]
    out = run().output
    assert "1 secret Latet replaced (its fake left instead):" in out
    assert "The secrets Latet replaced need nothing." in out
    fake.report.findings = [finding(replaced=occ(2, "2026-10-12", "2026-11-14", 1))]
    out = run().output
    assert "1 secret Latet replaced, or may have (its fake left instead where the session went through Latet):" in out
    assert "The secrets Latet replaced need nothing where the session went through Latet." in out


def test_with_redacted_secrets_alone_the_closing_lines_keep_their_may(fake: Fake) -> None:
    fake.report.findings = [
        finding(etype="DB_PASSWORD", hint="Db password (20 characters)", redacted=occ(2, "2026-11-01", "2026-11-02"))
    ]
    out = run().output
    assert "No secret is listed in clear." in out
    assert "The redacted secrets may need nothing" in out
    assert "Latet replaced need nothing" not in out and "Rotate" not in out


def test_personal_data_alone(fake: Fake) -> None:
    fake.report.personal = {"EMAIL": 12}
    result = run("--personal")
    assert result.exit_code == 1
    assert "No secrets found." in result.output
    assert "Personal data found, by type (counted, never shown):" in result.output and "Email: 12" in result.output


def test_personal_data_but_no_session_read_exits_two(fake: Fake) -> None:
    fake.report = Report(personal={"EMAIL": 1})
    assert run("--personal").exit_code == 2


def test_progress_goes_to_a_console_on_standard_error(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    from latet.cli import leak as leak_module

    clock = iter(range(10_000))
    monkeypatch.setattr(leak_module, "_console", lambda: True)
    monkeypatch.setattr(leak_module, "_now", lambda: float(next(clock)))
    result = run()
    assert "Claude Code: 3 sessions read" in result.stderr
    assert "Codex: 1 session read" in result.stderr
    assert "Read in " in result.stderr
    assert "sessions read" not in result.stdout and "Read in" not in result.stdout
    json_run = run("--json")
    assert json.loads(json_run.stdout)["sessions"] == {"claude": 3, "codex": 1}
    assert "Claude Code: 3 sessions read" in json_run.stderr


def test_no_progress_where_standard_error_is_no_console(fake: Fake) -> None:
    result = run()
    assert result.stderr == ""


def test_a_long_run_says_how_to_read_less(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    from latet.cli import leak as leak_module

    clock = iter(range(0, 10_000, 40))
    monkeypatch.setattr(leak_module, "_console", lambda: True)
    monkeypatch.setattr(leak_module, "_now", lambda: float(next(clock)))
    result = run()
    assert "--agent or --since" in result.stderr
    assert "min" in result.stderr


def _entries(out: str, header: str) -> list[str]:
    """The secrets' lines of the list under ``header``: its lines indented by two, but its counts and its last line."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header))
    entries = []
    for line in lines[start + 1 :]:
        if not line.startswith("  "):
            break
        if not line.startswith(("      ", "  By type: ", "  … and ")):
            entries.append(line)
    return entries


def test_each_list_puts_a_vendors_secrets_first_then_the_most_sessions(fake: Fake) -> None:
    """A vendor's key (a typed prefix, `ghp_`, `AKIA`, `latet_adm_`, or a vendor rule's type) is the one to rotate
    first: it comes before a password or a value assigned to a name, whatever their sessions."""
    rows = [
        ("PASSWORD", "Password (17 characters)", 10),
        ("ASSIGNED_SECRET", "Assigned secret (26 characters)", 5),
        ("GITHUB_PAT", "ghp_… (40 characters)", 1),
        ("AWS_ACCESS_TOKEN", "AKIA… (20 characters)", 3),
        ("GENERIC_API_KEY", "Generic api key (30 characters)", 4),
        ("HASHICORP_TF_PASSWORD", "Hashicorp tf password (20 characters)", 2),
        ("LATET_ADMIN_TOKEN", "latet_adm_… (53 characters)", 2),
        ("CURL_AUTH_USER", "Curl auth user (20 characters)", 6),  # a Gitleaks rule, but any service's password
    ]
    fake.report.findings = [
        finding(etype=etype, hint=shown, in_clear=occ(n, "2026-10-02", "2026-10-09")) for etype, shown, n in rows
    ]
    entries = _entries(run().output, "8 secrets in clear")
    order = [next(etype for etype, shown, _ in rows if shown in line) for line in entries]
    assert order == [
        "AWS_ACCESS_TOKEN",
        "HASHICORP_TF_PASSWORD",
        "LATET_ADMIN_TOKEN",
        "GITHUB_PAT",
        "PASSWORD",
        "CURL_AUTH_USER",
        "ASSIGNED_SECRET",
        "GENERIC_API_KEY",
    ]


def test_a_list_prints_twenty_secrets_and_says_how_to_see_the_others(fake: Fake) -> None:
    fake.report.findings = [
        finding(etype="PASSWORD", hint=f"Password ({n} characters)", in_clear=occ(1, "2026-10-02", "2026-10-02"))
        for n in range(10, 35)
    ]
    out = run().output
    assert len(_entries(out, "25 secrets in clear")) == 20
    assert "  … and 5 more: latet leak --all lists every one, --json gives them all." in out
    every = run("--all").output
    assert len(_entries(every, "25 secrets in clear")) == 25
    assert "… and" not in every
    assert len(json.loads(run("--json").output)["findings"]) == 25


def test_a_list_counts_its_secrets_by_type_under_its_header(fake: Fake) -> None:
    """The six types with the most secrets in the list, then the secrets of the others in one count."""
    counts = {
        "PASSWORD": 9,
        "ASSIGNED_SECRET": 7,
        "GENERIC_API_KEY": 5,
        "GITHUB_PAT": 4,
        "JWT": 3,
        "AWS_ACCESS_TOKEN": 2,
        "SLACK_BOT_TOKEN": 1,
        "STRIPE_ACCESS_TOKEN": 1,
    }
    fake.report.findings = [
        finding(etype=etype, hint=f"{etype} {n}", in_clear=occ(1, "2026-10-02", "2026-10-02"))
        for etype, many in counts.items()
        for n in range(many)
    ]
    lines = run("--all").output.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("32 secrets in clear"))
    assert lines[header + 1] == (
        "  By type: 9 Password, 7 Assigned secret, 5 Generic api key, 4 Github pat, 3 Jwt, 2 Aws access token "
        "and 2 of other types."
    )
    fake.report.findings = [
        finding(etype="GITHUB_PAT", replaced=occ(1, "2026-10-12", "2026-10-12", 1)),
        finding(etype="PASSWORD", hint="Password (9 characters)", replaced=occ(1, "2026-10-12", "2026-10-12", 1)),
    ]
    lines = run().output.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("2 secrets Latet replaced"))
    assert lines[header + 1] == "  By type: 1 Github pat and 1 Password."


def test_the_redacted_list_names_the_values_too_long_to_keep(fake: Fake) -> None:
    """A value longer than a vault keeps is redacted whatever its type's policy: the list says so with the settings."""
    fake.report.findings = [
        finding(etype="PRIVATE_KEY", hint="Private key (20000 characters)", redacted=occ(1, "2026-11-01", "2026-11-01"))
    ]
    out = run().output
    assert "1 secret your settings redact, or Latet redacts as too long to keep (" in out
    assert "The redacted secrets may need nothing: if the session went through Latet" in out
    fake.known = None
    out = run().output
    assert "The secrets your settings redact, or Latet redacts as too long to keep, are listed as redacted" in out


def _progress_lines(stderr: str) -> list[str]:
    return [part for line in stderr.split("\n") for part in line.split("\r") if part.strip()]


def test_every_progress_line_fits_in_eighty_columns(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refresh goes back to the start of its row alone (`\\r`): a line that wraps leaves a line at each refresh."""
    from latet.cli import leak as leak_module

    clock = iter([0.0, *(3599.0 + n for n in range(100))])
    monkeypatch.setattr(leak_module, "_console", lambda: True)
    monkeypatch.setattr(leak_module, "_now", lambda: next(clock))

    def scan(texts, config, personal=False, known=None, progress=None):  # type: ignore[no-untyped-def]
        assert progress is not None
        progress("hermes", 9999)
        progress("claude", 9999)
        return fake.report

    monkeypatch.setattr(scan_module, "scan", scan)
    shown = _progress_lines(run().stderr)
    assert any(line.startswith("Hermes Agent: 9999 sessions read, 59 min 59 s") for line in shown)
    assert any("--agent or --since read less" in line for line in shown)
    assert all(len(line) < 80 for line in shown), [len(line) for line in shown]
    clock = iter([0.0, *(3599.0 + n for n in range(100))])
    monkeypatch.setattr(leak_module, "_columns", lambda: 40)
    assert all(len(line) < 40 for line in _progress_lines(run().stderr) if not line.startswith("Read in"))


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_the_progress_line_is_ended_when_the_scan_stops(
    fake: Fake, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    """A scan that fails or is stopped (Ctrl-C) leaves its progress line ended: what follows starts a line of its own."""
    from latet.cli import leak as leak_module

    monkeypatch.setattr(leak_module, "_console", lambda: True)

    def scan(texts, config, personal=False, known=None, progress=None):  # type: ignore[no-untyped-def]
        assert progress is not None
        progress("claude", 2)
        raise failure()

    monkeypatch.setattr(scan_module, "scan", scan)
    result = run()
    assert result.exit_code != 0  # 130 for Ctrl-C
    assert "Claude Code: 2 sessions read" in result.stderr
    assert result.stderr.endswith("\n")  # a traceback, "Aborted!" or the shell's prompt starts a row of its own
    assert "Read in" not in result.stderr


def test_json_exit_codes(fake: Fake) -> None:
    assert run("--json").exit_code == 0
    fake.report = Report()
    assert run("--json").exit_code == 2


def test_a_value_found_as_two_types_gives_them_in_json(fake: Fake) -> None:
    fake.report.findings = [
        finding(
            etype="GITHUB_PAT", types=("GITHUB_PAT", "CURL_AUTH_HEADER"), in_clear=occ(2, "2026-10-02", "2026-10-03")
        )
    ]
    [f] = json.loads(run("--json").output)["findings"]
    assert f["type"] == "GITHUB_PAT" and f["types"] == ["GITHUB_PAT", "CURL_AUTH_HEADER"]
    assert run().output.count("ghp_… (40 characters)") == 1


def test_the_command_says_which_may_have_reached_a_model_provider() -> None:
    """``--help`` and the CLI reference say "may": a session's text is what the agent kept, not proof it was sent."""
    from latet.cli import leak as leak_module

    doc = " ".join((leak_module.leak.__doc__ or "").split())
    assert "say which may have reached a model provider" in doc


def test_each_line_has_the_agents_and_folders_of_its_lists_sessions(fake: Fake) -> None:
    fake.report.findings = [
        finding(
            agents=frozenset({"claude", "codex"}),
            folders=("C:/billing", "C:/shop"),
            in_clear=occ(1, "2026-10-02", "2026-10-02", folders=("C:/shop",)),
            replaced=occ(1, "2026-10-20", "2026-10-20", 1, agents=("codex",), folders=("C:/billing",)),
        )
    ]
    lines = run().output.splitlines()
    clear, replaced = [line for line in lines if "ghp_… (40 characters)" in line]
    assert clear.endswith("Claude Code, 1 session in C:/shop")
    assert replaced.endswith("Codex, 1 session in C:/billing")


def _local(year: int, month: int, d: int, hour: int, minute: int) -> datetime:
    """A time of this computer's clock, as the scan keeps it: in UTC."""
    return datetime(year, month, d, hour, minute).astimezone().astimezone(UTC)


def test_dates_are_this_computers_as_since_reads_them(fake: Fake) -> None:
    """``--since 2026-10-03`` reads from midnight of this computer's clock: the dates shown are of that clock too, so a
    session at half past midnight is on the 3rd, one at half past eleven the evening before on the 2nd."""
    first, last = _local(2026, 10, 2, 23, 30), _local(2026, 10, 3, 0, 30)
    fake.report.findings = [finding(in_clear=Occurrences(2, first, last, 0, frozenset({"claude"})))]
    fake.known = FakeKnown(_local(2026, 10, 10, 0, 30))
    out = run().output
    assert "2026-10-02 to 2026-10-03" in out
    assert "Latet can tell only from 2026-10-10 on" in out
    run("--since", "2026-10-03")
    assert fake.read_args["since"] == datetime(2026, 10, 3)  # read in this computer's zone (``sources.read``)


def test_a_date_the_clock_cannot_show_is_shown_as_in_utc(fake: Fake) -> None:
    """A record read with ``--path`` can carry any date (.NET writes `0001-01-01` for none): one this computer's clock
    cannot convert (before 1970 or after 3000 on Windows, year 1 or 9999 elsewhere) is shown as its date in UTC, never
    a crash after the whole scan."""
    first, last = datetime(1, 1, 1, tzinfo=UTC), datetime(9999, 12, 31, 23, 59, tzinfo=UTC)
    fake.report.findings = [finding(in_clear=Occurrences(1, first, last, 0, frozenset({"claude"})))]
    result = run()
    assert not isinstance(result.exception, OSError | OverflowError | ValueError), result.exception
    assert "0001-01-01 to 9999-12-31" in result.output


@pytest.mark.parametrize(("zone", "day"), [("XXX-5", "2026-10-03"), ("XXX+5", "2026-10-02")])
def test_dates_follow_the_clocks_zone_whatever_it_is(zone: str, day: str) -> None:
    """The tests of this computer's dates pass alike on a clock in UTC, as CI's is: here the zone is set (UTC+5, then
    UTC-5), in a process of its own, as the C runtime reads ``TZ`` when it starts. 22:30 UTC is the next day at UTC+5,
    the same day at UTC-5."""
    code = "from datetime import UTC, datetime\nfrom latet.cli.leak import _day\n"
    code += "print(_day(datetime(2026, 10, 2, 22, 30, tzinfo=UTC)))\n"
    env = {**os.environ, "TZ": zone}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert done.stdout.strip() == day


@pytest.mark.parametrize(
    ("unknown", "said"),
    [
        (NoAnswer(CONNECTED), "this computer is connected to a Latet server, whose vault and history are not here."),
        (NoAnswer(NO_KEY), "no key found on this computer opens its vault."),
        (
            NoAnswer(UNREADABLE_KEY, "it is not in base64"),
            "the key to its vault could not be read (it is not in base64).",
        ),
        (
            NoAnswer(UNREADABLE_VAULT, "not enough free space for a private copy"),
            "its vault on this computer could not be read (not enough free space for a private copy).",
        ),
    ],
)
def test_without_latet_answers_the_command_says_why(fake: Fake, unknown: NoAnswer, said: str) -> None:
    fake.known, fake.unknown = None, unknown
    fake.report.findings = [finding(in_clear=occ(3, "2026-10-02", "2026-10-09"))]
    out = run().output
    assert "Latet could not tell" in out and said in out
    assert sum("Latet could not tell" in line for line in out.splitlines()) == 1
    assert fake.scan_args["known"] is None


def test_with_no_vault_and_no_server_latet_replaced_nothing_here(fake: Fake) -> None:
    fake.known, fake.unknown = None, NoAnswer(NO_VAULT)
    fake.report.findings = [finding(in_clear=occ(3, "2026-10-02", "2026-10-09"))]
    out = run().output
    assert (
        "Latet has no vault on this computer and is connected to no Latet server: it replaced nothing here, so every "
        "secret is listed in clear."
    ) in out
    assert "could not tell" not in out and "can tell only from" not in out
    assert isinstance(fake.scan_args["known"], NothingHere)
    assert json.loads(run("--json").output)["latet_answers"] is False


def test_old_private_copies_are_removed_before_anything_is_read(fake: Fake) -> None:
    run()
    assert fake.events[:2] == ["cleaned", "read"]
