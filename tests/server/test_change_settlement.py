"""A settings proposal and its winning decision are atomic across instances."""

import asyncio
import json
import threading

import pytest

from latet.api import rest as rest_module
from latet.config import Config, TermConfig
from latet.gateway.approvals import Change
from latet.runtime import digest
from latet.server.app import create_server_app
from latet.server.queue import PgChanges, PgSettled

from ..conftest import ROOT_KEY
from .conftest import identity_in
from .support import listening, until
from .test_app import API, _asgi

pytestmark = pytest.mark.server


def _change(config, base):
    return Change(config, base, "command", None, ["a term removed"], ["terms"])


@pytest.fixture
async def pair(instances):
    a, b = instances
    person, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        runtimes = await asyncio.wait_for(asyncio.gather(a.spaces.runtime(person), b.spaces.runtime(person)), 30)
        assert digest(runtimes[0].config) == digest(runtimes[1].config)
        yield (a, b), runtimes, person


async def _decision(server, person, change_id, approve=True):
    sid = server.sign_ins.open(person.id)
    sent = []
    status, _ = await asyncio.wait_for(
        _asgi(
            create_server_app(server),
            f"{API}/changes/{change_id}",
            method="POST",
            headers=[(b"cookie", f"latet_session={sid}".encode()), (b"x-latet-ui", b"1")],
            body=json.dumps({"action": "approve" if approve else "reject"}).encode(),
            sent=sent,
        ),
        20,
    )
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, json.loads(body)


@pytest.mark.parametrize("other", ["reject", "supersede"])
async def test_a_lost_approval_never_applies_settings(pair, monkeypatch, other):
    """Break: ignore the atomic settlement result and apply the now rejected/superseded proposal."""
    (a, _b), (here, there), person = pair
    approvals = here.mediator.approvals
    before = here.config
    proposal = before.model_copy(update={"terms": [TermConfig(value="Orion")]})
    change = approvals.open_change(_change(proposal, digest(before)))
    entered, release = threading.Event(), threading.Event()
    settle = approvals.settle_change
    applied, apply = [], here.apply_config

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10), "the competing decision never released the settlement"
        return settle(*args, **kwargs)

    def applying(*args, **kwargs):
        if kwargs.get("save"):
            applied.append(args[0])
        return apply(*args, **kwargs)

    monkeypatch.setattr(approvals, "settle_change", held)
    monkeypatch.setattr(here, "apply_config", applying)
    request = asyncio.create_task(_decision(a, person, change.id))
    replacement = None
    try:
        await until(entered.is_set)
        if other == "reject":
            assert await asyncio.to_thread(there.decide_change, change.id, False) == "rejected"
        else:
            replacement = await asyncio.to_thread(
                there.mediator.approvals.open_change, _change(Config(), "another base")
            )
    finally:
        release.set()
        answer = await asyncio.wait_for(request, 15)
    assert answer == (409, {"error": "it is not waiting any more"})
    assert not applied and digest(here.config) == digest(before)
    assert approvals.settled[change.id] == ("rejected" if other == "reject" else "superseded")
    if replacement is not None:
        assert list(approvals.changes) == [replacement.id]


@pytest.mark.parametrize("other", ["supersede", "approve", "reject"])
async def test_a_lost_rejection_does_not_save_settings_over_a_new_proposal(pair, monkeypatch, other):
    """Break: save after losing settlement, or report success for the opposite decision."""
    (a, _b), (here, there), person = pair
    approvals = here.mediator.approvals
    change = approvals.open_change(_change(here.config, digest(here.config)))
    entered, release = threading.Event(), threading.Event()
    settle, save = approvals.settle_change, here.settings.save
    saved = []

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10), "the competing open never released the rejection"
        return settle(*args, **kwargs)

    def saving(config):
        saved.append(config)
        return save(config)

    monkeypatch.setattr(approvals, "settle_change", held)
    monkeypatch.setattr(here.settings, "save", saving)
    request = asyncio.create_task(_decision(a, person, change.id, approve=False))
    newer = None
    try:
        await until(entered.is_set)
        if other == "supersede":
            newer = await asyncio.to_thread(there.mediator.approvals.open_change, _change(Config(), "another base"))
        else:
            assert await asyncio.to_thread(there.decide_change, change.id, other == "approve") == (
                "approved" if other == "approve" else "rejected"
            )
    finally:
        release.set()
        answer = await asyncio.wait_for(request, 15)
    assert answer == (
        (200, {"ok": True, "state": "rejected"})
        if other == "reject"
        else (409, {"error": "it is not waiting any more"})
    )
    assert saved == []
    assert list(approvals.changes) == ([newer.id] if newer is not None else [])
    assert (
        approvals.settled[change.id] == {"supersede": "superseded", "approve": "approved", "reject": "rejected"}[other]
    )
    assert digest(here.config) == digest(there.config) == digest(change.config)


async def test_only_the_winning_atomic_settlement_is_announced(pair, monkeypatch):
    """Break: return/publish the change after end reported that another instance already ended it."""
    _, (here, there), _ = pair
    first, second = here.mediator.approvals, there.mediator.approvals
    change = first.open_change(_change(here.config, digest(here.config)))
    feed = there.backends.approvals_feed
    events = feed.subscribe()
    entered, release = threading.Event(), threading.Event()
    end = first.backend.end

    def held(*args):
        entered.set()
        assert release.wait(10), "the other instance did not settle the change"
        return end(*args)

    monkeypatch.setattr(first.backend, "end", held)
    request = asyncio.create_task(asyncio.to_thread(first.settle_change, change.id, "approved"))
    try:
        try:
            await until(entered.is_set)
            assert await asyncio.to_thread(second.settle_change, change.id, "rejected") is not None
        finally:
            release.set()
            result = await asyncio.wait_for(request, 15)
        second.publish_change({}, "task23a_settlement_barrier")
        told = []
        while True:
            event, payload = await asyncio.wait_for(events.get(), 5)
            if event == "task23a_settlement_barrier":
                break
            if event == "change_settled":
                told.append(payload)
        assert result is None and first.settled[change.id] == "rejected"
        assert told == [{"id": change.id, "state": "rejected"}]
    finally:
        feed.unsubscribe(events)


async def test_a_stale_approval_is_settled_without_applying_it(pair):
    """Compatibility: proposals over another base retain the existing stale refusal."""
    (a, _b), (here, _there), person = pair
    before = here.config
    change = here.mediator.approvals.open_change(_change(Config(terms=[TermConfig(value="Orion")]), "old base"))
    status, body = await _decision(a, person, change.id)
    assert status == 409 and body == {"error": "your settings changed since: it was dropped, ask for it again"}
    assert here.mediator.approvals.settled[change.id] == "stale" and digest(here.config) == digest(before)


async def test_a_winning_rejection_is_persisted_before_the_dashboard_answers_success(pair):
    """Break: describe a successfully persisted undo as a conflict to the dashboard."""
    (a, _b), (here, _there), person = pair
    before = here.config
    proposal = before.model_copy(update={"terms": [TermConfig(value="Orion")]})
    change = here.mediator.approvals.open_change(_change(proposal, digest(before)))
    assert await _decision(a, person, change.id, approve=False) == (200, {"ok": True, "state": "rejected"})
    assert here.mediator.approvals.settled[change.id] == "rejected"
    assert list(here.mediator.approvals.changes) == []
    assert digest(here.config) == digest(before) and digest(here.settings.load()) == digest(before)


@pytest.mark.parametrize("approve", [True, False], ids=["approve", "reject"])
async def test_a_later_decision_reads_no_completed_state_and_performs_no_save(pair, monkeypatch, approve):
    """Break: turn a later initial missing read into success, a completed lookup or another write."""
    (a, _b), (here, _there), person = pair
    approvals = here.mediator.approvals
    before = here.config
    change = approvals.open_change(_change(before, digest(before)))
    assert await asyncio.to_thread(here.decide_change, change.id, False) == "rejected"
    looked_up, saved, applied = [], [], []
    settled_get = approvals.settled.get

    def completed(*args):
        looked_up.append(args)
        return settled_get(*args)

    monkeypatch.setattr(approvals.settled, "get", completed)
    monkeypatch.setattr(here.settings, "save", lambda *args: saved.append(args))
    monkeypatch.setattr(here, "apply_config", lambda *args, **kwargs: applied.append((args, kwargs)))
    assert await _decision(a, person, change.id, approve=approve) == (409, {"error": "it is not waiting any more"})
    assert looked_up == saved == applied == []
    assert digest(here.config) == digest(before) and digest(here.settings.load()) == digest(before)


async def test_a_server_change_decision_uses_the_work_pool(pair, monkeypatch):
    """Break: keep _decide_change in asyncio.to_thread and contend with space construction."""
    (a, _b), (here, _there), person = pair
    change = here.mediator.approvals.open_change(_change(here.config, digest(here.config)))

    async def forbidden(*args, **kwargs):
        raise AssertionError("server change decisions must use the work pool")

    with monkeypatch.context() as patch:
        patch.setattr(rest_module.asyncio, "to_thread", forbidden)
        assert await _decision(a, person, change.id) == (200, {"ok": True, "state": "approved"})
    assert here.mediator.approvals.settled[change.id] == "approved"


async def test_two_approvals_apply_once_and_both_answer_approved(pair, monkeypatch):
    """Break: a losing approval still applies, or returns conflict after the other approval won."""
    (a, b), runtimes, person = pair
    proposal = runtimes[0].config.model_copy(update={"terms": [TermConfig(value="Orion")]})
    change = runtimes[0].mediator.approvals.open_change(_change(proposal, digest(runtimes[0].config)))
    ready = threading.Barrier(2, timeout=10)
    applied = []

    for runtime in runtimes:
        approvals = runtime.mediator.approvals
        settle, apply = approvals.settle_change, runtime.apply_config

        def held(*args, _settle=settle, **kwargs):
            ready.wait()
            return _settle(*args, **kwargs)

        def applying(*args, _apply=apply, **kwargs):
            if kwargs.get("save"):
                applied.append(args[0])
            return _apply(*args, **kwargs)

        monkeypatch.setattr(approvals, "settle_change", held)
        monkeypatch.setattr(runtime, "apply_config", applying)
    answers = await asyncio.wait_for(
        asyncio.gather(_decision(a, person, change.id), _decision(b, person, change.id)), 20
    )
    assert answers == [(200, {"ok": True, "state": "approved"})] * 2
    assert len(applied) == 1 and digest(applied[0]) == digest(proposal)
    assert runtimes[0].mediator.approvals.settled[change.id] == "approved"
    assert digest(runtimes[0].settings.reviewed()) == digest(proposal)


async def test_equivalent_proposals_open_once_and_publish_once(pair):
    """Break: write two proposals, or publish again when the existing equivalent proposal was kept."""
    _, (here, there), _ = pair
    first, second = here.mediator.approvals, there.mediator.approvals
    proposal = here.config.model_copy(update={"terms": [TermConfig(value="Orion")]})
    changes = [_change(proposal, digest(here.config)), _change(proposal.model_copy(deep=True), digest(here.config))]
    # Source/reason/agent/id/creation time are deliberately different: equivalence is config plus base alone.
    changes[1].source = "file"
    changes[1].agent = "claude-code"
    feed = there.backends.approvals_feed
    events = feed.subscribe()
    start = threading.Barrier(2, timeout=10)

    def opened(approvals, change):
        start.wait()
        return approvals.open_change(change)

    try:
        opened_changes = await asyncio.wait_for(
            asyncio.gather(asyncio.to_thread(opened, first, changes[0]), asyncio.to_thread(opened, second, changes[1])),
            20,
        )
        # A sentinel on the same feed is an ordering barrier; no timing-based absence assertion.
        second.publish_change({"done": True}, "task23a_barrier")
        told = []
        while True:
            event, payload = await asyncio.wait_for(events.get(), 5)
            if event == "task23a_barrier":
                break
            told.append((event, payload))
        assert opened_changes[0].id == opened_changes[1].id
        assert list(first.changes) == list(second.changes) == [opened_changes[0].id]
        assert [(event, payload["id"]) for event, payload in told] == [("change", opened_changes[0].id)]
        assert dict(first.settled) == {}
    finally:
        feed.unsubscribe(events)


async def test_another_base_supersedes_and_tells_the_viewer_once(pair):
    """Break: compare config alone, or supersede inside storage without announcing the settled id."""
    _, (here, there), _ = pair
    first, second = here.mediator.approvals, there.mediator.approvals
    older = first.open_change(_change(here.config, "old base"))
    feed = there.backends.approvals_feed
    events = feed.subscribe()
    try:
        # Flush the earlier opening before recording this operation's events.
        first.publish_change({}, "task23a_before")
        while (await asyncio.wait_for(events.get(), 5))[0] != "task23a_before":
            pass
        newer = await asyncio.to_thread(second.open_change, _change(here.config, "new base"))
        second.publish_change({}, "task23a_after")
        told = []
        while True:
            event, payload = await asyncio.wait_for(events.get(), 5)
            if event == "task23a_after":
                break
            told.append((event, payload))
        assert newer.id != older.id and list(first.changes) == [newer.id]
        assert first.settled[older.id] == "superseded"
        assert told == [
            ("change_settled", {"id": older.id, "state": "superseded"}),
            ("change", newer.summary(full=True)),
        ]
    finally:
        feed.unsubscribe(events)


def test_an_unreadable_waiting_row_is_superseded_by_a_readable_proposal(db):
    """Break: retain an unreadable row as equivalent, or leave it waiting outside the readable list."""
    identity = identity_in(db)
    wrong = PgChanges(db, identity, b"\x19" * 32)
    unreadable = _change(Config(), "base")
    wrong[unreadable.id] = unreadable
    here = PgChanges(db, identity, ROOT_KEY)
    proposal = _change(Config(), "base")
    opened, superseded = here.open(proposal)
    assert opened.id == proposal.id and superseded == [unreadable.id]
    with db.connection() as conn:
        rows = conn.execute("SELECT id FROM changes WHERE identity_id = %s AND state IS NULL", (identity,)).fetchall()
    assert rows == [(proposal.id,)]
    assert PgSettled(db, identity)[unreadable.id] == "superseded"
