"""The ID token and the provider's discovery document, checked as OpenID Connect asks (ee/oidc.py)."""

import base64
import gzip
import hashlib
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from jwt.warnings import InsecureKeyLengthWarning

from latet.ee.oidc import (
    LEEWAY,
    InvalidKeyIdError,
    Provider,
    SignInError,
    discover,
    exchange,
    fetch_keys,
    groups_in,
    member_of,
    pkce_pair,
    verify,
    within_domains,
)

from .fake_provider import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeProvider, hmac_signed, rsa_key, signed
from .server_support import NOW, Clock

NONCE = "n-0123456789"
REDIRECT = "https://latet.acme.lan/_latet/sso/callback"
KIB = 1024


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def provider(clock):
    return FakeProvider(clock)


async def _keys(provider):
    async with provider.client() as http:
        return await fetch_keys(http, await discover(http, ISSUER))


def _check(token, keys, clock):
    return verify(token, keys, issuer=ISSUER, client_id=CLIENT_ID, nonce=NONCE, now=clock())


def _part(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def _unsigned(claims, header=None):
    return f"{_part(header or {'alg': 'none'})}.{_part(claims)}."


def _signed(provider, body):
    """A token signed with the provider's key, with exactly ``body`` as its claims."""
    return signed(body, provider.key)


async def _keys_of(published):
    """The keys Latet reads from a key set that lists ``published`` (JWKs)."""
    found = {"keys": published}
    async with _provider_answering(lambda request: httpx.Response(200, json=found)) as http:
        return await fetch_keys(http, Provider(*_document().values(), ("client_secret_basic",)))


def _usual(**claims):
    return {"iss": ISSUER, "aud": CLIENT_ID, "iat": int(NOW), "exp": int(NOW) + 300, "sub": "0f6a-emily", **claims}


async def test_a_token_the_provider_signed_for_latet_gives_its_claims(provider, clock):
    keys = await _keys(provider)
    assert _check(provider.id_token({"sub": "0f6a-emily", "nonce": NONCE}), keys, clock)["sub"] == "0f6a-emily"


@pytest.mark.parametrize(
    "change",
    [
        {"aud": "another-app"},
        {"iss": "https://login.acme.test/realms/other"},
        {"iss": ISSUER + "/"},  # the discovered issuer is compared exactly
        {"nonce": "n-another-sign-in"},
        {"nonce": [NONCE]},  # a list holding the value is not the value
        {"nonce": None},
        {"exp": int(NOW) - 600},
        {"exp": float("nan")},  # never earlier than now, never later
        {"exp": float("inf")},
        {"exp": True},
        {"exp": "2099-01-01"},
        {"exp": 10**400},  # too large for a float: math.isfinite cannot say
        {"iat": -(10**400)},
        {"nonce": chr(0xD800)},  # a lone surrogate: no UTF-8 for it
        {"iat": int(NOW) + 600},
        {"iat": float("nan")},
        {"nbf": int(NOW) + 600},
        {"nbf": float("nan")},
        {"azp": "another-app"},
        {"aud": [CLIENT_ID, "another-app"]},
        {"sub": ""},
        {"sub": None},
        {"sub": 7},
    ],
)
async def test_a_token_meant_for_something_else_is_refused(provider, clock, change):
    keys = await _keys(provider)
    with pytest.raises(SignInError) as refused:
        _check(provider.id_token({"sub": "0f6a-emily", "nonce": NONCE, **change}), keys, clock)
    assert refused.value.code == "failed"


async def test_several_audiences_hold_when_latet_is_the_authorized_party(provider, clock):
    keys = await _keys(provider)
    token = provider.id_token({"sub": "0f6a-emily", "nonce": NONCE, "aud": [CLIENT_ID, "x"], "azp": CLIENT_ID})
    assert _check(token, keys, clock)["azp"] == CLIENT_ID


@pytest.mark.parametrize("missing", ["iss", "aud", "sub", "exp", "iat", "nonce"])
async def test_a_token_without_an_essential_claim_is_refused(provider, clock, missing):
    keys = await _keys(provider)
    body = _usual(nonce=NONCE)
    del body[missing]
    with pytest.raises(SignInError) as refused:
        _check(_signed(provider, body), keys, clock)
    assert refused.value.code == "failed"


async def test_the_clocks_may_differ_by_the_leeway_and_no_more(provider, clock):
    keys = await _keys(provider)
    assert LEEWAY == 120
    now = int(NOW)
    held = [_usual(nonce=NONCE, exp=now - 100), _usual(nonce=NONCE, iat=now + 100, exp=now + 400)]
    for body in held:
        assert _check(_signed(provider, body), keys, clock)["sub"] == "0f6a-emily"
    refused = [_usual(nonce=NONCE, exp=now - 200), _usual(nonce=NONCE, iat=now + 200, exp=now + 400)]
    for body in refused:
        with pytest.raises(SignInError):
            _check(_signed(provider, body), keys, clock)


async def test_the_leeway_ends_at_120_seconds_exactly(provider, clock):
    keys = await _keys(provider)
    now = int(NOW)
    held = [_usual(nonce=NONCE, exp=now - 119), _usual(nonce=NONCE, iat=now + 120, exp=now + 400)]
    for body in held:
        assert _check(_signed(provider, body), keys, clock)["sub"] == "0f6a-emily"
    refused = [_usual(nonce=NONCE, exp=now - 120), _usual(nonce=NONCE, iat=now + 121, exp=now + 400)]
    for body in refused:
        with pytest.raises(SignInError):
            _check(_signed(provider, body), keys, clock)


async def test_a_token_the_providers_key_did_not_sign_is_refused(provider, clock):
    keys = await _keys(provider)
    claims = {"sub": "0f6a-emily", "nonce": NONCE}
    body = _usual(nonce=NONCE)
    public_pem = provider.key.public_pem()
    forged = [
        provider.id_token(claims, key=rsa_key("key-1")),  # the same key id, another key
        provider.id_token(claims, key=CLIENT_SECRET, alg="HS256"),  # the client secret as a key
        hmac_signed(public_pem, {"alg": "HS256", "kid": "key-1"}, body),  # the provider's public key as an HMAC key
        _unsigned(body),
        _unsigned(body, {"alg": "none", "kid": "key-1"}),
        _signed(provider, body)[:-4] + "AAAA",  # its signature changed
        "not-a-token",
        "a.b.c",
        "",
    ]
    for token in forged:
        with pytest.raises(SignInError) as refused:
            _check(token, keys, clock)
        assert refused.value.code == "failed"


async def test_none_and_hmac_with_the_public_key_are_refused_for_a_key_that_names_no_algorithm(provider, clock):
    """The strongest case: the published key does not say its algorithm, so every RSA algorithm is open to it."""
    keys = await _keys_of([{**provider.key.jwk(), "alg": None}])
    body = _usual(nonce=NONCE)
    assert _check(signed(body, provider.key, "PS256"), keys, clock)["sub"] == "0f6a-emily"
    for token in (
        _unsigned(body, {"alg": "none", "kid": "key-1"}),
        _unsigned(body, {"alg": "none"}),
        hmac_signed(provider.key.public_pem(), {"alg": "HS256", "kid": "key-1"}, body),
        hmac_signed(json.dumps(provider.key.jwk()).encode(), {"alg": "HS256", "kid": "key-1"}, body),
    ):
        with pytest.raises(SignInError) as refused:
            _check(token, keys, clock)
        assert refused.value.code == "failed"


async def test_a_key_verifies_only_with_the_algorithm_it_is_published_for(provider, clock):
    keys = await _keys(provider)  # the fake publishes its key for RS256
    with pytest.raises(SignInError):
        _check(signed(_usual(nonce=NONCE), provider.key, "PS256"), keys, clock)


async def test_an_ec_key_verifies_with_the_algorithm_of_its_curve_only(clock):
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = {**ECAlgorithm.to_jwk(private.public_key(), as_dict=True), "kid": "ec-1"}
    keys = await _keys_of([jwk])
    body = _usual(nonce=NONCE)
    assert _check(jwt.encode(body, private, algorithm="ES256", headers={"kid": "ec-1"}), keys, clock)["sub"]
    token = jwt.encode(body, private, algorithm="ES256", headers={"kid": "ec-1"})
    forged = _part({"alg": "ES384", "kid": "ec-1"}) + token[token.index(".") :]  # another algorithm named
    with pytest.raises(SignInError):
        _check(forged, keys, clock)


async def test_a_key_shorter_than_2048_bits_is_refused(clock):
    weak = rsa_key("weak", size=1024)
    keys = await _keys_of([weak.jwk()])
    with pytest.warns(InsecureKeyLengthWarning):
        token = signed(_usual(nonce=NONCE), weak)
    with pytest.raises(SignInError):
        _check(token, keys, clock)


async def test_a_key_set_with_its_private_members_is_read_for_its_public_key(provider, clock):
    private_jwk = {**RSAAlgorithm.to_jwk(provider.key.private, as_dict=True), "kid": "key-1", "alg": "RS256"}
    keys = await _keys_of([private_jwk])
    assert _check(_signed(provider, _usual(nonce=NONCE)), keys, clock)["sub"] == "0f6a-emily"


async def test_an_encryption_key_is_left_aside(provider, clock):
    encryption = {**rsa_key("enc-1").jwk(), "use": "enc", "alg": None}  # an RSA key naming no algorithm, for encryption
    keys = await _keys_of([encryption, provider.key.jwk()])
    assert len(keys) == 1
    assert _check(_signed(provider, _usual(nonce=NONCE)), keys, clock)["sub"] == "0f6a-emily"
    with pytest.raises(InvalidKeyIdError):
        _check(signed(_usual(nonce=NONCE), rsa_key("enc-1")), keys, clock)


async def test_with_several_keys_a_token_must_name_its_key(provider, clock):
    keys = await _keys_of([provider.key.jwk(), rsa_key("key-2").jwk()])
    unnamed = jwt.encode(_usual(nonce=NONCE), provider.key.private, algorithm="RS256")
    with pytest.raises(InvalidKeyIdError):
        _check(unnamed, keys, clock)
    alone = await _keys_of([provider.key.jwk()])
    assert _check(unnamed, alone, clock)["sub"] == "0f6a-emily"


async def test_a_signed_token_whose_claims_are_not_an_object_is_refused(provider, clock):
    keys = await _keys(provider)
    payload = json.dumps([_usual(nonce=NONCE)]).encode()
    token = jwt.PyJWS().encode(payload, provider.key.private, algorithm="RS256", headers={"kid": provider.key.kid})
    with pytest.raises(SignInError) as refused:
        _check(token, keys, clock)
    assert refused.value.code == "failed"


async def test_a_key_id_the_provider_does_not_publish_is_said_so_for_the_keys_to_be_fetched_again(provider, clock):
    keys = await _keys(provider)
    provider.rotate()
    with pytest.raises(InvalidKeyIdError):
        _check(provider.id_token({"sub": "0f6a-emily", "nonce": NONCE}), keys, clock)


async def test_a_refused_token_is_logged_by_fixed_words_never_its_own_text(provider, clock):
    keys = await _keys(provider)
    said = "\n2027-03-01 CRITICAL forged"
    for header in (
        {"alg": "RS256" + said, "kid": "key-1"},
        {"alg": "RS256", "kid": "key-1", "crit": ["b64" + said]},  # PyJWT's error quotes the extension's name
    ):
        with pytest.raises(SignInError) as refused:
            _check(_unsigned(_usual(nonce=NONCE), header), keys, clock)
        assert "forged" not in refused.value.detail and "\n" not in refused.value.detail


async def test_an_issuer_written_another_way_is_named_and_a_trailing_slash_is_the_same(provider):
    provider.issuer_named = "https://login.acme.test/realms/Acme"
    async with provider.client() as http:
        with pytest.raises(SignInError) as refused:
            await discover(http, ISSUER)
    # The provider's own text goes only to an administrator's test of the settings, never to the log (L3).
    assert "realms/Acme" in refused.value.said and "realms/Acme" not in refused.value.detail
    assert refused.value.code == "unavailable"
    provider.issuer_named = ISSUER + "/"
    async with provider.client() as http:
        assert (await discover(http, ISSUER)).issuer == ISSUER + "/"


async def test_a_provider_whose_endpoints_are_not_https_is_refused(provider):
    provider.endpoints["token_endpoint"] = "http://login.acme.test/token"
    async with provider.client() as http:
        with pytest.raises(SignInError, match="token_endpoint"):
            await discover(http, ISSUER)


@pytest.mark.parametrize(
    "address",
    [
        "http://127.0.0.1:6379/certs",
        "https://127.0.0.1:6379/certs",
        "http://localhost/certs",
        "https://localhost:8443/certs",
        "https://[::1]/certs",
        # Numbers the resolver reads as an address though no address is written so: 127.1, 127.0.0.1 in one number,
        # in hex, 0 (this computer), with leading zeros.
        "https://127.1/certs",
        "https://2130706433/certs",
        "https://0x7f000001/certs",
        "https://0x7f.0.0.1/certs",
        "https://0/certs",
        "https://0177.0.0.1/certs",
        "https://127.000.000.001/certs",
    ],
)
async def test_an_https_provider_cannot_send_latet_to_its_own_loopback(provider, address):
    provider.endpoints["jwks_uri"] = address
    async with provider.client() as http:
        with pytest.raises(SignInError, match="jwks_uri") as refused:
            await discover(http, ISSUER)
    assert refused.value.code == "unavailable"
    assert provider.requested == [f"{ISSUER}/.well-known/openid-configuration"]  # nothing asked of the loopback


async def test_a_loopback_issuer_reaches_its_own_origin_only(provider):
    local = "http://localhost:8080/realms/acme"
    provider.issuer_named = local
    provider.endpoints = {
        "authorization_endpoint": f"{local}/protocol/openid-connect/auth",
        "token_endpoint": f"{local}/protocol/openid-connect/token",
        "jwks_uri": f"{local}/protocol/openid-connect/certs",
    }
    async with provider.client() as http:
        assert (await discover(http, local)).jwks_uri == f"{local}/protocol/openid-connect/certs"
    for elsewhere in ("http://localhost:6379/certs", "http://127.0.0.1:8080/certs", "http://login.acme.test/certs"):
        provider.endpoints["jwks_uri"] = elsewhere
        async with provider.client() as http:
            with pytest.raises(SignInError, match="jwks_uri"):
                await discover(http, local)


async def test_an_issuer_in_clear_off_this_computer_is_never_asked(provider):
    async with provider.client() as http:
        with pytest.raises(SignInError) as refused:
            await discover(http, "http://login.acme.test/realms/acme")
    assert refused.value.code == "unavailable"
    assert provider.requested == []


def _document(issuer=ISSUER):
    endpoint = f"{issuer}/protocol/openid-connect"
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{endpoint}/auth",
        "token_endpoint": f"{endpoint}/token",
        "jwks_uri": f"{endpoint}/certs",
    }


def _provider_answering(answer):
    """An http client whose every request gets ``answer(request)``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(answer), trust_env=False)


def _padded(value, size):
    """``value`` (a JSON object) with a field that brings it to about ``size`` bytes."""
    return json.dumps({**value, "padding": "x" * size}).encode()


class _Streamed:
    """A body sent in 64 KiB chunks with no length announced, counting the chunks read."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read = 0

    async def __aiter__(self):
        for start in range(0, len(self.body), 64 * KIB):
            self.read += 1
            yield self.body[start : start + 64 * KIB]


async def test_a_discovery_document_or_a_key_set_past_256_kib_is_not_read():
    keys = {"keys": [rsa_key("key-1").jwk()]}
    provider = Provider(*_document().values(), ("client_secret_basic",))
    async with _provider_answering(
        lambda request: httpx.Response(200, content=_padded(_document(), 300 * KIB))
    ) as http:
        with pytest.raises(SignInError) as refused:
            await discover(http, ISSUER)
    assert refused.value.code == "unavailable"
    async with _provider_answering(lambda request: httpx.Response(200, content=_padded(keys, 10 * KIB * KIB))) as http:
        with pytest.raises(SignInError) as refused:
            await fetch_keys(http, provider)
    assert refused.value.code == "unavailable"
    streamed = _Streamed(_padded(keys, 10 * KIB * KIB))
    async with _provider_answering(lambda request: httpx.Response(200, content=streamed)) as http:
        with pytest.raises(SignInError) as refused:
            await fetch_keys(http, provider)
    assert refused.value.code == "unavailable"
    assert streamed.read <= 5  # it stopped reading at the cap, not after the whole body
    announced = _Streamed(_padded(keys, 10 * KIB * KIB))
    with_length = {"content-length": str(len(announced.body))}
    async with _provider_answering(lambda request: httpx.Response(200, headers=with_length, content=announced)) as http:
        with pytest.raises(SignInError) as refused:
            await fetch_keys(http, provider)
    assert refused.value.code == "unavailable"
    assert announced.read == 0  # its announced length was enough
    # Under the cap, they are read.
    async with _provider_answering(
        lambda request: httpx.Response(200, content=_padded(_document(), 200 * KIB))
    ) as http:
        assert (await discover(http, ISSUER)).issuer == ISSUER
    async with _provider_answering(lambda request: httpx.Response(200, content=_padded(keys, 200 * KIB))) as http:
        assert len(list(await fetch_keys(http, provider))) == 1


@pytest.mark.parametrize(
    "body",
    [b"[" * 100_000, b"not json", b"[1, 2]", b'"text"', b"\xff\xfe"],
    ids=["nested", "text", "list", "string", "not-utf-8"],
)
async def test_a_discovery_document_that_is_not_a_json_object_is_unavailable(body):
    async with _provider_answering(lambda request: httpx.Response(200, content=body)) as http:
        with pytest.raises(SignInError) as refused:
            await discover(http, ISSUER)
    assert refused.value.code == "unavailable"


_PUBLISHED = rsa_key("key-1").jwk()
_SYMMETRIC = {"kty": "oct", "kid": "secret", "k": base64.urlsafe_b64encode(CLIENT_SECRET.encode()).decode()}


@pytest.mark.parametrize(
    "found",
    [
        {"keys": "key-1"},
        {"keys": ["key-1"]},
        {"keys": [{"kty": "RSA"}]},
        {"keys": []},
        {"no": "keys"},
        {"keys": [_PUBLISHED, _SYMMETRIC]},  # a symmetric key published: the whole set is refused
        {"keys": [_PUBLISHED, {**_SYMMETRIC, "use": "enc"}]},
        {"keys": [_PUBLISHED, {**rsa_key("x").jwk(), "kid": "key-1"}]},  # two keys of one id
        {"keys": [{**_PUBLISHED, "kid": None}, {**rsa_key("x").jwk(), "kid": None}]},  # two keys of no id
        {"keys": [{**_PUBLISHED, "kid": 7}]},
        {"keys": [{**_PUBLISHED, "alg": "HS256"}]},  # no algorithm Latet verifies with: no key
        {"keys": [{**_PUBLISHED, "n": "not base64!"}]},
    ],
    ids=lambda found: json.dumps(found)[:40],
)
async def test_a_key_set_that_cannot_be_read_is_unavailable(found):
    provider = Provider(*_document().values(), ("client_secret_basic",))
    async with _provider_answering(lambda request: httpx.Response(200, json=found)) as http:
        with pytest.raises(SignInError) as refused:
            await fetch_keys(http, provider)
    assert refused.value.code == "unavailable"


async def test_a_provider_that_cannot_be_reached_is_logged_by_the_errors_type_and_its_host():
    def unreachable(request):
        raise httpx.ConnectError("refused at https://login.acme.test/realms/acme/private?ticket=7", request=request)

    async with _provider_answering(unreachable) as http:
        with pytest.raises(SignInError) as refused:
            await discover(http, ISSUER)
    assert refused.value.code == "unavailable"
    assert "ConnectError" in refused.value.detail and "login.acme.test" in refused.value.detail
    assert "private" not in refused.value.detail and "ticket" not in refused.value.detail
    assert "ticket" in refused.value.said


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip", "deflate", "GZIP", "identity, gzip"])
async def test_a_compressed_answer_is_not_read_and_none_is_asked_for(encoding):
    """A few compressed bytes can stand for gigabytes: the caps count what is read, so nothing compressed is read."""
    asked = []
    body = gzip.compress(_padded(_document(), 10 * KIB * KIB))  # 10 MB in a few KiB (what the encoding says aside)

    class Compressed(httpx.AsyncBaseTransport):
        """Answers as a server does: the body still a stream (MockTransport would read and decode it itself)."""

        async def handle_async_request(self, request):
            asked.append(request.headers.get("accept-encoding"))
            return httpx.Response(200, headers={"content-encoding": encoding}, stream=httpx.ByteStream(body))

    provider = Provider(*_document().values(), ("client_secret_basic",))
    async with httpx.AsyncClient(transport=Compressed(), trust_env=False) as http:
        for call in (
            lambda: discover(http, ISSUER),
            lambda: fetch_keys(http, provider),
            lambda: exchange(http, provider, CLIENT_ID, CLIENT_SECRET, "code", "v" * 64, REDIRECT),
        ):
            with pytest.raises(SignInError) as refused:
                await call()
            assert refused.value.code == "unavailable" and "compressed" in refused.value.detail
    assert asked == ["identity"] * 3


async def test_an_answer_said_to_be_identity_is_read():
    answer = json.dumps(_document()).encode()
    async with _provider_answering(
        lambda request: httpx.Response(200, headers={"content-encoding": "identity"}, content=answer)
    ) as http:
        assert (await discover(http, ISSUER)).issuer == ISSUER


async def test_a_redirect_from_the_provider_is_not_followed_even_by_a_client_that_follows_them():
    asked = []

    def redirecting(request):
        asked.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:6379/certs"})

    transport = httpx.MockTransport(redirecting)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True, trust_env=False) as http:
        with pytest.raises(SignInError) as refused:
            await discover(http, ISSUER)
    assert refused.value.code == "unavailable"
    assert asked == [f"{ISSUER}/.well-known/openid-configuration"]


def _authorized(provider, verifier_challenge):
    verifier, challenge = verifier_challenge
    query = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": "openid",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "nonce": NONCE,
        "state": "s-1",
        "redirect_uri": REDIRECT,
    }
    back = provider.authorize(f"{provider.endpoints['authorization_endpoint']}?{urlencode(query)}", {"sub": "0f6a-e"})
    return parse_qs(urlsplit(back).query)["code"][0], verifier


@pytest.mark.parametrize(
    ("methods", "posted"),
    [(("client_secret_post", "client_secret_basic"), True), (("client_secret_basic",), False)],
)
async def test_the_code_is_exchanged_for_the_id_token_with_the_verifier(provider, clock, methods, posted):
    provider.methods = methods
    async with provider.client() as http:
        found = await discover(http, ISSUER)
        code, verifier = _authorized(provider, pkce_pair())
        token = await exchange(http, found, CLIENT_ID, CLIENT_SECRET, code, verifier, REDIRECT)
        keys = await fetch_keys(http, found)
    assert _check(token, keys, clock)["sub"] == "0f6a-e"
    sent = provider.token_requests[-1]
    assert ("client_secret" in sent) is posted and sent["authorization"].startswith("Basic ") is not posted
    assert sent["code_verifier"] == verifier


async def test_a_code_the_provider_refuses_is_a_failed_sign_in_said_by_its_error_code(provider):
    async with provider.client() as http:
        found = await discover(http, ISSUER)
        code, _ = _authorized(provider, pkce_pair())
        with pytest.raises(SignInError) as refused:
            await exchange(http, found, CLIENT_ID, CLIENT_SECRET, code, pkce_pair()[0], REDIRECT)
    assert refused.value.code == "failed" and "invalid_grant" in refused.value.detail


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (
            {"error": "invalid_grant", "error_description": "Code not valid\n2027-03-01 CRITICAL forged"},
            "invalid_grant",
        ),
        ({"error": "evil\n2027-03-01 CRITICAL forged"}, "other"),
    ],
)
async def test_the_token_endpoints_own_words_never_reach_the_log(answer, said):
    provider = Provider(*_document().values(), ("client_secret_basic",))
    async with _provider_answering(lambda request: httpx.Response(400, json=answer)) as http:
        with pytest.raises(SignInError) as refused:
            await exchange(http, provider, CLIENT_ID, CLIENT_SECRET, "code", "v" * 64, REDIRECT)
    assert refused.value.code == "failed"
    assert said in refused.value.detail and "forged" not in refused.value.detail
    assert "forged" in refused.value.said


async def test_a_token_answer_past_64_kib_is_not_read():
    provider = Provider(*_document().values(), ("client_secret_basic",))
    answer = {"id_token": "a.b.c", "token_type": "Bearer"}
    async with _provider_answering(lambda request: httpx.Response(200, content=_padded(answer, 80 * KIB))) as http:
        with pytest.raises(SignInError) as refused:
            await exchange(http, provider, CLIENT_ID, CLIENT_SECRET, "code", "v" * 64, REDIRECT)
    assert refused.value.code == "unavailable"
    async with _provider_answering(lambda request: httpx.Response(200, content=_padded(answer, 60 * KIB))) as http:
        assert await exchange(http, provider, CLIENT_ID, CLIENT_SECRET, "code", "v" * 64, REDIRECT) == "a.b.c"


def test_pkce_s256_is_the_verifier_hashed():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert pkce_pair()[0] != verifier


@pytest.mark.parametrize(
    ("claims", "name", "groups"),
    [
        ({"groups": ["latet-users", 7]}, "groups", ["latet-users"]),
        ({"groups": "latet-users"}, "groups", ["latet-users"]),
        ({"realm_access": {"roles": ["latet-admins"]}}, "realm_access.roles", ["latet-admins"]),
        ({"https://acme.com/groups": ["payments"]}, "https://acme.com/groups", ["payments"]),
        ({"_claim_names": {"groups": "src1"}}, "groups", []),  # Entra ID past 200 groups: none in the token
    ],
)
def test_the_groups_claim_is_read_as_providers_write_it(claims, name, groups):
    assert groups_in(claims, name) == groups


def test_a_group_is_compared_case_aside_and_without_keycloaks_leading_slash():
    assert member_of(["/Latet-Users"], "latet-users")
    assert not member_of(["latet-users-old"], "latet-users")


def test_an_e_mail_is_within_the_allowed_domains_by_its_own_domain_in_ascii():
    """Fix round 3: the e-mail a person's own link takes is at an allowed domain, case aside; a character whose lower
    case is an ASCII letter (the Kelvin sign's is k) never stands for that letter."""
    assert within_domains("daniel.okafor@ACME.com", ["acme.com"])
    assert within_domains("daniel.okafor@anywhere.example", [])
    assert not within_domains("daniel.okafor@acme.com.evil.example", ["acme.com"])
    assert not within_domains("acme.com", ["acme.com"])  # no domain at all
    assert not within_domains("daniel@\N{KELVIN SIGN}iwi.com", ["kiwi.com"])
    assert within_domains("daniel@Kiwi.com", ["kiwi.com"])
