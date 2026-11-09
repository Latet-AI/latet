"""What the unit tests of a Latet server share: the address, the shared settings, Emily, an identity's settings over
shared ones kept in memory (what PgSettings keeps in PostgreSQL), and Emily's space as a server makes it, its app with
her requests handed on as the server hands them."""

import socket
from dataclasses import replace

import pytest

from latet.config import Config, Destination, IdentityConfig, LlmReview, ProjectEntry, TermConfig
from latet.gateway.app import OnServer
from latet.runtime import make_runtime
from latet.server import egress
from latet.server.shared import OwnSettings
from latet.state import IdentityRef, local_backends

from ...conftest import ROOT_KEY

URL = "https://latet.acme.lan"
EMILY = IdentityRef("u_emilycarte", "Emily Carter", "person")
SHARED = Config(
    terms=[TermConfig(value="Project Falcon", type="PROJECT")],
    allow_values=["support@acme.com"],
    upstreams=[Destination(name="llm", url="https://llm.acme.com")],
    llm_review=LlmReview(url="https://gpu.acme.lan/v1", remote=True, model="qwen"),
    identity=IdentityConfig(usernames=["latet-admin"]),  # the server machine's: never an identity's
    projects=[ProjectEntry(path="/srv/latet")],
)


@pytest.fixture(autouse=True)
def _egress(monkeypatch):
    """Each test its own lookups under way, and no test resolves ipv4only.arpa (the system's resolver is not stopped
    by pytest-socket): the NAT64 prefixes count as discovered, none found. A test of the discovery gives its own."""
    monkeypatch.setattr(egress, "_LOOKUPS", egress._Lookups())
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64(()))


class MemorySettings(OwnSettings):
    """An identity's settings over ``shared``, kept in memory."""

    def __init__(self, shared: Config) -> None:
        super().__init__(lambda: shared)
        self.own: dict = {}
        self.approved: Config | None = None

    def _own(self):
        return self.own

    def _keep_own(self, own):
        self.own = own

    def reviewed(self):
        return self.approved

    def save_reviewed(self, config):
        self.approved = config


def refused_on_v6(start):
    """A server ``start()`` opens on 127.0.0.1, and beside it a socket bound to ``[::1]`` at the same port that does not
    listen: a connection there is refused, as a provider in Docker that listens on 127.0.0.1 alone refuses Windows's
    ``localhost``, whose first address is ``::1``. The socket is None on a computer without IPv6's loopback, where a
    connection to ``::1`` fails all the same."""
    for _ in range(20):
        server = start()
        try:
            held = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:  # no IPv6 here at all
            return server, None
        try:
            held.bind(("::1", server.port))
        except OSError:  # that port is another program's on ::1, or ::1 is not this computer's: another port
            held.close()
            server.close()
            continue
        return server, held
    return start(), None  # ::1 is not this computer's address: nothing answers there


def as_identity(app, identity_id):
    """``app``, its requests handed on by the server for ``identity_id``."""

    async def handed(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "latet.identity": identity_id}
        await app(scope, receive, send)

    return handed


@pytest.fixture
def space(tmp_path):
    """Emily's space as a server makes it, over this computer's backends but for her settings (the shared ones under
    hers, in memory); and the store of her settings."""
    settings = MemorySettings(SHARED)
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY)
    rt.gateway.on_server = OnServer(URL, EMILY.id)
    yield rt, settings
    rt.close()
