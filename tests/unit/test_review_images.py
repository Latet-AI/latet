"""The local reviewer shown the images: what of its answer is kept, how it is asked, and the settings."""

from __future__ import annotations

import asyncio
import base64
import io
import json

import httpx
import pytest

from latet.config import Config, LlmReview, ProjectEntry, ProjectSettings
from latet.engine.reviewer import Picture, ReviewClient, Reviewer, View, read_answer, view_of
from latet.engine.reviewer.probe import sees_images
from latet.engine.reviewer.prompt import IMAGE_NOTE, SYSTEM, system_for
from latet.engine.types import EntityClass, Policy, Replacement, SlotKind
from latet.projects import effective_config, repo_settings
from latet.protection import loosened

TEXT = "Cliente: Giulia Conti\nIBAN IT60X0542811101000000123456"


def _answer(*findings: tuple[str, str]) -> str:
    return json.dumps({"findings": [{"text": t, "category": c} for t, c in findings], "block": False})


# --- what of its answer is kept --------------------------------------------------------------------


def test_a_value_seen_in_the_image_is_kept_as_the_text_has_it():
    view = View(TEXT)
    answer = read_answer(
        _answer(("giulia  conti", "person"), ("IT60 X054 2811 1010 0000 0123 456", "financial")), view, True
    )
    assert [(f.value, f.category) for f in answer.findings] == [
        ("Giulia Conti", "person"),
        ("IT60X0542811101000000123456", "financial"),
    ]
    assert answer.unlocated == []


def test_a_value_nowhere_in_the_text_is_unlocated_and_only_with_an_image():
    view = View(TEXT)
    seen = read_answer(_answer(("Paolo Neri", "person"), ("Giulia Conti", "person")), view, True)
    assert [f.value for f in seen.findings] == ["Giulia Conti"]
    assert [(f.value, f.category) for f in seen.unlocated] == [("Paolo Neri", "person")]
    text_only = read_answer(_answer(("Paolo Neri", "person")), view)
    assert text_only.findings == [] and text_only.unlocated == []


def test_labels_and_masked_values_are_never_reported():
    view = view_of("Email: a.b@acme.it, Giulia", [(7, 18, "EMAIL")])
    assert view.text.startswith("Email: ⟦EMAIL#1⟧")
    answer = read_answer(_answer(("[EMAIL#1]", "email"), ("EMAIL#1", "email"), ("Giulia", "person")), view, True)
    assert [f.value for f in answer.findings] == ["Giulia"]
    assert answer.unlocated == []


def test_unlocated_values_are_remembered_with_the_answer():
    from latet.engine.reviewer import Answer, Finding

    answer = Answer([Finding("Giulia", "person")], unlocated=[Finding("Paolo Neri", "person")])
    back = Answer.from_json(json.loads(json.dumps(answer.to_json())))
    assert back.unlocated == [Finding("Paolo Neri", "person")] and back.findings == [Finding("Giulia", "person")]


# --- how it is asked ------------------------------------------------------------------------------


class VisionServer:
    """Answers for the words of the text, and for ``seen`` when it was shown an image."""

    def __init__(self, seen=None, vision=True):
        self.seen = seen or {}
        self.vision = vision
        self.bodies: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"total_slots": 1, "modalities": {"vision": self.vision}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fake-model"}]})
        body = json.loads(request.content)
        self.bodies.append(body)
        content = body["messages"][1]["content"]
        found = []
        if isinstance(content, list):
            found = [{"text": t, "category": c} for t, c in self.seen.items()]
        chunk = {"choices": [{"delta": {"content": json.dumps({"findings": found, "block": False})}}]}
        stream = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode())


def _reviewer(server, **config):
    settings = LlmReview(enabled=True, model="fake-model", **config)
    client = ReviewClient(settings.url, "fake-model", transport=httpx.MockTransport(server))
    return Reviewer(settings, b"k" * 32, client=client)


def test_the_reviewer_is_shown_the_image_with_its_text_and_remembers_the_answer_with_it():
    server = VisionServer({"Paolo Neri": "person"})
    reviewer = _reviewer(server)
    picture = Picture("img-1", "data:image/png;base64,AAAA")

    async def run():
        first = await reviewer.review(View(TEXT), TEXT, picture=picture)
        again = await reviewer.review(View(TEXT), TEXT, picture=picture)
        alone = await reviewer.review(View(TEXT), TEXT)
        await reviewer.aclose()
        return first, again, alone

    first, again, alone = asyncio.run(run())
    assert [f.value for f in first.unlocated] == ["Paolo Neri"]
    assert again.cached and again.unlocated == first.unlocated
    assert alone.unlocated == []
    shown, text_only = server.bodies  # the second review of the image came from memory
    assert shown["messages"][1]["content"] == [
        {"type": "text", "text": TEXT},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    assert IMAGE_NOTE in shown["messages"][0]["content"]
    assert text_only["messages"][1]["content"] == TEXT and IMAGE_NOTE not in text_only["messages"][0]["content"]
    assert reviewer.known(TEXT, "img-1") is not None and reviewer.known(TEXT, "img-2") is None


def test_the_image_note_comes_before_your_guidance():
    system = system_for("Our suppliers are not confidential.", image=True)
    assert system.startswith(f"{SYSTEM}\n\n{IMAGE_NOTE}") and system.endswith("Our suppliers are not confidential.")


def test_llama_cpp_says_whether_its_model_sees_images():
    assert sees_images({"modalities": {"vision": True}}) is True
    assert sees_images({"modalities": {"vision": False, "audio": False}}) is False
    assert sees_images({"total_slots": 1}) is None and sees_images(None) is None


# --- what it is shown ------------------------------------------------------------------------------


def _replacement(start: int, end: int, original: str, fake: str, etype: str, policy=Policy.MASK) -> Replacement:
    return Replacement(start, end, original, fake, etype, EntityClass.PII, "test", policy, None)


def test_the_image_shown_has_a_label_for_each_masked_value_and_is_not_too_large():
    from PIL import Image

    from latet.vision.masking import REVIEW_SIDE, ImageMasking, ImageWork, review_covers

    from ..fake_ocr import FakeOcr

    reader = FakeOcr()
    data = reader.screenshot(["Email: mario@acme.it, copy mario@acme.it", "Boss: luca@acme.it"])
    masking = ImageMasking(reader)
    work = masking.work(_ref(data))
    assert isinstance(work, ImageWork) and work.result is not None
    text = work.result.text
    first = text.index("mario@acme.it")
    second = text.index("mario@acme.it", first + 1)
    third = text.index("luca@acme.it")
    replacements = [
        _replacement(first, first + 13, "mario@acme.it", "anna@bianchi.it", "EMAIL"),
        _replacement(second, second + 13, "mario@acme.it", "anna@bianchi.it", "EMAIL"),
        _replacement(third, third + 12, "luca@acme.it", "rosa@verdi.it", "EMAIL", Policy.REDACT),
    ]
    labels = [c.text for c in review_covers(work.result, replacements)]
    assert labels == ["[EMAIL#1]", "[EMAIL#1]", None]
    work.replacements = replacements
    url = masking.review_picture(work)
    assert url is not None and url.startswith("data:image/png;base64,")
    image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(image.size) <= REVIEW_SIDE and image.size == (work.result.width, work.result.height)


def _ref(data: str):
    from latet.protocols.base import ImageRef

    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    return ImageRef(block["source"], "data", "base64", "image/png", SlotKind.USER_TEXT, 0, None, [block], 0)


# --- the settings ------------------------------------------------------------------------------------


def test_turning_the_images_off_or_sending_them_away_lowers_protection():
    on = Config(llm_review={"enabled": True, "images": True})
    off = Config(llm_review={"enabled": True, "images": False})
    assert "the local reviewer: no longer sees the images" in loosened(on, off)
    assert loosened(off, on) == []
    remote = {"enabled": True, "url": "https://reviewer.example.net/v1", "remote": True}
    away = loosened(Config(llm_review=remote), Config(llm_review={**remote, "images": True}))
    assert away == ["the local reviewer: the images go to a server on another machine"]


def test_a_project_of_yours_can_show_the_images_or_not_a_repository_file_cannot():
    config = Config(llm_review={"enabled": True, "images": True})
    mine, _ = effective_config(config, None, ProjectEntry(path="/p", review_images=False))
    assert mine.llm_review.images is False
    repo, _ = effective_config(config, ProjectSettings(review_images=False), None)
    assert repo.llm_review.images is True
    settings, notices = repo_settings({"review_images": False})
    assert settings.review_images is None
    assert [(n.key, n.level) for n in notices] == [("review_images", "ignored")]
    changed = Config(
        llm_review={"enabled": True, "images": True}, projects=[ProjectEntry(path="/p", review_images=False)]
    )
    assert "project p: the local reviewer: no longer sees the images" in loosened(config, changed)


@pytest.mark.parametrize("value", [True, False])
def test_the_setting_round_trips(value):
    assert LlmReview.model_validate(LlmReview(images=value).model_dump(mode="json")).images is value


@pytest.mark.parametrize(("vision", "status"), [(True, None), (False, "fail")])
def test_the_doctor_says_when_the_model_does_not_see_images(monkeypatch, vision, status):
    from latet.doctor import checks
    from latet.engine.reviewer import reviewer as module

    config = Config(llm_review={"enabled": True, "model": "fake-model", "images": True})
    transport = httpx.MockTransport(VisionServer(vision=vision))
    monkeypatch.setattr(checks, "load_config", lambda: config)
    monkeypatch.setattr(
        module, "client_for", lambda c, timeout=300.0: ReviewClient(c.url, c.model, transport=transport)
    )
    found = checks.check_reviewer()
    assert found[0].status == "ok"
    seen = [c for c in found if "see images" in c.detail]
    assert [c.status for c in seen] == ([status] if status else [])


def test_images_mask_with_the_max_profile_sends_more_so_it_waits_and_a_repository_cannot_set_it():
    from latet.config import Profile

    strict = Config(profile=Profile.MAX)
    assert "images: from block to mask" in loosened(strict, Config(profile=Profile.MAX, images="mask"))
    assert loosened(Config(profile=Profile.MAX, images="mask"), strict) == []
    repo, notices = effective_config(strict, ProjectSettings(images="mask"), None)
    assert repo.images != "mask" and [(n.key, n.level) for n in notices] == [("images", "ignored")]
    mine, _ = effective_config(strict, None, ProjectEntry(path="/p", images="mask"))
    assert mine.images == "mask"
