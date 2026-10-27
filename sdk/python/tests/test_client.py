"""``Latet``, the client of a Latet server's applications API, against a stand-in over ``httpx.MockTransport``: one text
in and one result out, a list in and a list out in the same order across calls, the errors, the retries, and a token
that is in no message."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from latet_client import (
    AsyncLatet,
    Finding,
    Forbidden,
    Latet,
    LatetError,
    MaskResult,
    RateLimited,
    ScanResult,
    TooLarge,
    Unauthorized,
    Unavailable,
    UnmaskResult,
    VaultFull,
)
from latet_stand_in import TOKEN, URL, error

MAIL = "mario.rossi@acme.it"


def test_one_text_in_one_result_out(fake):
    with fake.client() as client:
        scanned = client.scan(f"write to {MAIL}")
        masked = client.mask(f"write to {MAIL}")
        restored = client.unmask(masked.text)
    assert scanned == ScanResult(findings=[Finding("EMAIL", "pii", 9, 9 + len(MAIL))], counts={"EMAIL": 1})
    assert masked == MaskResult(
        text="write to person1@masked.test",
        replacements=[Finding(type="EMAIL", entity_class="pii", start=9, end=28)],
        counts={"EMAIL": 1},
        blocked=None,
    )
    assert restored == UnmaskResult(text=f"write to {MAIL}", restored=1)
    assert [call.operation for call in fake.calls] == ["scan", "mask", "unmask"]


def test_the_call_is_the_servers(fake):
    with fake.client() as client:
        client.mask(["hello"])
        client.mask("again", kind="data")
        client.unmask("hello")
    first, second, third = fake.calls
    assert first.body == {"texts": ["hello"], "kind": "prose"}
    assert second.body == {"texts": ["again"], "kind": "data"}
    assert third.body == {"texts": ["hello"]}  # an unmask takes no kind
    for call in fake.calls:
        assert call.headers["authorization"] == f"Bearer {TOKEN}"
        assert call.headers["content-type"] == "application/json"


def test_a_list_in_a_list_out(fake):
    with fake.client() as client:
        one = client.mask([f"to {MAIL}"])
        several = client.scan(["nothing", f"to {MAIL}", ""])
    assert isinstance(one, list) and [r.text for r in one] == ["to person1@masked.test"]
    assert [r.counts for r in several] == [{}, {"EMAIL": 1}, {}]


def test_an_empty_list_makes_no_call(fake):
    with fake.client() as client:
        assert client.scan([]) == [] and client.mask([]) == [] and client.unmask([]) == []
    assert fake.calls == []


def test_a_list_keeps_its_order_across_batches(fake):
    texts = [f"line {n}: {n}@acme.it" for n in range(2500)]
    with fake.client() as client:
        masked = client.mask(texts)
        restored = client.unmask([r.text for r in masked])
    assert [len(call.body["texts"]) for call in fake.calls] == [1000, 1000, 500] * 2
    assert [r.text for r in restored] == texts
    assert [r.text.split(":")[0] for r in masked] == [f"line {n}" for n in range(2500)]


def test_batches_stay_under_max_bytes(fake):
    texts = [f"{n:04d} " + "é" * 200 for n in range(60)]  # two bytes a letter in UTF-8
    with fake.client(max_bytes=5000) as client:
        scanned = client.scan(texts)
    assert len(scanned) == 60
    assert len(fake.calls) > 1
    assert all(len(call.content) <= 5000 for call in fake.calls)
    assert [text for call in fake.calls for text in call.body["texts"]] == texts


def test_a_text_past_max_bytes_goes_alone(fake):
    texts = ["small", "x" * 3000, "another small one"]
    with fake.client(max_bytes=1000) as client:
        client.mask(texts)
    assert [call.body["texts"] for call in fake.calls] == [["small"], ["x" * 3000], ["another small one"]]


def test_max_texts(fake):
    with fake.client(max_texts=3) as client:
        client.scan([str(n) for n in range(7)])
    assert [len(call.body["texts"]) for call in fake.calls] == [3, 3, 1]


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, Unauthorized), (403, Forbidden), (413, TooLarge), (507, VaultFull), (503, Unavailable)],
)
def test_errors(fake, slept, status, kind):
    fake.scripted = [error(status, "the server's own words")] * 5
    with fake.client() as client, pytest.raises(kind) as raised:
        client.mask("hello")
    assert type(raised.value) is kind
    assert raised.value.status == status and raised.value.message == "the server's own words"
    assert isinstance(raised.value, LatetError) and "the server's own words" in str(raised.value)


@pytest.mark.parametrize("status", [400, 404, 405, 422, 500])
def test_another_refusal_is_a_plain_error(fake, status):
    fake.scripted = [error(status, "words about the call")]
    with fake.client() as client, pytest.raises(LatetError) as raised:
        client.scan("hello")
    assert type(raised.value) is LatetError and raised.value.status == status
    assert raised.value.message == "words about the call"


def test_an_answer_without_words(fake):
    fake.scripted = [error(502), httpx.Response(500, text="<html>no JSON here</html>"), error(401, "x" * 2000)]
    with fake.client() as client:
        with pytest.raises(LatetError, match="HTTP 502") as bad_gateway:
            client.scan("hello")
        with pytest.raises(LatetError) as page:
            client.scan("hello")
        with pytest.raises(Unauthorized) as long:
            client.scan("hello")
    assert bad_gateway.value.status == 502
    assert page.value.message == "HTTP 500" and "html" not in str(page.value)
    assert len(long.value.message) <= 500


def test_a_redirect_is_not_followed_and_says_so(fake):
    fake.scripted = [httpx.Response(301, headers={"location": "https://latet.acme.lan/_latet/apps/v1/mask"})]
    with fake.client() as client, pytest.raises(LatetError) as raised:
        client.mask("hello")
    assert raised.value.status == 301 and "redirected" in raised.value.message
    assert len(fake.calls) == 1 and TOKEN not in str(raised.value)


def test_a_refusal_ends_the_call(fake):
    fake.scripted = [error(507, "full")]
    with fake.client(max_texts=2) as client, pytest.raises(VaultFull):
        client.mask(["a", "b", "c"])
    assert len(fake.calls) == 1  # nothing after a refusal


def test_retries_after_429_then_succeeds(fake, slept):
    fake.scripted = [error(429, "too many refused", **{"retry-after": "2"})]
    with fake.client() as client:
        masked = client.mask(f"to {MAIL}")
    assert masked.text == "to person1@masked.test"
    assert slept == [2.0]
    assert len(fake.calls) == 2 and fake.calls[0].content == fake.calls[1].content


def test_a_503_is_retried_too(fake, slept):
    fake.scripted = [error(503, "a store does not answer", **{"retry-after": "1"})] * 2
    with fake.client() as client:
        assert client.scan("hello").counts == {}
    assert slept == [1.0, 1.0]


def test_a_wait_is_thirty_seconds_at_most(fake, slept):
    fake.scripted = [error(429, "slow down", **{"retry-after": "3600"})]
    with fake.client() as client:
        client.scan("hello")
    assert slept == [30.0]


def test_without_retry_after_the_wait_grows(fake, slept):
    fake.scripted = [error(503, "later")] * 3
    with fake.client() as client:
        client.scan("hello")
    assert slept == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("given", ["soon", "-5", "", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_a_retry_after_that_is_not_seconds_is_not_believed(fake, slept, given):
    fake.scripted = [error(429, "slow down", **{"retry-after": given})]
    with fake.client() as client:
        client.scan("hello")
    assert slept == [1.0]


def test_gives_up_after_retries(fake, slept):
    fake.scripted = [error(429, "too many", **{"retry-after": "3"})] * 10
    with fake.client(retries=3) as client, pytest.raises(RateLimited) as raised:
        client.scan("hello")
    assert raised.value.status == 429 and raised.value.retry_after == 3.0
    assert len(fake.calls) == 4 and slept == [3.0, 3.0, 3.0]


def test_no_retries(fake, slept):
    fake.scripted = [error(503, "later")]
    with fake.client(retries=0) as client, pytest.raises(Unavailable):
        client.scan("hello")
    assert len(fake.calls) == 1 and slept == []


def test_a_429_without_retry_after_says_none(fake, slept):
    fake.scripted = [error(429, "too many")] * 2
    with fake.client(retries=1) as client, pytest.raises(RateLimited) as raised:
        client.scan("hello")
    assert raised.value.retry_after is None


def test_other_errors_are_not_retried(fake, slept):
    fake.scripted = [error(401, "latet: unauthorized")]
    with fake.client() as client, pytest.raises(Unauthorized):
        client.scan("hello")
    assert len(fake.calls) == 1 and slept == []


def test_the_token_is_in_no_message_and_no_repr(fake, caplog):
    fake.scripted = [error(401)]  # an answer whose body is empty
    caplog.set_level(logging.DEBUG)
    with fake.client() as client:
        with pytest.raises(Unauthorized) as raised:
            client.mask("hello")
        shown = [str(raised.value), repr(raised.value), repr(raised.value.args), repr(client), str(client)]
    assert all(TOKEN not in text and TOKEN[10:] not in text for text in shown)
    assert URL in repr(client)
    assert TOKEN not in caplog.text


def test_a_server_that_echoes_the_token_is_not_repeated(fake):
    fake.scripted = [error(401, f"no such token: {TOKEN}")]
    with fake.client() as client, pytest.raises(Unauthorized) as raised:
        client.mask("hello")
    assert TOKEN not in str(raised.value) and TOKEN not in raised.value.message


def test_no_token_in_what_the_log_says_of_a_retry(fake, slept, caplog):
    fake.scripted = [error(429, "wait", **{"retry-after": "1"})]
    caplog.set_level(logging.DEBUG, logger="latet_client")
    with fake.client() as client:
        client.mask(f"to {MAIL}")
    assert TOKEN not in caplog.text and MAIL not in caplog.text


@pytest.mark.parametrize("token", ["", "   ", "two words", "a\ttab", "café", 7])
def test_a_token_a_header_cannot_carry_is_refused_without_being_quoted(token):
    with pytest.raises((ValueError, TypeError)) as raised:
        Latet(URL, token)
    assert str(token).strip() == "" or str(token) not in str(raised.value)


def test_a_token_read_from_a_file_loses_its_line_break(fake):
    with Latet(URL, TOKEN + "\r\n", client=httpx.Client(transport=httpx.MockTransport(fake))) as client:
        client.scan("hello")
    assert fake.calls[0].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("url", ["", "latet.acme.lan", "ftp://latet.acme.lan", "https://"])
def test_an_address_that_is_none_is_refused(url):
    with pytest.raises(ValueError, match="address"):
        Latet(url, TOKEN)


def test_the_address_is_the_servers_with_or_without_a_slash(fake):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return fake(request)

    for url in (URL, URL + "/", "http://127.0.0.1:5283"):
        with Latet(url, TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            client.scan("hello")
    assert seen == [
        f"{URL}/_latet/apps/v1/scan",
        f"{URL}/_latet/apps/v1/scan",
        "http://127.0.0.1:5283/_latet/apps/v1/scan",
    ]
    with Latet(
        "https://latet.acme.lan/proxy/", TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        client.scan("hello")
    assert seen[-1] == "https://latet.acme.lan/proxy/_latet/apps/v1/scan"


def test_an_address_with_a_password_is_refused_without_being_quoted():
    with pytest.raises(ValueError, match="address") as raised:
        Latet("https://person:hunter2@latet.acme.lan:8443/", TOKEN)
    assert "hunter2" not in str(raised.value) and "person" not in str(raised.value)


def test_the_repr_is_the_address_without_a_slash_or_a_query():
    client = Latet("https://latet.acme.lan:8443/proxy/?a=b", TOKEN)
    assert repr(client) == "Latet(url='https://latet.acme.lan:8443/proxy')"
    client.close()


def test_blocked_item(fake):
    with fake.client() as client:
        results = client.mask(["fine", "BLOCK this one", f"to {MAIL}"])
    assert results[1] == MaskResult(text=None, blocked="the local reviewer stopped this text")
    assert results[1].replacements == [] and results[1].counts == {}
    assert results[0].blocked is None and results[0].text == "fine"
    assert results[2].text == "to person1@masked.test"


def test_what_ran_weaker_is_said_on_every_result_of_the_call(fake):
    fake.degraded = ["llm-review"]
    with fake.client() as client:
        masked = client.mask(["one", "BLOCK", "three"])
        scanned = client.scan("one")
    assert [r.degraded for r in masked] == [("llm-review",)] * 3
    assert not hasattr(scanned, "degraded")


def test_nothing_ran_weaker(fake):
    with fake.client() as client:
        assert client.mask("one").degraded == ()


@pytest.mark.parametrize(
    "answer",
    [
        {"nothing": []},
        {"results": "text"},
        {"results": []},  # one text in, no result out
        {"results": [{"text": "x"}, {"text": "y"}]},  # one text in, two out
        {"results": [{"findings": "no", "counts": {}}]},
        {"results": [{"findings": [{"type": "EMAIL"}], "counts": {}}]},
        [1, 2],
    ],
)
def test_an_answer_that_is_not_the_servers_is_an_error(fake, answer):
    fake.scripted = [httpx.Response(200, json=answer)]
    with fake.client() as client, pytest.raises(LatetError) as raised:
        client.scan("hello")
    assert raised.value.status == 200 and TOKEN not in str(raised.value)


def test_an_answer_that_is_not_json_is_an_error(fake):
    fake.scripted = [httpx.Response(200, text="<html>a proxy's page</html>")]
    with fake.client() as client, pytest.raises(LatetError) as raised:
        client.mask("hello")
    assert raised.value.status == 200 and "proxy" not in str(raised.value)


def test_texts_are_texts(fake):
    with fake.client() as client:
        for bad in (b"bytes", ["fine", 3], 3, None):
            with pytest.raises(TypeError):
                client.mask(bad)  # type: ignore[arg-type]
    assert fake.calls == []


def test_a_sequence_that_is_not_a_list(fake):
    with fake.client() as client:
        assert len(client.scan(("a", "b"))) == 2
        assert [r.text for r in client.unmask(iter(["c"]))] == ["c"]  # type: ignore[arg-type]


def test_the_body_is_compact_utf8(fake):
    with fake.client() as client:
        client.scan(["café ✉"])
    assert fake.calls[0].content == '{"texts":["café ✉"],"kind":"prose"}'.encode()
    assert json.loads(fake.calls[0].content)["texts"] == ["café ✉"]


def test_close_closes_the_client_it_made_and_not_the_one_it_was_given(fake):
    given = httpx.Client(transport=httpx.MockTransport(fake))
    Latet(URL, TOKEN, client=given).close()
    assert not given.is_closed
    with Latet(URL, TOKEN, client=given) as client:
        client.scan("hello")
    assert not given.is_closed
    given.close()
    own = Latet(URL, TOKEN)
    own.close()
    assert own._http.is_closed
    with pytest.raises(RuntimeError):
        own.scan("hello")


def test_the_default_timeout_outlasts_the_servers_own_bounds():
    """The final review's M7: a call that waits on PostgreSQL meets the server's bounds (a statement 60 s, an answer
    75 s) and is answered 503, which the client tries again; a client that gave up at 60 s raised ConnectionFailed
    instead, never tried again. Its default waits 90 s."""
    import inspect

    for kind in (Latet, AsyncLatet):
        assert inspect.signature(kind.__init__).parameters["timeout"].default == 90.0


def test_every_call_has_the_timeout_of_the_client(fake):
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return fake(request)

    with Latet(URL, TOKEN, timeout=7.5, client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        client.scan("hello")
    assert seen == [{"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}]
