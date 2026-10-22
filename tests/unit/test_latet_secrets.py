"""Latet credentials and identity addresses stay private through the shared masking engine."""

import json
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
KINDS = (
    ("pat", "LATET_PERSONAL_TOKEN"),
    ("key", "LATET_API_KEY"),
    ("adm", "LATET_ADMIN_TOKEN"),
    ("ck", "LATET_COMPUTER_KEY"),
)
TOKENS = [(f"latet_{prefix}_" + TAIL, etype) for prefix, etype in KINDS]
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
        assert re.fullmatch(r"latet_(?:pat|key|adm|ck)_[A-Za-z0-9_-]{43}", replacement.replacement)
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


@pytest.mark.parametrize("prefix", ["pat", "key", "adm", "ck"])
@pytest.mark.parametrize("character", ["_", "-", "a", "0"])
def test_each_allowed_credential_suffix_can_be_replaced(prefix, character):
    token = f"latet_{prefix}_" + character * 43
    masked = _built().masker.mask(token)
    [replacement] = masked.replacements
    assert replacement.original == token and token not in masked.text
    assert re.fullmatch(r"latet_" + prefix + r"_[A-Za-z0-9_-]{43}", replacement.replacement)


def test_a_computer_keys_fake_keeps_its_prefix_and_draws_every_character_of_its_secret():
    """``latet_ck_`` is one character shorter than the other prefixes: none of its secret stays in its fake."""
    firsts = "ABCDEFGHIJKL"
    fakes = [_built().masker.mask(f"latet_ck_{c}{TAIL[1:]}").replacements[0].replacement for c in firsts]
    assert all(fake.startswith("latet_ck_") and len(fake) == 52 for fake in fakes)
    assert sum(fake[9] == c for fake, c in zip(fakes, firsts, strict=True)) < len(firsts)  # drawn, never kept


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
    pat, key, admin, computer = (token for token, _ in TOKENS)
    env = {
        "ANTHROPIC_BASE_URL": URL + "/anthropic",
        "LATET_BRIDGE_URL": URL,
        "LATET_BRIDGE_TOKEN": pat,
        "LATET_SERVER_ADMIN_TOKEN": admin,
        "BILLING_API_KEY": key,
        "COPIED_BY_MISTAKE": computer,  # a computer's key, which no agent is given: masked all the same if it leaks
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
            f'COPIED_BY_MISTAKE = "{computer}"\n'
        )
    built = _built()
    session = built.engine.session("latet-secrets")
    masked = built.engine.mask_text(text, SlotKind.TOOL_RESULT, session, tool="Read")
    assert all(value not in masked.text for value in (pat, key, admin, computer, ROUTE))
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
