"""The pages that say what Latet stores of request exceptions and of history (M1) match what migration 017 stores (P2-6
F-3, ruling (c)): a server keeps an index of each person's approved exceptions in PostgreSQL, by id, kind of scope,
permanence, end and state, never content. Checked in each of the five languages, on Security and in the changelog."""

from __future__ import annotations

from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs" / "src" / "content" / "docs"
LANGUAGES = ("", "it/", "de/", "fr/", "es/")
_MIGRATION = (
    Path(__file__).resolve().parents[2] / "src" / "latet" / "server" / "migrations" / "017_request_exception_index.sql"
)


def _bullet(text: str, marker: str) -> str:
    """The list item of ``text`` that holds ``marker``, its lines joined."""
    items = [" ".join(part.split()) for part in text.split("\n- ")]
    found = [item for item in items if marker in item]
    assert found, marker
    return found[0]


# What each page names of the index, in its own words: the id, the kind of scope, permanence, the end, the state, and
# that it never keeps an exception's description (review Minor 11).
_NAMED = {
    "": ("id", "scope", "permanent", "end", "in force", "never its description, its destination"),
    "it/": ("id", "ambito", "permanente", "fine", "in vigore", "mai la sua descrizione, la sua destinazione"),
    "de/": ("ID", "Geltungsbereichs", "dauerhaft", "Ende", "in Kraft", "nie ihre Beschreibung, ihr Ziel"),
    "fr/": ("identifiant", "portée", "permanente", "fin", "en vigueur", "jamais sa description, sa destination"),
    "es/": ("id", "alcance", "permanente", "fin", "en vigor", "nunca su descripción, su destino"),
}
_CHANGELOG = {
    "": ("ids", "scope", "ends", "states", "never what one names"),
    "it/": ("id", "ambito", "fini", "stati", "mai ciò che un'eccezione indica"),
    "de/": ("IDs", "Geltungsbereichs", "Enden", "Zustände", "nie das, was eine Ausnahme nennt"),
    "fr/": ("identifiants", "portée", "fins", "états", "jamais ce qu'une exception nomme"),
    "es/": ("ids", "alcance", "fines", "estados", "nunca lo que nombra una excepción"),
}


def test_the_index_holds_what_the_pages_say_and_nothing_else():
    sql = _MIGRATION.read_text(encoding="utf-8")
    table = sql.split("CREATE TABLE", 1)[1].split("\n);", 1)[0]  # its own closing line: a comment holds ");" too
    columns = {
        line.split()[0]
        for line in table.splitlines()[1:]
        if line.strip() and not line.strip().startswith(("--", "PRIMARY", "CHECK"))
    }
    assert columns == {"identity", "exception", "scope", "permanent", "expires", "state"}


@pytest.mark.parametrize("lang", LANGUAGES)
def test_security_says_what_postgresql_keeps_of_exceptions(lang):
    item = _bullet((DOCS / lang / "security.md").read_text(encoding="utf-8"), "latet server status")
    assert "PostgreSQL" in item, f"{lang or 'en/'}security.md: the index is not named"
    for word in _NAMED[lang]:
        assert word in item, f"{lang or 'en/'}security.md: {word!r}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_changelog_says_what_postgresql_keeps_of_exceptions(lang):
    text = (DOCS / lang / "changelog.md").read_text(encoding="utf-8")
    unreleased = text.split("\n## ", 2)[1]
    item = _bullet(unreleased, "latet server status")
    assert "PostgreSQL" in item, f"{lang or 'en/'}changelog.md: the index is not named"
    for word in _CHANGELOG[lang]:
        assert word in item, f"{lang or 'en/'}changelog.md: {word!r}"
