"""The images of a request, read and masked.

Each image's text becomes a slot of its own (``SlotKind.IMAGE_TEXT``), masked like any other text: the
detectors, the vault, the local reviewer and the Live Reviewer see it, and the dashboard records it.
When that text is written back masked, the image is painted where the values were read: the fake
over each one, a bar for what is redacted. An image that cannot be read is withheld, never sent as it
was, once Latet reads images at all.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from latet.engine.pipeline import LRU
from latet.engine.types import Policy, Replacement, SlotKind
from latet.logline import Paced, described
from latet.protocols.base import ImageRef, SlotRef
from latet.vision.codec import MAX_PIXELS, MAX_SCANS, Decoded, ImageError, decode, encode
from latet.vision.layout import Cover, OcrResult, reference_for

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

    from latet.vision.ocr import Ocr

log = logging.getLogger(__name__)

MASKED, CLEAN, WITHHELD, UNREAD = "masked", "clean", "withheld", "unread"
YOU_WITHHELD = "you withheld it in the Live Reviewer"
# Why an image is withheld when the text recognition a server's spaces share could not read it in time: not kept, so
# that the image is read when it comes again.
BUSY = "its text could not be read: the text recognition was busy with other images"
# Why an image is one a server could not read when its text recognition is not available (its models could not load):
# the image is withheld, or the request that only carries it stopped, until the recognition mends.
NOT_AVAILABLE = "the text recognition is not available"
REVIEW_SIDE = 1536  # the longest side of an image shown to the local reviewer
MAX_SENT = 5 * 1024 * 1024  # the most a provider takes for one image (Anthropic's limit, the lowest)
# On a server, how long an image whose text could not be read because of the moment (a read past its budget, memory, an
# error of ONNX Runtime's) stays so when it comes again: after it, it is read again. What its read says whatever the
# moment (its text, an image too long or with a line too wide to read) is kept for good, as on a computer.
KEPT_SECONDS = 120.0
# ONNX Runtime's own errors (a run that fails, a run ended at its deadline), by their module: a computer without the ocr
# extra imports nothing to know them.
_ONNXRUNTIME = "onnxruntime.capi.onnxruntime_pybind11_state"


OcrUnreadReason = Literal["busy", "too_long", "too_many_tiles", "too_wide", "unavailable"]


@dataclass(frozen=True)
class _UnreadOcr:
    """A terminal OCR outcome, retaining its typed reason beside safe cache/traffic words."""

    why: str
    reason: OcrUnreadReason
    retry: bool = False  # busy/loading: ask again on the next occurrence instead of caching


class Decisions(Protocol):
    """Where your decisions on images are kept (sealed, by a keyed hash of the image)."""

    def load_image_decision(self, key: str) -> dict[str, Any] | None: ...

    def save_image_decision(self, key: str, decision: dict[str, Any]) -> None: ...


def image_key(data: str) -> str:
    """The key of an image's bytes (its base64, as the request carries it)."""
    return hashlib.sha256(data.encode("ascii", "replace")).hexdigest()


def split_fake(fake: str, widths: Sequence[float]) -> list[str]:
    """A fake cut into one piece for each line it covers, in proportion to their widths, at spaces
    when there is one near the cut. Every character goes somewhere."""
    if len(widths) <= 1:
        return [fake]
    total = sum(widths) or 1.0
    pieces: list[str] = []
    rest = fake
    left = total
    for width in widths[:-1]:
        cut = round(len(rest) * width / left) if left else 0
        spaces = [i for i, c in enumerate(rest) if c == " " and 0 < i < len(rest)]
        if spaces:
            near = min(spaces, key=lambda i: abs(i - cut))
            if abs(near - cut) <= max(2, len(rest) // 4):
                cut = near
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
        left -= width
    pieces.append(rest)
    return pieces


def covers(result: OcrResult, replacements: Sequence[Replacement]) -> list[Cover]:
    """What to paint for the values masked in an image's text (offsets in the text as read)."""
    out: list[Cover] = []
    for r in replacements:
        pieces = result.cover(r.start, r.end)
        if not pieces:
            continue
        if r.policy == Policy.REDACT:
            texts: list[str | None] = [None] * len(pieces)
        else:
            texts = list(split_fake(r.replacement, [rect[2] - rect[0] for _, rect in pieces]))
        for (i, rect), text in zip(pieces, texts, strict=True):
            line = result.lines[i]
            box = (float(line.box[0]), float(line.box[1]), float(line.box[2]), float(line.box[3]))
            start = line.edges[max(r.start - result.starts[i], 0)]
            out.append(Cover(rect, box, text or None, reference_for(line.text), start))
    return out


def sized(image: PILImage, media_type: str, limit: int) -> tuple[str, str]:
    """The painted image, encoded no larger than ``limit`` bytes: in its own format, else as a JPEG.
    Larger still, it cannot be sent (``ImageError``)."""
    data, media = encode(image, media_type)
    if len(data) * 3 // 4 > limit and media != "image/jpeg":
        data, media = encode(image, "image/jpeg")
    if len(data) * 3 // 4 > limit:
        raise ImageError("masked, it was larger than providers take")
    return data, media


def review_covers(result: OcrResult, replacements: Sequence[Replacement]) -> list[Cover]:
    """What to paint on the image the local reviewer is shown: a label for each value masked, numbered
    as the placeholders of the text it reads (``[EMAIL#1]`` for ``⟦EMAIL#1⟧``), a bar for what is redacted."""
    labels: dict[str, str] = {}
    counts: Counter[str] = Counter()
    out: list[Cover] = []
    for r in sorted(replacements, key=lambda r: r.start):
        label = None
        if r.policy != Policy.REDACT:
            if r.replacement not in labels:
                counts[r.etype] += 1
                labels[r.replacement] = f"[{r.etype}#{counts[r.etype]}]"
            label = labels[r.replacement]
        for i, rect in result.cover(r.start, r.end):
            line = result.lines[i]
            box = (float(line.box[0]), float(line.box[1]), float(line.box[2]), float(line.box[3]))
            out.append(Cover(rect, box, label, reference_for(line.text), rect[0]))
    return out


def placeholder(width: int, height: int) -> tuple[str, str]:
    """An image of the same size saying it was withheld: for a place that must hold an image."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (max(1, width), max(1, height)), (128, 128, 128))
    size = max(10, min(width, height) // 12)
    ImageDraw.Draw(image).text((size, size), "Image withheld by Latet", font=ImageFont.load_default(size), fill="white")
    return encode(image, "image/png")


class ImageWork:
    """One image of a request: the container of its text's slot, painted when that text is masked."""

    def __init__(
        self,
        service: ImageMasking,
        ref: ImageRef,
        key: str,
        decoded: Decoded | None = None,
        result: OcrResult | None = None,
        state: str = CLEAN,
        reason: str | None = None,
    ) -> None:
        self.service = service
        self.ref = ref
        self.key = key
        self.decoded = decoded
        self.result = result
        self.state = state
        self.reason = reason
        self.original = (ref.data(), ref.media_type)
        self.text = result.text if result is not None else ""
        self.replacements: list[Replacement] = []
        self.rects: list[tuple[int, int, int, int]] = []  # drawn by you: covered with a bar
        self.slot: SlotRef | None = None
        if result is not None and result.text and state == CLEAN:
            self.slot = SlotRef(self, "text", SlotKind.IMAGE_TEXT, tool=ref.tool)

    @property
    def path_key(self) -> tuple[int, str]:
        """Where the image's data sits in the request (for the dashboard's paths)."""
        return id(self.ref.holder), self.ref.key

    @property
    def held_bytes(self) -> int:
        """What this image keeps in memory, in bytes: its base64 as it came and what the request carries now, once
        while they are one string (an image that leaves as it came), and its pixels once read, kept to paint it again,
        four bytes each as Pillow keeps an RGB or RGBA image. A server's space weighs it for each batch of the Live
        Reviewer it holds (``spaces.space_size``)."""
        came, carried = self.original[0], self.ref.holder.get(self.ref.key)
        size = sys.getsizeof(came) + (sys.getsizeof(carried) if isinstance(carried, str) and carried is not came else 0)
        if self.decoded is not None:
            size += self.decoded.image.width * self.decoded.image.height * 4
        return size

    def __getitem__(self, key: str) -> str:
        return self.text

    def __setitem__(self, key: str, value: str) -> None:
        if value != self.text:  # written without saying where the values are: nothing to paint by
            self.withhold("Latet could not tell where the values in it are")

    def masked(self, key: str, text: str, replacements: Sequence[Replacement]) -> None:
        if self.state == WITHHELD:
            return
        self.text = text
        self.replacements = list(replacements)
        if (not self.replacements and not self.rects) or self.result is None or self.decoded is None:
            self.ref.set(*self.original)
            self.state = CLEAN
            return
        try:
            data, media = self.service.paint(self, self.replacements)
        except ImageError as exc:
            self.withhold(str(exc))
            return
        except Exception as exc:  # a painting that fails must not let the image go as it was
            log.error("could not paint an image: %s", type(exc).__name__)  # its message could quote the image's text
            self.withhold("Latet could not mask it")
            return
        self.ref.set(data, media)
        self.state = MASKED

    def decide(self, rects: list[tuple[int, int, int, int]], withhold: bool, reason: str = YOU_WITHHELD) -> None:
        """Your decision in the Live Reviewer (or the local reviewer's, withholding it for ``reason``):
        applied now, and kept for this image sent again."""
        self.service.remember(self.key, rects, withhold, reason if withhold else None)
        if withhold:
            self.withhold(reason)
            return
        self.rects = rects
        self.masked("text", self.text, self.replacements)

    def view(self, replacements: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """The image as the Live Reviewer shows it: its size, where each line and each masked value is
        (offsets in the original text, as ``replacements`` gives them), and your rectangles."""
        assert self.result is not None
        lines = [
            {"box": list(line.box), "start": at, "end": at + len(line.text)}
            for line, at in zip(self.result.lines, self.result.starts, strict=True)
        ]
        covered = [
            {"rect": [round(v) for v in rect], "start": r["start"], "end": r["end"], "type": r.get("type")}
            for r in replacements
            for _, rect in self.result.cover(int(r["start"]), int(r["end"]))
        ]
        return {
            "width": self.result.width,
            "height": self.result.height,
            "media_type": self.original[1],
            "lines": lines,
            "covered": covered,
            "rects": [list(r) for r in self.rects],
            "withheld": self.state == WITHHELD,
        }

    def withhold(self, reason: str) -> None:
        """Put a notice in place of the image, or, where an image must stay, a grey one saying so."""
        self.state, self.reason = WITHHELD, reason
        if not self.ref.withhold(f"[image withheld by Latet: {reason}]"):
            width, height = self.decoded.image.size if self.decoded is not None else (640, 360)
            data, media = placeholder(width, height)
            self.ref.set(data, media)
            self.service.known.put(image_key(data), WITHHELD)  # the placeholder, never the original


class ImageMasking:
    """Reads the images of requests and paints them; one image at a time, results kept by their bytes. ``max_pixels``:
    the largest image read (a server's spaces read smaller ones than a computer).

    ``space``: an identity's space on a server, whose reads go through its server's readers one at a time for the
    identity, each bounded (``SpaceReads``), so it takes no lock of its own around them; what it could not read because
    of the moment is kept ``KEPT_SECONDS`` at most (on ``clock``), a JPEG of more than ``MAX_SCANS`` scans is not
    decoded, ONNX Runtime's own errors are images not read, and the lines it says of images not read are said once a
    minute: anyone can send such images at will."""

    BUDGET_S = 20.0  # how long a request waits for the models to load

    def __init__(
        self,
        ocr: Ocr,
        decisions: Decisions | None = None,
        *,
        max_pixels: int = MAX_PIXELS,
        space: bool = False,
        clock: Callable[[], float] = time.monotonic,
        ocr_unread: Callable[[OcrUnreadReason], None] | None = None,
    ) -> None:
        self.ocr = ocr
        self.decisions = decisions
        self.max_pixels = max_pixels
        self.space = space
        self._clock = clock
        self._ocr_unread = ocr_unread if space else None
        self._report_failed = Paced(clock=clock)
        self._decided: LRU[str, dict[str, Any]] = LRU(1024)
        # An image's text, or why it could not be read, and until when it holds (None: whatever the moment).
        self._results: LRU[str, tuple[OcrResult | _UnreadOcr, float | None]] = LRU(128)
        self._painted: LRU[tuple[str, str], tuple[str, str]] = LRU(64)
        # Images Latet wrote (masked, or the placeholder) or found clean: sent again, they are not read again.
        self.known: LRU[str, str] = LRU(4096)
        self._lock = threading.Lock()
        self._paced: dict[tuple[str, str], Paced] = {}  # on a server, each line for images not read, once a minute
        self._pacing = threading.Lock()

    def start(self) -> None:
        self.ocr.start_loading()

    @property
    def available(self) -> bool:
        return self.ocr.error is None

    def works(self, images: Sequence[ImageRef], modify: bool = True) -> list[ImageWork]:
        return [self.work(ref, modify) for ref in images]

    def work(self, ref: ImageRef, modify: bool = True) -> ImageWork:
        """An image read; one that cannot be read is withheld (``modify``), or left unread."""
        data = ref.data()
        key = image_key(data)
        if self.known.get(key) in (MASKED, WITHHELD):  # Latet's own image, sent again
            return ImageWork(self, ref, key, state=MASKED)
        decoded: Decoded | None = None
        ocr_reason: OcrUnreadReason | None = None
        try:
            decoded = decode(data, self.max_pixels, max_scans=MAX_SCANS if self.space else None)
        except ImageError as exc:
            why = f"it could not be read ({exc})"
        except MemoryError:  # withheld, as one that cannot be read
            self._say(logging.ERROR, "could not decode an image: %s", MemoryError.__name__)
            why = "it could not be read (MemoryError)"
        else:
            found = self._kept(key)
            if found is None:
                found, lasting = self._read(decoded)
                if not isinstance(found, _UnreadOcr) or not found.retry:
                    self._results.put(key, (found, None if lasting else self._clock() + KEPT_SECONDS))
            if not isinstance(found, _UnreadOcr):
                return self._as_decided(ImageWork(self, ref, key, decoded, found), modify)
            why, ocr_reason = found.why, found.reason
        return self._unread(ref, key, decoded, why, modify, ocr_reason)

    def _unread(
        self,
        ref: ImageRef,
        key: str,
        decoded: Decoded | None,
        why: str,
        modify: bool,
        reason: OcrUnreadReason | None,
    ) -> ImageWork:
        """One terminal outcome per occurrence, before checked/rewritten presentation; never at final check/withhold."""
        work = ImageWork(self, ref, key, decoded, state=UNREAD, reason=why)
        if self.space and reason is not None and self._ocr_unread is not None:
            try:
                self._ocr_unread(reason)
            except Exception as exc:  # reporting cannot change traffic; process interruption still propagates
                if count := self._report_failed.due():
                    log.warning(
                        "could not report an unread OCR occurrence: %s%s",
                        described(exc),
                        f" ({count} times since the line before)" if count > 1 else "",
                    )
        if modify:
            work.withhold(why)
        return work

    def unread(self, ref: ImageRef, why: str, modify: bool = True) -> ImageWork:
        """An image whose text could not be read for ``why``, nothing asked of the text recognition (a server's, not
        available): withheld (``modify``), or left unread; one Latet wrote itself, sent again, as ``work`` has it."""
        key = image_key(ref.data())
        if self.known.get(key) in (MASKED, WITHHELD):
            return ImageWork(self, ref, key, state=MASKED)
        return self._unread(ref, key, None, why, modify, "unavailable")

    def _kept(self, key: str) -> OcrResult | _UnreadOcr | None:
        """What an image's read said before, while it holds."""
        found = self._results.get(key)
        if found is None:
            return None
        outcome, until = found
        return outcome if until is None or self._clock() < until else None

    def _say(self, level: int, message: str, kind: str) -> None:
        """A line for an image not read (``kind``: busy, or the error's type): on a server once a minute for each kind,
        with how many times since the line before."""
        if not self.space:
            log.log(level, message, kind)
            return
        with self._pacing:
            paced = self._paced.setdefault((message, kind), Paced())
        if count := paced.due():
            since = f" ({count} times since the line before)" if count > 1 else ""
            log.log(level, message + "%s", kind, since)

    def remember(
        self, key: str, rects: list[tuple[int, int, int, int]], withhold: bool, reason: str | None = None
    ) -> None:
        decision: dict[str, Any] = {"rects": [list(r) for r in rects], "withhold": withhold}
        if withhold and reason and reason != YOU_WITHHELD:
            decision["reason"] = reason
        self._decided.put(key, decision)
        if self.decisions is not None:
            try:
                self.decisions.save_image_decision(key, decision)
            except Exception as exc:  # the decision still applies in this run
                log.error("could not keep a decision on an image: %s", type(exc).__name__)

    def decided(self, key: str) -> dict[str, Any] | None:
        found = self._decided.get(key)
        if found is None and self.decisions is not None:
            found = self.decisions.load_image_decision(key)
            if found is not None:
                self._decided.put(key, found)
        return found

    def _as_decided(self, work: ImageWork, modify: bool) -> ImageWork:
        """An image you decided on before: withheld again, or your rectangles drawn again."""
        decision = self.decided(work.key)
        if not decision:
            return work
        if decision.get("withhold"):
            reason = str(decision.get("reason") or YOU_WITHHELD)
            work.state, work.reason, work.slot = UNREAD, reason, None
            if modify:
                work.withhold(reason)
            return work
        rects = [(int(a), int(b), int(c), int(d)) for a, b, c, d in decision.get("rects") or []]
        if rects:
            work.rects = rects
            if modify:
                work.masked("text", work.text, [])
        return work

    def _read(self, decoded: Decoded) -> tuple[OcrResult | _UnreadOcr, bool]:
        """The image's text, or why it could not be read; and whether that holds whatever the moment (a computer keeps
        all it keeps for good, a server what its read says whatever the moment: ``KEPT_SECONDS``)."""
        from latet.vision.ocr import OcrBusy, OcrError, OcrTooLong, OcrTooManyTiles, OcrTooWide

        if not self.ocr.ready:
            self.ocr.start_loading()
            wait = getattr(self.ocr, "wait_ready", None)
            if wait is not None:
                wait(self.BUDGET_S)
            if not self.ocr.ready:
                error = self.ocr.error
                if error is None:
                    return _UnreadOcr(
                        "the text recognition models were still loading", "unavailable", retry=True
                    ), False
                # On a server by its type: its words could name the server's folders.
                why = f"the text recognition failed ({described(error) if self.space else error})"
                return _UnreadOcr(why, "unavailable"), not self.space
        try:
            # On a server the identity's turn (``SpaceReads``) holds its two threads' reads to one, within its wait.
            with contextlib.nullcontext() if self.space else self._lock:
                return self.ocr.read(decoded.image), True
        except OcrBusy:  # a server's spaces share the text recognition, and others held it for longer than a read waits
            self._say(logging.WARNING, "could not read an image in time: the text recognition was %s", "busy")
            return _UnreadOcr(BUSY, "busy", retry=True), False
        except (OcrError, ValueError, OSError, MemoryError) as exc:
            # The error's type, in the log and in why the image is withheld (which takes its place in the request and
            # is recorded): the message of an error raised while an image is read could quote what was read in it. Out
            # of memory, the image is withheld all the same: never sent unread, and never an error of the request.
            self._say(logging.ERROR, "could not read an image: %s", type(exc).__name__)
            lasting = not self.space or isinstance(exc, OcrTooManyTiles | OcrTooWide)
            reason: OcrUnreadReason = "unavailable"
            if isinstance(exc, OcrTooLong):
                reason = "too_long"
            elif isinstance(exc, OcrTooManyTiles):
                reason = "too_many_tiles"
            elif isinstance(exc, OcrTooWide):
                reason = "too_wide"
            return _UnreadOcr(f"its text could not be read ({type(exc).__name__})", reason), lasting
        except Exception as exc:
            if not self.space or type(exc).__module__ != _ONNXRUNTIME:
                raise
            # ONNX Runtime's own error (an allocation inside a run, a run ended at its deadline): on a server, an image
            # whose text could not be read, as any other, never an error of the request.
            self._say(logging.ERROR, "could not read an image: %s", type(exc).__name__)
            return _UnreadOcr(f"its text could not be read ({type(exc).__name__})", "unavailable"), False

    def review_picture(self, work: ImageWork) -> str | None:
        """The image as the local reviewer is shown it: each masked value as a label, your rectangles as
        bars, its longest side at most ``REVIEW_SIDE``; a PNG data URL. None: there is nothing to show."""
        if work.result is None or work.decoded is None or work.state == WITHHELD:
            return None
        from PIL import Image

        from latet.vision.paint import paint  # NumPy: the ocr extra

        bars = [Cover(tuple(float(v) for v in rect), tuple(float(v) for v in rect)) for rect in work.rects]  # type: ignore[arg-type]
        image = paint(work.decoded.image, [*review_covers(work.result, work.replacements), *bars])
        if max(image.size) > REVIEW_SIDE:
            image = image.copy()
            image.thumbnail((REVIEW_SIDE, REVIEW_SIDE), Image.Resampling.LANCZOS)
        data, media = encode(image, "image/png")
        return f"data:{media};base64,{data}"

    def paint(self, work: ImageWork, replacements: Sequence[Replacement]) -> tuple[str, str]:
        assert work.result is not None
        assert work.decoded is not None
        cache = (work.key, work.text + "\0" + repr(work.rects))
        found = self._painted.get(cache)
        if found is None:
            from latet.vision.paint import paint  # NumPy: the ocr extra

            bars = [Cover(tuple(float(v) for v in rect), tuple(float(v) for v in rect)) for rect in work.rects]  # type: ignore[arg-type]
            image = paint(work.decoded.image, [*covers(work.result, replacements), *bars])
            # Never larger than providers take, unless the image came larger (a provider that takes it).
            found = sized(image, work.decoded.media_type, max(MAX_SENT, len(work.original[0]) * 3 // 4))
            self._painted.put(cache, found)
        self.known.put(image_key(found[0]), MASKED)
        return found
