"""``latet tutorial``: a sandbox in a temporary folder, on a port of its own, opened in the browser, and gone when it
ends. The sandbox itself is replaced here (``run_sandbox``): what is checked is what the command does around it. It
refuses the container, lists the chapters, refuses a chapter that is not one, makes the folder and the port, switches
the environment to the sandbox's and puts yours back, ends on Ctrl+C or a failure and cleans up, keeps the folder on
request, leaves your own data folder alone (but for the chapters you finished), and deletes the sandboxes a run that
was cut off left behind."""

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from latet.cli import tutorial as command
from latet.tutorial import chapters, sandbox
from latet.tutorial.progress import Progress
from latet.tutorial.sandbox import SandboxError, environment

REAL_WEB = command.WEB  # where the dashboard's files are, before a test points it elsewhere
BASICS = Path(chapters.__file__).parent / "chapters" / "basics.yaml"
# The variables the sandbox runs without or with others than yours.
VARIABLES = (
    "LATET_DATA_DIR",
    "LATET_CONFIG",
    "LATET_PORT",
    "LATET_KEYRING",
    "LATET_ROOT_KEY",
    "LATET_ROOT_KEY_FILE",
    "LATET_BRIDGE_TOKEN",
    "LATET_HOST",
    "LATET_LLM_API_KEY",
)


def variables() -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in VARIABLES}


def tree(folder: Path) -> dict[str, bytes]:
    """Every file under ``folder``, by its path from it."""
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def an_ended_process() -> int:
    """The number of a process that is gone."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


@pytest.fixture
def yours(tmp_path, monkeypatch):
    """Your own Latet: a data folder (with a vault in it) and settings, variables of yours the sandbox must put aside,
    the dashboard built, a temporary folder to make sandboxes in, two chapters written, and no real browser."""
    data, config = tmp_path / "yours" / "data", tmp_path / "yours" / "config" / "config.yaml"
    data.mkdir(parents=True)
    config.parent.mkdir()
    config.write_text("profile: max\n", "utf-8")
    (data / "latet.db").write_bytes(b"your vault")
    (data / "logs").mkdir()
    (data / "logs" / "daemon.log").write_bytes(b"your log")
    temp, web = tmp_path / "temp", tmp_path / "web"
    temp.mkdir()
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", "utf-8")
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setattr(command, "WEB", web)
    for name in ("LATET_PORT", "LATET_KEYRING", "LATET_ROOT_KEY_FILE", "LATET_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LATET_DATA_DIR", str(data))
    monkeypatch.setenv("LATET_CONFIG", str(config))
    monkeypatch.setenv("LATET_ROOT_KEY", "your-root-key")
    monkeypatch.setenv("LATET_BRIDGE_TOKEN", "your-token")
    monkeypatch.setenv("LATET_HOST", "127.0.0.1")
    monkeypatch.setenv("LATET_LLM_API_KEY", "your-reviewer-key")
    folder = tmp_path / "chapters"  # the chapters written are these two, whatever else the package holds
    folder.mkdir()
    basics = yaml.safe_load(BASICS.read_text("utf-8"))
    for cid, other in (("basics", "terms"), ("terms", "basics")):
        text = yaml.safe_dump({**basics, "id": cid, "title": f"Chapter {cid}", "end": {**basics["end"], "next": other}})
        (folder / f"{cid}.yaml").write_bytes(text.encode())
    monkeypatch.setattr(chapters, "_folder", lambda: folder)
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail(f"a real browser was asked to open {url}"))
    return SimpleNamespace(data=data, config=config, temp=temp, web=web)


@pytest.fixture
def run(yours, monkeypatch):
    """``run_sandbox`` replaced by a recorder: what the command gave it, and the world it runs in. ``run.then(call)``
    does more (it may raise, as Ctrl+C or a failure does)."""
    box = SimpleNamespace(calls=[], then=None)

    def run_sandbox(root, port, *, requested, progress, announce, sock, reader=None):
        again = socket.socket()
        try:
            again.bind(("127.0.0.1", port))
            taken = False
        except OSError:
            taken = True  # nobody else can bind the port: the command holds it
        finally:
            again.close()
        pid = root / "sandbox.pid"
        call = SimpleNamespace(
            root=root,
            port=port,
            requested=requested,
            progress=progress,
            announce=announce,
            sock=sock,
            reader=reader,
            env=variables(),
            made=root.is_dir(),
            pid=pid.read_text("utf-8") if pid.is_file() else None,
            bound=sock.getsockname(),
            held=taken,
        )
        box.calls.append(call)
        if box.then is not None:
            box.then(call)

    monkeypatch.setattr(command, "run_sandbox", run_sandbox)
    return box


def invoke(*args: str):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, ["tutorial", *args])


# --- before anything is made --------------------------------------------------------------------------------


def test_it_runs_on_your_computer_not_in_the_container(run, yours, monkeypatch):
    monkeypatch.setenv("LATET_CONTAINER", "1")
    for args in ([], ["--list"], ["basics"]):
        result = invoke(*args)
        assert result.exit_code == 2 and "runs on your computer, not in the container" in result.output, args
        assert "agents print" not in result.output  # that advice is for the commands that point agents at Latet
    assert run.calls == [] and list(yours.temp.iterdir()) == []


def test_list_prints_the_chapters_and_which_you_finished(run, yours):
    Progress(yours.data / "tutorial.json").mark("terms")
    result = invoke("--list")
    assert result.exit_code == 0, result.output
    rows = {line.split()[1]: line for line in result.output.splitlines() if line.strip()[:1].isdigit()}
    assert list(rows) == ["basics", "terms"]  # the chapters written, in the menu's order
    assert rows["basics"].split()[0] == "1" and "Chapter basics" in rows["basics"] and "5 min" in rows["basics"]
    assert rows["terms"].split()[0] == "2" and "Chapter terms" in rows["terms"]
    assert rows["terms"].rstrip().endswith("done") and "to do" in rows["basics"]
    assert run.calls == [] and list(yours.temp.iterdir()) == []  # nothing was made, nothing started


def test_a_chapter_that_is_not_one_is_refused_before_anything_is_made(run, yours):
    for name in ("nonsense", "live"):  # unknown, and in the menu's order but not written yet
        result = invoke(name)
        assert result.exit_code == 2, name
        assert f'There is no chapter "{name}"' in result.output and "basics, terms" in result.output
    assert run.calls == [] and list(yours.temp.iterdir()) == []


def test_the_chapter_named_is_the_one_the_page_opens_first(run, yours):
    assert invoke("terms").exit_code == 0
    assert invoke().exit_code == 0
    assert [call.requested for call in run.calls] == ["terms", None]


def test_a_chapter_file_that_cannot_be_read_is_said(run, yours, monkeypatch):
    def broken():
        raise chapters.ChapterError("chapters/basics.yaml: not a chapter's YAML")

    monkeypatch.setattr(chapters, "load_all", broken)
    result = invoke()
    assert result.exit_code == 1 and "not a chapter's YAML" in result.output
    assert run.calls == [] and list(yours.temp.iterdir()) == []


def test_a_dashboard_that_is_not_built_is_said_and_the_tutorial_goes_on(run, yours, monkeypatch):
    assert "npm run build" not in invoke().output  # built: no hint
    monkeypatch.setattr(command, "WEB", yours.web / "nothing here")
    result = invoke()
    assert result.exit_code == 0 and "npm run build" in result.output and "dashboard" in result.output
    assert len(run.calls) == 2  # the server and its API run without the page: the dashboard's own dev server uses them


def test_the_dashboard_it_looks_for_is_the_one_the_server_serves():
    from latet.api.rest import Dashboard

    assert Dashboard(None).web_dir == REAL_WEB  # type: ignore[arg-type]


# --- the sandbox's folder and port --------------------------------------------------------------------------


def test_the_sandbox_is_made_in_a_temporary_folder_and_gone_when_it_ends(run, yours):
    result = invoke()
    assert result.exit_code == 0, result.output
    [call] = run.calls
    assert os.path.samefile(call.root.parent, yours.temp) and call.root.name.startswith("latet-tutorial-")
    assert call.made and call.pid == str(os.getpid())  # it says whose it is, for the sweep of what a cut-off run leaves
    assert not call.root.exists() and list(yours.temp.iterdir()) == []
    assert result.output.rstrip().splitlines()[-1] == "Tutorial ended"


def test_keep_leaves_the_folder_and_says_where_it_is(run, yours):
    result = invoke("--keep")
    [call] = run.calls
    assert result.exit_code == 0 and call.root.is_dir() and str(call.root) in result.output
    assert not (call.root / "sandbox.pid").exists()  # kept on purpose: not a leftover
    assert invoke().exit_code == 0 and call.root.is_dir()  # the next run's sweep leaves it alone


def test_the_port_is_one_the_computer_gave_and_held_until_the_server_takes_it(run, yours, monkeypatch):
    from latet.service import daemon, ports

    monkeypatch.setattr(ports, "choose", lambda *a, **k: pytest.fail("a port chosen from the daemon's"))
    for name in ("start", "stop", "status"):
        monkeypatch.setattr(daemon, name, lambda *a, **k: pytest.fail("your daemon was asked"))
    assert invoke().exit_code == 0
    [call] = run.calls
    assert call.bound == ("127.0.0.1", call.port) and call.port not in (0, 5283)
    assert call.held  # while the sandbox runs, no other program can bind that port
    assert call.sock.fileno() == -1  # and the socket is closed when it ends


def test_the_sandbox_runs_in_an_environment_of_its_own_and_yours_comes_back(run, yours):
    before = variables()
    assert before["LATET_ROOT_KEY"] == "your-root-key"
    assert invoke().exit_code == 0
    [call] = run.calls
    assert call.env == {
        "LATET_DATA_DIR": str(call.root / "data"),
        "LATET_CONFIG": str(call.root / "config.yaml"),
        "LATET_PORT": str(call.port),
        "LATET_KEYRING": "off",
        "LATET_ROOT_KEY": None,
        "LATET_ROOT_KEY_FILE": None,
        "LATET_BRIDGE_TOKEN": None,
        "LATET_HOST": None,
        "LATET_LLM_API_KEY": None,
    }
    assert variables() == before  # yours back, and the ones that were not set not set


# --- how it ends --------------------------------------------------------------------------------------------


def test_ctrl_c_ends_it_and_cleans_up(run, yours):
    before = variables()

    def interrupted(call):
        raise KeyboardInterrupt

    run.then = interrupted
    result = invoke()
    assert result.exit_code == 0, result.output
    assert result.output.rstrip().splitlines()[-1] == "Tutorial ended"
    assert not run.calls[0].root.exists() and list(yours.temp.iterdir()) == []
    assert variables() == before and run.calls[0].sock.fileno() == -1


def test_a_failure_cleans_up_too_and_is_not_called_the_end(run, yours):
    before = variables()

    def fails(call):
        raise RuntimeError("the sandbox broke")

    run.then = fails
    result = invoke()
    assert result.exit_code == 1 and isinstance(result.exception, RuntimeError)
    assert "Tutorial ended" not in result.output
    assert not run.calls[0].root.exists() and variables() == before and run.calls[0].sock.fileno() == -1  # no log


@pytest.mark.parametrize(
    "ending", [SystemExit(3), RuntimeError("the sandbox broke")], ids=["the server could not start", "an error"]
)
def test_a_run_that_stops_on_an_error_prints_what_its_log_says_and_keeps_it(run, yours, ending):
    """uvicorn logs why and exits (3) when the server cannot start, and an error ends a run: the last lines of the log
    are printed, and the folder is kept for it, until a later run deletes it (its sandbox.pid names this command)."""

    def fails(call):
        (call.root / "tutorial.log").write_text("".join(f"log line {n}\n" for n in range(1, 31)), "utf-8")
        raise ending

    run.then = fails
    result = invoke()
    root = run.calls[0].root
    assert result.exit_code == (3 if isinstance(ending, SystemExit) else 1), result.output
    assert "Tutorial ended" not in result.output and "stopped on an error" in result.output
    printed = [line.strip() for line in result.output.splitlines()]
    assert [n for n in range(1, 31) if f"log line {n}" in printed] == list(range(11, 31))  # its last 20 lines
    assert str(root / "tutorial.log") in result.output and (root / "tutorial.log").is_file()
    assert (root / "sandbox.pid").read_text("utf-8") == str(os.getpid())


def test_a_sandbox_that_is_refused_is_said_and_cleaned_up(run, yours):
    def refused(call):
        raise SandboxError("/somewhere is in, around or as your own Latet folder: not a sandbox")

    run.then = refused
    result = invoke()
    assert result.exit_code == 1 and "not a sandbox" in result.output and "Tutorial ended" not in result.output
    assert not run.calls[0].root.exists()


def test_a_folder_a_program_holds_a_moment_is_deleted_at_the_next_try(run, yours, monkeypatch):
    real, failed = shutil.rmtree, []

    def rmtree(path, *args, **kwargs):
        if len(failed) < 2:
            failed.append(path)
            raise PermissionError(13, "The process cannot access the file because it is being used")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = invoke()
    assert result.exit_code == 0 and len(failed) == 2
    assert not run.calls[0].root.exists() and "Could not delete" not in result.output


def test_a_folder_that_cannot_be_deleted_is_named(run, yours, monkeypatch):
    def rmtree(path, *args, **kwargs):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = invoke()
    root = run.calls[0].root
    assert result.exit_code == 0 and f"Could not delete {root}" in result.output
    assert root.exists()  # and the run that left it is gone, so the next one's sweep will delete it
    assert (root / "sandbox.pid").read_text("utf-8") == str(os.getpid())


# --- the link ------------------------------------------------------------------------------------------------


def test_the_link_is_printed_with_print_url_and_opened_in_the_browser_otherwise(run, yours, monkeypatch):
    link = "http://127.0.0.1:1/_latet/ui/login?code=one-time"
    run.then = lambda call: call.announce(link)
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    printed = invoke("--print-url")
    banner = f"Tutorial at http://127.0.0.1:{run.calls[0].port} — Ctrl+C ends it"
    assert opened == [] and link in printed.output and banner in printed.output
    assert "works once" in printed.output
    assert printed.output.index(link) < printed.output.index(banner)

    opening = invoke()
    banner = f"Tutorial at http://127.0.0.1:{run.calls[1].port} — Ctrl+C ends it"
    assert opened == [link] and link not in opening.output and banner in opening.output

    monkeypatch.setattr(webbrowser, "open", lambda url: False)  # no browser to open: the link is printed instead
    assert link in invoke().output


def test_a_browser_that_fails_is_no_failure_of_the_tutorial(run, yours, monkeypatch):
    def broken(url):
        raise webbrowser.Error("no runnable browser")

    link = "http://127.0.0.1:1/_latet/ui/login?code=one-time"
    run.then = lambda call: call.announce(link)
    monkeypatch.setattr(webbrowser, "open", broken)
    result = invoke()
    assert result.exit_code == 0 and link in result.output


# --- your own folders ----------------------------------------------------------------------------------------


def test_your_data_folder_is_left_as_it_is_but_for_the_chapters_you_finished(run, yours):
    run.then = lambda call: call.progress.mark("basics")  # the sandbox's session does this at the end of a chapter
    yours_before = tree(yours.data.parent)
    assert invoke().exit_code == 0
    assert run.calls[0].progress.path == yours.data / "tutorial.json"  # the path of yours, taken before the switch
    after = tree(yours.data.parent)
    assert json.loads(after.pop("data/tutorial.json")) == {"done": ["basics"]}  # chapter ids, nothing else
    assert after == yours_before  # the vault, the log, the settings: as they were


def test_the_sandbox_is_never_made_where_yours_is(run, yours):
    assert invoke().exit_code == 0
    root = run.calls[0].root
    assert not root.is_relative_to(yours.data.parent) and not yours.data.is_relative_to(root)


def test_the_sandbox_is_given_a_finder_of_your_text_recognition_models_taken_in_your_data_folder(
    run, yours, monkeypatch
):
    """The finder is made where `data_dir()` is still yours (before the sandbox's folders replace it), and it is the
    one the sandbox is given."""
    made = []

    def finder():
        return None

    def user_reader(folder):
        made.append((folder, os.environ["LATET_DATA_DIR"]))
        return finder

    monkeypatch.setattr(sandbox, "user_reader", user_reader)
    assert invoke().exit_code == 0
    [call] = run.calls
    assert call.reader is finder
    assert made == [(yours.data, str(yours.data))]


# --- the sandboxes a cut-off run left ------------------------------------------------------------------------


def leftover(temp: Path, name: str, pid: object = None) -> Path:
    folder = temp / name
    folder.mkdir()
    (folder / "latet.db").write_bytes(b"a vault")
    if pid is not None:
        (folder / "sandbox.pid").write_text(str(pid), "utf-8")
    return folder


def test_the_sandboxes_a_cut_off_run_left_are_deleted_and_nothing_else_is(run, yours):
    stale = leftover(yours.temp, "latet-tutorial-stale", an_ended_process())
    running = leftover(yours.temp, "latet-tutorial-running", os.getpid())  # another run, still there
    kept = leftover(yours.temp, "latet-tutorial-kept")  # --keep: no pid
    odd = leftover(yours.temp, "latet-tutorial-odd", "not a number")
    big = leftover(yours.temp, "latet-tutorial-big", 2**32 + an_ended_process())  # no process has that number
    other = leftover(yours.temp, "something-else", an_ended_process())  # not a sandbox of the tutorial
    (yours.temp / "latet-tutorial-file").write_text("a file", "utf-8")
    assert invoke().exit_code == 0
    assert not stale.exists()
    assert running.is_dir() and kept.is_dir() and odd.is_dir() and big.is_dir() and other.is_dir()
    assert (yours.temp / "latet-tutorial-file").is_file()


def test_a_sandbox_that_cannot_be_deleted_is_left_for_the_next_time_without_a_word(run, yours, monkeypatch):
    stale = leftover(yours.temp, "latet-tutorial-stale", an_ended_process())
    real = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == stale:
            if kwargs.get("ignore_errors"):
                return
            raise PermissionError(13, "in use")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    result = invoke()
    assert result.exit_code == 0 and stale.is_dir() and "Could not delete" not in result.output


def test_the_log_has_uvicorn_s_own_errors_whoever_set_its_loggers_up(tmp_path, monkeypatch):
    """uvicorn's default setup (any server run before in this process with it) keeps its messages from the root
    logger: the log of the run still gets them, as why the server could not start is one of them."""
    uvicorn_log = logging.getLogger("uvicorn")
    monkeypatch.setattr(uvicorn_log, "propagate", False)
    monkeypatch.setattr(uvicorn_log, "handlers", [])
    log = tmp_path / "tutorial.log"
    with command._logging_to(log):
        logging.getLogger("uvicorn.error").error("Application startup failed. Exiting.")
    assert "Application startup failed" in log.read_text("utf-8")
    assert uvicorn_log.propagate is False  # as it was


def test_a_process_that_runs_is_told_from_one_that_ended():
    assert command.alive(os.getpid())
    assert not command.alive(an_ended_process())


def test_a_number_no_process_can_have_is_taken_for_one_that_runs():
    """Never delete on doubt: a number above 32 bits (a sandbox.pid the command did not write) is not cut down to the
    number of another process, which may have ended."""
    assert command.alive(2**32 + an_ended_process())


# --- the environment -----------------------------------------------------------------------------------------


def test_the_environment_names_what_the_sandbox_names(tmp_path, monkeypatch):
    for name in VARIABLES:
        monkeypatch.setenv(name, f"yours-{name}")
    root = tmp_path / "latet-tutorial-x"
    with command.sandbox_env(root, 6701):
        assert {name: os.environ.get(name) for name in VARIABLES} == {
            **dict.fromkeys(VARIABLES),
            "LATET_DATA_DIR": str(root / "data"),
            "LATET_CONFIG": str(root / "config.yaml"),
            "LATET_PORT": "6701",
            "LATET_KEYRING": "off",
        }
        assert set(environment(root, 6701)) == set(VARIABLES)  # the sandbox's own list, not a second one
    assert variables() == {name: f"yours-{name}" for name in VARIABLES}


def test_the_environment_comes_back_whatever_happens_and_unset_stays_unset(tmp_path, monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LATET_HOST", "127.0.0.1")

    def fails_inside():
        with command.sandbox_env(tmp_path, 6702):
            assert os.environ["LATET_PORT"] == "6702" and "LATET_HOST" not in os.environ
            raise RuntimeError

    with pytest.raises(RuntimeError):
        fails_inside()
    assert variables() == {**dict.fromkeys(VARIABLES), "LATET_HOST": "127.0.0.1"}  # not even an empty string


def test_ctrl_c_while_the_environment_is_switched_puts_yours_back(tmp_path, monkeypatch):
    """Ctrl+C can come between two variables of the switch: the ones switched by then are put back too."""
    for name in VARIABLES:
        monkeypatch.setenv(name, f"yours-{name}")
    real = environment(tmp_path, 6703)

    class Interrupted(dict):
        def items(self):
            yield from list(super().items())[:3]
            raise KeyboardInterrupt  # three of them switched

    monkeypatch.setattr(sandbox, "environment", lambda root, port: Interrupted(real))
    with pytest.raises(KeyboardInterrupt), command.sandbox_env(tmp_path, 6703):
        pytest.fail("the sandbox ran in an environment half switched")
    assert variables() == {name: f"yours-{name}" for name in VARIABLES}
