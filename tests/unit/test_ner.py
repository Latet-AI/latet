import hashlib
import itertools
import re
import threading
from pathlib import Path

import numpy as np
import pytest

from latet.engine.detectors.base import DetectContext, DetectorSkipped
from latet.engine.detectors.ner import (
    ModelSpec,
    NerDetector,
    OnnxTokenModel,
    Token,
    decode_bioes,
    detector_for,
    is_installed,
    model_dir,
    windows,
)
from latet.engine.masker import Masker, MaskReport
from latet.engine.rehydrate import restore_text, restores_for
from latet.engine.types import SlotKind
from latet.engine.verifier import Verifier

ITALIAN = (
    "Ciao, sono Giulia Bianchi e abito in via Garibaldi 12, 20121 Milano. Il mio collega Marco Esposito ti chiama."
)


class Spans:
    """Tokens cut like the real tokenizer's (a word with the space before it), labelled BIOES over phrases.

    The phrases are what Privacy Filter (int8) labelled in these sentences on the development machine.
    """

    def __init__(self, *phrases: tuple[str, str], score: float = 0.95) -> None:
        self.phrases = phrases
        self.score = score
        self.calls = 0

    def predict(self, text: str, max_tokens: int | None = None) -> list[Token]:
        self.calls += 1
        tokens = [(m.start(), m.end()) for m in re.finditer(r"\s*(?:\w+|[^\w\s])", text)]
        labels = ["O"] * len(tokens)
        for phrase, kind in self.phrases:
            for m in re.finditer(re.escape(phrase), text):
                inside = [i for i, (s, e) in enumerate(tokens) if s < m.end() and e > m.start()]
                if len(inside) == 1:
                    labels[inside[0]] = f"S-{kind}"
                    continue
                labels[inside[0]] = f"B-{kind}"
                for i in inside[1:-1]:
                    labels[i] = f"I-{kind}"
                labels[inside[-1]] = f"E-{kind}"
        return [Token(s, e, label, self.score) for (s, e), label in zip(tokens, labels, strict=True)]


PERSON, ADDRESS = "private_person", "private_address"
SEEN = Spans(  # every phrase below, as the real model labelled it
    ("Giulia Bianchi", PERSON), ("Garibaldi 12, 20121 Milano", ADDRESS), ("Marco Esposito", PERSON),
    ("Francesca Romano", PERSON), ("Corso Vittorio Emanuele II", PERSON), ("00186 Roma RM", ADDRESS),
    ("Rossi", PERSON), ("viale Monza 87/B, scala C.", ADDRESS),
    ("Alessandro Conti", PERSON), ("Piazza della Repubblica 5 a Firenze.", ADDRESS),
    ("Herrn Thomas Müller", PERSON), ("Hauptstraße 23, 10115 Berlin", ADDRESS),
    ("Sophie Martin", PERSON), ("12 rue de la Paix, 75002 Paris.", ADDRESS),
    ("Ciao Paolo", PERSON), ("Elena Ferri", PERSON), ("via dei Mille 8 a Napoli", ADDRESS),
    ("elena.ferri@studiolegale.it", "private_email"), ("339 123 4567", "private_phone"),
    ("Oggi Rosa", PERSON), ("Giovanni Mele", PERSON),
    ("3 marzo 1985", "private_date"), ("sk-proj-4f9aQz7LmN2pR8sT1vW3xY5bC6dE0gH", "secret"),
)  # fmt: skip


def _found(detector, text, kind=SlotKind.USER_TEXT):
    return [(text[s.start : s.end], s.etype) for s in detector.detect(text, DetectContext(kind))]


def test_names_and_address_in_italian_prose():
    assert _found(NerDetector(SEEN), ITALIAN) == [
        ("Giulia Bianchi", "PERSON"),
        ("via Garibaldi 12", "STREET_ADDRESS"),  # the street type just before the span joins it
        ("20121", "ZIPCODE"),
        ("Milano", "CITY"),
        ("Marco Esposito", "PERSON"),
    ]


def test_addresses_are_split_into_street_postcode_and_city():
    detector = NerDetector(SEEN)
    assert _found(detector, "Contactez Sophie Martin au 12 rue de la Paix, 75002 Paris.") == [
        ("Sophie Martin", "PERSON"),
        ("12 rue de la Paix", "STREET_ADDRESS"),
        ("75002", "ZIPCODE"),
        ("Paris", "CITY"),
    ]
    assert _found(detector, "Il dott. Alessandro Conti risiede in Piazza della Repubblica 5 a Firenze.") == [
        ("Alessandro Conti", "PERSON"),
        ("Piazza della Repubblica 5", "STREET_ADDRESS"),
        ("Firenze", "CITY"),
    ]
    assert _found(detector, "abita in viale Monza 87/B, scala C.") == [("viale Monza 87/B, scala C", "STREET_ADDRESS")]


def test_a_person_that_starts_with_a_street_type_is_the_street():
    text = "Spedire a: Francesca Romano, Corso Vittorio Emanuele II 154, 00186 Roma RM"
    assert _found(NerDetector(SEEN), text) == [
        ("Francesca Romano", "PERSON"),
        ("Corso Vittorio Emanuele II 154", "STREET_ADDRESS"),  # the number the model left out joins it
        ("00186", "ZIPCODE"),
        ("Roma RM", "CITY"),
    ]


def test_a_street_named_by_the_model_is_masked_and_restored():
    vault = _vault()
    text = "Spedire a: Francesca Romano, Corso Vittorio Emanuele II 154, 00186 Roma RM"
    masked = Masker(vault, [NerDetector(SEEN)]).mask(text, SlotKind.USER_TEXT)
    assert "Vittorio" not in masked.text and "154" not in masked.text and "00186" not in masked.text
    assert masked.text.startswith("Spedire a: ")
    restored, _ = restore_text(masked.text, restores_for(vault.entities()))
    assert restored == text


def test_greetings_and_titles_are_not_part_of_a_name():
    text = "Ciao Paolo, ti inoltro la mail di Elena Ferri (elena.ferri@studiolegale.it, 339 123 4567)."
    assert _found(NerDetector(SEEN), text) == [("Elena Ferri", "PERSON")]  # "Paolo" alone: max only
    assert _found(NerDetector(SEEN, single_names=True), text) == [("Paolo", "PERSON"), ("Elena Ferri", "PERSON")]
    german = "Bitte schicken Sie die Rechnung an Herrn Thomas Müller, Hauptstraße 23, 10115 Berlin."
    assert _found(NerDetector(SEEN), german)[0] == ("Thomas Müller", "PERSON")
    assert _found(NerDetector(SEEN), "Oggi Rosa e Franco sono andati a Bari con Giovanni Mele.") == [
        ("Giovanni Mele", "PERSON")
    ]


def test_german_greetings_and_titles_are_not_part_of_a_name():
    for phrase, name in (
        ("Hallo Paul", "Paul"),  # "Paul" alone: max only
        ("Liebe Anna", "Anna"),
        ("Sehr geehrte Frau Weber", "Weber"),  # dropped a word at a time: "Sehr", "geehrte", "Frau"
        ("Guten Tag Herr Dr. Müller", "Müller"),
    ):
        text = f"{phrase}, anbei die Unterlagen."
        assert _found(NerDetector(Spans((phrase, PERSON)), single_names=True), text) == [(name, "PERSON")], text


def test_french_greetings_and_titles_are_not_part_of_a_name():
    for phrase, name in (
        ("Bonjour Paul", "Paul"),
        ("Chère Marie", "Marie"),
        ("Chere Marie", "Marie"),
        ("Bonsoir Mademoiselle Laurent", "Laurent"),
    ):
        text = f"{phrase}, voici les documents."
        assert _found(NerDetector(Spans((phrase, PERSON)), single_names=True), text) == [(name, "PERSON")], text


def test_spanish_greetings_and_titles_are_not_part_of_a_name():
    for phrase, name in (
        ("Hola Paul", "Paul"),
        ("Querida Ana", "Ana"),
        ("Estimada Sra. López", "López"),
        ("Buenos días Dra. Fernández", "Fernández"),
        ("Buenas tardes senora Ruiz", "Ruiz"),
    ):
        text = f"{phrase}, adjunto los documentos."
        assert _found(NerDetector(Spans((phrase, PERSON)), single_names=True), text) == [(name, "PERSON")], text


def test_other_labels_are_left_to_the_validated_detectors():
    text = "La chiave è sk-proj-4f9aQz7LmN2pR8sT1vW3xY5bC6dE0gH, nato il 3 marzo 1985, mail elena.ferri@studiolegale.it"
    assert _found(NerDetector(SEEN, single_names=True, cities=True), text) == []


def test_a_city_or_postcode_without_a_street_is_masked_only_in_max():
    model = Spans(("20121 Milano", ADDRESS))
    assert _found(NerDetector(model), "Sono a 20121 Milano.") == []
    assert _found(NerDetector(model, single_names=True, cities=True), "Sono a 20121 Milano.") == [
        ("20121", "ZIPCODE"),
        ("Milano", "CITY"),
    ]


def test_product_names_and_tech_words_are_not_people():
    model = Spans(("Claude Code", PERSON), ("Hermes Agent", PERSON), ("Redis", PERSON), ("Postgres", PERSON))
    text = "Use Claude Code with the Hermes Agent profile; see the Redis and Postgres docs."
    assert _found(NerDetector(model, single_names=True), text) == []
    latet = Spans(("Latet", PERSON), ("Latet Enterprise", PERSON))
    assert _found(NerDetector(latet, single_names=True), "Latet masks it; Latet Enterprise shares rules.") == []


def test_low_scores_are_ignored():
    model = Spans(("Giulia Bianchi", PERSON), score=0.3)
    assert _found(NerDetector(model), ITALIAN) == []


def test_runs_only_on_the_configured_kinds_and_size():
    model = Spans(*SEEN.phrases)
    detector = NerDetector(model, max_chars=500)
    assert _found(detector, ITALIAN, SlotKind.TOOL_RESULT) == []
    assert model.calls == 0  # not even run on code and tool output in balanced
    report = MaskReport()
    masker = Masker(_vault(), [detector])
    masked = masker.mask(ITALIAN * 6, SlotKind.USER_TEXT, report=report)  # over max_chars
    assert report.degraded == ["ner"] and masked.text == ITALIAN * 6


def test_requests_never_wait_for_the_model_to_load():
    class Loading(Spans):
        ready = False
        error = None
        started = 0

        def start_loading(self):
            self.started += 1

    model = Loading(*SEEN.phrases)
    report = MaskReport()
    masked = Masker(_vault(), [NerDetector(model)]).mask(ITALIAN, SlotKind.USER_TEXT, report=report)
    assert masked.text == ITALIAN and report.degraded == ["ner"]  # flagged, not delayed
    assert model.started == 1 and model.calls == 0


def test_masked_and_restored_end_to_end():
    vault = _vault()
    masker = Masker(vault, [NerDetector(SEEN)])
    masked = masker.mask(ITALIAN, SlotKind.USER_TEXT)
    for value in ("Giulia Bianchi", "Marco Esposito", "Garibaldi 12", "Milano"):
        assert value not in masked.text
    assert "Ciao, sono " in masked.text  # only the values change
    restored, _ = restore_text(masked.text, restores_for(vault.entities()))
    assert restored == ITALIAN
    # Learnt once, the full name is masked anywhere afterwards, even where the model does not run.
    again = masker.mask("log: user Giulia Bianchi logged in", SlotKind.TOOL_RESULT)
    assert "Giulia Bianchi" not in again.text


def test_verifier_does_not_run_the_model():
    model = Spans(*SEEN.phrases)
    verifier = Verifier(Masker(_vault(), [NerDetector(model)]))
    verifier.verify([(ITALIAN, SlotKind.USER_TEXT, False)], "s")
    assert model.calls == 0


def test_profiles(tmp_path):
    balanced = detector_for(tmp_path, "balanced", SEEN)
    maximal = detector_for(tmp_path, "max", SEEN)
    assert balanced is not None and maximal is not None
    assert SlotKind.TOOL_RESULT not in balanced.kinds and SlotKind.TOOL_RESULT in maximal.kinds
    assert maximal.single_names and not balanced.single_names
    assert detector_for(tmp_path, "balanced") is None  # no model downloaded


# --- download and verification ------------------------------------------------------------------------


@pytest.fixture
def hub():
    """A local stand-in for huggingface.co serving two small files."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    files = {"config.json": b'{"id2label": {"0": "O"}}', "onnx/model_int8.onnx": b"\x08" * 50_000}
    served: list[str] = []

    async def resolve(request):
        path = request.path_params["path"]
        served.append(path)
        return Response(files[path]) if path in files else Response(status_code=404)

    app = Starlette(routes=[Route("/org/model/resolve/rev123/{path:path}", resolve)])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        pass
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", files, served
    server.should_exit = True
    thread.join(5)


def _spec(files, tamper=False):
    hashes = {p: (hashlib.sha256(b).hexdigest() if not tamper else "0" * 64, len(b)) for p, b in files.items()}
    return ModelSpec("test-model", "org/model", "rev123", hashes, "MIT", "https://example.invalid")


def test_install_verifies_and_resumes(tmp_path: Path, hub):
    from latet.models import install, remove

    url, files, served = hub
    spec = _spec(files)
    directory = install(tmp_path, spec, hub=url)
    assert directory == model_dir(tmp_path, spec) and is_installed(tmp_path, spec)
    assert (directory / "onnx" / "model_int8.onnx").read_bytes() == files["onnx/model_int8.onnx"]
    served.clear()
    install(tmp_path, spec, hub=url)
    assert served == []  # already there: nothing downloaded
    assert remove(tmp_path, spec) and not is_installed(tmp_path, spec)


def test_install_rejects_a_tampered_file(tmp_path: Path, hub):
    from latet.models import ModelError, install

    url, files, _ = hub
    with pytest.raises(ModelError, match="checksum"):
        install(tmp_path, _spec(files, tamper=True), hub=url)
    assert not is_installed(tmp_path, _spec(files, tamper=True))


def _vault():
    from latet.engine.vault import Vault

    from ..conftest import ROOT_KEY

    return Vault(ROOT_KEY)


def test_names_rejected_by_newer_filters_are_forgotten_at_start(tmp_path):
    from latet.config import Config, IdentityConfig
    from latet.engine.types import EntityClass
    from latet.runtime import make_runtime

    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=tmp_path, record=False)
    try:
        vault = rt.built.vault
        product = vault.intern("PERSON", EntityClass.NER, "Claude Code")
        person = vault.intern("PERSON", EntityClass.NER, "Giulia Bianchi")
        assert rt.forget_rejected_names() == 1
        assert vault.get(product.id) is None and vault.get(person.id) is not None
    finally:
        rt.close()


def test_the_names_forgotten_at_start_are_forgotten_in_one_go(tmp_path, monkeypatch):
    """A filter that changed can reject hundreds of names at the next start: the store is asked once for all of them
    (and so is every upgrade copy of the vault, opened once), never once a name."""
    from latet.config import Config, IdentityConfig
    from latet.engine.detectors import ner
    from latet.engine.types import EntityClass
    from latet.runtime import make_runtime

    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=tmp_path, record=False)
    try:
        vault = rt.built.vault
        names = [vault.intern("PERSON", EntityClass.NER, f"Rejected Name {n}") for n in range(5)]
        kept = vault.intern("PERSON", EntityClass.NER, "Giulia Bianchi")
        monkeypatch.setattr(ner, "rejected_now", lambda etype, original: original.startswith("Rejected"))
        asked: list[list[str]] = []
        monkeypatch.setattr(rt.store, "forget_entities", lambda ids: asked.append(sorted(ids)))
        monkeypatch.setattr(rt.store, "forget_entity", lambda entity_id: asked.append([entity_id]))
        assert rt.forget_rejected_names() == 5
        assert asked == [sorted(e.id for e in names)] and vault.get(kept.id) is not None
    finally:
        rt.close()


# --- decoding ------------------------------------------------------------------------------------------

LABELS_PF = ["O", "B-private_person", "I-private_person", "E-private_person", "S-private_person",
             "B-private_address", "I-private_address", "E-private_address", "S-private_address"]  # fmt: skip


def _logits(rows: list[dict[str, float]]) -> np.ndarray:
    """One row per token: the labels named get those scores, every other label -5."""
    out = np.full((len(rows), len(LABELS_PF)), -5.0, dtype=np.float32)
    for i, row in enumerate(rows):
        for label, score in row.items():
            out[i, LABELS_PF.index(label)] = score
    return out


def test_bioes_decoding_never_starts_inside_a_span():
    # Argmax would say I-person for the first token: a span cannot begin inside.
    logits = _logits([{"I-private_person": 3.0, "B-private_person": 2.5}, {"E-private_person": 3.0}])
    assert [LABELS_PF[i] for i in decode_bioes(logits, LABELS_PF)] == ["B-private_person", "E-private_person"]


def test_bioes_decoding_closes_a_span_with_its_own_type():
    # After B-person the next token may only be I-person or E-person, never E-address.
    logits = _logits([{"B-private_person": 3.0}, {"E-private_address": 3.0, "E-private_person": 2.0}, {"O": 3.0}])
    assert [LABELS_PF[i] for i in decode_bioes(logits, LABELS_PF)] == ["B-private_person", "E-private_person", "O"]


def test_bioes_decoding_of_plain_text_is_all_outside():
    logits = _logits([{"O": 2.0}] * 4)
    assert decode_bioes(logits, LABELS_PF) == [0, 0, 0, 0]


def test_windows_cover_every_token_and_overlap():
    assert windows(10, 4096, 3584) == [0]
    starts = windows(9000, 4096, 3584)
    assert starts == [0, 3584, 4904]  # the last window ends exactly at the last token
    assert all(b - a <= 3584 for a, b in itertools.pairwise(starts))


class _Encoding:
    def __init__(self, text: str) -> None:
        words = list(re.finditer(r"\s*(?:\w+|[^\w\s])", text))  # a word with the space before it, or a sign
        self.ids = [hash(m.group().strip()) % 1000 for m in words]
        self.offsets = [(m.start(), m.end()) for m in words]


class _Tokenizer:
    def encode(self, text: str, add_special_tokens: bool = False) -> _Encoding:
        assert add_special_tokens is False
        return _Encoding(text)


class _Session:
    """Labels the words "Giulia Bianchi" as a person, wherever they fall; records each window's length."""

    def __init__(self, text: str, label_in_calls: set[int] | None = None) -> None:
        self.text = text
        self.lengths: list[int] = []
        self.label_in_calls = label_in_calls  # None: every window sees the name

    def run(self, _outputs, feed):
        ids = feed["input_ids"][0]
        assert feed["attention_mask"].shape == feed["input_ids"].shape
        call = len(self.lengths)
        self.lengths.append(len(ids))
        name = [hash("Giulia") % 1000, hash("Bianchi") % 1000]
        out = np.full((1, len(ids), len(LABELS_PF)), -5.0, dtype=np.float32)
        out[0, :, 0] = 2.0
        if self.label_in_calls is not None and call not in self.label_in_calls:
            return [out]
        for i in range(len(ids) - 1):
            if [int(ids[i]), int(ids[i + 1])] == name:
                out[0, i, LABELS_PF.index("B-private_person")] = 5.0
                out[0, i + 1, LABELS_PF.index("E-private_person")] = 5.0
        return [out]


def _wrapped(text: str, label_in_calls: set[int] | None = None) -> tuple[OnnxTokenModel, _Session]:
    model = OnnxTokenModel(Path("unused"))
    session = _Session(text, label_in_calls)
    model._session, model._tokenizer, model._labels, model._np = session, _Tokenizer(), LABELS_PF, np
    return model, session


def test_a_name_in_a_long_text_is_found_once_and_whole():
    filler = "parola " * 3580  # several windows, each of which sees the name whole
    text = filler + "Giulia Bianchi " + "parola " * 2000
    model, session = _wrapped(text)
    tokens = [t for t in model.predict(text) if t.label != "O"]
    assert [text[t.start : t.end].strip() for t in tokens] == ["Giulia", "Bianchi"]
    assert [t.label for t in tokens] == ["B-private_person", "E-private_person"]
    assert len(session.lengths) > 1 and max(session.lengths) <= OnnxTokenModel.WINDOW


def test_short_text_is_one_window():
    model, session = _wrapped("Ciao, sono Giulia Bianchi.")
    assert [t.label for t in model.predict("Ciao, sono Giulia Bianchi.") if t.label != "O"] == [
        "B-private_person",
        "E-private_person",
    ]
    assert session.lengths == [6]  # "Ciao" "," " sono" " Giulia" " Bianchi" "."


class Pieces:
    """Exactly these tokens, in order, over the text: (piece, label)."""

    def __init__(self, *pieces: tuple[str, str]) -> None:
        self.pieces = pieces

    def predict(self, text: str, max_tokens: int | None = None) -> list[Token]:
        out, pos = [], 0
        for piece, label in self.pieces:
            start = text.index(piece, pos)
            out.append(Token(start, start + len(piece), label, 0.95))
            pos = start + len(piece)
        return out


def test_a_quote_around_a_name_is_left_out_of_it():
    """Node's console.table writes a string between quotes ('Francesca Solieri'), and the model may take the opening
    one into the name: no name begins with an apostrophe, so the fake never leaves a quote behind it."""
    text = "│ 0 │ 'Francesca Solieri' │ and Luca D'Amico' wrote to “Anna Rossi” and «Marco Neri»"
    model = Pieces(
        ("'Francesca", "B-private_person"), (" Solieri'", "E-private_person"),
        (" Luca", "B-private_person"), (" D'Amico'", "E-private_person"),
        (" “Anna", "B-private_person"), (" Rossi”", "E-private_person"),
        (" «Marco", "B-private_person"), (" Neri»", "E-private_person"),
    )  # fmt: skip
    assert _found(NerDetector(model), text) == [
        ("Francesca Solieri", "PERSON"),
        ("Luca D'Amico", "PERSON"),  # an apostrophe inside a name stays
        ("Anna Rossi", "PERSON"),
        ("Marco Neri", "PERSON"),
    ]


def test_a_text_over_the_token_budget_is_skipped_not_read():
    text = "parola " * 5000
    model, session = _wrapped(text)
    detector = NerDetector(model, max_chars=10**6, max_tokens=4096)
    with pytest.raises(DetectorSkipped, match="tokens"):
        list(detector.detect(text, DetectContext(SlotKind.USER_TEXT)))
    assert session.lengths == []  # not one window run


def test_profiles_bound_the_tokens_read(tmp_path):
    balanced, maximal = detector_for(tmp_path, "balanced", SEEN), detector_for(tmp_path, "max", SEEN)
    assert balanced is not None and maximal is not None
    assert balanced.max_tokens == 4096 and maximal.max_tokens == 8192


def test_long_texts_are_read_in_windows_of_1024_tokens():
    text = "parola " * 3000
    model, session = _wrapped(text)
    model.predict(text)
    assert session.lengths == [1024, 1024, 1024, 1024]  # starts 0, 768, 1536, 1976


def test_a_name_one_window_sees_is_kept_where_the_next_window_does_not():
    # 1,500 tokens, the name at 749-750: where the second window becomes the more central one.
    text = "parola " * 749 + "Giulia Bianchi " + "parola " * 749
    for seen_by in ({0}, {1}):
        model, _ = _wrapped(text, label_in_calls=seen_by)
        assert [t.label for t in model.predict(text) if t.label != "O"] == ["B-private_person", "E-private_person"]


def test_a_sign_the_tokenizer_joins_to_a_name_is_not_part_of_it():
    # Privacy Filter's tokenizer reads "=Mario" as "=M" + "ario", "(Mario" as "(M" + "ario".
    for text, first in (
        ("autore=Mario Rossi", "=M"),
        ("Partecipanti: (Mario Rossi)", "(M"),
        ("testo.Mario Rossi", ".M"),
    ):
        model = Pieces((first, "B-private_person"), ("ario", "I-private_person"), (" Rossi", "E-private_person"))
        assert _found(NerDetector(model), text) == [("Mario Rossi", "PERSON")], text


def test_house_numbers_of_four_or_five_digits_are_not_postcodes():
    for text, address in (
        (
            "Send it to 1600 Amphitheatre Parkway, Mountain View, CA 94043",
            "1600 Amphitheatre Parkway, Mountain View, CA 94043",
        ),
        ("He lives at 4521 Oak Ridge Drive.", "4521 Oak Ridge Drive"),
        ("Ship to 1234 Pacific Highway, Sydney NSW 2000", "1234 Pacific Highway, Sydney NSW 2000"),
    ):
        assert _found(NerDetector(Spans((address, ADDRESS))), text) == [(address, "STREET_ADDRESS")], text


def test_a_street_named_with_in_is_not_split_at_it():
    text = "Abito in via in Selci 84 a Roma."
    assert _found(NerDetector(Spans(("via in Selci 84 a Roma", ADDRESS))), text) == [
        ("via in Selci 84", "STREET_ADDRESS"),
        ("Roma", "CITY"),
    ]


def test_a_city_alone_is_masked_only_in_max():
    model = Spans(("Milano", ADDRESS))
    assert _found(NerDetector(model), "Sono a Milano.") == []
    assert _found(NerDetector(model, single_names=True, cities=True), "Sono a Milano.") == [("Milano", "CITY")]


def test_a_title_or_a_name_that_is_also_a_street_word_stays_a_person():
    model = Spans(("Dr. Thomas Müller", PERSON), ("Lane Johnson", PERSON))
    assert _found(NerDetector(model), "Please forward it to Dr. Thomas Müller tomorrow.") == [
        ("Thomas Müller", "PERSON")
    ]
    assert _found(NerDetector(model), "Lane Johnson called.") == [("Lane Johnson", "PERSON")]
