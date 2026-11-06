"""A request is never refused only because the rules moved while it was prepared (on a server, a settings change
another instance announced and this one applied meanwhile, ``Engine.rules_changed``): its route authority is taken
again under the rules in force and the request is prepared again by them. Stricter rules refuse it; settings that
keep moving past the bound still refuse it, as before. The two-instance flow is tests/server/test_valkey_nothing_in_
clear.py, where a request on one instance follows a settings change announced from the other."""

from __future__ import annotations

import pytest

from latet.engine.types import EntityClass
from latet.gateway import forward

from .test_request_coverage import counts, gateway

EMAIL = "emily.carter@acme.com"
BODY = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"write to {EMAIL}"}]}
BEARER = {"authorization": "Bearer sk-test"}


def _moving(mediator, times: int, change=None):
    """The rules move ``times`` times, each while a preparation runs (as an announced change applied meanwhile)."""
    prepare = mediator.prepare_http
    left = [times]

    def prepared(*args, **kwargs):
        if left[0] > 0:
            left[0] -= 1
            if change is not None:
                change(mediator)
            mediator.engine.rules_changed()
        return prepare(*args, **kwargs)

    mediator.prepare_http = prepared


def test_a_request_prepared_while_the_rules_moved_is_prepared_again_and_sent():
    with gateway() as (client, transport, made, mediator):
        _moving(mediator, 1)
        answer = client.post("/openai/v1/chat/completions", json=BODY, headers=BEARER)
        assert answer.status_code == 200, answer.text
        assert counts(made) == (1, 1) and EMAIL.encode() not in transport.sent[-1].content


def test_stricter_rules_applied_meanwhile_decide_the_request():
    """A value the vault learnt meanwhile (a rule of the new settings): the second preparation masks it by them."""

    def stricter(mediator):
        mediator.engine.vault.intern("GITHUB_PAT", EntityClass.SECRET, "billing")

    secret = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "deploy with billing"}]}
    with gateway() as (client, transport, _made, mediator):
        _moving(mediator, 1, stricter)
        answer = client.post("/openai/v1/chat/completions", json=secret, headers=BEARER)
        assert answer.status_code == 200 and b"billing" not in transport.sent[-1].content  # masked by the new rules


def test_rules_that_keep_moving_past_the_bound_still_refuse_it():
    with gateway() as (client, _transport, made, mediator):
        _moving(mediator, forward._REDERIVE + 1)
        answer = client.post("/openai/v1/chat/completions", json=BODY, headers=BEARER)
        assert answer.status_code == 400 and counts(made) == (0, 0)
        # Told that the settings changed and to send it again (minors after 0.11, item 2), never coverage's words.
        assert "the settings changed while this request was prepared" in answer.text
        assert "could not account for every part" not in answer.text


@pytest.mark.parametrize("bound", [0])
def test_with_no_second_preparation_the_request_was_refused(monkeypatch, bound):
    """The behaviour before: one preparation, and a refusal whenever the rules moved during it."""
    monkeypatch.setattr(forward, "_REDERIVE", bound)
    with gateway() as (client, _transport, _made, mediator):
        _moving(mediator, 1)
        assert client.post("/openai/v1/chat/completions", json=BODY, headers=BEARER).status_code == 400
