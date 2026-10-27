"""``forget``: values taken out of the API key's vault, a list or one value, sent in calls of ``max_texts`` values and
``max_bytes`` of JSON at most, each value once, the counts of every call summed in one ``ForgetResult``; a token without
the permission is ``Forbidden``; ``AsyncLatet`` does the same with ``await``."""

from __future__ import annotations

import json

import pytest

from latet_client import Forbidden, ForgetResult, LatetError
from latet_stand_in import Fake, error

MAIL = "mario.rossi@acme.it"
VALUES = [f"customer.{n}@acme.it" for n in range(2500)]


def test_one_value_or_a_list_and_the_counts(fake: Fake) -> None:
    with fake.client() as latet:
        latet.mask([f"to {MAIL}", f"cc {VALUES[0]}"])
        assert latet.forget(MAIL) == ForgetResult(forgotten=1, unknown=0)
        assert latet.forget([MAIL, VALUES[0], VALUES[1]]) == ForgetResult(forgotten=1, unknown=2)
        assert latet.unmask("to person1@masked.test").restored == 0  # its fake restores nothing
    forgets = [call for call in fake.calls if call.operation == "forget"]
    assert [call.body for call in forgets] == [{"values": [MAIL]}, {"values": [MAIL, VALUES[0], VALUES[1]]}]
    assert forgets[0].content == json.dumps({"values": [MAIL]}, separators=(",", ":")).encode()


def test_a_long_list_goes_in_calls_and_each_value_once(fake: Fake) -> None:
    with fake.client() as latet:
        latet.mask([f"line {value}" for value in VALUES[::2]])
        found = latet.forget([*VALUES, VALUES[0], VALUES[2]])
    assert found == ForgetResult(forgotten=1250, unknown=1250)
    sent = [call.body["values"] for call in fake.calls if call.operation == "forget"]
    assert [len(values) for values in sent] == [1000, 1000, 500]
    assert [value for values in sent for value in values] == VALUES  # in order, each once


def test_max_texts_and_max_bytes_bound_a_call(fake: Fake) -> None:
    with fake.client(max_texts=300, max_bytes=4000) as latet:
        latet.forget(VALUES[:1000])
    calls = [call for call in fake.calls if call.operation == "forget"]
    assert all(len(call.body["values"]) <= 300 and len(call.content) <= 4000 for call in calls)
    assert [value for call in calls for value in call.body["values"]] == VALUES[:1000]


def test_an_empty_list_makes_no_call(fake: Fake) -> None:
    with fake.client() as latet:
        assert latet.forget([]) == ForgetResult(forgotten=0, unknown=0)
    assert fake.calls == []


def test_a_token_without_the_permission_is_forbidden(fake: Fake) -> None:
    fake.scripted.append(error(403, "this token lacks the forget permission"))
    with fake.client() as latet, pytest.raises(Forbidden) as raised:
        latet.forget(MAIL)
    assert raised.value.status == 403 and raised.value.message == "this token lacks the forget permission"
    assert MAIL not in str(raised.value)


@pytest.mark.parametrize("given", [[MAIL, 1], 7, None, [b"bytes"]])
def test_values_are_texts(fake: Fake, given: object) -> None:
    with fake.client() as latet, pytest.raises(TypeError, match="values is a str, or a sequence of str"):
        latet.forget(given)  # type: ignore[arg-type]
    assert fake.calls == []


def test_a_value_utf8_cannot_write_sends_nothing(fake: Fake) -> None:
    with fake.client() as latet, pytest.raises(ValueError, match="value 1 cannot be written") as raised:
        latet.forget([MAIL, "caf\udce9"])
    assert fake.calls == [] and MAIL not in str(raised.value)


@pytest.mark.parametrize(
    "answer",
    [{"results": []}, {"forgotten": 1}, {"forgotten": "1", "unknown": 0}, {"forgotten": True, "unknown": 0}, []],
)
def test_an_answer_that_is_not_the_servers_is_an_error(fake: Fake, answer: object) -> None:
    import httpx

    fake.scripted.append(httpx.Response(200, json=answer))
    with fake.client() as latet, pytest.raises(LatetError, match="not the applications API's"):
        latet.forget(MAIL)


async def test_the_async_client_forgets_as_the_sync_one() -> None:
    sync, asynchronous = Fake(), Fake()
    with sync.client() as one:
        one.mask([f"line {value}" for value in VALUES[:40]])
        expected = [one.forget(VALUES[0]), one.forget(VALUES[:1500])]
    async with asynchronous.aclient() as other:
        await other.mask([f"line {value}" for value in VALUES[:40]])
        found = [await other.forget(VALUES[0]), await other.forget(VALUES[:1500])]
    assert found == expected == [ForgetResult(1, 0), ForgetResult(39, 1461)]
    assert [(c.operation, c.content) for c in asynchronous.calls] == [(c.operation, c.content) for c in sync.calls]


async def test_the_async_client_is_forbidden_too() -> None:
    fake = Fake()
    fake.scripted.append(error(403, "this token lacks the forget permission"))
    async with fake.aclient() as latet:
        with pytest.raises(Forbidden):
            await latet.forget([MAIL])
