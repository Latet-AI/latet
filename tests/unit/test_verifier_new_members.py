"""A request member Latet has no rule for is checked as masking reads a user's text (``Verifier.verify``'s
``new_members``): a name or a phone number in it blocks, a token of control does not. Its values as their fields
(``new_member_fields``) are read by your field rules as masking reads a JSON document's, by their key or their path. The
local name model reads a request's new members at once, as masking reads a document, in the time it has: past it,
nothing of the request is checked or recorded (``NewMembersUnread``)."""

import time

import pytest

from latet.config import Config, FieldRule
from latet.engine.build import build_masker
from latet.engine.detectors.ner import NerDetector, PastDeadline
from latet.engine.masker import Masker
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault
from latet.engine.verifier import NEW_MEMBERS_READ_SECONDS, NewMembersUnread, Verifier
from latet.gateway import mediate, new_members
from latet.storage.exchanges import safe_leaks

from ..conftest import ROOT_KEY
from .test_ner import PERSON, Spans

IBAN_TEXT = "IT60X0542811101000000123456"


def _verifier(**config):
    vault = Vault(ROOT_KEY)
    return Verifier(build_masker(Config(ner="off", **config), vault)[0]), vault


def test_a_new_member_holding_a_name_blocks():
    vault = Vault(ROOT_KEY)
    verifier = Verifier(Masker(vault, [NerDetector(Spans(("Mario Rossi", PERSON)))]))
    result = verifier.verify([], "s", new_members=["call Mario Rossi tomorrow"])
    assert [(leak.etype, leak.kind) for leak in result.blocking] == [("PERSON", SlotKind.NEW_MEMBER)]


def test_a_new_member_holding_a_bare_phone_blocks():
    verifier, _ = _verifier()
    assert verifier.verify([], "s", new_members=["3471234567"]).blocking


def test_a_new_member_holding_a_struct_value_blocks():
    # A structure's value (strict data, a field rule) is class STRUCT, which checked-only texts never stop.
    verifier, _ = _verifier(fields=[FieldRule(name="tier", type="TIER")])
    assert verifier.verify([], "s", checked_only=['{"tier": "platinumzx"}']).blocking == []
    result = verifier.verify([], "s", new_members=['{"tier": "platinumzx"}'])
    assert [(leak.etype, leak.eclass) for leak in result.blocking] == [("TIER", EntityClass.STRUCT)]


def test_a_new_member_holding_a_fake_passes():
    verifier, vault = _verifier()
    fake = vault.intern("EMAIL", EntityClass.PII, "anna.verdi@example.org").surrogate
    assert verifier.verify([], "s", new_members=[f"to {fake}"]).blocking == []


def test_a_type_let_through_passes_in_a_new_member():
    verifier, _ = _verifier(policies={"IBAN": "allow"})
    assert verifier.verify([], "s", new_members=[IBAN_TEXT]).blocking == []
    assert _verifier()[0].verify([], "s", new_members=[IBAN_TEXT]).blocking


def test_a_new_member_that_left_passes_again_after_the_vault_learns_it():
    verifier, vault = _verifier()
    text = "ask Zorbak Quillon"
    assert verifier.verify([], "s", new_members=[text], record=True).blocking == []
    vault.intern("PERSON", EntityClass.PII, "Zorbak Quillon")
    again = verifier.verify([], "s", new_members=[text])
    assert again.blocking == [] and "sent before in this conversation" in again.rules
    assert verifier.verify([], "other", new_members=[text]).blocking


def test_control_tokens_pass():
    verifier, _ = _verifier()
    members = ["xhigh", "direct", "clear_thinking_20251015", "true", "3"]
    assert verifier.verify([], "s", new_members=members).blocking == []


def test_the_clean_cache_keys_on_the_kind():
    verifier, _ = _verifier()
    phone = "3471234567"
    assert verifier.verify([], "s", checked_only=[phone]).blocking == []
    assert verifier.verify([], "s", new_members=[phone]).blocking


def test_safe_leaks_keeps_the_new_member_kind():
    assert safe_leaks([{"type": "EMAIL", "kind": "new_member"}])[0]["kind"] == "new_member"


# --- a value as its field ---------------------------------------------------------------------------------------------


def test_a_new_member_s_value_is_read_by_its_field_path():
    """A path no JSON text shows the fields detector (an array's ``[]``): its value read by its field, as masking reads
    a JSON document's."""
    verifier, _ = _verifier(fields=[FieldRule(name="items[].sku", type="SKU")])
    result = verifier.verify([], "s", new_member_fields=[new_members.field("items[].sku", "AB-99812")])
    assert [(leak.etype, leak.eclass, leak.kind) for leak in result.blocking] == [
        ("SKU", EntityClass.STRUCT, SlotKind.NEW_MEMBER)
    ]
    assert verifier.verify([], "s", new_member_fields=[new_members.field("items[].qty", "AB-99812")]).blocking == []


def test_a_value_found_by_its_key_and_its_field_is_one_leak():
    verifier, _ = _verifier(fields=[FieldRule(name="tier", type="TIER")])
    result = verifier.verify([], "s", new_member_fields=[new_members.field("tier", "platinumzx")])
    assert [leak.preview for leak in result.blocking] == ["TIER[10]"]


def test_a_value_several_texts_of_a_request_hold_is_said_once_and_none_of_them_kept_as_clean():
    verifier, _ = _verifier(fields=[FieldRule(name="tier", type="TIER")])
    texts = [new_members.field("tier", "platinumzx"), new_members.field("x_ctx.tier", "platinumzx")]
    assert [leak.preview for leak in verifier.verify([], "s", new_member_fields=texts).blocking] == ["TIER[10]"]
    for text in texts:  # each read again alone, and found again
        assert [leak.preview for leak in verifier.verify([], "s", new_member_fields=[text]).blocking] == ["TIER[10]"]


def test_a_value_as_its_field_is_not_read_by_the_name_model():
    """Its value is a new member's text of its own, which the name model reads: the field is read by every other
    detector. A text that is a value as its field and a member's own value too is read as each, apart."""
    model = Spans(("Mario Rossi", PERSON))
    verifier = Verifier(Masker(Vault(ROOT_KEY), [NerDetector(model)]))
    text = new_members.field("note", "call Mario Rossi")
    assert verifier.verify([], "s", new_member_fields=[text]).blocking == [] and model.calls == 0
    # In another conversation: in this one it left, and passes again as sent before.
    assert [leak.etype for leak in verifier.verify([], "other", new_members=[text]).blocking] == ["PERSON"]


def test_a_value_as_its_field_that_left_passes_again_after_the_vault_learns_it():
    verifier, vault = _verifier()
    text = new_members.field("note", "ask Zorbak Quillon")
    assert verifier.verify([], "s", new_member_fields=[text], record=True).blocking == []
    vault.intern("PERSON", EntityClass.PII, "Zorbak Quillon")
    again = verifier.verify([], "s", new_member_fields=[text])
    assert again.blocking == [] and "sent before in this conversation" in again.rules
    assert verifier.verify([], "other", new_member_fields=[text]).blocking


# --- the name model reads them at once, in the time it has ------------------------------------------------------------


class _Counted:
    """A name model that reads many texts in one run (``predict_many``), as the local one does: it counts its runs and
    the texts it was asked to read alone."""

    def __init__(self, *phrases: tuple[str, str]) -> None:
        self.spans = Spans(*phrases)
        self.runs = 0
        self.alone = 0

    def predict(self, text, max_tokens=None):
        self.alone += 1
        return self.spans.predict(text, max_tokens)

    def predict_many(self, texts, max_tokens, deadline=None):
        if deadline is not None and time.monotonic() >= deadline:
            raise PastDeadline
        self.runs += 1
        return [self.spans.predict(text, max_tokens) for text in texts]


@pytest.mark.timing
def test_many_new_member_texts_are_read_by_the_name_model_at_once():
    """As many distinct texts as the new rule reads of one request: one run of the model, none read alone, well under
    a second; found clean, they are not read again."""
    model = _Counted(("Mario Rossi", PERSON))
    verifier = Verifier(Masker(Vault(ROOT_KEY), [NerDetector(model)]))
    texts = [f"mode number {index}" for index in range(new_members.TEXTS - 1)] + ["call Mario Rossi"]
    started = time.monotonic()
    result = verifier.verify([], "s", new_members=texts, deadline=time.monotonic() + 5)
    assert time.monotonic() - started < 1
    assert (model.runs, model.alone) == (1, 0)
    assert [leak.etype for leak in result.blocking] == ["PERSON"]
    assert verifier.verify([], "s", new_members=texts[:-1]).blocking == []
    assert (model.runs, model.alone) == (1, 0)  # found clean before: not read again


def test_a_text_given_twice_is_read_once():
    model = _Counted()
    verifier = Verifier(Masker(Vault(ROOT_KEY), [NerDetector(model)]))
    result = verifier.verify([], "s", new_members=["compact mode"] * 50)
    assert (model.runs, model.alone, result.checked) == (1, 0, 1)


@pytest.mark.parametrize("batched", [True, False], ids=["many at once", "one at a time"])
def test_new_members_the_name_model_cannot_read_in_time_are_refused_and_recorded_nowhere(batched):
    model = _Counted() if batched else Spans()
    verifier = Verifier(Masker(Vault(ROOT_KEY), [NerDetector(model)]))
    with pytest.raises(NewMembersUnread) as unread:
        verifier.verify([], "s", new_members=["a mode", "another"], deadline=time.monotonic() - 1, record=True)
    assert unread.value.count == 2 and "a mode" not in str(unread.value)
    assert not verifier.sent("s", "a mode")
    # Without the model, or with no text it reads, nothing waits for it.
    assert Verifier(Masker(Vault(ROOT_KEY), [])).verify([], "s", new_members=["a mode"], deadline=0.0).blocking == []
    assert verifier.verify([], "s", new_members=["42"], deadline=0.0).blocking == []


def test_the_time_given_is_masking_s_for_a_document():
    assert NEW_MEMBERS_READ_SECONDS == mediate.DOCUMENT_READ_SECONDS
