"""The tutorial's sandbox is the Community Edition, whatever license this computer has: the tutorial shows Latet as
everyone has it."""

import pytest

from latet.edition import Community

from .tutorial_sandbox import API, opened

pytestmark = pytest.mark.e2e


async def test_the_tutorials_sandbox_is_the_community_edition(tmp_path, monkeypatch):
    async with opened(tmp_path, monkeypatch) as tutorial:
        assert isinstance(tutorial.sandbox.runtime.edition, Community)
        edition = (await tutorial.client.get(f"{API}/me")).json()["edition"]
        assert edition == {
            "name": "community",
            "license": None,
            "features": [],
            "pages": [],
            "roles": [],
            "actions": [],
        }
