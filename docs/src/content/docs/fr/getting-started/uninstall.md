---
title: Désinstallation
description: Supprimez Latet proprement, dans le bon ordre, pour que vos agents continuent de fonctionner.
---

L'ordre compte : annulez la configuration des agents **avant** de supprimer Latet. Sinon, vos agents
continuent d'envoyer leur trafic vers `127.0.0.1:5283` et échouent avec des erreurs de connexion une
fois le daemon disparu.

## 1. Désactiver Latet

```sh
latet global-uninstall
```

Cette seule commande :

- rétablit chaque agent que Latet a configuré. Pour Claude Code, elle désinstalle le plugin et remet
  `settings.json` tel qu'il était avant `latet global-install`. Pour Codex, elle supprime la ligne
  `openai_base_url`, pour OpenCode les surcharges de `baseURL`. Si vous avez modifié l'un de ces
  fichiers depuis, Latet ne retire que ce qu'il a ajouté et conserve vos modifications ;
- supprime le démarrage à l'ouverture de session ;
- arrête le daemon.

Elle ne change pas les outils que vous avez pointés vers Latet vous-même, dans leurs propres
réglages ou dans votre shell : GitHub Copilot, Zed, Cline, Kilo Code, Continue, Crush, Prime Agent,
Junie, Android Studio, Hermes Agent, OpenClaw, Goose, OpenHands, Aider, et une ligne `latet env`
dans le profil de votre shell. Remettez l'adresse propre à chacun, sinon ils échouent avec des
erreurs de connexion une fois le daemon arrêté.

Elle affiche ce qu'elle a fait pour chaque partie, par exemple :

```text
Claude Code: restored
Codex: restored
OpenCode: not applied
autostart: removed
daemon: stopped
```

Redémarrez ensuite vos agents. Pour rétablir un seul agent, lancez plutôt
`latet agents revert claude` (ou `codex`, `opencode`).

## 2. Supprimer l'outil

```sh
uv tool uninstall latet
```

Si vous exécutiez Latet depuis une copie locale du dépôt, il n'y a rien à désinstaller : lancez
`uv run latet global-uninstall` à l'étape 1, puis supprimez la copie locale. Si vous l'avez installé
avec `py -m pip`, lancez `py -m latet global-uninstall` à l'étape 1, puis `py -m pip uninstall latet`.

## 3. Supprimer les données locales (facultatif)

Pour garder vos réglages et le coffre-fort, pour plus tard ou pour une autre machine, sauvegardez-les
d'abord : `latet backup latet.backup --vault` (voir
[Exportation et importation](../../concepts/export-import/#votre-installation-pour-une-autre-machine)).

Latet conserve ses données uniquement sur votre machine, dans deux dossiers :

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Données | `%LOCALAPPDATA%\latet` | `~/Library/Application Support/latet` | `~/.local/share/latet` |
| Configuration (`config.yaml`) | `%APPDATA%\latet` | `~/Library/Application Support/latet` | `~/.config/latet` |

Si vous avez défini `LATET_DATA_DIR` ou `LATET_CONFIG`, regardez plutôt là. Le dossier de données
contient :

- `latet.db` : le coffre-fort, qui associe chaque original à sa valeur factice. Les originaux sont
  chiffrés.
- `exchanges.db` : l'historique du tableau de bord.
- `backups/` : des copies des fichiers de configuration de vos agents d'avant leur modification par
  `latet global-install`. `latet global-uninstall` en a besoin : ne les supprimez donc qu'après
  l'étape 1. Dans `backups/before-<version>-<heure>/` se trouvent les copies du coffre-fort, de
  l'historique et des réglages que `latet upgrade` fait avant chaque mise à jour (les trois plus
  récentes).
- `models/` : le modèle local de noms et les modèles de reconnaissance de texte (environ 1,6 Go et
  31 Mo), que suppriment `latet ner remove` et `latet ocr remove`.
- `logs/`, `bridge.token`, `integrations.json` (la trace de ce que `latet global-install` a
  changé), `claude-marketplace/` (la copie du plugin Claude Code), `tutorial.json` (les chapitres du
  [tutoriel](../tutorial/) que vous avez terminés), `repositories.json` (les dossiers des dépôts avec
  un `latet.yaml` que Latet a vus), et `root.key` si votre système n'a pas de magasin d'identifiants.

La clé qui chiffre le coffre-fort se trouve dans le magasin d'identifiants de votre système
d'exploitation (le Gestionnaire d'identification de Windows, le Trousseau de macOS ou le trousseau de
clés de l'environnement de bureau sous Linux), sous le nom de service `latet` et le compte
`root-key`. Supprimez aussi cette entrée.

## Si la commande `latet` a déjà disparu

Annulez les modifications à la main :

- **Claude Code** : lancez `claude plugin uninstall latet@latet` et
  `claude plugin marketplace remove latet`. Retirez ensuite ces clés du bloc `env` de
  `~/.claude/settings.json` (ou `$CLAUDE_CONFIG_DIR/settings.json`) : `ANTHROPIC_BASE_URL`,
  `LATET_BRIDGE_URL`, `LATET_BRIDGE_TOKEN`, `ENABLE_TOOL_SEARCH` et
  `CLAUDE_CODE_AUTO_MODE_SERVER`. Gardez celles que vous aviez définies vous-même auparavant.
  Supprimez aussi `LATET_BRIDGE_HELPER`, `NODE_EXTRA_CA_CERTS`, `USER` et `USERNAME` si Latet les a
  définies, et remettez telle qu'elle est dans la copie de `settings.json` de `backups/` toute adresse
  qui pointe vers `127.0.0.1` (`ANTHROPIC_BEDROCK_BASE_URL`, `ANTHROPIC_VERTEX_BASE_URL`,
  `ANTHROPIC_FOUNDRY_BASE_URL`).
- **Codex** : supprimez la ligne `openai_base_url` de `~/.codex/config.toml` (ou
  `$CODEX_HOME/config.toml`), et, pour un fournisseur à vous, remettez le `base_url` de sa section
  `[model_providers.<id>]`, dans `config.toml` ou dans le `<name>.config.toml` du profil, d'après la
  copie de `backups/`.
- **OpenCode** : supprimez de `~/.config/opencode/opencode.jsonc` (ou `opencode.json`) les entrées
  `baseURL` qui pointent vers `127.0.0.1:5283`.
- **Autres clients** : supprimez les variables `OPENAI_BASE_URL`, `OPENAI_API_BASE`,
  `ANTHROPIC_BASE_URL`, `GOOGLE_GEMINI_BASE_URL` et `GOOGLE_VERTEX_BASE_URL`, ou remettez l'URL de base
  que vous utilisiez avant. Dans les outils que vous avez pointés vers Latet dans leurs propres
  réglages, remettez leur propre adresse.
- **Démarrage à l'ouverture de session** : sous Windows, supprimez la valeur `Latet` sous
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. Sous macOS, lancez
  `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.latet.daemon.plist`, puis supprimez ce
  fichier. Sous Linux, lancez `systemctl --user disable latet.service`, puis supprimez
  `~/.config/systemd/user/latet.service`.

Les copies de vos fichiers de configuration d'avant `latet global-install` se trouvent dans le
dossier `backups/` du dossier de données.
