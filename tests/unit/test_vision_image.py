"""Images decoded and encoded the same way every time, and text covered with its fake or a bar."""

from __future__ import annotations

import base64
import hashlib
import io
import time

import pytest
from PIL import Image, ImageDraw, ImageFont

from latet.engine.types import SlotKind
from latet.protocols.base import ImageRef
from latet.vision import codec
from latet.vision.layout import Cover
from latet.vision.masking import WITHHELD, ImageMasking
from latet.vision.paint import colours, ink, paint

from ..fake_ocr import FakeOcr


def _png(image: Image.Image, fmt: str = "PNG", **kw) -> str:
    out = io.BytesIO()
    image.save(out, fmt, **kw)
    return base64.b64encode(out.getvalue()).decode()


def _screen(dark: bool = False) -> Image.Image:
    bg, fg = ((30, 30, 30), (220, 220, 220)) if dark else ((255, 255, 255), (20, 20, 20))
    image = Image.new("RGB", (420, 60), bg)
    ImageDraw.Draw(image).text((10, 15), "Email: mario.rossi@acme.it", font=ImageFont.load_default(20), fill=fg)
    return image


def test_decode_reports_what_the_image_really_is():
    decoded = codec.decode(_png(_screen(), "JPEG"))
    assert decoded.media_type == "image/jpeg" and decoded.image.mode == "RGB" and decoded.image.size == (420, 60)


@pytest.mark.parametrize(
    ("data", "why"),
    [
        ("not base64!", "base64"),
        (base64.b64encode(b"hello, not an image").decode(), "not an image"),
    ],
)
def test_decode_refuses_what_it_cannot_read(data, why):
    with pytest.raises(codec.ImageError, match=why):
        codec.decode(data)


def test_decode_refuses_animations():
    frames = [Image.new("RGB", (20, 20), c) for c in ("red", "blue")]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:])
    with pytest.raises(codec.ImageError, match="animation"):
        codec.decode(base64.b64encode(out.getvalue()).decode())


def _gif_of(frames: int) -> str:
    """A GIF of ``frames`` frames of one pixel, written by hand: 15 bytes a frame."""
    head = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\x00\x00\x00\xff\xff\xff"
    frame = b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00"
    return base64.b64encode(head + frame * frames + b"\x3b").decode()


def test_an_animation_is_refused_at_once_however_many_frames_it_has():
    """T22.9: counting a GIF's frames walks every one of them in Python (the pre-read's GIF of 19 MB was refused in 1.96
    s, in the space's thread before any budget); looking for a frame past the first is enough."""
    data = _gif_of(1_000_000)
    started = time.monotonic()
    with pytest.raises(codec.ImageError, match="animation"):
        codec.decode(data)
    assert time.monotonic() - started < 0.5


def _jpeg_of_scans(scans: int) -> str:
    """A JPEG whose bytes hold ``scans`` start-of-scan markers (FF DA): its own, and the rest after its end."""
    out = io.BytesIO()
    _screen().save(out, "JPEG")
    raw = out.getvalue()
    assert raw.count(b"\xff\xda") == 1
    return base64.b64encode(raw + b"\xff\xda" * (scans - 1)).decode()


def test_a_jpeg_of_more_scans_than_a_progressive_one_has_is_not_decoded_where_scans_are_bounded(monkeypatch):
    """T22.9: a JPEG is decoded in as many passes as it has scans, which libjpeg does not bound (a progressive one has
    about ten): on a server, one of more than 64 is refused before it is opened; a computer decodes it as before."""
    opened: list[int] = []
    real = Image.open
    monkeypatch.setattr(Image, "open", lambda *args, **kwargs: opened.append(1) or real(*args, **kwargs))
    with pytest.raises(codec.ImageError, match="more than 64 scans"):
        codec.decode(_jpeg_of_scans(65), max_scans=64)
    assert opened == []
    assert codec.decode(_jpeg_of_scans(64), max_scans=64).image.size == (420, 60)
    assert codec.decode(_jpeg_of_scans(65)).image.size == (420, 60)  # no bound: decoded as before


def test_a_spaces_images_are_decoded_with_their_scans_bounded_and_a_computers_as_before():
    def ref() -> ImageRef:
        return ImageRef({"data": _jpeg_of_scans(65)}, "data", "base64", "image/jpeg", SlotKind.TOOL_RESULT, 0)

    reader = FakeOcr()
    work = ImageMasking(reader, space=True).work(ref())
    assert work.state == WITHHELD and "scans" in (work.reason or "") and reader.reads == 0
    assert ImageMasking(FakeOcr()).work(ref()).state != WITHHELD


@pytest.mark.parametrize("media", ["image/png", "image/jpeg", "image/webp"])
def test_encoding_is_the_same_every_time(media):
    image = _screen()
    first, kind = codec.encode(image, media)
    assert kind == media and codec.encode(image.copy(), media)[0] == first
    assert codec.decode(first).media_type == media


def test_a_gif_comes_back_as_a_png():
    assert codec.encode(_screen().convert("P"), "image/gif")[1] == "image/png"


@pytest.mark.parametrize("dark", [False, True])
def test_colours_and_ink_follow_the_theme(dark):
    image = _screen(dark)
    bg, fg = colours(image, (0, 0, 420, 60))
    assert (sum(bg) < 300) == dark and (sum(fg) > 300) == dark
    top, bottom = ink(image, (0, 0, 420, 60), bg) or (0, 0)
    assert 10 <= top < bottom <= 45


def test_paint_writes_the_fake_and_is_deterministic():
    image = _screen()
    covers = [
        Cover((75, 10, 300, 42), (0, 10, 420, 42), "anna.verdi@example.com"),
        Cover((320, 10, 400, 42), (0, 10, 420, 42)),
    ]
    first = paint(image, covers)
    again = paint(image.copy(), covers)
    assert hashlib.sha256(first.tobytes()).digest() == hashlib.sha256(again.tobytes()).digest()
    assert image.tobytes() == _screen().tobytes()  # the original is left alone
    assert first.getpixel((360, 26)) == colours(image, (0, 10, 420, 42))[1]  # the bar: the text's colour


def test_a_long_fake_takes_the_free_space_then_shrinks():
    image = Image.new("RGB", (420, 60), "white")
    ImageDraw.Draw(image).text((10, 15), "Name: Ugo", font=ImageFont.load_default(20), fill="black")
    out = paint(image, [Cover((68, 10, 110, 42), (0, 10, 420, 42), "Massimiliano Bartolomei")])
    changed = [x for x in range(420) if any(out.getpixel((x, y)) != image.getpixel((x, y)) for y in range(10, 42))]
    assert max(changed) > 200  # nothing to its right: it took the free space, at full size
    crowded = paint(_screen(), [Cover((75, 10, 120, 42), (0, 10, 420, 42), "Massimiliano Bartolomei")])
    changed = [
        x for x in range(420) if any(crowded.getpixel((x, y)) != _screen().getpixel((x, y)) for y in range(10, 42))
    ]
    assert max(changed) < 130  # text to its right: squeezed and smaller, never over the next text


def test_a_painted_image_is_never_larger_than_providers_take():
    import os

    from latet.vision.masking import sized

    noise = Image.frombytes("RGB", (256, 256), os.urandom(256 * 256 * 3))  # a PNG of it barely shrinks
    as_png, media = sized(noise, "image/png", 10_000_000)
    assert media == "image/png"
    as_jpeg, media = sized(noise, "image/png", len(as_png) * 3 // 4 - 1)
    assert media == "image/jpeg" and len(as_jpeg) < len(as_png)
    with pytest.raises(codec.ImageError, match="larger than providers take"):
        sized(noise, "image/png", 100)


def test_what_an_image_keeps_in_memory_counts_each_string_it_holds_and_its_pixels():
    """What a batch of the Live Reviewer holds of an image (``ImageWork.held_bytes``, which a server's space weighs):
    its base64 as it came, once while it leaves as it came, the base64 it would leave as besides, a data URL as the
    request carries it besides its base64, and its pixels once read, four bytes each as Pillow keeps them."""
    import sys

    from latet.engine.types import SlotKind
    from latet.protocols.base import ImageRef
    from latet.vision.masking import ImageWork

    data = _png(_screen())
    ref = ImageRef({"type": "base64", "data": data}, "data", "base64", "image/png", SlotKind.TOOL_RESULT, 0)
    work = ImageWork(None, ref, "key", codec.decode(data))  # never painted here: no service to paint it
    assert work.held_bytes == sys.getsizeof(data) + 420 * 60 * 4
    painted = _png(_screen(dark=True))
    ref.set(painted, "image/png")
    assert work.held_bytes == sys.getsizeof(data) + sys.getsizeof(painted) + 420 * 60 * 4
    url = {"url": f"data:image/png;base64,{data}"}
    unread = ImageWork(None, ImageRef(url, "url", "data_url", "image/png", SlotKind.USER_TEXT, 0), "key")
    assert unread.held_bytes == sys.getsizeof(unread.original[0]) + sys.getsizeof(url["url"])  # no pixels: not read
