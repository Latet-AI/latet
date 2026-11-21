"""`latet server reports` and `latet server siem` (Enterprise): a server's reports and SIEM outputs, through its
administration API with the administration token, as every `latet server` command run away from the server (R21). What
the server said is printed escaped (E3's ``_said``); a refusal of the server, and a server that cannot be reached or
named, exit 2. A change is refused to an agent by ``admin_call`` itself; a secret is asked for, hidden, never taken as
an argument. A group named by its name is resolved to its id first, as E3's commands do: a query names ids."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent

from latet.cli.server import _mutation, _seen, admin_call, server_app
from latet.ee.server_cli import GROUP_ID, _said
from latet.fileio import FileReadError, _windows_device, read_regular_bytes
from latet.server.client import ServerError
from latet.server.connection import NotConnected

reports_app = typer.Typer(help="Reports of the server's event log (Latet Enterprise).", no_args_is_help=True)
siem_app = typer.Typer(help="Events to your SIEM (Latet Enterprise).", no_args_is_help=True)
server_app.add_typer(reports_app, name="reports")
server_app.add_typer(siem_app, name="siem")

MAX_FILE = 64 * 1024  # bytes of a file of outputs: a few hundred, as the server keeps a section
MAX_FILE_DEPTH = 32
MAX_FILE_EVENTS = 4096
MOST_SECRET = 4096
BOM = chr(0xFEFF)


def _out(words: str) -> None:
    """Escape each composed terminal line exactly once."""
    typer.echo(_said(words))


def _stop(words: str) -> NoReturn:
    typer.secho(f"latet: {_said(words)}", fg="red", err=True)
    raise typer.Exit(2)


def _call(method: str, path: str, body: Any = None, *, query: dict[str, Any] | None = None) -> Any:
    """The server's answer to ``path`` under its administration API's ee/; its refusal printed (escaped, with what is
    wrong where it says) and exit 2, as is a server that cannot be named or reached."""
    try:
        answer = admin_call(method, f"ee/{path}", body, query=query)
    except ServerError as exc:
        typer.secho(f"latet: {_said(str(exc.message))}", fg="red", err=True)
        for detail in exc.details:
            typer.secho(f"  {_said(str(detail))}", fg="red", err=True)
        raise typer.Exit(2) from None
    except NotConnected as exc:
        _stop(str(exc.message))
    if not isinstance(answer, dict):
        _stop("the server's answer is not a JSON object")
    return answer


def _group_id(group: str) -> str:
    """A group's id: as given when it is one, else the id of the group of that name (case aside), as `latet server
    groups list` lists them."""
    if GROUP_ID.fullmatch(group):
        return group
    answer = _call("GET", "groups")
    found = [
        g
        for g in answer.get("groups", [])
        if isinstance(g, dict)
        and isinstance(g.get("name"), str)
        and g["name"].casefold() == group.casefold()
        and isinstance(g.get("id"), str)
        and GROUP_ID.fullmatch(g["id"])
    ]
    if len(found) != 1:
        _stop(f"no one group is named {group}: `latet server groups list` lists them")
    return str(found[0]["id"])


@reports_app.command("export")
def reports_export(
    start: Annotated[
        str | None, typer.Option("--from", help="The first day, YYYY-MM-DD (default: 30 days ago).")
    ] = None,
    end: Annotated[str | None, typer.Option("--to", help="The last day, YYYY-MM-DD (default: today).")] = None,
    group: Annotated[str | None, typer.Option(help="One group, by its id (g_…) or its name.")] = None,
    project: Annotated[str | None, typer.Option(help="One project, by its name.")] = None,
    agent: Annotated[str | None, typer.Option(help="One agent: claude-code, codex, opencode, apps…")] = None,
    category: Annotated[str | None, typer.Option(help="One kind of value: EMAIL, IBAN, a term's type…")] = None,
    fmt: Annotated[str, typer.Option("--format", help="csv, or json (with the administration log).")] = "csv",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write it to a new regular file; existing destinations are refused."),
    ] = None,
) -> None:
    """Export a report of the server's event log: requests, stopped ones and values masked, a row a day for each group,
    kind, name, project and agent. Counts only; people as `reports.people` says, API keys by name. Recorded in the
    administration log."""
    if fmt not in ("csv", "json"):
        _stop("--format is csv or json")
    asked = {"format": fmt, "from": start, "to": end, "group": _group_id(group) if group else None,
             "project": project, "agent": agent, "category": category}  # fmt: skip
    answer = _call("GET", "reports/export", query={key: value for key, value in asked.items() if value})
    text = answer.get("file")
    if not isinstance(text, str):
        _stop("the server's answer holds no report file")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        _stop("the server's report file is not UTF-8")
    if output is None:
        for line in text.removeprefix(BOM).splitlines():  # escaped: a name may hold what a terminal acts on
            _out(line)
        return
    try:
        if _windows_device(output) or (os.name == "nt" and ":" in output.name):
            _stop("the report cannot be written: choose a new regular file")
        # O_EXCL refuses an existing file, link or special node atomically. Never use a server-provided filename.
        with output.open("xb") as target:
            if not stat.S_ISREG(os.fstat(target.fileno()).st_mode):
                _stop("the report cannot be written: choose a new regular file")
            target.write(data)
    except (OSError, ValueError):
        _stop("the report cannot be written: choose a new regular file in a writable folder")
    _out(f"Report written to {output}.")


@reports_app.command("settings")
def reports_settings(
    people: Annotated[str | None, typer.Option(help="group-only, pseudonymous or named.")] = None,
    retention_days: Annotated[
        int | None, typer.Option("--retention-days", help="Days the log keeps its counts.")
    ] = None,
) -> None:
    """Show, or change, how people appear in reports and in the events sent to the SIEM, and how long the event log
    keeps its counts. In several countries a tool that can follow each employee's work needs an agreement first: see
    Reports and SIEM in the documentation."""
    change = {
        key: value for key, value in {"people": people, "retention_days": retention_days}.items() if value is not None
    }
    answer = _call("PUT", "reports/settings", change) if change else _call("GET", "reports/settings")
    _out(f"people: {answer.get('people')}")
    _out(f"retention_days: {answer.get('retention_days')}")


def _when(seconds: Any) -> str:
    return _seen(seconds)


@siem_app.command("show")
def siem_show() -> None:
    """The SIEM outputs, and how each is doing."""
    answer = _call("GET", "siem")
    outputs = [o for o in answer.get("outputs", []) if isinstance(o, dict)]
    if not outputs:
        typer.echo("No SIEM output: `latet server siem set outputs.yaml` sets them.")
    for output in outputs:
        secret = "secret kept" if output.get("has_secret") else "no secret kept"
        if output.get("secret_destination_changed"):  # I1: set before the output moved; never sent there
            secret = f"its secret was set for another address: `latet server siem secret {output.get('name')}`"
        collapsed = output.get("collapsed")
        if isinstance(collapsed, int) and collapsed > 0:  # the bound (round 3)
            secret += f"; {collapsed:,} entries of the administration log collapsed into a gap marker"
        if output.get("held") or output.get("blocked"):
            doing = (
                f"held at events it refuses ({output.get('last_error')}); "
                f"`latet server siem skip {output.get('name')}` sends them no more"
            )
        elif output.get("failing_since"):
            doing = f"failing since {_when(output['failing_since'])} ({output.get('last_error')})"
        else:
            doing = f"last sent {_when(output.get('last_ok'))}"
        _out(f"{output.get('name')}: {output.get('kind')} {output.get('url')}")
        _out(f"  {secret}; {doing}; {output.get('queued', 0):,} waiting, {output.get('dropped', 0):,} dropped")
    if answer.get("paused"):
        typer.echo("No event is sent to the SIEM now: `latet server status` says why.")


def _outputs_value(text: str) -> Any:
    """Bound construction before loading YAML, then its JSON representation before the administration helper.
    Aliases are unnecessary for an output list and are refused, including cycles and expansion graphs."""
    depth = 0
    for count, event in enumerate(yaml.parse(text, Loader=yaml.SafeLoader), 1):
        if isinstance(event, AliasEvent) or count > MAX_FILE_EVENTS:
            raise ValueError("unsupported YAML structure")
        if isinstance(event, CollectionStartEvent):
            depth += 1
            if depth > MAX_FILE_DEPTH:
                raise ValueError("unsupported YAML depth")
        elif isinstance(event, CollectionEndEvent):
            depth -= 1
    value = yaml.safe_load(text)
    _json_value(value)
    # JSON escaping can expand an alias-free YAML scalar beyond the source-file limit too.
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FILE:
        raise ValueError("outputs exceed the JSON byte limit")
    return value


def _json_value(value: Any) -> None:
    """Only JSON values; JSON's encoder alone would silently stringify non-string mapping keys and accept tuples."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("outputs need string keys")
        for item in value.values():
            _json_value(item)
    elif isinstance(value, list):
        for item in value:
            _json_value(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("outputs need JSON values")


@siem_app.command("set")
def siem_set(
    file: Annotated[Path, typer.Argument(help="A YAML or JSON file: a list of outputs, or `siem:` and the list.")],
) -> None:
    """Set the SIEM outputs from a file. A secret is never written there: `latet server siem secret` sets it."""
    try:
        loaded = _outputs_value(read_regular_bytes(file, limit=MAX_FILE, follow=False).decode("utf-8"))  # M1
    except (FileReadError, ValueError, TypeError, yaml.YAMLError, RecursionError):
        _stop(f"{file.name} cannot be read as a file of outputs (YAML or JSON, {MAX_FILE // 1024} KB at most)")
    outputs = loaded.get("siem") if isinstance(loaded, dict) else loaded
    if not isinstance(outputs, list):
        _stop(f"{file.name} holds no list of outputs")
    answer = _call("PUT", "siem", {"outputs": outputs})
    names = [str(output.get("name")) for output in answer.get("outputs", []) if isinstance(output, dict)]
    _out(f"SIEM outputs saved: {', '.join(names) or 'none'}.")


@siem_app.command("test")
def siem_test(name: Annotated[str | None, typer.Argument(help="The output (default: each of them).")] = None) -> None:
    """Send a test event to each SIEM output, or to the one named, and say whether it arrived."""
    names = (
        [name] if name else [str(o.get("name")) for o in _call("GET", "siem").get("outputs", []) if isinstance(o, dict)]
    )
    if not names:
        typer.echo("No SIEM output: `latet server siem set outputs.yaml` sets them.")
        return
    failed = False
    for each in names:
        answer = _call("POST", "siem/test", {"name": each})
        arrived = answer.get("ok") is True
        _out(f"{each}: {'arrived' if arrived else 'did not arrive: ' + str(answer.get('detail'))}")
        failed = failed or not arrived
    raise typer.Exit(1 if failed else 0)


@siem_app.command("skip")
def siem_skip(name: Annotated[str, typer.Argument(help="The output.")]) -> None:
    """Pass the refused entry or held run at a SIEM output's queue. Administration-log entries stand behind a gap
    marker, recorded as `siem.skipped` with its range; refused non-admin events are dropped and counted. Read the
    receiver's own log first: these entries never reach it. Refused while another instance sends to the output."""
    answer = _call("POST", "siem/skip", {"name": name})
    first, last, count, dropped = (answer.get(key) for key in ("from", "to", "count", "dropped"))
    if type(count) is not int or type(dropped) is not int or count < 0 or dropped < 0:
        _stop("the server's answer holds no skipped counts")
    protected = ""
    if first is not None:
        if type(first) is not int or type(last) is not int or first < 1 or last < first or count < 1:
            _stop("the server's answer holds no skipped range")
        what = f"entry {first}" if first == last else f"entries {first} to {last} ({count})"
        protected = f"skipped {what} of the administration log; "
    elif last is not None or count != 0:
        _stop("the server's answer holds no skipped range")
    _out(f"{name}: {protected}{dropped:,} non-admin events dropped; the queue goes on.")


@siem_app.command("secret")
def siem_secret(
    name: Annotated[str, typer.Argument(help="The output.")],
    remove: Annotated[bool, typer.Option("--remove", help="Remove its secret instead.")] = False,
) -> None:
    """Set the secret of a SIEM output (its token or key), asked for and hidden: kept sealed in the server's database,
    never in the settings, never shown again."""
    _mutation("PUT")
    if remove:
        _call("PUT", "siem/secret", {"name": name, "secret": None})
        typer.echo("Secret removed.")
        return
    value = typer.prompt("Secret", hide_input=True).strip()
    if not value or len(value) > MOST_SECRET or not all(32 <= ord(c) <= 126 for c in value):
        _stop(f"a secret is one line of visible ASCII characters, {MOST_SECRET} at most")  # it goes in a header (I8)
    _call("PUT", "siem/secret", {"name": name, "secret": value})
    typer.echo("Secret saved on the server. Every instance uses it within a minute.")
