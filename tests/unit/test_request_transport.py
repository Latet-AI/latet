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
    prepared again under the whole settings and sent."""
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
        elif len(preparations) == 2:  # prepared again inside that window: now the rest of the settings lands
            resume.set()
            applying[0].join(10)
        return prepared

    monkeypatch.setattr(runtime_module, "image_policy", held_halfway)
    monkeypatch.setattr(Mediator, "prepare_http", prepare_http)
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
    assert runtime.mediator.dump_guard == 3 and len(preparations) == 3, guards  # before, in the window, after
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
