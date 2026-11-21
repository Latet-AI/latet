"""The fixtures of the tests against real providers (README.md): a server of two instances on the test services, its
clock this computer's (the providers' tokens and certificates are dated by theirs), a license of every feature valid
today, and `latet server` run as an administrator runs it, its calls answered by instance A."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from typer.testing import CliRunner

from latet.server.client import _refusal, answer_of

from ..people.server_support import SERVER_TESTS, Clock, license, start, stop

PROVIDER_TESTS = [
    pytest.mark.providers,
    pytest.mark.skipif(
        os.environ.get("LATET_PROVIDER_TESTS") != "1",
        reason="real providers in Docker: LATET_PROVIDER_TESTS=1, the images pulled by hand (tests/ee/providers/README.md)",
    ),
    *SERVER_TESTS,
]
ADMIN_API = "/_latet/server/v1/admin"
# The host Latet is given for a provider on this computer, as an administrator writes it: 127.0.0.1, or localhost with
# LATET_PROVIDER_HOST=localhost. Windows resolves localhost to ::1 first, where Docker's ports, published on 127.0.0.1
# alone, refuse the connection: Latet reaches the provider at 127.0.0.1, the next address its lookup checked.
HOST = os.environ.get("LATET_PROVIDER_HOST", "127.0.0.1")
if HOST not in ("127.0.0.1", "localhost"):
    raise pytest.UsageError("LATET_PROVIDER_HOST is 127.0.0.1 or localhost")


class RealClock(Clock):
    """This computer's clock, which Keycloak's ID tokens and the directory's certificates are checked against."""

    def __call__(self) -> float:
        return time.time()


@pytest.fixture
def clock():
    return RealClock()


@pytest.fixture
def cluster(instances, licensed, clock):
    started = start(instances, licensed, clock)
    today = datetime.now(UTC).date()
    license(started, issued=str(today - timedelta(days=1)), expires=str(today + timedelta(days=300)))
    yield started
    stop(started)


@pytest.fixture
def latet_server(cluster, monkeypatch):
    """`latet server …` as an administrator runs it from their computer, with the administration token: each call
    answered by instance A through its administration API (E0c's ``admin_call``, sent to the in-process instance)."""
    import latet.cli.admin  # registers the commands, Latet Enterprise's too
    import latet.cli.server
    import latet.ee.server_cli
    import latet.ee.sync.cli
    from latet.cli.main import app

    def admin_call(method: str, path: str, body: Any = None, *, query: Any = None, **_options: Any) -> Any:
        where = path.removeprefix(ADMIN_API).lstrip("/")
        answer = cluster.a.request(
            method,
            f"{ADMIN_API}/{where}",
            json=body if body is not None else None,
            params=query,
            headers={"x-latet-admin": cluster.admin_token},
        )
        if not 200 <= answer.status_code < 300:
            raise _refusal(answer)
        return answer_of(answer)

    for module in (latet.cli.server, latet.ee.server_cli, latet.ee.sync.cli):
        monkeypatch.setattr(module, "admin_call", admin_call)
    monkeypatch.setattr("latet.cli.agent.agent_name", lambda strict=False: None)  # run by the administrator
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: False)

    def run(*args: str, input: str | None = None, ok: bool = True):
        done = CliRunner().invoke(app, ["server", *args], input=input)
        if ok:
            assert done.exit_code == 0, (args, done.output, done.exception)
        return done

    return run
