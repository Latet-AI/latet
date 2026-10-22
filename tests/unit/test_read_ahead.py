"""The local name model reads a JSON document's strings and keys at once, in as few runs as hold them, within a time
for the document; run once for each, it cost a document of a thousand short values minutes. A document it cannot read in
that time is withheld: nothing it would have found leaves in clear. A text masked before is not read again; a value
strict data mode masks whole is read all the same, since what the model finds there is what the vault learns."""

import json
import time
from pathlib import Path

import numpy as np
import pytest

from latet.bridge.api import Bridge
from latet.engine.detectors.base import DetectContext, DetectorSkipped
from latet.engine.detectors.fields import FieldDetector
from latet.engine.detectors.ner import NerDetector, OnnxTokenModel, PastDeadline
from latet.engine.masker import Masker, MaskReport
from latet.engine.pipeline import WITHHELD_UNREAD, Engine
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.engine.verifier import Verifier
from latet.gateway import mediate
from latet.gateway.mediate import Mediator

from ..conftest import ROOT_KEY
from .test_ner import LABELS_PF, Spans, _Tokenizer

EVERYWHERE = (SlotKind.USER_TEXT, SlotKind.TOOL_RESULT, SlotKind.OTHER)
NAME = "Giulia Bianchi"


class _Session:
    """Labels the words "Giulia Bianchi" as a person wherever they fall in a row, its padding apart; records each run's
    rows and length."""

    def __init__(self) -> None:
        self.runs: list[tuple[int, int]] = []
        self.read: list[list[int]] = []  # each run, the tokens each row holds by its mask

    def run(self, _outputs, feed):
        ids, mask = feed["input_ids"], feed["attention_mask"]
        assert mask.shape == ids.shape
        self.runs.append(ids.shape)
        self.read.append([int(row.sum()) for row in mask])
        name = [hash("Giulia") % 1000, hash("Bianchi") % 1000]
        out = np.full((*ids.shape, len(LABELS_PF)), -5.0, dtype=np.float32)
        out[:, :, 0] = 2.0
        for row in range(ids.shape[0]):
            for i in range(int(mask[row].sum()) - 1):
                if [int(ids[row, i]), int(ids[row, i + 1])] == name:
                    out[row, i, LABELS_PF.index("B-private_person")] = 5.0
                    out[row, i + 1, LABELS_PF.index("E-private_person")] = 5.0
        return [out]


def _model() -> tuple[OnnxTokenModel, _Session]:
    """The real model's reading (windows, rows of many texts), over a tokenizer that cuts words and a session that knows
    one name."""
    model = OnnxTokenModel(Path("unused"))
    session = _Session()
    model._session, model._tokenizer, model._labels, model._np = session, _Tokenizer(), LABELS_PF, np
    return model, session


def _mediator(model, strict: bool = False) -> Mediator:
    vault = Vault(ROOT_KEY)
    ner = NerDetector(model, EVERYWHERE, single_names=True, cities=True, max_chars=64_000, max_tokens=8192)
    masker = Masker(vault, [FieldDetector(strict=strict), ner])
    return Mediator(Engine(vault, masker), Verifier(masker))


def _rows(n: int) -> str:
    return json.dumps(
        {"rows": [{"note": f"value number {i} of the tool"} for i in range(n)] + [{"note": f"call {NAME}"}]}
    )


def _masked(mediator: Mediator, text: str, session: str = "s-1"):
    report = MaskReport()
    done = mediator.mask_value(text, SlotKind.TOOL_RESULT, mediator.engine.session(session), report)
    return done, report


# --- the model ---------------------------------------------------------------------------------------------------------


def test_many_short_texts_are_read_as_rows_of_a_few_runs_each_as_alone():
    model, session = _model()
    texts = [f"value number {i} of the tool" for i in range(400)] + [f"call {NAME} back", ""]
    found = model.predict_many(texts, 8192)
    assert session.runs == [(341, 6), (60, 6)]  # the shortest first, padded to the longest: BATCH tokens a run
    assert found[-1] == [] and all(t.label == "O" for tokens in found[:400] for t in tokens or [])
    alone = model.predict(f"call {NAME} back")
    assert found[400] == alone  # the same tokens, at the same places in their own text
    assert found[7] == model.predict("value number 7 of the tool")
    assert session.read[0][:2] == [4, 6]  # each row's padding masked from it: the name's four tokens, a value's six


def test_a_text_over_the_budget_is_not_read_and_one_longer_than_a_window_is_read_alone_in_windows():
    model, session = _model()
    long = "parola " * 1500 + NAME
    found = model.predict_many(["short one", long, "parola " * 9000], 8192)
    assert found[2] is None
    assert [t.label for t in found[1] or [] if t.label != "O"] == ["B-private_person", "E-private_person"]
    assert session.runs == [(1, 1024), (1, 1024), (1, 2)]  # the long one in two windows, the short one a row


def test_the_time_given_is_checked_before_each_run():
    model, session = _model()
    with pytest.raises(PastDeadline):
        model.predict_many([f"value {i}" for i in range(500)], 8192, deadline=0.0)
    with pytest.raises(PastDeadline):
        model.predict_many(["parola " * 1500], 8192, deadline=0.0)
    assert session.runs == []


# --- the detector ------------------------------------------------------------------------------------------------------


def test_what_is_read_ahead_is_what_detect_reads_and_only_while_reading():
    model, session = _model()
    detector = NerDetector(model, EVERYWHERE, max_chars=10**6)
    ctx = DetectContext(SlotKind.TOOL_RESULT)
    with detector.reading():
        assert detector.read_ahead([f"call {NAME}", "parola " * 9000], SlotKind.TOOL_RESULT, float("inf"))
        runs = len(session.runs)
        assert [s.etype for s in detector.detect(f"call {NAME}", ctx)] == ["PERSON"]
        assert len(session.runs) == runs  # read already
        with pytest.raises(DetectorSkipped, match="over the 4096 tokens"):
            list(detector.detect("parola " * 9000, ctx))
    assert detector.read_ahead([f"call {NAME}"], SlotKind.TOOL_RESULT, float("inf"))  # nothing kept outside
    list(detector.detect(f"call {NAME}", ctx))
    assert len(session.runs) == runs + 1


def test_a_model_that_reads_one_text_at_a_time_is_given_the_time_between_two():
    model = Spans((NAME, "private_person"))
    detector = NerDetector(model, EVERYWHERE)
    with detector.reading():
        assert not detector.read_ahead(["a", "b"], SlotKind.TOOL_RESULT, 0.0)
        assert model.calls == 0
        assert detector.read_ahead([f"call {NAME}"], SlotKind.TOOL_RESULT, float("inf")) and model.calls == 1


# --- a document --------------------------------------------------------------------------------------------------------


def test_a_documents_strings_and_keys_are_read_in_a_few_runs():
    model, session = _model()
    mediator = _mediator(model)
    output = json.loads(_rows(300))
    output[NAME] = {"role": "owner"}  # a key that is a name
    done, report = _masked(mediator, json.dumps(output))
    assert NAME not in done.text and not done.withheld and report.degraded == []
    assert len(session.runs) <= 4  # where it was run once for each of 304 strings and keys


def test_a_document_the_model_cannot_read_in_its_time_is_withheld_and_leaves_so_again(monkeypatch):
    monkeypatch.setattr(mediate, "DOCUMENT_READ_SECONDS", 0.0)
    model, session = _model()
    mediator = _mediator(model)
    done, report = _masked(mediator, _rows(300))
    assert done.withheld and done.text.startswith(WITHHELD_UNREAD) and NAME not in done.text
    assert "303 texts" in done.text and report.degraded == ["ner"] and session.runs == []  # 301 notes, 2 keys
    again, _ = _masked(mediator, _rows(300))  # sent again in its conversation: as it left
    assert again.text == done.text


def test_a_document_given_no_time_is_withheld_on_a_clock_that_has_not_moved(monkeypatch):
    """Windows' clock moves in steps of about 16 ms: a time of none is spent on the clock's same step, not only once
    it has moved on."""
    monkeypatch.setattr(mediate, "DOCUMENT_READ_SECONDS", 0.0)
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    model, session = _model()
    done, _ = _masked(_mediator(model), _rows(300))
    assert done.withheld and NAME not in done.text and session.runs == []


def test_a_document_the_plugin_has_masked_past_the_time_is_withheld(monkeypatch):
    monkeypatch.setattr(mediate, "DOCUMENT_READ_SECONDS", 0.0)
    model, _ = _model()
    bridge = Bridge(_mediator(model), "token")
    _, out, _, _, _ = bridge._mask({"session": "s-1", "items": [{"text": _rows(50), "kind": "tool_result"}]})
    assert out[0]["withheld"] is True and out[0]["text"].startswith(WITHHELD_UNREAD)


def test_a_text_masked_before_is_not_read_again():
    model, session = _model()
    mediator = _mediator(model)
    first, _ = _masked(mediator, _rows(100), "s-1")
    runs = len(session.runs)
    again, _ = _masked(mediator, _rows(100), "s-2")  # another conversation: masked anew, from the cache
    assert again.text == first.text and len(session.runs) == runs


def test_a_value_strict_data_mode_masks_is_read_and_its_name_learnt():
    model, session = _model()
    mediator = _mediator(model, strict=True)
    _masked(mediator, json.dumps({"customer": f"{NAME} of Milano"}))
    entity = mediator.engine.vault.by_original(NAME)
    assert entity is not None and entity.etype == "PERSON" and session.runs
    later, _ = _masked(mediator, f"log: {NAME} logged in")
    assert NAME not in later.text and entity.surrogate in later.text
