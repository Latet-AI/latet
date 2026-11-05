"""scripts/vault_fuzz.py (the server performance plan's S10): an API key's vault, its values in a store and none of
them in memory, masks every fuzz text where a plain vault holding them masks it, and restores what that one restores,
in every configuration, as the gateway masks and as an applications call masks."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import vault_fuzz  # noqa: E402

from latet.engine.stored import WHOLE_VALUES  # noqa: E402

TEXTS = 200
# A stored vault may mask more, and a call read joined more than its texts alone; and what was counted. With memory 0, a
# value no word finds glued on both sides let go is counted apart (``let-go``), as an API key lets it go (the plan's S4).
_MAY_DIFFER = ("texts", "calls", "more", "joined-more", "read-texts", "let-go")


@pytest.mark.parametrize("glued", [False, True], ids=["memory 0", "memory 0, glued all"])
@pytest.mark.parametrize("path", ["gateway", "apps"])
@pytest.mark.parametrize("config", list(vault_fuzz.CONFIGS))
def test_a_stored_vault_masks_and_restores_as_a_plain_one(config, path, glued):
    """With memory 0, and with every value no word finds glued on both sides held whatever its age besides (``glued_all``,
    a person's space: the plan's S4), which then lets none go."""
    counts, cases = vault_fuzz.run_config(
        config, vault_fuzz.fuzz_texts(TEXTS), path=path, memory=0, new_store=vault_fuzz.MemoryStore, glued=glued
    )
    assert counts["texts"] == TEXTS
    assert {name: n for name, n in counts.items() if name not in _MAY_DIFFER and n}.keys() == set(), cases[:3]
    assert not glued or counts["let-go"] == 0


@pytest.mark.parametrize("whole", [WHOLE_VALUES, vault_fuzz.CROSS], ids=["held whole", "held whole until it grows"])
@pytest.mark.parametrize("path", ["gateway", "apps"])
@pytest.mark.parametrize("config", list(vault_fuzz.CONFIGS))
def test_a_persons_vault_held_whole_masks_and_restores_as_a_plain_one(config, path, whole, monkeypatch):
    """A person's vault held whole (``--whole``, the owner's ruling of 2026-10-31): below the bound, its texts read from
    memory alone; at ``CROSS``, the values each store holds as its vault is made, held whole as it is made and on its
    store from the first value it keeps, a call begun whole reading memory alone to its end."""
    monkeypatch.setattr(vault_fuzz, "WHOLE_AT_MOST", whole)
    counts, cases = vault_fuzz.run_config(
        config, vault_fuzz.fuzz_texts(TEXTS), path=path, memory=0, new_store=vault_fuzz.MemoryStore, glued=True
    )
    assert counts["texts"] == TEXTS
    assert {name: n for name, n in counts.items() if name not in _MAY_DIFFER and n}.keys() == set(), cases[:3]
    assert counts["let-go"] == 0


def test_a_value_glued_on_both_sides_is_masked_and_restored_even_after_eviction():
    """Interior edge lookup finds the cold long token too; masking and restoration need no let-go exception."""
    text = f"ref x{vault_fuzz.LONG_TOKEN}y end"
    cold, cases = vault_fuzz.run_config("none", [text], memory=0, new_store=vault_fuzz.MemoryStore)
    kept, _ = vault_fuzz.run_config("none", [text], memory=0, new_store=vault_fuzz.MemoryStore, glued=True)
    in_memory, _ = vault_fuzz.run_config("none", [text], memory=1000, new_store=vault_fuzz.MemoryStore)
    assert not cases and cold == kept == in_memory
    assert vault_fuzz.clean(cold) and cold["let-go"] == 0
    plain = vault_fuzz.run_plain(text, {}, ())
    key = vault_fuzz.run_key(vault_fuzz.MemoryStore(), text, {}, (), as_read=plain[4], memory=0)
    assert key[:6] == plain
    assert key[0] == list(range(5, 45)) and key[1:3] == (True, True)
    assert key[6:8] == (text, text)


def test_a_loss_is_let_go_only_where_a_value_glued_on_both_sides_stands():
    """The review of Task 7: ``let-go`` excuses what a key's vault whose memory holds none masks or restores less only
    where a value (or its fake) no word finds glued on both sides stands so, letters or digits on both its sides (each
    run of positions masked less inside its ASCII word and overlapping it: the group it names); any other loss counts
    as less or lost and fails the run."""
    from latet.engine.vault import Entity

    token = Entity("e1", "API_KEY", vault_fuzz.EntityClass.SECRET, vault_fuzz.LONG_TOKEN, "f" * 40, 0, 0.0, None)
    text = f"ref x{vault_fuzz.LONG_TOKEN}y mail mario.rossi@acme.it end"
    glued = (text.index(vault_fuzz.LONG_TOKEN), text.index(vault_fuzz.LONG_TOKEN) + 40)
    mail = (text.index("mario.rossi@acme.it"), text.index(" end"))
    masked_all = list(range(*glued)) + list(range(*mail))
    plain = (masked_all, True, True, "", "", "")

    def key(masked: list[int], back: str = text) -> tuple:
        return (masked, back == text, back == text, "", "", "", back, back, [token])

    cases: list[dict] = []
    for masked, back, expected in (
        (list(range(*mail)), text, "let-go"),  # the token glued on both sides alone in clear
        (list(range(*glued)), text, "less"),  # the address in clear: no excuse
        ([], text, "less"),  # both: the address is not excused by the token beside it
        (masked_all, text.replace(vault_fuzz.LONG_TOKEN, "f" * 40), "let-go"),  # its fake glued on both sides, kept
        (masked_all, text.replace("mario", "maria"), "restore-lost"),  # a restore lost elsewhere
    ):
        counts = dict.fromkeys(vault_fuzz.COUNTERS, 0)
        vault_fuzz._tally(counts, cases, text, plain, key(masked, back), memory_none=True)
        assert {name for name in ("let-go", "less", "restore-lost") if counts[name]} == {expected}, (masked, back)
        if expected != "let-go":
            assert not vault_fuzz.clean(counts)
    counts = dict.fromkeys(vault_fuzz.COUNTERS, 0)
    vault_fuzz._tally(counts, cases, text, plain, key(list(range(*mail))), memory_none=False)
    assert counts["less"] == 1 and counts["let-go"] == 0  # with memory: never excused
    # A text that holds the fake itself, glued so (the fuzz's pieces hold old fakes): left as it is, and only the
    # occurrence the restore lost is the token's.
    held = f"{text} q{'f' * 40}z"
    counts = dict.fromkeys(vault_fuzz.COUNTERS, 0)
    lost = held.replace(vault_fuzz.LONG_TOKEN, "f" * 40)
    lost_key = (masked_all, False, False, "", "", "", lost, lost, [token])
    vault_fuzz._tally(counts, cases, held, plain, lost_key, memory_none=True)
    assert counts["let-go"] == 1 and vault_fuzz.clean(counts)


def _counts_for_loss(text, entities, positions, mode):
    """Feed the classifier a masking loss or a restore leaving the named entities' fakes behind."""
    back = text
    for entity in entities:
        back = back.replace(entity.original, entity.surrogate)
    plain = (positions, True, True, "", "", "")
    key = (
        [] if mode == "mask" else positions,
        mode != "restore",
        mode != "json",
        "",
        "",
        "",
        back if mode == "restore" else text,
        back if mode == "json" else text,
        entities,
    )
    counts = dict.fromkeys(vault_fuzz.COUNTERS, 0)
    vault_fuzz._tally(counts, [], text, plain, key, memory_none=True)
    return counts


@pytest.mark.parametrize("length", [7, 16])
@pytest.mark.parametrize(
    ("mode", "counter"), [("mask", "less"), ("restore", "restore-lost"), ("json", "json-restore-lost")]
)
def test_let_go_does_not_excuse_a_secret_held_even_without_working_memory(length, mode, counter):
    from latet.engine.vault import Entity

    entity = Entity("held", "PASSWORD", vault_fuzz.EntityClass.SECRET, "a" * length, "f" * 40, 0, 0.0, None)
    text = f"x{entity.original}y"
    counts = _counts_for_loss(text, [entity], list(range(1, length + 1)), mode)
    assert counts["let-go"] == 0
    assert counts[counter] == 1 and not vault_fuzz.clean(counts)


@pytest.mark.parametrize("boundary", ["_", "-", ".", " ", "é"])
@pytest.mark.parametrize("side", ["left", "right"])
def test_let_go_does_not_excuse_a_contiguous_loss_past_the_tokens_ascii_word(boundary, side):
    from latet.engine.vault import Entity

    entity = Entity("long", "API_KEY", vault_fuzz.EntityClass.SECRET, vault_fuzz.LONG_TOKEN, "f" * 40, 0, 0.0, None)
    word = f"x{entity.original}y"
    text = f"private{boundary}{word}" if side == "left" else f"{word}{boundary}private"
    counts = _counts_for_loss(text, [entity], list(range(len(text))), "mask")
    assert counts["let-go"] == 0
    assert counts["less"] == 1 and not vault_fuzz.clean(counts)


@pytest.mark.parametrize("second", [vault_fuzz.LONG_TOKEN, "b" * 40], ids=["same-token", "two-tokens"])
@pytest.mark.parametrize("mode", ["mask", "restore", "json"])
def test_let_go_keeps_legitimate_losses_of_two_glued_occurrences(second, mode):
    from latet.engine.vault import Entity

    first = vault_fuzz.LONG_TOKEN
    values = list(dict.fromkeys([first, second]))
    entities = [
        Entity(str(i), "API_KEY", vault_fuzz.EntityClass.SECRET, value, chr(102 + i) * 40, 0, 0.0, None)
        for i, value in enumerate(values)
    ]
    text = f"x{first}y q{second}z"
    positions = list(range(42)) + list(range(43, 85))
    counts = _counts_for_loss(text, entities, positions, mode)
    assert counts["let-go"] == 1 and vault_fuzz.clean(counts)


@pytest.mark.parametrize("position", [0, 41])
def test_let_go_does_not_excuse_a_loss_beside_but_not_overlapping_the_token(position):
    from latet.engine.vault import Entity

    entity = Entity("long", "API_KEY", vault_fuzz.EntityClass.SECRET, vault_fuzz.LONG_TOKEN, "f" * 40, 0, 0.0, None)
    text = f"x{entity.original}y"
    counts = _counts_for_loss(text, [entity], [position], "mask")
    assert counts["let-go"] == 0
    assert counts["less"] == 1 and not vault_fuzz.clean(counts)


@pytest.mark.parametrize("separator", ["_", "-", ".", " ", "\n", "é", "中"])
@pytest.mark.parametrize(
    ("mode", "counter"), [("mask", "less"), ("restore", "restore-lost"), ("json", "json-restore-lost")]
)
def test_let_go_rejects_an_internal_run_boundary_despite_repeated_anchors(separator, mode, counter):
    from latet.engine.vault import Entity

    repeated = "a" * 17 + separator + "a" * 17
    original = repeated if mode == "mask" else vault_fuzz.LONG_TOKEN
    fake = "f" * 40 if mode == "mask" else repeated
    entity = Entity("repeated", "API_KEY", vault_fuzz.EntityClass.SECRET, original, fake, 0, 0.0, None)
    # Equal anchors at both ends satisfy glued_both even though the occurrence crosses a run boundary.
    assert vault_fuzz.glued_both(entity, "original" if mode == "mask" else "surrogate")
    assert not vault_fuzz.held_glued(entity)
    text = f"x{original}y"
    counts = _counts_for_loss(text, [entity], list(range(len(text))), mode)
    assert counts["let-go"] == 0
    assert counts[counter] == 1 and not vault_fuzz.clean(counts)


def test_the_apps_path_masks_calls_of_many_texts_as_the_applications_api_does(monkeypatch):
    """``--path apps`` masks the texts in calls of one to ``CALL_MOST`` (``Apps._mask``: joined, then ``settle``), the
    kinds in turn, the texts with a line break among the others; each call also masked with its texts each alone, by
    the plain vault, against which the call read joined masks no less and restores what it restores."""
    from latet.engine.masker import Masker

    joined: list[int] = []
    real = Masker._read_joined

    def reading(self, texts, kind):
        joined.append(len(texts))
        return real(self, texts, kind)

    monkeypatch.setattr(Masker, "_read_joined", reading)
    texts = vault_fuzz.fuzz_texts(TEXTS)
    counts, cases = vault_fuzz.run_config("none", texts, path="apps", memory=0, new_store=vault_fuzz.MemoryStore)
    calls = vault_fuzz.calls_of(texts)
    assert counts["texts"] == TEXTS and counts["calls"] == len(calls) and 1 < len(calls) < TEXTS
    assert max(map(len, calls)) <= vault_fuzz.CALL_MOST and any("\n" in t for call in calls for t in call)
    assert joined and max(joined) > 2  # parts of several texts read joined
    assert counts["joined-less"] == counts["joined-restore-lost"] == 0, cases[:3]


def test_a_call_read_joined_that_fails_is_counted_against_the_run(monkeypatch):
    """The plain vault's call read joined is the code under test on the apps path: its failure counts as an error of
    every text of the call (the run is not clean), where the plain vault's own failure with each text alone (0.10.0's
    way) is a case of its own, not counted."""
    real = vault_fuzz._apps_call

    def failing(vault, texts, kind, policies, allowed, terms, joined):
        if joined and isinstance(vault, vault_fuzz.Vault) and not isinstance(vault, vault_fuzz.StoredVault):
            raise RuntimeError("the joined reading failed")
        return real(vault, texts, kind, policies, allowed, terms, joined)

    monkeypatch.setattr(vault_fuzz, "_apps_call", failing)
    texts = vault_fuzz.fuzz_texts(30)
    counts, cases = vault_fuzz.run_config("none", texts, path="apps", memory=0, new_store=vault_fuzz.MemoryStore)
    assert counts["errors"] == len(texts) and not vault_fuzz.clean(counts)
    assert all("joined-error" in case for case in cases)


def test_a_run_that_ends_without_its_summary_is_not_clean_whatever_an_earlier_run_left(tmp_path, monkeypatch):
    """A run whose process exits 1 (an error before its counts, PostgreSQL down) without writing its summary fails
    ``all``, even where an earlier ``all`` into the same folder left a clean summary of that run."""
    crashing = tmp_path / "scripts"
    crashing.mkdir()
    (crashing / "vault_fuzz.py").write_text("import sys\nsys.exit(1)\n", "utf-8")
    monkeypatch.setattr(vault_fuzz, "HERE", crashing)
    out = tmp_path / "out"
    out.mkdir()
    clean = dict.fromkeys(vault_fuzz.COUNTERS, 0)
    for path in vault_fuzz.PATHS:
        for memory in vault_fuzz.MEMORIES:
            for config in vault_fuzz.CONFIGS:
                left = out / f"{path}-m{memory}-{config}.json"
                left.write_text(json.dumps({"summary": {config: {**clean, "texts": 1}}}), "utf-8")
    assert vault_fuzz.main(["all", "--out", str(out), "--count", "1", "--jobs", "32"]) == 1
    runs = json.loads((out / "summary.json").read_text("utf-8"))
    assert len(runs) == 32
    assert all(run["failed"].startswith("no summary") for run in runs.values()), runs
