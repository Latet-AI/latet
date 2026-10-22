"""Where each agent sends its requests today, read from the agent's own settings (never a repository's),
and how Latet goes in front of it: a company's gateway, Amazon Bedrock, Google Vertex, Microsoft
Foundry, Azure OpenAI. What Latet cannot stand in front of is said, never gone around in silence."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

from pydantic import ValidationError

from latet.config import Config, Destination
from latet.gateway.aws import credentials_end, profile_tag
from latet.gateway.cloud import PROFILE_NAME, REGION_NAME
from latet.gateway.sigv4 import AwsCredentials

log = logging.getLogger(__name__)

# An address → the name of its destination, added when new (by the daemon, or into the settings).
Registrar = Callable[[str], str]
# Credentials found only in the shell → the id of the session the daemon keeps them for.
SessionRegistrar = Callable[[AwsCredentials], str]

# The variables that decide where Claude Code sends its requests, and how it signs in to a cloud.
ROUTE_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SESSION_EXPIRATION",
    "AWS_CREDENTIAL_EXPIRATION",
    "CLOUD_ML_REGION",
)
BASE_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
)
# An organization's managed settings: above every other source, --settings included.
if sys.platform == "win32":
    MANAGED_SETTINGS: tuple[Path, ...] = (
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "ClaudeCode" / "managed-settings.json",
        Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "ClaudeCode" / "managed-settings.json",
    )
elif sys.platform == "darwin":
    MANAGED_SETTINGS = (Path("/Library/Application Support/ClaudeCode/managed-settings.json"),)
else:
    MANAGED_SETTINGS = (Path("/etc/claude-code/managed-settings.json"),)
_SOURCES = (("managed", "managed settings"), ("user", "user settings"), ("shell", "your shell"))
_CLOUD_SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
# What readers of an address may take apart differently (Python finds a host where Node finds another):
# spaces, control characters, a backslash.
_UNSAFE = re.compile(r"[\x00-\x20\x7f\\]")
_HERE = ("127.0.0.1", "localhost", "::1")
# Amazon's own address for Bedrock's runtime, as Claude Code may write it out: the standard host or the FIPS one.
_BEDROCK_RUNTIME = re.compile(r"bedrock-runtime(-fips)?\.([a-z0-9-]+)\.amazonaws\.com")


def _on(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _parts(url: object) -> SplitResult | None:
    """An address's parts, when every reader takes it the same way (Claude Code's, Codex's, Latet'):
    http or https, a host, no user or password, no spaces, control characters or backslashes."""
    if not isinstance(url, str) or _UNSAFE.search(url):
        return None
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - a port that is not one raises
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return None
    return parts


def _host(url: object) -> str:
    """The host of an address (and its port), for what Latet says: never a user or password in it."""
    if not isinstance(url, str) or _UNSAFE.search(url):
        return "an address that cannot be read"
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "an address that cannot be read"
    if not host:
        return "an address that cannot be read"
    return f"{host}:{port}" if port else host


def _origin(parts: SplitResult) -> tuple[str, str | None, int]:
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def is_latet(url: str, port: int, base: str | None = None) -> bool:
    """An address of Latet itself: on this computer on ``port``; with ``base``, the identity's space on a Latet server
    (`https://latet.acme.lan/u/<route key>`) and what is below it, as an agent sends it: a plain path (no segment empty,
    ``.`` or ``..``, nothing escaped with ``%``), no query and no fragment. An agent resolves dot segments, escaped ones
    too, by the URL standard: `/u/<mine>/../<theirs>/` goes to another identity's space on the same server."""
    parts = _parts(url)
    if base is None:
        return parts is not None and parts.scheme == "http" and parts.hostname in _HERE and parts.port == port
    home = _parts(base)
    if parts is None or home is None or _origin(parts) != _origin(home) or any(sign in url for sign in "%?#"):
        return False
    prefix = home.path.rstrip("/")
    if parts.path in (prefix, f"{prefix}/"):
        return True
    if not parts.path.startswith(f"{prefix}/"):
        return False
    return all(segment not in ("", ".", "..") for segment in parts.path[len(prefix) + 1 :].removesuffix("/").split("/"))


def this_machine(url: str) -> bool:
    """An address on this computer: a local model, or a proxy you run (which Latet cannot tell apart)."""
    parts = _parts(url)
    host = parts.hostname if parts is not None else None
    if not host:
        return False
    if host in ("localhost", socket.gethostname().lower()) or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _unroutable(url: str, var: str) -> str | None:
    """Why Latet cannot stand in front of an address, or None: one it cannot read as Claude Code and
    Codex do, or one it would not forward to (http on the internet)."""
    if _parts(url) is None:
        return (
            f"{var} ({_host(url)}) is not an address Latet can read the way your agent does: write it as "
            "https://host/path, without spaces, backslashes, user or password"
        )
    try:
        Destination(name="check", url=url)
    except ValidationError as exc:
        said = str(exc.errors()[0].get("msg", "")).removeprefix("Value error, ")
        return f"{var} sends to {_host(url)}, and {said}"
    return None


def _both(names: Sequence[str]) -> str:
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _declared(url: str, config: Config) -> bool:
    return any(d.url == url.rstrip("/") for d in config.upstreams)


def local_note(url: str) -> str:
    return (
        f"{_host(url)} is on this computer, so Latet leaves it alone; if it is a proxy to the cloud "
        f'(such as LiteLLM), add it as a destination: latet config add upstreams "{{name: local-proxy, url: '
        f'{url.rstrip("/")}}}"'
    )


def settings_env(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    env = data.get("env") if isinstance(data, dict) else None
    return {k: str(v) for k, v in env.items() if k in ROUTE_VARS} if isinstance(env, dict) else {}


def managed_setting(name: str) -> str | None:
    """The value your organization's managed settings give Claude Code's variable ``name`` (above every other source,
    --settings included), or None."""
    found = None
    for path in MANAGED_SETTINGS:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        env = data.get("env") if isinstance(data, dict) else None
        if isinstance(env, dict) and env.get(name) is not None:
            found = str(env[name])
    return found


def claude_sources(
    environ: Mapping[str, str],
    config_dir: Path,
    managed: Sequence[Path] | None = None,
    *,
    _user: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """Claude Code's route variables by where they are set: the shell, your settings.json, your
    organization's managed settings. A repository's .claude/settings.json is never read."""
    organization: dict[str, str] = {}
    for path in MANAGED_SETTINGS if managed is None else managed:
        organization.update(settings_env(path))
    return {
        "shell": {k: v for k, v in environ.items() if k in ROUTE_VARS},
        "user": settings_env(config_dir / "settings.json") if _user is None else _user,
        "managed": organization,
    }


@dataclass
class ClaudeRoute:
    """Where Claude Code goes, and the variables that put Latet in front of it."""

    kind: str  # anthropic, gateway, bedrock, vertex, foundry, local (left alone), blocked
    env: dict[str, str] = field(default_factory=dict)  # Claude Code's variables for it (none: as it is)
    summary: str = ""
    problem: str | None = None  # blocked: why, and what to do
    note: str | None = None
    source: str | None = None  # where its address was read


def detect_claude(
    sources: Mapping[str, Mapping[str, str]],
    config: Config,
    token: str,
    destination: Registrar,
    session: SessionRegistrar | None = None,
    address: str | None = None,
) -> ClaudeRoute:
    """Claude Code's route, from its variables as Claude Code merges them (the shell, then your settings,
    then the managed ones). ``session`` None: a permanent install, which cannot keep the shell's
    credentials. ``address``: Latet's address for agents, `wrappers.base_url()`; on a connected computer, the
    identity's space."""
    managed = sources.get("managed", {})
    env = {**sources.get("shell", {}), **sources.get("user", {}), **managed}
    latet = address or f"http://127.0.0.1:{config.port}"
    server = address if address is not None and _here(address) is None else None  # the identity's space on a server

    def source_of(var: str) -> str | None:
        return next((name for key, name in _SOURCES if var in sources.get(key, {})), None)

    def through(url: str, var: str) -> ClaudeRoute:
        if not is_latet(url, config.port, server) and (problem := _unroutable(url, var)):
            return ClaudeRoute("blocked", problem=problem, source=source_of(var))
        if is_latet(url, config.port, server):
            anthropic = f"{urlsplit(latet).path.rstrip('/')}/anthropic"
            kind = "anthropic" if urlsplit(url).path.rstrip("/") == anthropic else "gateway"
            return ClaudeRoute(kind, {var: url}, "already through Latet", source=source_of(var))
        if this_machine(url) and server is not None:  # a proxy here would send around the server
            problem = f"{var} sends to {_host(url)}, on this computer: with Latet on a server, Claude Code's requests "
            problem += "go through the server; remove it from your settings, or `latet disconnect`"
            return ClaudeRoute("blocked", problem=problem, source=source_of(var))
        if this_machine(url) and not _declared(url, config):
            return ClaudeRoute("local", {}, f"{_host(url)}, on this computer: left as it is", note=local_note(url))
        summary = f"your company's gateway {_host(url)}: Latet goes in front of it"
        return ClaudeRoute("gateway", {var: f"{latet}/{destination(url)}"}, summary, source=source_of(var))

    def amazon(base: str) -> ClaudeRoute | None:
        """Amazon's own addresses for Bedrock, written out while Latet must sign. As a gateway they would get
        Claude Code's signature, made for Latet's address, and Amazon refuses it: the public one is signed by
        Latet as if none were written; a FIPS address and a VPC endpoint are refused, as Latet cannot sign for
        them (it would send the first to the standard address). None: any other address."""
        if (runtime := _bedrock_runtime(base)) is not None:
            region, fips = runtime
            if not fips:
                # its region wins over AWS_REGION: the address is where the requests go
                return _bedrock({**env, "AWS_REGION": region}, latet, token, session, server is not None)
            problem = (
                f"Amazon Bedrock's FIPS address {_host(base)} is not supported yet, because Latet would send to the "
                "standard address, which is not a FIPS endpoint: use a Bedrock API key (AWS_BEARER_TOKEN_BEDROCK) or "
                "your company's gateway, or Bedrock's standard address if you do not need FIPS"
            )
        elif (parts := _parts(base)) is not None and (parts.hostname or "").endswith(".vpce.amazonaws.com"):
            problem = (
                f"Amazon Bedrock through the VPC endpoint {_host(base)} is not supported yet, because Latet would "
                "have to sign for that endpoint: use Bedrock's public address, a Bedrock API key "
                "(AWS_BEARER_TOKEN_BEDROCK) or your company's gateway"
            )
        else:
            return None
        return ClaudeRoute("blocked", problem=problem, source=source_of("ANTHROPIC_BEDROCK_BASE_URL"))

    for var in BASE_VARS:  # an address the organization forces: nothing overrides it, --settings neither
        url = managed.get(var)
        if url and not is_latet(url, config.port, server):
            return ClaudeRoute(
                "blocked",
                problem=f"your organization's managed settings send Claude Code to {_host(url)} ({var}) and nothing "
                "overrides them, so Latet cannot go in front: ask your IT to point it at Latet (Company "
                "endpoints, in the documentation)",
                source="managed settings",
            )
    if _on(env.get("CLAUDE_CODE_USE_MANTLE")):
        return ClaudeRoute(
            "blocked",
            problem="Amazon Bedrock's Mantle API is not one Latet reads yet: use Bedrock's Invoke API "
            "(CLAUDE_CODE_USE_BEDROCK) or your company's gateway",
        )
    if len(clouds := [name for name in _CLOUD_SWITCHES if _on(env.get(name))]) > 1:
        return ClaudeRoute(
            "blocked", problem=f"{_both(clouds)} are on, and Latet cannot tell which one Claude Code uses: keep one"
        )
    if _on(env.get("CLAUDE_CODE_USE_BEDROCK")):
        if base := env.get("ANTHROPIC_BEDROCK_BASE_URL"):
            if _latet_signs(env) and (route := amazon(base)) is not None:
                return route
            return through(base, "ANTHROPIC_BEDROCK_BASE_URL")
        return _bedrock(env, latet, token, session, server is not None)
    if _on(env.get("CLAUDE_CODE_USE_VERTEX")):
        if base := env.get("ANTHROPIC_VERTEX_BASE_URL"):
            return through(base, "ANTHROPIC_VERTEX_BASE_URL")
        summary = "Google Vertex, with your Google login: Latet goes in front of it"
        return ClaudeRoute("vertex", {"ANTHROPIC_VERTEX_BASE_URL": f"{latet}/vertex/v1"}, summary)
    if _on(env.get("CLAUDE_CODE_USE_FOUNDRY")):
        resource = env.get("ANTHROPIC_FOUNDRY_RESOURCE")
        url = env.get("ANTHROPIC_FOUNDRY_BASE_URL") or (
            resource and f"https://{resource}.services.ai.azure.com/anthropic"
        )
        if not url:
            return ClaudeRoute(
                "blocked",
                problem="Microsoft Foundry is on, with neither ANTHROPIC_FOUNDRY_RESOURCE nor "
                "ANTHROPIC_FOUNDRY_BASE_URL",
            )
        route = through(url, "ANTHROPIC_FOUNDRY_BASE_URL")
        if route.kind == "gateway" and not is_latet(url, config.port, server):
            route.kind, route.summary = "foundry", f"Microsoft Foundry {_host(url)}: Latet goes in front of it"
        return route
    if base := env.get("ANTHROPIC_BASE_URL"):
        parts = _parts(base)
        if parts is None or parts.hostname != "api.anthropic.com":
            return through(base, "ANTHROPIC_BASE_URL")  # not Anthropic's own address, written out
    return ClaudeRoute(
        "anthropic", {"ANTHROPIC_BASE_URL": f"{latet}/anthropic"}, "Anthropic: Latet goes in front of it"
    )


def _latet_signs(env: Mapping[str, str]) -> bool:
    """Whether Latet must sign Bedrock's requests: Claude Code signs them for the address it sends them to,
    unless it has an API key (which passes as it is) or is told not to."""
    return not (env.get("AWS_BEARER_TOKEN_BEDROCK") or _on(env.get("CLAUDE_CODE_SKIP_BEDROCK_AUTH")))


def _bedrock_runtime(url: str) -> tuple[str, bool] | None:
    """The region of Amazon's own address for Bedrock's runtime, written out as it is (https, the standard port,
    no path or query), and whether it is the FIPS one; None for any other address, a look-alike included."""
    parts = _parts(url)
    if parts is None or parts.scheme != "https" or parts.port not in (None, 443):
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    found = _BEDROCK_RUNTIME.fullmatch(parts.hostname or "")
    if found is None or not REGION_NAME.fullmatch(found[2]):
        return None
    return found[2], found[1] is not None


def _bedrock(
    env: Mapping[str, str], latet: str, token: str, session: SessionRegistrar | None, on_server: bool = False
) -> ClaudeRoute:
    """Bedrock straight from Claude Code: its API key passes as it is; otherwise Latet signs, with an
    AWS profile or with the shell's credentials. ``on_server``: the identity's space on a Latet server signs, with
    the shell's credentials `latet claude` hands it, never with an AWS profile (the server's own)."""
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1"  # as Claude Code falls back
    if not REGION_NAME.fullmatch(region):
        return ClaudeRoute("blocked", problem=f"{region} is not an AWS region Latet can route")
    route, where = f"{latet}/bedrock/{region}", f"Amazon Bedrock ({region})"
    if not _latet_signs(env):
        return ClaudeRoute(
            "bedrock", {"ANTHROPIC_BEDROCK_BASE_URL": route}, f"{where}, with its API key: Latet goes in front of it"
        )
    unsigned = {"CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1"}  # Claude Code's signature would be for Latet's address
    if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
        if session is None and on_server:
            return ClaudeRoute(
                "blocked",
                problem="your AWS credentials are only in this shell's variables: start Claude Code with `latet "
                "claude`, which hands this shell's AWS credentials to the Latet server for that session, or use a "
                "Bedrock API key (AWS_BEARER_TOKEN_BEDROCK)",
            )
        if session is None:
            return ClaudeRoute(
                "blocked",
                problem="your AWS credentials are only in this shell's variables, which Latet cannot read once "
                "it closes: name an AWS profile (AWS_PROFILE) to install it for good, or start Claude Code with "
                "`latet claude`",
            )
        credentials = AwsCredentials(
            env["AWS_ACCESS_KEY_ID"],
            env["AWS_SECRET_ACCESS_KEY"],
            env.get("AWS_SESSION_TOKEN") or None,
            credentials_end(env.get("AWS_SESSION_EXPIRATION"), env.get("AWS_CREDENTIAL_EXPIRATION")),
        )
        base = f"{route}/session/{session(credentials)}"
        summary = f"{where}, signed by Latet with this shell's AWS credentials"
        if on_server:
            summary = (
                f"{where}: the Latet server signs with this shell's AWS credentials, which `latet claude` hands it for "
                "this session (kept sealed in its Valkey, ended when Claude Code ends, an hour after their last use at "
                "most); temporary credentials (a role, SSO, aws-vault) or a Bedrock API key keep it from holding "
                "long-lived keys"
            )
        return ClaudeRoute("bedrock", {"ANTHROPIC_BEDROCK_BASE_URL": base, **unsigned}, summary)
    if on_server:
        problem = "with Latet on a server, Latet signs Amazon Bedrock's requests only with the AWS credentials of this "
        problem += "shell (`latet claude`), or passes a Bedrock API key (AWS_BEARER_TOKEN_BEDROCK): "
        return ClaudeRoute("blocked", problem=problem + "never with an AWS profile, which would be the server's")
    profile = env.get("AWS_PROFILE") or "default"
    if not PROFILE_NAME.fullmatch(profile):
        return ClaudeRoute("blocked", problem=f"the AWS profile name {profile!r} has characters Latet cannot route")
    base = f"{route}/profile/{profile}/{profile_tag(token, profile)}"
    return ClaudeRoute(
        "bedrock",
        {"ANTHROPIC_BEDROCK_BASE_URL": base, **unsigned},
        f"{where}, signed by Latet with AWS profile {profile}",
    )


@dataclass
class CodexRoute:
    """Where Codex goes, and the settings that put Latet in front of it."""

    # `-c key=value` for a session: what config.toml and the profile it names set (Codex reads no other file;
    # a `-c` for a provider it does not load makes it refuse to start).
    overrides: dict[str, str] = field(default_factory=dict)
    # For good: each file's own keys, a profile's address in that profile's file.
    edits: dict[str, dict[str, str]] = field(default_factory=dict)
    # Where Codex's own OpenAI provider goes in this session: Latet's address for it, or the one its files write
    # when Latet leaves that as it is (Latet's own, or one on this computer). What a Codex older than 0.115 gets
    # in OPENAI_BASE_URL, the only address it reads.
    openai: str | None = None
    summary: str = ""
    notes: list[str] = field(default_factory=list)
    problem: str | None = None


def codex_texts(codex_home: Path) -> dict[str, str]:
    """Codex's config.toml and its profiles' files (`<name>.config.toml`), by file name."""
    out = {"config.toml": ""}
    for path in [codex_home / "config.toml", *sorted(codex_home.glob("*.config.toml"))]:
        try:
            out[path.name] = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return out


def destinations_pointed_at(texts: Iterable[str], port: int) -> set[str]:
    """The names of the destinations these settings point at: an address of Latet on this computer
    (127.0.0.1, localhost or [::1], on the daemon's ``port``) followed by ``/<name>``. The name is read whole,
    up to the next ``/``, a quote, a space, ``?`` or ``#`` or the end of the text: ``/acme-2`` is not ``/acme``."""
    hosts = "|".join(re.escape(f"[{host}]" if ":" in host else host) for host in _HERE)
    address = re.compile(rf"""(?<![\w.-])(?:{hosts}):{port}/([^/\s"'`?#]+)""", re.IGNORECASE)
    return {name for text in texts for name in address.findall(text)}


def _codex_addresses(document: Mapping[str, Any]) -> dict[str, str]:
    """The addresses a Codex file writes: OpenAI's (`openai_base_url`) and its providers', by key."""
    out: dict[str, str] = {}
    if isinstance(document.get("openai_base_url"), str) and document["openai_base_url"]:
        out["openai_base_url"] = document["openai_base_url"]
    for provider, table in (document.get("model_providers") or {}).items():
        if isinstance(table, dict) and isinstance(table.get("base_url"), str):
            out[f"model_providers.{provider}.base_url"] = table["base_url"]
    return out


def detect_codex(
    texts: Mapping[str, str],
    config: Config,
    chatgpt: bool,
    destination: Registrar,
    profile: str | None = None,
    session: bool = False,
    address: str | None = None,
) -> CodexRoute:
    """Codex's route: OpenAI's address (or a company's in its place) and its providers'. ``session``: for
    one session (`latet codex`), config.toml with the profile it names laid over it; otherwise for good,
    every file (one may switch to any profile), each address changed in the file that writes it. ``address``:
    Latet's address for agents, `wrappers.base_url()`; on a connected computer, the identity's space, and an address on
    this computer (a proxy such as LiteLLM) is refused, as it would send Codex's requests around the server."""
    latet = address or f"http://127.0.0.1:{config.port}"
    server = address if address is not None and _here(address) is None else None  # the identity's space on a server
    documents: dict[str, dict] = {}
    for name, text in texts.items():
        try:
            documents[name] = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            return CodexRoute(problem=f"Codex's {name} cannot be read ({exc}): fix it, then try again")
    chosen_file = f"{profile}.config.toml" if profile else None
    reads = ["config.toml", *([chosen_file] if chosen_file in documents else [])]
    files = reads if session else list(documents)
    written = {name: _codex_addresses(documents.get(name, {})) for name in files}
    for name, addresses in written.items():
        for key, url in addresses.items():
            if server is not None and this_machine(url) and not is_latet(url, config.port, server):
                return CodexRoute(
                    problem=f"{key} in Codex's {name} sends to {_host(url)}, on this computer: with Latet on a server, "
                    "Codex's requests go through the server; remove it, or `latet disconnect`"
                )
            if not is_latet(url, config.port, server) and not this_machine(url) and (problem := _unroutable(url, key)):
                return CodexRoute(problem=f"in Codex's {name}, {problem}")
    route = CodexRoute()
    default = f"{latet}/backend-api/codex" if chatgpt else f"{latet}/openai/v1"
    seen: dict[str, str | None] = {}

    def through(key: str, url: str) -> str | None:
        """Where Codex should send what goes to ``url`` (None: leave it as it is)."""
        if key == "openai_base_url" and (parts := _parts(url)) is not None and parts.hostname == "api.openai.com":
            return default  # OpenAI's own address, written out
        if url not in seen:
            if is_latet(url, config.port, server):
                seen[url] = None
            elif this_machine(url) and not _declared(url, config):
                route.notes.append(local_note(url))
                seen[url] = None
            else:
                seen[url] = f"{latet}/{destination(url)}"
        return seen[url]

    def routed(addresses: Mapping[str, str], top: bool) -> dict[str, str]:
        out = {"openai_base_url": default} if top and "openai_base_url" not in addresses else {}  # OpenAI's
        out.update({key: value for key, url in addresses.items() if (value := through(key, url)) is not None})
        return out

    merged: dict[str, str] = {}
    for name in reads:  # the profile's addresses over config.toml's, as Codex lays them
        merged.update(_codex_addresses(documents.get(name, {})))
    route.overrides = routed(merged, top=True)
    route.openai = route.overrides.get("openai_base_url") or merged.get("openai_base_url") or default
    if not session:
        route.edits = {name: routed(addresses, top=name == "config.toml") for name, addresses in written.items()}
    provider = (
        documents.get(chosen_file or "", {}).get("model_provider")
        or documents.get("config.toml", {}).get("model_provider")
        or "openai"
    )
    # An address Latet leaves as it is (one on this computer) is said so, as for Claude Code: not "in front".
    key = f"model_providers.{provider}.base_url"
    if (url := merged.get(key)) is not None:
        local = key not in route.overrides and not is_latet(url, config.port, server)
        said = ", on this computer: left as it is" if local else ": Latet goes in front of it"
        route.summary = f"provider {provider} ({_host(url)}){said}"
    elif provider == "openai" and route.openai and not is_latet(route.openai, config.port, server):
        route.summary = f"{_host(route.openai)}, on this computer: left as it is"
    else:
        route.summary = "OpenAI: Latet goes in front of it"
    return route


# --- the plugin's check, at the start of a Claude Code session ----------------------------------------

_FILES = {
    "project": "this project's .claude/settings.json",
    "local": "this project's .claude/settings.local.json",
    "user": "your settings.json",
    "flag": "the --settings it was started with",
    "policy": "your organization's managed settings",
    "shell": "your shell",
}
_CLOUDS = (
    ("CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_BEDROCK_BASE_URL", "Amazon Bedrock"),
    ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_BASE_URL", "Google Vertex"),
    ("CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_FOUNDRY_BASE_URL", "Microsoft Foundry"),
)
_FIX = "start it with `latet claude`, or `latet agents apply claude` for every session"
_PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
# What a repository's settings never set, though Claude Code lets them: where its requests go, and the
# proxy they go through.
REPOSITORY_NEVER = (*BASE_VARS, *_CLOUD_SWITCHES, "CLAUDE_CODE_USE_MANTLE", "ANTHROPIC_FOUNDRY_RESOURCE", *_PROXIES)
_REPOSITORY = ("project", "local")


@dataclass
class RouteVerdict:
    """Whether a Claude Code session's requests go through Latet."""

    ok: bool
    reason: str | None = None  # why its prompts are stopped
    toast: str | None = None  # said once at its start: nothing is stopped


def check_claude_route(
    facts: Mapping[str, Any], space: str | None = None, *, server_origin: str | None = None
) -> RouteVerdict:
    """The plugin's facts (Claude Code's variables, as it runs with them, and the settings file that
    set each) → whether the session's requests reach Latet. A repository never decides where they go
    nor names Latet's address. With a server's configured origin, its bridge requires the gate's
    canonical space; a missing space never selects local mode. Desktop advice is local only.
    Facts that cannot be read stop the session: nothing says where its requests would go."""
    try:
        if server_origin is not None:
            home, origin = _space_parts(space), _parts(server_origin)
            if home is None or origin is None or _origin(home) != _origin(origin):
                return RouteVerdict(False, "Latet could not establish this server's address for you")
        return _check(facts, space)
    except Exception as exc:  # any failure fails closed
        # By its type: the facts are the client's, and an error's words or its traceback would quote them.
        log.warning("the plugin's facts about a session's route could not be read: %s", type(exc).__name__)
        return RouteVerdict(False, "Latet could not read where Claude Code sends its requests: run `latet doctor`")


def _check(facts: Mapping[str, Any], space: str | None = None) -> RouteVerdict:
    given, sources = facts.get("env") or {}, facts.get("sources") or {}
    if not isinstance(given, Mapping) or not isinstance(sources, Mapping):
        raise TypeError("the facts' env and sources are not tables")
    env = {k: v for k, v in given.items() if isinstance(k, str) and isinstance(v, str)}

    def where(var: str) -> str:
        return _FILES.get(str(sources.get(var, "shell")), "your shell")

    if sources.get("LATET_BRIDGE_URL") in _REPOSITORY:
        reason = f"{where('LATET_BRIDGE_URL')} sets LATET_BRIDGE_URL: Latet never takes its address from a repository"
        return RouteVerdict(False, reason)
    bridge = facts.get("bridge")
    if space is not None:
        if not _is_space(bridge, space):
            return RouteVerdict(False, "LATET_BRIDGE_URL is not this Latet server's address for you")
        port = 0
    elif not (port := _here(bridge)):
        said = _host(bridge) if bridge else "not set"
        return RouteVerdict(False, f"LATET_BRIDGE_URL is {said}, not an address on this computer")
    if (named := next((var for var in REPOSITORY_NEVER if sources.get(var) in _REPOSITORY), None)) is not None:
        to = f" to {_host(env[named])}" if space is None and named in BASE_VARS and named in env else ""
        problem: str | None = (
            f"{where(named)} sets {named}{to}: a repository's settings never decide where Claude Code sends its "
            "requests. Move it to your own settings, or remove it"
        )
        toast = None
    else:
        problem, toast = _route_problem(env, port, where, space)
    trusted = sources.get("CLAUDE_CODE_ENTRYPOINT") not in _REPOSITORY  # a repository could name Claude Desktop
    if (
        space is None
        and problem
        and (facts.get("surface") == "desktop" or (trusted and facts.get("entrypoint") == "claude-desktop"))
    ):
        said = f"{problem}. In Claude Desktop's Code tab the app sends requests itself: Latet masks them, but its "
        return RouteVerdict(True, toast=said + "daemon does not check them")
    return RouteVerdict(problem is None, problem, toast)


def _here(url: object) -> int | None:
    """The port of Latet's own address on this computer (``http://127.0.0.1:<port>``), or None."""
    parts = _parts(url)
    if parts is None or parts.scheme != "http" or parts.hostname not in _HERE or parts.path not in ("", "/"):
        return None
    return parts.port


def _space_parts(url: object) -> SplitResult | None:
    """A canonical HTTPS space, without permissive URL-parser rewrites or extra path segments."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    if any(char.isspace() or 0x7F <= ord(char) <= 0x9F for char in url):
        return None
    parts = _parts(url)
    if (
        parts is None
        or parts.scheme != "https"
        or any(sign in url for sign in "%?#")
        or re.fullmatch(r"/u/[A-Za-z0-9_-]{16,128}/?", parts.path) is None
    ):
        return None
    host = parts.hostname
    assert host is not None
    authority = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        if parts.port == 0:
            return None
        authority += f":{parts.port}"
    if parts.netloc.lower() != authority:
        return None
    return parts


def _is_space(url: object, space: str) -> bool:
    parts, home = _space_parts(url), _space_parts(space)
    return (
        parts is not None
        and home is not None
        and _origin(parts) == _origin(home)
        and parts.path.removesuffix("/") == home.path.removesuffix("/")
    )


def _route_problem(
    env: Mapping[str, str], port: int, where: Callable[[str], str], space: str | None = None
) -> tuple[str | None, str | None]:
    if _on(env.get("CLAUDE_CODE_USE_MANTLE")):
        return "Claude Code uses Amazon Bedrock's Mantle API, which Latet does not read", None
    if len(on := [switch for switch in _CLOUD_SWITCHES if _on(env.get(switch))]) > 1:
        return f"{_both(on)} are on, and Latet cannot tell which one Claude Code uses: keep one", None
    for switch, var, cloud in _CLOUDS:
        if _on(env.get(switch)):
            if url := env.get(var):
                return _address(url, var, port, where, space)
            said = f"Claude Code sends its requests to {cloud} directly ({switch} from {where(switch)}), so Latet "
            return said + f"cannot check them: {_FIX}", None
    if url := env.get("ANTHROPIC_BASE_URL"):
        return _address(url, "ANTHROPIC_BASE_URL", port, where, space)
    return f"Claude Code sends its requests to api.anthropic.com directly, so Latet cannot check them: {_FIX}", None


def _address(
    url: str, var: str, port: int, where: Callable[[str], str], space: str | None = None
) -> tuple[str | None, str | None]:
    if is_latet(url, port, space):
        return None, None
    if space is not None:
        return f"{var} from {where(var)} does not send requests through your Latet server: {_FIX}", None
    if _parts(url) is None:
        said = f"{var} from {where(var)} ({_host(url)}) is not an address Latet can read the way Claude Code does, "
        return said + f"so Latet cannot check where its requests go: {_FIX}", None
    if this_machine(url):
        return None, f"Claude Code sends its requests to {_host(url)}, on this computer: Latet leaves it alone"
    said = f"Claude Code sends its requests to {_host(url)} ({var} from {where(var)}), not through Latet, so "
    return said + f"Latet cannot check them: {_FIX}", None
