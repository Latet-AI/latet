"""The edits Latet makes to each agent's settings, as pure text-to-text functions.

``agents apply`` uses them on the files of this machine; ``agents merge`` applies them to a file read
from stdin, for a Latet running in a container that cannot reach the host's files.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import tomlkit

from latet.integrations import jsonc

OPENCODE_PROVIDERS = ("anthropic", "openai", "openrouter")


def claude_merge(text: str, env: dict[str, str]) -> str:
    """Claude Code's settings.json with these variables in its ``env`` block."""
    data = json.loads(text) if text.strip() else {}
    data.setdefault("env", {}).update(env)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def claude_previous(text: str, keys: Iterable[str]) -> dict[str, str | None]:
    """The values these variables have in settings.json's ``env`` block (None: not set)."""
    data = json.loads(text) if text.strip() else {}
    block = data.get("env") if isinstance(data, dict) else None
    return {key: (block or {}).get(key) for key in keys}


def claude_unmerge(
    text: str,
    keys: Iterable[str],
    previous: Mapping[str, str | None] | None = None,
    written: Mapping[str, str] | None = None,
) -> str:
    """Latet's variables out of the ``env`` block, and yours back where it had replaced them (a
    company's ANTHROPIC_BASE_URL). A variable you changed since keeps your value."""
    data = json.loads(text) if text.strip() else {}
    block = data.setdefault("env", {})
    for key in keys:
        if written is not None and key in written and key in block and block[key] != written[key]:
            continue  # yours since
        block.pop(key, None)
        if previous is not None and previous.get(key) is not None:
            block[key] = previous[key]
    if not block:
        data.pop("env", None)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _node(doc: Any, key: str, create: bool = False) -> tuple[Any, str] | None:
    """The table holding a dotted key (``model_providers.azure.base_url``), and the key's last part."""
    *tables, leaf = key.split(".")
    node = doc
    for name in tables:
        if name not in node:
            if not create:
                return None
            node[name] = tomlkit.table()
        node = node[name]
    return node, leaf


def codex_merge(text: str, overrides: Mapping[str, str]) -> str:
    """Codex's config.toml with these keys pointing at Latet (``openai_base_url``,
    ``model_providers.<id>.base_url``), comments kept."""
    doc = tomlkit.parse(text)
    for key, value in overrides.items():
        found = _node(doc, key, create=True)
        assert found is not None
        found[0][found[1]] = value
    return tomlkit.dumps(doc)


def codex_previous(text: str, keys: Iterable[str]) -> dict[str, str | None]:
    """The values these keys have in a Codex file (None: not set)."""
    doc = tomlkit.parse(text)
    out: dict[str, str | None] = {}
    for key in keys:
        found = _node(doc, key)
        out[key] = str(found[0][found[1]]) if found is not None and found[1] in found[0] else None
    return out


def codex_unmerge(
    text: str, previous: Mapping[str, str | None] | None = None, written: Mapping[str, str] | None = None
) -> str:
    """Latet's addresses out of a Codex file, and yours back where it had replaced them. Without
    ``previous`` (an older install): only an ``openai_base_url`` of Latet's goes."""
    doc = tomlkit.parse(text)
    if previous is None:
        if "openai_base_url" in doc and "127.0.0.1" in str(doc["openai_base_url"]):
            del doc["openai_base_url"]
        return tomlkit.dumps(doc)
    for key, before in previous.items():
        found = _node(doc, key)
        if found is None or found[1] not in found[0]:
            continue
        node, leaf = found
        if written is not None and key in written and str(node[leaf]) != written[key]:
            continue  # yours since
        if before is None:
            del node[leaf]
        else:
            node[leaf] = before
    return tomlkit.dumps(doc)


def opencode_merge(text: str, urls: dict[str, str]) -> str:
    """OpenCode's opencode.json(c) with each provider's ``baseURL`` pointing at Latet."""
    for provider in OPENCODE_PROVIDERS:
        text = jsonc.set_path(text, ["provider", provider, "options", "baseURL"], urls[provider])
    return text


def opencode_unmerge(text: str) -> str:
    for provider in OPENCODE_PROVIDERS:
        text = jsonc.remove_path(text, ["provider", provider, "options", "baseURL"])
    return text
