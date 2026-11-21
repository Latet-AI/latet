"""The edition: the Community Edition fills no hook (the rules a server's administrators would set for everyone are the
Enterprise Edition's), and nothing in Latet's own code imports the Enterprise Edition."""

import ast
import importlib.util
import inspect
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from latet.config import Config, TermConfig
from latet.edition import Community, Edition, EditionContext, load_edition

SRC = Path(__file__).resolve().parents[2] / "src" / "latet"
NOTHING = {"name": "community", "license": None, "features": [], "pages": [], "roles": [], "actions": []}
# The Community code reaches latet.ee only where it looks for it and takes its absence for the Community Edition:
# load_edition here, and the command line's registration.
LOOKS_FOR_EE = {"edition.py: latet.ee", "cli/admin.py: latet.ee"}


def _context(tmp_path):
    """A server's context: only there is latet.ee looked for (a computer is the Community Edition without looking)."""
    return EditionContext(data=tmp_path / "data", config_dir=tmp_path, server=SimpleNamespace(), clock=time.time)


def test_the_community_edition_shares_no_rules():
    stored = Config(terms=[TermConfig(value="Project Falcon")], profile="max", allow_values=["support@acme.com"])
    assert Community().shared_rules(stored) == {}
    assert "shared_rules" in dir(Edition)


def test_the_community_editions_hooks_do_nothing():
    edition = Community()
    assert edition.name == "community" and edition.floor(None) is None
    assert edition.changes_go_to(None) == "self" and edition.live_review_goes_to(None) == "self"
    assert edition.keeps_events() is False and edition.record({"type": "license"}) is None
    assert edition.synced_terms(None) is None and edition.shared_rules(Config(profile="max")) == {}
    assert edition.dashboard_routes(object()) == [] and edition.server_routes(object()) == []
    assert edition.admin_routes(object(), object()) == []
    assert edition.me(None) == NOTHING and edition.checks() == []
    heard = []
    stop = edition.watch(lambda: heard.append("changed"))  # its floor never changes: nobody is told
    stop()
    assert heard == []
    edition.close()


def test_without_ee_latet_is_the_community_edition(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "latet.ee", None)  # as if src/latet/ee/ were not there
    assert isinstance(load_edition(_context(tmp_path)), Community)


def test_enterprise_code_that_is_there_but_broken_is_raised(tmp_path, monkeypatch):
    broken = types.ModuleType("latet.ee")

    def missing(name):
        raise ModuleNotFoundError("No module named 'joserfc'", name="joserfc")

    broken.__getattr__ = missing  # a dependency of the Enterprise code is not installed
    monkeypatch.setitem(sys.modules, "latet.ee", broken)
    with pytest.raises(ModuleNotFoundError, match="joserfc"):
        load_edition(_context(tmp_path))


def test_enterprise_code_without_its_entry_point_is_raised(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "latet.ee", types.ModuleType("latet.ee"))
    with pytest.raises(ImportError, match="make_edition"):
        load_edition(_context(tmp_path))


def test_a_computer_is_the_community_edition_without_looking_for_latet_ee(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "latet.ee", types.ModuleType("latet.ee"))  # no make_edition: raised if looked for
    computer = EditionContext(data=tmp_path / "data", config_dir=tmp_path, server=None, clock=time.time)
    assert isinstance(load_edition(computer), Community)


def test_nothing_of_a_computers_enterprise_side_is_left_in_the_community_code():
    from latet import edition
    from latet.doctor import checks
    from latet.server import service
    from latet.storage import keys

    assert importlib.util.find_spec("latet.managed") is None
    assert not hasattr(edition, "local_edition") and not hasattr(checks, "check_edition")
    assert not hasattr(keys, "keyring_on")
    assert "parent_extra" not in inspect.signature(service.trusted_path_problem).parameters


def test_nothing_in_latet_imports_the_enterprise_edition():
    found = []
    for path in SRC.rglob("*.py"):
        if SRC / "ee" in path.parents:
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"), str(path))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            found += [
                f"{path.relative_to(SRC).as_posix()}: {name}"
                for name in names
                if name == "latet.ee" or name.startswith(("latet.ee.", "ee.")) or name == "ee"
            ]
    assert set(found) <= LOOKS_FOR_EE, found


_WITHOUT_EE = """
import sys
sys.modules["latet.ee"] = None  # as if src/latet/ee/ were not there
import typer.main
import latet.cli.admin  # registers every command the Community Edition has
from latet.cli.main import app
command = typer.main.get_command(app)
names = set(command.commands)
assert "license" not in names and {"doctor", "status", "server"} <= names, sorted(names)
assert "license" not in command.commands["server"].commands, sorted(command.commands["server"].commands)
from latet.server.db import MIGRATIONS
assert "latet.ee.migrations" not in MIGRATIONS, MIGRATIONS
print("community")
"""


def test_without_ee_the_command_line_has_no_license_command_and_works():
    done = subprocess.run([sys.executable, "-c", _WITHOUT_EE], capture_output=True, text=True, timeout=180, check=False)
    assert done.returncode == 0 and "community" in done.stdout, done.stdout + done.stderr
