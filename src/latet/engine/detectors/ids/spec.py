"""How a national ID or tax number is written, told apart from other numbers, and faked.

Each country module describes its numbers with an ``IdSpec``: a pattern for how it is written, a
validator for its check digits (or structure), the keywords that mark it, when those keywords are
required, and a generator of valid surrogates in the same layout.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from latet.engine.surrogates.layout import fill_like
from latet.engine.types import Span


class Rng(Protocol):
    """The deterministic generator surrogates are drawn from (``surrogates.generators.Drbg``)."""

    def below(self, n: int) -> int: ...

    def choice(self, seq: Sequence[str]) -> str: ...


# When a keyword near the value is required:
# - "never": the check digits (or check letter) are enough;
# - "bare": only when written without separators (the grouped layout is distinctive);
# - "code": in code and tool output, not in prose;
# - "always": the number alone looks like any other number.
Context = Literal["never", "bare", "code", "always"]


@dataclass(frozen=True)
class IdSpec:
    etype: str
    country: str  # ISO 3166-1 alpha-2
    name: str  # the number's name, as the detector reference shows it
    checked: str  # how a match is confirmed, for the reference
    shape: str  # how it is written, digits as N and letters as A (never a real number)
    pattern: str  # RE2, the whole match is the value
    validate: Callable[[str], bool]  # the matched text
    generate: Callable[[str, Rng], str]  # a valid fake in the layout of the original
    context: Context = "never"
    words: tuple[str, ...] = ()  # any case, whole words
    acronyms: tuple[str, ...] = ()  # exact case
    numeric: bool = True  # digits only: also not part of a version, a decimal or a path
    rule: str = ""
    # A detector of its own instead of pattern + validate (the Italian numbers, kept as they were).
    finder: Callable[[str, bool], Iterator[Span]] | None = None

    @property
    def rule_id(self) -> str:
        return self.rule or "pii:" + self.etype.lower().replace("_", "-")


def isolated(text: str, start: int, end: int, numeric: bool) -> bool:
    """Not part of a longer token, a decimal or an exponent (``1.38435736E+12``); a number also not
    part of a version, a range or a path. A comma is a separator (CSV), never a reason to skip."""
    # A space stands for the edges of the text ("" would be in every string below).
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    if before.isalnum() or before == "_" or after.isalnum() or after == "_":
        return False
    two_before = text[start - 2] if start > 1 else " "
    two_after = text[end + 1] if end + 1 < len(text) else " "
    if (before == "." and two_before.isdigit()) or (after in "+-." and two_after.isdigit()):
        return False
    if not numeric:
        return True
    if before in "./\\" or after in "/\\":
        return False
    return not (before in "-+" and two_before.isdigit())


def compact(value: str) -> str:
    """Letters and digits only, upper case."""
    return "".join(c for c in value if c.isalnum()).upper()


def digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def is_date(year: int, month: int, day: int) -> bool:
    try:
        datetime.date(year, month, day)
    except ValueError:
        return False
    return True


def placeholder(number: str) -> bool:
    """All one digit (000000000, 111111111): test data, not a person."""
    return len(set(number)) <= 1


def random_digits(rng: Rng, n: int) -> str:
    return "".join(str(rng.below(10)) for _ in range(n))


def laid_out(original: str, new: str) -> str:
    """``new`` (compact) in the layout and case of ``original``."""
    return fill_like(original, new)


def by_rejection(original: str, rng: Rng, valid: Callable[[str], bool], tries: int = 20_000) -> str:
    """A fake with new random digits where the original has digits (letters and separators kept)
    that passes ``valid``: for check-digit schemes with no simpler construction."""
    for _ in range(tries):
        candidate = "".join(str(rng.below(10)) if c.isdigit() else c for c in original)
        if candidate != original and valid(candidate):
            return candidate
    raise ValueError("no valid surrogate found")
