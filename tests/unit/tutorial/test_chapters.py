"""The tutorial's chapters: one YAML file each, shipped in the package and checked when loaded. A file with a
mistake is refused with a reason; the chapter's invented values take the place of their placeholders, and what
the model received is filled in only when it has received it."""

import copy
import re
from importlib import resources

import pytest
import yaml

from latet.config import Config
from latet.engine.build import build
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.gitleaks import GitleaksDetector
from latet.engine.detectors.misread import repaired_iban
from latet.engine.detectors.pii import iban_valid
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.tutorial import chapters
from latet.tutorial.chapters import (
    ENGLISH,
    LAST,
    ORDER,
    WAITS,
    ChapterError,
    NotReceived,
    languages,
    load,
    load_all,
    parse,
    relative,
    render,
    render_json,
)
from latet.tutorial.sandbox import BASE

from ...fake_ocr import reading

KEY = "3D8QnFhA0QhAC9t5j7t6JtgLgPWLFMHbLRuT7TWZ"  # the M0 probe's key: found by gitleaks' generic rule


def _chapter() -> dict:
    """A small chapter that loads: a file read, an answer that names what the model got, a value to click."""
    return {
        "id": "terms",
        "title": "Your own terms",
        "minutes": 3,
        "phase": 1,
        "learn": ["one", "two", "three"],
        "values": {"secret": "s3cr3t-Value-12345"},
        "settings": {},
        "files": {"notes.md": "the secret is {{value:secret}}\n"},
        "received": {"SECRET": {"pattern": r"secret is (\S+)", "in": "tool_result"}},
        "steps": [
            {
                "kind": "watch",
                "title": "Read it",
                "prompt": "read notes.md",
                "button": "Send it",
                "agent": {"files": ["notes.md"], "says": ["On it."]},
                "model": [
                    {"id": "read", "tool": {"name": "Read", "input": {"file_path": "notes.md"}}},
                    {"id": "said", "text": "It says {{received:SECRET}}."},
                ],
                "show": {"exchange": "said", "part": "request", "highlight": ["{{value:secret}}"]},
                "explain": "The model got {{received:SECRET}} instead of {{value:secret}}.",
                "checks": [
                    {"model_received": {"not_contains": ["{{value:secret}}"]}},
                    {"received": {"name": "SECRET", "not": "{{value:secret}}"}},
                    {"exchange": {"ref": "said", "mode": "rewrite"}},
                ],
            },
            {
                "kind": "do",
                "title": "Click it",
                "wait": {"pin": "{{value:secret}}"},
                "show": "previous",
                "explain": "Click the highlighted value.",
            },
        ],
        "end": {
            "learned": ["a", "b", "c"],
            "where": [{"label": "The request", "to": "exchange:said"}, {"label": "Live", "to": "/live"}],
            "next": "basics",
        },
    }


# A chapter with the Tutorial Reviewer: what it finds, and a step that waits for the guidance written in for the reader.
REVIEWED = {
    "id": "llm",
    "title": "The local reviewer",
    "minutes": 4,
    "phase": 2,
    "learn": ["one", "two", "three"],
    "values": {"name": "Daniel Okafor", "ward": "Ward 7B"},
    "settings": {"llm_review": {"auto_mask": ["person", "confidential"]}},
    "reviewer": [
        {"value": "{{value:name}}", "category": "person"},
        {"value": "{{value:ward}}", "category": "confidential", "guidance": True},
    ],
    "files": {"note.md": "{{value:name}} is on {{value:ward}}.\n"},
    "steps": [
        {
            "kind": "watch",
            "title": "Read it",
            "prompt": "read note.md",
            "button": "Send it",
            "agent": {"files": ["note.md"]},
            "model": [
                {"id": "read", "tool": {"name": "Read", "input": {"file_path": "note.md"}}},
                {"id": "said", "text": "Read."},
            ],
            "show": {"component": "review", "exchange": "said", "highlight": ["{{value:name}}"]},
            "explain": "The reviewer found the name.",
        },
        {
            "kind": "do",
            "title": "Your guidance",
            "wait": {"guidance": "Ward names are confidential.", "hint": "Save it."},
            "look": {"component": "guidance"},
            "show": {"component": "guidance"},
            "explain": "Save your guidance.",
        },
    ],
    "end": {"learned": ["a", "b", "c"], "where": [{"label": "Rules", "to": "/rules#reviewer"}], "next": None},
}


def test_a_chapter_with_the_tutorial_reviewer_says_what_it_finds_and_waits_for_guidance():
    chapter = parse(_text(REVIEWED))
    assert [(f.value, f.category, f.guidance) for f in chapter.reviewer] == [
        ("Daniel Okafor", "person", False),
        ("Ward 7B", "confidential", True),
    ]
    wait = chapter.steps[1].wait
    assert wait is not None and wait.kind == "guidance" and wait.target == "Ward names are confidential."
    assert chapter.settings == {"llm_review": {"auto_mask": ["person", "confidential"]}}
    assert chapter.show_of(0) is not None and chapter.show_of(0).component == "review"


def test_a_chapter_can_hold_a_picture_need_text_recognition_and_show_an_image_as_it_was():
    data = _chapter()
    data["needs"] = ["ocr"]
    data["pictures"] = {"shots/payment.png": "payment.png"}
    _step(data)["show"] = {"component": "images", "exchange": "said", "original": "shots/payment.png"}
    chapter = parse(_text(data))
    assert chapter.needs == ["ocr"] and chapter.pictures == {"shots/payment.png": "payment.png"}
    assert chapters.asset("payment.png").startswith(b"\x89PNG\r\n")


def test_a_picture_is_of_the_type_its_name_says_in_any_case():
    data = _chapter()
    data["pictures"] = {"shots/Payment.PNG": "payment.png"}
    assert parse(_text(data)).pictures == {"shots/Payment.PNG": "payment.png"}


@pytest.mark.parametrize(
    "name",
    ["../chapters.py", "..\\chapters.py", "chapters/basics.yaml", "..", ".", "", "PAYMENT.PNG", "nowhere.png"],
    ids=["parent", "parent, backslash", "subfolder", "dotdot", "dot", "empty", "another case", "none such"],
)
def test_asset_reads_only_a_file_of_the_assets_folder_by_the_name_the_package_ships_it_under(name):
    with pytest.raises(ValueError, match="no file"):
        chapters.asset(name)


def _text(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _step(data: dict, index: int = 0) -> dict:
    return data["steps"][index]


def test_a_chapter_loads_with_its_values_in_place():
    chapter = parse(_text(_chapter()))
    assert chapter.id == "terms" and chapter.values == {"secret": "s3cr3t-Value-12345"}
    assert chapter.files == {"notes.md": "the secret is s3cr3t-Value-12345\n"}
    first, second = chapter.steps
    assert first.show is not None and first.show != "previous"
    assert first.show.highlight == ["s3cr3t-Value-12345"]
    assert second.wait is not None and (second.wait.kind, second.wait.target) == ("pin", "s3cr3t-Value-12345")
    assert first.checks[1].received is not None and first.checks[1].received.differs == "s3cr3t-Value-12345"
    # What the model received is known only when it has received it: those placeholders stay.
    assert first.explain == "The model got {{received:SECRET}} instead of s3cr3t-Value-12345."
    assert first.model[1].text == "It says {{received:SECRET}}."
    assert chapter.show_of(1) is first.show  # `previous` shows what the step before showed
    assert chapter.received["SECRET"].where == "tool_result"


def test_what_the_model_received_is_filled_in_once_it_is_known():
    assert render("It says {{received:SECRET}}.", {"SECRET": "fake-1"}) == "It says fake-1."
    given = {"file_path": "a.json", "content": '{"key": "{{received:SECRET}}"}', "n": [1, "{{received:SECRET}}"]}
    assert render_json(given, {"SECRET": "fake-1"}) == {
        "file_path": "a.json",
        "content": '{"key": "fake-1"}',
        "n": [1, "fake-1"],
    }
    with pytest.raises(NotReceived) as missing:
        render("It says {{received:SECRET}}.", {})
    assert missing.value.name == "SECRET"
    with pytest.raises(NotReceived):  # nothing found is not found: never an empty text in its place
        render("It says {{received:SECRET}}.", {"SECRET": ""})


@pytest.mark.parametrize(
    ("typed", "found"),
    [
        ("the secret is {{value:secret}}, keep it", re.escape("the secret is ") + "(.+?)" + re.escape(", keep it")),
        ("keep {{value:secret}}", re.escape("keep ") + "(.+)"),  # nothing follows it
        ("{{value:secret}} is kept", "(.+?)" + re.escape(" is kept")),  # nothing before it
        ("{{value:other}} and {{value:secret}}", re.escape(" and ") + "(.+)"),  # the words since its neighbour
        ("keep {{value:secret}} and {{value:other}}", re.escape("keep ") + "(.+?)" + re.escape(" and ")),
    ],
)
def test_a_capture_by_value_is_found_by_the_words_around_it_where_the_user_typed_it(typed, found):
    """What the user typed is found by the words of the text itself, in the chapter's language: no pattern to write."""
    data = _chapter()
    data["values"]["other"] = "other-Value-678"
    data["received"]["TYPED"] = {"value": "secret", "in": "user"}
    _step(data)["prompt"] = typed
    assert parse(_text(data)).received["TYPED"].pattern == found


def test_a_capture_by_value_is_found_in_the_first_text_the_user_typed_that_holds_it():
    """A step's prompt, then what the user said in the conversation it resumes, step by step."""
    data = _chapter()
    data["received"]["TYPED"] = {"value": "secret", "in": "user"}
    _step(data)["agent"]["history"] = [{"user": "remember {{value:secret}} for me"}, {"model": "Remembered."}]
    assert parse(_text(data)).received["TYPED"].pattern == re.escape("remember ") + "(.+?)" + re.escape(" for me")
    _step(data)["prompt"] = "forget {{value:secret}} now"
    assert parse(_text(data)).received["TYPED"].pattern == re.escape("forget ") + "(.+?)" + re.escape(" now")


def test_a_check_names_a_turn_of_its_step_the_agent_must_have_got():
    """The turn's text, restored, whatever the language; values are language-free, and stay comparable."""
    data = _chapter()
    _step(data)["checks"] = [{"agent_got": {"turn": "said", "contains": ["{{value:secret}}"]}}]
    check = parse(_text(data)).steps[0].checks[0].agent_got
    assert check is not None and (check.turn, check.contains) == ("said", ["s3cr3t-Value-12345"])


def test_what_the_model_received_fills_the_texts_a_check_compares():
    data = _chapter()
    _step(data)["checks"] = [
        {"model_received": {"contains": ["{{received:SECRET}}"], "not_contains": ["{{received:SECRET}}x"]}},
        {"received": {"name": "SECRET", "equals": "{{received:SECRET}}", "not": "{{value:secret}}"}},
        {"file": {"path": "notes.md", "contains": ["{{received:SECRET}}"], "not_contains": ["{{received:SECRET}}"]}},
        {"agent_got": {"contains": ["{{received:SECRET}}"], "not_contains": ["{{received:SECRET}}"]}},
        {"agent_error": {"contains": "{{received:SECRET}}"}},
        {"shown": {"marked": ["{{received:SECRET}}"], "clear": ["{{received:SECRET}}"]}},
    ]
    first = parse(_text(data)).steps[0]
    assert first.checks[0].model_received is not None
    assert first.checks[0].model_received.contains == ["{{received:SECRET}}"]  # filled when the checks run


# What each kind of wait names: a value (pin, term, allow, approve: the value masked by hand in the batch that
# holds it), a setting (change: the top-level key of config.yaml it changes), columns (fields: kept in clear), the
# sentence of guidance written in for the reader (guidance).
TARGETS = {
    "pin": "{{value:secret}}",
    "term": "{{value:secret}}",
    "allow": "{{value:secret}}",
    "approve": "{{value:secret}}",
    "change": "profile",
    "fields": ["id", "plan"],
    "guidance": "Ward names are confidential.",
}


def _held(data: dict) -> None:
    """The `do` step sends its own request, which the Live Reviewer holds until the user decides."""
    _step(data, 1).update(prompt="file it", button="Send it", model=[{"id": "filed", "text": "Filed."}])


def test_every_kind_of_wait_and_a_prepared_command_are_understood():
    assert set(TARGETS) == set(WAITS)
    for kind, target in TARGETS.items():
        data = _chapter()
        _step(data, 1)["wait"] = {kind: target, "hint": "Do it."}
        if kind == "approve":
            _held(data)
        step = parse(_text(data)).steps[1]
        assert step.wait is not None and step.wait.kind == kind and step.wait.hint == "Do it."
        assert step.wait.target == ("s3cr3t-Value-12345" if isinstance(target, str) and "{{" in target else target)
    data = _chapter()
    _step(data)["agent"]["bash"] = {'psql -c "select * from customers limit 5"': " id | name\n----+-----\n"}
    assert parse(_text(data)).steps[0].agent.bash == {
        'psql -c "select * from customers limit 5"': " id | name\n----+-----\n"
    }


def test_a_do_step_shows_what_to_act_on_before_it_runs_and_its_result_after():
    data = _chapter()
    _held(data)
    do = _step(data, 1)
    do["wait"] = {"approve": "{{value:secret}}", "hint": "Mask it, then approve."}
    do["look"] = {"component": "approvals"}
    do["show"] = {"exchange": "filed", "part": "request", "highlight": ["{{value:secret}}"]}
    chapter = parse(_text(data))
    look = chapter.look_of(1)
    assert look is not None and look.component == "approvals" and look.exchange is None
    show = chapter.show_of(1)
    assert show is not None and show.exchange == "filed"
    # `look: previous` is what the step before showed; without a look, a step looks as it shows.
    do["look"] = "previous"
    assert parse(_text(data)).look_of(1) == chapter.show_of(0)
    assert chapter.look_of(0) == chapter.show_of(0)
    # The views the dashboard embeds: the Live Reviewer's queue, the changes waiting, a tool output's columns.
    for view in ({"component": "changes"}, {"component": "columns", "exchange": "said"}):
        do["look"] = view
        assert parse(_text(data)).steps[1].look is not None


def test_a_step_that_waits_in_the_live_reviewer_says_how_it_ended_otherwise():
    """Approved with the value still in clear, or a word of it, or not sent: the step says so, in the chapter's
    words."""
    data = _chapter()
    _held(data)
    do = _step(data, 1)
    do["wait"] = {"approve": "{{value:secret}}"}
    do["otherwise"] = {
        "in_clear": "The model got {{value:secret}} in clear.",
        "part_in_clear": "Part of it was left in clear.",
        "not_sent": "Nothing was sent.",
    }
    otherwise = parse(_text(data)).steps[1].otherwise
    assert otherwise is not None
    assert (otherwise.in_clear, otherwise.part_in_clear, otherwise.not_sent) == (
        "The model got s3cr3t-Value-12345 in clear.",
        "Part of it was left in clear.",
        "Nothing was sent.",
    )
    for alone in ("in_clear", "part_in_clear", "not_sent"):  # any one alone
        do["otherwise"] = {alone: "Said."}
        assert parse(_text(data)).steps[1].otherwise is not None
    do["otherwise"] = {}
    with pytest.raises(ChapterError, match="says how the step ended"):
        parse(_text(data))


def test_a_step_after_a_change_waited_for_says_how_it_went_when_the_change_was_approved():
    """Undo it is the advice where a step waits for a change to the settings: at that step or after it, a step can
    say something else (`otherwise.approved`), and the model answer something else (a turn's `approved`), when the
    user approved the change instead."""
    data = _chapter()
    _step(data, 1)["wait"] = {"change": "profile"}
    data["steps"].append(
        {
            "kind": "watch",
            "title": "Checked",
            "prompt": "what happened to your change?",
            "button": "Send it",
            "model": [{"id": "told", "text": "Undone.", "approved": "Approved: it leaves in clear."}],
            "show": {"component": "none"},
            "explain": "You undid it.",
            "otherwise": {"approved": "You approved it."},
        }
    )
    step = parse(_text(data)).steps[2]
    assert (step.model[0].text, step.model[0].approved) == ("Undone.", "Approved: it leaves in clear.")
    assert step.otherwise is not None and step.otherwise.approved == "You approved it."


def test_a_step_can_resume_a_conversation_from_before_latet_with_the_values_said_then():
    """`agent.history`: what the user and the model said in a conversation from before Latet was on, the user's
    first and the model's last, sent before the step's prompt. What was said then holds the values in clear."""
    data = _chapter()
    step = _step(data)
    step["agent"]["history"] = [
        {"user": "the secret is {{value:secret}}, keep it"},
        {"model": "Kept: {{value:secret}}."},
    ]
    resumed = parse(_text(data)).steps[0].agent.history
    assert [(said.user, said.model) for said in resumed] == [
        ("the secret is s3cr3t-Value-12345, keep it", None),
        (None, "Kept: s3cr3t-Value-12345."),
    ]


def test_a_read_step_runs_nothing_and_a_stopped_request_gets_no_answer():
    data = _chapter()
    data["steps"].append({"kind": "read", "title": "For real", "explain": "Run `latet claude`."})
    data["steps"].append(
        {
            "kind": "watch",
            "title": "Stopped",
            "prompt": "deploy with {{value:secret}}",
            "agent": {"auth": "subscription", "clear": True, "expect": "blocked"},
            "show": {"component": "why", "exchange": LAST, "focus": "why"},
            "explain": "Stopped.",
            "checks": [{"exchange": {"ref": LAST, "blocked": True}}, {"model_received": {"requests": 0}}],
        }
    )
    chapter = parse(_text(data))
    assert [step.kind for step in chapter.steps] == ["watch", "do", "read", "watch"]
    assert chapter.steps[3].agent.expect == "blocked" and chapter.steps[3].prompt == "deploy with s3cr3t-Value-12345"


def test_a_tool_call_left_by_a_step_is_what_the_next_one_sends():
    data = _chapter()
    _step(data)["model"][1]["tool"] = {"name": "Write", "input": {"file_path": "out.txt", "content": "x"}}
    data["steps"].insert(
        1,
        {
            "kind": "watch",
            "title": "Let it write",
            "button": "Let it write",
            "model": [{"id": "done", "text": "Done."}],
            "explain": "Written.",
        },
    )
    assert [step.title for step in parse(_text(data)).steps] == ["Read it", "Let it write", "Click it"]


@pytest.mark.parametrize(
    "another", [{"clear": True}, {"history": [{"user": "a"}, {"model": "b"}]}], ids=["clear", "history"]
)
def test_a_tool_call_left_by_a_step_is_not_sent_in_another_conversation(another):
    """A new conversation (`/clear`), or one resumed from before Latet was on, starts without the tool call the step
    before left: it is not run there, so the step reads none of its files."""
    data = _chapter()
    _step(data)["model"][1]["tool"] = READ  # left for the next step
    data["steps"].insert(
        1,
        {
            "kind": "watch",
            "title": "Another conversation",
            "prompt": "go on",
            "button": "Send it",
            "agent": another,  # no files: it reads nothing
            "model": [{"id": "done", "text": "Done."}],
            "explain": "Done.",
        },
    )
    assert [step.title for step in parse(_text(data)).steps] == ["Read it", "Another conversation", "Click it"]


def _at(data, path: tuple):
    for key in path:
        data = data[key]
    return data


def _set(path: tuple, value):
    def change(data: dict) -> None:
        _at(data, path[:-1])[path[-1]] = value

    return change


def _drop(path: tuple):
    def change(data: dict) -> None:
        del _at(data, path[:-1])[path[-1]]

    return change


def _then(*changes):
    def change(data: dict) -> None:
        for one in changes:
            one(data)

    return change


STEP = ("steps", 0)
DO = ("steps", 1)
READ = {"name": "Read", "input": {"file_path": "notes.md"}}

# Each load-time check, and a file it refuses.
BAD = [
    ("undefined value", _set((*STEP, "prompt"), "read {{value:nope}}"), "no value named nope"),
    ("undefined capture", _set((*STEP, "explain"), "Got {{received:NOPE}}."), "no `received` pattern named NOPE"),
    # A `do` step's explanation is shown before it runs too, when the model has received nothing of it yet.
    (
        "capture in a do step's explanation",
        _set((*DO, "explain"), "Click it: the model got {{received:SECRET}}."),
        "a `do` step's explanation is shown before it runs too: it names nothing the model receives",
    ),
    ("capture in a prompt", _set((*STEP, "prompt"), "read {{received:SECRET}}"), "only the model's turns"),
    ("value in a turn", _set((*STEP, "model", 1, "text"), "It says {{value:secret}}."), "knows only what it received"),
    ("not a placeholder", _set((*STEP, "prompt"), "read {{values:secret}}"), "not a placeholder"),
    ("stray braces", _set((*STEP, "prompt"), "read {{value:secret}"), "not a placeholder"),
    ("placeholder in a value", _set(("values", "other"), "{{value:secret}}"), "a value holds no placeholder"),
    ("placeholder in a key", _set((*STEP, "agent", "bash"), {"echo {{value:secret}}": "x"}), "no placeholder in a key"),
    (
        "placeholder in a pattern",
        _set(("received", "SECRET", "pattern"), "is ({{value:secret}})"),
        "no placeholder here",
    ),
    ("show: unknown turn", _set((*STEP, "show", "exchange"), "nope"), "names `nope`, not a turn of this step or an"),
    (
        "show: a later turn",
        _then(
            _set((*DO, "model"), [{"id": "later", "text": "Later."}]),
            _set((*DO, "prompt"), "again"),
            _set((*STEP, "show", "exchange"), "later"),
        ),
        "names `later`, not a turn of this step or an earlier one",
    ),
    ("show: last, nothing sent", _set((*DO, "show"), {"exchange": LAST, "part": "request"}), "this step sends nothing"),
    ("show: previous first", _set((*STEP, "show"), "previous"), "`show: previous` on the first step"),
    ("show: exchange, no part", _drop((*STEP, "show", "part")), "shows a request or a response"),
    ("check: unknown turn", _set((*STEP, "checks", 2, "exchange", "ref"), "nope"), "names `nope`, not a turn"),
    ("wait on a watch step", _set((*STEP, "wait"), {"pin": "x"}), "only a `do` step waits"),
    ("wait: two predicates", _set((*DO, "wait"), {"pin": "x", "term": "y"}), "exactly one of pin, term"),
    ("wait: none", _set((*DO, "wait"), {"hint": "Do it."}), "exactly one of pin, term"),
    ("wait: unknown kind", _set((*DO, "wait"), {"click": "x"}), "Extra inputs are not permitted"),
    ("wait: nothing named", _set((*DO, "wait"), {"term": ""}), "at least 1 character"),
    ("wait: not a setting", _set((*DO, "wait"), {"change": "colour"}), "wait.change: no setting `colour`"),
    ("wait: no columns", _set((*DO, "wait"), {"fields": []}), "at least 1 item"),
    ("wait: a column pattern", _set((*DO, "wait"), {"fields": ["note_*"]}), "`note_*` is not one column"),
    (
        "approve, nothing sent",
        _set((*DO, "wait"), {"approve": "x"}),
        "an `approve` wait is met while the step's own request waits: give it a prompt, `model` turns and a button",
    ),
    (
        "approve, no button",
        _then(_held, _drop((*DO, "button")), _set((*DO, "wait"), {"approve": "x"})),
        "an `approve` wait is met while the step's own request waits",
    ),
    ("do without wait", _drop((*DO, "wait")), "a `do` step waits for something"),
    (
        "otherwise, no request waiting",
        _set((*DO, "otherwise"), {"not_sent": "Nothing was sent."}),
        "only a step whose request waits in the Live Reviewer (`approve`) says how it ended there",
    ),
    (
        "history: the model first",
        _set((*STEP, "agent", "history"), [{"model": "Hi."}, {"user": "hi"}, {"model": "Hi."}]),
        "history: the user's turn first, then the model's, and so on: the model's last",
    ),
    (
        "history: two of the user's in a row",
        _set((*STEP, "agent", "history"), [{"user": "a"}, {"user": "b"}, {"model": "Hi."}]),
        "history: the user's turn first, then the model's, and so on: the model's last",
    ),
    (
        "history: the user's last",
        _set((*STEP, "agent", "history"), [{"user": "a"}]),
        "history: the user's turn first, then the model's, and so on: the model's last",
    ),
    (
        "history: both in one turn",
        _set((*STEP, "agent", "history"), [{"user": "a", "model": "b"}]),
        "a turn of the history is what the user typed (`user`) or what the model answered (`model`)",
    ),
    (
        "history: a new conversation",
        _then(
            _set((*STEP, "agent", "history"), [{"user": "a"}, {"model": "b"}]), _set((*STEP, "agent", "clear"), True)
        ),
        "a conversation resumed (`history`) is not a new one (`clear`)",
    ),
    (
        "history: no prompt",
        _set((*DO, "agent"), {"history": [{"user": "a"}, {"model": "b"}]}),
        "a conversation resumed (`history`) goes on with the step's prompt",
    ),
    # A conversation resumed runs no tool call the step before left, but its own: their files are still its own.
    (
        "history: reads, not its files",
        _then(_set((*STEP, "agent", "history"), [{"user": "a"}, {"model": "b"}]), _set((*STEP, "agent", "files"), [])),
        "reads `notes.md`, not one of its agent.files",
    ),
    (
        "otherwise approved, no change waited for",
        _set((*DO, "otherwise"), {"approved": "You approved it."}),
        "`approved` says how a change to the settings ended: only at or after a step that waits for one (`change`)",
    ),
    (
        "a turn approved, no change waited for",
        _set((*STEP, "model", 1, "approved"), "Approved."),
        "`approved` says how a change to the settings ended: only at or after a step that waits for one (`change`)",
    ),
    (
        "a turn approved, no text",
        _then(_set((*DO, "wait"), {"change": "profile"}), _set((*STEP, "model", 0, "approved"), "Approved.")),
        "turn `read`: `approved` is said instead of its text: give it a text",
    ),
    (
        "otherwise: nothing said",
        _then(_held, _set((*DO, "wait"), {"approve": "x"}), _set((*DO, "otherwise"), {})),
        "`otherwise` says how the step ended: in_clear, part_in_clear, not_sent, approved, or more",
    ),
    (
        "otherwise: a capture",
        _then(
            _held, _set((*DO, "wait"), {"approve": "x"}), _set((*DO, "otherwise"), {"in_clear": "{{received:SECRET}}"})
        ),
        "what a step says when it did not end as written names nothing the model receives",
    ),
    ("look on a watch step", _set((*STEP, "look"), {"component": "terms"}), "only a `do` step has a `look`"),
    (
        "look: previous first",
        _set(STEP, {"kind": "do", "title": "Act", "wait": {"pin": "x"}, "look": "previous", "explain": "Act."}),
        "`look: previous` on the first step",
    ),
    (
        "look: its own turn",
        _then(_held, _set((*DO, "look"), {"exchange": "filed", "part": "request"})),
        "a look names `filed`, not a turn of an earlier step",
    ),
    ("look: last", _set((*DO, "look"), {"exchange": LAST, "part": "request"}), "a look names `last`, not a turn"),
    ("columns, no exchange", _set((*STEP, "show"), {"component": "columns"}), "a `columns` view names its exchange"),
    ("stopped, answered", _set((*STEP, "agent", "expect"), "blocked"), "a request expected to be stopped gets no"),
    ("turn without text or tool", _set((*STEP, "model", 1), {"id": "said"}), "a text, a tool call, or both"),
    (
        "a text turn before the last",
        _set((*STEP, "model"), [{"id": "said", "text": "It says."}, {"id": "more", "text": "More."}]),
        "`said` has no tool call, so the step ends there: only its last turn may",
    ),
    ("two turns, one id", _set((*STEP, "model", 1, "id"), "read"), "two turns named `read`"),
    ("turn named last", _set((*STEP, "model", 1, "id"), LAST), "`last` names a step's last exchange"),
    ("tool without its input", _set((*STEP, "model", 0, "tool", "input"), {}), "Read needs file_path"),
    ("tool: unknown input", _set((*STEP, "model", 0, "tool", "input", "limit"), "3"), "Read takes file_path"),
    ("absolute path in files", _set(("files", "/etc/passwd"), "x"), "not a path inside the project"),
    ("parent in files", _set(("files", "../notes.md"), "x"), "not a path inside the project"),
    ("backslashes in files", _set(("files", "sub\\notes.md"), "x"), "not a path inside the project"),
    ("drive in agent files", _set((*STEP, "agent", "files"), ["C:/notes.md"]), "not a path inside the project"),
    ("parent in a tool call", _set((*STEP, "model", 0, "tool", "input", "file_path"), "../a"), "not a path inside"),
    (
        "parent in a file check",
        _set((*STEP, "checks"), [{"file": {"path": "a/../../b", "exists": True}}]),
        "not a path inside the project",
    ),
    ("read, not its files", _set((*STEP, "agent", "files"), []), "reads `notes.md`, not one of its agent.files"),
    (
        "watch sends nothing",
        _then(_drop((*STEP, "model")), _drop((*STEP, "show")), _set((*STEP, "checks"), []), _set(("end", "where"), [])),
        "a `watch` step sends a request",
    ),
    (
        "nothing to send",
        _then(_drop((*DO, "wait")), _set((*DO, "kind"), "watch"), _set((*DO, "model"), [{"id": "x", "text": "?"}])),
        "nothing to send: no prompt, and no tool call left by the step before",
    ),
    (
        "a prompt, no answer",
        _set((*DO, "prompt"), "again"),
        "a prompt is sent: give the step `model` turns (or `expect: blocked`)",
    ),
    (
        "a read step runs nothing",
        _then(_set((*DO, "kind"), "read"), _drop((*DO, "wait")), _set((*DO, "model"), [{"id": "x", "tool": READ}])),
        "a `read` step runs nothing",
    ),
    ("pattern does not compile", _set(("received", "SECRET", "pattern"), "is (\\S+"), "does not compile"),
    ("pattern without a group", _set(("received", "SECRET", "pattern"), r"is \S+"), "exactly one group (it has 0)"),
    ("pattern, two groups", _set(("received", "SECRET", "pattern"), r"(is) (\S+)"), "exactly one group (it has 2)"),
    ("capture: unknown place", _set(("received", "SECRET", "in"), "system"), "'any', 'user' or 'tool_result'"),
    (
        "capture: a pattern and a value",
        _set(("received", "SECRET", "value"), "secret"),
        "exactly one of pattern, value",
    ),
    ("capture: neither", _set(("received", "SECRET"), {"in": "user"}), "exactly one of pattern, value"),
    (
        "capture by value, not where the user typed it",
        _set(("received", "SECRET"), {"value": "secret", "in": "tool_result"}),
        "a capture by `value` finds it where the user typed it: `in: user`",
    ),
    ("next: unknown chapter", _set(("end", "next"), "nope"), "end.next: no chapter `nope`"),
    ("next: itself", _set(("end", "next"), "terms"), "end.next: the chapter itself"),
    ("where: not a page", _set(("end", "where", 0, "to"), "live"), "a page (`/live`) or `exchange:<turn>`"),
    ("where: unknown exchange", _set(("end", "where", 0, "to"), "exchange:nope"), "no turn named `nope`"),
    ("id not a chapter", _set(("id",), "nope"), "`nope` is not one of the tutorial's chapters"),
    ("learn: two lines", _set(("learn",), ["one", "two"]), "at least 3 items"),
    ("learned: four lines", _set(("end", "learned"), ["a", "b", "c", "d"]), "at most 3 items"),
    ("extra key", _set(("author",), "me"), "Extra inputs are not permitted"),
    ("extra key in a step", _set((*STEP, "colour"), "red"), "Extra inputs are not permitted"),
    ("check: no kind", _set((*STEP, "checks"), [{}]), "a check is exactly one of"),
    (
        "check: two kinds",
        _set((*STEP, "checks"), [{"agent_got": {"contains": ["x"]}, "agent_error": {"contains": "x"}}]),
        "a check is exactly one of",
    ),
    ("check: nothing to check", _set((*STEP, "checks"), [{"agent_got": {}}]), "says nothing to check"),
    (
        "check: a turn of another step",
        _set((*STEP, "checks"), [{"agent_got": {"turn": "nope"}}]),
        "an agent_got check names turn `nope`, not one of this step's (read, said)",
    ),
    (
        "check: a turn with no text",
        _set((*STEP, "checks"), [{"agent_got": {"turn": "read"}}]),
        "an agent_got check names turn `read`, which says nothing (a tool call only)",
    ),
    (
        "check: not a setting",
        _set((*STEP, "checks"), [{"setting": {"key": "colour.red", "running": "x"}}]),
        "setting: no setting `colour`",
    ),
    (
        "check: a setting, nothing to compare",
        _set((*STEP, "checks"), [{"setting": {"key": "profile"}}]),
        "says nothing to check: give it running or saved",
    ),
    ("check: bad regex", _set((*STEP, "checks"), [{"received": {"name": "SECRET", "matches": "("}}]), "not compile"),
    ("check: unknown capture", _set((*STEP, "checks"), [{"received": {"name": "NOPE", "equals": "x"}}]), "named NOPE"),
    # What the model received fills only the texts a check compares: elsewhere it would be compared as written.
    (
        "capture in an exchange check",
        _set((*STEP, "checks"), [{"exchange": {"ref": "said", "reason_contains": "{{received:SECRET}}"}}]),
        "fills only the texts a check compares",
    ),
    (
        "capture in a rules check",
        _set((*STEP, "checks"), [{"rules": {"term": "{{received:SECRET}}"}}]),
        "fills only the texts a check compares",
    ),
    (
        "capture in a pattern",
        _set((*STEP, "checks"), [{"received": {"name": "SECRET", "matches": "{{received:SECRET}}"}}]),
        "fills only the texts a check compares",
    ),
    ("settings: unknown key", _set(("settings", "colour"), "red"), "settings: no setting `colour`"),
    ("settings: the sandbox's", _set(("settings", "port"), 7000), "settings: `port` is the sandbox's own"),
    ("settings: bad value", _set(("settings", "ner"), "maybe"), "Input should be 'auto' or 'off'"),
    ("minutes: none", _set(("minutes",), 0), "greater than 0"),
    # The Tutorial Reviewer: what a chapter says it finds, the settings it may give it, and the view of its findings.
    (
        "reviewer: its settings without it",
        _set(("settings", "llm_review"), {"auto_mask": ["person"]}),
        "`llm_review` is the sandbox's own, but in a chapter with the Tutorial Reviewer",
    ),
    (
        "reviewer: its server",
        _then(
            _set(("reviewer",), [{"value": "{{value:secret}}", "category": "secret"}]),
            _set(("settings", "llm_review"), {"url": "http://127.0.0.1:8080/v1"}),
        ),
        "settings.llm_review: only auto_mask, inline",
    ),
    (
        "reviewer: a value in no text",
        _set(("reviewer",), [{"value": "nowhere at all", "category": "person"}]),
        "reviewer 1: `nowhere at all` is in no file or prompt of the chapter",
    ),
    (
        "reviewer: no such category",
        _set(("reviewer",), [{"value": "{{value:secret}}", "category": "colour"}]),
        "Input should be 'person'",
    ),
    ("show: a review names its exchange", _set((*STEP, "show"), {"component": "review"}), "a `review` view names"),
    # The pictures a chapter's project holds (shipped with the package), what it needs of this computer, and the view
    # of an exchange's images.
    ("pictures: not a picture", _set(("pictures",), {"notes.txt": "payment.png"}), "`notes.txt` is not a picture"),
    ("pictures: none such", _set(("pictures",), {"shot.png": "nowhere.png"}), "no picture `nowhere.png` in the"),
    (
        "pictures: a file too",
        _then(_set(("files", "shot.png"), "x"), _set(("pictures",), {"shot.png": "payment.png"})),
        "`shot.png` is a file and a picture",
    ),
    # A name is the one the package ships (a computer that ignores case would open `PAYMENT.PNG` as `payment.png`), and
    # a picture has the type its path says.
    ("pictures: another case", _set(("pictures",), {"shot.png": "PAYMENT.PNG"}), "no picture `PAYMENT.PNG` in the"),
    (
        "pictures: not its type",
        _set(("pictures",), {"shot.jpg": "payment.png"}),
        "`shot.jpg` is a .jpg, its picture `payment.png` a .png: the same type",
    ),
    ("needs: unknown", _set(("needs",), ["gpu"]), "Input should be 'ocr'"),
    ("show: images name their exchange", _set((*STEP, "show"), {"component": "images"}), "a `images` view names"),
    (
        "show: an original that is no picture of the chapter",
        _set((*STEP, "show"), {"component": "images", "exchange": "said", "original": "nope.png"}),
        "shows `nope.png`, not a picture of the chapter",
    ),
    (
        "look: an original that is no picture of the chapter",
        _set((*DO, "look"), {"component": "images", "exchange": "said", "original": "nope.png"}),
        "shows `nope.png`, not a picture of the chapter",
    ),
]


@pytest.mark.parametrize(("change", "reason"), [(change, reason) for _, change, reason in BAD], ids=[b[0] for b in BAD])
def test_a_chapter_with_a_mistake_is_refused_with_the_reason(change, reason):
    data = copy.deepcopy(_chapter())
    change(data)
    with pytest.raises(ChapterError) as refused:
        parse(_text(data))
    assert reason in str(refused.value)


def test_only_a_mapping_is_a_chapter():
    with pytest.raises(ChapterError, match="a chapter is a mapping"):
        parse("- just a list\n")
    with pytest.raises(ChapterError, match="aliases"):
        parse("id: &a basics\ntitle: *a\n")


@pytest.mark.parametrize(
    ("path", "inside"),
    [
        (".env", True),
        ("src/app/config.local.json", True),
        ("", False),
        ("/etc/passwd", False),
        ("../x", False),
        ("a/../../x", False),
        ("./x", False),
        ("a//b", False),
        ("a/", False),
        ("C:/x", False),
        ("C:x", False),
        ("a\\b", False),
        ("\\\\server\\share\\x", False),
        (" x", False),
        # Windows' devices, whatever follows a dot (`nul.txt` is NUL), in any part: reading CON waits for the console.
        ("CON", False),
        ("con", False),
        ("nul.txt", False),
        ("aux.tar.gz", False),
        ("src/com1/app.py", False),
        ("LPT9.log", False),
        ("conin$", False),
        ("CONOUT$.txt", False),
        ("com¹", False),
        ("prn .txt", False),
        # A trailing dot or space, which Windows drops (`.. ` is `..` there), and what no file name holds.
        ("notes.", False),
        ("a /b", False),
        (".. /x", False),
        ("a/.. /b", False),
        ("a?.md", False),
        ("a*.md", False),
        ('a"b', False),
        ("a<b", False),
        ("a>b", False),
        ("a|b", False),
        ("a\tb", False),
        # Names that only begin as a device's are names.
        ("console.log", True),
        ("COM10", True),
        ("nullable/aux_notes.md", True),
        ("tests/test_checkout.py", True),
    ],
)
def test_a_path_is_inside_the_project_or_refused(path, inside):
    assert relative(path) is inside


def test_the_basics_chapter_loads():
    chapter = load("basics")
    assert chapter.id == "basics" and chapter.phase == 1 and chapter.end.next == "terms"
    assert [step.kind for step in chapter.steps] == ["watch", "watch", "do", "watch"]
    assert chapter.values["key"] == KEY
    assert f"DEMO_API_KEY={KEY}\n" in chapter.files[".env"]
    assert "{{value:" not in yaml.safe_dump(chapter.model_dump(by_alias=True))
    first, second, do, people = chapter.steps
    assert first.prompt == "read .env and set up config.local.json for prod"
    assert first.model[-1].tool is not None and first.model[-1].tool.name == "Write"  # left for "Let it write"
    assert second.prompt is None and second.button == "Let it write"
    assert do.wait is not None and (do.wait.kind, do.wait.target) == ("pin", KEY)
    assert chapter.show_of(2) == second.show
    assert chapter.look_of(2) == second.show  # the value to click is there before the step runs
    assert people.prompt == "email james.wilson@acme.com the invoice, his number is +44 20 7946 0958"


def test_no_detector_finds_what_the_llm_chapter_leaves_to_the_reviewer():
    """The four values of the ticket have no shape Latet's patterns know, in a file and in the text of an image (the
    misread IBANs' detector) alike: a detector that learns one makes the chapter's example untrue, and it changes."""
    chapter = load("llm")
    ticket = chapter.files["ticket-4182.md"]
    masker = build(Config.model_validate(BASE), Vault(b"\x01" * 32)).masker
    spans = [
        (s.start, s.end)
        for kind in (SlotKind.TOOL_RESULT, SlotKind.IMAGE_TEXT)
        for s in masker.detect(ticket, DetectContext(kind))
    ]
    for name in ("name", "condition", "written", "account"):
        at, value = ticket.index(chapter.values[name]), chapter.values[name]
        assert not any(start < at + len(value) and at < end for start, end in spans), value
    assert not iban_valid(chapter.values["account"].replace(" ", ""))
    assert repaired_iban(chapter.values["account"]) is None


def test_the_images_chapter_s_values_are_in_what_text_recognition_reads_in_its_picture():
    chapter = load("images")
    text = reading().text
    assert chapter.pictures == {"payment.png": "payment.png"} and chapter.needs == ["ocr"]
    assert [name for name, value in chapter.values.items() if value not in text] == []


def test_the_ends_of_the_live_and_settings_chapters_open_approvals_on_their_tab():
    """What waits in Approvals is in two tabs: the Live Reviewer's chapter leads to the texts, the settings' to the
    change (`/approvals#texts`, `/approvals#settings`, as dashboard/src/mock/tutorial.ts)."""
    assert [w.to for w in load("live").end.where if w.to.startswith("/approvals")] == ["/approvals#texts"]
    assert [w.to for w in load("settings").end.where if w.to.startswith("/approvals")] == ["/approvals#settings"]


def test_an_unknown_chapter_is_refused():
    with pytest.raises(ChapterError, match="not one of the tutorial's chapters"):
        load("nope")
    with pytest.raises(ChapterError, match="not one of the tutorial's chapters"):
        load("../basics")


def test_a_file_in_the_chapters_folder_is_a_chapter_of_its_name(tmp_path, monkeypatch):
    monkeypatch.setattr(chapters, "_folder", lambda: tmp_path)
    (tmp_path / "terms.yaml").write_bytes(_text({**_chapter(), "id": "stopped"}).encode())
    with pytest.raises(ChapterError, match=r"terms\.yaml: its id is `stopped`"):
        load("terms")
    (tmp_path / "terms.yaml").write_bytes(_text(_chapter()).encode())
    assert list(load_all()) == ["terms"]
    (tmp_path / "extra.yaml").write_bytes(_text(_chapter()).encode())
    with pytest.raises(ChapterError, match="not chapters of the tutorial"):
        load_all()


def _shipped() -> dict[str, str]:
    """The text of each chapter file the package ships, by chapter."""
    folder = resources.files("latet.tutorial") / "chapters"
    files = {entry.name.removesuffix(".yaml"): entry for entry in folder.iterdir() if entry.name.endswith(".yaml")}
    return {cid: files[cid].read_text("utf-8") for cid in ORDER if cid in files}


def _texts(value) -> list[str]:
    """Every text of a chapter as loaded, its values in place."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (str(key), *_texts(item))]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return []


# Words gitleaks' generic rule takes for a sign that a value is no secret: a key holding one is found by nothing
# in a prompt (neither masked nor stopped), whatever its chapter says.
STOPWORDS = ("demo", "fake", "example", "sandbox", "tutorial")
# How real providers' keys begin: a secret scanner takes a value that begins so for a real one.
PROVIDER = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-|sk_live_|pk_live_|rk_live_|AKIA|ASIA|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-"
    r"|xox[abposr]-|AIza|hf_|npm_|pypi-)"
)


def _written(cid: str, lang: str) -> list[str]:
    """Chapter ``cid``'s files as written: the English one, and ``lang``'s texts for it when it has some."""
    folder = resources.files("latet.tutorial") / "chapters"
    written = [(folder / f"{cid}.yaml").read_text("utf-8")]
    if lang != ENGLISH and (translated := folder / lang / f"{cid}.yaml").is_file():
        written.append(translated.read_text("utf-8"))
    return written


@pytest.mark.parametrize(("cid", "lang"), [(cid, lang) for lang in languages() for cid in _shipped()])
def test_secret_scanners_find_in_a_chapter_its_invented_key_only(cid, lang):
    """The vendored gitleaks rules over every text of the chapter, as written and as loaded, in every language the
    package ships: the generic rule finds each of its keys (a value named key, token, secret or password), whole, and
    nothing else is found. So Latet masks or stops the key as the chapter says, in every language (a translation that
    puts a dot or a comma right after a key changes what the rule finds), and a secret scanner sees no provider's
    key."""
    chapter = load(cid, lang)
    texts = [*_written(cid, lang), *_texts(chapter.model_dump(by_alias=True))]
    keys = {value for name, value in chapter.values.items() if re.search("key|token|secret|password", name)}
    gitleaks, found = GitleaksDetector(), set()
    for text in texts:
        found |= {(s.rule, text[s.start : s.end]) for s in gitleaks.detect(text, DetectContext(SlotKind.TOOL_RESULT))}
    assert found == {("gitleaks:generic-api-key", key) for key in keys}
    assert [key for key in keys if any(word in key.lower() for word in STOPWORDS)] == []
    assert [text for text in texts if PROVIDER.search(text)] == []


# On a narrow screen the page puts Latet's pane below the agent's: "on the right" would be wrong there. In every
# language the tutorial comes in.
ON_SCREEN = {
    "en": re.compile(r"(?i)\b(?:on|to) the (?:right|left)\b|\b(?:right|left)(?:-hand)? (?:pane|side|column)\b"),
    "it": re.compile(
        r"(?i)\b(?:a|sulla|alla|dalla)(?: tua)? (?:destra|sinistra)\b"
        r"|\b(?:riquadro|pannello|colonna|lato|parte) (?:di |a )?(?:destr|sinistr)[ao]\b"
    ),
    # "links" in lowercase only: "Links" is also a link's plural.
    "de": re.compile(r"\b(?:[Rr]echts|links)\b|(?i:\b(?:recht|link)e[nmrs]? (?:Bereich|Seite|Spalte|Fenster)\b)"),
    "fr": re.compile(
        r"(?i)\b(?:à|sur la|sur votre) (?:droite|gauche)\b"
        r"|\b(?:volet|panneau|colonne|partie|côté) (?:de )?(?:droit|gauche)e?\b"
    ),
    "es": re.compile(
        r"(?i)\b(?:a|de) (?:la|tu) (?:derecha|izquierda)\b"
        r"|\b(?:panel|recuadro|columna|lado|parte) (?:de la )?(?:derech|izquierd)[oa]\b"
    ),
}
# Each language the tutorial comes in, with each chapter.
IN_EVERY_LANGUAGE = [(lang, cid) for lang in chapters.languages() for cid in _shipped()]


def test_every_language_the_tutorial_comes_in_has_its_editorial_rules():
    assert set(chapters.languages()) <= set(ON_SCREEN) == set(JARGON)


@pytest.mark.parametrize(("lang", "cid"), IN_EVERY_LANGUAGE)
def test_a_chapter_never_says_where_on_the_screen_a_pane_is(lang, cid):
    assert [text for text in _texts(load(cid, lang).model_dump(by_alias=True)) if ON_SCREEN[lang].search(text)] == []


# Words that name how a request is authenticated, or a mode of Latet's by its name: to a user they say nothing of
# what happens, or of what to do. In the other languages, the words for a signature, a subscription and the two
# modes (the mode Verify's name is a common verb there: "verificare", "prüfen").
JARGON = {
    "en": re.compile(r"(?i)\bsign(?:ed|s)?\b|\bsubscriptions?\b|\bPro or Max\b|\bverify\b|\bcheck-only\b"),
    "it": re.compile(
        r"(?i)\bfirm(?:a|e|are|at[oaie])\b|\babbonament[oi]\b|\bPro o Max\b|\bmodalità (?:client|gateway)\b"
    ),
    "de": re.compile(
        r"(?i)\bsigni(?:ert|eren|erte[nmrs]?)\b|\bsignatur(?:en)?\b|\babo(?:nnement)?s?\b|\bPro oder Max\b"
        r"|\b(?:client|gateway)[- ]?modus\b"
    ),
    "fr": re.compile(
        r"(?i)\bsign(?:é|ée|és|ées|er|ature|atures)\b|\babonnements?\b|\bPro ou Max\b|\bmode (?:client|passerelle)\b"
    ),
    "es": re.compile(
        r"(?i)\bfirm(?:a|as|ar|ó|ando|ad[oa]s?)\b|\bsuscripci(?:ón|ones)\b|\bPro o Max\b"
        r"|\bmodo (?:de )?(?:cliente|puerta de enlace)\b"
    ),
}


def _shown(cid: str, lang: str = chapters.ENGLISH) -> list[str]:
    """What the page shows of a chapter, word for word: titles, lessons, prompts, buttons, the agent's lines, a
    conversation resumed, hints, the guidance written in, explanations and its end."""
    chapter = load(cid, lang)
    texts = [chapter.title, *chapter.learn, *chapter.end.learned, *(place.label for place in chapter.end.where)]
    for step in chapter.steps:
        texts += [step.title, step.explain, step.prompt or "", step.button or "", *step.agent.says]
        texts += [said.user or said.model or "" for said in step.agent.history]
        texts += [step.wait.hint or ""] if step.wait is not None else []
        texts += [step.wait.guidance or ""] if step.wait is not None else []
        texts += [text for text in (step.otherwise.model_dump() if step.otherwise else {}).values() if text]
    return texts


@pytest.mark.parametrize(("lang", "cid"), IN_EVERY_LANGUAGE)
def test_a_chapter_says_what_happens_without_naming_how_a_request_is_signed(lang, cid):
    assert [text for text in _shown(cid, lang) if JARGON[lang].search(text)] == []


def test_the_package_ships_every_chapter_and_only_those():
    """The wheel ships what is under src/latet that git does not ignore: the chapter files are found the way
    the installed package finds them, one for each chapter of ORDER, and loaded in its order."""
    folder = resources.files("latet.tutorial") / "chapters"
    shipped = [entry.name.removesuffix(".yaml") for entry in folder.iterdir() if entry.name.endswith(".yaml")]
    assert set(shipped) == set(ORDER)
    loaded = load_all()
    assert list(loaded) == list(ORDER)
    assert all(chapter.id == cid for cid, chapter in loaded.items())


def test_a_chapter_of_the_menu_without_its_file_is_not_written_yet(tmp_path, monkeypatch):
    """A folder without a chapter's file, as a test makes one: that chapter is not written yet, the others load."""
    monkeypatch.setattr(chapters, "_folder", lambda: tmp_path)
    (tmp_path / "terms.yaml").write_bytes(_text(_chapter()).encode())
    with pytest.raises(ChapterError, match="the chapter `stopped` is not written yet"):
        load("stopped")
    assert list(load_all()) == ["terms"]
