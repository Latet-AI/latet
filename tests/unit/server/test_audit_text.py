"""What a Latet server's administration log makes of what it is given, with no services needed: the hash of an entry, the
tag of an anchor, and the texts and details it keeps."""

import copy

import pytest

from latet.server import audit
from latet.server.audit import _details, _text, anchor_tag, entry_hash
from latet.server.tokens import keyed_hash

KEY, OTHER_KEY = b"\x0e" * 32, b"\x0b" * 32
NUL, LONE, SUBSTITUTE = chr(0), chr(0xD83D), chr(0xFFFD)  # a zero byte, a lone surrogate, and what each is kept as
ENTRY = ("p" * 64, 1_000_000.0, "admin:a_first00000", "person.added", "u_emilycarte", {"kind": "person"}, "10.0.0.5")


def test_an_entry_hash_is_a_keyed_hash_of_the_previous_hash_and_every_field():
    base = entry_hash(KEY, *ENTRY)
    assert len(base) == 64 and set(base) <= set("0123456789abcdef")
    assert entry_hash(KEY, *ENTRY) == base  # the same entry, the same hash
    assert entry_hash(OTHER_KEY, *ENTRY) != base  # another key, another hash
    others = [
        "q" * 64,
        1_000_001.0,
        "admin:a_second0000",
        "person.removed",
        "u_someoneelse",
        {"kind": "key"},
        "10.0.0.6",
    ]
    for place, value in enumerate(others):
        changed = list(ENTRY)
        changed[place] = value
        assert entry_hash(KEY, *changed) != base, place


def test_nothing_an_empty_text_and_the_word_none_are_three_different_values():
    hashes = {
        entry_hash(KEY, *ENTRY[:4], target, ENTRY[5], address)
        for target in (None, "", "None")
        for address in (None, "", "None")
    }
    assert len(hashes) == 9


def test_the_text_of_one_field_cannot_move_into_the_next():
    assert entry_hash(KEY, "p", 1.0, "a", "bc", None, {}, None) != entry_hash(KEY, "p", 1.0, "ab", "c", None, {}, None)
    assert entry_hash(KEY, "p", 1.0, "a", "b", "c", {}, None) != entry_hash(KEY, "p", 1.0, "a", "b", None, {}, "c")
    assert entry_hash(KEY, "p", 1.0, "a", "b", None, {}, None) != entry_hash(KEY, "pa", 1.0, "", "b", None, {}, None)


def test_a_details_hash_does_not_depend_on_the_order_of_its_keys_but_on_its_values():
    def hashed(details):
        return entry_hash(KEY, *ENTRY[:5], details, ENTRY[6])

    first = hashed({"a": 1, "b": {"c": 2, "d": [3, 4]}})
    assert first == hashed({"b": {"d": [3, 4], "c": 2}, "a": 1})
    assert first != hashed({"a": 1, "b": {"c": 2, "d": [4, 3]}})  # a list keeps its order
    assert first != hashed({"a": "1", "b": {"c": 2, "d": [3, 4]}})  # a text is not a count


def test_an_anchors_tag_is_keyed_and_covers_both_what_it_says():
    base = anchor_tag(KEY, 7, "h" * 64)
    assert base == anchor_tag(KEY, 7, "h" * 64) and len(base) == 64
    assert (
        len({base, anchor_tag(OTHER_KEY, 7, "h" * 64), anchor_tag(KEY, 8, "h" * 64), anchor_tag(KEY, 7, "i" * 64)}) == 4
    )
    assert anchor_tag(KEY, 1, "23") != anchor_tag(KEY, 12, "3")  # where the id ends and the hash begins


def test_an_anchors_tag_is_made_under_a_label_of_its_own_and_never_stands_for_an_entrys_hash():
    """The two are made under one key from texts of the same kind: the label keeps what one says from being taken for
    what the other says, whatever a writer of the table copies from the one to the other."""
    assert audit._ENTRY != audit._ANCHOR
    parts = ("7", "h" * 64)
    assert anchor_tag(KEY, 7, "h" * 64) == keyed_hash(KEY, audit._ANCHOR, *parts)
    assert anchor_tag(KEY, 7, "h" * 64) != keyed_hash(KEY, audit._ENTRY, *parts)  # the same parts, the entries' label


def test_the_walk_reads_pages_of_ten_thousand():
    assert audit.PAGE == 10_000


def test_a_zero_byte_and_a_lone_surrogate_become_the_replacement_character_and_nothing_else_changes():
    assert _text(f"a{NUL}b{LONE}c") == f"a{SUBSTITUTE}b{SUBSTITUTE}c"
    assert _text(NUL) == SUBSTITUTE and all(_text(chr(cp)) == SUBSTITUTE for cp in range(0xD800, 0xE000))
    # Accents, an emoji with its joiner, a tab, a line break, an escape: a record says it all.
    accents = "Zo" + chr(0xEB) + " M" + chr(0xFC) + "ller " + chr(0x1F469) + chr(0x200D) + chr(0x1F4BB)
    controls = chr(9) + chr(10) + chr(0x1B) + chr(0x7F) + chr(0xFFFD) + chr(0xFFFE)
    assert _text(accents + controls) == accents + controls


def test_every_string_of_the_details_is_cleaned_keys_included_and_the_given_details_are_left_as_they_were():
    given = {f"k{NUL}": [f"a{LONE}", {f"d{LONE}": (1, True, None, NUL)}], "n": 3, "flag": False}
    before = copy.deepcopy(given)
    kept = _details(given)
    assert kept == {
        f"k{SUBSTITUTE}": [f"a{SUBSTITUTE}", {f"d{SUBSTITUTE}": [1, True, None, SUBSTITUTE]}], "n": 3, "flag": False
    }  # fmt: skip
    assert kept["flag"] is False and type(kept["n"]) is int and given == before


def test_no_details_are_an_empty_mapping():
    assert _details(None) == {} and _details({}) == {}


@pytest.mark.parametrize("value", [1.5, 2.0, -0.0, float("nan"), float("inf")], ids=repr)
def test_a_float_is_refused_wherever_it_is_and_the_error_does_not_quote_it(value):
    for details in ({"x": value}, {"x": [value]}, {"x": {"y": (1, value)}}):
        with pytest.raises(TypeError, match="float") as refused:
            _details(details)
        assert repr(value) not in str(refused.value)


@pytest.mark.parametrize(
    "value", [b"bytes", {1, 2}, object(), 1 + 2j, ..., frozenset()], ids=lambda v: type(v).__name__
)
def test_what_json_has_no_name_for_is_refused_by_its_type(value):
    with pytest.raises(TypeError) as refused:
        _details({"x": [value]})
    assert type(value).__name__ in str(refused.value)


@pytest.mark.parametrize("details", [{1: "a"}, {None: "a"}, {("t",): 1}, {"a": {2: 3}}, [1, 2], "text", 5], ids=repr)
def test_a_key_that_is_no_text_and_details_that_are_no_mapping_are_refused(details):
    with pytest.raises(TypeError):
        _details(details)
