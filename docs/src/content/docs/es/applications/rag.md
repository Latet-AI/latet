---
title: Enmascarar documentos para RAG
description: Un pipeline de LangChain o LangGraph indexa documentos enmascarados, pregunta al modelo con una pregunta enmascarada y restaura su respuesta.
sidebar:
  label: RAG
---

Una aplicación RAG envía sus documentos dos veces: una para indexarlos (a un servicio de embeddings y
a un almacén vectorial), y otra, por fragmentos, al modelo con cada pregunta. Con la
[API para aplicaciones](../), los documentos se enmascaran antes de indexarlos, la pregunta antes de
hacerla, y la respuesta del modelo se restaura para la persona que preguntó. El servicio de
embeddings, el almacén y el modelo solo ven valores ficticios; la persona lee los valores reales.

Funciona porque los valores ficticios vienen de la caja fuerte de una sola clave de API: un valor
tiene el mismo valor ficticio en cada documento y en cada pregunta, así que la búsqueda encuentra los
documentos del cliente que nombra una pregunta, y el modelo entiende que dos documentos hablan de la
misma persona.

## El flujo

1. **Indexa** los documentos enmascarados, con un token que puede enmascarar.
2. **Enmascara** la pregunta con la misma clave.
3. **Busca** con la pregunta enmascarada, y **pregunta al modelo** con la pregunta y los documentos
   enmascarados. Llama al modelo directamente, con tu propia clave del proveedor: no a través de la
   puerta de enlace de Latet, que volvería a enmascarar los valores ficticios.
4. **Restaura** la respuesta del modelo con un token que puede restaurar, y muéstrasela a la persona.

```sh
latet server --server https://latet.acme.lan keys add "Support assistant" --out assistant-admin.key
latet server --server https://latet.acme.lan keys add-token "Support assistant" indexer --mask --out indexer.token
latet server --server https://latet.acme.lan keys add-token "Support assistant" assistant --mask --unmask --out assistant.token
```

El trabajo de indexación tiene `indexer.token`; la aplicación que responde tiene `assistant.token`.

## Con LangChain

`pip install "latet-client[langchain]"` añade al cliente de Python un transformador de documentos y dos
runnables:

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
    Document(page_content="Ticket 4411: mario.rossi@acme.it pide un reembolso a IT60X0542811101000000123456."),
]
store = InMemoryVectorStore(OpenAIEmbeddings())
store.add_documents(LatetMasker(latet).transform_documents(documents))

prompt = ChatPromptTemplate.from_template("Responde solo a partir de estos tickets.\n\n{context}\n\nPregunta: {question}")


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

print(chain.invoke("¿Adónde debe ir el reembolso de mario.rossi@acme.it?"))
```

Lo que ve cada uno, por ejemplo:

| | Texto |
| --- | --- |
| El almacén | `Ticket 4411: tommaso.gallo@margie-6e5a.example pide un reembolso a IT17F3945471087320356678487.` |
| El modelo, al recibir la pregunta | `¿Adónde debe ir el reembolso de tommaso.gallo@margie-6e5a.example?` |
| El modelo, al responder | `El reembolso va a IT17F3945471087320356678487.` |
| La persona | `El reembolso va a IT60X0542811101000000123456.` |

`LatetMasker` devuelve copias de los documentos con el texto enmascarado y, en sus metadatos,
`latet_counts`: cuántos valores de cada tipo se enmascararon. Solo se enmascara el texto, no los
metadatos: deja fuera de ellos los valores sensibles, o enmascáralos tú con `latet.mask`. Cuando el
servidor bloquea un documento (`blocked`), `LatetMasker` lanza `Blocked` con su índice y no devuelve
nada, así que ningún documento de ese lote se indexa sin enmascarar.

Con un cliente `AsyncLatet`, `atransform_documents` y `ainvoke` hacen lo mismo sin bloquear.

## Con LangGraph

`mask_runnable` toma una cadena y devuelve una cadena; `unmask_runnable` toma una cadena o el mensaje
de un modelo de chat (`AIMessage`) y devuelve una cadena. En un grafo cuyo estado
contiene más que una cadena, llama al cliente en los nodos:

```python
def take_question(state: State) -> dict:
    return {"question": latet.mask(state["question"]).text}


def give_answer(state: State) -> dict:
    return {"answer": latet.unmask(state["answer"]).text}
```

Un texto enmascarado es `None` cuando el servidor lo bloqueó (`blocked` dice por qué): decide en el
nodo qué ve entonces la persona.

## Conviene saber

- **Una clave por colección.** Los documentos y las preguntas de una aplicación comparten una clave.
  Dos claves dan dos juegos de valores ficticios, y una pregunta enmascarada con la otra clave no
  encuentra sus documentos.
- **Los nombres** los encuentra el modelo de nombres local instalado en el servidor
  ([modelos de reconocimiento en un servidor](../../concepts/server-operations/#modelos-de-reconocimiento-en-un-servidor)),
  en los documentos enviados como `prose`, el valor predeterminado. Sin él, el revisor local del
  servidor, cuando las reglas de la clave indican uno, los encuentra lo mejor que puede; los términos
  de la clave atrapan los nombres que conoces.
- **Lo que restaura `unmask`.** Los valores ficticios que el modelo escribe tal como se le dieron, en
  mayúsculas, o codificados en un enlace; no uno que reescribe de otra forma, como un IBAN agrupado de
  cuatro en cuatro. El nombre de una persona solo vuelve entero: un nombre de pila solo se queda como
  lo escribió el modelo. Pide al modelo, en el prompt, que cite nombres, direcciones y números
  exactamente como los escriben los documentos.
- **Indexa a partir de los originales.** Para volver a indexar un documento, enmascara su original:
  los mismos valores reciben los mismos valores ficticios. Enmascarar una copia ya enmascarada da
  valores ficticios de valores ficticios, que una sola restauración solo devuelve un paso atrás.
- **Listas largas.** Una llamada admite hasta 1.000 textos y el `apps_request_mb` del servidor; el
  cliente divide una lista más larga en varias llamadas, una tras otra. Un valor que solo enseña a la
  caja fuerte una llamada posterior (por ejemplo, un nombre que el revisor encuentra en un documento)
  no se enmascara en los documentos de las llamadas anteriores: mantén en una sola llamada los
  documentos que deben enmascararse juntos. Un documento de unos 20 KB tarda de 80 a 95 ms en
  enmascararse y 30 ms en restaurarse en una instancia ([las mediciones](../#velocidad)).
- **Quién restaura.** Cada restauración está en el registro de administración del servidor. Una
  herramienta que responde a muchas personas escribe una entrada por llamada; quién preguntó le toca
  registrarlo a la aplicación.
