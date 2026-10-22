---
title: Gemini CLI et Google GenAI
description: Faire passer Gemini CLI, les SDK Google GenAI et Gemini sur Vertex AI par la passerelle de Latet.
---

Gemini CLI et les programmes construits sur les SDK GenAI de Google utilisent le **mode
passerelle** : le daemon local masque les requêtes et restaure les réponses. Il couvre la Gemini API
avec une clé d'API, et Gemini sur Vertex AI, avec un projet Google Cloud ou en mode express avec une
clé d'API.

## URL de base

| Service | Au lieu de | Utilisez | Variable |
| --- | --- | --- | --- |
| Gemini API | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` | `GOOGLE_GEMINI_BASE_URL` |
| Vertex AI | `https://<location>-aiplatform.googleapis.com` | `http://127.0.0.1:5283/vertex` | `GOOGLE_VERTEX_BASE_URL` |

Votre clé d'API ou votre connexion à Google Cloud restent où elles sont. La passerelle les transmet
sans les modifier (une clé dans l'en-tête `x-goog-api-key` ou dans le paramètre `key` de l'adresse,
un jeton Vertex dans `Authorization`) et n'écrit jamais leur valeur dans les journaux, dans le
tableau de bord ou dans une capture. Sur Vertex, chaque requête va à l'adresse de la location
qu'elle nomme.

## Gemini CLI

Faites passer Gemini CLI par Latet avec le wrapper. Il démarre le daemon si besoin, définit les deux
URL de base pour cette exécution seulement, et transmet chaque argument à `gemini` :

```sh
latet gemini
latet gemini -p "write the release notes for the payments module"
```

`latet run gemini` fait de même. Gemini CLI ne prend son adresse que dans l'environnement, il n'est
donc pas disponible en mode global : `latet global-install` ne le configure pas, et il n'y a pas de
`latet agents apply gemini`. Un changement durable n'atteindrait pas un projet qui a son propre
fichier `.env`, et Gemini CLI y parlerait directement à Google sans le dire.

### Choisir une clé d'API ou Vertex AI

Dans Gemini CLI, `/auth` choisit comment il atteint son modèle, et ce choix est gardé dans
`~/.gemini/settings.json` :

```json title="~/.gemini/settings.json"
{ "security": { "auth": { "selectedType": "gemini-api-key" } } }
```

- **Une clé d'API** (`gemini-api-key`) : `GEMINI_API_KEY` la contient.
- **Vertex AI** (`vertex-ai`) : `GOOGLE_GENAI_USE_VERTEXAI=true`, avec `GOOGLE_CLOUD_PROJECT` et
  `GOOGLE_CLOUD_LOCATION` et votre connexion à Google Cloud, ou `GOOGLE_API_KEY` seul en mode
  express.

Quand rien n'est encore choisi, `latet gemini` propose la clé d'API dans la fenêtre de Gemini CLI, ou
Vertex AI quand `GOOGLE_GENAI_USE_VERTEXAI=true` est défini. Une exécution avec `-p` a besoin que le
choix soit fait avant : définissez `selectedType` comme ci-dessus.

**Connecté avec Google**, que ce choix vienne de vos réglages, de ceux du projet
(`.gemini/settings.json` dans le dossier où vous le lancez) ou de ceux de la machine, Gemini CLI
envoie ses requêtes à un point d'accès que Latet ne lit pas, rien ne serait donc masqué. `latet
gemini` ne le démarre pas alors, et dit où le choix est fait et quoi choisir à la place.

### Statistiques d'utilisation

Gemini CLI envoie ses statistiques d'utilisation directement à Google, pas par Latet. Pour les
désactiver :

```json title="~/.gemini/settings.json"
{ "privacy": { "usageStatisticsEnabled": false } }
```

## Scripts et SDK Google GenAI

`latet run` définit `GOOGLE_GEMINI_BASE_URL` et `GOOGLE_VERTEX_BASE_URL` à côté des variables
d'OpenAI et d'Anthropic, et `latet env` les affiche pour tout un shell. Les SDK de Google pour Python
(`google-genai`) et JavaScript (`@google/genai`) les lisent :

```python title="welcome.py"
from google import genai

client = genai.Client()  # reads GEMINI_API_KEY and GOOGLE_GEMINI_BASE_URL
reply = client.models.generate_content(
    model="gemini-3-pro",
    contents="Write a short welcome email to emily.carter@northwind-legal.com",
)
print(reply.text)
```

Lancez-le avec `latet run python welcome.py` : Google lit une fausse adresse de la même forme, et le
script affiche l'e-mail avec la vraie. Les réponses en streaming fonctionnent de la même façon,
morceau par morceau.

Le point d'accès de Google compatible avec OpenAI passe aussi par Latet : donnez à un client OpenAI
l'URL de base `http://127.0.0.1:5283/gemini/v1beta/openai`.

## Ce que lit la passerelle

- **Générer**, en streaming ou non (`generateContent`, `streamGenerateContent`), **compter les
  tokens** (`countTokens`), les **embeddings** (`embedContent`, `batchEmbedContents`) et **créer un
  cache** (`cachedContents`) : l'instruction système, chaque message, les appels de fonction et
  leurs résultats, les déclarations des fonctions et le schéma de la réponse sont masqués, et les
  réponses restaurées.
- Les **images** envoyées dans la requête sont lues et masquées avec la
  [reconnaissance de texte](../../concepts/images/) facultative. Les autres fichiers, et les fichiers
  donnés par leur adresse, sont envoyés tels quels, avec l'indicateur *Non inspecté* dans le tableau
  de bord.
- La **réflexion** du modèle, et le code que Google exécute pour lui, restent tels que le modèle les
  a écrits.
- **Toute autre requête avec un corps** sur ces routes est refusée, avec une erreur qui le dit, et
  un WebSocket de la Live API est fermé avant que rien ne passe : l'Interactions API, la Live API,
  la Batch API, l'envoi de fichiers et le `:predict` de Vertex (ses propres embeddings et modèles
  d'images) portent une conversation sous des formes que Latet ne lit pas encore (elles sont dans la
  [feuille de route](../../roadmap/)). Une requête sans corps, comme la liste des modèles, passe.

## Limites

- **Gemini CLI connecté avec Google** n'est pas couvert (voir plus haut).
- **Antigravity CLI** accepte une URL de base avec une clé d'API (`GOOGLE_GEMINI_BASE_URL`), mais sa
  documentation ne dit pas quel protocole de Google il parle alors, et il n'est pas vérifié avec
  Latet. S'il utilise l'Interactions API, Latet refuse ses requêtes au lieu de les envoyer sans les
  masquer.
- Une réponse qui utilise un **cache explicite** retrouve les originaux des valeurs que porte sa
  propre conversation.
