"""Restoring originals in model output: exact surrogate matches only, streaming-safe."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from latet.engine.escapes import TAIL, ends_with_escape
from latet.engine.vault import Entity, restored_whole


@dataclass(frozen=True, slots=True)
class Restore:
    pattern: str
    replacement: str
    entity_id: str
    word: bool  # needs word boundaries on both sides


def _json_inner(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)[1:-1]


def restores_for(entities: Iterable[Entity], json_mode: bool = False) -> list[Restore]:
    """Every textual form a surrogate may take in model output, with its matching original form."""
    out: dict[str, Restore] = {}

    def add(pattern: str, replacement: str, e: Entity, word: bool) -> None:
        if pattern and pattern != replacement and pattern not in out:
            out[pattern] = Restore(pattern, replacement, e.id, word)

    for e in entities:
        word = restored_whole(e.etype, e.eclass)
        sur, orig = e.surrogate, e.original
        forms = [(sur, orig)]
        if e.etype in ("EMAIL", "DOMAIN", "HOSTNAME"):
            forms += [(sur.lower(), orig.lower()), (sur.upper(), orig.upper())]
        if e.etype in ("EMAIL", "URL_PASSWORD", "PASSWORD"):
            forms.append((urllib.parse.quote(sur, safe=""), urllib.parse.quote(orig, safe="")))
        if word:
            forms += [(sur.upper(), orig.upper()), (sur.lower(), orig.lower())]
            if e.etype == "PERSON" and " " in sur:
                for s_part, o_part in zip(sur.split(), orig.split(), strict=False):
                    if len(s_part) >= 3:
                        forms.append((s_part, o_part))
        for s_form, o_form in forms:
            if json_mode:
                add(_json_inner(s_form), _json_inner(o_form), e, word)
            else:
                add(s_form, o_form, e, word)
    return list(out.values())


def _is_word_char(c: str) -> bool:
    return c.isalnum() or c == "_"


class _Trie:
    __slots__ = ("first", "root")

    def __init__(self, restores: Iterable[Restore]) -> None:
        self.root: dict = {}
        for r in restores:
            node = self.root
            for ch in r.pattern:
                node = node.setdefault(ch, {})
            node[""] = r
        self.first = frozenset(self.root)


class StreamRehydrator:
    """Restores surrogates in a stream of text chunks, holding back only a possible partial match.

    Invariant: ``"".join(feed(c) for c in chunks) + flush()`` equals the static restore of the
    concatenated chunks, for every way of splitting the text.
    """

    def __init__(self, restores: Iterable[Restore], *, _admit: Callable[[str], None] | None = None) -> None:
        self._trie = _Trie(restores)
        self._admit = _admit
        self._buf = ""
        self._prev = ""  # last character already emitted, for left word boundaries
        self._read = ""  # the end of the text read so far, as written: an escape there ends a word
        self.restored: list[str] = []  # entity ids restored, in order

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    def _drain(self, final: bool) -> str:
        buf = self._buf
        n = len(buf)
        out: list[str] = []
        i = 0
        first = self._trie.first
        while i < n:
            ch = buf[i]
            if ch not in first:
                if self._admit is not None:
                    self._admit(ch)
                out.append(ch)
                i += 1
                continue
            node = self._trie.root
            j = i
            best: tuple[int, Restore] | None = None
            open_path = False
            while j < n and buf[j] in node:
                node = node[buf[j]]
                j += 1
                if "" in node:
                    best = (j, node[""])
            if j == n and len(node) > (1 if "" in node else 0):
                open_path = True  # the text so far is a prefix of a longer pattern
            if open_path and not final:
                break
            if best is not None:
                end, restore = best
                prev = out[-1][-1:] if out else self._prev
                left_ok = not restore.word or not prev or not _is_word_char(prev)
                left_ok = left_ok or ends_with_escape(self._read + buf[max(0, i - TAIL) : i])
                if end == n and restore.word and not final:
                    break  # cannot know yet whether a word character follows
                right_ok = not restore.word or end == n or not _is_word_char(buf[end])
                if left_ok and right_ok:
                    if self._admit is not None:
                        self._admit(restore.replacement)
                    out.append(restore.replacement)
                    self.restored.append(restore.entity_id)
                    i = end
                    continue
            if self._admit is not None:
                self._admit(ch)
            out.append(ch)
            i += 1
        self._buf = buf[i:]
        self._read = (self._read + buf[max(0, i - TAIL) : i])[-TAIL:]
        emitted = "".join(out)
        if emitted:
            self._prev = emitted[-1]
        return emitted


def restore_text(
    text: str, restores: Iterable[Restore], *, _admit: Callable[[str], None] | None = None
) -> tuple[str, list[str]]:
    """Static restore of a complete text. Returns the text and the restored entity ids."""
    stream = StreamRehydrator(restores, _admit=_admit)
    out = stream.feed(text) + stream.flush()
    return out, stream.restored


@dataclass(frozen=True, slots=True)
class Found:
    """A surrogate found by :class:`Annotator`: where it is in the text, and where its original lands."""

    start: int  # in the text searched
    end: int
    restored_start: int  # in the restored text
    restored_end: int
    restore: Restore


class Annotator:
    """Static restore that also reports where each surrogate was (for the dashboard).

    Same matching rules as :class:`StreamRehydrator`: ``annotate(t)[0] == restore_text(t, r)[0]``.
    """

    def __init__(self, restores: Iterable[Restore]) -> None:
        self._trie = _Trie(restores)

    def annotate(self, text: str) -> tuple[str, list[Found]]:
        root, first = self._trie.root, self._trie.first
        out: list[str] = []
        found: list[Found] = []
        size = 0  # length of the restored text so far
        i = last = 0  # ``text[last:i]`` is copied as is
        n = len(text)
        while i < n:
            if text[i] not in first:
                i += 1
                continue
            node, j, best = root, i, None
            while j < n and text[j] in node:
                node = node[text[j]]
                j += 1
                if "" in node:
                    best = (j, node[""])
            if best is not None:
                end, restore = best
                prev = text[i - 1] if last < i else (out[-1][-1:] if out else "")
                left_ok = not restore.word or not prev or not _is_word_char(prev)
                left_ok = left_ok or ends_with_escape(text[max(0, i - TAIL) : i])
                right_ok = not restore.word or end == n or not _is_word_char(text[end])
                if left_ok and right_ok:
                    if last < i:
                        out.append(text[last:i])
                        size += i - last
                    found.append(Found(i, end, size, size + len(restore.replacement), restore))
                    out.append(restore.replacement)
                    size += len(restore.replacement)
                    i = last = end
                    continue
            i += 1
        out.append(text[last:])
        return "".join(out), found
