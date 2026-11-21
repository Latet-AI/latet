"""The daily check's report (`.github/workflows/agent-drift.yml`): every request an agent sent to the stand-in of its
provider (`stand_in.py`'s record, one JSON line each), read by Latet's inspection as the gateway reads it, and each
member no profile declares named by its JSON path, the member's name last.

A request is read as the gateway reads it: its route from its path (``RouteTable``), its protocol (``protocol_for``),
whether Latet only checks it (``verify_only``: Claude Code's, by its headers), the body scan of its protocol; and its
body by ``inspect_body`` under ``unknown_members: check``, told the place of each member no profile declares
(``observe``). A body the inspection refuses all the same (a dedicated reader's member, as ``stream_options``, which the
walk's new rule never reaches) is listed by its category. A run fails too when it did not start, crashed, hung, sent no
request, or did not finish its scripted turn (the shell tool called, its result brought back): a canary that sees
nothing says so.

The names are the canary's own traffic, never a person's. The summary goes to ``--summary`` (by default the file
``$GITHUB_STEP_SUMMARY`` names) and to the standard output; the status is 1 when anything was found, 0 otherwise.

    uv run python scripts/agent_drift/report.py RECORD.jsonl [RECORD.jsonl ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latet.gateway.body_coverage import BodyRefused, Ledger, inspect_body
from latet.gateway.cloud import shape_of
from latet.gateway.mediate import _scan, protocol_for, verify_only  # the gateway's own reading of a request
from latet.gateway.routes import RouteTable
from latet.protocols import codec

ROUTES = RouteTable()
# Where the stand-in takes each protocol (the routes of Latet's own), for a body read without its path.
PATHS = {
    "anthropic": "/anthropic/v1/messages",
    "responses": "/openai/v1/responses",
    "chat": "/openai/v1/chat/completions",
}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_BACKTICK = "`"


@dataclass(frozen=True)
class Reading:
    """One request as the inspection read it: ``paths``, each member no profile declares, by its JSON path (once each,
    in the order it was met), and ``names``, its name, the path's last step; ``refused``, the category of the refusal
    of its body, None when it passes; and whether Latet only checks it (``checked_only``), as Claude Code's."""

    paths: tuple[str, ...] = ()
    refused: str | None = None
    checked_only: bool = False
    names: tuple[str, ...] = ()


def _quoted(name: str) -> str:
    """A key that is no plain name, quoted as JSON with no backtick: the summary's code span shows it as it is."""
    return json.dumps(name, ensure_ascii=True).replace(_BACKTICK, "\\u0060")


def json_path(place: tuple[str | int, ...]) -> str:
    """A member's place (its keys and indexes from the body's top) as a JSON path, any key no plain name quoted."""
    steps = ["$"]
    for step in place:
        if isinstance(step, int):
            steps.append(f"[{step}]")
        elif _NAME.match(step):
            steps.append("." + step)
        else:
            steps.append("[" + _quoted(step) + "]")
    return "".join(steps)


def _name(place: tuple[str | int, ...]) -> str:
    last = str(place[-1]) if place else ""
    return last if _NAME.match(last) else _quoted(last)


def read(
    body: Any,
    protocol: str,
    *,
    method: str = "POST",
    path: str | None = None,
    headers: dict[str, str] | None = None,
) -> Reading:
    """``body`` as the gateway reads a request of ``protocol`` sent to ``path`` (by default the stand-in's route of the
    protocol) with ``headers`` (no credential needed: Claude Code is known by its session's header)."""
    path = path or PATHS[protocol]
    headers = {name.lower(): value for name, value in (headers or {}).items()}
    upstream = ROUTES.match(path)
    if upstream is None:
        return Reading(refused="no route of Latet's")
    if not isinstance(body, dict):
        return Reading(refused="not an object")
    checked_only = verify_only(protocol, shape_of(method, path), headers, body)
    if checked_only and codec.coded(headers.get("content-encoding")):
        return Reading(refused="a coded body where Latet only checks", checked_only=True)
    try:
        scan = _scan(protocol, body)
    except (TypeError, KeyError):
        return Reading(refused="body-shape", checked_only=checked_only)
    refused = "unreadable: " + ", ".join(scan.unreadable) if scan.unreadable else None
    places: list[tuple[str | int, ...]] = []
    try:
        inspection = inspect_body(
            body, protocol, upstream, scan, checked_only, Ledger(), unknown_members="check", observe=places.append
        )
    except BodyRefused as error:
        refused = refused or error.code
    else:
        refused = refused or inspection.problem
    found = {json_path(place): _name(place) for place in places}  # once each, in the order met
    return Reading(tuple(found), refused, checked_only, tuple(found.values()))


def undeclared(body: Any, protocol: str, **selection: Any) -> list[str]:
    """Each member of ``body`` no profile declares, by its JSON path (``selection``: ``read``'s method, path and
    headers)."""
    return list(read(body, protocol, **selection).paths)


@dataclass
class Run:
    """One agent's run, as its record tells it."""

    name: str
    agent: str | None = None  # its user agent, as its first request gave it
    requests: int = 0  # the requests with a body Latet reads
    paths: Counter[str] = field(default_factory=Counter)  # each member no profile declares: in how many requests
    names: dict[str, str] = field(default_factory=dict)  # each one's name, by its path
    refused: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.paths or self.refused or self.problems)


def _ending(agent: dict[str, Any]) -> str | None:
    if "timeout" in agent:
        return f"it hung: stopped after {agent['timeout']} s"
    if "error" in agent:
        return f"it did not start ({agent['error']})"
    if agent.get("exit") != 0:
        return f"it exited with status {agent.get('exit')}"
    return None


def run(record: Path) -> Run:
    """The run ``record`` holds, every request of it read."""
    found = Run(record.stem)
    if not record.is_file():
        found.problems.append("no record of it: the run did not start")
        return found
    entries: list[dict[str, Any]] = []
    for line in record.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line) if line.strip() else {}
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            found.problems.append("its record holds a line that is no entry of the stand-in's")
        elif entry:
            entries.append(entry)
    sent = [entry for entry in entries if "request" in entry]
    for number, entry in enumerate(sent, 1):
        request = entry["request"]
        headers = request.get("headers") or {}
        found.agent = found.agent or headers.get("user-agent")
        method, path, body = request.get("method", ""), request.get("path", ""), request.get("body")
        where = f"request {number}, {method} {path}"
        if body is None and request.get("unreadable") is None:
            continue  # a call with no body (a model list): nothing of it is a member
        upstream = ROUTES.match(path)
        protocol = protocol_for(upstream, method, path) if upstream is not None else None
        if protocol is None:
            found.refused.append(f"{where}: refused (a body Latet does not read)")
            continue
        if request.get("unreadable") is not None:
            found.refused.append(f"{where}: refused (unreadable body: {request['unreadable']})")
            continue
        found.requests += 1
        reading = read(body, protocol, method=method, path=path, headers=headers)
        found.paths.update(reading.paths)
        found.names.update(zip(reading.paths, reading.names, strict=True))
        if reading.refused is not None:
            found.refused.append(f"{where}: refused ({reading.refused})")
    endings = [entry["agent"] for entry in entries if "agent" in entry]
    if not endings:
        found.problems.append("its end was not recorded: the stand-in stopped before the agent")
    elif (ending := _ending(endings[-1])) is not None:
        found.problems.append(ending)
    answered = {entry.get("answered") for entry in sent}
    if not sent:
        found.problems.append("it sent no request")
    elif "tool call" not in answered:
        found.problems.append("it called no shell tool: the stand-in found none it knows in its requests")
    elif "final text" not in answered:
        found.problems.append("no request brought the tool's result back")
    return found


def summary(runs: list[Run]) -> str:
    lines = ["## Agent drift", ""]
    if not runs:
        lines.append("Nothing was read: no run was recorded.")
    elif not any(found.failed for found in runs):
        lines.append("Every request of every run was read with no member no profile declares.")
    else:
        lines.append(
            "What the runs found is listed below, each member no profile declares by its JSON path, the member's name"
            " last."
        )
    for found in runs:
        lines += ["", f"### {found.name}" + (f": `{found.agent.replace(_BACKTICK, '')}`" if found.agent else "")]
        if not found.failed:
            lines.append(f"{found.requests} requests, each read with no member no profile declares.")
            continue
        for path, count in found.paths.items():
            lines.append(f"- `{found.names[path]}` at `{path}`, in {count} of {found.requests} requests")
        lines += [f"- {refused}" for refused in found.refused]
        lines += [f"- {problem}" for problem in found.problems]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Name each member no profile declares in the agents' requests.")
    parser.add_argument("records", nargs="*", type=Path, help="the stand-in's record of each run")
    parser.add_argument("--summary", type=Path, help="where the summary goes (default: $GITHUB_STEP_SUMMARY)")
    arguments = parser.parse_args(argv)
    runs = [run(record) for record in arguments.records]
    text = summary(runs)
    sys.stdout.write(text)
    target = arguments.summary
    if target is None and os.environ.get("GITHUB_STEP_SUMMARY"):
        target = Path(os.environ["GITHUB_STEP_SUMMARY"])
    if target is not None:
        with target.open("a", encoding="utf-8") as out:
            out.write(text)
    return 1 if not runs or any(found.failed for found in runs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
