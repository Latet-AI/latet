"""The decision that sends a request or not: ``SendDecision = Forward | Pass | Block`` (the request-coverage plan's
typed outcome, ``notes/server-request-coverage-plan.md``; the name ``Decision`` is the Live Reviewer's,
``approvals.py``).

The forwarder and the WebSocket relay build each request's ``Envelope`` and, once nothing will change its arena (the
``Prepared`` that ``Mediator.prepare`` made, after the local reviewer and the Live Reviewer; a WebSocket turn after it
is written anew), finalize it once (``finalize``) into a frozen value: a refusal (``Block``) carries nothing of the
request, only why and the status its client is answered; ``Forward`` and ``Pass`` carry the payload that leaves (shared
with the arena, never copied: ownership moves into the value), the texts of its slots as they leave, the rules it was
decided under and how its answer is restored (``ResponsePlan``), which a ``Pass`` never switches off. ``Forward`` needs
a complete proof that every part of the request was covered; a ``Pass`` names the built-in rules it rests on
(``request_policy``), with the same complete proof of what it carries. Nothing else leaves: Phase 1's internal rule that
named 0.10's behaviour is gone (P2-6), and a request of a project where Latet is off is refused (D5).

Every decision is minted here, by one private factory that registers it (RC-REC-8): ``sendable``, the check the guarded
transport makes before it builds anything (P1-3, P1-4), refuses a value this module did not mint (one built by hand, a
copy, one written over after it was minted, down to the values it holds), a refusal, a bridge's ``Observation``, a
decision presented for another request than its own, one whose payload is not what its arena leaves with, and a
decision whose arena changed after it was finalized (a reviewer's or a late writer's change to the body, the document, a
slot, an image, or what its answer is read by): what would leave is no longer what was decided. The factory takes from
its caller neither an HTTP request's payload (always its arena's body, or the request's own without an arena) nor how
the answer is read when there is an arena (derived from it); a frame's payload, as written anew, is given and checked.
What happened at the send (``Execution``) and the resources an exchange holds (``lease.ExchangeLease``) are kept apart
from the value, and no value prints what a request carried. Nothing here reads a setting, the environment or a
header."""

from __future__ import annotations

import threading
import weakref
from collections.abc import Iterable
from dataclasses import dataclass, field, fields, is_dataclass
from typing import TYPE_CHECKING

from latet.engine.types import SlotKind
from latet.engine.verifier import Leak
from latet.gateway.body_coverage import BodyProof
from latet.gateway.body_selectors import exception_rule
from latet.gateway.coverage import (
    ENVELOPE_STATUSES,
    TRANSPORTS,
    CoverageProof,
    EmptyBodyProof,
    Envelope,
    EnvelopeProof,
    Transport,
    envelope_status,
)
from latet.gateway.request_policy import PolicySnapshot, body_exception_ids, current

if TYPE_CHECKING:
    from latet.engine.pipeline import Session
    from latet.gateway.mediate import Prepared

_REWRITE = "rewrite"  # mediate.REWRITE: a request Latet rewrote, whose answer it restores

# Why a request is refused, by code (its words are the ``reason`` its client is told, as 0.10 tells it).
BLOCK_CODES: frozenset[str] = frozenset(ENVELOPE_STATUSES) | frozenset(
    {
        "refused",  # 0.10's preparation refused it (its ``block_reason``)
        "too-large",  # its body would decode past a server's max_request_mb (413)
        "masking-failed",  # its masking failed: a vault at its limit, no fake free (400)
        "proof-incomplete",  # a proof that leaves a part of it unaccounted for (Phase 2)
        "proof-foreign",  # a proof of another request than its own
        "evidence",  # evidence its rules do not admit
        # Refused before its content was read (404 or 400, as 0.10 answers), and nothing of it read: no route for its
        # path, a cloud's call Latet does not read, a signing route this Latet did not make, a destination of the
        # identity's own the server may not reach.
        "no-route",
        "unread",
        "no-signing-route",
        "egress-refused",
        "credentials",  # the AWS credentials Latet signs with could not be read (a login that ended): 400
        # A WebSocket's, before its frames are read: its conversation could not be read (closed 1011), or a turn asked
        # while its identity has as many in flight as it may (an error frame, the socket open).
        "conversation-unread",
        "busy",
        "coverage-limit",
        "envelope-binding",
        "envelope-region",
        "envelope-location",
        "envelope-route",
        "envelope-resource",
        "envelope-escape",
        "envelope-encoding",
        "envelope-control",
        "envelope-path",
        "envelope-bodyless",
        "envelope-query-unknown",
        "envelope-query-duplicate",
        "envelope-query-value",
        "envelope-header-unknown",
        "envelope-header-duplicate",
        "envelope-header-syntax",
        "envelope-header-value",
        "envelope-credential",
        "envelope-sensitive",
    }
)
_INCOMPLETE = "Latet could not account for every part of this request, so it does not send it"
_NOT_ADMITTED = "Latet's rules do not let this request leave, so it does not send it"
_OFF = "Latet is off in this project, so it does not send its requests"
# What reads a request without deciding whether it leaves: the plugin's bridge (P1-5).
OBSERVERS: frozenset[str] = frozenset({"route-check", "outbound", "final-check"})


class AlreadyFinal(RuntimeError):
    """A request's arena is finalized once: a second decision of the same request is a mistake of the caller's."""


class NotSendable(Exception):
    """What ``sendable`` refuses, by code: ``not-a-decision``, ``unknown``, ``unminted``, ``tampered``, ``block``,
    ``proof-incomplete``, ``evidence``, ``foreign`` (another request's or arena's decision, or one given an arena it had
    not), ``payload`` (a payload that is not what its request or arena leaves with),
    ``stale`` (its arena changed after it)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PolicyEvidence:
    """What a ``Pass`` rests on besides its complete proof: the built-in rules it names, by name, its profile, and the
    request exceptions (Latet's ids) that let through what no profile names (P2-6)."""

    rules: tuple[str, ...]
    profile: str
    exceptions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResponsePlan:
    """How the answer to a request is read: its protocol, whether it asked for a stream (an answer naming no
    Content-Type is read as one), whether what its conversation was shown is restored in it (``restore``: a request
    Latet rewrote), whether what the model writes is read as it passes (Claude Code's), and the conversation whose
    values it restores (its session's key)."""

    protocol: str | None = None
    stream: bool = False
    restore: bool = False
    written: bool = False
    session: str | None = field(default=None, repr=False)  # a session header's value: never printed


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class Forward:
    """A request every part of which was covered (``proof``): it leaves as ``payload``. Printed without its payload or
    texts."""

    envelope: Envelope
    payload: bytes | str = field(repr=False)
    texts: tuple[str, ...] = field(repr=False)
    proof: CoverageProof | BodyProof | EmptyBodyProof
    policy: PolicySnapshot
    response: ResponsePlan
    envelope_proof: EnvelopeProof | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class Pass:
    """A request that leaves as ``payload`` under a built-in rule, visibly (``evidence`` names it), its parts covered
    as a ``Forward``'s are. Printed without its payload or texts."""

    envelope: Envelope
    payload: bytes | str = field(repr=False)
    texts: tuple[str, ...] = field(repr=False)
    evidence: PolicyEvidence
    policy: PolicySnapshot
    response: ResponsePlan
    envelope_proof: EnvelopeProof | None = field(default=None, repr=False)
    body_proof: BodyProof | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class Block:
    """A request that does not leave: why (``code``, and the words its client is told), and the status it is answered.
    Nothing of the request: no payload, envelope, header, target or text."""

    transport: Transport
    destination: str | None
    code: str
    reason: str
    status: int


SendDecision = Forward | Pass | Block


@dataclass(frozen=True, slots=True)
class ObservedText:
    """One text the plugin's bridge read, as the dashboard shows it: its words (never printed), what it is, and whether
    the model wrote it."""

    text: str = field(repr=False)
    kind: SlotKind
    from_model: bool = False


@dataclass(frozen=True, slots=True)
class Observation:
    """What the plugin's bridge read of a request it never sends (a route check, a tool call stopped, its final check):
    a record, never a decision nor an arena one is made of; ``finalize`` and ``decide`` refuse it, ``sendable`` too. Its
    texts (``blocks``), whether its values in clear came from history Latet never masked (``history``), and the
    conversation it is recorded in (``session``, the engine's: what its fakes restore to is read from it as it is
    recorded). Printed without its texts, its tools or its conversation."""

    source: str
    protocol: str | None
    reason: str | None
    uninspected: tuple[str, ...]
    leaks: tuple[Leak, ...]
    outbound: tuple[str, ...] = field(repr=False)  # tool names: never printed
    bypass: bool = False
    blocks: tuple[ObservedText, ...] = field(default=(), repr=False)
    history: bool = False
    session: Session | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class NotAttempted:
    """A decision that sent nothing: why, by code (credentials that could not be read, say)."""

    code: str


@dataclass(frozen=True, slots=True)
class Attempted:
    """A decision sent ``attempts`` times (two only for AWS's expired token); the provider's last status, None when
    no answer came."""

    attempts: int
    status: int | None = None


Execution = NotAttempted | Attempted


# --- the arena: what a decision was finalized from --------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _Seal:
    """What an arena was when it was finalized: its body, document and session (the very objects), the payload it leaves
    with, its slots' texts, its images' states and what each carries, its mode, refusal and counts, and what its answer
    is read by. Any change after it is seen (``_unchanged``). Never printed."""

    body: object
    document: object
    session: object
    payload: object
    texts: tuple[str, ...]
    images: tuple[tuple[str, object], ...]
    state: tuple[object, ...]
    generation: object  # immutable worker-freeze token; no mutable-container metadata retained here


def _texts(arena: Prepared) -> tuple[str, ...]:
    return arena.frozen_texts


def _images(arena: Prepared) -> tuple[tuple[str, object], ...]:
    return tuple((work.state, work.ref.holder.get(work.ref.key)) for work in arena.images)


def _state(arena: Prepared) -> tuple[object, ...]:
    """Its mode, refusal and counts, and what its answer is read by (``forward.py`` reads those after the decision)."""
    return (
        arena.mode,
        arena.off,
        arena.block_reason,
        arena.masked_count,
        arena.amended,
        arena.stream,
        arena.protocol,
        arena.claude_code,
        arena.encoding,
        arena.shape,
        arena.envelope_checked,
        arena.envelope_refusal,
        arena.body_proof,
        arena.body_structure,
        arena.body_epoch.token if arena.body_epoch is not None else None,
        arena.body_verified_token,
        arena.authority,
        arena.route_authority,
        arena.control_target,
        arena.control_current,
        arena.protected_current,
    )


def _seal_of(arena: Prepared, payload: object) -> _Seal:
    """What the arena is as it is finalized, on the event loop: what its worker froze (its generation, its frozen texts,
    the proof minted over its payload), never walked again here. The worker walked its body's shape as it froze it
    (``Prepared.freeze_inputs``), and the send guard checks its slots against the freeze before it leaves
    (``sendable``): a change since is refused there as stale."""
    if arena.block_reason:  # a refusal sends nothing: what it was is all there is to keep
        return _Seal(arena.body, arena.document, arena.session, payload, (), (), _state(arena), arena.freeze_generation)
    return _Seal(
        arena.body,
        arena.document,
        arena.session,
        payload,
        _texts(arena),
        _images(arena),
        _state(arena),
        arena.freeze_generation,
    )


def _unchanged(arena: Prepared, seal: _Seal) -> bool:
    if arena.body is not seal.body or arena.document is not seal.document or arena.session is not seal.session:
        return False
    if arena.freeze_generation is not seal.generation or _state(arena) != seal.state:
        return False
    images = _images(arena)
    if len(images) != len(seal.images):
        return False
    if any(
        state != was or carried is not held for (state, carried), (was, held) in zip(images, seal.images, strict=True)
    ):
        return False
    texts = _texts(arena)
    # Its generation, payload and identities sealed are compared above, each in constant time; its slots here, each
    # the very slot holding the very text frozen. The body's whole shape was walked once, in its worker, as it froze.
    return (
        len(texts) == len(seal.texts)
        and arena.slots_unchanged()
        and all(now is was for now, was in zip(texts, seal.texts, strict=True))
    )


# --- the mint ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _Binding:
    values: tuple[object, ...]  # the decision's fields as it was minted, and the fields of each value it holds
    seal: _Seal | None  # its arena's, None for a request with none


_MINTED: weakref.WeakKeyDictionary[object, _Binding] = weakref.WeakKeyDictionary()
_MINTING = threading.Lock()


def _values(decision: SendDecision) -> tuple[object, ...]:
    """Its fields, and the fields of each frozen value among them (its envelope, proof or evidence, policy, plan): each
    compared by identity, so that a value written over at either level is seen."""
    out: list[object] = []
    for f in fields(decision):
        value = getattr(decision, f.name)
        out.append(value)
        if is_dataclass(value) and not isinstance(value, type):
            out.extend(getattr(value, g.name) for g in fields(value))
    return tuple(out)


def _mint[D: (Forward, Pass, Block)](decision: D, seal: _Seal | None) -> D:
    with _MINTING:
        _MINTED[decision] = _Binding(_values(decision), seal)
    return decision


def _admitted(evidence: PolicyEvidence, policy: PolicySnapshot) -> bool:
    """Its rules each built in and in force, or the rule of an exception it names (``exception_rule``: the body
    members an exception admitted), so that a Forward never hides an exception and a Pass names each it rests on."""
    if type(evidence) is not PolicyEvidence or not (evidence.rules or evidence.exceptions):
        return False
    named = {exception_rule(one) for one in evidence.exceptions}
    return all(policy.allows(rule) or rule in named for rule in evidence.rules)


def _an_arena(arena: object) -> None:
    """``arena`` is a request's arena (``mediate.Prepared``), or None: what was only observed (a bridge's
    ``Observation``), or anything else, is no arena a decision is made of (``TypeError``)."""
    from latet.gateway.mediate import Prepared  # here: mediate imports this module

    if arena is not None and not isinstance(arena, Prepared):
        raise TypeError("a decision is made of a request's arena, never of what was only observed")


def finalize(
    arena: Prepared | None,
    envelope: Envelope,
    policy: PolicySnapshot,
    response: ResponsePlan,
    *,
    payload: bytes | str | None = None,
    envelope_proof: EnvelopeProof | None = None,
) -> SendDecision:
    """The decision of the request ``envelope`` is, made once, after the last change to its ``arena`` (None: a request
    with none, or a frame Latet reads nothing of): a ``Block`` when the arena refuses it, when its proofs (the arena's
    body proof, ``envelope_proof``) are not complete proofs of this very envelope, when Latet is off in its project
    (D5), or when its rules are not admitted by ``policy``; else a ``Forward`` on complete ordinary coverage, or a
    ``Pass`` naming its built-in rules.
    Body coverage is taken only from the worker-owned arena; callers cannot supply a replacement body proof.

    What leaves is not the caller's to choose: a request leaves as its arena's body, or as it came without an arena;
    ``payload`` is a WebSocket frame's alone, and required there (a turn as written anew; a frame Latet reads nothing
    of, as it came), never a masked turn as it came. With an arena, ``response`` must be how 0.10 reads its answer
    (``response_plan``), restoration included; without one, nothing is restored. A caller's mistake raises
    (``TypeError``, ``ValueError``) before anything is finalized; an arena finalized before raises ``AlreadyFinal``."""
    _an_arena(arena)
    if not isinstance(envelope, Envelope) or not isinstance(policy, PolicySnapshot):
        raise TypeError("a decision is finalized from an envelope and a policy")
    if not isinstance(response, ResponsePlan):
        raise TypeError("a decision is finalized with its response plan")
    if envelope.transport == "ws-frame":
        if not isinstance(payload, bytes | str):
            raise TypeError("a frame's decision is given the frame as it leaves, text or bytes")
        if arena is None and payload != envelope.body:
            raise ValueError("a frame Latet reads nothing of leaves as it came")
        if arena is not None and not arena.block_reason and payload is not arena.frame_payload:
            raise ValueError("a frame leaves exactly as its worker finalized it, never as it came after a change")
    elif payload is not None:
        raise TypeError("a request leaves as its arena's body, or as it came: no payload is given")
    else:
        payload = arena.body if arena is not None else envelope.body
        if not isinstance(payload, bytes):
            raise TypeError("a request leaves as bytes")
    if response != response_plan(arena):
        raise ValueError("an answer is read as its arena says, restored where it was rewritten")
    seal = None
    if arena is not None:
        if arena.sealed is not None:
            raise AlreadyFinal("this request was finalized already")
        seal = arena.sealed = _seal_of(arena, payload)
        if arena.block_reason:
            if arena.envelope_refusal is not None:
                return _mint(
                    Block(
                        envelope.transport,
                        envelope.destination,
                        arena.envelope_refusal,
                        arena.block_reason,
                        envelope_status(arena.envelope_refusal),
                    ),
                    seal,
                )
            return _mint(Block(envelope.transport, envelope.destination, "refused", arena.block_reason, 400), seal)
    texts = seal.texts if seal is not None else ()
    # Ordinary body/handshake authority is produced by the mediator, never a caller's flag or count proof.
    authority = arena.authority if arena is not None else None
    if (
        arena is None
        or authority is None
        or not authority.valid()
        or authority.off != arena.off
        or (arena.route_authority is not None and not arena.route_authority.valid())
        or (envelope.transport != "ws-frame" and (envelope_proof is None or not envelope_proof.proves(envelope)))
    ):
        return _mint(Block(envelope.transport, envelope.destination, "proof-incomplete", _INCOMPLETE, 400), seal)
    if arena.off:  # D5: a project where Latet is off sends nothing (its arena refuses already: never sent unread)
        return _mint(Block(envelope.transport, envelope.destination, "refused", _OFF, 400), seal)
    exceptions = envelope_proof.exceptions if envelope_proof is not None else ()
    if authority.empty and payload == b"" and envelope.body == b"":
        if exceptions:  # what no profile names let through by an exception: visibly, never a Forward
            evidence = PolicyEvidence((), envelope_proof.profile if envelope_proof is not None else "", exceptions)
            return _mint(Pass(envelope, payload, texts, evidence, policy, response, envelope_proof), seal)
        return _mint(
            Forward(envelope, payload, texts, EmptyBodyProof(envelope), policy, response, envelope_proof), seal
        )
    if arena.body_ledger is not None:
        body = arena.body_proof
        if (
            body is None
            or not body.proves(envelope)
            or body.payload is not payload
            or body.generation is not arena.freeze_generation
            or arena.body_epoch is None
            or body.content_token is not arena.body_epoch.token
        ):
            return _mint(Block(envelope.transport, envelope.destination, "proof-incomplete", _INCOMPLETE, 400), seal)
        if envelope_proof is not None and not envelope_proof.proves(envelope):
            return _mint(Block(envelope.transport, envelope.destination, "proof-incomplete", _INCOMPLETE, 400), seal)
        # The exceptions its body's rules name (its admitted members), beside its envelope's: vetted by the sender
        # before this (their project, their lifetime, their visibility), named here so that the Pass says each.
        exceptions = tuple(dict.fromkeys((*exceptions, *body_exception_ids(body.rules))))
        if body.rules or exceptions:
            evidence = PolicyEvidence(body.rules, body.profile, exceptions)
            if not _admitted(evidence, policy):
                return _mint(Block(envelope.transport, envelope.destination, "evidence", _NOT_ADMITTED, 400), seal)
            return _mint(Pass(envelope, payload, texts, evidence, policy, response, envelope_proof, body), seal)
        return _mint(Forward(envelope, payload, texts, body, policy, response, envelope_proof), seal)
    return _mint(Block(envelope.transport, envelope.destination, "proof-incomplete", _INCOMPLETE, 400), seal)


def response_plan(arena: Prepared | None) -> ResponsePlan:
    """How the answer to a request with this arena is read: as 0.10 reads it (nothing restored without one)."""
    if arena is None:
        return ResponsePlan()
    return ResponsePlan(
        protocol=arena.protocol,
        stream=arena.stream,
        restore=arena.mode == _REWRITE,
        written=arena.claude_code,
        session=arena.session.key if arena.session is not None else None,
    )


def decide(arena: Prepared | None, envelope: Envelope, *, payload: bytes | str | None = None) -> SendDecision:
    """``finalize`` under the rules in force, the answer read as its arena says: a frame's decision (``payload``, its
    exact frozen representation) and a request's without an envelope proof (refused: every request needs one)."""
    _an_arena(arena)
    return finalize(arena, envelope, current(), response_plan(arena), payload=payload)


def refuse(transport: Transport, destination: str | None, code: str, reason: str, status: int) -> Block:
    """A refusal minted without an arena: of a request refused before its content was read (no route, a call Latet does
    not read, a signing route not made here, an egress refused), of one read before it had an arena (its body would
    decode too large, its masking failed), or of one whose AWS credentials could not be read after its decision (which
    the refusal replaces): the words its client is told and the status it is answered. A WebSocket refused before it
    connects is answered its close code (1011, 4400, 4404) as ``status``."""
    if transport not in TRANSPORTS or code not in BLOCK_CODES:
        raise ValueError("not a refusal Latet knows")
    closes = transport == "ws-handshake" and (status in {1008, 1011} or 4000 <= status < 5000)
    if not isinstance(reason, str) or isinstance(status, bool) or not (400 <= status < 600 or closes):
        raise ValueError("a refusal says why, with an error's status")
    return _mint(Block(transport, destination, code, reason, status), None)


def observe(source: str, arena: Prepared) -> Observation:
    """What an arena held, recorded as an observation of ``source``: its record alone, each list a tuple of its own."""
    blocks = tuple(
        ObservedText(text, slot.kind, slot.from_model)
        for (slot, _, _), text in zip(arena.pending, arena.frozen_texts, strict=True)
    )
    return observed(
        source,
        arena.session,
        arena.block_reason,
        protocol=arena.protocol,
        uninspected=arena.uninspected,
        blocks=blocks,
        leaks=arena.leaks,
        outbound=arena.outbound,
        bypass=arena.bypass,
        history=arena.history,
    )


def observed(
    source: str,
    session: Session | None,
    reason: str | None,
    *,
    protocol: str | None = "anthropic",
    uninspected: Iterable[str] = (),
    blocks: Iterable[ObservedText] = (),
    leaks: Iterable[Leak] = (),
    outbound: Iterable[str] = (),
    bypass: bool = False,
    history: bool = False,
) -> Observation:
    """What the plugin's bridge read of a request it never sends (``source``: a route check, a tool call stopped, its
    final check), in the conversation ``session``, and why it stopped it (``reason``): each list a tuple of its own. The
    plugin's are Claude Code's requests, Anthropic's protocol."""
    if source not in OBSERVERS:
        raise ValueError("not an observer Latet knows")
    texts = tuple(blocks)
    if not all(type(text) is ObservedText for text in texts):
        raise TypeError("an observation's texts are observed texts")
    return Observation(
        source,
        protocol,
        reason,
        tuple(uninspected),
        tuple(leaks),
        tuple(outbound),
        bypass,
        texts,
        history,
        session,
    )


def sendable(decision: object, arena: Prepared | None, envelope: Envelope) -> Forward | Pass:
    """``decision``, when it may send what it carries for the request ``envelope`` is (the exchange's own: the very
    envelope it was finalized for) and ``arena`` (the arena it was finalized from; None for a request that had none):
    minted here and never written over, not a refusal, its proof a complete proof of that envelope or its evidence
    admitted, its payload what its arena (or, without one, its request) leaves with, and its arena unchanged since.
    Raises ``NotSendable`` with the code of what it is not."""
    if not isinstance(decision, Forward | Pass | Block):
        raise NotSendable("not-a-decision")
    if type(decision) not in (Forward, Pass, Block):
        raise NotSendable("unknown")
    with _MINTING:
        binding = _MINTED.get(decision)
    if binding is None:
        raise NotSendable("unminted")
    if any(now is not was for now, was in zip(_values(decision), binding.values, strict=True)):
        raise NotSendable("tampered")
    if isinstance(decision, Block):
        raise NotSendable("block")
    if decision.envelope is not envelope:
        raise NotSendable("foreign")
    if decision.policy is not current():
        raise NotSendable("policy")
    if envelope.transport != "ws-frame" and decision.envelope_proof is None:
        raise NotSendable("proof-incomplete")
    if isinstance(decision, Forward) and not decision.proof.proves(envelope):
        raise NotSendable("proof-incomplete")
    if isinstance(decision, Pass) and not _admitted(decision.evidence, decision.policy):
        raise NotSendable("evidence")
    if decision.envelope_proof is not None and not decision.envelope_proof.proves(envelope):
        raise NotSendable("proof-incomplete")
    seal = binding.seal
    if seal is None:
        if arena is not None:
            raise NotSendable("foreign")
        if decision.payload != envelope.body or decision.response != ResponsePlan():
            raise NotSendable("payload")
        return decision
    if arena is None or getattr(arena, "sealed", None) is not seal:  # what is no arena has no seal: foreign too
        raise NotSendable("foreign")
    if decision.payload is not seal.payload or (envelope.transport == "http" and decision.payload is not seal.body):
        raise NotSendable("payload")
    if not _unchanged(arena, seal) or decision.response != response_plan(arena):
        raise NotSendable("stale")
    if (
        arena.authority is None
        or not arena.authority.valid()
        or (arena.route_authority is not None and not arena.route_authority.valid())
        or (arena.control_current is not None and not arena.control_current())
        or not arena.opaque_current()
    ):
        raise NotSendable("stale")
    return decision


def still_current(decision: Forward | Pass, arena: Prepared) -> None:
    """Last synchronous gate of an already authorized immutable payload, constant in body size.

    Authorization checked every source slot and proof occurrence. Their immutable field identities and worker
    generation bind that result here; only the frozen payload can be sent. Do not traverse the source document again
    after an await, refresh a receipt, or spend another lookup allowance at the transport boundary.
    """
    with _MINTING:
        binding = _MINTED.get(decision)
    if binding is None:
        raise NotSendable("unminted")
    if any(now is not was for now, was in zip(_values(decision), binding.values, strict=True)):
        raise NotSendable("tampered")
    if decision.policy is not current():
        raise NotSendable("policy")
    seal = binding.seal
    if seal is None or arena.sealed is not seal:
        raise NotSendable("foreign")
    if decision.payload is not seal.payload:
        raise NotSendable("payload")
    if (
        arena.body is not seal.body
        or arena.document is not seal.document
        or arena.session is not seal.session
        or arena.freeze_generation is not seal.generation
        or any(now is not was for now, was in zip(_state(arena), seal.state, strict=True))
        or decision.response != response_plan(arena)
        or arena.authority is None
        or not arena.authority.valid()
        or (arena.route_authority is not None and not arena.route_authority.valid())
        or (arena.control_current is not None and not arena.control_current())
        or not arena.opaque_current()
    ):
        raise NotSendable("stale")
