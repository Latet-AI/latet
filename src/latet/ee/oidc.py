"""Single sign-on to a server's dashboard with OpenID Connect: the authorization code flow with PKCE (RFC 7636).

The browser goes to the company's identity provider and comes back with a code; the server swaps the code for an ID
token at the provider's token endpoint and checks it: its signature against the keys the provider publishes
(PyJWT, with the key of the token's id and the algorithms that key may verify, never the ones the token names), then
who issued it, for whom, until when (by the server's clock), and the one-time nonce of this sign-in. Nothing else of
the provider is used: no access token, no user info endpoint. SAML 2.0 is not spoken.

What the provider answers is read up to a size (``DISCOVERY_SIZE``, ``KEYS_SIZE``, ``TOKEN_SIZE``), and the addresses
its discovery document names are https, never this computer unless the issuer itself is on it. A refusal says why in
fixed words (``SignInError.detail``, for the server's log); the provider's own words, and httpx's with the address,
only in ``SignInError.said``, for an administrator testing the settings.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import secrets
import ssl
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import SplitResult, quote, urlencode, urlsplit

import httpx
import jwt
import truststore
from cryptography.exceptions import InvalidTag
from jwt.exceptions import MissingRequiredClaimError, PyJWTError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from latet.api.rest import COOKIE, SESSION_TTL
from latet.ee.license import Standing, State
from latet.ee.people import NOT_YOURS, USER_NAME_LENGTH, NotYours, Taken, group_key
from latet.ee.work import working
from latet.events import make_event
from latet.gateway import request_transport
from latet.logline import Paced, described
from latet.server import egress
from latet.server.identities import NAME_LENGTH, EmailTaken, clean
from latet.server.live import Via
from latet.server.machines import StoresUnavailable, address, client_of, in_work
from latet.server.machines import _counting as counted_in

if TYPE_CHECKING:
    from latet.ee.edition import EnterpriseEdition
    from latet.ee.people import People
    from latet.ee.settings import SsoSettings
    from latet.server.app import Server
    from latet.server.identities import Identity

log = logging.getLogger(__name__)

# What an ID token may be signed with: public-key algorithms only, each key the ones of its type (and curve). HS256
# would make the client secret a signing key, and "none" is no signature.
_RSA = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512")
_CURVES = {"P-256": "ES256", "P-384": "ES384", "P-521": "ES512"}
ALGORITHMS = (*_RSA, *_CURVES.values(), "EdDSA")
_PRIVATE = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})  # a JWK's private members: never read
CLAIMS = ("iss", "aud", "exp", "iat", "sub")  # what every ID token carries (OpenID Connect Core 1.0, 2)
LEEWAY = 120  # seconds the server's clock and the provider's may differ
DISCOVERY_SIZE = 256 * 1024  # bytes of the provider's discovery document read at most
KEYS_SIZE = 256 * 1024  # bytes of its key set
TOKEN_SIZE = 64 * 1024  # bytes of its token endpoint's answer
# The token endpoint's errors (RFC 6749, 5.2), the only words of its answer a log line names.
TOKEN_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)
_SAID = 200  # characters of a value of the provider's quoted for an administrator
# A host of numbers alone (decimal, octal or hex, dotted or not), which inet_aton reads as an IPv4 address.
_NUMBERS = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+))*")
_IDENTITY = ("", "identity")  # the content encodings read: none
REQUEST_SECONDS = 20.0  # a request to the provider, its connection and its whole answer: past it, refused
_PORTS = {"https": 443, "http": 80}


class SignInError(Exception):
    """Why a sign-in did not happen. ``code`` is what the sign-in page shows (expired, failed, not-allowed,
    not-provisioned, disabled, unavailable); ``detail`` goes to the server's log, in fixed words and an error's type;
    ``identity``: the person, when known; ``said``: ``detail`` with the provider's own words and the addresses, for an
    administrator's test of the settings only (never a log line)."""

    def __init__(self, code: str, detail: str, identity: str | None = None, *, said: str | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.identity = identity
        self.said = said if said is not None else detail


@dataclass(frozen=True)
class Provider:
    """What Latet needs of the provider's discovery document."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    auth_methods: tuple[str, ...]


class InvalidKeyIdError(Exception):
    """The ID token names a key id the provider's keys lack, or names none while they are several: the caller may fetch
    the keys again (the provider may have rotated them)."""


@dataclass(frozen=True)
class ProviderKey:
    """One of the provider's signing keys: its id, its public key, and the algorithms it may verify."""

    kid: str | None
    key: Any
    algorithms: tuple[str, ...]


class KeySet:
    """The provider's signing keys, read from its key set (``key_set``)."""

    def __init__(self, keys: Iterable[ProviderKey]) -> None:
        self._keys = tuple(keys)

    def __iter__(self) -> Iterator[ProviderKey]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def find(self, kid: str | None) -> ProviderKey:
        """The key of id ``kid``; with no id, the only key there is."""
        if kid is None and len(self._keys) == 1:
            return self._keys[0]
        for key in self._keys:
            if kid is not None and key.kid == kid:
                return key
        raise InvalidKeyIdError("no key of the provider's has the ID token's key id")


def _algorithms_of(kty: object, crv: object) -> tuple[str, ...]:
    if kty == "RSA":
        return _RSA
    if kty == "EC" and isinstance(crv, str) and crv in _CURVES:
        return (_CURVES[crv],)
    if kty == "OKP" and crv == "Ed25519":
        return ("EdDSA",)
    return ()


def key_set(found: Mapping[str, Any]) -> KeySet:
    """The signing keys of a key set (RFC 7517, 5), parsed with PyJWT's ``PyJWK``: a symmetric key or two keys of one
    id make the whole set unreadable (ValueError, in fixed words); an encryption key, or a key of a type, curve or
    algorithm Latet does not verify with, is left aside; private members are never read."""
    listed = found.get("keys")
    if not isinstance(listed, list):
        raise ValueError("the key set holds no list of keys")
    keys: list[ProviderKey] = []
    for data in cast(list[Any], listed):
        if not isinstance(data, dict):
            raise ValueError("a key of the set is not a JSON object")
        data = cast(dict[str, Any], data)
        if data.get("kty") == "oct":
            raise ValueError("the key set publishes a symmetric key")
        if data.get("use", "sig") != "sig":
            continue  # an encryption key
        kid = data.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise ValueError("a key's id is not a string")
        allowed = _algorithms_of(data.get("kty"), data.get("crv"))
        alg = data.get("alg")
        if alg is not None:
            allowed = (alg,) if alg in allowed else ()
        if not allowed:
            continue
        public = {name: value for name, value in data.items() if name not in _PRIVATE}
        try:
            parsed = jwt.PyJWK(public, allowed[0])
        except (PyJWTError, ValueError, TypeError, KeyError, AttributeError, NotImplementedError) as exc:
            raise ValueError("a key of the set cannot be read") from exc
        keys.append(ProviderKey(kid, parsed.key, allowed))
    kids = [key.kid for key in keys]
    if len(set(kids)) != len(kids):
        raise ValueError("two keys of the set have the same id")
    if not keys:
        raise ValueError("the key set holds no key Latet verifies with")
    return KeySet(keys)


def pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge (RFC 7636, 4.1 and 4.2)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def secure_address(url: str) -> bool:
    """An issuer Latet may be given: https, or http on this computer (Keycloak in Docker, for a test). Its twin in the
    sign-in settings is ``SsoSettings._issuer``."""
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"))


def _loopback(host: str) -> bool:
    """A name or an address of this computer, as written (resolving a name is the egress checks' part). Numbers that
    are not an address written the usual way (``127.1``, ``2130706433``, ``0x7f000001``, ``0``, leading zeros) count
    as one: the system's resolver reads them as addresses, and nothing checks which."""
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return _NUMBERS.fullmatch(name) is not None
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return address.is_loopback or address.is_unspecified or (mapped is not None and mapped.is_loopback)


def _origin(parts: SplitResult) -> tuple[str, str | None, int | None]:
    return parts.scheme, parts.hostname, parts.port or _PORTS.get(parts.scheme)


def _reachable(url: str, issuer: str) -> bool:
    """Whether the discovery document of ``issuer`` may send Latet to ``url``: an https address off this computer, or
    one at the issuer's own origin (http too, when the issuer is http on this computer). A loopback address is
    reached only when the issuer itself names it (M5)."""
    parts = urlsplit(url)
    try:
        same = _origin(parts) == _origin(urlsplit(issuer))
    except ValueError:  # a port that is not a number
        return False
    if parts.scheme == "https" and parts.hostname:
        return same or not _loopback(parts.hostname)
    return parts.scheme == "http" and same and secure_address(issuer)


def _described(exc: BaseException) -> str:
    """An error as the server's log names it: its type, and for httpx's the host it was reaching, never its words."""
    said = described(exc)
    if isinstance(exc, httpx.RequestError):
        try:
            host = exc.request.url.host
        except RuntimeError:  # an error raised with no request
            host = ""
        if host:
            said += f" at {host}"
    return said


def _quoted(value: object) -> str:
    """A value of the provider's as an administrator reads it: quoted, and cut."""
    said = repr(value)
    return said if len(said) <= _SAID else said[:_SAID] + "..."


async def _read(
    http: httpx.AsyncClient,
    url: str,
    what: str,
    size: int,
    *,
    form: dict[str, str] | None = None,
    auth: httpx.BasicAuth | None = None,
) -> tuple[int, bytes]:
    """The status and the body of a GET of ``url`` (a POST of ``form``), the body read up to ``size`` bytes. Nothing
    compressed is asked for or read: a few compressed bytes can stand for gigabytes, which a cap counted on what is
    read would only see once they were in memory."""
    too_large = SignInError("unavailable", f"{what} is larger than {size} bytes")
    try:
        async with (
            asyncio.timeout(REQUEST_SECONDS),
            http.stream(
                "GET" if form is None else "POST",
                url,
                data=form,
                auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
                headers={"accept": "application/json", "accept-encoding": "identity"},
                follow_redirects=False,  # a redirect would lead past the addresses discover checked
            ) as response,
        ):
            if response.headers.get("content-encoding", "").strip().lower() not in _IDENTITY:
                raise SignInError("unavailable", f"{what} answered compressed, though Latet asked for no compression")
            declared = response.headers.get("content-length", "")
            if declared.isascii() and declared.isdigit() and int(declared) > size:
                raise too_large
            body = bytearray()
            async for chunk in response.aiter_bytes():  # no decoding: the encoding is none
                body += chunk
                if len(body) > size:
                    raise too_large
            return response.status_code, bytes(body)
    except TimeoutError:  # the whole request, its connection and its body together
        raise SignInError("unavailable", f"{what} did not answer within {REQUEST_SECONDS:g} seconds") from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise SignInError(
            "unavailable", f"{what} could not be reached ({_described(exc)})", said=f"{what} at {url}: {exc}"
        ) from exc


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):  # not JSON, or nested past what the parser follows
        return None


async def _get_json(http: httpx.AsyncClient, url: str, what: str, size: int) -> dict[str, Any]:
    status, body = await _read(http, url, what, size)
    found = _json(body) if status == 200 else None
    if not isinstance(found, dict):
        raise SignInError(
            "unavailable", f"{what} answered {status}, not a JSON object", said=f"{what} at {url} answered {status}"
        )
    return cast(dict[str, Any], found)


async def discover(http: httpx.AsyncClient, issuer: str) -> Provider:
    """The provider's discovery document (OpenID Connect Discovery 1.0, 4), checked: it names the issuer Latet was
    given (a trailing slash aside) and the addresses it names are ones Latet may reach (``_reachable``)."""
    if not secure_address(issuer):
        raise SignInError(
            "unavailable", "the issuer is not an https:// address", said=f"the issuer {_quoted(issuer)} is not https://"
        )
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    found = await _get_json(http, url, "the provider's discovery document", DISCOVERY_SIZE)
    named = found.get("issuer")
    if not isinstance(named, str) or named.rstrip("/") != issuer.rstrip("/"):
        raise SignInError(
            "unavailable",
            "the provider's discovery document names another issuer than the one configured",
            said=f"the provider names its issuer {_quoted(named)}, not {issuer!r}: write it as the issuer",
        )
    endpoints: dict[str, str] = {}
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = found.get(key)
        if not isinstance(value, str) or not _reachable(value, issuer):
            raise SignInError(
                "unavailable",
                f"the provider's {key} is not an https:// address, or names this computer while the issuer does not",
                said=f"the provider's {key} is not an address Latet reaches: {_quoted(value)}",
            )
        endpoints[key] = value
    methods = found.get("token_endpoint_auth_methods_supported")
    auth = tuple(m for m in methods if isinstance(m, str)) if isinstance(methods, list) else ("client_secret_basic",)
    return Provider(
        named, endpoints["authorization_endpoint"], endpoints["token_endpoint"], endpoints["jwks_uri"], auth
    )


async def fetch_keys(http: httpx.AsyncClient, provider: Provider) -> KeySet:
    found = await _get_json(http, provider.jwks_uri, "the provider's keys", KEYS_SIZE)
    try:
        return key_set(found)
    except ValueError as exc:  # key_set's own fixed words; PyJWT's, which quote the key, only for an administrator
        cause = f": {exc.__cause__}" if exc.__cause__ is not None else ""
        raise SignInError(
            "unavailable",
            f"the provider's keys cannot be read: {exc}",
            said=f"the provider's keys at {provider.jwks_uri} cannot be read: {exc}{cause}",
        ) from exc


async def exchange(
    http: httpx.AsyncClient,
    provider: Provider,
    client_id: str,
    secret: str | None,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> str:
    """The ID token the provider gives for ``code``, with the PKCE verifier. The client authenticates with
    client_secret_post when the provider takes it, else client_secret_basic, encoded as RFC 6749 (2.3.1) asks."""
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier}
    auth: httpx.BasicAuth | None = None
    if secret is None:
        form["client_id"] = client_id
    elif "client_secret_post" in provider.auth_methods:
        form |= {"client_id": client_id, "client_secret": secret}
    else:
        auth = httpx.BasicAuth(quote(client_id, safe=""), quote(secret, safe=""))
    status, body = await _read(
        http, provider.token_endpoint, "the provider's token endpoint", TOKEN_SIZE, form=form, auth=auth
    )
    answer = _json(body)
    token = answer.get("id_token") if isinstance(answer, dict) else None
    if status != 200 or not isinstance(token, str):
        error = answer.get("error") if isinstance(answer, dict) else None
        named = error if isinstance(error, str) and error in TOKEN_ERRORS else "other" if error else "no ID token"
        description = answer.get("error_description") if isinstance(answer, dict) else None
        raise SignInError(
            "failed",
            f"the token endpoint answered {status} ({named})",
            said=f"the token endpoint answered {status} ({_quoted(error)}: {_quoted(description)})",
        )
    return token


def _bytes(text: str) -> bytes:
    return text.encode("utf-8", "surrogatepass")


def _time(claims: Mapping[str, Any], name: str) -> None:
    value = claims[name]
    try:
        finite = not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)
    except OverflowError:  # an integer too large for a float
        finite = False
    if not finite:
        raise SignInError("failed", f"the ID token's {name} is not a time")


def verify(id_token: str, keys: KeySet, *, issuer: str, client_id: str, nonce: str, now: float) -> dict[str, Any]:
    """The claims of ``id_token`` once checked (OpenID Connect Core 1.0, 3.1.3.7): signed by the key of its id among
    ``keys``, with an algorithm that key may verify (the list Latet keeps for it, never the token's own word); issued
    by ``issuer`` exactly, for ``client_id``, and authorized for it when it names an authorized party; not expired and
    not issued in the future by ``now`` (the server's clock), give or take ``LEEWAY``, its times numbers; carrying this
    sign-in's ``nonce``. Raises InvalidKeyIdError when no key has the token's id (the caller fetches the keys again),
    SignInError for anything else, its detail never quoting the token."""
    try:
        kid = jwt.get_unverified_header(id_token).get("kid")  # which key to try only: PyJWT checks it is a string
    except (PyJWTError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SignInError("failed", f"the ID token cannot be read ({described(exc)})") from exc
    key = keys.find(kid)
    try:
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=list(key.algorithms),
            audience=client_id,
            issuer=issuer,
            # The times are checked below by the server's clock (PyJWT reads the computer's own).
            options={
                "require": list(CLAIMS),
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "enforce_minimum_key_length": True,
            },
        )
    except (PyJWTError, ValueError, TypeError, KeyError, AttributeError) as exc:
        why = f"{described(exc)} {exc.claim}" if isinstance(exc, MissingRequiredClaimError) else described(exc)
        raise SignInError("failed", f"the ID token does not hold ({why})") from exc
    # Times that are numbers (NaN or infinity never compare as expired), then the clocks' leeway.
    for name in ("exp", "iat", *(("nbf",) if "nbf" in claims else ())):
        _time(claims, name)
    if claims["exp"] <= now - LEEWAY:
        raise SignInError("failed", "the ID token has expired")
    if claims["iat"] > now + LEEWAY or claims.get("nbf", now) > now + LEEWAY:
        raise SignInError("failed", "the ID token is not valid yet (issued in the future)")
    found = claims.get("nonce")
    # surrogatepass: a lone surrogate (JSON lets one through) compares as bytes rather than raising.
    if not isinstance(found, str) or not hmac.compare_digest(_bytes(found), _bytes(nonce)):
        raise SignInError("failed", "the ID token does not carry this sign-in's nonce")
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    party = claims.get("azp")
    if (party is not None and party != client_id) or (len(audiences) > 1 and party is None):
        raise SignInError("failed", "the ID token was not issued to Latet's client (azp)")
    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise SignInError("failed", "the ID token names no subject")
    return claims


def claim(claims: Mapping[str, Any], name: str) -> Any:
    """A claim by its name; when no claim has it, a dotted path into the claims (``realm_access.roles``). A name with
    dots of its own (``https://acme.com/groups``) is found first."""
    if name in claims:
        return claims[name]
    value: Any = claims
    for part in name.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def groups_in(claims: Mapping[str, Any], name: str) -> list[str]:
    """The person's groups in the ID token: a list of strings, or one string."""
    value = claim(claims, name)
    if isinstance(value, str):
        return [value]
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def member_of(groups: Iterable[str], group: str) -> bool:
    return group_key(group) in {group_key(g) for g in groups}


def within_domains(email: str, domains: Iterable[str]) -> bool:
    """Whether ``email`` is at one of ``domains`` (the settings' ``allowed_domains``: ASCII, in lower case), or
    ``domains`` names none. Only ASCII is folded: a character whose lower case is an ASCII letter (the Kelvin sign's
    is ``k``) never stands for one."""
    domains = tuple(domains)
    _local, at, domain = email.rpartition("@")
    return not domains or (bool(at) and domain.isascii() and domain.lower() in domains)


# --- reaching the provider ----------------------------------------------------------------------------------------

_LOOPBACK_ISSUERS = ("localhost", "127.0.0.1", "::1")  # an issuer on this computer (Keycloak by hand), as written


class PinnedTransport(httpx.AsyncBaseTransport):
    """How the server reaches the identity provider (R10): each request is sent to the address ``egress.pin`` resolves
    and checks for it at that moment (``inside``: the company's own network allowed, never a link-local address such
    as the cloud's metadata address, an address with a zone or an IPv6 form that carries an IPv4 one), its ``Host`` and
    TLS's name still the provider's, so that a name pointed elsewhere after it was saved does not get through. This
    computer only when the configured ``issuer`` is on it, and then only at the issuer's own origin (M5): an address
    naming this computer otherwise, numbers the resolver reads as one among them, is refused before any lookup. The
    system's certificates (truststore).

    A name with several addresses (``localhost`` on Windows: ``::1``, then 127.0.0.1, where Keycloak in Docker listens
    alone) is reached at the next one the same lookup checked when the TCP connection to the one before failed, before
    any byte of the request left, as a server reaches an identity's own destination (``request_transport.reaching``:
    eight addresses at most, within the connect timeout; TLS checked against the provider's name at each; a connection
    kept serves only its address and TLS name; httpx's own limits, no retry)."""

    def __init__(self, issuer: str) -> None:
        self._issuer = urlsplit(issuer)
        self._sent = request_transport.reaching(truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))

    def _here_allowed(self, parts: SplitResult) -> bool:
        """Whether a request to this computer may go: the issuer is on it, and the request is at its origin."""
        return self._issuer.hostname in _LOOPBACK_ISSUERS and _origin(parts) == _origin(self._issuer)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        try:
            parts = urlsplit(url)
            if _loopback(parts.hostname or "") and not self._here_allowed(parts):
                raise egress.EgressRefused("this computer, which only an issuer on it leads to")
            pinned, host, extensions = await egress.pin(url, holder="sso", inside=True)
        except (egress.EgressRefused, ValueError) as exc:
            # The words name no value of the request: the log says the error's type and the host (``_described``).
            raise httpx.ConnectError("the provider's address is refused", request=request) from exc
        request.url = httpx.URL(pinned)
        request.headers["host"] = host
        request.extensions = {**request.extensions, **extensions}
        return await self._sent.handle_async_request(request)

    async def aclose(self) -> None:
        await self._sent.aclose()


# --- the sign-in --------------------------------------------------------------------------------------------------

FLOW_COOKIE = "latet_sso"
FLOW_TTL = 600.0  # a sign-in comes back within ten minutes
PROVIDER_TTL = 3600.0  # the discovery document and the keys are fetched again after an hour
KEYS_AGAIN = 30.0  # a key id the keys lack fetches them again: once a sign-in, at most once in 30 s
UNREACHED = 30.0  # a provider that did not answer is not asked again for 30 s by this instance
LOGINS = (600, 60.0)  # sign-ins begun from one address: at most 600 a minute
CALLBACKS = (600, 60.0)  # callbacks from one address: as many
# Sign-ins begun and not yet finished, each a sealed flow waiting in Valkey for FLOW_TTL, apart from LOGINS: Valkey
# keeps each in a sorted set of its address's (the limits' bucket: an IPv4 address, an IPv6 /64), for IPv6 in its /48's
# too (E3's release gate 5: machines.AGGREGATE_PREFIX, as E0c's limits count misses), and in the server's, by when it
# ends, until its callback takes the flow (``_take``: once, whether it then signs in or is refused) or it ends
# unfinished. Past FLOWS_FROM_ONE waiting from its address, or FLOWS_FROM_WIDE from its /48, a sign-in begun is refused
# (429), past FLOWS_WAITING on the server (503), until one of them finishes or ends; its address's first, then its
# /48's, so that what an address or a /48 is refused takes nothing of the server's: filling the server's takes
# FLOWS_WAITING / FLOWS_FROM_ONE (200) IPv4 addresses, or FLOWS_WAITING / FLOWS_FROM_WIDE (20) /48s, each leaving its
# sign-ins unfinished, and no longer the 256 /64s of one home's /56. An office behind one NAT or proxy shares its
# address's: it is refused only by as many sign-ins left unfinished at once. A company's whole staff signing in stays
# well under the server's; a flood stops there, at about 16 MB of Valkey at worst (~0.5 KB a flow, and its entries).
# Sessions already open, and `latet dashboard`, still work.
FLOWS_FROM_ONE = 100
FLOWS_FROM_WIDE = 1_000  # sign-ins begun and not finished from one IPv6 /48: ten /64s' worth, one site
FLOWS_WAITING = 20_000
FLOWS_FROM = "sso-waiting-from"  # an address's set in Valkey, named with the address's keyed hash (``Bus.rate_key``)
FLOWS = "sso-waiting"  # the server's set in Valkey
CALLBACK = "/_latet/sso/callback"
LINK = "/_latet/sso/link"
CLIENT_SECRET_PLACE = "sso-issuer"  # noqa: S105 - a name, not a secret; what the client secret is sealed for, beside its issuer's origin (M4)
# The errors a provider sends back that the log names (OpenID Connect Core 1.0, 3.1.2.6; RFC 6749, 4.1.2.1): any
# other is "other", never its own words.
PROVIDER_ERRORS = frozenset({"access_denied", "login_required", "consent_required", "interaction_required"})
_NO_STORE = {"cache-control": "no-store", "referrer-policy": "no-referrer"}
# The answer to a sign-in: a page of the server's own that moves to the dashboard, so that the session's cookie
# (SameSite=Strict), set on a navigation begun at the provider, goes with the requests that follow.
_SIGNED_IN = (
    '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/_latet/ui/">'
    '<title>Latet</title><p><a href="/_latet/ui/">Latet</a></p>'
)
_UNWAITED = Paced()  # callbacks no sign-in waits for: said once a minute
_LIMITED = Paced()  # sign-ins past their limit from one address: said once a minute
_CROWDED = Paced()  # sign-ins past FLOWS_WAITING on the server: said once a minute
_FLOODED = Paced()  # sign-ins past FLOWS_FROM_ONE from one address: said once a minute
_FLOODED_WIDE = Paced()  # sign-ins past FLOWS_FROM_WIDE from one IPv6 /48: said once a minute
_UNCOUNTED = Paced()  # sign-ins finished that Valkey did not take out of the sets: said once a minute
# A sign-in begun, kept waiting in one step when its address's sets (KEYS[1] to KEYS[n], n = ARGV[4]: the /64's or the
# IPv4 address's, then for IPv6 the /48's, each of most ARGV[5 + i]) and the server's (KEYS[n + 1], of most ARGV[5])
# have room, weighed in that order: each first loses the sign-ins that ended (scored by their end, in Valkey's own
# time), then the flow (KEYS[n + 2], ARGV[3]) is stored and named in each. Answers {0, 0, 0}, or the refusal's status
# (429: an address's set, 503: the server's), the milliseconds until the oldest sign-in waiting there ends, and which
# set it is (1 to n + 1); nothing is written then.
_WAIT = """
local t = redis.call('time')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local member, life, sets = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[4])
local function full(key, most)
  redis.call('zremrangebyscore', key, '-inf', now)
  if redis.call('zcard', key) < most then return nil end
  local oldest = redis.call('zrange', key, 0, 0, 'withscores')
  return math.max(1, tonumber(oldest[2]) - now)
end
for i = 1, sets do
  local left = full(KEYS[i], tonumber(ARGV[5 + i]))
  if left then return {429, left, i} end
end
local left = full(KEYS[sets + 1], tonumber(ARGV[5]))
if left then return {503, left, sets + 1} end
for i = 1, sets + 1 do
  redis.call('zadd', KEYS[i], now + life, member)
  redis.call('pexpire', KEYS[i], life)
end
redis.call('set', KEYS[sets + 2], ARGV[3], 'px', life)
return {0, 0, 0}
"""
# The answers to a sign-in begun while FLOWS_WAITING wait already on the server (503) or FLOWS_FROM_ONE from its
# address (429), with retry-after: fixed words, and the browser sent to the sign-in page, which says that single sign-on
# is not available now.
_BUSY = (
    '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/_latet/ui/?sign-in=unavailable">'
    "<title>Latet</title><p>Too many sign-ins are waiting on this server: try again in a few minutes. "
    '<a href="/_latet/ui/?sign-in=unavailable">Latet</a></p>'
)
_BUSY_FROM_YOURS = (
    '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/_latet/ui/?sign-in=unavailable">'
    "<title>Latet</title><p>Too many sign-ins begun from your address are waiting on this server: try again in a few "
    'minutes. <a href="/_latet/ui/?sign-in=unavailable">Latet</a></p>'
)


def secret_bound(issuer: str) -> str:
    """What the client secret is sealed for (``Secrets.save(…, bound=)``): its whole issuer (a trailing slash aside),
    since Keycloak's realms and Entra's tenants share one origin, so that an issuer changed since it was saved never
    gets it (M4): it is saved again for the new one."""
    return f"{CLIENT_SECRET_PLACE}:{issuer.strip().rstrip('/')}"


def refused_by(standing: Standing) -> str | None:
    """Why single sign-on does not serve under ``standing``, as the sign-in page says it (R2 to R4): ``unavailable``
    for a license without sso or one that cannot be used, ``trial-ended`` once a trial ended; None while it serves
    (a paid license expired, in its grace or paused serves)."""
    lic = standing.license
    if standing.state is State.INVALID or lic is None or "sso" not in lic.features:
        return "unavailable"
    if standing.state is State.TRIAL_ENDED:
        return "trial-ended"
    return None


@dataclass(frozen=True)
class _Known:
    """The provider as this instance last fetched it."""

    issuer: str
    provider: Provider
    keys: KeySet
    fetched: float
    keys_fetched: float


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_sent(value: Any) -> str | None:
    """The user name claim as the ID token sends it, never stripped: None, naming nobody, unless it is text that Latet
    keeps as it is (E0b's ``clean`` leaves it whole: no white space around it, no control or bidirectional character,
    not past its length), as ``People.link_own`` takes it. Stripped, a claim with a no-break space after it would be
    another account's user name (the final security review's L1)."""
    return value if isinstance(value, str) and value and clean(value, USER_NAME_LENGTH) == value else None


def _back(code: str) -> RedirectResponse:
    """To the dashboard's sign-in page, which says why (``?sign-in=<code>``)."""
    response = RedirectResponse(f"/_latet/ui/?sign-in={code}", status_code=303, headers=_NO_STORE)
    response.delete_cookie(FLOW_COOKIE, path="/_latet/sso")
    return response


def _busy(status: int, seconds: float) -> HTMLResponse:
    """To a sign-in begun while FLOWS_FROM_ONE wait already from its address (429) or FLOWS_WAITING on the server (503),
    with how long until the oldest of them ends (one finished sooner makes room sooner)."""
    headers = {
        **_NO_STORE,
        "content-security-policy": "default-src 'none'",
        "retry-after": str(max(1, math.ceil(seconds))),
    }
    return HTMLResponse(_BUSY_FROM_YOURS if status == 429 else _BUSY, status_code=status, headers=headers)


def _said(paced: Paced, line: str, *args: object) -> None:
    if count := paced.due():
        log.warning(line + ("%s"), *args, f" ({count} times since the line before)" if count > 1 else "")


class Oidc:
    """Single sign-on for a server: ``GET /_latet/sso`` (whether it serves, for the sign-in page), ``/login`` (to the
    provider), ``/link`` (**Link my company account**: the same, begun from a person's own dashboard session) and
    ``/callback`` (back from it, into a dashboard session opened ``via="sso"``). What a sign-in begun on one instance
    needs waits in Valkey, sealed with a key of its own derived from the master key: the provider may send the browser
    back to any instance. It serves while the license lists sso and no trial has ended (E1's ``serving``): a paid
    license expired or paused still serves, and whoever it does not serve opens their dashboard the Community's way
    (R5).

    Who signed in (H1): the account whose subject (``<issuer> <sub>``) the ID token names; else an account SCIM
    provisioned or an administrator linked by that user name, which has no subject yet; else, by the settings'
    ``accounts``, a new account (``group``: a member of the access group; ``open``: anyone) or none (``scim``:
    ``not-provisioned``). Never a person added by hand found by an e-mail or a user name: they link their account from
    their own session, to a company account that names them as their account does (``People.link_own``), or an
    administrator links them. The ``email`` claim is a person's e-mail only when ``email_verified`` is true."""

    def __init__(
        self,
        edition: EnterpriseEdition,
        server: Server,
        people: People,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.edition = edition
        self.server = server
        self.people = people
        self.clock = clock
        # How the server reaches the provider: at an address checked at each connection (PinnedTransport); the tests
        # give the fake provider's client here.
        self.http: Callable[[], httpx.AsyncClient] | None = None
        self._sealer = server.master.sealer(b"sso-flow")  # the waiting sign-ins: a purpose of their own (F15)
        self._known: _Known | None = None
        self._unreached: tuple[str, float] | None = None  # the issuer that did not answer, and when
        self._waits: Any = None  # _WAIT, registered with Valkey's client at the first sign-in begun

    def routes(self) -> list[BaseRoute]:
        return [
            Route("/_latet/sso", self.status, methods=["GET"]),
            Route("/_latet/sso/login", self.login, methods=["GET"]),
            Route(LINK, self.link, methods=["GET"]),
            Route(CALLBACK, self.callback, methods=["GET"]),
        ]

    def settings(self) -> SsoSettings | None:
        return self.edition.settings().sso

    def refusal(self) -> str | None:
        """Why single sign-on does not serve now (``refused_by``); None while it does."""
        return refused_by(self.edition.licensing.standing())

    def ready(self) -> bool:
        """Single sign-on can be used: the license serves sso and the settings set it up."""
        return self.edition.licensing.serving("sso") and self.settings() is not None

    def redirect_uri(self) -> str:
        return self.server.settings.url.rstrip("/") + CALLBACK

    def _secure(self) -> bool:
        return self.server.settings.url.startswith("https://")

    def _client(self, issuer: str) -> httpx.AsyncClient:
        if self.http is not None:
            return self.http()
        return httpx.AsyncClient(
            transport=PinnedTransport(issuer), timeout=15.0, trust_env=False, follow_redirects=False
        )

    async def status(self, request: Request) -> Response:
        try:
            ready = await in_work(self.ready)
        except StoresUnavailable:
            ready = False
        if not ready:  # the sign-in page shows the terminal's way alone
            return JSONResponse({"error": "not found"}, status_code=404, headers={"cache-control": "no-store"})
        return JSONResponse({"login": "/_latet/sso/login"}, headers={"cache-control": "no-store"})

    async def _provider(self, settings: SsoSettings, *, keys_again: bool = False) -> tuple[Provider, KeySet]:
        """The provider's discovery document and keys, kept for an hour; the keys fetched again when a token names a
        key id they lack, at most once in ``KEYS_AGAIN``. A provider that did not answer is not asked again for
        ``UNREACHED`` seconds (M5)."""
        now = self.clock()
        unreached = self._unreached
        if unreached is not None and unreached[0] == settings.issuer and 0 <= now - unreached[1] < UNREACHED:
            raise SignInError("unavailable", f"the provider did not answer less than {UNREACHED:g} seconds ago")
        known = self._known
        if known is not None and known.issuer == settings.issuer and 0 <= now - known.fetched < PROVIDER_TTL:
            if not keys_again:
                return known.provider, known.keys
            if 0 <= now - known.keys_fetched < KEYS_AGAIN:
                raise SignInError(
                    "failed",
                    f"the ID token is signed with a key the provider does not publish (its keys were read less than "
                    f"{KEYS_AGAIN:g} seconds ago)",
                )
            self._known = known = replace(known, keys_fetched=now)  # tried now, whatever it answers
            async with self._client(settings.issuer) as http:
                keys = await fetch_keys(http, known.provider)
            self._known = replace(known, keys=keys)
            return known.provider, keys
        try:
            async with self._client(settings.issuer) as http:
                provider = await discover(http, settings.issuer)
                keys = await fetch_keys(http, provider)
        except SignInError:
            self._unreached = (settings.issuer, now)
            raise
        self._known, self._unreached = _Known(settings.issuer, provider, keys, now, now), None
        return provider, keys

    async def test(self, settings: SsoSettings) -> tuple[dict[str, Any], str]:
        """Whether the provider answers as single sign-on needs: its discovery document and its keys (for an
        administrator: its own words in ``error``, never in a log line), and the outcome the administration log records
        of the test: ``ok``, or the code of what refused it (``unavailable``, ``failed``), never the provider's words
        nor an address. The client secret is checked by a real sign-in only."""
        answer: dict[str, Any] = {
            "ok": False,
            "error": None,
            "issuer": None,
            "authorization_endpoint": None,
            "token_endpoint": None,
            "keys": 0,
        }
        try:
            async with self._client(settings.issuer) as http:
                provider = await discover(http, settings.issuer)
                keys = await fetch_keys(http, provider)
        except SignInError as exc:
            return {**answer, "error": exc.said}, exc.code
        found = {
            **answer,
            "ok": True,
            "issuer": provider.issuer,
            "authorization_endpoint": provider.authorization_endpoint,
            "token_endpoint": provider.token_endpoint,
            "keys": len(keys),
        }
        return found, "ok"

    async def login(self, request: Request) -> Response:
        return await self._begin(request, None)

    async def link(self, request: Request) -> Response:
        """**Link my company account** (H1): a person added by hand, in their own dashboard session, signs in at the
        provider; the subject it comes back with is linked to them only when the ID token names them as their account
        does (``People.link_own``: its user name, else the e-mail given them by hand or by SCIM, verified), since the
        session may be held by an administrator who connected a computer as them, or an agent there; else refused
        ``not-provisioned`` (an administrator links them), and ``failed`` when it is another person's already. Only a
        navigation of the server's own pages begins it (``sec-fetch-site``), and the session's cookie is SameSite
        strict."""
        if request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
            return _back("failed")
        try:
            if not await in_work(self.server.bus.allow, f"sso-login:{address(request)}", *LOGINS):
                _said(_LIMITED, "single sign-on: more than %d sign-ins a minute begun from one address", LOGINS[0])
                return _back("unavailable")
            found = await in_work(self._signed_in, request.cookies.get(COOKIE, ""))
        except StoresUnavailable:
            return _back("unavailable")
        if found is None:
            return _back("expired")
        person, via = found
        return await self._begin(request, person, limited=False, via=via)

    def _signed_in(self, sid: str) -> tuple[str, Via] | None:
        """In a work thread: the enabled person whose dashboard session ``sid`` is, as PostgreSQL has them now, and how
        that session was opened."""
        session = self.server.sign_ins.session_of(sid) if sid else None
        identity = self.server.identities.current(session[0]) if session else None
        if session is None or identity is None or identity.disabled or identity.kind != "person":
            return None
        return identity.id, session[1]

    async def _begin(self, request: Request, link: str | None, *, limited: bool = True, via: Via = "sso") -> Response:
        """To the provider, the sign-in waiting in Valkey (``link``: the person whose session asked to link their
        account; ``via``: the kind of the session it opens, that session's for a link, so that linking from a one-time
        code's session opens no sso session, which ``company_changes: sso`` would take for one)."""
        try:
            refused = await in_work(self.refusal)
            if refused is not None:
                return _back(refused)
            settings = await in_work(self.settings)
            if settings is None:
                return _back("unavailable")
            if limited and not await in_work(self.server.bus.allow, f"sso-login:{address(request)}", *LOGINS):
                _said(_LIMITED, "single sign-on: more than %d sign-ins a minute begun from one address", LOGINS[0])
                return _back("unavailable")
            provider, _keys = await self._provider(settings)
        except SignInError as exc:
            log.warning("single sign-on: %s", exc.detail)
            return _back(exc.code)
        except StoresUnavailable:
            return _back("unavailable")
        state, nonce, browser = (secrets.token_urlsafe(32) for _ in range(3))
        verifier, challenge = pkce_pair()
        member = _digest(state)
        name = f"sso:flow:{member}"
        # Its address's sets, each named by a keyed hash (no address in Valkey): the /64's or the IPv4 address's, and
        # for IPv6 its /48's (``wide``). A flow begun before the /48 was counted names no ``wide``.
        bucket, *wider = counted_in(f"{FLOWS_FROM}:{address(request)}")
        counted = self.server.bus.rate_key(bucket)
        wide = self.server.bus.rate_key(wider[0]) if wider else None
        flow = {
            "verifier": verifier,
            "nonce": nonce,
            "browser": _digest(browser),
            "expires": self.clock() + FLOW_TTL,
            "issuer": settings.issuer,
            "link": link,
            "via": via,
            "counted": counted,  # which sets of an address's its callback takes it out of (``_given_back``)
        }
        if wide is not None:
            flow["wide"] = wide  # beside ``counted``, which an older instance frees as it knows it (L2)
        sealed = self._sealer.seal_json(flow, self.server.bus.key(name))  # bound to its own key
        try:  # room in the address's sets and the server's first: past any, nothing is stored
            refused = await in_work(self._wait, counted, wide, name, member, sealed)
        except StoresUnavailable:
            return _back("unavailable")
        if refused is not None:
            status, seconds, which = refused
            if status == 429 and which == 2 and wide is not None:
                line = (
                    "single sign-on: more than %d sign-ins begun from one /48 network are waiting: new ones from it "
                    "wait"
                )
                _said(_FLOODED_WIDE, line, FLOWS_FROM_WIDE)
            elif status == 429:
                line = "single sign-on: more than %d sign-ins begun from one address are waiting: new ones from it wait"
                _said(_FLOODED, line, FLOWS_FROM_ONE)
            else:
                _said(_CROWDED, "single sign-on: %d sign-ins are waiting at the provider: new ones wait", FLOWS_WAITING)
            return _busy(status, seconds)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.client_id,
                "redirect_uri": self.redirect_uri(),
                "scope": " ".join(settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        joiner = "&" if "?" in provider.authorization_endpoint else "?"
        response = RedirectResponse(f"{provider.authorization_endpoint}{joiner}{query}", 303, headers=_NO_STORE)
        response.set_cookie(
            FLOW_COOKIE, browser, max_age=int(FLOW_TTL), path="/_latet/sso", httponly=True, samesite="lax",
            secure=self._secure(),
        )  # fmt: skip
        return response

    def _wait(
        self, counted: str, wide: str | None, name: str, member: str, sealed: bytes
    ) -> tuple[int, float, int] | None:
        """In a work thread: the sign-in ``sealed`` kept in Valkey under ``name`` for FLOW_TTL, and named ``member`` in
        its address's set (``counted``: FLOWS_FROM_ONE), its /48's (``wide``, IPv6 alone: FLOWS_FROM_WIDE) and the
        server's (FLOWS_WAITING), in one step when each has room (``_WAIT``); None then. Else nothing is stored, and the
        refusal's status (429: an address's set, 503: the server's), the seconds until the oldest sign-in waiting there
        ends, and which set refused it (1: the address's, 2: the /48's, the last: the server's). The address's sets
        first, the /64 before the /48: what they refuse takes nothing of the server's, so that a few addresses or a few
        /48s never fill it."""
        bus = self.server.bus
        if self._waits is None:
            self._waits = bus.client.register_script(_WAIT)
        sets = [counted] if wide is None else [counted, wide]
        most = [FLOWS_FROM_ONE] if wide is None else [FLOWS_FROM_ONE, FLOWS_FROM_WIDE]
        keys = [*(bus.key(named) for named in sets), bus.key(bus.rate_key(FLOWS)), bus.key(name)]
        life = max(1, int(FLOW_TTL * 1000))
        args = [member, life, sealed, len(sets), FLOWS_WAITING, *most]
        status, left, which = self._waits(keys=keys, args=args)
        return None if int(status) == 0 else (int(status), int(left) / 1000, int(which))

    def _take(self, key: str) -> dict[str, Any] | None:
        """The sign-in waiting for this state, taken and opened. Valkey reads and deletes it in one command (GETDEL,
        as E0b's SignIns takes a one-time code), so that of two callbacks with the same state, on one instance or on
        two, at the same moment too, only the first gets it, and it waits no more (``_given_back``). None when none
        waits or it is not this server's."""
        bus = self.server.bus
        name = bus.key(f"sso:flow:{key}")
        sealed = bus.client.getdel(name)
        if sealed is None:
            return None
        try:
            found = self._sealer.open_json(sealed, name)
        except (InvalidTag, ValueError, TypeError, KeyError):  # not sealed by this server, or under another key
            log.warning("single sign-on: a sign-in waiting in Valkey could not be read")
            return None
        if not isinstance(found, dict):
            return None
        self._given_back(key, found.get("counted"), found.get("wide"))
        return cast(dict[str, Any], found)

    def _given_back(self, member: str, counted: object, wide: object = None) -> None:
        """A sign-in its callback took (``_take``, once, whether it then signs in or is refused) waits no more: out of
        the server's set and its address's, which the flow names (sealed by this server, bound to the flow's name):
        ``counted``, and for IPv6 ``wide``, its /48's, each only when it names a set of an address's. A flow begun
        before the /48 was counted names no ``wide``, and frees what it names. Taking out what is not there takes out
        nothing: a flow never counted, or a sign-in given back already, gives nothing back, and no set is made. A
        Valkey that does not take it is said by its error's type: the sign-in goes on, and the entry ends with the
        flow's life."""
        bus = self.server.bus
        sets = [bus.key(bus.rate_key(FLOWS))]
        for named in (counted, wide):
            if isinstance(named, str) and named.startswith(f"rate:{FLOWS_FROM}:"):
                sets.append(bus.key(named))
        try:
            pipe = bus.client.pipeline()
            for name in sets:
                pipe.zrem(name, member)
            pipe.execute()
        except Exception as exc:  # its words can name Valkey's address: its type alone is said
            _said(_UNCOUNTED, "single sign-on: a finished sign-in stays counted until it ends (%s)", described(exc))

    async def callback(self, request: Request) -> Response:
        query = request.query_params
        try:
            if not await in_work(self.server.bus.allow, f"sso-callback:{address(request)}", *CALLBACKS):
                _said(_LIMITED, "single sign-on: more than %d callbacks a minute from one address", CALLBACKS[0])
                return _back("unavailable")
            flow = await in_work(self._take, _digest(query.get("state", "")))  # a state is used once
            if flow is None:  # none waits: too late, used already, or never begun; no sign-in to record
                _said(_UNWAITED, "single sign-on: a callback came back with no sign-in waiting for its state")
                return _back("expired")
            refused = await in_work(self.refusal)
        except StoresUnavailable:
            return _back("unavailable")
        try:
            if refused is not None:  # a trial that ended meanwhile: said so, whatever else
                raise SignInError(refused, "single sign-on does not serve now (the license)")
            if error := query.get("error"):
                named = error if error in PROVIDER_ERRORS else "other"
                code = "not-allowed" if named == "access_denied" else "failed"
                raise SignInError(code, f"the provider answered {named}")
            if not isinstance(flow.get("expires"), int | float) or flow["expires"] < self.clock():
                raise SignInError("expired", "the sign-in came back too late")
            browser = request.cookies.get(FLOW_COOKIE, "")
            if not browser or not hmac.compare_digest(_digest(browser), str(flow.get("browser", ""))):
                raise SignInError("failed", "the sign-in came back to another browser than the one that began it")
            settings = await in_work(self.settings)
            if settings is None:
                raise SignInError("unavailable", "single sign-on is off")
            if settings.issuer != flow.get("issuer"):
                raise SignInError("failed", "the issuer changed while the sign-in was at the provider")
            issuer, claims = await self._claims(settings, query.get("code", ""), flow)
            link = flow.get("link") if isinstance(flow.get("link"), str) else None
            identity = await working(
                self._person, settings, issuer, claims, client_of(request.scope), link, refusals=(SignInError,)
            )
            await in_work(self._record, identity.id, "signed-in", None)
            via: Via = "code" if flow.get("via") == "code" else "sso"  # a link keeps its session's kind
            sid = await in_work(functools.partial(self.server.sign_ins.open, identity.id, via=via))
        except SignInError as exc:
            log.warning("single sign-on refused: %s", exc.detail)  # fixed words, an error's type and host (L3)
            with contextlib.suppress(StoresUnavailable):
                await in_work(self._record, exc.identity, "refused", exc.code)
            return _back(exc.code)
        except StoresUnavailable:
            return _back("unavailable")
        self.server.known_sessions.keep(sid)  # known valid from its first request, as the Community's sign-in does
        response = HTMLResponse(_SIGNED_IN, headers={**_NO_STORE, "content-security-policy": "default-src 'none'"})
        response.set_cookie(
            COOKIE, sid, max_age=int(SESSION_TTL), path="/_latet", httponly=True, samesite="strict",
            secure=self._secure(),
        )  # fmt: skip
        response.delete_cookie(FLOW_COOKIE, path="/_latet/sso")
        return response

    def _client_secret(self, settings: SsoSettings) -> str | None:
        """In a work thread: the client secret, which opens only for the issuer it was saved for (M4)."""
        if settings.client_secret is None:
            return None
        try:
            secret = self.edition.secrets.load(settings.client_secret, bound=secret_bound(settings.issuer))
        except (InvalidTag, ValueError):
            raise SignInError(
                "unavailable",
                "the client secret was saved for another issuer: save it again (`latet server sign-in secret`)",
            ) from None
        if secret is None:
            raise SignInError("unavailable", "the client secret is not saved: `latet server sign-in secret`")
        return secret

    async def _claims(self, settings: SsoSettings, code: str, flow: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        secret = await working(self._client_secret, settings, refusals=(SignInError,))
        provider, keys = await self._provider(settings)
        verifier, nonce = flow.get("verifier"), flow.get("nonce")
        if not isinstance(verifier, str) or not isinstance(nonce, str):
            raise SignInError("failed", "the sign-in waiting in Valkey holds no verifier or nonce")
        async with self._client(settings.issuer) as http:
            id_token = await exchange(http, provider, settings.client_id, secret, code, verifier, self.redirect_uri())
        expected = {"issuer": provider.issuer, "client_id": settings.client_id, "nonce": nonce}
        try:
            return provider.issuer, verify(id_token, keys, now=self.clock(), **expected)
        except InvalidKeyIdError:
            provider, keys = await self._provider(settings, keys_again=True)  # once for this sign-in
        try:
            return provider.issuer, verify(id_token, keys, now=self.clock(), **expected)
        except InvalidKeyIdError as exc:
            raise SignInError("failed", "the ID token is signed with a key the provider does not publish") from exc

    def _person(
        self, settings: SsoSettings, issuer: str, claims: Mapping[str, Any], at: str, link: str | None
    ) -> Identity:
        """Who signed in (in a work thread; the class's words, H1), their account linked and, unless SCIM provisions
        the accounts, their groups from the ID token: one transaction with their entries (R6), by the person, the
        license weighed in it (F18)."""
        identities, people = self.server.identities, self.people
        sub = str(claims["sub"])
        subject = f"{issuer} {sub}"
        verified = claims.get("email_verified") is True
        if settings.user_name_claim == "email" and not verified:
            raise SignInError("failed", "the user name is the ID token's e-mail, which the provider has not verified")
        user_name = _as_sent(claim(claims, settings.user_name_claim))
        email = _text(claims.get("email")) if verified else None
        if settings.allowed_domains:
            hd = claims.get("hd")
            if not isinstance(hd, str) or hd.strip().lower() not in settings.allowed_domains:
                raise SignInError("not-allowed", "the ID token's hosted domain is not an allowed domain")
        groups = groups_in(claims, settings.groups_claim)
        allowed = settings.access_group is None or member_of(groups, settings.access_group)
        by_subject = people.find(subject=subject)
        claiming: str | None = None
        if link is not None:
            if by_subject is not None and by_subject != link:
                raise SignInError("failed", "this company account is linked to another person", identity=link)
            identity_id: str | None = link
        else:
            identity_id = by_subject
            if identity_id is None and user_name is not None:
                identity_id = claiming = people.unclaimed(user_name, scim_only=False)
        identity = identities.current(identity_id) if identity_id is not None else None
        if identity_id is not None and (identity is None or identity.kind != "person"):
            raise SignInError("failed", "the account found for this sign-in is gone")
        if identity is not None and identity.disabled:
            raise SignInError("disabled", "the account is disabled", identity=identity.id)
        if identity is not None and not allowed:  # the account stays: a typo in access_group must not disable everyone
            raise SignInError("not-allowed", "no longer in the access group", identity=identity.id)
        if identity is None and settings.accounts == "scim":
            raise SignInError("not-provisioned", "accounts come from SCIM, and none was provisioned for this person")
        if identity is None and not allowed:
            raise SignInError("not-allowed", "not in the access group")
        account = people.account(identity.id) if identity is not None else None
        try:
            with people.changing(identity.id if identity is not None else "server", at) as within:
                refused = refused_by(self.edition.licensing.standing_in(within.conn))
                if refused is not None:  # as the transaction finds the license
                    raise SignInError(refused, "single sign-on does not serve now (the license)")
                if identity is None:
                    named = _text(claims.get("name")) or user_name or sub
                    try:
                        identity, _code = identities.add_person(
                            clean(named, NAME_LENGTH) or "person", email, source="oidc", within=within.changes
                        )
                    except EmailTaken:
                        raise SignInError(
                            "not-provisioned", "an enabled person has this e-mail already: an administrator links them"
                        ) from None
                    within.actor = identity.id  # the person signing in made their account
                    within.note("person.added", identity.id, {"source": "oidc"})
                if link is not None and by_subject is None:  # only a company account that names them (fix round 3)
                    own = email if email is not None and within_domains(email, settings.allowed_domains) else None
                    people.link_own(identity.id, subject=subject, user_name=user_name, email=own, within=within)
                elif claiming is not None:
                    people.link(identity.id, within=within, subject=subject, claim=user_name)
                elif by_subject is None:
                    people.link(identity.id, within=within, subject=subject)
                elif user_name is not None:  # by the subject: a user name SCIM changed is confirmed by this one alone
                    people.confirmed(identity.id, user_name, within=within)
                if user_name is not None and (account is None or account.user_name is None):
                    try:
                        people.link(identity.id, within=within, user_name=user_name)
                    except (Taken, ValueError):  # another account has it, or it cannot be kept: the subject names them
                        log.warning("single sign-on: the user name of %s is not kept (another account's)", identity.id)
                if settings.accounts != "scim":
                    people.apply_claims(identity.id, groups, settings.groups, within=within)
        except NotYours:  # fixed words, naming no claim; an administrator links them
            raise SignInError("not-provisioned", NOT_YOURS, identity=link) from None
        except Taken:
            raise SignInError(
                "failed", "the account is linked to another company account, or this one to another person"
            ) from None
        except ValueError:  # People's checks: a subject, a user name or a group past its length, or a control character
            raise SignInError("failed", "the ID token names its person in a way Latet does not keep") from None
        identities.seen(identity.id)
        return identity

    def _record(self, identity_id: str | None, outcome: str, reason: str | None) -> None:
        """A sign-in event (E4 keeps it): the person's id and their first group's, never their name or e-mail."""
        groups = self.people.groups_of(identity_id) if identity_id is not None else []
        event = make_event(
            "sign-in",
            clock=self.clock,
            identity=identity_id,
            kind="person",
            group=groups[0].id if groups else None,
            outcome=outcome,
            reason=reason,
        )
        self.edition.record(event)
