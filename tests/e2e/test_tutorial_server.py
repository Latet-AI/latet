"""``latet tutorial`` with a real server: the sandbox listens on a port of its own on 127.0.0.1 (the command runs in a
thread here, as it runs in a terminal), the one-time link signs a browser in, the page ends it with ``POST end``, the
command then closes the runtime and deletes the folder (kept with ``--keep``, its files closed), the server's warnings
are in a file of the sandbox's, and your own environment and folders are as they were."""

import contextlib
import io
import logging
import re
import shutil
import signal
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer.main
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers the commands
import latet.tutorial.sandbox as sandbox_module
from latet.cli.main import app

pytestmark = pytest.mark.e2e

PATIENCE = 30.0  # seconds the server may take to start or to end
UI = {"x-latet-ui": "1"}
TUTORIAL = "/_latet/api/v1/tutorial"
LINK = re.compile(r"http://127\.0\.0\.1:(\d+)/_latet/ui/login\?code=\S+")
YOUR_TOKEN = "your-bridge-token"


@pytest.fixture
def yours(tmp_path, monkeypatch):
    """Your own Latet (a data folder with a vault in it, your settings, a token and a root key in the environment),
    a temporary folder to make the sandbox in, and no real browser."""
    data, config = tmp_path / "yours" / "data", tmp_path / "yours" / "config.yaml"
    data.mkdir(parents=True)
    config.write_text("profile: max\n", "utf-8")
    (data / "latet.db").write_bytes(b"your vault")
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setenv("LATET_DATA_DIR", str(data))
    monkeypatch.setenv("LATET_CONFIG", str(config))
    monkeypatch.setenv("LATET_BRIDGE_TOKEN", YOUR_TOKEN)
    monkeypatch.setenv("LATET_ROOT_KEY", "your-root-key-is-not-base64")
    for name in ("LATET_PORT", "LATET_CONTAINER", "LATET_KEYRING", "LATET_ROOT_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail(f"a real browser was asked to open {url}"))
    return data, config, temp


class Running:
    """``latet tutorial ARGS`` in a thread: what it prints, and how it ended."""

    def __init__(self, args: list[str]) -> None:
        self.console = io.StringIO()
        self.exit: int | None = None
        self.error: BaseException | None = None
        self.client: httpx.Client | None = None  # a browser signed in, once the test has done it
        self._thread = threading.Thread(target=self._run, args=(args,), daemon=True)

    def _run(self, args: list[str]) -> None:
        try:
            command = typer.main.get_command(app)
            self.exit = command.main(args=["tutorial", *args], prog_name="latet", standalone_mode=False) or 0
        except BaseException as exc:
            self.error = exc

    @property
    def ended(self) -> bool:
        return not self._thread.is_alive()

    def waited_for(self, found: Callable[[], Any], what: str) -> Any:
        """What ``found`` returns once it is not None; a command that ends meanwhile, or is too slow, fails the test."""
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline:
            if (value := found()) is not None:
                return value
            assert not self.ended, f"the command ended before {what}: {self.error!r} {self.console.getvalue()}"
            time.sleep(0.05)
        raise AssertionError(f"waited {PATIENCE:.0f} s for {what}: {self.console.getvalue()}")

    def link(self) -> tuple[str, int]:
        """The sign-in link the command printed, and the port it names."""

        def printed() -> tuple[str, int] | None:
            found = LINK.search(self.console.getvalue())
            return (found.group(0), int(found.group(1))) if found else None

        return self.waited_for(printed, "the sign-in link was printed")

    def browser(self, url: str) -> httpx.Client:
        """A browser: the link followed once, which sets its cookie."""
        port = int(LINK.fullmatch(url).group(1))  # type: ignore[union-attr]
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=PATIENCE)
        signed = client.get(url, follow_redirects=False)
        assert signed.status_code == 303 and signed.headers["location"] == "/_latet/ui/", signed.text
        assert f"latet_tutorial_{port}" in client.cookies  # the sandbox's own cookie name, never the daemon's
        self.client = client
        return client

    def finish(self) -> None:
        self._thread.join(timeout=PATIENCE)


@contextlib.contextmanager
def running(*args: str) -> Iterator[Running]:
    tutorial = Running(list(args))
    with contextlib.redirect_stdout(tutorial.console), contextlib.redirect_stderr(tutorial.console):
        tutorial._thread.start()
        try:
            yield tutorial
        finally:
            if not tutorial.ended and tutorial.client is not None:
                with contextlib.suppress(httpx.HTTPError):
                    tutorial.client.post(f"{TUTORIAL}/end", headers=UI)
            tutorial.finish()
            if tutorial.client is not None:
                tutorial.client.close()


def latet_threads(before: set[threading.Thread], patience: float = 5.0) -> list[str]:
    """The threads of Latet's own (the mediator's and the recorder's workers) that were not there ``before``, and
    are still there after ``patience`` seconds: a worker told to stop ends once it notices, a moment after the
    command returned (as the runtime's own test waits for them)."""
    threads = [t for t in set(threading.enumerate()) - before if t.name.startswith("latet-")]
    deadline = time.monotonic() + patience
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return [t.name for t in threads if t.is_alive()]


def test_the_sandbox_serves_until_the_page_ends_it_and_nothing_is_left(yours, monkeypatch):
    data, config, temp = yours
    before_files = {p: p.read_bytes() for p in data.rglob("*") if p.is_file()}
    before_threads = set(threading.enumerate())
    opened: list[tuple[str, Any]] = []

    def open_browser(url: str) -> bool:
        """The browser: it asks the server at the moment it is opened, which only works once the server runs."""
        health = f"http://127.0.0.1:{LINK.fullmatch(url).group(1)}/_latet/health"  # type: ignore[union-attr]
        try:
            opened.append((url, httpx.get(health, trust_env=False, timeout=PATIENCE).status_code))
        except httpx.HTTPError as exc:
            opened.append((url, exc))
        return True

    monkeypatch.setattr(webbrowser, "open", open_browser)
    with running() as tutorial:
        link, answer = tutorial.waited_for(lambda: opened[0] if opened else None, "the browser was opened")
        assert answer == 200, f"the browser was opened before the server answered: {answer!r}"
        port = int(LINK.fullmatch(link).group(1))  # type: ignore[union-attr]
        [folder] = [p for p in temp.iterdir() if p.name.startswith("latet-tutorial-")]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as stranger:
            health = stranger.get("/_latet/health").json()
            assert health["name"] == "latet" and health["masking"] is True and health["tutorial"] is True
            assert stranger.get("/_latet/api/v1/me").status_code == 401  # the page needs its sign-in
            token = stranger.post("/_latet/admin/login-code", headers={"x-latet-token": YOUR_TOKEN})
            assert token.status_code == 401  # your token is not the sandbox's: it has its own
        browser = tutorial.browser(link)
        again = browser.get(link, follow_redirects=False)  # the link works once
        assert again.status_code == 303 and again.headers["location"].endswith("?login=expired")
        me = browser.get("/_latet/api/v1/me").json()
        assert me["port"] == port and me["tutorial"] == {"current": None, "reviewer": False}
        assert Path(me["data_dir"]).resolve() == (folder / "data").resolve()  # the sandbox's, not yours
        menu = browser.get(f"{TUTORIAL}/chapters").json()
        assert "basics" in [card["id"] for card in menu["chapters"]] and menu["requested"] is None
        assert (folder / "sandbox.pid").is_file()
        # A step: the agent's requests go through the masking, whose worker thread the end must stop.
        assert browser.post(f"{TUTORIAL}/chapters/basics/start", headers=UI).status_code == 200
        state = browser.post(f"{TUTORIAL}/chapters/basics/next", json={"step": 0}, headers=UI).json()["state"]
        deadline = time.monotonic() + PATIENCE
        while state["busy"] and time.monotonic() < deadline:
            time.sleep(0.05)
            state = browser.get(f"{TUTORIAL}/chapters/basics/state").json()["state"]
        assert state["steps"][0]["ran"] and state["error"] is None, state
        assert latet_threads(before_threads, patience=0) != []
        ending = browser.post(f"{TUTORIAL}/end", headers=UI)
        assert ending.status_code == 200 and ending.json() == {"ending": True}
        tutorial.finish()
        assert tutorial.ended, "the server did not stop"
    assert tutorial.error is None and tutorial.exit == 0, tutorial.console.getvalue()
    output = tutorial.console.getvalue()
    assert f"Tutorial at http://127.0.0.1:{port} — Ctrl+C ends it" in output
    assert output.rstrip().splitlines()[-1] == "Tutorial ended"
    assert not folder.exists() and list(temp.iterdir()) == []  # runtime closed first, then the folder deleted
    assert latet_threads(before_threads) == []  # and no worker thread of the mediator left behind
    assert {p: p.read_bytes() for p in data.rglob("*") if p.is_file()} == before_files  # no chapter finished
    assert config.read_text("utf-8") == "profile: max\n"
    with pytest.raises(httpx.TransportError):  # nobody answers on the port any more
        httpx.get(f"http://127.0.0.1:{port}/_latet/health", trust_env=False, timeout=2)


def test_ctrl_c_stops_the_real_server_and_the_command_cleans_up(yours, monkeypatch):
    """The signal a terminal sends, to uvicorn's own handler: the server shuts down, uvicorn raises the signal again, and
    the command ends as it does on the page's end. (Here the command runs in the main thread, as in a terminal.)"""
    _, _, temp = yours
    before_threads = set(threading.enumerate())
    pressed: list[str] = []

    def open_browser(url: str) -> bool:
        """The browser opens once the server answers: that is when Ctrl+C is pressed."""
        pressed.append(url)
        signal.raise_signal(signal.SIGINT)
        return True

    monkeypatch.setattr(webbrowser, "open", open_browser)
    original = signal.signal(signal.SIGINT, signal.default_int_handler)  # what Python has at a terminal
    try:
        result = CliRunner().invoke(app, ["tutorial"])
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C went past the command")
    finally:
        signal.signal(signal.SIGINT, original)
    assert len(pressed) == 1
    assert result.exit_code == 0, result.output
    assert result.output.rstrip().splitlines()[-1] == "Tutorial ended"
    assert list(temp.iterdir()) == []  # the runtime was closed, then the folder deleted
    assert latet_threads(before_threads) == []


def test_a_server_that_cannot_start_says_why(yours, monkeypatch):
    """The application cannot start (here its pretend model cannot be reached): uvicorn logs why and exits (3). What
    the log says is printed, where nothing was, and the folder is kept for the log."""
    from latet.tutorial.model import PretendModel

    _, _, temp = yours

    def unreachable(self: PretendModel) -> httpx.AsyncClient:
        raise RuntimeError("the pretend model cannot be reached")

    monkeypatch.setattr(PretendModel, "client", unreachable)
    result = CliRunner().invoke(app, ["tutorial", "--print-url"])
    assert result.exit_code == 3, result.output
    [folder] = list(temp.iterdir())
    assert str(folder / "tutorial.log") in result.output
    assert "RuntimeError: the pretend model cannot be reached" in result.output
    assert "Application startup failed" in result.output  # uvicorn's words, from the log
    assert "Tutorial ended" not in result.output and "works once" not in result.output  # no link: nothing to open


def test_with_keep_the_folder_stays_closed_and_holds_the_servers_warnings(yours):
    data, _, temp = yours
    handlers = list(logging.getLogger().handlers)
    with running("--print-url", "--keep") as tutorial:
        link, port = tutorial.link()
        browser = tutorial.browser(link)
        assert "works once" in tutorial.console.getvalue()
        logging.getLogger("latet.tutorial.test").warning("a warning of the sandbox's server")
        assert browser.post(f"{TUTORIAL}/end", headers=UI).status_code == 200
        tutorial.finish()
        assert tutorial.ended, "the server did not stop"
    assert tutorial.error is None and tutorial.exit == 0, tutorial.console.getvalue()
    [folder] = list(temp.iterdir())
    assert str(folder) in tutorial.console.getvalue() and "Tutorial ended" in tutorial.console.getvalue()
    assert not (folder / "sandbox.pid").exists()  # kept on purpose: the next run does not sweep it
    assert f"port: {port}" in (folder / "config.yaml").read_text("utf-8")
    assert (folder / "data" / "latet.db").is_file() and (folder / "data" / "exchanges.db").is_file()
    assert "a warning of the sandbox's server" in (folder / "tutorial.log").read_text("utf-8")
    assert logging.getLogger().handlers == handlers  # the log file is let go of
    assert (data / "latet.db").read_bytes() == b"your vault"
    shutil.rmtree(folder)  # every file of it was closed: Windows would refuse otherwise


def test_the_tutorial_looks_for_your_text_recognition_models_in_your_own_data_folder(yours, monkeypatch):
    data, _, _ = yours
    asked: list[Path] = []

    def user_reader(folder: Path):
        asked.append(folder)
        return lambda: None

    monkeypatch.setattr(sandbox_module, "user_reader", user_reader)
    with running("--print-url") as tutorial:
        tutorial.browser(tutorial.link()[0])
    assert tutorial.exit == 0 and asked == [data]
