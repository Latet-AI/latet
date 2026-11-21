"""Latet servers with the Enterprise Edition, for E3's tests: two instances of one server (E1's `instances`: two
open_server over one PostgreSQL schema and one Valkey prefix of the test's own, each with its own memory, as behind a
load balancer), each served as `latet server run` serves it, one browser in front of them, a license installed as an
administrator installs one (signed by the tests' signer: E1's `licensed`), people and their dashboard sessions. The
tests that use them carry SERVER_TESTS (the marker `server` and E1's `services`) and need LATET_TEST_DATABASE_URL and
LATET_TEST_VALKEY_URL (`uv run python scripts/server-services.py up`)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from starlette.testclient import TestClient

from latet.api.rest import COOKIE
from latet.ee.roles import Role
from latet.server.app import Server
from latet.server.operations import server_app

from ..licenses import TRIAL, Signer, at
from ..licenses import Clock as LicenseClock
from ..servers import URL, Instances, services

HOST = "latet.acme.lan"  # E1's servers' address (tests/ee/servers.py: URL)
API = "/_latet/api/v1"
ADMIN_EE = "/_latet/server/v1/admin/ee"
UI = {"x-latet-ui": "1"}
NOW = at("2027-03-01")  # E1's `clock` fixture's day, within the tests' license (PAYLOAD: 2026-11-01 to 2027-10-31)
BY_HAND = "admin:tests"  # the actor of what a test changes directly
SERVER_TESTS = [pytest.mark.server, services]


class Clock(LicenseClock):
    """E1's clock of the tests (tests/ee/licenses.py), which E3's tests also move by seconds."""

    def __init__(self, now: float = NOW) -> None:
        super().__init__(now)

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Cluster:
    """Two instances of one server, and one browser in front of them: the clients share one cookie jar, as a browser
    behind a load balancer does whichever instance answers."""

    clock: Clock
    signer: Signer
    servers: list[Server]
    clients: list[TestClient]
    admin_token: str

    @property
    def a(self) -> TestClient:
        return self.clients[0]

    @property
    def b(self) -> TestClient:
        return self.clients[1]

    @property
    def server(self) -> Server:
        """Instance A: where a test changes things directly."""
        return self.servers[0]

    @property
    def people(self):
        return self.server.edition.people


def start(instances: Instances, signer: Signer, clock: Clock, count: int = 2) -> Cluster:
    servers = [instances.open() for _ in range(count)]
    clients = [TestClient(server_app(server), base_url=URL, client=("203.0.113.7", 50000)) for server in servers]
    for client in clients:
        client.__enter__()
    for client in clients[1:]:
        client.cookies = clients[0].cookies.jar  # the jar itself: one browser
    cluster = Cluster(clock, signer, servers, clients, servers[0].admin.issue("tests"))
    license(cluster)  # every feature (E1's PAYLOAD), until 2027-10-31
    return cluster


def stop(cluster: Cluster) -> None:
    """The clients' lifespans end (each app's spaces close); E1's `instances` then closes the servers (their edition and
    its listener first), drops the schema and removes the test's keys."""
    for client in cluster.clients:
        client.__exit__(None, None, None)


def license(cluster: Cluster, **changes: Any) -> None:
    """A license installed as an administrator installs it (E1's install_license, by BY_HAND), every instance reading
    it at once: the tests' PAYLOAD with ``changes`` (``features=[...]``, ``expires=...``, E1's TRIAL)."""
    cluster.server.edition.install_license(cluster.signer.make(**changes), actor=BY_HAND)
    for server in cluster.servers:
        server.edition.refresh()


def on_trial(cluster: Cluster) -> None:
    """A trial of every feature (E1's TRIAL), installed on its last day, 2026-12-02, at 18:00 UTC."""
    cluster.clock.now = at("2026-12-02", "18:00:00")
    license(cluster, **TRIAL)


def end_trial(cluster: Cluster) -> None:
    """The trial ends six hours later, the first second after its last day: a session opened on its last day still
    lasts. Every instance weighs the license again at once (E1's refresh), as each would within REFRESH_SECONDS."""
    cluster.clock.now = at("2026-12-03", "00:00:00")
    for server in cluster.servers:
        server.edition.refresh()


def paused(cluster: Cluster) -> None:
    """A paid license past its 30 days of grace: installed before its last day (2027-03-10), the clock then moved to
    2027-04-15. Sessions opened before it are past their twelve hours: a test signs in after it."""
    license(cluster, expires="2027-03-10")
    cluster.clock.now = at("2027-04-15")
    for server in cluster.servers:
        server.edition.refresh()


def settle(done: Callable[[], bool], seconds: float = 5.0) -> None:
    """Waits until ``done()``: what another instance learns through a message (E1's settings, E0b's `identities`), five
    seconds at most."""
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError("the other instance did not learn it in time")
        time.sleep(0.05)


def add_person(cluster: Cluster, name: str, email: str | None, *roles: Role):
    """A person added by hand, with roles given to them by hand."""
    person, _code = cluster.server.identities.add_person(name, email)
    for role in roles:
        cluster.people.grant(person.id, role, actor=BY_HAND)
    return person


def sign_in_as(cluster: Cluster, identity) -> None:
    """The person's dashboard session, as the server opens one: its cookie goes to both instances."""
    cluster.a.cookies.set(COOKIE, cluster.server.sign_ins.open(identity.id), domain=HOST, path="/_latet")


def community_sign_in(cluster: Cluster, identity) -> None:
    """The Community Edition's way into one's own dashboard (R5): a computer connected with a one-time code (`latet
    connect`), then the one-time link `latet dashboard` asks for with that computer's token (E0c's machines'
    `login-code`, bound to the token), opened in the browser, which keeps the session's cookie."""
    identities = cluster.server.identities
    _identity, token, _key = identities.enrol(identities.code(identity.id), "laptop")
    code = cluster.server.sign_ins.issue_code(identity.id, token=identities.secret_digest(token))
    cluster.server.known_codes.keep(code)  # as the machines' call keeps the code it issues
    cluster.a.cookies.clear()
    opened = cluster.a.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
    assert opened.status_code == 303 and "login=" not in opened.headers["location"], opened.headers.get("location")


def as_admin(cluster: Cluster) -> dict[str, str]:
    """The headers `latet server` sends: the server's administration token."""
    return {"x-latet-admin": cluster.admin_token}
