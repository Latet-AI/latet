"""The uvicorn settings `latet serve`, the daemon and the tutorial's sandbox share: one helper makes the server, so
they cannot drift apart. The app is told its server (what `POST end` and the shutdown route stop)."""

import os
import socket
from types import SimpleNamespace

import pytest
import uvicorn
from starlette.applications import Starlette

from latet.cli import main as cli_main
from latet.cli.main import run_server, uvicorn_server
from latet.service import ports


def test_the_server_has_the_gateways_settings():
    application = Starlette()
    server = uvicorn_server(application, "127.0.0.1", 6699, "warning", log_config=None)
    config = server.config
    assert (config.host, config.port, config.log_level) == ("127.0.0.1", 6699, "warning")
    assert config.ws == "websockets-sansio" and config.ws_per_message_deflate is True
    assert config.ws_max_size == 64 * 1024 * 1024
    assert config.timeout_keep_alive == 75 and config.timeout_graceful_shutdown == 3
    assert config.log_config is None  # the caller logs (the daemon to a file, the tutorial to its folder)
    assert application.state.server is server


def test_serve_and_the_daemon_make_their_server_with_it(tmp_path, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free = sock.getsockname()[1]
    sock.close()
    made = []

    def fake(application, host, port, log_level="info", log_config="default"):
        made.append((host, port, log_level, log_config))
        return SimpleNamespace(run=lambda: None)

    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LATET_HOST", raising=False)
    monkeypatch.setattr(cli_main, "uvicorn_server", fake)
    # A server made any other way would run for good: fail instead.
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: pytest.fail("a server of its own was run"))
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, True, "free"))
    run_server(port=free, passthrough=True)  # latet serve
    run_server(port=free, passthrough=True, log_config=None, log_level="warning")  # as the daemon passes its own
    assert made == [("127.0.0.1", free, "info", "default"), ("127.0.0.1", free, "warning", None)]


def test_the_daemon_leaves_a_port_latet_already_runs_on_without_failing(monkeypatch):
    """launchd and systemd start the daemon again when it fails: one that found Latet running there (a daemon another
    start made) was started again every few seconds for good. `latet serve` still fails there."""
    from latet.service import daemon

    monkeypatch.delenv("LATET_HOST", raising=False)
    monkeypatch.setattr(daemon, "configure_file_logging", lambda: None)
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, True, "latet"))
    monkeypatch.setattr(cli_main, "uvicorn_server", lambda *args, **kwargs: pytest.fail("a second server was made"))
    with pytest.raises(SystemExit) as background:
        daemon.main()
    with pytest.raises(SystemExit) as foreground:
        run_server(passthrough=True)
    # Windows starts it once, at login (the Run key), and restarts nothing: there it is as it was.
    assert (background.value.code, foreground.value.code) == (1 if os.name == "nt" else 0, 1)


def test_the_daemon_still_fails_on_a_port_another_program_holds(monkeypatch):
    from latet.service import daemon

    monkeypatch.delenv("LATET_HOST", raising=False)
    monkeypatch.setattr(daemon, "configure_file_logging", lambda: None)
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, False, "in use by another program"))
    with pytest.raises(SystemExit) as background:
        daemon.main()
    assert background.value.code == 1  # started again until the port is free, as before


def test_the_server_takes_a_certificate_for_https(tmp_path):
    server = uvicorn_server(Starlette(), "127.0.0.1", 6699, tls=(tmp_path / "server.crt", tmp_path / "server.key"))
    assert (server.config.ssl_certfile, server.config.ssl_keyfile) == (
        str(tmp_path / "server.crt"), str(tmp_path / "server.key"),
    )  # fmt: skip


def test_the_server_believes_forwarded_headers_from_the_proxys_address_alone(monkeypatch):
    # uvicorn's own variable, which the proxies' addresses of a Latet server are never read from
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    server = uvicorn_server(Starlette(), "127.0.0.1", 6699, forwarded_allow_ips="10.0.5.2")
    assert server.config.proxy_headers and server.config.forwarded_allow_ips == "10.0.5.2"


def test_a_latet_servers_instance_bounds_its_websockets_and_waits_for_its_requests_when_it_stops():
    server = uvicorn_server(
        Starlette(), "0.0.0.0", 5283, None, log_config=None, ws_max_size=8 * 1024 * 1024,
        timeout_graceful_shutdown=30, access_log=False,
    )  # fmt: skip
    config = server.config
    assert config.ws_max_size == 8 * 1024 * 1024  # a message once inflated, as max_request_mb bounds a body
    assert config.timeout_graceful_shutdown == 30  # a rolling restart: what is under way ends first
    assert config.log_level is None and config.access_log is False and config.log_config is None  # the server's log
    assert config.limit_concurrency is None  # a process-wide count would let any client refuse everyone
