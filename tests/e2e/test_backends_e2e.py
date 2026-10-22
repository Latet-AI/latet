"""The state the interfaces note lists is behind a Latet's backends: two runtimes made over the same ones act as one
Latet, as two instances of a Latet server do over PostgreSQL and Valkey (E0b). Their vaults are their own, as an
instance's is: a value one masked is found by the other through the store they share. (What a conversation's texts
were when they left, and what the final check saw leave, are there too since E0b: ``StateBackends.given``.)"""

import asyncio
import contextlib

import httpx
import pytest

from latet.config import Config, IdentityConfig, LiveReview, save_config
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime
from latet.state import local_backends

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
ENGINE = "/_latet/engine/v1"
EMAIL = "emily.carter@acme.com"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@contextlib.asynccontextmanager
async def _two(tmp_path, upstream, **settings):
    """Two runtimes over one set of backends, their apps started, a client for each."""
    config = Config(identity=IdentityConfig(enabled=False), **settings)
    save_config(config, tmp_path / "config.yaml")
    backends = local_backends(tmp_path / "data", tmp_path / "config.yaml", ROOT_KEY)
    runtimes = [
        make_runtime(
            config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY, backends=backends
        )
        for _ in range(2)
    ]
    apps = []
    for rt in runtimes:
        rt.gateway.upstreams = [Upstream("openai", "/openai", upstream.url)]
        apps.append(create_app(rt.gateway))
    try:
        async with contextlib.AsyncExitStack() as stack:
            clients = []
            for app in apps:
                await stack.enter_async_context(app.router.lifespan_context(app))
                transport = httpx.ASGITransport(app=app)
                client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False)
                clients.append(await stack.enter_async_context(client))
            yield runtimes, clients
    finally:
        for rt in runtimes:
            rt.close()


async def _signed_in(client, rt):
    token = {"x-latet-token": rt.gateway.bridge_token}
    code = (await client.post("/_latet/admin/login-code", headers=token)).json()["code"]
    assert (await client.get(f"/_latet/ui/login?code={code}", follow_redirects=False)).status_code == 303


async def test_a_conversation_masked_by_one_is_restored_by_the_other_and_no_other(tmp_path, upstream):
    async with _two(tmp_path, upstream) as ((one, _), (a, b)):
        token = {"x-latet-token": one.gateway.bridge_token}
        first = {"session": "s1", "items": [{"text": f"write to {EMAIL}", "kind": "user_text"}]}
        [masked] = (await a.post(f"{ENGINE}/mask", json=first, headers=token)).json()["items"]
        fake = masked["text"].removeprefix("write to ")
        again = {"session": "s3", "items": [{"text": f"and to {EMAIL}", "kind": "user_text"}]}
        [theirs] = (await b.post(f"{ENGINE}/mask", json=again, headers=token)).json()["items"]
        assert EMAIL not in fake and theirs["text"] == f"and to {fake}"  # the same fake: the store kept it first
        back = {"session": "s1", "items": [f"You said: write to {fake}"]}
        restored = (await b.post(f"{ENGINE}/rehydrate", json=back, headers=token)).json()["items"]
        assert restored == [f"You said: write to {EMAIL}"]  # the other runtime knows what s1 was shown
        other = {"session": "s2", "items": [f"You said: write to {fake}"]}
        assert EMAIL not in (await b.post(f"{ENGINE}/rehydrate", json=other, headers=token)).json()["items"][0]


async def test_a_decision_in_one_dashboard_lets_a_request_waiting_in_the_other_leave(tmp_path, upstream):
    body = {"model": "m", "messages": [{"role": "user", "content": f"write to {EMAIL} about Orion"}]}
    async with _two(tmp_path, upstream, live_review=LiveReview(enabled=True)) as ((_, two), (a, b)):
        await _signed_in(b, two)
        sent = asyncio.ensure_future(
            a.post("/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-x"})
        )
        batches = []
        for _ in range(200):
            batches = (await b.get(f"{API}/approvals")).json()["batches"]
            if batches:
                break
            await asyncio.sleep(0.05)
        [batch] = batches
        decided = await b.post(f"{API}/approvals/{batch['id']}", json={"action": "approve"}, headers=UI)
        assert decided.status_code == 200
        assert (await asyncio.wait_for(sent, 10)).status_code == 200
    assert EMAIL not in upstream.all_received()


async def test_a_code_one_gives_signs_in_at_the_other(tmp_path, upstream):
    async with _two(tmp_path, upstream) as ((one, _), (a, b)):
        token = {"x-latet-token": one.gateway.bridge_token}
        code = (await a.post("/_latet/admin/login-code", headers=token)).json()["code"]
        assert (await b.get(f"/_latet/ui/login?code={code}", follow_redirects=False)).status_code == 303
        assert (await b.get(f"{API}/me")).status_code == 200
