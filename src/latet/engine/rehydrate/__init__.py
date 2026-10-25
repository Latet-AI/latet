"""Restoring originals in model output: exact surrogate matches only, streaming-safe."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from latet.engine.escapes import TAIL, cased, ends_with_escape, readings, written_forms
from latet.engine.vault import Entity, restored_whole, written_in_escapes


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
    entities = list(entities)  # read twice

    def add(pattern: str, replacement: str, e: Entity, word: bool) -> None:
        if pattern and pattern != replacement and pattern not in out:
            out[pattern] = Restore(pattern, replacement, e.id, word)

    def add_all(forms: list[tuple[str, str]], e: Entity, word: bool) -> None:
        for s_form, o_form in forms:
            if json_mode:
                add(_json_inner(s_form), _json_inner(o_form), e, word)
            else:
                add(s_form, o_form, e, word)

    for e in entities:
        add_all(
            _forms(e.etype, e.surrogate, e.original, restored_whole(e.etype, e.eclass)),
            e,
            restored_whole(e.etype, e.eclass),
        )
    # A value kept as written in escapes (`Mario%20Rossi`): the other forms a model may write its fake in, each for the
    # value exactly as written; and its fake as it reads (`Nicola Greco` for `Nicola%20Greco`, which the vault keeps
    # apart from every other value and fake), in every form a fake takes, for the value as it reads (`Mario Rossi`), as
    # 0.9.8 gave a name back (Ruling 099-R2). After every value's own forms, which they never take the place of.
    for e in entities:
        words = written_in_escapes(e)
        if words is None:
            continue  # found as it is written, or written as it reads
        word = restored_whole(e.etype, e.eclass)
        add_all([(form, e.original) for form in written_forms(e.original, e.surrogate, words=words)], e, word)
        read = readings(e.original, e.surrogate, words=words)
        if read is not None and read[0] != e.surrogate:
            add_all(_forms(e.etype, *read, word), e, word)
    return list(out.values())


def _forms(etype: str, sur: str, orig: str, word: bool) -> list[tuple[str, str]]:
    """The forms a fake ``sur`` may take in model output, each with the form of ``orig`` it stands for. Capitals and
    small letters leave escapes as they are written (``escapes.cased``)."""
    forms = [(sur, orig)]
    if etype in ("EMAIL", "DOMAIN", "HOSTNAME"):
        forms += [(cased(sur, False), cased(orig, False)), (cased(sur, True), cased(orig, True))]
    if etype in ("EMAIL", "URL_PASSWORD", "PASSWORD"):
        forms.append((urllib.parse.quote(sur, safe=""), urllib.parse.quote(orig, safe="")))
    if word:
        forms += [(cased(sur, True), cased(orig, True)), (cased(sur, False), cased(orig, False))]
        if etype == "PERSON" and " " in sur:
            for s_part, o_part in _parts(sur.split(), orig.split()):
                if len(s_part) >= 3:
                    forms.append((s_part, o_part))
    return forms


def _parts(fake: list[str], value: list[str]) -> list[tuple[str, str]]:
    """Each name of a fake person with the name of the value it stands for: one for one, or, where the fake has names
    the value has not (a wider form, ``generators.gen_person``: `Marco Luca Ferri` for `Mario Rossi`, `Marco Ferri`
    for `Mario`), its first for the first and its last for the last, the others for none."""
    if len(fake) == len(value) or not value:
        return list(zip(fake, value, strict=False))
    return [(fake[0], value[0])] + ([(fake[-1], value[-1])] if len(value) > 1 else [])


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
