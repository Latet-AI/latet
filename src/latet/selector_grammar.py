"""The grammar of a body selector, a module of its own below the settings and the gateway: the settings hold an
exception's body components to it (``config.ExceptionComponent``), the gateway reads them by it (``body_selectors``,
``body_coverage``), and neither imports the other for it (``test_logline``).

A body selector is an exact JSON pointer (RFC 6901) typed by the value it names: ``/x_context/team`` of kind
``string`` names that one member and nothing beside or below it. Its only wildcard is an array index, a whole segment
``*``, which matches an element of an array and never an object's member; there is no key wildcard, no regex, no
parent object. At most 256 characters and 32 segments; a segment is printable text, ``~0`` and ``~1`` its only
escapes. A selector never names a credential member: a segment whose words say key, token, secret, password,
authorization, cookie, signature and the like is refused when the selector is made, whatever the body it would meet."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

KINDS = frozenset({"string", "number", "boolean", "null"})
MAX_POINTER = 256
MAX_SEGMENTS = 32
_ESCAPE = re.compile(r"~(?![01])")
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
# Words that name a credential (the secret header markers of headers.py among them), matched as whole words, so that
# "author" or "keyboard" stay members while "auth", "apiKey" and "refresh_token" are credentials.
_CREDENTIAL_WORDS = frozenset(
    {
        "key",
        "keys",
        "apikey",
        "token",
        "secret",
        "secrets",
        "password",
        "passwords",
        "passwd",
        "pwd",
        "passphrase",
        "credential",
        "credentials",
        "auth",
        "authorization",
        "authentication",
        "bearer",
        "cookie",
        "cookies",
        "signature",
        "attestation",
        "account",
        "jwt",
        "otp",
        "pin",
    }
)
# Run together without a separator, these still say credential.
_CREDENTIAL_JOINED = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "sessiontoken",
    "clientsecret",
    "privatekey",
    "secretkey",
    "password",
    "passwd",
    "credential",
    "authorization",
    "bearer",
)


class SelectorRefused(ValueError):
    """A selector, an exception's selectors or a revision outside the grammar. Its words name the rule broken, never
    the selector's text, which may hold what the member would carry."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("body selector refused (" + reason + ")")


def _credential(segment: str) -> bool:
    words = {word.lower() for word in _WORDS.findall(segment)}
    joined = "".join(c for c in segment.lower() if c.isalnum())
    return bool(words & _CREDENTIAL_WORDS) or any(marker in joined for marker in _CREDENTIAL_JOINED)


def pointer_segments(pointer: object, *, credentials: bool) -> tuple[str | None, ...]:
    """A pointer's decoded segments, None for the array index wildcard. ``credentials``: a segment that names a
    credential member is refused."""
    if not isinstance(pointer, str):
        raise SelectorRefused("shape")
    if not pointer.startswith("/"):
        raise SelectorRefused("pointer")  # "" is the whole body: no parent object
    if len(pointer) > MAX_POINTER:
        raise SelectorRefused("length")
    raw = pointer[1:].split("/")
    if len(raw) > MAX_SEGMENTS:
        raise SelectorRefused("segments")
    found: list[str | None] = []
    for part in raw:
        if part == "*":
            found.append(None)
            continue
        if not part:
            raise SelectorRefused("empty segment")
        if "*" in part:
            raise SelectorRefused("key wildcard")
        if not part.isprintable():
            raise SelectorRefused("character")
        if _ESCAPE.search(part):
            raise SelectorRefused("escape")
        key = part.replace("~1", "/").replace("~0", "~")
        if credentials and _credential(key):
            raise SelectorRefused("credential member")
        found.append(key)
    if found[0] is None:
        raise SelectorRefused("top-level wildcard")  # the body is an object: an index never names its member
    return tuple(found)


def selectable(path: Iterable[str | int]) -> bool:
    """Whether an exception's body selector can name the member at ``path`` (object keys and array indexes from the
    body's top, an index read as the array wildcard): the very grammar a selector is held to (``pointer_segments``,
    as `latet config add` holds an exception's pointer), so that a refusal never points to an exception that cannot
    be written (a credential's word or a ``*`` in a key, an unprintable key, a pointer too long or too deep)."""
    steps = []
    for step in path:
        if type(step) is int:
            steps.append("*")
        elif type(step) is str and "*" not in step:
            steps.append(step.replace("~", "~0").replace("/", "~1"))
        else:
            return False  # a key holding a `*` is never named: the grammar reads it as a wildcard or refuses it
    try:
        pointer_segments("/" + "/".join(steps), credentials=True)
    except SelectorRefused:
        return False
    return True


@dataclass(frozen=True, slots=True)
class BodySelector:
    """One exact, typed JSON pointer into a request's body; made only through this grammar (``SelectorRefused``)."""

    pointer: str
    kind: str
    segments: tuple[str | None, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not isinstance(self.pointer, str):
            raise SelectorRefused("shape")
        if self.kind not in KINDS:
            raise SelectorRefused("kind")
        object.__setattr__(self, "segments", pointer_segments(self.pointer, credentials=True))
