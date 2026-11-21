"""Changes that lower protection, and the Live Reviewer's texts, go where the edition says: to the person whose space
it is (the Community Edition, as before), or to someone else (Latet Enterprise, a stand-in here): the company's
security team, or an API key's controllers, named to whoever waits. The person can undo what waits for others, never
approve it; a save in the dashboard that lowers protection waits for them too, and in an API key's space keeps who made
it; how each ends is an event, and the company's record is told."""

import asyncio
import json
import logging
import threading

import httpx
import pytest
from starlette.applications import Starlette

from latet import runtime as runtime_module
from latet.config import Config, IdentityConfig, LiveReview, Profile, load_config
from latet.engine.vault import MaskingError
from latet.gateway.app import create_app
from latet.gateway.approvals import Approvals, Batch, HumanMask, NotYours, Waits, batch_version
from latet.gateway.routes import Upstream
from latet.logline import Paced
from latet.runtime import make_runtime
from latet.server.shared import ChangeWaits, SettingsRefused
from latet.state import IdentityRef

from ..central import Company, Queue
from ..floors import CompanyFloor

MARK = IdentityRef("u_markellisa", "Mark Ellis", "person")
EMILY = IdentityRef("u_emilycarte", "Emily Carter", "person")
TEXT = "call Ada Byron on Monday about the Orion contract"


def _runtime(tmp_path, edition=None, identity=None):
    config = Config(identity=IdentityConfig(enabled=False), live_review=LiveReview(enabled=True))
    extra = {} if edition is None else {"edition": edition}
    if identity is not None:
        extra["identity"] = identity
    return make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", **extra)


@pytest.fixture
def company():
    return Company(Queue(["Daniel Hayes", "Mark Ellis"]))


@pytest.fixture
def rt(tmp_path, company):
    runtime = _runtime(tmp_path, company)
    yield runtime
    runtime.close()


def _lowered(current):
    return current.model_copy(update={"allow_values": ["ACME"]})


def _batch() -> Batch:
    item = {"kind": "user_text", "path": None, "tool": None, "original": TEXT, "masked": TEXT, "replacements": [],
            "suggestions": [], "failed": None}  # fmt: skip
    return Batch("cc:s", None, "claude-code", [item])


def test_a_change_that_lowers_protection_waits_for_the_security_team(rt, company):
    change = rt.propose(_lowered(rt.config), "command", "hermes")
    assert change is not None and change.decides == "security"
    assert change.deciders == ["Daniel Hayes", "Mark Ellis"] and company.queue.opens == [change]
    assert change.summary()["decides"] == "security" and rt.config.allow_values == []


def test_the_person_can_undo_it_but_not_approve_it(rt, company):
    change = rt.propose(_lowered(rt.config), "command")
    assert rt.decide_change(change.id, True) == "not-yours"
    assert change.id in rt.mediator.approvals.changes and rt.config.allow_values == []
    assert rt.decide_change(change.id, False) == "rejected"
    assert company.queue.ends == [(change.id, "change", "rejected", None)]


def test_the_security_team_s_approval_applies_it_and_is_recorded_with_who_decided(rt, company):
    change = rt.propose(_lowered(rt.config), "command", "hermes")
    assert rt.decide_change(change.id, True, by=MARK) == "approved"
    assert rt.config.allow_values == ["ACME"] and load_config(rt.config_file).allow_values == ["ACME"]
    assert company.queue.ends == [(change.id, "change", "approved", MARK)]
    [event] = [e for e in company.events if e["type"] == "change"]
    assert (event["outcome"], event["by"], event["reason"], event["agent"]) == (
        "approved",
        MARK.id,
        "command",
        "hermes",
    )


def test_a_change_approved_after_the_settings_changed_is_stale(rt, company):
    change = rt.propose(_lowered(rt.config), "command")
    rt.apply_config(rt.config.model_copy(update={"profile": Profile.MAX}), save=False)
    assert rt.decide_change(change.id, True, by=MARK) == "stale"
    assert rt.config.allow_values == [] and company.queue.ends == [(change.id, "change", "stale", MARK)]


def test_a_save_in_the_dashboard_that_lowers_protection_waits_too(rt):
    with pytest.raises(Waits) as waits:
        rt.update_config(_lowered, from_dashboard=True)
    assert waits.value.change.source == "dashboard" and waits.value.change.decides == "security"
    assert rt.config.allow_values == [] and load_config(rt.config_file).allow_values == []
    # What adds protection still applies at once.
    raised = rt.update_config(lambda current: current.model_copy(update={"profile": Profile.MAX}), from_dashboard=True)
    assert raised is not None and rt.config.profile == Profile.MAX


def test_turning_the_live_reviewer_off_in_the_dashboard_waits_and_leaves_it_on(rt):
    """The whole-branch review's m5 (Review Focus 1): the switch on the Live page, or the form, turning the Live
    Reviewer off is a save in the dashboard that lowers protection: it waits for the security team as any other, and
    the texts keep waiting meanwhile."""

    def off(current):
        return current.model_copy(update={"live_review": current.live_review.model_copy(update={"enabled": False})})

    rt.settings.save(rt.config)  # config.yaml holds the Live Reviewer on
    assert load_config(rt.config_file).live_review.enabled
    with pytest.raises(Waits) as waits:
        rt.update_config(off, from_dashboard=True)
    change = waits.value.change
    assert change.source == "dashboard" and change.decides == "security"
    assert change.summary()["loosened"] == ["the Live Reviewer: turned off"]
    assert rt.config.live_review.enabled and load_config(rt.config_file).live_review.enabled


@pytest.mark.parametrize(
    "refusal", [OSError("written since by another instance"), SettingsRefused("more rules than a server keeps")]
)
def test_an_approval_its_settings_do_not_take_is_told_as_it_is(tmp_path, monkeypatch, refusal):
    """R60, R70 (the plan re-reviews' RR-L1, RR3-L1): a decider's approval whose save is then refused, by the store or
    for what the settings hold, is told to the person's dashboard as approved and not applied, after the approval."""
    runtime = _runtime(tmp_path, Company(Queue(["Mark Ellis"])), identity=EMILY)
    try:
        change = runtime.propose(_lowered(runtime.config), "command")
        approvals, told = runtime.mediator.approvals, []
        publish = approvals.publish_change

        def kept(data, kind):
            told.append((kind, dict(data)))
            publish(data, kind)

        def refused(config):
            raise refusal

        monkeypatch.setattr(approvals, "publish_change", kept)
        monkeypatch.setattr(runtime.settings, "save", refused)
        with pytest.raises(type(refusal)):
            runtime.decide_change(change.id, True, by=MARK)
        assert told[-1] == ("change_settled", {"id": change.id, "state": "approved-unapplied", "by": "Mark Ellis"})
        assert runtime.config.allow_values == []
    finally:
        runtime.close()


def test_a_save_equal_to_a_change_that_waits_for_the_person_says_so_as_the_community_does(tmp_path):
    """R42 (the plan review's L1): a change opened while the person decided keeps its route; a save equal to it says it
    waits for them (decide on it first), never that the security team decides, and applies nothing."""
    company = Company(Queue(["Mark Ellis"]), changes="self")
    runtime = _runtime(tmp_path, company)
    try:
        before = runtime.propose(_lowered(runtime.config), "command")
        assert before is not None and before.decides == "self"
        company.changes = "security"  # the company chooses its security team afterwards
        with pytest.raises(ChangeWaits):
            runtime.update_config(_lowered, from_dashboard=True)
        assert runtime.config.allow_values == [] and runtime.decide_change(before.id, True) == "approved"
    finally:
        runtime.close()


def test_an_api_key_s_change_waits_for_its_controllers_and_keeps_who_made_it(tmp_path):
    company = Company(Queue(["Daniel Okafor"]), changes="controllers")
    runtime = _runtime(tmp_path, company)
    try:
        change = runtime.propose(_lowered(runtime.config), "command")
        assert change.decides == "controllers" and change.deciders == ["Daniel Okafor"]
        assert runtime.decide_change(change.id, True) == "not-yours"
        assert runtime.decide_change(change.id, False) == "rejected"
        # A save in the key's space, by one of the people who control it (E3's latet.acting): it waits for another.
        with pytest.raises(Waits) as waits:
            runtime.update_config(_lowered, from_dashboard=True, asked_by="u_emilycarte")
        made = waits.value.change
        assert made.asked_by == "u_emilycarte" and made.summary()["asked_by"] == "u_emilycarte"
        assert company.queue.askers[-1] == "u_emilycarte" and "controls this API key" in str(waits.value)
        assert runtime.config.allow_values == [] and runtime.decide_change(made.id, True) == "not-yours"
    finally:
        runtime.close()


def test_settings_that_arrive_are_routed_where_it_cannot_be_told_what_lowered_them(tmp_path):
    """[D1], R30: a change of source file stays the person's only when the server's shared settings alone lowered it
    (``own_lowers``, tested in tests/unit/server/test_shared.py); settings that are no server's cannot tell it, and the
    company decides."""
    company = Company(Queue())
    runtime = _runtime(tmp_path, company, identity=EMILY)
    try:
        change = runtime.propose(_lowered(runtime.config), "file")
        assert change is not None and change.decides == "security" and company.queue.opens == [change]
        assert runtime.decide_change(change.id, True) == "not-yours"
    finally:
        runtime.close()


def test_a_computer_s_destinations_never_save_what_waits_for_someone_else(tmp_path):
    """R29 (the plan review's C1): in a server's space, a computer's destinations while a change waits for the security
    team are saved as approved settings and what waits is asked again with them; what waits is never written as the
    person's own settings, which a re-application would offer them to approve."""
    company = Company(Queue())
    runtime = _runtime(tmp_path, company, identity=EMILY)
    try:
        with pytest.raises(Waits):
            runtime.update_config(_lowered, from_dashboard=True)
        names = runtime.add_destinations([("https://llm.acme.com/v1", "claude")])
        assert names and runtime.settings.load() == runtime.config  # what is kept is what is in use
        assert runtime.config.allow_values == [] and runtime.settings.reviewed() == runtime.config
        [again] = runtime.mediator.approvals.waiting_changes()
        assert again.decides == "security" and again.config.allow_values == ["ACME"]
        assert any(d.url == "https://llm.acme.com/v1" for d in again.config.upstreams)
    finally:
        runtime.close()


def test_an_api_key_s_save_leaves_a_change_that_waits_for_its_controllers(tmp_path):
    """R53: a save that lowers nothing in a key's space supersedes a change of its own, never one that waits for the
    key's other controllers, which ends stale at its decision."""
    company = Company(Queue(["Daniel Okafor"]), changes="controllers")
    key = IdentityRef("u_billingcia", "Billing CI", "key")
    runtime = _runtime(tmp_path, company, identity=key)
    try:
        with pytest.raises(Waits) as waits:
            runtime.update_config(_lowered, from_dashboard=True, asked_by="u_emilycarte")
        runtime.update_config(lambda current: current.model_copy(update={"profile": Profile.MAX}), from_dashboard=True)
        assert [c.id for c in runtime.mediator.approvals.waiting_changes()] == [waits.value.change.id]
        assert runtime.decide_change(waits.value.change.id, True, by=MARK) == "stale"
    finally:
        runtime.close()


def test_settings_applied_by_latet_itself_are_not_held(rt):
    """A computer's destination, the server's re-application: not the person's save, so not held."""
    assert rt.update_config(_lowered) is not None and rt.config.allow_values == ["ACME"]


def test_on_the_community_edition_the_person_decides_as_before(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        assert runtime.edition.central() is None
        change = runtime.propose(_lowered(runtime.config), "command")
        assert change.decides == "self" and change.deciders == []
        assert runtime.decide_change(change.id, True) == "approved"
        relaxed = runtime.update_config(
            lambda current: current.model_copy(update={"allow_values": ["ACME", "Northwind"]}), from_dashboard=True
        )
        assert relaxed is not None
    finally:
        runtime.close()


async def test_texts_wait_for_the_security_team_when_the_company_says_so(tmp_path):
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        approvals = runtime.mediator.approvals
        batch = await approvals.open(_batch())
        assert batch.decides == "security" and batch.deciders == ["Mark Ellis"] and company.queue.opens == [batch]
        with pytest.raises(NotYours):
            await approvals.decide(batch.id, True)
        assert await approvals.decide(batch.id, False)
        decision = await approvals.wait(batch, 1)
        assert decision is not None and decision.state == "rejected" and decision.by is None
        for _ in range(100):  # the record is told in the work threads, never in the way: soon
            if company.queue.ends:
                break
            await asyncio.sleep(0.01)
        assert company.queue.ends == [(batch.id, "batch", "rejected", None)]
        [event] = [e for e in company.events if e["type"] == "approval"]
        assert event["outcome"] == "rejected" and event["stopped"] == 1 and event["by"] is None
        assert event["agent"] == "claude-code"
    finally:
        runtime.close()


def test_a_change_the_company_s_record_cannot_take_neither_waits_nor_applies(tmp_path):
    runtime = _runtime(tmp_path, Company(Queue(broken=True)))
    try:
        with pytest.raises(ConnectionError):
            runtime.update_config(_lowered, from_dashboard=True)
        assert runtime.mediator.approvals.pending_changes() == [] and runtime.config.allow_values == []
    finally:
        runtime.close()


# --- carried from the review of Task 1 -------------------------------------------------------------------------------


class _Floored(Company):
    """A company that decides elsewhere and has a floor (``locked``: None until it comes)."""

    def __init__(self, queue: Queue) -> None:
        super().__init__(queue)
        self.locked = None

    def floor(self, identity):
        return self.locked


def test_a_dashboard_save_that_waits_is_rejected_once_the_floor_rises_above_it(tmp_path):
    """Task 1's review, C1: a save in the dashboard is the person's own change, as a command is. Asked again once the
    company's floor rose above what it lowers (a computer's destinations), it is rejected at once, never opened again
    for the security team, who could not approve it."""
    company = _Floored(Queue())
    runtime = _runtime(tmp_path, company)
    try:
        with pytest.raises(Waits) as waits:
            runtime.update_config(_lowered, from_dashboard=True)
        company.locked = CompanyFloor()  # it counts none of a person's own exceptions
        assert runtime.add_destinations([("https://llm.acme.com/v1", "claude")])
        approvals = runtime.mediator.approvals
        assert approvals.settled[waits.value.change.id] == "rejected" and approvals.waiting_changes() == []
        assert load_config(runtime.config_file).allow_values == [] and runtime.config.allow_values == []
    finally:
        runtime.close()


def test_a_change_that_cannot_be_kept_is_dropped_from_the_company_s_record(rt, company, monkeypatch):
    """Task 1's review, M2: routed and recorded, then refused by the queue, the change never waited: the record
    forgets it."""

    def refused(change):
        raise OSError("the database does not answer")

    monkeypatch.setattr(rt.mediator.approvals.backend, "open", refused)
    with pytest.raises(OSError, match="does not answer"):
        rt.propose(_lowered(rt.config), "command")
    [opened] = company.queue.opens
    assert company.queue.drops == [opened.id] and rt.mediator.approvals.pending_changes() == []


async def test_a_text_that_cannot_be_kept_is_dropped_and_why_it_was_not_kept_is_what_is_raised(
    tmp_path, monkeypatch, caplog
):
    """Task 1's review, M2 and C2: routed and recorded, then refused by the queue, the text never waited: the record
    forgets it. A record that cannot forget it is said by its type, once a minute, and never hides the queue's
    refusal, which is what the agent's request is told."""
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        approvals = runtime.mediator.approvals

        async def refused(batch, announce=None):
            raise OSError("the queue does not answer")

        monkeypatch.setattr(approvals.backend, "store", refused)
        batch = _batch()
        with pytest.raises(OSError, match="the queue does not answer"):
            await approvals.open(batch)
        assert company.queue.opens == [batch] and company.queue.drops == [batch.id]

        def unreachable(identity, item_id):
            raise ConnectionError("db.acme.internal does not answer")

        monkeypatch.setattr(company.queue, "dropped", unreachable)
        monkeypatch.setattr(runtime_module, "_UNDROPPED", Paced())
        caplog.set_level(logging.WARNING)
        with pytest.raises(OSError, match="the queue does not answer"):  # the queue's refusal, not the record's
            await approvals.open(_batch())
        said = [r.getMessage() for r in caplog.records if "company's record" in r.getMessage()]
        assert len(said) == 1 and "(ConnectionError)" in said[0] and "db.acme.internal" not in caplog.text
    finally:
        runtime.close()


async def test_a_hook_that_cannot_forget_a_text_never_hides_why_it_was_not_kept(monkeypatch):
    """Task 1's review, C2: whatever the hook that forgets a text raises, ``open`` raises the queue's refusal."""
    approvals = Approvals()

    async def routed(batch):
        batch.decides = "security"

    async def unforgetting(batch_id):
        raise RuntimeError("the company's record does not answer")

    async def refused(batch, announce=None):
        raise OSError("the queue does not answer")

    approvals.route_batch, approvals.dropped_batch = routed, unforgetting
    monkeypatch.setattr(approvals.backend, "store", refused)
    with pytest.raises(OSError, match="the queue does not answer"):
        await approvals.open(_batch())


# --- the review of Task 2 ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("then", "now"), [("security", "self"), ("self", "security")])
def test_a_change_asked_again_keeps_the_route_it_opened_with(tmp_path, then, now):
    """I1 and [D4]: what waits keeps its route. A change asked again with a computer's destinations, after the company
    changed who decides, waits for whoever it waited for, named as before."""
    company = Company(Queue(["Mark Ellis"]), changes=then)
    runtime = _runtime(tmp_path, company)
    try:
        change = runtime.propose(_lowered(runtime.config), "command")
        assert change is not None and change.decides == then
        company.changes, company.queue.names = now, ["Daniel Hayes"]
        assert runtime.add_destinations([("https://llm.acme.com/v1", "claude")])
        [again] = runtime.mediator.approvals.waiting_changes()
        assert again.id != change.id and (again.decides, again.deciders) == (change.decides, change.deciders)
        assert any(d.url == "https://llm.acme.com/v1" for d in again.config.upstreams)
    finally:
        runtime.close()


def test_a_change_asked_again_over_a_composition_keeps_its_route(tmp_path, monkeypatch):
    """I1: ``_asked_again`` (a person's save over a composition, on a server) asks the change waiting again with the
    settings kept: it waits for the security team still, though the company chose since to let people decide."""
    company = Company(Queue(["Mark Ellis"]))
    runtime = _runtime(tmp_path, company, identity=EMILY)
    try:
        change = runtime.propose(_lowered(runtime.config), "file")
        assert change is not None and change.decides == "security"
        company.changes = "self"
        kept = _lowered(runtime.config).model_copy(update={"profile": Profile.MAX})
        monkeypatch.setattr(runtime, "_held", lambda: (kept, runtime.config))  # what a composition holds
        with runtime._lock:
            runtime._asked_again()
        [again] = runtime.mediator.approvals.waiting_changes()
        assert again.id != change.id and again.decides == "security" and again.config.profile == Profile.MAX
        assert runtime.decide_change(again.id, True) == "not-yours"
    finally:
        runtime.close()


def _unreachable(*args):
    raise ConnectionError("db.acme.internal does not answer")


def test_how_a_change_ended_is_an_event_even_when_the_company_s_record_cannot_be_told(tmp_path, monkeypatch):
    """m1 (R17): the record is told first, and the event of the end goes out whatever it answered."""
    company = Company(Queue())
    runtime = _runtime(tmp_path, company)
    try:
        change = runtime.propose(_lowered(runtime.config), "command", "hermes")
        monkeypatch.setattr(company.queue, "ended", _unreachable)
        assert runtime.decide_change(change.id, False) == "rejected"
        [event] = [e for e in company.events if e["type"] == "change"]
        assert (event["outcome"], event["agent"], event["reason"]) == ("rejected", "hermes", "command")
    finally:
        runtime.close()


async def test_how_a_text_ended_is_an_event_even_when_the_record_cannot_be_told_and_that_is_said_once(
    tmp_path, monkeypatch, caplog
):
    """m1 and m2: with no event loop yet, the record of a text's end is told at once; its failure neither loses the
    event nor reaches the decision, and is said by its type, once a minute."""
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        approvals = runtime.mediator.approvals
        batches = [await approvals.open(_batch()), await approvals.open(_batch())]
        assert runtime.mediator.loop is None  # nothing served yet: the record is told here, at once
        monkeypatch.setattr(company.queue, "ended", _unreachable)
        monkeypatch.setattr(runtime_module, "_UNTOLD_LATER", Paced())
        caplog.set_level(logging.WARNING)
        for batch in batches:
            assert await approvals.decide(batch.id, False)
        events = [e for e in company.events if e["type"] == "approval"]
        assert [e["outcome"] for e in events] == ["rejected", "rejected"]
        said = [r.getMessage() for r in caplog.records if "record of approvals was not told" in r.getMessage()]
        assert len(said) == 1 and "(ConnectionError)" in said[0] and "db.acme.internal" not in caplog.text
    finally:
        runtime.close()


# --- a decider's approval through the gateway (proxy agents: Codex, OpenCode): the review of Task 10, fix round 3, m1


OPENAI = Upstream("openai", "/openai", "http://127.0.0.1:1")
EMAIL = "emily.carter@acme.com"
ASKED = f"ask {EMAIL} about the Orion contract"


def _chat(text: str) -> bytes:
    return json.dumps({"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": text}]}).encode()


async def _waiting_at_the_gateway(runtime, text: str):
    """A proxy agent's request through the gateway, its new text waiting for the security team: the request's task,
    and the batch as the dashboards are told it."""
    mediator = runtime.mediator
    headers = {"authorization": "Bearer sk-test"}
    prepared = mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", headers, _chat(text))
    assert prepared.block_reason is None
    feed = await mediator.approvals.watch()
    waiting = asyncio.ensure_future(mediator.live_prepared(prepared, "codex"))
    while (told := await asyncio.wait_for(feed.get(), 10))[0] != "pending":
        pass
    mediator.approvals.unwatch(feed)
    return waiting, told[1]


async def _read(runtime, batch_id: str) -> str:
    batch = await runtime.mediator.approvals.backend.fetch(batch_id)
    assert batch is not None
    return batch_version(batch.items)


async def test_a_text_a_decider_approved_leaves_the_gateway_as_they_read_it(tmp_path):
    """The gateway's preview of a text (the request's masking, settled, read back from its slot) is the one the
    sending's comparison makes: an unchanged text a decider approved leaves, masked as they read it."""
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        waiting, batch = await _waiting_at_the_gateway(runtime, ASKED)
        assert batch["decides"] == "security"
        version = await _read(runtime, batch["id"])
        assert await runtime.mediator.approvals.decide(batch["id"], True, by=MARK, version=version)
        sent = await asyncio.wait_for(waiting, 10)
        assert sent.block_reason is None, sent.block_reason
        assert EMAIL not in sent.body.decode() and "Orion" in sent.body.decode()
        assert company.queue.withholds == []
    finally:
        runtime.close()


async def test_a_lowering_between_the_gateway_s_comparison_and_its_masking_still_withholds_the_text(
    tmp_path, monkeypatch
):
    """The review of Task 10, fix round 4 (I-a), on the gateway's path: the rules change right after the sending's
    comparison and before its real masking. The request is stopped all the same, the record told, and the agent's next
    request with the text waits for the security team again."""
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        waiting, batch = await _waiting_at_the_gateway(runtime, ASKED)
        version = await _read(runtime, batch["id"])
        mediator = runtime.mediator
        compared = mediator._views_now

        def lowered_after(*args, **kwargs):
            views = compared(*args, **kwargs)
            runtime.update_config(lambda config: config.model_copy(update={"allow_values": [EMAIL]}))
            return views

        monkeypatch.setattr(mediator, "_views_now", lowered_after)
        assert await mediator.approvals.decide(batch["id"], True, by=MARK, version=version)
        stopped = await asyncio.wait_for(waiting, 10)
        assert stopped.block_reason is not None and "masked otherwise" in stopped.block_reason, stopped.block_reason
        for _ in range(100):
            if company.queue.withholds:
                break
            await asyncio.sleep(0.01)
        assert company.queue.withholds == [batch["id"]]
        monkeypatch.setattr(mediator, "_views_now", compared)
        again, waits = await _waiting_at_the_gateway(runtime, ASKED)
        assert waits["id"] != batch["id"] and waits["decides"] == "security"
        assert await mediator.approvals.decide(waits["id"], False, by=MARK)
        await asyncio.wait_for(again, 10)
    finally:
        runtime.close()


async def test_a_text_masked_otherwise_when_the_gateway_sends_it_than_a_decider_approved_it_is_withheld(
    tmp_path, monkeypatch
):
    """A change that lowers protection lands between the decider's approval and the gateway's sending (the address let
    through): the request is stopped, the record told (``approved-changed``), and the agent's next request with the text
    waits for the security team again."""
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        waiting, batch = await _waiting_at_the_gateway(runtime, ASKED)
        version = await _read(runtime, batch["id"])
        mediator = runtime.mediator
        applied = mediator._apply_live_prepared

        def lowered_first(prepared, items, decision):
            runtime.update_config(lambda config: config.model_copy(update={"allow_values": [EMAIL]}))
            return applied(prepared, items, decision)

        monkeypatch.setattr(mediator, "_apply_live_prepared", lowered_first)
        feed = await mediator.approvals.watch()  # her dashboard
        assert await mediator.approvals.decide(batch["id"], True, by=MARK, version=version)
        stopped = await asyncio.wait_for(waiting, 10)
        assert stopped.block_reason is not None and "masked otherwise" in stopped.block_reason
        told = [feed.get_nowait() for _ in range(feed.qsize())]
        assert ("withheld", {"id": batch["id"]}) in told  # told in her own words (fix round 3, m3)
        mediator.approvals.unwatch(feed)
        for _ in range(100):  # the record is told in the work threads, never in the way: soon
            if company.queue.withholds:
                break
            await asyncio.sleep(0.01)
        assert company.queue.withholds == [batch["id"]]
        again, waits = await _waiting_at_the_gateway(runtime, ASKED)
        assert waits["id"] != batch["id"] and waits["decides"] == "security"
        assert await mediator.approvals.decide(waits["id"], False, by=MARK)
        await asyncio.wait_for(again, 10)
    finally:
        runtime.close()


# --- a decider's approval whose texts are not given because the settings changed (the merge of 0.12.0, fix round 1)

ENGINE = "/_latet/engine/v1"


async def _next_pending(feed: asyncio.Queue, request: asyncio.Future) -> dict | None:
    """The next batch the dashboards are told waits, or None when ``request`` ends first (it waited for nobody)."""
    while True:
        told = asyncio.ensure_future(feed.get())
        done, _ = await asyncio.wait({told, request}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if told not in done:
            told.cancel()
            return None
        kind, data = told.result()
        if kind == "pending":
            return data


@pytest.mark.parametrize("when", ["over-its-answer", "before-it"])
async def test_a_decider_s_approval_the_bridge_refuses_because_settings_changed_is_withheld_and_waits_again(
    tmp_path, monkeypatch, when
):
    """The security review of the merge of 0.12.0, M1: the security team approves a text of the plugin as they read it,
    then 0.12's check of the answer refuses it (503, the settings changed while it was answered), an apply having begun
    over the decided masking (``over-its-answer``) or settings under way as it comes (``before-it``). The text was taken
    as approved all the same and never recorded as withheld: sent again, it no longer waited, and left masked by the new
    settings (here the address let through), which no decider read. It is now taken as approved only once it is given;
    refused, the company's record is told it was withheld, and sent again it waits for the security team again."""
    from latet.bridge.api import SETTINGS_CHANGED, Bridge

    monkeypatch.setattr("latet.bridge.api.HOLD", 5.0)
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        bridge = Bridge(runtime.mediator, runtime.gateway.bridge_token)
        transport = httpx.ASGITransport(app=Starlette(routes=bridge.routes()))
        token = {"x-latet-token": runtime.gateway.bridge_token}
        body = {"session": "live-9", "items": [{"text": ASKED, "kind": "user_text"}]}
        engine = runtime.built.engine
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False) as client:
            first = (await client.post(f"{ENGINE}/mask", json=body, headers=token)).json()
            pending = first["pending"]
            assert first["decides"] == "security", first
            version = await _read(runtime, pending)
            real = bridge._decided

            def lowered_after(waiting, decision):
                out = real(waiting, decision)  # masked as the decider read it
                runtime.update_config(lambda config: config.model_copy(update={"allow_values": [EMAIL]}))
                return out

            if when == "over-its-answer":
                monkeypatch.setattr(bridge, "_decided", lowered_after)
            else:  # settings that could not be put back: the mark held as the decision comes (``Engine.stranded``)
                engine.applying, engine.stranded = engine.applying + 1, True
            assert await runtime.mediator.approvals.decide(pending, True, by=MARK, version=version)
            answer = await client.post(f"{ENGINE}/approvals/wait", json={"id": pending}, headers=token)
            for _ in range(500):  # the bridge's answer worked out, and the record told in the work threads
                if not bridge._answering and company.queue.withholds:
                    break
                await asyncio.sleep(0.01)
            if when == "over-its-answer":
                monkeypatch.setattr(bridge, "_decided", real)
            else:
                engine.applying, engine.stranded = engine.applying - 1, False
            feed = await runtime.mediator.approvals.watch()
            again = asyncio.ensure_future(client.post(f"{ENGINE}/mask", json=body, headers=token))
            waits = await _next_pending(feed, again)
            runtime.mediator.approvals.unwatch(feed)
            sent_again = (await asyncio.wait_for(again, 10)).json()
        assert answer.status_code == 503 and answer.json() == {"error": SETTINGS_CHANGED}, answer.text
        assert company.queue.withholds == [pending]
        assert waits is not None and waits["id"] != pending and waits["decides"] == "security", sent_again
        assert sent_again.get("pending") == waits["id"] and EMAIL not in json.dumps(sent_again)
    finally:
        runtime.close()


async def test_a_decider_s_approval_the_gateway_refuses_because_settings_changed_is_withheld_and_waits_again(tmp_path):
    """The security review of the merge of 0.12.0, L1, the gateway's path of M1: a proxy agent's text the security
    team approved as they read it, then settings that change after its reviews and before it leaves (here the address
    let through), so that the request is refused because its settings changed (400, nothing sent). The text stayed
    approved and was never recorded as withheld: the agent's next request with it did not wait, and left masked by the
    new settings, which no decider read. Refused so now, its approval is taken back and the company's record told it
    was withheld; the next request with it waits for the security team again."""
    from latet.config import Destination

    from ..request_recorder import RecordingClient, RecordingTransport
    from .test_request_transport import _Provider

    company = Company(Queue(), changes="self", texts="security")
    acme = Destination(name="acme", url="https://llm.acme.invalid")
    config = Config(identity=IdentityConfig(enabled=False), live_review=LiveReview(enabled=True), upstreams=[acme])
    runtime = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=company)
    transport = RecordingTransport(_Provider())
    runtime.gateway.client = lambda: RecordingClient(transport)
    mediator = runtime.mediator
    reviewed = mediator.live_prepared
    moved: list[bool] = []

    async def lowered_after(prepared, *args, **kwargs):
        done = await reviewed(prepared, *args, **kwargs)
        if not moved:  # the first request alone: after its reviews, before it leaves
            moved.append(True)
            runtime.update_config(lambda current: current.model_copy(update={"allow_values": [EMAIL]}))
        return done

    mediator.live_prepared = lowered_after
    app = create_app(runtime.gateway)
    headers = {"authorization": "Bearer sk-test", "content-type": "application/json"}
    chat = "/acme/v1/chat/completions"
    try:
        # The app's lifespan makes its forwarder, as a server's start does (an ASGI transport runs none of its own).
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283", trust_env=False
            ) as client,
        ):
            feed = await mediator.approvals.watch()
            first = asyncio.ensure_future(client.post(chat, content=_chat(ASKED), headers=headers))
            batch = await _next_pending(feed, first)
            assert batch is not None and batch["decides"] == "security"
            version = await _read(runtime, batch["id"])
            assert await mediator.approvals.decide(batch["id"], True, by=MARK, version=version)
            refused = await asyncio.wait_for(first, 10)
            for _ in range(500):  # the record is told in the work threads
                if company.queue.withholds:
                    break
                await asyncio.sleep(0.01)
            again = asyncio.ensure_future(client.post(chat, content=_chat(ASKED), headers=headers))
            waits = await _next_pending(feed, again)
            if waits is not None:
                assert await mediator.approvals.decide(waits["id"], False, by=MARK)
            await asyncio.wait_for(again, 10)
            mediator.approvals.unwatch(feed)
    finally:
        runtime.close()
    assert refused.status_code == 400 and "the settings changed" in refused.text, refused.text
    assert company.queue.withholds == [batch["id"]]
    assert waits is not None and waits["id"] != batch["id"] and waits["decides"] == "security"
    assert all(EMAIL.encode() not in sent.content for sent in transport.sent), "the address left in clear"


async def test_a_decider_s_approval_whose_answer_the_bridge_cannot_give_is_recorded_withheld(tmp_path, monkeypatch):
    """Fix round 3 of the merge of 0.12.0: the security team approves a text of the plugin as they read it, and the
    bridge cannot give its answer (here its masking fails after the decision). The text is not taken as approved, so
    sent again it waits again (fix round 1), and now the company's record no longer keeps the batch approved: it is
    recorded withheld."""
    from latet.bridge.api import Bridge

    monkeypatch.setattr("latet.bridge.api.HOLD", 5.0)
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        bridge = Bridge(runtime.mediator, runtime.gateway.bridge_token)
        transport = httpx.ASGITransport(app=Starlette(routes=bridge.routes()))
        token = {"x-latet-token": runtime.gateway.bridge_token}
        body = {"session": "live-9", "items": [{"text": ASKED, "kind": "user_text"}]}
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False) as client:
            pending = (await client.post(f"{ENGINE}/mask", json=body, headers=token)).json()["pending"]
            version = await _read(runtime, pending)

            def failing(waiting, decision):
                raise RuntimeError("the images could not be rendered")

            monkeypatch.setattr(bridge, "_decided", failing)
            assert await runtime.mediator.approvals.decide(pending, True, by=MARK, version=version)
            answer = await client.post(f"{ENGINE}/approvals/wait", json={"id": pending}, headers=token)
            for _ in range(500):  # the record is told in the work threads
                if not bridge._answering and company.queue.withholds:
                    break
                await asyncio.sleep(0.01)
        assert answer.status_code == 500 and answer.json() == {"error": "masking failed"}, answer.text
        assert company.queue.withholds == [pending]
    finally:
        runtime.close()


# --- the final security review of Latet Enterprise (approvals): a decider's approval is never kept "approved" for
# --- texts that never left, and no batch is kept waiting by nobody

LAUNCH_KEY = "latet_lk_" + "A" * 43  # the shape of a Latet launch key, which never leaves


async def _withheld(company, batch_id: str) -> None:
    """The company's record told that ``batch_id`` was withheld (in the work threads: soon)."""
    for _ in range(500):
        if company.queue.withholds:
            break
        await asyncio.sleep(0.01)
    assert company.queue.withholds == [batch_id]


@pytest.mark.parametrize("failure", ["left-in-clear", "an-error", "an-unsafe-mask"])
async def test_a_decider_s_approval_the_gateway_cannot_apply_is_recorded_withheld(tmp_path, monkeypatch, failure):
    """The final security review, L1: Mark approves Emily's text as he read it, and applying his approval where her
    request waits fails otherwise than because it would leave masked otherwise than he read it: a value he masked
    would leave in clear there (``left-in-clear``), any other error (``an-error``), or a mask Latet never sends
    (``an-unsafe-mask``: the request discarded). Nothing left, and the company's record kept the batch approved by
    Mark all the same: Company approvals said Approved, not Approved, not sent. It is now recorded withheld, whatever
    stopped it."""
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        mediator = runtime.mediator
        if failure == "an-unsafe-mask":
            waited = mediator.await_decision

            async def unsafe(batch, gone, held=None):
                decision = await waited(batch, gone, held)
                decision.masks = [HumanMask(0, LAUNCH_KEY, "secret")]
                return decision

            monkeypatch.setattr(mediator, "await_decision", unsafe)
        else:
            raised = (
                MaskingError("a value masked in the Live Reviewer would have left in clear")
                if failure == "left-in-clear"
                else RuntimeError("the vault's store did not answer")
            )

            def failing(prepared, items, decision):
                raise raised

            monkeypatch.setattr(mediator, "_apply_live_prepared", failing)
        waiting, batch = await _waiting_at_the_gateway(runtime, ASKED)
        version = await _read(runtime, batch["id"])
        assert await mediator.approvals.decide(batch["id"], True, by=MARK, version=version)
        if failure == "an-error":
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(waiting, 10)
        else:
            stopped = await asyncio.wait_for(waiting, 10)
            assert stopped.block_reason is not None and stopped.decider_approval is None, stopped.block_reason
        await _withheld(company, batch["id"])
    finally:
        runtime.close()


class _ClosingProvider:
    """A provider's WebSocket that closes on its own when told (its idle limit, or its connection's), sent nothing."""

    def __init__(self) -> None:
        from websockets.sync.server import serve

        self.now = threading.Event()
        self.got: list[str | bytes] = []
        self.server = serve(self._held, "127.0.0.1", 0, open_timeout=1, close_timeout=1)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def _held(self, conn) -> None:
        self.now.wait(20)
        conn.close()

    def close(self) -> None:
        self.now.set()
        self.server.shutdown()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


async def test_a_websocket_turn_whose_relay_ends_while_it_waits_is_abandoned(tmp_path):
    """The final security review, L2: Emily's Codex sends a turn over a WebSocket, and it waits for the security team.
    The provider's socket closes first, so the relay ends while the turn waits. Its batch stayed in her queue, held by
    nobody, and Mark's approval of it hours later was recorded approved and told her so, though nothing could leave.
    The batch is now abandoned as the relay ends: recorded so, and no longer there to approve."""
    from latet.gateway import ws
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable

    from .test_request_outcomes import _Client

    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    provider, client = _ClosingProvider(), _Client()
    try:
        mediator = runtime.mediator
        expected = frozenset({("model", "m")})
        route = Upstream("relay", "/relay", f"http://127.0.0.1:{provider.port}", envelope_expected=expected)
        turn = {
            "type": "response.create",
            "model": "m",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": ASKED}]}],
        }
        feed = await mediator.approvals.watch()
        relaying = asyncio.create_task(ws.relay(client.socket, RouteTable([route]), Capture(None), mediator))
        client.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(turn)})
        batch = await _next_pending(feed, relaying)
        mediator.approvals.unwatch(feed)
        assert batch is not None and batch["decides"] == "security"
        provider.now.set()  # the provider's socket closes while the turn waits
        await asyncio.wait_for(relaying, 10)
        for _ in range(500):  # the record is told in the work threads
            if company.queue.ends:
                break
            await asyncio.sleep(0.01)
        assert company.queue.ends == [(batch["id"], "batch", "abandoned", None)]
        assert batch["id"] not in mediator.approvals.batches
        assert not await mediator.approvals.decide(batch["id"], True, by=MARK, version="read before it ended")
        assert provider.got == [] and company.queue.withholds == []
    finally:
        provider.close()
        runtime.close()


async def test_a_decider_s_approval_whose_answer_the_queue_never_takes_is_withheld_and_waits_again(
    tmp_path, monkeypatch
):
    """The final security review, L3: Mark approves Emily's plugin text as he read it; her instance worked out the
    answer and took the text as approved in her conversation before handing the answer over, and then the queue did not
    take it, three times (Valkey in trouble). The plugin never got the text, the record kept it approved, and sent again
    it no longer waited: it would have left masked by the rules of then, which nobody read. It is now taken as approved
    only once the answer is given; never given, it is recorded withheld and waits for the security team again."""
    from latet.bridge import api as bridge_api
    from latet.bridge.api import Bridge

    monkeypatch.setattr(bridge_api, "HOLD", 5.0)
    monkeypatch.setattr(bridge_api, "GIVE_PAUSE", 0.0)
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        bridge = Bridge(runtime.mediator, runtime.gateway.bridge_token)
        transport = httpx.ASGITransport(app=Starlette(routes=bridge.routes()))
        token = {"x-latet-token": runtime.gateway.bridge_token}
        body = {"session": "live-9", "items": [{"text": ASKED, "kind": "user_text"}]}
        approvals = runtime.mediator.approvals
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False) as client:
            pending = (await client.post(f"{ENGINE}/mask", json=body, headers=token)).json()["pending"]
            version = await _read(runtime, pending)
            given, tries = approvals.give, []

            def untaken(batch_id, answer):
                tries.append(batch_id)
                raise ConnectionError("the queue does not answer")

            monkeypatch.setattr(approvals, "give", untaken)
            assert await approvals.decide(pending, True, by=MARK, version=version)
            for _ in range(500):  # the bridge's answer worked out, and given up after its tries
                if not bridge._answering:
                    break
                await asyncio.sleep(0.01)
            monkeypatch.setattr(approvals, "give", given)
            assert tries == [pending] * bridge_api.GIVE_TRIES
            await _withheld(company, pending)
            feed = await approvals.watch()
            again = asyncio.ensure_future(client.post(f"{ENGINE}/mask", json=body, headers=token))
            waits = await _next_pending(feed, again)
            approvals.unwatch(feed)
            sent_again = (await asyncio.wait_for(again, 10)).json()
            if waits is not None:
                assert await approvals.decide(waits["id"], False, by=MARK)
        assert waits is not None and waits["id"] != pending and waits["decides"] == "security", sent_again
        assert sent_again.get("pending") == waits["id"] and EMAIL not in json.dumps(sent_again)
    finally:
        runtime.close()


async def test_a_decider_s_approval_whose_answer_the_queue_keeps_otherwise_is_withheld_and_waits_again(
    tmp_path, monkeypatch
):
    """The re-review of the fix rounds, R1: the queue takes the answer to Mark's approval without keeping it as given
    (a server's queue past the identity's bytes keeps an error in its place; no call waits for it any more): ``give``
    returned all the same, so the bridge took the approval, and sent again the text no longer waited. ``give`` now says
    whether it kept the answer; not kept, the approval is never taken, the batch is recorded withheld, and the text waits
    for the security team again."""
    from latet.bridge import api as bridge_api
    from latet.bridge.api import Bridge

    monkeypatch.setattr(bridge_api, "HOLD", 5.0)
    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        bridge = Bridge(runtime.mediator, runtime.gateway.bridge_token)
        transport = httpx.ASGITransport(app=Starlette(routes=bridge.routes()))
        token = {"x-latet-token": runtime.gateway.bridge_token}
        body = {"session": "live-9", "items": [{"text": ASKED, "kind": "user_text"}]}
        approvals = runtime.mediator.approvals
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False) as client:
            pending = (await client.post(f"{ENGINE}/mask", json=body, headers=token)).json()["pending"]
            version = await _read(runtime, pending)
            given, tries = approvals.give, []

            def kept_otherwise(batch_id, answer):
                tries.append(batch_id)
                return False  # taken, not kept as given: the plugin gets an error, or nothing

            monkeypatch.setattr(approvals, "give", kept_otherwise)
            assert await approvals.decide(pending, True, by=MARK, version=version)
            for _ in range(500):  # the bridge's answer worked out and given
                if not bridge._answering:
                    break
                await asyncio.sleep(0.01)
            monkeypatch.setattr(approvals, "give", given)
            assert tries == [pending]  # given once: a queue that answers is not asked again
            await _withheld(company, pending)
            feed = await approvals.watch()
            again = asyncio.ensure_future(client.post(f"{ENGINE}/mask", json=body, headers=token))
            waits = await _next_pending(feed, again)
            approvals.unwatch(feed)
            sent_again = (await asyncio.wait_for(again, 10)).json()
            if waits is not None:
                assert await approvals.decide(waits["id"], False, by=MARK)
        assert waits is not None and waits["id"] != pending and waits["decides"] == "security", sent_again
        assert sent_again.get("pending") == waits["id"] and EMAIL not in json.dumps(sent_again)
    finally:
        runtime.close()


async def test_a_decider_s_approval_heard_as_the_wait_is_given_up_is_recorded_withheld(tmp_path):
    """The re-review of the fix rounds, R3: Mark's approval is decided at the very instant the WebSocket relay gives up
    the wait (its provider's socket closed). Giving it up finds it decided and changes nothing, so the record kept it
    approved, though the turn never left. A decider's approval this process heard when its wait was given up is now
    recorded withheld."""
    from latet.gateway.approvals import Decision

    company = Company(Queue(), changes="self", texts="security")
    runtime = _runtime(tmp_path, company)
    try:
        mediator = runtime.mediator
        batch = _batch()
        batch.decides = "security"
        feed = await mediator.approvals.watch()
        waiting = asyncio.ensure_future(mediator.await_decision(batch, None))
        assert await _next_pending(feed, waiting) is not None  # it waits for the security team
        mediator.approvals.unwatch(feed)
        await asyncio.sleep(0.05)  # the wait under way
        version = batch_version(batch.items)
        assert mediator.approvals._settle(batch.id, Decision(True, by=MARK, version=version), "security", batch.agent)
        waiting.cancel()  # in the same instant: the relay ended before the wait heard it
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await _withheld(company, batch.id)
        for _ in range(500):  # how it ended is told in the work threads too
            if company.queue.ends:
                break
            await asyncio.sleep(0.01)
        assert company.queue.ends == [(batch.id, "batch", "approved", MARK)]  # as decided; the record says not sent
    finally:
        runtime.close()
