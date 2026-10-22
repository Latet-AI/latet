---
title: Hermes Agent, OpenClaw et autres agents
description: Les agents autonomes via la passerelle de Latet, dont Hermes Agent, OpenClaw, Goose et OpenHands, et tout agent qui accepte une URL de base compatible OpenAI.
sidebar:
  label: Agents autonomes
---

Les agents autonomes travaillent seuls, pendant des heures : ils lisent des boîtes mail, naviguent sur le
web, interrogent des bases de données, exécutent des commandes et appellent des API, sans que personne ne
relise chaque étape. Chaque page, chaque fichier et chaque résultat qu'ils lisent part vers le modèle avec
la requête suivante. Quand ils pointent vers Latet, chacune de ces requêtes est masquée avant de quitter
votre machine, et les originaux sont remis en place dans les réponses, sur votre machine.

Ils passent par la **passerelle** de Latet, comme tout client compatible OpenAI : vous réglez l'URL de base
de l'agent sur Latet et gardez la clé d'API de votre fournisseur dans l'agent, comme d'habitude.
`latet global-install` ne modifie pas les fichiers de ces agents : les réglages ci-dessous suffisent.

| Fournisseur | URL de base à définir | Clé |
| --- | --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` | Votre clé d'API OpenAI |
| OpenRouter (Claude, Gemini et des centaines d'autres modèles) | `http://127.0.0.1:5283/openrouter/api/v1` | Votre clé d'API OpenRouter |
| Anthropic, pour un agent qui parle le format d'Anthropic | `http://127.0.0.1:5283/anthropic` | Votre clé d'API Anthropic |

Un abonnement Claude Pro ou Max ne fonctionne que dans Claude Code, comme le disent les conditions
d'Anthropic, donc ces agents utilisent une clé d'API : ils atteignent les modèles Claude via OpenRouter, ou
avec une clé d'API Anthropic. La clé traverse Latet jusqu'au fournisseur et n'est jamais journalisée ni
affichée dans le tableau de bord. Hermes Agent connecté avec ChatGPT n'a besoin d'aucune clé : il passe
par Latet avec cette connexion, comme Codex ([plus bas](#avec-la-connexion-chatgpt)).

## Hermes Agent

Hermes lit son modèle et son point de terminaison dans `config.yaml`, dans son dossier : `~/.hermes`
sous macOS et Linux, `%LOCALAPPDATA%\hermes` sous Windows (`HERMES_HOME` en désigne un autre).
Avec une clé d'API, faites pointer un fournisseur personnalisé vers Latet :

```yaml title="config.yaml"
model:
  provider: custom
  base_url: http://127.0.0.1:5283/openrouter/api/v1   # or http://127.0.0.1:5283/openai/v1
  key_env: OPENROUTER_API_KEY                          # the variable that holds your key
  default: anthropic/claude-sonnet-4.5                 # a model your provider lists
```

`hermes model` règle la même chose de façon interactive. `OPENAI_BASE_URL` dans l'environnement ne suffit
pas : Hermes ne le prend en compte que pour son fournisseur `openai-api`, donc `latet run hermes` ne ferait
pas passer un fournisseur personnalisé par Latet.

### Avec la connexion ChatGPT

Hermes connecté avec votre compte ChatGPT (son fournisseur `openai-codex`) passe par Latet, à l'adresse
qu'utilise Codex. Ajoutez une ligne à `.env`, dans le dossier de Hermes :

```sh title="~/.hermes/.env"
HERMES_CODEX_BASE_URL=http://127.0.0.1:5283/backend-api/codex
```

Hermes relit le fichier de lui-même : son message suivant passe par Latet. La variable déplace chaque
requête que Hermes envoie avec cette connexion : la conversation, et le travail qu'il confie au même
modèle à côté, comme le titre d'une session, le résumé d'un long contexte ou la lecture d'une image ou
d'une page web. Elle passe avant `model.base_url` dans `config.yaml`, que vous pouvez laisser tel quel.

- **Un Latet 0.9.3 ou antérieur** rend à Hermes les valeurs factices à la place des vraies : mettez
  d'abord Latet à jour (`latet upgrade`).
- **Un fournisseur de secours avec son propre `base_url`** garde cette adresse : dans
  `fallback_providers`, mettez-y aussi l'adresse de Latet, ou retirez la ligne. Sinon une requête qui
  se replie sur lui part vers ChatGPT sans rien de masqué.
- Si Latet ne tourne pas, Hermes ne peut pas joindre le modèle. C'est voulu : rien ne sort sans être
  masqué.
- Dans le tableau de bord, les requêtes de Hermes figurent sous Codex ou sous un autre agent : Hermes ne
  se nomme qu'auprès de l'adresse de ChatGPT elle-même.

Testé en conditions réelles : Hermes Agent connecté avec ChatGPT.

## OpenClaw

OpenClaw lit `~/.openclaw/openclaw.json` (JSON5) et applique de lui-même les modifications qu'on y fait.
Ajoutez un fournisseur qui pointe vers Latet, et faites de l'un de ses modèles le modèle par défaut :

```json5 title="~/.openclaw/openclaw.json"
{
  agents: {
    defaults: { model: { primary: "latet/gpt-5.1" } },
  },
  models: {
    providers: {
      latet: {
        baseUrl: "http://127.0.0.1:5283/openai/v1",
        apiKey: "${OPENAI_API_KEY}",
        api: "openai-completions",
        models: [{ id: "gpt-5.1", name: "GPT-5.1 through Latet" }],
      },
    },
  },
}
```

Pour OpenRouter, utilisez `http://127.0.0.1:5283/openrouter/api/v1`, votre clé OpenRouter et ses noms de
modèles.

## Goose

Le fournisseur OpenAI de Goose prend un hôte et un chemin, qu'il assemble :

```sh
export OPENAI_HOST=http://127.0.0.1:5283
export OPENAI_BASE_PATH=openai/v1/chat/completions
export OPENAI_API_KEY=...        # your OpenAI key, as usual
export GOOSE_PROVIDER=openai
export GOOSE_MODEL=gpt-5.1
goose session
```

`GOOSE_PROVIDER` et `GOOSE_MODEL` choisissent le fournisseur et le modèle : sans elles, Goose vous
demande de lancer d'abord `goose configure`, qui enregistre les mêmes valeurs. Pour OpenRouter,
ajoutez avec `goose configure` un fournisseur personnalisé compatible OpenAI, avec l'URL de base
`http://127.0.0.1:5283/openrouter/api/v1` et votre clé OpenRouter ; cette configuration ne fait pas
partie de celles qui ont été testées.

## OpenHands

Dans les réglages LLM d'OpenHands, sous **Advanced**, choisissez un modèle personnalisé avec le préfixe de
son fournisseur (`openai/gpt-5.1`), réglez l'URL de base sur `http://127.0.0.1:5283/openai/v1` et
saisissez votre clé d'API. En ligne de commande :

```sh
LLM_MODEL=openai/gpt-5.1 LLM_BASE_URL=http://127.0.0.1:5283/openai/v1 LLM_API_KEY=... \
  openhands --override-with-envs
```

## Tout autre agent

Un agent qui accepte une URL de base compatible OpenAI passe par Latet avec les adresses du tableau
ci-dessus. Un agent qui lit `OPENAI_BASE_URL` ou `ANTHROPIC_BASE_URL` dans l'environnement peut être lancé
avec [`latet run`](../openai-compatible/#pour-une-commande--latet-run), qui les définit pour lui seul.

**Un agent dans un conteneur** n'atteint pas Latet à `127.0.0.1` : là, `127.0.0.1` est le conteneur
lui-même, et Latet ne répond qu'aux requêtes adressées à `127.0.0.1` ou `localhost`. Lancez l'agent sur
votre machine ; sous Linux, un conteneur démarré avec `--network host` partage aussi le `127.0.0.1` de
votre machine.

## Pour un agent que personne ne surveille

- **Activez le [relecteur local](../../concepts/llm-review/)** si vous avez un serveur de modèles local.
  Il lit ce que l'agent récupère, la partie que personne ne tape, et par défaut une requête attend sa
  relecture et est arrêtée quand le relecteur ne peut pas répondre.
- **Utilisez le [mode données strict](../../concepts/production-data/)** pour un agent qui lit des bases
  de données ou des journaux : chaque valeur d'une table ou d'un document JSON est masquée, et les
  jointures fonctionnent toujours.
- **Surveillez-le dans le tableau de bord :** `latet dashboard` affiche chaque requête que l'agent a
  envoyée, ce qui y a été masqué, et ce qui a été bloqué.
- **Laissez-le gérer Latet, en toute sécurité :** indiquez-lui
  [`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md). Ce qui ajoute de la
  protection s'applique ; ce qui la réduit attend votre approbation dans le tableau de bord. Voir
  [Les agents et vos réglages](../../concepts/agents-and-settings/).

Chacun de ces quatre agents est testé pour de vrai, configuré comme ci-dessus : il envoie une tâche
via Latet à un fournisseur de test qui répond comme OpenAI ; le fournisseur reçoit des valeurs
fictives, et l'agent récupère les vraies. Si un agent ne fonctionne pas via Latet, merci
d'[ouvrir un ticket](https://github.com/Latet-AI/latet/issues).
