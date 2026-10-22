"""What ``latet rules test`` and the dashboard's recognizer editor report: each recognizer's state,
its examples (found, or why not), and its matches in ordinary code and, as shapes only, in yours."""

from __future__ import annotations

from pathlib import Path
from typing import Any

OK, WARNINGS, ERRORS = 0, 1, 2


def report(entries: list[Any], corpus: Path | None = None) -> list[dict[str, Any]]:
    """What ``rules test`` prints: each recognizer's state, examples, and matches in ordinary code."""
    from latet.engine.build import reserved_types
    from latet.engine.corpus import benign_sample
    from latet.engine.detectors.recognizers import compile_one
    from latet.scan import shape, text_files

    sample = benign_sample()
    reserved = reserved_types()
    files = list(text_files(corpus)) if corpus is not None else []
    names: set[str] = set()
    report = []
    for entry in entries:
        recognizer, status = compile_one(entry, reserved, None, include_off=True)
        item: dict[str, Any] = status.as_dict()
        if status.name in names:
            item["state"] = "error"
            item["errors"] = [*item["errors"], f"name: another recognizer is already called {status.name}"]
        names.add(status.name)
        if recognizer is None:
            report.append(item)
            continue
        item["type"], item["class"] = recognizer.etype, recognizer.spec.class_
        examples = []
        for text in recognizer.spec.examples.match:
            found, reason = recognizer.explain(text)
            examples.append({"text": text, "expect": "match", "ok": bool(found), "found": found, "reason": reason})
        for text in recognizer.spec.examples.no_match:
            found, reason = recognizer.explain(text)
            examples.append({"text": text, "expect": "no_match", "ok": not found, "found": found, "reason": reason})
        item["examples"] = examples
        hits = [sample[s.start : s.end] for s in recognizer.find(sample)]
        item["stdlib"] = {"matches": len(hits), "samples": hits[:5]}
        if len(hits) > 50:
            item["state"] = "error"
            item["errors"] = [*item["errors"], f"too broad: {len(hits)} matches in the standard-library sample"]
        if corpus is not None:
            shapes: dict[str, int] = {}
            with_hits = 0
            for _, text in files:  # counts and shapes only: these are the user's own files
                spans = list(recognizer.find(text))
                with_hits += bool(spans)
                for span in spans:
                    form = shape(text[span.start : span.end])
                    shapes[form] = shapes.get(form, 0) + 1
            item["corpus"] = {"files": len(files), "files_with_matches": with_hits, "shapes": shapes}
        report.append(item)
    return report


def outcome(report: list[dict[str, Any]]) -> int:
    if any(item["state"] == "error" for item in report):
        return ERRORS
    failing = any(not e["ok"] for item in report for e in item.get("examples", []))
    return WARNINGS if failing or any(item["warnings"] for item in report) else OK
