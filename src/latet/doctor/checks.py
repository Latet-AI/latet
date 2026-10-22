"""``latet doctor``: is everything wired up, and does masking work end to end on this machine?"""

from __future__ import annotations

import datetime
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from latet.config import data_dir, load_config
from latet.gateway.aws import cli_folders, find_aws_cli

if TYPE_CHECKING:
    from latet.integrations.agents import ClaudeCode
    from latet.server.connection import _PreparedConnection

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""


def _diagnostic_connection() -> _PreparedConnection:
    """This computer's connection as `latet doctor` and `latet status` read it: none when there is no connection file
    (local Latet: nothing of a connection is admitted, R1); else admitted, its refusal said in words that name the
    folder, why and the fix."""
    from latet.server import connection

    selected = connection._selected_connection()
    if connection.connection_present(selected) and (problem := connection.connection_folder_problem()):
        raise connection.NotConnected(f"this computer's connection is unavailable: {problem}")
    prepared = connection._PreparedConnection(selected, connection._load_connection(selected))
    connection._require_selection(prepared)
    return prepared


def server_refusal(exc: Exception) -> str:
    """Controlled server words, or an unexpected exception's type alone; never an exception's raw value."""
    from latet.server.client import ServerError, printable
    from latet.server.connection import NotConnected

    if isinstance(exc, NotConnected):
        return printable(exc.message, 2000)
    if isinstance(exc, ServerError):
        detail = "; ".join([printable(exc.message), *(printable(item) for item in exc.details)])
        if exc.retry_after is not None:
            detail += f"; retry after {exc.retry_after} seconds"
        return detail
    return type(exc).__name__


def check_server(now: datetime.datetime | None = None, *, _prepared: _PreparedConnection | None = None) -> list[Check]:
    from latet.engine.reviewer import certificate
    from latet.server.client import ServerClient, ServerError, _connection_credential, _connection_pin, printable

    try:
        prepared = _prepared if _prepared is not None else _diagnostic_connection()
    except Exception as exc:
        return [Check(FAIL, "Connection", server_refusal(exc))]
    connection = prepared.connection
    if connection is None:
        return []
    out = [Check(OK, "Server", f"{connection.url}; {printable(connection.user_name)} ({connection.user})")]
    pem = None
    try:
        pem = _connection_pin(prepared)
        prepared = _connection_credential(prepared)
        with ServerClient(
            connection.url, prepared.token, connection.fingerprint, _prepared=prepared, _pem=pem
        ) as client:
            answer = client.whoami()
        if (
            not isinstance(answer, dict)
            or answer.get("id") != connection.user
            or answer.get("kind") != "person"
            or not isinstance(answer.get("version"), str)
            or not 1 <= len(answer["version"]) <= 500
            or not isinstance(answer.get("name"), str)
            or len(answer["name"]) > 1000
        ):
            raise ServerError(0, "the server's identity answer is not a matching personal identity and version")
        out.append(
            Check(OK, "Server token", f"accepted; {printable(answer['name'])}; Latet {printable(answer['version'])}")
        )
        from latet.server.connection import _prepare_key, key_missing

        if _prepare_key(prepared).key is None:  # R16: the agents work; the dashboard and the identity need it
            out.append(Check(WARN, "Computer key", key_missing(connection)))
    except ServerError as exc:
        if exc.status == 401:  # R7: why the server may refuse it, and what to do
            from latet.server.connection import token_refused

            out.append(Check(FAIL, "Server token", token_refused(connection)))
        else:
            out.append(Check(FAIL, "Server connection", server_refusal(exc)))
    except Exception as exc:
        out.append(Check(FAIL, "Server connection", server_refusal(exc)))
    if pem is not None:
        try:
            until = certificate.expires(pem)
            current = now or datetime.datetime.now(datetime.UTC)
            state = FAIL if until <= current else WARN if until <= current + datetime.timedelta(days=30) else OK
            out.append(Check(state, "Server certificate", f"expires {until.date().isoformat()}"))
        except Exception as exc:
            out.append(Check(FAIL, "Server certificate", f"expiry unavailable ({type(exc).__name__})"))
    return out


def check_folders() -> list[Check]:
    """Who else Latet trusts on the parent folders of its data folder, beyond you, SYSTEM, the Administrators and
    TrustedInstaller (R29): the accounts LATET_TRUSTED_ACCOUNTS names, with where it is set in the registry (R30), and
    each of its entries that trusts nothing; a value this terminal's environment alone holds, which Latet does not read;
    the direct members of the Administrators group whose entry on one of those folders it accepted; and, on a
    computer not connected to a Latet server, each of those folders another account may change, which Latet goes on
    past (R28), with the ways out."""
    import sys

    from latet.server.connection import connection_present
    from latet.storage import folders

    out: list[Check] = []
    variable, here = folders.TRUSTED_ACCOUNTS, os.environ.get(folders.TRUSTED_ACCOUNTS, "").strip()
    if sys.platform != "win32":
        if here:
            detail = f"{variable} is set, and changes nothing here: on Linux and macOS Latet's folder checks are by "
            out.append(Check(WARN, "Trusted accounts", detail + "owner and mode"))
    else:
        named, unresolved = folders.trusted_accounts()  # R30: the registry's environments, never this process's
        for item in named:
            detail = f"{folders.account_name(item.sid)} may change the parent folders of Latet's data folder, as the "
            out.append(Check(OK, "Trusted accounts", detail + f"Administrators may ({variable} in {item.where})"))
        out += [Check(WARN, "Trusted accounts", words) for words in unresolved]
        if here and here not in {text.strip() for _, text in folders.trusted_values()[0]}:
            detail = (
                f"{variable} is set in this terminal's environment alone, where Latet does not read it (a "
                "repository's settings or an agent's session could set it there): set it for your account with "
                f'`setx {variable} "DOMAIN\\name"`, or in the system settings, then open a new terminal'
            )
            out.append(Check(WARN, "Trusted accounts", detail))
    data = data_dir() if data_dir().is_absolute() else Path.cwd() / data_dir()
    parents: list[tuple[Path, os.stat_result]] = []
    for parent in reversed(data.parents):
        try:
            parents.append((parent, parent.lstat()))
        except OSError:
            break
    members = dict.fromkeys(sid for parent, _ in parents for sid in folders.members_trusted(parent))
    for sid in members:
        detail = f"{folders.account_name(sid)}, a direct member of the Administrators group, may change the parent "
        out.append(Check(OK, "Administrators' members", detail + "folders of Latet's data folder, as the group may"))
    if not connection_present():
        out += [
            Check(WARN, "Data folder", words)
            for parent, found in parents
            if (words := folders.namespace_warning(parent, found)) is not None
        ]
    return out


def _settings_folder_problem(folder: Path) -> str | None:
    """Why another account could change Claude Code's settings folder, in words that name the fix; None when only you
    can. On POSIX: owned by you, written by nobody else and, on macOS, no access-list entry that lets another account
    in. A folder others may only read and go into (macOS makes ~/.claude 0755) shows them no file that is yours alone,
    as `latet agents apply claude` writes settings.json (0600): the file's own mode says what they read. On Windows:
    its access list, as for a secret's folder."""
    from latet.storage import folders

    if os.name == "nt":
        return folders.private_folder_problem(folder)
    found = os.stat(folder)
    if found.st_uid != os.getuid() or found.st_mode & 0o022:
        return folders.private_folder_problem(folder)
    if sys.platform == "darwin":
        try:
            if folders._darwin_allows(folder, found):
                return (
                    f"{folder} has an access-list entry that lets another account in (`ls -led {folder}` shows it): "
                    f"`chmod -N {folder}`"
                )
        except OSError:
            return f"the access list of {folder} cannot be read"
    return None


def _record_here(agent: Any, record: Any) -> bool:
    """Whether Latet's record of an agent names the settings file it reads now: one Latet wrote under another
    CLAUDE_CONFIG_DIR, CODEX_HOME or XDG_CONFIG_HOME put nothing in these. A record that names no file counts."""
    from latet.integrations.agents import ClaudeCode

    current = agent.settings_path() if isinstance(agent, ClaudeCode) else agent.config_path()
    here = os.path.normcase(os.path.abspath(current))
    return not record.files or any(os.path.normcase(os.path.abspath(f.path)) == here for f in record.files)


def _claude_settings(agent: ClaudeCode) -> tuple[Check, dict[str, Any] | None]:
    from latet.integrations import agents, base
    from latet.storage.folders import private_folder_problem

    title = "Claude Code credentials"
    path = agent.settings_path()

    def unknown(reason: str) -> tuple[Check, None]:
        return Check(FAIL, title, f"unknown: {reason}; use a private settings folder"), None

    def exposed(problem: str, then: str) -> tuple[Check, None]:
        """Others may read the file: exposed once Latet applied Claude Code there (its token or route may be in it);
        before, Claude Code's own file (macOS makes the folder 0755, Claude Code the file 0644), never read."""
        record = base.load_records().get(agent.id)
        if record is not None and _record_here(agent, record):
            return Check(FAIL, title, f"exposed: {problem}"), None
        return Check(WARN, title, f"not read: {problem} ({then})"), None

    try:
        if not base._namespace(path.parent):
            return Check(OK, title, "absent: no settings file"), {}
        if (problem := _settings_folder_problem(path.parent)) is not None:
            return exposed(problem, "Latet keeps nothing of its own there yet")
        found = base._metadata(path)
        if found is None:
            return Check(OK, title, "absent: no settings file"), {}
        base._plain_path(path)
        if not stat.S_ISREG(found.st_mode):
            return unknown("settings is not a regular file")
        applying = "`latet agents apply claude` makes it yours alone"
        if os.name == "nt" and private_folder_problem(path) is not None:
            return exposed(f"{path}'s access list lets another account read it: {applying}", applying)
        if os.name != "nt" and found.st_uid != os.getuid():
            return Check(FAIL, title, f"exposed: {path} belongs to another account"), None
        # On POSIX nobody else reaches a file in a folder they cannot go into (`chmod 700` on the folder)
        if os.name != "nt" and found.st_mode & 0o077 and os.stat(path.parent).st_mode & 0o011:
            mode = stat.S_IMODE(found.st_mode)
            return exposed(f"other accounts can read {path} (mode {mode:04o}): `chmod 600 {path}`", applying)
        if agents._roaming(path.parent) is not None:
            return unknown("the settings folder may roam")
        # Use the reviewed desktop-record ceiling; no second doctor-specific byte policy. Its folder was checked above,
        # by the rule for an agent's settings (macOS's ~/.claude, 0755, passes), not by the stricter one for Latet's own
        # secrets, which would refuse it
        raw = base._checked_bytes(path, base.RECORD_BYTES + 1, folder_checked=True)
        if len(raw) > base.RECORD_BYTES:
            return unknown("settings exceeds the bounded diagnostic snapshot")
        own = json.loads(raw.decode("utf-8"), object_pairs_hook=base._unique_object)
        if not isinstance(own, dict):
            return unknown("settings is not an object")
        env = own.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            return unknown("settings env is not a string mapping")
    except (base.RecordsUnavailable, OSError, ValueError, RecursionError):
        return unknown("settings cannot be safely inspected")
    present = bool(env.get("LATET_BRIDGE_TOKEN")) or any(
        "/u/" in value
        for key, value in env.items()
        if key
        in (
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_BEDROCK_BASE_URL",
            "ANTHROPIC_VERTEX_BASE_URL",
            "ANTHROPIC_FOUNDRY_BASE_URL",
        )
    )
    return Check(
        WARN if present else OK, title, "present: treat the token and route as secrets" if present else "absent"
    ), own


def _bridge_ping(port: int, token: str) -> bool:
    from latet.service.daemon import local_urlopen

    request = urllib.request.Request(f"http://127.0.0.1:{port}/_latet/engine/v1/ping", headers={"x-latet-token": token})
    try:
        with local_urlopen(request, timeout=3) as response:
            return json.loads(response.read()).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _stopped() -> Check:
    """The daemon not running: a failure once an agent goes through Latet for good or Latet starts at login; before
    that (right after the installer) a warning, as the wrappers start it themselves."""
    from latet.integrations.agents import ALL
    from latet.integrations.base import RecordsUnavailable, load_records
    from latet.service import autostart

    try:
        routed = [
            ALL[key].name for key, record in load_records().items() if key in ALL and _record_here(ALL[key], record)
        ]
    except (RecordsUnavailable, OSError):
        return Check(FAIL, "Daemon", "not running: `latet start`")
    if routed:
        who = f"{', '.join(routed[:-1])} and {routed[-1]} go" if len(routed) > 1 else f"{routed[0]} goes"
        return Check(FAIL, "Daemon", f"not running, and {who} through it: `latet start`")
    if autostart.installed():
        return Check(FAIL, "Daemon", "not running, though it starts at login: `latet start`")
    return Check(WARN, "Daemon", "not running: `latet start`, or `latet claude` (and the other wrappers) start it")


def check_daemon() -> list[Check]:
    from latet.service import autostart, daemon
    from latet.storage.keys import load_bridge_token

    config = load_config()
    current = daemon.status(config.port)
    out = []
    if current.running:
        mode = "masking" if current.masking else "PASS-THROUGH (not masking)"
        out.append(Check(OK if current.masking else FAIL, "Daemon", f"running on 127.0.0.1:{config.port}, {mode}"))
        token = load_bridge_token(data_dir())
        bridged = _bridge_ping(config.port, token)
        out.append(Check(OK if bridged else FAIL, "Plugin bridge", "token accepted" if bridged else "token rejected"))
        if current.unreadable:
            what = f"{current.unreadable} value{'s' if current.unreadable != 1 else ''}"
            out.append(
                Check(
                    WARN,
                    "Vault",
                    f"{what} sealed with another root key cannot be read: was the key replaced? `latet restore` "
                    "puts a backup's key back",
                )
            )
    else:
        out.append(_stopped())
    from latet.config import in_container

    if in_container():
        out.append(Check(OK, "Start at login", "the container's restart policy starts it"))
    else:
        out.append(
            Check(
                OK if autostart.installed() else WARN,
                "Start at login",
                "installed" if autostart.installed() else "not installed: `latet service install`",
            )
        )
    return out


def check_port() -> list[Check]:
    from latet.service import ports

    state = ports.probe(load_config().port)
    if state.reason == "latet":
        return [Check(OK, "Port", f"{state.port}, in use by Latet")]
    if state.usable:
        return [Check(OK, "Port", f"{state.port} is free")]
    return [Check(FAIL, "Port", f"{state.port} is {state.reason}: `latet global-install` moves Latet to a free port")]


def check_engine() -> list[Check]:
    """Mask a canary text with the real configuration and restore it, in process."""
    from latet.engine.build import build
    from latet.engine.rehydrate import restore_text, restores_for
    from latet.engine.types import SlotKind
    from latet.engine.vault import Vault
    from latet.runtime import resolve_identity

    config = load_config()
    vault = Vault(os.urandom(32))
    built = build(config, vault, resolve_identity(config))
    token = "ghp_" + "Z9yX8wV7uT6sR5qP4oN3mL2kJ1iH0gF9eD8c"
    canary = f"API_TOKEN={token}\nOWNER=jane.doe@example-corp.it\nIBAN=IT60X0542811101000000123456\n"
    masked = built.masker.mask(canary, SlotKind.TOOL_RESULT)
    leaked = [v for v in (token, "jane.doe@example-corp.it", "IT60X0542811101000000123456") if v in masked.text]
    restored, _ = restore_text(masked.text, restores_for(vault.entities()))
    if leaked:
        return [Check(FAIL, "Masking self-test", f"{len(leaked)} canary value(s) not masked")]
    if restored != canary:
        return [Check(FAIL, "Masking self-test", "restore did not give back the original")]
    return [
        Check(
            OK, "Masking self-test", f"{len(masked.replacements)} values masked and restored ({config.profile} profile)"
        )
    ]


def check_agents() -> list[Check]:
    from latet.integrations.agents import ALL, WRAPPED, ClaudeCode, Codex
    from latet.integrations.base import records_preflight
    from latet.server.connection import _prepare_token, _require_selection

    records_preflight()
    prepared = _prepare_token(_diagnostic_connection())
    out = []
    for agent in ALL.values():
        _require_selection(prepared)
        own = None
        if isinstance(agent, ClaudeCode):
            exposure, own = _claude_settings(agent)
            out.append(exposure)
            if own is None:
                continue
        info = agent.detect()
        if not info.installed:
            continue
        state = agent.status(_settings=own, _prepared=prepared) if isinstance(agent, ClaudeCode) else agent.status()
        status = OK if state == "applied" else WARN
        detail = f"{info.version or ''} - {state}".strip(" -")
        out.append(Check(status, info.name, detail + ("" if state == "applied" else ": `latet agents apply`")))
        out += [Check(WARN, info.name, note) for note in info.notes if "older" in note or "cannot" in note]
        if isinstance(agent, ClaudeCode) and state == "applied":
            out += user_name_checks(agent)
            if prepared.connection is not None:
                out += helper_checks(prepared, own or {})
        if isinstance(agent, (ClaudeCode, Codex)):
            out += route_checks(agent, _settings=own, _prepared=prepared)
    for wrapped in WRAPPED.values():  # protected when started with Latet's command: said how, and what stops it
        info = wrapped.detect()
        if not info.installed:
            continue
        route = wrapped.route()
        out.append(Check(OK, info.name, f"{info.version or ''} - started with `latet {wrapped.id}`".strip(" -")))
        if route.problem:
            out.append(Check(WARN, f"{info.name} route", route.problem))
        out += [Check(OK, f"{info.name} route", note) for note in route.notes]
    return out


# Where Claude Code goes: a value of these in the shell is overridden by one in its settings.
_SHELL_ROUTE = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_REGION",
    "AWS_PROFILE",
)


def route_checks(
    agent: Any, *, _settings: dict[str, Any] | None = None, _prepared: _PreparedConnection | None = None
) -> list[Check]:
    """Where an agent goes, as its settings say: what Latet cannot stand in front of, a shell that now
    says otherwise, and what signing for Bedrock needs."""
    from latet.integrations.agents import ClaudeCode
    from latet.integrations.base import load_records, records_preflight
    from latet.server.connection import _prepare_token, _require_selection

    records_preflight()
    prepared = _prepared if _prepared is not None else _prepare_token(_diagnostic_connection())
    _require_selection(prepared)
    title = f"{agent.name} route"
    if not isinstance(agent, ClaudeCode):
        route = agent.route(load_config())
        if route.problem:
            return [Check(WARN, title, route.problem)]
        return [Check(OK, title, route.summary), *(Check(OK, title, note) for note in route.notes)]
    if _settings is None:
        exposure, _settings = _claude_settings(agent)
        if _settings is None:
            return [exposure]
    route = agent.route(load_config(), _settings=_settings.get("env", {}), _prepared=prepared, _diagnostic=True)
    if route.problem:
        return [Check(WARN, title, route.problem)]
    out = [Check(OK, title, route.summary + (f"; {route.note}" if route.note else ""))]
    settings = _settings.get("env", {}) if _settings is not None else {}
    record = load_records().get(agent.id)
    previous = (record.data.get("previous") if record is not None else None) or {}
    written = (record.data.get("written") if record is not None else None) or {}
    for var in _SHELL_ROUTE:
        shell = os.environ.get(var)
        # Latet wrote this one in front of the shell's own address: the settings file had no value for it
        ours = var in previous and previous[var] is None and written.get(var) == settings.get(var)
        if shell is not None and var in settings and settings[var] != shell and not ours:
            detail = f"your shell sets {var}, but Claude Code's settings set it too, and they win: change it "
            out.append(Check(WARN, title, detail + "there, or run `latet agents apply claude` again"))
    applied = (record.data.get("route") or {}).get("summary") if record is not None else None
    if applied and applied != route.summary:
        detail = f"Claude Code's settings say {applied}; your settings and shell now say {route.summary}"
        out.append(Check(WARN, title, detail + ": `latet agents apply claude`"))
    if "/profile/" in route.env.get("ANTHROPIC_BEDROCK_BASE_URL", ""):
        if find_aws_cli() is None:
            detail = "Latet signs for Bedrock with an AWS profile, and the AWS CLI is not on your PATH nor in "
            detail += f"{cli_folders()}: Latet reads only keys written in ~/.aws, and an SSO login needs the AWS CLI "
            detail += "(https://aws.amazon.com/cli/)"
            out.append(Check(WARN, title, detail))
        own = _settings
        if isinstance(own, dict) and ("awsCredentialExport" in own or "awsAuthRefresh" in own):
            detail = "Claude Code's awsCredentialExport and awsAuthRefresh are not used while Latet signs: it reads "
            out.append(Check(WARN, title, detail + "the AWS profile itself"))
    return out


def helper_checks(prepared: _PreparedConnection, settings: dict[str, Any]) -> list[Check]:
    """Claude Code applied for good on a connected computer: whether its plugin can reach the server, through the
    helper binding `latet agents apply claude` made (R5). Outside Windows none is made in this version; on Windows the
    binding must be there and match this installation and connection (an upgrade or a new connection changes it).
    Nothing to say when its settings hold no token (kept out on purpose: `latet claude` gives one per session)."""
    from latet import claude_bridge

    title = "Claude Code helper"
    if (held := claude_bridge.platform_problem()) is not None:
        return [Check(FAIL, title, held)]
    env = settings.get("env")
    if not isinstance(env, dict) or not env.get("LATET_BRIDGE_TOKEN"):
        return []
    problem = claude_bridge.binding_problem(prepared)
    return [Check(FAIL, title, problem)] if problem else [Check(OK, title, "bound to this installation and server")]


def check_project(cwd: Path | None = None) -> list[Check]:
    """On a connected computer, a latet.yaml in the working folder's project that the server does not apply (R9)."""
    from latet.projects import not_applied_here

    note = not_applied_here(cwd or Path.cwd())
    return [Check(WARN, "Project", note)] if note else []


def user_name_checks(agent: ClaudeCode) -> list[Check]:
    """``auto_mode_review.user_name: surrogate`` reaches Claude Code's settings with `agents apply`."""
    from latet.integrations.base import load_records, records_preflight

    records_preflight()
    wanted = load_config().auto_mode_review.user_name == "surrogate"
    record = load_records().get(agent.id)
    given = bool(record is not None and record.data.get("user_name_surrogate"))
    if wanted and not given:
        detail = "it has your real user name, not its surrogate (auto_mode_review.user_name: surrogate)"
    elif given and not wanted:
        detail = "it still has your user name's surrogate in USERNAME and USER"
    else:
        return []
    return [Check(WARN, agent.name, f"{detail}: `latet agents apply claude`, then restart Claude Code")]


def check_recognizers() -> list[Check]:
    """Your own recognizers: each one loads, and its examples pass (`latet rules test` for details)."""
    from latet.engine.build import recognizers_for

    config = load_config()
    if not config.recognizers:
        return []
    _, statuses = recognizers_for(config)
    out = [Check(FAIL, f"Recognizer {s.name}", f"turned off: {e}") for s in statuses for e in s.errors]
    out += [Check(WARN, f"Recognizer {s.name}", w) for s in statuses for w in s.warnings]
    if not out:
        active = sum(1 for s in statuses if s.state == "active")
        out.append(Check(OK, "Recognizers", f"{active} active, examples pass"))
    elif any(c.status == FAIL for c in out):
        out.append(Check(WARN, "Recognizers", "run `latet rules test` to see why"))
    return out


def check_instructions(cwd: Path | None = None) -> list[Check]:
    """The CLAUDE.md files Claude Code loads here: values in them that would stop its auto mode reviews."""
    from latet.engine.build import build
    from latet.engine.vault import Vault
    from latet.instructions import describe, findings, instruction_files
    from latet.projects import Projects
    from latet.runtime import resolve_identity

    folder = cwd or Path.cwd()
    files = instruction_files(folder)
    if not files:
        return []
    config = load_config()
    vault = Vault(os.urandom(32))
    identity = resolve_identity(config)
    # The rules in effect in this folder's project: its latet.yaml and your own settings for it.
    project = Projects(config, vault, identity).resolve(str(folder))
    if not project.enabled:
        return [Check(OK, "CLAUDE.md", f"Latet is off in {project.name}: nothing is stopped there")]
    masker = build(project.config.model_copy(update={"ner": "off"}), vault, identity).masker
    found = findings(files, masker)
    if not found:
        return [Check(OK, "CLAUDE.md", f"{len(files)} here, nothing in them that stops auto mode's reviews")]
    return [Check(WARN, "CLAUDE.md", line) for line in describe(found)]


def check_reviewer() -> list[Check]:
    """The local reviewer, when it is on: its server answers, has the model, and room for a review."""
    import asyncio

    from latet.engine.reviewer.client import ReviewError
    from latet.engine.reviewer.probe import sees_images
    from latet.engine.reviewer.reviewer import client_for

    config = load_config().llm_review
    if not config.enabled:
        return []
    found: list[Check] = []
    if config.certificate:
        from latet.config import data_dir
        from latet.engine.reviewer.certificate import check

        state = check(data_dir(), config.certificate)
        if state is not None and state[0] == "fail":
            return [Check(FAIL, "Local reviewer", f"{config.url}: {state[1]}")]
        if state is not None:
            found.append(Check(WARN, "Local reviewer", state[1]))

    async def probe() -> tuple[list[str], dict | None]:
        client = client_for(config, timeout=10)
        try:
            return await client.models(), await client.props()
        finally:
            await client.aclose()

    stop = config.on_failure == "stop"
    try:
        models, props = asyncio.run(probe())
    except ReviewError as exc:
        after = "requests are stopped until it answers" if stop else "requests leave masked by the detectors alone"
        return [Check(FAIL if stop else WARN, "Local reviewer", f"{config.url}: {exc}; {after}")]
    model = config.model or (models[0] if models else None)
    if config.model and config.model not in models:
        return [Check(FAIL, "Local reviewer", f"{config.url} has no model {config.model} (it has {', '.join(models)})")]
    if model is None:
        return [Check(FAIL, "Local reviewer", f"{config.url} lists no model")]
    slots = int((props or {}).get("total_slots") or 1)
    n_ctx = ((props or {}).get("default_generation_settings") or {}).get("n_ctx")
    detail = f"{config.url}, model {model}, {slots} slot{'s' if slots != 1 else ''}"
    out = [Check(OK, "Local reviewer", detail), *found]
    if config.in_clear():
        out.append(
            Check(
                WARN,
                "Local reviewer",
                "the original texts cross your network unencrypted (http): give the server a certificate and "
                "use https, `latet llm trust` trusts a self-signed one",
            )
        )
    if isinstance(n_ctx, int) and n_ctx < 4096:
        out.append(Check(WARN, "Local reviewer", f"each slot has {n_ctx} tokens of context; a review needs about 4096"))
    if config.images:
        vision = sees_images(props)
        if vision is False:
            out.append(
                Check(
                    FAIL,
                    "Local reviewer",
                    f"llm_review.images is on, but model {model} does not see images: start llama.cpp with its "
                    "--mmproj file, or turn llm_review.images off",
                )
            )
        elif vision is None:
            out.append(
                Check(
                    WARN,
                    "Local reviewer",
                    "llm_review.images is on; the server does not say whether its model sees images (llama.cpp "
                    "does): try it from the dashboard",
                )
            )
    return out


def check_live_review() -> list[Check]:
    """The Live Reviewer, when it is on: a dashboard is open to approve what waits."""
    from latet.service import daemon

    if not load_config().live_review.enabled:
        return []
    current = daemon.status()
    if not current.running:
        return [Check(WARN, "Live Reviewer", "on; the daemon is not running")]
    waiting = f"{current.waiting} waiting" if current.waiting else "nothing waiting"
    if current.viewers == 0:
        detail = f"on, {waiting}, and no dashboard is open: new texts wait until you run `latet dashboard`"
        return [Check(WARN, "Live Reviewer", detail)]
    return [Check(OK, "Live Reviewer", f"on, {waiting}, {current.viewers} dashboard(s) open")]


def check_ocr() -> list[Check]:
    """The text in images: read and masked when the models are installed, and they read a sample."""
    import time

    from latet.vision import models
    from latet.vision.policy import image_policy

    config = load_config()
    policy = image_policy(config)
    if policy.blocks:
        why = "the max profile" if policy.because == "profile" else "`images: block`"
        return [Check(OK, "Text in images", f"requests that carry images are not sent ({why})")]
    if config.ocr == "off":
        return [Check(OK, "Text in images", "not read (`ocr: off`): images are sent as they are, and flagged")]
    if not models.runtime_available() or not models.installed(data_dir()):
        need = "`latet ocr install`" if models.runtime_available() else "the ocr extra, then `latet ocr install`"
        return [Check(OK, "Text in images", f"not read: images are sent as they are, and flagged ({need} reads them)")]
    from PIL import Image, ImageDraw, ImageFont

    from latet.vision.ocr import OnnxOcr

    ocr = OnnxOcr(*models.folders(data_dir()))
    if not ocr.wait_ready(30):
        return [Check(FAIL, "Text in images", f"the models do not load: {ocr.error or 'timed out'}")]
    sample = Image.new("RGB", (520, 64), "white")
    ImageDraw.Draw(sample).text((12, 12), "Latet 4217 Niccolo", font=ImageFont.load_default(32), fill="black")
    started = time.monotonic()
    text = ocr.read(sample).text
    took = (time.monotonic() - started) * 1000
    if "4217" not in text or "Latet" not in text:
        return [Check(FAIL, "Text in images", f"the models read the sample wrong ({text!r})")]
    return [Check(OK, "Text in images", f"read and masked; a sample read in {took:.0f} ms")]


def check_ner() -> list[Check]:
    """Local name detection: where names and addresses are looked for, or what it needs."""
    from latet.engine.detectors import ner

    config = load_config()
    if config.profile.value == "secrets":
        return [Check(OK, "Names", "not looked for (the secrets profile)")]
    if config.ner == "off":
        return [Check(OK, "Names", "not looked for (`ner: off`)")]
    if not ner.runtime_available():
        return [Check(OK, "Names", "not looked for: they need the ner extra, then `latet ner install`")]
    if not ner.is_installed(data_dir()):
        return [Check(OK, "Names", "not looked for: `latet ner install` downloads the model (about 1.6 GB, once)")]
    where = "everywhere" if config.profile.value == "max" else "in your prompts and attachments"
    return [Check(OK, "Names", f"looked for {where}")]


@dataclass(frozen=True)
class _ServerMachine:
    config: Path | None = None
    problem: str | None = None


def _server_machine_selected() -> _ServerMachine | None:
    from latet.server import service
    from latet.server.settings import _VARIABLES

    if explicit := os.environ.get("LATET_SERVER_CONFIG"):
        return _ServerMachine(Path(explicit))
    # URL also selects a remote administration target on an ordinary personal computer.
    environment = any(os.environ.get(variable) for key, variable in _VARIABLES.items() if key != "url")
    fallback = _ServerMachine() if environment else None
    try:
        kind = service.platform()
        if kind == "windows":
            base = service.windows_folder()
            try:
                found = base.lstat()
            except FileNotFoundError:
                return fallback
            if not stat.S_ISDIR(found.st_mode) or getattr(found, "st_file_attributes", 0) & 0x400:
                return _ServerMachine(problem="the server folder is not a plain directory")
            config = base / "server.yaml"
        elif kind == "linux":
            config = service.LINUX_CONFIG
        else:
            return fallback
        try:
            found = config.lstat()
        except FileNotFoundError:
            # An existing Windows service root remains a machine even when its config is missing.
            return _ServerMachine(config) if kind == "windows" else fallback
        if not stat.S_ISREG(found.st_mode) or getattr(found, "st_file_attributes", 0) & 0x400:
            return _ServerMachine(problem="the server configuration is not a plain file")
        return _ServerMachine(config)
    except Exception as exc:
        return _ServerMachine(problem=f"default server metadata unavailable ({type(exc).__name__})")


def check_server_folder() -> list[Check]:
    """Inspect the Windows service directories without repairing them or reading their files."""
    from latet.server import service

    if service.platform() != "windows":
        return []
    base = service.windows_folder()
    out: list[Check] = []
    for path, writable, title in (
        (base, False, "Server folder"),
        (base / "data", True, "Server data folder"),
        (base / "logs", True, "Server logs folder"),
    ):
        try:
            problem = service.folder_problem(path, writable=writable)
        except Exception as exc:
            problem = f"metadata unavailable ({type(exc).__name__})"
        out.append(Check(FAIL if problem else OK, title, problem or "service access is private"))
    return out


def check_server_machine(*, _selection: _ServerMachine | None = None) -> list[Check]:
    """Selected machine settings and secret metadata only; never instantiate validated server settings."""
    from pydantic import TypeAdapter, ValidationError

    from latet.server import service

    out = check_server_folder()
    if "SSLKEYLOGFILE" in os.environ:
        out.append(Check(WARN, "Server SSLKEYLOGFILE", "set: TLS session keys may be written to a file"))
    if _selection is not None and _selection.problem is not None:
        out.append(Check(FAIL, "Server settings", _selection.problem))
        return out
    try:
        values = service.configuration_values(_selection.config if _selection is not None else None)
    except Exception as exc:
        out.append(Check(FAIL, "Server settings", f"settings cannot be safely inspected ({type(exc).__name__})"))
        return out
    out.append(Check(OK, "Server settings", "selected server-machine settings inspected"))
    boolean = TypeAdapter(bool)
    for key, detail in (
        ("database_private_network", "on: PostgreSQL relies on private network isolation instead of checked TLS"),
        ("valkey_private_network", "on: Valkey relies on private network isolation instead of checked TLS"),
        ("valkey_unauthenticated", "on: Valkey authentication is not required"),
    ):
        try:
            enabled = boolean.validate_python(values.get(key, False))
        except ValidationError:
            out.append(Check(FAIL, f"Server {key}", "setting is not a boolean"))
        else:
            if enabled:
                out.append(Check(WARN, f"Server {key}", detail))
    for key in (
        "master_key_file",
        "database_password_file",
        "valkey_password_file",
        "metrics_token_file",
        "reviewer_key_file",
        "key",
    ):
        value = values.get(key)
        if value is None:
            continue
        problem = "setting does not name a file"
        if isinstance(value, str) and value:
            try:
                problem = service.secret_file_problem(Path(value))
            except Exception as exc:
                problem = f"metadata unavailable ({type(exc).__name__})"
        out.append(Check(FAIL if problem else OK, f"Server {key}", problem or "secret file access is private"))
    return out


def run_all() -> list[Check]:
    from latet.integrations.base import RecordsUnavailable, records_preflight
    from latet.server.connection import NotConnected, _require_selection

    try:
        prepared = _diagnostic_connection()
    except NotConnected as exc:
        return [Check(FAIL, "Connection", server_refusal(exc))]
    try:
        records_preflight()
    except RecordsUnavailable as exc:  # Latet's own data folder, connected or not: never said as a connection's
        return [Check(FAIL, "Data folder", str(exc))]
    try:
        checks = check_folders()
    except Exception as exc:  # a broken check must not hide the others
        checks = [Check(FAIL, "Folders", type(exc).__name__)]
    machine = None
    if prepared.connection is not None:
        checks += check_server(_prepared=prepared)
        if any(check.status == FAIL for check in checks):
            return checks
        groups = (check_agents, check_project, check_instructions)
    elif (machine := _server_machine_selected()) is not None:
        groups = (check_server_machine,)
    else:
        groups = (
            check_daemon,
            check_port,
            check_engine,
            check_recognizers,
            check_ner,
            check_ocr,
            check_reviewer,
            check_live_review,
            check_agents,
            check_instructions,
        )
    for group in groups:
        try:
            _require_selection(prepared)
            checks += check_server_machine(_selection=machine) if group is check_server_machine else group()
        except Exception as exc:  # a broken check must not hide the others
            checks.append(Check(FAIL, group.__name__.removeprefix("check_").title(), type(exc).__name__))
    return checks
