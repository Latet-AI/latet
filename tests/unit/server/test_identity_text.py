"""What a Latet server keeps of the text a caller gives (a name, an email, a label): without the characters a terminal
acts on or that make one text read as another, stripped, and no longer than a limit. No services are needed."""

import unicodedata

from latet.server import identities
from latet.server.identities import clean

ESC, NUL = chr(0x1B), chr(0)
# The bidirectional controls, as the Unicode database lists them (its Bidi_Control property): the Arabic letter mark,
# the left-to-right and right-to-left marks, the embeddings and overrides, and the isolates.
BIDI = {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}
# What shows as nothing, or splits a line: the zero-width space, the line and paragraph separators (which Python's
# splitlines splits on), the word joiner and the byte order mark. The zero-width non-joiner and joiner, which scripts and
# emoji need, are not among them.
INVISIBLE = {0x200B, 0x2028, 0x2029, 0x2060, 0xFEFF}


def test_exactly_these_characters_go_the_controls_the_surrogates_the_bidirectional_controls_and_the_invisible_ones():
    everything = "".join(map(chr, range(0x110000)))
    removed = set(range(0x110000)) - {ord(c) for c in identities._UNWANTED.sub("", everything)}
    # By the Unicode database of this Python: the C0 and C1 controls (Cc), the surrogates (Cs) and the lists above.
    wanted = {cp for cp in range(0x110000) if unicodedata.category(chr(cp)) in ("Cc", "Cs") or cp in BIDI | INVISIBLE}
    assert sorted(map(hex, wanted - removed)) == []  # none that should go stays
    assert sorted(map(hex, removed - wanted)) == []  # and no other goes
    assert len(removed) == 65 + 2048 + 12 + 5
    assert all(unicodedata.category(chr(cp)) == "Cf" for cp in BIDI)  # the list is of format characters
    explicit = {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}  # the bidi algorithm's own controls
    assert {ord(c) for c in everything if unicodedata.bidirectional(c) in explicit} <= BIDI
    assert {0x200C, 0x200D}.isdisjoint(removed)  # the non-joiner and the joiner stay


def test_a_text_is_stripped_after_its_controls_go_and_cut_without_a_space_at_the_cut():
    assert clean("  Emily  ", 200) == "Emily"
    assert clean(f"{ESC} Emily {NUL}", 200) == "Emily"
    assert clean("a" * 199 + " b", 200) == "a" * 199
    assert clean("a" * 300, 200) == "a" * 200
    assert clean("", 200) == clean(f"{ESC}{NUL}", 200) == ""


def test_the_characters_a_name_is_written_with_are_kept():
    names = [
        "Zo" + chr(0xEB) + " M" + chr(0xFC) + "ller",
        "".join(map(chr, (0x5C71, 0x7530, 0x20, 0x592A, 0x90CE))),
        "".join(map(chr, (0x0645, 0x062D, 0x0645, 0x062F))),
        "Anne-Marie O'Neil",
        chr(0x1F469) + chr(0x200D) + chr(0x1F4BB),  # a joiner between two emoji is not a bidirectional control
        "".join(map(chr, (0x0645, 0x06CC, 0x200C, 0x062E, 0x0648, 0x0627, 0x0647, 0x0645))),  # Persian: a non-joiner
    ]
    for name in names:
        assert clean(name, 200) == name
