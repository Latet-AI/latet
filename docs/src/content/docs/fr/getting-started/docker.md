---
title: Docker
description: Exécutez le daemon Latet dans un conteneur, et dirigez-y vos agents.
---

Latet fonctionne aussi en conteneur : pas de Python sur votre machine, et le daemon tenu à l'écart du
reste du système. L'image contient le daemon, le tableau de bord et l'environnement d'exécution du
[modèle de noms](../installation/) et de la [reconnaissance de texte](../../concepts/images/),
facultatifs ; leurs modèles ne sont téléchargés que si vous le demandez. Vos agents restent sur
votre machine et communiquent avec le conteneur sur `127.0.0.1`.

Le conteneur ne peut ni modifier les réglages de vos agents ni savoir qui vous êtes. Deux étapes que
`latet global-install` fait de lui-même se font donc ici à la main : diriger les agents vers le
conteneur, et lui indiquer votre identité. Toutes deux se résument à un copier-coller.

## Le démarrer

Enregistrez [`compose.yaml`](https://github.com/Latet-AI/latet/blob/main/compose.yaml) depuis le
dépôt, puis :

```sh
docker compose up -d
```

Sans Compose, la même chose en une seule commande :

```sh
docker run -d --name latet --restart unless-stopped -p 127.0.0.1:5283:5283 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true --init \
  -v latet-data:/data -v latet-config:/config ghcr.io/latet-ai/latet:latest
```

`docker exec latet latet status` indique s'il est en marche. Le conteneur tourne sous un utilisateur
sans privilèges, sur un système de fichiers en lecture seule, et garde tout dans deux volumes :
`latet-data` (le coffre-fort, sa clé et l'historique du tableau de bord) et `latet-config`
(`config.yaml`).

## Y diriger vos agents

```sh
docker exec latet latet agents print
```

La commande affiche, pour chaque agent, ce qu'il faut changer et une commande qui le fait. Sous
Windows, ajoutez `--shell powershell`. Si le fichier de réglages d'un agent n'existe pas encore,
créez-le d'abord vide, par exemple avec `mkdir -p ~/.codex && touch ~/.codex/config.toml`. En bref :

- **Claude Code** a besoin du plugin et de quelques variables dans `~/.claude/settings.json` :

  ```sh
  claude plugin marketplace add Latet-AI/latet
  claude plugin install latet@latet
  docker exec -i latet latet agents merge claude < ~/.claude/settings.json > ~/.claude/settings.json.new \
    && mv ~/.claude/settings.json.new ~/.claude/settings.json
  ```

  `agents merge` lit le fichier, ajoute les variables et le réécrit : le jeton que le plugin
  présente au daemon entre ainsi dans le fichier sans être affiché. L'une des variables,
  `LATET_AUTOSTART=0`, indique au plugin de ne pas démarrer son propre daemon : si le conteneur est
  arrêté, Claude Code le signale et retient les requêtes jusqu'à son retour.
- **Codex** a besoin de `openai_base_url` dans `~/.codex/config.toml`, que cette commande écrit
  (`--codex-login api-key` si Codex se connecte avec une clé d'API plutôt qu'avec ChatGPT) :

  ```sh
  docker exec -i latet latet agents merge codex < ~/.codex/config.toml > ~/.codex/config.toml.new \
    && mv ~/.codex/config.toml.new ~/.codex/config.toml
  ```

- **OpenCode** a besoin d'un `baseURL` pour chaque fournisseur dans `opencode.json`, de la même
  façon.

PowerShell n'a pas de redirection `<` : `agents print --shell powershell` copie donc le fichier dans
le volume `latet-data` du conteneur avec `docker cp`, y fusionne les réglages, puis le recopie.

Redémarrez ensuite les agents. Pour ces routes, les [modes](../../concepts/how-it-works/) et la
protection sont les mêmes qu'avec le daemon installé sur votre machine : Anthropic pour Claude Code,
OpenAI ou ChatGPT pour Codex, et les fournisseurs d'OpenCode. Claude Code sur Bedrock, Vertex AI ou
Microsoft Foundry, un fournisseur propre à Codex et la passerelle d'une entreprise sont configurés
par `latet claude`, `latet codex` et `latet global-install`, qui tournent sur votre machine :
utilisez pour eux Latet installé sur votre machine.

## Lui dire qui vous êtes

Latet masque votre nom d'utilisateur dans les chemins, votre nom d'hôte et votre auteur git. Dans le
conteneur, il ne peut pas les voir : indiquez-les-lui donc une fois, depuis votre machine (c'est
aussi dans `agents print`) :

```sh
docker exec latet latet identity set --user-name "$(id -un)" --user-name "$(basename "$HOME")" \
  --host-name "$(hostname)" --full-name "$(git config --global user.name)" \
  --email "$(git config --global user.email)"
```

`docker exec latet latet identity show` affiche ce qu'il masque.

## Au quotidien

| Quoi | Commande |
| --- | --- |
| Ouvrir le tableau de bord | `docker exec latet latet dashboard --print-url`, puis ouvrir le lien sur votre machine |
| Tout vérifier | `docker exec latet latet doctor` |
| Télécharger le modèle de noms (environ 1,6 Go, une seule fois) | `docker exec latet latet ner install --yes` |
| Télécharger les modèles de reconnaissance de texte (environ 31 Mo, une seule fois) | `docker exec latet latet ocr install --yes` |
| Vos propres reconnaisseurs | `docker exec -i latet latet rules add - < rules.yaml` |
| Mettre à jour | `docker compose pull && docker compose up -d` |
| Un autre port | `LATET_PORT=6655 docker compose up -d`, puis `agents merge` à nouveau |
| Une sauvegarde des réglages et du coffre-fort | `docker exec -it latet latet backup /data/latet.backup --vault`, puis `docker cp latet:/data/latet.backup .` ([pour la restaurer](../../concepts/export-import/#dans-docker)) |

Les réglages se trouvent dans `config.yaml`, dans le volume `latet-config` ; la page **Règles** du
tableau de bord les modifie, et `docker exec latet latet reload` applique un fichier que vous avez
modifié.

## Les projets et le relecteur local

- **[Projets](../../concepts/projects/) :** vos propres réglages pour un projet fonctionnent d'après
  le chemin de son dossier. Le `latet.yaml` d'un projet n'est lu que lorsque le dossier est monté
  dans le conteneur au même chemin.
- **Le [relecteur local](../../concepts/llm-review/)** tourne sur votre machine, hors du conteneur,
  et dans le conteneur `127.0.0.1` désigne le conteneur lui-même. Donnez-lui l'adresse
  `http://host.docker.internal:8080/v1`, que Latet n'accepte que dans son conteneur. Sous Linux,
  ajoutez `extra_hosts: ["host.docker.internal:host-gateway"]` au service dans `compose.yaml`, et
  faites écouter le serveur du modèle sur une adresse que le conteneur peut atteindre.

## Sécurité

- **Ne publiez le port que sur `127.0.0.1`.** Sous Linux, les ports publiés par Docker contournent
  le pare-feu : `-p 5283:5283` ouvrirait à votre réseau Latet et les requêtes que vos agents
  envoient par lui. `compose.yaml` et la commande ci-dessus publient sur `127.0.0.1`.
- Les autres conteneurs du même réseau Docker peuvent atteindre le port. Le pont du plugin, le
  tableau de bord et les routes d'administration exigent quand même le jeton ou une session du
  tableau de bord, et chaque requête doit indiquer `127.0.0.1` ou `localhost` comme hôte ; les
  routes du proxy transmettent les identifiants de l'appelant lui-même.
- Quiconque peut lire le volume `latet-data` peut lire le coffre-fort : il contient la clé qui
  chiffre le coffre-fort. Pour garder la clé ailleurs, fournissez-la comme secret Docker et
  définissez `LATET_ROOT_KEY_FILE` sur son chemin.

## Derrière un proxy

Quand les fournisseurs sont joints via un proxy de votre réseau, définissez `HTTPS_PROXY` (et
`NO_PROXY`) avant `docker compose up` ; `compose.yaml` les transmet au conteneur. Si le proxy
inspecte le TLS avec son propre certificat, montez ce certificat dans le conteneur et faites
pointer `SSL_CERT_FILE` vers lui.

## Revenir en arrière

Cette commande retire les réglages de Claude Code (de même pour `codex` et `opencode`, avec leurs
fichiers) :

```sh
docker exec -i latet latet agents merge claude --remove < ~/.claude/settings.json > ~/.claude/settings.json.new \
  && mv ~/.claude/settings.json.new ~/.claude/settings.json
```

`claude plugin uninstall latet@latet` supprime le plugin. Puis `docker compose down` ;
`docker compose down -v` supprime aussi les volumes, et avec eux le coffre-fort.
