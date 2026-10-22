"""The command line's calls to this computer's daemon never go through a proxy, whatever HTTP_PROXY and NO_PROXY say:
the bridge token they carry would reach it."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from latet.config import Config, data_dir, save_config

from .conftest import run_cli

DAEMON_ANSWER = {  # what each of the command line's calls reads in this computer's daemon's answer
    "ok": True,
    "version": "test",
    "masking": True,
    "known": [],
    "surrogate": "usr_4f2a",
    "names": ["llm-acme-com"],
    "url": "http://127.0.0.1/_latet/ui/login?code=c",
}


class _Recorder:
    """A server on 127.0.0.1 that answers every request with ``DAEMON_ANSWER`` and keeps the path each asked: this
    computer's daemon, or a proxy standing by (a request sent through a proxy names its whole address)."""

    def __init__(self):
        paths = self.paths = []

        class Answer(BaseHTTPRequestHandler):
            def _answer(self):
                paths.append(self.path)
                self.rfile.read(int(self.headers.get("content-length") or 0))
                body = json.dumps(DAEMON_ANSWER).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Answer)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_the_command_lines_calls_to_this_computers_daemon_never_go_through_a_proxy(computer, monkeypatch):
    from latet import wrappers
    from latet.cli.admin import _admin
    from latet.cli.rules import _known_in_vault
    from latet.doctor.checks import _bridge_ping
    from latet.service import daemon
    from latet.storage.keys import load_bridge_token

    here, proxy = _Recorder(), _Recorder()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, proxy.url)  # a proxy that listens: urllib's own opener would send it all there
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        config = Config(port=here.port)
        save_config(config)
        assert _admin("GET", "/_latet/admin/changes/c") == DAEMON_ANSWER
        assert _known_in_vault(["a value"]) == []
        assert _bridge_ping(here.port, load_bridge_token(data_dir()))
        assert daemon.status(here.port).running
        daemon.stop(timeout=0.1)
        assert wrappers.user_name_surrogate(config) == "usr_4f2a"
        assert wrappers.destination_for("https://llm.acme.com", "claude", config) == "llm-acme-com"
        shown = run_cli("dashboard", "--print-url")
        assert shown.exit_code == 0, shown.output
    finally:
        here.close()
        proxy.close()
    assert proxy.paths == []  # the bridge token never reached it
    asked = {
        "/_latet/admin/changes/c", "/_latet/admin/known", "/_latet/engine/v1/ping", "/_latet/health",
        "/_latet/admin/shutdown", "/_latet/admin/user-name", "/_latet/admin/destinations", "/_latet/admin/login-code",
    }  # fmt: skip
    assert asked <= set(here.paths), set(here.paths)
