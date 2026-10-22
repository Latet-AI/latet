"""``latet connect`` and ``latet disconnect``: this computer's agents masked on a Latet server, in your own space.

Both are the person's own. An agent that runs either is refused: a text it reads could make it send every request of
this computer's agents to a server of someone else's. Each says what it changes and asks at the person's terminal
before anything is sent or written; ``--yes`` takes the question's place only where no terminal is there (a script of
the person's). The one-time code is never needed on the command line, where other users of the computer can read it:
it is asked for, hidden, or read from standard input (``--code -``). What the server answers is printed with its
control characters escaped. Nothing is kept of a connection that failed: not the certificate pinned for it, nor the
token the server issued, which is revoked again when it cannot be kept.

The server gives this computer two secrets: its token, which its agents use (Claude Code holds it in its environment),
and its computer key, which only Latet's own commands use (a sign-in link to the dashboard, the identity sent) and which
is kept in the token's record alone, never in an agent's environment, settings or session files (R16).

``latet disconnect`` leaves no copy of the token, of the computer key or of the space's address in what Latet wrote,
even half-way: it revokes, puts every agent back while the connection is still known (an agent that cannot be put back
is cleaned of the server's values one by one), removes the session files and certificate copies, checks every file,
and forgets the connection only once nothing is left; else it names the file and keeps the connection, so that it can
be run again."""

from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import re
import socket
import stat
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from latet.cli.main import app

if TYPE_CHECKING:
    from latet.server.client import ServerError

CODE = re.compile(r"[A-Za-z0-9_-]{1,128}")  # a one-time code: E0b's are 32 of these, and it reads 128 at most
_CODE_WORDS = "a one-time code is letters, digits, - and _ alone (128 at most), as your administrator gave it"
_CODE_LINE = 1024  # characters of standard input read for `--code -` at most
_WAIT_MOST = 5  # seconds of a Retry-After waited at most before this computer's identity is sent again
_wait = time.sleep  # how that wait is made (tests put their own)
# A server's dashboard shows the identity's names but cannot edit them: connecting again sends them.
_AGAIN = "retry later with `latet identity send`"
_NOTE = "disconnecting.json"  # in the connection's own folder, while `latet disconnect` runs: the agents to apply again
_READ_MOST = 16 * 1024 * 1024  # bytes of a file `latet disconnect` reads at most for what of the server it holds
_CODEX_PROFILE = re.compile(r"latet-[0-9a-f]{16}\.config\.toml")


def _refuse(words: str, code: int = 2) -> NoReturn:
    typer.secho(words, fg="red", err=True)
    raise typer.Exit(code)


def _show(lead: str, rows: list[tuple[str, str]], after: str) -> None:
    """What a command changes, a row each, before it asks."""
    typer.echo(lead)
    typer.echo()
    width = max(len(name) for name, _ in rows)
    for name, what in rows:
        typer.echo(f"  {name:<{width}}  {what}")
    typer.echo()
    typer.echo(after)
    typer.echo()


def _admin_revokes(user: str) -> str:
    """How an administrator revokes a token this computer could not (Task 13's options), the token told by its label:
    this computer's host name, as the server keeps it."""
    from latet.server.client import printable

    label = printable(socket.gethostname() or "a computer", 100)
    return (
        f"an administrator can revoke it, with `latet server people tokens {user} --revoke <token id>` (this "
        f"computer's token is labelled {label}) or `--revoke-all`"
    )


def _revoke(url: str, token: str, fingerprint: str | None) -> str | None:
    """``token`` revoked by the server at ``url``: None when it was, otherwise why not, in the server's words."""
    from latet.server.client import ServerClient, ServerError

    try:
        with ServerClient(url, token, fingerprint) as client:
            client.revoke()
    except ServerError as exc:
        return "server unavailable" if exc.status == 0 else f"server refusal (HTTP {exc.status})"
    return None


_NO_LONGER = (
    "it no longer takes it: revoked already, or its identity disabled, and then the token comes back when the "
    "identity is enabled"
)


def _revoked(url: str, token: str, fingerprint: str | None) -> tuple[bool, str | None]:
    """``token`` revoked by the server at ``url``, for `latet disconnect`: whether it was, and why not. A server that
    refuses it (401) no longer takes it now, but has not said it revoked it (``_NO_LONGER``): revoked already (an
    earlier run that stopped, an administrator), or its identity disabled, which gives the token back once the
    identity is enabled."""
    from latet.server.client import ServerClient, ServerError

    try:
        with ServerClient(url, token, fingerprint) as client:
            client.revoke()
    except ServerError as exc:
        if exc.status == 401:
            return False, _NO_LONGER
        return False, exc.message
    return True, None


def _refresh(said: str) -> bool:
    """The agents applied for good, applied again (to the server, or back to this computer's Latet), each on its own
    and said with ``said``. False, and said, when one of them failed: each failure in its own words, the others
    applied all the same (R8)."""
    from latet.cli.admin import NotAllApplied, refresh_applied
    from latet.logline import described
    from latet.server.connection import NotConnected

    try:
        names = refresh_applied()
    except NotAllApplied as exc:
        for name in exc.done:
            typer.echo(f"{name}: {said}")
        typer.secho(f"Not applied again: {exc}. `latet agents apply` applies them once that is fixed.", fg="yellow")
        return False
    except Exception as exc:  # Latet's record of them cannot be read
        from latet.integrations.base import RecordsUnavailable

        why = exc.message if isinstance(exc, NotConnected) else described(exc)
        if isinstance(exc, RecordsUnavailable):
            why = str(exc)
        typer.secho(
            f"Your agents could not all be applied again ({why}): `latet agents apply` applies them.", fg="yellow"
        )
        return False
    for name in names:
        typer.echo(f"{name}: {said}")
    return True


# --- latet connect ----------------------------------------------------------------------------------------------


def _applied() -> list[tuple[str, str]] | None:
    """The agents applied for good (their id and their name), which follow the connection; None when Latet's record of
    them cannot be read."""
    from latet.integrations.agents import ALL
    from latet.integrations.base import RecordsUnavailable, load_records

    try:
        records = load_records()
    except (OSError, ValueError, TypeError, AttributeError, RecordsUnavailable):
        return None
    return [(agent_id, ALL[agent_id].name) for agent_id in records if agent_id in ALL]


def _claude_held_back() -> str | None:
    """Why Claude Code will not go through the server from this computer, when it will not: outside Windows (R5), or
    on Windows an installation its helper cannot be bound to (T20.9), in words that name what and why."""
    import shutil

    from latet import claude_bridge

    if shutil.which("claude") is None and not any(agent_id == "claude" for agent_id, _ in _applied() or []):
        return None
    if (held := claude_bridge.platform_problem()) is not None:
        return held
    if (problem := claude_bridge.installation_problem()) is not None:
        return f"`latet claude` and its plugin refuse here until this is fixed: {problem}"
    return None


def _claude_cannot_bind() -> str | None:
    """Why `latet connect` stops before the one-time code is spent (R8): Claude Code applied for good, its settings
    able to hold the token, on a Windows installation its helper cannot be bound to. Connected, its plugin would hold
    back every prompt and `latet agents apply claude` could not change that."""
    from latet import claude_bridge
    from latet.integrations.agents import ClaudeCode, _token_kept_out

    applied = any(agent_id == "claude" for agent_id, _ in _applied() or [])
    if claude_bridge.platform_problem() is not None or not applied:
        return None
    if _token_kept_out(ClaudeCode().settings_path()) is not None:
        return None  # no helper binding is made for its settings; `latet claude` says its own refusal
    return claude_bridge.installation_problem()


def _show_connect(address: str, pinned: str | None) -> None:
    from latet.config import detect_identity
    from latet.server.client import printable

    rows = [
        ("Server", address),
        ("Its certificate", f"pinned: {pinned}" if pinned else "checked against this computer's authorities"),
        (
            "This computer",
            "its Latet daemon stops, and its own rules (config.yaml) no longer apply: your settings are the server's, "
            "in your dashboard",
        ),
    ]
    applied = _applied()
    if applied is None:
        rows.append(("Your agents", "Latet's record of them (integrations.json) cannot be read"))
    elif not applied:
        rows.append(
            ("Your agents", "none applied for good; `latet claude`, `latet codex`, `latet opencode` go to the server")
        )
    held = _claude_held_back()
    for agent_id, name in applied or []:
        token = "; its settings will hold this computer's token" if agent_id == "claude" and held is None else ""
        rows.append((name, f"applied for good: every request goes to the server from now on{token}"))
    if held is not None:  # R5: said before the code is spent
        rows.append(("Claude Code", held))
    mine = detect_identity()
    # Each detail escaped: a git author's name is anyone's to write (an agent's among them), and the terminal this is
    # shown on is the one the question is asked at.
    details = [printable(detail) for detail in (*mine.usernames, mine.hostname, mine.full_name, *mine.emails) if detail]
    rows.append(("Your identity", "sent to the server, which masks it: " + (", ".join(details) or "-")))
    rows.append(
        (
            "Token and key",
            "this computer's own, kept in its credential store (or a private file): the token goes to your agents, the "
            "key, which opens your dashboard, never does",
        )
    )
    typer.secho("latet connect", bold=True)
    _show(
        "It connects this computer to a Latet server: your agents' requests are masked there. What it changes:",
        rows,
        "`latet disconnect` undoes it: the token revoked, your agents back to the Latet of this computer.",
    )


def _the_code(given: str | None) -> str:
    """The one-time code: as written after --code, one line of standard input (``-``), or asked for, hidden. Refused
    when it is not one, in words that never repeat it."""
    if given is None:
        said = typer.prompt("One-time code", hide_input=True)
    elif given == "-":
        said = sys.stdin.readline(_CODE_LINE) if sys.stdin is not None else ""
    else:
        said = given
    code = said.strip()
    if not CODE.fullmatch(code):
        _refuse(f"Not connected: {_CODE_WORDS}.")
    return code


def _unpin(pinned: str | None, trusted: bool) -> str | None:
    """The certificate pinned for a connection that failed, forgotten again (one trusted before stays)."""
    from latet.config import data_dir
    from latet.engine.reviewer import certificate as certificates

    if pinned is not None and not trusted:
        try:
            certificates.forget(data_dir(), pinned)
        except BaseException as exc:
            from latet.logline import described

            return described(exc)
    return None


def _waiting(exc: ServerError) -> str:
    """The server's words, with how long to wait when it said so (a 429's Retry-After: E0b's enrolment asks 600 s)."""
    if exc.status != 429 or not exc.retry_after:
        return exc.message
    seconds = exc.retry_after
    count, unit = (math.ceil(seconds / 60), "minute") if seconds >= 60 else (seconds, "second")
    return f"{exc.message}. Try again in {count} {unit}{'s' if count != 1 else ''}"


def _not_kept(exc: BaseException) -> str:
    from latet.logline import described
    from latet.server.connection import NotConnected

    if isinstance(exc, NotConnected):  # its words name the folder and the fix
        return exc.message
    return described(exc)


def _identity_refused(exc: ServerError) -> str:
    if exc.status in (409, 413):  # the identity full, the person's settings refusing it, or too large: its own words
        return f"Your identity did not reach the server: {exc.message}."
    busy = ", which cannot answer now" if exc.status == 503 else ""
    return f"Your identity did not reach the server{busy} ({exc.message}): {_AGAIN}."


def _send_identity(address: str, token: str, key: str, pinned: str | None) -> None:
    """This computer's identity, sent to the server with its token and key, which masks it: asked again once after a
    429's Retry-After (the person's identity being applied); a refusal said, in the server's words."""
    from latet.config import detect_identity
    from latet.server.client import ServerClient, ServerError

    mine = detect_identity().model_dump(include={"usernames", "hostname", "full_name", "emails"})
    with ServerClient(address, token, pinned, key=key) as client:
        try:
            try:
                client.put_identity(mine)
            except ServerError as exc:
                if exc.status != 429 or exc.retry_after is None or exc.retry_after > _WAIT_MOST:
                    raise
                _wait(exc.retry_after)
                client.put_identity(mine)
        except ServerError as exc:
            typer.secho(_identity_refused(exc), fg="yellow")


def _connected(address: str, pinned: str | None, code: str) -> None:
    """The certificate pinned, the code swapped for this computer's token and key, kept with its connection, this
    computer's identity sent, its daemon stopped and its agents applied again. Failed keeps attempt every cleanup
    phase."""
    from latet.config import data_dir
    from latet.engine.reviewer import certificate as certificates
    from latet.logline import described
    from latet.server.client import ServerClient, ServerError, pin_certificate, printable
    from latet.server.connection import (
        Connection,
        connection_path,
        keep_connection,
        remove_server_token,
        token_file_problem,
    )
    from latet.service import daemon

    if (problem := token_file_problem(data_dir())) is not None:  # known now: the code is not spent on a lost token
        _refuse(f"Not connected: {problem}.")
    trusted = pinned is not None and certificates.load(data_dir(), pinned) is not None  # by `latet llm trust`, say
    try:
        if pinned is not None:
            pin_certificate(address, pinned)
        with ServerClient(address, None, pinned) as client:
            answer = client.enrol(code, socket.gethostname() or "a computer")
    except ServerError as exc:
        _unpin(pinned, trusted)
        _refuse(f"Not connected: {_waiting(exc)}.", 1)
    token, key, who = answer["token"], answer["computer_key"], answer["identity"]
    try:
        where = keep_connection(
            Connection(url=address, user=who["id"], user_name=who["name"], fingerprint=pinned),
            token,
            key,
            cleanup=False,
        )
    except BaseException as exc:  # the just-issued PAT remains held here, even when the keep was interrupted
        try:
            refused = _revoke(address, token, pinned)
        except BaseException as cleanup_error:
            refused = f"revocation unavailable ({described(cleanup_error)})"
        try:
            remains = remove_server_token(data_dir())
        except BaseException as cleanup_error:
            remains = [f"local credential storage (cleanup unavailable: {described(cleanup_error)})"]
        try:
            pin_problem = _unpin(pinned, trusted)
        except BaseException as cleanup_error:
            pin_problem = described(cleanup_error)
        typer.secho(
            f"Not connected: this computer's token could not be kept ({printable(_not_kept(exc))}).",
            fg="red",
            err=True,
        )
        if refused is not None:
            typer.echo(f"The server did not revoke it ({refused}): {_admin_revokes(who['id'])}.")
        else:
            typer.echo("The server revoked it again. Ask your administrator for a new code.")
        for place in remains:
            typer.secho(f"A copy of this computer's token remains in {printable(place)}: remove it there.", fg="yellow")
        if pin_problem is not None:
            typer.secho(
                f"The newly selected pin may remain ({pin_problem}): remove it before reconnecting.", fg="yellow"
            )
        if os.path.lexists(connection_path()):
            typer.secho("Connection state may remain: run `latet disconnect` before connecting again.", fg="yellow")
        if not isinstance(exc, Exception):
            raise
        raise typer.Exit(1) from None
    kept = "this computer's credential store" if where == "keyring" else "a private file in Latet's data folder"
    typer.echo(f"Connected to {address} as {printable(who['name'])}; this computer's token and key are in {kept}.")
    _send_identity(address, token, key, pinned)
    if daemon.status().running:  # nothing uses it while this computer is connected
        if daemon.stop():
            typer.echo("The Latet daemon of this computer is stopped: your agents go to the server now.")
        else:
            typer.secho("The Latet daemon of this computer is still running: `latet stop` stops it.", fg="yellow")
    refreshed = _refresh("now through the server")
    _say_still_local()
    typer.echo("Start your agents with `latet claude`, `latet codex` or `latet opencode`, or route them for every")
    typer.echo("session with `latet global-install`. `latet dashboard` opens your dashboard on the server.")
    if not refreshed:
        raise typer.Exit(1)


@app.command()
def connect(
    url: Annotated[str, typer.Argument(help="The server's address, such as https://latet.acme.lan.")],
    fingerprint: Annotated[
        str | None,
        typer.Option(
            help="The fingerprint of the server's self-signed certificate (sha256:… or AB:CD:…), as your administrator "
            "read it on the server's machine."
        ),
    ] = None,
    code: Annotated[
        str | None,
        typer.Option(
            help="The one-time code an administrator gave you: `--code -` reads it from standard input; not given, it "
            "is asked for, hidden. Written here, other users of this computer can read it while the command runs."
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Connect without asking, where no terminal is there (a script of yours)."),
    ] = False,
) -> None:
    """Connect this computer to a Latet server: your agents' requests are masked there, in your own space.

    It shows what changes and asks first, at your own terminal; an agent cannot run it. The code an administrator gave
    you works once, within 24 hours: it is asked for, hidden, or read from standard input with `--code -`. This
    computer gets a personal token, which your agents use, and a computer key, which only Latet's own commands use for
    your dashboard and identity, both kept in its credential store (or a private file); the server gets this
    computer's identity (user name, host name, git author) to mask it. From then on `latet claude`, `latet codex`,
    `latet opencode`, `latet run` and what `latet agents apply` set up go to the server, and your settings are there,
    in your dashboard (`latet dashboard`). `latet disconnect` undoes it.
    """
    from latet.cli.admin import _yourself

    _yourself("latet connect", "sends every request of your agents to the server it is given")
    from latet.cli.agent import at_your_terminal
    from latet.config import data_dir
    from latet.server.connection import (
        NotConnected,
        connection_folder_problem,
        fingerprint_form,
        load_connection,
        server_url,
        token_copies,
    )

    try:
        current = load_connection()
    except NotConnected as exc:  # a connection file that is not one: `latet disconnect` forgets it
        _refuse(f"Not connected: {exc.message}.")
    if current is not None:
        _refuse(f"This computer is connected to {current.url} already: `latet disconnect` first.")
    # The folders a connection would be kept in, admitted before anything of a credential is read (no connection file
    # is admitted by nothing else, R1): it could not be read back otherwise. Nothing sent, nothing kept.
    if (problem := connection_folder_problem()) is not None:
        _refuse(f"Not connected: {problem}.")
    kept, unread = token_copies(data_dir())  # the keyring's copy whatever LATET_KEYRING says
    if kept:  # a token is never put in the place of another one unrevoked
        _refuse(f"This computer still keeps a token for {kept[0].url}: `latet disconnect` first, which revokes it.")
    if unread is not None:
        _refuse(f"Not connected: {unread}; `latet disconnect` removes that copy.")
    try:
        address = server_url(url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="URL") from None
    try:
        pinned = fingerprint_form(fingerprint) if fingerprint else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="'--fingerprint'") from None
    if code not in (None, "-") and not CODE.fullmatch(code.strip()):
        raise typer.BadParameter(_CODE_WORDS, param_hint="'--code'")
    asked = at_your_terminal()  # at a terminal of yours it asks, --yes or not
    if not asked and not yes:
        _refuse(
            "Nothing changed: run it in a terminal of your own, or, in a script of yours, add --yes and give the code "
            "with --code -."
        )
    if not asked and code is None:
        _refuse("Nothing changed: with no terminal to ask for the code at, give it on standard input with --code -.")
    if (problem := _claude_cannot_bind()) is not None:  # known now: the code is not spent on a computer held back
        _refuse(
            f"Not connected: Claude Code is applied for good on this computer, and connected its plugin would hold "
            f"back every prompt: {problem}. Fix that, or `latet agents revert claude` first. Nothing was sent."
        )
    _show_connect(address, pinned)
    if asked and not typer.confirm(f"Connect this computer to {address}?", default=False):
        typer.echo("Nothing changed.")
        raise typer.Exit(1)
    _connected(address, pinned, _the_code(code))


def _say_still_local() -> None:
    """What still points at this computer's Latet once it is connected (X-M3): this shell's variables and the agents'
    own files that name its address. Nothing answers there while this computer is connected, and on a shared computer
    another account could take the port and receive what is sent to it."""
    from latet.config import load_config
    from latet.integrations.agents import ClaudeCode, Codex, OpenCode

    port = load_config().port
    here = (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")
    found = [f"${name}" for name, value in sorted(os.environ.items()) if name.endswith(("_BASE_URL", "_API_BASE"))
             and any(address in value for address in here)]  # fmt: skip
    opencode = OpenCode().config_path().parent
    files = (
        ClaudeCode().settings_path(),
        Codex().config_path(),
        opencode / "opencode.json",
        opencode / "opencode.jsonc",
    )
    for path in files:
        with contextlib.suppress(OSError, ValueError):
            raw = _read_regular(path)
            if raw is not None and any(address.encode() in raw for address in here):
                found.append(str(path))
    lead = f"Still pointed at this computer's Latet (http://127.0.0.1:{port}), which nothing answers while connected"
    if found:
        typer.secho(
            f"{lead}: {', '.join(found)}. Point them at the server: `latet agents apply`, `latet env`.", fg="yellow"
        )
    typer.echo(
        "A shell profile that keeps what `latet env` printed before still points at this computer: run `latet env` "
        "again there, or start programs with `latet run`."
    )


# --- latet disconnect -------------------------------------------------------------------------------------------


@app.command()
def disconnect(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Disconnect without asking, where no terminal is there (a script of yours)."),
    ] = False,
) -> None:
    """Disconnect this computer from its Latet server: its token is revoked and forgotten, and your agents come back to
    the Latet of this computer.

    It shows what changes back and asks first, at your own terminal; an agent cannot run it. A server that does not
    answer is forgotten here all the same, and it says how an administrator revokes this computer's token there. It
    leaves no copy of the token or of your space's address in what Latet wrote: a file it cannot clean is named, and
    the computer stays connected until it can, so that it can be run again.
    """
    from latet.cli.admin import _yourself

    _yourself("latet disconnect", "changes where every request of your agents is masked")
    from latet.cli.agent import at_your_terminal
    from latet.config import data_dir
    from latet.logline import described
    from latet.server.client import printable
    from latet.server.connection import (
        NotConnected,
        connection_dir,
        connection_folder_problem,
        connection_path,
        forget_connection,
        load_connection,
        remove_server_token,
        token_copies,
    )

    try:  # read past the folder check: forgetting a connection kept where others may change it is the way out of it
        connection, unreadable = load_connection(unchecked=True), False
    except NotConnected:  # forgotten all the same: its token is revoked with its own record
        connection, unreadable = None, True
    copies, unread = token_copies(data_dir())  # the keyring's copy whatever LATET_KEYRING says, as it is removed
    noted = os.path.lexists(connection_dir() / _NOTE)
    if connection is None and not unreadable and not copies and unread is None and not noted:
        # Nothing of a connection here: none was ever made, or the last disconnect finished (R1, no admission).
        typer.echo("This computer is not connected to a Latet server.")
        return
    # A folder others may change (T5.7) is trusted for nothing past the connection: no record, backup or note of it.
    unsafe = connection_folder_problem()
    earlier = _note() if unsafe is None else {}  # a run that stopped: what it kept (never read from an unsafe folder)
    servers: list[str] = [connection.url] if connection else []
    named = ", ".join(dict.fromkeys([*servers, *(kept.url for kept in copies)])) or "its Latet server"
    rows = [
        ("Token", "revoked on the server, and forgotten on this computer"),
        (
            "Your agents",
            "back to the Latet of this computer: the server's settings and your rules there no longer apply, this "
            "computer's config.yaml does",
        ),
    ]
    if unreadable:
        rows.append(("Connection", f"{connection_path()} cannot be read: it is forgotten all the same"))
    typer.secho("latet disconnect", bold=True)
    whose = f" ({printable(connection.user_name)})" if connection else ""
    _show(
        f"It disconnects this computer from {named}{whose}. What changes back:",
        rows,
        "`latet connect` connects it again, with a new code from an administrator.",
    )
    asked = at_your_terminal()  # at a terminal of yours it asks, --yes or not
    if not asked and not yes:
        _refuse("Nothing changed: run it in a terminal of your own, or add --yes in a script of yours.")
    if asked and not typer.confirm(f"Disconnect this computer from {named}?", default=False):
        typer.echo("Nothing changed.")
        raise typer.Exit(1)

    # 1. The token revoked, each copy with its own record's server.
    revoked = True
    any_revoked = False
    before = _revoked_before(earlier)  # the copies an earlier run that stopped revoked, each by its digest (C-M3)
    revoked_now: set[str] = set()
    for kept in {copy.token: copy for copy in copies}.values():
        done, why = _revoked(kept.url, kept.token, kept.fingerprint)
        if not done and why == _NO_LONGER and _digest(kept.token) in before:  # that copy, revoked by an earlier run
            typer.echo(f"{kept.url} revoked this computer's token in an earlier run of `latet disconnect`.")
            done = True
        if done:
            revoked_now.add(_digest(kept.token))
        any_revoked |= done
        if not done:
            revoked = False
            said = f"{kept.url} did not revoke this computer's token ({why}): {_admin_revokes(kept.user)}."
            typer.secho(said, fg="yellow")
    if unread is not None:
        revoked = False
        if unsafe is not None:
            unread = "the token file in a data folder others may change was not read"
        typer.secho(f"{unread[:1].upper()}{unread[1:]}: that copy is removed all the same, unrevoked.", fg="yellow")
    if connection is not None and not any(kept.is_for(connection) for kept in copies):
        revoked = False
        said = f"{connection.url} was not asked to revoke this computer's token, which is not here to send"
        typer.secho(f"{said}: {_admin_revokes(connection.user)}.", fg="yellow")
    # 2. Every agent put back while the connection is still known; one that cannot be is cleaned value by value.
    from latet.server.tokens import route_key

    urls = list(dict.fromkeys([*([connection.url] if connection else []), *(kept.url for kept in copies)]))
    tokens = list(dict.fromkeys(kept.token for kept in copies))
    if unsafe is not None:
        from latet.integrations.agents import ClaudeCode

        with contextlib.suppress(OSError, ValueError):
            raw = _read_regular(ClaudeCode().settings_path()) or b""
            tokens += [match.decode() for match in _A_TOKEN_BYTES.findall(raw)]
    routes = [route_key(token) for token in tokens]  # what no file holds once this computer is disconnected, with them
    # This server's values alone (C-M5): its token and the route key of its space when a copy was read; else any
    # personal token, and the spaces at this server's address (a person's own API-key space elsewhere is theirs).
    spaces = [] if tokens else [f"{url}/u/" for url in urls]
    cleaned = [*tokens, *routes, *spaces, str(connection_dir())]  # nor an agent's value
    if unsafe is None and (early := _backups_holding(tokens, routes, spaces)):
        for path, what in early:  # a backup the revert leaves: found before anything is put back (C-I6)
            typer.secho(f"{path} still holds {what}: remove it there, then `latet disconnect` again.", fg="yellow")
        _agents_to_apply_again(revoked=revoked_now | before)
        typer.secho(
            "Nothing was put back: this computer stays connected, and your agents go to the server until "
            "`latet disconnect` finishes.",
            fg="red",
        )
        raise typer.Exit(1)
    if unsafe is None:
        again = _agents_to_apply_again(revoked=revoked_now | before)
        failed = _put_back(cleaned)
    else:  # nothing put back from records others may have written: the agents' own files cleaned, value by value
        again, failed = {}, _clean_every_agent(cleaned)
        typer.secho(
            "Use a new data folder of your own (LATET_DATA_DIR), then `latet agents apply`. Remove the old data "
            "folder once you have the new one; Latet deletes nothing inside its connection and claude-settings "
            "folders.",
            fg="yellow",
        )
        typer.secho(
            f"Retained directories: {printable(str(data_dir() / 'claude-settings'))}, "
            f"{printable(str(connection_dir()))}.",
            fg="yellow",
        )
    # 3. What Latet wrote for the connection on its own: removed, or cleaned of it.
    from latet.integrations.base import RecordsUnavailable

    try:
        _forget_files(cleaned, unsafe=unsafe is not None)
    except RecordsUnavailable as exc:
        _refuse(f"{exc} The connection is retained; clean the old data folder explicitly. {_UNTIL}", 1)
    # 4. Nothing of the server left, or the connection is kept so that this can be run again.
    if left := _still_holding(tokens, routes, spaces, unsafe=unsafe is not None):
        for path, what in left:
            typer.secho(f"{path} still holds {what}: remove it there, then `latet disconnect` again.", fg="yellow")
        typer.secho(
            f"The connection is kept until then. Use a new data folder of your own and remove the old one. {_UNTIL}"
            if unsafe is not None
            else "This computer stays connected until then. Your agents are put back as they were, and "
            f"`latet disconnect` applies them again to this computer's Latet once it finishes. {_UNTIL}",
            fg="red",
        )
        raise typer.Exit(1)
    # 5. Only then the token's record and the connection forgotten.
    remains = remove_server_token(data_dir())
    try:
        forget_connection()
    except OSError as exc:
        _refuse(
            f"{connection_path()} could not be removed ({exc.strerror or described(exc)}): remove it, then `latet "
            "disconnect` again. Your agents are back to their own settings and not applied again: until it finishes, "
            "they go to their providers without Latet.",
            1,
        )
    # 6. The agents applied again, each on its own, to this computer's Latet; one that cannot be stays without Latet.
    applied = _apply_again(again)
    if unsafe is None:
        _forget_note()  # only now: a run stopped before here takes the agents up again
    if unsafe is not None:
        typer.secho(
            "Latet's data folder is open to others: nothing of it was trusted. Your agents' files were "
            "cleaned of the server's values, none was put back from Latet's records there, and none was applied "
            "again: they now go to their providers without Latet. Use a new data folder of your own "
            "(LATET_DATA_DIR), then `latet agents apply`. Remove the old data folder once you have the new one.",
            fg="yellow",
        )
    typer.echo(
        "Sessions running now keep this computer's token in their environment until they end, and your agents' own "
        "logs and transcripts, and a shell profile that kept what `latet env` printed, may hold the address of your "
        "space."
    )
    if not revoked:
        typer.secho(
            "The server did not revoke the token in every copy: "
            + (
                "no token was revoked in this run; an unrevoked copy may remain valid until an administrator "
                "revokes it."
                if not any_revoked
                else "a copy not revoked may remain valid until an administrator revokes it."
            ),
            fg="yellow",
        )
    for place in remains:
        typer.secho(f"A copy of this computer's token remains in {place}: remove it there.", fg="yellow")
    typer.echo(f"Disconnected from {named}.")
    if remains or failed or not applied:
        raise typer.Exit(1)


# A personal token of a Latet server, by its shape (`latet_pat_` and 43 more): known even when no copy of this
# computer's token can be read, and held by no agent's file or Latet's of a computer that is not connected.
_A_TOKEN = re.compile(r"latet_pat_[A-Za-z0-9_-]{43}")
_A_TOKEN_BYTES = re.compile(_A_TOKEN.pattern.encode())
# A computer key, by its shape (`latet_ck_` and 43 more): Latet writes it in the token's record alone, which is
# removed with the token, so that any other file of Latet's or of an agent's that holds one holds a copy (R16).
_A_KEY = re.compile(r"latet_ck_[A-Za-z0-9_-]{43}")
_A_KEY_BYTES = re.compile(_A_KEY.pattern.encode())
_A_SPACE = re.compile(r"/u/([a-z2-7]{26})(?![A-Za-z0-9_-])")


def _has_space(value: str) -> bool:
    from latet.server.tokens import is_route_key

    return any(is_route_key(match[1]) for match in _A_SPACE.finditer(value))


def _holds(value: str, needles: list[str], anywhere: bool = False) -> bool:
    """Whether ``value`` holds one of this connection's values: one of ``needles`` (its token and the route key of its
    space, or the spaces at this server's address when no copy of the token could be read), or a personal token by its
    shape (Latet writes no other's). The address of a space elsewhere counts only ``anywhere`` (a data folder others
    may change: nothing of its connection is trusted) or when neither its route nor this server's address is known: a
    person's own API-key space on another server is theirs (C-M5)."""
    from latet.server.tokens import is_route_key

    if any(needle in value for needle in needles) or _A_TOKEN.search(value) or _A_KEY.search(value):
        return True
    known = any(needle.endswith("/u/") or is_route_key(needle) for needle in needles)
    return (anywhere or not known) and _has_space(value)


_UNTIL = "Until `latet disconnect` finishes, your agents go to their providers without Latet."


def _note() -> dict[str, Any]:
    """What a run of `latet disconnect` that stopped kept in the connection's own folder; {} when none can be read."""
    from latet.server.connection import connection_dir

    with contextlib.suppress(OSError, ValueError, RecursionError):
        kept = json.loads((connection_dir() / _NOTE).read_text("utf-8"))
        return kept if isinstance(kept, dict) else {}
    return {}


def _digest(token: str) -> str:
    """A copy of this computer's token as the note names it: its SHA-256, which says nothing of the token."""
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _revoked_before(note: dict[str, Any]) -> set[str]:
    """The copies of the token a run that stopped revoked, by their digests (``_digest``); none when it names none."""
    kept = note.get("revoked")
    if not isinstance(kept, list):
        return set()
    return {item for item in kept if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)}


def _agents_to_apply_again(*, revoked: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """The agents applied for good, with their choices, to apply again once the connection is forgotten: those a run of
    `latet disconnect` that stopped kept (their ids alone, with Latet's choices), and those applied now (with theirs,
    from Latet's records). Their ids are kept in the connection's own folder before any is put back, so that a run that
    stops knows them; nothing else is kept there, nothing that would go into an agent's settings. Called only when the
    data folder is the person's alone."""
    from latet.config import write_atomic
    from latet.integrations.agents import ALL, ClaudeCode, ClaudeOptions
    from latet.server.connection import connection_dir
    from latet.storage.folders import make_private_folder

    again: dict[str, dict[str, Any]] = {}
    note = connection_dir() / _NOTE
    with contextlib.suppress(OSError, ValueError, RecursionError, AttributeError, TypeError):
        kept = json.loads(note.read_text("utf-8")).get("agents") or []
        again = {agent_id: {} for agent_id in kept if isinstance(agent_id, str) and agent_id in ALL}
    for agent_id in _records() or {}:
        agent = ALL.get(agent_id)
        if isinstance(agent, ClaudeCode):
            again[agent_id] = asdict(agent.applied_options() or ClaudeOptions())
        elif agent is not None:
            again[agent_id] = {}
    make_private_folder(note.parent)
    # Which copies a run revoked, by digest: a later run told 401 for one of them says so, rather than that nothing
    # was revoked; another copy is still said as not revoked (C-M3).
    write_atomic(note, json.dumps({"agents": sorted(again), "revoked": sorted(revoked or ())}), private=True)
    return again


def _forget_note() -> None:
    """The connection's own folder gone, once the agents are applied again: its note, and the folder when it is empty
    and a real one (never through a link)."""
    from latet.server.connection import connection_dir

    folder = connection_dir()
    if _real_folder(folder):
        note = folder / _NOTE
        with contextlib.suppress(OSError):
            if note.is_file() and not note.is_symlink():
                note.unlink()
        helper = folder / "claude-helper"  # the helper's folders, empty once its descriptors are gone (C-M6)
        empty = [helper / "homes" / name for name in ("user", "codex", "claude", "xdg", "roaming", "local")]
        for child in (*empty, helper / "homes", helper / "cwd", helper / "temp", helper):
            if _real_folder(child):
                with contextlib.suppress(OSError):
                    child.rmdir()  # refused while anything is left in it
        with contextlib.suppress(OSError):
            folder.rmdir()  # refused while anything is left in it


def _real_folder(folder: Path) -> bool:
    """Whether ``folder`` is one, and neither a link nor a junction (what is found in it is in it)."""
    try:
        return folder.is_dir() and not folder.is_symlink() and not folder.is_junction()
    except OSError:
        return False


def _clean_every_agent(cleaned: list[str]) -> list[str]:
    """Every agent's own files cleaned of the server's values, value by value, nothing put back from Latet's records
    (they are in a folder others may change): the agents whose files were changed, said."""
    from latet.integrations.agents import ALL

    changed_agents = []
    for agent in ALL.values():
        if changed := _clean(agent, cleaned, None, anywhere=True):
            changed_agents.append(agent.name)
            typer.secho(f"{agent.name}: the server's values were taken out of {', '.join(changed)}.", fg="yellow")
    return changed_agents


def _records() -> dict[str, Any] | None:
    """Latet's records of what it applied; None when they cannot be read."""
    from latet.integrations.base import RecordsUnavailable, load_records

    try:
        return dict(load_records())
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, RecordsUnavailable):
        return None


def _put_back(cleaned: list[str]) -> list[str]:
    """Every agent that has a record put back as it was, each on its own; one whose revert fails cleaned of the
    server's values one by one (``_clean``), and said, naming its files. When Latet's records cannot be read, every
    agent's files are cleaned that way. The names of the agents that could not be put back."""
    from latet.integrations.agents import ALL
    from latet.logline import described

    records = _records()
    failed = []
    for agent_id, agent in ALL.items():
        if records is None:
            if changed := _clean(agent, cleaned, None):
                failed.append(agent.name)
                said = f"{agent.name} could not be put back as it was (Latet's record of what it applied cannot be "
                typer.secho(f"{said}read): the server's values were taken out of {', '.join(changed)}.", fg="yellow")
            continue
        if agent_id not in records:
            continue
        try:
            agent.revert()
        except Exception as exc:  # its own tool gone, a file of its own that cannot be read: cleaned all the same
            changed = _clean(agent, cleaned, records.get(agent_id))
            failed.append(agent.name)
            said = f"{agent.name} could not be put back as it was ({described(exc)}): "
            if changed:
                typer.secho(f"{said}the server's values were taken out of {', '.join(changed)}.", fg="yellow")
            else:
                typer.secho(f"{said}nothing of the server's was found to take out of its files.", fg="yellow")
    return failed


def _clean(agent: Any, cleaned: list[str], record: Any, *, anywhere: bool = False) -> list[str]:
    """``agent``'s own files cleaned of the server's values: each value that holds this computer's token, the route key
    or the address of its space, or the connection's certificate files, removed, the person's own value put back where
    the record kept one. The files changed."""
    from latet.integrations import jsonc
    from latet.integrations.agents import ClaudeCode, Codex, OpenCode
    from latet.integrations.merge import OPENCODE_PROVIDERS

    data = getattr(record, "data", None) or {}
    changed: list[str] = []
    if isinstance(agent, ClaudeCode):
        path = agent.settings_path()
        previous = data.get("previous") or {}
        with contextlib.suppress(OSError, ValueError, RecursionError, AttributeError, TypeError):
            settings = json.loads(path.read_text("utf-8"))
            env = settings.get("env") or {}
            hit = [key for key, value in env.items() if isinstance(value, str) and _holds(value, cleaned, anywhere)]
            for key in hit:
                env.pop(key)
                if isinstance(previous.get(key), str) and not _holds(previous[key], cleaned, anywhere):
                    env[key] = previous[key]
            if hit:
                path.write_bytes((json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
                changed.append(str(path))
    elif isinstance(agent, Codex):
        import tomlkit

        home, edits = agent.config_path().parent, data.get("edits") or {}
        for path in [home / "config.toml", *sorted(home.glob("*.config.toml"))]:
            with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, KeyError):
                document = tomlkit.parse(path.read_text("utf-8"))
                previous = (edits.get(path.name) or {}).get("previous") or {}
                places = [(document, "openai_base_url", "openai_base_url")]
                for name, table in (document.get("model_providers") or {}).items():
                    places.append((table, "base_url", f"model_providers.{name}.base_url"))
                hit = False
                for table, key, written in places:
                    value = table.get(key) if hasattr(table, "get") else None
                    if isinstance(value, str) and _holds(value, cleaned, anywhere):
                        del table[key]
                        hit = True
                        if isinstance(previous.get(written), str) and not _holds(previous[written], cleaned, anywhere):
                            table[key] = previous[written]
                if hit:
                    path.write_bytes(tomlkit.dumps(document).encode("utf-8"))
                    changed.append(str(path))
    elif isinstance(agent, OpenCode):
        path = agent.config_path()
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            text = before = path.read_text("utf-8")
            providers = jsonc.loads(text).get("provider") or {}
            for provider in OPENCODE_PROVIDERS:
                url = ((providers.get(provider) or {}).get("options") or {}).get("baseURL")
                if isinstance(url, str) and _holds(url, cleaned, anywhere):
                    text = jsonc.remove_path(text, ["provider", provider, "options", "baseURL"])
            if text != before:
                path.write_bytes(text.encode("utf-8"))
                changed.append(str(path))
    return changed


def _forget_files(cleaned: list[str], *, unsafe: bool = False) -> None:
    """What Latet wrote for the connection on its own, removed: `latet claude`'s session settings files (a crashed
    session leaves one, the token in it), `latet codex`'s profile files, the certificate copies given to the agents;
    and the space's address taken out of Latet's record of what it applied."""
    from latet import claude_bridge
    from latet.config import data_dir, write_atomic
    from latet.integrations.agents import Codex
    from latet.integrations.base import load_records, manifest_path, records_preflight
    from latet.server.connection import connection_dir

    if not unsafe:
        records_preflight()
        claude_bridge._remove_owned_bindings(data_dir())
    places = (
        (data_dir() / "claude-settings", re.compile(r"[^/\\]+\.json")),
        (connection_dir(), re.compile(r"[^/\\]+\.pem")),
        (Codex().config_path().parent, _CODEX_PROFILE),  # Latet's per-session files: Codex's own profiles are yours
    )
    for folder, name in places:
        if unsafe and folder in (data_dir() / "claude-settings", connection_dir()):
            continue  # another account could swap either folder after a check: delete nothing inside it
        if not _real_folder(folder):  # checked before anything is listed: nothing is removed through a link
            continue
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for path in entries:
            with contextlib.suppress(OSError):
                if name.fullmatch(path.name) and path.is_file() and not path.is_symlink():
                    path.unlink()
    if unsafe:
        _quarantine_records(unsafe=True)
        return
    records = {key: asdict(record) for key, record in load_records().items()}
    kept = _without(records, cleaned)
    if kept != records:
        records_preflight(write=True)
        write_atomic(manifest_path(), json.dumps(kept, indent=2), private=True)


def _quarantine_records(*, unsafe: bool = False) -> list[Path]:
    """Set aside untrusted undo records under names no integration command reads."""
    from latet.config import data_dir
    from latet.integrations.base import RecordsUnavailable, _metadata, _trusted_folder, manifest_path

    candidates = [manifest_path(), data_dir() / "backups"]
    unavailable = (
        "Untrusted integration records retained: quarantine unavailable or destination already exists. "
        "Manually set aside the retained records, use a new private data folder (LATET_DATA_DIR), "
        "then explicitly reapply."
    )
    if os.name == "nt":
        if unsafe:  # A known-unsafe namespace must never become trusted through a later pathname check.
            typer.secho(unavailable, fg="yellow")
            return candidates
        try:
            if _metadata(data_dir()) is None:
                return []
            _trusted_folder(data_dir())  # Standing-namespace refusal only, not an anchored rename.
        except RecordsUnavailable:
            typer.secho(unavailable, fg="yellow")
            return candidates
    retained = []
    for path in candidates:
        if path.name.endswith(".untrusted"):
            continue
        try:
            if os.name == "nt":  # Windows rename refuses an existing destination atomically.
                if _metadata(path) is None:
                    continue
                path.rename(path.with_name(path.name + ".untrusted"))
            else:
                if not os.path.lexists(path):
                    continue
                from latet.integrations.quarantine import quarantine_record

                quarantine_record(path.parent, path.name)
        except FileNotFoundError:
            continue
        except (OSError, RecordsUnavailable):
            retained.append(path)
            typer.secho(unavailable, fg="yellow")
    return retained


def _without(value: Any, cleaned: list[str]) -> Any:
    """``value`` (a record, as JSON) without any text that holds the server's values."""
    if isinstance(value, dict):
        return {k: _without(v, cleaned) for k, v in value.items() if not (isinstance(v, str) and _holds(v, cleaned))}
    if isinstance(value, list):
        return [_without(v, cleaned) for v in value if not (isinstance(v, str) and _holds(v, cleaned))]
    return value


def _still_holding(
    tokens: list[str], routes: list[str], spaces: list[str], *, unsafe: bool = False
) -> list[tuple[Path, str]]:
    """Each file of Latet's or of an agent's that still holds this computer's token, the route key of its space or its
    address (``spaces``: the server's address and ``/u/``, known even when no copy of the token is), and which: the
    agents' settings, `latet claude`'s session files, the connection's own folder, Latet's record of what it applied,
    and the backups `agents apply` made. A file that cannot be read is named too: nothing says it holds none of them.
    Only plain files are read (a pipe planted there would hold the read). On an unsafe data folder the session and
    certificate directories are never traversed: a session directory still present conservatively keeps the
    connection."""
    from latet.config import data_dir
    from latet.integrations.agents import ClaudeCode, Codex, OpenCode
    from latet.integrations.base import (
        RecordsUnavailable,
        _metadata,
        _trusted_folder,
        checked_backup,
        manifest_path,
        records_preflight,
    )
    from latet.server.connection import connection_dir

    if not unsafe:
        records_preflight()
    home, opencode = Codex().config_path().parent, OpenCode().config_path().parent
    files = [ClaudeCode().settings_path(), home / "config.toml", *home.glob("*.config.toml")]
    files += [opencode / "opencode.json", opencode / "opencode.jsonc"]
    if not unsafe:
        files.append(manifest_path())
    sessions = data_dir() / "claude-settings"
    folders = [] if unsafe else [sessions, connection_dir()]
    for folder in folders:
        with contextlib.suppress(OSError):
            files += [path for path in folder.rglob("*") if path.is_file()]
    left: list[tuple[Path, str]] = []
    backup_files: set[Path] = set()
    if not unsafe:
        backups = data_dir() / "backups"
        pending = [backups]
        while pending:
            folder = pending.pop()
            try:
                if folder == backups and _metadata(folder) is None:
                    continue
                _trusted_folder(folder)
                for path in folder.iterdir():
                    found = _metadata(path)
                    if found is not None and stat.S_ISDIR(found.st_mode):
                        pending.append(path)
                    else:
                        files.append(path)
                        backup_files.add(path)
            except (RecordsUnavailable, OSError):
                left.append((folder, "integration backup retained and unavailable"))
    if unsafe and os.path.lexists(sessions):
        left.append((sessions, "session settings Latet cannot safely inspect in a data folder others may change"))
    if unsafe:
        for root in (manifest_path(), data_dir() / "backups"):
            try:
                root.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                reason = "integration records retained: metadata unavailable"
            else:
                reason = "integration records retained and unavailable in a data folder others may change"
            left.append((root, reason))
    for path in dict.fromkeys(files):
        try:
            raw = checked_backup(path, most=_READ_MOST) if path in backup_files else _read_regular(path)
        except RecordsUnavailable:
            left.append((path, "integration backup retained and unavailable"))
            continue
        except OSError as exc:
            left.append((path, f"something Latet could not read ({exc.strerror or type(exc).__name__})"))
            continue
        if raw is None:
            continue
        if _holding(raw, tokens, routes, spaces, left, path, anywhere=unsafe):
            continue
    return left


def _holding(
    raw: bytes,
    tokens: list[str],
    routes: list[str],
    spaces: list[str],
    left: list[tuple[Path, str]],
    path: Path,
    *,
    anywhere: bool = False,
) -> bool:
    """Whether ``raw`` holds this connection's token, a computer key or its space's address (``_holds``'s rule), said
    in ``left``."""
    if any(token.encode() in raw for token in tokens) or _A_TOKEN_BYTES.search(raw):
        left.append((path, "this computer's token"))
        return True
    if _A_KEY_BYTES.search(raw):
        left.append((path, "this computer's key"))
        return True
    elsewhere = (anywhere or not (routes or spaces)) and _has_space(raw.decode("utf-8", "replace"))
    if any(secret.encode() in raw for secret in (*routes, *spaces)) or elsewhere:
        left.append((path, "the address of your space on the server"))
        return True
    return False


def _backups_holding(tokens: list[str], routes: list[str], spaces: list[str]) -> list[tuple[Path, str]]:
    """The backups `latet agents apply` made that hold this connection's token or the address of its space, or that
    cannot be checked: a revert leaves them, so they are found before anything is put back (C-I6)."""
    from latet.config import data_dir
    from latet.integrations.base import RecordsUnavailable, _metadata, _trusted_folder, checked_backup

    backups, left = data_dir() / "backups", []
    pending, files = [backups], []
    while pending:
        folder = pending.pop()
        try:
            if folder == backups and _metadata(folder) is None:
                continue
            _trusted_folder(folder)
            for path in folder.iterdir():
                found = _metadata(path)
                (pending if found is not None and stat.S_ISDIR(found.st_mode) else files).append(path)
        except (RecordsUnavailable, OSError):
            left.append((folder, "integration backup retained and unavailable"))
    for path in files:
        try:
            raw = checked_backup(path, most=_READ_MOST)
        except (RecordsUnavailable, OSError):
            left.append((path, "integration backup retained and unavailable"))
            continue
        _holding(raw, tokens, routes, spaces, left, path)
    return left


def _read_regular(path: Path) -> bytes | None:
    """``path``'s bytes, ``_READ_MOST`` at most, when it is a plain file; None when there is none (or it is a pipe or
    device, never read). Raises OSError for a folder, a dangling link or a file that cannot be read."""
    if not os.path.lexists(path):
        return None
    if (path.is_symlink() or path.is_junction()) and not path.exists():
        raise OSError(errno.ENOENT, "a dangling link, which Latet cannot check")
    if path.is_dir():
        raise OSError(errno.EISDIR, "a folder, which Latet cannot read as a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except IsADirectoryError:
        return None
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            return None
        return file.read(_READ_MOST)


def _apply_again(again: dict[str, dict[str, Any]]) -> bool:
    """The agents put back, applied again to this computer's Latet, each on its own: one that cannot be stays with its
    own settings, without Latet, and is said. Whether all were."""
    from latet.config import load_config
    from latet.integrations.agents import ALL, ClaudeCode, ClaudeOptions, RouteProblem
    from latet.logline import described

    every = True
    for agent_id, choices in again.items():
        agent = ALL[agent_id]
        try:
            if isinstance(agent, ClaudeCode):
                known = {k: v for k, v in choices.items() if k in ClaudeOptions.__dataclass_fields__}
                agent.apply(ClaudeOptions(**known), load_config())
            else:
                agent.apply()
        except Exception as exc:  # its own tool gone, a route Latet cannot stand in front of: it stays without Latet
            every = False
            why = str(exc) if isinstance(exc, RouteProblem) else described(exc)
            said = f"{agent.name} is back to its own settings, without Latet ({why}): `latet agents apply {agent_id}` "
            typer.secho(said + "applies it again.", fg="yellow")
            continue
        typer.echo(f"{agent.name}: back to the Latet of this computer")
    return every
