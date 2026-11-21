"""A company's identity provider in-process, for the tests of single sign-on: its discovery document, its keys and its
token endpoint (OpenID Connect, with PKCE), and what a person's browser does at its sign-in page. The server under
test reaches it through httpx's ASGI transport: nothing leaves the process."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ISSUER = "https://login.acme.test/realms/acme"
CLIENT_ID = "latet"
CLIENT_SECRET = "Q7v9-KxW2~mP4.zR8" + "-hT5wL3nB6yD1"  # the characters real secrets have; split for secret scanners
SSO = {
    "issuer": ISSUER,
    "client_id": CLIENT_ID,
    "accounts": "group",
    "access_group": "latet-users",
    "groups": {"latet-admins": "Latet admins", "payments-leads": "Payments leads"},
}
_PATH = urlsplit(ISSUER).path
_ENDPOINT = f"{ISSUER}/protocol/openid-connect"


@dataclass(frozen=True)
class SigningKey:
    """A key pair of the provider's: what it signs ID tokens with, and what it publishes of it."""

    kid: str
    private: rsa.RSAPrivateKey

    def jwk(self) -> dict[str, Any]:
        """The public key as the provider's key set lists it."""
        return {
            **RSAAlgorithm.to_jwk(self.private.public_key(), as_dict=True),
            "kid": self.kid,
            "use": "sig",
            "alg": "RS256",
        }

    def public_pem(self) -> bytes:
        return self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


def rsa_key(kid: str, size: int = 2048) -> SigningKey:
    return SigningKey(kid, rsa.generate_private_key(public_exponent=65537, key_size=size))


def _part(value: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def hmac_signed(secret: bytes, header: Mapping[str, Any], claims: Any) -> str:
    """A token signed with HMAC-SHA256 by hand, whatever the secret is (PyJWT refuses a public key as one)."""
    signing_input = f"{_part(header)}.{_part(claims)}"
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def signed(claims: Mapping[str, Any], key: SigningKey, alg: str = "RS256") -> str:
    """A token signed with ``key``, its id in the header, with exactly ``claims``."""
    return jwt.encode(dict(claims), key.private, algorithm=alg, headers={"kid": key.kid})


class _Recorded(httpx.AsyncBaseTransport):
    """The provider's transport, noting the address of every request it is sent (``FakeProvider.requested``)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, requested: list[str]) -> None:
        self.inner = inner
        self.requested = requested

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class FakeProvider:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.methods: tuple[str, ...] = ("client_secret_post", "client_secret_basic")
        self.key = rsa_key("key-1")
        self.published = [self.key]
        self.issuer_named = ISSUER  # the issuer its discovery document names
        self.endpoints = {
            "authorization_endpoint": f"{_ENDPOINT}/auth",
            "token_endpoint": f"{_ENDPOINT}/token",
            "jwks_uri": f"{_ENDPOINT}/certs",
        }
        self.codes: dict[str, dict[str, Any]] = {}
        self.token_requests: list[dict[str, str]] = []
        self.keys_fetched = 0
        self.requested: list[str] = []  # every address the server under test asked for, in order
        self.app = Starlette(
            routes=[
                Route(f"{_PATH}/.well-known/openid-configuration", self._discovery),
                Route(f"{_PATH}/protocol/openid-connect/certs", self._keys),
                Route(f"{_PATH}/protocol/openid-connect/token", self._token, methods=["POST"]),
            ]
        )

    def client(self) -> httpx.AsyncClient:
        """How the server under test reaches it (`Oidc.http`)."""
        transport = _Recorded(httpx.ASGITransport(app=self.app), self.requested)
        return httpx.AsyncClient(transport=transport, trust_env=False)

    def rotate(self) -> None:
        """New keys: ID tokens are signed with a key of a new id, and the old one is no longer published."""
        self.key = rsa_key("key-2")
        self.published = [self.key]

    def id_token(self, claims: Mapping[str, Any], *, key: Any = None, alg: str = "RS256") -> str:
        """An ID token for Latet's client: the usual claims, then ``claims``."""
        now = int(self.clock())
        body = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **claims}
        if alg.startswith("HS"):  # ``key`` is then a shared secret
            return hmac_signed(key.encode() if isinstance(key, str) else key, {"alg": alg, "typ": "JWT"}, body)
        return signed(body, key or self.key, alg)

    def authorize(self, url: str, person: Mapping[str, Any]) -> str:
        """What the provider does when ``person`` (their claims) signs in at its page, opened at ``url``: it checks the
        request, keeps a code for it, and sends the browser back to the redirect URI (the address returned)."""
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == self.endpoints["authorization_endpoint"]
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert query["response_type"] == "code" and query["client_id"] == CLIENT_ID
        assert query["code_challenge_method"] == "S256" and "openid" in query["scope"].split()
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "person": dict(person),
            "nonce": query["nonce"],
            "challenge": query["code_challenge"],
            "redirect_uri": query["redirect_uri"],
        }
        return f"{query['redirect_uri']}?{urlencode({'code': code, 'state': query['state']})}"

    async def _discovery(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": self.issuer_named,
                **self.endpoints,
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": list(self.methods),
            }
        )

    async def _keys(self, request: Request) -> JSONResponse:
        self.keys_fetched += 1
        return JSONResponse({"keys": [key.jwk() for key in self.published]})

    async def _token(self, request: Request) -> JSONResponse:
        form = {key: values[0] for key, values in parse_qs((await request.body()).decode()).items()}
        self.token_requests.append({**form, "authorization": request.headers.get("authorization", "")})
        client_id, secret = form.get("client_id"), form.get("client_secret")
        basic = request.headers.get("authorization", "")
        if basic.startswith("Basic "):
            user, _, password = base64.b64decode(basic[6:]).decode().partition(":")
            client_id, secret = unquote(user), unquote(password)
        if client_id != CLIENT_ID or secret != CLIENT_SECRET:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        grant = self.codes.pop(form.get("code", ""), None)
        if (
            grant is None
            or form.get("grant_type") != "authorization_code"
            or form.get("redirect_uri") != grant["redirect_uri"]
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        verifier = form.get("code_verifier", "").encode()
        if base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=").decode() != grant["challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, status_code=400)
        token = self.id_token({"nonce": grant["nonce"], **grant["person"]})
        return JSONResponse(
            {"access_token": secrets.token_hex(8), "token_type": "Bearer", "expires_in": 300, "id_token": token}
        )
