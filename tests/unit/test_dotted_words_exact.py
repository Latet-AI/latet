"""The dotted words of a text read in one pass of a compiled pattern (Task 8's fix round 2) are exactly those the loop
over each run's segments read before (``_reference`` below, 0.11's first form, kept verbatim as the oracle): every
string of a small alphabet up to six characters, random longer ones, and every reading of the bench's and the fuzz's
texts."""

import itertools
import random
import re
import sys
from pathlib import Path

import pytest

from latet.engine import anchors
from latet.engine.anchors import SEGMENTS_AT_MOST, _folded, dotted

ROOT = Path(__file__).resolve().parents[2]
_DOTTED = re.compile(r"[A-Za-z0-9._-]+")
_SEGMENT = re.compile(r"[A-Za-z0-9]+")
_NAME = re.compile(r"[A-Za-z0-9-]+")


def _reference(folded: str) -> set[str]:
    words: set[str] = set()
    local_at: dict[int, str] = {}
    for match in _DOTTED.finditer(folded):
        run = match.group()
        local_at[match.end()] = run
        segments = [(m.start(), m.end()) for m in _SEGMENT.finditer(run)]
        for i, (start, _) in enumerate(segments):
            for j in range(i + 1, min(len(segments), i + SEGMENTS_AT_MOST)):
                word = run[start : segments[j][1]]
                if len(word) >= 3:
                    words.add(word)
    at = folded.find("@")
    while at != -1:
        local, name = local_at.get(at), _NAME.match(folded, at + 1)
        domain = name.group().strip("-") if name is not None else ""
        if local is not None and domain:
            for start in [m.start() for m in _SEGMENT.finditer(local)][-SEGMENTS_AT_MOST:]:
                word = dotted(local[start:])
                if word is not None:
                    words.add(f"{word}@{domain}")
        at = folded.find("@", at + 1)
    return words


def test_every_short_string_of_the_shapes_alphabet_has_the_same_dotted_words():
    alphabet = "a9.-_@ "
    for length in range(1, 7):
        for letters in itertools.product(alphabet, repeat=length):
            text = "".join(letters)
            assert anchors._dotted_words(text) == _reference(text), text


@pytest.mark.parametrize("seed", range(4))
def test_random_long_strings_have_the_same_dotted_words(seed):
    rng = random.Random(seed)
    alphabet = "abZ09.-_@ /:%K\N{LATIN SMALL LETTER E WITH ACUTE}\N{KELVIN SIGN}\N{LATIN SMALL LETTER SHARP S}\N{NO-BREAK SPACE}x7"
    for _ in range(5000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 80)))
        for reading in (text, _folded(text)):
            assert anchors._dotted_words(reading) == _reference(reading), reading


def test_every_reading_of_the_bench_and_fuzz_texts_has_the_same_dotted_words():
    sys.path.insert(0, str(ROOT / "scripts"))
    import bench_apps
    import vault_fuzz

    texts = bench_apps.lines(2000, 1, 0.5, tokens=True) + bench_apps.documents(3, 1) + vault_fuzz.fuzz_texts(3000)
    seen = 0
    for text in texts:
        for reading in {text, _folded(text), _folded(anchors._separated(anchors._composed(text)))}:
            assert anchors._dotted_words(reading) == _reference(reading)
            seen += 1
    assert seen > 5000
