"""The Live Reviewer's queue: what waits, what you may change, and when a text is given up."""

import asyncio

import pytest

from latet.config import Config
from latet.gateway import approvals as approvals_module
from latet.gateway.approvals import Approvals, Batch, Change, Changed, MaskError, NotYours, batch_version
from latet.gateway.recorder import Broadcaster
from latet.state import IdentityRef, LocalApprovals
from latet.state.protocols import Timing

TEXT = "call Ada Byron on Monday about the Orion contract"


def _batch(**kwargs) -> Batch:
    start = TEXT.index("Monday")
    item = {
        "kind": "user_text",
        "path": None,
        "tool": None,
        "original": TEXT,
        "masked": TEXT.replace("Monday", "Friday"),
        "replacements": [{"start": start, "end": start + 6, "masked_start": start, "masked_end": start + 6}],
        "suggestions": [],
        "failed": None,
    }
    return Batch("cc:s", None, "claude-code", [item], **kwargs)


def _span(value: str) -> dict:
    start = TEXT.index(value)
    return {"item": 0, "start": start, "end": start + len(value), "category": "person"}


async def test_you_approve_with_masks_of_your_own():
    queue = Approvals()
    batch = await queue.open(_batch())
    assert [b["id"] for b in await queue.pending()] == [batch.id]
    assert await queue.wait(batch, 0.01) is None  # not decided yet: the plugin asks again
    assert await queue.decide(batch.id, True, [_span("Ada Byron"), _span("Ada Byron")])
    decision = await queue.wait(batch, 1)
    assert decision is not None and decision.approved and decision.state == "approved"
    assert [(m.value, m.category) for m in decision.masks] == [("Ada Byron", "person")]
    assert await queue.pending() == [] and not await queue.decide(batch.id, True)


@pytest.mark.parametrize(
    ("mask", "why"),
    [
        ({"item": 3, "start": 0, "end": 4, "category": "person"}, "no text 3"),
        ({"item": 0, "start": 5, "end": 500, "category": "person"}, "inside its text"),
        (_span("Orion") | {"category": "planet"}, "unknown category"),
        (_span("Monday"), "masked already"),
        ({"item": 0, "start": 4, "end": 9, "category": "person"}, "no space around"),
        ({"item": 0, "start": 0}, "a mask is"),
        (_span("Ada Byron") | {"start": TEXT.index("Ada") + 1}, "whole words"),
        ({"item": 0, "start": TEXT.index(" on "), "end": TEXT.index(" on ") + 1, "category": "person"}, "two"),
    ],
)
async def test_you_can_only_add_masking(mask, why):
    queue = Approvals()
    batch = await queue.open(_batch())
    with pytest.raises(MaskError, match=why):
        await queue.decide(batch.id, True, [mask])
    assert await queue.pending()  # still waiting


def _shown_again(original: str, replacements: list[dict]) -> dict:
    return {"kind": "user_text", "path": None, "tool": None, "original": original, "masked": original,
            "replacements": replacements, "suggestions": [], "failed": None}  # fmt: skip


async def test_a_value_you_masked_is_found_again_in_a_text_shown_anew_meanwhile():
    """The text was masked again while you looked (a name redacted before yours is longer now): the place you chose
    moved, and your value is masked where it is now, never what came to sit at the old place."""
    queue = Approvals()
    batch = await queue.open(_batch())
    mask = _span("Orion") | {"value": "Orion", "category": "org"}
    now = TEXT.replace("Ada Byron", "[REDACTED:PERSON]")
    assert await queue.update(batch.id, [_shown_again(now, [])])
    assert await queue.decide(batch.id, True, [mask])
    decision = await queue.wait(batch, 1)
    assert decision is not None and [(m.value, m.category) for m in decision.masks] == [("Orion", "org")]


async def test_a_value_you_masked_stays_yours_when_the_text_shown_anew_masks_a_part_of_it():
    """Shown anew, the text masks "Byron" alone: "Ada Byron", which you chose, is still masked whole (by its value, as
    every value you mask is), never dropped with "Ada" left in clear."""
    queue = Approvals()
    batch = await queue.open(_batch())
    now = "about " + TEXT
    start = now.index("Byron")
    covered = [{"start": start, "end": start + 5, "masked_start": start, "masked_end": start + 5}]
    assert await queue.update(batch.id, [_shown_again(now, covered)])
    assert await queue.decide(batch.id, True, [_span("Ada Byron") | {"value": "Ada Byron"}])
    decision = await queue.wait(batch, 1)
    assert decision is not None and [(m.value, m.category) for m in decision.masks] == [("Ada Byron", "person")]


async def test_a_value_you_masked_stays_yours_when_a_part_of_it_is_masked_meanwhile_in_its_place():
    queue = Approvals()
    batch = await queue.open(_batch())
    start = TEXT.index("Orion")
    covered = [{"start": start, "end": start + 5, "masked_start": start, "masked_end": start + 5}]
    assert await queue.update(batch.id, [_shown_again(TEXT, covered)])
    chosen = _span("Orion contract") | {"value": "Orion contract", "category": "org"}
    assert await queue.decide(batch.id, True, [chosen])
    decision = await queue.wait(batch, 1)
    assert decision is not None and [(m.value, m.category) for m in decision.masks] == [("Orion contract", "org")]


async def test_a_value_sent_with_a_mask_is_checked_as_any_value_you_mask():
    queue = Approvals()
    batch = await queue.open(_batch())
    with pytest.raises(MaskError, match="letters or digits"):
        await queue.decide(batch.id, True, [_span("Orion") | {"value": "--", "category": "org"}])
    with pytest.raises(MaskError, match="at most"):
        await queue.decide(batch.id, True, [_span("Orion") | {"value": "x" * 5000, "category": "org"}])


async def test_a_mask_of_underscores_alone_is_refused():
    # An underscore is a word character but no letter or digit: "__" would be its own fake, and the request stopped.
    text = "keep __ as it is"
    item = {"kind": "user_text", "path": None, "tool": None, "original": text, "masked": text, "replacements": []}
    queue = Approvals()
    batch = await queue.open(Batch("cc:s", None, "claude-code", [item | {"suggestions": [], "failed": None}]))
    with pytest.raises(MaskError, match="letters or digits"):
        await queue.decide(batch.id, True, [{"item": 0, "start": 5, "end": 7, "category": "confidential"}])


async def test_a_fake_in_use_is_not_masked_again():
    queue = Approvals()
    queue.is_surrogate = lambda value: value == "Orion"
    batch = await queue.open(_batch())
    with pytest.raises(MaskError, match="fake"):
        await queue.decide(batch.id, True, [_span("Orion")])


async def test_stopped_approved_all_and_watched():
    queue = Approvals()
    feed = await queue.watch()
    assert feed.get_nowait() == ("snapshot", {"batches": [], "changes": [], "viewers": 1})
    first, second = await queue.open(_batch()), await queue.open(_batch())
    assert await queue.decide(first.id, False)
    stopped = await queue.wait(first, 1)
    assert stopped is not None and not stopped.approved and "did not approve" in (stopped.reason or "")
    assert await queue.approve_all("p1", every=False) == 0  # only one project's
    assert await queue.approve_all(None, every=False) == 1  # those of no project
    assert await queue.approve_all() == 0
    approved = await queue.wait(second, 1)
    assert approved is not None and approved.approved
    events = [feed.get_nowait()[0] for _ in range(feed.qsize())]
    assert events == ["viewers", "pending", "pending", "settled", "settled"]
    queue.unwatch(feed)
    assert queue.viewers == 0


async def test_given_up_when_nobody_waits_or_time_is_up(monkeypatch):
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    monkeypatch.setattr(approvals_module, "ABANDON_AFTER", 0.05)
    queue = Approvals()
    polled = await queue.open(_batch(polled=True))
    held = await queue.open(_batch(polled=True))
    waiting = asyncio.ensure_future(queue.wait(held, 0.5))  # a poll held open: not given up
    expiring = await queue.open(_batch(timeout_s=10))
    expiring.created -= 11
    await asyncio.sleep(0.2)
    assert polled.future.result().state == "abandoned"
    assert expiring.future.result().state == "expired"
    assert not held.future.done()
    await queue.decide(held.id, True)
    assert (await waiting) is not None


async def test_a_round_of_the_reaper_that_fails_is_said_and_the_next_one_goes_on(monkeypatch, caplog):
    """A round that fails (a server's Valkey not answering) ended the reaper for good: what it would give up waited on.
    It is said in the log by its type alone, once a minute at most, and the next round tries again."""
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)

    class Failing(LocalApprovals):
        failed = 0

        def settle(self, batch_id, decision):
            if self.failed < 3:
                self.failed += 1
                raise ConnectionError("Valkey at 10.0.0.5:6379 did not answer")
            return super().settle(batch_id, decision)

    backend = Failing()
    queue = Approvals(backend, Broadcaster())
    expiring = await queue.open(_batch(timeout_s=10))
    expiring.created -= 11
    decision = await asyncio.wait_for(expiring.future, 5)
    assert decision.state == "expired" and backend.failed == 3
    [line] = [r.getMessage() for r in caplog.records if r.name == "latet.gateway.approvals"]
    assert "ConnectionError" in line and "10.0.0.5" not in line
    queue.close()


def test_a_change_that_stops_waiting_has_its_answer_already():
    """A command that waits for a change (`--wait`) asks whether it still waits, then how it ended: from the
    moment it no longer waits, the answer is there (a poll in between would read none, and give up)."""
    queue = Approvals()
    change = queue.open_change(Change(None, "base", "command", None, ["it lowers protection"], ["profile"]))
    seen: list[str | None] = []

    class Waiting(dict):  # what a poll reads, the moment the change stops waiting
        def pop(self, key, *default):
            seen.append(queue.settled.get(key))
            return super().pop(key, *default)

        def __delitem__(self, key):
            seen.append(queue.settled.get(key))
            super().__delitem__(key)

    queue.changes = Waiting(queue.changes)
    queue.settle_change(change.id, "approved")
    assert seen == ["approved"] and change.id not in queue.changes


async def test_a_secret_may_be_part_of_a_word():
    queue = Approvals()
    batch = await queue.open(_batch())
    start = TEXT.index("Orion") + 1
    assert await queue.decide(batch.id, True, [{"item": 0, "start": start, "end": start + 4, "category": "secret"}])


async def test_one_category_per_value_and_nothing_marked_not_sensitive():
    queue = Approvals()
    queue.is_allowed = lambda _batch, value: value == "Orion"
    batch = await queue.open(_batch())
    with pytest.raises(MaskError, match="two different things"):
        await queue.decide(batch.id, True, [_span("Ada"), _span("Ada") | {"category": "org"}])
    with pytest.raises(MaskError, match="Not sensitive"):
        await queue.decide(batch.id, True, [_span("Orion")])
    with pytest.raises(MaskError, match="at most"):
        await queue.decide(batch.id, True, [_span("Ada")] * 201)


MARK = IdentityRef("u_markellisa", "Mark Ellis", "person")


def _routed(queue: Approvals, decides: str = "security", names: tuple[str, ...] = ("Mark Ellis",)) -> None:
    """The queue of a space whose company decides elsewhere (what the runtime does with Latet Enterprise, Task 2)."""

    async def route_batch(batch):
        batch.decides, batch.deciders = decides, list(names)

    def route_change(change):
        change.decides, change.deciders = decides, list(names)

    queue.route_batch, queue.route_change = route_batch, route_change


async def test_a_text_someone_else_decides_on_you_may_stop_but_not_approve():
    queue = Approvals()
    _routed(queue)
    feed = queue.broadcaster.subscribe()
    batch = await queue.open(_batch())
    assert batch.decides == "security" and (await queue.pending())[0]["deciders"] == ["Mark Ellis"]
    with pytest.raises(NotYours, match="security team"):
        await queue.decide(batch.id, True)
    assert await queue.approve_all() == 0 and await queue.pending()  # approving everything leaves it alone
    assert await queue.decide(batch.id, False)
    decision = await queue.wait(batch, 1)
    assert decision is not None and decision.state == "rejected" and decision.by is None
    events = [feed.get_nowait() for _ in range(feed.qsize())]
    assert ("settled", {"id": batch.id, "state": "rejected", "by": None}) in events


async def test_the_decision_of_someone_else_names_who_made_it():
    queue = Approvals()
    _routed(queue)
    ended = []
    queue.on_batch_settled = lambda batch_id, decision, decides, agent: ended.append(
        (batch_id, decision.state, decision.by, decides, agent)
    )
    batch = await queue.open(_batch())
    assert await queue.decide(batch.id, True, [_span("Ada Byron")], by=MARK, version=batch_version(batch.items))
    decision = await queue.wait(batch, 1)
    assert decision is not None and decision.approved and decision.by == MARK
    assert ended == [(batch.id, "approved", MARK, "security", "claude-code")]
    other = await queue.open(_batch())
    await queue.decide(other.id, False, by=MARK)
    stopped = await queue.wait(other, 1)
    assert stopped is not None and "security team did not approve" in (stopped.reason or "")


async def test_a_text_the_security_team_lets_run_out_of_time_says_so(monkeypatch):
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    queue = Approvals()
    _routed(queue)
    ended = []
    queue.on_batch_settled = lambda batch_id, decision, decides, agent: ended.append((batch_id, decision.state, agent))
    batch = await queue.open(_batch(timeout_s=10))
    batch.created -= 11
    decision = await queue.wait(batch, 1)
    assert decision is not None and decision.state == "expired" and "security team" in (decision.reason or "")
    assert ended == [(batch.id, "expired", "claude-code")]
    assert Timing.of(batch).decides == "security" and Timing.of(batch).agent == "claude-code"


def test_a_change_is_routed_before_it_is_kept_and_ends_with_who_decided():
    queue = Approvals()
    _routed(queue, names=("Daniel Hayes", "Mark Ellis"))
    published = []
    queue.publish_change = lambda summary, event: published.append((event, summary))
    ended = []
    queue.on_change_settled = lambda change_id, change, state, by, stopped_by: ended.append((change_id, state, by))
    lowered = Change(Config(allow_values=["ACME"]), "base", "dashboard", None, ["values never to mask: 1 added"],
                     ["allow_values"], asked_by="u_emilycarte")  # fmt: skip
    change = queue.open_change(lowered)
    event, summary = published[0]
    assert event == "change" and summary["decides"] == "security" and summary["source"] == "dashboard"
    assert summary["deciders"] == ["Daniel Hayes", "Mark Ellis"] and summary["asked_by"] == "u_emilycarte"
    assert queue.changes[change.id].decides == "security"  # kept with it, as every instance reads it
    queue.settle_change(change.id, "approved", MARK)
    assert published[-1] == ("change_settled", {"id": change.id, "state": "approved", "by": "Mark Ellis"})
    assert queue.settled[change.id] == "approved" and ended == [(change.id, "approved", MARK)]


def test_a_newer_change_supersedes_the_one_waiting_and_an_equivalent_one_is_dropped():
    queue = Approvals()
    _routed(queue)
    ended, dropped = [], []
    queue.on_change_settled = lambda change_id, change, state, by, stopped_by: ended.append((change_id, change, state))
    queue.dropped_change = dropped.append
    first = queue.open_change(Change(Config(profile="max"), "base", "dashboard", None, ["it lowers protection"],
                                     ["profile"]))  # fmt: skip
    second = queue.open_change(Change(Config(allow_values=["ACME"]), "base", "dashboard", None,
                                      ["values never to mask: 1 added"], ["allow_values"]))  # fmt: skip
    assert [(change_id, change.id if change else None, state) for change_id, change, state in ended] == [
        (first.id, first.id, "superseded")
    ]
    again = Change(Config(allow_values=["ACME"]), "base", "dashboard", None, ["values never to mask: 1 added"],
                   ["allow_values"])  # fmt: skip
    kept = queue.open_change(again)
    assert kept.id == second.id and dropped == [again.id]  # recorded, and never waited: the record forgets it


async def test_what_cannot_be_routed_never_waits():
    """The company's record does not answer: nothing opens, so nobody, the person included, can approve it."""
    queue = Approvals()

    async def broken_batch(batch):
        raise ConnectionError("the company's record does not answer")

    def broken_change(change):
        raise ConnectionError("the company's record does not answer")

    queue.route_batch, queue.route_change = broken_batch, broken_change
    with pytest.raises(ConnectionError):
        await queue.open(_batch())
    with pytest.raises(ConnectionError):
        queue.open_change(Change(Config(), "base", "command", None, ["it lowers protection"], ["profile"]))
    assert await queue.pending() == [] and queue.pending_changes() == []


async def test_a_decider_approves_a_text_only_as_they_read_it():
    """The text masked again after the decider read it (its previews made anew while they looked: a change of the rules
    or of the vault, which may mask less): their approval of the version they read decides nothing (``Changed``), and
    the text still waits; of the version as it is now, it is approved. Stopping it names no version."""
    queue = Approvals()
    _routed(queue)
    batch = await queue.open(_batch())
    read = batch_version((await queue.backend.fetch(batch.id)).items)
    assert await queue.update(batch.id, [_shown_again(TEXT, [])])  # Monday in clear now
    with pytest.raises(Changed):
        await queue.decide(batch.id, True, [_span("Ada Byron")], by=MARK, version=read)
    assert [b["id"] for b in await queue.pending()] == [batch.id]  # nothing decided
    now = batch_version((await queue.backend.fetch(batch.id)).items)
    assert now != read
    assert await queue.decide(batch.id, True, [_span("Ada Byron")], by=MARK, version=now)
    other = await queue.open(_batch())
    assert await queue.decide(other.id, False, by=MARK, version="anything")  # stopping it is never refused for it


async def test_a_decider_s_approval_without_the_version_they_read_decides_nothing():
    """The whole-branch security review's L4: an approval by someone who decides for the company names the version of
    the text they read. Without one it is refused, nothing decided: where the text is sent, an approval with no
    version would be taken for the person's own and never compared with what was read. The deciders' route asks for it
    first (400); this is the queue's own guard. Stopping it needs none."""
    queue = Approvals()
    _routed(queue)
    batch = await queue.open(_batch())
    for missing in (None, ""):
        with pytest.raises(Changed, match="version"):
            await queue.decide(batch.id, True, [_span("Ada Byron")], by=MARK, version=missing)
    assert [b["id"] for b in await queue.pending()] == [batch.id]  # nothing decided
    read = batch_version((await queue.backend.fetch(batch.id)).items)
    assert await queue.decide(batch.id, True, [_span("Ada Byron")], by=MARK, version=read)
    other = await queue.open(_batch())
    assert await queue.decide(other.id, False, by=MARK)


def test_a_text_s_version_says_how_it_would_leave_never_its_values():
    """What it would leave as (its masked text, and where each value sits), never the values in clear: a text whose
    originals differ alone has the same version."""
    item = _batch().items[0]
    version = batch_version([item])
    assert batch_version([item | {"original": "call Grace Hopper on Monday about the Vega contract"}]) == version
    assert batch_version([item | {"masked": TEXT}]) != version
    assert batch_version([item | {"replacements": []}]) != version
    assert batch_version([item, item]) != version
    assert "Ada" not in version and len(version) >= 16


async def test_a_decider_masks_only_what_the_text_holds():
    queue = Approvals()
    _routed(queue)
    batch = await queue.open(_batch())
    elsewhere = {"item": 0, "start": 0, "end": 4, "category": "person", "value": "Northwind Traders"}
    with pytest.raises(MaskError, match="not in the text"):
        await queue.decide(batch.id, True, [elsewhere], by=MARK, version=batch_version(batch.items))
    assert await queue.decide(batch.id, True, [_span("Ada Byron")], by=MARK, version=batch_version(batch.items))
