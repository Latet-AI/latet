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

from latet.bridge.api import Bridge, a_store_not_answering
from latet.config import Config, IdentityConfig, LiveReview
from latet.engine import wellformed
from latet.engine.types import EntityClass
from latet.engine.vault import MaskingError
from latet.gateway import approvals as approvals_module
from latet.gateway.approvals import EXPIRED, REJECTED, batch_version
from latet.runtime import make_runtime
from latet.state import IdentityRef
from latet.vision.masking import ImageMasking

from ...conftest import ROOT_KEY
from ...fake_ocr import FakeOcr
from ...synced import SyncedEdition
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


async def _shown_as_it_leaves(live, batch_id):
    """The image of ``batch_id``'s first text as the dashboards show it "As it leaves now"."""
    found = await live.mediator.approvals.image(batch_id, 0, sent=True)
    assert found is not None
    return found


async def test_an_image_waiting_is_shown_as_it_leaves_now_once_its_texts_are_masked_again(live, plugin):
    """The final security review's approvals L4. A value in an image is covered with a bar when it first waits; let
    through meanwhile (Not sensitive: always allow), or a value of it kept in the vault, its texts are masked again, and
    its picture "As it leaves now" is painted again with them, never the one painted when it first waited: what you see
    there is what leaves once you approve."""
    reader = FakeOcr()
    live.mediator.vision = ImageMasking(reader, live.store)
    picture = reader.screenshot([f"Email: {EMAIL}", "Account of Orion since 2019"])
    pending = await plugin.mask([{"image": {"data": picture, "media_type": "image/png"}, "kind": "tool_result"}])
    covered = await _shown_as_it_leaves(live, pending)
    assert covered[0] != picture  # the address under a bar

    def allowed(config):
        config = config.model_copy(deep=True)
        config.allow_values.append(EMAIL)
        return config

    live.update_config(allowed)
    await _first_preview(live, pending, lambda masked: EMAIL in masked)
    clear = await _shown_as_it_leaves(live, pending)
    assert clear != covered  # nothing covers the address any more
    await live.mediator.run(_keep, live, "Orion")  # and a value of its second line is masked now
    await _first_preview(live, pending, lambda masked: "Orion" not in masked)
    shown = await _shown_as_it_leaves(live, pending)
    assert shown not in (covered, clear)
    assert await live.mediator.approvals.decide(pending, True)
    [item] = (await plugin.wait(pending)).json()["items"]
    assert (item["image"]["data"], item["image"]["media_type"]) == shown  # it left as it was shown


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

    async def once_refused(batch_id, items, pictures=None):
        if not refused:
            refused.append(batch_id)
            return False
        return await real(batch_id, items, pictures)

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


MARK = IdentityRef("u_markellisx", "Mark Ellis", "person")  # a decider of the company (Latet Enterprise)


def _allowed(config):
    """The address let through (Not sensitive: always allow): a change that lowers protection."""
    config = config.model_copy(deep=True)
    config.allow_values.append(EMAIL)
    return config


async def _read(live, pending):
    """The version of the text waiting, as a decider reads it (the Company approvals route's ``version``)."""
    batch = await live.mediator.approvals.backend.fetch(pending)
    assert batch is not None
    return batch_version(batch.items)


async def test_a_text_a_decider_approved_leaves_as_they_read_it(live, plugin):
    """Nothing changed between the reading and the sending: the text masked with the rules of sending is the one they
    approved, and it leaves."""
    pending = await plugin.mask()
    assert await live.mediator.approvals.decide(pending, True, by=MARK, version=await _read(live, pending))
    answer = await plugin.wait(pending)
    [item] = answer.json()["items"]
    assert answer.status_code == 200 and EMAIL not in item["text"] and "Orion" in item["text"]


async def test_a_text_masked_otherwise_when_it_is_sent_than_a_decider_approved_it_is_withheld_and_waits_again(
    live, plugin, monkeypatch
):
    """The review of Task 10 (fix round 2): a decider approved the text as they read it; a change of the rules that
    lowers protection lands between their approval and its sending (the address let through). Masked as it would
    leave, it is not what they approved: it is withheld (nothing of it in clear reaches the plugin), it is not taken
    as approved, and the agent's next call with it waits again. The person's own approval leaves masked with the rules
    of now, as before (``test_a_value_you_let_through_while_a_text_waits_leaves_in_clear_once_you_approve``)."""
    pending = await plugin.mask()
    version = await _read(live, pending)
    real = plugin.bridge._decided

    def lowered_first(waiting, decision):
        live.update_config(_allowed)  # between the approval and the sending
        return real(waiting, decision)

    monkeypatch.setattr(plugin.bridge, "_decided", lowered_first)
    feed = await live.mediator.approvals.watch()  # her dashboard
    assert await live.mediator.approvals.decide(pending, True, by=MARK, version=version)
    answer = await plugin.wait(pending)
    assert EMAIL not in answer.text and "masked otherwise" in answer.json()["error"], answer.text
    told = [feed.get_nowait() for _ in range(feed.qsize())]
    assert ("withheld", {"id": pending}) in told, told  # her dashboard is told, in her own words (fix round 3, m3)
    live.mediator.approvals.unwatch(feed)
    again = await plugin.mask()  # the agent tries again: it waits again
    assert again != pending


async def test_a_lowering_between_the_comparison_and_the_masking_still_withholds_the_text(live, plugin, monkeypatch):
    """The review of Task 10, fix round 4 (I-a): the rules change right after the sending's comparison and before its
    real masking (another request saved them on its own thread). The text is withheld all the same, nothing of it in
    clear, never taken as approved: the agent's next call with it waits again."""
    pending = await plugin.mask()
    version = await _read(live, pending)
    mediator = live.mediator
    compared = mediator._views_now

    def lowered_after(*args, **kwargs):
        views = compared(*args, **kwargs)
        live.update_config(_allowed)  # right after the comparison's masking, before the real one
        return views

    monkeypatch.setattr(mediator, "_views_now", lowered_after)
    assert await mediator.approvals.decide(pending, True, by=MARK, version=version)
    answer = await plugin.wait(pending)
    assert EMAIL not in answer.text and "masked otherwise" in answer.json().get("error", ""), answer.text
    monkeypatch.setattr(mediator, "_views_now", compared)
    again = await plugin.mask()  # the agent tries again: it waits again
    assert again != pending


async def test_a_masker_swapped_before_its_rules_are_counted_still_withholds_the_text(live, plugin, monkeypatch):
    """The review of Task 10, fix round 5 (m-c): ``Engine.use`` puts the new masker in place, then counts the new rules;
    a masking in between uses the new masker under the old count. Here the masker is swapped and not counted right
    after the comparison, and the decider's own mask keeps the real masking off every cache: the text is withheld all
    the same (the stamp holds the masker itself, compared by identity), and waits again."""
    pending = await plugin.mask()
    version = await _read(live, pending)
    mediator = live.mediator
    compared = mediator._views_now

    def swapped_after(*args, **kwargs):
        views = compared(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(mediator.engine, "rules_changed", lambda: None)  # the new masker, its rules not counted yet
            live.update_config(_allowed)
        return views

    monkeypatch.setattr(mediator, "_views_now", swapped_after)
    start = PROMPT.index("Orion")
    mask = {"item": 0, "start": start, "end": start + len("Orion"), "category": "org", "value": "Orion"}
    assert await mediator.approvals.decide(pending, True, [mask], by=MARK, version=version)
    answer = await plugin.wait(pending)
    assert EMAIL not in answer.text and "masked otherwise" in answer.json().get("error", ""), answer.text
    monkeypatch.setattr(mediator, "_views_now", compared)
    again = await plugin.mask()
    assert again != pending


@pytest.fixture
def synced_live(tmp_path):
    """A runtime as ``live`` (the Live Reviewer on), made with an edition that syncs terms (Latet Enterprise, on a
    server): the client the conftest's PROMPT names, `Orion`, among them."""
    edition = SyncedEdition()
    edition.live.use([("Orion", "CLIENT")], "1.1")
    config = Config(identity=IdentityConfig(enabled=False), live_review=LiveReview(enabled=True))
    rt = make_runtime(
        config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY, edition=edition
    )
    yield rt
    rt.close()


@pytest.fixture
async def synced_plugin(synced_live, monkeypatch):
    """``plugin``, asking the bridge of ``synced_live``."""
    monkeypatch.setattr("latet.bridge.api.HOLD", 5.0)
    async with Plugin(synced_live) as asking:
        yield asking


async def test_a_sync_between_the_comparison_and_the_masking_still_withholds_the_text(
    synced_live, synced_plugin, monkeypatch
):
    """R7: the terms a company syncs change in place, under the same masker and the same count of the rules; a sync
    right after the sending's comparison and before its real masking withholds the text all the same (the terms' tag is
    in the stamp of the rules), nothing of it in clear, never taken as approved: the agent's next call waits again."""
    pending = await synced_plugin.mask()
    version = await _read(synced_live, pending)
    mediator = synced_live.mediator
    compared = mediator._views_now

    def synced_after(*args, **kwargs):
        views = compared(*args, **kwargs)
        synced_live.edition.live.use([], "2.1")  # right after the comparison's masking, before the real one
        return views

    monkeypatch.setattr(mediator, "_views_now", synced_after)
    assert await mediator.approvals.decide(pending, True, by=MARK, version=version)
    answer = await synced_plugin.wait(pending)
    assert "Orion" not in answer.text and "masked otherwise" in answer.json().get("error", ""), answer.text
    monkeypatch.setattr(mediator, "_views_now", compared)
    again = await synced_plugin.mask()  # the agent tries again: it waits again
    assert again != pending


async def test_a_text_waiting_is_shown_masked_again_after_a_sync_and_a_change_of_the_vault_that_misses_it(
    synced_live, synced_plugin
):
    synced_live.edition.live.use([], "1.0")  # no synced term yet: Orion in clear in the preview
    pending = await synced_plugin.mask()
    assert "Orion" in (await _first_preview(synced_live, pending, lambda masked: True))
    synced_live.edition.live.use([("Orion", "CLIENT")], "2.1")
    await synced_live.mediator.run(_keep, synced_live, "Vega Systems")  # a value no preview holds
    await _first_preview(synced_live, pending, lambda masked: "Orion" not in masked)


def test_the_previews_and_a_deciders_stamps_move_when_the_same_terms_come_back(synced_live):
    """The re-review's R1: the terms taken away and the same ones given again come back under the same tag, never the
    same stamp: the previews are made again (``_live_stamp``) and a decider's approval of before is withheld
    (``_rules_stamp``)."""
    from latet.gateway.mediate import _same_rules

    mediator = synced_live.mediator
    session = mediator.engine.session("live-1")
    previews, rules = mediator._live_stamp(), mediator._rules_stamp(session)
    synced_live.edition.live.use([], "")
    synced_live.edition.live.use([("Orion", "CLIENT")], "1.1")
    assert mediator.engine.masker.tag.endswith("1.1")
    assert mediator._live_stamp() != previews and not _same_rules(mediator._rules_stamp(session), rules)


async def test_a_text_quoting_a_fake_of_an_earlier_answer_leaves_as_a_decider_approved_it(live, plugin):
    """The review of Task 10, fix round 4 (m-a): Claude Code's prompts quote the fakes of earlier answers, which the
    plugin's masking keeps as they are (``keep``). The sending's comparison masks the text the same way: a decider's
    approval of it lets it leave, the fake as it was and the new value masked."""
    first = await plugin.mask()
    assert await live.mediator.approvals.decide(first, True)  # the person's own, earlier
    [said] = (await plugin.wait(first)).json()["items"]
    fake = said["text"].split()[1]  # "ask <fake> about the Orion contract"
    assert fake != EMAIL and "@" in fake
    text = f"also copy {fake} on the mail to grace.hopper@acme.com"  # a fake of before, and a new value
    quoted = {"session": "live-1", "items": [{"text": text, "kind": "user_text"}]}
    pending = (await plugin.client.post("/_latet/engine/v1/mask", json=quoted, headers=plugin.token)).json()["pending"]
    assert await live.mediator.approvals.decide(pending, True, by=MARK, version=await _read(live, pending))
    answer = await plugin.wait(pending)
    [sent] = answer.json()["items"]
    assert fake in sent["text"] and "grace.hopper@acme.com" not in sent["text"] and EMAIL not in sent["text"], sent


async def test_a_preview_that_could_not_be_made_again_is_never_taken_for_current(live, plugin, monkeypatch):
    """The rules changed while the text waited, and its preview could not be made again (its masking failed): the old
    preview is not said to be made as of now (the next change tries again), and a decider's approval of it is withheld
    when it is sent, never leaving under the rules of now."""
    pending = await plugin.mask()
    version = await _read(live, pending)
    mediator, real, tried = live.mediator, live.mediator.mask_value, []

    def failing(*args, **kwargs):
        tried.append(True)
        raise RuntimeError("the masking failed")

    monkeypatch.setattr(mediator, "mask_value", failing)
    live.update_config(_allowed)
    await _until(lambda: tried)
    await asyncio.sleep(0.2)  # the refresh's pass ends after the failure
    assert mediator._held_live[pending].made_at != mediator._live_stamp()  # not taken for made as of now
    monkeypatch.setattr(mediator, "mask_value", real)
    assert EMAIL not in (await mediator.approvals.pending())[0]["items"][0]["masked"]  # the old preview, still stored
    assert await mediator.approvals.decide(pending, True, by=MARK, version=version)
    answer = await plugin.wait(pending)
    assert EMAIL not in answer.text and "masked otherwise" in answer.json()["error"], answer.text


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


@pytest.mark.parametrize(
    ("error", "silent"),
    [
        (redis.exceptions.TimeoutError("Timeout reading from 10.0.0.5:6379"), True),
        (redis.exceptions.ConnectionError("Error connecting to 10.0.0.5:6379"), True),
        (psycopg.OperationalError("connection to server at db.acme.lan failed"), True),
        (TimeoutError(), True),
        (ConnectionResetError(), True),
        (ValueError("a fault of Latet's own"), False),
        (MaskingError("masking failed"), False),
    ],
    ids=["valkey-timeout", "valkey-gone", "postgresql", "timeout", "connection", "own-fault", "masking"],
)
def test_a_store_that_does_not_answer_is_told_by_its_error(error, silent):
    """What the bridge and the applications API answer 503: one helper, public, that both read."""
    assert a_store_not_answering(error) is silent


async def test_a_plugin_calls_failure_of_its_own_is_no_503(live, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("a fault of Latet's own")

    monkeypatch.setattr(live.mediator, "mask_value", broken)
    async with Plugin(live) as plugin:
        body = {"session": "s1", "items": [{"text": PROMPT, "kind": "user_text"}]}
        with pytest.raises(ValueError, match="own"):  # raised out of the app: the server answers it 500
            await plugin.client.post("/_latet/engine/v1/mask", json=body, headers=plugin.token)
