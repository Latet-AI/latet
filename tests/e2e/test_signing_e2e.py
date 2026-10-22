"""Bedrock signed by Latet end to end: only for you, only to Amazon, never recorded, renewed when stale."""

import base64
import json
import subprocess
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.gateway.app import create_app
from latet.gateway.aws import AwsCredentialStore, profile_tag
from latet.gateway.cloud import BedrockUpstream
from latet.gateway.sigv4 import AwsCredentials, sign
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

CLAUDE_CODE = {"user-agent": "claude-cli/2.1.294 (external, sdk-cli)", "x-claude-code-session-id": "cc-sign"}
CALL = "/model/us.anthropic.claude-opus-5-5/invoke"
BODY = b'{"anthropic_version":"bedrock-2023-05-31","max_tokens":5,"messages":[{"role":"user","content":"hello"}]}'


def _export(token: str) -> str:
    return json.dumps(
        {
            "Version": 1,
            "AccessKeyId": "ASIAEXAMPLE",
            "SecretAccessKey": "the-secret",
            "SessionToken": token,
            "Expiration": "2099-01-01T00:00:00+00:00",
        }
    )


class FakeAwsCli:
    def __init__(self, *tokens: str) -> None:
        self.tokens = list(tokens) or ["the-token"]
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        token = self.tokens[min(self.calls, len(self.tokens)) - 1]
        if token == "sso-expired":
            return subprocess.CompletedProcess(argv, 255, "", "Error when retrieving token from sso: Token has expired")
        return subprocess.CompletedProcess(argv, 0, _export(token), "")


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def aws():
    return FakeAwsCli()


@pytest.fixture
def runtime(tmp_path, monkeypatch, upstream, aws):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    rt.gateway.upstreams = [BedrockUpstream(runtime=upstream.url, control=upstream.url)]
    rt.gateway.aws = AwsCredentialStore(which=lambda name: "aws", run=aws)
    yield rt
    rt.close()


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as c:
        yield c


def _route(runtime, profile="work") -> str:
    return f"/bedrock/us-east-1/profile/{profile}/{profile_tag(runtime.gateway.bridge_token, profile)}"


def _post(client, path, headers=CLAUDE_CODE):
    return client.post(path, content=BODY, headers={**headers, "content-type": "application/json"})


def _check_signature(upstream, credentials):
    sent = upstream.requests[-1]
    when = datetime.strptime(sent["headers"]["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    expected = dict(
        sign("POST", upstream.url + sent["path"], upstream.received[-1], credentials, "us-east-1", now=when)
    )
    assert sent["headers"]["authorization"] == expected["authorization"]
    assert sent["headers"]["authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={credentials.access_key}/")


def test_latet_signs_with_your_aws_profile(client, runtime, upstream):
    r = _post(client, _route(runtime) + CALL, headers={**CLAUDE_CODE, "authorization": "Bearer from-the-client"})
    assert r.status_code == 200
    assert upstream.requests[-1]["path"] == CALL and upstream.received[-1] == BODY
    assert upstream.requests[-1]["headers"]["x-amz-security-token"] == "the-token"
    _check_signature(upstream, AwsCredentials("ASIAEXAMPLE", "the-secret", "the-token"))


def test_a_key_not_made_by_this_latet_signs_nothing(client, runtime, upstream, aws):
    before = len(upstream.received)
    r = _post(client, f"/bedrock/us-east-1/profile/work/{'0' * 32}{CALL}")
    assert r.status_code == 404
    assert len(upstream.received) == before and aws.calls == 0


def test_a_session_signs_until_it_is_dropped(client, runtime, upstream):
    token = {"x-latet-token": runtime.gateway.bridge_token}
    secret = {"access_key": "AKIDSESSION", "secret_key": "s3cr3t", "token": "session-token"}
    assert client.post("/_latet/engine/v1/aws-sessions", json=secret).status_code == 401
    session = client.post("/_latet/engine/v1/aws-sessions", json=secret, headers=token).json()["id"]
    assert _post(client, f"/bedrock/us-east-1/session/{session}{CALL}").status_code == 200
    _check_signature(upstream, AwsCredentials("AKIDSESSION", "s3cr3t", "session-token"))
    assert client.delete(f"/_latet/engine/v1/aws-sessions/{session}", headers=token).json() == {"dropped": True}
    r = _post(client, f"/bedrock/us-east-1/session/{session}{CALL}")
    assert r.status_code == 400 and "latet claude" in r.json()["message"]


def test_the_record_keeps_no_key(client, runtime):
    route = _route(runtime)
    _post(client, route + CALL)
    runtime.recorder.drain()
    [last] = runtime.exchanges.list(limit=1)[0]
    assert last["path"] == "/bedrock/us-east-1" + CALL and last["signed"] == "aws-profile:work"
    assert route.rsplit("/", 1)[1] not in json.dumps(last)


@pytest.mark.parametrize("aws", [FakeAwsCli("sso-expired")])
def test_an_expired_aws_login_sends_nothing_and_says_what_to_do(client, runtime, upstream, aws):
    before = len(upstream.received)
    r = _post(client, _route(runtime) + CALL)
    assert r.status_code == 400 and "aws sso login --profile work" in r.json()["message"]
    assert len(upstream.received) == before
    runtime.recorder.drain()
    [last] = runtime.exchanges.list(limit=1)[0]
    assert "aws sso login --profile work" in last["block_reason"]  # the dashboard says it in plain words


@pytest.mark.parametrize("aws", [FakeAwsCli("expired-token", "fresh-token")])
def test_credentials_amazon_finds_stale_are_renewed_once(client, runtime, upstream, aws):
    before = len(upstream.received)
    assert _post(client, _route(runtime) + CALL).status_code == 200
    assert len(upstream.received) == before + 2 and aws.calls == 2
    assert upstream.requests[-1]["headers"]["x-amz-security-token"] == "fresh-token"
