"""The anchor word of a value or a fake: the longest run of plain letters and digits in it. A vault kept in a database
finds the values and fakes it holds in a text by the anchors of the words of that text, so the anchor must be there in
every form a value or a fake takes in a text."""

import json
import random
import time
import unicodedata
from urllib.parse import quote

import pytest

from latet.engine.anchors import (
    ANCHORS_VERSION,
    EDGE,
    ONE_RUN_HELD,
    SEGMENTS_AT_MOST,
    address_word,
    anchor,
    dotted,
    edges,
    glued_unfound,
    words_in,
)
from latet.engine.rehydrate import restores_for
from latet.engine.types import EntityClass
from latet.engine.vault import Entity


def test_the_longest_run_in_lower_case():
    assert anchor("Laura.Bianchi@nordest.it") == "bianchi"


def test_a_run_of_an_accented_name_survives_json_escapes():
    assert anchor("Niccolò Ferri") == "niccol"
    assert anchor("Niccolò Ferri") in words_in('"Niccol\\u00f2 Ferri"')


def test_url_quoting_keeps_the_anchor():
    assert anchor("laura.bianchi@nordest.it") in words_in("laura.bianchi%40nordest.it")


def test_no_anchor_in_a_short_or_signs_only_value():
    assert anchor("ab-1") is None
    assert anchor("—·—") is None


def test_the_first_of_two_runs_as_long_wins():
    assert anchor("Anna Rosa") == "anna"
    assert anchor("Rosa Anna") == "rosa"


def test_a_letter_and_its_accent_written_apart_are_the_letter_written_whole():
    apart = "Niccolo\N{COMBINING GRAVE ACCENT} Ferri"
    whole = "Niccol\N{LATIN SMALL LETTER O WITH GRAVE} Ferri"
    assert apart != whole
    assert anchor(apart) == anchor(whole) == "niccol"
    assert words_in(apart) == words_in(whole)
    assert "niccol" in words_in(apart)


def test_an_accent_written_apart_in_a_word_of_a_text_does_not_cut_it_short():
    apart = "M" + "u\N{COMBINING DIAERESIS}" + "ller"
    assert anchor(apart) == anchor("M\N{LATIN SMALL LETTER U WITH DIAERESIS}ller") == "ller"
    assert "ller" in words_in(f"Laura {apart}")


def test_the_letter_of_an_escape_does_not_glue_a_word_to_the_one_before():
    assert {"anna", "laura"} <= words_in("Anna\\nLaura")
    assert {"laura", "anna"} <= words_in("Laura\\nAnna")
    assert {"laura", "bianchi"} <= words_in("Laura\\tBianchi")
    assert {"laura", "bianchi"} <= words_in("Laura\\r\\nBianchi")
    assert {"laura", "bianchi"} <= words_in("Laura\\bBianchi\\fLaura")


def test_an_escape_of_a_character_that_is_not_a_letter_glues_nothing():
    assert {"laura", "bianchi"} <= words_in('"Laura\\/Bianchi"')
    assert {"laura", "bianchi"} <= words_in('\\"Laura\\" \\\\Bianchi')


@pytest.mark.parametrize("written", ["\\u00e9", "\\u00E9", "\\xe9", "\\xE9"])
def test_an_accented_letter_written_as_an_escape_is_no_part_of_a_word(written):
    assert anchor("Müller") == "ller"
    assert "ller" in words_in(f'"M{written}ller"')
    assert "ller" in words_in(f"Laura M{written}ller")


def test_an_accented_letter_as_an_escape_at_the_start_of_a_word():
    assert "bianchi" in words_in("\\u00e9Bianchi")
    assert "bianchi" in words_in("\\xe9Bianchi")
    assert "bianchi" in words_in("Laura \\u00c8Bianchi")


def test_a_backslash_written_twice_is_read_as_a_backslash_and_as_the_escape_of_a_text_inside_a_text():
    # `\\n` is a backslash and an `n`, and it is a line break of JSON inside a JSON string: both are read.
    words = words_in("Anna\\\\nLaura")
    assert {"nlaura", "laura", "anna"} <= words
    assert {"laura", "anna"} <= words_in("Anna\\\\\\nLaura")
    assert {"laura", "anna"} <= words_in("Anna\\\\\\\\nLaura")
    assert {"bianchi", "laura"} <= words_in("Laura\\\\tBianchi")
    assert {"bianchi", "laura"} <= words_in("Laura\\\\r\\\\nBianchi")
    assert "bianchi" in words_in("\\\\u00e9Bianchi")
    assert "bianchi" in words_in("\\\\xe9Bianchi")


def test_the_case_is_folded_and_not_only_lowered():
    assert anchor("Hauptstraße") == anchor("HAUPTSTRASSE") == anchor("hauptstrasse") == "hauptstrasse"
    for written in ("Hauptstraße", "HAUPTSTRASSE", "hauptstraße", "hauptstrasse"):
        assert "hauptstrasse" in words_in(written)
    assert anchor("Müller-Weiß") == "weiss"
    assert anchor("Bianchi") == anchor("BIANCHI") == anchor("bianchi") == "bianchi"
    assert "bianchi" in words_in("LAURA BIANCHI")


DOTLESS_I = "\N{LATIN SMALL LETTER DOTLESS I}"
TURKISH = f"Y{DOTLESS_I}ld{DOTLESS_I}zhan"


def test_the_dotless_i_of_a_turkish_name_is_the_i_of_its_upper_case():
    assert TURKISH.upper() == "YILDIZHAN"
    assert anchor(TURKISH) == anchor("YILDIZHAN") == anchor(TURKISH.lower()) == "yildizhan"
    for written in (
        TURKISH,
        "YILDIZHAN",
        TURKISH.lower(),
        "yildizhan",
        "Y\\u0131ld\\u0131zhan",
        "Y%C4%B1ld%C4%B1zhan",
        "x\\nY\\u0131ld\\u0131zhan\\ty",
    ):
        assert "yildizhan" in words_in(written), written


@pytest.mark.parametrize("letter", ["v", "a", "0", "e", "U0001f600", "U0001F600"])
def test_the_letters_of_the_escapes_of_c_and_python_logs_glue_nothing(letter):
    assert {"laura", "anna"} <= words_in(f"Anna\\{letter}Laura")
    assert {"laura", "anna"} <= words_in(f"Anna\\\\{letter}Laura")
    assert {"laura", "anna"} <= words_in(f"Anna\\\\\\{letter}Laura")


def test_half_of_a_character_written_as_an_escape_is_no_part_of_a_word():
    assert {"laura", "bianchi"} <= words_in("Laura \\ud83d Bianchi")
    assert {"laura", "bianchi"} <= words_in("Laura\\ud83d\\ude00Bianchi")
    assert {"laura", "bianchi"} <= words_in("Laura\\\\ud83d\\\\ude00Bianchi")


@pytest.mark.parametrize(
    "form",
    [
        "Via Hauptstra\\u00dfe",
        "Hauptstra\\u00DFe",
        "Hauptstra\\xdfe",
        "Hauptstra\\xc3\\x9fe",
        "Hauptstra%C3%9Fe",
        "Hauptstra\\\\u00dfe",
        "\\nHauptstra\\u00dfe\\n",
        "x\\\\nHauptstra\\\\u00dfe\\\\ty",
        "HAUPTSTRASSE",
        "hauptstraße",
    ],
)
def test_a_letter_that_folds_to_two_is_in_the_anchor_in_every_form_it_takes(form):
    assert anchor("Hauptstraße") in words_in(form)


@pytest.mark.parametrize(
    "fake", ["Laura Bianchi", "Bianchi Laura", "Zoë Müller", "Müller Zoë", "Hauptstraße 12", "12 Hauptstraße"]
)
@pytest.mark.parametrize(
    "form",
    [
        "{}",
        "\\n{}",
        "{}\\n",
        "a\\n{}\\nb",
        "\\t{}\\t",
        "\\r\\n{}\\r\\n",
        "\\u00e9{}",
        "{}\\u00e9",
        "\\xe9{}",
        "{}\\xe9",
        "%20{}",
        "{}%20",
        "\\u00e9\\n{}\\t\\u00e9",
        "\\\\n{}",
        "{}\\\\n",
        "\\\\t{}\\\\t",
        "\\\\u00e9{}",
        "{}\\\\u00e9",
        "\\\\xe9{}",
        "\\\\\\\\n{}\\\\\\\\n",
        "\\\\u00e9\\\\n{}\\\\t\\\\u00e9",
        "\\v{}\\v",
        "\\a{}\\e",
        "\\0{}",
        "\\U0001f600{}",
        "{}\\U0001f600",
        "\\\\v{}\\\\a",
        "\\\\U0001f600{}",
    ],
)
def test_a_fake_next_to_an_escape_is_found_by_its_anchor(fake, form):
    assert anchor(fake) in words_in(form.format(fake))


# What a form can stand between: nothing, a line break, an accent, and the same written twice (a JSON string in a
# JSON string).
AROUND = ["{}", "x\\n{}\\ny", "\\u00e9{}\\t", "x\\\\n{}\\\\ny", "\\\\u00e9{}\\\\t"]

ENTITIES = [
    Entity("a" * 20, "PERSON", EntityClass.NER, "Niccolò Ferri", "Zoë Müller-Rossi", 0, 0.0),
    Entity("b" * 20, "PERSON", EntityClass.NER, "Anna Verdi", "Giulia Marchetti", 0, 0.0),
    Entity("c" * 20, "EMAIL", EntityClass.PII, "laura.bianchi@nordest.it", "zoë.müller@nordest.it", 0, 0.0),
    Entity("d" * 20, "EMAIL", EntityClass.PII, "anna@acme.it", "Marta.Galli@Example.org", 0, 0.0),
    Entity("e" * 20, "PASSWORD", EntityClass.SECRET, "hunter2", "my secret phrase&x=1", 0, 0.0),
    Entity("f" * 20, "PASSWORD", EntityClass.SECRET, "swordfish", "Zoë è/Pass+Word", 0, 0.0),
    Entity("g" * 20, "HOSTNAME", EntityClass.PII, "db1.acme.it", "srv-4f2a.Example.net", 0, 0.0),
    Entity("h" * 20, "API_KEY", EntityClass.SECRET, "sk-live-abc", "sk-test-Ab12Cd34", 0, 0.0),
    Entity("i" * 20, "COMPANY", EntityClass.NER, "Acme Spa", "Nordest Srl", 0, 0.0),
    Entity("j" * 20, "STREET_ADDRESS", EntityClass.PII, "Via Roma 1", "Hauptstraße 12", 0, 0.0),
    Entity("k" * 20, "PERSON", EntityClass.NER, "Mario Rossi", "Müller-Weiß", 0, 0.0),
    Entity("l" * 20, "PERSON", EntityClass.NER, "Ali Veli", f"{TURKISH} Çelik", 0, 0.0),
    # 0.9.9: values kept as written in escapes (found where they read as what they stand for, ``decoded``), each with a
    # fake written in the same escapes, restored as written, in a link's other forms, and as it reads (`Giulia Martini`).
    Entity("m" * 20, "PERSON", EntityClass.NER, "Mario%20Rossi", "Giulia%20Martini", 0, 0.0, None, True),
    Entity("n" * 20, "EMAIL", EntityClass.PII, "ugo\\u0040acme.it", "luca.neri\\u0040nordest.it", 0, 0.0, None, True),
    Entity("o" * 20, "PERSON", EntityClass.NER, "Mario+Rossi", "Giulia+Martini", 0, 0.0, None, True),
    Entity("p" * 20, "PERSON", EntityClass.NER, "Niccol%C3%B2 Ferri", "Paolo%20Fontana", 0, 0.0, None, None),
    # Every run of the fake glued to an escape of its own (a phone's `%2B` in a link, a password's signs): the anchor is
    # a run the escapes leave free (version 4), a word of the fake as written and as it reads.
    Entity("q" * 20, "PHONE", EntityClass.PII, "%2B393331112222", "%2B393331234567", 0, 0.0, None, True),
    Entity("r" * 20, "PHONE", EntityClass.PII, "%2B39%20333%20111222", "%2B39%20412%20867453", 0, 0.0, None, True),
    Entity("s" * 20, "PASSWORD", EntityClass.SECRET, "p%40ss%2Fw0rd%2BZq9", "s%40fy%2Ft1lb%2BMd3", 0, 0.0, None, True),
]
FAKES = {e.id: e.surrogate for e in ENTITIES}


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("around", AROUND)
def test_the_anchor_of_a_fake_is_in_the_words_of_every_form_a_restore_reads_it_in(json_mode, around):
    restores = restores_for(ENTITIES, json_mode=json_mode, parts=False)
    assert len(restores) > len(ENTITIES)
    for restore in restores:
        fake = FAKES[restore.entity_id]
        assert anchor(fake) is not None
        assert anchor(fake) in words_in(around.format(restore.pattern)), (fake, restore.pattern)


@pytest.mark.parametrize("around", AROUND)
def test_the_anchor_of_a_fake_is_in_the_words_of_the_fake_as_json_writes_it_in_ascii(around):
    for fake in FAKES.values():
        assert anchor(fake) in words_in(around.format(json.dumps(fake)))


# The text of an API that takes any text: a run of backslashes is read in time that grows with its length, never with
# its square (a regex that began at every backslash of a run read 10,000 of them in a second, and 200,000 in minutes).
BOUND = 5.0


def _seconds(text: str) -> tuple[set[str], float]:
    started = time.perf_counter()
    words = words_in(text)
    return words, time.perf_counter() - started


@pytest.mark.timing
def test_a_very_long_run_of_backslashes_is_read_in_time_that_grows_with_its_length():
    words, seconds = _seconds(chr(92) * 200_000)
    assert words == set()
    assert seconds < BOUND, f"{seconds:.1f} s for a run of 200,000 backslashes"


@pytest.mark.timing
def test_many_long_runs_of_backslashes_are_read_in_time_that_grows_with_their_number():
    text = (chr(92) * 1000 + "\n") * 2000  # 2 MB, a run on each line
    words, seconds = _seconds(text)
    assert words == set()
    assert seconds < BOUND, f"{seconds:.1f} s for 2,000 runs of 1,000 backslashes"


@pytest.mark.timing
@pytest.mark.parametrize("escape", ["n", "t", "u00e9", "xe9", "v", "U0001f600", "ud83d"])
@pytest.mark.parametrize("fake", ["Laura Bianchi", "Bianchi Laura"])
def test_a_fake_after_a_long_run_of_backslashes_and_an_escape_is_still_found(fake, escape):
    for backslashes in (2, 3, 1000, 100_001):
        words, seconds = _seconds(f"{chr(92) * backslashes}{escape}{fake}")
        assert anchor(fake) in words, (backslashes, escape)
        assert seconds < BOUND, f"{seconds:.1f} s for a run of {backslashes:,} backslashes"


# Values in other scripts than the Latin one (Ruling T7a): with no run of three plain letters and digits, the anchor is
# the longest run of letters and digits of any script, of two at least, read without its accents and marks; in a script
# written without spaces, the first two characters of its run, and a text's words are every two characters in a row of
# such a run, as a name there is glued to the words around it.
NAMES = {
    "Иван Петров": "петров",
    "Γιώργος Παπαδόπουλος": "παπαδοπουλοσ",
    "محمد علي": "محمد",
    "王小明": "王小",
    "山田太郎": "山田",
    "김민수": "김민",
    "राम शर्मा": "शरम",
}


def test_a_name_in_another_script_has_an_anchor_of_its_own():
    for name, word in NAMES.items():
        assert anchor(name) == word, name
    assert anchor("Иван") == "иван" and anchor("Иван2024") == "2024"  # noqa: RUF001 - a run of plain letters and digits first
    assert anchor("Zoë") == "zoe" and anchor("Ré-1") == "re"
    assert anchor("Ж") is None and anchor("Ж-1") is None and anchor("ab-1") is None and anchor("—·—") is None


def test_a_name_in_another_script_is_read_without_its_accents_and_marks():
    assert anchor("Фёдоров") == anchor("Федоров") == "федоров"  # its dots are left out as often as not
    assert anchor("ΠΑΠΑΔΟΠΟΥΛΟΣ") == anchor("Παπαδόπουλος")  # Greek capitals drop their accents
    assert anchor("مُحَمَّد") == anchor("محمد") == anchor("محـــمد")  # vowel points, and the stretching of a line
    assert anchor("Иван\N{COMBINING BREVE}") == anchor("Иван")  # an accent alone is no letter of the word
    assert anchor("Йван") == anchor("Иван") == "иван"


def _forms(name: str) -> list[str]:
    """The forms a value takes in a text: as written, in upper case, in a sentence, in JSON in ASCII (once, and inside
    another JSON string), quoted in a URL, as a log writes its bytes, and between escapes."""
    return [
        name,
        name.upper(),
        f"a: {name}, b",
        json.dumps(name),
        json.dumps(json.dumps(name)),
        quote(name),
        str(name.encode())[2:-1],
        *(around.format(name) for around in AROUND),
        *(around.format(json.dumps(name)) for around in AROUND),
    ]


@pytest.mark.parametrize("name", [*NAMES, "Иван2024", "Фёдоров", "Zoë Ré", "مُحَمَّد"])  # noqa: RUF001
def test_a_name_in_another_script_is_in_the_words_of_every_form_it_takes(name):
    for form in _forms(name):
        assert anchor(name) in words_in(form), form


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("王小明", "请联系王小明先生，谢谢。"),  # noqa: RUF001 - a Chinese comma
        ("山田太郎", "山田太郎さんへのメール"),
        ("김민수", "김민수가 어제 왔다"),
        ("สมชาย", "คุณสมชายมาแล้ว"),
    ],
)
def test_a_name_in_a_script_without_spaces_is_found_glued_to_the_words_around_it(name, text):
    assert anchor(name) is not None
    assert anchor(name) in words_in(text)
    assert anchor(name) in words_in(json.dumps(text)) and anchor(name) in words_in(quote(text))


def test_a_text_in_plain_ascii_has_no_word_of_another_script():
    assert words_in("Laura Bianchi, to it") == {"laura", "bianchi", "bianch", "ianchi"}  # and a long run's edges


@pytest.mark.timing
def test_a_long_text_in_cyrillic_is_read_in_time_that_grows_with_its_length():
    escaped = json.dumps("Иван")[1:-1]  # its letters as JSON writes them in ASCII
    line = f"Иван Петров написал Марии Ивановой{chr(92)}nи Фёдору %D0%98 {escaped}. "
    text = line * (2_000_000 // len(line))  # 2 MB, every reading of it made (escapes, quotes)
    words, seconds = _seconds(text + "Сергей Смирнов")
    assert {"петров", "смирнов", "иван", "федору"} <= words
    assert seconds < BOUND, f"{seconds:.1f} s for 2 MB of Cyrillic"


@pytest.mark.timing
def test_a_long_text_without_spaces_is_read_in_time_that_grows_with_its_length():
    letters = [chr(code) for code in range(0x4E00, 0x4E00 + 300)]
    text = "".join(random.Random(7).choices(letters, k=2_000_000))  # 2 MB of Chinese, no space, no sign
    words, seconds = _seconds(text + "王小明")
    assert anchor("王小明") in words
    assert seconds < BOUND, f"{seconds:.1f} s for 2 MB of Chinese without spaces"


# Ruling T7b. A format character (a zero-width space or non-joiner, a word joiner, a soft hyphen, a mark of direction)
# is the edge of a word to the engine, which finds a value next to one: it is the edge of a word here too.
@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("رضایی", "خانواده رضایی\N{ZERO WIDTH NON-JOINER}ها آمدند"),  # noqa: RUF001 - a Persian plural
        ("Петров", "Иван\N{ZERO WIDTH SPACE}Петров"),
        ("Петров", "Иван\N{WORD JOINER}Петров"),
        ("Петров", "Иван\N{SOFT HYPHEN}Петров"),
        ("محمد", "علي\N{RIGHT-TO-LEFT MARK}محمد\N{RIGHT-TO-LEFT MARK}حسن"),
        ("Иван", "Иван\N{LEFT-TO-RIGHT MARK}Сидоров"),
        ("Zoë", "Zoë\N{ZERO WIDTH SPACE}Müller"),
    ],
)
def test_a_format_character_is_the_edge_of_a_word_as_the_engine_reads_it(value, text):
    assert anchor(value) is not None
    for form in (text, json.dumps(text), quote(text)):
        assert anchor(value) in words_in(form), form


@pytest.mark.timing
def test_a_long_run_of_combining_marks_is_read_in_time_that_grows_with_its_length():
    """T6d: putting a run of marks in their order takes time that grows with its square (80,000 out of their order took
    2.2 s, 200,000 take a minute); no word has more than a few, and a run of more is read as a space."""
    marks = "\N{COMBINING GRAVE ACCENT BELOW}\N{COMBINING COMMA ABOVE RIGHT}" * 100_000  # out of their order
    text = f"Laura a{marks} Bianchi"
    started = time.perf_counter()
    assert anchor(text) == "bianchi"
    seconds = time.perf_counter() - started
    assert seconds < BOUND, f"{seconds:.1f} s for the anchor of a run of 200,000 marks"
    words, seconds = _seconds(text)
    assert {"laura", "bianchi"} <= words
    assert seconds < BOUND, f"{seconds:.1f} s for the words of a run of 200,000 marks"


def test_a_word_with_a_few_marks_is_still_put_together():
    apart = "Zoe\N{COMBINING DIAERESIS} Mu\N{COMBINING DIAERESIS}ller"
    assert anchor(apart) == anchor("Zoë Müller") == "ller" and words_in(apart) == words_in("Zoë Müller")


def test_the_iteration_marks_and_the_phonetic_letters_of_chinese_and_japanese_are_letters_of_their_names():
    assert anchor("佐々木") == "佐々" and "佐々" in words_in("佐々木さんへ")
    zero = chr(0x3007)  # the ideographic zero
    assert anchor(f"{zero}{zero}商事") == f"{zero}{zero}"
    bopomofo = "".join(map(chr, (0x3113, 0x3128, 0x3125)))
    assert anchor(bopomofo) == bopomofo[:2] and bopomofo[:2] in words_in(f"他叫{bopomofo}。")


def test_a_character_beyond_the_first_plane_written_as_two_escapes_is_read_as_the_character():
    name = chr(0x20BB7) + "田"  # JSON in ASCII writes its first character as two escapes, the halves of a pair
    assert anchor(name) == name
    for form in (json.dumps(name), json.dumps(json.dumps(name)), f"x{chr(92)}n{json.dumps(name)}"):
        assert anchor(name) in words_in(form), form


# What ``anchor`` gives, as a store keeps it (Rulings T7d, T8a): a change to any of these gives a kept value another anchor,
# which a store finds no more until it makes its words again, so it bumps ``ANCHORS_VERSION``, and this table with it.
_ANCHORS_4 = [
    ("Laura.Bianchi@nordest.it", "bianchi"),
    ("Niccolò Ferri", "niccol"),
    ("ab-1", None),
    ("Anna Rosa", "anna"),
    ("Hauptstraße 12", "hauptstrasse"),
    (TURKISH, "yildizhan"),
    ("Фёдоров", "федоров"),
    ("Иван Петров", "петров"),
    ("ΠΑΠΑΔΟΠΟΥΛΟΣ", "παπαδοπουλοσ"),
    ("王小明", "王小"),
    ("佐々木", "佐々"),
    ("김민준", "김민"),
    ("สมชาย", "สม"),
    ("رضایی", "رضایی"),
    ("Zoë", "zoe"),
    ("+39 347 123 4567", "4567"),
    ("GB82WEST12345698765432", "gb82west12345698765432"),
    ("x9-k2", None),
    ("Ж", None),
    ("mario.rossi@acme.it", "mario"),
    ("ugo.neri@gmail.com", "neri"),  # a public mail domain's labels are no anchor while there is another run
    ("maria.rossi@hotmail.it", "maria"),
    ("a1@libero.it", "libero"),  # none other: the label
    ("ab" + "e\N{COMBINING ACUTE ACCENT}" + "\N{COMBINING DOUBLE TILDE}" * 40 + "cd", "abe"),
    # Version 3: a run an escape glues letters to (`20martini`, `u0040nordest`) is no anchor while the string has
    # another, so that a fake written in escapes is found as it reads (`Giulia Martini`) as well as it is written.
    ("Giulia%20Martini", "giulia"),
    ("luca.neri\\u0040nordest.it", "luca"),
    ("Niccol%C3%B2 Ferri", "niccol"),
    ("Mario+Rossi", "mario"),
    ("pa%41ss", "41ss"),  # every run glued, and none left free by the escapes: the longest of them, as before
    # Version 4: every run glued to an escape of the string's own, the runs the escapes leave free come first.
    ("%2B393331234567", "393331234567"),
    ("%2B39%20412%20867453", "867453"),
    ("s%40fy%2Ft1lb%2BMd3", "t1lb"),
]


@pytest.mark.parametrize(("value", "found"), _ANCHORS_4, ids=[ascii(v)[:40] for v, _ in _ANCHORS_4])
def test_the_anchors_of_version_four_and_five(value, found):
    """Version 5 gave no value another anchor: it added the edges (``_EDGES_5``). Nor did 6 to 9."""
    assert ANCHORS_VERSION == 9, "the anchors changed version: this table is version 9's, make the new one's"
    assert anchor(value) == found


# Version 5 (Ruling T10h): an anchor at an edge of its string, and longer than ``EDGE`` letters, has a word more, which
# finds the string glued to a letter or a digit on that side: its head, its first ``EDGE`` letters, where it ends the
# string (a run of a text that begins with them), its tail, its last, where it begins it.
_EDGES_5 = [
    ("tok_9fQ2xL7pZ4", ("9fq2xl", None)),  # at the end: glued after, `tok_9fQ2xL7pZ4acme`
    ("IT84W7093766632581800787511", ("it84w7", "787511")),  # the whole string: glued after or before
    ("Massimiliano%20Rossi", (None, "iliano")),  # at the start
    ("+39 3331234567", ("333123", None)),
    ("RSSMRA85T10A562S", ("rssmra", "0a562s")),
    ("Laura.Bianchi@nordest.it", (None, None)),  # inside it: no letter glued to the string touches it
    (
        "luca84@gmail.com",
        (None, None),
    ),  # no longer than EDGE: its own word is the first or last letters of a text's run
    ("198.18.0.7", (None, None)),
    ("Фёдоров", (None, None)),  # another script: none
    ("ab-1", (None, None)),
]


@pytest.mark.parametrize(("value", "found"), _EDGES_5, ids=[ascii(v)[:40] for v, _ in _EDGES_5])
def test_the_edges_of_version_five(value, found):
    assert ANCHORS_VERSION == 9 and EDGE == 6
    assert edges(value) == found


@pytest.mark.parametrize("value", [v for v, found in _EDGES_5 if any(found)] + ["luca84@gmail.com"])
@pytest.mark.parametrize("glue", ["x", "D", "7", "Angelo", "4539148803436467"])
def test_a_string_glued_to_letters_or_digits_on_the_side_of_its_anchor_is_found_by_a_word_of_the_text(value, glue):
    """Ruling T10h: a secret is found inside a word, and a fake that is no word is restored there, by a vault that holds
    them; a store that finds them by their words finds them glued too, on the side their anchor touches."""
    kept = {anchor(value), *edges(value)} - {None}
    folded = value.upper().casefold()
    if folded.endswith(anchor(value)):
        assert kept & words_in(f"see {value}{glue} now"), "glued after"
        assert kept & words_in(json.dumps(f"{value}{glue}\n")), "glued after, in JSON"
    if folded.startswith(anchor(value)):
        assert kept & words_in(f"see {glue}{value} now"), "glued before"
        assert kept & words_in(json.dumps(f"\n{glue}{value}")), "glued before, in JSON"


def test_a_text_has_the_first_and_last_letters_of_its_long_runs_among_its_words():
    found = words_in("billing-service took 5f3a9c1d2b4e6f70")
    assert {"billing", "billin", "illing", "servic", "ervice", "5f3a9c", "4e6f70"} <= found
    assert "took" in found and "too" not in found and "ook" not in found  # a run no longer than EDGE: itself alone


def test_a_long_run_of_marks_is_cut_in_a_composed_text_too():
    """Ruling T7d: a run of more than ``MARKS_AT_MOST`` marks is read as a space whether the text is composed (NFC)
    already or not, so that a value's anchor does not hang on the form it is written in: the words on either side of
    the run stay apart in both."""
    apart = "ab" + "e\N{COMBINING ACUTE ACCENT}" + "\N{COMBINING DOUBLE TILDE}" * 40 + "cd"
    whole = unicodedata.normalize("NFC", apart)
    assert apart != whole and unicodedata.is_normalized("NFC", whole)
    assert anchor(apart) == anchor(whole) == "abe"
    assert "abecd" not in words_in(whole) and "abe" in words_in(whole)


def test_a_public_mail_domain_is_no_anchor_while_the_value_has_another_word():
    """Ruling T8a: thousands of addresses share `gmail` or `hotmail`, and a text that names one would have the store
    open all of them: an address's anchor is a word of its own, in any script, and the domain's only when it has none."""
    assert anchor("ugo.neri@gmail.com") == "neri" and anchor("UGO.NERI@GMAIL.COM") == "neri"
    assert anchor("Иван@gmail.com") == "иван"
    assert anchor("gmail") == "gmail" and anchor("x1@outlook.com") == "outlook"
    assert anchor("ugo.neri@gmail.com") in words_in("write to ugo.neri%40gmail.com")


# Version 6 (Task 10's F3): a fake is a string Latet made, and one shaped as an address, an IPv4 address or a domain is
# found by a word of its own, its letters and digits with the dots, hyphens and underscores between them (``dotted``):
# the fakes at one company share their domain, all the fakes of a vault share `example`, and the IPv4 fakes share
# their first number, so that a text naming one opened thousands of them.
_DOTTED_6 = [
    ("lily.harris", "lily.harris"),
    ("198.51.100.7", "198.51.100.7"),
    ("Liberty-D05E.example", "liberty-d05e.example"),
    ("swroioqcop59", "swroioqcop59"),
    ("a.b", "a.b"),
    ("ab", None),  # no word of a text: a run of letters and digits has three at least
    ("a.b.c.d.e", None),  # more than SEGMENTS_AT_MOST parts
    ("lily+news", None),  # a sign that is no dot, hyphen or underscore
    ("Zoë.li", None),
    (".lily.", "lily"),
]


@pytest.mark.parametrize(("value", "found"), _DOTTED_6, ids=[ascii(v)[:40] for v, _ in _DOTTED_6])
def test_the_dotted_words_of_version_six(value, found):
    assert ANCHORS_VERSION == 9 and SEGMENTS_AT_MOST == 4
    assert dotted(value) == found


@pytest.mark.parametrize(
    "text",
    [
        "user=lily.harris@liberty-d05e.example ip=198.51.100.7 status=200",
        "USER=LILY.HARRIS@LIBERTY-D05E.EXAMPLE IP=198.51.100.7.",
        "user=lily.harris%40liberty-d05e.example&ip=198.51.100.7",
        json.dumps({"to": "lily.harris@liberty-d05e.example", "from": "198.51.100.7:8080"}, ensure_ascii=True),
        "x-lily.harris@liberty-d05e.example via v2-198.51.100.7",
        "<lily.harris@liberty-d05e.example>, (198.51.100.7)",
    ],
)
def test_a_text_has_the_dotted_words_of_the_fakes_in_it_where_it_is_read_for_fakes(text):
    found = words_in(text, dotted=True)
    assert {"lily.harris", "198.51.100.7", "liberty-d05e.example", "lily.harris@liberty-d05e"} <= found
    assert not {"lily.harris", "198.51.100.7"} & words_in(text)  # a text read for values: its runs alone


def test_a_long_dotted_run_gives_its_parts_of_a_few_runs_alone():
    """A run of a million dotted parts is read in time that grows with its length: words of at most
    ``SEGMENTS_AT_MOST`` parts."""
    text = ".".join(["ab"] * 200_000)
    started = time.perf_counter()
    found = words_in(text, dotted=True)
    assert time.perf_counter() - started < 5
    assert found == {"ab.ab", "ab.ab.ab", "ab.ab.ab.ab"}


# Version 8 (review 10b, I1, and the unmask at 1,000,000 values): a string a vault finds inside a word whose glued form
# no word of a text holds is held in memory for good (``glued_unfound``), and an address's fake is found by its local
# part with its domain's name (``address_word``), which few fakes share at any size of the vault.
_HELD_8 = [
    ("ab12", True),  # one run: glued on both sides, in the middle of a text's run
    ("hunter2", True),
    ("Zt8kQ2pW", True),
    ("a" * ONE_RUN_HELD, True),
    ("a" * (ONE_RUN_HELD + 1), False),  # a longer run: its head and tail find it glued on one side (the class left)
    ("5f3a9c1d2b4e6f70a1b2", False),
    ("tok_ab12", True),  # an anchor of EDGE letters or fewer that ends it: glued after, the start of a text's run
    ("ab12_tok", True),  # that begins it
    ("tok_9fQ2xL7pZ4", False),  # longer than EDGE at its end: its head finds it
    ("-ab12-", False),  # inside it: no letter glued to the string touches it
    ("x9-k2", False),  # no anchor: a vault holds it for good already
    ("IT84W7093766632581800787511", False),
]


@pytest.mark.parametrize(("value", "found"), _HELD_8, ids=[ascii(v)[:40] for v, _ in _HELD_8])
def test_the_strings_held_for_good_of_version_eight(value, found):
    assert ANCHORS_VERSION == 9 and ONE_RUN_HELD == 16
    assert glued_unfound(value) == found


@pytest.mark.parametrize(
    ("value", "found"),
    [
        ("lily.harris@liberty-d05e.example", "lily.harris@liberty-d05e"),
        ("Lily.Harris@Liberty-D05E.example", "lily.harris@liberty-d05e"),
        ("rqxylehj.tblngz24771@gmail.com", "rqxylehj.tblngz24771@gmail"),
        ("lily+news@liberty.example", None),  # a local part that is no dotted word
        ("lily.harris@", None),
        ("lily.harris", None),
    ],
)
def test_the_word_of_an_address_of_version_eight(value, found):
    assert address_word(value) == found


def test_a_text_has_the_address_words_of_its_addresses_where_it_is_read_for_fakes():
    found = words_in("to a.lily.harris@liberty-d05e.example.com, cc Ab9@b-c.example", dotted=True)
    assert {"lily.harris@liberty-d05e", "a.lily.harris@liberty-d05e", "harris@liberty-d05e", "ab9@b-c"} <= found
    assert not any("@" in word for word in words_in("to lily.harris@liberty-d05e.example"))  # read for values


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("Bianchiops@gktk.fzy8n@liberty-6a4e.example", "gktk.fzy8n@liberty-6a4e"),  # the fuzz's: after an `@`
        ("&#39;_@sophia.white@liberty-6a4e.example", "sophia.white@liberty-6a4e"),
        ("a@b@c@lily.harris@liberty-d05e.example", "lily.harris@liberty-d05e"),
    ],
)
def test_an_address_glued_after_another_at_sign_gives_its_word(text, word):
    """An address's local part begins after the `@` before it, where a text glues two: each `@` is read with the run
    before it and the name after it, whatever the address before it took."""
    assert word in words_in(text, dotted=True)
