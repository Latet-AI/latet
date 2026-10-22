"""The anchors of the dashboard's links to the documentation, in each translation: dashboard/src/lib/docsAnchors.json.

The link table (dashboard/src/lib/docsLinks.json) names each section by its page and its English anchor. A translated
page's anchors are made from its translated headings; each is paired with the English one by its heading's position on
the page (the h2 and h3 headings of its content, which translations keep in number and order), read from the built
documentation. A page whose headings differ in number from the English page's is reported, never paired.

uv run python scripts/gen_docs_anchors.py           # writes the file, from docs/dist (npm run build in docs/)
uv run python scripts/gen_docs_anchors.py --check   # exit 1 when the file is not what the built documentation says (CI)
uv run python scripts/gen_docs_anchors.py --root src/latet/docs   # from the copy Latet serves (npm run build:bundle)
"""

from __future__ import annotations

import argparse
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "docs" / "dist"
LINKS = ROOT / "dashboard" / "src" / "lib" / "docsLinks.json"
OUT = ROOT / "dashboard" / "src" / "lib" / "docsAnchors.json"
# The translations, in the order the dashboard lists its languages; English, at the documentation's root, is the
# reference.
TRANSLATIONS = ("it", "de", "fr", "es")
_HEADINGS = frozenset({"h2", "h3"})
_CONTENT = "sl-markdown-content"  # Starlight's element around a page's own text


class _Headings(HTMLParser):
    """The ids of the h2 and h3 headings of a page's content (not of Starlight's own parts), in order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self._depth = 0  # how deep in the content's element, 0 outside it

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if self._depth:
            if tag == "div":
                self._depth += 1
            if tag in _HEADINGS and (heading := values.get("id")):
                self.ids.append(heading)
        elif tag == "div" and _CONTENT in (values.get("class") or "").split():
            self._depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self._depth and tag == "div":
            self._depth -= 1


def headings(file: Path) -> list[str]:
    """The anchors of a built page's sections, in the order they come."""
    parser = _Headings()
    parser.feed(file.read_bytes().decode("utf-8"))
    parser.close()
    return parser.ids


def page_file(site: Path, lang: str, page: str) -> Path:
    """The built file of ``page`` (``concepts/profiles/``) in ``lang``: English at the root, the others under their
    code."""
    return site / ("" if lang == "en" else lang) / page / "index.html"


def shown_page(lang: str, page: str) -> str:
    return page if lang == "en" else f"{lang}/{page}"


def anchors(site: Path, links: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, dict[str, str]]], list[str]]:
    """For each translation, each page the links name with an anchor: the English anchors they name, each with the
    translation's; and what keeps an anchor from being paired, sorted."""
    wanted: dict[str, set[str]] = {}
    problems: list[str] = []
    english: dict[str, list[str]] = {}
    for target, link in sorted(links.items()):
        page, anchor = link["page"], link.get("anchor")
        if not anchor:
            continue
        wanted.setdefault(page, set()).add(anchor)
        file = page_file(site, "en", page)
        if not file.is_file():
            problems.append(f"{target}: no page {page} in English")
            continue
        found = english.setdefault(page, headings(file))
        if anchor not in found:
            problems.append(f"{target}: no heading #{anchor} on {page} in English")
    paired: dict[str, dict[str, dict[str, str]]] = {lang: {} for lang in TRANSLATIONS}
    for lang in TRANSLATIONS:
        for page in sorted(wanted):
            if page not in english:
                continue
            file = page_file(site, lang, page)
            if not file.is_file():
                problems.append(f"{shown_page(lang, page)}: no page in {lang}")
                continue
            local = headings(file)
            if len(local) != len(english[page]):
                problems.append(
                    f"{page} ({lang}): {len(local)} headings where the English page has {len(english[page])}: the "
                    "anchors cannot be paired by position"
                )
                continue
            pairs = dict(zip(english[page], local, strict=True))
            paired[lang][page] = {anchor: pairs[anchor] for anchor in sorted(wanted[page]) if anchor in pairs}
    return paired, sorted(set(problems))


def rendered(paired: dict[str, dict[str, dict[str, str]]]) -> str:
    """The file's text: the translations in the dashboard's order, the pages and anchors sorted, LF line ends."""
    return json.dumps(paired, indent=2, ensure_ascii=False) + "\n"


def _shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pair the dashboard's documentation anchors in each translation.")
    parser.add_argument("--root", type=Path, default=SITE, help="the built documentation (default: docs/dist)")
    parser.add_argument(
        "--links", type=Path, default=LINKS, help="the link table (default: dashboard's docsLinks.json)"
    )
    parser.add_argument("--out", type=Path, default=OUT, help="the anchors (default: dashboard's docsAnchors.json)")
    parser.add_argument("--check", action="store_true", help="exit 1 when the file is not what it would be written")
    args = parser.parse_args(argv)
    if not (args.root / "index.html").is_file():
        print(f"No built documentation at {_shown(args.root)}: run `npm run build` in docs/ first.")
        return 1
    links = json.loads(args.links.read_text(encoding="utf-8"))
    paired, problems = anchors(args.root, links)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    text = rendered(paired)
    current = args.out.read_bytes().decode("utf-8") if args.out.is_file() else None
    if args.check:
        if current != text:
            print(f"{_shown(args.out)} is stale: run `uv run python scripts/gen_docs_anchors.py` and commit it.")
            return 1
        print(f"{_shown(args.out)} is what the documentation's headings say.")
        return 0
    if current != text:
        args.out.write_bytes(text.encode("utf-8"))
    count = sum(len(page) for lang in paired.values() for page in lang.values())
    print(f"{_shown(args.out)}: {count} anchors in {len(TRANSLATIONS)} translations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
