"""A step's checks: each kind holds, or says in plain words what did not. They read what the model received,
what it captured, the agent's files and answers, the recorded exchanges, the live rules and what the step
shows, and fill in the chapter's placeholders for what the model received first."""

import json

import pytest

from latet.config import Config, LiveReview, Profile, save_config
from latet.engine.masker import AllowList
from latet.engine.types import EntityClass, SlotKind
from latet.gateway.recorder import replacement_dicts
from latet.tutorial.chapters import Check, Show
from latet.tutorial.checks import Context, evaluate

EMAIL = "james.wilson@acme.com"


def _checks(*raw: dict) -> list[Check]:
    return [Check.model_validate(one) for one in raw]


def test_what_the_model_received_in_the_step_or_in_the_whole_run():
    ctx = Context(received=['{"content": "say \\"hi\\" to fake-1"}'], received_run=["secret-1", "x"])
    assert evaluate(_checks({"model_received": {"contains": ["fake-1"], "requests": 1}}), ctx) == []
    assert evaluate(_checks({"model_received": {"not_contains": ["secret-1"]}}), ctx) == []
    # A value is found in the JSON body as JSON writes it, escaped.
    [failure] = evaluate(_checks({"model_received": {"not_contains": ['say "hi"']}}), ctx)
    assert failure == "the model received 'say \"hi\"' in this step"
    failures = evaluate(_checks({"model_received": {"not_contains": ["secret-1"], "requests": 1, "in": "run"}}), ctx)
    assert failures == [
        "the model received 'secret-1' in this chapter",
        "the model received 2 requests in this chapter, not 1",
    ]
    assert evaluate(_checks({"model_received": {"contains": ["nope"]}}), ctx) == [
        "the model never received 'nope' in this step"
    ]


def test_what_the_model_captured():
    ctx = Context(captured={"KEY": "3G0OoYyX6TrNM8v8g6r5AjrVwCAXUXKoGDzV6URN"})
    holds = {"name": "KEY", "not": "3D8QnFhA0QhAC9t5j7t6JtgLgPWLFMHbLRuT7TWZ", "matches": "[A-Za-z0-9]{40}"}
    assert evaluate(_checks({"received": holds}), ctx) == []
    failures = evaluate(_checks({"received": {"name": "KEY", "equals": "x", "matches": "[0-9]+"}}), ctx)
    assert failures == [
        "the model received '3G0OoYyX6TrNM8v8g6r5AjrVwCAXUXKoGDzV6URN' as KEY, not 'x'",
        "KEY is '3G0OoYyX6TrNM8v8g6r5AjrVwCAXUXKoGDzV6URN', which does not match [0-9]+",
    ]
    assert evaluate(_checks({"received": {"name": "EMAIL", "equals": "x"}}), ctx) == ["the model never received EMAIL"]


def test_the_files_the_agent_wrote(tmp_path):
    (tmp_path / "config.local.json").write_bytes(b'{"api_key": "real-key-1", "note": "a \\"quoted\\" word"}')
    ctx = Context(workspace=tmp_path)
    holds = {"path": "config.local.json", "exists": True, "contains": ["real-key-1", 'a "quoted" word']}
    assert evaluate(_checks({"file": holds}), ctx) == []
    assert evaluate(_checks({"file": {"path": "config.local.json", "not_contains": ["real-key-1"]}}), ctx) == [
        "config.local.json holds 'real-key-1'"
    ]
    assert evaluate(_checks({"file": {"path": "nope.txt", "exists": False}}), ctx) == []
    assert evaluate(_checks({"file": {"path": "nope.txt", "contains": ["x"]}}), ctx) == ["there is no nope.txt"]


def test_the_exchange_a_turn_answered():
    summary = {
        "mode": "verify",
        "flags": ["blocked", "leak"],
        "block_reason": "sensitive values would leave in clear: GENERIC_API_KEY[40]",
        "by_class": {"SECRET": 1},
        "by_type": {"GENERIC_API_KEY": 1},
        "project": None,
    }
    ctx = Context(exchanges={"last": {"summary": summary}})
    holds = {
        "ref": "last",
        "mode": "verify",
        "blocked": True,
        "reason_contains": "would leave in clear",
        "masked_classes": ["SECRET"],
        "masked_types": ["GENERIC_API_KEY"],
        "flags": ["leak"],
        "no_project": True,
    }
    assert evaluate(_checks({"exchange": holds}), ctx) == []
    fails = {"ref": "last", "mode": "rewrite", "blocked": False, "masked_types": ["EMAIL"], "flags": ["history"]}
    assert evaluate(_checks({"exchange": fails}), ctx) == [
        "exchange last: mode verify, not rewrite",
        "exchange last: stopped",
        "exchange last: nothing masked as EMAIL",
        "exchange last: no flag history",
    ]
    assert evaluate(_checks({"exchange": {"ref": "write", "mode": "rewrite"}}), ctx) == ["no exchange write"]


def test_the_rules_that_masked_the_values_of_an_exchange(masker, vault):
    """By the rule each value was masked with, as the dashboard shows it: `human:person` for a name masked by hand."""
    found = _slot(masker, vault, f"email {EMAIL} the invoice")
    by_hand = {"masked": "write to Evelyn Young", "replacements": [{"rule": "human:person", "type": "PERSON"}]}
    ctx = Context(exchanges={"filed": {"summary": {}, "request": {"slots": [found, by_hand]}}})
    assert evaluate(_checks({"exchange": {"ref": "filed", "masked_rules": ["human:person", "pii:email"]}}), ctx) == []
    assert evaluate(_checks({"exchange": {"ref": "filed", "masked_rules": ["human:org"]}}), ctx) == [
        "exchange filed: nothing masked by human:org"
    ]


def test_what_the_agent_got_and_the_error_it_was_given():
    ctx = Context(got=["Done.", '{"content": "{\\"api_key\\": \\"real-key-1\\"}"}'], error="Latet: stopped")
    assert evaluate(_checks({"agent_got": {"contains": ["real-key-1"], "not_contains": ["fake-1"]}}), ctx) == []
    assert evaluate(_checks({"agent_got": {"contains": ["fake-1"]}}), ctx) == ["the agent never got 'fake-1'"]
    assert evaluate(_checks({"agent_error": {"contains": "stopped"}}), ctx) == []
    assert evaluate(_checks({"agent_error": {"contains": "stopped"}}), Context()) == ["the agent got no error"]


def test_what_the_agent_got_of_a_turn_of_the_model_restored(vault):
    """A check names the model's turn instead of a sentence: the agent got the turn's text as the model sent it, every
    fake in it restored to its original (what the agent shows), whatever its language."""
    vault.intern("CUSTOM", EntityClass.CUSTOM, "Project Aurora", generate=lambda _original, _rng: "Zephyr-9")
    turns = {"again": "What's Zephyr-9? No idea."}
    again = _checks({"agent_got": {"turn": "again"}})
    assert evaluate(again, Context(turns=turns, vault=vault, got=["What's Project Aurora? No idea."])) == []
    assert evaluate(again, Context(turns=turns, vault=vault, got=["What's Zephyr-9? No idea."])) == [
        "the agent never got turn `again` restored"
    ]
    assert evaluate(again, Context(vault=vault, got=["What's Project Aurora? No idea."])) == [
        "the model never sent turn `again`"
    ]
    assert evaluate(again, Context(turns=turns, got=["What's Project Aurora? No idea."])) == [
        "no vault to restore turn `again`"
    ]


def test_a_setting_in_use_and_in_config_yaml(tmp_path):
    """A setting as the sandbox runs it, and as config.yaml holds it: an agent's change that lowers protection is
    written there at once, and runs only once it is approved."""
    path = tmp_path / "config.yaml"
    save_config(Config(profile=Profile.SECRETS, live_review=LiveReview(enabled=True)), path)
    ctx = Context(config=Config(), config_file=path)
    holds = {"key": "profile", "running": "balanced", "saved": "secrets"}
    assert evaluate(_checks({"setting": holds}), ctx) == []
    assert evaluate(_checks({"setting": {"key": "live_review.enabled", "running": False, "saved": True}}), ctx) == []
    assert evaluate(_checks({"setting": {"key": "profile", "running": "secrets", "saved": "balanced"}}), ctx) == [
        "profile is 'balanced' in use, not 'secrets'",
        "profile is 'secrets' in config.yaml, not 'balanced'",
    ]
    assert evaluate(_checks({"setting": {"key": "live_review.colour", "running": "red"}}), ctx) == [
        "no setting live_review.colour"
    ]
    assert evaluate(_checks({"setting": holds}), Context()) == ["no settings to look at"]


def test_the_live_rules(masker):
    masker.allow = AllowList(values={"4242 4242 4242 4242"})
    ctx = Context(masker=masker)
    holds = {"rules": {"term": "acme corp", "allowed": "4242 4242 4242 4242", "masks": EMAIL}}
    assert evaluate(_checks(holds), ctx) == []
    fails = {"rules": {"term": "Project Falcon", "allowed": EMAIL, "masks": "4242 4242 4242 4242"}}
    assert evaluate(_checks(fails), ctx) == [
        "no term Project Falcon in the rules",
        f"{EMAIL!r} is not marked Not sensitive",
        "the rules do not mask '4242 4242 4242 4242'",
    ]


def _slot(masker, vault, text: str) -> dict:
    masked = masker.mask(text, SlotKind.USER_TEXT)
    return {"masked": masked.text, "replacements": replacement_dicts(masked.replacements, vault)}


def test_what_the_step_shows_marked_or_in_clear(masker, vault):
    slots = [
        _slot(masker, vault, f"email {EMAIL} the invoice"),
        {"masked": "card 4242 4242 4242 4242", "replacements": []},
    ]
    answer = _slot(masker, vault, f"I wrote down {EMAIL}.")
    response = {"text": answer["masked"], "text_spans": answer["replacements"], "tool_calls": []}
    detail = {"summary": {}, "request": {"slots": slots}, "response": response}
    request = Context(exchanges={"email": detail}, show=Show(exchange="email", part="request"), vault=vault)
    assert evaluate(_checks({"shown": {"marked": [EMAIL], "clear": ["4242 4242 4242 4242"]}}), request) == []
    assert evaluate(_checks({"shown": {"marked": ["4242 4242 4242 4242"], "clear": [EMAIL]}}), request) == [
        "'4242 4242 4242 4242' is in clear in the request shown",
        f"{EMAIL!r} is marked in the request shown",
    ]
    assert evaluate(_checks({"shown": {"marked": ["nowhere"]}}), request) == ["'nowhere' is not in the request shown"]
    shown = Context(exchanges={"email": detail}, show=Show(exchange="email", part="response"), vault=vault)
    assert evaluate(_checks({"shown": {"marked": [EMAIL]}}), shown) == []
    assert evaluate(_checks({"shown": {"marked": [EMAIL]}}), Context()) == ["the step shows no exchange"]


def test_a_value_that_stopped_the_request_is_marked_where_the_dashboard_boxes_it(masker, vault):
    """A stopped request's value is in clear in its block, as it would have left: the dashboard boxes it where the
    record says it is (offsets in the text as sent, moved by the values masked before it), when it stopped the
    request: new, not let through. Two stopped values that overlap are one box."""
    value = "PLAIN-VALUE-1"  # no rule finds it: as the check read it, in clear
    sent = _slot(masker, vault, f"email {EMAIL} and deploy with {value} now")
    at = sent["masked"].index(value)  # after the email's fake, longer than the email
    leak = {"type": "SECRET", "kind": "user_text", "preview": "SECRET[13]", "new": True, "block": "b1"}
    leak |= {"masked_start": at, "masked_end": at + len(value)}
    other = {"masked": "nothing here", "replacements": [], "block": "b2"}
    stopped = {"summary": {"flags": ["blocked", "leak"]}, "request": {"slots": [{**sent, "block": "b1"}, other]}}
    show = Show(component="why", exchange="last", part="request")

    def shown(*leaks: dict, flags: tuple[str, ...] = ("blocked", "leak"), **check: list[str]) -> list[str]:
        detail = {**stopped, "summary": {"flags": list(flags)}, "leaks": list(leaks)}
        return evaluate(_checks({"shown": check}), Context(exchanges={"last": detail}, show=show, vault=vault))

    assert shown(leak, marked=[value, EMAIL]) == []
    assert shown(leak, marked=[value[:5], value[-5:]]) == []  # inside the box
    in_clear = [f"{value!r} is in clear in the request shown"]
    assert shown(leak, flags=("leak",), marked=[value]) == in_clear  # not stopped: no box
    assert shown({**leak, "allowed": True}, marked=[value]) == in_clear  # let through (auto mode's reviewer)
    assert shown({**leak, "new": False}, marked=[value]) == in_clear  # sent before: it did not stop it
    assert shown({**leak, "block": "b2"}, marked=[value]) == in_clear  # in another block
    assert shown(leak, clear=[value]) == [f"{value!r} is marked in the request shown"]
    # The key and its tail read as two values, one over the other: one box over both.
    head = {**leak, "masked_end": at + 9}
    tail = {**leak, "masked_start": at + 4}
    assert shown(head, tail, marked=[value]) == []


def test_placeholders_are_filled_with_what_the_model_received():
    ctx = Context(captured={"KEY": "fake-1"}, got=["fake-1 is the key"])
    assert evaluate(_checks({"agent_got": {"contains": ["{{received:KEY}}"]}}), ctx) == []
    assert evaluate(_checks({"agent_got": {"contains": ["{{received:EMAIL}}"]}}), ctx) == [
        "agent_got: the model never received EMAIL"
    ]


@pytest.mark.parametrize("value", ['a "quoted" word', "caffè", "tab\there"])
def test_a_value_is_found_as_json_writes_it(value):
    body = json.dumps({"messages": [{"content": json.dumps({"content": value})}]})
    assert evaluate(_checks({"model_received": {"contains": [value]}}), Context(received=[body])) == []
