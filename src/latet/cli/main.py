"""The ``latet`` command line."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from latet import __version__

if TYPE_CHECKING:
    import uvicorn
    from starlette.applications import Starlette

    from latet.server.connection import _PreparedConnection
    from latet.server.operations import Operations

app = typer.Typer(
    name="latet",
    help="A local privacy firewall for AI agents.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    # A traceback shows no frame's locals, a token, a key or a value among them: Typer's default today, written out
    # (older Typer showed them all).
    pretty_exceptions_show_locals=False,
)
_PASS = {"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []}


LOOPBACK = ("127.0.0.1", "::1", "localhost")


def uvicorn_server(
    application: Starlette | Operations,
    host: str,
    port: int,
    log_level: str | None = "info",
    log_config: object = "default",
    *,
    tls: tuple[Path, Path] | None = None,
    forwarded_allow_ips: str | None = None,
    ws_max_size: int = 64 * 1024 * 1024,
    timeout_graceful_shutdown: int = 3,
    access_log: bool = True,
) -> uvicorn.Server:
    """The server `latet serve`, the daemon, the tutorial's sandbox and a Latet server's instance run: the gateway's
    settings for uvicorn, and the application told its server (what a shutdown route stops). ``log_config``: "default"
    is uvicorn's own, None sets no logging up (the caller does), or a logging configuration; ``log_level`` None leaves
    uvicorn's loggers to the caller too, and ``access_log`` False writes no access line.

    For a Latet server (``latet.server.instance.make_instance``): ``tls``, its own certificate and key (TLS may end at
    its load balancer instead); ``forwarded_allow_ips``, the proxies' addresses, the only ones whose X-Forwarded-For
    and X-Forwarded-Proto are believed (uvicorn then reads no FORWARDED_ALLOW_IPS variable, and the settings refuse *:
    anyone could pose as the proxy, pass the administration's HTTPS check over plain http, and forge the address the
    log and the limits see); ``ws_max_size``, the largest WebSocket message once inflated, which the server's body
    limit does not see; ``timeout_graceful_shutdown``, how long a stop waits for what is under way."""
    import uvicorn

    from latet.gateway.cloud import redact_access_log

    config_kwargs: dict[str, Any] = {} if log_config == "default" else {"log_config": log_config}
    if tls is not None:
        config_kwargs.update(ssl_certfile=str(tls[0]), ssl_keyfile=str(tls[1]))
    if forwarded_allow_ips is not None:
        config_kwargs.update(proxy_headers=True, forwarded_allow_ips=forwarded_allow_ips)
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=host,
            port=port,
            log_level=log_level,
            access_log=access_log,
            ws="websockets-sansio",
            ws_max_size=ws_max_size,
            ws_per_message_deflate=True,
            timeout_keep_alive=75,
            timeout_graceful_shutdown=timeout_graceful_shutdown,
            **config_kwargs,
        )
    )
    application.state.server = server
    redact_access_log()  # after uvicorn set its loggers up: a signing route's key never reaches a log
    return server


def run_server(
    port: int | None = None,
    capture: Path | None = None,
    log_level: str = "info",
    passthrough: bool = False,
    log_config: object = "default",
    host: str | None = None,
    background: bool = False,
) -> None:
    from latet.config import data_dir, in_container, load_config

    host = host or os.environ.get("LATET_HOST") or "127.0.0.1"
    if host not in LOOPBACK:
        if not in_container():
            message = f"Latet listens on this machine only; {host} is allowed in its container image."
            typer.secho(message, fg="red", err=True)
            raise SystemExit(2)
        # In the container the published port decides who can connect: 127.0.0.1 only, on the host.
        typer.secho(f"Listening on {host} inside the container: publish the port on 127.0.0.1 only.", err=True)
    from latet.gateway.app import GatewayConfig, create_app
    from latet.runtime import make_runtime
    from latet.service import ports
    from latet.service.daemon import PID_FILE
    from latet.storage.backup import restoring

    if restoring(data_dir()):  # exit 0: a service manager does not restart it in a loop
        typer.secho("Not started: a restore is under way. Finish it, or `latet restore --abort`.", err=True)
        raise SystemExit(0)
    chosen = port or load_config().port
    state = ports.probe(chosen)
    if not state.usable or state.reason == "latet":
        what = "Latet is already running there" if state.reason == "latet" else state.reason
        typer.secho(f"Cannot listen on 127.0.0.1:{chosen}: {what}.", fg="red", err=True)
        # The daemon has nothing to do where Latet runs already (another start's daemon): no failure, which launchd and
        # systemd would start again for good. Windows starts it once, at login, and restarts nothing: as it was there.
        running = background and state.reason == "latet" and os.name != "nt"
        raise SystemExit(0 if running else 1)  # also the windowless daemon's entry point: no typer context there
    runtime = None
    if passthrough:
        gateway = GatewayConfig(port=port or 5283, capture_dir=capture)
    else:
        try:
            runtime = make_runtime(capture=capture)
        except (ValueError, RuntimeError, OSError) as exc:
            # What stops the start, in one line and never a traceback (R24 M2): Latet's own words for settings that
            # cannot be read (they name places, never values), else the error's type alone.
            from latet.config import ConfigError
            from latet.logline import described

            words = str(exc) if isinstance(exc, ConfigError) else described(exc)
            typer.secho(f"Not started: {words}", fg="red", err=True)
            raise SystemExit(1) from None
        gateway = runtime.gateway
        if port:
            gateway.port = port
    server = uvicorn_server(create_app(gateway), host, gateway.port, log_level, log_config)
    from latet.storage.folders import make_private_folder

    pid_path = make_private_folder(data_dir()) / PID_FILE
    pid_path.write_text(str(os.getpid()))
    try:
        server.run()
    finally:
        if pid_path.exists() and pid_path.read_text().strip() == str(os.getpid()):
            pid_path.unlink()
        if runtime is not None:
            runtime.close()


@app.command()
def version() -> None:
    """Print the Latet version."""
    typer.echo(__version__)


@app.command()
def serve(
    port: Annotated[int | None, typer.Option(help="Local port to listen on (127.0.0.1 only).")] = None,
    capture: Annotated[
        Path | None, typer.Option(help="Write raw exchanges (credentials redacted) to this local directory.")
    ] = None,
    log_level: Annotated[str, typer.Option(help="uvicorn log level.")] = "info",
    passthrough: Annotated[bool, typer.Option(help="Forward without masking (debugging only).")] = False,
    host: Annotated[
        str | None, typer.Option(help="The address to listen on: 127.0.0.1, or 0.0.0.0 in the container image.")
    ] = None,
) -> None:
    """Run the gateway in the foreground."""
    run_server(port, capture, log_level, passthrough, host=host)


def _cannot_start(current: object) -> None:
    port = getattr(current, "port", None)
    reason = getattr(current, "reason", None)
    if reason:
        typer.secho(f"Latet cannot listen on 127.0.0.1:{port}: {reason}.", fg="red", err=True)
        typer.echo("Run `latet global-install` to move it to a free port (your agents are updated too).", err=True)
    else:
        from latet.config import data_dir

        logs = data_dir() / "logs"
        exited = getattr(current, "exited", None)
        why = f"it stopped while starting (exit code {exited})" if exited is not None else "it did not answer in time"
        typer.secho(f"Latet did not start: {why}. See daemon.log and daemon.out in {logs}", fg="red", err=True)


@app.command()
def start(
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait until the daemon answers.")] = True,
) -> None:
    """Start the background daemon (no-op when it is already running)."""
    from latet import wrappers
    from latet.server.connection import NotConnected
    from latet.service import daemon

    try:
        connection = wrappers.connected()
    except NotConnected as exc:  # a connection file that is refused: said, never a traceback
        typer.secho(f"Not started: {exc.message}.", fg="red", err=True)
        raise typer.Exit(2) from None
    if connection is not None:  # C-M4: nothing would use it, and status shows the server
        typer.secho(
            f"This computer is connected to {connection.url}: your agents go there, and the Latet daemon of this "
            "computer is not used. `latet status` shows the server; `latet disconnect` brings your agents back here.",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)

    def slow() -> None:
        typer.echo("Latet is still starting (the first start after an install takes longer)...")

    current = daemon.start(wait=15.0 if wait else 0.0, slow=slow)
    if not current.running and (wait or current.reason):
        _cannot_start(current)
        raise typer.Exit(1)
    if current.running:
        typer.echo(f"Latet {current.version} running on 127.0.0.1:{current.port}")
    else:
        typer.echo(f"Latet is starting on 127.0.0.1:{current.port}")


@app.command()
def stop() -> None:
    """Stop the background daemon."""
    from latet.service import daemon

    if daemon.stop():
        typer.echo("Latet stopped.")
    else:
        typer.secho("Latet is still running.", fg="red", err=True)
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show whether the local daemon runs, or the connected server accepts this computer's token."""
    from latet.config import load_config
    from latet.doctor.checks import FAIL, _diagnostic_connection, check_server, server_refusal
    from latet.service import daemon

    try:
        prepared = _diagnostic_connection()
    except Exception as exc:
        typer.secho(f"Connection: {server_refusal(exc)}", fg="red", err=True)
        raise typer.Exit(3) from None
    if prepared.connection is not None:
        checks = check_server(_prepared=prepared)
        for check in checks:
            typer.echo(f"{check.title}: {check.detail}")
        if any(check.status == FAIL for check in checks):
            raise typer.Exit(3)
        return
    current = daemon.status()
    if current.running:
        mode = "masking" if current.masking else "pass-through"
        typer.echo(f"running: Latet {current.version} on 127.0.0.1:{current.port} ({mode}), pid {current.pid}")
        if load_config().live_review.enabled:
            dashboards = f"{current.viewers} dashboard{'s' if current.viewers != 1 else ''} open"
            typer.echo(f"Live Reviewer: on, {current.waiting} waiting for your approval, {dashboards}")
    else:
        typer.echo("not running")
        raise typer.Exit(3)


@app.command()
def env(
    shell: Annotated[
        str | None, typer.Option(help="powershell, bash or cmd (default: this system's usual shell).")
    ] = None,
    agent: Annotated[str, typer.Option(help="generic or claude.")] = "generic",
    key_file: Annotated[Path | None, typer.Option(help="Read an API key for generic clients from this file.")] = None,
    server: Annotated[
        str | None, typer.Option(help="The API key's server (otherwise LATET_SERVER_URL or connection).")
    ] = None,
) -> None:
    """Print the environment variables that route a client through Latet."""
    from latet import wrappers

    shell = shell or ("powershell" if os.name == "nt" else "bash")
    if shell not in ("powershell", "bash", "cmd") or agent not in ("generic", "claude"):
        raise typer.BadParameter("shell: powershell, bash or cmd; agent: generic or claude")
    if key_file is not None:
        from latet.server.connection import NotConnected, read_key, server_target, server_url
        from latet.server.tokens import route_key

        if agent != "generic":
            raise typer.BadParameter("--key-file is for --agent generic only")
        explicit = server or os.environ.get("LATET_SERVER_URL")
        if explicit:
            try:
                address = server_url(explicit)
            except ValueError:
                raise NotConnected("--server or LATET_SERVER_URL must name a valid Latet server address") from None
        else:
            address, _ = server_target()
        values = wrappers.generic_env(base=f"{address}/u/{route_key(read_key(key_file))}")
    else:
        if server is not None:
            raise typer.BadParameter("--server requires --key-file")
        values = wrappers.claude_env() if agent == "claude" else wrappers.generic_env()
    # On a computer connected to a Latet server the token opens the person's space and dashboard from anywhere the
    # server is reached: never printed where a shell's profile would keep it.
    withheld = agent == "claude" and wrappers.connected() is not None
    if withheld:
        values.pop("LATET_BRIDGE_TOKEN", None)
    for key, value in values.items():
        if shell == "powershell":
            typer.echo(f'$env:{key} = "{value}"')
        elif shell == "cmd":
            typer.echo(f"set {key}={value}")
        else:
            typer.echo(f"export {key}='{value}'")
    if withheld:
        typer.echo(
            "This computer's token is not printed: `latet claude`, or `latet agents apply claude`, gives it to Claude "
            "Code.",
            err=True,
        )
    if key_file is not None:
        typer.echo("The route URLs are secrets: keep them out of shared logs and shell history.", err=True)
    elif wrappers.connected() is not None:
        typer.echo(
            "These addresses hold the address of your space on the server, a secret like this computer's token: keep "
            "them out of a shell profile you sync or share.",
            err=True,
        )


def _ensure_daemon(*, _prepared: _PreparedConnection | None = None) -> None:
    from latet.server.connection import _require_selection, load_connection
    from latet.service import daemon

    if _prepared is not None:
        _require_selection(_prepared)
    connection = _prepared.connection if _prepared is not None else load_connection()
    if connection is not None:
        if _prepared is None:
            _ensure_server(connection)
        else:
            _ensure_server(connection, _prepared=_prepared)
        return
    current = daemon.start()
    if not current.running:
        _cannot_start(current)
        raise typer.Exit(1)


def _ensure_server(connection: Any, *, _prepared: _PreparedConnection | None = None) -> None:
    """A computer connected to a Latet server: the server answers, and takes this computer's token."""
    from latet import wrappers
    from latet.server.client import ServerClient, ServerError
    from latet.server.connection import _require_selection

    try:
        token = wrappers._daemon_token(_prepared) if _prepared is not None else wrappers.daemon_token()
        if _prepared is not None:
            _require_selection(_prepared)  # The legacy client TLS reader must still see these selected roots.
        with ServerClient(connection.url, token, connection.fingerprint) as client:
            client.whoami()
    except ServerError as exc:
        if exc.status == 401:  # R7: why, and what to do (`latet doctor` would say no more)
            from latet.server.connection import token_refused

            typer.secho(f"Latet's server {connection.url}: {token_refused(connection)}.", fg="red", err=True)
            raise typer.Exit(1) from None
        what = "refuses this computer's token" if exc.status == 403 else "cannot be reached"
        typer.secho(
            f"Latet's server {connection.url} {what}: {exc.message}. `latet doctor` says more.", fg="red", err=True
        )
        raise typer.Exit(1) from None


@app.command()
def dashboard(
    print_url: Annotated[bool, typer.Option("--print-url", help="Print the login link instead of opening it.")] = False,
) -> None:
    """Open the dashboard here or on the connected server: what reached the model, and how it was masked.

    On a computer connected to a server an agent cannot run it: the sign-in link opens every original masked for you
    and your settings, from wherever the server is reached.
    """
    import json
    import urllib.request
    import webbrowser

    from latet.config import data_dir, load_config
    from latet.doctor.checks import server_refusal
    from latet.server.client import ServerClient, _connection_material
    from latet.server.connection import _prepare_connection
    from latet.service.daemon import local_urlopen
    from latet.storage.keys import load_bridge_token

    try:
        prepared = _prepare_connection()
    except Exception as exc:
        typer.secho(f"Could not reach the dashboard: {server_refusal(exc)}", fg="red", err=True)
        raise typer.Exit(1) from None
    if prepared.connection is not None:  # R16: the agent check the administration CLI makes, before any credential
        from latet.cli.admin import _yourself

        _yourself(
            "latet dashboard",
            "opens your dashboard on the server, where every original masked for you can be revealed and your settings "
            "changed",
        )
    try:
        if prepared.connection is not None:
            prepared, pem = _connection_material(prepared, key=True)
            connection = prepared.connection
            assert connection is not None
            origin = connection.url
            with ServerClient(
                origin, prepared.token, connection.fingerprint, key=prepared.key, _prepared=prepared, _pem=pem
            ) as client:
                url = client.login_code()
        else:
            _ensure_daemon()
            origin = f"http://127.0.0.1:{load_config().port}"
            request = urllib.request.Request(  # noqa: S310 - the local daemon's literal loopback origin above
                f"{origin}/_latet/admin/login-code",
                data=b"{}",
                method="POST",
                headers={"x-latet-token": load_bridge_token(data_dir()), "content-type": "application/json"},
            )
            with local_urlopen(request, timeout=10) as response:
                url = json.loads(response.read())["url"]
    except Exception as exc:
        typer.secho(f"Could not reach the dashboard: {server_refusal(exc)}", fg="red", err=True)
        raise typer.Exit(1) from None
    if print_url:
        typer.echo(url)
        typer.echo("The link works once, for two minutes.", err=True)
    else:
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            typer.secho(
                "Could not open a browser. Run `latet dashboard --print-url` to request a login link.",
                fg="red",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(f"Dashboard opened at {origin}/_latet/ui/")


def _require(command: str) -> None:
    import shutil

    if shutil.which(command) is None:
        typer.secho(f"Cannot find `{command}`: install it, or run Latet where `{command}` runs.", fg="red", err=True)
        raise typer.Exit(127)


def _launch(argv: list[str], env: dict[str, str]) -> int:
    from latet import wrappers

    try:
        return wrappers.run(argv, env)
    except FileNotFoundError:
        typer.secho(f"Cannot find `{argv[0]}`: install it, or run Latet where `{argv[0]}` runs.", fg="red", err=True)
        return 127


def _claude(args: list[str], exe: str = "claude") -> int:
    import os
    import secrets

    from latet import claude_bridge, wrappers
    from latet.config import load_config
    from latet.integrations.agents import ClaudeCode
    from latet.integrations.routes import claude_sources, detect_claude
    from latet.server.connection import _agent_base, _prepare_connection, _prepare_token, _require_selection

    _require(exe)
    prepared = _prepare_token(_prepare_connection())
    _require_selection(prepared)
    if prepared.connection is not None and (held := claude_bridge.platform_problem()) is not None:
        typer.secho(f"Latet: {held}.", fg="red", err=True)  # R5: said up front, nothing started
        return 2
    _ensure_daemon(_prepared=prepared)
    config = load_config()
    if prepared.connection is not None:
        from latet.projects import not_applied_here

        if (note := not_applied_here(Path.cwd())) is not None:  # R9: once, before the session starts
            typer.secho(f"Latet: {note}.", fg="yellow", err=True)
    try:  # on a server the space decides, from its own settings
        asked = config.auto_mode_review.user_name == "surrogate" or prepared.connection is not None
        surrogate = wrappers.user_name_surrogate(config, _prepared=prepared) if asked else None
    except wrappers.DaemonError as exc:
        typer.secho(f"Latet cannot give Claude Code your user name's fake: {exc}.", fg="red", err=True)
        return 2
    sessions: list[str] = []

    def session(credentials) -> str:
        _require_selection(prepared)
        sessions.append(wrappers.aws_session(credentials, config))
        _require_selection(prepared)
        wrappers.hold_aws_session(sessions[-1], config)  # the daemon drops it when this process ends, killed too
        return sessions[-1]

    def destination(url: str) -> str:
        _require_selection(prepared)
        return wrappers.destination_for(url, "claude", config, _prepared=prepared)

    try:
        _require_selection(prepared)
        route = detect_claude(
            claude_sources(os.environ, ClaudeCode().config_dir()),
            config,
            wrappers._daemon_token(prepared),
            destination,
            session,
            address=_agent_base(prepared, config),
        )
    except wrappers.DaemonError as exc:
        typer.secho(f"Latet could not set Claude Code's route: {exc}", fg="red", err=True)
        return 2
    if route.problem:
        typer.secho(f"Latet cannot go in front of Claude Code: {route.problem}.", fg="red", err=True)
        return 2
    if route.note:
        typer.secho(f"Latet: {route.note}", fg="yellow", err=True)
    typer.secho(f"Claude Code: {route.summary}.", dim=True, err=True)
    said: list[str] = []
    env = wrappers.claude_env(config, user_name=surrogate, said=said, _prepared=prepared)
    env = {k: v for k, v in env.items() if k != "ANTHROPIC_BASE_URL"}
    for line in said:
        typer.secho(f"Latet: {line}.", fg="yellow", err=True)
    if (why := wrappers.untrusting("claude", _prepared=prepared)) is not None:
        typer.secho(f"Latet: {why}.", fg="yellow", err=True)
    env.update(route.env)
    binding = claude_bridge._plan_binding(
        prepared, claude_bridge._binding_path(prepared.selected.data, f"session-{secrets.token_hex(16)}.json")
    )
    if binding is not None:
        env[claude_bridge.HELPER_KEY] = binding.setting
    try:
        settings, rest = wrappers.claude_settings_file(env, args, _prepared=prepared)
    except (OSError, ValueError) as exc:
        typer.secho(f"Cannot read your --settings: {exc}", fg="red", err=True)
        return 2
    try:
        if binding is not None:
            claude_bridge._publish_binding(binding)
        _require_selection(prepared)
        return _launch(wrappers.claude_argv(rest, exe, settings), env)
    finally:
        try:
            if binding is not None:
                claude_bridge._remove_binding(binding.path)
        finally:
            settings.unlink(missing_ok=True)
            for one in sessions:
                wrappers.drop_aws_session(one, config)


def _codex(args: list[str], exe: str = "codex") -> int:
    from latet import wrappers
    from latet.config import load_config
    from latet.integrations.agents import Codex
    from latet.integrations.routes import codex_texts, detect_codex

    _require(exe)
    try:  # the profile Codex will read, in each form it takes: its addresses are judged as config.toml's are
        profile = wrappers.codex_profile(args)[0]
        if wrappers.connected() is not None:
            wrappers.check_codex_session(args)
    except wrappers.CodexArgs as exc:
        typer.secho(f"Latet cannot start Codex: {exc}.", fg="red", err=True)
        return 2
    _ensure_daemon()
    config = load_config()
    chatgpt = wrappers.codex_uses_chatgpt(exe)
    home = Codex().config_path().parent
    try:
        route = detect_codex(
            codex_texts(home),
            config,
            chatgpt,
            lambda url: wrappers.destination_for(url, "codex", config),
            profile,
            session=True,
            address=wrappers.base_url(config),
        )
    except wrappers.DaemonError as exc:
        typer.secho(f"Latet could not set Codex's route: {exc}", fg="red", err=True)
        return 2
    if route.problem:
        typer.secho(f"Latet cannot go in front of Codex: {route.problem}.", fg="red", err=True)
        return 2
    for note in route.notes:
        typer.secho(f"Latet: {note}", fg="yellow", err=True)
    profile_file = None  # on a connected computer the space's address is in a profile file, never on a command line
    if wrappers.connected() is not None:
        try:
            profile_file = wrappers.codex_session_profile(route.overrides, profile, home)
        except (OSError, ValueError, wrappers.CodexArgs) as exc:
            typer.secho(f"Latet cannot write Codex's profile file in {home}: {exc}.", fg="red", err=True)
            return 2
    try:
        try:
            argv, extra = wrappers.codex_argv(
                args, route.overrides, exe=exe, openai=route.openai, profile_file=profile_file
            )
        except (wrappers.CodexTooOld, wrappers.CodexArgs) as exc:
            typer.secho(f"Latet cannot go in front of Codex: {exc}.", fg="red", err=True)
            return 2
        said: list[str] = []
        extra.update(wrappers.extra_ca_env("codex", said))
        typer.secho(f"Codex: {route.summary}.", dim=True, err=True)
        for line in said:
            typer.secho(f"Latet: {line}.", fg="yellow", err=True)
        if (why := wrappers.untrusting("codex")) is not None:
            typer.secho(f"Latet: {why}.", fg="yellow", err=True)
        return _launch(argv, extra)
    finally:
        if profile_file is not None:
            profile_file.unlink(missing_ok=True)


def _opencode(args: list[str], exe: str = "opencode") -> int:
    from latet import wrappers

    _require(exe)
    _ensure_daemon()
    said: list[str] = []
    env = wrappers.opencode_env(said=said)
    for line in said:
        typer.secho(f"Latet: {line}.", fg="yellow", err=True)
    if (why := wrappers.untrusting("opencode")) is not None:
        typer.secho(f"Latet: {why}.", fg="yellow", err=True)
    return _launch([exe, *args], env)


# What `latet gemini` tells you when Gemini CLI reaches its model in a way Latet cannot go in front of.
_GEMINI_SIGNED_IN = (
    "Latet cannot go in front of Gemini CLI signed in with Google: that sign-in goes to an endpoint Latet does not "
    "read, so nothing would be masked. Choose an API key or Vertex AI in Gemini CLI (/auth), or set "
    "security.auth.selectedType to gemini-api-key in {settings}"
)
_GEMINI_UNCHOSEN = (
    "Gemini CLI has no way of reaching its model chosen yet: choose {offered} in its dialog. A run with -p needs it "
    "chosen first: set security.auth.selectedType to {offered} in {settings}"
)


def _gemini(args: list[str], exe: str = "gemini") -> int:
    import os

    from latet import wrappers

    _require(exe)
    settings = wrappers.gemini_settings_path()
    chosen, where = wrappers.gemini_choice()
    signed_in = os.environ.get("GOOGLE_GENAI_USE_GCA") == "true"
    if (chosen is not None and chosen not in wrappers.GEMINI_THROUGH_LATET) or (chosen is None and signed_in):
        said_by = f" ({chosen}, chosen in {where})" if chosen is not None else " (GOOGLE_GENAI_USE_GCA)"
        typer.secho(_GEMINI_SIGNED_IN.format(settings=settings) + said_by + ".", fg="red", err=True)
        return 2
    _ensure_daemon()
    said: list[str] = []
    env = wrappers.gemini_env(said=said)
    if chosen is None:  # what its dialog offers first; with a base URL set it would otherwise pick a gateway
        offered = "vertex-ai" if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") == "true" else "gemini-api-key"
        env["GEMINI_DEFAULT_AUTH_TYPE"] = offered
        typer.secho(f"Latet: {_GEMINI_UNCHOSEN.format(offered=offered, settings=settings)}.", fg="yellow", err=True)
    for line in said:
        typer.secho(f"Latet: {line}.", fg="yellow", err=True)
    if (why := wrappers.untrusting("gemini")) is not None:
        typer.secho(f"Latet: {why}.", fg="yellow", err=True)
    return _launch([exe, *args], env)


def _copilot(args: list[str], exe: str = "copilot") -> int:
    import os

    from latet import wrappers
    from latet.config import load_config
    from latet.integrations.routes import COPILOT_SIGNED_IN, copilot_given, detect_copilot

    _require(exe)
    if not copilot_given(os.environ):  # signed in with GitHub: said before a daemon is started for nothing
        typer.secho(f"{COPILOT_SIGNED_IN}.", fg="red", err=True)
        return 2
    _ensure_daemon()
    config = load_config()
    try:
        route = detect_copilot(
            os.environ,
            config,
            lambda url: wrappers.destination_for(url, None, config),
            address=wrappers.base_url(config),
        )
    except wrappers.DaemonError as exc:
        typer.secho(f"Latet could not set Copilot CLI's route: {exc}", fg="red", err=True)
        return 2
    if route.problem:
        typer.secho(f"Latet cannot go in front of Copilot CLI: {route.problem}.", fg="red", err=True)
        return 2
    for note in route.notes:
        typer.secho(f"Latet: {note}", fg="yellow", err=True)
    env = {"COPILOT_PROVIDER_BASE_URL": route.base} if route.base is not None else {}
    said: list[str] = []
    env.update(wrappers.extra_ca_env("copilot", said))
    typer.secho(f"Copilot CLI: {route.summary}.", dim=True, err=True)
    for line in said:
        typer.secho(f"Latet: {line}.", fg="yellow", err=True)
    if (why := wrappers.untrusting("copilot")) is not None:
        typer.secho(f"Latet: {why}.", fg="yellow", err=True)
    return _launch([exe, *args], env)


@app.command(context_settings=_PASS)
def claude(ctx: typer.Context) -> None:
    """Run Claude Code through Latet, for this session only (plugin + verify-only gateway); arguments pass through."""
    raise typer.Exit(_claude(list(ctx.args)))


@app.command(context_settings=_PASS)
def codex(ctx: typer.Context) -> None:
    """Run the Codex CLI through Latet, for this session only; arguments pass through."""
    raise typer.Exit(_codex(list(ctx.args)))


@app.command(context_settings=_PASS)
def opencode(ctx: typer.Context) -> None:
    """Run OpenCode through Latet, for this session only (API-key providers); arguments pass through."""
    raise typer.Exit(_opencode(list(ctx.args)))


@app.command(context_settings=_PASS)
def gemini(ctx: typer.Context) -> None:
    """Run Gemini CLI through Latet, for this session only (an API key or Vertex AI); arguments pass through."""
    raise typer.Exit(_gemini(list(ctx.args)))


@app.command(context_settings=_PASS)
def copilot(ctx: typer.Context) -> None:
    """Run GitHub Copilot CLI through Latet, for this session only (a provider of your own); arguments pass through."""
    raise typer.Exit(_copilot(list(ctx.args)))


_AGENTS = {"claude": _claude, "codex": _codex, "opencode": _opencode, "gemini": _gemini, "copilot": _copilot}


@app.command(context_settings=_PASS)
def run(ctx: typer.Context) -> None:
    """Run a script or tool with OPENAI_BASE_URL, ANTHROPIC_BASE_URL and GOOGLE_GEMINI_BASE_URL pointing at Latet.

    For programs that read those variables, such as the OpenAI, Anthropic and Google GenAI SDKs: their
    requests go through Latet. Any other program runs as usual. `latet run claude`, `codex`, `opencode`,
    `gemini` or `copilot` is `latet claude`, `latet codex`, `latet opencode`, `latet gemini` or `latet copilot`.
    """
    from latet import wrappers

    if not ctx.args:
        typer.secho("usage: latet run <command> [args...]", err=True)
        raise typer.Exit(2)
    command, *args = ctx.args
    agent = wrappers.agent_of(command)
    if agent is not None:
        typer.secho(f"latet run {agent} is latet {agent}.", err=True)
        raise typer.Exit(_AGENTS[agent](args, command))
    _require(command)
    _ensure_daemon()
    raise typer.Exit(_launch([command, *args], wrappers.generic_env()))


def main() -> None:
    from latet.cli import admin  # noqa: F401 - registers the install and maintenance commands

    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                reconfigure(encoding="utf-8", errors="replace")
    app()


if __name__ == "__main__":
    # Run as a script, this file is `__main__`: use the real module, where the other commands register.
    from latet.cli.main import main as entry

    entry()
