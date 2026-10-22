"""What the unit tests of a Latet server share: the address, the shared settings, Emily, an identity's settings over
shared ones kept in memory (what PgSettings keeps in PostgreSQL), and Emily's space as a server makes it, its app with
her requests handed on as the server hands them."""

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
