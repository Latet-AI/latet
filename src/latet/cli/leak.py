"""``latet leak``: the secrets in the coding agents' saved sessions, found with Latet's own detectors and never shown.

It reads the sessions (``latet.leak.sources``), scans them (``latet.leak.scan``, whose docstring says how each session
is counted) and, on a computer with its own Latet, asks Latet's vault and history which secrets Latet replaced
(``latet.leak.known``). It reads and prints; it changes nothing, and no value is ever in its output
(notes/latet-leak-design.md, "The command").

Each list (in clear, replaced, redacted) says under its header how many of its secrets are of each type, then lists
them, a vendor's keys first (a GitHub token, an Anthropic key, an AWS key: the ones to rotate first), then those in the
most sessions: the first ``_SHOWN`` of them, and how many more; ``--all`` lists every one, as ``--json`` does. A
secret's line names the agents and the folders of that list's sessions, and its dates are this computer's, as
``--since`` reads them. Where Latet's vault and history give no answer, a line says why (``known.NoAnswer``).

While it reads, a console on standard error is told the agent being read and how many of its sessions were read so far,
in one line refreshed in place and narrower than the console, then how long it took; past ``_LONG`` seconds, that
``--agent`` and ``--since`` read less. Counts alone, never on standard output: ``--json`` stays one document there.

Exit codes: 0 nothing found, 1 something found (in clear, replaced or redacted), 2 nothing could be read.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from latet.cli.main import app

if TYPE_CHECKING:
    from latet.leak.known import NoAnswer
    from latet.leak.scan import Finding, Known, Occurrences, Report

_FOLDERS_SHOWN = 3
_SHOWN = 20  # the secrets a list prints, without --all
_BY_TYPE = 6  # the types a list's counts name one by one, the secrets of the others in one count
_REFRESH = 0.25  # seconds between two refreshes of the progress line
_LONG = 60.0  # a run longer than this is told how to read less
# A console's width at most, and where its own is unknown: a progress line is kept narrower than its console, since
# `\r` goes back to the start of a row alone and a line that wraps would leave a row behind at each refresh.
_COLUMNS = 80


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _day(when: datetime | None) -> str:
    """A date of this computer's clock, as ``--since`` reads one (``sources.read``): a time just after midnight here is
    of the day it begins, whatever the date in UTC. A date the clock cannot convert (before 1970 or after 3000 on
    Windows, year 1 or 9999 elsewhere: a record read with ``--path`` can carry any) is the date in UTC."""
    if when is None:
        return "an unknown date"
    try:
        return when.astimezone().date().isoformat()
    except (OSError, OverflowError, ValueError):
        return when.date().isoformat()


def _range(occ: Occurrences) -> str:
    first, last = _day(occ.first), _day(occ.last)
    return first if first == last else f"{first} to {last}"


def _agents(agents: frozenset[str]) -> str:
    from latet.leak.sources import AGENTS

    return _join(sorted(AGENTS.get(a, a) for a in agents))


def _where(occ: Occurrences) -> str:
    folders = list(occ.folders)
    if not folders:
        return ""
    shown = folders[:_FOLDERS_SHOWN]
    rest = len(folders) - len(shown)
    text = ", ".join(shown) + (f" and {_plural(rest, 'other folder')}" if rest > 0 else "")
    return f" in {text}"


def _by_type(findings: list[Finding]) -> str:
    """A list's secrets counted by type: the ``_BY_TYPE`` types with the most, then the others' in one count."""
    from latet.leak.scan import label

    counts = Counter(label(f.etype) for f in findings)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    parts = [f"{n} {name}" for name, n in ranked[:_BY_TYPE]]
    rest = ranked[_BY_TYPE:]
    if rest:
        parts.append(f"{sum(n for _, n in rest)} of {'another type' if len(rest) == 1 else 'other types'}")
    return f"  By type: {_join(parts)}."


def _section(
    title: str, rows: list[tuple[Finding, Occurrences]], detail: Callable[[Occurrences], str], every: bool
) -> list[str]:
    """A list: its header, its counts by type, and its secrets, a vendor's first (``scan.vendor``: the keys to rotate
    first), then those in the most sessions; ``_SHOWN`` of them, or with ``every`` all. Each with the agents and the
    folders of this list's sessions (``Occurrences``), not of all the secret's."""
    if not rows:
        return []
    from latet.leak.scan import label, vendor

    rows = sorted(rows, key=lambda row: (not vendor(row[0].etype), -row[1].sessions))
    shown = rows if every else rows[:_SHOWN]
    lines = ["", title, _by_type([finding for finding, _ in rows])]
    for finding, occ in shown:
        name = label(finding.etype)
        hint = finding.hint if finding.hint.startswith(name) else f"{name}  {finding.hint}"
        lines.append(f"  {hint}  {_agents(occ.agents)}, {_plural(occ.sessions, 'session')}{_where(occ)}")
        lines.append(f"      {_range(occ)}{detail(occ)}")
    if len(rows) > len(shown):
        more = len(rows) - len(shown)
        lines.append(f"  … and {more} more: latet leak --all lists every one, --json gives them all.")
    return lines


def _no_detail(occ: Occurrences) -> str:
    return ""


def _replaced_detail(occ: Occurrences) -> str:
    unconfirmed = occ.sessions - occ.confirmed
    parts = []
    if occ.confirmed:
        parts.append(f"{occ.confirmed} confirmed by Latet's history")
    if unconfirmed:
        parts.append(f"{unconfirmed} if the session went through Latet")
    return "; " + ", ".join(parts) if parts else ""


def _secrets(count: int) -> str:
    return _plural(count, "secret")


def _clear_title(count: int) -> str:
    it, has = ("it", "it") if count == 1 else ("them", "they")
    return f"{_secrets(count)} in clear (no sign that Latet replaced {it}: {has} may have reached the provider):"


def _from_when(began: datetime | None) -> str:
    """From when Latet can tell, below the secrets in clear: its earliest date here, and what may have come before."""
    if began is None:
        return (
            "Latet's vault and history on this computer keep no date yet: Latet could not tell whether it replaced "
            "any of them."
        )
    return (
        f"Latet can tell only from {_day(began)} on, the earliest date its vault and history keep on "
        "this computer: an emptied vault, or a history past the days it keeps (14 by default), keeps nothing older. "
        "A session listed in clear from before then may have gone through Latet all the same."
    )


_REDACTED = "your settings redact, or Latet redacts as too long to keep"  # a value longer than a vault keeps


def _no_answer(unknown: NoAnswer) -> str:
    """Why Latet's vault and history gave no answer here, and what the lists below are then."""
    from latet.leak import known

    if unknown.why == known.NO_VAULT:
        return (
            "Latet has no vault on this computer and is connected to no Latet server: it replaced nothing here, so "
            "every secret is listed in clear."
        )
    reasons = {
        known.CONNECTED: "this computer is connected to a Latet server, whose vault and history are not here",
        known.NO_KEY: "no key found on this computer opens its vault",
        known.UNREADABLE_KEY: f"the key to its vault could not be read ({unknown.detail})",
        known.UNREADABLE_VAULT: f"its vault on this computer could not be read ({unknown.detail})",
    }
    where = " from here" if unknown.why == known.CONNECTED else ""
    return (
        f"Latet could not tell{where} which secrets it replaced: {reasons.get(unknown.why, unknown.why)}. The secrets "
        f"{_REDACTED}, are listed as redacted, every other as in clear."
    )


def render(
    report: Report, unknown: NoAnswer | None = None, began: datetime | None = None, every: bool = False
) -> list[str]:
    """The report as the lines the command prints. ``unknown``: why Latet's vault and history gave no answer, None
    where they did; ``began``: the earliest date they keep on this computer, None where they keep none; ``every``: each
    list prints all its secrets, not the first ``_SHOWN``."""
    from latet.leak.scan import label
    from latet.leak.sources import AGENTS

    lines: list[str] = []
    counts = [_plural(n, f"{AGENTS.get(agent, agent)} session") for agent, n in sorted(report.sessions.items()) if n]
    if counts:
        lines.append(f"Latet read {_join(counts)}.")
    else:
        lines.append("Latet read no saved sessions: there was nothing to read.")
    if unknown is not None:
        lines.append(_no_answer(unknown))

    clear = [(f, f.in_clear) for f in report.findings if f.in_clear.sessions]
    replaced = [(f, f.replaced) for f in report.findings if f.replaced.sessions]
    redacted = [(f, f.redacted) for f in report.findings if f.redacted.sessions]
    unconfirmed = any(occ.confirmed < occ.sessions for _, occ in replaced)
    lines += _section(_clear_title(len(clear)), clear, _no_detail, every)
    if clear and unknown is None:
        lines.append(_from_when(began))
    replaced_title = (
        f"{_secrets(len(replaced))} Latet replaced, or may have (its fake left instead where the session went "
        "through Latet):"
        if unconfirmed
        else f"{_secrets(len(replaced))} Latet replaced (its fake left instead):"
    )
    lines += _section(replaced_title, replaced, _replaced_detail, every)
    lines += _section(
        f"{_secrets(len(redacted))} {_REDACTED} (Latet may have sent a marker in its place, if the session went "
        "through it; your settings may have been different then):",
        redacted,
        _no_detail,
        every,
    )

    if report.personal:
        lines += ["", "Personal data found, by type (counted, never shown):"]
        lines += [f"  {label(t)}: {n}" for t, n in sorted(report.personal.items())]

    advice: list[str] = []
    if clear:
        advice.append(
            "Rotate each secret in clear at its vendor: what reached a model provider may stay in its logs. Do not "
            "resume those sessions (a resumed conversation sends them again): start new ones."
        )
    elif replaced or redacted:
        advice.append("No secret is listed in clear.")
    if replaced:
        where = " where the session went through Latet" if unconfirmed else ""
        advice.append(f"The secrets Latet replaced need nothing{where}.")
    if redacted:
        advice.append(
            "The redacted secrets may need nothing: if the session went through Latet, it sent a marker in their place."
        )
    if advice:
        lines += ["", " ".join(advice)]
    elif counts:
        lines += ["", "No secrets found."]

    # ``report.unread`` counts these: --json gives both, the text one line.
    if report.skipped:
        lines += ["", f"{_plural(len(report.skipped), 'thing')} could not be read:"]
        lines += [f"  {s.where}: {s.why}" for s in report.skipped]
    return lines


def _when(when: datetime | None) -> str | None:
    return when.isoformat() if when is not None else None


def _occurrences(occ: Occurrences) -> dict[str, object]:
    return {
        "sessions": occ.sessions,
        "first": _when(occ.first),
        "last": _when(occ.last),
        "confirmed": occ.confirmed,
        "agents": sorted(occ.agents),
        "folders": list(occ.folders),
    }


def to_json(report: Report, answers: bool, began: datetime | None = None) -> dict[str, object]:
    """The report as records: the same fields as the text, dates in ISO form, and no value."""
    return {
        "latet_answers": answers,
        "latet_since": _when(began),
        "sessions": dict(report.sessions),
        "findings": [
            {
                "type": f.etype,
                "types": list(f.types) or [f.etype],
                "class": f.eclass,
                "hint": f.hint,
                "agents": sorted(f.agents),
                "folders": list(f.folders),
                "in_clear": _occurrences(f.in_clear),
                "replaced": _occurrences(f.replaced),
                "redacted": _occurrences(f.redacted),
            }
            for f in report.findings
        ],
        "personal": dict(report.personal),
        "skipped": [{"where": s.where, "why": s.why} for s in report.skipped],
        "unread": report.unread,
    }


def _console() -> bool:
    """Someone there to see the progress: standard error is a terminal."""
    return sys.stderr.isatty()


def _columns() -> int:
    """The width of the console on standard error, ``_COLUMNS`` where it cannot be told."""
    try:
        return os.get_terminal_size(sys.stderr.fileno()).columns
    except (AttributeError, OSError, ValueError):
        return _COLUMNS


def _now() -> float:
    return time.monotonic()


def _duration(seconds: float) -> str:
    whole = round(seconds)
    if whole < 60:
        return f"{whole} s"
    return f"{whole // 60} min {whole % 60:02d} s"


class _Progress:
    """The scan's progress on standard error, where it is a console (``scan.Progress``): one line refreshed in place, at
    most every ``_REFRESH`` seconds and narrower than the console (Hermes Agent's longest, "Hermes Agent: 9999 sessions
    read, 59 min 59 s; --agent or --since read less", is 75 characters; on a console narrower than it, the hint is left
    out and the line cut), and the time it took at the end."""

    def __init__(self) -> None:
        self.on = _console()
        self.started = _now()
        self.shown: float | None = None
        self.agent = ""
        self.width = 0

    def __call__(self, agent: str, sessions: int) -> None:
        if not self.on:
            return
        now = _now()
        if agent == self.agent and self.shown is not None and now - self.shown < _REFRESH:
            return
        self.shown, self.agent = now, agent
        from latet.leak.sources import AGENTS

        elapsed = now - self.started
        line = f"{AGENTS.get(agent, agent)}: {_plural(sessions, 'session')} read, {_duration(elapsed)}"
        room = min(_columns(), _COLUMNS) - 1  # a line as wide as its console wraps on some
        hint = "; --agent or --since read less" if elapsed > _LONG else ""
        self._show(line + hint if len(line + hint) <= room else line[:room])

    def _show(self, line: str) -> None:
        typer.echo("\r" + line + " " * max(self.width - len(line), 0), err=True, nl=False)
        self.width = len(line)

    def done(self, finished: bool = True) -> None:
        """The line cleared and the time the scan took; where it stopped instead (an error, Ctrl-C), the line ended as
        it stands, so that what follows (a traceback, the shell's prompt) starts a row of its own."""
        if not self.on:
            return
        if not finished:
            if self.width:
                typer.echo("", err=True)
            return
        elapsed = _now() - self.started
        if self.width:
            self._show("")
            typer.echo("\r", err=True, nl=False)
        hint = " --agent or --since read less, for a quicker run." if elapsed > _LONG else ""
        typer.echo(f"Read in {_duration(elapsed)}.{hint}", err=True)


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(2)


def _began(answers: Known | None, report: Report) -> datetime | None:
    """The earliest date Latet's vault and history keep here, asked only where a secret is listed in clear."""
    if answers is None or not any(f.in_clear.sessions for f in report.findings):
        return None
    return answers.latet_since()


@app.command("leak")
def leak(
    agent: Annotated[
        list[str] | None, typer.Option("--agent", help="Read this agent's sessions only (repeat for more).")
    ] = None,
    since: Annotated[str | None, typer.Option("--since", help="Read records from this date on (2026-10-01).")] = None,
    personal: Annotated[bool, typer.Option("--personal", help="Also count personal data, by type.")] = False,
    path: Annotated[
        list[Path] | None,
        typer.Option("--path", help="Also read this file or folder of JSON or JSON lines (repeat for more)."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print records instead of text.")] = False,
    every: Annotated[
        bool,
        typer.Option("--all", help=f"List every secret of each list, not the first {_SHOWN} (--json lists them all)."),
    ] = False,
) -> None:
    """Find the secrets in your coding agents' saved sessions, and say which may have reached a model provider.

    Reads the sessions Claude Code, Codex, OpenCode and the other agents saved on this computer with Latet's own
    detectors. It reads and prints; it changes nothing and never shows a value. Exits 0 when nothing is found, 1 when
    something is, 2 when nothing could be read."""
    from latet.config import load_config
    from latet.leak import known as known_module
    from latet.leak import scan as scan_module
    from latet.leak import sources

    moment: datetime | None = None
    if since is not None:
        try:
            moment = datetime.fromisoformat(since)
        except ValueError:
            raise _fail(f"--since takes a date such as 2026-10-01, not {since!r}.") from None
    sources.remove_old_copies()  # what a run stopped hard left in the temporary folder
    try:
        texts = sources.read(agents=agent or None, extra=path or (), since=moment)
    except ValueError:
        names = ", ".join(sources.AGENTS)
        raise _fail(f"Unknown agent in --agent {', '.join(agent or [])}. The agents are: {names}.") from None

    progress = _Progress()
    finished = False
    try:
        # ``known``: what the scan asks; with no vault and no server, answers that say Latet masked nothing here.
        known, unknown = known_module.answer()
        report = scan_module.scan(texts, load_config(), personal=personal, known=known, progress=progress)
        began = _began(known if unknown is None else None, report)
        finished = True
    finally:
        progress.done(finished)

    if as_json:
        typer.echo(json.dumps(to_json(report, unknown is None, began), indent=2))
    else:
        typer.echo("\n".join(render(report, unknown, began, every)))
    if not report.sessions:
        raise typer.Exit(2)
    found = bool(report.findings or report.personal)
    raise typer.Exit(1 if found else 0)
