---
title: Masquer les documents pour le RAG
description: Un pipeline LangChain ou LangGraph indexe des documents masqués, interroge le modèle avec une question masquée et restaure sa réponse.
sidebar:
  label: RAG
---

Une application RAG envoie ses documents deux fois : une fois pour les indexer (à un service
d'embeddings et à une base vectorielle), et à nouveau, par morceaux, au modèle avec chaque question.
Avec l'[API pour les applications](../), les documents sont masqués avant d'être indexés, la question
avant d'être posée, et la réponse du modèle est restaurée pour la personne qui a posé la question. Le
service d'embeddings, la base et le modèle ne voient que des valeurs factices ; la personne lit les
vraies valeurs.

Cela fonctionne parce que les valeurs factices viennent du coffre-fort d'une seule clé d'API : une
valeur a la même valeur factice dans chaque document et dans chaque question, de sorte que la
recherche trouve les documents du client qu'une question nomme, et que le modèle comprend que deux
documents parlent de la même personne.

## Le flux

1. **Indexez** les documents masqués, avec un jeton qui peut masquer.
2. **Masquez** la question avec la même clé.
3. **Cherchez** avec la question masquée, et **interrogez le modèle** avec la question et les
   documents masqués. Appelez le modèle directement, avec votre propre clé de fournisseur : pas par la
   passerelle de Latet, qui masquerait à nouveau les valeurs factices.
4. **Restaurez** la réponse du modèle avec un jeton qui peut restaurer, et montrez-la à la personne.

```sh
latet server --server https://latet.acme.lan keys add "Support assistant" --out assistant-admin.key
latet server --server https://latet.acme.lan keys add-token "Support assistant" indexer --mask --out indexer.token
latet server --server https://latet.acme.lan keys add-token "Support assistant" assistant --mask --unmask --out assistant.token
```

La tâche d'indexation détient `indexer.token` ; l'application qui répond détient `assistant.token`.

## Avec LangChain

`pip install "latet-client[langchain]"` ajoute au client Python un transformateur de documents et deux
runnables :

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
    Document(page_content="Ticket 4411 : mario.rossi@acme.it demande un remboursement sur IT60X0542811101000000123456."),
]
store = InMemoryVectorStore(OpenAIEmbeddings())
store.add_documents(LatetMasker(latet).transform_documents(documents))

prompt = ChatPromptTemplate.from_template("Réponds uniquement d'après ces tickets.\n\n{context}\n\nQuestion : {question}")


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

print(chain.invoke("Où doit aller le remboursement de mario.rossi@acme.it ?"))
```

Ce que voit chacun, par exemple :

| | Texte |
| --- | --- |
| La base | `Ticket 4411 : tommaso.gallo@margie-6e5a.example demande un remboursement sur IT17F3945471087320356678487.` |
| Le modèle, interrogé | `Où doit aller le remboursement de tommaso.gallo@margie-6e5a.example ?` |
| Le modèle, en réponse | `Le remboursement va sur IT17F3945471087320356678487.` |
| La personne | `Le remboursement va sur IT60X0542811101000000123456.` |

`LatetMasker` renvoie des copies des documents avec le texte masqué et, dans leurs métadonnées,
`latet_counts` : combien de valeurs de chaque type ont été masquées. Seul le texte est masqué, pas les
métadonnées : n'y mettez pas de valeurs sensibles, ou masquez-les vous-même avec `latet.mask`. Quand le
serveur bloque un document (`blocked`), `LatetMasker` lève `Blocked` avec son index et ne renvoie rien :
aucun document de ce lot n'est indexé sans masque.

Avec un client `AsyncLatet`, `atransform_documents` et `ainvoke` font de même sans bloquer.

## Avec LangGraph

`mask_runnable` prend une chaîne et renvoie une chaîne ; `unmask_runnable` prend une chaîne ou le
message d'un modèle de chat (`AIMessage`) et renvoie une chaîne. Dans un graphe dont
l'état contient plus qu'une chaîne, appelez le client dans les nœuds :

```python
def take_question(state: State) -> dict:
    return {"question": latet.mask(state["question"]).text}


def give_answer(state: State) -> dict:
    return {"answer": latet.unmask(state["answer"]).text}
```

Un texte masqué vaut `None` quand le serveur l'a bloqué (`blocked` dit pourquoi) : décidez dans le nœud
ce que la personne voit alors.

## À savoir

- **Une clé par collection.** Les documents et les questions d'une application partagent une clé.
  Deux clés donnent deux jeux de valeurs factices, et une question masquée avec l'autre clé ne trouve
  pas ses documents.
- **Les noms** sont trouvés par le modèle local de noms installé sur le serveur
  ([modèles de reconnaissance sur un serveur](../../concepts/server-operations/#modèles-de-reconnaissance-sur-un-serveur)),
  dans les documents envoyés en `prose`, la valeur par défaut. Sans lui, le relecteur local du
  serveur, quand les règles de la clé en désignent un, les trouve au mieux ; les termes de la clé
  attrapent les noms que vous connaissez.
- **Ce que restaure `unmask`.** Les valeurs factices que le modèle écrit telles qu'elles lui ont été
  données, en majuscules, ou encodées dans un lien ; pas une qu'il réécrit sous une autre forme, comme
  un IBAN regroupé par quatre. Le nom d'une personne ne revient qu'entier : un prénom seul reste tel
  que le modèle l'a écrit. Demandez au modèle, dans le prompt, de citer les noms, adresses et numéros
  exactement comme les documents les écrivent.
- **Indexez à partir des originaux.** Pour indexer à nouveau un document, masquez son original : les
  mêmes valeurs reçoivent les mêmes valeurs factices. Masquer une copie déjà masquée donne des valeurs
  factices de valeurs factices, qu'une seule restauration ne ramène que d'un pas.
- **Listes longues.** Un appel prend jusqu'à 1 000 textes et l'`apps_request_mb` du serveur ; le
  client découpe une liste plus longue en plusieurs appels, l'un après l'autre. Une valeur que seul un
  appel ultérieur apprend au coffre-fort (un nom que le relecteur trouve dans un document, par
  exemple) n'est pas masquée dans les documents des appels précédents : gardez dans un même appel les
  documents qui doivent être masqués ensemble. Un document d'environ 20 Ko prend de 80 à 95 ms à
  masquer et 30 ms à restaurer sur une instance ([les mesures](../#à-quelle-vitesse)).
- **Qui restaure.** Chaque restauration figure dans le journal d'administration du serveur. Un outil
  qui répond à beaucoup de personnes écrit une entrée par appel ; qui a posé la question, c'est à
  l'application de le consigner.
