"""What the tests of this folder share: how Latet's parts reach what it keeps, through latet.state's backends, which
a computer and a server (E0b) give differently. Each file takes one subject; a fixture or a helper that only one of
them uses stays in it."""

import asyncio
import time

import pytest

from latet.config import Config, IdentityConfig, LiveReview
from latet.gateway.approvals import Batch
from latet.runtime import make_runtime

from ...conftest import ROOT_KEY

EMAIL = "emily.carter@acme.com"
PROMPT = f"ask {EMAIL} about the Orion contract"


def _live_runtime(tmp_path, **live):
    """A runtime with the Live Reviewer on: what the plugin brings that is new waits for you."""
    config = Config(identity=IdentityConfig(enabled=False), live_review=LiveReview(enabled=True, **live))
    return make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)


@pytest.fixture
def live(tmp_path, request):
    """A runtime with the Live Reviewer on (``indirect`` parametrization gives it other settings)."""
    rt = _live_runtime(tmp_path, **getattr(request, "param", {}))
    yield rt
    rt.close()


def _text_waiting(**kwargs):
    """A batch of one text, as the Live Reviewer holds it, with nothing masked in it yet."""
    item = {"kind": "user_text", "path": None, "tool": None, "original": "call Ada Byron", "masked": "call Ada Byron",
            "replacements": [], "suggestions": [], "failed": None}  # fmt: skip
    return Batch("cc:s", None, "claude-code", [item], **kwargs)


async def _until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"not within {seconds} seconds"
        await asyncio.sleep(0.01)
