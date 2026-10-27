---
title: Dokumente für RAG maskieren
description: Eine LangChain- oder LangGraph-Pipeline indiziert maskierte Dokumente, fragt das Modell mit einer maskierten Frage und stellt seine Antwort wieder her.
sidebar:
  label: RAG
---

Eine RAG-Anwendung sendet ihre Dokumente zweimal: einmal zum Indizieren (an einen Embedding-Dienst und
einen Vektorspeicher) und erneut, in Stücken, mit jeder Frage an das Modell. Mit der
[API für Anwendungen](../) werden die Dokumente maskiert, bevor sie indiziert werden, die Frage, bevor
sie gestellt wird, und die Antwort des Modells wird für die Person wiederhergestellt, die gefragt hat.
Der Embedding-Dienst, der Speicher und das Modell sehen nur Ersatzwerte; die Person liest die echten
Werte.

Das funktioniert, weil die Ersatzwerte aus dem Tresor eines einzigen API-Schlüssels stammen: Ein Wert
hat in jedem Dokument und in jeder Frage denselben Ersatzwert. So findet die Suche die Dokumente des
Kunden, den eine Frage nennt, und das Modell erkennt, dass zwei Dokumente von derselben Person
sprechen.

## Der Ablauf

1. **Indizieren** Sie die Dokumente maskiert, mit einem Token, der maskieren darf.
2. **Maskieren** Sie die Frage mit demselben Schlüssel.
3. **Suchen** Sie mit der maskierten Frage, und **fragen Sie das Modell** mit der maskierten Frage und
   den maskierten Dokumenten. Rufen Sie das Modell direkt auf, mit Ihrem eigenen Anbieterschlüssel:
   nicht über das Gateway von Latet, das die Ersatzwerte erneut maskieren würde.
4. **Stellen Sie** die Antwort des Modells mit einem Token, der wiederherstellen darf, **wieder her**
   und zeigen Sie sie der Person.

```sh
latet server --server https://latet.acme.lan keys add "Support assistant" --out assistant-admin.key
latet server --server https://latet.acme.lan keys add-token "Support assistant" indexer --mask --out indexer.token
latet server --server https://latet.acme.lan keys add-token "Support assistant" assistant --mask --unmask --out assistant.token
```

Der Indizierungsjob erhält `indexer.token`; die Anwendung, die antwortet, erhält `assistant.token`.

## Mit LangChain

`pip install "latet-client[langchain]"` ergänzt den Python-Client um einen Dokument-Transformer und
zwei Runnables:

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
    Document(page_content="Ticket 4411: mario.rossi@acme.it bittet um eine Erstattung auf IT60X0542811101000000123456."),
]
store = InMemoryVectorStore(OpenAIEmbeddings())
store.add_documents(LatetMasker(latet).transform_documents(documents))

prompt = ChatPromptTemplate.from_template("Antworte nur anhand dieser Tickets.\n\n{context}\n\nFrage: {question}")


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

print(chain.invoke("Wohin geht die Erstattung für mario.rossi@acme.it?"))
```

Was jeder sieht, zum Beispiel:

| | Text |
| --- | --- |
| Der Speicher | `Ticket 4411: tommaso.gallo@margie-6e5a.example bittet um eine Erstattung auf IT17F3945471087320356678487.` |
| Das Modell, gefragt | `Wohin geht die Erstattung für tommaso.gallo@margie-6e5a.example?` |
| Das Modell, antwortend | `Die Erstattung geht auf IT17F3945471087320356678487.` |
| Die Person | `Die Erstattung geht auf IT60X0542811101000000123456.` |

`LatetMasker` gibt Kopien der Dokumente zurück, mit dem maskierten Text und in ihren Metadaten
`latet_counts`: wie viele Werte jedes Typs maskiert wurden. Nur der Text wird maskiert, nicht die
Metadaten: Halten Sie vertrauliche Werte daraus fern, oder maskieren Sie sie selbst mit `latet.mask`.
Wenn der Server ein Dokument stoppt (`blocked`), löst `LatetMasker` `Blocked` mit seinem Index aus und
gibt nichts zurück, sodass kein Dokument dieses Stapels unmaskiert indiziert wird.

Mit einem `AsyncLatet`-Client tun `atransform_documents` und `ainvoke` dasselbe, ohne zu blockieren.

## Mit LangGraph

`mask_runnable` nimmt eine Zeichenkette und gibt eine Zeichenkette zurück; `unmask_runnable` nimmt eine
Zeichenkette oder die Nachricht eines Chat-Modells (`AIMessage`) und gibt eine Zeichenkette zurück. In
einem Graphen, dessen Zustand mehr als eine Zeichenkette enthält, rufen Sie den Client in den Knoten
auf:

```python
def take_question(state: State) -> dict:
    return {"question": latet.mask(state["question"]).text}


def give_answer(state: State) -> dict:
    return {"answer": latet.unmask(state["answer"]).text}
```

Ein maskierter Text ist `None`, wenn der Server ihn gestoppt hat (`blocked` sagt, warum):
Entscheiden Sie im Knoten, was die Person dann sieht.

## Gut zu wissen

- **Ein Schlüssel pro Sammlung.** Die Dokumente und die Fragen einer Anwendung teilen sich einen
  Schlüssel. Zwei Schlüssel ergeben zwei Sätze von Ersatzwerten, und eine mit dem anderen Schlüssel
  maskierte Frage findet ihre Dokumente nicht.
- **Namen** findet das auf dem Server installierte lokale Namensmodell
  ([Erkennungsmodelle auf einem Server](../../concepts/server-operations/#erkennungsmodelle-auf-einem-server)),
  in Dokumenten, die als `prose` gesendet werden, der Voreinstellung. Ohne es findet sie der lokale
  Prüfer des Servers, wenn die Regeln des Schlüssels einen festlegen, nach bestem Bemühen; die
  Begriffe des Schlüssels fangen die Namen ab, die Sie kennen.
- **Was `unmask` wiederherstellt.** Die Ersatzwerte, die das Modell so schreibt, wie sie ihm gegeben
  wurden, in Großbuchstaben oder in einem Link kodiert; nicht einen, den es in eine andere Form
  umschreibt, etwa eine in Vierergruppen gegliederte IBAN. Der Name einer Person kommt nur als Ganzes
  zurück: Ein Vorname allein bleibt so, wie ihn das Modell geschrieben hat. Bitten Sie das Modell im
  Prompt, Namen, Adressen und Nummern genau so zu zitieren, wie die Dokumente sie schreiben.
- **Aus den Originalen indizieren.** Um ein Dokument erneut zu indizieren, maskieren Sie sein Original:
  Dieselben Werte erhalten dieselben Ersatzwerte. Eine bereits maskierte Kopie zu maskieren ergibt
  Ersatzwerte von Ersatzwerten, die eine einzige Wiederherstellung nur um einen Schritt zurückführt.
- **Lange Listen.** Ein Aufruf nimmt bis zu 1.000 Texte und das `apps_request_mb` des Servers; der
  Client teilt eine längere Liste in mehrere Aufrufe nacheinander auf. Ein Wert, den erst ein
  späterer Aufruf dem Tresor beibringt (etwa ein Name, den der Prüfer in einem Dokument findet), wird
  in den Dokumenten der früheren Aufrufe nicht maskiert: Halten Sie Dokumente, die gemeinsam maskiert
  werden müssen, in einem Aufruf zusammen. Ein Dokument von etwa 20 KB braucht auf einer Instanz etwa
  80 bis 95 ms zum Maskieren und 30 ms zum Wiederherstellen ([die Messungen](../#wie-schnell-es-ist)).
- **Wer wiederherstellt.** Jede Wiederherstellung steht im Administrationsprotokoll des Servers. Ein
  Werkzeug, das vielen Personen antwortet, schreibt einen Eintrag pro Aufruf; wer gefragt hat,
  protokolliert die Anwendung.
