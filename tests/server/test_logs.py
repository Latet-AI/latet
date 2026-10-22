"""What a server's log shows of PostgreSQL's errors: their types and their frames, never the value the driver quotes, when
a caller logs one (``log.exception``) and when uvicorn logs an application's error it did not catch (a unique violation
quotes the value that was there already; a foreign key's, the key that was not)."""

import io
import json
import logging

import psycopg
import pytest

from latet.server.identities import Identities
from latet.server.logs import configure_logging, saved

from .conftest import CREDENTIALS_KEY

pytestmark = pytest.mark.server

EMAIL = "emily.carter@acme.com"


@pytest.fixture
def logs():
    with saved():
        stream = io.StringIO()
        configure_logging("json", "info", stream=stream)
        yield stream


def _logged(stream):
    """What a caller and uvicorn log of the error being handled: the two lines, read back once the log's writer wrote
    them."""
    logging.getLogger("latet.server.admin").exception("an administration call failed")
    logging.getLogger("uvicorn.error").exception("Exception in ASGI application\n")
    logging.getLogger().handlers[0].flush()
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_a_second_person_with_the_same_external_id_is_logged_by_the_type_of_the_violation(db, bus, clock, logs):
    identities = Identities(db, bus, key=CREDENTIALS_KEY, clock=clock)
    identities.add_person("Emily Carter", EMAIL, external_id=EMAIL)
    try:  # no e-mail address the second time: an enabled person's address is refused before the insert (EmailTaken)
        identities.add_person("Emily Carter", external_id=EMAIL)
    except psycopg.errors.UniqueViolation as exc:
        detail = exc.diag.message_detail
        lines = _logged(logs)
    else:
        pytest.fail("the same external id was taken twice")
    assert EMAIL in str(detail)  # the driver's words quote it
    assert [line["exception"].rsplit("\n", 1)[-1] for line in lines] == ["psycopg.errors.UniqueViolation"] * 2
    assert EMAIL not in logs.getvalue() and "already exists" not in logs.getvalue()


def test_a_code_for_a_person_removed_meanwhile_is_logged_by_the_type_of_the_violation(db, bus, clock, logs):
    identities = Identities(db, bus, key=CREDENTIALS_KEY, clock=clock)
    person, _ = identities.add_person("Emily Carter", EMAIL)
    assert identities.remove(person.id)  # on another instance, say
    try:  # a code written for her as it was before Identities read her row first (C-05, the end-of-plan wave's W2)
        with db.connection() as conn:
            conn.execute("INSERT INTO codes (hash, identity_id, expires, tag) VALUES ('h', %s, 0, 't')", (person.id,))
    except psycopg.errors.ForeignKeyViolation as exc:
        detail = exc.diag.message_detail
        lines = _logged(logs)
    else:
        pytest.fail("a code was made for a person no longer there")
    assert person.id in str(detail)  # the driver's words quote the key that is not there
    assert [line["exception"].rsplit("\n", 1)[-1] for line in lines] == ["psycopg.errors.ForeignKeyViolation"] * 2
    assert person.id not in logs.getvalue() and "is not present" not in logs.getvalue()
