"""The local reviewer in the real gateway: what it finds never reaches the upstream, and comes back restored."""

import httpx
import pytest
from starlette.testclient import TestClient

from latet.config import Config
from latet.engine.build import build
from latet.engine.reviewer import ReviewClient, Reviewer
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from ..unit.test_reviewer import FakeServer
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

PROMPT = "mia moglie Maria ha la chemio giovedì: scrivi al dottor Ferri"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


def _client(upstream, server, **review):
    config = Config(llm_review={"enabled": True, "model": "fake-model", **review})
    built = build(config, Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    mediator.llm_review = config.llm_review
    client = ReviewClient(config.llm_review.url, "fake-model", transport=httpx.MockTransport(server))
    mediator.reviewer = Reviewer(config.llm_review, b"r" * 32, client=client)
    gateway = GatewayConfig(port=5283, upstreams=[Upstream("anthropic", "/anthropic", upstream.url)], mediator=mediator)
    return TestClient(create_app(gateway), base_url="http://127.0.0.1:5283")


def test_what_the_reviewer_finds_never_reaches_the_upstream(upstream):
    with _client(upstream, FakeServer()) as c:
        body = {"model": "x", "messages": [{"role": "user", "content": PROMPT}]}
        r = c.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"})
    assert r.status_code == 200
    sent = upstream.all_received()
    for value in ("Maria", "chemio", "Ferri"):
        assert value not in sent
    assert "Maria" in r.text and "chemio" in r.text  # the echo comes back restored


def test_with_the_reviewer_down_nothing_leaves(upstream):
    async def refuse(request):
        raise httpx.ConnectError("refused")

    before = len(upstream.all_received())
    with _client(upstream, refuse) as c:
        body = {"model": "x", "messages": [{"role": "user", "content": PROMPT}]}
        r = c.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "test"})
    assert r.status_code == 400 and "local reviewer" in r.text
    assert len(upstream.all_received()) == before


def test_the_daemon_keeps_its_reviewer_until_the_server_changes(tmp_path):
    config = Config(llm_review={"enabled": True, "model": "m"})
    rt = make_runtime(config, data=tmp_path, record=True, config_file=tmp_path / "config.yaml")
    first = rt.mediator.reviewer
    assert first is not None and rt.mediator.on_review is not None
    rt.apply_config(Config(llm_review={"enabled": True, "model": "m", "block": True}), save=False)
    assert rt.mediator.reviewer is first and first.config.block
    rt.apply_config(Config(llm_review={"enabled": True, "model": "other"}), save=False)
    assert rt.mediator.reviewer is not first and first in rt.mediator.retired
    rt.apply_config(Config(), save=False)
    assert rt.mediator.reviewer is None
