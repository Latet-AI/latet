"""Claude Code, Codex and OpenCode integrations (global mode)."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from latet import __version__, wrappers
from latet.config import Config, data_dir, load_config, save_config, write_atomic
from latet.destinations import add_destination
from latet.integrations import jsonc, merge
from latet.integrations.base import (
    AgentInfo,
    FileEdit,
    Record,
    RecordsUnavailable,
    backup,
    checked_backup,
    edit_text,
    load_records,
    records_preflight,
    restore,
    save_records,
)
from latet.integrations.routes import (
    BASE_VARS,
    ROUTE_VARS,
    ClaudeRoute,
    CodexRoute,
    Registrar,
    claude_sources,
    codex_texts,
    detect_claude,
    detect_codex,
    is_latet,
    settings_env,
)
from latet.logline import described
from latet.storage.folders import private_folder_problem

if TYPE_CHECKING:
    from latet.server.connection import _PreparedConnection

# Kept with the route in the record, so that `revert` keeps a value you set since: the certificates Claude Code is
# given for a server whose self-signed certificate this computer pinned.
_CERTIFICATES = "NODE_EXTRA_CA_CERTS"


class RouteProblem(RuntimeError):
    """Latet cannot stand in front of where the agent goes: why, and what to do."""


def _backup_env(path: Path) -> dict[str, str]:
    try:
        data = json.loads(checked_backup(path))
    except (ValueError, RecursionError):
        raise RecordsUnavailable("Integration records unavailable (malformed prior settings backup).") from None
    env = data.get("env") if isinstance(data, dict) else None
    return {k: str(v) for k, v in env.items() if k in ROUTE_VARS} if isinstance(env, dict) else {}


def register_destination(url: str, agent: str, config: Config) -> str:
    """A destination for an address an agent's settings name: added by the daemon when it runs (in use at
    once), else written to config.yaml for the daemon to read when it starts. That is for no daemon at all,
    or an older one without the route (404). One that answers and refuses may not be this Latet (another
    token, another program on the port): it is not gone round, and the agent is left as it is. On a computer
    connected to a Latet server the identity's space adds it, or nothing does: its destinations are the server's."""
    try:
        return wrappers.destination_for(url, agent, config)
    except wrappers.DaemonError as exc:
        if wrappers.server_base() is not None:  # the server keeps the identity's destinations, never this computer
            said = f"the Latet server did not add {url} ({exc.status or 'no answer'}: {exc})"
            raise RouteProblem(said) from None
        if exc.status not in (None, 404):  # None: no daemon runs; 404: an older one, without the route
            raise RouteProblem(
                f"what answers on port {config.port} refused to add {url} ({exc.status}: {exc}): is it the "
                "Latet of this computer? `latet doctor` shows what is on the port"
            ) from None
    current = load_config()
    try:
        current, name, added = add_destination(current, url, agent)  # type: ignore[arg-type]
    except ValidationError as exc:
        reason = exc.errors(include_url=False, include_context=False, include_input=False)[0]["msg"]
        raise RouteProblem(f"{url} cannot be a destination: {reason}") from None
    if added:
        save_config(current)
    return name


def _registering(config: Config, agent: str) -> Registrar:
    """Destinations' names, each one added (by the daemon, else in config.yaml): for what is applied."""
    return lambda url: register_destination(url, agent, config)


def _naming(config: Config, agent: str) -> Registrar:
    """Destinations' names without adding any: for what a summary or a check says."""
    return lambda url: add_destination(config, url, agent)[1]  # type: ignore[arg-type]


def _linked(path: Path, root: Path) -> Path | None:
    """The first of ``path`` and its folders, up to ``root`` (the agent's own settings folder), that is a link or a
    junction, or None: on a computer connected to a Latet server, an agent's file that would hold this computer's token
    or the address of its space is never written through one, which other places may share (a dotfiles repository)."""
    current = path
    while True:
        try:
            if current.is_symlink() or current.is_junction():
                return current
        except OSError:
            return current
        if current == root or current.parent == current:
            return None
        current = current.parent


def _left_as_it_is(agent: str, link: Path, command: str) -> RouteProblem:
    return RouteProblem(
        f"{link} is a link, which other places may share (a repository of dotfiles, say), and {agent}'s settings would "
        f"hold the address of your space on the server there, a secret like this computer's token: Latet leaves "
        f"{agent} as it is; `{command}` keeps working"
    )


def _token_kept_out(settings: Path) -> str | None:
    """Why Claude Code's settings.json may not hold this computer's token for its server, in words of its own; None
    when it may (the user's ruling accepts the token there, not where others read it): a plain file (neither it nor its
    folder a link: refused before, ``_linked``), in a folder that is the person's alone (on POSIX owned by them and
    writable by nobody else; on Windows its access list), and on Windows a profile that does not roam (it would carry
    the file to a profile server and the person's other computers)."""
    folder = settings.parent
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if settings.exists() and not settings.is_file():
            return f"{settings} is not a plain file"
    except OSError as exc:
        return f"{settings} cannot be read ({exc.strerror or described(exc)})"
    if (problem := private_folder_problem(folder)) is not None:
        return problem
    return _roaming(folder)


def _roaming(folder: Path) -> str | None:
    """Why ``folder`` may be carried to other computers by a Windows profile that roams, or None: the ``ProfileList``
    entry whose ``ProfileImagePath`` holds it has a ``CentralProfile`` (a folder in no profile does not roam)."""
    if sys.platform != "win32":
        return None
    import winreg

    target = os.path.normcase(os.path.abspath(folder))
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
        ) as root:
            for index in itertools.count():
                try:
                    sid = winreg.EnumKey(root, index)
                except OSError:  # no more entries
                    return None
                with winreg.OpenKey(root, sid) as entry:
                    try:
                        image = str(winreg.QueryValueEx(entry, "ProfileImagePath")[0])
                    except OSError:
                        continue
                    home = os.path.normcase(os.path.abspath(os.path.expandvars(image)))
                    if target != home and not target.startswith(home.rstrip("\\") + "\\"):
                        continue
                    try:
                        central = str(winreg.QueryValueEx(entry, "CentralProfile")[0] or "")
                    except OSError:
                        central = ""
                    return "your Windows profile roams, and the file with it, to other computers" if central else None
    except OSError as exc:
        return f"Latet cannot tell whether your Windows profile roams ({described(exc)})"
    return None


def _token_said(settings: Path, server: str, kept_out: str | None) -> str:
    if kept_out is None:
        return (
            f"{settings} now holds this computer's token for {server}: any program Claude Code runs can read it; never "
            "sync or commit the file; `latet disconnect` removes it."
        )
    return (
        f"{settings} does not hold this computer's token: {kept_out}. Sessions started without `latet claude` are "
        "stopped by the plugin until it can; `latet claude` keeps working, its token in a file of Latet's own."
    )


def _address_said(path: Path, server: str) -> str:
    return (
        f"{path} now holds the address of your space on {server}, a secret like this computer's token: never sync or "
        "commit it."
    )


def _version(exe: str) -> str | None:
    path = shutil.which(exe)
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else None


@dataclass
class ClaudeOptions:
    # Claude Code attaches a `safeguards` field (home directory, working directory, OS user name,
    # git state) for server-side auto-mode review; no hook can mask it. Disabling it turns off the
    # server-side classifier review of auto mode.
    disable_classifier_context: bool = True
    # claude.ai connectors (Gmail, Calendar...) receive tool arguments through Anthropic's servers.
    disable_claudeai_connectors: bool = False
    disable_nonessential_traffic: bool = False
    extra_env: dict[str, str] = field(default_factory=dict)


class ClaudeCode:
    id = "claude"
    name = "Claude Code"

    def __init__(self) -> None:
        self.said: list[str] = []  # what the last apply has to say, a line each (where the token is, ...)

    def config_dir(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")

    def settings_path(self) -> Path:
        return self.config_dir() / "settings.json"

    def detect(self) -> AgentInfo:
        exe = shutil.which("claude")
        return AgentInfo(
            self.id, self.name, exe is not None, _version("claude") if exe else None, str(self.settings_path())
        )

    def env_for(
        self,
        options: ClaudeOptions,
        config: Config,
        user_name: str | None = None,
        said: list[str] | None = None,
        *,
        _prepared: _PreparedConnection | None = None,
    ) -> dict[str, str]:
        env = wrappers.claude_env(
            config,
            disable_classifier_context=options.disable_classifier_context,
            user_name=user_name,
            said=said,
            _prepared=_prepared,
        )
        if options.disable_claudeai_connectors:
            env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        if options.disable_nonessential_traffic:
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env.update(options.extra_env)
        return env

    def marketplace_dir(self) -> Path:
        return data_dir() / "claude-marketplace"

    def prepare_marketplace(self) -> Path:
        """A local marketplace holding this version's plugin (Claude Code caches by version)."""
        from latet.storage.folders import make_private_folder

        root = self.marketplace_dir()
        make_private_folder(root.parent)  # the data folder, when this makes it, is the person's alone
        target = root / "plugins" / "latet"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            wrappers.plugin_dir(),
            target,
            ignore=shutil.ignore_patterns("types", "node_modules", "tests", "tsconfig.json"),
        )
        manifest = target / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text("utf-8"))
        data["version"] = __version__
        manifest.write_text(json.dumps(data, indent=2), "utf-8")
        market = {
            "name": "latet",
            "owner": {"name": "Latet"},
            "plugins": [
                {
                    "name": "latet",
                    "source": "./plugins/latet",
                    "description": "Masks secrets and personal data before Claude Code sends them.",
                    "version": __version__,
                }
            ],
        }
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps(market, indent=2), "utf-8")
        return root

    def _claude(self, *args: str) -> dict:
        exe = shutil.which("claude")
        if exe is None:
            raise FileNotFoundError("claude")
        out = subprocess.run([exe, *args, "--json"], capture_output=True, text=True, timeout=120, check=False)  # noqa: S603
        lines = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")]
        result: dict = json.loads(lines[-1]) if lines else {"outcome": "error", "message": out.stderr.strip()}
        result["returncode"] = out.returncode
        return result

    def original_env(self, *, _settings: dict[str, str] | None = None) -> dict[str, str]:
        """Your settings' route variables as they were before Latet wrote its own."""
        record = load_records().get(self.id)
        current = settings_env(self.settings_path()) if _settings is None else _settings
        if record is None:
            return current
        previous = record.data.get("previous")
        if previous is None:  # an install older than this: its backup holds the values it replaced
            saved = record.files[0].backup if record.files else None
            return _backup_env(Path(saved)) if saved else {}
        out = {k: v for k, v in current.items() if k not in record.data.get("env", [])}
        out.update({k: v for k, v in previous.items() if v is not None and k in ROUTE_VARS})
        return out

    def route(
        self,
        config: Config,
        register: Registrar | None = None,
        *,
        _prepared: _PreparedConnection | None = None,
        _settings: dict[str, str] | None = None,
        _diagnostic: bool = False,
    ) -> ClaudeRoute:
        """Where Claude Code goes for good, read past Latet's own values; ``register`` None: destinations
        are only named, none is added."""
        from latet.server.connection import _agent_base, _prepare_connection, _prepare_token, _require_selection

        prepared = _prepared if _prepared is not None else _prepare_token(_prepare_connection())
        _require_selection(prepared)
        sources = claude_sources(os.environ, self.config_dir(), _user=_settings)
        sources["user"] = self.original_env(_settings=_settings)
        _require_selection(prepared)
        token = "" if _diagnostic and prepared.connection is None else wrappers._daemon_token(prepared)
        naming = register or _naming(config, "claude")
        return detect_claude(sources, config, token, naming, None, _agent_base(prepared, config))

    def apply(
        self,
        options: ClaudeOptions | None = None,
        config: Config | None = None,
        *,
        _prepared: _PreparedConnection | None = None,
    ) -> Record:
        """Claude Code routed through Latet for good: the plugin, and its settings' env block. On a computer connected
        to a Latet server, the token goes into settings.json only where nobody else reads it (``_token_kept_out``),
        said in ``said`` either way, and the certificates it is given hold the person's own as they are now."""
        from latet import claude_bridge
        from latet.server.connection import _prepare_connection, _prepare_token, _require_selection

        records_preflight(write=True)
        options = options or ClaudeOptions()
        config = config or load_config()
        self.said = []
        prepared = _prepared if _prepared is not None else _prepare_connection()
        _require_selection(prepared)
        connection = prepared.connection
        if connection is not None and (link := _linked(self.settings_path(), self.config_dir())) is not None:
            raise _left_as_it_is("Claude Code", link, "latet claude")  # nothing written, the undo included
        if _prepared is None:
            prepared = _prepare_token(prepared)
        _require_selection(prepared)
        registering = _registering(config, "claude")

        def register(url: str) -> str:
            _require_selection(prepared)
            result = registering(url)
            _require_selection(prepared)
            return result

        already = self.id in load_records()
        # Asked before undo, so a refused route preserves the previous successful install and binding.
        if already and (ahead := self.route(config, register, _prepared=prepared)).problem:
            raise RouteProblem(ahead.problem)
        descriptor = claude_bridge._binding_path(prepared.selected.data)
        platform = claude_bridge.platform_problem() if connection is not None else None  # R5: Windows alone
        eligible = connection is not None and platform is None and _token_kept_out(self.settings_path()) is None
        binding = claude_bridge._plan_binding(prepared, descriptor) if eligible else None
        if already:
            _require_selection(prepared)
            self.revert()
        route = self.route(config, register, _prepared=prepared)
        if route.problem:
            raise RouteProblem(route.problem)
        try:  # on a server the space decides, from its own settings
            asked = config.auto_mode_review.user_name == "surrogate" or connection is not None
            _require_selection(prepared)
            surrogate = wrappers.user_name_surrogate(config, _prepared=prepared) if asked else None
            _require_selection(prepared)
        except wrappers.DaemonError as exc:
            raise RouteProblem(f"Latet cannot give Claude Code your user name's fake ({exc})") from None
        _require_selection(prepared)
        claude_bridge._remove_binding(
            descriptor
        )  # Before the first bundle/plugin/settings effect, even after a partial apply.
        notes: list[str] = []
        env = {
            k: v
            for k, v in self.env_for(options, config, surrogate, notes, _prepared=prepared).items()
            if k != "ANTHROPIC_BASE_URL"
        }
        env.update(route.env)  # Anthropic, or the company's endpoint, through Latet (or as it is, when local)
        settings = self.settings_path()
        kept_out = _token_kept_out(settings) if connection is not None and platform is None else None
        if kept_out is not None:  # the route and the plugin, and the token where nobody else reads it, or nowhere
            env.pop("LATET_BRIDGE_TOKEN", None)
            binding = None
        if connection is not None and binding is None:  # no token where no helper binding can use it (R5)
            env.pop("LATET_BRIDGE_TOKEN", None)
        if binding is not None:
            env[claude_bridge.HELPER_KEY] = binding.setting
        _require_selection(prepared)
        previous = merge.claude_previous(settings.read_text("utf-8") if settings.exists() else "", env)
        _require_selection(prepared)
        backup_path, sha_before = backup(settings)  # the user's file, before anything touches it
        # 1. The plugin, from a local marketplace (the claude CLI edits settings.json itself).
        _require_selection(prepared)
        market = self.prepare_marketplace()
        _require_selection(prepared)
        add = self._claude("plugin", "marketplace", "add", str(market))
        if add.get("outcome") != "ok" and "already" not in str(add.get("message", "")).lower():
            raise RuntimeError(f"claude plugin marketplace add failed: {add.get('message')}")
        self._claude("plugin", "marketplace", "update", "latet")
        install = self._claude("plugin", "install", "latet@latet")
        if install.get("outcome") != "ok" and "already" not in str(install.get("message", "")).lower():
            raise RuntimeError(f"claude plugin install failed: {install.get('message')}")
        self._claude("plugin", "update", "latet@latet")
        # 2. The env block: ANTHROPIC_BASE_URL and the bridge settings.
        _require_selection(prepared)
        final = merge.claude_merge(settings.read_text("utf-8") if settings.exists() else "", env).encode("utf-8")
        settings.parent.mkdir(parents=True, exist_ok=True)
        if connection is not None or not settings.is_symlink():
            # the route and the bridge token (it opens the daemon's engine): the person's alone, whatever mode it had
            # (Claude Code writes it 0644)
            write_atomic(settings, final.decode("utf-8"), private=True)
        else:  # a dotfiles link on this computer: written through, and what it names made private (POSIX)
            settings.write_bytes(final)
            if os.name != "nt":
                os.chmod(settings, 0o600)
        edit = FileEdit(str(settings), backup_path, sha_before, hashlib.sha256(final).hexdigest())
        applied = {
            "env": sorted(env),
            "options": asdict(options),
            "user_name_surrogate": surrogate is not None,
            # What it replaced (a company's ANTHROPIC_BASE_URL), to give it back on undo even after you
            # edited the file; and its own route values, to leave one you changed since.
            "previous": {k: v for k, v in previous.items() if not k.startswith("LATET_")},
            "written": {
                k: v for k, v in env.items() if k in ROUTE_VARS or k in (_CERTIFICATES, claude_bridge.HELPER_KEY)
            },
            "route": {"kind": route.kind, "summary": route.summary},
        }
        record = Record(self.id, time.time(), [edit], applied)
        _require_selection(prepared)
        records = load_records()
        records[self.id] = record
        _require_selection(prepared)
        save_records(records)
        if binding is not None:
            claude_bridge._publish_binding(binding)
        if connection is not None and platform is not None:
            self.said.append(f"{platform}. {settings} does not hold this computer's token.")
        elif connection is not None:
            self.said.append(_token_said(settings, connection.url, kept_out))
        self.said.extend(notes)
        if connection is not None and env.get(_CERTIFICATES) and _bundles_theirs(Path(env[_CERTIFICATES])):
            self.said.append(
                f"The certificates Claude Code is given ({env[_CERTIFICATES]}) hold yours as they are now, then the "
                "server's: one you remove later stays until the next `latet agents apply claude`."
            )
        return record

    def applied_options(self) -> ClaudeOptions | None:
        """The options of the current integration, to apply the same again (after an upgrade)."""
        record = load_records().get(self.id)
        if record is None:
            return None
        known = {k: v for k, v in (record.data.get("options") or {}).items() if k in ClaudeOptions.__dataclass_fields__}
        return ClaudeOptions(**known)

    def revert(self) -> str:
        from latet import claude_bridge

        records_preflight(write=True)
        records = load_records()
        record = records.get(self.id)
        claude_bridge._remove_binding(claude_bridge._binding_path(data_dir()))
        if record is None:
            return "not applied"
        edit = record.files[0]
        saved = checked_backup(Path(edit.backup)) if edit.backup is not None else None
        settings = Path(edit.path)
        current = settings.read_bytes() if settings.exists() else None
        untouched = current is not None and hashlib.sha256(current).hexdigest() == edit.sha_after
        if shutil.which("claude"):
            self._claude("plugin", "uninstall", "latet@latet")
            self._claude("plugin", "marketplace", "remove", "latet")
        if untouched:
            if edit.backup:
                settings.write_bytes(saved if saved is not None else b"")
                outcome = "restored"
            else:
                settings.unlink(missing_ok=True)
                outcome = "removed"
        elif settings.exists():
            previous = record.data.get("previous")
            if previous is None and edit.backup:  # an older install: what it replaced is in its backup
                previous = _backup_env(Path(edit.backup))
            text = settings.read_text("utf-8")
            text = merge.claude_unmerge(text, record.data.get("env", []), previous, record.data.get("written"))
            data = json.loads(text)
            data.get("enabledPlugins", {}).pop("latet@latet", None)
            data.get("extraKnownMarketplaces", {}).pop("latet", None)
            for key in ("enabledPlugins", "extraKnownMarketplaces"):
                if key in data and not data[key]:
                    data.pop(key)
            settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
            outcome = "edited since: removed Latet settings only"
        else:
            outcome = "missing"
        records.pop(self.id)
        save_records(records)
        return outcome

    def status(self, *, _settings: dict[str, Any] | None = None, _prepared: _PreparedConnection | None = None) -> str:
        from latet.server.connection import _agent_base, _require_selection

        if _prepared is not None:
            _require_selection(_prepared)
        settings = self.settings_path()
        if _settings is None and not settings.exists():
            return "not applied"
        data = json.loads(settings.read_text("utf-8")) if _settings is None else _settings
        env = data.get("env", {})
        plugin = data.get("enabledPlugins", {}).get("latet@latet")
        port = load_config().port
        record = load_records().get(self.id)
        local = record is not None and (record.data.get("route") or {}).get("kind") == "local"
        server = _agent_base(_prepared) if _prepared is not None else wrappers.server_base()
        if plugin and (local or any(is_latet(str(env.get(var, "")), port, server) for var in BASE_VARS)):
            return "applied"
        if plugin or any(env.get(var) for var in BASE_VARS):
            return "partial"
        return "not applied"


def _bundles_theirs(path: Path) -> bool:
    """Whether the certificates Latet wrote for an agent hold the person's own beside the server's pin (one)."""
    try:
        return path.read_text("utf-8").count("-----BEGIN CERTIFICATE-----") > 1
    except OSError:
        return False


class Codex:
    id = "codex"
    name = "Codex"

    def __init__(self) -> None:
        self.said: list[str] = []

    def config_path(self) -> Path:
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"

    def detect(self) -> AgentInfo:
        exe = shutil.which("codex")
        info = AgentInfo(
            self.id, self.name, exe is not None, _version("codex") if exe else None, str(self.config_path())
        )
        version = wrappers.codex_version() if exe else None
        if version is not None and version < (0, 115, 0):
            info.notes.append("Codex older than 0.115 ignores openai_base_url: update it (npm i -g @openai/codex).")
        desktop = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex"
        if os.name == "nt" and desktop.exists():
            info.notes.append("Codex Desktop found: it reads the same config.toml.")
        return info

    def outdated(self) -> str | None:
        """Why `codex` does not read the addresses Latet would write in its files: a Codex older than 0.115
        reads only OPENAI_BASE_URL. None when it reads them, or when no Codex says its version."""
        version = wrappers.codex_version()
        if version is None or version >= (0, 115, 0):
            return None
        return (
            f"Codex {'.'.join(map(str, version))} ignores openai_base_url in config.toml: update it "
            "(npm i -g @openai/codex), or start it with `latet codex`"
        )

    def original_texts(self) -> dict[str, str]:
        """config.toml and the profiles' files as they were before Latet wrote its addresses."""
        record = load_records().get(self.id)
        texts = codex_texts(self.config_path().parent)
        if record is None:
            return texts
        edits = record.data.get("edits") or {}
        for edit in record.files:
            name = Path(edit.path).name
            info = edits.get(name)
            text = texts.get(name, "")
            texts[name] = (
                merge.codex_unmerge(text, info["previous"], info["overrides"]) if info else merge.codex_unmerge(text)
            )
        return texts

    def route(self, config: Config, chatgpt: bool | None = None, register: Registrar | None = None) -> CodexRoute:
        """Where Codex goes for good, read past Latet's own addresses; ``register`` None: destinations
        are only named, none is added."""
        if chatgpt is None:
            chatgpt = wrappers.codex_uses_chatgpt()
        naming = register or _naming(config, "codex")
        return detect_codex(self.original_texts(), config, chatgpt, naming, address=wrappers.base_url(config))

    def apply(self, config: Config | None = None, chatgpt: bool | None = None) -> Record:
        """Codex routed through Latet for good, in the files that write its addresses. On a computer connected to a
        Latet server they hold the address of the identity's space, a secret like the token: private on POSIX, and
        said in ``said``."""
        records_preflight(write=True)
        config = config or load_config()
        self.said = []
        home = self.config_path().parent
        if (connection := wrappers.connected()) is not None:  # no file of its own written through a link
            for path in (home, *(home / name for name in codex_texts(home))):
                if (link := _linked(path, home)) is not None:
                    raise _left_as_it_is("Codex", link, "latet codex")
        if (outdated := self.outdated()) is not None:  # its files would say Latet, and it would go around it
            raise RouteProblem(outdated)
        if chatgpt is None:  # asked once: the route is worked out twice when this is a re-apply
            chatgpt = wrappers.codex_uses_chatgpt()
        register = _registering(config, "codex")
        records = load_records()
        if self.id in records:
            # Asked before the undo, so the install stays whole when the daemon refuses, or when Latet can no
            # longer stand in front of the route (a file it cannot read, an address over http...): undone, Codex
            # would go around Latet. Otherwise this route is dropped: the files are read again once undone.
            if (ahead := self.route(config, chatgpt, register)).problem:
                raise RouteProblem(ahead.problem)
            self.revert()
        route = self.route(config, chatgpt, register)
        if route.problem:
            raise RouteProblem(route.problem)
        by_file = {name: keys for name, keys in route.edits.items() if keys}  # a profile's in its own file
        files, edits = [], {}
        for name, overrides in by_file.items():
            path = home / name
            previous = merge.codex_previous(path.read_text("utf-8") if path.exists() else "", overrides)
            change = lambda text, o=overrides: merge.codex_merge(text, o)  # noqa: E731
            files.append(edit_text(path, change, private=connection is not None))
            edits[name] = {"overrides": overrides, "previous": previous}
            if connection is not None:
                self.said.append(_address_said(path, connection.url))
        data = {
            "edits": edits,
            "route": {"summary": route.summary},
            "openai_base_url": route.edits.get("config.toml", {}).get("openai_base_url"),
        }
        record = Record(self.id, time.time(), files, data)
        records = load_records()
        records[self.id] = record
        save_records(records)
        return record

    def revert(self) -> str:
        records_preflight(write=True)
        records = load_records()
        record = records.get(self.id)
        if record is None:
            return "not applied"
        edits = record.data.get("edits") or {}
        outcomes = []
        for edit in record.files:
            info = edits.get(Path(edit.path).name)
            if info is None:  # an older install: only its openai_base_url
                outcomes.append(restore(edit, merge.codex_unmerge))
            else:
                outcomes.append(
                    restore(edit, lambda text, i=info: merge.codex_unmerge(text, i["previous"], i["overrides"]))
                )
        records.pop(self.id)
        save_records(records)
        return ", ".join(outcomes) or "nothing to undo"

    def status(self) -> str:
        """Applied when every address Codex may use goes through Latet (or stays on this computer)."""
        home = self.config_path().parent
        texts = codex_texts(home)
        address = wrappers.base_url()
        route = detect_codex(texts, load_config(), False, _naming(load_config(), "codex"), address=address)
        if route.problem is None and not any(route.edits.values()):
            return "applied"
        here = wrappers.server_base() or "127.0.0.1"
        return "partial" if any(here in text for text in texts.values()) else "not applied"


def _opencode_desktop_paths(platform: str, local: str | None, home: Path) -> list[Path]:
    """Where OpenCode Desktop's installer puts the app: on Windows in the person's programs folder, on macOS among the
    applications. None looked for elsewhere."""
    if platform == "win32":
        return [Path(local) / "Programs" / "@opencode-aidesktop" / "OpenCode.exe"] if local else []
    if platform == "darwin":
        return [Path("/Applications/OpenCode.app"), home / "Applications" / "OpenCode.app"]
    return []


def _opencode_desktop() -> Path | None:
    """OpenCode Desktop on this computer, or None."""
    paths = _opencode_desktop_paths(sys.platform, os.environ.get("LOCALAPPDATA"), Path.home())
    return next((path for path in paths if path.exists()), None)


class OpenCode:
    id = "opencode"
    name = "OpenCode"
    PROVIDERS = merge.OPENCODE_PROVIDERS

    def __init__(self) -> None:
        self.said: list[str] = []

    def config_path(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
        for name in ("opencode.jsonc", "opencode.json"):
            if (base / name).exists():
                return base / name
        return base / "opencode.jsonc"

    def detect(self) -> AgentInfo:
        exe = shutil.which("opencode")
        app = None if exe else _opencode_desktop()  # the app reads the same configuration as the command line
        version = _version("opencode") if exe else ("desktop app" if app else None)
        info = AgentInfo(self.id, self.name, exe is not None or app is not None, version, str(self.config_path()))
        if app is not None:
            info.notes.append(
                "OpenCode Desktop reads this configuration: `latet agents apply opencode` routes it once the app is "
                "restarted; `latet opencode` starts the command-line OpenCode only."
            )
        info.notes.append(
            "API-key providers only: OpenCode's ChatGPT sign-in plugin cannot be routed (on the roadmap)."
        )
        return info

    def apply(self, config: Config | None = None) -> Record:
        """OpenCode routed through Latet for good, in its config file. On a computer connected to a Latet server it
        holds the address of the identity's space: private on POSIX, and said in ``said``."""
        records_preflight(write=True)
        self.said = []
        path = self.config_path()
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if (connection := wrappers.connected()) is not None and (link := _linked(path, root)) is not None:
            raise _left_as_it_is("OpenCode", link, "latet opencode")  # nothing written, the undo included
        records = load_records()
        if self.id in records:
            self.revert()
        overrides = wrappers.opencode_overrides(config)["provider"]
        urls = {provider: overrides[provider]["options"]["baseURL"] for provider in self.PROVIDERS}
        edit = edit_text(path, lambda text: merge.opencode_merge(text, urls), private=connection is not None)
        record = Record(self.id, time.time(), [edit], {"providers": list(self.PROVIDERS)})
        records[self.id] = record
        save_records(records)
        if connection is not None:
            self.said.append(_address_said(path, connection.url))
        return record

    def revert(self) -> str:
        records_preflight(write=True)
        records = load_records()
        record = records.get(self.id)
        if record is None:
            return "not applied"

        outcome = ", ".join(restore(f, merge.opencode_unmerge) for f in record.files)
        records.pop(self.id)
        save_records(records)
        return outcome

    def status(self) -> str:
        path = self.config_path()
        if not path.exists():
            return "not applied"
        data = jsonc.loads(path.read_text("utf-8"))
        urls = [data.get("provider", {}).get(p, {}).get("options", {}).get("baseURL", "") for p in self.PROVIDERS]
        port, server = load_config().port, wrappers.server_base()
        if all(isinstance(u, str) and is_latet(u, port, server) for u in urls):
            return "applied"
        return "partial" if any(urls) else "not applied"


@dataclass
class GeminiRoute:
    """How Gemini CLI reaches its model, as its settings say, and whether Latet goes in front of it."""

    summary: str = ""
    problem: str | None = None
    notes: list[str] = field(default_factory=list)


class GeminiCli:
    """Gemini CLI, which Latet protects when it is started with `latet gemini`: it takes its base URL only from the
    environment and from one `.env` file (the first it finds from the working folder up, so a project's own hides the
    one in your home), and no setting holds it. An edit for good would leave it unprotected in every project with a
    `.env` of its own, without a word: none is made (``WRAPPED``, not ``ALL``)."""

    id = "gemini"
    name = "Gemini CLI"

    def detect(self) -> AgentInfo:
        exe = shutil.which("gemini")
        return AgentInfo(
            self.id,
            self.name,
            exe is not None,
            _version("gemini") if exe else None,
            str(wrappers.gemini_settings_path()),
        )

    def status(self) -> str:
        return "wrapper"

    def route(self, config: Config | None = None) -> GeminiRoute:
        chosen, where = wrappers.gemini_choice()
        if chosen in wrappers.GEMINI_THROUGH_LATET:
            how = {"gemini-api-key": "an API key", "vertex-ai": "Vertex AI", "gateway": "a gateway"}[chosen]
            return GeminiRoute(f"{how}: Latet goes in front of it when it is started with `latet gemini`")
        if chosen is None:
            note = (
                "no way of reaching its model is chosen in its settings yet: `latet gemini` offers an API key; a run "
                "with -p needs security.auth.selectedType set first"
            )
            return GeminiRoute("Latet goes in front of it when it is started with `latet gemini`", notes=[note])
        return GeminiRoute(
            problem=f"it is signed in with Google ({chosen}, in {where}), an endpoint Latet does not read"
        )


ALL = {"claude": ClaudeCode(), "codex": Codex(), "opencode": OpenCode()}
# Agents Latet protects when they are started with its command, which it never edits for good.
WRAPPED = {"gemini": GeminiCli()}
