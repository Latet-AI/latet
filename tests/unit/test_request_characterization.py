"""The characterization of the requests on a computer (P1-1 of `notes/server-request-coverage-plan.md`): the
transport recorder first, reviewed on its own; then the table (``tests/request_coverage.py``) on a computer's runtime
and on an app with no mediator (0.10's ``latet serve --passthrough``), row after row in one cell, each row's
observation compared exactly with Phase 2's baseline (``baseline_judge``) and held to what the RC plan intends of it on
0.11 (``INTENDED``); and what documents 0.10 from its golden: RC-REC-11's leak rows (every masked WebSocket turn among
them) at 0.10's counts and Phase 1's rule (``phase_1_judge``). It documents behaviour: a request passed on as it came is
recorded so, never called safe."""

import asyncio
import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import connect

from latet.gateway import app as gateway_app

from ..request_coverage import (
    _METHODS,
    ADDED_AFTER_0_11,
    INPUTS,
    INPUTS_0_10,
    TABLE,
    TABLE_0_10,
    Canon,
    EngineSeam,
    _source,
    baseline_judge,
    computer_cell,
    expected,
    holds_no_value,
    leak_counts,
    load_baseline,
    load_golden,
    observe_machine,
    passthrough_cell,
    phase_1_differences,
    phase_1_judge,
    run_case,
    table_for,
)
from ..request_recorder import RecordingClient, RecordingTransport, RecordingWSUpstream

# --- the recorder ----------------------------------------------------------------------------------------------------


async def test_the_transport_records_the_request_as_httpx_sends_it():
    """The raw path with its escapes and every query occurrence, each header in its order with its name's case and its
    duplicates, the content whole; one build and one send counted."""
    transport = RecordingTransport(lambda request: httpx.Response(204))
    async with RecordingClient(transport) as client:
        url = httpx.URL("https://api.example.test/v1/chat/complet%69ons?a=1&&b=&a=2")
        headers = [("X-Dup", "one"), ("content-type", "application/json"), ("x-dup", "two")]
        outgoing = client.build_request("POST", url, headers=headers, content=b'{"k": 1}')
        response = await client.send(outgoing)
    assert response.status_code == 204 and client.builds == 1 and client.sends == 1
    [sent] = transport.sent
    assert sent.method == "POST" and sent.host == "api.example.test"
    assert sent.raw_path == b"/v1/chat/complet%69ons?a=1&&b=&a=2"
    # httpx's own first, then the caller's in their order, each name's case kept, then the length it adds.
    names = [name for name, _ in sent.headers]
    assert names == [b"Host", b"Accept", b"Accept-Encoding", b"Connection", b"User-Agent", b"X-Dup", b"content-type",
                     b"x-dup", b"Content-Length"]  # fmt: skip
    assert [(n, v) for n, v in sent.headers if n.lower() == b"x-dup"] == [(b"X-Dup", b"one"), (b"x-dup", b"two")]
    assert dict(sent.headers)[b"Content-Length"] == b"8"
    assert sent.content == b'{"k": 1}'


async def test_the_transport_raises_the_failure_it_is_given_after_recording():
    transport = RecordingTransport(lambda request: httpx.Response(200))
    transport.failure = httpx.ConnectError("no route")
    async with RecordingClient(transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post("https://api.example.test/v1/x", content=b"x")
    assert len(transport.sent) == 1


async def test_the_websocket_upstream_records_the_handshake_and_every_frame():
    upstream = RecordingWSUpstream().start()
    try:
        headers = [("X-One", "a"), ("x-one", "b"), ("session-id", "s")]
        async with connect(
            f"ws://127.0.0.1:{upstream.port}/backend-api/codex/responses?x=1&x=2",
            additional_headers=headers,
            user_agent_header=None,
        ) as ws:
            await ws.send(json.dumps({"type": "response.create", "input": "hello there"}))
            answers = [json.loads(await ws.recv()) for _ in range(4)]
            await ws.send(b"\x00\x01")
            assert json.loads(await ws.recv()) == {"type": "ack"}
        await asyncio.sleep(0.05)
    finally:
        upstream.stop()
    assert [a["type"] for a in answers][-1] == "response.completed"
    [shake] = upstream.handshakes
    assert shake.raw_path == b"/backend-api/codex/responses" and shake.query_string == b"x=1&x=2"
    ones = [(n, v) for n, v in shake.headers if n.lower() == b"x-one"]
    assert [v for _, v in ones] == [b"a", b"b"]  # order and duplicates as received
    assert shake.frames[1] == b"\x00\x01" and shake.accepted


def test_the_methods_drawn_are_the_apps():
    assert list(_METHODS) == gateway_app._METHODS


def test_a_canon_names_originals_by_place_and_fakes_as_they_appear():
    canon = Canon(["fake-two@x.example", "fake-one@x.example"])
    text = "fake-one@x.example, giulia.marchetti@gmail.com, fake-two@x.example, fake-one@x.example"
    assert canon.text(text) == "<F:0>, <O:0>, <F:1>, <F:0>"
    assert canon.text("fake-two@x.example") == "<F:1>"  # numbered once for the whole case


# --- the table -------------------------------------------------------------------------------------------------------


def _compare(machine, observations, baseline):
    """Each row's observation against the baseline's by ``baseline_judge`` (exact: the rule ``replay`` follows too):
    ``{row: why}`` for the rows that differ."""
    differing = {}
    for case, observation in observations:
        want = expected(baseline, machine, case)
        assert want is not None, (
            f"{case.id} has no baseline on the {machine}: record it (scripts/request_characterization.py record)"
        )
        if (why := baseline_judge(observation, want)) is not None:
            differing[case.id] = why
    return differing


def _report(differing):
    return "\n".join(f"{row}: {why}" for row, why in differing.items())


async def test_the_table_on_a_computer(tmp_path):
    baseline = load_baseline()
    upstream = RecordingWSUpstream().start()
    try:
        async with computer_cell(tmp_path, upstream) as cell:
            observations = [(case, await run_case(cell, case)) for case in table_for("computer")]
    finally:
        upstream.stop()
    assert not holds_no_value([o for _, o in observations])
    differing = _compare("computer", observations, baseline)
    assert not differing, _report(differing)
    documented("computer", dict((case.id, o) for case, o in observations))


async def test_the_table_with_no_mediator():
    """An app with no mediator (H0, 0.10's ``latet serve --passthrough``): 0.11 refuses every request and WebSocket,
    nothing sent, nothing held."""
    baseline = load_baseline()
    upstream = RecordingWSUpstream().start()
    try:
        async with passthrough_cell(upstream) as cell:
            observations = [(case, await run_case(cell, case)) for case in table_for("passthrough")]
    finally:
        upstream.stop()
    differing = _compare("passthrough", observations, baseline)
    assert not differing, _report(differing)
    documented("passthrough", dict((case.id, o) for case, o in observations))


# --- what the RC plan intends each row to do on 0.11 ----------------------------------------------------------------


@dataclass(frozen=True)
class Intended:
    """What the RC plan intends a row to do on 0.11 (``why``: the plan's reason, and what changed from 0.10), held
    besides the baseline's exact comparison: the client's ``status`` (an HTTP answer's or a WebSocket's denial; None
    for a WebSocket accepted or closed by Latet, or a request cancelled), the ``close`` code, words the client is told
    (``said``: in its error, an error frame or the close reason), what reached the provider (``sends`` at S1, ``connects``
    and ``frames_up`` at S3/S4), whether the request or its first frame left rewritten with a fake (``masked``) or as it
    came (``as_sent``), the history's records (``records``: each one's refusal category in order, ``sent`` for one with
    none) and ``flags`` one of them carries."""

    status: int | None
    sends: int = 0
    said: str | None = None
    close: int | None = None
    connects: int | None = None
    frames_up: int | None = None
    masked: bool | None = None
    as_sent: bool | None = None
    records: tuple[str, ...] | None = None
    flags: tuple[str, ...] = ()
    why: str = ""


SENT = ("sent",)
REFUSED = ("refused",)


def _refused(category: str, why: str, said: str | None = None, **more: Any) -> Intended:
    """An HTTP request refused for its envelope, with nothing sent: its category said and in its record (P2-1, R-79)."""
    return Intended(400, said=said or category, records=(category,), why=why, **more)


def _masked(why: str, status: int = 200, **more: Any) -> Intended:
    """An HTTP request sent once, its values masked, recorded."""
    return Intended(status, sends=1, masked=True, records=SENT, why=why, **more)


def _turn_refused(said: str, why: str, **more: Any) -> Intended:
    """A WebSocket turn refused at the relay: the handshake connected, the frame never sent, its refusal recorded."""
    return Intended(None, said=said, connects=1, frames_up=0, records=REFUSED, why=why, **more)


def _handshake_refused(category: str, why: str) -> Intended:
    """A WebSocket refused for its envelope at its handshake (P2-8a, ruling 5): closed 1008 naming the category, nothing
    connected, recorded as an HTTP request refused so is."""
    return Intended(None, close=1008, said=category, connects=0, records=(category,), why=why)


INTENDED: dict[str, Intended] = {
    # I1: the app's own paths and methods (unchanged from 0.10)
    "i1-own-path": Intended(404, said="no such route", records=(), why="I1: under /_latet only Latet's own routes"),
    "i1-method-unknown": Intended(405, records=(), why="I1: a method outside the app's"),
    "i1-host-foreign": Intended(403, said="host not allowed", records=(), why="I1: a Host not the app's own"),
    # I3: routes
    "i3-no-route": Intended(404, said="no upstream", records=(), why="I3: no route, so no destination to record it at"),
    "i3-escaped-path": _masked("I3/P2-1: the readable path selects the profile; the raw target is proven and sent"),
    "i3-query-kept": _refused(
        "envelope-query-unknown",
        "P2-1: api-version is an exact configured value and x is in no profile: an unnamed query name refuses "
        "(0.10 sent each occurrence as it came)",
    ),
    "i3-query-value": _refused(
        "envelope-query-unknown", "P2-1: a query parameter no profile names refuses naming it (0.10 left its value)"
    ),
    "i3-destination": _masked("I3/I4: a destination's route masks as a provider's"),
    # I4: clouds
    "i4-bedrock-invoke": Intended(
        200, sends=1, as_sent=True, records=SENT, why="I4/H10: InvokeModel, a model id of its grammar, checked and sent"
    ),
    "i4-bedrock-unread-call": _refused("envelope-route", "P2-1: Converse is in no profile (0.10: 404, unread)"),
    "i4-bedrock-bad-region": _refused("envelope-region", "P2-1 S-M4: a region outside the enumeration, before resolve"),
    "i4-bedrock-profiles": Intended(
        200, sends=1, as_sent=True, records=SENT, why="D1: Bedrock's inference profiles, their type of the enum"
    ),
    "i4-bedrock-profile-unsigned": _refused("envelope-route", "P2-1: a profile route whose key is not this Latet's"),
    "i4-vertex": _refused(
        "envelope-resource",
        "D4 notes, round 2: a Vertex project stays an owner-listed request_resources row, and the cells list none "
        "(0.10 checked the call and sent it)",
    ),
    "i4-vertex-unread": _refused("envelope-resource", "D4 notes: the unlisted Vertex project refuses first"),
    "i4-vertex-read": Intended(
        200,
        sends=1,
        as_sent=True,
        records=SENT,
        why="Minors after 0.11, item 13: the Vertex read path, its project the cell lists; checked, sent as it came",
    ),
    "i4-vertex-stream": Intended(
        200, sends=1, as_sent=True, records=SENT, why="Minors after 0.11, item 13: the same, streamed"
    ),
    "i4-gemini": _masked("I4/H12: generateContent masked; key, a destination-bound credential, kept"),
    # H1-H12: preparation
    "h1-bodyless": Intended(
        200, sends=1, as_sent=True, records=SENT, why="H1/P2-1: the model list's profile, a bodyless GET as it came"
    ),
    "h2-no-protocol": _refused("envelope-route", "P2-1/P2-2: a route no profile names (0.10 sent it as it came)"),
    "h2-gemini-unread": _refused("envelope-route", "P2-1: a Gemini route no profile names"),
    "h3-undecodable": Intended(
        400,
        said="unreadable request body",
        records=REFUSED,
        why="P2-2: both machines decode through the bounded codec, 400 on both (0.10's computer: its own 500)",
    ),
    "h3-unknown-encoding": _refused(
        "envelope-header-value", "P2-1: content-encoding takes its named codings, refused naming the header"
    ),
    "h3-gzip": _masked(
        "H3: a gzip body decoded, masked and encoded again (the fake provider reads no gzip: its 400)", status=400
    ),
    "h3-malformed-recognized": Intended(
        400, said="request body refused (shape)", records=REFUSED, why="P2-2: malformed JSON on a route Latet reads"
    ),
    "h3-malformed-unknown": _refused("envelope-route", "P2-1: the batches route, refused before its body is read"),
    "h4-bedrock-not-object": Intended(400, said="is not one", records=REFUSED, why="H4: a Bedrock body no object"),
    "h4-not-object": Intended(
        400, said="is not one", records=REFUSED, why="P2-2: a computer refuses what a server does (0.10 sent it)"
    ),
    "h4-empty-object": Intended(
        200, sends=1, as_sent=True, records=SENT, why="H4: an empty object holds nothing to read, sent as it came"
    ),
    "h4-empty-body": Intended(
        400, said="requires a JSON body", records=REFUSED, why="P2-2: an empty recognized POST (0.10 sent it)"
    ),
    "h5-bedrock-count-unread": Intended(
        400, said="cannot read this token count", records=REFUSED, why="H5: a count of a kind Latet does not read"
    ),
    "h5-bedrock-no-messages": Intended(
        400, said="is not one", records=REFUSED, why="H5: a Bedrock body with no messages"
    ),
    "h6-images-blocked": Intended(
        400, said="images: block", records=REFUSED, flags=("uninspected:image",), why="H6: images: block"
    ),
    "h7-twice-verify-only": Intended(
        400, said="gives a key twice", records=REFUSED, why="P2-2: verify-only cannot send duplicate raw bytes"
    ),
    "h8-unreadable-fields": Intended(
        400,
        said="of a kind Latet does not read",
        records=REFUSED,
        why="P2-2, one code path: a computer refuses the unread block too (0.10's computer sent it as it came)",
    ),
    "h8-schema-read": Intended(200, sends=1, as_sent=True, records=SENT, why="H8: the schema read, nothing to mask"),
    "h9-image": Intended(
        400,
        said="latet ocr install",
        records=REFUSED,
        why="P2-2 M3: an image where text recognition is not available refuses naming latet ocr install (0.10 sent "
        "it unread)",
    ),
    "h9-image-claude-code": Intended(
        400, said="latet ocr install", records=REFUSED, why="P2-2 M3, on a verify-only route too (0.10 sent it)"
    ),
    "h10-verify-only-clean": Intended(
        200, sends=1, as_sent=True, records=SENT, why="H10: verify-only, nothing found, sent as it came"
    ),
    "h10-verify-only-blocked": Intended(
        400, said="GITHUB_PAT", records=REFUSED, flags=("leak",), why="H10: verify-only stops a secret"
    ),
    "h11-project-off": Intended(
        400,
        said="Latet is off in this project",
        records=REFUSED,
        flags=("project-off",),
        why="D5: an off project's request refused with D5's words, recorded (0.10 sent it unmasked)",
    ),
    "h11-project-off-ws": Intended(
        None,
        close=1008,
        said="Latet is off in this project",
        connects=0,
        records=REFUSED,
        flags=("project-off",),
        why="D5, F-5: an off project's WebSocket closed 1008 with D5's words before connecting (0.10 relayed it "
        "unmasked), and recorded as over HTTP (F1 of the P2 baseline, which recorded none: root's ruling)",
    ),
    "h12-masked": _masked("H12: masked, restored"),
    "h12-masked-stream": _masked("H12: masked, streamed back restored"),
    "h12-anthropic": _masked("H12: Anthropic's messages masked"),
    "h12-responses": _masked("H12: Responses masked"),
    "h12-duplicate-key": _masked("P2-2: a rewrite sends only its normalized serialization, the key's last copy"),
    "h12-mended": _masked("P2-2: half a character read as U+FFFD on a rewritten profile, written anew"),
    "h12-unknown-field": _masked("P2-2: metadata, a declared Chat Completions member, its value masked"),
    "h12-blocked-card": Intended(
        400, said="CREDIT_CARD", records=REFUSED, flags=("leak",), why="H13: a card as a JSON number, the final check"
    ),
    "h12-header-value": _refused(
        "envelope-header-unknown", "P2-1: x-note is in no profile, refused naming it (0.10 sent its value unread)"
    ),
    "h12-duplicate-headers": _refused(
        "envelope-header-unknown", "P2-1: x-dup is in no profile, refused naming it (0.10 sent the duplicates in order)"
    ),
    # H13: reviews
    "h13-local-reviewer": _masked("H13: the local reviewer's finding masked in the tool result"),
    "h13-live-approved": _masked("H13: the Live Reviewer's approval applied"),
    "h13-live-refused": Intended(400, said="you refused it", records=REFUSED, why="H13: the Live Reviewer refused it"),
    # H14 and the response path
    "h14-upstream-error": _masked("H14: the provider's 500 relayed", status=500, said="overloaded"),
    "h14-redirect": _masked("H14: a redirect is not followed", status=502, said="redirect", flags=("error",)),
    "h14-unreachable": _masked("H14: the provider unreachable", status=502, said="unreachable", flags=("error",)),
    # W0-W3: the WebSocket
    "w0-codex": Intended(
        None, connects=1, frames_up=1, masked=True, records=SENT, why="W0: a masked turn; RC-REC-11 (c) released"
    ),
    "w0-destination": _handshake_refused(
        "envelope-query-unknown", "P2-1: x is in no profile, the handshake refused (0.10 connected and relayed)"
    ),
    "w0-no-route": Intended(None, close=4404, connects=0, records=(), why="W0: no route"),
    "w0-refused": Intended(
        403,
        connects=1,
        frames_up=0,
        records=SENT,
        why="W0: the provider's denial relayed, and recorded as over HTTP (minors after 0.11, item 4)",
    ),
    "w0-redirect": Intended(
        502,
        said="redirect",
        connects=1,
        frames_up=0,
        records=SENT,
        why="W0: no redirect followed, and recorded as over HTTP (minors after 0.11, item 4)",
    ),
    "w1-binary": _turn_refused("binary frame", "P2-2: a binary frame refused unread (0.10's computer relayed it)"),
    "w2-contentless": Intended(
        None, connects=1, frames_up=1, as_sent=True, records=(), why="W2: response.cancel, no content, as it came"
    ),
    "w3-unknown-type": _turn_refused(
        "a frame of another type", "P2-2: a frame type Latet does not read (0.10's computer relayed it)"
    ),
    "w3-malformed": _turn_refused("request body refused (shape)", "P2-2: a malformed frame (0.10's computer relayed)"),
    "w3-not-object": _turn_refused("a frame of another type", "P2-2: a frame no object (0.10's computer relayed it)"),
    "w3-blocked": _turn_refused("CREDIT_CARD", "W3: a turn masked, then stopped by the final check", flags=("leak",)),
    "w3-header-value": _handshake_refused(
        "envelope-header-unknown", "P2-1: x-note is in no profile, the handshake refused (0.10 sent it unread)"
    ),
    # C0: the plugin's bridge, which reaches no provider
    "c0-mask": Intended(200, records=(), why="C0: masked for the plugin"),
    "c0-rehydrate": Intended(200, records=(), why="C0: restored for the plugin"),
    "c0-verify-blocked": Intended(200, records=REFUSED, flags=("leak",), why="C0: the plugin's check stops a secret"),
    "c0-outbound": Intended(
        200, records=REFUSED, flags=("outbound:web_fetch",), why="C0: a fake to a web tool stopped"
    ),
    "c0-route-check": Intended(200, records=REFUSED, flags=("route-bypass",), why="C0: the route check's record"),
    "c0-unauthorized": Intended(401, said="unauthorized", records=(), why="C0: no token"),
    # RC-REC-11's own rows, their fixes in (P1-4)
    "rc11-a-unstarted-stream": _masked(
        "RC-REC-11 (a): the client gone before the answer starts: the hold released, the answer closed",
        flags=("error",),
    ),
    "rc11-b-cancel-masking": Intended(
        None, records=(), why="RC-REC-11 (b): cancelled while masking, the hold released"
    ),
    "rc11-b-cancel-live": Intended(None, records=(), why="RC-REC-11 (b): cancelled at the decision, each handle once"),
    "rc11-c-ws-turns": Intended(
        None, connects=1, frames_up=2, masked=True, records=("sent", "sent"), why="RC-REC-11 (c): two turns released"
    ),
}
# What a machine of its own does besides (or instead of) the rows above.
ON_A_COMPUTER: dict[str, Intended] = {
    "h3-too-large": Intended(
        400, sends=1, records=SENT, why="R-17: a computer's bound is 32 MiB, far above this body (the fake's 400)"
    ),
    "s2-expired-token": Intended(
        200, sends=2, as_sent=True, records=SENT, why="S2: AWS's ExpiredToken, credentials read again, sent once more"
    ),
}
ON_A_SERVER: dict[str, Intended] = {
    "h3-too-large": Intended(
        413, said="larger than 65536 bytes", records=("too-large",), why="H3: a server's max_request_mb, decoded"
    ),
    "i2-no-address": Intended(403, said="no such address", records=(), why="I2: the gate"),
    "i2-unknown-key": Intended(403, said="no such address", records=(), why="I2: an address no key has"),
    "i2-browser": Intended(403, said="browser requests are not accepted", records=(), why="I2: a browser's Origin"),
    "i2-bridge-not-a-call": Intended(404, said="no such route", records=(), why="I2/C0: no bridge on a server's"),
    "h12-other-instance": _masked("H12/I2: the conversation's next request on the other instance"),
}
WITH_NO_MEDIATOR: dict[str, Intended] = {
    "h0-passthrough": Intended(
        400,
        said="request protection is unavailable",
        records=(),
        why="D5, P2-4: --passthrough removed, an app with no mediator sends nothing (0.10 sent it as it came)",
    ),
    "h0-passthrough-ws": Intended(
        None,
        close=1008,
        said="request protection is unavailable",  # as over HTTP (minors after 0.11, item 12)
        connects=0,
        records=(),
        why="D5, P2-4: no unmediated WebSocket (0.10 relayed both frames as they came)",
    ),
}


def intended_for(machine: str) -> dict[str, Intended]:
    if machine == "passthrough":
        return WITH_NO_MEDIATOR
    return INTENDED | (ON_A_SERVER if machine == "server" else ON_A_COMPUTER)


def _said(observation: dict[str, Any]) -> str:
    client = observation["client"]
    words = [
        part["error"]
        for part in (client.get("body"), client.get("denial"))
        if isinstance(part, dict) and isinstance(part.get("error"), str)
    ]
    if client.get("close"):
        words.append(client["close"][1])
    words += [frame["error"] for frame in client.get("frames") or [] if isinstance(frame.get("error"), str)]
    return " | ".join(words)


def _fakes_left(views: Any) -> bool:
    text = json.dumps(views)
    return '"sF"' in text or "<F:" in text


def _left(observation: dict[str, Any]) -> tuple[Any, bool | None]:
    """The request's first body (or its socket's frames) as it left, and whether it left as it came."""
    if observation["sockets"]:
        socket = observation["sockets"][0]
        return socket["frames"], (bool(socket["as_sent"]) and all(i >= 0 for i in socket["as_sent"]))
    if observation["upstream"]:
        body = observation["upstream"][0]["body"]
        return body.get("json"), body.get("as_sent")
    return None, None


def _in_clear(views: Any) -> bool:
    text = json.dumps(views)
    return "<O:" in text or '"sO' in text or '"vO' in text


def intended_problems(observation: dict[str, Any], want: Intended) -> list[str]:
    """Where ``observation`` does otherwise than ``want``: empty when it holds."""
    found = []
    client, counts = observation["client"], observation["counts"]
    if client.get("status") != want.status:
        found.append(f"status {client.get('status')} != {want.status}")
    if counts["send"] != want.sends:
        found.append(f"{counts['send']} sends != {want.sends}")
    if want.said is not None and want.said not in _said(observation):
        found.append(f"not told {want.said!r} ({_said(observation)[:160]!r})")
    if want.close is not None and (client.get("close") or [None])[0] != want.close:
        found.append(f"close {client.get('close')} != {want.close}")
    if want.connects is not None and counts["connect"] != want.connects:
        found.append(f"{counts['connect']} connects != {want.connects}")
    if want.frames_up is not None and counts["frames_up"] != want.frames_up:
        found.append(f"{counts['frames_up']} frames up != {want.frames_up}")
    left, as_came = _left(observation)
    if want.masked and (as_came is not False or not _fakes_left(left)):
        found.append("not masked as it left")
    if want.as_sent is not None and as_came is not want.as_sent:
        found.append(f"as sent {as_came} != {want.as_sent}")
    records = observation["records"]
    if want.records is not None:
        kinds = tuple(record["block_reason"] or "sent" for record in records)
        if kinds != want.records:
            found.append(f"records {kinds} != {want.records}")
    flags = {flag for record in records for flag in record["flags"]}
    if missing := [flag for flag in want.flags if flag not in flags]:
        found.append(f"no record flagged {missing}")
    return found


def invariant_problems(observation: dict[str, Any]) -> list[str]:
    """What every row must hold on 0.11, whatever it is: no original in clear in what left, the history or the log;
    every hold released (a server's vault holding no more after the row), no renewer left running, every answer of
    the provider closed; and a WebSocket turn answered whole recorded whole, never as an error with its answer
    omitted (0.10 recorded it so; F2 of the P2 baseline, whose client gone at once raced it, has a test of its own)."""
    found = []
    for part in ("upstream", "sockets", "records", "logs"):
        if _in_clear(observation[part]):
            found.append(f"an original in clear in {part}")
    counts = leak_counts(observation)
    if not counts["hold"] <= counts["release"] <= counts["hold"] + counts["reacquire"]:
        found.append(f"{counts['release']} releases for {counts['hold']} holds and {counts['reacquire']} reacquires")
    if counts["held"]:
        found.append(f"the vault holds {counts['held']} more")
    if counts["renewer_running"]:
        found.append("a renewer still running")
    if counts["unclosed"]:
        found.append(f"{counts['unclosed']} answers of the provider left open")
    for record in observation["records"]:
        if record["status"] == 101 and ("error" in record["flags"] or "omitted" in (record["response"] or [])):
            found.append("a WebSocket turn answered whole recorded as an error, its answer omitted (F2)")
    return found


def documented(machine: str, found: dict[str, dict[str, Any]]) -> None:
    """Every row held to what the RC plan intends of it on 0.11 and to the invariants, all rows judged before the
    failure names those that do otherwise (a FINDING's row among them, until root rules)."""
    intended = intended_for(machine)
    problems = {}
    for row, observation in found.items():
        assert row in intended, f"{row} has no intended outcome on the {machine}"
        if listed := intended_problems(observation, intended[row]) + invariant_problems(observation):
            problems[row] = listed
    assert not problems, "\n".join(f"{machine} {row}: {'; '.join(why)}" for row, why in problems.items())


async def _observe_the_computer(tmp_path):
    """``scripts/request_characterization.py record|replay``: the computer's table, the passthrough's, and its two
    cells of generated cases (``LATET_RC_SEED``, ``LATET_RC_COUNT``), written to ``LATET_RC_OUT``/computer.json."""
    upstream = RecordingWSUpstream().start()
    made = [0]

    def fresh():
        made[0] += 1
        folder = tmp_path / f"cell{made[0]}"
        folder.mkdir()
        return computer_cell(folder, upstream)

    try:
        out = Path(os.environ["LATET_RC_OUT"]) / "computer.json"
        seed, count = int(os.environ["LATET_RC_SEED"]), int(os.environ["LATET_RC_COUNT"])
        await observe_machine(fresh, "computer", seed, count, out, lambda: passthrough_cell(upstream))
    finally:
        upstream.stop()


if os.environ.get("LATET_RC_OUT"):  # collected for the script alone: never a skip (CI fails a server test skipped)
    test_observe_the_computer_for_the_script = _observe_the_computer


async def test_a_websocket_turn_answered_whole_is_recorded_whole_though_its_client_goes_at_once(tmp_path):
    """F2 of the P2 baseline: a Codex turn answered whole (its response.completed relayed, restored) whose client
    closes at once after it is recorded whole, as 0.10 recorded it. Before root's ruling the close stopped the turn's
    finalizer while the engine's thread screened it, and the record had the error flag and its answer omitted; a
    turn relayed whole now settles alone once its relay goes (``ResponseFinalizer.relayed`` and ``leave``). A race on
    the real relay, three runs; the order that lost it is forced by test_response_ws_lifecycle's and
    test_response_finalizer_lifecycle's own tests."""
    row = next(case for case in TABLE if case.id == "w0-codex")
    case = dataclasses.replace(row, id="f2-leaves-at-once", setup=("leaves-at-once",))
    upstream = RecordingWSUpstream().start()
    try:
        async with computer_cell(tmp_path, upstream) as cell:
            found = [await run_case(cell, case) for _ in range(3)]
    finally:
        upstream.stop()
    assert all(o["client"]["frames"][-1]["type"] == "response.completed" for o in found)  # each answered whole
    recorded = [[(r["status"], r["flags"], r["response"]) for r in o["records"]] for o in found]
    assert not [problem for o in found for problem in invariant_problems(o)], recorded


def test_every_row_has_an_intended_outcome_on_each_of_its_machines():
    for machine in ("computer", "server", "passthrough"):
        rows = {case.id for case in table_for(machine)}
        assert rows == set(intended_for(machine)), machine


def test_the_p2_table_is_0_10s_with_valid_ids():
    """The P2 table's rows are 0.10's, in order, on the same machines, with the same setups and leak marks: only the
    ids 0.10 read none of (``Inputs``) are written otherwise, and none of 0.10's placeholders is left."""
    kept = [case for case in TABLE if case.id not in ADDED_AFTER_0_11]  # the rows added since stand apart
    assert {case.id for case in TABLE} - {case.id for case in kept} == ADDED_AFTER_0_11
    assert [case.id for case in kept] == [case.id for case in TABLE_0_10]
    for now, then in zip(kept, TABLE_0_10, strict=True):
        assert (now.ledger, now.method, now.transport, now.machines, now.setup, now.leak, now.after_phase_1) == (
            then.ledger,
            then.method,
            then.transport,
            then.machines,
            then.setup,
            then.leak,
            then.after_phase_1,
        )

    def written(case):
        parts = [case.target, *(f"{name}: {value}" for name, value in case.headers)]
        parts += [case.body.decode("latin-1") if isinstance(case.body, bytes) else ""]
        return "\n".join(parts + [f if isinstance(f, str) else f.decode("latin-1") for f in case.frames])

    text = "\n".join(written(case) for case in TABLE)
    for placeholder in ('"model": "m"', '"model": "n"', '"id": "c1"', '"id": "t0"', '"call_id": "c"'):
        assert placeholder not in text, placeholder
    old = INPUTS_0_10
    for placeholder in (old.bedrock, f"/{old.vertex}:", f"/{old.gemini}:", old.codex_session, old.claude_session):
        assert placeholder not in text, placeholder
    assert INPUTS.openai in text and INPUTS.codex in text and INPUTS.bedrock in text


@pytest.mark.parametrize("row", [case.id for case in TABLE_0_10 if case.leak is not None])
def test_the_leak_rows_say_0_10s_counts_and_phase_1s(row):
    """RC-REC-11's rows: 0.10's golden holds its counts (a computer's vault holds nothing, its engine is still asked);
    the expected counts after Phase 1 differ from them only where a fix releases or closes what stayed held or open in
    0.10, never in the holds taken."""
    golden = load_golden()
    case = next(case for case in TABLE_0_10 if case.id == row)
    assert case.after_phase_1 is not None
    today = golden["table"]["computer"][row]
    assert set(phase_1_differences(case, today)) <= {"release", "renewer_running", "unclosed"}
    assert leak_counts(today)["hold"] == case.after_phase_1["hold"]  # as many holds


def test_every_masked_websocket_turn_of_the_table_is_a_leak_row():
    """RC-REC-11 (c) is every masked WebSocket turn's: each table row that holds and releases less in 0.10's golden, on
    either machine, is marked, and no other row holds without releasing (a reacquire after the Live Reviewer finds its
    hold live)."""
    golden = load_golden()
    marked = {case.id for case in TABLE_0_10 if case.leak is not None}
    for machine in ("computer", "server"):
        for row, observation in golden["table"][machine].items():
            counts = leak_counts(observation)
            if (counts["release"] or 0) < (counts["hold"] or 0):
                assert row in marked, f"{machine} {row} holds without releasing and is not marked"


# --- the one rule of Phase 1 (``phase_1_judge``: the modules' and replay's) ---------------------------------------


def _turn(
    hold=1, release=0, reacquire=0, held: int | None = 1, frames_up=1, renewers=(), answers=(0, 0), **engine
) -> dict[str, Any]:
    counts = {"hold": hold, "release": release, "reacquire": reacquire, **engine}
    return {
        "case": "x",
        "machine": "server",
        "upstream": [],
        "sockets": [{"frames": [{"sha": "aa"}] * frames_up}],
        "counts": {"build": 0, "send": 0, "connect": 1, "frames_up": frames_up},
        "client": {"status": None, "frames": [{"type": "response.completed"}]},
        "engine": {k: v for k, v in counts.items() if v} | {"renewers": list(renewers)},
        "left": 1,
        "held": held,
        "answers": {"made": answers[0], "closed": answers[1]},
        "records": [{"status": 101}],
        "logs": [],
    }


def test_the_rule_is_exact_until_the_fixes_are_in():
    today = _turn()
    assert phase_1_judge(today, today, cell="server-ws", fixes_in=False) is None
    assert phase_1_judge(_turn(release=1, held=0), today, cell="server-ws", fixes_in=False) is not None


def test_once_in_a_generated_websocket_case_releases_each_hold_once_and_changes_nothing_else():
    today = _turn(hold=2, held=None)
    assert phase_1_judge(_turn(hold=2, release=2, held=None), today, cell="server-ws", fixes_in=True) is None
    assert "releases" in str(phase_1_judge(today, today, cell="server-ws", fixes_in=True))  # still held
    twice = _turn(hold=2, release=3, held=None)
    assert "releases" in str(phase_1_judge(twice, today, cell="server-ws", fixes_in=True))
    sent = _turn(hold=2, release=2, held=None, frames_up=2)  # one frame more upstream
    assert phase_1_judge(sent, today, cell="server-ws", fixes_in=True) is not None
    renewed = _turn(hold=2, release=2, held=None, **{"vault.renew.transform": 1})
    assert phase_1_judge(renewed, today, cell="server-ws", fixes_in=True) is not None
    # An HTTP case, and a table row not marked, stay exact.
    assert phase_1_judge(_turn(release=1), _turn(), cell="server-http", fixes_in=True) is not None
    plain = next(case for case in TABLE if case.id == "h12-masked")
    assert phase_1_judge(_turn(release=1), _turn(), case=plain, fixes_in=True) is not None


def test_once_in_a_leak_row_holds_its_expected_counts_and_changes_nothing_else():
    row = next(case for case in TABLE if case.id == "w0-codex")
    today = _turn()
    assert phase_1_judge(_turn(release=1, held=0), today, case=row, fixes_in=True) is None
    assert "after Phase 1" in str(phase_1_judge(today, today, case=row, fixes_in=True))
    changed = _turn(release=1, held=0) | {"left": 2}
    assert phase_1_judge(changed, today, case=row, fixes_in=True) is not None
    live = next(case for case in TABLE if case.id == "rc11-b-cancel-live")
    before = _turn(reacquire=1, release=1)
    for releases in (1, 2):  # either lease design releases each handle once
        assert phase_1_judge(_turn(reacquire=1, release=releases, held=0), before, case=live, fixes_in=True) is None
    assert phase_1_judge(_turn(reacquire=1, release=3, held=0), before, case=live, fixes_in=True) is not None
    unstarted = next(case for case in TABLE if case.id == "rc11-a-unstarted-stream")
    open_today = _turn(renew=2, renewers=["running"], answers=(1, 0), **{"vault.renew.timer": 2})
    closed = _turn(release=1, held=0, renewers=["cancelled"], answers=(1, 1))
    assert phase_1_judge(closed, open_today, case=unstarted, fixes_in=True) is None
    still_open = _turn(release=1, held=0, renewers=["cancelled"], answers=(1, 0))
    assert "unclosed" in str(phase_1_judge(still_open, open_today, case=unstarted, fixes_in=True))


# --- the engine seam -------------------------------------------------------------------------------------------------


class _Vault:
    def renew(self, key):
        return True


class _Engine:
    def __init__(self, renews):
        self.vault, self.renews = _Vault(), renews

    def hold(self, session):
        return "held"

    def reacquire(self, session, held):
        return held

    def release(self, session, held):
        return None

    def renew(self, session):
        return self.vault.renew(session)

    def restores(self, session, json_mode):
        if self.renews:  # Task 9's renew on restore
            self.vault.renew(session)
        return []


@pytest.mark.parametrize("renews", [True, False], ids=["renews", "renews-no-more"])
def test_the_seam_counts_the_vaults_renews_by_who_asked(renews):
    """Review I-2: the vault's own renews are counted by their source, apart from the calls of ``restores``, so a
    ``restores`` that renews no more changes the counts."""
    engine = _Engine(renews)
    seam = EngineSeam()
    seam.attach(engine)
    engine.restores("s", False)
    with _source("recorder"):
        engine.restores("s", True)
    engine.renew("s")
    engine.release("s", engine.hold("s"))
    restored = {"vault.renew.transform": 1, "vault.renew.recorder": 1} if renews else {}
    assert dict(seam.counts) == {
        "restores.transform": 1, "restores.recorder": 1, "renew": 1, "vault.renew.timer": 1, "hold": 1, "release": 1,
        **restored,
    }  # fmt: skip
    seam.detach()
    assert "renew" not in vars(engine.vault) and "restores" not in vars(engine)
