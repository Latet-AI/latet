"""What the engine, the plugin's bridge and the gateway tell the conversations: the entities a conversation is shown,
its project, the texts approved in it, the responses that name it and what the model wrote."""

import json

import pytest

from latet.bridge.api import Bridge
from latet.config import Config, IdentityConfig, LiveReview
from latet.engine.build import build
from latet.engine.masker import MaskReport
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.gateway.approvals import Decision
from latet.gateway.mediate import LiveItem, Masked, Mediator, WSResponseRewriter
from latet.gateway.routes import Upstream
from latet.projects import Projects
from latet.state import LocalConversations

from ...conftest import ROOT_KEY

OPENAI = Upstream("openai", "/openai", "http://127.0.0.1:1")


def _held(session):
    """A conversation as its conversations are told of it: its key, its entities, its project, its approved texts."""
    return (session.key, frozenset(session.entity_ids), session.project_id, frozenset(session.approved))


class Recording(LocalConversations):
    """A computer's conversations, with what the engine told them."""

    def __init__(self) -> None:
        super().__init__()
        self.updates: list[tuple[str, frozenset[str], str | None, frozenset[str]]] = []

    def update(self, session, shown=()):
        self.updates.append(_held(session))
        super().update(session, shown)


@pytest.fixture
def wired():
    """A ``Recording``, the engine that keeps its conversations there (with projects) and its mediator. Each test
    below changes a conversation in one way and says what the conversations are told of it."""
    kept = Recording()
    built = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY), conversations=kept)
    built.engine.scopes = Projects(Config(), built.vault)
    mediator = Mediator(built.engine, built.verifier)
    yield kept, built.engine, mediator
    mediator.close()


def _known_address(kept, engine):
    """An address the vault knows because a first conversation was shown it; the record starts empty after it."""
    engine.mask_text("mail emily.carter@acme.com", SlotKind.USER_TEXT, engine.session("conv-1"))
    kept.updates.clear()
    return engine.vault.by_original("emily.carter@acme.com")


def _request(mediator, path, body, headers=None):
    """A request of an agent that is not Claude Code, as the gateway prepares it."""
    return mediator.prepare(OPENAI, "POST", path, headers or {}, json.dumps(body).encode())


def test_a_text_masked_now_shows_its_conversation_the_entities(wired):
    kept, engine, _ = wired
    session = engine.session("conv-1")
    engine.mask_text("mail emily.carter@acme.com", SlotKind.USER_TEXT, session)
    engine.mask_text("mail emily.carter@acme.com", SlotKind.USER_TEXT, session)  # the same block: nothing new
    assert session.entity_ids and kept.updates == [_held(session)]


def test_a_text_found_in_the_cache_shows_another_conversation_the_entities(wired):
    kept, engine, _ = wired
    address = _known_address(kept, engine)
    other = engine.session("conv-4")  # the same text in another conversation: found in the cache
    assert engine.mask_text("mail emily.carter@acme.com", SlotKind.USER_TEXT, other).cached
    assert other.entity_ids == {address.id} and kept.updates == [_held(other)]


def test_the_fakes_in_the_text_of_a_resumed_conversation_are_shown_to_it(wired):
    kept, engine, _ = wired
    address = _known_address(kept, engine)
    resumed = engine.session("conv-2")
    engine.mask_text(f"again {address.surrogate}", SlotKind.USER_TEXT, resumed, keep_surrogates=True)
    assert resumed.entity_ids == {address.id} and kept.updates == [_held(resumed)]


def test_a_text_its_conversation_sent_before_shows_it_the_entities_again(wired):
    kept, engine, mediator = wired
    again, report = engine.session("conv-3"), MaskReport()
    mediator.mask_value("write emily.carter@acme.com", SlotKind.USER_TEXT, again, report)
    shown = set(again.entity_ids)
    kept.updates.clear()
    again.entity_ids.clear()  # the conversation forgotten and made again, while its text is still known as sent
    mediator.mask_value("write emily.carter@acme.com", SlotKind.USER_TEXT, again, report)
    assert shown and again.entity_ids == shown and kept.updates == [_held(again)]


def test_the_fakes_the_plugin_registers_are_shown_to_the_conversation(wired):
    kept, engine, mediator = wired
    address = _known_address(kept, engine)
    history = [f"again {address.surrogate}"]  # of a resumed conversation, as the plugin sends it to /scope
    scoped = Bridge(mediator, "token")._scope({"session": "resumed", "items": history})
    resumed = engine.session("cc:resumed")
    assert scoped == 1 and resumed.entity_ids == {address.id} and kept.updates == [_held(resumed)]


def test_the_project_a_conversation_gets_is_kept_with_it(wired, tmp_path):
    kept, engine, _ = wired
    session = engine.session("conv-1")
    engine.attach(session, str(tmp_path))
    engine.attach(session, str(tmp_path))  # the same folder: nothing new
    assert session.project_id is not None and kept.updates == [_held(session)]


def test_the_plugin_gives_a_conversation_its_project_through_the_engine(wired, tmp_path):
    kept, _, mediator = wired
    session = Bridge(mediator, "token")._session({"session": "attached", "cwd": str(tmp_path)})
    assert session.key == "cc:attached" and session.project_id is not None and kept.updates == [_held(session)]


def test_the_gateway_gives_a_conversation_its_project_through_the_engine(wired, tmp_path):
    kept, _, mediator = wired
    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]}
    session = _request(mediator, "/openai/v1/chat/completions", body, {"x-latet-project": str(tmp_path)}).session
    assert not session.key.startswith("cc:")  # not Claude Code's, whose project the plugin gives
    assert session.project_id is not None and kept.updates == [_held(session)]


def test_a_text_you_approve_is_kept_with_its_conversation_and_still_waits_in_another(wired):
    kept, engine, mediator = wired
    session, live = engine.session("conv-1"), LiveReview(enabled=True)
    done = Masked("call Ada Byron", [], original="call Ada Byron")
    assert mediator.live_item(live, session, 0, SlotKind.USER_TEXT, done) is not None  # it waits for you
    item = LiveItem(0, "call Ada Byron", SlotKind.USER_TEXT, None, None, False, "call Ada Byron")
    mediator.apply_live(session, [item], Decision(True), keep=False)
    assert session.approved and kept.updates == [_held(session)]
    assert mediator.live_item(live, session, 0, SlotKind.USER_TEXT, done) is None  # approved
    assert mediator.live_item(live, engine.session("conv-other"), 0, SlotKind.USER_TEXT, done) is not None


def test_a_conversation_keeps_two_thousand_approved_texts_and_past_that_an_older_one_waits_again():
    """Each approved text is a key the conversation keeps for as long as it is among those kept: with clean texts
    approved by themselves, a long session would add them without end. Past 2,000 another one is forgotten, never one
    approved now: a text forgotten only waits for you once more."""
    built = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    session, live = built.engine.session("conv-1"), LiveReview(enabled=True)

    def approve(*notes):
        texts = [LiveItem(0, f"note {n}", SlotKind.USER_TEXT, None, None, False, f"note {n}") for n in notes]
        mediator.apply_live(session, texts, Decision(True), keep=False)

    def waits(n):
        done = Masked(f"note {n}", [], original=f"note {n}")
        return mediator.live_item(live, session, 0, SlotKind.USER_TEXT, done) is not None

    try:
        for n in range(2_000):
            approve(n)
        assert len(session.approved) == 2_000 and not any(waits(n) for n in range(2_000))
        approve(2_000)
        assert len(session.approved) == 2_000 and not waits(2_000)  # the text approved now is not the one forgotten
        assert sum(waits(n) for n in range(2_000)) == 1  # one older text waits again
        approve(2_001, 2_002, 2_003)  # one call's texts, approved together
        assert len(session.approved) == 2_000 and not any(waits(n) for n in (2_001, 2_002, 2_003))
    finally:
        mediator.close()


def test_a_response_names_its_conversation_for_the_request_that_follows(wired):
    kept, engine, mediator = wired
    session = engine.session("conv-1")
    mediator._on_response_id(session)("resp_1")
    assert kept.named("resp_1") == "conv-1"
    following = {"model": "gpt-5", "input": "and then?", "previous_response_id": "resp_1"}
    assert _request(mediator, "/openai/v1/responses", following).session is session


def test_a_websocket_answer_names_its_conversation_once_it_holds_a_value_as_an_answer_over_http_does(wired):
    """A conversation with nothing masked in it has nothing to restore: its frames pass as they come and the response a
    frame creates names nothing, as an answer over HTTP has no transform then (``response_transform``); once it holds
    a value, the response a frame creates names it for the request that follows."""
    kept, engine, mediator = wired
    session = engine.session("ws:emily")
    rewriter = WSResponseRewriter(mediator, session)
    created = json.dumps({"type": "response.created", "response": {"id": "resp_empty"}})
    assert rewriter.on_frame(created) == [created] and kept.named("resp_empty") is None
    engine.mask_text("mail emily.carter@acme.com", SlotKind.USER_TEXT, session)
    rewriter.on_frame(json.dumps({"type": "response.created", "response": {"id": "resp_held"}}))
    assert kept.named("resp_held") == "ws:emily"


def test_what_the_model_wrote_is_kept_by_its_restored_form(wired):
    kept, engine, _ = wired
    engine.remember_model_output("You said: mail Rachel.Huang@fourthcoffee.example", "You said: mail emily")
    assert kept.recall(engine._key("R", "You said: mail emily")) == "You said: mail Rachel.Huang@fourthcoffee.example"
