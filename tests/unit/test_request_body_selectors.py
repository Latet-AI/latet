"""P2-6: how an exception names the body members it admits (S-M7, R-42) and follows a revised profile (R-100).

A body selector is an exact JSON pointer whose only wildcard is an array index, at most 256 characters and 32
segments, typed by the value it names: no key wildcard, no parent object, no regex, never a credential member and
never a member the profile declares or refuses by its kind. These tests read the grammar alone, the body walk through
the request's own preparation (``Mediator.prepare_http``) and the production gateway."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from latet.config import Config, IdentityConfig, RequestException
from latet.gateway import body_selectors, mediate, request_profiles
from latet.gateway.body_selectors import (
    BodyGrant,
    BodyGrants,
    BodyRevision,
    BodySelector,
    SelectorRefused,
    carries_over,
    exception_rule,
)
from latet.gateway.mediate import Mediator, Prepared
from latet.gateway.outcome import Block
from latet.protection import loosened

from .test_request_body_coverage import CASES, EMAIL, prepare
from .test_request_coverage import gateway
from .test_request_outcomes import _mediator

LAUNCH_KEY = "latet_lk_" + "A" * 43
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
CHAT = CASES[1]
ANTHROPIC = CASES[0]
RESPONSES = CASES[3]


def selector(pointer: str, kind: str = "string") -> BodySelector:
    return BodySelector(pointer, kind)


def grant(*pointers: str | tuple[str, str], exception: str = "rx-1") -> BodyGrant:
    return BodyGrant(exception, tuple(selector(*p) if isinstance(p, tuple) else selector(p) for p in pointers))


@pytest.fixture
def mediator() -> Iterator[Mediator]:
    made = _mediator()
    yield made
    made.close()


@pytest.fixture
def granted(monkeypatch) -> Callable[..., None]:
    """The exceptions in force for the next preparations: their body selectors reach the body walk where the policy
    hands them over (every ``inspect_body`` and ``declared_body_safe`` of ``mediate``)."""
    held: dict[str, BodyGrants | None] = {"grants": None}
    real_inspect, real_declared = mediate.inspect_body, mediate.declared_body_safe

    # In place of the grants the request's route hands mediate (none here: these routes have no exceptions in force).
    def inspect(*args: Any, **kwargs: Any):
        return real_inspect(*args, **{**kwargs, "grants": held["grants"]})

    def declared(*args: Any, **kwargs: Any):
        return real_declared(*args, **{**kwargs, "grants": held["grants"]})

    monkeypatch.setattr(mediate, "inspect_body", inspect)
    monkeypatch.setattr(mediate, "declared_body_safe", declared)

    def give(*grants: BodyGrant) -> None:
        held["grants"] = BodyGrants(grants)

    return give


def chat(**members: Any) -> dict[str, Any]:
    body = copy.deepcopy(CHAT[2])
    body.update(members)
    return body


def run(mediator: Mediator, case: tuple[str, str, Any], body: Any, *, verify: bool = False) -> Prepared:
    return prepare(mediator, case[0], case[1], body, verify=verify)


def refused(result: Prepared) -> bool:
    return result.block_reason is not None


# --- the grammar ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pointer", "kind", "segments"),
    [
        ("/x_context", "string", ("x_context",)),
        ("/x_context/team", "number", ("x_context", "team")),
        ("/messages/*/x_trace", "boolean", ("messages", None, "x_trace")),
        ("/x_list/0/flag", "null", ("x_list", "0", "flag")),
        ("/a~1b/c~0d", "string", ("a/b", "c~d")),
        ("/[0-9]+/.?", "string", ("[0-9]+", ".?")),  # read as the literal keys they are, never as a pattern
        ("/" + "/".join(["k"] * 32), "string", ("k",) * 32),
        ("/" + "k" * 255, "string", ("k" * 255,)),
    ],
)
def test_a_body_selector_is_an_exact_json_pointer(pointer, kind, segments):
    made = selector(pointer, kind)
    assert (made.pointer, made.kind, made.segments) == (pointer, kind, segments)


@pytest.mark.parametrize(
    ("pointer", "kind", "reason"),
    [
        ("", "string", "pointer"),  # the whole body: no parent object
        ("/", "string", "empty segment"),
        ("x_context", "string", "pointer"),
        ("/a//b", "string", "empty segment"),
        ("/a/", "string", "empty segment"),
        ("/*", "string", "top-level wildcard"),  # the body is an object: an index never names its member
        ("/a/b*", "string", "key wildcard"),
        ("/a/**", "string", "key wildcard"),
        ("/a/*b", "string", "key wildcard"),
        ("/a/~2", "string", "escape"),
        ("/a/~", "string", "escape"),
        ("/a\x00b", "string", "character"),
        # A right-to-left override would disguise what the owner approves.
        ("/a\N{RIGHT-TO-LEFT OVERRIDE}b", "string", "character"),
        ("/a\N{NO-BREAK SPACE}b", "string", "character"),
        ("/" + "k" * 256, "string", "length"),
        ("/" + "/".join(["k"] * 33), "string", "segments"),
        ("/x_context", "object", "kind"),  # no parent object
        ("/x_context", "array", "kind"),
        ("/x_context", "any", "kind"),
        ("/x_context", "String", "kind"),
        (1, "string", "shape"),
        (None, "string", "shape"),
        ("/x_context", None, "shape"),
    ],
)
def test_selector_grammar_bounds(pointer, kind, reason):
    with pytest.raises(SelectorRefused) as raised:
        BodySelector(pointer, kind)
    assert raised.value.reason == reason
    assert str(raised.value) == f"body selector refused ({reason})"


def test_a_refused_selector_never_repeats_its_text():
    with pytest.raises(SelectorRefused) as raised:
        BodySelector("/CANARY_" + "k" * 300, "string")
    assert "CANARY" not in str(raised.value)


@pytest.mark.parametrize(
    "pointer",
    [
        "/api_key",
        "/apiKey",
        "/APIKey",
        "/x/access_token",
        "/auth",
        "/headers/Authorization",
        "/client_secret",
        "/password",
        "/data/*/refreshToken",
        "/thinking/signature",
        "/cookie",
        "/x-goog-api-key",
        "/x_context/bearer",
        "/user_credentials",
    ],
)
def test_a_selector_never_names_a_credential_member(pointer):
    with pytest.raises(SelectorRefused) as raised:
        BodySelector(pointer, "string")
    assert raised.value.reason == "credential member"


@pytest.mark.parametrize("pointer", ["/author", "/keyboard_layout", "/x_context/monkey"])
def test_a_word_that_only_contains_a_credential_marker_is_no_credential(pointer):
    assert BodySelector(pointer, "string").segments


def test_an_exception_names_at_most_64_distinct_selectors():
    BodyGrant("rx-1", tuple(selector(f"/x_{n}") for n in range(64)))
    with pytest.raises(SelectorRefused, match="selectors"):
        BodyGrant("rx-1", tuple(selector(f"/x_{n}") for n in range(65)))
    with pytest.raises(SelectorRefused, match="selectors"):
        BodyGrant("rx-1", ())
    with pytest.raises(SelectorRefused, match="selectors"):
        BodyGrant("rx-1", (selector("/x_a"), selector("/x_a")))
    with pytest.raises(SelectorRefused, match="selectors"):
        BodyGrant("rx-1", ("/x_a",))  # type: ignore[arg-type]  # a text is no selector: only BodySelector parses


def test_the_exceptions_for_one_route_are_bounded_and_distinct():
    BodyGrants(tuple(BodyGrant(f"rx-{n}", (selector(f"/x_{n}"),)) for n in range(128)))
    with pytest.raises(SelectorRefused, match="exceptions"):
        BodyGrants(tuple(BodyGrant(f"rx-{n}", (selector(f"/x_{n}"),)) for n in range(129)))
    with pytest.raises(SelectorRefused, match="exceptions"):
        BodyGrants((grant("/x_a"), grant("/x_b")))  # the same exception twice


@pytest.mark.parametrize("exception", ["", "a b", "x" * 65, "rx/1", 7, "-rx"])
def test_an_exception_id_is_an_internal_bounded_name(exception):
    with pytest.raises(SelectorRefused, match="exception"):
        BodyGrant(exception, (selector("/x_a"),))


# --- what one selector admits -----------------------------------------------------------------------------------------


def admits(pointers: tuple[str | tuple[str, str], ...], path: tuple[str | int, ...], value: Any):
    return BodyGrants((grant(*pointers),)).admits(path, value)


def test_a_sibling_key_under_a_selected_parent_blocks(mediator, granted):
    granted(grant("/x_context/team", "/messages/*/x_meta/team"))
    assert not refused(run(mediator, CHAT, chat(x_context={"team": "core"})))
    sibling = chat(x_context={"team": "core", "sibling_CANARY": "core"})
    result = run(mediator, CHAT, sibling)
    assert refused(result)
    assert "sibling_CANARY" not in (result.block_reason or "")
    nested = chat()
    nested["messages"][0]["x_meta"] = {"team": "core"}
    assert not refused(run(mediator, CHAT, nested))
    nested["messages"][0]["x_meta"]["sibling_CANARY"] = "core"
    assert refused(run(mediator, CHAT, nested))
    # The member's own siblings stay what they were: unknown, so refused.
    assert refused(run(mediator, CHAT, chat(x_context={"team": "core"}, x_other="core")))
    assert admits(("/x_context/team",), ("x_context",), {"team": "core", "sibling": "core"}) is None


def test_an_array_index_wildcard_never_matches_an_object_key():
    assert admits(("/x_list/*",), ("x_list",), ["a", "b"]) is not None
    assert admits(("/x_list/*",), ("x_list",), []) is not None
    assert admits(("/x_list/*",), ("x_list",), {"0": "a"}) is None
    assert admits(("/x_list/*",), ("x_list",), {"*": "a"}) is None
    assert admits(("/x_list/*/name",), ("x_list",), [{"name": "a"}, {"name": "b"}]) is not None
    assert admits(("/x_list/*/name",), ("x_list",), [{"name": "a"}, {"name": "b", "other": "c"}]) is None


def test_an_exact_index_names_that_element_alone():
    assert admits(("/x_list/0",), ("x_list",), ["a"]) is not None
    assert admits(("/x_list/0",), ("x_list",), ["a", "b"]) is None
    assert admits(("/x_list/1",), ("x_list",), ["a", "b"]) is None
    assert admits(("/x_list/0", "/x_list/1"), ("x_list",), ["a", "b"]) is not None
    assert admits(("/x_list/01",), ("x_list",), ["a", "b"]) is None  # not RFC 6901's form of an index
    assert admits(("/x_map/0",), ("x_map",), {"0": "a"}) is not None  # RFC 6901: an object's member "0"


def test_a_selector_never_admits_a_parent_object_or_a_deeper_level(mediator, granted):
    assert admits(("/x_context",), ("x_context",), "core") is not None
    assert admits(("/x_context",), ("x_context",), {"team": "core"}) is None
    assert admits(("/x_context",), ("x_context",), ["core"]) is None
    assert admits(("/x_context/team",), ("x_context",), {"team": {"deep": "core"}}) is None
    assert admits(("/x_context/team",), ("x_context",), {"team": ["core"]}) is None
    assert admits(("/x_context/team",), ("x_context",), {}) is not None  # nothing in it but what is named
    deep: Any = "core"
    for _ in range(60):
        deep = {"team": deep}
    assert admits(("/x_context/team/team",), ("x_context",), deep) is None
    granted(grant("/x_context"))
    assert refused(run(mediator, CHAT, chat(x_context={"team": "core"})))


@pytest.mark.parametrize(
    ("kind", "good", "bad"),
    [
        ("string", "core", [1, True, None, 1.5]),
        ("number", 3, ["3", True, False, None]),
        ("number", 2.5, [float("nan"), float("inf"), -float("inf")]),
        ("boolean", True, [1, 0, "true", None]),
        ("null", None, ["", 0, False]),
    ],
)
def test_a_selector_is_typed_by_the_value_it_names(kind, good, bad):
    assert admits((("/x_value", kind),), ("x_value",), good) is not None
    for value in bad:
        assert admits((("/x_value", kind),), ("x_value",), value) is None, value


def test_an_admitted_member_is_checked_never_masked(mediator, granted):
    granted(grant("/x_context/team", ("/x_context/size", "number")))
    result = run(mediator, CHAT, chat(x_context={"team": "core", "size": 4}))
    assert not refused(result)
    assert {"x_context", "team", "core", "size", "4"} <= set(result.body_checked)
    assert result.masked_count == 0
    for leaked in ({"team": "write to " + EMAIL, "size": 1}, {"team": "core", "size": 4111111111111111}):
        refusal = run(mediator, CHAT, chat(x_context=leaked))
        assert refused(refusal), leaked  # the final check read the text and the number, and stopped them
        assert "x_context" not in (refusal.block_reason or "")


def test_an_admitted_member_names_its_exception(mediator, granted):
    granted(grant("/x_context/team", exception="rx-team"), grant("/x_context/size", exception="rx-size"))
    one = run(mediator, CHAT, chat(x_context={"team": "core"}))
    assert exception_rule("rx-team") in one.body_rules
    assert exception_rule("rx-size") not in one.body_rules
    both = run(mediator, CHAT, chat(x_context={"team": "core", "size": "4"}))
    assert {exception_rule("rx-team"), exception_rule("rx-size")} <= set(both.body_rules)
    assert exception_rule("rx-team") == "exception rx-team"
    plain = run(mediator, CHAT, chat())
    assert not any(rule.startswith("exception ") for rule in plain.body_rules)


def test_an_exception_is_consulted_only_where_the_profile_would_refuse(mediator, granted):
    """On a verify-only profile D6 already passes an unknown text member: no exception is named for it."""
    granted(grant("/x_note", "/x_context/team"))
    body = copy.deepcopy(ANTHROPIC[2])
    body["x_note"] = "core"
    flat = run(mediator, ANTHROPIC, body, verify=True)
    assert not refused(flat) and exception_rule("rx-1") not in flat.body_rules
    body["x_context"] = {"team": "core"}
    nested = run(mediator, ANTHROPIC, body, verify=True)
    assert not refused(nested) and exception_rule("rx-1") in nested.body_rules


@pytest.mark.parametrize(
    ("case", "pointer", "edit", "reason"),
    [
        # A member the profile declares keeps its own treatment: a model outside every grammar stays refused.
        (CHAT, "/model", lambda body: body.update(model="not a model id"), "(model resource)"),
        # A control outside its enumeration stays refused.
        (CHAT, "/service_tier", lambda body: body.update(service_tier="x_tier"), "(control)"),
        # Inside a provider's own item (M2's domain): refused by its kind.
        (
            ANTHROPIC,
            "/messages/*/content/*/x_note",
            lambda body: body["messages"].append(
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "x_note": "core"}]}
            ),
            "(unknown member in provider item)",
        ),
        (
            RESPONSES,
            "/input/*/x_note",
            lambda body: body.update(input=[{"type": "reasoning", "summary": [], "x_note": "core"}]),
            "(unknown member in provider item)",
        ),
        # An inline image's source: a provider holder.
        (
            ANTHROPIC,
            "/messages/*/content/*/source/x_note",
            lambda body: body["messages"].append(
                {
                    "role": "user",
                    "content": [{"type": "image", "source": {"type": "base64", "x_note": "core"}}],
                }
            ),
            "(unknown member in inline image)",
        ),
        # A block of a kind the profile does not know: refused before any member is read.
        (
            ANTHROPIC,
            "/messages/*/content/*/x_note",
            lambda body: body["messages"].append({"role": "user", "content": [{"type": "x_block", "x_note": "c"}]}),
            "of a kind Latet does not read",
        ),
    ],
)
def test_a_selector_never_reaches_a_declared_member_or_one_the_profile_refuses_by_kind(
    mediator, granted, case, pointer, edit, reason
):
    granted(grant(pointer))
    body = copy.deepcopy(case[2])
    edit(body)
    result = run(mediator, case, body)
    assert reason in (result.block_reason or "")
    assert exception_rule("rx-1") not in result.body_rules
    if "unknown member in" in reason:  # no exception reaches it: its code says so, and the dashboard offers none (I-8)
        assert result.envelope_refusal == "body-unknown-member-no-exception"


def test_the_same_selector_admits_the_member_on_a_readable_block(mediator, granted):
    granted(grant("/messages/*/content/*/x_note"))
    body = copy.deepcopy(ANTHROPIC[2])
    body["messages"][0]["content"] = [{"type": "text", "text": "hello", "x_note": "core"}]
    result = run(mediator, ANTHROPIC, body)
    assert not refused(result)
    assert exception_rule("rx-1") in result.body_rules


def test_no_grant_changes_nothing(mediator, granted):
    body = chat(x_context={"team": "core"})
    granted()
    assert refused(run(mediator, CHAT, body))
    granted(grant("/x_elsewhere/team"))
    assert refused(run(mediator, CHAT, body))
    assert not BodyGrants(())
    assert BodyGrants((grant("/x_a"),))


def test_an_admitted_member_is_screened_for_a_launch_credential(mediator, granted):
    granted(grant("/x_context/team"))
    result = run(mediator, CHAT, chat(x_context={"team": "key " + LAUNCH_KEY}))
    assert refused(result)
    assert LAUNCH_KEY not in (result.block_reason or "")


def test_a_launch_credential_in_the_copy_not_read_of_an_admitted_member_is_refused(mediator, granted):
    """A key given twice: the walk reads its last copy, which the exception admits; the first copy, never read and
    never sent, would still stay in what is recorded of the request's bytes, so it is screened like the rest."""
    granted(grant("/x_context/team"))
    body = json.dumps(chat()).encode()[:-1]
    clean = body + b', "x_context": {"team": "core"}}'
    assert not refused(run(mediator, CHAT, clean))
    twice = body + b', "x_context": {"team": "key ' + LAUNCH_KEY.encode() + b'"}, "x_context": {"team": "core"}}'
    result = run(mediator, CHAT, twice)
    assert refused(result)
    assert LAUNCH_KEY.encode() not in result.body
    assert LAUNCH_KEY not in (result.block_reason or "")


def test_the_walk_refuses_a_member_whose_holder_it_cannot_place():
    """A holder outside the document (never given by the walk) finds no path: nothing is admitted."""
    from latet.gateway import body_coverage

    reader = body_coverage._Body.__new__(body_coverage._Body)
    reader.grants, reader.root, reader.parents = BodyGrants((grant("/x_context/team"),)), {"a": {}}, None
    assert reader._path({}) is None


# --- through the production gateway -----------------------------------------------------------------------------------


def test_a_sibling_key_under_a_selected_parent_is_never_sent(granted):
    granted(grant("/x_context/team"))
    with gateway() as (client, transport, _made, _mediator):
        response = client.post(CHAT[1], json=chat(x_context={"team": "core", "sibling_CANARY": "core"}))
        assert response.status_code == 400
        assert "sibling_CANARY" not in response.text
        assert not transport.sent


def test_an_exception_the_rules_in_force_do_not_name_sends_nothing(granted, monkeypatch):
    """An admitted member names its exception in the decision's evidence: a decision the rules in force do not list
    is not minted, so a Forward can never hide an exception, and a revoked one stops at once."""
    from latet.gateway import forward

    made = []
    real = forward._Turn

    def turn():
        item = real()
        made.append(item)
        return item

    monkeypatch.setattr(forward, "_Turn", turn)
    granted(grant("/x_context/team"))
    with gateway() as (client, transport, _made, _mediator):
        response = client.post(CHAT[1], json=chat(x_context={"team": "core"}))
        assert response.status_code == 400
        assert not transport.sent
    assert made and all(type(item.decision) is Block for item in made)
    # The same request without the member leaves: what stopped it was the exception the rules do not list.
    granted()
    with gateway() as (client, transport, _made, _mediator):
        assert client.post(CHAT[1], json=chat()).status_code == 200
        assert len(transport.sent) == 1


# --- in the settings: an exception's body component (config.ExceptionComponent) ---------------------------------------


def body_exception(*components: dict[str, Any], stamped: bool = True) -> RequestException:
    stamps = {"id": "x0123456789", "granted": NOW, "revision": request_profiles.REVISION} if stamped else {}
    return RequestException.model_validate(
        {
            "destination": "openai",
            "profile": "openai.chat.1",
            "reason": "other",
            "components": list(components),
            **stamps,
        }
    )


def settings(*rows: RequestException) -> Config:
    return Config(identity=IdentityConfig(enabled=False), request_exceptions=list(rows))


TEAM = {"kind": "body", "name": "/x_context/team", "type": "string"}


def test_an_exception_names_a_body_member_by_its_selector():
    row = body_exception(TEAM, {"kind": "body", "name": "/messages/*/x_meta/size", "type": "number"})
    assert [(c.kind, c.name, c.type, c.role) for c in row.components] == [
        ("body", "/x_context/team", "string", "checked"),
        ("body", "/messages/*/x_meta/size", "number", "checked"),
    ]
    selectors = [BodySelector(c.name, c.type or "") for c in row.components]
    assert [one.segments for one in selectors] == [("x_context", "team"), ("messages", None, "x_meta", "size")]


@pytest.mark.parametrize(
    "component",
    [
        {"kind": "body", "name": "/x_context/team", "type": "object"},  # never a parent object
        {"kind": "body", "name": "/x_context/team", "type": "string", "role": "credential"},  # always checked
        {"kind": "body", "name": "/x_context/te*", "type": "string"},  # no key wildcard
        {"kind": "body", "name": "/x_context/api_key", "type": "string"},  # never a credential member
        {"kind": "body", "name": "x_context", "type": "string"},  # a pointer
        {"kind": "body", "name": "/" + "k" * 256, "type": "string"},
        {"kind": "header", "name": "x-team", "type": "string"},  # a header names no type
        {"kind": "query", "name": "team", "type": "string"},
    ],
)
def test_a_body_component_outside_the_selector_grammar_is_refused(component):
    with pytest.raises(ValidationError) as raised:
        body_exception(component)
    assert "k" * 200 not in str(raised.value) and "api_key" not in str(raised.value)


def test_a_body_component_names_the_type_of_its_value():
    with pytest.raises(ValidationError):
        body_exception({"kind": "body", "name": "/x_context/team"})
    body_exception({"kind": "body", "name": "/x_list/*", "type": "string"})  # an index wildcard is the grammar's


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param((TEAM,), (TEAM, {"kind": "body", "name": "/x_context/size", "type": "number"}), id="widened"),
        pytest.param((TEAM,), ({**TEAM, "type": "number"},), id="another-type"),
        pytest.param((TEAM,), ({**TEAM, "name": "/x_context/lead"},), id="another-member"),
    ],
)
def test_naming_another_body_member_or_type_is_a_loosening(before, after):
    reasons = loosened(settings(body_exception(*before)), settings(body_exception(*after)))
    assert reasons and all("request exception" in reason for reason in reasons)
    assert all("x_context" not in reason for reason in reasons)  # never what it names


def test_naming_fewer_body_members_applies_at_once():
    wide = body_exception(TEAM, {"kind": "body", "name": "/x_context/size", "type": "number"})
    assert loosened(settings(wide), settings(body_exception(TEAM))) == []
    assert loosened(settings(body_exception(TEAM)), settings(body_exception(TEAM))) == []


# --- R-100: a revised profile ----------------------------------------------------------------------------------------


def revised(*changed: str) -> dict[str, tuple[BodyRevision, ...]]:
    return {"chat": (BodyRevision("chat.body.1"), BodyRevision("chat.body.2", changed))}


SELECTED = (selector("/messages/*/x_meta/team"), selector("/x_context/size", "number"))


def test_a_body_revision_that_keeps_the_selected_components_keeps_the_selector():
    assert carries_over(SELECTED, "chat", "chat.body.1", history=revised())
    assert carries_over(
        SELECTED, "chat", "chat.body.1", history=revised("/tools/*/x_meta", "/x_contexts", "/messages/*/name")
    )
    assert carries_over(
        SELECTED, "chat", "chat.body.2", history=revised("/messages/*/x_meta")
    )  # approved after the change


@pytest.mark.parametrize(
    "changed",
    [
        "/messages/*/x_meta/team",  # the selected member itself
        "/messages/*/x_meta",  # its holder
        "/messages",  # an ancestor
        "/messages/3/x_meta",  # one element of what the wildcard selects
        "/messages/*/x_meta/team/deep",  # a level below it, newly declared
        "/x_context/size",
    ],
)
def test_a_body_revision_that_changes_a_selected_component_suspends_the_selector(changed):
    assert not carries_over(SELECTED, "chat", "chat.body.1", history=revised(changed))


@pytest.mark.parametrize("approved", ["chat.body.3", "chat.body.0", "anthropic.body.1", "chat.body", ""])
def test_an_unknown_or_later_body_revision_suspends_the_selector(approved):
    """A revision this version does not have (a downgrade, another protocol's) is never assumed to keep anything."""
    assert not carries_over(SELECTED, "chat", approved, history=revised())
    assert not carries_over(SELECTED, "x_protocol", "chat.body.1", history=revised())


def test_the_shipped_body_revisions_change_nothing_yet_and_are_the_proofs_own(mediator):
    for protocol, chain in body_selectors.HISTORY.items():
        assert [revision.id for revision in chain] == [f"{protocol}.body.1"]
        assert body_selectors.revision(protocol) == f"{protocol}.body.1"
        assert carries_over(SELECTED, protocol, f"{protocol}.body.1", history=body_selectors.HISTORY)
    result = run(mediator, CHAT, chat())
    result.freeze_inputs()
    assert result.body_proof is not None
    assert result.body_proof.profile == body_selectors.revision("chat")


def test_a_body_proof_names_the_revision_its_protocol_is_read_by(mediator, monkeypatch):
    """The proof's profile is the body revision in force for its protocol (``body_selectors.revision``), never a
    fixed ``.body.1``: a later revision is the one a proof names, while the other protocols keep theirs."""
    monkeypatch.setattr(body_selectors, "HISTORY", {**body_selectors.HISTORY, **revised("/x_tenant")})
    result = run(mediator, CHAT, chat())
    result.freeze_inputs()
    assert result.body_proof is not None and result.body_proof.profile == "chat.body.2"
    other = run(mediator, ANTHROPIC, copy.deepcopy(ANTHROPIC[2]))
    other.freeze_inputs()
    assert other.body_proof is not None and other.body_proof.profile == "anthropic.body.1"


def test_a_revision_lists_its_changes_as_selector_shaped_pointers():
    with pytest.raises(SelectorRefused):
        BodyRevision("chat.body.2", ("messages/x",))
    with pytest.raises(SelectorRefused):
        BodyRevision("chat.body.2", ("/messages/x*",))
    BodyRevision("chat.body.2", ("/messages/*/x", "/api_key"))  # a change may name a credential member: it suspends
