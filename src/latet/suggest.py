"""``latet rules suggest``: which code-like values in a folder no rule covers, by shape.

Local only: no model, no network. Values never leave this module: each is counted (distinct values
by an HMAC with a key made for the run), checked against the check-digit algorithms and summed into
its shape (``AB-123456`` → ``AA-999999``). The report holds shapes, counts, file names, likely check
digits and the labels the values sit next to; draft recognizers get examples invented from the
shape, never taken from the files.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latet.engine.detectors import checksums
from latet.scan import shape, text_files

# Letters and digits, joined by single hyphens or underscores: AB-123456, CR12345674, INV_2024_0012.
_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-./])[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*(?![A-Za-z0-9_\-./])")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_DATE = re.compile(r"^(?:\d{4}[-_]\d{1,2}[-_]\d{1,2}|\d{1,2}[-_]\d{1,2}[-_]\d{2,4}|\d{8}T?\d*)")
_LABEL = re.compile(r"([A-Za-z][A-Za-z_\-]{2,30})\W{0,4}$")
MIN_LENGTH, MAX_LENGTH, MIN_DIGITS = 5, 48, 4
MIN_DISTINCT = 3  # a shape seen with fewer distinct values is a constant, not a format


def _ignored(token: str) -> bool:
    if not MIN_LENGTH <= len(token) <= MAX_LENGTH or sum(c.isdigit() for c in token) < MIN_DIGITS:
        return True
    if _UUID.match(token) or _DATE.match(token):
        return True
    if _HEX.match(token) and any(c.isalpha() for c in token) and len(token) >= 7:
        return True  # a hash
    # Small numbers, and Unix timestamps in seconds or milliseconds.
    return token.isdigit() and (len(token) < 6 or (len(token) in (10, 13) and token[0] == "1"))


def _header_label(text: str, line_start: int, start: int) -> str | None:
    """The column header of a CSV or TSV value (the first line of the file), as a label."""
    if line_start == 0:
        return None
    line_end = text.find("\n", start)
    line = text[line_start : len(text) if line_end == -1 else line_end]
    separator = next((s for s in ("\t", ",", ";", "|") if s in line), None)
    if separator is None:
        return None
    column = text[line_start:start].count(separator)
    cells = text[: text.find("\n")].split(separator)
    if column >= len(cells):
        return None
    cell = cells[column].strip().strip("\"'").lower()
    return cell if re.fullmatch(r"[a-z][a-z _\-]{1,30}", cell) else None


@dataclass
class ShapeStats:
    shape: str
    count: int = 0
    distinct: set[bytes] = field(default_factory=set)  # HMACs, never the values
    files: Counter[str] = field(default_factory=Counter)
    passes: Counter[str] = field(default_factory=Counter)  # checksum name → distinct values passing
    labels: Counter[str] = field(default_factory=Counter)  # label → distinct values next to it
    _labelled: set[tuple[str, bytes]] = field(default_factory=set)

    def likely_checksum(self) -> str | None:
        n = len(self.distinct)
        if n < 5:
            return None
        best = max(self.passes.items(), key=lambda p: p[1], default=None)
        return best[0] if best and best[1] >= 0.9 * n else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "count": self.count,
            "distinct": len(self.distinct),
            "files": [name for name, _ in self.files.most_common(5)],
            "file_count": len(self.files),
            "checksum": self.likely_checksum(),
            "labels": [{"word": w, "values": n} for w, n in self.labels.most_common(3) if n >= MIN_DISTINCT],
        }


def suggest(root: Path, covered: Any = None, show_values: int = 0, draft_limit: int = 0) -> dict[str, Any]:
    """Scan ``root``. ``covered(text)`` returns the spans existing rules already find (skipped).
    ``show_values`` keeps that many sample values per shape (for a person at a terminal only);
    ``draft_limit`` adds draft recognizers whose examples are invented and checked to be no real value."""
    key = secrets.token_bytes(32)
    stats: dict[str, ShapeStats] = {}
    samples: dict[str, list[str]] = {}
    files = tokens = 0
    for path, text in text_files(root):
        files += 1
        spans = list(covered(text)) if covered is not None else []
        name = os.path.relpath(path, root) if root.is_dir() else path.name
        for m in _TOKEN.finditer(text):
            token = m.group()
            tokens += 1
            if _ignored(token) or any(s.start < m.end() and m.start() < s.end for s in spans):
                continue
            form = shape(token)
            item = stats.setdefault(form, ShapeStats(form))
            digest = hmac.new(key, token.encode(), hashlib.sha256).digest()
            new = digest not in item.distinct
            item.count += 1
            item.files[name] += 1
            if new:
                item.distinct.add(digest)
                for algorithm in checksums.CHECKSUMS.values():
                    if algorithm.validate(checksums.compact(token, algorithm.reads)):
                        item.passes[algorithm.name] += 1
                if show_values and len(samples.setdefault(form, [])) < show_values:
                    samples[form].append(token)
            line_start = text.rfind("\n", 0, m.start()) + 1
            label = _LABEL.search(text[max(line_start, m.start() - 40) : m.start()])
            if label is not None:
                word = label.group(1).lower().strip("_-")
            else:
                word = _header_label(text, line_start, m.start())
            if word and (word, digest) not in item._labelled:
                item._labelled.add((word, digest))
                item.labels[word] += 1
    shapes = [s for s in stats.values() if len(s.distinct) >= MIN_DISTINCT]
    # Formats with letters first (pure numbers are mostly ids and constants), then the most varied.
    shapes.sort(key=lambda s: (not any(c.isalpha() for c in s.shape), -len(s.distinct), s.shape))
    result: dict[str, Any] = {"files": files, "tokens": tokens, "shapes": [s.as_dict() for s in shapes]}
    if show_values:
        for item in result["shapes"]:
            item["samples"] = samples.get(item["shape"], [])
    if draft_limit:
        seen = {s.shape: s.distinct for s in shapes}

        def real(form: str, value: str) -> bool:
            return hmac.new(key, value.encode(), hashlib.sha256).digest() in seen.get(form, set())

        result["drafts"] = drafts(result, draft_limit, real)
    return result


def _files(n: int) -> str:
    return "1 file" if n == 1 else f"{n} files"


def pattern_for(form: str) -> str:
    """An RE2 pattern for a shape: ``AA-999999`` → ``\\b[A-Z]{2}-\\d{6}\\b``."""
    parts = []
    for run in re.finditer(r"A+|a+|9+|.", form):
        text = run.group()
        unit = {"A": "[A-Z]", "a": "[a-z]", "9": r"\d"}.get(text[0])
        if unit is None:
            parts.append(text if text in "-_" else re.escape(text))
        else:
            parts.append(unit + (f"{{{len(text)}}}" if len(text) > 1 else ""))
    return r"\b" + "".join(parts) + r"\b"


def invented(form: str, checksum: str | None, real: Callable[[str, str], bool] | None = None, seed: int = 0) -> str:
    """A value of the shape that no file holds (``real`` tells); it passes ``checksum`` when given."""
    import random

    rng = random.Random(f"{form}|{seed}")  # noqa: S311 - examples, not secrets
    for _ in range(10_000):
        value = "".join(
            rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") if c == "A" else rng.choice("abcdefghjkmnpqrstuvwxyz") if c == "a"
            else str(rng.randrange(10)) if c == "9" else c
            for c in form
        )  # fmt: skip
        if real is not None and real(form, value):
            continue
        if checksum is None:
            return value
        algorithm = checksums.CHECKSUMS[checksum]
        if algorithm.validate(checksums.compact(value, algorithm.reads)):
            return value
    raise ValueError("no example passes the checksum")


def drafts(
    result: dict[str, Any], limit: int = 5, real: Callable[[str, str], bool] | None = None
) -> list[dict[str, Any]]:
    """Draft recognizers for the shapes with letters, with invented examples; only drafts that pass
    `rules test` (the examples, and not too broad in ordinary code) are kept."""
    from latet.rules_report import OK, outcome, report

    out = []
    candidates = (s for s in result["shapes"] if any(c.isalpha() for c in s["shape"]))
    for item in candidates:
        if len(out) >= limit:
            break
        form, checksum = item["shape"], item["checksum"]
        labels = [label["word"] for label in item["labels"]]
        good = [invented(form, checksum, real, seed=n) for n in range(2)]
        lead = f"{labels[0]} " if labels else ""
        entry: dict[str, Any] = {
            "name": f"suggested-{len(out) + 1}",
            "description": f"{item['distinct']} different values shaped {form} in {_files(item['file_count'])}: "
            "say what they are",
            "pattern": pattern_for(form),
        }
        if checksum:
            entry["checksum"] = checksum
        if labels:
            entry["context"] = {"words": labels}
        shorter = good[0][:-1]  # one character short: not the format
        entry["examples"] = {"match": [f"{lead}{v}" for v in good], "no_match": [f"{lead}{shorter}"]}
        if outcome(report([entry])) == OK:
            out.append(entry)
    return out
