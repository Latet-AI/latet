"""The plugin's answers: worked out where its call waits and handed over, once, by whichever process the plugin asks;
what is left in the queue's backend after each way a call ends, and what a failure after your decision shows."""

import asyncio
import gc
import logging
import threading
import weakref

import httpx
import psycopg
import pytest
import redis
from starlette.applications import Starlette

from latet.bridge.api import Bridge
from latet.engine import wellformed
from latet.engine.types import EntityClass
from latet.engine.vault import MaskingError
from latet.gateway import approvals as approvals_module
from latet.gateway.approvals import EXPIRED, REJECTED
from latet.vision.masking import ImageMasking

from ...fake_ocr import FakeOcr
from .conftest import EMAIL, PROMPT, _until


def _client(bridge):
    transport = httpx.ASGITransport(app=Starlette(routes=bridge.routes()))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False)


class Plugin:
    """Claude Code's plugin, calling the routes of a bridge. A bridge is one process that serves you: a test makes two
    to have a call and its polls reach different ones."""

    def __init__(self, rt):
        self.bridge = Bridge(rt.mediator, rt.gateway.bridge_token)
        self.client = _client(self.bridge)
        self.token = {"x-latet-token": rt.gateway.bridge_token}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def mask(self, items=None):
        """A call with new texts, which wait for you: the answer says which batch to ask about."""
        body = {"session": "live-1", "items": items or [{"text": PROMPT, "kind": "user_text"}]}
        return (await self.client.post("/_latet/engine/v1/mask", json=body, headers=self.token)).json()["pending"]

    async def wait(self, pending):
        """One poll for the answer, held as long as HOLD."""
        return await self.client.post("/_latet/engine/v1/approvals/wait", json={"id": pending}, headers=self.token)


@pytest.fixture
async def plugin(live, monkeypatch):
    """A plugin asking one bridge; a poll is held 5 s (20 s in use), so that a test that fails does not wait long."""
    monkeypatch.setattr("latet.bridge.api.HOLD", 5.0)
    async with Plugin(live) as asking:
        yield asking


def _left(rt):
    """What the queue's backend keeps of the plugin's calls: their batches (with the original texts and the images)
    and the slots of their answers."""
    backend = rt.mediator.approvals.backend
    return backend.batches, backend._answers


async def _ended(*tasks):
    """Wait for the tasks to end; their bridge has dropped them from its set by then (it is the first to be told)."""
    await asyncio.wait_for(asyncio.gather(*tasks), 5)


async def test_the_plugins_poll_is_answered_by_another_process_than_the_one_its_call_waits_in(live, monkeypatch):
    monkeypatch.setattr("latet.bridge.api.HOLD", 0.05)  # a poll is held this long (20 s in use)
    async with Plugin(live) as opened, Plugin(live) as other:
        pending = await opened.mask()
        waiting = await other.wait(pending)
        assert waiting.json() == {"pending": pending, "viewers": 0}  # not decided: the plugin asks again
        assert await live.mediator.approvals.decide(pending, True)
        monkeypatch.setattr("latet.bridge.api.HOLD", 10.0)  # the answer comes while this poll is held
        answer = (await other.wait(pending)).json()
    [item] = answer["items"]
    assert EMAIL not in item["text"] and "Orion" in item["text"]


async def test_a_call_nobody_asks_about_is_given_up_and_forgotten(live, plugin, monkeypatch):
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    monkeypatch.setattr(approvals_module, "ABANDON_AFTER", 0.05)  # the gateway gives the batch up
    monkeypatch.setattr(live.mediator.approvals.backend, "abandon_after", 0.05)  # its backend forgets the answer
    pending = await plugin.mask()
    [task] = plugin.bridge._answering
    await _ended(task)  # Claude Code stopped asking: the queue gave the call up, and its task gave the answer and ended
    assert await live.mediator.approvals.pending() == [] and plugin.bridge._answering == set()
    assert (await plugin.wait(pending)).status_code == 404
    assert _left(live) == ({}, {})  # no batch, no original text, no image, no answer


@pytest.mark.parametrize(("approve", "blocked"), [(True, None), (False, REJECTED)], ids=["approved", "rejected"])
async def test_an_answer_the_plugin_reads_is_handed_over_once_and_leaves_nothing_in_the_backend(
    live, plugin, approve, blocked
):
    pending = await plugin.mask()
    [task] = plugin.bridge._answering  # kept until it gave: a task nobody holds can be collected mid-flight
    assert await live.mediator.approvals.decide(pending, approve)
    answer = await plugin.wait(pending)
    [item] = answer.json()["items"]
    assert answer.status_code == 200 and item.get("blocked") == blocked  # what you stopped is a notice, not the text
    assert ("Orion" in item["text"]) == approve and EMAIL not in item["text"]
    await _ended(task)
    assert plugin.bridge._answering == set() and _left(live) == ({}, {})
    assert (await plugin.wait(pending)).status_code == 404  # handed over once


async def test_a_value_you_let_through_while_a_text_waits_leaves_in_clear_once_you_approve(live, plugin):
    """In Approvals you click a masked value, choose Not sensitive: always allow, then Approve: the text leaves masked
    with the rules of now, the value in clear, not as it was masked when it came."""
    pending = await plugin.mask()

    def allowed(config):
        config = config.model_copy(deep=True)
        config.allow_values.append(EMAIL)
        return config

    live.update_config(allowed)
    assert await live.mediator.approvals.decide(pending, True)
    [item] = (await plugin.wait(pending)).json()["items"]
    assert EMAIL in item["text"] and "Orion" in item["text"]


async def _first_preview(live, batch_id, condition):
    """The first text of ``batch_id`` as the dashboards show it, once ``condition`` holds of it (5 s at most)."""
    for _ in range(250):
        batches = {b["id"]: b for b in await live.mediator.approvals.pending()}
        masked = batches[batch_id]["items"][0]["masked"]
        if condition(masked):
            return masked
        await asyncio.sleep(0.02)
    raise AssertionError(f"the preview did not change: {masked!r}")


async def test_a_value_you_let_through_shows_in_clear_in_the_texts_still_waiting(live, plugin):
    """Not sensitive: always allow, on one text: every text still waiting shows the value in clear at once, as it will
    leave, and the dashboards are told ("updated")."""
    pending = await plugin.mask()
    assert EMAIL not in (await live.mediator.approvals.pending())[0]["items"][0]["masked"]
    events = await live.mediator.approvals.watch()

    def allowed(config):
        config = config.model_copy(deep=True)
        config.allow_values.append(EMAIL)
        return config

    live.update_config(allowed)
    await _first_preview(live, pending, lambda masked: EMAIL in masked)
    told = [events.get_nowait() for _ in range(events.qsize())]
    assert any(event == "updated" and summary["id"] == pending for event, summary in told), told
    live.mediator.approvals.unwatch(events)


async def test_a_value_you_mask_in_one_text_is_masked_in_the_texts_still_waiting(live, plugin):
    """A value masked by hand in one text, approved: the other texts still waiting show it masked, as they will
    leave."""
    first = await plugin.mask()
    later = {"session": "live-1", "items": [{"text": "the Orion contract is late", "kind": "user_text"}]}
    second = (await plugin.client.post("/_latet/engine/v1/mask", json=later, headers=plugin.token)).json()["pending"]
    assert "Orion" in (await _first_preview(live, second, lambda masked: True))
    start = PROMPT.index("Orion")
    mask = {"item": 0, "start": start, "end": start + len("Orion"), "category": "org"}
    assert await live.mediator.approvals.decide(first, True, [mask])
    await plugin.wait(first)  # the plugin's answer: the value masked by hand is kept in the vault
    await _first_preview(live, second, lambda masked: "Orion" not in masked)


def _keep(live, value):
    """``value`` kept in the vault, as a value masked by hand or found by a detector in another request is."""
    live.mediator.engine.vault.intern("ORG", EntityClass.CUSTOM, value, rule="human:org")


async def test_a_value_kept_that_no_text_waiting_holds_does_not_mask_them_again(live, plugin, monkeypatch):
    """Every request keeps values in the vault: the texts waiting are masked again only where one of them is in clear
    in their previews, never each time (a name model over every text waiting, for each value kept)."""
    pending = await plugin.mask()
    mediator, masked = live.mediator, []
    real = mediator.mask_value

    def counting(text, *args, **kwargs):
        masked.append(text)
        return real(text, *args, **kwargs)

    monkeypatch.setattr(mediator, "mask_value", counting)
    await mediator.run(_keep, live, "Vega Systems")
    await _until(lambda: mediator._held_live[pending].made_at == mediator._live_stamp())
    assert PROMPT not in masked
    await mediator.run(_keep, live, "Orion")  # one it holds in clear: masked in it
    await _first_preview(live, pending, lambda text: "Orion" not in text)


async def test_a_value_kept_while_a_text_was_being_opened_is_masked_in_it(live, plugin, monkeypatch):
    """The vault changed after the text's preview was made and before it was held here: it is shown masked again."""
    mediator = live.mediator
    real = mediator.hold_live

    def late(*args, **kwargs):
        _keep(live, "Orion")  # nothing held yet: no refresh asked
        real(*args, **kwargs)

    monkeypatch.setattr(mediator, "hold_live", late)
    pending = await plugin.mask()
    await _first_preview(live, pending, lambda text: "Orion" not in text)


async def test_a_preview_the_queue_did_not_keep_is_made_again_at_the_next_change(live, plugin, monkeypatch):
    """The queue may refuse a preview (a server's is full): the next change makes it again, never taking the one it
    refused for shown."""
    pending = await plugin.mask()
    approvals, refused = live.mediator.approvals, []
    real = approvals.update

    async def once_refused(batch_id, items):
        if not refused:
            refused.append(batch_id)
            return False
        return await real(batch_id, items)

    monkeypatch.setattr(approvals, "update", once_refused)

    def allowed(config):
        config = config.model_copy(deep=True)
        config.allow_values.append(EMAIL)
        return config

    live.update_config(allowed)
    await _until(lambda: refused)
    await asyncio.sleep(0.5)
    live.mediator.live_changed()
    await _first_preview(live, pending, lambda text: EMAIL in text)


async def test_the_text_latet_sent_after_your_approval_does_not_wait_again(live, plugin):
    """Claude Code keeps the prompt it sent, already masked, and the plugin hands that row to Latet once more (its
    session.append): what Latet sent once you approved it is approved too, never a second approval whose original holds
    the stand-ins of the first."""
    pending = await plugin.mask()
    assert await live.mediator.approvals.decide(pending, True)
    [item] = (await plugin.wait(pending)).json()["items"]
    sent = item["text"]
    assert EMAIL not in sent
    body = {"session": "live-1", "items": [{"text": sent, "kind": "user_text"}]}
    again = (await plugin.client.post("/_latet/engine/v1/mask", json=body, headers=plugin.token)).json()
    assert not again.get("pending"), again
    assert await live.mediator.approvals.pending() == []
    assert again["items"][0]["text"] == sent


@pytest.mark.parametrize("live", [{"timeout_s": 10}], indirect=True, ids=["ten-second-limit"])
async def test_a_call_whose_time_is_up_is_answered_withheld_and_leaves_nothing_in_the_backend(
    live, plugin, monkeypatch
):
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    pending = await plugin.mask()
    [task] = plugin.bridge._answering
    live.mediator.approvals.batches[pending].created -= 11  # it waited longer than live_review.timeout_s
    answer = await plugin.wait(pending)  # the poll is held open while the time runs out
    [item] = answer.json()["items"]
    assert answer.status_code == 200 and item["blocked"] == EXPIRED and "Orion" not in item["text"]
    await _ended(task)
    assert plugin.bridge._answering == set() and _left(live) == ({}, {})


async def test_a_call_nobody_asks_about_keeps_neither_its_texts_nor_its_images(live, plugin, monkeypatch):
    monkeypatch.setattr(approvals_module, "_REAP_EVERY", 0.01)
    monkeypatch.setattr(approvals_module, "ABANDON_AFTER", 0.05)  # the gateway gives the batch up
    monkeypatch.setattr(live.mediator.approvals.backend, "abandon_after", 0.05)  # its backend forgets the answer
    reader = FakeOcr()
    live.mediator.vision = ImageMasking(reader, live.store)
    picture = reader.screenshot([f"Email: {EMAIL}", "Customer since 2019, VIP"])
    image = {"image": {"data": picture, "media_type": "image/png"}, "kind": "tool_result"}
    pending = await plugin.mask([{"text": PROMPT, "kind": "user_text"}, image])
    [task] = plugin.bridge._answering
    [batch] = live.mediator.approvals.batches.values()
    assert len(batch.items) == 2 and len(batch.images) == 1  # two texts as they came, one of them read in an image
    holding = [weakref.ref(batch.future), *(weakref.ref(work) for work in batch.images.values())]  # batch, image
    del batch
    await _ended(task)  # Claude Code stopped asking: the queue gave the call up, and its task gave the answer and ended
    del task
    assert (await plugin.wait(pending)).status_code == 404
    gc.collect()
    assert _left(live) == ({}, {}) and [kept() for kept in holding] == [None, None]  # nothing holds them any more


def _refuses_to_mask(rt, monkeypatch):
    def refuse(*args, **kwargs):
        raise MaskingError("a value masked in the Live Reviewer would have left in clear")

    monkeypatch.setattr(rt.mediator, "apply_live", refuse)


def _detector_fails_on_a_value(rt, monkeypatch):
    """A mandatory detector that fails raises ``MaskingError`` from what it failed on, and that can quote the text."""

    def fail(*args, **kwargs):
        try:
            int(EMAIL)  # ValueError: invalid literal for int() with base 10: 'emily.carter@acme.com'
        except ValueError as exc:
            raise MaskingError("detector pii failed") from exc

    monkeypatch.setattr(rt.mediator, "apply_live", fail)


def _cannot_put_the_answer_together(rt, monkeypatch):
    """Any other failure, as a library's: its message can quote what it was working on."""

    def fail(out):
        raise ValueError(f"the image read as {EMAIL!r} could not be encoded")

    monkeypatch.setattr("latet.bridge.api._answered", fail)


@pytest.mark.parametrize(
    ("breaks", "logged", "told"),
    [
        pytest.param(
            _refuses_to_mask,
            "bridge masking failed: a value masked in the Live Reviewer would have left in clear",
            "masking failed: a value masked in the Live Reviewer would have left in clear",
            id="masking",
        ),
        pytest.param(
            _detector_fails_on_a_value,
            "bridge masking failed: detector pii failed",
            "masking failed: detector pii failed",
            id="masking-of-a-value",
        ),
        pytest.param(
            _cannot_put_the_answer_together,
            "bridge masking failed: ValueError (batch {batch})",
            "masking failed",
            id="answer",
        ),
    ],
)
async def test_a_failure_after_you_decided_answers_the_plugin_with_an_error_at_once_and_hands_over_nothing(
    live, plugin, breaks, logged, told, monkeypatch, caplog
):
    pending = await plugin.mask()
    [task] = plugin.bridge._answering
    breaks(live, monkeypatch)
    assert await live.mediator.approvals.decide(pending, True)
    failed = await plugin.wait(pending)  # an error now, not a poll that waits for an answer that cannot come
    # No text, masked or not: a masking error says what failed by its own words (a vault at its limit says so), which
    # name no value; any other, that masking failed.
    assert (failed.status_code, failed.json()) == (500, {"error": told})
    await _ended(task)
    [record] = [r for r in caplog.records if r.name == "latet.bridge.api" and r.levelno >= logging.ERROR]
    # A masking error is logged by its own words, which name no value; any other by its type and its batch. Never a
    # traceback: it ends with the error's message, and that can quote a text.
    assert record.getMessage() == logged.format(batch=pending) and record.exc_info is None
    assert EMAIL not in caplog.text
    assert plugin.bridge._answering == set() and _left(live) == ({}, {})
    assert (await plugin.wait(pending)).status_code == 404  # told once


async def test_a_task_that_cannot_give_its_answer_is_logged_by_its_type_and_its_batch_only(
    live, plugin, monkeypatch, caplog
):
    def refuse(batch_id, answer):
        raise ConnectionError(f"the store refused the answer for {EMAIL}")  # a server's backend failing, quoting a text

    monkeypatch.setattr(live.mediator.approvals.backend, "give", refuse)
    pending = await plugin.mask()
    [task] = plugin.bridge._answering
    assert await live.mediator.approvals.decide(pending, True)
    done, _ = await asyncio.wait([task], timeout=5)  # (``_ended`` would raise what the task ended with)
    assert done == {task}
    del task, done
    gc.collect()  # a task whose error nobody read is reported when it is collected, with that error
    [record] = [r for r in caplog.records if r.name == "latet.bridge.api" and r.levelno >= logging.ERROR]
    assert record.getMessage() == f"the answer to a plugin call could not be given: ConnectionError (batch {pending})"
    assert record.exc_info is None and EMAIL not in caplog.text and plugin.bridge._answering == set()


async def test_a_task_cancelled_while_its_call_waits_is_no_failure(live, plugin, caplog):
    await plugin.mask()
    [task] = plugin.bridge._answering
    task.cancel()  # the daemon stops while the call waits for you
    await asyncio.wait([task], timeout=5)
    assert task.cancelled() and plugin.bridge._answering == set()
    failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in failures if r.name == "latet.bridge.api" or "callback" in r.getMessage()] == []


async def test_a_value_you_mask_is_learned_when_you_decide_whether_or_not_the_plugin_asks_again(live, plugin):
    vault = live.built.vault
    pending = await plugin.mask()
    [task] = plugin.bridge._answering
    [shown] = await live.mediator.approvals.pending()
    original = shown["items"][0]["original"]
    start = original.index("Orion")
    mask = {"item": 0, "start": start, "end": start + len("Orion"), "category": "confidential"}
    assert vault.by_original("Orion") is None
    assert await live.mediator.approvals.decide(pending, True, [mask])
    await _ended(task)  # nobody asked again: the answer is worked out and kept for the plugin's next poll
    assert vault.by_original("Orion") is not None
    [item] = (await plugin.wait(pending)).json()["items"]
    assert "Orion" not in item["text"]


# --- the event loop, and a store that does not answer ---------------------------------------------------------------


async def test_the_bridge_reads_a_calls_body_off_the_event_loop(live, monkeypatch):
    """A body of tens of MiB holds the GIL in the JSON parser for a second or more: read in the bridge's own thread, as
    the call's work is, never on the event loop that serves every other request meanwhile."""
    read_in: list[str] = []
    real = wellformed.loads

    def reading(raw, *args, **kwargs):
        read_in.append(threading.current_thread().name)
        return real(raw, *args, **kwargs)

    monkeypatch.setattr(wellformed, "loads", reading)
    async with Plugin(live) as plugin:
        body = {"session": "s1", "items": ["hello"]}
        answer = await plugin.client.post("/_latet/engine/v1/rehydrate", json=body, headers=plugin.token)
    assert answer.status_code == 200 and read_in and all(name.startswith("latet-bridge") for name in read_in)


@pytest.mark.parametrize(
    "silent",
    [
        lambda: redis.exceptions.TimeoutError("Timeout reading from 10.0.0.5:6379"),
        lambda: redis.exceptions.ConnectionError("Error connecting to 10.0.0.5:6379"),
        lambda: psycopg.OperationalError("connection to server at db.acme.lan failed"),
    ],
    ids=["valkey-timeout", "valkey-gone", "postgresql"],
)
async def test_a_plugin_call_whose_store_does_not_answer_is_answered_503(live, monkeypatch, caplog, silent):
    """As a dashboard page is: the plugin's call tries again later. Said in the log by the error's type, never its
    words, which name the address a driver could not reach."""

    def stalled(*args, **kwargs):
        raise silent()

    monkeypatch.setattr(live.mediator, "mask_value", stalled)
    async with Plugin(live) as plugin:
        body = {"session": "s1", "items": [{"text": PROMPT, "kind": "user_text"}]}
        answer = await plugin.client.post("/_latet/engine/v1/mask", json=body, headers=plugin.token)
    assert answer.status_code == 503 and "try again" in answer.json()["error"]
    [record] = [r for r in caplog.records if r.name == "latet.bridge.api"]
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert "10.0.0.5" not in caplog.text and "db.acme.lan" not in caplog.text and EMAIL not in caplog.text


async def test_a_plugin_calls_failure_of_its_own_is_no_503(live, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("a fault of Latet's own")

    monkeypatch.setattr(live.mediator, "mask_value", broken)
    async with Plugin(live) as plugin:
        body = {"session": "s1", "items": [{"text": PROMPT, "kind": "user_text"}]}
        with pytest.raises(ValueError, match="own"):  # raised out of the app: the server answers it 500
            await plugin.client.post("/_latet/engine/v1/mask", json=body, headers=plugin.token)
