"""The guarded HTTP transport at S1 and S2 (P1-3 of ``notes/server-request-coverage-plan.md``): a request leaves for its
provider only from a minted decision that ``request_transport.validate_send`` authorized for that very exchange (its
arena and envelope, the rules in force, its attempt), as the bytes the decision froze; the request built from it is
frozen in turn (``FinalEnvelope``) and sent only as it was built. A refusal, no decision, another request's, one never
minted, one of an unknown kind, one written over, one whose arena changed, or a request changed after it was built
gives no build, or no send; the AWS retry of an expired token (S2) is the decision's second attempt, as checked as its
first. Refusals made before a request's content is read, and an AWS login that ended, are minted ``Block``s.

Nothing here changes what 0.10 sends, refuses, records or logs (P1-1's characterization, the replay of its golden)."""

from __future__ import annotations

import dataclasses
import gzip
import json
import struct
import zlib
from collections.abc import AsyncIterator, Callable
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import httpx
import pytest
import zstandard
from starlette.testclient import TestClient

from latet.bridge.apps import _UNDER_THE_SETTINGS
from latet.engine.vault import MaskingError
from latet.gateway import forward, outcome, request_policy, request_profiles, request_transport
from latet.gateway import request_audit as request_audit_module
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.aws import AwsError, profile_tag
from latet.gateway.cloud import BedrockUpstream
from latet.gateway.coverage import EnvelopeRefused, envelope_of, frame_envelope, inventory
from latet.gateway.mediate import Mediator
from latet.gateway.outcome import Attempted, Block, Forward, NotAttempted, NotSendable, Pass
from latet.gateway.request_transport import AuthorizedRequest, BuiltRequest, SendRefusal, validate_send
from latet.gateway.routes import Upstream
from latet.gateway.sigv4 import AwsCredentials
from latet.protocols import codec

from ..request_recorder import RecordingClient, RecordingTransport
from .test_request_outcomes import (
    CARD,
    CHAT,
    EMAIL,
    HEADERS,
    OPENAI,
    _chat,
    _envelope,
    _mediator,
    _prepared,
    _rule_pass,
    _strict,
)

TOKEN = "rc-bridge-token-p13"  # the computer's bridge token, which makes the profile route's key
REGION = "us-east-1"
# A Bedrock model named by its ARN, its colons and slash escaped as Claude Code sends them; and a raw query.
MODEL = "arn%3Aaws%3Abedrock%3Aus-east-1%3A123456789012%3Ainference-profile%2Fus.anthropic.claude-opus-5-5"
QUERY = ""  # accepted fixed Bedrock invoke profile has no query members
STALE, FRESH = "rc-dummy-stale-token", "rc-dummy-fresh-token"
LOGIN_ENDED = "your AWS login ended: run `aws sso login --profile work`"
# A Bedrock call is checked, never rewritten (masking it would refuse it): words with no value in them.
HELLO = "hello there, from the AWS route"
AMAZON = f"bedrock-runtime.{REGION}.amazonaws.com"


def _invoke(text: str) -> bytes:
    body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 5, "messages": [{"role": "user", "content": text}]}
    return json.dumps(body).encode()


def _route(key: str | None = None) -> str:
    return f"/bedrock/{REGION}/profile/work/{key or profile_tag(TOKEN, 'work')}/model/{MODEL}/invoke"


class _Answer(httpx.AsyncByteStream):
    """An answer's body, which knows whether the gateway closed it."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


class _Provider:
    """Answers a chat request, or a Bedrock call, with the first text it was sent; a Bedrock call signed with the stale
    token, with Amazon's ExpiredToken. ``seen`` runs as each request is answered."""

    def __init__(self, seen: Callable[[httpx.Request], None] | None = None) -> None:
        self.answers: list[_Answer] = []
        self.seen = seen

    def _answer(self, status: int, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
        stream = _Answer(json.dumps(body).encode())
        self.answers.append(stream)
        return httpx.Response(status, headers={"content-type": "application/json", **headers}, stream=stream)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.seen is not None:
            self.seen(request)
        if request.headers.get("x-amz-security-token") == STALE:
            stale = {"message": "The security token included in the request is expired"}
            return self._answer(403, {"x-amzn-ErrorType": "ExpiredTokenException:http://internal"}, stale)
        sent = json.loads(request.content)
        content = sent["messages"][0]["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        if "anthropic_version" in sent:
            message = {
                "id": "msg_rc", "type": "message", "role": "assistant", "model": "m", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 1, "output_tokens": 1},
            }  # fmt: skip
            return self._answer(200, {}, message)
        return self._answer(200, {}, {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]})


class _Aws:
    """A profile's credentials as Latet reads them: dummy ones, stale at the first read and fresh after. ``on_read``
    runs as the n-th read is made (between the decision and S1, or between S1 and S2); ``fails``: the read that finds
    the login ended."""

    def __init__(self, on_read: Callable[[int], None] | None = None, fails: int | None = None) -> None:
        self.reads = 0
        self.invalidated: list[str] = []
        self.on_read = on_read
        self.fails = fails

    async def profile(self, name: str) -> AwsCredentials:
        self.reads += 1
        if self.on_read is not None:
            self.on_read(self.reads)
        if self.fails == self.reads:
            raise AwsError("sso-expired", LOGIN_ENDED)
        return AwsCredentials("AKIARCDUMMY", "rc-dummy-secret", STALE if self.reads == 1 else FRESH)

    def invalidate(self, name: str) -> None:
        self.invalidated.append(name)

    def session(self, name: str) -> AwsCredentials:
        raise AwsError("no-session", "no such session")


class _Recorder:
    """A recorder that keeps what it is handed (``finished``)."""

    enabled = True

    def __init__(self) -> None:
        self.finished: list[Any] = []

    def finish(self, outcome_: Any) -> None:
        self.finished.append(outcome_)

    # A Recorder's two steps for an answered request (``ResponseFinalizer``): its request frozen before the response's
    # teardown, then handed with the screened response.
    def freeze_request(self, outcome_: Any) -> Any:
        self.finished.append(outcome_)
        return outcome_

    def submit_safe(self, request: Any, capture: Any, headers: Any, error: Any) -> None:
        return None

    async def prune_when_due(self) -> None:
        return None

    def drain(self, timeout: float = 0) -> None:
        return None


@dataclasses.dataclass
class _Gateway:
    client: TestClient
    transport: RecordingTransport
    provider: _Provider
    made: list[RecordingClient]
    config: GatewayConfig | None = None  # its routes (``config.routes``), applied again as a settings change does

    def counts(self) -> tuple[int, int]:
        return sum(c.builds for c in self.made), sum(c.sends for c in self.made)


def _gateway(mediator: Mediator | None, provider: _Provider | None = None, **config: Any) -> _Gateway:
    provider = provider or _Provider()
    transport = RecordingTransport(provider)
    made: list[RecordingClient] = []

    def client() -> RecordingClient:
        made.append(RecordingClient(transport))
        return made[-1]

    upstreams = config.pop("upstreams", [OPENAI])
    settings = GatewayConfig(port=5283, upstreams=upstreams, mediator=mediator, client=client, **config)
    app = create_app(settings)
    return _Gateway(TestClient(app, base_url="http://127.0.0.1:5283"), transport, provider, made, settings)


def _bedrock(mediator: Mediator | None, aws: _Aws, provider: _Provider | None = None, **config: Any) -> _Gateway:
    upstreams = [BedrockUpstream(envelope_expected=frozenset({("model", unquote(MODEL))}))]
    return _gateway(mediator, provider, upstreams=upstreams, aws=cast(Any, aws), bridge_token=TOKEN, **config)


@pytest.fixture
def turns(monkeypatch) -> list[Any]:
    """Each request's ``_Turn``, as ``Forwarder.handle`` made it: its decision and what happened at its send."""
    made: list[Any] = []
    real = forward._Turn

    def kept() -> Any:
        made.append(real())
        return made[-1]

    monkeypatch.setattr(forward, "_Turn", kept)
    return made


class _Sneaky(Forward):
    """A kind of decision ``outcome`` does not know."""

    __slots__ = ()


def _another() -> Forward:
    body = _chat("another request")
    decision = _strict(_prepared(_mediator(), body), _envelope(body))
    assert type(decision) is Forward
    return decision


def _block(turn: Any) -> None:
    turn.decision = outcome.refuse("http", "bedrock", "refused", "stopped", 400)


def _none(turn: Any) -> None:
    turn.decision = None


def _foreign(turn: Any) -> None:
    turn.decision = _another()


def _copied(turn: Any) -> None:
    turn.decision = dataclasses.replace(turn.decision)


def _unknown(turn: Any) -> None:
    turn.decision = _Sneaky(*(getattr(turn.decision, f.name) for f in dataclasses.fields(Forward)))


def _written_over(turn: Any) -> None:
    object.__setattr__(turn.decision, "payload", _invoke(f"write to {EMAIL}"))


def _stale(turn: Any) -> None:
    turn.prepared.body = turn.prepared.body + b" "


def _no_arena(turn: Any) -> None:
    """Injected at validate_send below, retaining turn.prepared for exact owner cleanup."""


# What S1 or S2 is given in place of the exchange's own decision, and the code it refuses it with.
DECISIONS: dict[str, tuple[Callable[[Any], None], str]] = {
    "a block": (_block, "block"),
    "no decision": (_none, "not-a-decision"),
    "another request's": (_foreign, "foreign"),
    "a copy, never minted": (_copied, "unminted"),
    "an unknown kind": (_unknown, "unknown"),
    "written over": (_written_over, "tampered"),
    "its arena changed after it": (_stale, "stale"),
    "decided without its arena": (_no_arena, "foreign"),
}


def _body(request: httpx.Request) -> None:
    request.stream = httpx.ByteStream(b'{"messages": []}')  # what httpx sends; its read content left as it was


def _target(request: httpx.Request) -> None:
    request.url = request.url.copy_with(raw_path=b"/model/another/invoke")


def _header(request: httpx.Request) -> None:
    request.headers["x-added"] = "after the freeze"


def _tls_name(request: httpx.Request) -> None:
    request.extensions = {**request.extensions, "sni_hostname": "another.example.test"}


# A request changed after it was built and frozen: it is not sent.
BUILT: dict[str, Callable[[httpx.Request], None]] = {
    "its body after the freeze": _body,
    "its target after the freeze": _target,
    "its header list after the freeze": _header,
    "its TLS name after the freeze": _tls_name,
}


@pytest.mark.parametrize("attempt", [1, 2], ids=["S1", "S2"])
@pytest.mark.parametrize("change", [*DECISIONS, *BUILT])
def test_s1_and_s2_send_only_a_minted_validated_decision(attempt, change, turns, monkeypatch):
    def on_read(read: int) -> None:
        if read == attempt and change in DECISIONS:
            DECISIONS[change][0](turns[-1])

    real_validate = request_transport.validate_send

    def validating(decision: Any, arena: Any, envelope: Any, policy: Any, number: int, **kwargs: Any):
        consumer = None if change == "decided without its arena" and number == attempt else arena
        return real_validate(decision, consumer, envelope, policy, number, **kwargs)

    monkeypatch.setattr(request_transport, "validate_send", validating)

    built: list[BuiltRequest] = []
    real_build = request_transport.build

    def building(*args: Any, **kwargs: Any) -> Any:
        made = real_build(*args, **kwargs)
        assert isinstance(made, BuiltRequest)
        built.append(made)
        if len(built) == attempt and change in BUILT:
            BUILT[change](made.request)
        return made

    monkeypatch.setattr(request_transport, "build", building)
    aws = _Aws(on_read)
    gateway = _bedrock(_mediator(), aws)
    with gateway.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert answered.status_code == 500
    words = answered.json()["message"]
    if attempt == 1:
        assert "Nothing was sent" in words
    else:
        assert "No further attempt was sent" in words and "may have occurred" in words
        assert "Nothing was sent" not in words
    code = DECISIONS[change][1] if change in DECISIONS else "changed"
    builds = attempt - 1 + (change in BUILT)
    assert gateway.counts() == (builds, attempt - 1)  # no build for a decision refused, no send for a request changed
    assert len(gateway.transport.sent) == attempt - 1
    assert all(EMAIL.encode() not in sent.content for sent in gateway.transport.sent)
    turn = turns[-1]
    if attempt == 1:
        assert turn.execution == NotAttempted(code)
    else:  # the first attempt went, its stale answer closed; the second was not sent
        assert turn.execution == Attempted(1, 403) and gateway.provider.answers[0].closed
        assert aws.reads == 2


def test_a_mediated_request_decided_without_its_arena_sends_nothing(turns, monkeypatch):
    """The review's N-3: the transport takes the arena from the exchange, never from the decision: a mediated request
    whose decision was made as if it had no arena (its payload then the request as it came) is refused, nothing built."""

    def without_arena(arena: Any, envelope: Any, *args: Any, **kwargs: Any) -> Any:
        return outcome.decide(None, envelope)

    monkeypatch.setattr(forward, "finalize", without_arena)
    gateway = _gateway(_mediator())
    with gateway.client as client:
        answered = client.post(CHAT, content=_chat(f"write to {EMAIL}"), headers=HEADERS)
    assert answered.status_code == 400 and gateway.counts() == (0, 0) and gateway.transport.sent == []
    assert turns[-1].execution == NotAttempted("proof-incomplete") and turns[-1].prepared is not None


@pytest.mark.parametrize("change_arena", [False, True], ids=["request-and-record-copies", "stale-arena"])
def test_a_legacy_pass_delivers_exactly_its_frozen_bytes(turns, monkeypatch, change_arena):
    """Request/record copies cannot change frozen bytes; changing the actual arena refuses the send."""
    mediator, body = _rule_pass(f"write to {EMAIL}")
    requests: list[Any] = []
    records: list[Any] = []
    real_handle = forward.Forwarder._handle
    real_outcome = forward.Outcome

    async def handling(self: Any, request: Any, turn: Any) -> Any:
        requests.append(request)
        return await real_handle(self, request, turn)

    def recorded(*args: Any, **kwargs: Any) -> Any:
        records.append(real_outcome(*args, **kwargs))
        return records[-1]

    real_validate = request_transport.validate_send

    def validating(*args: Any, **kwargs: Any) -> Any:
        authorized = real_validate(*args, **kwargs)
        assert isinstance(authorized, AuthorizedRequest)
        if change_arena:
            turns[-1].prepared.body = body  # the original canary, written after authorization
        requests[-1]._body = b"{}"  # the request as it came, another body
        records[-1].prepared, records[-1].body_bytes = None, 0  # the record
        return authorized

    monkeypatch.setattr(forward.Forwarder, "_handle", handling)
    monkeypatch.setattr(forward, "Outcome", recorded)
    monkeypatch.setattr(request_transport, "validate_send", validating)
    gateway = _gateway(mediator, recorder=cast(Any, _Recorder()))
    with gateway.client as client:
        answered = client.post(CHAT, content=body, headers=HEADERS)
    if change_arena:
        assert answered.status_code == 500 and turns[-1].execution == NotAttempted("stale")
        assert gateway.counts() == (1, 0) and gateway.transport.sent == []
        return
    assert answered.status_code == 200
    decision = turns[-1].decision
    [sent] = gateway.transport.sent
    assert type(decision) is Pass and sent.content == decision.payload and EMAIL.encode() not in sent.content
    assert gateway.counts() == (1, 1) and turns[-1].execution == Attempted(1, 200)
    assert answered.json()["choices"][0]["message"]["content"] == f"write to {EMAIL}"


def test_aws_retry_reuses_the_decisions_payload_and_lease(turns, monkeypatch):
    """S2, Amazon's ExpiredToken: one retry with fresh dummy credentials, of the same decision (its second attempt,
    validated as its first), with the same payload, route (escaped slash and colons, raw query), Host and framing, a new
    signature; the stale answer closed; one arena and one hold handle across both attempts, the request prepared once
    (the lease is wired in P1-4). A Bedrock call is checked, never rewritten: its handle is None, and the engine is asked
    to hold or release nothing, before or between its attempts."""
    mediator = _mediator()
    holds: list[object] = []
    releases: list[object] = []
    prepares: list[object] = []
    at_send: list[tuple[object, object]] = []
    real_prepare = mediator.prepare

    def hold(session: Any) -> object:
        holds.append(object())
        return holds[-1]

    def release(session: Any, held: object) -> None:
        releases.append(held)

    def prepare(*args: Any, **kwargs: Any) -> Any:
        prepares.append(args)
        return real_prepare(*args, **kwargs)

    def seen(request: httpx.Request) -> None:
        at_send.append((turns[-1].prepared, turns[-1].prepared.hold))

    monkeypatch.setattr(mediator.engine, "hold", hold)
    monkeypatch.setattr(mediator.engine, "release", release)
    monkeypatch.setattr(mediator, "prepare", prepare)
    validated: list[tuple[Any, Any, int]] = []
    real_validate = request_transport.validate_send

    def validating(decision: Any, arena: Any, envelope: Any, policy: Any, attempt: int, **kwargs: Any) -> Any:
        validated.append((decision, arena, attempt))
        return real_validate(decision, arena, envelope, policy, attempt, **kwargs)

    monkeypatch.setattr(request_transport, "validate_send", validating)
    aws = _Aws()
    gateway = _bedrock(mediator, aws, _Provider(seen))
    with gateway.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert answered.status_code == 200 and answered.json()["content"][0]["text"] == HELLO
    turn = turns[-1]
    decision = turn.decision
    first, second = gateway.transport.sent
    assert type(decision) is Forward and first.content == second.content == decision.payload
    assert decision.payload == _invoke(HELLO) and turn.prepared.mode == "verify"
    assert first.method == second.method == "POST" and first.host == second.host == AMAZON
    assert first.raw_path == second.raw_path == f"/model/{MODEL}/invoke".encode()
    signed = (b"authorization", b"x-amz-date", b"x-amz-security-token")
    kept = [[(k.lower(), v) for k, v in sent.headers if k.lower() not in signed] for sent in (first, second)]
    assert kept[0] == kept[1]  # generated Host and framing as before, every other header too, in order
    framing = dict(kept[0])
    assert framing[b"host"] == AMAZON.encode() and framing[b"content-length"] == str(len(decision.payload)).encode()
    assert b"transfer-encoding" not in framing
    tokens = [dict((k.lower(), v) for k, v in sent.headers)[b"x-amz-security-token"] for sent in (first, second)]
    assert tokens == [STALE.encode(), FRESH.encode()]
    signatures = [dict((k.lower(), v) for k, v in sent.headers)[b"authorization"] for sent in (first, second)]
    assert signatures[0] != signatures[1]  # a new signature, for the fresh credentials
    assert gateway.provider.answers[0].closed  # the stale answer closed before the retry
    assert [(d is decision, a is turn.prepared, n) for d, a, n in validated] == [(True, True, 1), (True, True, 2)]
    assert aws.reads == 2 and aws.invalidated == ["work"] and gateway.counts() == (2, 2)
    assert len(prepares) == 1 and at_send == [(turn.prepared, None), (turn.prepared, None)]  # one arena, one handle
    assert holds == [] and releases == []  # nothing held or let go by the retry
    assert turn.execution == Attempted(2, 200)


_BEDROCK_TEAM = {
    "destination": "bedrock",
    "profile": "bedrock.invoke.1",
    "components": [{"kind": "header", "name": "x-team"}],
    "reason": "custom-header",
}


@pytest.mark.parametrize("when", ["egress-pin", "credential-read", "second-attempt"])
def test_an_exception_that_ends_after_its_decision_is_not_sent_under(turns, monkeypatch, when):
    """R-1: a request sent under a stored exception is judged again after each wait between its decision and what it
    sends (its egress pin, the read of its AWS credentials, AWS's second attempt), by the same read as at its
    decision: an exception that ended meanwhile is refused naming it, and nothing more is sent."""
    from datetime import UTC, datetime, timedelta

    from latet.config import RequestException

    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    clock = {"now": now}
    row = RequestException.model_validate(
        {
            **_BEDROCK_TEAM,
            "id": "x0123456789",
            "granted": now - timedelta(days=30) + timedelta(minutes=5),  # it ends five minutes after its decision
            "revision": request_profiles.REVISION,
        }
    )

    def later() -> None:
        clock["now"] = now + timedelta(hours=1)

    aws = _Aws(on_read=lambda n: later() if (when, n) in (("credential-read", 1), ("second-attempt", 2)) else None)
    if when == "egress-pin":
        routed = forward.Forwarder._route

        async def pinned(self: Any, *args: Any, **kwargs: Any) -> Any:
            found = await routed(self, *args, **kwargs)
            later()  # its address looked up and pinned meanwhile
            return found

        monkeypatch.setattr(forward.Forwarder, "_route", pinned)
    gateway = _bedrock(_mediator(), aws, request_exceptions=(row,), exception_clock=lambda: clock["now"])
    headers = {"content-type": "application/json", "x-team": "data-science"}
    with gateway.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers=headers)
    assert answered.status_code == 400 and "x0123456789" in answered.text and "expired" in answered.text
    assert len(gateway.transport.sent) == (1 if when == "second-attempt" else 0)
    if when == "second-attempt":  # its first attempt left under the exception: counted as it went, no second one
        assert type(turns[-1].decision) is Pass and turns[-1].execution == Attempted(1, 403)
        # G2 review I-2: the agent is told what reached Bedrock, as ``_not_sent`` tells it, never "Nothing was sent".
        assert "Nothing was sent" not in answered.text and "No further attempt was sent" in answered.text
        assert "may have occurred" in answered.text
    else:
        assert "Nothing was sent" in answered.text


def test_settings_unread_before_aws_second_attempt_say_the_first_may_have_arrived(turns):
    """G2 review I-2, RC-REC-3's 503 path: the settings a stored exception is judged by cannot be read before AWS's
    second attempt (``exception_fresh`` fails), after the first reached Bedrock. Refused, no second attempt, and the
    agent is told that no further attempt was sent and that the first may have arrived, never "Nothing was sent"."""
    from datetime import UTC, datetime, timedelta

    from latet.config import RequestException

    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    row = RequestException.model_validate(
        {
            **_BEDROCK_TEAM,
            "id": "x0123456789",
            "granted": now - timedelta(days=1),
            "revision": request_profiles.REVISION,
        }
    )
    aws = _Aws()

    async def fresh() -> tuple[datetime, bool]:
        if aws.reads == 2:  # its fresh credentials read: the call made before the second attempt
            raise OSError("the settings could not be read")
        return now, False

    gateway = _bedrock(_mediator(), aws, request_exceptions=(row,), exception_fresh=fresh)
    headers = {"content-type": "application/json", "x-team": "data-science"}
    with gateway.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers=headers)
    assert answered.status_code == 503 and "request exception state unavailable" in answered.text
    assert len(gateway.transport.sent) == 1 and aws.reads == 2
    assert type(turns[-1].decision) is Pass and turns[-1].execution == Attempted(1, 403)
    assert "Nothing was sent" not in answered.text and "No further attempt was sent" in answered.text
    assert "may have occurred" in answered.text


def test_a_route_gone_before_aws_second_attempt_says_the_first_may_have_arrived(turns):
    """Minors after 0.11, item 7 (G2 review I-2's 404 path): settings read again before AWS's second attempt, after the
    first reached Bedrock, remove its route (its destination deleted). Still 404, nothing more sent, but in Bedrock's
    shape and in Latet's words: no further attempt was sent and the first may have arrived, never "Nothing was sent",
    never the bare "no upstream for this path"."""
    from datetime import UTC, datetime, timedelta

    from latet.config import RequestException

    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    row = RequestException.model_validate(
        {
            **_BEDROCK_TEAM,
            "id": "x0123456789",
            "granted": now - timedelta(days=1),
            "revision": request_profiles.REVISION,
        }
    )
    aws = _Aws()
    holder: dict[str, _Gateway] = {}

    async def fresh() -> tuple[datetime, bool]:
        if aws.reads == 2:  # its fresh credentials read: the read before the second attempt
            config = holder["gateway"].config
            assert config is not None and config.routes is not None
            config.routes.use([])  # the destination gone meanwhile
            return now, True
        return now, False

    gateway = holder["gateway"] = _bedrock(_mediator(), aws, request_exceptions=(row,), exception_fresh=fresh)
    headers = {"content-type": "application/json", "x-team": "data-science"}
    with gateway.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers=headers)
    assert answered.status_code == 404 and len(gateway.transport.sent) == 1 and aws.reads == 2
    assert type(turns[-1].decision) is Pass and turns[-1].execution == Attempted(1, 403)
    assert "Nothing was sent" not in answered.text and "No further attempt was sent" in answered.text
    assert "may have occurred" in answered.text and answered.json()["message"].startswith("Latet: ")


# --- settings applied again while a request is prepared (minors after 0.11, item 2; G2 review M-2) ---------------------

# What a request is told when the settings it was prepared under changed before it left: never a coverage fault.
SETTINGS_MOVED = "the settings changed while this request was prepared"


def _applied_again(gateway: _Gateway, monkeypatch, swap: Callable[[Any], None]) -> list[int]:
    """While the first preparation of a request runs, its identity's settings applied again as ``apply_config`` applies
    them: the engine's rules move and the route table is swapped (``swap``, given it). Each preparation counted."""
    real = Mediator.prepare_http
    prepared: list[int] = []

    def prepare_http(self: Mediator, *args: Any, **kwargs: Any) -> Any:
        found = real(self, *args, **kwargs)
        prepared.append(1)
        if len(prepared) == 1:
            self.engine.rules_changed()
            assert gateway.config is not None and gateway.config.routes is not None
            swap(gateway.config.routes)
        return found

    monkeypatch.setattr(Mediator, "prepare_http", prepare_http)
    return prepared


def _openai_exception() -> Any:
    from datetime import UTC, datetime

    from latet.config import RequestException

    return RequestException.model_validate(
        {
            "destination": "openai",
            "profile": "openai.chat.1",
            "components": [{"kind": "header", "name": "x-team"}],
            "reason": "custom-header",
            "id": "x0123456789",
            "granted": datetime.now(UTC),
            "revision": request_profiles.REVISION,
        }
    )


def test_a_request_prepared_while_its_settings_are_applied_again_is_prepared_again_and_sent(monkeypatch):
    """Every settings change applied again (a save, a shared change, another instance's write on a server) makes a new
    ``Upstream`` object for each configured destination. A request prepared meanwhile, its route the same in all but
    the object, is prepared again under the rules now in force and sent; before, its route authority compared the
    route by identity, was never taken again, and the request was refused "could not account for every part"."""
    gateway = _gateway(_mediator())
    prepared = _applied_again(gateway, monkeypatch, lambda routes: routes.use([dataclasses.replace(OPENAI)]))
    with gateway.client as client:
        answered = client.post(CHAT, content=_chat("hello"), headers=HEADERS)
    assert answered.status_code == 200, answered.text
    assert len(gateway.transport.sent) == 1 and len(prepared) == 2


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(
            lambda routes: routes.use([dataclasses.replace(OPENAI, base_url="http://127.0.0.1:2")]), id="address"
        ),
        pytest.param(lambda routes: routes.use([OPENAI], exceptions=(_openai_exception(),)), id="exception"),
        pytest.param(
            lambda routes: routes.use([OPENAI], resources=(("openai", OPENAI.base_url, "model", "m2"),)), id="resource"
        ),
    ],
)
def test_a_request_whose_route_changed_while_it_was_prepared_is_still_refused(monkeypatch, caplog, turns, changed):
    """The negative controls: a route whose address, exceptions or request resources changed while a request was
    prepared is not the route it was prepared for. Nothing is sent, and the agent is told why: the settings changed.
    So are the history, the log and the audit (the fix round's Q-M1): the refusal's code is ``settings-changed``,
    never ``proof-incomplete``'s, whose words blame coverage."""
    import logging

    recorder = _Recorder()
    gateway = _gateway(_mediator(), recorder=recorder)
    prepared = _applied_again(gateway, monkeypatch, changed)
    with caplog.at_level(logging.WARNING, logger="latet.gateway.forward"), gateway.client as client:
        answered = client.post(CHAT, content=_chat("hello"), headers=HEADERS)
    assert answered.status_code == 400 and len(gateway.transport.sent) == 0 and len(prepared) == 1
    assert SETTINGS_MOVED in answered.text and "could not account" not in answered.text
    assert turns[-1].decision.code == "settings-changed"
    assert [record.block_code for record in recorder.finished] == ["settings-changed"]
    logged = [record.getMessage() for record in caplog.records if record.name == "latet.gateway.forward"]
    assert any(SETTINGS_MOVED in line for line in logged) and not any("could not account" in line for line in logged)
    assert request_audit_module.reason_code("settings-changed") not in (3, 31)  # its own, never coverage's or unknown


@pytest.mark.parametrize("rules", ["kept", "moved"])
def test_settings_read_again_while_its_exceptions_are_judged_never_blame_coverage(turns, rules):
    """G2 review M-2, RC-REC-3 on a server: the read made while a request's exceptions are judged finds PostgreSQL's
    counts ahead, and the settings are applied again (``spaces._reapply``). The same route and rules: it is sent. The
    rules moved after it was prepared and reviewed: refused, nothing sent, with words that say the settings changed and
    to send it again, never "could not account for every part of this request"."""
    from datetime import UTC, datetime, timedelta

    from latet.config import RequestException

    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    row = RequestException.model_validate(
        {
            **_BEDROCK_TEAM,
            "id": "x0123456789",
            "granted": now - timedelta(days=1),
            "revision": request_profiles.REVISION,
        }
    )
    calls: list[int] = []
    mediator = _mediator()
    holder: dict[str, _Gateway] = {}

    async def fresh() -> tuple[Any, bool]:
        calls.append(1)
        if len(calls) != 2:  # the second read is the one made while its exceptions are judged
            return now, False
        config = holder["gateway"].config
        assert config is not None and config.routes is not None
        config.routes.use(config.routes.upstreams, exceptions=(row,))  # new Upstream objects, the same content
        if rules == "moved":
            mediator.engine.rules_changed()
        return now, True

    gateway = holder["gateway"] = _bedrock(mediator, _Aws(), request_exceptions=(row,), exception_fresh=fresh)
    headers = {"content-type": "application/json", "x-team": "data-science"}
    with gateway.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers=headers)
    assert "could not account" not in answered.text
    if rules == "kept":
        assert answered.status_code == 200 and len(gateway.transport.sent) == 2  # AWS's expired token, then fresh
    else:
        assert answered.status_code == 400 and len(gateway.transport.sent) == 0
        assert SETTINGS_MOVED in answered.text and "send it again" in answered.text
        assert turns[-1].decision.code == "settings-changed"  # the fix round's Q-M1


def test_a_request_prepared_while_settings_are_half_applied_is_prepared_again_under_the_whole(
    tmp_path, monkeypatch, turns
):
    """Fix round S-M1 (item 2): ``Runtime.apply_config`` moves the rules version (``engine.use``), then sets the image,
    dump guard and review settings, then swaps the route table. A request prepared again in between (its authority
    taken again on the loop because the rules had moved) carried the new version and the old route, equal by value to
    the new one. Its body's proof was refused at its decision all the same (``live_changed`` renews the content epoch
    as the settings end), and its agent was told that Latet could not account for every part of it. The version now
    moves once more when everything is in place: the authority taken in the window is never valid, and the request is
    prepared again under the whole settings and sent. Since item 23 its authority is not even taken again in the
    window: the gateway waits for the apply to end first (``request_policy.settled``, which lets the apply go on here as it starts
    waiting), so the request is prepared twice, before the apply and after the whole of it, never in between."""
    import base64
    import threading

    from latet import runtime as runtime_module
    from latet.config import Config, Destination, IdentityConfig
    from latet.runtime import make_runtime

    from ..conftest import ROOT_KEY

    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    acme = Destination(name="acme", url="https://llm.acme.invalid")
    settings = Config(identity=IdentityConfig(enabled=False), ner="off", upstreams=[acme])
    stronger = settings.model_copy(update={"dump_guard": 3})
    runtime = make_runtime(settings, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    transport = RecordingTransport(_Provider())
    runtime.gateway.client = lambda: RecordingClient(transport)
    halfway, resume = threading.Event(), threading.Event()
    applying: list[threading.Thread] = []
    policy = runtime_module.image_policy

    def held_halfway(config: Any) -> Any:
        # Inside apply_config: the rules version moved and the masker is in use; the image, dump guard and review
        # settings and the route table are not applied yet.
        if applying and threading.current_thread() is applying[0]:
            halfway.set()
            assert resume.wait(10)
        return policy(config)

    prepare = Mediator.prepare_http
    # Each preparation and the dump guard it read, in the order they ran: kept, so never told apart by an address a
    # freed one could pass on (the re-review's nit).
    preparations: list[tuple[Any, Any]] = []

    def prepare_http(self: Mediator, *args: Any, **kwargs: Any) -> Any:
        guard = self.dump_guard  # what this preparation read of the settings
        prepared = prepare(self, *args, **kwargs)
        preparations.append((prepared, guard))
        if len(preparations) == 1:  # the settings applied again meanwhile, held after their version moved
            applying.append(threading.Thread(target=runtime.apply_config, args=(stronger,), kwargs={"save": False}))
            applying[0].start()
            assert halfway.wait(10)
        return prepared

    settled = request_policy.settled
    waits: list[int] = []

    async def waited(engine: Any) -> None:
        waits.append(len(preparations))
        resume.set()  # the gateway waits for the apply: now the rest of the settings lands
        await settled(engine)

    monkeypatch.setattr(runtime_module, "image_policy", held_halfway)
    monkeypatch.setattr(Mediator, "prepare_http", prepare_http)
    monkeypatch.setattr(request_policy, "settled", waited)
    body = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}).encode()
    try:
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            answered = client.post("/acme/v1/chat/completions", content=body, headers=HEADERS)
    finally:
        resume.set()
        for thread in applying:
            thread.join(10)
        runtime.close()
    guards = [guard for _, guard in preparations]
    assert runtime.mediator.dump_guard == 3 and len(preparations) == 2 and waits == [1], guards  # before, after
    assert answered.status_code == 200, answered.text
    # Sent as last prepared, under the whole settings.
    assert len(transport.sent) == 1 and turns[-1].prepared is preparations[-1][0] and guards[-1] == 3, guards


def test_a_request_prepared_as_the_settings_end_is_prepared_again_once_they_have(tmp_path, monkeypatch, turns):
    """The fix round's re-review (security, its wording point): ``Runtime.apply_config`` moved the rules version the
    second time before its last steps, ``live_changed`` among them, which renews the content epoch. A request with a
    body prepared in between carried the final version and the content epoch of before: its route authority stayed
    valid and its body's proof did not, so it was refused with the coverage words, nothing sent. The second move now
    comes after ``live_changed``: that request's authority is no longer valid once the apply has ended, and it is
    prepared again and sent."""
    import base64
    import threading

    from latet.config import Config, Destination, IdentityConfig
    from latet.runtime import make_runtime

    from ..conftest import ROOT_KEY

    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    acme = Destination(name="acme", url="https://llm.acme.invalid")
    settings = Config(identity=IdentityConfig(enabled=False), ner="off", upstreams=[acme])
    stronger = settings.model_copy(update={"dump_guard": 3})
    runtime = make_runtime(settings, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    transport = RecordingTransport(_Provider())
    runtime.gateway.client = lambda: RecordingClient(transport)
    ending, resume = threading.Event(), threading.Event()
    applying: list[threading.Thread] = []
    warm_up = runtime.warm_up

    def held_as_they_end() -> None:
        # Inside apply_config, among its last steps: every part of the settings is in place, and ``live_changed`` has
        # not renewed the content epoch yet.
        if applying and threading.current_thread() is applying[0]:
            ending.set()
            assert resume.wait(10)
        warm_up()

    prepare = Mediator.prepare_http
    preparations: list[tuple[Any, Any]] = []  # each preparation and the dump guard it read, in the order they ran

    def prepare_http(self: Mediator, *args: Any, **kwargs: Any) -> Any:
        prepared = prepare(self, *args, **kwargs)
        preparations.append((prepared, self.dump_guard))
        if len(preparations) == 1:  # its authority taken and prepared in that window: now the apply ends
            resume.set()
            applying[0].join(10)
        return prepared

    monkeypatch.setattr(runtime, "warm_up", held_as_they_end)
    monkeypatch.setattr(Mediator, "prepare_http", prepare_http)
    body = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}).encode()
    try:
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            # The settings applied again first, held as they end; the request comes in that window.
            applying.append(threading.Thread(target=runtime.apply_config, args=(stronger,), kwargs={"save": False}))
            applying[0].start()
            assert ending.wait(10)
            answered = client.post("/acme/v1/chat/completions", content=body, headers=HEADERS)
    finally:
        resume.set()
        for thread in applying:
            thread.join(10)
        runtime.close()
    assert "could not account" not in answered.text
    assert answered.status_code == 200 and len(preparations) == 2, (answered.text, len(preparations))
    assert len(transport.sent) == 1 and turns[-1].prepared is preparations[-1][0] and preparations[-1][1] == 3


def _row(record: Any) -> dict[str, Any]:
    """What a request's history row is made of, its times left out."""
    kept = ("upstream", "path", "transport", "status", "forwarded", "request_headers", "body_bytes", "error", "host")
    kept += ("signed", "history_code", "block_code", "passes", "profile", "removed_headers")
    return {name: getattr(record, name) for name in kept}


@pytest.mark.parametrize("decided", ["forward", "pass"])
@pytest.mark.parametrize("moved", ["rules", "route"])
def test_a_request_whose_settings_change_between_its_decision_and_its_send_is_refused_as_settings_changed(
    monkeypatch, caplog, turns, moved, decided
):
    """After 0.11, item 3 (the review of 0.11.1's merge into enterprise): the rules move after a request's final
    decision and before its send (its address looked up meanwhile), or its route is swapped for one of other content.
    The transport refuses it as stale, nothing sent. It is told, recorded and audited as a request whose settings
    changed while it was prepared (400, send it again, a refusal in the history), where before it was answered 500
    "could not send this request as it was decided" and recorded as an exchange, with no block code. Its history row is
    the row of the same request refused at its decision because its settings changed (the fix round's M1): no error,
    no passes of the decision it was not sent under (``decided``: a ``Forward``, or a ``Pass`` by a built-in rule), no
    request headers, its size as it came."""
    import logging

    recorder = _Recorder()
    mediator, body = _rule_pass("hello") if decided == "pass" else (_mediator(), _chat("hello"))
    gateway = _gateway(mediator, recorder=recorder)
    routed = forward.Forwarder._route

    async def moving(self: Any, *args: Any, **kwargs: Any) -> Any:
        found = await routed(self, *args, **kwargs)
        if moved == "rules":
            self.mediator.engine.rules_changed()
        else:
            assert gateway.config is not None and gateway.config.routes is not None
            gateway.config.routes.use([dataclasses.replace(OPENAI, base_url="http://127.0.0.1:2")])
        return found

    monkeypatch.setattr(forward.Forwarder, "_route", moving)
    with caplog.at_level(logging.WARNING, logger="latet.gateway.forward"), gateway.client as client:
        answered = client.post(CHAT, content=body, headers=HEADERS)
    monkeypatch.setattr(forward.Forwarder, "_route", routed)
    turn = turns[-1]
    assert type(turn.decision) is Block and turn.prepared is not None
    # The same request, its route changed while it was prepared: refused at its decision because its settings changed.
    at_decision = _Recorder()
    twin = _gateway(_rule_pass("hello")[0] if decided == "pass" else _mediator(), recorder=at_decision)
    _applied_again(
        twin, monkeypatch, lambda routes: routes.use([dataclasses.replace(OPENAI, base_url="http://127.0.0.1:2")])
    )
    with twin.client as client:
        assert client.post(CHAT, content=body, headers=HEADERS).status_code == 400
    assert turns[-1].decision.code == "settings-changed"
    assert answered.status_code == 400, answered.text
    words = answered.json()["error"]["message"]
    assert SETTINGS_MOVED in words and "send it again" in words and "as it was decided" not in words
    assert "Nothing was sent" in words
    assert gateway.transport.sent == [] and gateway.counts() == (0, 0)
    assert turn.decision.code == "settings-changed" and turn.execution == NotAttempted("settings-changed")
    assert turn.refusal == request_audit_module.reason_code("settings-changed") == 19
    [record] = recorder.finished
    assert record.block_code == "settings-changed" and record.history_code == 28 and record.forwarded is None
    assert record.error is None and record.passes == () and record.request_headers == []
    [refused] = at_decision.finished
    assert _row(record) == _row(refused)
    logged = [record.getMessage() for record in caplog.records if record.name == "latet.gateway.forward"]
    assert "blocked HTTP request: settings-changed" in logged and not any("not sent" in line for line in logged)


def test_a_request_whose_settings_change_before_aws_second_attempt_is_refused_as_settings_changed(turns):
    """The Bedrock twin of the test above: the rules move as AWS's fresh credentials are read, after the first attempt
    reached Bedrock and was answered ExpiredToken. No second attempt; the refusal is settings-changed's, and its agent
    is told that no further attempt was sent and that the first may have arrived, never "Nothing was sent". The first
    attempt's record stands: it left (``forwarded``), so it carries no block code of a request never sent."""
    recorder = _Recorder()
    mediator = _mediator()
    aws = _Aws(on_read=lambda read: mediator.engine.rules_changed() if read == 2 else None)
    gateway = _bedrock(mediator, aws, recorder=recorder)
    with gateway.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert answered.status_code == 400, answered.text
    words = answered.json()["message"]
    assert SETTINGS_MOVED in words and "send it again" in words and "as it was decided" not in words
    assert "No further attempt was sent" in words and "may have occurred" in words and "Nothing was sent" not in words
    assert len(gateway.transport.sent) == 1 and aws.reads == 2 and gateway.provider.answers[0].closed
    turn = turns[-1]
    assert turn.decision.code == "settings-changed" and turn.execution == Attempted(1, 403)
    assert turn.refusal == request_audit_module.reason_code("settings-changed")
    [record] = recorder.finished
    assert record.history_code == 28 and record.block_code is None and record.forwarded is not None


# --- a request taken and sent while a save is applied (minors after 0.11, item 23) ------------------------------------

ACME_CHAT = "/acme/v1/chat/completions"
ACME_BODY = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}).encode()


def _a_runtime(tmp_path, monkeypatch) -> tuple[Any, RecordingTransport, Any]:
    """A computer's runtime whose requests reach a recording provider, and stronger settings to apply while one of them
    is handled (its dump guard 3)."""
    import base64

    from latet.config import Config, Destination, IdentityConfig
    from latet.runtime import make_runtime

    from ..conftest import ROOT_KEY

    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    acme = Destination(name="acme", url="https://llm.acme.invalid")
    settings = Config(identity=IdentityConfig(enabled=False), ner="off", upstreams=[acme])
    runtime = make_runtime(settings, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    transport = RecordingTransport(_Provider())
    runtime.gateway.client = lambda: RecordingClient(transport)
    return runtime, transport, settings.model_copy(update={"dump_guard": 3})


class _HeldApply:
    """A save applied in a thread of its own, as the dashboard's runs in a worker (``Runtime.apply_config``), held
    inside it until ``resume`` is set: at ``image_policy`` (``where="first-move"``: after ``engine.use``, the rules'
    first move, the masker in use, the image, dump guard and review settings and the route table not yet), or at
    ``projects.use`` (``"first-change"``: its first change, the rules not moved yet). What it raised, if it did, is
    kept in ``raised``."""

    def __init__(self, runtime: Any, config: Any, monkeypatch: Any, where: str = "first-move") -> None:
        import threading

        from latet import runtime as runtime_module

        self.halfway, self.resume = threading.Event(), threading.Event()
        self.raised: list[BaseException] = []

        def applying() -> None:
            try:
                runtime.apply_config(config, save=False)
            except Exception as exc:
                self.raised.append(exc)

        self.thread = threading.Thread(target=applying)
        self.started = False
        if where == "first-move":
            policy = runtime_module.image_policy

            def at_image_policy(config: Any) -> Any:
                self._hold()
                return policy(config)

            monkeypatch.setattr(runtime_module, "image_policy", at_image_policy)
        else:
            use = runtime.projects.use

            def at_projects(*args: Any, **kwargs: Any) -> Any:
                self._hold()
                return use(*args, **kwargs)

            monkeypatch.setattr(runtime.projects, "use", at_projects)

    def _hold(self) -> None:
        import threading

        if threading.current_thread() is self.thread:
            self.halfway.set()
            assert self.resume.wait(10)

    def start(self) -> None:
        """The apply started, and under way: held where it was asked to be."""
        self.started = True
        self.thread.start()
        assert self.halfway.wait(10)

    def end(self) -> None:
        self.resume.set()
        if self.started:
            self.thread.join(10)


def _preparations(monkeypatch) -> list[tuple[Any, Any, int]]:
    """Each preparation of a request with the dump guard and the rules version it was made under, in the order they ran
    (kept, so never told apart by an address a freed one could pass on)."""
    prepare = Mediator.prepare_http
    made: list[tuple[Any, Any, int]] = []

    def prepare_http(self: Mediator, *args: Any, **kwargs: Any) -> Any:
        guard, version = self.dump_guard, self.engine.rules_version
        made.append((prepare(self, *args, **kwargs), guard, version))
        return made[-1][0]

    monkeypatch.setattr(Mediator, "prepare_http", prepare_http)
    return made


def test_a_request_taken_and_sent_while_a_save_is_applied_never_leaves_with_it_half_applied(
    tmp_path, monkeypatch, turns
):
    """Item 23 (the security re-review of 0.11.1's fix round, R1): a request whose authority was taken and which was
    sent entirely while ``Runtime.apply_config`` ran, after its first rules move and before its last, passed its send
    check: its authority carried the first move's version, and it left with the new masker and the old dump guard,
    image, review settings and route. An apply under way now marks the engine (``Engine.applying``), and no authority
    is valid while it does: the request, prepared once in the window, waits for the apply to end (``request_policy.settled``), is
    prepared again under the whole settings and sent as last prepared; nothing is sent before."""
    runtime, transport, stronger = _a_runtime(tmp_path, monkeypatch)
    held = _HeldApply(runtime, stronger, monkeypatch)
    preparations = _preparations(monkeypatch)
    sent_at_wait: list[int] = []
    settled = request_policy.settled

    async def waited(engine: Any) -> None:
        sent_at_wait.append(len(transport.sent))
        held.resume.set()  # the apply goes on as the gateway starts waiting for it
        await settled(engine)

    monkeypatch.setattr(request_policy, "settled", waited)
    try:
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            held.start()
            answered = client.post(ACME_CHAT, content=ACME_BODY, headers=HEADERS)
    finally:
        held.end()
        runtime.close()
    final = runtime.built.engine.rules_version
    made = [(guard, version) for _, guard, version in preparations]
    assert len(preparations) == 2 and sent_at_wait == [0], (made, final, sent_at_wait, len(transport.sent))
    assert answered.status_code == 200, answered.text
    # The second after the apply's last move, under the whole settings; the first in the window, under part of them.
    assert made[1] == (3, final) and made[0][0] != 3 and made[0][1] < final, made
    last = preparations[-1][0]
    assert turns[-1].prepared is last and last.route_authority.rules == final and last.route_authority.valid()
    assert len(transport.sent) == 1


def test_a_request_caught_by_an_apply_that_outlasts_its_wait_is_refused_as_settings_changed(
    tmp_path, monkeypatch, turns
):
    """The same, with the apply held until the request is answered, past the gateway's wait for it
    (``request_policy.SETTLE_SECONDS``): the request is never sent with the settings half applied. It is refused because its settings
    changed while it was prepared (400, send it again), never with the coverage words; and at once once the wait is
    over (the fix round's M3): prepared once, in the window, never again under the mark, which no authority then taken
    could pass, nor read by a reviewer. Before, it was prepared twice more and waited twice, about twice the bound."""
    import time

    runtime, transport, stronger = _a_runtime(tmp_path, monkeypatch)
    held = _HeldApply(runtime, stronger, monkeypatch)
    preparations = _preparations(monkeypatch)
    asked: list[int] = []
    live = Mediator.live_prepared

    async def live_prepared(self: Mediator, *args: Any, **kwargs: Any) -> Any:
        asked.append(1)
        return await live(self, *args, **kwargs)

    monkeypatch.setattr(Mediator, "live_prepared", live_prepared)
    try:
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            held.start()
            began = time.monotonic()
            answered = client.post(ACME_CHAT, content=ACME_BODY, headers=HEADERS)
            took = time.monotonic() - began
            assert held.thread.is_alive()  # still held as it was answered
    finally:
        held.end()
        runtime.close()
    assert len(preparations) == 1 and asked == [], (len(preparations), asked, took)
    assert request_policy.SETTLE_SECONDS <= took < 2 * request_policy.SETTLE_SECONDS, took  # one wait, its bound
    assert answered.status_code == 400, answered.text
    assert SETTINGS_MOVED in answered.text and "send it again" in answered.text
    assert "could not account" not in answered.text
    assert transport.sent == [] and turns[-1].decision.code == "settings-changed"


@pytest.mark.parametrize("where", ["first-change", "first-move"])
def test_a_request_decided_before_a_save_and_sent_while_it_is_applied_is_refused_as_settings_changed(
    tmp_path, monkeypatch, turns, where
):
    """A request decided before a save, and sent while the save is applied (its address looked up meanwhile,
    ``Forwarder._route``): its route authority is not valid while the apply runs, held at its first change (the rules
    not moved yet, where before it was sent) or after the rules' first move. The transport refuses it as stale, and it
    is told that its settings changed (400, Task 1's mapping), nothing sent."""
    runtime, transport, stronger = _a_runtime(tmp_path, monkeypatch)
    held = _HeldApply(runtime, stronger, monkeypatch, where)
    routed = forward.Forwarder._route

    async def routing(self: Any, *args: Any, **kwargs: Any) -> Any:
        found = await routed(self, *args, **kwargs)
        held.start()  # its decision made: the save starts, and is held under way
        return found

    monkeypatch.setattr(forward.Forwarder, "_route", routing)
    try:
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            answered = client.post(ACME_CHAT, content=ACME_BODY, headers=HEADERS)
    finally:
        held.end()
        runtime.close()
    assert answered.status_code == 400, answered.text
    assert SETTINGS_MOVED in answered.text and "send it again" in answered.text
    assert transport.sent == [] and turns[-1].decision.code == "settings-changed"
    assert turns[-1].execution == NotAttempted("settings-changed")


@pytest.mark.parametrize("cause", ["launch", "arena"])
def test_a_stale_refusal_for_another_reason_while_a_save_is_applied_keeps_its_500(tmp_path, monkeypatch, turns, cause):
    """Item 23's fix round, M2: only the stale refusal its route authority makes (its settings moved since its
    decision) is told that the settings changed. One for another reason keeps its 500 and its code, even while a save
    is applied: its launch no longer current, found by the transport's awaited check while the save starts
    (``launch``), or its arena changed after its decision while a save is under way (``arena``, the Latet fault the
    500 is kept for). Before, either was answered settings-changed whenever the route authority was invalid as it was
    refused."""
    runtime, transport, stronger = _a_runtime(tmp_path, monkeypatch)
    held = _HeldApply(runtime, stronger, monkeypatch)

    async def ended() -> bool:
        held.start()  # the save starts while its launch is looked at, as it is sent
        return False  # and its launch is no longer current

    if cause == "launch":  # a launch Latet protects, as `latet claude` starts it: looked at again where it is sent
        prepare = Mediator.prepare_http

        def protected(self: Mediator, *args: Any, **kwargs: Any) -> Any:
            prepared = prepare(self, *args, **kwargs)
            prepared.protected_current = ended
            return prepared

        monkeypatch.setattr(Mediator, "prepare_http", protected)
    else:
        routed = forward.Forwarder._route

        async def routing(self: Any, *args: Any, **kwargs: Any) -> Any:
            found = await routed(self, *args, **kwargs)
            held.start()
            turns[-1].prepared.body = turns[-1].prepared.body + b" "  # its arena changed after its decision
            return found

        monkeypatch.setattr(forward.Forwarder, "_route", routing)
    try:
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            answered = client.post(ACME_CHAT, content=ACME_BODY, headers=HEADERS)
    finally:
        held.end()
        runtime.close()
    assert held.started  # refused while the save was under way
    assert answered.status_code == 500, answered.text
    assert "could not send this request as it was decided (stale)" in answered.text
    assert SETTINGS_MOVED not in answered.text
    assert transport.sent == [] and turns[-1].execution == NotAttempted("stale")


def _a_server_space(tmp_path) -> tuple[Any, Any]:
    """A server's space, a person's settings over shared ones, and its settings' store: kept in memory, it does not
    answer once its ``refuse`` is set."""
    from dataclasses import replace

    from latet.config import Config, Destination
    from latet.runtime import make_runtime
    from latet.server.shared import OwnSettings
    from latet.state import IdentityRef, local_backends

    from ..conftest import ROOT_KEY

    shared = Config(upstreams=[Destination(name="llm", url="https://llm.acme.com")])

    class Refusing(OwnSettings):
        """An identity's settings over ``shared``, kept in memory, whose store does not answer once ``refuse`` is set."""

        def __init__(self) -> None:
            super().__init__(lambda: shared)
            self.own: dict[str, Any] = {}
            self.refuse = False

        def _own(self) -> dict[str, Any]:
            return self.own

        def _keep_own(self, own: dict[str, Any]) -> None:
            if self.refuse:
                raise OSError("the settings could not be kept (PoolTimeout)")
            self.own = own

        def reviewed(self) -> Any:
            return None

        def save_reviewed(self, config: Any) -> None:
            return None

    settings = Refusing()
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    emily = IdentityRef("u_emilycarte", "Emily Carter", "person")
    runtime = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=emily)
    return runtime, settings


def _unrestorable_while(monkeypatch, runtime: Any, unrestorable: list[bool]) -> None:
    """What ran before raises as it is put back (``Runtime._restore``) while ``unrestorable[0]`` is set; then it is put
    back as it is."""
    restore = runtime._restore

    def restoring(was: Any) -> None:
        if unrestorable[0]:
            raise RuntimeError("what ran before could not be put back")
        restore(was)

    monkeypatch.setattr(runtime, "_restore", restoring)


def _said(caplog: Any) -> list[tuple[str, str]]:
    """What the runtime said in the log, by level and words, in order."""
    return [(record.levelname, record.getMessage()) for record in caplog.records if record.name == "latet.runtime"]


# What a put-back that raised says, by the error's type (``Runtime._put_back``).
_PUT_BACK_FAILED = ("WARNING", "the settings that ran before could not be put back: RuntimeError")


def test_a_save_applied_again_inside_a_refused_one_is_counted_by_the_mark(tmp_path, monkeypatch):
    """Item 23's count: a server's space whose save its store refuses applies what ran before inside that save
    (``Runtime._put_back``, under the same lock): the very objects that ran before its first change (fix round R1),
    never applied again. The mark is held while they are put back, and every mark is lowered: it is 0 after, and the
    rules moved twice, the apply's first move and its last."""
    from latet import runtime as runtime_module

    runtime, settings = _a_server_space(tmp_path)
    engine = runtime.built.engine
    marks: list[int] = []
    policy = runtime_module.image_policy

    def marked(config: Any) -> Any:
        marks.append(engine.applying)
        return policy(config)

    monkeypatch.setattr(runtime_module, "image_policy", marked)
    try:
        version, before = engine.rules_version, runtime.config
        settings.refuse = True
        with pytest.raises(OSError, match="PoolTimeout"):
            runtime.apply_config(runtime.config.model_copy(update={"dump_guard": 3}))
        assert runtime.config == before  # what ran before runs again
        assert marks == [1] and engine.applying == 0 and not engine.stranded, marks  # no apply of what ran before
        assert engine.rules_version == version + 2  # the apply's first move and its last
    finally:
        runtime.close()


def test_a_stranded_space_whose_whole_save_its_store_refuses_says_nothing_new(tmp_path, monkeypatch, caplog):
    """The whole-branch review of 0.12, Minor 2: a server's space is stranded (a save its store refused, and what ran
    before raised as it was put back). Its next save had its settings whole and was refused by its store too: it said
    that requests were sent again, then that the settings could not be put back, again. Neither was so: no request was
    sent, and what ran before, the mix, was put back. The space is stranded still, its mark held, and says nothing new;
    a save its store then keeps says once that requests are sent again."""
    import logging

    from latet import runtime as runtime_module

    runtime, settings = _a_server_space(tmp_path)
    engine = runtime.built.engine
    unrestorable = [True]
    _unrestorable_while(monkeypatch, runtime, unrestorable)
    try:
        with caplog.at_level(logging.WARNING, logger="latet.runtime"):
            settings.refuse = True
            with pytest.raises(OSError, match="PoolTimeout"):
                runtime.apply_config(runtime.config.model_copy(update={"dump_guard": 3}))  # stranded
            unrestorable[0] = False
            with pytest.raises(OSError, match="PoolTimeout"):
                runtime.apply_config(runtime.config.model_copy(update={"dump_guard": 4}))  # whole, then refused
            refused = (_said(caplog), engine.stranded, engine.applying)
            settings.refuse = False
            runtime.apply_config(runtime.config.model_copy(update={"dump_guard": 5}))  # whole, and kept
            kept = (_said(caplog)[len(refused[0]) :], engine.stranded, engine.applying)
    finally:
        runtime.close()
    assert refused == ([_PUT_BACK_FAILED, ("ERROR", runtime_module._STRANDED)], True, 1), refused
    assert kept == ([("WARNING", runtime_module._UNSTRANDED)], False, 0), kept
    assert runtime.config.dump_guard == 5


def test_a_stranded_computer_whose_whole_save_is_not_written_says_requests_are_sent_again(
    tmp_path, monkeypatch, caplog
):
    """A computer runs the settings it could not write to config.yaml, as before: a stranded computer whose next save
    has its settings whole runs them, and says once that requests are sent again, although its save raised. A later
    strand is said again."""
    import logging

    from latet import runtime as runtime_module
    from latet.state.local import LocalSettings

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    engine = runtime.built.engine
    failing = [1]  # the apply's image masking
    _image_masking_failing(monkeypatch, runtime, failing)
    unrestorable = [True]
    _unrestorable_while(monkeypatch, runtime, unrestorable)

    def unwritten(self: Any, config: Any) -> None:
        raise OSError("config.yaml could not be written")

    monkeypatch.setattr(LocalSettings, "save", unwritten)
    try:
        with caplog.at_level(logging.WARNING, logger="latet.runtime"):
            with pytest.raises(RuntimeError, match="image models"):
                runtime.apply_config(stronger, save=False)  # stranded
            failing[0], unrestorable[0] = 0, False
            with pytest.raises(OSError, match="could not be written"):
                runtime.apply_config(stronger)  # whole, and not written: it runs
            ran = (_said(caplog), engine.stranded, engine.applying, runtime.config.dump_guard)
            failing[0], unrestorable[0] = 1, True
            runtime.mediator.vision = None  # built again by the next apply, and raising there
            with pytest.raises(RuntimeError, match="image models"):
                runtime.apply_config(stronger, save=False)  # stranded again
            again = (_said(caplog)[len(ran[0]) :], engine.stranded, engine.applying)
    finally:
        runtime.close()
    stranded = [_PUT_BACK_FAILED, ("ERROR", runtime_module._STRANDED)]
    assert ran == ([*stranded, ("WARNING", runtime_module._UNSTRANDED)], False, 0, 3), ran
    assert again == (stranded, True, 1), again


# A term the stronger settings mask, in a request's text: it leaves as written under the settings that ran before them.
ORION = "Orion"
ORION_BODY = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": f"hello from {ORION}"}]}).encode()


def _with_a_term(config: Any) -> Any:
    from latet.config import TermConfig

    return config.model_copy(update={"terms": [TermConfig(value=ORION, type="PROJECT")]})


def _image_masking_failing(monkeypatch, runtime: Any, times: list[int]) -> None:
    """The image masking an apply builds raises (its models cannot be read) while ``times[0]`` is above 0, once each
    time; then it is built as it is."""
    from latet import runtime as runtime_module

    read = runtime_module.image_masking

    def failing(*args: Any, **kwargs: Any) -> Any:
        if times[0] > 0:
            times[0] -= 1
            raise RuntimeError("the image models could not be read")
        return read(*args, **kwargs)

    runtime.mediator.vision = None  # built again by each apply: models may have been installed since
    monkeypatch.setattr(runtime_module, "image_masking", failing)


def test_an_apply_that_raises_midway_still_makes_its_last_move_and_lowers_its_mark(tmp_path, monkeypatch):
    """Item 23, ruled: an apply that raises after its first rules move (its image masking cannot be built) left the
    settings half applied with no last move, so that an authority taken meanwhile stayed valid. Its last move is made
    however it ends, what ran before is put back as it ran (the very objects, never built again: fix rounds I1 and
    R1), and every mark is lowered."""
    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    engine = runtime.built.engine
    _image_masking_failing(monkeypatch, runtime, [1])
    try:
        version, masker, built = engine.rules_version, engine.masker, runtime.built
        with pytest.raises(RuntimeError, match="image models"):
            runtime.apply_config(stronger, save=False)
        assert engine.rules_version == version + 2  # ``engine.use``, then its last move, raised midway
        assert engine.applying == 0 and engine.masker is masker and runtime.built is built
    finally:
        runtime.close()


@pytest.mark.parametrize("before", ["off", "the same server"])
def test_an_apply_that_raises_after_its_reviewer_puts_back_the_reviewer_that_ran(tmp_path, monkeypatch, before):
    """R1's put-back of the local reviewer (a control): a save that raises after its reviewer was set (its routes
    cannot be made) puts back the reviewer that ran, as it ran: none when it was off, the one the save made retired
    (closed as a reviewer of earlier settings is); or, on the same server, the very one, its own settings again (the
    save had changed them in place), never among the retired ones."""
    from types import SimpleNamespace

    from latet import runtime as runtime_module
    from latet.config import LlmReview
    from latet.engine.reviewer import Reviewer

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    runtime.review_client = lambda review: cast(Any, SimpleNamespace(key_id=None))

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the routes could not be made")

    try:
        if before == "the same server":
            runtime.apply_config(runtime.config.model_copy(update={"llm_review": LlmReview(enabled=True)}), save=False)
        ran, reviewer = runtime.mediator.llm_review, runtime.mediator.reviewer
        kept = reviewer.config if reviewer is not None else None
        monkeypatch.setattr(runtime_module, "routes_of", failing)
        saved = stronger.model_copy(update={"llm_review": LlmReview(enabled=True, budget_ms=1234)})
        with pytest.raises(RuntimeError, match="routes"):
            runtime.apply_config(saved, save=False)
        retired = list(runtime.mediator.retired)
        assert runtime.mediator.llm_review == ran and runtime.mediator.reviewer is reviewer
        if reviewer is None:
            assert len(retired) == 1 and isinstance(retired[0], Reviewer)  # the one the save made
        else:
            assert reviewer.config is kept and reviewer not in retired
        assert not runtime.built.engine.stranded and runtime.built.engine.applying == 0
    finally:
        runtime.close()


def test_a_request_during_a_put_back_waits_for_it_and_is_answered_under_what_ran_before(tmp_path, monkeypatch, turns):
    """Item 23's second fix round, R2: while what ran before is put back (a save raised midway, or a server's store
    refused it), a request was refused at once, the mark held as for settings that could not be put back, where it
    used to wait for the apply under way. The put-back now runs under the apply's own mark: a request waits for it, as
    for any apply, and is answered under what ran before; the space is stranded only once a put-back has raised."""
    import threading

    runtime, transport, stronger = _a_runtime(tmp_path, monkeypatch)
    stronger = _with_a_term(stronger)
    engine = runtime.built.engine
    _image_masking_failing(monkeypatch, runtime, [1])
    halfway, resume = threading.Event(), threading.Event()
    uses = runtime.projects.use
    calls: list[int] = []

    def use(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:  # the put-back's, after the save raised: held there
            halfway.set()
            assert resume.wait(10)
        return uses(*args, **kwargs)

    monkeypatch.setattr(runtime.projects, "use", use)
    raised: list[BaseException] = []

    def applying() -> None:
        try:
            runtime.apply_config(stronger, save=False)
        except Exception as exc:
            raised.append(exc)

    thread = threading.Thread(target=applying)
    settled = request_policy.settled
    waits: list[tuple[int, bool]] = []

    async def waited(engine: Any) -> None:
        waits.append((engine.applying, engine.stranded))
        resume.set()  # the put-back goes on as the request waits for it
        await settled(engine)

    monkeypatch.setattr(request_policy, "settled", waited)
    preparations = _preparations(monkeypatch)
    maskers: list[Any] = []
    prepare = Mediator.prepare_http

    def noting(self: Mediator, *args: Any, **kwargs: Any) -> Any:
        maskers.append(self.engine.masker)
        return prepare(self, *args, **kwargs)

    monkeypatch.setattr(Mediator, "prepare_http", noting)
    first = engine.masker
    try:
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            thread.start()
            assert halfway.wait(10)
            answered = client.post(ACME_CHAT, content=ORION_BODY, headers=HEADERS)
    finally:
        resume.set()
        thread.join(10)
        runtime.close()
    assert [type(error) for error in raised] == [RuntimeError]
    assert answered.status_code == 200, answered.text
    assert waits == [(1, False)], waits  # it waited, under the apply's own mark
    assert maskers[-1] is first and preparations[-1][1] is None and turns[-1].prepared is preparations[-1][0]
    assert len(transport.sent) == 1 and not engine.stranded and engine.applying == 0


def test_an_apply_whose_failure_persists_puts_back_what_ran_and_serves_it(tmp_path, monkeypatch, turns):
    """Item 23's second fix round, R1: the image models cannot be read, and stay so. A save raises at its image masking;
    its put-back applied the old settings again, built them again, reached the same image masking, raised there too and
    stranded the space, every request refused until a save succeeded, which none could while the models failed. The
    put-back now restores the very objects that ran before the save's first change, read nothing again: the space
    serves its old settings, not stranded, each later save that fails the same leaving it so."""
    runtime, transport, stronger = _a_runtime(tmp_path, monkeypatch)
    stronger = _with_a_term(stronger)
    engine = runtime.built.engine
    _image_masking_failing(monkeypatch, runtime, [99])  # unreadable for good
    preparations = _preparations(monkeypatch)
    try:
        first = engine.masker
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            for _ in range(2):
                with pytest.raises(RuntimeError, match="image models"):
                    runtime.apply_config(stronger, save=False)
            answered = client.post(ACME_CHAT, content=ORION_BODY, headers=HEADERS)
    finally:
        runtime.close()
    assert not engine.stranded and engine.applying == 0
    assert answered.status_code == 200, answered.text
    [sent] = transport.sent
    assert ORION.encode() in sent.content and preparations[-1][1] is None  # the old masker and dump guard
    assert engine.masker is first and runtime.config.terms == []


# The apply's own mark (item 23), which ``apply_config`` sets before its save and its ``finally`` settles, never put
# back as a setting.
_THE_APPLYS_MARK = frozenset({"self.built.engine.applying", "self.built.engine.stranded", "self._stranded_said"})


def _set_before_the_save() -> tuple[set[str], set[str]]:
    """What ``Runtime.apply_config`` changes before its save, read from its code and from the runtime's own methods it
    calls there (``use_reviewer``): each attribute it assigns, by its path from the runtime (``self.mediator.images``),
    and each ``use`` it calls (``self.gateway.routes.use``). A local name that stands for such a path (``engine``,
    ``mediator``, ``old``) is read as that path. What it changes after its save is kept with the settings it saved."""
    import ast
    import inspect
    import textwrap

    from latet.runtime import Runtime

    assigned: set[str] = set()
    used: set[str] = set()

    def parsed(name: str) -> ast.FunctionDef:
        [node] = ast.parse(textwrap.dedent(inspect.getsource(vars(Runtime)[name]))).body
        assert isinstance(node, ast.FunctionDef)
        return node

    def path(node: ast.expr, aliases: dict[str, str]) -> str | None:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        return ".".join([aliases.get(node.id, node.id), *reversed(parts)]) if isinstance(node, ast.Name) else None

    def method(name: str) -> bool:
        function = vars(Runtime).get(name)
        return inspect.isfunction(function) and function.__qualname__ == f"Runtime.{name}"

    def read(function: ast.FunctionDef, statements: list[ast.stmt]) -> None:
        aliases: dict[str, str] = {}
        named_once = [node for node in ast.walk(function) if isinstance(node, ast.Assign)]
        for node in sorted(named_once, key=lambda node: (node.lineno, node.col_offset)):  # in the order they are set
            named = path(node.value, aliases) if [type(target) for target in node.targets] == [ast.Name] else None
            if named is not None and named.startswith("self."):
                aliases[cast(ast.Name, node.targets[0]).id] = named  # a local name that stands for a path
        for statement in statements:
            for node in ast.walk(statement):
                targets = node.targets if isinstance(node, ast.Assign) else []
                targets = [node.target] if isinstance(node, ast.AugAssign) else targets
                assigned.update(
                    named
                    for target in targets
                    if isinstance(target, ast.Attribute) and (named := path(target, aliases)) is not None
                )
                if isinstance(node, ast.Call) and (called := path(node.func, aliases)) is not None:
                    if called.endswith(".use"):
                        used.add(called)
                    elif called.count(".") == 1 and called.startswith("self.") and method(called[5:]):
                        callee = parsed(called[5:])
                        read(callee, callee.body)

    apply = parsed("apply_config")
    [attempt] = [node for node in ast.walk(apply) if isinstance(node, ast.Try) and node.finalbody]
    [save] = [i for i, node in enumerate(attempt.body) if isinstance(node, ast.If) and ast.unparse(node.test) == "save"]
    read(apply, attempt.body[:save])
    return assigned, used


def test_every_setting_an_apply_changes_is_put_back_as_it_ran(tmp_path, monkeypatch):
    """Task 8 of the 0.12 plan (item 23's put-back, R1): each attribute ``apply_config`` sets before its save, and each
    ``use`` it calls there, is read by ``_in_force`` and put back by ``_restore``, the very object, so that a setting
    added to an apply is never applied and then left in force when the apply raises midway or a server's store
    refuses it. The fields are found in the code by name; each is then changed as an apply would and put back."""
    from types import SimpleNamespace

    from latet.config import Config, LlmReview

    assigned, used = _set_before_the_save()
    assert {"self.mediator.dump_guard", "self.gateway.destinations", "self.config", "self.built"} <= assigned
    assert "self.mediator.reviewer.config" in assigned  # read through ``use_reviewer``'s ``old``
    # How the test reads what each ``use`` changes (the objects in use), and changes it as an apply would; a new one is
    # added here.
    uses: dict[str, tuple[Callable[[Any], tuple[Any, ...]], Callable[[Any], None]]] = {
        "self.projects.use": (lambda r: r.projects.in_use(), lambda r: r.projects.use(Config(), None)),
        "self.built.engine.use": (lambda r: (r.built.engine.masker,), lambda r: r.built.engine.use(object())),
        "self.mediator.verifier.use": (
            lambda r: (r.mediator.verifier.masker,),
            lambda r: r.mediator.verifier.use(object()),
        ),
        "self.gateway.routes.use": (lambda r: (r.gateway.routes.table,), lambda r: r.gateway.routes.use([])),
    }
    assert used == set(uses), "a use that apply_config calls: say here how it is read and changed"
    runtime, _, _ = _a_runtime(tmp_path, monkeypatch)
    runtime.review_client = lambda review: cast(Any, SimpleNamespace(key_id=None))
    create_app(runtime.gateway)  # its route table, as a gateway that runs has
    try:
        # A reviewer runs, so that its settings are there to put back; every owner below is there.
        runtime.apply_config(runtime.config.model_copy(update={"llm_review": LlmReview(enabled=True)}), save=False)
        settings = sorted(assigned - _THE_APPLYS_MARK)
        owners = {}
        for named in settings:
            owner: Any = runtime
            for part in named.split(".")[1:-1]:
                owner = getattr(owner, part)
            assert owner is not None, named
            owners[named] = owner
        ran = {named: getattr(owners[named], named.rsplit(".", 1)[1]) for named in settings}
        ran_uses = {named: read(runtime) for named, (read, _) in uses.items()}
        was = runtime._in_force()
        for _, change in uses.values():
            change(runtime)
        made = {named: object() for named in settings}  # what an apply leaves, each its own
        for named in settings:
            setattr(owners[named], named.rsplit(".", 1)[1], made[named])
        runtime._restore(was)
        moved = [named for named in settings if getattr(owners[named], named.rsplit(".", 1)[1]) is not ran[named]]
        moved += [
            named
            for named, (read, _) in uses.items()
            if len(now := read(runtime)) != len(ran_uses[named])
            or any(a is not b for a, b in zip(now, ran_uses[named], strict=True))
        ]
        assert moved == [], "applied by apply_config and not put back by _in_force and _restore"
        # The reviewer the apply made is retired, as _restore retires one: taken out, it was never a reviewer.
        assert runtime.mediator.retired[-1] is made["self.mediator.reviewer"]
        runtime.mediator.retired.pop()
    finally:
        runtime.close()


@pytest.mark.parametrize("when", ["after", "across"])
def test_a_request_after_an_apply_that_raised_midway_is_prepared_under_the_settings_that_ran_before(
    tmp_path, monkeypatch, turns, when
):
    """Item 23's fix round, I1: an apply that raises after its first rules move (its image models cannot be read) left
    its new masker and image policy in force beside the dump guard and route table of before, and lowered its mark: a
    request sent after it (``after``), or one that waited for it in the gateway (``across``), was prepared under that
    mix and sent. What ran before is now put back as it ran: such a request is prepared under it alone, its masker
    and dump guard. Sent after, the term the new settings mask leaves as it was written. Across, its first preparation,
    made in the apply's window under the new masker (never sent), taught its conversation a fake for that term, which
    a value once shown it keeps (only ever more masking): what is checked is the masker it was sent from."""
    runtime, transport, stronger = _a_runtime(tmp_path, monkeypatch)
    stronger = _with_a_term(stronger)
    engine = runtime.built.engine
    _image_masking_failing(monkeypatch, runtime, [1])
    preparations = _preparations(monkeypatch)
    maskers: list[Any] = []  # the masker each preparation was made under, in order
    prepare = Mediator.prepare_http

    def noting(self: Mediator, *args: Any, **kwargs: Any) -> Any:
        maskers.append(self.engine.masker)
        return prepare(self, *args, **kwargs)

    monkeypatch.setattr(Mediator, "prepare_http", noting)
    first = engine.masker
    held = _HeldApply(runtime, stronger, monkeypatch) if when == "across" else None
    if held is not None:
        settled = request_policy.settled

        async def waited(engine: Any) -> None:
            held.resume.set()  # the apply goes on as the gateway waits for it, and raises midway
            await settled(engine)

        monkeypatch.setattr(request_policy, "settled", waited)
    try:
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            if held is None:
                with pytest.raises(RuntimeError, match="image models"):
                    runtime.apply_config(stronger, save=False)
            else:
                held.start()
            answered = client.post(ACME_CHAT, content=ORION_BODY, headers=HEADERS)
    finally:
        if held is not None:
            held.end()
        runtime.close()
    assert held is None or [type(error) for error in held.raised] == [RuntimeError]
    assert answered.status_code == 200, answered.text
    [sent] = transport.sent
    if held is None:
        assert ORION.encode() in sent.content, sent.content  # never masked by settings that did not apply
        assert maskers == [first] and engine.masker is first  # what ran before, the very masker
    else:  # in the window, under the new masker; then under what ran before, the very masker
        assert len(maskers) == 2 and maskers[0] is not first and maskers[-1] is first, maskers
        assert engine.masker is first
    assert preparations[-1][1] is None and turns[-1].prepared is preparations[-1][0]  # the dump guard of before
    assert runtime.mediator.dump_guard is None and runtime.config.terms == []
    assert engine.applying == 0 and not engine.stranded


def test_settings_that_cannot_be_applied_nor_put_back_refuse_every_request_until_an_apply_ends_whole(
    tmp_path, monkeypatch, turns, caplog
):
    """I1, failing closed: settings that raise midway, and what ran before raising as well as it is put back, leave a
    mix of both in force. The mark then stays up: every request is refused because its settings changed, none of it
    prepared under that mix, nothing sent; it is said once, in fixed words, however many applies fail meanwhile. A
    later apply that ends whole lowers it, and requests are served again, under it."""
    import logging

    from latet import runtime as runtime_module

    runtime, transport, stronger = _a_runtime(tmp_path, monkeypatch)
    stronger = _with_a_term(stronger)
    engine = runtime.built.engine
    failing = [1]  # the apply's image masking
    _image_masking_failing(monkeypatch, runtime, failing)
    preparations = _preparations(monkeypatch)
    restore = runtime._restore
    unrestorable = [True]

    def restoring(was: Any) -> None:
        if unrestorable[0]:
            raise RuntimeError("what ran before could not be put back")
        restore(was)

    monkeypatch.setattr(runtime, "_restore", restoring)
    try:
        with (
            caplog.at_level(logging.WARNING, logger="latet.runtime"),
            TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client,
        ):
            with pytest.raises(RuntimeError, match="image models"):
                runtime.apply_config(stronger, save=False)
            first = client.post(ACME_CHAT, content=ORION_BODY, headers=HEADERS)
            failing[0] = 1  # another apply fails meanwhile, and what ran before cannot be put back again
            with pytest.raises(RuntimeError, match="image models"):
                runtime.apply_config(stronger, save=False)
            second = client.post(ACME_CHAT, content=ORION_BODY, headers=HEADERS)
            refused = (len(preparations), len(transport.sent), [getattr(turn.decision, "code", None) for turn in turns])
            failing[0], unrestorable[0] = 0, False
            runtime.apply_config(stronger, save=False)  # a later apply that ends whole
            served = client.post(ACME_CHAT, content=ORION_BODY, headers=HEADERS)
    finally:
        runtime.close()
    for answer in (first, second):
        assert answer.status_code == 400 and SETTINGS_MOVED in answer.text, answer.text
    assert refused == (0, 0, ["settings-changed", "settings-changed"]), refused  # none prepared under the mix
    errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors == [runtime_module._STRANDED], errors  # said once, in fixed words
    assert _said(caplog).count(("WARNING", runtime_module._UNSTRANDED)) == 1  # and said over once, applied whole
    assert served.status_code == 200, served.text
    [sent] = transport.sent
    assert ORION.encode() not in sent.content and preparations[-1][1] == 3  # under the settings applied whole
    assert engine.applying == 0 and not engine.stranded


# --- the applications API while a save is applied (item 23's fix round, root's ruling on concern 2) -------------------

APPS_GRANT = {"token": "t_billing", "permissions": ["scan", "mask", "unmask"]}


def _apps_client(runtime: Any, engine: Any = None, entries: list[tuple[str, Any]] | None = None) -> TestClient:
    """The applications API over ``runtime``'s engine (``engine``: the same, as the calls read it), as an API key's
    space on a server mounts it, each call handed on as the server's gate hands it on (its identity, a grant of the
    three permissions). The administration log takes every entry, each kept in ``entries`` when given."""
    from starlette.applications import Starlette

    from latet.bridge.apps import Apps
    from latet.gateway.app import OnServer

    async def audit(action: str, details: Any, address: str | None) -> None:
        if entries is not None:
            entries.append((action, details))

    on_server = OnServer("https://latet.acme.lan", "u_billingcix", audit=audit, kind="key")
    apps = Apps(runtime.mediator, on_server)
    if engine is not None:
        apps.engine = engine
    inner = Starlette(routes=apps.routes())

    async def gated(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope = {**scope, "latet.identity": on_server.identity, "latet.grant": APPS_GRANT}
        await inner(scope, receive, send)

    return TestClient(gated, base_url="http://127.0.0.1:5283")


def test_an_application_call_during_a_save_waits_for_it_and_is_answered_under_the_new_settings(tmp_path, monkeypatch):
    """An application's call (``bridge.apps``) that comes while a save is applied waits for it, as a request over HTTP
    does (``request_policy.settled``), and is masked under the settings once they are all in place. Before, it was
    masked at once under what the apply had reached: here, its first change, the masker of before, which leaves the
    new settings' term in clear."""
    from latet.bridge.apps import APPS_API

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    held = _HeldApply(runtime, _with_a_term(stronger), monkeypatch, "first-change")
    settled = request_policy.settled
    waits: list[int] = []

    async def waited(engine: Any) -> None:
        waits.append(engine.applying)
        held.resume.set()  # the apply goes on as the call waits for it
        await settled(engine)

    monkeypatch.setattr(request_policy, "settled", waited)
    try:
        with _apps_client(runtime) as client:
            held.start()
            answered = client.post(f"{APPS_API}/mask", json={"texts": [f"hello from {ORION}"]})
    finally:
        held.end()
        runtime.close()
    assert answered.status_code == 200, answered.text
    [result] = answered.json()["results"]
    assert ORION not in result["text"] and result["counts"] == {"PROJECT": 1}, (result, waits)
    assert waits == [1]


@pytest.mark.parametrize("operation", ["scan", "mask", "unmask"])
def test_an_application_call_while_settings_cannot_be_put_back_is_refused_and_nothing_is_read(
    tmp_path, monkeypatch, operation
):
    """With the mark held for settings that could neither be applied nor put back (``Engine.stranded``), what runs is
    a mix of both: an application's call is refused at once, 503, in fixed words that say the settings changed and to
    send it again; nothing of it is masked, scanned or restored under the mix."""
    from latet.bridge import apps as apps_module
    from latet.bridge.apps import APPS_API

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    _image_masking_failing(monkeypatch, runtime, [1])

    def unrestorable(was: Any) -> None:
        raise RuntimeError("what ran before could not be put back")

    monkeypatch.setattr(runtime, "_restore", unrestorable)
    read: list[str] = []
    for name in ("_scan", "_mask", "_unmask"):
        real = getattr(apps_module.Apps, name)

        def reading(self: Any, *args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            read.append(_name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(apps_module.Apps, name, reading)
    try:
        with pytest.raises(RuntimeError, match="image models"):
            runtime.apply_config(_with_a_term(stronger), save=False)
        assert runtime.built.engine.stranded
        with _apps_client(runtime) as client:
            answered = client.post(f"{APPS_API}/{operation}", json={"texts": [f"write to {EMAIL} from {ORION}"]})
    finally:
        runtime.close()
    assert answered.status_code == 503, answered.text
    assert answered.json() == {"error": apps_module.SETTINGS_CHANGED} and "send it again" in answered.text
    assert read == []


def test_an_application_call_a_save_overtakes_is_refused(tmp_path, monkeypatch):
    """A save that starts while an application's call is masked (after its wait): its answer, made as the settings
    began to change, is not given; the call is refused in the same fixed words, to send it again."""
    from latet.bridge import apps as apps_module
    from latet.bridge.apps import APPS_API

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    held = _HeldApply(runtime, _with_a_term(stronger), monkeypatch)
    real = apps_module.Apps._mask

    def overtaken(self: Any, *args: Any, **kwargs: Any) -> Any:
        done = real(self, *args, **kwargs)
        held.start()  # the save starts as the call's texts are masked
        return done

    monkeypatch.setattr(apps_module.Apps, "_mask", overtaken)
    try:
        with _apps_client(runtime) as client:
            answered = client.post(f"{APPS_API}/mask", json={"texts": [f"hello from {ORION}"]})
    finally:
        held.end()
        runtime.close()
    assert held.started
    assert answered.status_code == 503, answered.text
    assert answered.json() == {"error": apps_module.SETTINGS_CHANGED}


def test_an_application_call_a_save_begins_and_ends_within_is_refused(tmp_path, monkeypatch):
    """Item 23's second fix round, R4: a whole save, begun and ended while an application's call is answered, leaves no
    mark up as the answer is checked; only the count of applies begun (``Engine.applies``) tells that the settings
    changed under the call. Its answer is not given (503, the fixed words)."""
    from latet.bridge import apps as apps_module
    from latet.bridge.apps import APPS_API

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    real = apps_module.Apps._mask

    def across(self: Any, *args: Any, **kwargs: Any) -> Any:
        done = real(self, *args, **kwargs)
        runtime.apply_config(_with_a_term(stronger), save=False)  # begun and ended as the texts are masked
        return done

    monkeypatch.setattr(apps_module.Apps, "_mask", across)
    try:
        with _apps_client(runtime) as client:
            answered = client.post(f"{APPS_API}/mask", json={"texts": [f"hello from {ORION}"]})
    finally:
        runtime.close()
    assert runtime.built.engine.applying == 0 and runtime.built.engine.applies == 1  # no mark left: the count alone
    assert answered.status_code == 503, answered.text
    assert answered.json() == {"error": apps_module.SETTINGS_CHANGED}


class _Watched:
    """An engine as an application's call reads it: its mark, then the count of applies begun. ``at_mark`` runs right
    after the call's own check of the mark read it (its second read: the wait made the first)."""

    def __init__(self, engine: Any, at_mark: Callable[[], None]) -> None:
        self._engine, self._at_mark, self._reads = engine, at_mark, 0

    @property
    def applying(self) -> int:
        value = self._engine.applying
        self._reads += 1
        if self._reads == 2:
            self._at_mark()
        return value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)


def test_an_application_call_reads_the_count_of_applies_before_its_mark(tmp_path, monkeypatch):
    """R4: the call read the count of applies begun after its check of the mark. A save that began between the two,
    and ended while the call's texts were masked (under what that save had reached), was counted as begun before the
    call: both checks of the answer passed, and it was given, masked half way (the new term in clear). The count is
    read first now: the answer is not given."""
    from latet.bridge import apps as apps_module
    from latet.bridge.apps import APPS_API

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    held = _HeldApply(runtime, _with_a_term(stronger), monkeypatch, "first-change")
    real = apps_module.Apps._mask

    def masked_then_ended(self: Any, *args: Any, **kwargs: Any) -> Any:
        done = real(self, *args, **kwargs)  # while the save is held at its first change
        held.end()  # then it ends, before the answer is checked
        return done

    monkeypatch.setattr(apps_module.Apps, "_mask", masked_then_ended)
    try:
        with _apps_client(runtime, _Watched(runtime.built.engine, held.start)) as client:
            answered = client.post(f"{APPS_API}/mask", json={"texts": [f"hello from {ORION}"]})
    finally:
        held.end()
        runtime.close()
    assert held.started and runtime.built.engine.applying == 0
    assert answered.status_code == 503, answered.text
    assert answered.json() == {"error": apps_module.SETTINGS_CHANGED}


class _AppsHeard:
    """The reports of the applications API's calls to the metrics (``observe.apps``), as a server's Metrics hears
    them; the others are heard and dropped."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def masked(self, found: Any) -> None:
        return None

    def exchange(self, agent: str, stopped: bool) -> None:
        return None

    def waiting(self, delta: int) -> None:
        return None

    def apps(self, operation: str, texts: int, values: int) -> None:
        self.calls.append((operation, texts, values))


def _a_fake_of_the_email(client: TestClient) -> str:
    """``EMAIL`` masked through the applications API: a text holding its fake alone, for an unmask to restore."""
    from latet.bridge.apps import APPS_API

    masked = client.post(f"{APPS_API}/mask", json={"texts": [f"write to {EMAIL}"]})
    assert masked.status_code == 200, masked.text
    [result] = masked.json()["results"]
    assert EMAIL not in result["text"] and result["counts"] == {"EMAIL": 1}, result
    return result["text"]


@pytest.mark.parametrize("operation", ["scan", "mask", "unmask"])
def test_an_application_call_a_save_overtakes_records_nothing_and_its_retry_is_recorded_once(
    tmp_path, monkeypatch, operation
):
    """The 0.12 merge's reviews (L2, L4): a call a save overtakes is refused (503), but what records an answer was made
    before that check, in the work: the call's count in the metrics (``observe.apps``) and an unmask's
    ``apps.unmasked`` entry in the administration log, which Enterprise sends on to the SIEM. An unmask never given
    was logged as given, and its retry logged again. Now the call refused records nothing, and its retry, answered,
    is recorded once."""
    from latet import observe
    from latet.bridge import apps as apps_module
    from latet.bridge.apps import APPS_API

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    entries: list[tuple[str, Any]] = []
    heard = _AppsHeard()
    unlisten = observe.listen(heard)
    held = _HeldApply(runtime, _with_a_term(stronger), monkeypatch)
    real = getattr(apps_module.Apps, f"_{operation}")

    def overtaken(self: Any, *args: Any, **kwargs: Any) -> Any:
        done = real(self, *args, **kwargs)
        if not held.started:
            held.start()  # the save starts as the first call's texts are read, and is held there
        return done

    try:
        with _apps_client(runtime, entries=entries) as client:
            text = _a_fake_of_the_email(client) if operation == "unmask" else f"write to {EMAIL}"
            entries.clear()
            heard.calls.clear()
            monkeypatch.setattr(apps_module.Apps, f"_{operation}", overtaken)
            refused = client.post(f"{APPS_API}/{operation}", json={"texts": [text]})
            recorded_on_refusal = (list(entries), list(heard.calls))
            held.end()  # the save ends: the call is sent again
            retried = client.post(f"{APPS_API}/{operation}", json={"texts": [text]})
    finally:
        unlisten()
        held.end()
        runtime.close()
    assert held.started and not held.raised
    assert refused.status_code == 503, refused.text
    assert refused.json() == {"error": apps_module.SETTINGS_CHANGED}
    assert recorded_on_refusal == ([], [])
    assert retried.status_code == 200, retried.text
    logged = [("apps.unmasked", {"token": "t_billing", "texts": 1, "restored": 1})] if operation == "unmask" else []
    assert entries == logged
    assert heard.calls == [(operation, 1, 1)]
    if operation == "unmask":
        assert retried.json() == {"results": [{"text": f"write to {EMAIL}", "restored": 1}]}


def test_an_unmask_a_save_begins_over_as_its_entry_is_written_is_answered(tmp_path, monkeypatch):
    """An unmask whose texts were restored before any save began, a save beginning only while its entry is written in
    the administration log: what it restored was restored under whole settings, and the entry says it was given. It is
    answered, its entry written once and its call counted once. Before, the check that came after the entry refused
    it (503), the entry written all the same."""
    from latet import observe
    from latet.bridge.apps import APPS_API

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    heard = _AppsHeard()
    unlisten = observe.listen(heard)
    held = _HeldApply(runtime, _with_a_term(stronger), monkeypatch)

    class _StartingTheSave(list[tuple[str, Any]]):
        """The administration log's entries; the first one written starts the save, held under way."""

        def append(self, entry: tuple[str, Any]) -> None:
            super().append(entry)
            if not held.started:
                held.start()

    entries = _StartingTheSave()
    try:
        with _apps_client(runtime, entries=entries) as client:
            fake = _a_fake_of_the_email(client)  # a mask: no entry
            heard.calls.clear()
            answered = client.post(f"{APPS_API}/unmask", json={"texts": [fake]})
            applying = runtime.built.engine.applying
    finally:
        unlisten()
        held.end()
        runtime.close()
    assert held.started and applying == 1 and not held.raised
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"results": [{"text": f"write to {EMAIL}", "restored": 1}]}
    assert entries == [("apps.unmasked", {"token": "t_billing", "texts": 1, "restored": 1})]
    assert heard.calls == [("unmask", 1, 1)]


@pytest.mark.parametrize("operation", sorted(_UNDER_THE_SETTINGS))
def test_a_gated_operation_that_never_asks_whether_a_save_came_over_it_is_refused(
    tmp_path, monkeypatch, caplog, operation
):
    """The review of the 0.12.1 minors (L2): each gated operation asks itself whether a save came over it
    (``overtaken``), before it records anything. One that never asked, as a new operation could forget to, was answered
    across a save, and nothing noticed. The gate fails closed now: a gated operation that did not ask is refused (503,
    the fixed words, to send it again), with no save at all, and the log says which operation did not ask."""
    import logging

    from starlette.responses import JSONResponse

    from latet.bridge import apps as apps_module
    from latet.bridge.apps import APPS_API

    runtime, _, _ = _a_runtime(tmp_path, monkeypatch)

    async def never_asks(self: Any, request: Any, grant: Any, overtaken: Any, *given: Any) -> Any:
        return JSONResponse({"results": []})

    monkeypatch.setattr(apps_module.Apps, operation, never_asks)
    try:
        with caplog.at_level(logging.ERROR, logger="latet.bridge.apps"), _apps_client(runtime) as client:
            answered = client.post(f"{APPS_API}/{operation}", json={"texts": ["hello"]})
    finally:
        runtime.close()
    assert runtime.built.engine.applying == 0 and runtime.built.engine.applies == 0  # no save at all
    assert answered.status_code == 503, answered.text
    assert answered.json() == {"error": apps_module.SETTINGS_CHANGED}
    said = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert any(operation in line and "never asked" in line for line in said), said


# --- the Claude Code plugin's bridge while a save is applied (item 23's second fix round, R3) ---------------------------

BRIDGE = "/_latet/engine/v1"
_NEW_TERM = {"session": "s1", "items": [{"text": f"hello from {ORION}", "kind": "user_text"}]}


def _bridge_client(runtime: Any) -> TestClient:
    """The plugin's bridge over ``runtime``'s engine, as a computer serves it, each call carrying the bridge's token."""
    from starlette.applications import Starlette

    from latet.bridge.api import Bridge

    token = runtime.gateway.bridge_token
    assert token is not None
    app = Starlette(routes=Bridge(runtime.mediator, token).routes())
    return TestClient(app, base_url="http://127.0.0.1:5283", headers={"x-latet-token": token})


def test_a_plugin_mask_during_a_save_waits_for_it_and_is_answered_under_the_new_settings(tmp_path, monkeypatch):
    """Item 23's second fix round, R3: the plugin's ``mask`` (in client mode, what Claude Code then sends) was answered
    under whatever a save under way had reached. It now waits for the save as a request over HTTP does
    (``request_policy.settled``), and is masked under the settings once they are all in place: the new term masked."""
    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    held = _HeldApply(runtime, _with_a_term(stronger), monkeypatch, "first-change")
    settled = request_policy.settled
    waits: list[int] = []

    async def waited(engine: Any) -> None:
        waits.append(engine.applying)
        held.resume.set()  # the save goes on as the call waits for it
        await settled(engine)

    monkeypatch.setattr(request_policy, "settled", waited)
    try:
        with _bridge_client(runtime) as client:
            held.start()
            answered = client.post(f"{BRIDGE}/mask", json=_NEW_TERM)
    finally:
        held.end()
        runtime.close()
    assert answered.status_code == 200, answered.text
    [item] = answered.json()["items"]
    assert ORION not in item["text"] and item["count"] == 1 and waits == [1], (item, waits)


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("mask", _NEW_TERM),
        ("outbound", {"session": "s1", "tool": "WebSearch", "values": [f"news of {ORION}"]}),
        ("approvals/wait", {"id": "b_nothing"}),
    ],
)
def test_a_plugin_call_while_settings_cannot_be_put_back_is_refused_and_nothing_is_read(
    tmp_path, monkeypatch, route, body
):
    """R3: while the mark is held for settings that could not be put back (a mix in force), the plugin's ``mask``,
    ``outbound`` and ``approvals/wait`` are refused at once, 503, in fixed words that say the settings changed and to
    send it again; nothing of them is masked, looked at for stand-ins or asked of the Live Reviewer's queue."""
    from latet.bridge import api as bridge_module

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    _image_masking_failing(monkeypatch, runtime, [1])

    def unrestorable(was: Any) -> None:
        raise RuntimeError("what ran before could not be put back")

    monkeypatch.setattr(runtime, "_restore", unrestorable)
    read: list[str] = []
    for name in ("_mask", "_outbound"):
        real = getattr(bridge_module.Bridge, name)

        def reading(self: Any, *args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            read.append(_name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(bridge_module.Bridge, name, reading)
    answer = runtime.mediator.approvals.answer

    async def asked(*args: Any, **kwargs: Any) -> Any:
        read.append("answer")
        return await answer(*args, **kwargs)

    monkeypatch.setattr(runtime.mediator.approvals, "answer", asked)
    try:
        with pytest.raises(RuntimeError, match="image models"):
            runtime.apply_config(_with_a_term(stronger), save=False)
        assert runtime.built.engine.stranded
        with _bridge_client(runtime) as client:
            answered = client.post(f"{BRIDGE}/{route}", json=body)
    finally:
        runtime.close()
    assert answered.status_code == 503, answered.text
    assert answered.json() == {"error": bridge_module.SETTINGS_CHANGED}, answered.text
    assert read == []


@pytest.mark.parametrize(
    ("route", "body", "work"),
    [
        ("mask", _NEW_TERM, "_mask"),
        ("outbound", {"session": "s1", "tool": "WebSearch", "values": [f"news of {ORION}"]}, "_outbound"),
    ],
)
def test_a_plugin_call_a_save_begins_and_ends_within_is_refused(tmp_path, monkeypatch, route, body, work):
    """R3: a whole save begun and ended while the plugin's ``mask`` or ``outbound`` is answered: the answer, made as
    the settings changed under it, is not given (503, the fixed words); the count of applies begun tells it."""
    from latet.bridge import api as bridge_module

    runtime, _, stronger = _a_runtime(tmp_path, monkeypatch)
    real = getattr(bridge_module.Bridge, work)

    def across(self: Any, *args: Any, **kwargs: Any) -> Any:
        done = real(self, *args, **kwargs)
        runtime.apply_config(_with_a_term(stronger), save=False)  # begun and ended as the call is answered
        return done

    monkeypatch.setattr(bridge_module.Bridge, work, across)
    try:
        with _bridge_client(runtime) as client:
            answered = client.post(f"{BRIDGE}/{route}", json=body)
    finally:
        runtime.close()
    assert runtime.built.engine.applying == 0 and runtime.built.engine.applies == 1
    assert answered.status_code == 503, answered.text
    assert answered.json() == {"error": bridge_module.SETTINGS_CHANGED}


async def test_a_plugin_answer_decided_during_a_save_waits_for_it_and_is_masked_under_the_new_settings(
    tmp_path, monkeypatch
):
    """R3, ``approvals/wait``: with the Live Reviewer on, the plugin's texts wait for you, and the answer it polls for
    is worked out once you decide, masked again with your values (``Bridge._answer``). Decided while a save is applied,
    it was worked out at once under what the save had reached (here its first change: the masker of before, the new
    term left in clear). It now waits for the save first, as ``mask`` does, and is masked under the new settings."""
    import asyncio
    import base64

    import httpx
    from starlette.applications import Starlette

    from latet.bridge import api as bridge_module
    from latet.bridge.api import Bridge
    from latet.config import Config, Destination, IdentityConfig, LiveReview
    from latet.runtime import make_runtime

    from ..conftest import ROOT_KEY

    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    monkeypatch.setattr(bridge_module, "HOLD", 10.0)
    acme = Destination(name="acme", url="https://llm.acme.invalid")
    settings = Config(
        identity=IdentityConfig(enabled=False), ner="off", upstreams=[acme], live_review=LiveReview(enabled=True)
    )
    runtime = make_runtime(settings, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    held = _HeldApply(runtime, _with_a_term(settings), monkeypatch, "first-change")
    token = runtime.gateway.bridge_token
    assert token is not None
    bridge = Bridge(runtime.mediator, token)
    transport = httpx.ASGITransport(app=Starlette(routes=bridge.routes()))
    headers = {"x-latet-token": token}
    settled = request_policy.settled

    async def waited(engine: Any) -> None:
        task = asyncio.current_task()
        if task is not None and task.get_coro().__qualname__.endswith("Bridge._answer"):  # type: ignore[union-attr]
            held.resume.set()  # the save goes on as the answer waits for it
        await settled(engine)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False) as client:
            pending = (await client.post(f"{BRIDGE}/mask", json=_NEW_TERM, headers=headers)).json()["pending"]
            monkeypatch.setattr(request_policy, "settled", waited)
            await asyncio.to_thread(held.start)  # a save under way, held at its first change
            assert await runtime.mediator.approvals.decide(pending, True)  # you decide meanwhile
            answered = await client.post(f"{BRIDGE}/approvals/wait", json={"id": pending}, headers=headers)
    finally:
        held.end()
        runtime.close()
    assert answered.status_code == 200, answered.text
    [item] = answered.json()["items"]
    assert ORION not in item["text"], item


def test_an_absent_mediator_is_refused_before_transport(turns, monkeypatch):
    """Missing mediation cannot mint transport authority."""
    validated: list[tuple[Any, Any, int]] = []
    real_validate = request_transport.validate_send

    def validating(decision: Any, arena: Any, envelope: Any, policy: Any, attempt: int, **kwargs: Any) -> Any:
        validated.append((decision, arena, attempt))
        return real_validate(decision, arena, envelope, policy, attempt, **kwargs)

    monkeypatch.setattr(request_transport, "validate_send", validating)
    body = _chat(f"write to {EMAIL}")
    gateway = _gateway(None)
    with gateway.client as client:
        assert client.post(CHAT, content=body, headers=HEADERS).status_code == 400
    assert validated == [] and gateway.transport.sent == []
    assert type(turns[-1].decision) is Block
    assert gateway.counts() == (0, 0) and turns[-1].execution == NotAttempted("proof-incomplete")


def _sends_nothing(block: Any) -> None:
    with pytest.raises(NotSendable) as refused:
        outcome.sendable(block, None, _envelope(b"{}"))
    assert refused.value.code == "block"


def test_requests_refused_before_their_content_is_read_are_blocks(turns):
    """No route, a cloud's call Latet cannot read, a signing route this Latet did not make (404 each), and on a
    server a destination of the identity's own it may not reach (400): each a minted ``Block``, nothing built or sent,
    and the client answered as before."""
    body = _invoke("hello")
    json_headers = {"content-type": "application/json"}
    gateway = _bedrock(_mediator(), _Aws())
    with gateway.client as client:
        nowhere = client.post("/nowhere/v1/chat", content=body, headers=json_headers)
        unread = client.post(f"/bedrock/{REGION}/model/m/converse", content=body, headers=json_headers)
        unsigned = client.post(_route("0" * 32), content=body, headers=json_headers)
    assert nowhere.status_code == 404 and nowhere.json() == {"error": "no upstream for this path"}
    assert unread.status_code == 400 and "envelope-route" in unread.json()["message"]
    assert unsigned.status_code == 404 and "no such route" in unsigned.json()["message"]
    expected = [
        (None, "no-route", 404, "no upstream for this path"),
        ("bedrock", "envelope-route", 400, "request envelope refused (envelope-route)"),
        ("bedrock", "no-signing-route", 404, "there is no such route"),
    ]
    assert [(t.decision.destination, t.decision.code, t.decision.status, t.decision.reason) for t in turns] == expected
    for turn in turns:
        assert type(turn.decision) is Block and turn.decision.transport == "http" and turn.prepared is None
        assert turn.execution == NotAttempted(turn.decision.code)
        _sends_nothing(turn.decision)
    assert gateway.counts() == (0, 0)

    async def pinned(url: str) -> tuple[str, str, dict[str, Any]]:
        raise AssertionError("an address in clear is refused before it is looked up")

    egress = _gateway(_mediator(), pin=pinned, personal_names={"openai"})
    with egress.client as client:
        refused = client.post(CHAT, content=_chat("hello"), headers=HEADERS)
    assert refused.status_code == 400 and "Nothing was sent" in refused.json()["error"]["message"]
    block = turns[-1].decision
    assert type(block) is Block and (block.destination, block.code, block.status) == ("openai", "egress-refused", 400)
    assert "127.0.0.1" not in block.reason and "127.0.0.1" not in repr(block)  # the identity's address: told, not kept
    assert turns[-1].execution == NotAttempted("egress-refused") and egress.counts() == (0, 0)
    _sends_nothing(block)


def test_an_aws_login_that_ended_is_a_block_that_sends_nothing(turns):
    gateway = _bedrock(_mediator(), _Aws(fails=1))
    with gateway.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert answered.status_code == 400 and "aws sso login --profile work" in answered.json()["message"]
    turn = turns[-1]
    block = turn.decision  # no stale Pass kept: the decision is the refusal
    assert type(block) is Block and (block.code, block.status, block.reason) == ("credentials", 400, LOGIN_ENDED)
    assert turn.prepared.block_reason == LOGIN_ENDED  # the record says why in plain words, as before
    assert turn.execution == NotAttempted("credentials") and gateway.counts() == (0, 0)
    _sends_nothing(block)
    # A login that ends between the two attempts: the first went, the second is not sent; the decision stays its own.
    again = _bedrock(_mediator(), _Aws(fails=2))
    with again.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert answered.status_code == 400 and again.counts() == (1, 1)
    assert type(turns[-1].decision) is Forward and turns[-1].execution == Attempted(1, 403)


# --- the transport itself ---------------------------------------------------------------------------------------------


def _exchange(text: str, *, url: str = "https://api.example.test/v1/chat/completions", headers=None):
    target = urlsplit(url)
    anthropic = target.path == "/v1/messages"
    name = "anthropic" if anthropic else "openai"
    upstream = Upstream(
        name, f"/{name}", f"{target.scheme}://{target.netloc}", envelope_expected=frozenset({("model", "m")})
    )
    path = f"/{name}{target.path}"
    given = list(HEADERS.items()) if headers is None else headers
    body = _chat(text)
    envelope = envelope_of(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": target.query.encode(),
            "headers": [(k.encode(), v.encode()) for k, v in given],
        },
        "http",
        name,
        body,
    )
    admission = request_profiles.admit(inventory(envelope), upstream)
    arena = _mediator().prepare_http(
        upstream, "POST", path, dict(given), body, admission.checked, len(admission.inventory.entries), target.query
    )
    decision = _strict(arena, envelope, upstream)
    assert type(decision) is Forward
    return arena, envelope, decision


def test_validate_send_authorizes_each_attempt_once():
    policy = request_policy.current()
    arena, envelope, decision = _exchange("hello")
    first = validate_send(decision, arena, envelope, policy, 1)
    assert isinstance(first, AuthorizedRequest) and first.decision is decision and first.payload is decision.payload
    assert (first.policy, first.credentials, first.attempt, first.ceiling) == (policy, "client", 1, 1)
    assert "hello" not in repr(first)  # what the request carries is never printed
    assert validate_send(decision, arena, envelope, policy, 1) == SendRefusal("spent")  # once
    assert validate_send(decision, arena, envelope, policy, 2) == SendRefusal("attempt")  # a retry is AWS's alone
    signed_arena, signed_envelope, signed = _exchange("signed")
    assert validate_send(signed, signed_arena, signed_envelope, policy, 2, credentials="aws") == SendRefusal("spent")
    assert validate_send(signed, signed_arena, signed_envelope, policy, 0, credentials="aws") == SendRefusal("attempt")
    assert isinstance(
        validate_send(signed, signed_arena, signed_envelope, policy, 1, credentials="aws"), AuthorizedRequest
    )
    retry = validate_send(signed, signed_arena, signed_envelope, policy, 2, credentials="aws")
    assert isinstance(retry, AuthorizedRequest) and retry.ceiling == 2
    assert validate_send(signed, signed_arena, signed_envelope, policy, 3, credentials="aws") == SendRefusal("attempt")
    other_arena, later, other = _exchange("later")
    equal = request_policy.PolicySnapshot()  # equal to the rules in force, but not them
    assert validate_send(other, other_arena, later, equal, 1) == SendRefusal("policy")
    assert isinstance(
        validate_send(other, other_arena, later, policy, 1), AuthorizedRequest
    )  # a refusal spends nothing
    handshake = envelope_of({"type": "websocket", "path": "/r", "headers": []}, "ws-handshake", "relay", connection=1)
    frame = frame_envelope(handshake, "{}")
    framed = outcome.decide(None, frame, payload="{}")
    assert validate_send(framed, None, frame, policy, 1) == SendRefusal("transport")  # the WebSocket adapter's (P1-4)


async def test_a_built_request_is_frozen_and_sent_only_as_built():
    policy = request_policy.current()
    transport = RecordingTransport(lambda request: httpx.Response(200))
    client = RecordingClient(transport)
    url = "https://api.example.test/v1/messages?beta=%74rue"
    headers = [("content-type", "application/json"), ("authorization", "Bearer sk-dummy")]

    def authorized(text: str) -> AuthorizedRequest:
        arena, envelope, decision = _exchange(text, url=url, headers=headers)
        made = validate_send(decision, arena, envelope, policy, 1)
        assert isinstance(made, AuthorizedRequest)
        return made

    one = authorized("rc-text-one")
    built = request_transport.build(one, client, "POST", url, headers)
    assert isinstance(built, BuiltRequest) and built.authorized is one
    final = built.final
    assert (final.method, final.url, final.content) == ("POST", url.encode(), one.payload)
    assert final.headers == tuple(built.request.headers.raw)
    names = [name.lower() for name, _ in final.headers]
    assert b"accept" not in names and b"user-agent" not in names  # httpx's own defaults, which the client did not send
    assert "sk-dummy" not in repr(built) and "rc-text-one" not in repr(built)
    sent = await request_transport.send(client, built)
    assert isinstance(sent, httpx.Response) and sent.status_code == 200
    assert [(s.method, s.raw_path, s.content) for s in transport.sent] == [
        ("POST", b"/v1/messages?beta=%74rue", one.payload)
    ]
    assert await request_transport.send(client, built) == SendRefusal("spent")  # sent once
    assert client.builds == 1 and client.sends == 1
    # Built as nothing it was authorized for: another method, a signature it was not to carry, or by hand.
    two = authorized("two")
    assert request_transport.build(two, client, "PUT", url, headers) == SendRefusal("method")
    signer = (AwsCredentials("AKIARCDUMMY", "rc-dummy-secret"), REGION)
    assert request_transport.build(two, client, "POST", url, headers, signer) == SendRefusal("credentials")
    forged = dataclasses.replace(two)
    assert request_transport.build(forged, client, "POST", url, headers) == SendRefusal("unauthorized")
    assert client.builds == 1
    twice = request_transport.build(two, client, "POST", url, headers)
    assert isinstance(twice, BuiltRequest)
    by_hand = BuiltRequest(two, twice.request, twice.final, twice.request.stream)
    assert await request_transport.send(client, by_hand) == SendRefusal("unbuilt")
    assert client.sends == 1
    decision = cast(Any, outcome.decide(None, _envelope(b"{}")))  # a decision is not an authorization
    assert request_transport.build(decision, client, "POST", url, []) == SendRefusal("unauthorized")
    assert client.builds == 2
    with pytest.raises(EnvelopeRefused, match="envelope-query-unknown"):
        _exchange("unknown carrier", url="https://api.example.test/v1/chat/completions?b=a%2Fc", headers=headers)
    assert client.builds == 2 and client.sends == 1


async def test_one_authorization_is_built_and_sent_once():
    """The review's I-1: an attempt authorized once is built once; a second build of it is ``spent``, and the provider
    receives the decision's payload once."""
    policy = request_policy.current()
    transport = RecordingTransport(lambda request: httpx.Response(200))
    client = RecordingClient(transport)
    url, headers = "https://api.example.test/v1/chat/completions", [("content-type", "application/json")]
    arena, envelope, decision = _exchange("rc-built-once", url=url, headers=headers)
    authorized = validate_send(decision, arena, envelope, policy, 1)
    assert isinstance(authorized, AuthorizedRequest)
    first = request_transport.build(authorized, client, "POST", url, headers)
    again = request_transport.build(authorized, client, "POST", url, headers)
    assert isinstance(first, BuiltRequest) and again == SendRefusal("spent")
    assert isinstance(await request_transport.send(client, first), httpx.Response)
    assert len(transport.sent) == 1 and client.builds == 1


def test_an_attempt_keeps_the_credentials_of_the_first():
    """The review's M-1: a decision's attempts go with one kind of credentials; a second attempt signed by Latet after
    a first sent with the client's own is refused."""
    policy = request_policy.current()
    arena, envelope, decision = _exchange("rc-credentials")
    assert isinstance(validate_send(decision, arena, envelope, policy, 1), AuthorizedRequest)
    assert validate_send(decision, arena, envelope, policy, 2, credentials="aws") == SendRefusal("credentials")
    with pytest.raises(TypeError):
        request_transport.ATTEMPTS["client"] = 2  # type: ignore[index]


async def test_what_httpx_does_at_the_send_is_frozen_too():
    """The review's M-2: a client that follows redirects, a URL with credentials in it, and another client than the one
    that built are refused; the send follows no redirect and adds no authorization whatever the client is set to after
    the build; the TLS name is frozen with the request."""
    policy = request_policy.current()
    url, headers = "https://api.example.test/v1/chat/completions", [("authorization", "Bearer sk-dummy")]

    def authorized(text: str) -> AuthorizedRequest:
        arena, envelope, decision = _exchange(text, url=url, headers=headers)
        made = validate_send(decision, arena, envelope, policy, 1)
        assert isinstance(made, AuthorizedRequest)
        return made

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.example.test":
            return httpx.Response(307, headers={"location": "https://elsewhere.example.test/v1/chat/completions"})
        return httpx.Response(200)

    transport = RecordingTransport(answer)
    following = httpx.AsyncClient(transport=transport, follow_redirects=True, trust_env=False)
    assert request_transport.build(authorized("rc-a"), following, "POST", url, headers) == SendRefusal("redirects")
    client = RecordingClient(transport)
    with_userinfo = "https://user:secret@api.example.test/v1/chat/completions"
    assert request_transport.build(authorized("rc-b"), client, "POST", with_userinfo, headers) == SendRefusal(
        "userinfo"
    )
    assert request_transport.build(
        authorized("rc-mismatched-sni"), client, "POST", url, headers, extensions={"sni_hostname": "a.test"}
    ) == SendRefusal("envelope-destination")
    assert client.builds == 0 and client.sends == 0
    built = request_transport.build(
        authorized("rc-c"), client, "POST", url, headers, extensions={"sni_hostname": "api.example.test"}
    )
    assert isinstance(built, BuiltRequest) and built.final.tls_name == "api.example.test"
    assert "api.example.test" not in repr(built.final)
    other = RecordingClient(transport)
    assert await request_transport.send(other, built) == SendRefusal("client")
    client.follow_redirects = True  # set on the client after the build: the send follows nothing all the same
    client.auth = httpx.BasicAuth("user", "secret")
    answered = await request_transport.send(client, built)
    assert isinstance(answered, httpx.Response) and answered.status_code == 307
    [sent] = transport.sent
    assert sent.host == "api.example.test" and sent.headers == built.final.headers  # no redirect, no Basic added
    await following.aclose()


def test_a_transport_error_is_an_attempt_without_an_answer(turns):
    """The review's M-6: a provider that cannot be reached: one attempt, no status."""
    gateway = _gateway(_mediator())
    gateway.transport.failure = httpx.ConnectError("refused")
    with gateway.client as client:
        answered = client.post(CHAT, content=_chat("hello"), headers=HEADERS)
    assert answered.status_code == 502 and turns[-1].execution == Attempted(1, None)


def test_each_refusal_records_that_nothing_was_attempted(turns, monkeypatch):
    """The review's M-6: a request 0.10's checks refuse, one whose masking failed, one too large: nothing attempted."""
    gateway = _gateway(_mediator())
    with gateway.client as client:
        refused = client.post(CHAT, content=_chat("list the cards", tool=json.dumps({"card": CARD})), headers=HEADERS)
    assert refused.status_code == 400 and turns[-1].execution == NotAttempted("refused")
    mediator = _mediator()

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise MaskingError("the vault is full")

    monkeypatch.setattr(mediator, "prepare", failing)
    with _gateway(mediator).client as client:
        assert client.post(CHAT, content=_chat("hello"), headers=HEADERS).status_code == 400
    assert turns[-1].execution == NotAttempted("masking-failed")
    mediator = _mediator()
    mediator.body_limit = 1024
    inflated = gzip.compress(_chat("x" * 20_000))
    with _gateway(mediator).client as client:
        assert client.post(CHAT, content=inflated, headers={**HEADERS, "content-encoding": "gzip"}).status_code == 413
    assert turns[-1].execution == NotAttempted("too-large") and gateway.counts() == (0, 0)


# --- a provider's cookie (G2 review C-1) -------------------------------------------------------------------------------


class _Cookies(_Provider):
    """A provider whose every answer sets a cookie, as api.openai.com's and chatgpt.com's do through Cloudflare."""

    def _answer(self, status: int, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
        return super()._answer(status, {**headers, "set-cookie": "__cf_bm=rc-cookie; path=/; HttpOnly; Secure"}, body)


@pytest.mark.parametrize("trust_env", [True, False], ids=["shared", "direct"])
async def test_the_gateways_client_keeps_no_cookie_a_provider_sets(trust_env):
    """The clients Latet sends with (``make_client``: the shared one, and a server's ``direct`` one) keep no cookie an
    answer sets, so the build's refusal of an ambient cookie (``request_transport.build``) never meets a provider's,
    and no cookie of one provider's answer is ever sent with a later request."""
    async with forward.make_client(trust_env=trust_env) as client:
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        cookie = "_cfuvid=rc-cookie; path=/; domain=.api.openai.com; HttpOnly; Secure"
        client.cookies.extract_cookies(httpx.Response(200, headers={"set-cookie": cookie}, request=request))
        assert not client.cookies


def test_the_gateway_cookie_test_fails_when_latets_clients_keep_cookies(monkeypatch):
    """G2 re-review N-1 (minors after 0.11, item 14): the gateway-level cookie test below runs on the tests' recording
    clients, which built a jar of their own, so it passed whether or not Latet's clients kept cookies. They now take
    their options from Latet's (``forward.client_options``): with the cookie jar taken out of those options, as a
    regression of ``make_client`` would, the second request is refused as C-1 refused it."""
    real = forward.client_options

    def keeping(**kwargs: Any) -> dict[str, Any]:
        options = real(**kwargs)
        del options["cookies"]  # httpx's default jar: every answer's Set-Cookie kept
        return options

    monkeypatch.setattr(forward, "client_options", keeping)
    gateway = _gateway(_mediator(), _Cookies())
    with gateway.client as client:
        answers = [client.post(CHAT, content=_chat("hello"), headers=HEADERS) for _ in range(2)]
    assert [answer.status_code for answer in answers] == [200, 500] and len(gateway.transport.sent) == 1


def test_a_provider_that_sets_a_cookie_never_stops_the_next_request():
    """G2 C-1: before, the first answer's cookie stayed in the client and every later request of every agent was
    refused (500, ``envelope-client-headers``) until Latet restarted."""
    gateway = _gateway(_mediator(), _Cookies())
    with gateway.client as client:
        answers = [client.post(CHAT, content=_chat("hello"), headers=HEADERS) for _ in range(2)]
    assert [answer.status_code for answer in answers] == [200, 200] and len(gateway.transport.sent) == 2


# --- a coded request body (G2 review I-1) ------------------------------------------------------------------------------

# What a coding can carry besides the document: a value Latet never read, which must never reach the provider.
PLANTED = b"rc-planted john.doe@example.com"


def _raw_deflate(document: bytes) -> bytes:
    deflater = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return deflater.compress(document) + deflater.flush()


def _gzip_fields(document: bytes) -> bytes:
    """One gzip member whose header carries FEXTRA, FNAME and FCOMMENT holding planted bytes, and an MTIME."""
    extra = b"RC" + struct.pack("<H", len(PLANTED)) + PLANTED
    header = b"\x1f\x8b\x08" + bytes([0x04 | 0x08 | 0x10]) + struct.pack("<I", 1_762_000_000) + b"\0\xff"
    header += struct.pack("<H", len(extra)) + extra + b"customer_" + PLANTED + b".json\0" + PLANTED + b"\0"
    return header + _raw_deflate(document) + struct.pack("<II", zlib.crc32(document), len(document))


def _zstd_skippable() -> bytes:
    return struct.pack("<II", 0x184D2A50, len(PLANTED)) + PLANTED


class _Bits:
    """Brotli's bit order (RFC 7932: least significant bit first), for a stream written by hand."""

    def __init__(self) -> None:
        self.out, self.value, self.count = bytearray(), 0, 0

    def put(self, value: int, count: int) -> _Bits:
        for bit in range(count):
            self.value |= ((value >> bit) & 1) << self.count
            self.count += 1
            if self.count == 8:
                self.out.append(self.value)
                self.value = self.count = 0
        return self

    def align(self) -> _Bits:
        if self.count:
            self.out.append(self.value)
            self.value = self.count = 0
        return self

    def raw(self, data: bytes) -> _Bits:
        self.out += data
        return self


def _brotli_metadata(document: bytes) -> bytes:
    """A brotli stream with a metadata meta-block holding the planted bytes (MNIBBLES 0, which a decoder skips), then
    the document in an uncompressed meta-block, then the last, empty one."""
    bits = _Bits().put(0, 1)  # WBITS 16
    bits.put(0, 1).put(3, 2).put(0, 1).put(1, 2).put(len(PLANTED) - 1, 8).align().raw(PLANTED)  # the metadata
    bits.put(0, 1).put(0, 2).put(len(document) - 1, 16).put(1, 1).align().raw(document)  # ISUNCOMPRESSED
    return bytes(bits.put(1, 1).put(1, 1).align().out)


# Each coding Latet decodes, as a client may send it with more than the document in it.
CONTAINERS: dict[str, tuple[str, Callable[[bytes], bytes]]] = {
    "gzip": ("gzip", lambda document: gzip.compress(document, mtime=0)),
    "gzip-header-fields": ("gzip", _gzip_fields),
    "gzip-second-member": ("gzip", lambda document: gzip.compress(document, mtime=0) + _gzip_fields(b"")),
    "deflate-zlib-trailer": ("deflate", lambda document: zlib.compress(document) + PLANTED),
    "deflate-raw-trailer": ("deflate", lambda document: _raw_deflate(document) + PLANTED),
    "zstd-skippable-first": ("zstd", lambda document: _zstd_skippable() + zstandard.compress(document)),
    "zstd-skippable-last": ("zstd", lambda document: zstandard.compress(document) + _zstd_skippable()),
    "br-metadata": ("br", _brotli_metadata),
}


class _Coded(_Provider):
    """A provider that reads a request's body as its content-encoding says."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-amz-security-token") == STALE:
            return super().__call__(request)
        sent = json.loads(codec.decode(request.content, request.headers.get("content-encoding")))
        text = sent["messages"][0]["content"]
        return self._answer(200, {}, {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]})


@pytest.mark.parametrize("container", CONTAINERS)
def test_the_containers_carry_planted_bytes_the_decoder_never_shows(container):
    """The cases below are what the review found: each decodes to the document alone, the planted bytes skipped."""
    coding, make = CONTAINERS[container]
    document = _chat("hello there")
    assert codec.decode(make(document), coding, limit=32 << 20) == document
    assert container == "gzip" or PLANTED in make(document)


@pytest.mark.parametrize("container", CONTAINERS)
def test_a_coded_body_leaves_re_encoded_from_the_document_latet_read(container):
    """G2 I-1, on a route Latet rewrites: a coded body with nothing to mask left as it came, with what its coding
    carried besides the document (data after a deflate stream, gzip's header fields or a second member, a zstd
    skippable frame, a brotli metadata block). It leaves encoded again from the document Latet read, as a masked one."""
    coding, make = CONTAINERS[container]
    document = _chat("hello there")
    gateway = _gateway(_mediator(), _Coded())
    with gateway.client as client:
        answered = client.post(CHAT, content=make(document), headers={**HEADERS, "content-encoding": coding})
    assert answered.status_code == 200 and answered.json()["choices"][0]["message"]["content"] == "hello there"
    [sent] = gateway.transport.sent
    assert PLANTED not in sent.content and b"customer_" not in sent.content
    assert dict((k.lower(), v) for k, v in sent.headers)[b"content-encoding"] == coding.encode()
    decoded = codec.decode(sent.content, coding)
    assert json.loads(decoded) == json.loads(document)
    assert sent.content == codec.encode(decoded, coding)  # Latet's own container of what it read, nothing else


_ANTHROPIC = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1", envelope_expected=frozenset({("model", "m")}))
_CLAUDE_CODE = {
    "x-api-key": "sk-ant-test",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
    "x-claude-code-session-id": "7e57a000-0000-4000-8000-000000000002",
}


@pytest.mark.parametrize("route", ["bedrock", "claude-code"])
@pytest.mark.parametrize("container", CONTAINERS)
def test_a_coded_body_on_a_route_latet_only_checks_is_refused_with_nothing_sent(container, route, turns):
    """G2 I-1, on a route Latet only checks (Bedrock, Vertex, Claude Code: the body leaves byte for byte): a coded body
    left as it came, with what its coding carried. It is refused before anything is sent, under the content-encoding
    header's own code, with words that name the header and say to send the body uncompressed."""
    coding, make = CONTAINERS[container]
    if route == "bedrock":
        gateway = _bedrock(_mediator(), _Aws(), _Coded())
        target, headers = _route(), {"content-type": "application/json"}
        body = _invoke(HELLO)
    else:
        gateway = _gateway(_mediator(), _Coded(), upstreams=[_ANTHROPIC])
        target, headers = "/anthropic/v1/messages", dict(_CLAUDE_CODE)
        body = json.dumps({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": HELLO}]}).encode()
    with gateway.client as client:
        answered = client.post(target, content=make(body), headers={**headers, "content-encoding": coding})
    assert answered.status_code == 400 and gateway.transport.sent == [] and gateway.counts() == (0, 0)
    words = answered.text
    assert "content-encoding" in words and "uncompressed" in words and "Nothing was sent" in words
    assert "rc-planted" not in words
    block = turns[-1].decision
    assert type(block) is Block and (block.code, block.status) == ("envelope-header-value", 400)
    assert turns[-1].execution == NotAttempted("envelope-header-value")


def test_an_identity_coding_on_a_route_latet_only_checks_still_leaves_byte_for_byte():
    """``content-encoding: identity`` names no coding: the body leaves as it came, as before."""
    gateway = _bedrock(_mediator(), _Aws())
    headers = {"content-type": "application/json", "content-encoding": "identity"}
    with gateway.client as client:
        answered = client.post(_route(), content=_invoke(HELLO), headers=headers)
    assert answered.status_code == 200 and gateway.transport.sent[-1].content == _invoke(HELLO)


# --- a missing launch (G2 review M-3) --------------------------------------------------------------------------------


def _bare(path: str, headers: dict[str, str], body: bytes) -> Any:
    """A request as an app with no launch ingress before its forwarder hands it on: no launch, no selection."""
    from starlette.requests import Request

    sent = [False]

    async def receive() -> dict[str, Any]:
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http", "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "server": ("127.0.0.1", 5283), "client": ("127.0.0.1", 50000),
    }  # fmt: skip
    return Request(scope, receive)


async def test_a_forwarder_with_no_launch_ingress_decides_each_request_as_a_claim(tmp_path, monkeypatch):
    """G2 review M-3: a forwarder mounted with no launch ingress before it (neither the computer's launch dispatch nor
    a server's gate) took a missing launch as the isolated preparation seam: prepare_http without a launch decision,
    and the folder the request claims attached with its project's whole scope, its loosenings included (against D7).
    It is decided as the WebSocket relay decides it: ABSENT, a claim, which only tightens."""
    from latet.config import Config, Profile, ProjectEntry
    from latet.engine.pipeline import request_scope
    from latet.gateway.capture import Capture
    from latet.gateway.launch_authority import ABSENT
    from latet.gateway.routes import RouteTable
    from latet.projects import Projects

    folder = tmp_path / "loosened"
    folder.mkdir()
    mediator = _mediator()
    entry = ProjectEntry(path=str(folder.resolve()), profile=Profile.SECRETS)  # its rules mask secrets alone
    mediator.engine.scopes = Projects(Config(ner="off", projects=[entry]), mediator.engine.vault, files=False)
    assert mediator.engine.scopes.resolve(str(folder.resolve())).lowers
    launches: list[Any] = []
    real = mediator.prepare_http

    def preparing(*args: Any, **kwargs: Any) -> Any:
        launches.append(kwargs.get("launch"))
        return real(*args, **kwargs)

    monkeypatch.setattr(mediator, "prepare_http", preparing)
    transport = RecordingTransport(_Provider())
    headers = {**HEADERS, "x-latet-project": str(folder.resolve())}
    async with RecordingClient(transport) as client:
        forwarder = forward.Forwarder(RouteTable([OPENAI]), Capture(None), client, mediator)
        answered = await forwarder.handle(_bare(CHAT, headers, _chat(f"write to {EMAIL}")))
    assert answered.status_code == 200 and launches == [ABSENT]
    [sent] = transport.sent
    assert EMAIL.encode() not in sent.content  # the claimed project's loosening never applied: masked as everywhere
    assert request_scope.get() is None  # the selection it made for the request is its own, gone with it


# --- D4 (b), option B: the open list on a request Latet only checks (minors after 0.11, item 1) ----------------------
#
# On a request Latet only checks (Claude Code's, a subscription's, Bedrock's and Vertex's calls), an anthropic-* header
# no profile names passes once the final check has read it, its decision a Pass naming "checked only"; an
# x-claude-code-* one is taken off and counted. A request Latet rewrites (an Anthropic SDK call) is refused exactly as
# in 0.11.1, before anything of it is masked or learnt.

# The headers a Claude Code release could add before Latet names them: two to pass checked, one to take off.
_OPEN = {"anthropic-new-capability": "on", "anthropic-workspace-id": "wrkspc_01AbCdEfGh", "x-claude-code-new-hint": "1"}
_VERTEX = "/vertex/v1/projects/p/locations/us-east5/publishers/anthropic/models/m:rawPredict"


def _message(request: httpx.Request) -> httpx.Response:
    """An Anthropic message carrying the first text the request did, or an empty model list for a model list."""
    answer: dict[str, Any] = {"data": [], "has_more": False}
    if request.method != "GET":
        content = json.loads(request.content)["messages"][0]["content"]
        answer = {
            "id": "msg_rc", "type": "message", "role": "assistant", "model": "m", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": content}], "usage": {"input_tokens": 1, "output_tokens": 1},
        }  # fmt: skip
    stream = _Answer(json.dumps(answer).encode())
    return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)


def _checked_call(route: str, mediator: Mediator, recorder: Any, text: str = HELLO) -> tuple[Any, ...]:
    """(gateway, target, headers, body) of a request Latet only checks: Claude Code's on Anthropic's route (its session
    header), a Bedrock call (signed, on its profile's route) or a Vertex Claude call; each body one its profile names
    whole, so that nothing of it passes by a rule."""
    from latet.gateway.cloud import VertexUpstream

    messages = [{"role": "user", "content": text}]
    if route == "bedrock":
        return (
            _bedrock(mediator, _Aws(), _message, recorder=recorder),
            _route(),
            {"content-type": "application/json"},
            (_invoke(text)),
        )
    if route == "vertex":
        vertex = VertexUpstream(envelope_expected=frozenset({("model", "m"), ("project", "p")}))
        body = {"anthropic_version": "vertex-2023-10-16", "max_tokens": 5, "messages": messages}
        gateway = _gateway(mediator, _message, upstreams=[vertex], recorder=recorder)
        return (
            gateway,
            _VERTEX,
            {"content-type": "application/json", "authorization": "Bearer ya29.fake"},
            (json.dumps(body).encode()),
        )
    body = {"model": "m", "max_tokens": 5, "messages": messages}
    gateway = _gateway(mediator, _message, upstreams=[_ANTHROPIC], recorder=recorder)
    return gateway, "/anthropic/v1/messages", dict(_CLAUDE_CODE), json.dumps(body).encode()


def _lowered(sent: Any) -> list[tuple[bytes, bytes]]:
    return [(name.lower(), value) for name, value in sent.headers]


def _checked_sender(route: str, mediator: Mediator, recorder: Any = None) -> tuple[Any, Any]:
    """(gateway, send) of a request Latet only checks: ``_checked_call``'s three routes, or Claude Code's model list
    (bodyless, its session header). ``send(client, headers)`` sends it with those headers added."""
    if route == "a model list":
        gateway = _gateway(mediator, _message, upstreams=[_ANTHROPIC], recorder=recorder)
        listed = {k: v for k, v in _CLAUDE_CODE.items() if k != "content-type"}
        return gateway, lambda client, more: client.get("/anthropic/v1/models", headers={**listed, **more})
    gateway, target, headers, body = _checked_call(route, mediator, recorder)
    return gateway, lambda client, more: client.post(target, content=body, headers={**headers, **more})


@pytest.mark.parametrize("route", ["claude-code", "bedrock", "vertex"])
def test_a_new_anthropic_header_passes_checked_on_a_request_latet_only_checks(route, turns):
    """Claude Code keeps working on a release that adds a header before Latet names it: the provider gets each new
    anthropic-* header byte for byte, never the new x-claude-code-* one; the body leaves as it came; the decision is a
    Pass naming "checked only" and the history says so, its removed header counted. The same request without them is a
    Forward that names no rule."""
    recorder = _Recorder()
    gateway, target, headers, body = _checked_call(route, _mediator(), recorder)
    with gateway.client as client:
        control = client.post(target, content=body, headers=headers)
        answered = client.post(target, content=body, headers={**headers, **_OPEN})
    assert control.status_code == 200 and type(turns[0].decision) is Forward, control.text
    assert answered.status_code == 200, answered.text
    _, sent = gateway.transport.sent
    assert (b"anthropic-new-capability", b"on") in _lowered(sent)
    assert (b"anthropic-workspace-id", b"wrkspc_01AbCdEfGh") in _lowered(sent)
    assert b"x-claude-code-new-hint" not in {name for name, _ in _lowered(sent)}
    assert sent.content == body
    decision = turns[-1].decision
    assert type(decision) is Pass and decision.evidence.rules == ("checked only",)
    assert forward.decision_passes(decision) == ("checked-only",)
    first, record = recorder.finished
    assert first.passes == () and first.removed_headers == 0
    assert record.passes == ("checked-only",) and record.removed_headers == 1 and record.block_code is None


def test_a_new_anthropic_header_on_a_request_latet_rewrites_is_refused_as_in_0_11_1(turns):
    """An Anthropic SDK call (no Claude Code marker, an API key) is one Latet rewrites: the open list never reaches it.
    It is refused naming the first new header, nothing sent and nothing of it masked or learnt (the vault knows no more
    values than before), recorded as 0.11.1 recorded it: as the same request refused at its envelope's admission."""
    mediator = _mediator()
    recorder = _Recorder()
    gateway = _gateway(mediator, _message, upstreams=[_ANTHROPIC], recorder=recorder)
    headers = {k: v for k, v in _CLAUDE_CODE.items() if k != "x-claude-code-session-id"}
    body = json.dumps({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": f"to {EMAIL}"}]})
    values = mediator.engine.vault.count
    with gateway.client as client:
        answered = client.post("/anthropic/v1/messages", content=body.encode(), headers={**headers, **_OPEN})
    assert answered.status_code == 400, answered.text
    words = answered.json()["error"]["message"]
    assert "request envelope refused (envelope-header-unknown): header anthropic-new-capability" in words
    assert "Nothing was sent" in words and "wrkspc_01AbCdEfGh" not in words
    assert gateway.transport.sent == [] and gateway.counts() == (0, 0)
    assert mediator.engine.vault.count == values, "nothing of it masked or learnt"
    block = turns[-1].decision
    assert type(block) is Block and (block.code, block.status) == ("envelope-header-unknown", 400)
    assert turns[-1].execution == NotAttempted("envelope-header-unknown")
    # 0.11.1's record of it: the same request refused at its envelope's admission, for a header no rule names.
    twin_recorder = _Recorder()
    twin = _gateway(_mediator(), _message, upstreams=[_ANTHROPIC], recorder=twin_recorder)
    with twin.client as client:
        refused = client.post("/anthropic/v1/messages", content=body.encode(), headers={**headers, "x-team": "on"})
    assert refused.status_code == 400 and "header x-team" in refused.text
    [record], [admitted] = recorder.finished, twin_recorder.finished
    assert record.block_code == "envelope-header-unknown" and record.history_code == 28 and record.forwarded is None
    assert _row(record) == _row(admitted)


@pytest.mark.parametrize("held", ["a key", "a value the vault knows", "a card number", "an email"])
@pytest.mark.parametrize("route", ["claude-code", "bedrock"])
def test_a_sensitive_value_in_a_new_anthropic_header_is_still_refused(route, held, turns):
    """The final check reads what the open list lets through: a secret (a fake key, written in two pieces), a value the
    vault knows (learnt, never detected on its own) or a detected personal value in a new anthropic-* header refuses
    the request, envelope-sensitive, nothing sent, the value nowhere in the answer."""
    from latet.engine.types import EntityClass
    from latet.engine.vault import Entity

    mediator = _mediator()
    known = "acme-payroll-" + "7731"
    vault = mediator.engine.vault
    vault.learn(
        Entity(vault.entity_id("API_KEY", known), "API_KEY", EntityClass.SECRET, known, "fk-" + "1" * 32, 0, 0.0)
    )
    value = {
        "a key": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        "a value the vault knows": known,
        "a card number": "4111 1111 1111 1111",
        "an email": EMAIL,
    }[held]
    gateway, target, headers, body = _checked_call(route, mediator, None)
    with gateway.client as client:
        answered = client.post(target, content=body, headers={**headers, "anthropic-workspace-id": value})
    assert answered.status_code == 400, answered.text
    assert "request envelope refused (envelope-sensitive)" in answered.text and value not in answered.text
    assert gateway.transport.sent == []
    assert type(turns[-1].decision) is Block and turns[-1].decision.code == "envelope-sensitive"


@pytest.mark.parametrize("held", ["a value the vault knows", "a card number", "a key's form"])
@pytest.mark.parametrize("route", ["claude-code", "bedrock", "vertex", "a model list"])
def test_a_sensitive_value_in_a_new_anthropic_header_s_name_is_refused(route, held, turns):
    """The review of Task 3, I-1: a new anthropic-* header's name is the client's own text, which no profile chose, so
    the final check reads it beside its value, as D6 reads a new member's key (R-81). A value the vault knows, a card
    number or a key's form in the name refuses the request, envelope-sensitive, nothing sent, on Claude Code's model
    list (bodyless) as on a call with a body; before, the name left unread, the decision a Pass naming "checked only".
    A name that holds nothing still passes. (The re-review of Task 3, M-a: the model list was refused already, and is
    pinned here. Its first key's form, a Databricks key of 36 characters without a dash, is refused by the name's bound
    before the check reads it since N-1, and is pinned by the test of that bound; the key's form here, a Slack bot
    token's, has words of 12 at most, so the final check is what finds it: the scoped check of N-1, P-2.)"""
    from latet.engine.types import EntityClass
    from latet.engine.vault import Entity

    mediator = _mediator()
    known = "acme-payroll-" + "7731"
    vault = mediator.engine.vault
    vault.learn(
        Entity(vault.entity_id("API_KEY", known), "API_KEY", EntityClass.SECRET, known, "fk-" + "1" * 32, 0, 0.0)
    )
    held_value = {
        "a value the vault knows": known,
        "a card number": "4111" + "1" * 12,
        "a key's form": "xoxb-" + "1234567890-" + "1234567890-" + "abcdefghijkl",  # gitleaks slack-bot-token
    }[held]
    name = "anthropic-" + held_value
    gateway, send = _checked_sender(route, mediator)
    with gateway.client as client:
        answered = send(client, {name: "1"})
        clean = send(client, {"anthropic-new-capability": "1"})
    assert answered.status_code == 400, answered.text
    assert "request envelope refused (envelope-sensitive)" in answered.text and name not in answered.text
    assert type(turns[0].decision) is Block and turns[0].decision.code == "envelope-sensitive"
    assert clean.status_code == 200, clean.text
    [sent] = gateway.transport.sent  # the clean one alone
    assert name.encode() not in {header for header, _ in _lowered(sent)}


@pytest.mark.parametrize("held", ["an IBAN", "a vault value in capitals", "a vault value with each word capitalised"])
@pytest.mark.parametrize("route", ["claude-code", "bedrock", "vertex", "a model list"])
def test_a_value_with_capitals_in_a_new_anthropic_header_s_name_is_refused(route, held, turns):
    """The re-review of Task 3, I-A: a header's name arrives in lowercase (the server lowers it, whatever the client
    wrote), and an IBAN or a value the vault learnt with capitals is found only with its capitals. The final check reads
    an open header's name in lowercase, in capitals and with each word capitalised, so each of them in the name refuses
    the request, envelope-sensitive, nothing sent, on a model list as on a call with a body; before, the name was read
    in lowercase alone and the request passed, the value sent in the name. Those forms are only texts for the check to
    read: a name with capitals that holds nothing passes, sent in lowercase as it arrived. (Since N-1 no word of such a
    name is longer than 16 characters, so the IBAN here is Belgium's, of 16; an IBAN of 27 and an AWS key id's form, of
    20, are refused by that bound before the check reads them, pinned by the test of that bound.)"""
    from latet.engine.types import EntityClass
    from latet.engine.vault import Entity

    mediator = _mediator()
    upper, title = "CUST-PAYROLL-" + "7731", "Acme-Payroll-" + "7731"
    vault = mediator.engine.vault
    for known, etype, eclass, fake in (
        (upper, "API_KEY", EntityClass.SECRET, "fk-" + "2" * 32),
        (title, "CUSTOM_TERM", EntityClass.CUSTOM, "Term-Fake-3333"),  # a customer's code, learnt as it is written
    ):
        vault.learn(Entity(vault.entity_id(etype, known), etype, eclass, known, fake, 0, 0.0))
    held_value = {
        "an IBAN": "BE68" + "539007547034",  # found only in capitals (pii:iban)
        "a vault value in capitals": upper,
        "a vault value with each word capitalised": title,
    }[held]
    name = "anthropic-" + held_value
    gateway, send = _checked_sender(route, mediator)
    with gateway.client as client:
        answered = send(client, {name: "1"})
        clean = send(client, {"Anthropic-New-Capability": "1"})
    assert answered.status_code == 400, answered.text
    assert "request envelope refused (envelope-sensitive)" in answered.text
    assert held_value not in answered.text and held_value.lower() not in answered.text
    assert type(turns[0].decision) is Block and turns[0].decision.code == "envelope-sensitive"
    assert clean.status_code == 200, clean.text
    [sent] = gateway.transport.sent  # the clean one alone
    assert (b"anthropic-new-capability", b"1") in _lowered(sent)
    assert name.lower().encode() not in {header for header, _ in _lowered(sent)}


@pytest.mark.parametrize(
    "held",
    [
        "a Google API key's form",
        "a word of 17 characters",
        "an IBAN of 27 characters",
        "an AWS key id's form",
        "a Databricks key's form",
    ],
)
@pytest.mark.parametrize("route", ["claude-code", "bedrock", "vertex", "a model list"])
def test_a_word_longer_than_16_characters_in_a_new_anthropic_header_s_name_is_refused_as_in_0_11_1(route, held, turns):
    """The scoped check of Task 3's second fix round, N-1: the final check reads a new anthropic-* header's name in
    lowercase, in capitals and with each word capitalised, so a secret whose form mixes capitals (a Google API key,
    AIza and 35 characters, written in pieces) left in the lowered name, where the same text as a value is refused. No
    word of such a name, between its dashes, is longer than 16 characters now (the longest word Anthropic's headers
    use is organization, 12): a name with a longer word is not in the open list, refused as in 0.11.1,
    envelope-header-unknown naming the header to the agent alone, nothing sent and its record that of any header no
    profile names, on a model list as on a call with a body. A key, a token, an IBAN or a long id runs longer than 16
    without a dash: the IBAN, the AWS key id's form and the Databricks key's form I-1's and I-A's tests held in a name
    are refused so now, before the check reads them. A name whose words have 16 characters still passes checked. (The
    scoped check of N-1, P-1: the agent is told the header with each word longer than 16 characters given by its
    length, so the key's form it held never comes back in the answer.)"""
    value, shown = {
        "a Google API key's form": ("AIza" + "SyQ7k2Lm9Np4Rs6Tv8Wx0Yz3Ab5Cd7Ef9G1", "anthropic-<39 characters>"),
        "a word of 17 characters": ("new-" + "capability" + "1234567", "anthropic-new-<17 characters>"),
        "an IBAN of 27 characters": ("IT60X0542811101000000123456", "anthropic-<27 characters>"),
        "an AWS key id's form": ("AKIA" + "ZQ7XK2MV4PL3NB6W", "anthropic-<20 characters>"),
        "a Databricks key's form": ("dapi" + "3f9a1c7e5b2d8046" + "a1b2c3d4e5f60718", "anthropic-<36 characters>"),
    }[held]
    name, fits = "anthropic-" + value, "anthropic-" + "a" * 16 + "-" + "b" * 16
    recorder, twin_recorder = _Recorder(), _Recorder()
    gateway, send = _checked_sender(route, _mediator(), recorder)
    twin, twin_send = _checked_sender(route, _mediator(), twin_recorder)
    with gateway.client as client:
        answered = send(client, {name: "1"})
        passed = send(client, {fits: "1"})
    with twin.client as client:
        refused = twin_send(client, {"x-team": "1"})  # 0.11.1's refusal of a header no profile names
    assert answered.status_code == 400, answered.text
    assert f"request envelope refused (envelope-header-unknown): header {shown}." in answered.text
    long = max(value.split("-"), key=len)
    assert long not in answered.text and long.lower() not in answered.text
    assert "Nothing was sent" in answered.text
    block = turns[0].decision
    assert type(block) is Block and (block.code, block.status) == ("envelope-header-unknown", 400)
    assert turns[0].execution == NotAttempted("envelope-header-unknown")
    assert refused.status_code == 400 and "(envelope-header-unknown): header x-team" in refused.text
    assert _row(recorder.finished[0]) == _row(twin_recorder.finished[0]), "recorded as 0.11.1 recorded it"
    assert passed.status_code == 200, passed.text
    [sent] = gateway.transport.sent  # the name that fits alone
    assert (fits.encode(), b"1") in _lowered(sent)
    decision = turns[1].decision
    assert type(decision) is Pass and decision.evidence.rules == ("checked only",)


@pytest.mark.parametrize("route", ["claude-code", "bedrock", "vertex", "a model list", "an SDK"])
def test_a_refused_header_s_word_longer_than_16_characters_is_named_by_its_length(route, turns, caplog):
    """The scoped check of N-1, P-1 (root's ruling, wider than the check proposed): a header no profile names is told to
    the agent alone, by name, and a client may put that answer into its conversation, so that a key's form in the name
    would come back, lowercased, in a later request's body, where the check cannot find it. Every word of a refused
    header's name (between its dashes) longer than 16 characters is told by its length, on any unknown header, not only
    a new anthropic-* one: x-custom-<20 characters>. A name whose words are 16 or shorter is told as before. Nothing
    else changes: nothing sent, the log keeps the category alone, and the history's row is the one a short name has."""
    import logging

    token = "tk" + "7Qx2Lm9Np4Rs6Tv8Wx"  # 20 characters
    long, short = "x-custom-" + token, "x-custom-" + "a" * 16
    recorder, twin_recorder = _Recorder(), _Recorder()
    if route == "an SDK":  # a request Latet rewrites: an Anthropic SDK call, no Claude Code marker
        sdk = {k: v for k, v in _CLAUDE_CODE.items() if k != "x-claude-code-session-id"}
        body = json.dumps({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": HELLO}]}).encode()

        def sender(recorder: Any) -> tuple[Any, Any]:
            gateway = _gateway(_mediator(), _message, upstreams=[_ANTHROPIC], recorder=recorder)
            return gateway, lambda client, more: client.post(
                "/anthropic/v1/messages", content=body, headers={**sdk, **more}
            )

        (gateway, send), (twin, twin_send) = sender(recorder), sender(twin_recorder)
    else:
        gateway, send = _checked_sender(route, _mediator(), recorder)
        twin, twin_send = _checked_sender(route, _mediator(), twin_recorder)
    with caplog.at_level(logging.DEBUG), gateway.client as client:
        answered = send(client, {long: "1"})
    with twin.client as client:
        named = twin_send(client, {short: "1"})
    assert answered.status_code == 400, answered.text
    assert "request envelope refused (envelope-header-unknown): header x-custom-<20 characters>." in answered.text
    assert token not in answered.text and token.lower() not in answered.text
    assert named.status_code == 400 and f"(envelope-header-unknown): header {short}." in named.text
    assert gateway.transport.sent == [] and twin.transport.sent == []
    assert token.lower() not in caplog.text and "envelope-header-unknown" in caplog.text
    assert turns[0].decision.code == "envelope-header-unknown" == turns[1].decision.code
    assert _row(recorder.finished[0]) == _row(twin_recorder.finished[0]), "the history holds no name"


@pytest.mark.parametrize(("client", "passes"), [("claude-code", True), ("subscription", True), ("an SDK", False)])
def test_a_model_list_takes_the_open_list_by_the_same_test_as_a_call_with_a_body(client, passes, turns):
    """A bodyless call (a model list) is one Latet only checks by its headers alone: Claude Code's (its session header)
    or a subscription's passes a new anthropic-* header checked, a Pass naming "checked only"; an SDK's is refused
    naming it, nothing sent."""
    recorder = _Recorder()
    gateway = _gateway(_mediator(), _message, upstreams=[_ANTHROPIC], recorder=recorder)
    headers = {
        "claude-code": {"x-api-key": "sk-ant-test", "x-claude-code-session-id": "7e57a000-0000-4000-8000-000000000002"},
        "subscription": {"authorization": "Bearer sk-ant-oat01-" + "fixture"},
        "an SDK": {"x-api-key": "sk-ant-test"},
    }[client]
    with gateway.client as http:
        answered = http.get("/anthropic/v1/models", headers={**headers, "anthropic-version": "2023-06-01", **_OPEN})
    if not passes:
        assert answered.status_code == 400, answered.text
        assert "(envelope-header-unknown): header anthropic-new-capability" in answered.text
        assert gateway.transport.sent == [] and turns[-1].decision.code == "envelope-header-unknown"
        return
    assert answered.status_code == 200, answered.text
    [sent] = gateway.transport.sent
    assert (b"anthropic-workspace-id", b"wrkspc_01AbCdEfGh") in _lowered(sent)
    assert b"x-claude-code-new-hint" not in {name for name, _ in _lowered(sent)}
    decision = turns[-1].decision
    assert type(decision) is Pass and decision.evidence.rules == ("checked only",) and not decision.evidence.exceptions
    [record] = recorder.finished
    assert record.passes == ("checked-only",) and record.removed_headers == 1
    # The review of Task 3, M-1: every part of it accounted for, as the same list with no new header is (a Forward,
    # 21): the history's coverage capsule of a Pass (25), so that the dashboard says "checked only", never "unknown".
    assert record.history_code == 25 == forward.history_coverage(decision)
    from latet.storage.exchanges import history_coverage_summary

    assert (
        history_coverage_summary({"id": "e" * 32}, record.history_code, "e" * 32)["coverage"] == "complete-accounting"
    )


def test_a_model_list_let_through_by_an_exception_alone_has_the_coverage_capsule_of_a_pass(turns):
    """The review of Task 3, M-1, its other case (the re-review's M-b): Claude Code's model list carrying a header an
    approved exception names, and nothing the open list passes, is a Pass naming the exception alone, its envelope
    proved and its body empty. Its history code is the coverage capsule of a Pass (25, complete-accounting), which the
    dashboard shows as passed by an exception; in 0.11.1, before M-1, the same call was coded 0 and read as unknown."""
    from latet.storage.exchanges import history_coverage_summary

    exception = _anthropic_exception("x-team", role="checked", profile="anthropic.models.1")
    recorder = _Recorder()
    gateway = _gateway(
        _mediator(), _message, upstreams=[_ANTHROPIC], recorder=recorder, request_exceptions=(exception,)
    )
    headers = {k: v for k, v in _CLAUDE_CODE.items() if k != "content-type"}
    with gateway.client as http:
        answered = http.get("/anthropic/v1/models", headers={**headers, "x-team": "blue"})
    assert answered.status_code == 200, answered.text
    [sent] = gateway.transport.sent
    assert (b"x-team", b"blue") in _lowered(sent)
    decision = turns[-1].decision
    assert type(decision) is Pass and decision.evidence.exceptions and decision.evidence.rules == ()
    [record] = recorder.finished
    assert record.passes == ("exception",) and record.removed_headers == 0
    assert record.history_code == 25 == forward.history_coverage(decision)
    assert (
        history_coverage_summary({"id": "e" * 32}, record.history_code, "e" * 32)["coverage"] == "complete-accounting"
    )


def _anthropic_exception(name: str, role: str = "credential", **extra: Any) -> Any:
    from datetime import UTC, datetime, timedelta

    from latet.config import RequestException

    return RequestException.model_validate(
        {
            "destination": "anthropic",
            "profile": "anthropic.messages.1",
            "components": [{"kind": "header", "name": name, "role": role}],
            "reason": "destination-credential" if role == "credential" else "custom-header",
            "id": "x0123456789",
            "granted": datetime.now(UTC) - timedelta(days=1),
            "revision": request_profiles.REVISION,
            **extra,
        }
    )


@pytest.mark.parametrize("row", ["another project's", "one that ended"])
def test_an_exception_naming_a_new_header_decides_it_even_where_it_does_not_apply(row, turns, tmp_path):
    """The review of Task 3, M-2 (root's ruling: kept, failing closed): an approved exception that names a new
    anthropic-* header takes it out of the open list wherever the exception is, even where it does not apply. On a
    request of no project of its, or once it has ended, the header is refused as it was before the open list
    (``envelope-header-unknown`` naming it; ``envelope-exception-expired`` naming the exception and the day it ended),
    nothing sent: never read and sent by the open list in the exception's place, as a credential's value would then be.
    With no exception at all, the same request passes checked."""
    from datetime import UTC, datetime, timedelta

    extra = (
        {"project": str(tmp_path)}
        if row == "another project's"
        else {"granted": datetime.now(UTC) - timedelta(days=31), "days": 30}
    )
    exception = _anthropic_exception("anthropic-workspace-id", **extra)
    body = json.dumps({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": HELLO}]}).encode()
    headers = {**_CLAUDE_CODE, "anthropic-workspace-id": "wrkspc_01AbCdEfGh"}
    gateway = _gateway(_mediator(), _message, upstreams=[_ANTHROPIC], request_exceptions=(exception,))
    with gateway.client as client:
        answered = client.post("/anthropic/v1/messages", content=body, headers=headers)
    assert answered.status_code == 400, answered.text
    if row == "another project's":
        assert "(envelope-header-unknown): header anthropic-workspace-id" in answered.text
        assert turns[-1].decision.code == "envelope-header-unknown"
    else:
        assert "(envelope-exception-expired): exception x0123456789 expired on" in answered.text
        assert turns[-1].decision.code == "envelope-exception-expired"
    assert gateway.transport.sent == [] and "wrkspc_01AbCdEfGh" not in answered.text
    bare = _gateway(_mediator(), _message, upstreams=[_ANTHROPIC])
    with bare.client as client:
        assert client.post("/anthropic/v1/messages", content=body, headers=headers).status_code == 200
    assert type(turns[-1].decision) is Pass and turns[-1].decision.evidence.rules == ("checked only",)


def test_an_exception_added_on_another_instance_applies_to_a_header_the_open_list_would_pass(turns):
    """RC-REC-3: a request whose envelope lets a new anthropic-* header through by the open list reads the settings in
    force first, as one refused for an unknown header does, so that an exception added on another instance meanwhile
    applies (exceptions apply first): here a destination credential, sent unread, the Pass naming the exception."""
    from datetime import UTC, datetime

    holder: dict[str, _Gateway] = {}
    reads: list[int] = []

    async def fresh() -> tuple[datetime, bool]:
        reads.append(1)
        if len(reads) == 1:  # another instance approved the exception: these settings are behind PostgreSQL's
            config = holder["gateway"].config
            assert config is not None and config.routes is not None
            config.routes.use([_ANTHROPIC], exceptions=(_anthropic_exception("anthropic-workspace-id"),))
            return datetime.now(UTC), True
        return datetime.now(UTC), False

    recorder = _Recorder()
    gateway = holder["gateway"] = _gateway(
        _mediator(), _message, upstreams=[_ANTHROPIC], recorder=recorder, exception_fresh=fresh
    )
    body = json.dumps({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": HELLO}]}).encode()
    headers = {**_CLAUDE_CODE, "anthropic-workspace-id": "wrkspc_01AbCdEfGh"}
    with gateway.client as client:
        answered = client.post("/anthropic/v1/messages", content=body, headers=headers)
    assert answered.status_code == 200, answered.text
    assert reads, "the settings were never read again"
    decision = turns[-1].decision
    assert type(decision) is Pass and decision.evidence.exceptions == ("x0123456789",)
    assert decision.evidence.rules == (), "the exception applied, never the open list"
    [record] = recorder.finished
    assert record.passes == ("exception",)
    assert (b"anthropic-workspace-id", b"wrkspc_01AbCdEfGh") in _lowered(gateway.transport.sent[0])


@pytest.mark.parametrize("client", ["claude-code", "an SDK"])
def test_a_header_left_to_the_open_list_by_an_exception_revoked_before_its_decision_is_refused_naming_it(client, turns):
    """``Forwarder._judged``'s second reading is held to the open list's terms: a header an exception let through at
    admission, the exception revoked on another instance before the decision, is never sent by the open list instead
    (its value was not read as the open list reads it: a credential's never is). It is refused as it was before the
    exception, naming it, nothing sent; on a request Latet rewrites as on one it only checks."""
    from datetime import UTC, datetime

    holder: dict[str, _Gateway] = {}
    reads: list[int] = []

    async def fresh() -> tuple[datetime, bool]:
        reads.append(1)
        if len(reads) == 2:  # the reading at the decision: another instance revoked the exception
            config = holder["gateway"].config
            assert config is not None and config.routes is not None
            config.routes.use([_ANTHROPIC])
            return datetime.now(UTC), True
        return datetime.now(UTC), False

    exception = _anthropic_exception("anthropic-workspace-id")
    gateway = holder["gateway"] = _gateway(
        _mediator(), _message, upstreams=[_ANTHROPIC], request_exceptions=(exception,), exception_fresh=fresh
    )
    headers = {**_CLAUDE_CODE, "anthropic-workspace-id": "wrkspc_01AbCdEfGh"}
    if client == "an SDK":
        del headers["x-claude-code-session-id"]
    body = json.dumps({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": HELLO}]}).encode()
    with gateway.client as http:
        answered = http.post("/anthropic/v1/messages", content=body, headers=headers)
    assert len(reads) == 2
    assert answered.status_code == 400, answered.text
    assert "(envelope-header-unknown): header anthropic-workspace-id" in answered.text
    assert gateway.transport.sent == [] and gateway.counts() == (0, 0)
    assert turns[-1].decision.code == "envelope-header-unknown"
