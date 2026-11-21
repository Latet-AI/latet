"""Central approvals on a server (Latet Enterprise), the company's side: where each item goes, who decides and is
named, what of a change reaches them (what it lowers, never a value) and of a text (as its author sees it, when opened),
decisions carried out in the asking identity's space and kept with who made them, nobody approving their own request
(in an API key's space, whoever made it), an API key's change decided by whoever else controls it, and what ends
elsewhere or is lost leaving the queue."""

import asyncio
import base64
import dataclasses
import json
import threading

import pytest

from latet.config import Config, IdentityConfig, LiveReview, Profile
from latet.ee.approvals import (
    APPROVED_UNAPPLIED,
    LOST_AFTER,
    MOST_NAMED,
    RECONCILE_EVERY,
    CentralApprovals,
    MemoryRecords,
    Row,
    View,
)
from latet.ee.roles import Role
from latet.ee.settings import ApprovalsSettings
from latet.gateway import approvals as approvals_module
from latet.gateway.approvals import Batch, Change, Changed, MaskError, Waits, batch_version
from latet.runtime import make_runtime
from latet.server.shared import SettingsRefused

from ...conftest import ROOT_KEY
from .stand_ins import FakeDirectory, Served, Spaces

TEXT = "Send the overdue notice to laura.bennett@whitfield.co.uk about Project Falcon"
run = asyncio.to_thread
EVERYTHING = View("", security=True, keys=frozenset(), admin=True)  # what the record holds, for a test to read


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Watched(dict):
    """How a space's changes ended, as a local queue keeps them, saying in which thread each was read."""

    def __init__(self) -> None:
        super().__init__()
        self.read_in: list[int | None] = []

    def get(self, key, default=None):
        self.read_in.append(threading.get_ident())
        return super().get(key, default)


class Queues:
    """What the record asks of the spaces' queues: which texts still wait, how changes ended (none here unless set)."""

    def __init__(self) -> None:
        self.alive: dict[str, set[str]] = {}
        self.ended: dict[str, dict[str, str]] = {}

    def batches(self, items: list[tuple[str, str]]) -> set[str]:
        return {i for identity_id, i in items if identity_id not in self.alive or i in self.alive[identity_id]}

    def changes(self, items: list[tuple[str, str]]) -> dict[str, str]:
        return {i: s for identity_id, i in items for j, s in self.ended.get(identity_id, {}).items() if i == j}


@pytest.fixture
def people():
    """A server's people: Emily asks; Mark and Daniel decide for the security team; Paul is an admin."""
    directory = FakeDirectory()
    emily = directory.add("Emily Carter", group="Payments")
    mark = directory.add("Mark Ellis", roles=(Role.SECURITY,))
    daniel = directory.add("Daniel Hayes", roles=(Role.SECURITY,))
    paul = directory.add("Paul Ferris", roles=(Role.ADMIN,))
    return directory, emily, mark, daniel, paul


@pytest.fixture
def chosen():
    """Who decides, as the server's settings say it now (a test may change it)."""
    return {"settings": ApprovalsSettings(changes="security", live_review="security")}


@pytest.fixture
def queues():
    return Queues()


@pytest.fixture
def central(people, chosen, queues):
    return CentralApprovals(
        MemoryRecords(), people[0], settings=lambda: chosen["settings"], licensed=lambda: True, space=Spaces(),
        waiting_batches=queues.batches, ended_changes=queues.changes, clock=Clock(),
    )  # fmt: skip


@pytest.fixture
def space(tmp_path, central, monkeypatch):
    """An identity's space on the server: its runtime with the server's edition, reached by central approvals."""
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    made = []

    def make(identity):
        config = Config(identity=IdentityConfig(enabled=False), live_review=LiveReview(enabled=True))
        folder = tmp_path / identity.id
        rt = make_runtime(
            config, data=folder, config_file=folder / "config.yaml", identity=identity, edition=Served(central)
        )
        central.space.runtimes[identity.id] = rt
        made.append(rt)
        return rt

    yield make
    for rt in made:
        rt.close()


def _lowered(rt, value="ACME"):
    return rt.config.model_copy(update={"allow_values": [value]})


def _lowering(value="ACME"):
    """A save in the dashboard that lowers protection: ``value`` never masked."""
    return lambda current: current.model_copy(update={"allow_values": [value]})


def _batch(**kwargs) -> Batch:
    item = {"kind": "user_text", "path": None, "tool": None, "original": TEXT, "masked": TEXT, "replacements": [],
            "suggestions": [], "failed": None}  # fmt: skip
    return Batch("cc:emily", None, "claude-code", [item], **kwargs)


async def _decide_change(central, who, item_id, approve):
    """As Task 7's route does: admitted (here without a transaction), then carried out."""
    row, capacity = central.admitted(None, who.id, item_id, "change", approve)
    if row is None:
        return capacity  # why not
    return await central.carry_change(row, approve, None if capacity == "own" else who, run=run)


async def _decide_batch(central, who, item_id, approve, masks=()):
    """As Task 7's route does: admitted, the text read and its version named in an approval, checked, carried out."""
    row, capacity = central.admitted(None, who.id, item_id, "batch", approve)
    if row is None:
        return capacity
    version = None
    if approve:
        found = await central.batch(row, run)
        version = batch_version(found.items) if found is not None else "gone"
    await central.check(row, list(masks), [], run, version=version)
    by = None if capacity == "own" else who
    return await central.carry_batch(row, approve, list(masks), [], by, run=run, version=version)


def test_where_an_item_goes_follows_the_settings_the_kind_and_the_license(people, queues):
    directory, emily, *_ = people
    key = directory.add("Billing CI", kind="key")
    chosen, licensed = {"settings": ApprovalsSettings()}, [True]
    central = CentralApprovals(
        MemoryRecords(), directory, settings=lambda: chosen["settings"], licensed=lambda: licensed[0], space=Spaces(),
        waiting_batches=queues.batches, ended_changes=queues.changes,
    )  # fmt: skip
    assert central.changes_go_to(emily) == "self" and central.changes_go_to(key) == "controllers"  # [D2]
    assert central.live_review_goes_to(key) == "self"
    chosen["settings"] = ApprovalsSettings(changes="security", live_review="security")
    assert central.changes_go_to(emily) == "security" and central.changes_go_to(key) == "security"
    assert central.live_review_goes_to(emily) == "security"
    assert central.changes_go_to(None) == "self"
    licensed[0] = False
    assert central.changes_go_to(key) == "self" and central.live_review_goes_to(emily) == "self"


def test_who_decides_is_named_and_asked_again_after_a_while(central, people):
    directory, emily, mark, daniel, paul = people
    key = directory.add("Billing CI", kind="key")
    laura = directory.add("Laura Bennett")
    assert central.deciders(emily, "security") == ["Daniel Hayes", "Mark Ellis"]
    assert central.deciders(key, "controllers") == ["Paul Ferris"]  # nobody controls it: the admins
    directory.controlling[key.id] = [emily.id]
    assert central.deciders(key, "controllers") == ["Emily Carter"]
    directory.disabled.add(daniel.id)
    assert central.deciders(emily, "security") == ["Daniel Hayes", "Mark Ellis"]  # the names, kept for a while
    central.clock.now += 11
    assert central.deciders(emily, "security") == ["Mark Ellis"]
    assert central.me(emily) == {
        "changes": "security", "live_review": "security", "deciders": ["Mark Ellis"], "text_deciders": ["Mark Ellis"],
        "deciders_more": 0, "text_deciders_more": 0,
    }  # fmt: skip
    assert central.decides_anything(mark) and central.decides_anything(paul) and central.decides_anything(emily)
    assert not central.decides_anything(daniel) and not central.decides_anything(laura)
    assert central.decider(daniel.id) is None and central.decider(mark.id) == mark


async def test_a_change_reaches_the_security_team_without_a_value_and_applies_when_approved(central, space, people):
    _, emily, mark, _, _ = people
    rt = space(emily)
    change = rt.propose(_lowered(rt), "command", "hermes")
    assert change.decides == "security" and change.deciders == ["Daniel Hayes", "Mark Ellis"]
    queue = central.queue(mark)
    [waiting] = queue["changes"]
    assert waiting["identity"] == {"id": emily.id, "name": "Emily Carter", "kind": "person"}
    assert waiting["group"] == "Payments" and waiting["route"] == "security" and not waiting["mine"]
    assert waiting["loosened"] == ["values never to mask: 1 added"] and "ACME" not in json.dumps(queue)
    assert await _decide_change(central, mark, change.id, True) == "approved"
    assert rt.config.allow_values == ["ACME"] and central.queue(mark)["changes"] == []
    [decided] = central.history(mark)
    assert decided["state"] == "approved" and decided["by"] == {"id": mark.id, "name": "Mark Ellis"}
    assert decided["identity"]["name"] == "Emily Carter" and decided["loosened"] == ["values never to mask: 1 added"]


async def test_nobody_approves_their_own_request_and_others_may_not_decide(central, space, people):
    directory, emily, mark, daniel, _ = people
    rt = space(mark)
    change = rt.propose(_lowered(rt), "command")
    assert change.deciders == ["Daniel Hayes"]  # never whoever asked
    assert central.queue(mark)["changes"][0]["mine"]
    assert await _decide_change(central, mark, change.id, True) == "yours"
    assert await _decide_change(central, emily, change.id, True) == "not-yours"  # not in the security team
    assert central.queue(emily) == {"changes": [], "batches": [], "more": 0}
    directory.granted[daniel.id] = set()  # his role taken away: refused at once, whatever names were told
    assert await _decide_change(central, daniel, change.id, True) == "not-yours"
    directory.granted[daniel.id] = {Role.SECURITY}
    assert rt.config.allow_values == []
    assert await _decide_change(central, daniel, change.id, True) == "approved"
    assert rt.config.allow_values == ["ACME"]


async def test_an_api_key_s_change_is_decided_by_whoever_controls_it(central, space, people, chosen):
    directory, emily, mark, _, paul = people
    chosen["settings"] = ApprovalsSettings()  # each decides on theirs: a key's changes go to its controllers [D2]
    key = directory.add("Billing CI", kind="key")
    rt = space(key)
    first = rt.propose(_lowered(rt), "command")
    assert first.decides == "controllers" and first.deciders == ["Paul Ferris"]
    assert await _decide_change(central, mark, first.id, True) == "not-yours"  # the security team does not decide it
    assert await _decide_change(central, paul, first.id, False) == "rejected"  # nobody controls it: an admin does
    directory.controlling[key.id] = [emily.id]
    second = rt.propose(_lowered(rt, "Northwind"), "command")
    assert await _decide_change(central, paul, second.id, True) == "not-yours"  # it has a controller now
    assert [c["id"] for c in central.queue(emily)["changes"]] == [second.id]
    assert await _decide_change(central, emily, second.id, True) == "approved"
    assert rt.config.allow_values == ["Northwind"]


async def test_a_change_made_in_a_key_s_space_is_never_approved_by_whoever_made_it(central, space, people, chosen):
    directory, emily, _, daniel, paul = people
    chosen["settings"] = ApprovalsSettings()  # a key's changes go to its controllers [D2]
    key = directory.add("Billing CI", kind="key")
    directory.controlling[key.id] = [emily.id, daniel.id]
    rt = space(key)
    with pytest.raises(Waits) as first:  # Emily saves in the key's space (E3 gives her as latet.acting)
        rt.update_config(_lowering(), from_dashboard=True, asked_by=emily.id)
    change = first.value.change
    assert change.decides == "controllers" and change.deciders == ["Daniel Hayes"]
    [waiting] = central.queue(emily)["changes"]
    assert waiting["mine"] and waiting["asked_by"] == {"id": emily.id, "name": "Emily Carter"}
    assert await _decide_change(central, emily, change.id, True) == "yours"
    assert not central.queue(daniel)["changes"][0]["mine"]
    assert await _decide_change(central, daniel, change.id, True) == "approved"
    assert rt.config.allow_values == ["ACME"]
    directory.controlling[key.id] = [emily.id]  # she alone controls it now: the admins decide, and she may withdraw
    with pytest.raises(Waits) as second:
        rt.update_config(_lowering("Northwind"), from_dashboard=True, asked_by=emily.id)
    assert second.value.change.deciders == ["Paul Ferris"]
    assert await _decide_change(central, emily, second.value.change.id, False) == "rejected"
    ended = {row["id"]: row["state"] for row in central.history(paul)}
    assert ended == {change.id: "approved", second.value.change.id: "withdrawn"}


async def test_a_stop_in_a_key_s_space_names_whoever_stopped_it_there_unless_they_asked_for_it(
    central, space, people, chosen
):
    """The whole-branch review's m1: in an API key's space, a change or a text stopped there (Undo it, Don't send) by
    someone working for the key who did not ask for it ends rejected by them, in the record and in its event, never as
    withdrawn by whoever asked; a change whoever made it stops there is withdrawn, as in a person's own space. Neither
    the agent's reason nor the dashboards take it for a decision of the company's."""
    directory, emily, _, daniel, _ = people
    chosen["settings"] = ApprovalsSettings(live_review="security")  # a key's changes to its controllers [D2]
    key = directory.add("Billing CI", kind="key")
    directory.controlling[key.id] = [emily.id, daniel.id]
    rt = space(key)
    approvals, told = rt.mediator.approvals, []
    publish = approvals.publish_change

    def kept(data, kind):
        told.append((kind, dict(data)))
        publish(data, kind)

    approvals.publish_change = kept
    with pytest.raises(Waits) as first:  # Emily saves in the key's space; Daniel undoes it there
        rt.update_config(_lowering(), from_dashboard=True, asked_by=emily.id)
    assert rt.decide_change(first.value.change.id, False, stopped_by=daniel.id) == "rejected"
    with pytest.raises(Waits) as second:  # Emily saves again, and undoes it herself
        rt.update_config(_lowering("Northwind"), from_dashboard=True, asked_by=emily.id)
    assert rt.decide_change(second.value.change.id, False, stopped_by=emily.id) == "rejected"
    batch = await approvals.open(_batch())  # the key's agent's text, for the security team: Emily stops it there
    assert batch.decides == "security"
    assert await approvals.decide(batch.id, False, stopped_by=emily.id)
    decision = await approvals.wait(batch, 1)
    assert decision is not None and (decision.reason, decision.by) == (approvals_module.REJECTED, None)
    for _ in range(100):  # the text's end reaches the record through the work threads
        if not rt._later_tasks:
            break
        await asyncio.sleep(0.01)
    ended = {row["id"]: (row["state"], row["by"]) for row in central.history(daniel)}
    assert ended == {
        first.value.change.id: ("rejected", {"id": daniel.id, "name": "Daniel Hayes"}),
        second.value.change.id: ("withdrawn", None),
        batch.id: ("rejected", {"id": emily.id, "name": "Emily Carter"}),
    }
    events = [(e["type"], e["outcome"], e["by"]) for e in rt.edition.events if e["type"] in ("change", "approval")]
    assert events == [
        ("change", "rejected", daniel.id),
        ("change", "rejected", None),
        ("approval", "rejected", emily.id),
    ]
    assert [data["by"] for kind, data in told if kind == "change_settled"] == [None, None]


async def test_an_admin_s_history_keeps_what_they_decided_for_a_key_whatever_else_its_controller_asked(
    central, space, people, chosen
):
    """Whether an admin sees a key's item is weighed for each item by whoever made it (the plan's ``_seen``): one that
    its lone controller made, which an admin decided, stays in the admin's history when a later item of the same key,
    which nobody made in its space, is that controller's to decide (and not the admin's)."""
    directory, emily, _, _, paul = people
    chosen["settings"] = ApprovalsSettings()
    key = directory.add("Billing CI", kind="key")
    directory.controlling[key.id] = [emily.id]
    rt = space(key)
    with pytest.raises(Waits) as made:  # Emily, its only controller, saves in its space: the admins decide
        rt.update_config(_lowering(), from_dashboard=True, asked_by=emily.id)
    assert await _decide_change(central, paul, made.value.change.id, True) == "approved"
    central.clock.now += 1
    later = rt.propose(_lowered(rt, "Northwind"), "command")  # nobody made it in the key's space: Emily decides
    assert await _decide_change(central, paul, later.id, True) == "not-yours"
    assert await _decide_change(central, emily, later.id, True) == "approved"
    assert [row["id"] for row in central.history(paul)] == [made.value.change.id]
    assert [row["id"] for row in central.history(emily)] == [later.id, made.value.change.id]


async def test_a_change_approved_after_the_person_s_settings_changed_is_stale(central, space, people):
    _, emily, mark, _, _ = people
    rt = space(emily)
    change = rt.propose(_lowered(rt), "command")
    rt.apply_config(rt.config.model_copy(update={"profile": Profile.MAX}), save=False)
    assert await _decide_change(central, mark, change.id, True) == "stale"
    assert rt.config.allow_values == [] and central.history(mark)[0]["state"] == "stale"


async def test_what_the_person_withdraws_is_withdrawn_and_a_disabled_identity_s_is_gone(central, space, people):
    directory, emily, mark, _, _ = people
    rt = space(emily)
    first = rt.propose(_lowered(rt), "command")
    assert rt.decide_change(first.id, False) == "rejected"  # Emily undoes it herself
    second = rt.propose(_lowered(rt, "Northwind"), "command")
    directory.disabled.add(emily.id)  # her account is disabled: nobody can carry a decision out
    assert central.queue(mark) == {"changes": [], "batches": [], "more": 0}
    assert {row["id"]: row["state"] for row in central.history(mark)} == {first.id: "withdrawn", second.id: "gone"}
    assert await _decide_change(central, mark, second.id, True) == "gone"


async def test_a_change_that_ended_where_its_end_was_not_recorded_leaves_the_queue(central, space, people, queues):
    _, emily, mark, _, _ = people
    rt = space(emily)
    change = rt.propose(_lowered(rt), "command")
    queues.ended[emily.id] = {change.id: "superseded"}  # its row in `changes` ended while the record did not hear
    assert central.queue(mark)["changes"] == []  # hidden at once
    assert central.history(mark) == []  # its own end, with who decided, may still be on its way: not ended yet
    central.clock.now += RECONCILE_EVERY
    central.queue(mark)  # the queue says so again a while later: ended as it ended there
    assert central.history(mark)[0]["state"] == "superseded"


async def test_a_text_reaches_the_security_team_when_opened_and_leaves_as_they_decided(central, space, people):
    _, emily, mark, _, _ = people
    rt = space(emily)
    batch = await rt.mediator.approvals.open(_batch())
    assert batch.decides == "security"
    [waiting] = central.queue(mark)["batches"]
    assert waiting["identity"]["name"] == "Emily Carter" and (waiting["agent"], waiting["items"]) == ("claude-code", 1)
    assert TEXT not in json.dumps(central.queue(mark))  # the queue holds no text
    row, capacity = central.admitted(None, mark.id, batch.id, "batch", None)
    assert capacity == "security" and (await central.batch(row, run)).items[0]["original"] == TEXT
    start = TEXT.index("Falcon")
    with pytest.raises(MaskError):  # checked before anything is recorded
        await _decide_batch(central, mark, batch.id, True, [{"item": 0, "start": start, "end": start + 3,
                                                             "category": "confidential"}])  # fmt: skip
    mask = {"item": 0, "start": start, "end": start + len("Falcon"), "category": "confidential"}
    assert await _decide_batch(central, mark, batch.id, True, [mask]) == "approved"
    decision = await rt.mediator.approvals.wait(batch, 1)
    assert decision is not None and decision.approved and decision.by == mark
    assert [m.value for m in decision.masks] == ["Falcon"]
    for _ in range(100):  # the record is told in the work threads: soon
        if central.history(mark):
            break
        await asyncio.sleep(0.01)
    [decided] = central.history(mark)
    assert (decided["kind"], decided["agent"], decided["items"]) == ("batch", "claude-code", 1)
    kept = json.dumps([dataclasses.asdict(row) for row in central.records.history(EVERYTHING, 10)])
    assert "Falcon" not in kept and "laura.bennett" not in kept  # the record keeps no text
    assert central.admitted(None, people[1].id, batch.id, "batch", None)[1] == "gone"


async def test_a_text_that_runs_out_of_time_or_is_lost_leaves_the_queue(central, space, people, queues, monkeypatch):
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    _, emily, mark, _, _ = people
    rt = space(emily)
    batch = await rt.mediator.approvals.open(_batch(timeout_s=10))
    batch.created -= 11
    await rt.mediator.approvals.wait(batch, 1)
    for _ in range(100):
        if central.history(mark):
            break
        await asyncio.sleep(0.01)
    assert central.queue(mark)["batches"] == [] and central.history(mark)[0]["state"] == "expired"
    assert await _decide_batch(central, mark, batch.id, True) == "gone"
    # A text the record holds and its queue no longer does (Valkey restarted): abandoned once a minute has passed.
    summary = {"agent": "codex", "kinds": ["user_text"], "items": 1}
    central.records.add(Row("lost1", emily.id, "Emily Carter", "person", "batch", "security", summary, central.clock()))
    queues.alive[emily.id] = set()
    assert central.queue(mark)["batches"] == [] and central.records.get("lost1").state is None  # hidden, still kept
    central.clock.now += LOST_AFTER + 1
    central.queue(mark)
    assert central.records.get("lost1").state == "abandoned"


async def test_a_change_recorded_before_its_row_is_written_waits_unlisted_and_ends_gone_later(central, people, queues):
    """R35 (the plan review's B-I3): the record is written before the change's row commits: a queue read in between
    does not end it, nor list it; past LOST_AFTER with no row, it ends ``gone``."""
    _, emily, mark, _, _ = people
    reasons = {"source": "dashboard", "agent": None, "loosened": ["values never to mask: 1 added"], "keys": ["x"]}
    central.records.add(Row("c9", emily.id, "Emily Carter", "person", "change", "security", reasons, central.clock()))
    queues.ended[emily.id] = {"c9": "gone"}  # no row of that id in `changes` yet
    assert central.queue(mark)["changes"] == [] and central.records.get("c9").state is None
    central.clock.now += LOST_AFTER + 1
    central.queue(mark)
    assert central.records.get("c9").state == "gone"


async def test_the_queues_are_asked_once_a_while_and_one_count_answers_the_menu(central, space, people, queues):
    """R39 (the plan review's M2): what is still waiting is asked of the spaces' queues once every RECONCILE_EVERY
    seconds an instance, whoever asks; the menu's count is the record's alone."""
    _, emily, mark, daniel, _ = people
    rt = space(emily)
    rt.propose(_lowered(rt), "command")
    asked = []
    central.ended_changes = lambda items: asked.append(len(items)) or {}
    central.queue(mark)
    central.queue(daniel)
    assert asked == [1]  # one query across identities, then the verdict kept a while
    central.clock.now += RECONCILE_EVERY + 1
    central.queue(mark)
    assert asked == [1, 1] and central.count(mark) == 1 and central.count(emily) == 0


@pytest.mark.parametrize("refusal", [OSError("written since"), SettingsRefused("more rules than a server keeps")])
async def test_a_decision_reads_the_space_s_settings_again_and_says_an_approval_not_applied(
    central, space, people, monkeypatch, refusal
):
    """R41, R70 (the plan review's M3, the re-review's RR3-L1): the asking space's settings are read again before a
    decision; an approval whose save is then refused (by the store, or for what the settings hold) is said as approved,
    not applied, never answered 500."""
    from latet.state.local import LocalSettings

    _, emily, mark, _, _ = people
    rt = space(emily)
    change = rt.propose(_lowered(rt), "command")
    watched = rt.mediator.approvals.settled = Watched()  # on a server, PostgreSQL (``PgSettled``)

    def refused(self, config):
        raise refusal

    monkeypatch.setattr(LocalSettings, "save", refused)
    assert await _decide_change(central, mark, change.id, True) == APPROVED_UNAPPLIED
    assert central.space.fresh[-1] is True and rt.config.allow_values == []
    assert central.records.get(change.id).state == APPROVED_UNAPPLIED  # R60: recorded as what it is
    # How it ended is read in the work threads, never on the event loop (the review of Task 6, M1).
    assert watched.read_in and threading.main_thread().ident not in watched.read_in


def test_what_the_record_keeps_of_a_change_is_its_reasons_bounded_never_its_guidance_or_destinations(central, people):
    """Task 5's review: the record keeps a change's reasons and nothing else of it (never its guidance, its
    destinations, its id or who decides, which the queue says from the row), however many reasons there are: at most
    ``MOST_REASONS`` of them, each cut to ``REASON_CHARS``, the rest counted in a last one."""
    from latet.ee.approvals import MOST_REASONS, REASON_CHARS

    _, emily, mark, _, _ = people
    reasons = [f"project Orion {n}: values never to mask: 1 added" for n in range(MOST_REASONS + 40)]
    reasons[0] = "projects " + ", ".join(["C:/work/" + "x" * 80] * 10) + ": values never to mask: 1 added"
    change = Change(Config(), "base", "dashboard", "hermes", reasons, ["projects"], decides="security",
                    guidance=[{"before": "Mask what names a client", "after": None}],
                    destinations=[{"name": "falcon", "before": "https://falcon.acme.com", "after": None}])  # fmt: skip
    central.opened(emily, change)
    kept = central.records.get(change.id).summary
    assert set(kept) == {"source", "agent", "loosened", "keys"}
    assert len(kept["loosened"]) == MOST_REASONS + 1 and kept["loosened"][-1] == "40 more"
    assert kept["loosened"][1:MOST_REASONS] == reasons[1:MOST_REASONS]
    # A reason too long is cut in its middle (the review of Task 6, M4): its start, and what it lowers at its end.
    cut = kept["loosened"][0]
    assert len(cut) == REASON_CHARS and "…" in cut and cut.startswith("projects C:/work/")
    assert cut.endswith(": values never to mask: 1 added")
    assert "falcon.acme.com" not in json.dumps(kept) and "client" not in json.dumps(kept)
    text = _batch(decides="security")
    text.agent = "a" * 500  # an agent's name as its request gives it
    central.opened(emily, text)
    assert central.records.get(text.id).summary == {"agent": "a" * 63 + "…", "kinds": ["user_text"], "items": 1}
    assert [row["id"] for row in central.queue(mark)["changes"]] == [change.id]


# --- the review of Task 6, I1: what a queue says ended reaches the record after the item's own end --------------------


async def test_a_text_approved_while_another_decider_s_page_reads_is_recorded_as_approved_by_who_decided(
    central, space, people, queues
):
    """I1, the first window: Mark's approval takes the text out of its queue, and the record hears it a thread's hop
    later; Daniel's page reads in between, finding no text there. The text is hidden from Daniel at once, and the record
    keeps Mark's approval, never ``abandoned`` by nobody."""
    _, emily, mark, daniel, _ = people
    rt = space(emily)
    batch = await rt.mediator.approvals.open(_batch())
    central.queue(daniel)  # it waits, as its queue says
    central.clock.now += LOST_AFTER + 1  # an old text: one its queue lost would be ended at once

    async def polled_between(work, *args):
        if work == central._end:  # the carry's own end of the record: Daniel's page reads just before it
            queues.alive[emily.id] = set()  # the text left its queue with Mark's decision
            assert central.queue(daniel)["batches"] == []
        return await asyncio.to_thread(work, *args)

    row, capacity = central.admitted(None, mark.id, batch.id, "batch", True)
    assert capacity == "security"
    rt.mediator.loop = asyncio.get_running_loop()  # as on a server: the runtime's own end goes to the work threads
    version = batch_version((await central.batch(row, run)).items)
    assert await central.carry_batch(row, True, [], [], mark, run=polled_between, version=version) == "approved"
    for _ in range(100):  # the runtime's own end, in the work threads: it finds the record ended
        if not rt._later_tasks:
            break
        await asyncio.sleep(0.01)
    [decided] = central.history(mark)
    assert decided["state"] == "approved" and decided["by"] == {"id": mark.id, "name": "Mark Ellis"}


@pytest.mark.parametrize(("who", "approve", "recorded"), [("mark", True, "approved"), ("emily", False, "withdrawn")])
async def test_a_change_ended_while_another_decider_s_page_reads_is_recorded_as_it_ended_and_by_whom(
    central, space, people, queues, who, approve, recorded
):
    """I1, the second window: the change's row in ``changes`` ends first, and the record hears it from the runtime's
    hook a moment later; Daniel's page reads in between, finding it ended. The record keeps how it ended with who ended
    it (Mark's approval; Emily's own undoing, ``withdrawn``), never what the queue alone says with nobody."""
    _, emily, mark, daniel, _ = people
    rt = space(emily)
    change = rt.propose(_lowered(rt), "command")
    central.queue(daniel)
    central.clock.now += RECONCILE_EVERY + 1  # due again at the next read
    told = central.ended

    def read_between(identity, item_id, kind, state, by, stopped_by=None):
        queues.ended[emily.id] = {item_id: state}  # its row ended: the queue says so
        assert central.queue(daniel)["changes"] == []
        told(identity, item_id, kind, state, by, stopped_by)

    central.ended = read_between
    decider = {"mark": mark, "emily": emily}[who]
    assert await _decide_change(central, decider, change.id, approve) == ("approved" if approve else "rejected")
    [ended] = central.history(mark)
    assert ended["state"] == recorded and ended["by"] == ({"id": mark.id, "name": "Mark Ellis"} if approve else None)


async def test_a_text_that_ran_out_of_time_while_a_page_reads_is_recorded_as_expired(
    central, space, people, queues, monkeypatch
):
    """I1, the person's own ends: an expiry reaches the record through the work threads (``_later``), later than its
    queue lets the text go; a page reading in between hides it, and the record says ``expired``, not ``abandoned``."""
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    _, emily, mark, daniel, _ = people
    rt = space(emily)
    batch = await rt.mediator.approvals.open(_batch(timeout_s=10))
    central.queue(daniel)
    central.clock.now += LOST_AFTER + 1
    told = central.ended

    def read_between(identity, item_id, kind, state, by, stopped_by=None):
        queues.alive[emily.id] = set()  # its queue let it go as it expired
        central.queue(daniel)
        told(identity, item_id, kind, state, by, stopped_by)

    central.ended = read_between
    batch.created -= 11
    await rt.mediator.approvals.wait(batch, 1)
    for _ in range(100):
        if central.history(mark):
            break
        await asyncio.sleep(0.01)
    assert central.history(mark)[0]["state"] == "expired"


# --- the review of Task 6, I2, M5, M6, and its carry of two deciders ------------------------------------------------


def _key_row(item_id, key, created, asked_by=None):
    reasons = {"source": "command", "agent": None, "loosened": ["values never to mask: 1 added"], "keys": ["x"]}
    return Row(item_id, key.id, key.name, "key", "change", "controllers", reasons, created, asked_by=asked_by)


async def test_an_admin_s_history_keeps_their_decisions_among_many_of_keys_others_control(central, people):
    """I2: an admin's history is read past what other people's keys fill (``WAITING_READ`` rows, then filtered, then
    cut to the limit, as ``count`` reads), and keeps what the admin decided even once the key has a controller."""
    directory, emily, _, _, paul = people
    lone, busy, later = (directory.add(name, kind="key") for name in ("Billing CI", "Release CI", "Deploy CI"))
    directory.controlling[busy.id] = [emily.id]
    for item_id, key, when in (("lone1", lone, 1.0), ("later1", later, 2.0)):
        central.records.add(_key_row(item_id, key, when))
        central.records.end(key.id, item_id, "approved", when, paul)
    for n in range(60):  # newer ends of a key Emily controls, which are hers to decide
        central.records.add(_key_row(f"busy{n}", busy, 10.0 + n))
        central.records.end(busy.id, f"busy{n}", "approved", 10.0 + n, emily)
    directory.controlling[later.id] = [emily.id]  # a controller since Paul decided: his decision is still his
    assert [row["id"] for row in central.history(paul)] == ["later1", "lone1"]
    assert len(central.history(emily, 10)) == 10


async def test_more_counts_what_waits_for_them_to_decide_beyond_what_is_listed(central, people, monkeypatch):
    """M5: past one read, ``more`` counts the same set on both sides: what waits for the approver to decide, their own
    requests left out of what is listed as well as of what is counted."""
    from latet.ee import approvals as ee_approvals

    monkeypatch.setattr(ee_approvals, "MOST_LISTED", 2)
    monkeypatch.setattr(ee_approvals, "WAITING_READ", 3)
    directory, emily, mark, daniel, paul = people
    reasons = {"source": "command", "agent": None, "loosened": ["values never to mask: 1 added"], "keys": ["x"]}
    askers = [mark, emily, daniel, paul, directory.add("Laura Bennett")]
    for n, asker in enumerate(askers):  # Mark's own first: listed, never his to decide
        central.records.add(Row(f"c{n}", asker.id, asker.name, "person", "change", "security", reasons, float(n)))
    queue = central.queue(mark)
    assert [row["id"] for row in queue["changes"]] == ["c0", "c1"] and queue["changes"][0]["mine"]
    assert queue["more"] == 3  # c2, c3 and c4 wait for him, and are not listed
    assert central.count(mark) == 4


async def test_the_queue_asks_each_identity_s_group_once(central, space, people):
    """M6: the group of whoever asked is read once for each identity listed, never once a row."""
    directory, emily, mark, _, _ = people
    rt = space(emily)
    rt.propose(_lowered(rt), "command")
    await rt.mediator.approvals.open(_batch())
    asked = []
    group_of = directory.group_of
    directory.group_of = lambda identity_id: asked.append(identity_id) or group_of(identity_id)
    queue = central.queue(mark)
    assert len(queue["changes"]) == 1 and len(queue["batches"]) == 1
    assert {row["group"] for row in queue["changes"] + queue["batches"]} == {"Payments"} and asked == [emily.id]


async def test_a_second_decider_who_finds_the_change_ended_by_the_first_is_told_it_is_gone(central, space, people):
    """The review's carry: two deciders admitted to one change at once, on two instances; Daniel's rejection finds the
    change waiting, then Mark's approval ends it first on the other; Daniel's is told ``gone``, never Mark's outcome as
    his own (``decide_change`` answers how it ended)."""
    _, emily, _, daniel, _ = people
    rt = space(emily)
    change = rt.propose(_lowered(rt), "command")
    second, capacity = central.admitted(None, daniel.id, change.id, "change", False)
    assert capacity == "security"
    approvals = rt.mediator.approvals
    settle = approvals.settle_change

    def raced(change_id, state, by=None, stopped_by=None):
        approvals.backend.end(change_id, "approved")  # Mark's approval, on another instance, lands first
        return settle(change_id, state, by, stopped_by)

    approvals.settle_change = raced
    assert await central.carry_change(second, False, daniel, run=run) == "gone"
    assert approvals.settled[change.id] == "approved"


async def test_a_rejection_whose_settings_are_not_written_back_stands_and_is_said_so(
    central, space, people, monkeypatch
):
    """The review of Task 6's carry: a rejection ends the change first, then writes the settings in use back; that save
    refused by the store leaves the rejection standing, and the decider is told it was rejected, never that nothing
    was decided."""
    from latet.state.local import LocalSettings

    _, emily, mark, _, _ = people
    rt = space(emily)
    change = rt.propose(_lowered(rt), "command")

    def refused(self, config):
        raise OSError("the store does not answer")

    monkeypatch.setattr(LocalSettings, "save", refused)
    assert await _decide_change(central, mark, change.id, False) == "rejected"
    assert central.records.get(change.id).state == "rejected" and rt.config.allow_values == []


def test_the_names_of_who_decides_are_bounded(central, people):
    """The review of Task 6's carry: a large security team is named on every item that waits and in /me, so the names
    are bounded (MOST_NAMED, the first in their order): enough to tell who decides."""
    directory, emily, *_ = people
    for number in range(MOST_NAMED + 5):
        directory.add(f"Security Person {number:03d}", roles=(Role.SECURITY,))
    named = central.deciders(emily, "security")
    assert len(named) == MOST_NAMED and named == sorted(named) and named[0] == "Daniel Hayes"
    told = central.me(emily)  # with how many more there are (the review of Task 7, m7)
    assert len(told["deciders"]) == MOST_NAMED and told["deciders_more"] == 7  # 27 in the security team
    assert told["text_deciders_more"] == 7


def test_a_small_team_is_named_whole(central, people):
    _, emily, *_ = people
    told = central.me(emily)
    assert told["deciders"] == ["Daniel Hayes", "Mark Ellis"] and told["deciders_more"] == 0
    assert told["text_deciders_more"] == 0


async def test_an_approval_the_floor_turns_into_a_rejection_whose_save_fails_answers_rejected(
    central, space, people, monkeypatch
):
    """The review of Task 7, m2: an approval below the company's floor of now is ended rejected by the decider, then
    the settings in use are written back; that save refused leaves the rejection standing, and it is answered as it
    stands, never "nothing was decided"."""
    from latet.state.local import LocalSettings

    _, emily, mark, _, _ = people
    rt = space(emily)
    change = rt.propose(_lowered(rt), "command")
    monkeypatch.setattr(rt, "_refused", lambda before, after, **kwargs: ["values never to mask: set by the company"])

    def refused(self, config):
        raise OSError("the store does not answer")

    monkeypatch.setattr(LocalSettings, "save", refused)
    assert await _decide_change(central, mark, change.id, True) == "rejected"
    assert central.records.get(change.id).state == "rejected" and rt.config.allow_values == []


async def test_an_end_written_by_a_queue_waits_again_when_how_it_ended_changes(central, people, queues):
    """The re-review of Task 6, m1: a young change first found missing in its queue (its row not written yet), then
    found ended at the next check, is ended in the record only once its queue has said that end for a while too, so
    that its own end, with who decided, comes first."""
    _, emily, mark, _, _ = people
    reasons = {"source": "dashboard", "agent": None, "loosened": ["values never to mask: 1 added"], "keys": ["x"]}
    central.records.add(Row("c8", emily.id, "Emily Carter", "person", "change", "security", reasons, central.clock()))
    queues.ended[emily.id] = {"c8": "gone"}  # no row of that id in `changes` yet
    assert central.queue(mark)["changes"] == []
    central.clock.now += RECONCILE_EVERY + 1
    queues.ended[emily.id] = {"c8": "approved"}  # its row written and ended: its own end on its way to the record
    assert central.queue(mark)["changes"] == [] and central.records.get("c8").state is None
    central.clock.now += RECONCILE_EVERY + 1
    central.queue(mark)
    assert central.records.get("c8").state == "approved"


def test_what_ended_past_the_retention_goes_now_when_asked_and_once_an_hour_from_the_history(central, people):
    """R73: ``prune_now`` (E4's jobs' thread, once an hour on one instance) removes what ended more than ``keep_days``
    ago and says how many; the history and the metrics prune too, at most once an hour on this instance (R52, R63)."""
    from latet.ee.approvals import PRUNE_EVERY

    _, emily, mark, _, _ = people
    day = 86400.0

    def ended(item_id: str, days_ago: float) -> None:
        at = central.clock() - days_ago * day
        central.records.add(Row(item_id, emily.id, "Emily Carter", "person", "change", "security", {}, at))
        central.records.end(emily.id, item_id, "approved", at, mark)

    ended("c_old", central.keep_days + 1)
    ended("c_kept", central.keep_days - 1)
    assert central.prune_now() == 1 and central.records.get("c_old") is None
    assert central.records.get("c_kept") is not None and central.prune_now() == 0
    central.history(mark)  # the first prune of this instance's hour
    ended("c_later", central.keep_days + 1)
    central.history(mark)
    assert central.records.get("c_later") is not None  # pruned within the hour already: left to the next
    central.clock.now += PRUNE_EVERY
    central.history(mark)
    assert central.records.get("c_later") is None


def test_a_text_withheld_where_it_was_sent_is_recorded_as_approved_and_changed(central, people):
    """The review of Task 10 (fix rounds 2 and 3): a text a decider approved, withheld where it was sent (masked then,
    it was not what they approved). The record says `approved-changed` whether the approval's own end came first or
    comes after (the instance that sends it may hear the decision before the deciding one writes its end); one that
    comes after still names who approved and counts the approval (m2). A text that ended otherwise is left as it
    ended."""
    _, emily, mark, _, _ = people
    counted: list[tuple[str, str]] = []
    central.counted = lambda kind, state: counted.append((kind, state))
    summary = {"agent": "claude-code", "kinds": ["user_text"], "items": 1}
    for item_id in ("b_ended_first", "b_ended_after", "b_rejected"):
        central.records.add(Row(item_id, emily.id, emily.name, emily.kind, "batch", "security", summary, 1.0))
    central.ended(emily, "b_ended_first", "batch", "approved", mark)
    central.withheld(emily, "b_ended_first")
    central.withheld(emily, "b_ended_after")
    central.ended(emily, "b_ended_after", "batch", "approved", mark)  # the decider's end, arriving after: named
    central.ended(emily, "b_rejected", "batch", "rejected", mark)
    central.withheld(emily, "b_rejected")
    ids = ("b_ended_first", "b_ended_after", "b_rejected")
    states = {item_id: central.records.get(item_id).state for item_id in ids}
    assert states == dict(zip(ids, ("approved-changed", "approved-changed", "rejected"), strict=True))
    assert counted == [("batch", "approved"), ("batch", "approved-changed"), ("batch", "approved-changed"),
                       ("batch", "approved"), ("batch", "rejected")]  # fmt: skip
    for item_id in ("b_ended_first", "b_ended_after"):  # who approved is named, whichever end came first
        row = central.records.get(item_id)
        assert (row.by_id, row.by_name) == (mark.id, "Mark Ellis"), item_id
    central.ended(emily, "b_ended_after", "batch", "approved", mark)  # once only
    assert counted[-1] == ("batch", "rejected")


async def test_an_approval_of_a_text_masked_again_since_its_decider_read_it_decides_nothing(central, space, people):
    """The review of Task 10, I1, in both places a decider's approval of a text passes: checked before anything is
    recorded (``check``), and where it is carried out (``carry_batch``, past a mask made between the two). Its record
    stays open, and the text as it is now is approved."""
    _, emily, mark, _, _ = people
    rt = space(emily)
    batch = await rt.mediator.approvals.open(_batch())
    read = batch_version(batch.items)
    row, capacity = central.admitted(None, mark.id, batch.id, "batch", True)
    assert row is not None and capacity == "security"
    await central.check(row, [], [], run, version=read)  # as he read it: it passes
    shown = [item | {"masked": item["original"].replace("Project Falcon", "Project Orion")} for item in batch.items]
    assert await rt.mediator.approvals.update(batch.id, shown)  # masked again while he looked
    with pytest.raises(Changed):
        await central.check(row, [], [], run, version=read)
    with pytest.raises(Changed):
        await central.carry_batch(row, True, [], [], mark, run=run, version=read)
    record = central.records.get(batch.id)
    assert record is not None and record.state is None  # nothing decided: it still waits
    assert await central.carry_batch(row, True, [], [], mark, run=run, version=batch_version(shown)) == "approved"
