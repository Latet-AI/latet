"""Surgical edits of JSON-with-comments files (OpenCode's ``opencode.jsonc``).

Only the edited member changes; comments, formatting and every other member stay byte for byte.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class JSONCError(ValueError):
    pass


@dataclass
class Node:
    kind: str  # object | array | value
    start: int
    end: int  # exclusive
    members: dict[str, Member] = field(default_factory=dict)  # objects only, in order


@dataclass
class Member:
    key: str
    key_start: int
    value: Node
    start: int  # first char of the key
    end: int  # end of the value


class _Parser:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0

    def ws(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n﻿":
                self.i += 1
            elif s.startswith("//", self.i):
                j = s.find("\n", self.i)
                self.i = n if j == -1 else j + 1
            elif s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j == -1:
                    raise JSONCError("unterminated comment")
                self.i = j + 2
            else:
                break

    def string(self) -> str:
        assert self.s[self.i] == '"'
        j = self.i + 1
        while True:
            if j >= len(self.s):
                raise JSONCError("unterminated string")
            c = self.s[j]
            if c == "\\":
                j += 2
                continue
            if c == '"':
                break
            j += 1
        raw = self.s[self.i : j + 1]
        self.i = j + 1
        return json.loads(raw)

    def value(self) -> Node:
        self.ws()
        if self.i >= len(self.s):
            raise JSONCError("unexpected end")
        c = self.s[self.i]
        start = self.i
        if c == "{":
            return self.obj()
        if c == "[":
            self.i += 1
            while True:
                self.ws()
                if self.s[self.i] == "]":
                    self.i += 1
                    return Node("array", start, self.i)
                self.value()
                self.ws()
                if self.s[self.i] == ",":
                    self.i += 1
        if c == '"':
            self.string()
            return Node("value", start, self.i)
        j = self.i
        while j < len(self.s) and self.s[j] not in ",}] \t\r\n/":
            j += 1
        if j == self.i:
            raise JSONCError(f"unexpected {c!r} at {self.i}")
        self.i = j
        return Node("value", start, j)

    def obj(self) -> Node:
        start = self.i
        self.i += 1
        node = Node("object", start, start)
        while True:
            self.ws()
            if self.s[self.i] == "}":
                self.i += 1
                node.end = self.i
                return node
            if self.s[self.i] != '"':
                raise JSONCError(f"expected a key at {self.i}")
            key_start = self.i
            key = self.string()
            self.ws()
            if self.s[self.i] != ":":
                raise JSONCError(f"expected ':' at {self.i}")
            self.i += 1
            value = self.value()
            node.members[key] = Member(key, key_start, value, key_start, value.end)
            self.ws()
            if self.s[self.i] == ",":
                self.i += 1


def parse(text: str) -> Node:
    p = _Parser(text)
    root = p.value()
    if root.kind != "object":
        raise JSONCError("the top level is not an object")
    return root


def loads(text: str) -> Any:
    """Plain data from JSONC (comments and trailing commas removed)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    cleaned = "".join(out)
    import re

    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return json.loads(cleaned or "{}")


def _indent_of(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    j = line_start
    while j < len(text) and text[j] in " \t":
        j += 1
    return text[line_start:j]


def _render(value: Any, indent: str, step: str) -> str:
    if isinstance(value, dict):
        if not value:
            return "{}"
        inner = indent + step
        parts = [f"{inner}{json.dumps(k)}: {_render(v, inner, step)}" for k, v in value.items()]
        return "{\n" + ",\n".join(parts) + "\n" + indent + "}"
    return json.dumps(value, ensure_ascii=False)


def set_path(text: str, path: list[str], value: Any) -> str:
    """``text`` with the member at ``path`` set to ``value`` (created, with parents, if missing)."""
    if not text.strip():
        text = "{\n}\n"
    node = parse(text)
    step = "  "
    for depth, key in enumerate(path):
        member = node.members.get(key)
        last = depth == len(path) - 1
        if member is not None and (last or member.value.kind == "object"):
            if last:
                indent = _indent_of(text, member.start)
                return text[: member.value.start] + _render(value, indent, step) + text[member.value.end :]
            node = member.value
            continue
        # Insert the missing member (and what is below it) at the end of this object.
        remaining = value
        for k in reversed(path[depth + 1 :]):
            remaining = {k: remaining}
        close = node.end - 1  # position of "}"
        body = text[node.start + 1 : close]
        if node.members:
            last_member = list(node.members.values())[-1]
            indent = _indent_of(text, last_member.start)
            rendered = f"{json.dumps(key)}: {_render(remaining, indent, step)}"
            insert_at = last_member.end
            return text[:insert_at] + f",\n{indent}{rendered}" + text[insert_at:]
        parent_indent = _indent_of(text, node.start)
        indent = parent_indent + step
        rendered = f"{json.dumps(key)}: {_render(remaining, indent, step)}"
        stripped = body.strip()
        if stripped:  # only comments inside
            return text[:close].rstrip() + f"\n{indent}{rendered}\n{parent_indent}" + text[close:]
        return text[: node.start + 1] + f"\n{indent}{rendered}\n{parent_indent}" + text[close:]
    return text


def remove_path(text: str, path: list[str]) -> str:
    """``text`` without the member at ``path`` (no-op when absent)."""
    node = parse(text)
    for key in path[:-1]:
        member = node.members.get(key)
        if member is None or member.value.kind != "object":
            return text
        node = member.value
    member = node.members.get(path[-1])
    if member is None:
        return text
    members = list(node.members.values())
    index = members.index(member)
    start, end = member.start, member.end
    after = text[end:]
    stripped = after.lstrip(" \t")
    if stripped.startswith(","):
        end += len(after) - len(stripped) + 1
    elif index > 0:
        prev = members[index - 1]
        between = text[prev.end : start]
        comma = between.find(",")
        if comma != -1:
            start = prev.end + comma
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip() == "":
        start = line_start
        if text[end : end + 1] == "\n":
            end += 1
    return text[:start] + text[end:]
