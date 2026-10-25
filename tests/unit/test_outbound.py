"""The tools a provider ran, found in the history of a request, and the stand-ins in what they are given."""

import unicodedata

from latet.engine.masker import Masker
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, Vault
from latet.gateway.outbound import server_calls, stand_ins

from ..conftest import ROOT_KEY

_FAKE = "Niccol\N{LATIN SMALL LETTER O WITH GRAVE} Ferri"


def test_a_fake_written_decomposed_in_a_call_that_runs_elsewhere_is_a_stand_in():
    """A fake with an accent, written decomposed (as macOS writes a file's name) in a web search's query, is a stand-in
    all the same: the call is stopped, as 0.9.8 stopped it."""
    vault = Vault(ROOT_KEY, [Entity("e1", "PERSON", EntityClass.NER, "Mario Rossi", _FAKE, 0, 0.0, "ner")])
    masker = Masker(vault, [])
    query = f"{unicodedata.normalize('NFD', _FAKE)} contatti"
    assert _FAKE not in query
    assert stand_ins(masker, [query]) == ["PERSON"]
    assert stand_ins(masker, [f"{_FAKE} contatti"]) == ["PERSON"]


def test_a_value_that_reads_as_a_fake_written_in_escapes_is_no_stand_in_however_it_is_composed():
    """`Niccol%C3%B2%20Ferri`, a fake kept in a link, reads as a value the vault knows too: that value is no stand-in,
    composed or decomposed (the value wins over the fake's reading), and the fake as written is one."""
    link = Entity("e1", "PERSON", EntityClass.NER, "Mario%20Rossi", "Niccol%C3%B2%20Ferri", 0, 0.0, "ner")
    real = Entity("e2", "CLIENT", EntityClass.CUSTOM, _FAKE, "Lucerne Adatum", 0, 0.0, "term:client")
    masker = Masker(Vault(ROOT_KEY, [link, real]), [])
    for written in (_FAKE, unicodedata.normalize("NFD", _FAKE)):
        assert stand_ins(masker, [f"{written} contatti"]) == []
    assert stand_ins(masker, ["q=Niccol%C3%B2%20Ferri"]) == ["PERSON"]


def test_anthropic_server_tools_and_remote_mcp():
    body = {
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "server_tool_use", "name": "web_search", "input": {"query": "Ada Byron"}},
                    {"type": "mcp_tool_use", "name": "crm_lookup", "input": {"who": {"name": "Ada"}}},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},  # runs here
                ],
            },
        ]
    }
    found = [(tool, [ref.get() for ref in refs]) for tool, refs in server_calls("anthropic", body)]
    assert found == [("web_search", ["Ada Byron"]), ("crm_lookup", ["Ada"])]


def test_responses_web_search_and_mcp_calls():
    body = {
        "input": [
            {"type": "web_search_call", "action": {"type": "search", "query": "Ada Byron"}},
            {"type": "mcp_call", "name": "send_mail", "arguments": '{"to": "ada@example.com"}'},
            {"type": "function_call", "name": "shell", "arguments": "{}"},  # runs here
        ]
    }
    found = [(tool, [ref.get() for ref in refs]) for tool, refs in server_calls("responses", body)]
    assert found == [("web_search", ["search", "Ada Byron"]), ("send_mail", ['{"to": "ada@example.com"}'])]
    assert list(server_calls("chat", body)) == []
