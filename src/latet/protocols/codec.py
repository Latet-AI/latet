"""Content-Encoding codecs for request and response bodies (identity, gzip, deflate, br, zstd).

``decode`` with a ``limit`` (on a Latet server, ``max_request_mb``: what one request may decode to, the pre-read's C1)
undoes each coding a step at a time and stops as soon as what it makes would pass the limit: a few KiB that would
inflate to gigabytes cost no more memory than about the limit, whatever the coding or how many are stacked. Each step
hands the decoder one piece of the body at most, never the rest of it, so that a body costs time in proportion to it
(a decoder that is handed the rest of a body keeps a copy of it at each step); and a gzip body of more members than a
client writes is refused. Without a limit (a computer, where only its own agents reach it), decoding is as it always
was."""

from __future__ import annotations

import gzip
import zlib

import brotli
import zstandard

DECODE_STEP = 64 * 1024  # bytes a bounded decode makes at a time, and hands its decoder at a time, at most
CODINGS_KEPT = 3  # codings a bounded decode undoes at most: a client applies one, and each costs a decode of the limit
MEMBERS_KEPT = 64  # members of a gzip body a bounded decode reads at most: a client writes one
ZSTD_WINDOW = 8 * 1024 * 1024  # the largest window a bounded zstd decode takes (RFC 9659: an HTTP coder's most)
_GZIP_BITS = 16 + zlib.MAX_WBITS  # zlib reading a gzip member, header and trailer


class UnsupportedEncodingError(ValueError):
    """The body uses a Content-Encoding Latet cannot decode (the request is refused)."""


class DecodedTooLarge(ValueError):
    """A body that would decode to more than ``limit`` bytes: refused as soon as it passed the limit, its rest never
    decoded. Its words say the limit, nothing of the body."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"the request body is larger than {limit} bytes once decoded")
        self.limit = limit


class UnreadableBody(ValueError):
    """A body that is not what its coding says (not that coding, cut short, followed by what is no part of it), as a
    bounded decode finds it: the request is refused as unreadable. Its words name the coding and the decoder's error by
    its type, never what the body holds."""


def _codings(header: str | None) -> list[str]:
    if not header:
        return []
    return [c.strip().lower() for c in header.split(",") if c.strip() and c.strip().lower() != "identity"]


def decode(body: bytes, content_encoding: str | None, *, limit: int | None = None) -> bytes:
    """Undo every coding listed in ``Content-Encoding``, last applied first. ``limit``: the bytes each coding undone
    may make, at most (DecodedTooLarge as soon as it would pass them; UnreadableBody for a body its coding does not
    describe; UnsupportedEncodingError past ``CODINGS_KEPT`` codings); None, no bound."""
    codings = list(reversed(_codings(content_encoding)))
    if limit is None:
        for coding in codings:
            body = _decode_one(body, coding)
        return body
    if len(codings) > CODINGS_KEPT:
        raise UnsupportedEncodingError(f"{len(codings)} codings, where {CODINGS_KEPT} are read at most")
    for coding in codings:
        body = _bounded(body, coding, limit)
    return body


def encode(body: bytes, content_encoding: str | None) -> bytes:
    """Apply the codings listed in ``Content-Encoding`` in order."""
    for coding in _codings(content_encoding):
        body = _encode_one(body, coding)
    return body


def _decode_one(body: bytes, coding: str) -> bytes:
    if coding in ("gzip", "x-gzip"):
        return gzip.decompress(body)
    if coding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)  # raw deflate, as some servers send
    if coding == "br":
        return brotli.decompress(body)
    if coding == "zstd":
        return zstandard.ZstdDecompressor().decompressobj().decompress(body)
    raise UnsupportedEncodingError(coding)


def _encode_one(body: bytes, coding: str) -> bytes:
    if coding in ("gzip", "x-gzip"):
        return gzip.compress(body, mtime=0)
    if coding == "deflate":
        return zlib.compress(body)
    if coding == "br":
        return brotli.compress(body)
    if coding == "zstd":
        return zstandard.ZstdCompressor().compress(body)
    raise UnsupportedEncodingError(coding)


# --- within a bound --------------------------------------------------------------------------------------------------


def _bounded(body: bytes, coding: str, limit: int) -> bytes:
    """``body`` with ``coding`` undone, ``limit`` bytes at most."""
    kind = ""
    try:
        if coding in ("gzip", "x-gzip"):
            return _inflated(body, _GZIP_BITS, limit)
        if coding == "deflate":
            try:
                return _inflated(body, zlib.MAX_WBITS, limit)
            except zlib.error:
                return _inflated(body, -zlib.MAX_WBITS, limit)  # raw deflate, as some servers send
        if coding == "br":
            return _unbrotlied(body, limit)
        if coding == "zstd":
            return _unzstded(body, limit)
    except (DecodedTooLarge, UnreadableBody):
        raise
    except (zlib.error, brotli.error, zstandard.ZstdError, EOFError, OSError, ValueError) as exc:
        kind = type(exc).__name__  # the decoder's words may quote the body: only its type goes on
    if kind:
        raise UnreadableBody(f"{coding}: {kind}")
    raise UnsupportedEncodingError(coding)


def _grown(out: bytearray, chunk: bytes, limit: int) -> None:
    if len(out) + len(chunk) > limit:
        raise DecodedTooLarge(limit)
    out += chunk


def _inflated(body: bytes, wbits: int, limit: int) -> bytes:
    """``body`` inflated by zlib, a step at a time: a zlib stream, raw deflate, or a gzip member and each that follows
    it (as ``gzip.decompress`` reads them), ``MEMBERS_KEPT`` at most. Each call is handed one piece of the body, or
    what zlib left of the piece before when its output was full (at most a piece): the place in the body moves by what
    each call took, and what follows a member is given back to it. Once the body is spent, zlib may still hold what it
    decoded of the last piece: it is asked again with no more input while it gives something, and only then is the
    stream cut short. Raises zlib.error for what zlib refuses and for a stream cut short, UnreadableBody for a body of
    more members than that."""
    view, place, out, members = memoryview(body), 0, bytearray(), 0
    while True:
        members += 1
        if members > MEMBERS_KEPT:
            raise UnreadableBody(f"gzip: more than {MEMBERS_KEPT} members")
        inflater = zlib.decompressobj(wbits)
        left = b""
        while not inflater.eof:
            if left:
                piece, fresh = left, 0
            else:
                piece = view[place : place + DECODE_STEP]
                fresh = len(piece)
                if not fresh:  # the body is spent: what zlib still holds of it, a step at a time
                    held = inflater.decompress(b"", DECODE_STEP)
                    if not held:
                        raise zlib.error("the stream ends before its last block")
                    _grown(out, held, limit)
                    continue
            chunk = inflater.decompress(piece, DECODE_STEP)
            if not chunk and not fresh and inflater.unconsumed_tail == piece:
                raise zlib.error("the stream makes no progress")
            _grown(out, chunk, limit)
            place += fresh
            left = inflater.unconsumed_tail
        place -= len(inflater.unused_data)  # what follows the member, which zlib was handed with it
        if wbits != _GZIP_BITS or place >= len(view):
            return bytes(out)


def _unbrotlied(body: bytes, limit: int) -> bytes:
    """``body`` decompressed by brotli, a step at a time: each call is handed one piece of the body, and makes about
    ``DECODE_STEP``; what the decoder holds of a piece is drained before the next is handed. A body that goes on past
    its stream's end is unreadable; one whose stream does not end, cut short."""
    view, out = memoryview(body), bytearray()
    decompressor = brotli.Decompressor()
    for place in range(0, len(view), DECODE_STEP):
        if decompressor.is_finished():
            raise UnreadableBody("br: what follows the stream's end")
        _grown(out, decompressor.process(view[place : place + DECODE_STEP], output_buffer_limit=DECODE_STEP), limit)
        while not decompressor.can_accept_more_data():
            chunk = decompressor.process(b"", output_buffer_limit=DECODE_STEP)
            if not chunk:
                break  # nothing more to drain: the next piece, or the end
            _grown(out, chunk, limit)
    while not decompressor.is_finished():
        chunk = decompressor.process(b"", output_buffer_limit=DECODE_STEP)
        if not chunk:
            raise UnreadableBody("br: the stream ends before its last block")
        _grown(out, chunk, limit)
    return bytes(out)


def _unzstded(body: bytes, limit: int) -> bytes:
    """``body`` decompressed by zstd, a step at a time, every frame of it, with a window of ``ZSTD_WINDOW`` at most. A
    frame cut short ends with what it holds (zstd's readers say nothing of it): no more is read than a provider
    would."""
    out = bytearray()
    reader = zstandard.ZstdDecompressor(max_window_size=ZSTD_WINDOW).stream_reader(body, read_across_frames=True)
    with reader:
        while chunk := reader.read(DECODE_STEP):
            _grown(out, chunk, limit)
    return bytes(out)
