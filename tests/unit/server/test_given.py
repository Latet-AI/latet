"""Two engines over one memory of what left (latet.state.Given), as two instances of a server share it: a text a
conversation sent through one leaves the same through the other, whatever the vault learnt since; a block the final
check let leave through one does not stop the conversation on the other. No services: a computer's memory stands in."""

import json
import sys

import pytest

from latet.config import Config, ProjectEntry, TermConfig
from latet.engine.build import build
from latet.engine.masker import ExtraValue, Masker, MaskReport
from latet.engine.types import EntityClass, Policy, Replacement, SlotKind
from latet.engine.vault import Vault
from latet.gateway.mediate import Mediator
from latet.projects import Projects
from latet.runtime import digest
from latet.state import Left, LocalGiven

from ...conftest import ROOT_KEY

TEXT = "ask Emily Carter about the budget"
PERSON = ExtraValue("Emily Carter", "PERSON", EntityClass.CUSTOM, "human:person")  # your mask, in the Live Reviewer
REDACT = {"PERSON": "redact"}  # a person's name leaves as [REDACTED:PERSON], not as a fake
PORTAL = json.dumps({"site": "https://portal.acme-intra.it/home"})
TEACH = "write to ops@acme-intra.it"  # an address: it teaches the vault the domain of the portal


def _instance(vault, given, config=None):
    """An instance's engine, verifier and mediator over the shared vault and memory, with the same rules."""
    config = config or Config()
    built = build(config, vault, None, given=given)
    built.engine.rules_tag = digest(config)
    built.engine.scopes = Projects(config, vault)
    return Mediator(built.engine, built.verifier, given=given), built


def _mask(mediator, session="cc:s1", text=TEXT, **kwargs):
    return mediator.mask_value(text, SlotKind.USER_TEXT, mediator.engine.session(session), MaskReport(), **kwargs)


def _sent(mediator, text, session="cc:s1"):
    """The request that carries ``text`` leaves: the plugin's final check lets it out, and records it."""
    assert mediator.verifier.verify([(text, SlotKind.USER_TEXT, False)], session).blocking == []


def _request(mediator, session, *outputs):
    """One request's tool outputs, as the gateway and the plugin mask a request's texts: each one, then ``settle``."""
    call = mediator.call()
    done = [
        mediator.mask_value(o, SlotKind.TOOL_RESULT, session, MaskReport(), tool="Bash", call=call) for o in outputs
    ]
    mediator.settle(call)
    return [d.text for d in done]


class InFront:
    """An instance's own memory in front of the one the instances share (as a server's keeps one): it answers what
    it read or kept before, whatever another instance kept since."""

    def __init__(self, shared):
        self.shared, self.here = shared, {}

    def masked(self, key):
        if key not in self.here:
            self.here[key] = self.shared.masked(key)
        return self.here[key]

    def keep(self, key, left):
        self.shared.keep(key, left)
        self.here[key] = left

    def checked(self, session, key):
        return self.shared.checked(session, key)

    def keep_checked(self, session, keys):
        self.shared.keep_checked(session, keys)


class Read(LocalGiven):
    """The memory the instances share, and every record ever kept in it: what someone who reads the store has seen."""

    def __init__(self):
        super().__init__()
        self.kept = []

    def keep(self, key, left):
        self.kept.append((key, left))
        super().keep(key, left)


def test_a_text_leaves_as_it_left_on_another_instance_whatever_the_vault_learnt_since():
    given, vault = LocalGiven(), Vault(ROOT_KEY)
    here, _ = _instance(vault, given)
    first = here.mask_value(TEXT, SlotKind.USER_TEXT, here.engine.session("cc:s1"), MaskReport())
    assert first.text == TEXT  # nobody knows the name yet
    _sent(here, first.text)  # it left
    vault.intern("PERSON", EntityClass.CUSTOM, "Emily Carter")  # learnt since (the local reviewer, say)
    there, _ = _instance(vault, given)
    there.engine.rules_changed()  # this process's own count of rules applied: not the rules' name
    again = there.mask_value(TEXT, SlotKind.USER_TEXT, there.engine.session("cc:s1"), MaskReport())
    assert again.text == TEXT  # as it left: the provider's cache still holds it
    fresh = there.mask_value(TEXT, SlotKind.USER_TEXT, there.engine.session("cc:s2"), MaskReport())
    assert "Emily Carter" not in fresh.text  # a new conversation: masked with what the vault knows now


def test_an_api_keys_text_whose_value_was_forgotten_since_is_masked_anew_never_sent_as_it_left():
    """Task 11b (Ruling 11b-R1): an API key's value forgotten by itself since a text left took its entry with it, and
    the key's vault draws in a new generation. The text, sent again in its conversation, is masked anew, with the new
    fake and the new entry, never sent as it left, with the old fake, which restores nothing, nor its old entry shown
    to the conversation again, whether the entry was in memory or not."""
    from ..test_stored_vault import _Store, _vault

    text, value = "write to laura.bianchi@gmail.com", "laura.bianchi@gmail.com"
    for in_memory in (True, False):
        given, store = LocalGiven(), _Store()
        vault = _vault(store)
        here, built = _instance(vault, given)
        first = _mask(here, text=text)
        _sent(here, first.text)
        assert _mask(here, text=text).text == first.text  # as it left, and found so for the vault's version
        old = vault.by_original(value)
        assert old is not None and old.surrogate in first.text
        store.forget_entity(old.id)
        if in_memory:
            vault.forgotten([old.id], 1)  # this instance forgot it
        else:  # another instance did, heard here (``spaces._forget``), the value out of this one's memory: the
            vault._unindex(old)  # vault's version does not move
            store.generation = 1
            vault.forget(old.id)
        built.engine.rules_changed()  # as a forget heard does: its masked texts dropped
        for one in (here, _instance(vault, given)[0]):  # the instance that found it fit, and another
            session = one.engine.session("cc:s1")
            session.entity_ids.discard(old.id)
            again = _mask(one, text=text)
            new = vault.by_original(value)
            assert new is not None and (new.id, new.surrogate) != (old.id, old.surrogate)
            assert old.surrogate not in again.text and new.surrogate in again.text
            assert old.id not in session.entity_ids
            assert _mask(one, text=text).text == again.text  # and as it left from then on


def test_a_persons_text_leaves_as_it_left_after_a_forget():
    """A person's Forget (the dashboard's Vault) is unchanged by Task 11b (review 11b's N2): a text the conversation
    sent before still leaves as it left, the value not kept again by it."""
    given, vault = LocalGiven(), Vault(ROOT_KEY)
    here, _ = _instance(vault, given)
    text, value = "write to laura.bianchi@gmail.com", "laura.bianchi@gmail.com"
    first = _mask(here, text=text)
    _sent(here, first.text)
    old = vault.by_original(value)
    assert old is not None
    vault.forget(old.id)
    there, _ = _instance(vault, given)
    assert _mask(there, text=text).text == first.text and vault.by_original(value) is None


def test_a_block_that_left_through_one_instance_does_not_stop_the_conversation_on_another():
    given, vault = LocalGiven(), Vault(ROOT_KEY)
    _, here = _instance(vault, given)
    slots = [(TEXT, SlotKind.USER_TEXT, False)]
    assert here.verifier.verify(slots, "cc:s1").leaks == []  # clean then: it left
    vault.intern("PERSON", EntityClass.CUSTOM, "Emily Carter")  # learnt since, as a value that blocks
    _, there = _instance(vault, given)
    result = there.verifier.verify(slots, "cc:s1")
    assert result.leaks and not result.blocking  # found now, reported, never blocking: it left already
    assert there.verifier.verify(slots, "cc:s2").blocking  # a conversation it never left through: stopped


def test_a_conversation_in_a_project_with_rules_of_its_own_leaves_as_it_left_on_another_instance():
    """The project's rules have a name of their own the same on every instance, whatever each one counts of the
    settings it applied."""
    project = "/home/emily/billing-api"
    config = Config(projects=[ProjectEntry(path=project, terms=[TermConfig(value="Orion", type="PROJECT")])])
    text = "ask Emily Carter about the Orion contract"
    given, vault = LocalGiven(), Vault(ROOT_KEY)
    here, _ = _instance(vault, given, config)
    session = here.engine.session("cc:s1")
    here.engine.attach(session, project)
    first = _mask(here, text=text)
    assert "Orion" not in first.text and "Emily Carter" in first.text  # the project's term, the name not known yet
    _sent(here, first.text)
    vault.intern("PERSON", EntityClass.CUSTOM, "Emily Carter")
    there, built = _instance(vault, given, config)
    built.engine.scopes.use(config, None)  # the same settings applied once more there: its own count moved on
    there.engine.attach(there.engine.session("cc:s1"), project)
    assert _mask(there, text=text).text == first.text  # as it left


@pytest.mark.parametrize("policies", [{}, REDACT])
def test_a_text_masked_but_never_sent_does_not_leave_once_masked_again_on_another_instance(policies):
    """The text waits for you on one instance (masked with the name in clear: not known yet); you mask the name, and
    another instance sends it masked. The conversation's next request comes back to the first instance, whose own
    memory still holds the text as it was before your decision: it is masked anew, never sent as it never left. The
    same where the name's type is redacted: what you redact is in the vault all the same."""
    config = Config(policies=policies)
    shared, vault = LocalGiven(), Vault(ROOT_KEY)
    here, _ = _instance(vault, InFront(shared), config)
    there, _ = _instance(vault, InFront(shared), config)
    assert _mask(here).text == TEXT  # held for your decision, and kept as masked so far
    decided = _mask(there, extra=[PERSON])
    assert "Emily Carter" not in decided.text
    _sent(there, decided.text)
    assert _mask(here).text == decided.text  # masked anew there: the name stays masked


@pytest.mark.parametrize("policies", [{}, REDACT])
def test_a_record_written_back_as_it_was_before_your_decision_does_not_leave(policies):
    """Someone who reads the store saves the first record of a text, then writes it back once your decision replaced
    it: an instance that reads it sends the text masked as you decided, the one that found the decided text fit to
    leave a moment before too (nothing new in the vault since: what it found is of that text alone)."""
    config = Config(policies=policies)
    shared, vault = Read(), Vault(ROOT_KEY)
    here, _ = _instance(vault, shared, config)
    assert _mask(here).text == TEXT  # held for your decision
    decided = _mask(here, extra=[PERSON])
    _sent(here, decided.text)
    assert _mask(here).text == decided.text  # the next request: as it left
    key, before = shared.kept[0]
    shared.keep(key, before)  # written back by hand
    assert "Emily Carter" not in _mask(here).text
    shared.keep(key, before)  # and again, for an instance that never read it
    fresh, _ = _instance(vault, shared, config)
    assert "Emily Carter" not in _mask(fresh).text


@pytest.mark.parametrize(
    ("found", "blocks"), [(PERSON, True), (ExtraValue("Emily Carter", "PERSON", EntityClass.LLM, "llm:person"), False)]
)
def test_a_value_you_or_the_reviewer_redact_is_redacted_in_a_later_text_too(found, blocks):
    """A value you mask in the Live Reviewer, or the local reviewer finds, under a type whose policy is ``redact``:
    ``[REDACTED:…]`` in what leaves, and the value in the vault all the same (with a fake of its own, never sent), so
    that a later text that holds it is redacted too and the final check knows it (yours blocks; the reviewer's, best
    effort, never does)."""
    here, built = _instance(Vault(ROOT_KEY), LocalGiven(), Config(policies=REDACT))
    assert _mask(here, extra=[found]).text == "ask [REDACTED:PERSON] about the budget"
    assert built.vault.by_original("Emily Carter") is not None
    assert _mask(here, "cc:s2", "and call Emily Carter about it").text == "and call [REDACTED:PERSON] about it"
    assert bool(here.verifier.verify([("Emily Carter called", SlotKind.USER_TEXT, False)], "cc:s3").blocking) is blocks


def test_a_value_you_or_the_reviewer_redact_gets_the_fake_its_type_makes():
    """The fake never leaves; it is made as any value of its type is (a recognizer of yours may make them), so that one
    value has one fake whatever the type's policy is later."""
    vault = Vault(ROOT_KEY)
    masker = Masker(vault, [], policies={"ORG": Policy.REDACT}, generators={"ORG": lambda original, rng: "Vega-7"})
    finding = ExtraValue("Acme", "ORG", EntityClass.LLM, "llm:org")
    assert masker.mask("ask Acme about it", extra=[finding]).text == "ask [REDACTED:ORG] about it"
    entity = vault.by_original("Acme")
    assert entity is not None and entity.surrogate == "Vega-7"


def test_a_value_a_detector_redacts_stays_out_of_the_vault():
    """A detector finds its values again by itself: one it redacts is not kept, as before."""
    here, built = _instance(Vault(ROOT_KEY), LocalGiven(), Config(policies={"EMAIL": "redact"}))
    assert _mask(here, text="write to emily.carter@acme.com").text == "write to [REDACTED:EMAIL]"
    assert built.vault.entities() == []


def test_a_text_that_left_in_one_conversation_has_not_left_in_another():
    given, vault = LocalGiven(), Vault(ROOT_KEY)
    here, _ = _instance(vault, given)
    _sent(here, _mask(here, "cc:s1").text, "cc:s1")
    assert _mask(here, "cc:s2").text == TEXT  # masked for another conversation too (the name not known yet), not sent
    vault.intern("PERSON", EntityClass.CUSTOM, "Emily Carter")
    assert _mask(here, "cc:s1").text == TEXT  # as it left
    assert "Emily Carter" not in _mask(here, "cc:s2").text  # it never left there: masked anew


def test_a_text_that_left_as_it_was_masked_is_checked_once_until_the_vault_changes(monkeypatch):
    given, vault = LocalGiven(), Vault(ROOT_KEY)
    here, _ = _instance(vault, given)
    first = _mask(here)
    _sent(here, first.text)
    vault.intern("PERSON", EntityClass.CUSTOM, "Emily Carter")
    looked, asked = [], []
    masker = here.engine.masker
    # The text as its client sends it is what is read (0.10.2: a value learnt around one masked in it, too).
    known_outside, checked = masker.known_outside, given.checked
    monkeypatch.setattr(
        masker, "known_outside", lambda text, masked: looked.append(text) or known_outside(text, masked)
    )
    monkeypatch.setattr(given, "checked", lambda session, key: asked.append(session) or checked(session, key))
    assert [_mask(here).text for _ in range(3)] == [TEXT] * 3  # as it left
    assert looked == [TEXT] and asked == ["cc:s1"]  # once: the vault is the same
    vault.intern("PERSON", EntityClass.CUSTOM, "Hannah Mitchell")
    assert _mask(here).text == TEXT and looked == [TEXT, TEXT] and asked == ["cc:s1", "cc:s1"]  # the vault changed


@pytest.mark.parametrize("outputs", [(PORTAL, TEACH), (PORTAL, TEACH, PORTAL)])
def test_a_text_kept_but_never_sent_does_not_leave_with_a_value_a_later_text_of_its_request_teaches(outputs):
    """An earlier request kept the output as it was masked then (the domain not known yet), and never left (declined,
    stopped, given up). The next request carries it again with an address after it, which teaches the vault the domain:
    every sight of the output leaves masked."""
    here, _ = _instance(Vault(ROOT_KEY), LocalGiven())
    session = here.engine.session("conv:s1")
    assert _request(here, session, PORTAL) == [PORTAL]  # kept so, and never sent
    assert [text for text in _request(here, session, *outputs) if "acme-intra.it" in text] == []


@pytest.mark.parametrize("outputs", [(PORTAL, TEACH), (PORTAL, TEACH, PORTAL)])
def test_a_text_that_left_stays_as_it_left_when_a_later_text_of_its_request_teaches_a_value_in_it(outputs):
    here, _ = _instance(Vault(ROOT_KEY), LocalGiven())
    session = here.engine.session("conv:s1")
    assert _request(here, session, PORTAL) == [PORTAL]
    _sent(here, PORTAL, "conv:s1")  # it left
    out = _request(here, session, *outputs)
    sights = [text for text, output in zip(out, outputs, strict=True) if output == PORTAL]
    assert sights == [PORTAL] * outputs.count(PORTAL)  # as it left
    assert "acme-intra.it" not in out[1]  # the address: masked


def test_a_text_as_it_left_shows_neither_a_value_nor_the_text_in_its_repr():
    replaced = Replacement(
        9, 30, "emily.carter@acme.com", "EMAIL_1", "EMAIL", EntityClass.PII, "pii:email", Policy.MASK, "e" + "1" * 19
    )
    kept_before = Left("write to emily.carter@acme.com", (), True)  # kept before the vault knew the address
    for left in (Left("write to EMAIL_1", (replaced,), True), kept_before):
        assert "emily.carter@acme.com" not in repr(left) and "write to" not in repr(left)


def test_the_memory_of_texts_found_fit_to_leave_as_they_left_says_what_it_takes():
    """Beside the engine's cache (``Engine.cached_bytes``), what a server weighs of each space: its entries are all of
    one size, and that size is not less than what they take."""
    given, vault = LocalGiven(), Vault(ROOT_KEY)
    here, _ = _instance(vault, given)
    assert here.checked_bytes == 0
    _sent(here, _mask(here).text)
    vault.intern("PERSON", EntityClass.CUSTOM, "Emily Carter")
    assert _mask(here).text == TEXT  # found fit to leave as it left: remembered
    one = here.checked_bytes - here.verifier.cached_bytes  # the final check's memory is weighed beside it
    assert one > 0
    memory = here._as_it_left._data
    for n in range(20_000):  # as full as a busy space's: keys of 32 bytes, vault versions past the small numbers
        here._as_it_left.put(n.to_bytes(32, "big"), 1_000_000 + n)
    taken = sys.getsizeof(memory) + sum(sys.getsizeof(key) + sys.getsizeof(value) for key, value in memory.items())
    mine = here.checked_bytes - here.verifier.cached_bytes
    assert mine == one * len(memory) and mine >= taken
