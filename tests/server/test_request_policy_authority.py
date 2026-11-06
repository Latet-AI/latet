"""Who may approve a request exception on a server, and whose clock its lifetime runs by (P2-6, D3, R-20)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from latet.config import RequestException
from latet.gateway import request_policy, request_profiles
from latet.server.spaces import database_now

pytestmark = pytest.mark.server

TEAM = {
    "destination": "llm-acme",
    "profile": "openai.chat.1",
    "reason": "custom-header",
    "components": [{"kind": "header", "name": "x-team"}],
}


async def test_a_persons_exception_waits_for_her_and_is_stamped_by_postgresqls_clock(server, monkeypatch):
    server.listening.set()  # spaces are built here
    person, _ = server.identities.add_person("Ada", "ada@example.com")
    runtime = await server.spaces.runtime(person)
    # The instance's own clock is a year off: an exception's lifetime never runs by it.
    skewed = datetime.now(UTC) + timedelta(days=365)

    class _Skewed(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return skewed

    monkeypatch.setattr(server, "clock", lambda: skewed.timestamp())
    monkeypatch.setattr("latet.runtime.datetime", _Skewed)
    wanted = runtime.config.model_copy(update={"request_exceptions": [RequestException.model_validate(TEAM)]})
    change = await runtime.mediator.run(runtime.propose, wanted, "command")
    assert change is not None and any("request exception" in reason for reason in change.loosened)
    assert request_policy.in_force(runtime.config, database_now(server.db)) == ()  # waiting: it grants nothing
    before = database_now(server.db)
    assert await runtime.mediator.run(runtime.decide_change, change.id, True) == "approved"
    after = database_now(server.db)
    [row] = runtime.config.request_exceptions
    assert row.approved and row.revision == request_profiles.REVISION and row.issuer == "person"
    assert row.granted is not None and before <= row.granted <= after  # PostgreSQL's time, not the skewed instance's
    assert request_policy.in_force(runtime.config, after) == (row,)
    assert request_policy.in_force(runtime.config, after + timedelta(days=31)) == ()  # expired by that clock


async def test_a_servers_request_exception_is_judged_by_postgresqls_clock(server, monkeypatch):
    """On a server an exception lets through what it names for its identity alone, and its lifetime runs by
    PostgreSQL's clock as each request is decided (D3): a request decided past it is refused naming the exception."""
    import httpx

    from latet.config import Destination
    from latet.gateway.app import create_app

    sent: list[httpx.Headers] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers)
        body = b'{"id":"c","object":"chat.completion","model":"gpt-4.1-mini","choices":[]}'
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(
        "latet.gateway.app.make_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
    )
    server.listening.set()
    person, _ = server.identities.add_person("Grace", "grace@example.com")
    runtime = await server.spaces.runtime(person)
    destination = Destination(name="llm-acme", url="https://llm.acme.example")
    await runtime.mediator.run(runtime.update_config, lambda c: c.model_copy(update={"upstreams": [destination]}))
    wanted = runtime.config.model_copy(update={"request_exceptions": [RequestException.model_validate(TEAM)]})
    change = await runtime.mediator.run(runtime.propose, wanted, "command")
    assert change is not None and await runtime.mediator.run(runtime.decide_change, change.id, True) == "approved"

    async def reached(url: str):  # her own destination, its address judged reachable (no name lookup here)
        return url, None, None

    runtime.gateway.pin = reached
    app = create_app(runtime.gateway)
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "an ordinary question"}]}
    headers = {"authorization": "Bearer k", "x-team": "data-science"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=server.settings.url) as client,
    ):
        assert (await client.post("/llm-acme/v1/chat/completions", json=body, headers=headers)).status_code == 200
        assert sent[-1]["x-team"] == "data-science"
        # PostgreSQL's clock a month on, as RC-REC-3's one statement reads it with the counts: the decision finds it
        # expired, whatever the instance's clock says.
        from latet.server import spaces as spaces_module

        read = spaces_module._counts_and_now

        def later(db, owner):
            now, settings, shared = read(db, owner)
            return now + timedelta(days=31), settings, shared

        monkeypatch.setattr(spaces_module, "_counts_and_now", later)
        refused = await client.post("/llm-acme/v1/chat/completions", json=body, headers=headers)
    [row] = runtime.config.request_exceptions
    assert refused.status_code == 400 and row.id in refused.text and "expired" in refused.text and len(sent) == 1


async def test_approving_a_permanent_exception_writes_an_administration_log_entry(server, monkeypatch):
    """R-108: on a server, an exception made permanent is written to the administration log as it is approved: its id
    and its scope's category, never its description or what it names."""
    written: list[tuple[str, str, object]] = []
    record = server.audit.record

    def kept(actor, action, target=None, details=None, address=None, **more):
        written.append((actor, action, details))
        return record(actor, action, target, details, address, **more)

    monkeypatch.setattr(server.audit, "record", kept)
    server.listening.set()
    person, _ = server.identities.add_person("Hedy", "hedy@example.com")
    runtime = await server.spaces.runtime(person)
    permanent = RequestException.model_validate({**TEAM, "permanent": True, "description": "the acme team's gateway"})
    wanted = runtime.config.model_copy(update={"request_exceptions": [permanent]})
    change = await runtime.mediator.run(runtime.propose, wanted, "command")
    assert change is not None and await runtime.mediator.run(runtime.decide_change, change.id, True) == "approved"
    [row] = runtime.config.request_exceptions
    entries = [(actor, details) for actor, action, details in written if action == "exception.permanent"]
    assert entries == [(person.id, {"exception": row.id, "scope": "identity"})]
    assert all("acme" not in repr(details) and "x-team" not in repr(details) for _, _, details in written)


async def test_an_edition_that_forbids_permanent_exceptions_routes_none(server, monkeypatch):
    server.listening.set()
    monkeypatch.setattr(server.edition, "permanent_exceptions", lambda: False, raising=False)
    person, _ = server.identities.add_person("Ida", "ida@example.com")
    runtime = await server.spaces.runtime(person)
    rows = [RequestException.model_validate({**TEAM, "permanent": True})]
    change = await runtime.mediator.run(
        runtime.propose, runtime.config.model_copy(update={"request_exceptions": rows}), "command"
    )
    assert change is not None and await runtime.mediator.run(runtime.decide_change, change.id, True) == "approved"
    assert runtime.config.request_exceptions[0].approved and runtime.gateway.request_exceptions == ()


def _key_with(server, rows: list[RequestException]):
    """An API key whose settings hold ``rows``, approved (as the administration's save stamps them), and its token."""
    from latet.server.master import root_key
    from latet.server.space_settings import PgSettings

    identity, token = server.identities.add_key("P2-6 fixture key")
    identity_root = root_key(server.db, server.master, identity.id, clock=server.clock)
    storage = PgSettings(server.db, server.shared, identity.id, identity_root, kind="key")
    config = storage.prepare(storage.load().model_copy(update={"request_exceptions": rows}))
    storage.save(config)
    storage.save_reviewed(config)
    return identity, token


def _stamped(**fields) -> RequestException:
    # A minute back: PostgreSQL's clock, which decides, may run a little behind this machine's.
    stamps = {
        "id": "x0123456789",
        "granted": datetime.now(UTC) - timedelta(minutes=1),
        "revision": request_profiles.REVISION,
    }
    return RequestException.model_validate({**TEAM, **stamps, **fields})


async def test_an_api_keys_own_calls_never_approve_its_pending_exceptions(server, monkeypatch):
    """R-2: an API key's exceptions are approved by the administration's save of its settings alone. A save the key's
    own calls make (its computer's identity, a destination its agent names) leaves a pending one pending: never
    stamped, never logged as approved by the administration."""
    written: list[str] = []
    record = server.audit.record

    def kept(actor, action, target=None, details=None, address=None, **more):
        written.append(action)
        return record(actor, action, target, details, address, **more)

    monkeypatch.setattr(server.audit, "record", kept)
    server.listening.set()
    pending = RequestException.model_validate({**TEAM, "permanent": True})  # no stamp: nobody approved it
    identity, _ = _key_with(server, [pending])
    runtime = await server.spaces.runtime(identity)
    assert not runtime.config.request_exceptions[0].approved
    await runtime.mediator.run(runtime.update_config, lambda c: c.model_copy(update={"retention_days": 7}))
    [row] = runtime.config.request_exceptions
    assert not row.approved and row.issuer is None and "exception.permanent" not in written
    assert runtime.gateway.request_exceptions == ()


async def test_an_edition_that_forbids_permanent_exceptions_routes_none_from_a_spaces_first_build(server, monkeypatch):
    """Minor 1: the edition's answer is the space's from its first build: a permanent exception kept from before (a
    restart, another edition) is never routed until the next settings apply, and the index marks it forbidden."""
    from latet.server.space_settings import listed_exceptions

    server.listening.set()
    monkeypatch.setattr(server.edition, "permanent_exceptions", lambda: False, raising=False)
    identity, _ = _key_with(server, [_stamped(permanent=True)])
    runtime = await server.spaces.runtime(identity)
    assert runtime.config.request_exceptions[0].approved and runtime.gateway.request_exceptions == ()
    listed = listed_exceptions(server.db)
    assert listed == [{"identity": identity.id, "exception": "x0123456789", "scope": "identity", "state": "forbidden"}]


async def test_server_status_lists_an_exception_an_upgrade_suspended(server):
    """Minor 2, R-100: an exception approved under a request profile this version revised needs approval again; `latet
    server status` lists it so, with its id, once the identity's space is built by this version (its index written
    again then), and never as permanent; its index row goes when it is revoked."""
    from latet.server.space_settings import listed_exceptions

    server.listening.set()
    suspended = _stamped(revision="http-envelope-0")  # an earlier Latet's revision, not carried
    identity, _ = _key_with(server, [suspended])
    assert listed_exceptions(server.db) == []  # kept by a save that wrote no index: unknown until its space is built
    runtime = await server.spaces.runtime(identity)
    assert runtime.gateway.request_exceptions == ()  # it grants nothing
    said = "needs approval again (profile revised)"
    assert listed_exceptions(server.db) == [
        {"identity": identity.id, "exception": "x0123456789", "scope": "identity", "state": said}
    ]
    await runtime.mediator.run(runtime.update_config, lambda c: c.model_copy(update={"request_exceptions": []}))
    assert listed_exceptions(server.db) == []


async def test_server_status_lists_permanent_exceptions(server):
    """Root's ruling on F-3: each identity's approved exceptions are indexed, by Latet's id and scope category alone,
    as they are approved, and the index row goes when the exception is revoked; `latet server status` lists the
    permanent ones from it, never a description, a destination or what an exception names."""
    import httpx

    from latet.server.app import create_server_app

    from .support import URL

    server.listening.set()
    person, _ = server.identities.add_person("Joan", "joan@example.com")
    runtime = await server.spaces.runtime(person)
    rows = [
        RequestException.model_validate({**TEAM, "permanent": True, "description": "the acme team's gateway"}),
        RequestException.model_validate({**TEAM, "project": "/repo"}),
    ]
    change = await runtime.mediator.run(
        runtime.propose, runtime.config.model_copy(update={"request_exceptions": rows}), "command"
    )
    assert change is not None and await runtime.mediator.run(runtime.decide_change, change.id, True) == "approved"
    permanent = next(row for row in runtime.config.request_exceptions if row.permanent)
    admin = {"x-latet-admin": server.admin.issue("status reader")}
    app = create_server_app(server)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL) as client:
        listed = (await client.get("/_latet/server/v1/admin/status", headers=admin)).json()["exceptions"]
        assert listed == [{"identity": person.id, "exception": permanent.id, "scope": "identity", "state": "permanent"}]
        assert "acme" not in repr(listed) and "x-team" not in repr(listed) and "llm-acme" not in repr(listed)
        await runtime.mediator.run(runtime.update_config, lambda c: c.model_copy(update={"request_exceptions": []}))
        assert (await client.get("/_latet/server/v1/admin/status", headers=admin)).json()["exceptions"] == []


def test_a_permanent_exception_the_administration_sets_for_an_api_key_is_logged(server, monkeypatch):
    """I-7, R-108: the administration's save of an API key's settings approves the exceptions it adds; one made
    permanent there is written to the administration log as a person's approval is, its id and its scope's category,
    never its description or what it names."""
    from starlette.testclient import TestClient

    from latet.server import egress
    from latet.server.app import create_server_app

    from .support import URL

    monkeypatch.setattr(egress, "_NAT64", egress._Nat64(()))  # no lookup of ipv4only.arpa from a test
    admin = {"x-latet-admin": server.admin.issue("first administrator")}
    base = "/_latet/server/v1/admin"
    with TestClient(create_server_app(server), base_url=URL) as client:
        key = client.post(f"{base}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin).json()
        path = f"{base}/identities/{key['identity']['id']}/settings"
        settings = client.get(path, headers=admin).json()["settings"]
        permanent = {**TEAM, "permanent": True, "description": "the acme team's gateway"}
        changed = client.put(path, json={**settings, "request_exceptions": [permanent]}, headers=admin)
        assert changed.status_code == 200, changed.text
        [row] = changed.json()["settings"]["request_exceptions"]
        assert row["issuer"] == "administration"
    entries = server.audit.query(action="exception.permanent")
    assert [(entry.target, entry.details) for entry in entries] == [
        (key["identity"]["id"], {"exception": row["id"], "scope": "identity"})
    ]
    assert all("acme" not in repr(entry.details) and "x-team" not in repr(entry.details) for entry in entries)


def test_the_index_is_kept_with_the_approval_and_the_status_writes_nothing(server, monkeypatch):
    """Minor 4: the index of the approved exceptions is written in the transaction that keeps the approved settings:
    one that cannot be written keeps no approval either (the administration's save says so), never an approved
    permanent exception left unlisted; and `latet server status` only reads it."""
    from starlette.testclient import TestClient

    from latet.server import backends, egress, space_settings
    from latet.server.app import create_server_app

    from .support import URL

    monkeypatch.setattr(egress, "_NAT64", egress._Nat64(()))
    admin = {"x-latet-admin": server.admin.issue("first administrator")}
    base = "/_latet/server/v1/admin"
    with TestClient(create_server_app(server), base_url=URL) as client:
        key = client.post(f"{base}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin).json()
        path = f"{base}/identities/{key['identity']['id']}/settings"
        settings = client.get(path, headers=admin).json()["settings"]
        permanent = {**TEAM, "permanent": True}
        written = space_settings.index_exceptions

        def unwritable(conn, *args, **kwargs):
            raise RuntimeError("the index does not take it")

        monkeypatch.setattr(backends, "index_exceptions", unwritable)
        refused = client.put(path, json={**settings, "request_exceptions": [permanent]}, headers=admin)
        assert refused.status_code == 503
        assert client.get(path, headers=admin).json()["settings"]["request_exceptions"] == []  # neither kept nor run
        assert client.get(f"{base}/status", headers=admin).json()["exceptions"] == []
        assert server.audit.query(action="exception.permanent") == []  # an approval not kept is logged by nobody
        monkeypatch.setattr(backends, "index_exceptions", written)
        assert client.put(path, json={**settings, "request_exceptions": [permanent]}, headers=admin).status_code == 200
        with server.db.transaction() as conn:  # a row past its end: never listed, and left where it is
            conn.execute(
                "INSERT INTO request_exception_index (identity, exception, scope, permanent, expires, state)"
                " VALUES (%s, 'x0000000001', 'identity', false, clock_timestamp() - interval '1 day', 'suspended')",
                (key["identity"]["id"],),
            )
        listed = client.get(f"{base}/status", headers=admin).json()["exceptions"]
        assert [row["state"] for row in listed] == ["permanent"]
    with server.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM request_exception_index").fetchone()[0] == 2  # the status wrote none


def _indexed(server, identity_id: str) -> list[str]:
    with server.db.connection() as conn:
        rows = conn.execute(
            "SELECT exception FROM request_exception_index WHERE identity = %s ORDER BY exception", (identity_id,)
        ).fetchall()
    return [row[0] for row in rows]


def test_two_builds_at_once_write_the_exception_index_once(server, monkeypatch, caplog):
    """Minors after 0.11, item 5 (Task 13's final review, M-7): several workers or instances build one identity's space
    at once (a start, Valkey reconnected), each writing the index of its request exceptions again. Before, each deleted
    and inserted the rows: two at once met on the primary key and one was logged "was not written again:
    UniqueViolation", for nothing. Now one writes at a time, and only what changed: the second finds the rows written.
    The first build waits, once it has deleted, until the second has deleted too or waits for the index's lock: a
    condition, never a fixed sleep."""
    import contextlib
    import logging
    import threading
    import time

    from latet.config import Config
    from latet.server import spaces as spaces_module

    # A permanent exception and a timed one: the timed row's end, a timestamptz, read back equal (the fix round's Q-M3).
    rows = [_stamped(permanent=True), _stamped(id="x0000000abc")]
    identity, _ = _key_with(server, rows)
    assert _indexed(server, identity.id) == []  # a save that wrote no index
    config = Config(request_exceptions=rows)
    transaction = server.db.transaction
    deleted = {"first": threading.Event(), "second": threading.Event()}
    deletes: list[str] = []

    def other_waits_on_the_lock() -> bool:
        with server.db.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            ).fetchone()
        return bool(row and row[0])

    class _Watched:
        def __init__(self, conn, me: str) -> None:
            self.conn, self.me = conn, me

        def execute(self, query, *args, **kwargs):
            found = self.conn.execute(query, *args, **kwargs)
            if str(query).startswith("DELETE FROM request_exception_index"):
                deletes.append(self.me)
                deleted[self.me].set()
                other = deleted["second" if self.me == "first" else "first"]
                for _ in range(1000):  # until the other build deleted too, or waits for the lock: at most 10 s
                    if other.is_set() or other_waits_on_the_lock():
                        break
                    time.sleep(0.01)
            return found

        def __getattr__(self, name):
            return getattr(self.conn, name)

    @contextlib.contextmanager
    def watched():
        with transaction() as conn:
            yield _Watched(conn, threading.current_thread().name)

    monkeypatch.setattr(server.db, "transaction", watched)
    builds = [
        threading.Thread(target=spaces_module._index_again, args=(server, identity.id, config), name=name)
        for name in ("first", "second")
    ]
    with caplog.at_level(logging.WARNING, logger="latet.server.spaces"):
        for build in builds:
            build.start()
        for build in builds:
            build.join(30)
    assert not any("was not written again" in record.getMessage() for record in caplog.records), caplog.text
    assert _indexed(server, identity.id) == ["x0000000abc", "x0123456789"] and len(deletes) == 1, deletes


def test_a_build_that_read_the_settings_before_an_approval_never_brings_back_what_it_revoked(server):
    """Minors after 0.11, item 5: a space built from settings read before an approval revoked an exception, writing its
    index after that approval wrote its own, put the revoked one back in what `latet server status` lists. The build
    says the settings count it read (``read_at``): a newer approval's index stands."""
    from latet.config import Config
    from latet.server import spaces as spaces_module
    from latet.server.master import root_key
    from latet.server.space_settings import PgSettings, index_exceptions, settings_owner

    identity, _ = _key_with(server, [_stamped(permanent=True)])
    stale = Config(request_exceptions=[_stamped(permanent=True)])  # what the build read
    read = spaces_module._read_counts(server.db, [settings_owner(identity.id)])[settings_owner(identity.id)]
    storage = PgSettings(
        server.db,
        server.shared,
        identity.id,
        root_key(server.db, server.master, identity.id, clock=server.clock),
        kind="key",
        index=lambda conn, config: index_exceptions(conn, identity.id, config, permanent_allowed=True),
    )
    revoked = storage.prepare(storage.load().model_copy(update={"request_exceptions": []}))
    storage.save(revoked)
    storage.save_reviewed(revoked)  # the approval: its index, without the exception, in its own transaction
    assert _indexed(server, identity.id) == []
    spaces_module._index_again(server, identity.id, stale, read_at=read)  # the build, writing after it
    assert _indexed(server, identity.id) == []


def test_a_build_after_a_save_that_wrote_no_index_still_writes_it(server):
    """Fix round S-M2 (item 5): a save of the settings' own part (``PgSettings.save``, a change waiting for approval,
    another instance's save) moves the settings count without writing the index. A build that read the settings before
    it skipped its index all the same, taking any newer count for an index written since: the first build after an
    edition forbids permanent exceptions, after a profile revision suspends some, or after migration 017's empty index
    then left `latet server status` listing what was there. Only an index written after what the build read stands."""
    from latet.server import spaces as spaces_module
    from latet.server.master import root_key
    from latet.server.space_settings import PgSettings, settings_owner

    identity, _ = _key_with(server, [_stamped(permanent=True)])  # kept with no index (migration 017's state)
    assert _indexed(server, identity.id) == []
    owner = settings_owner(identity.id)
    read = spaces_module._read_counts(server.db, [owner])[owner]  # what the build read
    storage = PgSettings(
        server.db,
        server.shared,
        identity.id,
        root_key(server.db, server.master, identity.id, clock=server.clock),
        kind="key",
    )
    loaded = storage.load()
    storage.save(loaded.model_copy(update={"retention_days": loaded.retention_days + 1}))  # its own part alone
    assert spaces_module._read_counts(server.db, [owner])[owner] > read
    spaces_module._index_again(server, identity.id, loaded, read_at=read)
    assert _indexed(server, identity.id) == ["x0123456789"]


async def test_a_spaces_build_never_writes_over_an_approval_kept_while_it_was_built(server, monkeypatch):
    """Fix round Q-M3 (item 5): through ``Spaces._build`` itself, its ``read_at`` from what the build applied
    (``_Counts.highest``). An approval revoking an exception is kept between the build's reading of the settings and
    its writing of the index: the approval's index stands, the revoked exception never comes back."""
    import asyncio
    import threading

    from latet.server import spaces as spaces_module
    from latet.server.master import root_key
    from latet.server.space_settings import PgSettings, index_exceptions

    server.listening.set()
    identity, _ = _key_with(server, [_stamped(permanent=True)])
    reached, approved = threading.Event(), threading.Event()
    index_again = spaces_module._index_again
    seen: list[int | None] = []

    def held(server_, identity_id, config, read_at=None):
        seen.append(read_at)
        reached.set()  # the build read the settings; its index not written yet
        assert approved.wait(10)
        index_again(server_, identity_id, config, read_at=read_at)

    monkeypatch.setattr(spaces_module, "_index_again", held)

    def approve() -> None:
        storage = PgSettings(
            server.db,
            server.shared,
            identity.id,
            root_key(server.db, server.master, identity.id, clock=server.clock),
            kind="key",
            index=lambda conn, config: index_exceptions(conn, identity.id, config, permanent_allowed=True),
        )
        revoked = storage.prepare(storage.load().model_copy(update={"request_exceptions": []}))
        storage.save(revoked)
        storage.save_reviewed(revoked)

    building = asyncio.create_task(server.spaces.runtime(identity))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        await asyncio.to_thread(approve)
        assert _indexed(server, identity.id) == []  # the approval's index
    finally:
        approved.set()
    await asyncio.wait_for(building, 30)
    assert _indexed(server, identity.id) == []  # never the revoked one back
    assert seen and seen[0] is not None  # the build said what it read
