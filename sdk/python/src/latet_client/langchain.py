"""The LangChain extra (``pip install "latet-client[langchain]"``): :class:`LatetMasker`, a document transformer that
masks documents before they are indexed, and :func:`mask_runnable` and :func:`unmask_runnable`, runnables that mask a
question and restore an answer, for a chain or a node of a LangGraph graph.

    masked = LatetMasker(latet).transform_documents(documents)  # before indexing them
    # the model sees fakes, the person reads values
    chain = mask_runnable(latet) | model | StrOutputParser() | unmask_runnable(latet)

A chat model answers a message (``AIMessage``): ``StrOutputParser`` (``langchain_core.output_parsers``) gives its text,
and ``unmask_runnable`` takes the message too, and gives its text back restored.

Only a document's text is masked: its metadata is kept as it is, and holds ``latet_counts``, how many values of each
type were masked in the text."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

try:
    from langchain_core.documents import BaseDocumentTransformer, Document
    from langchain_core.messages import BaseMessage
    from langchain_core.runnables import Runnable, RunnableLambda
except ImportError as exc:  # pragma: no cover - the message is the point
    raise ImportError('latet_client.langchain needs LangChain\'s core: pip install "latet-client[langchain]"') from exc

from .client import AsyncLatet, Kind, Latet
from .errors import Blocked
from .results import MaskResult

__all__ = ["Blocked", "LatetMasker", "mask_runnable", "unmask_runnable"]

COUNTS = "latet_counts"  # the metadata a masked document gets


def _masked(results: Sequence[MaskResult]) -> list[str]:
    """The masked texts; ``Blocked`` for the first the server stopped."""
    texts = []
    for index, result in enumerate(results):
        if result.text is None:
            raise Blocked(index, result.blocked or "the server did not answer this text")
        texts.append(result.text)
    return texts


class LatetMasker(BaseDocumentTransformer):
    """Documents masked by a Latet server, for a pipeline that indexes them: the same value gets the same fake in every
    document, today and in a year, and the question that asks about it (``mask_runnable``) the same one.

    ``transform_documents`` returns copies with the masked text and ``metadata["latet_counts"]``, and leaves the
    documents it was given as they are. A document the server stopped raises :class:`Blocked` with its index and
    nothing is returned, so no document of that batch is indexed unmasked.

    ``client`` is a :class:`Latet` for ``transform_documents`` and ``atransform_documents`` (which then runs it in a
    thread), or an :class:`AsyncLatet` for ``atransform_documents`` alone. ``kind`` is ``prose`` for text people wrote,
    ``data`` for logs or records."""

    def __init__(self, client: Latet | AsyncLatet, *, kind: Kind = "prose") -> None:
        self.client = client
        self.kind: Kind = kind

    def transform_documents(self, documents: Sequence[Document], **kwargs: Any) -> Sequence[Document]:
        if not isinstance(self.client, Latet):
            raise TypeError("this masker has an AsyncLatet: use atransform_documents")
        results = self.client.mask([document.page_content for document in documents], kind=self.kind)
        return self._copies(documents, results)

    async def atransform_documents(self, documents: Sequence[Document], **kwargs: Any) -> Sequence[Document]:
        if not isinstance(self.client, AsyncLatet):
            return await super().atransform_documents(documents, **kwargs)  # the sync client, in a thread
        results = await self.client.mask([document.page_content for document in documents], kind=self.kind)
        return self._copies(documents, results)

    @staticmethod
    def _copies(documents: Sequence[Document], results: Sequence[MaskResult]) -> list[Document]:
        texts = _masked(results)
        return [
            document.model_copy(
                update={
                    "page_content": text,
                    "metadata": {**document.metadata, COUNTS: dict(result.counts)},
                }
            )
            for document, text, result in zip(documents, texts, results, strict=True)
        ]


def mask_runnable(client: Latet | AsyncLatet, *, kind: Kind = "prose") -> Runnable[str, str]:
    """A runnable from a text to the same text masked, for the question of a RAG chain. A text the server stopped
    raises :class:`Blocked`. With an :class:`AsyncLatet` it can only be awaited (``ainvoke``)."""
    if isinstance(client, AsyncLatet):

        async def amask(text: str) -> str:
            return _masked([await client.mask(text, kind=kind)])[0]

        return RunnableLambda(amask, name="latet_mask")

    def mask(text: str) -> str:
        return _masked([client.mask(text, kind=kind)])[0]

    return RunnableLambda(mask, name="latet_mask")


def _text(answer: str | BaseMessage) -> str:
    """The text of a model's answer: a text as it is, a chat model's message (``AIMessage``) by its content, which must
    be a text (a message of several parts, an image's, raises TypeError)."""
    if isinstance(answer, BaseMessage):
        if not isinstance(answer.content, str):
            raise TypeError("unmask_runnable takes a message whose content is a text: put StrOutputParser() before it")
        return answer.content
    return answer


def unmask_runnable(client: Latet | AsyncLatet) -> Runnable[str | BaseMessage, str]:
    """A runnable from a text to the same text with every fake of the API key's vault replaced by its value, for the
    answer of a model that was shown fakes: a text, or a chat model's message, whose text it gives back restored. With
    an :class:`AsyncLatet` it can only be awaited (``ainvoke``)."""
    if isinstance(client, AsyncLatet):

        async def aunmask(answer: str | BaseMessage) -> str:
            return (await client.unmask(_text(answer))).text

        return RunnableLambda(aunmask, name="latet_unmask")

    def unmask(answer: str | BaseMessage) -> str:
        return client.unmask(_text(answer)).text

    return RunnableLambda(unmask, name="latet_unmask")
