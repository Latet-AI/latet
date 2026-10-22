"""``latet project``: the settings of one project, its latet.yaml and your own for it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from latet.cli.main import app

project_app = typer.Typer(help="Settings for one project: its latet.yaml, and your own for it.", no_args_is_help=True)
app.add_typer(project_app, name="project")


@project_app.callback()
def _project_here(ctx: typer.Context) -> None:
    from latet.cli.admin import settings_here

    settings_here("project", ctx.invoked_subcommand)


FolderArg = Annotated[
    Path | None, typer.Argument(help="The project's folder, or any folder in it; the current one if not given.")
]


def _n(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def locate(folder: Path | None) -> tuple[str, Path | None]:
    """The project's folder and its latet.yaml (None when it has none)."""
    from latet.projects import project_root

    return project_root(str((folder or Path.cwd()).resolve()))


def repo_file_for(folder: Path | None) -> Path:
    """Where a project's latet.yaml is, or goes: the root of its repository, if there is one."""
    from latet.projects import PROJECT_FILE, find_project_file, git_root

    start = (folder or Path.cwd()).resolve()
    return find_project_file(start) or (git_root(start) or start) / PROJECT_FILE


def load_repo(path: Path) -> Any:
    """A project file's settings, to change them; refused when the file has errors to fix first."""
    from latet.config import ProjectSettings
    from latet.projects import read_project_file

    if not path.exists():
        return ProjectSettings()
    settings, notices = read_project_file(path)
    errors = [n for n in notices if n.level == "error"]
    if settings is None or errors:
        for notice in errors or notices:
            typer.secho(f"{path}: {notice.key or 'the file'}: {notice.message}", fg="red")
        typer.echo("Fix it first: `latet project check` says what is wrong.")
        raise typer.Exit(2)
    return settings


def save_repo(path: Path, settings: Any) -> None:
    from latet.projects import render_project_file, write_project_file

    write_project_file(path, render_project_file(settings))
    typer.echo(f"wrote {path} (only its first lines of comments are kept)")


def personal_entry(config: Any, root: str) -> Any:
    from latet.config import ProjectEntry
    from latet.projects import norm_path

    found = next((e for e in config.projects if norm_path(e.path) == norm_path(root)), None)
    return found or ProjectEntry(path=root)


def _drafts(root: Path) -> list[dict[str, Any]]:
    import os

    from latet.config import load_config
    from latet.engine.build import build
    from latet.engine.detectors.base import DetectContext
    from latet.engine.types import SlotKind
    from latet.engine.vault import Vault
    from latet.runtime import resolve_identity
    from latet.suggest import suggest

    config = load_config()
    masker = build(config, Vault(os.urandom(32)), resolve_identity(config)).masker
    context = DetectContext(SlotKind.TOOL_RESULT)
    return suggest(root, covered=lambda text: masker.detect(text, context), draft_limit=20).get("drafts") or []


@project_app.command("init")
def project_init(
    folder: FolderArg = None,
    with_drafts: Annotated[
        bool, typer.Option("--suggest", help="Also draft recognizers for the formats no rule covers there.")
    ] = False,
) -> None:
    """Write a latet.yaml to start from, at the root of the project's repository.

    It can only add protection to each person's own settings; your own settings for the project
    (values never to mask, Latet off) go in your config.yaml instead.
    """
    from latet.cli.admin import _host_only
    from latet.projects import TEMPLATE, write_project_file

    _host_only("latet project init")
    path = repo_file_for(folder)
    if path.exists():
        typer.secho(f"{path} exists already: `latet project show` says what it does.", fg="yellow")
        raise typer.Exit(1)
    text = TEMPLATE
    if with_drafts:
        drafts = _drafts(path.parent)
        if drafts:
            text += (
                "\n# Drafts from `latet rules suggest`: the examples are invented from each shape. Describe what\n"
                "# each one finds and rename it, then `latet project check`.\n"
                + yaml.safe_dump({"recognizers": drafts}, sort_keys=False, allow_unicode=True)
            )
    write_project_file(path, text)
    typer.echo(f"wrote {path}")


def _offline(root: str, repo_file: Path | None) -> tuple[Any, Any, Any, list[Any]]:
    """What applies in a project, worked out from the files (no daemon needed)."""
    from latet.config import load_config
    from latet.projects import effective_config, personal_for, read_project_file

    config = load_config()
    repo, notices = read_project_file(repo_file) if repo_file is not None else (None, [])
    personal = personal_for(config.projects, root)
    effective, more = effective_config(config, repo, personal)
    return repo, personal, effective, [*notices, *more]


@project_app.command("show")
def project_show(
    folder: FolderArg = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as JSON.")] = False,
) -> None:
    """What applies in a project: its latet.yaml, your own settings for it, and what is ignored."""
    from latet.config import Profile
    from latet.engine.detectors.pii import effective_pii

    root, repo_file = locate(folder)
    repo, personal, effective, notices = _offline(root, repo_file)
    pii = [] if effective.profile == Profile.SECRETS else effective_pii(
        effective.pii, effective.id_regions, every_region=effective.profile == Profile.MAX
    )  # fmt: skip
    enabled = personal.enabled if personal is not None else True
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "root": root,
                    "repo_file": str(repo_file) if repo_file else None,
                    "repo": repo.model_dump(mode="json", exclude_defaults=True) if repo else None,
                    "personal": personal.model_dump(mode="json", exclude={"path"}) if personal else None,
                    "enabled": enabled,
                    "effective": {
                        "profile": effective.profile.value,
                        "pii": pii,
                        "id_regions": effective.id_regions,
                        "terms": len(effective.terms),
                        "recognizers": len(effective.recognizers),
                        "allow_values": len(effective.allow_values),
                        "policies": {k: str(v) for k, v in effective.policies.items()},
                        "data": effective.data_mode(),
                        "data_tools": effective.data_tools,
                        "data_files": effective.data_files,
                        "data_hosts": effective.data_hosts,
                        "fields": [f.model_dump(exclude_defaults=True) for f in effective.fields],
                        "dump_guard": effective.dump_guard,
                        "images": effective.images,
                    },
                    "notices": [n.as_dict() for n in notices],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    typer.secho(f"Project: {root}", bold=True)
    typer.echo(f"  latet.yaml:  {repo_file or 'none (`latet project init` writes one)'}")
    typer.echo(f"  your settings: {'yes, in config.yaml' if personal is not None else 'none'}")
    if not enabled:
        typer.secho("  Latet is OFF in this project: nothing is masked or blocked (`latet project on`).", fg="yellow")
    typer.echo(
        f"  in effect:     profile {effective.profile.value}, {len(pii)} personal-data types, ID numbers of "
        f"{', '.join(effective.id_regions) or 'no country'}, {_n(len(effective.terms), 'term')}, "
        f"{_n(len(effective.recognizers), 'recognizer')}, {_n(len(effective.allow_values), 'exception')}"
    )
    data = effective.data_mode()
    if data == "strict" and effective.data_tools:
        data += f" ({', '.join(effective.data_tools)})"
    named = [*effective.data_files, *effective.data_hosts]  # data with strict data mode on or off
    if named:
        data += f", always data: {', '.join(named)}"
    typer.echo(
        f"  data:          {data}, {_n(len(effective.fields), 'field rule')}, dump guard "
        f"{effective.dump_guard or 'off'}, images {'not sent' if effective.images == 'block' else 'sent'}"
    )
    for notice in notices:
        color = "red" if notice.level == "error" else "yellow"
        where = "latet.yaml" if notice.source == "repo" else "your settings"
        typer.secho(f"  {notice.level}: {where}: {notice.key or 'the file'}: {notice.message}", fg=color)


@project_app.command("check")
def project_check(folder: FolderArg = None) -> None:
    """Check a project's latet.yaml, recognizers included (for CI, too).

    Exit code 0 when all is well, 1 when something in it is ignored or an example fails, 2 for an
    error or when there is no file.
    """
    from latet.cli.rules import _print
    from latet.projects import read_project_file
    from latet.rules_report import ERRORS, OK, WARNINGS
    from latet.rules_report import outcome as _outcome
    from latet.rules_report import report as _report

    root, repo_file = locate(folder)
    if repo_file is None:
        typer.secho(f"No latet.yaml in {root} (`latet project init` writes one).", fg="red")
        raise typer.Exit(ERRORS)
    settings, notices = read_project_file(repo_file)
    for notice in notices:
        typer.secho(
            f"{notice.level}: {notice.key or 'the file'}: {notice.message}",
            fg="red" if notice.level == "error" else "yellow",
        )
    if settings is None:
        raise typer.Exit(ERRORS)
    report = _report(settings.recognizers) if settings.recognizers else []
    if report:
        _print(report)
    if settings.terms:
        typer.secho(
            f"note: {_n(len(settings.terms), 'term')} in the repository: whoever can read it sees them.", fg="yellow"
        )
    outcome = _outcome(report) if report else OK
    if any(n.level == "error" for n in notices):
        outcome = ERRORS
    elif notices and outcome == OK:
        outcome = WARNINGS  # something in the file is ignored
    if outcome == OK:
        typer.secho(f"{repo_file}: all is well.", fg="green")
    raise typer.Exit(outcome)


def _set_enabled(folder: Path | None, enabled: bool) -> None:
    from latet.cli.rules import _save_and_reload
    from latet.config import load_config
    from latet.projects import with_personal

    root, _ = locate(folder)
    config = load_config()
    entry = personal_entry(config, root).model_copy(update={"enabled": enabled})
    empty = not entry.model_dump(exclude_defaults=True, exclude={"path"})
    _save_and_reload(with_personal(config, root, None if empty else entry))
    if enabled:
        typer.echo(f"Latet is on in {root}.")
    else:
        typer.secho(f"Latet is off in {root}: nothing is masked or blocked there.", fg="yellow")
        typer.echo("Surrogates already in a conversation are still restored. `latet project on` undoes it.")


@project_app.command("off")
def project_off(folder: FolderArg = None) -> None:
    """Turn Latet off in a project: nothing is masked or blocked there (in your own settings only)."""
    _set_enabled(folder, False)


@project_app.command("on")
def project_on(folder: FolderArg = None) -> None:
    """Turn Latet on again in a project."""
    _set_enabled(folder, True)
