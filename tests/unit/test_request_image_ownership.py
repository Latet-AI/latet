"""Image cache ownership across an actual pending Live Review and later carrier changes."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, PngImagePlugin

from latet.config import LiveReview
from latet.gateway.recorder import EncodedImage, Outcome, Recorder
from latet.gateway.routes import Upstream
from latet.server.queue import ValkeyApprovals
from latet.server.sealing import Sealer
from latet.vision.masking import CLEAN, MASKED, WITHHELD

from ..conftest import ROOT_KEY
from ..fake_ocr import FakeOcr
from ..thread_pools import frame_locals
from .test_request_observations import _bare_recorder
from .test_request_outcomes import OPENAI, _chat, _mediator, _strict, _until


def _request(form: str, png: str) -> tuple[bytes, str]:
    if form == "data_url":
        return _chat("please inspect this image", image=png), "/v1/chat/completions"
    return json.dumps(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "please inspect this image"},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": png.replace("+", "-").replace("/", "_").rstrip("="),
                            }
                        },
                    ],
                }
            ]
        }
    ).encode(), "/v1beta/models/m:generateContent"


async def _prepared_image(form: str, reader: FakeOcr, png: str) -> tuple[Any, Any, str]:
    mediator = _mediator(reader)
    body, path = _request(form, png)
    arena = await mediator.run_held(mediator.prepare, OPENAI, "POST", path, {}, body)
    assert len(arena.images) == 1 and not arena.block_reason
    return mediator, arena, path


def _snapshot(arena: Any, path: str) -> Any:
    return Recorder(None).snapshot(Outcome({}, "openai", path, "http", 0, arena))


def _recorded_image(tmp_path: Any, arena: Any, path: str, expected: str) -> None:
    recorder, store = _bare_recorder(tmp_path)
    try:
        recorder.finish(Outcome({}, "openai", path, "http", 0, arena))
        assert recorder.drain()
        assert store.image(store.image_id(expected)) == ("image/png", base64.b64decode(expected))
    finally:
        recorder.close()
        store.close()


@pytest.mark.parametrize("form", ["data_url", "base64url"])
async def test_pending_live_review_image_cache_uses_an_already_charged_payload(form):
    reader = FakeOcr()
    png = reader.screenshot(["ordinary image words"])
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("synthetic-padding", "x" * (64 * 1024))
    with Image.open(io.BytesIO(base64.b64decode(png))) as image:
        padded = io.BytesIO()
        image.save(padded, format="PNG", pnginfo=metadata)
    mediator, arena, _ = await _prepared_image(form, reader, base64.b64encode(padded.getvalue()).decode())
    mediator.live_review = LiveReview(enabled=True, auto_approve_clean=False)
    task = asyncio.create_task(mediator.live_prepared(arena, "image-ownership"))
    try:
        await _until(lambda: bool(mediator.approvals.batches) and bool(mediator._held_live))
        batch = next(iter(mediator.approvals.batches.values()))
        work = arena.images[0]
        assert not task.done() and frame_locals(task.get_stack()[0])["prepared"] is arena
        assert any(image is work for image in batch.images.values())
        # Exercise the real admission and held-byte algorithms without any database/Valkey writes.
        queue = object.__new__(ValkeyApprovals)
        queue.identity, queue.instance = "image-ownership", "synthetic-instance"
        queue._held = {batch.id: batch}
        queue.bus = SimpleNamespace(key=lambda value: "image-ownership:" + value)
        queue._sealer = Sealer(ROOT_KEY, b"valkey")
        await mediator.run(queue._sealed_records, batch)
        charge = queue.held_bytes
        charged = [
            item[side] for item in batch.items for side in ("original", "masked") if isinstance(item.get(side), str)
        ]
        for image in batch.images.values():
            charged.extend((image.original[0], image.ref.holder.get(image.ref.key)))
        frozen = arena.frozen_image_data[0][2]
        assert isinstance(frozen, EncodedImage) and frozen.raw is work.ref.holder[work.ref.key]
        assert frozen.form == work.ref.form and work.current_original is None
        assert arena.record_images[0].data is frozen and any(frozen.raw is value for value in charged)
        # M3 normalization changed the carrier; its shared wrapper adds no canonical payload while waiting.
        await mediator.run(arena.refresh_recording)
        assert arena.frozen_image_data[0][2] is frozen and arena.record_images[0].data is frozen
        arena.frozen_image_data = ()
        arena.record_images = ()
        assert queue.held_bytes == charge and not task.done()  # caches added no payload outside those owners
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if arena.lease is not None:
            arena.lease.release()
        mediator.approvals.close()
        mediator.executor.shutdown(wait=True)


@pytest.mark.parametrize("form", ["data_url", "base64url"])
@pytest.mark.parametrize("changed", [False, True], ids=["equal-distinct-carrier", "changed-carrier"])
async def test_an_unproven_clean_carrier_is_shared_and_normalized_only_by_recorder(form, changed, tmp_path):
    reader = FakeOcr()
    first, second = reader.screenshot(["same"]), reader.screenshot(["different"])
    mediator, arena, path = await _prepared_image(form, reader, first)
    calls: list[str] = []

    class Carrier(str):
        def partition(self, separator: str) -> tuple[str, str, str]:
            calls.append(threading.current_thread().name)
            return super().partition(separator)

        def replace(self, old: str, new: str, count: int = -1) -> str:
            calls.append(threading.current_thread().name)
            return super().replace(old, new, count)

    work = arena.images[0]
    expected = second if changed else first
    encoded = (
        ("data:image/png;base64," + expected)
        if form == "data_url"
        else expected.replace("+", "-").replace("/", "_").rstrip("=")
    )
    raw = Carrier(encoded)
    work.ref.holder[work.ref.key] = raw
    assert work.state == CLEAN and raw is not work.original[0]
    try:
        await mediator.run(arena.refresh_recording)
        snap = _snapshot(arena, path)
        assert snap is not None
        carried = snap.images[0].data
        assert getattr(work, "_original_carrier", None) is None  # no obsolete carrier owner after provenance failed
        assert calls == []  # no early canonical duplicate is materialized during refresh/snapshot
        assert carried is raw or getattr(carried, "raw", None) is raw
        _recorded_image(tmp_path, arena, path, expected)
        assert calls and all(name.startswith("latet-recorder") for name in calls)
    finally:
        if arena.lease is not None:
            arena.lease.release()
        mediator.executor.shutdown(wait=True)


@pytest.mark.parametrize("form", ["data_url", "base64url"])
async def test_paint_then_original_restore_and_withhold_keep_image_ownership_coherent(form, tmp_path, monkeypatch):
    reader = FakeOcr()
    original, painted = reader.screenshot(["same"]), reader.screenshot(["painted"])
    mediator, arena, path = await _prepared_image(form, reader, original)
    work = arena.images[0]
    monkeypatch.setattr(work.service, "paint", lambda *args: (painted, "image/png"))
    try:

        def paint() -> None:
            work.rects = [(0, 0, 1, 1)]
            work.masked("text", work.text, [])
            arena.refresh_recording()

        await mediator.run(paint)
        assert work.state == MASKED
        assert getattr(work, "_original_carrier", None) is None  # painting must not keep the old raw carrier alive
        snap = _snapshot(arena, path)
        assert snap is not None
        raw = work.ref.holder[work.ref.key]
        assert snap.images[0].data is raw or getattr(snap.images[0].data, "raw", None) is raw
        _recorded_image(tmp_path, arena, path, painted)

        def restore() -> None:
            work.rects = []
            work.masked("text", work.text, [])
            arena.refresh_recording()

        await mediator.run(restore)
        snap = _snapshot(arena, path)
        assert snap is not None and work.state == CLEAN
        carried = snap.images[0].data
        assert isinstance(carried, EncodedImage) and carried.raw is work.ref.holder[work.ref.key]
        assert carried.form == work.ref.form and work.current_original is None
        assert getattr(work, "_original_carrier", None) is None
        assert arena.frozen_image_data[0][2] is carried
        _recorded_image(tmp_path, arena, path, original)  # reset preserves the exact clean normalized pixels
        await mediator.run(arena.refresh_recording)
        assert _snapshot(arena, path).images[0].data is carried
        assert arena.frozen_image_data[0][2] is carried
        await mediator.run(work.withhold, "test withholding")
        await mediator.run(arena.refresh_recording)
        assert work.state == WITHHELD and _snapshot(arena, path).images[0].data is None
    finally:
        if arena.lease is not None:
            arena.lease.release()
        mediator.executor.shutdown(wait=True)


async def test_record_only_carrier_wrapper_failure_drops_record_but_keeps_masked_send(monkeypatch):
    from latet.gateway import mediate, outcome
    from latet.gateway.coverage import envelope_of

    reader = FakeOcr()
    png = reader.screenshot(["Email: emily.carter@acme.com"])
    mediator = _mediator(reader)
    body, path = _request("data_url", png)

    def unavailable(*args: Any) -> Any:
        raise MemoryError("synthetic wrapper allocation")

    monkeypatch.setattr(mediate, "EncodedImage", unavailable)
    arena = None
    try:
        allocation_error = None
        try:
            arena = await mediator.run_held(mediator.prepare, OPENAI, "POST", path, {}, body)
        except MemoryError:
            allocation_error = "MemoryError"
        assert allocation_error is None, "record-only wrapper allocation escaped request preparation"
        assert arena is not None and arena.record_error == "MemoryError" and not arena.block_reason
        assert arena.masked_count > 0 and arena.images[0].state == MASKED
        envelope = envelope_of({"type": "http", "method": "POST", "path": path}, "http", "openai", body)
        # With the admission's proof, as the forwarder decides (855a0188), for the route the path was prepared on.
        decision = _strict(
            arena, envelope, Upstream("openai", "", OPENAI.base_url, envelope_expected=OPENAI.envelope_expected)
        )
        assert outcome.sendable(decision, arena, envelope).payload is arena.body
        assert _snapshot(arena, path) is None and arena.frozen_image_data == () and arena.record_images == ()
    finally:
        if arena is not None and arena.lease is not None:
            arena.lease.release()
        mediator.executor.shutdown(wait=True)


@pytest.mark.parametrize("form", ["data_url", "base64url"])
async def test_original_provenance_requires_the_form_as_well_as_the_carrier(form):
    reader = FakeOcr()
    mediator, arena, path = await _prepared_image(form, reader, reader.screenshot(["same"]))
    work = arena.images[0]
    raw = work.ref.holder[work.ref.key]
    changed_form = "base64url" if form == "data_url" else "data_url"
    try:
        work.ref.form = changed_form  # same immutable raw object, a different interpretation
        await mediator.run(arena.refresh_recording)
        snap = _snapshot(arena, path)
        assert snap is not None
        assert getattr(work, "_original_carrier", None) is None
        assert getattr(snap.images[0].data, "raw", None) is raw
        assert getattr(snap.images[0].data, "form", None) == changed_form
    finally:
        if arena.lease is not None:
            arena.lease.release()
        mediator.executor.shutdown(wait=True)


async def test_a_non_string_replaced_carrier_drops_record_instead_of_retaining_a_mutable_payload():
    reader = FakeOcr()
    mediator, arena, path = await _prepared_image("data_url", reader, reader.screenshot(["same"]))
    try:
        arena.images[0].ref.holder[arena.images[0].ref.key] = {"mutable": "replacement"}
        await mediator.run(arena.refresh_recording)
        assert arena.record_error == "TypeError"
        assert arena.frozen_image_data == () and arena.record_images == () and _snapshot(arena, path) is None
    finally:
        if arena.lease is not None:
            arena.lease.release()
        mediator.executor.shutdown(wait=True)
