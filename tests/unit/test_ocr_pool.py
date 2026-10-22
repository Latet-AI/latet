"""A server's text recognitions: a pool of readers, each its own models' sessions and its share of the processors this
instance may use, a read taking the first one free in the order the reads asked, waiting a bounded time, reading a
bounded time and ended there; one read of an identity at a time, so that its burst waits behind itself and never ahead
of another identity's; the bounds server settings, the wait never shorter than the read. Past any bound an image is one
whose text could not be read: nothing is sent unread. A computer reads as before."""

import base64
import io
import math
import threading
import time
from types import SimpleNamespace

import annotated_types
import onnxruntime
import pytest
import yaml

from latet.engine.types import SlotKind
from latet.protocols.base import ImageRef
from latet.server.settings import ServerSettings
from latet.vision import models
from latet.vision import ocr as ocr_module
from latet.vision import pool as pool_module
from latet.vision.masking import BUSY, WITHHELD, ImageMasking
from latet.vision.ocr import (
    BATCH_COLUMNS,
    OcrBusy,
    OcrTooLong,
    OnnxOcr,
    shared_ocr,
)
from latet.vision.pool import (
    SPACE_BATCH_COLUMNS,
    OcrPool,
    ServerReading,
    SpaceReads,
    shared_pool,
    usable_cpus,
)

from ..fake_ocr import ModelSession, blank_image, lines_map, long_lines_map, nothing_read, stand_in_ocr

SETTINGS = {"url": "https://latet.acme.lan", "database_url": "postgresql://latet@127.0.0.1/latet",
            "valkey_url": "redis://127.0.0.1:6379/0"}  # fmt: skip


def _held(go: threading.Event, **options):
    """A reader whose detector waits for ``go`` (``started`` says a read began)."""
    return stand_in_ocr(det=ModelSession(lines_map, go=go), **options)


def _until(done, seconds: float = 5.0) -> bool:
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if done():
            return True
        time.sleep(0.01)
    return done()


def _ref(image) -> ImageRef:
    out = io.BytesIO()
    image.save(out, "PNG")
    data = base64.b64encode(out.getvalue()).decode()
    return ImageRef({"data": data}, "data", "base64", "image/png", SlotKind.TOOL_RESULT, 0)


def _space(pool, wait: float = 10.0, budget: float = 10.0) -> ImageMasking:
    """An identity's space's images, read through ``pool`` as a server's space reads them."""
    return ImageMasking(SpaceReads(pool, wait=wait, budget=budget), space=True)


def _heights(pool) -> list[int]:
    """The height of each image the pool's detectors were given, as they read it (a multiple of 32)."""
    return [shape[2] for reader in pool.readers for shape in reader._det.shapes]


# --- the pool ---------------------------------------------------------------------------------------------------------


def test_two_readers_read_two_images_at_once_and_a_third_waits_for_one_of_them():
    first, second = threading.Event(), threading.Event()
    pool = OcrPool([_held(first), _held(second)])
    reads = [threading.Thread(target=pool.read, args=(blank_image(),)) for _ in range(2)]
    for read in reads:
        read.start()
    assert all(reader._det.started.wait(5) for reader in pool.readers)  # both read at once
    with pytest.raises(OcrBusy):
        pool.read(blank_image(), wait=0.2)  # no reader free within its wait
    first.set()
    second.set()
    for read in reads:
        read.join(10)
    assert pool.read(blank_image(), wait=1, budget=5).text == ""  # every reader given back


def test_a_reader_whose_load_ends_while_its_pool_is_made_is_counted(monkeypatch):
    """T22.16: a reader whose load ends just as its pool counts the loaded ones (here, as the pool's turns are made)
    is counted all the same: otherwise the pool is ready with no turn to give, and every read is refused as busy."""
    reader = stand_in_ocr()
    recognizer, reader._rec = reader._rec, None  # still loading

    class _LoadEnds(pool_module._Turns):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            reader._rec = recognizer  # its load ends now, and it says so if it can (``OnnxOcr._load_guarded``)
            if reader.loaded is not None:
                reader.loaded()

    monkeypatch.setattr(pool_module, "_Turns", _LoadEnds)
    pool = OcrPool([reader])
    assert pool.ready and pool.read(blank_image(), wait=1, budget=5).text == ""


def test_reads_take_the_free_readers_in_the_order_they_asked():
    go = threading.Event()
    pool = OcrPool([_held(go)])
    holder = threading.Thread(target=pool.read, args=(blank_image(),))
    holder.start()
    assert pool.readers[0]._det.started.wait(5)
    order: list[str] = []

    def read(name: str) -> None:
        pool.read(blank_image(), wait=10)
        order.append(name)

    waiting = threading.Thread(target=read, args=("waiting",))
    waiting.start()
    time.sleep(0.2)  # it waits first
    again = threading.Thread(target=read, args=("again",))
    again.start()
    time.sleep(0.2)
    go.set()
    for thread in (holder, waiting, again):
        thread.join(10)
    assert order == ["waiting", "again"]


def test_a_read_asking_again_at_once_for_a_reader_waits_behind_the_one_that_was_waiting():
    """The read that held the pool's one reader asks again as soon as it gives it back (its identity's next image): the
    read that was waiting has the reader first."""
    pool = OcrPool([stand_in_ocr()])
    assert pool._turns.acquire(0)  # the one reader held, as by a read
    order: list[str] = []

    def waiting() -> None:
        pool.read(blank_image(), wait=5)
        order.append("waiting")

    other = threading.Thread(target=waiting)
    other.start()
    assert _until(lambda: len(pool._turns._queue) == 1)
    image = blank_image()  # made first: Pillow lets other threads run while it fills an image
    pool._turns.release()
    pool.read(image, wait=5)  # at once, before the other thread has run
    order.append("again")
    other.join(5)
    assert order == ["waiting", "again"]


def test_at_most_sixteen_reads_for_each_reader_wait_and_one_more_is_refused_at_once():
    """T22.14: each read that waits holds its image and its request's body; past sixteen for each reader, one more is
    refused at once, its image's pixels never made ready."""
    go = threading.Event()
    pool = OcrPool([_held(go)])
    holder = threading.Thread(target=pool.read, args=(blank_image(),))
    holder.start()
    assert pool.readers[0]._det.started.wait(5)
    waiting = [threading.Thread(target=pool.read, args=(blank_image(),), kwargs={"wait": 10}) for _ in range(16)]
    for thread in waiting:
        thread.start()
    assert _until(lambda: len(pool._turns._queue) == 16)
    made_ready: list[str] = []
    image = SimpleNamespace(width=300, height=200, convert=lambda mode: made_ready.append(mode))
    started = time.monotonic()
    with pytest.raises(OcrBusy):
        pool.read(image, wait=2)
    assert time.monotonic() - started < 0.5 and made_ready == []
    go.set()
    for thread in (holder, *waiting):
        thread.join(10)


class _Endless:
    """A model session whose first run lasts until its options are ended (``terminate``), ten seconds at most, and then
    raises as ONNX Runtime does for a run ended so; every later run answers at once."""

    def __init__(self) -> None:
        self.runs = 0
        self.started = threading.Event()

    def run(self, names, inputs, options=None):
        self.runs += 1
        self.started.set()
        ends = time.monotonic() + 10
        while self.runs == 1 and time.monotonic() < ends:
            if options is not None and options.terminate:
                raise RuntimeError("Exiting due to terminate flag being set to true.")
            time.sleep(0.01)
        return lines_map(inputs)


def test_a_read_is_ended_at_its_budget_in_the_middle_of_a_run_and_gives_its_reader_back():
    """T22.4: a run of the models is ended at the read's budget (``RunOptions.terminate``), not at its own end."""
    pool = OcrPool([stand_in_ocr(det=_Endless())])
    started = time.monotonic()
    with pytest.raises(OcrTooLong):
        pool.read(blank_image(), budget=0.2)
    assert time.monotonic() - started < 1
    assert pool.read(blank_image(), wait=1, budget=5).text == ""  # its reader given back


def test_a_read_waiting_behind_one_past_its_budget_is_served_within_the_budget_and_a_second():
    det = _Endless()
    pool = OcrPool([stand_in_ocr(det=det)])
    ended: list[BaseException] = []

    def first() -> None:
        try:
            pool.read(blank_image(), budget=0.2)
        except OcrTooLong as exc:
            ended.append(exc)

    holder = threading.Thread(target=first)
    holder.start()
    assert det.started.wait(5)
    assert pool.read(blank_image(), wait=0.2 + 1, budget=5).text == ""  # never OcrBusy
    holder.join(5)
    assert len(ended) == 1


def test_the_deadline_is_checked_before_the_first_tile_and_before_a_tiles_detection_is_post_processed(monkeypatch):
    """T22.4: between two runs, the deadline is checked too: a read whose budget is spent reads no first tile, and the
    detection that ran past it is not post-processed."""
    processed: list[int] = []
    real = ocr_module.db.boxes
    monkeypatch.setattr(ocr_module.db, "boxes", lambda *args: processed.append(1) or real(*args))
    spent = stand_in_ocr()
    now = time.monotonic()
    with monkeypatch.context() as frozen:  # Windows' clock moves in steps of about 16 ms: no time is spent on one step
        frozen.setattr(time, "monotonic", lambda: now)
        with pytest.raises(OcrTooLong):
            spent.read(blank_image(), budget=0.0)
    assert spent._det.runs == 0
    slow = stand_in_ocr(det=ModelSession(lines_map, seconds=0.3))  # a run that ignores its end
    with pytest.raises(OcrTooLong):
        slow.read(blank_image(), budget=0.1)
    assert slow._det.runs == 1 and processed == []


def test_a_read_waits_a_second_for_a_readers_own_turn_its_loader_may_hold_for_an_instant():
    """T22.11: a reader's loader holds the reader's own turn for the instant it takes to say it loaded."""
    pool = OcrPool([stand_in_ocr()])
    (reader,) = pool.readers
    assert reader._lock.acquire(0)
    threading.Timer(0.3, reader._lock.release).start()
    assert pool.read(blank_image(), wait=1, budget=5).text == ""


def test_every_space_keeps_the_one_pool_whose_readers_failed_which_mends_itself(tmp_path):
    """T22.1: no second pool is made behind the spaces' back while they hold the first."""
    pool = shared_pool(tmp_path / "det", tmp_path / "rec", 1)  # its model folders hold nothing: its loads fail
    assert not pool.wait_ready(5) and pool.error is not None
    assert shared_pool(tmp_path / "det", tmp_path / "rec", 1) is pool


def test_a_read_past_its_budget_through_the_pool_is_not_read_and_gives_its_reader_back():
    pool = OcrPool([stand_in_ocr(rec=ModelSession(nothing_read, seconds=0.05))])
    with pytest.raises(OcrTooLong):
        pool.read(blank_image(height=2000), budget=0.1)
    assert pool.read(blank_image(height=40), wait=1, budget=5).text == ""


# --- an identity's reads ----------------------------------------------------------------------------------------------


def test_one_identitys_reads_wait_behind_each_other_and_never_ahead_of_another_identitys():
    """Through each space's images, as a server reads them: Emily's second image waits for her first, not for a reader,
    and the reader left free goes to Daniel, who asked after her."""
    go = threading.Event()
    pool = OcrPool([_held(go), _held(go)])
    emily, daniel = _space(pool), _space(pool)
    done: list[str] = []

    def read(space: ImageMasking, name: str, height: int) -> None:
        space.work(_ref(blank_image(height=height)))
        done.append(name)

    first = threading.Thread(target=read, args=(emily, "emily-1", 200))  # read 192 high
    first.start()
    assert _until(lambda: _heights(pool) == [192])  # Emily's first holds a reader
    second = threading.Thread(target=read, args=(emily, "emily-2", 232))  # 224
    second.start()
    time.sleep(0.2)  # Emily's second waits for her first, not for a reader
    theirs = threading.Thread(target=read, args=(daniel, "daniel", 264))  # 256
    theirs.start()
    assert _until(lambda: sorted(_heights(pool)) == [192, 256])  # the free reader went to Daniel...
    time.sleep(0.2)
    assert sorted(_heights(pool)) == [192, 256]  # ...and none to Emily's second
    go.set()
    for thread in (first, second, theirs):
        thread.join(10)
    assert done[-1] == "emily-2" and sorted(done) == ["daniel", "emily-1", "emily-2"]


class _OneByOne:
    """A detector whose runs each wait for a gate of their own, in the order they run."""

    def __init__(self, *gates: threading.Event) -> None:
        self.gates, self.runs, self.started, self.shapes = list(gates), 0, threading.Event(), []

    def run(self, names, inputs, options=None):
        gate = self.gates[self.runs]
        self.runs += 1
        self.started.set()
        assert gate.wait(10)
        return lines_map(inputs)


def test_an_identitys_wait_counts_its_own_turn_and_a_free_reader_together():
    """Emily's second image waits for her first, which ends 0.8 s later, and then for the one reader, which Daniel took
    meanwhile: it is refused within her one wait of 2 s, never a wait for each."""
    hers, theirs = threading.Event(), threading.Event()
    pool = OcrPool([stand_in_ocr(det=_OneByOne(hers, theirs))])
    emily, daniel = _space(pool, wait=2.0), _space(pool)
    first = threading.Thread(target=emily.work, args=(_ref(blank_image(height=200)),))
    first.start()
    assert pool.readers[0]._det.started.wait(5)  # Emily's first holds the one reader
    daniels = threading.Thread(target=daniel.work, args=(_ref(blank_image(height=232)),))
    daniels.start()
    time.sleep(0.2)  # Daniel waits for it
    got: dict = {}

    def second() -> None:
        started = time.monotonic()
        got["work"] = emily.work(_ref(blank_image(height=264)))
        got["seconds"] = time.monotonic() - started

    again = threading.Thread(target=second)
    again.start()
    time.sleep(0.8)  # her second waits for her first
    hers.set()  # her first ends: Daniel takes the reader, and her second waits for it
    again.join(5)
    assert got["work"].state == WITHHELD and got["work"].reason == BUSY
    assert 1.6 < got["seconds"] < 2.4  # 2 s in all, not 0.8 s and then 2 s more
    theirs.set()
    for thread in (first, daniels):
        thread.join(10)


def test_a_spaces_two_threads_share_its_one_bounded_turn():
    """T22.8: the space's engine thread and its bridge thread read through its one turn (``SpaceReads``), bounded by its
    wait: while one holds a reader, the other is refused within its wait, a reader free or not."""
    go = threading.Event()
    pool = OcrPool([_held(go), _held(go)])
    mine = _space(pool, wait=0.5)
    engine = threading.Thread(target=mine.work, args=(_ref(blank_image(height=200)),))
    engine.start()
    assert _until(lambda: len(_heights(pool)) == 1)
    got: list = []
    bridge = threading.Thread(target=lambda: got.append(mine.work(_ref(blank_image(height=232)))))
    bridge.start()
    bridge.join(0.5 + 1)
    try:
        assert not bridge.is_alive() and got[0].state == WITHHELD and got[0].reason == BUSY
    finally:
        go.set()
        for thread in (engine, bridge):
            thread.join(10)


# --- the processors, the memory -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("affinity", "cpu_max", "usable"),
    [
        (20, None, 20),
        (20, "max 100000\n", 20),  # no quota
        (20, "400000 100000\n", 4),  # a container limited to four processors (--cpus 4)
        (20, "150000 100000", 1),  # 1.5, rounded down
        (20, "50000 100000", 1),  # half of one: one at least
        (3, "800000 100000", 3),  # a container held to three (--cpuset-cpus)
        (8, "not cpu.max", 8),
        (0, None, None),  # nothing said: the machine's
    ],
)
def test_the_processors_an_instance_may_use_are_the_fewest_its_affinity_and_its_quota_allow(affinity, cpu_max, usable):
    import os

    assert usable_cpus(affinity=lambda: affinity, cpu_max=lambda: cpu_max) == (usable or os.cpu_count() or 1)


def test_each_reader_has_its_share_of_the_processors_this_instance_may_use(tmp_path, monkeypatch):
    monkeypatch.setattr(pool_module, "usable_cpus", lambda: 8)
    pool = shared_pool(tmp_path / "det", tmp_path / "rec", 3)
    assert len(pool.readers) == 3 and {reader.threads for reader in pool.readers} == {2}
    assert shared_pool(tmp_path / "det", tmp_path / "rec", 3) is pool  # one per process
    monkeypatch.setattr(pool_module, "usable_cpus", lambda: 2)
    assert {r.threads for r in shared_pool(tmp_path / "det2", tmp_path / "rec2", 4).readers} == {1}
    monkeypatch.setattr(pool_module, "usable_cpus", lambda: 64)
    assert {r.threads for r in shared_pool(tmp_path / "det3", tmp_path / "rec3", 2).readers} == {4}  # four at most


def test_an_instance_refuses_more_readers_than_its_processors_and_takes_fewer_when_left_unset(monkeypatch):
    """T22.5: readers past the processors would share them past what each was given; given so, the instance does not
    start, in words that name the setting and the numbers; left unset, its default or the processors if fewer."""
    from latet.server.app import open_server, server_reading

    monkeypatch.setattr(pool_module, "usable_cpus", lambda: 2)
    with pytest.raises(ValueError, match="ocr_readers is 3, more than the 2 processors"):
        open_server(ServerSettings(**SETTINGS, ocr_readers=3))  # at its start, before anything is opened
    assert server_reading(ServerSettings(**SETTINGS, ocr_readers=2)).readers == 2
    monkeypatch.setattr(pool_module, "usable_cpus", lambda: 1)
    assert server_reading(ServerSettings(**SETTINGS)).readers == 1


def test_a_servers_reader_reads_one_long_line_at_a_time_and_a_computers_as_before(tmp_path, monkeypatch):
    """T22.3: a server's readers give the recognizer batches of 16,384 columns at most, which bound what one read takes
    whatever its image; a computer's reader keeps its batches of 65,536."""
    monkeypatch.setattr(pool_module, "usable_cpus", lambda: 4)
    (server,) = shared_pool(tmp_path / "det", tmp_path / "rec", 1).readers
    computer = shared_ocr(tmp_path / "det", tmp_path / "rec")
    seen: dict[str, list[tuple[int, ...]]] = {"server": [], "computer": []}
    for name, reader in (("server", server), ("computer", computer)):
        reader._det = ModelSession(long_lines_map)
        reader._rec = ModelSession(
            lambda inputs, name=name: seen[name].append(inputs["x"].shape) or nothing_read(inputs)
        )
        reader._chars = ["", "a", " "]
        reader.read(blank_image(3_000, 1_200))  # lines read at about 10,000 columns wide
    assert seen["server"] and all(batch * width <= SPACE_BATCH_COLUMNS for batch, _, _, width in seen["server"])
    assert all(batch * width <= BATCH_COLUMNS for batch, _, _, width in seen["computer"])
    assert max(batch for batch, *_ in seen["computer"]) > 1  # several lines a batch, as before


def test_a_servers_readers_neither_spin_nor_keep_an_arena_and_a_computers_as_before(tmp_path, monkeypatch):
    """T22.5, T22.3: a server's readers' sessions do not spin between runs, holding cores another reader needs, nor keep
    what a read took; a computer's are made as before."""
    made: list = []

    class _Options:  # ONNX Runtime's SessionOptions, standing in
        def __init__(self) -> None:
            self.entries: dict[str, str] = {}
            self.enable_cpu_mem_arena = True
            made.append(self)

        def add_session_config_entry(self, key: str, value: str) -> None:
            self.entries[key] = value

    monkeypatch.setattr(onnxruntime, "SessionOptions", _Options)
    monkeypatch.setattr(onnxruntime, "InferenceSession", lambda path, options, providers: SimpleNamespace())
    monkeypatch.setattr(pool_module, "usable_cpus", lambda: 4)
    det, rec = tmp_path / "det", tmp_path / "rec"
    det.mkdir()
    rec.mkdir()
    (det / "inference.yml").write_text(yaml.safe_dump({"PostProcess": {"thresh": 0.3, "box_thresh": 0.6,
                                                                        "unclip_ratio": 1.5}}), "utf-8")  # fmt: skip
    (rec / "inference.yml").write_text(yaml.safe_dump({"PostProcess": {"character_dict": ["a"]}}), "utf-8")
    (server,) = shared_pool(det, rec, 1).readers
    server._load()
    OnnxOcr(det, rec)._load()
    assert made[0].entries == {"session.intra_op.allow_spinning": "0"} and made[0].enable_cpu_mem_arena is False
    assert made[1].entries == {} and made[1].enable_cpu_mem_arena is True


# --- the bounds -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bounds",
    [
        {"readers": 0},
        {"readers": 17},
        {"readers": True},
        {"wait": math.nan},
        {"wait": math.inf},
        {"budget": math.nan},
        {"budget": 0},
        {"budget": -1.0},
        {"wait": 10.0, "budget": 20.0},  # a wait shorter than the read
    ],
)
def test_a_servers_reading_takes_no_value_that_removes_a_bound(bounds):
    with pytest.raises(ValueError, match=r"1 to 16 text recognitions|finite number of seconds|at least its budget"):
        ServerReading(**bounds)


def test_no_wait_and_no_budget_that_never_ends():
    """T22.12: ``Condition.wait_for(..., nan)`` never times out and spins a core; a NaN budget gives no deadline."""
    turns = ocr_module._Turns()
    for wrong in (math.nan, math.inf):
        with pytest.raises(ValueError, match="finite number of seconds"):
            turns.acquire(wrong)
    assert turns.acquire(0)  # the turn was never taken by the refused ones
    turns.release()
    with pytest.raises(ValueError, match="finite number of seconds"):
        stand_in_ocr().read(blank_image(), budget=math.nan)


def test_the_servers_settings_bound_the_reading_and_the_wait_is_never_shorter_than_the_read():
    settings = ServerSettings(**SETTINGS)
    assert (settings.ocr_readers, settings.ocr_read_seconds, settings.ocr_wait_seconds) == (
        pool_module.SPACE_READERS, pool_module.SPACE_READ_SECONDS, pool_module.SPACE_WAIT_SECONDS,
    )  # fmt: skip
    assert ServerSettings(**SETTINGS, ocr_readers=2, ocr_read_seconds=30, ocr_wait_seconds=40).ocr_readers == 2
    with pytest.raises(ValueError, match="ocr_wait_seconds"):
        ServerSettings(**SETTINGS, ocr_read_seconds=30, ocr_wait_seconds=10)
    for wrong in ({"ocr_readers": 0}, {"ocr_readers": 17}, {"ocr_read_seconds": 1}, {"ocr_wait_seconds": 301}):
        with pytest.raises(ValueError, match=next(iter(wrong))):
            ServerSettings(**SETTINGS, **wrong)


def test_each_default_of_the_servers_settings_lies_within_its_bounds():
    """T22.6: pydantic checks no default against its field's bounds: a measure past one would pass silently."""
    for name, field in ServerSettings.model_fields.items():
        for bound in field.metadata:
            if isinstance(bound, annotated_types.Ge):
                assert field.default >= bound.ge, name
            if isinstance(bound, annotated_types.Le):
                assert field.default <= bound.le, name
    reading = ServerReading()  # the defaults make a reading
    assert (reading.readers, reading.wait, reading.budget) == (
        ServerSettings(**SETTINGS).ocr_readers, ServerSettings(**SETTINGS).ocr_wait_seconds,
        ServerSettings(**SETTINGS).ocr_read_seconds,
    )  # fmt: skip


def test_a_space_reads_through_the_servers_pool_and_a_computer_as_before(tmp_path, monkeypatch):
    from latet.runtime import image_masking

    monkeypatch.setattr(models, "runtime_available", lambda: True)
    monkeypatch.setattr(models, "installed", lambda data: True)
    reading = ServerReading(readers=3, wait=40, budget=30)
    space, computer = image_masking(tmp_path, None, space=True, reading=reading), image_masking(tmp_path, None)
    assert space is not None and computer is not None
    assert isinstance(space.ocr, SpaceReads) and (space.ocr.wait, space.ocr.budget) == (40, 30)
    assert space.ocr.ocr is shared_pool(*models.folders(tmp_path), 3) and space.space
    assert computer.ocr is shared_ocr(*models.folders(tmp_path)) and not computer.space  # one reader, no bound
    assert (computer.ocr.batch_columns, computer.ocr.arena, computer.ocr.spinning) == (BATCH_COLUMNS, True, True)
