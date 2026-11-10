import pytest

from latet.config import Config
from latet.engine.build import build
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault


def test_surrogate_with_documentation_ellipsis_is_not_new_bearer_secret():
    vault = Vault(b"5" * 32)
    entity = vault.intern("HOSTNAME", EntityClass.IDENTITY, "sample", rule="identity:hostname")
    built = build(Config(ner="off"), vault)
    text = "Authorization: Bearer " + entity.surrogate + "..."
    result = built.verifier.verify([(text, SlotKind.TOOL_RESULT, False)], "synthetic", rewritten=True)
    assert not result.blocking


@pytest.mark.parametrize("etype", ["BEARER_TOKEN", "ASSIGNED_SECRET", "PASSWORD"])
@pytest.mark.parametrize("prefix", ["Authorization: *** ", "Documentation: "])
@pytest.mark.parametrize("cached_clean", [False, True])
def test_known_secret_fake_with_ellipsis_still_blocks(etype, prefix, cached_clean):
    vault = Vault(b"5" * 32)
    entity = vault.intern("HOSTNAME", EntityClass.IDENTITY, "sample", rule="identity:hostname")
    built = build(Config(ner="off"), vault)
    value = entity.surrogate + "..."
    text = prefix + value
    slots = [(text, SlotKind.TOOL_RESULT, False)]
    if cached_clean:
        assert not built.verifier.verify(slots, "synthetic", rewritten=True, record=False).blocking
    vault.intern(etype, EntityClass.SECRET, value, rule="synthetic:known-secret")

    result = built.verifier.verify(slots, "synthetic", rewritten=True)

    assert result.blocking
    assert any(leak.rule == "known-values" and text[leak.start : leak.end] == value for leak in result.leaks)


@pytest.mark.parametrize("suffix", [".", "..", "....", "ActualSecret123", ".ActualSecret123", "...ActualSecret123"])
def test_surrogate_prefix_does_not_hide_extra_credential_content(suffix):
    vault = Vault(b"5" * 32)
    entity = vault.intern("HOSTNAME", EntityClass.IDENTITY, "sample", rule="identity:hostname")
    built = build(Config(ner="off"), vault)
    text = "Authorization: Bearer " + entity.surrogate + suffix
    assert built.verifier.verify([(text, SlotKind.TOOL_RESULT, False)], "synthetic", rewritten=True).blocking
