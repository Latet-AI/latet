---
title: Mask documents for RAG
description: A LangChain or LangGraph pipeline indexes masked documents, asks the model with a masked question and unmasks its answer.
sidebar:
  label: RAG
---

A RAG application sends its documents twice: once to be indexed (to an embeddings service and a
vector store), and again, in pieces, to the model with each question. With the
[applications API](../), the documents are masked before they are indexed, the question is masked
before it is asked, and the model's answer is unmasked for the person who asked. The embeddings
service, the store and the model see fakes only; the person reads the real values.

It works because the fakes come from one API key's vault: a value has the same fake in every
document and in every question, so retrieval finds the documents of the customer a question names,
and the model can tell that two documents speak of the same person.

## The flow

1. **Index** the documents masked, with a token that may mask.
2. **Mask** the question with the same key.
3. **Retrieve** with the masked question, and **ask the model** with the masked question and the
   masked documents. Call the model directly, with your own provider key: not through Latet's
   gateway, which would mask the fakes again.
4. **Unmask** the model's answer with a token that may unmask, and show it to the person.

```sh
latet server --server https://latet.acme.lan keys add "Support assistant" --out assistant-admin.key
latet server --server https://latet.acme.lan keys add-token "Support assistant" indexer --mask --out indexer.token
latet server --server https://latet.acme.lan keys add-token "Support assistant" assistant --mask --unmask --out assistant.token
```

The indexing job holds `indexer.token`; the application that answers holds `assistant.token`.

## With LangChain

`pip install "latet-client[langchain]"` adds a document transformer and two runnables to the Python
client:

```python
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from latet_client import Latet
from latet_client.langchain import LatetMasker, mask_runnable, unmask_runnable

latet = Latet("https://latet.acme.lan", Path("assistant.token").read_text().strip())

# Index the documents masked: the embeddings service and the store never see a real value.
documents = [
    Document(page_content="Ticket 4411: mario.rossi@acme.it asks for a refund to IT60X0542811101000000123456."),
]
store = InMemoryVectorStore(OpenAIEmbeddings())
store.add_documents(LatetMasker(latet).transform_documents(documents))

prompt = ChatPromptTemplate.from_template("Answer from these tickets only.\n\n{context}\n\nQuestion: {question}")


def joined(found: list[Document]) -> str:
    return "\n\n".join(document.page_content for document in found)


# Mask the question, retrieve with it, ask the model directly, unmask its answer.
chain = (
    mask_runnable(latet)
    | {"context": store.as_retriever() | joined, "question": RunnablePassthrough()}
    | prompt
    | ChatOpenAI(model="gpt-4.1-mini")
    | StrOutputParser()
    | unmask_runnable(latet)
)

print(chain.invoke("Where should the refund for mario.rossi@acme.it go?"))
```

What each one sees, for example:

| | Text |
| --- | --- |
| The store | `Ticket 4411: tommaso.gallo@margie-6e5a.example asks for a refund to IT17F3945471087320356678487.` |
| The model, asked | `Where should the refund for tommaso.gallo@margie-6e5a.example go?` |
| The model, answering | `The refund goes to IT17F3945471087320356678487.` |
| The person | `The refund goes to IT60X0542811101000000123456.` |

`LatetMasker` returns copies of the documents with the masked text and, in their metadata,
`latet_counts`: how many values of each type were masked. Only the text is masked, not the metadata:
keep sensitive values out of it, or mask them yourself with `latet.mask`. When the server stops a
document (`blocked`), `LatetMasker` raises `Blocked` with its index and returns nothing, so no
document of that batch is indexed unmasked.

With an `AsyncLatet` client, `atransform_documents` and `ainvoke` do the same without blocking.

## With LangGraph

`mask_runnable` takes a string and gives a string; `unmask_runnable` takes a string or a chat model's
message (`AIMessage`) and gives a string. In a graph whose state holds
more than a string, call the client in the nodes:

```python
def take_question(state: State) -> dict:
    return {"question": latet.mask(state["question"]).text}


def give_answer(state: State) -> dict:
    return {"answer": latet.unmask(state["answer"]).text}
```

A masked text is `None` when the server stopped it (`blocked` says why): decide in the node what the
person sees then.

## Things to know

- **One key per collection.** The documents and the questions of one application share a key. Two
  keys give two sets of fakes, and a question masked with the other key does not find its documents.
- **Names** are found by the local name model installed on the server
  ([recognition models on a server](../../concepts/server-operations/#recognition-models-on-a-server)),
  in documents sent as `prose`, the default. Without it, the server's local reviewer, when the key's
  rules set one, finds them best effort; the key's terms catch the names you know.
- **What unmask restores.** The fakes the model writes as they were given, in capitals, or encoded in
  a link; not one it rewrites in another shape, such as an IBAN regrouped in fours. A person's name
  comes back whole only: a first name alone stays as the model wrote it. Ask the model, in the prompt,
  to quote names, addresses and numbers exactly as the documents write them.
- **Index from the originals.** To index a document again, mask its original: the same values get the
  same fakes. Masking a masked copy gives fakes of fakes, which one unmask takes back one step only.
- **Long lists.** A call takes up to 1,000 texts and the server's `apps_request_mb`; the client
  splits a longer list into calls, one after the other. A value that only a later call teaches the
  vault (a name the reviewer finds in one document, say) is not masked in the documents of the calls
  before it: keep the documents that must be masked together in one call. A document of about 20 KB
  takes about 80 to 95 ms to mask and 30 ms to unmask on one instance
  ([the measurements](../#how-fast-it-is)).
- **Who unmasks.** Every unmask is in the server's administration log. A tool that answers many
  people writes one entry for each call; who asked is for the application to log.
