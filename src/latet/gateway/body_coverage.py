"""Request-local occurrence accounting and closed body declarations.

The raw structural pass runs before JSON materialization. Existing protocol scans
remain the masking path; this module accounts for their residuals without granting
arbitrary dictionaries or interpreting a detector non-match as protocol authority.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from hashlib import sha256
from itertools import chain, product
from typing import Any, cast

from latet.engine.response_guard import safe_text
from latet.engine.wellformed import well_formed
from latet.gateway import coverage
from latet.gateway.body_selectors import BodyGrants, exception_rule, selectable
from latet.gateway.model_ids import body_model
from latet.gateway.routes import Upstream
from latet.protocols import gemini
from latet.protocols.anthropic import _PROVIDERS_BLOCKS
from latet.protocols.base import BodyScan, JsonRef, SlotRef
from latet.protocols.openai_responses import _PROVIDERS_ITEMS

# A live launch key where Latet cannot mask it (a request it only checks, a field it never rewrites): how to go on.
PROTECTED_LAUNCH = (
    "request contains a protected launch credential: a Latet launch key, from the agent's environment, sits where "
    "Latet cannot mask it, so it does not send the request; rewind the conversation to before the output that "
    "carries it, or start a new conversation"
)

D6 = "checked only"
M3 = "image remainder after text recognition"
HISTORY = "sent before in this conversation"
REPORTED = "reported as written by the model"
REMOVED = "removed before the send"
# Codex c1382380 core/src/client.rs:580-603 (prompt_cache_key: the session's ThreadId, or
# "{InternalSessionSource}:{parent}" or "guardian:{parent}"), as its session-id header.
_UUID = "[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
# Hermes Agent 25ce7ccf agent/transports/codex.py:428-449, 798-806: "pck_" + sha256[:24] of its static prefix, else
# its scope, the session id (hermes_state_ids.py:53-60).
_CACHE_KEY = re.compile(
    f"(?:(?:memory_consolidation|guardian):)?{_UUID}|pck_[0-9a-f]{{24}}|[0-9]{{8}}_[0-9]{{6}}_[0-9a-f]{{6}}"
)
# Codex's client_metadata (codex-api/src/common.rs:301, a HashMap<String, String>): bounded, then removed.
_METADATA_ENTRIES = 64
_METADATA_KEY = re.compile(r"[A-Za-z0-9_.-]{1,128}")
# A response id the provider issued (OpenAI's documented resp_ ids; Codex sends back the id it was answered with,
# core/src/client.rs:1996-2069, from an earlier connection too after a resume or a reconnect): never bound to one.
_RESPONSE_ID = re.compile(r"resp_[A-Za-z0-9]{1,128}")


_SAFE_CODES = frozenset({"coverage-limit", "body-unknown-member", "body-unknown-member-no-exception"})


class BodyRefused(ValueError):
    """A body refused, by its category (the kind of object or member, a fixed word of the declaration, never what the
    request named). ``unknown``: a member no profile names (D6), under its own safe reason code (``safe_code``), the
    object it was in said by its category alone; ``reachable``: whether an exact request exception could name it, which
    the code says (``body-unknown-member``, or ``body-unknown-member-no-exception`` inside a provider's item or an
    inline image's source, and in a WebSocket frame, which takes no exception in 0.11.0: P2-7 review I-8)."""

    def __init__(self, category: str, *, limit: bool = False, unknown: bool = False, reachable: bool = True) -> None:
        if limit:
            self.code = "coverage-limit"
        elif unknown:
            self.code = "body-unknown-member" if reachable else "body-unknown-member-no-exception"
        else:
            self.code = "body-" + category
        if limit:
            words = "request coverage limit exceeded"
        elif unknown:
            words = "request body refused (unknown member in " + category + ")"
        else:
            words = "request body refused (" + category + ")"
        super().__init__(words)

    @property
    def safe_code(self) -> str | None:
        """The safe reason code its refusal is recorded under (``outcome.BLOCK_CODES``); None for one recorded as
        ``refused``, its words alone saying why."""
        return self.code if self.code in _SAFE_CODES else None


@dataclass(frozen=True, slots=True)
class RawOccurrence:
    start: int
    end: int
    kind: str


# What follows a string token's opening quote, up to and with its closing one: runs of anything but a quote or a
# backslash, or a backslash and the unit after it, whatever that is. Possessive, so it never backtracks: one pass in C
# where a Python loop read each character (P2-8), the same end found (tests/unit/test_request_coverage_speed.py).
_QUOTED_END = re.compile(rb'(?:[^"\\]++|\\.)*+"', re.DOTALL)
_QUOTED_END_TEXT = re.compile(r'(?:[^"\\]++|\\.)*+"', re.DOTALL)
# The raw pre-pass's tokens, one match each after the separators before it (P2-8: one pass in C where a Python loop
# read each byte): an opener, a closer, a string (to its closing quote), a quote that never closes, or a scalar (from
# any other byte, up to a separator or a closer). Every byte is one of them, so the matches leave no gap. Each is
# matched where the last one ended (``match``, possessive: no backtracking), never searched for.
_TOKEN = re.compile(
    rb'[ \t\r\n,:]*+(?:([{\[])|([}\]])|("(?:[^"\\]++|\\.)*+")|(")|([^ \t\r\n,:\]}"{\[][^ \t\r\n,\]}:]*+))', re.DOTALL
)
_OPEN, _CLOSE, _STRING, _UNENDED = 1, 2, 3, 4
_SEPARATORS = re.compile(rb"[ \t\r\n,:]*+")


def _quoted_end(data: bytes | str, at: int) -> int | None:
    """The offset just past the closing quote of the string token whose opening quote is at ``at``; None for one that
    never ends (no closing quote, or a backslash as its last unit)."""
    found = _QUOTED_END.match(data, at + 1) if isinstance(data, bytes) else _QUOTED_END_TEXT.match(data, at + 1)
    return found.end() if found is not None else None


def _declared_quoted_safe(text: str, start: int, end: int) -> bool:
    try:
        unit = json.loads(text[start:end])
    except ValueError:
        return True  # the containing raw text is screened separately
    unit = well_formed(unit)
    return safe_text(unit)


def declared_string_safe(
    text: str, charge: Callable[[], None] | None = None, *, depth_limit: int | None = None
) -> bool:
    # Do not alter raw ordinals or links. Only one decoded token is live at a time.
    at, depth = 0, 0
    while at < len(text):
        unit = text[at]
        if unit in " \t\r\n,:":
            at += 1
            continue
        if unit in "]}":
            depth = max(0, depth - 1)
            at += 1
            continue
        if charge is not None:
            charge()
        if unit in "[{":
            if depth_limit is not None and depth >= depth_limit:
                raise BodyRefused("limit", limit=True)
            depth += 1
            at += 1
        elif unit == '"':
            end = _quoted_end(text, at)
            if end is None:
                break
            if not _declared_quoted_safe(text, at, end):
                return False
            at = end
        else:
            at += 1
            while at < len(text) and text[at] not in ' \t\r\n,:"{}[]':
                at += 1
    # Preserve the existing raw-text reading for malformed arguments.
    return safe_text(text)


@dataclass(slots=True, repr=False)
class Ledger:
    """One exchange's occurrences, including removed input and generated output.

    Raw links name a parent occurrence, so a later duplicate container cannot
    accidentally inherit an earlier container's descendants. Final paths map to
    raw ordinals one-to-one; changes reuse those entries, new positions charge
    once and removal never refunds the exchange's retained allowance.
    """

    limits: coverage.CoverageLimits = field(default_factory=lambda: coverage.LIMITS)
    raw: list[RawOccurrence] = field(default_factory=list)
    retained: int = 0
    envelope: int = 0
    declared_occurrences: int = 0
    shadow_work: int = 0
    shadow_bytes: int = 0  # body bytes its shadow projections read, cumulatively (``CoverageLimits.shadow_bytes``)
    final: dict[tuple[object, ...], tuple[int, str]] = field(default_factory=dict)
    generated: set[tuple[object, ...]] = field(default_factory=set)
    links: dict[tuple[int, object], int] = field(default_factory=dict)
    shadowed: dict[int, int] = field(default_factory=dict)
    declared_serialized: dict[tuple[int, object], bytes] = field(default_factory=dict)
    active: set[tuple[object, ...]] = field(default_factory=set)
    metadata_paths: set[tuple[object, ...]] = field(default_factory=set)
    initialized: bool = False
    receipts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.charge(0, self.envelope * 256)

    def charge(self, count: int, size: int) -> None:
        used = self.envelope + len(self.raw) + len(self.generated) + self.declared_occurrences + self.shadow_work
        if count + used > self.limits.occurrences:
            raise BodyRefused("limit", limit=True)
        if self.retained + size > self.limits.ledger_bytes:
            raise BodyRefused("limit", limit=True)
        self.retained += size

    def preflight(self, data: bytes, *, projected: bool = False) -> None:
        """Bound tokens and depth before constructing the JSON object tree."""
        # Stack: container kind, its raw ordinal, next array index / object key,
        # whether the next object string is a key. Syntax is also checked by read.
        stack: list[list[Any]] = []
        at, length = 0, len(data)
        while at < length:
            # Anchored where the last token ended, never searched for: a search tries again from each later byte,
            # which costs the square of a trailing run of separators (review C-1).
            token = _TOKEN.match(data, at)
            if token is None:
                # Only separators remain, as the alternatives cover every other byte; anything else is no JSON.
                if _SEPARATORS.fullmatch(data, at) is None:
                    raise BodyRefused("shape")
                break
            at, group = token.end(), token.lastindex or 0  # one group always matches
            if group == _CLOSE:
                if not stack:
                    raise BodyRefused("shape")
                stack.pop()
                continue
            begin = token.start(group)
            byte = data[begin]
            key = bool(stack and stack[-1][0] == 123 and stack[-1][3])
            if group == _OPEN:
                if len(stack) >= self.limits.depth:
                    raise BodyRefused("limit", limit=True)
                kind = "container"
            elif group == _STRING:
                kind = "key" if key else "string"
            elif group == _UNENDED:
                raise BodyRefused("shape")
            else:
                kind = "scalar"
                if key:
                    raise BodyRefused("shape")
            if projected:
                self._charge_shadow(1, 768)
                if stack and stack[-1][0] == 123:
                    stack[-1][3] = not key
                if byte in b"{[":
                    stack.append([byte, -1, 0, byte == 123])
                continue
            # Charge before retaining a decoded key or descriptor.
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

    def declared_slot_safe(self, slot: SlotRef, *, projection: bool = False) -> bool:
        """Screen only text units selected by the existing protocol scan."""
        if isinstance(slot, JsonRef):
            return self._declared_tree_safe(slot.container[slot.key])
        text = slot.get()
        if slot.mode != "json":
            return safe_text(text)
        if projection:
            return self._declared_string_safe(text)
        identity = id(slot.container), slot.key
        digest = sha256()
        for start in range(0, len(text), 16 * 1024):
            digest.update(text[start : start + 16 * 1024].encode("utf-8", "surrogatepass"))
        reading = digest.digest()
        previous = self.declared_serialized.get(identity)
        # The fingerprint affects charging only. Every actual value is screened,
        # including an unchanged value, so admission never depends on a hash match.
        if previous == reading:
            return declared_string_safe(text, depth_limit=self.limits.depth)
        if previous is None:
            self.charge(0, 160)
        if not self._declared_string_safe(text):
            return False
        self.declared_serialized[identity] = reading
        return True

    def declared_scan_safe(self, scan: BodyScan, *, projection: bool = False, masked: bool = False) -> bool:
        """No live launch key in what the scan selected. ``masked``: a request Latet rewrites, whose texts and tool
        input keys are masked, a live key in them a known secret masked like the bridge token (R-103): only what is
        never masked is screened, and what leaves is screened again once it is masked."""
        if masked:
            return all(safe_text(key) for holder in scan.confidential_keys for key in holder)
        return (
            all(
                self.declared_slot_safe(slot, projection=projection)
                for slot in scan.slots
                if slot.mode in ("text", "json")
            )
            and all(safe_text(key) for holder, _, _ in scan.keyed for key in holder)
            and all(safe_text(key) for holder in scan.confidential_keys for key in holder)
        )

    def _charge_shadow(self, count: int, size: int) -> None:
        self.charge(count, size)
        self.shadow_work += count

    def _charge_projected(self, size: int) -> None:
        """``size`` more body bytes read by a shadow projection, before it is made: refused past the request's ceiling
        (the fixed coverage limit), never given back."""
        if self.shadow_bytes + size > self.limits.shadow_bytes:
            raise BodyRefused("limit", limit=True)
        self.shadow_bytes += size

    def shadow_projections(self, data: bytes, stop: int) -> Iterator[dict[str, Any]]:
        """Yield one typed-reader input for every non-winning raw choice combination.

        The outer root is ordinal zero. Its links still address its own parents
        even after an embedded region reuses the root-link sentinel.
        """
        if not any(winner < stop for winner in self.shadowed):
            return
        # Child rows, choice lists and one product cursor are the retained index.
        self._charge_shadow(0, 256 * stop)
        children: dict[int, list[tuple[object, int]]] = {}
        groups: dict[int, list[int]] = {}
        for (parent, link), winner in self.links.items():
            if not 0 <= parent < stop:
                continue
            role, member = cast(tuple[str, object], link)
            if role != "value":
                continue
            children.setdefault(parent, []).append((member, winner))
            earlier = self.shadowed.get(winner)
            if earlier is not None:
                choices = groups[winner] = [winner]
                while earlier is not None:
                    choices.append(earlier)
                    earlier = self.shadowed.get(earlier)
        if not groups:
            return

        def materialize(ordinal: int, picked: dict[int, int]) -> Any:
            entry = self.raw[ordinal]
            if entry.kind == "container":
                rows = children.get(ordinal)
                if rows is None:
                    return {} if data[entry.start] == 123 else []
                if data[entry.start] == 123:
                    return {member: materialize(picked.get(child, child), picked) for member, child in rows}
                return [materialize(picked.get(child, child), picked) for _, child in rows]
            value = json.loads(data[entry.start : entry.end])
            return well_formed(value) if isinstance(value, str) else value

        combinations = product(*groups.values())
        for choice in combinations:
            # Even a choice in an inactive branch consumes the full upper
            # bound. No projection or typed scan begins on exhausted budget.
            # Its bytes are a reservation (P2-8): held while it is materialized and read, then given back, with what
            # its reading charged (an embedded body's precharge: a projected pre-pass retains no entry), so that the
            # ledger retains its entries, links and index and one live projection at most. Its work counts on,
            # cumulatively, against the occurrence limit, and the whole body it reads against the bytes all of them
            # may read (review M-2).
            self._charge_projected(len(data))
            retained = self.retained
            self._charge_shadow(stop, 768 * stop + 12 * len(data))
            try:
                picked = dict(zip(groups, choice, strict=True))
                projected = materialize(0, picked)
                if not isinstance(projected, dict):
                    raise BodyRefused("shadow shape")
                yield projected
                del projected
            finally:
                self.retained = retained

    def projected_embedded(self, data: bytes) -> None:
        """Precharge a decoded alternative before strict syntax/object materialization."""
        self._charge_projected(len(data))
        self._charge_shadow(0, 12 * len(data))
        self.preflight(data, projected=True)

    def _declared_tree_safe(self, value: Any) -> bool:
        # The outer preflight already counted these occurrences.
        stack: list[Iterator[Any]] = [iter((value,))]
        while stack:
            try:
                item = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            if isinstance(item, str):
                if not safe_text(item):
                    return False
            elif isinstance(item, (dict, list)):
                if len(stack) > self.limits.depth:
                    raise BodyRefused("limit", limit=True)
                stack.append(chain.from_iterable(item.items()) if isinstance(item, dict) else iter(item))
        return True

    def _declared_string_safe(self, text: str) -> bool:
        def charge() -> None:
            self.charge(1, 0)
            self.declared_occurrences += 1

        return declared_string_safe(text, charge, depth_limit=self.limits.depth)

    def embedded(self, data: bytes) -> None:
        """A declared base64 wrapper and its JSON share the same retained budget."""
        outer = len(self.raw)
        self.preflight(data)
        for ordinal in range(outer):
            path = (("encoded-wrapper", ordinal),)
            self.charge(0, 160)
            self.metadata_paths.add(path)
            self.final[path] = (ordinal, "pending")

    def account(self, document: Any, *, initial: bool = False) -> BodyFreeze:
        """Bind every final path of ``document`` to its raw ordinal (or charge it as generated); returns the shape it
        accounted (``BodyFreeze.same_shape``), whose final paths a body of that very shape has still."""
        self.active = set(self.metadata_paths)
        self.receipts = ()
        nodes: list[tuple[Any, tuple[Any, ...] | None, tuple[Any, ...]]] = []

        def bind(path: tuple[object, ...], parent: int, member: object) -> int:
            self.active.add(path)
            found = self.final.get(path)
            if found is not None:
                return found[0]
            origin = self.links.get((parent, member)) if parent < len(self.raw) else None
            self.charge(int(origin is None), 160 + 8 * len(path))
            if origin is None:
                origin = len(self.raw) + len(self.generated)
                self.generated.add(path)
            self.final[path] = (origin, "pending")
            return origin

        def walk(value: Any, path: tuple[object, ...], parent: int, member: object, depth: int) -> None:
            if depth > self.limits.depth:
                raise BodyRefused("limit", limit=True)
            origin = bind(path, parent, member)
            if isinstance(value, dict):
                nodes.append((value, tuple(value), tuple(value.values())))
                for key, child in value.items():
                    bind((*path, ("key", key)), origin, ("key", key))
                    walk(child, (*path, key), origin, ("value", key), depth + 1)
            elif isinstance(value, list):
                nodes.append((value, None, tuple(value)))
                for index, child in enumerate(value):
                    walk(child, (*path, index), origin, ("value", index), depth + 1)

        walk(document, (), -1, ("root", 0), 0)
        self.initialized = True
        return BodyFreeze(tuple(nodes))

    def metadata(self, image: int, kind: str, index: int, size: int) -> None:
        path = (("image", image), kind, index)
        if path in self.final:
            return
        self.charge(1, 256 + size)
        ordinal = len(self.raw) + len(self.generated)
        self.generated.add(path)
        self.metadata_paths.add(path)
        self.active.add(path)
        self.final[path] = (ordinal, "pending")

    def finish(self) -> None:
        """Only a successful complete closed declaration walk may seal receipts.

        Text coverage here remains conditional on the final verifier; Prepared
        only mints a BodyProof after that same verifier succeeds.
        """
        receipts = ["removed"] * (len(self.raw) + len(self.generated))
        for path in self.active:
            ordinal, _ = self.final[path]
            if receipts[ordinal] != "removed":
                raise BodyRefused("nonunique mapping")
            receipts[ordinal] = "covered"
            self.final[path] = (ordinal, "covered")
        self.receipts = tuple(receipts)


@dataclass(frozen=True, slots=True, repr=False)
class BodyFreeze:
    """Private arena metadata, made only by the worker; never placed in a proof."""

    nodes: tuple[tuple[Any, tuple[Any, ...] | None, tuple[Any, ...]], ...]

    def unchanged(self) -> bool:
        for holder, keys, values in self.nodes:
            if len(holder) != len(values):
                return False
            if keys is None:
                if any(now is not was for now, was in zip(holder, values, strict=True)):
                    return False
            elif any(now is not was for now, was in zip(holder, keys, strict=True)) or any(
                holder[key] is not was for key, was in zip(keys, values, strict=True)
            ):
                return False
        return True

    def same_shape(self, root: Any) -> bool:
        """Whether ``root`` is the very document frozen, each of its containers the one frozen with the same keys in the
        same order, a container (that very one) where one was and a leaf where one was: the same final paths, whatever
        its leaves say now (masking changes leaf strings alone; a member removed, a key masked, a document written anew
        in its place, an image withheld change the shape)."""
        if not self.nodes:
            return not isinstance(root, (dict, list))
        if self.nodes[0][0] is not root:
            return False
        for holder, keys, values in self.nodes:
            if len(holder) != len(values) or (keys is not None and tuple(holder) != keys):
                return False
            for now, was in zip(holder.values() if keys is not None else holder, values, strict=True):
                # A child changed since is a leaf that was a leaf; an unchanged container is checked as its own node.
                if now is not was and (isinstance(now, (dict, list)) or isinstance(was, (dict, list))):
                    return False
        return True


def freeze_body(document: Any) -> BodyFreeze:
    nodes = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            nodes.append((value, tuple(value), tuple(value.values())))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            nodes.append((value, None, tuple(value)))
            for child in value:
                walk(child)

    walk(document)
    return BodyFreeze(tuple(nodes))


@dataclass(slots=True, repr=False)
class ContentEpoch:
    token: object = field(default_factory=object)


@dataclass(frozen=True, slots=True, repr=False)
class BodyProof:
    raw: bytes | str
    payload: bytes | str
    generation: object
    input_count: int
    final_count: int
    retained_bytes: int
    rules: tuple[str, ...]
    checked: tuple[str, ...]
    profile: str
    content_token: object
    receipts: tuple[str, ...]
    final_receipts: tuple[int, ...]
    authority: tuple[str, ...]

    def proves(self, envelope: coverage.Envelope) -> bool:
        return (
            self.raw is envelope.body
            and self.input_count > 0
            and self.final_count > 0
            and len(self.receipts) >= self.input_count
            and len(self.final_receipts) == self.final_count
            and len(set(self.final_receipts)) == self.final_count
            and all(state in {"removed", "covered"} for state in self.receipts)
            and all(
                0 <= entry < len(self.receipts) and self.receipts[entry] == "covered" for entry in self.final_receipts
            )
        )


@dataclass(slots=True, repr=False)
class BodyInspection:
    checked: list[str] = field(default_factory=list)
    rules: set[str] = field(default_factory=set)
    semantic: list[str] = field(default_factory=list)
    problem: str | None = None
    # Declared removable members found, as (their holder, their key): the caller removes them before the send.
    removed: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    # The exceptions whose body selectors admitted an undeclared member (body_selectors): each is named in ``rules``.
    exceptions: set[str] = field(default_factory=set)
    # The shape the ledger accounted in this reading (None: an account read again, ``inspect_body``'s ``accounted``).
    shape: BodyFreeze | None = None


def cancel_fields(frame: dict[str, Any], ledger: Ledger) -> tuple[str, ...]:
    """Closed WS-only control declaration; correlation is checked text, never target authority."""
    if frame.get("type") != "response.cancel" or not frame.keys() <= {"type", "event_id", "response_id"}:
        raise BodyRefused("control")
    checked = []
    for key in ("event_id", "response_id"):
        if key not in frame:
            continue
        value = frame[key]
        if not isinstance(value, str) or not value or len(value) > 512:
            raise BodyRefused("control id")
        try:
            if len(value.encode("utf-8")) > 2048:
                raise BodyRefused("control id")
        except UnicodeError:
            raise BodyRefused("control id") from None
        if key == "response_id" and not _ID.fullmatch(value):
            raise BodyRefused("control id")
        checked.extend((key, value))
    ledger.account(frame, initial=True)
    ledger.finish()
    return tuple(checked)


# Types are versioned code declarations, never user-configurable patterns (model ids: model_ids).
_ID = re.compile(
    r"(?:call_|toolu_|msg_|resp_|item_|fc_|rs_|comp_|cmp_|srvtoolu_|mcp_|ws_|fs_|ig_|ci_|cu_|ap_|ml_|ar_|ts_)[A-Za-z0-9_-]{1,128}\Z"
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}\Z")
# Gemini CLI v0.66.0 packages/core/src/core/turn.ts:470-485 (handlePendingFunctionCall): a call the model sent with no
# id gets f"{name}__{name}_{Date.now()}_{counter}", set on the functionCall kept in the history and echoed by its
# functionResponse (utils/generateContentResponseUtilities.ts:24-30). Its text is the tool's own name, twice, and
# digits; on Gemini's protocol alone.
_GEMINI_CLI_ID = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_.:-]{0,63})__(?P=name)_[0-9]{13}_[0-9]{1,6}\Z")
# With context management on, turn.ts:476-477: f"{name}__synth_{prompt_id}_{Date.now()}_{counter}"; the prompt id is
# the session's UUID with "########" and the prompt's count (cli/ui/hooks/useGeminiStream.ts:1885), a subagent's UUID
# with "#" and its turn (core/agents/local-executor.ts:330, 348), or the bare session id headless (cli/gemini.tsx:985).
_GEMINI_CLI_SYNTH = re.compile(
    rf"[A-Za-z_][A-Za-z0-9_.:-]{{0,63}}__synth_{_UUID}(?:#{{8}}[0-9]{{1,6}}|#[0-9]{{1,6}})?_[0-9]{{13}}_[0-9]{{1,6}}\Z"
)
# The model's own id (FunctionCall.id, the provider's): a bounded token, bare as a GenAI client sends it back, or after
# the tool's name as Gemini CLI keeps it (turn.ts:480-482).
_GEMINI_MODEL_ID = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.:-]{0,63}__)?[A-Za-z0-9_-]{1,128}\Z")
# Chat Completions tool call ids OpenAI-compatible providers issue, each its own form:
_CHAT_PROVIDER_IDS = (
    re.compile(r"[a-zA-Z0-9]{9}\Z"),  # Mistral: mistral-common 5bd2a815 protocol/instruct/validator.py:50
    re.compile(r"[0-9A-Za-z]{32}\Z"),  # llama.cpp's server: gen_tool_call_id, 7071ba37 server-common.cpp:168-188
    # vLLM 81a28794 renderers/chat_utils/tool_call.py:27-32: random_uuid() is 16 hex digits now (utils/__init__.py:
    # 11-12), uuid4().hex in earlier releases; and Kimi K2's f"functions.{name}:{index}", as Moonshot's API names them.
    re.compile(r"chatcmpl-tool-(?:[0-9a-f]{16}|[0-9a-f]{32})\Z"),
    re.compile(r"functions\.[A-Za-z_][A-Za-z0-9_.-]{0,63}:[0-9]{1,6}\Z"),
)


def _provider_id(protocol: str, value: str) -> bool:
    """A correlation id in the form its provider or agent makes, on that protocol alone."""
    if _ID.fullmatch(value):
        return True
    if protocol == "gemini":
        return any(grammar.fullmatch(value) for grammar in (_GEMINI_CLI_ID, _GEMINI_CLI_SYNTH, _GEMINI_MODEL_ID))
    if protocol == "chat":
        return any(grammar.fullmatch(value) for grammar in _CHAT_PROVIDER_IDS)
    return False


class _Body:
    def __init__(
        self,
        protocol: str,
        upstream: Upstream,
        scan: BodyScan,
        verify: bool,
        ledger: Ledger,
        opaque: dict[tuple[int, str], str] | None = None,
        *,
        declarations: bool = False,
        projection: bool = False,
        grants: BodyGrants | None = None,
        root: Any = None,
        excepted: bool = True,
        cache_create: bool = False,
    ) -> None:
        self.protocol, self.upstream, self.verify, self.ledger = protocol, upstream, verify, ledger
        self.cache_create = cache_create  # a Gemini cache's creation: its model may be named models/{id}
        self.result = BodyInspection()
        self.selected = {(id(slot.container), slot.key) for slot in scan.slots}
        self.json_roots = {id(slot.container[slot.key]) for slot in scan.slots if isinstance(slot, JsonRef)}
        self.declared: set[str] = set()
        self.references: list[str] = []
        # The body continues a response (a previous_response_id its rule admits): a reference may name a call that
        # response made, which the body does not hold (``request``'s end).
        self.continued = False
        self.provider_depth = 0
        self.opaque = opaque or {}
        self.declarations = declarations
        self.projection = projection
        # An exception's body selectors, and the document they point into: a holder's path is found from its top.
        self.grants = grants
        self.excepted = excepted  # whether a request exception may apply to this body at all (never a frame's)
        self.root = root
        self.parents: dict[int, tuple[int, str | int]] | None = None

    def text(self, value: Any, *, semantic: bool = False) -> None:
        if not isinstance(value, str):
            raise BodyRefused("shape")
        if self.declarations:
            if not safe_text(value):
                raise ProtectedLaunch()
            return
        self.result.checked.append(value)
        if semantic:
            self.result.semantic.append(value)

    def _model_id(self, value: str) -> str:
        """The model id a body names: the GenAI SDK names the model of a cache it creates as ``models/{id}`` (its
        t_caches_model), the same grammar after the prefix, on Gemini's cache creation alone (P2-8a, ruling 4)."""
        if self.cache_create and self.protocol == "gemini" and value.startswith("models/"):
            return value[len("models/") :]
        return value

    def member(self, holder: dict[str, Any], key: str, rule: Any) -> None:
        value = holder[key]
        if rule == "text":
            if not isinstance(value, str):
                raise BodyRefused("shape")
            if (id(holder), key) not in self.selected:
                self.text(value)
        elif rule == "name":
            self.text(value, semantic=True)
            if not self.declarations and not _NAME.fullmatch(value):
                self.result.problem = "request body refused (semantic name)"
        elif rule == "model":
            self.text(value, semantic=True)
            if (
                not self.declarations
                and not body_model(self.protocol, self.upstream.name, self._model_id(value))
                and ("model", value) not in self.upstream.envelope_expected
            ):
                self.result.problem = "request body refused (model resource)"
        elif rule in {"cache key", "response id"}:
            if not isinstance(value, str):
                raise BodyRefused(key)
            self.text(value, semantic=True)
            grammar = _CACHE_KEY if rule == "cache key" else _RESPONSE_ID
            if not self.declarations and not grammar.fullmatch(value):
                self.result.problem = "request body refused (" + key + ")"
            elif rule == "response id" and not self.declarations:
                self.continued = True
        elif rule == "removable":
            # Never forwarded, so never read as text: a value outside the declared shape refuses, naming the member.
            if (
                not isinstance(value, dict)
                or len(value) > _METADATA_ENTRIES
                or not all(
                    isinstance(name, str) and _METADATA_KEY.fullmatch(name) and isinstance(item, str)
                    for name, item in value.items()
                )
            ):
                raise BodyRefused(key)
            if self.declarations:
                return
            if self.verify:  # a verify-only profile forwards the bytes it read: nothing can be removed there
                raise BodyRefused(key)
            self.result.removed.append((holder, key))
            self.result.rules.add(REMOVED)
        elif rule in ("id", "reference"):
            self.text(value, semantic=True)
            if not self.declarations and not _provider_id(self.protocol, value):
                self.result.problem = "request body refused (correlation identifier)"
            if not self.declarations:
                if rule == "reference":
                    self.references.append(value)
                else:
                    self.declared.add(value)
        elif rule == "resource":
            self.text(value, semantic=True)
            if not self.declarations and (key, value) not in self.upstream.envelope_expected:
                self.result.problem = "request body refused (external resource)"
        elif rule == "opaque":
            if self.declarations:
                return
            admitted = self.opaque.get((id(holder), key))
            if not isinstance(value, str) or len(value) > 1 << 20 or admitted is None:
                raise BodyRefused("provider item")
            self.result.rules.add(admitted)
        elif rule == "bool":
            if type(value) is not bool:
                raise BodyRefused("control")
        elif rule == "number":
            if type(value) not in (int, float) or (
                not self.declarations and (not -1e9 <= value <= 1e9 or not math.isfinite(value))
            ):
                raise BodyRefused("control")
        elif rule == "system":
            if isinstance(value, str):
                self.member(holder, key, "text")
            else:
                self.content(value)
        elif rule == "input":
            if isinstance(value, str):
                self.member(holder, key, "text")
            else:
                self.listed(value, self.item)
        elif rule == "data":
            self.data(value, schema=False)
        elif rule == "schema":
            self.data(value, schema=True)
        elif rule == "metadata":
            if not isinstance(value, dict):
                raise BodyRefused("metadata")
            for name, content in value.items():
                self.text(name)
                if not isinstance(content, str):
                    raise BodyRefused("metadata")
                if (id(value), name) not in self.selected:
                    self.text(content)
        elif rule == "texts":
            if isinstance(value, str):
                self.member(holder, key, "text")
            elif isinstance(value, list) and all(isinstance(part, str) for part in value):
                for index, part in enumerate(value):
                    if (id(value), index) not in self.selected:
                        self.text(part)
            else:
                raise BodyRefused("text input")
        elif isinstance(rule, frozenset):
            if self.declarations:
                self.text(value)
            elif not isinstance(value, str) or value not in rule:
                raise BodyRefused("control")
        elif rule == self.content and isinstance(value, str):
            self.member(holder, key, "text")
        elif callable(rule):
            rule(value)
        else:
            raise BodyRefused("shape")

    def obj(self, value: Any, fields: dict[str, Any], category: str, *, provider: bool = False) -> None:
        if not isinstance(value, dict):
            raise BodyRefused(category)
        self.provider_depth += int(provider)
        try:
            for key, item in value.items():
                rule = fields.get(key)
                if rule is None:
                    if (
                        (self.verify or self.declarations)
                        and not self.provider_depth
                        and (
                            isinstance(item, str) or (isinstance(item, list) and all(isinstance(x, str) for x in item))
                        )
                    ):
                        self.text(key)
                        for text in [item] if isinstance(item, str) else item:
                            self.text(text)
                        if not self.declarations:
                            self.result.rules.add(D6)
                    elif self._granted(value, key, item):
                        continue
                    elif not self.declarations:
                        # No exception's selector reaches a provider item or a frame (``_granted``, F-2), nor a
                        # member whose name its grammar refuses (``selectable``, re-review R-1).
                        raise BodyRefused(category, unknown=True, reachable=self._reachable(value, key))
                else:
                    self._read(self.member, value, key, rule)
        finally:
            self.provider_depth -= int(provider)

    def _reachable(self, holder: dict[str, Any], key: str) -> bool:
        """Whether an exact exception could name the member ``key`` of ``holder``: a body exceptions may apply to,
        outside a provider's item, at a place the selector grammar can name."""
        if not self.excepted or self.provider_depth:
            return False
        path = self._path(holder)
        return path is not None and selectable((*path, key))

    def _granted(self, holder: dict[str, Any], key: str, item: Any) -> bool:
        """An undeclared member that an exception's selectors name, every key and value of it with the kind named
        (body_selectors): its keys and values are checked by the final check, never masked, and the exception is named.
        Never inside a provider item: what the profile refuses by its kind stays refused."""
        if not self.grants or self.provider_depth:
            return False
        path = self._path(holder)
        if path is None:
            return False
        admitted = self.grants.admits((*path, key), item)
        if admitted is None:
            return False
        self.text(key)
        for text in admitted.texts:
            self.text(text)
        if not self.declarations:
            self.result.exceptions.update(admitted.exceptions)
            self.result.rules.update(exception_rule(exception) for exception in admitted.exceptions)
        return True

    def _path(self, holder: Any) -> tuple[str | int, ...] | None:
        """A holder's place in the document, as object keys and array indexes from its top; None for one outside it.
        The links are made once per walk, by identity, one per container (the ledger has already bounded them)."""
        if self.parents is None:
            parents: dict[int, tuple[int, str | int]] = {}
            pending: list[tuple[Any, int, str | int]] = [(self.root, -1, "")]
            while pending:
                value, parent, step = pending.pop()
                if isinstance(value, dict):
                    parents[id(value)] = (parent, step)
                    pending.extend((child, id(value), name) for name, child in value.items())
                elif isinstance(value, list):
                    parents[id(value)] = (parent, step)
                    pending.extend((child, id(value), index) for index, child in enumerate(value))
            self.parents = parents
        at = id(holder)
        if at not in self.parents:
            return None
        steps: list[str | int] = []
        for _ in range(self.ledger.limits.depth + 1):  # deeper, or a container met twice, is no place in it
            parent, step = self.parents[at]
            if parent == -1:
                return tuple(reversed(steps))
            steps.append(step)
            at = parent
        return None

    def _read(self, reader: Callable[..., None], *values: Any) -> None:
        if not self.declarations or self.projection:
            reader(*values)
            return
        # An unsupported role is not granted strict authority here. Its
        # recognized siblings still receive their confidentiality check.
        with suppress(BodyRefused):
            reader(*values)

    def listed(self, value: Any, reader: Any) -> None:
        if not isinstance(value, list):
            raise BodyRefused("shape")
        for item in value:
            self._read(reader, item)

    def data(self, value: Any, *, schema: bool) -> None:
        # Data payloads are declared textual JSON slots, not arbitrary provider objects. On a route Latet only checks,
        # a string the scan selected (a tool call's input, string by string) is checked once, as its slot, with what
        # it is and who wrote it: checked again here as text of no kind, the model's own words would stop the request.
        pending: list[tuple[Any, bool, int, Any]] = [(value, False, 0, None)]
        while pending:
            item, names, holder, at = pending.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    self.text(key, semantic=schema and names)
                    names_below = schema and key in {"properties", "definitions", "$defs", "required"}
                    pending.append((child, names_below, id(item), key))
            elif isinstance(item, list):
                pending.extend((child, names, id(item), index) for index, child in enumerate(item))
            elif isinstance(item, str):
                if self.verify and not self.declarations and (holder, at) in self.selected:
                    continue
                self.text(item, semantic=schema and names)
            elif item is not None and type(item) not in (bool, int, float):
                raise BodyRefused("data")
            elif isinstance(item, int | float) and not isinstance(item, bool):
                if isinstance(item, float) and not math.isfinite(item):
                    raise BodyRefused("data")
                self.text(str(item))

    def tool_choice(self, value: Any) -> None:
        """Chat Completions' and Responses' tool_choice (Codex 0.162.0 sends "auto", client.rs:1000): a mode, or one
        named function or custom tool, or one hosted tool type; a value outside refuses naming the member."""
        if isinstance(value, str):
            if value not in {"auto", "none", "required"}:
                raise BodyRefused("tool_choice")
            if self.declarations:
                self.text(value)
            return
        if not isinstance(value, dict):
            raise BodyRefused("tool_choice")
        kind = value.get("type")
        hosted = {"web_search", "web_search_preview", "file_search", "code_interpreter", "image_generation"}
        if self.protocol == "chat" and kind == "function" and value.keys() == {"type", "function"}:
            self.obj(value["function"], {"name": "name"}, "tool_choice")
            if not isinstance(value["function"], dict) or "name" not in value["function"]:
                raise BodyRefused("tool_choice")
        elif self.protocol == "responses" and kind in {"function", "custom"} and value.keys() == {"type", "name"}:
            self.member(value, "name", "name")
        elif self.protocol == "responses" and kind in hosted and value.keys() == {"type"}:
            pass
        else:
            raise BodyRefused("tool_choice")
        if self.declarations:
            self.text(kind)

    def stream_options(self, value: Any) -> None:
        """include_usage (Chat Completions), include_obfuscation (Responses), and Codex 0.162.0's
        reasoning_summary_delivery (codex-api/src/common.rs:187-195, "sequential_cutoff")."""
        if not isinstance(value, dict):
            raise BodyRefused("stream_options")
        for key, item in value.items():
            if key in {"include_usage", "include_obfuscation"} and type(item) is bool:
                continue
            if key == "reasoning_summary_delivery" and item == "sequential_cutoff":
                if self.declarations:
                    self.text(item)
                continue
            raise BodyRefused("stream_options")

    def tool(self, value: Any) -> None:
        fields = {
            # Codex 0.162.0 also declares a "namespace" of function tools (multi_agent_v1) and "web_search".
            "type": frozenset(
                {"function", "custom", "tool_definition", "web_search_20250305", "mcp", "namespace", "web_search"}
            ),
            "name": "name",
            "description": "text",
            "input_schema": "schema",
            "parameters": "schema",
            "input_examples": "data",
            "strict": "bool",
            "cache_control": self.cache,
            "function": self.tool,
            "format": self.format,
            "defer_loading": "bool",
            "max_uses": "number",
            "server_label": "resource",
            "server_url": "resource",
            "server_description": "text",
            "external_web_access": "bool",
        }
        if isinstance(value, dict) and value.get("type") == "namespace":
            fields = {**fields, "tools": lambda x: self.listed(x, self.tool)}
        self.obj(value, fields, "tool definition")

    def fallback(self, value: Any) -> None:
        self.obj(
            value,
            {
                "model": "model",
                "output_config": lambda x: self.obj(
                    x,
                    {"format": self.format, "effort": frozenset({"low", "medium", "high", "max"})},
                    "fallback output settings",
                ),
                "output_format": self.format,
            },
            "fallback",
        )

    def cache(self, value: Any) -> None:
        self.obj(value, {"type": frozenset({"ephemeral"}), "ttl": frozenset({"5m", "1h"})}, "cache control")

    def format(self, value: Any) -> None:
        self.obj(
            value,
            {
                "type": frozenset({"json_schema", "json_object", "text", "grammar"}),
                "name": "name",
                "description": "text",
                "schema": "schema",
                "strict": "bool",
                "json_schema": self.format,
                "syntax": frozenset({"lark", "regex"}),
                "definition": "text",
            },
            "output schema",
        )

    def content(self, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            self.text(value)
            return
        self.listed(value, self.block)

    def block(self, value: Any) -> None:
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise BodyRefused("message content block")
        kind = value["type"]
        common = {"type": frozenset({kind}), "cache_control": self.cache}
        kinds: dict[str, dict[str, Any]] = {
            "text": {"text": "text", "citations": lambda x: self.listed(x, self.citation)},
            "input_text": {"text": "text"},
            "output_text": {"text": "text", "annotations": lambda x: self.listed(x, self.citation)},
            "summary_text": {"text": "text"},
            "refusal": {"refusal": "text"},
            "tool_use": {"id": "id", "name": "name", "input": "data"},
            "tool_result": {"tool_use_id": "reference", "content": self.content, "is_error": "bool"},
            "image": {"source": self.image_source},
            "image_url": {"image_url": self.image_url},
            "computer_screenshot": {"image_url": self.image_url},
            "input_image": {"image_url": self.image_url, "detail": frozenset({"auto", "low", "high"})},
            "thinking": {"thinking": "text", "signature": "opaque"},
            "redacted_thinking": {"data": "opaque"},
            "compaction": {"content": "text", "encrypted_content": "opaque"},
            "connector_text": {"text": "text"},
            "tool_reference": {"tool_name": "name"},
            "tool_removal": {"name": "name", "tool": self.added_tool},
            "tool_addition": {"tool": self.added_tool},
            "server_tool_use": {"id": "id", "name": "name", "input": "data"},
            "mcp_tool_use": {"id": "id", "name": "name", "server_name": "resource", "input": "data"},
            "mcp_tool_result": {"tool_use_id": "reference", "content": self.content, "is_error": "bool"},
            "mcp_tool_listing": {"server_name": "resource", "tools": lambda x: self.listed(x, self.tool)},
            "fallback": {"from": self.fallback, "to": self.fallback},
            "container_upload": {"file_id": "resource"},
        }
        for result_kind in (
            "web_search_tool_result",
            "web_fetch_tool_result",
            "code_execution_tool_result",
            "bash_code_execution_tool_result",
            "text_editor_code_execution_tool_result",
            "tool_search_tool_result",
            "advisor_tool_result",
        ):
            kinds[result_kind] = {"tool_use_id": "reference", "content": self.provider_result}
        provider = kind in _PROVIDERS_BLOCKS
        if kind == "container_upload" and "file_id" not in value:
            raise BodyRefused("provider item")
        fields = kinds.get(kind)
        if fields is None:
            raise BodyRefused("provider item" if "tool" in kind or "search" in kind else "message content block")
        self.obj(value, common | fields, "provider item" if provider else "message content block", provider=provider)

    def image_source(self, value: Any) -> None:
        self.obj(
            value,
            {
                "type": frozenset({"base64"}),
                "media_type": frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
                "data": self.image_data,
            },
            "inline image",
            provider=True,
        )

    def image_data(self, value: Any) -> None:
        if not isinstance(value, str):
            raise BodyRefused("inline image")
        self.result.rules.add(M3)  # preparation requires OCR or a declared removal before minting

    def image_url(self, value: Any) -> None:
        if isinstance(value, dict):
            self.obj(
                value,
                {"url": self.image_url, "detail": frozenset({"auto", "low", "high"})},
                "inline image",
                provider=True,
            )
        elif isinstance(value, str) and value.startswith(
            ("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,", "data:image/gif;base64,")
        ):
            self.result.rules.add(M3)
        else:
            raise BodyRefused("remote media")

    def citation(self, value: Any) -> None:
        self.obj(
            value,
            {
                "type": frozenset(
                    {
                        "char_location",
                        "page_location",
                        "content_block_location",
                        "web_search_result_location",
                        "url_citation",
                    }
                ),
                "cited_text": "text",
                "title": "text",
                "url": "text",
                "document_index": "number",
                "start_char_index": "number",
                "end_char_index": "number",
                "start_page_number": "number",
                "end_page_number": "number",
                "start_block_index": "number",
                "end_block_index": "number",
                "encrypted_index": "opaque",
            },
            "provider item",
            provider=True,
        )

    def message(self, value: Any) -> None:
        self.obj(
            value,
            {
                "role": frozenset({"system", "developer", "user", "assistant", "tool", "function"}),
                "content": self.content,
                "refusal": "text",
                "name": "name",
                "tool_call_id": "reference",
                "tool_calls": lambda x: self.listed(x, self.call),
                "function_call": self.function,
            },
            "message",
        )

    def function(self, value: Any) -> None:
        self.obj(value, {"name": "name", "arguments": "text", "input": "text"}, "tool input")

    def call(self, value: Any) -> None:
        self.obj(
            value,
            {"id": "id", "type": frozenset({"function", "custom"}), "function": self.function, "custom": self.function},
            "tool input",
        )

    def added_tool(self, value: Any) -> None:
        if not isinstance(value, dict):
            raise BodyRefused("tool definition")
        if value.get("type") == "tool_reference":
            self.obj(
                value,
                {"type": frozenset({"tool_reference"}), "tool_name": "name", "name": "name"},
                "tool definition",
                provider=True,
            )
        else:
            self.obj(
                value,
                {"type": frozenset({"tool_definition"}), "definition": self.tool},
                "tool definition",
                provider=True,
            )

    def provider_result(self, value: Any) -> None:
        if isinstance(value, list):
            self.listed(value, self.provider_result)
            return
        fields = {
            "type": frozenset(
                {
                    "web_search_result",
                    "web_fetch_result",
                    "code_execution_result",
                    "bash_code_execution_result",
                    "text_editor_code_execution_view_result",
                    "text_editor_code_execution_create_result",
                    "text_editor_code_execution_str_replace_result",
                    "tool_search_tool_search_result",
                    "advisor_result",
                    "web_search_tool_result_error",
                    "web_fetch_tool_result_error",
                    "code_execution_tool_result_error",
                }
            ),
            "url": "text",
            "title": "text",
            "page_age": "text",
            "retrieved_at": "text",
            "encrypted_content": "opaque",
            "stdout": "text",
            "stderr": "text",
            "return_code": "number",
            "text": "text",
            "content": "text",
            "file_type": frozenset({"text", "image", "pdf"}),
            "file_id": "resource",
            "start_line": "number",
            "num_lines": "number",
            "total_lines": "number",
            "is_file_update": "bool",
            "old_start": "number",
            "old_lines": "number",
            "new_start": "number",
            "new_lines": "number",
            "error_code": frozenset(
                {"invalid_input", "unavailable", "max_uses_exceeded", "query_too_long", "too_many_requests"}
            ),
            "tool_references": lambda x: self.listed(x, self.block),
        }
        self.obj(value, fields, "provider item", provider=True)

    def shell_output(self, value: Any) -> None:
        if isinstance(value, list) and all(isinstance(chunk, dict) and "type" not in chunk for chunk in value):
            for chunk in value:
                self.obj(
                    chunk,
                    {
                        "stdout": "text",
                        "stderr": "text",
                        "outcome": lambda x: self.obj(
                            x,
                            {"type": frozenset({"exit", "timeout"}), "exit_code": "number"},
                            "shell outcome",
                            provider=True,
                        ),
                    },
                    "shell output",
                    provider=True,
                )
        else:
            self.content(value)

    def provider_action(self, value: Any) -> None:
        self.obj(
            value,
            {
                "type": frozenset(
                    {
                        "search",
                        "open_page",
                        "find_in_page",
                        "click",
                        "double_click",
                        "drag",
                        "keypress",
                        "move",
                        "screenshot",
                        "scroll",
                        "type",
                        "wait",
                    }
                ),
                "query": "text",
                "queries": "texts",
                "url": "resource",
                "pattern": "text",
                "text": "text",
                "keys": "texts",
                "x": "number",
                "y": "number",
                "scroll_x": "number",
                "scroll_y": "number",
                "button": frozenset({"left", "right", "wheel", "back", "forward"}),
                "path": lambda x: self.listed(
                    x, lambda point: self.obj(point, {"x": "number", "y": "number"}, "action", provider=True)
                ),
            },
            "provider item",
            provider=True,
        )

    def patch_operation(self, value: Any) -> None:
        self.obj(
            value,
            {"type": frozenset({"create_file", "update_file", "delete_file"}), "path": "text", "diff": "text"},
            "provider item",
            provider=True,
        )

    def item(self, value: Any) -> None:
        if not isinstance(value, dict):
            raise BodyRefused("input item")
        kind = value.get("type", "message")
        if not isinstance(kind, str):
            raise BodyRefused("input item")
        fields = {
            "type": frozenset({kind}),
            "id": "id",
            "status": frozenset({"completed", "in_progress", "incomplete", "failed", "searching"}),
        }
        if kind == "message":
            fields |= {"role": frozenset({"system", "developer", "user", "assistant"}), "content": self.content}
        elif kind in {"function_call", "custom_tool_call"}:
            fields |= {"call_id": "id", "name": "name", "arguments": "text", "input": "text"}
        elif kind in {
            "function_call_output",
            "custom_tool_call_output",
            "local_shell_call_output",
            "computer_call_output",
            "apply_patch_call_output",
        }:
            fields |= {"call_id": "reference", "output": self.output, "max_output_length": "number"}
        elif kind in {"local_shell_call", "shell_call"}:
            fields |= {"call_id": "id", "action": "data"}
        elif kind == "shell_call_output":
            fields |= {"call_id": "reference", "output": self.shell_output, "max_output_length": "number"}
        elif kind == "additional_tools":
            fields |= {"tools": lambda x: self.listed(x, self.tool)}
        elif kind == "mcp_call":
            fields |= {
                "name": "name",
                "arguments": "text",
                "output": "text",
                "error": "text",
                "server_label": "resource",
                "approval_request_id": "reference",
            }
        elif kind == "mcp_approval_response":
            fields |= {"approval_request_id": "reference", "approve": "bool", "reason": "text"}
        elif kind in {"reasoning", "compaction"}:
            fields |= {
                "encrypted_content": "opaque",
                "summary": lambda x: self.listed(x, self.block),
                "content": self.content,
            }
        elif kind == "item_reference":
            if "id" not in value:
                raise BodyRefused("provider item")
            fields["id"] = "resource"
        elif kind == "web_search_call":
            fields |= {"action": self.provider_action}
        elif kind == "file_search_call":
            fields |= {"queries": "texts", "results": lambda x: self.listed(x, self.search_result)}
        elif kind == "image_generation_call":
            fields |= {"result": "opaque", "revised_prompt": "text"}
        elif kind == "code_interpreter_call":
            fields |= {
                "code": "text",
                "container_id": "resource",
                "outputs": lambda x: self.listed(x, self.code_output),
            }
        elif kind == "computer_call":
            fields |= {
                "call_id": "id",
                "action": self.provider_action,
                "pending_safety_checks": lambda x: self.listed(x, self.safety_check),
            }
        elif kind == "apply_patch_call":
            fields |= {"call_id": "id", "operation": self.patch_operation}
        elif kind in {"mcp_list_tools", "tool_search_output"}:
            fields |= {
                "server_label": "resource",
                "tools": lambda x: self.listed(x, self.tool),
                "error": "text",
                "call_id": "reference",
            }
        elif kind in {"mcp_approval_request", "tool_search_call"}:
            fields |= {"server_label": "resource", "name": "name", "arguments": "text", "call_id": "id"}
        else:
            raise BodyRefused("provider item")
        self.obj(
            value,
            fields,
            "provider item" if kind in _PROVIDERS_ITEMS else "input item",
            provider=kind in _PROVIDERS_ITEMS,
        )

    def output(self, value: Any) -> None:
        if isinstance(value, dict):
            self.block(value)
        else:
            self.content(value)

    def safety_check(self, value: Any) -> None:
        self.obj(value, {"id": "id", "code": "text", "message": "text"}, "provider item", provider=True)

    def search_result(self, value: Any) -> None:
        self.obj(
            value,
            {"file_id": "resource", "filename": "text", "text": "text", "score": "number"},
            "provider item",
            provider=True,
        )

    def code_output(self, value: Any) -> None:
        self.obj(
            value,
            {"type": frozenset({"logs", "image"}), "logs": "text", "url": "resource"},
            "provider item",
            provider=True,
        )

    def request(self, body: dict[str, Any]) -> None:
        if not isinstance(body, dict):
            raise BodyRefused("top-level")
        common = {
            "model": "model",
            "stream": "bool",
            "metadata": "metadata",
            "user": "text",
            "safety_identifier": "text",
            "temperature": "number",
            "top_p": "number",
            "max_tokens": "number",
            "max_output_tokens": "number",
            "max_completion_tokens": "number",
            "frequency_penalty": "number",
            "presence_penalty": "number",
            "seed": "number",
            "n": "number",
            "store": "bool",
            "tools": lambda x: self.listed(x, self.tool),
            "parallel_tool_calls": "bool",
            "stop": "texts",
            "stop_sequences": "texts",
            "service_tier": frozenset({"auto", "default", "flex", "priority"}),
        }
        if self.protocol in {"chat", "responses"}:
            common |= {"tool_choice": self.tool_choice, "stream_options": self.stream_options}
        if self.protocol == "anthropic":
            common |= {
                "messages": lambda x: self.listed(x, self.message),
                "system": "system",
                "top_k": "number",
                "output_format": self.format,
                "output_config": lambda x: self.obj(
                    x, {"format": self.format, "effort": frozenset({"low", "medium", "high", "max"})}, "output settings"
                ),
                "thinking": lambda x: self.obj(
                    x,
                    {"type": frozenset({"enabled", "disabled", "adaptive"}), "budget_tokens": "number"},
                    "thinking control",
                ),
                "fallbacks": lambda x: self.listed(x, self.fallback),
                "anthropic_version": frozenset({"bedrock-2023-05-31", "vertex-2023-10-16"}),
            }
        elif self.protocol == "chat":
            common |= {
                "messages": lambda x: self.listed(x, self.message),
                "prompt": "texts",
                "suffix": "text",
                "functions": lambda x: self.listed(x, self.tool),
                "response_format": self.format,
                "prediction": lambda x: self.obj(
                    x, {"type": frozenset({"content"}), "content": self.content}, "prediction"
                ),
                "logprobs": "bool",
                "top_logprobs": "number",
                "echo": "bool",
            }
        elif self.protocol == "responses":
            common |= {
                "input": "input",
                "instructions": "text",
                "prompt": lambda x: self.obj(
                    x, {"id": "resource", "version": "text", "variables": "data"}, "prompt", provider=True
                ),
                "previous_response_id": "response id",
                "prompt_cache_key": "cache key",
                # OpenAI's documented retentions; Hermes Agent sends "24h" (codex.py:357-372).
                "prompt_cache_retention": frozenset({"in-memory", "24h"}),
                # Server-side compaction, as Hermes Agent sends it (agent/native_compaction.py:3, 142).
                "context_management": lambda x: self.listed(
                    x,
                    lambda entry: self.obj(
                        entry,
                        {"type": frozenset({"compaction"}), "compact_threshold": "number"},
                        "context management",
                    ),
                ),
                "client_metadata": "removable",
                "include": "texts",
                "background": "bool",
                "truncation": frozenset({"auto", "disabled"}),
                "reasoning": lambda x: self.obj(
                    x,
                    {
                        "effort": frozenset({"none", "minimal", "low", "medium", "high", "xhigh"}),
                        "summary": frozenset({"auto", "concise", "detailed"}),
                    },
                    "reasoning control",
                ),
                "text": lambda x: self.obj(
                    x, {"format": self.format, "verbosity": frozenset({"low", "medium", "high"})}, "output settings"
                ),
            }
        elif self.protocol == "embeddings":
            common = {
                "model": "model",
                "input": "texts",
                "encoding_format": frozenset({"float", "base64"}),
                "dimensions": "number",
                "user": "text",
            }
        elif self.protocol == "gemini":
            common = {
                "model": "model",
                "contents": lambda x: self.listed(x, self.gcontent),
                "content": self.gcontent,
                "systemInstruction": self.gcontent,
                "title": "text",
                "taskType": frozenset(
                    {
                        "RETRIEVAL_QUERY",
                        "RETRIEVAL_DOCUMENT",
                        "SEMANTIC_SIMILARITY",
                        "CLASSIFICATION",
                        "CLUSTERING",
                        "QUESTION_ANSWERING",
                        "FACT_VERIFICATION",
                        "CODE_RETRIEVAL_QUERY",
                    }
                ),
                "outputDimensionality": "number",
                "generateContentRequest": self.request,
                "requests": lambda x: self.listed(x, self.request),
                "generationConfig": self.gconfig,
                "tools": lambda x: self.listed(x, self.gtool),
                "cachedContent": "resource",
                "displayName": "text",
                "ttl": "text",
                "expireTime": "text",
            }
            common = self.aliases(common)
        if self.protocol == "responses" and body.get("type") == "response.create":
            # Codex's WebSocket prewarm asks for no output (ResponseCreateWsRequest.generate, common.rs:331-365).
            common |= {
                "type": frozenset({"response.create"}),
                "response": self.request,
                "event_id": "id",
                "generate": "bool",
            }
        self.obj(body, common, "top-level")
        # A reference names what the same body declares. One that continues a response may name a call of that
        # response, which it does not hold: Codex over a WebSocket sends a tool's output alone after its call
        # (core/src/client.rs, the incremental ResponseCreateWsRequest), as a Responses client over HTTP can. Root's
        # ruling: it passes, its provider-id grammar read (``member``) and its text checked by the final check, as
        # every reference is; without a previous_response_id, it is refused.
        if not self.continued and any(value not in self.declared for value in self.references):
            self.result.problem = "request body refused (correlation reference)"

    @staticmethod
    def aliases(fields: dict[str, Any]) -> dict[str, Any]:
        return fields | {re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): value for key, value in fields.items()}

    def gcontent(self, value: Any) -> None:
        self.obj(
            value,
            {"role": frozenset({"user", "model", "function", "system"}), "parts": lambda x: self.listed(x, self.gpart)},
            "message",
        )

    def gpart(self, value: Any) -> None:
        fields = {
            "text": "text",
            "thought": "bool",
            "thoughtSignature": "opaque",
            "inlineData": lambda x: self.obj(
                x,
                self.aliases(
                    {
                        "mimeType": frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
                        "data": self.image_data,
                    }
                ),
                "inline image",
                provider=True,
            ),
            "functionCall": lambda x: self.obj(x, {"id": "id", "name": "name", "args": "data"}, "tool input"),
            "functionResponse": lambda x: self.obj(
                x,
                {"id": "reference", "name": "name", "response": "data", "parts": lambda p: self.listed(p, self.gpart)},
                "tool result",
            ),
            # The parts the provider ran (gemini._PROVIDERS), covered as a function's call and response are.
            "toolCall": lambda x: self.obj(
                x,
                self.aliases({"id": "id", "toolType": self.gkind, "toolName": "name", "args": "data"}),
                "tool input",
            ),
            "toolResponse": lambda x: self.obj(
                x, self.aliases({"id": "reference", "toolType": self.gkind, "response": "data"}), "tool result"
            ),
            "executableCode": lambda x: self.obj(
                x, self.aliases({"id": "id", "language": self.gkind, "code": "text"}), "tool input"
            ),
            "codeExecutionResult": lambda x: self.obj(
                x, self.aliases({"id": "reference", "outcome": self.gkind, "output": "text"}), "tool result"
            ),
        }
        self.obj(
            value,
            self.aliases(fields),
            "message content block",
            provider="thoughtSignature" in value or "thought_signature" in value,
        )

    def gkind(self, value: Any) -> None:
        """The kind a part the provider ran names (its tool's type, its code's language, its result's outcome): a token
        of ``gemini.KIND``'s bounded form, and one this version does not know passes; checked by the final check as
        semantic text, as an id or a name is, since the form alone does not keep a value of yours out of it."""
        if not isinstance(value, str) or not gemini.KIND.fullmatch(value):
            raise BodyRefused("control")
        self.text(value, semantic=True)

    def gconfig(self, value: Any) -> None:
        self.obj(
            value,
            self.aliases(
                {
                    "temperature": "number",
                    "topP": "number",
                    "topK": "number",
                    "candidateCount": "number",
                    "maxOutputTokens": "number",
                    "stopSequences": "texts",
                    "seed": "number",
                    "responseMimeType": frozenset({"text/plain", "application/json"}),
                    "responseSchema": "schema",
                    "responseJsonSchema": "schema",
                }
            ),
            "generation control",
        )

    def gtool(self, value: Any) -> None:
        self.obj(
            value,
            self.aliases(
                {
                    "functionDeclarations": lambda x: self.listed(
                        x,
                        lambda item: self.obj(
                            item,
                            self.aliases(
                                {
                                    "name": "name",
                                    "description": "text",
                                    "parameters": "schema",
                                    "parametersJsonSchema": "schema",
                                    "response": "schema",
                                    "responseJsonSchema": "schema",
                                }
                            ),
                            "tool definition",
                        ),
                    )
                }
            ),
            "tool definition",
        )


class ProtectedLaunch(ValueError):
    """Only the request's categorical result crosses the reader boundary."""


def declared_body_safe(
    body: dict[str, Any],
    protocol: str,
    upstream: Upstream,
    scan: BodyScan,
    ledger: Ledger,
    *,
    projection: bool = False,
    grants: BodyGrants | None = None,
) -> bool:
    """Screen what the closed declaration reads, and what the exceptions in force (``grants``) admit, for a protected
    launch credential."""
    reader = _Body(
        protocol, upstream, scan, False, ledger, declarations=True, projection=projection, grants=grants, root=body
    )
    try:
        reader.request(body)
    except ProtectedLaunch:
        return False
    return True


def inspect_body(
    body: dict[str, Any],
    protocol: str,
    upstream: Upstream,
    scan: BodyScan,
    verify: bool,
    ledger: Ledger,
    opaque: dict[tuple[int, str], str] | None = None,
    *,
    grants: BodyGrants | None = None,
    excepted: bool = True,
    accounted: BodyFreeze | None = None,
    cache_create: bool = False,
) -> BodyInspection:
    """Read ``body`` by the closed declaration of its protocol. ``grants`` are the body selectors of the exceptions in
    force for this exact route (``body_selectors``): an undeclared member they name is checked and passes, naming its
    exception in the inspection's ``rules``; none, or None, leaves every undeclared member refused. ``excepted``:
    whether a request exception may apply to this body at all (False for a WebSocket frame, F-2), which an unknown
    member's refusal says (``BodyRefused``). ``accounted``: the shape the ledger last accounted of this body (the
    inspection's ``shape``): a body of that very shape since, its leaf strings alone changed as masking changes them,
    has the final paths, the active set and the receipts that account made, and is not accounted again (P2-8); any
    other is. ``cache_create``: the body creates a Gemini cache, whose model the GenAI SDK names as ``models/{id}``."""
    shape = None
    if accounted is None or not accounted.same_shape(body):
        shape = ledger.account(body, initial=not ledger.initialized)
    reader = _Body(
        protocol,
        upstream,
        scan,
        verify,
        ledger,
        opaque,
        grants=grants,
        root=body,
        excepted=excepted,
        cache_create=cache_create,
    )
    reader.request(body)
    if shape is not None:
        ledger.finish()
    reader.result.shape = shape
    return reader.result
