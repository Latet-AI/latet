"""Public code that holds no secrets and almost no personal data: a yardstick for false positives.

The false-positive budget test runs every detector over it, and user-defined recognizers are
checked against it before they are turned on (a rule that fires all over the standard library
would mask ordinary code).
"""

from __future__ import annotations

import sysconfig
from functools import cache
from pathlib import Path

STDLIB_SAMPLE = (
    "json/__init__.py", "json/decoder.py", "http/client.py", "http/cookies.py", "http/cookiejar.py",
    "email/utils.py", "email/_header_value_parser.py", "urllib/request.py", "urllib/parse.py",
    "smtplib.py", "ftplib.py", "imaplib.py", "poplib.py", "netrc.py", "getpass.py", "secrets.py",
    "hmac.py", "base64.py", "configparser.py", "argparse.py", "logging/__init__.py",
    "logging/handlers.py", "ssl.py", "locale.py", "inspect.py", "subprocess.py", "shutil.py",
    "tarfile.py", "zipfile/__init__.py", "_pydecimal.py", "tokenize.py", "pickle.py",
)  # fmt: skip

# Used when the Python installation ships no sources (some embedded or frozen builds).
_FALLBACK = """
import os, sys, json, hashlib
VERSION = (3, 12, 4)
PORT = 8080
TIMEOUT_SECONDS = 30.0
RETRIES = [1, 2, 4, 8, 16]
def checksum(path: str, chunk: int = 65536) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()
class Order:
    def __init__(self, order_id: int, amount_cents: int, created: str = "2024-01-31T10:15:00Z"):
        self.order_id = order_id
        self.amount_cents = amount_cents
        self.created = created
if __name__ == "__main__":
    print(json.dumps({"id": 123456789, "build": 20240131, "ratio": 0.0123456789}))
"""


def stdlib_files() -> list[Path]:
    root = Path(sysconfig.get_paths()["stdlib"])
    return [root / m for m in STDLIB_SAMPLE if (root / m).is_file()]


@cache
def benign_sample(limit: int = 600_000) -> str:
    """Up to ``limit`` characters of the standard-library sample, or a small built-in snippet."""
    parts: list[str] = []
    size = 0
    for path in stdlib_files():
        text = path.read_text("utf-8", errors="replace")
        parts.append(text)
        size += len(text)
        if size >= limit:
            break
    return "\n".join(parts)[:limit] if parts else _FALLBACK
