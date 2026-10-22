"""One isolated Claude bridge call. No general CLI, credential discovery, repair, retry or fallback."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

_INPUT = 4 * 1024 * 1024
_OUTPUT = _INPUT - 1
_OPERATIONS = frozenset(
    ("ping", "route-check", "mask", "rehydrate", "scope", "verify", "instructions", "approvals/wait", "outbound")
)


class _Rejected(Exception):
    def __init__(self, code: str):
        self.code = code


def _json(raw: bytes, most: int) -> Any:
    if len(raw) > most:
        raise ValueError
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 64:
                raise ValueError
        elif byte in (93, 125):
            depth -= 1
            if depth < 0:
                raise ValueError

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result or any(0xD800 <= ord(char) <= 0xDFFF for char in key):
                raise ValueError
            result[key] = value
        return result

    def constant(_):
        raise ValueError

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
    pending, count = [value], 1
    while pending:
        current = pending.pop()
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError
        if isinstance(current, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in current):
            raise ValueError
        children = current.values() if isinstance(current, dict) else current if isinstance(current, list) else ()
        for child in children:
            count += 1
            if count > 100_000:
                raise ValueError
            pending.append(child)
    return value


def _deadline(end: float) -> None:
    if time.monotonic() >= end:
        raise _Rejected("deadline")


def _private_chain(folder: Path) -> None:
    """Config-free initial cwd/descriptor admission; no creation, resolution or content reads."""
    from latet.storage import folders

    if not folder.is_absolute() or not re.fullmatch(r"[A-Za-z]:", folder.drive) or ".." in folder.parts:
        raise _Rejected("binding")
    # The data folder is the one the binding's connection/claude-helper is in: it and every folder in it are checked
    # with no trust in another account (M3); the folders above it trust LATET_TRUSTED_ACCOUNTS, read from the registry
    # (R30), and the Administrators' direct members, as `latet agents apply claude` did (M6).
    parts = [part.casefold() for part in folder.parts]
    data = next(
        (
            Path(*folder.parts[:index])
            for index in range(len(parts) - 1)
            if parts[index : index + 2] == ["connection", "claude-helper"]
        ),
        None,
    )
    for parent in reversed((folder, *folder.parents)):
        found = parent.lstat()
        if (
            not stat.S_ISDIR(found.st_mode)
            or stat.S_ISLNK(found.st_mode)
            or getattr(found, "st_file_attributes", 0) & 0x400
        ):
            raise _Rejected("binding")
        above = data is not None and parent in data.parents
        problem = (
            folders.namespace_folder_problem(parent, found)
            if above
            else folders.namespace_folder_problem(parent, found, trust=False)
        )
        if problem is not None:
            raise _Rejected("binding")
    if folders.private_folder_problem(folder) is not None:
        raise _Rejected("binding")


def _body(operation: str, body: Any) -> None:
    if operation == "ping":
        if body is not None:
            raise ValueError
        return
    if not isinstance(body, dict):
        raise ValueError
    allowed = {
        "route-check": {
            "bridge",
            "env",
            "sources",
            "entrypoint",
            "surface",
            "agent",
            "scope",
            "client_bridge",
            "client_agent_base",
            "anthropic_base_url",
        },
        "mask": {"items"},
        "rehydrate": {"items", "json", "model"},
        "scope": {"items"},
        "verify": {"slots", "images", "history"},
        "instructions": set(),
        "approvals/wait": {"id"},
        "outbound": {"tool", "values"},
    }[operation] | {"session", "cwd"}
    if set(body) - allowed or any(key in body and not isinstance(body[key], str) for key in ("session", "cwd")):
        raise ValueError
    if operation in ("mask", "rehydrate", "scope") and not isinstance(body.get("items"), list):
        raise ValueError
    if operation in ("rehydrate", "scope") and any(not isinstance(item, str) for item in body["items"]):
        raise ValueError
    if operation == "mask":
        for item in body["items"]:
            if not isinstance(item, dict) or set(item) - {
                "text",
                "image",
                "kind",
                "from_model",
                "json",
                "tool",
                "path",
            }:
                raise ValueError
            if not any(key in item for key in ("text", "image")):
                raise ValueError
            if any(key in item and not isinstance(item[key], str) for key in ("text", "kind", "tool", "path")):
                raise ValueError
            if any(key in item and type(item[key]) is not bool for key in ("from_model", "json")):
                raise ValueError
            if "image" in item:
                image = item["image"]
                if (
                    not isinstance(image, dict)
                    or set(image) != {"data", "media_type"}
                    or any(not isinstance(v, str) for v in image.values())
                ):
                    raise ValueError
    if operation == "rehydrate" and any(key in body and type(body[key]) is not bool for key in ("json", "model")):
        raise ValueError
    if operation == "verify":
        for field, fields in (("slots", {"text", "kind", "from_model"}), ("images", {"data", "media_type", "kind"})):
            items = body.get(field, [])
            if not isinstance(items, list):
                raise ValueError
            for item in items:
                if (
                    not isinstance(item, dict)
                    or set(item) - fields
                    or any(
                        not isinstance(v, str) if k != "from_model" else type(v) is not bool for k, v in item.items()
                    )
                ):
                    raise ValueError
                if ("text" if field == "slots" else "data") not in item:
                    raise ValueError
        if "history" in body and (type(body["history"]) is not int or body["history"] < 0):
            raise ValueError
    if operation == "approvals/wait" and (not isinstance(body.get("id"), str) or not 0 < len(body["id"]) <= 128):
        raise ValueError
    if operation == "outbound" and (
        not isinstance(body.get("tool"), str)
        or not isinstance(body.get("values"), list)
        or any(not isinstance(value, str) for value in body["values"])
    ):
        raise ValueError
    if operation == "route-check":
        for field in ("env", "sources"):
            if field in body and (
                not isinstance(body[field], dict) or any(not isinstance(v, str) for v in body[field].values())
            ):
                raise ValueError
        if any(
            key in body and body[key] is not None and not isinstance(body[key], str)
            for key in allowed - {"env", "sources"}
        ):
            raise ValueError


def _response(operation: str, value: Any, body: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError
    if operation == "route-check" and type(value.get("ok")) is not bool:
        raise ValueError
    if operation == "ping" and not isinstance(value.get("version"), str):
        raise ValueError
    for field in {
        "scope": ("added",),
        "instructions": ("findings",),
        "verify": ("blocking", "flagged"),
        "outbound": ("found",),
    }.get(operation, ()):
        invalid = (
            type(value.get(field)) is not int or value[field] < 0
            if field == "added"
            else not isinstance(value.get(field), list)
        )
        if invalid:
            raise ValueError
    if operation == "outbound" and value.get("reason") is not None and not isinstance(value["reason"], str):
        raise ValueError
    if operation in ("mask", "approvals/wait"):
        if "pending" in value:
            if not isinstance(value["pending"], str) or not value["pending"]:
                raise ValueError
        elif not isinstance(value.get("items"), list):
            raise ValueError
    if operation == "rehydrate" and (
        not isinstance(value.get("items"), list)
        or len(value["items"]) != len(body["items"])
        or any(not isinstance(item, str) for item in value["items"])
    ):
        raise ValueError


def _run(end: float) -> int:
    phase = "bad_input"
    try:
        if sys.platform != "win32":
            raise _Rejected("unsupported")
        if len(sys.argv) != 3 or sys.argv[1] != "--descriptor":
            raise ValueError
        value = _json(sys.stdin.buffer.read(_INPUT + 1), _INPUT)
        if not isinstance(value, dict) or set(value) != {"v", "op", "bridge", "token", "body"}:
            raise ValueError
        if (
            type(value["v"]) is not int
            or value["v"] != 1
            or not isinstance(value["op"], str)
            or value["op"] not in _OPERATIONS
        ):
            raise ValueError
        if not isinstance(value["token"], str) or re.fullmatch(r"latet_pat_[A-Za-z0-9_-]{43}", value["token"]) is None:
            raise ValueError
        if not isinstance(value["bridge"], str) or len(value["bridge"]) > 2048:
            raise ValueError
        _body(value["op"], value["body"])
        _deadline(end)
        phase = "binding"
        import hashlib
        import hmac

        _deadline(end)
        descriptor = Path(sys.argv[2])
        root, cwd = descriptor.parent, Path.cwd()
        if root.name != "claude-helper" or root.parent.name != "connection" or cwd != root / "cwd":
            raise ValueError
        from latet import claude_bridge

        if not claude_bridge._NAME.fullmatch(descriptor.name):
            raise ValueError
        names = (
            *claude_bridge._EMPTY_ENV,
            "PATH",
            "SystemRoot",
            "WINDIR",
            "HOME",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LATET_KEYRING",
            "PYTHON_KEYRING_BACKEND",
        )
        incoming = {key: os.environ.get(key) for key in names}
        _private_chain(root)
        _private_chain(cwd)
        if any(cwd.iterdir()):
            raise ValueError
        os.environ["LATET_DATA_DIR"] = str(cwd)
        os.environ["LATET_CONNECTION"] = str(cwd / "connection-unavailable.yaml")
        os.environ["LATET_CONFIG"] = str(cwd / "config-unavailable.yaml")
        prepared, digest = claude_bridge._read_binding(descriptor, _json, incoming)
        _deadline(end)
        from dataclasses import replace

        from latet.integrations.routes import _is_space
        from latet.server.tokens import route_key

        assert prepared.connection is not None
        normalized = f"{prepared.connection.url}/u/{route_key(value['token'])}"
        if not _is_space(value["bridge"], normalized) or not hmac.compare_digest(
            hashlib.sha256(normalized.encode("utf-8")).hexdigest(), digest
        ):
            raise ValueError
        prepared = replace(prepared, token=value["token"])
        _deadline(end)
        phase = "unavailable"
        from latet.server.client import ReceiveBudget, _bridge_once

        raw = _bridge_once(prepared, value["op"], value["body"], end, ReceiveBudget(_OUTPUT))
        _deadline(end)
        phase = "response"
        answer = _json(raw, _OUTPUT)
        _response(value["op"], answer, value["body"])
        result = {"v": 1, "ok": True, "response": answer}
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _OUTPUT:
            raise ValueError
        _json(encoded, _OUTPUT)  # Complete stdout's envelope adds depth and value nodes to the network object.
        _deadline(end)
        code = 0
    except Exception as exc:
        error = (
            exc.code
            if isinstance(exc, _Rejected)
            else "deadline"
            if isinstance(exc, TimeoutError) or time.monotonic() >= end
            else "refused"  # the server refused this computer's token: the plugin says what to do (R6, R7)
            if phase == "unavailable" and getattr(exc, "status", None) == 401
            else phase
        )
        encoded = json.dumps({"v": 1, "ok": False, "code": error}, separators=(",", ":")).encode("utf-8")
        code = 2
    try:
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    except (OSError, ValueError):
        return 2  # A closed output pipe cannot receive a failure object; never print its exception.
    return code


def main() -> int:
    end = time.monotonic() + 30.0
    finished = threading.Event()

    def expire() -> None:
        if not finished.wait(max(0.0, end - time.monotonic())):
            # A blocked stdin/stdout/native call cannot cooperate. Partial/no output is a host refusal, never success.
            os._exit(2)

    watchdog = threading.Thread(target=expire, name="latet-helper-deadline", daemon=True)
    watchdog.start()
    try:
        return _run(end)
    finally:
        finished.set()
        watchdog.join()


if __name__ == "__main__":
    raise SystemExit(main())
