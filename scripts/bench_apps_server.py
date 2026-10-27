"""The measurements of the applications API behind its documentation (notes/apps-api-plan.md, Task 10), on a Latet
server opened in this process the way the server's tests open one (``tests/server/support.py``): on the test services
(`uv run python scripts/server-services.py up`, then the two variables `env` prints), in a schema and a Valkey prefix
of its own, with no models (no name model, no text recognition). Never a real server, nor through `latet server`
commands.

    uv run python scripts/bench_apps_server.py build --values 200000   # a schema, an API key, its vault filled
    uv run python scripts/bench_apps_server.py serve --working 5000 -- --lines 40000 --documents 0 --no-unmask
    uv run python scripts/bench_apps_server.py serve --new-key --analyze 20     # an API key of its own, empty
    uv run python scripts/bench_apps_server.py serve --new-key --held 0 -- --lines 5000 --session-tokens  # none held
    uv run python scripts/bench_apps_server.py serve --profile -- --lines 1000  # and where a call's time goes
    uv run python scripts/bench_apps_server.py words      # 2 MB of Chinese: its words, their hashing, the store
    uv run python scripts/bench_apps_server.py fanout     # the values a log line opens, at 200,000 and 1,000,000
    uv run python scripts/bench_apps_server.py weigh      # which of the values found a text holds
    uv run python scripts/bench_apps_server.py unmasks    # unmasks of several keys at once, and the other work
    uv run python scripts/bench_apps_server.py missing    # what a key's space build asks PostgreSQL first
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
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # the server's tests' helpers, ``tests.server.support``

import bench_apps  # noqa: E402
import httpx  # noqa: E402
import psycopg  # noqa: E402
import uvicorn  # noqa: E402
from psycopg import sql  # noqa: E402
from terms_bench import memory_mb  # noqa: E402
from tests.server.support import DATABASE, URL, VALKEY  # noqa: E402

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
    """What ``build`` made, kept in ``folder``: the schema and the prefix, the master key, the API key and its token."""

    folder: Path
    schema: str = ""
    prefix: str = ""
    key: str = ""
    values: int = 0
    seed: int = 1

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
        (self.folder / "state.json").write_text(json.dumps({**found, "seed": self.seed}), "utf-8")

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
        migrate(db)
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
    two strings, its two words and its edges), a few thousand in a transaction, and the change counted once."""
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
            value_word, fake_word = store._words(e)
            words.append((identity, e.id, value_word, ANCHORS_VERSION, identity, e.id, fake_word, ANCHORS_VERSION))
            its = store._edges(e, "original") + store._edges(e, "surrogate")
            if its:
                edged.append((identity, e.id, ANCHORS_VERSION, [role for role, _ in its], [word for _, word in its]))
        with store.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.executemany(_INSERT, rows)
            cursor.executemany(_FAKE, fakes)
            cursor.executemany(_VALUE, values)
            cursor.executemany(_WORDS, words)
            if edged:
                cursor.executemany(_EDGES, edged)
        said(start + len(rows))
    with store.db.transaction() as conn:
        conn.execute(_COUNT, (identity,))


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


def serve(args: argparse.Namespace, rest: Sequence[str]) -> None:
    state = State.load(args.folder)
    token = state.token
    try:
        with (
            timing() as timed,
            profiled(args.profile) as profiles,
            Sampled() as sampled,
            serving(state, args.working, args.held) as (server, url),
            contextlib.ExitStack() as stack,  # closed first: what it holds uses the server
        ):
            if args.new_key:
                _, secret = _new_key(server, "bench, new")
                token = state.folder / "token-new.txt"
                _secret_file(token, secret)
            if args.analyze:
                stack.enter_context(analyzing(server, args.analyze))
            before = memory_mb()
            started = time.perf_counter()
            command = [sys.executable, str(HERE / "bench_apps.py"), "--url", url, "--token-file", str(token), *rest]
            said = (
                f"key_working_values {args.working}, key_held_secrets {args.held}; vault {state.values} values; "
                f"bench_apps.py {' '.join(rest)}"
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


# --- the command -------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    folder = Path(tempfile.gettempdir()) / "latet-bench-apps"
    found.add_argument("--folder", type=Path, default=folder, help="where a build's state is kept")
    commands = found.add_subparsers(dest="command", required=True)
    made = commands.add_parser("build", help="a schema, an API key, its vault filled")
    made.add_argument("--values", type=int, default=200_000)
    made.add_argument("--seed", type=int, default=1)
    run = commands.add_parser("serve", help="bench_apps.py against the server, its options after --")
    run.add_argument("--working", type=int, default=KEY_WORKING_VALUES, help="key_working_values")
    run.add_argument("--held", type=int, default=KEY_HELD_SECRETS, help="key_held_secrets")
    run.add_argument("--new-key", action="store_true", help="an API key of its own, its vault empty")
    run.add_argument("--profile", action="store_true", help="where mask's and unmask's time goes in the server")
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
        "missing": missing, "drop": drop,
    }  # fmt: skip
    if args.command == "serve":
        serve(args, rest)
    else:
        commands[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
