"""Real request masking and response restoration shared by conversation-boundary tests."""

import json

from latet.gateway.mediate import REWRITE, Mediator, Prepared
from latet.gateway.routes import Upstream

FIRST = "scope.first@gmail.com"
SECOND = "scope.second@gmail.com"
OPENAI = Upstream("openai", "/openai", "http://127.0.0.1:1")


def request(value: str, *, opening: str = "Continue the report.", responses: bool = False, **extra) -> bytes:
    messages = [
        {"role": "user", "content": opening},
        {"role": "assistant", "content": "Please provide the contact."},
        {"role": "user", "content": value},
    ]
    return json.dumps({"model": "test", "input" if responses else "messages": messages, **extra}).encode()


def prepare(mediator: Mediator, raw: bytes, headers: dict[str, str] | None = None, *, responses=False) -> Prepared:
    path = "/openai/v1/responses" if responses else "/openai/v1/chat/completions"
    prepared = mediator.prepare(OPENAI, "POST", path, headers or {}, raw)
    assert prepared.mode == REWRITE and prepared.block_reason is None
    assert prepared.session is not None and prepared.held
    return prepared


def fake(mediator: Mediator, original: str, prepared: Prepared) -> str:
    entity = mediator.engine.vault.by_original(original)
    assert entity is not None
    assert original not in prepared.body.decode()
    assert entity.surrogate in prepared.body.decode()
    return entity.surrogate


def answer(mediator: Mediator, prepared: Prepared, text: str) -> str:
    if prepared.protocol == "responses":
        body = {
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]
        }
    else:
        body = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    raw = json.dumps(body).encode()
    transform = mediator.response_transform(prepared, "application/json", None)
    restored = raw if transform is None else transform.feed(raw) + transform.end()
    doc = json.loads(restored)
    if prepared.protocol == "responses":
        return doc["output"][0]["content"][0]["text"]
    return doc["choices"][0]["message"]["content"]


def release(mediator: Mediator, *requests: Prepared) -> None:
    for prepared in requests:
        if prepared.session is not None and prepared.held and not prepared.released:
            mediator.engine.release(prepared.session, prepared.hold)
            prepared.released = True


def name_response(mediator: Mediator, prepared: Prepared, response_id: str) -> None:
    """Feed a real provider response-created event through the response transform's naming callback."""
    stream = mediator.response_transform(prepared, "text/event-stream", None)
    assert stream is not None and prepared.protocol == "responses"
    event = {"type": "response.created", "response": {"id": response_id}}
    try:
        stream.feed(f"event: response.created\ndata: {json.dumps(event)}\n\n".encode())
        stream.end()
    finally:
        stream.close()
