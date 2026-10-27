"""What fails before or without an answer: a text that cannot be written (nothing is sent), a call that does not go
through (an error that holds nothing of the request, so nothing of the token), and plain ``http://`` to another
computer (a warning, since the token and the texts then travel in clear)."""

from __future__ import annotations

import warnings

import httpx
import pytest

import latet_client
from latet_client import AsyncLatet, Latet, LatetError
from latet_stand_in import TOKEN, URL, error, holds

MAIL = "mario.rossi@acme.it"
LONE = "e\udc80 a log line read with surrogateescape"  # a lone surrogate: UTF-8 cannot write it


@pytest.mark.parametrize("operation", ["scan", "mask", "unmask"])
def test_a_text_that_cannot_be_written_stops_the_call_before_anything_is_sent(fake, operation):
    texts = ["a", "b", "c", "d", LONE]
    with fake.client(max_texts=2) as client, pytest.raises(ValueError, match="text 4") as raised:
        getattr(client, operation)(texts)
    assert fake.calls == []  # the batches before it were not sent either
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert not holds(raised.value, "log line") and "\udc80" not in str(raised.value)


@pytest.mark.parametrize("operation", ["scan", "mask", "unmask"])
async def test_a_text_that_cannot_be_written_stops_the_async_call_too(fake, operation):
    async with fake.aclient(max_texts=2) as client:
        with pytest.raises(ValueError, match="text 4") as raised:
            await getattr(client, operation)(["a", "b", "c", "d", LONE])
    assert fake.calls == []
    assert raised.value.__cause__ is None and raised.value.__context__ is None


FAILURES = [
    httpx.ConnectError("[Errno 111] Connection refused"),
    httpx.ConnectTimeout("timed out"),
    httpx.ReadTimeout("timed out"),
    httpx.RemoteProtocolError("server disconnected"),
]


def _the_token_is_reachable_from(failure):
    """The control: the transport's own error, as it is raised, holds the request, whose headers hold the token."""
    sent = httpx.Request("POST", URL, headers={"authorization": f"Bearer {TOKEN}"})
    return holds(type(failure)("x", request=sent), TOKEN)


@pytest.mark.parametrize("failure", FAILURES, ids=lambda failure: type(failure).__name__)
def test_a_call_that_does_not_go_through_is_an_error_that_holds_nothing_of_it(failure):
    def handler(request):
        raise type(failure)(str(failure), request=request)

    assert _the_token_is_reachable_from(failure)
    with (
        Latet(URL, TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))) as client,
        pytest.raises(latet_client.ConnectionFailed) as raised,
    ):
        client.mask(f"to {MAIL}")
    refusal = raised.value
    assert isinstance(refusal, LatetError) and refusal.status == 0
    assert refusal.message == f"the call to latet.acme.lan did not go through ({type(failure).__name__})"
    assert str(refusal) == f"0: {refusal.message}"
    assert refusal.__cause__ is None and refusal.__context__ is None  # the transport's error, with its request
    assert not holds(refusal, TOKEN) and TOKEN not in repr(refusal) and MAIL not in str(refusal)


async def test_a_call_that_does_not_go_through_is_the_same_error_in_the_async_client():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    async with AsyncLatet(URL, TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        with pytest.raises(latet_client.ConnectionFailed) as raised:
            await client.unmask("hello")
    assert raised.value.status == 0
    assert raised.value.message == "the call to latet.acme.lan did not go through (ReadTimeout)"
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert not holds(raised.value, TOKEN)


def test_a_call_that_does_not_go_through_is_not_tried_again(slept):
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.ConnectError("refused", request=request)

    with (
        Latet(URL, TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))) as client,
        pytest.raises(latet_client.ConnectionFailed),
    ):
        client.scan("hello")
    assert len(seen) == 1 and slept == []


def test_an_answer_is_still_an_answer(fake):
    fake.scripted = [error(401, "latet: unauthorized")]
    with fake.client() as client, pytest.raises(latet_client.Unauthorized):
        client.scan("hello")


@pytest.mark.parametrize(
    "url", ["http://latet.acme.lan", "http://10.0.0.5:5283", "http://192.168.1.10", "http://[2001:db8::1]:5283"]
)
def test_plain_http_to_another_computer_is_a_warning(url):
    with pytest.warns(latet_client.ClearTextWarning, match="travel in clear") as warned:
        client = Latet(url, TOKEN)
    client.close()
    assert len(warned) == 1 and TOKEN not in str(warned[0].message)
    assert warned[0].filename == __file__  # at the line that made the client


async def test_plain_http_to_another_computer_is_a_warning_for_the_async_client_too():
    with pytest.warns(latet_client.ClearTextWarning) as warned:
        client = AsyncLatet("http://latet.acme.lan", TOKEN)
    await client.aclose()
    assert warned[0].filename == __file__


@pytest.mark.parametrize(
    "url",
    [
        "https://latet.acme.lan",
        "https://10.0.0.5",
        "http://127.0.0.1:5283",
        "http://127.1.2.3",
        "http://localhost:5283",
        "http://[::1]:5283",
    ],
)
def test_https_and_this_computer_are_no_warning(url):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Latet(url, TOKEN).close()
