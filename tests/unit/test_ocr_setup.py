"""The text recognition models: when they count as installed, what the doctor, the CLI and the dashboard say."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers the commands, `ocr` among them
from latet.api.rest import Dashboard
from latet.cli.main import app
from latet.config import Config, IdentityConfig, Profile, save_config
from latet.doctor.checks import OK, check_ocr
from latet.engine.modelspec import VERIFIED, model_dir
from latet.runtime import make_runtime
from latet.vision import models
from latet.vision.masking import ImageMasking

from ..fake_ocr import FakeOcr


def _fake_install(data: Path) -> None:
    for spec in models.MODELS:
        folder = model_dir(data, spec)
        folder.mkdir(parents=True, exist_ok=True)
        for path, (_, size) in spec.files.items():
            (folder / path).write_bytes(b"\0" * size)
        (folder / VERIFIED).write_text("{}", "utf-8")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    return tmp_path


def test_installed_needs_both_models_verified(setup):
    data = Path(os.environ["LATET_DATA_DIR"])
    assert not models.installed(data)
    _fake_install(data)
    assert models.installed(data)
    (model_dir(data, models.RECOGNIZER) / VERIFIED).unlink()
    assert not models.installed(data)


@pytest.mark.parametrize(
    ("config", "says"),
    [
        (Config(profile=Profile.MAX), "not sent (the max profile)"),
        (Config(images="block"), "not sent (`images: block`)"),
        (Config(ocr="off"), "not read (`ocr: off`)"),
        (Config(), "`latet ocr install` reads them"),
    ],
)
def test_the_doctor_says_whether_images_are_read(setup, config, says):
    save_config(config)
    [check] = check_ocr()
    assert check.status == OK and says in check.detail


def test_ocr_status_and_remove(setup):
    runner = CliRunner()
    out = runner.invoke(app, ["ocr", "status"]).output
    assert "models: not downloaded" in out and "in use: no" in out
    _fake_install(Path(os.environ["LATET_DATA_DIR"]))
    assert "in use: yes" in runner.invoke(app, ["ocr", "status"]).output
    assert "Models removed." in runner.invoke(app, ["ocr", "remove"]).output
    assert not models.installed(Path(os.environ["LATET_DATA_DIR"]))


@pytest.mark.parametrize(
    ("installed", "reader", "says"),
    [
        (False, None, {"model": False, "active": False}),  # nothing installed, nothing read
        (True, None, {"model": True, "active": False}),  # installed since the daemon started: read from its next reload
        (False, "works", {"model": True, "active": True}),  # a reader this folder does not explain: the tutorial's
        (True, "failed", {"model": True, "active": False}),  # installed, but they did not load
        (False, "failed", {"model": False, "active": False}),  # a reader that did not load is no models
    ],
    ids=["none", "installed-not-reloaded", "reader-from-another-folder", "installed-not-loaded", "reader-not-loaded"],
)
def test_the_dashboard_says_which_models_images_are_read_with(setup, monkeypatch, installed, reader, says):
    """`ocr_status`: the models in the daemon's data folder, or the reader it holds (the tutorial's sandbox reads with
    the models of your own folder, not its own); active while a reader works and `ocr` is on. A daemon without a
    reader says what its data folder has, as it always did."""
    monkeypatch.setattr(models, "runtime_available", lambda: True)
    data = Path(os.environ["LATET_DATA_DIR"])
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=data, config_file=setup / "config.yaml")
    try:
        if installed:
            _fake_install(data)  # after the daemon started: it holds no reader
        if reader is not None:
            fake = FakeOcr()
            fake.error = RuntimeError("the models could not be loaded") if reader == "failed" else None
            rt.mediator.vision = ImageMasking(fake)
        assert Dashboard(rt)._ocr_status() == {"runtime": True, **says}
    finally:
        rt.close()
