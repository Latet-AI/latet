"""``AsyncLatet`` does what ``Latet`` does, with ``await``: the same calls, results, batches, errors and retries."""

from __future__ import annotations

import httpx
import pytest

from latet_client import AsyncLatet, Forbidden, RateLimited, Unauthorized, VaultFull
from latet_stand_in import TOKEN, URL, Fake, error

MAIL = "mario.rossi@acme.it"
TEXTS = [f"line {n}: {n}@acme.it" if n % 3 else f"line {n}, nothing" for n in range(2300)] + ["BLOCK", ""]


async def test_async_client_matches_the_sync_one():
    sync, asynchronous = Fake(), Fake()
    with sync.client() as one:
        expected = {
            "scan one": one.scan(f"to {MAIL}"),
            "mask one": one.mask(f"to {MAIL}"),
            "unmask one": one.unmask("to person1@masked.test"),
            "scan data": one.scan(TEXTS, kind="data"),
            "mask all": one.mask(TEXTS),
            "unmask all": one.unmask([f"x person{n}@masked.test" for n in range(1, 40)]),
        }
    async with asynchronous.aclient() as other:
        found = {
            "scan one": await other.scan(f"to {MAIL}"),
            "mask one": await other.mask(f"to {MAIL}"),
            "unmask one": await other.unmask("to person1@masked.test"),
            "scan data": await other.scan(TEXTS, kind="data"),
            "mask all": await other.mask(TEXTS),
            "unmask all": await other.unmask([f"x person{n}@masked.test" for n in range(1, 40)]),
        }
    assert found == expected
    assert [(c.operation, c.content) for c in asynchronous.calls] == [(c.operation, c.content) for c in sync.calls]
    assert len(asynchronous.calls) > 6  # the lists were sent in batches
    assert all(call.headers["authorization"] == f"Bearer {TOKEN}" for call in asynchronous.calls)


async def test_a_list_keeps_its_order_across_batches(fake):
    texts = [f"line {n}" for n in range(2500)]
    async with fake.aclient() as client:
        results = await client.scan(texts)
    assert len(results) == 2500
    assert [len(call.body["texts"]) for call in fake.calls] == [1000, 1000, 500]
    assert [text for call in fake.calls for text in call.body["texts"]] == texts


async def test_an_empty_list_makes_no_call(fake):
    async with fake.aclient() as client:
        assert await client.mask([]) == []
    assert fake.calls == []


@pytest.mark.parametrize(("status", "kind"), [(401, Unauthorized), (403, Forbidden), (507, VaultFull)])
async def test_errors(fake, slept, status, kind):
    fake.scripted = [error(status, "words of the server")]
    async with fake.aclient() as client:
        with pytest.raises(kind) as raised:
            await client.mask("hello")
    assert type(raised.value) is kind and raised.value.message == "words of the server"
    assert TOKEN not in str(raised.value) and TOKEN not in repr(client)


async def test_retries_after_429_then_succeeds(fake, slept):
    fake.scripted = [error(429, "too many", **{"retry-after": "2"}), error(503, "later", **{"retry-after": "1"})]
    async with fake.aclient() as client:
        masked = await client.mask(f"to {MAIL}")
    assert masked.text == "to person1@masked.test"
    assert slept == [2.0, 1.0]


async def test_gives_up_after_retries(fake, slept):
    fake.scripted = [error(429, "too many", **{"retry-after": "3"})] * 10
    async with fake.aclient(retries=2) as client:
        with pytest.raises(RateLimited) as raised:
            await client.scan("hello")
    assert raised.value.retry_after == 3.0 and len(fake.calls) == 3 and slept == [3.0, 3.0]


async def test_blocked_and_degraded(fake):
    fake.degraded = ["llm-review"]
    async with fake.aclient() as client:
        blocked, fine = await client.mask(["BLOCK", "fine"])
    assert blocked.text is None and blocked.blocked and blocked.degraded == ("llm-review",)
    assert fine.text == "fine" and fine.blocked is None


async def test_texts_are_texts(fake):
    async with fake.aclient() as client:
        with pytest.raises(TypeError):
            await client.scan(["fine", 3])  # type: ignore[list-item]
    assert fake.calls == []


async def test_close_closes_the_client_it_made_and_not_the_one_it_was_given(fake):
    given = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    async with AsyncLatet(URL, TOKEN, client=given) as client:
        await client.scan("hello")
    await AsyncLatet(URL, TOKEN, client=given).aclose()
    assert not given.is_closed
    await given.aclose()
    own = AsyncLatet(URL, TOKEN)
    await own.aclose()
    assert own._http.is_closed
    await AsyncLatet(URL, TOKEN).close()  # the same as aclose


async def test_the_repr_has_no_token():
    client = AsyncLatet(URL, TOKEN)
    assert TOKEN not in repr(client) and URL in repr(client)
    await client.aclose()
