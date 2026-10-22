"""Each chapter of the tutorial is a test. Played from its start to its end through the tutorial's API, as a user
plays it in the page (each `do` step's action done in the real dashboard, `next`, the state polled while the agent
works), every step's checks hold and every scripted turn is used; at its end the chapter is finished, and
tutorial.json in the user's own folder says so. What the user sees of it is pinned: the fakes the model got (the
sandbox's fixed key makes them, the same for everyone), the model's lines in the pane, what Latet's view shows. Played
again (Start over), it gives the same fakes and the same pane, so the chapters' texts and the screenshots hold at every
run. A change to Latet that makes a chapter untrue fails here."""

import json
import re
from dataclasses import dataclass, field

import pytest

from latet.config import load_config
from latet.tutorial.chapters import load_all

from .test_tutorial_roundtrip import CARD, EMAIL, FAKE, KEY, PHONE, TERM
from .tutorial_sandbox import API, TUTORIAL, UI, held, opened, play, run, settled

pytestmark = pytest.mark.e2e

# The false-positives chapter: what the model says of the test card it got masked, a fake it takes for a customer's.
REFUSED = "That is not Stripe's test card, so it may be a customer's. I won't put a real card in a test."
# The live chapter's customer: no rule knows her name, which the user masks by hand (People), and a rule finds her
# address. What the sandbox's fixed key makes of each.
CUSTOMER = "Emily Carter"
CUSTOMER_EMAIL = "emily.carter@acme.com"
CUSTOMER_FAKE = "David Green"  # a PERSON, masked by hand: `human:person`
CUSTOMER_EMAIL_FAKE = "michael.smith@proseware-4795.example"
DRAFT = "Draft: Dear {}, we're sorry you were charged twice; the refund is on its way. Send it to {}."
# The settings chapter: what the model says once the agent asked to let e-mail addresses through, and once it read
# the settings back: the change undone, as the chapter advises.
ASKED = (
    "The test needs real e-mail addresses and Latet masks them, so I asked it to let them through. It waits for "
    "your approval."
)
UNDONE = "Undone: e-mail addresses stay masked. I'll fix the test without the real ones."
# The data chapter: the first customer's id and plan, and what strict data mode made of them (the same at every run).
FIRST_ID, FIRST_ID_FAKE, PLAN, PLAN_FAKE = "1001", "8146", "pro", "bzr"
NONE_ON_PRO = "None of them: not one row says pro."
TWO_ON_PRO = (
    "Two of the five: customers 1001 and 1004, on pro since January and March. Who they are, I still can't tell."
)
# The llm chapter: four values no pattern finds, found by the Tutorial Reviewer, and the wards once guided. What the
# sandbox's fixed key makes of each (an address written out gets a fake written out).
OKAFOR, CONDITION = "Daniel Okafor", "type 2 diabetes"
WRITTEN, ACCOUNT = "daniel dot okafor at stmarys-health dot org", "GB82 WEST 1234 5698 7654 23"
OKAFOR_FAKE, CONDITION_FAKE, ACCOUNT_FAKE = "Thomas Adams", "etzx 6 mslookdz", "GT61 MRIP 5646 5638 4242 65"
WRITTEN_FAKE = "laura dot turner at southridge-ebf9 dot example"
WARD, OTHER_WARD, WARD_FAKE, OTHER_WARD_FAKE = "Ward 7B", "Ward 3A", "Fqnm 2T", "Axvn 2S"
SUMMARY = (
    "{} has not been able to get into the patient portal since Monday and needs the blood test results before Friday "
    "({}). Reply to {}; the parking refund goes to {}."
)
NIGHT = "{} is short of one nurse tonight, and the agency sends one at 23:00. {} has two beds free."
# The images chapter: the three values drawn in the screenshot, and what the sandbox's fixed key makes of each.
SHOT = {
    "amelia.hughes@acme.com": "jack.jones@proseware-4795.example",
    "GB82 WEST 1234 5698 7654 32": "GB83 CDIA 1362 6967 9217 64",
    "+44 20 7946 0321": "+44 21 8430 8193",
}


@dataclass
class Seen:
    """What the user sees of a chapter: what the model got in place of the invented values (as the chapter's
    patterns last found it: the explanations show it), the model's lines and the errors in the agent's pane (as it
    got them: the real values), what Latet's view shows at each step (a `do` step's view while it waits, then once
    it ran), and pieces of the explanations that name what the model got."""

    captured: dict[str, str]
    lines: list[tuple[int, str, str]]
    views: list[tuple[str | None, str]]
    explained: dict[int, str] = field(default_factory=dict)


SEEN = {
    "basics": Seen(
        captured={"DEMO_API_KEY": FAKE[KEY], "EMAIL": FAKE[EMAIL], "PHONE": FAKE[PHONE]},
        lines=[
            (0, "model", "Got the key. Writing the config."),
            (1, "model", "Done. config.local.json is ready for prod."),
            (3, "model", f"I can't send emails, but I noted {EMAIL} and {PHONE}."),
        ],
        views=[(None, "exchange"), (None, "exchange"), ("exchange", "exchange"), (None, "exchange")],
        explained={1: f"The model put its fake ({FAKE[KEY]}) in the Write call."},
    ),
    "terms": Seen(
        # In clear in the roadmap; then, a term, a fake in a new conversation's prompt, with nothing before it.
        captured={"CODENAME": TERM, "ASKED": FAKE[TERM]},
        lines=[
            (0, "model", f"{TERM} ships first, in March: the new checkout."),
            (2, "model", f"What's {TERM}? No idea what this is."),  # the model got a fake name: restored
        ],
        views=[(None, "exchange"), ("terms", "terms"), (None, "exchange")],
        explained={2: f"The model got {FAKE[TERM]}: the request Latet shows has it in place of the name."},
    ),
    "false-positives": Seen(
        captured={"CARD": CARD},  # in clear, once it is Not sensitive
        lines=[
            (0, "model", REFUSED),
            (1, "model", f"Got it: {CARD} is Stripe's test card. Adding the refund test."),
        ],
        views=[(None, "exchange"), ("exchange", "exchange")],
        explained={
            0: f"the model got {FAKE[CARD]}",
            1: "The agent then reads the file again, and this time the card goes in clear",
        },
    ),
    "stopped": Seen(
        captured={},  # the model never gets the key: the first request is stopped, the second has none
        lines=[
            (0, "model", "Deploying to prod. I'll tell you when it's done."),  # yesterday's, resumed
            (
                0,
                "error",
                "Latet: sensitive values would leave in clear: GENERIC_API_KEY[40]. Nothing was sent; see where "
                "it was in Latet's pane",  # not `latet dashboard`: it would open your own dashboard
            ),
            (1, "model", "Deploying with the key in DEMO_API_KEY. The key itself never reached me."),
        ],
        views=[(None, "why"), (None, "exchange")],
        explained={
            0: "Claude Code seals each request with a fingerprint of that conversation, so on the way out Latet "
            "may not change it, only check it.",
            1: "the old one is gone, and the key with it",
        },
    ),
    "live": Seen(
        captured={"WHO": CUSTOMER_FAKE, "MAIL": CUSTOMER_EMAIL_FAKE},
        lines=[(0, "model", DRAFT.format(CUSTOMER, CUSTOMER_EMAIL))],  # the model wrote the fakes: restored
        views=[("approvals", "exchange")],  # the queue while the request waits, then the request as it left
        explained={0: f"nothing tells it that {CUSTOMER} is a customer: this sandbox has no name detection."},
    ),
    "settings": Seen(
        captured={},
        lines=[(0, "model", ASKED), (2, "model", UNDONE)],  # Undo it, as the explanation advises
        views=[(None, "changes"), ("changes", "none"), (None, "none")],  # the change waiting, then nothing to show
        explained={
            0: "which lets every e-mail address leave in clear",
            1: "A guardrail against mistakes, not a lock: an agent runs as you.",
            2: "you undid the change, so your settings are as they were",
        },
    ),
    "data": Seen(
        captured={"ID": FIRST_ID, "PLAN": PLAN},  # in clear in the second query
        lines=[(0, "model", NONE_ON_PRO), (1, "model", TWO_ON_PRO)],
        views=[(None, "exchange"), ("columns", "exchange")],  # the table, the columns to choose, the second table
        explained={0: f"The model got {FIRST_ID_FAKE} for the id {FIRST_ID} and {PLAN_FAKE} for the plan {PLAN}"},
    ),
    "llm": Seen(
        captured={
            "NAME": OKAFOR_FAKE,
            "CONDITION": CONDITION_FAKE,
            "WRITTEN": WRITTEN_FAKE,
            "ACCOUNT": ACCOUNT_FAKE,
            "WARD": WARD_FAKE,
            "OTHER_WARD": OTHER_WARD_FAKE,
        },
        lines=[
            (0, "model", SUMMARY.format(OKAFOR, CONDITION, WRITTEN, ACCOUNT)),  # the model wrote the fakes: restored
            (2, "model", NIGHT.format(OTHER_WARD, WARD)),
        ],
        views=[(None, "review"), ("guidance", "guidance"), (None, "review")],
        explained={
            0: f"the model got {OKAFOR_FAKE} for the name",
            2: f"The model got {OTHER_WARD_FAKE} for {OTHER_WARD}.",
        },
    ),
    "images": Seen(
        captured={},  # the model reads no value: the image is all it gets
        lines=[
            (
                0,
                "model",
                "The receiving bank closed the account, so the payment could not go through. Ask the customer for "
                "another account, then send it again.",
            )
        ],
        views=[(None, "images"), ("images", "images")],
        explained={0: "covered each with its fake before the image left"},
    ),
}


@pytest.mark.parametrize("cid", list(load_all()))
async def test_a_chapter_holds_from_its_first_step_to_its_last(tmp_path, monkeypatch, cid):
    async with opened(tmp_path, monkeypatch) as box:
        last = await play(box, cid)
        assert last["next"] == len(box.sandbox.session.chapters[cid].steps)
        assert json.loads(box.progress.read_text("utf-8")) == {"done": [cid]}
        menu = (await box.client.get(f"{TUTORIAL}/chapters")).json()
        assert next(card for card in menu["chapters"] if card["id"] == cid)["done"] is True


@pytest.mark.parametrize("cid", list(SEEN))
async def test_what_the_user_sees_of_a_chapter(tmp_path, monkeypatch, cid):
    seen, before = SEEN[cid], []
    async with opened(tmp_path, monkeypatch) as box:
        last = await play(box, cid, checked=False, before=before)
        assert box.sandbox.model.captured == seen.captured
    lines = [(line["step"], line["type"], line["text"]) for line in last["lines"] if line["type"] in ("model", "error")]
    assert lines == seen.lines
    looks = [state["steps"][index]["shown"] for index, state in enumerate(before)]
    shown = [step["shown"] for step in last["steps"]]
    views = [
        (look["component"] if look else None, after["component"]) for look, after in zip(looks, shown, strict=True)
    ]
    assert views == seen.views
    for index, piece in seen.explained.items():
        assert piece in last["steps"][index]["explain"]


async def test_false_positives_reads_the_file_again_and_shows_the_card_in_clear_there(tmp_path, monkeypatch):
    """After Not sensitive, the agent reads the file again: the model gets the card itself in a new tool output, and
    Latet's view shows that one, not the first read sent again with the conversation."""
    async with opened(tmp_path, monkeypatch) as box:
        last = await play(box, "false-positives")
        tools = [line for line in last["lines"] if line["type"] == "tool" and line["step"] == 1]
        assert [(line["name"], "tests/test_checkout.py" in line["summary"]) for line in tools] == [("Read", True)]
        shown = last["steps"][1]["shown"]
        detail = (await box.client.get(f"{API}/exchanges/{shown['exchange']}")).json()
        slots = detail["request"]["slots"]
        reads = [index for index, slot in enumerate(slots) if slot["kind"] == "tool_result"]
        assert len(reads) == 2 and shown["slot"] == reads[-1]
        assert CARD in slots[reads[-1]]["masked"]  # what the model got: the card, in clear


async def test_settings_approved_instead_of_undone_says_what_it_means(tmp_path, monkeypatch):
    """Undo it is the advice, and Approve the user's to choose: then the agent finds e-mail addresses let through and
    says so, and the step says what that means and how to try again."""
    async with opened(tmp_path, monkeypatch) as box:
        client, rt = box.client, box.sandbox.runtime
        steps = box.sandbox.session.chapters["settings"].steps
        assert (await client.post(f"{TUTORIAL}/chapters/settings/start", headers=UI)).status_code == 200
        await run(box, "settings", 0, steps[0], [])
        assert rt.config.policies == {}  # it waits: nothing changed yet
        [change] = (await client.get(f"{API}/changes")).json()["changes"]
        assert change["loosened"] == ["policy for EMAIL: from mask to allow"] and change["keys"] == ["policies"]
        approved = await client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
        assert approved.status_code == 200
        assert rt.config.policies == load_config(rt.config_file).policies == {"EMAIL": "allow"}  # applied, kept
        await run(box, "settings", 1, steps[1], [])
        done = await run(box, "settings", 2, steps[2], [])
        assert done["error"] is None and done["finished"]
        told = steps[2].model[-1].approved
        assert told is not None and done["lines"][-1] == {"step": 2, "type": "model", "text": told}
        assert "e-mail addresses now leave in clear" in told
        assert steps[2].otherwise is not None and done["steps"][2]["explain"] == steps[2].otherwise.approved
        assert "Start over" in done["steps"][2]["explain"]


async def test_stopped_resumes_a_conversation_whose_key_stops_the_request_marked_where_it_was_said(
    tmp_path, monkeypatch
):
    """The first step resumes a conversation from before Latet was on, with a key pasted in it: the request carries
    it again, Latet stops it, the model gets nothing, and the key is marked where it was said, yesterday."""
    async with opened(tmp_path, monkeypatch) as box:
        steps = box.sandbox.session.chapters["stopped"].steps
        assert (await box.client.post(f"{TUTORIAL}/chapters/stopped/start", headers=UI)).status_code == 200
        done = await run(box, "stopped", 0, steps[0], [])
        assert done["error"] is None and box.sandbox.model.received == []
        resumed = [line for line in done["lines"] if line.get("resumed")]
        assert [line["type"] for line in resumed] == ["prompt", "model"] and KEY in resumed[0]["text"]
        shown = done["steps"][0]["shown"]
        slots = (await box.client.get(f"{API}/exchanges/{shown['exchange']}")).json()["request"]["slots"]
        said = [index for index, slot in enumerate(slots) if slot["kind"] == "user_text"]
        assert len(said) == 2 and shown["slot"] == said[0]  # yesterday's prompt, not today's
        assert KEY in slots[said[0]]["original"] and KEY not in slots[said[1]]["original"]


async def test_live_approved_as_it_was_says_the_name_left_in_clear(tmp_path, monkeypatch):
    """Enter in the embedded Approvals approves the request as it is. The step is done all the same, and says plainly
    that the model got the name in clear, that nothing left the computer, and how to try again."""
    async with opened(tmp_path, monkeypatch) as box:
        [step] = box.sandbox.session.chapters["live"].steps
        assert step.otherwise is not None and step.otherwise.in_clear is not None
        batch = await held(box.client, "live")
        approved = await box.client.post(f"{API}/approvals/{batch['id']}", json={"action": "approve"}, headers=UI)
        assert approved.status_code == 200
        done = await settled(box.client, "live")
        assert done["error"] is None and done["finished"] and done["steps"][0]["ran"]
        explain = done["steps"][0]["explain"]
        assert explain == step.otherwise.in_clear
        assert f"the model got {CUSTOMER} in clear" in explain and "Start over" in explain
        assert "nothing left your computer" in explain
        [received] = box.sandbox.model.received
        assert CUSTOMER in received.text and CUSTOMER_EMAIL not in received.text  # the address, masked all the same


async def test_live_with_part_of_the_name_masked_says_the_rest_left_in_clear(tmp_path, monkeypatch):
    """A hand mask over one word of the name leaves the other in clear: the step says so, and how to try again, where
    its explanation would say the model got a fake for the name."""
    first, last = CUSTOMER.split()
    async with opened(tmp_path, monkeypatch) as box:
        [step] = box.sandbox.session.chapters["live"].steps
        assert step.otherwise is not None and step.otherwise.part_in_clear is not None
        batch = await held(box.client, "live")
        at = batch["items"][0]["original"].find(first)
        mask = {"item": 0, "start": at, "end": at + len(first), "category": "person"}
        body = {"action": "approve", "masks": [mask]}
        approved = await box.client.post(f"{API}/approvals/{batch['id']}", json=body, headers=UI)
        assert approved.status_code == 200
        done = await settled(box.client, "live")
        assert done["error"] is None and done["finished"] and done["steps"][0]["ran"]
        assert done["steps"][0]["explain"] == step.otherwise.part_in_clear
        assert "select the whole name" in step.otherwise.part_in_clear and "Start over" in step.otherwise.part_in_clear
        [received] = box.sandbox.model.received
        assert first not in received.text and f" {last}, she was" in received.text


async def test_live_not_sent_says_so(tmp_path, monkeypatch):
    """Don't send: the request is not sent, the step is done, and says what happened and how to try again; Latet's
    view shows why the request was stopped."""
    async with opened(tmp_path, monkeypatch) as box:
        [step] = box.sandbox.session.chapters["live"].steps
        assert step.otherwise is not None and step.otherwise.not_sent is not None
        batch = await held(box.client, "live")
        said_no = await box.client.post(f"{API}/approvals/{batch['id']}", json={"action": "reject"}, headers=UI)
        assert said_no.status_code == 200
        done = await settled(box.client, "live")
        assert done["error"] is None and done["finished"] and done["steps"][0]["ran"]
        assert done["steps"][0]["explain"] == step.otherwise.not_sent and "Don't send" in step.otherwise.not_sent
        assert done["steps"][0]["shown"]["component"] == "why" and box.sandbox.model.received == []


async def test_data_waits_for_the_columns_latet_proposes(tmp_path, monkeypatch):
    """The columns the step asks the user to keep in clear are the ones the columns panel proposes, ticked when it
    opens (ids, dates of events, states): the people's it proposes to keep masked, and the amounts it leaves to the
    user."""
    async with opened(tmp_path, monkeypatch) as box:
        client = box.client
        steps = box.sandbox.session.chapters["data"].steps
        assert (await client.post(f"{TUTORIAL}/chapters/data/start", headers=UI)).status_code == 200
        after = await run(box, "data", 0, steps[0], [])
        look = after["steps"][1]["shown"]
        assert look["component"] == "columns" and look["exchange"] == box.sandbox.session.turns["table"]
        panel = await client.get(f"{API}/exchanges/{look['exchange']}/columns", params={"slot": look["slot"]})
        proposals = {column["name"]: column["proposal"] for column in panel.json()["columns"]}
        assert proposals == {
            "id": "keep",
            "name": "mask",
            "email": "mask",
            "signup_date": "keep",
            "plan_type": "keep",
            "amount": "ask",
        }
        assert steps[1].wait is not None
        assert steps[1].wait.target == [name for name, proposal in proposals.items() if proposal == "keep"]


async def test_images_sends_the_text_read_in_the_screenshot_with_the_three_fakes(tmp_path, monkeypatch):
    """The model gets the screenshot alone, so the fakes are pinned where the user sees them: in the text read in it,
    as it was sent. The tests' fake reader reads it as the real models do; the mock dashboard plays the chapter with the
    same fakes (dashboard/src/mock/tutorial.test.ts)."""
    async with opened(tmp_path, monkeypatch) as box:
        last = await play(box, "images")
        exchange = last["steps"][0]["shown"]["exchange"]
        detail = (await box.client.get(f"{API}/exchanges/{exchange}")).json()
        [text] = [slot for slot in detail["request"]["slots"] if slot["kind"] == "image_text"]
        assert [value for value in SHOT if value in text["original"]] == list(SHOT)
        assert [fake for fake in SHOT.values() if fake in text["masked"]] == list(SHOT.values())
        assert [value for value in SHOT if value in text["masked"]] == []


@pytest.mark.parametrize("cid", list(load_all()))
async def test_a_chapter_played_again_gives_the_same_fakes_and_the_same_pane(tmp_path, monkeypatch, cid):
    async with opened(tmp_path, monkeypatch) as box:
        model = box.sandbox.model
        first = await play(box, cid, checked=False)
        sent, captured = [_same(r.text) for r in model.received], dict(model.captured)
        second = await play(box, cid, checked=False)  # Start over: the vault, the history and the rules emptied
        assert [_same(r.text) for r in model.received] == sent and model.captured == captured
        assert _same(json.dumps(second["lines"])) == _same(json.dumps(first["lines"])) and second["run"] != first["run"]
        assert [step["explain"] for step in second["steps"]] == [step["explain"] for step in first["steps"]]


# What `latet config set` prints of a change waiting: its id is random, as on a real daemon, and the one text of a
# chapter that differs from run to run.
CHANGE_ID = re.compile(r"(Change id: )[\w-]+")


def _same(text: str) -> str:
    return CHANGE_ID.sub(r"\1<id>", text)
