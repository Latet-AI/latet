"""A stand-in for a Latet server's applications API, over ``httpx.MockTransport``: it answers the shape the server
answers (``{"results": [...]}``, ``{"forgotten", "unknown"}`` for a forget, an error as ``{"error": words}``) for any
texts, keeps every call it was made, and gives the answers a test scripts before it behaves. A value, for it, is an
e-mail address; a text that holds ``BLOCK`` is one the local reviewer stopped."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from latet_client import AsyncLatet, Latet

URL = "https://latet.acme.lan"
TOKEN = "latet_key_" + "T0kenOfTheTests" * 3
PREFIX = "/_latet/apps/v1/"
VALUE = re.compile(r"[\w.]+@[\w.]+\w")


@dataclass
class Call:
    operation: str
    headers: httpx.Headers
    content: bytes
    body: dict[str, Any]


def error(status: int, words: str | None = None, **headers: str) -> httpx.Response:
    """What the server answers a refused call: its words in ``error``, or nothing at all."""
    if words is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, json={"error": words}, headers=headers)


@dataclass
class Fake:
    calls: list[Call] = field(default_factory=list)
    scripted: list[httpx.Response] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    fakes: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)  # a chain's batch calls from several threads

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            return self._answer(request)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and PREFIX in request.url.path, request.url
        operation = request.url.path.rpartition("/")[2]
        body = json.loads(request.content)
        self.calls.append(Call(operation, request.headers, request.content, body))
        if self.scripted:
            return self.scripted.pop(0)
        if operation == "forget":
            return httpx.Response(200, json=self._forget(body["values"]))
        results = [getattr(self, f"_{operation}")(text) for text in body["texts"]]
        answer: dict[str, Any] = {"results": results}
        if operation == "mask" and self.degraded:
            answer["degraded"] = self.degraded
        return httpx.Response(200, json=answer)

    def _scan(self, text: str) -> dict[str, Any]:
        found = [(m.start(), m.end()) for m in VALUE.finditer(text)]
        return {"findings": [_place(start, end) for start, end in found], "counts": _counts(found)}

    def _mask(self, text: str) -> dict[str, Any]:
        if "BLOCK" in text:
            return {"blocked": "the local reviewer stopped this text"}
        out, places, last = [], [], 0
        length = 0
        for match in VALUE.finditer(text):
            fake = self.fakes.setdefault(match.group(), f"person{len(self.fakes) + 1}@masked.test")
            before = text[last : match.start()]
            out.append(before)
            length += len(before)
            places.append(_place(length, length + len(fake)))
            out.append(fake)
            length += len(fake)
            last = match.end()
        out.append(text[last:])
        return {"text": "".join(out), "replacements": places, "counts": _counts(places)}

    def _forget(self, values: list[str]) -> dict[str, int]:
        """Each value taken out of the stand-in's vault, its fake with it: how many it held, how many it did not."""
        asked = list(dict.fromkeys(values))
        gone = [value for value in asked if self.fakes.pop(value, None) is not None]
        return {"forgotten": len(gone), "unknown": len(asked) - len(gone)}

    def _unmask(self, text: str) -> dict[str, Any]:
        restored = 0
        for value, fake in self.fakes.items():
            restored += text.count(fake)
            text = text.replace(fake, value)
        return {"text": text, "restored": restored}

    def client(self, **options: Any) -> Latet:
        """A ``Latet`` over this stand-in; ``options`` are its own (``retries``, ``max_bytes``...)."""
        return Latet(URL, TOKEN, client=httpx.Client(transport=httpx.MockTransport(self)), **options)

    def aclient(self, **options: Any) -> AsyncLatet:
        return AsyncLatet(URL, TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(self)), **options)


def _place(start: int, end: int) -> dict[str, Any]:
    return {"type": "EMAIL", "class": "pii", "start": start, "end": end}


def _counts(found: list[Any]) -> dict[str, int]:
    return {"EMAIL": len(found)} if found else {}


def holds(root: Any, needle: str) -> bool:
    """Whether ``needle`` is in anything reachable from ``root`` by attributes, arguments, items, an exception's cause
    and context: what a handler that logs ``vars(error)`` or walks ``error.__cause__`` would see. Frames are not
    walked."""
    wanted = needle.encode()
    seen: set[int] = set()
    stack = [root]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, str):
            if needle in item:
                return True
        elif isinstance(item, bytes | bytearray):
            if wanted in item:
                return True
        elif isinstance(item, dict):
            stack += [*item.keys(), *item.values()]
        elif isinstance(item, list | tuple | set | frozenset):
            stack += list(item)
        elif isinstance(item, int | float | type(None) | type):
            continue
        else:
            stack += list(getattr(item, "__dict__", {}).values())
            if isinstance(item, BaseException):
                stack += [item.args, item.__cause__, item.__context__]
    return False
