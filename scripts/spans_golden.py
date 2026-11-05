"""0.10.0's spans, frozen (the server performance plan's S9): the bench corpora and the fuzz texts, each text's spans as
the masker found them, so that every detector made faster later is checked against what 0.10.0 found, text by text.

    uv run python scripts/spans_golden.py make tests/data/spans-0.10.0.json.gz   # on 0.10.0 as released, once
    uv run python scripts/spans_golden.py check tests/data/spans-0.10.0.json.gz  # exit 1 on any difference

The corpora: 2,000 log lines (``bench_apps.lines(2000, 1, 0.5)``), the first 500 of them as JSON written in ASCII with
escapes (a line break, and a name in ``\\u`` escapes: the blanked and the decoded readings run), ten documents of about
20 KB (``bench_apps.documents(10, 1)``), and the fuzz's 5,000 texts as 0.10.0 drew them (``vault_fuzz.fuzz_texts`` of
``PIECES_0_10_0``: the pieces 0.10.1's fuzz added change the texts). Each text's spans are
``Masker.spans(text, kind)`` (log lines, JSON lines and fuzz texts as a tool's result, documents as a user's text) by
the masker of the balanced profile with no name model, over an empty vault: ``[start, end, type, class, rule]``, in the
order the masker gives them. The file holds them with the SHA-256 of the texts (all, and each corpus's)."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bench_apps
from vault_fuzz import PIECES_0_10_0, fuzz_texts

from latet import __version__
from latet.config import Config
from latet.engine.build import build_masker
from latet.engine.masker import Masker
from latet.engine.types import SlotKind
from latet.engine.vault import Vault

SHOWN = 10  # the differences ``check`` prints at most
_NAME = "Niccol\N{LATIN SMALL LETTER O WITH GRAVE} Ferri"


@dataclass(frozen=True)
class Corpus:
    name: str
    kind: SlotKind
    texts: list[str]


def corpora() -> list[Corpus]:
    """The texts the spans are kept for, in the file's order."""
    lines = bench_apps.lines(2000, 1, 0.5)
    escaped = [json.dumps({"msg": line + "\n", "who": _NAME}, ensure_ascii=True) for line in lines[:500]]
    return [
        Corpus("log lines", SlotKind.TOOL_RESULT, lines),
        Corpus("JSON lines, escaped", SlotKind.TOOL_RESULT, escaped),
        Corpus("documents 20 KB", SlotKind.USER_TEXT, bench_apps.documents(10, 1)),
        Corpus("fuzz texts", SlotKind.TOOL_RESULT, fuzz_texts(5000, PIECES_0_10_0)),
    ]


def _sha256(texts: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(list(texts), ensure_ascii=False).encode("utf-8")).hexdigest()


def digest(found: Sequence[Corpus]) -> str:
    """The SHA-256 of every text of ``found``, in order."""
    return _sha256([text for corpus in found for text in corpus.texts])


def masker() -> Masker:
    """The masker the spans are found by: the balanced profile, no name model, an empty vault (``spans`` keeps
    nothing, so each text is read alone)."""
    return build_masker(Config(profile="balanced", ner="off"), Vault(b"\x01" * 32))[0]


def spans_of(found: Sequence[Corpus], by: Masker | None = None) -> list[list[list[Any]]]:
    """Each text's spans, ``[start, end, type, class, rule]``, every corpus's texts one after the other."""
    by = by if by is not None else masker()
    out: list[list[list[Any]]] = []
    for corpus in found:
        for text in corpus.texts:
            spans, _ = by.spans(text, corpus.kind)
            out.append([[s.start, s.end, s.etype, s.eclass.name, s.rule] for s in spans])
    return out


def differences(found: Sequence[Corpus], before: Sequence[Sequence[Any]], now: Sequence[Sequence[Any]]) -> list[str]:
    """Where ``now``'s spans are not ``before``'s, a line for each text: its corpus and number, the spans 0.10.0 found
    and these do not, and those found now alone."""
    where = [(corpus.name, k, text) for corpus in found for k, text in enumerate(corpus.texts)]
    if len(before) != len(now) or len(now) != len(where):
        return [f"{len(before)} texts in the file, {len(now)} read now, {len(where)} in the corpora"]
    out: list[str] = []
    for (name, k, text), old, new in zip(where, before, now, strict=True):
        old_spans, new_spans = [tuple(s) for s in old], [tuple(s) for s in new]
        if old_spans == new_spans:
            continue
        gone = [s for s in old_spans if s not in new_spans]
        added = [s for s in new_spans if s not in old_spans]
        moved = "" if gone or added else "; the same spans in another order"
        out.append(f"{name} #{k}: 0.10.0 only {gone}, now only {added}{moved}; text {text[:100]!r}")
    return out


def load(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def make(path: Path) -> int:
    found = corpora()
    spans = spans_of(found)
    kept = {
        "latet": __version__,
        "made_by": "scripts/spans_golden.py make",
        "sha256": digest(found),
        "corpora": [
            {"name": c.name, "kind": c.kind.value, "texts": len(c.texts), "sha256": _sha256(c.texts)} for c in found
        ],
        "spans": spans,
    }
    data = json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))  # the same bytes from the same spans
    count = sum(len(s) for s in spans)
    print(f"{path}: {len(spans)} texts, {count} spans, Latet {__version__}; {path.stat().st_size / 1e3:.0f} KB")
    return 0


def check(path: Path) -> int:
    golden = load(path)
    found = corpora()
    if digest(found) != golden["sha256"]:
        print(f"the corpora are not the texts {path} was made from: they changed, not the engine")
        return 1
    spans = spans_of(found)
    differ = differences(found, golden["spans"], spans)
    for line in differ[:SHOWN]:
        print(line)
    if differ:
        print(f"spans differ in {len(differ)} of {len(spans)} texts (the first {min(SHOWN, len(differ))} above)")
        return 1
    print(f"spans differ: 0 ({len(spans)} texts, {sum(len(s) for s in spans)} spans, as Latet {golden['latet']})")
    return 0


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    commands = found.add_subparsers(dest="command", required=True)
    commands.add_parser("make", help="the spans written").add_argument("out", type=Path)
    commands.add_parser("check", help="the spans compared, exit 1 on any difference").add_argument("file", type=Path)
    return found


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return make(args.out) if args.command == "make" else check(args.file)


if __name__ == "__main__":
    sys.exit(main())
