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

`latet gemini` donne aussi à Gemini CLI une clé de lancement dans `GEMINI_CLI_CUSTOM_HEADERS`,
ajoutée aux en-têtes que vous y définissez, pour que Latet considère le projet de la session comme de confiance ;
là où il ne peut pas vérifier que Gemini CLI l'enverra, il n'en passe aucune, le dit, et le projet que
Gemini CLI nomme est déclaré ([Projets](../../concepts/projects/#de-confiance-et-déclarés)). Vos propres en-têtes
dans `GEMINI_CLI_CUSTOM_HEADERS` sont refusés, chacun nommé, tant qu'une
[exception exacte](../../getting-started/upgrading-to-0-11/#approuver-une-exception-exacte) ne le laisse pas passer.

Gemini CLI lit `GEMINI_CLI_CUSTOM_HEADERS` à partir de la version 0.19.0, et Latet a vérifié comment il
les envoie de la 0.19.0 à la 0.66.0. Un Gemini CLI plus ancien ignore la variable : sa session est
déclarée, et `latet doctor` nomme sa version. En mode ACP (`--acp` ou `--experimental-acp`, quand un
éditeur démarre Gemini CLI), l'éditeur peut donner à Gemini CLI une adresse et des en-têtes à lui, donc
`latet gemini` n'y passe aucune clé de lancement et le dit au démarrage.

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
  [reconnaissance de texte](../../concepts/images/) facultative ; sans elle, une requête avec une
  image n'est pas envoyée. Les autres fichiers, et les fichiers donnés par leur adresse, sont
  refusés.
- La **réflexion** du modèle reste telle que le modèle l'a écrite. Le code que Google exécute pour
  lui et ce que ce code a affiché, ainsi que les entrées et les sorties des outils de Google
  lui-même (Google Search, contexte d'URL, Maps, File Search), sont masqués, comme l'appel d'une
  fonction et son résultat.
- **Toute autre requête avec un corps** sur ces routes est refusée, avec une erreur qui le dit, et
  un WebSocket de la Live API est fermé avant que rien ne passe : l'Interactions API, la Live API,
  la Batch API, l'envoi de fichiers et le `:predict` de Vertex (ses propres embeddings et modèles
  d'images) portent une conversation sous des formes que Latet ne lit pas (elles sont dans la
  [feuille de route](../../roadmap/)). La liste des modèles et la lecture d'un modèle passent ; tout
  autre appel sans corps est refusé aussi.
- L'en-tête de la bibliothèque cliente de Google `x-goog-api-client` est retiré ; votre clé d'API,
  dans le paramètre `key` ou dans `x-goog-api-key`, est envoyée telle qu'elle est arrivée et jamais
  journalisée.

## Limites

- **Gemini CLI connecté avec Google** n'est pas couvert (voir plus haut).
- **Antigravity CLI** accepte une URL de base avec une clé d'API (`GOOGLE_GEMINI_BASE_URL`), mais sa
  documentation ne dit pas quel protocole de Google il parle alors, et il n'est pas vérifié avec
  Latet. S'il utilise l'Interactions API, Latet refuse ses requêtes au lieu de les envoyer sans les
  masquer.
- Une réponse qui utilise un **cache explicite** retrouve les originaux des valeurs que porte sa
  propre conversation.
