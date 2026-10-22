"""Google's Gemini API (``generateContent`` and its kin): what Latet reads of a request, under either spelling of its
fields, and what it leaves as it is."""

import pytest

from latet.engine.types import SlotKind
from latet.protocols import gemini
from latet.protocols.base import BodyScan, ImageRef, JsonRef

PNG = "iVBORw0KGgo="


def _slots(scan: BodyScan) -> list[tuple[str, SlotKind, bool]]:
    return [(s.get(), s.kind, s.from_model) for s in scan.slots]


def _conversation(snake: bool = False) -> dict:
    """A conversation with a system instruction, an image, a function call and its response, in camelCase or in
    snake_case."""

    def key(name: str) -> str:
        return gemini.snake(name) if snake else name

    return {
        key("systemInstruction"): {"parts": [{"text": "You are terse."}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": "mail ann"}, {key("inlineData"): {key("mimeType"): "image/png", "data": PNG}}],
            },
            {"role": "model", "parts": [{key("functionCall"): {"name": "send", "args": {"to": "ann@acme.it"}}}]},
            {"role": "user", "parts": [{key("functionResponse"): {"name": "send", "response": {"output": "sent"}}}]},
        ],
    }


# --- the conversation -------------------------------------------------------------------------------------------------


def test_a_conversation_is_read_by_role():
    body = {
        "systemInstruction": {"parts": [{"text": "system words"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "user words"}]},
            {"role": "model", "parts": [{"text": "model words"}]},
            {"parts": [{"text": "no role"}]},
            {"role": "function", "parts": [{"text": "older output"}]},
        ],
    }
    scan = gemini.scan_request(body)
    assert _slots(scan) == [
        ("system words", SlotKind.SYSTEM, False),
        ("user words", SlotKind.USER_TEXT, False),
        ("model words", SlotKind.ASSISTANT_TEXT, True),
        ("no role", SlotKind.USER_TEXT, False),
        ("older output", SlotKind.TOOL_RESULT, False),
    ]
    assert scan.unreadable == []


def test_a_system_instruction_given_as_a_text_is_read():
    scan = gemini.scan_request({"systemInstruction": "be brief", "contents": []})
    assert _slots(scan) == [("be brief", SlotKind.SYSTEM, False)]


def test_snake_case_and_camel_case_are_read_alike():
    camel, snake = gemini.scan_request(_conversation()), gemini.scan_request(_conversation(snake=True))
    assert _slots(camel) == _slots(snake)
    assert [s.get() for s in camel.slots] == ["You are terse.", "mail ann", "ann@acme.it", "sent"]
    assert len(camel.images) == len(snake.images) == 1
    assert camel.unreadable == snake.unreadable == []


def test_a_field_given_under_both_names_is_unreadable():
    body = {
        "systemInstruction": {"parts": [{"text": "a"}]},
        "system_instruction": {"parts": [{"text": "b"}]},
        "contents": [
            {"role": "user", "parts": [{"inlineData": {"mimeType": "image/png", "data": PNG}, "inline_data": {}}]}
        ],
    }
    scan = gemini.scan_request(body)
    assert scan.unreadable.count("a field given under two names") == 2


def test_parts_given_as_one_object_are_read():
    scan = gemini.scan_request({"contents": {"role": "user", "parts": {"text": "only one"}}})
    assert _slots(scan) == [("only one", SlotKind.USER_TEXT, False)]


def test_content_of_another_type_is_unreadable():
    scan = gemini.scan_request({"contents": [{"role": "user", "parts": "plain text"}], "systemInstruction": 3})
    assert len(scan.unreadable) == 2


# --- tools -------------------------------------------------------------------------------------------------------------


def test_a_function_call_and_its_response():
    body = {
        "contents": [
            {
                "role": "model",
                "parts": [{"functionCall": {"id": "c1", "name": "read_file", "args": {"file_path": "C:/data/x.json"}}}],
            },
            {
                "role": "user",
                "parts": [
                    {"functionResponse": {"id": "c1", "name": "read_file", "response": {"output": "ann@acme.it"}}}
                ],
            },
        ]
    }
    scan = gemini.scan_request(body)
    call, result = scan.slots
    assert (call.get(), call.kind, call.from_model) == ("C:/data/x.json", SlotKind.TOOL_INPUT, True)
    assert (result.get(), result.kind, result.from_model, result.tool) == (
        "ann@acme.it",
        SlotKind.TOOL_RESULT,
        False,
        "read_file",
    )
    assert result.source == "C:/data/x.json"
    args = body["contents"][0]["parts"][0]["functionCall"]["args"]
    response = body["contents"][1]["parts"][0]["functionResponse"]["response"]
    assert scan.keyed == [(args, SlotKind.TOOL_INPUT, True), (response, SlotKind.TOOL_RESULT, False)]


def test_a_response_without_ids_is_matched_to_its_call_by_name():
    body = {
        "contents": [
            {"role": "model", "parts": [{"functionCall": {"name": "fetch", "args": {"url": "https://acme.it/x"}}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "fetch", "response": {"content": "page"}}}]},
        ]
    }
    result = gemini.scan_request(body).slots[-1]
    assert (result.tool, result.source) == ("fetch", "https://acme.it/x")


def test_images_are_read_and_other_media_are_not():
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "look"},
                    {"inlineData": {"mimeType": "image/png", "data": PNG}},
                    {"inlineData": {"mimeType": "application/pdf", "data": "JVBE"}},
                    {"inline_data": {"mime_type": "audio/wav", "data": "UklG"}},
                    {"inlineData": {"mimeType": "video/mp4", "data": "AAAA"}},
                    {"fileData": {"mimeType": "image/jpeg", "fileUri": "https://x/y.jpg"}},
                    {"fileData": {"mimeType": "application/pdf", "fileUri": "files/abc"}},
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "screenshot",
                            "response": {"image": {"$ref": "shot"}},
                            "parts": [{"inlineData": {"mimeType": "image/png", "data": PNG, "displayName": "shot"}}],
                        }
                    }
                ],
            },
        ]
    }
    scan = gemini.scan_request(body)
    assert scan.uninspected == ["document", "audio", "file", "image", "document"]
    first, second = scan.images
    assert (first.form, first.media_type, first.kind, first.type_key, first.at) == (
        "base64url",
        "image/png",
        SlotKind.USER_TEXT,
        "mimeType",
        1,
    )
    assert first.data() == PNG
    assert first.withhold("withheld") and body["contents"][0]["parts"][1] == {"text": "withheld"}
    assert (second.kind, second.tool) == (SlotKind.TOOL_RESULT, "screenshot")


def test_display_names_are_texts_of_the_same_kind_as_the_ref_that_names_them():
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": PNG, "displayName": "invoice ann.png"}},
                    {
                        "functionResponse": {
                            "name": "shot",
                            "response": {"image": {"$ref": "ann.png"}},
                            "parts": [{"inlineData": {"mimeType": "image/png", "data": PNG, "displayName": "ann.png"}}],
                        }
                    },
                ],
            }
        ]
    }
    texts = [(s.get(), s.kind) for s in gemini.scan_request(body).slots]
    assert ("invoice ann.png", SlotKind.USER_TEXT) in texts
    assert ("ann.png", SlotKind.TOOL_RESULT) in texts
    assert texts.count(("ann.png", SlotKind.TOOL_RESULT)) == 2  # the $ref and the name it points at, masked alike


def test_the_providers_parts_pass_and_metadata_holds_no_text():
    parts = [
        {"executableCode": {"language": "PYTHON", "code": "print(1)"}},
        {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "1"}},
        {"toolCall": {"id": "t1", "toolType": "GOOGLE_SEARCH", "args": {"query": "q"}}},
        {"toolResponse": {"id": "t1", "toolType": "GOOGLE_SEARCH", "response": {"results": []}}},
        {"thoughtSignature": "c2ln"},
        {"text": "signed", "thoughtSignature": "c2ln"},
    ]
    scan = gemini.scan_request({"contents": [{"role": "model", "parts": parts}]})
    assert _slots(scan) == [("signed", SlotKind.ASSISTANT_TEXT, True)]
    assert scan.unreadable == []


def test_a_parts_own_metadata_is_read_as_other_text():
    scan = gemini.scan_request(
        {"contents": [{"role": "user", "parts": [{"text": "x", "partMetadata": {"note": "ann"}}]}]}
    )
    assert ("ann", SlotKind.OTHER, False) in _slots(scan)


@pytest.mark.parametrize(
    "part",
    [
        {"text": "heard", "audioTranscription": {"text": "what was said"}},
        {"weird": 1},
        {"text": "a", "functionCall": {"name": "f", "args": {}}},
        "not an object",
        {"text": 3},
        {"functionCall": {"name": "f", "args": "not an object"}},
    ],
)
def test_a_part_latet_cannot_read_is_named_unreadable(part):
    scan = gemini.scan_request({"contents": [{"role": "user", "parts": [part]}]})
    assert scan.unreadable


def test_a_thought_is_read_as_the_models_text():
    scan = gemini.scan_request({"contents": [{"role": "model", "parts": [{"text": "pondering", "thought": True}]}]})
    assert _slots(scan) == [("pondering", SlotKind.ASSISTANT_TEXT, True)]


# --- other methods -----------------------------------------------------------------------------------------------------


def test_count_tokens_embeddings_and_caches():
    counted = gemini.scan_request(
        {
            "generateContentRequest": {
                "model": "models/g",
                "contents": [{"role": "user", "parts": [{"text": "count me"}]}],
            }
        }
    )
    assert _slots(counted) == [("count me", SlotKind.USER_TEXT, False)]
    embedded = gemini.scan_request(
        {"content": {"parts": [{"text": "embed me"}]}, "title": "a title", "embedContentConfig": {"title": "b title"}}
    )
    assert [s.get() for s in embedded.slots] == ["embed me", "a title", "b title"]
    assert {s.kind for s in embedded.slots} == {SlotKind.USER_TEXT}
    batch = gemini.scan_request({"requests": [{"model": "m", "content": {"parts": [{"text": "one"}]}, "title": "t"}]})
    assert [s.get() for s in batch.slots] == ["one", "t"]
    cached = gemini.scan_request(
        {"model": "models/g", "contents": [{"role": "user", "parts": [{"text": "cache me"}]}], "ttl": "300s"}
    )
    assert [s.get() for s in cached.slots] == ["cache me"]


def test_tools_and_answer_schemas_are_read_as_definitions():
    body = {
        "contents": [],
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "send",
                        "description": "Send to ann@acme.it",
                        "parameters": {"type": "OBJECT", "properties": {"to": {"type": "STRING"}}},
                        "parametersJsonSchema": {"type": "object"},
                        "response": {"type": "OBJECT"},
                        "responseJsonSchema": {"type": "object"},
                    }
                ]
            },
            {"function_declarations": [{"name": "f", "parameters_json_schema": {"type": "object"}}]},
            {"googleSearch": {}},
            {"mcpServers": [{"name": "m", "streamableHttpTransport": {"url": "https://mcp", "headers": {"k": "v"}}}]},
        ],
        "generationConfig": {"responseJsonSchema": {"type": "object"}, "responseSchema": {"type": "OBJECT"}},
        "generateContentRequest": {"generation_config": {"_response_json_schema": {"type": "object"}}},
    }
    tools = BodyScan()
    gemini.definitions(body, tools)
    assert [s.kind for s in tools.slots] == [SlotKind.TOOL_DESCRIPTION] * len(tools.slots)
    assert [type(s) for s in tools.slots] == [
        type(tools.slots[0]),
        JsonRef,
        JsonRef,
        JsonRef,
        JsonRef,
        JsonRef,
        JsonRef,
        JsonRef,
        JsonRef,
    ]
    assert tools.slots[0].get() == "Send to ann@acme.it"
    assert all(s.mode == "json" for s in tools.slots[1:])
    assert tools.unreadable == []
    assert gemini.scan_request(body).slots == []  # read by ``definitions`` alone


def test_settings_are_not_read():
    body = {
        "model": "models/g",
        "contents": [],
        "safetySettings": [{"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"}],
        "toolConfig": {"retrievalConfig": {"latLng": {"latitude": 45.4, "longitude": 9.2}, "languageCode": "it"}},
        "generationConfig": {"stopSequences": ["ann"], "temperature": 0.1},
        "labels": {"team": "ann"},
        "cachedContent": "cachedContents/abc",
        "serviceTier": "standard",
    }
    scan = gemini.scan_request(body)
    assert scan.slots == [] and scan.unreadable == []


def test_the_opening_names_the_conversation():
    body = _conversation(snake=True)
    system, contents = gemini.opening(body)
    assert system == body["system_instruction"] and contents == body["contents"]


def test_a_tool_the_provider_ran_is_named_with_its_input():
    body = {
        "contents": [
            {"role": "model", "parts": [{"toolCall": {"toolName": "google_search", "args": {"query": "ann", "n": 2}}}]},
            {"role": "model", "parts": [{"tool_call": {"tool_type": "URL_CONTEXT", "args": {"url": "https://x"}}}]},
        ]
    }
    calls = list(gemini.server_calls(body))
    assert [(name, [r.get() for r in refs]) for name, refs in calls] == [
        ("google_search", ["ann"]),
        ("URL_CONTEXT", ["https://x"]),
    ]
    assert {r.kind for _, refs in calls for r in refs} == {SlotKind.OUTBOUND_TOOL}


# --- paths and images ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/gemini/v1beta/models/g:generateContent", "generateContent"),
        ("/gemini/v1beta/models/g%3AstreamGenerateContent", "streamGenerateContent"),
        ("/gemini/v1beta/models/g%3acountTokens", "countTokens"),
        ("/gemini/v1beta/models/g:embedContent/", "embedContent"),
        ("/gemini/v1beta/models/g:batchEmbedContents", "batchEmbedContents"),
        ("/vertex/v1beta1/projects/p/locations/l/publishers/google/models/g:generateContent", "generateContent"),
        ("/gemini/v1beta/cachedContents", "cachedContents"),
        ("/gemini/v1beta/cachedContents/abc", "cachedContents"),
        ("/gemini/v1beta/interactions", None),
        ("/gemini/v1beta/models", None),
        ("/gemini/v1beta/models/g:predict", None),
        ("/gemini/v1beta/models/g:batchGenerateContent", None),
        ("/gemini/upload/v1beta/files", None),
    ],
)
def test_method_of(path, method):
    assert gemini.method_of(path) == method


def test_an_image_ref_writes_its_media_type_under_its_own_key():
    holder = {"mimeType": "image/png", "data": "x"}
    ref = ImageRef(holder, "data", "base64", "image/png", SlotKind.USER_TEXT, 0, type_key="mimeType")
    ref.set("y", "image/jpeg")
    assert holder == {"mimeType": "image/jpeg", "data": "y"}
    anthropic = {"type": "base64", "media_type": "image/png", "data": "x"}
    ImageRef(anthropic, "data", "base64", "image/png", SlotKind.USER_TEXT, 0).set("y", "image/jpeg")
    assert anthropic["media_type"] == "image/jpeg"


def test_an_image_in_url_safe_base64_is_read_in_the_standard_alphabet():
    holder = {"mime_type": "image/png", "data": "ab-_cd"}
    scan = gemini.scan_request({"contents": [{"role": "user", "parts": [{"inline_data": holder}]}]})
    [image] = scan.images
    assert image.data() == "ab+/cd=="
    image.set("AAAA", "image/jpeg")
    assert holder == {"mime_type": "image/jpeg", "data": "AAAA"}


def test_streamed_arguments_sent_back_are_read_as_the_models_input():
    call = {
        "name": "send",
        "partialArgs": [{"jsonPath": "$.to", "stringValue": "ann@acme.it"}, {"jsonPath": "$.n", "numberValue": 2}],
    }
    snake = {
        "name": "send",
        "partial_args": [{"json_path": "$.to", "string_value": "bob@acme.it", "will_continue": True}],
    }
    body = {"contents": [{"role": "model", "parts": [{"functionCall": call}, {"function_call": snake}]}]}
    scan = gemini.scan_request(body)
    assert _slots(scan) == [("ann@acme.it", SlotKind.TOOL_INPUT, True), ("bob@acme.it", SlotKind.TOOL_INPUT, True)]
    assert scan.unreadable == []


@pytest.mark.parametrize(
    "part",
    [
        {"functionCall": {"name": "f", "args": {}, "extra": "x"}},
        {"functionResponse": {"name": "f", "response": {}, "output": "x"}},
        {"functionCall": {"name": "f", "partialArgs": "not a list"}},
    ],
)
def test_a_call_or_a_response_with_a_field_latet_does_not_know_is_unreadable(part):
    assert gemini.scan_request({"contents": [{"role": "model", "parts": [part]}]}).unreadable


def test_a_single_object_where_google_reads_a_list_is_read():
    tools = BodyScan()
    body = {
        "tools": [{"functionDeclarations": {"name": "f", "description": "about ann", "parameters": {"type": "object"}}}]
    }
    gemini.definitions(body, tools)
    assert [s.get() for s in tools.slots][:1] == ["about ann"] and len(tools.slots) == 2
    batch = gemini.scan_request({"requests": {"content": {"parts": [{"text": "embed ann"}]}}})
    assert [s.get() for s in batch.slots] == ["embed ann"]


def test_declarations_or_requests_of_another_type_are_unreadable():
    tools = BodyScan()
    gemini.definitions({"tools": [{"functionDeclarations": "f"}]}, tools)
    assert tools.unreadable
    assert gemini.scan_request({"requests": "embed ann"}).unreadable
