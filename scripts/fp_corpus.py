"""False-positive survey: run the detectors over public source code (which holds no real secrets or
PII) and report what fires, per rule, with timing. Usage:

    uv run python scripts/fp_corpus.py [--profile balanced] [--js DIR] [--limit-mb 20]
"""

from __future__ import annotations

import argparse
import collections
import sys
import sysconfig
import time
from pathlib import Path

from latet.config import Config, Profile
from latet.engine.build import detectors_for
from latet.engine.detectors.base import DetectContext
from latet.engine.types import SlotKind


def files(root: Path, patterns: tuple[str, ...], limit_bytes: int) -> list[Path]:
    out, total = [], 0
    for pattern in patterns:
        for path in sorted(root.rglob(pattern)):
            if "test" in path.parts or not path.is_file():
                continue
            size = path.stat().st_size
            if size > 400_000:
                continue
            out.append(path)
            total += size
            if total >= limit_bytes:
                return out
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="balanced", choices=[p.value for p in Profile])
    ap.add_argument("--js", type=Path, default=Path("docs/node_modules"))
    ap.add_argument("--limit-mb", type=float, default=20)
    ap.add_argument("--show", type=int, default=3, help="sample values shown per rule")
    args = ap.parse_args()

    detectors = detectors_for(Config(profile=Profile(args.profile)), identity=None)
    corpora = {"python-stdlib": files(Path(sysconfig.get_paths()["stdlib"]), ("*.py",), int(args.limit_mb * 1e6))}
    if args.js.exists():
        corpora["javascript"] = files(args.js, ("*.js", "*.mjs", "*.ts"), int(args.limit_mb * 1e6))
    ctx = DetectContext(SlotKind.TOOL_RESULT)
    for name, paths in corpora.items():
        counts: collections.Counter[str] = collections.Counter()
        samples: dict[str, list[str]] = collections.defaultdict(list)
        timing: collections.Counter[str] = collections.Counter()
        size = 0
        for path in paths:
            text = path.read_text("utf-8", errors="replace")
            size += len(text)
            for det in detectors:
                t0 = time.perf_counter()
                spans = list(det.detect(text, ctx))
                timing[det.id] += time.perf_counter() - t0
                for s in spans:
                    counts[s.rule] += 1
                    if len(samples[s.rule]) < args.show:
                        samples[s.rule].append(f"{text[s.start : s.end][:60]!r} ({path.name})")
        mb = size / 1e6
        print(f"\n== {name}: {len(paths)} files, {mb:.1f} MB, {sum(counts.values())} findings")
        print(
            "   ms per MB by detector: "
            + ", ".join(f"{k}={v * 1000 / max(mb, 1e-9):.0f}" for k, v in timing.most_common())
        )
        for rule, n in counts.most_common(25):
            print(f"   {n:5d}  {rule}")
            for sample in samples[rule]:
                print(f"            {sample}")


if __name__ == "__main__":
    main()
