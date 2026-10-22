---
title: OpenCode
description: Faites passer les fournisseurs à clé d'API d'OpenCode par la passerelle de Latet.
---

OpenCode utilise le **mode passerelle** : le daemon local masque les requêtes et remet les originaux en
place dans les réponses. Il couvre les fournisseurs que vous utilisez avec une clé d'API : Anthropic,
OpenAI et OpenRouter.

## Le faire passer par Latet

`latet global-install`, ou `latet agents apply opencode`, fait pointer ces trois fournisseurs vers le
daemon dans votre configuration globale d'OpenCode, `~/.config/opencode/opencode.jsonc` (ou
`opencode.json` si c'est le fichier que vous avez). Le fichier est d'abord sauvegardé, et ses commentaires
sont conservés. La commande ajoute :

```json title="~/.config/opencode/opencode.jsonc"
{
  "provider": {
    "anthropic": { "options": { "baseURL": "http://127.0.0.1:5283/anthropic/v1" } },
    "openai": { "options": { "baseURL": "http://127.0.0.1:5283/openai/v1" } },
    "openrouter": { "options": { "baseURL": "http://127.0.0.1:5283/openrouter/api/v1" } }
  }
}
```

Vos clés d'API restent dans OpenCode comme avant : la passerelle les transmet inchangées et n'écrit jamais
leurs valeurs dans les journaux ni dans le tableau de bord. Redémarrez ensuite OpenCode.

Le fichier `opencode.json` d'un projet est lu après votre fichier global, et une adresse qu'il fixe pour
l'un de ces fournisseurs prend la place de celle de Latet : dans un tel projet, démarrez OpenCode avec
`latet opencode`, dont OpenCode lit les adresses en dernier.

**Sans toucher à votre configuration**, faites passer une seule session par Latet avec le lanceur. Il
transmet les mêmes surcharges à OpenCode pour cette exécution seulement (via `OPENCODE_CONFIG_CONTENT`),
démarre le daemon si besoin, et transmet tous les arguments à `opencode` :

```sh
latet opencode
latet opencode run "add the payments key from .env to config/local.toml"
```

**OpenCode Desktop** lit la même configuration globale : `latet agents apply opencode` (ou
`latet global-install`) le fait passer par Latet une fois l'application redémarrée, tandis que
`latet opencode` ne lance que l'OpenCode en ligne de commande. `latet agents revert opencode` retire
à nouveau les adresses de Latet et laisse le reste du fichier tel quel.

## Limites

- **Seuls ces trois fournisseurs passent par Latet.** Un modèle de tout autre fournisseur (Amazon Bedrock
  ou Azure dans OpenCode, par exemple) dialogue directement avec son fournisseur, sans Latet, et rien ne
  vous avertit : choisissez un modèle d'Anthropic, d'OpenAI ou d'OpenRouter.
  Claude Code et Codex passent par Latet avec ces clouds aussi : voir
  [Points de terminaison d'entreprise](../company-endpoints/).
- **La connexion ChatGPT n'est pas couverte.** Le plugin ChatGPT d'OpenCode a l'adresse du fournisseur
  intégrée en dur : une URL de base ne peut donc pas le rediriger. OpenCode connecté avec ChatGPT figure
  dans la [feuille de route](../../roadmap/).
- **Pas de connexion Claude Pro ou Max.** Anthropic autorise un abonnement dans Claude Code et dans ses
  propres applications, et Latet ne réécrit jamais une requête qui porte un jeton d'abonnement Claude : il
  la vérifie seulement, et la bloque si elle contient une valeur sensible. Sans un plugin qui masque
  d'abord le contenu, de telles requêtes seraient bloquées au lieu d'être masquées. Utilisez une clé d'API
  Anthropic.

Pour ne plus utiliser Latet, lancez `latet agents revert opencode`, ou `latet global-uninstall` pour tous
les agents.
