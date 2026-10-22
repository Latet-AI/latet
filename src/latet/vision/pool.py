"""A server's spaces share bounded text-recognition readers and take turns through one pool."""

from __future__ import annotations

import functools
import math
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from latet.vision import tiles
from latet.vision.layout import OcrResult
from latet.vision.ocr import MAX_TILES, OcrBusy, OcrNotReady, OcrTooManyTiles, OnnxOcr, _Turns

if TYPE_CHECKING:
    from PIL import Image

# A server's bounds on the text recognitions its spaces share (the settings ocr_read_seconds, ocr_wait_seconds and
# ocr_readers, ``ServerSettings``: these are their defaults). Measured on the machine the plan was executed on (Task
# 22's report: an i9-10850K, other processes using 6 of its 20 logical processors on average meanwhile): the process
# held to four logical processors, each reader with the threads ``shared_pool`` gives it there (four for one reader, two
# each for two), batches of SPACE_BATCH_COLUMNS, ONNX Runtime's arena off, no spinning; the slowest of three reads.
# - The read's budget: 1.5 times the slowest dense 4K or 5K screenshot's read while two readers read at once, rounded
#   up to a multiple of 5 s (the 5120x2160 screen at 100%, 18.1 s; 8.2 s for one reader alone): 30 s.
# - The wait: the budget and the larger of 5 s and twice the longest a read ran past its budget before it ended (0.17 s
#   at one thread, its runs ended at the budget: ``_Deadlines``), so that a read waits at least as long as another
#   reads, overrun included (turns are taken in order: ``_Turns``): 35 s.
# - The readers: two, since two at once read every large screen within the budget, and two of the worst reads the
#   ceilings let through at once (a 5K screen; a terminal across a 5K screen, a page of long lines, a page full of text)
#   took 2.4 GB at most with what the process holds idle, under 4 GiB; else one.
# A reader holds 0.05 GB at rest (the process held 0.73 GB with one, Python and ONNX Runtime included), and one read up
# to 0.9 GB more (a 5K screen). A terminal across a 5K screen, a page of long lines or a page full of text takes 33 to
# 40 s even alone: not read within the budget, its image is withheld, or the request that only carries it stopped.
SPACE_READ_SECONDS = 30.0
SPACE_WAIT_SECONDS = 35.0  # SPACE_READ_SECONDS + 5
SPACE_READERS = 2
# And the largest image a space reads, below a computer's: a screen of 5K is 14.7 megapixels, and what an image costs to
# read and hold grows with its pixels.
SPACE_MAX_PIXELS = 16_000_000
# What a server's reader reads at once: batches of the recognizer one line at MAX_LINE_WIDTH wide at most, which bounds
# the memory one read takes whatever its image (a terminal across a 5K screen took 3.6 to 4.7 GB more than an idle
# reader in batches of BATCH_COLUMNS, 0.6 GB in these). And no memory arena of ONNX Runtime's, which would keep what a
# read took for the next ones (0.9 GB after that terminal): without it the slowest large screen read no slower (18.1 s
# two at once, against 19.4 s with it), which the budget is measured by, and small screens up to a third slower (1440p
# two at once, 10.2 s against 7.7 s).
SPACE_BATCH_COLUMNS = 16_384
SPACE_ARENA = False
# How many readers a pool holds at most (the setting ocr_readers), how many reads may wait for one of them at once for
# each (past them a read is refused at once: each holds its image and its request's body while it waits), and how often
# a reader whose load failed is made again at most.
MAX_READERS = 16
WAITING_PER_READER = 16
MEND_SECONDS = 60.0


def usable_cpus(
    *, affinity: Callable[[], int | None] | None = None, cpu_max: Callable[[], str | None] | None = None
) -> int:
    """The processors this process may use, one at least: the smaller of those its affinity lets it run on (a
    container's --cpuset-cpus; ``os.process_cpu_count``) and, on Linux, its control group's quota (``cpu.max``: the
    quota over the period, rounded down, one at least). ``affinity`` and ``cpu_max`` give the two counts' sources."""
    count = (affinity or _affinity)() or os.cpu_count() or 1
    quota = _quota((cpu_max or _cpu_max)())
    return max(1, min(count, quota) if quota is not None else count)


def _affinity() -> int | None:
    """The processors this process's affinity lets it run on (the whole machine's where nothing says)."""
    counted = getattr(os, "process_cpu_count", None)  # Python 3.13
    if counted is not None:
        return counted()
    allowed = getattr(os, "sched_getaffinity", None)  # Python 3.12, on Linux
    if allowed is not None:
        return len(allowed(0))
    return os.cpu_count()


def _cpu_max() -> str | None:
    """What the process's control group says of its share of the processors (cgroup v2), on Linux; None elsewhere."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return Path("/sys/fs/cgroup/cpu.max").read_text("ascii")
    except (OSError, UnicodeDecodeError):
        return None


def _quota(text: str | None) -> int | None:
    """``cpu.max``'s quota in whole processors: ``200000 100000`` is 2, ``50000 100000`` is 1 (rounded down, one at
    least); None for no quota (``max 100000``), or for what cannot be read."""
    parts = (text or "").split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, quota // period)


@dataclass(frozen=True)
class ServerReading:
    """A server's bounds on the text recognitions every space of its process shares: ``readers`` of them, each reading
    one image at a time; a read waits ``wait`` seconds at most (for its identity's other read, then for a reader) and
    reads ``budget`` seconds at most. Raises ValueError for bounds that would bound nothing: readers outside 1 to
    ``MAX_READERS``, a wait or a budget that is not a finite number above zero, a wait shorter than the budget."""

    readers: int = SPACE_READERS
    wait: float = SPACE_WAIT_SECONDS
    budget: float = SPACE_READ_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.readers, bool) or not isinstance(self.readers, int) or not 1 <= self.readers <= MAX_READERS:
            raise ValueError(f"a server keeps 1 to {MAX_READERS} text recognitions")
        for name, value in (("wait", self.wait), ("budget", self.budget)):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"a read's {name} is a finite number of seconds above zero")
        if self.wait < self.budget:
            raise ValueError("a read waits at least as long as another reads: its wait is at least its budget")


class OcrPool:
    """Text recognitions of one pair of model folders, every space of a server's process reading through them
    (``SpaceReads``): a read takes a free one that has loaded, in the order the reads asked, waiting ``wait`` seconds at
    most for one, and reads ``budget`` seconds at most there; at most ``WAITING_PER_READER`` reads for each reader wait
    at once, and one past them is refused at once. Each holds its own models' sessions in memory, and its share of the
    processors (``shared_pool``); one image's pixels are made ready to read once a reader is free, never while a read
    waits. It is ready while one reader at least has loaded; a reader whose load failed is made again in its place by
    ``make``, at a read, at most once every ``MEND_SECONDS`` (on ``clock``), the others reading meanwhile; and it has an
    error only while every reader has failed and none is due to be made again."""

    def __init__(
        self,
        readers: Sequence[OnnxOcr],
        *,
        make: Callable[[], OnnxOcr] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not readers or len(readers) > MAX_READERS:
            raise ValueError(f"a pool of text recognitions holds 1 to {MAX_READERS}")
        self.readers = list(readers)
        self._make, self._clock = make, clock
        self._mended = clock()  # when its readers were made, or made again
        self._taking = threading.Lock()
        self._free: deque[OnnxOcr] = deque(self.readers)
        self._turns = _Turns(0, waiting=len(readers) * WAITING_PER_READER)
        # With its arena on, a reader keeps what its last read took: the one given back last is taken first, so that one
        # identity's reads in a row stay on one reader; with none, the one that waited longest.
        self._last_first = any(reader.arena for reader in self.readers)
        # Told first, then counted: a reader whose load ends meanwhile is counted either way, never left out.
        for reader in self.readers:
            reader.loaded = self._loaded
        self._loaded()

    @property
    def ready(self) -> bool:
        return any(reader.ready for reader in self.readers)

    @property
    def error(self) -> BaseException | None:
        errors = [reader.error for reader in self.readers]
        if any(error is None for error in errors) or self._due():
            return None
        return errors[0]

    def start_loading(self) -> None:
        self._mend()
        for reader in list(self.readers):
            reader.start_loading()

    def wait_ready(self, timeout: float = 120.0) -> bool:
        self.start_loading()
        deadline = time.monotonic() + timeout
        for reader in list(self.readers):
            reader.wait_ready(max(0.0, deadline - time.monotonic()))
        return self.ready

    def read(self, image: Image.Image, *, wait: float | None = None, budget: float | None = None) -> OcrResult:
        """The text in ``image``, read by the first reader free with ``budget`` (``OnnxOcr.read``). Raises OcrNotReady
        while no reader has loaded, OcrTooManyTiles for an image that would take more than ``MAX_TILES`` tiles (at once,
        waiting for nothing), and OcrBusy when no reader is free within ``wait``, or too many reads wait already."""
        self._mend()
        if not self.ready:
            raise OcrNotReady("the text recognition models are still loading")
        plan = tiles.tiling(image.width, image.height, OnnxOcr.MAX_SIDE)
        if len(plan.regions) > MAX_TILES:
            raise OcrTooManyTiles(f"the image would be read in {len(plan.regions)} tiles, more than {MAX_TILES}")
        if not self._turns.acquire(wait):
            raise OcrBusy("the text recognition was busy with other images")
        try:
            reader = self._take()
        except BaseException:
            self._turns.release()
            raise
        try:
            # A second at most for its own turn: its loader may hold it for the instant it takes to say it loaded.
            return reader.read(image, wait=1.0, budget=budget)
        finally:
            with self._taking:
                self._free.append(reader)
            self._turns.release()

    def _take(self) -> OnnxOcr:
        """A free reader that has loaded: the turns let no more reads in than there are."""
        with self._taking:
            loaded = [reader for reader in self._free if reader.ready]
            if not loaded:
                raise OcrBusy("the text recognition was busy with other images")
            reader = loaded[-1] if self._last_first else loaded[0]
            self._free.remove(reader)
            return reader

    def _loaded(self) -> None:
        """A reader's load is over (loaded, or failed): as many reads at once as readers have loaded."""
        with self._taking:
            self._turns.allow(sum(reader.ready for reader in self.readers))

    def _due(self) -> bool:
        return self._make is not None and self._clock() - self._mended >= MEND_SECONDS

    def _mend(self) -> None:
        """Each reader whose load failed made again in its place, when one is due (``MEND_SECONDS`` since the pool's
        readers were made, or made again), and started: meanwhile the others read."""
        if self._make is None:
            return
        made: list[OnnxOcr] = []
        with self._taking:
            failed = [n for n, reader in enumerate(self.readers) if reader.error is not None]
            if not failed or not self._due():
                return
            self._mended = self._clock()
            for n in failed:
                old, new = self.readers[n], self._make()
                new.loaded = self._loaded
                self.readers[n] = new
                if old in self._free:  # never taken: it never loaded
                    self._free[self._free.index(old)] = new
                made.append(new)
        for reader in made:
            reader.start_loading()
        self._loaded()  # one that loaded already counts at once


_POOLS: dict[tuple[Path, Path, int], OcrPool] = {}
_POOLS_LOCK = threading.Lock()


def shared_pool(detector: Path, recognizer: Path, readers: int) -> OcrPool:
    """One pool of ``readers`` text recognitions per pair of model folders and process (a server's spaces read through
    it: ``SpaceReads``), the processors this instance may use (``usable_cpus``) shared among them, four threads each at
    most and one at least; each reads batches of ``SPACE_BATCH_COLUMNS`` columns at most, with ONNX Runtime's arena as
    ``SPACE_ARENA`` says and threads that never spin. Every space gets the same pool, which makes a reader whose load
    failed again in its place (``OcrPool``)."""
    with _POOLS_LOCK:
        key = (detector, recognizer, readers)
        pool = _POOLS.get(key)
        if pool is None:
            threads = max(1, min(4, usable_cpus() // readers))
            make = functools.partial(
                OnnxOcr, detector, recognizer, threads,
                batch_columns=SPACE_BATCH_COLUMNS, arena=SPACE_ARENA, spinning=False,
            )  # fmt: skip
            pool = _POOLS[key] = OcrPool([make() for _ in range(readers)], make=make)
        return pool


class SpaceReads:
    """One space's reads of the text recognitions every space of a server shares (``shared_pool``): one read of this
    identity at a time, so that its burst of images waits behind itself and never ahead of another identity's; each
    read waits ``wait`` seconds at most, for its identity's other read and for a free reader together, and reads
    ``budget`` seconds at most. A read past either is one whose text could not be read, which withholds the image or
    stops the request that only carries it: never sends it unread."""

    def __init__(
        self, ocr: OcrPool | OnnxOcr, wait: float = SPACE_WAIT_SECONDS, budget: float = SPACE_READ_SECONDS
    ) -> None:
        self.ocr, self.wait, self.budget = ocr, wait, budget
        self._mine = threading.Lock()  # this identity's read under way

    @property
    def ready(self) -> bool:
        return self.ocr.ready

    @property
    def error(self) -> BaseException | None:
        return self.ocr.error

    def start_loading(self) -> None:
        self.ocr.start_loading()

    def wait_ready(self, timeout: float = 120.0) -> bool:
        return self.ocr.wait_ready(timeout)

    def read(self, image: Image.Image) -> OcrResult:
        started = time.monotonic()
        if not self._mine.acquire(timeout=self.wait):
            raise OcrBusy("this identity's other images were being read for longer than a read may wait")
        try:
            left = max(0.0, self.wait - (time.monotonic() - started))
            return self.ocr.read(image, wait=left, budget=self.budget)
        finally:
            self._mine.release()
