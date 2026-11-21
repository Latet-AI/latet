"""`latet policy check` (a policy file checked before it is set on a server) and `latet server policy` (a Latet
server's policy, through its administration API). Latet Enterprise, locked rules: the Enterprise Edition is a
server's, so a computer has no policy of its own to show. E1's latet.ee.cli registers both, beside `latet server
license`, and lends its way of calling the administration API and of saying what a server answered
(escaped: a terminal acts on what it is shown)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from latet.ee.policy import Floor, PolicyError, read_policy
from latet.ee.policy_sources import local_time
from latet.transfer import KEYS

policy_app = typer.Typer(
    help="A policy file, checked before it is set on your Latet server (Latet Enterprise).",
    no_args_is_help=True,
)
server_policy_app = typer.Typer(
    help="This server's policy: the settings no person or API key goes below (Latet Enterprise).",
    no_args_is_help=True,
)

_EXCEPTIONS = {"personal": "each person may add their own", "company": "only the company's count"}
_NOT_A_POLICY = "the server's answer is not a policy's"


def _said(text: object) -> str:
    """What a file or the server said, as a terminal may show it: what it would act on escaped."""
    from latet.ee.cli import _said as said

    return said(str(text))


def _fail(message: str, code: int = 2) -> NoReturn:
    typer.secho(_said(message), fg="red", err=True)
    raise typer.Exit(code)


def _keys(keys: Iterable[str]) -> str:
    chosen = set(keys)
    return ", ".join(key for key in KEYS if key in chosen) or "nothing"


def _echo(text: str, *, color: str | None = None, bold: bool = False) -> None:
    typer.secho(_said(text), fg=color, bold=bold)


@policy_app.command("check")
def policy_check(
    file: Annotated[Path, typer.Argument(help="The policy file.")],
) -> None:
    """Check a policy file before it is set on a server with `latet server policy set`: what it locks, what it leaves
    to each person, whose exceptions count, and what in it is not read.

    Exit code 0 when all is well, 1 when something in it is not read, 2 when it cannot be read at all.
    """
    _, floor = _load(file, "")
    _echo(f"{floor.origin}: read")
    _echo(f"It locks: {_keys(floor.keys)}")
    if floor.brought:
        _echo(f"Its profile locks too: {_keys(floor.brought)}")
    _echo(f"It leaves to each person: {', '.join(key for key in KEYS if key not in floor.locked) or 'nothing'}")
    _echo(f"Exceptions: {_EXCEPTIONS[floor.exceptions]}")
    for notice in floor.notices:
        _echo(f"Not read in it: {notice}", color="yellow")
    raise typer.Exit(1 if floor.notices else 0)


def _admin(method: str, path: str, body: Any = None, *, refused: str) -> Any:
    """E1's call of the administration API (``path`` below /_latet/server/v1/admin/): a refusal, or a server that cannot
    be reached or named, said in its words, escaped, and exit 2."""
    from latet.ee.cli import _admin as admin

    return admin(method, path, body, refused=refused)


def _load(file: Path, refused: str) -> tuple[str, Floor]:
    """The policy file's text and what it holds, one reading for `check` and `set` (``read_policy``), before anything
    is sent: a regular file of a rules file's size at most, text in the encoding its byte order mark names (Notepad's),
    and a policy that can be read. ``refused`` begins what is said when it cannot be (exit 2)."""
    try:
        return read_policy(file)
    except PolicyError as exc:
        _fail(f"{refused}{exc}")


def _strings(value: object) -> list[str] | None:
    """``value`` when it is a list of texts; else None: an answer that says it wrongly says nothing."""
    return list(value) if isinstance(value, list) and all(isinstance(each, str) for each in value) else None


UNCOMPARED = "replaces a policy of the server's that cannot be read here, so nothing can be compared"


def _loosened(held: object, floor: Floor) -> list[str]:
    """What ``floor`` gives up of the policy the server holds (``held``: its answer to a GET), in words that follow
    "This file": a setting it no longer locks, whose exceptions count going from the company's to each person's.
    A policy stored but not readable here, or an answer that is not a policy's, cannot be compared: that is said too,
    so that a person decides what they cannot see. Nothing for a server that has no policy and nothing wrong."""
    if not isinstance(held, dict):
        return [UNCOMPARED]
    stored = held.get("policy")
    if stored is None:
        problems = _strings(held.get("problems"))
        return [UNCOMPARED] if problems is None or problems else []
    old = _strings(stored.get("keys")) if isinstance(stored, dict) else None
    if old is None:
        return [UNCOMPARED]
    out = []
    dropped = [key for key in old if key not in floor.keys]
    if dropped:
        out.append(
            f"no longer locks: {', '.join(sorted(dropped, key=lambda key: KEYS.index(key) if key in KEYS else 99))}"
        )
    if stored.get("exceptions") == "company" and floor.exceptions == "personal":
        out.append("lets each person add their own exceptions again (the company's alone count now)")
    return out


TRIES = 3  # changes refused because the server's policy changed meanwhile, each read again, before the command stops
_CHANGED_MEANWHILE = object()  # a change's answer when the server's policy changed since the GET it names
_CHANGED_SINCE = "the server's policy was changed since this command read it"


def _replacing(held: object) -> dict[str, Any]:
    """What a change names of the policy it replaces or removes: the etag the server's GET answered (null: none
    stored), so that the server refuses it (409) when another policy was set meanwhile, and judges it (400 for one that
    is no etag); nothing when the answer names none: a server of an earlier release, which checks none."""
    return {"etag": held["etag"]} if isinstance(held, dict) and "etag" in held else {}


def _change(method: str, body: dict[str, Any] | None, refused: str) -> Any:
    """The PUT that sets the file, or the DELETE that removes the policy: the server's answer, or
    ``_CHANGED_MEANWHILE`` when it says its policy changed since the GET the body names (409, in its words); any other
    refusal, or a server not reached, said as ``_admin`` says it after ``refused``, exit 2."""
    from latet.cli import server as server_cli
    from latet.server.client import ServerError
    from latet.server.connection import NotConnected

    try:
        return server_cli.admin_call(method, "ee/policy", body)
    except ServerError as exc:
        from latet.ee.policy_routes import CHANGED

        if exc.status == 409 and exc.message == CHANGED:
            return _CHANGED_MEANWHILE
        _fail(f"{refused}: {exc.message}.")
    except NotConnected as exc:
        _fail(f"{refused}: {exc.message}.")


def _agreed(looser: list[str], changed: bool, yes: bool) -> None:
    """Before the PUT: asked, at your own terminal, when the file gives up something of the server's policy
    (``looser``), or when that policy was changed since this command read it (``changed``: the file replaces someone's
    change); with ``yes`` said, and the set goes on; declined, exit 1; not at your terminal, exit 2."""
    from latet.cli.agent import at_your_terminal

    if not looser and not changed:
        return
    summary = "; ".join(looser)
    if changed:
        what = _CHANGED_SINCE
        if looser:
            what += f", and this file {summary}"
        question, refusal = f"{what[0].upper()}{what[1:]}. Set this file in its place?", "run it again"
    else:
        what = f"this file {summary}"
        question, refusal = f"This file {summary}. Set it?", "run it"
    if yes:
        _echo(f"{what[0].upper()}{what[1:]}.", color="yellow")
        return
    if not at_your_terminal():
        _fail(f"Not changed: {what}: {refusal} at your own terminal, or add --yes.")
    if not typer.confirm(_said(question), default=False):
        raise typer.Exit(1)


@server_policy_app.command("set")
def server_policy_set(
    file: Annotated[Path, typer.Argument(help="The policy file to set.")],
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Set it without asking when something in it is not read, it locks fewer settings, it lets each "
            "person add exceptions again, it replaces a policy that cannot be read here, or the server's policy was "
            "changed while it ran.",
        ),
    ] = False,
) -> None:
    """Make FILE this server's policy: every person and API key, on every instance, is raised to it at once.

    It is checked here first, as `latet policy check` does: a file that cannot be read changes nothing, and what in it
    is not read (a setting with a mistake is left out whole: one malformed term leaves out every term) is said, and
    asked about, before anything is sent. When the server has a policy and FILE locks fewer settings, or lets each
    person add exceptions where only the company's counted, it says so and asks first; so it does when the server's
    policy cannot be read here. It compares which settings are locked and whose exceptions count, not what is in them
    (a term added or removed is not asked about). It sets FILE in place of the policy it compared with: when someone
    changed the server's policy meanwhile, the server refuses it, and it reads that policy again, compares again and
    asks again. The change is recorded in the server's administration log, with the settings it locks and never a
    value.
    """
    from latet.cli.admin import _yourself
    from latet.cli.agent import at_your_terminal

    _yourself("latet server policy set", "changes the protection of every person and API key on the server")
    text, floor = _load(file, "Not changed: ")
    for notice in floor.notices:  # before anything is sent: what is not read is not locked
        _echo(f"Not read in it: {notice}", color="yellow")
    if floor.notices and not yes:
        what = "what is said above is not read in this file, so it is not locked"
        if not at_your_terminal():
            _fail(f"Not changed: {what}: correct it, run it at your own terminal, or add --yes.")
        if not typer.confirm(f"{what[0].upper()}{what[1:]}. Set it all the same?", default=False):
            raise typer.Exit(1)
    body: dict[str, Any] = {"policy": text}
    if floor.notices:  # agreed above: the server sets a policy with keys left out whole only when told so
        body["accept_notices"] = True
    answer: Any = _CHANGED_MEANWHILE
    for tried in range(TRIES):  # the policy it is weighed against, and set in place of (final review L3)
        held = _admin("GET", "ee/policy", refused="Not changed")
        _agreed(_loosened(held, floor), tried > 0, yes)
        if (answer := _change("PUT", {**body, **_replacing(held)}, "Not changed")) is not _CHANGED_MEANWHILE:
            break
    if answer is _CHANGED_MEANWHILE:
        _fail("Not changed: the server's policy kept changing while this command ran: run it again.")
    answer = answer if isinstance(answer, dict) else {}
    keys = _strings(answer.get("keys"))
    said = answer.get("exceptions")
    exceptions = said if isinstance(said, str) and said in _EXCEPTIONS else floor.exceptions
    _echo(f"The server's policy is set: it locks {', '.join(keys) if keys else _keys(floor.keys)}.")
    _echo(f"Exceptions: {_EXCEPTIONS[exceptions]}.")
    for notice in _strings(answer.get("notices")) or []:
        if notice not in floor.notices:  # the server read it otherwise than this Latet (another release): said too
            _echo(f"Not read in it: {notice}", color="yellow")
    problems = _strings(answer.get("problems"))
    for problem in problems or []:
        _echo(problem, color="yellow")
    if problems == []:  # said only when the server says nothing is left undone: not told, or not applied somewhere
        _echo("Every person and API key, on every instance, is raised to it now.")


def _view(stored: object) -> dict[str, Any]:
    """The policy the server answered, checked to be one (ValueError when it is not): its texts, its keys, a time that
    can be shown."""
    if not isinstance(stored, dict):
        raise ValueError(_NOT_A_POLICY)
    exceptions = stored.get("exceptions")
    notices = _strings(stored.get("notices"))
    keys = _strings(stored.get("keys"))
    set_at = stored.get("set_at")
    if (
        not isinstance(stored.get("text"), str)
        or not isinstance(stored.get("set_by"), str)
        or not isinstance(stored.get("in_force"), bool)
        or notices is None
        or keys is None
        or isinstance(set_at, bool)
        or not isinstance(set_at, int | float)
        or not (exceptions is None or isinstance(exceptions, str))
    ):
        raise ValueError(_NOT_A_POLICY)
    try:
        local_time(set_at)
    except (OverflowError, OSError, ValueError):
        raise ValueError(_NOT_A_POLICY) from None
    return stored


@server_policy_app.command("show")
def server_policy_show(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as JSON, without its text.")] = False,
    file: Annotated[Path | None, typer.Option("--file", help="Write its text to this file, to edit it.")] = None,
) -> None:
    """This server's policy: when and by whom it was set, what it sets, whose exceptions count, and whether it is in
    force. Its text holds values (the company's terms): only --file writes it, to a file only you can read."""
    from latet.config import write_atomic

    answer = _admin("GET", "ee/policy", refused="Not shown")
    problems = _strings(answer.get("problems")) if isinstance(answer, dict) else None
    try:
        stored = None if not isinstance(answer, dict) or answer.get("policy") is None else _view(answer["policy"])
    except ValueError:
        stored = None
        problems = None
    if not isinstance(answer, dict) or problems is None:
        _fail(f"Not shown: {_NOT_A_POLICY}.", 1)
    if file is not None:
        if stored is None:
            _fail("Not saved: " + ("the server's policy cannot be read." if problems else "the server has no policy."))
        try:
            write_atomic(file, stored["text"], private=True)
        except OSError as exc:
            _fail(f"Not saved: {file}: {exc.strerror or type(exc).__name__}")
        _echo(f"saved {file}")
        return
    if as_json:
        shown = {key: value for key, value in stored.items() if key != "text"} if stored is not None else None
        # The one line not through `_echo`, which would escape its newlines: JSON escapes what a terminal acts on.
        typer.echo(json.dumps({"policy": shown, "problems": problems}, indent=2))
        return
    for problem in problems:
        _echo(problem, color="yellow")
    if stored is None:
        if problems:  # one is stored: it is not read now
            raise typer.Exit(1)
        _echo("The server has no policy: `latet server policy set <file>` sets one.")
        return
    _echo(f"Set {local_time(stored['set_at'])} by {stored['set_by']}", bold=True)
    _echo(f"  It sets: {', '.join(stored['keys']) or 'nothing'}")
    if brought := _strings(stored.get("brought")):  # a server of another release says none
        _echo(f"  Its profile locks too: {', '.join(brought)}")
    if (exceptions := stored["exceptions"]) is not None:
        _echo(f"  Exceptions: {_EXCEPTIONS.get(exceptions, exceptions)}")
    _echo(f"  In force: {'yes' if stored['in_force'] else 'no'}")
    for notice in stored["notices"]:
        _echo(f"  Not read in it: {notice}", color="yellow")


@server_policy_app.command("remove")
def server_policy_remove(
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Remove it without asking (once you agreed), also when the server's policy was changed while it ran.",
        ),
    ] = False,
) -> None:
    """Remove this server's policy: every person and API key may then go below what it set (a change that lowers
    protection still waits for their approval). It asks first.

    It removes the policy it read before asking: when someone changed the server's policy meanwhile, the server refuses
    the removal, and it reads that policy again and asks again.
    """
    from latet.cli.admin import _yourself
    from latet.cli.agent import at_your_terminal

    _yourself("latet server policy remove")
    if not yes and not at_your_terminal():  # before anything is sent
        _fail("Not removed: run it at your own terminal, or add --yes.")
    answer: Any = _CHANGED_MEANWHILE
    for tried in range(TRIES):  # the policy asked about, and removed: never one set after it was read (final review L3)
        held = _admin("GET", "ee/policy", refused="Not removed")
        if tried and yes:
            _echo(f"{_CHANGED_SINCE[0].upper()}{_CHANGED_SINCE[1:]}.", color="yellow")
        elif not yes:
            first = "Remove the server's policy?"
            again = f"{_CHANGED_SINCE[0].upper()}{_CHANGED_SINCE[1:]}. Remove it all the same?"
            question = f"{again if tried else first} Every person and API key may then go below it."
            if not typer.confirm(_said(question), default=False):
                raise typer.Exit(1)
        if (answer := _change("DELETE", _replacing(held) or None, "Not removed")) is not _CHANGED_MEANWHILE:
            break
    if answer is _CHANGED_MEANWHILE:
        _fail("Not removed: the server's policy kept changing while this command ran: run it again.")
    removed = isinstance(answer, dict) and answer.get("removed") is True
    _echo("The server's policy is removed." if removed else "The server has no policy.")
