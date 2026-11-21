"""`latet server terms` (Latet Enterprise, E6): the synced terms of this server, through its administration API with an
administration token (E0c's `admin_call`, which refuses an agent's change). What it prints holds no term, but `gone`'s
list, which an agent is refused (`_values`). What the server said is printed escaped (E3's ``_said``); a refusal of the
server, and a server that cannot be reached or named, exit 2."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from latet.cli.server import _id, _mutation, _not_secret, admin_call, server_app
from latet.ee.server_cli import _said
from latet.ee.settings import SOURCE_NAME, SOURCES_CHANGED
from latet.server.client import ServerError
from latet.server.connection import NotConnected

server_terms_app = typer.Typer(
    help="Synced terms on this Latet server (Latet Enterprise): your company's lists, masked for everyone it serves.",
    no_args_is_help=True,
)
sources_app = typer.Typer(help="Where the synced terms come from.", no_args_is_help=True)
server_terms_app.add_typer(sources_app, name="sources")
server_app.add_typer(server_terms_app, name="terms")
PATH = "ee/term-sync"
MAX_FILE = 64 * 1024  # bytes of a file of sources, as the server takes a section (ee/admin.py MAX_SECTION)
# While a sync asked for runs, the status is read after 2 seconds, then after twice as long each time, 16 seconds at
# most: each read is an entry of the administration log (E0b's gate records every call), a handful for one run.
WAIT_FIRST = 2.0
WAIT_LONGEST = 16.0
WAIT_MOST = 60.0  # seconds `run` waits at most
BOM = chr(0xFEFF)  # a byte order mark, as Notepad writes one: not part of the text
# What `run` waits with: module names, so that the tests give their own.
clock = time.monotonic
sleep = time.sleep
_ID_SHAPE = re.compile(r"[0-9a-f]{24}")  # a term's id, as `gone` lists it
_NAME_SHAPE = re.compile(SOURCE_NAME)  # a source's name, as the settings take one


def _out(words: str, **style: Any) -> None:
    """Each composed line escaped once (what the server says can hold what a terminal acts on)."""
    typer.secho(_said(words), **style)


def _stop(words: str, code: int = 2) -> NoReturn:
    typer.secho(_said(words), fg="red", err=True)
    raise typer.Exit(code)


def _call(method: str, path: str, body: Any = None, *, refused: str, query: dict[str, Any] | None = None) -> Any:
    """The server's answer to ``path`` under term sync's routes; its refusal said in its words, escaped, and exit 2, as
    a server that cannot be named or reached."""
    try:
        answer = (
            admin_call(method, f"{PATH}{path}", body, query=query) if query else admin_call(method, PATH + path, body)
        )
    except ServerError as exc:
        if exc.status == 409 and exc.message == SOURCES_CHANGED:  # told by its words: a change made from a version read
            _stop(f"{refused}: the sources were changed on the server since this command read them: run it again.")
        _stop(f"{refused}: {exc.message}.")
    except NotConnected as exc:
        _stop(f"{refused}: {exc.message}.")
    if not isinstance(answer, dict):
        _stop(f"{refused}: the server's answer is not a JSON object.")
    return answer


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _terms(n: int) -> str:
    return f"{n:,} term{'' if n == 1 else 's'}"


def _problems(answer: dict[str, Any]) -> list[str]:
    said = answer.get("problems")
    return [each for each in said if isinstance(each, str)] if isinstance(said, list) else []


def _sources(answer: dict[str, Any]) -> list[dict[str, Any]]:
    found = answer.get("sources")
    return (
        [s for s in found if isinstance(s, dict) and isinstance(s.get("name"), str)] if isinstance(found, list) else []
    )


def _confirm(question: str, yes: bool, refused: str) -> None:
    """Asked at your own terminal unless ``yes``; anywhere else ``yes`` is needed."""
    from latet.cli.agent import at_your_terminal

    if yes:
        return
    if not at_your_terminal():
        _stop(f"{refused}: run it at your own terminal, or add --yes.")
    if not typer.confirm(question, default=False):
        raise typer.Exit(1)


def ago(ts: Any, now: float | None = None) -> str:
    """How long ago ``ts`` was: `12 minutes ago`, `3 hours ago`, `just now` (the server's time, as it said it)."""
    from latet.ee.sync.records import ago_words  # the status's words (``api._failing``) alike

    if isinstance(ts, bool) or not isinstance(ts, int | float) or not math.isfinite(ts):
        return "at a time not known"
    return ago_words((time.time() if now is None else now) - ts)


def describe(source: dict[str, Any], now: float | None = None) -> str:
    """`customers (file): 1,204 terms (CLIENT 1,180, PERSON 24), 3 no longer in the source; synced 12 minutes ago`:
    a source as the status describes it, never a term."""
    kind = source.get("kind") or "no longer set up"
    found = source.get("by_type")
    by_type: dict[str, Any] = found if isinstance(found, dict) else {}
    counted = sorted(((str(t), _count(c)) for t, c in by_type.items()), key=lambda tc: (-tc[1], tc[0]))
    types = ", ".join(f"{t} {c:,}" for t, c in counted)
    line = f"{source.get('name')} ({kind}): {_terms(_count(source.get('terms')))}" + (f" ({types})" if types else "")
    if gone := _count(source.get("gone")):
        line += f", {gone:,} no longer in the source"
    last = source.get("last")
    if not isinstance(last, dict):
        line += "; not synced yet"
    elif last.get("outcome") == "ok":
        line += f"; synced {ago(last.get('finished'), now)}"
    else:
        line += f"; failed {ago(last.get('finished'), now)}: {last.get('reason') or last.get('code') or 'no reason'}"
    if source.get("asked") is not None:
        line += "; a sync is asked for"
    return line


@server_terms_app.command("status")
def terms_status() -> None:
    """What each source gave and when it last synced, and how many terms the server keeps. Never a term."""
    state = _call("GET", "", refused="Not shown")
    if state.get("paused") is True:
        typer.echo("Paused by the license: no source is read; the terms synced so far stay masked.")
    if unreadable := _count(state.get("unreadable")):
        _out(
            f"{_terms(unreadable)} cannot be opened with this server's master key: they stay masked as they were; "
            "`latet server terms remove --unreadable` removes those no source gives any more."
        )
    from latet.ee.sync.service import trailing

    behind = state.get("behind")
    if trailing(behind):  # past two ticks, as `latet server status` says it: a reload under way is no failure
        _out(
            f"This instance masks the synced terms of version {behind.get('loaded')} while the server holds "
            f"{behind.get('store')}, for {behind['seconds']:.0f} seconds: it cannot read them again (its log says "
            "why).",
            fg="yellow",
        )
    sources = _sources(state)
    if not sources:
        typer.echo("No source yet: `latet server terms sources set FILE` sets them.")
    for source in sources:
        _out(describe(source))
    kept, most, gone = _count(state.get("kept")), _count(state.get("most")), _count(state.get("gone"))
    said = f"{kept:,} of {most:,} terms kept" + (f", {gone:,} of them no longer in their source" if gone else "")
    _out(said + ".")
    for problem in _problems(state):
        _out(problem, fg="yellow")


@server_terms_app.command("run")
def terms_run(
    source: Annotated[str | None, typer.Argument(help="Only this source.")] = None,
    no_wait: Annotated[bool, typer.Option("--no-wait", help="Ask for it, and do not wait for it to end.")] = False,
) -> None:
    """Sync now: every source, or the one named, on whichever instance of the server holds the sync's turn. Waits a
    minute at most for it to end, and says what each source gave."""
    if source is not None:
        _named(source)
    answer = _call("POST", "/run", {"source": source} if source is not None else {}, refused="Not asked")
    found = answer.get("asked")
    asked = [name for name in found if isinstance(name, str)] if isinstance(found, list) else []
    if not asked:
        typer.echo("No source to sync: `latet server terms sources set FILE` sets them.")
        return
    if no_wait:
        _out(f"Asked: {', '.join(asked)}. `latet server terms status` shows how it went.")
        return
    until, wait = clock() + WAIT_MOST, WAIT_FIRST
    while True:
        by_name = {s["name"]: s for s in _sources(_call("GET", "", refused="Asked, but not followed"))}
        waiting = [
            n for n in asked if n in by_name and (by_name[n].get("asked") is not None or not by_name[n].get("last"))
        ]
        if not waiting or clock() >= until:
            break
        sleep(min(wait, until - clock()))  # the last read at the minute
        wait = min(wait * 2, WAIT_LONGEST)
    failed = False
    for name in asked:
        found = by_name.get(name)
        if found is None:
            _out(f"{name}: no longer set up")
            continue
        last = found.get("last")
        if name in waiting or not isinstance(last, dict):
            _out(f"{name}: still running: `latet server terms status` shows it")
        elif last.get("outcome") == "ok":
            counts = f"{_count(last.get('added')):,} new, {_count(last.get('gone')):,} no longer in the source"
            _out(f"{name}: {_terms(_count(last.get('found')))} ({counts})")
        else:
            failed = True
            _out(f"{name}: failed: {last.get('reason') or last.get('code') or 'no reason'}", fg="red")
    raise typer.Exit(1 if failed else 0)


@server_terms_app.command("gone")
def terms_gone(source: Annotated[str | None, typer.Option("--source", help="Only this source's.")] = None) -> None:
    """The terms no longer in their source, still masked until they are removed. It shows the terms themselves: run it
    yourself, never through an agent."""
    from latet.cli import server as server_cli

    server_cli._values()  # an agent is refused: the list holds the terms
    if source is not None:
        _named(source)
    answer = _call("GET", "/gone", refused="Not shown", query={"source": source} if source is not None else None)
    items = [t for t in answer.get("items", []) if isinstance(t, dict)] if isinstance(answer.get("items"), list) else []
    for term in items:
        _out(f"{term.get('id')}  {term.get('type')}  {term.get('source')}  {term.get('value')}")
    total = _count(answer.get("total"))
    more = f" (the first {len(items):,} shown)" if total > len(items) else ""
    _out(f"{_terms(total)} no longer in their source{more}: `latet server terms remove` stops masking them.")


def _named(name: str) -> str:
    _not_secret(name)
    if not _NAME_SHAPE.fullmatch(name):
        _stop("Not a source's name: lowercase letters, digits and hyphens, as `latet server terms status` lists them.")
    return name


@server_terms_app.command("remove")
def terms_remove(
    ids: Annotated[
        list[str] | None, typer.Argument(help="The terms' ids, as `latet server terms gone` lists them.")
    ] = None,
    source: Annotated[str | None, typer.Option("--source", help="Every term of this source no longer in it.")] = None,
    every: Annotated[bool, typer.Option("--all", help="Every term no longer in its source.")] = False,
    unreadable: Annotated[
        bool,
        typer.Option(
            "--unreadable",
            help="The terms no master key of this server opens, among those no source gives any more.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Remove them without asking.")] = False,
) -> None:
    """Stop masking terms no longer in their source, for everyone the server serves: this lowers protection, they leave
    in clear from now on. A term its source still gives is never removed."""
    ways = sum((bool(ids), source is not None, every, unreadable))
    if ways != 1:
        said = "Say which" if ways == 0 else "Say one of them"
        _stop(f"{said}: the ids `latet server terms gone` lists, --source NAME, --all, or --unreadable.")
    chosen = [_id(each, _ID_SHAPE) for each in ids or []]
    if source is not None:
        _named(source)
    _mutation("POST")  # an agent is refused before anything is read or asked
    if unreadable:
        question = "Remove the synced terms no master key of this server opens and no source gives any more?"
        _confirm(question, yes, "Not removed")
        body: dict[str, Any] = {"unreadable": True}
    else:
        if chosen:
            count, body = len(set(chosen)), {"ids": chosen}
        else:
            state = _call("GET", "", refused="Not removed")
            if source is not None:
                count = sum(_count(s.get("gone")) for s in _sources(state) if s["name"] == source)
                body = {"source": source}
            else:
                count, body = _count(state.get("gone")), {"all": True}
            if count == 0:
                typer.echo("Nothing to remove: every term is still in its source.")
                return
        _confirm(f"Stop masking {_terms(count)} for everyone the server serves?", yes, f"Not removed ({_terms(count)})")
    answer = _call("POST", "/remove", body, refused="Not removed")
    removed = _count(answer.get("removed"))
    if removed == 0:
        typer.echo("Nothing removed: no such term is kept, or its source still gives it.")
        return
    typer.echo(f"{_terms(removed)} removed: Latet no longer masks {'it' if removed == 1 else 'them'}.")


@server_terms_app.command("secret")
def terms_secret(
    source: Annotated[str, typer.Argument(help="The source, by its name.")],
    remove: Annotated[bool, typer.Option("--remove", help="Remove its secret instead.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="With --remove: remove it without asking.")] = False,
) -> None:
    """Set a source's secret (a token, a password, a client secret), asked for and hidden, never taken as an argument:
    kept sealed on the server for where the source goes and what it reads, never shown again."""
    _named(source)
    _mutation("PUT")  # an agent is refused before it is asked for anything
    shown = _call("GET", "/sources", refused="Not removed" if remove else "Not kept")
    etag = shown.get("etag")
    sent: dict[str, Any] = {"etag": etag} if isinstance(etag, str) else {}  # refused once the sources changed since
    found = next((s for s in _sources(shown) if s["name"] == source), None)
    if remove:  # a secret left by a source taken out may be removed too
        _confirm(f"Remove the secret of {source}? The source cannot be read until it is set again.", yes, "Not removed")
        _call("DELETE", f"/secrets/{source}", sent, refused="Not removed")
        typer.echo(f"Removed from the server: the source {source} cannot be read until its secret is set again.")
        return
    if found is None:
        _stop("Not kept: no source has that name: `latet server terms status` lists them.")
    _out(_where(found))  # before the secret is asked for: the place it is kept for
    value = typer.prompt(f"The secret of {source}", hide_input=True).strip()  # as `latet server siem secret` keeps one
    if not value:
        _stop("Not kept: a secret is not empty.")
    _call("PUT", f"/secrets/{source}", {"value": value, **sent}, refused="Not kept")
    typer.echo(f"Kept on the server for the source {source}.")


def _where(source: dict[str, Any]) -> str:
    """Where ``source`` (as `GET /sources` gives it) takes its secret: each address it connects to, and the DN a
    directory signs in as; the source's secret is kept for that and for what it reads. Exit 2 for a source that reads
    with no secret, before it is asked for."""
    from latet.ee.settings import LdapSource, TermSyncSettings, destination_addresses, needs_secret

    name = source["name"]
    try:
        [parsed] = TermSyncSettings.model_validate({"sources": [source]}).sources
    except ValueError:  # a source of a later Latet's: its addresses said as the server gave them
        given = [str(source[key]) for key in ("url", "login_url", "graph_url") if isinstance(source.get(key), str)]
        return f"{name} goes to {', '.join(given) or 'the place its settings name'}."
    if not needs_secret(parsed):
        _stop(f"Not kept: the source {name} reads with no secret.")
    places = destination_addresses(parsed)  # as the page's secret dialog says them
    signs_in = f", signing in as {parsed.bind}" if isinstance(parsed, LdapSource) and parsed.bind else ""
    return (
        f"{name} ({parsed.kind}) goes to {', '.join(places)}{signs_in}: its secret is kept for that place and for what "
        "it reads now, and is set again when either changes."
    )


@sources_app.command("show")
def sources_show() -> None:
    """The sources, as `latet server terms sources set` takes them (YAML): the company's addresses, directories,
    filters and excluded values, so run it yourself, never through an agent. A source names its secret, never holds
    it."""
    from latet.cli import server as server_cli

    server_cli._values()  # an agent is refused: the sources hold the company's addresses and values
    answer = _call("GET", "/sources", refused="Not shown")
    text = answer.get("text")
    if not isinstance(text, str):
        _stop("Not shown: the server's answer holds no sources.")
    for line in text.rstrip("\n").splitlines():
        _out(line)


def _read(file: Path) -> tuple[str, list[str]]:
    """The file's text and the names of the sources it sets, checked as the server checks them before anything is
    sent: a file written wrong exits 2 saying where, never what."""
    import yaml

    from latet.config import load_yaml, yaml_error_at
    from latet.ee.settings import EnterpriseSettings, EnterpriseSettingsError, with_section
    from latet.fileio import FileReadError, read_regular_bytes

    try:
        text = read_regular_bytes(file, limit=MAX_FILE, follow=False).decode("utf-8").removeprefix(BOM)
    except (FileReadError, UnicodeDecodeError):
        _stop(f"{file.name} cannot be read as a file of sources: UTF-8 text, {MAX_FILE // 1024} KB at most.")
    try:
        found = load_yaml(text)
    except yaml.YAMLError as exc:  # where, never what
        _stop(f"{file.name}: not valid YAML{yaml_error_at(exc, text)}.")
    except RecursionError:
        _stop(f"{file.name}: not valid YAML: it nests too deeply.")
    if not isinstance(found, dict):
        _stop(f"{file.name}: the file is `sources:` and the list of sources.")
    try:
        sources = with_section(EnterpriseSettings(), "term_sync", found).term_sync.sources
    except EnterpriseSettingsError as exc:  # by place and kind of error, never what was given
        _stop(f"{file.name}: not set: {exc}.")
    return text, [s.name for s in sources]


@sources_app.command("set")
def sources_set(
    file: Annotated[Path, typer.Argument(help="A YAML file: `sources:` and the list, as `sources show` prints it.")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Set them without asking when a source with terms is taken out.")
    ] = False,
) -> None:
    """Use the sources the file lists, in place of those before. A source taken out leaves its terms "no longer in the
    source": still masked until someone removes them. A secret is never written there: `latet server terms secret`."""
    text, names = _read(file)
    _mutation("PUT")
    shown = _call("GET", "/sources", refused="Not set")
    etag = shown.get("etag")
    # The sources taken out, as PostgreSQL has them (`GET /sources`), never as the answering instance's copy of the
    # settings has them, which can miss one set a moment ago elsewhere; their terms as the store counts them.
    out = [s["name"] for s in _sources(shown) if s["name"] not in names]
    terms = {s["name"]: _count(s.get("terms")) for s in _sources(_call("GET", "", refused="Not set"))}
    count = sum(terms.get(name, 0) for name in out)
    if count:
        taken = ", ".join(name for name in out if terms.get(name))
        _out(f'{_terms(count)} of {taken} become "no longer in the source": still masked until they are removed.')
        _confirm("Set these sources?", yes, "Not set")
    body: dict[str, Any] = {"text": text, **({"etag": etag} if isinstance(etag, str) else {})}
    if len(json.dumps(body)) > MAX_FILE:
        _stop(f"Not set: the sources are larger than the server takes ({MAX_FILE // 1024} KB).")
    change = _call("PUT", "/sources", body, refused="Not set")
    done = []
    for what in ("added", "removed", "changed"):
        named = [n for n in change.get(what, []) if isinstance(n, str)] if isinstance(change.get(what), list) else []
        if named:
            done.append(f"{', '.join(named)} {what}")
    _out(f"{len(names)} source{'' if len(names) == 1 else 's'}: " + ("; ".join(done) or "nothing changed") + ".")
    for problem in _problems(change):
        _out(problem, fg="yellow")
