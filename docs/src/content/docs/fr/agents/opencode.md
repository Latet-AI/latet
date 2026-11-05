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

`latet opencode` donne aussi à OpenCode une clé de lancement, dans `options.headers` de chaque
fournisseur qu'il redirige, pour que Latet considère le projet de la session comme de confiance et applique
entièrement ses réglages. OpenCode configuré durablement n'en porte pas : le projet qu'il nomme dans
ses requêtes est déclaré, et seules ses règles qui ajoutent de la protection s'appliquent
([Projets](../../concepts/projects/#de-confiance-et-déclarés)). Vos propres en-têtes dans `options.headers` d'un
fournisseur sont refusés, chacun nommé, tant qu'une
[exception exacte](../../getting-started/upgrading-to-0-11/#approuver-une-exception-exacte) ne le laisse pas passer.

`latet opencode` ne passe cette clé qu'à OpenCode 1.19.5 ou plus récent, la version que Latet a
vérifiée : un OpenCode plus ancien pourrait envoyer la référence à la clé telle qu'elle est écrite, donc
en dessous de la 1.19.5, ou quand Latet ne peut pas lire la version d'OpenCode, il n'en passe aucune. Il
le dit au démarrage, le projet de la session est déclaré, et `latet doctor` nomme un OpenCode plus
ancien que la 1.19.5.

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
- **Les modèles d'Anthropic, d'OpenAI et de Google sur OpenRouter passent d'eux-mêmes.** Ceux de tout
  autre fournisseur (`meta-llama/…`, `deepseek/…`) sont refusés tant qu'ils ne sont pas listés dans
  `request_resources` :
  [Bedrock et Vertex : ce qu'il faut lister](../../getting-started/upgrading-to-0-11/#bedrock-et-vertex--ce-quil-faut-lister).
- **Une organisation de la console peut déplacer un fournisseur.** Quand OpenCode est connecté à une
  organisation de la console OpenCode, les réglages de l'organisation peuvent changer l'adresse d'un
  fournisseur, et les requêtes de ce fournisseur ne passent alors plus par Latet. Latet ne peut pas le
  voir : lancez `latet doctor`, et vérifiez les réglages de l'organisation.
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
