"""R21: a way out of a repository that stays unreadable, decided by the person at their own terminal.

``latet repositories`` lists the repositories a computer remembers, each with its state (read, nothing to weigh, or
not readable now and why, by category); ``latet repositories forget <folder>`` takes one out of the list under the
list's lock, merged before it is written. Forgetting lowers protection (that repository is weighed again only when a
conversation reaches it), so it refuses an agent. The running daemon forgets it too, or its memory would write it back.
"""

import json
from pathlib import Path

import pytest
from starlette.requests import Request
from typer.testing import CliRunner

from latet import projects as projects_module
from latet.api import rest
from latet.config import Config, IdentityConfig, data_dir, save_config
from latet.projects import PROJECT_FILE
from latet.runtime import make_runtime
from latet.state.local import LocalRepositories, LocalSettings

from ..conftest import ROOT_KEY


def _repo(base: Path, name: str, raw: bytes | None = b"profile: max\n") -> Path:
    root = base / name
    (root / ".git").mkdir(parents=True)
    if raw is not None:
        (root / PROJECT_FILE).write_bytes(raw)
    return root


def _unreadable(monkeypatch, root: Path) -> None:
    original = projects_module.own_file

    def refused(path, most, **kwargs):
        if Path(path) == root / PROJECT_FILE:
            raise PermissionError("private repository marker")
        return original(path, most, **kwargs)

    monkeypatch.setattr(projects_module, "own_file", refused)


def _cli(*args: str, input: str | None = None):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


@pytest.fixture
def no_daemon(monkeypatch):
    from latet.cli import admin

    monkeypatch.setattr(admin, "_admin", lambda *args, **kwargs: None)


@pytest.fixture
def not_an_agent(monkeypatch):
    """You, at a terminal of your own: no agent's marker, and a terminal that answers ``forget``'s question."""
    from latet.cli import agent

    monkeypatch.setattr(agent, "agent_name", lambda strict=False: None)
    monkeypatch.setattr(agent, "at_your_terminal", lambda strict=False: True)


def test_the_list_says_each_repositorys_state_and_never_what_its_file_holds(computer, no_daemon, monkeypatch):
    strict = _repo(computer, "strict")
    broken = _repo(computer, "broken", b"profile: [max\n")
    away = computer / "away"
    refused = _repo(computer, "refused")
    _unreadable(monkeypatch, refused)
    LocalRepositories(data_dir()).save([str(strict), str(broken), str(away), str(refused)])
    result = _cli("repositories")
    assert result.exit_code == 0, result.output

    def line(folder: Path) -> str:
        [found] = [said for said in result.output.splitlines() if said.strip().startswith(f"{folder}  ")]
        return found

    assert line(strict).rstrip().endswith("  read")
    assert line(broken).rstrip().endswith("nothing to weigh: not valid")
    assert line(away).rstrip().endswith("nothing to weigh: not there now")
    assert line(refused).rstrip().endswith("unavailable: not readable now")
    assert "max" not in result.output and "private repository marker" not in result.output
    assert "latet repositories forget" in result.output  # how to take an unreadable one out


def test_forgetting_a_repository_refuses_an_agent(computer, no_daemon, monkeypatch):
    from latet.cli import agent

    strict = _repo(computer, "strict")
    LocalRepositories(data_dir()).save([str(strict)])
    monkeypatch.setattr(agent, "agent_name", lambda strict=False: "claude-code")
    result = _cli("repositories", "forget", str(strict))
    assert result.exit_code == 2 and "run it yourself" in result.output
    assert LocalRepositories(data_dir()).load() == [str(strict)]


def test_forgetting_a_repository_with_no_daemon_takes_it_out_of_the_list_and_says_what_that_means(
    computer, no_daemon, not_an_agent
):
    strict, other = _repo(computer, "strict"), _repo(computer, "other")
    LocalRepositories(data_dir()).save([str(strict), str(other)])
    declined = _cli("repositories", "forget", str(strict), input="n\n")
    assert declined.exit_code == 1 and "nothing changed" in declined.output.lower()
    assert LocalRepositories(data_dir()).load() == [str(strict), str(other)]
    result = _cli("repositories", "forget", str(strict), input="y\n")
    assert result.exit_code == 0, result.output
    assert "weighed again when a conversation reaches it" in result.output
    assert LocalRepositories(data_dir()).load() == [str(other)]
    unknown = _cli("repositories", "forget", str(computer / "never-seen"), input="y\n")
    assert unknown.exit_code == 1 and "not one Latet remembers" in unknown.output


def test_forgetting_needs_a_terminal_of_your_own(computer, no_daemon, monkeypatch):
    """R25 N2: as approving a change does, ``forget`` asks you at a terminal of your own; run where none answers (a
    script, an agent whose markers Latet does not know), it refuses and changes nothing."""
    from latet.cli import agent

    strict = _repo(computer, "strict")
    LocalRepositories(data_dir()).save([str(strict)])
    monkeypatch.setattr(agent, "agent_name", lambda strict=False: None)
    monkeypatch.setattr(agent, "at_your_terminal", lambda strict=False: False)
    result = _cli("repositories", "forget", str(strict), input="y\n")
    assert result.exit_code == 2 and "terminal of your own" in result.output
    assert LocalRepositories(data_dir()).load() == [str(strict)]


def test_a_daemon_that_runs_and_does_not_answer_is_never_gone_around(computer, no_daemon, not_an_agent):
    """R25 N1: a running daemon (its PID file names a live process) that did not answer keeps the repository in its
    memory, and would write it back: the file is not changed behind it."""
    from latet.service.daemon import PID_FILE

    strict = _repo(computer, "strict")
    LocalRepositories(data_dir()).save([str(strict)])
    (data_dir() / PID_FILE).write_text(str(__import__("os").getpid()))  # this very process: alive
    result = _cli("repositories", "forget", str(strict), input="y\n")
    assert result.exit_code == 1 and "the daemon did not answer: nothing changed" in result.output.lower()
    assert "latet stop" in result.output
    assert LocalRepositories(data_dir()).load() == [str(strict)]


def test_another_daemon_that_answered_is_said_as_such(computer, monkeypatch, not_an_agent):
    """R25 N1: a 401 is a daemon of another data folder answering on the port: said so, nothing changed."""
    from latet.cli import admin

    strict = _repo(computer, "strict")
    LocalRepositories(data_dir()).save([str(strict)])
    monkeypatch.setattr(admin, "_admin", lambda *args, **kwargs: {"error": "unauthorized", "status": 401})
    result = _cli("repositories", "forget", str(strict), input="y\n")
    said = " ".join(result.output.split())  # the folder's own name holds this test's words: the sentence is checked
    assert result.exit_code == 1 and "Another daemon answered" in said and "it did not take this one's token" in said
    assert "cannot be read" not in said
    assert "nothing changed" in said and LocalRepositories(data_dir()).load() == [str(strict)]


def test_a_list_that_cannot_be_read_is_said_with_its_way_out(computer, no_daemon):
    """R25 N4: a repositories.json that is not read (a folder, a link, a file past its size) holds back every change
    that could lower a repository's protection: ``latet repositories`` says how to get out of it."""
    from latet.state.local import REPOSITORIES_FILE

    (data_dir() / REPOSITORIES_FILE).mkdir(parents=True)
    result = _cli("repositories")
    assert result.exit_code == 0, result.output
    assert "cannot be read now" in result.output and "move" in result.output and REPOSITORIES_FILE in result.output
    assert "as conversations reach them" in result.output


@pytest.mark.parametrize("reader", ["weighed", "entries"])
def test_a_forget_landing_while_the_list_is_read_is_never_undone(tmp_path, reader):
    """R25 N5: the list is read under its lock for a check (``weighed``) and for ``latet repositories`` (``entries``),
    as for a forget: a forget that lands meanwhile waits, and is not merged back from what was read a moment before."""
    import threading

    from latet.projects import SeenRepositories

    strict = _repo(tmp_path, "strict")
    store = LocalRepositories(tmp_path / "data")
    store.save([str(strict)])
    seen = SeenRepositories(store)
    real, calls, forgetting = store.load, [], []

    def load():
        calls.append(1)
        found = real()
        if len(calls) == 1:  # the check's read: a forget lands now, from another thread
            forgetting.append(threading.Thread(target=seen.forget, args=(str(strict),)))
            forgetting[0].start()
            forgetting[0].join(0.3)
        return found

    store.load = load  # type: ignore[method-assign]
    getattr(seen, reader)()
    forgetting[0].join(10)
    store.load = real  # type: ignore[method-assign]
    folders, _ = seen.entries()
    assert str(strict) not in folders and store.load() == []


def test_forgetting_asks_the_running_daemon_which_forgets_it_too(computer, monkeypatch, not_an_agent):
    from latet.cli import admin

    asked = []
    monkeypatch.setattr(admin, "_admin", lambda *args, **kwargs: asked.append(args) or {"forgotten": True})
    folder = "C:/dev/strict" if __import__("os").name == "nt" else "/dev/strict"
    result = _cli("repositories", "forget", folder, input="y\n")
    assert result.exit_code == 0, result.output
    assert asked and asked[0][:2] == ("POST", "/_latet/admin/repositories/forget")


def _request(rt, data=None, method="POST"):
    body = json.dumps(data or {}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"x-latet-token", rt.gateway.bridge_token.encode())]
    return Request({"type": "http", "method": method, "path": "/", "query_string": b"", "headers": headers}, receive)


async def test_the_daemon_forgets_a_repository_under_its_lock_and_a_change_weighs_without_it(tmp_path, monkeypatch):
    """A repository not readable now holds back every change that could lower its protection; forgotten, the change is
    weighed in the others, and the repository comes back to the list once a conversation reaches it."""
    refused, strict = _repo(tmp_path, "refused"), _repo(tmp_path, "strict")
    approved = Config(identity=IdentityConfig(enabled=False))
    start = {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}
    LocalSettings(start["config_file"], start["data"]).save_reviewed(approved)
    LocalRepositories(start["data"]).save([str(refused), str(strict)])
    save_config(approved, start["config_file"])
    rt = make_runtime(**start)
    try:
        rt.projects.resolve(str(refused))  # a conversation reached it in this run, before it became unreadable
        _unreadable(monkeypatch, refused)
        dashboard = rest.Dashboard(rt)
        listed = json.loads(bytes((await dashboard.repositories(_request(rt, method="GET"))).body))
        states = {entry["folder"]: (entry["state"], entry["why"]) for entry in listed["repositories"]}
        assert states == {str(refused): ("unavailable", "not readable now"), str(strict): ("read", "")}
        save_config(approved.model_copy(update={"include_private_ips": False}), start["config_file"])
        refusal = await dashboard.reload(_request(rt))
        assert refusal.status_code == 503
        answer = await dashboard.forget_repository(_request(rt, {"folder": str(refused)}))
        assert answer.status_code == 200 and json.loads(bytes(answer.body)) == {"forgotten": True}
        assert LocalRepositories(start["data"]).load() == [str(strict)]
        weighed = json.loads(bytes((await dashboard.reload(_request(rt))).body))
        assert weighed["applied"] is False and any("strict" in r for r in weighed["pending"]["loosened"])
        unknown = await dashboard.forget_repository(_request(rt, {"folder": str(refused)}))
        assert unknown.status_code == 404
        rt.projects.resolve(str(refused))  # a conversation reaches it again
        assert str(refused) in LocalRepositories(start["data"]).load()
    finally:
        rt.close()


async def test_a_list_that_cannot_be_read_now_is_never_written_over_by_a_forget(tmp_path, monkeypatch):
    from latet.state import local as local_module

    strict = _repo(tmp_path, "strict")
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data",
                      config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)  # fmt: skip
    try:
        store = LocalRepositories(tmp_path / "data")
        store.save([str(strict)])
        before = store.path.read_bytes()
        real = local_module.own_file

        def refused(path, most, **kwargs):
            if Path(path) == store.path:
                raise PermissionError("sharing violation")
            return real(path, most, **kwargs)

        monkeypatch.setattr(local_module, "own_file", refused)
        answer = await rest.Dashboard(rt).forget_repository(_request(rt, {"folder": str(strict)}))
        assert answer.status_code == 503 and store.path.read_bytes() == before
    finally:
        rt.close()


@pytest.mark.parametrize("change", [{"port": 5299}, {"retention_days": 7}, {"history": "metadata"}])
async def test_a_change_of_what_does_not_protect_applies_while_a_repository_cannot_be_read(
    tmp_path, monkeypatch, change
):
    """R21 (M1): the port, the history and its retention lower nothing in a repository: none is weighed for them."""
    refused = _repo(tmp_path, "refused")
    approved = Config(identity=IdentityConfig(enabled=False))
    start = {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}
    LocalSettings(start["config_file"], start["data"]).save_reviewed(approved)
    LocalRepositories(start["data"]).save([str(refused)])
    save_config(approved, start["config_file"])
    rt = make_runtime(**start)
    try:
        _unreadable(monkeypatch, refused)
        save_config(approved.model_copy(update=change), start["config_file"])
        answer = await rest.Dashboard(rt).reload(_request(rt))
        assert answer.status_code == 200 and json.loads(bytes(answer.body))["applied"] is True
    finally:
        rt.close()
