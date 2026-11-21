"""Microsoft Entra ID through Microsoft Graph, against a fake of both (httpx.MockTransport, or a server of the test's own
on 127.0.0.1): no network."""

import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from latet.ee.settings import GraphSource
from latet.ee.sync import graph as graph_module
from latet.ee.sync import records as records_module
from latet.ee.sync import sources as sources_module
from latet.ee.sync.graph import REFUSAL_BYTES, _error_code, _retry_after, read_graph
from latet.ee.sync.records import SourceError, bounded, terms_of
from latet.ee.sync.sources import readers

SECRET = "graph-" + "client-secret-4b1d"
LOGIN = "https://login.microsoftonline.com/acme.onmicrosoft.com/oauth2/v2.0/token"
PEOPLE = "/v1.0/users", "/v1.0/groups/g-employees/transitiveMembers/microsoft.graph.user"
PAGES = [
    [{"displayName": "Emily Carter", "mail": "emily.carter@acme.com"}],
    [{"displayName": "Noah Brennan", "mail": None}],
]


class FakeGraph:
    """Microsoft Entra ID's token endpoint and Microsoft Graph, as the client credentials flow and paging use them."""

    def __init__(self, throttle: int = 0) -> None:
        self.throttle = throttle
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == LOGIN:
            form = parse_qs(request.content.decode())
            if form.get("client_secret") != [SECRET]:
                description = "AADSTS7000215: Invalid client secret provided for app 6f1c2b9e in tenant acme"
                return httpx.Response(401, json={"error": "invalid_client", "error_description": description})
            assert form["grant_type"] == ["client_credentials"]
            assert form["scope"] == ["https://graph.microsoft.com/.default"]
            return httpx.Response(200, json={"access_token": "graph-token", "token_type": "Bearer", "expires_in": 3599})
        assert request.headers["authorization"] == "Bearer graph-token"
        if self.throttle:
            self.throttle -= 1
            return httpx.Response(429, headers={"retry-after": "2"})
        path = request.url.path
        if path == "/v1.0/groups":
            return httpx.Response(200, json={"value": [{"displayName": "Payments Team"}]})
        if path in PEOPLE and "$skiptoken" not in request.url.params:
            more = f"https://graph.microsoft.com{path}?$skiptoken=2"
            return httpx.Response(200, json={"value": PAGES[0], "@odata.nextLink": more})
        if path in PEOPLE:
            return httpx.Response(200, json={"value": PAGES[1]})
        return httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound", "message": "no such thing"}})


def source(**changes):
    settings = {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
                "mapping": {"displayName": "PERSON", "mail": "EMAIL"}}  # fmt: skip
    return GraphSource.model_validate(settings | changes)


def read(fake, src=None, password=SECRET, sleeps=None):
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        sleep = sleeps.append if sleeps is not None else lambda seconds: None
        return read_graph(src or source(), lambda: password, client=client, sleep=sleep)


def test_people_are_read_page_by_page_with_the_fields_the_mapping_names():
    fake = FakeGraph()
    terms = terms_of(source(), read(fake)).terms
    assert terms == [("Emily Carter", "PERSON"), ("emily.carter@acme.com", "EMAIL"), ("Noah Brennan", "PERSON")]
    assert fake.requests[1].url.params["$select"] == "displayName,mail"


def test_a_groups_members_and_the_groups_names():
    assert [r["displayName"] for r in read(FakeGraph(), source(group="g-employees"))] == [
        "Emily Carter",
        "Noah Brennan",
    ]
    groups = source(objects="groups", mapping={"displayName": "TEAM"})
    assert terms_of(groups, read(FakeGraph(), groups)).terms == [("Payments Team", "TEAM")]


def test_a_filter_is_sent_as_an_advanced_query():
    fake = FakeGraph()
    read(fake, source(filter="accountEnabled eq true"))
    query = fake.requests[1]
    assert (query.url.params["$filter"], query.url.params["$count"]) == ("accountEnabled eq true", "true")
    assert query.headers["consistencylevel"] == "eventual"


def test_a_throttled_answer_is_asked_again_after_the_time_graph_asks():
    sleeps: list[float] = []
    assert len(read(FakeGraph(throttle=2), sleeps=sleeps)) == 2
    assert sleeps == [2.0, 2.0]


def test_credentials_entra_refuses_fail_with_their_code_never_their_description():
    with pytest.raises(SourceError) as caught:
        read(FakeGraph(), password="wrong")
    assert caught.value.code == "refused" and "invalid_client" in caught.value.message
    assert "AADSTS" not in caught.value.message and "6f1c2b9e" not in caught.value.message


def test_a_secret_never_kept_names_the_command_that_keeps_it():
    with pytest.raises(SourceError, match="latet server terms secret colleagues") as caught:
        read(FakeGraph(), password=None)
    assert caught.value.code == "no-secret"


def test_a_next_link_to_another_host_is_never_followed_with_the_token():
    class Elsewhere(FakeGraph):
        def __call__(self, request):
            answer = super().__call__(request)
            if request.url.path == "/v1.0/users" and "$skiptoken" not in request.url.params:
                return httpx.Response(
                    200,
                    json={"value": PAGES[0], "@odata.nextLink": "https://graph.evil.example/v1.0/users?$skiptoken=2"},
                )
            return answer

    fake = Elsewhere()
    with pytest.raises(SourceError, match="another address") as caught:
        read(fake)
    assert caught.value.code == "unreadable" and all(r.url.host != "graph.evil.example" for r in fake.requests)


def test_the_token_is_asked_for_graph_url_and_goes_to_it_alone():
    """The design security review's B-M6: the token's scope is derived from `graph_url` (which the secret is bound to),
    and every request after the token's goes to `graph_url`'s host."""
    fake = FakeGraph()
    read(fake)
    token, *pages = fake.requests
    assert token.url.path.endswith("/oauth2/v2.0/token")
    assert parse_qs(token.content.decode())["scope"] == ["https://graph.microsoft.com/.default"]
    assert pages and {request.url.host for request in pages} == {"graph.microsoft.com"}


def test_the_token_and_the_pages_go_to_the_origins_endpoints_whatever_the_addresses_text():
    """Task 8's review, behind the settings that refuse them: the token's address, its scope and the pages' are built
    from each address's scheme, host and port as httpx reads them, so that a query or a fragment its text may hold
    (written before the settings refused them) never moves the token's request or the pages."""
    fake = FakeGraph()
    odd = source().model_copy(
        update={"login_url": "https://login.microsoftonline.com/?#", "graph_url": "https://Graph.Microsoft.com:443?"}
    )
    assert len(read(fake, odd)) == 2
    token, first, second = fake.requests
    assert str(token.url) == LOGIN  # FakeGraph checks the scope too: https://graph.microsoft.com/.default
    assert (first.url.host, first.url.path) == ("graph.microsoft.com", "/v1.0/users")
    assert second.url.params["$skiptoken"] == "2"


def test_an_address_that_is_no_origin_sends_nothing():
    fake = FakeGraph()
    for odd in (
        {"login_url": "https://user@login.microsoftonline.com"},
        {"graph_url": "https://graph.microsoft.com:x"},
    ):
        with pytest.raises(SourceError, match="not an origin") as caught:
            read(fake, source().model_copy(update=odd))
        said = caught.value.message
        assert caught.value.code == "unreadable" and "microsoftonline" not in said and "graph.microsoft" not in said
    assert fake.requests == []


def test_a_run_reads_graph_within_its_deadline_and_its_bytes(monkeypatch):
    """The design security review's B-I1 and B-M5: each request and each page is checked against the run's deadline,
    and every byte of every answer is taken from the run's budget (`records.bounded`)."""
    monkeypatch.setattr(records_module, "MAX_BYTES", 64)  # less than the pages hold together
    with bounded(float("inf"), lambda: 0.0), pytest.raises(SourceError) as caught:
        read(FakeGraph())
    assert caught.value.code == "too-large"
    with bounded(-1.0, lambda: 0.0), pytest.raises(SourceError) as caught:  # its turn over before the first request
        read(FakeGraph())
    assert caught.value.code == "timeout"


def test_a_refusal_is_read_for_its_code_within_a_cap():
    """B-R3 and A-R3: a refusal's body is read under the run's budget and at most `REFUSAL_BYTES`, for its code alone
    (`_error_code` parses the bytes `_send` read); the rest is never read, its description never quoted."""

    class Refusing(FakeGraph):
        def __call__(self, request):
            body = {"error": "invalid_client", "error_description": "AADSTS7000215 " + "x" * (1 << 20)}
            return httpx.Response(401, json=body)

    with bounded(float("inf"), lambda: 0.0) as limits, pytest.raises(SourceError) as caught:
        read(Refusing())
    assert caught.value.code == "refused" and "401" in caught.value.message and "AADSTS" not in caught.value.message
    assert records_module.MAX_BYTES - limits.left_bytes <= REFUSAL_BYTES


# --- the root's rulings and Task 7's carries ---------------------------------------------------------------------------


def test_field_names_are_encoded_and_none_adds_a_parameter_or_a_field():
    """A field the mapping names holding `&`, `,` or `%` stays one field of `$select`: it adds no parameter (no
    `$expand`, no second `$top`) and splits into no second field; a filter holding `&` stays the filter's."""
    mapping = {"displayName": "PERSON", "mail&$expand=manager": "EMAIL", "id,mobilePhone": "PHONE",
               "x%26$top=1": "CODE"}  # fmt: skip
    fake = FakeGraph()
    read(fake, source(mapping=mapping, filter="department eq 'R&D'"))
    page = fake.requests[1].url
    assert [name for name, _ in page.params.multi_items()] == ["$select", "$top", "$filter", "$count"]
    assert page.params["$top"] == "999" and page.params["$filter"] == "department eq 'R&D'"
    selected = dict(part.split(b"=", 1) for part in page.query.split(b"&"))[b"$select"].split(b",")
    assert selected == [b"displayName", b"id%2CmobilePhone", b"mail%26%24expand%3Dmanager", b"x%2526%24top%3D1"]


def test_each_read_has_a_pinned_client_of_its_own_for_a_public_address_closed_after(monkeypatch):
    """Task 7's carry: one pinned client for each read, never reused across runs, holding egress's lookups for this
    source, Microsoft's clouds public (`inside=False`); closed when the read ends."""
    made = []

    def pinned(holder, *, inside):
        client = sources_module.pinned_client(holder, inside=inside, transport=httpx.MockTransport(FakeGraph()))
        made.append((holder, inside, client))
        return client

    async def pin(url, *, holder=None, inside=False):
        return url, httpx.URL(url).host, {}

    monkeypatch.setattr(graph_module, "pinned_client", pinned)
    monkeypatch.setattr(sources_module.egress, "pin", pin)
    assert len(read_graph(source(), lambda: SECRET)) == 2
    assert len(read_graph(source(), lambda: SECRET)) == 2
    assert [(holder, inside) for holder, inside, _ in made] == [("term-sync:colleagues", False)] * 2
    assert made[0][2] is not made[1][2] and all(client.is_closed for *_, client in made)


def test_an_address_egress_refuses_fails_naming_no_host(monkeypatch):
    """A refusal of egress (raised by the pinned client) passes as it is: `unreachable`, in fixed words (B-M4)."""

    async def refuse(url, *, holder=None, inside=False):
        raise sources_module.egress.EgressRefused("login.microsoftonline.com leads into the server's own network")

    monkeypatch.setattr(sources_module.egress, "pin", refuse)
    with pytest.raises(SourceError) as caught:
        read_graph(source(), lambda: SECRET)
    assert caught.value.code == "unreachable" and "microsoftonline" not in caught.value.message


@pytest.mark.parametrize(
    "link",
    [
        "http://graph.microsoft.com/v1.0/users?$skiptoken=2",  # another scheme
        "https://graph.microsoft.com:8443/v1.0/users?$skiptoken=2",  # another port
        "https://user@graph.microsoft.com/v1.0/users?$skiptoken=2",  # a user's name in it
        "https://graph.microsoft.com:x/v1.0/users",  # no address at all
        "/v1.0/users?$skiptoken=2",  # no host
    ],
)
def test_a_next_link_on_any_other_origin_is_never_followed(link):
    class Elsewhere(FakeGraph):
        def __call__(self, request):
            answer = super().__call__(request)
            if request.url.path == "/v1.0/users" and "$skiptoken" not in request.url.params:
                return httpx.Response(200, json={"value": PAGES[0], "@odata.nextLink": link})
            return answer

    fake = Elsewhere()
    with pytest.raises(SourceError, match="another address") as caught:
        read(fake)
    assert caught.value.code == "unreadable" and len(fake.requests) == 2 and "8443" not in caught.value.message


def test_a_next_link_on_graph_url_s_own_origin_is_followed_whatever_its_case_or_default_port():
    class Same(FakeGraph):
        def __call__(self, request):
            answer = super().__call__(request)
            if request.url.path == "/v1.0/users" and "$skiptoken" not in request.url.params:
                link = "https://GRAPH.microsoft.com:443/v1.0/users?$skiptoken=2"
                return httpx.Response(200, json={"value": PAGES[0], "@odata.nextLink": link})
            return answer

    assert len(read(Same())) == 2


def test_no_redirect_is_followed_whatever_the_client_given():
    sent = []

    def moved(request):
        sent.append(request)
        return httpx.Response(302, headers={"location": "https://login.evil.example/token"})

    with (
        httpx.Client(transport=httpx.MockTransport(moved), follow_redirects=True) as client,
        pytest.raises(SourceError, match="302") as caught,
    ):
        read_graph(source(), lambda: SECRET, client=client)
    assert caught.value.code == "unreachable" and len(sent) == 1 and "evil" not in caught.value.message


def test_no_compression_is_asked_for_and_a_compressed_answer_is_refused():
    """As a url source's (B-M5): a few compressed bytes can stand for gigabytes, which a bound counted on what is
    decoded sees only once they are in memory."""
    asked = []

    def compressed(request):
        asked.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"\x1f\x8b")

    with (
        httpx.Client(transport=httpx.MockTransport(compressed)) as client,
        pytest.raises(SourceError, match="compressed") as caught,
    ):
        read_graph(source(), lambda: SECRET, client=client)
    assert asked == ["identity"] and caught.value.code == "unreadable"


@pytest.mark.parametrize(
    "answer",
    [b"[]", b"not json", b'{"value": [', b'"graph-token"', b"\xff\xfe\x00"],
    ids=["list", "text", "cut", "string", "bytes"],
)
def test_an_answer_that_is_no_json_object_is_unreadable_and_never_quoted(answer):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=answer))) as client,
        pytest.raises(SourceError, match="no JSON object") as caught,
    ):
        read_graph(source(), lambda: SECRET, client=client)
    assert caught.value.code == "unreadable" and "graph-token" not in caught.value.message


@pytest.mark.parametrize(
    "page",
    [
        {"value": 5},
        {"value": "Emily Carter"},
        {"value": {"displayName": "Emily Carter"}},
        {"displayName": "Emily Carter"},  # Task 8's review: no `value` at all, as a misaddressed 200 answers
        {},
    ],
    ids=["a number", "a text", "an object", "no value", "nothing"],
)
def test_a_page_with_no_list_of_records_is_unreadable_and_never_quoted(page):
    """Graph sends `value` with every page of a collection, an empty list when there is nothing: a page without it is
    no page of records, never one of none (which would mark every synced term as gone from the source)."""

    class Odd(FakeGraph):
        def __call__(self, request):
            answer = super().__call__(request)
            return answer if request.url.path.endswith("/token") else httpx.Response(200, json=page)

    with pytest.raises(SourceError, match="no list of records") as caught:
        read(Odd())
    assert caught.value.code == "unreadable" and "Emily" not in caught.value.message


@pytest.mark.parametrize("token", [None, "", 42, "graph-token\r\nX-Injected: 1", "graph token"])
def test_a_token_that_cannot_go_in_a_header_is_none(token):
    sent = []

    def entra(request):
        sent.append(request)
        return httpx.Response(200, json={"access_token": token, "token_type": "Bearer"})

    with (
        httpx.Client(transport=httpx.MockTransport(entra)) as client,
        pytest.raises(SourceError, match="gave no token") as caught,
    ):
        read_graph(source(), lambda: SECRET, client=client)
    assert caught.value.code == "refused" and len(sent) == 1 and "Injected" not in caught.value.message


@pytest.mark.parametrize(
    ("body", "said"),
    [
        (b'{"error": "invalid_client", "error_description": "AADSTS7000215 tenant acme"}', " (invalid_client)"),
        (
            b'{"error": {"code": "Authorization_RequestDenied", "message": "Emily Carter"}}',
            " (Authorization_RequestDenied)",
        ),
        (b'{"error": "invalid_client", "error_description": "AADSTS', ""),  # cut at the cap: no JSON, no code
        (b'{"error": {"code": "Emily Carter"}}', ""),  # a code is one word of a few letters, never a text
        (b'{"error": {"code": "' + b"a" * 65 + b'"}}', ""),
        (b'{"error": "\xc3\xa9t\xc3\xa9"}', ""),
        (b"<html>busy</html>", ""),
        (b"[1, 2]", ""),
    ],
)
def test_a_refusal_s_code_is_one_word_from_its_own_bytes(body, said):
    assert _error_code(body) == said


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("2", 2.0),
        ("0.5", 0.5),
        ("-3", 0.0),
        ("100000", 60.0),
        ("inf", 60.0),
        ("nan", 5.0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", 5.0),
        (None, 5.0),
    ],
)
def test_a_throttled_answer_waits_what_it_asks_within_a_minute(header, seconds):
    headers = {"retry-after": header} if header is not None else {}
    assert _retry_after(httpx.Response(429, headers=headers)) == seconds


def test_a_throttled_wait_ends_with_the_run_s_turn():
    """The wait a throttled answer asks for never passes the run's deadline, checked again after it (B-I1)."""
    clock = [100.0]
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    with (
        httpx.Client(transport=httpx.MockTransport(FakeGraph(throttle=1))) as client,
        bounded(101.5, lambda: clock[0]),
        pytest.raises(SourceError) as caught,
    ):
        read_graph(source(), lambda: SECRET, client=client, sleep=sleep)
    assert sleeps == [1.5] and caught.value.code == "timeout"


def test_the_deadline_is_checked_inside_the_reader_before_the_watchdog_fires():
    """B-I1: each answer that takes the run past its deadline ends the read at the reader's next check, whatever its
    watchdog (armed here for 300 real seconds, so that it never fires)."""
    clock = [0.0]
    fake = FakeGraph()

    def slow(request):
        clock[0] += 400  # each answer takes a long while by the run's clock
        return fake(request)

    with (
        httpx.Client(transport=httpx.MockTransport(slow)) as client,
        bounded(300.0, lambda: clock[0]),
        pytest.raises(SourceError) as caught,
    ):
        read_graph(source(), lambda: SECRET, client=client)
    assert caught.value.code == "timeout" and len(fake.requests) == 1


def test_an_answer_is_read_chunk_by_chunk_within_the_deadline():
    """B-I1: the deadline is checked between two chunks of an answer: one that passes it mid-answer is read no
    further."""
    clock = [0.0]
    pulled = []

    def body():
        for index in range(3):
            pulled.append(index)
            clock[0] += 200
            yield b" " * 8192  # a chunk as the reader reads them

    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body()))) as client,
        bounded(300.0, lambda: clock[0]),
        pytest.raises(SourceError) as caught,
    ):
        read_graph(source(), lambda: SECRET, client=client)
    assert caught.value.code == "timeout" and pulled == [0, 1]


def test_each_request_waits_no_longer_than_the_run_has_left():
    """As a url source's: every phase of each request is bounded by the time the run has left, never below a floor."""
    waits = []

    def answer(request):
        waits.append(set(request.extensions["timeout"].values()))
        return FakeGraph()(request)

    with httpx.Client(transport=httpx.MockTransport(answer)) as client, bounded(112.0, lambda: 100.0):
        read_graph(source(), lambda: SECRET, client=client)
    assert waits == [{12.0}] * 3


def test_the_server_reads_graph_sources():
    assert readers(Path("."))["graph"] is read_graph


# --- Task 7's carry: the whole read under the deadline's watchdog --------------------------------------------------------


class _Trickling:
    """A server on 127.0.0.1 that answers ``answered`` connections whole (a token, `Connection: close`), then answers
    the next 200 and sends its headers a byte every 50 ms, well within each socket read's timeout, until ``stop`` is
    set or ``seconds`` pass."""

    TOKEN = b'{"access_token": "graph-token", "token_type": "Bearer"}'

    def __init__(self, answered: int, seconds: float) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(10)
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.connections = 0
        self.thread = threading.Thread(target=self._serve, args=(answered, seconds), daemon=True)
        self.thread.start()

    def _serve(self, answered: int, seconds: float) -> None:
        try:
            for _ in range(answered):
                conn, _ = self.listener.accept()
                with conn:
                    self.connections += 1
                    _request(conn)  # whole: a socket closed with bytes unread is reset, its answer lost
                    head = f"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {len(self.TOKEN)}\r\n"
                    conn.sendall(head.encode() + b"connection: close\r\n\r\n" + self.TOKEN)
            conn, _ = self.listener.accept()
            with conn:
                self.connections += 1
                _request(conn)
                conn.sendall(b"HTTP/1.1 200 OK\r\nx-trickle")
                ends = time.monotonic() + seconds
                while not self.stop.wait(0.05) and time.monotonic() < ends:
                    conn.sendall(b"e")  # a header's name, one byte at a time
        except OSError:  # the client cut the connection, or never came
            pass

    def close(self) -> None:
        self.stop.set()
        self.thread.join(10)
        self.listener.close()


def _request(conn: socket.socket) -> None:
    """One request read whole: its head, and the bytes of body its content-length says."""
    data = b""
    while b"\r\n\r\n" not in data:
        got = conn.recv(65536)
        if not got:
            return
        data += got
    head, _, body = data.partition(b"\r\n\r\n")
    sizes = [line.split(b":", 1)[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")]
    length = int(sizes[0]) if sizes else 0
    while len(body) < length:
        got = conn.recv(65536)
        if not got:
            return
        body += got


@pytest.mark.parametrize(
    ("answered", "until", "had"),
    [(0, 100.5, "the 1 second"), (1, 103.0, "the 3 seconds")],  # a page: a whole token's exchange in time, loaded too
    ids=["the token", "a page"],
)
def test_an_answer_that_trickles_in_ends_at_the_run_s_deadline(monkeypatch, answered, until, had):
    """Task 7's carry: the token's request and every page are read within `cut_at_deadline`, so that an answer whose
    headers come a byte at a time (no socket's timeout passes) is cut at the run's deadline and fails `timeout`. The
    trickle lasts 8 seconds: a read that ends sooner was cut."""
    server = _Trickling(answered, seconds=8.0)

    async def pin(url, *, holder=None, inside=False):
        return f"http://127.0.0.1:{server.port}{httpx.URL(url).raw_path.decode()}", httpx.URL(url).host, {}

    monkeypatch.setattr(sources_module.egress, "pin", pin)
    began = time.monotonic()
    try:  # by a clock that stands still: only the watchdog can end the read
        with bounded(until, lambda: 100.0), pytest.raises(SourceError) as caught:
            read_graph(source(), lambda: SECRET)
        took = time.monotonic() - began
    finally:
        server.close()
    assert server.connections == answered + 1  # the token answered first: a page's answer was cut
    assert caught.value.code == "timeout" and took < 7.0
    assert caught.value.message == f"the source took longer than {had} its run had: narrow it"
