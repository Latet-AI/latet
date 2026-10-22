"""How well does a local model catch what Latet's deterministic engine missed?

Each case of ``tests/data/llm_review/cases.yaml`` (every value in it is invented) is masked by
Latet first. A local model then reviews the result through an OpenAI-compatible chat API
(llama.cpp, Ollama, LM Studio) and lists what it would still mask, and whether it would stop the
text altogether.

Variants:
  A  the original text with Latet's replacements shown as placeholders, no reasoning
  B  the same, with the model's reasoning on
  C  the text as it would leave (realistic surrogates instead of placeholders), no reasoning
  D  long texts reviewed in pieces of about 2000 tokens, no reasoning
  T  as A, with a user's own terms as context (clients and code names that are not in the cases)

  uv run python scripts/llm_review_bench.py run --label qwen3.5-4b --variants A,B,C,D
  uv run python scripts/llm_review_bench.py report

With --guidance FILE every variant is told your guidance after the instructions, as Latet tells
it (`llm_review.guidance`): run it with and without, under two labels, to see what it changes.

The server comes from LATET_LLM_URL (default http://127.0.0.1:8080/v1), the model from
LATET_LLM_MODEL and the API key, when the server needs one, from LATET_LLM_API_KEY. Only
loopback addresses are accepted: the model is shown the original text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from latet.config import Config
from latet.engine.build import build
from latet.engine.corpus import benign_sample
from latet.engine.masker import Masker
from latet.engine.reviewer.prompt import CLOSE, OPEN, SCHEMA, SYSTEM, system_for  # the measured instructions
from latet.engine.types import SlotKind
from latet.engine.vault import Vault

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "data" / "llm_review" / "cases.yaml"
RESULTS = ROOT / "benchmarks" / "llm-review" / "results"
CATEGORIES = SCHEMA["properties"]["findings"]["items"]["properties"]["category"]["enum"]

CHUNK_CHARS = 7000  # about 2000 tokens of code
SUBSET = 40  # cases for the B and C variants
# Variant T: a user's own terms, of the kinds the cases hold (clients, partners, code names), none of
# them in the cases: what they teach the reviewer, not what they mask.
TERMS = [
    ("Fonderia Brambilla S.p.A.", "CLIENT"),
    ("Banca Ligure", "CLIENT"),
    ("Studio Associato Ferraris", "PARTNER"),
    ("Progetto Orione", "PROJECT"),
    ("Northwind Logistics", "CLIENT"),
]
# Fake tokens of real formats, put together here so that no secret scanner flags the case file.
FAKE = {"GH_PAT": "ghp_" + "7fK2mQ9xLw4RtY8vB3nZ6pJ1sD5hG0cE2aUi", "AWS_KEY": "AKIA" + "4TQ2Z7XWNVB3K5LM"}


def with_terms(system: str, terms: list[tuple[str, str]]) -> str:
    """The instructions with a user's own terms as context (variant T). Measured on 2026-10-08 with
    Bonsai 27B: no change (see benchmarks/llm-review/README.md), so Latet does not send them."""
    listed = "; ".join(f"{value} ({kind})" for value, kind in terms)
    return (
        f"{system}\n\nThe user's own confidential terms, already masked where they appear: {listed}. Values of the "
        "same kind (other clients, partners, project code names) are confidential too: report them."
    )


# --- cases -----------------------------------------------------------------------------------------


@dataclass
class Case:
    id: str
    kind: str
    text: str
    expect: list[tuple[str, str]]
    ok: list[str]
    block: bool | None
    tags: list[str]


def _long_cases(spec: dict[str, Any]) -> list[Case]:
    lines = benign_sample().splitlines()
    out, pos = [], 0
    for size in spec["sizes"]:
        for with_inserts in (True, False):
            chunk: list[str] = []
            n = 0
            while n < size * 3.6 and pos < len(lines):
                chunk.append(lines[pos])
                n += len(lines[pos]) + 1
                pos += 1
            expect: list[tuple[str, str]] = []
            if with_inserts:
                inserts = spec["inserts"]
                at = [len(chunk) * (i + 1) // (len(inserts) + 1) for i in range(len(inserts))]
                for i in reversed(range(len(inserts))):
                    chunk.insert(at[i], inserts[i]["text"])
                for ins in inserts:
                    expect += [(str(t), c) for t, c in ins["expect"]]
            name = f"long-{size // 1000}k" + ("" if with_inserts else "-benign")
            tags = ["long", "code"] + ([] if with_inserts else ["benign"])
            out.append(Case(name, "tool_result", "\n".join(chunk), expect, [], False, tags))
    return out


def _fake(value: object) -> str:
    text = str(value)
    for name, token in FAKE.items():
        text = text.replace("{" + name + "}", token)
    return text


def load_cases(path: Path = CASES) -> list[Case]:
    data = yaml.safe_load(path.read_text("utf-8"))
    cases = [
        Case(
            c["id"],
            c["kind"],
            _fake(c["text"]),
            [(_fake(t), cat) for t, cat in c.get("expect") or []],
            [str(x) for x in c.get("ok") or []],
            c.get("block"),
            list(c.get("tags") or []),
        )
        for c in data["cases"]
    ]
    for tpl in data.get("templates") or []:
        for i, raw in enumerate(tpl["fills"], 1):
            fill = {k: str(v) for k, v in raw.items()}
            text = re.sub(r"\{(\w+)\}", lambda m, f=fill: f.get(m.group(1), m.group(0)), tpl["text"])
            expect = [(fill[k], cat) for k, cat in tpl["slots"].items()]
            ok = [fill[k] for k in tpl.get("ok_slots") or []]
            tags = list(tpl.get("tags") or [])
            cases.append(Case(f"{tpl['id']}-{i}", tpl["kind"], text, expect, ok, tpl.get("block", False), tags))
    if data.get("long"):
        cases += _long_cases(data["long"])
    for case in cases:
        for value, cat in case.expect:
            if value not in case.text:
                raise SystemExit(f"{case.id}: expected value {value!r} is not in the text")
            if cat not in CATEGORIES:
                raise SystemExit(f"{case.id}: unknown category {cat!r}")
    return cases


def subset(cases: list[Case], n: int) -> list[Case]:
    short = sorted((c for c in cases if "long" not in c.tags), key=lambda c: c.id)
    if len(short) <= n:
        return short
    step = len(short) / n
    return [short[int(i * step)] for i in range(n)]


# --- what Latet does first ----------------------------------------------------------------------


@dataclass
class Prepared:
    view: str  # the original, with each replacement as a placeholder
    holes: list[tuple[int, int]]  # where the placeholders are in the view
    masked: str  # the text as it would leave
    surrogates: list[tuple[int, int]]  # where the surrogates are in the masked text
    covered: list[tuple[int, int]]  # what Latet replaced, in the original
    types: list[str]


def make_masker() -> Masker:
    return build(Config(ner="off"), Vault(secrets.token_bytes(32))).masker


def prepare(masker: Masker, case: Case) -> Prepared:
    result = masker.mask(case.text, SlotKind(case.kind))
    reps = sorted(result.replacements, key=lambda r: r.start)
    parts: list[str] = []
    holes: list[tuple[int, int]] = []
    names: dict[str, str] = {}
    counts: Counter[str] = Counter()
    pos = size = 0
    for r in reps:
        clear = case.text[pos : r.start]
        parts.append(clear)
        size += len(clear)
        key = r.entity_id or r.replacement
        if key not in names:
            counts[r.etype] += 1
            names[key] = f"{OPEN}{r.etype}#{counts[r.etype]}{CLOSE}"
        parts.append(names[key])
        holes.append((size, size + len(names[key])))
        size += len(names[key])
        pos = r.end
    parts.append(case.text[pos:])
    surrogates, delta = [], 0
    for r in reps:
        start = r.start + delta
        surrogates.append((start, start + len(r.replacement)))
        delta += len(r.replacement) - (r.end - r.start)
    covered = [(r.start, r.end) for r in reps]
    return Prepared("".join(parts), holes, result.text, surrogates, covered, [r.etype for r in reps])


def _overlaps(span: tuple[int, int], ranges: list[tuple[int, int]]) -> bool:
    return any(a < span[1] and span[0] < b for a, b in ranges)


def masked_already(value: str, case: Case, prep: Prepared) -> bool:
    spans = [(m.start(), m.end()) for m in re.finditer(re.escape(value), case.text)]
    return bool(spans) and all(_overlaps(s, prep.covered) for s in spans)


# --- the model ------------------------------------------------------------------------------------


def _loopback(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"refusing {url}: the reviewer is shown original text, so only loopback is allowed")


def _parse(content: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(content)
    except ValueError:
        found = re.search(r"\{.*\}", content, re.S)
        if found is None:
            return None
        try:
            obj = json.loads(found.group(0))
        except ValueError:
            return None
    if not isinstance(obj, dict) or not isinstance(obj.get("findings"), list):
        return None
    return obj


def ask(client: httpx.Client, url: str, model: str, text: str, thinking: bool, system: str = SYSTEM) -> dict[str, Any]:
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 6144 if thinking else 1536,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "review", "schema": SCHEMA}},
        "chat_template_kwargs": {"enable_thinking": thinking},
        "cache_prompt": True,
    }
    started = time.perf_counter()
    response = client.post(f"{url}/chat/completions", json=body)
    wall = time.perf_counter() - started
    response.raise_for_status()
    data = response.json()
    choice = data["choices"][0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    return {
        "wall": wall,
        "content": content[:4000],
        "reasoning_chars": len(message.get("reasoning_content") or ""),
        "finish": choice.get("finish_reason"),
        "parsed": _parse(content),
        "timings": data.get("timings") or {},
        "usage": data.get("usage") or {},
    }


def chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    out, current, size = [], [], 0
    for line in text.split("\n"):
        if current and size + len(line) + 1 > limit:
            out.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    out.append("\n".join(current))
    return out


# --- scoring --------------------------------------------------------------------------------------


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def same(a: str, b: str) -> bool:
    """One value is the other, or a whole-word part of it."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    short, long = sorted((a, b), key=len)
    if len(short) < 3:
        return a == b
    return re.search(rf"(?<!\w){re.escape(short)}(?!\w)", long) is not None


def check(findings: list[Any], reviewed: str, holes: list[tuple[int, int]], hole_status: str) -> list[dict[str, Any]]:
    out = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        text = str(f.get("text") or "").strip()
        item = {"text": text, "category": str(f.get("category") or "")}
        if not 2 <= len(text) <= 200:
            out.append(item | {"status": "bad-length"})
            continue
        spans = [(m.start(), m.end()) for m in re.finditer(re.escape(text), reviewed)]
        if not spans:
            out.append(item | {"status": "not-in-text"})
        elif all(_overlaps(s, holes) for s in spans):
            out.append(item | {"status": hole_status})
        else:
            out.append(item | {"status": "valid"})
    return out


def score(case: Case, prep: Prepared, checked: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [f for f in checked if f["status"] == "valid"]
    open_items = [(v, c) for v, c in case.expect if not masked_already(v, case, prep)]
    hit = [(v, c) for v, c in open_items if any(same(f["text"], v) for f in valid)]
    right_cat = [(v, c) for v, c in hit if any(same(f["text"], v) and f["category"] == c for f in valid)]
    known = [v for v, _ in case.expect] + case.ok
    fps = [f["text"] for f in valid if not any(same(f["text"], k) for k in known)]
    return {
        "open": len(open_items),
        "hit": len(hit),
        "right_category": len(right_cat),
        "missed": [[v, c] for v, c in open_items if (v, c) not in hit],
        "valid": len(valid),
        "fp": fps,
        "invalid": dict(Counter(f["status"] for f in checked if f["status"] != "valid")),
    }


# --- run ------------------------------------------------------------------------------------------


def _done(path: Path) -> set[tuple[str, int, str]]:
    if not path.exists():
        return set()
    seen = set()
    for line in path.read_text("utf-8").splitlines():
        row = json.loads(line)
        seen.add((row["variant"], row["run"], row["case"]))
    return seen


def run(args: argparse.Namespace) -> None:
    url = (os.environ.get("LATET_LLM_URL") or "http://127.0.0.1:8080/v1").rstrip("/")
    _loopback(url)
    model = os.environ.get("LATET_LLM_MODEL") or args.label
    headers = {}
    key = os.environ.get("LATET_LLM_API_KEY")
    if key is None and args.saved_key:  # the key saved for Latet's reviewer, never printed
        from latet.config import load_config
        from latet.engine.reviewer.reviewer import api_key_of

        key = api_key_of(load_config().llm_review)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    cases = load_cases()
    if args.skip_tags:
        skip = set(args.skip_tags.split(","))
        cases = [c for c in cases if not skip & set(c.tags)]
    if args.tags or args.cases:
        tags = set((args.tags or "").split(",")) - {""}
        ids = set((args.cases or "").split(",")) - {""}
        cases = [c for c in cases if tags & set(c.tags) or c.id in ids]
    instructions = SYSTEM
    if args.guidance:
        from latet.config import clean_guidance

        instructions = system_for(clean_guidance(args.guidance.read_text("utf-8")))
    masker = make_masker()
    prepared = {c.id: prepare(masker, c) for c in cases}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.label}.jsonl"
    done = _done(out)
    with httpx.Client(timeout=900, headers=headers) as client:
        models = client.get(f"{url}/models").json()
        (RESULTS / f"{args.label}.meta.json").write_text(json.dumps(models, indent=1), "utf-8")
        for _ in range(2):  # warm up: load the weights, compile the grammar, cache the instructions
            ask(client, url, model, "Ciao, come stai?", thinking=False, system=instructions)
        with out.open("a", encoding="utf-8") as sink:
            for variant in args.variants.split(","):
                if variant == "D":
                    chosen = [c for c in cases if "long" in c.tags]
                elif variant in ("B", "C") and not (args.tags or args.cases):
                    chosen = subset(cases, args.subset)
                else:
                    chosen = cases
                for run_no in range(args.repeat):
                    for case in chosen:
                        if (variant, run_no, case.id) in done:
                            continue
                        prep = prepared[case.id]
                        if variant == "C":
                            reviewed, holes, hole_status = prep.masked, prep.surrogates, "surrogate"
                        else:
                            reviewed, holes, hole_status = prep.view, prep.holes, "placeholder"
                        pieces = chunks(reviewed) if variant == "D" else [reviewed]
                        system = with_terms(instructions, TERMS) if variant == "T" else instructions
                        answers = [ask(client, url, model, p, thinking=variant == "B", system=system) for p in pieces]
                        findings: list[Any] = []
                        blocked = False
                        for a in answers:
                            if a["parsed"] is not None:
                                findings += a["parsed"]["findings"]
                                blocked = blocked or bool(a["parsed"].get("block"))
                        checked = check(findings, reviewed, holes, hole_status)
                        row = {
                            "label": args.label,
                            "variant": variant,
                            "run": run_no,
                            "case": case.id,
                            "tags": case.tags,
                            "latet_types": prep.types,
                            "pieces": len(pieces),
                            "wall": sum(a["wall"] for a in answers),
                            "prompt_tokens": sum(int(a["usage"].get("prompt_tokens") or 0) for a in answers),
                            "completion_tokens": sum(int(a["usage"].get("completion_tokens") or 0) for a in answers),
                            "prompt_n": sum(int(a["timings"].get("prompt_n") or 0) for a in answers),
                            "prompt_ms": sum(float(a["timings"].get("prompt_ms") or 0) for a in answers),
                            "predicted_n": sum(int(a["timings"].get("predicted_n") or 0) for a in answers),
                            "predicted_ms": sum(float(a["timings"].get("predicted_ms") or 0) for a in answers),
                            "reasoning_chars": sum(a["reasoning_chars"] for a in answers),
                            "server_ms": sum(
                                float(a["timings"].get("prompt_ms") or 0) + float(a["timings"].get("predicted_ms") or 0)
                                for a in answers
                            ),
                            "valid_json": all(a["parsed"] is not None for a in answers),
                            "finish": [a["finish"] for a in answers],
                            "block": blocked,
                            "block_expected": case.block,
                            "findings": checked,
                            "score": score(case, prep, checked),
                        }
                        if not row["valid_json"]:
                            row["content"] = [a["content"] for a in answers]
                        sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                        sink.flush()
                        s = row["score"]
                        print(
                            f"{args.label} {variant}{run_no} {case.id:<24} {row['wall']:6.1f}s "
                            f"hit {s['hit']}/{s['open']} fp {len(s['fp'])} json {'ok' if row['valid_json'] else 'BAD'}",
                            flush=True,
                        )


# --- report ---------------------------------------------------------------------------------------

BUCKETS = [(0, 1000, "<1k"), (1000, 3000, "1-3k"), (3000, 10000, "3-10k"), (10000, 10**9, ">10k")]


def _pct(a: float, b: float) -> str:
    return f"{100 * a / b:.0f}%" if b else "-"


def _q(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def report(args: argparse.Namespace) -> None:
    rows = []
    for path in sorted(RESULTS.glob("*.jsonl")):
        rows += [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["label"], row["variant"])].append(row)
    out = ["## Quality", ""]
    out.append(
        "| Model | Var | Cases | Recall | Precision | Right category | FP per 1k benign tokens | "
        "Injection recall | Block right | JSON ok | Placeholder/surrogate hits |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for (label, variant), group in sorted(groups.items()):
        first = [r for r in group if r["run"] == 0]
        opened = sum(r["score"]["open"] for r in first)
        hit = sum(r["score"]["hit"] for r in first)
        valid = sum(r["score"]["valid"] for r in first)
        fps = sum(len(r["score"]["fp"]) for r in first)
        cat = sum(r["score"]["right_category"] for r in first)
        benign = [r for r in first if "benign" in r["tags"]]
        benign_fp = sum(len(r["score"]["fp"]) for r in benign)
        benign_tokens = sum(r["prompt_tokens"] for r in benign)
        inj = [r for r in first if "injection" in r["tags"]]
        scored = [r for r in first if r["block_expected"] is not None]
        right = sum(1 for r in scored if r["block"] == r["block_expected"])
        holes = sum(
            r["score"]["invalid"].get("placeholder", 0) + r["score"]["invalid"].get("surrogate", 0) for r in first
        )
        out.append(
            f"| {label} | {variant} | {len(first)} | {_pct(hit, opened)} ({hit}/{opened}) | "
            f"{_pct(valid - fps, valid)} | {_pct(cat, hit)} | "
            f"{1000 * benign_fp / benign_tokens if benign_tokens else 0:.1f} | "
            f"{_pct(sum(r['score']['hit'] for r in inj), sum(r['score']['open'] for r in inj))} | "
            f"{right}/{len(scored)} | {_pct(sum(r['valid_json'] for r in first), len(first))} | {holes} |"
        )
    out += ["", "## Speed (wall time per text, seconds)", ""]
    heads = " | ".join(f"{b[2]} p50 / p95" for b in BUCKETS)
    out.append(f"| Model | Var | {heads} | Prefill tok/s | Output tok/s |")
    out.append("|---|---|" + "---|" * len(BUCKETS) + "---|---|")
    for (label, variant), group in sorted(groups.items()):
        cells = []
        for lo, hi, _ in BUCKETS:
            walls = [r["wall"] for r in group if lo <= r["prompt_tokens"] < hi]
            cells.append(f"{_q(walls, 0.5):.1f} / {_q(walls, 0.95):.1f} ({len(walls)})" if walls else "-")
        # Medians of each text's own rate: one slow request (the GPU busy elsewhere) does not skew them.
        pre = [1000 * r["prompt_n"] / r["prompt_ms"] for r in group if r["prompt_n"] >= 200 and r["prompt_ms"]]
        gen = [
            1000 * r["predicted_n"] / r["predicted_ms"] for r in group if r["predicted_n"] >= 10 and r["predicted_ms"]
        ]
        prefill = f"{statistics.median(pre):.0f}" if pre else "-"
        output = f"{statistics.median(gen):.0f}" if gen else "-"
        out.append(f"| {label} | {variant} | " + " | ".join(cells) + f" | {prefill} | {output} |")
    out += ["", "## Stability (same case, repeated runs: share of identical finding sets)", ""]
    for (label, variant), group in sorted(groups.items()):
        by_case: dict[str, list[frozenset[str]]] = defaultdict(list)
        for r in group:
            by_case[r["case"]].append(frozenset(_norm(f["text"]) for f in r["findings"] if f["status"] == "valid"))
        repeated = {k: v for k, v in by_case.items() if len(v) > 1}
        if repeated:
            same_sets = sum(1 for v in repeated.values() if len(set(v)) == 1)
            out.append(f"- {label} {variant}: {same_sets}/{len(repeated)} cases identical across runs")
    out += ["", "## Misses and false positives (first run)", ""]
    for (label, variant), group in sorted(groups.items()):
        first = [r for r in group if r["run"] == 0]
        missed = Counter(c for r in first for _, c in r["score"]["missed"])
        fp = Counter(t for r in first for t in r["score"]["fp"])
        out.append(f"- **{label} {variant}** missed by category: {dict(missed.most_common())}")
        out.append(f"  - false positives ({sum(fp.values())}): {', '.join(f'`{t}`' for t, _ in fp.most_common(25))}")
    text = "\n".join(out) + "\n"
    (RESULTS.parent / "tables.md").write_text(text, "utf-8")
    sys.stdout.write(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label", required=True, help="name of the model in the results (and default model id)")
    r.add_argument("--variants", default="A,B,C,D")
    r.add_argument("--tags", help="only cases with one of these tags (comma separated)")
    r.add_argument("--cases", help="also these case ids (comma separated)")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--subset", type=int, default=SUBSET, help="cases for the B and C variants")
    r.add_argument("--skip-tags", help="leave out the cases with one of these tags (comma separated)")
    r.add_argument("--saved-key", action="store_true", help="use the API key saved for Latet's reviewer")
    r.add_argument("--guidance", type=Path, help="a file with your guidance, told after the instructions")
    sub.add_parser("report")
    sub.add_parser("cases", help="print the cases with what Latet masks first")
    args = parser.parse_args()
    if args.command == "run":
        run(args)
    elif args.command == "report":
        report(args)
    else:
        masker = make_masker()
        for case in load_cases():
            prep = prepare(masker, case)
            opened = [v for v, _ in case.expect if not masked_already(v, case, prep)]
            print(f"{case.id:<26} {len(case.text):>6} chars  latet {len(prep.types):>2}  left {len(opened)}")


if __name__ == "__main__":
    main()
