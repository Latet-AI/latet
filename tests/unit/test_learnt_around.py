"""A value the vault learns around a value it masked already in a text (0.10.2): that text, masked again, is masked
whole.

The engine keeps each text's masking and gives it again while the vault has learnt nothing it leaves in clear, and a
conversation sends a text again as it left. Until 0.10.2 both asked the masked text alone: a value learnt since around
a value masked in it (an IBAN kept as a secret's group, its secret masked alone in the earlier text; `Mario Rossi`
learnt whole, `Rossi` masked alone) is not in the masked text, which holds a fake where part of it was, so the rest of
it stayed in clear, and the final check, which finds no whole value in clear, let it out. Now the text itself is read:
a value the vault knows there that is not inside a value masked there masks the text again. Every text read alone, as
0.10.0 and 0.10.1 read them (found by 0.11.0's fuzz with the integration review's pieces)."""

import json

import pytest

from latet.bridge.apps import Apps
from latet.bridge.review import MASKED
from latet.config import Config
from latet.engine.build import build
from latet.engine.masker import AllowList, Masker, MaskReport
from latet.engine.pipeline import Session
from latet.engine.surrogates.generators import Drbg, shape
from latet.engine.types import EntityClass, Policy, Replacement, SlotKind
from latet.engine.vault import Entity, Vault, derive_keys
from latet.gateway.app import OnServer
from latet.gateway.mediate import Mediator

from ..conftest import ROOT_KEY

DATA = SlotKind.TOOL_RESULT
IBAN = "IT60X0542811101000000123456"
SECRET_FIRST = "x password=0542811101 y"  # the assignment detector finds the secret, and the vault keeps it
GLUED = f"Mario{IBAN}Ferri"  # no IBAN found, glued on both sides: the secret inside it masked alone
THE_IBAN = f"{IBAN} Acme"  # the IBAN found, the secret inside it: the vault keeps it as a secret's group
NAMED = "please ask Mario Rossi about the invoice"


def _mediator(vault: Vault | None = None) -> Mediator:
    built = build(Config(profile="balanced", ner="off"), vault if vault is not None else Vault(ROOT_KEY))
    return Mediator(built.engine, built.verifier)


def _apps(mediator: Mediator, texts: list[str]) -> list[str]:
    """``texts`` masked as one applications call masks them (``Apps._mask``): each as one text, then those a later
    text of the call made stale masked again (``settle``)."""
    out = Apps(mediator, OnServer("https://latet.invalid", "u_key", kind="key"))._mask(texts, DATA)[1]
    return [item[MASKED].text for item in out]


def _request(mediator: Mediator, session: Session, texts: list[str]) -> list[str]:
    """``texts`` masked as a request of the conversation ``session`` masks them: each in turn (one sent before as it
    left, when it may), then those a later text made stale (``settle``). Nothing records the request as sent."""
    report = MaskReport()
    call = mediator.call()
    done = [mediator.mask_value(text, DATA, session, report, call=call) for text in texts]
    mediator.settle(call)
    return [masked.text for masked in done]


def _in_clear(masked: str) -> list[str]:
    """The IBAN's parts ``masked`` shows."""
    return [part for part in ("IT60X", "0542811101", "000000123456") if part in masked]


PAY = f"pay {IBAN} now"  # the IBAN at 4 to 31


@pytest.mark.parametrize(
    ("places", "outside"),
    [
        ((), True),  # in clear
        (((4, 31),), False),  # masked whole
        (((0, 35),), False),  # inside a larger value masked
        (((14, 24),), True),  # around a value masked (the secret inside it)
        (((0, 10),), True),  # across the end of one
        (((4, 17), (17, 31)), True),  # across two: inside neither
        (((0, 35), (10, 12)), False),  # inside the one begun first, a later one inside it too
    ],
)
def test_a_known_value_is_outside_the_values_masked_unless_it_is_inside_one(places, outside):
    vault = Vault(ROOT_KEY)
    vault.intern("IBAN", EntityClass.PII, IBAN)
    replacements = [
        Replacement(s, e, PAY[s:e], "x", "IBAN", EntityClass.PII, "t", Policy.MASK, None) for s, e in places
    ]
    assert Masker(vault, []).known_outside(PAY, replacements) is outside


def test_a_known_value_you_let_through_is_never_outside():
    vault = Vault(ROOT_KEY)
    vault.intern("IBAN", EntityClass.PII, IBAN)
    assert not Masker(vault, [], allow=AllowList(values={IBAN})).known_outside(PAY, [])


def test_a_text_masked_before_a_later_text_of_its_call_teaches_a_value_around_its_secret_is_masked_whole():
    """The secret first, then the glued IBAN (only the secret masked), then the IBAN found whole, in one call: settle
    masks the glued one again. Before, its masking showed no value in clear, and the IBAN's other characters stayed."""
    masked = _apps(_mediator(), [SECRET_FIRST, GLUED, THE_IBAN])[1]
    assert _in_clear(masked) == [], masked


def test_a_text_the_cache_keeps_is_masked_whole_in_a_later_call_once_the_vault_learnt_a_value_around_its_secret():
    mediator = _mediator()
    first = _apps(mediator, [SECRET_FIRST, GLUED])[1]
    assert _in_clear(first) == ["IT60X", "000000123456"]  # the IBAN unknown yet: its secret alone masked
    _apps(mediator, [THE_IBAN])
    assert not mediator.engine.cached(GLUED, DATA, Session("apps", kept=False))  # read ahead again, not skipped
    masked = _apps(mediator, [GLUED])[0]
    assert _in_clear(masked) == [], masked


def test_the_same_text_in_a_new_conversation_is_masked_whole_once_the_vault_learnt_a_value_around_its_secret():
    """The engine's cache serves every conversation: a text the first masked is given to the second from it."""
    mediator = _mediator()
    _request(mediator, Session("conversation-1"), [SECRET_FIRST, GLUED])
    _request(mediator, Session("conversation-2"), [THE_IBAN])
    masked = _request(mediator, Session("conversation-3"), [GLUED])[0]
    assert _in_clear(masked) == [], masked


def _given_by_0_10_0(etype: str, value: str) -> Entity:
    """``value`` as 0.10.0 kept a short secret: with a fake of its shape (four digits for `1234`)."""
    fake = shape(value, Drbg(derive_keys(ROOT_KEY)["surrogate"], f"{etype}\x00{value}\x000".encode()))
    vault = Vault(ROOT_KEY)
    return Entity(vault.entity_id(etype, value), etype, EntityClass.SECRET, value, fake, 0, 0.0, "assignment:password")


@pytest.mark.parametrize("given", ["0.10.0", "0.10.1"])
def test_a_short_secret_known_first_inside_the_glued_iban_has_the_iban_masked_whole_once_the_call_learns_it(given):
    """0.10.0's `1234` (its fake of four digits), or 0.10.1's (fifteen digits), masked inside the glued IBAN; a later
    text of the call makes the IBAN a secret's group: the glued one is masked again, whole."""
    if given == "0.10.0":
        vault = Vault(ROOT_KEY, [_given_by_0_10_0("PASSWORD", "1234")])
    else:
        vault = Vault(ROOT_KEY)
        vault.intern("PASSWORD", EntityClass.SECRET, "1234")
    masked = _apps(_mediator(vault), [GLUED, THE_IBAN])[0]
    assert "1234" not in masked and _in_clear(masked) == [], masked


@pytest.mark.parametrize("path", ["conversation", "applications"])
def test_a_name_learnt_whole_around_a_surname_masked_before_is_masked_whole_in_a_text_masked_before(path):
    """`Rossi` known, the text masked with it alone; then `Mario Rossi` learnt whole (another text's name): the text,
    masked again from the cache, has the whole name masked, `Mario` no longer in clear."""
    vault = Vault(ROOT_KEY)
    vault.intern("PERSON", EntityClass.NER, "Rossi")
    mediator = _mediator(vault)

    def masking(n: int) -> str:
        if path == "applications":
            return _apps(mediator, [NAMED, f"x{n}"])[0]
        return _request(mediator, Session(f"conversation-{n}"), [NAMED])[0]

    first = masking(1)
    assert "Mario" in first and "Rossi" not in first
    vault.intern("PERSON", EntityClass.NER, "Mario Rossi")
    masked = masking(2)
    assert "Mario" not in masked and "Rossi" not in masked, masked


def test_a_text_kept_but_never_sent_is_masked_whole_once_the_vault_learnt_a_value_around_its_secret():
    """A request that never left (blocked, or its client gave up): its texts are kept as they were masked, and none is
    known to have left. Sent again once the IBAN is learnt elsewhere, the glued one is masked anew, whole, never sent
    with the IBAN's other characters in clear."""
    mediator = _mediator()
    session = Session("conversation-1")
    _request(mediator, session, [SECRET_FIRST, GLUED])
    _request(mediator, Session("conversation-2"), [THE_IBAN])
    masked = _request(mediator, session, [SECRET_FIRST, GLUED])[1]
    assert _in_clear(masked) == [], masked


def test_a_text_kept_but_never_sent_is_masked_whole_when_a_later_text_of_its_request_teaches_a_value_around_it():
    """The same text sent again in a request whose later text teaches the IBAN: ``settle`` asks it again."""
    mediator = _mediator()
    session = Session("conversation-1")
    _request(mediator, session, [SECRET_FIRST, GLUED])
    masked = _request(mediator, session, [SECRET_FIRST, GLUED, THE_IBAN])[1]
    assert _in_clear(masked) == [], masked


def test_a_json_document_kept_but_never_sent_is_masked_whole_once_the_vault_learnt_a_value_around_its_secret():
    """A tool output that is a JSON document, masked value by value (its replacements' places are inside its values):
    the same."""
    mediator = _mediator()
    session = Session("conversation-1")
    document = json.dumps({"owner": GLUED, "note": "ok"})
    _request(mediator, session, [SECRET_FIRST, document])
    _request(mediator, Session("conversation-2"), [THE_IBAN])
    masked = _request(mediator, session, [SECRET_FIRST, document])[1]
    assert _in_clear(masked) == [], masked


def test_a_json_documents_value_is_masked_whole_when_a_later_value_teaches_a_value_around_its_secret():
    """A JSON document's values are masked one by one, then those a later value made stale, by the document itself
    (masked with no request to settle, as the Live Reviewer masks what you approve): the same rule."""
    document = json.dumps({"login": SECRET_FIRST, "owner": GLUED, "account": THE_IBAN})
    masked = _mediator().mask_value(document, DATA, Session("conversation-1"), MaskReport()).text
    assert _in_clear(json.loads(masked)["owner"]) == [], masked


@pytest.mark.parametrize("document", [False, True], ids=["text", "json"])
def test_a_text_that_left_leaves_again_as_it_left_whatever_the_vault_learnt_since(document):
    """By design: the provider holds it as it left, and its cache with it. Only a text never sent is masked anew."""
    mediator = _mediator()
    session = Session("conversation-1")
    texts = [SECRET_FIRST, json.dumps({"owner": GLUED}) if document else GLUED]
    first = _request(mediator, session, texts)
    mediator.verifier.left(session.key, first)  # the gateway saw the request reach its provider
    _request(mediator, Session("conversation-2"), [THE_IBAN])
    assert _request(mediator, session, texts) == first


def test_a_text_whose_known_values_are_inside_its_masked_values_is_served_from_the_cache():
    """No needless masking: the secret the text holds is masked whole in it, whatever the vault learns since."""
    mediator = _mediator()
    engine, session = mediator.engine, Session("conversation-1")
    first = engine.mask_text(SECRET_FIRST, DATA, session)
    assert not first.cached and "0542811101" not in first.text
    engine.vault.intern("PASSWORD", EntityClass.SECRET, "an0ther-Secret-77")  # the vault's version moves
    assert engine.cached(SECRET_FIRST, DATA, session)
    again = engine.mask_text(SECRET_FIRST, DATA, session)
    assert again.cached and again.text == first.text


def test_a_text_kept_whose_known_values_are_inside_its_masked_values_leaves_as_it_left_without_asking(monkeypatch):
    """Nor a needless question to the record of what left (a server's store): nothing of it in clear, nothing asked."""
    mediator = _mediator()
    session = Session("conversation-1")
    first = _request(mediator, session, [SECRET_FIRST])
    mediator.engine.vault.intern("PASSWORD", EntityClass.SECRET, "an0ther-Secret-77")
    asked: list[str] = []
    monkeypatch.setattr(mediator.verifier, "sent", lambda key, text: asked.append(text) or False)
    assert _request(mediator, session, [SECRET_FIRST]) == first
    assert asked == []
