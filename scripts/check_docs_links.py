"""The links between the documentation's pages: relative in the sources, so a translated page keeps them in its
language, and landing on a page and an anchor that exist in the built site, in every language.

uv run python scripts/check_docs_links.py                       # the sources, and docs/dist if it is built (CI)
uv run python scripts/check_docs_links.py --site docs/no-site   # the sources only
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "docs" / "src" / "content" / "docs"
SITE = ROOT / "docs" / "dist"

# A link written from the site's root: one "/" (two are another site), in a Markdown link or an href attribute
# (an MDX component, raw HTML), or as a hero action's `link:` in the frontmatter.
_ABSOLUTE = re.compile(r"""(?:\]\(|\bhref=["'])(/(?!/)[^\s)"']*)""")
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FRONTMATTER_LINK = re.compile(r"""^[ \t-]*link:[ \t]*["']?(/(?!/)[^\s"']*)""", re.MULTILINE)


def absolute_links(text: str) -> list[str]:
    """The targets of a Markdown or MDX page's links that start at the site's root, in the order they come."""
    found: list[tuple[int, str]] = []
    front = _FRONTMATTER.match(text)
    if front:
        offset = front.start(1)
        found += [(offset + m.start(1), m.group(1)) for m in _FRONTMATTER_LINK.finditer(front.group(1))]
    found += [(m.start(1), m.group(1)) for m in _ABSOLUTE.finditer(text)]
    return [target for _, target in sorted(found)]


class _Page(HTMLParser):
    """The ids of a built page, and where its links go."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if element_id := values.get("id"):
            self.ids.add(element_id)
        if tag == "a" and (href := (values.get("href") or "").strip()):
            self.links.append(href)


def _url(site: Path, file: Path) -> str:
    """The address a built file is served at: a folder's index.html is the folder."""
    rel = file.relative_to(site).as_posix()
    return "/" + (rel[: -len("index.html")] if file.name == "index.html" else rel)


def broken_links(site: Path) -> list[str]:
    """Every link of the built site that lands on no page, or on an anchor its page does not have, sorted:
    "<page> -> <href>: <why>". Links to other sites, mailto: and tel: are left alone, and so is the top of a page
    (#top, which browsers always find, and Starlight's #_top); an anchor on the same page must exist like any
    other; a link to a file of the site (a schema, the sitemap) only needs the file."""
    site = Path(site)
    pages: dict[str, _Page] = {}
    for file in sorted(site.rglob("*.html")):
        page = _Page()
        page.feed(file.read_text(encoding="utf-8"))
        pages[_url(site, file)] = page
    problems: set[str] = set()
    for url, page in pages.items():
        for href in page.links:
            parts = urlsplit(href)
            if parts.scheme or parts.netloc:
                continue
            target = urlsplit(urljoin("https://site" + url, href))
            path = unquote(target.path)
            if (site / path.lstrip("/")).is_file():
                continue
            key = path if path.endswith("/") else path + "/"
            fragment = unquote(target.fragment)
            if key not in pages:
                problems.add(f"{url} -> {href}: no such page")
            elif fragment and fragment.lower() not in ("top", "_top") and fragment not in pages[key].ids:
                problems.add(f"{url} -> {href}: no such anchor")
    return sorted(problems)


def _shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the links between the documentation's pages, in the sources and in the built site."
    )
    parser.add_argument(
        "--sources", type=Path, default=SOURCES, help="the pages' Markdown and MDX (default: docs/src/content/docs)"
    )
    parser.add_argument(
        "--site", type=Path, default=SITE, help="the built site, checked when it exists (default: docs/dist)"
    )
    args = parser.parse_args(argv)
    if not args.sources.is_dir():
        parser.error(f"no such folder: {args.sources}")

    sources = sorted(f for f in args.sources.rglob("*") if f.suffix in (".md", ".mdx") and f.is_file())
    problems = [
        f"{_shown(file)}: absolute link {target}: use a relative one, so translations keep it"
        for file in sources
        for target in absolute_links(file.read_text(encoding="utf-8"))
    ]
    built = args.site.is_dir()
    if built:
        problems += broken_links(args.site)

    for problem in problems:
        print(problem)
    if problems:
        return 1
    site = args.site.as_posix()
    pages = f"{sum(1 for _ in args.site.rglob('*.html'))} built pages" if built else f"no site ({site} not built)"
    print(f"Links between pages: {len(sources)} source pages and {pages} checked, nothing broken.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
