"""The keyed hash of a credential: what a server keeps of a token, an API key, a one-time code, an administration token or
a route key in place of its plain SHA-256, and the tag that binds a row to what it says. No services are needed."""

import hashlib
import hmac

import pytest

from latet.server.tokens import keyed_hash, secret_hash

KEY, OTHER = b"\x01" * 32, b"\x02" * 32


def test_a_keyed_hash_is_the_hmac_of_a_label_and_its_parts_under_the_key():
    found = keyed_hash(KEY, "secret", "latet_pat_x")
    assert len(found) == 64 and found == found.lower() and found == keyed_hash(KEY, "secret", "latet_pat_x")
    assert found != keyed_hash(OTHER, "secret", "latet_pat_x")  # another key
    assert found != secret_hash("latet_pat_x")  # not the plain SHA-256, which whoever writes the database can make
    assert found != keyed_hash(KEY, "route", "latet_pat_x")  # a label of its own
    assert found != keyed_hash(KEY, "secret", "latet_pat_y")
    # An HMAC-SHA256 of the label and the parts, each after its length: rows keep these, so the format is pinned.
    message = b"latet-credential" + bytes(1)
    message += (6).to_bytes(4, "big") + b"secret" + (11).to_bytes(4, "big") + b"latet_pat_x"
    assert found == hmac.new(KEY, message, hashlib.sha256).hexdigest()


def test_the_known_answers_are_pinned():
    """The rows of a database keep these: a format that changed would leave every token, code and tag unknown."""
    assert (
        keyed_hash(KEY, "secret", "latet_pat_x") == "96a1796a8b2b54f8fd5f12c669d66bcd4a10009519a85ef6cd5f8dc08e3888a0"
    )
    assert keyed_hash(KEY, "token-tag", "t_a", "u_b", "c", "d") == (
        "de088aaaffe832d73da8bf1a6e894ce9286668d358e2d138ce366e41e8d12483"
    )


@pytest.mark.parametrize(
    ("one", "other"),
    [
        (("a", "bc"), ("ab", "c")),
        (("a", "", "b"), ("a", "b")),
        (("ab",), ("a", "b")),
        ((), ("",)),
        (("a",), ("", "a")),
        (("a\x00b", "c"), ("a", "b\x00c")),
    ],
)
def test_no_two_different_lists_of_parts_make_the_same_hash(one, other):
    assert keyed_hash(KEY, "tag", *one) != keyed_hash(KEY, "tag", *other)


def test_the_label_is_not_a_part():
    assert keyed_hash(KEY, "ab", "c") != keyed_hash(KEY, "a", "bc") != keyed_hash(KEY, "abc")


@pytest.mark.parametrize("bad", [b"", b"\x01" * 31, b"\x01" * 33, "x" * 32, None, bytearray(32)])
def test_a_keyed_hash_needs_a_key_of_32_bytes(bad):
    with pytest.raises(ValueError, match="32 bytes"):
        keyed_hash(bad, "secret", "latet_pat_x")


def test_the_key_is_in_no_message():
    key = bytes(range(32))
    with pytest.raises(ValueError, match="32 bytes") as raised:
        keyed_hash(key[:31], "secret", "x")
    assert key[:31].hex() not in str(raised.value) and repr(key[:31]) not in str(raised.value)


def test_a_text_with_a_lone_surrogate_gives_a_hash_that_matches_nothing_and_raises_nothing():
    lone = [keyed_hash(KEY, "secret", "latet_pat_" + chr(code)) for code in (0xD800, 0xD801, 0xDFFF)]
    assert len({*lone}) == 3 and all(len(found) == 64 for found in lone)
