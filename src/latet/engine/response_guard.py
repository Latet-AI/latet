"""Value-free response admission, independent of launch authority and its registry."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from latet.engine.escapes import uncached

_CREDENTIAL = re.compile(r"latet_lk_[A-Za-z0-9_-]{43}")


def safe_text(text: str) -> bool:
    """Screen one complete semantic unit before any cached escaped reading.

    Only the engine's existing single escaped reading is supported. Nothing is
    cached here, and the predicate neither replaces nor returns the input.
    """
    if _CREDENTIAL.search(text) is not None:
        return False
    reading = uncached(text)
    return reading is None or _CREDENTIAL.search(reading.text) is None


def latet_key(etype: str, original: str) -> bool:
    """A live launch key a vault holds (a masked tool output put it there): Latet's own credential, which a person
    gains nothing by seeing. The Vault page never reveals it nor finds it by its words, on a computer or a server
    (N-1, R-1)."""
    return etype == "LATET_LAUNCH_KEY" or not safe_text(original)


def safe_tree(value: Any) -> bool:
    """Screen complete JSON leaves and keys with a depth-bounded iterator stack."""
    stack: list[Iterator[Any]] = [iter((value,))]
    while stack:
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(item, str):
            if not safe_text(item):
                return False
        elif isinstance(item, dict):
            if len(stack) >= 64 or any(not safe_text(key) for key in item):
                return False
            stack.append(iter(item.values()))
        elif isinstance(item, list):
            if len(stack) >= 64:
                return False
            stack.append(iter(item))
    return True
