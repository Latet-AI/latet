"""``latet reserve-port``: Latet's port reserved on Windows as an administered TCP port exclusion, so that Hyper-V, WSL
and Docker cannot take it when Windows starts; ``--remove`` takes Latet's reservations away
(notes/windows-port-reservation-design.md, "The command")."""

from __future__ import annotations

import os
import sys
from typing import Annotated

import typer
import typer.exceptions

from latet.cli.main import app

_CONFIRMATION = "Windows asks for an administrator's confirmation once."
# netsh may refuse to exclude a port a program listens on (design, step 7): the line run by hand, with Latet stopped.
_BY_HAND = (
    "by hand, with Latet stopped: `latet stop`, then this line in a PowerShell run as administrator, then "
    "`latet start`:"
)
_TABLE_BY_HAND = (
    "`netsh int ipv4 show excludedportrange protocol=tcp` lists the excluded ranges, with a `*` beside those an "
    "administrator added."
)


def _windows() -> bool:
    return os.name == "nt"


def _console() -> bool:
    """Someone there to answer the question: standard input is a terminal."""
    return sys.stdin.isatty()


def _later(said: str, port: int | None) -> list[str]:
    """``said``, then what to run later: ``latet reserve-port``, which does all of it, and, for ``port`` when Latet uses
    it (None: no netsh line), the netsh line to run by hand with Latet stopped."""
    from latet.service import reservation

    first = f"{said} To do it later: `latet reserve-port`, which does all of it."
    if port is None:
        return [first]
    return [first, f"Or {_BY_HAND}", f"  {reservation.manual_command(port)}"]


def offer_lines(port: int, moving_to: int | None) -> list[str]:
    """What is printed when nothing changes (a no, or no console to ask) for Latet's ``port``: what to run later. Where
    Latet would move (``moving_to``) only `latet reserve-port`: a netsh line for a port Latet does not use yet would
    reserve a port nothing moves to."""
    if moving_to is None:
        return _later("Not reserved.", port)
    return _later(f"Not moved to port {moving_to}, and not reserved.", None)


def _problem(error: BaseException) -> str:
    from latet.logline import described

    return str(error) or described(error)


@app.command("reserve-port")
def reserve_port(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Do it without asking: for a script, once you said yes."),
    ] = False,
    offer: Annotated[
        bool,
        typer.Option("--offer", help="Ask only where Windows holds the port or may take it; else say nothing."),
    ] = False,
    remove: Annotated[bool, typer.Option("--remove", help="Take away the ports Latet reserved.")] = False,
) -> None:
    """Reserve Latet's port on Windows, so that Hyper-V, WSL and Docker cannot take it when Windows starts.

    Windows reserves blocks of ports each time it starts, and Latet's port can be among them: Latet
    then cannot start until the next restart. This reserves the port for Latet, with one
    administrator's confirmation. Where Windows holds the port already, Latet moves to a free one and
    reserves that one, and your agents move with it. It asks once before it changes anything.
    `latet reserve-port --remove` takes Latet's reservations away.
    """
    from latet.cli.admin import _yourself

    if remove or not offer:  # the installer's offer, which an agent may run for you, says what you run yourself
        _yourself("latet reserve-port", "changes a setting of Windows")
    if not _windows():
        if not offer:
            typer.echo("Only Windows takes ports for itself when it starts: nothing to reserve here.")
        return
    if remove:
        _remove()
        return
    if not offer:
        _here(yes, offer)
        return
    try:  # at the end of the installer: a failure is one line, never a traceback that makes the install look failed
        _here(yes, offer)
    except (typer.Exit, typer.Abort, typer.exceptions.TyperException):  # the command's own exits, said already
        raise
    except Exception as exc:
        typer.secho(
            f"The offer to reserve Latet's port could not be made ({' '.join(_problem(exc).split())}): `latet "
            "reserve-port` does it later.",
            fg="yellow",
            err=True,
        )
        raise typer.Exit(1) from None


def _here(yes: bool, offer: bool) -> None:
    """The port of this computer's daemon reserved; none when this computer is connected to a Latet server."""
    from latet import wrappers
    from latet.server.connection import NotConnected

    try:
        connection = wrappers.connected()
    except NotConnected as exc:  # a connection file that is refused: said, never a traceback
        typer.secho(f"Not reserved: {exc.message}.", fg="red", err=True)
        raise typer.Exit(2) from None
    if connection is not None:
        if not offer:
            typer.echo(
                f"This computer is connected to {connection.url}: your agents go there, not to a Latet daemon of this "
                "computer, so there is no port here to reserve."
            )
        return
    _reserve(yes, offer)


def _reserve(yes: bool, offer: bool) -> None:
    """Steps 3 to 9 of the design: the finding, the question, then the daemon stopped, the elevated step, the move.
    With ``offer``, an agent that runs it (the installer run for you) is given what you run yourself, as with no
    console: nothing is asked and nothing changes."""
    from latet import config as settings
    from latet.cli.admin import refreshed
    from latet.cli.agent import agent_name
    from latet.service import daemon, ports, reservation

    config = settings.load_config()
    port = config.port
    from_environment = bool(os.environ.get("LATET_PORT"))
    finding = ports.reservation(port)
    if finding == "safe":
        if not offer:
            typer.echo(f"Port {port} is not among the ports Windows may take when it starts: nothing to reserve.")
        return
    if finding == "reserved":
        if not offer:
            typer.echo(f"Port {port} is reserved already: Windows cannot take it.")
        return
    new = port
    if finding == "held":
        if from_environment:
            typer.secho(
                f"Windows holds port {port} since it started, and the port comes from LATET_PORT: Latet does not move "
                "it. Set LATET_PORT to a port Windows does not hold (`netsh int ipv4 show excludedportrange "
                "protocol=tcp` lists the ones it holds).",
                fg="red",
                err=True,
            )
            raise typer.Exit(1)
        try:  # past every administered range but Latet's own: an exclusion of someone else's is never recorded
            new = ports.free_port(port, ours=reservation.recorded()).port
        except ports.NoFreePortError:
            typer.secho(
                f"Windows holds port {port} since it started, and no port above it up to 49151 is free: nothing "
                "changed.",
                fg="red",
                err=True,
            )
            raise typer.Exit(1) from None
    moving = new != port
    # With neither --yes nor a console, or with an agent running the offer, nothing is asked and nothing changes.
    asking = (yes or _console()) and not (offer and agent_name() is not None)
    if moving and asking:
        from latet.cli.admin import _records_trusted

        # The move applies the agents again from their records: records that cannot be used refuse it here, before
        # anything changed, never after the elevated step and config.yaml's save.
        _records_trusted()
    running = daemon.status(port).running
    if moving:
        does = (
            f"moves to port {new}, free, and reserves it"
            if asking
            else f"would move to port {new}, free, and reserve it"
        )
        typer.echo(f"Windows holds port {port} since it started: Latet {does}.")
        question = f"Move Latet to port {new} and reserve it? {_CONFIRMATION}"
    else:
        typer.echo(
            f"Windows reserves ports for Hyper-V, WSL and Docker each time it starts, and port {port}, Latet's, is "
            "among the ports it may take: Latet then cannot start until the next restart."
        )
        question = f"Reserve port {port} for Latet? {_CONFIRMATION}"
    if running and asking:
        typer.echo("Latet restarts for a moment: the agents retry what they send meanwhile.")
    if not asking or not (yes or typer.confirm(question, default=True)):
        for line in offer_lines(port, new if moving else None):
            typer.echo(line)
        return

    # netsh may refuse to exclude a port a program is bound to; a daemon that moves leaves its old port.
    if running and not daemon.stop():
        typer.secho(
            "Latet's daemon did not stop: nothing changed. `latet stop`, then `latet reserve-port` again.",
            fg="red",
            err=True,
        )
        raise typer.Exit(1)
    # Only ports Latet recorded, and never config.yaml's: with LATET_PORT set, config.yaml's may be any of them.
    release = [] if from_environment else [p for p in reservation.recorded() if p not in (port, new)]
    moved = failed = False
    try:
        reserved, done = _reservation(new, release)
        failed = not done
        if moving:  # whatever the dialog's answer: the user said yes to the move, and it lets Latet start now
            if not reserved:
                typer.echo(f"Latet moves to port {new} all the same.")
            config.port = new
            try:
                path = settings.save_config(config)
            except OSError as exc:
                typer.secho(f"config.yaml was not saved ({_problem(exc)}): Latet stays on port {port}.", fg="red")
                failed = True
            else:
                moved = True
                typer.echo(f"saved {path}")
                failed |= not refreshed(f"now on port {new}")
    finally:  # the daemon stopped above runs again whatever happened meanwhile (an error, Ctrl+C in the wait)
        if running or moved:
            failed |= not _started()
    if moved:  # an agent session reads the port when it starts
        typer.echo(f"Agent sessions open now keep calling port {port} until you start them again.")
    if failed:
        raise typer.Exit(1)


def _reservation(port: int, release: list[int]) -> tuple[bool, bool]:
    """The elevated step for ``port``, its outcome said: whether the port is reserved now, and whether all went well
    (a reservation Latet could not record is reserved, and did not go well)."""
    from latet.service import ports, reservation

    try:
        if reservation.reserve(port, release=release):
            typer.echo(f"Port {port} is reserved for Latet: Windows can no longer take it.")
            return True, True
        problem = f"Windows does not list port {port} among its reserved ports afterwards"
    except reservation.ReservedAlready:  # read before anything ran: an exclusion that is not Latet's to record
        typer.echo(
            f"Port {port} is reserved already, by an exclusion not on Latet's record: Windows cannot take it, and "
            "`latet reserve-port --remove` leaves it alone."
        )
        return True, True
    except reservation.ElevationDeclined:
        problem = "the administrator's confirmation was declined"
    except reservation.OutcomeUnknown as exc:  # the elevated step ran; what it did is not known, nothing was recorded
        typer.secho(
            f"Latet could not read Windows' table of excluded ports afterwards ({exc}): whether port {port} is "
            "reserved is not known, and Latet recorded nothing. `latet reserve-port` checks it again.",
            fg="yellow",
        )
        return False, False
    except ports.TableUnreadable as exc:  # read before anything ran: nothing changed
        problem = f"Latet could not read Windows' table of excluded ports ({exc})"
    except OSError as exc:
        problem = _problem(exc)
        ports.excluded_ranges.cache_clear()
        if ports.reservation(port) == "reserved":  # the exclusion is there: its record is what failed
            typer.secho(
                f"Port {port} is reserved for Latet, but Latet could not record it ({problem}): `latet reserve-port "
                f"--remove` cannot take it away. To take it away later {_BY_HAND}",
                fg="yellow",
            )
            typer.echo(f"  netsh {reservation.delete_line(port)}")
            return True, False
    lines = _later(f"Not reserved: {problem}.", port)
    typer.secho(lines[0], fg="yellow")
    for line in lines[1:]:
        typer.echo(line)
    return False, False


def _started() -> bool:
    """The daemon started on config.yaml's port, said either way."""
    from latet.cli.main import _cannot_start
    from latet.service import daemon

    current = daemon.start()
    if current.running:
        typer.echo(f"daemon: running on 127.0.0.1:{current.port}")
        return True
    _cannot_start(current)
    return False


def unreadable_record(error: OSError, *, failed: bool) -> None:
    """Latet's record of the ports it reserved that cannot be read, said: what Windows holds is then not known, and
    netsh shows it. ``failed``: said as the command's failure."""
    typer.secho(f"{error}: nothing taken away. {_TABLE_BY_HAND}", fg="red" if failed else "yellow", err=failed)


def _named(found: list[int]) -> tuple[str, str]:
    """``port 5283`` or ``ports 5283, 6000``, and ``it`` or ``them``."""
    if len(found) == 1:
        return f"port {found[0]}", "it"
    return f"ports {', '.join(map(str, found))}", "them"


def _remove(uninstalling: bool = False) -> None:
    """Step 10: each port Latet recorded taken away, in one elevated step. ``uninstalling``: from global-uninstall,
    which stopped the daemon for good, so the line to run by hand never says to start Latet again."""
    from latet.service import ports, reservation

    try:
        found = reservation.read_record()
    except reservation.RecordUnreadable as exc:
        unreadable_record(exc, failed=True)
        raise typer.Exit(1) from None
    if not found:
        typer.echo("Latet reserved no port on this computer: nothing to take away.")
        return
    not_updated: reservation.RecordNotUpdated | None = None
    try:
        gone = reservation.remove(found)
    except reservation.RecordNotUpdated as exc:  # taken away, but the record still names them
        gone, not_updated = exc.gone, exc
    except (reservation.ElevationDeclined, OSError) as exc:
        # The ports Windows' table showed gone before the elevated step are off the record already: said as gone.
        try:
            left = reservation.read_record()
        except reservation.RecordUnreadable:  # what is still on it is not known: no port is said gone
            left = list(found)
        for p in found:
            if p not in left:
                typer.echo(f"Port {p} is no longer reserved.")
        nothing = "Nothing else taken away" if len(left) < len(found) else "Nothing taken away"
        if isinstance(exc, reservation.OutcomeUnknown):  # the elevated step ran: what it did is not known
            named = f"port {left[0]} was" if len(left) == 1 else f"ports {', '.join(map(str, left)) or 'recorded'} were"
            typer.secho(
                f"Latet could not read Windows' table of excluded ports afterwards ({exc}): whether {named} taken "
                "away is not known. `latet reserve-port --remove` again tells.",
                fg="yellow",
            )
        elif isinstance(exc, reservation.ElevationDeclined):
            typer.secho(
                f"{nothing}: the administrator's confirmation was declined. `latet reserve-port --remove` asks again.",
                fg="yellow",
            )
        elif isinstance(exc, ports.TableUnreadable):
            # Read before anything changed: the record and Windows are as they were.
            typer.secho(
                f"{nothing}: Latet could not read Windows' table of excluded ports ({exc}). "
                "`latet reserve-port --remove` tries again.",
                fg="red",
                err=True,
            )
        else:
            typer.secho(f"{nothing}: {_problem(exc)}.", fg="red", err=True)
        raise typer.Exit(1) from None
    kept = [p for p in found if p not in gone]  # one gone already is among the gone (`reservation.remove`)
    hand = "by hand, in a PowerShell run as administrator:" if uninstalling else _BY_HAND
    for p in gone:
        typer.echo(f"Port {p} is no longer reserved.")
    for p in kept:
        typer.secho(f"Port {p} is still reserved. To take it away {hand}", fg="yellow")
        typer.echo(f"  netsh {reservation.delete_line(p)}")
    if not_updated is not None:
        named, them = _named([p for p in reservation.recorded() if p in gone] or gone)
        typer.secho(
            f"Latet could not update its record of the ports it reserved ({_problem(not_updated.error)}): it still "
            f"names {named}. `latet reserve-port --remove` again takes {them} off it.",
            fg="yellow",
        )
    if kept or not_updated is not None:
        raise typer.Exit(1)
