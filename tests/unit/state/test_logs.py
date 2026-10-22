"""What a failure shows in the daemon's log, and in a server's: the type of its error, never the error's message or its
traceback, which an error raised while a text or an image is read can fill with what it was reading; nor the folder of
a project (nor its id, a hash of it), nor what a recognizer is made of. (The plugin's answer, the same way:
test_bridge.py; every logging call of Latet's is read for it: tests/unit/test_logging_calls.py.)"""

import errno
import io
import logging
import os
import sqlite3
from types import SimpleNamespace

from starlette.requests import Request

from latet.api.rest import Dashboard
from latet.config import Config, IdentityConfig, LlmReview
from latet.engine.build import build
from latet.engine.detectors.ner import NerDetector
from latet.engine.detectors.recognizers import compile_all
from latet.engine.masker import Masker, MaskReport
from latet.engine.reviewer import Answer, Finding
from latet.engine.reviewer.reviewer import Reviewer, _Batch
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.gateway.mediate import Masked, Mediator, ReviewItem
from latet.gateway.recorder import Recorder
from latet.integrations import routes
from latet.projects import PROJECT_FILE, Projects, project_id
from latet.protocols.base import ImageRef
from latet.runtime import make_runtime
from latet.storage.exchanges import ExchangeStore
from latet.vision.masking import WITHHELD, ImageMasking

from ...conftest import ROOT_KEY
from ...fake_ocr import FakeOcr
from .conftest import EMAIL, PROMPT, _until


class Misreading:
    """An optional detector that fails as a library does: its error quotes the text it was reading."""

    id, mandatory = "misread", False

    def detect(self, text, ctx):
        raise ValueError(f"invalid literal for int() with base 10: {text!r}")


def _logged(caplog, logger):
    """The one record ``logger`` wrote, which carries no traceback; and nothing the logs hold quotes the address."""
    [record] = [r for r in caplog.records if r.name == logger]
    assert record.exc_info is None and EMAIL not in caplog.text
    return record.levelno, record.getMessage()


def test_an_optional_detector_that_fails_is_logged_by_the_type_of_its_error(caplog):
    masker, report = Masker(Vault(ROOT_KEY), [Misreading()]), MaskReport()
    assert masker.mask(PROMPT, report=report).text == PROMPT and report.degraded == ["misread"]  # said, not hidden
    assert _logged(caplog, "latet.engine.masker") == (logging.WARNING, "optional detector misread failed: ValueError")


class AnswersAtOnce:
    """A local reviewer whose background review of a text is answered at once."""

    def __init__(self, answer):
        self.answer = answer

    def submit(self, view, original, done, guidance, picture):
        done(self.answer)


async def test_a_background_review_that_cannot_be_applied_is_logged_by_the_type_of_its_error(caplog):
    def refuse(entity):  # the store fails on the address the reviewer found, and its error quotes it
        if entity.etype == "EMAIL":
            raise sqlite3.OperationalError(f"near {entity.original!r}: syntax error")

    built = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY, on_insert=refuse))
    mediator = Mediator(built.engine, built.verifier)
    try:
        mediator.reviewer = AnswersAtOnce(Answer([Finding(EMAIL, "email")]))
        view = mediator.review_view(PROMPT, built.masker)
        item = ReviewItem(0, PROMPT, SlotKind.USER_TEXT, None, None, False, view, False, PROMPT)
        mediator.submit_background(built.engine.session("conv-1"), LlmReview(enabled=True), [item])
        await _until(lambda: [r for r in caplog.records if r.name == "latet.gateway.mediate"])
    finally:
        mediator.close()
    failure = "a background review could not be applied: OperationalError"
    assert _logged(caplog, "latet.gateway.mediate") == (logging.ERROR, failure)
    assert built.vault.by_original(EMAIL) is None  # not learned: the next review of a text that holds it finds it again


def _image_showing(reader, lines, blocks=None):
    """An image that shows ``lines``, as a request carries it: its reference, and the image's base64. With ``blocks``
    the request has a place for a notice, should the image be withheld."""
    picture = reader.screenshot(lines)
    holder = {"data": picture, "media_type": "image/png"}
    notice = (lambda text: {"type": "text", "text": text}) if blocks is not None else None
    return ImageRef(holder, "data", "base64", "image/png", SlotKind.TOOL_RESULT, 0, None, blocks, 0, notice), picture


def test_an_image_that_cannot_be_painted_is_withheld_and_logged_by_the_type_of_its_error(masker, monkeypatch, caplog):
    reader = FakeOcr()
    service = ImageMasking(reader)
    ref, picture = _image_showing(reader, [f"Email: {EMAIL}"])
    work = service.work(ref)

    def fail(work, replacements):  # a library's error, quoting what it was drawing over
        raise ValueError(f"cannot draw over {replacements[0].original!r}")

    monkeypatch.setattr(service, "paint", fail)
    done = masker.mask(work.text, SlotKind.IMAGE_TEXT)
    assert [r.original for r in done.replacements] == [EMAIL]
    work.masked("text", done.text, done.replacements)
    assert (work.state, work.reason) == (WITHHELD, "Latet could not mask it")
    assert ref.data() != picture  # never as it was
    assert _logged(caplog, "latet.vision.masking") == (logging.ERROR, "could not paint an image: ValueError")


def test_a_decision_on_an_image_that_cannot_be_kept_is_logged_by_the_type_of_its_error(caplog):
    class Full:
        def load_image_decision(self, key):
            return None

        def save_image_decision(self, key, decision):  # a store's error, quoting what it could not write
            raise sqlite3.OperationalError(f"disk full while writing {decision!r} for the image of {EMAIL}")

    service = ImageMasking(FakeOcr(), Full())
    service.remember("f" * 64, [(0, 0, 10, 10)], False)
    assert service.decided("f" * 64) == {"rects": [[0, 0, 10, 10]], "withhold": False}  # it still applies in this run
    failure = "could not keep a decision on an image: OperationalError"
    assert _logged(caplog, "latet.vision.masking") == (logging.ERROR, failure)


def test_an_image_that_cannot_be_read_is_withheld_with_the_type_of_the_error_in_the_log_and_in_its_notice(caplog):
    reader, blocks = FakeOcr(), [{"type": "image"}]
    ref, _ = _image_showing(reader, [f"Email: {EMAIL}"], blocks)

    def fail(image):  # a library's error, quoting the line it was reading
        raise ValueError(f"could not broadcast the line {EMAIL!r} into shape (48, 320)")

    reader.read = fail
    work = ImageMasking(reader).work(ref)
    # Why it was withheld is what takes the image's place in the request, and what the dashboard shows: no value in it.
    assert (work.state, work.reason) == (WITHHELD, "its text could not be read (ValueError)")
    assert blocks == [{"type": "text", "text": "[image withheld by Latet: its text could not be read (ValueError)]"}]
    assert _logged(caplog, "latet.vision.masking") == (logging.ERROR, "could not read an image: ValueError")


# --- on every path a space of a Latet server runs, too -------------------------------------------------------------


def _mediator():
    built = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    return built, Mediator(built.engine, built.verifier)


def test_an_image_that_cannot_be_shown_to_the_reviewer_is_logged_by_the_type_of_its_error(caplog):
    class Unshowable:
        def review_picture(self, work):  # a library's error, quoting what it was drawing over
            raise ValueError(f"cannot label {EMAIL} on the image")

    built, mediator = _mediator()
    try:
        mediator.vision = Unshowable()
        done = Masked(PROMPT, [], fresh=True, original=PROMPT)
        config = LlmReview(enabled=True, images=True)
        item = mediator.review_item(
            config, built.masker, 0, SlotKind.IMAGE_TEXT, done, image=SimpleNamespace(key="f" * 64)
        )
    finally:
        mediator.close()
    assert item is not None and item.picture is None  # the reviewer still reads the text
    failure = "could not prepare an image for the local reviewer: ValueError"
    assert _logged(caplog, "latet.gateway.mediate") == (logging.ERROR, failure)


def test_a_review_that_cannot_be_recorded_is_logged_by_the_type_of_its_error(caplog):
    def refuse(record):  # the dashboard's store fails, and its error quotes the block it was given
        raise sqlite3.OperationalError(f"cannot record the review of {record.sent!r}")

    built, mediator = _mediator()
    try:
        mediator.on_review = refuse
        view = mediator.review_view(PROMPT, built.masker)
        item = ReviewItem(0, PROMPT, SlotKind.USER_TEXT, None, None, False, view, True, PROMPT)
        mediator._record(built.engine.session("conv-1"), item, Answer([Finding(EMAIL, "email")]), [], inline=True)
    finally:
        mediator.close()
    failure = "a review could not be recorded: OperationalError"
    assert _logged(caplog, "latet.gateway.mediate") == (logging.ERROR, failure)


def test_an_exchange_that_cannot_be_queued_is_logged_by_the_type_of_its_error(tmp_path, caplog):
    class Unreadable:  # what was exchanged, which raises as it is read: the error quotes it
        ended = None

        @property
        def prepared(self):
            raise ValueError(f"cannot read the exchange of {EMAIL}")

    store = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
    recorder = Recorder(store)
    try:
        recorder.finish(Unreadable())  # never raised: the dashboard must never break the traffic it observes
    finally:
        recorder.close()
        store.close()
    failure = "could not queue an exchange for the dashboard: ValueError"
    assert _logged(caplog, "latet.gateway.recorder") == (logging.ERROR, failure)


async def test_a_dashboard_call_that_fails_is_logged_by_its_handler_and_the_type_of_its_error(tmp_path, caplog):
    async def put_personal(request):  # a setting refused, as pydantic quotes it: input_value=...
        raise ValueError(f"1 validation error for ProjectEntry: input_value={EMAIL!r}")

    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    project = project_id("/home/emily/src/falcon-holdings")  # a hash of the folder, which a list of folders gives back
    try:
        dashboard = Dashboard(rt)
        dashboard._session_valid = lambda request: True  # signed in
        path = f"/_latet/api/v1/projects/{project}/personal"
        request = Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})
        response = await dashboard._guarded(put_personal)(request)
    finally:
        rt.close()
    assert response.status_code == 500
    [record] = [r for r in caplog.records if r.name == "latet.api.rest"]
    assert (record.levelno, record.getMessage()) == (logging.ERROR, "dashboard API error in put_personal: ValueError")
    assert record.exc_info is not None and "in put_personal" in caplog.text  # where it failed: its frames
    assert EMAIL not in caplog.text and project not in caplog.text  # a frame shows its code, never what it held


def test_a_computers_log_shows_an_errors_types_and_frames_and_never_its_words(tmp_path):
    """daemon.log's formatter is a plain one: once a runtime runs, a record made with an exception carries that
    exception's types and frames as its text, as a server's log shows them, never its words nor its cause's."""
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data",
                      config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)  # fmt: skip
    rt.close()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))  # daemon.log's
    log = logging.getLogger("latet.api.rest")
    log.addHandler(handler)
    try:
        try:
            try:
                raise KeyError(EMAIL)
            except KeyError as cause:
                raise ValueError(f"could not read the address {EMAIL}") from cause
        except ValueError:
            log.exception("dashboard API error in put_personal: %s", "ValueError")
    finally:
        log.removeHandler(handler)
    out = stream.getvalue()
    assert "Traceback (most recent call last):" in out and "in test_a_computers_log_shows" in out
    assert "direct cause" in out and "KeyError" in out and out.rstrip().endswith("ValueError")
    assert EMAIL not in out


def test_a_background_reviews_result_that_cannot_be_applied_is_logged_by_the_type_of_its_error(caplog):
    def refuse(answer):  # what applies it fails, and its error quotes what the reviewer found
        raise ValueError(f"cannot learn {answer.findings[0].value}")

    _Batch(1, refuse, lambda answer: None).deliver(Answer([Finding(EMAIL, "email")]))
    failure = "a background review's result could not be applied: ValueError"
    assert _logged(caplog, "latet.engine.reviewer.reviewer") == (logging.ERROR, failure)


class Unreliable:
    """A store of reviews that fails, its errors quoting what it holds or was given."""

    def load_review(self, key):
        raise sqlite3.OperationalError(f"cannot read the review that found {EMAIL}")

    def save_review(self, key, data):
        raise sqlite3.OperationalError(f"cannot write {data!r}")


def test_a_review_that_cannot_be_read_back_or_kept_is_logged_by_the_type_of_its_error(caplog):
    reviewer = Reviewer(LlmReview(enabled=True), b"\x01" * 32, client=object(), memory=Unreliable())
    assert reviewer.known(PROMPT) is None  # not reviewed before, as far as can be told
    reviewer.remember(PROMPT, Answer([Finding(EMAIL, "email")]))
    said = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "latet.engine.reviewer.reviewer"]
    assert said == [
        (logging.ERROR, "a remembered review could not be read: OperationalError"),
        (logging.ERROR, "a review could not be remembered: OperationalError"),
    ]
    assert all(r.exc_info is None for r in caplog.records) and EMAIL not in caplog.text


def test_a_project_whose_rules_cannot_be_built_is_logged_by_the_type_of_its_error_and_no_folder(
    tmp_path, monkeypatch, caplog
):
    root = tmp_path / "falcon-holdings"
    (root / ".git").mkdir(parents=True)
    (root / PROJECT_FILE).write_text("profile: max\n", "utf-8")
    projects = Projects(Config(), Vault(ROOT_KEY))

    def fail(config, enabled):  # what builds the rules fails, and its error names the folder
        raise ValueError(f"no rules could be built for {root}")

    monkeypatch.setattr(projects, "_scope", fail)
    scope = projects.resolve(str(root)).scope
    assert scope is not None and scope.key.startswith("unbuilt:")  # its texts refused, never the global rules (S-03)
    failure = "could not build the rules of a project (ValueError): its texts are refused"
    assert _logged(caplog, "latet.projects") == (logging.ERROR, failure)
    for held in ("falcon-holdings", project_id(str(root))):
        assert held not in caplog.text


def test_a_recognizer_turned_off_is_logged_by_what_its_problem_is_about(caplog):
    _, [status] = compile_all([{"name": "acme-clients", "pattern": "(Falcon Holdings|Mercury Labs"}])
    assert status.state == "error" and "Falcon Holdings" in status.errors[0]  # the dashboard shows it to its author
    failure = "recognizer acme-clients turned off: a problem with its pattern"
    assert _logged(caplog, "latet.engine.detectors.recognizers") == (logging.WARNING, failure)
    assert "Falcon Holdings" not in caplog.text and "Mercury Labs" not in caplog.text


def test_a_recognizer_whose_name_is_refused_is_logged_by_its_place(caplog):
    _, statuses = compile_all(
        [
            {"name": "acme-codes", "pattern": "(ACME-[0-9]{6}"},
            {"name": f"Clients of {EMAIL}", "pattern": "FALCON-[0-9]{6}"},
        ]
    )
    assert [s.state for s in statuses] == ["error", "error"] and statuses[
        1
    ].name == f"Clients of {EMAIL}"  # its author's
    said = [r.getMessage() for r in caplog.records if r.name == "latet.engine.detectors.recognizers"]
    assert said == [
        "recognizer acme-codes turned off: a problem with its pattern",  # a name that is one: as it is
        "recognizer number 2 turned off: a problem with its name",  # anything else a person wrote: by its place
    ]
    assert EMAIL not in caplog.text and "Clients of" not in caplog.text


class _NotLoaded:
    """The local name model, which could not be loaded: its error names the model's file, in a folder named after the
    person, as the runtime's does."""

    ready = False
    error = OSError(errno.ENOENT, "No such file or directory", f"/home/{EMAIL}/models/model_quantized.onnx")

    def start_loading(self):
        pass


def test_a_model_that_could_not_be_loaded_is_said_once_a_minute_by_the_type_of_its_error(caplog):
    caplog.set_level(logging.INFO, logger="latet.engine.masker")
    masker = Masker(Vault(ROOT_KEY), [NerDetector(_NotLoaded())])
    reports = [MaskReport() for _ in range(1000)]
    for number, report in enumerate(reports):
        masker.mask(f"Text number {number} for the local model.", SlotKind.USER_TEXT, report=report)
    assert all("ner" in report.degraded for report in reports)  # each text still says the model did not read it
    said = [r.getMessage() for r in caplog.records if r.name == "latet.engine.masker"]
    reason = f"the local model could not be loaded: FileNotFoundError ({os.strerror(errno.ENOENT)})"
    assert said == [f"ner did not read a text: {reason}"]  # a thousand texts, one line; then one a minute at most
    assert EMAIL not in caplog.text and "model_quantized" not in caplog.text


def test_the_plugins_facts_that_cannot_be_read_are_logged_by_the_type_of_the_error(monkeypatch, caplog):
    def misread(facts):  # a bug in the reading, quoting the facts the client sent
        raise TypeError(f"cannot read {facts['env']['HOME']}")

    monkeypatch.setattr(routes, "_check", misread)
    assert not routes.check_claude_route({"env": {"HOME": f"/home/{EMAIL}"}}).ok  # it fails closed
    failure = "the plugin's facts about a session's route could not be read: TypeError"
    assert _logged(caplog, "latet.integrations.routes") == (logging.WARNING, failure)
