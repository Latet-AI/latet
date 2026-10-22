"""Running agents through Latet without touching their global configuration (``latet claude``...)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from latet.config import RESERVED_DESTINATIONS, Config, data_dir, load_config, write_atomic
from latet.storage.keys import load_bridge_token

if TYPE_CHECKING:
    import ssl

    from latet.gateway.sigv4 import AwsCredentials
    from latet.server.connection import Connection, _PreparedConnection

_SETTINGS_KEPT = 24 * 3600  # settings files left behind (a crash) are swept after a day
_ANSWER_MOST = 1024 * 1024  # bytes of the daemon's answer read at most

# What each agent reads to trust one more certificate: the server's this computer pinned (`latet connect
# --fingerprint`), as notes/spike-server-certificates.md found; each adds to the authorities the agent trusts already.
# SSL_CERT_FILE is never set: Codex's takes their place, and OpenCode passes it whole to the programs it starts.
EXTRA_CA_VARS: dict[str, tuple[str, ...]] = {
    "claude": ("NODE_EXTRA_CA_CERTS",),
    "codex": ("CODEX_CA_CERTIFICATE",),
    "opencode": ("NODE_EXTRA_CA_CERTS",),
    "gemini": ("NODE_EXTRA_CA_CERTS",),
}
# Whether Claude Code takes NODE_EXTRA_CA_CERTS from its settings.json, as `latet agents apply claude` writes it.
CLAUDE_SETTINGS_CA = True
_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "gemini": "Gemini CLI"}
# Why an agent applied for good will not reach a server whose self-signed certificate this computer pinned: it reads
# no extra certificate from its own files. The way that changes nothing on this computer first (`latet <agent>`), then
# the company's authority; the computer's store only for a certificate constrained to the server's host (STORE).
NO_EXTRA_CA = (
    "{agent} started on its own trusts only this computer's certificate authorities, and the server's certificate is "
    "self-signed: start it with `latet {command}`, which gives it that certificate for its session; or give the "
    "server a certificate of your company's authority{store} (Latet on a server, in the documentation)"
)
STORE = (
    "; or add the server's certificate to this computer's store of authorities, which every program of this computer "
    "will then trust"
)
# A pin that is not constrained to the server's host is given to no agent (T3.8): each would trust whatever its key
# signs, for any address.
UNCONSTRAINED = (
    "{agent} is not given the server's certificate: it is self-signed without a name constraint for {host}, and "
    "{agent} would trust whatever its key signs, for any address. It needs a certificate made by `latet server "
    "certificate --make` on the server, or one of your company's authority (Latet on a server, in the documentation)"
)
MISSING = (
    "{agent} is not given the server's certificate: the one this computer pinned for {url} is missing or changed "
    "(`latet disconnect`, then connect again with its fingerprint)"
)
MANAGED_CA = (
    "Claude Code's managed settings set NODE_EXTRA_CA_CERTS, which wins over Latet's: Claude Code is not given the "
    "server's certificate, and refuses the server; ask your IT to add that certificate there, or give the server a "
    "certificate of your company's authority"
)
# What the daemon, or the identity's space, answers is taken only in Latet's shapes.
_SESSION = re.compile(r"[0-9a-f]{32}")  # an AWS session's id (secrets.token_hex(16))
_DESTINATION = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")  # a destination's name (config.Destination.name)
_NOT_IN_A_USER_NAME = frozenset('/\\:*?"<>|')
_CONTROLS = frozenset(
    (*range(0x20), *range(0x7F, 0xA0), 0x061C, 0x200E, 0x200F, 0x2028, 0x2029, *range(0x202A, 0x202F),
     *range(0x2066, 0x206A))
)  # fmt: skip
_CODEX_PROFILE = re.compile(r"latet-[0-9a-f]{16}\.config\.toml")


def connected() -> Connection | None:
    """This computer's connection to a Latet server, or None. Its file is read only when there is one, by a module that
    imports nothing heavy: an agent's start on a computer that is not connected pays nothing for it. Raises NotConnected
    for a connection that cannot be used (kept in a folder others may change, say): never taken for none."""
    from latet.server.connection import load_connection

    return load_connection()


def base_url(config: Config | None = None) -> str:
    """The address agents are given: Latet on this computer, or the identity's space on the Latet server this computer
    is connected to."""
    from latet.server.connection import agent_base

    return agent_base(config)


def server_base() -> str | None:
    """The identity's space on the server when this computer is connected to one; None otherwise."""
    return base_url() if connected() is not None else None


def daemon_token() -> str:
    """What the plugin and Latet's own calls present: this computer's personal token when it is connected to a server,
    else the local bridge token."""
    from latet.server.connection import _prepare_connection, _prepare_token

    return _daemon_token(_prepare_token(_prepare_connection()))


def _daemon_token(prepared: _PreparedConnection) -> str:
    from latet.server.connection import NotConnected, token_missing

    connection, token = prepared.connection, prepared.token
    if connection is None:
        return load_bridge_token(prepared.selected.data)
    if token is None:
        raise NotConnected(token_missing(connection))
    return token


def tls() -> ssl.SSLContext | None:
    """The TLS context of Latet's own calls to its server: the certificate this computer pinned, or the system's
    authorities; None for the daemon on this computer, over http."""
    from latet.server.connection import _prepare_connection

    prepared = _prepare_connection()
    connection = prepared.connection
    if connection is None:
        return None
    from latet.server.client import _tls_context

    return _tls_context(connection.fingerprint, _data=prepared.selected.data)


def extra_ca_env(agent: str, said: list[str] | None = None) -> dict[str, str]:
    """The variables that give ``agent`` the server's certificate this computer pinned: a file Latet writes now, from
    the pin it checked, in the connection's own folder, with the person's own certificates first (their variable's
    value in this shell, and for Claude Code in its settings), so that none of theirs is dropped. None when nothing is
    pinned, the agent reads no certificate, or the pin is not one to give an agent (``untrusting`` says why). What was
    said of the person's files goes into ``said``."""
    if not EXTRA_CA_VARS.get(agent):
        return {}
    from latet.server.connection import _prepare_connection

    return _extra_ca_env(_prepare_connection(), agent, said)


def _extra_ca_env(prepared: _PreparedConnection, agent: str, said: list[str] | None = None) -> dict[str, str]:
    from latet.server.connection import _agent_certificate, _require_selection

    variables = EXTRA_CA_VARS.get(agent, ())
    connection = prepared.connection if variables else None
    _require_selection(prepared)  # Older prior-settings/record readers must not select another state.
    if connection is None or connection.fingerprint is None:
        return {}

    def certificates():
        _require_selection(prepared)
        yield from _their_certificates(agent)
        _require_selection(prepared)

    path, notes = _agent_certificate(prepared.selected.data, connection, agent, certificates())
    if said is not None:
        said.extend(notes)
    return {var: str(path) for var in variables} if path is not None else {}


def _their_certificates(agent: str) -> list[str | None]:
    """The person's own values of the variable that gives ``agent`` more certificates: this shell's; for Claude Code
    also its user settings' (which Latet's --settings overrides in a session), and in place of a value of Latet's own
    there, the person's that it replaced (the record `agents apply` keeps)."""
    values = [os.environ.get(var) for var in EXTRA_CA_VARS.get(agent, ())]
    if agent == "claude":
        values.append(_claude_settings_certificates())
    return values


def _claude_settings_certificates() -> str | None:
    from latet.integrations.agents import ClaudeCode
    from latet.integrations.base import load_records
    from latet.server.connection import is_latets_own

    try:
        data = json.loads(ClaudeCode().settings_path().read_text("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    value = (data.get("env") or {}).get("NODE_EXTRA_CA_CERTS") if isinstance(data, dict) else None
    if not isinstance(value, str) or not is_latets_own(value):
        return value if isinstance(value, str) else None
    try:
        record = load_records().get("claude")
        previous = (record.data.get("previous") or {}).get("NODE_EXTRA_CA_CERTS") if record is not None else None
    except (OSError, ValueError, TypeError, AttributeError):  # a record that cannot be read keeps nothing of yours
        return None
    return previous if isinstance(previous, str) else None


def untrusting(agent: str, *, for_good: bool = False, _prepared: _PreparedConnection | None = None) -> str | None:
    """Why ``agent`` will not reach a server whose self-signed certificate this computer pinned: the pin is not one to
    give an agent (not constrained to the server's host, or gone), or the agent reads no extra certificate
    (``for_good``: from what `latet agents apply` writes), or Claude Code's managed settings name certificates of their
    own. None when it takes it, or nothing is pinned."""
    from latet.server.connection import _pin_for_agents, _require_selection, pin_for_agents

    if _prepared is not None:
        _require_selection(_prepared)
    connection = _prepared.connection if _prepared is not None else connected()
    if connection is None or connection.fingerprint is None:
        return None
    from urllib.parse import urlsplit

    name = _NAMES.get(agent, agent)
    pem, why = (
        _pin_for_agents(_prepared.selected.data, connection) if _prepared is not None else pin_for_agents(connection)
    )
    if why == "unconstrained":
        return UNCONSTRAINED.format(agent=name, host=urlsplit(connection.url).hostname)
    if pem is None:
        return MISSING.format(agent=name, url=connection.url)
    if agent == "claude":
        from latet.integrations.routes import managed_setting

        if managed_setting("NODE_EXTRA_CA_CERTS"):  # above --settings and your settings: Latet's is never read
            return MANAGED_CA
    takes = (bool(EXTRA_CA_VARS.get(agent)) and not for_good) or (agent == "claude" and CLAUDE_SETTINGS_CA)
    return None if takes else NO_EXTRA_CA.format(agent=name, command=agent, store=STORE)


def plugin_dir() -> Path:
    """The Claude Code plugin shipped inside this package."""
    return Path(str(resources.files("latet").joinpath("claude_plugin")))


def claude_env(
    config: Config | None = None,
    disable_classifier_context: bool = True,
    user_name: str | None = None,
    said: list[str] | None = None,
    *,
    _prepared: _PreparedConnection | None = None,
) -> dict[str, str]:
    from latet.server.connection import _agent_base, _prepare_connection, _prepare_token

    prepared = _prepared if _prepared is not None else _prepare_token(_prepare_connection())
    url = _agent_base(prepared, config)
    env = {
        "ANTHROPIC_BASE_URL": f"{url}/anthropic",
        "LATET_BRIDGE_URL": url,
        "LATET_BRIDGE_TOKEN": _daemon_token(prepared),
        # A custom base URL turns MCP tool search off; keep Claude Code's default behaviour.
        "ENABLE_TOOL_SEARCH": "true",
    }
    if disable_classifier_context:
        # Without this Claude Code attaches a `safeguards` field (home directory, working
        # directory, OS user name, git state) that no hook can mask.
        env["CLAUDE_CODE_AUTO_MODE_SERVER"] = "0"
    if user_name:
        # auto_mode_review.user_name: surrogate. Claude Code tells its auto mode reviewer the user
        # name it finds here, so the reviewer sees the surrogate the model sees everywhere else.
        env["USERNAME"] = env["USER"] = user_name
    env.update(_extra_ca_env(prepared, "claude", said))
    return env


def user_name_surrogate(config: Config | None = None, *, _prepared: _PreparedConnection | None = None) -> str | None:
    """The OS user name's surrogate: from the daemon's vault (started if needed), or from the identity's space on the
    server, which answers None when its settings want none. None: no answer. Raises DaemonError for an answer that is
    not a user name's fake of Latet's shape (it would go into Claude Code's USER and USERNAME)."""
    import urllib.error
    import urllib.request

    from latet.service import daemon

    config = config or load_config()
    connection = _prepared.connection if _prepared is not None else connected()
    if connection is not None:
        from latet.config import os_user_name

        try:
            answer = _space_call(
                connection, "/_latet/admin/user-name", {"name": os_user_name() or ""}, _prepared=_prepared
            )
        except DaemonError:
            return None  # it answered no (no user name, a vault that takes no new value), or did not answer
        return _surrogate_of(answer)
    for attempt in range(2):
        if _prepared is not None:
            from latet.server.connection import _agent_base, _require_selection

            _require_selection(_prepared)
            address = _agent_base(_prepared, config)
            token = load_bridge_token(_prepared.selected.data)
        else:
            address, token = base_url(config), load_bridge_token(data_dir())
        request = urllib.request.Request(  # noqa: S310 - 127.0.0.1 only
            f"{address}/_latet/admin/user-name",
            data=b"{}",
            method="POST",
            headers={"x-latet-token": token, "content-type": "application/json"},
        )
        try:
            with daemon.local_urlopen(request, timeout=10) as response:
                answer = _object(response.read(_ANSWER_MOST + 1))
        except urllib.error.HTTPError:
            return None  # the daemon answered: no OS user name, or an older daemon
        except (OSError, ValueError):
            if attempt == 0 and not daemon.start().running:
                return None
            continue
        return _surrogate_of(answer)
    return None


def _object(raw: bytes) -> dict[str, Any]:
    """An answer of the daemon's: a JSON object of ``_ANSWER_MOST`` bytes at most, however deep. Raises ValueError."""
    if len(raw) > _ANSWER_MOST:
        raise ValueError("the answer is too long")
    try:
        found = json.loads(raw or b"{}")
    except RecursionError:
        raise ValueError("the answer is nested too deep") from None
    if not isinstance(found, dict):
        raise ValueError("the answer is not a JSON object")
    return found


def _surrogate_of(answer: dict[str, Any]) -> str | None:
    """The user name's fake in an answer, when it is one of Latet's shape: 1 to 256 characters, no control character
    and none of / \\ : * ? " < > |. None when the answer gives none."""
    surrogate = answer.get("surrogate")
    if surrogate is None:
        return None
    if (
        isinstance(surrogate, str)
        and 0 < len(surrogate) <= 256
        and not any(ord(char) in _CONTROLS or char in _NOT_IN_A_USER_NAME for char in surrogate)
    ):
        return surrogate
    raise DaemonError(
        "the answer is not a user name's fake Latet gives Claude Code: 1 to 256 characters, with no control character "
        'and none of / \\ : * ? " < > |'
    )


def codex_version(exe: str = "codex") -> tuple[int, int, int] | None:
    path = shutil.which(exe)
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15, check=False).stdout  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def codex_uses_chatgpt(exe: str = "codex") -> bool:
    path = shutil.which(exe)
    if not path:
        return True
    try:
        out = subprocess.run([path, "login", "status"], capture_output=True, text=True, timeout=15, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return True
    return "api key" not in (out.stdout + out.stderr).lower()


def codex_base_url(config: Config | None = None, chatgpt: bool | None = None) -> str:
    url = base_url(config)
    if chatgpt is None:
        chatgpt = codex_uses_chatgpt()
    return f"{url}/backend-api/codex" if chatgpt else f"{url}/openai/v1"


def opencode_overrides(config: Config | None = None) -> dict:
    url = base_url(config)
    return {
        "provider": {
            "anthropic": {"options": {"baseURL": f"{url}/anthropic/v1"}},
            "openai": {"options": {"baseURL": f"{url}/openai/v1"}},
            "openrouter": {"options": {"baseURL": f"{url}/openrouter/api/v1"}},
        }
    }


def generic_env(config: Config | None = None, base: str | None = None) -> dict[str, str]:
    """The variables a script or an SDK reads: Latet's address, or ``base`` (an API key's space on a server)."""
    url = base or base_url(config)
    return {
        "OPENAI_BASE_URL": f"{url}/openai/v1",
        "OPENAI_API_BASE": f"{url}/openai/v1",
        "ANTHROPIC_BASE_URL": f"{url}/anthropic",
        "GOOGLE_GEMINI_BASE_URL": f"{url}/gemini",
        "GOOGLE_VERTEX_BASE_URL": f"{url}/vertex",
    }


# Gemini CLI's ways of reaching its model that go to the base URL Latet gives it (the Gemini API with a key, Vertex, a
# gateway): any other, its Google sign-in, goes to an endpoint Latet does not read.
GEMINI_THROUGH_LATET = ("gemini-api-key", "vertex-ai", "gateway")
_GEMINI_SETTINGS_BYTES = 1024 * 1024  # a settings file larger than this is no choice Latet reads


def gemini_settings_path() -> Path:
    """Gemini CLI's own settings: ``.gemini/settings.json`` in its home (``GEMINI_CLI_HOME``, else yours)."""
    return Path(os.environ.get("GEMINI_CLI_HOME") or Path.home()) / ".gemini" / "settings.json"


def gemini_auth(path: Path | None = None) -> str | None:
    """How Gemini CLI's settings say it reaches its model (``security.auth.selectedType``, or the older
    ``selectedAuthType``), which holds no secret; None when nothing is chosen, or the file cannot be read."""
    from latet.integrations import jsonc

    try:
        with (path or gemini_settings_path()).open("rb") as file:
            raw = file.read(_GEMINI_SETTINGS_BYTES + 1)
        data = jsonc.loads(raw.decode("utf-8-sig")) if len(raw) <= _GEMINI_SETTINGS_BYTES else None
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    security = data.get("security")
    auth = security.get("auth") if isinstance(security, dict) else None
    chosen = auth.get("selectedType") if isinstance(auth, dict) else None
    chosen = data.get("selectedAuthType") if chosen is None else chosen
    return chosen if isinstance(chosen, str) else None


def _gemini_system_path() -> Path:
    """The machine's settings, which Gemini CLI reads above every other (``GEMINI_CLI_SYSTEM_SETTINGS_PATH``)."""
    given = os.environ.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")
    if given:
        return Path(given)
    if sys.platform == "darwin":
        return Path("/Library/Application Support/GeminiCli/settings.json")
    if sys.platform == "win32":
        return Path(r"C:\ProgramData\gemini-cli\settings.json")
    return Path("/etc/gemini-cli/settings.json")


def gemini_choice(cwd: Path | None = None) -> tuple[str | None, str | None]:
    """How Gemini CLI will reach its model, as it merges its settings, and where that is said: the machine's settings,
    then the project's (``.gemini/settings.json`` in the folder it starts in, read whether or not that folder is
    trusted: a choice there that Latet cannot go in front of stops it all the same), then yours
    (``GEMINI_CLI_AUTH_OVERRIDE`` in their place when it is set), then the machine's defaults. (None, None) when none of
    them chooses."""
    system = _gemini_system_path()
    defaults = Path(os.environ.get("GEMINI_CLI_SYSTEM_DEFAULTS_PATH") or system.parent / "system-defaults.json")
    user = gemini_settings_path()
    workspace = (cwd or Path.cwd()) / ".gemini" / "settings.json"
    override = os.environ.get("GEMINI_CLI_AUTH_OVERRIDE") or None
    layers: list[tuple[str | None, str]] = [(gemini_auth(system), str(system))]
    if workspace.resolve() != user.resolve():
        layers.append((gemini_auth(workspace), str(workspace)))
    layers.append((override, "GEMINI_CLI_AUTH_OVERRIDE") if override else (gemini_auth(user), str(user)))
    layers.append((gemini_auth(defaults), str(defaults)))
    return next(((chosen, where) for chosen, where in layers if chosen is not None), (None, None))


def gemini_env(config: Config | None = None, said: list[str] | None = None) -> dict[str, str]:
    """What Gemini CLI starts with through Latet: the base URLs, and the server's certificate this computer pinned."""
    return {**generic_env(config), **extra_ca_env("gemini", said)}


def run(argv: list[str], extra_env: dict[str, str]) -> int:
    exe = shutil.which(argv[0])
    if exe is None:
        raise FileNotFoundError(argv[0])
    env = {**os.environ, **extra_env}  # Latet's variables win over your shell's
    env.pop("LATET_SERVER_ADMIN_TOKEN", None)
    return subprocess.call([exe, *argv[1:]], env=env)  # noqa: S603 - the user's own command


AGENTS = ("claude", "codex", "opencode", "gemini")


def agent_of(command: str) -> str | None:
    """The agent a command starts, when Latet has a wrapper for it (``claude``, ``codex.exe``)."""
    name = Path(command).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        name = name.removesuffix(suffix)
    return name if name in AGENTS else None


def claude_argv(args: list[str], exe: str = "claude", settings: Path | None = None) -> list[str]:
    flag = ["--settings", str(settings)] if settings is not None else []
    return [exe, *flag, "--plugin-dir", str(plugin_dir()), *args]


def claude_settings_file(
    env: dict[str, str], args: list[str], *, _prepared: _PreparedConnection | None = None
) -> tuple[Path, list[str]]:
    """A private settings file with Latet's variables for one Claude Code session: `--settings` beats
    your settings and the project's (only your organization's managed ones come first). A `--settings` of
    your own is merged in, and taken out of the arguments."""
    from latet.server.connection import _prepare_output, _require_selection

    if _prepared is not None:
        _require_selection(_prepared)
    folder = (_prepared.selected.data if _prepared is not None else data_dir()) / "claude-settings"
    on_server = _prepared.connection is not None if _prepared is not None else connected() is not None
    if on_server:
        _prepare_output(folder, "Claude Code's session settings are not written")
    yours: dict[str, Any] = {}
    rest: list[str] = []
    items = iter(args)
    for arg in items:
        given = (
            arg.split("=", 1)[1] if arg.startswith("--settings=") else next(items, "") if arg == "--settings" else None
        )
        if given is None:
            rest.append(arg)
            continue
        text = given if given.lstrip().startswith("{") else Path(given).read_text("utf-8")
        loaded = json.loads(text)
        yours.update(loaded if isinstance(loaded, dict) else {})
    mine = {**yours, "env": {**(yours.get("env") or {}), **env}}
    if _prepared is not None:
        _require_selection(_prepared)
    if on_server:
        _prepare_output(folder, "Claude Code's session settings are not written", create=True)
    else:
        from latet.storage.folders import make_private_folder

        make_private_folder(folder)
    for old in folder.glob("*.json"):
        if time.time() - old.stat().st_mtime > _SETTINGS_KEPT:
            old.unlink(missing_ok=True)
    path = folder / f"{secrets.token_hex(8)}.json"
    write_atomic(path, json.dumps(mine, indent=1), private=True)
    return path, rest


class CodexTooOld(Exception):
    """A Codex older than 0.115 cannot take the route (its own providers', or no address to give it): why, and
    what to do."""


def codex_session_profile(overrides: dict[str, str], profile: str | None, home: Path) -> Path:
    """A profile file of Latet's for one Codex session on a computer connected to a Latet server,
    ``<CODEX_HOME>/latet-<16 hex>.config.toml``, private: the profile the person named (or none) with Latet's addresses
    laid over it. The identity's address holds its route key, a secret like the token, and a command line is read by
    every user of the computer (`/proc/<pid>/cmdline`, `ps`); Codex reads a profile file of any name and lays it over
    config.toml key by key (notes/spike-server-certificates.md). Removed when Codex ends; one a crash left behind is
    swept after a day."""
    from latet.integrations import merge

    if home.is_symlink() or home.is_junction():
        raise CodexArgs("Codex's home is a link: use a folder of your own for CODEX_HOME")
    home.mkdir(parents=True, exist_ok=True)
    for old in home.glob("latet-*.config.toml"):
        with contextlib.suppress(OSError):
            if _CODEX_PROFILE.fullmatch(old.name) and time.time() - old.stat().st_mtime > _SETTINGS_KEPT:
                old.unlink(missing_ok=True)
    named = home / f"{profile}.config.toml" if profile else None
    base = named.read_text("utf-8") if named is not None and named.parent == home and named.is_file() else ""
    path = home / f"latet-{secrets.token_hex(8)}.config.toml"
    write_atomic(path, merge.codex_merge(base, overrides), private=True)
    return path


class CodexArgs(Exception):
    """Codex's arguments name a profile, or a subcommand, in a form Latet cannot give its addresses to: why."""


# Codex 0.162's subcommands, and those that take a profile of their own: the interactive session takes one in front,
# `exec`, `resume` and `fork` right after themselves (`exec`'s serves `exec resume`, `exec fork` and `exec review`).
CODEX_SUBCOMMANDS = frozenset(
    {"agents", "exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server", "remote-control", "app",
     "completion", "update", "doctor", "sandbox", "debug", "apply", "a", "resume", "queue", "archive", "delete",
     "migrate-rollouts", "unarchive", "fork", "cloud", "exec-server", "features", "help", "cloud-tasks", "execpolicy"}
)  # fmt: skip
CODEX_PROFILED = frozenset({"exec", "e", "resume", "fork"})


def check_codex_session(args: list[str]) -> None:
    """Refuse unsupported commands before contacting the server or writing a profile."""
    _, rest, _ = codex_profile(args)
    sub = _codex_subcommand(rest)
    if sub is not None and rest[sub] not in CODEX_PROFILED:
        raise CodexArgs(
            f"Latet does not route `codex {rest[sub]}` through its session profile: `latet codex exec ...` runs "
            "through the server. Running Codex on its own uses its own settings and may go directly to its providers"
        )


# Codex's options whose value may follow them apart (`-m gpt-5.5`): that value is never an option or a subcommand.
_CODEX_VALUED = frozenset(
    {"-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env", "-i", "--image", "-m", "--model",
     "--local-provider", "-s", "--sandbox", "-C", "--cd", "--add-dir", "-a", "--ask-for-approval", "--color",
     "--thread-source", "--cyber-access-program", "--output-schema", "-o", "--output-last-message", "--base",
     "--commit", "--title"}
)  # fmt: skip
_CODEX_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def codex_profile(args: list[str]) -> tuple[str | None, list[str], int | None]:
    """The profile Codex's arguments name, in each form Codex takes it (`--profile work`, `--profile=work`, `-p work`,
    `-pwork`, `-p=work`), the arguments without it, and where it was among them. Raises CodexArgs for a form Latet
    cannot read: a profile named twice, an option with no name after it, a name that is not a plain one."""
    found: list[tuple[int, str]] = []
    rest: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":  # what follows is the prompt's, never an option
            rest += args[index:]
            break
        if arg in ("--profile", "-p"):
            if index + 1 >= len(args):
                raise CodexArgs(f"{arg} names no profile")
            found.append((len(rest), args[index + 1]))
            index += 2
            continue
        if arg.startswith("--profile=") or (arg.startswith("-p") and not arg.startswith("--")):
            found.append(
                (len(rest), arg.removeprefix("--profile=") if arg.startswith("--") else arg[2:].removeprefix("="))
            )
            index += 1
            continue
        step = 2 if arg in _CODEX_VALUED else 1  # a value written apart is the option's, whatever it looks like
        rest += args[index : index + step]
        index += step
    if len(found) > 1:
        raise CodexArgs("Codex's arguments name more than one profile: name one")
    if not found:
        return None, rest, None
    where, name = found[0]
    if not _CODEX_PROFILE_NAME.fullmatch(name):
        raise CodexArgs("a profile's name is letters, digits, '.', '-' and '_' (the file <name>.config.toml)")
    return name, rest, where


def _codex_subcommand(args: list[str]) -> int | None:
    """Where Codex's subcommand is among ``args`` (the first argument that is no option nor an option's value, when it
    names a subcommand); None for the interactive session, its prompt included."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return None
        if arg.startswith("-"):
            index += 2 if arg in _CODEX_VALUED else 1
            continue
        return index if arg in CODEX_SUBCOMMANDS else None
    return None


def codex_argv(
    args: list[str],
    overrides: dict[str, str],
    exe: str = "codex",
    openai: str | None = None,
    profile_file: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Codex with Latet's addresses (`-c key="value"`, or ``profile_file`` in place of the profile you named: a
    connected computer's, `codex_session_profile`), and the variables to start it with. A Codex older than
    0.115 reads only OPENAI_BASE_URL: it gets its OpenAI provider's address there, the override's or ``openai``
    (where that provider goes when nothing moves it), over one your shell sets. With a provider of its own to
    route, or no address to give it, it does not start, since it would go round Latet. Latet's profile goes where
    Codex reads it: right after a subcommand that takes one, else where yours was, else in front (the interactive
    session); a subcommand that takes none, or a profile Latet cannot read, raises CodexArgs."""
    version = codex_version(exe)
    if version is not None and version < (0, 115, 0):
        found = ".".join(map(str, version))
        providers = [key for key in overrides if key != "openai_base_url"]
        if providers:
            names = ", ".join(key.removeprefix("model_providers.").removesuffix(".base_url") for key in providers)
            raise CodexTooOld(
                f"Codex {found} is older than 0.115 and cannot take the route for its own providers ({names}): "
                "update it (npm i -g @openai/codex)"
            )
        if not (address := overrides.get("openai_base_url") or openai):
            raise CodexTooOld(
                f"Codex {found} is older than 0.115 and reads only OPENAI_BASE_URL, and Latet has no address to "
                "give it: update it (npm i -g @openai/codex)"
            )
        return [exe, *args], {"OPENAI_BASE_URL": address}
    if profile_file is not None:
        check_codex_session(args)
        _, rest, where = codex_profile(args)
        latet = ["--profile", profile_file.name.removesuffix(".config.toml")]
        if (sub := _codex_subcommand(rest)) is not None:
            return [exe, *rest[: sub + 1], *latet, *rest[sub + 1 :]], {}
        place = 0 if where is None else where
        return [exe, *rest[:place], *latet, *rest[place:]], {}
    codex_profile(args)  # a profile Latet cannot read is never left for Codex to read otherwise
    settings = [item for key, value in overrides.items() for item in ("-c", f'{key}="{value}"')]
    return [exe, *settings, *args], {}


class DaemonError(Exception):
    """The daemon refused or did not answer: why, in its words. ``status`` is the HTTP status it refused
    with; None when nothing answered (connection refused, timeout)."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.status = status


def _daemon_call(
    path: str,
    body: dict[str, Any] | None,
    config: Config | None = None,
    method: str = "POST",
    *,
    _prepared: _PreparedConnection | None = None,
) -> Any:
    """One of Latet's own calls: to this computer's daemon (no proxy, no redirect followed), or, on a computer
    connected to a Latet server, to the identity's space there (``_space_call``). Raises DaemonError."""
    if _prepared is not None:
        from latet.server.connection import _require_selection

        _require_selection(_prepared)
    connection = _prepared.connection if _prepared is not None else connected()
    if connection is not None:
        if _prepared is None:
            return _space_call(connection, path, body, method)
        return _space_call(connection, path, body, method, _prepared=_prepared)
    import urllib.error
    import urllib.request

    from latet.service.daemon import local_urlopen

    if _prepared is not None:
        from latet.server.connection import _agent_base

        address, token = _agent_base(_prepared, config), _daemon_token(_prepared)
    else:
        address, token = base_url(config), load_bridge_token(data_dir())
    request = urllib.request.Request(  # noqa: S310 - 127.0.0.1 only
        f"{address}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"x-latet-token": token, "content-type": "application/json"},
    )
    try:
        with local_urlopen(request, timeout=30) as response:
            return _object(response.read(_ANSWER_MOST + 1))
    except urllib.error.HTTPError as exc:
        try:
            reason = _object(exc.read(_ANSWER_MOST + 1)).get("error") or exc.reason
        except (OSError, ValueError):
            reason = exc.reason
        raise DaemonError(str(reason), exc.code) from None
    except (OSError, ValueError) as exc:
        raise DaemonError(f"the daemon did not answer ({type(exc).__name__})") from None


def _space_call(
    connection: Connection,
    path: str,
    body: dict[str, Any] | None,
    method: str = "POST",
    *,
    _prepared: _PreparedConnection | None = None,
) -> dict[str, Any]:
    """One of this computer's calls to the identity's space on its server (``/u/<route key>/_latet/...``), through
    Latet's client of its server: the certificate this computer pinned (or the system's authorities, the host checked),
    no redirect, no proxy, the answer read within its bounds and deadline and by its shape, the server's words escaped.
    Raises DaemonError, with the status the server refused with (None when nothing usable answered)."""
    from latet.server.client import ServerClient, ServerError, answer_of
    from latet.server.tokens import route_key

    if _prepared is not None:
        from latet.server.connection import _require_selection

        _require_selection(_prepared)
    token = _daemon_token(_prepared) if _prepared is not None else daemon_token()
    try:
        with ServerClient(connection.url, token, connection.fingerprint) as client:
            return answer_of(client.request(method, f"/u/{route_key(token)}{path}", body=body))
    except ServerError as exc:
        if exc.status == 401:  # R7: why, and what to do
            from latet.server.connection import token_refused

            raise DaemonError(token_refused(connection), 401) from None
        raise DaemonError(exc.message, exc.status or None) from None


def destination_for(
    url: str, agent: str, config: Config | None = None, *, _prepared: _PreparedConnection | None = None
) -> str:
    """The name of the daemon's destination for ``url``, or the space's on a server: added when new (that lowers
    nothing). Raises DaemonError for an answer that is not a destination's name of Latet's shape (it goes into an
    agent's address)."""
    if _prepared is None:
        answer = _daemon_call("/_latet/admin/destinations", {"destinations": [{"url": url, "agent": agent}]}, config)
    else:
        answer = _daemon_call(
            "/_latet/admin/destinations", {"destinations": [{"url": url, "agent": agent}]}, config, _prepared=_prepared
        )
    names = answer.get("names")
    name = names[0] if isinstance(names, list) and len(names) == 1 else None
    if not (isinstance(name, str) and _DESTINATION.fullmatch(name) and name not in RESERVED_DESTINATIONS):
        raise DaemonError(
            "the answer is not a destination's name: lower-case letters, digits and dashes, not Latet's own"
        )
    return name


def aws_session(credentials: AwsCredentials, config: Config | None = None) -> str:
    """AWS credentials found only in this shell, handed to the daemon for one session (kept in its memory), or to the
    identity's space on a server (sealed in its Valkey). Raises DaemonError for an answer that is not a session's id of
    Latet's shape (it goes into the hold's path and Claude Code's address)."""
    body = {
        "access_key": credentials.access_key,
        "secret_key": credentials.secret_key,
        "token": credentials.token,
        "expires": credentials.expires,
    }
    session = _daemon_call("/_latet/engine/v1/aws-sessions", body, config).get("id")
    if not (isinstance(session, str) and _SESSION.fullmatch(session)):
        raise DaemonError("the answer is not a session's id: 32 lower-case hexadecimal digits")
    return session


def hold_aws_session(session: str, config: Config | None = None) -> threading.Thread | None:
    """Keep a session at the daemon, or at the identity's space on a server (over HTTPS, with the certificate this
    computer pinned), for as long as this process lives: a thread holds a request open to it and reads until it ends.
    The daemon drops the session when that connection closes, which the operating system does for a process that is
    killed too. None when nothing holds it (an older daemon has no such request, an id or a connection that cannot be
    used): the session then lasts until `drop_aws_session`, or until the daemon restarts."""
    import http.client
    from urllib.parse import urlsplit

    from latet.server.connection import NotConnected

    if not _SESSION.fullmatch(session):
        return None
    config = config or load_config()
    try:
        base = urlsplit(base_url(config))
        path = f"{base.path.rstrip('/')}/_latet/engine/v1/aws-sessions/{session}/hold"
        token = daemon_token()
        if base.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                base.hostname or "", base.port or 443, timeout=30, context=tls()
            )
        else:
            conn = http.client.HTTPConnection(base.hostname or "127.0.0.1", base.port or 80, timeout=30)
    except (NotConnected, OSError, ValueError):
        return None
    try:
        conn.request("GET", path, headers={"x-latet-token": token})
        answer = conn.getresponse()
    except (OSError, http.client.HTTPException):  # InvalidURL among them
        conn.close()
        return None
    if answer.status != 200 or conn.sock is None:
        conn.close()
        return None
    conn.sock.settimeout(None)  # held until it ends: a pause (a laptop asleep) must not end it

    def hold() -> None:
        try:
            with contextlib.suppress(OSError, http.client.HTTPException):  # the daemon went away, or ended it
                while answer.read(1):
                    pass
        finally:
            conn.close()

    thread = threading.Thread(target=hold, name="latet-aws-hold", daemon=True)
    thread.start()
    return thread


def drop_aws_session(session: str, config: Config | None = None) -> None:
    with contextlib.suppress(DaemonError):  # the daemon restarted since: nothing left to drop
        _daemon_call(f"/_latet/engine/v1/aws-sessions/{session}", None, config, method="DELETE")


def opencode_env(config: Config | None = None, said: list[str] | None = None) -> dict[str, str]:
    return {"OPENCODE_CONFIG_CONTENT": json.dumps(opencode_overrides(config)), **extra_ca_env("opencode", said)}
