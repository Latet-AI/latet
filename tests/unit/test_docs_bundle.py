"""scripts/check_docs_bundle.py: the documentation Latet serves itself, under /_latet/docs/, is checked before it is
shipped: every asset and link under that base, nothing fetched from another host, no video, one size of screenshot,
and a zipped size under its limit; the hashes of its inline scripts are written for the daemon's security policy."""

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_docs_bundle", Path(__file__).resolve().parents[2] / "scripts" / "check_docs_bundle.py"
)
assert _SPEC is not None and _SPEC.loader is not None
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)

BASE = "/_latet/docs/"
GOOD_HEAD = (
    f'<link rel="stylesheet" href="{BASE}_astro/page.css">'
    f'<link rel="shortcut icon" href="{BASE}favicon.svg" type="image/svg+xml">'
    f'<script type="module" src="{BASE}_astro/page.js"></script>'
    "<script>window.theme = 'auto';</script>"
)
GOOD_BODY = f'<a href="{BASE}dashboard/">Dashboard</a><a href="concepts/server/">Server</a>'


def _page(head: str = GOOD_HEAD, body: str = GOOD_BODY) -> str:
    return f"<!doctype html><html><head>{head}</head><body><main>{body}</main></body></html>"


def _bundle(root: Path, pages: dict[str, str] | None = None, files: dict[str, bytes] | None = None) -> Path:
    """A small bundle: ``index.html`` (a good page unless ``pages`` says otherwise) and the files named."""
    for rel, html in {"index.html": _page(), **(pages or {})}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(html.encode("utf-8"))  # its line ends as written, on Windows too
    for rel, data in {"_astro/page.css": b"body{font-family:Inter}", **(files or {})}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def _webp(width: int, height: int = 500) -> bytes:
    """The first bytes of a lossy WebP picture of ``width`` x ``height`` (what its header says, nothing drawn)."""
    frame = b"\x00\x00\x00" + b"\x9d\x01\x2a" + width.to_bytes(2, "little") + height.to_bytes(2, "little")
    chunk = b"VP8 " + len(frame).to_bytes(4, "little") + frame
    return b"RIFF" + (4 + len(chunk)).to_bytes(4, "little") + b"WEBP" + chunk


def _sha(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def test_a_good_bundle_passes(tmp_path):
    assert check.problems(_bundle(tmp_path), max_mb=14) == []


def test_a_bundle_without_its_index_is_refused(tmp_path):
    root = _bundle(tmp_path)
    (root / "index.html").unlink()
    assert any("no index.html" in problem for problem in check.problems(root, max_mb=14))
    assert any("no index.html" in problem for problem in check.problems(tmp_path / "missing", max_mb=14))


def test_a_bundle_with_an_external_script_is_refused(tmp_path):
    head = GOOD_HEAD + '<script src="https://cdn.example.com/analytics.js"></script>'
    found = check.problems(_bundle(tmp_path, {"dashboard/index.html": _page(head=head)}), max_mb=14)
    assert found == [
        "dashboard/index.html: <script src> fetches from another host: https://cdn.example.com/analytics.js"
    ]


def test_anything_that_fetches_from_another_host_is_refused(tmp_path):
    head = GOOD_HEAD + (
        '<link rel="stylesheet" href="https://fonts.example.com/inter.css">'
        '<link rel="preload" href="//cdn.example.com/font.woff2" as="font">'
        '<link rel="icon" href="https://example.com/favicon.ico">'
    )
    body = GOOD_BODY + (
        '<img src="https://example.com/a.png" alt="">'
        f'<img src="{BASE}_astro/b.webp" srcset="{BASE}_astro/b.webp 1440w, https://example.com/c.webp 800w" alt="">'
        '<picture><source srcset="https://example.com/d.avif"></picture>'
        '<video src="https://example.com/e.mp4"></video>'
        '<p style="background:url(https://example.com/f.png)">x</p>'
    )
    files = {"_astro/page.css": b"@font-face{src:url(https://fonts.example.com/inter.woff2)}"}
    found = check.problems(_bundle(tmp_path, {"index.html": _page(head, body)}, files), max_mb=14)
    for url in (
        "https://fonts.example.com/inter.css",
        "//cdn.example.com/font.woff2",
        "https://example.com/favicon.ico",
        "https://example.com/a.png",
        "https://example.com/c.webp",
        "https://example.com/d.avif",
        "https://example.com/e.mp4",
        "https://example.com/f.png",
        "https://fonts.example.com/inter.woff2",
    ):
        assert any(url in problem for problem in found), url
    assert len(found) == 9, found


def test_data_urls_and_links_to_other_sites_fetch_nothing(tmp_path):
    body = GOOD_BODY + (
        '<a href="https://github.com/Latet-AI/latet">GitHub</a><a href="https://latet.ai/privacy/">Privacy</a>'
        '<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E" alt="">'
    )
    files = {"_astro/page.css": b'a{background:url("data:image/svg+xml,%3Csvg%3E%3C/svg%3E")}'}
    assert check.problems(_bundle(tmp_path, {"index.html": _page(body=body)}, files), max_mb=14) == []


def test_a_bundle_with_a_video_is_refused(tmp_path):
    found = check.problems(_bundle(tmp_path, files={"_astro/latet-product.Ab12.mp4": b"\x00" * 64}), max_mb=14)
    assert found == ["_astro/latet-product.Ab12.mp4: a video (the bundle links to the video on docs.latet.ai instead)"]
    found = check.problems(_bundle(tmp_path / "webm", files={"_astro/latet-product.Cd34.webm": b"\x00"}), max_mb=14)
    assert found == ["_astro/latet-product.Cd34.webm: a video (the bundle links to the video on docs.latet.ai instead)"]


def test_a_file_the_daemon_would_not_serve_is_refused(tmp_path):
    """A name the daemon's route refuses (a character the build never used, a hidden file) would ship
    as a broken page or asset: the check fails on it, by the daemon's own rule. Its own .csp.json passes."""
    files = {"_astro/inter@4.Ab12.woff2": b"w", "_astro/.hidden.css": b"", "page~1.html": b"<!doctype html>"}
    found = check.problems(_bundle(tmp_path, files=files), max_mb=14)
    assert found == [
        "_astro/.hidden.css: a name the daemon does not serve",
        "_astro/inter@4.Ab12.woff2: a name the daemon does not serve",
        "page~1.html: a name the daemon does not serve",
    ]
    (tmp_path / ".csp.json").write_text('{"script_hashes": []}')
    assert not [p for p in check.problems(tmp_path, max_mb=14) if ".csp.json" in p]


def test_the_check_and_the_daemon_serve_the_same_names():
    from latet.api import rest

    for rel in ("index.html", "it/dashboard/index.html", "_astro/a-b.C1.js", "inter@4.woff2", ".x", "com1.js", "aé"):
        assert check.served(rel) == (rest.served_docs_path(rel)), rel


def test_a_screenshot_of_800_pixels_is_refused(tmp_path):
    files = {"_astro/live-light.Ab12.webp": _webp(1440), "_astro/live-light.Cd34.webp": _webp(800)}
    found = check.problems(_bundle(tmp_path, files=files), max_mb=14)
    assert found == ["_astro/live-light.Cd34.webp: a picture 800 px wide (the bundle keeps the 1440 px one only)"]


def test_a_link_outside_the_base_is_refused_but_canonical_and_alternate_pass(tmp_path):
    head = GOOD_HEAD + (
        '<link rel="canonical" href="https://docs.latet.ai/dashboard/">'
        '<link rel="alternate" hreflang="it" href="https://docs.latet.ai/it/dashboard/">'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
    )
    body = GOOD_BODY + '<a href="/dashboard/">Dashboard</a><img src="/_astro/live.webp" alt="">'
    found = check.problems(_bundle(tmp_path, {"index.html": _page(head, body)}), max_mb=14)
    assert found == [
        "index.html: <a href> outside /_latet/docs/: /dashboard/",
        "index.html: <img src> outside /_latet/docs/: /_astro/live.webp",
        "index.html: <link href> outside /_latet/docs/: /apple-touch-icon.png",
    ]


def test_a_relative_link_that_climbs_above_the_base_is_refused(tmp_path):
    # From /_latet/docs/it/concepts/: two levels up is the bundle's root, three is above it.
    page = _page(body='<a href="../../../concepts/server/">Server</a><a href="../../dashboard/">Dashboard</a>')
    found = check.problems(_bundle(tmp_path, {"it/concepts/index.html": page}), max_mb=14)
    assert found == ["it/concepts/index.html: <a href> outside /_latet/docs/: /_latet/concepts/server/"]


def test_a_css_url_outside_the_base_is_refused(tmp_path):
    files = {"_astro/page.css": f"@font-face{{src:url({BASE}_astro/a.woff2)}}a{{background:url('/x.png')}}".encode()}
    found = check.problems(_bundle(tmp_path, files=files), max_mb=14)
    assert found == ["_astro/page.css: url() outside /_latet/docs/: /x.png"]


def test_a_bundle_over_the_size_limit_is_refused(tmp_path):
    root = _bundle(tmp_path, files={"_astro/noise.bin": os.urandom(40_000)})  # random bytes do not compress
    found = check.problems(root, max_mb=0.03)
    assert len(found) == 1 and found[0].startswith("the bundle is 0.04 MB zipped, above the limit of 0.03 MB")
    assert check.problems(root, max_mb=0.05) == []


def test_the_inline_scripts_hashes_are_written(tmp_path):
    theme = "window.theme = 'auto';"
    menu = "\n\tdocument.querySelector('button').hidden = false;\n"
    other = _page(
        head=GOOD_HEAD + f'<script type="module">{menu}</script><script type="application/ld+json">{{}}</script>'
    )
    root = _bundle(tmp_path, {"dashboard/index.html": other, "it/index.html": _page()})
    assert check.main(["--root", str(root)]) == 0
    written = json.loads((root / ".csp.json").read_text(encoding="utf-8"))
    # Each script once, in a stable order; data that runs nothing (JSON-LD) is not a script.
    assert written == {"script_hashes": sorted({_sha(theme), _sha(menu)})}


def test_a_script_is_hashed_as_the_browser_reads_it(tmp_path):
    # A browser reads a CR LF of the page as LF before it hashes a script: so does the check.
    root = _bundle(tmp_path, {"index.html": _page(head="<script>\r\nlet a = 1;\r\n</script>")})
    assert check.inline_script_hashes(root) == [_sha("\nlet a = 1;\n")]


def test_a_bundle_refused_keeps_no_hashes(tmp_path, capsys):
    root = _bundle(tmp_path, files={"_astro/latet-product.mp4": b"\x00"})
    (root / ".csp.json").write_text('{"script_hashes": []}', encoding="utf-8")  # of an earlier build
    assert check.main(["--root", str(root)]) == 1
    assert not (root / ".csp.json").exists()  # the daemon serves no bundle that failed its check
    assert "a video" in capsys.readouterr().out
