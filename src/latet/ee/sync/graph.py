"""Microsoft Entra ID through Microsoft Graph: people's names and addresses, or group names, read with the app's own
credentials (the client credentials flow: no person signs in). Every request goes to the address egress checked (a
public cloud's: never one inside the company's network), and the access token only ever to graph_url's own origin."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from latet.ee.settings import GraphSource, default_port, mapped_fields
from latet.ee.sync.records import Bounds, SecretOf, SourceError, bounds, cut_at_deadline, no_secret
from latet.ee.sync.sources import _IDENTITY, FLOOR, TIMEOUT, cut_of, holder, pinned_client

PAGE = 999  # the most Microsoft Graph gives in one page
MOST_PAGES = 500
RETRIES = 3  # a throttled or busy answer is asked again after the time it asks for, at most a minute
REFUSAL_BYTES = 64 * 1024  # a refusal is read this far for its code (B-R3), the rest left unread
_BUSY = (429, 503, 504)
_REFUSED = (400, 401, 403)
_CHUNK = 8192
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")  # Entra's `invalid_client`, Graph's `Authorization_RequestDenied`
_TOKEN = re.compile(r"[\x21-\x7e]+")  # what goes in an Authorization header: visible ASCII, no space


def read_graph(
    source: GraphSource,
    secret: SecretOf,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """The people (or the groups) ``source`` names, page by page, with the fields its mapping reads. The token's
    request and every page are read within the run's deadline and bytes (B-I1, B-M5), under its watchdog
    (``records.cut_at_deadline``), with a pinned client of this read's own; a next page on another origin than
    graph_url's is never asked for (B-M6). Every reason is in fixed words: no host, tenant, secret or term."""
    password = secret()
    if password is None:
        raise no_secret(source.name)
    http = client or pinned_client(holder(source), inside=False)
    try:
        with cut_at_deadline(cut_of(http)):
            return _read(http, sleep, source, password)
    finally:
        if client is None:
            http.close()


def _read(
    http: httpx.Client, sleep: Callable[[float], None], source: GraphSource, password: str
) -> list[dict[str, Any]]:
    login, graph = _base(source.login_url), _base(source.graph_url)  # both before anything is sent
    form = {
        "client_id": source.client_id,
        "client_secret": password,
        "grant_type": "client_credentials",
        "scope": f"{graph}/.default",  # the token is good for graph_url alone (B-M6)
    }
    token_url = f"{login}/{quote(source.tenant, safe='')}/oauth2/v2.0/token"
    token = _send(http, sleep, "Microsoft Entra ID", "POST", token_url, data=form).get("access_token")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise SourceError("refused", "Microsoft Entra ID gave no token")
    headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
    home = _origin(graph)
    url, records = _first_page(source), []
    for _ in range(MOST_PAGES):
        bounds().check()  # each page within the run's deadline (B-I1)
        page = _send(http, sleep, "Microsoft Graph", "GET", url, headers=headers)  # its JSON, read bounded (B-M5)
        found = page.get("value")  # every page of a collection has it, an empty list when there is nothing
        if not isinstance(found, list):
            raise SourceError("unreadable", "Microsoft Graph answered a page with no list of records")
        records += [r for r in found if isinstance(r, dict)]
        following = page.get("@odata.nextLink")
        if not isinstance(following, str) or not following:
            return records
        if home is None or _origin(following) != home:  # the token goes to Microsoft Graph alone
            raise SourceError("unreadable", "Microsoft Graph's next page is at another address: not followed")
        url = following
    raise SourceError("too-many", f"more than {MOST_PAGES} pages: narrow it with `filter` or `group`")


def _parsed(url: str) -> httpx.URL | None:
    """An address as httpx reads it (the client sends to what httpx reads, never another parser's idea of it); None for
    one that holds a user's name or that httpx cannot read."""
    try:
        parts = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    return None if parts.userinfo else parts


def _origin(url: str) -> tuple[str, str, int] | None:
    """An address's scheme, host and port, as one compares them: the host in lowercase with no trailing dot, the port
    the scheme's own when none is written."""
    parts = _parsed(url)
    if parts is None:
        return None
    return parts.scheme, parts.host.rstrip(".").lower(), parts.port or default_port(parts.scheme)


def _base(url: str) -> str:
    """``login_url`` or ``graph_url`` rebuilt from its scheme, host and port alone (Task 8's review): no path, query or
    fragment its text may hold moves the token's request, its scope or the pages, whatever the settings let through."""
    parts = _parsed(url)
    if parts is None or not parts.host:
        raise SourceError("unreadable", "Microsoft's address is not an origin Latet reads: set the source again")
    return f"{parts.scheme}://{parts.netloc.decode('ascii')}"


def _first_page(source: GraphSource) -> str:
    """The first page's address on graph_url's origin: the fields the mapping reads in `$select` (those the source's
    secret is bound to), each name percent-encoded so that one holding `&`, `,` or `%` stays one field of one
    parameter; a filter as an advanced query, encoded alike."""
    if source.objects == "groups":
        path = "/groups"
    elif source.group:
        path = f"/groups/{quote(source.group, safe='')}/transitiveMembers/microsoft.graph.user"
    else:
        path = "/users"
    query = {"$select": ",".join(quote(name, safe="") for name in mapped_fields(source)), "$top": str(PAGE)}
    if source.filter:
        query |= {"$filter": quote(source.filter, safe=""), "$count": "true"}  # an advanced query, as some need
    return f"{_base(source.graph_url)}/v1.0{path}?" + "&".join(f"{name}={value}" for name, value in query.items())


def _send(
    http: httpx.Client, sleep: Callable[[float], None], who: str, method: str, url: str, **kwargs: Any
) -> dict[str, Any]:
    """One request's answer, a JSON object, asked again after the time asked while it is throttled or busy. Each
    request is sent within the run's deadline, each phase waiting no longer than the run has left, and each answer read
    within its deadline and bytes (B-I1, B-M5), uncompressed, following no redirect. A refusal says its status and its
    code (`invalid_client`, `Authorization_RequestDenied`) read from its first ``REFUSAL_BYTES`` alone, never its
    description, which names the tenant and the app. A ``SourceError`` of the pinned client (egress refused the
    address) passes as it is."""
    limits = bounds()
    headers = {**kwargs.pop("headers", {}), "accept-encoding": "identity"}
    for attempt in range(RETRIES + 1):
        limits.check()
        timeout = min(TIMEOUT, max(limits.left(), FLOOR))
        try:
            with http.stream(
                method, url, headers=headers, follow_redirects=False, timeout=timeout, **kwargs
            ) as response:
                if response.status_code not in _BUSY or attempt == RETRIES:
                    return _answer(who, response, limits)
                wait = _retry_after(response)
        except httpx.HTTPError as exc:
            raise SourceError("unreachable", f"{who} did not answer ({type(exc).__name__})") from None
        sleep(min(wait, max(limits.left(), 0.0)))
        limits.check()
    raise SourceError("unreachable", f"{who} stayed busy")  # not reached: the last attempt answers or fails


def _answer(who: str, response: httpx.Response, limits: Bounds) -> dict[str, Any]:
    """A 200's JSON object, read whole within the run's bounds; any other status a failure, saying its code."""
    plain = response.headers.get("content-encoding", "").strip().lower() in _IDENTITY
    if response.status_code != 200:
        body = _body(response, limits, most=REFUSAL_BYTES) if plain else b""
        code = "refused" if response.status_code in _REFUSED else "unreachable"
        raise SourceError(code, f"{who} answered {response.status_code}{_error_code(body)}")
    if not plain:
        raise SourceError("unreadable", f"{who} answered compressed, though Latet asked for no compression")
    try:
        found = json.loads(_body(response, limits))
    except (ValueError, RecursionError):  # not JSON, not text, or nested past what the parser follows
        found = None
    if not isinstance(found, dict):
        raise SourceError("unreadable", f"{who} answered with no JSON object")
    return found


def _body(response: httpx.Response, limits: Bounds, most: int | None = None) -> bytes:
    """An answer's bytes, the deadline checked and each chunk taken from the run's budget; past ``most``, left
    unread."""
    data = bytearray()
    for chunk in response.iter_bytes(chunk_size=_CHUNK):
        limits.check()
        kept = chunk if most is None else chunk[: most - len(data)]
        limits.take(len(kept))
        data += kept
        if most is not None and len(data) >= most:
            break
    return bytes(data)


def _retry_after(response: httpx.Response) -> float:
    """The seconds a throttled or busy answer asks to wait, within a minute; 5 when it says none in seconds."""
    try:
        seconds = float(response.headers.get("retry-after", "5"))
    except ValueError:
        return 5.0
    return 5.0 if math.isnan(seconds) else min(max(seconds, 0.0), 60.0)


def _error_code(body: bytes) -> str:
    """Entra's or Graph's code for a refusal, parsed from the bytes read of it: ` (invalid_client)`; nothing when they
    are no JSON (cut at ``REFUSAL_BYTES``, or another text), or the code is no word of a few ASCII letters."""
    try:
        found = json.loads(body)
    except (ValueError, RecursionError):
        return ""
    error = found.get("error") if isinstance(found, dict) else None
    code = error.get("code") if isinstance(error, dict) else error
    return f" ({code})" if isinstance(code, str) and _CODE.fullmatch(code) else ""
