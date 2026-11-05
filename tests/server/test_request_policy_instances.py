"""Request exceptions on two instances of one server (RC-REC-3, RC-REC-6, P2-6): the same committed settings decide a
request alike on either, whether or not the instance heard the settings' announcement."""

from __future__ import annotations

import asyncio
import contextlib
import sys

import httpx
import pytest

from latet.config import Config, Destination, RequestException
from latet.gateway.app import create_app

from .support import listening
from .test_workers import running  # noqa: F401  (the --workers 2 harness)

pytestmark = pytest.mark.server

TEAM = {
    "destination": "llm-acme",
    "profile": "openai.chat.1",
    "reason": "custom-header",
    "components": [{"kind": "header", "name": "x-team"}],
}
BODY = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "an ordinary question"}]}
HEADERS = {"authorization": "Bearer k", "x-team": "data-science"}


def _provider(monkeypatch) -> list[httpx.Request]:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        body = b'{"id":"c","object":"chat.completion","model":"gpt-4.1-mini","choices":[]}'
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(
        "latet.gateway.app.make_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(answer), trust_env=False),
    )
    return sent


def _deaf_to_settings(server, monkeypatch) -> None:
    """``server``'s spaces never hear an identity's settings announced (its message lost on the way)."""
    changed = server.spaces._changed

    async def dropping(space, message, seq=None):
        if message.get("kind") == "settings":
            return
        await changed(space, message, seq)

    monkeypatch.setattr(server.spaces, "_changed", dropping)


@contextlib.asynccontextmanager
async def _asking(server, runtime):
    async def reached(url: str):  # her own destination, judged reachable (no name lookup in a test)
        return url, None, None

    runtime.gateway.pin = reached
    app = create_app(runtime.gateway)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=server.settings.url) as client,
    ):

        async def ask(said: list[str] | None = None) -> int:
            answered = await client.post("/llm-acme/v1/chat/completions", json=BODY, headers=HEADERS)
            if said is not None:
                said.append(answered.text)
            return answered.status_code

        yield ask


async def _approved(runtime, rows: list[RequestException]) -> None:
    change = await runtime.mediator.run(
        runtime.propose, runtime.config.model_copy(update={"request_exceptions": rows}), "command"
    )
    assert change is not None and await runtime.mediator.run(runtime.decide_change, change.id, True) == "approved"


async def test_two_instances_decide_alike_after_an_exception_changes(instances, monkeypatch):
    a, b = instances
    sent = _provider(monkeypatch)
    person, _ = a.identities.add_person("Ada", "ada@example.com")
    async with listening(a, b):
        here, there = await a.spaces.runtime(person), await b.spaces.runtime(person)
        destination = Destination(name="llm-acme", url="https://llm.acme.example")
        await here.mediator.run(here.update_config, lambda c: c.model_copy(update={"upstreams": [destination]}))
        for _ in range(100):  # B hears the destination as it is announced
            if any(d.name == "llm-acme" for d in there.config.upstreams):
                break
            await asyncio.sleep(0.05)
        _deaf_to_settings(b, monkeypatch)  # from now on B never hears her settings' announcements
        async with _asking(a, here) as on_a, _asking(b, there) as on_b:
            assert await on_a() == await on_b() == 400  # no exception yet: refused on both
            await _approved(here, [RequestException.model_validate(TEAM)])  # added on A
            assert await on_a() == 200
            assert await on_b() == 200  # B reads the counts as it is asked: the added exception counts at once
            await here.mediator.run(here.update_config, lambda c: c.model_copy(update={"request_exceptions": []}))
            assert await on_a() == 400
            assert await on_b() == 400  # revoked on A, its announcement lost: B refuses at once all the same
    assert len(sent) == 2


async def test_an_exception_revoked_on_another_instance_while_a_request_waits_for_review_is_refused(
    instances, monkeypatch
):
    """I-1: a request admitted under an exception and held for your answer (the Live Reviewer waits until you answer)
    is judged again as it is sent: the exception revoked on A meanwhile, its announcement lost to B, B reads the counts
    at the send and refuses it, as it refuses the exception's header without the exception."""
    a, b = instances
    sent = _provider(monkeypatch)
    person, _ = a.identities.add_person("Lise", "lise@example.com")
    async with listening(a, b):
        here, there = await a.spaces.runtime(person), await b.spaces.runtime(person)
        destination = Destination(name="llm-acme", url="https://llm.acme.example")
        await here.mediator.run(here.update_config, lambda c: c.model_copy(update={"upstreams": [destination]}))
        await _approved(here, [RequestException.model_validate(TEAM)])
        for _ in range(100):  # B hears the exception as it is announced
            if there.config.request_exceptions:
                break
            await asyncio.sleep(0.05)
        _deaf_to_settings(b, monkeypatch)
        reviewed = there.mediator.live_prepared

        async def answered_after_a_revocation(prepared, *args, **kwargs):
            await here.mediator.run(here.update_config, lambda c: c.model_copy(update={"request_exceptions": []}))
            return await reviewed(prepared, *args, **kwargs)

        monkeypatch.setattr(there.mediator, "live_prepared", answered_after_a_revocation)
        said: list[str] = []
        async with _asking(b, there) as on_b:
            assert await on_b(said) == 400
    assert sent == [] and "x-team" in said[0]  # refused as without the exception: the header it named


async def test_a_shared_restriction_is_seen_with_its_message_dropped(instances, monkeypatch):
    """The shared settings' count (owner '') is read with the identity's: a change of the shared settings on A, its
    announcement lost to B, is in force on B before B decides the next request that may use an exception (this
    instance's copy of the shared settings forgotten, then read again)."""
    a, b = instances
    _provider(monkeypatch)
    person, _ = a.identities.add_person("Grace", "grace@example.com")
    async with listening(a, b):
        here, there = await a.spaces.runtime(person), await b.spaces.runtime(person)
        destination = Destination(name="llm-acme", url="https://llm.acme.example")
        await here.mediator.run(here.update_config, lambda c: c.model_copy(update={"upstreams": [destination]}))
        await _approved(here, [RequestException.model_validate(TEAM)])
        for _ in range(100):
            if there.config.request_exceptions:
                break
            await asyncio.sleep(0.05)
        async with _asking(b, there) as on_b:
            assert await on_b() == 200 and there.config.history != "metadata"
            monkeypatch.setattr(b.spaces, "_enqueue", lambda *args, **kwargs: None)  # no announcement reaches B
            a.shared.set(Config(history="metadata"), actor="server")  # the shared settings changed on A
            assert await on_b() == 200
            assert there.config.history == "metadata"  # read again as it was decided, not at the next beat


async def test_an_instance_without_the_profile_revision_refuses(instances, monkeypatch):
    """RC-REC-6: an exception stamped under a request-profile revision this instance does not have grants nothing here
    (a newer instance's, during a rolling upgrade): refused, never widened."""
    a, b = instances
    sent = _provider(monkeypatch)
    person, _ = a.identities.add_person("Hedy", "hedy@example.com")
    async with listening(a, b):
        here, there = await a.spaces.runtime(person), await b.spaces.runtime(person)
        destination = Destination(name="llm-acme", url="https://llm.acme.example")
        await here.mediator.run(here.update_config, lambda c: c.model_copy(update={"upstreams": [destination]}))
        await _approved(here, [RequestException.model_validate(TEAM)])
        [row] = here.config.request_exceptions
        newer = row.model_copy(update={"revision": "http-envelope-9"})  # a later Latet's stamp
        await here.mediator.run(here.update_config, lambda c: c.model_copy(update={"request_exceptions": [newer]}))
        async with _asking(b, there) as on_b:
            assert await on_b() == 400
    assert sent == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="several workers run on Linux alone (ruling I2)")
def test_two_workers_decide_alike_after_an_exception_is_revoked(running):  # noqa: F811
    """One `--workers 2` smoke (P2-6): an API key's exception approved by the administration's save lets its header
    through on either worker, and once revoked is refused by either, the counts read as each request is decided."""
    from datetime import UTC, datetime, timedelta

    from latet.gateway.request_profiles import REVISION
    from latet.server.master import root_key
    from latet.server.space_settings import PgSettings
    from latet.server.tokens import route_key

    from ..e2e.fake_upstream import FakeUpstream
    from .test_workers import _ready

    _supervisor, url, server = running
    _ready(server, 2)
    upstream = FakeUpstream().start()
    try:
        server.shared.set(Config(upstreams=[Destination(name="llm-acme", url=upstream.url)]), actor="server")
        identity, token = server.identities.add_key("P2-6 workers smoke")
        storage = PgSettings(
            server.db, server.shared, identity.id, root_key(server.db, server.master, identity.id, clock=server.clock),
            kind="key",
        )  # fmt: skip
        stamps = {"id": "x0123456789", "granted": datetime.now(UTC) - timedelta(minutes=1), "revision": REVISION}
        granted = storage.prepare(
            storage.load().model_copy(
                update={"request_exceptions": [RequestException.model_validate({**TEAM, **stamps})]}
            )
        )
        storage.save(granted)
        storage.save_reviewed(granted)
        route = f"{url}/u/{route_key(token)}/llm-acme/v1/chat/completions"

        def statuses() -> set[int]:
            found = set()
            for _ in range(12):  # each on a connection of its own: the kernel spreads them over both workers
                with httpx.Client(trust_env=False, timeout=30) as http:
                    answer = http.post(route, json=BODY, headers={**HEADERS, "connection": "close"})
                found.add(answer.status_code)
            return found

        assert statuses() == {200}
        revoked = storage.prepare(storage.load().model_copy(update={"request_exceptions": []}))
        storage.save(revoked)
        storage.save_reviewed(revoked)
        assert statuses() == {400}
    finally:
        upstream.stop()
