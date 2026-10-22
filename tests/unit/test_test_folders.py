"""A run of the tests leaves none of its own folders behind: the data folder every run makes for itself (keys the tests
save go there, the keyring being off) and the folder of the master key a run's test servers share (a test key, and a
key file all the same). Each is removed when the run ends, a run whose tests fail too."""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).parents[2]

_PROBE = """
import os
import socket
import sys

import pytest
from pytest_socket import SocketConnectBlockedError

from tests.server.support import master_key_file


def deny_native_connection(event, arguments):
    if event == "socket.connect":
        raise AssertionError("nested probe reached native socket.connect")


sys.addaudithook(deny_native_connection)


def test_it_makes_both_folders_and_fails():
    with pytest.warns(UserWarning, match="203[.]0[.]113[.]1"):
        with pytest.raises(SocketConnectBlockedError), socket.socket() as connection:
            connection.connect(("203.0.113.1", 443))
    print("made|" + str(master_key_file().parent) + "|" + os.environ["LATET_DATA_DIR"], flush=True)
    assert False
"""


def test_a_run_of_the_tests_leaves_none_of_its_folders_behind(tmp_path):
    # An unrelated parent's conftest must never be imported by the nested run.
    (tmp_path / "conftest.py").write_text('raise RuntimeError("collected outside the probe root")\n', "utf-8")
    own_root = tmp_path / "probe"
    own_root.mkdir()
    probe = own_root / "test_probe.py"
    probe.write_text(textwrap.dedent(_PROBE), "utf-8")
    run = [
        sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "-p", "tests.conftest",
        "--rootdir", str(own_root), "--confcutdir", str(own_root), "--basetemp", str(own_root / "pytest"),
        "-o", "addopts=", "--disable-socket", "--allow-hosts=127.0.0.1,::1", str(probe),
    ]  # fmt: skip
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(REPO / "src"), str(REPO)))}
    done = subprocess.run(run, cwd=own_root, env=environment, capture_output=True, text=True, timeout=120, check=False)
    made = [line for line in done.stdout.splitlines() if line.startswith("made|")]
    assert done.returncode == 1 and len(made) == 1, done.stdout[-2000:]  # its test failed, after making both
    _, key_folder, data = made[0].split("|")
    folders = [Path(key_folder), Path(data).parent]
    try:
        assert [f.name.split("-")[1] for f in folders] == ["server", "tests"]  # latet-server-tests-…, latet-tests-…
        assert [f for f in folders if f.exists()] == []
    finally:
        for folder in folders:  # this test's own, should they be left: none of the folders made before it
            shutil.rmtree(folder, ignore_errors=True)
