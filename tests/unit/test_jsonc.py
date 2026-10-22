from latet.integrations import jsonc

DOC = """{
  // global OpenCode config
  "$schema": "https://opencode.ai/config.json",
  "model": "anthropic/claude", /* trailing comment */
  "provider": {
    "openai": {
      "options": { "apiKey": "{env:OPENAI_API_KEY}" }
    },
  },
}
"""


def test_loads_ignores_comments_and_trailing_commas():
    data = jsonc.loads(DOC)
    assert data["model"] == "anthropic/claude"
    assert data["provider"]["openai"]["options"]["apiKey"] == "{env:OPENAI_API_KEY}"


def test_set_existing_nested_path_keeps_everything_else():
    out = jsonc.set_path(DOC, ["provider", "openai", "options", "baseURL"], "http://127.0.0.1:5283/openai/v1")
    assert "// global OpenCode config" in out
    assert "/* trailing comment */" in out
    data = jsonc.loads(out)
    assert data["provider"]["openai"]["options"] == {
        "apiKey": "{env:OPENAI_API_KEY}",
        "baseURL": "http://127.0.0.1:5283/openai/v1",
    }


def test_set_creates_missing_parents_and_remove_restores():
    out = jsonc.set_path(DOC, ["provider", "anthropic", "options", "baseURL"], "http://127.0.0.1:5283/anthropic/v1")
    data = jsonc.loads(out)
    assert data["provider"]["anthropic"] == {"options": {"baseURL": "http://127.0.0.1:5283/anthropic/v1"}}
    back = jsonc.remove_path(out, ["provider", "anthropic"])
    assert jsonc.loads(back) == jsonc.loads(DOC)
    assert "/* trailing comment */" in back


def test_replace_value_in_place():
    out = jsonc.set_path(DOC, ["model"], "openai/gpt")
    assert jsonc.loads(out)["model"] == "openai/gpt"
    assert out.count("\n") == DOC.count("\n")


def test_empty_and_minimal_files():
    assert jsonc.loads(jsonc.set_path("", ["a", "b"], 1)) == {"a": {"b": 1}}
    assert jsonc.loads(jsonc.set_path("{}", ["a"], "x")) == {"a": "x"}
    assert jsonc.loads(jsonc.set_path('{ "k": 1 }', ["z"], 2)) == {"k": 1, "z": 2}
