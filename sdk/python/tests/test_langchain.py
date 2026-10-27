"""The LangChain extra: a document transformer that masks documents before they are indexed, and runnables that mask a
question and unmask an answer, for a chain or a graph."""

from __future__ import annotations

import pytest
from langchain_core.documents import BaseDocumentTransformer, Document
from langchain_core.runnables import Runnable, RunnableLambda

from latet_client import Blocked
from latet_client.langchain import LatetMasker, mask_runnable, unmask_runnable
from latet_stand_in import TOKEN

MARIO = "mario.rossi@acme.it"
ANNA = "anna.verdi@acme.it"


def _documents():
    return [
        Document(page_content=f"Invoice 12 was paid by {MARIO}.", metadata={"source": "a.txt"}, id="a"),
        Document(page_content="Nothing private in this one.", metadata={"source": "b.txt"}),
        Document(page_content=f"{ANNA} and {MARIO} wrote about it.", metadata={"source": "c.txt", "latet_counts": 9}),
    ]


def test_langchain_masks_documents_and_raises_on_blocked(fake):
    documents = _documents()
    with fake.client() as client:
        masker = LatetMasker(client)
        assert isinstance(masker, BaseDocumentTransformer)
        masked = masker.transform_documents(documents)
    assert [d.page_content for d in masked] == [
        "Invoice 12 was paid by person1@masked.test.",
        "Nothing private in this one.",
        "person2@masked.test and person1@masked.test wrote about it.",
    ]
    assert [d.metadata["source"] for d in masked] == ["a.txt", "b.txt", "c.txt"]
    assert [d.metadata["latet_counts"] for d in masked] == [
        {"EMAIL": 1},
        {},
        {"EMAIL": 2},
    ]  # the call's, over a leftover
    assert masked[0].id == "a"
    assert documents == _documents()  # the documents it was given are as they were
    assert len(fake.calls) == 1 and fake.calls[0].body["kind"] == "prose"

    documents[1] = Document(page_content="BLOCK this one", metadata={"source": "b.txt"})
    with fake.client() as client, pytest.raises(Blocked) as raised:
        LatetMasker(client).transform_documents(documents)
    assert raised.value.index == 1 and "the local reviewer stopped this text" in raised.value.reason
    assert (
        str(raised.value).startswith("text 1 ") and TOKEN not in str(raised.value) and "BLOCK" not in str(raised.value)
    )


def test_data_documents(fake):
    with fake.client() as client:
        LatetMasker(client, kind="data").transform_documents(_documents())
    assert fake.calls[0].body["kind"] == "data"


def test_no_documents_make_no_call(fake):
    with fake.client() as client:
        assert LatetMasker(client).transform_documents([]) == []
    assert fake.calls == []


def test_the_first_blocked_document_is_the_one_named(fake):
    documents = [Document(page_content="fine"), Document(page_content="BLOCK 1"), Document(page_content="BLOCK 2")]
    with fake.client() as client, pytest.raises(Blocked) as raised:
        LatetMasker(client).transform_documents(documents)
    assert raised.value.index == 1


async def test_documents_masked_with_an_async_client(fake):
    async with fake.aclient() as client:
        masked = await LatetMasker(client).atransform_documents(_documents())
        with pytest.raises(Blocked) as raised:
            await LatetMasker(client).atransform_documents(
                [Document(page_content="ok"), Document(page_content="BLOCK")]
            )
        with pytest.raises(TypeError, match="atransform_documents"):
            LatetMasker(client).transform_documents(_documents())
    assert masked[0].page_content == "Invoice 12 was paid by person1@masked.test." and raised.value.index == 1


async def test_documents_masked_in_a_thread_with_a_sync_client(fake):
    with fake.client() as client:
        masked = await LatetMasker(client).atransform_documents(_documents())
    assert masked[2].metadata["latet_counts"] == {"EMAIL": 2}


def test_runnables(fake):
    with fake.client() as client:
        masking, unmasking = mask_runnable(client), unmask_runnable(client)
        assert isinstance(masking, Runnable) and isinstance(unmasking, Runnable)
        seen = []

        def model(question: str) -> str:
            seen.append(question)
            return f"The answer concerns {question.split()[-1]}"

        chain = masking | RunnableLambda(model) | unmasking
        assert chain.invoke(f"Who is {MARIO}") == f"The answer concerns {MARIO}"
        assert seen == ["Who is person1@masked.test"]  # the model never saw the real value
        assert chain.batch([f"Who is {MARIO}", f"Who is {ANNA}"]) == [
            f"The answer concerns {MARIO}",
            f"The answer concerns {ANNA}",
        ]
        with pytest.raises(Blocked) as raised:
            masking.invoke("BLOCK")
    assert raised.value.index == 0


def test_a_chat_models_message_is_unmasked_as_its_text(fake):
    """The final review's M5: a chat model answers a message (``AIMessage``), not a text: the runnable that unmasks takes
    it and gives its text back restored, as it does behind ``StrOutputParser`` (the module's example). A message whose
    content is not a text (an image's parts) is refused in words that say so."""
    from langchain_core.messages import AIMessage
    from langchain_core.output_parsers import StrOutputParser

    def chat_model(question: str) -> AIMessage:
        return AIMessage(content=f"The answer concerns {question.split()[-1]}")

    with fake.client() as client:
        for chain in (
            mask_runnable(client) | RunnableLambda(chat_model) | unmask_runnable(client),
            mask_runnable(client) | RunnableLambda(chat_model) | StrOutputParser() | unmask_runnable(client),
        ):
            assert chain.invoke(f"Who is {MARIO}") == f"The answer concerns {MARIO}"
        with pytest.raises(TypeError, match="text"):
            unmask_runnable(client).invoke(AIMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}]))


async def test_a_chat_models_message_is_unmasked_as_its_text_by_an_async_client(fake):
    from langchain_core.messages import AIMessage

    async with fake.aclient() as client:
        chain = (
            mask_runnable(client) | RunnableLambda(lambda text: AIMessage(content=f"{text}!")) | unmask_runnable(client)
        )
        assert await chain.ainvoke(f"Who is {MARIO}") == f"Who is {MARIO}!"


def test_the_modules_example_is_a_chain_a_chat_model_fits():
    """The example the module's words give puts the parser that turns a chat model's message into a text first."""
    import latet_client.langchain as module

    assert "model | StrOutputParser() | unmask_runnable(latet)" in (module.__doc__ or "")


def test_a_data_runnable(fake):
    with fake.client() as client:
        mask_runnable(client, kind="data").invoke("some log line")
    assert fake.calls[0].body["kind"] == "data"


async def test_runnables_with_an_async_client(fake):
    async with fake.aclient() as client:
        chain = mask_runnable(client) | RunnableLambda(lambda text: f"{text}!") | unmask_runnable(client)
        assert await chain.ainvoke(f"Who is {MARIO}") == f"Who is {MARIO}!"
        with pytest.raises(Blocked):
            await mask_runnable(client).ainvoke("BLOCK")


async def test_runnables_with_a_sync_client_in_an_async_chain(fake):
    with fake.client() as client:
        chain = mask_runnable(client) | unmask_runnable(client)
        assert await chain.ainvoke(f"Who is {MARIO}") == f"Who is {MARIO}"
