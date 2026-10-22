"""latet ocr: the optional text recognition that masks the text in images."""

from __future__ import annotations

from typing import Annotated

import typer

from latet.cli.main import app

ocr_app = typer.Typer(help="Optional text recognition: the text in images, masked.", no_args_is_help=True)
app.add_typer(ocr_app, name="ocr")


def install_ocr(yes: bool) -> bool:
    from latet.cli.admin import _reload_daemon
    from latet.config import data_dir
    from latet.models import ModelError, install
    from latet.vision import models

    if not models.runtime_available():
        typer.echo("Text recognition needs the `ocr` extra (ONNX Runtime and Pillow), which this Latet was")
        typer.echo("installed without. `latet upgrade` installs the latest release with it.")
        return False
    for spec in models.MODELS:
        typer.echo(f"Model: {spec.repo} ({spec.license} license, {spec.homepage})")
    typer.echo(f"Download: {models.size() / 1e6:.0f} MB from huggingface.co, once; it then runs on this machine only.")
    if not yes and not typer.confirm("Download it now?", default=True):
        return False
    with typer.progressbar(length=models.size(), label="Downloading") as bar:
        seen = [0]
        before = [0]  # the models already downloaded

        def advance(done: int, _total: int) -> None:
            bar.update(before[0] + done - seen[0])
            seen[0] = before[0] + done

        try:
            for spec in models.MODELS:
                install(data_dir(), spec, progress=advance)
                before[0] += spec.size
        except (ModelError, OSError) as exc:
            typer.secho(f"Download failed: {exc}", fg="red", err=True)
            return False
    typer.echo("Installed and verified.")
    if _reload_daemon():
        typer.echo("The running daemon now reads the text in images.")
    return True


@ocr_app.command("install")
def ocr_install(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask before downloading.")] = False,
) -> None:
    """Download the text recognition models (once; they then work offline)."""
    if not install_ocr(yes):
        raise typer.Exit(1)


@ocr_app.command("status")
def ocr_status() -> None:
    """Show whether the text in images is read and masked."""
    from latet.config import data_dir, load_config
    from latet.vision import models
    from latet.vision.policy import image_policy

    config = load_config()
    runtime = models.runtime_available()
    installed = models.installed(data_dir())
    typer.echo(f"runtime (the `ocr` extra): {'installed' if runtime else 'missing'}")
    typer.echo(f"models: {'installed' if installed else 'not downloaded'} ({data_dir() / 'models'})")
    policy = image_policy(config)
    if policy.blocks:
        why = "the max profile" if policy.because == "profile" else "`images: block`"
        typer.echo(f"in use: no, requests with images are not sent ({why})")
    else:
        active = runtime and installed and config.ocr == "auto"
        typer.echo(f"in use: {'yes' if active else 'no'} (setting `ocr: {config.ocr}`)")


@ocr_app.command("remove")
def ocr_remove() -> None:
    """Delete the downloaded models."""
    from latet.cli.admin import _reload_daemon
    from latet.config import data_dir
    from latet.models import remove
    from latet.vision import models

    removed = [remove(data_dir(), spec) for spec in models.MODELS]
    typer.echo("Models removed." if any(removed) else "No models to remove.")
    _reload_daemon()
