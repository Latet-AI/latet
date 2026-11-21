"""An event as the Enterprise Edition keeps and sends it: the fields of the schema (`latet.events.FIELDS`) and no
other, each of its kind. Everything the pipeline keeps goes through ``checked``: a field the schema does not have, or a
text where a code belongs, is refused. Each pattern is E1's (`latet.events.make_event`'s) or narrower, so that nothing
``make_event`` refuses is kept (M10); a person's pseudonym (``p_``, `reports.people: pseudonymous`) is the one form
added, which the pipeline writes in place of an id."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from latet.events import CODE, EVENT_TYPES, FIELDS

KINDS = ("person", "key")
MAX_KINDS = 200
MAX_COUNT = 1_000_000_000
MAX_PROJECT = 200  # an event's project, as E1's ``name_of`` makes one
_ID = re.compile(r"[0-9a-f]{32}")
_IDENTITY = re.compile(r"u_[a-z0-9]{1,32}|p_[a-z2-7]{10}")  # an identity's id (make_event's), or a person's pseudonym
_GROUP = re.compile(r"g_[a-z0-9]{1,32}")  # make_event's: a group's id
_TYPE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")  # make_event's (what ``type_code`` makes): EMAIL, IBAN, CLIENT_NAME
_CODE = CODE  # what make_event takes for a code (M10): lowercase, no space, never a text, 80 characters at most
_HASH = re.compile(r"[0-9a-f]{64}")  # a hash of the administration log's chain (R6)
# What a project's name never holds (E1's ``name_of``): a separator of a path, so that no event keeps a path.
_SEPARATORS = ("/", chr(0x5C))


class NotAnEvent(ValueError):
    """What ``checked`` refuses: not an event of the schema. A batch that holds one is refused whole, and would be
    refused again: whoever writes it drops it rather than trying it again, which a failure of the database is for."""


def iso(seconds: float) -> str:
    """A time as events write it: UTC, to the second."""
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(ts: str) -> float:
    """The seconds of an event's ``ts`` (ISO 8601, with ``Z`` or an offset)."""
    moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("a time without its zone")
    return moment.timestamp()


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_COUNT


def _project(value: object) -> str | None:
    """An event's project as E1 makes one: a name of printable characters, 200 at most, never a path."""
    if value is None:
        return None
    if not (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PROJECT
        and value.isprintable()
        and not any(separator in value for separator in _SEPARATORS)
    ):
        raise NotAnEvent("project is a name of 200 printable characters at most, never a path")
    return value


def checked(event: Mapping[str, Any]) -> dict[str, Any]:
    """``event`` with every field of the schema, in its order, each checked, its time in UTC and its zero counts left
    out. NotAnEvent (a ValueError): not an event of the schema, said in fixed words that never repeat a field's value
    nor a key it was given."""
    if not isinstance(event, Mapping):
        raise NotAnEvent("an event is a mapping of the schema's fields")
    unknown = set(event) - set(FIELDS)
    if unknown:  # how many, never their names: a key is whatever the mapping was given, and a log line says this
        raise NotAnEvent(f"fields the schema does not have: {len(unknown)}")

    def code(name: str, pattern: re.Pattern[str]) -> str | None:
        value = event.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise NotAnEvent(f"{name} is not a code")
        return value

    version = event.get("v")
    if type(version) is not int or version != 1:
        raise NotAnEvent("v is 1")
    if event.get("type") not in EVENT_TYPES:
        raise NotAnEvent(f"type is one of {', '.join(EVENT_TYPES)}")
    if not isinstance(event.get("id"), str) or not _ID.fullmatch(event["id"]):
        raise NotAnEvent("id is 32 hexadecimal digits")
    try:
        ts = iso(epoch(str(event.get("ts"))))
    except (ValueError, OverflowError, OSError):
        raise NotAnEvent("ts is a time in ISO 8601, with its zone") from None
    kind = event.get("kind")
    if kind is not None and kind not in KINDS:
        raise NotAnEvent("kind is person or key")
    masked = event.get("masked")
    masked = {} if masked is None else masked
    if not isinstance(masked, Mapping) or len(masked) > MAX_KINDS:
        raise NotAnEvent("masked counts values by type")
    for etype, n in masked.items():
        if not isinstance(etype, str) or not _TYPE.fullmatch(etype) or not _count(n):
            raise NotAnEvent("masked counts values by type")
    stopped = event.get("stopped")
    stopped = 0 if stopped is None else stopped
    if not _count(stopped):
        raise NotAnEvent("stopped is a count")
    chain = event.get("chain")
    if chain is not None:
        if not isinstance(chain, Mapping) or set(chain) != {"prev", "hash"}:
            raise NotAnEvent("chain is {prev, hash}")
        prev, found = chain["prev"], chain["hash"]
        if not (isinstance(prev, str) and (prev == "" or _HASH.fullmatch(prev))):
            raise NotAnEvent("chain is {prev, hash}: hashes of 64 hexadecimal digits")
        if not (isinstance(found, str) and _HASH.fullmatch(found)):
            raise NotAnEvent("chain is {prev, hash}: hashes of 64 hexadecimal digits")
        chain = {"prev": prev, "hash": found}
    out = {
        "v": 1,
        "id": event["id"],
        "ts": ts,
        "type": event["type"],
        "identity": code("identity", _IDENTITY),
        "kind": kind,
        "group": code("group", _GROUP),
        "agent": code("agent", _CODE),  # make_event's: an agent is a code (E1's ``code_of``)
        "project": _project(event.get("project")),
        "masked": {etype: n for etype, n in masked.items() if n},
        "stopped": stopped,
        "reason": code("reason", _CODE),
        "outcome": code("outcome", _CODE),
        "by": code("by", _IDENTITY),  # who decided (E5's decisions): a person's id, or their pseudonym
        "rule": code("rule", _CODE),
        "chain": chain,  # an admin event's place in the administration log's chain (R6); null for the others
    }
    return {name: out[name] for name in FIELDS}
