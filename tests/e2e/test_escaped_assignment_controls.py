import pytest

from latet.config import Config
from latet.engine.build import build
from latet.engine.types import SlotKind
from latet.engine.vault import Vault


@pytest.mark.parametrize("value", ["Synthet1c-Real-Secret-939", "aB7", "xy\\\\"])
def test_unmasked_real_credentials_still_block(value):
    built = build(Config(ner="off"), Vault(b"9" * 32))
    text = 'password="' + value + '"'
    result = built.verifier.verify([(text, SlotKind.TOOL_RESULT, False)], "negative-control")
    assert result.blocking


@pytest.mark.parametrize("slashes", [1, 2, 4])
def test_real_credential_before_serialized_quote_masks_value(slashes):
    built = build(Config(ner="off"), Vault(b"7" * 32))
    value = "Synthet1c-Real-Secret-939"
    text = "log token=" + value + "\\" * slashes + '" end'
    masked = built.masker.mask(text, SlotKind.TOOL_RESULT)
    assert value not in masked.text
    assert not built.verifier.verify(
        [(masked.text, SlotKind.TOOL_RESULT, False)], "masked-control", rewritten=True
    ).blocking
