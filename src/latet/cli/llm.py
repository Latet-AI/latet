"""`latet llm`: the local reviewer, a language model on your own server (see `llm_review`)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from latet.cli.main import app

llm_app = typer.Typer(help="The local reviewer: a language model on your own server.", no_args_is_help=True)
app.add_typer(llm_app, name="llm")


@llm_app.callback()
def _llm_here(ctx: typer.Context) -> None:
    from latet.cli.admin import settings_here

    settings_here("llm", ctx.invoked_subcommand)


ProjectOpt = Annotated[
    Path | None, typer.Option("--project", help="For the project in this folder (your own settings for it).")
]


def _config_here(project: Path | None) -> Any:
    """The settings that apply: the global ones, or a project's (with its own guidance)."""
    from latet.config import load_config

    if project is None:
        return load_config()
    from latet.cli.project import _offline, locate

    root, repo_file = locate(project)
    return _offline(root, repo_file)[2]


def _line_col(text: str, offset: int) -> str:
    line = text.count("\n", 0, offset) + 1
    col = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return f"{line}:{col}"


@llm_app.command("status")
def llm_status() -> None:
    """Show the reviewer's settings and whether its server answers (with the sample text)."""
    from latet.config import load_config
    from latet.engine.build import build
    from latet.engine.reviewer.probe import SAMPLE, probe, reviewed_view
    from latet.engine.reviewer.reviewer import client_for, key_source
    from latet.engine.vault import Vault

    config = load_config()
    review = config.llm_review
    typer.echo(f"enabled:     {'yes' if review.enabled else 'no (llm_review.enabled)'}")
    where = "this machine" if review.on_this_machine() else "another machine of yours (remote)"
    typer.echo(f"server:      {review.url} on {where}, model {review.model or 'the first it lists'}")
    source = key_source(review)
    keys = {
        "file": f"from {review.api_key_file}",
        "env": f"from the variable {review.api_key_env}",
        "saved": "saved with `latet llm key` or the dashboard",
    }
    typer.echo(f"API key:     {keys[source] if source else 'none'}")
    reads = ", ".join(review.inline) or "nothing"
    typer.echo(f"before sent: {reads}; after: {'the rest' if review.background else 'nothing'}")
    limit = f"{review.budget_ms} ms" if review.budget_ms else "none"
    typer.echo(f"time limit:  {limit}; when it cannot answer: {review.on_failure} the request")
    typer.echo(f"masks:       {', '.join(review.auto_mask) or 'nothing by itself'}; may block: {review.block}")
    guidance = f"{len(review.guidance)} characters (`latet llm prompt`)" if review.guidance else "none"
    typer.echo(f"guidance:    {guidance}")
    masker = build(config.model_copy(update={"ner": "off"}), Vault(b"\x00" * 32)).masker
    result = asyncio.run(probe(review, reviewed_view(masker, SAMPLE), SAMPLE, client=client_for(review, timeout=120)))
    if not result.ok:
        typer.secho(f"server:      {result.error}", fg="red")
        raise typer.Exit(1)
    slots = f"{result.slots} slot{'s' if result.slots != 1 else ''}" if result.slots else "slots not said"
    context = f", {result.n_ctx} tokens each" if result.n_ctx else ""
    typer.secho(f"answers:     model {result.model}, {slots}{context}", fg="green")
    found = ", ".join(sorted({f.category for f in result.answer.findings})) if result.answer else ""
    typer.echo(f"sample:      {result.seconds} s, found {found or 'nothing'}")


@llm_app.command("prompt")
def llm_prompt(project: ProjectOpt = None) -> None:
    """Print what the reviewer is told: Latet's own instructions, then your guidance, if any.

    Latet's part is fixed, the text its benchmark measures. Yours follows it: `llm_review.guidance`,
    then a project's own `review_guidance` (see `latet llm guidance`).
    """
    from latet.engine.reviewer.prompt import system_for

    typer.echo(system_for(_config_here(project).llm_review.guidance))


@llm_app.command("guidance")
def llm_guidance(
    text: Annotated[
        str | None, typer.Argument(help="The new guidance (`-`: standard input). None: print the one set.")
    ] = None,
    file: Annotated[Path | None, typer.Option("--file", help="Read the new guidance from this file.")] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove it.")] = False,
    project: ProjectOpt = None,
    wait: Annotated[
        bool, typer.Option("--wait", help="When the change waits for the user's approval, wait for the answer.")
    ] = False,
) -> None:
    """Show or set your guidance to the local reviewer, added after Latet's own instructions.

    Say what else is confidential in your work, or what to leave alone: "Ward and department names
    are confidential", "PRJ- followed by digits is an internal project". It applies to the texts
    reviewed from then on, and is sent to the reviewer's server with each review: describe kinds of
    values, and keep the confidential values themselves in `terms`. With --project, it is for that
    project only, after the global one. Changed by an agent, it waits for the user's approval.
    """
    from latet.cli.rules import _apply, _settings, _term_values
    from latet.config import clean_guidance

    data, build = _settings(project)
    kept = _term_values(data)
    holder, key = (data, "review_guidance") if project is not None else (data["llm_review"], "guidance")
    if sum((text is not None, file is not None, clear)) > 1:
        raise typer.BadParameter("give the guidance, --file or --clear")
    if text is None and file is None and not clear:
        typer.echo(holder.get(key) or "No guidance is set.")
        return
    if clear:
        value = None
    else:
        raw = sys.stdin.read() if text == "-" else text if file is None else file.read_text("utf-8")
        try:
            value = clean_guidance(raw)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
    holder[key] = value
    _apply(data, build, wait, kept)


@llm_app.command("key")
def llm_key(
    remove: Annotated[bool, typer.Option("--remove", help="Forget the saved key.")] = False,
) -> None:
    """Save the API key of the reviewer's server, or forget it (--remove).

    The key is asked without showing it, or read from standard input, and kept in the operating
    system's credential store, never in config.yaml. A key in the variable `api_key_env` names, or in
    `api_key_file`, comes first. The running daemon uses the new key at once.
    """
    from latet.cli.admin import _reload_answer, say_refusal
    from latet.config import data_dir
    from latet.secret_text import KEY_BYTES, KEY_REQUIREMENT, key_problem
    from latet.storage.keys import remove_reviewer_key, save_reviewer_key

    if remove:
        typer.echo("The saved key is forgotten." if remove_reviewer_key(data_dir()) else "No key was saved.")
    else:
        if sys.stdin.isatty():
            key = typer.prompt("API key", hide_input=True).strip()
        else:
            raw = sys.stdin.buffer.readline(KEY_BYTES + 1)
            if len(raw) > KEY_BYTES:
                raise typer.BadParameter(f"the key input holds more than {KEY_BYTES} bytes")
            try:
                key = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                key = None
        if key is None or key_problem(key) is not None:
            raise typer.BadParameter(KEY_REQUIREMENT)
        where = save_reviewer_key(data_dir(), key)
        place = "this computer's credential store" if where == "keyring" else f"{data_dir() / 'reviewer.key'}"
        typer.echo(f"Saved in {place}.")
    answer = _reload_answer()
    if answer is None:
        typer.echo("The daemon is not running: it uses the key when it starts.")
    elif "error" in answer:
        say_refusal(answer)
        raise typer.Exit(1)


@llm_app.command("trust")
def llm_trust(
    url: Annotated[str | None, typer.Argument(help="The server's https:// address (default: llm_review.url).")] = None,
    fingerprint: Annotated[
        str | None,
        typer.Option(help="The fingerprint you expect (sha256:...): trusted without asking when it matches."),
    ] = None,
    remove: Annotated[bool, typer.Option("--remove", help="Trust no certificate of its own any more.")] = False,
) -> None:
    """Trust the certificate of the reviewer's server on another machine of yours (self-signed).

    It shows the certificate's SHA-256 fingerprint: compare it with the one on the server, for example
    `openssl x509 -noout -fingerprint -sha256 -in cert.pem` there. Once trusted, only that certificate
    is accepted, checked before any text is sent.
    """
    from latet.cli.agent import at_your_terminal
    from latet.cli.rules import _save_and_reload
    from latet.config import LlmReview, data_dir, load_config
    from latet.engine.reviewer import certificate as certificates

    config = load_config()
    if remove:
        config.llm_review = config.llm_review.model_copy(update={"certificate": None})
        _save_and_reload(config)
        return
    address = (url or config.llm_review.url).rstrip("/")
    try:
        pem, found = certificates.fetch(address)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="URL") from None
    except OSError as exc:
        typer.secho(f"{address} could not be reached: {exc}", fg="red")
        raise typer.Exit(1) from None
    typer.echo(f"The certificate of {address}:")
    typer.echo(f"  SHA-256 {certificates.readable(found)}")
    if fingerprint is not None:
        if fingerprint.lower().replace(":", "").removeprefix("sha256") != found.removeprefix("sha256:"):
            typer.secho("It is not the one you expect: nothing is trusted.", fg="red")
            raise typer.Exit(1)
    elif not at_your_terminal():
        typer.secho("Compare it with the server's, then trust it with --fingerprint, or in the dashboard.", fg="yellow")
        raise typer.Exit(2)
    elif not typer.confirm("Is it the same as the one on your server?", default=False):
        typer.echo("Nothing is trusted.")
        raise typer.Exit(1)
    certificates.save(data_dir(), pem)
    try:
        config.llm_review = LlmReview.model_validate(
            config.llm_review.model_dump() | {"url": address, "certificate": found}
        )
    except ValueError as exc:
        typer.secho(f"Not trusted: {exc}", fg="red")
        raise typer.Exit(2) from None
    _save_and_reload(config)
    typer.echo("Only this certificate is accepted for the reviewer from now on.")


@llm_app.command("test")
def llm_test(
    file: Annotated[str, typer.Argument(help="The text to review (`-`: standard input).")] = "-",
    kind: Annotated[str, typer.Option(help="Read it as user_text, tool_result, attachment or system.")] = "tool_result",
    project: ProjectOpt = None,
) -> None:
    """Review a text as Latet would before sending it: what the reviewer finds beyond the detectors.

    It runs on this machine only, and prints categories and where they are (line:column), never the
    values themselves. With --project, with that project's rules and guidance.
    """
    from latet.engine.build import build
    from latet.engine.reviewer.probe import probe, reviewed_view
    from latet.engine.reviewer.reviewer import client_for
    from latet.engine.types import SlotKind
    from latet.engine.vault import Vault

    text = sys.stdin.read() if file == "-" else Path(file).read_text("utf-8", errors="replace")
    if not text.strip():
        typer.echo("Nothing to review.")
        return
    try:
        slot_kind = SlotKind(kind)
    except ValueError:
        raise typer.BadParameter(f"unknown kind {kind}", param_hint="--kind") from None
    config = _config_here(project)
    masker = build(config, Vault(b"\x00" * 32)).masker
    view = reviewed_view(masker, text, slot_kind)
    result = asyncio.run(probe(config.llm_review, view, text, client=client_for(config.llm_review, timeout=120)))
    if not result.ok or result.answer is None:
        typer.secho(result.error or "the review failed", fg="red", err=True)
        raise typer.Exit(1)
    findings = result.answer.findings
    typer.echo(f"{len(findings)} found by the reviewer in {result.seconds} s (model {result.model}):")
    for finding in findings:
        at = view.text.find(finding.value)
        masked = "masked" if finding.category in config.llm_review.auto_mask else "reported"
        where = _line_col(view.text, at) if at >= 0 else "?"
        typer.echo(f"  {where:>8}  {finding.category:<13} {len(finding.value):>3} chars  {masked}")
    if result.answer.block:
        typer.secho("It would stop this text as a whole (with `block: true`).", fg="yellow")
