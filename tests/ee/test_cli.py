"""`latet server license` is a command of `latet server` whatever is imported first (a computer has no `latet license`:
the Enterprise Edition is a Latet server's, tests/ee/test_computer.py), and reads the file it installs as a license
file is read: a regular file, never whole when it is longer than a license may be."""

import pytest
from typer.testing import CliRunner

from latet.cli.agent import MARKERS
from latet.ee import license as license_module


@pytest.fixture(autouse=True)
def yourself(monkeypatch):
    """Run by you, not by an agent: `latet server` refuses an agent before it reads anything."""
    for markers in MARKERS.values():
        for name in markers:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AGENT", raising=False)


def _install(path):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, ["server", "license", "install", str(path)])


def test_install_reads_a_license_only_up_to_its_size_and_says_a_longer_file_is_too_large(tmp_path):
    # Read up to one byte over what a license may be (the server refuses that one, saying why), never further: two
    # bytes over, and far over, are refused here, before any server is asked.
    over = license_module.MAX_BYTES + 2
    for size in (over, 5_000_000):
        path = tmp_path / f"big-{size}.json"
        path.write_bytes(b" " * size)
        result = _install(path)
        assert result.exit_code == 2 and "larger than 64 KB" in result.output, (size, result.output)


def test_install_reads_a_regular_file_only(tmp_path):
    result = _install(tmp_path)  # a folder
    assert result.exit_code == 2 and "cannot be read" in result.output


def test_latet_server_license_joins_latet_server_whatever_is_imported_first():
    """M1 of the final review: imported before latet.cli.server (a new process loading latet.ee.cli first), `latet
    server license` is still a command of `latet server`, never silently missing."""
    import subprocess
    import sys

    code = (
        "import latet.ee.cli\n"
        "from latet.cli.server import server_app\n"
        "print(sorted(group.name for group in server_app.registered_groups))\n"
    )
    ran = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    assert ran.returncode == 0, ran.stderr
    assert "'license'" in ran.stdout and ran.stdout.count("'license'") == 1, ran.stdout


def test_the_feature_the_license_command_weighs_for_central_approvals_is_theirs():
    """The final security review's L1: `latet server license` names central approvals' feature itself, since their
    module loads the server's own; it is the one their routes check."""
    from latet.ee import cli
    from latet.ee.approvals_api import FEATURE

    assert cli.APPROVALS == FEATURE
