"""The events of what happens in Latet, for a company's reports and security systems (E4 keeps and sends them).
Counts, categories, codes and ids only: never a value, a text, a fake, a path or a session key. ``make_event`` checks
the shape of each field, so a text cannot go where a code or an id goes."""

from __future__ import annotations

import re
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

EVENT_TYPES: Final = (
    "exchange", "stopped", "approval", "change", "policy", "sync", "sign-in", "license", "admin", "apps",
)  # fmt: skip
EVENT_VERSION: Final = 1
# Every field an event has, with its value when not given (masked: {} once made).
_DEFAULTS: Final[Mapping[str, Any]] = {
    "identity": None,  # a person or an API key: "u_" + its id
    "kind": None,  # person or key
    "group": None,  # the identity's first group (E3): "g_" + its id
    "agent": None,
    "project": None,
    "masked": None,
    "stopped": 0,
    "reason": None,
    "outcome": None,
    "by": None,  # who decided (E5): an identity
    "rule": None,
    "chain": None,  # an admin event's place in the administration log's chain (E4)
}
_CODE: Final = re.compile(r"[a-z0-9][a-z0-9._:-]{0,79}")
CODE: Final = _CODE  # what a code is: E4's check of every kept event takes the same (M10)
_TYPE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_IDENTITY: Final = re.compile(r"u_[a-z0-9]{1,32}")
_IDS: Final = {"identity": _IDENTITY, "by": _IDENTITY, "group": re.compile(r"g_[a-z0-9]{1,32}")}
_HASH: Final = re.compile(r"[0-9a-f]{64}")
_CHAIN: Final = "an event's chain is {prev, hash}: the administration log's hashes, 64 hexadecimal digits each"
# A project's name holds no separator of a path (/ and the backslash) and no control character: an event never holds a
# path, nor a line of something else.
_NOT_IN_A_NAME: Final = frozenset(["/", chr(0x5C), chr(0x7F), *map(chr, range(0x20))])
_MASKED: Final = "an event's masked counts values by type: {TYPE: count}"


def make_event(type: str, *, clock: Callable[[], float] = time.time, **fields: Any) -> dict[str, Any]:
    """An event of ``type`` (one of EVENT_TYPES) with ``fields`` (the schema's, the others left empty), its own id
    and its time. ValueError when a field has not the shape of the schema."""
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type!r}: one of {', '.join(EVENT_TYPES)}")
    unknown = sorted(set(fields) - set(_DEFAULTS))
    if unknown:
        raise ValueError(f"an event has no field {', '.join(unknown)}")
    stamp = datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    event: dict[str, Any] = {"v": EVENT_VERSION, "id": uuid.uuid4().hex, "ts": stamp, "type": type}
    event |= {name: fields.get(name, default) for name, default in _DEFAULTS.items()}
    if event["masked"] is not None and not isinstance(event["masked"], Mapping):
        raise ValueError(_MASKED)
    event["masked"] = dict(event["masked"] or {})
    _check(event)
    return event


def _check(event: dict[str, Any]) -> None:
    for name in ("agent", "reason", "outcome", "rule"):
        value = event[name]
        if value is not None and not (isinstance(value, str) and _CODE.fullmatch(value)):
            raise ValueError(f"an event's {name} is a code (lowercase, no spaces), never a text")
    for name, pattern in _IDS.items():
        value = event[name]
        if value is not None and not (isinstance(value, str) and pattern.fullmatch(value)):
            raise ValueError(f"an event's {name} is an id")
    if event["kind"] not in (None, "person", "key"):
        raise ValueError("an event's kind is person or key")
    project = event["project"]
    if project is not None and not (
        isinstance(project, str) and 0 < len(project) <= 200 and _NOT_IN_A_NAME.isdisjoint(project)
    ):
        raise ValueError("an event's project is a name of 200 characters at most, never a path")
    if not all(_count(n) and isinstance(k, str) and _TYPE.fullmatch(k) for k, n in event["masked"].items()):
        raise ValueError(_MASKED)
    if not _count(event["stopped"]):
        raise ValueError("an event's stopped is a count")
    chain = event["chain"]
    if chain is not None:
        if not isinstance(chain, Mapping) or set(chain) != {"prev", "hash"}:
            raise ValueError(_CHAIN)
        prev, found = chain["prev"], chain["hash"]
        if not (isinstance(prev, str) and (prev == "" or _HASH.fullmatch(prev))):
            raise ValueError(_CHAIN)
        if not (isinstance(found, str) and _HASH.fullmatch(found)):
            raise ValueError(_CHAIN)
        event["chain"] = {"prev": prev, "hash": found}  # a copy: the caller's mapping is never kept


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# --- what Community code records (E4) ------------------------------------------------------------------------------

# The fields of an event, in the order make_event gives them (the interfaces' Events section): no other reaches the
# event log or the SIEM.
FIELDS: Final[tuple[str, ...]] = tuple(make_event("exchange", clock=lambda: 0.0))
# Why Latet stopped a request, as a `stopped` event's `reason` says it: Latet's own text is never copied (it holds
# previews such as `EMAIL[21]`). In the order `stop_code` tries them: the flags and the path of a plugin's stop, the
# reasons Latet worded for one cause (a leak found in the same request does not hide them), then `leak`, whose flag a
# request carries whatever stopped it, and the flag of unread content. `settings-changed` (0.11.1's) comes before the
# auto mode reviewer's flag, which a reviewer's request carries whatever stopped it, and before AWS signing.
STOP_CODES: Final = (
    "route-bypass", "outbound-tool", "history", "live-reviewer", "local-reviewer", "settings-changed",
    "auto-mode-review", "aws-signing", "unreadable", "not-messages", "masking-failed", "unread-request", "too-large",
    "leak", "unread", "other",
)  # fmt: skip
# The applications API's calls an `apps` event is made for (R7): unmask and forget are entries of the administration
# log, which reach the SIEM as `admin` events, never twice.
APPS_OPERATIONS: Final = ("scan", "mask")
# What an event names a project whose name holds a term its space masks (R3 of the final security review's re-review)
# where no key is at hand to make one of its own (the edition's ``hidden_project``): one code for every such project.
# On a server, Latet Enterprise names each by `masked-` and 8 characters keyed with the server's events key.
HIDDEN_PROJECT: Final = "masked"
HIDDEN_SHAPE: Final = re.compile(r"masked(?:-[a-z2-7]{8})?")  # the names a hidden project goes by, and no other
_NOT_CODE: Final = re.compile(r"[^a-z0-9._:-]+")
_CODE_LONGEST: Final = 80  # what _CODE takes
_NAME_LONGEST: Final = 200  # what a project's name takes in an event
_SEPARATORS: Final = ("/", chr(0x5C))  # a path's: a name that holds one is a path
# A token's id, as the server makes one (`latet.server.tokens.new_id("t_")`): its secrets ("latet_key_...") are no id.
_TOKEN_ID: Final = re.compile(r"t_[a-z2-7]{10}")
# How the gateway words the refusals of what Latet does not read (`gateway/mediate.py`: `_unread_body`, which since 0.11
# words the endpoints it does not read too, `_NOT_AN_OBJECT`, `_unread_fields`, `_UNREAD_FRAME`), and of a body that
# gives a key twice (`_TWICE`): the start of each. The tests build the gateway's own words, so a reword there fails a
# test, not turns into `other`.
_UNREAD_REQUEST: Final = (
    "Latet reads the body of a model's call",
    "Latet reads a model's call as a JSON object",
    "Latet reads the content of a model's call",
    "Latet reads the turns of a WebSocket",
)
_TWICE: Final = "this request's body gives a key twice"
# Latet 0.11's refusals of a request a part of which no request profile names, or that it could not account for: a
# body's, worded by `gateway/body_coverage.py`'s `BodyRefused` (`request body refused (<category>)`, its shape one Latet
# could not read, and the coverage's limit), and the others by their code alone (`gateway.outcome.BLOCK_CODES`, which
# the recorder hands an event's summary as `block_code`): an envelope's, a proof's, evidence's.
_BODY_REFUSED: Final = ("request body refused (", "request coverage limit exceeded")
_BODY_SHAPE: Final = "request body refused (shape)"
_UNCOVERED: Final = ("envelope-", "body-")
_UNCOVERED_CODES: Final = frozenset({"proof-incomplete", "proof-foreign", "evidence", "coverage-limit"})
# Latet 0.11.1's refusal of a request whose settings changed after it was prepared, which is not prepared again (its
# route changed, or it was reviewed under the settings before; on Enterprise, a sync that changed the terms): its code
# (`gateway/forward.py`, `_SETTINGS_MOVED` its words), never a part left unaccounted for.
_SETTINGS_CHANGED: Final = "settings-changed"
# A value in clear in a request's header or query parameter (the final check's, or a profile's safe text): a leak.
_SENSITIVE: Final = "envelope-sensitive"
# The errors a request the transport refused to send is recorded with (``gateway/forward.py``: after an AWS attempt
# it keeps no refusal code of its own): its coverage limit, and evidence that expired.
_UNSENT_ERRORS: Final = frozenset({"coverage-limit", "opaque-expired"})
# How the gateway words a request whose body would decode past a server's bound (`gateway/forward.py`: `too_large`,
# from `DecodedTooLarge`): the start of it. The tests build it with the gateway's own words.
_TOO_LARGE: Final = "the request body is larger than "
_NOT_TYPE: Final = re.compile(r"[^A-Z0-9_]+")
_TYPE_LONGEST: Final = 64  # what _TYPE takes
_NO_TYPE: Final = "CUSTOM"  # a term's type when nothing of its label is left: the settings' own default


def code_of(value: object, limit: int = 40) -> str | None:
    """``value`` as a code an event may carry (an agent's name, an action of the administration log, what it acted
    on): lowercase letters, digits, dots, colons, dashes and underscores, beginning with a letter or a digit as every
    code of an event does, at most ``limit`` (and 80, an event's longest); None when nothing of it is left. It gives a
    value the shape of a code and does not hide what the value is: its callers pass ids, names and actions Latet wrote,
    never a secret (``apps_event`` checks the token's id for that)."""
    text = _NOT_CODE.sub("", str(value or "").lower()).lstrip("._:-")[: max(0, min(limit, _CODE_LONGEST))]
    return text or None


def name_of(value: object, limit: int = _NAME_LONGEST) -> str | None:
    """A project's name as an event carries it: printable characters only, at most ``limit`` (and 200, an event's
    longest); None when empty, and when it holds a separator of a path (``/`` or the backslash): a folder's name holds
    none, so what holds one is a path (a drive's root), and an event never carries a path."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    if any(separator in text for separator in _SEPARATORS):
        return None
    text = text[: max(0, min(limit, _NAME_LONGEST))].strip()
    return text or None


def type_code(value: object) -> str:
    """A type of value as an event names it (``[A-Z][A-Z0-9_]{0,63}``): Latet's own types as they are, and a term's,
    which is the label a person gave it, made one: its letters without their accents (NFKD, then ASCII alone),
    uppercase, each run of anything else one ``_``, none at either end, ``T_`` before one that does not begin with a
    letter, 64 characters at most; ``CUSTOM`` (the settings' own default) when nothing is left. Two labels can make one
    code: their counts add up (``_masked_counts``)."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").upper()
    text = _NOT_TYPE.sub("_", text).strip("_")
    if not text:
        return _NO_TYPE
    if not text[0].isalpha():
        text = "T_" + text
    return text[:_TYPE_LONGEST].rstrip("_")


def _masked_counts(counts: Mapping[Any, Any] | None) -> dict[str, int]:
    """Values masked by type as an event counts them: each type made a code (``type_code``), the counts of those that
    make the same one added up, and none that is not above 0. ValueError or TypeError for a count that is no number."""
    out: dict[str, int] = {}
    for etype, n in (counts or {}).items():
        count = int(n)
        if count > 0:
            code = type_code(etype)
            out[code] = out.get(code, 0) + count
    return out


def stop_code(summary: Mapping[str, Any]) -> str:
    """Why the request a recorder's ``summary`` describes was stopped: read from its flags, its path, the start of the
    reason Latet wrote for it and the code of its refusal (``block_code``, Latet 0.11's), which are Latet's own."""
    flags = set(summary.get("flags") or [])
    reason = str(summary.get("block_reason") or "")
    code = str(summary.get("block_code") or "")
    sensitive = code == _SENSITIVE
    uncovered = not sensitive and (
        code.startswith(_UNCOVERED)
        or code in _UNCOVERED_CODES
        or reason.startswith(_BODY_REFUSED)
        or summary.get("error") in _UNSENT_ERRORS
    )
    checks = (
        ("route-bypass", "route-bypass" in flags),
        ("outbound-tool", summary.get("path") == "/plugin/tool-call"),
        ("history", "history" in flags),
        ("live-reviewer", reason.startswith("Latet did not send this request:")),
        ("local-reviewer", reason.startswith("the local reviewer")),
        ("settings-changed", code == _SETTINGS_CHANGED),
        ("auto-mode-review", "auto-mode-review" in flags),
        # Signed, with an error: its AWS login, unless Latet refused it for a code of its own (above, ``uncovered``).
        ("aws-signing", bool(summary.get("signed")) and "error" in flags and not uncovered and not sensitive),
        ("unreadable", reason.startswith(("unreadable request body", _TWICE, _BODY_SHAPE))),
        ("not-messages", reason.startswith(("Latet reads Claude's messages", "Latet cannot read this token count"))),
        ("masking-failed", reason == "masking failed" or reason.startswith("masking failed: ")),
        ("unread-request", reason.startswith(_UNREAD_REQUEST) or uncovered),
        ("too-large", reason.startswith(_TOO_LARGE)),
        ("leak", "leak" in flags or sensitive),
        ("unread", any(flag.startswith("uninspected:") for flag in flags)),
    )
    return next((code for code, found in checks if found), "other")


def exchange_event(summary: Mapping[str, Any], identity: str | None = None, kind: str | None = None) -> dict[str, Any]:
    """The event of one exchange (``summary``, as the recorder makes it): the values masked in it as the Stats page
    counts them (once in a conversation), its agent and project, why Latet stopped it, and the identity it was sent for
    (a person or an API key, on a server). Its time is the exchange's. The summary's free texts (session, path, host,
    model, reason) are read, never copied."""
    stopped = "blocked" in (summary.get("flags") or [])
    masked = _masked_counts(summary.get("new_by_type"))
    if stopped:
        reason: str | None = stop_code(summary)
    else:
        reason = "uncounted" if summary.get("uncounted") else None
    at = float(summary["ts"])
    return make_event(
        "stopped" if stopped else "exchange",
        clock=lambda: at,
        identity=identity,
        kind=kind if identity is not None else None,
        agent=code_of(summary.get("agent")),
        project=name_of((summary.get("project") or {}).get("name")),
        masked=masked,
        stopped=int(stopped),
        reason=reason,
    )


def apps_event(
    operation: str,
    *,
    identity: str,
    token: str | None,
    masked: Mapping[str, int],
    stopped: int = 0,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """The event of one answered call of the applications API (0.10.0's ``scan`` or ``mask``, R7): the API key it was
    made with, the values a mask masked by type (a scan masks nothing: its event counts the call), the texts it did not
    answer, and the id of the token that made it (``t_...``). Its texts are never read here: the caller gives counts
    alone. ValueError (its words quote nothing given) for another operation, and for a token that is not an id: a
    secret is never written where an id goes."""
    if operation not in APPS_OPERATIONS:
        raise ValueError("an applications API event is a scan or mask: unmask and forget are in the administration log")
    if token and not (isinstance(token, str) and _TOKEN_ID.fullmatch(token)):
        raise ValueError("an applications API event names its token's id (t_ and 10 characters), never its secret")
    return make_event(
        "apps",
        clock=clock,
        identity=identity,
        kind="key",
        agent="apps",
        outcome=operation,
        masked=_masked_counts(masked),
        stopped=int(stopped),
        rule=token or None,
    )
