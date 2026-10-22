"""The tutorial's HTTP API, under /_latet/api/v1/tutorial/ in the sandbox only. Guarded like the rest of the
dashboard's. `start` puts the sandbox back to the chapter's beginning (vault, history, workspace, settings); `next`
runs a step in the background and answers at once, so a request the Live Reviewer holds waits for the user while the
page polls `state`; a call that does not fit where the chapter is gets 409 with the state to go on from; the state
after a reload is what the page showed (that Start over gives the same fakes is in test_tutorial_chapters.py, for
every chapter); `end` stops the server. And each kind of wait, with chapters made for the test: a term added, a
value marked Not sensitive, a request the Live Reviewer holds, a change to the settings the agent asks for, columns
kept in clear, guidance saved; and the local reviewer, as the Tutorial Reviewer, which a chapter turns on and whose
guidance is the one setting the Rules page may change."""

import asyncio
import copy
import json
import threading
import time
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from latet.config import load_config
from latet.gateway.approvals import Batch
from latet.tutorial import chapters
from latet.tutorial.chapters import load
from latet.tutorial.reviewer import URL
from latet.vision import models

from ..fake_ocr import FakeOcr, payment_reader
from .tutorial_sandbox import (
    API,
    CATEGORY,
    PATIENCE,
    PORT,
    TUTORIAL,
    UI,
    act,
    approve,
    opened,
    play,
    run,
    state,
    written,
)

pytestmark = pytest.mark.e2e

KEY = "3D8QnFhA0QhAC9t5j7t6JtgLgPWLFMHbLRuT7TWZ"
FAKE_KEY = "3G0OoYyX6TrNM8v8g6r5AjrVwCAXUXKoGDzV6URN"  # the sandbox's fixed key makes it: the same at every run
EMAIL = "james.wilson@acme.com"
PHONE = "+44 20 7946 0958"
NAME = "Emily Carter"  # a customer's name: no detector knows it
BASICS = load("basics")
BASICS_FILE = resources.files("latet.tutorial") / "chapters" / "basics.yaml"
STOPPED = load("stopped")


def _chapter(cid: str, steps: list[dict], **parts) -> dict:
    """A chapter made for a test: what it needs to load, and ``parts``."""
    return {
        "id": cid,
        "title": f"The {cid} chapter",
        "minutes": 2,
        "phase": 1,
        "learn": ["one", "two", "three"],
        **parts,
        "steps": steps,
        "end": {"learned": ["a", "b", "c"], "where": [{"label": "Live", "to": "/live"}], "next": "basics"},
    }


# A request the Live Reviewer holds: a detected address, and a name the user masks by hand.
LIVE = _chapter(
    "live",
    values={"name": NAME, "email": EMAIL},
    settings={"live_review": {"enabled": True, "kinds": ["user_text"]}},
    received={"WHO": {"pattern": r"about (.+)'s invoice", "in": "user"}},
    steps=[
        {
            "kind": "do",
            "title": "A request waits for you",
            "prompt": "write to {{value:email}} about {{value:name}}'s invoice",
            "button": "Send it",
            "wait": {"approve": "{{value:name}}", "hint": "Select the name, mask it as a person, then approve."},
            "look": {"component": "approvals"},
            "model": [{"id": "filed", "text": "Writing about {{received:WHO}}'s invoice."}],
            "show": {"exchange": "filed", "part": "request", "highlight": ["{{value:name}}", "{{value:email}}"]},
            "explain": "The request waits for you: mask the name, then approve.",
            "checks": [
                {"model_received": {"not_contains": ["{{value:name}}", "{{value:email}}"]}},
                {"exchange": {"ref": "filed", "masked_classes": ["CUSTOM", "PII"]}},
                {"shown": {"marked": ["{{value:name}}", "{{value:email}}"]}},
                {"agent_got": {"contains": ["Writing about {{value:name}}'s invoice."]}},
            ],
        }
    ],
)

# Two requests the Live Reviewer holds, one step each: the name masked by hand in the first is masked in the second.
TWICE = _chapter(
    "live",
    values={"name": NAME},
    settings={"live_review": {"enabled": True, "kinds": ["user_text"]}},
    steps=[
        {
            "kind": "do",
            "title": f"Request {n}",
            "prompt": f"write to {{{{value:name}}}}, take {n}",
            "button": "Send it",
            "wait": {"approve": "{{value:name}}"},
            "look": {"component": "approvals"},
            "model": [{"id": f"take-{n}", "text": "Written."}],
            "explain": "It waits for you.",
        }
        for n in (1, 2)
    ],
)

# A step to watch whose request the Live Reviewer holds, and whose explanation names what the model receives.
HELD = _chapter(
    "live",
    values={"name": NAME},
    settings={"live_review": {"enabled": True, "kinds": ["user_text"]}},
    received={"WHO": {"pattern": r"write to (.+)", "in": "user"}},
    steps=[
        {
            "kind": "watch",
            "title": "Held",
            "prompt": "write to {{value:name}}",
            "button": "Send it",
            "model": [{"id": "written", "text": "Written to {{received:WHO}}."}],
            "show": {"exchange": "written", "part": "request"},
            "explain": "The model got {{received:WHO}} instead of the name.",
        }
    ],
)

# The agent lowers the rules with the CLI; the user decides; it reads the setting back.
SETTINGS = _chapter(
    "settings",
    received={"PROFILE": {"pattern": r"\A(\w+)\s*\Z", "in": "tool_result"}},
    steps=[
        {
            "kind": "watch",
            "title": "Faster tests",
            "prompt": "make the tests faster",
            "button": "Send it",
            "model": [
                {"id": "lower", "tool": {"name": "Bash", "input": {"command": "latet config set profile secrets"}}},
                {"id": "asked", "text": "Done. It only needs your approval."},
            ],
            "show": {"component": "changes"},
            "explain": "The agent asked to lower your rules: the change waits for you.",
        },
        {
            "kind": "do",
            "title": "You decide",
            "wait": {"change": "profile", "hint": "Undo it."},
            "look": {"component": "changes"},
            "prompt": "so, did it work?",
            "model": [
                {"id": "check", "tool": {"name": "Bash", "input": {"command": "latet config get profile"}}},
                {"id": "told", "text": "The profile is {{received:PROFILE}}."},
            ],
            "explain": "Undo is the safe answer.",
            "checks": [{"agent_got": {"contains": ["The profile is balanced."]}}],
        },
    ],
)

QUERY = 'psql -c "select * from customers limit 2"'
TABLE = (
    "  id  |     name     |         email         | signup_date | plan\n"
    "------+--------------+-----------------------+-------------+------\n"
    " 1001 | Mary Johnson | mary.johnson@acme.com | 2025-01-14  | pro\n"
    " 1002 | James Smith  | james.smith@acme.com  | 2025-02-03  | free\n"
    "(2 rows)\n"
)


def _query(first: str, then: str, text: str) -> list[dict]:
    return [{"id": first, "tool": {"name": "Bash", "input": {"command": QUERY}}}, {"id": then, "text": text}]


# Strict data mode: every value of a table masked, then ids, dates and plans kept in clear.
DATA = _chapter(
    "data",
    settings={"data": "strict"},
    steps=[
        {
            "kind": "watch",
            "title": "A query on production",
            "prompt": "show me the first customers",
            "button": "Send it",
            "agent": {"bash": {QUERY: TABLE}},
            "model": _query("query", "table", "Two customers. I cannot read either."),
            "show": {"component": "columns", "exchange": "table"},
            "explain": "Every value of the table was masked.",
            "checks": [{"model_received": {"not_contains": ["1001", "Mary Johnson", "2025-01-14"]}}],
        },
        {
            "kind": "do",
            "title": "Keep ids, dates and plans in clear",
            "wait": {"fields": ["id", "plan"], "hint": "Keep id and plan in clear."},
            "look": "previous",
            "prompt": "again",
            "agent": {"bash": {QUERY: TABLE}},
            "model": _query("again-query", "again", "Two customers, on pro and free."),
            "show": {"exchange": "again", "part": "request", "highlight": ["1001"]},
            "explain": "Ids and plans say nothing about a person.",
            "checks": [{"model_received": {"contains": ["1001", "| pro"], "not_contains": ["Mary Johnson"]}}],
        },
    ],
)

# A codename in clear, then a term; a test card masked, then Not sensitive.
TERMS = _chapter(
    "terms",
    values={"codename": "Project Falcon", "card": "4242 4242 4242 4242"},
    steps=[
        {
            "kind": "watch",
            "title": "In clear",
            "prompt": "status of {{value:codename}}? pay with {{value:card}}",
            "button": "Send it",
            "model": [{"id": "said", "text": "Fine."}],
            "show": {"exchange": "said", "part": "request", "highlight": ["{{value:codename}}"]},
            "explain": "The codename left in clear; the card did not.",
            "checks": [{"model_received": {"contains": ["{{value:codename}}"], "not_contains": ["{{value:card}}"]}}],
        },
        {
            "kind": "do",
            "title": "Always mask",
            "wait": {"term": "{{value:codename}}"},
            "look": {"component": "terms"},
            "explain": "Add it to your terms.",
        },
        {
            "kind": "do",
            "title": "Not sensitive",
            "wait": {"allow": "{{value:card}}"},
            "look": {"exchange": "said", "part": "request", "highlight": ["{{value:card}}"]},
            "prompt": "again: status of {{value:codename}}? pay with {{value:card}}",
            "model": [{"id": "again", "text": "Fine again."}],
            "show": {"exchange": "again", "part": "request", "highlight": ["{{value:codename}}", "{{value:card}}"]},
            "explain": "Now the codename is masked and the card is not.",
            "checks": [
                {"model_received": {"contains": ["{{value:card}}"], "not_contains": ["{{value:codename}}"]}},
                {"rules": {"term": "{{value:codename}}", "allowed": "{{value:card}}"}},
                {"shown": {"marked": ["{{value:codename}}"], "clear": ["{{value:card}}"]}},
            ],
        },
    ],
)

# A step meant to be stopped, signed in with an API key: Latet masks the key and sends it.
NOT_STOPPED = _chapter(
    "stopped",
    values={"key": KEY},
    steps=[
        {
            "kind": "watch",
            "title": "Meant to be stopped",
            "prompt": "deploy with DEMO_API_KEY={{value:key}}",
            "button": "Send it",
            "agent": {"expect": "blocked"},
            "explain": "Stopped.",
        }
    ],
)


def _watch(turn: str) -> dict:
    """A step to watch: the agent sends its prompt, and the pretend model answers it."""
    return {
        "kind": "watch",
        "title": f"Step {turn}",
        "prompt": f"say {turn}",
        "button": "Send it",
        "model": [{"id": turn, "text": f"{turn.capitalize()}."}],
        "show": {"exchange": turn, "part": "request"},
        "explain": f"The model said {turn}.",
    }


# Two steps to watch, one after the other (under an id the tutorial knows: a chapter is one of its seven).
PAIR = _chapter("false-positives", steps=[_watch("one"), _watch("two")])

# A step meant to be stopped that is not (a mistake of the chapter's), then a step that is fine.
MISTAKE = _chapter("stopped", values={"key": KEY}, steps=[*NOT_STOPPED["steps"], _watch("two")])

# The local reviewer, as the Tutorial Reviewer: a name no detector knows, found; a ward found once guidance is saved,
# in a file it has not read before (a text read before keeps the answer it got then).
REVIEWED = _chapter(
    "llm",
    values={"name": NAME, "ward": "Ward 7B"},
    settings={"llm_review": {"auto_mask": ["person", "confidential"]}},
    reviewer=[
        {"value": "{{value:name}}", "category": "person"},
        {"value": "{{value:ward}}", "category": "confidential", "guidance": True},
    ],
    files={"note.md": "{{value:name}} asked for a refund.\n", "wards.md": "{{value:ward}}: two beds free.\n"},
    steps=[
        {
            "kind": "watch",
            "title": "A name",
            "prompt": "read note.md",
            "button": "Send it",
            "agent": {"files": ["note.md"]},
            "model": [
                {"id": "read", "tool": {"name": "Read", "input": {"file_path": "note.md"}}},
                {"id": "said", "text": "Read."},
            ],
            "show": {"component": "review", "exchange": "said", "highlight": ["{{value:name}}"]},
            "explain": "The reviewer found the name.",
            "checks": [
                {"model_received": {"not_contains": ["{{value:name}}"]}},
                {"exchange": {"ref": "said", "masked_rules": ["llm:person"]}},
            ],
        },
        {
            "kind": "do",
            "title": "Your guidance",
            "wait": {"guidance": "Ward names are confidential.", "hint": "Save it."},
            "look": {"component": "guidance"},
            "show": {"component": "guidance"},
            "explain": "Save your guidance.",
        },
        {
            "kind": "watch",
            "title": "A ward",
            "prompt": "read wards.md",
            "button": "Send it",
            "agent": {"files": ["wards.md"]},
            "model": [
                {"id": "wards", "tool": {"name": "Read", "input": {"file_path": "wards.md"}}},
                {"id": "beds", "text": "Two beds."},
            ],
            "explain": "The reviewer found the ward.",
            "checks": [
                {"model_received": {"not_contains": ["{{value:ward}}"]}},
                {"exchange": {"ref": "beds", "masked_rules": ["llm:confidential"]}},
            ],
        },
    ],
)

# A screenshot the agent reads: the address in it read and covered (the sandbox's reader is the tests' fake one,
# which reads the tutorial's screenshot as the models do).
SCREENSHOT = _chapter(
    "images",
    values={"email": "amelia.hughes@acme.com"},
    settings={"ocr": "auto"},
    needs=["ocr"],
    pictures={"payment.png": "payment.png"},
    steps=[
        {
            "kind": "watch",
            "title": "A screenshot",
            "prompt": "why was the payment in payment.png refused?",
            "button": "Send it",
            "agent": {"files": ["payment.png"]},
            "model": [
                {"id": "read", "tool": {"name": "Read", "input": {"file_path": "payment.png"}}},
                {"id": "why", "text": "The account is closed."},
            ],
            "show": {
                "component": "images",
                "exchange": "why",
                "original": "payment.png",
                "highlight": ["{{value:email}}"],
            },
            "explain": "Latet read the screenshot and covered the address.",
            "checks": [
                {"exchange": {"ref": "why", "flags": ["image:masked"], "masked_rules": ["pii:email"]}},
                {"shown": {"marked": ["{{value:email}}"]}},
            ],
        }
    ],
)


async def _next(client: httpx.AsyncClient, cid: str, body: dict) -> httpx.Response:
    return await client.post(f"{TUTORIAL}/chapters/{cid}/next", json=body, headers=UI)


async def _start(client: httpx.AsyncClient, cid: str) -> dict:
    answer = await client.post(f"{TUTORIAL}/chapters/{cid}/start", headers=UI)
    assert answer.status_code == 200, answer.text
    return answer.json()


async def _batch(client: httpx.AsyncClient) -> dict:
    """The batch waiting in the Live Reviewer, once there is one."""
    deadline = time.monotonic() + PATIENCE
    while time.monotonic() < deadline:
        batches = (await client.get(f"{API}/approvals")).json()["batches"]
        if batches:
            return batches[0]
        await asyncio.sleep(0.01)
    raise AssertionError("nothing came to wait in the Live Reviewer")


async def _settled(client: httpx.AsyncClient, cid: str) -> dict:
    deadline = time.monotonic() + PATIENCE
    now = await state(client, cid)
    while now["busy"]:
        assert time.monotonic() < deadline, "the step never ended"
        await asyncio.sleep(0.01)
        now = await state(client, cid)
    return now


async def test_the_tutorial_needs_the_dashboard_s_sign_in(tmp_path, monkeypatch):
    async with opened(tmp_path, monkeypatch) as box:
        transport = httpx.ASGITransport(app=box.app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{PORT}") as stranger:
            assert (await stranger.get(f"{TUTORIAL}/chapters")).status_code == 401
            assert (await stranger.get(f"{TUTORIAL}/chapters/images/pictures/payment.png")).status_code == 401
            assert (await stranger.post(f"{TUTORIAL}/chapters/basics/start", headers=UI)).status_code == 401
            assert (await stranger.post(f"{TUTORIAL}/end", headers=UI)).status_code == 401
        client = box.client
        assert (await client.post(f"{TUTORIAL}/chapters/basics/start")).status_code == 403  # no X-Latet-UI
        cross = {**UI, "sec-fetch-site": "cross-site"}
        assert (await client.post(f"{TUTORIAL}/chapters/basics/start", headers=cross)).status_code == 403
        foreign = {**UI, "origin": "http://tutorial.example"}
        assert (await client.post(f"{TUTORIAL}/end", headers=foreign)).status_code == 403
        assert box.sandbox.session.chapter is None  # nothing started


async def test_the_menu_a_chapter_and_where_the_tutorial_is(tmp_path, monkeypatch):
    # The chapters written so far are the package's basics.yaml alone, whatever else the package holds.
    written(tmp_path / "chapters", monkeypatch, yaml.safe_load(BASICS_FILE.read_text("utf-8")))
    async with opened(tmp_path, monkeypatch, requested="basics") as box:
        client = box.client
        menu = (await client.get(f"{TUTORIAL}/chapters")).json()
        assert menu["requested"] == "basics" and menu["current"] is None
        assert menu["chapters"] == [
            {
                "id": "basics",
                "number": 1,
                "title": "The basics",
                "minutes": 5,
                "learn": BASICS.learn,
                "phase": 1,
                "done": False,
                "missing": [],
            }
        ]
        assert (await client.get(f"{TUTORIAL}/chapters")).json()["requested"] is None  # given once
        view = (await client.get(f"{TUTORIAL}/chapters/basics")).json()
        assert view["agent"] == {"name": "Tutorial Agent", "model": "Tutorial Model"} and view["number"] == 1
        assert [(s["index"], s["kind"], s["title"], s["button"]) for s in view["steps"]] == [
            (0, "watch", "A key leaves as a fake", "Send it"),
            (1, "watch", "The file holds the real key", "Let it write"),
            (2, "do", "Ask Latet", None),
            (3, "watch", "People too", "Send it"),
        ]
        assert view["steps"][0]["prompt"] == BASICS.steps[0].prompt and view["steps"][1]["prompt"] is None
        assert [s["wait"] for s in view["steps"]] == [None, None, {"kind": "pin", "hint": ""}, None]
        for missing in ("nope", "terms"):  # unknown, and not written yet
            assert (await client.get(f"{TUTORIAL}/chapters/{missing}")).status_code == 404
            assert (await client.post(f"{TUTORIAL}/chapters/{missing}/start", headers=UI)).status_code == 404
        assert (await client.get(f"{API}/me")).json()["tutorial"] == {"current": None, "reviewer": False}
        started = await _start(client, "basics")
        assert started["chapter"] == view and started["state"]["next"] == 0
        current = {"chapter": "basics", "number": 1, "title": "The basics", "step": 0}
        assert (await client.get(f"{API}/me")).json()["tutorial"] == {"current": current, "reviewer": False}
        assert (await client.get(f"{TUTORIAL}/chapters")).json()["current"] == {"chapter": "basics", "step": 0}


# The basics in a language of the test's own, `xx`: its title, and its first step's title and explanation.
XX_BASICS = {
    "title": "Lé bàsì",
    "steps": [{"title": "Uñ çlé", "explain": "Lè àgèñt rèàd .env àñd sèñt ìt tò thè mòdèl."}],
}


def _in_xx(folder: Path, monkeypatch: pytest.MonkeyPatch, texts: dict[str, dict]) -> None:
    """The tutorial's chapters are the package's, and the language `xx` has ``texts`` (by chapter)."""
    folder.mkdir(parents=True)
    for entry in (resources.files("latet.tutorial") / "chapters").iterdir():
        if entry.name.endswith(".yaml"):
            (folder / entry.name).write_bytes(entry.read_bytes())
    (folder / "xx").mkdir()
    for cid, given in texts.items():
        (folder / "xx" / f"{cid}.yaml").write_bytes(yaml.safe_dump(given, allow_unicode=True).encode())
    monkeypatch.setattr(chapters, "_folder", lambda: folder)


async def test_the_reader_s_language_and_the_language_a_run_keeps(tmp_path, monkeypatch):
    """The menu and a chapter come in the page's language (English where it has no texts); a chapter runs in the
    language it started in, whatever the page asks for meanwhile; `/me` names it in that language."""
    _in_xx(tmp_path / "chapters", monkeypatch, {"basics": XX_BASICS})
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client

        async def titles(lang: str) -> dict[str, str]:
            menu = (await client.get(f"{TUTORIAL}/chapters", params={"lang": lang})).json()
            return {card["id"]: card["title"] for card in menu["chapters"]}

        assert (await titles("xx"))["basics"] == "Lé bàsì" and (await titles("xx"))["terms"] == "Your own terms"
        assert (await titles("en"))["basics"] == "The basics"
        assert (await client.get(f"{TUTORIAL}/chapters")).json()["chapters"][0]["title"] == "The basics"
        view = (await client.get(f"{TUTORIAL}/chapters/basics", params={"lang": "xx"})).json()
        assert (view["title"], view["steps"][0]["title"], view["steps"][1]["title"]) == (
            "Lé bàsì",
            "Uñ çlé",
            "The file holds the real key",  # a step the language leaves out: English
        )
        started = await client.post(f"{TUTORIAL}/chapters/basics/start", json={"lang": "xx"}, headers=UI)
        assert started.status_code == 200 and started.json()["chapter"]["title"] == "Lé bàsì"
        done = await run(box, "basics", 0, BASICS.steps[0], [])
        assert done["error"] is None and done["steps"][0]["explain"] == XX_BASICS["steps"][0]["explain"]
        # The reader switches the page to English: the run keeps its language, the menu follows the page.
        assert (await client.get(f"{TUTORIAL}/chapters/basics", params={"lang": "en"})).json()["title"] == "Lé bàsì"
        assert (await client.get(f"{API}/me")).json()["tutorial"]["current"]["title"] == "Lé bàsì"
        assert (await titles("en"))["basics"] == "The basics"
        # A language with no texts is English; a language that is not a code is refused.
        again = await client.post(f"{TUTORIAL}/chapters/basics/start", json={"lang": "pt"}, headers=UI)
        assert again.status_code == 200 and again.json()["chapter"]["title"] == "The basics"
        assert (await client.get(f"{API}/me")).json()["tutorial"]["current"]["title"] == "The basics"
        assert (await client.get(f"{TUTORIAL}/chapters/basics", params={"lang": "xx"})).json()["title"] == "The basics"
        refused = await client.post(f"{TUTORIAL}/chapters/basics/start", json={"lang": 5}, headers=UI)
        assert refused.status_code == 400


async def test_a_step_that_resumes_a_conversation_shows_it_before_anything_is_sent(tmp_path, monkeypatch):
    """As /resume shows a conversation before you type: the chapter's view has the lines the agent's pane shows of the
    conversation a step resumes, the very lines the pane then holds once the step ran. A step that resumes none has
    none."""
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        view = (await client.get(f"{TUTORIAL}/chapters/stopped")).json()
        resumed = view["steps"][0]["resumed"]
        assert resumed == [
            {
                "step": 0,
                "type": "system",
                "text": "/resume: yesterday's conversation, from before Latet was on",
                "note": "resumed",
            },
            {"step": 0, "type": "prompt", "text": STOPPED.steps[0].agent.history[0].user, "resumed": True},
            {"step": 0, "type": "model", "text": "Deploying to prod. I'll tell you when it's done.", "resumed": True},
        ]
        assert KEY in resumed[1]["text"] and view["steps"][1]["resumed"] == []
        started = await _start(client, "stopped")
        assert started["chapter"] == view and started["state"]["lines"] == []  # nothing ran, nothing was sent
        done = await run(box, "stopped", 0, STOPPED.steps[0], [])
        assert done["lines"][: len(resumed)] == resumed and done["lines"][len(resumed)]["text"] == "continue the deploy"


async def test_a_chapter_is_in_progress_until_its_last_step_ran_and_then_it_is_done(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, PAIR)
    async with opened(tmp_path, monkeypatch) as box:
        client, steps = box.client, box.sandbox.session.chapters["false-positives"].steps

        def at(step: int) -> tuple[dict, dict]:
            """What the menu and `/me` say of the chapter in progress, at ``step``."""
            menu = {"chapter": "false-positives", "step": step}
            me = {"chapter": "false-positives", "number": 3, "title": "The false-positives chapter", "step": step}
            return menu, me

        async def standing() -> tuple[dict | None, dict | None, bool]:
            """The menu's and `/me`'s chapter in progress, and whether the chapter's card says done."""
            menu = (await client.get(f"{TUTORIAL}/chapters")).json()
            me = (await client.get(f"{API}/me")).json()["tutorial"]["current"]
            return menu["current"], me, menu["chapters"][0]["done"]

        await _start(client, "false-positives")
        assert await standing() == (*at(0), False)
        await run(box, "false-positives", 0, steps[0], [])
        assert await standing() == (*at(1), False)  # half way: in progress
        await run(box, "false-positives", 1, steps[1], [])
        assert await standing() == (None, None, True)  # every step ran: it is done, not in progress any more
        await _start(client, "false-positives")  # Start again: a new run, in progress, and done still
        assert await standing() == (*at(0), True)


async def test_each_step_says_what_it_shows_and_where_its_values_are(tmp_path, monkeypatch):
    async with opened(tmp_path, monkeypatch) as box:
        client, session = box.client, box.sandbox.session
        await _start(client, "basics")
        await run(box, "basics", 0, BASICS.steps[0], [])
        after = await run(box, "basics", 1, BASICS.steps[1], [])
        write = session.turns["write"]
        detail = (await client.get(f"{API}/exchanges/{write}")).json()  # what the page renders
        first, second, do, people = after["steps"]
        slot = first["shown"]["slot"]
        assert first["shown"] == {
            "component": "exchange",
            "exchange": write,
            "part": "request",
            "slot": slot,
            "highlight": [KEY],
        }
        assert (
            detail["request"]["slots"][slot]["kind"] == "tool_result"
            and KEY in detail["request"]["slots"][slot]["original"]
        )
        assert second["shown"] == {**first["shown"], "part": "response", "slot": 0}
        assert (
            detail["response"]["tool_calls"][0]["name"] == "Write"
            and KEY in detail["response"]["tool_calls"][0]["args_restored"]
        )
        assert second["explain"].startswith(f"The model put its fake ({FAKE_KEY}) in the Write call.")
        # The `do` step says what to do and shows the value to click before it runs; a step to watch, nothing yet.
        assert do == {"index": 2, "ran": False, "explain": BASICS.steps[2].explain, "shown": second["shown"]}
        assert people == {"index": 3, "ran": False, "explain": "", "shown": None}
        assert after["waiting"] == {"step": 2, "kind": "pin", "satisfied": False}
        # What the agent's pane shows: the real key, never its fake; the Write first left for the next step.
        lines = after["lines"]
        assert [(line["step"], line["type"]) for line in lines] == [
            (0, "prompt"),
            (0, "agent"),
            (0, "tool"),
            (0, "model"),
            (0, "tool"),
            (1, "tool"),
            (1, "file"),
            (1, "model"),
        ]
        assert lines[4]["name"] == "Write" and lines[4]["pending"] is False  # the next step ran it
        assert lines[6]["path"] == "config.local.json" and KEY in lines[6]["content"]
        assert FAKE_KEY not in str(lines)
        await run(box, "basics", 2, BASICS.steps[2], ["pin"])
        last = await run(box, "basics", 3, BASICS.steps[3], [])
        email = session.turns["email"]
        ask = last["steps"][3]["shown"]
        assert ask == {
            "component": "exchange",
            "exchange": email,
            "part": "request",
            "slot": ask["slot"],
            "highlight": [EMAIL, PHONE],
        }
        slots = (await client.get(f"{API}/exchanges/{email}")).json()["request"]["slots"]
        assert ask["slot"] == len(slots) - 1 and slots[-1]["kind"] == "user_text"
        assert last["finished"] and last["waiting"] is None and last["error"] is None
        assert last["end"] == {
            "learned": BASICS.end.learned,
            "where": [
                {"label": "Every request, as it happens", "to": "/live"},
                {"label": "The request with the key", "to": f"/exchanges/{write}"},
                {"label": "Every value Latet replaced", "to": "/vault"},
            ],
            "next": "terms",
        }
        assert (await state(client, "basics")) == last  # a reload shows what the page showed


async def test_start_puts_the_sandbox_back_to_the_chapter_s_beginning(tmp_path, monkeypatch):
    async with opened(tmp_path, monkeypatch) as box:
        client, sandbox = box.client, box.sandbox
        rt = sandbox.runtime
        before = (await _start(client, "basics"))["state"]["run"]
        await run(box, "basics", 0, BASICS.steps[0], [])
        await run(box, "basics", 1, BASICS.steps[1], [])
        workspace = sandbox.agent.workspace
        assert (workspace / "config.local.json").is_file() and rt.built.vault.entities()
        assert (await client.get(f"{API}/exchanges")).json()["items"]
        assert (await client.post(f"{API}/terms", json={"value": "Project Falcon"}, headers=UI)).status_code == 200
        (workspace / "stray.txt").write_bytes(b"left by hand")
        again = (await _start(client, "basics"))["state"]
        assert again["run"] != before and again["next"] == 0 and again["lines"] == [] and not again["busy"]
        assert rt.built.vault.entities() == []
        assert (await client.get(f"{API}/exchanges")).json()["items"] == []
        files = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file())
        assert files == [".env", "notes.md"] and sandbox.agent.workspace == workspace
        assert rt.config.terms == [] and load_config(rt.config_file).terms == []
        assert sandbox.model.received == [] and sandbox.model.captured == {}
        assert sandbox.session.turns == {} and sandbox.session.ran == {}


async def test_a_call_that_does_not_fit_where_the_chapter_is_gets_409_and_where_it_is(tmp_path, monkeypatch):
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        for answer in (
            await _next(client, "basics", {"step": 0}),
            await client.get(f"{TUTORIAL}/chapters/basics/state"),
        ):
            assert answer.status_code == 409 and answer.json() == {"error": "not started", "state": None}
        await _start(client, "basics")
        for body in ({}, {"step": "0"}, {"step": True}, {"step": 0, "seen": "pin"}, {"step": 0, "seen": [1]}):
            assert (await _next(client, "basics", body)).status_code == 400, body
        ahead = await _next(client, "basics", {"step": 1})
        assert ahead.status_code == 409 and ahead.json()["error"] == "not this step"
        assert ahead.json()["state"]["next"] == 0
        await run(box, "basics", 0, BASICS.steps[0], [])
        twice = await _next(client, "basics", {"step": 0})  # a second Enter, or a stale tab
        assert twice.status_code == 409 and twice.json()["error"] == "not this step"
        await run(box, "basics", 1, BASICS.steps[1], [])
        waiting = await _next(client, "basics", {"step": 2})  # the value was not opened
        assert waiting.status_code == 409 and waiting.json()["error"] == "waiting"
        assert waiting.json()["state"]["waiting"] == {"step": 2, "kind": "pin", "satisfied": False}
        assert (await _next(client, "basics", {"step": 2, "seen": ["pin"]})).status_code == 200


async def test_end_stops_the_server(tmp_path, monkeypatch):
    async with opened(tmp_path, monkeypatch) as box:
        server = box.app.state.server = SimpleNamespace(should_exit=False)
        answer = await box.client.post(f"{TUTORIAL}/end", headers=UI)
        assert answer.status_code == 200 and answer.json() == {"ending": True}
        assert server.should_exit is True


@pytest.mark.parametrize("made", [LIVE, SETTINGS, DATA, TERMS], ids=lambda chapter: chapter["id"])
async def test_each_kind_of_wait_is_met_as_a_user_meets_it(tmp_path, monkeypatch, made):
    written(tmp_path / "chapters", monkeypatch, made)
    async with opened(tmp_path, monkeypatch) as box:
        await play(box, made["id"])


async def test_a_request_the_live_reviewer_holds_waits_for_the_user_and_next_does_not(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, LIVE)
    async with opened(tmp_path, monkeypatch) as box:
        client, rt = box.client, box.sandbox.runtime
        started = (await _start(client, "live"))["state"]
        assert started["waiting"] == {"step": 0, "kind": "approve", "satisfied": False}
        assert started["steps"][0]["shown"] == {"component": "approvals"}  # where to act, before it runs
        # It answers at once, while the request waits for the user (who decides only after it answered), with what
        # the pane shows of the step so far: the prompt.
        answer = await asyncio.wait_for(_next(client, "live", {"step": 0}), timeout=5)
        assert answer.status_code == 200 and answer.json()["state"]["busy"]
        prompt = box.sandbox.session.chapters["live"].steps[0].prompt
        assert answer.json()["state"]["lines"] == [{"step": 0, "type": "prompt", "text": prompt}]
        batch = await _batch(client)
        assert batch["agent"] == "tutorial-agent" and batch["session"] == box.sandbox.agent.session_key
        [item] = batch["items"]
        assert NAME in item["masked"] and EMAIL not in item["masked"]
        held = await state(client, "live")
        assert held["busy"] and held["waiting"] == {"step": 0, "kind": "approve", "satisfied": False}
        assert [line["type"] for line in held["lines"]] == ["prompt"]  # the pane shows what the agent sent
        assert held["steps"][0]["shown"] == {"component": "approvals"}
        busy = await _next(client, "live", {"step": 0})
        assert busy.status_code == 409 and busy.json()["error"] == "busy" and busy.json()["state"]["busy"]
        # Masked by hand and approved: the wait is met at once (nothing is awaited between the two).
        at = item["original"].index(NAME)
        mask = {"item": 0, "start": at, "end": at + len(NAME), "category": CATEGORY}
        assert await rt.mediator.approvals.decide(batch["id"], True, [mask])
        assert box.sandbox.session.state("live")["waiting"] == {"step": 0, "kind": "approve", "satisfied": True}
        done = await _settled(client, "live")
        assert done["finished"] and done["error"] is None and done["steps"][0]["ran"]
        assert done["steps"][0]["shown"]["component"] == "exchange"
        received = box.sandbox.model.received[0].text
        assert NAME not in received and EMAIL not in received
        entity = rt.built.vault.by_original(NAME)
        assert entity is not None and entity.rule == "human:person"
        assert done["lines"][-1] == {"step": 0, "type": "model", "text": f"Writing about {NAME}'s invoice."}


async def test_a_request_held_is_not_sent_when_the_user_starts_over_ends_or_says_no(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, LIVE)
    async with opened(tmp_path, monkeypatch) as box:
        client, model = box.client, box.sandbox.model
        # Start over while a request waits: it is not sent, and the chapter begins again, not finished.
        await _start(client, "live")
        await _next(client, "live", {"step": 0})
        await _batch(client)
        again = (await _start(client, "live"))["state"]
        assert not again["busy"] and again["next"] == 0 and not again["finished"] and again["lines"] == []
        assert (await client.get(f"{API}/approvals")).json()["batches"] == [] and model.received == []
        # Ending the tutorial gives up a request that waits, too.
        await _next(client, "live", {"step": 0})
        await _batch(client)
        assert (await client.post(f"{TUTORIAL}/end", headers=UI)).status_code == 200
        assert not box.sandbox.session.busy and model.received == []
        ended = await state(client, "live")
        assert ended["next"] == 0 and not ended["finished"] and ended["error"] is None
        assert not box.progress.exists()  # neither counts as the chapter done
        # Don't send: the request is not sent, and that is the user's answer to the step.
        await _next(client, "live", {"step": 0})
        batch = await _batch(client)
        assert (
            await client.post(f"{API}/approvals/{batch['id']}", json={"action": "reject"}, headers=UI)
        ).status_code == 200
        said_no = await _settled(client, "live")
        assert said_no["steps"][0]["ran"] and said_no["error"] is None  # the user's choice, not a failure
        assert said_no["lines"][-1] == {
            "step": 0,
            "type": "error",
            "text": "Latet did not send this request: you did not approve it in the Live Reviewer. Nothing was sent; "
            "see where it was in Latet's pane",
        }
        assert model.received == []
        # The answer the step names never came: it shows why the request was stopped.
        why = said_no["steps"][0]["shown"]
        assert (why["component"], why["focus"], why["part"]) == ("why", "why", "request")
        stopped = (await client.get(f"{API}/exchanges/{why['exchange']}")).json()
        assert "blocked" in stopped["summary"]["flags"] and NAME in stopped["request"]["slots"][why["slot"]]["original"]
        assert said_no["finished"] and box.progress.exists()


async def test_each_approve_waits_for_its_own_request(tmp_path, monkeypatch):
    """A batch decided before is not the step's own: the same step run again after the tutorial's end let its request
    go, or a second step that waits in the Live Reviewer, waits for the request it sends itself."""
    written(tmp_path / "chapters", monkeypatch, TWICE)
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        await _start(client, "live")
        await _next(client, "live", {"step": 0})
        await _batch(client)
        assert (await client.post(f"{TUTORIAL}/end", headers=UI)).status_code == 200  # the request is let go
        assert (await state(client, "live"))["waiting"] == {"step": 0, "kind": "approve", "satisfied": False}
        await _next(client, "live", {"step": 0})
        await _batch(client)
        assert (await state(client, "live"))["waiting"]["satisfied"] is False  # its own request waits
        assert await approve(client, NAME)
        first = await _settled(client, "live")
        assert first["next"] == 1 and first["error"] is None
        # The first step's batch holds the name too, and it was decided.
        assert first["waiting"] == {"step": 1, "kind": "approve", "satisfied": False}
        await _next(client, "live", {"step": 1})
        batch = await _batch(client)
        assert (await state(client, "live"))["waiting"] == {"step": 1, "kind": "approve", "satisfied": False}
        decided = await client.post(f"{API}/approvals/{batch['id']}", json={"action": "approve"}, headers=UI)
        assert decided.status_code == 200
        assert (await _settled(client, "live"))["finished"]


async def test_start_over_while_a_change_waits_leaves_one_settled_state(tmp_path, monkeypatch):
    """Start over undoes a change the agent asked for as Undo it does, under the settings' lock: an answer the user
    gives at the same moment and Start over's are never both taken, one approved and the other rejected."""
    written(tmp_path / "chapters", monkeypatch, SETTINGS)
    async with opened(tmp_path, monkeypatch) as box:
        client, rt = box.client, box.sandbox.runtime
        approvals = rt.mediator.approvals
        await _start(client, "settings")
        await run(box, "settings", 0, box.sandbox.session.chapters["settings"].steps[0], [])
        [change] = (await client.get(f"{API}/changes")).json()["changes"]
        events = approvals.broadcaster.subscribe()
        held, go, answers = threading.Event(), threading.Event(), []

        def user() -> None:  # the user's Approve, decided in a worker thread as the dashboard decides it
            with rt._lock:
                held.set()
                go.wait(PATIENCE)
                answers.append(rt.decide_change(change["id"], True))

        thread = threading.Thread(target=user)
        thread.start()
        try:
            assert await asyncio.to_thread(held.wait, PATIENCE)
            restart = asyncio.create_task(_start(client, "settings"))
            await asyncio.sleep(0.2)
            # Start over waits for the lock: nobody decided the change yet.
            assert change["id"] in approvals.changes and change["id"] not in approvals.settled
        finally:
            go.set()
            await asyncio.to_thread(thread.join, PATIENCE)
        await restart
        await asyncio.sleep(0)  # what the worker thread published reaches the loop
        assert answers == ["approved"] and approvals.settled[change["id"]] == "approved" and approvals.changes == {}
        told = []
        while not events.empty():
            event = events.get_nowait()
            if isinstance(event, tuple) and event[0] == "change_settled":
                told.append((event[1]["id"], event[1]["state"]))
        assert told == [(change["id"], "approved")]  # one answer, said once
        # The chapter's beginning all the same.
        assert rt.config.profile == "balanced" and load_config(rt.config_file).profile == "balanced"


async def test_a_step_whose_request_was_not_sent_still_explains(tmp_path, monkeypatch):
    """Don't send: the step is done, and its explanation, which names what the model would have received, gives way
    to one that says the request was not sent."""
    from latet.tutorial.session import NOT_SENT

    written(tmp_path / "chapters", monkeypatch, HELD)
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        await _start(client, "live")
        await _next(client, "live", {"step": 0})
        batch = await _batch(client)
        said_no = await client.post(f"{API}/approvals/{batch['id']}", json={"action": "reject"}, headers=UI)
        assert said_no.status_code == 200
        done = await _settled(client, "live")
        [step] = done["steps"]
        assert step["ran"] and done["error"] is None and done["finished"]
        assert step["explain"] == NOT_SENT and step["not_sent"] is True and step["shown"]["component"] == "why"


async def test_a_change_to_the_settings_the_agent_asks_for_waits_for_the_user(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, SETTINGS)
    async with opened(tmp_path, monkeypatch) as box:
        client, rt = box.client, box.sandbox.runtime
        await _start(client, "settings")
        asked = await run(box, "settings", 0, box.sandbox.session.chapters["settings"].steps[0], [])
        [tool] = [line for line in asked["lines"] if line["type"] == "tool"]
        assert tool["summary"] == "latet config set profile secrets"
        [change] = (await client.get(f"{API}/changes")).json()["changes"]
        assert (change["source"], change["agent"], change["keys"]) == ("command", "tutorial-agent", ["profile"])
        assert "profile: from balanced to secrets" in change["loosened"]
        # What the CLI prints, run by an agent: what it lowers, and that it waits for the user. Where: this tutorial's
        # dashboard, not `latet dashboard` (which would open the user's own, where this change is not).
        assert tool["output"].splitlines() == [
            "saved config.yaml",
            "This change lowers protection:",
            *(f"  - {reason}" for reason in change["loosened"]),
            "Not applied yet: it waits for the user's approval in the dashboard (here: Latet's pane). "
            "Tell them what you changed and why. If they do not approve it, config.yaml goes back as it was.",
            f"Change id: {change['id']} (`latet config set ... --wait` waits for the answer).",
        ]
        assert rt.config.profile == "balanced" and load_config(rt.config_file).profile == "secrets"
        assert asked["waiting"] == {"step": 1, "kind": "change", "satisfied": False}
        assert asked["steps"][1]["shown"] == {"component": "changes"}
        assert (await _next(client, "settings", {"step": 1})).json()["error"] == "waiting"
        decided = await client.post(f"{API}/changes/{change['id']}", json={"action": "reject"}, headers=UI)
        assert decided.status_code == 200
        assert decided.json() == {"ok": True, "state": "rejected"}
        assert (await state(client, "settings"))["waiting"]["satisfied"] is True
        assert rt.config.profile == "balanced" and load_config(rt.config_file).profile == "balanced"
        told = await run(box, "settings", 1, box.sandbox.session.chapters["settings"].steps[1], [])
        assert told["finished"] and told["lines"][-1]["text"] == "The profile is balanced."
        [get] = [line for line in told["lines"] if line["type"] == "tool" and line["step"] == 1]
        assert (get["summary"], get["output"]) == ("latet config get profile", "balanced")


async def test_columns_kept_in_clear_let_the_next_query_through(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, DATA)
    async with opened(tmp_path, monkeypatch) as box:
        client, session = box.client, box.sandbox.session
        steps = session.chapters["data"].steps
        await _start(client, "data")
        queried = await run(box, "data", 0, steps[0], [])
        shown = queried["steps"][0]["shown"]
        assert (shown["component"], shown["exchange"]) == ("columns", session.turns["table"])
        columns = (
            await client.get(f"{API}/exchanges/{shown['exchange']}/columns", params={"slot": shown["slot"]})
        ).json()
        assert [c["name"] for c in columns["columns"]] == ["id", "name", "email", "signup_date", "plan"]
        assert columns["strict"] is True and columns["reviewer"] is False
        assert queried["steps"][1]["shown"] == shown  # the do step: the same panel, before it runs
        assert queried["waiting"] == {"step": 1, "kind": "fields", "satisfied": False}
        assert (await _next(client, "data", {"step": 1})).json()["error"] == "waiting"
        await act(client, steps[1])
        assert (await state(client, "data"))["waiting"]["satisfied"] is True
        await run(box, "data", 1, steps[1], [])
        received = box.sandbox.model.received[-1].text
        assert "1001" in received and "Mary Johnson" not in received


async def test_the_tutorial_reviewer_finds_what_the_chapter_says_and_the_ward_once_guided(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, REVIEWED)
    async with opened(tmp_path, monkeypatch) as box:
        assert (await box.client.get(f"{API}/me")).json()["tutorial"]["reviewer"] is False  # none before the chapter
        last = await play(box, "llm")  # each step's checks hold: the name masked, then the ward with the guidance
        asked = box.sandbox.reviewer.asked
        assert any(a.guided and "Ward 7B" in a.found for a in asked)
        assert not any("Ward 7B" in a.found for a in asked if not a.guided)
        assert (await box.client.get(f"{API}/me")).json()["tutorial"]["reviewer"] is True
        view = (await box.client.get(f"{TUTORIAL}/chapters/llm")).json()
        assert view["steps"][1]["wait"] == {
            "kind": "guidance",
            "hint": "Save it.",
            "text": "Ward names are confidential.",
        }
        assert last["steps"][1]["shown"] == {"component": "guidance", "text": "Ward names are confidential."}
        assert last["steps"][0]["shown"]["component"] == "review"


async def test_in_the_sandbox_only_the_guidance_of_the_local_reviewer_changes(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, REVIEWED)
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        assert (await client.post(f"{TUTORIAL}/chapters/llm/start", headers=UI)).status_code == 200
        review = (await client.get(f"{API}/config")).json()["llm_review"]
        assert review["enabled"] is True and review["url"] == URL
        for change in ({"url": "http://127.0.0.1:8080/v1"}, {"enabled": False}, {"model": "another"}):
            refused = await client.put(f"{API}/config", json={"llm_review": {**review, **change}}, headers=UI)
            assert refused.status_code == 409, change
            assert "the Tutorial Reviewer" in refused.json()["error"]
        saved = await client.put(f"{API}/config", json={"llm_review": {**review, "guidance": "Beds too."}}, headers=UI)
        assert saved.status_code == 200
        assert box.sandbox.runtime.config.llm_review.guidance == "Beds too."
        assert box.sandbox.runtime.config.llm_review.url == URL
        # Its test, its key and its certificate stay refused with the Tutorial Reviewer on: they would reach outside.
        for method, path, body in (("POST", "/reviewer/test", {}), ("PUT", "/reviewer/key", {"key": "k"})):
            assert (await client.request(method, f"{API}{path}", json=body, headers=UI)).status_code == 409


async def test_the_guidance_wait_is_met_by_any_guidance_saved_and_not_by_none(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, REVIEWED)
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        started = await client.post(f"{TUTORIAL}/chapters/llm/start", headers=UI)
        assert started.status_code == 200
        chapter = box.sandbox.session.chapter
        assert chapter is not None
        await run(box, "llm", 0, chapter.steps[0], [])
        assert (await state(client, "llm"))["waiting"] == {"step": 1, "kind": "guidance", "satisfied": False}
        review = (await client.get(f"{API}/config")).json()["llm_review"]
        emptied = {**review, "guidance": "   "}  # emptied before saving: no guidance
        assert (await client.put(f"{API}/config", json={"llm_review": emptied}, headers=UI)).status_code == 200
        assert (await state(client, "llm"))["waiting"]["satisfied"] is False
        own = {**review, "guidance": "Wards and beds."}  # the reader's own words: guidance all the same
        assert (await client.put(f"{API}/config", json={"llm_review": own}, headers=UI)).status_code == 200
        assert (await state(client, "llm"))["waiting"]["satisfied"] is True


async def test_a_step_shows_its_review_and_the_guidance_to_save_before_it_runs(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, REVIEWED)
    async with opened(tmp_path, monkeypatch) as box:
        client, session = box.client, box.sandbox.session
        await _start(client, "llm")
        assert session.chapter is not None
        steps = session.chapter.steps
        read = await run(box, "llm", 0, steps[0], [])
        # The exchange with its Local reviewer panel: the request, at the block that holds the name.
        shown = read["steps"][0]["shown"]
        exchange = session.turns["said"]
        assert shown == {
            "component": "review",
            "exchange": exchange,
            "part": "request",
            "slot": shown["slot"],
            "highlight": [NAME],
        }
        detail = (await client.get(f"{API}/exchanges/{exchange}")).json()
        assert NAME in detail["request"]["slots"][shown["slot"]]["original"]
        # The `do` step shows the guidance to save, its sentence written in, before it runs; and `next` waits for it.
        assert read["steps"][1] == {
            "index": 1,
            "ran": False,
            "explain": "Save your guidance.",
            "shown": {"component": "guidance", "text": "Ward names are confidential."},
        }
        waiting = await _next(client, "llm", {"step": 1})
        assert waiting.status_code == 409 and waiting.json()["error"] == "waiting"
        await act(client, steps[1])
        assert (await _next(client, "llm", {"step": 1})).status_code == 200
        assert (await _settled(client, "llm"))["error"] is None


async def test_a_chapter_without_the_tutorial_reviewer_has_no_local_reviewer(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, REVIEWED, LIVE)
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        assert (await client.post(f"{TUTORIAL}/chapters/llm/start", headers=UI)).status_code == 200
        assert box.sandbox.runtime.mediator.reviewer is not None
        assert (await client.post(f"{TUTORIAL}/chapters/live/start", headers=UI)).status_code == 200
        assert box.sandbox.runtime.mediator.reviewer is None
        assert (await client.get(f"{API}/me")).json()["tutorial"]["reviewer"] is False


async def test_start_over_forgets_what_the_local_reviewer_read(tmp_path, monkeypatch):
    """A run whose reader saved guidance early (on the Rules page, before step 1) had the ward found in the first file:
    the next run, started over, reads that file again with no guidance, and leaves the ward in clear as the chapter
    says."""
    early = copy.deepcopy(REVIEWED)
    early["files"] = {**REVIEWED["files"], "note.md": "{{value:name}} is on {{value:ward}}.\n"}
    written(tmp_path / "chapters", monkeypatch, early)
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        assert (await client.post(f"{TUTORIAL}/chapters/llm/start", headers=UI)).status_code == 200
        review = (await client.get(f"{API}/config")).json()["llm_review"]
        await client.put(f"{API}/config", json={"llm_review": {**review, "guidance": "Wards too."}}, headers=UI)
        chapter = box.sandbox.session.chapter
        assert chapter is not None
        await run(box, "llm", 0, chapter.steps[0], [])
        assert "Ward 7B" not in box.sandbox.model.received[-1].text  # found with the guidance, masked
        assert (await client.post(f"{TUTORIAL}/chapters/llm/start", headers=UI)).status_code == 200  # Start over
        again = box.sandbox.session.chapter
        assert again is not None
        await run(box, "llm", 0, again.steps[0], [])
        assert "Ward 7B" in box.sandbox.model.received[-1].text  # read again, with no guidance: in clear
        assert box.sandbox.reviewer.asked  # asked again, not answered from memory


async def test_a_request_meant_to_be_stopped_that_reaches_the_model_is_an_error(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, NOT_STOPPED)
    async with opened(tmp_path, monkeypatch) as box:
        await _start(box.client, "stopped")
        after = await run(box, "stopped", 0, box.sandbox.session.chapters["stopped"].steps[0], [])
        assert after["error"] is not None and "meant to be stopped" in after["error"]
        assert after["error_step"] == 0


async def test_an_error_is_about_the_step_that_made_it(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, MISTAKE, PAIR)
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        mistake, pair = (box.sandbox.session.chapters[cid].steps for cid in ("stopped", "false-positives"))
        assert (await _start(client, "stopped"))["state"]["error_step"] is None
        # The step ran, but not as written: it is shown, with its error.
        ran = await run(box, "stopped", 0, mistake[0], [])
        assert ran["steps"][0]["ran"] and ran["next"] == 1
        assert "meant to be stopped" in ran["error"] and ran["error_step"] == 0
        # The next step is a new one: asking for it leaves the error of the step before behind.
        after = await run(box, "stopped", 1, mistake[1], [])
        assert after["steps"][1]["ran"] and after["error"] is None and after["error_step"] is None
        # A step that could not run keeps its error, and the step is the one still to run.
        await _start(client, "false-positives")

        async def broken(*_args: object) -> None:
            raise RuntimeError("no answer")

        with monkeypatch.context() as patch:
            patch.setattr(box.sandbox.agent, "run_step", broken)
            failed = await run(box, "false-positives", 0, pair[0], [])
        assert failed["error"] == "step 1 could not run: no answer"
        assert failed["error_step"] == 0 and failed["next"] == 0 and not failed["steps"][0]["ran"]
        # Start over forgets it.
        again = (await _start(client, "false-positives"))["state"]
        assert again["error"] is None and again["error_step"] is None


async def test_a_picture_the_agent_reads_goes_to_the_model_and_the_page_gets_it_as_it_was(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT)
    async with opened(tmp_path, monkeypatch) as box:
        # Played without the step's checks (the test of the sandbox's reader plays them): this one looks at the
        # picture itself, which reaches the model as an image, and at the page's copy of it.
        last = await play(box, "images", checked=False)
        sent = json.loads(box.sandbox.model.received[-1].body)  # the picture reached the model as an image
        [result] = [block for block in sent["messages"][-1]["content"] if block["type"] == "tool_result"]
        assert result["content"][0]["type"] == "image"
        shown = last["steps"][0]["shown"]
        assert shown["component"] == "images" and shown["original"] == "/tutorial/chapters/images/pictures/payment.png"
        picture = await box.client.get(f"{API}{shown['original']}")
        assert picture.status_code == 200 and picture.headers["content-type"] == "image/png"
        assert picture.content == chapters.asset("payment.png")
        assert picture.headers["x-content-type-options"] == "nosniff"
        for path in ("../config.yaml", "..%2Fconfig.yaml", "other.png", "notes.md"):
            assert (await box.client.get(f"{TUTORIAL}/chapters/images/pictures/{path}")).status_code == 404
        assert (await box.client.get(f"{TUTORIAL}/chapters/basics/pictures/payment.png")).status_code == 404


async def test_a_chapter_s_pictures_are_written_into_the_agent_s_project_as_they_are(tmp_path, monkeypatch):
    """Each start makes the project again with the chapter's pictures, in their folders; the page's copy is the
    package's, whatever happened to the agent's."""
    written(
        tmp_path / "chapters",
        monkeypatch,
        _chapter("images", pictures={"shots/payment.png": "payment.png"}, steps=[_watch("one")]),
    )
    async with opened(tmp_path, monkeypatch) as box:
        await _start(box.client, "images")
        picture = box.sandbox.agent.workspace / "shots" / "payment.png"
        assert picture.read_bytes() == chapters.asset("payment.png")
        picture.write_bytes(b"scribbled over by hand")
        again = await box.client.get(f"{TUTORIAL}/chapters/images/pictures/shots/payment.png")
        assert again.status_code == 200 and again.content == chapters.asset("payment.png")
        await _start(box.client, "images")  # Start over: the project is made again
        assert picture.read_bytes() == chapters.asset("payment.png")
        none = await box.client.get(f"{TUTORIAL}/chapters/images/pictures/nowhere.png")
        assert none.status_code == 404 and none.json() == {"error": "there is no such picture in this chapter"}


async def test_a_picture_the_agent_reads_is_read_and_covered_with_the_sandbox_s_reader(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT)
    monkeypatch.setattr(models, "runtime_available", lambda: True)  # a real reader is only where the `ocr` extra is
    async with opened(tmp_path, monkeypatch) as box:  # the tests' reader: it reads the screenshot as the models do
        await play(box, "images")  # its checks hold: the image flagged covered, the address masked in the text read
        # The Rules page the chapter's end links to says so too: the sandbox's own folder has no models, yours do.
        status = (await box.client.get(f"{API}/config")).json()["ocr_status"]
        assert status == {"runtime": True, "model": True, "active": True}


async def test_a_screenshot_that_cannot_be_read_is_withheld_and_the_step_still_ends(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT)
    failing = FakeOcr()
    failing.fail = True
    async with opened(tmp_path, monkeypatch, reader=lambda: failing) as box:
        assert (await box.client.post(f"{TUTORIAL}/chapters/images/start", headers=UI)).status_code == 200
        chapter = box.sandbox.session.chapter
        assert chapter is not None
        now = await run(box, "images", 0, chapter.steps[0], [])
        assert now["error"] is None and now["steps"][0]["ran"]
        exchange = box.sandbox.session.turns["why"]
        detail = (await box.client.get(f"{API}/exchanges/{exchange}")).json()
        assert [image["state"] for image in detail["request"]["images"]] == ["withheld"]


async def test_a_chapter_that_needs_text_recognition_waits_for_its_models(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT, LIVE)
    found: dict = {"reader": None}
    async with opened(tmp_path, monkeypatch, reader=lambda: found["reader"]) as box:
        client = box.client
        cards = {card["id"]: card for card in (await client.get(f"{TUTORIAL}/chapters")).json()["chapters"]}
        assert cards["images"]["missing"] == ["ocr"] and cards["live"]["missing"] == []
        refused = await client.post(f"{TUTORIAL}/chapters/images/start", headers=UI)
        assert refused.status_code == 409 and refused.json()["error"] == "needs ocr"
        found["reader"] = payment_reader()  # `latet ocr install`, while the tutorial runs
        assert (await client.get(f"{TUTORIAL}/chapters/images")).json()["missing"] == []
        assert (await client.post(f"{TUTORIAL}/chapters/images/start", headers=UI)).status_code == 200


async def test_a_chapter_refused_for_want_of_its_models_leaves_the_chapter_in_progress_as_it_is(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT, LIVE)
    async with opened(tmp_path, monkeypatch, reader=lambda: None) as box:
        client = box.client
        started = (await _start(client, "live"))["state"]
        refused = await client.post(f"{TUTORIAL}/chapters/images/start", headers=UI)
        assert refused.status_code == 409 and refused.json() == {"error": "needs ocr", "state": started}
        assert box.sandbox.session.chapter is not None and box.sandbox.session.chapter.id == "live"
        assert await state(client, "live") == started  # the same run: nothing was started over


async def test_the_models_are_asked_for_by_a_chapter_that_needs_them_until_they_are_found_and_then_kept(
    tmp_path, monkeypatch
):
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT, LIVE)
    asked: list[bool] = []
    reader = payment_reader()
    found: dict = {"reader": None}

    def find():
        asked.append(found["reader"] is not None)
        return found["reader"]

    async with opened(tmp_path, monkeypatch, reader=find) as box:
        client, mediator = box.client, box.sandbox.runtime.mediator
        assert (await client.get(f"{TUTORIAL}/chapters/live")).json()["missing"] == []
        assert (await client.post(f"{TUTORIAL}/chapters/live/start", headers=UI)).status_code == 200
        assert asked == [] and mediator.vision is None  # a chapter that needs nothing never asks for them
        await client.get(f"{TUTORIAL}/chapters")
        await client.get(f"{TUTORIAL}/chapters/images")
        assert asked == [False, False] and mediator.vision is None  # looked for again at each look
        found["reader"] = reader
        for _ in range(2):  # a card, then a chapter's view
            await client.get(f"{TUTORIAL}/chapters")
            await client.get(f"{TUTORIAL}/chapters/images")
        assert asked == [False, False, True]  # found once, and kept: the same reader, loaded once
        assert mediator.vision is not None and mediator.vision.ocr is reader


async def test_starting_a_chapter_that_reads_images_starts_the_models_loading(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT)
    reader = payment_reader()
    loading: list[bool] = []
    monkeypatch.setattr(reader, "start_loading", lambda: loading.append(True))
    async with opened(tmp_path, monkeypatch, reader=lambda: reader) as box:
        await _start(box.client, "images")
        assert box.sandbox.runtime.mediator.ocr is True and loading  # `ocr: auto`: loading now, not at the first image


async def test_a_picture_whose_name_needs_encoding_is_shown_by_an_address_the_page_can_ask_for(tmp_path, monkeypatch):
    """The page asks for `original` as it is written: a space, a `#` or a `%` in the picture's path is encoded in it,
    and the route finds the picture the encoding stands for."""
    path = "my shots/pay #1 100%.png"
    step = {**_watch("one"), "show": {"component": "images", "exchange": "one", "original": path}}
    written(tmp_path / "chapters", monkeypatch, _chapter("images", pictures={path: "payment.png"}, steps=[step]))
    async with opened(tmp_path, monkeypatch) as box:
        await _start(box.client, "images")
        done = await run(box, "images", 0, box.sandbox.session.chapters["images"].steps[0], [])
        original = done["steps"][0]["shown"]["original"]
        assert original == "/tutorial/chapters/images/pictures/my%20shots/pay%20%231%20100%25.png"
        asked = await box.client.get(f"{API}{original}")
        assert asked.status_code == 200 and asked.content == chapters.asset("payment.png")


async def test_a_broken_install_shows_the_chapter_as_needing_its_models_and_not_a_menu_that_fails(
    tmp_path, monkeypatch, caplog
):
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT, LIVE)

    def broken():
        raise ImportError("DLL load failed while importing onnxruntime")  # the `ocr` extra, installed and broken

    async with opened(tmp_path, monkeypatch, reader=broken) as box:
        client = box.client
        menu = await client.get(f"{TUTORIAL}/chapters")
        assert menu.status_code == 200
        cards = {card["id"]: card for card in menu.json()["chapters"]}
        assert cards["images"]["missing"] == ["ocr"] and cards["live"]["missing"] == []
        view = await client.get(f"{TUTORIAL}/chapters/images")
        assert view.status_code == 200 and view.json()["missing"] == ["ocr"]
        refused = await client.post(f"{TUTORIAL}/chapters/images/start", headers=UI)
        assert refused.status_code == 409 and refused.json()["error"] == "needs ocr"
        # Said in the log, by the type of the error (as every log line of Latet's says one), and not to the page.
        assert "your text recognition models could not be looked for: ImportError" in caplog.text


async def test_a_reader_whose_models_failed_to_load_counts_as_the_models_missing(tmp_path, monkeypatch):
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT)
    reader = payment_reader()
    reader.error = RuntimeError("the models could not be loaded")  # `ImageMasking.available` is False while it is set
    async with opened(tmp_path, monkeypatch, reader=lambda: reader) as box:
        client = box.client
        assert (await client.get(f"{TUTORIAL}/chapters/images")).json()["missing"] == ["ocr"]
        refused = await client.post(f"{TUTORIAL}/chapters/images/start", headers=UI)
        assert refused.status_code == 409 and refused.json()["error"] == "needs ocr"
        reader.error = None  # asked at each look: a reader that works again is not missing
        assert (await client.get(f"{TUTORIAL}/chapters/images")).json()["missing"] == []
        assert (await client.post(f"{TUTORIAL}/chapters/images/start", headers=UI)).status_code == 200


async def test_start_looks_for_the_reader_again_after_it_applies_the_chapter_s_settings(tmp_path, monkeypatch):
    """A settings save racing the start may write `mediator.vision` back to None (`apply_config` keeps what it read):
    the chapter's reset puts the reader back, and the models load now."""
    written(tmp_path / "chapters", monkeypatch, SCREENSHOT)
    reader = payment_reader()
    loading: list[bool] = []
    monkeypatch.setattr(reader, "start_loading", lambda: loading.append(True))
    async with opened(tmp_path, monkeypatch, reader=lambda: reader) as box:
        sandbox, mediator = box.sandbox, box.sandbox.runtime.mediator
        assert sandbox.missing(["ocr"]) == [] and mediator.vision is not None  # found, as the start finds it
        mediator.vision = None  # the racing save wrote it away
        await sandbox.reset(sandbox.session.chapters["images"])
        assert mediator.vision is not None and mediator.vision.ocr is reader
        assert loading  # and the models load now, as the chapter's `ocr: auto` asks


async def test_a_chapters_reset_lets_go_of_a_request_waiting_in_the_live_reviewer_unsent(tmp_path, monkeypatch):
    """What waits for the user goes first when a chapter begins again, whatever left it waiting: a batch of the Live
    Reviewer is decided not sent, and leaves the queue."""
    written(tmp_path / "chapters", monkeypatch, LIVE)
    async with opened(tmp_path, monkeypatch) as box:
        sandbox = box.sandbox
        approvals = sandbox.runtime.mediator.approvals
        waiting = await approvals.open(Batch("cc:tutorial", None, "claude-code", []))
        await sandbox.reset(sandbox.session.chapters["live"])
        assert approvals.batches == {} and waiting.future.result().state == "rejected"
