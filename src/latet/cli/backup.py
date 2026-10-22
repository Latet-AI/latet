"""``latet backup`` and ``latet restore``: your installation in one encrypted file, to move it to
another machine or keep it (the format is in storage/backup.py)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from latet.cli.main import app

# The databases and their WAL files: moved together, or SQLite would replay an old journal on a new file.
_DATABASES = ("latet.db", "exchanges.db")
_SIDE = ("", "-wal", "-shm")
NOT_MOVED = (
    "Not moved, each machine has its own: the local reviewer's API key (`latet llm key`), the local name "
    "model (`latet ner install`), the text recognition models (`latet ocr install`), and your agents' settings "
    "(`latet global-install`, or start one with `latet claude`)."
)


def _refuse_in_agent(command: str) -> None:
    from latet.cli.agent import agent_name

    agent = agent_name(strict=True)
    if agent is not None:
        typer.secho(
            f"`latet {command}` handles your vault and its key: run it yourself, in a terminal of your own "
            f"(it was run by {agent}).",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)


def _passphrase(new: bool) -> str:
    from latet.storage.backup import MIN_PASSPHRASE

    if sys.stdin.isatty():
        value = typer.prompt("Passphrase", hide_input=True, confirmation_prompt=new)
    else:
        value = sys.stdin.readline().rstrip("\r\n")
    if not value:
        raise typer.BadParameter("no passphrase given")
    if new and len(value) < MIN_PASSPHRASE:
        raise typer.BadParameter(
            f"at least {MIN_PASSPHRASE} characters: a few words are easy to remember and hard to guess"
        )
    return value


@app.command()
def backup(
    file: Annotated[Path, typer.Argument(help="Where to write it, such as latet-backup.latet.")],
    vault: Annotated[
        bool,
        typer.Option(
            "--vault", help="Also the vault and its root key: the same fakes there, and conversations still restored."
        ),
    ] = False,
    history: Annotated[bool, typer.Option("--history", help="Also the dashboard's history (needs --vault).")] = False,
) -> None:
    """Save your installation in one file, encrypted with a passphrase: to move it to another machine.

    In it: your settings (config.yaml) and the local reviewer's trusted certificates. With --vault,
    the vault and its root key too: the other machine then makes the same fakes, remembers the values
    found so far, and keeps restoring conversations started here. With --history, the dashboard's
    history. Never the reviewer's API key, the bridge token or your agents' settings: each machine
    has its own. Restore it with `latet restore FILE`; without the passphrase nobody can read it,
    you included. The passphrase is asked twice, or read from standard input.
    """
    _refuse_in_agent("backup")
    from latet.config import config_path, data_dir
    from latet.storage.backup import BackupError, write_backup
    from latet.storage.keys import load_root_key

    if history and not vault:
        raise typer.BadParameter("--history needs --vault: the history is sealed with the vault's key")
    data = data_dir()
    if vault and not (data / "latet.db").exists():
        typer.secho("There is no vault here yet: nothing to save with --vault.", fg="red")
        raise typer.Exit(2)
    passphrase = _passphrase(new=True)
    try:
        key = load_root_key(data) if vault else None
        contents = write_backup(file, passphrase, data, config_path(), vault=vault, history=history, root_key=key)
    except (BackupError, OSError) as exc:
        typer.secho(f"Nothing was saved: {exc}", fg="red")
        raise typer.Exit(1) from None
    parts = ["your settings"]
    if contents.vault:
        parts.append(f"the vault ({contents.values} values) with its key")
    if contents.history:
        parts.append("the dashboard's history")
    typer.echo(f"Saved {', '.join(parts)} in {file}.")
    typer.echo("Keep the passphrase: without it the file cannot be read, by anyone.")
    if contents.vault:
        typer.secho("The file holds your real values, encrypted: keep it where you would keep them.", fg="yellow")


@app.command()
def restore(
    file: Annotated[Path | None, typer.Argument(help="A file of `latet backup`.")] = None,
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Replace this machine's own vault and key (saved first, same passphrase)."),
    ] = False,
    paths: Annotated[
        list[str] | None,
        typer.Option("--paths", help="OLD=NEW: project folders under OLD are under NEW here (repeat it)."),
    ] = None,
    abort: Annotated[bool, typer.Option("--abort", help="Undo a restore that was cut off.")] = False,
) -> None:
    """Restore a backup here: your settings, and the vault with its key when it holds them.

    It reads and checks the whole file first, shows what it holds and what in its settings lowers
    protection compared with yours, and asks you. Kept from this machine: its port, its identity
    (added to the backup's), and nothing of capture_dir or api_key_file. With a vault, the daemon
    is stopped and started again around it; this machine's own vault and key, if it has values, are
    replaced only with --replace, after saving them in a backup of their own with the same
    passphrase. What it moves aside stays in a dated folder next to the vault.
    """
    _refuse_in_agent("restore")
    from latet.cli.agent import at_your_terminal
    from latet.config import data_dir, in_container
    from latet.service import daemon
    from latet.storage.backup import BackupError, read_backup, restoring
    from latet.storage.keys import keyring_state

    data = data_dir()
    if abort:
        _abort(data)
        return
    if file is None:
        raise typer.BadParameter("give the backup file (or --abort)", param_hint="FILE")
    if restoring(data):
        typer.secho("A restore was cut off: undo it first, with `latet restore --abort`.", fg="red")
        raise typer.Exit(2)
    if not at_your_terminal(strict=True):
        typer.secho("`latet restore` asks you to confirm what it does: run it in a terminal of your own.", fg="red")
        raise typer.Exit(2)
    if os.environ.get("LATET_ROOT_KEY") or os.environ.get("LATET_ROOT_KEY_FILE"):
        typer.secho(
            "Here the root key comes from LATET_ROOT_KEY or LATET_ROOT_KEY_FILE, which a restored key could "
            "not replace: unset it first.",
            fg="red",
        )
        raise typer.Exit(2)
    if keyring_state() == "unreachable":
        typer.secho(
            "The system's keyring cannot be reached (a session without it?): the key could not be saved there. "
            "Restore from a desktop session, or set LATET_KEYRING=off to keep the key in a file.",
            fg="red",
        )
        raise typer.Exit(2)
    if in_container() and daemon.status().running:
        typer.secho(
            "Stop the container first, `docker compose stop`, then restore with "
            "`docker compose run --rm -it latet latet restore FILE`.",
            fg="red",
        )
        raise typer.Exit(2)
    passphrase = _passphrase(new=False)
    try:
        opened = read_backup(file, passphrase, data)
    except (BackupError, OSError) as exc:
        typer.secho(f"Nothing was restored: {exc}.", fg="red")
        raise typer.Exit(1) from None
    try:
        restored = _restored_config(opened.config, paths or [])
        _summary(opened, restored, data)
        replacing = opened.contents.vault and _has_values(data)
        if replacing and not replace:
            typer.secho(
                "This machine's vault has values of its own: --replace replaces it (it is saved first, with this "
                "passphrase).",
                fg="red",
            )
            raise typer.Exit(2)
        if not typer.confirm("Restore it?", default=False):
            typer.echo("Nothing was restored.")
            raise typer.Exit(1)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        before = _save_before(data, passphrase, stamp) if replacing else None
        if opened.contents.vault:
            _restore_vault(opened, restored, data, stamp, before)
        else:
            _restore_settings(opened, restored, data, stamp)
    finally:
        opened.discard()  # what was not moved into place
    typer.echo(NOT_MOVED)


# --- the steps ----------------------------------------------------------------------------------------


def _restored_config(raw: bytes | None, remaps: list[str]) -> Any:
    """The backup's settings for this machine: its port, its identity added, no paths of the other one."""
    import yaml
    from pydantic import ValidationError

    from latet.config import Config, _yaml_error_at, config_path, detect_identity, in_container, load_yaml
    from latet.projects import norm_path

    text = ""
    try:
        text = raw.decode("utf-8") if raw else ""
        config = Config.model_validate(load_yaml(text) or {})
    except yaml.YAMLError as exc:  # where, never what: PyYAML's words quote the line
        typer.secho(f"Nothing was restored: its settings are not valid YAML{_yaml_error_at(exc, text)}.", fg="red")
        raise typer.Exit(1) from None
    except (ValueError, ValidationError) as exc:
        typer.secho(f"Nothing was restored: its settings cannot be read ({exc}).", fg="red")
        raise typer.Exit(1) from None
    here_port = 5283
    if config_path().exists():
        with_port = load_yaml(config_path().read_text("utf-8")) or {}
        if isinstance(with_port, dict) and isinstance(with_port.get("port"), int):
            here_port = with_port["port"]
    config.port = here_port
    config.capture_dir = None
    config.llm_review = config.llm_review.model_copy(update={"api_key_file": None})
    if not in_container():  # there the container's user is not yours: the backup's identity stays
        here, mine = detect_identity(), config.identity
        names = [*mine.usernames, *([mine.username] if mine.username else []), *here.usernames]
        config.identity = mine.model_copy(
            update={
                "usernames": list(dict.fromkeys(names)),
                "username": None,
                "emails": list(dict.fromkeys([*mine.emails, *here.emails])),
                "hostname": here.hostname or mine.hostname,
                "full_name": mine.full_name or here.full_name,
            }
        )
    pairs = []
    for spec in remaps:
        old, sep, new = spec.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise typer.BadParameter(f"{spec!r}: OLD=NEW", param_hint="--paths")
        pairs.append((norm_path(old), new.strip().rstrip("/\\")))
    for entry in config.projects:
        path = norm_path(entry.path)
        for old, new in pairs:
            if path == old or path.startswith(old.rstrip("/") + "/"):
                entry.path = new + entry.path.replace("\\", "/")[len(old) :]
                break
    return config


def _summary(opened: Any, restored: Any, data: Path) -> None:
    from latet.protection import loosened

    contents = opened.contents
    typer.echo(f"A backup of {contents.created}, by Latet {contents.version} on {contents.platform}:")
    typer.echo(f"  your settings{'' if opened.config else ': none, the defaults'}")
    if contents.vault:
        typer.echo(f"  the vault, {contents.values} values, with its root key")
    if contents.history:
        typer.echo("  the dashboard's history")
    if contents.certificates:
        typer.echo(f"  {contents.certificates} certificate(s) the local reviewer trusts")
    reasons = loosened(_current(data), restored)
    if reasons:
        typer.secho("Its settings lower protection compared with yours:", fg="yellow")
        for reason in reasons:
            typer.echo(f"  {reason}")
    review = restored.llm_review
    if review.enabled and review.remote:
        typer.secho(
            f"Its local reviewer is on another machine, {review.url}: it would be sent your original texts.",
            fg="yellow",
        )
    from latet.config import in_container

    if not in_container():
        missing = [e.path for e in restored.projects if not Path(e.path).exists()]
        if missing:
            typer.secho(
                f"Project folders not on this machine ({len(missing)}): --paths OLD=NEW moves them.", fg="yellow"
            )
            for path in missing[:10]:
                typer.echo(f"  {path}")


def _current(data: Path) -> Any:
    """The settings this machine runs with: those you approved last, else config.yaml."""
    from latet.config import Config, load_config
    from latet.runtime import REVIEWED_FILE

    try:
        return Config.model_validate(json.loads((data / REVIEWED_FILE).read_text("utf-8")))
    except (OSError, ValueError):
        return load_config()


def _has_values(data: Path) -> bool:
    import sqlite3

    path = data / "latet.db"
    if not path.exists():
        return False
    db = sqlite3.connect(path)
    try:
        return bool(db.execute("SELECT count(*) FROM entities").fetchone()[0])
    except sqlite3.Error:
        return True  # there, but not readable as a vault: never thrown away
    finally:
        db.close()


def _save_before(data: Path, passphrase: str, stamp: str) -> Path:
    from latet.config import config_path
    from latet.storage.backup import write_backup
    from latet.storage.keys import load_root_key

    before = data / f"before-restore-{stamp}.latet"
    history = (data / "exchanges.db").exists()
    write_backup(before, passphrase, data, config_path(), vault=True, history=history, root_key=load_root_key(data))
    typer.echo(f"This machine's own vault and key are saved first in {before}, with the same passphrase.")
    return before


def _journal(data: Path, **fields: Any) -> None:
    from latet.config import write_atomic
    from latet.storage.backup import RESTORE_JOURNAL

    path = data / RESTORE_JOURNAL
    current = json.loads(path.read_text("utf-8")) if path.exists() else {}
    write_atomic(path, json.dumps({**current, **fields}, indent=1))


def _write_settings(opened: Any, restored: Any, data: Path, aside: Path) -> None:
    from latet.config import config_path, load_config, save_config, write_private_where_allowed
    from latet.runtime import REVIEWED_FILE

    if config_path().exists():
        aside.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config_path(), aside / "config.yaml")
    save_config(restored)
    if opened.certificates:
        from latet.storage.folders import make_private_folder

        folder = make_private_folder(data / "certificates")  # read only from a folder that is yours alone
        for name, pem in opened.certificates.items():
            (folder / name).write_bytes(pem)
    # Confirmed at your own terminal: these are the settings you approved. Written as the daemon writes them, for you
    # alone where the system allows it (they hold your terms and identity), and kept all the same where it does not.
    write_private_where_allowed(data / REVIEWED_FILE, json.dumps(load_config().model_dump(mode="json"), indent=1))


def _restore_settings(opened: Any, restored: Any, data: Path, stamp: str) -> None:
    from latet.cli.admin import _reload_answer, say_refusal

    aside = data / f"before-restore-{stamp}"
    _write_settings(opened, restored, data, aside)
    answer = _reload_answer()
    taken = answer is not None and "error" not in answer
    typer.echo("Restored your settings" + (", and the daemon uses them." if taken else "."))
    if answer is not None and not taken:
        say_refusal(answer)
    if aside.exists():
        typer.echo(f"Your previous config.yaml is in {aside}.")


def _restore_vault(opened: Any, restored: Any, data: Path, stamp: str, before: Path | None) -> None:
    from latet.service import daemon
    from latet.storage.keys import save_root_key

    aside = data / f"before-restore-{stamp}"
    _journal(data, stamp=stamp, aside=str(aside), before=str(before) if before else None, key=False)
    was = daemon.status()
    if was.running:
        typer.echo("Stopping the daemon...")
        pid = was.pid
        daemon.stop()
        if pid and not daemon.wait_exit(pid):
            typer.secho(
                "The daemon did not stop: nothing was changed. `latet restore --abort`, then try again.", fg="red"
            )
            raise typer.Exit(1)
    aside.mkdir(parents=True, exist_ok=True)
    for name in _DATABASES:
        for side in _SIDE:
            if (data / f"{name}{side}").exists():
                os.replace(data / f"{name}{side}", aside / f"{name}{side}")
    for name, staged in list(opened.staged.items()):
        os.replace(staged, data / name)
        del opened.staged[name]
    _write_settings(opened, restored, data, aside)
    assert opened.root_key is not None
    where = save_root_key(data, opened.root_key, aside)
    _journal(data, key=True)
    from latet.storage.backup import RESTORE_JOURNAL

    (data / RESTORE_JOURNAL).unlink()
    typer.echo(f"Restored: the vault and its key ({'in the keyring' if where == 'keyring' else 'in root.key'}).")
    typer.echo(f"What this machine had is in {aside}.")
    if was.running:
        started = daemon.start()
        if not started.running:
            typer.secho("The daemon did not start again: `latet start`.", fg="yellow")
            return
        if started.unreadable:
            typer.secho(
                f"{started.unreadable} values of the vault cannot be read with the key: `latet doctor`.", fg="red"
            )
        from latet.cli.admin import refreshed

        refreshed("applied again")  # Claude Code's settings hold your user name's fake, made with the old key


def _abort(data: Path) -> None:
    from latet.config import config_path
    from latet.storage.backup import RESTORE_JOURNAL

    journal = data / RESTORE_JOURNAL
    if not journal.exists():
        typer.echo("There is no restore to undo.")
        return
    info = json.loads(journal.read_text("utf-8"))
    aside = Path(info.get("aside") or "")
    if aside.is_dir():
        for name in _DATABASES:
            for side in _SIDE:
                if (aside / f"{name}{side}").exists():
                    os.replace(aside / f"{name}{side}", data / f"{name}{side}")
                elif side and (data / f"{name}{side}").exists():
                    (data / f"{name}{side}").unlink()
        if (aside / "config.yaml").exists():
            shutil.copy2(aside / "config.yaml", config_path())
    for name in _DATABASES:
        (data / f".{name}.restoring").unlink(missing_ok=True)
    journal.unlink()
    typer.echo("The restore is undone: `latet start` starts the daemon again.")
    if info.get("key") and info.get("before"):
        typer.secho(
            f"The root key had been replaced already: to have this machine's own back, "
            f"`latet restore {info['before']} --replace`.",
            fg="yellow",
        )
