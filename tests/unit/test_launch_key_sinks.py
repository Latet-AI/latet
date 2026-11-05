"""Where a launch key, or a value Latet protects, could leave Latet: the launcher and a computer's daemon.

The launchers (`latet claude|codex|opencode|gemini|copilot`, and `latet run`, which is keyless), on a computer and on a
computer connected to a Latet server. The real CLI, wrapper, LaunchSession and run_owned run; only the control
transport (`_launch_call`, `_launch_hold`), the child's `Popen` and the wrapper's own `subprocess` names are inert
recorders. While the child "runs", it is checked that the key is on no command line (the process list), in exactly one
variable of the child's environment (the carrier), in no helper process's arguments or environment, and in no file but
Claude Code's private settings file; after the launch ends, that it is in no file, in nothing the CLI printed, in no log
record at DEBUG and in no control call's path or body. Claude Code's plugin reports a fixed list of route variables
(`claude_plugin/hooks/register.ts`, `routeFacts`), never `ANTHROPIC_CUSTOM_HEADERS`; a TypeScript test is not part of
this module.

A computer's daemon as `latet serve` builds it (`make_runtime`, its own launch registry, Recorder, History, capture
folder, dashboard and request audit), with a MockTransport provider: the key arrives in the header or in the path form
(`/_k/<key>/...`, a computer's alone), for a request the launch's binding decides and for requests refused for their
key (pending, ended, malformed), around the launch controls (OPEN, a refused recovery, END, EXIT). Neither the key nor
the original value reaches the provider, the refusals' answers, a log record at DEBUG or a metrics report; the key
reaches neither History (full or metadata), the capture, nor the dashboard's answers (`/me` with the request audit,
`/exchanges`, each exchange, `/sessions`, `/stats`). The dashboard restores the person's own values in an exchange's
detail by design, so only the key is looked for there."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from latet import claude_bridge, launch_session, wrappers
from latet.cli.main import app
from latet.integrations import jsonc, routes

from .test_agents_connected import URL, _connect
from .test_wrapper_routes import REAL_RUN, launched  # noqa: F401 - fixture

# A canonical base64url encoding of 32 bytes, never an issued credential.
KEY = "latet_lk_" + base64.urlsafe_b64encode(bytes(range(7, 39))).rstrip(b"=").decode("ascii")
LAUNCH_ID = "3" * 32
EPOCH = "4" * 32
BASE = "/_latet/engine/v1/launches"
HEADER = "x-latet-launch-key"
AGENTS = ("claude", "codex", "opencode", "gemini", "copilot")
# The child variable each launcher's carrier puts the key in; None: a fresh variable the agent's own config names.
CARRIER = {
    "claude": "ANTHROPIC_CUSTOM_HEADERS",
    "codex": None,
    "opencode": None,
    "gemini": "GEMINI_CLI_CUSTOM_HEADERS",
    "copilot": "COPILOT_PROVIDER_HEADERS",
}


class _Held:
    """An acknowledged HOLD: the real monitor waits on it until run_owned closes it."""

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self._closed = threading.Event()

    def wait(self) -> None:
        self._closed.wait(30)

    def close(self) -> None:
        self._closed.set()

    def drain(self) -> None:
        pass


class _Controls:
    """The daemon's (or the space's) launch routes, as the wrapper's thin adapters reach them."""

    def __init__(self, folder: str, protected: bool) -> None:
        self.folder, self.protected = folder, protected
        self.calls: list[tuple[str, str, str]] = []
        self.holds: list[tuple[str, int]] = []

    def call(self, method, path, body, **_kwargs):
        self.calls.append((method, path, json.dumps(body)))
        if (method, path) == ("POST", BASE):
            if not self.protected:
                return {"key": KEY, "launch_id": LAUNCH_ID}
            binding = {
                "folder": self.folder,
                "root": self.folder,
                "flavor": body["flavor"],
                "source": "client",
                "project": "a" * 12,
            }
            return {"key": KEY, "launch_id": LAUNCH_ID, "generation": 0, "binding": binding}
        if (method, path) == ("GET", BASE + "/prepare"):
            return {"epoch": EPOCH, "retirement": 0}
        if (method, path) == ("POST", f"{BASE}/{LAUNCH_ID}/exit"):
            return {"retired": True, "epoch": EPOCH, "retirement": 1}
        if (method, path) == ("POST", f"{BASE}/{LAUNCH_ID}/end"):
            return {"ended": True}
        pytest.fail(f"unexpected launch control {method} {path}")

    def hold(self, identifier, generation, **_kwargs):
        self.holds.append((identifier, generation))
        return _Held(generation + 1)


def _logged(records: list[logging.LogRecord]) -> list[str]:
    """What each record says: as its handler wrote it when it was made (``message``: arguments can change after),
    as it reads now, its arguments and its exception's text."""
    return [
        text
        for record in records
        for text in (getattr(record, "message", ""), record.getMessage(), repr(record.args), record.exc_text or "")
    ]


def _holding(root: Path) -> list[Path]:
    """Every file under ``root`` whose bytes hold the key."""
    found = []
    for path in sorted(root.rglob("*")):
        try:
            if path.is_file() and KEY.encode("ascii") in path.read_bytes():
                found.append(path)
        except OSError:
            continue
    return found


def _agent_setup(agent: str, tmp_path: Path, monkeypatch) -> list[str]:
    """The agent's own settings, chosen so that its carrier is qualified: the CLI arguments that start it."""
    if agent == "codex":
        (tmp_path / "codex" / "config.toml").write_text(
            'model_provider = "acme"\n[model_providers.acme]\n'
            'base_url = "https://llm.acme.example/v1"\nwire_api = "responses"\n'
            'http_headers = { "X-Company" = "kept" }\n',
            "utf-8",
        )
        monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 162, 0))
        return ["codex", "exec", "hello"]
    if agent == "opencode":
        monkeypatch.setenv(
            "OPENCODE_CONFIG_CONTENT",
            '{"model": "openai/gpt-4.1", "provider": {"openai": {"options": {"headers": {"X-Company": "kept"}}}}}',
        )
        monkeypatch.delenv("OPENCODE_AUTH_CONTENT", raising=False)
        monkeypatch.setattr(wrappers, "_opencode_sources", lambda: ([], [tmp_path / "absent-policy.json"], []))
    elif agent == "gemini":
        for name in ("GOOGLE_GENAI_USE_GCA", "GOOGLE_GENAI_USE_VERTEXAI", "GEMINI_DEFAULT_AUTH_TYPE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("GEMINI_CLI_AUTH_OVERRIDE", raising=False)
        home = tmp_path / "gemini-home"
        (home / ".gemini").mkdir(parents=True)
        (home / ".gemini" / "settings.json").write_text(
            '{"security":{"auth":{"selectedType":"gemini-api-key"}}}', "utf-8"
        )
        monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
        monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(tmp_path / "system" / "settings.json"))
        monkeypatch.setenv("GEMINI_CLI_SYSTEM_DEFAULTS_PATH", str(tmp_path / "system" / "system-defaults.json"))
        monkeypatch.setenv("GEMINI_CLI_CUSTOM_HEADERS", "X-Company: kept")
    elif agent == "copilot":
        for name in ("COPILOT_PROVIDER_WIRE_API", "COPILOT_PROVIDER_TRANSPORT"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("COPILOT_PROVIDER_TYPE", "openai")
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://api.openai.com/v1")
        monkeypatch.setenv("COPILOT_PROVIDER_HEADERS", "X-Company: kept")
    return [agent, "-p", "hello"]


def _carrier_of(agent: str, env: dict[str, str], argv: list[str], tmp_path: Path) -> str | None:
    """The variable the agent reads its launch header from, as its own config names it."""
    if CARRIER[agent] is not None:
        return CARRIER[agent]
    if agent == "opencode":
        headers = jsonc.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]["openai"]["options"]["headers"]
        found = re.fullmatch(r"\{env:([^{}]+)\}", headers.get(HEADER, ""))
        return found[1] if found else None
    profile = argv[argv.index("--profile") + 1]
    private = tomllib.loads((tmp_path / "codex" / f"{profile}.config.toml").read_text("utf-8"))
    return private["model_providers"]["acme"]["env_http_headers"].get(HEADER)


@pytest.fixture(params=["computer", "connected"])
def machine(request, computer, launched, monkeypatch):  # noqa: F811
    """This computer's Latet (the `launched` folders), or the same computer connected to a Latet server: the
    launcher asks the space's launch routes, with the protected registration, and gets a protected answer."""
    _, _, tmp_path = launched
    monkeypatch.setattr(routes, "MANAGED_SETTINGS", (tmp_path / "managed-settings.json",))
    for name in ("NODE_EXTRA_CA_CERTS", "CODEX_CA_CERTIFICATE", "LATET_BRIDGE_TOKEN", "LATET_LAUNCH_ID"):
        monkeypatch.delenv(name, raising=False)
    if request.param == "connected":
        from latet.config import data_dir
        from latet.engine.reviewer import certificate as certificates
        from latet.server.certificate import make_certificate

        crt, _, fingerprint = make_certificate(URL, tmp_path / "tls")
        certificates.save(data_dir(), crt.read_text())
        _connect(URL, fingerprint)
        monkeypatch.setattr(wrappers, "user_name_surrogate", lambda config=None, **_kwargs: None)
        if sys.platform == "win32":
            # Claude Code's helper descriptor is planned and published as it is (before the OPEN, so it cannot hold a
            # key); only the trust check of the installation's folders, not this module's subject, is left out.
            windows = Path(os.environ.get("SYSTEMROOT") or r"C:\Windows")
            installation = (
                Path(sys.executable),
                Path(sys.prefix),
                Path(sys.base_prefix),
                Path(claude_bridge.__file__).parent,
                windows,
            )
            monkeypatch.setattr(claude_bridge, "_installation", lambda: installation)
    project = tmp_path / "launched-project"
    project.mkdir()
    monkeypatch.chdir(project)
    return request.param, tmp_path, str(project.resolve(strict=True))


@pytest.mark.parametrize("agent", AGENTS)
def test_a_launcher_puts_the_key_in_its_carrier_alone(machine, agent, monkeypatch, caplog):
    kind, tmp_path, folder = machine
    if kind == "connected" and agent == "claude" and sys.platform != "win32":
        pytest.skip("Claude Code goes through a Latet server from Windows only (claude_bridge.platform_problem)")
    caplog.set_level(logging.DEBUG)
    arguments = _agent_setup(agent, tmp_path, monkeypatch)
    controls = _Controls(folder, protected=kind == "connected")
    children: list[dict] = []
    helpers: list[tuple[list[str], dict[str, str]]] = []

    class Child:
        """run_owned's actual Popen call, seen as the operating system would see it."""

        def __init__(self, argv, *, env):
            self.argv, self.env, self.code = list(argv), dict(env), None

        def poll(self):
            return self.code

        def wait(self):
            if self.code is None:
                carrier = _carrier_of(agent, self.env, self.argv, tmp_path)
                children.append(
                    {
                        "argv": self.argv,
                        "holders": sorted(name for name, value in self.env.items() if KEY in value),
                        "carrier": carrier,
                        "value": self.env.get(carrier or "", ""),
                        "files": _holding(tmp_path),
                        "settings": self.argv[self.argv.index("--settings") + 1] if "--settings" in self.argv else None,
                        "held": list(controls.holds),
                    }
                )
                self.code = 0
            return self.code

        def kill(self):
            self.code = -9

    def helper(argv, *args, **kwargs):
        helpers.append((list(argv), dict(kwargs.get("env") or {})))
        return SimpleNamespace(stdout="", stderr="", returncode=1)

    def keyless(argv, env):
        pytest.fail("a qualified carrier must start its agent through run_owned")

    monkeypatch.setattr(wrappers, "run", REAL_RUN)
    monkeypatch.setattr(wrappers, "_launch_call", controls.call)
    monkeypatch.setattr(wrappers, "_launch_hold", controls.hold)
    monkeypatch.setattr(
        wrappers, "subprocess", SimpleNamespace(call=keyless, run=helper, SubprocessError=subprocess.SubprocessError)
    )
    monkeypatch.setattr(launch_session, "subprocess", SimpleNamespace(Popen=Child))

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0, result.output
    [child] = children
    checks = {
        "OPEN, then the acknowledged HOLD, before the child": child["held"] == [(LAUNCH_ID, 0)],
        "no key on the child's command line": KEY not in json.dumps(child["argv"]),
        "the key in one variable of the child's environment, its carrier": child["holders"] == [child["carrier"]],
        "the carrier holds the launch header once": child["carrier"] is not None
        and (child["value"] == KEY or child["value"].count(f"{HEADER}: {KEY}") == 1),
        "while it runs, only Claude Code's private settings file holds it": child["files"]
        == ([Path(child["settings"])] if agent == "claude" else []),
        "no helper process given the key": all(
            KEY not in json.dumps(argv) and KEY not in json.dumps(env) for argv, env in helpers
        ),
        "no file holds it after the launch": _holding(tmp_path) == [],
        "nothing the CLI printed holds it": KEY not in result.output,
        "no log record at DEBUG holds it": all(KEY not in text for text in _logged(caplog.records)),
        "no control call's path or body holds it": all(
            KEY not in path and KEY not in body for _, path, body in controls.calls
        ),
        "one OPEN, retired by EXIT, no recovery": [path for _, path, _ in controls.calls]
        == [BASE, BASE + "/prepare", f"{BASE}/{LAUNCH_ID}/exit"],
    }
    assert all(checks.values()), {name: passed for name, passed in checks.items() if not passed}


def test_latet_run_starts_keyless_on_either_machine(machine, monkeypatch, caplog):
    _kind, tmp_path, _folder = machine
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", f"X-Company: kept\n{HEADER}: {KEY}")
    monkeypatch.setenv("COPILOT_PROVIDER_HEADERS", f"{HEADER}: {KEY}")
    started: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(wrappers, "run", REAL_RUN)
    monkeypatch.setattr(wrappers, "_launch_call", lambda *_a, **_k: pytest.fail("latet run opens no launch"))
    monkeypatch.setattr(
        wrappers,
        "subprocess",
        SimpleNamespace(
            call=lambda argv, env: started.append((list(argv), dict(env))) or 0,
            run=lambda *_a, **_k: pytest.fail("no helper process"),
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    result = CliRunner().invoke(app, ["run", "ordinary-tool", "--ordinary"])
    assert result.exit_code == 0 and "loosenings do not apply" in result.output
    [(argv, env)] = started
    assert argv[1:] == ["--ordinary"] and "LATET_LAUNCH_ID" not in env
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Company: kept" and env["COPILOT_PROVIDER_HEADERS"] == ""
    assert all(KEY not in value for value in env.values()) and KEY not in result.output
    assert _holding(tmp_path) == []
    assert all(KEY not in text for text in _logged(caplog.records))


# --- a computer's daemon ---------------------------------------------------------------------------------------------

ORIGINAL = "ghp_" + "Z8mK2pQ7rT4vW1xY6bN3cD9fG5hJ0kL2sA7e"  # a GitHub token shape: the secrets profile masks it
ASKED = f"ordinary launched request with token {ORIGINAL}"
REPLY = b'{"choices":[{"index":0,"message":{"role":"assistant","content":"ordinary provider reply"}}]}'
SESSION = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"  # a Claude Code session id, within its UUID grammar
CHAT = "/openai/v1/chat/completions"


class _Metrics:
    """Every metrics report, as a server's Metrics hears it (``observe.listen``)."""

    def __init__(self) -> None:
        self.heard: list[tuple[str, tuple]] = []

    def masked(self, found):
        self.heard.append(("masked", (found,)))

    def exchange(self, agent, stopped):
        self.heard.append(("exchange", (agent, stopped)))

    def waiting(self, delta):
        self.heard.append(("waiting", (delta,)))

    def apps(self, operation, texts, values):
        self.heard.append(("apps", (operation, texts, values)))


async def _answered(response) -> bytes:
    """A dashboard handler's answer as its route sends it (which also ends what the answer holds)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await response({"type": "http", "method": "GET", "headers": []}, receive, send)
    return b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")


def _asked(path_params=None):
    from starlette.requests import Request

    return Request(
        {"type": "http", "method": "GET", "headers": [], "query_string": b"", "path_params": path_params or {}}
    )


@pytest.mark.parametrize("history", ["full", "metadata"])
@pytest.mark.parametrize("form", ["header", "path"])
async def test_a_computers_daemon_keeps_the_launch_key_out_of_every_sink(tmp_path, caplog, form, history):
    import httpx

    from latet import observe
    from latet.config import Config, IdentityConfig, Profile, ProjectEntry
    from latet.gateway.app import create_app
    from latet.runtime import make_runtime

    from ..conftest import ROOT_KEY
    from .test_request_leaks import _Answer
    from .test_request_outcomes import OPENAI

    caplog.set_level(logging.DEBUG)
    project = tmp_path / "launched-project"
    project.mkdir()
    project = project.resolve(strict=True)
    capture = tmp_path / "capture"
    config = Config(
        ner="off",
        identity=IdentityConfig(enabled=False),
        history=history,
        projects=[ProjectEntry(path=str(project), profile=Profile.SECRETS)],
    )
    sent: list[httpx.Request] = []
    answers: list[_Answer] = []
    metrics = _Metrics()
    unlisten = observe.listen(metrics)
    rt = make_runtime(
        config, data=tmp_path / "data", capture=capture, config_file=tmp_path / "config.yaml", root_key=ROOT_KEY
    )

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        entity = rt.built.vault.by_original(ORIGINAL)
        fake = entity.surrogate.encode() if entity is not None else b""
        answer = _Answer(REPLY.replace(b"provider reply", b"provider reply " + fake))
        answers.append(answer)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    answered: dict[str, httpx.Response] = {}
    dashboard: list[bytes] = []
    items: list[dict] = []
    history_text = ""
    key = ""
    held = None
    try:
        rt.gateway.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False)
        # The session id needs only its grammar (request_profiles.ID_HEADERS), no configured row.
        rt.gateway.fixed_routes = [OPENAI]
        owner = rt.launch_keys
        assert owner is not None
        app = create_app(rt.gateway)
        token = {"x-latet-token": rt.gateway.bridge_token or ""}
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": ASKED}]}).encode()

        def carried(presented: str) -> tuple[str, dict[str, str]]:
            headers = {"content-type": "application/json", "x-claude-code-session-id": SESSION}
            if form == "path":
                return f"/_k/{presented}{CHAT}", headers
            return CHAT, {**headers, HEADER: presented}

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:5283", trust_env=False
            ) as client:

                async def ask(name: str, presented: str) -> None:
                    path, headers = carried(presented)
                    answered[name] = await client.post(path, headers=headers, content=body)

                opened = await client.post(BASE, headers=token, json={"folder": str(project)})
                assert opened.status_code == 200
                key, launch_id = opened.json()["key"], opened.json()["launch_id"]
                await ask("pending", key)
                held = owner.reserve_hold(launch_id, "computer", 0)
                assert held.acknowledge() == 1
                await ask("verified", key)
                prepared = (await client.get(BASE + "/prepare", headers=token)).json()
                recovery = {"key": key, "launch_id": launch_id, "folder": str(project), "epoch": "0" * 32}
                answered["recover"] = await client.post(
                    BASE + "/recover", headers=token, json={**recovery, "retirement": 0}
                )
                answered["end"] = await client.post(f"{BASE}/{launch_id}/end", headers=token, json={})
                await ask("ended", key)
                await ask("malformed", key[:-2])
                held.stop()
                await held.drain()
                held.close()
                held = None
                answered["exit"] = await client.post(
                    f"{BASE}/{launch_id}/exit", headers=token, json={"epoch": prepared["epoch"]}
                )
            assert rt.recorder is not None and rt.recorder.drain(10)
            store = rt.exchanges
            assert store is not None
            items = store.list()[0]
            history_text = json.dumps((items, [store.detail(row["id"]) for row in items]))
            board = rt.gateway.dashboard
            dashboard.extend(
                [
                    await _answered(await board.me(_asked())),
                    await _answered(await board.exchanges(_asked())),
                    await _answered(await board.sessions(_asked())),
                    await _answered(await board.stats(_asked())),
                    *[await _answered(await board.exchange(_asked({"id": row["id"]}))) for row in items],
                ]
            )
    finally:
        unlisten()
        if held is not None:
            held.stop()
            await held.drain()
            held.close()
        rt.close()

    raw = "".join(path.read_text("utf-8") for path in sorted(capture.glob("*.jsonl")))
    board_text = b"".join(dashboard).decode("utf-8")
    refused = ("pending", "ended", "malformed")
    told = repr(metrics.heard)
    # The agent's own HTTP client runs in this process here: its httpx lines name the daemon's address (with the path
    # form's key, as the agent sent it) and are the agent's, not Latet's. Latet's own client names the provider's.
    records = [
        record
        for record in caplog.records
        if not (record.name == "httpx" and "http://127.0.0.1:5283/" in record.getMessage())
    ]
    request = sent[0] if len(sent) == 1 else None
    checks = {
        "the launch's binding decides the keyed request": answered["verified"].status_code == 200
        and b"ordinary provider reply " + ORIGINAL.encode() in answered["verified"].content,
        "requests refused for their key": all(
            answered[name].status_code == 400 and answered[name].json() == {"error": "request launch authority refused"}
            for name in refused
        ),
        "controls answer codes": answered["recover"].json() == {"error": "launch-stale"}
        and answered["end"].json() == {"ended": True}
        and answered["exit"].json().get("retired") is True,
        "one provider send, without the key, its path form or the original": request is not None
        and key not in str(request.url)
        and "/_k/" not in str(request.url)
        and all(key not in value for value in request.headers.values())
        and HEADER not in request.headers
        and key.encode() not in request.content
        and ORIGINAL.encode() not in request.content
        and all(answer.closed for answer in answers),
        "no answer holds the key, no refusal the original": all(
            key not in response.text and key not in json.dumps(dict(response.headers)) for response in answered.values()
        )
        and all(ORIGINAL not in answered[name].text for name in (*refused, "recover", "end", "exit")),
        "History holds a row and not the key": bool(items) and key not in history_text and "/_k/" not in history_text,
        "the capture holds the exchange and not the key": "ordinary launched request" in raw
        and key not in raw
        and "/_k/" not in raw,
        "the dashboard's answers hold the rows and not the key": bool(items)
        and all(row["id"] in board_text for row in items)
        and key not in board_text,
        "no log record of Latet's at DEBUG holds the key or the original": any(
            record.name == "httpx" and "http://127.0.0.1:1/" in record.getMessage() for record in records
        )
        and all(secret not in text for text in _logged(records) for secret in (key, ORIGINAL)),
        "metrics heard the masking, not the key or the original": any(name == "masked" for name, _ in metrics.heard)
        and key not in told
        and ORIGINAL not in told,
    }
    assert all(checks.values()), {name: passed for name, passed in checks.items() if not passed}


@pytest.mark.parametrize("case", ["verified", "end-before-frame"])
async def test_a_launched_websocket_leaves_the_key_out_of_an_enabled_capture(tmp_path, monkeypatch, caplog, case):
    """The real local ingress and WebSocket relay (``ws_launch_authority_support``), its capture enabled: the
    handshake's incoming headers are captured, the carrier is not among them, and its key is in no frame or line."""
    from latet.config import Config, Profile, ProjectEntry
    from latet.gateway.launch_keys import Binding, LocalLaunchKeys
    from latet.gateway.local_launch import LocalLaunchDispatch, LocalLaunchIngress
    from latet.projects import Projects

    from .test_response_sink_failures import _sinks
    from .ws_launch_authority_support import exercise

    caplog.set_level(logging.DEBUG)
    directory = tmp_path / "ws-project"
    directory.mkdir()
    directory = directory.resolve()
    capture = tmp_path / "ws-capture"
    owner = LocalLaunchKeys(b"r" * 32)
    hold = None
    key = ""
    try:
        with _sinks(tmp_path) as (mediator, _recorder, _store):
            scopes = Projects(
                Config(ner="off", projects=[ProjectEntry(path=str(directory), profile=Profile.SECRETS)]),
                mediator.engine.vault,
                files=False,
            )
            mediator.engine.scopes = scopes
            project = scopes.resolve(str(directory))
            assert project.scope is not None
            mediator.engine.attach(
                mediator.engine.session("session-id:5f4a3e00-0000-4000-8000-00000000f4a3"), str(directory)
            )
            opened = owner.commit_open(
                owner.begin_open("computer"),
                Binding(str(directory).encode(), project.root.encode(), project.id),
            )
            key = opened["key"]
            hold = owner.reserve_hold(opened["launch_id"], "computer", 0)
            assert hold.acknowledge() == 1

            async def end():
                return owner.end(opened["launch_id"], "computer")

            def app_factory(relay):
                ingress = LocalLaunchIngress(LocalLaunchDispatch(relay), owner)

                async def app(scope, receive, send):
                    scope["headers"] = [*scope["headers"], (HEADER.encode(), key.encode())]
                    await ingress(scope, receive, send)

                return app

            await exercise(case, mediator, monkeypatch, app_factory, end, project.scope, capture_dir=capture)
    finally:
        if hold is not None:
            hold.stop()
            await hold.drain()
            hold.close()
        owner.close()
    lines = [
        json.loads(line) for path in sorted(capture.glob("*.jsonl")) for line in path.read_text("utf-8").splitlines()
    ]
    requested = [line for line in lines if line.get("t") == "request"]
    raw = json.dumps(lines)
    checks = {
        "the handshake's incoming headers are captured, the carrier not among them": len(requested) == 1
        and bool(requested[0]["headers"])
        and all(name.lower() != HEADER for name, _ in requested[0]["headers"]),
        "frames captured when one was relayed": case != "verified" or any(line.get("t") == "ws" for line in lines),
        "the capture holds no key": bool(key) and key not in raw,
        "no log record at DEBUG holds the key": all(key not in text for text in _logged(caplog.records)),
    }
    assert all(checks.values()), {name: passed for name, passed in checks.items() if not passed}
