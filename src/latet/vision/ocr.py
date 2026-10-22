"""Reading the text in an image with the local models (``latet[ocr]``).

The detector finds the text lines, the recognizer reads each one and tells where every character is:
the recognizer's output frames map back to x positions in the image, so a stretch of the text can be
covered exactly. Everything runs with ONNX Runtime on this machine. A tall, a wide or a large image (a
full page's screenshot, a 4K or 5K screen's) is read in tiles, so that its text keeps a size the
detector reads (``tiles``).

Loading takes about a second (more with an antivirus scanning the runtime), so it happens on a thread
of its own, started with the daemon.

A server's bounded pool of readers and per-identity turns live in ``latet.vision.pool``.
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import logging
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import yaml

from latet.logline import described
from latet.vision import db, tiles
from latet.vision.layout import OcrLine, OcrResult, join

if TYPE_CHECKING:
    from PIL import Image

log = logging.getLogger(__name__)

MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)

# What the recognizer reads at once is bounded whatever an image's shape: one line at its input height is at most this
# wide (a full line of a 5K screen's text is about 16,000; a line longer for its height is not read, and its image is
# withheld: unread, it could hold a value), and the lines of one batch are at most this wide together.
MAX_LINE_WIDTH = 16_384
BATCH_COLUMNS = 65_536
# What the detector reads of one image is bounded too: each tile costs what one image of up to 2048 pixels a side costs
# (and holds as much while it is read, one tile at a time), and an image is read in this many tiles at most: a page up
# to 28,928 pixels long, at its own size, so every image at least 553 pixels wide within a server's 16 megapixels, and
# 1,383 within a computer's 40 (an image read in a grid takes 10 at most). One longer for its width is not read, and its
# image is withheld: unread, it could hold a value.
MAX_TILES = 16


class OcrError(Exception):
    pass


class OcrNotReady(OcrError):
    pass


class OcrBusy(OcrError):
    """The text recognition was busy with other images for longer than a read may wait: not read now."""


class OcrTooLong(OcrError):
    """Reading the image took longer than a read may take: its text was not read."""


class OcrTooWide(OcrError):
    """A line of text in the image is longer for its height than the recognizer reads (``MAX_LINE_WIDTH``)."""


class OcrTooManyTiles(OcrError):
    """The image is so long for its width, or so wide for its height, that the detector would read it in more tiles than
    one image may take (``MAX_TILES``)."""


class _Turns:
    """A lock ``holders`` may hold at once (one by default), taken in the order it is asked for, waited for a bounded
    time when one is given: the reads of the text recognitions that several spaces share take turns, so that a read
    never passes one that waits before it (``threading.Lock`` wakes any waiter), and one that cannot have its turn in
    time gives up. ``waiting``: how many may wait at once at most (None: no bound); one more is refused at once."""

    def __init__(self, holders: int = 1, *, waiting: int | None = None) -> None:
        self._changed = threading.Condition()
        self._queue: deque[object] = deque()
        self._held = 0
        self._holders = holders
        self._waiting = waiting

    def allow(self, holders: int) -> None:
        """``holders`` may hold it at once from now on (a pool's readers that have loaded)."""
        with self._changed:
            self._holders = holders
            self._changed.notify_all()

    def acquire(self, timeout: float | None = None) -> bool:
        if timeout is not None and not math.isfinite(timeout):  # a wait of NaN would never end, and spin a core
            raise ValueError("a turn is waited for a finite number of seconds, or for as long as it takes")
        me = object()
        with self._changed:
            if self._waiting is not None and len(self._queue) >= self._waiting:
                return False
            self._queue.append(me)
            taken = False
            try:
                taken = self._changed.wait_for(lambda: self._held < self._holders and self._queue[0] is me, timeout)
            finally:
                self._queue.remove(me)
                if taken:
                    self._held += 1
                # The one after it may be first now, and find a turn still free (or the one it gave up).
                self._changed.notify_all()
            return taken

    def release(self) -> None:
        with self._changed:
            self._held -= 1
            self._changed.notify_all()

    def __enter__(self) -> None:
        self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass(order=True)
class _Deadline:
    """One read's deadline (on ``time.monotonic``), and the options its runs go with."""

    at: float
    order: int
    options: Any = field(compare=False)
    done: bool = field(default=False, compare=False)  # the read is over: nothing to end
    fired: bool = field(default=False, compare=False)  # its runs were ended at the deadline


class _Deadlines:
    """The reads with a budget under way, each ended once its budget is spent: one thread for them all
    (``latet-ocr-deadline``, started with the first), which sets the ``terminate`` of the read's own run options (ONNX
    Runtime then stops the run within a tenth of a second, with an error the read takes for ``OcrTooLong``); never a
    thread per read."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._changed = threading.Condition()
        self._heap: list[_Deadline] = []
        self._order = itertools.count()
        self._thread: threading.Thread | None = None

    @contextlib.contextmanager
    def ending(self, at: float, options: Any) -> Iterator[_Deadline]:
        """While it lasts, the runs with ``options`` are ended at ``at``."""
        deadline = _Deadline(at, next(self._order), options)
        with self._changed:
            heapq.heappush(self._heap, deadline)
            if self._thread is None:
                self._thread = threading.Thread(target=self._watch, name="latet-ocr-deadline", daemon=True)
                self._thread.start()
            self._changed.notify_all()
        try:
            yield deadline
        finally:
            with self._changed:
                deadline.done = True
                self._changed.notify_all()

    def _watch(self) -> None:
        while True:
            with self._changed:
                while self._heap and self._heap[0].done:
                    heapq.heappop(self._heap)
                if not self._heap:
                    self._changed.wait()
                    continue
                first = self._heap[0]
                left = first.at - self._clock()
                if left > 0:
                    self._changed.wait(left)
                    continue
                heapq.heappop(self._heap)
                first.fired = True
                first.options.terminate = True


_DEADLINES = _Deadlines()


class Ocr(Protocol):
    @property
    def ready(self) -> bool: ...

    @property
    def error(self) -> BaseException | None: ...

    def start_loading(self) -> None: ...

    def read(self, image: Image.Image) -> OcrResult: ...


def decode(probs: np.ndarray, chars: list[str], box: tuple[int, int, int, int], width: int, resized: int) -> OcrLine:
    """A recognizer's output for one line (frames by classes): its text, and where each character is.

    Greedy CTC: the best class of each frame, repeats and blanks dropped. A character sits at the middle
    of the frames that gave it; the edge between two characters is halfway between their middles.
    ``width`` is the padded input width, ``resized`` the line's own width in it.
    """
    best = probs.argmax(-1).tolist()
    conf = probs.max(-1)
    codes: list[int] = []
    runs: list[list[int]] = []
    scores: list[float] = []
    previous = 0
    for t, c in enumerate(best):
        if c != 0 and c != previous:
            codes.append(c)
            runs.append([t, t])
            scores.append(float(conf[t]))
        elif c != 0:
            runs[-1][1] = t
        previous = c
    x0, _, x1, _ = box
    per_frame = width / probs.shape[0] * (x1 - x0) / resized  # image pixels per output frame
    middles = [x0 + (a + b + 1) / 2 * per_frame for a, b in runs]
    text = "".join(chars[c] if c < len(chars) else "" for c in codes)
    if not middles:
        return OcrLine(box, "", (), 0.0)
    if len(middles) == 1:
        edges = [float(x0), float(x1)]
    else:
        mids = [(a + b) / 2 for a, b in pairwise(middles)]
        edges = [max(float(x0), 2 * middles[0] - mids[0]), *mids, min(float(x1), 2 * middles[-1] - mids[-1])]
    while text.startswith(" "):
        text, edges, scores = text[1:], edges[1:], scores[1:]
    while text.endswith(" "):
        text, edges, scores = text[:-1], edges[:-1], scores[:-1]
    return OcrLine(box, text, tuple(edges), float(np.mean(scores)) if scores else 0.0)


class OnnxOcr:
    MAX_SIDE = 2048  # the detector reads the image scaled down to this long side, at most
    HEIGHT = 48  # the recognizer's input height
    MIN_WIDTH = 320  # and its narrowest input, as PaddleOCR pads it
    GROUP = 1.3  # one batch holds lines whose widths differ at most by this factor
    BATCH = 8

    def __init__(
        self,
        detector: Path,
        recognizer: Path,
        threads: int = 0,
        *,
        batch_columns: int = BATCH_COLUMNS,
        arena: bool = True,
        spinning: bool = True,
    ) -> None:
        self.folders = (detector, recognizer)
        self.threads = threads or min(4, os.cpu_count() or 1)
        # How wide the recognizer's batches are at most (a server's readers: pool.SPACE_BATCH_COLUMNS); whether ONNX
        # Runtime keeps what a read took for the next ones (its memory arena) and lets its threads spin between runs,
        # holding cores another reader of the process needs (a server's readers: neither).
        self.batch_columns, self.arena, self.spinning = batch_columns, arena, spinning
        self._lock = _Turns()  # one image at a time, in turn
        self._starting = threading.Lock()
        self._loader: threading.Thread | None = None
        self._det: Any = None
        self._rec: Any = None
        self._run_options: Any = None
        self._chars: list[str] = []
        self._post: tuple[float, float, float] = (0.3, 0.6, 1.5)
        self.error: BaseException | None = None
        self.loaded: Callable[[], None] | None = None  # told once a load is over, loaded or failed (a pool's readers)

    @property
    def ready(self) -> bool:
        return self._rec is not None

    def start_loading(self) -> None:
        if self.ready or self.error is not None:
            return
        with self._starting:
            if self._loader is None:
                self._loader = threading.Thread(target=self._load_guarded, name="latet-ocr-load", daemon=True)
                self._loader.start()

    def wait_ready(self, timeout: float = 120.0) -> bool:
        self.start_loading()
        loader = self._loader
        if loader is not None:
            loader.join(timeout)
        return self.ready

    def _load_guarded(self) -> None:
        started = time.monotonic()
        try:
            with self._lock:
                if not self.ready:
                    self._load()
            log.info("text recognition loaded in %.1f s", time.monotonic() - started)
        except Exception as exc:  # a broken install must not take the daemon down
            self.error = exc
            log.error("text recognition could not load: %s", described(exc))
        finally:
            if self.loaded is not None:
                self.loaded()

    def _load(self) -> None:
        import onnxruntime as ort

        detector, recognizer = self.folders
        det_settings = yaml.safe_load((detector / "inference.yml").read_text("utf-8"))["PostProcess"]
        rec_settings = yaml.safe_load((recognizer / "inference.yml").read_text("utf-8"))["PostProcess"]
        self._post = (det_settings["thresh"], det_settings["box_thresh"], det_settings["unclip_ratio"])
        options = ort.SessionOptions()
        options.log_severity_level = 3
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL  # the same answer every time
        if not self.arena:
            options.enable_cpu_mem_arena = False
        if not self.spinning:
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        providers = ["CPUExecutionProvider"]
        det = ort.InferenceSession(str(detector / "inference.onnx"), options, providers=providers)
        rec = ort.InferenceSession(str(recognizer / "inference.onnx"), options, providers=providers)
        self._run_options = self._options()
        self._chars = ["", *rec_settings["character_dict"], " "]
        self._det = det
        self._rec = rec

    def _options(self) -> Any:
        """Options for runs: the arena gives back what a run took, when there is one (ONNX Runtime refuses the option
        without it)."""
        import onnxruntime as ort

        run = ort.RunOptions()
        if self.arena:
            run.add_run_config_entry("memory.enable_memory_arena_shrinkage", "cpu:0")  # give memory back
        return run

    # --- reading ---------------------------------------------------------------------------------

    def read(self, image: Image.Image, *, wait: float | None = None, budget: float | None = None) -> OcrResult:
        """The text in ``image``, one image at a time, in the order the reads asked. ``wait``: how long it waits for its
        turn at most, ``budget``: how long it reads at most once it has it, its runs ended there; past either it raises
        (``OcrBusy``, ``OcrTooLong``), and the image is one whose text could not be read. None: as long as it takes (a
        computer's own reads). An image that would take more than ``MAX_TILES`` tiles is refused at once
        (``OcrTooManyTiles``), its text unread, without waiting for the turn."""
        if not self.ready:
            raise OcrNotReady("the text recognition models are still loading")
        if budget is not None and not math.isfinite(budget):  # NaN would give no deadline at all
            raise ValueError("a read's budget is a finite number of seconds")
        plan = tiles.tiling(image.width, image.height, self.MAX_SIDE)
        if len(plan.regions) > MAX_TILES:
            raise OcrTooManyTiles(f"the image would be read in {len(plan.regions)} tiles, more than {MAX_TILES}")
        rgb = np.asarray(image.convert("RGB"))
        if not self._lock.acquire(wait):
            raise OcrBusy("the text recognition was busy with other images")
        try:
            if budget is None:
                lines = self._recognise(rgb, self._detect(rgb, plan))
            else:
                lines = self._bounded(rgb, plan, time.monotonic() + budget)
        finally:
            self._lock.release()
        return join(lines, image.width, image.height)

    def _bounded(self, rgb: np.ndarray, plan: tiles.Tiling, deadline: float) -> list[OcrLine]:
        """The lines of ``rgb`` read by ``deadline`` (on ``time.monotonic``), or OcrTooLong: the read's runs go with
        options of its own, which the deadlines' thread ends there (``_Deadlines``), and between two runs the deadline
        is checked."""
        options = self._options()
        with _DEADLINES.ending(deadline, options) as ending:
            try:
                return self._recognise(rgb, self._detect(rgb, plan, deadline, options), deadline, options)
            except OcrError:
                raise
            except Exception:
                if ending.fired:  # ONNX Runtime's own error for a run ended at the deadline
                    raise OcrTooLong("reading the image took longer than a read may take") from None
                raise

    def _detect(
        self,
        rgb: np.ndarray,
        plan: tiles.Tiling | None = None,
        deadline: float | None = None,
        options: Any = None,
    ) -> list[tuple[int, int, int, int]]:
        """The boxes of the lines of text in ``rgb``, in its pixels: the image read whole, or in the tiles of ``plan``
        (``tiles.tiling``), one after the other; past ``deadline`` (on ``time.monotonic``) no other tile is read, nor
        the one whose detection ran past it post-processed."""
        height, width = rgb.shape[:2]
        plan = plan or tiles.tiling(width, height, self.MAX_SIDE)
        found = []
        for x0, y0, x1, y1 in plan.regions:
            if deadline is not None and time.monotonic() >= deadline:
                raise OcrTooLong("reading the image took longer than a read may take")
            boxes = self._boxes(rgb[y0:y1, x0:x1], deadline, options)
            found.append([(a + x0, b + y0, c + x0, d + y0) for a, b, c, d in boxes])
        if len(found) == 1:  # read whole, as an image always was before tiles
            return found[0]
        return db.merge_rows(tiles.stitch(plan, found))

    def _boxes(
        self, rgb: np.ndarray, deadline: float | None = None, options: Any = None
    ) -> list[tuple[int, int, int, int]]:
        """The boxes the detector finds in ``rgb``, read with its long side scaled down to ``MAX_SIDE`` at most, the
        boxes of one line joined."""
        from PIL import Image

        height, width = rgb.shape[:2]
        scale = min(1.0, self.MAX_SIDE / max(height, width))
        nh = max(32, round(height * scale / 32) * 32)
        nw = max(32, round(width * scale / 32) * 32)
        img = (
            rgb
            if (nh, nw) == (height, width)
            else np.asarray(Image.fromarray(rgb).resize((nw, nh), Image.Resampling.BILINEAR))
        )
        x = (img[..., ::-1].astype(np.float32) / 255 - MEAN) / STD  # the model reads BGR
        run = options if options is not None else self._run_options
        prob = self._det.run(None, {"x": x.transpose(2, 0, 1)[None]}, run)[0][0, 0]
        if deadline is not None and time.monotonic() >= deadline:  # between the detection and its post-processing
            raise OcrTooLong("reading the image took longer than a read may take")
        sx, sy = nw / width, nh / height
        found = []
        for x0, y0, x1, y1 in db.boxes(prob, *self._post):
            box = (
                max(0, math.floor(x0 / sx)),
                max(0, math.floor(y0 / sy)),
                min(width, math.ceil(x1 / sx)),
                min(height, math.ceil(y1 / sy)),
            )
            if box[2] - box[0] >= 2 and box[3] - box[1] >= 2:
                found.append(box)
        return db.merge_rows(found)

    def _recognise(
        self,
        rgb: np.ndarray,
        boxes: list[tuple[int, int, int, int]],
        deadline: float | None = None,
        options: Any = None,
    ) -> list[OcrLine]:
        """Each line read, a batch at a time; past ``deadline`` (on ``time.monotonic``) no other batch is read. Raises
        OcrTooWide, before anything is read, when a line is longer for its height than the recognizer reads."""
        from PIL import Image

        ratios = [(b[2] - b[0]) / (b[3] - b[1]) for b in boxes]
        if any(self._width(ratio) > MAX_LINE_WIDTH for ratio in ratios):
            raise OcrTooWide("a line of text was too long for its height to be read")
        batches: list[list[int]] = []
        for i in sorted(range(len(boxes)), key=lambda i: ratios[i]):
            last = batches[-1] if batches else None
            # Sorted by shape, a line widens its batch to its own width: as many as the columns of a batch allow.
            if (
                last is not None
                and len(last) < self.BATCH
                and ratios[i] <= self.GROUP * ratios[last[0]]
                and (len(last) + 1) * self._width(ratios[i]) <= self.batch_columns
            ):
                last.append(i)
            else:
                batches.append([i])
        run = options if options is not None else self._run_options
        lines: list[OcrLine | None] = [None] * len(boxes)
        for batch in batches:
            if deadline is not None and time.monotonic() >= deadline:
                raise OcrTooLong("reading the image took longer than a read may take")
            width = self._width(max(ratios[i] for i in batch))
            x = np.zeros((len(batch), 3, self.HEIGHT, width), np.float32)
            resized = []
            for n, i in enumerate(batch):
                x0, y0, x1, y1 = boxes[i]
                rw = min(width, math.ceil(self.HEIGHT * ratios[i]))
                crop = Image.fromarray(rgb[y0:y1, x0:x1]).resize((rw, self.HEIGHT), Image.Resampling.BILINEAR)
                x[n, :, :, :rw] = ((np.asarray(crop, np.float32)[..., ::-1] / 255 - 0.5) / 0.5).transpose(2, 0, 1)
                resized.append(rw)
            probs = self._rec.run(None, {"x": x}, run)[0]
            for n, i in enumerate(batch):
                lines[i] = decode(probs[n], self._chars, boxes[i], width, resized[n])
        return [line for line in lines if line is not None and line.text]

    def _width(self, ratio: float) -> int:
        """The recognizer's input width for a line of this shape (its width over its height), in steps of 8."""
        width = int(self.HEIGHT * max(self.MIN_WIDTH / self.HEIGHT, ratio))
        return -(-width // 8) * 8


_SHARED: dict[tuple[Path, Path], OnnxOcr] = {}
_SHARED_LOCK = threading.Lock()


def shared_ocr(detector: Path, recognizer: Path) -> OnnxOcr:
    """One text recognition per pair of model folders and process: every runtime of a computer's process shares it,
    with no bound on its reads."""
    with _SHARED_LOCK:
        ocr = _SHARED.get((detector, recognizer))
        if ocr is None or ocr.error is not None:  # a load that failed is tried again
            ocr = _SHARED[(detector, recognizer)] = OnnxOcr(detector, recognizer)
        return ocr
