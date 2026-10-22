"""How fast Latet's term matcher masks with many terms, and how much memory it takes: the measure behind its design
(notes/enterprise-e6-terms-plan.md, Task 1).

uv run python scripts/terms_bench.py                  # 10,000 and 50,000 terms
uv run python scripts/terms_bench.py --terms 1000     # other counts
uv run python scripts/terms_bench.py --check          # exit 1 when 50,000 terms are over the threshold
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from latet.config import Config
from latet.engine.build import build_masker
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import Term, TermsDetector
from latet.engine.types import SlotKind
from latet.engine.vault import Vault

# The threshold, for 50,000 terms: the matcher finding them in 100 KB of text that holds some, building it, and the
# memory it adds. The whole masking is measured beside it: what the terms add to it is the matcher's time.
BUDGET_MS = 50.0
BUILD_S = 2.0
MEMORY_MB = 100.0
SIZE = 100_000

_SYLLABLES = "ka ro mi ten bel dor fa li nu sar vek to lan ber mo zi ga pe rin sol da ve qua mar ost elm".split()  # noqa: SIM905
_SUFFIXES = ("Logistics", "Holding", "Partners", "Industries", "Group", "Labs", "Systems", "Foods", "Energy", "Bank")
_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PROSE = (
    "The deployment script reads the configuration from the environment and retries the upload three times. "
    "def handler(event, context):\n    return {'status': 200, 'body': json.dumps(event)}\n"
    '{"id": 4821, "name": "invoice-batch", "items": [{"sku": "A-77", "qty": 3}], "total": 129.5}\n'
    "Error: connection refused at db.internal:5432 after 30s; see logs/2026-10-12.log for details.\n"
)


def _word(rng: random.Random) -> str:
    return "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 3))).capitalize()


def make_terms(n: int, seed: int = 1) -> list[Term]:
    """``n`` distinct invented terms: people's names, companies, addresses at acme.com."""
    rng = random.Random(seed)
    terms: list[Term] = []
    seen: set[str] = set()
    while len(terms) < n:
        roll = rng.random()
        if roll < 0.5:
            term = Term(f"{_word(rng)} {_word(rng)}", "PERSON")
        elif roll < 0.8:
            term = Term(f"{_word(rng)} {rng.choice(_SUFFIXES)}", "CLIENT")
        else:
            term = Term(f"{_word(rng).lower()}.{_word(rng).lower()}@acme.com", "EMAIL")
        if term.value.casefold() not in seen:
            seen.add(term.value.casefold())
            terms.append(term)
    return terms


def make_text(size: int, hits: Sequence[str], seed: int) -> str:
    """``size`` characters of code, logs, JSON and random identifiers, with one of ``hits`` every 50 pieces."""
    rng = random.Random(seed)
    pieces: list[str] = []
    total = 0
    while total < size:
        if hits and len(pieces) % 50 == 49:
            piece = f" Meeting with {rng.choice(hits)} today. "
        elif rng.random() < 0.5:
            piece = " " + "".join(rng.choice(_LETTERS) for _ in range(rng.randint(3, 12)))
        else:
            start = rng.randint(0, len(_PROSE) // 2)
            piece = _PROSE[start : rng.randint(start + 1, len(_PROSE))]
        pieces.append(piece)
        total += len(piece)
    return "".join(pieces)[:size]


def memory_mb() -> float:
    """What this process holds now: its private bytes on Windows, its resident pages on Linux, its peak elsewhere."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        names = ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage")
        names += ("QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                *((name, ctypes.c_size_t) for name in (*names, "PrivateUsage")),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return counters.PrivateUsage / 1e6
    try:
        with open("/proc/self/statm") as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except OSError:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6  # bytes on macOS


@dataclass(frozen=True)
class Measure:
    terms: int
    build_s: float | None  # None: the matcher did not build (error says why)
    error: str | None
    memory_mb: float  # what building and matching added
    match_ms: float  # the matcher alone, per 100 KB that holds some of the terms
    clean_ms: float  # the matcher alone, per 100 KB that holds none
    mask_ms: float  # the whole masker (balanced, no name model), per 100 KB that holds some

    def within(self) -> bool:
        return (
            self.build_s is not None
            and self.build_s <= BUILD_S
            and self.match_ms <= BUDGET_MS
            and self.memory_mb <= MEMORY_MB
        )


def _per_text(run: Callable[[str], object], texts: Sequence[str]) -> float:
    started = time.perf_counter()
    for text in texts:
        run(text)
    return (time.perf_counter() - started) * 1000 / len(texts)


def measure(n: int, texts: int = 5) -> Measure:
    terms = make_terms(n)
    hits = [t.value for t in terms[:: max(1, n // 50)]]
    some = [make_text(SIZE, hits, seed) for seed in range(texts)]
    none = [make_text(SIZE, [], seed) for seed in range(texts, 2 * texts)]
    before = memory_mb()
    started = time.perf_counter()
    try:
        detector = TermsDetector(terms)
    except Exception as exc:  # one RE2 alternation: "pattern too large - compile failed"
        return Measure(n, None, str(exc), 0.0, float("inf"), float("inf"), float("inf"))
    build_s = time.perf_counter() - started
    ctx = DetectContext()
    match_ms = _per_text(lambda t: list(detector.detect(t, ctx)), some)
    clean_ms = _per_text(lambda t: list(detector.detect(t, ctx)), none)
    added = memory_mb() - before
    masker, _ = build_masker(Config(ner="off"), Vault(b"\x07" * 32))
    masker.detectors.append(detector)
    mask_ms = _per_text(lambda t: masker.mask(t, SlotKind.TOOL_RESULT), some)
    return Measure(n, build_s, None, added, match_ms, clean_ms, mask_ms)


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the term matcher with many terms.")
    parser.add_argument("--terms", type=int, nargs="*", default=[10_000, 50_000])
    parser.add_argument("--check", action="store_true", help="exit 1 when 50,000 terms are over the threshold")
    args = parser.parse_args()
    print(f"{'terms':>7} {'build':>8} {'memory':>8} {'100 KB, none':>13} {'100 KB, some':>13} {'masking':>9}")
    found: dict[int, Measure] = {}
    for n in args.terms:
        m = found[n] = measure(n)
        if m.build_s is None:
            print(f"{n:>7} does not build: {m.error}")
            continue
        print(
            f"{n:>7} {m.build_s:>7.2f}s {m.memory_mb:>6.0f}MB {m.clean_ms:>11.1f}ms {m.match_ms:>11.1f}ms"
            f" {m.mask_ms:>7.1f}ms"
        )
    print(f"threshold at 50,000 terms: matched in {BUDGET_MS:.0f} ms per 100 KB, built in {BUILD_S:.0f} s,", end=" ")
    print(f"{MEMORY_MB:.0f} MB more memory")
    if args.check:
        return 0 if (found.get(50_000) or measure(50_000)).within() else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
