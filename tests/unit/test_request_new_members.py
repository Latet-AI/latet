"""A request member no profile declares, read and checked instead of refused (``notes/unknown-members-design.md`` §1):
the closed declaration's walk, after D6's string case, reads a member that carries only control values into the
inspection's ``new_members`` (the texts the final check reads with masking's detection) under its own rule, on every
profile, and refuses as today one that names a resource, a destination or a model, holds a blob, a non-finite number,
or, inside a provider's item, anything but short tokens. ``unknown_members="refuse"`` is today's walk exactly."""

from __future__ import annotations

import base64
import copy
import re
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from latet.config import Config, FieldRule
from latet.engine.build import build_masker
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.engine.verifier import Verifier
from latet.gateway import coverage, new_members
from latet.gateway.body_coverage import (
    D6,
    NEW_MEMBER_RULE,
    BodyInspection,
    BodyRefused,
    Ledger,
    declared_body_safe,
    inspect_body,
)
from latet.gateway.body_selectors import BodyGrant, BodyGrants, BodySelector, exception_rule
from latet.gateway.routes import DEFAULT_UPSTREAMS
from latet.protocols.base import BodyScan

from ..conftest import ROOT_KEY

ROUTES = {route.name: route for route in DEFAULT_UPSTREAMS}
UPSTREAMS = {
    "anthropic": ROUTES["anthropic"],
    "chat": ROUTES["openai"],
    "responses": ROUTES["openai"],
    "gemini": ROUTES["gemini"],
}
UUID = "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"
BLOB = base64.b64encode(bytes(range(60))).decode()  # 80 characters
LAUNCH_KEY = "latet_lk_" + "A" * 43
TOKEN = "a." * 32  # the longest token a provider's item takes, and no blob


def _body(protocol: str) -> dict[str, Any]:
    return {
        "anthropic": {"model": "claude-sonnet-4-6", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
        "chat": {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]},
        "responses": {"model": "gpt-4o", "input": [{"type": "message", "role": "user", "content": "hello"}]},
        "gemini": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
    }[protocol]


def _tool_use(body: dict[str, Any], member: dict[str, Any]) -> None:
    block = {"type": "tool_use", "id": "toolu_a", "name": "lookup", "input": {}}
    body["messages"].append({"role": "assistant", "content": [{**block, **member}]})


def _tool_result(body: dict[str, Any], member: dict[str, Any]) -> None:
    _tool_use(body, {})
    result = {"type": "tool_result", "tool_use_id": "toolu_a", "content": "found it"}
    body["messages"].append({"role": "user", "content": [{**result, **member}]})


def _text_block(body: dict[str, Any], member: dict[str, Any]) -> None:
    body["messages"][0]["content"] = [{"type": "text", "text": "hello", **member}]


def _web_search_result(body: dict[str, Any], member: dict[str, Any]) -> None:
    call = {"type": "server_tool_use", "id": "srvtoolu_a", "name": "web_search", "input": {}}
    result = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_a", "content": []}
    body["messages"].append({"role": "assistant", "content": [call, {**result, **member}]})


def _web_search_call(body: dict[str, Any], member: dict[str, Any]) -> None:
    call = {"type": "web_search_call", "id": "ws_a", "action": {"type": "search", "query": "weather"}}
    body["input"] = [{**call, **member}]


# Where a member is put: (protocol, the edit, the category of its holder, as a refusal names it).
PLACES: dict[str, tuple[str, Callable[[dict[str, Any], dict[str, Any]], None], str]] = {
    "anthropic top level": ("anthropic", dict.update, "top-level"),
    "anthropic tool_use block": ("anthropic", _tool_use, "message content block"),
    "anthropic tool_result block": ("anthropic", _tool_result, "message content block"),
    "anthropic text block": ("anthropic", _text_block, "message content block"),
    "chat top level": ("chat", dict.update, "top-level"),
    "chat message": ("chat", lambda body, member: body["messages"][0].update(member), "message"),
    "responses top level": ("responses", dict.update, "top-level"),
    "responses message item": ("responses", lambda body, member: body["input"][0].update(member), "input item"),
    "gemini top level": ("gemini", dict.update, "top-level"),
    "gemini part": (
        "gemini",
        lambda body, member: body["contents"][0]["parts"][0].update(member),
        "message content block",
    ),
}
# A provider's item: (protocol, the edit, whether the request is one Latet only checks).
PROVIDER_PLACES: dict[str, tuple[str, Callable[[dict[str, Any], dict[str, Any]], None], bool]] = {
    "anthropic web_search_tool_result block": ("anthropic", _web_search_result, True),
    "responses web_search_call item": ("responses", _web_search_call, False),
}
# Control members in no shape of D6's (a string, a flat list of strings), and the texts each is read as.
CONTROLS: list[tuple[dict[str, Any], list[str]]] = [
    ({"x_flag": True}, ["x_flag"]),
    ({"x_level": 3}, ["x_level", "3"]),
    ({"caller": {"type": "direct"}}, ["caller", "type", "direct"]),
    (
        {"x_ctx": {"flag": False, "level": 2.5, "modes": ["a", "b"], "caller": {"type": "direct"}}},
        ["x_ctx", "flag", "level", "2.5", "modes", "a", "b", "ab", "caller", "type", "direct"],
    ),
]


def _placed(place: str, member: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    protocol, edit = PLACES[place][:2] if place in PLACES else PROVIDER_PLACES[place][:2]
    body = copy.deepcopy(_body(protocol))
    edit(body, copy.deepcopy(member))
    return protocol, body


def inspect(protocol: str, body: dict[str, Any], *, verify: bool = False, **options: Any) -> BodyInspection:
    ledger = options.pop("ledger", None) or Ledger()
    return inspect_body(body, protocol, UPSTREAMS[protocol], BodyScan(), verify, ledger, **options)


def refused(protocol: str, body: dict[str, Any], *, verify: bool = False, **options: Any) -> BodyRefused:
    with pytest.raises(BodyRefused) as caught:
        inspect(protocol, body, verify=verify, **options)
    return caught.value


# --- control members pass -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize(("member", "texts"), CONTROLS, ids=lambda value: str(value)[:24])
@pytest.mark.parametrize("place", sorted(PLACES))
def test_a_control_member_passes_on_every_profile(place, member, texts, verify):
    protocol, body = _placed(place, member)
    inspection = inspect(protocol, body, verify=verify)
    assert inspection.problem is None
    assert NEW_MEMBER_RULE in inspection.rules and D6 not in inspection.rules
    assert inspection.new_members == texts
    assert not set(texts) & set(inspection.checked), "a new member's texts are read by the stricter check alone"


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize(("member", "texts"), CONTROLS, ids=lambda value: str(value)[:24])
@pytest.mark.parametrize("place", sorted(PLACES))
def test_strict_refuses_each_control_member_as_today(place, member, texts, verify):
    protocol, body = _placed(place, member)
    error = refused(protocol, body, verify=verify, unknown_members="refuse")
    assert str(error) == f"request body refused (unknown member in {PLACES[place][2]})"
    assert error.code == "body-unknown-member"
    assert next(iter(member)) not in str(error)


def test_a_flat_list_of_tokens_is_a_new_member_where_latet_masks():
    protocol, body = _placed("responses message item", {"x_modes": ["a", "b"]})
    inspection = inspect(protocol, body, verify=False)
    assert inspection.new_members == ["x_modes", "a", "b", "ab"] and NEW_MEMBER_RULE in inspection.rules
    error = refused(protocol, body, verify=False, unknown_members="refuse")
    assert str(error) == "request body refused (unknown member in input item)"


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize(
    "tool",
    ["file_read", "a file-based cache", "mcp__github__create_or_update_pull_request_review_comment_thread_v2"],
)
def test_a_member_naming_a_tool_passes(tool, verify):
    protocol, body = _placed("anthropic tool_use block", {"x_via": {"tool": tool, "nested": True}})
    inspection = inspect(protocol, body, verify=verify)
    assert inspection.problem is None and tool in inspection.new_members


def test_the_21_47_shape_passes_on_a_verify_only_request():
    """Claude Code 2.1.325, 2026-11-16 21:47: a member no profile declares, not a text, in a message content block of a
    request Latet only checks (three requests refused with ``body-unknown-member``)."""
    protocol, body = _placed("anthropic text block", {"x_meta": {"kind": "direct", "turns": 2, "eager": True}})
    inspection = inspect(protocol, body, verify=True)
    assert inspection.problem is None and NEW_MEMBER_RULE in inspection.rules
    assert inspection.new_members == ["x_meta", "kind", "direct", "turns", "2", "eager"]
    error = refused(protocol, body, verify=True, unknown_members="refuse")
    assert (error.code, str(error)) == (
        "body-unknown-member",
        "request body refused (unknown member in message content block)",
    )


# --- refused as today -------------------------------------------------------------------------------------------------

REFUSED: list[tuple[str, str, dict[str, Any], tuple[bool, ...]]] = [
    (
        "destination",
        "anthropic top level",
        {"mcp_servers": [{"type": "url", "url": "https://x.example/mcp"}]},
        (False, True),
    ),
    ("conversation", "anthropic top level", {"conversation": "conv_abc"}, (False,)),
    ("conversation nested", "anthropic top level", {"x_ctx": {"conversation": "conv_abc"}}, (False, True)),
    ("conversation id", "chat message", {"x_ctx": {"thread": "conv_abc"}}, (False, True)),
    ("container", "responses top level", {"container": "cntr_1"}, (False,)),
    ("container id", "responses message item", {"x_ctx": {"box": "cntr_1"}}, (False, True)),
    ("attachment blob", "anthropic tool_result block", {"attachments": [{"data": BLOB}]}, (False, True)),
    ("data uri", "gemini part", {"x_ctx": {"src": "data:image/png;base64,iVBORw0KGgo="}}, (False, True)),
    ("hex run", "anthropic tool_use block", {"x_digest": {"sha": "ab" * 35}}, (False, True)),
    ("image_url key", "chat top level", {"x_ctx": {"deep": [{"image_url": "abc"}]}}, (False, True)),
    ("not a number", "anthropic top level", {"x_ratio": float("nan")}, (False, True)),
    ("infinite", "gemini top level", {"x_ctx": {"ratio": [float("inf")]}}, (False, True)),
]


@pytest.mark.parametrize(
    ("place", "member", "verify"),
    [
        pytest.param(place, member, verify, id=f"{name}-{verify}")
        for name, place, member, verifies in REFUSED
        for verify in verifies
    ],
)
def test_a_resource_destination_blob_or_odd_number_is_refused_as_today(place, member, verify):
    protocol, body = _placed(place, member)
    error = refused(protocol, body, verify=verify)
    today = refused(protocol, copy.deepcopy(body), verify=verify, unknown_members="refuse")
    assert (error.code, str(error)) == (today.code, str(today))
    assert error.code == "body-unknown-member"
    assert str(error) == f"request body refused (unknown member in {PLACES[place][2]})"
    assert next(iter(member)) not in str(error)


# --- provider items ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("place", sorted(PROVIDER_PLACES))
def test_a_provider_item_passes_a_member_of_tokens(place):
    protocol, body = _placed(place, {"x": {"type": "direct"}})
    verify = PROVIDER_PLACES[place][2]
    inspection = inspect(protocol, body, verify=verify)
    assert inspection.problem is None and NEW_MEMBER_RULE in inspection.rules
    assert inspection.new_members == ["x", "type", "direct"]
    error = refused(protocol, body, verify=verify, unknown_members="refuse")
    assert (error.code, str(error)) == (
        "body-unknown-member-no-exception",
        "request body refused (unknown member in provider item)",
    )


@pytest.mark.parametrize(
    "member",
    [
        {"note": "a sentence of text"},
        {"x y": "a"},
        {"x": ""},
        {"x": TOKEN + "a"},
        {"x": list(range(33))},
        {"x": [TOKEN] * 32},  # 2,049 bytes with its key
        {"x1": [TOKEN] * 16, "x2": [TOKEN] * 16},  # each under the cap, the item over it
    ],
    ids=["free text", "odd key", "empty", "long token", "leaves", "bytes", "item bytes"],
)
@pytest.mark.parametrize("place", sorted(PROVIDER_PLACES))
def test_a_provider_item_refuses_free_text_odd_keys_and_oversized_members(place, member):
    protocol, body = _placed(place, member)
    error = refused(protocol, body, verify=PROVIDER_PLACES[place][2])
    assert error.code == "body-unknown-member-no-exception"
    assert str(error) == "request body refused (unknown member in provider item)"


# --- D6's string case, unchanged ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("member", "texts"),
    [
        ({"x_note": "core"}, ["x_note", "core"]),
        ({"x_parts": ["ghp_abc", "def123"]}, ["x_parts", "ghp_abc", "def123"]),
        ({"session_token": UUID}, ["session_token", UUID]),
    ],
)
@pytest.mark.parametrize("mode", ["check", "refuse"])
def test_d6_s_string_case_is_unchanged(member, texts, mode):
    """On a request Latet only checks, a string or a flat list of strings outside every provider item is read as
    today's D6 reads it: its texts checked as checked-only texts, no ``key: value`` and no join of its strings."""
    protocol, body = _placed("anthropic top level", member)
    inspection = inspect(protocol, body, verify=True, unknown_members=mode)
    assert inspection.problem is None
    assert inspection.rules == {D6} and inspection.new_members == []
    assert inspection.checked[-len(texts) :] == texts
    assert not any(": " in text or text == "ghp_abcdef123" for text in inspection.checked)


# --- what a new member is read as -------------------------------------------------------------------------------------


@pytest.mark.parametrize("verify", [False, True])
def test_a_password_under_its_name_is_read_with_the_name(verify):
    protocol, body = _placed("anthropic top level", {"auth": {"password": "hunter2"}})
    inspection = inspect(protocol, body, verify=verify)
    assert inspection.new_members == ["auth", "password", "hunter2", "password: hunter2"]


@pytest.mark.parametrize("verify", [False, True])
def test_a_control_value_is_not_read_with_its_name(verify):
    protocol, body = _placed("anthropic top level", {"caller": {"type": "direct"}})
    assert not any(": " in text for text in inspect(protocol, body, verify=verify).new_members)


def test_a_secret_split_across_strings_is_read_joined():
    protocol, body = _placed("anthropic top level", {"x_parts": ["ghp_abc", "def123"]})
    assert "ghp_abcdef123" in inspect(protocol, body, verify=False).new_members


def _stricter_check(texts: list[str]) -> list[str]:
    """What the final check stops of a new member's texts (``Verifier.verify``'s ``new_members``), by type."""
    verifier = Verifier(build_masker(Config(ner="off"), Vault(ROOT_KEY))[0])
    return [leak.etype for leak in verifier.verify([], "s", new_members=texts).blocking]


@pytest.mark.parametrize(("member", "texts"), CONTROLS, ids=lambda value: str(value)[:24])
def test_a_control_member_s_texts_pass_the_stricter_check(member, texts):
    protocol, body = _placed("anthropic tool_use block", member)
    assert _stricter_check(inspect(protocol, body, verify=True).new_members) == []


@pytest.mark.parametrize(
    ("member", "found"),
    [
        ({"x_ctx": {"n": 4111111111111111}}, "CREDIT_CARD"),
        ({"x_ctx": {"to": "emily.carter@acme.com"}}, "EMAIL"),
        ({"x_ctx": {"parts": ["4111 1111", " 1111 1111"]}}, "CREDIT_CARD"),  # a card only once joined
        ({"auth": {"password": "hunter2"}}, "PASSWORD"),  # a password only with its name
    ],
    ids=["card", "e-mail", "split card", "password"],
)
def test_a_sensitive_value_in_a_new_member_is_what_the_stricter_check_stops(member, found):
    protocol, body = _placed("anthropic top level", member)
    assert found in _stricter_check(inspect(protocol, body, verify=True).new_members)


def test_the_joined_texts_are_charged_to_the_ledger():
    joined = {"x_ctx": {"parts": ["a " * 500, "b " * 500], "password": "hunter2"}}
    plain = {"x_ctx": {"parts": ["a " * 500, 1], "other": "hunter2"}}  # the same paths, nothing joined
    ledgers = {}
    for name, member in (("joined", joined), ("plain", plain)):
        protocol, body = _placed("anthropic top level", member)
        ledgers[name] = ledger = Ledger()
        inspect(protocol, body, ledger=ledger)
    # Each value as its field is made here too, and charged (``new_members.fields``).
    made = {
        name: list(new_members.fields("x_ctx", member["x_ctx"]))
        for name, member in (("joined", joined), ("plain", plain))
    }
    fields = sum(map(len, made["joined"])) - sum(map(len, made["plain"]))
    assert ledgers["joined"].retained - ledgers["plain"].retained == 2000 + len("password: hunter2") + fields
    assert ledgers["joined"].declared_occurrences - ledgers["plain"].declared_occurrences == 2 + len(
        made["joined"]
    ) - len(made["plain"])
    protocol, body = _placed("anthropic top level", joined)
    tight = Ledger(limits=replace(coverage.LIMITS, ledger_bytes=ledgers["plain"].retained + 1000))
    assert refused(protocol, body, ledger=tight).code == "coverage-limit"


# --- a value as its field: your field rules find it -------------------------------------------------------------------


def test_each_value_is_read_as_its_field_by_its_key_and_its_paths():
    assert list(new_members.fields("x_ctx", {"tier": "platinumzx", "customer_code": "ACME-99812"})) == [
        '{"tier": "platinumzx"}',
        '{"x_ctx.tier": "platinumzx"}',
        '{"customer_code": "ACME-99812"}',
        '{"x_ctx.customer_code": "ACME-99812"}',
    ]
    assert list(new_members.fields("x_ctx", {"items": [{"sku": "AB-9981"}], "n": 12})) == [
        '{"sku": "AB-9981"}',
        '{"items[].sku": "AB-9981"}',
        '{"x_ctx.items[].sku": "AB-9981"}',
        '{"n": 12}',
        '{"x_ctx.n": 12}',
    ]
    assert list(new_members.fields("tier", "platinumzx")) == ['{"tier": "platinumzx"}']
    assert list(new_members.fields("codes", ["A1", 7])) == [
        '{"codes": "A1"}',
        '{"codes[]": "A1"}',
        '{"codes": 7}',
        '{"codes[]": 7}',
    ]
    assert list(new_members.fields("città", 'Niccolò "il" mago')) == ['{"città": "Niccolò \\"il\\" mago"}']
    assert list(new_members.fields("x", {"on": True, "none": None, "deep": [[]], "big": 10**5000})) == []


def test_a_value_as_its_field_is_capped():
    assert list(new_members.fields("note", "p" * 1000)) == ['{"note": "' + "p" * 256 + '"}']
    assert list(new_members.fields("y" * 300, 1)) == ['{"' + "y" * 256 + '": 1}']


@pytest.mark.parametrize("verify", [False, True])
def test_the_walk_reads_a_new_member_s_values_as_their_fields(verify):
    protocol, body = _placed("anthropic text block", {"x_ctx": {"tier": "platinumzx", "level": 2, "on": True}})
    inspection = inspect(protocol, body, verify=verify)
    assert inspection.new_member_fields == [
        '{"tier": "platinumzx"}',
        '{"x_ctx.tier": "platinumzx"}',
        '{"level": 2}',
        '{"x_ctx.level": 2}',
    ]
    assert inspection.new_members == ["x_ctx", "tier", "platinumzx", "level", "2", "on"]  # unchanged
    # How it was refused before the rule: what refuses it where its new members cannot be read in time.
    assert inspection.new_member_refusal == ("message content block", True)
    today = refused(protocol, copy.deepcopy(body), verify=verify, unknown_members="refuse")
    error = inspection.refuse_new_members()
    assert (error.code, str(error)) == (today.code, str(today))


def test_a_request_with_no_new_member_has_no_refusal_of_one():
    protocol, body = _placed("anthropic top level", {})
    inspection = inspect(protocol, body)
    assert inspection.new_member_refusal is None and inspection.new_member_fields == []
    with pytest.raises(ValueError, match="no new member"):
        inspection.refuse_new_members()


def test_a_provider_item_s_new_member_is_refused_where_no_exception_reaches():
    protocol, body = _placed("anthropic web_search_tool_result block", {"x": {"type": "direct"}})
    inspection = inspect(protocol, body, verify=True)
    assert inspection.new_member_refusal == ("provider item", False)
    assert inspection.refuse_new_members().code == "body-unknown-member-no-exception"


@pytest.mark.parametrize(
    ("rule", "member"),
    [
        ("tier", {"x_ctx": {"tier": "platinumzx", "customer_code": "ACME-99812"}}),  # the final review's probe
        ("customer_*", {"x_ctx": {"customer_code": "ACME-99812"}}),
        ("x_ctx.tier", {"x_ctx": {"tier": "platinumzx"}}),  # a path from the member's key
        ("customer.tier", {"x_ctx": {"customer": {"tier": "platinumzx"}}}),  # a path inside the member
        ("items[].sku", {"x_ctx": {"items": [{"sku": "AB-99812"}]}}),  # one no JSON text shows the fields detector
        ("tier", {"tier": 4242}),  # a number, the member itself
    ],
    ids=["key", "star", "path from the member", "path inside it", "array path", "number"],
)
@pytest.mark.parametrize("verify", [False, True])
def test_a_field_rule_finds_a_value_of_a_new_member(rule, member, verify):
    """Strict data mode reads no new member (it covers the tools, files and hosts it names); your field rules do, by a
    value's key or its path, as masking finds it in a JSON document."""
    masker = build_masker(Config(ner="off", fields=[FieldRule(name=rule, type="TIER")]), Vault(ROOT_KEY))[0]
    protocol, body = _placed("anthropic top level", member)
    inspection = inspect(protocol, body, verify=verify)
    found = Verifier(masker).verify(
        [], "s", new_members=inspection.new_members, new_member_fields=inspection.new_member_fields
    )
    assert {(leak.etype, leak.kind) for leak in found.blocking} == {("TIER", SlotKind.NEW_MEMBER)}
    clean = inspect(*_placed("anthropic top level", {"x_ctx": {"mode": "compact", "level": 2}}), verify=verify)
    assert (
        Verifier(masker).verify([], "s", new_members=clean.new_members, new_member_fields=clean.new_member_fields)
    ).blocking == []


# --- a long key, and what the rule reads of one request ---------------------------------------------------------------


def test_a_key_longer_than_the_bound_is_refused_as_today():
    assert new_members.refusal("Q" * new_members.KEY_CHARS, 1, provider=False) is None
    assert new_members.refusal("Q" * (new_members.KEY_CHARS + 1), 1, provider=False) == "length"
    assert new_members.refusal("x", {"y": [{"Q" * 257: 1}]}, provider=False) == "length"
    protocol, body = _placed("anthropic top level", {"x_ctx": {"Q" * 257: 1}})
    error = refused(protocol, body, verify=True)
    today = refused(protocol, copy.deepcopy(body), verify=True, unknown_members="refuse")
    assert (error.code, str(error)) == (today.code, str(today))
    assert str(error) == "request body refused (unknown member in top-level)"


@pytest.mark.timing
def test_a_run_of_capitals_is_read_for_its_words_in_linear_time():
    started = time.monotonic()
    assert not new_members._names("Q" * 64_000)
    assert not new_members._names("Q" * 64_000 + "sa")
    assert time.monotonic() - started < 0.5
    assert new_members._names("imageURIs") and new_members._names("URLs") and new_members._names("x_URLs2")


@pytest.mark.timing
@pytest.mark.parametrize("length", [new_members.KEY_CHARS, 32_000], ids=["at the bound", "past it"])
def test_a_long_key_costs_the_walks_little(length):
    """A key of capitals with no plural after it, at the bound (read whole) and far past it (refused): the launch-key
    screen and the walk, under `check`, well under a second (32,000 characters took 7 s)."""
    key = "Q" * (length - 2) + "sa"
    protocol, body = _placed("anthropic top level", {"x_ctx": {key: 1}})
    started = time.monotonic()
    assert declared_body_safe(body, protocol, UPSTREAMS[protocol], BodyScan(), Ledger())
    if length <= new_members.KEY_CHARS:
        assert NEW_MEMBER_RULE in inspect(protocol, body, verify=True).rules
    else:
        assert refused(protocol, body, verify=True).code == "body-unknown-member"
    assert time.monotonic() - started < 0.5


def _many(count: int) -> dict[str, Any]:
    """A member read as ``4 * count + 3`` distinct texts: its two keys, each value, the values joined, and each value
    as three fields (``values``, ``values[]``, ``x_many.values[]``)."""
    return {"x_many": {"values": [f"v{index}" for index in range(count)]}}


@pytest.mark.timing
def test_a_request_s_new_members_up_to_the_texts_it_reads_pass_in_bounded_time():
    count = (new_members.TEXTS - 3) // 4
    protocol, body = _placed("responses top level", _many(count))
    started = time.monotonic()
    assert declared_body_safe(body, protocol, UPSTREAMS[protocol], BodyScan(), Ledger())
    inspection = inspect(protocol, body)
    assert time.monotonic() - started < 1
    assert len(set(inspection.new_members) | set(inspection.new_member_fields)) == 4 * count + 3 <= new_members.TEXTS


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize(
    "member",
    [
        _many((new_members.TEXTS - 3) // 4 + 1),  # past them by a value's texts
        {"x_ctx": {"note": "z" * new_members.BYTES}},  # one byte past them, with its key's
        {"x_a": {"note": "z" * (new_members.BYTES // 2)}, "x_b": {"note": "y" * (new_members.BYTES // 2)}},
    ],
    ids=["texts", "bytes", "bytes of two members"],
)
def test_new_members_past_what_the_rule_reads_of_a_request_refuse_it_as_today(member, verify):
    protocol, body = _placed("anthropic top level", member)
    error = refused(protocol, body, verify=verify)
    today = refused(protocol, copy.deepcopy(body), verify=verify, unknown_members="refuse")
    assert (error.code, str(error)) == (today.code, str(today))
    assert (error.code, str(error)) == ("body-unknown-member", "request body refused (unknown member in top-level)")


def test_the_same_texts_in_many_members_count_once():
    """A member an agent adds to every content block of a conversation, the same value in each: its texts count once."""
    body = copy.deepcopy(_body("anthropic"))
    body["messages"] = [
        {"role": "user", "content": [{"type": "text", "text": f"turn {index}", "x_render": {"mode": "compact"}}]}
        for index in range(new_members.TEXTS)
    ]
    inspection = inspect("anthropic", body, verify=True)
    assert NEW_MEMBER_RULE in inspection.rules
    assert len(set(inspection.new_members) | set(inspection.new_member_fields)) == 5


_DOCS = Path(__file__).resolve().parents[2] / "docs" / "src" / "content" / "docs"


@pytest.mark.parametrize("lang", ["", "it", "de", "fr", "es"])
def test_the_upgrading_page_names_what_the_rule_reads_of_a_request(lang):
    """The page that explains the rule says its bounds as the code has them, in each language."""
    from latet.gateway import mediate

    page = (_DOCS / lang / "getting-started" / "upgrading-to-0-11.md").read_text(encoding="utf-8")
    words = " ".join(page.split())
    digits = re.sub(r"(?<=\d)[,. ](?=\d{3}(?!\d))", "", words)  # 4,096 4.096 4 096: 4096
    assert f" {new_members.KEY_CHARS} " in digits
    assert f" {new_members.TEXTS} " in digits
    assert re.search(rf" {new_members.BYTES // 1024} Ki[Bo] ", digits)
    assert f" {mediate.DOCUMENT_READ_SECONDS:g} " in digits


@pytest.mark.timing
def test_a_huge_array_of_tokens_is_a_coverage_limit_in_bounded_time():
    protocol, body = _placed("anthropic top level", {"x_tokens": ["a"] * 200_000})
    started = time.monotonic()
    error = refused(protocol, body, verify=False)
    assert error.code == "coverage-limit"
    assert time.monotonic() - started < 5


# --- what wins over the new rule, and where it reaches ---------------------------------------------------------------


@pytest.mark.parametrize("mode", ["check", "refuse"])
def test_an_approved_exception_wins_over_the_new_rule(mode):
    grants = BodyGrants((BodyGrant("rx-1", (BodySelector("/x_flag", "boolean"),)),))
    protocol, body = _placed("anthropic top level", {"x_flag": True})
    inspection = inspect(protocol, body, verify=False, grants=grants, unknown_members=mode)
    assert exception_rule("rx-1") in inspection.rules and inspection.exceptions == {"rx-1"}
    assert NEW_MEMBER_RULE not in inspection.rules and inspection.new_members == []


def test_a_frame_s_control_member_passes_and_a_refused_one_takes_no_exception():
    """A WebSocket frame takes no request exception (``excepted=False``): the new rule reads it all the same."""
    protocol, body = _placed("responses top level", {"x_flag": True})
    assert NEW_MEMBER_RULE in inspect(protocol, body, excepted=False).rules
    protocol, body = _placed("responses top level", {"x_ctx": {"url": "https://x.example"}})
    assert refused(protocol, body, excepted=False).code == "body-unknown-member-no-exception"


@pytest.mark.parametrize(
    ("member", "safe"),
    [
        ({"x_ctx": {"note": LAUNCH_KEY}}, False),
        ({"x_ctx": {"parts": [LAUNCH_KEY[:29], LAUNCH_KEY[29:]]}}, False),  # whole only once joined
        ({"x_ctx": {"note": "core", "level": 3}}, True),
    ],
    ids=["nested", "joined", "clean"],
)
def test_the_launch_key_screen_reads_a_new_member(member, safe):
    protocol, body = _placed("anthropic top level", member)
    assert declared_body_safe(body, protocol, UPSTREAMS[protocol], BodyScan(), Ledger()) is safe


def test_strict_leaves_the_launch_key_screen_as_today():
    """Today's screen passes over an unknown member that is no string (the walk refuses it after)."""
    protocol, body = _placed("anthropic top level", {"x_ctx": {"note": LAUNCH_KEY}})
    assert declared_body_safe(body, protocol, UPSTREAMS[protocol], BodyScan(), Ledger(), unknown_members="refuse")
    assert refused(protocol, body, unknown_members="refuse").code == "body-unknown-member"


@pytest.mark.parametrize("mode", ["check", "refuse"])
def test_every_unknown_member_s_path_is_observed(mode):
    """What a check of agents' requests lists by name (never a refusal, a rule or a log: R-79)."""
    seen: list[tuple[str | int, ...]] = []
    protocol, body = _placed("anthropic tool_use block", {"caller": {"type": "direct"}})
    if mode == "check":
        inspect(protocol, body, observe=seen.append)
    else:
        refused(protocol, body, unknown_members=mode, observe=seen.append)
    assert seen == [("messages", 1, "content", 0, "caller")]
    seen.clear()
    protocol, body = _placed("anthropic top level", {"x_ctx": {"url": "https://x.example"}})
    refused(protocol, body, unknown_members=mode, observe=seen.append)
    assert seen == [("x_ctx",)]


def test_an_unknown_mode_is_refused():
    protocol, body = _placed("anthropic top level", {})
    with pytest.raises(ValueError, match="unknown members"):
        inspect(protocol, body, unknown_members="loose")


# --- the classifier ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "xhigh",
        "clear_thinking_20251015",
        UUID,
        "memory_consolidation:" + UUID,
        "0123456789abcdef0123456789abcdef01234567",  # a commit, 40 hex digits
        "see the notes in the profile",
        "input data: 5 rows",
        "src/models/user.py",
        "~/projects/app",
        "file_read",  # a tool's name, no file's id
        "a file-based cache",
        "vs_code",
        "upload_mode",
        "skill_level",
        "mcp__github__create_or_update_pull_request_review_comment_thread_v2",  # 67 characters, no blob
        "// a comment",
        "a/b//c",
        3,
        -2.5,
        True,
        None,
        {"type": "direct"},
        ["a", "b"],
        {"profile": "fast", "outsourcing": 1, "modeling": "x", "filed": True},  # no key word of a resource
    ],
)
def test_a_control_value_is_readable(value):
    assert new_members.refusal("x_ctx", value, provider=False) is None


@pytest.mark.parametrize(
    ("key", "value", "category"),
    [
        ("x", "https://x.example/mcp", "destination"),
        ("x", "see HTTP://X.EXAMPLE", "destination"),
        ("x", "wss://x.example", "destination"),
        ("x", "gs://bucket/object", "destination"),
        ("x", "s3://bucket/key", "destination"),
        ("x", "data:image/png;base64,AA", "destination"),
        ("x", "mailto:a@b.example", "destination"),
        ("x", "file:notes.txt", "destination"),
        ("x", "urn:isbn:0451450523", "destination"),
        ("x", "//evil.example/mcp", "destination"),  # relative to the scheme
        ("x", "at //localhost:8080", "destination"),
        ("x", BLOB, "blob"),
        ("x", "ab" * 24, "blob"),
        ("x", "A" * 64, "blob"),
        ("x", 10**60, "blob"),
        ("x", "conv_abc", "resource"),
        ("x", "file-BK7bzQj3FfZFXr7DbL6xJwfo", "resource"),
        ("x", "file_011CNha8iCJcU1wXNR6q4V8w", "resource"),
        ("x", "skill_01AbCdEfGhIjKlMnOpQrStUv", "resource"),
        ("x", "upload_abc123DEF456ghi789JKL0", "resource"),
        ("x", "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-4-6", "resource"),
        ("x", "arn:aws-us-gov:s3:::bucket", "resource"),
        ("x", "publishers/google/models/gemini-2.5-flash", "resource"),
        ("x", "resp_abc", "resource"),
        ("x", "cntr_1", "resource"),
        ("x", "the files/abc", "resource"),
        ("x", "cachedContents/abc", "resource"),
        ("x", "models/gemini-2.5-flash", "resource"),
        ("x", "projects/p1/locations/us-central1/endpoints/e1", "resource"),
        ("x", "ft:gpt-4o:acme::abc", "resource"),
        ("x", "msgbatch_01abc", "resource"),
        ("mcp_servers", [], "resource"),
        ("imageUrl", "x", "resource"),
        ("MCPServers", 1, "resource"),
        ("sources", 1, "resource"),
        ("x_model2", "a", "resource"),
        ("mcp:server/name", 1, "resource"),
        ("URLs", 1, "resource"),
        ("imageURIs", 1, "resource"),
        ("x", {"fileId": "a"}, "resource"),
        ("x", [{"deep": {"attachments": []}}], "resource"),
        ("x", {"webhook": "hooks.x.example"}, "resource"),
        ("x", float("nan"), "number"),
        ("x", [float("-inf")], "number"),
        ("x", object(), "shape"),
        ("x", {1: "a"}, "shape"),
        ("x", ("a", "b"), "shape"),
    ],
)
def test_the_classifier_refuses_by_a_fixed_category(key, value, category):
    assert new_members.refusal(key, value, provider=False) == category
    assert new_members.refusal(key, value, provider=True) == category


@pytest.mark.parametrize(
    ("key", "value", "readable"),
    [
        ("x", {"type": "direct"}, True),
        ("x", list(range(32)), True),
        ("x", 3.5, True),
        ("x", "a.b:c-d_e", True),
        ("x", "a sentence", False),
        ("x y", 1, False),
        ("x", "", False),
        ("x", TOKEN + "a", False),
        ("x", list(range(33)), False),
        ("x", [TOKEN] * 32, False),
        ("é", 1, False),
    ],
)
def test_a_provider_item_takes_short_tokens_alone(key, value, readable):
    assert (new_members.refusal(key, value, provider=True) is None) is readable
    if not readable:
        assert new_members.refusal(key, value, provider=True) == "provider item"


def test_the_texts_in_reading_order():
    assert new_members.texts("x_ctx", {"flag": False, "level": 2.5, "modes": [1, "a", "b"], "caller": {}}) == [
        "x_ctx",
        "flag",
        "level",
        "2.5",
        "modes",
        "1",
        "a",
        "b",
        "ab",
        "caller",
    ]
    assert new_members.texts("password", "hunter2") == ["password", "hunter2", "password: hunter2"]
    assert new_members.texts("x_secret_token", 12345) == ["x_secret_token", "12345", "x_secret_token: 12345"]
    assert new_members.texts("x", ["a"]) == ["x", "a"]
    assert new_members.texts("x", [True, None]) == ["x"]


def test_the_joins_are_capped():
    assert new_members.texts("password", "p" * 1000)[-1] == "password: " + "p" * 256
    assert new_members.texts("y" * 300 + "_password", "v")[-1] == ("y" * 300 + "_password")[-256:] + ": v"
    assert new_members.texts("x", ["a" * 3000, "b" * 3000])[-1] == "a" * 3000 + "b" * 1096
