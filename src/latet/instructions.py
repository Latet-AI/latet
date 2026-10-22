"""The instruction files Claude Code loads (CLAUDE.md), checked for values that never go to its auto
mode reviewer. Claude Code gives the reviewer these files as they are on disk, where the plugin cannot
mask them, so a review carrying one of those values is stopped: better to know before it happens."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from latet.engine.detectors.base import DetectContext
from latet.engine.masker import Masker
from latet.engine.merge import merge_spans
from latet.engine.types import EntityClass, SlotKind

# What stops a review whatever auto_mode_review lets through: secrets, your terms, other people's data.
NEVER_TO_REVIEWER = frozenset({EntityClass.SECRET, EntityClass.CUSTOM, EntityClass.PII})
_NAMES = ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md"))
_LARGEST = 1_000_000  # bytes: a larger file is not an instruction file anyone wrote


@dataclass(frozen=True, slots=True)
class Finding:
    """A value in an instruction file: where it is and what it is, never the value itself."""

    path: Path
    line: int
    etype: str
    length: int


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def instruction_files(cwd: Path) -> list[Path]:
    """The CLAUDE.md files Claude Code loads in ``cwd``: yours, and the project's from the folder up."""
    candidates = [claude_dir() / "CLAUDE.md"]
    try:
        folder = cwd.resolve()
    except OSError:
        return []
    home = Path.home()
    for parent in (folder, *folder.parents):
        # In your home folder, .claude/CLAUDE.md is your own file, read above (or replaced by CLAUDE_CONFIG_DIR).
        candidates += [parent / name for name in _NAMES if not (parent == home and name == _NAMES[2])]
    seen: set[Path] = set()
    out: list[Path] = []
    for path in candidates:
        try:
            real = path.resolve()
            if real not in seen and path.is_file() and path.stat().st_size <= _LARGEST:
                seen.add(real)
                out.append(path)
        except OSError:
            continue
    return out


def findings(files: Iterable[Path], masker: Masker) -> list[Finding]:
    """The values in ``files`` that never go to auto mode's reviewer, one per value found."""
    out: list[Finding] = []
    for path in files:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        spans = masker.detect(text, DetectContext(SlotKind.SYSTEM), skip=("ner",))
        for span in merge_spans(spans, text):
            value = text[span.start : span.end]
            if span.eclass in NEVER_TO_REVIEWER and not masker.allow.allows(value):
                out.append(Finding(path, text.count("\n", 0, span.start) + 1, span.etype, len(value)))
    return out


def describe(found: Iterable[Finding]) -> list[str]:
    """One line per file: how many values, their types and lines. Never the values."""
    by_file: dict[Path, list[Finding]] = {}
    for f in found:
        by_file.setdefault(f.path, []).append(f)
    lines = []
    for path, items in by_file.items():
        types: dict[str, list[int]] = {}
        for f in items:
            types.setdefault(f.etype, []).append(f.line)
        where = "; ".join(f"{t} at line{'s' if len(n) > 1 else ''} {', '.join(map(str, n))}" for t, n in types.items())
        count = f"{len(items)} value{'s' if len(items) != 1 else ''}"
        lines.append(
            f"{path}: {count} that never go to auto mode's reviewer ({where}). Claude Code gives the reviewer "
            "this file unmasked, so Latet stops its reviews: move them to .env and name the variable here"
        )
    return lines
