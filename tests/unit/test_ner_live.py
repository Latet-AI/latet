"""The real local model, when it is on this machine (``latet ner install``, or LATET_NER_MODEL_DIR).

Never in CI: the model is a 1.6 GB download.
"""

import os
import time
from pathlib import Path

import platformdirs
import pytest

from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.ner import NerDetector, OnnxTokenModel, model_dir, runtime_available
from latet.engine.types import SlotKind

pytestmark = pytest.mark.live


def _directory() -> Path | None:
    configured = os.environ.get("LATET_NER_MODEL_DIR")
    directory = (
        Path(configured) if configured else model_dir(Path(platformdirs.user_data_dir("latet", appauthor=False)))
    )
    return directory if (directory / "onnx" / "model_quantized.onnx").is_file() else None


@pytest.fixture(scope="module")
def detector():
    directory = _directory()
    if directory is None or not runtime_available():
        pytest.skip("local NER model not installed")
    model = OnnxTokenModel(directory)
    assert model.wait_ready(), model.error
    return NerDetector(model)


def _found(detector, text):
    return {(text[s.start : s.end], s.etype) for s in detector.detect(text, DetectContext(SlotKind.USER_TEXT))}


def test_italian_names_and_address(detector):
    text = "Ciao, sono Giulia Bianchi e abito in via Garibaldi 12, 20121 Milano. Il collega Marco Esposito ti chiama."
    found = _found(detector, text)
    assert {("Giulia Bianchi", "PERSON"), ("Marco Esposito", "PERSON")} <= found
    assert any(etype == "STREET_ADDRESS" and "Garibaldi" in value for value, etype in found)


def test_english_names(detector):
    found = _found(detector, "Please send the contract to John Carter and Mary Johnson before Friday.")
    assert ("Mary Johnson", "PERSON") in found


def test_technical_prose_is_left_alone(detector):
    assert _found(detector, "The function returns a Promise; see the Redis and Postgres docs for the Worker.") == set()


def test_long_text_is_read_in_windows(detector):
    filler = "Il servizio risponde in pochi millisecondi e registra ogni richiesta nel log di sistema. " * 150
    text = filler + "Per domande scrivete a Francesca Romano, responsabile del progetto. " + filler
    wide = NerDetector(detector.model, max_chars=64_000, max_tokens=8192)  # several windows: max's limits
    assert ("Francesca Romano", "PERSON") in _found(wide, text)


def test_conversational_italian(detector):
    text = "Mandami il report entro venerdì, poi lo giro a Chiara e a Luca De Santis."
    assert ("Luca De Santis", "PERSON") in _found(detector, text)
    text = "Ho parlato con la signora Rossi del terzo piano: abita in viale Monza 87/B, scala C."
    assert any(t == "STREET_ADDRESS" and v.startswith("viale Monza 87/B") for v, t in _found(detector, text))


def test_greetings_are_not_names(detector):
    found = _found(detector, "Ciao Paolo, ti inoltro la mail di Elena Ferri: lunedì passa in via dei Mille 8 a Napoli.")
    assert ("Elena Ferri", "PERSON") in found and not any(v.startswith("Ciao") for v, _ in found)
    assert ("via dei Mille 8", "STREET_ADDRESS") in found and ("Napoli", "CITY") in found


def test_emoji_and_accents_before_a_name(detector):
    text = "Ciao 😀, è arrivata la mail di Giulia Bianchi ieri."
    assert ("Giulia Bianchi", "PERSON") in _found(detector, text)


def test_a_long_tool_result_in_max_is_read_or_skipped_quickly(detector):
    """What is measured is the CPU time of this process, not the clock. The model computes in this process's own
    threads, so its CPU time is what the reading costs; the clock adds the time other programs keep those threads
    waiting, and that has no limit. On the computer this was measured on, with the model's four threads, the reading
    costs 46 to 48 seconds of CPU and 12 by the clock when nothing else runs; 68 to 74 of CPU and 27 by the clock
    with every core busy with other programs; 77 of CPU and 51 by the clock with twice as many busy programs as
    cores. The limit leaves room for that, and is still a reading that ends within half a minute on four free
    threads: a model that read the text in one window, or read past its limit, would cost several times as much."""
    from latet.engine.detectors.base import DetectorSkipped
    from latet.engine.detectors.ner import detector_for

    maximal = detector_for(Path("unused"), "max", detector.model)
    assert maximal is not None
    lock = (Path(__file__).parents[2] / "uv.lock").read_text("utf-8")
    started = time.process_time()
    with pytest.raises(DetectorSkipped, match="tokens"):  # 63,000 characters of a lock file: ~30,000 tokens
        list(maximal.detect(lock[:63_000], DetectContext(SlotKind.TOOL_RESULT)))
    assert time.process_time() - started < 3  # skipped after tokenizing (a fifth of a second of CPU), never read
    started, clock = time.process_time(), time.monotonic()
    list(maximal.detect(lock[:16_000], DetectContext(SlotKind.TOOL_RESULT)))  # just under 8,192 tokens
    cpu, elapsed = time.process_time() - started, time.monotonic() - clock
    print(f"16,000 characters of uv.lock in {cpu:.1f} s of CPU ({elapsed:.1f} s by the clock)")
    assert cpu < 120


TEXTS = [
    "Ciao, sono Giulia Bianchi e abito in via Garibaldi 12, 20121 Milano. Il collega Marco Esposito ti chiama.",
    "value number 3 of the tool",
    "Please send the contract to John Carter and Mary Johnson before Friday.",
    "status: closed",
    "Ho parlato con la signora Rossi del terzo piano: abita in viale Monza 87/B, scala C.",
    "Mandami il report entro venerd\N{LATIN SMALL LETTER I WITH GRAVE}, poi lo giro a Chiara e a Luca De Santis.",
    "The function returns a Promise; see the Redis and Postgres docs for the Worker.",
]


def test_texts_read_at_once_find_what_each_finds_read_alone(detector):
    """A document's texts read in one run, each a row masked from the others, give each what reading it alone gives
    (texts read one after another in one row, a paragraph's end between two, gave less: the model reads around)."""
    wide = NerDetector(detector.model, single_names=True, cities=True, max_chars=64_000, max_tokens=8192)
    alone = [_found(wide, text) for text in TEXTS]
    with wide.reading():
        assert wide.read_ahead(TEXTS, SlotKind.USER_TEXT, float("inf"))
        at_once = [_found(wide, text) for text in TEXTS]
    assert at_once == alone
