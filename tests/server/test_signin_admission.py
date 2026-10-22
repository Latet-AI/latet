"""Only an issued, unspent code bypasses its address's miss budget."""

import asyncio
import gc
import hashlib
import json
import threading
import weakref
from http.cookies import SimpleCookie

import pytest
import redis

from latet.server import app as app_module
from latet.server import machines
from latet.server.app import create_server_app
from latet.server.logs import REFUSED
from latet.state.local import CODE_TTL

from .conftest import valkey_dump
from .support import listening, until
from .test_app import KEYS, SERVER, _asgi, _location

pytestmark = pytest.mark.server
ADDRESS = "203.0.113.23"


def _person(server):
    person, code = server.identities.add_person("Emily Carter")
    with server.identities.changing() as changes:
        found = server.identities.enrol(code, "laptop", within=changes)
    assert found is not None
    KEYS[found[1]] = found[2]  # the computer's key, which a sign-in link asks for with the token (R16)
    return person, found[1]


async def _issue(app, token):
    sent = []
    status, _ = await asyncio.wait_for(
        _asgi(
            app,
            f"{SERVER}/login-code",
            method="POST",
            headers=[(b"x-latet-token", token.encode()), (b"x-latet-computer-key", KEYS[token].encode())],
            sent=sent,
        ),
        15,
    )
    assert status == 200
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return json.loads(body)["code"]


async def _login(app, code, *, headers=()):
    sent = []
    status, scope = await asyncio.wait_for(
        _asgi(app, "/_latet/ui/login", query=f"code={code}".encode(), client=ADDRESS, headers=headers, sent=sent),
        15,
    )
    assert status == 303
    [start] = [message for message in sent if message["type"] == "http.response.start"]
    return _location(sent), dict(start["headers"]), scope


def _limited(server):
    key = machines.miss_key(ADDRESS)
    # Spend a real shared budget as well as remembering the refusal on this instance.
    for _ in range(machines.LOOKUP_LIMIT + 1):
        machines.counted(server.bus, server.limits, key, machines.LOOKUP_LIMIT, machines.LOOKUP_WINDOW)
    return key


def _slots(server):
    return int(server.bus.get(server.bus.rate_key(machines.miss_key(ADDRESS))) or b"0")


def _code_key(server, code):
    # Stored random-secret records use 32 hex characters; the separate keyed known-set hash uses all 64.
    return server.bus.key(f"signin:code:{hashlib.sha256(code.encode()).hexdigest()[:32]}")


def _known_key(server, code):
    return server.bus.key(f"known:code:{server.known_codes.hashed(code).hex()}")


def _session(headers):
    cookie = SimpleCookie()
    cookie.load(headers[b"set-cookie"].decode())
    return cookie["latet_session"].value


async def test_an_issued_link_opens_from_an_address_whose_misses_are_exhausted(instances):
    """Existing-route regression: a valid issued link must survive junk from a shared client address."""
    a, b = instances
    person, token = _person(a)
    issuing, receiving = create_server_app(a), create_server_app(b)
    async with listening(a, b):
        code = await _issue(issuing, token)
        where = ""
        for n in range(machines.LOOKUP_LIMIT + 1):
            where, headers, scope = await _login(receiving, f"admission-junk-{n}")
            assert b"set-cookie" not in headers and scope.get(REFUSED)
        assert where == "/_latet/ui/?login=limited"
        before = _slots(b)
        # Reach the public outcome before consulting any future known_codes/redeeming implementation surface.
        where, headers, scope = await _login(receiving, code)
        assert where == "/_latet/ui/"
        assert b.sign_ins.subject_of(_session(headers)) == person.id and not scope.get(REFUSED)
        assert _slots(b) == before


async def test_issuance_keeps_the_code_in_the_same_credentials_job(server, monkeypatch):
    """Break: keep a code on the event loop, in another pool, or after returning its link."""
    _, token = _person(server)
    app = create_server_app(server)
    async with listening(server):
        issue, keep = server.sign_ins.issue_code, server.known_codes.keep
        jobs = []

        def issued(*args, **kwargs):
            code = issue(*args, **kwargs)
            jobs.append(("issue", threading.get_ident(), threading.current_thread().name))
            return code

        def kept(code):
            jobs.append(("keep", threading.get_ident(), threading.current_thread().name))
            return keep(code)

        monkeypatch.setattr(server.sign_ins, "issue_code", issued)
        monkeypatch.setattr(server.known_codes, "keep", kept)
        code = await _issue(app, token)
        assert [job[0] for job in jobs] == ["issue", "keep"]
        assert jobs[0][1:] == jobs[1][1:] and jobs[0][1] != threading.get_ident()
        assert jobs[0][2].startswith("latet-credentials")
        assert server.bus.client.exists(_code_key(server, code)) and server.bus.client.exists(_known_key(server, code))


async def test_issued_on_a_opened_on_b_after_junk_and_never_redeemed_twice(instances, monkeypatch, prefix):
    """Break: skip the issued set, retain instance memory, re-keep a spent code, or give back an untaken slot."""
    a, b = instances
    person, token = _person(a)
    apps = [create_server_app(a), create_server_app(b)]
    async with listening(a, b):
        code = await _issue(apps[0], token)
        # Real junk requests exhaust the address on B; the known link must still open there.
        for n in range(machines.LOOKUP_LIMIT + 1):
            where, headers, scope = await _login(apps[1], f"junk-{n}")
            assert where.endswith(("login=expired", "login=limited")) and b"set-cookie" not in headers
            assert scope.get(REFUSED)
        _limited(a)
        before = _slots(b)
        known_key = _known_key(a, code)
        assert 0 < a.bus.client.pttl(known_key) <= int(CODE_TTL * 1000)
        assert not a.known_codes.holds_here(code) and not b.known_codes.holds_here(code)
        assert code.encode() not in valkey_dump(a.bus, prefix)
        # Independent credential kinds must not share a batching queue.
        assert (
            len(
                {
                    id(a.known_codes.reads),
                    id(a.known_secrets.reads),
                    id(a.known_routes.reads),
                    id(a.known_sessions.reads),
                }
            )
            == 4
        )
        where, headers, _ = await _login(apps[1], code)
        assert where == "/_latet/ui/" and b.sign_ins.subject_of(_session(headers)) == person.id
        assert _slots(b) == before and not a.bus.client.exists(known_key)
        assert not a.known_codes.holds(code) and not b.known_codes.holds(code)
        assert not a.bus.client.exists(_code_key(a, code))

        getdel, redeemed = b.bus.client.getdel, []

        def reading(key):
            redeemed.append(key)
            return getdel(key)

        # Both fixtures have separate Redis clients: watch both, and preserve real GETDEL if wrongly reached.
        other_getdel = a.bus.client.getdel

        def other_reading(key):
            redeemed.append(key)
            return other_getdel(key)

        monkeypatch.setattr(b.bus.client, "getdel", reading)
        monkeypatch.setattr(a.bus.client, "getdel", other_reading)
        for app in reversed(apps):
            where, headers, scope = await _login(app, code)
            assert where == "/_latet/ui/?login=limited" and b"set-cookie" not in headers and scope.get(REFUSED)
        assert not redeemed and _slots(b) == before


@pytest.mark.parametrize("invalid", ["expired", "missing", "revoked", "disabled", "unbound"])
async def test_a_known_code_that_opens_nothing_is_forgotten_and_counts_one_miss(server, monkeypatch, invalid):
    """Break: forget only successful codes, re-keep invalid codes, or fail to count an admitted miss."""
    person, token = _person(server)
    app = create_server_app(server)
    async with listening(server):
        if invalid == "unbound":
            code = server.sign_ins.issue_code(person.id)
            server.known_codes.keep(code)
        else:
            code = await _issue(app, token)
        if invalid == "expired":
            # The sealed record's logical expiry is independent of Redis's cleanup grace and the known-set TTL.
            clock = server.sign_ins._clock
            monkeypatch.setattr(server.sign_ins, "_clock", lambda: clock() + CODE_TTL + 1)
        elif invalid == "missing":
            assert server.bus.client.delete(_code_key(server, code)) == 1  # establish an actual missing-record case
        elif invalid == "revoked":
            with server.identities.changing() as changes:
                server.identities.revoked_secret(token, within=changes)
        elif invalid == "disabled":
            with server.db.connection() as conn:
                conn.execute("UPDATE identities SET disabled = true WHERE id = %s", (person.id,))
        _limited(server)
        before = _slots(server)
        where, headers, scope = await _login(app, code)
        assert where == "/_latet/ui/?login=expired" and b"set-cookie" not in headers and scope.get(REFUSED)
        assert _slots(server) == before + 1
        assert not server.bus.client.exists(_known_key(server, code))
        assert not server.known_codes.holds(code) and not server.redeeming
        where, _, _ = await _login(app, code)
        assert where == "/_latet/ui/?login=limited" and _slots(server) == before + 1


async def test_only_an_issued_code_bypasses_the_limit_and_cross_site_opens_nothing(server):
    """Break: trust the shape of a code, or consult/redeem its issued set before the other-site guard."""
    person, token = _person(server)
    app = create_server_app(server)
    async with listening(server):
        code = await _issue(app, token)
        _limited(server)
        before = _slots(server)
        where, headers, scope = await _login(app, code, headers=[(b"sec-fetch-site", b"cross-site")])
        assert where == "/_latet/ui/?login=expired" and b"set-cookie" not in headers and scope.get(REFUSED)
        assert server.bus.client.exists(_code_key(server, code)) and server.bus.client.exists(_known_key(server, code))
        assert _slots(server) == before
        # A real, sealed code inserted outside issuance is not a member of the issued set.
        unknown = server.sign_ins.issue_code(person.id, token=server.identities.secret_digest(token))
        where, headers, _ = await _login(app, unknown)
        assert where == "/_latet/ui/?login=limited" and b"set-cookie" not in headers
        assert server.bus.client.exists(_code_key(server, unknown))
        where, headers, _ = await _login(app, code)
        assert where == "/_latet/ui/" and server.sign_ins.subject_of(_session(headers)) == person.id


async def test_500_requests_of_one_code_have_one_redeem_and_one_session(server, monkeypatch):
    """Break: omit the event-loop redeem guard, or share the winning session with duplicate browsers."""
    person, token = _person(server)
    app = create_server_app(server)
    async with listening(server):
        code = await _issue(app, token)
        _limited(server)
        before = _slots(server)
        real = server.bus.client.getdel
        entered, release = threading.Event(), threading.Event()
        reads = []

        def held(key):
            reads.append(key)
            entered.set()
            assert release.wait(10), "duplicate requests did not finish before the winning redeem"
            return real(key)

        monkeypatch.setattr(server.bus.client, "getdel", held)
        winner = asyncio.create_task(_login(app, code))
        try:
            await until(entered.is_set)
            duplicates = await asyncio.wait_for(asyncio.gather(*(_login(app, code) for _ in range(499))), 5)
            assert all(where == "/_latet/ui/?login=expired" for where, _, _ in duplicates)
            assert all(b"set-cookie" not in headers and scope.get(REFUSED) for _, headers, scope in duplicates)
            assert _slots(server) == before
        finally:
            release.set()
            where, headers, _ = await asyncio.wait_for(winner, 15)
        assert where == "/_latet/ui/" and server.sign_ins.subject_of(_session(headers)) == person.id
        assert reads == [_code_key(server, code)] and _slots(server) == before and not server.redeeming


async def test_failed_getdel_keeps_the_code_known_and_releases_the_redeem_guard(server, monkeypatch):
    """Break: forget on a failed GETDEL, or leak the in-flight hash after a store exception."""
    person, token = _person(server)
    app = create_server_app(server)
    async with listening(server):
        code = await _issue(app, token)
        _limited(server)
        before = _slots(server)
        with monkeypatch.context() as patch:

            def unavailable(key):
                raise redis.TimeoutError("task23a controlled GETDEL outage")

            patch.setattr(server.bus.client, "getdel", unavailable)
            where, headers, scope = await _login(app, code)
        assert where == "/_latet/ui/?login=busy" and b"set-cookie" not in headers and scope.get(REFUSED)
        assert not server.redeeming and _slots(server) == before
        assert server.bus.client.exists(_known_key(server, code)) and server.bus.client.exists(_code_key(server, code))
        where, headers, _ = await _login(app, code)
        assert where == "/_latet/ui/" and server.sign_ins.subject_of(_session(headers)) == person.id


async def test_failed_known_keep_falls_back_to_a_counted_signin(server, monkeypatch, caplog):
    """Break: fail issuance on a known-set write outage, or invent a bypass after that write failed."""
    person, token = _person(server)
    app = create_server_app(server)
    async with listening(server):
        real = server.bus.set

        def unavailable(name, *args, **kwargs):
            if name.startswith("known:code:"):
                raise redis.TimeoutError("task23a controlled known-set write outage")
            return real(name, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(server.bus, "set", unavailable)
            code = await _issue(app, token)
        assert not server.bus.client.exists(_known_key(server, code))
        server.bus.slot(machines.miss_key(ADDRESS), machines.LOOKUP_LIMIT, machines.LOOKUP_WINDOW)
        before = _slots(server)
        where, headers, _ = await _login(app, code)
        assert where == "/_latet/ui/" and server.sign_ins.subject_of(_session(headers)) == person.id
        assert _slots(server) == before
        assert code not in caplog.text and token not in caplog.text


async def test_a_known_set_read_outage_does_not_bypass_the_budget(server, monkeypatch):
    """Break: treat an unanswered known-set read as an issued code, or fall back to instance memory."""
    _, token = _person(server)
    app = create_server_app(server)
    async with listening(server):
        code = await _issue(app, token)
        _limited(server)
        before = _slots(server)
        # Only the known read fails; the real code remains intact in the real Valkey.
        with monkeypatch.context() as patch:

            def unavailable(*args, **kwargs):
                raise redis.TimeoutError("task23a controlled known-set read outage")

            patch.setattr(server.bus.client, "pipeline", unavailable)
            where, headers, scope = await _login(app, code)
        assert where == "/_latet/ui/?login=limited" and b"set-cookie" not in headers and scope.get(REFUSED)
        assert server.bus.client.exists(_code_key(server, code)) and _slots(server) == before


async def test_a_known_codes_redeem_does_not_reread_the_known_set_in_a_credentials_thread(server, monkeypatch):
    """Break: reuse checked, which reads/keeps the known set and thereby renews a one-time credential."""
    person, token = _person(server)
    app = create_server_app(server)
    async with listening(server):
        code = await _issue(app, token)
        _limited(server)

        def forbidden(*args, **kwargs):
            raise AssertionError("a code admitted on the loop must not recheck Known in the credentials job")

        monkeypatch.setattr(server.known_codes, "holds", forbidden)
        where, headers, _ = await _login(app, code)
        assert where == "/_latet/ui/" and server.sign_ins.subject_of(_session(headers)) == person.id
        assert not server.bus.client.exists(_known_key(server, code))


@pytest.mark.parametrize("outcome", ["complete", "error"])
async def test_cancelled_signin_keeps_one_redeem_until_its_worker_finishes(server, monkeypatch, caplog, outcome):
    """Break: cancellation releases a running code claim, duplicates its job, or abandons its failed owner."""
    person, token = _person(server)
    app = create_server_app(server)
    async with listening(server):
        code = await _issue(app, token)
        _limited(server)
        before = _slots(server)
        waiting = machines.waiting_calls()
        entered, release = threading.Event(), threading.Event()
        real = server.bus.client.getdel
        pre_auth = app_module.pre_auth
        jobs, finished, reads, owners = [], [], [], []
        loop = asyncio.get_running_loop()
        previous_handler, errors = loop.get_exception_handler(), []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))

        async def counted_job(work, *args):
            if work.__name__ != "signed_in":
                return await pre_auth(work, *args)
            jobs.append(work.__name__)
            owners.append(weakref.ref(asyncio.current_task()))

            def observed(*given):
                try:
                    return work(*given)
                finally:
                    finished.append(True)

            return await pre_auth(observed, *args)

        def held(key):
            reads.append(key)
            entered.set()
            assert release.wait(20), "cancelled-owner test did not release its worker"
            if outcome == "error":
                raise redis.TimeoutError("task23a cancelled-owner private outage sentinel")
            return real(key)

        monkeypatch.setattr(server.bus.client, "getdel", held)
        monkeypatch.setattr(app_module, "pre_auth", counted_job)
        request = asyncio.create_task(_login(app, code))
        duplicate = None
        verified_duplicate = False
        try:
            await until(entered.is_set, seconds=5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 2)
            duplicate = asyncio.create_task(_login(app, code))
            await until(lambda: duplicate.done() or len(jobs) > 1, seconds=3)
            assert jobs == ["signed_in"], "duplicate submitted another credentials job while owner still ran"
            where, headers, scope = await asyncio.wait_for(duplicate, 2)
            assert where == "/_latet/ui/?login=expired" and b"set-cookie" not in headers and scope.get(REFUSED)
            assert reads == [_code_key(server, code)] and not finished and _slots(server) == before
            assert server.known_codes.hashed(code) in server.redeeming
            owner = owners[0]()
            assert owner is not None and not owner.done()
            del owner
            assert machines.waiting_calls() == waiting + 1
            verified_duplicate = True
        finally:
            release.set()
            try:
                tasks = [request] + ([duplicate] if duplicate is not None else [])
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                await until(lambda: len(finished) == len(jobs), seconds=5)
                await until(lambda: not server.redeeming and machines.waiting_calls() == waiting, seconds=5)
                if verified_duplicate:
                    gc.collect()
                    await until(lambda: errors or all(owner() is None for owner in owners), seconds=5)
                    assert errors == [], "detached redemption owner left an unobserved task failure"
            finally:
                loop.set_exception_handler(previous_handler)
        assert _slots(server) == before
        assert "task23a cancelled-owner private outage sentinel" not in caplog.text
        assert code not in caplog.text and token not in caplog.text
        if outcome == "error":
            assert server.bus.client.exists(_known_key(server, code)) and server.bus.client.exists(
                _code_key(server, code)
            )
            monkeypatch.setattr(server.bus.client, "getdel", real)
            where, headers, _ = await _login(app, code)
            assert where == "/_latet/ui/" and server.sign_ins.subject_of(_session(headers)) == person.id
        else:
            assert not server.bus.client.exists(_known_key(server, code))
            assert not server.bus.client.exists(_code_key(server, code))
            where, headers, _ = await _login(app, code)
            assert where == "/_latet/ui/?login=limited" and b"set-cookie" not in headers


async def test_2000_junk_codes_do_not_fill_a_colleagues_token_batch(instances):
    """Break: let codes use the token KnownReads queue, whose finite batch can evict the valid token."""
    a, b = instances
    person, token = _person(a)
    a.known_secrets.keep(token)
    b.identities.forget()
    app = create_server_app(b)
    async with listening(a, b):
        _limited(b)
        junk = [asyncio.create_task(_login(app, f"task23a-junk-{n}")) for n in range(2000)]
        try:
            sent = []
            status, _ = await asyncio.wait_for(
                _asgi(app, f"{SERVER}/whoami", client=ADDRESS, headers=[(b"x-latet-token", token.encode())], sent=sent),
                5,
            )
            assert status == 200
            body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
            assert json.loads(body)["id"] == person.id
        finally:
            answers = await asyncio.wait_for(asyncio.gather(*junk), 15)
        assert all(where == "/_latet/ui/?login=limited" and scope.get(REFUSED) for where, _, scope in answers)
