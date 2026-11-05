"""The engine's parts measured on one thread, in this process (the server performance plan, Task 1; the spike's
``notes/perf-spike/m1_detectors.py`` and ``m3_known.py`` on the product), each number beside the machine's load over
the time it took, since other work on this computer slows it: compare numbers taken in the same run.

    uv run python scripts/bench_engine.py detectors --rounds 7
    uv run python scripts/bench_engine.py known --folder FOLDER [--rounds 3] [--calls 4] [--fuzz 5000]
    uv run python scripts/bench_engine.py ready --folder FOLDER [--rounds 5] [--reads 300]

``detectors``: ``Masker.spans`` of the balanced profile with no name model and no known values, on the bench corpora
of ``spans_golden.py`` (2,000 log lines, the first 500 as JSON with escapes, ten documents of 20 KB), each corpus read
in applications calls of 500 texts (``Masker.joined``): texts a second for each corpus, with the generic detectors'
switches on ("now") and off ("switches off": 0.10.0's way, the personal-data scans all run, every gitleaks rule asked
for its keywords, the repeats found by a detector compiled for each text, each text of a call read alone), the time of
one against the other round by round, and the texts whose spans differ from 0.10.0's
(``tests/data/spans-0.10.0.json.gz``, each text read alone: a call read joined finds in a text the values of the others
too, which a text read alone finds once the vault holds them).

``known``: the known values and their fakes sent back (the echo) alone, as a key's space finds them in a call's texts,
on a bench vault built by ``bench_apps_server.py --folder FOLDER build --values N`` on the test services (its two
variables set): a plain ``Vault`` holding every value of the vault (0.10.0's in-memory vault), against the key's own
``StoredVault`` over its store (memory holding ``--working`` values, the server's default). Calls of 500 log lines of
the vault's seed, each call's ``prefetch`` (an applications call's, ``holding(scoped=True)``), then every reading of
each text (``read_escaped``) read by the vault's detectors of values and of fakes for it (``vault.matchers.for_text``: a
stored vault's, those the text's words find and those its detectors hold), as the masker reads them. First the stored
vault's calls alone (lines a second with no plain vault in the process, which holds gigabytes at a million values and
halves the other's speed beside it); then the plain vault made, and the characters one finds and the other does not,
text by text, on the lines; then the same on the fuzz's texts (``--fuzz``), which hold the fuzz harness's values and
none of a bench vault's: those values are kept in a key's vault of their own, in a schema of its own on the test
PostgreSQL (``vault_fuzz.PgStores``, dropped after), its memory holding none of them but those no word finds glued on
both sides (``glued_all``), against a plain vault holding them (``fuzz_differ``); then lines a second, the two in turn,
call by call; and the private bytes each took. Each comparison says in how many texts each side found something. It
keeps nothing in the bench vault (the fuzz harness's short secrets kept in a bench vault have their fakes found inside
the log lines' hexadecimal ids, and ``serve``'s lines no longer come back as they were sent).

``ready``: the prints of a bench vault held in this process (``latet.server.index.PrintIndex``; the spike's
``notes/perf-spike/m6_ready.py`` on the product), on a build of ``bench_apps_server.py`` (one built before 0.11.0 has
its prints made first, ``PgVaultStore.fill_prints``, timed): the rows and the bytes the index reads, the seconds it
takes to build and what it weighs (``nbytes``, and per million values); what a call pays to know it is current, the
change counter read with nothing new (``catch_up``, median and 95th percentile of ``--reads``); and the catch-up of 1,
100 and 1,000 values kept by another store of the key (an applications call's ``together`` for more than one), each
checked present after, ``--rounds`` times each. The values it keeps for that are forgotten at the end, and the vault
holds what it held. Never a real server."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import hashlib
import json
import math
import os
import re
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import spans_golden
import vault_fuzz
from terms_bench import memory_counters, memory_mb

from latet.config import Config
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.gitleaks import GitleaksDetector
from latet.engine.detectors.pii import PiiDetector
from latet.engine.escapes import read_escaped
from latet.engine.types import SlotKind, Span

if TYPE_CHECKING:
    from latet.engine.known import Matched
    from latet.engine.masker import Masker
    from latet.engine.vault import Vault

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "tests" / "data" / "spans-0.10.0.json.gz"
CALL = 500  # log lines in an applications call
OFF, NOW = "switches off", "now"  # the masker as 0.10.0 had its generic detectors, and as it has them
_CTX = DetectContext()
_now = time.perf_counter


# --- the machine's load, memory, a bound, and numbers -----------------------------------------------------------------


def _cpu_times() -> tuple[float, float] | None:
    """The machine's idle and total processor time so far (all processors, every process); None where unknown."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        idle, kernel, user = FileTime(), FileTime(), FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None

        def value(t: FileTime) -> int:
            return (t.high << 32) | t.low

        return value(idle), value(kernel) + value(user)  # kernel time includes idle time
    try:
        with open("/proc/stat") as stat:
            fields = [float(x) for x in stat.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    return fields[3] + (fields[4] if len(fields) > 4 else 0.0), sum(fields)


class Load:
    """The machine's processor load between its making and ``percent()``, in %, every process's (nan where unknown)."""

    def __init__(self) -> None:
        self._start = _cpu_times()

    def percent(self) -> float:
        sample = self.sample()
        return sample["percent"] if sample["percent"] is not None else float("nan")

    def sample(self) -> dict[str, Any]:
        """One endpoint read; additive raw deltas allow exact aggregation across unequal intervals."""
        now = _cpu_times()
        if now is None or self._start is None:
            return {"status": "unavailable", "idle": None, "total": None, "percent": None}
        idle, total = now[0] - self._start[0], now[1] - self._start[1]
        valid = math.isfinite(idle) and math.isfinite(total) and total > 0 and 0 <= idle <= total
        return {
            "status": "ok" if valid else "invalid",
            "idle": idle if math.isfinite(idle) else None,
            "total": total if math.isfinite(total) else None,
            "percent": 100.0 * (total - idle) / total if valid else None,
        }


class MemoryObservation:
    """10 ms samples are lower bounds on interval highs; native peaks remain lifetime counters."""

    def __init__(self, store: Callable[[], Any] = lambda: None):
        self.store = store
        self.stop = threading.Event()
        self.high: dict[str, int] = {}
        self.samples = 0
        self.errors: list[str] = []
        self.before: dict[str, object] = {}
        self.after: dict[str, object] = {}

    def sample(self) -> None:
        try:
            counters = memory_counters()
            if counters["status"] != "ok":
                raise RuntimeError("native memory unavailable")
            for name, value in counters.items():
                if name.startswith("current_") and isinstance(value, int):
                    self.high[name] = max(self.high.get(name, 0), value)
            store = self.store()
            if store is not None:
                building = store._building_index
                published = store.index
                for name, value in {
                    "reserved_build_bytes": building.building_bytes if building is not None else 0,
                    "store_index_bytes": store.index_bytes,
                    "published_array_bytes": published.nbytes if published is not None else 0,
                }.items():
                    self.high[name] = max(self.high.get(name, 0), value)
            self.samples += 1
        except Exception as error:
            self.errors.append(type(error).__name__)

    def __enter__(self):
        self.before = memory_counters()
        self.sample()

        def watch():
            while not self.stop.wait(0.01):
                self.sample()

        self.thread = threading.Thread(target=watch, name="bench-memory", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(5)
        if self.thread.is_alive():
            self.errors.append("sampler-stop-timeout")
        self.sample()
        self.after = memory_counters()

    def report(self) -> dict[str, Any]:
        return {
            "units": "bytes",
            "sample_interval_seconds": 0.01,
            "sampled_high_lower_bounds": self.high,
            "samples": self.samples,
            "native_before": self.before,
            "native_after": self.after,
            "errors": self.errors,
            "windows_native_peak_evidence": all(
                name in self.before and name in self.after
                for name in ("current_private_bytes", "lifetime_peak_working_set_bytes", "lifetime_peak_pagefile_bytes")
            ),
            "qualified": not self.errors and self.before.get("status") == self.after.get("status") == "ok",
        }


def atomic_record(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n", "utf-8")
    temporary.replace(path)


def measurement_manifest(path: Path, schema: str, purpose: str, *, planned: bool = True) -> dict[str, Any]:
    """Validate the explicit single-schema authority before allocating a connection or store."""
    record = json.loads(path.read_text("utf-8"))
    if record.get("version") != "task8-measurement-v1" or record.get("purpose") != purpose:
        raise ValueError("measurement manifest version/purpose mismatch")
    campaign = record.get("campaign", "")
    ordinal = record.get("ordinal")
    if purpose not in {"known", "interior"} or re.fullmatch(r"[0-9a-f]{12}", campaign) is None:
        raise ValueError("invalid campaign")
    if type(ordinal) is not int or not 0 <= ordinal <= 3:
        raise ValueError("invalid ordinal")
    expected = f"latet_t_keyfuzz_task8{purpose}_{campaign}_{ordinal}"
    if schema != expected or record.get("schema") != expected:
        raise ValueError("exact schema mismatch")
    if record.get("count") != (5000 if purpose == "known" else 1000):
        raise ValueError("fixed corpus count mismatch")
    if record.get("index") not in {"on", "off"} or (purpose == "known" and record["index"] != "on"):
        raise ValueError("index mode mismatch")
    if planned and record.get("phase") != "planned":
        raise ValueError("measurement manifest already used")
    if not Path(record["output"]).is_absolute() or Path(record["output"]).resolve().parent != path.resolve().parent:
        raise ValueError("output must be beside its manifest")
    if planned and Path(record["output"]).exists():
        raise ValueError("measurement output already exists")
    root = Path(record["source_root"]).resolve()
    if root != HERE.parent:
        raise ValueError("measurement helper source root mismatch")
    pins = record.get("pins", {})
    required = {
        "scripts/bench_engine.py",
        "scripts/vault_fuzz.py",
        "scripts/terms_bench.py",
        "src/latet/server/vault.py",
        "src/latet/server/index.py",
        "src/latet/engine/stored.py",
    }
    if purpose == "interior":
        required |= {"scripts/bench_apps.py", "notes/captures/perf0102/task8-interior-measure.py"}
    if not required.issubset(pins):
        raise ValueError("incomplete source pins")
    for relative, expected_hash in pins.items():
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or hashlib.sha256(source.read_bytes()).hexdigest() != expected_hash.lower():
            raise ValueError("source pin mismatch")
    return record


def _measurement_connection():
    import psycopg

    return psycopg.connect(
        os.environ["LATET_TEST_DATABASE_URL"],
        autocommit=True,
        connect_timeout=5,
        options="-c statement_timeout=10000 -c lock_timeout=5000",
    )


@contextlib.contextmanager
def owned_fuzz_stores(schema: str, manifest: Path, purpose: str, *, index: bool):
    """Exact CREATE establishes ownership. No constructor failure can authorize dropping a collision."""
    from psycopg import sql

    record = measurement_manifest(manifest, schema, purpose)
    if record["index"] != ("on" if index else "off"):
        raise ValueError("requested index differs from manifest")
    stores = None
    created = False
    failures: list[BaseException] = []
    record["phase"] = "allocating"
    atomic_record(manifest, record)
    try:
        with _measurement_connection() as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            created = True
        record["created"] = True
        record["phase"] = "allocated"
        atomic_record(manifest, record)
        stores = vault_fuzz.PgStores(schema=schema, index=index)
        yield stores
        record["outcome"] = "completed"
    except BaseException as error:
        failures.append(error)
        record["outcome"] = "failed"
    finally:
        if stores is not None:
            try:
                stores.close(drop=False)
            except BaseException as error:
                failures.append(error)
        record["cleanup_verified"] = False
        if created:
            try:
                with _measurement_connection() as connection:
                    connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
                    remaining = connection.execute(
                        "SELECT nspname FROM pg_namespace WHERE nspname = %s", (schema,)
                    ).fetchall()
                    if remaining:
                        raise RuntimeError("owned measurement schema remains")
                record["cleanup_verified"] = True
            except BaseException as error:
                failures.append(error)
        record["phase"] = "cleanup_verified" if record["cleanup_verified"] else "cleanup_unverified"
        record["errors"] = [type(error).__name__ for error in failures]
        try:
            atomic_record(manifest, record)
        except BaseException as error:
            failures.append(error)
    if failures:
        raise BaseExceptionGroup("measurement and cleanup failures", failures)


@contextlib.contextmanager
def measurement_store(stores):
    store = stores.new()
    failures = []
    try:
        yield store
    except BaseException as error:
        failures.append(error)
    finally:
        try:
            store.close()
        except BaseException as error:
            failures.append(error)
    if failures:
        raise BaseExceptionGroup("measurement and store-close failures", failures)


def guard(seconds: float, megabytes: float) -> None:
    """This process stopped (exit 3) once its memory passes ``megabytes``, or (exit 4) after ``seconds``."""
    deadline = time.monotonic() + seconds

    def watch() -> None:
        while True:
            if memory_mb() > megabytes:
                print(f"GUARD: past {megabytes:.0f} MB, stopped", flush=True)
                os._exit(3)
            if time.monotonic() > deadline:
                print(f"GUARD: past {seconds:.0f} s, stopped", flush=True)
                os._exit(4)
            time.sleep(0.25)

    threading.Thread(target=watch, name="bench-guard", daemon=True).start()


def spread(values: Iterable[float], fmt: str = "{:.0f}") -> str:
    """median [min-max] (n)"""
    found = sorted(v for v in values if not math.isnan(v))
    if not found:
        return "-"
    return f"{fmt.format(statistics.median(found))} [{fmt.format(found[0])}-{fmt.format(found[-1])}] (n={len(found)})"


def ratios(a: Sequence[float], b: Sequence[float]) -> list[float]:
    """Round by round, b / a."""
    return [y / x for x, y in zip(a, b, strict=True) if x]


# --- detectors -------------------------------------------------------------------------------------------------------


def _golden_by_corpus() -> dict[str, list[list[Any]]]:
    """0.10.0's spans of each corpus, by its name; none when the file is not there."""
    if not GOLDEN.exists():
        return {}
    golden = spans_golden.load(GOLDEN)
    out, at = {}, 0
    for corpus in golden["corpora"]:
        out[corpus["name"]] = golden["spans"][at : at + corpus["texts"]]
        at += corpus["texts"]
    return out


def switches_off() -> Masker:
    """``spans_golden.masker()`` with the generic detectors' switches off, as 0.10.0 had them: every personal-data scan
    run on every text (``prefilter=False``), every gitleaks rule asked for its keywords (``by_keyword=False``), the
    repeats of a text's values found by an RE2 detector compiled for it (``plain_repeats``), and each text of an
    applications call read alone (``joins``). Later tasks' switches go here too."""
    masker = spans_golden.masker()
    masker.plain_repeats = False
    masker.joins = False
    disabled = Config(profile="balanced", ner="off").disabled_secret_rules
    masker.detectors = [
        PiiDetector(d.enabled, d.phone_regions, d.include_private_ips, prefilter=False)
        if isinstance(d, PiiDetector)
        else GitleaksDetector(disabled, by_keyword=False)
        if isinstance(d, GitleaksDetector)
        else d
        for d in masker.detectors
    ]
    return masker


def read_calls(masker: Masker, texts: Sequence[str], kind: SlotKind) -> list[list[Span]]:
    """Each text's spans, ``texts`` read in applications calls of ``CALL`` texts, each read as ``Apps`` reads one
    (``Masker.joined``: joined, unless its switch is off)."""
    found: list[list[Span]] = []
    for at in range(0, len(texts), CALL):
        call = texts[at : at + CALL]
        with masker.joined(call, kind):
            found += [masker.spans(text, kind)[0] for text in call]
    return found


def detectors(args: argparse.Namespace) -> int:
    corpora = [c for c in spans_golden.corpora() if c.name != "fuzz texts"]
    variants = {OFF: switches_off(), NOW: spans_golden.masker()}
    golden = _golden_by_corpus()
    python = sys.version.split()[0]
    print(f"rounds {args.rounds}; one thread; python {python}; private {memory_mb():.0f} MB", flush=True)
    for masker in variants.values():  # warm: the patterns' caches, the lazy imports
        for corpus in corpora:
            for text in corpus.texts[:20]:
                masker.spans(text, corpus.kind)
    times: dict[tuple[str, str], list[float]] = {}
    loads: dict[str, list[float]] = {}
    differ: dict[tuple[str, str], int] = {}
    for round_ in range(args.rounds):
        order = list(variants.items())
        if round_ % 2:
            order.reverse()
        for corpus in corpora if round_ % 2 == 0 else corpora[::-1]:
            for name, masker in order:
                load = Load()
                started = _now()
                found = read_calls(masker, corpus.texts, corpus.kind)
                times.setdefault((corpus.name, name), []).append(_now() - started)
                loads.setdefault(corpus.name, []).append(load.percent())
                if round_ == 0 and corpus.name in golden:
                    now = [[[s.start, s.end, s.etype, s.eclass.name, s.rule] for s in spans] for spans in found]
                    differ[corpus.name, name] = sum(a != b for a, b in zip(golden[corpus.name], now, strict=True))
        print(f"round {round_ + 1} done, private {memory_mb():.0f} MB", flush=True)
    print()
    for corpus in corpora:
        n = len(corpus.texts)
        print(f"== {corpus.name}: {n} texts ({corpus.kind.value}); machine load {spread(loads[corpus.name])} %")
        base = times[corpus.name, OFF]
        for name in variants:
            took = times[corpus.name, name]
            said = f"spans differ: {differ[corpus.name, name]} (of {n} texts)" if differ else "no golden file"
            versus = "" if name == OFF else f"; time vs {OFF}: {spread(ratios(base, took), '{:.2f}')}"
            print(
                f"  {name:<12} {spread([n / t for t in took]):>24} texts/s; "
                f"{spread([t / n * 1e6 for t in took]):>24} us a text; {said}{versus}"
            )
    if differ:
        print(f"\nspans differ: {sum(differ.values())} (texts, all corpora and both ways, from 0.10.0's)")
    return 0


# --- known values ----------------------------------------------------------------------------------------------------


def matched_for(vault: Vault, text: str) -> Matched:
    """The detectors of values and fakes ``text`` is read with, as the masker asks the vault for them
    (``Matchers.for_text``, the plan's Task 7: a stored vault's for the values the text's words find and those its
    detectors hold; 0.10.0 read those of everything memory held, ``current()``)."""
    return vault.matchers.for_text(text)


def vault_state(vault: Any) -> dict[str, int]:
    """What a key's stored vault holds as it reads (Task 8's fix round 7): its values in memory, those held for good,
    the secrets held glued, and those its detectors hold (``matched``), which every text is read for whatever its words.
    Known compares populations in one state, and its record says which: a population a serve taught one-run secrets
    to reads every text for them (``StoredVault.matched``), one only built does not. And what its detectors' automata
    take past the ordinary budget, in bytes (``StoredVault.detectors_bytes``, fix round 8), weighed with its space."""
    return {
        "memory": len(vault.entities()),
        "held_for_good": len(vault._for_good),
        "held_glued": len(vault._glued),
        "matched": len(vault._matched),
        "detectors_bytes": vault.detectors_bytes,
    }


def read_call(vault: Vault, texts: Sequence[str], scoped: bool) -> list[tuple[list[Span], list[Span]]]:
    """A call's texts read for known values and for fakes sent back, as the masker reads them: the call's
    ``prefetch`` (an applications call's, ``scoped``), then each reading of each text (``read_escaped``) read by the
    detectors of values (``known``) and of fakes (``echo``)."""
    out: list[tuple[list[Span], list[Span]]] = []
    with vault.holding(scoped=scoped):
        vault.prefetch(texts)
        for text in texts:
            if not text:
                out.append(([], []))
                continue
            matched = matched_for(vault, text)
            out.append((_read(text, matched.known), _read(text, matched.echo)))
            matched.seen()  # what the text held brought into memory, as the masker tells the vault
    return out


def _read(text: str, detector: Any) -> list[Span]:
    """What ``detector`` finds in each reading of ``text``."""
    return read_escaped(text, lambda t: list(detector.detect(t, _CTX)))


@dataclass
class Differ:
    """What two vaults found differently in some texts: the characters each covers that the other does not, the texts
    where the first covers more, and a few of those; and the texts where each found something at all, so that a
    comparison of vaults that find nothing in the texts shows as such."""

    texts: int = 0
    fewer: int = 0  # characters the plain vault covers and the stored one does not
    more: int = 0  # characters the stored vault covers and the plain one does not
    texts_fewer: int = 0
    found_plain: int = 0  # texts where the plain vault found something
    found_stored: int = 0  # texts where the stored vault found something
    examples: list[str] = field(default_factory=list)

    def add(self, text: str, plain: tuple[list[Span], list[Span]], stored: tuple[list[Span], list[Span]]) -> None:
        a = {i for s in [*plain[0], *plain[1]] for i in range(s.start, s.end)}
        b = {i for s in [*stored[0], *stored[1]] for i in range(s.start, s.end)}
        self.texts += 1
        self.fewer += len(a - b)
        self.more += len(b - a)
        self.found_plain += bool(a)
        self.found_stored += bool(b)
        if a - b:
            self.texts_fewer += 1
            if len(self.examples) < 3:
                gone = sorted(
                    {(s.start, s.end, s.etype) for s in [*plain[0], *plain[1]]}
                    - {(s.start, s.end, s.etype) for s in [*stored[0], *stored[1]]}
                )
                self.examples.append(f"{text[:80]!r}: {gone[:3]}")

    def line(self, name: str) -> str:
        said = (
            f"{name}: found something in {self.found_plain} (plain) and {self.found_stored} (key's) of {self.texts} "
            f"texts; characters the plain vault finds and the key's does not {self.fewer} (in {self.texts_fewer} "
            f"texts); the key's finds more {self.more}"
        )
        return said + (f"; e.g. {self.examples}" if self.examples else "")


def _calls(texts: Sequence[str], size: int = CALL) -> list[list[str]]:
    return [list(texts[i : i + size]) for i in range(0, len(texts), size)]


def fuzz_differ(store: Any, texts: Sequence[str], *, working: int, held: int) -> Differ:
    """``texts`` (the fuzz's) read in calls of 500 by a plain ``Vault`` holding the fuzz harness's values and by a key's
    ``StoredVault`` over ``store``, a store of their own (``vault_fuzz.PgStores``' on PostgreSQL, never a build's
    vault), which holds them, kept through a vault of it as a space keeps a value, and whose memory holds ``working``
    of them besides the ``held`` secrets held glued and those no word finds glued on both sides (``glued_all``, the
    plan's S4: an API key's lets those go with its other values): what one finds and the other does not."""
    from latet.engine.stored import StoredVault
    from latet.engine.vault import Vault

    plain = Vault(vault_fuzz.ROOT_KEY)
    keeper = StoredVault(vault_fuzz.ROOT_KEY, store, working=0)
    for etype, eclass, value in vault_fuzz.VALUES:
        plain.intern(etype, eclass, value)
        keeper.intern(etype, eclass, value)
    stored = StoredVault(vault_fuzz.ROOT_KEY, store, working=working, held_at_most=held, glued_all=True)
    differ = Differ()
    for call in _calls(texts):
        for text, a, b in zip(call, read_call(plain, call, True), read_call(stored, call, True), strict=True):
            differ.add(text, a, b)
    return differ


@contextlib.contextmanager
def _bench_store(folder: Path, pool: int = 4, *, index: bool = False, build: bool = True):
    """The bench vault built in ``folder``: its state, its root key, and the key's ``PgVaultStore`` over its schema
    (migrated first, as ``bench_apps_server.py`` opens it), closed after."""
    import bench_apps_server as bas

    from latet.server.db import Database, migrate
    from latet.server.master import MasterKey, root_key
    from latet.server.settings import ServerSettings
    from latet.server.vault import PgVaultStore

    bas._on_this_computer()
    state = bas.State.load(folder)
    settings = ServerSettings(
        url="http://127.0.0.1:1", database_url=bas.DATABASE, valkey_url=bas.VALKEY, master_key_file=state.master
    )
    db = Database(bas.DATABASE, schema=state.schema, max_size=pool)
    try:
        master = MasterKey.load(settings)
        migrate(db, master=master)
        root = root_key(db, master, state.key)
        store = PgVaultStore(db, state.key, root, limit=bas.KEY_LIMIT, index=index)
        if index and build and not store.build_index():
            raise RuntimeError("the benchmark's indexed store did not become ready")
        try:
            yield state, root, store
        finally:
            store.close()
    finally:
        db.close()


def known(args: argparse.Namespace) -> int:
    folder: Path = args.folder
    if not (folder / "state.json").exists():
        raise SystemExit(
            f"nothing was built in {folder}: `uv run python scripts/bench_apps_server.py --folder {folder} build "
            "--values N` first, on the test services"
        )
    if args.fuzz:
        if not args.fuzz_schema or not args.fuzz_manifest or args.fuzz != 5000:
            raise SystemExit("known fuzz qualification requires count5000 and an explicit schema/manifest pair")
        measurement_manifest(args.fuzz_manifest, args.fuzz_schema, "known")
    guard(args.seconds, args.megabytes)
    import bench_apps

    from latet.engine.stored import StoredVault
    from latet.engine.vault import Vault

    with _bench_store(folder, index=True) as (state, root, store):
        print(
            f"vault {folder.name}: {state.values:,} values built, seed {state.seed}; working {args.working:,}, held "
            f"{args.held:,}; rounds {args.rounds}, {args.calls} calls of {CALL} lines, {args.fuzz} fuzz texts; "
            f"private {memory_mb():.0f} MB",
            flush=True,
        )
        lines = bench_apps.lines(CALL * args.calls, state.seed, 0.5)
        calls = _calls(lines)
        gc.collect()
        b_before, started = memory_mb(), _now()
        stored = StoredVault(root, store, working=args.working, held_at_most=args.held)
        stored.matchers.current()
        b_build, b_mem = _now() - started, memory_mb() - b_before
        b_state = {"start": vault_state(stored)}
        print(
            f"B, the key's stored vault: made in {b_build:.1f} s, private +{b_mem:.0f} MB "
            f"({len(stored.entities()):,} values in memory); held {b_state['start']}",
            flush=True,
        )
        # B alone first: a process that also holds the plain vault (gigabytes at a million values) runs B's calls at
        # half their speed or less, which no server's process does (1M values: 650 lines a second alone, 355 beside it).
        alone, alone_loads = [], []
        for _ in range(args.rounds):
            for call in calls:
                load = Load()
                started = _now()
                read_call(stored, call, True)
                alone.append(_now() - started)
                alone_loads.append(load.percent())
        gc.collect()
        b_after, b_values = memory_mb() - b_before, len(stored.entities())
        b_state["after_alone"] = vault_state(stored)
        print(
            f"B alone (no plain vault in the process): {spread([CALL / x for x in alone])} lines/s; "
            f"{spread([x * 1e3 for x in alone])} ms a call (load {spread(alone_loads)} %)",
            flush=True,
        )
        gc.collect()
        before, started = memory_mb(), _now()
        entities = store.load_entities()
        read_at, opened = _now() - started, memory_mb() - before
        gc.collect()
        before, started = memory_mb(), _now()
        plain = Vault(root, entities)
        del entities
        plain.matchers.current()
        a_build, a_mem = _now() - started, memory_mb() - before
        parts = len(plain.matchers.current().known._parts)
        print(
            f"A, a plain vault of {plain.count:,} values: entries read in {read_at:.1f} s (private +{opened:.0f} MB), "
            f"vault and detectors made in {a_build:.1f} s, private +{a_mem:.0f} MB ({parts} parts of values)",
            flush=True,
        )
        differ = Differ()
        for call in calls:
            for text, a, b in zip(call, read_call(plain, call, True), read_call(stored, call, True), strict=True):
                differ.add(text, a, b)
        print(differ.line("log lines (the vault's values)"), flush=True)
        if args.fuzz:  # the fuzz's texts hold its harness's values, which no bench vault holds: kept in a schema apart
            with (
                owned_fuzz_stores(args.fuzz_schema, args.fuzz_manifest, "known", index=True) as stores,
                measurement_store(stores) as fuzz_store,
            ):
                found = fuzz_differ(fuzz_store, vault_fuzz.fuzz_texts(args.fuzz), working=0, held=args.held)
            name = f"fuzz texts (the fuzz's {len(vault_fuzz.VALUES)} values in a schema of their own, memory 0)"
            print(found.line(name), flush=True)
        timed: dict[str, list[float]] = {"A": [], "B": []}
        loads: list[float] = []
        ways: dict[str, Callable[[list[str]], object]] = {
            "A": lambda call: read_call(plain, call, True),
            "B": lambda call: read_call(stored, call, True),
        }
        for round_ in range(args.rounds):
            for c, call in enumerate(calls):
                order = ["A", "B"] if (round_ + c) % 2 == 0 else ["B", "A"]
                for way in order:
                    load = Load()
                    started = _now()
                    ways[way](call)
                    timed[way].append(_now() - started)
                    loads.append(load.percent())
            print(f"round {round_ + 1} done, private {memory_mb():.0f} MB", flush=True)
        a, b = timed["A"], timed["B"]
        print(f"\n== {plain.count:,} values: calls of {CALL} log lines, known values and echo (load {spread(loads)} %)")
        for way, label in (("A", "A plain vault: "), ("B", "B stored vault:")):
            rate, each = spread([CALL / x for x in timed[way]]), spread([x * 1e3 for x in timed[way]])
            print(f"  {label} {rate:>26} lines/s; {each} ms a call")
        print(f"  B time / A time: {spread(ratios(a, b), '{:.3f}')}")
        rate, each = spread([CALL / x for x in alone]), spread([x * 1e3 for x in alone])
        print(f"  B alone:        {rate:>26} lines/s; {each} ms a call (load {spread(alone_loads)} %)")
        print(
            f"  memory: A +{a_mem:.0f} MB (vault and detectors, the entries read apart); B +{b_mem:.0f} MB made, "
            f"+{b_after:.0f} MB after its calls alone ({b_values:,} values in memory); private {memory_mb():.0f} MB"
        )
    qualified = not (
        differ.fewer
        or differ.more
        or (args.fuzz and (found.fewer or found.more))
        or any(not math.isfinite(value) for value in [*alone_loads, *loads])
    )
    fields = ("texts", "fewer", "more", "texts_fewer", "found_plain", "found_stored")
    result = {
        "measurement": "known",
        "population": state.values,
        "rounds": args.rounds,
        "calls": args.calls,
        "fuzz_count": args.fuzz,
        "alone_seconds": alone,
        "b_state": b_state,
        "interleaved_seconds": timed,
        "alone_load_percent": [value if math.isfinite(value) else None for value in alone_loads],
        "interleaved_load_percent": [value if math.isfinite(value) else None for value in loads],
        "differ": {name: getattr(differ, name) for name in fields},
        "fuzz_differ": {name: getattr(found, name) for name in fields} if args.fuzz else None,
        "qualified": qualified,
    }
    print(json.dumps(result, allow_nan=False), flush=True)
    if args.fuzz:
        manifest = json.loads(args.fuzz_manifest.read_text("utf-8"))
        atomic_record(Path(manifest["output"]), result)
    return 0 if qualified else 1


# --- a person's space (Task 9) ---------------------------------------------------------------------------------------


def person(args: argparse.Namespace) -> int:
    """A person's space masking the bench lines as its gateway masks a tool's output (``Engine.mask_text``, without
    HTTP): its stored vault (``StoredVault(glued_all=True)``, ``--working`` values in memory, held whole up to
    ``--whole`` values as ``runtime._person_vault`` holds it, ``stored.WHOLE_VALUES``; 0: on its store whatever its
    size) against 0.10.0's plain vault holding every value, each in an engine of its own (the space's bounds). B alone
    first, its private bytes taken before its store's index is built and after its calls; then A made, and both timed
    in turns, the machine's load beside each. Every round masks lines of its own, the alone rounds and the turns too,
    so no masked block an engine cached helps it (the Task 9 review's I5). B keeps its new values in the store, paying
    the commit and the catch-up a person's space pays; A, 0.10.0's, keeps them in memory alone (in the bench's favour of
    A)."""
    folder: Path = args.folder
    if not (folder / "state.json").exists():
        raise SystemExit(
            f"nothing was built in {folder}: `bench_apps_server.py --folder {folder} build --person` first"
        )
    guard(args.seconds, args.megabytes)
    import bench_apps

    from latet.engine.build import build
    from latet.engine.pipeline import SPACE_BLOCK_BYTES, SPACE_CACHE_BYTES
    from latet.engine.stored import StoredVault
    from latet.engine.vault import Vault

    def kept(_entity: Any) -> None:
        return None  # kept as given, in memory: the bench vault stays as built

    def engine_of(vault: Vault) -> Any:
        engine = build(Config(), vault).engine
        engine.bound(SPACE_CACHE_BYTES, SPACE_BLOCK_BYTES)
        return engine

    commits = [0]

    def masked(engine: Any, texts: Sequence[str]) -> float:
        session = engine.session("bench:person")
        if args.commit_between and engine is not a_engine_box[0]:  # another instance keeps a value before B's turn
            commits[0] += 1
            other.save_entity(Entity(f"t{commits[0]:019d}", "EMAIL", EntityClass.PII, f"turn{commits[0]}@posta.example",
                                     f"turn{commits[0]}.fake@posta.example", 0, time.time()))  # fmt: skip
        started = _now()
        for text in texts:
            engine.mask_text(text, SlotKind.TOOL_RESULT, session)
        return _now() - started

    a_engine_box: list[Any] = [None]

    with _bench_store(folder, index=True, build=False) as (state, root, store):
        if state.kind != "person":
            raise SystemExit(f"{folder} holds an API key's build: `build --person` makes a person's")
        if store.count_entities() != state.values:  # B keeps its new values: a build is measured once (n3)
            raise SystemExit(f"{folder} was measured already (its vault grew): measure a fresh `build --person`")
        if args.past_bound:  # the re-review's R-I1: B's every call past a bound, its whole vault read and shared
            from latet.engine import lookup

            lookup.LOOKUP_WORD_ROLES = 1
        from latet.engine.types import EntityClass
        from latet.engine.vault import Entity
        from latet.server.vault import PgVaultStore

        other = PgVaultStore(store.db, store.identity, root, limit=store.limit)  # another instance's store
        # The build's own lines (its seed: the customers its vault was filled with), a part of them a round.
        count = 2 * args.rounds + 1  # one to warm both, then the alone rounds, then the turns: each its own lines
        every = bench_apps.lines(args.lines * count, state.seed, 0.5)
        rounds = [every[n * args.lines : (n + 1) * args.lines] for n in range(count)]
        warm, solo, turns = rounds[0], rounds[1 : args.rounds + 1], rounds[args.rounds + 1 :]
        print(
            f"person {folder.name}: {state.values:,} values, working {args.working:,}; {args.rounds} rounds of "
            f"{args.lines} lines; private {memory_mb():.0f} MB",
            flush=True,
        )
        gc.collect()
        before, started = memory_mb(), _now()
        if not store.build_index():  # the index inside B's memory
            raise RuntimeError("the benchmark's indexed store did not become ready")
        stored = StoredVault(
            root, store, working=args.working, glued_all=True, whole_past_bounds=True, whole_at_most=args.whole
        )
        b_engine = engine_of(stored)
        b_whole = stored.holds_all
        stored.matchers.current()
        b_made, b_mem = _now() - started, memory_mb() - before
        alone = []
        for texts in solo:
            load = Load()
            alone.append((masked(b_engine, texts), load.percent()))
        gc.collect()
        b_after, b_values = memory_mb() - before, stored.count
        held = "held whole" if b_whole else "on its store"
        print(f"B, the stored vault ({held} as it was made): made in {b_made:.1f} s, private +{b_mem:.0f} MB, "
              f"+{b_after:.0f} MB after calls ({b_values:,} values in memory); alone "
              f"{spread([args.lines / s for s, _ in alone])} lines/s", flush=True)  # fmt: skip
        gc.collect()
        before, started = memory_mb(), _now()
        plain = Vault(root, store.load_entities(), on_insert=kept)
        a_engine = a_engine_box[0] = engine_of(plain)
        plain.matchers.current()
        a_made, a_mem = _now() - started, memory_mb() - before
        print(f"A, 0.10.0's plain vault of {plain.count:,} values: made in {a_made:.1f} s, private +{a_mem:.0f} MB",
              flush=True)  # fmt: skip
        masked(a_engine, warm)  # each engine warmed once on the same lines
        masked(b_engine, warm)
        timed: dict[str, list[float]] = {"A": [], "B": []}
        loads: list[float] = []
        engines = {"A": a_engine, "B": b_engine}
        for n, texts in enumerate(turns):
            for way in ("A", "B") if n % 2 == 0 else ("B", "A"):
                load = Load()
                timed[way].append(masked(engines[way], texts))
                loads.append(load.percent())
        a, b = timed["A"], timed["B"]
        print(f"\n== {state.values:,} values, {args.lines} lines a round (load {spread(loads)} %)")
        print(f"  A plain vault:  {spread([args.lines / x for x in a])} lines/s")
        print(f"  B stored vault: {spread([args.lines / x for x in b])} lines/s")
        print(f"  A time / B time: {spread(ratios(b, a), '{:.2f}')}")
        print(f"  private: A +{a_mem:.0f} MB, B +{b_mem:.0f} MB made, +{b_after:.0f} MB after its calls alone")
        other.close()
    result = {
        "measurement": "person",
        "population": state.values,
        "working": args.working,
        "whole": args.whole,
        "b_held_whole": b_whole,
        "lines": args.lines,
        "a_seconds": a,
        "b_seconds": b,
        "b_alone_seconds": [s for s, _ in alone],
        "load_percent": [value if math.isfinite(value) else None for value in loads],
        "a_private_mb": a_mem,
        "b_private_mb": b_mem,
        "b_private_after_mb": b_after,
    }
    print(json.dumps(result, allow_nan=False), flush=True)
    return 0


# --- the index of prints ----------------------------------------------------------------------------------------------


def _quantiles(seconds: Sequence[float]) -> str:
    """The median and the 95th percentile, in milliseconds."""
    found = sorted(seconds)
    if not found:
        return "-"
    p95 = found[min(len(found) - 1, int(len(found) * 0.95))]
    return f"p50 {statistics.median(found) * 1e3:.2f} ms, p95 {p95 * 1e3:.2f} ms (n={len(found)})"


def _catch_up_values(n: int) -> list[Any]:
    """``n`` new addresses and their fakes, of no other value's or fake's: a domain of their own, a random tag."""
    import secrets

    from latet.engine.types import EntityClass
    from latet.engine.vault import Entity

    tag, now = secrets.token_hex(6), time.time()
    value, fake = "kept{k}.{tag}@catch-up.invalid", "fake{k}.{tag}@catch-up.invalid"
    return [
        Entity(
            f"bench-{tag}-{k}", "EMAIL", EntityClass.PII, value.format(k=k, tag=tag), fake.format(k=k, tag=tag), 0, now
        )
        for k in range(n)
    ]


def _prints_of(index: Any, e: Any) -> list[int]:
    """The prints of ``e``'s word rows, as its store writes them: its value's and its fake's words and their edges, each
    in its role's slot."""
    from latet.engine.stored import edges_of, word_of
    from latet.server.prints import SLOT

    rows: list[tuple[str, str]] = []
    for role in ("original", "surrogate"):
        word = word_of(e, role)
        if word is not None:
            rows.append((role, word))
        head, tail = edges_of(e, role)
        rows += [(f"{role}-{side}", edge) for side, edge in (("head", head), ("tail", tail)) if edge is not None]
    made = index.prints_of([word for _, word in rows])
    return [int(made[k][SLOT[role]]) for k, (role, _) in enumerate(rows)]


def ready(args: argparse.Namespace) -> int:
    folder: Path = args.folder
    if not (folder / "state.json").exists():
        raise SystemExit(
            f"nothing was built in {folder}: `uv run python scripts/bench_apps_server.py --folder {folder} build "
            "--values N` first, on the test services"
        )
    guard(args.seconds, args.megabytes)
    if args.production_rebuild:
        return production_rebuild(folder)
    import tracemalloc

    import numpy as np

    from latet.server.index import PrintIndex
    from latet.server.prints import print_key
    from latet.server.vault import PgVaultStore

    with _bench_store(folder) as (state, root, store):
        values = store.count_entities()
        print(f"vault {folder.name}: {values:,} values, schema {state.schema}; private {memory_mb():.0f} MB")
        if not store.prints_complete():
            load, started = Load(), _now()
            filled = store.fill_prints()
            print(
                f"prints made first (a build before 0.11.0): {filled:,} rows in {_now() - started:.1f} s "
                f"(load {load.percent():.0f} %)",
                flush=True,
            )
        with store.db.connection() as conn:
            found = conn.execute(
                "SELECT count(*), count(DISTINCT print) FROM vault_words WHERE identity_id = %s AND print IS NOT NULL",
                (state.key,),
            ).fetchone()
        rows, distinct = (int(found[0]), int(found[1])) if found else (0, 0)
        index = PrintIndex(store.db, state.key, print_key(root))
        builds: list[float] = []
        loads: list[float] = []
        held: list[float] = []
        for _ in range(args.rounds):
            index = PrintIndex(store.db, state.key, print_key(root))  # the last one's arrays let go
            gc.collect()
            before, load, started = memory_mb(), Load(), _now()
            if not index.build():
                raise SystemExit("a word row has no print: the index is not ready")
            builds.append(_now() - started)
            loads.append(load.percent())
            held.append(memory_mb() - before)
        tracemalloc.start()
        PrintIndex(store.db, state.key, print_key(root)).build()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        gc.collect()
        print(
            f"read: {rows:,} rows with a print ({distinct:,} distinct), {rows * 8 / 1e6:.1f} MB of int8; "
            f"{rows / max(1, values):.2f} rows a value"
        )
        print(
            f"build: {spread(builds, '{:.2f}')} s (load {spread(loads)} %); per million values "
            f"{statistics.median(builds) * 1e6 / max(1, values):.2f} s"
        )
        print(
            f"nbytes: {index.nbytes / 1e6:.2f} MB ({len(index):,} prints), {index.nbytes / max(1, values):.2f} MB per"
            f" million values; private bytes +{spread(held)} MB held after a build, {peak / 1e6:.0f} MB at most "
            "while one builds (tracemalloc)",
            flush=True,
        )
        # What a call pays to know the index is current: the counter read, nothing new.
        reads: list[float] = []
        load = Load()
        for _ in range(args.reads):
            started = _now()
            if index.catch_up() is not None:
                raise SystemExit("the vault changed while it was measured")
            reads.append(_now() - started)
        print(f"counter read (catch_up, nothing new): {_quantiles(reads)} (load {load.percent():.0f} %)", flush=True)
        # Values kept by another store of the key (another instance's), then the catch-up that takes them in.
        other = PgVaultStore(store.db, state.key, root, limit=store.limit)
        kept: list[str] = []
        try:
            for n in (1, 100, 1000):
                spent: list[float] = []
                taken: list[int] = []
                load = Load()
                for _ in range(args.rounds):
                    entities = _catch_up_values(n)
                    together = other.together() if n > 1 else None
                    try:
                        for e in entities:
                            if other.save_entity(e) is not None:
                                raise SystemExit("a value of the catch-up was in the vault already")
                            kept.append(e.id)
                    except BaseException:
                        if together is not None:
                            together.rollback()
                        raise
                    if together is not None:
                        together.commit()
                    started = _now()
                    caught = index.catch_up()
                    spent.append(_now() - started)
                    expected = {printed for e in entities for printed in _prints_of(index, e)}
                    if caught is None or caught != ({e.id for e in entities}, expected):
                        raise SystemExit(f"the catch-up of {n} values did not take in their rows")
                    taken.append(len(caught[1]))
                    if not index.present(np.array(sorted(expected), dtype=np.int64)).all():
                        raise SystemExit(f"the catch-up of {n} values left a print out")
                print(
                    f"catch-up of {n:,} value{'s' if n > 1 else ''} kept by another store: {_quantiles(spent)}; "
                    f"{spread(taken)} prints taken in (load {load.percent():.0f} %)",
                    flush=True,
                )
        finally:
            started = _now()
            other.forget_entities(kept)
            print(f"{len(kept):,} values kept for the catch-ups forgotten in {_now() - started:.1f} s", flush=True)
        print(f"side set {index.side:,} prints, nbytes {index.nbytes / 1e6:.2f} MB; private {memory_mb():.0f} MB")
    return 0


# --- the command -----------------------------------------------------------------------------------------------------


def production_rebuild(folder: Path) -> int:
    """Three real store rebuilds with an old published index and bounded warmed candidate cache retained."""
    import bench_apps

    from latet.engine.stored import StoredVault

    qualified = True
    with _bench_store(folder, index=True) as (state, root, store):
        warm = bench_apps.lines(500, state.seed, 0.5)
        for trial in range(3):
            vault = StoredVault(root, store, working=5000, held_at_most=1000)
            read_call(vault, warm, True)
            old = store.index
            if old is None or not old.ready:
                raise RuntimeError("production rebuild lacks an old ready index")
            before = {
                "published_array_bytes": old.nbytes,
                "candidate_cache_bytes": store._cache.nbytes,
                "store_index_bytes": store.index_bytes,
            }
            with MemoryObservation(lambda: store) as memory:
                load, started = Load(), _now()
                built = store.build_index()
                seconds, cpu = _now() - started, load.sample()
            index = store.index
            valid = (
                built
                and index is not None
                and index.ready
                and index is not old
                and store._building_index is None
                and cpu["status"] == "ok"
                and memory.report()["qualified"]
            )
            qualified &= bool(valid)
            print(
                json.dumps(
                    {
                        "measurement": "production-store-rebuild",
                        "trial": trial,
                        "warm_texts": 500,
                        "seconds": seconds,
                        "cpu": cpu,
                        "before": before,
                        "memory": memory.report(),
                        "published_array_bytes": index.nbytes if index else None,
                        "watermark": index.watermark if index else None,
                        "qualified": bool(valid),
                    },
                    allow_nan=False,
                ),
                flush=True,
            )
            del old, vault
    return 0 if qualified else 1


def parser() -> argparse.ArgumentParser:
    from latet.engine.stored import HELD_SECRETS, WHOLE_VALUES, WORKING_VALUES

    found = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    commands = found.add_subparsers(dest="command", required=True)
    detected = commands.add_parser("detectors", help="the generic detectors, on the bench corpora")
    detected.add_argument("--rounds", type=int, default=5)
    knowing = commands.add_parser("known", help="known values and echo: a plain vault against a key's stored one")
    knowing.add_argument("--folder", type=Path, required=True, help="a build of bench_apps_server.py")
    knowing.add_argument("--rounds", type=int, default=3)
    knowing.add_argument("--calls", type=int, default=4, help=f"calls of {CALL} log lines")
    knowing.add_argument("--fuzz", type=int, default=5000, help="fuzz texts read for the characters found (0: none)")
    knowing.add_argument("--fuzz-schema")
    knowing.add_argument("--fuzz-manifest", type=Path)
    knowing.add_argument("--working", type=int, default=WORKING_VALUES, help="the stored vault's working set")
    knowing.add_argument("--held", type=int, default=HELD_SECRETS, help="the secrets it holds glued")
    personal = commands.add_parser("person", help="a person's space: its stored vault against 0.10.0's plain one")
    personal.add_argument("--folder", type=Path, required=True, help="a build of bench_apps_server.py build --person")
    personal.add_argument("--rounds", type=int, default=5, help="rounds, each of lines of its own")
    personal.add_argument("--lines", type=int, default=CALL, help="lines a round")
    personal.add_argument("--working", type=int, default=WORKING_VALUES, help="the stored vault's working set")
    personal.add_argument(
        "--whole", type=int, default=WHOLE_VALUES, help="held whole up to this many values (0: on its store)"
    )
    personal.add_argument(
        "--past-bound", action="store_true", help="every call of B past its lookup's bounds (its whole vault read)"
    )
    personal.add_argument(
        "--commit-between", action="store_true", help="another instance keeps a value before each B turn"
    )
    getting_ready = commands.add_parser("ready", help="the index of a vault's prints: its build, weight and catch-ups")
    getting_ready.add_argument("--folder", type=Path, required=True, help="a build of bench_apps_server.py")
    getting_ready.add_argument("--rounds", type=int, default=5, help="builds, and catch-ups of each size")
    getting_ready.add_argument("--reads", type=int, default=300, help="counter reads timed")
    getting_ready.add_argument("--production-rebuild", action="store_true")
    for command in (detected, knowing, personal, getting_ready):
        command.add_argument("--seconds", type=float, default=3 * 3600, help="stopped past this long")
        command.add_argument("--megabytes", type=float, default=12_000, help="stopped past this much memory")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "detectors":
        guard(args.seconds, args.megabytes)
        return detectors(args)
    if args.command == "ready":
        return ready(args)
    if args.command == "person":
        return person(args)
    return known(args)


if __name__ == "__main__":
    sys.exit(main())
