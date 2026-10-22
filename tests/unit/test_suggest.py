"""`latet rules suggest`: shapes, counts and labels from a folder, and never a value."""

import json
import random
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from latet.rules_report import OK, outcome, report
from latet.suggest import pattern_for, suggest

LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A repository with badge numbers in a CSV, invoice references in code, and an ignored file."""
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "latet"))
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / "private").mkdir()
    rng = random.Random(3)
    badges = [f"{rng.choice(LETTERS)}{rng.choice(LETTERS)}-{rng.randrange(10**6):06d}" for _ in range(30)]
    invoices = [f"INV{rng.randrange(10**7):07d}" for _ in range(8)]
    hidden = [f"zq{rng.randrange(10**6):06d}zq" for _ in range(12)]
    rows = ["name,badge,team", *(f"p{i},{b},t{i % 3}" for i, b in enumerate(badges))]
    (root / "data" / "staff.csv").write_text("\n".join(rows) + "\n", "utf-8")
    code = "\n".join(f'invoice_ref = "{v}"' for v in invoices)
    code += "\nVERSION = '1.2.3'\nID = '123e4567-e89b-12d3-a456-426614174000'\nSHA = 'a94a8fe5ccb19ba61c4c'\n"
    (root / "data" / "billing.py").write_text(code, "utf-8")
    (root / "private" / "notes.txt").write_text("\n".join(hidden), "utf-8")
    (root / ".gitignore").write_text("private/\n", "utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root, badges + invoices + hidden


def test_shapes_counts_and_labels(folder):
    root, _ = folder
    result = suggest(root)
    shapes = {s["shape"]: s for s in result["shapes"]}
    assert set(shapes) == {"AA-999999", "AAA9999999"}  # no version, UUID, hash, nor the ignored file
    badge = shapes["AA-999999"]
    assert (badge["distinct"], badge["count"], badge["file_count"]) == (30, 30, 1)
    assert badge["labels"] == [{"word": "badge", "values": 30}]  # the CSV column header
    assert shapes["AAA9999999"]["labels"][0]["word"] == "invoice_ref"


def test_values_never_appear_in_any_output(folder):
    root, values = folder
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    runner = CliRunner()
    outputs = [runner.invoke(app, ["rules", "suggest", str(root), *flags]) for flags in ([], ["--json"], ["--yaml"])]
    for result in outputs:
        assert result.exit_code == 0, result.output
        assert not [v for v in values if v in result.output]
    assert json.loads(outputs[1].output)["shapes"]
    refused = runner.invoke(app, ["rules", "suggest", str(root), "--show-values"])  # not a terminal
    assert refused.exit_code == 2 and not [v for v in values if v in refused.output]


def test_drafts_pass_their_own_test(folder):
    root, values = folder
    result = suggest(root, draft_limit=5)
    assert len(result["drafts"]) == 2
    assert outcome(report(result["drafts"])) == OK
    text = yaml.safe_dump(result["drafts"])
    assert not [v for v in values if v in text]  # examples are invented from the shapes
    badge = next(d for d in result["drafts"] if d["pattern"] == pattern_for("AA-999999"))
    assert badge["context"] == {"words": ["badge"]}


def test_patterns_from_shapes():
    assert pattern_for("AA-999999") == r"\b[A-Z]{2}-\d{6}\b"
    assert pattern_for("aa_9999_A") == r"\b[a-z]{2}_\d{4}_[A-Z]\b"
