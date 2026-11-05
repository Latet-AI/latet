---
title: Codex (CLI et Desktop)
description: Faites passer Codex CLI et Codex Desktop par la passerelle de Latet avec openai_base_url.
sidebar:
  label: Codex
---

Codex utilise le **mode passerelle** : le daemon local masque les requêtes et remet les originaux en place
dans les réponses. Cela fonctionne que vous vous connectiez avec ChatGPT ou que vous utilisiez une clé
d'API OpenAI.

## Le faire passer par Latet

Codex CLI et Codex Desktop lisent le même fichier de configuration, `~/.codex/config.toml` (ou
`$CODEX_HOME/config.toml` si vous définissez `CODEX_HOME`). Une seule ligne y suffit pour faire passer
Codex par Latet.

`latet global-install`, ou `latet agents apply codex`, l'écrit pour vous. La commande demande à Codex
comment vous vous connectez (`codex login status`) et choisit l'adresse qui correspond :

```toml title="~/.codex/config.toml"
# Signed in with ChatGPT:
openai_base_url = "http://127.0.0.1:5283/backend-api/codex"

# With an OpenAI API key instead:
# openai_base_url = "http://127.0.0.1:5283/openai/v1"
```

Le reste du fichier, commentaires compris, ne change pas, et une sauvegarde est faite au préalable.
Redémarrez ensuite Codex (ou l'application Codex Desktop).

- Les versions de Codex antérieures à la 0.115 ignorent `openai_base_url`. `latet doctor` vous en
  avertit : mettez Codex à jour (`npm i -g @openai/codex`). `latet global-install` et
  `latet agents apply codex` laissent ce Codex inchangé et disent pourquoi, puisque cette ligne ne le
  ferait pas passer par Latet. Tant que vous ne l'avez pas mis à jour, `latet codex` donne à Codex
  l'adresse dans `OPENAI_BASE_URL`, la seule variable que Codex lit, à la place de celle que définit
  votre shell. `latet codex` ne lance pas un Codex qui a un fournisseur à lui (voir plus bas) : ce
  Codex contournerait Latet.
- Si plus tard vous passez de la connexion ChatGPT à une clé d'API, ou l'inverse, relancez
  `latet agents apply codex` pour que l'adresse corresponde.

**Sans toucher à `config.toml`**, faites passer une seule session par Latet avec le lanceur. Il démarre
le daemon si besoin et transmet tous les arguments à `codex` :

```sh
latet codex
latet codex exec "add the payments key from .env to config/local.toml"
```

`latet run codex` revient au même que `latet codex`, qui choisit l'adresse selon votre connexion.

Sur un ordinateur connecté à un serveur Latet, `latet codex` lance la session interactive ainsi que
`codex exec`, `resume` et `fork` ; il refuse les autres sous-commandes de Codex, comme `review` ou `mcp`,
qu'il ne peut pas faire passer par le serveur.

**Un fournisseur à vous** (Azure OpenAI, la passerelle de votre entreprise, Mistral...) passe lui aussi par
Latet. Seul son `base_url` est redirigé vers Latet ; sa clé et tout le reste restent tels que vous les avez
écrits.

- **Pour une session**, `latet codex` redirige les fournisseurs de `config.toml` et du profil que vous
  nommez (`--profile work`), avec `-c model_providers.<id>.base_url=...`. Codex ne lit aucun autre
  fichier, et il refuse de démarrer avec une option visant un fournisseur qu'il ne charge pas. Un Codex
  antérieur à la version 0.115 ne peut pas recevoir ces options : s'il a un fournisseur à lui,
  `latet codex` ne le lance pas et indique de mettre Codex à jour.
- **Pour de bon**, `latet global-install` modifie chaque adresse dans le fichier qui l'écrit, y compris le
  fichier de chaque profil, puisqu'un profil peut faire basculer Codex vers n'importe laquelle de ces
  adresses.

Le `openai_base_url` propre à un profil (l'adresse d'une entreprise à la place de celle d'OpenAI) passe par
Latet de la même façon. Voir [Points de terminaison d'entreprise](../company-endpoints/).

## Comment il se comporte

- Avec une connexion ChatGPT, Codex dialogue avec le fournisseur par un WebSocket. Le daemon masque chaque
  requête que Codex y envoie et remet les originaux en place dans les événements qui reviennent en
  streaming. Avec une clé d'API, la même chose se passe en HTTP.
- Le fournisseur voit des valeurs factices. Codex voit les originaux, dans le texte de la réponse et dans
  les arguments des appels d'outils : les commandes qu'il exécute et les patchs qu'il applique utilisent
  donc les vraies valeurs. [Comment ça marche](../../concepts/how-it-works/) suit une clé de `.env` au fil
  d'un tour de Codex, étape par étape.
- Les corps de requête compressés sont décodés, masqués puis compressés à nouveau. Un corps que Latet ne
  peut pas décoder est refusé, et non envoyé sans vérification.
- Votre jeton de connexion ou votre clé d'API est transmis inchangé ; sa valeur n'est jamais écrite dans
  les journaux ni dans le tableau de bord.
- Si le daemon ne tourne pas, Codex ne peut pas joindre le fournisseur. C'est voulu : rien ne sort sans
  être masqué.
- Codex lit les fichiers avec des commandes shell : le
  [mode données strict](../../concepts/production-data/#quels-outils) couvre donc aussi les fichiers qu'il
  lit. Nommez son outil de base de données dans `data_tools` pour ne couvrir que celui-ci.
- Le chemin, les remotes et le commit du dépôt que Codex ajoute à chaque requête, dans l'en-tête
  `x-codex-turn-metadata` et dans `client_metadata` du corps, sont retirés avant l'envoi, de même que
  l'en-tête `x-openai-subagent`, le type ou l'étiquette libre que Codex donne à un sous-agent ; le
  tableau de bord les compte. Sa liste de modèles passe ; un appel que Latet ne lit pas, comme récupérer une réponse enregistrée, est refusé.
- Vos propres en-têtes dans `http_headers` ou `env_http_headers` d'un fournisseur sont refusés,
  chacun nommé, tant qu'une [exception exacte](../../getting-started/upgrading-to-0-11/#approuver-une-exception-exacte) ne
  le laisse pas passer (`openai.responses.1`). Sur le WebSocket d'une connexion ChatGPT, aucune
  exception ne s'applique dans la 0.11.0 : un en-tête ou un paramètre de requête que Latet ne lit pas
  ferme la connexion avant qu'elle ne s'ouvre, et la fermeture dit pourquoi et nomme l'en-tête ou le
  paramètre, jamais sa valeur. L'historique enregistre le refus.
- **Son projet.** `latet codex` donne à Codex une clé de lancement par un fournisseur de
  `model_providers` (`env_http_headers`), à partir de Codex 0.115 : les réglages de son projet
  s'appliquent alors entièrement. Sur les fournisseurs intégrés de Codex (connecté avec ChatGPT, ou le
  fournisseur d'OpenAI), et avant la 0.115, aucune clé ne peut voyager : `latet codex` le dit au
  démarrage, et le projet que Codex nomme dans ses requêtes est déclaré, donc ses règles qui ajoutent
  de la protection s'appliquent et ses assouplissements non
  ([Projets](../../concepts/projects/#de-confiance-et-déclarés)).

Testé en conditions réelles : la CLI Codex connectée avec ChatGPT. Codex Desktop lit le même
`config.toml` ; si quelque chose n'y fonctionne pas, merci
d'[ouvrir un ticket](https://github.com/Latet-AI/latet/issues).

Pour ne plus utiliser Latet, lancez `latet agents revert codex`, ou `latet global-uninstall` pour tous les
agents. Voir [Désinstallation](../../getting-started/uninstall/).
