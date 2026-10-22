"""Which part of Latet made a replacement, and how the value was found: the dashboard shows both.

A rule id names the detector that found a value (``pii:email``, ``gitleaks:github-pat``,
``recognizer:badge``). A few ids say instead how a value already known was found again; for those
the vault keeps the rule of the value's first sighting.
"""

from __future__ import annotations

from typing import Any

from latet.engine.detectors.ids import ID_TYPES
from latet.engine.detectors.structural import _key_kind

# Rule ids for a value found again, mapped to how: a value already in the vault, another occurrence
# in the same text, a surrogate the client sent back, masked by the Claude Code plugin before the
# daemon saw the request, or restored in an answer.
AGAIN = {
    "known-values": "known",
    "vault": "known",
    "repeat": "repeat",
    "echo": "echo",
    "plugin": "plugin",
    "restore": "restore",
}

MODULES = (
    "secrets",  # gitleaks rules: tokens and keys in known formats
    "secret-structure",  # assignments, private keys, credentials in URLs and headers
    "personal",  # e-mail, phone, IBAN, card, IP, crypto wallet
    "ids",  # national ID and tax numbers, VAT numbers
    "identity",  # this machine's user, host and git author
    "terms",  # the user's own values
    "recognizers",  # the user's own formats
    "names",  # the local name model
    "reviewer",  # the local language model
    "live",  # you, in the Live Reviewer
    "fields",  # the user's own field rules
    "data",  # strict data mode: every value of a table, JSON or key=value
    "other",
)

_PREFIX = {
    "latet-credentials": "secrets",
    "gitleaks": "secrets",
    "assignment": "secret-structure",
    "private-key": "secret-structure",
    "credentials": "secret-structure",
    "identity": "identity",
    "terms": "terms",
    "recognizer": "recognizers",
    "ner": "names",
    "llm": "reviewer",
    "human": "live",
    "field": "fields",
    "field-secret": "secret-structure",
    "strict": "data",
}


def kept(rule: str | None) -> str | None:
    """``rule`` as Latet keeps it in clear: the vault's rule of each value, beside its sealed original, and what stopped
    a request in a computer's history. A rule that finds a value by its field names the field, and a field's name can
    be a value itself (a JSON key that is an address, a column named after a person): it is named by its kind alone
    (``field-secret:<field>`` is ``field-secret:password``, ``:secret`` or ``:connection``, as the name says), or cut
    (``strict:<field>`` is ``strict``, ``field:<name>`` is ``field``). Any other rule is kept as it is. What a text was
    masked with, sealed wherever it is kept, still names the field."""
    if not rule:
        return rule
    prefix, _, field = rule.partition(":")
    if prefix in ("strict", "field"):
        return prefix
    if prefix == "field-secret" and field not in _SECRET_KINDS:
        return f"field-secret:{_key_kind(field) or 'secret'}"
    return rule


_SECRET_KINDS = frozenset({"password", "secret", "connection"})  # what ``_key_kind`` says a field's name holds


def module_of(rule: str | None, etype: str | None = None) -> str:
    """The module a detector rule belongs to (``other`` when the rule is unknown)."""
    if not rule:
        return "other"
    prefix = rule.split(":", 1)[0]
    if prefix == "pii":
        return "ids" if etype in ID_TYPES or etype == "VAT_NUMBER" else "personal"
    return _PREFIX.get(prefix, "other")


def origin(rule: str | None, first_rule: str | None, etype: str | None) -> dict[str, Any]:
    """``rule``, ``via`` and ``module`` of one replacement.

    ``rule`` is the id the replacement carries; ``first_rule`` is the rule the vault kept for the
    value. A value found again shows the rule that found it first.
    """
    via = AGAIN.get(rule or "")
    if via is None:
        return {"rule": rule, "via": "detector", "module": module_of(rule, etype)}
    found = first_rule if first_rule and first_rule not in AGAIN else None
    return {"rule": found, "via": via, "module": module_of(found, etype)}
