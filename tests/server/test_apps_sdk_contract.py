"""The Python client (``latet-client``, ``sdk/python``) against a Latet server listening on 127.0.0.1: the contract the
SDK reads (the routes, the JSON, the errors), and the two cases the applications API is for, end to end on two
instances of one server. Logs: a mask-only token masks a thousand log lines on one instance and none keeps a value; an
unmask-only token on the other restores one of them, and the administration log has that unmask. RAG: LangChain
documents and a question masked on one instance, a stand-in for the model that answers with their fakes, and the
answer restored on the other. A change to the API that breaks the SDK breaks this module: that change is a ``v2``."""

import contextlib
import socket
from urllib.parse import urlsplit

import httpx
import pytest
from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda
from latet_client import AsyncLatet, ConnectionFailed, Forbidden, ForgetResult, Latet, Unauthorized, VaultFull
from latet_client.langchain import LatetMasker, mask_runnable, unmask_runnable

from latet.server.app import create_server_app

from .support import URL
from .test_two_instances import _on_uvicorn

pytestmark = pytest.mark.server

HOST = urlsplit(URL).hostname  # the name the server answers to: a proxy in front of it would send it
CANARY = "mario.rossi@acme.it"
IBAN = "GB82WEST12345698765432"
PHONE = "+39 347 123 4567"


def _token(server, key_id, name, permissions):
    """Another token of the API key ``key_id``, with ``permissions``: its id and its secret."""
    with server.identities.changing() as changes:
        info, secret = server.identities.add_token(key_id, name, permissions, within=changes)
    return info.id, secret


@contextlib.contextmanager
def _serving(*servers):
    """Each instance served by uvicorn on a port of 127.0.0.1, as `latet server run` serves it: their ports."""
    with contextlib.ExitStack() as stack:
        yield [stack.enter_context(_on_uvicorn(create_server_app(server))) for server in servers]


@contextlib.contextmanager
def _latet(port, token, **options):
    """The SDK's client over 127.0.0.1, naming the server's own host as a proxy in front of it would."""
    with (
        httpx.Client(headers={"host": HOST}, trust_env=False) as http,
        Latet(f"http://127.0.0.1:{port}", token, client=http, **options) as latet,
    ):
        yield latet


def _values_of(text, items):
    return [text[item.start : item.end] for item in items]


def test_the_clients_default_timeout_outlasts_the_servers_bounds_on_postgresql():
    """The final review's M7: a call that waits on PostgreSQL (another instance's values not yet committed, a pool
    exhausted) is bounded by the server (``STATEMENT_SECONDS``, then ``ANSWER_SECONDS``) and answered 503, which the
    client tries again: its default timeout waits longer than both, never giving up first with a ConnectionFailed."""
    import inspect

    from latet.server.db import ANSWER_SECONDS, STATEMENT_SECONDS

    for kind in (Latet, AsyncLatet):
        default = inspect.signature(kind.__init__).parameters["timeout"].default
        assert default > ANSWER_SECONDS > STATEMENT_SECONDS, default


def test_scan_mask_unmask_round_trip(server):
    _, secret = server.identities.add_key("Billing CI")
    texts = [f"write to {CANARY}, IBAN {IBAN}, call {PHONE}", f"from {CANARY}", "nothing here"]
    with _serving(server) as [port], _latet(port, secret) as latet:
        scanned = latet.scan(texts)
        masked = latet.mask(texts)
        restored = latet.unmask([result.text for result in masked])
        one = latet.mask(f"to {CANARY}")
        back = latet.unmask(one.text)
    assert _values_of(texts[0], scanned[0].findings) == [CANARY, IBAN, PHONE]
    assert [(f.type, f.entity_class) for f in scanned[0].findings] == [
        ("EMAIL", "pii"),
        ("IBAN", "pii"),
        ("PHONE", "pii"),
    ]
    assert scanned[0].counts == {"EMAIL": 1, "IBAN": 1, "PHONE": 1} and scanned[2].counts == {}
    assert [r.text for r in restored] == texts and [r.restored for r in restored] == [3, 1, 0]
    for result in masked:
        assert result.blocked is None and result.degraded == ()
        assert not {CANARY, IBAN, PHONE} & {part for part in result.text.replace(",", " ").split()}
    assert [r.type for r in masked[0].replacements] == ["EMAIL", "IBAN", "PHONE"]
    [fake] = _values_of(masked[1].text, masked[1].replacements)
    assert masked[1].text == f"from {fake}" and CANARY not in masked[1].text  # positions are in the text it answers
    assert (
        one.text == f"to {fake}" and back.text == f"to {CANARY}" and back.restored == 1
    )  # the same value, the same fake


def test_a_list_in_several_calls_keeps_its_order(server):
    _, secret = server.identities.add_key("Billing CI")
    lines = [f"line {n}: customer{n}@acme.it asked" for n in range(120)]
    with _serving(server) as [port], _latet(port, secret, max_texts=50) as latet:
        masked = latet.mask(lines, kind="data")
        restored = latet.unmask([result.text for result in masked])
    assert [r.text for r in restored] == lines
    assert all(
        f"customer{n}@acme.it" not in masked[n].text and masked[n].text.startswith(f"line {n}: ") for n in range(120)
    )


async def test_the_async_client(server):
    _, secret = server.identities.add_key("Billing CI")
    with _serving(server) as [port]:
        async with (
            httpx.AsyncClient(headers={"host": HOST}, trust_env=False) as http,
            AsyncLatet(f"http://127.0.0.1:{port}", secret, client=http) as latet,
        ):
            masked = await latet.mask([f"to {CANARY}", "nothing"])
            restored = await latet.unmask(masked[0].text)
    assert CANARY not in masked[0].text and masked[1].text == "nothing"
    assert restored.text == f"to {CANARY}"


def test_a_token_the_server_does_not_know(server):
    server.identities.add_key("Billing CI")
    with (
        _serving(server) as [port],
        _latet(port, "latet_key_" + "x" * 43) as latet,
        pytest.raises(Unauthorized) as raised,
    ):
        latet.mask("hello")
    assert raised.value.status == 401 and raised.value.message == "latet: unauthorized"


def test_a_token_without_the_permission(server):
    key, _ = server.identities.add_key("Billing CI")
    _, scanner = _token(server, key.id, "scanner", ["scan"])
    with _serving(server) as [port], _latet(port, scanner) as latet:
        assert latet.scan(f"to {CANARY}").counts == {"EMAIL": 1}
        with pytest.raises(Forbidden) as raised:
            latet.mask(f"to {CANARY}")
        with pytest.raises(Forbidden):
            latet.unmask("hello")
    assert raised.value.status == 403 and raised.value.message == "this token lacks the mask permission"


def test_a_server_that_is_not_there_is_a_connection_failed_that_holds_no_request():
    """A port nothing listens on: httpx's own error holds the request and its Authorization header, the client's does not."""
    token = "latet_key_" + "x" * 43
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]  # free again once the socket is closed
    with _latet(port, token) as latet, pytest.raises(ConnectionFailed) as raised:
        latet.scan("hello")
    assert raised.value.status == 0 and raised.value.message.startswith("the call to 127.0.0.1 did not go through (")
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert token not in str(raised.value) and token not in repr(raised.value.__dict__)


def test_a_persons_token_is_refused(server):
    _, code = server.identities.add_person("Emily Carter")
    _, laptop, _ = server.identities.enrol(code, "laptop")
    with _serving(server) as [port], _latet(port, laptop) as latet, pytest.raises(Forbidden) as raised:
        latet.mask("hello")
    assert raised.value.message == "latet: the applications API takes an API key's token"


def test_a_full_vault(server):
    server.settings = server.settings.model_copy(update={"vault_limit": 1})
    _, secret = server.identities.add_key("Billing CI")
    with _serving(server) as [port], _latet(port, secret) as latet, pytest.raises(VaultFull) as raised:
        latet.mask([f"IBAN {IBAN}", f"to {CANARY}"])
    assert raised.value.status == 507
    assert (
        raised.value.message == "this API key's vault is full: its limit is 1 values; latet server keys limit raises it"
    )


def test_forget_against_the_server(server):
    """Task 11b: ``forget`` with a token given the permission; the key's first token, without it, is Forbidden; the
    value's old fake restores nothing, and the value gets a new fake."""
    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    customer = "mario.rossi84@gmail.com"  # at a mail provider's: no domain of its own kept beside it
    with _serving(server) as [port]:
        with _latet(port, secret) as latet:
            old = latet.mask(f"to {customer}").text
            with pytest.raises(Forbidden) as raised:
                latet.forget(customer)
            assert raised.value.message == "this token lacks the forget permission"
        with _latet(port, eraser, max_texts=2) as forgetting:
            found = forgetting.forget([customer, "nobody@nowhere.example", "x@y.example", customer])
            assert found == ForgetResult(forgotten=1, unknown=2)
            with pytest.raises(Forbidden):
                forgetting.mask(f"to {customer}")
        with _latet(port, secret) as latet:
            assert latet.unmask(old).restored == 0
            new = latet.mask(f"to {customer}").text
    assert new != old and customer not in new
    # Each value once, two a call: two calls, each in the administration log.
    assert sorted(entry.details["values"] for entry in server.audit.query(action="apps.forgotten")) == [1, 2]


async def test_forget_with_the_async_client(server):
    key, secret = server.identities.add_key("Billing CI")
    _, eraser = _token(server, key.id, "erasure", ["forget"])
    customers = [f"customer.number{n:05d}@gmail.com" for n in range(30)]
    with _serving(server) as [port]:
        with _latet(port, secret) as latet:
            latet.mask([f"login by {customer}" for customer in customers[::2]])
        async with (
            httpx.AsyncClient(headers={"host": HOST}, trust_env=False) as http,
            AsyncLatet(f"http://127.0.0.1:{port}", eraser, client=http, max_texts=7) as latet,
        ):
            found = await latet.forget(customers)
    assert found == ForgetResult(forgotten=15, unknown=15)


def test_logs_case(instances):
    """A mask-only token masks 1,000 log lines on the first instance, none holds a value; an unmask-only token on the
    second unmasks line 500 to the original, and the administration log has one ``apps.unmasked`` entry."""
    first, second = instances
    key, _ = first.identities.add_key("Billing CI")
    _, writer = _token(first, key.id, "log shipper", ["mask"])
    reader_id, reader = _token(first, key.id, "support", ["unmask"])
    mails = [f"customer{n}@acme.it" for n in range(1000)]
    lines = [
        f"2026-10-25 09:{n // 60 % 60:02d}:{n % 60:02d} INFO order {7000 + n} paid by {mails[n]} (retry 0)"
        for n in range(1000)
    ]
    with _serving(first, second) as [one, two]:
        with _latet(one, writer) as shipper:
            masked = shipper.mask(lines, kind="data")
        assert len(masked) == 1000
        assert all(
            result.blocked is None and mail not in result.text for result, mail in zip(masked, mails, strict=True)
        )
        assert all(result.counts == {"EMAIL": 1} for result in masked)
        shipped = "\n".join(result.text for result in masked)
        assert not any(mail in shipped for mail in mails)
        with _latet(two, reader) as support:
            copied = masked[500].text  # the line a person copied from the log service
            found = support.unmask(copied)
            with pytest.raises(Forbidden):  # an unmask-only token masks nothing
                support.mask(copied)
    assert found.text == lines[500] and found.restored == 1
    entries = first.audit.query(action="apps.unmasked")
    assert len(entries) == 1
    assert (entries[0].actor, entries[0].target) == (key.id, key.id)
    assert entries[0].details == {"token": reader_id, "texts": 1, "restored": 1}
    assert mails[500] not in str(entries[0].details) and copied not in str(entries[0].details)


def test_rag_case(instances):
    """LatetMasker masks three LangChain documents and ``mask_runnable`` the question on the first instance; the model
    (a function joining the question and the documents) sees and answers with fakes alone; ``unmask_runnable`` on the
    second gives the answer with the real values."""
    first, second = instances
    key, _ = first.identities.add_key("Support RAG")
    _, indexer = _token(first, key.id, "indexer", ["mask"])
    _, reader = _token(first, key.id, "answers", ["unmask"])
    documents = [
        Document(page_content=f"Invoice 12 was paid by {CANARY} on 3 May.", metadata={"source": "invoices.txt"}),
        Document(page_content=f"Anna Verdi writes that IBAN {IBAN} is the final one.", metadata={"source": "mail.txt"}),
        Document(
            page_content=f"Call {PHONE} to reach the customer {CANARY} about the refund.", metadata={"source": "crm"}
        ),
    ]
    question = f"Who paid invoice 12, and was it {CANARY}?"
    reals = [CANARY, IBAN, PHONE]
    seen = []

    def model(question, documents):
        """What a language model would be sent: it only joins them, as its answer to quote."""
        seen.append([question, *(document.page_content for document in documents)])
        return (
            "Answer for: "
            + question
            + "\n"
            + "\n".join(f"[{d.metadata['source']}] {d.page_content}" for d in documents)
        )

    with _serving(first, second) as [one, two], _latet(one, indexer) as masking, _latet(two, reader) as unmasking:
        masked = LatetMasker(masking).transform_documents(documents)
        chain = mask_runnable(masking) | RunnableLambda(lambda text: model(text, masked)) | unmask_runnable(unmasking)
        answer = chain.invoke(question)
        fake = masking.mask(CANARY).text  # an address alone: its fake, the one the question and the documents share

    [sent] = seen  # the model was called once, with the question and the three documents
    assert answer == model(question, documents)  # the person reads the real values
    assert not any(real in text for text in sent for real in reals)  # the model was shown fakes alone
    assert fake != CANARY and fake in sent[0] and fake in sent[1] and fake in sent[3]
    assert [d.metadata["source"] for d in masked] == ["invoices.txt", "mail.txt", "crm"]
    assert [d.metadata["latet_counts"] for d in masked] == [{"EMAIL": 1}, {"IBAN": 1}, {"PHONE": 1, "EMAIL": 1}]
    assert all(real not in d.page_content for d in masked for real in reals)
    assert (
        documents[0].page_content.startswith("Invoice 12 was paid by mario")
        and "latet_counts" not in documents[0].metadata
    )
