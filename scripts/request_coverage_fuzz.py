"""The request fuzz of P2-8 (`notes/server-request-coverage-plan.md`, "P2-8: Compatibility qualification, drift
fixtures, fuzz and measurements"): seeded requests through the whole app, in sixteen cells (a computer and a server,
HTTP and WebSocket, strict and with a precise request exception in force, the history on and off), each case judged
by the bullet's assertions, with its seed, its corpus and its counts of cases generated, executed and asserted.

    uv run python scripts/request_coverage_fuzz.py corpus --seed 20261104 --count 5000 --out DIR
    uv run python scripts/request_coverage_fuzz.py run --seed 20261104 --count 5000 --out DIR [--cells C ...]
        [--batch 2] [--seconds 3600] [--memory-every 16]
    uv run python scripts/request_coverage_fuzz.py summary DIR

``corpus`` writes the corpus of every cell (``corpus.json``: each case's descriptor, names and kinds only, and the
SHA-256 of its bytes and of the whole cell) without running anything. ``run`` runs the cells, a batch at a time (two
cells a process by default, the plan's "in batches of two if deadlines need"), inside pytest as the characterization
runs (``tests/unit/test_request_coverage_fuzz.py`` for the computer, ``tests/server/test_request_coverage_fuzz.py``
for the server, whose cells need ``LATET_TEST_DATABASE_URL`` and ``LATET_TEST_VALKEY_URL``): the suite's isolation
(private homes, no socket but 127.0.0.1, the server's exact schema and Valkey prefix, dropped after it). Each batch
writes ``<cell>.json`` into ``--out``; ``run`` then writes ``summary.json`` and prints a line a cell. Exit 1 when a
case broke an assertion, a cell did not finish, or a cell's corpus is not the one ``corpus`` writes for its seed.

A case is one template of a request an agent makes (bounded: its payloads at most 64 KiB, JSON at most 16 deep and 256
nodes, at most 16 events), with bounded field, escape and duplicate mutations, and a state sequence around it: the
request exception proposed and left waiting (``review``), approved after that (``approve``), narrowed (``amend``),
removed (``revoke``), past its lifetime (``expire``), the client gone in the middle of the answer (``cancel``), and the
same request sent again (``retry``). Each request step is judged by the assertions of ``ASSERTIONS``:

- ``coverage``: every decision that let something leave rests on a complete, disjoint proof of the very request: each
  occurrence of its envelope given one treatment, each entry of its body removed or covered, each final entry once;
- ``authority``: zero builds, sends or connections but those ``request_transport`` authorized, each carrying its token,
  a refusal sending nothing, two attempts only for AWS's expired token;
- ``bytes``: what reached the provider is byte for byte what the transport froze, whose content is the decision's
  payload; each WebSocket frame upstream is a frame authorized, as it was authorized;
- ``canary``: a canary planted in a component no profile names leaves only inside that very component, and only while
  an approved exception in force names it exactly (over HTTP: a WebSocket takes no exception in 0.11); or, on a
  verify-only route, as D6's checked text: a string canary holding no secret in an unknown body member, under a Pass
  "checked only" (``d6_permits``); an object or an array there, or a secret, never;
- ``masking``: no synthetic original is readable in anything that left (decoded: JSON escapes, percent-escapes,
  gzip), whatever the mutation;
- ``restoration``: an answer that echoes a masked text gives the original back and no fake; the history (when on)
  records each HTTP request once with its status, its refusal as blocked, and a request let through by an exception
  as passed by one;
- ``sinks``: no original, provider credential, credential-role value or Latet credential in a log line or a history
  record, and no record at all with the history off;
- ``ownership``: each hold released, each provider answer closed, no renewer left running, the runtime's retained
  request bytes within RC-R1's caps, and (one case in ``--memory-every``) the traced peak of a case within
  ``MEMORY_PEAK``.

The full run (16 cells x 5,000) is root's to schedule on a quiet machine, one at a time; the unit tests run a small
fixed-seed smoke of each cell."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import tracemalloc
import types
import urllib.parse
import zlib
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "latet-request-coverage-fuzz/1"
COUNT = 5000  # the plan's cases per cell
SMOKE_MOST = 100  # what a smoke runs at most per cell (root's rule for runs outside the scheduled campaign)
MACHINES = ("computer", "server")
TRANSPORTS = ("http", "ws")
POLICIES = ("strict", "exception")
HISTORIES = ("on", "off")
CELLS = tuple(f"{m}-{t}-{p}-history-{h}" for m in MACHINES for t in TRANSPORTS for p in POLICIES for h in HISTORIES)
ASSERTIONS = ("coverage", "authority", "bytes", "canary", "masking", "restoration", "sinks", "ownership")
TESTS = {
    "computer": "tests/unit/test_request_coverage_fuzz.py::test_fuzz_the_computer_for_the_script",
    "server": "tests/server/test_request_coverage_fuzz.py::test_fuzz_the_server_for_the_script",
}


@dataclass(frozen=True, slots=True)
class Bounds:
    """The plan's bounds of a case: a payload (a body or a frame), a JSON document's depth and nodes, the events of
    a case (its state steps, its requests and its frames)."""

    payload: int = 64 * 1024
    depth: int = 16
    nodes: int = 256
    events: int = 16


BOUNDS = Bounds()
MEMORY_PEAK = 64 << 20  # a case's traced peak (tracemalloc): a 64 KiB request needs far less
# RC-R1's per-owner caps on a computer: a mediator's opaque provenance (16 MiB) and a runtime's audit (1 MiB), with the
# live launch keys' bookkeeping (64 a identity, a few hundred bytes each).
RETAINED_MOST = (16 << 20) + (1 << 20) + (1 << 20)


def parts(cell: str) -> tuple[str, str, str, str]:
    """A cell's machine, transport, policy and history."""
    machine, transport, policy, _, history = cell.split("-")
    if cell not in CELLS:
        raise ValueError(f"not a cell of the fuzz: {cell}")
    return machine, transport, policy, history


# --- the request exception of the precise-exception cells ----------------------------------------------------------

DESTINATION = "llm"  # the cells' own destination (tests/request_coverage.py's computer_config, server_config)
HTTP_PROFILE = "openai.chat.1"  # POST /llm/v1/chat/completions
# What the exception names, each exactly: (kind, name, role). Over HTTP on the llm route alone.
COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("header", "x-rcf-team", "checked"),
    ("header", "x-rcf-pass", "credential"),
    ("query", "rcf", "checked"),
    ("body", "/metadata_rcf/team", "checked"),
)
WIDENED = ("header", "x-rcf-new", "checked")  # what ``review`` asks to add
NARROWED = ("query", "rcf", "checked")  # what ``amend`` takes away
WS_COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("header", "x-rcf-team", "checked"),
    ("header", "x-rcf-pass", "credential"),
)
STATE_EVENTS = ("review", "approve", "amend", "revoke", "expire")
REQUEST_EVENTS = ("request", "cancel", "retry")


# --- templates --------------------------------------------------------------------------------------------------------

BEARER = ("authorization", "Bearer sk-rc-test")
JSON_TYPE = ("content-type", "application/json")
ANTHROPIC = (("x-api-key", "sk-ant-rc-test"), ("anthropic-version", "2023-06-01"), JSON_TYPE)
CREDENTIALS = ("sk-rc-test", "sk-ant-rc-test")  # the provider credentials the cases carry: never in a sink


@dataclass(frozen=True, slots=True)
class Template:
    """A request an agent makes, admissible as it is: where it goes, its headers, its body's family (None: none), on
    which machines, whether it is the exception's route, and what ``setup_for`` changes around it."""

    name: str
    transport: str
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    family: str | None
    machines: tuple[str, ...] = MACHINES
    exception_route: bool = False
    setup: tuple[str, ...] = ()
    # Checked, never rewritten (``mediate.verify_only``: Bedrock's and Vertex's calls, Claude Code's): D6 reads an
    # unknown member whose value is a string or a flat list of strings as checked text, which passes "checked only".
    verify_only: bool = False


TEMPLATES: tuple[Template, ...] = (
    Template("openai-chat", "http", "POST", "/openai/v1/chat/completions", (BEARER, JSON_TYPE), "chat"),
    Template("llm-chat", "http", "POST", "/llm/v1/chat/completions", (BEARER, JSON_TYPE), "chat", exception_route=True),
    Template("anthropic-messages", "http", "POST", "/anthropic/v1/messages", ANTHROPIC, "anthropic"),
    Template("openai-responses", "http", "POST", "/openai/v1/responses", (BEARER, JSON_TYPE), "responses"),
    Template("openai-embeddings", "http", "POST", "/openai/v1/embeddings", (BEARER, JSON_TYPE), "embeddings"),
    Template("openai-models", "http", "GET", "/openai/v1/models", (BEARER,), None),
    Template(
        "bedrock-signed",
        "http",
        "POST",
        "/bedrock/us-east-1/profile/default/{profile_key}/model/anthropic.claude-sonnet-4-5-20250929-v1%3A0/invoke",
        (JSON_TYPE,),
        "bedrock",
        machines=("computer",),
        setup=("aws-expired",),
        verify_only=True,
    ),
    Template(
        "codex-ws",
        "ws",
        "GET",
        "/backend-api/codex/responses",
        (BEARER, ("session-id", "0198c5a0-1111-7222-8333-944445555666"), ("originator", "codex_cli_rs")),
        "ws",
    ),
    Template("llm-ws", "ws", "GET", "/llm/v1/responses", (BEARER,), "ws", exception_route=True),
)
TEMPLATE = {t.name: t for t in TEMPLATES}


# --- the cases --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Plant:
    """What a case planted, and where: a ``secret`` (a synthetic original, masked wherever it is read) or a
    ``canary`` (a token in a component no profile names: ``header:<name>``, ``query:<name>``, ``body:<pointer>``)."""

    kind: str
    where: str
    value: str


@dataclass(frozen=True, slots=True)
class FuzzCase:
    """One case of a cell: its request (target written after the cell's prefix), its frames, its events in order,
    the names of its mutations, what it planted, and the originals the provider echoes back (restoration)."""

    id: str
    cell: str
    template: str
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    frames: tuple[str | bytes, ...]
    events: tuple[str, ...]
    mutations: tuple[str, ...]
    plants: tuple[Plant, ...]
    echo: tuple[str, ...]
    nodes: int = 0
    depth: int = 0

    def descriptor(self) -> dict[str, Any]:
        """Names and kinds alone, never a value: what the corpus and the results say of the case."""
        return {
            "id": self.id,
            "template": self.template,
            "events": list(self.events),
            "mutations": list(self.mutations),
            "plants": [[p.kind, _where_kind(p.where)] for p in self.plants],
            "payload": len(self.body) if not self.frames else max(len(_bytes(f)) for f in self.frames),
            "frames": len(self.frames),
            "nodes": self.nodes,
            "depth": self.depth,
        }

    def digest(self) -> str:
        """The SHA-256 of what the case is (its bytes and events): the corpus' record of it."""
        h = hashlib.sha256()
        for part in (self.cell, self.template, self.method, self.target, json.dumps(self.headers)):
            h.update(part.encode("utf-8", "surrogatepass") + b"\0")
        h.update(self.body + b"\0")
        for frame in self.frames:
            h.update(_bytes(frame) + b"\0")
        h.update(json.dumps([self.events, self.mutations, [(p.kind, p.where, p.value) for p in self.plants]]).encode())
        return h.hexdigest()


def _bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8", "surrogatepass") if isinstance(value, str) else value


def _where_kind(where: str) -> str:
    return where.split(":", 1)[0] + (":" + where.split(":", 1)[1] if where.startswith(("header:", "query:")) else "")


def _secrets() -> tuple[str, ...]:
    from tests.request_coverage import CARD, EMAIL, EMAIL_2, IBAN, PHONE, SECRET

    return (EMAIL, EMAIL_2, SECRET, PHONE, IBAN, CARD)


_TEXTS = (
    "write to {0}",
    "call {3} about the invoice",
    "my key is {2}",
    "pay to {4}",
    "hello there",
    "forward it to {1} and {0}",
    "nothing to see",
    "card {5} expires soon",
    "the team wrote to {0} twice",
)


def _text(rng: random.Random) -> tuple[str, tuple[str, ...]]:
    secrets = _secrets()
    template = rng.choice(_TEXTS)
    used = tuple(secrets[int(n)] for n in re.findall(r"\{(\d)\}", template))
    return template.format(*secrets), used


def _doc(family: str, text: str, rng: random.Random) -> dict[str, Any]:
    stream = rng.random() < 0.3
    if family == "chat":
        doc: dict[str, Any] = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": text}]}
    elif family == "anthropic":
        doc = {"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": [{"role": "user", "content": text}]}
    elif family == "responses":
        doc = {"model": "gpt-5", "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}]}
    elif family == "embeddings":
        return {"model": "text-embedding-3-small", "input": text}
    elif family == "bedrock":
        return {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": text}],
        }
    else:
        raise ValueError(family)
    if stream:
        doc["stream"] = True
    return doc


def _turn(text: str) -> dict[str, Any]:
    return {
        "type": "response.create",
        "model": "gpt-5",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
    }


def measure(doc: Any, depth: int = 1) -> tuple[int, int]:
    """A JSON document's nodes (every value, containers included) and depth."""
    if isinstance(doc, dict):
        found = [measure(v, depth + 1) for v in doc.values()]
    elif isinstance(doc, list):
        found = [measure(v, depth + 1) for v in doc]
    else:
        return 1, depth
    return 1 + sum(n for n, _ in found), max([depth, *(d for _, d in found)])


def _canary(rng: random.Random) -> str:
    return "rcfk" + format(rng.getrandbits(48), "012x")


def _escape_letters(text: str, rng: random.Random) -> str:
    """A text's JSON form with some of its letters written as ``\\u`` escapes (the bytes change, the text does not)."""
    out = []
    for char in text:
        if char.isalpha() and rng.random() < 0.35:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(json.dumps(char)[1:-1])
    return "".join(out)


_HTTP_MUTATIONS = (
    "escape-slot",
    "escape-path",
    "dup-key-slot",
    "dup-key-top",
    "dup-header",
    "field-top",
    "field-nested",
    "field-type",
    "field-remove",
    "exc-header",
    "exc-credential",
    "exc-query",
    "exc-body",
    "exc-near",
    "unknown-header",
    "unknown-query",
    "secret-header",
    "many",
    "deep",
    "large",
)
_WS_MUTATIONS = (
    "escape-slot",
    "dup-key-slot",
    "field-top",
    "field-nested",
    "exc-header",
    "exc-credential",
    "unknown-header",
    "unknown-query",
    "binary-frame",
    "unknown-frame",
    "cancel-frame",
    "many",
)
# Drawn on a verify-only route alone, beside D6's string canaries (``field-top``, ``field-nested``): an unknown member
# holding an object or an array (never D6's form) and one holding a secret (D6's form, refused for what it holds).
_VERIFY_ONLY_MUTATIONS = ("field-object", "secret-member")
# Where D6's string canaries are planted: an unknown member at the top and in a message (no provider item).
_D6_WHERES = frozenset({"body:/x_rcf", "body:nested"})


def _events(rng: random.Random, policy: str, mutations: list[str]) -> tuple[str, ...]:
    """A state sequence for the case, and the mutation each sequence needs to bite (planted in ``mutations``)."""
    roll = rng.random()
    if roll < 0.55:
        return ("request",)
    if roll < 0.65:
        return ("request", "retry")
    if roll < 0.73:
        return ("cancel",) if rng.random() < 0.6 else ("cancel", "retry")
    if policy == "strict" or roll < 0.79:
        mutations.append("new-header" if policy == "exception" else "exc-header")
        return ("review", "request")
    if roll < 0.85:
        mutations.append("new-header")
        return ("review", "approve", "request")
    if roll < 0.90:
        mutations.append("exc-query")
        return ("amend", "request")
    if roll < 0.95:
        mutations.append("exc-header")
        return ("revoke", "request") if rng.random() < 0.7 else ("revoke", "request", "retry")
    mutations.append("exc-header")
    return ("expire", "request")


def generate(seed: int, count: int, cell: str) -> list[FuzzCase]:
    """``count`` cases of ``cell``, drawn by a generator seeded with ``seed`` and the cell's name alone (the same
    cases on every computer), each within ``BOUNDS``."""
    machine, transport, policy, _ = parts(cell)
    rng = random.Random(f"{SCHEMA}:{seed}:{cell}")
    templates = [t for t in TEMPLATES if t.transport == transport and machine in t.machines]
    weights = [(4.0 if t.exception_route and policy == "exception" else 1.0) for t in templates]
    out: list[FuzzCase] = []
    n = 0
    while len(out) < count:
        template = rng.choices(templates, weights)[0]
        case = _draw(rng, cell, policy, template, f"{cell}-{n:05d}")
        n += 1
        if not within(case):  # never generated past the bounds: drawn again (counted by the corpus)
            continue
        out.append(case)
    return out


def _draw(rng: random.Random, cell: str, policy: str, template: Template, case_id: str) -> FuzzCase:
    mutations: list[str] = []
    events = _events(rng, policy, mutations)
    pool = _WS_MUTATIONS if template.transport == "ws" else _HTTP_MUTATIONS
    if template.verify_only:
        pool = (*pool, *_VERIFY_ONLY_MUTATIONS)
    if template.family is None:
        pool = tuple(m for m in pool if not m.startswith(("dup-key", "field", "escape-slot", "exc-body", "many")))
        pool = tuple(m for m in pool if m not in ("deep", "large"))
    roll = rng.random()
    if policy == "exception" and template.exception_route and template.transport == "http" and roll < 0.4:
        # What the exception names, and only that (with mutations that refuse nothing): its path is exercised.
        named = ["exc-header", "exc-credential", "exc-query", "exc-body"]
        mutations += rng.sample(named, rng.randint(1, 4))
        mutations += [m for m in ("escape-slot", "dup-key-slot") if rng.random() < 0.2]
    else:
        extra = 0 if roll < 0.25 else 1 if roll < 0.7 else 2 if roll < 0.92 else 3
        for _ in range(extra):
            mutations.append(rng.choice(pool))
    mutations = list(dict.fromkeys(mutations))  # each once, in the order drawn
    if template.transport == "ws":
        return _draw_ws(rng, cell, template, case_id, events, mutations)
    return _draw_http(rng, cell, template, case_id, events, mutations)


def _draw_http(
    rng: random.Random, cell: str, template: Template, case_id: str, events: tuple[str, ...], mutations: list[str]
) -> FuzzCase:
    plants: list[Plant] = []
    headers = list(template.headers)
    target = template.path
    query: list[str] = []
    text, used = _text(rng)
    echo = used
    plants += [Plant("secret", "slot", s) for s in used]
    body = b""
    nodes = depth = 0
    if template.family is not None:
        doc = _doc(template.family, text, rng)
        holder = _message(doc)
        if "field-top" in mutations:
            value = _canary(rng)
            doc["x_rcf_" + format(rng.getrandbits(16), "04x")] = value
            plants.append(Plant("canary", "body:/x_rcf", value))
        if "field-nested" in mutations and holder is not None:
            value = _canary(rng)
            holder["x_rcf_note"] = value
            plants.append(Plant("canary", "body:nested", value))
        if "field-object" in mutations:  # not D6's form (no string, no flat list of strings): refused everywhere
            value = _canary(rng)
            doc["x_rcf_obj"] = rng.choice([{"note": value}, [{"note": value}], [[value]]])
            plants.append(Plant("canary", "body:/x_rcf_obj", value))
        if "secret-member" in mutations:  # D6's form holding a secret: a verify-only route refuses it
            secret = rng.choice(_secrets()[:5])
            doc["x_rcf_secret"] = f"note for {secret}"
            plants.append(Plant("secret", "body:/x_rcf_secret", secret))
        if "field-type" in mutations:
            doc["stream"] = rng.choice(["yes", 1, None, [True]])
        if "field-remove" in mutations:
            removed = rng.choice(sorted(doc))
            doc.pop(removed)
            if removed in ("messages", "input"):
                echo, holder = (), None  # no text left for the provider to say back
        if "exc-body" in mutations:
            value = _canary(rng)
            doc["metadata_rcf"] = {"team": value}
            plants.append(Plant("canary", "body:/metadata_rcf/team", value))
        if "exc-near" in mutations and rng.random() < 0.5:
            value = _canary(rng)
            doc["metadata_rcf"] = rng.choice([{"teams": value}, {"team": {"name": value}}, {"Team": value}])
            plants.append(Plant("canary", "body:/metadata_rcf/near", value))
        if "many" in mutations and holder is not None and isinstance(doc.get("messages"), list):
            extra_text, more = _text(rng)
            echo = more  # the provider echoes the last message alone
            plants += [Plant("secret", "slot", s) for s in more]
            history = [
                {"role": "assistant" if i % 2 == 0 else "user", "content": f"turn {i}"}
                for i in range(rng.randint(8, 60))
            ]
            doc["messages"] = [*history, *doc["messages"][:-1], {"role": "user", "content": extra_text}]
        if "deep" in mutations:
            value = _canary(rng)
            nested: Any = value
            for _ in range(rng.randint(4, 12)):
                nested = {"a": nested}
            doc["x_rcf_deep"] = nested
            plants.append(Plant("canary", "body:/x_rcf_deep", value))
        if "large" in mutations and holder is not None:
            pad = " ".join(["filler words of a long document"] * rng.randint(100, 1400))
            _set_text(doc, holder, _text_of(holder) + " " + pad)
        nodes, depth = measure(doc)
        raw = json.dumps(doc)
        if "escape-slot" in mutations and used:
            secret = used[0]
            written = json.dumps(secret)[1:-1]
            raw = raw.replace(written, _escape_letters(secret, rng), 1)
        if "dup-key-slot" in mutations and holder is not None and isinstance(holder.get("content"), str):
            # The user message gives its text twice, a shadow copy holding a secret before or after the real one: what
            # is read and masked must be what leaves, whichever a parser keeps.
            secret = _secrets()[rng.randrange(5)]
            plants.append(Plant("secret", "slot", secret))
            shadow = '"content": ' + json.dumps(f"the shadow copy of {secret}")
            real = json.dumps(holder)
            doubled = real[:-1] + ", " + shadow + "}" if rng.random() < 0.5 else "{" + shadow + ", " + real[1:]
            raw = raw.replace(real, doubled, 1)
            echo = ()  # which copy the provider echoes is the parser's
            nodes += 1
        if "dup-key-top" in mutations and raw.startswith('{"model": '):
            raw = '{"model": "gpt-4.1-mini", ' + raw[1:]
            nodes += 1
        body = raw.encode("utf-8", "surrogatepass")
    if "escape-path" in mutations:
        letters = [i for i, c in enumerate(target) if c.isalpha()]
        i = rng.choice(letters)
        target = target[:i] + "%" + format(ord(target[i]), "02X") + target[i + 1 :]
    if "exc-header" in mutations or "new-header" in mutations:
        name = "x-rcf-new" if "new-header" in mutations else "x-rcf-team"
        value = _canary(rng)
        headers.append((name, value))
        plants.append(Plant("canary", f"header:{name}", value))
    if "exc-credential" in mutations:
        value = _canary(rng)
        headers.append(("x-rcf-pass", value))
        plants.append(Plant("canary", "header:x-rcf-pass", value))
    if "exc-near" in mutations:
        value = _canary(rng)
        name = rng.choice(["x-rcf-team2", "x-rcf-tea", "x-rcf_team"])
        headers.append((name, value))
        plants.append(Plant("canary", f"header:{name}", value))
    if "unknown-header" in mutations:
        value = _canary(rng)
        headers.append(("x-rcf-other", value))
        plants.append(Plant("canary", "header:x-rcf-other", value))
    if "secret-header" in mutations:
        secret = rng.choice(_secrets()[:5])
        headers.append(("x-rcf-team", f"team of {secret}"))
        plants.append(Plant("secret", "header:x-rcf-team", secret))
    if "dup-header" in mutations:
        name, value = rng.choice(headers)
        headers.insert(rng.randrange(len(headers) + 1), (name.upper() if rng.random() < 0.3 else name, value))
    if "exc-query" in mutations:
        value = _canary(rng)
        query.append("rcf=" + ("%72" + value[1:] if rng.random() < 0.3 else value))  # its first letter escaped
        plants.append(Plant("canary", "query:rcf", value))
    if "unknown-query" in mutations:
        value = _canary(rng)
        query.append("rcfx=" + value)
        plants.append(Plant("canary", "query:rcfx", value))
    if query and rng.random() < 0.15:
        query.append(rng.choice(query))  # a name twice
        mutations.append("dup-query")
    if query:
        target += "?" + "&".join(query)
    return FuzzCase(
        case_id,
        cell,
        template.name,
        template.method,
        target,
        tuple(headers),
        body,
        (),
        events,
        tuple(mutations),
        tuple(plants),
        echo if template.family in ("chat", "anthropic", "responses") else (),
        nodes,
        depth,
    )


def _message(doc: dict[str, Any]) -> dict[str, Any] | None:
    messages = doc.get("messages") or doc.get("input")
    if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
        return messages[-1]
    return None


def _text_of(holder: dict[str, Any]) -> str:
    content = holder.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and isinstance(content[0], dict):
        return str(content[0].get("text", ""))
    return ""


def _set_text(doc: dict[str, Any], holder: dict[str, Any], text: str) -> None:
    content = holder.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        content[0]["text"] = text
    else:
        holder["content"] = text


def _draw_ws(
    rng: random.Random, cell: str, template: Template, case_id: str, events: tuple[str, ...], mutations: list[str]
) -> FuzzCase:
    plants: list[Plant] = []
    headers = list(template.headers)
    target = template.path
    frames: list[str | bytes] = []
    echo: tuple[str, ...] = ()
    nodes = depth = 0
    for i in range(rng.choice((1, 1, 2, 3))):
        text, used = _text(rng)
        plants += [Plant("secret", "slot", s) for s in used]
        echo = echo or used
        doc = _turn(text)
        holder = doc["input"][0]
        if i == 0 and "field-top" in mutations:
            value = _canary(rng)
            doc["x_rcf_" + format(rng.getrandbits(16), "04x")] = value
            plants.append(Plant("canary", "body:/x_rcf", value))
        if i == 0 and "field-nested" in mutations:
            value = _canary(rng)
            holder["x_rcf_note"] = value
            plants.append(Plant("canary", "body:nested", value))
        if i == 0 and "many" in mutations:
            doc["input"] = [
                *[{"role": "user", "content": [{"type": "input_text", "text": f"turn {k}"}]} for k in range(20)],
                holder,
            ]
        size, deep = measure(doc)
        nodes, depth = max(nodes, size), max(depth, deep)
        raw = json.dumps(doc)
        if i == 0 and "escape-slot" in mutations and used:
            raw = raw.replace(json.dumps(used[0])[1:-1], _escape_letters(used[0], rng), 1)
        if i == 0 and "dup-key-slot" in mutations:
            # The turn gives its input twice: a shadow copy holding a secret, before or after the real one.
            secret = _secrets()[rng.randrange(5)]
            plants.append(Plant("secret", "slot", secret))
            shadow = '"input": ' + json.dumps(f"the shadow copy of {secret}")
            raw = raw[:-1] + ", " + shadow + "}" if rng.random() < 0.5 else "{" + shadow + ", " + raw[1:]
            echo = ()
            nodes += 1
        frames.append(raw)
    if "binary-frame" in mutations:
        frames.insert(rng.randrange(len(frames) + 1), b"\x00rcf" + bytes([rng.randrange(256)]))
    if "unknown-frame" in mutations:
        value = _canary(rng)
        frames.insert(rng.randrange(len(frames) + 1), json.dumps({"type": "session.update", "note": value}))
        plants.append(Plant("canary", "body:/note", value))
    if "cancel-frame" in mutations:
        frames.append('{"type": "response.cancel"}')
    if "exc-header" in mutations or "new-header" in mutations:
        name = "x-rcf-new" if "new-header" in mutations else "x-rcf-team"
        value = _canary(rng)
        headers.append((name, value))
        plants.append(Plant("canary", f"header:{name}", value))
    if "exc-credential" in mutations:
        value = _canary(rng)
        headers.append(("x-rcf-pass", value))
        plants.append(Plant("canary", "header:x-rcf-pass", value))
    if "unknown-header" in mutations:
        value = _canary(rng)
        headers.append(("x-rcf-other", value))
        plants.append(Plant("canary", "header:x-rcf-other", value))
    if "unknown-query" in mutations or "exc-query" in mutations:
        value = _canary(rng)
        name = "rcf" if "exc-query" in mutations else "rcfx"
        target += f"?{name}={value}"
        plants.append(Plant("canary", f"query:{name}", value))
    return FuzzCase(
        case_id,
        cell,
        template.name,
        "GET",
        target,
        tuple(headers),
        b"",
        tuple(frames),
        events,
        tuple(mutations),
        tuple(plants),
        echo,
        nodes,
        depth,
    )


def events_of(case: FuzzCase) -> int:
    """A case's events, as the bound counts them: its state steps, and each request step's frames (one for HTTP)."""
    requests = sum(1 for e in case.events if e in REQUEST_EVENTS)
    return sum(1 for e in case.events if e in STATE_EVENTS) + requests * max(1, len(case.frames))


def within(case: FuzzCase, bounds: Bounds = BOUNDS) -> bool:
    payloads = [len(case.body)] + [len(_bytes(f)) for f in case.frames]
    return (
        max(payloads) <= bounds.payload
        and case.depth <= bounds.depth
        and case.nodes <= bounds.nodes
        and events_of(case) <= bounds.events
    )


def corpus(seed: int, count: int, cells: Sequence[str] = CELLS) -> dict[str, Any]:
    """The corpus of each cell: its cases' descriptors (no value) and digests, and the SHA-256 of the whole cell."""
    out: dict[str, Any] = {"schema": SCHEMA, "seed": seed, "count": count, "bounds": vars_of(BOUNDS), "cells": {}}
    for cell in cells:
        cases = generate(seed, count, cell)
        whole = hashlib.sha256("".join(c.digest() for c in cases).encode()).hexdigest()
        out["cells"][cell] = {
            "sha256": whole,
            "generated": len(cases),
            "mutations": dict(Counter(m for c in cases for m in c.mutations)),
            "events": dict(Counter(e for c in cases for e in c.events)),
            "templates": dict(Counter(c.template for c in cases)),
            "cases": [c.descriptor() | {"sha256": c.digest()} for c in cases],
        }
    return out


def vars_of(bounds: Bounds) -> dict[str, int]:
    return {name: getattr(bounds, name) for name in bounds.__slots__}


# --- what the transport authorized, built and sent (the witness) ---------------------------------------------------


@dataclass
class Witness:
    """``request_transport``'s steps as they ran: each authorization (and refusal), each request built, each send
    called, each WebSocket handshake authorized and opened, each frame authorized and sent; and, for each request the
    recording transport was handed, whether it carried the transport's token (``sending()`` was that very request)."""

    authorized: list[Any] = field(default_factory=list)
    refused: list[Any] = field(default_factory=list)
    built: list[Any] = field(default_factory=list)
    # Each request a client of the cell built, and whether ``request_transport.build`` was building an authorization
    # of this step as it did.
    client_builds: list[bool] = field(default_factory=list)
    sends: list[Any] = field(default_factory=list)
    connects: list[Any] = field(default_factory=list)
    opened: list[Any] = field(default_factory=list)
    frames: list[Any] = field(default_factory=list)
    frames_sent: list[Any] = field(default_factory=list)
    tokens: list[bool] = field(default_factory=list)
    # Each decision the request audit was given (``emit_decision``: the forwarder's, the relay's handshake and turns),
    # as its execution facts: (where, decision kind, attempts, answered, status, the exceptions a Pass names).
    facts: list[tuple[str, str, int | None, int | None, int | None, tuple[str, ...]]] = field(default_factory=list)

    def clear(self) -> None:
        for name in self.__dataclass_fields__:
            getattr(self, name).clear()

    def copy(self) -> Witness:
        return Witness(**{name: list(getattr(self, name)) for name in self.__dataclass_fields__})


@contextlib.contextmanager
def witnessing(witness: Witness, transport: Any) -> Iterator[Witness]:
    """``witness`` fed by ``request_transport``'s functions (module attributes, as the forwarder and the relay call
    them) and by the recording ``transport``, while it is open."""
    import contextvars

    from tests.request_coverage import patched
    from tests.request_recorder import RecordingClient

    from latet.gateway import request_transport as rt

    validate_send, build, send = rt.validate_send, rt.build, rt.send
    validate_connect, connect_ws = rt.validate_connect, rt.connect_ws
    validate_frame, send_frame = rt.validate_frame, rt.send_frame
    handled = transport.handle_async_request
    build_request = RecordingClient.build_request
    building_now: contextvars.ContextVar[Any] = contextvars.ContextVar("rcf_building", default=None)

    def validating(*args: Any, **kwargs: Any) -> Any:
        found = validate_send(*args, **kwargs)
        (witness.authorized if isinstance(found, rt.AuthorizedRequest) else witness.refused).append(found)
        return found

    def building(authorized: Any, *args: Any, **kwargs: Any) -> Any:
        token = building_now.set(authorized)
        try:
            found = build(authorized, *args, **kwargs)
        finally:
            building_now.reset(token)
        if isinstance(found, rt.BuiltRequest):
            witness.built.append(found)
        return found

    def client_built(self: Any, *args: Any, **kwargs: Any) -> Any:
        inside = building_now.get()
        witness.client_builds.append(inside is not None and any(inside is a for a in witness.authorized))
        return build_request(self, *args, **kwargs)

    async def sending(client: Any, built: Any) -> Any:
        witness.sends.append(built)
        return await send(client, built)

    def connecting(*args: Any, **kwargs: Any) -> Any:
        found = validate_connect(*args, **kwargs)
        if isinstance(found, rt.AuthorizedConnect):
            witness.connects.append(found)
        return found

    async def opening(*args: Any, **kwargs: Any) -> Any:
        found = await connect_ws(*args, **kwargs)
        if not isinstance(found, rt.SendRefusal):
            witness.opened.append(args[0] if args else kwargs.get("authorized"))
        return found

    def framing(*args: Any, **kwargs: Any) -> Any:
        found = validate_frame(*args, **kwargs)
        if isinstance(found, rt.AuthorizedFrame):
            witness.frames.append(found)
        return found

    async def sending_frame(connection: Any, authorized: Any) -> Any:
        found = await send_frame(connection, authorized)
        if found is None:
            witness.frames_sent.append(authorized)
        return found

    async def handle(request: Any) -> Any:
        witness.tokens.append(rt.sending() is request)
        return await handled(request)

    from latet.gateway import forward, ws

    def emitting(module: Any) -> Callable[..., None]:
        emit = module.emit_decision

        def emitted(owner: Any, decision: Any, **facts: Any) -> None:
            evidence = getattr(decision, "evidence", None)
            excepted = tuple(getattr(evidence, "exceptions", ()) or ())
            transport = getattr(decision, "transport", None) or getattr(
                getattr(decision, "envelope", None), "transport", "?"
            )
            witness.facts.append(
                (
                    str(transport),
                    type(decision).__name__,
                    facts.get("attempts"),
                    facts.get("answered"),
                    facts.get("status"),
                    excepted,
                )
            )
            emit(owner, decision, **facts)

        return emitted

    with (
        patched(forward, "emit_decision", emitting(forward)),
        patched(ws, "emit_decision", emitting(ws)),
        patched(rt, "validate_send", validating),
        patched(rt, "build", building),
        patched(rt, "send", sending),
        patched(rt, "validate_connect", connecting),
        patched(rt, "connect_ws", opening),
        patched(rt, "validate_frame", framing),
        patched(rt, "send_frame", sending_frame),
        patched(transport, "handle_async_request", handle),
        patched(RecordingClient, "build_request", client_built),
    ):
        yield witness


# --- what one request step did ------------------------------------------------------------------------------------


@dataclass
class Step:
    """One request step of a case, and everything the judge reads of it."""

    kind: str  # "request", "cancel" or "retry"
    permitted: frozenset[tuple[str, str]]  # the components an approved exception in force names now, (kind, name)
    status: int | None = None
    body: bytes = b""
    accepted: bool = False
    frames: list[str | bytes] = field(default_factory=list)
    close: tuple[int, str] | None = None
    error: str | None = None
    sent: list[Any] = field(default_factory=list)  # tests/request_recorder.py's SentRequest
    shakes: list[Any] = field(default_factory=list)  # RecordingWSUpstream's handshakes
    witness: Witness = field(default_factory=Witness)
    builds: int = 0
    sends: int = 0
    records: list[Any] = field(default_factory=list)
    seam: dict[str, int] = field(default_factory=dict)
    answers: tuple[int, int] = (0, 0)
    renewers: list[str] = field(default_factory=list)
    fakes: dict[str, str] = field(default_factory=dict)  # original -> its fake in the vault
    retained: int | None = None
    peak: int | None = None
    blocked_answer: bool = False  # the client was told Latet blocked it


@dataclass
class CaseResult:
    case: FuzzCase
    steps: list[Step]
    logs: list[str]
    violations: dict[str, list[str]]
    asserted: Counter[str]


# --- the judge ------------------------------------------------------------------------------------------------------


def _decoded_forms(data: bytes) -> list[str]:
    """What a payload reads as: its text, its percent-decoding, the strings of its JSON (escapes decoded), and those
    of what it decodes to when it is gzip."""
    forms = [data.decode("latin-1")]
    with contextlib.suppress(UnicodeDecodeError):
        text = data.decode("utf-8")
        forms.append(text)
        forms.append(urllib.parse.unquote(text))
    if data[:2] == b"\x1f\x8b":
        with contextlib.suppress(OSError, EOFError, zlib.error):
            return forms + _decoded_forms(gzip.decompress(data))
    with contextlib.suppress(ValueError, UnicodeDecodeError, RecursionError):
        forms += list(_strings(json.loads(data)))
    return forms


def _strings(doc: Any) -> Iterator[str]:
    if isinstance(doc, str):
        yield doc
    elif isinstance(doc, dict):
        for key, value in doc.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _strings(value)


def upstream_items(step: Step) -> list[tuple[str, str, bytes]]:
    """Everything that reached the provider in a step, each as (location, name, bytes): an HTTP request's path, each
    query occurrence, each header value and its content; a WebSocket's handshake path, query, headers and each
    frame."""
    out: list[tuple[str, str, bytes]] = []
    for sent in step.sent:
        path, _, query = sent.raw_path.partition(b"?")
        out.append(("path", "", path))
        for part in query.split(b"&") if query else []:
            name, _, value = part.partition(b"=")
            out.append(("query", urllib.parse.unquote(name.decode("latin-1")), value))
        out += [("header", name.decode("latin-1").lower(), value) for name, value in sent.headers]
        out.append(("body", "", sent.content))
    for shake in step.shakes:
        out.append(("path", "", shake.raw_path))
        for part in shake.query_string.split(b"&") if shake.query_string else []:
            name, _, value = part.partition(b"=")
            out.append(("query", urllib.parse.unquote(name.decode("latin-1")), value))
        out += [("header", name.decode("latin-1").lower(), value) for name, value in shake.headers]
        out += [("frame", "", _bytes(frame)) for frame in shake.frames]
    return out


def _body_value_at(content: bytes, pointer: str) -> str | None:
    with contextlib.suppress(ValueError, UnicodeDecodeError):
        doc: Any = json.loads(content)
        for part in pointer.strip("/").split("/"):
            if not isinstance(doc, dict) or part not in doc:
                return None
            doc = doc[part]
        return doc if isinstance(doc, str) else None
    return None


def judge(
    case: FuzzCase, steps: Sequence[Step], logs: Sequence[str], history: str, machine: str
) -> tuple[dict[str, list[str]], Counter[str]]:
    """Each assertion family's violations in a case (empty: none), and how many checks of each it made."""
    found: dict[str, list[str]] = {name: [] for name in ASSERTIONS}
    asserted: Counter[str] = Counter()

    def check(name: str, ok: bool, why: str) -> None:
        asserted[name] += 1
        if not ok:
            found[name].append(why)

    secrets = tuple(p.value for p in case.plants if p.kind == "secret")
    every_secret = _secrets()
    for n, step in enumerate(steps):
        where = f"{step.kind}#{n}"
        _judge_coverage(step, where, check)
        _judge_authority(step, where, check)
        _judge_bytes(step, where, check)
        _judge_canary(case, step, where, check)
        items = upstream_items(step)
        for location, _, data in items:
            forms = _decoded_forms(data)
            readable = [s for s in every_secret if any(s in form for form in forms)]
            check("masking", not readable, f"{where}: {len(readable)} original(s) readable upstream in {location}")
        if TEMPLATE[case.template].verify_only and any(p.where == "body:/x_rcf_secret" for p in case.plants):
            # D6's form holding a secret, on a route checked and never rewritten: refused, nothing sent.
            check("masking", not step.sent, f"{where}: a secret in an unknown member of a verify-only call was sent")
        _judge_restoration(case, step, where, history, check)
        _judge_sinks(case, step, where, history, check, secrets)
        _judge_ownership(step, where, machine, check)
    for line in logs:
        leaked = [s for s in every_secret if s in line] + [c for c in _credentials(case) if c in line]
        check("sinks", not leaked, f"a log line holds {len(leaked)} value(s) it must not")
    return found, asserted


def _credentials(case: FuzzCase) -> list[str]:
    """What must never be in a sink: the provider credentials, and the credential-role canaries the case planted."""
    out = list(CREDENTIALS)
    out += [p.value for p in case.plants if p.where == "header:x-rcf-pass"]
    return out


def _judge_coverage(step: Step, where: str, check: Callable[[str, bool, str], None]) -> None:
    from latet.gateway.outcome import Forward, Pass

    decisions = [a.decision for a in step.witness.authorized]
    decisions += [c.decision for c in step.witness.connects]
    decisions += [f.decision for f in step.witness.frames]
    for decision in decisions:
        envelope = decision.envelope
        kind = type(decision).__name__
        check("coverage", isinstance(decision, Forward | Pass), f"{where}: an authorized {kind} is not a decision")
        proof = decision.envelope_proof
        if envelope.transport != "ws-frame":
            check(
                "coverage",
                proof is not None and proof.proves(envelope),
                f"{where}: {kind} over {envelope.transport} without a complete envelope proof",
            )
        if proof is not None:
            check(
                "coverage",
                len(proof.treatments) == proof.inbound
                and all(t in ("inspected", "checked", "removed", "authorized") for t in proof.treatments),
                f"{where}: an envelope occurrence without exactly one treatment",
            )
        body = decision.proof if isinstance(decision, Forward) else decision.body_proof
        if body is None:
            check(
                "coverage",
                envelope.body in (b"", "") or envelope.transport == "ws-handshake",
                f"{where}: {kind} carries a body no proof covers",
            )
            continue
        check("coverage", body.proves(envelope), f"{where}: {kind}'s body proof does not prove its envelope")
        receipts = getattr(body, "receipts", None)
        if receipts is not None:  # a body's own proof (``BodyProof``): each entry removed or covered, finals once
            finals = getattr(body, "final_receipts", ())
            check(
                "coverage",
                len(set(finals)) == len(finals) and all(receipts[i] == "covered" for i in finals),
                f"{where}: a final entry covered twice or not covered",
            )
            check(
                "coverage",
                getattr(body, "payload", None) == decision.payload,
                f"{where}: the decision leaves with another payload than its proof's",
            )


def _judge_authority(step: Step, where: str, check: Callable[[str, bool, str], None]) -> None:
    w = step.witness
    authorized = {id(a) for a in w.authorized}
    built = {id(b) for b in w.built}
    check("authority", all(w.tokens), f"{where}: a request reached the transport without the token")
    check("authority", len(w.tokens) == len(step.sent), f"{where}: a request reached the provider unseen")
    check(
        "authority",
        step.builds == len(w.client_builds) and all(w.client_builds),
        f"{where}: a request built outside an authorized build of this step",
    )
    check("authority", step.sends <= len(w.sends), f"{where}: {step.sends} sent, {len(w.sends)} sends authorized")
    check(
        "authority",
        all(id(b.authorized) in authorized for b in w.built),
        f"{where}: a request built from no authorization of this step",
    )
    check("authority", all(id(b) in built for b in w.sends), f"{where}: a send of nothing built")
    check("authority", len(step.sent) <= len(w.sends), f"{where}: more requests reached the provider than were sent")
    attempts = [a.attempt for a in w.authorized]
    check(
        "authority",
        all(a == 1 or (a == 2 and x.credentials == "aws") for a, x in zip(attempts, w.authorized, strict=True)),
        f"{where}: a second attempt that is not AWS's",
    )
    check(
        "authority",
        len(step.shakes) == len(w.opened),
        f"{where}: {len(step.shakes)} handshakes, {len(w.opened)} opened",
    )
    check("authority", all(any(o is c for c in w.connects) for o in w.opened), f"{where}: a socket opened unauthorized")
    frames_up = sum(len(s.frames) for s in step.shakes)
    check(
        "authority", frames_up == len(w.frames_sent), f"{where}: {frames_up} frames upstream, {len(w.frames_sent)} sent"
    )
    if step.blocked_answer:
        check(
            "authority",
            not step.sent and not step.shakes,
            f"{where}: the client was told it was blocked, yet something reached the provider",
        )


def _judge_bytes(step: Step, where: str, check: Callable[[str, bool, str], None]) -> None:
    w = step.witness
    for built in w.built:
        final = built.final
        check("bytes", final.content == built.authorized.payload, f"{where}: frozen content is not the payload")
        check(
            "bytes",
            built.authorized.payload == built.authorized.decision.payload,
            f"{where}: authorized payload is not the decision's",
        )
    finals = [b.final for b in w.sends]
    for sent in step.sent:
        url = sent.host.encode("ascii") + sent.raw_path
        match = [
            f
            for f in finals
            if f.method == sent.method and f.url.split(b"://", 1)[-1] == url and tuple(f.headers) == tuple(sent.headers)
        ]
        check("bytes", bool(match), f"{where}: a request reached the provider unlike any frozen one")
        check(
            "bytes",
            any(f.content == sent.content for f in match),
            f"{where}: what reached the provider is not the frozen content",
        )
    payloads = [f.payload for f in w.frames_sent]
    upstream = [frame for shake in step.shakes for frame in shake.frames]
    check(
        "bytes",
        [_bytes(p) for p in payloads] == [_bytes(f) for f in upstream],
        f"{where}: the frames upstream are not the frames authorized",
    )
    for shake, opened in zip(step.shakes, w.opened, strict=False):
        library = {
            "host",
            "upgrade",
            "connection",
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-extensions",
        }
        given = [
            (k.decode("latin-1"), v.decode("latin-1")) for k, v in shake.headers if k.lower().decode() not in library
        ]
        check("bytes", given == list(opened.headers), f"{where}: the handshake's headers are not those authorized")


def _checked_only(step: Step) -> bool:
    """Whether every decision that let the step's request leave is a ``Pass`` under the built-in rule "checked only"
    (D6) and nothing else: no exception."""
    decisions = [a.decision for a in step.witness.authorized]
    return bool(decisions) and all(
        type(d).__name__ == "Pass" and "checked only" in d.evidence.rules and not d.evidence.exceptions
        for d in decisions
    )


def d6_permits(case: FuzzCase, plant: Plant, step: Step) -> bool:
    """D6 (approved 2026-10-29; ``body_coverage._Body.obj``): on a verify-only route an unknown body member whose value
    is a string is checked text, and passes "checked only". Root's ruling on the full fuzz of 2e3cea71: a string canary
    planted there may leave, when the step's decision is that Pass and the canary holds no secret; an object or an
    array there (``field-object``) never may, nor a secret (``secret-member``, judged by ``masking``)."""
    return (
        TEMPLATE[case.template].verify_only
        and plant.kind == "canary"
        and plant.where in _D6_WHERES
        and not any(secret in plant.value for secret in _secrets())
        and _checked_only(step)
    )


def _judge_canary(case: FuzzCase, step: Step, where: str, check: Callable[[str, bool, str], None]) -> None:
    items = upstream_items(step)
    for plant in case.plants:
        if plant.kind != "canary":
            continue
        kind, _, name = plant.where.partition(":")
        places = []
        for location, item_name, data in items:
            forms = _decoded_forms(data)
            if any(plant.value in form for form in forms):
                places.append((location, item_name, data))
        if not places:
            asserted_ok = True
        elif d6_permits(case, plant, step):
            asserted_ok = all(loc == "body" for loc, _, _ in places)  # in the body it came in, nowhere else
        else:
            permitted = (kind, name) in step.permitted
            exact = all(
                (kind == "header" and loc == "header" and nm == name)
                or (kind == "query" and loc == "query" and nm == name)
                or (kind == "body" and loc == "body" and _body_value_at(data, name) == plant.value)
                for loc, nm, data in places
            )
            asserted_ok = permitted and exact
        check(
            "canary",
            asserted_ok,
            f"{where}: a canary of {kind} {name if kind != 'body' else 'member'} left "
            f"{'outside its component' if places and (kind, name) in step.permitted else 'with no exception in force'}",
        )


_TEXT_KEYS = frozenset({"content", "text", "delta", "output_text", "partial_json", "arguments"})
# What a model's answer holds that Latet leaves untouched by design: its thinking, signed by the provider
# (``protocols/rewrite.py``: "thinking untouched"), never restored.
_UNTOUCHED_TYPES = frozenset({"thinking", "thinking_delta", "redacted_thinking", "signature_delta"})
_UNTOUCHED_KEYS = frozenset({"thinking", "signature"})


def _said(doc: Any) -> Iterator[str]:
    """The texts a model's answer (an event, a frame, a whole answer) says, in order: each string under ``content``,
    ``text``, ``delta``, ``output_text``, ``partial_json`` or ``arguments``, at any depth, its thinking left out."""
    if isinstance(doc, dict):
        if doc.get("type") in _UNTOUCHED_TYPES:
            return
        for key, value in doc.items():
            if key in _UNTOUCHED_KEYS:
                continue
            if key in _TEXT_KEYS and isinstance(value, str):
                yield value
            elif isinstance(value, dict | list):
                yield from _said(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _said(value)


def _untouched_removed(doc: Any) -> Any:
    if isinstance(doc, dict):
        if doc.get("type") in _UNTOUCHED_TYPES:
            return None
        return {k: _untouched_removed(v) for k, v in doc.items() if k not in _UNTOUCHED_KEYS}
    if isinstance(doc, list):
        return [_untouched_removed(v) for v in doc]
    return doc


def _readable(raw: str | bytes) -> str:
    """A body, an event or a frame as text, what Latet leaves untouched by design taken out of it."""
    try:
        return json.dumps(_untouched_removed(json.loads(raw)), ensure_ascii=False)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


def _answer_text(step: Step) -> str:
    """What the client was answered, as text: its body and frames (each SSE event, each frame, as it came, its thinking
    taken out), then what the answer says joined in order (a fake or an original split across deltas is whole again)."""
    texts: list[str] = []
    pieces: list[str] = []
    lines = step.body.replace(b"\r\n", b"\n").split(b"\n")
    if any(line.startswith(b"data:") for line in lines):
        for line in lines:
            if line.startswith(b"data:"):
                texts.append(_readable(line[5:].strip()))
                with contextlib.suppress(ValueError, UnicodeDecodeError):
                    pieces += list(_said(json.loads(line[5:])))
            elif line and not line.startswith(b"event:"):
                texts.append(line.decode("utf-8", "replace"))
    else:
        texts.append(_readable(step.body))
        with contextlib.suppress(ValueError, UnicodeDecodeError):
            pieces += list(_said(json.loads(step.body)))
    for frame in step.frames:
        texts.append(_readable(frame))
        if isinstance(frame, str):
            with contextlib.suppress(ValueError):
                pieces += list(_said(json.loads(frame)))
    return "\n".join(texts) + "\n" + "".join(pieces)


def _judge_restoration(
    case: FuzzCase, step: Step, where: str, history: str, check: Callable[[str, bool, str], None]
) -> None:
    text = _answer_text(step)
    fakes = [fake for original, fake in step.fakes.items() if len(fake) >= 6 and fake != original]
    check("restoration", not any(f in text for f in fakes), f"{where}: a fake left in the client's answer")
    turns = sum(1 for f in case.frames if isinstance(f, str) and '"response.create"' in f)
    whole = (
        step.status == 200 and len(step.sent) >= 1
        if not case.frames
        else bool(step.shakes) and sum(len(s.frames) for s in step.shakes) >= len(case.frames) and turns >= 1
    )
    if whole and step.kind != "cancel" and case.echo:
        # An original masked reversibly (its fake in the vault) that the provider said back comes back as it was.
        missing = [o for o in case.echo if o in step.fakes and o not in text]
        check("restoration", not missing, f"{where}: {len(missing)} original(s) not restored in the echoed answer")
    _judge_facts(case, step, where, history, check)


def _excepted_out(case: FuzzCase, step: Step) -> bool:
    """Whether a canary an exception in force permits reached the provider in this step (the request was let through
    by that exception)."""
    items = upstream_items(step)
    for plant in case.plants:
        kind, _, name = plant.where.partition(":")
        if (
            plant.kind == "canary"
            and (kind, name) in step.permitted
            and any(plant.value in form for _, _, data in items for form in _decoded_forms(data))
        ):
            return True
    return False


def _judge_facts(case: FuzzCase, step: Step, where: str, history: str, check: Callable[[str, bool, str], None]) -> None:
    """The execution facts the request audit was given (one decision a request, a handshake or a turn), and what the
    history records of them."""
    facts = step.witness.facts
    if not case.frames:  # an HTTP request: exactly one decision, its attempts what reached the provider
        check("restoration", len(facts) == 1, f"{where}: {len(facts)} decisions recorded for one request")
        if len(facts) != 1:
            return
        _, kind, attempts, _, _, excepted = facts[0]
        if kind == "Block":
            check("restoration", not step.sent and attempts == 0, f"{where}: a Block recorded with attempts or sends")
        else:
            check(
                "restoration",
                attempts is not None and attempts == len(step.sent) >= 1,
                f"{where}: {attempts} attempts recorded, {len(step.sent)} reached the provider",
            )
        if _excepted_out(case, step):
            check(
                "restoration",
                kind == "Pass" and bool(excepted),
                f"{where}: a request let through by an exception recorded as a {kind} naming none",
            )
        if history == "on" and step.kind != "cancel":
            check("restoration", len(step.records) <= 1, f"{where}: {len(step.records)} history records for a request")
            if step.sent:
                check("restoration", len(step.records) == 1, f"{where}: a request sent with no history record")
            for record in step.records:
                summary = record.summary
                blocked = "blocked" in (summary.get("flags") or [])
                check("restoration", blocked == (kind == "Block"), f"{where}: the history and the decision differ")
                check(
                    "restoration",
                    summary.get("status") == step.status or step.status is None,
                    f"{where}: the history's status is not the answer's",
                )
                if _excepted_out(case, step):
                    check(
                        "restoration",
                        "exception" in (summary.get("passes") or []),
                        f"{where}: the history does not show a request let through by an exception",
                    )
        return
    handshakes = [f for f in facts if f[0] == "ws-handshake" or (f[1] == "Block" and f is facts[0])]
    check("restoration", len(handshakes) >= 1 or not step.accepted, f"{where}: a socket accepted with no decision")
    sent_frames = [f for f in facts if f[0] == "ws-frame" and f[1] != "Block"]
    frames_up = sum(len(s.frames) for s in step.shakes)
    check(
        "restoration",
        len(sent_frames) >= frames_up,
        f"{where}: {frames_up} frames reached the provider, {len(sent_frames)} decided to leave",
    )


def _record_strings(record: Any) -> Iterator[str]:
    seen: set[int] = set()

    def walk(value: Any, depth: int = 0) -> Iterator[str]:
        if depth > 12 or id(value) in seen:
            return
        if isinstance(value, str):
            yield value
            return
        if isinstance(value, bytes):
            yield value.decode("latin-1")
            return
        seen.add(id(value))
        if isinstance(value, dict):
            for k, v in value.items():
                yield from walk(k, depth + 1)
                yield from walk(v, depth + 1)
        elif isinstance(value, list | tuple | set | frozenset):
            for v in value:
                yield from walk(v, depth + 1)
        elif hasattr(value, "__dataclass_fields__"):
            for name in value.__dataclass_fields__:
                yield from walk(getattr(value, name, None), depth + 1)
        elif hasattr(value, "__dict__"):
            yield from walk(vars(value), depth + 1)

    yield from walk(record)


def _judge_sinks(
    case: FuzzCase,
    step: Step,
    where: str,
    history: str,
    check: Callable[[str, bool, str], None],
    secrets: Sequence[str],
) -> None:
    if history == "off":
        check("sinks", not step.records, f"{where}: {len(step.records)} history records with the history off")
        return
    # The documented contract (docs dashboard, security): the history keeps a request as it left, masked, and a
    # blocked one as it would have left (encrypted), so that you see what stopped it; never a credential.
    for record in step.records:
        blocked = "blocked" in (record.summary.get("flags") or [])
        forbidden = [*([] if blocked else _secrets()), *_credentials(case)]
        text = "\n".join(_record_strings(record))
        leaked = [v for v in forbidden if v in text]
        check("sinks", not leaked, f"{where}: a history record holds {len(leaked)} value(s) it must not")


def _judge_ownership(step: Step, where: str, machine: str, check: Callable[[str, bool, str], None]) -> None:
    holds = step.seam.get("hold", 0) + step.seam.get("reacquire", 0)
    check(
        "ownership",
        step.seam.get("release", 0) >= holds,
        f"{where}: {holds} holds, {step.seam.get('release', 0)} released",
    )
    check(
        "ownership",
        step.answers[0] == step.answers[1],
        f"{where}: {step.answers[0] - step.answers[1]} answers left open",
    )
    check("ownership", "running" not in step.renewers, f"{where}: a renewer still running after the request")
    if step.retained is not None:
        check("ownership", step.retained <= RETAINED_MOST, f"{where}: retained request bytes past RC-R1's caps")
    if step.peak is not None:
        check("ownership", step.peak <= MEMORY_PEAK, f"{where}: traced peak {step.peak} bytes past {MEMORY_PEAK}")


# --- the policy state of a cell ---------------------------------------------------------------------------------------


class PolicyState:
    """The request exception of a cell, moved by a case's state events through the product's own paths (the settings
    proposed, approved or rejected as you would, the exception clock moved on), and set back after it. ``permitted``:
    the components an approved exception in force names exactly, as the judge's oracle says from the events alone."""

    def __init__(self, machine: str, transport: str, policy: str) -> None:
        self.machine, self.transport, self.policy = machine, transport, policy
        self.base = COMPONENTS if transport == "http" else WS_COMPONENTS
        self.profile: str = HTTP_PROFILE
        self.in_force: tuple[tuple[str, str, str], ...] = ()
        self.pending: str | None = None
        self.later: timedelta | None = None
        self.changed = False

    @property
    def permitted(self) -> frozenset[tuple[str, str]]:
        if self.transport != "http" or self.later is not None:
            return frozenset()  # a WebSocket takes no exception (F-2); one past its lifetime grants nothing
        return frozenset((kind, name) for kind, name, _ in self.in_force)

    async def _call(self, runtime: Any, function: Callable[..., Any], *args: Any) -> Any:
        if self.machine == "server":
            return await runtime.mediator.run(function, *args)
        return await asyncio.to_thread(function, *args)

    def _rows(self, components: Sequence[tuple[str, str, str]]) -> list[Any]:
        from latet.config import RequestException

        return [
            RequestException.model_validate(
                {
                    "destination": DESTINATION,
                    "profile": self.profile,
                    "reason": "custom-header",
                    "components": [
                        {"kind": k, "name": n, "role": r, **({"type": "string"} if k == "body" else {})}
                        for k, n, r in components
                    ],
                }
            )
        ]

    async def _propose(self, runtime: Any, components: Sequence[tuple[str, str, str]]) -> Any:
        rows = self._rows(components) if components else []
        wanted = runtime.config.model_copy(update={"request_exceptions": rows})
        return await self._call(runtime, runtime.propose, wanted, "command")

    async def _approved(self, runtime: Any, components: Sequence[tuple[str, str, str]]) -> None:
        change = await self._propose(runtime, components)
        if change is not None:
            decided = await self._call(runtime, runtime.decide_change, change.id, True)
            if decided != "approved":
                raise RuntimeError(f"the cell's exception was not approved ({decided})")

    async def _raised(self, runtime: Any, keep: Callable[[Any], Any | None]) -> None:
        """A change that raises protection, applied at once as the dashboard applies one (``update_config``: the
        exceptions' DELETE, a narrowing saved): each approved row as ``keep`` makes it (None: taken away), its stamps
        kept."""

        def change(current: Any) -> Any:
            rows = [kept for row in current.request_exceptions if (kept := keep(row)) is not None]
            return current.model_copy(update={"request_exceptions": rows})

        await self._call(runtime, runtime.update_config, change)

    def _running(self, runtime: Any) -> tuple[tuple[str, str, str], ...]:
        """What the approved exceptions in the runtime's settings name now."""
        rows = runtime.config.request_exceptions
        return tuple((c.kind, c.name, c.role) for row in rows if row.approved for c in row.components)

    async def start(self, runtime: Any) -> None:
        """The cell's base state: none (strict), or the exception approved and in force."""
        self.profile = profile_of(runtime, self.transport)
        if self.policy == "exception":
            await self._approved(runtime, self.base)
            self.in_force = self.base
        if self._running(runtime) != self.in_force:
            raise RuntimeError("the cell's exception is not as its policy says")
        self.changed = False

    async def apply(self, event: str, runtime: Any) -> None:
        self.changed = True
        if event == "review":
            wanted = (*self.in_force, WIDENED) if self.policy == "exception" else self.base
            change = await self._propose(runtime, wanted)
            self.pending = change.id if change is not None else None
        elif event == "approve":
            if self.pending is not None:
                decided = await self._call(runtime, runtime.decide_change, self.pending, True)
                if decided == "approved":
                    self.in_force = (*self.in_force, WIDENED) if self.policy == "exception" else self.base
                self.pending = None
        elif event == "amend":
            await self._raised(
                runtime,
                lambda row: row.model_copy(
                    update={"components": [c for c in row.components if (c.kind, c.name, c.role) != NARROWED]}
                ),
            )
            self.in_force = tuple(c for c in self.in_force if c != NARROWED)
        elif event == "revoke":
            await self._raised(runtime, lambda row: None)
            self.in_force = ()
        elif event == "expire":
            self.later = timedelta(days=31)
            self._clock(runtime, self.later)

    def _clock(self, runtime: Any, later: timedelta | None) -> None:
        """The exception clock moved on by ``later`` (None: as it is): the runtime's on a computer, RC-REC-3's read of
        PostgreSQL's on a server."""
        from datetime import UTC, datetime

        if self.machine == "computer":
            runtime.exception_clock = (
                (lambda: datetime.now(UTC) + later) if later is not None else (lambda: datetime.now(UTC))
            )
            return
        from latet.server import spaces as spaces_module

        if not hasattr(spaces_module, "_rcf_counts_and_now"):
            spaces_module._rcf_counts_and_now = spaces_module._counts_and_now  # type: ignore[attr-defined]
        read = spaces_module._rcf_counts_and_now  # type: ignore[attr-defined]
        if later is None:
            spaces_module._counts_and_now = read
            return

        def moved(db: Any, owner: str) -> tuple[Any, int, int]:
            now, settings, shared = read(db, owner)
            return now + later, settings, shared

        spaces_module._counts_and_now = moved

    async def reset(self, runtime: Any) -> None:
        """Back to the cell's base state after a case that moved it."""
        if not self.changed:
            return
        if self.later is not None:
            self._clock(runtime, None)
            self.later = None
        for change in list(runtime.mediator.approvals.changes.values()):
            with contextlib.suppress(Exception):
                await self._call(runtime, runtime.decide_change, change.id, False)
        self.pending = None
        base = self.base if self.policy == "exception" else ()
        if self._running(runtime) != base:
            await self._raised(runtime, lambda row: None)  # removing applies at once
            if self.policy == "exception":
                await self._approved(runtime, self.base)
        if self._running(runtime) != base:
            raise RuntimeError("the cell's exception could not be set back")
        self.in_force = base
        self.changed = False


def profile_of(runtime: Any, transport: str) -> str:
    """The request profile the cell's exception names, as the admission names the exception's route. A WebSocket's
    handshake id is an HTTP profile's with ``profile_ids.WS_SUFFIX``, which no exception may name (0.11.0 takes none
    over a WebSocket): its cells' exception names the same route's HTTP profile, and their WebSocket takes nothing of
    it."""
    from latet.gateway import request_profiles
    from latet.gateway.coverage import Envelope, inventory

    path = b"/llm/v1/chat/completions" if transport == "http" else b"/llm/v1/responses"
    upstream = runtime.gateway.routes.match(path.decode())
    kind = "http" if transport == "http" else "ws-handshake"
    method = "POST" if transport == "http" else "GET"
    headers = ((b"authorization", b"Bearer sk-rc-test"),)
    envelope = Envelope(kind, method, path, b"", headers, b"", DESTINATION)
    found = request_profiles.admit(inventory(envelope), upstream).profile.id
    try:
        from latet.gateway.profile_ids import WS_SUFFIX
    except ImportError:  # a head before P2-7's closed set
        return found
    return found.removesuffix(WS_SUFFIX)


# --- running a case ---------------------------------------------------------------------------------------------------


@dataclass
class FuzzCell:
    """A cell of the fuzz on its machine (``tests/request_coverage.py``'s ``Cell``), its policy state and witness."""

    name: str
    cell: Any
    state: PolicyState
    witness: Witness
    memory_every: int = 16


async def _http_step(
    cell: Any, method: str, target: bytes, headers: list[tuple[bytes, bytes]], body: bytes, cancel: bool
) -> Any:
    """One HTTP request through the cell's app (``http_call``), or, with ``cancel``, its client gone as the answer
    starts."""
    from tests.request_coverage import ClientView, _split, http_call

    if not cancel:
        return await http_call(cell.app, method, target, headers, body, cell.client, cell.server, cell.scheme)
    raw_path, query, path = _split(target)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": cell.scheme,
        "path": path,
        "raw_path": raw_path,
        "query_string": query,
        "root_path": "",
        "headers": list(headers),
        "client": cell.client,
        "server": cell.server,
        "state": {},
    }
    view = ClientView()
    started = asyncio.Event()
    asked = [False]

    async def receive() -> dict[str, Any]:
        if not asked[0]:
            asked[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            view.status = message["status"]
        elif message["type"] == "http.response.body":
            view.body += message.get("body", b"")
            started.set()

    try:
        await asyncio.wait_for(cell.app(scope, receive, send), 60)
    except Exception as exc:
        view.error = type(exc).__name__
    finally:
        started.set()
    return view


async def _ws_step(
    cell: Any, target: bytes, headers: list[tuple[bytes, bytes]], frames: Sequence[str | bytes], cancel: bool
) -> Any:
    """One WebSocket through the cell's app (``ws_call``), or, with ``cancel``, its client gone right after its first
    frame, before any answer to it."""
    from tests.request_coverage import ClientView, _split, ws_call

    scheme = "wss" if cell.scheme == "https" else "ws"
    if not cancel:
        return await ws_call(cell.app, target, headers, frames, cell.client, cell.server, scheme)
    raw_path, query, path = _split(target)
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": scheme,
        "path": path,
        "raw_path": raw_path,
        "query_string": query,
        "root_path": "",
        "headers": list(headers),
        "client": cell.client,
        "server": cell.server,
        "subprotocols": [],
        "state": {},
        "extensions": {"websocket.http.response": {}},
    }
    view = ClientView()
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    accepted = asyncio.Event()

    async def receive() -> dict[str, Any]:
        return await inbox.get()

    async def send(message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "websocket.accept":
            view.accepted = True
            accepted.set()
        elif kind == "websocket.send":
            data = message.get("text")
            view.frames.append(data if data is not None else message.get("bytes", b""))
        elif kind == "websocket.close":
            view.close = (message.get("code", 1000), message.get("reason") or "")
            accepted.set()
        elif kind == "websocket.http.response.start":
            view.status = message["status"]
            accepted.set()

    await inbox.put({"type": "websocket.connect"})
    task = asyncio.create_task(cell.app(scope, receive, send))
    task.add_done_callback(lambda _: accepted.set())
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(30):
            await accepted.wait()
    if view.accepted and frames:
        first = frames[0]
        await inbox.put(
            {"type": "websocket.receive", "text": first}
            if isinstance(first, str)
            else {"type": "websocket.receive", "bytes": first}
        )
    await inbox.put({"type": "websocket.disconnect", "code": 1001})
    try:
        await asyncio.wait_for(task, 30)
    except Exception as exc:
        view.error = type(exc).__name__
    return view


async def run_case(fuzz: FuzzCell, case: FuzzCase, index: int) -> CaseResult:
    """``case`` through its cell: its state events applied, each request step sent and observed, the state set back;
    judged."""
    from tests.request_coverage import _drain, logged, setup_for

    cell = fuzz.cell
    machine, transport, _, history = parts(fuzz.name)
    runtime = await cell.runtime()
    template = TEMPLATE[case.template]
    steps: list[Step] = []
    records: list[Any] = getattr(runtime.gateway.recorder, "_rc_records", [])
    with logged() as logs:
        setup = types.SimpleNamespace(setup=template.setup)
        async with setup_for(cell, setup, runtime) as found:  # type: ignore[arg-type]
            target = case.target.replace("{profile_key}", found.get("profile_key", "k" * 22))
            raw_target = (cell.prefix + target).encode("latin-1")
            headers = [(b"host", cell.host.encode())]
            headers += [(k.encode("latin-1"), v.encode("latin-1")) for k, v in case.headers]
            for event in case.events:
                if event in STATE_EVENTS:
                    await fuzz.state.apply(event, runtime)
                    continue
                step = Step(event, fuzz.state.permitted)
                cell.transport.clear()
                cell.ws.clear()
                cell.provider.answers.clear()
                records.clear()
                cell.seam.counts.clear()
                fuzz.witness.clear()
                builds0, sends0 = cell.clients.counts()
                traced = fuzz.memory_every > 0 and index % fuzz.memory_every == 0
                if traced:
                    tracemalloc.start()
                try:
                    if transport == "ws":
                        view = await _ws_step(cell, raw_target, headers, case.frames, event == "cancel")
                    else:
                        view = await _http_step(cell, case.method, raw_target, headers, case.body, event == "cancel")
                    await _drain(cell, runtime)
                    for _ in range(2):
                        await cell.clock.tick()
                    step.renewers = cell.clock.states()
                    await cell.clock.stop()
                    await _drain(cell, runtime)
                finally:
                    if traced:
                        step.peak = tracemalloc.get_traced_memory()[1]
                        tracemalloc.stop()
                builds, sends = cell.clients.counts()
                step.status, step.body, step.error = view.status, view.body, view.error
                step.accepted, step.frames, step.close = view.accepted, list(view.frames), view.close
                step.sent = list(cell.transport.sent)
                step.shakes = list(cell.ws.handshakes)
                step.witness = fuzz.witness.copy()
                step.builds, step.sends = builds - builds0, sends - sends0
                step.records = list(records)
                step.seam = dict(cell.seam.counts)
                answers = cell.provider.answers
                step.answers = (len(answers), sum(a.closed for a in answers))
                step.fakes = _fakes_of(runtime)
                step.retained = runtime.request_retained_bytes
                first = step.witness.facts[0] if step.witness.facts else None
                step.blocked_answer = first is not None and first[1] == "Block" and first[0] != "ws-frame"
                steps.append(step)
    lines = [_rendered(line) for line in logs.lines]
    try:
        await fuzz.state.reset(runtime)
    finally:
        violations, asserted = judge(case, steps, lines, history, machine)
    return CaseResult(case, steps, lines, violations, asserted)


def _rendered(line: tuple[str, str, str, tuple[Any, ...]]) -> str:
    name, level, template, args = line
    try:
        return f"{name} {level} " + (template % args if args else template)
    except (TypeError, ValueError):
        return f"{name} {level} {template} {args}"


def _fakes_of(runtime: Any) -> dict[str, str]:
    from tests.request_coverage import ORIGINALS

    vault = runtime.built.vault
    out: dict[str, str] = {}
    for original in ORIGINALS:
        with contextlib.suppress(Exception):
            entity = vault.by_original(original)
            if entity is not None:
                out[original] = entity.surrogate
    return out


@dataclass
class CellReport:
    """What a cell did: its cases generated, executed and judged, each assertion's checks and violations, the outcomes
    seen, and the first violations (names and categories, never a value)."""

    cell: str
    seed: int
    count: int
    corpus_sha256: str
    generated: int = 0
    executed: int = 0
    steps: int = 0
    asserted: Counter[str] = field(default_factory=Counter)
    violated: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    outcomes: Counter[str] = field(default_factory=Counter)
    samples: list[dict[str, Any]] = field(default_factory=list)
    peak: int = 0
    retained_peak: int = 0
    seconds: float = 0.0
    finished: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "cell": self.cell,
            "seed": self.seed,
            "count": self.count,
            "corpus_sha256": self.corpus_sha256,
            "generated": self.generated,
            "executed": self.executed,
            "steps": self.steps,
            "asserted": dict(self.asserted),
            "violated": dict(self.violated),
            "errors": dict(self.errors),
            "outcomes": dict(sorted(self.outcomes.items())),
            "samples": self.samples,
            "traced_peak_bytes": self.peak,
            "retained_peak_bytes": self.retained_peak,
            "seconds": round(self.seconds, 1),
            "finished": self.finished,
        }


def _outcome(step: Step) -> str:
    if step.blocked_answer:
        return "blocked"
    if step.sent or step.shakes:
        return "sent"
    return f"other:{step.status}"


@contextlib.asynccontextmanager
async def fuzz_cell(
    name: str, make: Callable[[], contextlib.AbstractAsyncContextManager[Any]], memory_every: int = 16
) -> AsyncIterator[FuzzCell]:
    """The cell ``name`` made by ``make`` (``computer_cell``, ``server_cell``) under its history setting, its witness
    installed and its policy state started."""
    import tests.request_coverage as rc

    from latet.config import Config

    machine, transport, policy, history = parts(name)
    setting = "full" if history == "on" else "off"
    computer_config, server_config = rc.computer_config, rc.server_config

    def with_history(made: Callable[[str], Config]) -> Callable[[str], Config]:
        return lambda url: made(url).model_copy(update={"history": setting})

    with (
        rc.patched(rc, "computer_config", with_history(computer_config)),
        rc.patched(rc, "server_config", with_history(server_config)),
    ):
        async with make() as cell:
            runtime = await cell.runtime()
            recorder = runtime.gateway.recorder
            if recorder is None or recorder.history != setting:
                raise RuntimeError(f"the cell's history is not {setting!r} as its settings say")
            witness = Witness()
            with witnessing(witness, cell.transport):
                state = PolicyState(machine, transport, policy)
                await state.start(runtime)
                yield FuzzCell(name, cell, state, witness, memory_every)


async def fuzz(
    fuzz_cell_: FuzzCell, seed: int, count: int, deadline: float | None = None, samples: int = 20
) -> CellReport:
    """The ``count`` cases of the cell, in order, each judged: its report. Stops at ``deadline`` (``time.monotonic``),
    the report then not ``finished``."""
    cases = generate(seed, count, fuzz_cell_.name)
    whole = hashlib.sha256("".join(c.digest() for c in cases).encode()).hexdigest()
    report = CellReport(fuzz_cell_.name, seed, count, whole, generated=len(cases))
    began = time.monotonic()
    for index, case in enumerate(cases):
        if deadline is not None and time.monotonic() > deadline:
            break
        try:
            result = await run_case(fuzz_cell_, case, index)
        except Exception as exc:  # the harness or the product raised: a case not judged, said by its type
            report.executed += 1
            report.errors[type(exc).__name__] += 1
            if len(report.samples) < samples:
                report.samples.append({"case": case.descriptor(), "assertion": "error", "why": [type(exc).__name__]})
            with contextlib.suppress(Exception):
                await fuzz_cell_.state.reset(await fuzz_cell_.cell.runtime())
            continue
        report.executed += 1
        report.steps += len(result.steps)
        report.asserted.update(result.asserted)
        for step in result.steps:
            report.outcomes[_outcome(step)] += 1
            if _excepted_out(case, step):
                report.outcomes["sent-by-exception"] += 1
            if any(d6_permits(case, p, step) for p in case.plants) and step.sent:
                report.outcomes["sent-checked-only"] += 1
            report.peak = max(report.peak, step.peak or 0)
            report.retained_peak = max(report.retained_peak, step.retained or 0)
        for name, found in result.violations.items():
            if found:
                report.violated[name] += len(found)
                if len(report.samples) < samples:
                    report.samples.append({"case": case.descriptor(), "assertion": name, "why": found[:3]})
    report.seconds = time.monotonic() - began
    report.finished = report.executed == report.generated
    return report


# --- the command --------------------------------------------------------------------------------------------------


def _batches(cells: Sequence[str], size: int) -> list[list[str]]:
    """The cells in batches of ``size``, each of one machine (a batch is one pytest process of that machine)."""
    out: list[list[str]] = []
    for machine in MACHINES:
        mine = [c for c in cells if parts(c)[0] == machine]
        out += [mine[i : i + size] for i in range(0, len(mine), size)]
    return out


def run(args: argparse.Namespace) -> int:
    cells = list(args.cells or CELLS)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    expected = corpus(args.seed, args.count, cells)
    (out / "corpus.json").write_text(json.dumps(expected, sort_keys=True), "utf-8")
    failed = 0
    for batch in _batches(cells, args.batch):
        machine = parts(batch[0])[0]
        env = dict(
            os.environ,
            LATET_RCF_OUT=str(out),
            LATET_RCF_SEED=str(args.seed),
            LATET_RCF_COUNT=str(args.count),
            LATET_RCF_CELLS=",".join(batch),
            LATET_RCF_SECONDS=str(args.seconds),
            LATET_RCF_MEMORY_EVERY=str(args.memory_every),
        )
        if machine == "server":
            if not env.get("LATET_TEST_DATABASE_URL") or not env.get("LATET_TEST_VALKEY_URL"):
                print(f"{','.join(batch)}: the server's cells need LATET_TEST_DATABASE_URL and LATET_TEST_VALKEY_URL")
                failed += 1
                continue
            env["LATET_TEST_REQUIRE_SERVICES"] = "1"
        command = [sys.executable, "-m", "pytest", TESTS[machine], "-q", "-p", "no:cacheprovider", "-p", "no:randomly"]
        began = time.monotonic()
        found = subprocess.run(command, cwd=ROOT, env=env, timeout=args.seconds * len(batch) + 600)
        print(f"batch {','.join(batch)}: pytest exit {found.returncode} in {time.monotonic() - began:.0f} s")
        failed += found.returncode != 0
    return summarize(out, expected) or (1 if failed else 0)


def summarize(out: Path, expected: dict[str, Any] | None = None) -> int:
    """Every cell's report in ``out`` merged into ``summary.json``, a line a cell; 1 when any cell broke an assertion,
    did not finish, or ran another corpus than the one expected."""
    corpus_doc: dict[str, Any] = expected or json.loads((out / "corpus.json").read_text("utf-8"))
    summary: dict[str, Any] = {"schema": SCHEMA, "seed": corpus_doc["seed"], "count": corpus_doc["count"], "cells": {}}
    bad = 0
    for cell in corpus_doc["cells"]:
        path = out / f"{cell}.json"
        if not path.exists():
            print(f"{cell}: no report")
            bad += 1
            continue
        report = json.loads(path.read_text("utf-8"))
        same = report["corpus_sha256"] == corpus_doc["cells"][cell]["sha256"]
        violated = sum(report["violated"].values())
        errors = sum(report["errors"].values())
        asserted = sum(report["asserted"].values())
        summary["cells"][cell] = {
            k: report[k]
            for k in (
                "generated",
                "executed",
                "steps",
                "asserted",
                "violated",
                "errors",
                "outcomes",
                "traced_peak_bytes",
                "retained_peak_bytes",
                "seconds",
                "finished",
                "corpus_sha256",
                "samples",
            )
        }
        summary["cells"][cell]["corpus_matches"] = same
        print(
            f"{cell}: generated {report['generated']}, executed {report['executed']}, steps {report['steps']}, "
            f"asserted {asserted}, violated {violated}, errors {errors}, {report['outcomes']}, {report['seconds']} s"
            + ("" if same else " (ANOTHER CORPUS)")
            + ("" if report["finished"] else " (NOT FINISHED)")
        )
        bad += violated > 0 or errors > 0 or not report["finished"] or not same
    (out / "summary.json").write_text(json.dumps(summary, sort_keys=True, indent=1), "utf-8")
    return 1 if bad else 0


async def run_machine_cells(make: Callable[[str], contextlib.AbstractAsyncContextManager[Any]]) -> None:
    """What a test node of the script runs: the cells of ``LATET_RCF_CELLS``, each made by ``make(cell)``, their
    reports written to ``LATET_RCF_OUT``."""
    out = Path(os.environ["LATET_RCF_OUT"])
    seed, count = int(os.environ["LATET_RCF_SEED"]), int(os.environ["LATET_RCF_COUNT"])
    seconds = float(os.environ.get("LATET_RCF_SECONDS", "3600"))
    memory_every = int(os.environ.get("LATET_RCF_MEMORY_EVERY", "16"))
    for cell in os.environ["LATET_RCF_CELLS"].split(","):
        async with fuzz_cell(cell, lambda cell=cell: make(cell), memory_every) as made:
            report = await fuzz(made, seed, count, deadline=time.monotonic() + seconds)
        (out / f"{cell}.json").write_text(json.dumps(report.as_json(), sort_keys=True), "utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    made = commands.add_parser("corpus", help="write every cell's corpus (no run)")
    made.add_argument("--seed", type=int, required=True)
    made.add_argument("--count", type=int, default=COUNT)
    made.add_argument("--out", type=Path, required=True)
    ran = commands.add_parser("run", help="run the cells and judge every case")
    ran.add_argument("--seed", type=int, required=True)
    ran.add_argument("--count", type=int, default=COUNT)
    ran.add_argument("--out", type=Path, required=True)
    ran.add_argument("--cells", nargs="+", choices=CELLS)
    ran.add_argument("--batch", type=int, default=2, help="cells a pytest process (one machine's)")
    ran.add_argument("--seconds", type=float, default=3600.0, help="a cell's deadline")
    ran.add_argument("--memory-every", type=int, default=16, help="trace one case in N (0: none)")
    summed = commands.add_parser("summary", help="merge the reports of a run")
    summed.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    if args.command == "corpus":
        args.out.mkdir(parents=True, exist_ok=True)
        found = corpus(args.seed, args.count)
        (args.out / "corpus.json").write_text(json.dumps(found, sort_keys=True), "utf-8")
        for cell, part in found["cells"].items():
            print(f"{cell}: {part['generated']} cases, sha256 {part['sha256'][:16]}")
        return 0
    if args.command == "run":
        if args.count > SMOKE_MOST and not os.environ.get("LATET_RCF_FULL"):
            print(f"more than {SMOKE_MOST} cases a cell is the scheduled full run: set LATET_RCF_FULL=1 to run it")
            return 2
        return run(args)
    return summarize(args.out)


if __name__ == "__main__":
    sys.exit(main())
