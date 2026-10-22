"""The guard of the server's tests (tests/server/conftest.py): they reach PostgreSQL and Valkey on this computer alone,
and are skipped, saying why, when the two addresses are unset, name another computer, or do not answer. None of these
needs the services."""

import ast
import contextlib
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ...server import conftest as guard

ROOT = Path(__file__).resolve().parents[3]
DATABASE = "postgresql://latet:s3cret-pw@127.0.0.1:55432/latet"
VALKEY = "redis://:s3cret-pw@127.0.0.1:56379/0"


@pytest.fixture(autouse=True)
def _libpq_as_written(monkeypatch):
    """The addresses are judged as they are written: this computer's PGHOST and the like change what libpq reads."""
    for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGREQUIREAUTH"):
        monkeypatch.delenv(name, raising=False)


def test_addresses_of_this_computer_are_let_through():
    assert guard._refusal(DATABASE, VALKEY) == ""
    assert guard._refusal("postgresql://u:p@[::1]:5432/db", "redis://:p@[::1]:6379/0") == ""
    assert guard._refusal("postgresql://u:p@127.0.0.1:1,[::1]:2/db?hostaddr=127.0.0.1", VALKEY) == ""


@pytest.mark.parametrize(
    ("database", "valkey", "words"),
    [
        ("", "", ["LATET_TEST_DATABASE_URL and LATET_TEST_VALKEY_URL not set", "scripts/server-services.py up"]),
        (DATABASE, "", ["LATET_TEST_VALKEY_URL not set"]),
        ("postgresql://u:s3cret-pw@127.0.0.1:5432,remote.example:5432/db", VALKEY, ["remote.example"]),
        ("postgresql://u:s3cret-pw@127.0.0.1/db?host=remote.example", VALKEY, ["remote.example"]),
        ("postgresql://u:s3cret-pw@127.0.0.1/db?hostaddr=10.0.0.5", VALKEY, ["10.0.0.5"]),
        ("postgresql://u:s3cret-pw@127.0.0.1,/db", VALKEY, ["default host"]),
        ("host=127.0.0.1 hostaddr=,10.0.0.5 password=s3cret-pw", VALKEY, ["default host"]),
        ("postgresql://u:s3cret-pw@localhost:55432/latet", VALKEY, ["localhost", "127.0.0.1 or ::1"]),
        ("postgresql://u:s3cret-pw@127.0.0.1/db?service=prod", VALKEY, ["service file"]),
        ("postgresql://u:s3cret-pw@/latet", VALKEY, ["no host"]),
        ("a database, s3cret-pw", VALKEY, ["not an address"]),
        (DATABASE, "redis://:s3cret-pw@cache.example:6379/0", ["cache.example"]),
        (DATABASE, "redis://:s3cret-pw@localhost:56379/0", ["localhost", "127.0.0.1 or ::1"]),
        (DATABASE, "redis://:s3cret-pw@/0?host=cache.example", ["no host"]),
        (DATABASE, "redis://:s3cret-pw@127.0.0.1:6379,cache.example:6379/0", ["not an address"]),
    ],
)
def test_the_servers_tests_are_skipped_with_the_reason(database, valkey, words):
    reason = guard._refusal(database, valkey)
    assert reason and all(word in reason for word in words), reason
    assert "s3cret-pw" not in reason


def test_what_libpq_takes_from_the_environment_counts_too(monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "10.0.0.5")
    assert "10.0.0.5" in guard._refusal(DATABASE, VALKEY)
    monkeypatch.delenv("PGHOSTADDR")
    monkeypatch.setenv("PGSERVICE", "prod")
    assert "service file" in guard._refusal(DATABASE, VALKEY)


@pytest.mark.parametrize("value", ["scram-sha-256", ""])
def test_a_variable_the_server_refuses_to_start_with_skips_the_tests_with_a_reason(monkeypatch, value):
    """``Database`` refuses PGSERVICE (even an empty one, which libpq takes for a service of that name) and
    PGREQUIREAUTH (the address not saying require_auth): a computer that has one would otherwise meet an error in every
    test, where it should meet a skip that says why."""
    for variable in ("PGSERVICE", "PGREQUIREAUTH"):
        monkeypatch.setenv(variable, value)
        reason = guard._refusal(DATABASE, VALKEY)
        assert variable in reason and "s3cret-pw" not in reason, (variable, reason)
        monkeypatch.delenv(variable)
    assert guard._refusal(DATABASE, VALKEY) == ""


def test_a_require_auth_of_the_test_address_itself_sets_the_variable_aside(monkeypatch):
    monkeypatch.setenv("PGREQUIREAUTH", "scram-sha-256")
    assert guard._refusal(DATABASE + "?require_auth=scram-sha-256", VALKEY) == ""  # as Database allows it


def test_the_services_are_asked_once_for_the_whole_session(monkeypatch):
    asked = []
    monkeypatch.setattr(guard, "DATABASE", DATABASE)
    monkeypatch.setattr(guard, "VALKEY", VALKEY)
    monkeypatch.setattr(guard, "_silent", lambda database, valkey: asked.append((database, valkey)) or "none answers")
    guard._unavailable.cache_clear()
    try:
        assert guard._unavailable() == guard._unavailable() == ("none answers", "")
        assert asked == [(DATABASE, VALKEY)]
    finally:
        guard._unavailable.cache_clear()


def test_an_address_of_another_computer_is_never_asked(monkeypatch):
    def asked(database, valkey):
        raise AssertionError("connected to " + database)

    monkeypatch.setattr(guard, "DATABASE", "postgresql://u:p@db.example/db")
    monkeypatch.setattr(guard, "VALKEY", VALKEY)
    monkeypatch.setattr(guard, "_silent", asked)
    monkeypatch.setattr(guard, "_refused", asked)
    guard._unavailable.cache_clear()
    try:
        assert "db.example" in guard._unavailable()[0]
    finally:
        guard._unavailable.cache_clear()


def test_a_service_that_answers_and_refuses_is_no_reason_to_skip():
    """Only an address where nothing accepts a connection skips the server's tests: one where a service answers and
    refuses (a wrong password, a database that is not there) lets them run and fail, saying why, so that a mistake in
    an address is never taken for services that are down (the CI job's above all)."""
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen(8)
        port = listening.getsockname()[1]
        valkey = f"redis://:wrong-pw@127.0.0.1:{port}/0"
        assert guard._silent(f"postgresql://latet:wrong-pw@127.0.0.1:{port}/latet", valkey) == ""
        either = f"postgresql://latet:wrong-pw@127.0.0.1:{_closed_ports(1)[0]},127.0.0.1:{port}/latet"
        assert guard._silent(either, valkey) == ""  # one of its hosts answers


@pytest.mark.parametrize(("required", "outcome"), [("1", pytest.fail.Exception), (None, pytest.skip.Exception)])
def test_where_the_services_are_required_a_reason_to_skip_fails_the_test(monkeypatch, required, outcome):
    """CI's job requires the services (LATET_TEST_REQUIRE_SERVICES=1): there a server's test that would be skipped
    fails, saying why, so that the job is never green with the server's tests not run."""
    if required is None:
        monkeypatch.delenv("LATET_TEST_REQUIRE_SERVICES", raising=False)
    else:
        monkeypatch.setenv("LATET_TEST_REQUIRE_SERVICES", required)
    # Both caught here, so that the one this test does not expect fails it (a skip let out would skip it).
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as raised:
        guard.unmet("Valkey does not answer at 127.0.0.1:56379")
    assert raised.type is outcome and "Valkey does not answer" in str(raised.value)


def _marked(source: str) -> bool:
    """Whether a module's ``pytestmark``, as it stands once the module has run (its last assignment), holds
    ``pytest.mark.server``, alone or among other marks."""
    marks: list[ast.expr] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            marks = node.value.elts if isinstance(node.value, ast.List | ast.Tuple) else [node.value]
    return any(ast.unparse(mark) == "pytest.mark.server" for mark in marks)


def test_a_mark_is_read_as_the_module_leaves_it():
    assert _marked("import pytest\npytestmark = [pytest.mark.slow, pytest.mark.server]\n")
    assert not _marked("import pytest\npytestmark = pytest.mark.server\npytestmark = pytest.mark.slow\n")
    assert not _marked("import pytest\n")


def test_every_module_of_the_servers_tests_carries_their_mark_so_that_ci_runs_it():
    """CI's job runs the server's tests by their mark (``-m server``), and the jobs without the services skip them: a
    module of tests/server without the mark would run nowhere, every job green all the same. pytest collects
    ``test_*.py`` and ``*_test.py``."""
    folder = ROOT / "tests" / "server"
    modules = sorted({*folder.rglob("test_*.py"), *folder.rglob("*_test.py")})
    assert modules and [path.name for path in modules if not _marked(path.read_text("utf-8"))] == []


@pytest.mark.parametrize(("required", "outcome"), [("1", "failed"), (None, "skipped")])
def test_where_the_services_are_required_any_skip_of_a_servers_test_fails_it(monkeypatch, required, outcome):
    """A server's test that skips by itself (its own ``pytest.skip``, a mark), not through ``unmet``, fails too where
    the services are required, saying why; an expected failure stays what it is."""
    if required is None:
        monkeypatch.delenv("LATET_TEST_REQUIRE_SERVICES", raising=False)
    else:
        monkeypatch.setenv("LATET_TEST_REQUIRE_SERVICES", required)
    where = ("tests/server/test_spaces.py", 365, "test_x")
    skipped = pytest.TestReport(
        "tests/server/test_spaces.py::test_x", where, {}, "skipped",
        ("tests/server/test_spaces.py", 366, "Skipped: no count of a process's own memory on this system"), "call",
    )  # fmt: skip
    guard.required(skipped)
    assert skipped.outcome == outcome
    if outcome == "failed":
        said = str(skipped.longrepr)
        assert "no count of a process's own memory" in said and "LATET_TEST_REQUIRE_SERVICES" in said
    expected = pytest.TestReport("tests/server/test_spaces.py::test_x", where, {}, "skipped", None, "call")
    expected.wasxfail = "a known fault"
    guard.required(expected)
    assert expected.outcome == "skipped"


def test_a_server_test_skipped_by_a_mark_fails_the_run_where_the_services_are_required(tmp_path):
    """The whole thing, as CI's job meets it: a plugin of the test's own marks a server's test skipped, and the run
    fails, saying why; without LATET_TEST_REQUIRE_SERVICES it is skipped."""
    (tmp_path / "t16_skip_plugin.py").write_text(
        "import pytest\n\n\ndef pytest_collection_modifyitems(items):\n"
        "    for item in items:\n        item.add_marker(pytest.mark.skip(reason='skipped by a plugin of the test'))\n",
        "utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PG", "LATET_TEST_"))}
    env["PYTHONPATH"] = str(tmp_path)
    argv = [sys.executable, "-m", "pytest", "tests/server/test_services.py", "-q", "-p", "no:cacheprovider", "-p",
            "t16_skip_plugin"]  # fmt: skip
    for required, code, words in (("1", 1, "LATET_TEST_REQUIRE_SERVICES"), (None, 0, "1 skipped")):
        run_env = {**env, "LATET_TEST_REQUIRE_SERVICES": required} if required else env
        done = subprocess.run(argv, cwd=ROOT, env=run_env, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=120)  # fmt: skip
        said = done.stdout + done.stderr
        assert done.returncode == code and words in said, said


@pytest.mark.parametrize(
    "database",
    ["postgresql://latet:s3cret@pw-tail@127.0.0.1:55432/latet", "postgresql://latet:s3cr@et-pw-tail@127.0.0.1/latet"],
)
def test_a_password_with_a_raw_at_is_not_shown_by_the_host_libpq_reads_out_of_it(database):
    """libpq ends a URL's user part at the first @: the rest of the password, and the host, are read as the host. The
    skip names no host that does not look like one."""
    reason = guard._refusal(database, VALKEY)
    assert reason and "a host that cannot be read" in reason and "%40" in reason
    assert "pw-tail" not in reason and "s3cr" not in reason
    assert "remote.example" in guard._refusal("postgresql://u:p@127.0.0.1/db?host=remote.example", VALKEY)


@contextlib.contextmanager
def _refusing_peer():
    """A listener that takes each connection and closes it at once: a service that answers on TCP and refuses what a
    driver asks of it, as one does a password that is not its own."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)

    def serve():
        while True:
            try:
                peer, _ = listener.accept()
            except OSError:
                return
            peer.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def test_a_service_that_answers_and_refuses_the_tests_is_not_said_to_be_silent():
    with _refusing_peer() as port:
        database = f"postgresql://latet:s3cret-pw@127.0.0.1:{port}/latet?connect_timeout=2"
        valkey = f"redis://:s3cret-pw@127.0.0.1:{port}/0"
        assert guard._silent(database, valkey) == ""  # something answers: no skip
        refused = guard._refused(database, valkey)
    assert f"PostgreSQL answers at 127.0.0.1:{port} and refuses the tests' connection" in refused
    assert f"Valkey answers at 127.0.0.1:{port} and refuses the tests' connection" in refused
    assert "s3cret-pw" not in refused and "scripts/server-services.py env" in refused


def test_a_service_that_refuses_the_tests_makes_them_fail_and_says_why():
    """The whole thing, as a person meets it: the variables set, a service that answers and refuses. The server's tests
    fail with the reason, where a skip would have hidden it."""
    with _refusing_peer() as port:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PG", "LATET_TEST_"))}
        env |= {
            "LATET_TEST_DATABASE_URL": f"postgresql://latet:s3cret-pw@127.0.0.1:{port}/latet",
            "LATET_TEST_VALKEY_URL": f"redis://:s3cret-pw@127.0.0.1:{port}/0",
            "PGCONNECT_TIMEOUT": "2",
        }
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/server/test_services.py", "-q", "-p", "no:cacheprovider"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    said = done.stdout + done.stderr
    assert done.returncode != 0 and "1 error" in said and "skipped" not in said, said  # at its setup: not run
    assert "refuses the tests' connection" in said and "s3cret-pw" not in said


def _closed_ports(count):
    held = [socket.socket() for _ in range(count)]
    try:
        for one in held:
            one.bind(("127.0.0.1", 0))
        return [one.getsockname()[1] for one in held]
    finally:
        for one in held:
            one.close()


def test_a_service_that_does_not_answer_skips_the_servers_tests_and_never_hangs():
    """The whole thing, as a person meets it: the two variables set, nothing listening. A pytest of its own runs the
    server's tests; on Windows psycopg waits for ever on a closed port unless libpq is given a time limit."""
    database_port, valkey_port = _closed_ports(2)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PG", "LATET_TEST_"))}
    env |= {
        "LATET_TEST_DATABASE_URL": f"postgresql://latet:s3cret-pw@127.0.0.1:{database_port}/latet",
        "LATET_TEST_VALKEY_URL": f"redis://:s3cret-pw@127.0.0.1:{valkey_port}/0",
        "PGCONNECT_TIMEOUT": "2",
    }
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/server/test_services.py", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    said = done.stdout + done.stderr
    assert done.returncode == 0 and "1 skipped" in said, said
    assert f"PostgreSQL does not answer at 127.0.0.1:{database_port}" in said
    assert f"Valkey does not answer at 127.0.0.1:{valkey_port}" in said
    assert "uv run python scripts/server-services.py up" in said
    assert "s3cret-pw" not in said
