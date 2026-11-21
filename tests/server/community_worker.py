"""A server's worker as a test spawns it while the suite runs as the Community Edition alone (the Enterprise branch's
``--without-ee``, ``tests/conftest.py``): a spawned process imports Latet afresh, so it hides latet.ee there too, and its
migrations are none of the test schema's."""

from __future__ import annotations

import sys
from typing import Any


def without_ee() -> bool:
    """Whether this process runs as the Community Edition alone (``--without-ee``: latet.ee marked unimportable)."""
    return "latet.ee" in sys.modules and sys.modules["latet.ee"] is None


def community_worker(*args: Any, **kwargs: Any) -> Any:
    """``workers.worker_main`` with latet.ee unimportable, as in the test that spawned it."""
    sys.modules["latet.ee"] = None  # what tests/conftest.py does in pytest's process
    from latet.server import db

    if "latet.ee.migrations" in db.MIGRATIONS:
        db.MIGRATIONS.remove("latet.ee.migrations")
    from latet.server.workers import worker_main

    return worker_main(*args, **kwargs)
