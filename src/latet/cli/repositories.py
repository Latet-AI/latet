"""``latet repositories``: the repositories this computer remembers, and a way out of one that stays unreadable.

A change of your settings is weighed in each repository Latet has seen with a latet.yaml of its own. One that cannot be
read now (a share that does not answer, a refused permission) holds back every change that could lower its protection
(``projects.RepositoriesUnchecked``). ``latet repositories`` says which, by state and a fixed reason, never what a file
holds; ``latet repositories forget <folder>`` takes one out of the list, the running daemon's memory too. Forgetting
lowers protection (that repository is weighed again only once a conversation reaches it), so it is yours alone to do: it
refuses an agent.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from latet.cli.main import app

repositories_app = typer.Typer(
    help="The repositories Latet remembers, each with its state; forget one that stays unreadable.",
    invoke_without_command=True,
)
app.add_typer(repositories_app, name="repositories")

_WHY_UNAVAILABLE = (
    "One that is unavailable holds back every change that could lower its protection. "
    "`latet repositories forget <folder>` takes it out of the list: it is weighed again when a conversation reaches it."
)


def _seen() -> Any:
    from latet.config import data_dir
    from latet.projects import SeenRepositories
    from latet.state.local import LocalRepositories

    return SeenRepositories(LocalRepositories(data_dir()))


def _way_out_of_the_list() -> str:
    """What to do when the list itself cannot be read (a folder, a link, a file larger than the list may be, in its
    place): R25 N4."""
    from latet.config import data_dir
    from latet.state.local import REPOSITORIES_FILE

    where = _printable(str(data_dir() / REPOSITORIES_FILE))
    return (
        f"If it stays so, move {where} aside at your own terminal: Latet then remembers repositories again as "
        "conversations reach them."
    )


def _daemon_runs() -> bool:
    """Whether the daemon's PID file names a process that is alive: a daemon that did not answer may still be there,
    with what it remembers in its memory (R25 N1)."""
    import os

    from latet.config import data_dir
    from latet.service.daemon import PID_FILE, wait_exit

    try:
        pid = int((data_dir() / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return not wait_exit(pid, timeout=0)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process: there, so not known to be gone
    return True


def _printable(text: str) -> str:
    from latet.server.client import printable

    return printable(text)


@repositories_app.callback()
def repositories(ctx: typer.Context) -> None:
    """List the repositories Latet remembers, the most recent first, each read now: read, nothing to weigh (and why),
    or unavailable (and why). Never what a file holds."""
    from latet.cli.admin import _admin, settings_here

    settings_here("repositories", ctx.invoked_subcommand)
    if ctx.invoked_subcommand is not None:
        return
    answer = _admin("GET", "/_latet/admin/repositories")
    if answer is None or "error" in answer or not isinstance(answer.get("repositories"), list):
        from latet.projects import repository_states

        answer = repository_states(_seen())  # no daemon answered: the list and the files read here, the same way
    if answer.get("list") != "readable":
        typer.secho(
            "The list of the repositories Latet has seen cannot be read now: a change that could lower protection in a "
            "repository neither applies nor waits until it can. " + _way_out_of_the_list(),
            fg="yellow",
        )
    entries = [entry for entry in answer["repositories"] if isinstance(entry, dict)]
    if not entries:
        typer.echo("Latet remembers no repository.")
        return
    typer.echo("The repositories Latet remembers, the most recent first:")
    unavailable = False
    for entry in entries:
        state, why = str(entry.get("state", "")), str(entry.get("why", ""))
        unavailable = unavailable or state == "unavailable"
        said = f"{state}: {why}" if why else state
        typer.secho(f"  {_printable(str(entry.get('folder', '')))}  {_printable(said)}",
                    fg="yellow" if state == "unavailable" else None)  # fmt: skip
    if unavailable:
        typer.echo(_WHY_UNAVAILABLE)


@repositories_app.command("forget")
def forget(
    folder: Annotated[str, typer.Argument(help="The folder, as `latet repositories` shows it.")],
) -> None:
    """Take a repository out of the list, the daemon's memory too: no change is weighed in it until a conversation
    reaches it again. It lowers protection: run it yourself, in a terminal of your own."""
    from latet.cli import agent
    from latet.cli.admin import _admin, _yourself

    why = "lowers protection (that repository is weighed again only when a conversation reaches it)"
    _yourself("latet repositories forget", why)
    shown = _printable(folder)
    # As approving a change at your terminal does (R25 N2): a terminal of your own, and your answer to the question;
    # never a script's or an agent's run, whatever markers it keeps.
    if not agent.at_your_terminal():
        typer.secho(
            f"`latet repositories forget` {why}: run it yourself, in a terminal of your own.", fg="red", err=True
        )
        raise typer.Exit(2)
    if not typer.confirm(f"Forget {shown}? No change is weighed in it until a conversation reaches it.", default=False):
        typer.echo("Not forgotten: nothing changed.")
        raise typer.Exit(1)
    answer = _admin("POST", "/_latet/admin/repositories/forget", {"folder": folder})
    if answer is None:
        if _daemon_runs():  # it runs, and did not answer: its memory would write the repository back
            typer.secho(
                "The daemon did not answer: nothing changed; try again, or after `latet stop`.", fg="red", err=True
            )
            raise typer.Exit(1)
        outcome = _seen().forget(folder)  # no daemon: the list itself, under its lock
    elif answer.get("forgotten") is True:
        outcome = "forgotten"
    elif answer.get("status") == 404:
        outcome = "unknown"
    elif answer.get("status") == 401:
        typer.secho(
            "Another daemon answered (one of another data folder: it did not take this one's token): nothing changed.",
            fg="red",
            err=True,
        )
        raise typer.Exit(1)
    else:
        outcome = "unreadable"
    if outcome == "unknown":
        typer.secho(f"{shown} is not one Latet remembers: `latet repositories` lists them.", fg="red", err=True)
        raise typer.Exit(1)
    if outcome == "unreadable":
        typer.secho(
            "The list of the repositories Latet has seen cannot be read or written now: nothing changed. "
            + _way_out_of_the_list(),
            fg="red",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"Forgotten: {shown}. It is weighed again when a conversation reaches it.")
