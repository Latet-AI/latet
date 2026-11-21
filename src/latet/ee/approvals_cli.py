"""`latet server approvals show|set` (Latet Enterprise, E5): who decides on what lowers protection on this server,
through its administration API with an administration token (E0c's `admin_call`, which refuses an agent's change).
What the server said is printed escaped (E3's ``_said``); a refusal of the server, and a server that cannot be reached
or named, exit 2."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

import typer

from latet.cli.server import admin_call, server_app
from latet.ee.server_cli import _said
from latet.server.client import ServerError
from latet.server.connection import NotConnected

server_approvals_app = typer.Typer(
    help="Who decides on what lowers protection on this Latet server (Latet Enterprise).", no_args_is_help=True
)
server_app.add_typer(server_approvals_app, name="approvals")
PATH = "ee/approvals/settings"


class Decider(StrEnum):
    """Who decides: whoever's settings they are, or the company's security team."""

    SELF = "self"
    SECURITY = "security"


def _answer(method: str, body: dict[str, Any] | None, refused: str) -> dict[str, Any]:
    try:
        answer = admin_call(method, PATH, body)
    except (ServerError, NotConnected) as exc:
        typer.secho(f"{refused}: {_said(str(exc.message))}.", fg="red", err=True)
        raise typer.Exit(2) from None
    return answer if isinstance(answer, dict) else {}


def _approvals(answer: dict[str, Any]) -> dict[str, Any]:
    approvals = answer.get("approvals")
    return approvals if isinstance(approvals, dict) else {}


def _problems(answer: dict[str, Any]) -> list[str]:
    """What the server says is not right yet (instances of another release alive: the review's L5); an answer that says
    it otherwise than as texts says nothing."""
    said = answer.get("problems")
    return [str(each) for each in said if isinstance(each, str)] if isinstance(said, list) else []


def _print(approvals: dict[str, Any]) -> None:
    if approvals.get("changes") == "security":
        typer.echo("Changes that lower protection: the security team decides, on API keys' too.")
    else:
        typer.echo(
            "Changes that lower protection: whoever's settings they are decides; on an API key's, another person who "
            "controls it, or the admins."
        )
    if approvals.get("live_review") == "security":
        typer.echo("Texts of the Live Reviewer: the security team decides, and sees each text.")
    else:
        typer.echo("Texts of the Live Reviewer: whoever sends them decides.")


@server_approvals_app.command("show")
def server_approvals_show() -> None:
    """Who decides now, on a change that lowers protection and on the Live Reviewer's texts."""
    _print(_approvals(_answer("GET", None, "Not shown")))


@server_approvals_app.command("set")
def server_approvals_set(
    changes: Annotated[
        Decider | None,
        typer.Option(
            help="Who decides on a change that lowers protection: self (whoever's settings they are) or security "
            "(the security team)."
        ),
    ] = None,
    live_review: Annotated[
        Decider | None,
        typer.Option(
            "--live-review",
            help="Who decides on the Live Reviewer's texts: self (whoever sends them) or security (the security "
            "team, who then see each text).",
        ),
    ] = None,
) -> None:
    """Change who decides. Each change is kept in the server's administration log; what waits keeps its route."""
    chosen = {key: value.value for key, value in (("changes", changes), ("live_review", live_review)) if value}
    if not chosen:
        typer.secho("Nothing to change: give --changes, --live-review or both.", fg="red", err=True)
        raise typer.Exit(2)
    answer = _answer("PUT", chosen, "Not changed")
    _print(_approvals(answer))
    for problem in _problems(answer):  # as `latet server policy set` says its own
        typer.secho(_said(problem), fg="yellow")
    if live_review is Decider.SECURITY:  # R46: people learn of it at their first text otherwise
        typer.echo(
            "Tell people first: the security team now sees each text their Live Reviewer holds. A person can turn "
            "their Live Reviewer off: pair this with --changes security, so that turning it off waits for the "
            "security team too and nobody decides alone."
        )
