"""The numbers of the applications page's "How fast it is" (the server-performance plan's Task 12, Ruling T12-S5): the
same tables in the five languages, row by row and column by column, each value with its unit, read with each
language's own separators; the same numbers in the section's text; and the English tables and text equal to the
accepted measurements, so that five pages agreeing on stale numbers fail as well.

Each column is known by its header in each language (``COLUMNS``), so that a translation whose mask and unmask columns
are swapped, headers and all, fails as surely as one whose values are swapped under the right headers. A row is known by
its place and the numbers of its first cell (a vault's size, a count of workers). A cell's numbers keep their unit
(``ms``, ``MB``; French writes ``Mo``), and a lines-a-second column has its unit in its header.

The negative controls run the same check on copies of the pages, each changed one way: a table missing, a value
changed, the directions swapped, a unit drifted, a separator read as another language's, a number of the text changed
in one translation, and the five pages agreeing on the numbers of Latet 0.10.0.

The same measurements repeated on other pages (Server operations, the server's concepts, the changelogs' changes not
released yet, the applications page outside this section, README.md) are held as well (the final review's M-6): each
page's numbers equal in the five languages, and each measurement it repeats, named in ``MEASURED`` from the same
evidence (and two limits from the code that sets them), among its English numbers; with negative controls of their
own."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "src" / "content" / "docs"
PAGE = "applications/index.md"
LANGUAGES = ("en", "it", "de", "fr", "es")
SECTION = {
    "en": "How fast it is",
    "it": "Quanto è veloce",
    "de": "Wie schnell es ist",
    "fr": "À quelle vitesse",
    "es": "Velocidad",
}

# Each table's columns by what they mean, as each language heads them, in order.
COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "instance": {
        "en": ("The key's vault", "Mask, lines a second", "Unmask, lines a second", "A document, mask / unmask"),
        "it": (
            "La cassaforte della chiave",
            "Mascheramento, righe al secondo",
            "Ripristino, righe al secondo",
            "Un documento, mascheramento / ripristino",
        ),
        "de": (
            "Der Tresor des Schlüssels",
            "Maskieren, Zeilen pro Sekunde",
            "Wiederherstellen, Zeilen pro Sekunde",
            "Ein Dokument, maskieren / wiederherstellen",
        ),
        "fr": (
            "Le coffre-fort de la clé",
            "Masquage, lignes par seconde",
            "Restauration, lignes par seconde",
            "Un document, masquage / restauration",
        ),
        "es": (
            "La caja fuerte de la clave",
            "Enmascarar, líneas por segundo",
            "Restaurar, líneas por segundo",
            "Un documento, enmascarar / restaurar",
        ),
    },
    "workers": {
        "en": ("Workers", "Mask, lines a second", "Unmask, lines a second", "Memory a worker"),
        "it": ("Worker", "Mascheramento, righe al secondo", "Ripristino, righe al secondo", "Memoria per worker"),
        "de": (
            "Worker",
            "Maskieren, Zeilen pro Sekunde",
            "Wiederherstellen, Zeilen pro Sekunde",
            "Speicher pro Worker",
        ),
        "fr": ("Workers", "Masquage, lignes par seconde", "Restauration, lignes par seconde", "Mémoire par worker"),
        "es": ("Workers", "Enmascarar, líneas por segundo", "Restaurar, líneas por segundo", "Memoria por worker"),
    },
}
# What each column is, in the same order as the headers above.
MEANING = {
    "instance": ("vault", "mask", "unmask", "document"),
    "workers": ("workers", "mask", "unmask", "memory"),
}
TABLES = tuple(COLUMNS)

# The accepted measurements, by table, row (the numbers of its first cell) and column: (numbers, unit).
EVIDENCE: dict[str, list[tuple[tuple[float, ...], dict[str, tuple[tuple[float, ...], str | None]]]]] = {
    # Task 13 Step 2, campaign 47be52951b80 on server-perf 57aac432 (the request-coverage merge): `bench_apps_server.py
    # serve --working 5000 --held 1000 --wait-index [--new-key] -- --lines 40000 --batch 500 --documents 50 --repeat
    # 0.5 --seed 1` on the i9-10850K, Windows 10 Pro, PostgreSQL 18.6 in Docker Desktop, one instance. The judged runs:
    # 200k and 1M on fresh populations (R, Task 8's order: t13-serve-200000-r2, t13-serve-1000000-r3), a new key's first
    # serve (P, t13-serve-new: its R overlapped another session's tests). Each row is its runner folder's output.log
    # (the stdout the evidence's sha256 names). Lines a second: the lines over the sum of their calls' seconds; a
    # document: the p50 of its 50 calls.
    "instance": [
        ((), {"mask": ((191,), None), "unmask": ((2894,), None), "document": ((126, 31), "ms")}),
        ((200_000,), {"mask": ((871,), None), "unmask": ((2960,), None), "document": ((117, 32), "ms")}),
        ((1_000_000,), {"mask": ((773,), None), "unmask": ((2967,), None), "document": ((124, 32), "ms")}),
    ],
    # The same campaign, t13-linux-workers (one run, judged): `bench_apps_server.py workers --counts 1 2 4 8` with its
    # defaults (200,000 distinct lines of seed 1, 62 a call, four connections a worker, 20 s phases) in Docker on Linux
    # (WSL2, 20 CPUs, 16,374,960 kB; PostgreSQL 18.6, Valkey 9.1.2); medians of three rounds; memory: each worker's
    # resident memory as its beat says it, after the phases.
    "workers": [
        ((1,), {"mask": ((1190,), None), "unmask": ((1947,), None), "memory": ((227,), "MB")}),
        ((2,), {"mask": ((2260,), None), "unmask": ((3711,), None), "memory": ((227,), "MB")}),
        ((4,), {"mask": ((4253,), None), "unmask": ((6594,), None), "memory": ((216,), "MB")}),
        ((8,), {"mask": ((7477,), None), "unmask": ((10147,), None), "memory": ((185,), "MB")}),
    ],
}

# Every number of the section's English text, outside its tables, its code, its links and the names of versions and
# products (``_plain``), with where it comes from.
TEXT = Counter(
    [
        # The first table's setting and workload (bench_apps.py's lines and documents, as in 0.10.0's table).
        64,  # GB of the measuring computer
        40_000,  # log lines (`--lines 40000`)
        200,  # characters a line
        500,  # lines a call (`--batch 500`)
        50,  # documents (`--documents 50`)
        20,  # KB a document
        50,  # a document's time: the median of its 50 calls
        # The documents again, their values known: t13-documents-again-200000 and -1000000, campaign 47be52951b80
        # (p50: mask 16.6 / 16.4 ms, unmask 23.0 / 23.0 ms).
        50,
        16.6,
        200_000,
        16.4,
        1_000_000,
        23,
        # Latet 0.10.0 on the same computer, the ledger's "0.10.0 on this machine" (Task 1, T1-C4).
        350,
        304,
        200_000,
        1_000_000,
        153,
        2114,
        2014,
        163,
        182,
        # The plan's absolute targets (S11, the targets' table), kept, and what campaign 47be52951b80 reached (R).
        1000,
        200_000,
        1_000_000,
        30,
        871,
        773,
        30,
        # The index of prints at 1,000,000 values, campaign 47be52951b80: retained 14.77 MB a million; opening to
        # background ready, three fresh trials (t13-opening-ready-0..2), 1.621 / 1.500 / 1.416 s, median 1.5; counter
        # read p50 0.51 ms, catch-up of 1 / 100 values p50 1.87 / 2.11 ms (t13-ready-v1m-r3). The index off at 200k,
        # not run again: campaign 042b63d29de6 (task8-serve-index-off-200000, c95b7ab4), mask 494, unmask 1,382.
        15,
        14.77,
        1_000_000,
        1.5,
        1_000_000,
        1.42,
        1.62,
        200_000,
        494,
        1382,
        0.51,
        1.87,
        2.11,
        100,
        # Mostly cached 500 + 1, campaign 47be52951b80 (t13-cached-pairs): median 167.6 ms against one alone, median
        # of the windows' means 6.06 ms, three pairs.
        500,
        168,
        6.1,
        # The workers' measurement, campaign 47be52951b80 (the container's CPUs and memory, the bench's defaults).
        20,  # processors
        16,  # GB
        200_000,  # values of the key
        200_000,  # distinct lines
        62,  # lines a call
        20,  # seconds a count
        # Its verdicts: the medians' ratios x6.28 mask (7,477 / 1,190) and x5.21 unmask (10,147 / 1,947) against the
        # plan's x5, the lowest round of eight workers' unmask 5.07, a worker's connections at eight workers from 1 to
        # 10 of the 32, the supervisor 47-48 MB.
        6.28,
        5.21,
        5,
        5.07,
        1,
        10,
        32,
        47,
        48,
        # database_pool's default (settings.py).
        10,
    ]
)

# Numbers in a language: its thousands separator between groups of three digits, its decimal mark.
_NUMBER = {
    "en": r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?",
    "it": r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?",
    "de": r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?",
    "es": r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?",
    "fr": r"\d{1,3}(?:[ \N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}]\d{3})+(?:,\d+)?|\d+(?:,\d+)?",
}
_UNITS = {"ms": "ms", "s": "s", "MB": "MB", "Mo": "MB", "GB": "GB", "Go": "GB", "KB": "KB", "Ko": "KB"}
_UNIT = re.compile(r"(?<![\w])(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")(?![\w])")
# What is not a measured number: code, a link's target, the names of versions and products, and IPv4 or IPv6; a
# version written alone (Latet's 0.10, 0.11 and 0.10.4, Codex's 0.115, Linux 5.14); and, in the four languages whose
# decimal mark is a comma, any number with a point and one or two digits after it, which is a version there (German's
# "Valkey ab 7.2"), where a point and three digits group thousands.
_NOT_NUMBERS = (
    re.compile(r"`[^`]*`"),
    re.compile(r"\]\([^)]*\)"),
    re.compile(r"\b(?:Latet|PostgreSQL|Valkey|Windows|Python|Linux)\s+\d+(?:\.\d+)*"),
    re.compile(r"\bi\d-\d+K\b"),
    re.compile(r"\bIPv\d\b"),
    re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{1,2}\b"),
    re.compile(r"\b0\.1[01]\b"),
    re.compile(r"\b0\.\d{3}\b"),
)
_COMMA_VERSION = re.compile(r"\b\d+\.\d{1,2}\b")


def _value(token: str, lang: str) -> float:
    """A number as ``lang`` writes it, as a float."""
    if lang == "en":
        return float(token.replace(",", ""))
    if lang == "fr":
        return float(re.sub(r"[ \N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}]", "", token).replace(",", "."))
    return float(token.replace(".", "").replace(",", "."))


def numbers(text: str, lang: str) -> list[float]:
    return [_value(m.group(0), lang) for m in re.finditer(_NUMBER[lang], text)]


def _plain(text: str, lang: str = "en") -> str:
    for pattern in _NOT_NUMBERS:
        text = pattern.sub(" ", text)
    return text if lang == "en" else _COMMA_VERSION.sub(" ", text)


def section(page: str, lang: str) -> str | None:
    """The page's "How fast it is", from its heading to the next of its level; None when there is none."""
    lines = page.splitlines()
    heading = f"## {SECTION[lang]}"
    try:
        start = lines.index(heading)
    except ValueError:
        return None
    end = next((n for n in range(start + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end])


def _tables(text: str) -> tuple[list[list[list[str]]], str]:
    """The section's tables (each its rows of cells, the header first, the separator left out), and its text without
    them."""
    tables: list[list[list[str]]] = []
    rest: list[str] = []
    current: list[list[str]] | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if current is None:
                current = []
                tables.append(current)
            if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                current.append(cells)
        else:
            current = None
            rest.append(line)
    return tables, "\n".join(rest)


def _cell(cell: str, lang: str) -> tuple[tuple[float, ...], str | None]:
    found = _UNIT.findall(cell)
    return tuple(numbers(_UNIT.sub(" ", cell), lang)), (_UNITS[found[-1]] if found else None)


def read(
    page: str, lang: str
) -> tuple[dict[str, list[tuple[tuple[float, ...], dict[str, tuple]]]], Counter, list[str]]:
    """``lang``'s "How fast it is": its tables by name, each row as (the numbers of its first cell, {column: (numbers,
    unit)}); the numbers of its text; and what is wrong with its shape."""
    wrong: list[str] = []
    text = section(page, lang)
    if text is None:
        return {}, Counter(), [f"{lang}: no section '{SECTION[lang]}'"]
    found, rest = _tables(text)
    if len(found) != len(TABLES):
        wrong.append(f"{lang}: {len(found)} tables in '{SECTION[lang]}', not {len(TABLES)}")
    tables: dict[str, list[tuple[tuple[float, ...], dict[str, tuple]]]] = {}
    for name, rows in zip(TABLES, found, strict=False):
        header, body = rows[0], rows[1:]
        if tuple(header) != COLUMNS[name][lang]:
            wrong.append(f"{lang}: table '{name}' is headed {header}, not {list(COLUMNS[name][lang])}")
            continue
        meaning = MEANING[name]
        tables[name] = [
            (tuple(numbers(row[0], lang)), {meaning[n]: _cell(row[n], lang) for n in range(1, len(meaning))})
            for row in body
            if len(row) == len(meaning)
        ]
        if any(len(row) != len(meaning) for row in body):
            wrong.append(f"{lang}: table '{name}' has a row of another number of cells")
    return tables, Counter(numbers(_plain(rest, lang), lang)), wrong


def problems(pages: dict[str, str]) -> list[str]:
    """What is wrong with the five pages' "How fast it is": a section or a table missing or headed otherwise, a row, a
    value or a unit that differs from English, a number of the text that differs from English's, and the English
    tables or text that are not the accepted measurements."""
    found: list[str] = []
    read_ = {lang: read(pages[lang], lang) for lang in LANGUAGES}
    for lang in LANGUAGES:
        found.extend(read_[lang][2])
    english, english_text, _ = read_["en"]
    for name in TABLES:
        if english.get(name) != EVIDENCE[name]:
            found.append(f"en: table '{name}' is {english.get(name)}, not the accepted {EVIDENCE[name]}")
    if english_text != TEXT:
        more, fewer = english_text - TEXT, TEXT - english_text
        found.append(f"en: the text's numbers differ from the accepted ones: more {dict(more)}, fewer {dict(fewer)}")
    for lang in LANGUAGES[1:]:
        tables, text, _ = read_[lang]
        for name in TABLES:
            if name in tables and name in english and tables[name] != english[name]:
                found.append(f"{lang}: table '{name}' is {tables[name]}, English {english[name]}")
        if read_[lang][0] and text != english_text:
            more, fewer = text - english_text, english_text - text
            found.append(f"{lang}: the text's numbers differ from English: more {dict(more)}, fewer {dict(fewer)}")
    return found


def _pages() -> dict[str, str]:
    return {
        lang: (DOCS / PAGE if lang == "en" else DOCS / lang / PAGE).read_text(encoding="utf-8") for lang in LANGUAGES
    }


def test_the_numbers_are_the_same_in_every_language():
    assert problems(_pages()) == []


def test_numbers_are_read_with_each_languages_separators():
    assert numbers("1,000,000 and 14.77 and 3,030", "en") == [1_000_000, 14.77, 3030]
    assert numbers("1.000.000 e 14,77 e 3.030", "it") == [1_000_000, 14.77, 3030]
    assert numbers("1 000 000 et 14,77 et 3\N{NARROW NO-BREAK SPACE}030", "fr") == [1_000_000, 14.77, 3030]
    assert _cell("119 / 33 ms", "en") == ((119, 33), "ms")
    assert _cell("223 Mo", "fr") == ((223,), "MB")
    assert (
        numbers(_plain("Latet 0.10.0 on PostgreSQL 18.6, `metrics_port + 1`, i9-10850K, IPv4, Windows 10"), "it") == []
    )


# --- negative controls: the same check on pages changed one way each -----------------------------------------------


def _changed(lang: str, old: str, new: str, count: int = 1) -> dict[str, str]:
    pages = _pages()
    assert pages[lang].count(old) >= count, f"{lang} has no {old!r} to change"
    pages[lang] = pages[lang].replace(old, new, count)
    return pages


def _table_lines(page: str, lang: str, name: str) -> list[str]:
    """The lines of a table of the page's section, its header first."""
    text = section(page, lang) or ""
    lines, inside = [], False
    for line in text.splitlines():
        header = (
            line.strip().startswith("|")
            and tuple(c.strip() for c in line.strip().strip("|").split("|")) == COLUMNS[name][lang]
        )
        inside = inside or header
        if inside and not line.strip().startswith("|"):
            break
        if inside:
            lines.append(line)
    return lines


def _found(pages: dict[str, str], *words: str) -> list[str]:
    found = problems(pages)
    assert found, "the check found nothing wrong"
    assert any(all(w in p for w in words) for p in found), found
    return found


def test_a_missing_table_is_found():
    pages = _pages()
    table = "\n".join(_table_lines(pages["de"], "de", "workers"))
    pages["de"] = pages["de"].replace(table + "\n", "")
    _found(pages, "de:", "1 tables")


def test_a_changed_value_is_found():
    _found(_changed("it", "| 1.947 |", "| 1.974 |"), "it:", "workers")


def test_swapped_directions_are_found():
    pages = _pages()
    for name in TABLES:  # French: mask and unmask swapped, headers and all
        for line in _table_lines(pages["fr"], "fr", name):
            cells = line.strip().strip("|").split("|")
            cells[1], cells[2] = cells[2], cells[1]
            pages["fr"] = pages["fr"].replace(line, "|" + "|".join(cells) + "|")
    _found(pages, "fr:", "headed")
    pages = _pages()
    for line in _table_lines(pages["es"], "es", "instance")[2:]:  # Spanish: the values alone swapped
        cells = line.strip().strip("|").split("|")
        cells[1], cells[2] = cells[2], cells[1]
        pages["es"] = pages["es"].replace(line, "|" + "|".join(cells) + "|")
    _found(pages, "es:", "instance")


def test_a_drifted_unit_is_found():
    _found(_changed("es", "126 / 31 ms", "126 / 31 s"), "es:", "instance")
    _found(_changed("de", "| 185 MB |", "| 185 GB |"), "de:", "workers")


def test_another_languages_separator_is_found():
    _found(_changed("en", "| 2,960 |", "| 2.960 |"), "en:", "instance")
    _found(_changed("it", "| 2.960 |", "| 2,960 |"), "it:", "instance")


def test_a_changed_number_of_the_text_is_found():
    _found(_changed("fr", "14,77", "14,7"), "fr:", "text")


def test_five_pages_agreeing_on_stale_numbers_are_found():
    pages = _pages()
    old = {
        "en": ("191", "2,894", "126 / 31 ms", "1,000,000 values | 773 | 2,967"),
        "it": ("191", "2.894", "126 / 31 ms", "1.000.000 di valori | 773 | 2.967"),
        "de": ("191", "2.894", "126 / 31 ms", "1.000.000 Werte | 773 | 2.967"),
        "fr": ("191", "2 894", "126 / 31 ms", "1 000 000 de valeurs | 773 | 2 967"),
        "es": ("191", "2.894", "126 / 31 ms", "1.000.000 de valores | 773 | 2.967"),
    }
    for lang in LANGUAGES:  # 0.10.0's table: 189, 2,691, 77 / 28 ms for a new key, 410 and 1,498 at 1,000,000
        mask, unmask, document, million = old[lang]
        sep = {"en": ",", "fr": " "}.get(lang, ".")
        pages[lang] = pages[lang].replace(f"| {mask} | {unmask} | {document} |", f"| 189 | 2{sep}691 | 77 / 28 ms |")
        pages[lang] = pages[lang].replace(million, million.split(" | ")[0] + f" | 410 | 1{sep}498")
    found = _found(pages, "en:", "accepted")
    assert not any(p.startswith(("it:", "de:", "fr:", "es:")) and "instance" in p for p in found), found


@pytest.mark.parametrize("lang", LANGUAGES)
def test_each_page_names_its_statistic(lang):
    """Each section says which statistic its numbers are: the median."""
    word = {"en": "median", "it": "mediana", "de": "Median", "fr": "médiane", "es": "mediana"}[lang]
    assert word in (section(_pages()[lang], lang) or "")


# --- the same numbers elsewhere (the final review's M-6) ------------------------------------------------------------
#
# Other pages repeat the measurements: each of them holds the same numbers in the five languages (the whole page, or
# for a changelog its section of changes not released yet, read as above), and every measurement a page repeats, named
# here, is among its English numbers with the value of the evidence. README.md, in English alone, is held to its
# measurements. A number here that is no measurement (a setting, a limit of the code) is parity's alone, unless it is
# named below with the code that sets it.


def _row(table: str, first: tuple[float, ...], column: str) -> float:
    """The first number of a cell of ``EVIDENCE``."""
    return next(cells[column][0][0] for row, cells in EVIDENCE[table] if row == first)


def _code() -> dict[str, float]:
    """The numbers the pages give that the code sets."""
    from latet.engine.stored import WHOLE_VALUES
    from latet.server.index import INDEX_BUILD_BYTES

    return {
        "a person's vault held whole, values at most": WHOLE_VALUES,
        "an index's prints at most": INDEX_BUILD_BYTES // 8,
    }


MEASURED: dict[str, float] = {
    # The first table (EVIDENCE, Task 8's fourth campaign 042b63d29de6).
    "a key of 200,000 values masks": _row("instance", (200_000,), "mask"),
    "a key of 200,000 values unmasks": _row("instance", (200_000,), "unmask"),
    "a key of 1,000,000 values masks": _row("instance", (1_000_000,), "mask"),
    "a key of 1,000,000 values unmasks": _row("instance", (1_000_000,), "unmask"),
    "a new key masks": _row("instance", (), "mask"),
    # Latet 0.10.0 on the same computer (TEXT; the ledger's "0.10.0 on this machine").
    "0.10.0 masked at 200,000": 350,
    "0.10.0 unmasked at 200,000": 2114,
    "0.10.0 masked at 1,000,000": 304,
    "0.10.0 unmasked at 1,000,000": 2014,
    "0.10.0 masked for a new key": 153,
    # The plan's absolute target (TEXT).
    "the mask goal": 1000,
    # The index (TEXT, campaign 47be52951b80; the index off, campaign 042b63d29de6): MB a million values, seconds to
    # ready at 1,000,000, the index off.
    "an index's MB a million values": 15,
    "an index ready at 1,000,000, seconds": 1.5,
    "the index off masks at 200,000": 494,
    # A rebuild at 1,000,000 values (campaign 042b63d29de6, R): MB above the start, seconds, MB set aside.
    "a rebuild's MB": 49,
    "a rebuild's seconds": 1.4,
    "a rebuild's MB set aside": 64,
    # A held secret's detectors (5b: 3,923,968 bytes for 173 held), KB.
    "a held secret's KB": 23,
    # Workers on Linux (EVIDENCE's second table, campaign 47be52951b80): the medians' ratios, memory a worker.
    "eight workers mask, times one": 6.28,
    "eight workers unmask, times one": 5.21,
    "a worker's MB, least": _row("workers", (8,), "memory"),
    "a worker's MB, most": _row("workers", (1,), "memory"),
    # A person (campaign 47be52951b80, bench_engine person, on the engine: t13-person-vp20k-r2, -vp5k-r2, -vp200k).
    "a person of 20,000 masks": 851,
    "0.10.0 masked a person of 20,000": 558,
    "a person of 20,000, times 0.10.0": 1.52,
    "a person of 5,000 masks": 885,
    "0.10.0 masked a person of 5,000": 1094,
    "a person of 5,000, times 0.10.0": 0.81,
    "a person of 200,000 masks": 385,
    "0.10.0 masked a person of 200,000": 59,
    "a person's space of 200,000, MB": 24,
    "0.10.0's vault of 200,000, MB": 495,
    # A person past a bound at 200,000 (campaign 0bd37a3c6aa0): the reading's MB.
    "a whole reading of 200,000, MB": 380,
    # Reading every part of a request (the request-coverage report's full measurement), ms at the median.
    "a small request on a server": 6.99,
    "a small request on a server before": 5.95,
    "a new key's first request": 104,
    "a new key's first request before": 97,
    "1 MiB of JSON": 391,
    "1 MiB of JSON before": 373,
    "4,000 slots on a server": 1088,
    "4,000 slots on a server before": 779,
    "500 + 1 on a server": 183,
    "500 + 1 on a server before": 158,
    "a small request on a computer": 2.02,
    "a small request on a computer before": 0.94,
    "4,000 slots on a computer": 362,
    "4,000 slots on a computer before": 96,
    "4,000 slots on a computer, times": 3.8,
    "4,000 slots on a server, times": 1.4,
    "500 + 1 on a computer": 36,
    "500 + 1 on a computer before": 13,
    "500 + 1 on a computer, times": 2.8,
    "500 + 1 on a server, times": 1.16,
}
MEASURED |= _code()

_UPGRADE = ("the index off masks at 200,000", "a key of 200,000 values masks", "an index's prints at most")
_REQUESTS = (
    "a small request on a server",
    "a small request on a server before",
    "a new key's first request",
    "a new key's first request before",
    "1 MiB of JSON",
    "1 MiB of JSON before",
    "4,000 slots on a server",
    "4,000 slots on a server before",
    "500 + 1 on a server",
    "500 + 1 on a server before",
)
_PERSON = (
    "a person of 20,000 masks",
    "0.10.0 masked a person of 20,000",
    "a person of 20,000, times 0.10.0",
    "a person of 5,000 masks",
    "0.10.0 masked a person of 5,000",
    "a person of 5,000, times 0.10.0",
    "a person of 200,000 masks",
    "0.10.0 masked a person of 200,000",
    "a person's space of 200,000, MB",
    "0.10.0's vault of 200,000, MB",
)
_WORKERS = (
    "eight workers mask, times one",
    "eight workers unmask, times one",
    "a worker's MB, least",
    "a worker's MB, most",
)
# Where each page repeats which measurements: (the page, whether its changes not released yet alone, the names).
PLACES: tuple[tuple[str, bool, tuple[str, ...]], ...] = (
    (
        "applications/index.md",
        False,
        (
            *_UPGRADE,
            "a held secret's KB",
            "an index's MB a million values",
            "a person's vault held whole, values at most",
        ),
    ),
    (
        "concepts/server-operations.md",
        False,
        (
            *_REQUESTS,
            *_PERSON,
            *_WORKERS,
            "an index's MB a million values",
            "a held secret's KB",
            "a rebuild's MB",
            "a rebuild's seconds",
            "a rebuild's MB set aside",
            "a whole reading of 200,000, MB",
            "an index's prints at most",
            "a person's vault held whole, values at most",
        ),
    ),
    ("concepts/server.md", False, ("a person's vault held whole, values at most",)),
    (
        "changelog.md",
        True,
        (
            "a key of 200,000 values masks",
            "a key of 200,000 values unmasks",
            "a key of 1,000,000 values masks",
            "a key of 1,000,000 values unmasks",
            "a new key masks",
            "0.10.0 masked at 200,000",
            "0.10.0 unmasked at 200,000",
            "0.10.0 masked at 1,000,000",
            "0.10.0 unmasked at 1,000,000",
            "0.10.0 masked for a new key",
            "the mask goal",
            "an index's MB a million values",
            "an index ready at 1,000,000, seconds",
            "a person's space of 200,000, MB",
            "0.10.0's vault of 200,000, MB",
            "a person's vault held whole, values at most",
            *_WORKERS,
            "a small request on a server",
            "a small request on a server before",
            "a small request on a computer",
            "a small request on a computer before",
            "4,000 slots on a computer",
            "4,000 slots on a computer before",
            "4,000 slots on a computer, times",
            "4,000 slots on a server, times",
            "500 + 1 on a computer",
            "500 + 1 on a computer before",
            "500 + 1 on a computer, times",
            "500 + 1 on a server, times",
        ),
    ),
)
README = (
    "a key of 200,000 values masks",
    "a key of 200,000 values unmasks",
    "a key of 1,000,000 values masks",
    "a key of 1,000,000 values unmasks",
    "eight workers mask, times one",
)


RELEASE = "0.11.0"  # the release whose changelog section carries these numbers


def _this_release(page: str) -> str:
    """A changelog's section for ``RELEASE``: from its heading of the second level to the next one."""
    found = re.search(rf"^## {re.escape(RELEASE)} \(.*?$(.*?)(?=^## |\Z)", page, flags=re.M | re.S)
    return found.group(1) if found else ""


def page_numbers(page: str, lang: str, this_release: bool = False) -> Counter:
    return Counter(numbers(_plain(_this_release(page) if this_release else page, lang), lang))


def _elsewhere() -> dict[str, dict[str, str]]:
    """Each place's page in the five languages, and README.md (under ``README.md``, English alone)."""
    pages = {
        name: {
            lang: (DOCS / name if lang == "en" else DOCS / lang / name).read_text(encoding="utf-8")
            for lang in LANGUAGES
        }
        for name, _, _ in PLACES
    }
    pages["README.md"] = {"en": (ROOT / "README.md").read_text(encoding="utf-8")}
    return pages


def elsewhere_problems(pages: dict[str, dict[str, str]]) -> list[str]:
    """What is wrong with the numbers the other pages repeat: a translation whose numbers are not English's, and a
    measurement a page names that is not among its English numbers with the evidence's value."""
    found: list[str] = []
    for name, this_release, measured in PLACES:
        english = page_numbers(pages[name]["en"], "en", this_release)
        for lang in LANGUAGES[1:]:
            theirs = page_numbers(pages[name][lang], lang, this_release)
            if theirs != english:
                more, fewer = dict(theirs - english), dict(english - theirs)
                found.append(f"{lang}: {name}'s numbers differ from English: more {more}, fewer {fewer}")
        for what in measured:
            if MEASURED[what] not in english:
                found.append(f"en: {name} does not say {what} ({MEASURED[what]})")
    readme = page_numbers(pages["README.md"]["en"], "en")
    for what in README:
        if MEASURED[what] not in readme:
            found.append(f"en: README.md does not say {what} ({MEASURED[what]})")
    return found


def test_the_numbers_said_elsewhere_are_the_same_and_the_evidences():
    assert elsewhere_problems(_elsewhere()) == []


def test_numbers_that_are_versions_are_not_read_as_measurements():
    assert numbers(_plain("Valkey ab 7.2 oder Redis 7, Codex vor 0.115, 0.10 und 0.10.4", "de"), "de") == [7]
    assert numbers(_plain("Valkey 7.2 or Redis 7, Codex before 0.115, 0.10 and 0.10.4; 0.51 ms"), "en") == [7, 0.51]
    assert numbers(_plain("1.000.000 e 16.777.216", "it"), "it") == [1_000_000, 16_777_216]


def _elsewhere_changed(name: str, langs: tuple[str, ...], old: dict[str, str], new: dict[str, str]):
    pages = _elsewhere()
    for lang in langs:
        assert pages[name][lang].count(old[lang]) >= 1, f"{lang} {name} has no {old[lang]!r}"
        pages[name][lang] = pages[name][lang].replace(old[lang], new[lang])
    return pages


def test_a_number_changed_in_one_translation_elsewhere_is_found():
    pages = _elsewhere_changed("concepts/server-operations.md", ("de",), {"de": "6,28-Fache"}, {"de": "6,27-Fache"})
    found = elsewhere_problems(pages)
    assert any(p.startswith("de: concepts/server-operations.md") for p in found), found


def test_five_changelogs_agreeing_on_a_stale_number_are_found():
    old = {"en": "6.28 times", "it": "6,28 volte", "de": "6,28-Fache", "fr": "6,28 fois", "es": "6,28 veces"}
    new = {lang: said.replace("6.28", "6.27").replace("6,28", "6,27") for lang, said in old.items()}
    found = elsewhere_problems(_elsewhere_changed("changelog.md", LANGUAGES, old, new))
    assert found == ["en: changelog.md does not say eight workers mask, times one (6.28)"], found


def test_a_stale_number_in_the_readme_is_found():
    pages = _elsewhere()
    pages["README.md"]["en"] = pages["README.md"]["en"].replace("871 log lines", "872 log lines")
    found = elsewhere_problems(pages)
    assert found == ["en: README.md does not say a key of 200,000 values masks (871)"], found
