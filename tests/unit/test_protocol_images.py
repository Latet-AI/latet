"""Images in requests: found as references to their bytes, by every protocol."""

from __future__ import annotations

from latet.config import Config, Profile
from latet.engine.types import SlotKind
from latet.protocols import anthropic, openai_chat, openai_responses
from latet.vision.policy import ALLOW, ImagePolicy, image_policy, refusal

PNG = "iVBORw0KGgo="
URL = f"data:image/png;base64,{PNG}"


def _anthropic_image(data: str = PNG) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def test_anthropic_images_in_a_message_and_in_a_tool_result():
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "look"}, _anthropic_image()]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"p": "a.png"}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [_anthropic_image()]}],
            },
        ]
    }
    scan = anthropic.scan_request(body)
    first, second = scan.images
    assert (first.kind, first.at, first.tool, first.media_type) == (SlotKind.USER_TEXT, 1, None, "image/png")
    assert (second.kind, second.tool) == (SlotKind.TOOL_RESULT, "Read")
    assert first.data() == PNG and scan.uninspected == [] and scan.unread() == ["image", "image"]


def test_anthropic_image_set_and_withheld():
    content = [{"type": "text", "text": "look"}, _anthropic_image()]
    [image] = anthropic.scan_request({"messages": [{"role": "user", "content": content}]}).images
    image.set("QUJD", "image/jpeg")
    assert content[1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}
    assert image.withhold("[image withheld]") and content[1] == {"type": "text", "text": "[image withheld]"}


def test_anthropic_images_by_url_and_documents_are_not_read():
    url = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
    pdf = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBE"}}
    scan = anthropic.scan_request({"messages": [{"role": "user", "content": [url, pdf]}]})
    assert scan.images == [] and scan.uninspected == ["image", "document"]


def test_chat_data_url_images_and_the_rest():
    content = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": URL, "detail": "high"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
    ]
    scan = openai_chat.scan_request({"messages": [{"role": "user", "content": content}]})
    [image] = scan.images
    assert (image.media_type, image.data(), image.at) == ("image/png", PNG, 1)
    assert scan.uninspected == ["image", "input_audio"]
    image.set("QUJD", "image/jpeg")
    assert content[1]["image_url"] == {"url": "data:image/jpeg;base64,QUJD", "detail": "high"}
    assert image.withhold("gone") and content[1] == {"type": "text", "text": "gone"}


def test_responses_images_in_messages_tool_outputs_and_screenshots():
    shot = {"type": "computer_screenshot", "image_url": URL}
    body = {
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": URL}]},
            {"type": "function_call", "call_id": "c1", "name": "browser", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_image", "image_url": URL}]},
            {"type": "computer_call_output", "call_id": "c2", "output": shot},
            {"type": "message", "role": "user", "content": [{"type": "input_image", "file_id": "file-1"}]},
        ]
    }
    scan = openai_responses.scan_request(body)
    user, tool, screen = scan.images
    assert (user.kind, tool.kind, tool.tool) == (SlotKind.USER_TEXT, SlotKind.TOOL_RESULT, "browser")
    assert scan.uninspected == ["image"]  # the file id
    assert tool.withhold("gone") and body["input"][2]["output"] == [{"type": "input_text", "text": "gone"}]
    assert not screen.withhold("gone")  # a screenshot answers the call: only the image itself can change
    screen.set("QUJD", "image/png")
    assert shot["image_url"] == "data:image/png;base64,QUJD"


def test_image_policy_and_why_a_request_is_not_sent():
    assert image_policy(Config()) is ALLOW
    assert image_policy(Config(profile=Profile.MAX)) == ImagePolicy("block", "profile")
    assert image_policy(Config(images="block")) == ImagePolicy("block", "setting")
    assert "max profile" in refusal(ImagePolicy("block", "profile"), ["image"])
    reason = refusal(ImagePolicy("block", "setting"), ["image", "document", "image"])
    assert "images: block" in reason and "(document, image)" in reason and "max profile" not in reason
