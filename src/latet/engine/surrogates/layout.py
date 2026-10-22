"""Keeping a value's layout: the national ID registry and the surrogate generators share it."""

from __future__ import annotations


def fill_like(original: str, new: str) -> str:
    """The letters and digits of ``new`` in the layout of ``original``: same separators, same case.

    ``fill_like("de 123.456.789", "DE987654321")`` gives ``"de 987.654.321"``.
    """
    chars = iter(new)
    out = []
    for c in original:
        if not c.isalnum():
            out.append(c)
            continue
        n = next(chars, None)
        if n is None:
            break
        out.append(n.lower() if c.islower() else n.upper() if c.isupper() else n)
    return "".join(out) + "".join(chars)
