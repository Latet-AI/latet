---
title: GitHub Copilot, JetBrains, Zed et autres outils
description: Faites passer GitHub Copilot, Junie et AI Assistant de JetBrains, Zed, Cline, Kilo Code, Continue, Qwen Code, Crush, Prime Agent, Android Studio et Claude Desktop par la passerelle de Latet, et voyez quels outils Latet ne peut pas protéger.
sidebar:
  label: Copilot, JetBrains et autres
---

De nombreux outils d'IA vous laissent choisir l'adresse du modèle qu'ils appellent. Réglez cette
adresse sur la **passerelle** de Latet : chaque requête que l'outil envoie est masquée avant de
quitter votre machine, et les originaux sont remis en place dans la réponse, sur votre machine. Rien
n'est installé dans l'outil : un réglage suffit.

Vous gardez la clé d'API de votre fournisseur dans l'outil, comme d'habitude. Latet la transmet
inchangée au fournisseur et ne l'écrit jamais dans ses journaux ni dans le tableau de bord.

| Fournisseur | Adresse à donner à l'outil |
| --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `http://127.0.0.1:5283/anthropic`, ou `http://127.0.0.1:5283/anthropic/v1` pour un outil qui n'y ajoute que `/messages` |
| OpenRouter | `http://127.0.0.1:5283/openrouter/api/v1` |

Chaque section ci-dessous donne l'adresse exacte dont l'outil a besoin. Un outil dont le fournisseur
OpenRouter intégré ne permet pas de changer l'adresse atteint OpenRouter par un fournisseur
compatible OpenAI, avec l'adresse OpenRouter ci-dessus.

Junie CLI, Kilo Code, Continue et Crush lisent aussi des réglages dans le projet que vous ouvrez, et les
réglages propres à un projet peuvent les faire pointer vers une autre adresse : vérifiez-les avant de
travailler dans un dépôt que vous n'avez pas écrit.

## En un coup d'œil

*Testé* signifie que l'outil lui-même a été lancé à travers Latet face à un fournisseur de test, sur
chaque API qu'il utilise : le fournisseur a reçu des valeurs factices, et l'outil a affiché les
vraies valeurs.

| Outil | Ce qui passe par Latet | Ce qui n'y passe pas | Testé |
| --- | --- | --- | --- |
| GitHub Copilot dans VS Code | Le chat et les agents, avec un modèle Custom Endpoint | Les suggestions inline et les autres fonctions qui utilisent le service de GitHub lui-même | Oui |
| GitHub Copilot CLI | Chaque requête, avec un fournisseur à vous | Rien, en mode hors ligne | Oui |
| GitHub Copilot dans les IDE JetBrains et Visual Studio | Le chat, avec un point de terminaison personnalisé compatible OpenAI | Les suggestions inline | Non |
| JetBrains Junie CLI | Chaque requête, avec un modèle personnalisé | Rien | Oui |
| JetBrains AI Assistant | AI Chat et les fonctions auxquelles vous attribuez le modèle | La complétion de code et les agents d'AI Chat | Non |
| Zed | Le panneau de l'agent et l'assistant inline | Les prédictions de modifications | Non |
| Cline | Chaque requête | Rien | La CLI |
| Kilo Code | Chaque requête, avec un fournisseur personnalisé | Rien | La CLI |
| Continue | Chaque requête d'un modèle dont l'`apiBase` est Latet | Rien | La CLI |
| Qwen Code | Chaque requête | Une adresse e-mail tapée dans le prompt | Oui |
| Crush | Chaque requête | Rien | Oui |
| Prime Agent | Chaque requête vers Anthropic, OpenAI et OpenRouter, avec une clé d'API | Prime Inference et un abonnement ChatGPT ; avec un abonnement Claude, les requêtes sont vérifiées, pas masquées | Oui |
| Android Studio | Le chat et le mode agent, avec un fournisseur distant tiers | Les fonctions propres à Gemini | Non |
| Claude Desktop sur une passerelle | Chaque requête est vérifiée, et arrêtée si elle devait transporter une valeur sensible | Le masquage, en dehors de l'onglet Code | Non |

## GitHub Copilot dans VS Code

Le **Custom Endpoint** de VS Code envoie Copilot Chat vers un modèle, à l'adresse de votre choix. Il
fonctionne avec ou sans connexion à GitHub, avec ou sans abonnement Copilot ; avec Copilot Business
et Enterprise, un administrateur peut le désactiver.

1. Lancez **Chat: Manage Language Models**, puis **Add Models** → **Custom Endpoint**.
2. Saisissez un nom et la clé d'API de votre fournisseur. VS Code conserve la clé dans son stockage
   de secrets.
3. Décrivez chaque modèle dans le fichier que VS Code ouvre, `chatLanguageModels.json`, à côté de
   votre `settings.json`. L'`url` de chaque modèle est l'adresse complète de l'appel, et `apiType`
   l'API qu'il utilise :

```json title="chatLanguageModels.json"
[
  {
    "name": "Latet",
    "vendor": "customendpoint",
    "models": [
      {
        "id": "gpt-5.1",
        "name": "GPT-5.1 through Latet",
        "url": "http://127.0.0.1:5283/openai/v1/responses",
        "apiType": "responses",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 272000,
        "maxOutputTokens": 128000
      },
      {
        "id": "claude-sonnet-4-5",
        "name": "Claude Sonnet 4.5 through Latet",
        "url": "http://127.0.0.1:5283/anthropic/v1/messages",
        "apiType": "messages",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 200000,
        "maxOutputTokens": 64000
      }
    ]
  }
]
```

Pour l'API Chat Completions, utilisez `http://127.0.0.1:5283/openai/v1/chat/completions` avec
`"apiType": "chat-completions"`. Choisissez le modèle dans le sélecteur de modèles du chat ; un
modèle a besoin de `"toolCalling": true` pour fonctionner en mode agent.

**Ce qui ne passe pas par Latet.** Les suggestions inline (complétions de code et suggestions de la
modification suivante), la recherche de code et les autres fonctions qui reposent sur les
embeddings de GitHub utilisent le service de GitHub lui-même, pas votre modèle : quand vous êtes
connecté, elles partent sans passer par Latet. Connecté, Copilot confie aussi de petites tâches,
comme les titres des conversations et les messages de commit, aux modèles de GitHub ; pour les
garder sur votre modèle, définissez
`"chat.byokUtilityModelDefault": "mainAgent"` dans vos réglages, ou choisissez-leur des modèles avec
`chat.utilityModel` et `chat.utilitySmallModel`.

## GitHub Copilot CLI

La CLI de Copilot passe par Latet avec un fournisseur à vous : une clé OpenAI, Anthropic ou
OpenRouter, ou une autre adresse, définie par des variables d'environnement. Connectée à GitHub, sur
les modèles d'un abonnement Copilot, elle envoie au service Copilot de GitHub, devant lequel Latet ne
peut pas se placer : `latet copilot` le dit et ne lance rien.

```sh
export COPILOT_PROVIDER_TYPE=openai          # Chat Completions
export COPILOT_PROVIDER_API_KEY=...          # your OpenAI key
export COPILOT_MODEL=gpt-5.1
export COPILOT_OFFLINE=true                  # no GitHub sign-in, no telemetry
latet copilot
```

En PowerShell :

```powershell
$env:COPILOT_PROVIDER_TYPE = "openai"
$env:COPILOT_PROVIDER_API_KEY = "..."        # your OpenAI key
$env:COPILOT_MODEL = "gpt-5.1"
$env:COPILOT_OFFLINE = "true"
latet copilot
```

`latet copilot` dirige l'adresse du fournisseur vers Latet pour cette session seulement : celles
d'OpenAI, d'Anthropic et d'OpenRouter vont aux routes propres de Latet, une autre adresse devient
l'une de vos destinations, et une adresse sur cet ordinateur reste telle quelle. Avec OpenRouter,
définissez `COPILOT_PROVIDER_BASE_URL=https://openrouter.ai/api/v1` et votre clé OpenRouter ; pour
Anthropic, `COPILOT_PROVIDER_TYPE=anthropic` et votre clé Anthropic. Pour lancer `copilot`
vous-même, définissez plutôt `COPILOT_PROVIDER_BASE_URL` sur l'adresse de Latet :
`http://127.0.0.1:5283/openai/v1`, `/openrouter/api/v1` ou `/anthropic`.

`COPILOT_PROVIDER_WIRE_API=responses` utilise plutôt l'API Responses. Le modèle doit prendre en
charge les appels d'outils et le streaming. Sans `COPILOT_OFFLINE=true`, la CLI envoie quand même de
la télémétrie à GitHub, et des fonctions comme `/delegate` demandent une connexion à GitHub ; vos
prompts ne vont qu'à votre fournisseur.

## GitHub Copilot dans les IDE JetBrains et Visual Studio

Le plugin JetBrains de Copilot accepte un point de terminaison personnalisé compatible OpenAI :
ouvrez **Manage Models** depuis le sélecteur de modèles du chat, ajoutez le point de terminaison
`http://127.0.0.1:5283/openai/v1` avec votre clé d'API, et choisissez son modèle dans le chat. Visual
Studio 2026 fait de même en mode agent, depuis ses propres réglages de modèles. Dans les deux, les
suggestions inline continuent d'utiliser le service de GitHub.

## JetBrains Junie CLI

Junie CLI lit un modèle personnalisé dans un fichier de profil placé dans `~/.junie/models/`, ou dans
`.junie/models/` au sein d'un projet. Son `baseUrl` est l'adresse complète de l'appel : Junie n'y
ajoute rien.

```json title="~/.junie/models/latet.json"
{
  "id": "gpt-5.1",
  "baseUrl": "http://127.0.0.1:5283/openai/v1/responses",
  "apiType": "OpenAIResponses",
  "apiKey": "${OPENAI_API_KEY}"
}
```

Lancez Junie avec `junie --model custom:latet`, ou choisissez le modèle sous **Custom** dans
`/model`. Les autres valeurs d'`apiType` :

| `apiType` | `baseUrl` |
| --- | --- |
| `OpenAICompletion` (Chat Completions) | `http://127.0.0.1:5283/openai/v1/chat/completions` |
| `Anthropic` | `http://127.0.0.1:5283/anthropic/v1/messages` |

Un modèle personnalisé ne demande pas de compte JetBrains. `JUNIE_SHARE_ANONYMOUS_STATISTICS=false`
désactive les statistiques d'utilisation anonymes. Junie dans l'IDE n'accepte que des clés OpenAI et
Anthropic, pas une adresse personnalisée.

## JetBrains AI Assistant

Dans **Settings | Tools | AI Assistant | Providers & API keys**, sous **Third-party AI providers**,
ajoutez un fournisseur compatible OpenAI avec l'URL `http://127.0.0.1:5283/openai/v1` et votre clé
d'API, et activez **Tool calling**. Ses modèles apparaissent dans le sélecteur de modèles d'AI Chat ;
dans **Models Assignment**, vous pouvez aussi leur confier les autres fonctions, comme les messages
de commit.

La complétion de code n'utilise pas un modèle à vous, et les agents d'AI Chat, comme Junie, Claude
Agent et Codex, prennent les clés de leurs propres fournisseurs, pas une adresse personnalisée : ils
ne passent pas par Latet.

## Zed

Ajoutez un fournisseur dans le `settings.json` de Zed, puis choisissez son modèle dans le panneau de
l'agent :

```json title="settings.json"
{
  "language_models": {
    "openai_compatible": {
      "Latet": {
        "api_url": "http://127.0.0.1:5283/openai/v1",
        "available_models": [{ "name": "gpt-5.1", "max_tokens": 272000 }]
      }
    },
    "anthropic": { "api_url": "http://127.0.0.1:5283/anthropic" }
  }
}
```

Zed lit la clé du fournisseur `Latet` dans les réglages de l'agent, ou dans la variable
`LATET_API_KEY` ; le fournisseur Anthropic garde sa propre clé. Les fournisseurs OpenAI et
OpenRouter intégrés à Zed acceptent aussi un `api_url`.

Les **prédictions de modifications** viennent par défaut du service de Zed lui-même et ne passent
pas par Latet : définissez `"edit_predictions": { "provider": "none" }` pour les désactiver.

## Cline

Dans les réglages de Cline, choisissez le fournisseur **OpenAI Compatible** avec l'URL de base
`http://127.0.0.1:5283/openai/v1`, votre clé d'API et un identifiant de modèle. Pour Anthropic,
choisissez le fournisseur **Anthropic**, cochez **Use custom base URL** et saisissez
`http://127.0.0.1:5283/anthropic/v1` : Cline y ajoute `/messages`.

La CLI de Cline reçoit le fournisseur compatible OpenAI en ligne de commande :

```sh
cline auth -p openai-compatible -k "$OPENAI_API_KEY" -m gpt-5.1 -b http://127.0.0.1:5283/openai/v1
```

Pour son fournisseur Anthropic, définissez `baseUrl` dans `~/.cline/data/settings/providers.json` :
`cline auth` n'accepte une URL de base que pour les fournisseurs compatibles OpenAI.

## Kilo Code

Dans l'extension Kilo Code, ajoutez un **fournisseur personnalisé** dans ses réglages, avec l'API
qu'il utilise (OpenAI Compatible, OpenAI Responses ou Anthropic Messages) et l'URL de base
ci-dessous. La CLI `kilo` lit le même fournisseur dans `kilo.json`, dans `~/.config/kilo/` ou dans
un projet :

```json title="kilo.json"
{
  "provider": {
    "latet": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Latet",
      "options": { "baseURL": "http://127.0.0.1:5283/openai/v1", "apiKey": "{env:OPENAI_API_KEY}" },
      "models": { "gpt-5.1": { "name": "GPT-5.1 through Latet" } }
    }
  },
  "model": "latet/gpt-5.1",
  "disabled_providers": ["kilo"]
}
```

`disabled_providers` désactive le fournisseur propre à Kilo, dont les requêtes vont au service de Kilo
sans passer par Latet.
Pour Anthropic, utilisez `"npm": "@ai-sdk/anthropic"` avec l'URL de base
`http://127.0.0.1:5283/anthropic/v1` ; pour l'API Responses, `"npm": "@ai-sdk/openai"` avec
`http://127.0.0.1:5283/openai/v1`.

## Continue

Les extensions de Continue pour VS Code et JetBrains et sa CLI `cn` lisent le même
`~/.continue/config.yaml`. Donnez à chaque modèle l'adresse de Latet dans `apiBase` :

```yaml title="~/.continue/config.yaml"
name: Latet
version: 1.0.0
schema: v1
models:
  - name: GPT-5.1 through Latet
    provider: openai
    model: gpt-5.1
    apiBase: http://127.0.0.1:5283/openai/v1
    apiKey: ${{ secrets.OPENAI_API_KEY }}
  - name: Claude through Latet
    provider: anthropic
    model: claude-sonnet-4-5
    apiBase: http://127.0.0.1:5283/anthropic/v1
    apiKey: ${{ secrets.ANTHROPIC_API_KEY }}
```

L'`apiBase` d'Anthropic se termine par `/v1` : Continue y ajoute `/messages`.

## Qwen Code

Qwen Code lit son adresse dans les mêmes variables que les SDK officiels, si bien que `latet run` le
fait passer par Latet :

```sh
latet run qwen
```

Il utilise `OPENAI_BASE_URL` avec son fournisseur compatible OpenAI et `ANTHROPIC_BASE_URL` avec son
fournisseur Anthropic ; choisissez le fournisseur dans `~/.qwen/settings.json`
(`"security": { "auth": { "selectedType": "openai" } }`, ou `"anthropic"`), et définissez la clé et
le modèle dans `OPENAI_API_KEY` et `OPENAI_MODEL`, ou dans `ANTHROPIC_API_KEY` et
`ANTHROPIC_MODEL`. Sans `latet run`, définissez vous-même les URL de base :
`http://127.0.0.1:5283/openai/v1` et `http://127.0.0.1:5283/anthropic`.

**Une adresse e-mail que vous tapez.** Qwen Code lit chaque `@` d'un prompt que vous tapez comme un
fichier à joindre, et envoie `mario.rossi@acme.it` sous la forme `mario.rossi @acme.it`. Latet n'y
reconnaît pas une adresse, qui part donc en clair. Les adresses contenues dans les fichiers, les
sorties de commandes et les pages que lit Qwen Code ne sont pas modifiées, et Latet les masque.

## Crush

Ajoutez un fournisseur au `crush.json` de Crush, dans un projet ou dans le dossier de réglages de
Crush :

```json title="crush.json"
{
  "providers": {
    "latet": {
      "type": "openai-compat",
      "base_url": "http://127.0.0.1:5283/openai/v1",
      "api_key": "$OPENAI_API_KEY",
      "models": [{ "id": "gpt-5.1", "name": "GPT-5.1 through Latet", "context_window": 272000, "default_max_tokens": 16000 }]
    }
  }
}
```

Choisissez ce fournisseur pour les deux modèles de Crush, afin qu'aucune requête, y compris les courtes
qui nomment une session, n'aille vers un fournisseur que Crush a configuré de lui-même d'après votre
environnement :

```json title="crush.json"
"models": {
  "large": { "model": "gpt-5.1", "provider": "latet" },
  "small": { "model": "gpt-5.1", "provider": "latet" }
}
```

Pour Anthropic, utilisez `"type": "anthropic"` avec `http://127.0.0.1:5283/anthropic`. Le type
`openai` de Crush utilise l'API Responses, et son type `openrouter` n'accepte pas d'URL de base :
atteignez OpenRouter avec `openai-compat` et `http://127.0.0.1:5283/openrouter/api/v1`.

## Prime Agent

Prime Agent lit l'adresse de chaque fournisseur dans `models.json`, dans `~/.prime/agent`
(`%USERPROFILE%\.prime\agent` sous Windows). Donnez à ses fournisseurs intégrés l'adresse de Latet :

```json title="~/.prime/agent/models.json"
{
  "providers": {
    "anthropic": { "baseUrl": "http://127.0.0.1:5283/anthropic" },
    "openai": { "baseUrl": "http://127.0.0.1:5283/openai/v1" },
    "openrouter": { "baseUrl": "http://127.0.0.1:5283/openrouter/api/v1" }
  }
}
```

Chaque modèle de ces fournisseurs passe alors par Latet, avec la clé d'API que vous avez donnée à
Prime Agent avec `/login` ou dans `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` et `OPENROUTER_API_KEY`.

**Avec un abonnement au lieu d'une clé.** Connecté avec un abonnement Claude, Prime Agent se présente
à Anthropic comme Claude Code. Ses requêtes passent quand même par Latet, qui vérifie les requêtes
faites avec un abonnement Claude et ne les réécrit jamais : celle qui transporterait en clair un
secret ou une donnée personnelle est arrêtée, avec un message à la place de la réponse. Utilisez une
clé d'API pour qu'elles soient masquées. Les requêtes faites avec un abonnement ChatGPT, et celles
vers Prime Inference, le service de Prime Intellect, ne passent pas par Latet.

## Android Studio

Dans **Settings → Tools → AI → Model Providers**, ajoutez un **Third-Party Remote Provider** avec
l'adresse de Latet à la place de celle du fournisseur, et votre clé d'API, puis actualisez la liste
des modèles et choisissez-les : `http://127.0.0.1:5283/openai/v1` pour OpenAI,
`http://127.0.0.1:5283/anthropic` pour Anthropic, `http://127.0.0.1:5283/openrouter/api/v1` pour
OpenRouter. Le chat et le mode agent utilisent le modèle que vous choisissez ; les fonctions propres
à Gemini continuent d'utiliser le service de Google.

## Claude Desktop sur une passerelle

Claude Desktop peut fonctionner sur une passerelle d'inférence au lieu du service d'Anthropic, sans
connexion à un compte Claude : c'est ainsi qu'une organisation l'utilise avec son propre fournisseur.
Il n'y a alors pas d'abonnement Pro ou Max : les requêtes sont facturées à l'usage, avec une clé
d'API Anthropic ou par le fournisseur de votre entreprise. Sur un ordinateur, sans vous connecter,
activez
**Help → Troubleshooting → Enable Developer Mode**, puis ouvrez
**Developer → Configure Third-Party Inference** ; une organisation définit les mêmes clés depuis un
profil de gestion des appareils. Faites de Latet la passerelle :

| Clé | Valeur |
| --- | --- |
| `inferenceProvider` | `gateway` |
| `inferenceGatewayBaseUrl` | `http://127.0.0.1:5283/anthropic` |
| `inferenceGatewayApiKey` | Votre clé d'API Anthropic |
| `inferenceGatewayAuthScheme` | `x-api-key` |

Claude Desktop accepte une adresse `http://` sur cet ordinateur. Chat, Cowork et Code envoient alors
chaque requête par Latet, et la liste des modèles vient d'Anthropic en passant par lui.

**Ce que Latet en fait.** Le moteur de Claude Code construit ces requêtes, et Latet ne réécrit jamais
une requête venant de Claude Code : il vérifie chacune d'elles, et arrête celle qui transporterait en
clair un secret ou une donnée personnelle, avec un message à la place de la réponse. Il ne les masque
pas, contrairement à ce qu'il fait pour les autres outils de cette page : le
[guide des passerelles](https://code.claude.com/docs/en/llm-gateway-protocol) d'Anthropic demande à
une passerelle d'inspecter les requêtes de Claude Code, pas de les modifier. Dans l'onglet Code, le
plugin de Latet masque le contenu avant que la requête ne soit construite : l'onglet Code bénéficie
donc à la fois du masquage et de la vérification.

Quand Claude Desktop est connecté à un compte Claude, il n'utilise pas de passerelle : Chat et Cowork
ne sont pas couverts, et le plugin masque le contenu dans l'onglet Code sans vérification derrière
lui. Voir [Claude Code](../claude-code/#couverture).

## Ce que Latet ne peut pas protéger

Latet s'exécute sur votre ordinateur, entre l'outil et le modèle. Les outils ci-dessous envoient
leurs requêtes depuis les serveurs de leur éditeur, ou appellent un service qu'aucun réglage ne
permet de changer, si bien que leurs requêtes ne passent jamais par Latet :

| Outil | Pourquoi |
| --- | --- |
| Cursor et sa CLI | Cursor construit et envoie chaque requête au modèle depuis ses propres serveurs, même avec votre propre clé d'API. |
| Windsurf et Devin Desktop | Les requêtes vont aux serveurs de l'éditeur, qui appellent le modèle. |
| Google Antigravity (l'IDE) | Les requêtes vont au service de Google, qui appelle le modèle. |
| Jules | Il s'exécute dans le cloud de Google, pas sur votre ordinateur. |
| Kiro et Amazon Q Developer | Les requêtes vont au service d'Amazon, qui appelle le modèle. |
| Warp | Les requêtes vont aux serveurs de Warp, qui appellent le modèle. |
| L'application de bureau ChatGPT | Elle ne communique qu'avec le service d'OpenAI ; elle n'a aucun réglage pour une autre adresse. |
| Claude Desktop connecté à un compte Claude, et les sessions dans le cloud | Chat et Cowork appellent directement le service d'Anthropic, et les sessions dans le cloud s'exécutent sur l'infrastructure d'Anthropic. |

Si un outil que vous utilisez ne figure pas sur cette page, cherchez un réglage nommé base URL, API
base, endpoint ou custom provider : s'il en a un, il passe par Latet comme les outils ci-dessus. Si
cela ne fonctionne pas, merci d'[ouvrir un ticket](https://github.com/Latet-AI/latet/issues).
