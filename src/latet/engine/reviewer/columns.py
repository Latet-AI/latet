"""Strict data mode, guided: the local reviewer's opinion on which columns of a tool's output may go to the
model in clear. Asked only when you ask, in the dashboard; never applied by itself."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from latet.engine.reviewer.client import ReviewClient, ReviewError

if TYPE_CHECKING:
    from latet.columns import Column

SYSTEM = """\
You help decide which columns of a database output (a table, JSON, key=value lines) may be sent in clear
to a cloud model. Each line of the input is one column: its name and a few of its values.

A column may stay in clear when its values say nothing about a person and hold no secret: ids, dates of
events, states, categories, counts, product codes, technical values.
A column must stay masked when its values could name, contact, locate or describe a person (names,
emails, phones, addresses, health, salaries, personal notes), or hold a secret, or are free text that
could hold anything.

Answer for every column, with a short reason (at most twelve words).
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "keep": {"type": "boolean"},
                    "why": {"type": "string"},
                },
                "required": ["name", "keep", "why"],
            },
        }
    },
    "required": ["columns"],
}
_VALUES = 5  # values of each column the reviewer is shown
_LONGEST = 80  # characters of each value


async def opinions(client: ReviewClient, columns: list[Column]) -> dict[str, dict[str, Any]]:
    """For each column, whether the local reviewer would keep it in clear, and why."""
    lines = []
    for column in columns:
        values = [v[:_LONGEST] for v in list(dict.fromkeys(column.values))[:_VALUES]]
        lines.append(json.dumps({"column": column.name, "values": values}, ensure_ascii=False))
    content = await client.ask(SYSTEM, "\n".join(lines), SCHEMA)
    try:
        data = json.loads(content)
    except ValueError:
        found = re.search(r"\{.*\}", content, re.S)
        try:
            data = json.loads(found.group(0)) if found else None
        except ValueError:
            data = None
    if not isinstance(data, dict) or not isinstance(data.get("columns"), list):
        raise ReviewError("the model's opinion could not be read")
    names = {c.name for c in columns}
    out: dict[str, dict[str, Any]] = {}
    for item in data["columns"]:
        if isinstance(item, dict) and item.get("name") in names and isinstance(item.get("keep"), bool):
            out[str(item["name"])] = {"keep": item["keep"], "why": str(item.get("why") or "").strip()[:200]}
    return out
