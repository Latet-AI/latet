r"""Half of a character, and a key given twice, in JSON from outside Latet.

UTF-16, the strings of JavaScript, writes a character past the first 65,536 (most emoji) as two halves, surrogates.
A JavaScript agent that cuts a tool's output at a fixed length can cut one in two, and JSON writes the half it keeps
as an escape: `"done \ud83d"`. Python reads it as a lone surrogate, which UTF-8 cannot write: every detector and hash
that reads text in UTF-8 fails on it, and Anthropic's API refuses the body as invalid JSON. Latet reads each half as
U+FFFD, the character that stands for one that cannot be read, as JavaScript's `toWellFormed` does: one for one, so
every place in the text holds. A whole character written as its two escapes is one character once read, and stays.

An object can give a key twice: `{"token": "ghp_...", "token": "redacted"}`. Python reads the last copy, and what read
the text otherwise (a model reads all of it) can take the first, which Latet never read. ``read`` says so: such a
document never leaves as it came (``Read.repeated``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_HALF = re.compile(r"[\ud800-\udfff]")
# How a half comes into a JSON text: as an escape (`\ud83d`, `\uDE00`), or written out in a string, as UTF-8 cannot
# (no encoder writes the bytes ED A0 to ED BF) and Python reads all the same. A text with neither holds no half, and
# its document is taken as it is: looking at each string of a large body takes longer than reading it.
_ESCAPED = re.compile(r"\\u[dD][89a-fA-F]")
_ESCAPED_BYTES = re.compile(rb"\\u[dD][89a-fA-F]")
_WRITTEN_BYTES = re.compile(rb"\xed[\xa0-\xbf]")


def well_formed(text: str) -> str:
    """``text`` with each half of a character in it (a lone surrogate) as U+FFFD."""
    return _HALF.sub("\N{REPLACEMENT CHARACTER}", text)


def loads(raw: str | bytes | bytearray) -> Any:
    """``json.loads`` for JSON from outside: every string of the document, every key too, well formed."""
    return loads_mended(raw)[0]


def loads_mended(raw: str | bytes | bytearray) -> tuple[Any, bool]:
    """Like :func:`loads`, and whether the document held half of a character."""
    found = _mended(json.loads(raw), raw)
    return found.doc, found.mended


@dataclass(frozen=True, slots=True)
class Read:
    """A JSON document from outside, as Latet read it."""

    doc: Any
    mended: bool = False  # it held half of a character, each read as U+FFFD
    # An object of it gave a key twice, or two keys that are one once mended: Latet read the last copy, and the text
    # holds another it never read.
    repeated: bool = False


def read(raw: str | bytes | bytearray) -> Read:
    """``raw`` read as :func:`loads_mended` reads it, and whether an object of it gave a key twice (``Read.repeated``):
    the document was then not read one to one, and its text must not leave as it came."""
    repeated = False

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal repeated
        found = dict(items)
        if len(found) != len(items):
            repeated = True
        return found

    found = _mended(json.loads(raw, object_pairs_hook=pairs), raw)
    return Read(found.doc, found.mended, repeated or found.repeated)


def _mended(doc: Any, raw: str | bytes | bytearray) -> Read:
    """``doc`` with each half of a character in its strings and keys as U+FFFD; ``repeated`` when two keys of an object
    became one so (``"k\\ud83d"`` and ``"k\\ude00"``: the last is kept)."""
    if not _may_hold_halves(raw):
        return Read(doc)
    mended = merged = False

    def mend(text: str) -> str:
        nonlocal mended
        if _HALF.search(text) is None:
            return text
        mended = True
        return well_formed(text)

    def walk(value: Any) -> Any:
        nonlocal merged
        if isinstance(value, str):
            return mend(value)
        if isinstance(value, dict):
            out = {mend(k): walk(v) for k, v in value.items()}
            merged = merged or len(out) != len(value)
            return out
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return Read(walk(doc), mended, merged)


def _may_hold_halves(raw: str | bytes | bytearray) -> bool:
    if isinstance(raw, str):
        return _ESCAPED.search(raw) is not None or (not raw.isascii() and _HALF.search(raw) is not None)
    if json.detect_encoding(raw) not in ("utf-8", "utf-8-sig"):
        return True  # UTF-16 or UTF-32, which JSON allows and no agent sends: each string is looked at
    return _ESCAPED_BYTES.search(raw) is not None or _WRITTEN_BYTES.search(raw) is not None
