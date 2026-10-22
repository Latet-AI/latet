"""Install and maintenance commands: global-install and global-uninstall, doctor, agents, service, ner,
reload, upgrade."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Annotated, Any

import typer

from latet.cli.main import app

if TYPE_CHECKING:
    from latet.config import AutoModeReview, Config
    from latet.server.connection import _PreparedConnection

agents_app = typer.Typer(help="Configure agents to go through Latet (global mode).", no_args_is_help=True)
service_app = typer.Typer(help="Start Latet at login.", no_args_is_help=True)
ner_app = typer.Typer(help="Optional local detection of names and addresses.", no_args_is_help=True)
app.add_typer(agents_app, name="agents")
app.add_typer(service_app, name="service")
app.add_typer(ner_app, name="ner")

_SYMBOLS = {"ok": ("OK  ", "green"), "warn": ("WARN", "yellow"), "fail": ("FAIL", "red")}
_AGENTS_PRINT = "`docker exec latet latet agents print` shows how to point your agents at it."


def settings_here(group: str, command: str | None = None) -> None:
    """Connected settings live in the person's server space; only the explicit file/schema/send exceptions run here."""
    from latet.server.connection import _prepare_connection

    prepared = _prepare_connection()
    if (group, command) in (("rules", "test"), ("config", "schema"), ("identity", "send")):
        return
    if prepared.connection is not None:
        typer.secho(
            f"This computer uses {prepared.connection.url}: change these settings with `latet dashboard`.",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)


def _yourself(command: str, why: str = "takes protection away from your agents") -> None:
    """Refuse a command that takes protection away when an agent runs it: you run it yourself. ``why``: what the
    command does that is yours to decide."""
    from latet.cli.agent import agent_name

    agent = agent_name()
    if agent is not None:
        typer.secho(
            f"`{command}` {why}: run it yourself, in a terminal of your own (it was run by {agent}).",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)


def _records_trusted() -> None:
    """Refuse unavailable history before any configuration, plugin or service side effect, even disconnected."""
    from latet.integrations.base import RecordsUnavailable, records_preflight

    try:
        records_preflight(write=True)
    except RecordsUnavailable as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from None


def _host_only(what: str, then: str | None = _AGENTS_PRINT) -> None:
    """Commands that run on this machine, on its agents, services or browser: not from inside the container.
    ``then``: what to do instead, as a line (None: nothing to add)."""
    from latet.config import in_container

    if in_container():
        typer.secho(f"{what} runs on your computer, not in the container.", fg="red", err=True)
        if then:
            typer.echo(then, err=True)
        raise typer.Exit(2)


def _reload_daemon() -> bool:
    """Ask a running daemon to rebuild its rules (new model, edited config file). One that refuses says why."""
    answer = _reload_answer()
    if answer is not None and "error" in answer:
        say_refusal(answer)
        return False
    return answer is not None


def _admin(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict | None:
    """The running daemon's answer to one of the CLI's own calls (None: it did not answer)."""
    import json
    import urllib.error
    import urllib.request

    from latet.cli.agent import agent_name
    from latet.config import data_dir, load_config
    from latet.service.daemon import local_urlopen
    from latet.storage.keys import load_bridge_token

    headers = {"x-latet-token": load_bridge_token(data_dir()), "content-type": "application/json"}
    headers |= {"x-latet-command": "1", "x-latet-agent": agent_name() or ""}
    request = urllib.request.Request(
        f"http://127.0.0.1:{load_config().port}{path}",
        data=None if method == "GET" else json.dumps(body or {}).encode(),
        method=method,
        headers=headers,
    )
    try:
        with local_urlopen(request, timeout) as response:
            answer = json.loads(response.read() or b"{}")
            return answer if isinstance(answer, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            answer = json.loads(exc.read() or b"{}")
        except ValueError:
            answer = {}
        refused = {"error": answer.get("error") or f"HTTP {exc.code}", "status": exc.code}
        reason = answer.get("reason") if isinstance(answer, dict) else None
        if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason):  # a code, never words to print
            refused["reason"] = reason
        return refused
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _reloaded() -> dict | None:
    """The running daemon's answer to a reload, as it gave it (None: it did not answer, it is not running). One that
    refused the reload holds ``error``, why in words that hold no value (``_refusal``), and nothing else."""
    answer = _admin("POST", "/_latet/admin/reload")
    if answer is not None and "error" in answer:
        return {"error": _refusal(answer)}
    return answer


def _refusal(answer: dict) -> str:
    """Why the daemon refused a reload, as the CLI says it, which an agent that ran the command reads and sends on: the
    daemon's own words for settings it cannot read (400), which name places and never what the file holds; for any
    other refusal its status alone, since those words are not the reload's own and could hold anything (another program
    may answer on the port)."""
    status, words = answer.get("status"), answer.get("error")
    if status == 400 and isinstance(words, str) and words.startswith("cannot read the configuration: "):
        return words
    if status == 401:
        return "it is another data directory's daemon (it did not take this one's token)"
    if status == 503 and answer.get("reason") == "repositories_unchecked":  # the CLI's own words, never the answer's
        return (
            "the repositories Latet has seen could not all be read now (a share that does not answer, a refused "
            "permission), and the change could lower their protection: nothing applied; reload again once they can "
            "be read"
        )
    if isinstance(status, int) and status >= 500:
        return f"it answered {status}: its daemon.log says where it failed"
    return f"it answered {status}" if isinstance(status, int) else "it answered with an error"


def say_refusal(answer: dict) -> None:
    """A reload the daemon refused (``_reloaded``), said as such: it is running, and applied nothing."""
    typer.secho(f"The daemon did not reload: {answer['error']}.", fg="red", err=True)


def say_notices(answer: dict) -> None:
    """What the daemon's load of config.yaml left out (an allow pattern RE2 cannot compile), each by its place."""
    from latet.config import config_path

    for notice in answer.get("notices") or []:
        typer.secho(f"{config_path().name}: {notice}.", fg="yellow")


def _reload_answer() -> dict | None:
    """The running daemon's answer to a reload (None: it did not answer). A change that lowers
    protection does not apply at once: at your own terminal you are asked; otherwise it waits for you
    in the dashboard. ``applied`` says whether it applied. A reload the daemon refused holds ``error``
    (``_reloaded``): nothing applied."""
    answer = _reloaded()
    if answer is None or "error" in answer:
        return answer
    say_notices(answer)
    pending = answer.get("pending")
    if pending:
        answer["applied"] = settle_change(pending)
    return answer


def settle_change(pending: dict) -> bool:
    """Ask you about a change that lowers protection (at your own terminal), or say it waits for you
    in the dashboard. True when it applied."""
    from latet.cli.agent import at_your_terminal
    from latet.gateway.approvals import lowered_lines, waiting_lines

    header, *reasons = lowered_lines(pending)
    typer.secho(header, fg="yellow")
    for reason in reasons:
        typer.echo(reason)
    if at_your_terminal():
        approve = typer.confirm("Apply it?", default=False)
        answer = _admin(
            "POST", f"/_latet/admin/changes/{pending['id']}", {"action": "approve" if approve else "reject"}
        )
        if answer is None or "error" in answer:
            typer.secho(f"Not applied: {(answer or {}).get('error', 'the daemon did not answer')}.", fg="red")
            return False
        if not approve:
            typer.echo("Not applied: config.yaml is as it was.")
        return approve
    for line in waiting_lines(pending):
        typer.echo(line)
    return False


def wait_for_change(change_id: str, timeout: float = 600) -> str | None:
    """How a change that waits for you ended: approved, rejected, stale, superseded; None: no answer."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = _admin("GET", f"/_latet/admin/changes/{change_id}", timeout=10)
        if answer is not None and not answer.get("waiting", True):
            return answer.get("state")
        time.sleep(2)
    return None


def print_recognizer_problems(problems: list[dict]) -> None:
    for problem in problems:
        for error in problem.get("errors", []):
            typer.secho(f"  recognizer {problem['name']} turned off: {error}", fg="red")
        for warning in problem.get("warnings", []):
            typer.secho(f"  recognizer {problem['name']}: {warning}", fg="yellow")


@app.command()
def reload() -> None:
    """Apply the configuration file to the running daemon, without a restart."""
    settings_here("reload")
    answer = _reload_answer()
    if answer is None:
        typer.secho("The daemon is not running (or did not answer): `latet start`.", fg="yellow")
        raise typer.Exit(1)
    if "error" in answer:
        say_refusal(answer)
        raise typer.Exit(1)
    if not answer.get("applied", True):
        raise typer.Exit(3)
    typer.echo("Rules reloaded.")
    if answer.get("recognizers"):
        print_recognizer_problems(answer["recognizers"])
        typer.echo("`latet rules test` explains each one.")


def _install_ner(yes: bool) -> bool:
    from latet.config import data_dir
    from latet.engine.detectors import ner
    from latet.models import ModelError, install

    if not ner.runtime_available():
        typer.echo("Local name detection needs the `ner` extra (ONNX Runtime and a tokenizer), which this Latet")
        typer.echo("was installed without. `latet upgrade` installs the latest release with it.")
        return False
    spec = ner.MODEL
    typer.echo(f"Model: {spec.repo} ({spec.license} license, {spec.homepage})")
    typer.echo(f"Download: {spec.size / 1e6:.0f} MB from huggingface.co, once; it then runs on this machine only.")
    if not yes and not typer.confirm("Download it now?", default=True):
        return False
    with typer.progressbar(length=spec.size, label="Downloading") as bar:
        seen = [0]

        def advance(done: int, _total: int) -> None:
            bar.update(done - seen[0])
            seen[0] = done

        try:
            install(data_dir(), progress=advance)
        except (ModelError, OSError) as exc:
            typer.secho(f"Download failed: {exc}", fg="red", err=True)
            return False
    typer.echo("Installed and verified.")
    if _reload_daemon():
        typer.echo("The running daemon now detects names and addresses.")
    return True


@ner_app.command("install")
def ner_install(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask before downloading.")] = False,
) -> None:
    """Download the local name and address model (once; it then works offline)."""
    if not _install_ner(yes):
        raise typer.Exit(1)


@ner_app.command("status")
def ner_status() -> None:
    """Show whether local name detection is available and in use."""
    from latet.config import data_dir, load_config
    from latet.engine.detectors import ner

    config = load_config()
    runtime = ner.runtime_available()
    installed = ner.is_installed(data_dir())
    typer.echo(f"runtime (the `ner` extra): {'installed' if runtime else 'missing'}")
    typer.echo(f"model: {'installed' if installed else 'not downloaded'} ({ner.model_dir(data_dir())})")
    active = runtime and installed and config.ner == "auto" and config.profile.value != "secrets"
    where = "everywhere" if config.profile.value == "max" else "in your prompts and attachments"
    state = f"yes, {where}" if active else "no"
    typer.echo(f"in use: {state} (setting `ner: {config.ner}`, profile {config.profile.value})")


@ner_app.command("remove")
def ner_remove() -> None:
    """Delete the downloaded model."""
    from latet.config import data_dir
    from latet.models import remove

    typer.echo("Model removed." if remove(data_dir()) else "No model to remove.")
    _reload_daemon()


@app.command()
def doctor() -> None:
    """Check the daemon, the plugin bridge, masking and the agent integrations."""
    from latet.doctor.checks import run_all

    worst = "ok"
    for check in run_all():
        label, color = _SYMBOLS[check.status]
        typer.secho(f"[{label}] ", fg=color, nl=False)
        typer.echo(f"{check.title}: {check.detail}")
        if check.status == "fail" or (check.status == "warn" and worst == "ok"):
            worst = check.status
    raise typer.Exit(1 if worst == "fail" else 0)


def _targets(names: list[str] | None) -> list:
    from latet.integrations.agents import ALL

    if not names or names == ["all"]:
        return [a for a in ALL.values() if a.detect().installed]
    unknown = [n for n in names if n not in ALL]
    if unknown:
        raise typer.BadParameter(f"unknown agent(s): {', '.join(unknown)} (choose from {', '.join(ALL)})")
    return [ALL[n] for n in names]


@agents_app.command("status")
def agents_status() -> None:
    """Show which agents are installed and whether they go through Latet."""
    from latet.config import load_config
    from latet.integrations.agents import ALL, ClaudeCode, Codex

    for agent in ALL.values():
        info = agent.detect()
        state = agent.status() if info.installed else "not installed"
        typer.echo(f"{info.name:12s} {info.version or '':28s} {state}")
        if info.installed and isinstance(agent, (ClaudeCode, Codex)):
            route = agent.route(load_config())
            typer.echo(f"{'':12s} route: {route.problem or route.summary}")
        for note in info.notes:
            typer.echo(f"{'':12s} note: {note}")


@agents_app.command("apply")
def agents_apply(
    names: Annotated[list[str] | None, typer.Argument(help="claude, codex, opencode (default: all installed).")] = None,
    keep_classifier_context: Annotated[
        bool, typer.Option(help="Claude Code: keep auto mode's server-side review (sends paths and OS user).")
    ] = False,
) -> None:
    """Route the agents through Latet (backed up, revertible with `agents revert`)."""
    from latet.integrations.agents import ClaudeCode, ClaudeOptions, RouteProblem

    _host_only("`agents apply`")
    _records_trusted()

    for agent in _targets(names):
        try:
            if isinstance(agent, ClaudeCode):
                record = agent.apply(ClaudeOptions(disable_classifier_context=not keep_classifier_context))
                typer.echo(f"{agent.name}: applied")
                _warn_surrogate(record.data)
            else:
                agent.apply()
                typer.echo(f"{agent.name}: applied")
        except RouteProblem as problem:
            typer.secho(f"{agent.name}: left as it is ({problem})", fg="yellow")
            continue
        say_applied(agent)


def say_applied(agent: Any) -> None:
    """What an agent just applied for good has to say, a line each: on a computer connected to a Latet server, where
    this computer's token or the space's address now is (or why not), and why the agent will not reach a server whose
    self-signed certificate it reads from nowhere."""
    from latet import wrappers

    for line in getattr(agent, "said", []):
        typer.secho(f"  {line}", fg="yellow")
    if (why := wrappers.untrusting(agent.id, for_good=True)) is not None:
        typer.secho(f"  {why}.", fg="yellow")


def _warn_surrogate(applied: dict) -> None:
    """``auto_mode_review.user_name: surrogate`` needs the daemon to tell the surrogate (on a computer connected to a
    Latet server, the space's settings decide)."""
    from latet import wrappers
    from latet.config import load_config

    if wrappers.connected() is not None:
        return
    if load_config().auto_mode_review.user_name == "surrogate" and not applied.get("user_name_surrogate"):
        typer.secho(
            "  Claude Code keeps your real user name: the daemon did not give its surrogate. "
            "Run `latet start`, then `latet agents apply claude`.",
            fg="yellow",
        )


def _parse_review(value: str) -> AutoModeReview:
    """``--auto-mode-review``: `block`, `allow-identity`, or the details to let through, such as
    `user_name,paths` (`user_name=surrogate` for the surrogate user name)."""
    from pydantic import ValidationError

    from latet.config import REVIEW_DETAILS, AutoModeReview

    hint = (
        f"block, allow-identity, or details to allow among {', '.join(REVIEW_DETAILS)} "
        "(comma-separated; user_name=surrogate for a surrogate user name)"
    )
    value = value.strip()
    if value in ("block", "allow-identity"):
        return AutoModeReview.model_validate(value)
    choices: dict[str, str] = {}
    for item in value.split(","):
        name, _, choice = item.strip().partition("=")
        name = name.strip().replace("-", "_")
        if name not in REVIEW_DETAILS:
            raise typer.BadParameter(hint, param_hint="--auto-mode-review")
        choices[name] = choice.strip() or "allow"
    try:
        return AutoModeReview.model_validate(choices)
    except ValidationError:
        raise typer.BadParameter(hint, param_hint="--auto-mode-review") from None


class NotAllApplied(Exception):
    """Agents ``refresh_applied`` could not apply again, each said already with its own words: their names, and those
    of the agents it did apply again."""

    def __init__(self, names: list[str], done: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names, self.done = names, done


def refresh_applied(skip: set[str] | None = None) -> list[str]:
    """Apply again what is applied, with the same choices (a new port, a new plugin version, a connection to a Latet
    server), each agent on its own: one whose route Latet can no longer stand in front of is left as it is, one that
    fails (its own tool gone, a connection's folder or helper binding refused) is said with the words of its refusal,
    and the others go on (R8). What each has to say (where this computer's token now is, ...) is said. Raises
    NotAllApplied, once every agent was tried, when one failed."""
    from latet.integrations.agents import ALL, ClaudeCode, RouteProblem
    from latet.integrations.base import RecordsUnavailable, load_records
    from latet.logline import described
    from latet.server.connection import NotConnected

    done, failed = [], []
    for agent_id in load_records():
        agent = ALL.get(agent_id)
        if agent is None or agent_id in (skip or set()):
            continue
        try:
            if isinstance(agent, ClaudeCode):
                _warn_surrogate(agent.apply(agent.applied_options()).data)
            else:
                agent.apply()
        except RouteProblem as problem:
            typer.secho(f"{agent.name}: left as it is ({problem})", fg="yellow")
            continue
        except Exception as exc:  # one agent's failure never stops the others' (C-I4)
            why = exc.message if isinstance(exc, NotConnected) else described(exc)
            if isinstance(exc, RecordsUnavailable):
                why = str(exc)
            typer.secho(f"{agent.name}: not applied again ({why}): `latet agents apply {agent_id}`", fg="yellow")
            failed.append(agent.name)
            continue
        say_applied(agent)
        done.append(agent.name)
    if failed:
        raise NotAllApplied(failed, done)
    return done


def refreshed(said: str, skip: set[str] | None = None) -> bool:
    """What is applied, applied again (``refresh_applied``), each agent said with ``said``; one that failed is said in
    its own words there, and here with what applies it. Whether all were."""
    failed: list[str] = []
    try:
        names = refresh_applied(skip=skip)
    except NotAllApplied as exc:
        names, failed = exc.done, exc.names
    for name in names:
        typer.echo(f"{name}: {said}")
    if failed:
        typer.secho(f"Not applied again: {', '.join(failed)}. `latet agents apply` applies them.", fg="yellow")
    return not failed


@agents_app.command("refresh", hidden=True)
def agents_refresh() -> None:
    """Apply again what is applied, with the same choices (after an upgrade: the new plugin)."""
    if not refreshed("refreshed"):
        raise typer.Exit(1)


@agents_app.command("revert")
def agents_revert(
    names: Annotated[list[str] | None, typer.Argument(help="claude, codex, opencode (default: all).")] = None,
) -> None:
    """Undo what `agents apply` changed (byte for byte when the files were not edited since)."""
    _yourself("latet agents revert")
    _records_trusted()
    from latet.integrations.agents import ALL

    targets = [ALL[n] for n in names] if names else list(ALL.values())
    for agent in targets:
        typer.echo(f"{agent.name}: {agent.revert()}")


@service_app.command("install")
def service_install() -> None:
    """Start Latet automatically at login (no administrator rights needed)."""
    from latet.service import autostart

    _host_only("`service install`")

    typer.echo(f"installed: {autostart.install()}")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Stop starting Latet at login."""
    _yourself("latet service uninstall")
    from latet.service import autostart

    typer.echo(autostart.uninstall())


@app.command("global-uninstall")
def global_uninstall() -> None:
    """Take Latet out of your agents for good: what `latet global-install` did, undone.

    Every agent's settings go back as they were, byte for byte; Latet no longer starts at login;
    the daemon stops. Your rules stay in config.yaml, for `latet claude` and the others.
    """
    from latet.integrations.agents import ALL
    from latet.service import autostart, daemon

    _yourself("latet global-uninstall")
    _records_trusted()
    for agent in ALL.values():
        typer.echo(f"{agent.name}: {agent.revert()}")
    typer.echo(f"autostart: {autostart.uninstall()}")
    typer.echo("daemon: stopped" if daemon.stop() else "daemon: still running")


@app.command()
def upgrade() -> None:
    """Install the latest release of Latet from GitHub, with local name detection and the text in images.

    It stops the daemon first, so Windows can replace its files, and starts it again after. Where name
    detection and the text in images cannot be installed, a Latet that has neither is installed without
    them; one that has them is left as it was.
    """
    _host_only("`upgrade` (pull the new image instead)")
    from latet import releases
    from latet.engine.detectors import ner
    from latet.service import daemon
    from latet.vision import models

    uv = shutil.which("uv")
    if uv is None:
        typer.secho("uv not found: upgrade with your installer, then run `latet agents apply`.", fg="yellow")
        raise typer.Exit(1)
    try:
        release = releases.latest()
    except releases.ReleaseError as exc:
        typer.secho(f"Could not look up the latest release: {exc}.", fg="red")
        typer.echo(
            f"Nothing changed. The releases are at {releases.PAGE}: to install one by hand, run the installer with "
            "LATET_SOURCE set to its wheel (see Troubleshooting, The latest release cannot be looked up)."
        )
        raise typer.Exit(1) from None
    # Installed without them, Latet would lose what it has: uv takes away what the new requirement does not name.
    had = ner.runtime_available() or models.runtime_available()
    typer.echo(f"Latet {release.version}, from {release.wheel}")
    daemon.stop()
    install = [uv, "tool", "install", "--upgrade", "--python", ">=3.12"]
    done = subprocess.run([*install, release.requirement()], check=False)  # noqa: S603
    off = False  # installed without name detection and the text in images
    if done.returncode != 0 and not had:
        typer.secho(
            "Name detection and the text in images cannot be installed here: installing Latet without them.",
            fg="yellow",
        )
        done = subprocess.run([*install, release.requirement(extras=False)], check=False)  # noqa: S603
        off = True
    if done.returncode != 0:
        daemon.start()
        if had:
            typer.secho(
                "The upgrade failed: uv could not install it with name detection and the text in images, which "
                "this Latet has. Latet is as it was.",
                fg="red",
            )
        else:
            typer.secho("The upgrade failed: Latet is as it was.", fg="red")
        raise typer.Exit(1)
    # The new version re-applies the integrations (the new plugin, the same choices): this process
    # still runs the old code.
    new = shutil.which("latet")
    if new is not None:
        subprocess.run([new, "agents", "refresh"], check=False)  # noqa: S603
        subprocess.run([new, "start"], check=False)  # noqa: S603
    else:
        daemon.start()
    typer.echo(f"Latet {release.version} installed.")
    if off:
        typer.secho(
            "Off: name detection and the text in images, which uv could not install here. `latet upgrade` tries again.",
            fg="yellow",
        )


def _install_port(current: int, option: str | None) -> tuple[int, str | None]:
    """The port global-install keeps, and why it moves: the current one when it can be used, else a
    free one (the agents follow)."""
    from latet.service import ports

    if option not in (None, "auto"):
        try:
            wanted = int(option)
        except ValueError:
            raise typer.BadParameter("a port number, or auto", param_hint="--port") from None
        state = ports.probe(wanted)
        if not state.usable:
            typer.secho(f"Port {wanted} is {state.reason}.", fg="red", err=True)
            raise typer.Exit(1)
        return wanted, None
    state = ports.probe(current)
    if state.usable:
        return current, None
    try:
        free = ports.choose(current)
    except ports.NoFreePortError:
        typer.secho(f"Port {current} is {state.reason}, and so is every port up to 5335.", fg="red", err=True)
        typer.echo(
            "Pick one with `latet global-install --port <number>` (see `netsh int ipv4 show excludedportrange`)."
        )
        raise typer.Exit(1) from None
    return free.port, f"{free.port}: {current} is {state.reason}, and your agents move with it"


def _install_regions(config: Config, option: str | None) -> list[str]:
    """The countries whose ID numbers to recognise: the option; else what config.yaml says; else this
    machine's country with the United States and the UK."""
    from pydantic import ValidationError

    from latet.config import ID_REGIONS, Config, config_path, load_yaml, system_country

    if option is not None:
        try:
            return Config(id_regions=[r for r in option.replace(" ", ",").split(",") if r]).id_regions
        except ValidationError:
            hint = f"country codes among {', '.join(ID_REGIONS)}"
            raise typer.BadParameter(hint, param_hint="--id-regions") from None
    written = load_yaml(config_path().read_text("utf-8")) if config_path().exists() else None
    country = system_country() or ""
    if (isinstance(written, dict) and "id_regions" in written) or country not in ID_REGIONS:
        return list(config.id_regions)
    return list(dict.fromkeys([country, "US", "GB"]))


@app.command("global-install")
def global_install(
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Apply it without asking (an agent or a script, once the user agreed).")
    ] = False,
    profile: Annotated[str | None, typer.Option(help="balanced, max or secrets.")] = None,
    identity: Annotated[
        bool | None,
        typer.Option("--identity/--no-identity", help="Mask this machine's user names, host name and git author."),
    ] = None,
    agents: Annotated[
        str | None,
        typer.Option(
            help="The agents to route: claude, codex, opencode (comma-separated), or none. Not given: every "
            "installed one."
        ),
    ] = None,
    ner_model: Annotated[
        bool | None,
        typer.Option("--ner/--no-ner", help="Download the local name and address model (needs the ner extra)."),
    ] = None,
    ocr_models: Annotated[
        bool | None,
        typer.Option("--ocr/--no-ocr", help="Download the text recognition models for images (needs the ocr extra)."),
    ] = None,
    auto_mode_review: Annotated[
        str | None,
        typer.Option(
            help="Claude Code's auto mode reviewer: block, allow-identity, or the details to let through "
            "(user_name, paths, host_name, git_author; user_name=surrogate gives it a fake user name)."
        ),
    ] = None,
    start_at_login: Annotated[
        bool | None, typer.Option("--autostart/--no-autostart", help="Start Latet when you log in (default: yes).")
    ] = None,
    open_dashboard: Annotated[
        bool | None, typer.Option("--dashboard/--no-dashboard", help="Open the dashboard at the end.")
    ] = None,
    port: Annotated[
        str | None,
        typer.Option(help="The daemon's port, or auto: keep it, or move to a free one if it cannot be used."),
    ] = None,
    id_regions: Annotated[
        str | None,
        typer.Option(
            help="Countries whose ID and tax numbers to recognise, comma-separated (such as IT,DE,US); "
            "the default adds this machine's country."
        ),
    ] = None,
) -> None:
    """Route your agents through Latet for good: every session, in the terminal and in the IDE.

    It shows everything it would change, then asks once: the agents it routes (their settings backed
    up first), Latet started at login, the profile and the countries, what is masked as your
    identity, Claude Code's auto mode reviewer, and the port. `latet global-uninstall` takes it all
    away. The options choose otherwise than the defaults, such as `--agents claude --no-autostart`.
    Run by an agent or a script, it needs --yes, once the user agreed: without a terminal of yours it
    changes nothing.
    """
    from latet.cli.agent import at_your_terminal
    from latet.config import Profile, data_dir, detect_identity, load_config, save_config
    from latet.engine.detectors import ner
    from latet.integrations.agents import ALL, ClaudeCode, ClaudeOptions, Codex, RouteProblem
    from latet.vision import models as ocr_models_spec

    _host_only("`global-install`")
    _records_trusted()
    from latet.service import autostart, daemon

    chosen: set[str] | None = None
    if agents is not None:
        chosen = {a.strip().lower() for a in agents.split(",") if a.strip() and a.strip().lower() != "none"}
        unknown = chosen - set(ALL)
        if unknown:
            raise typer.BadParameter(f"unknown agent(s): {', '.join(sorted(unknown))}", param_hint="--agents")
    from latet.server.connection import _prepare_connection, _prepare_token, _require_selection

    prepared = _prepare_connection()
    _require_selection(prepared)
    if prepared.connection is not None:
        prepared = _prepare_token(prepared)
        _require_selection(prepared)
        _global_install_on_server(prepared, yes, chosen)
        return
    config = load_config()
    review = _parse_review(auto_mode_review) if auto_mode_review is not None else config.auto_mode_review
    try:
        chosen_profile = Profile(profile) if profile is not None else config.profile
    except ValueError:
        raise typer.BadParameter("balanced, max or secrets", param_hint="--profile") from None
    old_port = config.port
    new_port, moved = _install_port(config.port, port)
    regions = _install_regions(config, id_regions)
    saved = config.identity
    written = bool(
        saved.usernames or saved.username or saved.hostname or saved.hostnames or saved.full_name or saved.emails
    )
    shown = saved if written else detect_identity()
    masked = saved.enabled if identity is None else identity
    autostart_on = True if start_at_login is None else start_at_login

    # What it would change, one row each.
    rows: list[tuple[str, str]] = []
    notes: list[str] = []
    selected = []
    for agent in ALL.values():
        info = agent.detect()
        if not info.installed:
            rows.append((info.name, "not installed" + (", skipped" if chosen and agent.id in chosen else "")))
            continue
        version = (info.version or "").replace(f"({info.name})", "").strip()  # "2.1.294 (Claude Code)"
        label = f"{info.name} {version}" if version else info.name
        if chosen is not None and agent.id not in chosen:
            rows.append((label, "left as it is"))
            continue
        if isinstance(agent, ClaudeCode):
            route = agent.route(config)
            if route.problem:  # Latet cannot stand in front of it: said, and left as it is
                rows.append((label, f"left as it is: {route.problem}"))
                continue
            rows.append((label, f"the Latet plugin, in every session; {route.summary} (its settings backed up first)"))
            notes += [route.note] if route.note else []
        elif isinstance(agent, Codex):
            codex_route = agent.route(config)
            if refused := codex_route.problem or agent.outdated():
                rows.append((label, f"left as it is: {refused}"))
                continue
            rows.append((label, f"{codex_route.summary} (its settings backed up first)"))
            notes += codex_route.notes
        else:
            rows.append((label, "its settings point at Latet (backed up first)"))
        selected.append(agent)
    rows.append(("Start at login", "yes" if autostart_on else "no"))
    if chosen_profile == Profile.SECRETS:
        rows.append(("Profile", "secrets: secrets and your own terms only"))
    else:
        countries = "every country" if chosen_profile == Profile.MAX else ", ".join(regions)
        strict = config.model_copy(update={"profile": chosen_profile}).data_mode() == "strict"
        rows.append(
            ("Profile", f"{chosen_profile.value}, ID numbers of {countries}" + (", strict data mode" if strict else ""))
        )
    if masked:
        details = [
            *shown.usernames, *([shown.username] if shown.username else []), shown.hostname, *shown.hostnames,
            shown.full_name,
        ]  # fmt: skip
        who = ", ".join(dict.fromkeys(d for d in [*details, *shown.emails] if d)) or "-"
        how = "as saved in config.yaml" if written else "detected each time Latet starts"
        rows.append(("Your identity", f"masked: {who} ({how})"))
    else:
        rows.append(("Your identity", "not masked (--identity masks it)"))
    if any(isinstance(agent, ClaudeCode) for agent in selected):
        allowed = sorted(review.allowed())
        if allowed:
            rows.append(("Auto mode reviewer", f"is sent your {', '.join(d.replace('_', ' ') for d in allowed)}"))
        else:
            rows.append(
                ("Auto mode reviewer", "your user name and paths stay local: auto mode cannot approve by itself")
            )
    if chosen_profile != Profile.SECRETS and not ner.is_installed(data_dir()):
        if not ner.runtime_available():
            rows.append(("Names and addresses", "need the ner extra: `latet upgrade` installs it"))
        elif ner_model:
            rows.append(("Names and addresses", "found by a local model, downloaded now (about 1.6 GB)"))
        else:
            rows.append(("Names and addresses", "no local model (--ner downloads it, about 1.6 GB)"))
    ocr_size = f"about {ocr_models_spec.size() / 1e6:.0f} MB"
    if chosen_profile == Profile.MAX:
        rows.append(("Text in images", "requests that carry images are not sent (the max profile)"))
    elif not ocr_models_spec.installed(data_dir()):
        if not ocr_models_spec.runtime_available():
            rows.append(("Text in images", "needs the ocr extra: `latet upgrade` installs it"))
        elif ocr_models:
            rows.append(("Text in images", f"read and masked by local models, downloaded now ({ocr_size})"))
        else:
            rows.append(
                ("Text in images", f"not read: images are sent as they are (--ocr downloads the models, {ocr_size})")
            )
    rows.append(("Port", moved or str(new_port)))

    typer.secho("latet global-install", bold=True)
    typer.echo("It routes your agents through Latet for good: every session, in the terminal and in the IDE,")
    typer.echo("without `latet claude`. What it changes:")
    typer.echo()
    width = max(len(name) for name, _ in rows)
    for name, what in rows:
        typer.echo(f"  {name:<{width}}  {what}")
    for note in notes:
        typer.echo(f"  Note: {note}")
    typer.echo()
    typer.echo("`latet global-uninstall` takes all of it away: your agents' settings back as they were, byte")
    typer.echo("for byte, no start at login, the daemon stopped. Your rules stay in config.yaml, for `latet claude`.")
    typer.echo()
    if not yes:
        if not at_your_terminal():
            typer.secho(
                "Nothing changed: run it in a terminal of your own, or add --yes once the user agreed.", fg="yellow"
            )
            raise typer.Exit(2)
        if not typer.confirm("Apply it?", default=False):
            typer.echo("Nothing changed.")
            raise typer.Exit(1)

    if new_port != old_port and port not in (None, "auto") and daemon.status(old_port).running:
        daemon.stop()  # the daemon moves too: started again below, on the new port
    config.port = new_port
    config.profile = chosen_profile
    config.id_regions = regions
    config.identity = saved.model_copy(update={"enabled": masked})  # empty: detected at every start
    config.auto_mode_review = review
    typer.echo(f"saved {save_config(config)}")
    if daemon.status().running:  # the daemon that stays; one stopped for a port move is not running
        # It takes the settings just saved before an agent registers a destination with it: it would add the
        # destination to the older settings it holds and save those over config.yaml. What lowers protection
        # waits there; the last reload below is the one that asks about it.
        _admin("POST", "/_latet/admin/reload")
    if ner_model and chosen_profile != Profile.SECRETS and not ner.is_installed(data_dir()):
        _install_ner(yes=True)
    if ocr_models and chosen_profile != Profile.MAX and not ocr_models_spec.installed(data_dir()):
        from latet.cli.ocr import install_ocr

        install_ocr(yes=True)
    applied_now: set[str] = set()
    for agent in selected:
        try:
            if isinstance(agent, ClaudeCode):
                # Always the client-side review: it travels in requests Latet checks (the server-side one
                # rides along every request in a field nothing can see).
                _warn_surrogate(agent.apply(ClaudeOptions(disable_classifier_context=True), config).data)
                typer.echo(f"{agent.name}: done. With Latet in the path it turns off Remote Control; claude.ai")
                typer.echo("  connectors (Gmail, Calendar...) keep receiving masked values only.")
            else:
                agent.apply()
                typer.echo(f"{agent.name}: done")
        except RouteProblem as problem:
            typer.secho(f"{agent.name}: left as it is ({problem})", fg="yellow")
            continue
        say_applied(agent)
        applied_now.add(agent.id)
    if config.port != old_port:  # agents set up earlier still point at the old port
        refreshed(f"now on port {config.port}", skip=applied_now)
    if autostart_on:
        autostart.install()
    running = daemon.status().running
    current = daemon.start()
    if running and current.running:
        reloaded = _reload_answer()  # the running daemon takes the new rules (what lowers protection is asked about)
        if reloaded is not None and "error" in reloaded:
            say_refusal(reloaded)
    typer.echo()
    typer.echo(f"daemon: {'running' if current.running else 'NOT running'} on 127.0.0.1:{current.port}")
    typer.echo("`latet doctor` checks everything, `latet dashboard` shows what reaches the model, and")
    typer.echo("`latet global-uninstall` takes it all away.")
    if current.running and open_dashboard:
        from latet.cli.main import dashboard

        dashboard()


def _global_install_on_server(prepared: _PreparedConnection, yes: bool, chosen: set[str] | None) -> None:
    """global-install on a computer connected to a Latet server: its agents only, to the server. Start at login, the
    port, the profile, the identity and the models are the server's business."""
    from latet.cli.agent import at_your_terminal
    from latet.config import load_config
    from latet.integrations.agents import ALL, ClaudeCode, ClaudeOptions, Codex, RouteProblem
    from latet.server.connection import _require_selection

    _require_selection(prepared)
    connection = prepared.connection
    assert connection is not None
    config = load_config()
    rows: list[tuple[str, str]] = []
    selected = []
    for agent in ALL.values():
        _require_selection(prepared)
        info = agent.detect()
        _require_selection(prepared)
        if not info.installed or (chosen is not None and agent.id not in chosen):
            continue
        route = None
        if isinstance(agent, ClaudeCode):
            route = agent.route(config, _prepared=prepared)
        elif isinstance(agent, Codex):
            route = agent.route(config)
        _require_selection(prepared)
        refused = route.problem if route is not None else None
        if isinstance(agent, Codex):
            refused = refused or agent.outdated()
        if refused:
            rows.append((info.name, f"left as it is: {refused}"))
            continue
        said = route.summary if route is not None else "its settings point at the server"
        if isinstance(agent, ClaudeCode):
            from latet import claude_bridge

            if (held := claude_bridge.platform_problem()) is not None:  # R5: said up front, before the question
                said = f"{said}; {held}"
        rows.append((info.name, said))
        selected.append(agent)
    typer.secho("latet global-install", bold=True)
    typer.echo(f"This computer is connected to {connection.url}, as {_printable(connection.user_name)}: your agents go")
    typer.echo("there in every session, and your settings are there (`latet dashboard`). What it changes:")
    for name, what in rows or [("Agents", "none installed")]:
        typer.echo(f"  {name}: {what}")
    if not yes:
        if not at_your_terminal():
            typer.secho(
                "Nothing changed: run it in a terminal of your own, or add --yes once the user agreed.", fg="yellow"
            )
            raise typer.Exit(2)
        if not typer.confirm("Apply it?", default=False):
            typer.echo("Nothing changed.")
            raise typer.Exit(1)
    for agent in selected:
        _require_selection(prepared)
        try:
            if isinstance(agent, ClaudeCode):
                agent.apply(ClaudeOptions(disable_classifier_context=True), config, _prepared=prepared)
            else:
                agent.apply()
        except RouteProblem as problem:
            typer.secho(f"{agent.name}: left as it is ({problem})", fg="yellow")
            continue
        _require_selection(prepared)
        typer.echo(f"{agent.name}: done")
        say_applied(agent)


def _printable(text: str) -> str:
    """A name the server gave, as a terminal may show it (its control characters escaped)."""
    from latet.server.client import printable

    return printable(text)


from latet.cli import (  # noqa: E402, F401 - registers their commands
    backup,
    connect,
    host,
    llm,
    ocr,
    project,
    repositories,
    rules,
    server,
    tutorial,
)
