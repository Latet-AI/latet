"""Exercise the root fixtures against a disposable stand-in for the person's original home."""

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
HOMES = ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA")

_PROBE = """
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from pytest_socket import SocketConnectBlockedError

from latet.integrations.agents import Codex
from latet.integrations.base import edit_text
from tests.unit.conftest import quiet  # noqa: F401 - the real nested fixture which removes CODEX_HOME

HOMES = ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA")
BASELINE = Path(os.environ["ISOLATION_SENTINEL"])
COLLECTION_HOMES = {name: Path(os.environ[name]) for name in HOMES}
COLLECTION_ROOT = Path.home().parent
print("ISOLATED_HOME_ROOT=" + str(COLLECTION_ROOT), flush=True)
seen = []


def deny_native_connection(event, arguments):
    if event == "socket.connect":
        raise AssertionError("nested probe reached native socket.connect")


sys.addaudithook(deny_native_connection)


def test_the_nested_run_blocks_non_loopback_before_native_networking():
    with pytest.warns(UserWarning, match="203[.]0[.]113[.]1"):
        with pytest.raises(SocketConnectBlockedError), socket.socket() as connection:
            connection.connect(("203.0.113.1", 443))


def test_all_seven_paths_are_redirected():
    assert COLLECTION_ROOT.parent == Path(os.environ["ISOLATION_EXPECTED_ROOT"]).resolve()
    if "LATET_TEST_HOME_ROOT" not in os.environ:
        assert COLLECTION_ROOT.name.startswith(".latet-test-homes-")
    for name in HOMES:
        assert not COLLECTION_HOMES[name].is_relative_to(BASELINE), name
        assert not Path(os.environ[name]).is_relative_to(BASELINE), name
        assert not COLLECTION_HOMES[name].is_relative_to(Path(tempfile.gettempdir()).resolve()), name
        assert not Path(os.environ[name]).is_relative_to(Path(tempfile.gettempdir()).resolve()), name
    assert Path.home() != BASELINE
    seen.append({name: os.environ[name] for name in HOMES})


def test_a_later_test_gets_a_fresh_home():
    assert len(seen) == 1
    for name in HOMES:
        assert os.environ[name] != seen[0][name], name


@pytest.mark.usefixtures("quiet")
def test_the_nested_fixture_can_remove_codex_home_without_reaching_the_baseline():
    assert "CODEX_HOME" not in os.environ
    target = Codex().config_path()
    edit_text(target, lambda text: 'model = "isolated"\\n')
    assert target.is_relative_to(Path.home())
    assert not target.is_relative_to(BASELINE)
    assert target.read_text() == 'model = "isolated"\\n'


def test_subprocesses_inherit_the_seven_isolated_paths():
    code = 'import json, os; from pathlib import Path; print(json.dumps([str(Path.home()), {name: os.environ[name] for name in ' + repr(HOMES) + '}]))'
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=30)
    assert done.returncode == 0, done.stderr
    home, variables = json.loads(done.stdout)
    assert Path(home) == Path.home()
    assert variables == {name: os.environ[name] for name in HOMES}


@pytest.mark.skipif(os.name != "nt", reason="Windows account-home fallback")
def test_a_subprocess_without_userprofile_still_has_an_isolated_windows_home():
    environment = dict(os.environ)
    environment.pop("USERPROFILE")
    done = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        env=environment, capture_output=True, text=True, check=False, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert Path(done.stdout.strip()) == Path.home()


def test_an_intentional_fixture_home_override_remains_usable(tmp_path, monkeypatch):
    for name in HOMES:
        monkeypatch.setenv(name, str(tmp_path))
    assert Path.home() == tmp_path
    target = Codex().config_path()
    edit_text(target, lambda text: 'model = "override"\\n')
    assert target == tmp_path / "config.toml"
    assert target.read_text() == 'model = "override"\\n'


@pytest.mark.parametrize("missing", [False, True])
def test_home_resolution_refuses_before_a_write_when_isolation_is_lost(monkeypatch, missing):
    with monkeypatch.context() as changed:
        changed.delenv("CODEX_HOME", raising=False)
        for name in ("HOME", "USERPROFILE"):
            if missing:
                changed.delenv(name, raising=False)
            else:
                changed.setenv(name, str(BASELINE))
        with pytest.raises(RuntimeError, match="test home isolation"):
            target = Codex().config_path()
            # Even a mutation of the guard cannot let this probe write outside its sentinel.
            assert target.is_relative_to(BASELINE)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("must never be written")
"""


@pytest.mark.parametrize("explicit_root", [True, False])
def test_root_fixtures_isolate_homes_before_fallbacks_and_subprocesses(tmp_path, explicit_root):
    # This original home is scratch too: the RED and mutations can only damage a sentinel we own.
    with tempfile.TemporaryDirectory(prefix="latet-home-probe-", dir=Path(os.environ["HOME"]).parent) as folder:
        root = Path(folder)
        baseline = root / "original-home"
        baseline.mkdir()
        sentinel = baseline / ".codex" / "config.toml"
        sentinel.parent.mkdir()
        sentinel.write_text('model = "sentinel"\n', encoding="utf-8")
        homes = root / "test-homes"
        homes.mkdir()
        probe = tmp_path / "test_probe.py"
        probe.write_text(textwrap.dedent(_PROBE), encoding="utf-8")
        environ = {
            **os.environ,
            **dict.fromkeys(HOMES, str(baseline)),
            "ISOLATION_SENTINEL": str(baseline),
            "ISOLATION_EXPECTED_ROOT": str(homes if explicit_root else REPO),
            "PYTHONPATH": os.pathsep.join((str(REPO / "src"), str(REPO))),
        }
        if explicit_root:
            environ["LATET_TEST_HOME_ROOT"] = str(homes)
        else:
            environ.pop("LATET_TEST_HOME_ROOT", None)
        if os.name == "nt":
            environ.update(HOMEDRIVE=baseline.drive, HOMEPATH=str(baseline)[len(baseline.drive) :])
        run = [
            sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "-p", "tests.conftest",
            "--rootdir", str(tmp_path), "--confcutdir", str(tmp_path), "--basetemp", str(tmp_path / "pytest"),
            "-o", "addopts=", "--disable-socket", "--allow-hosts=127.0.0.1,::1", str(probe),
        ]  # fmt: skip
        done = subprocess.run(run, cwd=tmp_path, env=environ, capture_output=True, text=True, timeout=120, check=False)
        assert sentinel.read_text(encoding="utf-8") == 'model = "sentinel"\n', done.stdout + done.stderr
        if not explicit_root and REPO.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            assert done.returncode != 0, done.stdout + done.stderr
            assert "test home isolation requires LATET_TEST_HOME_ROOT outside system TEMP" in done.stderr
            assert list(homes.iterdir()) == []
            return
        assert done.returncode == 0, done.stdout + done.stderr
        assert ("9 passed" if os.name == "nt" else "8 passed") in done.stdout
        created = next(line for line in done.stdout.splitlines() if line.startswith("ISOLATED_HOME_ROOT="))
        assert not Path(created.removeprefix("ISOLATED_HOME_ROOT=")).exists()
        assert list(homes.iterdir()) == []


@pytest.mark.parametrize("explicit_root", [True, False])
def test_a_home_root_under_system_temp_is_refused_before_allocation(tmp_path, explicit_root):
    root = tmp_path / "homes"
    root.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    environ = {**os.environ, "PYTHONPATH": os.pathsep.join((str(REPO / "src"), str(REPO)))}
    if explicit_root:
        environ["LATET_TEST_HOME_ROOT"] = str(root)
    else:
        environ.pop("LATET_TEST_HOME_ROOT", None)
    # The alternate __file__ is a synthetic checkout under TEMP, without copying or importing a real user's files.
    probe = """
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text(encoding="utf-8")
exec(compile(source, sys.argv[1], "exec"), {"__file__": sys.argv[2], "__name__": "root_containment_probe"})
"""
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(probe), str(REPO / "tests" / "conftest.py"),
         str(checkout / "tests" / "conftest.py")],
        cwd=tmp_path, env=environ, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert done.returncode != 0, done.stdout + done.stderr
    assert "test home isolation requires LATET_TEST_HOME_ROOT outside system TEMP" in done.stderr
    assert list(root.iterdir()) == []
    assert list(checkout.iterdir()) == []


def test_the_service_default_stays_outside_temp_without_an_explicit_home_root(tmp_path):
    environ = {**os.environ, "PYTHONPATH": os.pathsep.join((str(REPO / "src"), str(REPO)))}
    environ.pop("LATET_TEST_HOME_ROOT", None)
    node = "test_the_root_is_kept_in_a_folder_of_the_users_own_never_the_shared_temp_folder"
    run = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--rootdir", str(REPO), "--confcutdir", str(REPO), "--basetemp", str(tmp_path / "pytest"),
        "-o", "addopts=", "--disable-socket", "--allow-hosts=127.0.0.1,::1",
        str(REPO / "tests" / "unit" / "server" / "test_services_script.py") + "::" + node,
    ]  # fmt: skip
    done = subprocess.run(run, cwd=tmp_path, env=environ, capture_output=True, text=True, timeout=30, check=False)
    if REPO.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        assert done.returncode != 0, done.stdout + done.stderr
        assert "test home isolation requires LATET_TEST_HOME_ROOT outside system TEMP" in done.stderr
        return
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 passed" in done.stdout
