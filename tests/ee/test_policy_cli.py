"""`latet policy check`: a policy file checked before it is set on a server; `latet server policy`, through the
server's administration API; and the policy's line in `latet server status`."""

import json

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from latet.server.client import ServerError
from latet.server.connection import NotConnected

from .editions import Source
from .servers import URL, services

POLICY = "latet_rules: 1\nprofile: max\nterms: [{value: Project Falcon, type: PROJECT}]\n"
STORED = {
    "etag": "3f1c9a7e52d04b18", "set_at": 1_791_191_520.0, "set_by": "admin:t_ab12", "keys": ["profile", "terms"], "brought": ["pii", "id_regions", "include_private_ips", "data", "images"],
    "exceptions": "personal", "notices": [], "in_force": True, "text": POLICY,
}  # fmt: skip
NONE = {"policy": None, "problems": []}
HELD = {"policy": STORED, "problems": []}
ESC = "\N{ESCAPE}"
BIDI = "\N{RIGHT-TO-LEFT OVERRIDE}"


def _cli(*args, input=None):
    import latet.cli.admin  # noqa: F401 - registers the commands, the Enterprise ones with them
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


class Server:
    """The administration API as E0c's admin_call reaches it: the calls made, and the answers in turn."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def you(monkeypatch):
    """The commands run by you, not by an agent."""
    from latet.cli.agent import MARKERS

    for markers in MARKERS.values():
        for name in markers:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AGENT", raising=False)


@pytest.fixture
def terminal(monkeypatch):
    """Whether the commands run at your own terminal: `terminal.at = False` for a pipe."""

    class At:
        at = True

    state = At()
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: state.at)
    return state


def _file(tmp_path, text=POLICY, name="policy.yaml"):
    path = tmp_path / name
    path.write_bytes(text.encode())
    return path


def test_latet_policy_check_says_what_it_locks_and_what_it_leaves(tmp_path):
    good = tmp_path / "next.yaml"
    good.write_text("latet_rules: 1\nexceptions: company\nner: auto\nprofle: max\n", "utf-8")
    result = _cli("policy", "check", str(good))
    assert result.exit_code == 1, result.output  # a key it could not read
    assert "It locks: ner" in result.output and "profle" in result.output
    assert "It leaves to each person: profile," in result.output and "only the company's count" in result.output
    bad = tmp_path / "bad.yaml"
    bad.write_text("profile: [max", "utf-8")
    assert _cli("policy", "check", str(bad)).exit_code == 2
    assert _cli("policy", "check").exit_code == 2  # the file is required: a computer has no policy of its own


def test_latet_policy_check_lists_what_the_profile_brings_as_locked_not_left_to_each_person(tmp_path):
    result = _cli("policy", "check", str(_file(tmp_path, "latet_rules: 1\nprofile: max\n")))
    assert result.exit_code == 0, result.output
    assert "It locks: profile\n" in result.output
    assert "Its profile locks too: pii, id_regions, include_private_ips, data, images\n" in result.output
    left = next(line for line in result.output.splitlines() if line.startswith("It leaves to each person: "))
    left = left.split(": ", 1)[1].split(", ")
    assert not {"pii", "id_regions", "include_private_ips", "data", "images"} & set(left) and "ner" in left


def test_latet_policy_check_says_a_key_written_with_no_value_and_exits_1(tmp_path):
    """Final review L1: a value forgotten locks nothing, and is said; `exceptions:` alone reads as `company`."""
    result = _cli("policy", "check", str(_file(tmp_path, "latet_rules: 1\nexceptions:\nprofile:\nocr: auto\n")))
    assert result.exit_code == 1, result.output
    assert "It locks: ocr\n" in result.output and "Exceptions: only the company's count" in result.output
    assert "Not read in it: profile: written with no value, so it is not locked" in result.output
    assert "Not read in it: exceptions: personal or company; read as company, the stricter" in result.output


def test_latet_policy_check_never_connects_and_reads_a_notepad_file(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("policy check reached for the server")

    monkeypatch.setattr("latet.cli.server.admin_call", refuse)
    monkeypatch.setattr("latet.cli.server._connection", refuse, raising=False)
    path = tmp_path / "notepad.yaml"
    path.write_bytes("\N{ZERO WIDTH NO-BREAK SPACE}".encode() + POLICY.encode())
    result = _cli("policy", "check", str(path))
    assert result.exit_code == 0, result.output
    assert "It locks: profile, terms" in result.output and "each person may add their own" in result.output


def test_latet_policy_check_prints_nothing_a_terminal_would_act_on(tmp_path):
    path = tmp_path / "p.yaml"  # a file name cannot hold these on Windows: a key of the file does
    path.write_text('latet_rules: 1\nprofile: max\n"k\\e[31m\\u202e": 1\n', "utf-8")
    ok = _cli("policy", "check", str(path))
    assert ok.exit_code == 1 and ESC not in ok.output and BIDI not in ok.output
    assert "\\x1b" in ok.output
    missing = _cli("policy", "check", str(tmp_path / f"gone{ESC}[2J.yaml"))
    assert missing.exit_code == 2 and ESC not in missing.output and "\\x1b" in missing.output


def test_latet_policy_check_refuses_a_file_over_the_limit_and_one_that_is_not_text(tmp_path):
    from latet.transfer import MAX_BYTES

    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (MAX_BYTES + 1))
    refused = _cli("policy", "check", str(big))
    assert refused.exit_code == 2 and "larger than" in refused.output
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xfe\x00")
    assert _cli("policy", "check", str(binary)).exit_code == 2
    assert _cli("policy", "check", str(tmp_path)).exit_code == 2  # a folder


def test_latet_server_policy_set_checks_the_file_first_and_says_what_the_server_answered(tmp_path, monkeypatch, you):
    refusal = "the license does not allow changing Enterprise settings now: the policy in force stays"
    answer = {"etag": "3f1c9a7e52d04b18", "keys": ["profile", "terms"], "exceptions": "personal", "notices": []}
    server = Server(NONE, {**answer, "problems": []}, NONE, ServerError(403, refusal))
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    policy = tmp_path / "policy.yaml"
    policy.write_bytes(b"\xef\xbb\xbf" + POLICY.encode())  # saved by Notepad
    done = _cli("server", "policy", "set", str(policy))
    assert done.exit_code == 0, done.output
    assert server.calls == [("GET", "ee/policy", None), ("PUT", "ee/policy", {"policy": POLICY})]
    assert "it locks profile, terms" in done.output
    assert "Every person and API key, on every instance, is raised to it now." in done.output
    refused = _cli("server", "policy", "set", str(policy))
    assert refused.exit_code == 2 and f"Not changed: {refusal}" in refused.output
    bad = tmp_path / "bad.yaml"
    bad.write_text("profile: [max", "utf-8")
    assert _cli("server", "policy", "set", str(bad)).exit_code == 2 and len(server.calls) == 4  # never sent


def test_latet_server_policy_set_says_what_the_server_says_is_not_done(tmp_path, monkeypatch, you):
    late = "the other instances were not told now: they take it within 10 seconds"
    older = f"2 instances do not apply the policy {ESC}[2J"
    answer = {
        "etag": "x",
        "keys": ["profile"],
        "exceptions": "personal",
        "notices": [f"a{ESC}[1m"],
        "problems": [late, older],
    }
    server = Server(NONE, answer)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    done = _cli("server", "policy", "set", str(_file(tmp_path)))
    assert done.exit_code == 0, done.output
    assert late in done.output and "2 instances do not apply the policy" in done.output
    assert "is raised to it now" not in done.output  # the server said otherwise
    assert ESC not in done.output and "\\x1b" in done.output


def test_latet_server_policy_set_says_the_settings_the_server_kept(tmp_path, monkeypatch, you):
    server = Server(NONE, {"etag": "x", "keys": ["ner"], "exceptions": "company", "notices": [], "problems": []})
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    done = _cli("server", "policy", "set", str(_file(tmp_path)))  # the file locks profile and terms
    assert "it locks ner." in done.output and "only the company's count" in done.output


def test_latet_server_policy_show_writes_the_text_to_a_file_for_you_alone_and_says_when_it_cannot(
    tmp_path, monkeypatch
):
    written = []

    def write(path, text, private=False):
        written.append(private)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("latet.cli.server.admin_call", Server(HELD))
    monkeypatch.setattr("latet.config.write_atomic", write)
    refused = _cli("server", "policy", "show", "--file", str(tmp_path / "out.yaml"))
    assert refused.exit_code == 2 and "Not saved" in refused.output and written == [True]


def test_latet_server_policy_set_does_not_claim_when_it_cannot_tell(tmp_path, monkeypatch, you):
    server = Server(NONE, {"etag": "x", "keys": ["profile"], "exceptions": "personal", "notices": []})  # no problems
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    done = _cli("server", "policy", "set", str(_file(tmp_path)))
    assert done.exit_code == 0 and "is raised to it now" not in done.output


def test_latet_server_policy_set_says_when_the_server_is_not_reached_or_answers_in_escapes(tmp_path, monkeypatch, you):
    file = str(_file(tmp_path))
    monkeypatch.setattr("latet.cli.server.admin_call", Server(NONE, NotConnected("no connection")))
    unreached = _cli("server", "policy", "set", file)
    assert unreached.exit_code == 2 and "Not changed: no connection" in unreached.output
    monkeypatch.setattr("latet.cli.server.admin_call", Server(NONE, ServerError(400, f"bad {ESC}[31m{BIDI}")))
    refused = _cli("server", "policy", "set", file)
    assert refused.exit_code == 2 and ESC not in refused.output and BIDI not in refused.output
    assert "\\x1b" in refused.output
    monkeypatch.setattr("latet.cli.server.admin_call", Server(NotConnected("no connection")))
    assert _cli("server", "policy", "set", file).exit_code == 2  # the stored policy could not be read first


def test_latet_server_policy_set_is_refused_to_an_agent(tmp_path, monkeypatch):
    server = Server(NONE)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    monkeypatch.setenv("CLAUDECODE", "1")
    refused = _cli("server", "policy", "set", str(_file(tmp_path)), "--yes")
    assert refused.exit_code == 2 and "run it yourself" in refused.output and server.calls == []


def test_latet_server_policy_set_reads_a_file_of_text_only_and_within_the_limit(tmp_path, monkeypatch, you):
    from latet.transfer import MAX_BYTES

    server = Server(NONE)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (MAX_BYTES + 1))
    assert _cli("server", "policy", "set", str(big)).exit_code == 2
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xfe\x00")
    assert _cli("server", "policy", "set", str(binary)).exit_code == 2
    assert _cli("server", "policy", "set", str(tmp_path / "gone.yaml")).exit_code == 2
    assert server.calls == []


LOCKS_FEWER = "latet_rules: 1\nprofile: max\n"
LOOSER = (
    "latet_rules: 1\nprofile: max\nterms: [{value: Project Falcon, type: PROJECT}]\nner: auto\nexceptions: personal\n"
)


def _held(keys, exceptions="personal"):
    return {"policy": {**STORED, "keys": keys, "exceptions": exceptions}, "problems": []}


def test_latet_server_policy_set_asks_before_it_locks_fewer_settings(tmp_path, monkeypatch, you, terminal):
    answer = {"etag": "x", "keys": ["profile"], "exceptions": "personal", "notices": [], "problems": []}
    server = Server(HELD, HELD, answer, HELD)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    path = str(_file(tmp_path, LOCKS_FEWER))
    declined = _cli("server", "policy", "set", path, input="n\n")
    assert declined.exit_code == 1 and "terms" in declined.output and [c[0] for c in server.calls] == ["GET"]
    agreed = _cli("server", "policy", "set", path, input="y\n")
    assert agreed.exit_code == 0 and [c[0] for c in server.calls] == ["GET", "GET", "PUT"]
    terminal.at = False
    refused = _cli("server", "policy", "set", path)
    assert refused.exit_code == 2 and "--yes" in refused.output and "terms" in refused.output
    assert [c[0] for c in server.calls] == ["GET", "GET", "PUT", "GET"]


def test_latet_server_policy_set_says_what_is_not_read_and_asks_before_it_sends_anything(
    tmp_path, monkeypatch, you, terminal
):
    """Whole-branch review, Important 1: one malformed term leaves out every term. Said before anything is sent, and
    asked about, also with no policy stored; agreed, the server is told so (``accept_notices``)."""
    text = POLICY.replace("type: PROJECT}", "type: PROJECT, case_sensitive: often}") + "profle: max\n"
    answer = {"etag": "x", "keys": ["profile"], "exceptions": "personal", "notices": [], "problems": []}
    server = Server(NONE, answer, NONE, answer)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    path = str(_file(tmp_path, text))
    declined = _cli("server", "policy", "set", path, input="n\n")
    assert declined.exit_code == 1 and "Not read in it: terms:" in declined.output and "profle" in declined.output
    assert server.calls == []
    agreed = _cli("server", "policy", "set", path, input="y\n")
    assert agreed.exit_code == 0 and agreed.output.index("Not read in it") < agreed.output.index("is set")
    assert server.calls == [("GET", "ee/policy", None), ("PUT", "ee/policy", {"policy": text, "accept_notices": True})]
    terminal.at = False
    refused = _cli("server", "policy", "set", path)
    assert refused.exit_code == 2 and "--yes" in refused.output and len(server.calls) == 2
    done = _cli("server", "policy", "set", path, "--yes")
    assert done.exit_code == 0 and "Not read in it: terms:" in done.output and server.calls[-1][2]["accept_notices"]
    assert _cli("policy", "check", path).exit_code == 1  # `check` still says so by its exit code


def test_latet_server_policy_set_yes_goes_on_and_says_what_it_unlocks(tmp_path, monkeypatch, you, terminal):
    terminal.at = False
    answer = {"etag": "x", "keys": ["profile"], "exceptions": "personal", "notices": [], "problems": []}
    server = Server(HELD, answer)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    done = _cli("server", "policy", "set", str(_file(tmp_path, LOCKS_FEWER)), "--yes")
    assert done.exit_code == 0 and "no longer locks: terms" in done.output


def test_latet_server_policy_set_asks_before_exceptions_go_from_company_to_personal(
    tmp_path, monkeypatch, you, terminal
):
    terminal.at = False
    keys = ["profile", "terms"]
    server = Server(_held(keys, "company"))
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    refused = _cli("server", "policy", "set", str(_file(tmp_path, POLICY + "exceptions: personal\n")))
    assert refused.exit_code == 2 and "company" in refused.output and "--yes" in refused.output
    assert [c[0] for c in server.calls] == ["GET"]


def test_latet_server_policy_set_asks_nothing_when_it_locks_at_least_what_is_locked(
    tmp_path, monkeypatch, you, terminal
):
    terminal.at = False
    answer = {"etag": "x", "keys": ["profile"], "exceptions": "personal", "notices": [], "problems": []}
    server = Server(_held(["profile", "terms"]), answer, _held(["profile"], "company"), answer, NONE, answer)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    assert _cli("server", "policy", "set", str(_file(tmp_path, LOOSER))).exit_code == 0  # more locked, same exceptions
    stricter = POLICY.replace("terms:", "exceptions: company\nterms:")
    assert _cli("server", "policy", "set", str(_file(tmp_path, stricter))).exit_code == 0  # personal to company
    assert _cli("server", "policy", "set", str(_file(tmp_path, LOCKS_FEWER))).exit_code == 0  # nothing stored


def test_latet_server_policy_set_asks_before_it_replaces_what_it_cannot_read(tmp_path, monkeypatch, you, terminal):
    answer = {"etag": "x", "keys": ["profile"], "exceptions": "personal", "notices": [], "problems": []}
    unread = {"policy": None, "problems": ["the server's policy cannot be read"]}
    malformed = [["not", "an", "object"], {"policy": "text", "problems": []}, {"policy": {**STORED, "keys": "profile"}}]
    wrong_problems = {"policy": None, "problems": "x"}
    path = str(_file(tmp_path, LOCKS_FEWER))
    for held in (unread, *malformed, wrong_problems):
        terminal.at = False
        server = Server(held, held, held, answer)
        monkeypatch.setattr("latet.cli.server.admin_call", server)
        refused = _cli("server", "policy", "set", path)
        assert refused.exit_code == 2 and "nothing can be compared" in refused.output and "--yes" in refused.output
        assert [c[0] for c in server.calls] == ["GET"]
        terminal.at = True
        declined = _cli("server", "policy", "set", path, input="n\n")
        assert declined.exit_code == 1 and [c[0] for c in server.calls] == ["GET", "GET"]
        done = _cli("server", "policy", "set", path, "--yes")
        assert done.exit_code == 0 and "nothing can be compared" in done.output
        assert [c[0] for c in server.calls] == ["GET", "GET", "GET", "PUT"]


E1, E2 = "1" * 64, "2" * 64
SET = {"etag": "x", "keys": ["profile", "terms"], "exceptions": "personal", "notices": [], "problems": []}


def _held_at(etag, exceptions="personal"):
    """The server's GET as this release answers it: the policy, and the etag a set replacing it names."""
    return {"policy": {**STORED, "etag": etag, "exceptions": exceptions}, "problems": [], "etag": etag}


def _changed():
    from latet.ee.policy_routes import CHANGED

    return ServerError(409, CHANGED)


def test_latet_server_policy_set_names_the_policy_it_compared_with(tmp_path, monkeypatch, you, terminal):
    """Final review L3: the PUT names the etag of the policy the GET answered, null when none is stored, also one that
    cannot be read here; an answer that names none (a server of an earlier release, which checks none) sends none."""
    unread = {"policy": None, "problems": ["the server's policy cannot be read"], "etag": E2}
    server = Server({**NONE, "etag": None}, SET, _held_at(E1), SET, unread, SET, HELD, SET)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    path = str(_file(tmp_path))
    for _ in range(3):
        assert _cli("server", "policy", "set", path, "--yes").exit_code == 0
    assert [call[2] for call in server.calls if call[0] == "PUT"] == [
        {"policy": POLICY, "etag": None},
        {"policy": POLICY, "etag": E1},
        {"policy": POLICY, "etag": E2},
    ]
    assert _cli("server", "policy", "set", path).exit_code == 0
    assert server.calls[-1] == ("PUT", "ee/policy", {"policy": POLICY})


def test_latet_server_policy_set_asks_again_when_the_policy_changed_meanwhile(tmp_path, monkeypatch, you, terminal):
    """Final review L3: a policy set between the command's GET and its PUT (409): read again, compared again, and asked
    again, since the file replaces someone's change; declined, nothing more is sent."""
    path = str(_file(tmp_path))  # it locks profile and terms, each person's exceptions counting
    server = Server(_held_at(E1), _changed(), _held_at(E2, "company"), SET)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    done = _cli("server", "policy", "set", path, input="y\n")
    assert done.exit_code == 0, done.output
    assert [(c[0], (c[2] or {}).get("etag")) for c in server.calls] == [("GET", None), ("PUT", E1), ("GET", None),
                                                                        ("PUT", E2)]  # fmt: skip
    assert "changed since this command read it" in done.output
    assert "lets each person add their own exceptions again" in done.output
    server = Server(_held_at(E1), _changed(), _held_at(E2))
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    declined = _cli("server", "policy", "set", path, input="n\n")
    assert declined.exit_code == 1 and [c[0] for c in server.calls] == ["GET", "PUT", "GET"]
    terminal.at = False
    server = Server(_held_at(E1), _changed(), _held_at(E2))
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    refused = _cli("server", "policy", "set", path)
    assert (
        refused.exit_code == 2 and "--yes" in refused.output and [c[0] for c in server.calls] == ["GET", "PUT", "GET"]
    )
    server = Server(_held_at(E1), _changed(), _held_at(E2), SET)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    agreed = _cli("server", "policy", "set", path, "--yes")
    assert agreed.exit_code == 0 and server.calls[-1][2]["etag"] == E2
    assert "changed since this command read it" in agreed.output


def test_latet_server_policy_set_stops_when_the_policy_keeps_changing(tmp_path, monkeypatch, you, terminal):
    terminal.at = False
    path = str(_file(tmp_path))
    server = Server(*[_held_at(E1), _changed()] * 3)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    refused = _cli("server", "policy", "set", path, "--yes")
    assert refused.exit_code == 2 and "kept changing" in refused.output and len(server.calls) == 6
    server = Server(_held_at(E1), ServerError(409, f"another refusal {ESC}[2J"))  # said as a refusal, nothing again
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    other = _cli("server", "policy", "set", path, "--yes")
    assert other.exit_code == 2 and "Not changed: another refusal" in other.output and len(server.calls) == 2
    assert ESC not in other.output


def test_latet_server_policy_show_writes_its_text_only_to_the_file_asked(tmp_path, monkeypatch):
    server = Server(*[HELD] * 3)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    shown = _cli("server", "policy", "show")
    assert shown.exit_code == 0, shown.output
    assert "by admin:t_ab12" in shown.output and "In force: yes" in shown.output
    assert (
        "  It sets: profile, terms\n  Its profile locks too: pii, id_regions, include_private_ips, data, images\n"
        in shown.output
    )
    as_json = _cli("server", "policy", "show", "--json")
    assert "Project Falcon" not in shown.output + as_json.output and '"etag": "3f1c9a7e52d04b18"' in as_json.output
    assert _cli("server", "policy", "show", "--file", str(tmp_path / "out.yaml")).exit_code == 0
    assert (tmp_path / "out.yaml").read_text("utf-8") == POLICY


def test_latet_server_policy_show_escapes_what_the_server_says(monkeypatch):
    stored = {**STORED, "set_by": f"admin:{ESC}[31m{BIDI}", "notices": [f"n{ESC}[2J"]}
    server = Server(
        {"policy": stored, "problems": [f"p{ESC}[1m"]},
        {"policy": None, "problems": [f"q{ESC}[1m"]},
        NotConnected(f"no{ESC}[1m connection"),
    )
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    shown = _cli("server", "policy", "show")
    assert shown.exit_code == 0 and ESC not in shown.output and BIDI not in shown.output
    assert shown.output.count("\\x1b") == 3
    unreadable = _cli("server", "policy", "show")
    assert unreadable.exit_code == 1 and ESC not in unreadable.output and "q\\x1b[1m" in unreadable.output
    assert "has no policy" not in unreadable.output  # one is stored: it cannot be read
    unreached = _cli("server", "policy", "show")
    assert unreached.exit_code == 2 and ESC not in unreached.output and "Not shown" in unreached.output


def test_latet_server_policy_show_says_there_is_none_and_json_is_escaped(tmp_path, monkeypatch):
    stored = {**STORED, "set_by": f"admin:{ESC}[31m"}
    server = Server(NONE, NONE, {"policy": stored, "problems": []}, NONE)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    none = _cli("server", "policy", "show")
    assert none.exit_code == 0 and "has no policy" in none.output
    as_json = _cli("server", "policy", "show", "--json")
    assert as_json.exit_code == 0 and json.loads(as_json.output) == {"policy": None, "problems": []}
    escaped = _cli("server", "policy", "show", "--json")
    assert ESC not in escaped.output and json.loads(escaped.output)["policy"]["set_by"] == stored["set_by"]
    out = tmp_path / "out.yaml"
    nothing = _cli("server", "policy", "show", "--file", str(out))
    assert nothing.exit_code == 2 and not out.exists()


def test_latet_server_policy_show_says_an_answer_that_is_no_policy(monkeypatch):
    server = Server({"policy": {"keys": "profile"}, "problems": []}, ["not", "an", "object"])
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    for _ in range(2):
        shown = _cli("server", "policy", "show")
        assert shown.exit_code == 1 and "not a policy" in shown.output
        assert "Traceback" not in shown.output


def test_latet_server_policy_remove_asks_first_and_needs_yes_without_a_terminal(you, monkeypatch):
    server = Server(HELD, {"removed": True})
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: False)
    refused = _cli("server", "policy", "remove")
    assert refused.exit_code == 2 and server.calls == []
    done = _cli("server", "policy", "remove", "--yes")
    assert done.exit_code == 0 and "is removed" in done.output
    assert server.calls == [("GET", "ee/policy", None), ("DELETE", "ee/policy", None)]  # an answer naming no etag


def test_latet_server_policy_remove_asks_at_a_terminal_and_is_refused_to_an_agent(you, monkeypatch, terminal):
    server = Server(NONE, NONE, {"removed": False}, {"removed": True})
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    assert _cli("server", "policy", "remove", input="n\n").exit_code == 1 and [c[0] for c in server.calls] == ["GET"]
    assert "has no policy" in _cli("server", "policy", "remove", input="y\n").output
    monkeypatch.setenv("CLAUDECODE", "1")
    agent = _cli("server", "policy", "remove", "--yes")
    assert agent.exit_code == 2 and "run it yourself" in agent.output and len(server.calls) == 3


def test_latet_server_policy_remove_says_a_refusal_escaped(you, monkeypatch):
    for answers in ((ServerError(403, f"no{ESC}[1m"),), (HELD, ServerError(403, f"no{ESC}[1m"))):
        monkeypatch.setattr("latet.cli.server.admin_call", Server(*answers))
        refused = _cli("server", "policy", "remove", "--yes")
        assert refused.exit_code == 2 and ESC not in refused.output and "Not removed: no" in refused.output


def test_latet_server_policy_remove_names_the_policy_it_read_and_asks_again_when_it_changed(monkeypatch, you, terminal):
    """As `set` does: the DELETE names the etag of the policy the GET answered (null: none stored), so that a policy set
    meanwhile is never removed unseen; on the server's 409 it reads the policy again and asks again."""
    removed = {"removed": True}
    server = Server(_held_at(E1), removed, {**NONE, "etag": None}, {"removed": False})
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    assert _cli("server", "policy", "remove", "--yes").exit_code == 0
    assert "has no policy" in _cli("server", "policy", "remove", "--yes").output
    assert [c[2] for c in server.calls if c[0] == "DELETE"] == [{"etag": E1}, {"etag": None}]
    server = Server(_held_at(E1), _changed(), _held_at(E2), removed)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    done = _cli("server", "policy", "remove", input="y\ny\n")
    assert done.exit_code == 0 and "is removed" in done.output, done.output
    assert [(c[0], (c[2] or {}).get("etag")) for c in server.calls] == [("GET", None), ("DELETE", E1), ("GET", None),
                                                                        ("DELETE", E2)]  # fmt: skip
    assert "changed since this command read it" in done.output
    server = Server(_held_at(E1), _changed(), _held_at(E2))
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    declined = _cli("server", "policy", "remove", input="y\nn\n")
    assert declined.exit_code == 1 and [c[0] for c in server.calls] == ["GET", "DELETE", "GET"]
    terminal.at = False
    server = Server(_held_at(E1), _changed(), _held_at(E2), removed)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    agreed = _cli("server", "policy", "remove", "--yes")
    assert agreed.exit_code == 0 and server.calls[-1][2] == {"etag": E2}
    assert "changed since this command read it" in agreed.output
    server = Server(*[_held_at(E1), _changed()] * 3)
    monkeypatch.setattr("latet.cli.server.admin_call", server)
    stopped = _cli("server", "policy", "remove", "--yes")
    assert stopped.exit_code == 2 and "kept changing" in stopped.output and len(server.calls) == 6


def test_policy_checks_say_where_the_policy_comes_from_and_what_is_wrong():
    from latet.doctor.checks import FAIL, OK, WARN, Check
    from latet.ee.policy_sources import policy_checks

    said = {
        "sources": [{"source": "server", "origin": URL, "received": 1_791_191_520.0, "notices": 2}],
        "problems": [{"level": "warn", "text": "not in force"}, {"level": "fail", "text": "unreadable"}],
    }
    lines = policy_checks(Source(summary=said))
    assert lines[0] == Check(WARN, "Policy", "not in force") and lines[1] == Check(FAIL, "Policy", "unreadable")
    assert lines[2] == Check(OK, "Policy", f"{URL}, set 2026-10-05 09:12 UTC")  # the server's zone is no one's
    assert lines[3] == Check(WARN, "Policy", "2 settings in it are not read")
    assert policy_checks(Source()) == []  # a server without a policy has no policy line


@pytest.mark.server
@services
def test_latet_server_status_has_the_servers_policy_line(server, licensed):
    from latet.server.app import create_server_app

    server.edition.install_license(licensed.make(expires="2099-12-31"), actor="admin:t1")  # R8: E1's test license
    server.edition.refresh()
    server.edition.policy.set(POLICY, actor="admin:tests")
    server.edition.policy.committed()
    client = TestClient(create_server_app(server), base_url=URL)
    client.headers["x-latet-admin"] = server.admin.issue("tests")
    lines = client.get("/_latet/server/v1/admin/status").json()["edition"]
    [line] = [line for line in lines if line["title"] == "Policy"]
    assert line["status"] == "ok" and line["detail"].startswith(f"{URL}, set ")
