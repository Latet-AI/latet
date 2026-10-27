"""EOP-40's unread occurrences through the existing space builder and image consumers.

Prepared without collection or execution. The pool and runtime are real; only model
sessions and persistence are stand-ins. No proposed reporter API is called here.
"""

import base64
import io
import json
import logging
import re
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from onnxruntime.capi.onnxruntime_pybind11_state import Fail

from latet import observe
from latet.bridge.api import Bridge, _answered
from latet.config import Config
from latet.engine.types import SlotKind
from latet.gateway.app import create_app
from latet.gateway.mediate import REWRITE, VERIFY
from latet.gateway.routes import Upstream
from latet.protocols.base import ImageRef
from latet.runtime import image_masking
from latet.server import spaces as spaces_module
from latet.server.metrics import Counter, Metrics
from latet.server.spaces import Spaces
from latet.state import local_backends
from latet.vision import models
from latet.vision import pool as pool_owner
from latet.vision.masking import MASKED, UNREAD, WITHHELD, YOU_WITHHELD
from latet.vision.ocr import (
    OcrBusy,
    OcrError,
    OcrTooLong,
    OcrTooManyTiles,
    OcrTooWide,
)
from latet.vision.pool import (
    OcrPool,
    ServerReading,
)

from ...conftest import ROOT_KEY
from ...fake_ocr import ModelSession, blank_image, lines_map, stand_in_ocr
from .conftest import EMILY, URL, MemorySettings

NAME = "latet_ocr_unread_total"
REASONS = {"busy", "too_long", "too_many_tiles", "too_wide", "unavailable"}
PRIVATE = "synthetic-secret /srv/private-models /u/synthetic-route image-content"
SAMPLE = re.compile(r'^latet_ocr_unread_total\{reason="([a-z_]+)"\} ([0-9.]+)$')
UPSTREAM = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")


def _png(width=40, height=40):
    out = io.BytesIO()
    blank_image(width, height).save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


def _ref(data=None):
    return ImageRef({"data": data or _png()}, "data", "base64", "image/png", SlotKind.USER_TEXT, 0)


def _prepare(runtime, *, checked=True, data=None, copies=1):
    data = data or _png()
    images = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}} for _ in range(copies)
    ]
    body = json.dumps({"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": images}]}).encode()
    claude_code = {"user-agent": "claude-cli/2.1.291", "x-claude-code-session-id": "cc-ocr"}  # checked, never rewritten
    headers = {"content-type": "application/json", **(claude_code if checked else {"user-agent": "python-httpx"})}
    return runtime.mediator.prepare(UPSTREAM, "POST", "/anthropic/v1/messages", headers, body)


def _counts(metrics):
    found = {}
    for line in metrics.render().splitlines():
        match = SAMPLE.fullmatch(line)
        if match:
            found[match[1]] = float(match[2])
        elif line.startswith(NAME + "{"):
            raise AssertionError(f"unexpected OCR metric labels: {line}")
    assert set(found) <= REASONS
    return {reason: found.get(reason, 0.0) for reason in REASONS}


def _only(metrics, reason, amount):
    assert _counts(metrics) == {key: float(amount if key == reason else 0) for key in REASONS}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Build through Spaces._build, replacing database/Valkey edges with local test state."""
    now = [0.0]
    fault = [None]
    installed = [True]
    reads = [0]

    def detect(inputs):
        reads[0] += 1
        if fault[0] is not None:
            raise fault[0]
        return lines_map(inputs)

    reader = stand_in_ocr(det=ModelSession(detect))
    pool = OcrPool([reader], clock=lambda: now[0])
    monkeypatch.setattr(models, "runtime_available", lambda: True)
    monkeypatch.setattr(models, "installed", lambda data: installed[0])
    monkeypatch.setattr(pool_owner, "shared_pool", lambda *args: pool)
    monkeypatch.setattr(spaces_module, "_read_counts", lambda db, owners: dict.fromkeys(owners, 0))
    monkeypatch.setattr(spaces_module, "root_key", lambda *args, **kwargs: ROOT_KEY)
    made, registries, builders = [], [], []

    def backends(server, *args, **kwargs):
        state = replace(
            local_backends(tmp_path / f"state-{len(made)}", None, ROOT_KEY), settings=MemorySettings(Config(ner="off"))
        )
        return SimpleNamespace(state=state, conversations=SimpleNamespace(), vault=state.vault)

    monkeypatch.setattr(spaces_module, "space_backends", backends)

    def build():
        metrics = Metrics(clock=lambda: now[0])
        registries.append(metrics)
        identity = SimpleNamespace(id=EMILY.id, disabled=False, kind="person", vault_limit=None, ref=lambda: EMILY)
        server = SimpleNamespace(
            identities=SimpleNamespace(current=lambda identity_id: identity, tokens=lambda identity_id: ()),
            db=None,
            master=None,
            clock=lambda: now[0],
            shared=SimpleNamespace(counted=lambda **kwargs: (Config(ner="off"), 0)),
            instance=f"test-instance-{len(made)}",
            data=tmp_path / "models",
            reading=ServerReading(readers=1, wait=6, budget=5),
            reviewer_key=None,
            settings=SimpleNamespace(url=URL, max_request_mb=32, key_working_values=20_000, key_held_secrets=1_000),
            metrics=metrics,
        )
        builder = Spaces(server)
        builders.append(builder)
        runtime = builder._build(identity).runtime
        made.append(runtime)
        create_app(runtime.gateway)  # the real app marks the mediator as on-server; no socket is started
        if runtime.mediator.vision is not None:
            runtime.mediator.vision._clock = lambda: now[0]
        return runtime, metrics

    yield SimpleNamespace(build=build, pool=pool, reader=reader, fault=fault, now=now, installed=installed, reads=reads)
    for runtime in made:
        runtime.close()
    for builder in builders:
        builder._executor.shutdown(wait=True, cancel_futures=True)
    for metrics in registries:
        metrics.close()


@pytest.mark.parametrize("checked", [True, False], ids=["checked", "rewritten"])
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (OcrBusy, "busy"),
        (OcrTooLong, "too_long"),
        (OcrTooManyTiles, "too_many_tiles"),
        (OcrTooWide, "too_wide"),
        (OcrError, "unavailable"),
        (OSError, "unavailable"),
        (MemoryError, "unavailable"),
        (Fail, "unavailable"),
    ],
)
def test_terminal_typed_ocr_failure_counts_one_occurrence_without_changing_traffic(rig, error, reason, checked):
    runtime, metrics = rig.build()
    rig.fault[0] = error(PRIVATE)
    png = _png()
    prepared = _prepare(runtime, checked=checked, data=png)
    assert prepared.mode == (VERIFY if checked else REWRITE)
    assert bool(prepared.block_reason) is checked
    assert len(prepared.images) == 1 and prepared.images[0].state == (UNREAD if checked else WITHHELD)
    assert (png in prepared.body.decode()) is checked
    assert PRIVATE not in (prepared.block_reason or "") + metrics.render() + str(prepared.images[0].reason)
    _only(metrics, reason, 1)


def test_counter_is_registered_once_with_only_the_five_reason_values(rig):
    runtime, metrics = rig.build()
    create_app(runtime.gateway)
    create_app(runtime.gateway)
    text = metrics.render()
    assert text.count(f"# TYPE {NAME} counter\n") == 1
    assert text.count(f"# HELP {NAME} ") == 1
    counters = [metric for metric in metrics._all if metric.name == NAME]
    assert len(counters) == 1
    counter = counters[0]
    assert isinstance(counter, Counter) and counter.labels == ("reason",)
    assert counter._allowed == {"reason": frozenset(REASONS)}
    assert "occurrence" in counter.help.lower() and "cached" in counter.help.lower()
    counter.inc(PRIVATE)  # existing registry backstop: free-form labels become fixed 'other'
    assert PRIVATE not in metrics.render()
    assert f'{NAME}{{reason="other"}} 1' in metrics.render()


@pytest.mark.parametrize("checked", [True, False], ids=["checked", "rewritten"])
def test_unavailable_shortcut_reports_each_image_without_invoking_ocr(rig, checked):
    runtime, metrics = rig.build()
    rig.reader._rec = None
    rig.reader.error = OSError(PRIVATE)
    prepared = _prepare(runtime, checked=checked, copies=2)
    assert bool(prepared.block_reason) is checked
    assert len(prepared.images) == 2
    assert rig.reads[0] == 0
    _only(metrics, "unavailable", 2)


@pytest.mark.parametrize("checked", [True, False], ids=["checked", "rewritten"])
def test_a_cached_timeout_counts_later_occurrences_then_expires_without_a_new_failure(rig, checked):
    runtime, metrics = rig.build()
    rig.fault[0] = OcrTooLong(PRIVATE)
    for at in (0.0, 119.0):
        rig.now[0] = at
        assert bool(_prepare(runtime, checked=checked).block_reason) is checked
    assert rig.reads[0] == 1
    _only(metrics, "too_long", 2)
    rig.now[0], rig.fault[0] = 121.0, None
    assert _prepare(runtime, checked=checked).block_reason is None
    assert rig.reads[0] == 2
    _only(metrics, "too_long", 2)


@pytest.mark.parametrize(("error", "reason"), [(OcrTooWide, "too_wide"), (OcrTooManyTiles, "too_many_tiles")])
def test_cached_permanent_failures_keep_their_typed_reason_on_later_occurrences(rig, error, reason):
    runtime, metrics = rig.build()
    rig.fault[0] = error("busy too_long unavailable " + PRIVATE)  # prose cannot choose the label
    _prepare(runtime)
    rig.now[0] = 1_000.0
    _prepare(runtime)
    assert rig.reads[0] == 1
    _only(metrics, reason, 2)


def test_busy_is_retried_and_two_submitted_images_are_two_occurrences(rig):
    runtime, metrics = rig.build()
    rig.fault[0] = OcrBusy("too_wide " + PRIVATE)
    prepared = _prepare(runtime, checked=False, copies=2)
    assert prepared.block_reason is None and len(prepared.images) == 2
    assert rig.reads[0] == 2
    _only(metrics, "busy", 2)


def test_loading_is_retried_and_counts_once_per_occurrence_without_a_loader(rig, monkeypatch):
    runtime, metrics = rig.build()
    rig.reader._rec = None
    monkeypatch.setattr(rig.pool, "start_loading", lambda: None)
    monkeypatch.setattr(rig.pool, "wait_ready", lambda timeout: False)
    for _ in range(2):
        assert _prepare(runtime).block_reason is not None
    assert rig.reads[0] == 0
    _only(metrics, "unavailable", 2)


def test_final_check_and_manual_withhold_on_the_same_work_do_not_report_twice(rig):
    runtime, metrics = rig.build()
    rig.fault[0] = OcrTooLong(PRIVATE)
    prepared = _prepare(runtime, checked=False)
    assert prepared.block_reason is None
    runtime.mediator._check(prepared)
    prepared.images[0].withhold(YOU_WITHHELD)
    runtime.mediator._check(prepared)
    _only(metrics, "too_long", 1)


@pytest.mark.parametrize("late", [False, True], ids=["reapply", "late-install"])
def test_the_reporter_follows_its_runtime_and_never_the_shared_pool_or_observer_broadcast(rig, late):
    rig.installed[0] = not late
    first, own = rig.build()
    second, other = rig.build()
    unlisten = observe.listen(other)
    try:
        if late:
            assert first.mediator.vision is None and second.mediator.vision is None
            rig.installed[0] = True
        for runtime in (first, second):
            runtime.mediator.vision = None  # force reconstruction, as after late model availability
            runtime.apply_config(runtime.config, save=False)
            assert runtime.mediator.vision.ocr.ocr is rig.pool
            assert (runtime.mediator.vision.ocr.wait, runtime.mediator.vision.ocr.budget) == (6, 5)
        assert first.mediator.vision.ocr is not second.mediator.vision.ocr
        rig.fault[0] = OcrBusy(PRIVATE)
        _prepare(first)
        _only(own, "busy", 1)
        assert not any(_counts(other).values())
        _prepare(second)
        _only(other, "busy", 1)
        _only(own, "busy", 1)
    finally:
        unlisten()


@pytest.mark.parametrize("channel", ["bridge", "websocket"])
def test_unavailable_shortcut_on_bridge_and_websocket_reports_one_occurrence(rig, channel):
    runtime, metrics = rig.build()
    rig.reader._rec = None
    rig.reader.error = OSError(PRIVATE)
    png = _png()
    if channel == "bridge":
        asked = {"session": "s-1", "items": [{"image": {"data": png, "media_type": "image/png"}}]}
        bridge = Bridge(runtime.mediator, "test-token", on_server=runtime.gateway.on_server)
        _, out, *_ = bridge._mask(asked)
        assert _answered(out) == [{"count": 0, "image": None, "withheld": "the text recognition is not available"}]
    else:
        image = {"type": "input_image", "image_url": f"data:image/png;base64,{png}"}
        turn = json.dumps({"type": "response.create", "model": "m", "input": [{"role": "user", "content": [image]}]})
        sent, prepared = runtime.mediator.mask_ws_frame(turn, runtime.built.engine.session("s-1"))
        assert prepared is not None and prepared.block_reason is None and png not in sent
    assert rig.reads[0] == 0
    _only(metrics, "unavailable", 1)


def test_mending_a_failed_reader_without_an_image_is_not_an_unread_occurrence(rig):
    runtime, metrics = rig.build()
    rig.reader._rec = None
    rig.reader.error = OSError(PRIVATE)
    made = []

    def make():
        made.append(stand_in_ocr())
        return made[-1]

    rig.pool._make = make
    rig.now[0] = 60.0
    rig.pool.start_loading()
    assert rig.pool.ready and len(made) == 1
    assert _prepare(runtime).block_reason is None
    assert not any(_counts(metrics).values())


def test_success_manual_decision_decode_pixel_policy_and_no_ocr_are_excluded(rig):
    runtime, metrics = rig.build()
    vision = runtime.mediator.vision
    clean = vision.work(_ref(), modify=False)
    assert clean.state != UNREAD
    vision.remember(clean.key, [], True)
    assert vision.work(_ref()).reason == YOU_WITHHELD
    assert vision.work(_ref("not-base64")).state == WITHHELD
    vision.max_pixels = 100
    assert vision.work(_ref(_png(20, 20))).state == WITHHELD
    runtime.mediator.ocr = False
    assert _prepare(runtime).block_reason is None
    runtime.mediator.ocr = True
    runtime.mediator.vision = None
    assert _prepare(runtime).block_reason is None
    assert not any(_counts(metrics).values())


@pytest.mark.parametrize("kind", ["placeholder", "painted"])
def test_exact_own_image_replay_does_not_count_as_a_new_unread_occurrence(rig, kind):
    runtime, metrics = rig.build()
    if kind == "placeholder":
        rig.fault[0] = OcrTooLong(PRIVATE)
    ref = _ref()
    work = runtime.mediator.vision.work(ref)
    if kind == "painted":
        work.decide([(0, 0, 10, 10)], withhold=False)
        assert work.state == MASKED
    else:
        assert work.state == WITHHELD
    assert ref.data() != work.original[0]
    rig.fault[0] = OcrTooLong(PRIVATE)
    assert runtime.mediator.vision.work(_ref(ref.data())).state == MASKED
    assert runtime.mediator.vision.unread(_ref(ref.data()), "the text recognition is not available").state == MASKED
    assert rig.reads[0] == 1
    _only(metrics, "too_long", 1 if kind == "placeholder" else 0)


def test_a_clean_multitile_image_and_computer_failure_do_not_charge_server_registries(rig, tmp_path, monkeypatch):
    runtime, metrics = rig.build()
    assert runtime.mediator.vision.work(_ref(_png(100, 3_000))).state != WITHHELD
    assert rig.reads[0] > 1
    computer = image_masking(tmp_path / "computer")
    assert computer is not None
    monkeypatch.setattr(computer, "ocr", stand_in_ocr())
    assert computer.work(_ref(_png(100, 3_000))).state != UNREAD
    rig.fault[0] = OcrTooLong(PRIVATE)
    # Same physical pool on a computer proves that the reporter does not live on that pool.
    computer.ocr = rig.pool
    assert computer.work(_ref()).state == WITHHELD
    assert not any(_counts(metrics).values())


@pytest.mark.parametrize("checked", [True, False], ids=["checked", "rewritten"])
def test_counter_failure_preserves_traffic_and_logs_only_a_paced_error_type(rig, monkeypatch, caplog, checked):
    original = Counter.inc

    def failing(counter, *values, **kwargs):
        if counter.name == NAME:
            raise RuntimeError(PRIVATE)
        return original(counter, *values, **kwargs)

    monkeypatch.setattr(Counter, "inc", failing)
    runtime, _ = rig.build()
    rig.fault[0] = OcrBusy(PRIVATE)
    caplog.set_level(logging.WARNING)
    for width in range(40, 60):
        prepared = _prepare(runtime, checked=checked, data=_png(width))
        assert bool(prepared.block_reason) is checked
        assert prepared.images[0].state == (UNREAD if checked else WITHHELD)
    messages = [record.getMessage() for record in caplog.records]
    failures = [message for message in messages if "RuntimeError" in message]
    assert len(failures) == 1
    assert PRIVATE not in " ".join(messages)
    assert all(record.exc_info is None for record in caplog.records)


def test_metric_reporting_does_not_swallow_process_interruption(rig, monkeypatch):
    original = Counter.inc

    def interrupted(counter, *values, **kwargs):
        if counter.name == NAME:
            raise KeyboardInterrupt
        return original(counter, *values, **kwargs)

    monkeypatch.setattr(Counter, "inc", interrupted)
    runtime, _ = rig.build()
    rig.fault[0] = OcrBusy(PRIVATE)
    with pytest.raises(KeyboardInterrupt):
        runtime.mediator.vision.work(_ref())


def test_concurrent_occurrences_have_an_exact_count_and_bounded_workers(rig):
    runtime, metrics = rig.build()
    rig.fault[0] = OcrBusy(PRIVATE)
    errors, states = [], []
    gate = threading.Barrier(3, timeout=5)

    def read(width):
        try:
            gate.wait()
            states.append(runtime.mediator.vision.work(_ref(_png(width))).state)
        except Exception as exc:
            errors.append(type(exc).__name__)

    workers = [threading.Thread(target=read, args=(width,)) for width in (40, 41)]
    for worker in workers:
        worker.start()
    try:
        gate.wait()
    finally:
        for worker in workers:
            worker.join(10)
    assert all(not worker.is_alive() for worker in workers) and errors == []
    assert states == [WITHHELD, WITHHELD]
    _only(metrics, "busy", 2)
