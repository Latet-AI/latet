"""Latet comes from its releases on GitHub, never from PyPI: the wheel of the latest release (it carries the
dashboard), with local name detection and the text in images. What `latet upgrade` installs, and what the
commands say when those two are missing."""

import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import latet.cli.admin as admin  # registers upgrade
import latet.cli.agent as agent_module
from latet import releases
from latet.cli.main import app

from ..conftest import ROOT_KEY

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
    for name in list(os.environ):  # no agent around, the one these tests may run in: a test sets one where it wants
        if name in {m for markers in agent_module.MARKERS.values() for m in markers} | {"AGENT"}:
            monkeypatch.delenv(name)
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


# --- a copy before every upgrade, and a way back ------------------------------------------------------------
# What a new version may migrate as it opens it (the vault, the history, the settings) is copied before the daemon
# stops and anything is installed, into <data>/backups/before-<version>-<UTC time>/; `--to VERSION` installs that
# release, and going back to the version a copy was taken under offers to put it back.

TAGGED = "https://api.github.com/repos/Latet-AI/latet/releases/tags/v1.1.0"
OLD_WHEEL = WHEEL.replace("1.2.0", "1.1.0")


@pytest.fixture
def installation(upgrade, monkeypatch, tmp_path):
    """Latet 1.1.0 with a data folder of its own: a vault of two values, a history of one exchange, the settings in
    config.yaml and those you approved last. ``upgrade``'s calls, the data folder, config.yaml and the exchange's id."""
    import latet
    from latet.engine.types import EntityClass
    from latet.engine.vault import Vault
    from latet.storage.db import Store
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    data, config = tmp_path / "data", tmp_path / "config.yaml"
    monkeypatch.setenv("LATET_DATA_DIR", str(data))
    monkeypatch.setenv("LATET_CONFIG", str(config))
    monkeypatch.setattr(latet, "__version__", "1.1.0")
    monkeypatch.setattr(releases, "tagged", lambda version, client=None: releases.Release("1.1.0", OLD_WHEEL))
    store = Store(data / "latet.db", ROOT_KEY)
    vault = Vault(ROOT_KEY, on_insert=store.save_entity)
    for value in ("mario.rossi@acme.it", "giulia@gmail.com"):
        vault.intern("EMAIL", EntityClass.PII, value, rule="pii:email")
    store.close()
    history = ExchangeStore(data / "exchanges.db", ROOT_KEY)
    exchange = history.record(ExchangeRecord(summary={"ts": 1.0, "session": "s1", "flags": []}))
    history.close()
    config.write_text("profile: max\n", encoding="utf-8")
    (data / "config.reviewed.json").write_text('{"profile": "max"}', encoding="utf-8")
    return upgrade[0], data, config, exchange


def _copies(data: Path) -> list[Path]:
    return sorted(p for p in (data / "backups").iterdir() if p.name.startswith("before-"))


def _values(path: Path) -> set[str]:
    from latet.storage.db import Store

    store = Store(path, ROOT_KEY)
    try:
        return {e.original for e in store.load_entities()}
    finally:
        store.close()


def test_an_upgrade_copies_the_vault_the_history_and_the_settings_before_anything_is_installed(
    installation, monkeypatch
):
    from latet.service import daemon
    from latet.storage.exchanges import ExchangeStore

    calls, data, _, exchange = installation
    seen: list[list[Path]] = []
    monkeypatch.setattr(daemon, "stop", lambda: seen.append(_copies(data)) or calls.append(["daemon stop"]) or True)
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output
    [copy] = _copies(data)
    assert seen == [[copy]] and calls[0] == ["daemon stop"]  # taken before the daemon stopped and uv ran
    assert re.fullmatch(r"before-1\.2\.0-\d{8}T\d{6}Z", copy.name)
    assert f"{copy}" in result.output and len([line for line in result.output.splitlines() if str(copy) in line]) == 1
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    assert (manifest["installed"], manifest["installing"]) == ("1.1.0", "1.2.0")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", manifest["created"])
    assert sorted(manifest["files"]) == ["config.reviewed.json", "config.yaml", "exchanges.db", "latet.db"]
    # Readable with this computer's key, which is not in the copy: the values stay sealed as they are in the vault.
    assert _values(copy / "latet.db") == {"mario.rossi@acme.it", "acme.it", "giulia@gmail.com"}
    history = ExchangeStore(copy / "exchanges.db", ROOT_KEY)
    assert history.detail(exchange) is not None
    history.close()
    assert b"mario.rossi@acme.it" not in (copy / "latet.db").read_bytes()
    assert not [
        p for p in copy.iterdir() if p.name not in {*manifest["files"], "manifest.json"}
    ]  # no key, nothing else
    assert (copy / "config.yaml").read_text(encoding="utf-8") == "profile: max\n"
    if os.name != "nt":
        assert (copy.stat().st_mode & 0o077) == 0 and ((data / "backups").stat().st_mode & 0o077) == 0


def test_a_copy_that_cannot_be_made_stops_the_upgrade_with_nothing_changed(installation, monkeypatch):
    import sqlite3

    from latet.storage import upgrade_copies

    calls, data, config, _ = installation
    copied = upgrade_copies._backup

    def refused(source: Path, target: Path) -> None:  # the vault copied, then the disk refuses the history
        copied(source, target)
        if source.name == "exchanges.db":
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(upgrade_copies, "_backup", refused)
    before = {p: p.read_bytes() for p in (data / "latet.db", data / "exchanges.db", config)}
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 1 and calls == []  # the daemon never stopped, uv never ran
    assert "nothing changed" in result.output.lower()
    # No copy half made: the lock the copies are worked on under is all there is (a lock file stays, deleted it races)
    assert not (data / "backups").exists() or [p.name for p in (data / "backups").iterdir()] == [upgrade_copies.LOCK]
    assert {p: p.read_bytes() for p in before} == before


def test_the_three_newest_copies_are_kept_and_only_copies_of_their_kind_are_deleted(installation, monkeypatch):
    from latet.storage import upgrade_copies

    _, data, _, _ = installation
    backups = data / "backups"
    stamps = iter([1_700_000_000 + n * 3_600 for n in range(5)])
    monkeypatch.setattr(upgrade_copies, "_clock", lambda: next(stamps))
    (backups / "20240101-101010").mkdir(parents=True)  # what `agents apply` keeps there: never touched
    (backups / "20240101-101010" / "settings.json.1a2b3c4d").write_text("{}")
    (backups / "before-0.1.0-20200101T000000Z").mkdir()  # no manifest of a copy: not one of them
    for _ in range(5):
        assert CliRunner().invoke(app, ["upgrade"]).exit_code == 0
    kept = [p.name for p in _copies(data) if (p / "manifest.json").exists()]
    assert kept == [
        time.strftime("before-1.2.0-%Y%m%dT%H%M%SZ", time.gmtime(t))
        for t in [1_700_007_200, 1_700_010_800, 1_700_014_400]
    ]
    assert (backups / "20240101-101010" / "settings.json.1a2b3c4d").exists()
    assert (backups / "before-0.1.0-20200101T000000Z").exists()


def test_to_installs_the_release_of_that_tag():
    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TAGGED
        return httpx.Response(200, json={**LATEST, "tag_name": "v1.1.0", "assets": [
            {"name": "latet-1.1.0-py3-none-any.whl", "browser_download_url": OLD_WHEEL}]})  # fmt: skip

    client = httpx.Client(transport=httpx.MockTransport(answer))
    assert releases.tagged("1.1.0", client) == releases.Release("1.1.0", OLD_WHEEL)
    assert releases.tagged("v1.1.0", client) == releases.Release("1.1.0", OLD_WHEEL)


def test_to_a_tag_with_no_release_says_so_and_where_the_api_does_not_answer_the_tags_page_names_it():
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(releases.ReleaseError, match=r"no release v1\.1\.0 of Latet"):
        releases.tagged("1.1.0", httpx.Client(transport=httpx.MockTransport(missing)))

    def limited(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{releases.PAGE}/tag/v1.1.0":
            return httpx.Response(200, text="<html>Latet 1.1.0</html>")
        return httpx.Response(403, json={"message": "API rate limit exceeded"})

    assert releases.tagged("1.1.0", httpx.Client(transport=httpx.MockTransport(limited))) == releases.Release(
        "1.1.0", OLD_WHEEL
    )
    with pytest.raises(ValueError, match="not a version"):
        releases.tagged("../../evil")


def test_upgrade_to_a_version_installs_that_release(upgrade, monkeypatch):
    calls, _ = upgrade
    monkeypatch.setattr(releases, "tagged", lambda version, client=None: releases.Release(version, OLD_WHEEL))
    result = CliRunner().invoke(app, ["upgrade", "--to", "1.1.0"])
    assert result.exit_code == 0, result.output
    assert [*INSTALL, f"latet[ner,ocr] @ {OLD_WHEEL}"] in calls and "1.1.0" in result.output


def _went_to_1_2_0(installation, monkeypatch) -> Path:
    """The installation upgraded to 1.2.0 (its copy taken under 1.1.0), which then learnt three values."""
    import latet
    from latet.engine.types import EntityClass
    from latet.engine.vault import Vault
    from latet.storage.db import Store

    calls, data, _, _ = installation
    assert CliRunner().invoke(app, ["upgrade"]).exit_code == 0
    [copy] = _copies(data)
    monkeypatch.setattr(latet, "__version__", "1.2.0")
    store = Store(data / "latet.db", ROOT_KEY)
    vault = Vault(ROOT_KEY, store.load_entities(), on_insert=store.save_entity)
    for value in ("Emily Carter", "Daniel Okafor", "Ada Byron"):
        vault.intern("PERSON", EntityClass.NER, value)
    store.close()
    calls.clear()
    return copy


def test_going_back_offers_the_copy_says_how_many_values_would_be_lost_and_keeps_the_data_by_default(
    installation, monkeypatch
):
    calls, data, _, _ = installation
    copy = _went_to_1_2_0(installation, monkeypatch)
    monkeypatch.setattr(agent_module, "at_your_terminal", lambda strict=False: True)
    vault_before = (data / "latet.db").read_bytes()
    result = CliRunner().invoke(app, ["upgrade", "--to", "1.1.0"], input="\n")  # the default answer
    assert result.exit_code == 0, result.output
    assert str(copy) in result.output and "3 values" in result.output
    for name in ("Emily", "Daniel", "Ada"):
        assert name not in result.output  # counts, never values
    assert (data / "latet.db").read_bytes() == vault_before and len(_values(data / "latet.db")) == 6
    assert [*INSTALL, f"latet[ner,ocr] @ {OLD_WHEEL}"] in calls


def test_putting_a_copy_back_keeps_the_current_files_aside_first_then_puts_the_copys_in_their_place(
    installation, monkeypatch
):
    from latet.service import daemon

    calls, data, config, _ = installation
    copy = _went_to_1_2_0(installation, monkeypatch)
    config.write_text("profile: secrets\n", encoding="utf-8")
    monkeypatch.setattr(agent_module, "at_your_terminal", lambda strict=False: True)
    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(True, pid=4242, port=5283))
    waited: list[int] = []
    monkeypatch.setattr(daemon, "wait_exit", lambda pid, timeout=15.0: waited.append(pid) or True)
    result = CliRunner().invoke(app, ["upgrade", "--to", "1.1.0"], input="y\n")
    assert result.exit_code == 0, result.output
    aside = [p for p in _copies(data) if p != copy]
    assert len(aside) == 1 and aside[0].name.startswith("before-1.1.0-")  # the files as they were, kept first
    assert len(_values(aside[0] / "latet.db")) == 6 and (aside[0] / "config.yaml").read_text() == "profile: secrets\n"
    assert _values(data / "latet.db") == {"mario.rossi@acme.it", "acme.it", "giulia@gmail.com"}  # the copy's
    assert config.read_text(encoding="utf-8") == "profile: max\n"
    assert waited == [4242] and calls.index(["daemon stop"]) < calls.index([*INSTALL, f"latet[ner,ocr] @ {OLD_WHEEL}"])
    assert str(aside[0]) in result.output


def test_a_copy_is_offered_back_only_at_a_terminal_of_your_own(installation, monkeypatch):
    _, data, _, _ = installation
    _went_to_1_2_0(installation, monkeypatch)
    monkeypatch.setattr(agent_module, "at_your_terminal", lambda strict=False: False)
    before = (data / "latet.db").read_bytes()
    result = CliRunner().invoke(app, ["upgrade", "--to", "1.1.0"], input="y\n")
    assert result.exit_code == 0, result.output
    assert (data / "latet.db").read_bytes() == before and "at a terminal" in result.output


def test_on_windows_the_copy_is_taken_before_the_window_takes_over(installation, windows):
    _, data, _, _ = installation
    _, started = windows
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output
    [copy] = _copies(data)
    assert (copy / "latet.db").exists() and len(started) == 1
    Path(started[0][0][5]).unlink()


# --- round 1 of the review: what the copies keep, and the upgrade's own safety -----------------------------------


@pytest.fixture
def followed(tmp_path):
    """This computer's vault and history as the daemon opens them (``local_backends``), with a value to forget, an
    exchange past the retention and one within it, and an upgrade copy of all of it."""
    from latet.engine.types import EntityClass
    from latet.engine.vault import Vault
    from latet.state.local import local_backends
    from latet.storage import upgrade_copies
    from latet.storage.exchanges import ExchangeRecord

    data, config = tmp_path / "data", tmp_path / "config.yaml"
    config.write_text("profile: max\n", encoding="utf-8")
    backends = local_backends(data, config, ROOT_KEY)
    vault = Vault(ROOT_KEY, on_insert=backends.vault.save_entity)
    gone = vault.intern("EMAIL", EntityClass.PII, "mario.rossi@acme.it", rule="pii:email")
    vault.intern("PERSON", EntityClass.NER, "Emily Carter", rule="ner")
    assert backends.history is not None
    old = backends.history.record(
        ExchangeRecord(summary={"ts": time.time() - 30 * 86400, "session": "s0", "flags": []})
    )
    new = backends.history.record(ExchangeRecord(summary={"ts": time.time(), "session": "s1", "flags": []}))
    copy = upgrade_copies.take(data, config, "1.1.0", "1.2.0")
    assert copy is not None
    yield backends, data, copy, gone, old, new
    backends.vault.close()
    backends.history.close()


def _exchanges(path: Path) -> set[str]:
    from latet.storage.exchanges import ExchangeStore

    store = ExchangeStore(path, ROOT_KEY)
    try:
        items, _ = store.list(limit=100)
        return {item["id"] for item in items}
    finally:
        store.close()


def test_forget_takes_the_value_out_of_every_upgrade_copy_too(followed):
    """Forget removes a value from the vault, and so from the history: from the copies an upgrade took as well, so
    that putting one back brings back nothing forgotten. The copy stays one, its digests made again."""
    from latet.storage import upgrade_copies

    backends, _, copy, gone, _, _ = followed
    assert "mario.rossi@acme.it" in _values(copy.folder / "latet.db")
    backends.vault.forget_entity(gone.id)
    assert "mario.rossi@acme.it" not in _values(copy.folder / "latet.db")
    assert "Emily Carter" in _values(copy.folder / "latet.db")
    again = upgrade_copies.read(copy.folder)
    assert again is not None and again.files["latet.db"] == upgrade_copies._digest(copy.folder / "latet.db")


def test_clear_history_empties_the_history_of_every_upgrade_copy_too(followed):
    backends, _, copy, _, old, new = followed
    assert _exchanges(copy.folder / "exchanges.db") == {old, new}
    backends.history.clear()
    assert _exchanges(copy.folder / "exchanges.db") == set()


def test_the_retention_prunes_the_history_of_every_upgrade_copy_too(followed):
    backends, _, copy, _, old, new = followed
    assert _exchanges(copy.folder / "exchanges.db") == {old, new}
    backends.history.retention_days = 14
    backends.history.prune()
    assert _exchanges(copy.folder / "exchanges.db") == {new}


def test_a_copy_that_cannot_be_opened_to_forget_a_value_is_deleted_and_the_log_says_so(followed, caplog):
    backends, _, copy, gone, _, _ = followed
    (copy.folder / "latet.db").write_bytes(b"not a database " * 64)
    with caplog.at_level("WARNING"):
        backends.vault.forget_entity(gone.id)
    assert not copy.folder.exists()
    said = [r.getMessage() for r in caplog.records if "upgrade copy" in r.getMessage()]
    assert len(said) == 1 and "mario" not in said[0], said
    # named by its versions and its time, never by its path (a folder can hold a person's name: test_logging_calls)
    assert copy.installing in said[0] and copy.created in said[0], said
    assert str(copy.folder.parent) not in said[0], said


def test_an_upgrade_to_the_version_installed_takes_no_copy_and_deletes_none(installation, monkeypatch):
    """Running `latet upgrade` again at the version installed migrates nothing: it takes no copy, and so pushes out
    none of those that lead back (one 1.1.0 -> 1.2.0, then three runs at 1.2.0 left none taken under 1.1.0)."""
    import latet

    _, data, _, _ = installation
    assert CliRunner().invoke(app, ["upgrade"]).exit_code == 0
    [copy] = _copies(data)
    monkeypatch.setattr(latet, "__version__", "1.2.0")
    for _ in range(3):
        result = CliRunner().invoke(app, ["upgrade"])
        assert result.exit_code == 0, result.output
    assert _copies(data) == [copy] and "copied in" not in result.output


def test_an_agent_cannot_go_back_to_an_older_latet(installation, monkeypatch):
    """`--to` an older release takes protection away (the fixes of the releases after it): refused when an agent runs
    it, in the words of the other commands that lower protection. The latest, or a newer one, stays as it was."""
    import latet

    calls, data, _, _ = installation
    monkeypatch.setattr(latet, "__version__", "1.2.0")
    monkeypatch.setenv("CLAUDECODE", "1")
    refused = CliRunner().invoke(app, ["upgrade", "--to", "1.1.0"])
    assert refused.exit_code == 2 and calls == [] and not (data / "backups").exists()
    assert "run it yourself, in a terminal of your own (it was run by claude-code)" in refused.output
    monkeypatch.setattr(releases, "tagged", lambda version, client=None: releases.Release(version, WHEEL))
    assert CliRunner().invoke(app, ["upgrade", "--to", "1.3.0"]).exit_code == 0
    assert CliRunner().invoke(app, ["upgrade"]).exit_code == 0


def test_a_put_back_that_fails_half_way_puts_your_files_back_and_says_so(installation, monkeypatch):
    from latet.service import daemon
    from latet.storage import upgrade_copies

    calls, data, config, _ = installation
    _went_to_1_2_0(installation, monkeypatch)
    config.write_text("profile: secrets\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in (data / "latet.db", data / "exchanges.db", config)}
    monkeypatch.setattr(agent_module, "at_your_terminal", lambda strict=False: True)
    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(True, pid=4242, port=5283))
    monkeypatch.setattr(daemon, "wait_exit", lambda pid, timeout=15.0: True)
    real = os.replace

    def held(source, target):  # the history held open by another process, as Windows refuses to replace it
        if Path(target).name == "exchanges.db" and "putting-back" in Path(source).name:
            raise PermissionError(13, "The process cannot access the file because it is being used")
        return real(source, target)

    monkeypatch.setattr(upgrade_copies.os, "replace", held)
    result = CliRunner().invoke(app, ["upgrade", "--to", "1.1.0"], input="y\n")
    assert result.exit_code == 1, result.output
    assert {p: p.read_bytes() for p in before} == before  # yours, every one, as they were
    assert not [p.name for p in data.iterdir() if "putting-back" in p.name or "put-aside" in p.name]
    assert "nothing was put back" in result.output and "as they were" in result.output
    assert [*INSTALL, f"latet[ner,ocr] @ {OLD_WHEEL}"] not in calls and calls[-1] == ["daemon start"]


def test_the_values_learnt_since_a_copy_are_said_not_counted_when_the_vault_cannot_be_read(installation, monkeypatch):
    _, data, _, _ = installation
    _went_to_1_2_0(installation, monkeypatch)
    (data / "latet.db").write_bytes(b"not a database " * 64)
    monkeypatch.setattr(agent_module, "at_your_terminal", lambda strict=False: True)
    result = CliRunner().invoke(app, ["upgrade", "--to", "1.1.0"], input="\n")
    assert "0 values" not in result.output and "cannot be counted" in result.output


def test_a_copy_cut_off_by_a_crash_is_never_read_by_a_disconnect_and_the_next_upgrade_deletes_it(installation):
    from latet.storage import upgrade_copies

    _, data, _, _ = installation
    partial = data / "backups" / ".before-1.2.0-20261025T090000Z.partial"
    partial.mkdir(parents=True)
    (partial / "latet.db").write_bytes(b"latet_pat_" + b"Q" * 43)
    assert upgrade_copies.is_copied_database(partial / "latet.db", data)
    assert upgrade_copies.is_copied_database(partial / "latet.db-wal", data)
    assert CliRunner().invoke(app, ["upgrade"]).exit_code == 0
    assert not partial.exists() and len(_copies(data)) == 1


def test_an_upgrade_never_holds_more_than_three_copies_and_needs_room_for_the_next(installation, monkeypatch):
    from latet.storage import upgrade_copies

    calls, data, _, _ = installation
    stamps = iter(range(1_700_000_000, 1_700_000_000 + 3_600 * 10, 3_600))
    monkeypatch.setattr(upgrade_copies, "_clock", lambda: next(stamps))
    taken = upgrade_copies.take
    peaks: list[int] = []

    def counted(*args, **kwargs):
        peaks.append(len(upgrade_copies.copies(data)))
        return taken(*args, **kwargs)

    monkeypatch.setattr(upgrade_copies, "take", counted)
    for _ in range(4):
        assert CliRunner().invoke(app, ["upgrade"]).exit_code == 0
    # The new copy is taken first, the older ones deleted once it is whole: three are kept, a fourth for a moment.
    assert peaks == [0, 1, 2, 3] and len(_copies(data)) == 3
    monkeypatch.setattr(upgrade_copies, "_free", lambda path: 1024)
    calls.clear()
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 1 and calls == [] and len(_copies(data)) == 3  # nothing changed
    assert "free" in result.output and "MB" in result.output


# --- round 2 of the review: the copies are followed as they are, one at a time -----------------------------------


def _schema(path: Path) -> list[tuple]:
    """A database's tables, columns and indexes, as SQLite keeps them."""
    db = sqlite3.connect(path)
    try:
        return sorted(db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall(), key=repr)
    finally:
        db.close()


def _rows(path: Path, table: str) -> set[str]:
    """The ids in a table, read as they are (never through a store, which would bring the file to its schema)."""
    db = sqlite3.connect(path)
    try:
        return {str(row[0]) for row in db.execute(f"SELECT id FROM {table}")}
    finally:
        db.close()


def _as_made(folder: Path) -> bool:
    """Whether every file of the copy is as its manifest says: a copy that can be put back."""
    from latet.storage import upgrade_copies

    copy = upgrade_copies.read(folder)
    return copy is not None and all(upgrade_copies._digest(folder / n) == d for n, d in copy.files.items())


def _an_older_schema(copy) -> None:
    """The copy's files as an older Latet had them, as the next release meets this one's copies: the history without
    a column (exchanges.images), the vault without a table (image_decisions); its manifest made again to match."""
    from latet.storage import upgrade_copies

    for name, statement in (
        ("exchanges.db", "ALTER TABLE exchanges DROP COLUMN images"),
        ("latet.db", "DROP TABLE image_decisions"),
    ):
        db = sqlite3.connect(copy.folder / name, isolation_level=None)
        db.execute(statement)
        db.close()
        upgrade_copies._redigest(copy, name)


def test_a_copy_of_an_older_schema_is_followed_without_being_migrated_and_can_still_be_put_back(followed):
    """The copy exists for going back across a migration: Forget, Clear history and the retention act on it with plain
    deletions, which never add a column or a table to it, and its digests are made again whenever its file changed,
    so it can still be put back."""
    from latet.storage import upgrade_copies

    backends, data, copy, gone, _, new = followed
    _an_older_schema(copy)
    schemas = {name: _schema(copy.folder / name) for name in upgrade_copies.DATABASES}
    backends.history.retention_days = 14
    backends.history.prune()  # what the daemon does at its start, with nothing of the copy's past it but `old`
    assert _rows(copy.folder / "exchanges.db", "exchanges") == {new}
    backends.vault.forget_entity("no-such-id")  # a value learnt after the copy was taken
    backends.vault.forget_entity(gone.id)
    assert gone.id not in _rows(copy.folder / "latet.db", "entities")
    backends.history.clear()
    assert _rows(copy.folder / "exchanges.db", "exchanges") == set()
    assert {name: _schema(copy.folder / name) for name in upgrade_copies.DATABASES} == schemas  # never migrated
    assert _as_made(copy.folder)
    backends.vault.close()
    backends.history.close()
    again = upgrade_copies.read(copy.folder)
    assert again is not None
    upgrade_copies.put_back(again, data, data.parent / "config.yaml")
    assert _schema(data / "latet.db") == schemas["latet.db"] and gone.id not in _rows(data / "latet.db", "entities")


def test_a_prune_that_removes_only_a_review_makes_the_copys_digest_again(followed):
    """The retention deletes the reviews, blocks and images past it too, not only the exchanges: whatever it deletes
    in a copy, the copy's digest is made again."""
    from latet.storage import upgrade_copies

    backends, _, copy, _, old, _ = followed
    db = sqlite3.connect(copy.folder / "exchanges.db", isolation_level=None)
    db.execute("DELETE FROM exchanges WHERE id = ?", (old,))  # nothing but the review is past the retention
    db.execute(
        "INSERT INTO reviews (id, ts, session, project, block, kind, inline, summary) "
        "VALUES ('rv_old', ?, 's0', NULL, 'b0', 'text', 0, '{}')",
        (time.time() - 30 * 86400,),
    )
    db.close()
    upgrade_copies._redigest(copy, "exchanges.db")
    backends.history.retention_days = 14
    backends.history.prune()
    assert _rows(copy.folder / "exchanges.db", "reviews") == set() and _as_made(copy.folder)


@pytest.fixture
def held():
    """A database held by another connection in the middle of a write, as another process can hold it: ``held(path)``
    takes it, and every one taken is let go at the end."""
    holders: list[sqlite3.Connection] = []

    def hold(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path, isolation_level=None)
        db.execute("BEGIN EXCLUSIVE")
        holders.append(db)
        return db

    yield hold
    for db in holders:
        with contextlib.suppress(sqlite3.ProgrammingError):  # let go already (``_let_go``)
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.close()


def _let_go(db: sqlite3.Connection) -> None:
    db.execute("ROLLBACK")
    db.close()


def test_a_copy_in_use_is_never_deleted_and_what_was_removed_is_taken_out_of_it_the_next_time(
    followed, held, monkeypatch, caplog
):
    """A copy's database held by another process is no damaged copy: it is tried again, then left as it is, and what
    was removed is taken out of it at the next pass (the daemon's start, or the retention's, six hours on)."""
    from latet.storage import upgrade_copies

    backends, _, copy, gone, _, _ = followed
    monkeypatch.setattr(upgrade_copies, "_BUSY_SECONDS", 0.05, raising=False)
    vault, history = held(copy.folder / "latet.db"), held(copy.folder / "exchanges.db")
    with caplog.at_level("WARNING"):
        backends.vault.forget_entity(gone.id)
        backends.history.clear()
    assert upgrade_copies.read(copy.folder) is not None  # still a copy, still offered back
    said = [r.getMessage() for r in caplog.records if "upgrade copy" in r.getMessage()]
    assert said and all("in use" in line and "deleted" not in line for line in said), said
    _let_go(vault)
    _let_go(history)
    backends.history.retention_days = 14
    backends.history.prune()  # the next pass
    assert gone.id not in _rows(copy.folder / "latet.db", "entities")
    assert _rows(copy.folder / "exchanges.db", "exchanges") == set()  # cleared, not only pruned
    assert _as_made(copy.folder)


def test_a_copy_put_back_first_loses_what_was_removed_while_it_was_in_use(followed, held, monkeypatch):
    from latet.storage import upgrade_copies

    backends, data, copy, gone, _, _ = followed
    monkeypatch.setattr(upgrade_copies, "_BUSY_SECONDS", 0.05, raising=False)
    vault = held(copy.folder / "latet.db")
    backends.vault.forget_entity(gone.id)
    _let_go(vault)
    backends.vault.close()
    backends.history.close()
    again = upgrade_copies.read(copy.folder)
    assert again is not None
    upgrade_copies.put_back(again, data, data.parent / "config.yaml")
    assert gone.id not in _rows(data / "latet.db", "entities")


def test_the_work_on_the_copies_is_done_one_at_a_time_here_and_across_processes(followed, monkeypatch, tmp_path):
    """Forget, Clear history and the retention run in threads of their own, and `latet upgrade` in a process of its
    own: the copies are worked on one at a time (a lock in the process, and a lock on a file beside the copies for the
    others), so none of them meets a database another holds, nor writes a manifest another is writing."""
    import threading

    from latet.storage import upgrade_copies

    backends, data, copy, _, _, _ = followed
    order: list[str] = []
    entered = threading.Event()

    def holding() -> None:
        with upgrade_copies.locked(data):
            entered.set()
            time.sleep(0.3)
            order.append("held")

    thread = threading.Thread(target=holding)
    thread.start()
    assert entered.wait(10)
    backends.history.clear()
    order.append("cleared")
    thread.join()
    assert order == ["held", "cleared"] and _as_made(copy.folder)
    assert not [p.name for p in copy.folder.iterdir() if p.name not in {*copy.files, "manifest.json"}]
    inside, release = tmp_path / "inside", tmp_path / "release"
    script = (
        "import pathlib, sys, time\nfrom latet.storage import upgrade_copies\n"
        "with upgrade_copies.locked(pathlib.Path(sys.argv[1])):\n"
        "    pathlib.Path(sys.argv[2]).write_text('in')\n"
        "    while not pathlib.Path(sys.argv[3]).exists():\n        time.sleep(0.02)\n"
    )
    other = subprocess.Popen([sys.executable, "-c", script, str(data), str(inside), str(release)])
    try:
        deadline = time.monotonic() + 60
        while not inside.exists():
            assert other.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        monkeypatch.setattr(upgrade_copies, "_LOCK_WAIT", 0.3)
        with pytest.raises(upgrade_copies.CopyError, match="in use"):
            upgrade_copies.take(data, data.parent / "config.yaml", "1.1.0", "1.3.0")
    finally:
        release.write_text("go")
        other.wait(60)
    assert upgrade_copies.take(data, data.parent / "config.yaml", "1.1.0", "1.3.0") is not None


def test_a_copy_that_can_be_neither_opened_nor_deleted_is_never_offered_back(followed, monkeypatch, caplog):
    """It holds the value forgotten: set aside (its manifest says so), it is no copy to put back any more, and the next
    upgrade deletes it."""
    from latet.storage import upgrade_copies

    backends, data, copy, gone, _, _ = followed
    (copy.folder / "latet.db").write_bytes(b"not a database " * 64)
    real = shutil.rmtree

    def refused(path, *args, **kwargs):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(upgrade_copies.shutil, "rmtree", refused)
    with caplog.at_level("WARNING"):
        backends.vault.forget_entity(gone.id)
    assert copy.folder.exists() and upgrade_copies.copies(data) == []
    said = [r.getMessage() for r in caplog.records if "upgrade copy" in r.getMessage()]
    assert len(said) == 1 and "never offered back" in said[0], said
    monkeypatch.setattr(upgrade_copies.shutil, "rmtree", real)
    assert copy.folder in upgrade_copies.prune(data) and not copy.folder.exists()


def test_the_new_copy_is_taken_before_an_older_one_is_deleted_and_needs_room_beside_them(installation, monkeypatch):
    """Nothing changed means nothing deleted: a copy that cannot be made leaves the three there, and the disk needs room
    for the new one beside them (the older ones go only once it is whole)."""
    import sqlite3 as sqlite

    from latet.storage import upgrade_copies

    calls, data, config, _ = installation
    stamps = iter(range(1_700_000_000, 1_700_000_000 + 3_600 * 10, 3_600))
    monkeypatch.setattr(upgrade_copies, "_clock", lambda: next(stamps))
    for _ in range(3):
        assert CliRunner().invoke(app, ["upgrade"]).exit_code == 0
    three = _copies(data)
    copied = upgrade_copies._backup

    def refused(source: Path, target: Path) -> None:
        copied(source, target)
        if source.name == "exchanges.db":
            raise sqlite.OperationalError("disk I/O error")

    monkeypatch.setattr(upgrade_copies, "_backup", refused)
    calls.clear()
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 1 and "nothing changed" in result.output.lower() and calls == []
    assert _copies(data) == three
    monkeypatch.setattr(upgrade_copies, "_backup", copied)
    short = upgrade_copies.needed(data, config) - 1  # the older copies' room is not counted: they go after
    monkeypatch.setattr(upgrade_copies, "_free", lambda path: short)
    result = CliRunner().invoke(app, ["upgrade"])
    assert result.exit_code == 1 and _copies(data) == three and calls == []


def test_forgetting_many_values_at_once_opens_each_copy_once(followed, monkeypatch):
    """The start forgets the names today's filters reject all at once: each copy is opened once for all of them."""
    from latet.engine.types import EntityClass
    from latet.engine.vault import Vault
    from latet.storage import upgrade_copies

    backends, data, _, gone, _, _ = followed
    vault = Vault(ROOT_KEY, backends.vault.load_entities(), on_insert=backends.vault.save_entity)
    names = [vault.intern("PERSON", EntityClass.NER, f"Product Number {n}", rule="ner") for n in range(20)]
    second = upgrade_copies.take(data, data.parent / "config.yaml", "1.1.0", "1.3.0")
    assert second is not None
    opened: list[str] = []
    connect = sqlite3.connect

    def counted(path, *args, **kwargs):
        if "backups" in str(path):
            opened.append(Path(str(path)).parent.name)
        return connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counted)
    backends.vault.forget_entities([gone.id, *(e.id for e in names)])
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert sorted(set(opened)) == sorted(c.folder.name for c in upgrade_copies.copies(data))
    assert len(opened) == len(set(opened))  # once each
    held = _rows(second.folder / "latet.db", "entities")
    assert gone.id not in held and not {e.id for e in names} & held


# --- round 3 of the review: the daemon never waits on the copies, a Forget never rewrites them -------------------


def _another_process_holds(data: Path, inside: Path, release: Path) -> subprocess.Popen:
    """Another process holding the copies' lock (``upgrade_copies.locked``) until ``release`` exists."""
    script = (
        "import pathlib, sys, time\nfrom latet.storage import upgrade_copies\n"
        "with upgrade_copies.locked(pathlib.Path(sys.argv[1])):\n"
        "    pathlib.Path(sys.argv[2]).write_text('in')\n"
        "    while not pathlib.Path(sys.argv[3]).exists():\n        time.sleep(0.02)\n"
    )
    other = subprocess.Popen([sys.executable, "-c", script, str(data), str(inside), str(release)])
    deadline = time.monotonic() + 60
    while not inside.exists():
        assert other.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    return other


def test_the_daemon_waits_no_more_than_a_moment_for_the_copies_and_keeps_its_work_for_later(
    followed, monkeypatch, tmp_path
):
    """A `latet upgrade` copying a large history holds the copies' lock for as long as its copy takes: the daemon's
    Forget (the runtime's lock held), its prune and its start take the lock for a moment only, then keep the work for
    the next pass (``.asked``), where it is done."""
    from latet.storage import upgrade_copies

    backends, data, copy, gone, _, _ = followed
    monkeypatch.setattr(upgrade_copies, "_LOCK_WAIT", 20.0)  # the CLI's wait: the daemon's is a moment of its own
    release = tmp_path / "release"
    other = _another_process_holds(data, tmp_path / "inside", release)
    try:
        started = time.monotonic()
        backends.vault.forget_entity(gone.id)
        assert time.monotonic() - started < 10
        assert gone.id in _rows(copy.folder / "latet.db", "entities")  # not done yet: kept for later
        assert list((data / "backups").glob(".asked-*.json"))
    finally:
        release.write_text("go")
        other.wait(60)
    backends.history.retention_days = 14
    backends.history.prune()  # the next pass
    assert gone.id not in _rows(copy.folder / "latet.db", "entities") and _as_made(copy.folder)
    assert not list((data / "backups").glob(".asked-*.json"))


def test_a_forget_overwrites_the_value_in_a_copy_without_rewriting_the_copy(followed, monkeypatch):
    """What a copy's Forget deletes is overwritten where it was (``secure_delete``): no VACUUM, which needs room for a
    second vault and, failing on a nearly full disk, would have the copy deleted for nothing. Its digest is made again."""
    from latet.storage import upgrade_copies

    backends, _, copy, gone, _, _ = followed
    db = sqlite3.connect(copy.folder / "latet.db")
    [(sealed,)] = db.execute("SELECT original FROM entities WHERE id = ?", (gone.id,)).fetchall()
    db.close()
    assert bytes(sealed) in (copy.folder / "latet.db").read_bytes()
    statements: list[str] = []
    connect = upgrade_copies._connect

    def traced(path: Path) -> sqlite3.Connection:
        db = connect(path)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(upgrade_copies, "_connect", traced)
    backends.vault.forget_entity(gone.id)
    assert not [s for s in statements if "VACUUM" in s.upper()], statements
    assert bytes(sealed) not in (copy.folder / "latet.db").read_bytes() and _as_made(copy.folder)


# --- round 5: the local reviewer's remembered answers follow a Forget, in the copies too ------------------------


def _reviews_holding(path: Path, value: str) -> list[bytes]:
    """The keys of the reviewer's remembered answers in a vault that hold ``value`` (opened with the test's key)."""
    from latet.storage.db import sealer

    aead = sealer(ROOT_KEY)
    db = sqlite3.connect(path)
    try:
        rows = db.execute("SELECT key, data FROM reviews").fetchall()
    finally:
        db.close()
    return [bytes(key) for key, data in rows if value in aead.decrypt(data[:12], data[12:], None).decode()]


def test_a_value_forgotten_leaves_the_reviewers_answers_in_every_copy_too(followed, held, monkeypatch, tmp_path):
    """A copy keeps the reviewer's remembered answers as the vault does: Forget takes out of every copy those that hold
    the value, as it does from the vault; a copy put back with a Forget still to do loses them all (no key there to tell
    which hold it: they are read again)."""
    from latet.storage import upgrade_copies

    backends, data, _, gone, _, _ = followed
    backends.vault.save_review(b"k" * 32, {"findings": [["mario.rossi@acme.it", "EMAIL"]], "block": False})
    backends.vault.save_review(b"o" * 32, {"findings": [["Emily Carter", "PERSON"]], "block": False})
    clock = iter(range(1_900_000_000, 1_900_100_000, 100))
    monkeypatch.setattr(upgrade_copies, "_clock", lambda: next(clock))
    kept, busy = (upgrade_copies.take(data, data.parent / "config.yaml", "1.1.0", v) for v in ("1.3.0", "1.4.0"))
    assert kept is not None and busy is not None
    for copy in (kept, busy):
        assert _reviews_holding(copy.folder / "latet.db", "mario.rossi") == [b"k" * 32]
    monkeypatch.setattr(upgrade_copies, "_BUSY_SECONDS", 0.05)
    holding = held(busy.folder / "latet.db")  # in use: its Forget is kept for later
    backends.vault.forget_entity(gone.id)
    assert _reviews_holding(kept.folder / "latet.db", "mario.rossi") == []
    assert _reviews_holding(kept.folder / "latet.db", "Emily Carter") == [b"o" * 32]
    assert _as_made(kept.folder)
    holding.execute("ROLLBACK")
    holding.close()
    backends.vault.close()
    backends.history.close()
    again = upgrade_copies.read(busy.folder)
    assert again is not None
    upgrade_copies.put_back(again, data, data.parent / "config.yaml")
    db = sqlite3.connect(data / "latet.db")
    try:
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0
    finally:
        db.close()


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
