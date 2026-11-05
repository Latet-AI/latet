"""Nothing of a vault in clear in Valkey (Task 11 of the server performance plan): what Valkey holds and carries for a
vault is ids, kinds, counts and sealed data, on the routes the scenario below takes.

Each test runs one scenario on two instances of one server (``_scenario``):
- Canaries: a name (not ASCII, with a character JSON escapes), an email and a token. An API key masks them through the
  applications API and the gateway; a person masks them through the gateway on both instances, her calls reporting a
  project folder that moves (the folders are canaries too).
- Restored by the key's unmask, the gateway's answers and the person's dashboard reveal. Forgotten: the key's email
  through the applications API, the person's name in her dashboard.
- The person's calls past a lookup bound: her whole vault read (``StoredVault._whole_read``) and its base made again in
  the background (``_rebase_later``), her space on its store as a vault past the bound of one held whole is. A second
  person's vault held whole (``StoredVault.holds_all``, 35,000 values or fewer): her calls, past a lookup bound too, read
  from memory on both instances, a value of hers forgotten on one instance and her vault read whole again on the other.
- Both vaults as a version before 0.11 left them: the key's rules and prints gone, the person's words gone, each made
  again (announced as `rule` and `words`).
- Each instance's beat, as worker 0 and 1, on a worker's pipe, in Valkey and in the status.

Then: what Valkey holds under the test's prefix and every byte the instances sent it hold no value, none of its words
(``anchors.words_in``), none of their prints and none of the vault's keyed hashes, in the encodings ``_in_clear`` names;
every message of a vault carries ids, kinds and counts alone; a beat carries numbers and the version alone; and the logs
hold no canary.

Not taken here: the Live Reviewer's batches (pinned against Valkey by test_queue.py), a dashboard event too heavy to go
inline (kept by reference), the approved texts and the Responses API's ids."""

import asyncio
import base64
import contextlib
import itertools
import json
import multiprocessing
import re
import secrets
import threading
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
import numpy as np
import pytest
import redis.asyncio.connection
import redis.connection

from latet import __version__
from latet import runtime as runtime_module
from latet.config import ProjectEntry
from latet.engine import lookup
from latet.engine import stored as stored_module
from latet.engine.anchors import words_in
from latet.engine.stored import StoredVault, words_of
from latet.gateway.provenance import ECHO_RULE, EchoCheck, EchoScope, OpaqueCapture
from latet.server import app as app_module
from latet.server.app import create_server_app
from latet.server.bus import Bus
from latet.server.master import root_key
from latet.server.prints import print_key, prints
from latet.server.tokens import route_key
from latet.server.vault import PgVaultStore
from latet.server.workers import BEAT_BYTES, INSTANCE_ID, TELEMETRY, _Link, beat_checked

from .conftest import valkey_dump
from .support import URL, captured_logs, until
from .test_key_vault import upstream  # noqa: F401 - the stub model every space of a test reaches
from .test_launch_authority_core import _ISSUER, _sent
from .test_launch_authority_core import (
    launch_bootstrap_db as launch_bootstrap_db,
)
from .test_launch_authority_core import (
    owned_launch_namespaces as owned_launch_namespaces,
)
from .test_launch_authority_core import (
    protected_core as protected_core,
)

pytestmark = pytest.mark.server

SERVER = "/_latet/server/v1"
ADMIN = f"{SERVER}/admin"
API = "/_latet/api/v1"
APPS = "/_latet/apps/v1"
CHAT = "/openai/v1/chat/completions"
UI = {"x-latet-ui": "1"}
WHERE = ("10.0.3.9", 40000)

NAME = 'Ottavio "Brambillascà"'  # found by a rule of each identity's, then by its vault alone
DOMAIN = "quarzonexvio.zuerich"  # no 6 letters of it are part of a common word (``COMMON``)
EMAIL = f"rosalinda.quaglierini@{DOMAIN}"
TOKEN = "Vr4tY9uK2mQ7xZ3bN8cL5wP1sD6fG0hJaWe"  # found by gitleaks' generic rule after `OPENAI_API_KEY=`
TEXT = f"call {NAME} about the renewal, write to {EMAIL} and deploy with export OPENAI_API_KEY={TOKEN}"
CANARIES = (NAME, EMAIL, TOKEN)
# The person's new values, one a call past a lookup bound: each a change her whole read applies (``_Whole.updated``).
EXTRAS = tuple(f"filiberto.zanzottera{n}@{DOMAIN}" for n in range(1, 5))
VALUES = (*CANARIES, *EXTRAS)
# The folders the person's agents name, one after the other: her gateway calls claim them (``x-latet-project``), which
# binds nothing; her Claude Code plugin's bridge, a trusted source, binds its conversation to each in turn, each move
# written sealed to Valkey and announced as `where`.
FOLDERS = ("/zibellinovesta/quorvantile", "/zibellinovesta/drumelpasto")
PLUGIN_SESSION = "7c2e9a41-5b3d-4f8e-a6c0-1d9e8f7b2a35"  # Claude Code's session id, a UUID
NEEDLES = (*VALUES, *FOLDERS)
RULE = {"name": "customer-name", "type": "CUSTOMER", "pattern": NAME}
# Words Latet's lines and its libraries' say: a needle word inside one of them could be met with no leak at all.
COMMON = (
    "network", "museum", "request", "response", "connection", "instance", "settings", "address", "message",
    "conversation", "worker", "server", "process", "session", "identity", "original", "surrogate", "masked", "values",
    "restored", "protocol", "completions", "assistant", "content", "function", "choices", "stream", "generation",
    "database", "postgres", "valkey", "certificate", "listening", "started", "stopped", "readiness", "warning",
)  # fmt: skip
# What a message on an identity's channel may say: its kind, an entry's id, the space that made it and the count. The
# plan's list of keys, widened for one kind alone: `where` (a conversation's project and folder moved, live.py) says the
# conversation's keyed name (an id) and its version (a count), as test_live.py pins it, and nothing else.
KINDS = {
    "entity": {"kind", "id", "from", "counter"},
    "rule": {"kind", "id", "from", "counter"},
    "forgotten": {"kind", "id", "from", "counter"},
    "settings": {"kind", "from", "counter"},
    "words": {"kind", "from", "counter"},
    "prints": {"kind", "from", "counter"},  # a fill of the prints over (T13's I-1): its last count
    "where": {"kind", "conversation", "version"},
}
ENTITY_ID = re.compile(r"[0-9a-f]{20}")  # a keyed hash of a value's type and value (``Vault.entity_id``)
SOURCE = re.compile(r"[0-9a-f]{12}:(?:[0-9a-f]{12}|administration)")  # an instance's id and its space's, or the admin
CONVERSATION = re.compile(r"[0-9a-f]{32}")  # a keyed hash of the conversation's key (``ValkeyConversations._name``)
_MASK64 = (1 << 64) - 1
_REBASING = "latet-whole-base"  # the thread that makes a whole read's base again (``StoredVault._rebase_later``)
_WHOLE = threading.local()  # set while a thread reads a vault whole (``StoredVault._whole_read``)


@dataclass
class Seen:
    """What a scenario left: every message published under the test's prefix, every byte any instance sent to Valkey
    (and apart, what the asyncio connections sent), what Valkey held at its end, what was logged, each instance's beat
    as its pipe, its key and the status said it, the needles of what must never be in clear, how many times a vault was
    read whole and had its base made again, and what those sent Valkey."""

    published: list[tuple[str, dict]] = field(default_factory=list)
    wire: list[bytes] = field(default_factory=list)
    wire_async: list[bytes] = field(default_factory=list)
    held: bytes = b""
    logs: str = ""
    piped: list[tuple[int, bytes]] = field(default_factory=list)
    keys: list[bytes] = field(default_factory=list)
    status: list[dict] = field(default_factory=list)
    instances: list[str] = field(default_factory=list)
    rebases: int = 0  # bases made again: the thread that makes one ran
    whole_reads: int = 0
    from_whole: list[bytes] = field(default_factory=list)  # what a whole read or the making of its base sent Valkey
    identities: tuple[str, ...] = ()
    words: set[str] = field(default_factory=set)
    prints: set[int] = field(default_factory=set)
    hashed: set[bytes] = field(default_factory=set)  # the vault's own keyed hashes of its strings and words


def _cores(data: bytes) -> set[bytes]:
    """``data`` in base64, standard and for URLs, at each of the three places it can start in a group of three bytes:
    the groups its own bytes alone make, which any longer text that holds it encodes the same way."""
    found = set()
    for before in range(3):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            written = encode(bytes(before) + data)
            end = len(written) - (4 if (before + len(data)) % 3 else 0)  # a last group with other bytes in it
            found.add(written[4 if before else 0 : end])
    return found


def _forms(value: str) -> set[bytes]:
    """The ways a store or a line could write ``value``: as it is, in either case, escaped as a part of a URL and as a
    JSON string (``\\u`` escapes or not), in UTF-16 either way round, in hexadecimal of either case, and in base64."""
    plain = value.encode()
    texts = {value, value.upper(), value.lower(), quote(value, safe=""), json.dumps(value)[1:-1]}
    texts |= {json.dumps(value, ensure_ascii=False)[1:-1], plain.hex(), plain.hex().upper()}
    return {text.encode() for text in texts} | {value.encode("utf-16-le"), value.encode("utf-16-be")} | _cores(plain)


def _windows(blob: bytes) -> np.ndarray:
    """Every 8 bytes of ``blob``, at each of the 8 offsets, read as little-endian unsigned numbers."""
    found = [
        np.frombuffer(blob, dtype="<u8", count=(len(blob) - k) // 8, offset=k) for k in range(8) if len(blob) - k >= 8
    ]
    return np.concatenate(found) if found else np.zeros(0, dtype="<u8")


def _in_clear(blob: bytes, seen: Seen) -> list[str]:
    """What ``blob`` holds in clear, named and never quoted:
    - each needle (a value or a folder) in any of its forms (``_forms``);
    - each of their words, in folded case, against the blob in ASCII lower case;
    - each of the vault's keyed hashes, as bytes, hexadecimal text or base64;
    - each print as 8 bytes little-endian or big-endian, as decimal text (signed or unsigned), as hexadecimal text of
      either byte order, and as base64 of either."""
    found = [f"needle {NEEDLES.index(needle)}" for needle in NEEDLES for form in _forms(needle) if form in blob]
    lowered = blob.lower()
    found += [f"word {word!r}" for word in sorted(seen.words) if word.encode() in lowered]
    coded = b"\x00".join(re.findall(rb"[A-Za-z0-9+/_-]{8,}", blob))  # every run base64 could have written
    hexes = b"\x00".join(re.findall(rb"[0-9a-f]{16,}", lowered))  # every long run of hexadecimal digits
    digits = b"\x00".join(re.findall(rb"-?[0-9]{12,}", blob))  # every long run of digits
    for hashed in seen.hashed:
        if hashed in blob or hashed.hex().encode() in hexes or any(core in coded for core in _cores(hashed)):
            found.append("a keyed hash of the vault")
    if not seen.prints:
        return found
    unsigned = np.array(sorted(p & _MASK64 for p in seen.prints), dtype="<u8")
    needles = np.union1d(unsigned, unsigned.byteswap())  # big-endian bytes read little-endian: the swapped number
    found += ["a print as 8 bytes"] * int(np.isin(_windows(blob), needles).any())
    for printed in sorted(seen.prints):
        little, big = printed.to_bytes(8, "little", signed=True), printed.to_bytes(8, "big", signed=True)
        if any(text in digits for text in (str(printed).encode(), str(printed & _MASK64).encode())):
            found.append("a print as decimal text")
        if little.hex().encode() in hexes or big.hex().encode() in hexes:
            found.append("a print as hexadecimal text")
        if any(core in coded for core in _cores(little) | _cores(big)):
            found.append("a print in base64")
    return found


def _wire_spy(monkeypatch, seen: Seen) -> None:
    """Every command any connection of this process sends to Valkey from now on, as the bytes it sends (the instances'
    calls, their pipelines and scripts, their publishes and their subscriptions), in ``seen.wire``; those of the asyncio
    connections (the ears) in ``seen.wire_async`` too; and those sent by a whole read of a vault, or by the thread that
    makes its base again, in ``seen.from_whole``."""

    def joined(command) -> bytes:
        parts = [command] if isinstance(command, bytes | bytearray | memoryview | str) else command
        return b"".join(part.encode() if isinstance(part, str) else bytes(part) for part in parts)

    def keep(command, looped: bool) -> None:
        sent = joined(command)
        seen.wire.append(sent)
        if looped:
            seen.wire_async.append(sent)
        if getattr(_WHOLE, "reading", False) or threading.current_thread().name == _REBASING:
            seen.from_whole.append(sent)

    plain = redis.connection.AbstractConnection.send_packed_command
    looped = redis.asyncio.connection.AbstractConnection.send_packed_command

    def sending(self, command, check_health=True):
        keep(command, False)
        return plain(self, command, check_health)

    async def sending_async(self, command, check_health=True):
        keep(command, True)
        return await looped(self, command, check_health)

    monkeypatch.setattr(redis.connection.AbstractConnection, "send_packed_command", sending)
    monkeypatch.setattr(redis.asyncio.connection.AbstractConnection, "send_packed_command", sending_async)


def _publish_spy(monkeypatch, prefix: str) -> list[tuple[str, dict]]:
    """Every message an instance under ``prefix`` publishes from now on: its channel and a copy of it."""
    told: list[tuple[str, dict]] = []
    publish = Bus.publish

    def publishing(self, channel, message):
        if self.prefix == prefix:
            told.append((channel, json.loads(json.dumps(dict(message)))))
        return publish(self, channel, message)

    monkeypatch.setattr(Bus, "publish", publishing)
    return told


def _rebase_spy(monkeypatch, seen: Seen) -> None:
    """``seen.rebases`` counts the bases a whole read's thread (named ``_REBASING``) made again, as the thread makes
    one; ``seen.whole_reads`` the whole reads of a vault, each marked in its thread while it reads."""
    weighing = stored_module._Base.weighing
    read = StoredVault._whole_read

    def weighed(self):
        if threading.current_thread().name == _REBASING:
            seen.rebases += 1
        return weighing(self)

    def reading(self):
        seen.whole_reads += 1
        _WHOLE.reading = True
        try:
            return read(self)
        finally:
            _WHOLE.reading = False

    monkeypatch.setattr(stored_module._Base, "weighing", weighed)
    monkeypatch.setattr(StoredVault, "_whole_read", reading)


@contextlib.asynccontextmanager
async def _serving(*servers):
    """Each instance's app started (its ear, its beat), and a client for each."""
    async with contextlib.AsyncExitStack() as stack:
        clients = []
        for server in servers:
            app = create_server_app(server)
            await stack.enter_async_context(app.router.lifespan_context(app))
            transport = httpx.ASGITransport(app=app, client=WHERE)
            clients.append(await stack.enter_async_context(httpx.AsyncClient(transport=transport, base_url=URL)))
        yield clients


def _bearer(secret: str) -> dict[str, str]:
    return {"authorization": f"Bearer {secret}"}


async def _apps(client, secret, operation, body):
    answer = await client.post(f"{APPS}/{operation}", json=body, headers=_bearer(secret))
    assert answer.status_code == 200, answer.text
    return answer.json()


async def _masked(client, secret, text, kind="prose") -> str:
    return (await _apps(client, secret, "mask", {"texts": [text], "kind": kind}))["results"][0]["text"]


async def _chat(client, upstream, secret, text, folder=None) -> tuple[str, str]:  # noqa: F811
    """The answer the agent reads through ``secret``'s address, its project's folder reported when ``folder`` is given,
    and what reached the model."""
    before = len(upstream.received)
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": text}]}
    headers = {"authorization": "Bearer sk-t", **({"x-latet-project": folder} if folder else {})}
    answer = await client.post(f"/u/{route_key(secret)}{CHAT}", json=body, headers=headers)
    assert answer.status_code == 200, answer.text
    said = "".join(
        choice.get("delta", {}).get("content") or ""
        for line in answer.text.splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line.removeprefix("data: ")).get("choices", [])
    )
    return said, b"".join(upstream.received[before:]).decode()


def _clear_of(text: str, *values: str) -> bool:
    """Whether ``text`` holds none of ``values`` in any of their forms (a body JSON escapes them in)."""
    written = text.encode()
    return not any(form in written for value in values for form in _forms(value))


async def _plugin_masked(client, secret, text, folder) -> str:
    """``text`` as the person's Claude Code plugin has her space mask it, for its conversation, in ``folder``."""
    item = {"text": text, "kind": "user_text"}
    answer = await client.post(
        f"/u/{route_key(secret)}/_latet/engine/v1/mask",
        json={"session": PLUGIN_SESSION, "cwd": folder, "items": [item]},
        headers={"x-latet-token": secret},
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["items"][0]["text"]


async def _own_projects(server, identity) -> None:
    """Settings of the identity's own for each folder's project: what makes the plugin's bridge bind its conversation
    there (a project the owner configured), as a person's dashboard saves them."""
    runtime = await server.spaces.runtime(identity)
    projects = [ProjectEntry(path=folder) for folder in FOLDERS]
    await asyncio.to_thread(runtime.update_config, lambda current: current.model_copy(update={"projects": projects}))


async def _teach(server, identity) -> None:
    """The canary name found by a rule of the identity's own (its settings changed, and announced)."""
    runtime = await server.spaces.runtime(identity)
    await asyncio.to_thread(runtime.update_config, lambda current: current.model_copy(update={"recognizers": [RULE]}))


def _on_its_store(monkeypatch, person_id: str) -> None:
    """The space of ``person_id`` built on its store whatever her vault's size, as a vault past the bound of one held
    whole is (``stored.WHOLE_VALUES`` 0 while hers is built, the builds of people one at a time): her calls past a
    lookup bound read her whole vault (``StoredVault._whole_read``); every other person's is held whole."""
    real, building = runtime_module._person_vault, threading.Lock()

    def person_vault(key, store, *args, **kwargs):
        with building:
            if store.identity != person_id:
                return real(key, store, *args, **kwargs)
            bound = stored_module.WHOLE_VALUES
            stored_module.WHOLE_VALUES = 0
            try:
                return real(key, store, *args, **kwargs)
            finally:
                stored_module.WHOLE_VALUES = bound

    monkeypatch.setattr(runtime_module, "_person_vault", person_vault)


def _store(server, identity_id: str) -> PgVaultStore:
    return PgVaultStore(server.db, identity_id, root_key(server.db, server.master, identity_id), limit=1_000_000)


def _stored(server, seen: Seen, *identity_ids: str) -> None:
    """What the vaults of ``identity_ids`` keep in PostgreSQL that a reader of Valkey must never meet: every print, and
    every keyed hash of a value, a fake, a string and a word, added to ``seen``."""
    ids = (list(identity_ids),)
    with server.db.connection() as conn:
        seen.prints |= {row[0] for row in conn.execute(
            "SELECT print FROM vault_words WHERE identity_id = ANY(%s) AND print IS NOT NULL", ids
        )}  # fmt: skip
        for query in (
            "SELECT original_key FROM vault_entities WHERE identity_id = ANY(%s)",
            "SELECT surrogate_key FROM vault_entities WHERE identity_id = ANY(%s)",
            "SELECT key FROM vault_strings WHERE identity_id = ANY(%s)",
            "SELECT word FROM vault_words WHERE identity_id = ANY(%s) AND word IS NOT NULL",
        ):
            seen.hashed |= {bytes(row[0]) for row in conn.execute(query, ids)}


async def _sign_in(client, token: str, computer_key: str) -> None:
    """The person signed in to her dashboard on ``client``, as her computer's sign-in link does it."""
    cli = {"x-latet-token": token, "x-latet-computer-key": computer_key}
    link = (await client.post(f"{SERVER}/login-code", headers=cli)).json()["url"]
    assert (await client.get(link.removeprefix(URL), follow_redirects=False)).status_code == 303


@contextlib.contextmanager
def _piped(worker: int):
    """A worker's end of a pipe (``workers._Link``, its thread started) and the supervisor's end. The link is closed,
    and its thread waited for, before the supervisor's end is: a link whose pipe closes under it takes its supervisor
    for gone, and ends the process (``os._exit``) once ``GRACEFUL_SECONDS`` are past."""
    supervisor, own = multiprocessing.Pipe()
    link = _Link(own, worker)
    link.start()
    try:
        yield link, supervisor
    finally:
        link.close()
        link._thread.join(10)
        assert not link._thread.is_alive()
        supervisor.close()


async def _beats(servers, client, admin: dict[str, str], seen: Seen) -> None:
    """Each instance's beat (its worker's number set before it started): handed to a worker's end of a pipe, which its
    supervisor reads, written to its key in Valkey and published; then the status as the administration reads it."""
    for k, server in enumerate(servers):
        with _piped(k) as (link, supervisor):
            server.reporting = link.report
            try:
                await asyncio.to_thread(app_module._alive, server)
                assert await asyncio.to_thread(supervisor.poll, 10)
                seen.piped.append((k, supervisor.recv_bytes(BEAT_BYTES)))
            finally:
                server.reporting = None
        seen.keys.append(server.bus.client.get(server.bus.key(f"instance:{server.instance}")))
    answer = await client.get(f"{ADMIN}/status", headers=admin)
    assert answer.status_code == 200, answer.text
    seen.status = answer.json()["instances"]


async def _scenario(instances, upstream, prefix, monkeypatch) -> Seen:  # noqa: F811
    a, b = instances
    a.worker, b.worker = 0, 1  # before they start: every beat says it, the regular ones too
    seen = Seen(instances=[a.instance, b.instance])
    seen.published = _publish_spy(monkeypatch, prefix)
    _wire_spy(monkeypatch, seen)
    _rebase_spy(monkeypatch, seen)
    moves = itertools.cycle(FOLDERS)
    key, secret = a.identities.add_key("Billing CI")
    with a.identities.changing() as changes:
        _, eraser = a.identities.add_token(key.id, "erasure", ["forget"], within=changes)
    person, code = a.identities.add_person("Emily Carter")
    with a.identities.changing() as changes:
        enrolled = a.identities.enrol(code, "laptop", within=changes)
    assert enrolled is not None
    _, token, computer_key = enrolled
    held, held_code = a.identities.add_person("Grace Moretti")
    with a.identities.changing() as changes:
        held_enrolled = a.identities.enrol(held_code, "laptop", within=changes)
    assert held_enrolled is not None
    held_token = held_enrolled[1]
    _on_its_store(monkeypatch, person.id)
    seen.identities = (key.id, person.id, held.id, "Billing CI", "Emily Carter", "Grace Moretti")
    admin = {"x-latet-admin": a.admin.issue("first administrator")}
    with captured_logs(level="debug") as logs:
        async with _serving(a, b) as (one, two):
            # An API key through the applications API: taught on one instance, restored and masked alike on the other.
            await _teach(a, key)
            masked = await _masked(one, secret, TEXT)
            assert _clear_of(masked, *CANARIES)
            back = (await _apps(two, secret, "unmask", {"texts": [masked]}))["results"][0]
            assert back == {"text": TEXT, "restored": 3}
            assert await _masked(two, secret, TEXT) == masked
            assert _clear_of(await _masked(two, secret, f"2026-10-31 ticket closed by {NAME}", "data"), NAME)
            # The key through the gateway: masked on its way, its answer restored.
            said, sent = await _chat(two, upstream, secret, TEXT)
            assert _clear_of(sent, *CANARIES) and all(value in said for value in CANARIES)
            # A person through the gateway, on both instances, her project's folder moving from call to call.
            await _teach(a, person)
            said, sent = await _chat(one, upstream, token, TEXT, next(moves))
            assert _clear_of(sent, *CANARIES, *FOLDERS) and all(value in said for value in CANARIES)
            said, sent = await _chat(two, upstream, token, f"remind {NAME} that {EMAIL} expires", next(moves))
            assert _clear_of(sent, NAME, EMAIL) and NAME in said and EMAIL in said
            # Her Claude Code plugin, in a project of hers, its folder moving from call to call on both instances.
            await _own_projects(a, person)
            for client in (one, two, one):
                assert _clear_of(await _plugin_masked(client, token, TEXT, next(moves)), *CANARIES)
            # The person's calls past a lookup bound: her whole vault read, each new value a change applied to it, and
            # its base made again in the background once the changes pass what a delta holds; the key's too.
            runtime = await a.spaces.runtime(person)
            assert isinstance(runtime.built.vault, StoredVault)
            await until(lambda: runtime.store.index is not None and runtime.store.index.ready, 60)
            with monkeypatch.context() as bound:
                bound.setattr(lookup, "LOOKUP_WORD_ROLES", 1)
                for extra in EXTRAS:
                    said, sent = await _chat(one, upstream, token, f"forward {TEXT} to {extra}", next(moves))
                    assert _clear_of(sent, *CANARIES, extra) and extra in said
                assert _clear_of(await _masked(one, secret, TEXT), *CANARIES)
            assert seen.whole_reads
            await until(lambda: seen.rebases > 0 and not runtime.built.vault._rebasing, 30)
            # A person's vault held whole: the same calls, past a lookup bound too, read from memory on both instances
            # with no whole read; a value of hers forgotten on one instance, and her vault read whole again on the
            # other at its next call (a forget names nothing in the store's log).
            await _teach(a, held)
            reads = seen.whole_reads
            with monkeypatch.context() as bound:
                bound.setattr(lookup, "LOOKUP_WORD_ROLES", 1)
                for client, extra in ((one, EXTRAS[0]), (two, EXTRAS[1])):
                    said, sent = await _chat(client, upstream, held_token, f"forward {TEXT} to {extra}", next(moves))
                    assert _clear_of(sent, *CANARIES, extra) and all(v in said for v in (*CANARIES, extra))
            for server in (a, b):
                vault = (await server.spaces.runtime(held)).built.vault
                assert isinstance(vault, StoredVault) and vault.holds_all and vault.whole_count == 0
            assert seen.whole_reads == reads
            emailed = _store(a, held.id).by_original(EMAIL)
            assert emailed is not None
            assert await asyncio.to_thread((await a.spaces.runtime(held)).forget, emailed.id) is not None
            said, sent = await _chat(two, upstream, held_token, TEXT, next(moves))
            assert _clear_of(sent, *CANARIES) and emailed.surrogate not in sent
            again = (await b.spaces.runtime(held)).built.vault.in_memory(emailed.id)  # met again: a fake of its own
            assert again is None or again.surrogate != emailed.surrogate
            # Restored in the dashboard: the person signed in on the other instance reveals her token.
            await _sign_in(two, token, computer_key)
            entry = _store(a, person.id).by_original(TOKEN)
            assert entry is not None
            revealed = await two.post(f"{API}/vault/{entry.id}/reveal", headers=UI)
            assert revealed.status_code == 200 and revealed.json() == {"original": TOKEN}
            # The words of each needle; those the vault keeps of each value are among them.
            seen.words = {word for needle in NEEDLES for word in words_in(needle, dotted=True)}
            for identity in (key, person, held):
                for value in VALUES:
                    if (found := _store(a, identity.id).by_original(value)) is not None:
                        assert set(words_of(found, "original")) <= seen.words, value
            _stored(a, seen, key.id, person.id, held.id)  # with the entries forgotten next
            # Forgotten: the key's email through the applications API, the person's name in her dashboard.
            assert await _apps(one, eraser, "forget", {"values": [EMAIL]}) == {"forgotten": 1, "unknown": 0}
            named = _store(a, person.id).by_original(NAME)
            assert named is not None
            assert (await two.delete(f"{API}/vault/{named.id}", headers=UI)).status_code == 200
            await until(lambda: _store(a, person.id).by_original(NAME) is None)
            again = await _masked(two, secret, TEXT)
            assert _clear_of(again, *CANARIES) and again != masked
            stale = (await _apps(two, secret, "unmask", {"texts": [masked]}))["results"][0]
            assert EMAIL not in stale["text"]
            # Both vaults as a version before 0.11 left them: the key's rules and prints gone (each rule kept again as
            # its value is met, announced as `rule`; the prints made again in the background, each part announced as
            # `words`), the person's words gone (made again with their prints while her vault is served from memory).
            _stored(a, seen, key.id, person.id)  # with the words deleted next
            with a.db.connection() as conn:
                conn.execute("UPDATE vault_entities SET rule = NULL WHERE identity_id = %s", (key.id,))
                conn.execute("UPDATE vault_words SET print = NULL, counted = NULL WHERE identity_id = %s", (key.id,))
                conn.execute("DELETE FROM vault_words WHERE identity_id = %s", (person.id,))
            for identity in (key, person):
                await a.spaces.drop(identity.id)
            assert _clear_of(await _masked(one, secret, TEXT), *CANARIES)
            said, sent = await _chat(one, upstream, token, TEXT, next(moves))
            assert _clear_of(sent, *CANARIES)
            for identity in (key, person):
                store = (await a.spaces.runtime(identity)).store
                await until(lambda store=store: store.words_complete() and store.prints_complete(), 60)
            told = {m.get("kind") for channel, m in seen.published if channel.startswith("identity:")}
            assert {"rule", "words", "where"} <= told
            # Each instance's last calls, on the vaults made again.
            for client in (one, two):
                assert _clear_of(await _masked(client, secret, TEXT), *CANARIES)
            said, sent = await _chat(two, upstream, token, TEXT, next(moves))
            assert _clear_of(sent, *CANARIES)
            await _beats((a, b), one, admin, seen)
            # The opaque observation uses the same identity owners and wire spy as the full Task 11 scenario.
            # Originals, fakes, words and scope strings become keyed digests before the registry sees them.
            source = await a.spaces.runtime(person)
            target = await b.spaces.runtime(person)
            opaque = " | ".join((*NEEDLES, masked))
            body = {"input": [{"type": "image_generation_call", "result": opaque}]}
            scope = EchoScope((FOLDERS[0], EMAIL), "responses")
            check = EchoCheck(source.built.vault.cache_key, source.mediator.echoes, scope, TOKEN, {})
            capture = OpaqueCapture(check, source.mediator.echoes)
            await source.mediator.run(capture.body, {"output": body["input"]})
            assert await source.mediator.run(capture.commit)
            checked = await target.mediator.run(
                EchoCheck, target.built.vault.cache_key, target.mediator.echoes, scope, TOKEN, body
            )
            assert list(checked.rules(body).values()) == [ECHO_RULE]
            families = {name: a.bus.key("opaque-" + name) for name in ("members", "expiry", "identities", "size")}
            assert all(a.bus.client.exists(key) for key in families.values())
            records = a.bus.client.hgetall(families["members"])
            assert records and all(re.fullmatch(rb"[0-9a-f]{64}:[0-9a-f]{64}", member) for member in records)
            assert all(float(value) > 0 for value in records.values())
            assert set(a.bus.client.zrange(families["expiry"], 0, -1)) == set(records)
            identities = a.bus.client.hgetall(families["identities"])
            assert identities and all(re.fullmatch(rb"[0-9a-f]{64}:[cb]", key) for key in identities)
            assert all(re.fullmatch(rb"[0-9]+", value) for value in identities.values())
            assert int(a.bus.client.get(families["size"])) > 0
            seen.held = valkey_dump(a.bus, prefix)
        seen.held += valkey_dump(a.bus, prefix)  # and once the instances closed their spaces
    seen.logs = logs.getvalue()
    _stored(a, seen, key.id, person.id, held.id)
    for identity in (key, person, held):
        keyed = print_key(root_key(a.db, a.master, identity.id))
        seen.prints |= {printed for word in seen.words for printed in prints(keyed, word)}
    return seen


def _met_by_chance(words: set[str]) -> list[str]:
    """The words a text could hold with no leak: shorter than 6 characters, all hexadecimal digits, or part of a common
    word (``COMMON``)."""
    return sorted(
        word
        for word in words
        if len(word) < 6 or re.fullmatch(r"[0-9a-f]+", word) or any(word in common for common in COMMON)
    )


async def test_valkey_holds_no_value_word_or_print(instances, upstream, prefix, monkeypatch):  # noqa: F811
    """What Valkey holds under the server's prefix once the instances worked, and every byte they sent it meanwhile
    (its commands, pipelines, scripts, publishes and subscriptions: what a reader of Valkey or of its traffic sees),
    hold none of the needles, none of their words and none of their prints, nor any keyed hash the vault keeps in
    PostgreSQL. A whole read of a vault past a bound, and the thread that makes its base again, send Valkey nothing at
    all."""
    seen = await _scenario(instances, upstream, prefix, monkeypatch)
    assert b"conv:" in seen.held and b"instance:" in seen.held  # it holds the conversations and the beats
    # What the spy sees: a pipeline sent at once, a script, a publish, and the ears' subscriptions on the asyncio side.
    assert any(b"MULTI" in sent and b"EXEC" in sent for sent in seen.wire)
    assert any(b"EVALSHA" in sent for sent in seen.wire) and any(b"PUBLISH" in sent for sent in seen.wire)
    assert any(b"SUBSCRIBE" in sent for sent in seen.wire_async)
    assert len(seen.words) > 30 and len(seen.prints) > 100 and len(seen.hashed) > 50
    assert _met_by_chance(seen.words) == []
    assert _in_clear(seen.held, seen) == []
    assert _in_clear(b"\n".join(seen.wire), seen) == []
    assert seen.whole_reads and seen.rebases and seen.from_whole == []


async def test_a_vaults_messages_carry_ids_kinds_and_counts_alone(instances, upstream, prefix, monkeypatch):  # noqa: F811
    """Every message of an identity's channel says a kind of ``KINDS`` and the keys of that kind alone, each an entry's
    id, a space's name, a conversation's keyed name or a count; and no message of any channel (the beats, the
    identities', the feeds', the shared settings') holds a needle, a word, a print or a keyed hash."""
    seen = await _scenario(instances, upstream, prefix, monkeypatch)
    vault = [message for channel, message in seen.published if channel.startswith("identity:")]
    channels = {channel for channel, _ in seen.published if channel.startswith("identity:")}
    assert channels == {f"identity:{identity_id}" for identity_id in seen.identities[:3]}
    assert {message.get("kind") for message in vault} == set(KINDS)  # every kind, each met
    for message in vault:
        kind = message.get("kind")
        assert kind in KINDS and set(message) <= KINDS[kind] and "kind" in message, message
        if "id" in message:
            assert ENTITY_ID.fullmatch(message["id"]), message
        if "from" in message:
            assert SOURCE.fullmatch(message["from"]) and message["from"][:12] in seen.instances, message
        for count in ("counter", "version"):
            if count in message:
                assert type(message[count]) is int and message[count] >= 0, message
        if "conversation" in message:
            assert CONVERSATION.fullmatch(message["conversation"]), message
    for channel, message in seen.published:
        assert _in_clear(json.dumps(message).encode(), seen) == [], channel
        assert not any(name in json.dumps(message) for name in seen.identities[3:]), channel


async def test_a_beat_carries_numbers_and_the_version_alone(instances, upstream, prefix, monkeypatch):  # noqa: F811
    """An instance's beat, as a worker hands it to its supervisor on its pipe, as its key in Valkey and its message on
    the beat's channel hold it, and as the status lists every instance: the fields of ``TELEMETRY`` alone, its id, its
    worker's number, the version, booleans and counts, never an identity, a needle, a word or a print."""
    seen = await _scenario(instances, upstream, prefix, monkeypatch)
    beats = [message for channel, message in seen.published if channel == "beat"]
    assert len(beats) >= 2 and len(seen.piped) == 2 and len(seen.keys) == 2
    said = [json.loads(raw) for _, raw in seen.piped] + [json.loads(raw) for raw in seen.keys] + beats + seen.status
    for k, raw in seen.piped:
        found = json.loads(raw)
        assert beat_checked(raw) == found  # every field what it should be, none set to None by the check
        assert found["instance"] == seen.instances[k] and found["worker"] == k and found["spaces"] >= 1
    assert {row["instance"] for row in seen.status} == set(seen.instances)
    assert sorted(row["worker"] for row in seen.status) == [0, 1]
    for beat in said:
        assert set(beat) == set(TELEMETRY), beat
        assert INSTANCE_ID.fullmatch(beat["instance"]) and beat["instance"] in seen.instances
        assert beat["worker"] == seen.instances.index(beat["instance"])
        assert beat["version"] == __version__
        assert type(beat["ready"]) is bool
        for count in ("memory", "spaces", "index"):
            assert beat[count] is None or (type(beat[count]) is int and beat[count] >= 0), beat
        text = json.dumps(beat)
        assert not any(identity in text for identity in seen.identities), beat
        assert _in_clear(text.encode(), seen) == []


async def test_the_logs_hold_no_canary(instances, upstream, prefix, monkeypatch):  # noqa: F811
    """What the instances logged over the whole scenario, at the debug level: no needle in any of its forms, none of
    their words, none of their prints and no keyed hash of the vault."""
    seen = await _scenario(instances, upstream, prefix, monkeypatch)
    assert "POST /applications 200" in seen.logs  # the calls were logged
    assert _in_clear(seen.logs.encode(), seen) == []


async def test_d2_count_keys_carry_only_source_owned_ids_numbers_and_finite_facts(instances, prefix, monkeypatch):
    """Real request decisions become bounded binary counts; whole-prefix storage/wire keeps no request canary."""
    from latet.config import Config, IdentityConfig
    from latet.engine.build import build
    from latet.engine.vault import Vault
    from latet.gateway.capture import Capture
    from latet.gateway.forward import Forwarder
    from latet.gateway.mediate import Mediator
    from latet.gateway.request_audit import RequestAudit, _d2_decode
    from latet.gateway.routes import RouteTable
    from latet.server.live import ValkeyRequestStatus

    from ..unit.test_request_leaks import _Provider, _request
    from ..unit.test_request_outcomes import OPENAI, _chat

    a, b = instances  # existing two-server fixture; no app lifespan/listener or full three-identity scenario
    assert a.instance != b.instance
    seen = Seen(instances=[a.instance, b.instance])
    seen.published = _publish_spy(monkeypatch, prefix)
    _wire_spy(monkeypatch, seen)
    seen.words = {word for needle in NEEDLES for word in words_in(needle, dotted=True)}
    first, _first_secret = a.identities.add_key("Task11 D2 first key")
    other, _other_secret = a.identities.add_key("Task11 D2 other key")
    assert first.id != other.id
    seen.identities = (first.id, other.id)
    assert all(re.fullmatch(r"u_[a-z2-7]{10}", who) for who in seen.identities)
    assert all(INSTANCE_ID.fullmatch(who) for who in seen.instances)
    published_before_d2 = len(seen.published)
    catalogs_before = {kind: set(fields) for kind, fields in KINDS.items()}

    built = build(Config(ner="off", identity=IdentityConfig(enabled=False)), Vault(b"\x07" * 32))
    mediator = Mediator(built.engine, built.verifier)
    # Remote first, other identity second, local last: final aggregation must sum2+1, excluding other identity's1.
    owners = [(b, first.id, 2), (a, other.id, 1), (a, first.id, 1)]
    audits = [RequestAudit() for _ in owners]
    provider = _Provider()
    publications = []

    def publish_snapshot(audit, server, identity_id):
        # Same genuine SnapshotWork/ValkeyRequestStatus worker APIs, no _projection mutation or synthetic status row.
        work = audit.reserve_snapshot()
        assert work is not None
        try:
            assert work.claim_worker() and work.build(server=True)
            transport = ValkeyRequestStatus(server.bus, identity_id, server.instance)
            keys = transport._keys
            assert transport.publish(work)
            assert transport.aggregate(work)
            projection = json.loads(json.dumps(work.decode()))  # test-owned assertion copy, before work disposal
            return keys, projection
        finally:
            work.worker_finished()
            work.close()

    def base36(raw):
        assert len(raw) == 10 and re.fullmatch(rb"[0-9a-z]{10}", raw)
        value = int(raw, 36)
        assert 0 < value <= 9_999_999_999_999
        return value

    try:
        body = _chat(TEXT + " " + " ".join((*EXTRAS, *FOLDERS)))
        # Source-trace every Task11 needle into the REAL request body, not merely an output-side sentinel.
        source = json.loads(body)["messages"][0]["content"]
        assert all(needle in source for needle in NEEDLES)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            for audit, (server, identity_id, uses) in zip(audits, owners, strict=True):
                gateway = Forwarder(
                    RouteTable([OPENAI]), Capture(None), client, mediator, recorder=None, request_audit=audit
                )
                for _ in range(uses):
                    answered = await gateway.handle(_request(body))
                    assert answered.status_code == 200
                assert audit.sequence == uses
                keys, projection = await asyncio.to_thread(publish_snapshot, audit, server, identity_id)
                assert projection["local"]["counts"] == [[1, 0, 0, uses, uses, uses, 0]]
                publications.append((server.instance, identity_id, keys, projection))
        assert len(provider.sent) == 4, "actual guarded send/producer path must be reached"
        summed = publications[-1][3]["server"]
        assert summed["available"] and summed["published"] and summed["complete"]
        assert summed["contributors"] == 2 and summed["counts"] == [[1, 0, 0, 3, 3, 3, 0]]
        assert summed["overflow"] == summed["removed_headers"] == 0 and summed["saturated"] is False

        owned = {}
        deadlines = {}
        for instance, identity_id, keys, projection in publications:
            pair = (instance + identity_id[2:]).encode("ascii")
            assert keys == (
                prefix + "request-status:size",
                prefix + "request-status:expiry",
                prefix + "request-status:identity:" + identity_id,
                prefix + "request-status:row:" + instance + ":" + identity_id,
            )
            raw = a.bus.client.get(keys[3])
            assert a.bus.client.type(keys[3]) == b"string" and type(raw) is bytes
            assert len(raw) == 91 and re.fullmatch(rb"[0-9]{13}\n", raw[:14])
            stamp = int(raw[:13])
            assert 0 < a.bus.client.pttl(keys[3]) <= 30000
            seconds, micros = a.bus.client.time()
            assert 0 < stamp - (seconds * 1000 + micros // 1000) <= 30000
            # Independent exact byte grammar for this actual producer's one-builtin/no-exception row.
            local = projection["local"]
            assert raw[14:18] == b"\x02\x00\x01\x00"
            assert raw[18:34] == bytes.fromhex(local["epoch"])
            assert [int.from_bytes(raw[at : at + 8], "big") for at in (34, 42, 50)] == [local["sequence"], 0, 0]
            assert raw[58] == 1
            assert [int.from_bytes(raw[at : at + 8], "big") for at in (59, 67, 75, 83)] == local["counts"][0][3:]
            decoded = _d2_decode(raw)
            assert decoded == (stamp, local)
            deadlines[pair] = stamp
            owned[keys[3].encode()] = raw

        sizes_key, expiry_key = publications[0][2][:2]
        sizes = a.bus.client.get(sizes_key)
        expiry = a.bus.client.get(expiry_key)
        assert len(sizes) == len(expiry) == 96
        pairs = [sizes[at : at + 22] for at in range(0, 96, 32)]
        assert pairs == sorted(deadlines)
        assert {sizes[at : at + 22]: base36(sizes[at + 22 : at + 32]) for at in range(0, 96, 32)} == deadlines
        records = [expiry[at : at + 32] for at in range(0, 96, 32)]
        assert records == sorted(records)
        assert {row[10:32]: base36(row[:10]) for row in records} == deadlines
        owned[sizes_key.encode()], owned[expiry_key.encode()] = sizes, expiry
        for identity_id in seen.identities:
            key = prefix + "request-status:identity:" + identity_id
            directory = a.bus.client.get(key)
            expected = {pair[:12]: stamp for pair, stamp in deadlines.items() if pair[12:] == identity_id[2:].encode()}
            assert len(directory) == 22 * len(expected)
            entries = [directory[at : at + 22] for at in range(0, len(directory), 22)]
            assert [row[:12] for row in entries] == sorted(expected)
            assert {row[:12]: base36(row[12:]) for row in entries} == expected
            owned[key.encode()] = directory
        for key in owned:
            assert a.bus.client.type(key) == b"string"
            if b":row:" not in key:
                assert a.bus.client.pttl(key) == -1
        assert set(a.bus.client.scan_iter(match=prefix + "*", count=100)) == set(owned)
        assert len(owned) == 7  # size+expiry+two identity directories+three instance/identity rows
        seen.held = valkey_dump(a.bus, prefix)
        assert _met_by_chance(seen.words) == []
        assert _in_clear(seen.held, seen) == []
        assert _in_clear(b"\n".join(seen.wire), seen) == []
        assert seen.published[published_before_d2:] == [], "D2 storage must not invent a pub/sub kind"
        assert catalogs_before == KINDS
    finally:
        for audit in audits:
            audit.close()
        mediator.close()  # existing wait=False policy; runner owns final native/descendant completion
    assert all(audit.retained_bytes == 0 for audit in audits)


def test_launch_binding_valkey_keeps_only_one_sealed_string(protected_core, monkeypatch):
    """Task11's raw-value and representation canaries on the actual launch storage family."""
    from latet.gateway.launch_keys import BINDING_BYTES
    from latet.projects import project_id
    from latet.server.launch_binding import Binding, RemoteResult, binding_key
    from latet.server.launches import Receipt
    from latet.server.plugin_bindings import PluginBindings
    from latet.server.sealing import FORMAT

    db, bus, authority, codec, _master = protected_core
    entropy = secrets.token_bytes(32)
    credential = b"latet_lk_" + base64.urlsafe_b64encode(entropy).rstrip(b"=")
    locator, launch_id = authority.locator(credential), secrets.token_bytes(16)
    root = "/task11-vault-canary/zibellinovésta"
    folder = root + "/quorvantile"
    project = project_id(root)
    session = "cc:task11-launched-session-quorvantile"
    binding = Binding(folder, root, project, "server", "posix")
    verifier = codec.verifier(credential)
    needles = (
        entropy,
        credential[9:],
        *(form for value in (credential.decode("ascii"), folder, root, project, session) for form in _forms(value)),
    )
    publish = Bus.publish
    published = 0

    def counted_publish(owner, channel, message):
        nonlocal published
        if owner is bus:
            published += 1
        return publish(owner, channel, message)

    monkeypatch.setattr(Bus, "publish", counted_publish)

    def snapshot(receipt: Receipt) -> None:
        key = binding_key(bus, receipt.address.epoch, locator)
        expected = f"{bus.prefix}launch:{receipt.address.epoch.hex()}:{locator.hex()}".encode("ascii")
        keys = list(itertools.islice(bus.client.scan_iter(match=bus.prefix + "*", count=100), 2))
        exact_inventory = key == expected and keys == [key]
        assert exact_inventory
        assert bus.client.type(key) == b"string" and 0 < bus.client.pttl(key) <= 60_000
        blob = bus.client.getrange(key, 0, BINDING_BYTES)
        assert type(blob) is bytes and 30 <= len(blob) <= BINDING_BYTES
        assert blob[:2] == bytes((FORMAT, 1))
        fields = (*receipt.address.fields(), verifier, *binding.fields())
        assert len(fields) == 13 and len(blob) == 30 + sum(4 + len(field) for field in fields)
        expected_contents = codec.open(key, receipt.address, blob) == (verifier, binding)
        assert expected_contents
        clear = any(needle in key or needle in blob for needle in needles)
        assert not clear, "launch canary found in raw Valkey key or string"

    pending = _sent(authority, authority.reserve_open(_ISSUER, locator, launch_id, binding, verifier, codec))
    assert pending.receipt is not None and pending.receipt.address.generation == 0
    snapshot(pending.receipt)
    live = _sent(authority, authority.reserve_hold(_ISSUER, launch_id, 0, codec))
    assert live.receipt is not None and live.receipt.address.generation == 1
    store = PluginBindings(db, _ISSUER, b"r" * 32)
    assert store.refresh(session, project, root, required=True)
    plugin = store.read(session)
    assert plugin is not None and plugin.required
    snapshot(live.receipt)

    assert authority.terminal(_ISSUER, locator, 1, exit_confirmed=True)
    cleanup = authority.reserve_cleanup(_ISSUER, locator)
    assert cleanup is not None and authority.submit(cleanup).wire.remote == RemoteResult.DELETE
    assert authority.retire(_ISSUER, locator, 1)
    assert next(bus.client.scan_iter(match=bus.prefix + "*", count=100), None) is None
    assert published == 0, "launch storage must not publish an identity message"
