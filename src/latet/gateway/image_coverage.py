"""Bounded accounting of inline image containers, independent of pixel recognition.

Every byte is an enumerated container field, checked metadata, or the main image
codestream. Pixel authorization is supplied separately by successful OCR.
"""

from __future__ import annotations

import base64
import binascii
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass

from latet.protocols import codec
from latet.vision.codec import MAX_BYTES, MAX_PIXELS

MAX_METADATA = 1 << 20
MAX_PARTS = 4096
# The bytes one metadata block's distinct ranges (an ICC profile's tags, an EXIF block's IFDs and values) may cover in
# all, per byte of the block: ICC lets tags share their data, which is marked once; ranges that overlap otherwise are
# not what a profile or a camera writes, and past this they are refused (G2 review I-3).
COVERED_MULTIPLE = 2
_HOLES = bytes([255]) + bytes(255)  # a mark's 0 (nothing covers the byte) as 0xFF, its 1 as 0
PHOTO_RECOVERY = (
    "Latet cannot read all of this image's metadata or embedded images. Rewind to before the image or start a new "
    "conversation, because the image stays in its history. Remove the metadata before attaching it again: "
    "iOS: Share, Options, Location off; macOS Preview: Tools, Show Inspector, Remove Location Info; "
    "Windows: Properties, Details, Remove Properties and Personal Information."
)
WEBP_RECOVERY = (
    "Latet cannot fully inspect original WebP images in verify-only mode. Export or convert the image to PNG, "
    "then rewind to before the image or start a new conversation before attaching it again, "
    "because the original image stays in its history."
)
NO_OCR = (  # the same words on both machines (one code path): where each installs text recognition is said
    "Latet cannot read this image's text: text recognition is not installed. On a computer, latet ocr install "
    "installs it; on a Latet server, its administrator installs it there. Then rewind to before the image or start "
    "a new conversation, because the image stays in its history."
)


class UnreadImage(ValueError):
    def __init__(self, *, webp: bool = False) -> None:
        super().__init__(WEBP_RECOVERY if webp else PHOTO_RECOVERY)


@dataclass(frozen=True, slots=True)
class ImageCoverage:
    texts: tuple[str, ...]
    size: int


class _PngStream:
    """Account for the one zlib stream with at most 64 KiB of decoded bytes live.

    Pass extents include each scanline's filter byte. Adam7's empty passes have
    no scanlines. No pixel plane is retained or reconstructed here.
    """

    def __init__(self, header: bytes) -> None:
        width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", header)
        if width * height > MAX_PIXELS:
            raise UnreadImage()
        bits = depth * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        passes = (
            ((0, 0, 1, 1),)
            if not interlace
            else (
                (0, 0, 8, 8),
                (4, 0, 8, 8),
                (0, 4, 4, 8),
                (2, 0, 4, 4),
                (0, 2, 2, 4),
                (1, 0, 2, 2),
                (0, 1, 1, 2),
            )
        )
        self.passes: list[tuple[int, int]] = []
        total = 0
        for x, y, dx, dy in passes:
            w, h = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
            if w and h:
                stride = 1 + (w * bits + 7) // 8
                total += stride * h
                self.passes.append((stride, total))
        self.total, self.read, self.phase = total, 0, 0
        self.zlib = zlib.decompressobj()

    def feed(self, payload: bytes) -> None:
        if self.zlib.eof and payload:
            raise UnreadImage()
        pending = payload
        while pending:
            limit = min(65536, self.total - self.read + 1)
            try:
                block = self.zlib.decompress(pending, limit)
            except zlib.error as error:
                raise UnreadImage() from error
            if self.zlib.unused_data or self.read + len(block) > self.total:
                raise UnreadImage()
            offset = 0
            while offset < len(block):
                stride, end = self.passes[self.phase]
                start = self.passes[self.phase - 1][1] if self.phase else 0
                count = min(len(block) - offset, end - self.read)
                first_filter = (-(self.read - start)) % stride
                if any(value > 4 for value in block[offset + first_filter : offset + count : stride]):
                    raise UnreadImage()
                offset += count
                self.read += count
                if self.read == end:
                    self.phase += 1
            pending = self.zlib.unconsumed_tail

    def finish(self) -> None:
        if not self.zlib.eof or self.zlib.unused_data or self.read != self.total:
            raise UnreadImage()


def _uncovered(data: bytes | memoryview, marks: bytearray) -> bool:
    """Whether a byte of ``data`` that ``marks`` leaves uncovered is other than zero (padding is zero): one pass at C's
    speed, whatever the block's size."""
    return bool(int.from_bytes(data, "big") & int.from_bytes(marks.translate(_HOLES), "big"))


class _Covered:
    """The bytes of one metadata block its fields cover (G2 review I-3). Each distinct (start, size) is marked once, so
    that tags sharing their data cost one mark; every byte marked is charged to the reader's metadata budget, as its
    texts are; and ranges that cover more than ``COVERED_MULTIPLE`` times the block in all refuse it. Before, each tag
    or entry marked its whole range again: a profile of 4,090 tags naming one curve of 1 MiB cost seconds."""

    def __init__(self, reader: _Reader, size: int) -> None:
        self.reader = reader
        self.marks = bytearray(size)
        self.ranges: set[tuple[int, int]] = set()
        self.total = 0
        self.most = COVERED_MULTIPLE * size

    def mark(self, start: int, size: int) -> None:
        if (start, size) in self.ranges:
            return
        self.ranges.add((start, size))
        self.total += size
        if self.total > self.most:
            raise UnreadImage()
        self.reader.charge(size)
        self.marks[start : start + size] = b"\1" * size

    def uncovered(self, data: bytes | memoryview) -> bool:
        return _uncovered(data, self.marks)


class _Reader:
    def __init__(self, reserve: Callable[[str, int, int], None] | None = None) -> None:
        self.reserve = reserve
        self.texts: list[str] = []
        self.parts = self.metadata = 0

    def charge(self, size: int) -> None:
        """``size`` bytes of metadata read: within the image's ``MAX_METADATA``, or it is refused."""
        self.metadata += size
        if self.metadata > MAX_METADATA:
            raise UnreadImage()

    def part(self) -> None:
        if self.reserve is not None:
            self.reserve("part", self.parts, 0)
        self.parts += 1
        if self.parts > MAX_PARTS:
            raise UnreadImage()

    def text(self, data: bytes | memoryview, encoding: str = "utf-8") -> None:
        self.charge(len(data))
        if self.reserve is not None:
            self.reserve("text", len(self.texts), len(data) * 4)
        try:
            text = bytes(data).decode(encoding)
        except (UnicodeError, LookupError):
            raise UnreadImage() from None
        self.texts.append(text.rstrip("\0"))

    def inflated(self, data: bytes | memoryview) -> bytes:
        try:
            return codec.decode(bytes(data), "deflate", limit=MAX_METADATA - self.metadata)
        except ValueError:
            raise UnreadImage() from None

    def exif(self, raw: bytes | memoryview) -> None:
        if len(raw) > MAX_METADATA:
            raise UnreadImage()
        data = memoryview(raw)
        if bytes(data[:6]) == b"Exif\0\0":
            data = data[6:]
        if len(data) < 8 or bytes(data[:2]) not in (b"II", b"MM"):
            raise UnreadImage()
        order = "<" if bytes(data[:2]) == b"II" else ">"
        if struct.unpack_from(order + "H", data, 2)[0] != 42:
            raise UnreadImage()
        pending = [struct.unpack_from(order + "I", data, 4)[0]]
        seen: set[int] = set()
        covered = _Covered(self, len(data))
        covered.mark(0, 8)
        while pending:
            offset = pending.pop()
            if offset in seen or offset < 8 or offset + 2 > len(data):
                raise UnreadImage()
            seen.add(offset)
            count = struct.unpack_from(order + "H", data, offset)[0]
            end = offset + 2 + 12 * count + 4
            if end > len(data):
                raise UnreadImage()
            covered.mark(offset, end - offset)
            if struct.unpack_from(order + "I", data, end - 4)[0]:
                raise UnreadImage()  # IFD1 thumbnail or another embedded image
            for at in range(offset + 2, end - 4, 12):
                self.part()
                tag, kind, number, pointer = struct.unpack_from(order + "HHII", data, at)
                if tag in (0x8825, 0x927C, 0x0201, 0x0202, 0x014A):
                    raise UnreadImage()  # GPS, maker note, thumbnail, subimages
                if tag in (0x8769, 0xA005):
                    if kind != 4 or number != 1:
                        raise UnreadImage()
                    pending.append(pointer)
                    continue
                sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 9: 4, 10: 8}
                if kind not in sizes or number > MAX_METADATA:
                    raise UnreadImage()
                size = sizes[kind] * number
                if size < 4 and any(data[at + 8 + size : at + 12]):
                    raise UnreadImage()  # unused inline TIFF value bytes are padding, not metadata
                start = at + 8 if size <= 4 else pointer
                if start + size > len(data):
                    raise UnreadImage()
                if size > 4:  # an inline value lies in its entry, marked with its IFD
                    covered.mark(start, size)
                if kind == 2:
                    self.text(data[start : start + size], "ascii")
                elif tag not in _EXIF_NUMERIC:
                    raise UnreadImage()
        # TIFF alignment padding is zero; arbitrary unused bytes are not a text proof.
        if covered.uncovered(data):
            raise UnreadImage()

    def icc(self, raw: bytes | memoryview) -> None:
        if len(raw) > MAX_METADATA:
            raise UnreadImage()
        data = memoryview(raw)
        if len(data) < 132 or bytes(data[36:40]) != b"acsp" or struct.unpack_from(">I", data)[0] != len(data):
            raise UnreadImage()
        count = struct.unpack_from(">I", data, 128)[0]
        if count > MAX_PARTS or 132 + count * 12 > len(data):
            raise UnreadImage()
        covered = _Covered(self, len(data))
        covered.mark(0, 132 + count * 12)
        if any(data[100:128]):
            raise UnreadImage()
        read: set[tuple[int, int, bool]] = set()
        for at in range(132, 132 + count * 12, 12):
            self.part()
            name, start, size = struct.unpack_from(">4sII", data, at)
            if start < 132 + count * 12 or start + size > len(data) or size < 8:
                raise UnreadImage()
            textual = name in _ICC_TEXT
            if not textual and name not in _ICC_RENDERING:
                raise UnreadImage()
            if (start, size, textual) in read:
                continue  # data another tag of its kind shares (one curve for rTRC, gTRC and bTRC): read once
            read.add((start, size, textual))
            covered.mark(start, size)
            payload = data[start : start + size]
            kind = bytes(payload[:4])
            if any(payload[4:8]):
                raise UnreadImage()
            if textual:
                if kind == b"desc":
                    if len(payload) < 12:
                        raise UnreadImage()
                    length = struct.unpack_from(">I", payload, 8)[0]
                    if 12 + length > len(payload):
                        raise UnreadImage()
                    self.text(payload[12 : 12 + length], "ascii")
                    if any(payload[12 + length :]):
                        raise UnreadImage()
                elif kind == b"text":
                    self.text(payload[8:], "ascii")
                elif kind == b"mluc":
                    if len(payload) < 16:
                        raise UnreadImage()
                    number, stride = struct.unpack_from(">II", payload, 8)
                    if stride != 12 or number > MAX_PARTS or 16 + number * 12 > len(payload):
                        raise UnreadImage()
                    used = bytearray(len(payload))
                    used[: 16 + number * 12] = b"\1" * (16 + number * 12)
                    for entry in range(16, 16 + number * 12, 12):
                        length, where = struct.unpack_from(">II", payload, entry + 4)
                        if where < 16 + number * 12 or where + length > len(payload):
                            raise UnreadImage()
                        self.text(payload[entry : entry + 4], "ascii")
                        self.text(payload[where : where + length], "utf-16-be")
                        used[where : where + length] = b"\1" * length
                    if _uncovered(payload, used):
                        raise UnreadImage()
                else:
                    raise UnreadImage()
            elif not self.icc_rendering(kind, payload):
                raise UnreadImage()
        if covered.uncovered(data):
            raise UnreadImage()

    @staticmethod
    def icc_rendering(kind: bytes, payload: memoryview) -> bool:
        """Finite matrix/TRC and lookup-table grammars; no tag-length wildcard."""
        size = len(payload)
        if kind == b"XYZ ":
            return size == 20
        if kind == b"sf32":
            return size == 44  # 3x3 chromatic adaptation matrix
        if kind == b"curv":
            return size >= 12 and size == 12 + 2 * struct.unpack_from(">I", payload, 8)[0]
        if kind == b"para":
            if size < 12 or any(payload[10:12]):
                return False
            function = struct.unpack_from(">H", payload, 8)[0]
            return function < 5 and size == 12 + 4 * (1, 3, 4, 5, 7)[function]
        if kind == b"chrm":
            return size >= 12 and size == 12 + 8 * struct.unpack_from(">H", payload, 8)[0]
        if kind in {b"mft1", b"mft2"}:
            if size < 52 or payload[11] != 0:
                return False
            incoming, outgoing, grid = payload[8:11]
            if not 1 <= incoming <= 4 or not 1 <= outgoing <= 4 or not 2 <= grid <= 33:
                return False
            cells = grid**incoming * outgoing
            if kind == b"mft1":
                return size == 48 + 256 * (incoming + outgoing) + cells
            inputs, outputs = struct.unpack_from(">HH", payload, 48)
            return (
                2 <= inputs <= 4096
                and 2 <= outputs <= 4096
                and size == 52 + 2 * (inputs * incoming + cells + outputs * outgoing)
            )
        return size == {b"meas": 36, b"view": 36, b"sig ": 12}.get(kind, -1)

    def png(self, data: bytes) -> None:
        at, ended, first = 8, False, True
        color, palette, depth = -1, 0, 0
        seen: set[bytes] = set()
        pixels: _PngStream | None = None
        idat_ended = False
        while at < len(data):
            self.part()
            if ended or at + 12 > len(data):
                raise UnreadImage()
            size, kind = struct.unpack_from(">I4s", data, at)
            end = at + 12 + size
            if end > len(data) or zlib.crc32(data[at + 4 : end - 4]) != struct.unpack_from(">I", data, end - 4)[0]:
                raise UnreadImage()
            payload = data[at + 8 : end - 4]
            if first and kind != b"IHDR":
                raise UnreadImage()
            first = False
            if kind in seen and kind not in (b"IDAT", b"tEXt", b"zTXt", b"iTXt"):
                raise UnreadImage()
            if b"IDAT" in seen and kind != b"IDAT":
                idat_ended = True
            if kind in (b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB") and seen & {b"PLTE", b"IDAT"}:
                raise UnreadImage()
            if kind == b"PLTE" and seen & {b"tRNS", b"bKGD"}:
                raise UnreadImage()
            if kind in (b"PLTE", b"tRNS", b"bKGD", b"pHYs", b"iDOT", b"eXIf") and b"IDAT" in seen:
                raise UnreadImage()
            if (kind == b"sRGB" and b"iCCP" in seen) or (kind == b"iCCP" and b"sRGB" in seen):
                raise UnreadImage()
            seen.add(kind)
            if kind == b"IHDR":
                if size != 13 or color != -1:
                    raise UnreadImage()
                depth, color = payload[8:10]
                if color not in (0, 2, 3, 4, 6) or any(payload[10:12]) or payload[12] not in (0, 1):
                    raise UnreadImage()
                if depth not in {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}[
                    color
                ] or not all(struct.unpack_from(">II", payload)):
                    raise UnreadImage()
                pixels = _PngStream(payload)
            elif kind == b"IEND":
                if size or b"IDAT" not in seen:
                    raise UnreadImage()
                ended = True
            elif kind in _PNG_SIZES:
                if size not in _PNG_SIZES[kind] or not self.png_control(kind, payload, color, depth, palette):
                    raise UnreadImage()
            elif kind == b"PLTE":
                if color in (0, 4) or not 0 < size <= 768 or size % 3 or (color == 3 and size // 3 > 1 << depth):
                    raise UnreadImage()
                palette = size // 3
            elif kind == b"tRNS":
                if not (
                    (color == 0 and size == 2) or (color == 2 and size == 6) or (color == 3 and 0 < size <= palette)
                ):
                    raise UnreadImage()
            elif kind == b"IDAT":
                if pixels is None or idat_ended or (color == 3 and not palette):
                    raise UnreadImage()
                pixels.feed(payload)
            elif kind == b"eXIf":
                self.exif(payload)
            elif kind in (b"tEXt", b"zTXt", b"iTXt", b"iCCP"):
                keyword, sep, tail = payload.partition(b"\0")
                if not sep or not 1 <= len(keyword) <= 79:
                    raise UnreadImage()
                self.text(keyword, "latin-1")
                if kind == b"tEXt":
                    self.text(tail, "latin-1")
                elif kind in (b"zTXt", b"iCCP"):
                    if not tail or tail[0] != 0:
                        raise UnreadImage()
                    decoded = self.inflated(tail[1:])
                    if kind == b"iCCP":
                        self.icc(decoded)
                    else:
                        self.text(decoded, "latin-1")
                else:
                    if len(tail) < 4 or tail[0] not in (0, 1) or tail[1] != 0:
                        raise UnreadImage()
                    language, sep, rest = tail[2:].partition(b"\0")
                    translated, sep2, text = rest.partition(b"\0")
                    if not sep or not sep2:
                        raise UnreadImage()
                    self.text(language, "ascii")
                    self.text(translated)
                    self.text(self.inflated(text) if tail[0] else text)
            else:
                raise UnreadImage()
            at = end
        if not ended or pixels is None:
            raise UnreadImage()
        pixels.finish()

    @staticmethod
    def png_control(kind: bytes, payload: bytes, color: int, depth: int, palette: int) -> bool:
        if kind == b"sRGB":
            return payload[0] <= 3
        if kind == b"gAMA":
            return struct.unpack(">I", payload)[0] != 0
        if kind == b"pHYs":
            return payload[8] <= 1
        if kind == b"tIME":
            year, month, day, hour, minute, second = struct.unpack(">H5B", payload)
            return year > 0 and 1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second <= 60
        if kind == b"sBIT":
            return len(payload) == {0: 1, 2: 3, 3: 3, 4: 2, 6: 4}.get(color) and all(
                0 < v <= (8 if color == 3 else depth) for v in payload
            )
        if kind == b"bKGD":
            if color == 3:
                return len(payload) == 1 and payload[0] < palette
            count = 1 if color in (0, 4) else 3
            return len(payload) == 2 * count and all(
                value < 1 << depth for value in struct.unpack(">" + "H" * count, payload)
            )
        return True  # cHRM fixed unsigned coordinates; iDOT fixed layout

    def jpeg(self, data: bytes) -> None:
        at, ended = 2, False
        icc: dict[int, bytes] = {}
        icc_total = icc_bytes = 0
        while at < len(data):
            self.part()
            if data[at] != 255:
                raise UnreadImage()
            while at < len(data) and data[at] == 255:
                at += 1
            if at >= len(data):
                raise UnreadImage()
            kind = data[at]
            at += 1
            if kind == 0xD9:
                ended = at == len(data)
                break
            if kind in range(0xD0, 0xD8):
                continue
            if at + 2 > len(data):
                raise UnreadImage()
            size = struct.unpack_from(">H", data, at)[0]
            if size < 2 or at + size > len(data):
                raise UnreadImage()
            payload = data[at + 2 : at + size]
            at += size
            if kind == 0xDA:
                if not payload or not 1 <= payload[0] <= 4 or len(payload) != 4 + 2 * payload[0]:
                    raise UnreadImage()
                while at < len(data):
                    if data[at] == 255 and at + 1 < len(data):
                        after = data[at + 1]
                        if after == 0 or 0xD0 <= after <= 0xD7:
                            at += 2
                            continue
                        break
                    at += 1
            elif kind in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if len(payload) < 6 or not 1 <= payload[5] <= 4 or len(payload) != 6 + 3 * payload[5]:
                    raise UnreadImage()
            elif kind == 0xDD:
                if len(payload) != 2:
                    raise UnreadImage()
            elif kind in {0xC4, 0xDB}:
                cursor = 0
                while cursor < len(payload):
                    table = payload[cursor]
                    cursor += 1
                    if table & 15 > 3 or table >> 4 > 1:
                        raise UnreadImage()
                    if kind == 0xDB:
                        cursor += 64 * (1 + (table >> 4))
                    else:
                        if cursor + 16 > len(payload):
                            raise UnreadImage()
                        cursor += 16 + sum(payload[cursor : cursor + 16])
                    if cursor > len(payload):
                        raise UnreadImage()
                if not payload:
                    raise UnreadImage()
            elif kind == 0xE0:
                if (
                    not payload.startswith(b"JFIF\0")
                    or len(payload) < 14
                    or len(payload) != 14 + 3 * payload[12] * payload[13]
                ):
                    raise UnreadImage()
                if payload[12] or payload[13]:
                    raise UnreadImage()
            elif kind == 0xE1:
                if payload.startswith(b"Exif\0\0"):
                    self.exif(payload)
                elif payload.startswith(b"http://ns.adobe.com/xap/1.0/\0"):
                    self.text(payload[29:])
                else:
                    raise UnreadImage()
            elif kind == 0xE2:
                if not payload.startswith(b"ICC_PROFILE\0") or len(payload) < 14:
                    raise UnreadImage()
                number, total = payload[12:14]
                if not 1 <= number <= total or total > 255 or number in icc or (icc_total and total != icc_total):
                    raise UnreadImage()
                icc_bytes += len(payload) - 14
                if icc_bytes > MAX_METADATA:
                    raise UnreadImage()
                icc_total = total
                icc[number] = payload[14:]
            elif kind == 0xEE:
                if len(payload) != 12 or not payload.startswith(b"Adobe"):
                    raise UnreadImage()
            elif kind == 0xFE:
                self.text(payload, "latin-1")
            elif kind == 0xED:
                self.iptc(payload)
            else:
                raise UnreadImage()
        if not ended:
            raise UnreadImage()
        if icc:
            if len(icc) != icc_total:
                raise UnreadImage()
            self.icc(b"".join(icc[i] for i in range(1, icc_total + 1)))

    def iptc(self, data: bytes) -> None:
        if not data.startswith(b"Photoshop 3.0\0"):
            raise UnreadImage()
        at = 14
        while at < len(data):
            self.part()
            if data[at : at + 4] != b"8BIM" or at + 7 > len(data):
                raise UnreadImage()
            resource = struct.unpack_from(">H", data, at + 4)[0]
            size = data[at + 6] + 1
            name = data[at + 7 : at + 6 + size]
            self.text(name, "latin-1")
            if size % 2 and (at + 6 + size >= len(data) or data[at + 6 + size] != 0):
                raise UnreadImage()
            at += 6 + size + size % 2
            if at + 4 > len(data) or resource != 0x0404:
                raise UnreadImage()
            length = struct.unpack_from(">I", data, at)[0]
            at += 4
            end = at + length
            if end > len(data):
                raise UnreadImage()
            while at < end:
                if at + 5 > end or data[at] != 0x1C:
                    raise UnreadImage()
                record, field = data[at + 1 : at + 3]
                number = struct.unpack_from(">H", data, at + 3)[0]
                at += 5
                if number & 0x8000 or at + number > end or record != 2 or field not in _IPTC_TEXT:
                    raise UnreadImage()
                self.text(data[at : at + number], "latin-1")
                at += number
            if length % 2 and (end >= len(data) or data[end] != 0):
                raise UnreadImage()
            at = end + length % 2

    def gif(self, data: bytes) -> None:
        if len(data) < 13 or not all(struct.unpack_from("<HH", data, 6)):
            raise UnreadImage()
        at = 13 + (3 * (1 << ((data[10] & 7) + 1)) if data[10] & 128 else 0)
        while at < len(data):
            self.part()
            kind = data[at]
            at += 1
            if kind == 0x3B:
                if at != len(data):
                    raise UnreadImage()
                return
            if kind == 0x2C:
                if at + 9 >= len(data):
                    raise UnreadImage()
                flags = data[at + 8]
                if flags & 0x18 or not all(struct.unpack_from("<HH", data, at + 4)):
                    raise UnreadImage()
                at += 9 + (3 * (1 << ((flags & 7) + 1)) if flags & 128 else 0)
                if at >= len(data) or not 2 <= data[at] <= 8:
                    raise UnreadImage()
                at += 1
                _, at = self.subblocks(data, at, retain=False)
            elif kind == 0x21:
                if at >= len(data):
                    raise UnreadImage()
                label = data[at]
                at += 1
                if label == 0xFE:
                    payload, at = self.subblocks(data, at)
                    self.text(payload, "latin-1")
                elif label == 0xF9:
                    if at + 6 > len(data) or data[at] != 4 or data[at + 5] != 0:
                        raise UnreadImage()
                    if data[at + 1] & 0xE0 or (data[at + 1] >> 2) & 7 > 3:
                        raise UnreadImage()
                    at += 6
                elif label == 0xFF:
                    if at + 12 > len(data) or data[at] != 11:
                        raise UnreadImage()
                    name = data[at + 1 : at + 12]
                    at += 12
                    if name == b"NETSCAPE2.0":
                        if at + 5 > len(data) or data[at : at + 2] != b"\3\1" or data[at + 4] != 0:
                            raise UnreadImage()
                        at += 5
                    elif name == b"XMP DataXMP" and data[:6] == b"GIF89a":
                        begin = at
                        _, at = self.subblocks(data, at, retain=False)
                        magic = b"\1" + bytes(range(255, -1, -1)) + b"\0"
                        if at - begin < len(magic) or data[at - len(magic) : at] != magic:
                            raise UnreadImage()
                        self.text(memoryview(data)[begin : at - len(magic)])
                    else:
                        raise UnreadImage()
                else:
                    raise UnreadImage()
            else:
                raise UnreadImage()
        raise UnreadImage()

    def subblocks(self, data: bytes, at: int, *, retain: bool = True) -> tuple[bytes, int]:
        chunks = []
        size = 0
        while at < len(data):
            self.part()
            length = data[at]
            at += 1
            if not length:
                return b"".join(chunks), at
            if at + length > len(data):
                raise UnreadImage()
            size += length
            if size > (MAX_METADATA if retain else MAX_BYTES):
                raise UnreadImage()
            if retain:
                chunks.append(data[at : at + length])
            at += length
        raise UnreadImage()


_EXIF_NUMERIC = frozenset(
    {
        0x0100,
        0x0101,
        0x0102,
        0x0103,
        0x0106,
        0x0112,
        0x0115,
        0x0116,
        0x011A,
        0x011B,
        0x0128,
        0x0213,
        0x829A,
        0x829D,
        0x8822,
        0x8827,
        0x9201,
        0x9202,
        0x9203,
        0x9204,
        0x9205,
        0x9206,
        0x9207,
        0x9208,
        0x9209,
        0x920A,
        0xA001,
        0xA002,
        0xA003,
        0xA20E,
        0xA20F,
        0xA210,
        0xA217,
        0xA401,
        0xA402,
        0xA403,
        0xA404,
        0xA405,
        0xA406,
    }
)
_ICC_TEXT = frozenset({b"desc", b"cprt", b"dmnd", b"dmdd"})
_ICC_RENDERING = frozenset(
    {
        b"wtpt",
        b"bkpt",
        b"rXYZ",
        b"gXYZ",
        b"bXYZ",
        b"rTRC",
        b"gTRC",
        b"bTRC",
        b"kTRC",
        b"chad",
        b"chrm",
        b"A2B0",
        b"A2B1",
        b"A2B2",
        b"B2A0",
        b"B2A1",
        b"B2A2",
        b"gamt",
        b"lumi",
        b"meas",
        b"view",
        b"tech",
    }
)
_ICC_TYPES = frozenset(
    {b"XYZ ", b"curv", b"para", b"sf32", b"chrm", b"mft1", b"mft2", b"mAB ", b"mBA ", b"meas", b"view", b"sig "}
)
_PNG_SIZES = {
    b"IHDR": {13},
    b"cHRM": {32},
    b"gAMA": {4},
    b"sBIT": {1, 2, 3, 4},
    b"sRGB": {1},
    b"bKGD": {1, 2, 6},
    b"pHYs": {9},
    b"tIME": {7},
    b"iDOT": {28},
}
_IPTC_TEXT = frozenset(
    {
        5,
        7,
        10,
        12,
        15,
        20,
        25,
        40,
        55,
        60,
        62,
        63,
        65,
        70,
        75,
        80,
        85,
        90,
        92,
        95,
        100,
        101,
        103,
        105,
        110,
        115,
        116,
        118,
        120,
        122,
    }
)


def inspect_image(encoded: str, reserve: Callable[[str, int, int], None] | None = None) -> ImageCoverage:
    if len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
        raise UnreadImage()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise UnreadImage() from None
    if len(raw) > MAX_BYTES:
        raise UnreadImage()
    reader = _Reader(reserve)
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        reader.png(raw)
    elif raw.startswith(b"\xff\xd8"):
        reader.jpeg(raw)
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        # Public pixel decoders expose no complete VP8/VP8L/ALPH consumption proof.
        # Gateway rewriting converts its owned decoded pixels to a checked PNG.
        raise UnreadImage(webp=True)
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        reader.gif(raw)
    else:
        raise UnreadImage()
    return ImageCoverage(tuple(reader.texts), len(raw))
