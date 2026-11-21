"""A request is never refused only because the rules moved while it was prepared (on a server, a settings change
another instance announced and this one applied meanwhile, ``Engine.rules_changed``): its route authority is taken
again under the rules in force and the request is prepared again by them. Stricter rules refuse it; settings that
keep moving past the bound still refuse it, as before. The two-instance flow is tests/server/test_valkey_nothing_in_
clear.py, where a request on one instance follows a settings change announced from the other."""

from __future__ import annotations

import pytest

from latet.engine.detectors.terms import LiveTerms
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


# The terms a company syncs (Latet Enterprise, Task 3's fix round 1): a sync is a change of the rules for the send gates
# too, as a change of the company's locked terms is (it moves the rules' count). A request's authority holds the terms'
# tag (``LocalAuthority``): one whose terms moved after its final check is prepared again before its reviews, and refused
# after them; a sync that changes no term (the same tag) changes nothing.
HALDEN = ("Halden Insurance", "CLIENT")
KESTREL = "Kestrel Analytics"
SYNCED = {
    "model": "gpt-4.1-mini",
    "messages": [{"role": "user", "content": f"write to {KESTREL} and Halden Insurance"}],
}


def _synced(mediator) -> LiveTerms:
    """The terms a company syncs in the mediator's one masker (the engine's and the final check's), as
    ``build(…, extra=)`` puts them there."""
    live = LiveTerms()
    live.use([HALDEN], "1.1")
    mediator.engine.masker.detectors.append(live)
    return live


def _synced_after_preparing(mediator, live, terms, tag) -> list[int]:
    """``live`` given ``terms`` under ``tag`` once, right after the first preparation masked the request (its final
    check passed under the terms before), as a sync applied while it is in flight; how many preparations ran."""
    prepare = mediator.prepare_http
    calls: list[int] = []

    def prepared(*args, **kwargs):
        calls.append(1)
        done = prepare(*args, **kwargs)
        if len(calls) == 1:
            live.use(terms, tag)
        return done

    mediator.prepare_http = prepared
    return calls


def test_a_request_whose_synced_terms_moved_while_it_was_prepared_is_prepared_again_with_them():
    with gateway() as (client, transport, made, mediator):
        live = _synced(mediator)
        calls = _synced_after_preparing(mediator, live, [HALDEN, (KESTREL, "CLIENT")], "2.1")
        answer = client.post("/openai/v1/chat/completions", json=SYNCED, headers=BEARER)
        assert answer.status_code == 200, answer.text
        assert len(calls) == 2 and counts(made) == (1, 1)
        sent = transport.sent[-1].content
        assert KESTREL.encode() not in sent and b"Halden" not in sent  # the new term masked in what leaves


def test_a_sync_that_changes_no_term_prepares_nothing_again():
    with gateway() as (client, transport, made, mediator):
        live = _synced(mediator)
        calls = _synced_after_preparing(mediator, live, [HALDEN], "1.1")  # the same terms, the same tag
        answer = client.post("/openai/v1/chat/completions", json=SYNCED, headers=BEARER)
        assert answer.status_code == 200, answer.text
        assert len(calls) == 1 and counts(made) == (1, 1) and b"Halden" not in transport.sent[-1].content


def test_a_request_whose_synced_terms_moved_after_its_reviews_is_refused_and_nothing_is_sent():
    with gateway() as (client, transport, made, mediator):
        live = _synced(mediator)
        reviewed = mediator.live_prepared

        async def synced_after(prepared, *args, **kwargs):
            done = await reviewed(prepared, *args, **kwargs)
            live.use([HALDEN, (KESTREL, "CLIENT")], "2.1")  # after the reviews, before it is sent
            return done

        mediator.live_prepared = synced_after
        answer = client.post("/openai/v1/chat/completions", json=SYNCED, headers=BEARER)
        assert answer.status_code == 400, answer.text
        assert counts(made) == (0, 0) and transport.sent == []
        # Its agent is told that the settings changed and to send it again (Community 0.11.1's words, which
        # `enterprise/term-sync` gives), never coverage's.
        assert "the settings changed while this request was prepared: send it again" in answer.text
        assert "could not account for every part" not in answer.text


def _gone_and_back(live):
    """The re-review's R1: the terms taken away and the same ones given again, back under the same tag."""
    live.use([], "")
    live.use([HALDEN], "1.1")


def test_a_request_prepared_across_a_removal_and_the_same_terms_back_is_prepared_again():
    """The tag is the one of before, the stamp is not: the request's authority, captured before, is taken again."""
    with gateway() as (client, transport, made, mediator):
        live = _synced(mediator)
        prepare, calls = mediator.prepare_http, []

        def prepared(*args, **kwargs):
            calls.append(1)
            done = prepare(*args, **kwargs)
            if len(calls) == 1:
                _gone_and_back(live)
            return done

        mediator.prepare_http = prepared
        answer = client.post("/openai/v1/chat/completions", json=SYNCED, headers=BEARER)
        assert answer.status_code == 200, answer.text
        assert len(calls) == 2 and counts(made) == (1, 1) and b"Halden" not in transport.sent[-1].content


def test_a_request_whose_terms_went_and_came_back_after_its_reviews_is_refused_and_nothing_is_sent():
    with gateway() as (client, transport, made, mediator):
        live = _synced(mediator)
        reviewed = mediator.live_prepared

        async def gone_and_back(prepared, *args, **kwargs):
            done = await reviewed(prepared, *args, **kwargs)
            _gone_and_back(live)  # after the reviews, before it is sent
            return done

        mediator.live_prepared = gone_and_back
        answer = client.post("/openai/v1/chat/completions", json=SYNCED, headers=BEARER)
        assert answer.status_code == 400, answer.text
        assert counts(made) == (0, 0) and transport.sent == []
        assert "the settings changed while this request was prepared: send it again" in answer.text


@pytest.mark.parametrize("terms", [[HALDEN, (KESTREL, "CLIENT")], "gone and back"])
def test_a_request_whose_synced_terms_moved_after_its_final_decision_is_refused_and_nothing_is_sent(monkeypatch, terms):
    """The merge review of 0.11.1's M2: terms that move after the request's final decision, while it is on its way out
    (here while its address is looked up; its exceptions judged again and, for Bedrock, its AWS credentials read are
    the same window), refuse it all the same at the transport's gate, nothing of it sent. Since 0.12.0 (its stale
    refusal, `notes/enterprise-release-checklist.md`, item 9's Community follow-up) its route authority, no longer
    valid, has it refused as one whose settings changed while it was prepared: 400, its agent told that the settings
    changed and to send it again, as before that decision and as `enterprise/term-sync` says in five languages; never
    the words of a request Latet could not send as it was decided (a 500)."""
    routed = forward.Forwarder._route
    with gateway() as (client, transport, made, mediator):
        live = _synced(mediator)

        async def synced_after(self, *args, **kwargs):
            route = await routed(self, *args, **kwargs)
            if terms == "gone and back":
                _gone_and_back(live)
            else:
                live.use(terms, "2.1")  # after the final decision, before it is sent
            return route

        monkeypatch.setattr(forward.Forwarder, "_route", synced_after)
        answer = client.post("/openai/v1/chat/completions", json=SYNCED, headers=BEARER)
        assert answer.status_code == 400, answer.text
        assert counts(made) == (0, 0) and transport.sent == []
        assert "the settings changed while this request was prepared: send it again" in answer.text
        assert "could not send this request as it was decided" not in answer.text
