"""A morning of work through Latet whose edition hands its events to an E4 pipeline, as a server's space does: a
conversation that grows turn by turn (its history sent again each time), one in a project, and a request Latet stops."""

from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace

from latet.config import Config, IdentityConfig, TermConfig
from latet.edition import Community
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "emily.carter@acme.com"
SENT = (EMAIL, GH_PAT, "Acme Corp")  # every value a day sends: what no event, row or report may hold
AUTH = {"authorization": "Bearer sk-test"}
CLAUDE = {
    "user-agent": "claude-cli/2.1.291 (external, cli)",
    "x-claude-code-session-id": "3c1e5a7b-9d2f-4b6e-8a0c-4f7d1e2b9a63",
}
EMILY = SimpleNamespace(id="u_emilycarte", name="Emily Carter", kind="person")


class PipelineEdition:
    """The Community Edition, but for events, which go to ``pipeline``: what a server's Enterprise Edition does."""

    def __init__(self, pipeline) -> None:
        self._community = Community()
        self.pipeline = pipeline

    def __getattr__(self, name):
        return getattr(self._community, name)

    def keeps_events(self) -> bool:
        return self.pipeline.keeps()

    def record(self, event) -> None:
        self.pipeline.put(event)


def runtime(root: Path, upstream, pipeline, history: str = "full"):
    """A runtime for Emily whose events go to ``pipeline``, its model the fake ``upstream``."""
    terms = [TermConfig(value="Acme Corp", type="CLIENT")]
    config = Config(identity=IdentityConfig(enabled=False), history=history, terms=terms)
    rt = make_runtime(config, data=root / "data", config_file=root / "config.yaml", edition=PipelineEdition(pipeline))
    rt.identity = EMILY  # as a server's space is made for its identity
    rt.gateway.upstreams = [
        Upstream("openai", "/openai", upstream.url),
        Upstream("anthropic", "/anthropic", upstream.url),
    ]
    return rt


def a_day(client, root: Path) -> None:
    project = root / "billing-api"
    project.mkdir(parents=True, exist_ok=True)
    history: list[dict] = []
    for turn in (f"mail {EMAIL} about the Acme Corp invoice", "and attach the receipt", "thanks"):
        body = {"model": "gpt-4.1-mini", "messages": [*history, {"role": "user", "content": turn}]}
        assert client.post("/openai/v1/chat/completions", json=body, headers=AUTH).status_code == 200
        history += [{"role": "user", "content": turn}, {"role": "assistant", "content": "ok"}]
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"deploy with {GH_PAT}"}]}
    headers = AUTH | {"x-latet-project": str(project)}
    assert client.post("/openai/v1/chat/completions", json=body, headers=headers).status_code == 200
    stop = {"model": "claude-sonnet-4-5", "max_tokens": 5, "messages": [{"role": "user", "content": f"key {GH_PAT}"}]}
    assert client.post("/anthropic/v1/messages", json=stop, headers=CLAUDE).status_code == 400


def forbidden(rt, root: Path) -> set[str]:
    """What no event, row or report may hold: each value the day sent and the fake the vault gave it (found by the
    value, through the vault's own lookup: never by listing what a vault holds, which a server's does not keep in
    memory, R15), each conversation's key, each request's path and host, the project's folder (texts of four characters
    or more)."""
    out = {str(root / "billing-api")}
    for value in SENT:
        out.add(value)
        entity = rt.built.vault.by_original(value)
        if entity is not None:
            out.add(entity.surrogate)
    for summary in rt.exchanges.summaries_since(0):
        out |= {summary["session"], summary["path"], summary.get("host") or ""}
    return {value for value in out if len(value) >= 4}


def strings(value: object) -> Iterator[str]:
    """Every text in an event or a row, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from strings(item)
