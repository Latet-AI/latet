"""D06: a real dashboard mutation flood must not occupy space-build threads.

The space app receives authenticated gate output directly so all forty handlers
reach the save turn; the outer gate's sixteen-request cap has separate witnesses.
"""

import asyncio
import threading

import httpx
import pytest

from latet.api.rest import API, Dashboard
from latet.config import TermConfig

from .support import URL, listening, until

pytestmark = pytest.mark.server


async def test_forty_real_deletes_wait_for_own_save_without_holding_a_colleagues_build(server, monkeypatch):
    emily, _ = server.identities.add_person("Emily Carter")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    handlers, attempted = [], []
    all_handlers = asyncio.Event()
    save_entered, release_save = threading.Event(), threading.Event()
    first_forget, build_entered = threading.Event(), threading.Event()
    attempts_lock = threading.Lock()
    original_handler = Dashboard.forget

    async def observed_handler(dashboard, request):
        identity = dashboard.rt.identity
        if identity is not None and identity.id == emily.id:
            handlers.append(request.path_params["id"])
            if len(handlers) == 40:
                all_handlers.set()
        return await original_handler(dashboard, request)

    # Installed before the real space app binds its real Dashboard routes.
    monkeypatch.setattr(Dashboard, "forget", observed_handler)
    async with listening(server):
        own_app = await server.spaces.app(emily)
        own = server.spaces._spaces[emily.id].runtime
        assert own.gateway.work is not None
        assert daniel.id not in server.spaces._spaces
        save, forget, build = own.settings.save, own.forget, server.spaces._build

        def held_save(config):
            # Runtime.update_config has acquired its actual RLock before this real save boundary.
            save_entered.set()
            if not release_save.wait(30):
                raise TimeoutError("the test did not release its own settings save")
            return save(config)

        def observed_forget(identifier):
            # The real Runtime.forget starts by acquiring that same RLock; no fake lock/job is used.
            with attempts_lock:
                attempted.append(identifier)
            first_forget.set()
            return forget(identifier)

        def observed_build(identity):
            if identity.id == daniel.id:
                build_entered.set()
            return build(identity)  # The actual PostgreSQL-backed Spaces build still runs.

        monkeypatch.setattr(own.settings, "save", held_save)
        monkeypatch.setattr(own, "forget", observed_forget)
        monkeypatch.setattr(server.spaces, "_build", observed_build)

        def change(current):
            return current.model_copy(update={"terms": [*current.terms, TermConfig(value="D06 protected note")]})

        async def authenticated(scope, receive, send):
            await own_app({**scope, "latet.identity": emily.id}, receive, send)

        saving, building, deletes = None, None, []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=authenticated), base_url=URL, trust_env=False
        ) as client:
            try:
                saving = asyncio.create_task(asyncio.to_thread(own.update_config, change))
                await until(lambda: save_entered.is_set() or saving.done())
                if saving.done():
                    saving.result()
                assert save_entered.is_set() and not saving.done()
                identifiers = [f"d06-absent-{number}" for number in range(40)]
                deletes = [
                    asyncio.create_task(client.delete(f"{API}/vault/{identifier}", headers={"x-latet-ui": "1"}))
                    for identifier in identifiers
                ]
                await asyncio.wait_for(all_handlers.wait(), 5)
                await until(first_forget.is_set)
                assert sorted(handlers) == sorted(identifiers)
                assert all(not request.done() for request in deletes)
                assert not release_save.is_set() and not saving.done()

                building = asyncio.create_task(server.spaces.runtime(daniel))
                await until(lambda: build_entered.is_set() or building.done())
                if building.done():
                    building.result()
                assert build_entered.is_set(), "the real colleague build did not enter while the save was held"
                colleague = await asyncio.wait_for(asyncio.shield(building), 5)
                assert server.spaces._spaces[daniel.id].runtime is colleague
                assert colleague.identity is not None and colleague.identity.id == daniel.id
                assert not release_save.is_set() and not saving.done()
                assert all(not request.done() for request in deletes)
                with attempts_lock:
                    assert len(attempted) == 1  # The other thirty-nine are waiting without entering a worker.

                release_save.set()
                changed = await asyncio.wait_for(asyncio.shield(saving), 10)
                assert changed is not None and any(term.value == "D06 protected note" for term in changed.terms)
                answers = await asyncio.wait_for(asyncio.gather(*deletes), 10)
                # Missing IDs still execute Runtime.forget under its real lock and the actual vault lookup.
                assert [answer.status_code for answer in answers] == [404] * 40
                assert all(answer.json()["error"] == "not found" for answer in answers)
                assert sorted(attempted) == sorted(identifiers)
            finally:
                release_save.set()
                tasks = [
                    *deletes,
                    *([saving] if saving is not None else []),
                    *([building] if building is not None else []),
                ]
                drained = asyncio.gather(*tasks, return_exceptions=True)
                try:
                    # Let the released real save and lock waiters finish before fixtures close their stores.
                    await asyncio.wait_for(asyncio.shield(drained), 10)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.wait_for(drained, 10)
