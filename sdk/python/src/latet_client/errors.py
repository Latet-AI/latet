"""What goes wrong in a call, as errors. The server's refusal is a :class:`LatetError` of the HTTP status it answered
and the words it gave; the statuses a program acts on are classes of their own; a call that does not reach the server is
a :class:`ConnectionFailed`. The token of a call is in none of them: not in the message, not in the arguments, not in a
``repr``, and not in anything an error holds (a failed connection is not chained, because ``httpx``'s own error carries
the request and its ``Authorization`` header)."""

from __future__ import annotations

__all__ = [
    "Blocked",
    "ClearTextWarning",
    "ConnectionFailed",
    "Forbidden",
    "LatetError",
    "RateLimited",
    "TooLarge",
    "Unauthorized",
    "Unavailable",
    "VaultFull",
]


class LatetError(Exception):
    """The server refused a call, or answered something the applications API does not answer. ``status`` is the HTTP
    status (200 for an answer that was not the API's) and ``message`` the server's words for it, or ``HTTP <status>``
    when it gave none. A 400, 404, 405, 422 or 500 is this class itself; the statuses below are its subclasses."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(status, message)
        self.status = status
        self.message = message

    def __str__(self) -> str:
        return f"{self.status}: {self.message}"


class ConnectionFailed(LatetError):
    """The call did not go through: the server could not be reached, the connection broke or the call timed out. The
    status is 0 and the message names the kind of failure (``ConnectError``, ``ReadTimeout``...) and the server's host.
    It is not tried again by the client, and it is raised without ``__cause__`` or ``__context__``: ``httpx``'s own
    error holds the request, and the request holds the token."""


class Unauthorized(LatetError):
    """401: no token, or one the server does not know (revoked, rotated, mistyped)."""


class Forbidden(LatetError):
    """403: the token lacks the permission of the operation (scan, mask or unmask), or it is a person's token, which
    the applications API does not take."""


class TooLarge(LatetError):
    """413: the call is larger than the server takes (its ``apps_request_mb``, 8 MiB unless its settings say otherwise,
    never more than its ``max_request_mb``). Send fewer bytes in a call: the client's ``max_bytes``."""


class RateLimited(LatetError):
    """429: too many calls, or too many refused tokens, from this address. ``retry_after`` is the seconds the server
    asked to wait, ``None`` when it did not say. The client has waited and tried again ``retries`` times before it
    raises this."""

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(status, message)
        self.retry_after = retry_after


class Unavailable(LatetError):
    """503: a store of the server does not answer, or its administration log did not take an unmask (nothing was
    restored). The client has waited and tried again ``retries`` times before it raises this."""


class VaultFull(LatetError):
    """507: the API key's vault holds the most values it may, so the call masked nothing and nothing left in clear. An
    administrator raises the limit (``latet server keys limit``)."""


class Blocked(Exception):
    """A text the server did not answer: its local reviewer stopped it, or its guard against dumps withheld it.
    ``index`` is the place of the text in what was sent, ``reason`` the server's words (they name no value)."""

    def __init__(self, index: int, reason: str) -> None:
        super().__init__(index, reason)
        self.index = index
        self.reason = reason

    def __str__(self) -> str:
        return f"text {self.index} was blocked: {self.reason}"


class ClearTextWarning(UserWarning):
    """The client was made for a plain ``http://`` address of another computer: the token and the texts travel in
    clear. Use an ``https://`` address. An address of this computer (``localhost``, ``127.0.0.1``, ``::1``) is not
    warned of."""


# The class of each status that has one.
BY_STATUS: dict[int, type[LatetError]] = {
    401: Unauthorized,
    403: Forbidden,
    413: TooLarge,
    429: RateLimited,
    503: Unavailable,
    507: VaultFull,
}
