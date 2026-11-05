"""Actual WS prior-turn ownership at the next provider receive gate."""

from __future__ import annotations

import weakref
from pathlib import Path

import pytest

from latet.gateway.recorder import ResponseCapture
from latet.gateway.response_finalizer import ResponseFinalizer

from .test_response_ws_lifecycle import _answer, _history, _Profile, _raw, _relay, _Wire

_PROFILE = _Profile(client_frames=2, provider_frames=6, asgi_sends=8, owners=2, engine_jobs=16)


async def test_ws_releases_previous_response_before_next_provider_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs: tuple[weakref.ReferenceType[ResponseFinalizer], weakref.ReferenceType[ResponseCapture]] | None = None

    async with _relay(tmp_path, monkeypatch, _PROFILE) as (fixture, _, recorder, store, directory):
        construct = ResponseFinalizer.__init__
        receive = _Wire.__anext__

        def observe(finalizer, *args, **kwargs) -> None:
            nonlocal refs
            construct(finalizer, *args, **kwargs)
            if refs is None:
                refs = weakref.ref(finalizer), weakref.ref(finalizer.capture)

        async def receive_probe(wire: _Wire) -> str:
            if wire.reads == 3:
                wire.marks.mark("receive-after-a")
            return await receive(wire)

        with monkeypatch.context() as patch:
            patch.setattr(ResponseFinalizer, "__init__", observe)
            patch.setattr(_Wire, "__anext__", receive_probe)
            fixture.create("a")
            await fixture.marks.wait("send-1")
            fixture.wire.emit(*_answer("a"))
            await fixture.marks.wait("published-1")
            await fixture.marks.wait("receive-after-a")
            history_a = _history(recorder, store)
            a_published = (
                fixture.published == 1
                and set(history_a) == {"a"}
                and history_a["a"].get("text") == "ordinary answer a"
                and _raw(directory)[0] == _answer("a")
            )

            fixture.create("b")
            await fixture.marks.wait("send-2")
            b_gated = (
                fixture.owners == fixture.begins == 2
                and fixture.allowed == [True, True]
                and fixture.validations == fixture.send_calls == 2
                and fixture.wire.sent_types == ["response.create", "response.create"]
                and fixture.wire.reads == fixture.wire.emitted == 3
                and fixture.wire.incoming.empty()
                and fixture.published == 1
            )
            assert refs is not None, "A weak observation was not registered"
            finalizer_ref, capture_ref = refs
            finalizer_released = finalizer_ref() is None
            capture_released = capture_ref() is None

            fixture.wire.emit(*_answer("b"))
            await fixture.marks.wait("published-2")
            await fixture.join()

        history = _history(recorder, store)
        expected = _answer("a") + _answer("b")
        checks = {
            "A was actually published and drained before B": a_published,
            "B reached its provider with first frame still gated": b_gated,
            "A finalizer gone at B gate": finalizer_released,
            "A capture gone at B gate": capture_released,
            "both A weak references clear after relay cleanup": finalizer_ref() is None and capture_ref() is None,
            "two safe turns and normal close": (
                _raw(directory) == (expected, [], 1)
                and tuple(fixture.client.texts()) == expected
                and set(history) == {"a", "b"}
                and all(history[tag].get("text") == f"ordinary answer {tag}" for tag in ("a", "b"))
                and fixture.actual_cleanup()
            ),
        }
        assert all(checks.values()), checks
