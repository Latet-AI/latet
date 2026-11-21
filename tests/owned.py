"""What a test makes in the shared test services, named in a journal before it is made.

PostgreSQL and Valkey on this computer are shared by every run of the tests and by other campaigns (fuzzers, a
benchmark): a run stopped by force leaves its tests' schemas, key prefixes, roles, databases and Valkey users behind,
and nothing else says which of them were that run's. When ``LATET_TEST_OWNED`` names a file (the parallel runner names
one per child, beside its reports: ``scripts/test_parallel.md``), each name a test makes for those services is
appended to it, with the service's place, before anything is made under that name.

After a forced stop, what still exists at a record's place under it is that run's own, and nothing else is:
in PostgreSQL a schema, role or database whose name is the record's exactly, or the record's followed by ``_`` (a
schema's ``_decoy`` and ``_b``; a role named like its schema is the exact name); in Valkey the keys that begin with a
prefix's record (which ends with its colon), and a Valkey user of that exact name with the keys under ``<user>:``.
``scripts/test_owned_left.py`` lists them, read and never removed. That rule is safe only because every name is random
and of a shape no other campaign uses: ``owned`` refuses any other shape (``SHAPES``), so that no record can be empty,
short or another's and turn a cleanup into a purge by pattern. A run that passes leaves none of them.

A record holds the name, its kind, the service's host, port and database, the test that made it and its process. Never
an address's user or password, nor its other options: a host that cannot be read as a name or an address (the tail
of a password, when a raw @ ends the user's part early) is written as ``unreadable``.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

OWNED = "LATET_TEST_OWNED"

_A_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*|[0-9A-Fa-f:.]+")  # a name or an address, nothing else
_VALKEY = ("redis://", "rediss://", "valkey://", "valkeys://")

# The exact shapes the tests make, by kind: each random, none another campaign's (`latet_t_keyfuzz_…` is not one).
SHAPES = {
    "schema": re.compile(r"latet_t_[0-9a-f]{10}|t_[0-9a-f]{12}"),  # the `schema` fixture, the TLS one; Instances
    "prefix": re.compile(r"latet_t_[0-9a-f]{10}:|t[0-9a-f]{8}:"),  # the `prefix` fixture; Instances
    "role": re.compile(r"latet_r_[0-9a-f]{8}|latet_t_[0-9a-f]{8}"),  # `role_url` and test_db_start's two
    "database": re.compile(r"latet_t_[0-9a-f]{10}"),  # test_db_start's encodings
    "valkey-user": re.compile(r"latet_t_[0-9a-f]{32}"),  # test_bus's documented ACL
}
POSTGRES_KINDS = frozenset({"schema", "role", "database"})
VALKEY_KINDS = frozenset({"prefix", "valkey-user"})


def shaped(name: object, kind: object) -> bool:
    """Whether ``name`` is exactly a shape the tests make for ``kind``."""
    pattern = SHAPES.get(kind) if isinstance(kind, str) else None
    return pattern is not None and isinstance(name, str) and pattern.fullmatch(name) is not None


def owned(name: str, kind: str, service: str) -> str:
    """``name``, written down first when a journal is named: ``kind`` is what will be made under it (``schema``,
    ``prefix``, ``role``, ``database``, ``valkey-user``) and ``service`` the address it will be made at. A name of
    another shape is refused, journal or not, before anything is written. A journal that cannot be written raises,
    before anything is made: a run that cannot say what it made does not make it."""
    if not shaped(name, kind):
        raise ValueError(f"a {kind!r} name of a shape the tests do not make: {name!r} (tests/owned.py SHAPES)")
    path = os.environ.get(OWNED)
    if path:
        record = {
            "name": name,
            "kind": kind,
            "at": place(service),
            "test": os.environ.get("PYTEST_CURRENT_TEST", ""),
            "pid": os.getpid(),
        }
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            if os.write(descriptor, line) != len(line):
                raise OSError(f"the journal {OWNED} names was written in part")
        finally:
            os.close(descriptor)
    return name


def place(service: str) -> str:
    """Where an address leads, as ``host:port/database`` (PostgreSQL) or ``host:port/index`` (Valkey): never its user,
    its password or its other options; ``unreadable`` when its host cannot be read as a name or an address."""
    try:
        if service.startswith(_VALKEY):
            parts = urlsplit(service)
            host, port, where = parts.hostname or "", parts.port or 6379, parts.path.lstrip("/") or "0"
        else:
            from psycopg.conninfo import conninfo_to_dict

            found = conninfo_to_dict(service)
            host = str(found.get("hostaddr") or found.get("host") or "").split(",")[0]
            port = str(found.get("port") or 5432).split(",")[0]
            where = str(found.get("dbname") or "")
    except Exception:  # an address its driver cannot read: none of its words are kept
        return "unreadable"
    if not _A_HOST.fullmatch(host) or not str(port).isdigit() or not re.fullmatch(r"[A-Za-z0-9_.\-]*", where):
        return "unreadable"
    return f"[{host}]:{port}/{where}" if ":" in host else f"{host}:{port}/{where}"
