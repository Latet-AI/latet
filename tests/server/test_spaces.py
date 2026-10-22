"""Each identity's space on a Latet server: built once at its first use on an instance and kept there; the same on
every instance (a value masked or forgotten, its own settings or the shared ones changed, applied to the space as it
runs); closed on every instance when the identity is disabled; within a memory budget."""

import asyncio
import base64
import contextlib
import gc
import io
import random
import string
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from latet.api.rest import API
from latet.config import Config, Destination, LlmReview, Profile, ProjectEntry, TermConfig
from latet.engine.known import LET_GO
from latet.engine.pipeline import SPACE_CACHE_BYTES
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, MaskingError, Vault
from latet.gateway.app import OnServer
from latet.gateway.approvals import Batch, Decision
from latet.gateway.sigv4 import AwsCredentials
from latet.projects import masker_weight
from latet.protocols.base import ImageRef
from latet.runtime import digest
from latet.server import queue as queue_module
from latet.server import spaces as spaces_module
from latet.server.app import open_server
from latet.server.db import SchemaMismatch
from latet.server.live import WRITER_CLOSE, ValkeyAwsSessions
from latet.server.master import root_key
from latet.server.sealing import Sealer
from latet.server.settings import ServerSettings
from latet.server.shared import OWN_RULES, SharedSettings
from latet.server.spaces import BUILDS_AT_ONCE, CLOSE_SECONDS, SPACE_BASE, NotListening, SpaceUnavailable, space_size
from latet.server.vault import PgVaultStore
from latet.storage.exchanges import ExchangeRecord
from latet.vision.codec import decode
from latet.vision.masking import ImageWork, image_key

from .sharing import share_rules
from .support import URL, listening, until

pytestmark = pytest.mark.server

CANARY = "emily.carter@acme.com"


def _terms(runtime):
    return [t.value for t in runtime.config.terms]


def _add_term(value):
    return lambda current: current.model_copy(update={"terms": [*current.terms, TermConfig(value=value)]})


def _gateways(*names: str) -> Config:
    """Shared settings that lead agents to one destination of the company's for each of ``names``."""
    return Config(upstreams=[Destination(name=name, url=f"https://{name}.acme.com") for name in names])


def _destinations(runtime):
    return [d.name for d in runtime.config.upstreams]


ADMIN = "admin:a_first00000"


async def _leaves(runtime, text: str) -> str:
    """``text`` as the space would send it in a tool's output."""
    engine = runtime.built.engine
    session = await asyncio.to_thread(engine.session, "cc:leaves")
    return (await asyncio.to_thread(engine.mask_text, text, SlotKind.TOOL_RESULT, session)).text


async def _leaves_in(runtime, folder: str, text: str) -> str:
    """``text`` as the space would send it in a tool's output of a conversation in ``folder``, as its client reports
    the folder."""
    engine = runtime.built.engine

    def masked() -> str:
        session = engine.session(f"cc:{folder}")
        engine.attach(session, folder)
        return engine.mask_text(text, SlotKind.TOOL_RESULT, session).text

    return await asyncio.to_thread(masked)


def test_an_instance_refuses_a_schema_that_is_not_its_own(settings, schema):
    with pytest.raises(SchemaMismatch, match="latet server migrate"):
        open_server(settings, schema=schema)


async def test_a_space_is_built_once_at_its_first_use_and_kept(server):
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime, again = await asyncio.gather(server.spaces.runtime(emily), server.spaces.runtime(emily))
        assert runtime is again and server.spaces.count() == 1  # two requests at once: one space
        assert runtime.identity == emily.ref() and runtime.gateway.on_server == OnServer(URL, emily.id)
        assert await server.spaces.runtime(emily) is runtime


async def test_a_space_reads_through_the_instances_pool_with_its_settings_bounds(server, schema, prefix, monkeypatch):
    """Task 22: a space reads the text of images through its instance's pool of text recognitions, the settings'
    number of readers, wait and budget (the models stubbed installed: their loads fail, and nothing is read here)."""
    from latet.vision import models
    from latet.vision.pool import SpaceReads, shared_pool

    monkeypatch.setattr(models, "runtime_available", lambda: True)
    monkeypatch.setattr(models, "installed", lambda data: True)
    bounds = {"ocr_readers": 1, "ocr_read_seconds": 40.0, "ocr_wait_seconds": 50.0}
    settings = ServerSettings.model_validate({**server.settings.model_dump(exclude_unset=True), **bounds})
    instance = open_server(settings, schema=schema, prefix=prefix, data=server.data)  # an instance of the same server
    try:
        emily, _ = instance.identities.add_person("Emily Carter")
        async with listening(instance):
            vision = (await instance.spaces.runtime(emily)).mediator.vision
            assert vision is not None and vision.space and isinstance(vision.ocr, SpaceReads)
            assert vision.ocr.ocr is shared_pool(*models.folders(server.data), 1)
            assert (vision.ocr.wait, vision.ocr.budget) == (50.0, 40.0)
    finally:
        instance.close()


async def test_an_instances_spaces_keep_its_one_pool_whatever_its_processors_become(
    server, schema, prefix, monkeypatch
):
    """The reading is the instance's from its start: a container's processors lowered while it runs make no second pool
    beside the one its spaces read through (T22.1)."""
    from latet.vision import models
    from latet.vision import pool as pool_module

    monkeypatch.setattr(models, "runtime_available", lambda: True)
    monkeypatch.setattr(models, "installed", lambda data: True)
    monkeypatch.setattr(pool_module, "usable_cpus", lambda: 8)
    instance = open_server(server.settings, schema=schema, prefix=prefix, data=server.data)
    try:
        monkeypatch.setattr(pool_module, "usable_cpus", lambda: 1)
        emily, _ = instance.identities.add_person("Emily Carter")
        async with listening(instance):
            vision = (await instance.spaces.runtime(emily)).mediator.vision
            assert vision is not None and vision.ocr.ocr is pool_module.shared_pool(*models.folders(server.data), 2)
    finally:
        instance.close()


async def test_a_fake_that_shows_its_value_kept_before_is_drawn_again_as_the_space_is_built(server):
    """An identity's vault kept before the vault refused such fakes holds `emily -> emily59`: its space's build gives the
    entry the fake its value gets now, kept in PostgreSQL in place of the old one, and masks the value with it."""
    emily, _ = server.identities.add_person("Emily Carter")
    key = root_key(server.db, server.master, emily.id)
    store = PgVaultStore(server.db, emily.id, key)
    entity_id = Vault(key).entity_id("USERNAME", "emily")
    old = Entity(entity_id, "USERNAME", EntityClass.IDENTITY, "emily", "emily59", 0, time.time(), "identity:username")
    assert store.save_entity(old) is None
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        new = runtime.built.vault.by_original("emily")
        assert new is not None and new.id == old.id and "emily" not in new.surrogate.casefold()
        assert await _leaves(runtime, "ssh emily@build-07") == f"ssh {new.surrogate}@build-07"
    assert store.load_entity(old.id) == new


async def test_a_value_masked_or_forgotten_on_one_instance_is_so_on_the_other(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        entity = here.built.vault.intern("EMAIL", EntityClass.PII, CANARY)
        await until(lambda: there.built.vault.get(entity.id) == entity)
        assert await asyncio.to_thread(here.forget, entity.id) is not None
        await until(lambda: there.built.vault.get(entity.id) is None)


async def test_what_the_model_wrote_known_on_one_instance_is_known_on_the_other(instances):
    """The answer that carried what the model wrote passed through one instance: the request that sends it back, on the
    other, finds it the model's. In that conversation only, and exactly as it was written."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    text = 'TEST_PASSWORD = "hunter2"\n'
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        here.built.verifier.wrote("cc:s-1", [text])
        assert here.built.verifier.written("cc:s-1", text)  # here at once
        await until(lambda: there.built.verifier.written("cc:s-1", text))
        assert not there.built.verifier.written("cc:s-2", text)
        assert not there.built.verifier.written("cc:s-1", text.strip())


async def test_what_the_model_wrote_found_in_an_instances_own_memory_is_kept_alive_for_the_others(instances):
    """A conversation that sends the model's text back through the instance that relayed it finds it in that instance's
    memory: its record for the other instances is kept alive all the same (written again, at most once a minute), so
    that it is still there for them an hour after its last use, and made again if it went meanwhile."""
    from latet.engine.verifier import WRITTEN_KEPT_EVERY
    from latet.server.live import REVERSE_TTL

    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    text = 'TEST_PASSWORD = "hunter2"\n'
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        verifier, now = here.built.verifier, [1000.0]
        verifier.clock = lambda: now[0]
        verifier.wrote("cc:s-1", [text])
        assert verifier.shared is not None
        name = verifier.shared._reverse(verifier._written_key("cc:s-1", text).hex())  # type: ignore[attr-defined]
        client = a.bus.client
        await until(lambda: client.exists(name))
        client.expire(name, 30)
        now[0] += WRITTEN_KEPT_EVERY
        assert verifier.written("cc:s-1", text)  # found here
        await until(lambda: client.ttl(name) > 30)  # kept alive there
        client.delete(name)
        now[0] += WRITTEN_KEPT_EVERY
        assert verifier.written("cc:s-1", text)
        await until(lambda: client.exists(name))  # gone meanwhile: made again
        assert 30 < client.ttl(name) <= REVERSE_TTL
        assert there.built.verifier.written("cc:s-1", text)


async def test_a_conversation_naming_a_value_not_heard_of_yet_loads_it_first(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    a.listening.set()  # no ear (no message is heard), and the instances count as listening: a space is built only then
    b.listening.set()
    here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
    try:
        entity = here.built.vault.intern("EMAIL", EntityClass.PII, CANARY)
        conversations = here.built.engine.conversations
        session = conversations.session("cc:s1")
        session.add({entity.id})
        conversations.update(session)
        assert there.built.vault.get(entity.id) is None
        there.built.engine.conversations.session("cc:s1")
        assert there.built.vault.get(entity.id) == entity  # loaded from PostgreSQL before the conversation uses it
    finally:
        await a.spaces.close()
        await b.spaces.close()


async def test_ones_own_settings_changed_on_one_instance_apply_on_the_other_as_it_runs(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))
        await until(lambda: "Mercury Labs" in _terms(there))
        assert await b.spaces.runtime(emily) is there  # applied to it (Runtime.apply_config), not made again


async def test_a_shared_change_reaches_every_space_on_every_instance(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    async with listening(a, b):
        spaces = [await a.spaces.runtime(emily), await b.spaces.runtime(emily), await b.spaces.runtime(daniel)]
        a.shared.set(_gateways("falcon"), actor="admin:a_first00000")
        for runtime in spaces:
            await until(lambda runtime=runtime: "falcon" in _destinations(runtime))


async def test_disabling_an_identity_closes_its_space_on_every_instance(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    async with listening(a, b):
        await a.spaces.runtime(emily)
        await b.spaces.runtime(emily)
        await b.spaces.runtime(daniel)
        a.identities.disable(emily.id)
        await until(lambda: a.spaces.count() == 0 and b.spaces.count() == 1)


async def test_disabling_or_revoking_ends_the_identitys_sign_ins_and_keys_on_every_instance(instances):
    a, b = instances
    emily, code = a.identities.add_person("Emily Carter")
    _, token, _ = a.identities.enrol(code, "laptop")
    async with listening(a, b):
        sid, other = b.sign_ins.open(emily.id), b.sign_ins.open("u_danielokaf")
        # An AWS session of hers kept through B (listed for her: ended with one read of the list, no scan).
        aws = ValkeyAwsSessions(b.bus, b.master, emily.id)
        session = aws.add_session(AwsCredentials("AKIAEXAMPLE", "secret-of-emily", None))
        a.identities.revoke_secret(token)  # a computer's token revoked: the person's dashboards and sessions end
        await until(lambda: b.sign_ins.subject_of(sid) is None and not aws.has_session(session))
        assert b.sign_ins.subject_of(other) == "u_danielokaf"  # someone else's stay


async def test_a_space_loads_at_most_the_vault_limit_and_keeps_its_count_as_values_arrive(instances):
    a, b = instances
    a.settings = a.settings.model_copy(update={"vault_limit": 1_000})
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        entity = here.built.vault.intern("EMAIL", EntityClass.PII, CANARY)
        await until(lambda: there.built.vault.get(entity.id) == entity)
        # The address and its domain (an address brings its domain along).
        assert (
            a.spaces._spaces[emily.id].parts.vault.limit == 1_000 and b.spaces._spaces[emily.id].parts.vault.count == 2
        )
        await asyncio.to_thread(here.forget, entity.id)
        await until(lambda: there.built.vault.get(entity.id) is None)
        assert b.spaces._spaces[emily.id].parts.vault.count == 1  # its domain stays


def _hold_build(spaces, monkeypatch):
    """``spaces``' next build held once its identity and settings are read: (built, go), two events."""
    built, go = threading.Event(), threading.Event()
    build = spaces._build

    def slow(identity):
        found = build(identity)  # its settings read: a change announced from now on is one it has not seen
        built.set()
        go.wait(10)
        return found

    monkeypatch.setattr(spaces, "_build", slow)
    return built, go


async def test_a_change_announced_while_a_space_is_built_is_applied_once_it_is(instances, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here = await a.spaces.runtime(emily)
        built, go = threading.Event(), threading.Event()
        build = b.spaces._build

        def slow(identity):
            found = build(identity)  # its settings read: a change announced from now on is one it has not seen
            built.set()
            go.wait(10)
            return found

        monkeypatch.setattr(b.spaces, "_build", slow)
        building = asyncio.create_task(b.spaces.runtime(emily))
        await until(built.is_set)
        await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))
        await until(lambda: bool(b.spaces._missed.get(emily.id)))
        go.set()
        assert "Mercury Labs" in _terms(await building)


async def test_over_the_memory_budget_the_space_used_least_recently_is_closed(server):
    emily, _ = server.identities.add_person("Emily Carter")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    server.spaces.budget = SPACE_BASE + SPACE_BASE // 2  # room for one space
    async with listening(server):
        first = await server.spaces.runtime(emily)
        await server.spaces.runtime(daniel)
        assert server.spaces.count() == 1
        assert await server.spaces.runtime(emily) is not first  # made again at its next use


# --- the memory budget, in bytes, weighed as a space grows (the coordinator's ruling, I10, H8, H11, M6) ---------------


async def test_a_space_is_weighed_again_as_its_vault_grows(instances, monkeypatch):
    monkeypatch.setattr(spaces_module, "SPACE_SHARE", 1.0)  # the budget's room for two: neither space past its share
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    async with listening(a, b):
        here = await a.spaces.runtime(emily)
        await b.spaces.runtime(emily)
        await b.spaces.runtime(daniel)
        b.spaces.budget = sum(space_size(space.runtime) for space in b.spaces._spaces.values()) + 4096
        for n in range(5):  # five addresses of one domain: six values, 12 KiB as a space weighs them
            await asyncio.to_thread(here.built.vault.intern, "EMAIL", EntityClass.PII, f"person{n}@acme.com")
        # Each value B learns weighs Emily's space again: past the budget, the space used least recently goes.
        await until(lambda: b.spaces.count() == 1)
        assert emily.id in b.spaces._spaces and daniel.id not in b.spaces._spaces


async def test_a_space_is_weighed_again_at_each_use(server, monkeypatch):
    monkeypatch.setattr(spaces_module, "SPACE_SHARE", 1.0)  # the budget's room for two: neither space past its share
    emily, _ = server.identities.add_person("Emily Carter")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        await server.spaces.runtime(daniel)
        server.spaces.budget = sum(space_size(space.runtime) for space in server.spaces._spaces.values()) + 100_000
        engine = runtime.built.engine
        session = await asyncio.to_thread(engine.session, "cc:s1")
        text = "Quarterly figures, nothing personal in them. " * 9_000  # about 400 KiB, no value: nothing is announced
        await asyncio.to_thread(engine.mask_text, text, SlotKind.TOOL_RESULT, session)
        assert server.spaces.count() == 2
        assert await server.spaces.runtime(emily) is runtime  # weighed again as it is used: Daniel's goes
        assert server.spaces.count() == 1 and daniel.id not in server.spaces._spaces


async def test_a_spaces_weight_counts_the_bytes_its_caches_hold(server):
    emily, _ = server.identities.add_person("Emily Carter")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        engine = runtime.built.engine
        assert engine._frozen.budget == SPACE_CACHE_BYTES  # bounded in bytes, one space of many
        before = space_size(runtime)
        session = engine.session("cc:s1")
        text = "Quarterly figures, nothing personal in them. " * 9_000  # about 400 KiB
        await asyncio.to_thread(engine.mask_text, text, SlotKind.TOOL_RESULT, session)
        held = space_size(runtime)
        assert held - before >= len(text)  # the masked block it keeps
        await asyncio.to_thread(runtime.built.engine.session, "cc:s2")
        projects = runtime.projects
        assert projects is not None
        projects.attach(engine.session("cc:s3"), "/home/emily/billing-api")
        assert space_size(runtime) > held  # its conversations and its projects are weighed too
        server.spaces.budget = space_size(runtime) + SPACE_BASE // 2  # room for Emily's alone
        await server.spaces.runtime(daniel)
        assert emily.id not in server.spaces._spaces  # by what it holds, not by a count of its entries


async def test_a_space_at_its_caps_past_its_share_learns_nothing_new_and_leaves_the_others_in_place(server):
    """The review's probe: an API key whose settings hold as many recognizers as a server keeps for one identity (each
    weighed 64 KiB), on an instance whose budget leaves its space past its share. Its use closes no other space to make
    room (each of its uses closed every other idle space, whose next request built it again); its request that brings
    a new value is stopped with a clear error, never sent unmasked, and one that brings none is masked as ever; and the
    next use of another identity's space closes it first."""
    heavy, _ = server.identities.add_key("Heavy CI")
    others = [server.identities.add_key(f"Other {n}")[0] for n in range(3)]
    rules = [{"name": f"r{i}", "pattern": f"ZQ{i:05d}-[0-9]{{6}}"} for i in range(OWN_RULES["recognizers"])]
    async with listening(server):
        runtime = await server.spaces.runtime(heavy)
        await asyncio.to_thread(
            runtime.update_config, lambda current: current.model_copy(update={"recognizers": rules})
        )
        for other in others:
            await server.spaces.runtime(other)
        everyone = {heavy.id, *(other.id for other in others)}
        engine = runtime.built.engine
        session = await asyncio.to_thread(engine.session, "cc:heavy")
        server.spaces.budget = space_size(runtime) + 6 * SPACE_BASE  # room for every space: past its share alone
        await server.spaces.app(heavy)  # one use of it: weighed
        with pytest.raises(MaskingError, match="share of the memory") as stopped:
            await asyncio.to_thread(engine.mask_text, f"write to {CANARY}", SlotKind.TOOL_RESULT, session)
        assert CANARY not in str(stopped.value) and runtime.built.vault.by_original(CANARY) is None
        assert await _leaves(runtime, "the build is green") == "the build is green"
        server.spaces.budget = space_size(runtime) + 2 * SPACE_BASE  # and past the budget with the others
        await server.spaces.app(heavy)
        assert set(server.spaces._spaces) == everyone  # none closed to make room for it
        await server.spaces.runtime(others[0])  # another identity's use: the space past its share goes first
        assert set(server.spaces._spaces) == everyone - {heavy.id}


async def test_a_space_past_its_share_builds_no_new_project_masker_until_it_is_under_it(server):
    """Each project of hers that changes anything is a masker of its own in her space: past its share, the space builds
    no new one, and a conversation in such a folder is refused with the share's words, never masked by the global rules
    alone; once the space is under its share again, the folder's masker is built at its next use."""
    emily, _ = server.identities.add_person("Emily Carter")
    heron, folder = TermConfig(value="Project Heron", type="CODENAME"), "/home/emily/heron"
    async with listening(server):
        runtime = await server.spaces.runtime(emily)

        def with_heron(current):
            return current.model_copy(update={"projects": [ProjectEntry(path=folder, terms=[heron])]})

        await asyncio.to_thread(runtime.update_config, with_heron)
        server.spaces.budget = space_size(runtime)  # her space alone weighs past half of it
        await server.spaces.runtime(emily)  # weighed
        with pytest.raises(MaskingError, match="share of the memory"):  # a text with no value: the folder's refusal
            await _leaves_in(runtime, folder, "the build is green")
        assert await _leaves(runtime, "the build is green") == "the build is green"  # elsewhere, as ever
        server.spaces.budget = 2048 * 1024 * 1024
        await server.spaces.runtime(emily)  # weighed again: under its share
        assert "Project Heron" not in await _leaves_in(runtime, folder, "the Project Heron launch is on Friday")


async def test_two_clients_at_once_never_build_project_maskers_past_the_share(server, monkeypatch):
    """The re-review's probe (RP3): each project of hers that changes anything is a masker of its own, holding the
    global rules again; the share was checked by a weighing that read the projects' weight without waiting for their
    lock, so two clients at once, each building in turn while the other's request was weighed, built 38 maskers of 40
    where one client built 3. A masker is weighed under the projects' lock before it is built: two clients at once
    build what the share holds, and no more."""
    from latet import projects as projects_module

    build = projects_module.build_masker

    def slow(*args, **kwargs):  # a build that takes its time, as one at the caps does: the lock held meanwhile
        time.sleep(0.05)
        return build(*args, **kwargs)

    monkeypatch.setattr(projects_module, "build_masker", slow)
    emily, _ = server.identities.add_person("Emily Carter")
    recognizers = [{"name": f"r{n}", "pattern": f"ZQ{n:03d}-[0-9]{{6}}"} for n in range(100)]
    folders = [f"/home/emily/repo-{n:02d}" for n in range(40)]
    entries = [
        ProjectEntry(path=folder, terms=[TermConfig(value=f"Codename {n:02d}", type="CODENAME")])
        for n, folder in enumerate(folders)
    ]
    async with listening(server):
        runtime = await server.spaces.runtime(emily)

        def with_projects(current):
            return current.model_copy(update={"recognizers": recognizers, "projects": entries})

        await asyncio.to_thread(runtime.update_config, with_projects)
        server.spaces.budget = 2 * (space_size(runtime) + 5 * masker_weight(runtime.config) // 2)  # 2.5 maskers more
        await server.spaces.runtime(emily)  # weighed

        async def client(mine):  # each request in a thread of its own, weighed after it as the server weighs hers
            for folder in mine:
                with contextlib.suppress(MaskingError):  # its folder's masker not built: refused with the share's words
                    await _leaves_in(runtime, folder, "the build is green")
                await server.spaces.runtime(emily)

        await asyncio.gather(client(folders[::2]), client(folders[1::2]))
        projects = runtime.projects
        assert projects is not None
        assert 1 <= len(projects._scopes) <= 2, len(projects._scopes)  # what the share holds, as for one client alone


async def test_one_long_text_never_carries_a_space_past_its_share(server):
    """The re-review's probe (RP1): one tool output of 2.5 million characters, 160 lines of a key of 16,000 characters,
    kept 135 values worth 82 MiB that no weighing saw, each value's length counted only when the vault's parts were next
    read. Each value is weighed as it is kept, its length too: the call stops with the share's words once the next one
    would carry the space past its share, and what it kept before stays within it."""
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        vault, engine = runtime.built.vault, runtime.built.engine
        server.spaces.budget = 2 * (space_size(runtime) + 8 * 1024 * 1024)  # her share: room for 8 MiB more
        await server.spaces.runtime(emily)  # weighed
        rng, count = random.Random(7), vault.count
        text = "\n".join(f"api_key_{n} = {rng.randbytes(8_000).hex()}" for n in range(160))
        session = await asyncio.to_thread(engine.session, "cc:large")
        with pytest.raises(MaskingError, match="share of the memory") as stopped:
            await asyncio.to_thread(engine.mask_text, text, SlotKind.TOOL_RESULT, session)
        assert text[-200:] not in str(stopped.value)
        await asyncio.to_thread(vault.matchers.current)  # every value it kept, matched and so weighed by any count
        assert vault.count > count  # it kept what fitted
        assert space_size(runtime) <= server.spaces.budget * spaces_module.SPACE_SHARE


TAUGHT = "Orion Holdings"  # a client's name the local reviewer taught: only the vault's own detector finds it again


async def _past_its_share_on_b(a, b, emily):
    """Her spaces on A and on B, the one on B at its share of B's budget (no room for one more value), and a value
    taught on A."""
    here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
    b.spaces.budget = 2 * space_size(there)
    await b.spaces.runtime(emily)  # weighed
    return here, there, await asyncio.to_thread(here.built.vault.intern, "CLIENT", EntityClass.CUSTOM, TAUGHT)


async def test_a_space_past_its_share_learns_no_value_a_conversation_names_and_stops_what_needs_it(instances):
    """RP4: values another instance kept are learnt through ``Vault.learn``, outside the share's check of a value kept
    here, so a space past its share on one instance still grew by what another kept. It learns none past its share,
    whatever the route: a conversation the other instance showed a value is stopped with the share's words, never sent
    with the value in clear, which only the vault's own detector would find; and the space is made again from
    PostgreSQL at its next use, every value known."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    a.listening.set()  # no ear: B learns of the value through the conversation alone
    b.listening.set()
    try:
        here, there, entity = await _past_its_share_on_b(a, b, emily)
        conversations = here.built.engine.conversations
        session = await asyncio.to_thread(conversations.session, "cc:s1")
        session.add({entity.id})
        await asyncio.to_thread(conversations.update, session)
        text = f"the {TAUGHT} renewal is due on Friday"

        def masked(runtime) -> str:
            engine = runtime.built.engine
            return engine.mask_text(text, SlotKind.TOOL_RESULT, engine.session("cc:s1")).text

        with pytest.raises(MaskingError, match="share of the memory") as stopped:
            await asyncio.to_thread(masked, there)
        assert TAUGHT not in str(stopped.value) and there.built.vault.get(entity.id) is None  # not learnt
        again = await b.spaces.runtime(emily)
        assert again is not there and again.built.vault.get(entity.id) == entity  # made again, whole
        assert TAUGHT not in await asyncio.to_thread(masked, again)
    finally:
        await a.spaces.close()
        await b.spaces.close()


async def test_a_space_past_its_share_learns_no_value_another_instance_keeps_and_is_made_again(instances):
    """RP4: a value another instance keeps and announces is not learnt by a space past its share here: its vault stops
    every text with the share's words from then on, and the space is closed, then made again from PostgreSQL at its
    next use, every value known."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        _, there, entity = await _past_its_share_on_b(a, b, emily)
        await until(lambda: emily.id not in b.spaces._spaces)  # heard, not learnt: closed
        assert there.built.vault.get(entity.id) is None
        again = await b.spaces.runtime(emily)
        assert again is not there and again.built.vault.get(entity.id) == entity
        assert TAUGHT not in await _leaves(again, f"the {TAUGHT} renewal is due on Friday")


async def test_a_catch_up_past_its_share_learns_nothing_and_its_space_is_made_again(instances):
    """RP4: the beat's catch-up of a vault learns every value PostgreSQL holds that the space does not know, past its
    share none: the space is made again from PostgreSQL at its next use instead."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    a.listening.set()  # no ear: the catch-up alone
    b.listening.set()
    try:
        _, there, entity = await _past_its_share_on_b(a, b, emily)
        await b.spaces._run(spaces_module._resync, b.spaces._spaces[emily.id], b.db)
        assert there.built.vault.get(entity.id) is None
        again = await b.spaces.runtime(emily)
        assert again is not there and again.built.vault.get(entity.id) == entity
    finally:
        await a.spaces.close()
        await b.spaces.close()


def _scope(kind: str, path: str, identity_id: str) -> dict:
    """A request of ``identity_id``'s as the gate hands it to the identity's app: an HTTP GET, or a WebSocket."""
    scope = {
        "type": kind, "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "https", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": [(b"host", b"latet.acme.lan")],
        "client": ("10.0.3.9", 50000), "server": ("latet.acme.lan", 443), "latet.identity": identity_id,
    }  # fmt: skip
    return {**scope, "method": "GET"} if kind == "http" else {**scope, "scheme": "wss", "subprotocols": []}


async def _heavy_and_others(server):
    """An API key whose space weighs past its share once the budget is set (its own recognizers at their cap), and three
    others within theirs; the key's space, its app, and every identity's id."""
    heavy, _ = server.identities.add_key("Heavy CI")
    others = [server.identities.add_key(f"Other {n}")[0] for n in range(3)]
    rules = [{"name": f"r{i}", "pattern": f"ZQ{i:05d}-[0-9]{{6}}"} for i in range(OWN_RULES["recognizers"])]
    runtime = await server.spaces.runtime(heavy)
    await asyncio.to_thread(runtime.update_config, lambda current: current.model_copy(update={"recognizers": rules}))
    for other in others:
        await server.spaces.runtime(other)
    return heavy, others, server.spaces._spaces[heavy.id], {heavy.id, *(other.id for other in others)}


async def test_a_space_past_its_share_holding_only_long_connections_goes_first_and_ends_them(server):
    """RP2: a long connection keeps its space in use for its whole life (a dashboard's stream, a WebSocket), and a space
    in use was never closed for the budget: one carried past the budget so had each other identity's use close every
    other idle space, built again at its next request. A space past its share that holds only long connections goes
    first, its connections ended through the path a close takes (``Space.end_long``) with the share's words; the spaces
    within their share stay."""
    async with listening(server):
        heavy, others, space, everyone = await _heavy_and_others(server)
        inner = space.app

        async def held(scope, receive, send):  # its WebSocket held open, as a relay holds one
            if scope["type"] != "websocket":
                return await inner(scope, receive, send)
            await send({"type": "websocket.accept"})
            await asyncio.Event().wait()

        space.app = held
        app, sent, read, given = await server.spaces.app(heavy), [], [""], []

        async def socket_sent(message):
            sent.append(message)

        async def stream_sent(message):
            read[0] += message.get("body", b"").decode() if message["type"] == "http.response.body" else ""

        async def asked():  # a GET's empty body, then a viewer who stays
            if not given:
                given.append(True)
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def stays():
            await asyncio.Event().wait()

        socket = asyncio.create_task(app(_scope("websocket", "/relay/v1/responses", heavy.id), stays, socket_sent))
        stream = asyncio.create_task(app(_scope("http", f"{API}/stream", heavy.id), asked, stream_sent))
        await until(lambda: sent and "retry: 3000" in read[0])  # both open
        server.spaces.budget = space_size(space.runtime) + 2 * SPACE_BASE  # past the budget, and past its share alone
        await server.spaces.runtime(others[0])  # another identity's use
        assert set(server.spaces._spaces) == everyone - {heavy.id}  # it went, and none within its share
        await asyncio.wait_for(asyncio.gather(socket, stream), 5)  # both ended
    [closed] = [message for message in sent if message["type"] == "websocket.close"]
    assert closed["code"] == spaces_module.CLOSED_CODE and "past its share" in closed["reason"]
    assert len(closed["reason"].encode()) <= 123  # what a WebSocket's close carries


async def test_a_space_past_its_share_serving_a_request_counts_at_its_share_and_goes_when_it_ends(server):
    """RP2: a space past its share that serves a request is not closed under it; while it stays it counts at its share,
    so that no space within its share is closed for what it holds past it, and it goes when its request ends."""
    async with listening(server):
        heavy, others, space, everyone = await _heavy_and_others(server)
        inner, release = space.app, asyncio.Event()

        async def held(scope, receive, send):  # a request of the key's under way until released
            if scope.get("path") != "/held":
                return await inner(scope, receive, send)
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"done"})

        async def asked():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def ignored(message):
            pass

        space.app = held
        app = await server.spaces.app(heavy)
        request = asyncio.create_task(app(_scope("http", "/held", heavy.id), asked, ignored))
        await until(lambda: space.busy == 1)
        server.spaces.budget = space_size(space.runtime) + 2 * SPACE_BASE  # past the budget, and past its share alone
        await server.spaces.runtime(others[0])  # another identity's use
        assert set(server.spaces._spaces) == everyone  # neither it, serving, nor any within its share for it
        release.set()
        await asyncio.wait_for(request, 5)
        assert set(server.spaces._spaces) == everyone - {heavy.id}  # it went as its request ended


async def test_a_space_serving_a_request_is_never_closed_for_the_budget(server):
    emily, _ = server.identities.add_person("Emily Carter")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    server.spaces.budget = SPACE_BASE + SPACE_BASE // 2  # room for one space
    async with listening(server):
        await server.spaces.runtime(emily)
        server.spaces._spaces[emily.id].busy = 1  # a request of hers under way
        await server.spaces.runtime(daniel)
        assert server.spaces.count() == 2 and emily.id in server.spaces._spaces
        server.spaces._spaces[emily.id].busy = 0


async def test_a_spaces_own_changes_count_as_applied(server, monkeypatch):
    from latet.server import spaces as spaces_module

    emily, _ = server.identities.add_person("Emily Carter")
    caught_up = []
    resync, reapply = spaces_module._resync, spaces_module._reapply

    def counted(space, db):
        caught_up.append(("vault", space.identity.id))
        return resync(space, db)

    def reapplied(space):
        caught_up.append(("settings", space.identity.id))
        return reapply(space)

    monkeypatch.setattr(spaces_module, "_resync", counted)
    monkeypatch.setattr(spaces_module, "_reapply", reapplied)
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        await asyncio.to_thread(runtime.update_config, _add_term("Mercury Labs"))  # an own part, then a snapshot
        await asyncio.to_thread(runtime.built.vault.intern, "PASSWORD", EntityClass.SECRET, "hunter2-orchid")
        await asyncio.to_thread(runtime.settings.save, _add_term("Venus Corp")(runtime.config))  # a save alone
        for _ in range(3):
            await server.check()
        await asyncio.sleep(0.3)
        assert caught_up == []  # nothing it made itself counts as missed: neither its vault nor its settings


def _projects(count: int, start: int = 0):
    """Settings naming ``count`` folders of Emily's, each with a term of its own."""

    def change(current):
        entries = [
            ProjectEntry(path=f"/home/emily/client-{i:05d}", terms=[TermConfig(value=f"Client Name {i:05d}")])
            for i in range(start, start + count)
        ]
        return current.model_copy(update={"projects": entries})

    return change


def _private_bytes() -> int | None:
    """What this process holds of its own, C++ allocations included (RE2's programs and their caches, which tracemalloc
    does not see); None where there is no such count."""
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.wintypes.DWORD), ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]  # fmt: skip

        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(Counters), ctypes.wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return int(counters.PrivateUsage)
    if sys.platform.startswith("linux"):
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("RssAnon:"):
                    return int(line.split()[1]) * 1024
    return None


_CUSTOMERS = [TermConfig(value=f"Customer Company {i:04d}", type="ORG") for i in range(500)]


def _customers(current):
    """Her own list of customers, 500 terms: every folder's masker holds them."""
    return current.model_copy(update={"terms": _CUSTOMERS})


def _varied(rng: random.Random) -> str:
    """A tool's output of about 6 KB naming the customers, each word as written or run on into another."""
    words = [*(t.value for t in _CUSTOMERS), "lorem", "ipsum", "dolor", "sit", "amet", "Company"]
    return " ".join(rng.choice(words) + rng.choice(["", "x", "ly", "s", "-", "."]) for _ in range(400))


async def test_a_spaces_projects_are_weighed_by_what_they_hold(server):
    if _private_bytes() is None:
        pytest.skip("no count of a process's own memory on this system")
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        # As many folders as a server keeps, and her list of customers.
        await asyncio.to_thread(runtime.update_config, lambda c: _customers(_projects(200)(c)))
        projects = runtime.projects
        assert projects is not None
        gc.collect()
        before, held = space_size(runtime), _private_bytes() or 0
        for i in range(200):  # each folder used by a conversation
            await asyncio.to_thread(projects.resolve, f"/home/emily/client-{i:05d}")
        gc.collect()
        grown, weighed = (_private_bytes() or 0) - held, space_size(runtime) - before
        assert weighed >= grown / 2  # by what they hold: a masker and a copy of the settings each


async def test_a_spaces_projects_are_weighed_by_what_their_maskers_grow_to_as_they_work(server):
    if _private_bytes() is None:
        pytest.skip("no count of a process's own memory on this system")
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        await asyncio.to_thread(runtime.update_config, lambda c: _customers(_projects(50)(c)))
        projects = runtime.projects
        assert projects is not None
        gc.collect()
        before, held = space_size(runtime), _private_bytes() or 0
        rng = random.Random(7)

        def work() -> None:  # each folder used by a conversation, its masker then masking 30 texts
            for i in range(50):
                scope = projects.resolve(f"/home/emily/client-{i:05d}").scope
                assert scope is not None
                for _ in range(30):
                    scope.masker.mask(_varied(rng), SlotKind.TOOL_RESULT)

        await asyncio.to_thread(work)
        gc.collect()
        grown, weighed = (_private_bytes() or 0) - held, space_size(runtime) - before
        assert weighed >= grown / 2  # what RE2 grows to in C++ memory included


async def test_a_save_naming_more_projects_than_a_server_keeps_is_refused(server):
    from latet.server.shared import PROJECTS_LIMIT, TooManyProjects

    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        with pytest.raises(TooManyProjects, match="projects") as refused:
            await asyncio.to_thread(runtime.update_config, _projects(PROJECTS_LIMIT + 1))
        assert str(PROJECTS_LIMIT) in str(refused.value)
        assert runtime.config.projects == []  # nothing applied, nothing kept
        await asyncio.to_thread(runtime.update_config, _projects(PROJECTS_LIMIT))  # as many as it keeps: kept


async def test_a_space_heavy_with_projects_is_closed_for_another_identitys_build(server):
    emily, _ = server.identities.add_person("Emily Carter")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        await asyncio.to_thread(runtime.update_config, _projects(60))
        projects = runtime.projects
        assert projects is not None
        for i in range(60):
            await asyncio.to_thread(projects.resolve, f"/home/emily/client-{i:05d}")
        await server.spaces.runtime(emily)  # weighed again at her next use, as at the end of each request
        server.spaces.budget = SPACE_BASE * 2  # room for two light spaces, not for hers and another
        assert space_size(runtime) > SPACE_BASE * 1.05
        await server.spaces.runtime(daniel)
        assert emily.id not in server.spaces._spaces  # weighed by what its projects hold, and not in use


async def test_a_spaces_engine_keeps_its_restores_within_bytes_and_they_are_weighed(server):
    from latet.engine.pipeline import SPACE_RESTORES_BYTES

    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        engine, vault = runtime.built.engine, runtime.built.vault
        session = await asyncio.to_thread(engine.session, "cc:long")
        for n in range(300):  # a long conversation's values (kept in memory here: the restores are what is weighed)
            vault._index(Entity(f"e{n:019d}", "EMAIL", EntityClass.PII, f"p{n}@acme.example", f"q{n}@fake.example",
                                0, time.time()))  # fmt: skip
            session.entity_ids.add(f"e{n:019d}")
        before = space_size(runtime)
        for n in range(40):  # each response restored after a new value: the vault's version moves
            vault._index(Entity(f"f{n:019d}", "EMAIL", EntityClass.PII, f"n{n}@acme.example", f"m{n}@fake.example",
                                0, time.time()))  # fmt: skip
            restores = engine.restores(session, json_mode=n % 2 == 0)
            assert len(restores) >= 300  # whole, whatever is kept
        assert engine._restores.weight <= SPACE_RESTORES_BYTES
        assert space_size(runtime) - before >= engine._restores.weight > 0  # and the space is weighed by them


async def test_an_ordinary_save_makes_no_other_instance_reload_the_vault(instances, monkeypatch):
    from latet.server import spaces as spaces_module

    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    reloads, reapplied = [], []  # by the instance whose space did it
    resync, reapply = spaces_module._resync, spaces_module._reapply

    def counted_resync(space, *args):
        reloads.append(space.origin.partition(":")[0])
        return resync(space, *args)

    def counted_reapply(space, **kwargs):
        reapplied.append(space.origin.partition(":")[0])
        return reapply(space, **kwargs)

    monkeypatch.setattr(spaces_module, "_resync", counted_resync)
    monkeypatch.setattr(spaces_module, "_reapply", counted_reapply)
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))  # a save from her dashboard on A
        await until(lambda: "Mercury Labs" in _terms(there))
        await asyncio.sleep(0.3)  # its application over (the beat's checks are seconds apart)
        for _ in range(3):
            await b.check()
        await asyncio.sleep(0.3)
        assert reloads == []  # nothing of her vault read again, on either instance
        assert reapplied.count(b.instance) == 1  # her settings read once on B, as told: their untold own part with them
        reapplied.clear()
        await asyncio.to_thread(here.settings.save, _add_term("Venus Corp")(here.config))  # a save alone: untold
        for _ in range(2):
            await b.check()
        await until(lambda: "Venus Corp" in _terms(there))  # her settings caught up by their own count
        await asyncio.sleep(0.3)
        for _ in range(3):
            await b.check()
        await asyncio.sleep(0.3)
        assert reloads == [] and reapplied.count(b.instance) == 1  # once, and caught up from then on


async def test_a_reapplication_at_the_projects_cap_holds_no_instance_for_long(instances, monkeypatch):
    from latet.server import spaces as spaces_module

    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    a.shared.set(_gateways("atlas", "falcon"), actor=ADMIN)
    # Collections on, over what the instances hold: the test run's own objects set aside, as a server holds none
    # (one of Python's full collections over them held the loop past the bound once in a whole run: Minor 5).
    with _collected_as_a_server_is():
        async with listening(a, b):
            here = await a.spaces.runtime(emily)
            # As many folders as a server keeps, and her list of customers.
            await asyncio.to_thread(here.update_config, lambda c: _customers(_projects(200)(c)))
            there = await b.spaces.runtime(emily)
            a.shared.set(_gateways("falcon"), actor=ADMIN)  # a shared destination removed: a change waits for her
            await until(lambda: bool(list(there.mediator.approvals.changes)), seconds=60)
            await asyncio.sleep(0.3)
            took: list[float] = []
            reapply = spaces_module._reapply

            def timed(space):
                started = time.perf_counter()
                try:
                    return reapply(space)
                finally:
                    took.append(time.perf_counter() - started)

            monkeypatch.setattr(spaces_module, "_reapply", timed)
            loop, gaps, ticking = asyncio.get_running_loop(), [], [True]

            async def tick() -> None:  # B's event loop, as every other identity's requests there see it
                last = loop.time()
                while ticking[0]:
                    await asyncio.sleep(0.01)
                    now = loop.time()
                    gaps.append(now - last)
                    last = now

            ticker = asyncio.create_task(tick())
            await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))  # a save of hers on A
            await until(lambda: "Mercury Labs" in _terms(there) and bool(took), seconds=60)  # applied, and over
            ticking[0] = False
            await ticker
            assert max(took) < 1.0  # B's re-application, the composition with a change waiting included
            # The loop it shares with everyone else hardly waited meanwhile (a collection of Python's memory may hold it
            # once or twice anyway): every folder weighed made it wait past 50 ms a dozen times in such a window.
            assert sum(gap > 0.05 for gap in gaps) <= 2, sorted(gaps)[-5:]


@contextlib.contextmanager
def _collected_as_a_server_is():
    """Python's collections of memory on, as a server runs them, over what the instances under test hold: what the test
    run held before is set aside (``gc.freeze``), since a server holds no other test's objects, and a full collection
    visits every object the process holds."""
    gc.collect()
    gc.freeze()
    try:
        yield
    finally:
        gc.unfreeze()


async def test_her_own_thirty_thousand_long_terms_hold_no_instance_for_long(instances, monkeypatch):
    """Her own 30,000 terms of 60 characters that are not whole words were compiled in one RE2 call holding the GIL: a
    save of hers held another instance's loop 2.1 s, and so did each folder's build (the re-review's Important 3). In
    parts, each compile is bounded, B reapplies and builds the folder off-loop, and sampled terms stay masked. This
    checks the work's size and scheduling, rather than promising a wall-clock pause on a busy machine."""
    import re2

    from latet.engine.detectors.terms import PART_CHARS

    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    rng = random.Random(7)
    alphabet = string.ascii_lowercase + string.digits + "-"
    mine = [TermConfig(value="".join(rng.choice(alphabet) for _ in range(60)), whole_word=False) for _ in range(30_000)]
    with _collected_as_a_server_is():
        async with listening(a, b):
            here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
            loop_thread, operation = threading.get_ident(), threading.local()
            compiled: dict[str, list[int]] = {"reapply": [], "project": []}
            applied: list[int] = []
            failures: list[str] = []
            real_reapply, real_regexp = spaces_module._reapply, re2._Regexp

            @contextlib.contextmanager
            def observed(stage):
                if threading.get_ident() == loop_thread:
                    failures.append(f"{stage} ran on the event loop")
                    raise AssertionError(failures[-1])
                before = getattr(operation, "stage", None)
                operation.stage = stage
                try:
                    yield
                finally:
                    operation.stage = before

            class Counted(real_regexp):
                def __init__(self, pattern, options):
                    stage = getattr(operation, "stage", None)
                    if stage is not None:
                        size = len(pattern)
                        if size > PART_CHARS:
                            failures.append(f"{stage} compiled {size} bytes, past {PART_CHARS}")
                            raise AssertionError(failures[-1])  # reject before a giant GIL-holding compile
                        compiled[stage].append(size)
                    super().__init__(pattern, options)

            def reapply(space):
                if space.runtime is not there:
                    return real_reapply(space)
                with observed("reapply"):
                    result = real_reapply(space)
                applied.append(1)
                return result

            def propagated(check):
                assert not failures, failures
                return check()

            monkeypatch.setattr(spaces_module, "_reapply", reapply)
            monkeypatch.setattr(re2, "_Regexp", Counted)
            await asyncio.to_thread(here.update_config, lambda c: c.model_copy(update={"terms": mine}))
            await until(lambda: propagated(lambda: len(there.config.terms) >= 30_000 and bool(applied)), seconds=120)
            for value in ("Mercury Labs", "Venus Corp", "Mars Ltd"):
                applied.clear()
                compiled["reapply"].clear()
                await asyncio.to_thread(here.update_config, _add_term(value))
                await until(
                    lambda value=value: propagated(lambda: value in _terms(there) and bool(applied)), seconds=120
                )
                assert sum(compiled["reapply"]) >= sum(len(t.value) for t in mine)
            folder = "/home/emily/billing-api"
            applied.clear()
            await asyncio.to_thread(here.update_config, lambda c: c.model_copy(update={"projects": [
                ProjectEntry(path=folder, terms=[TermConfig(value="Project Falcon")])
            ]}))  # fmt: skip
            await until(lambda: propagated(lambda: bool(there.config.projects) and bool(applied)), seconds=120)
            projects = there.projects
            assert projects is not None
            real_scope = projects._scope

            def scope(*args, **kwargs):
                with observed("project"):
                    return real_scope(*args, **kwargs)

            monkeypatch.setattr(projects, "_scope", scope)
            sample = rng.sample([t.value for t in mine], 50)
            masked = await _leaves_in(there, folder, " ; ".join(["Project Falcon", *sample]))  # the folder built
            assert not failures, failures
            assert sum(compiled["project"]) >= sum(len(t.value) for t in mine)
    assert "Project Falcon" not in masked and not any(value in masked for value in sample)  # every term found


async def test_a_check_queues_no_catch_up_while_one_is_pending(instances, monkeypatch):
    from latet.server import spaces as spaces_module

    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    release, applied = threading.Event(), []
    reapply = spaces_module._reapply

    def held(space):  # B's re-applications wait until let go: the checks go on meanwhile
        if space.origin.startswith(b.instance):
            release.wait(10)
            applied.append(space.identity.id)
        return reapply(space)

    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        monkeypatch.setattr(spaces_module, "_reapply", held)
        await asyncio.to_thread(here.settings.save, _add_term("Venus Corp")(here.config))  # a save alone: untold
        for _ in range(8):  # a beat's checks, while B's catch-up waits
            await b.check()
        release.set()
        await until(lambda: "Venus Corp" in _terms(there))
        await asyncio.sleep(0.3)
        assert applied == [emily.id]  # one catch-up, however many checks found the space behind meanwhile
        await asyncio.to_thread(here.settings.save, _add_term("Mars Holdings")(here.config))  # another, later
        for _ in range(3):
            await b.check()
        await until(lambda: "Mars Holdings" in _terms(there))  # the one before is over: this one is caught up too
        assert applied == [emily.id, emily.id]


async def test_what_a_space_missed_while_built_is_read_once_however_many_changes_it_missed(instances, monkeypatch):
    """A-13: a burst of changes heard while a space is built costs one reading of its settings, as in its queue."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    readings = []
    reapply = spaces_module._reapply

    def counted(space):
        if space.origin.startswith(b.instance):
            readings.append(space.identity.id)
        return reapply(space)

    monkeypatch.setattr(spaces_module, "_reapply", counted)
    async with listening(a, b):
        here = await a.spaces.runtime(emily)
        built, go = threading.Event(), threading.Event()
        build = b.spaces._build

        def slow(identity):
            found = build(identity)
            built.set()
            go.wait(10)
            return found

        monkeypatch.setattr(b.spaces, "_build", slow)
        building = asyncio.create_task(b.spaces.runtime(emily))
        await until(built.is_set)
        names = ("Mercury Labs", "Venus Corp", "Mars Holdings", "Jupiter Group", "Saturn Partners")
        for name in names:
            await asyncio.to_thread(here.update_config, _add_term(name))  # five saves of hers, each told
        await until(lambda: len(b.spaces._missed.get(emily.id) or ()) == len(names))
        go.set()
        assert set(names) <= set(_terms(await building))
        assert readings == [emily.id]  # one reading for the five


async def test_a_change_missed_while_built_that_cannot_be_applied_is_refused_as_unavailable(instances, monkeypatch):
    """C-19: its requests are told to try again (503), never answered with the application's own error (500)."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here = await a.spaces.runtime(emily)
        built, go = threading.Event(), threading.Event()
        build = b.spaces._build

        def slow(identity):
            found = build(identity)
            built.set()
            go.wait(10)
            return found

        async def broken(space, channel, message, seq=None):
            raise RuntimeError("the words of a fault")

        monkeypatch.setattr(b.spaces, "_build", slow)
        monkeypatch.setattr(b.spaces, "_apply", broken)
        building = asyncio.create_task(b.spaces.runtime(emily))
        await until(built.is_set)
        await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))
        await until(lambda: bool(b.spaces._missed.get(emily.id)))
        go.set()
        with pytest.raises(SpaceUnavailable):
            await building
        assert b.spaces.count() == 0  # closed, made again at its next use


async def test_no_more_spaces_are_built_at_once_than_the_bound(server, monkeypatch):
    """M-08: two hundred builds at once held every identity's event loop for seconds: the others wait their turn."""
    people = [server.identities.add_person(f"Person {n}")[0] for n in range(BUILDS_AT_ONCE * 3)]
    building, most, lock = [0], [0], threading.Lock()
    build = server.spaces._build

    def counted(identity):
        with lock:
            building[0] += 1
            most[0] = max(most[0], building[0])
        try:
            time.sleep(0.05)
            return build(identity)
        finally:
            with lock:
                building[0] -= 1

    monkeypatch.setattr(server.spaces, "_build", counted)
    async with listening(server):
        runtimes = await asyncio.gather(*(server.spaces.runtime(person) for person in people))
    assert len({id(runtime) for runtime in runtimes}) == len(people) and most[0] == BUILDS_AT_ONCE


def test_closing_a_space_is_waited_for_as_long_as_its_parts_take():
    """A-11: its recorder drains for five seconds at most, then its conversations' writer sends for WRITER_CLOSE."""
    assert CLOSE_SECONDS > 5.0 + WRITER_CLOSE  # and a margin: its runtime closes after them


async def test_an_idle_space_weighs_what_it_holds(server):
    """A-14, M-10: an idle space measured 0.3 MB and was weighed at 24 MB (85 spaces in 2 GiB); its own masker is
    weighed by its rules, and each value at what its parts and its entity take."""
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        idle = space_size(runtime)
        assert SPACE_BASE <= idle < 2 * 1024 * 1024
        await asyncio.to_thread(runtime.update_config, lambda c: c.model_copy(update={"terms": [
            TermConfig(value=f"Client {n:05d}") for n in range(1_000)]}))  # fmt: skip
        with_terms = space_size(runtime)
        assert with_terms - idle >= 1_000 * 256  # its masker holds each term
        count = runtime.built.vault.count
        secret = await asyncio.to_thread(
            runtime.built.vault.intern, "PASSWORD", EntityClass.SECRET, "hunter2-correct-horse"
        )
        await asyncio.to_thread(runtime.built.vault.matchers.current)  # the parts hold it from their next reading
        added = runtime.built.vault.count - count
        # A value: its entity and its share of the parts that match the values and their fakes, and each character they
        # hold of it (2,841 bytes measured for a value of 40 characters: 3,136 weighed).
        chars = max(len(secret.original), len(secret.surrogate))
        weighed = spaces_module._PER_VALUE + spaces_module._PER_CHAR * chars
        assert added == 1 and space_size(runtime) - with_terms == weighed


async def test_a_spaces_long_values_are_weighed_by_their_length(server):
    """A-14: each value was weighed at 2,560 bytes whatever its length, and strict data mode keeps long ones: a whole
    JSON string of 40,000 characters takes about a megabyte in the parts that match the values and their fakes."""
    if _private_bytes() is None:
        pytest.skip("no count of a process's own memory on this system")
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        vault = runtime.built.vault
        await asyncio.to_thread(vault.matchers.current)
        gc.collect()
        before, held = space_size(runtime), _private_bytes() or 0

        def keep() -> None:  # forty strings of a table's column, each one value of strict data mode's
            rng = random.Random(11)
            for _ in range(40):
                vault.intern(
                    "CUSTOM", EntityClass.STRUCT, "".join(rng.choice(string.ascii_lowercase) for _ in range(40_000))
                )
            vault.matchers.current()

        await asyncio.to_thread(keep)
        gc.collect()
        grown, weighed = (_private_bytes() or 0) - held, space_size(runtime) - before
        assert weighed >= grown / 2, (weighed, grown)  # by what they hold
        assert weighed >= 40 * 40_000 * spaces_module._PER_CHAR  # by their length: it was 40 x 2,560 bytes


async def test_a_burst_of_settings_changes_is_read_once_after_the_one_being_read(instances, monkeypatch):
    from latet.server import spaces as spaces_module

    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    release, applied = threading.Event(), []
    reapply = spaces_module._reapply

    def held(space):
        if space.origin.startswith(b.instance):
            applied.append(space.identity.id)
            release.wait(10)
        return reapply(space)

    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        monkeypatch.setattr(spaces_module, "_reapply", held)
        for name in ("Mercury Labs", "Venus Corp", "Mars Holdings", "Jupiter Group", "Saturn Partners"):
            await asyncio.to_thread(here.update_config, _add_term(name))  # five saves of hers on A, each told
        await until(lambda: len(applied) == 1)  # B reads the first, and the rest wait in its queue
        await asyncio.sleep(0.3)
        release.set()
        await until(lambda: "Saturn Partners" in _terms(there))
        await asyncio.sleep(0.3)
        assert len(applied) == 2  # the first, then one reading of everything that waited behind it
        assert {"Mercury Labs", "Venus Corp", "Mars Holdings", "Jupiter Group"} <= set(_terms(there))


async def test_closing_a_space_flushes_its_conversations_off_the_event_loop(server, monkeypatch):
    emily, _ = server.identities.add_person("Emily Carter")
    flushed = []

    def slow_close() -> None:  # a conversations' store that waits for its queued writes (9b's writer thread)
        time.sleep(1.0)
        flushed.append(True)

    async with listening(server):
        await server.spaces.runtime(emily)
        space = server.spaces._spaces[emily.id]
        monkeypatch.setattr(space.parts.conversations, "close", slow_close, raising=False)
        loop, gaps, ticking = asyncio.get_running_loop(), [], [True]

        async def tick() -> None:
            last = loop.time()
            while ticking[0]:
                await asyncio.sleep(0.01)
                now = loop.time()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(tick())
        await server.spaces.drop(emily.id)  # waits until the space is closed
        ticking[0] = False
        await ticker
        assert flushed == [True]  # its queued writes flushed, once
        assert max(gaps) < 0.5  # while the loop went on serving everyone else


async def test_a_space_holding_a_batch_is_never_closed_for_the_budget(server):
    emily, _ = server.identities.add_person("Emily Carter")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    server.spaces.budget = SPACE_BASE + SPACE_BASE // 2  # room for one space
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        # the plugin's, waiting
        await runtime.mediator.approvals.open(Batch("cc:s1", None, "claude-code", [], polled=True))
        await server.spaces.runtime(daniel)
        assert server.spaces.count() == 2 and emily.id in server.spaces._spaces  # over the budget, but it waits


def _screenshot(width: int, height: int) -> ImageWork:
    """A screenshot of a request, read: its pixels kept, to paint it again with the rectangles of a decision."""
    out = io.BytesIO()
    Image.effect_noise((width, height), 64).convert("RGB").save(out, "PNG")
    data = base64.b64encode(out.getvalue()).decode()
    ref = ImageRef({"type": "base64", "media_type": "image/png", "data": data}, "data", "base64", "image/png",
                   SlotKind.TOOL_RESULT, 0)  # fmt: skip
    return ImageWork(None, ref, image_key(data), decode(data))  # never painted here: no service to paint it


async def test_a_batch_holding_images_raises_its_spaces_weight_by_their_size_until_it_is_let_go(server, monkeypatch):
    """A-14: what a space holds for a batch of the Live Reviewer, its texts and its images (each as it came and as it
    would leave, and its pixels, kept to paint it again with your rectangles), counts in the space's weight while the
    batch is held, and no longer once the keeper has let go of it, decided."""
    monkeypatch.setattr(queue_module, "KEEP_EVERY", 0.05)  # the keeper's rounds, quickly
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        before = space_size(runtime)
        clean, masked, painted = _screenshot(640, 360), _screenshot(800, 450), _screenshot(800, 450)
        masked.ref.set(painted.original[0], "image/png")  # as it would leave: painted where its values were
        texts = ["Call Ada Byron about the Orion figures.", "Ada Byron, Orion", "lena.marsh@acme.com"]
        items = [
            {"kind": "tool_result", "path": None, "tool": "Read", "original": text, "masked": text.upper(),
             "replacements": [], "suggestions": [], "failed": None}
            for text in texts
        ]  # fmt: skip
        batch = Batch("cc:s1", None, "claude-code", items)
        batch.images = {1: clean, 2: masked}
        await runtime.mediator.approvals.open(batch)
        images = sys.getsizeof(clean.original[0]) + 640 * 360 * 4  # leaves as it came: one string
        images += sys.getsizeof(masked.original[0]) + sys.getsizeof(painted.original[0]) + 800 * 450 * 4
        held = sum(sys.getsizeof(item[side]) for item in items for side in ("original", "masked")) + images
        assert space_size(runtime) - before == held  # four bytes a pixel, as Pillow keeps an RGB image
        assert await runtime.mediator.approvals.decide(batch.id, True)
        await until(lambda: space_size(runtime) == before)  # the keeper let go of it


async def test_a_dashboards_decision_and_its_view_of_an_image_are_read_off_the_event_loop(server, monkeypatch):
    """A decision taken in a dashboard read its batch's record on the event loop (35 ms for one near its limit, its
    texts opened with it), and the dashboard's view of an image read the image's record there too: both are read in the
    queue's thread, the loop serving the other requests meanwhile."""
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        app, queue = await server.spaces.app(emily), runtime.backends.approvals
        loop_thread, opened_on = threading.current_thread(), []  # whether each record opened on the loop's thread
        open_json = queue._sealer.open_json

        def noted(raw, aad):
            opened_on.append(threading.current_thread() is loop_thread)
            return open_json(raw, aad)

        monkeypatch.setattr(queue._sealer, "open_json", noted)
        text = "y" * (queue_module.BATCH_LIMIT // 2 - 4_000)  # a record near its limit: as it came, as it leaves
        item = {"kind": "tool_result", "path": None, "tool": "Read", "original": text, "masked": text,
                "replacements": [], "suggestions": [], "failed": None}  # fmt: skip
        batch = Batch("cc:s1", None, "claude-code", [item])
        batch.images = {0: _screenshot(640, 360)}
        await runtime.mediator.approvals.open(batch)

        async def as_emily(scope, receive, send):  # as the gate hands on the requests of her dashboard
            await app({**scope, "latet.identity": emily.id}, receive, send)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=as_emily), base_url=URL) as client:
            shown = await client.get(f"{API}/approvals/{batch.id}/images/0", params={"view": "sent"})
            decided = await client.post(
                f"{API}/approvals/{batch.id}", json={"action": "approve"}, headers={"x-latet-ui": "1"}
            )
        assert shown.status_code == 200 and shown.content == base64.b64decode(batch.images[0].original[0])
        assert decided.json() == {"ok": True}
        assert opened_on and not any(opened_on), opened_on  # the image's record and the batch's, off the loop


async def test_a_space_dropped_while_the_budget_closes_another_leaves_the_build_whole(instances, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    olivia, _ = a.identities.add_person("Olivia Brandt")
    async with listening(a, b):
        await b.spaces.runtime(emily)
        await b.spaces.runtime(daniel)
        b.spaces.budget = SPACE_BASE + SPACE_BASE // 2  # room for one: Olivia's build closes Emily's and Daniel's
        close = b.spaces._close

        async def slow(space):
            await asyncio.sleep(0.3)  # Daniel is disabled while Emily's space closes
            await close(space)

        monkeypatch.setattr(b.spaces, "_close", slow)
        building = asyncio.create_task(b.spaces.runtime(olivia))
        await asyncio.sleep(0.05)
        a.identities.disable(daniel.id)
        assert (await building).identity == olivia.ref()
        await until(lambda: set(b.spaces._spaces) == {olivia.id})


# --- what a space is given, on every instance (I1-I4, I6, I8, M2, M7, R-21) -------------------------------------------


async def test_settings_written_back_by_hand_are_weighed_at_the_start_and_at_a_shared_change(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    folder = "/home/emily/oss-site"
    async with listening(a, b):
        here = await a.spaces.runtime(emily)

        def off(current):  # Latet off in one of her folders
            return current.model_copy(update={"projects": [ProjectEntry(path=folder, enabled=False)]})

        def on_again(current):  # on again there, and a term of hers
            return _add_term("Mercury Labs")(current.model_copy(update={"projects": []}))

        await asyncio.to_thread(here.update_config, off)
        with a.db.connection() as conn:
            row = conn.execute("SELECT data FROM settings WHERE owner = %s AND kind = 'own'", (emily.id,)).fetchone()
        assert row is not None
        older = row[0]
        await asyncio.to_thread(here.update_config, on_again)
        approved = await asyncio.to_thread(here.reviewed)
        assert approved is not None and "Mercury Labs" in [t.value for t in approved.terms]
        with a.db.connection() as conn:  # the older sealed row, written back by whoever writes the database
            conn.execute("UPDATE settings SET data = %s WHERE owner = %s AND kind = 'own'", (older, emily.id))
        there = await b.spaces.runtime(emily)  # built afterwards: it weighs what it reads
        assert "Mercury Labs" in _terms(there) and not there.config.projects
        waiting = list(there.mediator.approvals.changes)
        assert waiting and all(c.loosened for c in there.mediator.approvals.waiting_changes())  # it waits for her
        assert await asyncio.to_thread(there.reviewed) == approved
        await b.spaces.drop(emily.id)
        again = await b.spaces.runtime(emily)  # built again (and on any instance): the change waiting is left as it is
        assert list(again.mediator.approvals.changes) == waiting and "Mercury Labs" in _terms(again)
        there = again
        a.shared.set(_gateways("falcon"), actor="admin:a_first00000")
        for runtime in (here, there):  # an open space weighs what it reads again too
            await until(lambda runtime=runtime: "falcon" in _destinations(runtime))
            assert "Mercury Labs" in _terms(runtime) and not runtime.config.projects
        kept = await asyncio.to_thread(here.reviewed)
        assert kept is not None and "Mercury Labs" in [t.value for t in kept.terms] and not kept.projects


async def test_a_space_built_after_a_shared_change_holds_what_it_adds_and_keeps_what_it_removes(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    a.shared.set(_gateways("atlas"), actor=ADMIN)
    async with listening(a, b):
        await a.spaces.runtime(emily)  # her snapshot: atlas
        await a.spaces.drop(emily.id)
        witness = await b.spaces.runtime(daniel)
        a.shared.set(_gateways("falcon"), actor=ADMIN)  # one save
        # B has heard it: her space misses it, as any built later.
        await until(lambda: "falcon" in _destinations(witness))
        there = await b.spaces.runtime(emily)  # built afterwards, on another instance
        assert {"atlas", "falcon"} <= set(_destinations(there))
        assert list(there.mediator.approvals.changes)  # atlas's removal waits for her
        await b.check()
        await b.check()
        await asyncio.sleep(0.3)
        assert {"atlas", "falcon"} <= set(_destinations(there))


async def test_a_space_built_after_the_reviewer_moved_reaches_it_where_it_is_now(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    old, new = "https://reviewer-old.acme.lan/v1", "https://reviewer-new.acme.lan/v1"
    a.shared.set(Config(llm_review=LlmReview(enabled=True, url=old, remote=True)), actor=ADMIN)
    async with listening(a, b):
        await a.spaces.runtime(emily)
        await a.spaces.drop(emily.id)
        a.shared.set(Config(llm_review=LlmReview(enabled=True, url=new, remote=True)), actor=ADMIN)
        there = await b.spaces.runtime(emily)
        assert there.config.llm_review.url == new and there.mediator.llm_review.url == new  # never the retired one
        assert not list(there.mediator.approvals.changes)  # where the reviewer runs is the server's, not hers


async def test_a_value_the_administrator_shares_in_clear_waits_for_each_person(instances):
    a, b = instances
    share_rules(a, b)
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        # In one save: a value no longer masked (a loosening), and a term to mask (a tightening).
        a.shared.set(Config(allow_values=[CANARY], terms=[TermConfig(value="Project Falcon")]), actor=ADMIN)
        for runtime in (here, there):
            await until(lambda runtime=runtime: "Project Falcon" in _terms(runtime))  # the term applies at once
        await until(lambda: bool(list(there.mediator.approvals.changes)))  # the value waits for her
        await asyncio.sleep(0.3)
        for runtime in (here, there):
            assert CANARY not in await _leaves(runtime, f"write to {CANARY}")
        approved = await asyncio.to_thread(there.reviewed)
        assert approved is not None and CANARY not in approved.allow_values
        assert "Project Falcon" in [t.value for t in approved.terms]  # what applied is what is approved
        await b.spaces.drop(emily.id)
        assert CANARY not in await _leaves(await b.spaces.runtime(emily), f"write to {CANARY}")  # built after


@pytest.mark.parametrize(
    ("option", "form"), [("case_sensitive", "ACME CORP"), ("whole_word", "Acme Corp")], ids=["case", "whole-word"]
)
async def test_a_shared_term_made_to_match_fewer_forms_waits_for_each_person(server, option, form):
    """The administrator turns on a shared term's ``case_sensitive`` or ``whole_word``: it matches fewer forms ("ACME
    CORP", "Acme Corporation"), which lowers protection, so it waits for each person: her space keeps the term she
    approved, and those forms stay masked meanwhile."""
    share_rules(server)
    emily, _ = server.identities.add_person("Emily Carter")
    term = TermConfig(value="Acme Corp", type="CLIENT", whole_word=option != "whole_word")
    server.shared.set(Config(terms=[term]), actor=ADMIN)
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        server.shared.set(Config(terms=[term.model_copy(update={option: True})]), actor=ADMIN)
        await until(lambda: bool(runtime.mediator.approvals.waiting_changes()), seconds=30)
        [change] = runtime.mediator.approvals.waiting_changes()
        assert change.loosened == ["terms: 1 matches fewer forms"]
        assert runtime.config.terms == [term]  # the term she approved
        text = "the ACME CORP contract" if option == "case_sensitive" else "the Acme Corporation contract"
        assert form not in await _leaves(runtime, text)


async def test_a_shared_row_written_back_by_hand_with_its_count_bumped_leaves_nothing_in_clear(instances):
    a, b = instances
    a.shared.set(Config(ocr="off"), actor=ADMIN)  # once, long ago: images sent with the text in them unread
    with a.db.connection() as conn:
        older = conn.execute("SELECT data FROM settings WHERE owner = '' AND kind = 'shared'").fetchone()[0]
    a.shared.set(Config(), actor=ADMIN)  # the administrator took it back
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        with a.db.connection() as conn:  # whoever writes PostgreSQL alone
            conn.execute("UPDATE settings SET data = %s WHERE owner = '' AND kind = 'shared'", (older,))
            conn.execute("UPDATE change_counters SET counter = counter + 1 WHERE owner = ''")
        for _ in range(2):
            await a.check()
            await b.check()
        await until(lambda: bool(list(here.mediator.approvals.changes)))
        await asyncio.sleep(0.3)
        for runtime in (here, there):
            assert runtime.config.ocr == "auto"
        approved = await asyncio.to_thread(here.reviewed)
        assert approved is not None and approved.ocr == "auto"
        await a.spaces.drop(emily.id)
        assert (await a.spaces.runtime(emily)).config.ocr == "auto"


async def test_a_shared_change_that_lowers_protection_only_in_her_stricter_folder_waits_for_her(instances):
    a, b = instances
    share_rules(a, b)
    emily, _ = a.identities.add_person("Emily Carter")
    payroll = "/home/emily/payroll"
    # A new address each time: one masked once is in her vault, and masked from then on whatever the settings say.
    addresses = (f"10.1.2.{n}" for n in range(3, 99))

    async def private_address_masked(runtime) -> bool:
        address = next(addresses)
        return address not in await _leaves_in(runtime, payroll, f"the payroll database runs at {address}")

    async with listening(a, b):
        here = await a.spaces.runtime(emily)

        def stricter(current):  # her payroll folder at the max profile: private addresses masked there
            return current.model_copy(update={"projects": [ProjectEntry(path=payroll, profile=Profile.MAX)]})

        await asyncio.to_thread(here.update_config, stricter)
        there = await b.spaces.runtime(emily)
        assert await private_address_masked(here) and await private_address_masked(there)
        # The balanced profile's own values, written out: nothing changes at the top, and in her max folder they turn
        # off the masking of private addresses and strict data mode.
        counter = a.shared.set(Config(include_private_ips=False, data="normal"), actor=ADMIN)
        spaces = [a.spaces._spaces[emily.id], b.spaces._spaces[emily.id]]
        await until(lambda: all(space.shared_counts.applied >= counter for space in spaces))  # re-applied on both
        await asyncio.sleep(0.3)
        for runtime in (here, there):
            assert await private_address_masked(runtime)  # "10.1.2.4" never leaves in clear
        waiting = list(there.mediator.approvals.waiting_changes())
        assert waiting and any(r.startswith("project payroll: private IP addresses") for r in waiting[0].loosened)
        approved = await asyncio.to_thread(here.reviewed)
        assert approved is not None and approved.include_private_ips is None and approved.data is None
        await b.spaces.drop(emily.id)
        again = await b.spaces.runtime(emily)  # built afterwards
        assert await private_address_masked(again)
        assert [c.id for c in again.mediator.approvals.waiting_changes()] == [c.id for c in waiting]  # still waits


async def test_a_setting_the_administrator_tightens_while_a_change_waits_applies_at_once(instances):
    a, b = instances
    share_rules(a, b)
    olivia, _ = a.identities.add_person("Olivia Brandt")
    a.shared.set(Config(terms=[TermConfig(value="Project Atlas", type="PROJECT")]), actor=ADMIN)
    async with listening(a, b):
        hers = await b.spaces.runtime(olivia)
        a.shared.set(Config(), actor=ADMIN)  # Atlas removed: a change waits for her
        await until(lambda: bool(list(hers.mediator.approvals.changes)))
        a.shared.set(Config(include_private_ips=True), actor=ADMIN)  # private addresses masked: a tightening
        await until(lambda: hers.config.include_private_ips is True)
        assert "10.1.2.3" not in await _leaves(hers, "the database is at 10.1.2.3")
        assert "Project Atlas" in _terms(hers)  # what waits still waits
        await b.spaces.drop(olivia.id)
        again = await b.spaces.runtime(olivia)
        assert "10.1.2.3" not in await _leaves(again, "the database is at 10.1.2.3")


async def test_a_save_of_hers_while_a_shared_removal_waits_leaves_the_removal_hers_to_approve(server):
    """C-17: a save of hers made over the composition (a removed shared term kept while it waits) is laid back on her
    own settings: the composition alone never becomes her own part, and the removal is asked again, hers to approve,
    the term masked meanwhile."""
    share_rules(server)
    emily, _ = server.identities.add_person("Emily Carter")
    atlas, orion = TermConfig(value="Project Atlas", type="PROJECT"), TermConfig(value="Project Orion", type="PROJECT")
    server.shared.set(Config(terms=[atlas, orion]), actor=ADMIN)
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        server.shared.set(Config(terms=[atlas]), actor=ADMIN)  # Orion removed: the removal waits for her
        await until(lambda: bool(list(runtime.mediator.approvals.changes)), seconds=30)
        assert "Project Orion" in _terms(runtime)  # masked until she decides
        await asyncio.to_thread(runtime.update_config, _add_term("Mercury Labs"))  # an unrelated save of hers
        own = await asyncio.to_thread(runtime.settings._own)
        assert [t["value"] for t in own.get("terms", [])] == ["Mercury Labs"]  # not the term the composition kept
        assert {"Project Orion", "Mercury Labs"} <= set(_terms(runtime))  # in force: never looser meanwhile
        # Asked again over what is kept: a change her approval applies, not one gone stale. Each read by its id, as a
        # dashboard reads them (``waiting_changes``): the one asked again supersedes the other between two reads.
        await until(
            lambda: any(c.base == digest(runtime.config) for c in runtime.mediator.approvals.waiting_changes()),
            seconds=30,
        )
        [change] = [c for c in runtime.mediator.approvals.waiting_changes() if c.base == digest(runtime.config)]
        assert await asyncio.to_thread(runtime.decide_change, change.id, True) == "approved"
        assert "Project Orion" not in _terms(runtime) and {"Project Atlas", "Mercury Labs"} <= set(_terms(runtime))


async def test_her_rejection_of_a_shared_removal_keeps_the_term_as_her_own(server):
    """C-17: the composition saved as it is (her rejection) keeps what it holds: the term the shared settings dropped
    becomes hers."""
    share_rules(server)
    emily, _ = server.identities.add_person("Emily Carter")
    atlas, orion = TermConfig(value="Project Atlas", type="PROJECT"), TermConfig(value="Project Orion", type="PROJECT")
    server.shared.set(Config(terms=[atlas, orion]), actor=ADMIN)
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        server.shared.set(Config(terms=[atlas]), actor=ADMIN)
        await until(lambda: bool(list(runtime.mediator.approvals.changes)), seconds=30)
        [change] = list(runtime.mediator.approvals.waiting_changes())
        assert await asyncio.to_thread(runtime.decide_change, change.id, False) == "rejected"
        own = await asyncio.to_thread(runtime.settings._own)
        assert [t["value"] for t in own.get("terms", [])] == ["Project Orion"] and "Project Orion" in _terms(runtime)


async def test_an_api_keys_approved_settings_are_laid_over_the_shared_settings_its_settings_were(server):
    """C-20: a copy of the shared settings read between a load and the reading of the approved settings (a forget in
    between) adds nothing to the key's approved own part."""
    share_rules(server)
    key, _ = server.identities.add_key("Billing CI")
    server.shared.set(Config(terms=[TermConfig(value="Project Atlas")]), actor=ADMIN)
    server.listening.set()  # no ear: no re-application reads the settings again meanwhile
    try:
        runtime = await server.spaces.runtime(key)
        await asyncio.to_thread(runtime.update_config, _add_term("Mercury Labs"))  # the administration's save
        settings = runtime.settings
        loaded = await asyncio.to_thread(settings.load)  # laid over the shared settings of now
        venus = Config(terms=[TermConfig(value="Project Atlas"), TermConfig(value="Project Venus")])
        await asyncio.to_thread(SharedSettings(server.db, server.bus, server.master).set, venus, actor=ADMIN)
        server.shared.forget()  # this instance's copy read again: it holds Venus
        approved = await asyncio.to_thread(settings.reviewed)
        assert approved is not None and approved.terms == loaded.terms  # over what the load was laid over
    finally:
        await server.spaces.close()


# --- an API key's settings: approved by the administration, its shared part never weighed (Important 2) -------------


async def test_an_api_keys_space_takes_every_shared_change_and_approves_it(instances):
    a, b = instances
    share_rules(a, b)
    key, _ = a.identities.add_key("Billing CI")
    a.shared.set(Config(terms=[TermConfig(value="Project Atlas", type="PROJECT")]), actor=ADMIN)
    async with listening(a, b):
        here = await a.spaces.runtime(key)
        await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))  # the administration's save
        for shared, expected in (
            (Config(), {"Mercury Labs"}),  # a shared loosening
            (Config(terms=[TermConfig(value="Project Falcon")]), {"Mercury Labs", "Project Falcon"}),  # a tightening
        ):
            a.shared.set(shared, actor=ADMIN)
            await until(lambda expected=expected: set(_terms(here)) == expected)
            built = await b.spaces.runtime(key)  # built after it, on another instance
            assert set(_terms(built)) == expected
            for runtime in (here, built):
                assert not list(runtime.mediator.approvals.changes)
                assert await asyncio.to_thread(runtime.reviewed) == await asyncio.to_thread(runtime.settings.load)
            await b.spaces.drop(key.id)
        await a.spaces.drop(key.id)
        emily, _ = a.identities.add_person("Emily Carter")
        witness = await b.spaces.runtime(emily)
        vega = Config(terms=[TermConfig(value="Project Vega")])  # Falcon out (a loosening), Vega in: in one save
        a.shared.set(vega, actor=ADMIN)  # while no space of the key is open anywhere
        await until(lambda: "Project Vega" in _terms(witness))  # B has heard it
        later = await b.spaces.runtime(key)
        assert set(_terms(later)) == {"Mercury Labs", "Project Vega"} and not list(later.mediator.approvals.changes)
        assert await asyncio.to_thread(later.reviewed) == await asyncio.to_thread(later.settings.load)


def _snapshot_row(db, identity_id: str) -> bytes:
    """The identity's approved snapshot as PostgreSQL keeps it, sealed: written again, its bytes change."""
    with db.connection() as conn:
        row = conn.execute(
            "SELECT data FROM settings WHERE owner = %s AND kind = 'reviewed'", (identity_id,)
        ).fetchone()
    assert row is not None
    return bytes(row[0])


async def test_an_api_keys_own_settings_written_back_by_hand_wait_whatever_the_shared_changes(instances):
    a, b = instances
    share_rules(a, b)
    key, _ = a.identities.add_key("Billing CI")
    a.shared.set(Config(terms=[TermConfig(value="Project Atlas")]), actor=ADMIN)
    async with listening(a, b):
        here = await a.spaces.runtime(key)
        await asyncio.to_thread(_as_the_administration, here, _add_term("Mercury Labs"))
        approved, snapshot = await asyncio.to_thread(here.reviewed), _snapshot_row(a.db, key.id)
        with a.db.connection() as conn:  # whoever writes PostgreSQL alone: the older own part (none), a count bumped
            conn.execute("DELETE FROM settings WHERE owner = %s AND kind = 'own'", (key.id,))
            conn.execute("UPDATE change_counters SET counter = counter + 1 WHERE owner = ''")
        for _ in range(2):
            await a.check()
        await until(lambda: bool(list(here.mediator.approvals.changes)))
        assert "Mercury Labs" in _terms(here)
        assert await asyncio.to_thread(here.reviewed) == approved  # the snapshot as the administration left it
        a.shared.set(Config(terms=[TermConfig(value="Project Falcon")]), actor=ADMIN)  # a shared change, by message
        await until(lambda: "Project Falcon" in _terms(here))
        # Asked again with the new settings: the change before it is settled, then the new one opened, a moment later.
        await until(lambda: bool(list(here.mediator.approvals.changes)))
        assert "Mercury Labs" in _terms(here)
        there = await b.spaces.runtime(key)  # built after the rollback
        assert {"Mercury Labs", "Project Falcon"} <= set(_terms(there)) and list(there.mediator.approvals.changes)
        assert _snapshot_row(a.db, key.id) == snapshot  # never written again: what waits was never approved


async def test_an_api_keys_approved_snapshot_holds_its_own_part_alone(server):
    share_rules(server)
    key, _ = server.identities.add_key("Billing CI")
    server.shared.set(Config(terms=[TermConfig(value="Project Falcon")]), actor=ADMIN)
    async with listening(server):
        runtime = await server.spaces.runtime(key)
        await asyncio.to_thread(runtime.update_config, _add_term("Mercury Labs"))
        with server.db.connection() as conn:
            row = conn.execute("SELECT data FROM settings WHERE owner = %s AND kind = 'reviewed'", (key.id,)).fetchone()
        assert row is not None
        sealer = Sealer(root_key(server.db, server.master, key.id), b"settings")
        own = sealer.open_json(row[0], f"{key.id}:reviewed")
        assert [t["value"] for t in own["terms"]] == ["Mercury Labs"]  # its own part: no shared term in it
        assert "profile" not in own  # nor a value of the shared settings or a default


async def test_an_api_keys_approval_is_worked_out_against_the_shared_settings_it_was_laid_over(server):
    share_rules(server)
    key, _ = server.identities.add_key("Billing CI")
    server.shared.set(Config(terms=[TermConfig(value="Project Atlas")]), actor=ADMIN)
    async with listening(server):
        runtime = await server.spaces.runtime(key)
        sealer = Sealer(root_key(server.db, server.master, key.id), b"settings")

        def save_while_atlas_goes():  # the administration's save, the shared Atlas dropped between apply and approval
            with runtime._lock:
                runtime.update_config(_add_term("Mercury Labs"))  # laid over the shared settings that hold Atlas
                server.shared.set(Config(), actor=ADMIN)
                server.shared.forget()
                runtime.settings.save_reviewed(runtime.config)

        await asyncio.to_thread(save_while_atlas_goes)
        own = sealer.open_json(_snapshot_row(server.db, key.id), f"{key.id}:reviewed")
        assert [t["value"] for t in own["terms"]] == ["Mercury Labs"]  # Atlas never became the key's own
        await until(lambda: _terms(runtime) == ["Mercury Labs"])  # and its removal reached the key's space


def _as_the_administration(runtime, change) -> None:
    """What the administration's save of a key's settings does (Task 15's ``PUT identities/{id}/settings``): applied
    through its runtime and approved (``save_reviewed``), under the runtime's lock."""
    with runtime._lock:
        runtime.update_config(change)
        runtime.settings.save_reviewed(runtime.config)


async def test_the_administrations_save_of_a_key_applies_and_settles_what_waited(server):
    key, _ = server.identities.add_key("Billing CI")
    async with listening(server):
        runtime = await server.spaces.runtime(key)
        await asyncio.to_thread(_as_the_administration, runtime, _add_term("Mercury Labs"))
        with server.db.connection() as conn:  # its own part rolled back by hand, its count bumped: a re-application
            conn.execute("DELETE FROM settings WHERE owner = %s AND kind = 'own'", (key.id,))
            conn.execute("UPDATE change_counters SET counter = counter + 1 WHERE owner = %s", (f"{key.id}:settings",))
        for _ in range(2):
            await server.check()
        await until(lambda: bool(list(runtime.mediator.approvals.changes)))  # it waits, for the administration alone
        [waiting] = list(runtime.mediator.approvals.changes)
        await asyncio.to_thread(_as_the_administration, runtime, _add_term("Venus Corp"))  # the administration saves
        assert {"Mercury Labs", "Venus Corp"} <= set(_terms(runtime))
        assert not list(runtime.mediator.approvals.changes)
        assert runtime.mediator.approvals.settled[waiting] == "superseded"
        await server.spaces.drop(key.id)
        again = await server.spaces.runtime(key)  # built after: nothing waits
        assert {"Mercury Labs", "Venus Corp"} <= set(_terms(again)) and not list(again.mediator.approvals.changes)


async def test_a_keys_change_opened_after_the_administrations_save_is_settled_at_its_next_reapplication(server):
    key, _ = server.identities.add_key("Billing CI")
    async with listening(server):
        runtime = await server.spaces.runtime(key)
        await asyncio.to_thread(_as_the_administration, runtime, _add_term("Mercury Labs"))
        rollback = runtime.config.model_copy(update={"terms": []})  # what a re-application elsewhere opened, late
        change = await asyncio.to_thread(runtime.propose, rollback, "file")
        assert change is not None
        server.shared.set(Config(history="metadata"), actor=ADMIN)  # its next re-application
        await until(lambda: not list(runtime.mediator.approvals.changes))  # nobody but the administration could
        assert runtime.mediator.approvals.settled[change.id] == "superseded"


async def test_a_change_settled_on_another_instance_is_no_longer_there_to_supersede(instances, monkeypatch):
    from latet.server.queue import PgChanges

    a, b = instances
    key, _ = a.identities.add_key("Billing CI")
    async with listening(a, b):
        here, there = await a.spaces.runtime(key), await b.spaces.runtime(key)
        await asyncio.to_thread(_as_the_administration, here, _add_term("Mercury Labs"))
        await until(lambda: "Mercury Labs" in _terms(there))
        older = await asyncio.to_thread(here.propose, here.config.model_copy(update={"terms": []}), "file")
        assert older is not None
        theirs, read, doomed = there.mediator.approvals.changes, PgChanges.__getitem__, {older.id}

        def settled_meanwhile(self, change_id):  # B has listed the change waiting; A settles it before B reads it
            if self is theirs and change_id in doomed:
                doomed.discard(change_id)
                here.mediator.approvals.settle_change(change_id, "superseded")
            return read(self, change_id)

        monkeypatch.setattr(PgChanges, "__getitem__", settled_meanwhile)
        rollback = there.config.model_copy(update={"terms": [], "allow_values": [CANARY]})
        newer = await asyncio.to_thread(there.propose, rollback, "file")  # as a re-application on B does
        assert newer is not None and list(there.mediator.approvals.changes) == [newer.id]
        assert there.mediator.approvals.settled[older.id] == "superseded"  # by A, and nothing raised on B
        doomed.add(newer.id)
        assert there.mediator.approvals.pending_changes() == []  # read while A settles it: no longer listed


async def test_a_space_being_built_for_an_identity_disabled_meanwhile_is_not_kept(instances, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        built, go = _hold_build(b.spaces, monkeypatch)
        building = asyncio.create_task(b.spaces.runtime(emily))
        await until(built.is_set)
        a.identities.disable(emily.id)
        # Heard while it is built (its first snapshot, which the build announces itself, is heard there too).
        await until(lambda: ("drop", {}) in b.spaces._missed.get(emily.id, []))
        go.set()
        with pytest.raises(SpaceUnavailable):  # refused: the gate answers 503, and its client tries again
            await building
        assert b.spaces.count() == 0


async def test_a_space_being_built_when_the_spaces_close_is_not_kept(server, monkeypatch):
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        built, go = _hold_build(server.spaces, monkeypatch)
        building = asyncio.create_task(server.spaces.runtime(emily))
        await until(built.is_set)
        await server.spaces.close()
        go.set()
        with pytest.raises(SpaceUnavailable):
            await building
        assert server.spaces.count() == 0


async def test_a_space_is_built_for_an_identity_as_the_database_has_it(server):
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        assert server.identities.get(emily.id) is not None  # this instance keeps her, enabled, for a few seconds
        with server.db.connection() as conn:  # disabled with no message that would make it forget
            conn.execute("UPDATE identities SET disabled = TRUE WHERE id = %s", (emily.id,))
        with pytest.raises(LookupError):
            await server.spaces.runtime(emily)
        assert server.spaces.count() == 0


async def test_a_change_one_space_cannot_apply_closes_that_space_alone(instances, monkeypatch):
    a, b = instances
    people = [a.identities.add_person(name)[0] for name in ("Emily Carter", "Daniel Okafor", "Olivia Brandt")]
    emily = people[0]
    async with listening(a, b):
        spaces = [await b.spaces.runtime(person) for person in people]
        settings = spaces[0].settings
        load = settings.load
        failed = []

        def unreadable():
            if not failed:
                failed.append(True)
                raise OSError(
                    "the settings of u_emilycarte could not be read or kept in the database (OperationalError)"
                )
            return load()

        monkeypatch.setattr(settings, "load", unreadable)
        a.shared.set(_gateways("falcon"), actor="admin:a_first00000")
        for runtime in spaces[1:]:
            await until(lambda runtime=runtime: "falcon" in _destinations(runtime))
        await until(lambda: emily.id not in b.spaces._spaces)  # closed: it cannot run without the change
        assert "falcon" in _destinations(await b.spaces.runtime(emily))  # made again from PostgreSQL


async def test_what_a_space_missed_while_built_is_applied_before_anyone_gets_it(instances, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here = await a.spaces.runtime(emily)
        built, go = threading.Event(), threading.Event()
        loading, loaded = threading.Event(), threading.Event()
        build = b.spaces._build

        def slow(identity):
            found = build(identity)
            settings = found.runtime.settings
            load = settings.load

            def held():  # the first load of what the space missed, held
                if not loading.is_set():
                    loading.set()
                    loaded.wait(10)
                return load()

            monkeypatch.setattr(settings, "load", held)
            built.set()
            go.wait(10)
            return found

        monkeypatch.setattr(b.spaces, "_build", slow)
        first = asyncio.create_task(b.spaces.runtime(emily))
        await until(built.is_set)
        await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))
        await until(lambda: bool(b.spaces._missed.get(emily.id)))
        go.set()
        await until(loading.is_set)
        await asyncio.to_thread(here.update_config, _add_term("Venus Corp"))  # heard while what it missed is applied
        second = asyncio.create_task(b.spaces.runtime(emily))
        await asyncio.sleep(0.2)
        assert not second.done() and b.spaces.count() == 0  # nobody has it before what it missed is applied
        loaded.set()
        for runtime in await asyncio.gather(first, second):
            assert {"Mercury Labs", "Venus Corp"} <= set(_terms(runtime))


async def test_a_save_made_while_a_space_reads_a_change_lands_on_top_of_it(instances, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        settings = there.settings
        load = settings.load
        loading, loaded = threading.Event(), threading.Event()

        def held():  # B has read A's change (the version it names from now on), and has not applied it yet
            found = load()
            if not loading.is_set():
                loading.set()
                loaded.wait(10)
            return found

        monkeypatch.setattr(settings, "load", held)
        await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))
        await until(loading.is_set)
        saving = asyncio.create_task(asyncio.to_thread(there.update_config, _add_term("Venus Corp")))
        await asyncio.sleep(0.2)
        loaded.set()
        await saving  # it waited for what B was applying, and lands on top of it
        kept = await asyncio.to_thread(load)
        assert {"Mercury Labs", "Venus Corp"} <= {t.value for t in kept.terms}  # never the second term alone
        assert {"Mercury Labs", "Venus Corp"} <= set(_terms(there))


async def test_no_space_is_built_or_handed_out_while_the_instance_does_not_listen(instances, monkeypatch):
    from latet.server import app as app_module

    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    with pytest.raises(NotListening):
        await b.spaces.runtime(emily)
    monkeypatch.setattr(app_module, "RETRY_SECONDS", 3.0)
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        b.silenced()  # B's ear: what a lost connection does, and three seconds before it listens again
        await until(lambda: not b.listening.is_set())
        with pytest.raises(NotListening):
            await b.spaces.runtime(emily)
        await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))  # published while B does not hear
        await until(b.listening.is_set, seconds=10)
        again = await b.spaces.runtime(emily)
        assert again is not there and "Mercury Labs" in _terms(again)


async def test_a_space_too_far_behind_its_changes_is_closed_and_made_again(instances, monkeypatch):
    from latet.server import spaces as spaces_module

    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        there = await b.spaces.runtime(emily)
        settings = there.settings
        load = settings.load
        loading, loaded = threading.Event(), threading.Event()

        def held():  # its first change held: what follows waits behind it
            if not loading.is_set():
                loading.set()
                loaded.wait(10)
            return load()

        monkeypatch.setattr(settings, "load", held)
        monkeypatch.setattr(spaces_module, "QUEUE_LIMIT", 2)
        for n in range(4):
            a.shared.set(Config(retention_days=10 + n), actor="admin:a_first00000")
        await until(lambda: emily.id not in b.spaces._spaces)  # past the limit: closed, never piling up
        loaded.set()
        again = await b.spaces.runtime(emily)
        assert again is not there and again.config.retention_days == 13  # made again from PostgreSQL


async def test_one_spaces_slow_change_holds_no_other_identitys_messages(instances, monkeypatch):
    from latet.server import spaces as spaces_module

    a, b = instances
    names = ("Emily Carter", "Daniel Okafor", "Olivia Brandt", "Noah Fischer", "Lena Rossi", "Omar Haddad")
    people = [a.identities.add_person(name)[0] for name in names]
    emily, daniel = people[0], people[1]
    async with listening(a, b):
        for person in people:
            await b.spaces.runtime(person)
        reapply = spaces_module._reapply

        def slow(space, **kwargs):
            time.sleep(1.0)
            reapply(space, **kwargs)

        monkeypatch.setattr(spaces_module, "_reapply", slow)
        a.shared.set(Config(history="metadata"), actor="admin:a_first00000")
        await asyncio.sleep(0.1)
        disabled = time.monotonic()
        a.identities.disable(daniel.id)
        await until(lambda: daniel.id not in b.spaces._spaces)
        assert time.monotonic() - disabled < 0.5  # the disable is not queued behind six slow re-applications
        monkeypatch.setattr(spaces_module, "_reapply", reapply)
        emilys = b.spaces._spaces[emily.id].runtime
        olivia = people[2]
        olivias = await a.spaces.runtime(olivia)
        with emilys._lock:  # a save of Emily's dashboard holds her space's lock
            await asyncio.to_thread(olivias.update_config, _add_term("Mercury Labs"))
            await until(lambda: "Mercury Labs" in _terms(b.spaces._spaces[olivia.id].runtime))


async def test_a_change_made_by_a_closed_space_reaches_the_space_made_again_on_its_instance(instances, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        there = await b.spaces.runtime(emily)
        settings = there.settings
        save = settings.save
        saving, go = threading.Event(), threading.Event()

        def held(config):  # a save of her dashboard on B, still under way when B closes her space
            saving.set()
            go.wait(10)
            save(config)

        monkeypatch.setattr(settings, "save", held)
        changing = asyncio.create_task(asyncio.to_thread(there.update_config, _add_term("Mercury Labs")))
        await until(saving.is_set)
        await b.spaces.drop(emily.id, wait=False)
        again = await b.spaces.runtime(emily)
        assert again is not there and "Mercury Labs" not in _terms(again)
        go.set()
        await changing
        await until(lambda: "Mercury Labs" in _terms(again))  # told from the space that made it, not its instance


async def test_a_space_and_its_runtime_print_nothing_of_the_identitys_settings(server):
    emily, _ = server.identities.add_person("Emily Carter", "emily.carter@acme.com")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        await asyncio.to_thread(runtime.update_config, _add_term("Mercury Labs"))
        space = server.spaces._spaces[emily.id]
        for shown in (repr(space), repr(space.runtime), str(space)):
            assert emily.id in shown
            assert "Emily" not in shown and "emily.carter" not in shown and "Mercury" not in shown


async def test_closing_spaces_holds_the_event_loop_for_no_one(server, monkeypatch):
    people = [server.identities.add_person(name)[0] for name in ("Emily Carter", "Daniel Okafor", "Olivia Brandt")]
    async with listening(server):
        for person in people:
            runtime = await server.spaces.runtime(person)
            recorder = runtime.recorder
            assert recorder is not None
            monkeypatch.setattr(recorder, "_prune", lambda: time.sleep(1.0))  # a recording that takes a second
            recorder.prune_now()
            close = runtime.close

            def slow(close=close):  # and its runtime two seconds and a half to close (its threads end)
                time.sleep(2.5)
                close()

            monkeypatch.setattr(runtime, "close", slow)
        gaps: list[float] = []
        ticking = True

        async def tick():
            last = time.monotonic()
            while ticking:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(tick())
        began = time.monotonic()
        await server.spaces.close()
        took = time.monotonic() - began
        ticking = False
        await ticker
        assert server.spaces.count() == 0
        assert took < 6.0  # side by side (three seconds and a half), not one after the other (eight and a half)
        assert max(gaps) < 0.25  # the event loop went on meanwhile


async def test_a_rename_updates_the_open_space_and_leaves_its_waits(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        there = await b.spaces.runtime(emily)
        batch = await there.mediator.approvals.open(Batch("cc:s1", None, "claude-code", [], polled=True))
        a.identities.update(emily.id, name="Emily Brown")
        await until(lambda: there.identity is not None and there.identity.name == "Emily Brown")
        assert await b.spaces.runtime(emily) is there and b.spaces._spaces[emily.id].identity.name == "Emily Brown"
        assert not batch.future.done()  # her decision is still awaited


async def test_a_forgotten_heard_while_postgresql_holds_the_value_forgets_nothing(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        entity = here.built.vault.intern("PASSWORD", EntityClass.SECRET, "hunter2-correct-horse")
        await until(lambda: there.built.vault.get(entity.id) == entity)
        # Out of order, or published again within its minute to an instance that just started: kept in PostgreSQL.
        a.bus.publish(f"identity:{emily.id}", {"kind": "forgotten", "id": entity.id, "from": "elsewhere", "counter": 1})
        a.bus.publish(f"identity:{emily.id}", {"kind": "rule", "id": "e" * 20, "from": "elsewhere", "counter": 2})
        await until(lambda: b.spaces._spaces[emily.id].queue.empty())
        await asyncio.sleep(0.3)
        assert there.built.vault.get(entity.id) == entity  # still masked as it is


async def test_a_value_a_conversation_loads_that_puts_another_out_has_the_engine_forget_its_blocks(instances,
                                                                                                   monkeypatch):  # fmt: skip
    """C-15, T-11 (R-21 through the conversations' hook): a value loaded for a conversation whose fake was given anew
    puts the old value out, the vault's count follows, and the engine forgets the masked blocks that hold the fake."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    a.listening.set()  # no ear: nothing is heard, and the instances count as listening
    b.listening.set()
    here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
    try:
        old = await asyncio.to_thread(here.built.vault.intern, "PASSWORD", EntityClass.SECRET, "hunter2-correct-horse")
        await asyncio.to_thread(lambda: there.built.vault.learn(old))  # B knows it, as from a message
        with a.db.connection() as conn:  # forgotten, with no message B hears
            conn.execute("DELETE FROM vault_strings WHERE identity_id = %s AND entity_id = %s", (emily.id, old.id))
            conn.execute("DELETE FROM vault_entities WHERE identity_id = %s AND id = %s", (emily.id, old.id))
        value = "tr0ub4dor-and-3"
        new = Entity(here.built.vault.entity_id("PASSWORD", value), "PASSWORD", EntityClass.SECRET, value,
                     old.surrogate, 0, time.time())  # fmt: skip
        assert await asyncio.to_thread(here.store.save_entity, new) is None  # its fake, given to another value
        forgot = []
        rules_changed = there.built.engine.rules_changed
        monkeypatch.setattr(there.built.engine, "rules_changed", lambda: forgot.append(True) or rules_changed())
        conversations = here.built.engine.conversations
        session = conversations.session("cc:s1")
        session.add({new.id})
        conversations.update(session)
        await asyncio.to_thread(there.built.engine.conversations.session, "cc:s1")  # loaded for the conversation
        assert there.built.vault.get(new.id) == new and there.built.vault.get(old.id) is None
        assert there.built.vault.by_surrogate(old.surrogate) == new  # the fake restores the new value alone
        assert forgot  # the blocks masked with the old value's fake are masked again
        store = b.spaces._spaces[emily.id].parts.vault
        assert store.count == there.built.vault.count + store.unreadable
    finally:
        await a.spaces.close()
        await b.spaces.close()


async def _shown_elsewhere(a, b, emily):
    """Emily's spaces on A and B, hearing nothing (the instances count as listening), and a value of hers that a
    conversation was shown on A alone: B has it from no message."""
    a.listening.set()
    b.listening.set()
    here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
    entity = await asyncio.to_thread(here.built.vault.intern, "EMAIL", EntityClass.PII, CANARY)
    conversations = here.built.engine.conversations
    session = await asyncio.to_thread(conversations.session, "cc:s1")
    session.add({entity.id})
    await asyncio.to_thread(conversations.update, session)
    return here, there, entity


async def test_a_value_forgotten_here_while_a_conversation_loads_it_is_not_learnt_back(instances, monkeypatch):
    """S17 (the final review's area 2, M-8): what a conversation loads of the values another instance showed it is
    learnt under the runtime's lock, as a value heard of is (``_learn``): one forgotten here meanwhile, out of the vault
    and not yet out of PostgreSQL, is not learnt back, and the store's count stays the vault's."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    try:
        _, there, entity = await _shown_elsewhere(a, b, emily)
        await asyncio.to_thread(lambda: there.built.vault.learn(entity))  # B knows it, as from a message
        store, between, release = b.spaces._spaces[emily.id].parts.vault, threading.Event(), threading.Event()
        forget_entity = store.forget_entity

        def paused(entity_id):  # out of B's vault, not yet out of PostgreSQL
            between.set()
            assert release.wait(10)
            return forget_entity(entity_id)

        monkeypatch.setattr(store, "forget_entity", paused)
        forgetting = asyncio.create_task(asyncio.to_thread(there.forget, entity.id))
        assert await asyncio.to_thread(between.wait, 10)
        loading = asyncio.create_task(asyncio.to_thread(there.built.engine.conversations.session, "cc:s1"))
        await asyncio.sleep(0.3)  # the conversation read meanwhile, in the thread that masks
        release.set()
        assert await forgetting == entity
        await loading
        assert there.built.vault.get(entity.id) is None  # forgotten, not learnt back
        assert await asyncio.to_thread(store.load_entity, entity.id) is None
        assert store.count == there.built.vault.count + store.unreadable
    finally:
        await a.spaces.close()
        await b.spaces.close()


async def test_a_conversation_read_on_the_event_loop_waits_for_no_save_to_learn_its_values(instances):
    """S17: a conversation read on the event loop (the Live Reviewer's decision reads its batch's there; a WebSocket's
    was read there before F-S1) takes the runtime's lock only when it is free: a save holds it for seconds, and the loop
    serves every identity. Held, the values are not learnt then, never learnt without the lock: they are as the
    instance hears of them, or at its next catch-up."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    try:
        _, there, entity = await _shown_elsewhere(a, b, emily)
        held, release = threading.Event(), threading.Event()

        def save() -> None:  # a save of the settings, holding the runtime's lock
            with there._lock:
                held.set()
                release.wait(2)

        holder = threading.Thread(target=save)
        holder.start()
        try:
            assert held.wait(5)
            started = time.monotonic()
            there.built.engine.conversations.session("cc:s1")  # on the event loop
            waited = time.monotonic() - started
            assert there.built.vault.get(entity.id) is None  # not learnt without the lock
        finally:
            release.set()
            holder.join(5)
        assert waited < 0.5, waited
    finally:
        await a.spaces.close()
        await b.spaces.close()


async def test_a_catch_up_reads_the_vault_outside_the_runtimes_lock_and_what_postgresql_holds_now_wins(instances,
                                                                                                    monkeypatch):  # fmt: skip
    """M-09: a vault at its limit took seconds to read under the runtime's lock; and a value read a moment before that
    would put another out is read again first, so that a fake given anew restores the value PostgreSQL holds."""
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    a.listening.set()
    b.listening.set()
    here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
    try:
        old = await asyncio.to_thread(here.built.vault.intern, "PASSWORD", EntityClass.SECRET, "hunter2-correct-horse")
        space = b.spaces._spaces[emily.id]
        store = space.parts.vault
        stale = await asyncio.to_thread(store.load_entities)  # what a reading found, a moment before
        with a.db.connection() as conn:  # then forgotten, and its fake given to another value
            conn.execute("DELETE FROM vault_strings WHERE identity_id = %s AND entity_id = %s", (emily.id, old.id))
            conn.execute("DELETE FROM vault_entities WHERE identity_id = %s AND id = %s", (emily.id, old.id))
        value = "tr0ub4dor-and-3"
        new = Entity(here.built.vault.entity_id("PASSWORD", value), "PASSWORD", EntityClass.SECRET, value,
                     old.surrogate, 0, time.time())  # fmt: skip
        assert await asyncio.to_thread(here.store.save_entity, new) is None
        await asyncio.to_thread(lambda: there.built.vault.learn(new))  # B learnt the new one meanwhile
        free = []

        def tried() -> None:  # in a thread of its own: whether anyone holds the runtime's lock now
            got = there._lock.acquire(blocking=False)
            if got:
                there._lock.release()
            free.append(got)

        def reading():  # the stale reading, and whether the runtime's lock is free while it is made
            probe = threading.Thread(target=tried)
            probe.start()
            probe.join(5)
            return stale

        monkeypatch.setattr(store, "load_entities", reading)
        await asyncio.to_thread(spaces_module._resync, space, b.db)
        assert free == [True]  # read with the lock free: a save, a value forgotten, wait for none of it
        assert there.built.vault.by_surrogate(old.surrogate) == new  # what PostgreSQL holds now
        assert there.built.vault.get(old.id) is None
    finally:
        await a.spaces.close()
        await b.spaces.close()


async def test_a_catch_up_of_a_vault_lets_go_of_the_gil_with_a_pause_that_yields(server, monkeypatch):
    """S10 (open item 4; area 1's Minor, area 4's M-1): what learns a vault's values under the runtime's lock lets go of
    the GIL every ``_YIELD_EVERY`` values with the pause ``engine.known`` takes: one of no time gives the event loop no
    turn on Windows (a 601 ms stall measured)."""
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        for n in range(5):
            await asyncio.to_thread(runtime.built.vault.intern, "PASSWORD", EntityClass.SECRET, f"hunter{n}-Lavender")
        space, pauses = server.spaces._spaces[emily.id], []
        monkeypatch.setattr(spaces_module, "_YIELD_EVERY", 2)
        monkeypatch.setattr(spaces_module, "time", SimpleNamespace(monotonic=time.monotonic, sleep=pauses.append))
        await asyncio.to_thread(spaces_module._resync, space, server.db)
    assert pauses == [LET_GO, LET_GO]  # at the second value and the fourth


async def test_a_fake_given_to_another_value_takes_the_place_of_a_value_forgotten_unheard(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        old = here.built.vault.intern("PASSWORD", EntityClass.SECRET, "hunter2-correct-horse")
        await until(lambda: there.built.vault.get(old.id) == old)
        with a.db.connection() as conn:  # forgotten, with no message B hears
            conn.execute("DELETE FROM vault_strings WHERE identity_id = %s AND entity_id = %s", (emily.id, old.id))
            conn.execute("DELETE FROM vault_entities WHERE identity_id = %s AND id = %s", (emily.id, old.id))
        value = "tr0ub4dor-and-3"
        new = Entity(here.built.vault.entity_id("PASSWORD", value), "PASSWORD", EntityClass.SECRET, value,
                     old.surrogate, 0, time.time())  # fmt: skip
        assert await asyncio.to_thread(here.store.save_entity, new) is None  # its fake, given to another value
        await until(lambda: there.built.vault.get(new.id) == new)
        assert there.built.vault.get(old.id) is None and there.built.vault.by_surrogate(old.surrogate) == new


async def test_a_spaces_history_is_pruned_by_its_recorder_and_not_at_its_start(server, monkeypatch):
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        assert runtime.exchanges is not None and runtime.recorder is not None
        summary = {"ts": time.time() - 30 * 86400, "session": "cc:s1", "agent": "claude-code", "flags": []}
        await asyncio.to_thread(runtime.exchanges.record, ExchangeRecord(summary))
        await server.spaces.drop(emily.id)
        again = await server.spaces.runtime(emily)
        assert again.exchanges is not None and again.recorder is not None
        assert len((await asyncio.to_thread(again.exchanges.list))[0]) == 1  # a space's start prunes nothing
        held = []
        jobs = again.recorder.jobs
        hold = jobs.hold

        def counted(name, seconds):
            held.append(name)
            return hold(name, seconds)

        monkeypatch.setattr(jobs, "hold", counted)
        again.recorder.prune_now()
        await asyncio.to_thread(again.recorder.drain)
        assert held == ["history-prune"]  # under the identity's job lock, one instance at a time
        assert (await asyncio.to_thread(again.exchanges.list))[0] == []


async def test_a_shorter_retention_set_on_one_instance_prunes_on_the_other(instances, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        assert there.recorder is not None and there.exchanges is not None
        pruned = []
        prune_now = there.recorder.prune_now

        def counted():
            pruned.append(True)
            prune_now()

        monkeypatch.setattr(there.recorder, "prune_now", counted)
        shorter = lambda current: current.model_copy(update={"retention_days": 2})  # noqa: E731
        await asyncio.to_thread(here.update_config, shorter)
        await until(lambda: bool(pruned))
        assert there.exchanges.retention_days == 2


# --- what other parts hand to a space (Tasks 9b, 10, 11) -------------------------------------------------------------


async def test_a_conversations_new_folder_said_on_one_instance_reaches_the_others_conversations(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        theirs = await asyncio.to_thread(there.built.engine.conversations.session, "cc:s1")
        theirs.reported = "/home/emily/billing-api"  # a client of hers reported its folder to B
        conversations = here.built.engine.conversations
        mine = await asyncio.to_thread(conversations.session, "cc:s1")
        mine.project_id, mine.cwd = "p_oss", "/home/emily/oss-site"  # another folder, through A
        await asyncio.to_thread(conversations.update, mine)
        await until(lambda: theirs.reported is None)  # what B was told no longer holds


async def test_what_waits_for_a_person_reaches_her_dashboards_and_her_waits_on_every_instance(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        viewer = there.mediator.approvals.broadcaster.subscribe()
        item = {"kind": "tool_output", "path": "", "original": "a note", "masked": "a note", "replacements": []}
        batch = await here.mediator.approvals.open(Batch("cc:s1", None, "claude-code", [item], polled=True))
        pending = await asyncio.wait_for(viewer.get(), 5)  # {"pending"}: the batch read from the queue, on B
        assert pending == ("pending", batch.summary())
        waiting = asyncio.create_task(here.mediator.approvals.decided(batch))
        await asyncio.sleep(0.2)
        assert await there.mediator.approvals.decide(batch.id, True)  # decided on B: {"decided"} ends A's wait at once
        decision = await asyncio.wait_for(waiting, 2)  # sooner than the wait's own reading (every five seconds)
        assert isinstance(decision, Decision) and decision.approved
        settled = await asyncio.wait_for(viewer.get(), 5)  # {"feed"}: an event small enough to travel inline
        assert settled == ("settled", {"id": batch.id, "state": "approved"})
        polled = asyncio.create_task(there.mediator.approvals.answer(batch.id, 5))
        await asyncio.sleep(0.2)
        here.mediator.approvals.give(batch.id, {"ok": True})  # {"answered"}: the plugin's poll on B gets it
        assert await asyncio.wait_for(polled, 2) == {"ok": True}
        here.mediator.approvals.broadcaster.publish({"note": "x" * 100_000}, "notice")  # {"event"}: by reference
        assert await asyncio.wait_for(viewer.get(), 5) == ("notice", {"note": "x" * 100_000})


async def test_a_burst_of_large_batches_reaches_a_viewer_on_another_instance(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        viewer = there.mediator.approvals.broadcaster.subscribe()
        text = "q" * (1536 * 1024)  # each batch about 3 MiB: seven of them weigh more than events kept by reference
        opened = []
        for n in range(7):
            item = {"kind": "tool_output", "path": "", "original": text, "masked": text, "replacements": [], "n": n}
            opened.append(await here.mediator.approvals.open(Batch(f"cc:s{n}", None, "claude-code", [item])))
        heard = [await asyncio.wait_for(viewer.get(), 10) for _ in opened]  # each named, and read from the queue
        assert [event for event, _ in heard] == ["pending"] * 7
        assert [summary["id"] for _, summary in heard] == [batch.id for batch in opened]
        for batch in opened:
            await here.mediator.approvals.decide(batch.id, False)


async def test_closing_a_space_ends_its_waits_and_stops_its_reaper(server):
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        approvals = runtime.mediator.approvals
        batch = await approvals.open(Batch("cc:s1", None, "claude-code", [], polled=True))
        reaper = approvals._reaper
        assert reaper is not None and not reaper.done()
        await server.spaces.drop(emily.id)
        assert batch.future.done() and batch.future.result().state == "abandoned"  # not approved, nothing sent
        await until(reaper.done)


async def test_a_save_refused_as_stale_reapplies_the_space_at_once(instances, monkeypatch):
    from latet.server import spaces as spaces_module

    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    threads: dict[str, int] = {}
    reapply = spaces_module._reapply

    def recorded(space, **kwargs):
        threads.setdefault("reapplied", threading.get_ident())
        return reapply(space, **kwargs)

    monkeypatch.setattr(spaces_module, "_reapply", recorded)
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        changed = _add_term("Mercury Labs")(here.config)
        await asyncio.to_thread(here.settings.save, changed)  # a save alone (what waits for her): told to nobody

        def saving():
            threads["saved"] = threading.get_ident()
            there.update_config(_add_term("Venus Corp"))

        with pytest.raises(OSError, match="changed on another instance"):
            await asyncio.to_thread(saving)
        await until(lambda: "Mercury Labs" in _terms(there))  # read again at once, not at the next beat
        assert threads["reapplied"] != threads["saved"]  # never inside the save, which holds the runtime's lock
        await asyncio.to_thread(there.update_config, _add_term("Venus Corp"))  # her next try passes
        kept = await asyncio.to_thread(there.settings.load)
        assert {"Mercury Labs", "Venus Corp"} <= {t.value for t in kept.terms}


async def test_a_change_left_waiting_by_a_reapply_is_settled_once_its_settings_apply(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        looser = there.config.model_copy(update={"projects": [ProjectEntry(path="/home/emily/oss", enabled=False)]})
        change = await asyncio.to_thread(there.propose, looser, "command")
        assert change is not None and change.id in there.mediator.approvals.changes
        await asyncio.to_thread(here.apply_config, looser)  # the same settings applied, and approved, through A
        await until(lambda: bool(there.config.projects))
        await until(lambda: change.id not in there.mediator.approvals.changes)
        assert there.mediator.approvals.settled[change.id] == "superseded"  # left waiting, it would undo nothing


async def test_a_spaces_app_trusts_the_server_alone(server):
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        await server.spaces.runtime(emily)
        app = server.spaces._spaces[emily.id].app
        paths = [getattr(route, "path", "") for route in getattr(app, "routes", [])]
        assert "/_latet/admin/shutdown" not in paths  # its app was made knowing it is a space of the server
