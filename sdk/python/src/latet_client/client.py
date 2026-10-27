"""The client of a Latet server's applications API: ``scan``, ``mask``, ``unmask`` and ``forget`` with an API key's
token, in :class:`Latet` and, with ``await``, in :class:`AsyncLatet`.

A call takes one text and answers one result, or takes a list of texts and answers a list in the same order. A long
list goes to the server in several calls, each of at most ``max_texts`` texts and ``max_bytes`` of JSON; a text larger
than ``max_bytes`` goes alone (the server answers 413 if it is past its own limit). The texts of one call are masked
together, so a value that a later text of the call teaches the server is masked in the earlier ones too; a value that
only a later call teaches is not, as with any two calls. ``forget`` takes values, each once, sent in calls bound the
same way, and answers one result: the counts of every call, summed.

The token stays in the client: it is in the ``Authorization`` header of each call and nowhere else, not in a ``repr``,
an exception's message or a log line (the client says what it does in the ``latet_client`` logger, without a token and
without a text). A call that does not go through raises :class:`ConnectionFailed`, not ``httpx``'s own error, which
carries the request and so its header."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from types import TracebackType
from typing import Any, Literal, TypeVar, overload

import httpx

from .errors import BY_STATUS, ClearTextWarning, ConnectionFailed, LatetError, RateLimited
from .results import (
    ForgetResult,
    MaskResult,
    ScanResult,
    UnmaskResult,
    forget_results,
    mask_results,
    scan_results,
    unmask_results,
)

__all__ = ["APPS_API", "AsyncLatet", "Kind", "Latet"]

log = logging.getLogger(__name__)

APPS_API = "/_latet/apps/v1"
Kind = Literal["prose", "data"]
KINDS = ("prose", "data")
MOST_WAIT = 30.0  # seconds waited at most before one more try, whatever the server asked
MOST_WORDS = 500  # characters kept of the server's words
_ADDRESS = "the server's address is a URL such as https://latet.example.com, with no user name or password in it"
_UNSENDABLE = "the token is empty, or holds a character a header cannot carry (a space, a line break)"
_SAFE = re.compile(r"[\x21-\x7e]+")
_SECONDS = re.compile(r"\d+(?:\.\d+)?")

T = TypeVar("T")
Reader = Callable[[Any, int], list[T]]


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


async def _asleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _address(url: str) -> httpx.URL:
    """The server's address, as ``httpx`` reads it, or ValueError."""
    if not isinstance(url, str):
        raise TypeError(_ADDRESS)
    try:
        found = httpx.URL(url)
    except httpx.InvalidURL:
        raise ValueError(_ADDRESS) from None  # never the address as given: it may hold a password
    if found.scheme not in ("http", "https") or not found.host or found.userinfo:
        raise ValueError(_ADDRESS)
    return found


def _secret(token: str) -> str:
    """The token without the line break a file ends with, or ValueError (that never quotes it)."""
    if not isinstance(token, str):
        raise TypeError("the token is a str")
    token = token.strip()
    if not _SAFE.fullmatch(token):
        raise ValueError(_UNSENDABLE)
    return token


def _listed(texts: str | Sequence[str], field: str = "texts") -> tuple[bool, list[str]]:
    """Whether ``texts`` is one text, and the texts as a list; a TypeError naming ``field`` for anything else."""
    wrong = f"{field} is a str, or a sequence of str"
    if isinstance(texts, str):
        return True, [texts]
    try:
        listed = list(texts)
    except TypeError:
        raise TypeError(wrong) from None
    if not all(isinstance(text, str) for text in listed):
        raise TypeError(wrong)
    return False, listed


def _seconds(header: str | None) -> float | None:
    """The seconds a ``Retry-After`` asks for; ``None`` for anything else than a number of them (a date is not read)."""
    found = (header or "").strip()
    return float(found) if _SECONDS.fullmatch(found) else None


def _written(texts: list[str], what: str = "text") -> list[bytes]:
    """Every text (``what``: or value) as a JSON string in UTF-8, before any call is made: a text UTF-8 cannot write (a
    lone surrogate, as a log read with ``errors="surrogateescape"`` holds) is a ValueError naming its place and nothing
    of it, and nothing has been sent."""
    parts: list[bytes] = []
    unwritable = None
    for index, text in enumerate(texts):
        try:
            parts.append(json.dumps(text, ensure_ascii=False).encode("utf-8"))
        except UnicodeEncodeError:
            unwritable = index
            break
    if unwritable is not None:  # outside the handler: the error, which holds the whole text, is not its context
        raise ValueError(f"{what} {unwritable} cannot be written as UTF-8 (a lone surrogate?): nothing was sent")
    return parts


def _bodies(
    parts: list[bytes], kind: Kind | None, max_texts: int, max_bytes: int, field: str = "texts"
) -> Iterator[tuple[bytes, int]]:
    """The bodies of the calls that carry the texts (as ``_written`` made them; a forget's values, ``field``) in order,
    each with how many it holds: at most ``max_texts`` of them and ``max_bytes`` bytes of compact JSON in UTF-8, as it
    is sent. A text that is past ``max_bytes`` alone makes a call of its own."""
    head = b'{"' + field.encode() + b'":['
    tail = b"]}" if kind is None else b'],"kind":' + json.dumps(kind).encode() + b"}"
    frame = len(head) + len(tail)
    batch: list[bytes] = []
    size = frame
    for part in parts:
        if batch and (len(batch) >= max_texts or size + 1 + len(part) > max_bytes):
            yield head + b",".join(batch) + tail, len(batch)
            batch, size = [], frame
        size += len(part) + (1 if batch else 0)
        batch.append(part)
    if batch:
        yield head + b",".join(batch) + tail, len(batch)


def _on_this_computer(host: str) -> bool:
    """Whether ``host`` is this computer: ``localhost`` or a loopback address (``127.0.0.0/8``, ``::1``)."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Wire:
    """What the two clients share: the address, the token, the limits, and how an answer is judged."""

    def __init__(self, url: str, token: str, *, timeout: float, retries: int, max_texts: int, max_bytes: int) -> None:
        if timeout <= 0 or retries < 0 or max_texts < 1 or max_bytes < 1:
            raise ValueError("timeout is above zero, retries is zero or more, max_texts and max_bytes are one or more")
        found = _address(url)
        prefix = found.path.rstrip("/")
        self._url = str(found.copy_with(path=prefix, query=None, fragment=None))
        self._urls = {
            operation: str(found.copy_with(path=f"{prefix}{APPS_API}/{operation}", query=None, fragment=None))
            for operation in ("scan", "mask", "unmask", "forget")
        }
        self._token = _secret(token)
        self._host = found.host
        if found.scheme == "http" and not _on_this_computer(found.host):
            message = f"the token and the texts travel in clear to {found.host} over http://: use an https:// address"
            warnings.warn(message, ClearTextWarning, stacklevel=3)  # at the line that made the client
        self._timeout = timeout
        self._retries = retries
        self._max_texts = max_texts
        self._max_bytes = max_bytes

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self._url!r})"

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token}",
            "content-type": "application/json",
            "accept": "application/json",
        }

    def _plan(self, texts: str | Sequence[str], kind: Kind | None) -> tuple[bool, Iterator[tuple[bytes, int]]]:
        """Whether ``texts`` is one text, and the bodies of the calls that carry it; the arguments are checked, and
        every text written, here, before any call."""
        if kind is not None and kind not in KINDS:
            raise ValueError("kind is prose (text people wrote) or data (logs, records, a program's output)")
        single, listed = _listed(texts)
        return single, _bodies(_written(listed), kind, self._max_texts, self._max_bytes)

    def _forgetting(self, values: str | Sequence[str]) -> Iterator[tuple[bytes, int]]:
        """The bodies of the calls that carry ``values`` to forget, each once, in the order given (a value sent again
        would be counted by a later call as not held); checked and written here, before any call."""
        _, listed = _listed(values, "values")
        once = list(dict.fromkeys(listed))
        return _bodies(_written(once, "value"), None, self._max_texts, self._max_bytes, "values")

    @staticmethod
    def _summed(found: list[ForgetResult]) -> ForgetResult:
        """The counts of every call of a forget, summed."""
        return ForgetResult(sum(r.forgotten for r in found), sum(r.unknown for r in found))

    def _unreachable(self, failure: str) -> ConnectionFailed:
        """The error of a call that did not go through, from the name of ``httpx``'s error alone."""
        return ConnectionFailed(0, f"the call to {self._host} did not go through ({failure})")

    def _after(self, response: httpx.Response, attempt: int) -> float | None:
        """``None`` for an answer to read; the seconds to wait before trying again, for a 429 or a 503 with tries left
        (the ``Retry-After`` the server gave, at most ``MOST_WAIT``, else 1, 2, 4... seconds); otherwise the refusal,
        raised."""
        status = response.status_code
        if status == 200:
            return None
        asked = _seconds(response.headers.get("retry-after"))
        if status in (429, 503) and attempt < self._retries:
            wait = min(asked if asked is not None else 2.0**attempt, MOST_WAIT)
            log.debug(
                "the server answered %s: trying again in %.1f s (%s of %s)", status, wait, attempt + 1, self._retries
            )
            return wait
        words = self._words(response)
        if status == 429:
            raise RateLimited(status, words, asked)
        raise BY_STATUS.get(status, LatetError)(status, words)

    def _words(self, response: httpx.Response) -> str:
        """The server's words for a refusal (``error``), printable and short, and never the token, whatever put it
        there (a proxy that quotes the request); ``HTTP <status>`` when it gave none."""
        try:
            body = response.json()
        except ValueError:
            body = None
        said = body.get("error") if isinstance(body, dict) else None
        words = said if isinstance(said, str) and said.strip() else f"HTTP {response.status_code}"
        if words == f"HTTP {response.status_code}" and 300 <= response.status_code < 400:
            words += (
                ": the server redirected the call, which the client does not follow (use the server's final address)"
            )
        words = words.replace(self._token, "[token]")
        return "".join(char for char in words if char.isprintable())[:MOST_WORDS]

    @staticmethod
    def _read(response: httpx.Response, count: int, read: Reader[T]) -> list[T]:
        """The ``count`` results an answer holds, or a ``LatetError`` when it is not the API's."""
        try:
            return read(response.json(), count)
        except ValueError:  # not JSON, or Malformed
            words = "the answer is not the applications API's: is this the address of a Latet server?"
            raise LatetError(response.status_code, words) from None


class Latet(_Wire):
    """A Latet server's applications API, for one API key's token.

    ``url`` is the server's address, ``token`` an API key's token (``latet_key_...``) with the permission of each
    operation used. ``timeout`` bounds each step of a call (connecting, sending, waiting for the answer), in seconds:
    90 by default, longer than a server waits on its database (60 s a statement, 75 s an answer), so that a call it
    could not answer in time comes back as a 503 rather than a :class:`ConnectionFailed`; a 429 or a 503 is tried again
    ``retries`` times after the ``Retry-After`` the server gave (30 s at most each time) before it is raised, and a call
    that does not go through is a :class:`ConnectionFailed`, not tried again. ``client`` is an ``httpx.Client`` of your
    own (a proxy, a certificate, a transport); the client closes only the one it made. A plain ``http://`` address of
    another computer is a :class:`ClearTextWarning`. It is safe to share between threads."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = 90.0,
        retries: int = 3,
        max_texts: int = 1000,
        max_bytes: int = 8 * 1024 * 1024,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(url, token, timeout=timeout, retries=retries, max_texts=max_texts, max_bytes=max_bytes)
        self._owned = client is None
        self._http = httpx.Client() if client is None else client

    def __enter__(self) -> Latet:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        """Closes the connections of the client it made; one it was given stays open."""
        if self._owned:
            self._http.close()

    @overload
    def scan(self, texts: str, *, kind: Kind = ...) -> ScanResult: ...  # pyright: ignore[reportOverlappingOverload]
    @overload
    def scan(self, texts: Sequence[str], *, kind: Kind = ...) -> list[ScanResult]: ...
    def scan(self, texts: str | Sequence[str], *, kind: Kind = "prose") -> ScanResult | list[ScanResult]:
        """Where the server's rules find values in each text; nothing is kept. ``kind`` is ``prose`` (text people
        wrote) or ``data`` (logs, records, a program's output)."""
        return self._run("scan", texts, kind, scan_results)

    @overload
    def mask(self, texts: str, *, kind: Kind = ...) -> MaskResult: ...  # pyright: ignore[reportOverlappingOverload]
    @overload
    def mask(self, texts: Sequence[str], *, kind: Kind = ...) -> list[MaskResult]: ...
    def mask(self, texts: str | Sequence[str], *, kind: Kind = "prose") -> MaskResult | list[MaskResult]:
        """Each text with the fakes of the API key's vault in place of its values: the same value gets the same fake
        in every call. A text the server stopped has no ``text`` and says ``blocked``."""
        return self._run("mask", texts, kind, mask_results)

    @overload
    def unmask(self, texts: str) -> UnmaskResult: ...  # pyright: ignore[reportOverlappingOverload]
    @overload
    def unmask(self, texts: Sequence[str]) -> list[UnmaskResult]: ...
    def unmask(self, texts: str | Sequence[str]) -> UnmaskResult | list[UnmaskResult]:
        """Each text with every fake of the API key's vault in it replaced by its value, whichever call made the
        fake. The server writes each unmask in its administration log, and refuses one it cannot write (503)."""
        return self._run("unmask", texts, None, unmask_results)

    def forget(self, values: str | Sequence[str]) -> ForgetResult:
        """Take values out of the API key's vault (a token with the ``forget`` permission, which no other brings): each
        value found exactly as written, gone with its fake, which restores nothing from then on, and given a new fake
        if it is met again. One result for all of them: how many the vault held, and how many it did not, each value
        counted once. The server writes each call in its administration log with its counts, never a value, and
        forgets nothing it cannot write there (503). A forget is safe to repeat: a value gone is counted as not held."""
        found: list[ForgetResult] = []
        for body, _ in self._forgetting(values):
            found += self._read(self._post("forget", body), 1, forget_results)
        return self._summed(found)

    def _run(self, operation: str, texts: str | Sequence[str], kind: Kind | None, read: Reader[T]) -> T | list[T]:
        single, planned = self._plan(texts, kind)
        results: list[T] = []
        for body, count in planned:
            results += self._read(self._post(operation, body), count, read)
        return results[0] if single else results

    def _post(self, operation: str, body: bytes) -> httpx.Response:
        attempt = 0
        while True:
            response, failure = None, ""
            try:
                response = self._http.post(
                    self._urls[operation], content=body, headers=self._headers(), timeout=self._timeout
                )
            except httpx.HTTPError as exc:
                failure = type(exc).__name__  # its name alone: it holds the request, and the request the token
            if response is None:  # outside the handler, so the error is neither the cause nor the context
                raise self._unreachable(failure)
            wait = self._after(response, attempt)
            if wait is None:
                return response
            _sleep(wait)
            attempt += 1


class AsyncLatet(_Wire):
    """:class:`Latet` with ``await``: the same arguments, results, batches, errors and retries, over an
    ``httpx.AsyncClient``. The batches of one call are sent one after the other."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = 90.0,
        retries: int = 3,
        max_texts: int = 1000,
        max_bytes: int = 8 * 1024 * 1024,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(url, token, timeout=timeout, retries=retries, max_texts=max_texts, max_bytes=max_bytes)
        self._owned = client is None
        self._http = httpx.AsyncClient() if client is None else client

    async def __aenter__(self) -> AsyncLatet:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, trace: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Closes the connections of the client it made; one it was given stays open."""
        if self._owned:
            await self._http.aclose()

    async def close(self) -> None:
        """The same as ``aclose``."""
        await self.aclose()

    @overload
    async def scan(self, texts: str, *, kind: Kind = ...) -> ScanResult: ...  # pyright: ignore[reportOverlappingOverload]
    @overload
    async def scan(self, texts: Sequence[str], *, kind: Kind = ...) -> list[ScanResult]: ...
    async def scan(self, texts: str | Sequence[str], *, kind: Kind = "prose") -> ScanResult | list[ScanResult]:
        """As :meth:`Latet.scan`."""
        return await self._run("scan", texts, kind, scan_results)

    @overload
    async def mask(self, texts: str, *, kind: Kind = ...) -> MaskResult: ...  # pyright: ignore[reportOverlappingOverload]
    @overload
    async def mask(self, texts: Sequence[str], *, kind: Kind = ...) -> list[MaskResult]: ...
    async def mask(self, texts: str | Sequence[str], *, kind: Kind = "prose") -> MaskResult | list[MaskResult]:
        """As :meth:`Latet.mask`."""
        return await self._run("mask", texts, kind, mask_results)

    @overload
    async def unmask(self, texts: str) -> UnmaskResult: ...  # pyright: ignore[reportOverlappingOverload]
    @overload
    async def unmask(self, texts: Sequence[str]) -> list[UnmaskResult]: ...
    async def unmask(self, texts: str | Sequence[str]) -> UnmaskResult | list[UnmaskResult]:
        """As :meth:`Latet.unmask`."""
        return await self._run("unmask", texts, None, unmask_results)

    async def forget(self, values: str | Sequence[str]) -> ForgetResult:
        """As :meth:`Latet.forget`."""
        found: list[ForgetResult] = []
        for body, _ in self._forgetting(values):
            found += self._read(await self._post("forget", body), 1, forget_results)
        return self._summed(found)

    async def _run(self, operation: str, texts: str | Sequence[str], kind: Kind | None, read: Reader[T]) -> T | list[T]:
        single, planned = self._plan(texts, kind)
        results: list[T] = []
        for body, count in planned:
            results += self._read(await self._post(operation, body), count, read)
        return results[0] if single else results

    async def _post(self, operation: str, body: bytes) -> httpx.Response:
        attempt = 0
        while True:
            response, failure = None, ""
            try:
                response = await self._http.post(
                    self._urls[operation], content=body, headers=self._headers(), timeout=self._timeout
                )
            except httpx.HTTPError as exc:
                failure = type(exc).__name__  # its name alone: it holds the request, and the request the token
            if response is None:  # outside the handler, so the error is neither the cause nor the context
                raise self._unreachable(failure)
            wait = self._after(response, attempt)
            if wait is None:
                return response
            await _asleep(wait)
            attempt += 1
