"""A request Latet refuses before it is masked, a masking that fails while the request is checked or a body that would
decode past the bound, is recorded as one it stopped: blocked, its reason in Latet's words, nothing sent. It was shown
as a request sent unread, with an error."""

import gzip
import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.engine.masker import Masker
from latet.engine.vault import MaskingError
from latet.gateway.app import create_app
from latet.gateway.forward import too_large
from latet.gateway.routes import Upstream
from latet.protocols.codec import DecodedTooLarge
from latet.runtime import make_runtime

from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

CLAUDE = {"user-agent": "claude-cli/2.1.291 (external, cli)", "x-claude-code-session-id": "cc-refused"}
AUTH = {"authorization": "Bearer sk-test"}
LACKING = "a value this vault's store keeps is not in memory"


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture(params=["full", "metadata"])
def runtime(request, tmp_path, upstream):
    config = Config(identity=IdentityConfig(enabled=False), history=request.param)
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [Upstream("anthropic", "/anthropic", upstream.url)]
    yield rt
    rt.close()


def _row(rt) -> dict:
    assert rt.recorder.drain()
    [row] = rt.exchanges.summaries_since(0)
    return row


def test_a_masking_that_fails_while_the_request_is_checked_stops_it_and_shows_it_stopped(
    runtime, upstream, monkeypatch
):
    def lacking(self):
        raise MaskingError(LACKING)

    monkeypatch.setattr(Masker, "_known_values", lacking)  # the check's masker, outside the masking's own try
    before = len(upstream.requests)
    body = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hello"}]}
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        answer = client.post("/anthropic/v1/messages", json=body, headers=CLAUDE)
    assert answer.status_code == 400 and "Nothing was sent" in answer.text
    assert len(upstream.requests) == before
    row = _row(runtime)
    assert row["status"] == 400 and row["block_reason"] == f"masking failed: {LACKING}"
    assert "blocked" in row["flags"] and "error" not in row["flags"] and "unknown-route" not in row["flags"]
    assert (row["mode"], row["protocol"], row["verified"]) == ("verify", "anthropic", False)


def test_a_body_that_would_decode_past_the_bound_is_shown_stopped_with_the_bound(runtime, upstream):
    runtime.mediator.body_limit = 2000  # a server's max_request_mb, made small
    before = len(upstream.requests)
    big = gzip.compress(json.dumps({"model": "m", "messages": [{"role": "user", "content": "x" * 50_000}]}).encode())
    headers = AUTH | {"content-type": "application/json", "content-encoding": "gzip"}
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        answer = client.post("/anthropic/v1/messages", content=big, headers=headers)
    assert answer.status_code == 413 and "max_request_mb" in answer.text and "Nothing was sent" in answer.text
    assert len(upstream.requests) == before
    row = _row(runtime)
    assert row["status"] == 413 and row["block_reason"] == too_large(DecodedTooLarge(2000))
    assert "blocked" in row["flags"] and "error" not in row["flags"]
    assert (row["mode"], row["protocol"], row["verified"]) == ("verify", "anthropic", False)
