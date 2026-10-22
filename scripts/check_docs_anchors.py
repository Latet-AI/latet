"""Every link of the dashboard to the documentation lands on a page and a section that exist, in all five languages:
each target of dashboard/src/lib/docsLinks.json, with its anchor in each translation from docsAnchors.json, checked
against the built documentation. A heading renamed in one language fails this check, naming the link and the language,
before any dashboard opens a section that is not there.

uv run python scripts/check_docs_anchors.py                         # docs/dist (npm run build in docs/)
uv run python scripts/check_docs_anchors.py --root src/latet/docs   # the copy Latet serves (npm run build:bundle)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "docs" / "dist"
LINKS = ROOT / "dashboard" / "src" / "lib" / "docsLinks.json"
ANCHORS = ROOT / "dashboard" / "src" / "lib" / "docsAnchors.json"


def _sibling(name: str) -> Any:
    """A script beside this one, as a module (scripts/ is no package)."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_gen = _sibling("gen_docs_anchors")  # how pages, their files and their headings are read

LANGUAGES = ("en", *_gen.TRANSLATIONS)


def problems(site: Path, links: dict[str, dict[str, Any]], anchors: dict[str, dict[str, dict[str, str]]]) -> list[str]:
    """Each link that lands on no page, or on no section, in a language: "<target> (<lang>): why", sorted."""
    found: list[str] = []
    read: dict[Path, list[str]] = {}
    for target, link in links.items():
        page, anchor = link["page"], link.get("anchor")
        for lang in LANGUAGES:
            where = _gen.shown_page(lang, page)
            file = _gen.page_file(site, lang, page)
            if not file.is_file():
                found.append(f"{target} ({lang}): no page {where}")
                continue
            if not anchor:
                continue
            local = anchor if lang == "en" else anchors.get(lang, {}).get(page, {}).get(anchor)
            if local is None:
                found.append(
                    f"{target} ({lang}): no anchor for #{anchor} in docsAnchors.json: run scripts/gen_docs_anchors.py"
                )
                continue
            if file not in read:
                read[file] = _gen.headings(file)
            if local not in read[file]:
                found.append(f"{target} ({lang}): no #{local} on {where}")
    return sorted(found)


def _shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that the dashboard's documentation links land somewhere.")
    parser.add_argument("--root", type=Path, default=SITE, help="the built documentation (default: docs/dist)")
    parser.add_argument(
        "--links", type=Path, default=LINKS, help="the link table (default: dashboard's docsLinks.json)"
    )
    parser.add_argument(
        "--anchors", type=Path, default=ANCHORS, help="the anchors (default: dashboard's docsAnchors.json)"
    )
    args = parser.parse_args(argv)
    if not (args.root / "index.html").is_file():
        print(f"No built documentation at {_shown(args.root)}: run `npm run build` in docs/ first.")
        return 1
    links = json.loads(args.links.read_text(encoding="utf-8"))
    anchors = json.loads(args.anchors.read_text(encoding="utf-8"))
    found = problems(args.root, links, anchors)
    for problem in found:
        print(problem)
    if found:
        return 1
    print(
        f"The dashboard's {len(links)} documentation links land on a page and a section in {len(LANGUAGES)} languages."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
