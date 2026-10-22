"""Latet comes from its releases on GitHub, never from PyPI: the wheel of the latest release (it carries the
dashboard), with local name detection and the text in images. What `latet upgrade` installs, and what the
commands say when those two are missing."""

import shutil
import subprocess

import httpx
import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers upgrade
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
