"""tests/server/support.py's ``captured_logs``: what the server logs while it lasts, as configure_logging writes it, and
after it every logger, how warnings are shown and the hooks as they were, so that no test's logging reaches another's
(the tests share one process). None of this needs the services."""

import logging
import sys
import threading
import warnings

import pytest

from latet.server import logs as server_logs

from ...server.support import captured_logs

# Every logger configure_logging changes, which ``saved()`` must give back as it was.
_NAMES = ("", "latet", "uvicorn", "uvicorn.error", "uvicorn.access", "py.warnings", *server_logs.LIBRARIES)


def _state():
    loggers = {name: logging.getLogger(name) for name in _NAMES}
    kept = {
        name: (list(found.handlers), found.level, found.propagate, found.disabled) for name, found in loggers.items()
    }
    return kept, (warnings.showwarning, sys.excepthook, threading.excepthook, sys.unraisablehook)


@pytest.mark.parametrize("format", ["json", "text"])
def test_what_the_server_logs_is_captured_and_every_logger_is_as_it_was_after(format):
    # Each logger at a level of its own first, which configure_logging sets otherwise: one not given back shows.
    levels = {name: logging.getLogger(name).level for name in _NAMES}
    try:
        for name in _NAMES:
            logging.getLogger(name).setLevel(5)
        before = _state()
        with captured_logs(format, level="debug") as logs:
            logging.getLogger("latet.server.requests").info("GET /u/bcdefghijklmnopqrstuvwxyz/llm/v1/models 404 0 ms")
            logging.getLogger("latet.t16").debug("a line of Latet's own at the debug level")
            logging.getLogger("websockets.client").debug("> authorization: Bearer sk-provider-0123456789")
        assert _state() == before
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)
    lines = logs.getvalue()
    assert "GET /u/***/llm/v1/models 404" in lines and "a line of Latet's own at the debug level" in lines
    assert "bcdefghijklmnopqrstuvwxyz" not in lines and "sk-provider" not in lines  # a library's debug line: never
