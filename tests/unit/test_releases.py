"""Latet comes from its releases on GitHub, never from PyPI: the wheel of the latest release (it carries the
dashboard), with local name detection and the text in images. What `latet upgrade` installs, and what the
commands say when those two are missing."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import latet.cli.admin as admin  # registers upgrade
from latet import releases
from latet.cli.main import app

WHEEL = "https://github.com/Latet-AI/latet/releases/download/v1.2.0/latet-1.2.0-py3-none-any.whl"
LATEST = {
    "tag_name": "v1.2.0",
    "assets": [
        {"name": "install.sh", "browser_download_url": WHEEL.replace("latet-1.2.0-py3-none-any.whl", "install.sh")},
        {"name": "latet-1.2.0.tar.gz", "browser_download_url": WHEEL.replace("-py3-none-any.whl", ".tar.gz")},
        {"name": "latet-1.2.0-py3-none-any.whl", "browser_download_url": WHEEL},
    ],
}


TAG_PAGE = "https://github.com/Latet-AI/latet/releases/tag/v1.2.0"


def _github(status: int = 200, body: object = LATEST, page: str | None = None) -> httpx.Client:
    """GitHub's API answering ``status`` with ``body``; the page of the latest release redirecting to ``page``
    (None: not found)."""

    def answer(request: httpx.Request) -> httpx.Response:
        if str(request.url) == releases.LATEST_PAGE:
            return httpx.Response(302, headers={"location": page}) if page else httpx.Response(404)
        assert str(request.url) == releases.LATEST
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(answer))


def test_the_latest_release_is_the_wheel_on_github():
    release = releases.latest(_github())
    assert (release.version, release.wheel) == ("1.2.0", WHEEL)
    assert release.requirement() == f"latet[ner,ocr] @ {WHEEL}"
    assert release.requirement(extras=False) == f"latet @ {WHEEL}"


def test_a_release_with_no_tag_name_takes_its_version_from_its_wheel():
    release = releases.latest(_github(body={**LATEST, "tag_name": ""}))
    assert (release.version, release.wheel) == ("1.2.0", WHEEL)


ELSEWHERE = {"name": "latet-1.2.0-py3-none-any.whl", "browser_download_url": "https://evil.example/latet.whl"}
LIMIT = "GitHub's API answered 403 (it takes 60 requests an hour from one address)"


@pytest.mark.parametrize(
    ("status", "body", "said"),
    [
        (404, {"message": "Not Found"}, f"there is no release of Latet yet ({releases.PAGE})"),
        (200, {"tag_name": "v1.2.0", "assets": []}, f"the latest release has no wheel of Latet ({releases.PAGE})"),
        (200, {"tag_name": "v1.2.0", "assets": [ELSEWHERE]}, "no wheel"),  # only from Latet's own releases
        (200, ["not", "a", "release"], "could not be read"),
        (403, {"message": "API rate limit exceeded"}, LIMIT),  # and the page names no release either
        (502, {"message": "Bad Gateway"}, "GitHub's API answered 502"),
    ],
)
def test_no_release_says_why(status, body, said):
    with pytest.raises(releases.ReleaseError) as caught:
        releases.latest(_github(status, body))
    assert said in str(caught.value)


@pytest.mark.parametrize("status", [403, 429, 500])
def test_where_the_api_does_not_answer_the_page_of_the_latest_release_names_it(status):
    """GitHub's API takes 60 requests an hour from one address: its page is not counted, and GitHub redirects it to
    the release's tag. The wheel is named after the tag, as the release workflow makes it."""
    assert releases.latest(_github(status, {"message": "no"}, page=TAG_PAGE)) == releases.Release("1.2.0", WHEEL)


@pytest.mark.parametrize(
    "page",
    [
        "https://github.com/Latet-AI/latet/releases",  # no release: GitHub sends the list
        "https://github.com/someone/else/releases/tag/v1.2.0",  # not Latet's own
        "https://github.com/Latet-AI/latet/releases/tag/latest-build",  # not a version
    ],
)
def test_a_page_that_names_no_release_of_latet_is_not_followed(page):
    with pytest.raises(releases.ReleaseError, match=r"GitHub's API answered 403"):
        releases.latest(_github(403, {"message": "API rate limit exceeded"}, page=page))


def test_github_out_of_reach_says_so():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    with pytest.raises(releases.ReleaseError, match="GitHub could not be reached"):
        releases.latest(httpx.Client(transport=httpx.MockTransport(fail)))


# --- latet upgrade ------------------------------------------------------------------------------------


@pytest.fixture
def upgrade(monkeypatch):
    """`latet upgrade` with GitHub, uv and the daemon replaced: what it runs, in order. The Latet that runs it
    has neither name detection nor the text in images, unless a test says so."""
    from latet.engine.detectors import ner
    from latet.service import daemon
    from latet.vision import models

    calls: list[list[str]] = []
    failing: set[str] = set()  # the requirements uv cannot install

    def run(argv, check=False):
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 1 if argv[-1] in failing else 0)

    monkeypatch.setattr(releases, "latest", lambda client=None: releases.Release("1.2.0", WHEEL))
    monkeypatch.setattr(shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(daemon, "stop", lambda: calls.append(["daemon stop"]) or True)
    monkeypatch.setattr(daemon, "start", lambda: calls.append(["daemon start"]))
    monkeypatch.setattr(ner, "runtime_available", lambda: False)
    monkeypatch.setattr(models, "runtime_available", lambda: False)
    monkeypatch.setattr(admin, "_running_files_stay", lambda: False)  # macOS and Linux; Windows has tests of its own
    return calls, failing


# The Pythons CI tests Latet on: on a newer one, dependencies such as google-re2 may have no wheel yet.
INSTALL = ["/tools/uv", "tool", "install", "--upgrade", "--python", ">=3.12,<3.14"]


def test_upgrade_installs_the_latest_release_with_names_and_images(upgrade):
    calls, _ = upgrade
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert calls == [
        ["daemon stop"],
        [*INSTALL, f"latet[ner,ocr] @ {WHEEL}"],
        ["/tools/latet", "agents", "refresh"],  # the new version applies the new plugin
        ["/tools/latet", "start"],
    ]
    assert "1.2.0" in result.output


def test_upgrade_goes_on_without_names_and_images_where_they_cannot_be_installed(upgrade):
    calls, failing = upgrade
    failing.add(f"latet[ner,ocr] @ {WHEEL}")
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert [c[-1] for c in calls if c[:3] == INSTALL[:3]] == [f"latet[ner,ocr] @ {WHEEL}", f"latet @ {WHEEL}"]
    assert "without them" in result.output
    # The run ends saying what is off, and how to try again.
    assert result.output.rstrip().splitlines()[-1] == (
        "Off: name detection and the text in images, which uv could not install here. `latet upgrade` tries again."
    )


@pytest.mark.parametrize("extra", ["ner", "ocr"])
def test_an_upgrade_never_takes_away_names_or_images_the_installed_latet_has(upgrade, monkeypatch, extra):
    """uv could not install the release with them (a network blip, a newer Python with no ONNX Runtime for it):
    installing it without them would uninstall what this Latet has. The upgrade fails instead, and the Latet
    that was there starts again."""
    from latet.engine.detectors import ner
    from latet.vision import models

    calls, failing = upgrade
    monkeypatch.setattr(ner if extra == "ner" else models, "runtime_available", lambda: True)
    failing.add(f"latet[ner,ocr] @ {WHEEL}")
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 1, result.output
    assert [c[-1] for c in calls if c[:3] == INSTALL[:3]] == [f"latet[ner,ocr] @ {WHEEL}"]  # never without them
    assert calls[-1] == ["daemon start"] and ["/tools/latet", "agents", "refresh"] not in calls
    assert "without them" not in result.output
    assert result.output.rstrip().splitlines()[-1] == (
        "The upgrade failed: uv could not install it with name detection and the text in images, which this "
        "Latet has. Latet is as it was."
    )


def test_upgrade_that_cannot_look_up_the_release_says_why_and_where_to_go(upgrade, monkeypatch):
    calls, _ = upgrade

    def none(client=None):
        raise releases.ReleaseError(LIMIT)

    monkeypatch.setattr(releases, "latest", none)
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 1
    assert result.output.splitlines()[0] == f"Could not look up the latest release: {LIMIT}."
    assert releases.PAGE in result.output and "LATET_SOURCE" in result.output  # the way out
    assert calls == []  # not even stopped


def test_an_upgrade_that_fails_starts_the_old_latet_again(upgrade):
    calls, failing = upgrade
    failing.update({f"latet[ner,ocr] @ {WHEEL}", f"latet @ {WHEEL}"})
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 1 and "as it was" in result.output
    assert calls[-1] == ["daemon start"] and ["/tools/latet", "agents", "refresh"] not in calls


# --- latet upgrade on Windows ---------------------------------------------------------------------------
# Windows replaces no file of a program that runs, and `latet upgrade` runs from the Latet it replaces: run there,
# uv could not remove that Latet's folder and left it half gone (0.9.0, 2026-10-22). The upgrade goes on in a window
# of its own (upgrade.ps1), once the command and its launcher have ended.


@pytest.fixture
def windows(upgrade, monkeypatch):
    started: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(admin, "_running_files_stay", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: started.append((list(argv), kwargs)))
    return upgrade[0], started


def _argument(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_on_windows_the_upgrade_goes_on_in_a_window_of_its_own_once_this_command_has_ended(windows):
    calls, started = windows
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert calls == [["daemon stop"]]  # never uv from here, nor a start
    [(argv, kwargs)] = started
    assert argv[:5] == ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    script = Path(argv[5])
    assert script.read_bytes() == (Path(admin.__file__).parent / "upgrade.ps1").read_bytes()
    assert script.parent == Path(tempfile.gettempdir())  # never in the folder the upgrade replaces
    assert str(os.getpid()) in _argument(argv, "-WaitFor").split(",")
    assert _argument(argv, "-Uv") == "/tools/uv" and _argument(argv, "-Latet") == "/tools/latet"
    assert _argument(argv, "-Pythons") == releases.PYTHONS
    assert _argument(argv, "-Full") == f"latet[ner,ocr] @ {WHEEL}" and _argument(argv, "-Plain") == f"latet @ {WHEEL}"
    assert _argument(argv, "-Had") == "0" and _argument(argv, "-Version") == "1.2.0"
    assert kwargs.get("creationflags") == getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    assert "new window" in result.output
    script.unlink()


def test_on_windows_the_window_is_told_the_latet_that_runs_has_names_and_images(windows, monkeypatch):
    from latet.engine.detectors import ner

    _, started = windows
    monkeypatch.setattr(ner, "runtime_available", lambda: True)
    assert CliRunner().invoke(app, ["upgrade"]).exit_code == 0
    [(argv, _)] = started
    assert _argument(argv, "-Had") == "1"
    Path(argv[5]).unlink()


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _stand_ins(tmp_path: Path, failing: tuple[str, ...] = ()) -> tuple[Path, Path, Path]:
    """uv and latet as PowerShell scripts that write each call they get, one a line; uv fails for ``failing``."""
    log = tmp_path / "calls.log"
    uv, latet = tmp_path / "uv.ps1", tmp_path / "latet.ps1"
    refused = ", ".join(f"'{item}'" for item in failing) or "''"
    uv.write_text(
        f"Add-Content -LiteralPath '{log}' ('uv ' + ($args -join ' '))\nif (@({refused}) -contains $args[-1]) {{ exit 1 }}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    latet.write_text(f"Add-Content -LiteralPath '{log}' ('latet ' + ($args -join ' '))\n", encoding="utf-8")
    return log, uv, latet


def _window(tmp_path: Path, uv: Path, latet: Path, had: str = "0", wait: str = "none") -> subprocess.CompletedProcess:
    assert POWERSHELL is not None
    script = Path(admin.__file__).parent / "upgrade.ps1"
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-WaitFor", wait,
         "-Uv", str(uv), "-Pythons", releases.PYTHONS, "-Full", f"latet[ner,ocr] @ {WHEEL}",
         "-Plain", f"latet @ {WHEEL}", "-Had", had, "-Latet", str(latet), "-Version", "1.2.0", "-Quiet"],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell")
def test_the_window_installs_the_release_then_has_the_new_version_apply_the_integrations_and_start(tmp_path):
    log, uv, latet = _stand_ins(tmp_path)
    done = _window(tmp_path, uv, latet)
    assert done.returncode == 0, done.stdout + done.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"uv tool install --upgrade --python {releases.PYTHONS} latet[ner,ocr] @ {WHEEL}",
        "latet agents refresh",
        "latet start",
    ]
    assert "Latet 1.2.0 installed." in done.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell")
def test_the_window_goes_on_without_names_and_images_where_they_cannot_be_installed(tmp_path):
    log, uv, latet = _stand_ins(tmp_path, failing=(f"latet[ner,ocr] @ {WHEEL}",))
    done = _window(tmp_path, uv, latet)
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[1] == f"uv tool install --upgrade --python {releases.PYTHONS} latet @ {WHEEL}"
    assert calls[2:] == ["latet agents refresh", "latet start"]
    assert "Off: name detection and the text in images" in done.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell")
def test_the_window_never_takes_away_names_or_images_and_starts_the_latet_that_was_there(tmp_path):
    log, uv, latet = _stand_ins(tmp_path, failing=(f"latet[ner,ocr] @ {WHEEL}",))
    done = _window(tmp_path, uv, latet, had="1")
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"uv tool install --upgrade --python {releases.PYTHONS} latet[ner,ocr] @ {WHEEL}",
        "latet start",
    ]
    assert "Latet is as it was." in done.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell")
def test_the_window_waits_for_the_command_that_started_it_to_end(tmp_path):
    ended = tmp_path / "ended"
    command = subprocess.Popen(
        [sys.executable, "-c", f"import time, pathlib; time.sleep(2); pathlib.Path({str(ended)!r}).touch()"]
    )
    log, uv, latet = _stand_ins(tmp_path)
    uv.write_text(f"Add-Content -LiteralPath '{log}' ('ended before uv: ' + (Test-Path -LiteralPath '{ended}'))\n")
    _window(tmp_path, uv, latet, wait=f"{command.pid}")
    command.wait(10)
    assert log.read_text(encoding="utf-8").splitlines()[0] == "ended before uv: True"


# --- the extras, when they are missing ------------------------------------------------------------------


def test_without_the_extras_the_models_point_at_upgrade_never_at_pypi(monkeypatch):
    from latet.engine.detectors import ner
    from latet.vision import models

    monkeypatch.setattr(ner, "runtime_available", lambda: False)
    monkeypatch.setattr(models, "runtime_available", lambda: False)
    for command in (["ner", "install", "--yes"], ["ocr", "install", "--yes"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1, result.output
        assert "latet upgrade" in result.output and "uv tool install" not in result.output, result.output
        assert "pip install" not in result.output
