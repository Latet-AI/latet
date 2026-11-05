"""Computer/server alignment: accepted preparation API, no alternate content logic."""

from __future__ import annotations

import json

import pytest

from latet.gateway.routes import Upstream
from latet.protocols.codec import DecodedTooLarge

from .test_request_body_coverage import CASES, CLAUDE, HEADERS
from .test_request_outcomes import _mediator


@pytest.mark.parametrize(
    "case", ["unknown-body", "not-object", "definitions", "tokens", "unread-frame", "ws-tokens", "gzip"]
)
def test_a_computer_refuses_what_a_server_refuses(case):
    results = []
    for server in (False, True):
        mediator = _mediator()
        mediator.on_server = server
        if server:
            mediator.body_limit = 32 << 20
        prepared = None
        try:
            upstream = Upstream("openai", "/openai", "https://api.openai.com")
            path, headers, raw = CASES[1][1], HEADERS, b"[]"
            if case == "unknown-body":
                path, raw = "/openai/v1/files", b"unread"
            elif case == "tokens":
                path, raw = "/openai/v1/completions", b'{"model":"m","prompt":[123,456]}'
            elif case == "definitions":
                upstream = Upstream("anthropic", "/anthropic", "https://api.anthropic.com")
                path, headers = CASES[0][1], CLAUDE
                raw = json.dumps(
                    {
                        **CASES[0][2],
                        "tools": [
                            {
                                "name": "lookup",
                                "description": "emily.carter@acme.com",
                                "input_schema": {"type": "object"},
                            }
                        ],
                    }
                ).encode()
            elif case == "gzip":
                headers, raw = {**HEADERS, "content-encoding": "gzip"}, b"not-gzip"
            if case in ("unread-frame", "ws-tokens"):
                session = mediator.engine.session("alignment")
                if case == "unread-frame":
                    prepared = mediator.unread_frame("binary", session)
                else:
                    _, prepared = mediator.mask_ws_frame('{"type":"response.create","input":[123]}', session)
            else:
                try:
                    prepared = mediator.prepare(upstream, "POST", path, headers, raw)
                except (OSError, EOFError) as error:
                    results.append(type(error).__name__)
                    continue
            results.append(bool(prepared and prepared.block_reason))
        finally:
            if prepared is not None and prepared.lease is not None:
                prepared.lease.release()
            mediator.close()
    assert results == [True, True], results


def test_a_computer_bounds_a_request_body_at_32_mib():
    mediator = _mediator()
    try:
        raw = b'{"model":"m","unknown_padding":"' + b"a" * (32 << 20) + b'"}'
        with pytest.raises(DecodedTooLarge):
            mediator.prepare(
                Upstream("openai", "/openai", "https://api.openai.com"), "POST", "/openai/v1/embeddings", HEADERS, raw
            )
    finally:
        mediator.close()


@pytest.mark.parametrize("method", ["read_image", "read_images", "not_read_here"])
def test_a_computer_refuses_what_a_server_refuses_for_unavailable_ocr(method):
    from latet.protocols import anthropic
    from latet.vision.masking import ImageMasking

    from ..fake_ocr import FakeOcr
    from .test_request_body_coverage import _png, image_body

    decisions = []
    for server in (False, True):
        mediator = _mediator()
        mediator.on_server = server
        reader = FakeOcr()
        reader.ready = False
        reader.error = RuntimeError("synthetic unavailable OCR")
        mediator.vision = ImageMasking(reader)
        try:
            scan = anthropic.scan_request(image_body(_png()))
            if method == "read_image":
                result = mediator.read_image(scan.images[0], mediator.engine.session("images"), False)
                decisions.append(result is not None)
            elif method == "read_images":
                decisions.append(bool(mediator._read_images(scan, None, False)))
            else:
                decisions.append(mediator._not_read_here(scan, [], None) is not None)
        finally:
            mediator.close()
    assert decisions == [True, True], decisions
