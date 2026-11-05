"""P2-8's performance fixes: each faster path decides exactly as the reference it replaces, kept here alone."""

from __future__ import annotations

import json
import random
import sys
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from latet.gateway import body_coverage
from latet.protocols import codec, rewrite

from .test_request_outcomes import _mediator

# Units a JSON string token is made of where its end is hard to find: quotes, backslashes (an escape takes the unit
# after it, whatever it is), controls, a lone half of a surrogate pair, astral and multibyte characters.
_UNITS = [
    '"',
    "\\",
    "a",
    "z",
    " ",
    "é",
    "\N{LINE SEPARATOR}",
    "\N{CJK UNIFIED IDEOGRAPH-4E00}",
    "\ud800",
    "\udc00",
    "\U0001f600",
    "\x00",
    "\n",
    "\x7f",
]


def _reference_quoted_end(data: bytes | str, at: int) -> int | None:
    """The per-character scan ``body_coverage._quoted_end`` was before (P2-8): kept as the oracle of its regex."""
    quote = data[at]
    escape = 92 if isinstance(data, bytes) else "\\"
    at += 1
    while at < len(data):
        unit = data[at]
        if unit == quote:
            return at + 1
        at += 2 if unit == escape else 1
    return None


def _lines_in(function: Callable[..., Any], call: Callable[[], object]) -> int:
    """How many lines of ``function`` Python ran during ``call``: its own work, whatever runs in C beneath it."""
    code = function.__code__
    count = 0

    def local(frame: Any, event: str, arg: object) -> Any:
        nonlocal count
        if event == "line":
            count += 1
        return local

    def tracer(frame: Any, event: str, arg: object) -> Any:
        return local if frame.f_code is code else None

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        call()
    finally:
        sys.settrace(previous)
    return count


def _every_quote(data: bytes | str) -> list[int]:
    quote = 34 if isinstance(data, bytes) else '"'
    return [at for at, unit in enumerate(data) if unit == quote]


@given(st.lists(st.sampled_from(_UNITS), max_size=48), st.lists(st.sampled_from(_UNITS), max_size=6))
def test_quoted_end_regex_equals_the_per_character_scan(units: list[str], prefix: list[str]) -> None:
    text = "".join(prefix) + '"' + "".join(units)
    for data in (text, text.encode("utf-8", "surrogatepass")):
        for at in _every_quote(data):
            assert body_coverage._quoted_end(data, at) == _reference_quoted_end(data, at), (data, at)


def test_quoted_end_regex_equals_the_scan_on_long_and_edge_inputs() -> None:
    rng = random.Random(20261104)
    long = '"' + "".join(rng.choice(_UNITS) for _ in range(300_000))
    escapes = '"' + "\\\\" * 100_000 + '\\"' * 1000 + '"'
    cases = [
        long,
        escapes,
        '"' + "a" * 200_000,  # never closed
        '"abc\\',  # a backslash as its last unit
        '"\\',
        '"',
        '""',
        '"\\""',
        '"\\\\"',
        '"a\\\nb"',  # an escaped line break: the regex reads any unit after a backslash
    ]
    for text in cases:
        for data in (text, text.encode("utf-8", "surrogatepass")):
            for at in _every_quote(data)[:64]:
                assert body_coverage._quoted_end(data, at) == _reference_quoted_end(data, at), at


def test_quoted_end_does_no_python_work_per_character() -> None:
    """The closing quote of a long string is found in C: the Python lines run do not grow with its length."""
    for data in ('"' + "a\\n" * 10_000 + '"', ('"' + 'é\\"' * 10_000 + '"').encode()):
        lines = _lines_in(body_coverage._quoted_end, lambda data=data: body_coverage._quoted_end(data, 0))
        assert lines <= 8, lines


# --- the size a rewritten body is written to: an upper bound first, the exact count only past it ----------------------

# What a JSON document Latet writes anew can hold (no lone surrogate: what it reads is well formed, wellformed.read).
_LEAF_UNITS = [unit for unit in _UNITS if unit not in ("\ud800", "\udc00")] + ["\x1f", "\t", "\b"]
_texts = st.text(alphabet=st.sampled_from(_LEAF_UNITS), max_size=24)
_documents = st.recursive(
    st.none() | st.booleans() | st.integers(-(10**12), 10**12) | st.floats(allow_nan=False) | _texts,
    lambda children: st.lists(children, max_size=5) | st.dictionaries(_texts, children, max_size=5),
    max_leaves=24,
)


def _compact(document: Any) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()


def _refused(mediator: Any, document: Any, limit: int) -> bool:
    mediator.body_limit = limit
    try:
        mediator._serialize_body(document)
    except codec.DecodedTooLarge:
        return True
    return False


@given(st.recursive(_documents, lambda children: st.lists(children, max_size=3), max_leaves=8))
def test_json_bound_is_at_least_the_exact_size(document: Any) -> None:
    assert rewrite._json_bound(document) >= rewrite._json_size(document)


@pytest.fixture(scope="module")
def mediator() -> Any:
    made = _mediator()
    yield made
    made.close()


@given(_documents, st.integers(-3, 3))
def test_output_size_decides_as_the_exact_count_around_the_limit(mediator: Any, document: Any, delta: int) -> None:
    """The decision is the exact count's (a body larger than the limit refused, ``DecodedTooLarge``), at the limit and
    around it, and at the bound and around it, whatever the bound says first."""
    exact = rewrite._json_size(document)
    assert exact == len(_compact(document))
    for limit in (exact + delta, rewrite._json_bound(document) + delta):
        if limit >= 0:
            assert _refused(mediator, document, limit) is (exact > limit), (document, limit)


def test_output_size_counts_each_character_only_past_the_bound(mediator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A body whose bound is within the limit is written without counting its characters (most of a 1 MiB body's time
    went there); one whose bound passes it is counted exactly, and refused only when that count passes it too."""
    counted = []
    exact_size = rewrite._quoted_size

    def counting(text: str) -> int:
        counted.append(len(text))
        return exact_size(text)

    monkeypatch.setattr(rewrite, "_quoted_size", counting)
    document = {"model": "m", "messages": [{"role": "user", "content": "a" * 100_000}]}
    exact, bound = len(_compact(document)), rewrite._json_bound(document)
    assert bound > 5 * exact
    assert not _refused(mediator, document, bound)
    assert not counted, "a body within its bound had each of its characters counted"
    assert not _refused(mediator, document, exact) and counted
    counted.clear()
    assert _refused(mediator, document, exact - 1) and counted


# --- the final check's declared walk: what it screened is screened by what remains of the check ----------------------

_KEY = "latet_" + "lk_" + "Ab3_" * 10 + "XYw"  # a launch key's shape, as response_guard knows it
_TOOL = {"type": "function", "function": {"name": "query", "description": "Runs a query", "parameters": {
    "type": "object", "properties": {"sql": {"type": "string", "description": "the query"}}, "required": ["sql"]}}}  # fmt: skip
# Bodies whose every declared member kind holds a string the walk reads: texts, names, models, ids and references,
# metadata, stop texts, tool definitions and schemas, a tool call's input, fixed words.
_KINDS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "/openai/v1/chat/completions",
        {
            "model": "gpt-4o",
            "user": "user-1",
            "metadata": {"purpose": "tests"},
            "stop": ["END", "STOP"],
            "service_tier": "auto",
            "tools": [_TOOL],
            "tool_choice": "auto",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "query", "arguments": '{"sql":"x"}'}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "one row"},
            ],
        },
    ),
    (
        "/anthropic/v1/messages",
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 100,
            "system": [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}],
            "stop_sequences": ["END"],
            "tools": [
                {"name": "query", "description": "Runs a query", "input_schema": _TOOL["function"]["parameters"]}
            ],
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "query",
                            "input": {"sql": "x", "rows": [1, "two"]},
                        }
                    ],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "one row"}]},
            ],
        },
    ),
    (
        "/openai/v1/responses",
        {
            "model": "gpt-4o",
            "instructions": "Be brief.",
            "prompt_cache_key": "pck_0123456789abcdef01234567",
            "reasoning": {"effort": "low", "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
            "text": {"verbosity": "low"},
            "tools": [
                {
                    "type": "function",
                    "name": "query",
                    "description": "Runs a query",
                    "parameters": _TOOL["function"]["parameters"],
                }
            ],
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
                {"type": "function_call", "call_id": "call_1", "name": "query", "arguments": '{"sql":"x"}'},
                {"type": "function_call_output", "call_id": "call_1", "output": "one row"},
            ],
        },
    ),
    (
        "/gemini/v1beta/models/gemini-2.5-flash:generateContent",
        {
            "systemInstruction": {"parts": [{"text": "Be brief."}]},
            "generationConfig": {"temperature": 0.2},
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        },
    ),
)


def _corpus() -> list[tuple[str, str, dict[str, str], bytes]]:
    """Requests Latet rewrites: the kinds above, and the coverage table's bodies a computer sends over HTTP."""
    import urllib.parse

    from ..request_coverage import TABLE

    rows = [("POST", path, {"content-type": "application/json"}, json.dumps(body).encode()) for path, body in _KINDS]
    for case in TABLE:
        if case.transport == "http" and isinstance(case.body, bytes) and "computer" in case.machines and case.body:
            path = urllib.parse.unquote(case.target.split("?", 1)[0])
            rows.append((case.method, path, dict(case.headers), case.body))
    return rows


def _places(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[str, tuple[Any, ...]]]:
    """Every string of a document, as ("value", its path) or ("key", the path of the member it names)."""
    found: list[tuple[str, tuple[Any, ...]]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append(("key", (*path, key)))
            found.extend(_places(child, (*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_places(child, (*path, index)))
    elif isinstance(value, str):
        found.append(("value", path))
    return found


def _prepare(mediator: Any, method: str, path: str, headers: dict[str, str], raw: bytes) -> Any:
    from latet.gateway.mediate import REWRITE
    from latet.gateway.routes import Upstream

    family = path.strip("/").split("/")[0]
    upstream = Upstream(
        family, "/" + family, "https://api.example.invalid", envelope_expected=frozenset({("model", "m")})
    )
    prepared = mediator.prepare(upstream, method, path, headers, raw)
    if prepared.mode != REWRITE or prepared.block_reason or prepared.body_scan is None:
        if prepared.lease is not None:
            prepared.lease.release()
        return None
    return prepared


def _plant(prepared: Any, kind: str, path: tuple[Any, ...]) -> bool:
    """A launch key written at ``path`` of the prepared document, in place, as a reviewer's change is: a value, or the
    name of a member (renamed where it stands, every other member kept in its order); False where a slot stands on it."""
    holder: Any = prepared.document
    for step in path[:-1]:
        holder = holder[step]
    last = path[-1]
    if kind == "value":
        holder[last] = _KEY
        return True
    slots = {(id(slot.container), slot.key) for slot in prepared.body_scan.slots}
    slots |= {(id(work.ref.holder), work.ref.key) for work in prepared.images}
    if (id(holder), last) in slots:
        return False
    items = list(holder.items())
    holder.clear()
    holder.update((_KEY if key == last else key, child) for key, child in items)
    return True


def _assert_final_check_screens_what_the_walk_screened(mediator: Any) -> int:
    """For every string of every corpus request Latet rewrites, a launch key written there after masking: wherever the
    closed declaration's walk (``declared_body_safe``, which the final check ran before P2-8) finds it, the final check
    refuses the request, as a protected launch or a body refusal (never left to a detector's verdict)."""
    from latet.gateway.body_coverage import PROTECTED_LAUNCH, BodyRefused, Ledger, declared_body_safe

    caught = 0
    for method, path, headers, raw in _corpus():
        first = _prepare(mediator, method, path, headers, raw)
        if first is None:
            continue
        places = _places(first.document)
        if first.lease is not None:
            first.lease.release()
        for kind, place in places:
            prepared = _prepare(mediator, method, path, headers, raw)
            assert prepared is not None
            try:
                if not _plant(prepared, kind, place):
                    continue
                try:
                    walk = not declared_body_safe(
                        prepared.document,
                        prepared.protocol,
                        prepared.body_upstream,
                        prepared.body_scan,
                        Ledger(),
                        grants=prepared.body_grants,
                    )
                except BodyRefused:
                    walk = True
                if not walk:
                    continue
                caught += 1
                mediator._check(prepared)
                reason = prepared.block_reason or ""
                refused = reason == PROTECTED_LAUNCH or reason.startswith("request body refused")
                assert refused, (path, kind, place, reason)
            finally:
                if prepared.lease is not None:
                    prepared.lease.release()
    return caught


def test_final_check_screens_every_text_the_declared_walk_screened(mediator: Any) -> None:
    caught = _assert_final_check_screens_what_the_walk_screened(mediator)
    assert caught > 100, caught  # the corpus reaches the walk's every kind of member


def test_the_final_check_walks_the_declaration_once_not_twice(mediator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The final check of a rewritten request reads its body once (``inspect_body``): its texts are screened there, its
    slots by ``declared_scan_safe``; the declaration's separate walk is made before masking, where it must be (R-103)."""
    from latet.gateway import mediate

    walks = []
    walk = mediate.declared_body_safe

    def counted(*args: Any, **kwargs: Any) -> bool:
        walks.append(True)
        return walk(*args, **kwargs)

    method, path, headers, raw = _corpus()[0]
    prepared = _prepare(mediator, method, path, headers, raw)
    assert prepared is not None
    monkeypatch.setattr(mediate, "declared_body_safe", counted)
    try:
        mediator._check(prepared)
    finally:
        if prepared.lease is not None:
            prepared.lease.release()
    assert prepared.block_reason is None and not walks


def test_the_proof_fails_once_the_final_checks_text_screen_is_mutated(
    mediator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspect
    import textwrap

    from latet.gateway import mediate

    source = textwrap.dedent(inspect.getsource(mediate.Mediator._check))
    screen = "if any(not safe_text(text) for text in prepared.body_checked):"
    assert source.count(screen) == 1
    namespace = dict(vars(mediate))
    exec(source.replace(screen, "if False:"), namespace)
    monkeypatch.setattr(mediate.Mediator, "_check", namespace["_check"])
    with pytest.raises(AssertionError):
        _assert_final_check_screens_what_the_walk_screened(mediator)


_PLACES = ("text", "json text", "json document")


def _slot_written(prepared: Any, place: str) -> None:
    """A launch key written after masking where a slot of the rewritten request stands, as a late reviewer would."""
    from latet.protocols.base import JsonRef

    slots = [slot for slot, _, _ in prepared.pending]
    if place == "text":
        slot = next(s for s in slots if s.mode == "text" and s.get() == "hello")
        slot.put("hello and " + _KEY)
    elif place == "json text":
        slot = next(s for s in slots if s.mode == "json" and not isinstance(s, JsonRef))
        slot.put(json.dumps({"sql": _KEY}))
    else:
        slot = next(s for s in slots if isinstance(s, JsonRef))
        slot.container[slot.key]["properties"]["sql"]["description"] = _KEY


def _assert_final_check_refuses_a_key_in_each_slot(mediator: Any, places: tuple[str, ...] = _PLACES) -> None:
    """Review M-1: the final check's screen of the slots as they leave (``declared_scan_safe``), which the dropped walk
    relied on (it skipped the selected slots): a launch key in a text slot, in a JSON text and in a JSON document
    stops the request as a protected launch, there, before any detector."""
    from latet.gateway.body_coverage import PROTECTED_LAUNCH

    path, body = _KINDS[0]
    raw = json.dumps(body).encode()
    for place in places:
        prepared = _prepare(mediator, "POST", path, {"content-type": "application/json"}, raw)
        assert prepared is not None
        try:
            _slot_written(prepared, place)
            mediator._check(prepared)
            assert prepared.block_reason == PROTECTED_LAUNCH, (place, prepared.block_reason)
            assert prepared.document is None and prepared.body_proof is None  # discarded, nothing to send
        finally:
            if prepared.lease is not None:
                prepared.lease.release()


def test_the_final_check_refuses_a_launch_key_in_each_kind_of_slot(mediator: Any) -> None:
    _assert_final_check_refuses_a_key_in_each_slot(mediator)


def test_the_slot_screen_mutated_away_fails_that_test(mediator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect
    import textwrap

    from latet.gateway import mediate

    source = textwrap.dedent(inspect.getsource(mediate.Mediator._check))
    screen = "if not ledger.declared_scan_safe(scan):"
    assert source.count(screen) == 1
    namespace = dict(vars(mediate))
    exec(source.replace(screen, "if False:"), namespace)
    monkeypatch.setattr(mediate.Mediator, "_check", namespace["_check"])
    for place in ("text", "json text"):  # a JSON document's strings are read by the inspection too
        with pytest.raises(AssertionError):
            _assert_final_check_refuses_a_key_in_each_slot(mediator, (place,))


# --- the ledger's account: made again only when the body's shape changed since --------------------------------------

_EMAIL = "emily.carter@acme.com"
_MASKED_BODIES = {
    "leaves": ("/openai/v1/chat/completions", {"model": "gpt-4o", "messages": [{"role": "user", "content": _EMAIL}]}),
    "removed member": (
        "/openai/v1/responses",
        {"model": "gpt-4o", "input": "write to " + _EMAIL, "client_metadata": {"origin": "tests"}},
    ),
    "masked key": (
        "/anthropic/v1/messages",
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": "query", "input": {_EMAIL: "x"}}],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "one"}]},
            ],
        },
    ),
    "schema written anew": (
        "/openai/v1/chat/completions",
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "mail",
                        "parameters": {"type": "object", "description": "writes to " + _EMAIL, "properties": {}},
                    },
                }
            ],
        },
    ),
}


def _counted_accounts(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []
    account = body_coverage.Ledger.account

    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        return account(self, *args, **kwargs)

    monkeypatch.setattr(body_coverage.Ledger, "account", counted)
    return calls


@pytest.mark.parametrize(
    ("case", "again"), [("leaves", 0), ("removed member", 1), ("masked key", 1), ("schema written anew", 1)]
)
def test_the_final_check_accounts_again_only_a_body_whose_shape_changed(
    mediator: Any, monkeypatch: pytest.MonkeyPatch, case: str, again: int
) -> None:
    """Masking that changed leaf strings alone leaves every final path as the first account bound it: the final check
    reads that account. A member removed, a key masked, a document written anew there: accounted again."""
    path, body = _MASKED_BODIES[case]
    calls = _counted_accounts(monkeypatch)
    prepared = _prepare(mediator, "POST", path, {"content-type": "application/json"}, json.dumps(body).encode())
    assert prepared is not None and prepared.masked_count > 0
    try:
        assert _EMAIL.encode() not in prepared.body and prepared.body_proof is not None
        assert len(calls) == 1 + again, len(calls)
    finally:
        if prepared.lease is not None:
            prepared.lease.release()


def _ledger_state(prepared: Any) -> tuple[Any, ...]:
    ledger, proof = prepared.body_ledger, prepared.body_proof
    return (
        prepared.body,
        prepared.block_reason,
        dict(ledger.final),
        frozenset(ledger.active),
        set(ledger.generated),
        ledger.receipts,
        ledger.retained,
        proof is not None
        and (proof.input_count, proof.final_count, proof.retained_bytes, proof.receipts, sorted(proof.final_receipts)),
    )


def test_an_account_not_made_again_leaves_the_ledger_and_proof_it_would_have_made(
    mediator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coverage oracle's proof is the same whether the final check accounts again or reads the first account: the
    final paths, the active set, the generated entries, the receipts, the retained bytes and the proof minted."""
    rows = _corpus() + [
        ("POST", path, {"content-type": "application/json"}, json.dumps(body).encode())
        for path, body in _MASKED_BODIES.values()
    ]
    compared = 0
    for method, path, headers, raw in rows:
        read = _prepare(mediator, method, path, headers, raw)
        if read is None:
            continue
        with monkeypatch.context() as forced:
            forced.setattr(body_coverage.BodyFreeze, "same_shape", lambda self, root: False, raising=False)
            again = _prepare(mediator, method, path, headers, raw)
        assert again is not None
        try:
            assert _ledger_state(read) == _ledger_state(again), path
            compared += 1
        finally:
            for prepared in (read, again):
                if prepared.lease is not None:
                    prepared.lease.release()
    assert compared >= 30, compared


# --- the raw pre-pass: one tokenizer, the same ordinals, links and charges -------------------------------------------


def _reference_preflight(self: Any, data: bytes, *, projected: bool = False) -> None:
    """``Ledger.preflight`` as it was before P2-8 (a byte at a time), kept as the oracle of its tokenizer."""
    from latet.engine.wellformed import well_formed
    from latet.gateway.body_coverage import BodyRefused, RawOccurrence

    stack: list[list[Any]] = []
    at, length = 0, len(data)
    while at < length:
        byte = data[at]
        if byte in b" \t\r\n,:":
            at += 1
            continue
        if byte in b"}]":
            if not stack:
                raise BodyRefused("shape")
            stack.pop()
            at += 1
            continue
        begin = at
        key = bool(stack and stack[-1][0] == 123 and stack[-1][3])
        if byte in b"{[":
            if len(stack) >= self.limits.depth:
                raise BodyRefused("limit", limit=True)
            kind = "container"
            at += 1
        elif byte == 34:
            kind = "key" if key else "string"
            end = _reference_quoted_end(data, at)
            if end is None:
                raise BodyRefused("shape")
            at = end
        else:
            kind = "scalar"
            while at < length and data[at] not in b" \t\r\n,]}:":
                at += 1
            if at == begin or key:
                raise BodyRefused("shape")
        if projected:
            self._charge_shadow(1, 768)
            if stack and stack[-1][0] == 123:
                stack[-1][3] = not key
            if byte in b"{[":
                stack.append([byte, -1, 0, byte == 123])
            continue
        self.charge(1, 256 + (4 * (at - begin) if key else 0))
        ordinal = len(self.raw)
        if not stack:
            parent, member = -1, ("root", 0)
        else:
            frame = stack[-1]
            parent = frame[1]
            if key:
                name = well_formed(json.loads(data[begin:at]))
                frame[2], frame[3] = name, False
                member = ("key", name)
            else:
                member = ("value", frame[2])
                if frame[0] == 123:
                    frame[3] = True
                else:
                    frame[2] += 1
        self.raw.append(RawOccurrence(begin, at, kind))
        link = (parent, member)
        previous = self.links.get(link)
        if previous is not None and parent >= 0:
            self.charge(0, 96)
            self.shadowed[ordinal] = previous
        self.links[link] = ordinal
        if byte in b"{[":
            stack.append([byte, ordinal, 0, byte == 123])
    if stack:
        raise BodyRefused("shape")


def _preflight_outcome(data: bytes, limits: Any, projected: bool, run: Callable[..., None]) -> tuple[Any, ...]:
    ledger = body_coverage.Ledger(limits=limits, envelope=1)
    try:
        run(ledger, data, projected=projected)
        ending: Any = None
    except body_coverage.BodyRefused as error:
        ending = ("refused", error.code)
    except ValueError as error:  # a key json.loads refuses (a control character in it)
        ending = ("unreadable", type(error).__name__)
    return (ending, ledger.raw, ledger.links, ledger.shadowed, ledger.retained, ledger.shadow_work)


# JSON's tokens and what breaks them: openers and closers, quotes, escapes, separators, scalars, a control in a key.
_JSONISH = [b"{", b"}", b"[", b"]", b'"', b"\\", b'\\"', b":", b",", b" ", b"\n", b"a", b"1", b"-2.5e3", b"true"]
_JSONISH += [b"null", b'"k"', b'"v"', b'"\\u00e9"', b'"\\ud83d"', "é".encode(), b"\x00", b'"\x01"', b"{}", b"[]"]


@given(
    st.one_of(
        st.lists(st.sampled_from(_JSONISH), max_size=40).map(b"".join),
        _documents.map(lambda document: json.dumps({"k": document, "k ": [document, document]}).encode()),
        st.lists(st.sampled_from([b'"a"', b'"a"', b'"b"']), min_size=1, max_size=6).map(
            lambda keys: b"{" + b",".join(key + b":[1," + key + b"]" for key in keys) + b"}"
        ),
    ),
    st.sampled_from([None, (3, 10**6, 10**6), (64, 12, 10**6), (64, 10**6, 2048)]),
    st.booleans(),
)
def test_preflight_tokenizer_equals_the_byte_scan(data: bytes, bounds: Any, projected: bool) -> None:
    """The same raw ordinals, links, shadowed copies, retained bytes and shadow work, and the same refusal at the same
    point (depth, occurrences, bytes, shape, an unreadable key), as the byte-at-a-time scan it replaces."""
    from dataclasses import replace

    from latet.gateway.coverage import LIMITS

    limits = (
        LIMITS if bounds is None else replace(LIMITS, depth=bounds[0], occurrences=bounds[1], ledger_bytes=bounds[2])
    )
    new = _preflight_outcome(data, limits, projected, body_coverage.Ledger.preflight)
    old = _preflight_outcome(data, limits, projected, _reference_preflight)
    assert new == old, data


def test_preflight_tokenizer_equals_the_byte_scan_on_the_corpus() -> None:
    for _, _, _, raw in _corpus():
        for projected in (False, True):
            new = _preflight_outcome(raw, body_coverage.coverage.LIMITS, projected, body_coverage.Ledger.preflight)
            old = _preflight_outcome(raw, body_coverage.coverage.LIMITS, projected, _reference_preflight)
            assert new == old


def test_preflight_does_no_python_work_per_byte() -> None:
    """Spaces between tokens and a long number are read in C: the Python lines run grow with the tokens alone."""
    data = b'{"a":' + b" " * 20_000 + b"1" * 20_000 + b"," + b"\n" * 20_000 + b'"b":[' + b"2," * 50 + b"3]}"
    ledger = body_coverage.Ledger()
    lines = _lines_in(body_coverage.Ledger.preflight, lambda: ledger.preflight(data))
    assert len(ledger.raw) == 56 and lines < 40 * 56, lines


# A run of separators at a body's end (JSON allows trailing whitespace): matched once, never rescanned from each of its
# later bytes (a search for a token there cost the square of its length, under the interpreter's lock: review C-1).
_RUNS = [b" ", b"\t", b"\r", b"\n", b",", b":", b" \r\n\t,:"]
_HEADS = [b'{"model":"m","input":"hello"}', b'{"model":"m","input":["hello"', b'{"a":', b"", b"1"]


@pytest.mark.parametrize("size", [0, 1, 7, 4096, 64 * 1024])
@pytest.mark.parametrize("run", _RUNS, ids=[repr(run) for run in _RUNS])
def test_preflight_equals_the_byte_scan_before_a_trailing_run(size: int, run: bytes) -> None:
    """Open and closed documents, an empty and a scalar body, projected or not: the same outcome, ordinals, links and
    charges as the byte-at-a-time scan, whatever separators end them."""
    for head in _HEADS:
        data = head + (run * size)[:size]
        for projected in (False, True):
            new = _preflight_outcome(data, body_coverage.coverage.LIMITS, projected, body_coverage.Ledger.preflight)
            old = _preflight_outcome(data, body_coverage.coverage.LIMITS, projected, _reference_preflight)
            assert new == old, (head, size, run, projected)


@pytest.mark.parametrize("size", [64 * 1024, 1 << 20], ids=["64KiB", "1MiB"])
@pytest.mark.parametrize("run", [b" ", b",", b"\n"], ids=["space", "comma", "newline"])
def test_a_trailing_run_of_separators_is_read_in_linear_time(size: int, run: bytes) -> None:
    """Linear: a mebibyte of trailing separators is read in a few milliseconds (the scan that searched again from each
    of its bytes took about 3 s for 64 KiB and about 12 minutes for 1 MiB, the whole process held meanwhile)."""
    import time

    data = b'{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}' + run * size
    ledger = body_coverage.Ledger()
    started = time.perf_counter()
    ledger.preflight(data)
    elapsed = time.perf_counter() - started
    assert len(ledger.raw) == 10 and elapsed < 0.5, elapsed
    lines = _lines_in(body_coverage.Ledger.preflight, lambda: body_coverage.Ledger().preflight(data))
    assert lines < 40 * 10, lines


def test_the_quote_scan_is_linear_before_a_trailing_run() -> None:
    """The string scan, matched where its opening quote is (never searched again): an unterminated string followed by
    a mebibyte of separators or of escape pairs ends at once."""
    import time

    for tail in (b" " * (1 << 20), b"\\\\" * (1 << 19), b"\\" + b" " * (1 << 20), b'\\"' * (1 << 19)):
        data = b'"' + tail
        started = time.perf_counter()
        assert body_coverage._quoted_end(data, 0) is None
        assert body_coverage._quoted_end(data.decode("latin-1"), 0) is None
        assert time.perf_counter() - started < 0.5


# --- the recording of a rewritten request: refreshed once, after its final check -------------------------------------


def _counted_refreshes(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    from latet.gateway import mediate

    calls: list[bool] = []
    refresh = mediate.Prepared._refresh_recording

    def counted(self: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(True)
        refresh(self, *args, **kwargs)

    monkeypatch.setattr(mediate.Prepared, "_refresh_recording", counted)
    return calls


def test_a_rewritten_request_is_recorded_once(mediator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Its record (the positions of its selected texts, its blocks, its images) is made once, as it leaves the worker
    (``_mark_outbound``), never also as its arena is built before its final check, which made it again."""
    path, body = _MASKED_BODIES["leaves"]
    calls = _counted_refreshes(monkeypatch)
    prepared = _prepare(mediator, "POST", path, {"content-type": "application/json"}, json.dumps(body).encode())
    assert prepared is not None
    try:
        assert prepared.masked_count and prepared.body_proof is not None and prepared.record_blocks
        assert len(calls) == 1, len(calls)
    finally:
        if prepared.lease is not None:
            prepared.lease.release()


def test_a_rewritten_websocket_turn_is_recorded_once(mediator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counted_refreshes(monkeypatch)
    turn = json.dumps({"type": "response.create", "model": "gpt-4o", "input": "write to " + _EMAIL})
    sent, prepared = mediator.mask_ws_frame(turn, mediator.engine.session("speed-ws"))
    assert prepared is not None
    try:
        assert prepared.masked_count and _EMAIL not in sent and prepared.record_blocks
        assert len(calls) == 1, len(calls)
    finally:
        if prepared.lease is not None:
            prepared.lease.release()


# --- the seal: the body's shape walked once, in the worker; the event loop checks what it sealed ---------------------


def _decided(mediator: Any) -> tuple[Any, Any, Any]:
    from .test_request_outcomes import _chat, _envelope, _prepared, _strict

    body = _chat(f"write to {_EMAIL}")
    arena = _prepared(mediator, body)
    envelope = _envelope(body)
    return arena, envelope, _strict(arena, envelope)


def test_the_event_loop_walks_no_body_to_finalize_and_send(mediator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker walked the body's shape as it froze it (``freeze_inputs``); finalizing and the send guard, on the
    event loop every identity shares, check what that freeze sealed and never walk the document again."""
    from latet.gateway import outcome

    from .test_request_outcomes import _strict

    walks: list[bool] = []
    unchanged = body_coverage.BodyFreeze.unchanged

    def counted(self: Any) -> bool:
        walks.append(True)
        return unchanged(self)

    arena, envelope, _ = _decided(mediator)  # the worker's walk is made here, as it freezes
    arena.sealed = None
    monkeypatch.setattr(body_coverage.BodyFreeze, "unchanged", counted)
    decision = _strict(arena, envelope)
    assert outcome.sendable(decision, arena, envelope) is decision
    assert not walks, len(walks)


@pytest.mark.parametrize("mismatch", ["generation", "payload", "slot written", "proof"])
def test_the_send_guard_refuses_any_mismatch_with_what_was_sealed(mediator: Any, mismatch: str) -> None:
    from latet.gateway import outcome

    arena, envelope, decision = _decided(mediator)
    assert outcome.sendable(decision, arena, envelope) is decision
    if mismatch == "generation":
        arena.freeze_generation = object()  # a worker froze it again since
    elif mismatch == "payload":
        arena.body = bytes(bytearray(arena.body))  # the same bytes, another object than the one sealed
    elif mismatch == "slot written":
        slot = arena.pending[0][0]
        slot.put(slot.get() + " and " + _EMAIL)  # a late writer, with no refresh
    else:
        arena.body_proof = None
    with pytest.raises(outcome.NotSendable) as refused:
        outcome.sendable(decision, arena, envelope)
    assert refused.value.code in {"stale", "payload"}


def test_a_member_changed_before_the_worker_froze_it_is_refused(mediator: Any) -> None:
    """The one walk of the body's shape: a member the final check did not see (no slot) changed in place before the
    worker freezes the arena stops it there, never sent."""
    from .test_request_outcomes import _chat, _envelope, _strict

    body = _chat("hello")
    arena = _prepare(mediator, "POST", "/openai/v1/chat/completions", {"content-type": "application/json"}, body)
    assert arena is not None
    try:
        arena.document["model"] = "another-model"
        arena.refresh_recording()
        assert arena.block_reason == "request body changed after the final check; send the request again"
        decision = _strict(arena, _envelope(body))
        assert type(decision).__name__ == "Block"
    finally:
        if arena.lease is not None:
            arena.lease.release()


# --- a text sent again: its mark computed once ------------------------------------------------------------------------


def test_a_text_sent_again_is_marked_once(mediator: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether a text kept as it left may leave so (``_leaves_as_it_left``) and whether its history allows it
    (``sent before in this conversation``) read one keyed mark of it, computed once, not twice."""
    from latet.gateway import mediate

    texts = ["write to " + _EMAIL, "and again"]
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": text} for text in texts]}
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    first = _prepare(mediator, "POST", "/openai/v1/chat/completions", headers, raw)
    assert first is not None
    if first.lease is not None:
        first.lease.release()
    marks: list[bytes] = []
    keyed = mediate.hmac.new

    def counted(key: bytes, message: bytes, digest: Any) -> Any:
        if message[:2] == b"L\x00":
            marks.append(message)
        return keyed(key, message, digest)

    monkeypatch.setattr(mediate.hmac, "new", counted)
    again = _prepare(mediator, "POST", "/openai/v1/chat/completions", headers, raw)
    assert again is not None
    try:
        assert again.body == first.body and [done.original for done in again.masked] == texts
        assert len(marks) == len(texts), len(marks)
    finally:
        if again.lease is not None:
            again.lease.release()


# --- a request with nothing to check against the vault holds no call of it -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "envelope", "held"),
    [
        (b"", (), False),  # a bodyless GET with no envelope text: nothing a value learnt elsewhere could be found in
        (b"", ("x-title: Ada Lovelace",), True),  # its envelope's texts are checked: the vault caught up first
        (b'{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}', (), True),
    ],
    ids=["nothing", "envelope-text", "body"],
)
def test_only_a_request_with_something_to_check_holds_the_vault(
    mediator: Any, monkeypatch: pytest.MonkeyPatch, raw: bytes, envelope: tuple[str, ...], held: bool
) -> None:
    """On a server, the outermost hold of an API key's vault asks its store what other instances learnt since (a
    PostgreSQL round trip): a request with neither a body nor an envelope text has nothing to find it in, and skips it;
    any text to check, of its body or of its envelope, still takes it."""
    import contextlib

    from latet.gateway.routes import Upstream

    vault = mediator.engine.vault
    calls: list[bool] = []
    holding = type(vault).holding

    @contextlib.contextmanager
    def counted(self: Any, **kwargs: Any) -> Any:
        calls.append(True)
        with holding(self, **kwargs):
            yield

    monkeypatch.setattr(type(vault), "holding", counted)
    method, path = ("POST", "/openai/v1/chat/completions") if raw else ("GET", "/openai/v1/models")
    upstream = Upstream(
        "openai", "/openai", "https://api.example.invalid", envelope_expected=frozenset({("model", "m")})
    )
    prepared = mediator.prepare_http(upstream, method, path, {"content-type": "application/json"}, raw, envelope)
    try:
        assert prepared.block_reason is None and prepared.envelope_checked == envelope
        assert bool(calls) is held, len(calls)
    finally:
        if prepared.lease is not None:
            prepared.lease.release()
