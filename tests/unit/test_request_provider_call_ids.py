"""Tool call ids that OpenAI-compatible providers issue, sent back in the next Chat Completions request: each provider's
own form passes on Chat Completions by its own grammar, with its anchor; anything else stays a correlation-identifier
refusal, and no form passes on another protocol.

- Mistral: ``^[a-zA-Z0-9]{9}$`` (mistral-common 5bd2a815 ``protocol/instruct/validator.py:50``, enforced on the
  assistant's tool calls and the tool messages, 544-584).
- llama.cpp's server: ``gen_tool_call_id()`` is ``random_string()``, 32 of ``[0-9A-Za-z]`` (7071ba37
  ``tools/server/server-common.cpp:168-188``).
- vLLM: ``make_tool_call_id`` gives ``chatcmpl-tool-{random_uuid()}``, 16 hex digits at 81a28794
  (``vllm/renderers/chat_utils/tool_call.py:27-32``, ``vllm/utils/__init__.py:11-12``; earlier releases 32), and for
  Kimi K2 ``functions.{name}:{index}`` (the same function), as Moonshot's API names them.
"""

from __future__ import annotations

import pytest

from latet.gateway.body_coverage import Ledger, inspect_body
from latet.gateway.routes import Upstream
from latet.protocols.base import BodyScan

LOCAL = Upstream("llama", "/llama", "http://192.168.1.20:8080/v1")


def _chat(call_id: str, name: str = "read_file") -> dict:
    call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": '{"path": "notes.txt"}'}}
    return {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "user", "content": "Who wrote notes.txt?"},
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call_id, "content": "Written by the team."},
        ],
    }


def _responses(call_id: str) -> dict:
    return {
        "model": "gpt-4.1-mini",
        "input": [
            {"type": "function_call", "call_id": call_id, "name": "read_file", "arguments": "{}"},
            {"type": "function_call_output", "call_id": call_id, "output": "done"},
        ],
    }


def _problem(body: dict, protocol: str = "chat") -> str | None:
    return inspect_body(body, protocol, LOCAL, BodyScan(), False, Ledger()).problem


PROVIDERS = {
    "mistral": "D681PevKs",
    "llama.cpp": "aZ3kQ9mB7xT2vW5nR8pL1cF4hJ6dS0yE",
    "vllm": "chatcmpl-tool-9f8e7d6c5b4a3210",
    "vllm, earlier": "chatcmpl-tool-0123456789abcdef0123456789abcdef",
    "kimi": "functions.read_file:0",
}


@pytest.mark.parametrize("call_id", PROVIDERS.values(), ids=PROVIDERS.keys())
def test_a_providers_own_tool_call_id_passes_on_chat_completions(call_id):
    assert _problem(_chat(call_id)) is None


def test_kimis_id_names_a_tool_and_its_index():
    assert _problem(_chat("functions.mcp__crm__list-accounts:12", "mcp__crm__list-accounts")) is None


@pytest.mark.parametrize(
    "call_id",
    [
        "D681PevK",  # 8: Mistral's are 9
        "D681PevKs0",  # 10, and not llama.cpp's 32
        "D681PevK-",
        "aZ3kQ9mB7xT2vW5nR8pL1cF4hJ6dS0y",  # 31
        "chatcmpl-tool-9F8E7D6C5B4A3210",  # random_uuid is lowercase hex
        "chatcmpl-tool-9f8e7d6c5b4a321",
        "chatcmpl-tool-mario.rossi",
        "functions.read_file:",
        "functions.read file:0",
        "functions.mario.rossi@example.com:0",
        "mario.rossi@example.com",
        "the tool call",
    ],
)
def test_an_id_outside_every_providers_form_is_refused(call_id):
    assert _problem(_chat(call_id)) == "request body refused (correlation identifier)"


@pytest.mark.parametrize("call_id", PROVIDERS.values(), ids=PROVIDERS.keys())
def test_no_chat_providers_form_passes_on_responses(call_id):
    assert _problem(_responses(call_id), "responses") == "request body refused (correlation identifier)"
