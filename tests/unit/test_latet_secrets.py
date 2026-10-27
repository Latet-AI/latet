"""Latet credentials and identity addresses stay private through the shared masking engine."""

import base64
import json
import random
import re

import pytest

from latet.config import Config, Profile
from latet.engine.build import build, reserved_types
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import KnownValuesDetector
from latet.engine.modules import module_of
from latet.engine.rehydrate import restore_text
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import MaskingError, Vault
from latet.gateway import outbound

from ..conftest import ROOT_KEY

TAIL = "Q7wZ2k4NBX5pL3mD_r8S-v6Hj9Ta0Bc1Eo2Fi3Gu4J5"
KINDS = (  # a kind's prefix, its type, and the URL-safe characters after the prefix
    ("pat", "LATET_PERSONAL_TOKEN", 43),
    ("key", "LATET_API_KEY", 43),
    ("adm", "LATET_ADMIN_TOKEN", 43),
    ("ck", "LATET_COMPUTER_KEY", 43),
    ("scim", "LATET_SCIM_TOKEN", 43),  # an Enterprise server's SCIM bearer token: the Community engine masks it as well
    ("brg", "LATET_BRIDGE_TOKEN", 43),  # the token the Claude Code plugin presents to the daemon on this computer
    ("code", "LATET_ONE_TIME_CODE", 32),  # the code an administrator gives a person to connect a computer
)
TOKENS = [(f"latet_{prefix}_" + TAIL[:length], etype) for prefix, etype, length in KINDS]
ROUTE = "Em1ly_Cart3r-Rout3K3y_7q9"
URL = "https://latet.acme.lan:8443/u/" + ROUTE


def _built(profile=Profile.BALANCED):
    return build(Config(profile=profile, ner="off"), Vault(ROOT_KEY))


@pytest.mark.parametrize(("token", "etype"), TOKENS)
@pytest.mark.parametrize("profile", list(Profile))
def test_each_latet_credential_has_its_own_secret_type_and_fake(token, etype, profile):
    built = _built(profile)
    for text in (token, f'API_KEY="{token}"', f"Authorization: Bearer {token}"):
        masked = built.masker.mask(text, SlotKind.TOOL_RESULT)
        [replacement] = masked.replacements
        assert replacement.original == token and replacement.etype == etype
        assert replacement.eclass == EntityClass.SECRET
        public = token[: token.index("_", len("latet_")) + 1]  # latet_pat_, latet_ck_: the prefix alone is kept
        assert token not in masked.text and replacement.replacement.startswith(public)
        assert len(replacement.replacement) == len(token)
        assert re.fullmatch(
            re.escape(public) + rf"[A-Za-z0-9_-]{{{len(token) - len(public)}}}", replacement.replacement
        )
    assert etype in reserved_types()


@pytest.mark.parametrize(("token", "etype"), TOKENS)
def test_latet_tokens_require_the_complete_prefix_length_and_boundaries(token, etype):
    built = _built()
    for text in (token[:-1], token + "a", "x" + token, "_" + token, "-" + token, token.upper()):
        assert not any(s.etype == etype for s in built.masker.detect(text, DetectContext()))
    text = f"({token}), '{token}'"
    assert [text[s.start : s.end] for s in built.masker.detect(text, DetectContext()) if s.etype == etype] == [
        token,
        token,
    ]


@pytest.mark.parametrize(("prefix", "length"), [(prefix, length) for prefix, _, length in KINDS])
@pytest.mark.parametrize("character", ["_", "-", "a", "0"])
def test_each_allowed_credential_suffix_can_be_replaced(prefix, length, character):
    token = f"latet_{prefix}_" + character * length
    masked = _built().masker.mask(token)
    [replacement] = masked.replacements
    assert replacement.original == token and token not in masked.text
    assert re.fullmatch(rf"latet_{prefix}_[A-Za-z0-9_-]{{{length}}}", replacement.replacement)


def test_a_computer_keys_fake_keeps_its_prefix_and_draws_every_character_of_its_secret():
    """``latet_ck_`` is one character shorter than the other prefixes: none of its secret stays in its fake."""
    firsts = "ABCDEFGHIJKL"
    fakes = [_built().masker.mask(f"latet_ck_{c}{TAIL[1:]}").replacements[0].replacement for c in firsts]
    assert all(fake.startswith("latet_ck_") and len(fake) == 52 for fake in fakes)
    assert sum(fake[9] == c for fake, c in zip(fakes, firsts, strict=True)) < len(firsts)  # drawn, never kept


ETYPES = {prefix: etype for prefix, etype, _ in KINDS}
# The credentials no name points at (a Latet secret is told by its prefix): the prefix, and the bytes that
# ``secrets.token_urlsafe`` is given for its body (43 characters for 32 bytes, 32 for 24).
ISSUED = {"scim": ("latet_scim_", 32), "brg": ("latet_brg_", 32), "code": ("latet_code_", 24)}


def _issued(kind: str, n: int) -> list[str]:
    """Credentials as Latet makes them (a prefix and ``secrets.token_urlsafe(size)``), drawn from a fixed seed so that
    a failure repeats."""
    prefix, size = ISSUED[kind]
    rng = random.Random(f"2026 {kind}")
    return [prefix + base64.urlsafe_b64encode(rng.randbytes(size)).rstrip(b"=").decode() for _ in range(n)]


CONTEXTS = {
    "alone": "TOKEN",
    "in prose": "Give the identity provider TOKEN and the base URL below.",
    "in a markdown code span": "Paste `TOKEN` into the provider's secret token field.",
    "in a json value no secret's name points at": '{"value": "TOKEN"}',
    "in a yaml value under a plain key": "provisioning:\n  note: TOKEN\n",
    "after a url": "https://latet.acme.lan/scim/v2 TOKEN",
    "in the output of the command that makes it": "Shown once: TOKEN\nBase URL: https://latet.acme.lan/scim/v2\n",
    "in a command that uses it": "echo TOKEN | latet connect https://latet.acme.lan --code -",
    "in a curl command": "curl -s -H 'Authorization: Bearer TOKEN' https://latet.acme.lan/scim/v2/Users",
}


@pytest.mark.parametrize("profile", list(Profile))
@pytest.mark.parametrize("slot", list(SlotKind))
@pytest.mark.parametrize("context", list(CONTEXTS))
@pytest.mark.parametrize("kind", list(ISSUED))
def test_a_latet_credential_with_no_name_pointing_at_it_is_masked_wherever_an_agent_sends_it(
    kind, context, slot, profile
):
    token = _issued(kind, 1)[0]
    public = ISSUED[kind][0]
    text = CONTEXTS[context].replace("TOKEN", token)
    built = _built(profile)
    masked = built.masker.mask(text, slot)
    [replacement] = [r for r in masked.replacements if r.original == token]
    assert replacement.etype == ETYPES[kind] and replacement.eclass == EntityClass.SECRET
    assert token not in masked.text and token[len(public) :] not in masked.text
    assert replacement.replacement.startswith(public) and len(replacement.replacement) == len(token)
    assert re.fullmatch(re.escape(public) + r"[A-Za-z0-9_-]+", replacement.replacement)
    assert masked.text == text.replace(token, replacement.replacement)  # nothing else around it changes
    assert built.verifier.verify([(masked.text, slot, False)], "credential-session").leaks == []


def _all_masked_and_restored(tokens: list[str], etype: str) -> None:
    built = _built()
    session = built.engine.session("credentials")
    text = "\n".join(f"NOTE_{i}: {token}" for i, token in enumerate(tokens))
    masked = built.engine.mask_text(text, SlotKind.TOOL_RESULT, session, tool="Read")
    assert not any(token in masked.text for token in tokens)
    assert sorted(r.original for r in masked.replacements) == sorted(tokens)
    assert {r.etype for r in masked.replacements} == {etype}
    assert restore_text(masked.text, built.engine.restores(session, False))[0] == text
    assert built.engine.mask_text(text, SlotKind.TOOL_RESULT, session, tool="Read").text == masked.text


@pytest.mark.parametrize("kind", list(ISSUED))
def test_every_credential_of_these_kinds_is_masked_and_restored(kind):
    tokens = _issued(kind, 300)  # some begin with `-` or `_`, which are URL-safe characters too
    assert any(t[len(ISSUED[kind][0])] in "-_" for t in tokens)
    _all_masked_and_restored(tokens, ETYPES[kind])


def test_what_latet_itself_generates_of_these_kinds_is_masked_and_restored():
    from latet.server.tokens import new_code
    from latet.storage.keys import new_bridge_token

    for make, etype in ((new_code, "LATET_ONE_TIME_CODE"), (new_bridge_token, "LATET_BRIDGE_TOKEN")):
        _all_masked_and_restored([make() for _ in range(300)], etype)


@pytest.mark.parametrize("kind", list(ISSUED))
def test_the_fake_of_these_credentials_keeps_the_prefix_and_draws_every_character_of_the_secret(kind):
    public = ISSUED[kind][0]
    tokens = _issued(kind, 40)
    fakes = [_built().masker.mask(token).replacements[0].replacement for token in tokens]
    assert all(fake.startswith(public) and len(fake) == len(token) for fake, token in zip(fakes, tokens, strict=True))
    assert all(fake != token for fake, token in zip(fakes, tokens, strict=True))
    n = len(public)
    assert sum(fake[n] == token[n] for fake, token in zip(fakes, tokens, strict=True)) < 8  # drawn, never kept


@pytest.mark.parametrize("kind", list(ISSUED))
def test_these_credentials_are_known_again_by_their_fake_for_the_outbound_check_and_the_policies(kind):
    token, etype = _issued(kind, 1)[0], ETYPES[kind]
    built = _built()
    session = built.engine.session("outbound")
    masked = built.engine.mask_text(f"token: {token}", SlotKind.TOOL_RESULT, session)
    [replacement] = masked.replacements
    assert outbound.stand_ins(built.masker, [replacement.replacement]) == [etype]
    reason = outbound.reason("WebFetch", [etype])
    assert token not in reason and replacement.replacement not in reason
    assert etype in reserved_types()


@pytest.mark.parametrize(
    ("kind", "length"),
    [
        (prefix, n)
        for prefix, _, size in KINDS
        if prefix in ISSUED
        for n in sorted({0, 1, 19, size - 1, size + 1, 43, 60} - {size})
    ],
)
def test_a_prefix_with_a_secret_of_another_length_is_not_that_credential(kind, length):
    text = ISSUED[kind][0] + ("Q7wZ2k4NBX5pL3mD" * 5)[:length]
    assert not any(s.etype == ETYPES[kind] for s in _built().masker.detect(text, DetectContext()))


@pytest.mark.parametrize(
    "word",
    [
        "latet_code_review",
        "latet_code_review_comments_for_the_dashboard_and_more",  # longer than a code
        "latet_code_" + "Ab3-" * 10,
        "latet_codec_" + "Ab3-" * 8,
        "latet_brg_notes",
        "latet_brg_" + "Ab3-" * 11,  # longer than a bridge token
    ],
)
def test_a_word_that_only_begins_like_a_code_or_a_bridge_token_is_no_credential(word):
    for text in (word, f"def {word}(): pass", f"/home/ada/src/{word}/main.py", f"{word}."):
        assert not any(
            s.etype in ("LATET_ONE_TIME_CODE", "LATET_BRIDGE_TOKEN")
            for s in _built().masker.detect(text, DetectContext())
        )


@pytest.mark.parametrize("kind", list(ISSUED))
def test_these_credentials_can_be_let_through_or_redacted_by_policy(kind):
    token, etype = _issued(kind, 1)[0], ETYPES[kind]
    assert build(Config(ner="off", policies={etype: "allow"}), Vault(ROOT_KEY)).masker.mask(token).text == token
    redacted = build(Config(ner="off", policies={etype: "redact"}), Vault(ROOT_KEY))
    assert redacted.masker.mask(token).text == f"[REDACTED:{etype}]" and redacted.vault.count == 0


@pytest.mark.parametrize("route", ["_" * 16, "-" * 128])
def test_each_allowed_route_key_can_be_replaced_even_when_it_has_only_signs(route):
    built = _built()
    session = built.engine.session("sign-only-route")
    url = "https://latet.acme.lan/u/" + route
    masked = built.engine.mask_text(url, SlotKind.TOOL_RESULT, session)
    [replacement] = masked.replacements
    assert replacement.original == route and route not in masked.text
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,128}", replacement.replacement)
    assert restore_text(masked.text, built.engine.restores(session, False))[0] == url
    assert outbound.stand_ins(built.masker, [masked.text]) == ["LATET_ROUTE_KEY"]
    assert built.engine.mask_text(url, SlotKind.TOOL_RESULT, session).text == masked.text


@pytest.mark.parametrize("route", ["_" * 16, "-" * 128])
def test_a_sign_only_route_key_stays_known_outside_its_address(route):
    built = _built()
    built.masker.mask("https://latet.acme.lan/u/" + route)
    text = "route=" + route
    masked = built.masker.mask(text, SlotKind.TOOL_RESULT)
    assert route not in masked.text
    assert [r.etype for r in masked.replacements] == ["LATET_ROUTE_KEY"]
    assert "LATET_ROUTE_KEY" in {
        leak.etype for leak in built.verifier.verify([(text, SlotKind.TOOL_RESULT, False)], "new-turn").leaks
    }


@pytest.mark.parametrize("etype", ["PASSWORD", "ASSIGNED_SECRET", "CUSTOM"])
def test_the_known_value_sign_filter_still_applies_to_other_types(etype):
    value = "_" * 16
    detector = KnownValuesDetector([(value, etype, EntityClass.SECRET)])
    assert list(detector.detect("value=" + value, DetectContext())) == []


@pytest.mark.parametrize("length", [16, 24, 128])
@pytest.mark.parametrize("host", ["latet.acme.lan", "127.0.0.1:5283", "[::1]:5283"])
def test_a_latet_address_masks_only_its_route_segment(length, host):
    route = ("Q7_-wZ2k4NBX5pL3mD" * 8)[:length]
    url = f"https://{host}/u/{route}/_latet/engine/v1/mask?mode=all#part"
    built = _built(Profile.SECRETS)
    masked = built.masker.mask(url, SlotKind.TOOL_RESULT)
    [replacement] = masked.replacements
    assert replacement.etype == "LATET_ROUTE_KEY" and replacement.original == route
    assert replacement.eclass == EntityClass.SECRET
    assert route not in masked.text
    assert masked.text == url.replace(route, replacement.replacement)
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,128}", replacement.replacement)
    assert "LATET_ROUTE_KEY" in reserved_types()


@pytest.mark.parametrize(
    "url",
    [
        "http://latet.acme.lan/u/" + ROUTE,
        "https://latet.acme.lan/u/" + "Q" * 15,
        "https://latet.acme.lan/u/" + "Q" * 129,
        "https://latet.acme.lan/users/" + ROUTE,
        "https://latet.acme.lan/other/u/" + ROUTE,
        "https://latet.acme.lan/u/" + ROUTE + "%41",
        "https://latet.acme.lan/u/" + ROUTE + ".suffix",
        "xhttps://latet.acme.lan/u/" + ROUTE,
    ],
)
def test_an_address_must_have_the_https_identity_route_shape(url):
    assert not any(s.etype == "LATET_ROUTE_KEY" for s in _built().masker.detect(url, DetectContext()))


@pytest.mark.parametrize("ending", ["", ".", "?mode=all", "#part", "/anthropic"])
def test_an_identity_address_in_prose_keeps_its_delimiters(ending):
    masked = _built().masker.mask(f"Open ({URL}{ending})")
    [replacement] = masked.replacements
    assert replacement.original == ROUTE
    assert masked.text == f"Open ({URL.replace(ROUTE, replacement.replacement)}{ending})"


@pytest.mark.parametrize(("value", "etype"), [*TOKENS, (URL, "LATET_ROUTE_KEY")])
def test_latet_secrets_keep_the_existing_policy_and_allow_list_contracts(value, etype):
    allowed = build(Config(ner="off", policies={etype: "allow"}), Vault(ROOT_KEY))
    assert allowed.masker.mask(value).text == value
    redacted = build(Config(ner="off", policies={etype: "redact"}), Vault(ROOT_KEY))
    assert f"[REDACTED:{etype}]" in redacted.masker.mask(value).text and redacted.vault.count == 0
    original = ROUTE if etype == "LATET_ROUTE_KEY" else value
    listed = build(Config(ner="off", allow_values=[original]), Vault(ROOT_KEY))
    assert listed.masker.mask(value).text == value


def test_a_latet_secret_detector_failure_stops_masking_without_quoting_the_value(monkeypatch):
    built = _built()
    detector = next(d for d in built.masker.detectors if d.id == "latet-credentials")

    def failed(text, ctx):
        raise ValueError(text)

    monkeypatch.setattr(detector, "detect", failed)
    with pytest.raises(MaskingError, match=r"^detector latet-credentials failed$"):
        built.masker.mask(URL)


def test_a_route_key_fake_randomizes_every_secret_segment():
    route = "emily-private_" + TAIL
    masked = _built().masker.mask("https://latet.acme.lan/u/" + route)
    [replacement] = masked.replacements
    assert "emily" not in replacement.replacement and "private" not in replacement.replacement
    assert module_of(replacement.rule, replacement.etype) == "secrets"


@pytest.mark.parametrize("format", ["environment", "claude", "codex"])
def test_connected_configuration_dumps_mask_restore_and_guard_all_latet_secrets(format):
    pat, key, admin, computer, scim, bridge, code = (token for token, _ in TOKENS)
    env = {
        "ANTHROPIC_BASE_URL": URL + "/anthropic",
        "LATET_BRIDGE_URL": URL,
        "LATET_BRIDGE_TOKEN": pat,
        "LATET_SERVER_ADMIN_TOKEN": admin,
        "BILLING_API_KEY": key,
        "COPIED_BY_MISTAKE": computer,  # a computer's key, which no agent is given: masked all the same if it leaks
        "IDP_NOTES": f"paste {scim} into the provider",  # a SCIM token, in a value no secret's name points at
        "HANDOVER": f"the daemon's token is {bridge}; give them {code} once",  # two more, as a person writes them
    }
    if format == "environment":
        text = "\n".join(f"{name}={value}" for name, value in env.items())
    elif format == "claude":
        text = json.dumps({"env": env, "permissions": {"allow": ["Bash(git status)"]}}, indent=2)
    else:
        text = (
            f'model_provider = "latet"\n[model_providers.latet]\nbase_url = "{URL}/openai/v1"\n'
            f'http_headers = {{ "Authorization" = "Bearer {pat}" }}\n'
            f'[env]\nBILLING_API_KEY = "{key}"\nLATET_SERVER_ADMIN_TOKEN = "{admin}"\n'
            f'COPIED_BY_MISTAKE = "{computer}"\nIDP_NOTES = "paste {scim} into the provider"\n'
            f'HANDOVER = "the daemon\'s token is {bridge}; give them {code} once"\n'
        )
    built = _built()
    session = built.engine.session("latet-secrets")
    masked = built.engine.mask_text(text, SlotKind.TOOL_RESULT, session, tool="Read")
    assert all(value not in masked.text for value in (pat, key, admin, computer, scim, bridge, code, ROUTE))
    assert "https://latet.acme.lan:8443/u/" in masked.text
    assert {r.etype for r in masked.replacements} == {*(etype for _, etype in TOKENS), "LATET_ROUTE_KEY"}
    assert built.verifier.verify([(masked.text, SlotKind.TOOL_RESULT, False)], session.key).leaks == []
    assert restore_text(masked.text, built.engine.restores(session, False))[0] == text
    for original, etype in [*TOKENS, (ROUTE, "LATET_ROUTE_KEY")]:
        entity = built.vault.by_original(original)
        assert entity is not None
        value = URL.replace(ROUTE, entity.surrogate) if original == ROUTE else entity.surrogate
        assert outbound.stand_ins(built.masker, [value]) == [etype]
        reason = outbound.reason("WebFetch", [etype])
        assert etype in reason and original not in reason and entity.surrogate not in reason
    unseen = built.engine.session("another-conversation")
    assert restore_text(masked.text, built.engine.restores(unseen, False))[0] == masked.text
