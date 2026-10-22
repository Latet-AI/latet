"""What a log line may hold, in every part of Latet: a path or a line without its secrets (``redacted``), an error
by its type (``described``), what anyone can make happen at will once a minute (``Paced``). A module of its own,
importing nothing of Latet, so that the engine, the settings, vision, storage, the gateway and the server all read it
and none of them imports another part for it."""

from __future__ import annotations

import errno
import os
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable

# Where a signing route carries its key, or a session's id (Bedrock's route, and the bridge's own, which holds
# the session): never in a log line.
_SIGNING_SEGMENT = re.compile(
    r"((?:/bedrock/[^/?#]+/(?:profile/[^/?#]+|session)|/_latet/engine/v1/aws-sessions)/)[^/?#]+"
)
# On a Latet server: an identity's route key (in any case, escaped in a query or in JSON too), a one-time code and the
# credentials a query or a form can carry (its ``=`` escaped too, as in a link inside another), Latet's secrets wherever
# they start, the values of its token headers and of the other credential headers (as a header line, folded on the
# next ones or written with no separator before its scheme, a JSON pair, escaped JSON's or a pair of a list, as text or
# bytes), a JSON field that names a credential, libpq's ``password=`` in key=value form, and the password in an address
# (PostgreSQL's, Valkey's), which runs to the address's last ``@``: never in a log line either. Each pattern starts with
# fixed text and reads what follows it once, so that a line of any length, whoever wrote it, costs time in proportion to
# it.
_ROUTE = re.compile(r"(?i)((?:/|%2F|\\/)u(?:/|%2F|\\/))[^/?#\s\"'%\\&]+")
_CREDENTIAL_NAMES = (
    r"code|key|api[-_]?key|token|access_token|refresh_token|id_token|client_secret|password|sig|signature"
    r"|x-amz-(?:signature|credential|security-token)|x-goog-(?:signature|credential)"
)
_QUERY = re.compile(rf"(?i)((?:[?&;'\"]|%3F|%26)(?:{_CREDENTIAL_NAMES})(?:=|%3D))[^&#\s\"']+")
_SECRET = re.compile(r"(latet_(?:pat|key|adm|ck)_)[A-Za-z0-9_-]+")
_HEADER = re.compile(
    r"(?i)(x-latet-(?:token|admin|computer-key)\\?['\"]?\s*(?:[:=]|,)\s*(?:b?\\?['\"])?)[^\s\"',;})\]\\]+"
)
_CREDENTIAL = re.compile(
    r"(?i)\b((?:proxy-)?authorization|x-api-key|api-key|x-amz-security-token|cookie|set-cookie)"
    r"(\\?['\"]?\s*(?:[:=]|,)\s*(?:b?\\?['\"])?"
    r"|[ \t]{1,8}(?=(?:bearer|basic|digest|negotiate|token|aws4-hmac-sha256)[ \t]))"
    r"[^\r\n\"']+(?:\r?\n[ \t][^\r\n\"']*)*"
)
_JSON_CREDENTIAL = re.compile(
    r"(?i)(\\?\"(?:api[-_]?key|password|passwd|secret|client_secret|token|access_token|refresh_token|id_token)\\?\""
    r"[ \t]{0,8}:[ \t]{0,8}\\?\")(?:[^\"\\\r\n]|\\.){0,4096}"
)
_CONNINFO = re.compile(r"(?i)(\b(?:ssl)?password[ \t]{0,8}=[ \t]{0,8})(?:'(?:[^'\\\r\n]|\\.){0,4096}'?|[^\s'\"&]+)")
_PASSWORD = re.compile(r"(://[^\s:/@]*:)[^\s/]*@")
SAY_EVERY = 60.0  # seconds between two lines about what anyone can make happen at will


def redacted(path: str) -> str:
    """A path, or a log line, as a log may show it: the key of a signing route or the id of its session, an
    identity's route key, a one-time code and a query's credentials, Latet's secrets, the values of its token headers
    and of the other credential headers, a JSON field's or a key=value pair's that names a credential, and the
    password in an address, left out."""
    path = _SIGNING_SEGMENT.sub(r"\1***", path)
    path = _ROUTE.sub(r"\1***", path)
    path = _QUERY.sub(r"\1***", path)
    path = _SECRET.sub(r"\1***", path)
    path = _HEADER.sub(r"\1***", path)
    path = _CREDENTIAL.sub(r"\1\2***", path)
    path = _JSON_CREDENTIAL.sub(r"\1***", path)
    path = _CONNINFO.sub(r"\1***", path)
    return _PASSWORD.sub(r"\1***@", path)


def described(error: BaseException) -> str:
    """An error as a log may name it: its type, and for an error of the operating system the C library's fixed text for
    its number (``FileNotFoundError (No such file or directory)``, ``OSError (No space left on device)``); never its own
    words, nor the file it names, which can hold a person's name, a folder or a value. Not for the numbers that are not
    the operating system's, whatever the C library would read in them: OpenSSL's (``ssl.SSLError``), the resolver's
    (``socket.gaierror``, ``socket.herror``)."""
    name = type(error).__name__
    if not isinstance(error, OSError) or isinstance(error, ssl.SSLError | socket.gaierror | socket.herror):
        return name
    number = error.errno
    if not isinstance(number, int) or isinstance(number, bool) or number not in errno.errorcode:
        return name
    return f"{name} ({os.strerror(number)})"


class Paced:
    """Something anyone can make happen at will, as a log says it: the first time at once, then at most once every
    ``every`` seconds, with how many times it happened since the line before; and, when asked (``flush``), how many
    times it happened that no line said yet. Safe across threads."""

    def __init__(self, every: float = SAY_EVERY, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._every, self._clock = every, clock
        self._lock = threading.Lock()
        self._said: float | None = None
        self._unsaid = 0

    def due(self) -> int:
        """It happened once more: how many times to say now (this one and those since the line before), or 0 while the
        line before was said less than ``every`` seconds ago."""
        now = self._clock()
        with self._lock:
            self._unsaid += 1
            if self._said is not None and now - self._said < self._every:
                return 0
            self._said, count, self._unsaid = now, self._unsaid, 0
        return count

    def flush(self) -> int:
        """How many times it happened that no line said yet (0: none), now taken as said: what a beat writes once a
        minute, so that a burst is told when it ends rather than when it happens again."""
        now = self._clock()
        with self._lock:
            count, self._unsaid = self._unsaid, 0
            if count:
                self._said = now
        return count
