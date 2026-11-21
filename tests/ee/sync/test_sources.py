"""Reading a source's records: a file inside the term-sync folder and nowhere else, an address pinned by egress with
its own secret, and every refusal saying why without a value."""

import gzip
import json
import socket
import threading
import time
import tracemalloc

import httpcore
import httpx
import pytest

from latet.ee.settings import FileSource, UrlSource
from latet.ee.sync import records as records_module
from latet.ee.sync import sources as sources_module
from latet.ee.sync.records import SourceError, bounded, terms_of
from latet.ee.sync.sources import pinned_client, read_file, read_url

from .. import addresses

URL = UrlSource(name="projects", kind="url", url="https://intranet.acme.com/api/projects", records="items",
                headers={"Authorization": "Bearer {secret}"}, mapping={"code_name": "PROJECT"})  # fmt: skip
TOKEN = "intranet-" + "7f3a9c"


def test_a_file_inside_the_folder_is_read_and_one_outside_is_refused(tmp_path, monkeypatch):
    folder = tmp_path / "term-sync"
    folder.mkdir()
    (folder / "customers.csv").write_bytes(b"company,contact\nHalden Insurance,Grace Whitfield\n")
    source = FileSource(name="customers", kind="file", path="customers.csv", mapping={"company": "CLIENT"})
    assert list(read_file(source, lambda: None, folder=folder)) == [{"company": "Halden Insurance"}]
    with pytest.raises(SourceError, match="cannot be read") as caught:
        read_file(source.model_copy(update={"path": "gone.csv"}), lambda: None, folder=folder)
    assert caught.value.code == "unreachable" and "gone.csv" not in caught.value.message  # B-M4: no path in a reason
    monkeypatch.setattr(sources_module, "MAX_BYTES", 8)  # fix round 2's N4: a file over the bound is too large
    with pytest.raises(SourceError, match="holds more than") as caught:
        read_file(source, lambda: None, folder=folder)
    assert caught.value.code == "too-large"
    monkeypatch.undo()
    (tmp_path / "outside.csv").write_bytes(b"company\nKestrel Analytics\n")
    try:
        (folder / "link.csv").symlink_to(tmp_path / "outside.csv")
    except OSError:
        pytest.skip("this system makes no symbolic link here")
    with pytest.raises(SourceError, match="inside") as caught:
        read_file(source.model_copy(update={"path": "link.csv"}), lambda: None, folder=folder)
    assert "Kestrel" not in caught.value.message


def _intranet(status: int = 200) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401)
        if status != 200:
            return httpx.Response(status, headers={"location": "http://169.254.169.254/"})
        return httpx.Response(200, json={"items": [{"code_name": "Project Falcon"}]})

    return httpx.MockTransport(answer)


def test_an_address_is_read_with_its_own_secret_where_the_source_says():
    with httpx.Client(transport=_intranet()) as client:
        assert read_url(URL, lambda: TOKEN, client=client) == [{"code_name": "Project Falcon"}]


def test_records_at_a_key_of_printable_unicode_are_read_from_a_file_and_an_address(tmp_path):
    """E6's deferred minor: a source whose records are at `données.éléments` reads them there, from a file as from an
    address; a key that holds no list fails in Latet's words, which do not quote it (visible ASCII alone is quoted)."""
    doc = {"données": {"éléments": [{"code_name": "Project Falcon"}]}}
    folder = tmp_path / "term-sync"
    folder.mkdir()
    (folder / "projects.json").write_bytes(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    source = FileSource(name="projects", kind="file", path="projects.json", records="données.éléments",
                        mapping={"code_name": "PROJECT"})  # fmt: skip
    assert read_file(source, lambda: None, folder=folder) == [{"code_name": "Project Falcon"}]
    address = UrlSource.model_validate({**URL.model_dump(), "records": "données.éléments"})
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=doc))
    with httpx.Client(transport=transport) as client:
        assert read_url(address, lambda: TOKEN, client=client) == [{"code_name": "Project Falcon"}]
    with pytest.raises(SourceError) as caught:
        read_file(source.model_copy(update={"records": "données.lignes"}), lambda: None, folder=folder)
    assert caught.value.code == "unreadable" and caught.value.message == "no list of records at the place set"


@pytest.mark.parametrize(("status", "code"), [(403, "refused"), (500, "unreachable"), (302, "unreachable")])
def test_an_address_that_answers_otherwise_fails_with_its_status_and_no_redirect_is_followed(status, code):
    with httpx.Client(transport=_intranet(status)) as client, pytest.raises(SourceError, match=str(status)) as caught:
        read_url(URL, lambda: TOKEN, client=client)
    assert caught.value.code == code


def test_a_secret_never_kept_names_the_command_that_keeps_it():
    with pytest.raises(SourceError, match="latet server terms secret projects") as caught:
        read_url(URL, lambda: None)
    assert caught.value.code == "no-secret"


def test_each_request_goes_to_the_address_egress_checked_with_the_host_written(monkeypatch):
    seen = []

    async def pin(url, *, holder=None, inside=False):
        seen.append((url, holder, inside))
        return url.replace("intranet.acme.com", "10.0.4.20"), "intranet.acme.com", {"sni_hostname": "intranet.acme.com"}

    monkeypatch.setattr(sources_module.egress, "pin", pin)
    sent = []
    inner = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200, json={"items": []}))
    client = pinned_client("term-sync:projects", inside=True, transport=inner)
    with client:
        client.get("https://intranet.acme.com/api/projects")
    assert seen == [("https://intranet.acme.com/api/projects", "term-sync:projects", True)]
    assert sent[0].url.host == "10.0.4.20" and sent[0].headers["host"] == "intranet.acme.com"


def test_an_address_the_server_does_not_reach_is_refused_naming_no_host(monkeypatch):
    """The design security review's B-M4: a run's reason is in Latet's fixed words, never a host or a path (the status,
    which shows it, is read without the company read the sources' addresses ask)."""

    async def refuse(url, *, holder=None, inside=False):
        raise sources_module.egress.EgressRefused("intranet.acme.com leads into the server's own network")

    monkeypatch.setattr(sources_module.egress, "pin", refuse)
    with pytest.raises(SourceError) as caught:
        read_url(URL, lambda: TOKEN)
    assert caught.value.code == "unreachable" and "intranet.acme.com" not in caught.value.message


def test_a_read_stops_at_its_deadline_and_at_the_run_s_bytes(monkeypatch):
    """The design security review's B-I1 and B-M5: a run reads under its deadline and one budget of bytes
    (`records.bounded`), checked inside the reader at each chunk."""
    clock = [0.0]

    def slow(request):
        clock[0] += 400  # each answer takes a long while: the deadline passes between two chunks
        return httpx.Response(200, json={"items": [{"code_name": "Project Falcon"}]})

    with (
        httpx.Client(transport=httpx.MockTransport(slow)) as client,
        bounded(300.0, lambda: clock[0]),
        pytest.raises(SourceError) as caught,
    ):
        read_url(URL, lambda: TOKEN, client=client)
    assert caught.value.code == "timeout"
    monkeypatch.setattr(records_module, "MAX_BYTES", 16)  # Bounds reads it when it is made: before `bounded`
    with (
        httpx.Client(transport=_intranet()) as client,
        bounded(float("inf"), lambda: 0.0),
        pytest.raises(SourceError) as caught,
    ):
        read_url(URL, lambda: TOKEN, client=client)
    assert caught.value.code == "too-large"


# --- beyond the brief: what the review of this task's code found -------------------------------------------------------


def test_an_address_is_asked_for_no_compression_and_a_compressed_answer_is_refused():
    """A few compressed bytes can stand for gigabytes, which a bound counted on what is decoded sees only once they are
    in memory (E3's identity provider is asked for none either)."""
    asked = []

    def compressed(request):
        asked.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=gzip.compress(b'{"items": []}'))

    with (
        httpx.Client(transport=httpx.MockTransport(compressed)) as client,
        pytest.raises(SourceError, match="compressed") as caught,
    ):
        read_url(URL, lambda: TOKEN, client=client)
    assert asked == ["identity"] and caught.value.code == "unreadable"


def test_no_redirect_is_followed_whatever_the_client_given():
    with (
        httpx.Client(transport=_intranet(302), follow_redirects=True) as client,
        pytest.raises(SourceError, match="302") as caught,
    ):
        read_url(URL, lambda: TOKEN, client=client)
    assert caught.value.code == "unreachable"


# --- fix round 1: the review of this task's code -------------------------------------------------------------------------


def test_a_secret_in_the_path_and_the_query_is_percent_encoded_and_moves_neither():
    """Fix round 1's 7: a secret holding / ? # @ : a space or % stays one segment and one value, on the host written."""
    source = UrlSource(name="projects", kind="url", url="https://intranet.acme.com/api/{secret}/projects?code={secret}",
                       records="items", mapping={"code_name": "PROJECT"})  # fmt: skip
    sent = []
    answer = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200, json={"items": []}))
    with httpx.Client(transport=answer) as client:
        assert read_url(source, lambda: "p/q?r#s@t:u v%w", client=client) == []
    escaped = b"p%2Fq%3Fr%23s%40t%3Au%20v%25w"
    assert sent[0].url.host == "intranet.acme.com"
    assert sent[0].url.raw_path == b"/api/" + escaped + b"/projects?code=" + escaped


def test_a_secret_that_cannot_go_in_a_header_is_refused_before_anything_is_sent():
    sent = []
    answer = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200, json={"items": []}))
    with httpx.Client(transport=answer) as client, pytest.raises(SourceError, match="cannot go in a header") as caught:
        read_url(URL, lambda: "intranet" + chr(13) + chr(10) + "X-Injected: 1", client=client)
    assert caught.value.code == "no-secret" and sent == [] and "Injected" not in caught.value.message


def test_each_request_waits_no_longer_than_the_run_has_left():
    """Fix round 1's 6: every phase of a request (connect, write, read, pool) is bounded by the time the run has left
    when it is sent, never below a floor, so no socket timeout outlives the turn."""
    waits = []

    def answer(request):
        waits.append(set(request.extensions["timeout"].values()))
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        read_url(URL, lambda: TOKEN, client=client)  # alone: the reader's own timeout
        with bounded(112.0, lambda: 100.0):
            read_url(URL, lambda: TOKEN, client=client)
        with bounded(100.2, lambda: 100.0):
            read_url(URL, lambda: TOKEN, client=client)
    assert waits == [{sources_module.TIMEOUT}, {12.0}, {sources_module.FLOOR}]


def test_a_file_over_the_bound_says_the_bound_itself(tmp_path, monkeypatch):
    """Fix round 1's 8: the number said is the bound, not one past it."""
    (tmp_path / "customers.csv").write_bytes(b"company\nHalden Insurance\n")
    monkeypatch.setattr(sources_module, "MAX_BYTES", 8)
    source = FileSource(name="customers", kind="file", path="customers.csv", mapping={"company": "CLIENT"})
    with pytest.raises(SourceError) as caught:
        read_file(source, lambda: None, folder=tmp_path)
    assert caught.value.message == "the source's file holds more than 8 bytes: not read"


# --- Task 7's fix round: an answer that trickles in --------------------------------------------------------------------


class _Trickling:
    """A server on 127.0.0.1 that answers 200, then sends its headers a byte every 50 ms, well within each socket read's
    timeout, until ``stop`` is set or ``seconds`` pass, and then the rest of a valid answer."""

    BODY = json.dumps({"items": [{"code_name": "Project Falcon"}]}).encode()

    def __init__(self, seconds: float) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, args=(seconds,), daemon=True)
        self.thread.start()

    def _serve(self, seconds: float) -> None:
        conn, _ = self.listener.accept()
        with conn:
            conn.recv(65536)
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\nx-trickle")
                ends = time.monotonic() + seconds
                while not self.stop.wait(0.05) and time.monotonic() < ends:
                    conn.sendall(b"e")  # a header's name, one byte at a time
                rest = f": 1\r\ncontent-type: application/json\r\ncontent-length: {len(self.BODY)}\r\n\r\n"
                conn.sendall(rest.encode() + self.BODY)
            except OSError:  # the client cut the connection
                pass

    def close(self) -> None:
        self.stop.set()
        self.thread.join(10)
        self.listener.close()


def test_an_answer_that_trickles_in_ends_at_the_run_s_deadline(monkeypatch):
    """Task 7's fix round: each socket read has its timeout, but an answer whose headers come a byte at a time never
    passes it, and the deadline is checked between two chunks of the body alone. The reader's watchdog cuts the
    connection at the deadline (a shutdown, which ends a read blocked in another thread where a close alone does not),
    and the read fails `timeout` in fixed words."""
    server = _Trickling(seconds=8.0)

    async def pin(url, *, holder=None, inside=False):
        return f"http://127.0.0.1:{server.port}/api/projects", "intranet.acme.com", {}

    monkeypatch.setattr(sources_module.egress, "pin", pin)
    try:  # half a second left, by a clock that stands still: only the watchdog can end the read
        with bounded(100.5, lambda: 100.0), pytest.raises(SourceError) as caught:
            read_url(URL, lambda: TOKEN)
    finally:
        server.close()
    assert caught.value.code == "timeout"
    assert caught.value.message == "the source took longer than the 1 second its run had: narrow it"


def test_a_connection_whose_socket_cannot_be_kept_for_the_cut_is_closed_at_once(monkeypatch):
    """The re-review's Minor: a connection the watchdog could not cut (its socket not duplicated: no descriptor left)
    is never left open; the connection fails."""

    class Opened:
        closed = False

        def get_extra_info(self, info):
            return self if info == "socket" else None

        def dup(self):
            raise OSError("too many open files")

        def close(self):
            self.closed = True

    opened = Opened()
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda self, *args, **kwargs: opened)
    with pytest.raises(OSError, match="too many open files"):
        sources_module._Sockets().connect_tcp("10.0.4.20", 443)
    assert opened.closed


# --- An address whose name has several: the next one its lookup checked, when the connection to the one before failed --

RECORDS = b'{"items": [{"code_name": "Project Falcon"}]}'


@pytest.fixture
def at_localhost(monkeypatch, tmp_path):
    """A source's server certificate for ``localhost`` alone, which the server's own store (truststore) trusts here."""
    cert, key = addresses.certificate(tmp_path / "localhost")
    monkeypatch.setattr(sources_module.truststore, "SSLContext", lambda protocol: addresses.trusting(cert))
    return addresses.serving(cert, key)


def _local(port: int) -> UrlSource:
    return URL.model_copy(update={"url": f"https://localhost:{port}/api/projects"})


def test_an_address_on_this_computer_is_read_at_the_next_address_its_lookup_checked(at_localhost, monkeypatch):
    """Windows resolves ``localhost`` to ``::1`` first, and a source in Docker listens on 127.0.0.1 alone: its records
    are read at 127.0.0.1 once ``::1`` refused the connection, TLS checked against ``localhost`` (the certificate
    names nothing else), its Host the one written and its secret with it; nothing is tried after."""
    server = addresses.Server(addresses.answering(RECORDS), tls=at_localhost)
    asked = addresses.resolving(monkeypatch, *addresses.LOCALHOST)
    connects = addresses.sync_connects(monkeypatch)
    try:
        with addresses.refused_on_v6(server.port):
            assert read_url(_local(server.port), lambda: TOKEN) == [{"code_name": "Project Falcon"}]
    finally:
        server.close()
    assert connects == ["::1", "127.0.0.1"] and asked == ["localhost"]
    [request] = server.requests
    assert f"\r\nhost: localhost:{server.port}\r\n".encode() in request.lower()
    assert f"\r\nauthorization: bearer {TOKEN}\r\n".encode() in request.lower()


def test_an_address_is_connected_at_no_address_its_lookup_did_not_check(at_localhost, monkeypatch):
    """The addresses tried are those egress's lookup checked, in its order, and nothing else; a name with an address
    the server never reaches among its answers is not read, nothing connected."""
    server = addresses.Server(addresses.answering(RECORDS), tls=at_localhost)
    connects = addresses.sync_connects(monkeypatch, refused=("::1", addresses.PAST))
    try:
        addresses.resolving(monkeypatch, *addresses.LOCALHOST)
        assert read_url(_local(server.port), lambda: TOKEN) == [{"code_name": "Project Falcon"}]
        assert connects == ["::1", "127.0.0.1"]
        connects.clear()
        addresses.resolving(monkeypatch, "::1", "127.0.0.1", addresses.METADATA)
        with pytest.raises(SourceError) as caught:
            read_url(_local(server.port), lambda: TOKEN)
    finally:
        server.close()
    assert caught.value.code == "unreachable" and connects == [] and len(server.requests) == 1


def test_an_address_is_asked_at_no_other_address_once_a_connection_to_it_was_made(at_localhost, monkeypatch, tmp_path):
    """Reached at 127.0.0.1, the source reads the request and closes the connection without an answer: it is not sent
    again, at the next address or anywhere. Nor is another address tried when TLS fails on the connection made (a
    certificate for another name)."""
    connects = addresses.sync_connects(monkeypatch, refused=("::1", addresses.PAST))
    addresses.resolving(monkeypatch, *addresses.LOCALHOST)
    silent = addresses.Server(None, tls=at_localhost)
    try:
        with pytest.raises(SourceError, match="RemoteProtocolError"):
            read_url(_local(silent.port), lambda: TOKEN)
    finally:
        silent.close()
    assert connects == ["::1", "127.0.0.1"] and silent.connections == 1 and len(silent.requests) == 1
    connects.clear()
    other = addresses.serving(*addresses.certificate(tmp_path / "elsewhere", "elsewhere.acme.lan"))
    elsewhere = addresses.Server(addresses.answering(RECORDS), tls=other)
    try:
        with pytest.raises(SourceError, match="ConnectError"):
            read_url(_local(elsewhere.port), lambda: TOKEN)
    finally:
        elsewhere.close()
    assert connects == ["::1", "127.0.0.1"] and elsewhere.requests == []


# --- the final security review's M1 and L1: what a document may make the server hold, and who chooses its parser ------


def test_a_csv_file_is_read_a_row_at_a_time_each_row_holding_the_fields_mapped_alone(tmp_path):
    """M1: a file source's rows hold the fields its mapping reads, read as the mapping takes them: a header of 1,000
    columns and 3,000 short rows (60 KB) held tens of megabytes as a list of rows of every column."""
    folder = tmp_path / "term-sync"
    folder.mkdir()
    header = ",".join(["company", *(f"c{i}" for i in range(1, 1000))])
    (folder / "customers.csv").write_bytes((header + "\n" + "Halden Insurance\n" * 3000).encode())
    source = FileSource(name="customers", kind="file", path="customers.csv", mapping={"company": "CLIENT"})
    tracemalloc.start()
    try:
        # What the read holds once it returns (its buffer of the file's bound, never filled, is given back by then),
        # and at most while the mapping takes the rows.
        rows = read_file(source, lambda: None, folder=folder)
        held, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        mapped = terms_of(source, rows)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert mapped.terms == [("Halden Insurance", "CLIENT")]
    assert held < 4 * 1024 * 1024, f"{held:,} bytes held once read"
    assert peak < 4 * 1024 * 1024, f"{peak:,} bytes held at most while the rows were mapped"
    assert next(iter(read_file(source, lambda: None, folder=folder))) == {"company": "Halden Insurance"}


def _answering(content_type: str, body: bytes) -> httpx.MockTransport:
    headers = {"content-type": content_type} if content_type else {}
    return httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, content=body))


@pytest.mark.parametrize(
    ("content_type", "body", "said", "fmt"),
    [
        ("application/yaml", b"items:\n- code_name: Project Falcon\n", "YAML", "yaml"),
        ("text/yaml; charset=utf-8", b"items:\n- code_name: Project Falcon\n", "YAML", "yaml"),
        ("text/csv; charset=utf-8", b"code_name\nProject Falcon\n", "CSV", "csv"),
    ],
    ids=["application-yaml", "text-yaml", "text-csv"],
)
def test_an_answer_s_content_type_never_chooses_its_parser_and_one_naming_another_format_is_refused(
    content_type, body, said, fmt
):
    """L1: the server behind a url source chose YAML's parser (and its memory) by its content type. The format comes
    from the source's `format`, else the extension of the address's path, else JSON; a content type that names another
    format refuses the answer, before its body is read. Whoever sets the sources chooses it."""
    with httpx.Client(transport=_answering(content_type, body)) as client:
        with pytest.raises(SourceError) as caught:
            read_url(URL, lambda: TOKEN, client=client)
        assert caught.value.code == "unreadable"
        assert caught.value.message == f"the address answered {said} where the source reads JSON: set `format`"
        given = {"format": fmt, "records": "items" if fmt == "yaml" else None}
        chosen = UrlSource.model_validate({**URL.model_dump(), **given})
        assert list(read_url(chosen, lambda: TOKEN, client=client)) == [{"code_name": "Project Falcon"}]


def test_an_address_s_format_is_its_path_s_extension_whatever_a_content_type_naming_none_says():
    source = UrlSource(name="projects", kind="url", url="https://intranet.acme.com/export/projects.csv",
                       mapping={"code_name": "PROJECT"})  # fmt: skip
    for kind in ("application/octet-stream", "text/plain; charset=utf-8", ""):
        with httpx.Client(transport=_answering(kind, b"code_name\nProject Falcon\n")) as client:
            assert list(read_url(source, lambda: None, client=client)) == [{"code_name": "Project Falcon"}]
    answer = _answering("application/json", b'[{"code_name": "Project Falcon"}]')
    with httpx.Client(transport=answer) as client, pytest.raises(SourceError) as caught:
        read_url(source, lambda: None, client=client)
    assert caught.value.code == "unreadable"
    assert caught.value.message == "the address answered JSON where the source reads CSV: set `format`"


def test_a_yaml_document_past_yaml_s_own_bound_is_refused_unparsed_from_a_file_and_an_address(tmp_path, monkeypatch):
    """L1: a YAML document is read up to 5 MB, a file's read stopping there and an address's too, never parsed."""

    def parsed(text):
        raise AssertionError("a YAML document past its bound was parsed")

    monkeypatch.setattr(records_module, "load_yaml", parsed)
    big = b"#" * (5 * 1024 * 1024 + 1)
    folder = tmp_path / "term-sync"
    folder.mkdir()
    (folder / "projects.yaml").write_bytes(big)
    source = FileSource(name="projects", kind="file", path="projects.yaml", mapping={"code_name": "PROJECT"})
    with pytest.raises(SourceError) as caught:
        read_file(source, lambda: None, folder=folder)
    assert caught.value.code == "too-large"
    assert caught.value.message == "the source's file holds more than 5 MB, the most Latet reads as YAML: not read"
    address = UrlSource.model_validate({**URL.model_dump(), "format": "yaml"})
    with httpx.Client(transport=_answering("application/yaml", big)) as client, pytest.raises(SourceError) as caught:
        read_url(address, lambda: TOKEN, client=client)
    assert caught.value.code == "too-large"
    assert caught.value.message == "larger than 5 MB, the most Latet reads as YAML: not read"
