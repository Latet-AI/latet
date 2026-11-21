"""The records of a file or an address, and the terms a source's mapping makes of them."""

import json
import socket
import threading
import tracemalloc

import pytest

from latet.ee.settings import FileSource
from latet.ee.sync import records as records_module
from latet.ee.sync.records import SocketCut, SourceError, ago_words, parse_records, read_inside, terms_of
from latet.fileio import FileReadError

SOURCE = FileSource(name="customers", kind="file", path="customers.csv",
                    mapping={"company": "CLIENT", "contact": "PERSON"})  # fmt: skip


def test_the_rows_of_a_csv_become_terms_of_their_types():
    records = parse_records(b"company,contact\nHalden Insurance,Grace Whitfield\nKestrel Analytics,\n", "csv")
    assert terms_of(SOURCE, records).terms == [
        ("Halden Insurance", "CLIENT"), ("Grace Whitfield", "PERSON"), ("Kestrel Analytics", "CLIENT"),
    ]  # fmt: skip


def test_a_csv_as_excel_writes_it_in_europe_is_read_as_written():
    data = "Company ; Contact\r\nMaison Brûlé;Zoë Laurent\r\n".encode("cp1252")
    source = SOURCE.model_copy(update={"mapping": {"Company": "CLIENT", "Contact": "PERSON"}})
    records = parse_records(data, "csv", delimiter=";", encoding="cp1252")
    assert terms_of(source, records).terms == [("Maison Brûlé", "CLIENT"), ("Zoë Laurent", "PERSON")]
    bom = "\N{ZERO WIDTH NO-BREAK SPACE}"
    assert list(parse_records(f"{bom}company\nHalden Insurance\n".encode(), "csv")) == [{"company": "Halden Insurance"}]


def test_text_in_another_encoding_is_refused_saying_which_setting_never_what_it_holds():
    with pytest.raises(SourceError, match="set `encoding`") as caught:
        parse_records("company\nMaison Brûlé\n".encode("cp1252"), "csv")
    assert caught.value.code == "unreadable" and "Brûl" not in caught.value.message


def test_json_and_yaml_give_their_list_of_records():
    doc = b'{"data": {"items": [{"code_name": "Project Falcon"}, "not a record"]}}'
    assert parse_records(doc, "json", records="data.items") == [{"code_name": "Project Falcon"}]
    assert parse_records(b"- code_name: Project Falcon\n", "yaml") == [{"code_name": "Project Falcon"}]
    with pytest.raises(SourceError, match=r"no list of records at data\.rows"):
        parse_records(doc, "json", records="data.rows")
    with pytest.raises(SourceError, match=r"not valid JSON \(line 1\)") as caught:
        parse_records(b'[{"company": "Halden Insurance",]', "json")
    assert "Halden" not in caught.value.message


def test_what_is_not_a_term_is_left_out_and_counted():
    source = SOURCE.model_copy(update={"mapping": {"name": "TEAM"}, "exclude": ["Sales", "All Company"]})
    values = ["Sales", "all company", "IT", "Payments  Team", "payments team", "x" * 201, "---", 42, None,
              ["Data Platform", "Ops"]]  # fmt: skip
    mapped = terms_of(source, [{"name": v} for v in values])
    assert mapped.terms == [("Payments Team", "TEAM"), ("Data Platform", "TEAM")]
    assert mapped.skipped == {"excluded": 2, "short": 3, "long": 1, "unreadable": 1}


def test_a_template_joins_fields_and_a_lower_minimum_takes_short_names():
    source = SOURCE.model_copy(update={"mapping": {"{givenName} {sn}": "PERSON", "company": "CLIENT"}, "min_length": 3})
    records = [{"givenName": "Emily", "sn": "Carter", "displayName": "Carter, Emily", "company": "SAP"},
               {"givenName": "Noah", "company": "Box"}]  # fmt: skip
    assert terms_of(source, records).terms == [("Emily Carter", "PERSON"), ("SAP", "CLIENT"), ("Box", "CLIENT")]


def test_more_terms_than_the_limit_from_one_source_are_refused(monkeypatch):
    monkeypatch.setattr(records_module, "MOST_TERMS", 3)
    with pytest.raises(SourceError, match="more than 3 terms") as caught:
        terms_of(SOURCE, [{"company": f"Company Number {i}"} for i in range(4)])
    assert caught.value.code == "too-many"


# --- beyond the brief: what the review of this task's code found -------------------------------------------------------


@pytest.mark.parametrize("fmt", ["json", "yaml"])
def test_a_document_nested_deeper_than_python_reads_is_unreadable_never_another_error(fmt):
    with pytest.raises(SourceError, match="nested deeper") as caught:
        parse_records(b"[" * 100_000, fmt)
    assert caught.value.code == "unreadable"


# --- fix round 1: the review of this task's code -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "fmt", "given"),
    [
        (b'[{"n": ' + b"7" * 5000 + b"}]", "json", {}),  # past Python's digits for an integer: a ValueError of its own
        (b"company\nHalden Insurance\n", "csv", {"delimiter": '"'}),  # the CSV reader refuses it when it is made
        (b"company\nHalden Insurance\n", "csv", {"delimiter": chr(10)}),
        (b"company\nHalden Insurance\n", "csv", {"encoding": "zlib"}),  # no text encoding: a LookupError
        (b"company\nHalden Insurance\n", "csv", {"encoding": "undefined"}),  # never decodes: a UnicodeError
        (b"company\nHalden Insurance\n", "csv", {"encoding": "no-such-codec"}),
        (b'{"data": []}', "json", {"records": "data" + chr(10) + "Halden"}),  # never said back
    ],
    ids=["long-integer", "quote-separator", "line-separator", "zlib", "undefined", "unknown-codec", "records-line"],
)
def test_every_failure_of_a_document_is_unreadable_in_fixed_words(data, fmt, given):
    """Fix round 1's 4: whatever the parser raises, a run's reason is Latet's own words, never a value nor a setting
    that could carry a line break."""
    with pytest.raises(SourceError) as caught:
        parse_records(data, fmt, **given)
    assert caught.value.code == "unreadable"
    assert not any(word in caught.value.message for word in ("Halden", "7777", "no-such", chr(10)))


def test_the_size_a_document_may_have_is_said_in_plain_words(monkeypatch):
    """Fix round 1's 8: the bound as it is set, never a number one past it."""
    monkeypatch.setattr(records_module, "MAX_BYTES", 2 * 1024 * 1024)
    with pytest.raises(SourceError, match=r"larger than 2 MB: not read") as caught:
        parse_records(b"x" * (2 * 1024 * 1024 + 1), "csv")
    assert caught.value.code == "too-large"
    monkeypatch.setattr(records_module, "MAX_BYTES", 16)
    with records_module.bounded(float("inf"), lambda: 0.0) as limits, pytest.raises(SourceError) as caught:
        limits.take(17)
    assert caught.value.message == "more than 16 bytes in all: not read"


@pytest.mark.parametrize(("seconds", "said"), [(300.0, "5 minutes"), (900.0, "15 minutes"), (45.0, "45 seconds")])
def test_a_run_past_its_deadline_says_the_time_it_had(seconds, said):
    """Fix round 1's 8: the words come from the bound, never a turn written in."""
    clock = [1000.0]
    with records_module.bounded(1000.0 + seconds, lambda: clock[0]) as limits:
        limits.check()
        clock[0] += seconds
        with pytest.raises(SourceError, match=f"the {said} its run had") as caught:
            limits.check()
    assert caught.value.code == "timeout"


# --- Task 6's fix round: what no store can keep ---------------------------------------------------------------------


def test_a_value_no_store_can_encode_is_left_out_as_unreadable_and_the_others_are_kept(store):
    """A JSON document may name a lone surrogate (half of an emoji): no store can encode it (a term's id and its sealed
    value are UTF-8), so it is left out and counted, never the end of the whole run."""
    broken = "Caf" + chr(0xD83D) + " Ltd"
    doc = json.dumps([{"company": "Halden Insurance"}, {"company": broken}, {"company": "Kestrel Analytics"}]).encode()
    mapped = terms_of(SOURCE, parse_records(doc, "json"))
    assert mapped.terms == [("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT")]
    assert mapped.skipped == {"unreadable": 1}
    assert store.apply("customers", mapped.terms, now=1.0) == (2, 0)
    assert sorted(t.value for t in store.terms()) == ["Halden Insurance", "Kestrel Analytics"]


# --- Task 7's fix round: a read whose bytes trickle in -----------------------------------------------------------------


def test_a_read_still_under_way_at_its_deadline_is_cut_and_fails_timeout():
    """The checks between two reads alone let a read whose bytes trickle in outlast the deadline: a watchdog armed in
    the reader's thread, from the time its run has left in real seconds, cuts it then from a thread of its own, and
    what the read that was cut raises is `timeout`, in fixed words."""
    cut = threading.Event()

    def read_cut_under_way():
        with records_module.cut_at_deadline(cut.set):
            assert cut.wait(5), "the read was not cut at its deadline"
            raise OSError("the socket was shut down")  # what a read cut under way raises

    # 0.2 seconds left, by a clock that stands still: only the watchdog can end the read
    with records_module.bounded(100.2, lambda: 100.0), pytest.raises(SourceError) as caught:
        read_cut_under_way()
    assert caught.value.code == "timeout"
    assert caught.value.message == "the source took longer than the 1 second its run had: narrow it"


def test_a_read_that_ends_as_it_is_cut_fails_timeout_all_the_same():
    """A body delimited by the connection's close ends without an error when the connection is cut: what was read is
    cut short, and is never taken as the whole of it."""
    cut = threading.Event()

    def read_ended_by_the_cut():
        with records_module.cut_at_deadline(cut.set):
            assert cut.wait(5), "the read was not cut at its deadline"  # then the body ends, as at its close

    with records_module.bounded(100.2, lambda: 100.0), pytest.raises(SourceError) as caught:
        read_ended_by_the_cut()
    assert caught.value.code == "timeout"


def test_a_read_that_ends_in_time_is_never_cut_nor_one_read_with_no_deadline():
    cut = threading.Event()
    with records_module.bounded(100.2, lambda: 100.0), records_module.cut_at_deadline(cut.set):
        pass
    with records_module.cut_at_deadline(cut.set):  # a reader called alone: no deadline, no watchdog
        pass
    assert not cut.wait(0.5)  # past the first one's deadline: its watchdog was put away with it


# --- the final review's M2: what a file source and a directory's certificate share, and the cut's sockets -------------


def test_a_file_is_read_inside_the_term_sync_folder_alone_never_through_a_link_out_of_it(tmp_path):
    """One check for a file source and a directory's certificate authority (B-M8): a name directly inside the folder,
    resolved there, opened without following a link, a regular file of ``limit`` bytes at most; refused naming what it
    is, never a path (B-M4)."""
    folder = tmp_path / "term-sync"
    folder.mkdir()
    (folder / "customers.csv").write_bytes(b"company\nHalden Insurance\n")
    assert read_inside(folder, "customers.csv", limit=1024, what="file") == b"company\nHalden Insurance\n"
    with pytest.raises(FileReadError):
        read_inside(folder, "customers.csv", limit=8, what="file")
    (tmp_path / "outside.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\n")
    for name in ("../outside.pem", "link.pem"):
        if name == "link.pem":
            try:
                (folder / name).symlink_to(tmp_path / "outside.pem")
            except OSError:
                continue  # this system makes no symbolic link here
        with pytest.raises(SourceError) as caught:
            read_inside(folder, name, limit=1024, what="certificate file")
        assert caught.value.code == "unreachable"
        assert caught.value.message == "the source's certificate file is not inside the server's term-sync folder"


@pytest.mark.parametrize(
    ("seconds", "said"),
    [(-5, "just now"), (59.9, "just now"), (60, "1 minute ago"), (12 * 60 + 5, "12 minutes ago"),
     (3 * 3600, "3 hours ago"), (86400, "1 day ago"), (3 * 86400 + 7200, "3 days ago")],
)  # fmt: skip
def test_how_long_ago_is_said_alike_by_the_status_and_the_command(seconds, said):
    assert ago_words(seconds) == said


def test_a_cut_shuts_down_each_socket_kept_and_one_kept_after_it_at_once():
    """The duplicates an address's reader and a directory's keep (``SocketCut``): a cut shuts each down, its peer told
    the connection ended (on Linux a read blocked on it ends too: the release checklist's Linux run of tests/ee/sync);
    one held after the cut is shut down as it is held, and says so; ``release`` closes the duplicates alone."""
    cut = SocketCut()
    first, peer = socket.socketpair()
    second, other = socket.socketpair()
    try:
        for sock in (peer, other):
            sock.settimeout(5)
        assert cut.hold(first.dup()) is False
        cut.cut()
        assert peer.recv(1) == b""  # shut down: its peer reads the end of the connection
        assert cut.hold(second.dup()) is True
        assert other.recv(1) == b""  # shut down as it was held
        cut.release()
        assert first.fileno() != -1 and second.fileno() != -1  # the sockets themselves are their readers' to close
    finally:
        for sock in (first, peer, second, other):
            sock.close()


# --- the final security review's M1 and L1: what a document may make the server hold ----------------------------------


def _header(columns: int) -> str:
    return ",".join(["company", *(f"c{i}" for i in range(1, columns))])


def test_a_csv_whose_header_names_more_columns_than_latet_reads_is_refused_too_large():
    """M1: every row of a CSV file was read with every column its header names, so a header of 20,000 columns and a few
    hundred kilobytes of short rows held gigabytes. A header of more than 1,000 columns is refused before any row is
    read, in fixed words; one of 1,000 is read."""
    with pytest.raises(SourceError) as caught:
        parse_records(f"{_header(1001)}\nHalden Insurance\n".encode(), "csv")
    assert caught.value.code == "too-large"
    assert caught.value.message == "a header of more than 1,000 columns: not read"
    rows = parse_records(f"{_header(1000)}\nHalden Insurance\n".encode(), "csv", fields=["company"])
    assert list(rows) == [{"company": "Halden Insurance"}]


def test_a_csv_is_read_a_row_at_a_time_each_holding_the_fields_mapped_alone():
    """M1: read as a list of rows of every column, a header of 1,000 columns and 3,000 rows of one value (60 KB) held
    tens of megabytes; read a row at a time as the mapping takes them, it holds a few hundred kilobytes. Given the
    fields the mapping reads, a row holds those alone (a column the header lacks is not made up)."""
    data = (_header(1000) + "\n" + "Halden Insurance\n" * 3000).encode()
    tracemalloc.start()
    try:
        mapped = terms_of(SOURCE, parse_records(data, "csv"))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert mapped.terms == [("Halden Insurance", "CLIENT")]
    assert peak < 4 * 1024 * 1024, f"{peak:,} bytes held to read {len(data):,}"
    given = b"contact,notes,company\nGrace Whitfield,met in May,Halden Insurance\nDaniel Okafor\n"
    rows = parse_records(given, "csv", fields=["company", "contact", "phone"])
    assert list(rows) == [{"company": "Halden Insurance", "contact": "Grace Whitfield"},
                          {"company": None, "contact": "Daniel Okafor"}]  # fmt: skip


def _listing(fmt: str, count: int) -> bytes:
    if fmt == "csv":
        return ("company\n" + "Halden Insurance\n" * count).encode()
    if fmt == "json":
        return json.dumps([{"company": "Halden Insurance"}] * count).encode()
    return ("- company: Halden Insurance\n" * count).encode()


@pytest.mark.parametrize("fmt", ["csv", "json", "yaml"])
def test_a_document_listing_more_records_than_the_bound_is_refused_too_many(fmt, monkeypatch):
    """M1 and L1: a document lists four times the terms one source may give at most (``MOST_TERMS``), room for records
    that give none (the same customer on several rows, values left out); a CSV file's rows are counted as they are
    read, a blank line not counted."""
    assert records_module.MOST_RECORDS == 4 * records_module.MOST_TERMS == 200_000
    monkeypatch.setattr(records_module, "MOST_RECORDS", 3)
    assert terms_of(SOURCE, parse_records(_listing(fmt, 3), fmt)).terms == [("Halden Insurance", "CLIENT")]
    if fmt == "csv":
        spaced = b"company\n\nHalden Insurance\n\n\nHalden Insurance\n\nHalden Insurance\n\n"
        assert terms_of(SOURCE, parse_records(spaced, fmt)).terms == [("Halden Insurance", "CLIENT")]
    with pytest.raises(SourceError) as caught:
        terms_of(SOURCE, parse_records(_listing(fmt, 4), fmt))
    assert caught.value.code == "too-many"
    assert caught.value.message == "more than 3 records: narrow the source (an export of fewer records)"


def test_a_csv_of_more_rows_than_the_bound_is_refused_as_its_rows_are_read():
    """M1, at the bound itself: 200,000 rows of one customer are read (3.4 MB), one row more is refused."""
    data = ("company\n" + "Halden Insurance\n" * 200_000).encode()
    assert terms_of(SOURCE, parse_records(data, "csv")).terms == [("Halden Insurance", "CLIENT")]
    with pytest.raises(SourceError, match=r"^more than 200,000 records: ") as caught:
        terms_of(SOURCE, parse_records(data + b"Halden Insurance\n", "csv"))
    assert caught.value.code == "too-many"


def test_a_csv_row_that_fails_as_it_is_read_is_unreadable_in_fixed_words():
    """Fix round 1's 4, for rows read as the mapping takes them: a CSV reader's error at a later row is `unreadable`
    with its line, never the reader's words, which may quote the row."""
    data = b'company\nHalden Insurance\n"Kestrel ' + b"x" * (200 * 1024) + b'"\n'
    with pytest.raises(SourceError) as caught:
        terms_of(SOURCE, parse_records(data, "csv"))
    assert caught.value.code == "unreadable"
    assert caught.value.message == "not valid CSV (line 3)"


def test_a_yaml_document_past_its_own_bound_is_refused_unparsed_and_json_is_read_past_it(monkeypatch):
    """L1: PyYAML's parser holds about 190 times a document of small nodes, so a YAML document is read up to 5 MB,
    checked before it is decoded or parsed, where CSV and JSON are read up to 50 MB."""
    assert records_module.MAX_YAML_BYTES == 5 * 1024 * 1024

    def parsed(text):
        raise AssertionError("a YAML document past its bound was parsed")

    monkeypatch.setattr(records_module, "load_yaml", parsed)
    with pytest.raises(SourceError) as caught:
        parse_records(b"#" * (5 * 1024 * 1024 + 1), "yaml")
    assert caught.value.code == "too-large"
    assert caught.value.message == "larger than 5 MB, the most Latet reads as YAML: not read"
    doc = _listing("json", 200_000)
    assert len(doc) > 6 * 1024 * 1024
    assert terms_of(SOURCE, parse_records(doc, "json")).terms == [("Halden Insurance", "CLIENT")]
