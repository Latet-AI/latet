---
title: Mascherare i documenti per il RAG
description: Una pipeline LangChain o LangGraph indicizza documenti mascherati, interroga il modello con una domanda mascherata e ripristina la sua risposta.
sidebar:
  label: RAG
---

Un'applicazione RAG invia i suoi documenti due volte: una volta per indicizzarli (a un servizio di
embedding e a un archivio vettoriale), e di nuovo, a pezzi, al modello con ogni domanda. Con l'[API per
le applicazioni](../), i documenti vengono mascherati prima di essere indicizzati, la domanda prima di
essere posta, e la risposta del modello viene ripristinata per la persona che ha chiesto. Il servizio
di embedding, l'archivio e il modello vedono solo valori fittizi; la persona legge i valori reali.

Funziona perché i valori fittizi vengono dalla cassaforte di una sola chiave API: un valore ha lo
stesso valore fittizio in ogni documento e in ogni domanda, così la ricerca trova i documenti del
cliente che una domanda nomina, e il modello capisce che due documenti parlano della stessa persona.

## Il flusso

1. **Indicizza** i documenti mascherati, con un token che può mascherare.
2. **Maschera** la domanda con la stessa chiave.
3. **Cerca** con la domanda mascherata, e **interroga il modello** con la domanda e i documenti
   mascherati. Chiama il modello direttamente, con la tua chiave del fornitore: non attraverso il
   gateway di Latet, che maschererebbe di nuovo i valori fittizi.
4. **Ripristina** la risposta del modello con un token che può ripristinare, e mostrala alla persona.

```sh
latet server --server https://latet.acme.lan keys add "Support assistant" --out assistant-admin.key
latet server --server https://latet.acme.lan keys add-token "Support assistant" indexer --mask --out indexer.token
latet server --server https://latet.acme.lan keys add-token "Support assistant" assistant --mask --unmask --out assistant.token
```

Il job di indicizzazione tiene `indexer.token`; l'applicazione che risponde tiene `assistant.token`.

## Con LangChain

`pip install "latet-client[langchain]"` aggiunge al client Python un trasformatore di documenti e due
runnable:

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
    Document(page_content="Ticket 4411: mario.rossi@acme.it chiede un rimborso su IT60X0542811101000000123456."),
]
store = InMemoryVectorStore(OpenAIEmbeddings())
store.add_documents(LatetMasker(latet).transform_documents(documents))

prompt = ChatPromptTemplate.from_template("Rispondi solo in base a questi ticket.\n\n{context}\n\nDomanda: {question}")


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

print(chain.invoke("Su quale conto va il rimborso per mario.rossi@acme.it?"))
```

Che cosa vede ciascuno, per esempio:

| | Testo |
| --- | --- |
| L'archivio | `Ticket 4411: tommaso.gallo@margie-6e5a.example chiede un rimborso su IT17F3945471087320356678487.` |
| Il modello, quando riceve la domanda | `Su quale conto va il rimborso per tommaso.gallo@margie-6e5a.example?` |
| Il modello, quando risponde | `Il rimborso va su IT17F3945471087320356678487.` |
| La persona | `Il rimborso va su IT60X0542811101000000123456.` |

`LatetMasker` restituisce copie dei documenti con il testo mascherato e, nei loro metadati,
`latet_counts`: quanti valori di ciascun tipo sono stati mascherati. Viene mascherato solo il testo,
non i metadati: tienici fuori i valori sensibili, oppure mascherali tu con `latet.mask`. Quando il
server blocca un documento (`blocked`), `LatetMasker` solleva `Blocked` con il suo indice e non
restituisce nulla, così nessun documento di quel gruppo viene indicizzato senza maschera.

Con un client `AsyncLatet`, `atransform_documents` e `ainvoke` fanno lo stesso senza bloccare.

## Con LangGraph

`mask_runnable` prende una stringa e restituisce una stringa; `unmask_runnable` prende una stringa o il
messaggio di un modello di chat (`AIMessage`) e restituisce una stringa. In un grafo il
cui stato contiene più di una stringa, chiama il client nei nodi:

```python
def take_question(state: State) -> dict:
    return {"question": latet.mask(state["question"]).text}


def give_answer(state: State) -> dict:
    return {"answer": latet.unmask(state["answer"]).text}
```

Un testo mascherato è `None` quando il server lo ha bloccato (`blocked` dice perché): decidi nel nodo
che cosa vede la persona in quel caso.

## Da sapere

- **Una chiave per raccolta.** I documenti e le domande di un'applicazione condividono una chiave. Due
  chiavi danno due serie di valori fittizi, e una domanda mascherata con l'altra chiave non trova i
  suoi documenti.
- **I nomi** vengono trovati dal modello locale dei nomi installato sul server
  ([modelli di riconoscimento su un server](../../concepts/server-operations/#modelli-di-riconoscimento-su-un-server)),
  nei documenti inviati come `prose`, il valore predefinito. Senza di esso, il revisore locale del
  server, quando le regole della chiave ne indicano uno, li trova al meglio delle sue possibilità; i
  termini della chiave colgono i nomi che conosci.
- **Che cosa ripristina `unmask`.** I valori fittizi che il modello scrive così come li ha ricevuti,
  in maiuscolo, o codificati in un link; non uno che riscrive in un'altra forma, come un IBAN
  raggruppato a quattro a quattro. Il nome di una persona torna solo intero: un nome di battesimo da
  solo resta come lo ha scritto il modello. Chiedi al modello, nel prompt, di citare nomi, indirizzi e
  numeri esattamente come li scrivono i documenti.
- **Indicizza dagli originali.** Per indicizzare di nuovo un documento, maschera il suo originale: gli
  stessi valori ricevono gli stessi valori fittizi. Mascherare una copia già mascherata dà valori
  fittizi di valori fittizi, che un solo ripristino riporta indietro di un passo soltanto.
- **Liste lunghe.** Una chiamata accetta fino a 1.000 testi e l'`apps_request_mb` del server; il
  client divide una lista più lunga in più chiamate, una dopo l'altra. Un valore che solo una chiamata
  successiva insegna alla cassaforte (per esempio un nome che il revisore trova in un documento) non
  viene mascherato nei documenti delle chiamate precedenti: tieni in una sola chiamata i documenti che
  vanno mascherati insieme. Un documento di circa 20 KB richiede da 80 a 95 ms per essere mascherato e
  30 ms per essere ripristinato su un'istanza ([le misure](../#quanto-è-veloce)).
- **Chi ripristina.** Ogni ripristino è nel registro amministrativo del server. Uno strumento che
  risponde a molte persone scrive una voce per ogni chiamata; chi ha chiesto spetta all'applicazione
  registrarlo.
