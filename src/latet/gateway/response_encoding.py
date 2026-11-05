"""Positive encoded completion proof for the private response sink."""

from __future__ import annotations

import zlib
from collections.abc import Iterator, Sequence
from hashlib import sha256
from typing import IO, cast

import brotli
import zstandard

from latet.protocols.codec import MEMBERS_KEPT, ZSTD_WINDOW

_STEP = 64 * 1024
_GZIP = zlib.MAX_WBITS | 16
_BROTLI_RETURN = 98_304
_ZSTD_TERMINAL_RESIDUAL = 131_072


class ResponseEncodingError(ValueError):
    """The encoded response has no complete bounded sink reading."""


def inflate(blocks: Sequence[bytes | str], bits: int, limit: int) -> Iterator[bytes]:
    """Read one proved dialect in bounded pieces without joining encoded blocks."""
    decoder = zlib.decompressobj(bits)
    members = 1
    total = 0
    for block in blocks:
        if not isinstance(block, bytes):
            raise ResponseEncodingError("response-malformed")
        for start in range(0, len(block), _STEP):
            data: bytes | memoryview = memoryview(block)[start : start + _STEP]
            while data:
                if decoder.eof:
                    if bits != _GZIP:
                        raise ResponseEncodingError("response-malformed")
                    if members == MEMBERS_KEPT:
                        raise ResponseEncodingError("response-budget")
                    members += 1
                    decoder = zlib.decompressobj(bits)
                piece = decoder.decompress(data, min(_STEP, limit - total + 1))
                total += len(piece)
                if total > limit:
                    raise ResponseEncodingError("response-budget")
                if piece:
                    yield piece
                data = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
    while not decoder.eof:
        piece = decoder.decompress(b"", min(_STEP, limit - total + 1))
        total += len(piece)
        if total > limit:
            raise ResponseEncodingError("response-budget")
        if not piece:
            break
        yield piece
    if not decoder.eof:
        raise ResponseEncodingError("response-incomplete")


def prove_zlib(blocks: Sequence[bytes | str], coding: str, limit: int) -> int:
    """Discard bounded proof output before any semantic reader sees this dialect."""
    bits = _GZIP if coding in ("gzip", "x-gzip") else zlib.MAX_WBITS
    try:
        for _ in inflate(blocks, bits, limit):
            pass
    except zlib.error:
        if bits == _GZIP:
            raise ResponseEncodingError("response-malformed") from None
    else:
        return bits

    bits = -zlib.MAX_WBITS
    try:
        for _ in inflate(blocks, bits, limit):
            pass
    except zlib.error:
        raise ResponseEncodingError("response-malformed") from None
    return bits


def _steps(blocks: Sequence[bytes | str]) -> Iterator[bytes]:
    for block in blocks:
        if not isinstance(block, bytes):
            raise ResponseEncodingError("response-malformed")
        for start in range(0, len(block), _STEP):
            yield block[start : start + _STEP]


def brotli_parts(blocks: Sequence[bytes | str], limit: int) -> Iterator[bytes]:
    """Decode a bounded, complete Brotli stream without retaining returned chunks."""
    decoder = brotli.Decompressor()
    total = 0

    def process(data: bytes) -> bytes:
        nonlocal total
        piece = decoder.process(data, output_buffer_limit=min(_STEP, limit - total + 1))
        if len(piece) > _BROTLI_RETURN:
            raise ResponseEncodingError("response-malformed")
        total += len(piece)
        if total > limit:
            raise ResponseEncodingError("response-budget")
        return piece

    def drain(*, final: bool) -> Iterator[bytes]:
        while not decoder.is_finished() and (final or not decoder.can_accept_more_data()):
            piece = process(b"")
            if not piece:
                if not decoder.is_finished() and (final or not decoder.can_accept_more_data()):
                    raise ResponseEncodingError("response-incomplete")
                return
            yield piece
            del piece

    try:
        for data in _steps(blocks):
            if decoder.is_finished():
                raise ResponseEncodingError("response-malformed")
            piece = process(data)
            if piece:
                yield piece
            del piece
            yield from drain(final=False)
        yield from drain(final=True)
    except brotli.error:
        raise ResponseEncodingError("response-malformed") from None


class _BlockReader:
    """Present the staged blocks as one source, with EOF only at real exhaustion."""

    __slots__ = ("_blocks", "_index", "_offset")

    def __init__(self, blocks: Sequence[bytes | str]) -> None:
        self._blocks = blocks
        self._index = self._offset = 0

    @property
    def exhausted(self) -> bool:
        return self._index == len(self._blocks)

    def read(self, size: int) -> bytes:
        if size <= 0:
            raise ResponseEncodingError("response-malformed")
        while self._index < len(self._blocks):
            block = self._blocks[self._index]
            if not isinstance(block, bytes):
                raise ResponseEncodingError("response-malformed")
            end = min(self._offset + min(size, _STEP), len(block))
            piece = block[self._offset : end]
            self._offset = end
            if end == len(block):
                self._index += 1
                self._offset = 0
            if piece:
                return piece
        return b""


def zstd_parts(blocks: Sequence[bytes | str], limit: int) -> Iterator[bytes]:
    """Read bounded output. This API alone cannot prove encoded completion."""
    source = _BlockReader(blocks)
    total = 0
    try:
        context = zstandard.ZstdDecompressor(max_window_size=ZSTD_WINDOW)
        with context.stream_reader(
            cast("IO[bytes]", source), read_size=_STEP, read_across_frames=True, closefd=False
        ) as reader:
            while True:
                size = min(_STEP, limit - total + 1)
                piece = reader.read(size)
                if len(piece) > size:
                    raise ResponseEncodingError("response-malformed")
                if not piece:
                    if not source.exhausted:
                        raise ResponseEncodingError("response-incomplete")
                    return
                total += len(piece)
                if total > limit:
                    raise ResponseEncodingError("response-budget")
                yield piece
                del piece
    except zstandard.ZstdError:
        raise ResponseEncodingError("response-malformed") from None


def _measure(parts: Iterator[bytes]) -> tuple[int, bytes]:
    total = 0
    digest = sha256()
    for part in parts:
        total += len(part)
        digest.update(part)
        del part
    return total, digest.digest()


def prove_zstd(blocks: Sequence[bytes | str], limit: int) -> None:
    """Prove original-byte EOS after bounding native replay output with a fresh reader."""
    expected_count, expected_digest = _measure(zstd_parts(blocks, limit))
    # The reader may strand one final native block. This is replay scratch,
    # never an increase to the accepted decoded-response allowance.
    replay_bound = expected_count + _ZSTD_TERMINAL_RESIDUAL
    total = 0
    digest = sha256()
    decoder = None
    finished = False
    try:
        for data in _steps(blocks):
            while data:
                if decoder is None:
                    decoder = zstandard.ZstdDecompressor(max_window_size=ZSTD_WINDOW).decompressobj(
                        write_size=_STEP, read_across_frames=False
                    )
                piece = decoder.decompress(data)
                total += len(piece)
                if total > replay_bound:
                    raise ResponseEncodingError("response-malformed")
                if total > limit:
                    raise ResponseEncodingError("response-budget")
                digest.update(piece)
                del piece

                unused = decoder.unused_data
                if decoder.unconsumed_tail or (
                    unused and (not decoder.eof or len(unused) >= len(data) or not data.endswith(unused))
                ):
                    raise ResponseEncodingError("response-malformed")
                if decoder.eof:
                    finished = True
                    decoder = None
                data = unused
    except zstandard.ZstdError:
        raise ResponseEncodingError("response-malformed") from None
    if not finished or decoder is not None:
        raise ResponseEncodingError("response-incomplete")
    if total != expected_count or digest.digest() != expected_digest:
        raise ResponseEncodingError("response-malformed")


def prove_brotli(blocks: Sequence[bytes | str], limit: int) -> None:
    """Complete a count/discard pass before any Brotli output reaches semantic readers."""
    for part in brotli_parts(blocks, limit):
        del part
