"""``Masker.planned``: what ``mask`` replaces in a text and by which policy, with nothing kept: the vault learns no value,
so no later text is read for the values or the fakes of the texts before it (``latet leak``'s scan)."""

from __future__ import annotations

import base64

import pytest

from latet.config import Config, TermConfig
from latet.engine.build import build_masker
from latet.engine.detectors.terms import VALUE_LONGEST
from latet.engine.types import Policy, SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

GITHUB = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "jane.doe" + "@example-corp.it"
PASSWORD = "Zq7#Kx9!" + "Lm2vBn4Jt"


def _pem() -> str:
    encoded = base64.b64encode(bytes(range(256)) * 50).decode()
    body = "".join(encoded[at : at + 64] + "\n" for at in range(0, len(encoded), 64))
    pem = f"-----BEGIN RSA PRIVATE KEY-----\n{body}-----END RSA PRIVATE KEY-----"
    assert len(pem) > VALUE_LONGEST
    return pem


def _locked() -> Config:
    longer = [TermConfig(value="Project Falcon Phase", type="PUBLIC")]
    config = Config(ner="off", terms=longer, policies={"PROJECT": Policy.REDACT})
    config.lock_terms([TermConfig(value="Project Falcon", type="PROJECT")])
    return config


CASES = [
    (Config(ner="off"), f"export GITHUB_TOKEN={GITHUB} and mail {EMAIL}, password: {PASSWORD}"),
    (Config(ner="off", policies={"GITHUB_PAT": Policy.REDACT}), f"the token {GITHUB} again"),
    (Config(ner="off", policies={"EMAIL": Policy.ALLOW}), f"mail {EMAIL} with {GITHUB}"),
    (Config(ner="off"), f"key:\n{_pem()}\n"),
    (_locked(), "see Project Falcon Phase 2"),
]


@pytest.mark.parametrize(("config", "text"), CASES, ids=["masked", "redacted", "let-through", "too-long", "locked"])
def test_the_plan_is_what_mask_replaces_by_the_same_policies(config: Config, text: str) -> None:
    masked = build_masker(config, Vault(ROOT_KEY))[0].mask(text, SlotKind.TOOL_RESULT)
    vault = Vault(ROOT_KEY)
    planned = build_masker(config, vault)[0].planned(text, SlotKind.TOOL_RESULT)
    assert masked.replacements
    assert [(s.start, s.end, s.etype, s.eclass, policy) for s, policy in planned] == [
        (r.start, r.end, r.etype, r.eclass, r.policy) for r in masked.replacements
    ]
    assert vault.version == 0 and vault.count == 0


def test_a_plan_teaches_the_vault_nothing_for_the_texts_after() -> None:
    """A value ``mask`` keeps is found again, bare, in a later text; one ``planned`` found is not: each text is read on
    its own."""
    first, later = f"password: {PASSWORD}", f"it was {PASSWORD}"
    masker = build_masker(Config(ner="off"), Vault(ROOT_KEY))[0]
    assert [p for _, p in masker.planned(first)] == [Policy.MASK]
    assert masker.planned(later) == []
    masker.mask(first)
    assert [s.etype for s, _ in masker.planned(later)] == ["PASSWORD"]


def test_an_empty_text_has_no_plan() -> None:
    assert build_masker(Config(ner="off"), Vault(ROOT_KEY))[0].planned("") == []
