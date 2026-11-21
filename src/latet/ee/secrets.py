"""Secrets of the Enterprise settings (a client secret for sign-in, a token for a SIEM): settings name a secret, never
hold it. They are a Latet server's alone, sealed with the master key in its PostgreSQL
(latet.ee.server.DatabaseSecrets). A secret's value is never in a log line or an error."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

if TYPE_CHECKING:
    from psycopg import Connection

MAX_BYTES: Final = 64 * 1024  # a client secret or a token is a few dozen bytes, a private key a few kilobytes
_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
# The names Windows keeps for its devices in every folder: a file of one of them would be the device.
_DEVICES: Final = frozenset({"con", "prn", "aux", "nul", *(f"{port}{n}" for port in ("com", "lpt") for n in range(10))})


def secret_name(name: str) -> str:
    """``name``, checked: a plain name, never a path, nor a name Windows keeps for a device."""
    if not _NAME.fullmatch(name):
        raise ValueError("a secret's name is lowercase letters, digits and hyphens, 63 at most")
    if name in _DEVICES:
        raise ValueError("a secret's name is not one Windows keeps for a device (con, prn, aux, nul, com0-9, lpt0-9)")
    return name


class Secrets(Protocol):
    # ``bound``: what a secret may be used for, sealed with it (the client secret: its issuer's origin); ``conn``: the
    # caller's transaction, beside the entry that records the change.
    def save(
        self, name: str, value: str, *, bound: str | None = None, conn: Connection[Any] | None = None
    ) -> Literal["server"]: ...  # where it went
    def load(self, name: str, *, bound: str | None = None) -> str | None: ...
    def remove(self, name: str, *, conn: Connection[Any] | None = None) -> bool: ...  # whether there was one


def value_problem(value: object) -> str | None:
    """Why ``value`` cannot be kept as a secret, in words that hold nothing of it; None when it can."""
    if not isinstance(value, str):
        return "a secret is text"
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate: the error holds the whole value
        return "a secret is text that UTF-8 can write"
    if size > MAX_BYTES:  # what could not be read back is not kept
        return f"a secret holds {MAX_BYTES // 1024} KB at most"
    return None
