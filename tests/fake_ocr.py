"""A stand-in for the text recognition models: screenshots drawn by the tests, read back exactly; the tutorial's
screenshot, read as the models read it; and stand-ins for the models' sessions, behind a real reader."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import time
from importlib import resources
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from latet.vision.layout import OcrLine, OcrResult, join
from latet.vision.ocr import OcrError, OnnxOcr

FONT_SIZE = 20
LINE_HEIGHT = 34
# The tutorial's screenshot, and what the text recognition models read in it (scripts/tutorial_screenshot.py --read).
SCREENSHOT = resources.files("latet.tutorial") / "assets" / "payment.png"
READING = Path(__file__).parent / "data" / "tutorial" / "payment-ocr.json"


class FakeOcr:
    """Reads, by their pixels, the screenshots :func:`screenshot` drew and the images it was taught with
    :func:`learn`; any other image reads empty."""

    def __init__(self) -> None:
        self.pages: dict[str, OcrResult] = {}
        self.ready = True
        self.error: BaseException | None = None
        self.fail = False  # every read fails
        self.reads = 0

    def start_loading(self) -> None:
        pass

    def wait_ready(self, timeout: float = 0) -> bool:
        return self.ready

    def read(self, image: Image.Image) -> OcrResult:
        self.reads += 1
        if self.fail:
            raise OcrError("the fake reader was told to fail")
        return self.pages.get(_pixels(image), OcrResult(image.width, image.height, (), "", ()))

    def learn(self, image: Image.Image, result: OcrResult) -> None:
        """``image`` reads as ``result`` from now on."""
        self.pages[_pixels(image)] = result

    def screenshot(self, lines: list[str], fmt: str = "PNG", dark: bool = False) -> str:
        """A screenshot showing ``lines``, as base64; its text is known to this reader."""
        font = ImageFont.load_default(FONT_SIZE)
        width = 40 + int(max(font.getlength(line) for line in lines))
        image = Image.new("RGB", (width, 20 + LINE_HEIGHT * len(lines)), (30, 30, 30) if dark else "white")
        draw = ImageDraw.Draw(image)
        read: list[OcrLine] = []
        for i, line in enumerate(lines):
            x, y = 20, 10 + i * LINE_HEIGHT
            draw.text((x, y), line, font=font, fill=(220, 220, 220) if dark else "black")
            edges = tuple(x + font.getlength(line[:k]) for k in range(len(line) + 1))
            read.append(OcrLine((x, y - 4, int(x + font.getlength(line)) + 1, y + FONT_SIZE + 4), line, edges))
        out = io.BytesIO()
        image.save(out, fmt)
        data = base64.b64encode(out.getvalue()).decode()
        self.pages[_pixels(Image.open(io.BytesIO(out.getvalue())).convert("RGB"))] = join(read, *image.size)
        return data


def reading(path: Path = READING) -> OcrResult:
    """A reading recorded by scripts/tutorial_screenshot.py, as the models gave it."""
    data = json.loads(path.read_bytes())
    lines = []
    for line in data["lines"]:
        x0, y0, x1, y1 = line["box"]
        lines.append(OcrLine((x0, y0, x1, y1), line["text"], tuple(line["edges"]), line["score"]))
    return join(lines, data["width"], data["height"])


def payment_reader() -> FakeOcr:
    """A fake reader that reads the tutorial's screenshot as the models do."""
    reader = FakeOcr()
    reader.learn(Image.open(io.BytesIO(SCREENSHOT.read_bytes())), reading())
    return reader


def _pixels(image: Image.Image) -> str:
    return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


# --- stand-ins for the models' sessions -------------------------------------------------------------------------------


class ModelSession:
    """A model session that stands in for one of ONNX Runtime's: each run waits until ``go`` is set (``started`` says
    a run began), then takes ``seconds``, and answers ``out(inputs)``; it keeps the shape of each input (``shapes``)."""

    def __init__(self, out, seconds=0.0, go=None):
        self.out, self.seconds = out, seconds
        self.go = go or threading.Event()
        if go is None:
            self.go.set()
        self.started = threading.Event()
        self.runs = 0
        self.shapes: list[tuple[int, ...]] = []

    def run(self, names, inputs, options=None):
        self.runs += 1
        self.shapes.append(inputs["x"].shape)
        self.started.set()
        assert self.go.wait(10)
        time.sleep(self.seconds)
        return self.out(inputs)


def lines_map(inputs):
    """The detector's map of an image: one line of text every 20 pixels down."""
    _, _, height, width = inputs["x"].shape
    prob = np.zeros((height, width), np.float32)
    for top in range(8, height - 12, 20):
        prob[top : top + 8, 8 : width - 8] = 0.9
    return [prob[None, None]]


def long_lines_map(inputs):
    """The detector's map of an image: a thin line of text across the whole width every 12 pixels down."""
    _, _, height, width = inputs["x"].shape
    prob = np.zeros((height, width), np.float32)
    for top in range(4, height - 8, 12):
        prob[top : top + 4, 2 : width - 2] = 0.9
    return [prob[None, None]]


def nothing_read(inputs):
    """The recognizer's answer for each line of a batch: nothing read."""
    batch, _, _, width = inputs["x"].shape
    return [np.zeros((batch, max(1, width // 8), 3), np.float32)]


def stand_in_ocr(det=None, rec=None, **options) -> OnnxOcr:
    """A text recognition with stand-ins for its two models (``options``: ``OnnxOcr``'s)."""
    ocr = OnnxOcr(Path("det"), Path("rec"), threads=1, **options)
    ocr._det, ocr._rec = det or ModelSession(lines_map), rec or ModelSession(nothing_read)
    ocr._chars = ["", "a", " "]
    return ocr


def blank_image(width=300, height=200):
    return Image.new("RGB", (width, height), "white")
