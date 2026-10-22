"""A recognizer drafted from a description, by the local reviewer's model.

The draft is only a starting point: the dashboard shows it only when it passes `latet rules test`
(its pattern compiles in RE2 and its examples come out as they say), and you decide whether to keep
it. The model is asked to invent its examples, never to copy real values.
"""

from __future__ import annotations

import json
import re
from typing import Any

from latet.engine.reviewer.client import ReviewClient, ReviewError

SYSTEM = """You write recognizers for a masking tool. A recognizer finds one kind of value in text with a regular
expression, so that the value can be replaced before the text leaves the computer.

From the user's description, write:
- name: lowercase words with dashes, such as badge-number
- type: an UPPER_SNAKE_CASE label, such as BADGE
- description: one sentence saying what the values are and how they are written
- pattern: a regular expression in RE2 syntax (no lookahead or lookbehind, no backreferences), with \\b around
  the value when it starts and ends with a letter or a digit, as precise as the description allows
- examples.match: three short texts, each containing one value written as described
- examples.no_match: three short texts that look similar but must not match

Invent every example value: never a real one."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string"},
        "description": {"type": "string"},
        "pattern": {"type": "string"},
        "examples": {
            "type": "object",
            "properties": {
                "match": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "no_match": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            },
            "required": ["match", "no_match"],
        },
    },
    "required": ["name", "type", "description", "pattern", "examples"],
}

_NAME = re.compile(r"[^a-z0-9]+")
_TYPE = re.compile(r"[^A-Z0-9]+")


async def draft_recognizer(client: ReviewClient, description: str) -> dict[str, Any]:
    """A recognizer entry for ``description`` (as config.yaml holds one), not checked yet."""
    content = await client.ask(SYSTEM, description.strip()[:2000], SCHEMA)
    try:
        data = json.loads(content)
    except ValueError:
        found = re.search(r"\{.*\}", content, re.S)
        try:
            data = json.loads(found.group(0)) if found else None
        except ValueError:
            data = None
    if not isinstance(data, dict) or not isinstance(data.get("pattern"), str):
        raise ReviewError("the model's draft could not be read")
    found_examples = data.get("examples")
    examples: dict[str, Any] = found_examples if isinstance(found_examples, dict) else {}
    return {
        "name": _NAME.sub("-", str(data.get("name") or "draft").lower()).strip("-")[:60] or "draft",
        "type": _TYPE.sub("_", str(data.get("type") or "DRAFT").upper()).strip("_")[:40] or "DRAFT",
        "description": str(data.get("description") or description).strip()[:300],
        "pattern": data["pattern"],
        "examples": {
            "match": [str(x) for x in examples.get("match") or []][:5],
            "no_match": [str(x) for x in examples.get("no_match") or []][:5],
        },
    }
