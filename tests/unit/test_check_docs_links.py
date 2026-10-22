"""scripts/check_docs_links.py: links between pages are relative in the sources, and land on a page and an
anchor that exist in the built site, in every language."""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_docs_links", Path(__file__).resolve().parents[2] / "scripts" / "check_docs_links.py"
)
assert _SPEC is not None and _SPEC.loader is not None
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


def test_an_absolute_link_to_a_page_is_found_in_markdown_mdx_and_frontmatter():
    text = (
        "---\nhero:\n  actions:\n    - text: Install\n      link: /getting-started/installation/\n---\n"
        'See [the dashboard](/dashboard/) and <LinkCard href="/concepts/protection-scope/" />.\n'
        "Relative [ones](../quickstart/) and [anchors](#here), and https://docs.latet.ai/config.schema.json are fine."
    )
    assert check.absolute_links(text) == [
        "/getting-started/installation/",
        "/dashboard/",
        "/concepts/protection-scope/",
    ]


def _page(root: Path, url: str, body: str) -> None:
    path = root / url.strip("/") / "index.html" if url.endswith("/") else root / url.strip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<!doctype html><html><body><main>{body}</main></body></html>", encoding="utf-8")


def test_a_link_to_a_missing_page_or_anchor_is_broken_in_any_language(tmp_path):
    _page(tmp_path, "/", '<a href="concepts/llm-review/#your-guidance">x</a>')
    _page(tmp_path, "/concepts/llm-review/", '<h2 id="your-guidance">Your guidance</h2>')
    _page(tmp_path, "/it/", '<a href="concepts/llm-review/#la-tua-guida">x</a><a href="../nowhere/">y</a>')
    _page(tmp_path, "/it/concepts/llm-review/", '<h2 id="your-guidance">Your guidance</h2>')
    assert check.broken_links(tmp_path) == [
        "/it/ -> ../nowhere/: no such page",
        "/it/ -> concepts/llm-review/#la-tua-guida: no such anchor",
    ]


def test_external_links_files_and_the_top_of_the_page_are_left_alone(tmp_path):
    _page(
        tmp_path,
        "/",
        '<a href="https://latet.ai/it/privacy/">p</a><a href="/config.schema.json">s</a><a href="#top">t</a>',
    )
    (tmp_path / "config.schema.json").write_text("{}", encoding="utf-8")
    assert check.broken_links(tmp_path) == []


def test_an_anchor_on_the_same_page_must_exist_there_too(tmp_path):
    # A translated heading changes its anchor: the page's own links to it must change with it.
    _page(
        tmp_path,
        "/it/concepts/llm-review/",
        '<h2 id="la-tua-guida">La tua guida</h2>'
        '<a href="#la-tua-guida">a</a><a href="#your-guidance">b</a><a href="#_top">c</a><a href="#Top">d</a>',
    )
    assert check.broken_links(tmp_path) == ["/it/concepts/llm-review/ -> #your-guidance: no such anchor"]
