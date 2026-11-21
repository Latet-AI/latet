"""What a server's Enterprise stores tell the other instances on Valkey: a Valkey that does not answer is said by its
type, never by its words, and never stops the change. No service is needed."""

import logging
from types import SimpleNamespace

from latet.ee.server import publish


def test_valkey_not_answering_is_logged_by_its_type_and_never_raised(caplog):
    def refuse(channel, message):
        raise ConnectionError("redis://:latet-tests-secret@10.0.0.5:6379 refused the connection")

    with caplog.at_level(logging.WARNING, logger="latet.ee.server"):
        publish(SimpleNamespace(publish=refuse), "ee:settings", {"section": "demo"})
    [record] = caplog.records
    assert "ConnectionError" in record.getMessage() and record.exc_info is None
    assert "latet-tests-secret" not in caplog.text and "10.0.0.5" not in caplog.text
