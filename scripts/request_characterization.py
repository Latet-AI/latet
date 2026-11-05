"""The characterization of the requests (P1-1 of `notes/server-request-coverage-plan.md`): what the table of
``tests/request_coverage.py`` and the generated cases of its four cells (computer and server, HTTP and WebSocket) do.
0.10's golden (``characterization-0.10.json.gz``) was recorded on Phase 1's pinned head, so that P1-2..P1-5 showed
that nothing changed but RC-REC-11's three leak fixes; it is Phase 1's evidence and is never recorded again. Phase 2's
baseline (``characterization-0.11.json.gz``) is what 0.11 does, recorded on its head, so that a later change (the
documentation's, a release's) that alters a request's outcome by accident is caught.

    uv run python scripts/request_characterization.py record --seed 20261101 --count 5000   # on a clean head
    uv run python scripts/request_characterization.py replay                                 # exit 1 on any difference

Each machine's cases run inside pytest (``tests/unit/test_request_characterization.py`` and
``tests/server/test_request_characterization.py``, their ``test_observe_*_for_the_script``), under the suite's
isolation (private homes, no socket but 127.0.0.1, the server's exact schema and Valkey prefix, dropped after it): the
server's need ``LATET_TEST_DATABASE_URL`` and ``LATET_TEST_VALKEY_URL`` (``scripts/server-services.py env``). This
script writes what they observed into the baseline with its seed, its count, the source pin (the commit, which must be
clean), its rule (exact) and the SHA-256 of what was observed; ``replay`` observes again and compares, case by case, by
the golden's own rule (0.10's by ``phase_1_judge``). A golden keeps names and categories only: ``record`` refuses to
write one in which any synthetic original, or any fake the runs' vaults drew, is found."""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "data" / "request-coverage" / "characterization-0.10.json.gz"  # Phase 1's evidence
BASELINE = ROOT / "tests" / "data" / "request-coverage" / "characterization-0.11.json.gz"  # Phase 2's
SCHEMA = "latet-request-characterization/1"
TESTS = {
    "computer": "tests/unit/test_request_characterization.py::test_observe_the_computer_for_the_script",
    "server": "tests/server/test_request_characterization.py::test_observe_the_server_for_the_script",
}
SHOWN = 20  # differences ``replay`` prints at most


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def _observe(seed: int, count: int, folder: Path) -> dict[str, Any]:
    """Both machines observed by pytest into ``folder``: their parts merged."""
    env = dict(os.environ, LATET_RC_OUT=str(folder), LATET_RC_SEED=str(seed), LATET_RC_COUNT=str(count))
    if not env.get("LATET_TEST_DATABASE_URL") or not env.get("LATET_TEST_VALKEY_URL"):
        sys.exit("the server's cells need LATET_TEST_DATABASE_URL and LATET_TEST_VALKEY_URL (server-services.py env)")
    env["LATET_TEST_REQUIRE_SERVICES"] = "1"  # never a golden with the server's cases skipped
    command = [sys.executable, "-m", "pytest", *TESTS.values(), "-q", "-p", "no:cacheprovider"]
    found = subprocess.run(command, cwd=ROOT, env=env)
    if found.returncode != 0:
        sys.exit(f"the observation failed (pytest exit {found.returncode})")
    merged: dict[str, Any] = {"table": {}, "cells": {}, "fakes": []}
    for machine in TESTS:
        part = json.loads((folder / f"{machine}.json").read_bytes())
        merged["table"].update(part["table"])
        merged["cells"].update(part["cells"])
        merged["fakes"] += part["fakes"]
    return merged


def _pooled(cells: dict[str, list[Any]]) -> tuple[list[Any], dict[str, list[Any]]]:
    """The cells' observations, each written once (a pool) and named by its place there: many generated cases are
    observed alike."""
    pool: list[Any] = []
    index: dict[str, int] = {}
    out: dict[str, list[Any]] = {}
    for name, cases in cells.items():
        rows = []
        for case_id, descriptor, observation in cases:
            key = json.dumps({k: v for k, v in observation.items() if k != "case"}, sort_keys=True)
            if key not in index:
                index[key] = len(pool)
                pool.append({k: v for k, v in observation.items() if k != "case"})
            rows.append([case_id, descriptor, index[key]])
        out[name] = rows
    return pool, out


def _unpooled(golden: dict[str, Any]) -> dict[str, list[Any]]:
    pool = golden["pool"]
    return {
        name: [[case_id, descriptor, {"case": case_id, **pool[at]}] for case_id, descriptor, at in rows]
        for name, rows in golden["cells"].items()
    }


def _sha256(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _values_in(doc: Any, fakes: list[str]) -> list[str]:
    sys.path.insert(0, str(ROOT))
    from tests.request_coverage import holds_no_value

    text = json.dumps(doc, ensure_ascii=False)
    found = holds_no_value(doc)
    found += [f"a fake of {len(fake)} characters" for fake in fakes if len(fake) >= 6 and fake in text]
    return found


def record(seed: int, count: int, out: Path, allow_dirty: bool) -> None:
    if out.resolve() == GOLDEN.resolve():
        sys.exit(f"{GOLDEN.name} is Phase 1's evidence (P2-2's provenance names its SHA-256): never recorded again")
    kept = out.resolve().relative_to(ROOT).as_posix() if out.resolve().is_relative_to(ROOT) else ""
    changed = _git("status", "--porcelain", "--untracked-files=no").splitlines()
    status = "\n".join(line for line in changed if line[3:].strip() != kept)  # the golden itself may change
    if status and not allow_dirty:
        sys.exit("the source has changes: a golden is recorded from a clean, pinned head (--allow-dirty to try)")
    pin = _git("rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="latet-rc-") as folder:
        observed = _observe(seed, count, Path(folder))
    pool, cells = _pooled(observed["cells"])
    golden = {
        "schema": SCHEMA,
        "what": "0.11's requests characterized, Phase 2's baseline: the table and four cells of generated cases, names "
        "and categories only (tests/request_coverage.py)",
        "rule": "exact",  # baseline_judge: every observation as recorded, RC-REC-11's fixes in
        "source_pin": pin + (" (dirty)" if status else ""),
        "seed": seed,
        "count": count,
        "recorded": datetime.date.today().isoformat(),
        "table": observed["table"],
        "cells": cells,
        "pool": pool,
    }
    golden["observed_sha256"] = _sha256({"table": golden["table"], "cells": _unpooled(golden)})
    if found := _values_in(golden, observed["fakes"]):
        sys.exit(f"the golden would hold values ({len(found)}): {found[:5]}; nothing written")
    raw = json.dumps(golden, sort_keys=True, separators=(",", ":")).encode()
    data = gzip.compress(raw, compresslevel=9, mtime=0)
    out.write_bytes(data)
    tables = sum(len(rows) for rows in golden["table"].values())
    cases = sum(len(rows) for rows in cells.values())
    print(f"{out}: {tables} table rows, {cases} generated cases ({len(pool)} distinct), seed {seed}, pin {pin[:12]}")
    print(f"observed SHA-256 {golden['observed_sha256']}; file SHA-256 {hashlib.sha256(data).hexdigest()}")


def replay(golden_path: Path) -> int:
    """Observe again and judge each observation by the golden's own rule (``judge_of``): the baseline's exact one
    (``baseline_judge``), or for 0.10's golden the test modules' Phase 1 rule (``phase_1_judge``: exact until
    ``LEAK_FIXES_IN``, then RC-REC-11's rows and the generated WebSocket cases by what the fixes may change)."""
    sys.path.insert(0, str(ROOT))
    from tests.request_coverage import LEAK_FIXES_IN, TABLE, judge_of

    golden = json.loads(gzip.decompress(golden_path.read_bytes()))
    if golden.get("schema") != SCHEMA:
        sys.exit(f"{golden_path} is not a golden of {SCHEMA}")
    judge = judge_of(golden)
    with tempfile.TemporaryDirectory(prefix="latet-rc-") as folder:
        observed = _observe(golden["seed"], golden["count"], Path(folder))
    cases = {case.id: case for case in TABLE}
    differences = []
    for machine, rows in golden["table"].items():
        for row, want in rows.items():
            got = observed["table"].get(machine, {}).get(row)
            if (found := judge(got, want, case=cases.get(row))) is not None:
                differences.append(f"table {machine} {row}: {found}")
    for name, rows in _unpooled(golden).items():
        got_rows = observed["cells"].get(name, [])
        if len(got_rows) != len(rows):
            differences.append(f"{name}: {len(got_rows)} cases != {len(rows)}")
            continue
        for (case_id, descriptor, want), (_, got_descriptor, got) in zip(rows, got_rows, strict=True):
            if got_descriptor != descriptor:
                differences.append(f"{name} {case_id}: another case was drawn ({got_descriptor} != {descriptor})")
            elif (found := judge(got, want, cell=name)) is not None:
                differences.append(f"{name} {case_id} {descriptor}: {found}")
    for line in differences[:SHOWN]:
        print(line)
    total = sum(len(r) for r in golden["table"].values()) + sum(len(r) for r in golden["cells"].values())
    rule = golden.get("rule") or f"Phase 1's, RC-REC-11's fixes {'in' if LEAK_FIXES_IN else 'not in'}"
    print(
        f"{len(differences)} differences in {total} observations ({golden_path.name}, pin "
        f"{golden['source_pin'][:12]}, seed {golden['seed']}, count {golden['count']}, rule {rule}; this head "
        f"{_git('rev-parse', 'HEAD')[:12]})"
    )
    return 1 if differences else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    made = commands.add_parser("record", help="observe this clean head and write the baseline")
    made.add_argument("--seed", type=int, required=True)
    made.add_argument("--count", type=int, default=5000, help="generated cases in each of the four cells")
    made.add_argument("--out", type=Path, default=BASELINE)
    made.add_argument("--allow-dirty", action="store_true", help="record from a head with changes (never committed)")
    again = commands.add_parser("replay", help="observe this head and compare with the baseline (or a golden given)")
    again.add_argument("golden", type=Path, nargs="?", default=BASELINE)
    args = parser.parse_args()
    if args.command == "record":
        record(args.seed, args.count, args.out, args.allow_dirty)
        return 0
    return replay(args.golden)


if __name__ == "__main__":
    sys.exit(main())
