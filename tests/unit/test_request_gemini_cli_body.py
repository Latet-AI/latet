"""Gemini CLI's tool calls through P2-2's body coverage: the call ids it makes are named by the grammar of its source.

Gemini CLI v0.66.0 (``packages/core/src/core/turn.ts:470-485``, ``handlePendingFunctionCall``) gives a call the model
sent without an id ``f"{name}__{name}_{Date.now()}_{counter}"``, sets it on the ``functionCall`` kept in the history,
and its ``functionResponse`` echoes it (``utils/generateContentResponseUtilities.ts:24-30``): from the second turn of a
conversation that used a tool, every request carries both. Its context-management form (``synth_``) and the model's
own id (bare, or after the tool's name) pass too; text outside these forms, or a form on another protocol, stays
refused as a correlation identifier, never naming its value.
"""

from __future__ import annotations

import copy
import json

import pytest

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.body_coverage import Ledger, inspect_body
from latet.gateway.mediate import REWRITE, Mediator
from latet.gateway.routes import Upstream
from latet.protocols import gemini
from latet.protocols.base import BodyScan

from ..conftest import ROOT_KEY

GEMINI = Upstream("gemini", "/gemini", "https://generativelanguage.googleapis.com", refuses_unread=True)
PATH = "/gemini/v1beta/models/gemini-2.5-flash:streamGenerateContent"
EMAIL = "emily.carter@acme.com"
# What Gemini CLI makes for its first call of a turn of read_file: the tool's name, twice, its milliseconds, a counter.
CALL_ID = "read_file__read_file_1761600000000_0"


def gemini_cli_turn(call_id: str = CALL_ID, response_id: str | None = None) -> dict:
    """Gemini CLI's second request of a conversation in which the model read a file (members as its source builds them;
    synthetic values)."""
    return {
        "contents": [
            {"role": "user", "parts": [{"text": "Read notes.txt and tell me who wrote it."}]},
            {
                "role": "model",
                "parts": [
                    {"functionCall": {"id": call_id, "name": "read_file", "args": {"absolute_path": "notes.txt"}}}
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": call_id if response_id is None else response_id,
                            "name": "read_file",
                            "response": {"output": f"Written by {EMAIL}."},
                        }
                    }
                ],
            },
        ],
        "systemInstruction": {"role": "user", "parts": [{"text": "You are an interactive CLI agent."}]},
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "read_file",
                        "description": "Reads a file.",
                        "parametersJsonSchema": {
                            "type": "object",
                            "properties": {"absolute_path": {"type": "string"}},
                            "required": ["absolute_path"],
                        },
                    }
                ]
            }
        ],
        "generationConfig": {"temperature": 0, "topP": 1},
    }


def _inspect(body: dict, protocol: str = "gemini"):
    return inspect_body(body, protocol, GEMINI, BodyScan(), False, Ledger())


@pytest.mark.parametrize(
    "call_id",
    [
        CALL_ID,
        "read_file__read_file_1761600000000_12",  # a later call of the same turn
        "run_shell_command__run_shell_command_1761600000123_1",
        "mcp_crm_list-accounts__mcp_crm_list-accounts_1761600000000_0",  # a tool of an MCP server, as Gemini names it
        # With context management on: synth_{prompt_id}_{ms}_{n}; the prompt id is the session's UUID, interactive
        # (useGeminiStream.ts:1885) with "########" and the prompt's count, a subagent's UUID with "#" and its turn
        # (agents/local-executor.ts:330, 348), or the bare session id headless (gemini.tsx:985).
        "read_file__synth_4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11########3_1761600000000_0",
        "read_file__synth_9b2d7e14-5c3a-4f86-a1e9-0d7c3b5f2a68#2_1761600000000_1",
        "read_file__synth_4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11_1761600000000_0",
        # The model's own id, which Gemini CLI keeps after the tool's name, and which a GenAI client sends back bare.
        "read_file__fc-8a1b2c3d4e5f",
        "fc-8a1b2c3d4e5f",
        "c1",
    ],
)
def test_gemini_cli_s_call_ids_pass_by_its_grammar(call_id):
    name = call_id.split("__", 1)[0]
    body = gemini_cli_turn(call_id)
    for content in body["contents"][1:]:
        part = content["parts"][0]
        (part.get("functionCall") or part["functionResponse"])["name"] = name
    assert _inspect(body).problem is None


def test_a_gemini_cli_turn_leaves_masked_through_the_mediator():
    built = build(Config(), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    raw = json.dumps(gemini_cli_turn()).encode()
    prepared = mediator.prepare(GEMINI, "POST", PATH, {"x-goog-api-key": "AIzaSyFAKE-gemini-0123456789"}, raw)
    assert prepared.mode == REWRITE and prepared.block_reason is None
    sent = json.loads(prepared.body)
    assert EMAIL not in prepared.body.decode()
    assert sent["contents"][1]["parts"][0]["functionCall"]["id"] == CALL_ID  # sent back as Gemini CLI made it
    assert sent["contents"][2]["parts"][0]["functionResponse"]["id"] == CALL_ID


@pytest.mark.parametrize(
    "call_id",
    [
        "read_file__mario.rossi@example.com",
        "read_file__read_file_1761600000000_0 mario.rossi",
        "read_file__synth_mario.rossi@example.com_1761600000000_0",
        "mario.rossi@example.com",
        r"C:\Users\mario.rossi\notes.txt",
        "fc-" + "a" * 126,  # past the bound
    ],
)
def test_a_call_id_outside_the_grammar_is_refused_as_a_correlation_identifier(call_id):
    found = _inspect(gemini_cli_turn(call_id))
    assert found.problem == "request body refused (correlation identifier)"
    assert call_id not in found.problem


def test_a_response_naming_a_call_the_request_does_not_hold_is_refused():
    body = gemini_cli_turn(response_id="read_file__read_file_1761600000999_0")
    assert _inspect(body).problem == "request body refused (correlation reference)"


def test_the_grammar_is_gemini_s_alone():
    """The same id as a Chat Completions tool call id is refused there: only Gemini CLI makes it."""
    call = {"id": CALL_ID, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    body = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "user", "content": "read it"},
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": CALL_ID, "content": "done"},
        ],
    }
    assert _inspect(copy.deepcopy(body), "chat").problem == "request body refused (correlation identifier)"


def test_the_scan_matches_the_response_to_its_call_by_the_id():
    scan = gemini.scan_request(gemini_cli_turn())
    result = [slot for slot in scan.slots if slot.tool == "read_file"]
    assert result and all(slot.get() != CALL_ID for slot in scan.slots)  # the id is never a masked text
