"""Final reviewer-key text policy; callers own decoding, normalization and source selection."""

from typing import Literal

KEY_LONGEST = 4096
KEY_BYTES = 8192
KEY_REQUIREMENT = "a key must be one line of 1 to 4096 printable ASCII characters without spaces"


def key_problem(text: str) -> Literal["empty", "characters", "long"] | None:
    """A fixed rejection category, or None for nonempty 0x21-0x7E text within the key ceiling."""
    if not text:
        return "empty"
    if any(not 0x21 <= ord(char) <= 0x7E for char in text):
        return "characters"
    if len(text) > KEY_LONGEST:
        return "long"
    return None
