"""``latet rules``: your own recognizers; ``latet config``: the file's schema, and a check."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from latet.cli.main import app
from latet.config import load_value, load_yaml
from latet.rules_report import ERRORS, OK, WARNINGS
from latet.rules_report import outcome as _outcome
from latet.rules_report import report as _report

rules_app = typer.Typer(help="Your own recognizers: test, add, list and remove them.", no_args_is_help=True)
config_app = typer.Typer(help="config.yaml: where it is, its JSON Schema, and a check.", no_args_is_help=True)
app.add_typer(rules_app, name="rules")
app.add_typer(config_app, name="config")


@rules_app.callback()
def _rules_here(ctx: typer.Context) -> None:
    from latet.cli.admin import settings_here

    settings_here("rules", ctx.invoked_subcommand)


@config_app.callback()
def _config_here(ctx: typer.Context) -> None:
    from latet.cli.admin import settings_here

    settings_here("config", ctx.invoked_subcommand)


def _read(file: Path) -> Any:
    text = sys.stdin.read() if str(file) == "-" else file.read_text("utf-8")
    return load_yaml(text)


def _entries(file: Path | None) -> list[Any]:
    """Recognizers from FILE (one, a list, or a mapping with `recognizers:`), or from config.yaml."""
    from latet.config import load_config

    if file is None:
        return list(load_config().recognizers)
    try:
        data = _read(file)
    except (OSError, yaml.YAMLError) as exc:
        raise typer.BadParameter(f"cannot read it: {exc}", param_hint="FILE") from None
    if isinstance(data, dict) and "recognizers" in data:
        data = data["recognizers"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise typer.BadParameter("a recognizer, a list of recognizers, or `recognizers:`", param_hint="FILE")
    return data


def _print(report: list[dict[str, Any]]) -> None:
    for item in report:
        kind = f"{item['type']}, {item['class']}" if "type" in item else "not loaded"
        color = {"active": "green", "off": "yellow", "error": "red"}[item["state"]]
        typer.secho(f"{item['name']}  ({kind}): {item['state']}", fg=color, bold=True)
        for error in item["errors"]:
            typer.secho(f"  error: {error}", fg="red")
        for example in item.get("examples", []):
            mark = "ok  " if example["ok"] else "FAIL"
            expect = "match   " if example["expect"] == "match" else "no match"
            line = f"  {mark} {expect} {example['text']!r}"
            if example["found"]:
                line += f"  ->  found {', '.join(map(repr, example['found']))}"
            elif example["reason"]:
                line += f"  ->  {example['reason']}"
            typer.secho(line, fg=None if example["ok"] else "yellow")
        for warning in item["warnings"]:
            if not warning.startswith("examples."):  # the examples are shown above
                typer.secho(f"  warning: {warning}", fg="yellow")
        if "stdlib" in item:
            samples = ", ".join(repr(s) for s in item["stdlib"]["samples"][:3])
            shown = f" ({samples}...)" if samples else ""
            typer.echo(f"  standard library: {item['stdlib']['matches']} matches{shown}")
        if "corpus" in item:
            corpus = item["corpus"]
            forms = ", ".join(f"{form} x{n}" for form, n in sorted(corpus["shapes"].items(), key=lambda p: -p[1])[:5])
            typer.echo(
                f"  your files: {sum(corpus['shapes'].values())} matches in {corpus['files_with_matches']} of "
                f"{corpus['files']} files" + (f": {forms}" if forms else "")
            )


@rules_app.command("test")
def rules_test(
    file: Annotated[
        Path | None, typer.Argument(help="A YAML file with recognizers (- for stdin); config.yaml if not given.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
    corpus: Annotated[
        Path | None, typer.Option(help="Also count matches in your own files (counts and shapes only).")
    ] = None,
) -> None:
    """Check recognizers: each example, with the reason when it fails, and what they find in ordinary code.

    Exit code 0 when all is well, 1 when an example fails, 2 when a recognizer cannot load.
    """
    report = _report(_entries(file), corpus)
    if as_json:
        typer.echo(json.dumps({"recognizers": report, "outcome": _outcome(report)}, indent=2, ensure_ascii=False))
    elif not report:
        typer.echo("No recognizers.")
    else:
        _print(report)
    raise typer.Exit(_outcome(report))


def _known_in_vault(values: list[str]) -> list[bool] | None:
    """Which values the running daemon has seen for real (None: it did not answer)."""
    import urllib.error
    import urllib.request

    from latet.config import data_dir, load_config
    from latet.service.daemon import local_urlopen
    from latet.storage.keys import load_bridge_token

    request = urllib.request.Request(
        f"http://127.0.0.1:{load_config().port}/_latet/admin/known",
        data=json.dumps({"values": values}).encode(),
        method="POST",
        headers={"x-latet-token": load_bridge_token(data_dir()), "content-type": "application/json"},
    )
    try:
        with local_urlopen(request, timeout=10) as response:
            known = json.loads(response.read()).get("known")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return known if isinstance(known, list) else None


def _save_and_reload(config: Any, wait: bool = False) -> None:
    """Save ``config`` and apply it. A change that lowers protection is asked about at your own
    terminal, and waits for you in the dashboard otherwise (exit code 3 while it waits)."""
    from latet.cli.admin import _reload_answer, print_recognizer_problems, say_refusal, wait_for_change
    from latet.config import save_config

    path = save_config(config)
    typer.echo(f"saved {path}")
    answer = _reload_answer()
    if answer is None:
        typer.echo(
            "The daemon is not running: the change applies when it starts (a change that lowers protection "
            "waits for your approval in the dashboard then)."
        )
        return
    if "error" in answer:
        say_refusal(answer)
        raise typer.Exit(1)
    if not answer.get("applied", True):
        pending = answer.get("pending") or {}
        if not (wait and pending.get("id")):
            raise typer.Exit(3)
        typer.echo("Waiting for the answer...")
        state = wait_for_change(pending["id"])
        typer.echo(f"The user's answer: {state or 'none yet'}.")
        if state != "approved":
            raise typer.Exit(3)
        return
    typer.echo("Rules reloaded.")
    print_recognizer_problems(answer.get("recognizers") or [])


ProjectOption = Annotated[
    Path | None, typer.Option("--project", help="In your own settings for the project in this folder instead.")
]
RepoOption = Annotated[
    Path | None,
    typer.Option(
        "--repo", help="In the latet.yaml of the project in this folder instead (shared through its repository)."
    ),
]


def _target(project: Path | None, repo: Path | None) -> str:
    if project is not None and repo is not None:
        raise typer.BadParameter("--project or --repo, not both")
    return "repo" if repo is not None else "project" if project is not None else "global"


def _recognizers_of(where: str, project: Path | None, repo: Path | None) -> list[Any]:
    from latet.cli.project import load_repo, locate, personal_entry, repo_file_for
    from latet.config import load_config

    if where == "repo":
        return list(load_repo(repo_file_for(repo)).recognizers)
    if where == "project":
        return list(personal_entry(load_config(), locate(project)[0]).recognizers)
    return list(load_config().recognizers)


def _save_recognizers(where: str, project: Path | None, repo: Path | None, entries: list[Any]) -> None:
    from latet.cli.project import load_repo, locate, personal_entry, repo_file_for, save_repo
    from latet.config import load_config
    from latet.projects import with_personal

    if where == "repo":
        path = repo_file_for(repo)
        save_repo(path, load_repo(path).model_copy(update={"recognizers": entries}))
        return  # the daemon looks at the file again by itself
    config = load_config()
    if where == "project":
        root = locate(project)[0]
        entry = personal_entry(config, root).model_copy(update={"recognizers": entries})
        _save_and_reload(with_personal(config, root, entry))
        return
    config.recognizers = entries
    _save_and_reload(config)


@rules_app.command("add")
def rules_add(
    file: Annotated[Path, typer.Argument(help="A YAML file with one or more recognizers (- for stdin).")],
    project: ProjectOption = None,
    repo: RepoOption = None,
) -> None:
    """Add recognizers to config.yaml (or update those with the same name), then apply them.

    They are checked first, as `rules test` does. Examples must be invented: an example whose value
    Latet has seen for real (it is in the vault) is refused. With --project or --repo they go to
    one project's settings instead.
    """
    where = _target(project, repo)

    entries = _entries(file)
    report = _report(entries)
    if _outcome(report) == ERRORS:
        _print(report)
        typer.secho("Nothing was added: fix the errors above (`latet rules test FILE`).", fg="red")
        raise typer.Exit(ERRORS)
    found = [(item["name"], value) for item in report for e in item.get("examples", []) for value in e["found"]]
    known = _known_in_vault([value for _, value in found]) if found else []
    if known is None:
        typer.secho("Could not check the examples against the vault: the daemon is not running.", fg="yellow")
    elif any(known):
        for (name, _), real in zip(found, known, strict=True):
            if real:
                typer.secho(f"{name}: an example holds a real value (Latet has masked it before).", fg="red")
        typer.echo("Invent the examples: change every letter and digit, keep the format.")
        raise typer.Exit(ERRORS)
    current = _recognizers_of(where, project, repo)
    by_name = {e.get("name"): i for i, e in enumerate(current) if isinstance(e, dict)}
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if name in by_name:
            current[by_name[name]] = entry
            typer.echo(f"{name}: updated")
        else:
            current.append(entry)
            typer.echo(f"{name}: added")
    _save_recognizers(where, project, repo, current)
    if _outcome(report) == WARNINGS:
        typer.secho("Some examples fail: `latet rules test` shows why.", fg="yellow")


@rules_app.command("list")
def rules_list(
    as_json: Annotated[bool, typer.Option("--json", help="Print the list as JSON.")] = False,
    project: ProjectOption = None,
    repo: RepoOption = None,
) -> None:
    """The recognizers in config.yaml (or one project's), and whether each one is active."""
    from latet.engine.build import reserved_types
    from latet.engine.detectors.recognizers import compile_all

    entries = _recognizers_of(_target(project, repo), project, repo)
    _, statuses = compile_all(entries, reserved_types())
    if as_json:
        typer.echo(json.dumps([s.as_dict() for s in statuses], indent=2, ensure_ascii=False))
        return
    if not statuses:
        typer.echo("No recognizers. `latet rules add FILE` adds some; see llms-rules.md for how to write them.")
        return
    for entry, status in zip(entries, statuses, strict=True):
        description = entry.get("description", "") if isinstance(entry, dict) else ""
        problems = len(status.errors) + len(status.warnings)
        note = f"  ({problems} to look at)" if problems else ""
        typer.echo(f"{status.name:24s} {status.state:7s} {description}{note}")


@rules_app.command("remove")
def rules_remove(
    name: Annotated[str, typer.Argument(help="The recognizer's name.")],
    project: ProjectOption = None,
    repo: RepoOption = None,
) -> None:
    """Remove a recognizer from config.yaml (or one project's settings), then apply the change."""
    where = _target(project, repo)
    current = _recognizers_of(where, project, repo)
    kept = [e for e in current if not (isinstance(e, dict) and e.get("name") == name)]
    if len(kept) == len(current):
        typer.secho(f"No recognizer is called {name}.", fg="red")
        raise typer.Exit(1)
    typer.echo(f"{name}: removed")
    _save_recognizers(where, project, repo, kept)


@rules_app.command("suggest")
def rules_suggest(
    folder: Annotated[Path, typer.Argument(help="The folder to look through (a git repository: its files only).")],
    as_yaml: Annotated[
        bool, typer.Option("--yaml", help="Print draft recognizers, with examples invented from the shapes.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
    show_values: Annotated[
        bool,
        typer.Option("--show-values", help="Also show three values of each shape: at a terminal, never in an agent."),
    ] = False,
    limit: Annotated[int, typer.Option(help="How many shapes to show.")] = 20,
) -> None:
    """Find code-like values that no rule covers, grouped by shape (AA-999999), on this machine.

    Never prints a value: shapes, counts, files, likely check digits and the labels next to them.
    Nothing leaves this machine.
    """
    import os

    from latet.cli.agent import at_your_terminal
    from latet.config import load_config
    from latet.engine.build import build
    from latet.engine.detectors.base import DetectContext
    from latet.engine.types import SlotKind
    from latet.engine.vault import Vault
    from latet.runtime import resolve_identity
    from latet.suggest import suggest

    if show_values and not at_your_terminal(strict=True):
        typer.secho(
            "--show-values only works at a terminal of your own, not through an agent or a pipe: "
            "the values would end up where you did not mean them to.",
            fg="red",
        )
        raise typer.Exit(2)
    if not folder.exists():
        raise typer.BadParameter("no such folder", param_hint="FOLDER")
    config = load_config()
    masker = build(config, Vault(os.urandom(32)), resolve_identity(config)).masker
    context = DetectContext(SlotKind.TOOL_RESULT)
    result = suggest(
        folder,
        covered=lambda text: masker.detect(text, context),
        show_values=3 if show_values else 0,
        draft_limit=limit if as_yaml else 0,
    )
    result["shapes"] = result["shapes"][:limit]
    if as_json:
        typer.echo(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if as_yaml:
        if not result.get("drafts"):
            typer.echo("# No shape with letters to draft a recognizer for.")
            return
        typer.echo("# Drafts from `latet rules suggest`: the examples are invented from each shape.")
        typer.echo("# Describe what each one finds, rename it, then `latet rules test` and `latet rules add`.")
        typer.echo(yaml.safe_dump({"recognizers": result["drafts"]}, sort_keys=False, allow_unicode=True).rstrip())
        return
    typer.echo(f"Looked at {result['files']} files ({result['tokens']} code-like tokens).")
    if not result["shapes"]:
        typer.echo("Every code-like value there is already covered, or too rare to be a format.")
        return
    typer.echo("Shapes no rule covers, most varied first (A letter, 9 digit):")
    typer.echo()
    for item in result["shapes"]:
        files = ", ".join(item["files"][:3]) + (", ..." if item["file_count"] > 3 else "")
        typer.secho(f"  {item['shape']:20s}", bold=True, nl=False)
        where = "1 file" if item["file_count"] == 1 else f"{item['file_count']} files"
        typer.echo(f" {item['distinct']} values, {item['count']} times, in {where} ({files})")
        extra = []
        if item["checksum"]:
            extra.append(f"check digits: {item['checksum']}")
        if item["labels"]:
            extra.append("next to: " + ", ".join(f"{label['word']} ({label['values']})" for label in item["labels"]))
        if item.get("samples"):
            extra.append("for example " + ", ".join(item["samples"]))
        if extra:
            typer.echo(f"  {'':20s} {' · '.join(extra)}")
    typer.echo()
    typer.echo("`--yaml` drafts recognizers for these, with invented examples; `llms-rules.md` explains the rest.")


@config_app.command("path")
def config_path_command() -> None:
    """Print where config.yaml is (`LATET_CONFIG` names another)."""
    from latet.config import config_path

    typer.echo(str(config_path()))


@config_app.command("schema")
def config_schema_command(
    project: Annotated[bool, typer.Option("--project", help="The schema of a project's latet.yaml instead.")] = False,
) -> None:
    """Print the JSON Schema of config.yaml (editors and agents can check a file with it)."""
    from latet.schema import config_schema, project_schema

    typer.echo(json.dumps(project_schema() if project else config_schema(), indent=2, ensure_ascii=False))


@config_app.command("validate")
def config_validate(
    file: Annotated[Path | None, typer.Argument(help="The file to check; config.yaml if not given.")] = None,
) -> None:
    """Check a configuration file: its keys and values, and every recognizer.

    Exit code 0 when all is well, 1 when a recognizer's example fails, 2 for an error.
    """
    from pydantic import ValidationError

    from latet.config import Config, config_path, term_errors

    path = file or config_path()
    try:
        data = _read(path) or {}
        config = Config.model_validate(data)
    except (OSError, yaml.YAMLError) as exc:
        typer.secho(f"{path}: cannot read it: {exc}", fg="red")
        raise typer.Exit(ERRORS) from None
    except ValidationError as exc:
        _say(exc.errors(include_url=False, include_context=False, include_input=False))
        raise typer.Exit(ERRORS) from None
    # A term Latet cannot mask is never looked for: the file is read all the same.
    unmaskable = term_errors(t.value for t in config.terms)
    for i, entry in enumerate(config.projects):
        unmaskable += term_errors((t.value for t in entry.terms), ("projects", i, "terms"))
    _say(unmaskable)
    report = _report(config.recognizers)
    outcome = max(_outcome(report), ERRORS if unmaskable else OK)
    if report:
        _print(report)
    typer.secho(f"{path}: {'valid' if outcome == OK else 'valid, with warnings' if outcome == WARNINGS else 'errors'}",
                fg="green" if outcome == OK else "yellow" if outcome == WARNINGS else "red")  # fmt: skip
    raise typer.Exit(outcome)


KeyArg = Annotated[
    str, typer.Argument(help="A key of config.yaml, with dots for what is inside: `profile`, `llm_review.enabled`.")
]
ConfigProject = Annotated[
    Path | None, typer.Option("--project", help="Your own settings for the project in this folder instead.")
]
WaitOption = Annotated[
    bool, typer.Option("--wait", help="When the change waits for the user's approval, wait for the answer.")
]


def _settings(project: Path | None) -> tuple[dict[str, Any], Any]:
    """config.yaml's settings, or your own for a project, as plain data; and how to build them back."""
    from latet.config import Config, ProjectEntry, load_config

    config = load_config()
    if project is None:
        return config.model_dump(mode="json"), Config.model_validate
    from latet.cli.project import locate, personal_entry
    from latet.projects import with_personal

    root = locate(project)[0]
    data = personal_entry(config, root).model_dump(mode="json", exclude={"path"})
    return data, lambda d: with_personal(config, root, ProjectEntry.model_validate({**d, "path": root}))


def _at(data: Any, key: str) -> tuple[Any, str | int]:
    """The container of ``key`` (dotted; a number picks a list item) and the last part of it."""
    parts = key.split(".")
    node = data
    for i, part in enumerate(parts[:-1]):
        node = _step(node, part, ".".join(parts[: i + 1]))
    last = parts[-1]
    if isinstance(node, list):
        if not last.isdigit() or int(last) >= len(node):
            raise typer.BadParameter(f"{key}: no such item", param_hint="KEY")
        return node, int(last)
    if not isinstance(node, dict) or last not in node:
        raise typer.BadParameter(f"{key}: no such key (`latet config schema` lists them)", param_hint="KEY")
    return node, last


def _step(node: Any, part: str, where: str) -> Any:
    if isinstance(node, list) and part.isdigit() and int(part) < len(node):
        return node[int(part)]
    if isinstance(node, dict) and isinstance(node.get(part), dict | list):
        return node[part]
    raise typer.BadParameter(f"{where}: no such key, or not one with keys inside", param_hint="KEY")


def _say(errors: Any) -> None:
    """Validation errors, one line each: where, and what is wrong."""
    for error in errors:
        typer.secho(f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}", fg="red")


def _term_values(data: dict[str, Any]) -> list[str]:
    """The values of the terms in ``data``, config.yaml's settings or yours for a project, as they are now."""
    return [term["value"] for term in data.get("terms") or []]


def _apply(data: dict[str, Any], build: Any, wait: bool, kept: list[str]) -> None:
    """Check the new settings, save them, and apply them (what lowers protection waits for the user). A term
    Latet cannot mask is refused, unless it is one of those ``kept`` from before (written by hand)."""
    from pydantic import ValidationError

    from latet.config import term_errors

    try:
        config = build(data)
    except ValidationError as exc:
        _say(exc.errors(include_url=False, include_context=False, include_input=False))
        raise typer.Exit(ERRORS) from None
    if unmaskable := term_errors(_term_values(data), kept=kept):
        _say(unmaskable)
        raise typer.Exit(ERRORS)
    _save_and_reload(config, wait=wait)


@config_app.command("get")
def config_get(
    key: Annotated[str | None, typer.Argument(help="A key, with dots for what is inside (none: every key).")] = None,
    project: ConfigProject = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as JSON.")] = False,
) -> None:
    """Print a setting of config.yaml (or every one), with its default when not set."""
    data, _ = _settings(project)
    value: Any = data
    if key:
        node, last = _at(data, key)
        value = node[last]
    if as_json:
        typer.echo(json.dumps(value, indent=2, ensure_ascii=False))
    else:
        text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
        typer.echo(text.rstrip().removesuffix("...").rstrip())  # a single value: no end-of-document line


@config_app.command("set")
def config_set(
    key: KeyArg,
    value: Annotated[
        str | None, typer.Argument(help="The new value, in YAML: `max`, `true`, `[IT, DE]`, `{enabled: true}`.")
    ] = None,
    unset: Annotated[bool, typer.Option("--unset", help="Back to its default.")] = False,
    project: ConfigProject = None,
    wait: WaitOption = False,
) -> None:
    """Change a setting of config.yaml and apply it.

    A change that adds protection applies at once. One that lowers it is asked about at your own
    terminal; run by an agent, it waits for the user's approval in the dashboard (exit code 3).
    """
    if (value is None) == (not unset):
        raise typer.BadParameter("give a VALUE, or --unset")
    data, build = _settings(project)
    kept = _term_values(data)
    node, last = _at(data, key)
    if unset:
        if isinstance(node, list):
            raise typer.BadParameter(f"{key}: `config remove` takes an item out of a list", param_hint="KEY")
        del node[last]
    else:
        try:
            node[last] = load_value(value or "")
        except yaml.YAMLError as exc:
            raise typer.BadParameter(f"not valid YAML: {exc}", param_hint="VALUE") from None
    _apply(data, build, wait, kept)


@config_app.command("add")
def config_add(
    key: KeyArg,
    value: Annotated[str, typer.Argument(help="The item to add, in YAML: `IBAN`, `{value: Falcon, type: PROJECT}`.")],
    project: ConfigProject = None,
    wait: WaitOption = False,
) -> None:
    """Add an item to a list of config.yaml (`terms`, `pii`, `allow_values`...) and apply it."""
    data, build = _settings(project)
    kept = _term_values(data)
    node, last = _at(data, key)
    items = node[last]
    if not isinstance(items, list):
        raise typer.BadParameter(f"{key} is not a list: `config set` changes it", param_hint="KEY")
    item = load_value(value)
    if item in items:
        typer.echo(f"{key}: there already")
        return
    items.append(item)
    _apply(data, build, wait, kept)


@config_app.command("remove")
def config_remove(
    key: KeyArg,
    value: Annotated[str, typer.Argument(help="The item to take out; for terms and recognizers, its value or name.")],
    project: ConfigProject = None,
    wait: WaitOption = False,
) -> None:
    """Take an item out of a list of config.yaml and apply it (taking protection away waits for the user)."""
    data, build = _settings(project)
    before = _term_values(data)
    node, last = _at(data, key)
    items = node[last]
    if not isinstance(items, list):
        raise typer.BadParameter(f"{key} is not a list: `config set --unset` resets it", param_hint="KEY")
    wanted = load_value(value)

    def matches(item: Any) -> bool:
        if item in (wanted, value):
            return True
        return isinstance(item, dict) and value in (item.get("value"), item.get("name"))

    kept = [item for item in items if not matches(item)]
    if len(kept) == len(items):
        typer.echo(f"{key}: not there")
        raise typer.Exit(1)
    node[last] = kept
    _apply(data, build, wait, before)


# --- your rules, for a colleague ------------------------------------------------------------------------


@rules_app.command("export")
def rules_export(
    file: Annotated[str | None, typer.Argument(help="Where to write them (none or -: standard output).")] = None,
    project: ConfigProject = None,
) -> None:
    """Write your rules to a file, for a colleague: terms, recognizers, policies and the rest.

    In it: the terms, recognizers, policies and field rules, the personal-data types and countries,
    the exceptions (values never to mask...) and the local reviewer's guidance. Never this machine's
    identity, port, paths or servers. With --project, your own settings for the project in that
    folder instead: its shared rules travel in its latet.yaml. The terms are values in clear:
    send the file the way you would send them.
    """
    from latet.cli.project import locate
    from latet.config import load_config, write_atomic
    from latet.transfer import export_rules

    root = locate(project)[0] if project is not None else None
    text, warnings = export_rules(load_config(), root, known=_known_in_vault)
    if file in (None, "-"):
        typer.echo(text, nl=False)
    else:
        write_atomic(Path(file), text, private=True)
        typer.echo(f"Your {'settings for the project' if root else 'rules'} are in {file}.", err=True)
    for warning in warnings:
        typer.secho(warning, fg="yellow", err=True)


@rules_app.command("import")
def rules_import(
    file: Annotated[str, typer.Argument(help="A rules file, or a config.yaml or latet.yaml (- for stdin).")],
    project: ConfigProject = None,
    everything: Annotated[
        bool, typer.Option("--all", help="Also what lowers protection: it waits for the user's approval.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only say what it would do.")] = False,
    wait: WaitOption = False,
) -> None:
    """Add a colleague's rules to yours: what adds protection applies at once.

    New terms, recognizers and field rules are added, and values stricter than yours taken; nothing
    of yours is removed or weakened. What would lower protection (the exceptions, a weaker value, the
    local reviewer's guidance) is listed and left out: with --all it is applied too, and waits for
    approval like any change that lowers protection. With --project, into your own settings for the
    project in that folder.
    """
    from latet.cli.project import locate
    from latet.config import load_config
    from latet.transfer import RulesError, merge_rules, read_rules

    try:
        text = sys.stdin.read() if file == "-" else Path(file).read_text("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise typer.BadParameter(f"cannot read it: {exc}", param_hint="FILE") from None
    try:
        settings, notices = read_rules(text)
        root = locate(project)[0] if project is not None else None
        merged = merge_rules(load_config(), settings, root)
    except RulesError as exc:
        typer.secho(f"Nothing was imported: {exc}.", fg="red")
        raise typer.Exit(ERRORS) from None
    report = merged.report
    report.notices = notices
    # One final candidate, saved and reloaded once: with --all what lowers protection is in it too, and the whole of it
    # waits for your approval then (what adds protection with it), never a first part applied and the rest saved past
    # it.
    held = everything and bool(report.held)
    candidate = merged.full if held else merged.protective if report.added else None
    _print_import(report.as_dict(), everything)
    if dry_run:
        return
    if candidate is None:
        typer.echo("Nothing to import.")
        return
    _import_and_reload(candidate, wait=wait, waits=held)


def _print_import(report: dict[str, list[str]], everything: bool) -> None:
    """What the import would do, before it is saved: planned, never said done (the daemon's reload says what applies,
    and it also weighs the repositories it remembers, which this preview does not read)."""
    sections = (
        ("added", "Would add:", "green"),
        ("skipped", "Yours already, or kept as yours:", None),
        (
            "held",
            "Would add too, and it lowers protection:" if everything else "Left out, it would lower protection:",
            "yellow",
        ),
        ("notices", "Not read:", "yellow"),
    )
    for key, title, color in sections:
        if report[key]:
            typer.secho(title, fg=color, bold=True)
            for sentence in report[key]:
                typer.echo(f"  {sentence}")
    if report["held"] and not everything:
        typer.echo("--all applies them too: they wait for your approval.")
    elif report["held"]:
        typer.echo("It waits for your approval, all of it: nothing of the import applies until then.")


def _import_and_reload(config: Any, *, wait: bool, waits: bool) -> None:
    """The import's one save and reload, and what came of it, said as it is (``_reload_answer``): applied (exit 0);
    waiting for your approval, rejected, stale or superseded (exit 3); the daemon refused the reload, or did not answer,
    so that whether the import applies is not known (exit 1). Never "added" for what was only saved."""
    from latet.cli.admin import _reload_answer, print_recognizer_problems, say_refusal, wait_for_change
    from latet.config import save_config

    path = save_config(config)
    typer.echo(f"saved {path}")
    answer = _reload_answer()
    if not answer:  # no answer (or none that is a reload's): the daemon may be stopped, or may not have answered
        typer.secho(
            "Saved, but whether it applies is unconfirmed: the daemon did not answer the reload. `latet reload` "
            "applies it once the daemon runs (what lowers protection waits for your approval then).",
            fg="yellow",
        )
        raise typer.Exit(1)
    if "error" in answer:
        say_refusal(answer)
        typer.echo("Saved in config.yaml, not applied: `latet reload` once the daemon can reload.")
        raise typer.Exit(1)
    if answer.get("applied") is True:
        typer.echo("Rules reloaded.")
        print_recognizer_problems(answer.get("recognizers") or [])
        return
    pending = answer.get("pending") or {}
    if not pending.get("id"):
        if answer.get("applied") is False:  # answered, and not applied (refused at your own terminal)
            typer.echo("Saved in config.yaml, not applied.")
            raise typer.Exit(3)
        typer.secho("Saved, but whether it applies is unconfirmed: the daemon's answer did not say.", fg="yellow")
        raise typer.Exit(1)
    if not wait:
        typer.echo(
            "Saved in config.yaml: nothing of the import applies until the change is approved"
            + (" (what adds protection waits with what lowers it)." if waits else ".")
        )
        raise typer.Exit(3)
    typer.echo("Waiting for the answer...")
    state = wait_for_change(pending["id"])
    typer.echo(f"The user's answer: {state or 'none yet'}.")
    if state != "approved":
        raise typer.Exit(3)
