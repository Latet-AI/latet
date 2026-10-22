"""The tools a provider ran, found in the history of a request."""

from latet.gateway.outbound import server_calls


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
