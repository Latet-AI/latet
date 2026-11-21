"""Servers for E4's tests that need PostgreSQL and Valkey (marker `server`): E3's cluster of two instances of one
licensed server (`tests/ee/people/server_support.py`), the destination `llm` of its shared settings sent to a fake
model, the people, groups and API key of a company, and what an agent or a program sends through an instance."""

from dataclasses import dataclass
from pathlib import Path

from latet.api.rest import COOKIE
from latet.config import Destination
from latet.server.tokens import route_key

from ..people.server_support import BY_HAND, HOST, add_person, settle

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "emily.carter@acme.com"
AUTH = {"authorization": "Bearer sk-test"}
CLAUDE = {
    "user-agent": "claude-cli/2.1.291 (external, cli)",
    "x-claude-code-session-id": "8f2d4b6a-1c3e-4a5f-9b7d-0e2c6a8f4d15",
}
APPS = "/_latet/apps/v1"
NOW_DAY = {"from": "2027-03-01", "to": "2027-03-01"}  # the day of E3's clock: the administration log's entries


@dataclass
class Member:
    """An identity of the server and the secret its agent or program uses: a person's token, or an API key."""

    identity: object
    secret: str

    @property
    def route(self) -> str:
        return f"/u/{route_key(self.secret)}"


def ready(cluster, upstream, *, receiver=None, people: str = "group-only") -> None:
    """The cluster's server set up for a morning: its shared destination `llm` sent to the fake model (the Community's
    shared settings take destinations, never rules: the edition model), how people appear in reports, and the SIEM
    output `hook` when a receiver is given; every instance weighing the license and reading the settings again."""
    server = cluster.server
    shared = server.shared.get()
    server.shared.set(
        shared.model_copy(update={"upstreams": [Destination(name="llm", url=upstream.url)]}), actor=BY_HAND
    )
    store = server.edition.settings_store
    store.set_section("reports", {"people": people}, actor=BY_HAND)
    if receiver is not None:
        store.set_section(
            "siem", {"outputs": [{"name": "hook", "kind": "webhook", "url": receiver.url}]}, actor=BY_HAND
        )
    for each in cluster.servers:
        each.edition.refresh()  # E1's copies dropped; E4's pipeline weighs the license again, its outputs made again
    settle(lambda: all(each.edition.events.pipeline.keeps() for each in cluster.servers))


def member(cluster, name: str, email: str, *roles, group: str | None = None) -> Member:
    """A person added by hand with roles given by hand (E3's `add_person`), in ``group`` when named, and their
    computer's token, as `latet connect` enrols one."""
    person = add_person(cluster, name, email, *roles)
    if group is not None:
        people = cluster.people
        found = next((g for g in people.groups() if g.name == group), None) or people.add_group(group, actor=BY_HAND)
        people.add_member(found.id, person.id, actor=BY_HAND)
    identities = cluster.server.identities
    _identity, token, _key = identities.enrol(identities.code(person.id), "laptop")
    return Member(person, token)


def api_key(cluster, name: str) -> Member:
    identity, key = cluster.server.identities.add_key(name)
    return Member(identity, key)


def a_morning(client, member: Member, root: Path) -> None:
    """Through an instance, as an agent on a person's computer: a conversation that grows turn by turn, one in a
    project, and a request Latet stops."""
    project = root / "billing-api"
    project.mkdir(parents=True, exist_ok=True)
    history: list[dict] = []
    for turn in (f"mail {EMAIL} about the Acme Corp invoice", "and attach the receipt", "thanks"):
        body = {"model": "gpt-4.1-mini", "messages": [*history, {"role": "user", "content": turn}]}
        assert client.post(f"{member.route}/llm/v1/chat/completions", json=body, headers=AUTH).status_code == 200
        history += [{"role": "user", "content": turn}, {"role": "assistant", "content": "ok"}]
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"deploy with {GH_PAT}"}]}
    headers = AUTH | {"x-latet-project": str(project)}
    assert client.post(f"{member.route}/llm/v1/chat/completions", json=body, headers=headers).status_code == 200
    stop = {"model": "claude-sonnet-4-5", "max_tokens": 5, "messages": [{"role": "user", "content": f"key {GH_PAT}"}]}
    assert client.post(f"{member.route}/llm/v1/messages", json=stop, headers=CLAUDE).status_code == 400


def drained(client, server, member: Member):
    """The member's space on this instance, once its recorder has recorded what it had."""
    runtime = client.portal.call(server.spaces.runtime, server.identities.get(member.identity.id))
    assert runtime.recorder.drain()
    return runtime


def company_session(cluster, identity, via: str) -> None:
    """The dashboard session of ``identity`` opened as ``via`` says (E3's decision (c)): `latet dashboard`'s one-time
    code (``code``) or the company's single sign-on (``sso``)."""
    cluster.a.cookies.set(COOKIE, cluster.server.sign_ins.open(identity.id, via=via), domain=HOST, path="/_latet")


def quiet(cluster) -> None:
    """Every instance's jobs stopped (the SIEM's delivery, the administration log's forwarding, the license's look, the
    retention), its pipeline still writing: for a test that sets an output's queue or state by hand, or drives a job
    itself, with nothing of E4 changing them meanwhile."""
    for server in cluster.servers:
        server.edition.events.jobs.close()
