"""The tests of a Latet server's real backends. Without the server's extra (its two drivers, psycopg and redis-py) they are
skipped, saying why, before anything here imports a driver: a computer's install collects the other tests all the same."""

import pytest

pytest.importorskip("psycopg", reason="the server's extra is not installed (uv sync --all-extras installs it)")
pytest.importorskip("redis", reason="the server's extra is not installed (uv sync --all-extras installs it)")
