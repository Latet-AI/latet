"""Detector interface and the per-call context detectors receive."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import re2

from latet.engine.types import SlotKind, Span


@dataclass(frozen=True, slots=True)
class DetectContext:
    kind: SlotKind = SlotKind.OTHER
    path: str | None = None  # a repository file's path, for gitleaks' path-scoped rules (no tool output sets it)
    tool: str | None = None  # the tool whose output (or input) the text is, when known
    field: str | None = None  # the JSON key path the text is the value of (a decoded leaf), when known
    # Where a tool's output came from, when its call said: the file a tool read, the web address it fetched. Read by
    # strict data mode alone (`data_files`, `data_hosts`): never a path gitleaks' list of paths to skip applies to, so
    # a secret read from node_modules or a lock file is found as any other.
    source: str | None = None


class DetectorSkipped(Exception):
    """A detector chose not to read this text (too long for it, say): reported, never logged as a failure."""


class Detector(Protocol):
    """Finds sensitive spans in a text. Detectors are pure and thread-safe."""

    id: str
    mandatory: bool  # a failure of a mandatory detector fails the masking closed

    def detect(self, text: str, ctx: DetectContext) -> Iterable[Span]: ...


def quiet_options(**settings: Any) -> re2.Options:
    """RE2's options for a pattern that is not written in the code (a person's setting, one made of the vault's values
    or of a recognizer's words), ``settings`` set on them (``case_sensitive``, ``max_mem``...): RE2 says that it cannot
    compile one by raising ``re2.error`` alone, never by writing it to the process's standard error, which is a
    server's log, past its redaction (tests/unit/test_patterns.py reads every compile for it)."""
    options = re2.Options()
    options.log_errors = False
    for name, value in settings.items():
        setattr(options, name, value)
    return options


def group(m: object, index: int) -> str:
    """A regex group as ``str`` ("" when it did not take part), whatever the binding's typing."""
    value = m.group(index)  # type: ignore[attr-defined]
    return value if isinstance(value, str) else ""


def shannon_entropy(value: str) -> float:
    """Shannon entropy in bits per character, as gitleaks computes it."""
    if not value:
        return 0.0
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in Counter(value).values())


def line_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Start and end offsets of the line(s) containing ``text[start:end]``."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    return line_start, len(text) if line_end == -1 else line_end
