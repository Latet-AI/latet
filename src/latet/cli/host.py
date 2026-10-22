"""Pointing agents at a Latet that cannot edit their files (the container image), and telling it
whose identity to mask: ``latet agents print``, ``latet agents merge``, ``latet identity``."""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any

import typer

from latet.cli import admin
from latet.cli.admin import agents_app, say_refusal
from latet.cli.main import app

identity_app = typer.Typer(help="This machine's identity, as Latet masks it.", no_args_is_help=True)
app.add_typer(identity_app, name="identity")


@identity_app.callback()
def _identity_here(ctx: typer.Context) -> None:
    from latet.cli.admin import settings_here

    settings_here("identity", ctx.invoked_subcommand)


@identity_app.command("send")
def identity_send() -> None:
    """Send this computer's detected identity to its existing personal server space. An agent cannot run it."""
    from latet.config import detect_identity
    from latet.doctor.checks import server_refusal
    from latet.server.client import ServerClient, _connection_material
    from latet.server.connection import _prepare_connection

    prepared = None
    try:
        prepared = _prepare_connection()
        if prepared.connection is None:  # C-M8: nothing to retry; there is no server to send it to
            typer.secho(
                "This computer is not connected to a Latet server: `latet connect` sends its identity when it "
                "connects; on this computer, `latet identity show` shows what Latet masks.",
                fg="red",
                err=True,
            )
            raise typer.Exit(2)
        from latet.cli.admin import _yourself

        # R16: the CLI's other call with the computer key, as `latet dashboard`: before any credential is read.
        _yourself("latet identity send", "sends your identity to your space on the server with this computer's key")
        prepared, pem = _connection_material(prepared, key=True)
        connection = prepared.connection
        assert connection is not None
        identity = detect_identity().model_dump(include={"usernames", "hostname", "full_name", "emails"})
        with ServerClient(
            connection.url, prepared.token, connection.fingerprint, key=prepared.key, _prepared=prepared, _pem=pem
        ) as client:
            client.put_identity(identity)
    except typer.Exit:
        raise
    except Exception as exc:
        from latet.server.client import ServerError
        from latet.server.connection import NotConnected, token_refused

        if isinstance(exc, ServerError) and exc.status == 401 and prepared and prepared.connection:  # R7
            typer.secho(f"Identity was not sent: {token_refused(prepared.connection)}.", fg="red", err=True)
            raise typer.Exit(1) from None
        if isinstance(exc, NotConnected):  # its words say what to do (a token or a computer key missing): no retry
            typer.secho(f"Identity was not sent: {server_refusal(exc)}.", fg="red", err=True)
            raise typer.Exit(1) from None
        typer.secho(
            f"Identity was not sent: {server_refusal(exc)}. Retry with `latet identity send`.", fg="red", err=True
        )
        raise typer.Exit(1) from None
    typer.echo("This computer's identity was sent to its server space.")


AGENTS = ("claude", "codex", "opencode")
TOKEN_SHOWN_AS = "<the bridge token: written by `agents merge claude`>"  # noqa: S105 - a placeholder


def _for_the_container(command: str) -> None:
    """Refuse on a computer connected to a Latet server: these commands point agents at a Latet that cannot edit their
    files, and there they would give out the server's address and this computer's token where `latet agents apply`
    checks where they may go, and says so."""
    from latet import wrappers

    if wrappers.connected() is not None:
        typer.secho(
            f"`{command}` points agents at Latet in its container; this computer is connected to a Latet server: "
            "`latet agents apply` routes its agents there.",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)


def _claude_env(token: bool) -> dict[str, str]:
    from latet import wrappers
    from latet.config import load_config

    env = wrappers.claude_env(load_config())
    env["LATET_AUTOSTART"] = "0"  # the container starts it, not the plugin
    if not token:
        env["LATET_BRIDGE_TOKEN"] = TOKEN_SHOWN_AS
    return env


def _codex_url(login: str) -> str:
    from latet import wrappers
    from latet.config import load_config

    return wrappers.codex_base_url(load_config(), chatgpt=login == "chatgpt")


def _opencode_urls() -> dict[str, str]:
    from latet import wrappers
    from latet.config import load_config
    from latet.integrations.merge import OPENCODE_PROVIDERS

    provider = wrappers.opencode_overrides(load_config())["provider"]
    return {name: provider[name]["options"]["baseURL"] for name in OPENCODE_PROVIDERS}


def _identity_command(shell: str) -> str:
    if shell == "powershell":
        return (
            "docker exec latet latet identity set --user-name $env:USERNAME --user-name (Split-Path $HOME -Leaf) "
            "--host-name $env:COMPUTERNAME --full-name (git config --global user.name) "
            "--email (git config --global user.email)"
        )
    return (
        'docker exec latet latet identity set --user-name "$(id -un)" --user-name "$(basename "$HOME")" '
        '--host-name "$(hostname)" --full-name "$(git config --global user.name)" '
        '--email "$(git config --global user.email)"'
    )


def _merge_command(agent: str, path: str, shell: str) -> str:
    if shell == "powershell":
        # PowerShell has no `<` redirection, and 5.1 pipes text in its own encoding: the file is copied in and out,
        # through the data volume, since the container's root is read-only and its /tmp a tmpfs `docker cp` cannot
        # write; the copies are removed afterwards.
        return (
            f"docker cp {path} latet:/data/merge-in; "
            f'docker exec latet sh -c "latet agents merge {agent} < /data/merge-in > /data/merge-out"; '
            f"docker cp latet:/data/merge-out {path}; "
            "docker exec latet rm -f /data/merge-in /data/merge-out"
        )
    return f"docker exec -i latet latet agents merge {agent} < {path} > {path}.new && mv {path}.new {path}"


def _instructions(shell: str, login: str) -> dict[str, Any]:
    home = "$HOME" if shell == "powershell" else "~"  # PowerShell takes / in paths too
    claude, codex = f"{home}/.claude/settings.json", f"{home}/.codex/config.toml"
    opencode = f"{home}/.config/opencode/opencode.json"
    return {
        "claude": {
            "plugin": ["claude plugin marketplace add Latet-AI/latet", "claude plugin install latet@latet"],
            "settings": claude,
            "env": _claude_env(token=False),
            "merge": _merge_command("claude", claude, shell),
            "undo": [
                _merge_command("claude", claude, shell).replace("merge claude", "merge claude --remove"),
                "claude plugin uninstall latet@latet",
            ],
        },
        "codex": {
            "settings": codex,
            "set": {"openai_base_url": _codex_url(login)},
            "merge": _merge_command("codex", codex, shell),
            "undo": [_merge_command("codex", codex, shell).replace("merge codex", "merge codex --remove")],
        },
        "opencode": {
            "settings": opencode,
            "set": {f"provider.{name}.options.baseURL": url for name, url in _opencode_urls().items()},
            "merge": _merge_command("opencode", opencode, shell),
            "undo": [_merge_command("opencode", opencode, shell).replace("merge opencode", "merge opencode --remove")],
        },
        "identity": _identity_command(shell),
    }


@agents_app.command("print")
def agents_print(
    agent: Annotated[str, typer.Option(help="claude, codex, opencode or all.")] = "all",
    shell: Annotated[str, typer.Option(help="The shell on your computer: sh (macOS, Linux) or powershell.")] = "sh",
    codex_login: Annotated[str, typer.Option(help="How Codex signs in: chatgpt or api-key.")] = "chatgpt",
    as_json: Annotated[bool, typer.Option("--json", help="Print the same as JSON.")] = False,
) -> None:
    """How to point each agent at this Latet by hand: for the container image, which cannot edit
    your agents' files. The bridge token is never printed: `agents merge` writes it into the file."""
    _for_the_container("latet agents print")
    if agent not in (*AGENTS, "all") or shell not in ("sh", "powershell") or codex_login not in ("chatgpt", "api-key"):
        raise typer.BadParameter("agent: claude, codex, opencode or all; shell: sh or powershell")
    info = _instructions(shell, codex_login)
    chosen = AGENTS if agent == "all" else (agent,)
    if as_json:
        typer.echo(json.dumps({k: v for k, v in info.items() if k in chosen or k == "identity"}, indent=2))
        return
    if "claude" in chosen:
        claude = info["claude"]
        typer.secho("Claude Code", bold=True)
        typer.echo("  1. Install the plugin, once:")
        for command in claude["plugin"]:
            typer.echo(f"       {command}")
        typer.echo(f'  2. Add these to the "env" block of {claude["settings"]}:')
        for key, value in claude["env"].items():
            typer.echo(f'       "{key}": "{value}",')
        typer.echo("     or let Latet write them, token included (it is never shown):")
        typer.echo(f"       {claude['merge']}")
        typer.echo("  Restart Claude Code. To undo: " + "; ".join(claude["undo"]))
        typer.echo()
    if "codex" in chosen:
        codex = info["codex"]
        typer.secho("Codex", bold=True)
        typer.echo(f'  In {codex["settings"]}:  openai_base_url = "{codex["set"]["openai_base_url"]}"')
        typer.echo(f"  or: {codex['merge']}")
        typer.echo("  (an API key instead of ChatGPT: `--codex-login api-key`). To undo: " + codex["undo"][0])
        typer.echo()
    if "opencode" in chosen:
        opencode = info["opencode"]
        typer.secho("OpenCode", bold=True)
        for key, url in opencode["set"].items():
            typer.echo(f'  {key} = "{url}"')
        typer.echo(f"  in {opencode['settings']}, or: {opencode['merge']}")
        typer.echo()
    typer.secho("Your identity", bold=True)
    typer.echo("  The container cannot see who you are. Tell it once, from your computer:")
    typer.echo(f"    {info['identity']}")


@agents_app.command("merge")
def agents_merge(
    agent: Annotated[str, typer.Argument(help="claude, codex or opencode.")],
    remove: Annotated[bool, typer.Option("--remove", help="Take the Latet settings out instead.")] = False,
    codex_login: Annotated[str, typer.Option(help="How Codex signs in: chatgpt or api-key.")] = "chatgpt",
) -> None:
    """Read an agent's settings file on stdin, write it on stdout with Latet in (or, with --remove,
    out). For the container: the bridge token goes into the file without being shown."""
    from latet.integrations import merge

    _for_the_container("latet agents merge")
    if agent not in AGENTS:
        raise typer.BadParameter("claude, codex or opencode", param_hint="AGENT")
    text = sys.stdin.read()
    if agent == "claude":
        env = _claude_env(token=True)
        out = merge.claude_unmerge(text, env) if remove else merge.claude_merge(text, env)
    elif agent == "codex":
        url = _codex_url(codex_login)
        out = merge.codex_unmerge(text) if remove else merge.codex_merge(text, {"openai_base_url": url})
    else:
        out = merge.opencode_unmerge(text) if remove else merge.opencode_merge(text, _opencode_urls())
    sys.stdout.write(out)


@identity_app.command("show")
def identity_show() -> None:
    """The identity Latet masks: your user names, host name and git author."""
    from latet.config import load_config
    from latet.runtime import resolve_identity

    config = load_config()
    identity = resolve_identity(config)
    if identity is None:
        state = "off" if not config.identity.enabled else "not known: `latet identity set`"
        typer.echo(f"identity: {state}")
        return
    typer.echo(f"user names: {', '.join(identity.usernames) or '-'}")
    typer.echo(f"host name:  {identity.hostname or '-'}")
    typer.echo(f"git author: {identity.full_name or '-'} {' '.join(identity.emails)}")


@identity_app.command("set")
def identity_set(
    user_name: Annotated[list[str] | None, typer.Option(help="Your user name; repeat for the user folder.")] = None,
    host_name: Annotated[str | None, typer.Option(help="Your computer's host name.")] = None,
    full_name: Annotated[str | None, typer.Option(help="Your name, as in your git author.")] = None,
    email: Annotated[list[str] | None, typer.Option(help="Your email, as in your git author; repeatable.")] = None,
) -> None:
    """Tell Latet whose identity to mask (in the container it cannot see it), then apply it."""
    from latet.config import IdentityConfig, load_config, save_config

    config = load_config()
    config.identity = IdentityConfig(
        enabled=True,
        usernames=list(dict.fromkeys(u for u in user_name or [] if u.strip())),
        hostname=host_name or None,
        full_name=full_name or None,
        emails=[e for e in email or [] if e.strip()],
    )
    typer.echo(f"saved {save_config(config)}")
    answer = admin._reload_answer()  # through the module, as tests replace it there
    if answer is None:
        typer.echo("Applies when the daemon starts.")
    elif "error" in answer:
        say_refusal(answer)
        raise typer.Exit(1)
    elif answer.get("applied", True):
        typer.echo("Rules reloaded.")
    else:
        raise typer.Exit(3)
