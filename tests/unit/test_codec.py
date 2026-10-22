"""A body's codings undone within a bound (on a server, ``max_request_mb``: the pre-read's C1): a body that would
decode past it is refused as soon as it passes it, holding no more than about the bound in memory, whatever its coding
(gzip, deflate, brotli, zstd, or several); a body under it decodes as it always did; one that is not what its coding
says is unreadable, an error the gateway knows, never one of another kind. With no bound (a computer), decoding is as
it was."""

import gzip
import os
import tracemalloc
import zlib

import brotli
import pytest
import zstandard

from latet.protocols import codec

LIMIT = 1024 * 1024


def _raw_deflate(data: bytes) -> bytes:
    packer = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


def _zstd(data: bytes) -> bytes:
    return zstandard.ZstdCompressor().compress(data)


def _zstd_streamed(data: bytes) -> bytes:
    """A zstd frame that says nothing of its size, as a stream writes it."""
    packer = zstandard.ZstdCompressor(write_content_size=False).compressobj()
    return packer.compress(data) + packer.flush()


PACKERS = [
    ("gzip", gzip.compress),
    ("x-gzip", gzip.compress),
    ("deflate", zlib.compress),
    ("deflate", _raw_deflate),
    ("br", brotli.compress),
    ("zstd", _zstd),
    ("zstd", _zstd_streamed),
]


@pytest.mark.parametrize(("coding", "pack"), PACKERS)
def test_a_body_that_would_decode_past_the_bound_is_refused_holding_about_the_bound(coding, pack):
    packed = pack(b"x" * (16 * LIMIT))
    assert len(packed) < 64 * 1024  # a few KiB on the wire
    tracemalloc.start()
    try:
        with pytest.raises(codec.DecodedTooLarge) as refused:
            codec.decode(packed, coding, limit=LIMIT)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert refused.value.limit == LIMIT and peak < 3 * LIMIT
    assert isinstance(refused.value, ValueError) and str(LIMIT) in str(refused.value)


@pytest.mark.parametrize(("coding", "pack"), PACKERS)
@pytest.mark.parametrize("size", [0, 1, 4096, LIMIT])
def test_a_body_within_the_bound_decodes_as_it_always_did(coding, pack, size):
    data = bytes(range(256)) * (size // 256) + b"x" * (size % 256)
    packed = pack(data)
    assert codec.decode(packed, coding, limit=LIMIT) == data == codec.decode(packed, coding)


def test_several_codings_are_each_bounded_and_more_than_a_few_are_refused():
    data = b'{"text": "' + b"x" * (4 * LIMIT) + b'"}'
    twice = gzip.compress(gzip.compress(data))
    with pytest.raises(codec.DecodedTooLarge):
        codec.decode(twice, "gzip, gzip", limit=LIMIT)
    assert codec.decode(twice, "gzip, gzip") == data  # a computer: as it was
    small = b'{"text": "hello"}'
    assert codec.decode(brotli.compress(gzip.compress(small)), "gzip, br", limit=LIMIT) == small
    layered = small
    for _ in range(codec.CODINGS_KEPT + 1):
        layered = gzip.compress(layered)
    with pytest.raises(codec.UnsupportedEncodingError):
        codec.decode(layered, ", ".join(["gzip"] * (codec.CODINGS_KEPT + 1)), limit=LIMIT)
    assert codec.decode(layered, ", ".join(["gzip"] * (codec.CODINGS_KEPT + 1))) == small
    assert codec.decode(small, "identity", limit=LIMIT) == small and codec.decode(small, None, limit=LIMIT) == small


class _Counted:
    """A decoder whose every call is weighed: how many bytes of input each was handed."""

    def __init__(self, made, handed):
        self._made, self._handed = made, handed

    def __getattr__(self, name):
        found = getattr(self._made, name)
        if name not in ("decompress", "process"):
            return found

        def call(data, *args, **kwargs):
            self._handed.append(len(data))
            return found(data, *args, **kwargs)

        return call


@pytest.fixture
def handed(monkeypatch):
    """The bytes of input each zlib or brotli decoder of the codec is handed, call by call; and how many were made."""
    calls, made = [], []
    inflater, decompressor = codec.zlib.decompressobj, codec.brotli.Decompressor

    def counted_inflater(*args):
        made.append("zlib")
        return _Counted(inflater(*args), calls)

    def counted_decompressor():
        made.append("br")
        return _Counted(decompressor(), calls)

    monkeypatch.setattr(codec.zlib, "decompressobj", counted_inflater)
    monkeypatch.setattr(codec.brotli, "Decompressor", counted_decompressor)
    return calls, made


def test_a_body_of_many_gzip_members_is_refused_past_the_cap_reading_no_further(handed):
    """Fix round 1, Important 1: a client writes one member; a body of many is refused as unreadable once it passes
    ``MEMBERS_KEPT``, the members past it never read (2 MiB of empty ones took 30 s before)."""
    _, made = handed
    empty = gzip.compress(b"", mtime=0)
    for members in (codec.MEMBERS_KEPT + 1, 104_857):
        made.clear()
        with pytest.raises(codec.UnreadableBody):
            codec.decode(empty * members + gzip.compress(b"end", mtime=0), "gzip", limit=LIMIT)
        assert len(made) == codec.MEMBERS_KEPT  # none begun past the cap
    made.clear()
    kept = gzip.compress(b"x", mtime=0) * codec.MEMBERS_KEPT
    assert codec.decode(kept, "gzip", limit=LIMIT) == b"x" * codec.MEMBERS_KEPT and len(made) == codec.MEMBERS_KEPT


@pytest.mark.parametrize(
    ("coding", "pack"),
    [("gzip", gzip.compress), ("deflate", zlib.compress), ("br", lambda data: brotli.compress(data, quality=1))],
)
@pytest.mark.parametrize("compresses", [False, True])
def test_a_large_body_is_handed_to_its_decoder_a_piece_at_a_time(handed, coding, pack, compresses):
    """Each call of a decoder is handed one piece of the body at most, never the rest of it, so that a large body
    decodes in time linear in what it holds (30 MiB of gzip that does not compress took 2.9 s, a whole decode 0.06 s)."""
    calls, _ = handed
    data = bytes(range(256)) * 16_384 if compresses else os.urandom(4 * 1024 * 1024)
    packed = pack(data)
    assert codec.decode(packed, coding, limit=LIMIT * 8) == data
    assert max(calls) <= codec.DECODE_STEP  # one piece at most at each call
    assert sum(calls) <= len(packed) + len(data) + 2 * codec.DECODE_STEP  # what it is handed grows with the body


@pytest.mark.parametrize("pack", [_raw_deflate, zlib.compress, gzip.compress], ids=["raw", "zlib", "gzip"])
def test_a_body_whose_last_piece_decodes_past_a_step_decodes_whole(pack):
    """Once the body is spent, zlib may still hold what it decoded of its last piece past a step: asked again with no
    more input, it gives it, and only a stream that then makes no more is cut short (one just past a multiple of
    64 KiB was refused before)."""
    for size in [step * codec.DECODE_STEP + delta for step in (1, 2, 3) for delta in range(-6, 7)]:
        data = os.urandom(size // 2) + b"a" * (size - size // 2)  # its end a run: little input, much output
        coding = "gzip" if pack is gzip.compress else "deflate"
        assert codec.decode(pack(data), coding, limit=LIMIT) == data, size


def test_a_gzip_body_of_several_members_decodes_whole_and_within_the_bound():
    two = gzip.compress(b"first,") + gzip.compress(b"second")
    assert codec.decode(two, "gzip", limit=LIMIT) == b"first,second" == gzip.decompress(two)
    many = gzip.compress(b"x" * (LIMIT // 2)) * 3
    with pytest.raises(codec.DecodedTooLarge):
        codec.decode(many, "gzip", limit=LIMIT)


@pytest.mark.parametrize(
    ("coding", "body"),
    [
        ("gzip", b"not gzip at all"),
        ("gzip", gzip.compress(b"x" * 10_000)[:-20]),  # cut short
        ("gzip", gzip.compress(b"x") + b"trailing"),
        ("deflate", b"not deflate at all"),
        ("deflate", zlib.compress(b"x" * 10_000)[:-20]),
        ("br", b"not brotli"),
        ("br", brotli.compress(b"x" * 10_000)[:-3]),
        ("zstd", b"not zstd at all"),
        ("zstd", _zstd(b"x") + b"trailing"),
    ],
    ids=[
        "gzip-invalid",
        "gzip-truncated",
        "gzip-trailing",
        "deflate-invalid",
        "deflate-truncated",
        "brotli-invalid",
        "brotli-truncated",
        "zstd-invalid",
        "zstd-trailing",
    ],
)
def test_a_body_that_is_not_what_its_coding_says_is_unreadable(coding, body):
    with pytest.raises(codec.UnreadableBody) as unread:
        codec.decode(body, coding, limit=LIMIT)
    assert isinstance(unread.value, ValueError)


def test_a_zstd_body_cut_short_decodes_to_no_more_than_it_holds():
    """zstd's readers end a frame cut short without a word: what it holds is all that is read (what a provider would
    read of it too), and the request is then judged by that, never by more."""
    data = b"y" * 100_000 + bytes(range(256)) * 400
    cut = _zstd_streamed(data)[:-10]
    try:
        found = codec.decode(cut, "zstd", limit=LIMIT)
    except codec.UnreadableBody:
        return
    assert data.startswith(found) and len(found) < len(data)


def test_a_zstd_frame_that_asks_a_window_past_what_http_allows_is_refused():
    params = zstandard.ZstdCompressionParameters.from_level(3, window_log=24)  # 16 MiB, past RFC 9659's 8 MiB
    packer = zstandard.ZstdCompressor(compression_params=params).compressobj()
    wide = packer.compress(b"z" * 1000) + packer.flush()
    with pytest.raises(codec.UnreadableBody):
        codec.decode(wide, "zstd", limit=LIMIT)
    assert codec.decode(wide, "zstd") == b"z" * 1000  # a computer: as it was


def test_an_unknown_coding_is_still_refused_as_unsupported():
    with pytest.raises(codec.UnsupportedEncodingError):
        codec.decode(b"x", "compress", limit=LIMIT)
