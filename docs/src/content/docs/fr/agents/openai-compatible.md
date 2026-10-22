---
title: Compatible OpenAI et OpenRouter
description: Utilisez Latet avec tout client qui vous permet de changer l'URL de base d'OpenAI ou d'Anthropic, et avec OpenRouter.
---

Tout client qui vous permet de changer son URL de base OpenAI ou Anthropic peut utiliser le **mode
passerelle** : vos propres scripts, les SDK officiels et les outils construits dessus.

## URL de base

| Fournisseur | Au lieu de | Utilisez |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `https://api.anthropic.com` | `http://127.0.0.1:5283/anthropic` |
| OpenRouter | `https://openrouter.ai/api/v1` | `http://127.0.0.1:5283/openrouter/api/v1` |
| Google Gemini | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` |

Gardez votre clé d'API dans le client, comme d'habitude. La passerelle la transmet inchangée et n'écrit
jamais sa valeur dans les journaux ni dans le tableau de bord.

## Pour une commande : `latet run`

`latet run` démarre le daemon si besoin et exécute n'importe quelle commande avec les URL de base définies :

```sh
latet run python welcome.py
```

La commande voit `OPENAI_BASE_URL` et `OPENAI_API_BASE` valant `http://127.0.0.1:5283/openai/v1`,
`ANTHROPIC_BASE_URL` valant `http://127.0.0.1:5283/anthropic`, et `GOOGLE_GEMINI_BASE_URL` et
`GOOGLE_VERTEX_BASE_URL` valant `http://127.0.0.1:5283/gemini` et `http://127.0.0.1:5283/vertex`. Les
SDK officiels d'OpenAI, d'Anthropic et Google GenAI lisent ces variables : pour ceux de Google, voir
[Gemini CLI et Google GenAI](../gemini/).

## Pour tout un shell : `latet env`

`latet env` affiche les mêmes variables dans la syntaxe de votre shell. Pour les appliquer à la session en
cours :

```sh
eval "$(latet env)"              # macOS and Linux
```

```powershell
latet env | Invoke-Expression    # Windows PowerShell
```

`--shell bash`, `--shell powershell` ou `--shell cmd` choisit la syntaxe ; par défaut, elle suit votre
système. Pour garder les variables, ajoutez les lignes affichées au profil de votre shell. Claude Code
lancé avec `claude` dans un tel shell atteint alors Latet sans son plugin : ses requêtes sont seulement
vérifiées, et chacune de celles qui portent une valeur sensible en clair est arrêtée. Démarrez-le avec
`latet claude`, ou configurez-le avec `latet global-install`, qui lui donnent aussi le plugin.

## Exemple

Un script qui demande à un modèle de rédiger un e-mail :

```python title="welcome.py"
from openai import OpenAI

client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL
reply = client.chat.completions.create(
    model="your-model",
    messages=[{"role": "user", "content": "Write a short welcome email to emily.carter@northwind-legal.com"}],
)
print(reply.choices[0].message.content)
```

Lancez-le avec `latet run python welcome.py` :

1. Le daemon remplace l'adresse avant que la requête ne parte. Le fournisseur lit *"Write a short
   welcome email to laura.bennett@contoso-4f1a.example"*.
2. Le modèle écrit un e-mail à `laura.bennett@contoso-4f1a.example`.
3. Au retour, le daemon remet l'original en place : le script affiche donc un e-mail adressé à
   `emily.carter@northwind-legal.com`.

Seule la valeur factice elle-même est remise en place. Si le modèle a aussi écrit *Dear Laura*, un prénom
qu'il a deviné à partir de l'adresse factice, ce mot reste tel que le modèle l'a écrit.

Les réponses en streaming fonctionnent de la même façon, fragment par fragment.

## Frameworks pour agents

LangChain, LangGraph, AutoGen et CrewAI atteignent le modèle par les SDK officiels ou par LiteLLM,
qui lisent les variables que `latet run` définit : lancez votre programme avec `latet run`, comme
n'importe quel script. Chacun est testé pour de vrai via Latet, configuré comme ci-dessous :

| Framework | Configuration | Testé |
| --- | --- | --- |
| LangChain et LangGraph | `ChatOpenAI`, sans URL de base (`ChatAnthropic` lit `ANTHROPIC_BASE_URL` de la même façon) | Un modèle de chat, et un graphe construit dessus |
| AutoGen | `OpenAIChatCompletionClient`, sans URL de base | Un `AssistantAgent` |
| CrewAI | `LLM(model="openai/gpt-5.1")`, sans URL de base | Son `LLM`, et une crew avec une tâche |

Pour définir l'adresse dans votre code, indiquez-la comme `base_url` : `http://127.0.0.1:5283/openai/v1`
pour les clients d'OpenAI, `http://127.0.0.1:5283/anthropic` pour ceux d'Anthropic.

Aider atteint le modèle par LiteLLM, qui lit `OPENAI_API_BASE` : lancez-le avec
`latet run aider --model openai/gpt-5.1`, ou gardez `openai-api-base: http://127.0.0.1:5283/openai/v1`
dans son `.aider.conf.yml`. Il est testé en conditions réelles de la même façon.

**Ce que Latet ne voit pas.** Latet se place entre votre programme et le modèle, et nulle part
ailleurs. Un outil que votre agent appelle lui-même, comme une API de recherche web, une base de
données ou une base vectorielle dans le cloud, reçoit les vraies valeurs : le modèle le demande avec
des valeurs factices, et Latet remet les originaux dans sa réponse avant que votre programme
n'exécute l'outil. Gardez ces outils sur votre machine, ou considérez ce qu'ils reçoivent comme
envoyé en clair.

## Ce que lit la passerelle

- **Chat Completions** (`/chat/completions`, et l'ancien `/completions`), la **Responses API**
  (`/responses`) et **Anthropic Messages** (`/v1/messages`, comptage des jetons compris) : le prompt
  système, chaque message, les appels d'outils et les résultats d'outils sont masqués, et les originaux
  sont remis en place dans les réponses, en streaming ou non.
- **Embeddings** (`/embeddings`) : l'entrée est masquée.
- Les **images** envoyées sous forme d'URL `data:` sont lues et masquées, avec la
  [reconnaissance de texte](../../concepts/images/) facultative. Les images désignées par leur adresse,
  l'audio et les fichiers sont envoyés tels quels, avec l'indicateur *Non inspecté* dans le tableau de
  bord.
- **Toute autre requête** vers OpenAI, Anthropic ou OpenRouter, comme la liste des modèles, passe
  inchangée. Sur les routes de Google, une requête dont Latet ne lit pas le corps est au contraire
  refusée : ce que Latet y lit figure dans
  [Gemini CLI et Google GenAI](../gemini/#ce-que-lit-la-passerelle).

Une requête vers Anthropic qui vient de Claude Code, ou qui porte un jeton d'abonnement Claude, n'est
jamais réécrite : elle est seulement vérifiée, et bloquée si elle contient une valeur sensible. Voir
[Claude Code](../claude-code/).

## Bon à savoir

- La passerelle n'écoute que sur `127.0.0.1` et refuse les requêtes qui viennent d'une page web.
- Un chemin hors des routes ci-dessus reçoit une erreur `404` : `no upstream for this path`.
- **Un autre fournisseur compatible OpenAI** (DeepSeek, Mistral, un serveur de modèles sur votre réseau) :
  ajoutez-le comme destination, puis utilisez sa route comme URL de base de l'agent, ici
  `http://127.0.0.1:5283/deepseek/v1` :

  ```sh
  latet config add upstreams "{name: deepseek, url: https://api.deepseek.com}"
  ```

  Voir [`upstreams`](../../reference/configuration/#upstreams-items).
- Les agents autonomes comme Hermes Agent et OpenClaw acceptent ces URL de base dans leurs propres
  réglages : voir [Hermes Agent, OpenClaw et autres agents](../autonomous-agents/).
