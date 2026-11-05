"""What 0.11 stops that 0.10 sent, seen on this computer (the request-coverage plan's P2-7, R-58): projects with Latet
off (D5), text recognition not installed (M3), ``--passthrough`` in a start entry (D5), and the calls seen in the local
history that no built-in profile covers (D1), by method and route template. `latet doctor` lists them, with the
launchers whose sessions Latet launched without their key (R-120); the daemon's first 0.11 start says them once in its
log. By category and count: never a value, a folder, a command line or a request's path as it was sent."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from latet.config import Config, data_dir

log = logging.getLogger(__name__)

# The file the daemon's first 0.11 start leaves in the data folder once it has said what 0.11 stops: said once.
MARKER = "upgrade-0.11-checked"
# The newest rows of the history read, at most: a history of 14 days is far smaller, a bound all the same.
_ROWS_MOST = 20_000
_ENTRY_BYTES = 64 * 1024
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
# The fixed words of the providers' routes a template keeps; any other segment (an id, a model, a deployment) is shown
# as {…}, so that nothing a request named is printed.
_WORDS = frozenset(
    {
        "v1", "v1beta", "v1beta1", "v1alpha", "v2", "api", "openai", "backend-api", "codex",
        "files", "uploads", "parts", "complete", "content", "batches", "cancel", "results",
        "audio", "speech", "transcriptions", "translations", "images", "generations", "edits", "variations",
        "moderations", "responses", "input_items", "input_tokens", "compact", "models", "model", "messages",
        "count_tokens", "chat", "completions", "embeddings", "cachedContents", "operations", "organization",
        "usage", "costs", "assistants", "threads", "runs", "steps", "vector_stores", "fine_tuning", "jobs",
        "checkpoints", "realtime", "sessions", "conversations", "items", "skills", "versions", "inference-profiles",
        "invoke", "invoke-with-response-stream", "converse", "converse-stream", "count-tokens", "projects",
        "locations", "publishers", "google", "anthropic", "generateContent", "streamGenerateContent", "countTokens",
        "embedContent", "batchEmbedContents", "batchGenerateContent", "predict", "rawPredict", "streamRawPredict",
    }
)  # fmt: skip
_AGENTS = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "gemini-cli": "Gemini CLI",
    "tutorial-agent": "Tutorial Agent",
}


@dataclass(frozen=True)
class Finding:
    """One thing 0.11 stops, by its category and how many: what it is (``title``), what happens and what to do
    (``detail``) and, for some, where (``lines``: a start entry's file, a route template, an agent and its version)."""

    category: str
    count: int
    title: str
    detail: str
    lines: tuple[str, ...] = ()

    def said(self) -> str:
        return f"{self.title}: {self.detail}" + (f": {'; '.join(self.lines)}" if self.lines else "")


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"


def findings(config: Config, data: Path | None = None) -> list[Finding]:
    """What 0.11 stops here that 0.10 sent: projects with Latet off, images no one reads, ``--passthrough`` in a start
    entry, and calls in the local history no built-in profile covers. Empty: nothing seen."""
    from latet.vision.policy import image_policy

    data = data_dir() if data is None else data
    out: list[Finding] = []
    off = sum(1 for project in config.projects if not project.enabled)
    if off:
        out.append(
            Finding(
                "projects-off",
                off,
                "projects with Latet off",
                f"{_n(off, 'project')}: in 0.11 their agents' requests are refused instead of being sent unmasked. "
                "The dashboard's Rules page names them, `latet project show` says it of a folder, `latet project on` "
                "turns Latet back on there; or start the agent without Latet, pointed at its provider, to reach the "
                "provider directly",
            )
        )
    if not image_policy(config).blocks and not _reads_images(config):
        out.append(
            Finding(
                "text-recognition",
                1,
                "images",
                "text recognition is not installed (or `ocr: off`): in 0.11 a request that carries an image is "
                "refused instead of being sent as it is. `latet ocr install` reads the text in images and masks it",
            )
        )
    entries = tuple(name for name, text in start_entries() if _passthrough(text))
    if entries:
        out.append(
            Finding(
                "passthrough",
                len(entries),
                "--passthrough",
                f"{_n(len(entries), 'start entry', 'start entries')} run `latet serve --passthrough`, which now exits "
                "with a message instead of forwarding unmasked: remove the option",
                entries,
            )
        )
    calls = _uncovered(_rows(data))
    if calls:
        out.append(
            Finding(
                "ancillary",
                len(calls),
                "calls no built-in profile covers",
                f"{_n(len(calls), 'route')} seen in your history: 0.11 does not support them and refuses each, "
                "so change the agent or its settings so that it does not make that call (0.10 recorded no method: "
                "a call without a body is shown as GET, one with a body as POST)",
                tuple(f"{method} {template} ({count} seen)" for (method, template), count in calls),
            )
        )
    return out


def launches(data: Path | None = None) -> list[Finding]:
    """The launchers whose sessions Latet launched without the key it gave them (R-120): each agent with its version,
    from the history's rows, and how many requests. Those requests were decided as claimed: their project's rules that
    add protection applied, its loosenings and exceptions did not."""
    from latet.storage.exchanges import _history_version

    rows = _rows(data_dir() if data is None else data)
    carriers = _carriers(rows)
    seen: Counter[tuple[str, str | None]] = Counter()
    for summary, _ in rows:
        if summary.get("launch") == "launched-key-missing":
            agent = summary.get("agent")
            name = _AGENTS.get(agent, "another agent") if type(agent) is str else "another agent"
            seen[(name, _history_version(summary.get("agent_version")))] += 1
    if not seen:
        return [carriers] if carriers is not None else []
    ordered = sorted(seen.items(), key=lambda item: (-item[1], item[0][0], item[0][1] is None, item[0][1] or ""))
    return [
        *([carriers] if carriers is not None else []),
        Finding(
            "launched-key-missing",
            sum(seen.values()),
            "launched by Latet without their key",
            "their requests were decided as claimed: the project's rules that add protection applied, its "
            "loosenings and exceptions did not. Update the agent, or start it again with its `latet` command",
            tuple(
                f"{name}{f' {version}' if version else ''} ({_n(count, 'request')})"
                for (name, version), count in ordered
            ),
        ),
    ]


def _release(version: str | None) -> tuple[int, ...] | None:
    """A release number's parts (``0.114.0`` is (0, 114, 0)); None for no version or one past its grammar."""
    found = re.match(r"([0-9]{1,6})\.([0-9]{1,6})(?:\.([0-9]{1,6}))?", version or "")
    return tuple(int(part or 0) for part in found.groups()) if found else None


def _no_carrier(agent: str, version: str | None, upstream: Any) -> str | None:
    """Why the launcher of ``agent`` passes it no launch key, from the pinned carrier table (R-119; P2-8a's
    ``latet.launch_carriers``, in the launchers' own sentences of ``launch_session``): a version below its carrier's
    floor (Codex 0.115, OpenCode 1.19.5, Gemini CLI 0.19), and Codex on its built-in providers (ChatGPT sign-in
    included). None where the table says the launcher's carrier is verified, for a version the history did not read,
    or for an agent the table says nothing of."""
    from latet.launch_carriers import floor
    from latet.launch_session import _WHY

    release = _release(version)
    below = release is not None and (pinned := floor(agent)) is not None and release < pinned
    if agent == "codex":
        if below:
            return _WHY["older-codex"]
        return _WHY["builtin-provider"] if upstream in ("openai", "chatgpt") else None
    if agent == "opencode":
        return _WHY["older-opencode"] if below else None
    if agent == "gemini-cli":
        return _WHY["older-gemini"] if below else None
    return None


def _carriers(rows: list[tuple[dict[str, Any], int]]) -> Finding | None:
    """The launchers whose carrier is not verified, for each agent and version the history saw decided as claimed:
    named by the static carrier table, with how many requests. Their sessions stay claimed."""
    from latet.storage.exchanges import _history_version

    seen: Counter[tuple[str, str | None, str]] = Counter()
    for summary, _ in rows:
        agent = summary.get("agent")
        if summary.get("launch") != "claimed" or type(agent) is not str or agent not in _AGENTS:
            continue
        version = _history_version(summary.get("agent_version"))
        why = _no_carrier(agent, version, summary.get("upstream"))
        if why is not None:
            seen[(_AGENTS[agent], version, why)] += 1
    if not seen:
        return None
    ordered = sorted(seen.items(), key=lambda item: (item[0][0], _release(item[0][1]) or (), item[0][2]))
    return Finding(
        "carrier-unverified",
        sum(seen.values()),
        "launchers whose carrier is not verified",
        "these agents were started by hand, or by a Latet launcher that gives them no launch key, so their "
        "requests were decided as claimed: the project's rules that add protection applied, its loosenings and "
        "exceptions did not",
        tuple(
            f"{name}{f' {version}' if version else ''}: {why} ({_n(count, 'request')})"
            for (name, version, why), count in ordered
        ),
    )


def first_start(config: Config, data: Path) -> bool:
    """At the daemon's first 0.11 start (no ``MARKER`` in ``data``): what 0.11 stops, said once in its log, then
    remembered. False when it was said before. A history or an entry it cannot read counts as nothing seen there."""
    marker = data / MARKER
    if marker.exists():
        return False
    found = findings(config, data)
    if found:
        log.warning("what 0.11 stops that 0.10 sent, seen here (`latet doctor` says it again):")
        for finding in found:
            log.warning("%s", finding.said())
    try:
        from latet.storage.folders import make_private_folder

        make_private_folder(data)
        marker.write_text("0.11\n", encoding="utf-8")
    except OSError as exc:
        log.debug("the upgrade's checks were not remembered: %s", type(exc).__name__)
    return True


def _reads_images(config: Config) -> bool:
    """Whether this computer reads the text in images: text recognition installed (its extra and its models) and not
    turned off. Without it, 0.11 refuses a request that carries an image (M3)."""
    from latet.vision import models

    return config.ocr != "off" and models.runtime_available() and models.installed(data_dir())


def start_entries(system: str | None = None) -> list[tuple[str, str]]:
    """What starts programs at login where Latet installs its own start (``service.autostart``), each by its kind and
    its file's name (never the folder, which names the user) and its text: the systemd user units (Linux), the launchd
    agents (macOS), the values of the Run key (Windows). Only those that name Latet; nothing that cannot be read."""
    system = system or ("windows" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux")
    out: list[tuple[str, str]] = []
    if system == "windows":
        out = list(_run_values())
    else:
        if system == "darwin":
            folder, pattern, kind = Path.home() / "Library" / "LaunchAgents", "*.plist", "launchd agent"
        else:
            config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
            folder, pattern, kind = Path(config) / "systemd" / "user", "*.service", "systemd user unit"
        try:
            files = sorted(folder.glob(pattern))
        except OSError:
            files = []
        for file in files:
            try:
                with file.open("rb") as handle:
                    text = handle.read(_ENTRY_BYTES).decode("utf-8", "replace")
            except OSError:
                continue
            out.append((f"{kind} {file.name}", text))  # by its kind and file name: never the folder
    return [(name, text) for name, text in out if "latet" in text.lower()]


def _run_values() -> Iterator[tuple[str, str]]:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            for n in range(1024):
                try:
                    name, value, _ = winreg.EnumValue(key, n)
                except OSError:
                    return
                if type(value) is str:
                    yield f"HKCU Run: {name}", value[:_ENTRY_BYTES]
    except (ImportError, OSError):
        return


def _passthrough(text: str) -> bool:
    return re.search(r"latet[^\n]*\bserve\b[^\n]*--passthrough(?![\w-])", text, re.IGNORECASE) is not None


def _rows(data: Path) -> list[tuple[dict[str, Any], int]]:
    """The newest rows of the local history, each summary as it was written (a 0.10 row's included) and its body's
    size; read only, never written. Empty when there is none, or it cannot be read."""
    path = data / "exchanges.db"
    if not path.exists():
        return []
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            found = db.execute(
                "SELECT summary, body_bytes FROM exchanges ORDER BY ts DESC LIMIT ?", (_ROWS_MOST,)
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return []
    out = []
    for raw, size in found:
        try:
            summary = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if type(summary) is dict:
            out.append((summary, size if type(size) is int else 0))
    return out


def _templates() -> list[re.Pattern[str]]:
    from latet.storage.exchanges import _HISTORY_ROUTES

    return [re.compile(re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(route))) for route in _HISTORY_ROUTES]


def _uncovered(rows: list[tuple[dict[str, Any], int]]) -> list[tuple[tuple[str, str], int]]:
    """The calls of ``rows`` no built-in profile covers, by method and route template, the most seen first: a 0.10
    row's path as it was sent, matched against the routes 0.11's profiles admit. A 0.11 row (a route template, an
    area) was decided under 0.11 already."""
    from latet.storage.exchanges import _HISTORY_AREAS, _HISTORY_ROUTES

    templates = _templates()
    seen: Counter[tuple[str, str]] = Counter()
    for summary, size in rows:
        path = summary.get("path")
        if type(path) is not str or not path.startswith("/") or len(path) > 2048:
            continue
        if path in _HISTORY_ROUTES or path in _HISTORY_AREAS or path.startswith("/_latet"):
            continue
        bare = path.split("?", 1)[0]
        if any(template.fullmatch(bare) for template in templates):
            continue
        method = summary.get("method")
        method = method if method in _METHODS else ("POST" if size > 0 else "GET")
        seen[(method, _template(bare))] += 1
    return sorted(seen.items(), key=lambda item: (-item[1], item[0][1], item[0][0]))


def _template(path: str) -> str:
    """A path as a route template: its first segment (the destination) as ``{destination}``, each fixed word of the
    providers' routes kept and any other segment ``{…}``."""
    parts = path.strip("/").split("/")[1:]
    out = ["{destination}"]
    for part in parts:
        if ":" in part:
            head, _, operation = part.partition(":")
            out.append(f"{head if head in _WORDS else '{…}'}:{operation if operation in _WORDS else '{…}'}")
        else:
            out.append(part if part in _WORDS else "{…}")
    return "/" + "/".join(out)
