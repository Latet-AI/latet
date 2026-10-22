"""The edition: the Community Edition fills no hook (the rules a server's administrators would set for everyone are the
Enterprise Edition's), and nothing in Latet's own code imports the Enterprise Edition."""

import ast
from pathlib import Path

from latet.config import Config, TermConfig
from latet.edition import Community, Edition

SRC = Path(__file__).resolve().parents[2] / "src" / "latet"


def test_the_community_edition_shares_no_rules():
    stored = Config(terms=[TermConfig(value="Project Falcon")], profile="max", allow_values=["support@acme.com"])
    assert Community().shared_rules(stored) == {}
    assert "shared_rules" in dir(Edition)


def test_nothing_in_latet_imports_the_enterprise_edition():
    found = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"), str(path))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            found += [
                f"{path.relative_to(SRC)}: {name}"
                for name in names
                if name == "latet.ee" or name.startswith(("latet.ee.", "ee.")) or name == "ee"
            ]
    assert found == []
