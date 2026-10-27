"""What the engine finds in a text without keeping anything (the applications API's scan), a session that keeps no
conversation, and the restore of a text from every fake of the vault (its unmask)."""

from typing import Any

import pytest

from latet.config import Config
from latet.engine import pipeline
from latet.engine.build import build, build_masker
from latet.engine.masker import MaskReport
from latet.engine.pipeline import Engine, Scope, Session
from latet.engine.rehydrate import restores_for
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, Vault
from latet.gateway.mediate import LiveItem, Mediator

from ..conftest import ROOT_KEY

CANARY = "mario.rossi@acme.it"
IBAN = "GB82WEST12345698765432"
GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"


def _engine(**config: Any) -> Engine:
    """An engine as the engine's unit tests build one: an in-memory vault, the `balanced` profile, no name model."""
    vault = Vault(ROOT_KEY)
    masker, _ = build_masker(Config(profile="balanced", ner="off", **config), vault)
    return Engine(vault, masker)


@pytest.fixture
def engine() -> Engine:
    return _engine()


class _Projects:
    """A session attached to a folder gets it as its project, with the rules ``scope`` gives (None: the global ones)."""

    def __init__(self, scope: Scope | None = None) -> None:
        self.scope = scope

    def scope_for(self, session: Session) -> Scope | None:
        return self.scope

    def attach(self, session: Session, cwd: Any) -> None:
        session.project_id, session.cwd = "acme", cwd


def test_findings_write_nothing(engine):
    before = (engine.vault.count, engine.vault.version)
    spans = engine.find(f"write to {CANARY}, IBAN {IBAN}", SlotKind.USER_TEXT, Session("s", kept=False))
    assert {s.etype for s in spans} == {"EMAIL", "IBAN"}
    assert (engine.vault.count, engine.vault.version) == before


def test_findings_are_what_mask_replaces(engine):
    # A token, an address whose domain comes back alone, an IBAN and a fake of the vault's own (a value like any other):
    # found where masking replaces them, as the types it replaces them as.
    fake = engine.mask_text("ops@nordest-intra.it", SlotKind.USER_TEXT, Session("before", kept=False)).text
    text = f"token {GH_PAT}, write to {CANARY} (acme.it), IBAN {IBAN}, cc {fake}"
    found = engine.find(text, SlotKind.USER_TEXT, Session("s", kept=False))
    masked = engine.mask_text(text, SlotKind.USER_TEXT, Session("s", kept=False))
    assert [(s.start, s.end, s.etype) for s in found] == [(r.start, r.end, r.etype) for r in masked.replacements]
    assert [text[s.start : s.end] for s in found] == [GH_PAT, CANARY, "acme.it", IBAN, fake]


def test_findings_leave_out_allowed_types():
    engine = _engine(policies={"EMAIL": "allow"})
    text = f"write to {CANARY}, IBAN {IBAN}"
    spans = engine.find(text, SlotKind.USER_TEXT, Session("s", kept=False))
    assert [s.etype for s in spans] == ["IBAN"]
    assert [r.etype for r in engine.mask_text(text, SlotKind.USER_TEXT, Session("s", kept=False)).replacements] == [
        "IBAN"
    ]


def test_findings_report_the_spaces_own_fakes(engine):
    fake = engine.mask_text(CANARY, SlotKind.USER_TEXT, Session("a", kept=False)).text
    text = f"reply to {fake}"
    spans = engine.find(text, SlotKind.USER_TEXT, Session("s", kept=False))
    assert [(s.etype, text[s.start : s.end]) for s in spans] == [("EMAIL", fake)]


def test_findings_follow_the_rules_of_the_sessions_project(engine):
    # As mask_text picks them: a project's own masker (one that lets addresses through), and nothing where Latet is off.
    text = f"write to {CANARY}, IBAN {IBAN}"
    own, _ = build_masker(Config(profile="balanced", ner="off", policies={"EMAIL": "allow"}), engine.vault)
    engine.scopes = _Projects(Scope(key="acme", tag="acme", masker=own))
    session = Session("s", kept=False)
    engine.attach(session, "/work/acme")
    assert [s.etype for s in engine.find(text, SlotKind.USER_TEXT, session)] == ["IBAN"]
    assert {s.etype for s in engine.find(text, SlotKind.USER_TEXT, Session("t", kept=False))} == {"EMAIL", "IBAN"}
    engine.scopes = _Projects(Scope(key="off", tag="off", masker=own, enabled=False))
    assert engine.find(text, SlotKind.USER_TEXT, session) == []


def test_unmask_restores_fakes_of_any_conversation(engine):
    masked = engine.mask_text(f"from {CANARY}", SlotKind.USER_TEXT, Session("a", kept=False)).text
    assert engine.unmask(masked) == (f"from {CANARY}", 1)
    other = engine.mask_text(f"IBAN {IBAN}", SlotKind.USER_TEXT, Session("b", kept=False)).text
    assert engine.unmask(f"{masked}; {other}; {masked}") == (f"from {CANARY}; IBAN {IBAN}; from {CANARY}", 2)


def test_unmask_keeps_the_vaults_restores_until_it_moves(engine, monkeypatch):
    made: list[int] = []
    real = pipeline.restores_for
    monkeypatch.setattr(pipeline, "restores_for", lambda entities, **kw: made.append(1) or real(entities, **kw))
    masked = engine.mask_text(f"from {CANARY}", SlotKind.USER_TEXT, Session("a", kept=False)).text
    engine.unmask(masked)
    assert engine.unmask(f"{masked} again") == (f"from {CANARY} again", 1)
    assert len(made) == 1
    other = engine.mask_text(f"IBAN {IBAN}", SlotKind.USER_TEXT, Session("b", kept=False)).text
    assert engine.unmask(other) == (f"IBAN {IBAN}", 1)  # a value kept since: made again, with it
    assert len(made) == 2


class _Narrowing(Vault):
    """A vault that gives, for a text, only the entities whose fake is in it (as a key's vault on a server will)."""

    def candidates_in(self, text: str) -> list[Entity]:
        return [e for e in self.entities() if e.surrogate in text]


def test_unmask_reads_a_vault_that_narrows_for_each_text():
    # Its restores depend on the text: none is kept from one text for the next, though the vault did not move.
    vault = _Narrowing(ROOT_KEY)
    masker, _ = build_masker(Config(profile="balanced", ner="off"), vault)
    engine = Engine(vault, masker)
    email = engine.mask_text(CANARY, SlotKind.USER_TEXT, Session("a", kept=False)).text
    iban = engine.mask_text(IBAN, SlotKind.USER_TEXT, Session("a", kept=False)).text
    assert engine.unmask(email) == (CANARY, 1)
    assert engine.unmask(iban) == (IBAN, 1)


def test_unmask_leaves_other_text_alone(engine):
    ours = engine.mask_text(CANARY, SlotKind.USER_TEXT, Session("a", kept=False)).text
    theirs = Vault(b"\x09" * 32).intern("EMAIL", EntityClass.PII, CANARY).surrogate  # the same value, another vault
    assert theirs != ours and theirs.split("@")[1] != ours.split("@")[1]
    for text in (f"from {theirs}", "plain words, nothing masked", "", "   "):
        assert engine.unmask(text) == (text, 0)


def test_unmask_restores_no_name_part(engine):
    person = Entity(
        id=engine.vault.entity_id("PERSON", "Mario Rossi"),
        etype="PERSON",
        eclass=EntityClass.NER,
        original="Mario Rossi",
        surrogate="Laura Bianchi",
        counter=0,
        created=0.0,
        rule="ner",
    )
    engine.vault.learn(person)
    assert "Bianchi" in {r.pattern for r in restores_for([person])}  # a conversation shown the name restores its parts
    assert "Bianchi" not in {r.pattern for r in restores_for([person], parts=False)}
    assert engine.unmask("Bianchi called") == ("Bianchi called", 0)
    assert engine.unmask("Laura Bianchi called, LAURA BIANCHI too") == ("Mario Rossi called, MARIO ROSSI too", 1)


def test_unmask_restores_a_one_word_name_as_it_was_given_only(engine):
    """Ruling T4a: a person's fake of one word is restored across a whole vault in the very form it was given, as its
    parts are left out: an ordinary `anna` or `ANNA` in a text is no fake, and becomes no one's real name. A
    conversation shown the name restores it in every case, as before."""
    person = Entity(
        id=engine.vault.entity_id("PERSON", "Mario"),
        etype="PERSON",
        eclass=EntityClass.NER,
        original="Mario",
        surrogate="Anna",
        counter=0,
        created=0.0,
        rule="ner",
    )
    engine.vault.learn(person)
    assert {r.pattern for r in restores_for([person])} == {"Anna", "ANNA", "anna"}
    assert {r.pattern for r in restores_for([person], parts=False)} == {"Anna"}
    assert engine.unmask("Anna called; anna and ANNA did not") == ("Mario called; anna and ANNA did not", 1)


def test_a_session_not_kept_writes_no_conversation(engine, monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(engine.conversations, "update", lambda session, shown=(): written.append(session.key))
    engine.scopes = _Projects()
    for key, kept in (("x", False), ("y", True)):
        session = Session(key, kept=kept)
        engine.attach(session, "/work/acme")
        engine.mask_text(f"from {CANARY}", SlotKind.USER_TEXT, session)
        assert session.project_id == "acme" and session.entity_ids  # what the call itself reads, in its memory
    assert written == ["y", "y"]  # the kept one alone: its project, then what it was shown


def test_findings_keep_what_is_redacted():
    # A type redacted, not masked, is found all the same: mask replaces it with a notice of its type, at that place.
    engine = _engine(policies={"IBAN": "redact"})
    text = f"IBAN {IBAN}, write to {CANARY}"
    found = engine.find(text, SlotKind.USER_TEXT, Session("s", kept=False))
    masked = engine.mask_text(text, SlotKind.USER_TEXT, Session("s", kept=False))
    assert [(s.start, s.end, s.etype) for s in found] == [(r.start, r.end, r.etype) for r in masked.replacements]
    [redacted] = [r for r in masked.replacements if r.etype == "IBAN"]
    assert text[redacted.start : redacted.end] == IBAN and redacted.replacement == "[REDACTED:IBAN]"
    assert masked.text.startswith("IBAN [REDACTED:IBAN], write to ") and CANARY not in masked.text


def _mediator(engine: Engine) -> Mediator:
    built = build(Config(profile="balanced", ner="off"), engine.vault)
    return Mediator(engine, built.verifier)


def test_a_session_not_kept_keeps_no_text_as_it_left(engine, monkeypatch):
    """An application's call is no conversation: none of its texts is kept as it left (``Mediator.given``), nor read
    back from there; a conversation's are, as before."""
    mediator = _mediator(engine)
    kept: list[bytes] = []
    read: list[bytes] = []
    real_keep, real_masked = mediator.given.keep, mediator.given.masked
    monkeypatch.setattr(mediator.given, "keep", lambda key, left: kept.append(key) or real_keep(key, left))
    monkeypatch.setattr(mediator.given, "masked", lambda key: read.append(key) or real_masked(key))
    for _ in range(2):
        done = mediator.mask_value(f"from {CANARY}", SlotKind.USER_TEXT, Session("apps", kept=False), MaskReport())
        assert CANARY not in done.text
    assert kept == [] and read == []
    mediator.mask_value(f"from {CANARY}", SlotKind.USER_TEXT, Session("conversation"), MaskReport())
    assert len(kept) == 1 and len(read) == 1


def test_a_session_not_kept_keeps_what_it_approved_in_its_memory_alone(engine, monkeypatch):
    mediator = _mediator(engine)
    written: list[str] = []
    monkeypatch.setattr(engine.conversations, "update", lambda session, shown=(): written.append(session.key))
    for key, kept in (("x", False), ("y", True)):
        session = Session(key, kept=kept)
        item = LiveItem(0, f"from {CANARY}", SlotKind.USER_TEXT, None, None, False, "from someone")
        mediator._approve(session, [item])
        assert session.approved  # what the call itself reads, in its memory
    assert written == ["y"]
