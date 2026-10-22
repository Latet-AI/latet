"""What the dashboard reads of the state through its runtime: the images of a batch that waits for you, and how many
conversations each value of the vault was shown to."""

import asyncio
import itertools
import json
import time
import types

from starlette.requests import Request

from latet.api.rest import Dashboard
from latet.config import Config, IdentityConfig
from latet.engine import vault as vault_module
from latet.engine.types import EntityClass, SlotKind
from latet.runtime import make_runtime

from ...conftest import ROOT_KEY
from .conftest import EMAIL, _text_waiting
from .contracts import _Image


def _image_request(batch_id, item, view=None):
    """The request the dashboard's route hands ``approval_image``: the batch and the image in the path, ``view`` asked."""
    query = f"view={view}".encode() if view else b""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [],
            "query_string": query,
            "path_params": {"id": batch_id, "item": item},
        }
    )


async def test_the_dashboard_serves_an_image_of_a_batch_only_while_it_waits(tmp_path):
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    try:
        dashboard, queue = Dashboard(rt), rt.mediator.approvals
        batch = _text_waiting()
        batch.images = {0: _Image("b3JpZ2luYWw=", "c2VudA==")}
        await queue.open(batch)

        async def asked(item="0", view=None, batch_id=batch.id):
            return await dashboard.approval_image(_image_request(batch_id, item, view))

        original, sent = await asked(), await asked(view="sent")
        assert (original.status_code, original.body, original.media_type) == (200, b"original", "image/png")
        assert (sent.status_code, sent.body, sent.media_type) == (200, b"sent", "image/png")
        for served in (original, sent):  # neither view is kept by the browser, nor read as something else
            assert served.headers["cache-control"] == "no-store"
            assert served.headers["x-content-type-options"] == "nosniff"
        odd = _text_waiting()  # a media type that is no image's, as a request may say it: <b>hi</b> as a page
        odd.images = {0: _Image("PGI+aGk8L2I+", "PGI+aGk8L2I+", "text/html")}
        await queue.open(odd)
        for view in (None, "sent"):
            served = await asked(view=view, batch_id=odd.id)
            assert (served.status_code, served.body) == (200, b"<b>hi</b>")
            assert served.media_type == "application/octet-stream"  # never served as the page it says it is
            assert served.headers["cache-control"] == "no-store"
            assert served.headers["x-content-type-options"] == "nosniff"
        for item in ("1", "-1", "zero"):  # no such image
            assert (await asked(item)).status_code == 404
        assert (await asked(batch_id="nothing-waits-with-this-id")).status_code == 404
        assert await queue.decide(batch.id, False)
        assert (await asked()).status_code == 404  # decided: nothing of it is shown any more
    finally:
        rt.close()


async def _vault_answer(rt, query=b""):
    """The dashboard's Vault page as its route answers ``query``: the status and the body."""
    request = Request({"type": "http", "method": "GET", "headers": [], "query_string": query})
    answer = await rt.gateway.dashboard.vault(request)
    return answer.status_code, json.loads(answer.body)


async def _vault_page(rt, query=b""):
    """The dashboard's Vault page, as its route answers it."""
    return (await _vault_answer(rt, query))[1]


def _newest_first(vault):
    """The vault's values in the page's order: the newest first, and by id among values made in the same instant."""
    return [e.id for e in sorted(vault.entities(), key=lambda e: (-e.created, e.id))]


def _addresses(vault, count, etype="EMAIL", eclass=EntityClass.PII):
    return [vault.intern(etype, eclass, f"{etype.lower()}{n}@example-corp.com") for n in range(count)]


async def test_the_vault_answers_fifty_values_a_page_newest_first_with_the_count_and_the_pages(live):
    vault = live.built.vault
    _addresses(vault, 120)
    order = _newest_first(vault)
    first, third = await _vault_page(live), await _vault_page(live, b"page=3")
    assert [item["id"] for item in first["items"]] == order[:50]
    assert [item["id"] for item in third["items"]] == order[100:150]
    pages = -(-len(order) // 50)
    assert (first["total"], first["page"], first["pages"], first["per_page"]) == (len(order), 1, pages, 50)
    assert (third["total"], third["page"], third["pages"]) == (len(order), 3, pages)
    twenty = await _vault_page(live, b"per_page=20&page=2")
    assert [item["id"] for item in twenty["items"]] == order[20:40]
    assert (twenty["page"], twenty["per_page"], twenty["pages"]) == (2, 20, -(-len(order) // 20))


async def test_the_vault_keeps_a_page_and_its_size_within_bounds(live):
    vault = live.built.vault
    _addresses(vault, 60)
    order = _newest_first(vault)
    last = -(-len(order) // 50)
    past = await _vault_page(live, b"page=99")  # past the last page: the last one
    assert past["page"] == last and [item["id"] for item in past["items"]] == order[(last - 1) * 50 :]
    for query in (b"page=0", b"page=-3"):
        assert (await _vault_page(live, query))["page"] == 1
    assert (await _vault_page(live, b"per_page=5"))["per_page"] == 10
    huge = await _vault_page(live, b"per_page=100000")
    assert huge["per_page"] == 200 and len(huge["items"]) == min(200, len(order))
    for query in (b"page=two", b"per_page=many"):
        status, body = await _vault_answer(live, query)
        assert status == 400 and "items" not in body
    nothing = await _vault_page(live, b"q=matches-no-value-at-all")
    assert (nothing["items"], nothing["total"], nothing["page"], nothing["pages"]) == ([], 0, 1, 1)


async def test_vault_values_made_in_the_same_instant_keep_one_order_across_pages(live, monkeypatch):
    monkeypatch.setattr("latet.engine.vault.time.time", lambda: 1_800_000_000.0)
    vault = live.built.vault
    _addresses(vault, 30)
    order = _newest_first(vault)
    pages = [await _vault_page(live, f"per_page=10&page={n}".encode()) for n in (1, 2, 3, 4)]
    shown = [item["id"] for page in pages for item in page["items"]]
    assert shown == order and len(set(shown)) == len(order)


async def test_the_vault_counts_the_conversations_of_the_values_on_a_later_page(live, monkeypatch):
    # The vault's clock moves at every value it makes: Windows's moves in steps of about 16 ms, and values made in one
    # step come in the order of their ids, which put the address shown twice on the first page.
    ticks, start = itertools.count(), time.time()
    monkeypatch.setattr(vault_module, "time", types.SimpleNamespace(time=lambda: start + next(ticks) / 1000))
    engine, vault = live.built.engine, live.built.vault
    for conversation in ("conv-1", "conv-2"):
        engine.mask_text(f"mail {EMAIL}", SlotKind.USER_TEXT, engine.session(conversation))
    _addresses(vault, 60)  # newer: the address shown twice is on the second page now
    second = await _vault_page(live, b"page=2")
    assert [item["id"] for item in second["items"]] == _newest_first(vault)[50:100]
    counted = {item["id"]: item["sessions"] for item in second["items"]}
    assert counted[vault.by_original(EMAIL).id] == 2
    assert all(count == 0 for entity_id, count in counted.items() if entity_id != vault.by_original(EMAIL).id)


async def test_the_vault_filters_by_type_and_class_and_names_every_type_it_holds(live):
    vault = live.built.vault
    _addresses(vault, 3)
    people = [vault.intern("PERSON", EntityClass.NER, name) for name in ("Ada Byron", "Grace Hopper")]
    held = {}
    for e in vault.entities():
        held[e.etype] = held.get(e.etype, 0) + 1
    person = await _vault_page(live, b"type=person")
    assert sorted(item["id"] for item in person["items"]) == sorted(p.id for p in people)
    assert person["total"] == 2
    assert person["types"] == held  # the menu of types does not shrink as you filter
    ner = await _vault_page(live, b"class=ner")
    assert {item["class"] for item in ner["items"]} == {"NER"} and ner["total"] == 2
    both = await _vault_page(live, b"class=pii&type=person")
    assert both["items"] == [] and both["total"] == 0


async def test_the_vault_page_counts_the_conversations_each_value_was_shown_to(live):
    engine, vault = live.built.engine, live.built.vault
    other = "daniel.okafor@fabrikam.com"
    for conversation in ("conv-1", "conv-2"):
        engine.mask_text(f"mail {EMAIL}", SlotKind.USER_TEXT, engine.session(conversation))
    engine.mask_text(f"and {other}", SlotKind.USER_TEXT, engine.session("conv-2"))
    unshown = vault.intern("PERSON", EntityClass.NER, "Ada Byron")  # kept, and no conversation was shown it
    page = await _vault_page(live)
    counted = {item["id"]: item["sessions"] for item in page["items"]}
    values = (vault.by_original(EMAIL), vault.by_original(other), unshown)
    assert [counted[value.id] for value in values] == [2, 1, 0]
    assert page["total"] == len(counted) == len(vault.entities())


async def test_the_vault_page_reads_the_conversations_and_the_vault_off_the_event_loop(live, monkeypatch):
    """At 2,000 conversations of 400 entities the counting takes seconds, and on a server ``sessions()`` reads Valkey:
    done in a thread, never on the event loop that serves everyone else meanwhile."""
    on_a_loop: list[bool] = []
    real = live.built.engine.sessions

    def sessions():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_a_loop.append(False)
        else:
            on_a_loop.append(True)
        return real()

    monkeypatch.setattr(live.built.engine, "sessions", sessions)
    page = await _vault_page(live)
    assert on_a_loop == [False] and page["total"] == len(live.built.vault.entities())


class ShownMeanwhile(str):
    """An entity's id that, the moment the Vault page counts it, has a request show its conversation one more entity:
    the engine's thread adds to a conversation's entities while the event loop reads them."""

    conversation = None

    def __hash__(self):
        conversation, self.conversation = self.conversation, None
        if conversation is not None:
            conversation.add({"shown-meanwhile"})
        return super().__hash__()


async def test_the_vault_page_counts_while_a_request_shows_a_conversation_one_more_entity(live):
    engine = live.built.engine
    session = engine.session("conv-1")
    engine.mask_text(f"mail {EMAIL}", SlotKind.USER_TEXT, session)
    address = live.built.vault.by_original(EMAIL)
    counting = ShownMeanwhile("an-entity-of-conv-1")
    session.add({counting})
    counting.conversation = session
    page = await _vault_page(live)  # not "Set changed size during iteration": the page counts over a copy
    assert "shown-meanwhile" in session.entity_ids
    assert [item["sessions"] for item in page["items"] if item["id"] == address.id] == [1]
