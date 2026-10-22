"""A JSON document's keys are masked as its values are: an address, a phone number, a secret, a name or a term that is a
key leaves as its fake, the fake the value has everywhere, and comes back restored in an answer. A field rule and strict
data mode still find a value by its key as it came; a key that names a field never changes; two keys of one object
that would be one once masked stop the request."""

import json

import pytest

from latet.api.rest import restore_detail
from latet.config import Config, FieldRule, TermConfig
from latet.engine import wellformed
from latet.engine.build import build
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault
from latet.gateway.mediate import REWRITE, JSONTransform, Mediator
from latet.gateway.recorder import json_paths
from latet.protocols.rewrite import ResponsesStream, restore_chat_completion, restore_response_item

from ..conftest import ROOT_KEY
from .test_reviewer import FakeServer, send
from .test_reviewer import mediator as reviewing
from .test_reviewer import request as reviewed_request
from .test_what_leaves import (
    ANTHROPIC,
    BEARER,
    EMAIL,
    OPENAI,
    PHONE,
    ROUTES,
    SECRET,
    _mediator,
    _object,
    _prepared,
    _responses_input,
    _sent_chat,
)

API_KEY = {"x-api-key": "sk-ant-api-test"}
# review 6's probe: an address, a phone number and a token as the keys of a tool's output
KEYS = json.dumps({"users": {value: {"note": "ok"} for value in (EMAIL, PHONE, SECRET)}})
COLLIDING = "Daniel Jordan"  # the fake a vault is made to draw for the term below, and a key beside it


def _fake(mediator: Mediator, value: str) -> str:
    """The fake ``value`` has in the mediator's vault."""
    entity = mediator.engine.vault.by_original(value)
    assert entity is not None, value
    return entity.surrogate


def _drawing(mediator: Mediator, original: str, fake: str) -> None:
    """The vault draws ``fake`` for ``original``, whatever its counter, and as it always does for the rest."""
    vault = mediator.engine.vault
    drawn = vault._generate
    vault._generate = lambda etype, eclass, value, *rest, **more: (  # type: ignore[method-assign]
        fake if value == original else drawn(etype, eclass, value, *rest, **more)
    )


# --- review 6's probe, masked and sent --------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["balanced", "max"])
@pytest.mark.parametrize("route", list(ROUTES))
def test_an_address_a_phone_number_and_a_token_as_keys_of_a_tool_output_leave_masked_and_are_sent(route, profile):
    mediator = _mediator(profile)
    prepared = _prepared(mediator, route, KEYS)
    assert prepared.mode == REWRITE and prepared.block_reason is None
    assert not [leak for leak in prepared.leaks if leak.blocks]
    body = prepared.body.decode()
    assert not any(value in body or json.dumps(value)[1:-1] in body for value in (EMAIL, PHONE, SECRET))
    fakes = {_fake(mediator, value) for value in (EMAIL, PHONE, SECRET)}
    assert len(fakes) == 3 and all(fake in body or json.dumps(fake)[1:-1] in body for fake in fakes)


def test_an_address_a_phone_number_and_a_token_as_keys_of_a_websocket_turn_leave_masked_and_are_sent():
    mediator = _mediator()
    frame = json.dumps({"type": "response.create", "model": "m", "input": _responses_input(KEYS)})
    text, prepared = mediator.mask_ws_frame(frame, mediator.engine.session("session-id:s-1"))
    assert prepared is not None and prepared.block_reason is None and not prepared.leaks
    output = json.loads(json.loads(text)["input"][2]["output"])
    assert sorted(output["users"]) == sorted(_fake(mediator, value) for value in (EMAIL, PHONE, SECRET))


def test_a_key_leaves_as_the_fake_its_value_has_everywhere():
    """The same vault, the same detectors: an address as a key and as a value, in two outputs, is one fake."""
    mediator = _mediator()
    as_key = _prepared(mediator, "chat completions", json.dumps({EMAIL: {"role": "admin"}}))
    as_value = _prepared(mediator, "chat completions", json.dumps({"owner": EMAIL}))
    fake = _fake(mediator, EMAIL)
    assert json.loads(_sent_chat(as_key)) == {fake: {"role": "admin"}}
    assert json.loads(_sent_chat(as_value)) == {"owner": fake}


@pytest.mark.parametrize("profile", ["balanced", "max"])
def test_a_key_that_names_a_field_never_changes(profile):
    """Keys are masked where a detector finds a value in them, never as a field's value: strict data mode masks every
    value of a structure, and no key of one."""
    output = json.dumps(
        {"id": 7, "name": "Northwind", "email": "billing", "status": "OVERDUE", "items": [{"sku": "A-100", "qty": 2}]}
    )
    prepared = _prepared(_mediator(profile), "chat completions", output)
    sent = json.loads(_sent_chat(prepared))
    assert prepared.block_reason is None and list(sent) == ["id", "name", "email", "status", "items"]
    assert list(sent["items"][0]) == ["sku", "qty"]
    if profile == "max":  # its values are masked, by their fields
        assert sent["name"] != "Northwind" and sent["status"] != "OVERDUE"


def test_a_value_is_found_by_its_key_as_it_came_and_a_key_that_is_a_value_is_masked():
    """A secret's name (`password`) and a field rule of yours (`pin`, secret) find their values under a key that is an
    address, masked itself as a value: the rules read the keys as they came."""
    mediator = _mediator(fields=[FieldRule(name="pin", secret=True)])
    output = json.dumps({EMAIL: {"password": "hunter2-Lavender", "pin": "4417"}})
    prepared = _prepared(mediator, "chat completions", output)
    sent = json.loads(_sent_chat(prepared))
    fake = _fake(mediator, EMAIL)
    assert prepared.block_reason is None and list(sent) == [fake]
    assert sent[fake]["password"] != "hunter2-Lavender" and sent[fake]["pin"] != "4417"
    assert list(sent[fake]) == ["password", "pin"]


def test_a_field_rule_naming_a_path_through_a_key_that_is_a_value_still_finds_it():
    """With strict data mode on, a field kept in clear by its whole path, through a key that is an address: the value
    stays in clear, found by the path as it came, and the address leaves masked."""
    keep = FieldRule(name=f"users.{EMAIL}.note", mask=False)
    mediator = _mediator("max", fields=[keep])
    output = json.dumps({"users": {EMAIL: {"note": "called on monday", "city": "Springfield"}}})
    sent = json.loads(_sent_chat(_prepared(mediator, "chat completions", output)))
    fake = _fake(mediator, EMAIL)
    assert list(sent["users"]) == [fake]
    assert sent["users"][fake]["note"] == "called on monday" and sent["users"][fake]["city"] != "Springfield"


def test_a_value_under_a_masked_key_names_its_field_as_it_was_sent():
    """What an exchange shows and keeps of a value strict data mode masked names its field: through a key that is an
    address, the field holds the address's fake, as the text sent does, and nowhere the address."""
    mediator = _mediator("max")
    output = json.dumps({"users": {EMAIL: {"note": "called on monday"}}})
    prepared = _prepared(mediator, "chat completions", output)
    fake = _fake(mediator, EMAIL)
    [(_, replacements, exact)] = [entry for entry in prepared.pending if entry[1]]
    rules = {r.rule for r in replacements}
    assert not exact and f"strict:users.{fake}.note" in rules
    assert not any(EMAIL in rule for rule in rules)


def test_the_dashboard_names_a_field_under_a_masked_key_by_the_key_itself():
    """The dashboard shows an exchange as it was on your machine: a value strict data mode masked under a key that is
    an address names its field with the address, as the text it shows does, so that keeping that field in clear makes
    a rule that matches it (named by the key's fake, it matched nothing). The record keeps the field as it was sent."""
    mediator = _mediator("max")
    output = json.dumps({"users": {EMAIL: {"note": "called on monday"}}})
    prepared = _prepared(mediator, "chat completions", output)
    [(slot, replacements, _)] = [entry for entry in prepared.pending if entry[1]]
    masked, fake = slot.get(), _fake(mediator, EMAIL)

    def kept(r) -> dict:  # a replacement as the history keeps it: where its fake is in the text sent, never its value
        start = masked.index(r.replacement)
        return {"masked_start": start, "masked_end": start + len(r.replacement), "type": r.etype,
                "class": r.eclass.name, "entity": r.entity_id, "rule": r.rule}  # fmt: skip

    recorded = [kept(r) for r in replacements]
    assert f"strict:users.{fake}.note" in {r["rule"] for r in recorded}
    detail = {"request": {"slots": [{"masked": masked, "replacements": recorded, "block": "b1"}]}, "leaks": []}
    shown = restore_detail(detail, mediator.engine.vault)["request"]["slots"][0]
    rules = {r["rule"] for r in shown["replacements"]}
    assert f"strict:users.{EMAIL}.note" in rules and not any(fake in (rule or "") for rule in rules)


def test_two_keys_that_would_be_one_once_restored_are_left_as_the_model_wrote_them():
    """The model writes a key holding a fake beside the key that fake stands for: restored, the two would be one and a
    value lost, as JSON keeps the last of two keys alike. Each is left as the model wrote it; the others are restored."""
    mediator = _mediator()
    fake = _taught(mediator, "anthropic with an API key")
    session = mediator.engine.session("session-id:s-1")
    users = {fake: {"role": "owner"}, EMAIL: {"role": "viewer"}}
    answer = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t9", "name": "grant", "input": {"users": users, fake: "notify"}}],
    }
    prepared = mediator.prepare(
        ANTHROPIC, "POST", "/anthropic/v1/messages", {**API_KEY, "session-id": "s-1"},
        json.dumps({"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "go"}]}).encode(),
    )  # fmt: skip
    transform = mediator.response_transform(prepared, "application/json", None)
    assert isinstance(transform, JSONTransform) and prepared.session is session
    transform.feed(json.dumps(answer).encode())
    restored = json.loads(transform.end())
    assert restored["content"][0]["input"] == {"users": users, EMAIL: "notify"}


def test_two_keys_that_would_be_one_once_restored_are_left_as_the_model_wrote_them_in_a_text_of_json():
    """The same in a tool call's arguments, a text of JSON: in chat completions' answer, and in a Responses item and the
    event that ends its arguments. Each of the two keys is left as the model wrote it, the others are restored, and no
    object gives a key twice (an agent would read the last of the two, the first one's value lost)."""
    mediator = _mediator()
    fake = _taught(mediator, "chat completions")
    ctx = mediator.restore_context(mediator.engine.session("session-id:s-1"))
    users = {fake: {"role": "owner"}, EMAIL: {"role": "viewer"}}
    arguments = json.dumps({"users": users, fake: "notify"})

    def left(text: str) -> bool:
        found = wellformed.read(text)
        return found.doc == {"users": users, EMAIL: "notify"} and not found.repeated

    call = {"id": "c9", "type": "function", "function": {"name": "grant", "arguments": arguments}}
    chat = {"choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
    assert left(restore_chat_completion(ctx, chat)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    item = {"type": "function_call", "id": "fc_1", "call_id": "c9", "name": "grant", "arguments": arguments}
    assert left(restore_response_item(ctx, item, remember=True)["arguments"])
    done = {"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 0}
    emitted = ResponsesStream(ctx).on_event({**done, "arguments": arguments})
    assert emitted is not None and left(emitted[-1]["arguments"])
    # Nothing to restore in a key: the text is restored as it is written, byte for byte around what changed.
    spaced = '{ "users" : { "' + fake + '" : "owner" } }'
    assert restore_response_item(ctx, {**item, "arguments": spaced})["arguments"] == spaced.replace(fake, EMAIL)
    # A key the model itself gave twice: restored as it is written, both copies kept.
    twice = '{"to": "' + fake + '", "to": "' + fake + '"}'
    assert restore_response_item(ctx, {**item, "arguments": twice})["arguments"] == twice.replace(fake, EMAIL)


def test_a_value_of_a_tool_calls_input_under_a_masked_key_is_shown_under_the_keys_fake():
    """A tool call's input is an object of the request: a key of it that is an address leaves masked, and the block of
    each value under it is shown at the path the request was sent with."""
    mediator = _mediator()
    first = _prepared(mediator, "anthropic with an API key", f"the owner is {EMAIL}")
    assert first.block_reason is None
    fake = _fake(mediator, EMAIL)
    call = {"type": "tool_use", "id": "t2", "name": "grant", "input": {"users": {EMAIL: "the owner"}}}
    messages = [
        {"role": "user", "content": "make them owner"},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "done"}]},
    ]
    raw = json.dumps({"model": "m", "max_tokens": 10, "messages": messages}).encode()
    prepared = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", API_KEY, raw)
    assert prepared.block_reason is None and EMAIL not in prepared.body.decode()
    sent = json.loads(prepared.body)["messages"][1]["content"][0]["input"]
    assert sent == {"users": {fake: "the owner"}}
    paths = json_paths(prepared.document)
    [slot] = [slot for slot, _, _ in prepared.pending if slot.kind == SlotKind.TOOL_INPUT]
    assert slot.get() == "the owner" and paths[(id(slot.container), slot.key)].endswith(f".input.users.{fake}")


def test_the_masked_keys_of_many_objects_are_renamed_each_object_looking_only_at_its_own_slots(monkeypatch):
    """A tool call's input of many objects, each with a key that is an address: each object's keys are renamed and the
    slots of its own values told, never every slot of the request for each object (as many steps as objects times
    slots, billions for a request of 32 MiB, on the identity's engine thread)."""
    from latet.gateway import mediate

    looked: list[int] = []
    renamed = mediate.rename

    def counted(obj, names, slots):
        slots = list(slots)
        looked.append(len(slots))
        return renamed(obj, names, slots)

    monkeypatch.setattr(mediate, "rename", counted)
    mediator = _mediator()
    owners = [f"owner{i}@acme.com" for i in range(200)]
    users = [{owner: f"note {i}"} for i, owner in enumerate(owners)]
    call = {"type": "tool_use", "id": "t2", "name": "grant", "input": {"users": users}}
    messages = [
        {"role": "user", "content": "make them owners"},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "done"}]},
    ]
    raw = json.dumps({"model": "m", "max_tokens": 10, "messages": messages}).encode()
    prepared = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", API_KEY, raw)
    assert prepared.block_reason is None and not any(owner in prepared.body.decode() for owner in owners)
    assert len(looked) == len(owners) and sum(looked) == len(owners)  # each object its own value's slot alone
    sent = json.loads(prepared.body)["messages"][1]["content"][0]["input"]["users"]
    assert [list(user) for user in sent] == [[_fake(mediator, owner)] for owner in owners]
    slots = [slot for slot, _, _ in prepared.pending if slot.kind == SlotKind.TOOL_INPUT]
    assert [slot.key for slot in slots] == [_fake(mediator, owner) for owner in owners]  # each told its key's name


# --- two keys that would be one -------------------------------------------------------------------------------------


def _colliding(mediator: Mediator) -> None:
    """A term whose fake is a key beside it: masked, the two would be one. The fake was drawn before, in a text where
    the other key was not (a fake drawn beside it is none of its object's other keys)."""
    _drawing(mediator, "Robert Johnson", COLLIDING)
    mediator.engine.masker.mask("Robert Johnson called")
    assert _fake(mediator, "Robert Johnson") == COLLIDING


@pytest.mark.parametrize("route", list(ROUTES))
def test_a_key_that_is_the_fake_of_another_key_is_masked_as_a_value_of_its_own(route):
    """A tool's output whose key is the fake another of its keys has: the fake, a text the client sent, is masked as a
    value of its own, as every fake a client sends is, so the two keys stay two and the request leaves."""
    mediator = _mediator(terms=[TermConfig(value="Robert Johnson", type="CLIENT")])
    _colliding(mediator)
    output = _object((COLLIDING, {"owed": "1200 EUR"}), ("Robert Johnson", {"owed": "80 EUR"}))
    prepared = _prepared(mediator, route, output)
    assert prepared.block_reason is None and b"Robert Johnson" not in prepared.body
    assert _fake(mediator, COLLIDING) not in (COLLIDING, "Robert Johnson")


@pytest.mark.parametrize("route", list(ROUTES))
def test_a_fake_drawn_for_a_key_is_none_of_the_other_keys_of_its_object(route):
    """The first fake the vault would draw for the term is a key beside it: the next one is drawn, and the request
    leaves, as it does when it is sent again (a fake drawn with the key alone as its context stopped it every time)."""
    mediator = _mediator(terms=[TermConfig(value="Robert Johnson", type="CLIENT")])
    vault = mediator.engine.vault
    drawn = vault._generate
    vault._generate = lambda etype, eclass, value, counter, *rest, **more: (  # type: ignore[method-assign]
        COLLIDING if value == "Robert Johnson" and counter == 0 else drawn(etype, eclass, value, counter, *rest, **more)
    )
    output = _object((COLLIDING, {"owed": "1200 EUR"}), ("Robert Johnson", {"owed": "80 EUR"}))
    for _ in range(2):
        prepared = _prepared(mediator, route, output)
        assert prepared.block_reason is None and b"Robert Johnson" not in prepared.body
    assert _fake(mediator, "Robert Johnson") not in (COLLIDING, "Robert Johnson")


def test_a_fake_drawn_for_a_key_of_a_tool_calls_input_is_none_of_the_other_keys():
    mediator = _mediator(terms=[TermConfig(value="Robert Johnson", type="CLIENT")])
    vault = mediator.engine.vault
    drawn = vault._generate
    vault._generate = lambda etype, eclass, value, counter, *rest, **more: (  # type: ignore[method-assign]
        COLLIDING if value == "Robert Johnson" and counter == 0 else drawn(etype, eclass, value, counter, *rest, **more)
    )
    call = {"type": "tool_use", "id": "t1", "name": "bill", "input": {COLLIDING: 1200, "Robert Johnson": 80}}
    messages = [
        {"role": "user", "content": "bill them"},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]},
    ]
    raw = json.dumps({"model": "m", "max_tokens": 10, "messages": messages}).encode()
    prepared = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", API_KEY, raw)
    assert prepared.block_reason is None and b"Robert Johnson" not in prepared.body


def test_two_keys_of_a_tool_calls_input_that_would_be_one_once_masked_stop_the_request():
    mediator = _mediator(terms=[TermConfig(value="Robert Johnson", type="CLIENT")])
    _colliding(mediator)
    call = {"type": "tool_use", "id": "t1", "name": "bill", "input": {COLLIDING: 1200, "Robert Johnson": 80}}
    messages = [
        {"role": "user", "content": "bill them"},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]},
    ]
    raw = json.dumps({"model": "m", "max_tokens": 10, "messages": messages}).encode()
    prepared = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", API_KEY, raw)
    assert (prepared.block_reason or "").startswith("masking failed: two keys of one of its JSON objects")


def test_a_key_given_twice_is_read_with_its_last_copy_and_leaves_written_anew_masked():
    mediator = _mediator()
    prepared = _prepared(mediator, "chat completions", _object((EMAIL, "first"), (EMAIL, "last")))
    assert prepared.block_reason is None and EMAIL not in prepared.body.decode()
    assert json.loads(_sent_chat(prepared)) == {_fake(mediator, EMAIL): "last"}


# --- the way back -----------------------------------------------------------------------------------------------------


def _taught(mediator: Mediator, route: str, session: str = "s-1") -> str:
    """The address masked once in the conversation (a tool's output names it): its fake."""
    first = _prepared(mediator, route, f"the owner is {EMAIL}", {"session-id": session})
    assert first.block_reason is None
    return _fake(mediator, EMAIL)


def test_a_key_the_model_writes_with_a_fake_in_it_is_restored_in_its_answer():
    mediator = _mediator()
    fake = _taught(mediator, "anthropic with an API key")
    session = mediator.engine.session("session-id:s-1")
    answer = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t9", "name": "grant", "input": {"users": {fake: {"role": "owner"}}}}],
    }
    prepared = mediator.prepare(
        ANTHROPIC, "POST", "/anthropic/v1/messages", {**API_KEY, "session-id": "s-1"},
        json.dumps({"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "go"}]}).encode(),
    )  # fmt: skip
    transform = mediator.response_transform(prepared, "application/json", None)
    assert isinstance(transform, JSONTransform) and prepared.session is session
    transform.feed(json.dumps(answer).encode())
    restored = json.loads(transform.end())
    assert restored["content"][0]["input"] == {"users": {EMAIL: {"role": "owner"}}}
    # The tools' arguments of chat completions too, a text of JSON.
    call = {"id": "c9", "type": "function", "function": {"name": "grant", "arguments": json.dumps({fake: "owner"})}}
    chat = {"choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
    back = restore_chat_completion(mediator.restore_context(session), chat)
    assert json.loads(back["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {EMAIL: "owner"}


def test_a_key_of_the_models_tool_call_restored_for_the_agent_leaves_as_the_model_wrote_it():
    """Restored for the agent, a key comes back with the conversation's next request: it leaves masked again, as the
    model wrote it (its fake in capitals, the address so restored), over chat completions (a text of JSON) and
    Anthropic (an object)."""
    mediator = _mediator()
    fake = _taught(mediator, "chat completions")
    session = mediator.engine.session("session-id:s-1")
    ctx = mediator.restore_context(session)
    arguments = json.dumps({"users": {fake.upper(): "owner"}})
    call = {"id": "c9", "type": "function", "function": {"name": "grant", "arguments": arguments}}
    restored = restore_chat_completion(
        ctx, {"choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
    )
    messages = [
        {"role": "user", "content": "make them owner"},
        restored["choices"][0]["message"],
        {"role": "tool", "tool_call_id": "c9", "content": "done"},
    ]
    assert EMAIL.upper() in json.dumps(messages)  # the agent holds the address
    raw = json.dumps({"model": "m", "messages": messages}).encode()
    prepared = mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", {**BEARER, "session-id": "s-1"}, raw)
    assert prepared.block_reason is None and EMAIL.upper() not in prepared.body.decode()
    assert json.loads(json.loads(prepared.body)["messages"][1]["tool_calls"][0]["function"]["arguments"]) == {
        "users": {fake.upper(): "owner"}
    }
    # Anthropic's tool calls are objects of the request: the same.
    tool_use = {"type": "tool_use", "id": "t9", "name": "grant", "input": {"users": {EMAIL: "owner"}}}
    history = [
        {"role": "user", "content": "make them owner"},
        {"role": "assistant", "content": [tool_use]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9", "content": "done"}]},
    ]
    raw = json.dumps({"model": "m", "max_tokens": 10, "messages": history}).encode()
    prepared = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", {**API_KEY, "session-id": "s-1"}, raw)
    assert prepared.block_reason is None and EMAIL not in prepared.body.decode()
    assert json.loads(prepared.body)["messages"][1]["content"][0]["input"] == {"users": {fake: "owner"}}


# --- names -----------------------------------------------------------------------------------------------------------


async def test_a_name_the_local_reviewer_finds_is_masked_in_the_keys_too():
    """RG3: a name the local reviewer finds among a document's values, also one of its keys, stopped the first request
    (masked in the values, in clear in the key) and left with the next. It is masked in both now, every time."""
    output = json.dumps({"Ferri": {"note": "the customer Ferri called"}})
    mediator, raw = reviewing(FakeServer({"Ferri": "person"})), reviewed_request(output, prompt="read the log")
    for _ in range(2):  # and the conversation's next request
        prepared = await send(mediator, raw)
        assert prepared.block_reason is None
        sent = json.loads(json.loads(prepared.body)["messages"][2]["content"][0]["content"])
        [key] = sent
        assert "Ferri" not in prepared.body.decode() and "Ferri" not in key
        assert sent[key]["note"] == f"the customer {key} called"  # the same fake in the key and in the value


async def test_a_name_that_is_only_a_key_is_read_by_the_local_reviewer_and_masked():
    output = json.dumps({"Ferri": {"status": "open"}, "Bianchi": {"status": "closed"}})
    server = FakeServer({"Ferri": "person", "Bianchi": "person"})
    mediator = reviewing(server)
    prepared = await send(mediator, reviewed_request(output, prompt="read the log"))
    assert prepared.block_reason is None
    assert "Ferri" not in prepared.body.decode() and "Bianchi" not in prepared.body.decode()
    assert any("Ferri" in text and "status" not in text for text in server.texts)  # read, not as a field's name


async def test_the_plugin_masks_a_name_in_a_key_as_in_a_value():
    """Claude Code's path: the plugin's tool output is masked by the same masking, its keys too, so the daemon's check
    of the request that carries it stops nothing."""
    from latet.bridge.api import Bridge

    mediator = reviewing(FakeServer({"Ferri": "person"}))
    bridge = Bridge(mediator, "token")
    output = json.dumps({"Ferri": {"note": "the customer Ferri called"}, EMAIL: {"note": "ok"}})
    data = {"session": "s1", "items": [{"text": output, "kind": "tool_result"}]}
    session, out, degraded, reviews, _ = await mediator.run_bridge(bridge._mask, data)
    await bridge._review(session, reviews, out, degraded)
    assert "blocked" not in out[0] and "Ferri" not in out[0]["text"] and EMAIL not in out[0]["text"]
    sent = [(out[0]["text"], SlotKind.TOOL_RESULT, False)]
    assert mediator.verifier.verify(sent, f"gw|{session.key}").blocking == []


def test_a_name_known_to_the_vault_or_a_term_is_masked_as_a_key():
    """A name your terms hold, or one the vault learnt from an earlier text, is masked as a key, on the gateway as in
    Claude Code's tool outputs."""
    config = Config(terms=[TermConfig(value="Project Falcon", type="PROJECT")])
    vault = Vault(ROOT_KEY)
    built = build(config, vault)
    mediator = Mediator(built.engine, built.verifier)
    vault.intern("PERSON", EntityClass.NER, "Giulia Bianchi")
    output = json.dumps({"Project Falcon": {"lead": "x"}, "Giulia Bianchi": {"role": "lead"}})
    prepared = _prepared(mediator, "responses", output)
    assert prepared.block_reason is None
    assert "Project Falcon" not in prepared.body.decode() and "Giulia Bianchi" not in prepared.body.decode()
