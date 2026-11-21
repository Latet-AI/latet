"""`latet server terms` (Latet Enterprise, E6) against E0c's administration call answered as term sync's routes answer:
the sources set from a file checked before anything is sent, a sync asked for and waited for, the status, the terms
gone listed to whoever runs it themselves and removed once asked, a secret asked for hidden, every refusal said in the
server's words, escaped; and `latet server license` asking before a change that stops the synced terms (R29)."""

import time
from typing import Any

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers the commands, Latet Enterprise's too
from latet.ee.settings import SOURCES_CHANGED
from latet.server.client import ServerError

ID = "0123456789abcdef01234567"
PROBLEM = "1 instance does not run this release of Latet with the Enterprise Edition: it masks no synced term"
ETAG = "e" * 32
CUSTOMERS = {"name": "customers", "kind": "file", "path": "customers.csv", "mapping": {"company": "CLIENT"}}
CRM = {"name": "crm", "kind": "url", "url": "https://crm.acme.com/a.csv", "mapping": {"company": "CLIENT"}}
COLLEAGUES = {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
              "mapping": {"displayName": "PERSON"}}  # fmt: skip
DIRECTORY = {"name": "directory", "kind": "ldap", "url": "ldaps://dc1.acme.com", "bind": "cn=latet,dc=acme,dc=com",
             "base": "ou=staff,dc=acme,dc=com", "mapping": {"cn": "PERSON"}}  # fmt: skip


def shown(*sources: dict[str, Any]) -> dict[str, Any]:
    """The sources as `GET ee/term-sync/sources` gives them, as PostgreSQL has them (the text is not read here)."""
    return {"sources": list(sources), "text": "", "etag": ETAG}


def _cli(*args, input=None):
    from latet.cli.main import app

    return CliRunner().invoke(app, ["server", "terms", *args], input=input)


def described(name="customers", kind="file", terms=2, by_type=None, gone=0, last=None, asked=None) -> dict[str, Any]:
    """A source as `GET ee/term-sync` describes it."""
    return {
        "name": name, "kind": kind, "every_minutes": 60, "configured": kind is not None, "terms": terms,
        "by_type": {"CLIENT": terms} if by_type is None else by_type, "gone": gone, "asked": asked, "last": last,
        "next": None,
    }  # fmt: skip


def ran(name="customers", outcome="ok", finished=None, **counts) -> dict[str, Any]:
    """A run as `GET ee/term-sync` gives a source's last."""
    finished = time.time() if finished is None else finished
    return {"source": name, "kind": "file", "started": finished - 1, "finished": finished, "outcome": outcome,
            "code": counts.pop("code", None), "reason": counts.pop("reason", None), "found": 0, "added": 0, "gone": 0,
            "skipped": {}, **counts}  # fmt: skip


def state(*sources: dict[str, Any], **changes: Any) -> dict[str, Any]:
    kept = sum(s["terms"] + s["gone"] for s in sources)
    return {"licensed": True, "paused": False, "masked": kept, "kept": kept, "most": 50_000,
            "gone": sum(s["gone"] for s in sources), "unreadable": 0, "behind": None, "problems": [],
            "sources": list(sources), **changes}  # fmt: skip


class Server:
    """E0c's ``admin_call``, answered as term sync's routes answer: each answer set for a method and a path, a callable
    of the body, or a ServerError raised; every call kept."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, Any]] = []
        self.answers: dict[tuple[str, str], Any] = {}
        self.file: Any = None  # a license file the test installs
        self.slept: list[float] = []  # each wait of `run`, on the tests' clock

    def __call__(self, method: str, path: str, body: Any = None, *, query: Any = None, **_options: Any) -> Any:
        self.calls.append((method, path, body, query))
        found = self.answers[(method, path)]
        if callable(found):
            found = found(body)
        if isinstance(found, Exception):
            raise found
        return found

    def sent(self, method: str) -> list[tuple[str, Any]]:
        return [(path, body) for m, path, body, _query in self.calls if m == method]


@pytest.fixture
def server(monkeypatch):
    from latet.ee.sync import cli

    fake = Server()
    monkeypatch.setattr(cli, "admin_call", fake)
    monkeypatch.setattr("latet.cli.agent.agent_name", lambda strict=False: None)  # run by you, not by an agent
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: False)
    now = [0.0]

    def sleep(seconds: float) -> None:
        fake.slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(cli, "clock", lambda: now[0])
    monkeypatch.setattr(cli, "sleep", sleep)
    return fake


@pytest.fixture
def terminal(monkeypatch):
    """You, at a terminal of your own: a question is asked."""
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)


@pytest.fixture
def sources_file(tmp_path):
    path = tmp_path / "term-sync.yaml"
    path.write_text(
        "sources:\n  - name: customers\n    kind: file\n    path: customers.csv\n    mapping: {company: CLIENT}\n"
    )
    return path


# --- sources set ------------------------------------------------------------------------------------------------------


def test_the_sources_are_set_from_a_file_with_the_etag_of_those_read(server, sources_file):
    server.answers[("GET", "ee/term-sync/sources")] = {"sources": [], "text": "sources: []\n", "etag": "e" * 32}
    server.answers[("GET", "ee/term-sync")] = state()
    changed = {"added": ["customers"], "removed": [], "changed": [], "fields": {}, "problems": [PROBLEM], "etag": "f"}
    server.answers[("PUT", "ee/term-sync/sources")] = changed
    result = _cli("sources", "set", str(sources_file))
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "1 source: customers added."
    assert PROBLEM in result.output
    sent = {"text": sources_file.read_bytes().decode("utf-8"), "etag": "e" * 32}  # the file's text as it is
    assert server.sent("PUT") == [("ee/term-sync/sources", sent)]


def test_a_sources_file_written_wrong_is_refused_saying_where_never_what_and_nothing_is_sent(server, tmp_path):
    wrong = tmp_path / "wrong.yaml"
    wrong.write_text(
        "sources:\n  - name: crm\n    kind: url\n    url: https://crm.acme.com/a.csv\n"
        "    headers: {Authorization: Bearer s3cr3t}\n    mapping: {company: CLIENT}\n"
    )
    result = _cli("sources", "set", str(wrong))
    assert result.exit_code == 2 and "sources.0" in result.output and "{secret}" in result.output, result.output
    assert "s3cr3t" not in result.output and server.calls == []
    broken = tmp_path / "broken.yaml"
    broken.write_text("sources:\n  - [Halden\n")
    result = _cli("sources", "set", str(broken))
    assert result.exit_code == 2 and "line" in result.output and "Halden" not in result.output
    assert server.calls == []


def test_a_file_that_takes_out_a_source_with_terms_says_how_many_and_asks_first(server, sources_file, monkeypatch):
    """The sources taken out are those PostgreSQL has (`GET ee/term-sync/sources`): the status of an instance whose copy
    of the settings has not heard of `crm` yet lists it as not set up, its terms counted all the same (the review's
    Minor 4)."""
    server.answers[("GET", "ee/term-sync/sources")] = shown(CUSTOMERS, CRM)
    lagging = {**described("crm", None, terms=3, gone=1), "configured": False}
    server.answers[("GET", "ee/term-sync")] = state(described(), lagging)
    server.answers[("PUT", "ee/term-sync/sources")] = {"added": [], "removed": ["crm"], "changed": [], "fields": {},
                                                       "problems": [], "etag": "f"}  # fmt: skip
    refused = _cli("sources", "set", str(sources_file))  # not at a terminal of your own
    assert refused.exit_code == 2 and "--yes" in refused.output
    assert '3 terms of crm become "no longer in the source"' in refused.output, refused.output
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    kept = _cli("sources", "set", str(sources_file), input="n\n")
    assert kept.exit_code == 1 and server.sent("PUT") == []
    done = _cli("sources", "set", str(sources_file), "--yes")
    assert done.exit_code == 0 and "1 source: crm removed." in done.output, done.output
    assert len(server.sent("PUT")) == 1


def test_sources_changed_meanwhile_on_the_server_are_said_and_nothing_changes(server, sources_file):
    server.answers[("GET", "ee/term-sync/sources")] = {"sources": [], "text": "", "etag": "e" * 32}
    server.answers[("GET", "ee/term-sync")] = state()
    server.answers[("PUT", "ee/term-sync/sources")] = ServerError(409, SOURCES_CHANGED)
    result = _cli("sources", "set", str(sources_file))
    assert result.exit_code == 2 and "changed on the server" in result.output and "run it again" in result.output
    other = "another refusal of the server, in its own words"
    server.answers[("PUT", "ee/term-sync/sources")] = ServerError(409, other)  # told apart by its words
    result = _cli("sources", "set", str(sources_file))
    assert result.exit_code == 2 and other in result.output and "run it again" not in result.output


def test_the_sources_are_shown_as_the_server_keeps_them_to_whoever_runs_it_themselves(server, monkeypatch):
    """The sources hold the company's addresses, DNs, filters and excluded values: shown as the other commands that
    show values show them (`_values`), an agent refused before anything is read (the review's Minor 3)."""
    text = "sources:\n- name: customers\n  kind: file\n  path: customers.csv\n"
    server.answers[("GET", "ee/term-sync/sources")] = {"sources": [], "text": text, "etag": ETAG}
    listed = _cli("sources", "show")
    assert listed.exit_code == 0 and "name: customers" in listed.output and "kind: file" in listed.output
    assert "keep them private" in listed.output
    monkeypatch.setattr("latet.cli.agent.agent_name", lambda strict=False: "codex")
    refused = _cli("sources", "show")
    assert refused.exit_code == 2 and "run it yourself" in refused.output and "customers" not in refused.output
    assert len(server.calls) == 1


# --- run and status ---------------------------------------------------------------------------------------------------


def test_a_sync_asked_for_is_waited_for_and_says_what_each_source_gave(server):
    server.answers[("POST", "ee/term-sync/run")] = {"asked": ["customers"]}
    answers = iter([state(described(asked=1.0, last=None)), state(described(last=ran(found=2, added=2)))])
    server.answers[("GET", "ee/term-sync")] = lambda body: next(answers)
    result = _cli("run")
    assert result.exit_code == 0, result.output
    assert "customers: 2 terms (2 new, 0 no longer in the source)" in result.output
    assert server.sent("POST") == [("ee/term-sync/run", {})]


def test_a_failed_source_is_said_with_its_reason_and_exits_1(server):
    server.answers[("POST", "ee/term-sync/run")] = {"asked": ["customers"]}
    failed = ran(outcome="failed", code="unreachable", reason="the address answered 503")
    server.answers[("GET", "ee/term-sync")] = state(described(last=failed))
    result = _cli("run", "customers")
    assert result.exit_code == 1 and "customers: failed: the address answered 503" in result.output
    assert server.sent("POST") == [("ee/term-sync/run", {"source": "customers"})]


def test_a_sync_still_running_after_a_minute_says_where_to_look(server):
    server.answers[("POST", "ee/term-sync/run")] = {"asked": ["customers"]}
    server.answers[("GET", "ee/term-sync")] = state(described(asked=1.0, last=None))
    result = _cli("run")
    assert result.exit_code == 0 and "still running: `latet server terms status` shows it" in result.output
    # Read after 2, 4, 8 then every 16 seconds, the last at the minute: each read is an entry of the administration
    # log (E0b's gate records every call), a handful for a run (the review's Minor 1).
    assert server.slept == [2, 4, 8, 16, 16, 14] and sum(server.slept) == 60
    assert len([c for c in server.calls if c[0] == "GET"]) == 7


def test_a_sync_asked_for_without_waiting_reads_nothing_more(server):
    server.answers[("POST", "ee/term-sync/run")] = {"asked": ["customers"]}
    result = _cli("run", "--no-wait")
    assert result.exit_code == 0 and "latet server terms status" in result.output
    assert [c[0] for c in server.calls] == ["POST"]


def test_the_status_says_each_source_and_the_count_kept_never_a_term(server):
    last = ran(found=2, added=2, finished=time.time() - 12 * 60 - 5)
    server.answers[("GET", "ee/term-sync")] = state(described(last=last), problems=[PROBLEM])
    result = _cli("status")
    assert result.exit_code == 0, result.output
    assert "customers (file): 2 terms (CLIENT 2); synced 12 minutes ago" in result.output
    assert "2 of 50,000 terms kept" in result.output and PROBLEM in result.output


@pytest.mark.parametrize(("seconds", "said"), [(5.0, False), (60.0, False), (61.0, True), ("a while", False)])
def test_the_status_says_an_instance_cannot_read_the_terms_again_only_past_two_ticks(server, seconds, said):
    """The final review's M1: a reload under way (a new version seen, the terms not swapped yet) is no failure to read
    them: `latet server terms status` says it past the bound `latet server status` says it at (two ticks, 60 seconds),
    never for any lag."""
    behind = {"loaded": 3, "store": 4, "seconds": seconds}
    server.answers[("GET", "ee/term-sync")] = state(described(last=ran(found=2, added=2)), behind=behind)
    result = _cli("status")
    assert result.exit_code == 0, result.output
    assert ("cannot read them again" in result.output) is said
    if said:
        assert "version 3 while the server holds 4, for 61 seconds" in result.output


def test_a_change_of_the_sources_names_the_sources_and_the_fields_changed_never_a_value():
    """B-I3: the administration log's entry of a change names the sources added, taken out and changed, and for each
    changed the names of its fields that changed; the etag a change names is that of the section as it was read."""
    from latet.ee.settings import TermSyncSettings
    from latet.ee.sync.api import sources_change, sources_etag

    customers = {"name": "customers", "kind": "file", "path": "customers.csv", "mapping": {"company": "CLIENT"}}
    projects = {"name": "projects", "kind": "url", "url": "https://intranet.acme.lan/api/projects",
                "mapping": {"code_name": "PROJECT"}}  # fmt: skip
    before = TermSyncSettings.model_validate({"sources": [customers, projects]})
    moved = {**projects, "url": "https://elsewhere.acme.lan/api/projects", "every_minutes": 30}
    now_a_url = {**projects, "name": "customers"}  # the same name, read from an address now
    after = TermSyncSettings.model_validate({"sources": [now_a_url, moved]})
    change = sources_change(before.sources, after.sources)
    assert change == {
        "added": [], "removed": [], "changed": ["customers", "projects"],
        # A source of another kind: every field either kind has that differs, a field one has alone included.
        "fields": {"customers": ["headers", "kind", "mapping", "path", "url"], "projects": ["every_minutes", "url"]},
    }  # fmt: skip
    assert "elsewhere" not in str(change) and "intranet" not in str(change)
    assert sources_change(before.sources, before.sources[:1]) == {"added": [], "removed": ["projects"], "changed": [],
                                                                  "fields": {}}  # fmt: skip
    etag = sources_etag(before)
    assert len(etag) == 32 and etag == sources_etag(TermSyncSettings.model_validate(before.model_dump()))
    assert etag != sources_etag(after)


def test_describe_and_ago():
    from latet.ee.sync.cli import ago, describe

    now = 1_800_000_000.0
    assert (
        ago(now - 30, now) == "just now"
        and ago(now - 3 * 3600, now) == "3 hours ago"
        and ago(now - 60, now) == ("1 minute ago")
    )
    gone = described("crm", None, terms=0, by_type={}, gone=4)
    assert describe(gone, now) == "crm (no longer set up): 0 terms, 4 no longer in the source; not synced yet"
    failed = described(by_type={"PERSON": 1, "CLIENT": 1}, last=ran(outcome="failed", reason="no answer", finished=now))
    assert describe(failed, now) == "customers (file): 2 terms (CLIENT 1, PERSON 1); failed just now: no answer"


# --- gone and remove --------------------------------------------------------------------------------------------------


def test_the_terms_gone_are_listed_to_whoever_runs_it_themselves(server, monkeypatch):
    item = {"id": ID, "source": "customers", "type": "CLIENT", "value": "Halden Insurance", "gone": 1.0}
    server.answers[("GET", "ee/term-sync/gone")] = {"items": [item], "total": 1}
    listed = _cli("gone", "--source", "customers")
    assert listed.exit_code == 0, listed.output
    assert f"{ID}  CLIENT  customers  Halden Insurance" in listed.output
    assert server.calls == [("GET", "ee/term-sync/gone", None, {"source": "customers"})]
    monkeypatch.setattr("latet.cli.agent.agent_name", lambda strict=False: "codex")
    refused = _cli("gone")
    assert refused.exit_code == 2 and "run it yourself" in refused.output and "Halden" not in refused.output
    assert len(server.calls) == 1  # an agent reads nothing


def test_terms_are_removed_once_asked_and_counted(server, terminal):
    server.answers[("POST", "ee/term-sync/remove")] = {"removed": 1, "by_type": {"CLIENT": 1}}
    said = _cli("remove")
    assert said.exit_code == 2 and "Say which" in said.output
    kept = _cli("remove", ID, input="n\n")
    assert kept.exit_code == 1 and "Stop masking 1 term for everyone the server serves?" in kept.output
    assert server.sent("POST") == []
    done = _cli("remove", ID, input="y\n")
    assert done.exit_code == 0 and "1 term removed: Latet no longer masks it." in done.output, done.output
    assert server.sent("POST") == [("ee/term-sync/remove", {"ids": [ID]})]


def test_every_term_gone_of_a_source_is_counted_before_it_is_asked(server):
    server.answers[("GET", "ee/term-sync")] = state(described(gone=3))
    server.answers[("POST", "ee/term-sync/remove")] = {"removed": 3, "by_type": {"CLIENT": 3}}
    refused = _cli("remove", "--source", "customers")
    assert refused.exit_code == 2 and "--yes" in refused.output and "3 terms" in refused.output
    done = _cli("remove", "--source", "customers", "--yes")
    assert done.exit_code == 0 and "3 terms removed: Latet no longer masks them." in done.output
    assert server.sent("POST") == [("ee/term-sync/remove", {"source": "customers"})]
    assert _cli("remove", ID, "--all").exit_code == 2  # one way at a time


def test_the_terms_no_key_opens_are_removed_once_asked_and_a_newer_key_is_said(server, terminal):
    server.answers[("POST", "ee/term-sync/remove")] = {"removed": 2, "by_type": {"CLIENT": 2}}
    question = "Remove the synced terms no master key of this server opens and no source gives any more?"
    kept = _cli("remove", "--unreadable", input="n\n")
    assert kept.exit_code == 1 and question in kept.output and server.sent("POST") == []
    done = _cli("remove", "--unreadable", input="y\n")
    assert done.exit_code == 0 and "2 terms removed" in done.output
    assert server.sent("POST") == [("ee/term-sync/remove", {"unreadable": True})]
    words = "a term names a newer master key version than this server's: give this server that version"
    server.answers[("POST", "ee/term-sync/remove")] = ServerError(409, words)
    newer = _cli("remove", "--unreadable", "--yes")
    assert newer.exit_code == 2 and words in newer.output


# --- secrets ----------------------------------------------------------------------------------------------------------


def test_a_secret_is_asked_for_hidden_and_never_taken_as_an_argument(server, terminal):
    """Asked for once the command has said where the source goes (the review's Minor 2), its surrounding blanks taken
    off as `latet server siem secret` takes them, and sent with the etag of the sources it was said from."""
    server.answers[("GET", "ee/term-sync/sources")] = shown(CUSTOMERS, COLLEAGUES)
    server.answers[("PUT", "ee/term-sync/secrets/colleagues")] = {"saved": "colleagues"}
    value = "graph-" + "client-secret-4b1d"
    kept = _cli("secret", "colleagues", input=f"  {value} \n")
    assert kept.exit_code == 0 and "Kept on the server for the source colleagues." in kept.output, kept.output
    said, asked_for = kept.output.index("https://login.microsoftonline.com"), kept.output.index("The secret of")
    assert said < asked_for and "https://graph.microsoft.com" in kept.output
    assert value not in kept.output
    assert server.sent("PUT") == [("ee/term-sync/secrets/colleagues", {"value": value, "etag": ETAG})]
    assert _cli("secret", "colleagues", value).exit_code == 2  # never on the command line
    server.answers[("DELETE", "ee/term-sync/secrets/colleagues")] = {"removed": "colleagues"}
    asked = _cli("secret", "colleagues", "--remove", input="n\n")
    assert asked.exit_code == 1 and server.sent("DELETE") == []
    removed = _cli("secret", "colleagues", "--remove", input="y\n")
    assert removed.exit_code == 0 and server.sent("DELETE") == [("ee/term-sync/secrets/colleagues", {"etag": ETAG})]


def test_a_directorys_secret_says_whom_it_signs_in_as(server, terminal):
    server.answers[("GET", "ee/term-sync/sources")] = shown(DIRECTORY)
    server.answers[("PUT", "ee/term-sync/secrets/directory")] = {"saved": "directory"}
    kept = _cli("secret", "directory", input="directory-" + "password\n")
    assert (
        kept.exit_code == 0 and "ldaps://dc1.acme.com:636" in kept.output and "cn=latet,dc=acme,dc=com" in kept.output
    )


def test_a_secret_that_cannot_be_kept_is_refused_before_anything_is_sent(server, terminal):
    """An empty secret, a source of no such name, a source that reads with no secret: refused before the value is
    sent; a source changed since its sources were read: said, and to run it again."""
    server.answers[("GET", "ee/term-sync/sources")] = shown(CUSTOMERS, COLLEAGUES)
    empty = _cli("secret", "colleagues", input="   \n")
    assert empty.exit_code == 2 and "not empty" in empty.output
    unknown = _cli("secret", "crm")
    assert (
        unknown.exit_code == 2 and "no source has that name" in unknown.output and "The secret of" not in unknown.output
    )
    none = _cli("secret", "customers")
    assert none.exit_code == 2 and "reads with no secret" in none.output and "The secret of" not in none.output
    assert server.sent("PUT") == []
    server.answers[("PUT", "ee/term-sync/secrets/colleagues")] = ServerError(409, SOURCES_CHANGED)
    moved = _cli("secret", "colleagues", input="graph-" + "client-secret-4b1d\n")
    assert moved.exit_code == 2 and "changed on the server" in moved.output and "run it again" in moved.output


def test_a_refusal_of_the_server_is_said_escaped_and_exits_2(server):
    server.answers[("GET", "ee/term-sync")] = ServerError(403, "this needs Latet Enterprise with term-sync\x1b[2J")
    result = _cli("status")
    assert result.exit_code == 2 and "this needs Latet Enterprise with term-sync" in result.output
    assert "\x1b" not in result.output and "\\x1b" in result.output


# --- a license change that stops the synced terms (R29) ---------------------------------------------------------------


@pytest.fixture
def licenses(licensed, monkeypatch, tmp_path):
    """`latet server license`, E0c's administration call answered as a server whose license lists term sync and keeps
    synced terms; no policy is stored (a server before locked rules answers 404 there)."""
    from latet.cli import server as server_cli
    from latet.ee.license import Standing, State, details, parse_license

    def standing(data: bytes | None) -> dict[str, Any]:
        lic = None if data is None else parse_license(data)
        return details(Standing(State.VALID if lic else State.NONE, lic, 3, 200 if lic else None, None, None, ()))

    fake = Server()
    fake.answers[("GET", "ee/license")] = standing(licensed.make(id="L-2027-0001"))
    fake.answers[("GET", "ee/policy")] = ServerError(404, "not found")
    fake.answers[("GET", "ee/term-sync")] = state(described())
    fake.answers[("GET", "ee/approvals/settings")] = {"approvals": {"changes": "self", "live_review": "self"}}
    fake.answers[("PUT", "ee/license")] = lambda body: standing(body["license"].encode())
    fake.answers[("DELETE", "ee/license")] = {**standing(None), "removed": True}
    monkeypatch.setattr(server_cli, "admin_call", fake)
    monkeypatch.setattr("latet.cli.agent.agent_name", lambda strict=False: None)
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: False)
    file = tmp_path / "without.json"
    file.write_bytes(licensed.make(id="L-2027-0002", features=["roles", "locked-rules", "reports"]))
    fake.file = file
    return fake


def _license(*args, input=None):
    from latet.cli.main import app

    return CliRunner().invoke(app, ["server", "license", *args], input=input)


def test_a_license_without_term_sync_while_terms_are_kept_says_they_stop_and_asks(licenses, monkeypatch):
    file = str(licenses.file)
    refused = _license("install", file)
    assert refused.exit_code == 2 and "synced terms stop being masked" in refused.output and "--yes" in refused.output
    assert licenses.sent("PUT") == []
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    kept = _license("install", file, input="n\n")
    assert kept.exit_code == 1 and "synced terms stop being masked. Install it?" in kept.output, kept.output
    assert licenses.sent("PUT") == []
    installed = _license("install", file, "--yes")
    assert installed.exit_code == 0 and "L-2027-0002" in installed.output, installed.output
    assert "Synced terms stop being masked." in installed.output and "policy" not in installed.output


def test_a_license_in_place_without_term_sync_asks_nothing_of_synced_terms(licenses):
    """Fix round 1's M7: a 403 there (the license in place lists no term-sync), or a 404 (a release before E6): nothing
    of it stops, nothing is asked."""
    for refusal in (ServerError(403, "this needs Latet Enterprise with term-sync"), ServerError(404, "not found")):
        licenses.answers[("GET", "ee/term-sync")] = refusal
        installed = _license("install", str(licenses.file))
        assert installed.exit_code == 0 and "synced terms" not in installed.output.lower(), installed.output
    licenses.answers[("GET", "ee/term-sync")] = state()  # no term kept: nothing stops either
    assert "synced terms" not in _license("install", str(licenses.file)).output.lower()
    licenses.answers[("GET", "ee/term-sync")] = ServerError(503, "the database does not answer")
    stopped = _license("install", str(licenses.file))
    assert stopped.exit_code == 2 and "the database does not answer" in stopped.output


def test_removing_the_license_while_terms_are_kept_says_they_stop(licenses, monkeypatch):
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    kept = _license("remove", input="n\n")
    assert kept.exit_code == 1 and "and synced terms stop being masked." in kept.output, kept.output
    removed = _license("remove", "--yes")
    assert removed.exit_code == 0 and "Synced terms stop being masked." in removed.output, removed.output
