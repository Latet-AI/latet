"""Images in and out: decoded once, encoded the same way every time.

The same image must come out as the same bytes in every request of a conversation, and after a
restart: the provider caches the conversation by its bytes, and a changed image would read as a new
one. So encoding has no timestamps or other metadata, and fixed settings.
"""

from __future__ import annotations

import base64
import binascii
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

MAX_BYTES = 20 * 1024 * 1024  # larger images are not read
MAX_PIXELS = 40_000_000
# A JPEG is decoded in as many passes over the whole image as it has scans, which libjpeg does not bound: a progressive
# one has about ten. A server's spaces decode none of more (each start of scan is the marker FF DA, which never stands
# in a scan's own data), counted before anything is decoded.
MAX_SCANS = 64
FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}


class ImageError(Exception):
    """An image that cannot be read: not an image, too large, or animated."""


@dataclass(slots=True)
class Decoded:
    image: Image.Image  # RGB or RGBA, orientation applied
    media_type: str  # what the image really is, whatever the request said


def decode(data: str, max_pixels: int = MAX_PIXELS, *, max_scans: int | None = None) -> Decoded:
    """An image from its base64; one of more than ``max_pixels`` is not read (a server's spaces read smaller ones), nor
    an animation, nor (``max_scans``: a server's spaces') a JPEG of more scans than that."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("not valid base64") from exc
    if len(raw) > MAX_BYTES:
        raise ImageError(f"larger than {MAX_BYTES // 2**20} MB")
    if max_scans is not None and raw.startswith(b"\xff\xd8") and raw.count(b"\xff\xda") > max_scans:
        raise ImageError(f"a JPEG of more than {max_scans} scans")
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            fmt = opened.format or ""
            media = next((m for m, f in FORMATS.items() if f == fmt), "")
            if not media:
                raise ImageError(f"an image format Latet does not read ({fmt or 'unknown'})")
            if opened.width * opened.height > max_pixels:
                raise ImageError(f"larger than {max_pixels / 1_000_000:g} megapixels")
            # One frame past the first is looked for: counting them all (n_frames) walks every frame of a GIF in Python.
            if getattr(opened, "is_animated", False):
                raise ImageError("an animation")
            image = ImageOps.exif_transpose(opened)
            image = image.convert("RGBA" if _has_alpha(image) else "RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageError("not an image Latet can read") from exc
    return Decoded(image, media)


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)


def encode(image: Image.Image, media_type: str) -> tuple[str, str]:
    """The image's base64 and media type: the same format as it came in (a GIF becomes a PNG)."""
    out = io.BytesIO()
    if media_type == "image/jpeg":
        image.convert("RGB").save(out, "JPEG", quality=92, subsampling=0, optimize=False)
    elif media_type == "image/webp":
        image.save(out, "WEBP", lossless=True, quality=80, method=4, exif=b"", icc_profile=None)
    else:
        media_type = "image/png"
        image.save(out, "PNG", compress_level=6, optimize=False)
    return base64.b64encode(out.getvalue()).decode("ascii"), media_type
