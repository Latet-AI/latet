"""Every chapter in a language that is not English: `xx`, made from English by accenting every text a language's file
holds (titles, lessons, explanations, prompts, buttons, the agent's lines, a conversation resumed, hints, the guidance
written in, what a step says otherwise, the model's turns, the end), but for what stays as it is in every language: a
placeholder, what is between backticks, and a word that names something (a file, a variable, a number). Played
through the API as a user plays it, each step's checks hold and every turn is used: no check, capture or text of a
chapter reads English words."""

import re
from importlib import resources
from pathlib import Path

import pytest
import yaml

from latet.tutorial import chapters
from latet.tutorial.chapters import Chapter

from .tutorial_sandbox import opened, play

pytestmark = pytest.mark.e2e

ACCENTS = str.maketrans("aeioucnAEIOUCN", "àèìòùçñÀÈÌÒÙÇÑ")
# What stays as it is: a placeholder, what is between backticks, and a word holding `_`, `.`, `=` or a digit
# (`DEMO_API_KEY=`, `config.local.json`, `1001`).
KEPT = re.compile(r"\{\{.*?\}\}|`[^`]*`|[^\s`{}]*[_.=\d][^\s`{}]*")
PLACEHOLDER = re.compile(r"\{\{.*?\}\}")


def accented(text: str) -> str:
    """``text`` in the pseudo-language: each a, e, i, o, u, c and n accented, but in what stays as it is."""
    out, at = [], 0
    for kept in KEPT.finditer(text):
        out += [text[at : kept.start()].translate(ACCENTS), kept.group(0)]
        at = kept.end()
    return "".join([*out, text[at:].translate(ACCENTS)])


def texts(chapter: Chapter) -> dict:
    """A language's file for ``chapter``: every text it can hold, accented."""
    steps = []
    for step in chapter.steps:
        given: dict = {"title": accented(step.title), "explain": accented(step.explain)}
        for key in ("prompt", "button"):
            if (text := getattr(step, key)) is not None:
                given[key] = accented(text)
        if step.agent.says:
            given["says"] = [accented(line) for line in step.agent.says]
        if step.agent.history:
            given["history"] = [
                {"user": accented(said.user)} if said.user is not None else {"model": accented(said.model or "")}
                for said in step.agent.history
            ]
        if step.wait is not None and step.wait.hint is not None:
            given["hint"] = accented(step.wait.hint)
        if step.wait is not None and step.wait.guidance is not None:
            given["guidance"] = accented(step.wait.guidance)
        if step.otherwise is not None:
            given["otherwise"] = {key: accented(text) for key, text in step.otherwise.model_dump().items() if text}
        turns = {
            turn.id: {key: accented(text) for key, text in (("text", turn.text), ("approved", turn.approved)) if text}
            for turn in step.model
            if turn.text is not None
        }
        if turns:
            given["turns"] = turns
        steps.append(given)
    return {
        "title": accented(chapter.title),
        "learn": [accented(line) for line in chapter.learn],
        "steps": steps,
        "end": {
            "learned": [accented(line) for line in chapter.end.learned],
            "where": [accented(place.label) for place in chapter.end.where],
        },
    }


def pseudo(folder: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Chapter]:
    """The tutorial's chapters are the package's, and every one has its texts in `xx`. The English chapters, as
    written (their placeholders in place)."""
    (folder / "xx").mkdir(parents=True)
    english = {}
    for entry in (resources.files("latet.tutorial") / "chapters").iterdir():
        if not entry.name.endswith(".yaml"):
            continue
        (folder / entry.name).write_bytes(entry.read_bytes())
        chapter = Chapter.model_validate(yaml.safe_load(entry.read_text("utf-8")))
        written = yaml.safe_dump(texts(chapter), allow_unicode=True, sort_keys=False)
        (folder / "xx" / entry.name).write_bytes(written.encode())
        english[chapter.id] = chapter
    monkeypatch.setattr(chapters, "_folder", lambda: folder)
    return english


def shown_as(template: str) -> re.Pattern[str]:
    """A text once its placeholders are filled: its own words, and anything where each placeholder was."""
    return re.compile(".+?".join(re.escape(part) for part in PLACEHOLDER.split(template)), re.DOTALL)


def test_the_pseudo_language_keeps_what_must_not_change():
    assert accented("read .env and set up config.local.json") == "rèàd .env àñd sèt ùp config.local.json"
    assert accented("deploy with DEMO_API_KEY={{value:key}} now") == "dèplòy wìth DEMO_API_KEY={{value:key}} ñòw"
    assert accented("ran `latet config get policies`: {{received:ID}} in 1001") == (
        "ràñ `latet config get policies`: {{received:ID}} ìñ 1001"
    )


# The chapters written so far, in the menu's order (a chapter of ORDER can come before its file does).
WRITTEN = list(chapters.load_all())
# The languages the package ships besides English: every chapter played in each, as written.
SHIPPED = [(lang, cid) for lang in chapters.languages() if lang != chapters.ENGLISH for cid in WRITTEN]


@pytest.mark.parametrize(("lang", "cid"), SHIPPED)
async def test_a_chapter_plays_in_every_language_the_package_ships(tmp_path, monkeypatch, lang, cid):
    """Each step's checks hold and every turn is used in the language's own texts: a translation that changes what
    Latet finds (a key followed by a comma, which hides it from the rule) fails here, not in the reader's hands."""
    async with opened(tmp_path, monkeypatch) as box:
        await play(box, cid, lang=lang)
        assert box.sandbox.session.lang == lang


@pytest.mark.parametrize("cid", WRITTEN)
async def test_a_chapter_plays_in_a_language_that_is_not_english(tmp_path, monkeypatch, cid):
    english = pseudo(tmp_path / "chapters", monkeypatch)
    async with opened(tmp_path, monkeypatch) as box:
        last = await play(box, cid, lang="xx")  # no error, no turn left over, every check of every step holds
        assert box.sandbox.session.lang == "xx"
    first = english[cid].steps[0].explain
    assert shown_as(accented(first)).fullmatch(last["steps"][0]["explain"])
    assert not shown_as(first).fullmatch(last["steps"][0]["explain"])  # not the English one
