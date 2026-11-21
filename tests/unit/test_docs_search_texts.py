"""The documentation's search speaks the page's language in Italian, German and French.

Starlight hands Pagefind's search box the ``pagefind.*`` strings of the page's locale. It ships them for Spanish but
not for Italian, German or French, and Pagefind 1.5's module build never reads the page's language itself (its
``onMount`` is Svelte's server-side one, which does nothing), so those three pages showed "17 results for eccezione"
in English. Our own UI strings (``docs/src/content/i18n/``) give the ten strings Starlight passes on, each keeping
the placeholders Pagefind fills in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
I18N = ROOT / "docs" / "src" / "content" / "i18n"

# The strings Starlight's i18n schema accepts for Pagefind, each with the placeholders Pagefind replaces.
PLACEHOLDERS = {
    "pagefind.clear_search": (),
    "pagefind.load_more": (),
    "pagefind.search_label": (),
    "pagefind.filters_label": (),
    "pagefind.zero_results": ("[SEARCH_TERM]",),
    "pagefind.many_results": ("[COUNT]", "[SEARCH_TERM]"),
    "pagefind.one_result": ("[COUNT]", "[SEARCH_TERM]"),
    "pagefind.alt_search": ("[SEARCH_TERM]", "[DIFFERENT_TERM]"),
    "pagefind.search_suggestion": ("[SEARCH_TERM]",),
    "pagefind.searching": ("[SEARCH_TERM]",),
}

# English is Pagefind's own fallback; Spanish comes with Starlight.
LANGUAGES = ("it", "de", "fr")


def _strings(language: str) -> dict[str, str]:
    return json.loads((I18N / f"{language}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_search_string_is_translated(language: str) -> None:
    strings = _strings(language)
    missing = sorted(set(PLACEHOLDERS) - set(strings))
    assert not missing, f"{language}.json lacks {missing}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_search_strings_keep_their_placeholders(language: str) -> None:
    strings = _strings(language)
    for key, placeholders in PLACEHOLDERS.items():
        text = strings.get(key, "")
        assert text.strip(), f"{language}.json: {key} is empty"
        for placeholder in placeholders:
            assert text.count(placeholder) == 1, f"{language}.json: {key} must hold {placeholder} once"
        assert "[" not in text.replace("[SEARCH_TERM]", "").replace("[COUNT]", "").replace("[DIFFERENT_TERM]", ""), (
            f"{language}.json: {key} holds a placeholder Pagefind does not fill"
        )


def test_search_strings_are_not_left_in_english() -> None:
    for language in LANGUAGES:
        strings = _strings(language)
        assert "results for" not in strings.get("pagefind.many_results", "results for"), language
