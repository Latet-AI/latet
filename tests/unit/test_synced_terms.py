"""The terms a company syncs (Latet Enterprise) as the Community engine masks them: one live detector, every term
masked as a company's locked term is, whatever the person lets through, and its changes followed by a tag."""

import json

import pytest

from latet.config import Config, TermConfig
from latet.engine.build import build, build_masker
from latet.engine.detectors import terms as terms_module
from latet.engine.detectors.terms import SYNCED_ID, LiveTerms, TermsDetector, is_locked
from latet.engine.types import EntityClass, Policy, SlotKind, Span
from latet.engine.vault import Vault
from latet.engine.verifier import Verifier
from latet.instructions import findings

from ..conftest import ROOT_KEY
from .test_locked_terms import ADDRESSES, WRITTEN  # E2's cases, for the terms a company syncs

HALDEN = ("Halden Insurance", "CLIENT")
FORMS = ("Halden Insurance", "HALDEN INSURANCE", "halden insurance")
AROUND = {"name": "renewals", "type": "RENEWAL", "pattern": r"(?i)Halden Insurance \w+", "policy": "allow"}


def _live(*terms, tag="1.1"):
    live = LiveTerms()
    live.use(terms or [HALDEN], tag)
    return live


def _masker(config, live=None):
    return build_masker(config, Vault(ROOT_KEY), extra=(live or _live(),))[0]


def test_a_synced_term_is_masked_in_any_case_as_whole_words_with_its_rule():
    masked = _masker(Config(ner="off")).mask("Halden Insurance and HALDEN INSURANCE, not Haldenville")
    assert "alden" not in masked.text.casefold().replace("haldenville", "")
    assert {r.rule for r in masked.replacements} == {"synced:client"} and "Haldenville" in masked.text


def test_a_synced_term_is_never_let_through_by_an_exception():
    patterns = [r"\AHalden Insurance\z", "[A-Z ]{5,}", "(?-i:halden insurance)"]
    masker = _masker(Config(ner="off", allow_values=list(FORMS), allow_patterns=patterns))
    for form in FORMS:
        assert "halden" not in masker.mask(f"see {form} today").text.casefold()
        assert "halden" not in masker.mask(json.dumps({"notes": form}), SlotKind.TOOL_RESULT).text.casefold()


def test_a_policy_of_yours_that_lets_its_type_through_never_lets_a_synced_term_through():
    masker = _masker(Config(ner="off", policies={"CLIENT": Policy.ALLOW}))
    masked = masker.mask("Halden Insurance renewed")
    assert "Halden" not in masked.text and masked.replacements[0].policy == Policy.MASK
    assert [s.etype for s in masker.findings("Halden Insurance renewed")] == ["CLIENT"]


def test_the_final_check_reports_a_synced_term_left_in_clear_whatever_a_policy_says_of_its_type():
    masker = _masker(Config(ner="off", policies={"CLIENT": Policy.ALLOW}))
    leaks = Verifier(masker).verify([("send it to Halden Insurance", SlotKind.USER_TEXT, False)], "s").leaks
    assert [leak.preview for leak in leaks] == ["CLIENT[16]"]  # a leak, never "your settings let this type through"


def test_a_text_verified_clean_is_checked_again_once_the_live_terms_gain_a_term():
    """Fix round 2's N1: the final check's memory of clean blocks is keyed by the terms' tag too (S4)."""
    live = LiveTerms()
    verifier = Verifier(_masker(Config(ner="off"), live))
    slot = [("send it to Kestrel Analytics", SlotKind.USER_TEXT, False)]
    assert verifier.verify(slot, "s").leaks == []  # verified clean: kept as such
    live.use([("Kestrel Analytics", "CLIENT")], "2.1")  # a sync gains the term; the vault does not move
    assert [leak.preview for leak in verifier.verify(slot, "s").leaks] == ["CLIENT[17]"]


def test_a_sync_during_the_final_check_never_marks_a_block_clean_under_the_new_terms():
    """The design security review's B-M2: the tag is read once, before the detection, so a block checked with the terms
    before a sync is never kept as clean under the new ones (another conversation would skip it)."""
    live = LiveTerms()
    masker = _masker(Config(ner="off"), live)
    verifier, detect = Verifier(masker), masker.detect

    def syncing(*args, **kwargs):
        found = detect(*args, **kwargs)  # read with the terms before the sync
        live.use([("Kestrel Analytics", "CLIENT")], "2.1")
        return found

    masker.detect = syncing
    slot = [("send it to Kestrel Analytics", SlotKind.USER_TEXT, False)]
    assert verifier.verify(slot, "a").leaks == []  # checked with the terms of before: clean then
    masker.detect = detect
    assert [leak.preview for leak in verifier.verify(slot, "b").leaks] == ["CLIENT[17]"]  # never skipped as clean


def test_what_you_leave_in_clear_around_a_synced_term_is_cut_around_it():
    """A regression check (0.9.10's merge already gives way here, A-I3): the address test below pins the cut."""
    masked = _masker(Config(ner="off", recognizers=[AROUND])).mask("see Halden Insurance renewal")
    assert "Halden" not in masked.text and masked.text.endswith(" renewal")
    [replacement] = masked.replacements
    assert (replacement.original, replacement.etype, replacement.rule) == (
        "Halden Insurance",
        "CLIENT",
        "synced:client",
    )


@pytest.mark.parametrize("address", list(ADDRESSES))
def test_a_synced_domain_inside_an_address_you_let_through_is_masked(address):
    """The review's A-I3, as E2's `test_a_locked_domain_inside_an_address_you_let_through_is_masked`: only the cut
    around a company's term (`is_locked` knowing `synced:`) masks the domain inside an address you let through."""
    mine, text, kind = ADDRESSES[address]
    masked = _masker(mine, _live(("acme-intra.it", "DOMAIN"))).mask(text, kind)
    assert "acme-intra.it" not in masked.text and "ops@" in masked.text
    assert [(r.etype, r.rule) for r in masked.replacements] == [("DOMAIN", "synced:domain")]


def test_a_synced_term_written_in_escapes_is_found():
    masked = _masker(Config(ner="off", allow_values=list(FORMS))).mask("q=Halden%20Insurance&x=1")
    assert "Halden" not in masked.text


@pytest.mark.parametrize("form", list(WRITTEN))
def test_a_synced_term_written_in_escapes_is_masked_whatever_policy_of_yours_names_its_type(tmp_path, form):
    """The review's A-I2: a policy of yours that lets the type through never drops a synced term read through escapes
    (`detect`'s `deep`, `_found`'s `masked_with`): masked, a leak to the final check as written, seen by `findings`."""
    text, kind = WRITTEN[form]  # "Project Falcon", written in escapes of no character
    masker = _masker(Config(ner="off", policies={"PROJECT": "allow"}), _live(("Project Falcon", "PROJECT")))
    masked = masker.mask(text, kind)
    assert "falcon" not in masked.text.casefold() and any(r.etype == "PROJECT" for r in masked.replacements)
    assert any(leak.etype == "PROJECT" for leak in Verifier(masker).verify([(text, kind, False)], "s").leaks)
    instructions = tmp_path / "CLAUDE.md"
    instructions.write_text(text + "\n", "utf-8")
    assert "PROJECT" in [f.etype for f in findings([instructions], masker)]


@pytest.mark.parametrize("filler", [0, 2_001], ids=["an-alternation", "word-by-word"])
def test_synced_terms_across_one_another_are_each_found(filler):
    """A regression check (A-R2): crossing synced terms are masked, as an alternation and as a word index. Every synced
    term is masked, so they are masked whole with or without `every=True`: the next test pins `every` (B-R1)."""
    more = [(f"Client {n:04d}", "CLIENT") for n in range(filler)]
    live = _live(("Project Falcon", "CLIENT"), ("Falcon Phase", "PROJECT"), *more)
    masked = _masker(Config(ner="off"), live).mask("see Project Falcon Phase 2").text
    assert "Falcon" not in masked and "Phase" not in masked


def test_the_live_terms_matcher_finds_every_term_at_every_place(monkeypatch):
    """The design security review's B-M1, pinned on the matcher itself (B-R1): `LiveTerms` builds each of its matchers
    with `every=True`, as E2's `LockedTermsDetector` does, so that no term hides another however many overlap."""
    made, real = [], terms_module.TermsDetector

    def recording(*args, **kwargs):
        made.append(kwargs.get("every", False))
        return real(*args, **kwargs)

    monkeypatch.setattr(terms_module, "TermsDetector", recording)
    LiveTerms().use([HALDEN, ("Kestrel Analytics", "CLIENT")], "1.1")
    assert made and all(made)  # the empty matcher of a new LiveTerms, and the one `use` builds


def test_the_checks_never_take_a_synced_term_for_an_exception(tmp_path):
    masker = _masker(Config(ner="off", allow_values=list(FORMS)))
    span = next(iter(masker.detect("Halden Insurance", _ctx())))
    assert is_locked(span) and not masker.fake_or_let_through("Halden Insurance", span)
    instructions = tmp_path / "CLAUDE.md"
    instructions.write_text("send it to Halden Insurance")
    assert [f.etype for f in findings([instructions], masker)] == ["CLIENT"]  # auto mode's check sees it


def test_the_live_detector_is_shared_found_by_its_id_and_never_weighed_as_a_space_s_terms():
    live = _live()
    masker = _masker(Config(ner="off"), live)
    assert live in masker.detectors and live.id == SYNCED_ID and live.locks
    assert not isinstance(live, TermsDetector)  # Projects._scope weighs TermsDetectors: this one is the instance's


def test_the_masker_s_tag_follows_the_live_terms():
    live = LiveTerms()  # no term yet: no tag
    masker = _masker(Config(ner="off"), live)
    assert masker.tag == "" and _masker(Config(ner="off"), LiveTerms()).tag == ""
    live.use([HALDEN, ("Kestrel Analytics", "CLIENT")], "7.1")
    assert masker.tag == "7.1" and live.count == 2
    assert "Kestrel" not in masker.mask("Kestrel Analytics called").text


def test_a_term_set_is_never_used_without_a_tag():
    """Fix round 1: every key keeps 0.11's form while the tag is empty, so terms under no tag would be served the
    maskings and the clean marks made before them. None is taken; an empty set needs none (a new LiveTerms)."""
    live = LiveTerms()
    with pytest.raises(ValueError, match="tag"):
        live.use([HALDEN], "")
    assert (live.tag, live.count) == ("", 0)  # nothing taken
    live.use([], "")
    live.use([HALDEN], "1.1")
    assert (live.tag, live.count) == ("1.1", 1)


def test_the_types_it_is_given_are_upper_cased_as_the_settings_types_are():
    """Fix round 1: a type given in lower case still meets your company's policy of its type (``_policy``)."""
    config = Config(ner="off", policies={"CLIENT": Policy.REDACT})
    masked = _masker(config, _live(("Halden Insurance", "client"))).mask("Halden Insurance renewed")
    assert masked.text == "[REDACTED:CLIENT] renewed"
    assert [(r.etype, r.rule) for r in masked.replacements] == [("CLIENT", "synced:client")]


def test_e2_s_locked_terms_are_still_locked_and_found_by_their_detector():
    config = Config(ner="off", allow_values=["Project Falcon"])
    config.lock_terms([TermConfig(value="Project Falcon", type="PROJECT")])
    assert "Falcon" not in _masker(config).mask("see Project Falcon").text
    assert is_locked(Span(0, 4, "PROJECT", EntityClass.CUSTOM, "company:project"))


def test_a_value_the_vault_knows_outside_what_was_masked_holds_a_synced_term_whatever_you_let_through():
    """Latet 0.11's check of a cached masking (``known_outside``), as ``known_in``: a value your exception lets through
    that holds a synced term is in clear there (the merge's R3, for the terms a company syncs: S2)."""
    mine = Config(ner="off", allow_patterns=["Halden Insurance"])
    masker = _masker(mine)
    masker.vault.intern("CLIENT", EntityClass.CUSTOM, "Halden Insurance")
    assert masker.known_outside("the Halden Insurance renewal", []) is True
    yours = build_masker(mine, Vault(ROOT_KEY))[0]
    yours.vault.intern("CLIENT", EntityClass.CUSTOM, "Halden Insurance")
    assert yours.known_outside("the Halden Insurance renewal", []) is False  # no synced term: your exception, as before


def test_a_call_is_read_joined_only_while_no_synced_term_is_masked_and_never_across_a_sync():  # [D6], S3
    """Latet 0.11 reads an applications call's texts joined, and a joined answer carries no locked span: the live
    terms are joinable while they hold none, and a part read before a sync gained one answers none of its texts after
    (each is read alone, with the terms)."""
    live = LiveTerms()
    masker = _masker(Config(ner="off"), live)
    texts = ["write to Maria about Halden Insurance", "and to Luca about the renewal"]
    assert masker._parts(texts, SlotKind.TOOL_RESULT) == [texts]
    with masker.joined(texts, SlotKind.TOOL_RESULT):
        masker.mask(texts[1], SlotKind.TOOL_RESULT)  # the part is read joined here, before the sync
        live.use([HALDEN], "2.1")
        assert "Halden" not in masker.mask(texts[0], SlotKind.TOOL_RESULT).text  # read alone, with the terms
    assert masker._parts(texts, SlotKind.TOOL_RESULT) == []  # a synced term masked: each text alone, as locked terms


def _ctx():
    from latet.engine.detectors.base import DetectContext

    return DetectContext()


# --- the re-review's R1: a tag that comes back never brings back what straddled a change ------------------------------
KESTREL = ("Kestrel Analytics", "CLIENT")


def _removed_while_read(masker, live):
    """``masker.detect`` as a change lands while a check or a masking reads: the terms taken away (a removal, or the
    license gone) after its stamp was read, before it detects; ``done()`` puts the detector back."""
    detect = masker.detect

    def removed(*args, **kwargs):
        live.use([], "")
        return detect(*args, **kwargs)

    masker.detect = removed
    return lambda: setattr(masker, "detect", detect)


def test_a_block_checked_across_a_removal_is_checked_again_when_the_same_terms_come_back():
    """The re-review's R1, its probe: the final check reads the stamp once, before it detects (B-M2), so a block checked
    while the term was taken away is kept clean under the stamp of before. The same terms back give the same tag, never
    the same stamp: the block is checked again, in another conversation too, and the term found."""
    live = _live(KESTREL, tag="t1")
    masker = _masker(Config(ner="off"), live)
    verifier = Verifier(masker)
    slot = [("send it to Kestrel Analytics", SlotKind.USER_TEXT, False)]
    done = _removed_while_read(masker, live)
    assert verifier.verify(slot, "a").leaks == []  # checked with no term: clean then
    done()
    live.use([KESTREL], "t1")  # the term given again (or the license installed again): the same tag
    assert masker.tag == "t1"
    assert [leak.preview for leak in verifier.verify(slot, "b").leaks] == ["CLIENT[17]"]


def test_a_masking_across_a_removal_is_never_served_when_the_same_terms_come_back():
    """The engine's cache of masked blocks is keyed by the stamp read before masking: a block masked while the term was
    taken away is never served once the same terms are back (the applications API has no final check after it)."""
    live = _live(KESTREL, tag="t1")
    engine = build(Config(ner="off"), Vault(ROOT_KEY), extra=(live,)).engine
    session = engine.session("s")
    done = _removed_while_read(engine.masker, live)
    assert "Kestrel" in engine.mask_text("Kestrel Analytics called", SlotKind.USER_TEXT, session).text
    done()
    live.use([KESTREL], "t1")
    assert "Kestrel" not in engine.mask_text("Kestrel Analytics called", SlotKind.USER_TEXT, session).text


def test_the_stamp_is_empty_until_the_terms_first_change_and_moves_at_every_change_after():
    """What every key and stamp held in this process takes (``Masker.stamp``): "" while the terms never changed, so a
    space that syncs nothing keeps 0.11's keys (A-M4); after, the tag and a count of this process's changes, which never
    comes back. The tag alone (``Masker.tag``) names the terms on every instance (the texts kept in Valkey)."""
    live = LiveTerms()
    masker = _masker(Config(ner="off"), live)
    live.use([], "")  # nothing to take: nothing changed
    assert (masker.stamp, masker.tag, _masker(Config(ner="off"), LiveTerms()).stamp) == ("", "", "")
    live.use([KESTREL], "t1")
    first = masker.stamp
    assert first.startswith("t1") and first != "t1" and "|" not in first
    live.use([KESTREL], "t1")  # the same terms under the same tag: nothing changed
    assert masker.stamp == first
    live.use([], "")
    gone = masker.stamp
    live.use([KESTREL], "t1")
    assert masker.tag == "t1" and len({first, gone, masker.stamp}) == 3 and gone != ""
