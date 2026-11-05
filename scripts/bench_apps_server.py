"""The measurements of the applications API behind its documentation (notes/apps-api-plan.md, Task 10), on a Latet
server opened in this process the way the server's tests open one (``tests/server/support.py``): on the test services
(`uv run python scripts/server-services.py up`, then the two variables `env` prints), in a schema and a Valkey prefix
of its own, with no models (no name model, no text recognition). Never a real server, nor through `latet server`
commands.

    uv run python scripts/bench_apps_server.py build --values 200000   # a schema, an API key, its vault filled
    uv run python scripts/bench_apps_server.py --folder F build --person --values 200000   # a person's, for
        # bench_engine.py person (Task 9): no token, and none of the commands below
    uv run python scripts/bench_apps_server.py serve --working 5000 -- --lines 40000 --documents 0 --no-unmask
    uv run python scripts/bench_apps_server.py serve --new-key --analyze 20     # an API key of its own, empty
    uv run python scripts/bench_apps_server.py serve --new-key --held 0 -- --lines 5000 --session-tokens  # none held
    uv run python scripts/bench_apps_server.py serve --profile -- --lines 1000  # and where a call's time goes
    uv run python scripts/bench_apps_server.py words      # 2 MB of Chinese: its words, their hashing, the store
    uv run python scripts/bench_apps_server.py fanout     # the values a log line opens, at 200,000 and 1,000,000
    uv run python scripts/bench_apps_server.py weigh      # which of the values found a text holds
    uv run python scripts/bench_apps_server.py unmasks    # unmasks of several keys at once, and the other work
    uv run python scripts/bench_apps_server.py missing    # what a key's space build asks PostgreSQL first
    uv run python scripts/bench_apps_server.py workers --counts 1 2 4 8   # `latet server run --workers N`, one port:
        # each count over a fixed time (--seconds), four connections a worker (--per-worker), the same lines
    uv run python scripts/bench_apps_server.py drop       # the schema and the prefix removed

``serve`` serves the server's app (what `latet server run` serves) on a port of 127.0.0.1 with uvicorn, and runs
``scripts/bench_apps.py`` against it in a process of its own, with the options after ``--``; then says where a call's
time went in the server (the store's lookups, the weighing). What ``build`` made the others use is kept in a folder
(``--folder``): the schema's name, the master key it was made with, the API key's token; ``drop`` removes it too."""

from __future__ import annotations

import argparse
import contextlib
import cProfile
import itertools
import json
import math
import os
import pstats
import random
import secrets
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent

import bench_apps  # noqa: E402
import httpx  # noqa: E402
import psycopg  # noqa: E402
import uvicorn  # noqa: E402
from psycopg import sql  # noqa: E402
from terms_bench import memory_mb  # noqa: E402

from latet.bridge.apps import Apps  # noqa: E402
from latet.engine import modules  # noqa: E402
from latet.engine import stored as stored_module  # noqa: E402
from latet.engine.anchors import ANCHORS_VERSION, anchor, words_in  # noqa: E402
from latet.engine.types import EntityClass  # noqa: E402
from latet.engine.vault import Entity, Vault  # noqa: E402
from latet.server.app import Server, open_server  # noqa: E402
from latet.server.audit import AuditLog  # noqa: E402
from latet.server.db import Database, migrate  # noqa: E402
from latet.server.master import MasterKey, root_key  # noqa: E402
from latet.server.operations import server_app  # noqa: E402
from latet.server.settings import KEY_HELD_SECRETS, KEY_WORKING_VALUES, SERVER_API, ServerSettings  # noqa: E402
from latet.server.vault import (  # noqa: E402
    _COUNT,
    _EDGES,
    _FAKE,
    _INSERT,
    _MISSING,
    _NULL_WORDS,
    _VALUE,
    _WORDS,
    CANDIDATES_AT_MOST,
    WORDS_AT_ONCE,
    PgVaultStore,
)

_NUL = chr(0)
KEY_LIMIT = 10_000_000  # the bench's API key's own vault limit: past the server's 200,000
_HERE = {"127.0.0.1", "::1"}


# The test services and the server's address, as the server's tests read them (``tests/server/support.py``, whose
# package imports pytest: the measurements also run where no development tool is installed, the container image).
DATABASE = os.environ.get("LATET_TEST_DATABASE_URL", "")
VALKEY = os.environ.get("LATET_TEST_VALKEY_URL", "")
URL = "https://latet.acme.lan"


# --- the server -------------------------------------------------------------------------------------


def _on_this_computer() -> None:
    """The test services' two addresses, set and on this computer, as the server's tests require them."""
    if not DATABASE or not VALKEY:
        raise SystemExit(
            "LATET_TEST_DATABASE_URL and LATET_TEST_VALKEY_URL are not set: `uv run python scripts/server-services.py "
            "up` starts the test services, and `env` prints them"
        )
    for name, address in (("LATET_TEST_DATABASE_URL", DATABASE), ("LATET_TEST_VALKEY_URL", VALKEY)):
        if urlsplit(address).hostname not in _HERE:
            raise SystemExit(f"{name} names another computer: the measurements run on the test services alone")


@dataclass
class State:
    """What ``build`` made, kept in ``folder``: the schema and the prefix, the master key, the API key and its token
    (``kind`` "person": a person's identity, its vault filled alike, no token)."""

    folder: Path
    schema: str = ""
    prefix: str = ""
    key: str = ""
    values: int = 0
    seed: int = 1
    kind: str = "key"

    @property
    def master(self) -> Path:
        return self.folder / "master.key"

    @property
    def token(self) -> Path:
        return self.folder / "token.txt"

    @property
    def models(self) -> Path:
        return self.folder / "models"  # none: no name model, no text recognition

    def save(self) -> None:
        found = {"schema": self.schema, "prefix": self.prefix, "key": self.key, "values": self.values}
        (self.folder / "state.json").write_text(json.dumps({**found, "seed": self.seed, "kind": self.kind}), "utf-8")

    @classmethod
    def load(cls, folder: Path) -> State:
        path = folder / "state.json"
        if not path.exists():
            raise SystemExit(f"nothing was built in {folder}: `build` first")
        return cls(folder, **json.loads(path.read_text("utf-8")))


def _secret_file(path: Path, text: str) -> None:
    """``text`` written to ``path``, which only this user reads (a test key and a test token, all the same)."""
    path.write_text(text, "utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def open_bench_server(
    state: State, working: int = KEY_WORKING_VALUES, url: str = URL, held: int = KEY_HELD_SECRETS
) -> Server:
    """An instance of a Latet server in the state's schema and prefix, as ``open_test_server`` opens one, with the
    state's master key (kept from one command to the next), ``working`` values of an API key's vault in memory, and
    ``held`` secrets held glued (``key_held_secrets``), and no models; ``url`` its address."""
    state.models.mkdir(exist_ok=True)
    settings = ServerSettings(
        url=url,
        database_url=DATABASE,
        valkey_url=VALKEY,
        master_key_file=state.master,
        key_working_values=working,
        key_held_secrets=held,
    )
    db = Database(DATABASE, schema=state.schema, max_size=1)
    try:
        migrate(db, master=MasterKey.load(settings))
    finally:
        db.close()
    return open_server(settings, schema=state.schema, prefix=state.prefix, data=state.models)


@contextlib.contextmanager
def serving(
    state: State, working: int = KEY_WORKING_VALUES, held: int = KEY_HELD_SECRETS
) -> Iterator[tuple[Server, str]]:
    """An instance (``open_bench_server``) whose app (what `latet server run` serves: ``server_app``) is served by
    uvicorn on a port of 127.0.0.1, its address (the gate takes the calls made to its own address alone), in a thread
    with an event loop of its own, as the server's tests serve it (``test_two_instances._on_uvicorn``): its lifespan
    starts the instance's ear and beat, and closes its spaces at the end. The instance, and its address."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        server = open_bench_server(state, working, url, held)
    except BaseException:
        sock.close()
        raise
    config = uvicorn.Config(
        server_app(server), log_config=None, log_level=None, proxy_headers=True, forwarded_allow_ips="127.0.0.1"
    )
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    try:
        while not running.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise SystemExit("uvicorn did not start")
            time.sleep(0.02)
        yield server, url
    finally:
        running.should_exit = True
        thread.join(60)
        sock.close()
        server.close()


def _new_key(server: Server, name: str) -> tuple[str, str]:
    """An API key with a vault limit of ``KEY_LIMIT``: its id and its token."""
    identity, secret = server.identities.add_key(name)
    server.identities.limit_vault(identity.id, KEY_LIMIT)
    return identity.id, secret


def _bearer(token: Path) -> dict[str, str]:
    return {"authorization": f"Bearer {token.read_text('utf-8').strip()}"}


def _store(server: Server, key: str) -> PgVaultStore:
    return PgVaultStore(server.db, key, root_key(server.db, server.master, key), limit=KEY_LIMIT)


# --- the vault filled -------------------------------------------------------------------------------


def keep_all(store: PgVaultStore, entities: Sequence[Entity], said: Callable[[int], None] = lambda _: None) -> None:
    """``entities`` kept in ``store`` as ``PgVaultStore.save_entity`` keeps each (the same statements: the entry, its
    two strings, its two words and its edges, each word with its print), a few thousand in a transaction, each
    transaction's values counted once and their word rows stamped with that count, as an applications call's are."""
    sealer, identity = store._sealer, store.identity
    for start in range(0, len(entities), WORDS_AT_ONCE):
        rows, fakes, values, words, edged = [], [], [], [], []
        for e in entities[start : start + WORDS_AT_ONCE]:
            rows.append((
                identity, e.id, e.etype, int(e.eclass),
                sealer.seal(e.original.encode(), store._aad(e.id, "original", e.etype, e.eclass)),
                sealer.keyed(f"original{_NUL}{e.original}"),
                sealer.seal(e.surrogate.encode(), store._aad(e.id, "surrogate", e.etype, e.eclass)),
                sealer.keyed(f"surrogate{_NUL}{e.surrogate}"), e.counter, e.created, modules.kept(e.rule),
                e.decoded, store._generations, store.generation,
            ))  # fmt: skip
            fakes.append((identity, sealer.keyed(e.surrogate), e.id))
            values.append((identity, sealer.keyed(e.original), e.id))
            (value_word, value_print), (fake_word, fake_print) = store._words(e)
            words.append((
                identity, e.id, value_word, ANCHORS_VERSION, value_print,
                identity, e.id, fake_word, ANCHORS_VERSION, fake_print,
            ))  # fmt: skip
            its = store._edges(e, "original") + store._edges(e, "surrogate")
            if its:
                roles, hashed, printed = (list(column) for column in zip(*its, strict=True))
                edged.append((identity, e.id, ANCHORS_VERSION, roles, hashed, printed))
        with store.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.executemany(_INSERT, rows)
            cursor.executemany(_FAKE, fakes)
            cursor.executemany(_VALUE, values)
            cursor.executemany(_WORDS, words)
            if edged:
                cursor.executemany(_EDGES, edged)
            conn.execute(_COUNT, (identity, 1, identity, [row[1] for row in rows]))
        said(start + len(rows))


def fill(server: Server, key: str, values: int, seed: int) -> int:
    """The key's vault filled with ``values`` values as its space would keep them, the oldest first: the public IPv4
    addresses the lines name (``bench_apps.addresses``), then the run's customers in the order a run names them first
    (``bench_apps.Customers``: a run of the same seed finds them in the store), each address at a company with its
    domain. Their fakes are made by a vault of the key's root key, as its space makes them. How many it holds."""
    root = root_key(server.db, server.master, key)
    made: list[Entity] = []

    def keep(entity: Entity) -> None:
        made.append(entity)

    vault = Vault(root, on_insert=keep)
    for address in bench_apps.addresses():
        vault.intern("IP_ADDRESS", EntityClass.PII, address, rule="pii:ipv4")
    people = bench_apps.Customers(seed)
    while len(made) < values:
        vault.intern("EMAIL", EntityClass.PII, people.next(), rule="pii:email")
    started = time.perf_counter()
    keep_all(_store(server, key), made, lambda done: print(f"  {done} kept", end="\r", flush=True))
    print(f"  {len(made)} values kept in {time.perf_counter() - started:.0f} s ({len(people.named)} customers)")
    return len(made)


def build(args: argparse.Namespace) -> None:
    folder: Path = args.folder
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "state.json").exists():
        raise SystemExit(f"{folder} holds a build already: `drop` it first")
    state = State(folder, "latet_bench_" + secrets.token_hex(5), "latet_bench_" + secrets.token_hex(5) + ":")
    state.seed = args.seed
    _secret_file(state.master, MasterKey.generate())
    state.save()  # first: `drop` finds the schema whatever happens next
    server = open_bench_server(state)
    try:
        if args.person:  # a person's vault (Task 9): its space is measured by bench_engine.py person
            state.kind = "person"
            state.key = server.identities.add_person("Bench person")[0].id
        else:
            state.key, secret = _new_key(server, "bench")
            _secret_file(state.token, secret)
        state.values = fill(server, state.key, args.values, args.seed)
        state.save()
        with server.db.connection() as conn:
            size = conn.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()
        print(f"schema {state.schema}: {state.values} values; the database is {size[0] if size else '?'} now")
    finally:
        server.close()


def drop(args: argparse.Namespace) -> None:
    state = State.load(args.folder)
    with psycopg.connect(DATABASE, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(state.schema)))
    import redis

    from latet.server.bus import redis_url

    client = redis.Redis.from_url(redis_url(VALKEY))
    keys = list(client.scan_iter(match=state.prefix + "*", count=1000))
    if keys:
        client.delete(*keys)
    client.close()
    for path in (state.master, state.token, args.folder / "token-new.txt", args.folder / "state.json"):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    with contextlib.suppress(OSError):  # left when something else is in them
        state.models.rmdir()
        args.folder.rmdir()
    print(f"schema {state.schema} dropped, {len(keys)} keys of its prefix removed")


# --- where a call's time goes -------------------------------------------------------------------------


@dataclass
class Spent:
    """What the server spent on some work, as ``timed`` adds it up: calls, seconds, and counts."""

    calls: int = 0
    seconds: float = 0.0
    counts: Counter[str] = field(default_factory=Counter)
    largest: int = 0


class Timed:
    """Methods of the server's classes wrapped, while it lasts, to add up their calls and their time (``Spent``, by
    name): the store's lookups by words and the weighing of what they found."""

    def __init__(self) -> None:
        self.spent: dict[str, Spent] = defaultdict(Spent)
        self._lock = threading.Lock()
        self._undo: list[Callable[[], None]] = []

    def add(self, name: str, seconds: float, **counts: int) -> None:
        with self._lock:
            spent = self.spent[name]
            spent.calls += 1
            spent.seconds += seconds
            spent.counts.update(counts)
            spent.largest = max(spent.largest, counts.get("found", 0))

    def wrap(self, owner: type, method: str, name: Callable[..., str], **counted: Callable[..., int]) -> None:
        real = getattr(owner, method)
        timed = self

        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            answer = real(*args, **kwargs)
            took = time.perf_counter() - started
            timed.add(name(*args, **kwargs), took, **{key: count(answer, *args) for key, count in counted.items()})
            return answer

        setattr(owner, method, wrapped)
        self._undo.append(lambda: setattr(owner, method, real))

    def __enter__(self) -> Timed:
        return self

    def __exit__(self, *_: object) -> None:
        for undo in reversed(self._undo):
            undo()

    def report(self) -> None:
        for name, spent in sorted(self.spent.items()):
            counts = ", ".join(f"{key} {value}" for key, value in sorted(spent.counts.items()))
            each = spent.seconds / spent.calls * 1000 if spent.calls else 0.0
            print(
                f"  {name:<28} {spent.calls:>6} calls {spent.seconds:>8.2f} s ({each:.1f} ms each)"
                f"  {counts}; most found at once {spent.largest}"
            )


def timing() -> Timed:
    """The store's lookups by words (by role: the words asked for, the entries found) and the weighing (its detector
    made, and the texts read against it), the space's vault made (its working set read), and the words' backfill."""
    found = Timed()
    found.wrap(
        PgVaultStore, "with_words", lambda _self, role, *_: f"store.with_words {role}",
        words=lambda _answer, _self, _role, words: len(set(words)), found=lambda answer, *_: len(answer),
    )  # fmt: skip
    found.wrap(
        PgVaultStore, "bounded_words", lambda _self, role, *_: f"store.bounded_words {role}",
        words=lambda _answer, _self, _role, words, _budget: len(set(words)), found=lambda answer, *_: len(answer),
    )  # fmt: skip
    found.wrap(stored_module._Weighing, "__init__", lambda *_args, **_kw: "weighing made")
    found.wrap(stored_module._Weighing, "held_in", lambda *_: "weighing read", found=lambda answer, *_: len(answer))
    found.wrap(stored_module.StoredVault, "__init__", lambda *_args, **_kw: "space's vault made")
    found.wrap(PgVaultStore, "backfill_words", lambda *_: "store.backfill_words")
    found.wrap(AuditLog, "record", lambda *_args, **_kw: "administration log, an entry")
    return found


class Sampled:
    """Where the spaces' bridge threads (``latet-bridge``) are, every few milliseconds while it lasts: each function
    counted once in each sample it is in the stack of (its share of the time, what it called included), and the
    function the thread is in (its own share). Lighter than a profile, which slows what it measures."""

    def __init__(self, every: float = 0.005) -> None:
        self.every = every
        self.samples = 0
        self.inclusive: Counter[str] = Counter()
        self.own: Counter[str] = Counter()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bench-sampler", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            names = {thread.ident: thread.name for thread in threading.enumerate()}
            for ident, frame in sys._current_frames().items():
                if not names.get(ident, "").startswith("latet-bridge"):
                    continue
                code = frame.f_code
                if code.co_filename.endswith(("threading.py", "queue.py")) or code.co_name == "_worker":
                    continue  # its pool's thread, waiting for work
                self.samples += 1
                self.own[_where(frame)] += 1
                seen: set[str] = set()
                while frame is not None:
                    where = _where(frame)
                    if where not in seen:
                        seen.add(where)
                        self.inclusive[where] += 1
                    frame = frame.f_back

    def __enter__(self) -> Sampled:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()

    def report(self, top: int = 25) -> None:
        if not self.samples:
            return
        print(f"where the bridge threads were busy ({self.samples} samples, every {self.every * 1000:.0f} ms):")
        print("  with what they called:")
        for where, count in self.inclusive.most_common(top):
            print(f"    {count / self.samples:>6.1%}  {where}")
        print("  in the function itself:")
        for where, count in self.own.most_common(top // 2):
            print(f"    {count / self.samples:>6.1%}  {where}")


def _where(frame) -> str:
    name = frame.f_code.co_filename.replace(chr(92), "/")
    for marker in ("/src/latet/", "/site-packages/", "/Lib/"):
        if marker in name:
            name = name.split(marker, 1)[1]
            break
    return f"{name}:{frame.f_code.co_name}"


@contextlib.contextmanager
def profiled(on: bool) -> Iterator[dict[str, cProfile.Profile]]:
    """While it lasts (``on``), the work of mask and unmask in the space's bridge thread (``Apps._mask``,
    ``Apps._unmask``) profiled, each in a profile of its own."""
    profiles: dict[str, cProfile.Profile] = {}
    if not on:
        yield profiles
        return
    undo = []
    for name in ("_mask", "_unmask"):
        real = getattr(Apps, name)
        profile = profiles[name.lstrip("_")] = cProfile.Profile()

        def wrapped(self, *args, _real=real, _profile=profile):
            _profile.enable()
            try:
                return _real(self, *args)
            finally:
                _profile.disable()

        setattr(Apps, name, wrapped)
        undo.append((name, real))
    try:
        yield profiles
    finally:
        for name, real in undo:
            setattr(Apps, name, real)


@contextlib.contextmanager
def analyzing(server: Server, every: float) -> Iterator[None]:
    """While it lasts, the vault's two tables analyzed every ``every`` seconds: PostgreSQL's statistics then know a
    new key's rows as they come, which its own analysis, once a tenth of a table has changed, learns late."""
    stop = threading.Event()

    def run() -> None:
        while not stop.wait(every):
            with contextlib.suppress(Exception), server.db.connection() as conn:
                conn.execute("ANALYZE vault_entities, vault_words")

    thread = threading.Thread(target=run, name="bench-analyze", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def ready_identity(server, url, token, identity):
    """Opening request through observed background publication; the 600s includes the HTTP request."""
    from bench_engine import Load, MemoryObservation

    def store():
        space = server.spaces._spaces.get(identity)
        return space.parts.vault if space else None

    with MemoryObservation(store) as memory:
        load = Load()
        started = time.perf_counter()
        deadline = time.monotonic() + 600
        with httpx.Client(base_url=url, timeout=600) as client:
            response = client.post(
                "/_latet/apps/v1/scan",
                json={"texts": [""]},
                headers={"Authorization": f"Bearer {token.read_text().strip()}"},
            )
            response.raise_for_status()
        while True:
            found = store()
            index = found.index if found else None
            if time.monotonic() >= deadline:
                raise RuntimeError("the benchmark's key index did not become ready")
            if index is not None and index.ready:
                break
            time.sleep(0.05)
        seconds = time.perf_counter() - started
        cpu = load.sample()
    report = {
        "measurement": "identity-opening-through-background-ready",
        "seconds": seconds,
        "poll_interval_seconds": 0.05,
        "cpu": cpu,
        "population": found.count_entities(),
        "published_array_bytes": index.nbytes,
        "watermark": index.watermark,
        "prints_complete_after": found.prints_complete(),
        "memory": memory.report(),
    }
    report["qualified"] = cpu["status"] == "ok" and report["prints_complete_after"] and report["memory"]["qualified"]
    print(json.dumps(report, allow_nan=False), flush=True)
    if not report["qualified"]:
        raise RuntimeError("readiness measurement unavailable or incomplete")
    return report


def _digest(value):
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fixture_source():
    import hashlib

    paths = (
        "scripts/bench_apps.py",
        "scripts/bench_apps_server.py",
        "scripts/bench_engine.py",
        "src/latet/bridge/apps.py",
        "src/latet/gateway/mediate.py",
        "src/latet/engine/pipeline.py",
        "src/latet/engine/stored.py",
        "src/latet/server/vault.py",
        "src/latet/server/index.py",
    )
    return {path: hashlib.sha256((HERE.parent / path).read_bytes()).hexdigest() for path in paths}


def _fixture_state(space):
    from latet.runtime import digest

    store = space.parts.vault
    with store.db.connection() as connection:
        row = connection.execute("SELECT counter FROM change_counters WHERE owner = %s", (store.identity,)).fetchone()
    return {
        "counter": int(row[0]) if row else 0,
        "population": store.count_entities(),
        "config_sha256": digest(space.runtime.config),
    }


def _require_answers(measured, count):
    if (
        measured.refused
        or measured.blocked
        or measured.degraded
        or len(measured.answers) != count
        or any(answer is None for answer in measured.answers)
    ):
        raise RuntimeError("cached workload response failed its masking/cardinality precondition")


def _known_cleartext_check(vault, texts, answers):
    from bench_engine import _read

    found = 0
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        for text, answer in zip(texts, answers, strict=True):
            reading = vault.matchers.for_text(text)
            found += len(_read(text, reading.known))
            if _read(answer, reading.known):
                raise RuntimeError("a known original remains in the masked fixture")
            reading.seen()
    if not found:
        raise RuntimeError("cached workload has no known spans")
    return found


@contextlib.contextmanager
def cached_observation(engine, masker, texts):
    """Delegate exactly once per actual Apps decision; never probe Engine.cached ahead of the request."""
    ordinal = {text: at for at, text in enumerate(texts)}
    if len(ordinal) != 501:
        raise ValueError("fixture must contain 501 distinct full texts")
    observed = {"decisions": [], "joined": []}
    cached, joined = engine.cached, masker.joined

    def observe_cached(text, *args, **kwargs):
        result = cached(text, *args, **kwargs)
        observed["decisions"].append([ordinal.get(text, -1), bool(result)])
        return result

    def observe_joined(unread, *args, **kwargs):
        observed["joined"].append([ordinal.get(text, -1) for text in unread])
        return joined(unread, *args, **kwargs)

    engine.cached, masker.joined = observe_cached, observe_joined
    try:
        yield observed
    finally:
        engine.cached, masker.joined = cached, joined


def require_cached_proof(proof, arm, repeats=1):
    """The cache's actual decisions and the joined reads of the arm: 500 texts from the cache and the new one read, in
    one request; or the new one alone, read each of the ``repeats`` times it was sent (``repeated``)."""
    if arm == "500+1":
        expected = {"decisions": [[at, True] for at in range(500)] + [[500, False]], "joined": [[500]]}
        if repeats != 1:
            raise RuntimeError("actual cache/joined evidence did not match the selected arm")
    else:
        expected = {"decisions": [[500, False]] * repeats, "joined": [[500]] * repeats}
    if proof != expected:
        raise RuntimeError("actual cache/joined evidence did not match the selected arm")


# The one text's arm of the cached comparison is timed as one window of the same request sent back to back
# (``repeated``), this many seconds at least: one request of a few milliseconds is shorter than the clock that counts
# the processors' time (15.6 ms on Windows), whose deltas then say nothing of it, or nothing true (the Task 8
# re-measurement's cached arms were blocked so, at 6 to 7 ms a request). A tenth of a second is six of its ticks.
CACHED_WINDOW_SECONDS = 0.1
CACHED_REPEATS_AT_MOST = 1_000


def repeated(send, between, *, window=None, at_most=CACHED_REPEATS_AT_MOST, clock=time.perf_counter):
    """``send()`` back to back, ``between()`` before each but the first, until ``window`` seconds have passed
    (``CACHED_WINDOW_SECONDS`` when not given) and two at least were sent, or ``at_most`` were: timed as one window,
    its clock and the processors' time (``bench_engine.Load``) read once at each end. What each send gave, and the
    window: how many, its seconds, the processors' raw deltas over it, and each request's share of both (the window's
    over their number; the percentage is the window's)."""
    from bench_engine import Load

    window = CACHED_WINDOW_SECONDS if window is None else window
    load = Load()
    started = clock()
    results = []
    while True:
        if results:
            between()
        results.append(send())
        took = clock() - started
        if len(results) >= at_most or (len(results) >= 2 and took >= window):
            break
    cpu = load.sample()
    repeats = len(results)

    def share(value):
        return value / repeats if type(value) in (int, float) else None

    per_request = {
        "seconds": took / repeats,
        "cpu_idle": share(cpu.get("idle")),
        "cpu_total": share(cpu.get("total")),
        "cpu_percent": cpu.get("percent"),
    }
    return results, {"repeats": repeats, "seconds": took, "cpu": cpu, "per_request": per_request}


def cached_fixture(args):
    """One explicit, untimed preparation, then a frozen fixture shared by all six fresh serving processes."""
    from bench_engine import atomic_record

    state = State.load(args.folder)
    if args.output.exists():
        raise ValueError("fixture output already exists")
    texts = bench_apps.lines(501, state.seed, 0.5)
    if len(set(texts)) != 501:
        raise ValueError("full texts are not distinct")
    with serving(state, 5000, KEY_HELD_SECRETS) as (server, url):
        ready_identity(server, url, state.token, state.key)
        original_state = _fixture_state(server.spaces._spaces[state.key])
        with httpx.Client(
            base_url=url, headers={"authorization": f"Bearer {bench_apps._token(state.token)}"}, timeout=600
        ) as client:
            masked = bench_apps.run(client, "mask", texts, 501, "data", workload="cached-fixture-preparation")
            _require_answers(masked, 501)
            restored = bench_apps.run(client, "unmask", masked.answers, 501, None)
            _require_answers(restored, 501)
            if restored.answers != texts:
                raise RuntimeError("fixture does not restore exactly")
        space = server.spaces._spaces[state.key]
        known = _known_cleartext_check(space.runtime.built.vault, texts, masked.answers)
        if _fixture_state(space) != original_state:
            raise RuntimeError("fixture preparation taught new values; population is not the required known fixture")
        record = {
            "version": "task8-cached-v1",
            "schema": state.schema,
            "identity": state.key,
            "seed": state.seed,
            "working": 5000,
            "held": KEY_HELD_SECRETS,
            "kind": "data",
            "texts_sha256": _digest(texts),
            "answer_hashes": [_digest(answer) for answer in masked.answers],
            "known_spans": known,
            "state": _fixture_state(space),
            "source": _fixture_source(),
        }
    atomic_record(args.output, record)


def cached_arm(args, server, url, state):
    from bench_engine import atomic_record

    if args.new_key or args.working != 5000 or args.held != KEY_HELD_SECRETS or not args.wait_index:
        raise ValueError("cached arms require the frozen identity, working5000, default held and ready index")
    if args.cached_output is None or args.cached_output.exists() or args.cached_fixture is None:
        raise ValueError("cached arm requires an existing fixture and fresh result path")
    fixture = json.loads(args.cached_fixture.read_text("utf-8"))
    texts = bench_apps.lines(501, state.seed, 0.5)
    space = server.spaces._spaces[state.key]
    signature = {
        "version": "task8-cached-v1",
        "schema": state.schema,
        "identity": state.key,
        "seed": state.seed,
        "working": args.working,
        "held": args.held,
        "kind": "data",
        "texts_sha256": _digest(texts),
        "state": _fixture_state(space),
        "source": _fixture_source(),
    }
    if any(fixture.get(key) != value for key, value in signature.items()):
        raise RuntimeError("cached fixture differs before priming")
    report = {
        "fixture_sha256": _digest(fixture),
        "pair": args.cached_pair,
        "arm": args.cached_arm,
        "state_before": signature["state"],
        "qualified": False,
    }
    try:
        with httpx.Client(
            base_url=url, headers={"authorization": f"Bearer {bench_apps._token(state.token)}"}, timeout=600
        ) as client:
            for _ in range(2):
                primed = bench_apps.run(client, "mask", texts[:500], 500, "data", workload="cached-fixed-prime")
                _require_answers(primed, 500)
                if [_digest(answer) for answer in primed.answers] != fixture["answer_hashes"][:500]:
                    raise RuntimeError("priming outputs differ from the frozen fixture")
            if _fixture_state(space) != fixture["state"]:
                raise RuntimeError("priming changed the fixture")
            sent = texts if args.cached_arm == "500+1" else texts[-1:]
            engine = space.runtime.built.engine
            # Applications use the default session's masker for this no-project fixture.
            with cached_observation(engine, engine.masker, texts) as proof:
                if args.cached_arm == "500+1":
                    runs = [bench_apps.run(client, "mask", sent, len(sent), "data", workload=args.cached_arm)]
                else:
                    # The one text alone, sent again and again in one window (``repeated``), the engine's blocks of
                    # the rules before dropped between two (``rules_changed``: they no longer match), so that each is
                    # read as the first was, never answered from that cache. Its values are in memory from the second
                    # on: each is at most as long as the first, and the 500 cached texts' share of the other arm's
                    # time (its time less this one's) is at most what is said of it, never less.
                    runs, window = repeated(
                        lambda: bench_apps.run(client, "mask", sent, 1, "data", workload=args.cached_arm),
                        engine.rules_changed,
                    )
                    report["window"] = window
            masked = runs[-1]
            report.update({"requests": [request for each in runs for request in each.requests], "proof": proof})
            for each in runs:
                _require_answers(each, len(sent))
            require_cached_proof(proof, args.cached_arm, len(runs))
            expected = fixture["answer_hashes"] if args.cached_arm == "500+1" else fixture["answer_hashes"][-1:]
            if any([_digest(answer) for answer in each.answers] != expected for each in runs):
                raise RuntimeError("measured masking differs from the frozen fixture")
            restored = bench_apps.run(client, "unmask", masked.answers, len(sent), None)
            _require_answers(restored, len(sent))
            if restored.answers != sent:
                raise RuntimeError("measured answers do not restore exactly")
        report["known_spans"] = _known_cleartext_check(engine.vault, sent, masked.answers)
        report["state_after"] = _fixture_state(space)
        report["new_answer_sha256"] = _digest(masked.answers[-1])
        if args.cached_arm == "500+1":
            timed = all(sample["cpu"]["status"] == "ok" for sample in masked.requests)
        else:  # each request is shorter than the clock's tick: the window's time is the evidence
            timed = report["window"]["cpu"]["status"] == "ok" and report["window"]["seconds"] >= CACHED_WINDOW_SECONDS
        report["qualified"] = report["state_after"] == fixture["state"] and timed
        if not report["qualified"]:
            raise RuntimeError("fixture changed or CPU evidence unavailable")
    except BaseException as error:
        report["error"] = type(error).__name__
        raise
    finally:
        atomic_record(args.cached_output, report)


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _cached_cpu(cpu):
    """The processors' raw deltas of a measurement, checked: available, finite, consistent. Their busy seconds."""
    if not isinstance(cpu, dict) or cpu.get("status") != "ok":
        raise ValueError("cached raw CPU measurement is unavailable")
    idle, total, percent = (cpu.get(name) for name in ("idle", "total", "percent"))
    if (
        not all(_finite_number(value) for value in (idle, total, percent))
        or total <= 0
        or not 0 <= idle <= total
        or percent != 100.0 * (total - idle) / total
    ):
        raise ValueError("cached raw CPU deltas or percentage are invalid or inconsistent")
    return total - idle


def _cached_timing(record):
    """An arm's time and processors' busy time a request: the one request of 500+1, checked whole; the one text's
    window of requests sent back to back (``repeated``), each request checked as the arm's successful one, the window
    ``CACHED_WINDOW_SECONDS`` at least, of two requests at least and of as many as it counts, its seconds and busy time
    over their number. And how many requests."""
    requests = record.get("requests")
    alone = record["arm"] != "500+1"
    window = record.get("window") if alone else None
    if alone:
        if not isinstance(window, dict) or type(window.get("repeats")) is not int or window["repeats"] < 2:
            raise ValueError("the one text's arm requires a window of two requests at least")
        count = window["repeats"]
    else:
        count = 1
    if not isinstance(requests, list) or len(requests) != count or not all(isinstance(r, dict) for r in requests):
        raise ValueError("cached arm requires exactly its raw requests")
    expected_texts = 1 if alone else 501
    for request in requests:
        if (
            request.get("operation") != "mask"
            or request.get("workload") != record["arm"]
            or type(request.get("texts")) is not int
            or request["texts"] != expected_texts
            or type(request.get("request_index")) is not int
            or request["request_index"] != 0
            or type(request.get("status")) is not int
            or request["status"] != 200
        ):
            raise ValueError("cached raw request does not match its successful measured arm")
        seconds = request.get("seconds")
        if not _finite_number(seconds) or seconds <= 0:
            raise ValueError("cached raw seconds must be finite and strictly positive")
    if not alone:
        return requests[0]["seconds"], _cached_cpu(requests[0].get("cpu")), 1
    seconds = window.get("seconds")
    if not _finite_number(seconds) or seconds < CACHED_WINDOW_SECONDS:
        raise ValueError("the one text's window must be finite and as long as CACHED_WINDOW_SECONDS at least")
    return seconds / count, _cached_cpu(window.get("cpu")) / count, count


def cached_pairs(args):
    """Read six records in the predetermined AB/BA/AB order; no time ratio is a pass threshold."""
    records = [json.loads(path.read_text("utf-8")) for path in args.results]
    expected = [(0, "500+1"), (0, "1-alone"), (1, "1-alone"), (1, "500+1"), (2, "500+1"), (2, "1-alone")]
    if len(records) != 6 or [(record["pair"], record["arm"]) for record in records] != expected:
        raise ValueError("supply exactly the six fixed AB/BA/AB records")
    fixture = records[0]["fixture_sha256"]
    if any(not record["qualified"] or record["fixture_sha256"] != fixture for record in records):
        raise RuntimeError("not every arm qualified on the same fixture")
    pairs = []
    for number in range(3):
        arms = {record["arm"]: record for record in records if record["pair"] == number}
        a, b = arms["500+1"], arms["1-alone"]
        if a["new_answer_sha256"] != b["new_answer_sha256"] or a["state_before"] != b["state_before"]:
            raise RuntimeError("asymmetric cached pair")
        (mixed, mixed_busy, _), (alone, alone_busy, repeats) = _cached_timing(a), _cached_timing(b)
        pairs.append(
            {
                "pair": number,
                "mixed_seconds": mixed,
                "alone_seconds": alone,
                "delta_seconds": mixed - alone,
                "ratio": mixed / alone,
                "mixed_cpu_busy_seconds": mixed_busy,
                "alone_cpu_busy_seconds": alone_busy,
                "alone_repeats": repeats,
            }
        )
    import statistics

    summary = {
        name: {
            "median": statistics.median(pair[name] for pair in pairs),
            "min": min(pair[name] for pair in pairs),
            "max": max(pair[name] for pair in pairs),
        }
        for name in ("mixed_seconds", "alone_seconds", "delta_seconds", "ratio")
    }
    print(
        json.dumps(
            {"fixture_sha256": fixture, "pairs": pairs, "summary": summary, "numeric_acceptance_threshold": None},
            allow_nan=False,
        ),
        flush=True,
    )


@contextlib.contextmanager
def indexing(on: bool) -> Iterator[None]:
    """With ``on`` False, every store of an API key's space this process opens has no index of prints
    (``PgVaultStore(index=False)``): its words are asked of PostgreSQL as 0.10.0 asked them, on the same source, so
    that a serve sets the index's share of what a call costs apart (Task 8's fix round)."""
    if on:
        yield
        return
    from latet.server import backends

    made = backends.PgVaultStore

    def unindexed(*args: Any, **kwargs: Any) -> Any:
        return made(*args, **{**kwargs, "index": False})

    backends.PgVaultStore = unindexed  # type: ignore[assignment,misc]
    try:
        yield
    finally:
        backends.PgVaultStore = made  # type: ignore[misc]


def serve(args: argparse.Namespace, rest: Sequence[str]) -> None:
    if args.index == "off" and (args.wait_index or args.readiness_only or args.cached_arm):
        raise SystemExit("--index off serves without an index: give --no-wait-index, and no readiness or cached arm")
    state = State.load(args.folder)
    token, identity = state.token, state.key
    try:
        with (
            timing() as timed,
            profiled(args.profile) as profiles,
            Sampled() as sampled,
            indexing(args.index == "on"),
            serving(state, args.working, args.held) as (server, url),
            contextlib.ExitStack() as stack,  # closed first: what it holds uses the server
        ):
            if args.new_key:
                identity, secret = _new_key(server, "bench, new")
                token = state.folder / "token-new.txt"
                _secret_file(token, secret)
            if args.wait_index:
                ready_identity(server, url, token, identity)
            if args.readiness_only:
                if not args.wait_index:
                    raise ValueError("readiness-only requires index readiness")
                return
            if args.cached_arm:
                cached_arm(args, server, url, state)
                return
            if args.analyze:
                stack.enter_context(analyzing(server, args.analyze))
            before = memory_mb()
            started = time.perf_counter()
            command = [sys.executable, str(HERE / "bench_apps.py"), "--url", url, "--token-file", str(token), *rest]
            said = (
                f"key_working_values {args.working}, key_held_secrets {args.held}; vault {state.values} values; "
                f"index {args.index}; bench_apps.py {' '.join(rest)}"
            )
            print(said, flush=True)
            subprocess.run(command, check=True)
            took = time.perf_counter() - started
            print(f"in {took:.0f} s; the server's process holds {memory_mb():.0f} MB ({memory_mb() - before:+.0f} MB)")
            timed.report()
            sampled.report()
            for name, profile in profiles.items():
                print(f"where {name}'s time went, by the time spent in each function and what it called:")
                pstats.Stats(profile, stream=sys.stdout).sort_stats("cumulative").print_stats(30)
    finally:
        if args.new_key:
            with contextlib.suppress(FileNotFoundError):
                (state.folder / "token-new.txt").unlink()


# --- (a) two megabytes of Chinese ---------------------------------------------------------------------


def _han(n: int, seed: int, common: int | None) -> str:
    """``n`` characters of Han: any of the 20,992 of the first block alike (``common`` None: every pair of characters
    a new word, the worst case), or the ``common`` most used of them, the commoner more often (as a Chinese text
    writes them), with a full stop every 20 or so."""
    rng = random.Random(seed)
    if common is None:
        chars = [chr(rng.randint(0x4E00, 0x9FFF)) for _ in range(n)]
    else:
        weights = list(itertools.accumulate(1 / (rank + 5) for rank in range(1, common + 1)))
        alphabet = [chr(0x4E00 + i * 7 % 20992) for i in range(common)]
        chars = rng.choices(alphabet, cum_weights=weights, k=n)
    for at in range(20, n, 23):
        chars[at] = "。"
    return "".join(chars)


def words(args: argparse.Namespace) -> None:
    state = State.load(args.folder)
    server = open_bench_server(state)
    try:
        store = _store(server, state.key)
        sealer = store._sealer
        kinds = (("random Han (every pair new)", None), ("Han, 3,500 characters, common ones often", 3500))
        for label, common in kinds:
            text = _han(args.chars, 7, common)
            started = time.perf_counter()
            found = words_in(text)
            read = time.perf_counter() - started
            started = time.perf_counter()
            hashed = sorted({sealer.keyed(f"original-word{_NUL}{word}") for word in found})
            hashing = time.perf_counter() - started
            statements = math.ceil(len(hashed) / WORDS_AT_ONCE)
            started = time.perf_counter()
            entries = store.with_words("original", found)
            asked = time.perf_counter() - started
            how = "the vault's words read once" if len(hashed) > 50_000 else f"{statements} statements"
            print(
                f"{label}: {len(text):,} characters ({len(text.encode()) / 1e6:.1f} MB), {len(found):,} words read in "
                f"{read:.2f} s, hashed in {hashing:.2f} s; the store asked ({how}) in {asked:.2f} s, "
                f"{len(entries)} entries found ({state.values:,} values in the vault)"
            )
    finally:
        server.close()
    text = _han(args.chars, 7, None)
    with (
        timing() as timed,
        serving(state) as (_, url),
        httpx.Client(base_url=url, timeout=600, headers=_bearer(state.token)) as client,
    ):
        client.post("/_latet/apps/v1/mask", json={"texts": ["warm up"]})
        for operation in ("mask", "unmask"):
            started = time.perf_counter()
            answer = client.post(f"/_latet/apps/v1/{operation}", json={"texts": [text]})
            took = time.perf_counter() - started
            print(f"{operation} of the random Han through the API: {answer.status_code} in {took:.1f} s", flush=True)
            if answer.status_code == 200:
                text = answer.json()["results"][0].get("text", text)
        timed.report()


# --- (b) the values a log line opens --------------------------------------------------------------------


def realistic(k: int, seed: int) -> str:
    """The ``k``-th customer's address as people's addresses are written, those a vault cannot give a fake to at
    volume too: at a mailbox, ``first.last`` (a third of them), ``firstlast``, ``f.last``, with digits or a year; at
    their company otherwise (``bench_apps.customer``'s)."""
    first, first_w, last, last_w, _, _ = bench_apps._population()
    rng = random.Random(f"realistic:{seed}:{k}")
    if rng.random() >= 2 / 3:
        return bench_apps.customer(k, seed)
    given = rng.choices(first, cum_weights=first_w)[0].lower()
    family = rng.choices(last, cum_weights=last_w)[0].lower()
    mailbox = rng.choices(list(bench_apps._MAILBOXES), weights=list(bench_apps._MAILBOXES.values()))[0]
    year = f"{rng.randint(50, 109) % 100:02d}"
    local = rng.choices(
        (f"{given}.{family}", f"{given}{family}", f"{given[0]}.{family}", f"{given}.{family}{year}",
         f"{given[0]}{family}{year}", f"{given}{rng.randint(1950, 2009)}", f"{family}.{given}"),
        weights=(33, 12, 8, 17, 10, 12, 8),
    )[0]  # fmt: skip
    return f"{local}@{mailbox}"


def _public_ipv4(rng: random.Random) -> str:
    while True:
        address = str(bench_apps.ipaddress.IPv4Address(rng.getrandbits(32)))
        found = bench_apps.ipaddress.ip_address(address)
        if found.is_global and not (found.is_multicast or found.is_reserved):
            return address


def _groups(values: Sequence[str]) -> Counter[str]:
    """How many of ``values`` each anchor word is the anchor of: the entries a text that holds the word opens."""
    return Counter(word for word in map(anchor, values) if word is not None)


def _opened(groups: Counter[str], texts: Sequence[str]) -> int:
    """The entries the store opens for ``texts`` in one call: each entry whose anchor is a word of one of them."""
    found: set[str] = set()
    for text in texts:
        found |= words_in(text)
    return sum(groups.get(word, 0) for word in found)


def _spread(values: Sequence[int]) -> str:
    ordered = sorted(values)
    return (
        f"median {statistics.median(ordered):,.0f}, 95 in 100 under {ordered[int(len(ordered) * 0.95)]:,}, "
        f"most {ordered[-1]:,}"
    )


def _log_of(emails: Sequence[str], n: int, seed: int, addresses: Sequence[str]) -> list[str]:
    """``n`` log lines (``bench_apps._line``'s), each naming one of ``emails`` and one of ``addresses``."""
    rng = random.Random(f"log:{seed}")
    return [bench_apps._line(rng, index, rng.choice(emails), rng.choice(addresses)) for index in range(n)]


def _vault_of(size: int, ipv4: float) -> tuple[list[str], list[str], list[str]]:
    """``size`` values as a key's vault of customers holds them (``realistic`` addresses, the domain of each at a
    company, and the ``ipv4`` share public IPv4 addresses, of any network); the addresses among them, and the IPv4
    addresses the lines name: the vault's own, or (none in it) ``bench_apps.addresses``."""
    rng = random.Random(11)
    ips = [_public_ipv4(rng) for _ in range(int(size * ipv4))]
    values: dict[str, None] = dict.fromkeys(ips)
    emails: list[str] = []
    k = 0
    while len(values) < size:
        email = realistic(k, 1)
        k += 1
        if email in values:
            continue
        emails.append(email)
        values[email] = None
        domain = email.rpartition("@")[2]
        if domain not in bench_apps._MAILBOXES:
            values[domain] = None
    return list(values)[:size], emails, ips or list(bench_apps.addresses())


def fanout(args: argparse.Namespace) -> None:
    print("The entries a log line opens, as the store finds them by their values' anchor words (Ruling T8a(d)):")
    for size in args.sizes:
        for ipv4 in (0.0, 1 / 3):
            values, emails, ips = _vault_of(size, ipv4)
            groups = _groups(values)
            lines = _log_of(emails, 2000, 3, ips)
            one = [_opened(groups, [line]) for line in lines]
            calls = [_opened(groups, lines[i : i + 500]) for i in range(0, 2000, 500)]
            top = ", ".join(f"{word} {count:,}" for word, count in groups.most_common(8))
            over = [word for word, count in groups.items() if count > CANDIDATES_AT_MOST]
            print(f"{len(values):,} values, {ipv4:.0%} of them IPv4 addresses:")
            print(f"  a line opens: {_spread(one)}")
            print(f"  a call of 500 lines opens: {_spread(calls)} (refused past {CANDIDATES_AT_MOST:,})")
            print(f"  the largest groups: {top}; words past the cap: {len(over)}")
    _fanout_in_store(args)


def _fanout_in_store(args: argparse.Namespace) -> None:
    """The same count for the built vault, as the store answers it and as the anchors say, which must agree; and the
    fakes' side: the entries an unmask of the masked lines opens."""
    state = State.load(args.folder)
    server = open_bench_server(state)
    try:
        store = _store(server, state.key)
        started = time.perf_counter()
        entities = store.load_entities()
        print(f"the built vault: {len(entities):,} entries opened in {time.perf_counter() - started:.1f} s")
        values = _groups([e.original for e in entities])
        fakes = _groups([e.surrogate for e in entities])
        by_value = {e.original: e.surrogate for e in entities}
        lines = bench_apps.lines(2000, state.seed, 0.5)
        for start in (0, 500, 1000):
            batch = lines[start : start + 500]
            words: set[str] = set()
            for line in batch:
                words |= words_in(line)
            asked = len(store.with_words("original", words))
            print(f"  500 lines from {start}: the store opens {asked:,}, the anchors say {_opened(values, batch):,}")
        masked = [_masked(line, by_value) for line in lines]
        one = [_opened(fakes, [line]) for line in masked]
        calls = [_opened(fakes, masked[i : i + 500]) for i in range(0, 2000, 500)]
        print(f"  unmask, a masked line opens: {_spread(one)}; a call of 500: {_spread(calls)}")
        top = ", ".join(f"{count:,}" for _, count in fakes.most_common(8))
        print(f"  the largest groups of fakes: {top} (their words are the fakes' names)")
    finally:
        server.close()


def _masked(line: str, fakes: dict[str, str]) -> str:
    """``line`` with each value the vault knows written as its fake, as a mask writes it."""
    for part in line.replace("(", " ").replace(")", " ").replace(";", " ").split():
        value = part.split("=", 1)[-1].rstrip(":")
        if value in fakes:
            line = line.replace(value, fakes[value])
    return line


# --- (c) the weighing --------------------------------------------------------------------------------


def weigh(args: argparse.Namespace) -> None:
    state = State.load(args.folder)
    server = open_bench_server(state)
    try:
        store = _store(server, state.key)
        entities = store.load_entities()
    finally:
        server.close()
    rng = random.Random(5)
    emails = [e for e in entities if e.etype == "EMAIL"]
    for candidates in (5_000, 20_000, 100_000):
        chosen = rng.sample(entities, min(candidates, len(entities)))
        named = [e.original for e in chosen if e.etype == "EMAIL"] or [e.original for e in emails[:100]]
        for n in (500, 1000):
            lines = _log_of(named, n, 9, list(bench_apps.addresses()))
            started = time.perf_counter()
            weighing = stored_module._Weighing(chosen)
            made = time.perf_counter() - started
            started = time.perf_counter()
            held = weighing.held_in(lines)
            read = time.perf_counter() - started
            print(
                f"{len(chosen):>7,} candidates, {n:>5} lines: made in {made:.2f} s, read in {read:.2f} s "
                f"({len(held):,} held)"
            )


# --- (d) unmasks of several keys at once ----------------------------------------------------------------


def load(args: argparse.Namespace) -> None:
    """(In a process of its own.) ``--clients`` clients, each unmasking ``--batch`` of the texts at a time, one call
    after the other, for ``--seconds``: printed as JSON, how many calls and their times."""
    texts = json.loads(Path(args.texts).read_text("utf-8"))
    token = Path(args.token_file).read_text("utf-8").strip()
    deadline = time.monotonic() + args.seconds
    times: list[float] = []
    statuses: Counter[int] = Counter()
    lock = threading.Lock()

    def client(index: int) -> None:
        rng = random.Random(index)
        with httpx.Client(base_url=args.url, headers={"authorization": f"Bearer {token}"}, timeout=120) as http:
            while time.monotonic() < deadline:
                start = rng.randrange(max(1, len(texts) - args.batch))
                started = time.perf_counter()
                answer = http.post("/_latet/apps/v1/unmask", json={"texts": texts[start : start + args.batch]})
                took = time.perf_counter() - started
                with lock:
                    statuses[answer.status_code] += 1
                    if answer.status_code == 200:
                        times.append(took)

    threads = [threading.Thread(target=client, args=(i,)) for i in range(args.clients)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print(json.dumps({"times": times, "statuses": statuses}))


def _probe(url: str, codes: list[str], tokens: list[str], seconds: float) -> dict[str, list[float]]:
    """For ``seconds``: an enrolment (a code spent) and a credential looked up (a token's first call, ``whoami``) one
    after the other, a quarter of a second apart: their times."""
    found: dict[str, list[float]] = {"enrolment": [], "lookup": []}
    deadline = time.monotonic() + seconds
    with httpx.Client(base_url=url, timeout=60) as client:
        while time.monotonic() < deadline and codes and tokens:
            started = time.perf_counter()
            answer = client.post(f"{SERVER_API}/enrol", json={"code": codes.pop(), "label": "bench"})
            if answer.status_code == 200:
                found["enrolment"].append(time.perf_counter() - started)
            started = time.perf_counter()
            answer = client.get(f"{SERVER_API}/whoami", headers={"x-latet-token": tokens.pop()})
            if answer.status_code == 200:
                found["lookup"].append(time.perf_counter() - started)
            time.sleep(0.25)
    return found


def _ms(values: Sequence[float]) -> str:
    if not values:
        return "none"
    return f"p50 {statistics.median(values) * 1000:.0f} ms, max {max(values) * 1000:.0f} ms ({len(values)})"


def _entries(server: Server) -> int:
    """How many unmasks the administration log holds."""
    with server.db.connection() as conn:
        row = conn.execute("SELECT count(*) FROM audit WHERE action = 'apps.unmasked'").fetchone()
    return int(row[0]) if row is not None else 0


def unmasks(args: argparse.Namespace) -> None:
    state = State.load(args.folder)
    try:
        with (
            timing() as timed,
            serving(state) as (server, url),
            httpx.Client(base_url=url, timeout=600) as client,
        ):
            keys = [_new_key(server, f"bench, unmask {i}") for i in range(max(args.keys))]
            codes = [server.identities.add_person(f"Bench person {i}")[1] for i in range(400)]
            tokens = [_new_key(server, f"bench, lookup {i}")[1] for i in range(400)]
            texts_of: list[Path] = []
            for index, (_, secret) in enumerate(keys):  # each key's vault: the customers of 2,000 lines
                lines = bench_apps.lines(2000, 100 + index, 0.5)
                masked: list[str] = []
                for start in range(0, len(lines), 500):
                    answer = client.post(
                        "/_latet/apps/v1/mask",
                        json={"texts": lines[start : start + 500], "kind": "data"},
                        headers={"authorization": f"Bearer {secret}"},
                    )
                    masked += [result["text"] for result in answer.json()["results"]]
                path = state.folder / f"texts-{index}.json"
                path.write_text(json.dumps(masked), "utf-8")
                texts_of.append(path)
                _secret_file(state.folder / f"token-{index}.txt", secret)
            # The new keys' rows known to PostgreSQL's statistics, which learn them late otherwise (the report's
            # finding): what is measured is the log's threads, not that.
            with server.db.connection() as conn:
                conn.execute("ANALYZE vault_entities, vault_words")
            idle = _probe(url, codes, tokens, 8)
            print(f"idle: enrolments {_ms(idle['enrolment'])}; lookups {_ms(idle['lookup'])}")
            for count in args.keys:
                before = _entries(server)
                command = [sys.executable, str(HERE / "bench_apps_server.py"), "load", "--url", url]
                command += ["--clients", str(args.clients), "--batch", str(args.batch), "--seconds", str(args.seconds)]
                loads = [
                    subprocess.Popen(
                        [*command, "--token-file", str(state.folder / f"token-{i}.txt"), "--texts", str(texts_of[i])],
                        stdout=subprocess.PIPE,
                        text=True,
                    )
                    for i in range(count)
                ]
                time.sleep(2)  # the load under way
                cpu, wall = time.process_time(), time.perf_counter()
                during = _probe(url, codes, tokens, args.seconds - 4)
                cpu, wall = time.process_time() - cpu, time.perf_counter() - wall
                answers = [json.loads(process.communicate()[0]) for process in loads]
                times = [took for answer in answers for took in answer["times"]]
                statuses: Counter[str] = Counter()
                for answer in answers:
                    statuses.update(answer["statuses"])
                logged = _entries(server) - before
                rate = len(times) / args.seconds
                print(
                    f"{count} keys x {args.clients} clients, {args.batch} lines a call: {rate:.0f} unmasks a second "
                    f"({rate * args.batch:.0f} lines), {_ms(times)}; "
                    f"answers {dict(statuses)}; entries written {logged}"
                )
                print(f"  meanwhile: enrolments {_ms(during['enrolment'])}; lookups {_ms(during['lookup'])}")
                print(f"  the server's process used {cpu / wall:.2f} processors meanwhile")
            timed.report()
    finally:
        for path in state.folder.glob("token-*.txt"):
            path.unlink()
        for path in state.folder.glob("texts-*.json"):
            path.unlink()


# --- (e) what a key's space build asks first -----------------------------------------------------------


def missing(args: argparse.Namespace) -> None:
    state = State.load(args.folder)
    server = open_bench_server(state)
    try:
        store = _store(server, state.key)
        with server.db.connection() as conn:
            for name, query, params in (
                ("_MISSING", _MISSING, (state.key, state.key, ANCHORS_VERSION)),
                ("_NULL_WORDS, the first part", _NULL_WORDS, (state.key, 0, state.key, 1000)),
            ):
                times = []
                for _ in range(5):
                    started = time.perf_counter()
                    conn.execute(query, params).fetchall()
                    times.append(time.perf_counter() - started)
                plan = conn.execute(sql.SQL("EXPLAIN (ANALYZE, BUFFERS) ") + sql.SQL(query), params).fetchall()
                print(f"{name}: {_ms(times)}")
                for row in plan:
                    print(f"    {row[0]}")
        started = time.perf_counter()
        filled = store.backfill_words()
        took = (time.perf_counter() - started) * 1000
        print(f"backfill_words with nothing missing: {filled} filled in {took:.0f} ms")
        for working in (1_000, 20_000, 100_000):
            started = time.perf_counter()
            vault = stored_module.StoredVault(bytes(32), store, working=working)
            made = time.perf_counter() - started
            started = time.perf_counter()
            vault.matchers.current()
            print(
                f"a key's vault of {state.values:,} values made at {working:,} working values: read in {made:.1f} s, "
                f"its detectors made in {time.perf_counter() - started:.1f} s"
            )
    finally:
        server.close()


# --- workers (Task 10) -------------------------------------------------------------------------------


# The server as `latet server run --workers N` runs it (``python -c``, arguments: the state's folder, N, the port): one
# instance in its own process (the supervisor's own path for one) or the supervisor and its N workers, in the state's
# schema, prefix, master key and empty models folder, given explicitly (never the defaults). Started by ``-c``, it
# imports nothing of this script, and neither do its workers (spawn runs a parent's main script again in each child,
# and this one's imports would weigh in every worker's memory): their memory, and the supervisor's, is what `latet
# server run` holds. Its own memory printed as JSON when a stop signal ends it.
_SERVE = """
import functools, json, os, sys
from pathlib import Path
from latet.server.instance import make_instance, process_memory
from latet.server.logs import configure_logging
from latet.server.settings import ServerSettings
from latet.server.workers import Supervisor, worker_main
folder, count, port = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
state = json.loads((folder / "state.json").read_text("utf-8"))
(folder / "models").mkdir(exist_ok=True)
settings = ServerSettings(
    url=f"http://127.0.0.1:{port}", database_url=os.environ["LATET_TEST_DATABASE_URL"],
    valkey_url=os.environ["LATET_TEST_VALKEY_URL"], master_key_file=folder / "master.key", host="127.0.0.1",
    port=port, workers=count,
)
where = {"schema": state["schema"], "prefix": state["prefix"], "data": folder / "models"}
configure_logging("text", "warning")
if count == 1:
    make_instance(settings, **where).run()
else:
    Supervisor(settings, count, target=functools.partial(worker_main, **where)).run()
print(json.dumps({"supervisor_memory": process_memory()}), flush=True)
"""


def _stop_serving(process: subprocess.Popen[str]) -> dict[str, Any]:
    """The serving process stopped as a stop signal stops `latet server run` (Ctrl-Break in its own process group on
    Windows, SIGTERM elsewhere), waited for, then killed; what it printed of its memory."""
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.terminate()
    try:
        out, _ = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
        out, _ = process.communicate(timeout=30)
    for line in (out or "").splitlines():
        if line.startswith('{"supervisor_memory"'):
            return json.loads(line)
    return {"supervisor_memory": None}


def _instances(server: Server, count: int, seconds: float = 180) -> list[dict[str, Any]]:
    """The server's ``count`` instances once each is ready, as the status lists them."""
    deadline = time.monotonic() + seconds
    while True:
        ready = [row for row in server.instances() if row["ready"] is True]
        if len(ready) >= count:
            return ready
        if time.monotonic() > deadline:
            raise SystemExit(f"{len(ready)} of {count} workers ready within {seconds:.0f} s")
        time.sleep(0.5)


def _warmed(server: Server, url: str, token: str, count: int, alive: set[str], seconds: float = 300) -> None:
    """Each of the ``count`` workers (``alive``: their ids) given calls until it holds the key's space: ``2 x count``
    calls at once each time, every one on a connection of its own, as the waves' ``2 x count`` clients send theirs, so
    that the port deals them to every worker (calls one at a time went to the same few workers on one shared socket,
    and eight were never all warmed)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(_: int) -> None:
        with httpx.Client(base_url=url, headers={"authorization": f"Bearer {token}"}, timeout=300) as http:
            http.post("/_latet/apps/v1/mask", json={"texts": ["warm up"], "kind": "data"}).raise_for_status()

    deadline = time.monotonic() + seconds
    with ThreadPoolExecutor(2 * count) as pool:
        while True:
            rows = [row for row in server.instances() if row["instance"] in alive]
            if len(rows) >= count and all((row["spaces"] or 0) >= 1 for row in rows):
                return
            if time.monotonic() > deadline:
                raise SystemExit("the workers did not all build the key's space")
            list(pool.map(one, range(2 * count)))
            time.sleep(0.2)


def calls_of(texts: Sequence[str], batch: int) -> list[list[str]]:
    """``texts`` in calls of ``batch``, in order."""
    return [list(texts[i : i + batch]) for i in range(0, len(texts), batch)]


def workers_clients(count: int, per_worker: int, lines: int, values: int) -> int:
    """How many clients a phase of ``count`` workers has: ``per_worker`` connections a worker, four at least. The kernel
    gives each new connection to a worker by a hash of its addresses and ports, so with two a worker (16 for eight) one
    worker has three or more in most runs and another none, and a measurement that waits for every client follows the
    busiest worker; with four a worker the spread is closer, and a fixed time (``measured_window``) leaves an idle
    worker costing only its share. Raises SystemExit for fewer than four, and for a pool of more ``lines`` than the
    vault's ``values``: their customers would be new values, learned during the measurement."""
    if per_worker < 4:
        raise SystemExit(f"row 6 is measured with four connections a worker at least, not {per_worker} (--per-worker)")
    if lines > values:
        raise SystemExit(
            f"{lines} lines need more customers than the vault's {values} values know: give fewer --lines, or build "
            "with more --values"
        )
    return count * per_worker


def client_loop(
    post: Callable[[list[str]], list[Any]],
    calls: Sequence[list[str]],
    claim: Callable[[], int],
    deadline: float,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """One client's phase: the next call of the shared count (``claim``) sent (``post``), one after the other, until
    ``deadline`` (``clock``'s) or until no call is left. Each call answered and when (``done``), its answers by the
    call's index, and when the calls ran out (``ran_out_at``, None when the time was up first)."""
    done: list[list[float]] = []
    answers: dict[int, list[Any]] = {}
    ran_out_at: float | None = None
    while clock() < deadline:
        index = claim()
        if index >= len(calls):
            ran_out_at = clock()
            break
        answers[index] = post(calls[index])
        done.append([index, clock()])
    return {"done": done, "answers": answers, "ran_out_at": ran_out_at}


def measured_window(
    results: Sequence[dict[str, Any]], calls: Sequence[list[str]], go_at: float, seconds: float
) -> tuple[float, int, bool]:
    """A phase's measured time and lines: from the go every client waited for (``go_at``) to ``seconds`` later, or to
    the moment the calls ran out when that came first (every client was sending until then: whether it did is the
    third value), and the lines of the calls answered within it. Lines over that time is the throughput, so that no
    phase ends with its slowest client."""
    end = go_at + seconds
    ran_out = [result["ran_out_at"] for result in results if result["ran_out_at"] is not None]
    short = bool(ran_out) and min(ran_out) < end
    if short:
        end = min(ran_out)
    lines = sum(len(calls[int(index)]) for result in results for index, at in result["done"] if at <= end)
    return end - go_at, lines, short


def established_on(table: str, port: int) -> set[int]:
    """The inodes of the sockets ``table`` (``/proc/net/tcp`` or ``tcp6``) lists established on local ``port``."""
    found: set[int] = set()
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 9 and fields[3] == "01" and int(fields[1].rsplit(":", 1)[1], 16) == port:
            found.add(int(fields[9]))
    return found


def processor_seconds(stat: str, ticks: int) -> float:
    """The processor time a process spent, user and system, from its ``/proc/<pid>/stat``, ``ticks`` a second."""
    fields = stat.rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / ticks


_LINUX = sys.platform.startswith("linux")


def _worker_pids(serving: int, count: int) -> list[int]:
    """The processes of the workers serving (``serving``: the process `latet server run` runs in): itself for one, its
    children ``spawn`` started for several (Linux; none known elsewhere)."""
    if count == 1:
        return [serving]
    found = []
    if _LINUX:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            with contextlib.suppress(OSError, ValueError, IndexError):
                parent = int((entry / "stat").read_text("ascii").rsplit(")", 1)[1].split()[1])
                if parent == serving and b"spawn_main" in (entry / "cmdline").read_bytes():
                    found.append(int(entry.name))
    return sorted(found)


def _processor_time(pid: int) -> float | None:
    if not _LINUX:
        return None
    with contextlib.suppress(OSError, ValueError, IndexError):
        return processor_seconds(Path(f"/proc/{pid}/stat").read_text("ascii"), os.sysconf("SC_CLK_TCK"))
    return None


def _connections(pid: int, port: int) -> int | None:
    """How many connections established on ``port`` process ``pid`` holds (Linux; None elsewhere)."""
    if not _LINUX:
        return None
    mine: set[int] = set()
    with contextlib.suppress(OSError):
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            with contextlib.suppress(OSError):
                target = os.readlink(fd)
                if target.startswith("socket:["):
                    mine.add(int(target[8:-1]))
    established: set[int] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        with contextlib.suppress(OSError):
            established |= established_on(Path(table).read_text("ascii"), port)
    return len(mine & established)


def phase_http(url: str, token: str) -> httpx.Client:
    """A client of a phase: one connection, kept however long it waits. httpx drops a connection idle for five
    seconds by default and opens another at the next call, which the kernel may give to another worker than the one
    counted; a client waits at the go for the slowest of 32 to start. The server keeps an idle connection 75 s."""
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=None)
    return httpx.Client(base_url=url, headers={"authorization": f"Bearer {token}"}, timeout=600, limits=limits)


def local_port(answer: httpx.Response) -> int | None:
    """The local port of the connection ``answer`` came on, while it is open; None when httpx does not say."""
    with contextlib.suppress(Exception):
        return int(answer.extensions["network_stream"].get_extra_info("socket").getsockname()[1])
    return None


def _poster(http: httpx.Client, op: str, ports: list[int | None] | None = None) -> Callable[[list[str]], list[Any]]:
    """Each call of ``op`` on ``http``; ``ports``, when given, ends with the last call's connection's local port."""

    def post(texts: list[str]) -> list[Any]:
        body: dict[str, Any] = {"texts": texts}
        if op == "mask":
            body["kind"] = "data"
        answer = http.post(f"/_latet/apps/v1/{op}", json=body)
        if answer.status_code != 200:
            raise SystemExit(f"a {op} call was refused ({answer.status_code}): no number from this phase")
        if ports is not None:
            ports[1:] = [local_port(answer)]
        return [result["text"] for result in answer.json()["results"]]

    return post


def connections_read(
    counts: Sequence[int | None], clients: int, read_at: float, results: Sequence[dict[str, Any]]
) -> str:
    """The workers' connections as a phase's line says them, read while it ran (``read_at``): beside their sum and the
    clients'. Raises SystemExit, so that the round gives no number, when a client connected again during the phase
    (its local port changed: the counts describe other connections) or when, read while every client was sending,
    they do not add up to the clients. Read after the lines ran out (clients closing), they are said untrusted;
    unknown (not Linux), as such."""
    shown = "[" + ", ".join("?" if count is None else str(count) for count in counts) + "]"
    moved = sum(1 for result in results if len({port for port in result.get("ports", []) if port is not None}) > 1)
    if moved:
        raise SystemExit(
            f"{moved} clients connected again during the phase: the workers' counts describe other connections; no "
            "number from this round"
        )
    if not counts or any(count is None for count in counts):
        return f"{shown} (unknown here)"
    ran_out = [result["ran_out_at"] for result in results if result["ran_out_at"] is not None]
    if ran_out and min(ran_out) <= read_at:
        return f"{shown} (not read while every client sent: the lines ran out first)"
    held = sum(count for count in counts if count is not None)
    if held != clients:
        raise SystemExit(
            f"the workers held {held} of {clients} clients' connections while the phase ran: no number from this round"
        )
    return f"{shown} ({held} of {clients} clients)"


def lines_changed(calls: Sequence[list[str]], answers: dict[int, list[Any]]) -> int:
    """How many lines of ``calls`` came back otherwise than they were sent, a call not answered counting whole."""
    changed = 0
    for index, call in enumerate(calls):
        answer = answers.get(index)
        if answer is None or len(answer) != len(call):
            changed += len(call)
        else:
            changed += sum(1 for sent, back in zip(call, answer, strict=True) if sent != back)
    return changed


def _phase_client(
    url: str, token: str, op: str, pool: str, counter: Any, together: Any, seconds: float, out: str
) -> None:
    """(In a process of its own, ``spawn``.) One client of a phase: its one connection (``phase_http``) made first (a
    readiness call), then, once every client and the measurement are ready (``together``), ``client_loop`` on the
    phase's calls (``pool``), the next of the shared ``counter`` each time, for ``seconds``; what it did, and its
    connection's local port before the go and at its last call, written to ``out`` as JSON."""
    calls = json.loads(Path(pool).read_text("utf-8"))

    def claim() -> int:
        with counter.get_lock():
            index = counter.value
            counter.value += 1
        return index

    with phase_http(url, token) as http:
        ready = http.get("/_latet/ready")  # the connection, made before the clock runs: the kernel gave it its worker
        ports = [local_port(ready)]
        together.wait()
        found = client_loop(_poster(http, op, ports), calls, claim, time.time() + seconds)
    found["ports"] = ports  # its connection's local port before the go, and at its last call
    Path(out).write_text(json.dumps(found), "utf-8")


@dataclass
class Phase:
    """A phase measured: its throughput (lines a second), its time and lines, whether the calls ran out before the time
    was up, the workers' connections as its line says them (``connections_read``), each worker's processor seconds
    (by process; None where unknown), and the answers by call index."""

    rate: float
    seconds: float
    lines: int
    short: bool
    connections: str
    processor: list[float | None]
    answers: dict[int, list[Any]]


def _phase(
    work: Path,
    url: str,
    token: str,
    op: str,
    calls: list[list[str]],
    clients: int,
    seconds: float,
    tag: str,
    pids: list[int],
    port: int,
) -> Phase:
    """``op`` by ``clients`` client processes for ``seconds``, the calls of one pool taken from a shared count (none
    sent twice, no client idle while calls are left), every client connected before the go: the lines answered within
    the time (``measured_window``), each worker's connections (read halfway through the time, every client sending,
    and checked: ``connections_read``) and its processor seconds over the phase. A client that fails stops it."""
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    pool = work / f"{tag}-pool.json"
    pool.write_text(json.dumps(calls), "utf-8")
    counter = context.Value("q", 0)
    together = context.Barrier(clients + 1, timeout=600)
    outs = [work / f"{tag}-{c}.json" for c in range(clients)]
    procs = []
    for out in outs:
        out.unlink(missing_ok=True)
        args = (url, token, op, str(pool), counter, together, seconds, str(out))
        procs.append(context.Process(target=_phase_client, args=args, daemon=True))
    for process in procs:
        process.start()
    together.wait()
    go_at = time.time()
    before = [_processor_time(pid) for pid in pids]
    time.sleep(seconds / 2)
    read_at = time.time()
    counts = [_connections(pid, port) for pid in pids]  # while every client sends, unless the lines ran out
    for process in procs:
        process.join(seconds + 900)
    after = [_processor_time(pid) for pid in pids]
    if any(process.exitcode != 0 for process in procs):
        raise SystemExit(f"a client of the {op} phase failed: no number from it")
    results = [json.loads(out.read_text("utf-8")) for out in outs]
    took, lines, short = measured_window(results, calls, go_at, seconds)
    answers = {int(index): texts for result in results for index, texts in result["answers"].items()}
    processor = [None if a is None or b is None else b - a for a, b in zip(before, after, strict=True)]
    connections = connections_read(counts, clients, read_at, results)
    return Phase(lines / took if took > 0 else 0.0, took, lines, short, connections, processor, answers)


def _answered_rest(url: str, token: str, op: str, calls: list[list[str]], answers: dict[int, list[Any]]) -> None:
    """The calls of the pool no client reached in the phase, answered now (not timed), so that the next phase has
    every line."""
    from concurrent.futures import ThreadPoolExecutor

    missing = [index for index in range(len(calls)) if index not in answers]

    def one(index: int) -> None:
        with httpx.Client(base_url=url, headers={"authorization": f"Bearer {token}"}, timeout=600) as http:
            answers[index] = _poster(http, op)(calls[index])

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(one, missing))


def _spread_of(values: Sequence[float], fmt: str = "{:.0f}") -> str:
    """median [min-max] (n)"""
    if not values:
        return "-"
    ordered = sorted(values)
    shown = (fmt.format(statistics.median(ordered)), fmt.format(ordered[0]), fmt.format(ordered[-1]))
    return f"{shown[0]} [{shown[1]}-{shown[2]}] (n={len(ordered)})"


def _per_worker(values: Sequence[float | None], fmt: str = "{:.0f}") -> str:
    return "[" + ", ".join("?" if value is None else fmt.format(value) for value in values) + "]"


def workers(args: argparse.Namespace) -> None:
    """Task 10's measurement (the plan's target row 6): `latet server run --workers N` for each of ``--counts``, on
    one port of 127.0.0.1, against the state's API key (build's vault: every line of seed ``--seed`` names a customer it
    keeps already, so no more lines than it has values: ``--lines``, the vault's values by default). Each count is
    offered the same pool of distinct lines, in calls of ``--batch``; ``--per-worker`` client processes a worker (four
    at least: 32 for eight), each on one connection made before the go and kept however long it waits
    (``phase_http``), take the calls one after the other from a shared count, so that no line is sent twice in a phase
    and no client waits while lines are left. Throughput is counted over a fixed time (``--seconds``) from the go: the
    lines answered within it, divided by it, never a wave that ends with its slowest client; the calls running out
    first end the time there, said in the line. The kernel deals connections to the workers by a hash of their
    addresses, not in turn, so a worker may hold more connections than another, or none: each phase records every
    worker's connections, read halfway through its time while every client sends, beside their sum and the clients'
    (a sum that differs, or a client that connected again, gives no number), and its processor seconds (Linux,
    ``/proc``), so that a miss can be laid at the spread or at the workers. Mask first, then the lines no client
    reached masked untimed; unmask on every masked line, the lines no client reached unmasked untimed too, and every
    line checked to come back as it was. Rounds in turn, the counts' order reversed every other round, fresh workers
    each time, every worker ready and holding the key's space before the clock runs. Each phase: lines a second, its
    time, the machine's load beside it; each worker's memory as its beat says it, their sum, and the supervisor's
    own. The target: eight workers five times one, for mask and for unmask."""
    from bench_engine import Load

    state = State.load(args.folder)
    token = state.token.read_text("utf-8").strip()
    lines = bench_apps.lines(state.values if args.lines is None else args.lines, args.seed, 0.5)
    for count in args.counts:
        workers_clients(count, args.per_worker, len(lines), state.values)
    calls = calls_of(lines, args.batch)
    print(
        f"{len(lines)} distinct lines of seed {args.seed} offered to every count, {args.batch} a call; "
        f"{args.per_worker} connections a worker; {args.seconds:.0f} s a phase; workers {args.counts}; rounds "
        f"{args.rounds}; vault of {state.values} values; cores: {os.cpu_count()} logical",
        flush=True,
    )
    keys = ("mask", "unmask", "load_mask", "load_unmask", "worker_mb", "total_mb", "supervisor_mb")
    results: dict[int, dict[str, list[float]]] = {n: {key: [] for key in keys} for n in args.counts}
    spread: dict[int, list[str]] = {n: [] for n in args.counts}
    work = args.folder / "workers-work"
    work.mkdir(exist_ok=True)
    server = open_bench_server(state)  # this process's own instance: reads the instances' beats, serves nothing
    try:
        for round_ in range(args.rounds):
            for n in args.counts if round_ % 2 == 0 else args.counts[::-1]:
                clients = workers_clients(n, args.per_worker, len(lines), state.values)
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                sock.close()
                url = f"http://127.0.0.1:{port}"
                command = [sys.executable, "-c", _SERVE, str(args.folder), str(n), str(port)]
                flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
                serving = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, creationflags=flags)
                try:
                    ready = _instances(server, n)
                    alive = {row["instance"] for row in ready}
                    _warmed(server, url, token, n, alive)
                    pids = _worker_pids(serving.pid, n)
                    tag = f"r{round_}-n{n}"
                    load = Load()
                    masked = _phase(work, url, token, "mask", calls, clients, args.seconds, f"{tag}-mask", pids, port)
                    load_m = load.percent()
                    _answered_rest(url, token, "mask", calls, masked.answers)
                    hidden = [masked.answers[index] for index in range(len(calls))]
                    load = Load()
                    back = _phase(work, url, token, "unmask", hidden, clients, args.seconds, f"{tag}-un", pids, port)
                    load_u = load.percent()
                    _answered_rest(url, token, "unmask", hidden, back.answers)
                    changed = lines_changed(calls, back.answers)
                    if changed:
                        raise SystemExit(f"{changed} lines came back changed: no number from this round")
                    rows = [row for row in server.instances() if row["instance"] in alive]
                    memory = [row["memory"] / 1e6 for row in rows if row["memory"] is not None]
                finally:
                    own = _stop_serving(serving)
                r = results[n]
                r["mask"].append(masked.rate)
                r["unmask"].append(back.rate)
                r["load_mask"].append(load_m)
                r["load_unmask"].append(load_u)
                r["worker_mb"].append(statistics.mean(memory) if memory else math.nan)
                r["total_mb"].append(sum(memory))
                if own["supervisor_memory"] is not None and n > 1:
                    r["supervisor_mb"].append(own["supervisor_memory"] / 1e6)
                spread[n].append(masked.connections)
                shorter = {phase.short for phase in (masked, back)}
                print(
                    f"round {round_ + 1}, {n} workers, {clients} connections: mask {masked.rate:.0f} lines/s over "
                    f"{masked.seconds:.1f} s (load {load_m:.0f} %), unmask {back.rate:.0f} over {back.seconds:.1f} s "
                    f"(load {load_u:.0f} %); every line back as it was; connections a worker, mask "
                    f"{masked.connections}, unmask {back.connections}; processor "
                    f"seconds a worker mask {_per_worker(masked.processor, '{:.1f}')} unmask "
                    f"{_per_worker(back.processor, '{:.1f}')}; memory {r['worker_mb'][-1]:.0f} MB a worker, "
                    f"{r['total_mb'][-1]:.0f} MB in all"
                    + ("; the lines ran out before the time was up: give more --lines" if True in shorter else ""),
                    flush=True,
                )
    finally:
        server.close()
    print()
    base = results[min(args.counts)]
    for n in args.counts:
        r = results[n]
        ratio_m = [b / a for a, b in zip(base["mask"], r["mask"], strict=True) if a]
        ratio_u = [b / a for a, b in zip(base["unmask"], r["unmask"], strict=True) if a]
        print(
            f"{n} workers: mask {_spread_of(r['mask'])} lines/s (x{_spread_of(ratio_m, '{:.2f}')}), unmask "
            f"{_spread_of(r['unmask'])} (x{_spread_of(ratio_u, '{:.2f}')}); machine load "
            f"{_spread_of(r['load_mask'])} % / {_spread_of(r['load_unmask'])} %; {_spread_of(r['worker_mb'])} MB a "
            f"worker, {_spread_of(r['total_mb'])} MB in all; supervisor {_spread_of(r['supervisor_mb'])} MB; "
            f"connections a worker by round {' '.join(spread[n])}"
        )
    if 1 in results and 8 in results:
        met = [statistics.median(results[8][op]) >= 5 * statistics.median(results[1][op]) for op in ("mask", "unmask")]
        print(f"target (8 workers at least 5x one, mask and unmask): {'met' if all(met) else 'missed'}")


# --- the command -------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    folder = Path(tempfile.gettempdir()) / "latet-bench-apps"
    found.add_argument("--folder", type=Path, default=folder, help="where a build's state is kept")
    commands = found.add_subparsers(dest="command", required=True)
    made = commands.add_parser("build", help="a schema, an API key, its vault filled")
    made.add_argument("--values", type=int, default=200_000)
    made.add_argument("--seed", type=int, default=1)
    made.add_argument("--person", action="store_true", help="a person's identity and vault, not an API key's")
    run = commands.add_parser("serve", help="bench_apps.py against the server, its options after --")
    run.add_argument("--working", type=int, default=KEY_WORKING_VALUES, help="key_working_values")
    run.add_argument("--held", type=int, default=KEY_HELD_SECRETS, help="key_held_secrets")
    run.add_argument("--new-key", action="store_true", help="an API key of its own, its vault empty")
    run.add_argument(
        "--wait-index",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="wait for the measured key's print index before starting the load",
    )
    run.add_argument(
        "--index",
        choices=("on", "off"),
        default="on",
        help="the key stores' index of prints; off: the SQL lookup on the same source (with --no-wait-index)",
    )
    run.add_argument("--profile", action="store_true", help="where mask's and unmask's time goes in the server")
    run.add_argument("--readiness-only", action="store_true")
    run.add_argument("--cached-arm", choices=("500+1", "1-alone"))
    run.add_argument("--cached-pair", type=int, choices=range(3), default=0)
    run.add_argument("--cached-fixture", type=Path)
    run.add_argument("--cached-output", type=Path)
    fixture = commands.add_parser("cached-fixture", help="prepare and freeze the 501 known-text fixture")
    fixture.add_argument("--output", type=Path, required=True)
    pairs = commands.add_parser("cached-pairs", help="validate and report the fixed three cached pairs")
    pairs.add_argument("results", type=Path, nargs=6)
    run.add_argument(
        "--analyze", type=float, default=0, help="the vault's tables analyzed every so many seconds (0: by PostgreSQL)"
    )
    han = commands.add_parser("words", help="2 MB of Chinese")
    han.add_argument("--chars", type=int, default=2_000_000)
    spread = commands.add_parser("fanout", help="the entries a log line opens")
    spread.add_argument("--sizes", type=int, nargs="*", default=[200_000, 1_000_000])
    commands.add_parser("weigh", help="which of the values found a text holds")
    many = commands.add_parser("unmasks", help="unmasks of several keys at once")
    many.add_argument("--keys", type=int, nargs="*", default=[1, 4, 8])
    many.add_argument("--clients", type=int, default=4)
    many.add_argument("--batch", type=int, default=100)
    many.add_argument("--seconds", type=float, default=20)
    loaded = commands.add_parser("load", help="(used by unmasks)")
    for name in ("--url", "--token-file", "--texts"):
        loaded.add_argument(name, required=True)
    loaded.add_argument("--clients", type=int, default=4)
    loaded.add_argument("--batch", type=int, default=100)
    loaded.add_argument("--seconds", type=float, default=20)
    commands.add_parser("missing", help="what a key's space build asks PostgreSQL first")
    commands.add_parser("drop", help="the schema and the prefix removed")
    workers_ = commands.add_parser(
        "workers",
        help="`latet server run --workers N`'s throughput for each N, over a fixed time, four connections a worker at "
        "least, each worker's connections and processor time recorded",
    )
    workers_.add_argument("--counts", type=int, nargs="+", default=[1, 2, 4, 8])
    workers_.add_argument("--rounds", type=int, default=3)
    workers_.add_argument(
        "--lines",
        type=int,
        help="distinct lines offered to every count (default and most: the vault's values)",
    )
    workers_.add_argument("--batch", type=int, default=62, help="lines a call")
    workers_.add_argument("--seed", type=int, default=1)
    workers_.add_argument("--per-worker", type=int, default=4, help="connections a worker, four at least")
    workers_.add_argument("--seconds", type=float, default=20.0, help="the fixed time of each phase, mask and unmask")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    rest: list[str] = []
    if "--" in given:
        given, rest = given[: given.index("--")], given[given.index("--") + 1 :]
    args = parser().parse_args(given)
    if args.command != "load":
        _on_this_computer()
        # A space looks for the name model in the process's data folder (``latet.config.data_dir``), whatever folder
        # the instance was opened with: one of the measurement's own, empty, so that none is used.
        os.environ["LATET_DATA_DIR"] = str(args.folder / "models")
    commands: dict[str, Callable[[argparse.Namespace], None]] = {
        "build": build, "words": words, "fanout": fanout, "weigh": weigh, "unmasks": unmasks, "load": load,
        "missing": missing, "drop": drop, "cached-fixture": cached_fixture, "cached-pairs": cached_pairs,
        "workers": workers,
    }  # fmt: skip
    if args.command == "serve":
        serve(args, rest)
    else:
        commands[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
