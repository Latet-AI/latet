"""What the values of one document share, read once for all of them.

A tool's JSON output is masked value by value, each with the path of its key (``Mediator._mask_new``), and what is read
in that path costs in proportion to the key's length: the field it names, the words of that name, its digest in the
key of the cache of masked texts. A long key over thousands of values was read again for each, and the masking of one
output grew with the square of its size (45 ms a value for a key of 200 KB). While a document is masked (``reading``),
each of them is read once (``once``) and remembered until the document is done: nothing of it outlives the document,
and another thread's document is its own (a context variable)."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

_READ: ContextVar[dict[Hashable, Any] | None] = ContextVar("latet_document", default=None)


@contextmanager
def reading() -> Iterator[None]:
    """A document is masked: what ``once`` reads is remembered until it is done."""
    token = _READ.set({})
    try:
        yield
    finally:
        _READ.reset(token)


def once[T](key: Hashable, read: Callable[[], T]) -> T:
    """``read()``, once for ``key`` while a document is masked (``reading``); at each call otherwise. ``key`` holds the
    string read (a path, a field), which is the same object for every value of the document: its hash is made once."""
    memo = _READ.get()
    if memo is None:
        return read()
    if key in memo:
        return cast(T, memo[key])
    found = memo[key] = read()
    return found
