"""What the dashboard shows of a request's coverage (P2-7), recorded on its exchange by fixed categories alone: the rules
and exceptions a ``Pass`` rested on (``passes``), its envelope's request profile (``profile``), the headers and body
members its profile removed (``removed``, D4) and its agent's version by a release number's grammar (what `latet doctor`
names a launcher by, R-120). Never a value, a rule's argument or an exception's id."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from latet.gateway import request_policy
from latet.gateway.app import GatewayConfig, create_app
from latet.storage.exchanges import safe_summary

from .test_request_leaks import _Answer
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _TIMEOUT, _sinks

_TOKEN = "history-passes-bridge-token"
_ANSWER = b'{"choices":[{"index":0,"message":{"role":"assistant","content":"ordinary provider reply"}}]}'


@pytest.mark.parametrize("category", sorted(request_policy.PASSES))
def test_the_summary_keeps_each_pass_category(category):
    assert safe_summary({"passes": [category, category]})["passes"] == [category]


def test_every_built_in_rule_has_a_category():
    assert set(request_policy.PASS_CATEGORIES) == request_policy.BUILT_IN
    assert "exception" in request_policy.PASSES


@pytest.mark.parametrize(
    "given",
    [["exception x0123456789"], ["checked only"], ["provider encrypted field (1)"], "checked-only", [1], [None], {}],
)
def test_anything_else_is_no_pass_category(given):
    assert safe_summary({"passes": given})["passes"] == []


@pytest.mark.parametrize("profile", ["anthropic.messages.1", "openai.models.1", "vertex.anthropic.rawPredict.1"])
def test_the_summary_keeps_a_profile_id(profile):
    assert safe_summary({"profile": profile})["profile"] == profile


@pytest.mark.parametrize("given", ["/v1/messages", "anthropic", "Bearer sk-ant-api03-x", "a" * 200, 3, None])
def test_anything_else_is_no_profile(given):
    assert safe_summary({"profile": given})["profile"] is None


def test_the_removed_counts_are_numbers_alone():
    assert safe_summary({"removed": {"headers": 2, "members": 1}})["removed"] == {"headers": 2, "members": 1}
    assert safe_summary({"removed": {"headers": "x-stainless-os", "members": -1}})["removed"] == {
        "headers": 0,
        "members": 0,
    }
    assert safe_summary({})["removed"] == {"headers": 0, "members": 0}


@pytest.mark.parametrize("version", ["2.1.312", "0.162.0", "0.162.0-alpha.3", "1.0.103"])
def test_an_agent_version_is_kept_by_its_grammar(version):
    assert safe_summary({"agent_version": version})["agent_version"] == version


@pytest.mark.parametrize(
    "given",
    [
        "P25_PRIVATE_CATEGORY_39127",
        "2.1.312 (emily's laptop)",
        "latest",
        "v2",
        "/home/emily",
        2,
        None,
        "1.2.3-AKIAIOSFODNN7EXAMPLE",  # a short opaque token after a release number (review Minor 4)
        "1.2.3+build.abcdef0123456789",
    ],
)
def test_anything_else_is_no_agent_version(given):
    assert safe_summary({"agent_version": given})["agent_version"] is None


async def test_a_request_records_its_profile_and_the_headers_its_profile_removed(tmp_path):
    """A Chat Completions request through the real local gateway, recorder and store, carrying a tracing header its
    profile declares removable: the history names the profile and counts the header, never its value."""
    sent: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Answer(_ANSWER))

    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "ordinary request"}]}).encode()
    with _sinks(tmp_path) as (mediator, recorder, store):
        app = create_app(
            GatewayConfig(
                mediator=mediator,
                recorder=recorder,
                bridge_token=_TOKEN,
                fixed_routes=[OPENAI],
                client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
            )
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client,
        ):
            response = await asyncio.wait_for(
                client.post(
                    "/openai/v1/chat/completions",
                    headers={"content-type": "application/json", "traceparent": "00-trace-value-77-01"},
                    content=body,
                ),
                _TIMEOUT,
            )
            assert response.status_code == 200, response.text
        assert recorder.drain(_TIMEOUT)
        [row] = store.list()[0]
        detail = store.detail(row["id"])
    assert len(sent) == 1 and "traceparent" not in sent[0].headers
    assert row["profile"] == "openai.chat.1"
    assert row["removed"] == {"headers": 1, "members": 0}
    assert row["passes"] == []  # a Forward: every part read, no rule needed
    assert "trace-value-77" not in json.dumps((row, detail))


async def test_an_unknown_body_member_is_refused_under_its_own_code_naming_its_category(tmp_path):
    """Root's ruling on P2-7's concern 5: a member no profile names in a body Latet rewrites is refused under a safe
    reason code of its own, ``body-unknown-member``, the history keeping that code and the agent told the category of
    the object it was in, never its name's value or what it held."""
    sent: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Answer(_ANSWER))

    body = json.dumps(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "ordinary request"}],
            "zz_unknown": {"note": "SECRET-VALUE-4471"},
        }
    ).encode()
    with _sinks(tmp_path) as (mediator, recorder, store):
        app = create_app(
            GatewayConfig(
                mediator=mediator,
                recorder=recorder,
                bridge_token=_TOKEN,
                fixed_routes=[OPENAI],
                client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
            )
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client,
        ):
            response = await asyncio.wait_for(
                client.post("/openai/v1/chat/completions", headers={"content-type": "application/json"}, content=body),
                _TIMEOUT,
            )
        assert recorder.drain(_TIMEOUT)
        [row] = store.list()[0]
        detail = store.detail(row["id"])
    assert response.status_code == 400 and sent == []
    told = response.json()["error"]["message"]
    assert "unknown member" in told
    assert "SECRET-VALUE-4471" not in told and "zz_unknown" not in told
    assert row["block_reason"] == "body-unknown-member" and "blocked" in row["flags"]
    assert "SECRET-VALUE-4471" not in json.dumps((row, detail))


async def _recorded(tmp_path, upstream, path: str, body: dict, headers: dict[str, str] | None = None):
    """One request through the real local gateway, recorder and store to ``upstream``: the answer, what reached the
    provider, and the history's row of it."""
    sent: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Answer(_ANSWER))

    with _sinks(tmp_path) as (mediator, recorder, store):
        app = create_app(
            GatewayConfig(
                mediator=mediator,
                recorder=recorder,
                bridge_token=_TOKEN,
                fixed_routes=[upstream],
                client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
            )
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client,
        ):
            response = await asyncio.wait_for(
                client.post(
                    path,
                    headers={"content-type": "application/json", **(headers or {})},
                    content=json.dumps(body).encode(),
                ),
                _TIMEOUT,
            )
        assert recorder.drain(_TIMEOUT)
        [row] = store.list()[0]
    return response, sent, row


async def test_a_request_let_through_by_an_exception_records_the_exception_among_its_passes(tmp_path):
    """Review I-6: a header no profile names, let through by an approved exception for the identity, makes the
    history say so (``passes`` holds ``exception``), never its id or the header's value."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from latet.config import ExceptionComponent, RequestException
    from latet.gateway.request_profiles import REVISION

    row = RequestException(
        id="x0123456789",
        destination="openai",
        profile="openai.chat.1",
        components=[ExceptionComponent(kind="header", name="x-company-team")],
        reason="custom-header",
        granted=datetime.now(UTC),
        revision=REVISION,
        issuer="owner",
    )
    upstream = replace(OPENAI, envelope_exceptions=(row,))
    body = {"model": "m", "messages": [{"role": "user", "content": "ordinary request"}]}
    response, sent, summary = await _recorded(
        tmp_path, upstream, "/openai/v1/chat/completions", body, {"x-company-team": "billing-team-7"}
    )
    assert response.status_code == 200, response.text
    assert len(sent) == 1 and sent[0].headers["x-company-team"] == "billing-team-7"
    assert summary["passes"] == ["exception"]
    assert "x0123456789" not in json.dumps(summary) and "billing-team-7" not in json.dumps(summary)


async def test_a_body_member_its_profile_removes_is_counted(tmp_path):
    """Review I-6 (mutation P4): a Responses request carrying Codex's ``client_metadata``, which its profile declares
    removable, leaves without it, and the history counts one member removed."""
    body = {"model": "m", "input": "ordinary request", "client_metadata": {"session_note": "kept-local-55"}}
    response, sent, summary = await _recorded(tmp_path, OPENAI, "/openai/v1/responses", body)
    assert response.status_code == 200, response.text
    assert len(sent) == 1 and b"client_metadata" not in sent[0].content
    assert summary["removed"]["members"] == 1
    assert "kept-local-55" not in json.dumps(summary)


async def test_a_provider_item_let_through_by_the_built_in_rule_records_its_category(tmp_path):
    """Review I-6 (mutation P1): a Responses request to OpenAI's own destination carrying the provider's encrypted
    reasoning from an earlier turn, with no observed echo (a computer's daemon restarted), is let through by M2's
    built-in rule, and the history names the rule's category, never the item."""
    from latet.gateway.routes import DEFAULT_UPSTREAMS

    [openai] = [upstream for upstream in DEFAULT_UPSTREAMS if upstream.name == "openai"]
    body = {
        "model": "gpt-5.1",
        "input": [
            {"role": "user", "content": "ordinary request"},
            {"type": "reasoning", "id": "rs_0123456789abcdef", "summary": [], "encrypted_content": "gAAAAB" + "Q" * 64},
            {"role": "user", "content": "and the next turn"},
        ],
    }
    response, sent, summary = await _recorded(tmp_path, openai, "/openai/v1/responses", body)
    assert response.status_code == 200, response.text
    assert len(sent) == 1
    assert "provider-encrypted" in summary["passes"]
    assert "gAAAAB" not in json.dumps(summary)


async def test_an_unknown_member_no_exception_can_reach_is_refused_under_a_code_that_says_so(tmp_path):
    """Review I-8: a member no profile names inside a provider's own item (a reasoning item here), which no exception's
    selector can reach, is refused under ``body-unknown-member-no-exception``; a top-level one keeps
    ``body-unknown-member``, which an exact exception can name. The dashboard offers an exception for the second
    alone."""
    body = {
        "model": "m",
        "input": [
            {"role": "user", "content": "ordinary request"},
            {"type": "reasoning", "id": "rs_0123456789abcdef", "summary": [], "x_note": "core"},
        ],
    }
    response, sent, summary = await _recorded(tmp_path, OPENAI, "/openai/v1/responses", body)
    assert response.status_code == 400 and sent == []
    assert summary["block_reason"] == "body-unknown-member-no-exception"


def test_a_websocket_frames_unknown_member_is_one_no_exception_can_reach():
    """Review I-8, F-2: a WebSocket takes no request exception in 0.11.0, so a frame's unknown member is refused under
    the code that says no exception reaches it."""
    from .server.test_reads_or_refuses import _session_mediator

    frame = json.dumps({"type": "response.create", "model": "m", "input": "hello", "zz_unknown": {"a": "b"}})
    for on_server in (True, False):
        mediator, session = _session_mediator(on_server=on_server)
        _, prepared = mediator.mask_ws_frame(frame, session)  # type: ignore[arg-type]
        assert prepared is not None and prepared.envelope_refusal == "body-unknown-member-no-exception", on_server


@pytest.mark.parametrize("member", ["x_auth_mode", "cache_token", "a*b"])
async def test_a_member_no_selector_can_name_is_refused_under_the_no_exception_code(tmp_path, member):
    """Re-review R-1: a member whose name holds a credential's word or a ``*`` cannot be named by an exception's body
    selector (``latet config add`` refuses one: "credential member", "key wildcard"), so its refusal says no exception
    reaches it. Both read one predicate, ``body_selectors.selectable``."""
    from latet.gateway.body_selectors import BodySelector, SelectorRefused, selectable

    with pytest.raises(SelectorRefused):
        BodySelector("/" + member, "string")
    assert selectable((member,)) is False
    body = {"model": "m", "messages": [{"role": "user", "content": "ordinary request"}], member: "plain words"}
    response, sent, summary = await _recorded(tmp_path, OPENAI, "/openai/v1/chat/completions", body)
    assert response.status_code == 400 and sent == []
    assert summary["block_reason"] == "body-unknown-member-no-exception"


def test_the_predicate_follows_the_selector_grammar():
    from latet.gateway.body_selectors import BodySelector, SelectorRefused, selectable

    for path in [("x_note",), ("metadata_x", "team"), ("messages", 0, "x_note"), ("a/b",), ("c~d",)]:
        assert selectable(path) is True, path
        pointer = "/" + "/".join(
            "*" if type(step) is int else str(step).replace("~", "~0").replace("/", "~1") for step in path
        )
        BodySelector(pointer, "string")  # config add takes it
    for path in [("x_api_key",), ("metadata_x", "session_secret"), ("x" * 300,), ("*",), ("bad\nname",)]:
        assert selectable(path) is False, path
        pointer = "/" + "/".join(path)
        with pytest.raises(SelectorRefused):
            BodySelector(pointer, "string")
