"""A chapter in another language: English is the chapter (its steps, values, files and checks) and its texts; a
language adds `chapters/<lang>/<chapter>.yaml` with texts only, by step and by the model's turns, each with the
placeholders of its English text. A language with no file for a chapter, or none at all, is English."""

import re
from importlib import resources
from pathlib import Path

import pytest
import yaml

from latet.tutorial import chapters

from .test_chapters import REVIEWED, _text

XX_BASICS = """\
title: Lé bàsì
steps:
  - title: Uñ çlé
    turns: { write: { text: "Çlé prèsà. Scrìvo." } }
"""


@pytest.fixture
def folder(tmp_path, monkeypatch) -> Path:
    """The package's English chapters, copied, and the language `xx` with texts for basics."""
    for entry in (resources.files("latet.tutorial") / "chapters").iterdir():
        if entry.name.endswith(".yaml"):
            (tmp_path / entry.name).write_bytes(entry.read_bytes())
    (tmp_path / "xx").mkdir()
    (tmp_path / "xx" / "basics.yaml").write_text(XX_BASICS, encoding="utf-8")
    monkeypatch.setattr(chapters, "_folder", lambda: tmp_path)
    return tmp_path


def test_every_folder_of_the_package_s_chapters_is_a_language_of_chapters_only():
    """What the package ships besides English: each folder a language that holds chapters' texts only
    (`<chapter>.yaml`), so none is left unread (a misspelt chapter, a `.yml`)."""
    package = resources.files("latet.tutorial") / "chapters"
    folders = [entry for entry in package.iterdir() if entry.is_dir()]
    assert sorted(entry.name for entry in folders) == [lang for lang in chapters.languages() if lang != "en"]
    names = {f"{cid}.yaml" for cid in chapters.ORDER}
    for folder in folders:
        assert sorted(entry.name for entry in folder.iterdir() if entry.name not in names) == [], folder.name


@pytest.mark.parametrize("lang", chapters.languages())
def test_every_chapter_loads_in_every_language_the_package_ships(lang):
    assert list(chapters.load_all(lang)) == list(chapters.load_all())


def test_a_language_lists_itself_when_its_folder_holds_a_chapter(folder):
    assert chapters.languages() == ("en", "xx")


def test_a_folder_without_a_chapter_is_no_language(folder):
    (folder / "it").mkdir()
    (folder / "it" / "notes.md").write_bytes(b"to do\n")
    assert chapters.languages() == ("en", "xx")


def test_the_texts_come_from_the_language_and_the_rest_from_english(folder):
    en, xx = chapters.load("basics"), chapters.load("basics", "xx")
    assert xx.title == "Lé bàsì" and xx.steps[0].title == "Uñ çlé"
    assert xx.steps[1].title == en.steps[1].title  # a step the file leaves out stays English
    assert (xx.values, xx.files, xx.received) == (en.values, en.files, en.received)
    assert [c.model_dump() for c in xx.steps[0].checks] == [c.model_dump() for c in en.steps[0].checks]


def test_a_turn_is_translated_by_its_id(folder):
    write = next(t for t in chapters.load("basics", "xx").steps[0].model if t.id == "write")
    assert write.text == "Çlé prèsà. Scrìvo."


def test_an_unknown_language_is_english(folder):
    assert chapters.load("basics", "pt") == chapters.load("basics")


def test_an_empty_file_translates_nothing_yet(folder):
    (folder / "xx" / "basics.yaml").write_bytes(b"")
    assert chapters.load("basics", "xx") == chapters.load("basics")


def test_every_chapter_in_a_language_is_its_texts_or_english(folder):
    """A chapter the language has no file for is English."""
    loaded = chapters.load_all("xx")
    # Every chapter that is written, in the menu's order (a chapter of ORDER can come before its file does).
    assert list(loaded) == [cid for cid in chapters.ORDER if (folder / f"{cid}.yaml").is_file()]
    assert loaded["basics"].title == "Lé bàsì" and loaded["terms"] == chapters.load("terms")


def test_a_file_in_a_language_s_folder_is_a_chapter_of_its_name(folder):
    """As in English: a language's file named after no chapter (a misspelt one, say) is refused, never left aside."""
    (folder / "xx" / "false_positives.yaml").write_text("title: Fàlsì\n", encoding="utf-8")
    stray = re.escape("not chapters of the tutorial (ORDER): xx/false_positives.yaml")
    with pytest.raises(chapters.ChapterError, match=stray):
        chapters.load_all("xx")


@pytest.mark.parametrize(
    ("overlay", "says"),
    [
        ("steps: [{}, {}, {}, {}, {}]", "5 steps, the chapter has 4"),
        ("steps: [{title: x, colour: y}]", "colour"),
        ("steps: [{hint: x}]", "step 1 has no hint in English"),
        ("steps: [{turns: {nope: {text: x}}}]", "no turn `nope`"),
        ("learn: [a, b]", "learn: 2 lines, English has 3"),
        ("steps: [{}, {explain: 'no placeholders here'}]", "step 2 explain"),
        ("steps: [{prompt: 'read {{value:secret}}'}]", "{{value:secret}}"),
    ],
)
def test_a_translation_that_does_not_fit_is_refused(folder, overlay, says):
    (folder / "xx" / "basics.yaml").write_text(overlay, encoding="utf-8")
    with pytest.raises(chapters.ChapterError, match=re.escape(says)) as refused:
        chapters.load("basics", "xx")
    assert str(refused.value).startswith("chapters/xx/basics.yaml: ")  # the file, then the place


def test_a_capture_is_built_from_the_template_in_its_language(folder):
    (folder / "xx" / "live.yaml").write_text(
        'steps: [{prompt: "scrivi a {{value:name}}, le è stato addebitato due volte. la sua email è {{value:email}}"}]',
        encoding="utf-8",
    )
    who = chapters.load("live", "xx").received["WHO"]
    assert who.pattern == re.escape("scrivi a ") + "(.+?)" + re.escape(
        ", le è stato addebitato due volte. la sua email è "
    )
    assert (
        re.search(who.pattern, "scrivi a Evelyn Young, le è stato addebitato due volte. la sua email è x@y").group(1)
        == "Evelyn Young"
    )
    assert chapters.load("live").received["WHO"].pattern == re.escape("draft a reply to ") + "(.+?)" + re.escape(
        ", she was charged twice. her address is "
    )


def test_a_template_capture_needs_a_user_text_that_holds_its_value(tmp_path, monkeypatch):
    """A capture by value finds the value by the words around it where the user typed it: a value no prompt holds (nor
    a line the user said in a conversation resumed) has none."""
    chapter = {
        "id": "terms",
        "title": "Your own terms",
        "minutes": 3,
        "phase": 1,
        "learn": ["one", "two", "three"],
        "values": {"codename": "Project Falcon"},
        "received": {"ASKED": {"value": "codename", "in": "user"}},
        "steps": [
            {
                "kind": "watch",
                "title": "Asked",
                "prompt": "what ships next?",
                "button": "Send it",
                "model": [{"id": "again", "text": "No idea."}],
                "explain": "The model got {{received:ASKED}}.",
            }
        ],
        "end": {"learned": ["a", "b", "c"]},
    }
    (tmp_path / "terms.yaml").write_bytes(yaml.safe_dump(chapter, sort_keys=False, allow_unicode=True).encode())
    monkeypatch.setattr(chapters, "_folder", lambda: tmp_path)
    with pytest.raises(chapters.ChapterError, match=re.escape("received.ASKED: no prompt holds {{value:codename}}")):
        chapters.load("terms")


def test_a_dropped_placeholder_is_named_where_it_is_missing(folder):
    (folder / "xx" / "basics.yaml").write_text("steps: [{}, {explain: 'no placeholders here'}]", encoding="utf-8")
    with pytest.raises(chapters.ChapterError, match=re.escape("step 2 explain: {{received:DEMO_API_KEY}}")):
        chapters.load("basics", "xx")


def test_the_guidance_written_in_comes_in_the_language(folder):
    (folder / "llm.yaml").write_text(_text(REVIEWED), encoding="utf-8")
    (folder / "xx" / "llm.yaml").write_text("steps: [{}, {guidance: Lì nòmì dèì rèpàrtì.}]", encoding="utf-8")
    wait = chapters.load("llm", "xx").steps[1].wait
    assert wait is not None and wait.target == "Lì nòmì dèì rèpàrtì."


def test_a_guidance_where_english_has_none_is_refused(folder):
    (folder / "xx" / "basics.yaml").write_text("steps: [{guidance: x}]", encoding="utf-8")
    with pytest.raises(chapters.ChapterError, match="step 1 has no guidance in English"):
        chapters.load("basics", "xx")
