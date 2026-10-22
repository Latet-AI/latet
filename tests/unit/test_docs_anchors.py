"""scripts/gen_docs_anchors.py and scripts/check_docs_anchors.py: each link of the dashboard to the documentation lands
on a page and a section that exist, in every language. A section's anchor in a translation is made from its translated
heading: it is paired with the English one by the heading's position on the page, which translations keep."""

import importlib.util
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load("gen_docs_anchors")
check = _load("check_docs_anchors")

PROFILES = {
    "en": [("h2", "profiles"), ("h3", "choosing-a-profile"), ("h2", "policies"), ("h2", "allow-lists")],
    "it": [("h2", "profili"), ("h3", "scegliere-un-profilo"), ("h2", "policy"), ("h2", "elenchi-consentiti")],
    "de": [("h2", "profile"), ("h3", "ein-profil-wählen"), ("h2", "richtlinien"), ("h2", "zulassungslisten")],
    "fr": [("h2", "profils"), ("h3", "choisir-un-profil"), ("h2", "politiques"), ("h2", "listes-dautorisation")],
    "es": [("h2", "perfiles"), ("h3", "elegir-un-perfil"), ("h2", "políticas"), ("h2", "listas-de-permitidos")],
}
LINKS = {
    "page.rules": {"page": "dashboard/", "anchor": None},
    "section.allow": {"page": "concepts/profiles/", "anchor": "allow-lists"},
    "section.profile": {"page": "concepts/profiles/", "anchor": "choosing-a-profile"},
}


def _page(site: Path, lang: str, page: str, headings: list[tuple[str, str]]) -> None:
    """A page as Starlight builds it: its content's headings, and Starlight's own around them."""
    folder = site / ("" if lang == "en" else lang) / page
    folder.mkdir(parents=True, exist_ok=True)
    content = "".join(
        f'<div class="sl-heading-wrapper level-{level}"><{level} id="{anchor}">{anchor}</{level}>'
        f'<a class="sl-anchor-link" href="#{anchor}">#</a></div><p>Text <a href="#x">here</a>.</p>'
        for level, anchor in headings
    )
    html = (
        '<!doctype html><html><body><nav><h2 id="starlight__on-this-page">On this page</h2></nav>'
        f'<h1 id="_top">Title</h1><div class="sl-markdown-content">{content}</div>'
        '<footer><h2 id="footer-heading">Footer</h2></footer></body></html>'
    )
    (folder / "index.html").write_bytes(html.encode("utf-8"))


def _site(root: Path, profiles: dict[str, list[tuple[str, str]]] | None = None) -> Path:
    for lang, headings in (profiles or PROFILES).items():
        _page(root, lang, "", [("h2", f"what-it-protects-{lang}")])
        _page(root, lang, "concepts/profiles/", headings)
        _page(root, lang, "dashboard/", [("h2", f"opening-it-{lang}")])
    return root


def test_the_headings_of_a_page_are_its_contents_in_order(tmp_path):
    _site(tmp_path)
    assert gen.headings(tmp_path / "concepts" / "profiles" / "index.html") == [
        "profiles",
        "choosing-a-profile",
        "policies",
        "allow-lists",
    ]


def test_anchors_are_paired_by_position(tmp_path):
    anchors, problems = gen.anchors(_site(tmp_path), LINKS)
    assert problems == []
    assert anchors == {
        "it": {
            "concepts/profiles/": {"allow-lists": "elenchi-consentiti", "choosing-a-profile": "scegliere-un-profilo"}
        },
        "de": {"concepts/profiles/": {"allow-lists": "zulassungslisten", "choosing-a-profile": "ein-profil-wählen"}},
        "fr": {
            "concepts/profiles/": {"allow-lists": "listes-dautorisation", "choosing-a-profile": "choisir-un-profil"}
        },
        "es": {"concepts/profiles/": {"allow-lists": "listas-de-permitidos", "choosing-a-profile": "elegir-un-perfil"}},
    }


def test_a_page_whose_headings_differ_in_number_is_reported_never_paired(tmp_path):
    profiles = {**PROFILES, "de": PROFILES["de"][:-1]}  # a heading the German page lacks: every pair after it is wrong
    anchors, problems = gen.anchors(_site(tmp_path, profiles), LINKS)
    assert problems == [
        "concepts/profiles/ (de): 3 headings where the English page has 4: the anchors cannot be paired by position"
    ]
    assert "de" not in anchors or "concepts/profiles/" not in anchors["de"]
    assert anchors["it"]["concepts/profiles/"]["allow-lists"] == "elenchi-consentiti"


def test_an_english_anchor_the_page_lacks_is_reported(tmp_path):
    links = {**LINKS, "section.terms": {"page": "concepts/profiles/", "anchor": "your-own-terms"}}
    _, problems = gen.anchors(_site(tmp_path), links)
    assert problems == ["section.terms: no heading #your-own-terms on concepts/profiles/ in English"]


def test_check_docs_anchors_reports_a_missing_anchor_by_link_and_language(tmp_path):
    site = _site(tmp_path)
    anchors, _ = gen.anchors(site, LINKS)
    assert check.problems(site, LINKS, anchors) == []
    # A heading renamed in the Italian page after the anchors were made: the link that lands on it is named.
    renamed = [
        (level, "elenchi-di-valori" if anchor == "elenchi-consentiti" else anchor) for level, anchor in PROFILES["it"]
    ]
    _page(site, "it", "concepts/profiles/", renamed)
    # And one renamed in English, which every language's link starts from.
    _page(site, "en", "concepts/profiles/", [*PROFILES["en"][:1], ("h3", "picking-a-profile"), *PROFILES["en"][2:]])
    assert check.problems(site, LINKS, anchors) == [
        "section.allow (it): no #elenchi-consentiti on it/concepts/profiles/",
        "section.profile (en): no #choosing-a-profile on concepts/profiles/",
    ]


def test_check_docs_anchors_reports_a_missing_page_or_translation(tmp_path):
    site = _site(tmp_path)
    anchors, _ = gen.anchors(site, LINKS)
    (site / "fr" / "dashboard" / "index.html").unlink()
    links = {**LINKS, "page.vault": {"page": "vault/", "anchor": None}}
    assert check.problems(site, links, {**anchors, "es": {}}) == [
        "page.rules (fr): no page fr/dashboard/",
        "page.vault (de): no page de/vault/",
        "page.vault (en): no page vault/",
        "page.vault (es): no page es/vault/",
        "page.vault (fr): no page fr/vault/",
        "page.vault (it): no page it/vault/",
        "section.allow (es): no anchor for #allow-lists in docsAnchors.json: run scripts/gen_docs_anchors.py",
        "section.profile (es): no anchor for #choosing-a-profile in docsAnchors.json: run scripts/gen_docs_anchors.py",
    ]


def test_gen_check_fails_on_a_stale_file(tmp_path, capsys):
    site = _site(tmp_path / "site")
    links, out = tmp_path / "docsLinks.json", tmp_path / "docsAnchors.json"
    links.write_text(json.dumps(LINKS), encoding="utf-8")
    args = ["--root", str(site), "--links", str(links), "--out", str(out)]
    assert gen.main([*args, "--check"]) == 1  # not made yet
    assert gen.main(args) == 0
    assert gen.main([*args, "--check"]) == 0
    _page(site, "es", "concepts/profiles/", [*PROFILES["es"][:-1], ("h2", "listas-permitidas")])  # renamed since
    assert gen.main([*args, "--check"]) == 1
    assert "stale" in capsys.readouterr().out
    assert (
        json.loads(out.read_text(encoding="utf-8"))["es"]["concepts/profiles/"]["allow-lists"] == "listas-de-permitidos"
    )


def test_the_file_is_written_in_a_stable_form(tmp_path):
    site = _site(tmp_path / "site")
    links, out = tmp_path / "docsLinks.json", tmp_path / "docsAnchors.json"
    links.write_text(json.dumps(LINKS), encoding="utf-8")
    assert gen.main(["--root", str(site), "--links", str(links), "--out", str(out)]) == 0
    written = out.read_bytes()
    assert written.endswith(b"}\n") and b"\r" not in written  # LF line ends, on Windows too
    assert list(json.loads(written)) == ["it", "de", "fr", "es"]
