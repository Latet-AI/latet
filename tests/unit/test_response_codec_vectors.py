"""Finite strict codec boundary checks using the actual pinned native APIs."""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace

import brotli
import pytest
import zstandard

from latet.gateway import response_encoding as codec
from latet.gateway.recorder import ResponseCapture

_STEP = 65_536
_WIRE = 16_384
_CAP = 8_000_000
_SMALL = 131_089
_TAIL = b"\0"
_DIRECT = (
    "gzip-64",
    "gzip-65",
    "xgzip-64-split",
    "raw-deflate",
    "raw-deflate-suffix",
    "wrapped-deflate-suffix-split",
    "br-fragment-drain",
    "br-suffix-same",
    "br-second-stream-later",
    "zstd-concat-same",
    "zstd-concat-split",
    "zstd-suffix",
    "zstd-empty-frame",
    "zstd-empty-wire",
)
_CAP_CASES = ("br-cap", "zstd-cap", "br-cap-plus-one", "zstd-cap-plus-one")


@dataclass
class _Calls:
    gzip: int = 0
    br: int = 0
    br_drained: int = 0
    reader_passes: int = 0
    proof_objects: int = 0
    proof_calls: int = 0


def _require(condition: bool, category: str) -> None:
    if not condition:
        pytest.fail(category, pytrace=False)


def _measure(parts: Iterable[bytes]) -> tuple[int, bytes]:
    digest = sha256()
    count = 0
    for piece in parts:
        count += len(piece)
        digest.update(piece)
        del piece
    return count, digest.digest()


def _zstd_compressor() -> zstandard.ZstdCompressor:
    return zstandard.ZstdCompressor(write_content_size=False, write_checksum=True, threads=0)


def _small_fixture(case: str) -> tuple[tuple[bytes, ...], bytes, str, str | None]:
    if case in ("gzip-64", "gzip-65", "xgzip-64-split"):
        count = 65 if case == "gzip-65" else 64
        plain = bytes(65 + index % 26 for index in range(count))
        members = tuple(zlib.compress(bytes((symbol,)), wbits=31) for symbol in plain)
        if case == "xgzip-64-split":
            return (members[0], b"".join(members[1:])), plain, "x-gzip", None
        return (b"".join(members),), plain, "gzip", "response-budget" if count == 65 else None
    if case in ("raw-deflate", "raw-deflate-suffix", "wrapped-deflate-suffix-split"):
        plain = b"raw deflate control"
        bits = 15 if case == "wrapped-deflate-suffix-split" else -15
        wire = zlib.compress(plain, wbits=bits)
        if case == "raw-deflate":
            return (wire,), plain, "deflate", None
        if case == "raw-deflate-suffix":
            return (wire + _TAIL,), plain, "deflate", "response-malformed"
        return (wire, _TAIL), plain, "deflate", "response-malformed"
    if case in ("br-fragment-drain", "br-suffix-same", "br-second-stream-later"):
        plain = b"A" * _SMALL if case == "br-fragment-drain" else b"body"
        wire = brotli.compress(plain, quality=1, lgwin=20)
        if case == "br-fragment-drain":
            return (wire[:1], wire[1:]), plain, "br", None
        if case == "br-suffix-same":
            return (wire + _TAIL,), plain, "br", "response-malformed"
        other = brotli.compress(b"tail", quality=1, lgwin=20)
        return (wire, other), plain, "br", "response-malformed"
    zstd = _zstd_compressor()
    if case == "zstd-empty-wire":
        return (), b"", "zstd", "response-incomplete"
    if case == "zstd-empty-frame":
        return (zstd.compress(b""),), b"", "zstd", None
    first, second = zstd.compress(b"body"), zstd.compress(b"tail")
    if case == "zstd-suffix":
        return (first + _TAIL,), b"body", "zstd", "response-malformed"
    blocks = (first + second,) if case == "zstd-concat-same" else (first, second)
    return blocks, b"bodytail", "zstd", None


def _count_gzip(patch: pytest.MonkeyPatch, calls: _Calls) -> None:
    native = codec.zlib.decompressobj

    def make(bits):
        calls.gzip += 1
        _require(calls.gzip <= 128, "gzip test call bound")
        return native(bits)

    patch.setattr(codec.zlib, "decompressobj", make)


def _count_brotli(patch: pytest.MonkeyPatch, calls: _Calls) -> None:
    native = codec.brotli.Decompressor

    def make():
        decoder = native()

        def process(data, *, output_buffer_limit):
            calls.br += 1
            _require(calls.br <= 1024, "Brotli test call bound")
            piece = decoder.process(data, output_buffer_limit=output_buffer_limit)
            calls.br_drained += bool(piece) and not data
            return piece

        return SimpleNamespace(
            process=process,
            is_finished=decoder.is_finished,
            can_accept_more_data=decoder.can_accept_more_data,
        )

    patch.setattr(codec.brotli, "Decompressor", make)


def _count_zstd(patch: pytest.MonkeyPatch, calls: _Calls, *, cap_case: bool) -> None:
    native = codec.zstandard.ZstdDecompressor

    def make(**options):
        context = native(**options)

        @contextmanager
        def stream_reader(*args, **kwargs) -> Iterator[SimpleNamespace]:
            calls.reader_passes += 1
            _require(calls.reader_passes <= 2, "zstd reader pass bound")
            reads = 0
            with context.stream_reader(*args, **kwargs) as reader:

                def read(size):
                    nonlocal reads
                    reads += 1
                    _require(reads <= (124 if cap_case else 128), "zstd reader call bound")
                    return reader.read(size)

                yield SimpleNamespace(read=read)

        def decompressobj(**kwargs):
            calls.proof_objects += 1
            _require(calls.proof_objects <= (1 if cap_case else 3), "zstd proof object bound")
            decoder = context.decompressobj(**kwargs)

            class _Proof:
                def decompress(self, data):
                    calls.proof_calls += 1
                    _require(calls.proof_calls <= (1 if cap_case else 128), "zstd proof call bound")
                    return decoder.decompress(data)

                def __getattr__(self, name):
                    return getattr(decoder, name)

            return _Proof()

        return SimpleNamespace(stream_reader=stream_reader, decompressobj=decompressobj)

    patch.setattr(codec.zstandard, "ZstdDecompressor", make)


def _prove(blocks: tuple[bytes, ...], coding: str, limit: int) -> Iterable[bytes]:
    if coding == "br":
        codec.prove_brotli(blocks, limit)
        return codec.brotli_parts(blocks, limit)
    if coding == "zstd":
        codec.prove_zstd(blocks, limit)
        return codec.zstd_parts(blocks, limit)
    bits = codec.prove_zlib(blocks, coding, limit)
    return codec.inflate(blocks, bits, limit)


@pytest.mark.parametrize("case", _DIRECT, ids=_DIRECT)
def test_strict_codec_vectors(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        blocks, plain, coding, refusal = _small_fixture(case)
    except (brotli.error, zstandard.ZstdError, zlib.error):
        pytest.fail("small native fixture generation failed", pytrace=False)
    size = sum(map(len, blocks))
    _require(size <= _WIRE and (size > 0 or case == "zstd-empty-wire"), "small fixture wire bound")
    expected = _measure((plain,))
    del plain
    calls = _Calls()
    with monkeypatch.context() as patch:
        if coding == "br":
            _count_brotli(patch, calls)
        elif coding == "zstd":
            _count_zstd(patch, calls, cap_case=False)
        elif coding in ("gzip", "x-gzip"):
            _count_gzip(patch, calls)

        observed = None
        count_matches = False
        try:
            replay = _prove(blocks, coding, _SMALL)
            proof_gzip = calls.gzip
            count_matches = _measure(replay) == expected
        except codec.ResponseEncodingError as exc:
            observed = str(exc)
            proof_gzip = calls.gzip
        except (brotli.error, zstandard.ZstdError, zlib.error):
            pytest.fail("uncategorized native codec error", pytrace=False)

        checks = {
            "expected categorical verdict": observed == refusal,
            "accepted output count and digest match": refusal is not None or count_matches,
            "gzip proof constructs at most 64 members": coding not in ("gzip", "x-gzip") or proof_gzip == 64,
            "Brotli fragment case drained output": case != "br-fragment-drain" or calls.br_drained > 0,
        }
        assert all(checks.values()), checks


def _large_fixture(coding: str, count: int) -> tuple[bytes, tuple[int, bytes]]:
    step = b"A" * _STEP
    full, rest = divmod(count, _STEP)
    _require(full == 122 and rest in (4608, 4609), "cap fixture source shape")
    digest = sha256()
    wire = bytearray()
    calls = 0
    if coding == "br":
        br = brotli.Compressor(quality=1, lgwin=20)
        encode, finish = br.process, br.finish
    else:
        zstd = _zstd_compressor().compressobj()
        encode, finish = zstd.compress, zstd.flush

    def keep(piece: bytes) -> None:
        nonlocal calls
        calls += 1
        _require(calls <= 124 and len(wire) + len(piece) <= _WIRE, "cap fixture encoded bound")
        wire.extend(piece)

    for index in range(full + 1):
        part = step if index < full else step[:rest]
        digest.update(part)
        keep(encode(part))
    keep(finish())
    _require(calls == 124 and bool(wire), "cap fixture compressor count")
    return bytes(wire), (count, digest.digest())


@pytest.mark.parametrize("case", _CAP_CASES, ids=_CAP_CASES)
def test_strict_codec_decoded_cap(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    coding = "br" if case.startswith("br-") else "zstd"
    exceeded = case.endswith("-plus-one")
    try:
        wire, expected = _large_fixture(coding, _CAP + exceeded)
    except (brotli.error, zstandard.ZstdError):
        pytest.fail("cap native fixture generation failed", pytrace=False)
    calls = _Calls()
    semantic_calls = 0

    def semantic_consumer(_capture: ResponseCapture, _piece: bytes) -> None:
        nonlocal semantic_calls
        semantic_calls += 1

    with monkeypatch.context() as patch:
        if coding == "br":
            _count_brotli(patch, calls)
        else:
            _count_zstd(patch, calls, cap_case=True)
        if exceeded:
            patch.setattr(ResponseCapture, "_decoded", semantic_consumer)
            capture = ResponseCapture(None, "text/event-stream", coding)
            capture.feed(wire)
            capture.end()
            accepted = capture.screen()
            checks = {
                "cap plus one refused before semantic reading": (
                    not accepted and capture.omission == "response-budget" and semantic_calls == 0
                ),
                "zstd first pass refused without proof": coding != "zstd"
                or (calls.reader_passes == 1 and calls.proof_objects == 0 and calls.proof_calls == 0),
            }
        else:
            try:
                matched = _measure(_prove((wire,), coding, _CAP)) == expected
            except (codec.ResponseEncodingError, brotli.error, zstandard.ZstdError):
                pytest.fail("exact cap helper refused", pytrace=False)
            checks = {
                "exact cap count and digest preserved": matched,
                "zstd exact cap uses one proof and bounded replay": coding != "zstd"
                or (calls.reader_passes == 2 and calls.proof_objects == 1 and calls.proof_calls == 1),
            }
        assert all(checks.values()), checks
