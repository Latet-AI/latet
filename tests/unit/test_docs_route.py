"""The documentation of Latet's own version, served by the daemon at /_latet/docs/ from the copy in its package
(``npm run build:bundle``): its static files only, under that folder only, with no sign-in (it is public), and under a
security policy of its own, as strict as the dashboard's, that lets run only the bundle's own inline scripts, by hash,
and its search. Without the bundle the route answers 404 and ``/me`` says so: the dashboard's links then go to
docs.latet.ai."""

import base64
import contextlib
import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from latet.api import rest
from latet.config import Config, IdentityConfig
from latet.gateway.app import create_app
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY

API = "/_latet/api/v1"
DOCS = "/_latet/docs"
HASHES = ["sha256-" + base64.b64encode(bytes([n]) * 32).decode() for n in (1, 2)]
SECRET = "the contents of a file outside the bundle"


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def _bundle(package: Path) -> Path:
    """A package with the dashboard (``web``) and a small documentation bundle (``docs``) side by side, and a secret
    beside them: what a path leaving the bundle would reach."""
    docs = package / "docs"
    for lang, title in (("", "Dashboard guide"), ("it/", "Guida alla dashboard"), ("de/", "Dashboard-Leitfaden")):
        _write(docs / f"{lang}index.html", f"<!doctype html><title>Latet</title><h1>{lang.rstrip('/') or 'en'}</h1>")
        _write(docs / f"{lang}dashboard/index.html", f"<!doctype html><title>{title}</title><h1>{title}</h1>")
    for lang, title in (("fr/", "Guide du tableau de bord"), ("es/", "Guía del panel")):
        _write(docs / f"{lang}dashboard/index.html", f"<!doctype html><title>{title}</title><h1>{title}</h1>")
    _write(docs / "404.html", "<!doctype html><title>404</title><h1>Page not found</h1>")
    _write(docs / "_astro" / "page.Ab12Cd34.css", "body{color:black}")
    _write(docs / "_astro" / "page.Ab12Cd34.js", "console.log(1)")
    _write(docs / "_astro" / "inter.Ef56.woff2", b"wOF2")
    _write(docs / "favicon.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
    _write(docs / "pagefind" / "pagefind.js", "export const search = 1")
    _write(docs / "pagefind" / "pagefind-entry.json", '{"version":"1"}')
    _write(docs / "pagefind" / "wasm.en.pagefind", b"\x1f\x8b\x08pagefind")
    _write(docs / "pagefind" / "fragment" / "en_4c1e.pf_fragment", b"\x1f\x8b\x08fragment")
    _write(docs / "pagefind" / "index" / "en_77aa.pf_index", b"\x1f\x8b\x08index")
    _write(docs / "pagefind" / "pagefind.en_9b0c.pf_meta", b"\x1f\x8b\x08meta")
    _write(docs / "pagefind" / "search.wasm", b"\x00asm")
    _write(docs / ".csp.json", json.dumps({"script_hashes": HASHES}))
    _write(package / "web" / "index.html", "<!doctype html><div id=root></div>" + SECRET)
    _write(package / "secret.txt", SECRET)
    return docs


@pytest.fixture
def runtime(tmp_path):
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    rt.gateway.dashboard.docs_dir = _bundle(tmp_path / "latet")
    yield rt
    rt.close()


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.gateway), base_url=f"http://127.0.0.1:{runtime.gateway.port}") as found:
        yield found


def _signed_in(client, runtime):
    code = runtime.gateway.dashboard.issue_code()
    assert client.get(f"/_latet/ui/login?code={code}", follow_redirects=False).status_code == 303


def test_the_docs_route_serves_a_page_in_each_language(client):
    for path, title in (
        ("dashboard/", "Dashboard guide"),
        ("it/dashboard/", "Guida alla dashboard"),
        ("de/dashboard/", "Dashboard-Leitfaden"),
        ("fr/dashboard/", "Guide du tableau de bord"),
        ("es/dashboard/", "Guía del panel"),
    ):
        page = client.get(f"{DOCS}/{path}")
        assert page.status_code == 200, path
        assert page.headers["content-type"] == "text/html; charset=utf-8"
        assert f"<h1>{title}</h1>" in page.text and page.headers["cache-control"] == "no-cache"
    assert "<h1>en</h1>" in client.get(f"{DOCS}/").text
    assert "<h1>it</h1>" in client.get(f"{DOCS}/it/").text
    assert client.head(f"{DOCS}/it/dashboard/").status_code == 200


def test_a_page_asked_without_its_last_slash_is_sent_to_it(client):
    # Its relative links (../concepts/server/) only work from the address that ends with a slash.
    for asked, sent in ((DOCS, f"{DOCS}/"), (f"{DOCS}/it/dashboard", f"{DOCS}/it/dashboard/")):
        answer = client.get(asked, follow_redirects=False)
        assert answer.status_code == 308 and answer.headers["location"] == sent


def test_a_missing_page_is_the_bundles_own_page_not_found(client):
    for path in ("it/nowhere/", "_astro/missing.js", "_astro/"):  # a folder without its page is not listed
        answer = client.get(f"{DOCS}/{path}")
        assert answer.status_code == 404 and "<h1>Page not found</h1>" in answer.text, path
        assert "frame-ancestors 'none'" in answer.headers["content-security-policy"]


def test_the_docs_route_serves_nothing_outside_the_bundle(client, runtime, tmp_path):
    for path in (
        "%2e%2e/web/index.html",  # ../web/index.html, which a browser would have sent as /_latet/web/index.html
        "%2e%2e%2fweb%2findex.html",
        "%2E%2E%2Fsecret.txt",
        "dashboard/%2e%2e/%2e%2e/%2e%2e/secret.txt",
        "..%5cweb%5cindex.html",  # a backslash, a separator on Windows
        "%5c..%5c..%5csecret.txt",
        "it%2f..%2f..%2fsecret.txt",
        "/" + (tmp_path / "latet" / "secret.txt").as_posix(),  # an absolute path
        (tmp_path / "latet" / "secret.txt").as_posix(),
        "C:%5cWindows%5cwin.ini",
        "index.html%00.txt",
        ".csp.json",  # the bundle's own, not one of its pages
        "pagefind/.%2e/.%2e/secret.txt",
    ):
        answer = client.get(f"{DOCS}/{path}")
        assert answer.status_code == 404, path
        assert SECRET not in answer.text, path
    # And the function itself, given what a router would hand it (a client sends ../ itself resolved).
    docs = runtime.gateway.dashboard.docs_dir
    for rel in ("../web/index.html", "..\\web\\index.html", "dashboard/../../secret.txt", "./../secret.txt",
                str(tmp_path / "latet" / "secret.txt"), "//server/share/secret.txt", "a\\..\\..\\secret.txt"):  # fmt: skip
        answer = rest.docs_file(docs, rel)
        assert answer.status_code == 404, rel
        assert SECRET.encode() not in bytes(answer.body), rel


def _link_out(link: Path, target: Path) -> bool:
    """``link``, a folder that leads to ``target``: a symbolic link, or on Windows, where making one needs a privilege,
    a junction. False when this computer allows neither."""
    with contextlib.suppress(OSError):
        link.symlink_to(target, target_is_directory=True)
        return True
    if sys.platform == "win32":
        import _winapi

        with contextlib.suppress(OSError):
            _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
            return True
    return False


def test_a_link_inside_the_bundle_that_leads_out_of_it_serves_nothing(client, runtime, tmp_path):
    docs = runtime.gateway.dashboard.docs_dir
    if not _link_out(docs / "outside", tmp_path / "latet"):
        pytest.skip("this computer lets a test make neither a symbolic link nor a junction")
    assert (docs / "outside" / "secret.txt").is_file()  # the link leads where it says
    assert client.get(f"{DOCS}/dashboard/").status_code == 200
    for path in ("outside/secret.txt", "outside/web/index.html", "outside/web/"):
        answer = client.get(f"{DOCS}/{path}")
        assert answer.status_code == 404 and SECRET not in answer.text, path


def test_the_docs_route_sends_its_own_security_policy(client):
    for path in ("dashboard/", "it/dashboard/", "_astro/page.Ab12Cd34.js", "nowhere/"):
        headers = client.get(f"{DOCS}/{path}").headers
        policy = headers["content-security-policy"]
        directives = dict(part.strip().split(" ", 1) for part in policy.split(";"))
        assert directives["default-src"] == "'self'"
        assert directives["script-src"].split() == ["'self'", *(f"'{h}'" for h in HASHES), "'wasm-unsafe-eval'"]
        assert directives["frame-ancestors"] == "'none'" and directives["base-uri"] == "'none'"
        assert directives["connect-src"] == "'self'" and directives["img-src"] == "'self' data:"
        assert not {"'unsafe-inline'", "'unsafe-eval'"} & set(directives["script-src"].split())
        assert "http" not in policy and "*" not in policy  # no other host, anywhere
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"


def test_a_policy_file_that_holds_anything_but_hashes_is_never_trusted(client, runtime):
    docs = runtime.gateway.dashboard.docs_dir
    assert client.get(f"{DOCS}/dashboard/").status_code == 200
    for written in (
        {"script_hashes": [*HASHES, "'unsafe-inline'"]},
        {"script_hashes": ["https://cdn.example.com"]},
        {"script_hashes": ["sha256-AAAA; script-src *"]},
        {"script_hashes": "sha256-" + "A" * 43 + "="},
        ["sha256-" + "A" * 43 + "="],
    ):
        (docs / ".csp.json").write_text(json.dumps(written), encoding="utf-8")
        assert client.get(f"{DOCS}/dashboard/").status_code == 404, written  # a bundle without its policy is none
    (docs / ".csp.json").write_text("not json", encoding="utf-8")
    assert client.get(f"{DOCS}/dashboard/").status_code == 404


def test_the_files_have_their_types_and_hashed_assets_are_kept(client):
    types = {
        "_astro/page.Ab12Cd34.css": "text/css; charset=utf-8",
        "_astro/page.Ab12Cd34.js": "text/javascript; charset=utf-8",
        "_astro/inter.Ef56.woff2": "font/woff2",
        "favicon.svg": "image/svg+xml",
        "pagefind/pagefind.js": "text/javascript; charset=utf-8",
        "pagefind/pagefind-entry.json": "application/json",
        "pagefind/wasm.en.pagefind": "application/octet-stream",
        "pagefind/fragment/en_4c1e.pf_fragment": "application/octet-stream",
        "pagefind/index/en_77aa.pf_index": "application/octet-stream",
        "pagefind/pagefind.en_9b0c.pf_meta": "application/octet-stream",
        "pagefind/search.wasm": "application/wasm",
    }
    for path, media in types.items():
        answer = client.get(f"{DOCS}/{path}")
        assert answer.status_code == 200 and answer.headers["content-type"] == media, path
        assert "content-encoding" not in answer.headers, path  # Pagefind reads its files as they are
    assert (
        client.get(f"{DOCS}/_astro/page.Ab12Cd34.js").headers["cache-control"] == "public, max-age=31536000, immutable"
    )
    assert client.get(f"{DOCS}/pagefind/pagefind.js").headers["cache-control"] == "no-cache"


def test_no_session_is_needed(client):
    client.cookies.clear()
    assert client.get(f"{API}/me").status_code == 401  # the dashboard's API wants one
    assert client.get(f"{DOCS}/dashboard/").status_code == 200  # its documentation, public, does not


def test_me_says_whether_the_docs_are_bundled(client, runtime, tmp_path):
    _signed_in(client, runtime)
    assert client.get(f"{API}/me").json()["docs"] is True
    docs = runtime.gateway.dashboard.docs_dir
    (docs / ".csp.json").unlink()  # built, not checked: not served
    assert client.get(f"{API}/me").json()["docs"] is False
    runtime.gateway.dashboard.docs_dir = tmp_path / "not-built"
    assert client.get(f"{API}/me").json()["docs"] is False
    assert client.get(f"{DOCS}/").status_code == 404
    assert client.get(f"{DOCS}/dashboard/").status_code == 404


def test_health_says_whether_the_docs_are_bundled_to_a_page_not_signed_in(client, runtime, tmp_path):
    """The sign-in page cannot ask /me: it reads /health, public, to open this install's own docs."""
    client.cookies.clear()
    assert client.get("/_latet/health").json()["docs"] is True
    runtime.gateway.dashboard.docs_dir = tmp_path / "not-built"
    assert client.get("/_latet/health").json()["docs"] is False


def test_the_installed_package_has_its_bundle_beside_the_dashboard():
    assert Path(rest.__file__).resolve().parent.parent / "docs" == rest.DOCS_DIR
    assert rest.DOCS_DIR.parent == rest.WEB_DIR.parent


@pytest.mark.parametrize("rel", ["COM1", "aux.html", "it/nul", "con.txt/x", "LPT9", "com¹"])
def test_a_windows_device_name_is_answered_404_without_looking_it_up(tmp_path, monkeypatch, rel):
    """On Windows a device name (COM1, aux.html) opened by a lookup holds the event loop for half a second: any page
    could slow the daemon with a loop of them. The docs and the dashboard answer 404 before any lookup."""
    docs = _bundle(tmp_path)

    def looked_up(self, *args, **kwargs):
        raise AssertionError(f"{self} was looked up")

    monkeypatch.setattr(Path, "resolve", looked_up)
    assert rest.docs_file(docs, rel).status_code == 404
    assert rest.ui_file(tmp_path / "web", rel).status_code == 404
