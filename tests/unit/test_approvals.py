"""The Live Reviewer's queue: what waits, what you may change, and when a text is given up."""

import asyncio

import pytest

from latet.gateway import approvals as approvals_module
from latet.gateway.approvals import Approvals, Batch, Change, MaskError
from latet.gateway.recorder import Broadcaster
from latet.state import LocalApprovals

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
