"""P2-2 whole-body behavior through accepted production preparation and send seams."""

from __future__ import annotations

import base64
import copy
import gzip
import io
import json
import struct
import time
import zlib
from collections.abc import Iterator
from typing import Any

import pytest
from PIL import Image

from latet.gateway.mediate import Mediator, Prepared
from latet.gateway.routes import Upstream
from latet.protocols import anthropic, codec, openai_responses
from latet.vision.masking import ImageMasking

from ..fake_ocr import FakeOcr
from .test_request_coverage import gateway
from .test_request_outcomes import _mediator

EMAIL = "emily.carter@acme.com"
HEADERS = {"content-type": "application/json"}
CLAUDE = {**HEADERS, "x-claude-code-session-id": "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"}
CASES = [
    (
        "anthropic",
        "/anthropic/v1/messages",
        {"model": "claude-sonnet-4-6", "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]},
    ),
    ("openai", "/openai/v1/chat/completions", {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}),
    ("openai", "/openai/v1/completions", {"model": "gpt-4o", "prompt": "hello"}),
    ("openai", "/openai/v1/responses", {"model": "gpt-4o", "input": "hello"}),
    ("openai", "/openai/v1/embeddings", {"model": "text-embedding-3-small", "input": "hello"}),
    (
        "gemini",
        "/gemini/v1beta/models/gemini-2.5-flash:generateContent",
        {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
    ),
]


@pytest.fixture
def mediator() -> Iterator[Mediator]:
    made = _mediator()
    yield made
    made.close()


def prepare(mediator: Mediator, family: str, path: str, body: Any, *, verify: bool = False) -> Prepared:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    result = mediator.prepare_http(
        Upstream(family, "/" + family, "https://api.example.invalid", envelope_expected=frozenset({("model", "m")})),
        "POST",
        path,
        CLAUDE if verify else HEADERS,
        raw,
        (),
    )
    if result.lease is not None:
        result.lease.release()
    return result


@pytest.mark.parametrize(("family", "path", "body"), CASES)
def test_known_readable_body_remains_sendable(mediator, family, path, body):
    result = prepare(mediator, family, path, copy.deepcopy(body))
    assert result.block_reason is None


@pytest.mark.parametrize(("family", "path", "body"), CASES)
@pytest.mark.parametrize(
    "value", ["UNDECLARED_CANARY", ["one", "two"], 4111111111111111, {"nested": "UNDECLARED_CANARY"}]
)
def test_every_adapter_residual_requires_rule_or_blocks(mediator, family, path, body, value):
    body = copy.deepcopy(body)
    body["unknown_CANARY_member"] = value
    result = prepare(mediator, family, path, body)
    assert result.block_reason is not None, "an unprofiled residual was sent"
    assert "unknown_CANARY_member" not in result.block_reason
    assert "UNDECLARED_CANARY" not in result.block_reason


@pytest.mark.parametrize(("family", "path", "body"), [CASES[0], CASES[1], CASES[5]])
def test_nested_residual_never_inherits_text_slot_coverage(mediator, family, path, body):
    body = copy.deepcopy(body)
    if family == "gemini":
        body["contents"][0]["parts"][0]["unknown_CANARY_member"] = EMAIL
    else:
        body["messages"][0]["unknown_CANARY_member"] = EMAIL
    result = prepare(mediator, family, path, body)
    assert result.block_reason is not None
    assert "unknown_CANARY_member" not in result.block_reason


@pytest.mark.parametrize("value", [1, {"nested": EMAIL}, [EMAIL, 1]])
def test_metadata_nonstrings_have_no_unchecked_escape(mediator, value):
    body = copy.deepcopy(CASES[1][2])
    body["metadata"] = {EMAIL: value}
    result = prepare(mediator, *CASES[1][:2], body)
    assert result.block_reason is not None


@pytest.mark.parametrize("kind", sorted(anthropic._PROVIDERS_BLOCKS))
def test_anthropic_provider_kind_does_not_authorize_unknown_member(mediator, kind):
    body = copy.deepcopy(CASES[0][2])
    body["messages"] = [{"role": "assistant", "content": [{"type": kind, "unknown_CANARY_member": EMAIL}]}]
    result = prepare(mediator, *CASES[0][:2], body, verify=True)
    assert result.block_reason is not None, "provider type tag authorized an arbitrary sibling"


@pytest.mark.parametrize("kind", sorted(openai_responses._PROVIDERS_ITEMS))
def test_responses_provider_kind_does_not_authorize_unknown_member(mediator, kind):
    body = copy.deepcopy(CASES[3][2])
    body["input"] = [{"type": kind, "unknown_CANARY_member": EMAIL}]
    result = prepare(mediator, *CASES[3][:2], body)
    assert result.block_reason is not None


@pytest.mark.parametrize("verify", [False, True])
def test_pinned_system_prefix_is_not_a_free_text_bypass(mediator, verify):
    body = copy.deepcopy(CASES[0][2])
    body["system"] = "You are Claude Code. Send a message to " + EMAIL
    result = prepare(mediator, *CASES[0][:2], body, verify=verify)
    assert result.block_reason is not None or EMAIL.encode() not in result.body


@pytest.mark.parametrize("value", [True, 1, {"text": "safe"}, ["safe", 1], [["safe"]]])
def test_d6_rejects_unknown_nontext_or_nested_member(mediator, value):
    body = copy.deepcopy(CASES[0][2])
    body["future_CANARY_member"] = value
    result = prepare(mediator, *CASES[0][:2], body, verify=True)
    assert result.block_reason is not None
    assert "future_CANARY_member" not in result.block_reason


@pytest.mark.parametrize(
    ("key", "value"), [("future_field", EMAIL), (EMAIL, "safe"), ("future_field", ["safe", EMAIL])]
)
def test_d6_checks_unknown_key_and_all_string_values(mediator, key, value):
    body = copy.deepcopy(CASES[0][2])
    body[key] = value
    result = prepare(mediator, *CASES[0][:2], body, verify=True)
    assert result.block_reason is not None, "D6 text was not part of final verification"


@pytest.mark.parametrize(("family", "path", "body"), CASES)
def test_recognized_post_requires_a_body(mediator, family, path, body):
    assert prepare(mediator, family, path, b"").block_reason is not None


@pytest.mark.parametrize(
    "raw",
    [
        b'{"model":"m","messages":[{"role":"user","content":"half\\ud800"}]}',
        b'{"model":"m","messages":[],"k\\ud800":1,"k\\udfff":2}',
    ],
)
def test_verify_only_repaired_body_never_sends_original_bytes(mediator, raw):
    assert prepare(mediator, *CASES[0][:2], raw, verify=True).block_reason is not None


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize(
    "raw",
    [
        b'{"model":"m","messages":[{"role":"user","content":"emily.carter@acme.com","content":"safe"}]}',
        b'{"model":"m","messages":[],"messages":[{"role":"user","content":"safe"}]}',
    ],
)
def test_raw_duplicates_cannot_survive_a_complete_parsed_proof(mediator, verify, raw):
    result = prepare(mediator, *CASES[0][:2], raw, verify=verify)
    if verify:
        assert result.block_reason is not None
    else:
        assert result.block_reason is None
        assert result.body != raw and EMAIL.encode() not in result.body
        assert result.body.count(b'"messages"') == 1


@pytest.mark.parametrize("coding", ["gzip", "deflate", "br", "gzip, deflate"])
def test_valid_bounded_codings_preserve_readable_body(mediator, coding):
    raw = json.dumps(CASES[1][2]).encode()
    coded = codec.encode(raw, coding)
    result = mediator.prepare(
        Upstream("openai", "/openai", "https://api.openai.com"),
        "POST",
        CASES[1][1],
        {**HEADERS, "content-encoding": coding},
        coded,
    )
    try:
        assert result.block_reason is None
        # The document Latet read, encoded again, never the container it came in (G2 review I-1).
        decoded = codec.decode(result.body, coding, limit=1 << 20)
        assert json.loads(decoded) == json.loads(raw) and result.body == codec.encode(decoded, coding)
    finally:
        if result.lease is not None:
            result.lease.release()


def test_unknown_body_path_refuses_before_decoding(mediator, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unknown path reached content decoding")

    monkeypatch.setattr(codec, "decode", forbidden)
    result = prepare(mediator, "openai", "/openai/v1/files", b"not-json")
    assert result.block_reason is not None


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(extra: bytes = b"", *, trailer: bytes = b"") -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, "PNG")
    raw = stream.getvalue()
    return raw[:33] + extra + raw[33:] + trailer


def image_body(raw: bytes) -> dict[str, Any]:
    body = copy.deepcopy(CASES[0][2])
    body["messages"][0]["content"] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(raw).decode()},
        }
    ]
    return body


@pytest.mark.parametrize("verify", [False, True])
def test_ordinary_inline_image_with_ocr_still_passes(mediator, verify):
    mediator.vision = ImageMasking(FakeOcr())
    assert prepare(mediator, *CASES[0][:2], image_body(_png()), verify=verify).block_reason is None


@pytest.mark.parametrize(
    "chunk",
    [
        _chunk(b"tEXt", b"Comment\0" + EMAIL.encode()),
        _chunk(b"iTXt", b"Comment\0\0\0\0\0" + EMAIL.encode()),
        _chunk(b"zTXt", b"Comment\0\0" + zlib.compress(EMAIL.encode())),
    ],
)
def test_verify_only_image_metadata_is_checked(mediator, chunk):
    mediator.vision = ImageMasking(FakeOcr())
    result = prepare(mediator, *CASES[0][:2], image_body(_png(chunk)), verify=True)
    assert result.block_reason is not None, "metadata bypassed the text check"


@pytest.mark.parametrize(("extra", "trailer"), [(_chunk(b"vpAg", b"opaque"), b""), (b"", b"embedded-video")])
def test_bytes_after_png_iend_are_unread_remainder(mediator, extra, trailer):
    mediator.vision = ImageMasking(FakeOcr())
    result = prepare(mediator, *CASES[0][:2], image_body(_png(extra, trailer=trailer)), verify=True)
    assert result.block_reason is not None


def test_rewritten_clean_image_strips_all_textual_metadata(mediator):
    mediator.vision = ImageMasking(FakeOcr())
    raw = _png(_chunk(b"tEXt", b"Comment\0" + EMAIL.encode()), trailer=b"embedded-video")
    result = prepare(mediator, *CASES[0][:2], image_body(raw))
    assert result.block_reason is None
    carried = json.loads(result.body)["messages"][0]["content"][0]["source"]["data"]
    sent = base64.b64decode(carried)
    assert EMAIL.encode() not in sent and b"embedded-video" not in sent
    assert b"tEXt" not in sent


def test_an_image_without_ocr_refuses_with_recovery_words(mediator):
    result = prepare(mediator, *CASES[0][:2], image_body(_png()))
    assert result.block_reason is not None
    assert "latet ocr install" in result.block_reason and "new conversation" in result.block_reason


def test_the_production_gateway_does_not_send_unknown_body_members():
    body = copy.deepcopy(CASES[1][2])
    body["unknown_CANARY_member"] = "UNDECLARED_CANARY"
    with gateway() as (client, transport, _made, _mediator):
        response = client.post(CASES[1][1], json=body)
        assert response.status_code == 400
        assert not transport.sent


@pytest.mark.parametrize(("resource", "limit"), [("depth", 2), ("occurrences", 3), ("ledger_bytes", 1)])
def test_body_coverage_bounds_fail_closed_before_parse(mediator, monkeypatch, resource, limit):
    from dataclasses import replace

    from latet.gateway import coverage

    monkeypatch.setattr(coverage, "LIMITS", replace(coverage.LIMITS, **{resource: limit}))
    result = prepare(mediator, *CASES[1][:2], CASES[1][2])
    assert result.block_reason is not None
    assert result.envelope_refusal == "coverage-limit"


@pytest.mark.parametrize("where", ["tool-name", "schema-property"])
def test_semantic_names_that_need_masking_refuse_without_exposing_old_slots(mediator, where):
    body = copy.deepcopy(CASES[0][2])
    tool = {
        "name": "lookup",
        "description": "description",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
    if where == "tool-name":
        tool["name"] = EMAIL
    else:
        tool["input_schema"]["properties"] = {EMAIL: {"type": "string"}}
    body["tools"] = [tool]
    result = prepare(mediator, *CASES[0][:2], body)
    assert result.block_reason is not None, "semantic tool/schema name would leave unchecked or renamed"
    if where == "tool-name":  # a name outside its grammar: an unadmitted body keeps its category alone (3646b6e0)
        assert result.body == b"" and result.pending == [] and result.document is None, "nothing left to expose"
    else:  # a property name the final check stops: the request's masking slots stay selected
        assert result.pending, "old masking slots must remain selected on the refused request"


def _raster(fmt: str) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, fmt)
    return stream.getvalue()


def _jpeg_segment(marker: int, data: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(data) + 2) + data


def _exif_ascii(text: str) -> bytes:
    data = text.encode() + b"\0"
    return (
        b"Exif\0\0II*\0\x08\0\0\0"
        + struct.pack("<H", 1)
        + struct.pack("<HHII", 0x010E, 2, len(data), 26)
        + struct.pack("<I", 0)
        + data
    )


def _icc(text: str) -> bytes:
    data = b"desc\0\0\0\0" + struct.pack(">I", len(text) + 1) + text.encode() + b"\0"
    header = bytearray(128)
    header[:4] = struct.pack(">I", 144 + len(data))
    header[36:40] = b"acsp"
    return bytes(header) + struct.pack(">I", 1) + b"desc" + struct.pack(">II", 144, len(data)) + data


def _media_body(raw: bytes, media: str) -> dict[str, Any]:
    body = image_body(raw)
    body["messages"][0]["content"][0]["source"]["media_type"] = media
    return body


@pytest.mark.parametrize("kind", ["exif-ascii", "jpeg-com", "iptc-caption", "icc-desc", "gif-comment"])
def test_all_enumerated_image_text_stores_reach_final_check(mediator, kind):
    mediator.vision = ImageMasking(FakeOcr())
    raw, media = _raster("JPEG"), "image/jpeg"
    if kind == "exif-ascii":
        segment = _jpeg_segment(0xE1, _exif_ascii(EMAIL))
    elif kind == "jpeg-com":
        segment = _jpeg_segment(0xFE, EMAIL.encode())
    elif kind == "iptc-caption":
        record = b"\x1c\x02\x78" + struct.pack(">H", len(EMAIL)) + EMAIL.encode()
        resource = b"8BIM\x04\x04\0\0" + struct.pack(">I", len(record)) + record + (b"\0" if len(record) % 2 else b"")
        segment = _jpeg_segment(0xED, b"Photoshop 3.0\0" + resource)
    elif kind == "icc-desc":
        segment = _jpeg_segment(0xE2, b"ICC_PROFILE\0\x01\x01" + _icc(EMAIL))
    else:
        raw, media = _raster("GIF"), "image/gif"
        raw = raw[:-1] + b"!\xfe" + bytes([len(EMAIL)]) + EMAIL.encode() + b"\0;"
        segment = b""
    if segment:
        raw = raw[:2] + segment + raw[2:]
    result = prepare(mediator, *CASES[0][:2], _media_body(raw, media), verify=True)
    assert result.block_reason is not None, kind


@pytest.mark.parametrize("kind", ["thumbnail", "mpf", "jpeg-trailer", "gps", "maker-note"])
@pytest.mark.parametrize("verify", [False, True])
def test_embedded_or_unread_photo_bytes_are_refused_or_stripped(mediator, kind, verify):
    mediator.vision = ImageMasking(FakeOcr())
    raw = _raster("JPEG")
    marker = b"UNREAD_MEDIA_CANARY"
    if kind == "mpf":
        primary = _raster("JPEG")
        secondary = _raster("JPEG")
        secondary = secondary[:2] + _jpeg_segment(0xFE, marker) + secondary[2:]
        xmp = _jpeg_segment(0xE1, b'http://ns.adobe.com/xap/1.0/\0<rdf:Description hdrgm:Version="1.0"/>')
        # CIPA MP Index: version, two images, two complete 16-byte MP entries.
        tiff = b"II*\0\x08\0\0\0" + struct.pack("<H", 3)
        tiff += struct.pack("<HHI", 0xB000, 7, 4) + b"0100"
        tiff += struct.pack("<HHII", 0xB001, 4, 1, 2)
        tiff += struct.pack("<HHII", 0xB002, 7, 32, 50) + b"\0" * 4
        primary_size = len(primary) + len(xmp) + 90
        entries = struct.pack("<IIIHH", 0x030000, primary_size, 0, 0, 0)
        entries += struct.pack("<IIIHH", 0, len(secondary), primary_size - (2 + len(xmp) + 8), 0, 0)
        mpf = _jpeg_segment(0xE2, b"MPF\0" + tiff + entries)
        raw = primary[:2] + xmp + mpf + primary[2:] + secondary
        segment = b""
    elif kind == "thumbnail":
        # IFD0 empty; IFD1 has JPEG offset/length plus embedded-image payload.
        tiff = b"II*\0\x08\0\0\0\0\0" + struct.pack("<I", 14)
        tiff += (
            struct.pack("<H", 2)
            + struct.pack("<HHII", 0x0201, 4, 1, 44)
            + struct.pack("<HHII", 0x0202, 4, 1, len(marker))
            + b"\0" * 4
            + marker
        )
        segment = _jpeg_segment(0xE1, b"Exif\0\0" + tiff)
    elif kind in ("gps", "maker-note"):
        tag = 0x8825 if kind == "gps" else 0x927C
        tiff = (
            b"II*\0\x08\0\0\0"
            + struct.pack("<H", 1)
            + struct.pack("<HHII", tag, 7, len(marker), 26)
            + b"\0" * 4
            + marker
        )
        segment = _jpeg_segment(0xE1, b"Exif\0\0" + tiff)
    else:
        raw += marker
        segment = b""
    raw = raw[:2] + segment + raw[2:]
    result = prepare(mediator, *CASES[0][:2], _media_body(raw, "image/jpeg"), verify=verify)
    if verify:
        assert result.block_reason is not None, kind
        assert "new conversation" in result.block_reason
    else:
        assert result.block_reason is None, kind
        sent = base64.b64decode(json.loads(result.body)["messages"][0]["content"][0]["source"]["data"])
        assert marker not in sent, kind


def test_a_photo_refusal_says_how_to_recover(mediator):
    mediator.vision = ImageMasking(FakeOcr())
    raw = _raster("JPEG") + b"motion-video"
    result = prepare(mediator, *CASES[0][:2], _media_body(raw, "image/jpeg"), verify=True)
    assert result.block_reason is not None
    for words in ("new conversation", "iOS", "Preview", "Windows"):
        assert words in result.block_reason


def test_a_macos_screenshot_with_textual_metadata_passes_checked(mediator):
    mediator.vision = ImageMasking(FakeOcr())
    chunks = _chunk(b"iCCP", b"Display\0\0" + zlib.compress(_icc("Display profile")))
    chunks += _chunk(b"iDOT", struct.pack(">7I", 2, 0, 0, 0, 0, 0, 0))
    chunks += _chunk(b"pHYs", struct.pack(">IIB", 2835, 2835, 1))
    chunks += _chunk(
        b"iTXt",
        b"XML:com.adobe.xmp\0\0\0\0\0<x:xmpmeta xmlns:x='adobe:ns:meta/'><Software>Screenshot</Software></x:xmpmeta>",
    )
    result = prepare(mediator, *CASES[0][:2], image_body(_png(chunks)), verify=True)
    assert result.block_reason is None


@pytest.mark.parametrize(("fmt", "media"), [("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")])
def test_ordinary_known_image_containers_are_not_blanket_refused(mediator, fmt, media):
    mediator.vision = ImageMasking(FakeOcr())
    result = prepare(mediator, *CASES[0][:2], _media_body(_raster(fmt), media), verify=True)
    if fmt == "WEBP":
        assert "WebP" in result.block_reason
        _assert_webp_unsupported_then_rewritten(_raster(fmt))
    else:
        assert result.block_reason is None


def test_known_clean_request_has_forward_outcome(monkeypatch):
    from latet.gateway import forward
    from latet.gateway.outcome import Forward

    made = []
    real = forward._Turn

    def turn():
        item = real()
        made.append(item)
        return item

    monkeypatch.setattr(forward, "_Turn", turn)
    with gateway() as (client, transport, _clients, _mediator):
        response = client.post(CASES[1][1], json=CASES[1][2])
        assert response.status_code == 200 and len(transport.sent) == 1
    assert type(made[0].decision) is Forward


@pytest.fixture
def decision_turns(monkeypatch):
    from latet.gateway import forward

    made = []
    real = forward._Turn

    def turn():
        item = real()
        made.append(item)
        return item

    monkeypatch.setattr(forward, "_Turn", turn)
    return made


def _anthropic_gateway():
    return gateway(
        upstream=Upstream("anthropic", "/anthropic", "https://api.anthropic.com"),
        request_resources=(
            ("anthropic", "https://api.anthropic.com", "x-claude-code-session-id", CLAUDE["x-claude-code-session-id"]),
        ),
    )


@pytest.mark.parametrize("source", ["observed", "bridge"])
def test_model_written_allowance_has_its_actual_source_outcome(decision_turns, source):
    from latet.bridge.api import Bridge
    from latet.gateway.outcome import Forward, Pass

    text = "The model proposed the contact " + EMAIL
    body = copy.deepcopy(CASES[0][2])
    body["messages"].append({"role": "assistant", "content": text})
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        key = "cc:" + CLAUDE["x-claude-code-session-id"]
        if source == "observed":
            mediator.verifier.wrote(key, [text])
        else:
            Bridge(mediator, "test-bridge-token")._rehydrate(
                {"session": CLAUDE["x-claude-code-session-id"], "model": True, "items": [text]}
            )
        response = client.post(CASES[0][1], json=body, headers=CLAUDE)
        assert response.status_code == 200 and len(transport.sent) == 1
    decision = decision_turns[-1].decision
    if source == "observed":
        assert type(decision) is Forward
    else:
        assert type(decision) is Pass
        assert "reported as written by the model" in str(decision.evidence)


def test_history_that_shows_no_known_value_is_forward(decision_turns):
    from latet.gateway.outcome import Forward

    with gateway() as (client, transport, _clients, _mediator):
        for index in range(3):
            body = copy.deepcopy(CASES[1][2])
            body["messages"] += [
                {"role": "assistant", "content": "A harmless answer"},
                {"role": "user", "content": "continue " + str(index)},
            ]
            assert client.post(CASES[1][1], json=body).status_code == 200
        assert len(transport.sent) == 3
    assert all(type(turn.decision) is Forward for turn in decision_turns)


def test_a_known_value_in_clear_sent_before_is_a_visible_pass(decision_turns):
    from latet.gateway.outcome import Pass

    text = "Previously sent contact " + EMAIL
    body = copy.deepcopy(CASES[0][2])
    body["messages"][0]["content"] = text
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        key = "gw|cc:" + CLAUDE["x-claude-code-session-id"]
        mediator.verifier.left(key, [text])
        for _ in range(2):
            assert client.post(CASES[0][1], json=body, headers=CLAUDE).status_code == 200
        assert len(transport.sent) == 2
    for turn in decision_turns:
        assert type(turn.decision) is Pass
        assert "sent before in this conversation" in str(turn.decision.evidence)


@pytest.mark.parametrize("value", ["safe future text", ["safe", "future"]])
def test_d6_unknown_text_has_checked_only_outcome(decision_turns, value):
    from latet.gateway.outcome import Pass

    body = copy.deepcopy(CASES[0][2])
    body["future_field"] = value
    with _anthropic_gateway() as (client, transport, _clients, _mediator):
        response = client.post(CASES[0][1], json=body, headers=CLAUDE)
        assert response.status_code == 200 and len(transport.sent) == 1
        assert json.loads(transport.sent[0].content) == body
    assert type(decision_turns[-1].decision) is Pass
    assert "checked only" in str(decision_turns[-1].decision.evidence)


@pytest.mark.parametrize("mutation", ["text", "image"])
def test_review_amendment_invalidates_proof_even_with_same_mask_count(mutation, monkeypatch):
    from latet.gateway import outcome
    from latet.gateway.outcome import NotSendable

    from .test_request_outcomes import _chat, _envelope, _prepared, _strict

    reader = FakeOcr()
    data = json.loads(_chat("write to " + EMAIL, image=reader.screenshot(["ordinary image"])))
    data["model"] = "gpt-4o"
    raw = json.dumps(data).encode()
    mediator = _mediator(reader)
    prepared = _prepared(mediator, raw)
    try:
        envelope = _envelope(raw)
        decision = _strict(prepared, envelope)
        assert isinstance(decision, outcome.Forward | outcome.Pass)
        assert outcome.sendable(decision, prepared, envelope) is decision
        before = prepared.masked_count
        if mutation == "text":
            slot = prepared.pending[0][0]
            slot.put(slot.get() + " changed after approval")
        else:
            prepared.images[0].withhold("changed after approval")
        assert prepared.masked_count == before
        with pytest.raises(NotSendable, match="stale"):
            outcome.sendable(decision, prepared, envelope)
        # Named in-memory scratch mutation proves the same assertion notices lost freshness protection.
        monkeypatch.setattr(outcome, "_unchanged", lambda arena, seal: True)
        assert outcome.sendable(decision, prepared, envelope) is decision
    finally:
        if prepared.lease is not None:
            prepared.lease.release()
        mediator.close()


@pytest.mark.parametrize(
    "inner",
    [
        b'{"messages":[{"role":"user","content":"half\\ud800"}]}',
        b'{"messages":[{"role":"user","content":"sensitive","content":"safe"}]}',
    ],
)
def test_raw_duplicates_and_repairs_in_count_wrapper_cannot_escape(mediator, inner):
    body = {"input": {"invokeModel": {"body": base64.b64encode(inner).decode()}}}
    result = prepare(mediator, "bedrock", "/bedrock/us-east-1/model/anthropic.claude-v2/count-tokens", body)
    assert result.block_reason is not None


@pytest.mark.parametrize("body", [[1, 2], 0, None, "not-object"])
def test_recognized_nonobject_shape_is_refused_on_both_machines(mediator, body):
    assert prepare(mediator, *CASES[1][:2], body).block_reason is not None


@pytest.mark.parametrize("kind", ["document", "remote-image", "audio", "file"])
def test_unread_media_never_inherits_an_allow_setting(mediator, kind):
    body = copy.deepcopy(CASES[1][2])
    part = {
        "document": {"type": "input_file", "file_data": "ZmlsZQ=="},
        "remote-image": {"type": "image_url", "image_url": {"url": "https://example.invalid/private-image.png"}},
        "audio": {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "wav"}},
        "file": {"type": "file", "file": {"file_id": "file-123"}},
    }[kind]
    body["messages"][0]["content"] = [part]
    assert prepare(mediator, *CASES[1][:2], body).block_reason is not None


def test_an_allow_rule_has_forward_policy_evidence(decision_turns):
    from latet.engine.types import Policy
    from latet.gateway.outcome import Forward

    body = copy.deepcopy(CASES[1][2])
    body["messages"][0]["content"] = EMAIL
    with gateway() as (client, transport, _clients, mediator):
        mediator.verifier.masker.policies["EMAIL"] = Policy.ALLOW
        response = client.post(CASES[1][1], json=body)
        assert response.status_code == 200 and EMAIL.encode() in transport.sent[0].content
    assert type(decision_turns[-1].decision) is Forward
    assert "configured policy" in getattr(decision_turns[-1].decision.proof, "authority", ())


@pytest.mark.parametrize("events", [("observed", "bridge"), ("bridge", "observed")])
def test_observed_authority_survives_both_source_orders_and_new_instance(decision_turns, events):
    from latet.bridge.api import Bridge
    from latet.engine.verifier import Verifier
    from latet.gateway.outcome import Forward
    from latet.state.local import LocalConversations

    text = "Model-authored contact " + EMAIL
    body = copy.deepcopy(CASES[0][2])
    body["messages"].append({"role": "assistant", "content": text})
    with _anthropic_gateway() as (client, _transport, _clients, mediator):
        store = LocalConversations()
        mediator.verifier.shared = store
        for event in events:
            if event == "observed":
                mediator.verifier.wrote("cc:" + CLAUDE["x-claude-code-session-id"], [text])
            else:
                Bridge(mediator, "test-bridge-token")._rehydrate(
                    {"session": CLAUDE["x-claude-code-session-id"], "model": True, "items": [text]}
                )
        assert client.post(CASES[0][1], json=body, headers=CLAUDE).status_code == 200
        another = Verifier(mediator.verifier.masker, given=mediator.verifier.given)
        another.shared = store
        mediator.verifier = another
        assert client.post(CASES[0][1], json=body, headers=CLAUDE).status_code == 200
    assert all(type(turn.decision) is Forward for turn in decision_turns)
    assert all("provider-observed text" in getattr(turn.decision.proof, "authority", ()) for turn in decision_turns)


def test_ambiguous_old_written_record_never_gains_observed_authority(decision_turns):
    from latet.gateway.outcome import Pass
    from latet.state.local import LocalConversations

    text = "Historical model contact " + EMAIL
    body = copy.deepcopy(CASES[0][2])
    body["messages"].append({"role": "assistant", "content": text})
    with _anthropic_gateway() as (client, _transport, _clients, mediator):
        store = LocalConversations()
        mediator.verifier.shared = store
        key = mediator.verifier._written_key("cc:" + CLAUDE["x-claude-code-session-id"], text)
        store.remember(key, "w")
        assert client.post(CASES[0][1], json=body, headers=CLAUDE).status_code == 200
    assert type(decision_turns[-1].decision) is Pass
    assert "reported as written by the model" in str(decision_turns[-1].decision.evidence)


def test_late_learning_invalidates_proof_and_freeze_cannot_clear_refusal(mediator):
    from latet.engine.types import EntityClass
    from latet.engine.vault import Entity
    from latet.gateway import outcome

    from .test_request_outcomes import _envelope

    raw = json.dumps(CASES[1][2]).encode()
    arena = mediator.prepare(
        Upstream("openai", "/openai", "https://api.example.invalid"),
        "POST",
        CASES[1][1],
        {"content-type": "application/json"},
        raw,
    )
    try:
        assert arena.block_reason is None
        mediator.engine.vault.learn(Entity("late", "TERM", EntityClass.CUSTOM, "hello", "masked-late", 1, 1.0))
        arena.freeze_inputs()
        decision = outcome.decide(arena, _envelope(raw))
        assert isinstance(decision, outcome.Block)
    finally:
        if arena.lease is not None:
            arena.lease.release()


def test_raw_occurrence_mapping_preserves_duplicate_and_removed_charges():
    from dataclasses import replace

    from latet.gateway.body_coverage import BodyRefused, Ledger
    from latet.gateway.coverage import LIMITS

    ledger = Ledger(limits=replace(LIMITS, occurrences=12), envelope=2)
    ledger.preflight(b'{"a":1,"a":2,"b":[3]}')
    ledger.account({"a": 2, "b": [3]})
    assert len(ledger.raw) == 8
    assert ledger.final[("a",)][0] == 4  # raw fifth occurrence, the retained duplicate value
    assert ledger.final[("b", 0)][0] == 7
    ledger.account({"a": 2, "b": [3], "c": 4})
    assert len(ledger.generated) == 2  # generated key and value consume the exact remaining allowance
    ledger.account({"a": 2})
    assert len(ledger.raw) + len(ledger.generated) + ledger.envelope == 12
    with pytest.raises(BodyRefused, match="limit"):
        ledger.account({"a": 2, "d": 5})


def test_surrogate_key_collisions_keep_distinct_raw_entries():
    from latet.gateway.body_coverage import Ledger

    ledger = Ledger()
    ledger.preflight(b'{"k\\ud800":1,"k\\udfff":2}')
    ledger.account({"k\ufffd": 2})
    assert len(ledger.raw) == 5
    assert ledger.final[("k\ufffd",)][0] == 4


def test_envelope_retention_is_part_of_same_body_ledger_limit():
    from dataclasses import replace

    from latet.gateway.body_coverage import BodyRefused, Ledger
    from latet.gateway.coverage import LIMITS

    ledger = Ledger(limits=replace(LIMITS, ledger_bytes=600), envelope=2)
    with pytest.raises(BodyRefused, match="limit"):
        ledger.preflight(b"{}")


@pytest.mark.parametrize("nested", [False, True])
def test_ws_turn_residual_uses_same_closed_body_rules(mediator, nested):
    session = mediator.engine.session("ws-body-coverage")
    body = {"model": "gpt-4o", "input": "hello", "future_CANARY": "unknown"}
    frame = {"type": "response.create", "response": body} if nested else {"type": "response.create", **body}
    _, prepared = mediator.mask_ws_frame(json.dumps(frame), session)
    try:
        assert prepared is not None and prepared.block_reason is not None
    finally:
        if prepared is not None and prepared.lease is not None:
            prepared.lease.release()


def test_inline_image_parts_share_the_exchange_occurrence_limit(mediator, monkeypatch):
    from dataclasses import replace

    from latet.gateway import coverage

    mediator.vision = ImageMasking(FakeOcr())
    monkeypatch.setattr(coverage, "LIMITS", replace(coverage.LIMITS, occurrences=23))
    # The body has exactly23JSON occurrences; even its first PNG chunk is additional.
    result = prepare(mediator, *CASES[0][:2], image_body(_png()), verify=True)
    assert result.envelope_refusal == "coverage-limit"


def test_stacked_decoding_charges_every_decoded_stage():
    raw = b"x" * 120
    inner = gzip.compress(raw)
    encoded = gzip.compress(inner)
    assert len(inner) < 120
    with pytest.raises(codec.DecodedTooLarge):
        codec.decode(encoded, "gzip, gzip", limit=120)
    assert codec.decode(gzip.compress(raw), "gzip", limit=120) == raw


def test_residual_edit_cannot_be_hidden_by_refreezing(mediator):
    from latet.gateway import outcome

    from .test_request_outcomes import _envelope

    raw = json.dumps({**CASES[1][2], "temperature": 0.2}).encode()
    arena = mediator.prepare(
        Upstream("openai", "/openai", "https://api.example.invalid"), "POST", CASES[1][1], HEADERS, raw
    )
    try:
        assert arena.block_reason is None
        arena.document["temperature"] = 0.7
        arena.freeze_inputs()
        decision = outcome.decide(arena, _envelope(raw))
        assert isinstance(decision, outcome.Block)
    finally:
        if arena.lease is not None:
            arena.lease.release()


def test_shown_and_sent_cached_assistant_history_is_visible_on_every_reuse(decision_turns):
    from latet.engine.types import EntityClass
    from latet.engine.vault import Entity
    from latet.gateway.outcome import Forward, Pass

    body = copy.deepcopy(CASES[1][2])
    body["messages"].append({"role": "assistant", "content": "The label is CrimsonHarbor"})
    with gateway() as (client, transport, _clients, mediator):
        assert client.post(CASES[1][1], json=body).status_code == 200
        mediator.engine.vault.learn(
            Entity("history-label", "TERM", EntityClass.CUSTOM, "CrimsonHarbor", "HiddenLabel", 1, 1.0)
        )
        for _ in range(2):
            assert client.post(CASES[1][1], json=body).status_code == 200
            assert b"CrimsonHarbor" in transport.sent[-1].content
    assert type(decision_turns[0].decision) is Forward
    assert all(
        type(turn.decision) is Pass and "sent before in this conversation" in str(turn.decision.evidence)
        for turn in decision_turns[1:]
    )


def test_icc_rendering_tag_does_not_grant_arbitrary_trailing_payload():
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    payload = b"XYZ \0\0\0\0" + b"\0" * 12 + b"UNREAD_TEXT_CANARY"
    header = bytearray(128)
    header[:4] = struct.pack(">I", 144 + len(payload))
    header[36:40] = b"acsp"
    profile = bytes(header) + struct.pack(">I4sII", 1, b"rXYZ", 144, len(payload)) + payload
    jpeg = _raster("JPEG")
    raw = jpeg[:2] + _jpeg_segment(0xE2, b"ICC_PROFILE\0\1\1" + profile) + jpeg[2:]
    with pytest.raises(UnreadImage):
        inspect_image(base64.b64encode(raw).decode())


@pytest.mark.parametrize("extra", [{}, {"unknown_CANARY": "safe"}])
def test_fallback_models_have_closed_schema_siblings(mediator, extra):
    body = copy.deepcopy(CASES[0][2])
    body["fallbacks"] = [
        {
            "model": "claude-sonnet-4-6",
            "output_config": {
                "effort": "high",
                "format": {
                    "type": "json_schema",
                    "schema": {"type": "object", "properties": {"status": {"type": "string", "description": EMAIL}}},
                },
            },
            **extra,
        }
    ]
    result = prepare(mediator, *CASES[0][:2], body)
    if extra:
        assert result.block_reason is not None
    else:
        assert result.block_reason is None
        assert EMAIL.encode() not in result.body


def test_generated_output_is_bounded_before_serialization(mediator, monkeypatch):
    body = copy.deepcopy(CASES[1][2])
    body["messages"][0]["content"] = EMAIL
    raw = json.dumps(body).encode()
    mediator.body_limit = len(raw) + 8
    real_mask = mediator._mask_slots

    def expanded(*args, **kwargs):
        count, done, lease = real_mask(*args, **kwargs)
        args[0][0].put("x" * (mediator.body_limit + 1))
        return count, done, lease

    monkeypatch.setattr(mediator, "_mask_slots", expanded)
    with pytest.raises(codec.DecodedTooLarge):
        mediator.prepare(
            Upstream("openai", "/openai", "https://api.example.invalid"), "POST", CASES[1][1], HEADERS, raw
        )


def test_coverage_shares_existing_outer_call_and_two_verifier_boundaries(monkeypatch):
    from contextlib import contextmanager

    from latet.engine.verifier import Verifier
    from latet.gateway import mediate

    from .test_stored_vault import _engine, _Store, _vault

    vault = _vault(_Store())
    engine = _engine(vault)
    verifier = Verifier(engine.masker)
    mediator = Mediator(engine, verifier)
    outer, budgets, checks = [], [], []
    real_hold, real_verify = vault.holding, verifier.verify

    @contextmanager
    def counted_hold(**kwargs):
        first = vault._call() is None
        with real_hold(**kwargs):
            if first:
                outer.append(vault._call())
            budgets.append(vault._call().lookup)
            yield

    def counted_verify(*args, **kwargs):
        checks.append(vault._call().lookup)
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(vault, "holding", counted_hold)
    monkeypatch.setattr(verifier, "verify", counted_verify)
    body = {**CASES[1][2], "metadata": {"category": "ordinary description"}}
    raw = json.dumps(body).encode()
    upstream = Upstream("openai", "/openai", "https://api.example.invalid")

    def check():
        outer.clear()
        budgets.clear()
        checks.clear()
        arena = mediator.prepare_http(upstream, "POST", CASES[1][1], HEADERS, raw, ("envelope words",))
        try:
            assert arena.block_reason is None
            assert len(outer) == 1
            assert len(checks) == 2  # accepted body-final check and P2-1 envelope check
            assert all(budget is checks[0] for budget in [*budgets, *checks])
        finally:
            if arena.lease is not None:
                arena.lease.release()

    try:
        check()
        real_walk = mediate.inspect_body

        def duplicate_final_check(*args, **kwargs):
            verifier.verify((), "scratch-extra-verifier", record=False)
            return real_walk(*args, **kwargs)

        monkeypatch.setattr(mediate, "inspect_body", duplicate_final_check)
        with pytest.raises(AssertionError):
            check()  # named mutation: a coverage-only extra verifier is detected
    finally:
        mediator.close()


_ORACLE_NORMALIZER = r"""
import hashlib

def normalize_mask(text, restored, replacements, exact, entities, aliases, originals):
    # Normalize only replacement spans actually emitted by this masking path.
    # A vault-looking substring elsewhere is ordinary literal text, even if the
    # vault happens to contain it. Original offsets are mapped to outgoing offsets.
    entities = {entity.id: entity for entity in entities}
    pieces, used = [], set()
    cursor, previous, delta = 0, 0, 0
    for replacement in replacements:
        assert previous <= replacement.start <= replacement.end
        assert replacement.end - replacement.start == len(replacement.original)
        start = replacement.start + delta
        end = start + len(replacement.replacement)
        assert cursor <= start <= end <= len(text)
        assert text[start:end] == replacement.replacement
        pieces.append(text[cursor:start])
        if str(replacement.policy) == "mask":
            entity = entities[replacement.entity_id]
            assert (entity.original, entity.etype, entity.eclass, entity.surrogate) == (
                replacement.original, replacement.etype, replacement.eclass, replacement.replacement)
            identity = entity.etype + ":" + hashlib.sha256(entity.original.encode()).hexdigest()
            assert aliases.setdefault(entity.surrogate, identity) == identity
            assert originals.setdefault(identity, entity.surrogate) == entity.surrogate
            pieces.append("<MASK:" + identity + ">")
            used.add(identity)
        else:
            pieces.append(text[start:end])  # withholding is literal and remains a policy/span change
        cursor, previous = end, replacement.end
        delta += len(replacement.replacement) - (replacement.end - replacement.start)
    pieces.append(text[cursor:])
    canonical = "".join(pieces)
    spans = [[r.start, r.end, r.original, r.etype, int(r.eclass), str(r.policy), r.rule] for r in replacements]
    return {"literal": text, "canonical": canonical, "restored": restored, "spans": spans,
            "aliases": sorted(used), "exact": exact}
"""


def _normalize_oracle_mask(*args):
    namespace = {}
    exec(_ORACLE_NORMALIZER, namespace)
    return namespace["normalize_mask"](*args)


def _same_oracle_mask(original, was, now):
    assert now is not None
    assert was["restored"] == now["restored"] == original
    assert {k: v for k, v in now.items() if k != "literal"} == {k: v for k, v in was.items() if k != "literal"}


_ORACLE = (
    _ORACLE_NORMALIZER
    + r"""
import base64, hashlib, inspect, json, pathlib, sys, urllib.parse
root, fixture, manifest, flavor = map(pathlib.Path, sys.argv[1:5])
sys.path.insert(0, str(root / "src"))
pins = json.loads(manifest.read_text())
for name, expected in pins.items():
    data = (root / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected, name
from latet.config import Config, IdentityConfig
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.engine.rehydrate import restore_text
from latet.engine import wellformed
from latet.gateway import mediate
from latet.gateway.routes import Upstream
from latet.protocols import codec, openai_responses, anthropic
from latet.protocols.base import BodyScan

def locations(root):
    found = {}
    def walk(value, path):
        if isinstance(value, dict):
            found[id(value)] = path
            for index, child in enumerate(value.values()):
                walk(child, (*path, "field", index))
        elif isinstance(value, list):
            found[id(value)] = path
            for index, child in enumerate(value):
                walk(child, (*path, "item", index))
    walk(root, ())
    return found

def describe(slots, document):
    paths = locations(document)
    out = []
    for slot in slots:
        path = paths.get(id(slot.container))
        if path is None:
            continue  # OCR's private text is covered by the independent image cases
        index = list(slot.container).index(slot.key) if isinstance(slot.container, dict) else slot.key
        out.append((json.dumps([*path, index, slot.kind.value, slot.mode, slot.from_model]), slot.get()))
    return out

out = {}
for row in json.loads(fixture.read_text()):
    raw = base64.b64decode(row["raw"])
    headers = dict(row["headers"])
    path = urllib.parse.unquote(row["target"].split("?", 1)[0])
    family = path.strip("/").split("/")[0] or "openai"
    upstream = Upstream(family, "/"+family, "https://api.example.invalid", envelope_expected=frozenset({("model", "m")}))
    built = build(Config(profile="balanced", ner="off", identity=IdentityConfig(enabled=False)), Vault(b"\7"*32))
    mediator = mediate.Mediator(built.engine, built.verifier)
    mediator.on_server = row["machine"] == "server"
    if mediator.on_server:
        mediator.body_limit = 32 << 20
    prepared = None
    selected = []
    try:
        try:
            doc = wellformed.read(codec.decode(raw, headers.get("content-encoding"))).doc
            if row["transport"] == "ws":
                protocol = "responses"
                scan = openai_responses.scan_turn(doc) if isinstance(doc, dict) else BodyScan()
                tools = BodyScan()
                for level in (doc, doc.get("response") if isinstance(doc, dict) else None):
                    if isinstance(level, dict):
                        mediate._defined("responses", level, tools)
                scan.first(tools)
            else:
                protocol = mediate.protocol_for(upstream, row["method"], path)
                if mediate.shape_of(row["method"], path) == "bedrock-count":
                    doc = mediate._counted(doc)
                scan = mediate._scan(protocol, doc) if protocol and isinstance(doc, dict) else BodyScan()
                if protocol and isinstance(doc, dict):
                    only = protocol == "anthropic" and (mediate.shape_of(row["method"], path) is not None or anthropic.is_claude_code(headers, doc) or "sk-ant-oat" in headers.get("authorization", ""))
                    if str(flavor) == "current" or mediator.on_server or not (only and anthropic.is_claude_code(headers, doc)):
                        tools = BodyScan()  # as Mediator.prepare reads definitions since 3646b6e0
                        mediate._defined(protocol, doc, tools)
                        scan.first(tools)
            selected = describe(scan.slots, doc)
        except (ValueError, TypeError, AttributeError, OSError, EOFError):
            pass
        if row["transport"] == "ws":
            session = mediator.engine.session("oracle-ws")
            if str(flavor) == "current":
                _, prepared = mediator.mask_ws_frame(raw.decode(), session, upstream)
            else:
                _, prepared = mediator.mask_ws_frame(raw.decode(), session)
        else:
            prepared = mediator.prepare(upstream, row["method"], path, headers, raw)
        final = {}
        aliases, originals = {}, {}
        if prepared is not None and not prepared.block_reason:
            for slot, replacements, exact in prepared.pending:
                described = describe([slot], prepared.document)
                if not described:
                    continue
                selector, text = described[0]
                mode = json.loads(selector)[-2]
                restored = restore_text(text, mediator.engine.restores(prepared.session, json_mode=mode == "json"))[0] if prepared.session else text
                final[selector] = normalize_mask(text, restored, replacements, exact, built.vault.entities(), aliases, originals)
        out[row["id"]] = {"selected": dict(selected), "blocked": prepared is not None and bool(prepared.block_reason), "final": final}
    except Exception as error:
        out[row["id"]] = {"selected": dict(selected), "error": type(error).__name__, "blocked": True, "final": {}}
    finally:
        if prepared is not None and prepared.lease is not None:
            prepared.lease.release()
        mediator.close()
for name, module in tuple(sys.modules.items()):
    if name == "latet" or name.startswith("latet."):
        origin = getattr(module, "__file__", None)
        if origin:
            assert pathlib.Path(origin).resolve().is_relative_to((root / "src" / "latet").resolve()), (name, origin)
print(json.dumps(out, sort_keys=True))
"""
)


def test_never_less_slots_and_masks_against_independent_baseline_fixture(tmp_path):
    """Portable observations captured from the complete isolated accepted baseline package.

    The ignored capture is evidence only: normal CI needs neither it nor a private Git object.
    Direct preparation is compared here; setup-sensitive actual-path tests are separate.
    """
    import hashlib
    import subprocess
    import sys
    from pathlib import Path

    # The oracle was captured from 0.10's table, whose requests name 0.10's placeholders (``INPUTS_0_10``).
    from ..request_coverage import GOLDEN
    from ..request_coverage import TABLE_0_10 as TABLE

    root = Path(__file__).resolve().parents[2]
    source = root / "tests/data/request-coverage/p2-2/body-oracle.json.gz"
    capture = json.loads(gzip.decompress(source.read_bytes()))
    evidence = capture["evidence"]
    assert hashlib.sha256(GOLDEN.read_bytes()).hexdigest() == evidence["golden_sha256"]
    rows = []
    for case in TABLE:
        if case.transport == "bridge" or not isinstance(case.body, bytes):
            continue
        for machine in case.machines:
            inputs = case.frames if case.transport == "ws" else (case.body,)
            for index, raw in enumerate(inputs):
                if isinstance(raw, str):
                    raw = raw.encode()
                rows.append(
                    {
                        "id": f"{case.id}/{machine}/{index}",
                        "machine": machine,
                        "method": case.method,
                        "target": case.target,
                        "headers": [list(pair) for pair in case.headers],
                        "transport": case.transport,
                        "raw": base64.b64encode(raw).decode(),
                    }
                )
    assert rows == capture["inputs"], "the immutable synthetic input inventory changed"
    fixture = tmp_path / "fixtures.json"
    fixture.write_text(json.dumps(rows), encoding="utf-8")
    script = tmp_path / "oracle.py"
    script.write_text(_ORACLE, encoding="utf-8")
    pins = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "src/latet").rglob("*.py")
    }
    manifest = tmp_path / "current-manifest.json"
    manifest.write_text(json.dumps(pins, sort_keys=True), encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-I", "-B", str(script), str(root), str(fixture), str(manifest), "current"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr[-3000:]
    before, after = capture["baseline"], json.loads(run.stdout)
    added, refused, allocation_only = {}, [], []

    def subset(old_slots, new_slots):
        assert old_slots.keys() <= new_slots.keys()
        assert all(new_slots[key] == value for key, value in old_slots.items())

    for name, was in before.items():
        now = after[name]
        assert now.get("error") in (None, was.get("error")), name
        subset(was["selected"], now["selected"])
        added[name] = len(now["selected"]) - len(was["selected"])
        if now["blocked"]:
            if not was["blocked"]:
                refused.append(name)
            continue
        assert not was["blocked"], (name, "previous refusal became sendable")
        for selector, value in was["final"].items():
            found = now["final"].get(selector)
            _same_oracle_mask(was["selected"][selector], value, found)
            if found["literal"] != value["literal"]:
                allocation_only.append([name, selector])
    # Named omitted-slot and wrong-restore mutants independently challenge the comparison.
    witness = next(name for name in before if before[name]["selected"])
    mutant = dict(after[witness]["selected"])
    mutant.pop(next(iter(before[witness]["selected"])))
    with pytest.raises(AssertionError):
        subset(before[witness]["selected"], mutant)
    witness = next(name for name in before if before[name]["final"] and not after[name]["blocked"])
    selector, was = next(iter(before[witness]["final"].items()))
    mutant = {**after[witness]["final"][selector], "restored": "wrong entity original"}
    with pytest.raises(AssertionError):
        _same_oracle_mask(before[witness]["selected"][selector], was, mutant)
    # Production-seam mutants run one immutable captured case in a fresh child.
    # They change current code behavior in memory, never fixture observations or files.
    witness = next(
        name
        for name in before
        if before[name]["selected"]
        and before[name]["final"]
        and not after[name]["blocked"]
        and any(v["spans"] for v in before[name]["final"].values())
        and next(row for row in rows if row["id"] == name)["transport"] == "http"
    )
    one = next(row for row in rows if row["id"] == witness)
    fixture.write_text(json.dumps([one]), encoding="utf-8")
    mutations = {
        "current-scanner-omitted-slot": """
real_scan = mediate._scan
def omitted_slot(*args):
    scan = real_scan(*args)
    if scan.slots:
        scan.slots.pop(0)
    return scan
mediate._scan = omitted_slot
""",
        "current-restore-wrong-original": """
import latet.engine.rehydrate as rehydrate
real_restore = rehydrate.restore_text
def wrong_restore(*args, **kwargs):
    restored, count = real_restore(*args, **kwargs)
    return restored + ' WRONG_ORIGINAL', count
rehydrate.restore_text = wrong_restore
restore_text = rehydrate.restore_text
""",
    }
    for mutant_name, patch in mutations.items():
        script.write_text(_ORACLE.replace("out = {}\nfor row", patch + "\nout = {}\nfor row"), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-I", "-B", str(script), str(root), str(fixture), str(manifest), "current"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert run.returncode == 0, run.stderr[-3000:]
        changed = json.loads(run.stdout)[witness]
        assert changed.get("error") is None, changed
        if mutant_name == "current-scanner-omitted-slot":
            with pytest.raises(AssertionError):
                subset(before[witness]["selected"], changed["selected"])
        else:
            assert not changed["blocked"]
            selector, original_mask = next(iter(before[witness]["final"].items()))
            with pytest.raises(AssertionError):
                _same_oracle_mask(before[witness]["selected"][selector], original_mask, changed["final"].get(selector))
        print("P22_SOURCE_MUTANT " + json.dumps({"name": mutant_name, "case": witness, "detected": True}))
    print(
        "P22_NEVER_LESS "
        + json.dumps(
            {
                "cases": len(rows),
                "added": {k: v for k, v in added.items() if v},
                "new_refusals": refused,
                "allocation_only": allocation_only,
                "oracle_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        )
    )


def test_count_wrapper_inner_is_bounded_before_json_materialization(mediator, monkeypatch):
    from dataclasses import replace

    from latet.gateway import coverage, mediate

    monkeypatch.setattr(coverage, "LIMITS", replace(coverage.LIMITS, depth=5))
    inner = b'{"messages":[{"role":"user","content":[[[[["hello"]]]]]}]}'
    body = {"input": {"invokeModel": {"body": base64.b64encode(inner).decode()}}}
    reads = []
    real = mediate.wellformed.read

    def read(data):
        reads.append(data)
        return real(data)

    monkeypatch.setattr(mediate.wellformed, "read", read)
    result = prepare(mediator, "bedrock", "/bedrock/us-east-1/model/anthropic.claude-v2/count-tokens", body)
    assert result.block_reason is not None
    assert len(reads) == 1, "inner JSON was allocated before depth admission"
    assert result.envelope_refusal == "coverage-limit"


@pytest.mark.parametrize("kind", ["webp-padding", "icc-mluc-gap", "jpeg-dqt-tail", "png-trns-tail"])
def test_container_rendering_grammar_has_no_unread_tail(kind):
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    if kind == "webp-padding":
        payload = b"XMP " + struct.pack("<I", 3) + b"abc" + b"X"
        raw = b"RIFF" + struct.pack("<I", len(payload) + 4) + b"WEBP" + payload
    elif kind == "icc-mluc-gap":
        payload = (
            b"mluc\0\0\0\0" + struct.pack(">II", 1, 12) + b"enUS" + struct.pack(">II", 4, 32) + b"HIDE" + b"\0o\0k"
        )
        header = bytearray(128)
        header[:4] = struct.pack(">I", 144 + len(payload))
        header[36:40] = b"acsp"
        profile = bytes(header) + struct.pack(">I4sII", 1, b"desc", 144, len(payload)) + payload
        jpeg = _raster("JPEG")
        raw = jpeg[:2] + _jpeg_segment(0xE2, b"ICC_PROFILE\0\1\1" + profile) + jpeg[2:]
    elif kind == "jpeg-dqt-tail":
        jpeg = _raster("JPEG")
        raw = jpeg[:2] + _jpeg_segment(0xDB, b"\0" + b"\1" * 64 + b"UNREAD") + jpeg[2:]
    else:
        png = _raster("PNG")
        raw = png[:33] + _chunk(b"tRNS", b"UNREAD_CANARY") + png[33:]
    with pytest.raises(UnreadImage):
        inspect_image(base64.b64encode(raw).decode())


def test_body_limit_refusal_mints_422_without_sending(monkeypatch, decision_turns):
    from dataclasses import replace

    from latet.gateway import coverage
    from latet.gateway.outcome import Block

    with gateway() as (client, transport, _clients, _mediator):
        monkeypatch.setattr(coverage, "LIMITS", replace(coverage.LIMITS, depth=2))
        response = client.post(CASES[1][1], json=CASES[1][2], headers=HEADERS)
        assert response.status_code == 422 and transport.sent == []
    assert isinstance(decision_turns[-1].decision, Block)


def test_an_off_scope_refuses_and_keeps_nothing_to_send(mediator, monkeypatch):
    """D5: a project where Latet is off refuses its requests; 0.11 sends none of them unread (P2-6)."""
    from latet.engine.pipeline import Scope
    from latet.gateway.mediate import OFF_THERE

    scope = Scope("off", "off", mediator.engine.masker, enabled=False)
    monkeypatch.setattr(mediator.engine, "scope", lambda session: scope)
    body = {**CASES[1][2], "unknown_CANARY": EMAIL}
    prepared = prepare(mediator, *CASES[1][:2], body)
    assert prepared.off and prepared.block_reason == OFF_THERE
    assert EMAIL.encode() not in prepared.body and prepared.body == b""
    assert prepared.body_proof is None


@pytest.mark.parametrize("server", [False, True])
def test_authenticated_bridge_reporting_preserves_authority_boundary(mediator, server):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from latet.bridge.api import Bridge
    from latet.gateway.app import OnServer

    bridge = Bridge(
        mediator, "local-bridge-token", on_server=OnServer("https://latet.invalid", "identity-one") if server else None
    )
    app = Starlette(routes=bridge.routes())
    identity = ["identity-one"]

    async def authorized(scope, receive, send):
        await app({**scope, "latet.identity": identity[0]}, receive, send)

    text = "reported contact " + EMAIL
    session = CLAUDE["x-claude-code-session-id"]
    data = {"session": session, "model": True, "items": [text]}
    with TestClient(authorized) as client:
        denied = client.post(
            "/_latet/engine/v1/rehydrate",
            json=data,
            headers={"x-latet-token": "wrong"} if not server else {"origin": "https://evil.invalid"},
        )
        assert denied.status_code == 401
        assert mediator.verifier.written_source("cc:" + session, text) is None
        response = client.post(
            "/_latet/engine/v1/rehydrate", json=data, headers={"x-latet-token": "local-bridge-token"}
        )
        assert response.status_code == 200
        assert mediator.verifier.written_source("cc:" + session, text) == (None if server else "reported")
        assert mediator.verifier.written_source("cc:another-session", text) is None
        if server:
            identity[0] = "identity-two"
            assert client.post("/_latet/engine/v1/rehydrate", json=data).status_code == 401
    body = copy.deepcopy(CASES[0][2])
    body["messages"].append({"role": "assistant", "content": text})
    prepared = prepare(mediator, *CASES[0][:2], body, verify=True)
    assert bool(prepared.block_reason) is server
    assert ("reported as written by the model" in prepared.body_rules) is not server


# Closed readable/provider migration inventory. These are independent synthetic controls.
_PROVIDER_BLOCKS = [
    {"type": "thinking", "thinking": "ordinary thought", "signature": "SIG"},
    {"type": "redacted_thinking", "data": "OPAQUE"},
    {"type": "server_tool_use", "id": "srvtoolu_a", "name": "web_search", "input": {"query": "weather"}},
    {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_a",
        "content": [
            {
                "type": "web_search_result",
                "url": "https://example.invalid/news",
                "title": "News",
                "encrypted_content": "OPAQUE",
            }
        ],
    },
    {
        "type": "web_fetch_tool_result",
        "tool_use_id": "srvtoolu_a",
        "content": {"type": "web_fetch_result", "url": "https://example.invalid/news"},
    },
    {
        "type": "code_execution_tool_result",
        "tool_use_id": "srvtoolu_a",
        "content": {"type": "code_execution_result", "stdout": "ok", "stderr": "", "return_code": 0},
    },
    {"type": "bash_code_execution_tool_result", "tool_use_id": "srvtoolu_a", "content": {"stdout": "ok", "stderr": ""}},
    {
        "type": "text_editor_code_execution_tool_result",
        "tool_use_id": "srvtoolu_a",
        "content": {"file_type": "text", "content": "ordinary text"},
    },
    {
        "type": "tool_search_tool_result",
        "tool_use_id": "srvtoolu_a",
        "content": {
            "type": "tool_search_tool_search_result",
            "tool_references": [{"type": "tool_reference", "tool_name": "notify"}],
        },
    },
    {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_a", "content": {"type": "advisor_result", "text": "fine"}},
    {"type": "mcp_tool_use", "id": "mcp_a", "name": "list", "input": {}},
    {"type": "mcp_tool_result", "tool_use_id": "mcp_a", "content": [{"type": "text", "text": "two items"}]},
    {"type": "mcp_tool_listing", "tools": []},
    {"type": "compaction", "content": "ordinary summary"},
    {"type": "connector_text", "text": "ordinary text"},
    {"type": "fallback", "from": {"model": "claude-sonnet-4-6"}, "to": {"model": "claude-opus-4-6"}},
    {"type": "container_upload", "file_id": "file_a"},
    {"type": "tool_reference", "tool_name": "notify"},
    {"type": "tool_removal", "name": "notify"},
]
_PROVIDER_ITEMS = [
    {"type": "reasoning", "id": "rs_a", "summary": [], "encrypted_content": "OPAQUE"},
    {"type": "compaction", "id": "cmp_a", "encrypted_content": "OPAQUE"},
    {"type": "item_reference", "id": "msg_a"},
    {"type": "web_search_call", "id": "ws_a", "action": {"type": "search", "query": "weather"}},
    {"type": "file_search_call", "id": "fs_a", "queries": ["pricing"]},
    {"type": "image_generation_call", "id": "ig_a", "result": "OPAQUE"},
    {"type": "code_interpreter_call", "id": "ci_a", "code": "print(1)"},
    {"type": "computer_call", "id": "cu_a", "call_id": "call_a", "action": {"type": "click", "x": 1, "y": 2}},
    {
        "type": "apply_patch_call",
        "id": "ap_a",
        "call_id": "call_a",
        "operation": {"type": "update_file", "path": "notes.txt", "diff": "ordinary text"},
    },
    {"type": "mcp_list_tools", "id": "ml_a", "tools": []},
    {"type": "mcp_approval_request", "id": "ar_a", "name": "send", "arguments": "{}"},
    {"type": "tool_search_call", "id": "ts_a", "arguments": "ordinary query"},
    {"type": "tool_search_output", "id": "ts_a", "tools": []},
]


@pytest.mark.parametrize("block", _PROVIDER_BLOCKS, ids=lambda value: value["type"])
def test_finite_provider_block_readable_control_and_unknown_sibling(mediator, block):
    assert {value["type"] for value in _PROVIDER_BLOCKS} == anthropic._PROVIDERS_BLOCKS
    body = copy.deepcopy(CASES[0][2])
    body["messages"] = [
        {
            "role": "assistant",
            "content": [
                {"type": "server_tool_use", "id": "srvtoolu_a", "name": "web_search", "input": {}},
                {"type": "mcp_tool_use", "id": "mcp_a", "name": "list", "input": {}},
                copy.deepcopy(block),
            ],
        }
    ]
    benign = prepare(mediator, *CASES[0][:2], body, verify=True)
    refused = {"container_upload", "thinking", "redacted_thinking", "web_search_tool_result"}
    assert (benign.block_reason is not None) == (block["type"] in refused), block["type"]
    body["messages"][0]["content"][-1]["unknown_CANARY"] = "ordinary"
    assert prepare(mediator, *CASES[0][:2], body, verify=True).block_reason is not None


@pytest.mark.parametrize("item", _PROVIDER_ITEMS, ids=lambda value: value["type"])
def test_finite_provider_item_readable_control_and_unknown_sibling(mediator, item):
    assert {value["type"] for value in _PROVIDER_ITEMS} == openai_responses._PROVIDERS_ITEMS
    body = {**CASES[3][2], "input": [copy.deepcopy(item)]}
    benign = prepare(mediator, *CASES[3][:2], body)
    refused = {"item_reference", "reasoning", "compaction", "image_generation_call"}
    assert (benign.block_reason is not None) == (item["type"] in refused), item["type"]
    body["input"][0]["unknown_CANARY"] = "ordinary"
    assert prepare(mediator, *CASES[3][:2], body).block_reason is not None


@pytest.mark.parametrize(
    "item",
    [
        {"type": "code_interpreter_call", "container_id": "container_a", "code": "print(1)"},
        {"type": "item_reference", "id": "msg_a"},
        {"type": "mcp_list_tools", "server_label": "crm", "tools": []},
    ],
)
def test_body_external_lookup_has_no_invented_config_role(mediator, item):
    body = {**CASES[3][2], "input": [item]}
    result = prepare(mediator, *CASES[3][:2], body)
    assert result.block_reason and "external resource" in result.block_reason


@pytest.mark.parametrize("field", ["body", "frame_payload"])
def test_unverified_serialized_bytes_cannot_be_reminted(mediator, field):
    if field == "body":
        prepared = prepare(mediator, *CASES[1])
        prepared.body = b'{"model":"gpt-4o","messages":[{"role":"user","content":"unchecked"}]}'
    else:
        _, prepared = mediator.mask_ws_frame(
            json.dumps({"type": "response.create", **CASES[3][2]}), mediator.engine.session("session")
        )
        assert prepared and not prepared.block_reason
        prepared.frame_payload = '{"type":"response.create","input":"unchecked"}'
    prepared.refresh_recording()
    assert prepared.block_reason is not None
    assert prepared.body_proof is None


def test_auto_mode_allow_has_forward_authority_and_stays_checked(decision_turns):
    from latet.engine.detectors.terms import IdentityDetector, LocalIdentity
    from latet.engine.review import ReviewPolicy
    from latet.gateway.outcome import Forward

    body = copy.deepcopy(CASES[0][2])
    body["system"] = "You are a security monitor for autonomous AI coding agents\nUser identity: `fixtureuser`"
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        mediator.verifier.masker.detectors.append(IdentityDetector(LocalIdentity(usernames=("fixtureuser",))))
        mediator.review = ReviewPolicy(frozenset({"user_name"}))
        for _ in range(2):
            assert client.post(CASES[0][1], json=body, headers=CLAUDE).status_code == 200
        mediator.review = ReviewPolicy()
        assert client.post(CASES[0][1], json=body, headers=CLAUDE).status_code == 400
        assert len(transport.sent) == 2
    assert all(type(turn.decision) is Forward for turn in decision_turns[:2])
    assert all(
        "auto-mode identity detail" in getattr(turn.decision.proof, "authority", ()) for turn in decision_turns[:2]
    )


def test_frame_cap_remains_32mib_when_server_allows_larger_http_body(mediator, monkeypatch):
    from latet.gateway import mediate

    mediator.body_limit = 64 << 20
    # Lower the named protocol ceiling, preserving the distinct configured HTTP ceiling.
    from latet.protocols import sse

    monkeypatch.setattr(sse, "MAX_DECODED", 256)
    reads = []
    real = mediate.wellformed.read

    def read(raw):
        reads.append(raw)
        return real(raw)

    monkeypatch.setattr(mediate.wellformed, "read", read)
    frame = json.dumps({"type": "response.create", "input": "x" * 256})
    _, prepared = mediator.mask_ws_frame(frame, mediator.engine.session("frame-cap"))
    assert prepared and prepared.block_reason
    assert reads == [], "oversized frame was parsed"


@pytest.mark.parametrize("bad", [False, True])
def test_closed_custom_grammar_preserves_masking_and_refuses_unknown_syntax(mediator, bad):
    body = {
        **CASES[3][2],
        "tools": [
            {
                "type": "custom",
                "name": "draft",
                "format": {
                    "type": "grammar",
                    "syntax": "unknown" if bad else "lark",
                    "definition": 'start: "' + EMAIL + '"',
                },
            }
        ],
    }
    result = prepare(mediator, *CASES[3][:2], body)
    if bad:
        assert result.block_reason is not None
    else:
        assert result.block_reason is None and EMAIL.encode() not in result.body
        assert any(replacements for _, replacements, _ in result.pending)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_real_lcms_profile_is_checked_in_supported_containers(mediator, fmt):
    from PIL import ImageCms

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(out, fmt, icc_profile=profile)
    raw = out.getvalue()
    if fmt == "JPEG":
        raw = raw[:2] + _jpeg_segment(0xEE, b"Adobe\0d\0\0\0\0\0") + raw[2:]
    mediator.vision = ImageMasking(FakeOcr())
    prepared = prepare(mediator, *CASES[0][:2], _media_body(raw, "image/" + fmt.lower()), verify=True)
    if fmt == "WEBP":
        assert "WebP" in prepared.block_reason
        _assert_webp_unsupported_then_rewritten(raw)
    else:
        assert prepared.block_reason is None
        assert any("sRGB" in text for text in prepared.body_metadata)


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (b"sRGB", b"\xff"),
        (b"gAMA", b"\0" * 4),
        (b"pHYs", b"\0" * 8 + b"\xff"),
        (b"tIME", b"\x07\xe9\x13\x01\x00\x00\x00"),
        (b"sBIT", b"\xff\xff\xff"),
        (b"bKGD", b"\0"),
    ],
)
def test_png_rendering_controls_have_value_grammars(kind, payload):
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    png = _raster("PNG")
    raw = png[:33] + _chunk(kind, payload) + png[33:]
    with pytest.raises(UnreadImage):
        inspect_image(base64.b64encode(raw).decode())


def test_gif_xmp_application_keeps_the_whole_utf8_packet(mediator):
    packet = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">Screenshot</x:xmpmeta>'
    trailer = b"\x01" + bytes(range(255, -1, -1)) + b"\0"
    gif = b"GIF89a" + _raster("GIF")[6:]
    raw = gif[:-1] + b"!\xff\x0bXMP DataXMP" + packet + trailer + b";"
    mediator.vision = ImageMasking(FakeOcr())
    prepared = prepare(mediator, *CASES[0][:2], _media_body(raw, "image/gif"), verify=True)
    assert prepared.block_reason is None
    assert packet.decode() in prepared.body_metadata


@pytest.mark.parametrize("extension", [b"!\xf9\x04\xe0\0\0\0\0", b"!\xff\x0bNETSCAPE2.0\x03\xff\0\0\0"])
def test_gif_control_and_loop_extensions_have_no_reserved_payload(extension):
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    gif = _raster("GIF")
    raw = gif[:-1] + extension + b";"
    with pytest.raises(UnreadImage):
        inspect_image(base64.b64encode(raw).decode())


def test_iptc_padding_is_zero_by_grammar():
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    # A one-byte caption makes the data record even; Pascal resource name needs padding.
    record = b"\x1c\x02\x78\0\1x"
    resource = b"8BIM\x04\x04\0X" + struct.pack(">I", len(record)) + record
    jpeg = _raster("JPEG")
    raw = jpeg[:2] + _jpeg_segment(0xED, b"Photoshop 3.0\0" + resource) + jpeg[2:]
    with pytest.raises(UnreadImage):
        inspect_image(base64.b64encode(raw).decode())


def test_image_byte_cap_precedes_base64_allocation(monkeypatch):
    from latet.vision import codec as image_codec

    monkeypatch.setattr(image_codec, "MAX_BYTES", 4)
    calls = []
    original = image_codec.base64.b64decode

    def decoded(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(image_codec.base64, "b64decode", decoded)
    with pytest.raises(image_codec.ImageError):
        image_codec.decode("YQ==" * 100)
    assert calls == []


def test_reencoded_image_parts_share_the_exchange_ledger(mediator):
    mediator.vision = ImageMasking(FakeOcr())
    prepared = prepare(mediator, *CASES[0][:2], _media_body(_raster("PNG"), "image/png"))
    assert prepared.block_reason is None and prepared.body_ledger
    assert any(path[0] == ("image", 0) for path in prepared.body_ledger.generated)
    before = len(prepared.body_ledger.generated), prepared.body_ledger.retained
    mediator._check(prepared)
    prepared.refresh_recording()
    assert before == (len(prepared.body_ledger.generated), prepared.body_ledger.retained)


def test_d6_does_not_escape_through_a_nested_provider_tool_definition(mediator):
    body = copy.deepcopy(CASES[0][2])
    body["messages"] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "mcp_tool_listing",
                    "tools": [
                        {"name": "lookup", "description": "ordinary", "input_schema": {}, "unknown_member": "ordinary"}
                    ],
                }
            ],
        }
    ]
    assert prepare(mediator, *CASES[0][:2], body, verify=True).block_reason is not None


def test_huge_numeric_control_is_a_safe_refusal(mediator):
    raw = b'{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}],"max_tokens":' + b"9" * 500 + b"}"
    prepared = prepare(mediator, *CASES[1][:2], raw)
    assert prepared.block_reason == "request body refused (control)"


def test_nonfinite_tool_data_is_not_a_readable_json_value(mediator):
    body = copy.deepcopy(CASES[0][2])
    body["messages"] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_a", "name": "lookup", "input": {"n": float("nan")}}],
        }
    ]
    assert prepare(mediator, *CASES[0][:2], body).block_reason is not None


@pytest.mark.parametrize("padding", [b"\0\0", b"XY"])
def test_exif_inline_numeric_padding_has_no_unread_bytes(padding):
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    # Orientation SHORT 1 occupies two of the four inline value bytes.
    exif = b"II*\0\x08\0\0\0" + struct.pack("<H", 1) + struct.pack("<HHI", 0x0112, 3, 1) + b"\1\0" + padding + b"\0" * 4
    jpeg = _raster("JPEG")
    raw = jpeg[:2] + _jpeg_segment(0xE1, b"Exif\0\0" + exif) + jpeg[2:]
    if padding.strip(b"\0"):
        with pytest.raises(UnreadImage):
            inspect_image(base64.b64encode(raw).decode())
    else:
        inspect_image(base64.b64encode(raw).decode())


@pytest.mark.parametrize("secret", [False, True])
def test_gif_xmp_complete_packet_reaches_final_verifier(mediator, secret):
    text = EMAIL if secret else "ordinary screenshot"
    packet = ('<x:xmpmeta xmlns:x="adobe:ns:meta/">' + text + "</x:xmpmeta>").encode()
    trailer = b"\x01" + bytes(range(255, -1, -1)) + b"\0"
    gif = b"GIF89a" + _raster("GIF")[6:]
    raw = gif[:-1] + b"!\xff\x0bXMP DataXMP" + packet + trailer + b";"
    mediator.vision = ImageMasking(FakeOcr())
    prepared = prepare(mediator, *CASES[0][:2], _media_body(raw, "image/gif"), verify=True)
    assert (prepared.block_reason is not None) == secret
    assert packet.decode() in prepared.body_metadata


@pytest.mark.parametrize("rule", [False, True], ids=["Forward", "M3-Pass"])
async def test_local_reviewer_masks_and_refreezes_body_with_exact_restore(rule):
    from latet.engine.rehydrate import restore_text
    from latet.gateway import outcome

    from .test_request_outcomes import CHAT, OPENAI, _chat, _envelope, _strict
    from .test_reviewer import FakeServer
    from .test_reviewer import mediator as reviewing

    made = reviewing(FakeServer(found={"Falcon": "person"}))
    reader = FakeOcr()
    if rule:
        made.vision = ImageMasking(reader)
    raw = _chat("write about Falcon", image=reader.screenshot(["ordinary image"]) if rule else None)
    prepared = await made.run_held(made.prepare_http, OPENAI, "POST", CHAT, HEADERS, raw, ())
    try:
        assert not prepared.block_reason and prepared.reviews
        old_proof = prepared.body_proof
        prepared = await made.review_prepared(prepared)
        assert not prepared.block_reason and b"Falcon" not in prepared.body
        assert prepared.body_proof is not old_proof and prepared.body_proof.payload is prepared.body
        decision = _strict(prepared, _envelope(raw))
        assert type(decision) is (outcome.Pass if rule else outcome.Forward)
        masked = prepared.pending[0][0].get()
        assert restore_text(masked, made.engine.restores(prepared.session, False))[0] == "write about Falcon"
    finally:
        if prepared.lease:
            prepared.lease.release()
        await made.reviewer.aclose()
        made.close()


@pytest.mark.parametrize("rule", [False, True], ids=["Forward", "M3-Pass"])
@pytest.mark.parametrize("stage", ["masking", "live"])
@pytest.mark.parametrize("stored", [False, True], ids=["local-vault", "stored-vault"])
async def test_cancelled_actual_forwarder_releases_worker_lease_once(rule, stage, stored, monkeypatch):
    import asyncio
    import threading

    import httpx
    from starlette.requests import Request

    from latet.config import LiveReview
    from latet.gateway.approvals import Decision
    from latet.gateway.capture import Capture
    from latet.gateway.forward import Forwarder
    from latet.gateway.routes import RouteTable

    from .test_request_outcomes import CHAT, OPENAI, _chat, _Provider, _until

    if stored:
        from latet.engine.verifier import Verifier

        from .test_stored_vault import _engine, _Store, _vault

        engine = _engine(_vault(_Store()))
        made = Mediator(engine, Verifier(engine.masker))
        if rule:
            made.vision = ImageMasking(FakeOcr())
    else:
        made = _mediator(FakeOcr() if rule else None)
    reader = FakeOcr()
    raw = _chat("write to " + EMAIL, image=reader.screenshot(["ordinary image"]) if rule else None)
    entered, release_worker = threading.Event(), threading.Event()
    seen, releases, holds = [], [], []
    real_prepare, real_release, real_hold = made.prepare, made.engine.release, made.engine.hold

    def prepared(*args, **kwargs):
        arena = real_prepare(*args, **kwargs)
        seen.append(arena)
        if stage == "masking":
            entered.set()
            assert release_worker.wait(10)
        return arena

    def held(*args, **kwargs):
        handle = real_hold(*args, **kwargs)
        holds.append(handle)
        return handle

    def released(session, handle):
        releases.append(handle)
        return real_release(session, handle)

    monkeypatch.setattr(made, "prepare", prepared)
    monkeypatch.setattr(made.engine, "hold", held)
    monkeypatch.setattr(made.engine, "release", released)
    if stage == "live":
        made.live_review = LiveReview(enabled=True)

        async def approved(*args, **kwargs):
            return Decision(True)

        monkeypatch.setattr(made, "await_decision", approved)
        real_reacquire = made.engine.reacquire

        def reacquire(*args, **kwargs):
            entered.set()
            assert release_worker.wait(10)
            return real_reacquire(*args, **kwargs)

        monkeypatch.setattr(made.engine, "reacquire", reacquire)
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Event().wait()

    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": CHAT,
        "raw_path": CHAT.encode(),
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:5283"), (b"content-type", b"application/json")],
        "server": ("127.0.0.1", 5283),
        "client": ("127.0.0.1", 1234),
        "root_path": "",
    }
    provider = _Provider()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(None), client, made)
            task = asyncio.create_task(forwarder.handle(Request(scope, receive)))
            await _until(entered.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release_worker.set()
            await made.run(lambda: None)
            assert len(seen) == 1 and seen[0].lease and seen[0].lease.released
            assert not seen[0].block_reason and seen[0].body_proof
            assert bool(seen[0].body_proof.rules) == rule
            assert provider.sent == [] and len(holds) == 1
            assert len(releases) == (2 if stored and stage == "live" else 1)
            assert len({id(handle) for handle in releases}) == len(releases)
    finally:
        release_worker.set()
        made.close()


@pytest.mark.parametrize("rule", [False, True], ids=["Forward", "M3-Pass"])
def test_live_refusal_sends_nothing_after_actual_worker_preparation(rule, monkeypatch, decision_turns):
    from latet.config import LiveReview
    from latet.gateway.approvals import Decision
    from latet.gateway.outcome import Block

    from .test_request_outcomes import CHAT, _chat, _gateway, _Provider, _rule_pass

    made, raw = _rule_pass("write to " + EMAIL) if rule else (_mediator(), _chat("write to " + EMAIL))
    made.live_review = LiveReview(enabled=True)

    async def refused(*args, **kwargs):
        return Decision(False, reason="declined by reviewer")

    monkeypatch.setattr(made, "await_decision", refused)
    provider = _Provider()
    with _gateway(provider, made) as client:
        response = client.post(CHAT, content=raw, headers=HEADERS)
    assert response.status_code == 400 and provider.sent == []
    assert type(decision_turns[-1].decision) is Block
    assert decision_turns[-1].prepared.lease.released


async def test_background_reviewer_answer_after_freeze_invalidates_old_authority():
    from latet.engine.reviewer import Answer, Finding
    from latet.gateway import outcome

    from .test_request_outcomes import CHAT, OPENAI, _chat, _envelope, _strict
    from .test_reviewer import FakeServer
    from .test_reviewer import mediator as reviewing

    made = reviewing(FakeServer(found={}))
    raw = _chat("write about Falcon")
    prepared = await made.run_held(made.prepare_http, OPENAI, "POST", CHAT, HEADERS, raw, ())
    try:
        envelope = _envelope(raw)
        old = _strict(prepared, envelope)
        assert isinstance(old, outcome.Forward)
        await made.run(
            made.learn, prepared.session, made.llm_review, prepared.reviews[0], Answer([Finding("Falcon", "person")])
        )
        with pytest.raises(outcome.NotSendable, match="stale"):
            outcome.sendable(old, prepared, envelope)
        prepared.refresh_recording()
        assert prepared.block_reason and prepared.body_proof is None
        prepared.refresh_recording()
        assert prepared.block_reason and prepared.body_proof is None
    finally:
        prepared.lease.release()
        await made.reviewer.aclose()
        made.close()


def test_verify_only_mended_character_has_accurate_safe_reason(mediator):
    raw = b'{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"safe\\ud800"}]}'
    prepared = prepare(mediator, *CASES[0][:2], raw, verify=True)
    assert prepared.block_reason and "character" in prepared.block_reason
    assert "twice" not in prepared.block_reason and prepared.body is raw


@pytest.mark.parametrize("kind", ["part", "text"])
def test_image_metadata_budget_accepts_exact_edge_then_refuses_without_retaining(monkeypatch, kind):
    from latet.gateway import image_coverage as image

    reader = image._Reader()
    if kind == "part":
        monkeypatch.setattr(image, "MAX_PARTS", 2)
        reader.part()
        reader.part()
        with pytest.raises(image.UnreadImage):
            reader.part()
    else:
        monkeypatch.setattr(image, "MAX_METADATA", 4)
        reader.text(b"test")
        with pytest.raises(image.UnreadImage):
            reader.text(b"x")
        assert reader.texts == ["test"]


@pytest.mark.parametrize(
    ("kind", "payload"),
    [(b"ANIM", b"\0" * 5), (b"ANMF", b"\0" * 15 + b"\xff"), (b"ANMF", b"\0" * 16 + b"EVIL\x02\0\0\0xy")],
)
def test_webp_animation_structural_blocks_do_not_authorize_unknown_bytes(kind, payload):
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    block = kind + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) % 2 else b"")
    raw = b"RIFF" + struct.pack("<I", 4 + len(block)) + b"WEBP" + block
    with pytest.raises(UnreadImage):
        inspect_image(base64.b64encode(raw).decode())


def test_webp_animation_remains_unread_by_existing_pixel_reader(mediator):
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(
        out, "WEBP", save_all=True, append_images=[Image.new("RGB", (8, 8), "black")], duration=100, loop=0
    )
    mediator.vision = ImageMasking(FakeOcr())
    prepared = prepare(mediator, *CASES[0][:2], _media_body(out.getvalue(), "image/webp"), verify=True)
    assert prepared.block_reason is not None


def test_concurrent_source_writes_preserve_observed_across_adapter_instances(mediator):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from latet.engine.verifier import Verifier
    from latet.state.local import LocalConversations

    shared = LocalConversations()
    observed = Verifier(mediator.engine.masker)
    reported = Verifier(mediator.engine.masker)
    observed.shared = reported.shared = shared
    ready = Barrier(2)

    def write(verifier, report):
        ready.wait(5)
        verifier.wrote("own-session", [EMAIL], reported=report)

    with ThreadPoolExecutor(2) as workers:
        first = workers.submit(write, observed, False)
        second = workers.submit(write, reported, True)
        first.result(5)
        second.result(5)
    fresh = Verifier(mediator.engine.masker)
    fresh.shared = shared
    assert len(shared._reverse) == 2  # two normal bounded adapter entries, neither overwrites the other
    for verifier in (observed, reported, fresh):
        assert verifier.written_source("own-session", EMAIL) == "observed"
        assert verifier.written_source("other-session", EMAIL) is None
    assert len(reported._written) == 2 and reported.cached_bytes == 2 * observed.cached_bytes


@pytest.mark.parametrize("refusal", ["model", "semantic", "resource", "correlation"])
def test_refused_body_never_publishes_history_before_learn_and_retry(decision_turns, refusal):
    from latet.engine.types import EntityClass
    from latet.engine.vault import Entity
    from latet.gateway.outcome import Block

    text = "The label is CrimsonHarbor"
    body = copy.deepcopy(CASES[0][2])
    body["messages"][0]["content"] = text
    bad = copy.deepcopy(body)
    if refusal == "model":
        bad["model"] = "unapproved-model"
    elif refusal == "semantic":
        bad["tools"] = [{"name": "invalid name", "input_schema": {"type": "object"}}]
    elif refusal == "resource":
        bad["messages"].append(
            {"role": "assistant", "content": [{"type": "mcp_tool_listing", "server_name": "unbound", "tools": []}]}
        )
    else:
        bad["messages"].append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_missing", "content": "ordinary result"}],
            }
        )
    key = "gw|cc:" + CLAUDE["x-claude-code-session-id"]
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        raw = json.dumps(bad).encode()
        response = client.post(CASES[0][1], content=raw, headers=CLAUDE)
        assert response.status_code == 400 and not transport.sent
        assert type(decision_turns[-1].decision) is Block
        assert decision_turns[-1].prepared.body == raw
        assert not mediator.verifier.sent(key, text), "refused body acquired sent-history authority"
        client.portal.call(
            mediator.run,
            mediator.engine.vault.learn,
            Entity("fix1", "TERM", EntityClass.CUSTOM, "CrimsonHarbor", "HiddenLabel", 1, 1.0),
        )
        response = client.post(CASES[0][1], json=body, headers=CLAUDE)
        assert response.status_code == 400 and not transport.sent
        assert type(decision_turns[-1].decision) is Block
        assert not mediator.verifier.sent(key, text)


@pytest.mark.parametrize("sent_first", [False, True])
def test_refusal_history_fresh_and_successful_controls(decision_turns, sent_first):
    from latet.engine.types import EntityClass
    from latet.engine.vault import Entity
    from latet.gateway.outcome import Block, Forward, Pass

    text = "The label is CrimsonHarbor"
    body = copy.deepcopy(CASES[0][2])
    body["messages"][0]["content"] = text
    raw = json.dumps(body).encode()
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        if sent_first:
            assert client.post(CASES[0][1], content=raw, headers=CLAUDE).status_code == 200
            assert type(decision_turns[-1].decision) is Forward
            assert transport.sent[-1].content == raw
        client.portal.call(
            mediator.run,
            mediator.engine.vault.learn,
            Entity("fix1", "TERM", EntityClass.CUSTOM, "CrimsonHarbor", "HiddenLabel", 1, 1.0),
        )
        response = client.post(CASES[0][1], content=raw, headers=CLAUDE)
        if sent_first:
            assert response.status_code == 200 and len(transport.sent) == 2
            assert transport.sent[-1].content == raw
            assert type(decision_turns[-1].decision) is Pass
            assert "sent before in this conversation" in str(decision_turns[-1].decision.evidence)
        else:
            assert response.status_code == 400 and not transport.sent
            assert type(decision_turns[-1].decision) is Block


def _unread_codestream(case):
    png = _png()
    if case.startswith("png"):
        if case == "png-control":
            return png, "image/png"
        if case == "png-extra-idat":
            return png[:-12] + _chunk(b"IDAT", b"UNREAD_MEDIA_CANARY") + png[-12:], "image/png"
        at, chunks = 8, [png[:8]]
        while at < len(png):
            size, kind = struct.unpack_from(">I4s", png, at)
            payload = png[at + 8 : at + 8 + size]
            chunks.append(_chunk(kind, payload + (b"UNREAD_MEDIA_CANARY" if kind == b"IDAT" else b"")))
            at += 12 + size
        return b"".join(chunks), "image/png"
    images = []
    for color in ("white", "black"):
        out = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(out, "WEBP", lossless=True)
        images.append(out.getvalue())
    raw = images[0] if case == "webp-control" else images[0] + images[1][12:]
    return raw[:4] + struct.pack("<I", len(raw) - 8) + raw[8:], "image/webp"


@pytest.mark.parametrize(
    "case", ["png-control", "png-idat-unused-tail", "png-extra-idat", "webp-control", "webp-second-codestream"]
)
def test_m3_gateway_covers_only_the_single_consumed_codestream(decision_turns, case):
    from latet.gateway.image_coverage import UnreadImage, inspect_image
    from latet.gateway.outcome import Block, Pass
    from latet.vision.codec import decode

    raw, mime = _unread_codestream(case)
    encoded = base64.b64encode(raw).decode()
    assert decode(encoded).image.getpixel((0, 0)) == (255, 255, 255)
    body = json.dumps(_media_body(raw, mime)).encode()
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        mediator.vision = ImageMasking(FakeOcr())
        response = client.post(CASES[0][1], content=body, headers=CLAUDE)
        if case == "png-control":
            inspect_image(encoded)
            assert response.status_code == 200 and len(transport.sent) == 1
            assert transport.sent[-1].content == body
            assert type(decision_turns[-1].decision) is Pass
        else:
            assert response.status_code == 400 and not transport.sent
            assert type(decision_turns[-1].decision) is Block
            assert decision_turns[-1].prepared.body == body
            with pytest.raises(UnreadImage):
                inspect_image(encoded)


@pytest.mark.parametrize("case", ["png-idat-unused-tail", "png-extra-idat", "webp-second-codestream"])
def test_rewrite_removes_unread_codestream_remainders(decision_turns, case):
    from latet.gateway.image_coverage import inspect_image
    from latet.gateway.outcome import Pass
    from latet.vision.codec import decode

    raw, mime = _unread_codestream(case)
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        mediator.vision = ImageMasking(FakeOcr())
        response = client.post(CASES[0][1], json=_media_body(raw, mime), headers=HEADERS)
        assert response.status_code == 200 and len(transport.sent) == 1
        encoded = json.loads(transport.sent[-1].content)["messages"][0]["content"][0]["source"]["data"]
        assert base64.b64decode(encoded) != raw
        assert b"UNREAD_MEDIA_CANARY" not in base64.b64decode(encoded)
        inspect_image(encoded)
        assert decode(encoded).image.getpixel((0, 0)) == (255, 255, 255)
        assert type(decision_turns[-1].decision) is Pass


def _png_stream(width=3, height=2, depth=8, color=2, interlace=0, *, stream=None, middle=()):
    passes = (
        [(0, 0, 1, 1)]
        if not interlace
        else [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    )
    if stream is None:
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        stream = b""
        for x, y, dx, dy in passes:
            w, h = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
            if w and h:
                stream += (b"\0" + b"\0" * ((w * channels * depth + 7) // 8)) * h
    header = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace)
    palette = _chunk(b"PLTE", b"\0\0\0") if color == 3 else b""
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + palette
        + b"".join(middle)
        + _chunk(b"IDAT", zlib.compress(stream))
        + _chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("interlace", [0, 1])
@pytest.mark.parametrize(("color", "depth"), [(0, 1), (0, 16), (2, 8), (2, 16), (3, 2), (4, 8), (6, 16)])
def test_png_complete_stream_has_exact_scanline_extent(color, depth, interlace):
    from latet.gateway.image_coverage import inspect_image
    from latet.vision.codec import decode

    raw = _png_stream(9, 9, color=color, depth=depth, interlace=interlace)
    encoded = base64.b64encode(raw).decode()
    inspect_image(encoded)
    assert decode(encoded).image.size == (9, 9)


@pytest.mark.parametrize(
    "case",
    [
        "extra-row",
        "short-row",
        "invalid-filter",
        "missing-idat",
        "split-stream",
        "nonconsecutive",
        "late-palette",
        "duplicate-gamma",
        "late-gamma",
        "conflicting-color",
        "missing-palette",
    ],
)
def test_png_stream_extent_order_and_cardinality_refuse_unread(case):
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    raw = _png_stream()
    chunks, at = [], 8
    while at < len(raw):
        size, kind = struct.unpack_from(">I4s", raw, at)
        chunks.append((kind, raw[at + 8 : at + 8 + size]))
        at += size + 12
    payload = chunks[1][1]
    if case in {"extra-row", "short-row", "invalid-filter"}:
        pixels = zlib.decompress(payload)
        pixels = (
            pixels + b"\0" * 10 if case == "extra-row" else pixels[:-1] if case == "short-row" else b"\x05" + pixels[1:]
        )
        chunks[1] = (b"IDAT", zlib.compress(pixels))
    elif case == "missing-idat":
        chunks.pop(1)
    elif case == "split-stream":
        chunks[1] = (b"IDAT", payload + zlib.compress(b"unread"))
    elif case == "nonconsecutive":
        chunks[1:2] = [(b"IDAT", payload[:4]), (b"tEXt", b"Comment\0hello"), (b"IDAT", payload[4:])]
    elif case == "late-palette":
        chunks.insert(2, (b"PLTE", b"\0\0\0"))
    elif case == "duplicate-gamma":
        chunks[1:1] = [(b"gAMA", struct.pack(">I", 45455))] * 2
    elif case == "late-gamma":
        chunks.insert(2, (b"gAMA", struct.pack(">I", 45455)))
    elif case == "conflicting-color":
        chunks[1:1] = [(b"sRGB", b"\0"), (b"iCCP", b"Profile\0\0" + zlib.compress(_icc("Display")))]
    else:
        header = bytearray(chunks[0][1])
        header[9] = 3
        chunks[0] = (b"IHDR", bytes(header))
    encoded = base64.b64encode(raw[:8] + b"".join(_chunk(k, p) for k, p in chunks)).decode()
    with pytest.raises(UnreadImage):
        inspect_image(encoded)


def _webp_chunks(raw):
    chunks, at = [], 12
    while at < len(raw):
        kind, size = struct.unpack_from("<4sI", raw, at)
        chunks.append((kind, raw[at + 8 : at + 8 + size]))
        at += 8 + size + size % 2
    return chunks


def _webp_container(chunks):
    payload = b"WEBP" + b"".join(
        kind + struct.pack("<I", len(data)) + data + b"\0" * (len(data) % 2) for kind, data in chunks
    )
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


@pytest.mark.parametrize("lossless", [False, True])
@pytest.mark.parametrize("alpha", [False, True])
def test_webp_single_image_extended_metadata_and_alpha_require_rewrite(lossless, alpha):

    out = io.BytesIO()
    Image.new("RGBA" if alpha else "RGB", (8, 8), (255, 255, 255, 128) if alpha else "white").save(
        out,
        "WEBP",
        lossless=lossless,
        icc_profile=_icc("Display"),
        exif=_exif_ascii("Camera"),
        xmp=b"<xmp>ordinary</xmp>",
    )
    _assert_webp_unsupported_then_rewritten(out.getvalue())


@pytest.mark.parametrize(
    "case",
    [
        "no-image",
        "duplicate-image",
        "mixed-images",
        "duplicate-header",
        "late-header",
        "late-alpha",
        "duplicate-alpha",
        "alpha-with-lossless",
        "duplicate-xmp",
        "late-icc",
        "flag-mismatch",
    ],
)
def test_webp_single_image_order_and_cardinality(case):
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    out = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 255, 255, 128)).save(out, "WEBP", lossless=False, xmp=b"ordinary")
    chunks = _webp_chunks(out.getvalue())
    by_kind = dict(chunks)
    if case == "no-image":
        chunks = [(k, v) for k, v in chunks if k != b"VP8 "]
    elif case == "duplicate-image":
        chunks.append((b"VP8 ", by_kind[b"VP8 "]))
    elif case == "mixed-images":
        other, _ = _unread_codestream("webp-control")
        chunks += _webp_chunks(other)
    elif case == "duplicate-header":
        chunks.insert(1, chunks[0])
    elif case == "late-header":
        chunks.append(chunks.pop(0))
    elif case == "late-alpha":
        chunks = [(k, v) for k, v in chunks if k != b"ALPH"] + [(b"ALPH", by_kind[b"ALPH"])]
    elif case == "duplicate-alpha":
        chunks.insert(2, (b"ALPH", by_kind[b"ALPH"]))
    elif case == "alpha-with-lossless":
        other, _ = _unread_codestream("webp-control")
        chunks = [(k, v) for k, v in chunks if k != b"VP8 "] + _webp_chunks(other)
    elif case == "duplicate-xmp":
        chunks.append((b"XMP ", b"second"))
    elif case == "late-icc":
        header = bytes([by_kind[b"VP8X"][0] | 32]) + by_kind[b"VP8X"][1:]
        chunks[0] = (b"VP8X", header)
        chunks.append((b"ICCP", _icc("Display")))
    else:
        chunks[0] = (b"VP8X", bytes([by_kind[b"VP8X"][0] & ~4]) + by_kind[b"VP8X"][1:])
    with pytest.raises(UnreadImage):
        inspect_image(base64.b64encode(_webp_container(chunks)).decode())


def _assert_ledger_byte_edge():
    from dataclasses import replace

    from latet.gateway.body_coverage import BodyRefused, Ledger
    from latet.gateway.coverage import LIMITS

    # Eight raw descriptors + three 3-byte key charges; the repeated key's two shadow links (its key and its value,
    # indexed so the copy not read is screened too, 3646b6e0); six final paths; two envelope entries.
    exact = 8 * 256 + 3 * 12 + 2 * 96 + (160 + 4 * 168 + 176) + 2 * 256
    assert exact == 3796
    raw, final = b'{"a":1,"a":2,"b":[3]}', {"a": 2, "b": [3]}
    ledger = Ledger(limits=replace(LIMITS, ledger_bytes=exact), envelope=2)
    ledger.preflight(raw)
    ledger.account(final)
    ledger.finish()
    assert ledger.retained == exact and ledger.receipts.count("removed") == 2
    for document in (final, {"a": 2}, final):
        ledger.account(document)
        assert ledger.retained == exact  # no reset or refund on removal/review
    before = dict(ledger.final), set(ledger.generated), ledger.retained
    try:
        ledger.account({"a": 2, "b": [3], "c": 4})
    except BodyRefused:
        refused = True
    else:
        refused = False
    assert refused, "retention guard was bypassed for a newly generated entry"
    assert (ledger.final, ledger.generated, ledger.retained) == before
    short = Ledger(limits=replace(LIMITS, ledger_bytes=exact - 1), envelope=2)
    short.preflight(raw)
    with pytest.raises(BodyRefused):
        short.account(final)
    assert ("b", 0) not in short.final and short.retained == exact - 176
    # Re-pinned by P2-8: the repeated key's projections (its two copies) each reserve their materialization, 768 bytes a
    # raw entry and 12 a body byte, before it is made, and give it back once it is read; the shadow index (256 bytes a
    # raw entry) is retained. Their peak is the 2788 bytes retained after the pre-pass, the index and one reservation:
    # at it they are read, one byte under it the first is refused before it is made. Before, every reservation was kept.
    before, index, reservation = exact - 1008, 256 * 8, 768 * 8 + 12 * len(raw)
    assert (before, index, reservation) == (2788, 2048, 6396)
    for limit, admitted in ((before + index + reservation, True), (before + index + reservation - 1, False)):
        shadow = Ledger(limits=replace(LIMITS, ledger_bytes=limit), envelope=2)
        shadow.preflight(raw)
        assert shadow.retained == before
        read = []
        try:
            for projection in shadow.shadow_projections(raw, 8):
                read.append(projection["a"])
                assert shadow.retained == before + index + reservation  # one live projection reserved
        except BodyRefused:
            pass
        if admitted:
            assert read == [2, 1] and shadow.retained == before + index
            shadow.account(final)
            shadow.finish()
            assert shadow.retained == exact + index and shadow.receipts.count("removed") == 2
        else:
            assert read == [], "retention guard was bypassed for a projection"
            assert shadow.retained == before + index and shadow.shadow_work == 0


def test_body_ledger_exact_bytes_removed_entries_and_guard_mutant(monkeypatch):
    import inspect
    import textwrap

    from latet.gateway import body_coverage

    _assert_ledger_byte_edge()
    source = textwrap.dedent(inspect.getsource(body_coverage.Ledger.charge))
    source = source.replace("if self.retained + size > self.limits.ledger_bytes:", "if False:")
    namespace = dict(vars(body_coverage))
    exec(source, namespace)
    monkeypatch.setattr(body_coverage.Ledger, "charge", namespace["charge"])
    with pytest.raises(AssertionError, match="retention guard was bypassed"):
        _assert_ledger_byte_edge()


def test_body_raw_depth_exact_edge_precedes_parse_and_retention(monkeypatch):
    import inspect
    import textwrap
    from dataclasses import replace

    from latet.gateway import body_coverage
    from latet.gateway.coverage import LIMITS

    def check():
        ledger = body_coverage.Ledger(limits=replace(LIMITS, depth=3))
        ledger.preflight(b'{"a":[{"b":1}]}')
        ledger.account({"a": [{"b": 1}]})
        ledger.finish()
        assert all(receipt == "covered" for receipt in ledger.receipts)
        beyond = body_coverage.Ledger(limits=replace(LIMITS, depth=3))
        try:
            beyond.preflight(b'{"a":[{"b":{}}]}')
        except body_coverage.BodyRefused:
            refused = True
        else:
            refused = False
        assert refused, "depth guard admitted the fourth container"
        assert len(beyond.raw) == 5 and beyond.raw[-1].kind == "key"
        assert not any(entry.start == 11 for entry in beyond.raw)

    check()
    source = textwrap.dedent(inspect.getsource(body_coverage.Ledger.preflight))
    source = source.replace("if len(stack) >= self.limits.depth:", "if False:")
    namespace = dict(vars(body_coverage))
    exec(source, namespace)
    monkeypatch.setattr(body_coverage.Ledger, "preflight", namespace["preflight"])
    with pytest.raises(AssertionError, match="depth guard admitted"):
        check()


@pytest.mark.parametrize("resource", ["depth", "ledger_bytes"])
@pytest.mark.parametrize("beyond", [False, True])
def test_body_exact_budget_gateway_boundary_has_fixed_422_and_no_send(monkeypatch, decision_turns, resource, beyond):
    from dataclasses import replace

    from latet.engine import wellformed
    from latet.gateway import coverage
    from latet.gateway.outcome import Block, Forward

    raw = (
        b'{"model":"gpt-4o","prompt":"removed","prompt":"hello"}'
        if resource == "ledger_bytes"
        else json.dumps(CASES[1][2]).encode()
    )
    path = "/openai/v1/completions" if resource == "ledger_bytes" else CASES[1][1]
    # 13 envelope occurrences: method + 4 path segments + 4 header name/value pairs.
    # Seven raw descriptors + three keys and five retained final paths (2716 body bytes), the repeated key's two shadow
    # links (96 each) and the projections of the copies, which 3646b6e0 screens for a launch credential (their row index
    # and the reservation of each projection, 768 * 7 + 12 * 54 = 6024 bytes). Since P2-8 a projection's reservation is
    # given back once it is read: the peak is the 7196 bytes retained before it and one reservation (13220), the request
    # retaining 8028 once its final check is done (20076 before, both projections' 6024 kept).
    limit = 13220 if resource == "ledger_bytes" else 3
    reads = []
    real_read = wellformed.read

    def observed_read(*args, **kwargs):
        reads.append(True)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(wellformed, "read", observed_read)
    with gateway() as (client, transport, _clients, _mediator):
        client.headers.clear()
        headers = {**HEADERS, "x-stainless-future-observation": "removed"}
        monkeypatch.setattr(coverage, "LIMITS", replace(coverage.LIMITS, **{resource: limit - int(beyond)}))
        response = client.post(path, content=raw, headers=headers)
        prepared = decision_turns[-1].prepared
        if beyond:
            assert response.status_code == 422 and not transport.sent
            assert type(decision_turns[-1].decision) is Block
            assert prepared.envelope_refusal == "coverage-limit"
            assert "request coverage limit exceeded" in response.text
            if resource == "depth":
                assert not reads  # body JSON materialization is prohibited beyond depth
        else:
            assert response.status_code == 200 and len(transport.sent) == 1
            assert type(decision_turns[-1].decision) is Forward
            assert reads
            if resource == "ledger_bytes":
                ledger = prepared.body_ledger
                assert ledger.envelope == 13 and ledger.retained == 8028  # the reservation given back
                assert ledger.receipts.count("removed") == 2
                assert json.loads(transport.sent[-1].content)["prompt"] == "hello"
            assert all(k.lower() != b"x-stainless-future-observation" for k, _ in transport.sent[-1].headers)


def _oracle_counter_witness(occupied):
    from latet.engine.rehydrate import restore_text, restores_for
    from latet.engine.types import EntityClass, Policy, Replacement
    from latet.engine.vault import Vault

    vault = Vault(b"\7" * 32)
    draws = iter(["SharedAlias", "SharedAlias", "OtherAlias"] if occupied else ["SharedAlias"])

    def generate(original, rng):
        return next(draws)

    if occupied:
        vault.intern("TERM", EntityClass.CUSTOM, "OtherOriginal", generate=generate)
    entity = vault.intern("TERM", EntityClass.CUSTOM, "CrimsonHarbor", generate=generate)
    original = "CrimsonHarbor and CrimsonHarbor"
    positions = [0, len("CrimsonHarbor and ")]
    replacements = [
        Replacement(
            start,
            start + len(entity.original),
            entity.original,
            entity.surrogate,
            entity.etype,
            entity.eclass,
            "fixture",
            Policy.MASK,
            entity.id,
        )
        for start in positions
    ]
    text = entity.surrogate + " and " + entity.surrogate
    restored = restore_text(text, restores_for(vault.entities(), json_mode=False))[0]
    assert restored == original
    return vault, entity, original, text, restored, replacements


def test_actual_oracle_normalizer_positive_cold_counter_order():
    import hashlib

    observations = []
    for occupied in (False, True):
        vault, entity, original, text, restored, replacements = _oracle_counter_witness(occupied)
        assert entity.counter == int(occupied)
        result = _normalize_oracle_mask(text, restored, replacements, False, vault.entities(), {}, {})
        marker = "<MASK:TERM:" + hashlib.sha256(b"CrimsonHarbor").hexdigest() + ">"
        assert result["canonical"] == marker + " and " + marker
        assert result["literal"] == ("OtherAlias and OtherAlias" if occupied else "SharedAlias and SharedAlias")
        observations.append(result)
    assert observations[0]["literal"] != observations[1]["literal"]
    _same_oracle_mask(original, *observations)


def test_actual_oracle_normalizer_keeps_ordinary_surrogate_like_literal():
    vault, _entity, _original, _text, _restored, _replacements = _oracle_counter_witness(False)
    ordinary = "An ordinary literal SharedAlias and <MASK:TERM:literal>"
    normalized = _normalize_oracle_mask(ordinary, ordinary, [], True, vault.entities(), {}, {})
    assert normalized["canonical"] == ordinary
    assert normalized["aliases"] == []


@pytest.mark.parametrize("mutant", ["wrong-entity", "missing-mask", "withholding", "ordinary-literal"])
def test_actual_oracle_normalizer_and_comparator_reject_changed_semantics(mutant):
    from dataclasses import replace

    from latet.engine.rehydrate import restore_text, restores_for
    from latet.engine.types import Policy

    vault, entity, original, text, restored, replacements = _oracle_counter_witness(False)
    baseline = _normalize_oracle_mask(text, restored, replacements, False, vault.entities(), {}, {})
    if mutant == "wrong-entity":
        entities = [replace(entity, original="WrongOriginal")]
        restored = restore_text(text, restores_for(entities, json_mode=False))[0]
    else:
        entities = vault.entities()
        if mutant == "missing-mask":
            text, replacements = original, []
        elif mutant == "withholding":
            text = "[REDACTED:TERM] and [REDACTED:TERM]"
            replacements = [
                replace(r, replacement="[REDACTED:TERM]", policy=Policy.REDACT, entity_id=None) for r in replacements
            ]
            restored = text
        else:
            text += " ordinary changed text"
            restored += " ordinary changed text"
    with pytest.raises(AssertionError):
        _same_oracle_mask(
            original, baseline, _normalize_oracle_mask(text, restored, replacements, False, entities, {}, {})
        )


@pytest.mark.parametrize("mutant", ["alias-to-two-entities", "entity-to-two-aliases"])
def test_actual_oracle_normalizer_requires_bijective_repeated_aliases(mutant):
    from dataclasses import replace

    vault, entity, _original, text, restored, replacements = _oracle_counter_witness(False)
    aliases, originals = {}, {}
    first = _normalize_oracle_mask(text, restored, replacements, False, vault.entities(), aliases, originals)
    assert first == _normalize_oracle_mask(text, restored, replacements, False, vault.entities(), aliases, originals)
    if mutant == "alias-to-two-entities":
        changed = replace(entity, original="WrongOriginal")
        replacements = [replace(r, original="WrongOriginal") for r in replacements]
    else:
        changed = replace(entity, surrogate="SecondAlias")
        text = "SecondAlias and SecondAlias"
        replacements = [replace(r, replacement="SecondAlias") for r in replacements]
    with pytest.raises(AssertionError):
        _normalize_oracle_mask(text, restored, replacements, False, [changed], aliases, originals)


def test_actual_oracle_normalizer_never_replaces_literal_adjacent_to_emitted_mask():
    import hashlib

    vault, entity, _original, _text, _restored, replacements = _oracle_counter_witness(False)
    text = entity.surrogate + " ordinary literal " + entity.surrogate
    restored = entity.original + " ordinary literal " + entity.surrogate
    result = _normalize_oracle_mask(text, restored, replacements[:1], False, vault.entities(), {}, {})
    assert (
        result["canonical"]
        == "<MASK:TERM:" + hashlib.sha256(b"CrimsonHarbor").hexdigest() + "> ordinary literal SharedAlias"
    )
    assert result["literal"] == "SharedAlias ordinary literal SharedAlias"


def test_actual_oracle_normalizer_rejects_wrong_entity_even_with_claimed_restore():
    from dataclasses import replace

    _vault, entity, _original, text, restored, replacements = _oracle_counter_witness(False)
    with pytest.raises(AssertionError):
        _normalize_oracle_mask(text, restored, replacements, False, [replace(entity, original="WrongOriginal")], {}, {})


@pytest.mark.parametrize("interlace", [0, 1])
@pytest.mark.parametrize("size", [(1, 1), (1, 9), (9, 1), (257, 257)])
def test_png_stream_split_boundaries_and_bounded_output(size, interlace, monkeypatch):
    from latet.gateway import image_coverage

    raw = _png_stream(*size, color=6, depth=16, interlace=interlace)
    size_header = struct.unpack_from(">I", raw, 8)[0]
    at = 8 + 12 + size_header
    size_stream = struct.unpack_from(">I", raw, at)[0]
    compressed = raw[at + 8 : at + 8 + size_stream]
    raw = (
        raw[:at]
        + b"".join(_chunk(b"IDAT", part) for part in (b"", compressed[:1], compressed[1:-1], compressed[-1:], b""))
        + raw[-12:]
    )
    real_factory = image_coverage.zlib.decompressobj
    retained = []

    class BoundedInflater:
        def __init__(self):
            self.inner = real_factory()

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def decompress(self, data, limit):
            assert 0 < limit <= 65536
            block = self.inner.decompress(data, limit)
            retained.append(len(block))
            return block

    monkeypatch.setattr(image_coverage.zlib, "decompressobj", BoundedInflater)
    image_coverage.inspect_image(base64.b64encode(raw).decode())
    assert retained and max(retained) <= 65536
    if size == (257, 257):
        assert sum(retained) > 65536 and len(retained) > 8


def test_png_declared_pixel_bound_precedes_inflater_and_extra_decoded_bytes_stop_early(monkeypatch):
    from latet.gateway import image_coverage

    too_many_pixels = _png_stream(9, 8)
    exact = _png_stream(8, 8)
    bomb = _png_stream(8, 8, stream=b"\0" * 1_000_000)
    monkeypatch.setattr(image_coverage, "MAX_PIXELS", 64)
    real_factory = image_coverage.zlib.decompressobj
    allocations = []

    class BoundedInflater:
        def __init__(self):
            allocations.append([])
            self.inner = real_factory()

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def decompress(self, data, limit):
            block = self.inner.decompress(data, limit)
            allocations[-1].append(len(block))
            return block

    monkeypatch.setattr(image_coverage.zlib, "decompressobj", BoundedInflater)
    with pytest.raises(image_coverage.UnreadImage):
        image_coverage.inspect_image(base64.b64encode(too_many_pixels).decode())
    assert allocations == []
    image_coverage.inspect_image(base64.b64encode(exact).decode())
    assert allocations == [[200]]  # eight RGB rows of 24 bytes + one filter byte
    with pytest.raises(image_coverage.UnreadImage):
        image_coverage.inspect_image(base64.b64encode(bomb).decode())
    assert allocations[-1] == [201]  # at most one byte past the declared extent, never a pixel buffer


@pytest.mark.parametrize("earlier", [b"tRNS", b"bKGD"])
def test_png_optional_palette_still_precedes_dependent_chunks(earlier):
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    raw = _png_stream(middle=[_chunk(earlier, b"\0" * 6), _chunk(b"PLTE", b"\0\0\0")])
    with pytest.raises(UnreadImage):
        inspect_image(base64.b64encode(raw).decode())


def test_webp_reordered_metadata_still_requires_rewrite():

    out = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 255, 255, 128)).save(
        out, "WEBP", lossless=False, icc_profile=_icc("Display"), exif=_exif_ascii("Camera"), xmp=b"ordinary"
    )
    chunks = _webp_chunks(out.getvalue())
    metadata = [(kind, payload) for kind, payload in chunks if kind in (b"EXIF", b"XMP ")]
    reconstruction = [(kind, payload) for kind, payload in chunks if kind not in (b"EXIF", b"XMP ")]
    reordered = reconstruction[:1] + metadata[::-1] + reconstruction[1:]
    _assert_webp_unsupported_then_rewritten(_webp_container(reordered))


def test_actual_oracle_comparison_preserves_withholding_even_if_restore_is_claimed():
    from dataclasses import replace

    from latet.engine.types import Policy

    vault, _entity, original, text, restored, replacements = _oracle_counter_witness(False)
    baseline = _normalize_oracle_mask(text, restored, replacements, False, vault.entities(), {}, {})
    withheld = [replace(r, replacement="[REDACTED:TERM]", policy=Policy.REDACT, entity_id=None) for r in replacements]
    changed = _normalize_oracle_mask(
        "[REDACTED:TERM] and [REDACTED:TERM]", restored, withheld, False, vault.entities(), {}, {}
    )
    assert changed["restored"] == baseline["restored"] == original
    assert changed["canonical"] == "[REDACTED:TERM] and [REDACTED:TERM]"
    with pytest.raises(AssertionError):
        _same_oracle_mask(original, baseline, changed)


@pytest.mark.parametrize("late", [False, True])
def test_png_exif_precedes_image_data(late):
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    metadata = _chunk(b"eXIf", _exif_ascii("Camera")[6:])
    png = _png()
    raw = png[:-12] + metadata + png[-12:] if late else _png(metadata)
    encoded = base64.b64encode(raw).decode()
    if late:
        with pytest.raises(UnreadImage):
            inspect_image(encoded)
    else:
        assert inspect_image(encoded).texts == ("Camera",)


def _webp_in_chunk_tail(kind, tail):
    out = io.BytesIO()
    alpha = kind == b"ALPH"
    Image.new("RGBA" if alpha else "RGB", (8, 8), (255, 255, 255, 128) if alpha else "white").save(
        out, "WEBP", lossless=kind == b"VP8L"
    )
    original = out.getvalue()
    chunks = _webp_chunks(original)
    assert sum(name == kind for name, _ in chunks) == 1
    if alpha:
        assert dict(chunks)[b"ALPH"][0] & 3 == 1, "fixture must exercise compressed auxiliary pixels"
    if not tail:
        return original
    return _webp_container(
        [(name, payload + (b"UNREAD_MEDIA_CANARY" if name == kind else b"")) for name, payload in chunks]
    )


@pytest.mark.parametrize("kind", [b"VP8 ", b"VP8L", b"ALPH"])
@pytest.mark.parametrize("tail", [False, True])
def test_webp_every_admitted_compressed_stream_is_consumed_before_gateway_send(kind, tail, decision_turns):
    from latet.gateway.image_coverage import UnreadImage, inspect_image
    from latet.gateway.outcome import Block
    from latet.vision.codec import decode

    raw = _webp_in_chunk_tail(kind, tail)
    encoded = base64.b64encode(raw).decode()
    expected = (255, 255, 255, 128) if kind == b"ALPH" else (255, 255, 255)
    assert decode(encoded).image.getpixel((0, 0)) == expected
    body = json.dumps(_media_body(raw, "image/webp")).encode()
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        mediator.vision = ImageMasking(FakeOcr())
        response = client.post(CASES[0][1], content=body, headers=CLAUDE)
        assert response.status_code == 400 and not transport.sent
        assert type(decision_turns[-1].decision) is Block
        assert decision_turns[-1].prepared.body == body
        assert all(word in response.text for word in ("WebP", "PNG", "rewind", "new conversation"))
        with pytest.raises(UnreadImage, match="WebP"):
            inspect_image(encoded)


@pytest.mark.parametrize("kind", [b"VP8 ", b"VP8L", b"ALPH"])
def test_rewrite_strips_tail_inside_each_webp_codestream(kind, decision_turns):
    from latet.gateway.image_coverage import inspect_image
    from latet.gateway.outcome import Pass
    from latet.vision.codec import decode

    raw = _webp_in_chunk_tail(kind, True)
    expected = (255, 255, 255, 128) if kind == b"ALPH" else (255, 255, 255)
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        mediator.vision = ImageMasking(FakeOcr())
        response = client.post(CASES[0][1], json=_media_body(raw, "image/webp"), headers=HEADERS)
        assert response.status_code == 200 and len(transport.sent) == 1
        encoded = json.loads(transport.sent[-1].content)["messages"][0]["content"][0]["source"]["data"]
        assert b"UNREAD_MEDIA_CANARY" not in base64.b64decode(encoded)
        assert base64.b64decode(encoded) != raw
        assert decode(encoded).image.getpixel((0, 0)) == expected
        inspect_image(encoded)
        assert type(decision_turns[-1].decision) is Pass


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("mime", ["image/webp", "image/png"])
def test_gateway_webp_conversion_uses_actual_carrier_and_owned_decode(monkeypatch, known, mime, decision_turns):
    from latet.gateway.image_coverage import inspect_image
    from latet.gateway.outcome import Pass
    from latet.vision import masking

    reader = FakeOcr()
    service = ImageMasking(reader)
    raw = _webp_in_chunk_tail(b"VP8L", False)
    if known:
        ref = anthropic.scan_request(_media_body(raw, mime)).images[0]
        prior = service.work(ref)
        prior.decide([(0, 0, 1, 1)], False)
        assert prior.ref.media_type == "image/webp"  # general API is unchanged
        raw = base64.b64decode(prior.ref.data())
    reads = reader.reads
    decoded = []
    real_decode = masking.decode

    def counted(*args, **kwargs):
        result = real_decode(*args, **kwargs)
        decoded.append(result)
        return result

    monkeypatch.setattr(masking, "decode", counted)
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        mediator.vision = service
        original = json.dumps(_media_body(raw, mime)).encode()
        refusal = client.post(CASES[0][1], content=original, headers=CLAUDE)
        assert refusal.status_code == 400 and not transport.sent
        assert decision_turns[-1].prepared.body == original
        assert all(word in refusal.text for word in ("WebP", "PNG", "rewind", "new conversation"))
        assert not decoded and reader.reads == reads  # no pixel work or cache grants in verify-only
        response = client.post(CASES[0][1], json=_media_body(raw, mime), headers=HEADERS)
        assert response.status_code == 200 and len(transport.sent) == 1
        source = json.loads(transport.sent[-1].content)["messages"][0]["content"][0]["source"]
        assert source["media_type"] == "image/png"
        assert base64.b64decode(source["data"]).startswith(b"\x89PNG\r\n\x1a\n")
        inspect_image(source["data"])
        assert type(decision_turns[-1].decision) is Pass
        assert len(decoded) == 1 and decision_turns[-1].prepared.images[0].decoded is decoded[0]
        assert reader.reads == reads + (0 if known else 1)


def test_gateway_webp_repaint_reset_and_cache_preserve_the_owned_pixels(mediator):
    from latet.gateway.image_coverage import inspect_image
    from latet.vision import masking

    reader = FakeOcr()
    raw = base64.b64decode(reader.screenshot(["ordinary words"], fmt="WEBP"))
    service = mediator.vision = ImageMasking(reader)
    prior = service.work(anthropic.scan_request(_media_body(raw, "image/webp")).images[0])
    prior.decide([(0, 0, 2, 2)], False)  # populate generic WebP painted cache, with a retained decision
    assert prior.ref.media_type == "image/webp"
    old_pixels = prior.decoded.image
    result = prepare(mediator, *CASES[0][:2], _media_body(raw, "image/webp"))
    assert not result.block_reason and reader.reads == 1
    work = result.images[0]
    pixels = work.decoded.image
    assert pixels is not old_pixels  # separate request, exactly one owner each
    assert work.ref.media_type == "image/png"  # no generic painted-cache collision
    painted = work.ref.data()
    work.decide([], False)
    clean = work.ref.data()
    assert clean != painted and work.ref.media_type == "image/png"
    assert masking.decode(clean).image.tobytes() == pixels.tobytes()
    work.decide([(0, 0, 2, 2)], False)
    assert work.ref.data() == painted and work.decoded.image is pixels and reader.reads == 1
    inspect_image(clean)
    inspect_image(painted)


@pytest.mark.parametrize("refuse", [False, True])
def test_gateway_webp_conversion_keeps_size_fallback_and_refusal(mediator, monkeypatch, refuse):
    from latet.gateway.image_coverage import inspect_image
    from latet.vision import masking

    real_encode = masking.encode
    calls = []

    def oversized(image, media):
        calls.append(media)
        encoded, actual = real_encode(image, media)
        if media == "image/png" or refuse:
            encoded = base64.b64encode(base64.b64decode(encoded) + b"X" * 2000).decode()
        return encoded, actual

    monkeypatch.setattr(masking, "MAX_SENT", 1000)
    monkeypatch.setattr(masking, "encode", oversized)
    mediator.vision = ImageMasking(FakeOcr())
    raw = _webp_in_chunk_tail(b"VP8L", False)
    result = prepare(mediator, *CASES[0][:2], _media_body(raw, "image/webp"))
    work = result.images[0]
    assert calls[:2] == ["image/png", "image/jpeg"]
    if refuse:
        assert work.state == masking.WITHHELD and "larger" in work.reason
        assert raw not in result.body and base64.b64encode(raw) not in result.body
    else:
        assert not result.block_reason and work.ref.media_type == "image/jpeg"
        assert len(base64.b64decode(work.ref.data())) <= 1000
        inspect_image(work.ref.data())


def _assert_webp_unsupported_then_rewritten(raw):
    from latet.gateway.image_coverage import UnreadImage, inspect_image
    from latet.vision.codec import decode

    encoded = base64.b64encode(raw).decode()
    expected = decode(encoded)
    with pytest.raises(UnreadImage, match="WebP"):
        inspect_image(encoded)
    body = json.dumps(_media_body(raw, "image/webp")).encode()
    with _anthropic_gateway() as (client, transport, _clients, mediator):
        mediator.vision = ImageMasking(FakeOcr())
        refused = client.post(CASES[0][1], content=body, headers=CLAUDE)
        assert refused.status_code == 400 and not transport.sent
        assert all(word in refused.text for word in ("WebP", "PNG", "rewind", "new conversation"))
        rewritten = client.post(CASES[0][1], content=body, headers=HEADERS)
        assert rewritten.status_code == 200 and len(transport.sent) == 1
        source = json.loads(transport.sent[0].content)["messages"][0]["content"][0]["source"]
        assert source["media_type"] == "image/png"
        inspected = inspect_image(source["data"])
        assert not inspected.texts
        pixels = decode(source["data"])
        assert pixels.image.size == expected.image.size and pixels.image.mode == expected.image.mode
        assert pixels.image.tobytes() == expected.image.tobytes()


def test_raw_alpha_webp_is_unsupported_original_and_png_when_rewritten():
    raw = _webp_in_chunk_tail(b"ALPH", False)
    chunks = [(kind, b"\0" + bytes([128]) * 64 if kind == b"ALPH" else payload) for kind, payload in _webp_chunks(raw)]
    _assert_webp_unsupported_then_rewritten(_webp_container(chunks))


@pytest.mark.parametrize("bound", ["bytes", "pixels"])
def test_known_webp_conversion_still_enforces_decode_bounds(mediator, monkeypatch, bound):
    from latet.vision import codec as image_codec
    from latet.vision.masking import WITHHELD

    reader = FakeOcr()
    service = mediator.vision = ImageMasking(reader)
    raw = _webp_in_chunk_tail(b"VP8L", False)
    prior = service.work(anthropic.scan_request(_media_body(raw, "image/webp")).images[0])
    prior.decide([(0, 0, 1, 1)], False)
    raw = base64.b64decode(prior.ref.data())
    if bound == "bytes":
        monkeypatch.setattr(image_codec, "MAX_BYTES", len(raw) - 1)
    else:
        service.max_pixels = 63
    before = reader.reads
    prepared = prepare(mediator, *CASES[0][:2], _media_body(raw, "image/webp"))
    assert prepared.images[0].state == WITHHELD and reader.reads == before
    assert base64.b64encode(raw) not in prepared.body


def test_gateway_webp_masked_text_survives_conversion_and_restores(mediator):
    from latet.engine.rehydrate import restore_text, restores_for
    from latet.gateway.image_coverage import inspect_image

    reader = FakeOcr()
    raw = base64.b64decode(reader.screenshot([EMAIL], fmt="WEBP"))
    mediator.vision = ImageMasking(reader)
    prepared = prepare(mediator, *CASES[0][:2], _media_body(raw, "image/webp"))
    assert not prepared.block_reason
    work = prepared.images[0]
    assert work.ref.media_type == "image/png" and work.replacements and EMAIL not in work.text
    assert restore_text(work.text, restores_for(mediator.engine.vault.entities(), json_mode=False))[0] == EMAIL
    assert work.ref.data() != base64.b64encode(raw).decode() and reader.reads == 1
    inspect_image(work.ref.data())


# --- what an image's accounting costs (G2 review I-3) -----------------------------------------------------------------

_PROFILE_END = (1 << 20) - 64  # a profile of almost the 1 MiB its metadata may be


def _one_pixel_png(*chunks: tuple[bytes, bytes]) -> str:
    """A 1x1 grayscale PNG with ``chunks`` before its pixels, in base64, as a request carries it."""
    header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    body = [(b"IHDR", header), *chunks, (b"IDAT", zlib.compress(b"\0\0")), (b"IEND", b"")]
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"".join(_chunk(kind, data) for kind, data in body)).decode()


def _profile(tags: list[tuple[int, int]], regions: bytes | bytearray) -> bytes:
    """An ICC profile of ``rTRC`` tags at (start, size), and ``regions`` placed after the header and the tag table."""
    table_end = 132 + 12 * len(tags)
    header = bytearray(128)
    struct.pack_into(">I", header, 0, table_end + len(regions))
    header[36:40] = b"acsp"
    table = b"".join(b"rTRC" + struct.pack(">II", start, size) for start, size in tags)
    return bytes(header) + struct.pack(">I", len(tags)) + table + bytes(regions)


def _curve(size: int) -> bytes:
    return b"curv\0\0\0\0" + struct.pack(">I", (size - 12) // 2)


def _one_curve_profile(tags: int = 4090) -> bytes:
    """The review's profile: ``tags`` tags that all name one curve of almost the whole profile."""
    table_end = 132 + 12 * tags
    size = (_PROFILE_END - table_end) // 2 * 2
    curve = _curve(size) + bytes(size - 12)
    return _profile([(table_end, size)] * tags, curve)


def _overlapping_curves_profile(tags: int = 4000) -> bytes:
    """``tags`` tags that each name a curve of its own, every one starting inside the one before and ending at the
    profile's end: distinct ranges, each of almost the whole profile."""
    table_end = 132 + 12 * tags
    end = table_end + (_PROFILE_END - table_end) // 2 * 2
    regions = bytearray(end - table_end)
    ranges = [(table_end + 12 * index, end - table_end - 12 * index) for index in range(tags)]
    for start, size in ranges:
        regions[start - table_end : start - table_end + 12] = _curve(size)
    return _profile(ranges, regions)


def _exif(entries: list[tuple[int, int]]) -> bytes:
    """A little-endian TIFF whose one IFD holds XResolution entries (RATIONAL, numeric) at (pointer, count): 1 MiB."""
    ifd = struct.pack("<H", len(entries)) + b"".join(struct.pack("<HHII", 0x011A, 5, n, at) for at, n in entries)
    tiff = b"II*\0" + struct.pack("<I", 8) + ifd + b"\0\0\0\0"
    return tiff + bytes((1 << 20) - len(tiff))


def _exif_entries(overlapping: bool, count: int = 4000) -> list[tuple[int, int]]:
    start = 8 + 2 + 12 * count + 4
    whole = ((1 << 20) - start) // 8
    return [(start + 8 * index, whole - index) if overlapping else (start, whole) for index in range(count)]


def _fastest(encoded: str) -> tuple[float, type[BaseException] | None]:
    """The best of three runs of ``inspect_image`` (refused or not), and whether it refused."""
    from latet.gateway.image_coverage import UnreadImage, inspect_image

    times, refused = [], None
    for _ in range(3):
        began = time.perf_counter()
        try:
            inspect_image(encoded)
        except UnreadImage as error:
            refused = type(error)
        times.append(time.perf_counter() - began)
    return min(times), refused


@pytest.mark.parametrize("case", ["icc-one-curve", "icc-overlapping-curves", "exif-one-range", "exif-overlapping"])
def test_overlapping_metadata_ranges_are_accounted_once_within_a_bound(case):
    """G2 I-3: an ICC profile's tags, or an EXIF IFD's numeric entries, that all name one large range were covered
    whole once per tag (the review's 1,660-character PNG: 2.2 s of CPU, then accepted); distinct ranges that overlap
    cost the same. Each distinct range is now marked once and every byte covered is charged to the image's metadata
    budget: one shared range is accounted (or refused) at once, and distinct ranges that cover more than the metadata
    holds are refused at once."""
    from latet.gateway.image_coverage import UnreadImage

    if case.startswith("icc"):
        profile = _one_curve_profile() if case == "icc-one-curve" else _overlapping_curves_profile()
        assert len(profile) <= 1 << 20
        encoded = _one_pixel_png((b"iCCP", b"icc\0\0" + zlib.compress(profile, 9)))
        assert len(encoded) < (4096 if case == "icc-one-curve" else 64 * 1024)  # a few KiB on the wire
    else:
        encoded = _one_pixel_png((b"eXIf", _exif(_exif_entries(case == "exif-overlapping"))))
    elapsed, raised = _fastest(encoded)
    if case.endswith("overlapping") or case.endswith("curves"):
        assert raised is UnreadImage
    assert elapsed < 0.05, f"{case}: {elapsed:.3f} s"


def test_a_real_icc_profile_with_shared_tag_data_still_passes():
    """ICC lets tags share their data: Little CMS's sRGB names one curve for its red, green and blue TRCs. A profile
    so made is read, its texts checked, as before."""
    from PIL import ImageCms

    from latet.gateway.image_coverage import inspect_image

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    count = struct.unpack_from(">I", profile, 128)[0]
    ranges = [struct.unpack_from(">II", profile, 136 + 12 * index) for index in range(count)]
    assert len(set(ranges)) < len(ranges)  # shared tag data
    encoded = _one_pixel_png((b"iCCP", b"sRGB\0\0" + zlib.compress(profile)))
    assert any("sRGB" in text for text in inspect_image(encoded).texts)
