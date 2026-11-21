"""How the Company approvals page's API answers a decision (Latet Enterprise): each refusal and each decision not taken
with a code beside its English words, which the dashboard says in each of its languages; an end no answer expects is
never answered as done (the review of Task 7, m5 and m6)."""

import json

import pytest

from latet.ee.approvals_api import GONE, STALE, UNAPPLIED, UNEXPECTED, _decided, _refused


@pytest.mark.parametrize(("state", "words"), [("stale", STALE), ("gone", GONE), ("approved-unapplied", UNAPPLIED)])
def test_a_decision_not_taken_answers_409_with_its_code(state, words):
    answer = _decided(state)
    assert answer.status_code == 409 and json.loads(bytes(answer.body)) == {"error": words, "code": state}


@pytest.mark.parametrize("state", ["approved", "rejected"])
def test_a_decision_taken_answers_how_it_ended(state):
    answer = _decided(state)
    assert answer.status_code == 200 and json.loads(bytes(answer.body)) == {"ok": True, "state": state}


@pytest.mark.parametrize("state", ["not-yours", "superseded", "withdrawn", ""])
def test_an_end_no_answer_expects_is_never_said_as_done(state):
    answer = _decided(state)
    assert answer.status_code == 500
    assert json.loads(bytes(answer.body)) == {"error": UNEXPECTED, "code": "unexpected"}


@pytest.mark.parametrize(("why", "status"), [("yours", 403), ("not-yours", 403), ("gone", 409), ("changed", 409)])
def test_each_refusal_of_a_decision_carries_its_code(why, status):
    answer = _refused(why).answer()
    assert answer.status_code == status and json.loads(bytes(answer.body))["code"] == why
