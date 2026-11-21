"""``escapes.escapes``: where the letters of a text's escapes are, found from the backslashes on."""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from latet.engine import escapes

# The pattern before 0.15.0: a backslash not itself escaped, read at every place of the text for the look behind.
_BEHIND = re.compile(r"(?<!\\)(?:\\\\)*\\([nrtbf]|u[0-9A-Fa-f]{4})")


def _places_as_they_were(text: str) -> list[tuple[int, int]]:
    return [m.span(1) for m in _BEHIND.finditer(text)] if "\\" in text else []


@given(st.text(alphabet="\\nrtbfux0aF9 ", max_size=80))
@settings(max_examples=600, deadline=None)
def test_the_places_of_escapes_are_as_they_were(text: str) -> None:
    assert escapes.escapes(text) == _places_as_they_were(text)


def test_odd_and_even_runs_of_backslashes() -> None:
    for text in ("a\\nb", "a\\\\nb", "a\\\\\\nb", "\\u00e9\\u12g", "x\\\\\\\\u00e9", "\\t\\r\\b\\f", "no escape"):
        assert escapes.escapes(text) == _places_as_they_were(text)


def test_a_long_run_of_backslashes_with_no_letter_after_it_is_read_once() -> None:
    """Each run is taken whole once: a pattern of the run and its letter was tried again from each of its backslashes,
    and a run of tens of thousands with no letter after it held the reading for minutes."""
    text = "\\" * 200_001 + "x"
    assert escapes.escapes(text) == []
    assert escapes.escapes("\\" * 200_001 + "n") == [(200_001, 200_002)]
