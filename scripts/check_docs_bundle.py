"""The documentation Latet serves itself under /_latet/docs/ (`npm run build:bundle` in docs/), checked before it is
shipped in the wheel and the image: it has its pages; every asset and link stays under /_latet/docs/ (but the canonical
and alternate links, which a browser does not fetch); nothing in it fetches from another host; it holds no video (a link
to watch it on docs.latet.ai stands in its place) and one size of screenshot (1440 px, not the 800 px copies); and its
zipped size is under the limit. When it passes, the hashes of its inline scripts are written to .csp.json in it, for
the daemon's security policy (``latet.api.rest``): the daemon serves no bundle without that file.

uv run python scripts/check_docs_bundle.py                         # src/latet/docs, at most 14 MB zipped
uv run python scripts/check_docs_bundle.py --root other --max-mb 15
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "src" / "latet" / "docs"
BASE = "/_latet/docs/"
CSP_FILE = ".csp.json"
MAX_MB = 14.0
MB = 1_000_000
VIDEOS = frozenset({".mp4", ".webm", ".mov", ".ogv"})
SMALL_WIDTH = 800  # the screenshots' smaller copy, which the public site keeps and the bundle does not
# The paths the daemon's docs route serves at all (``latet.api.rest.served_docs_path``, kept the same by a test): this
# script runs where Latet is not installed. A file of another name would ship as a broken page or asset.
_SERVED = re.compile(r"[A-Za-z0-9._/-]*")
_DEVICE = re.compile(r"(?i)(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?")


def served(rel: str) -> bool:
    """Whether the daemon serves the path ``rel`` of the bundle at all (by its name alone)."""
    parts = rel.split("/")
    return (
        _SERVED.fullmatch(rel) is not None
        and "" not in parts[:-1]
        and not any(part.startswith(".") for part in parts)
        and not any(_DEVICE.fullmatch(part) for part in rel.replace("\\", "/").split("/"))
    )


# What an element fetches by itself, by the attributes that name it.
_FETCHED = {
    "script": ("src",),
    "img": ("src", "srcset"),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
    "audio": ("src",),
    "track": ("src",),
    "iframe": ("src",),
    "embed": ("src",),
    "object": ("data",),
    "input": ("src",),
}
# The kinds of <link> a browser fetches, or opens a connection for.
_FETCHED_LINKS = frozenset(
    {
        "stylesheet",
        "preload",
        "modulepreload",
        "prefetch",
        "preconnect",
        "dns-prefetch",
        "icon",
        "apple-touch-icon",
        "apple-touch-icon-precomposed",
        "mask-icon",
        "manifest",
    }
)
# Links that name the public site for search engines, never fetched by a browser.
_NAMING_LINKS = frozenset({"canonical", "alternate"})
# Inline scripts that run nothing: data, not code.
_DATA_SCRIPTS = frozenset({"application/ld+json", "application/json"})
_CSS_URL = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)|@import\s+(['"])(.*?)\3""", re.IGNORECASE)
_SAME = "bundle.invalid"  # the bundle's own origin, to resolve a relative address against


def page_url(root: Path, file: Path) -> str:
    """The address a file of the bundle is served at: a folder's index.html is the folder."""
    rel = file.relative_to(root).as_posix()
    return BASE + (rel[: -len("index.html")] if file.name == "index.html" else rel)


def _where(address: str, at: str) -> tuple[str, str] | None:
    """Where ``address``, written in the file served at ``at``, leads: ("same", its path) on the bundle's origin,
    ("other", the address) on another host, None for what fetches nothing from a host (data:, a mailto: link)."""
    address = address.strip()
    if not address:
        return None
    parts = urlsplit(address)
    if parts.scheme in ("http", "https") or (not parts.scheme and address.startswith("//")):
        return ("other", address)
    if parts.scheme:
        return None
    target = urlsplit(urljoin(f"https://{_SAME}{at}", address))
    return ("same", target.path or "/")


def _css_addresses(text: str) -> list[str]:
    return [m.group(2) if m.group(2) is not None else m.group(4) for m in _CSS_URL.finditer(text)]


def _srcset(value: str) -> list[str]:
    return [candidate.split()[0] for candidate in value.split(",") if candidate.strip()]


class _Page(HTMLParser):
    """What a page fetches and links to, and the text of its inline scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fetched: list[tuple[str, str]] = []  # (what names it, address)
        self.linked: list[tuple[str, str]] = []
        self.scripts: list[str] = []
        self._script: list[str] | None = None
        self._style: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        for name in _FETCHED.get(tag, ()):
            if name in values:
                found = _srcset(values[name]) if name == "srcset" else [values[name]]
                self.fetched += [(f"<{tag} {name}>", address) for address in found]
        if tag == "link" and "href" in values:
            rels = set(values.get("rel", "").lower().split())
            if not rels & _NAMING_LINKS:
                target = self.fetched if rels & _FETCHED_LINKS else self.linked
                target.append(("<link href>", values["href"]))
        if tag == "a" and "href" in values:
            self.linked.append(("<a href>", values["href"]))
        if "style" in values:
            self.fetched += [(f"<{tag} style>", address) for address in _css_addresses(values["style"])]
        if tag == "script" and "src" not in values and values.get("type", "").lower() not in _DATA_SCRIPTS:
            self._script = []
        if tag == "style":
            self._style = []

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        if self._style is not None:
            self._style.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            # A browser reads a page's CR LF (and a CR alone) as LF before it hashes a script.
            self.scripts.append("".join(self._script).replace("\r\n", "\n").replace("\r", "\n"))
            self._script = None
        if tag == "style" and self._style is not None:
            self.fetched += [("<style>", address) for address in _css_addresses("".join(self._style))]
            self._style = None


def _parsed(file: Path) -> _Page:
    """A page as it is served, its line ends untouched (``_Page`` reads a script's as a browser does)."""
    page = _Page()
    page.feed(file.read_bytes().decode("utf-8"))
    page.close()
    return page


def _webp_width(data: bytes) -> int | None:
    """The width a WebP picture's header says (lossy, lossless or extended), None for another file."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    if kind == b"VP8 ":
        return int.from_bytes(data[26:28], "little") & 0x3FFF
    if kind == b"VP8L":
        return (int.from_bytes(data[21:25], "little") & 0x3FFF) + 1
    if kind == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1
    return None


def zipped_size(root: Path) -> int:
    """The bytes the bundle takes in a zip (a wheel is one), deflated as a wheel's files are."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(f for f in root.rglob("*") if f.is_file()):
            archive.write(file, file.relative_to(root).as_posix())
    return buffer.tell()


def _address_problems(where: str, what: str, address: str, at: str, *, fetches: bool) -> list[str]:
    found = _where(address, at)
    if found is None:
        return []
    kind, target = found
    if kind == "other":
        return [f"{where}: {what} fetches from another host: {address}"] if fetches else []
    if not target.startswith(BASE):
        return [f"{where}: {what} outside {BASE}: {target}"]
    return []


def problems(root: Path, max_mb: float = MAX_MB) -> list[str]:
    """Why the bundle at ``root`` cannot be shipped, sorted; none when it can."""
    return _checked(Path(root), max_mb)[0]


def _checked(root: Path, max_mb: float) -> tuple[list[str], int]:
    """The problems of the bundle at ``root``, sorted, and its zipped size (0 when it has no pages)."""
    if not (root / "index.html").is_file():
        return [f"no index.html in {root.as_posix()}: build it with `npm run build:bundle` in docs/"], 0
    found: list[str] = []
    for file in sorted(f for f in root.rglob("*") if f.is_file()):
        rel = file.relative_to(root).as_posix()
        at = page_url(root, file)
        suffix = file.suffix.lower()
        if rel != CSP_FILE and not served(rel):
            found.append(f"{rel}: a name the daemon does not serve")
        if suffix in VIDEOS:
            found.append(f"{rel}: a video (the bundle links to the video on docs.latet.ai instead)")
        elif suffix == ".webp" and _webp_width(file.read_bytes()[:30]) == SMALL_WIDTH:
            found.append(f"{rel}: a picture {SMALL_WIDTH} px wide (the bundle keeps the 1440 px one only)")
        elif suffix == ".css":
            for address in _css_addresses(file.read_text(encoding="utf-8")):
                found += _address_problems(rel, "url()", address, at, fetches=True)
        elif suffix == ".html":
            page = _parsed(file)
            for what, address in page.fetched:
                found += _address_problems(rel, what, address, at, fetches=True)
            for what, address in page.linked:
                found += _address_problems(rel, what, address, at, fetches=False)
    size = zipped_size(root)
    if size > max_mb * MB:
        found.append(f"the bundle is {size / MB:.2f} MB zipped, above the limit of {max_mb:g} MB")
    return sorted(set(found)), size


def inline_script_hashes(root: Path) -> list[str]:
    """The CSP hash of each distinct inline script of the bundle's pages (``sha256-...``), sorted."""
    hashes = {
        "sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
        for file in Path(root).rglob("*.html")
        for script in _parsed(file).scripts
    }
    return sorted(hashes)


def _shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the documentation bundled with Latet, and write its .csp.json.")
    parser.add_argument("--root", type=Path, default=BUNDLE, help="the bundle (default: src/latet/docs)")
    parser.add_argument(
        "--max-mb", type=float, default=MAX_MB, help=f"the most it may take zipped, in MB (default: {MAX_MB:g})"
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    csp = root / CSP_FILE
    csp.unlink(missing_ok=True)  # an earlier build's: a bundle that fails keeps none, and the daemon serves it not
    found, size = _checked(root, args.max_mb)
    for problem in found:
        print(problem)
    if found:
        print(f"The documentation bundle at {_shown(root)} cannot be shipped: {len(found)} problem(s).")
        return 1
    hashes = inline_script_hashes(root)
    csp.write_text(json.dumps({"script_hashes": hashes}, indent=2) + "\n", encoding="utf-8")
    pages = sum(1 for _ in root.rglob("*.html"))
    print(
        f"The documentation bundle at {_shown(root)}: {pages} pages, {size / MB:.2f} MB zipped "
        f"(limit {args.max_mb:g} MB), {len(hashes)} inline scripts hashed in {CSP_FILE}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
