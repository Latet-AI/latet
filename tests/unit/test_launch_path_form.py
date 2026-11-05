"""The path form of a launch key (``/_k/<key>/<route>/...``): kept on a computer as the owner decided, accepted only by a
computer's ingress, refused by a server's gate, and emitted by no launcher today (root's ruling I-4 on P2-6a's review:
the carriers that ship are headers; an agent with no verified header carrier passes no key and is claimed)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from latet.cli.main import app
from latet.gateway.launch_keys import LocalLaunchKeys
from latet.gateway.local_launch import take_local_launch

from .test_wrapper_routes import launched  # noqa: F401 - fixture

SRC = Path(__file__).resolve().parents[2] / "src" / "latet"
# Where the path form may be named: the computer's ingress that takes it, the server's gate that refuses it, the
# access-log redactor and the server log's note on it. No launcher, wrapper or integration builds such a URL.
_NAMERS = {"gateway/local_launch.py", "server/app.py", "logline.py", "server/logs.py"}


def test_no_launcher_builds_the_path_form():
    named = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text("utf-8")
        if "/_k" in text or "_k/" in text:
            named.add(path.relative_to(SRC).as_posix())
    assert named <= _NAMERS, named - _NAMERS


@pytest.mark.parametrize("agent", ["codex", "opencode", "gemini", "copilot"])
def test_no_launched_agent_is_given_the_path_form(launched, agent):  # noqa: F811
    runs, _, _ = launched
    args = [agent, "exec", "hi"] if agent == "codex" else [agent, "-p", "hi"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code in (0, 2), result.output
    for run in runs:
        assert all("/_k/" not in str(value) for value in run["env"].values()), run["env"]
        assert all("/_k/" not in str(argument) for argument in run["argv"])


def test_only_a_computer_takes_the_path_form():
    owner = LocalLaunchKeys(b"\x26" * 32)
    key = "latet_lk_" + "A" * 43
    path = f"/_k/{key}/v1/chat/completions"
    try:
        taken = {"type": "http", "path": path, "raw_path": path.encode(), "headers": []}
        assert take_local_launch(taken, owner, computer=True).status == "claimed"  # a key it does not know: a claim
        assert taken["path"] == "/v1/chat/completions" and key.encode() not in taken["raw_path"]
        refused = {"type": "http", "path": path, "raw_path": path.encode(), "headers": []}
        assert take_local_launch(refused, owner, computer=False).status == "invalid"
        assert refused["path"] == "/" and refused["raw_path"] == b"/"
    finally:
        owner.close()


_PLAN = SRC.parents[1] / "notes" / "server-request-coverage-plan.md"


@pytest.mark.skipif(not _PLAN.exists(), reason="the plan is a private note, not exported")
def test_the_plan_records_g2s_ruling_on_the_unanchored_carriers():
    """Root's ruling on P2-6a's re-review (R-5): G2 requires the pinned versions and source anchors of Gemini CLI's and
    OpenCode's carriers, or else their launchers pass no key and their sessions show claimed."""
    plan = _PLAN.read_text("utf-8")
    assert "root to rule whether G2 needs them first" not in plan
    assert plan.count("G2 requires the pinned versions and source anchors") >= 3  # the RED, and each ledger row
