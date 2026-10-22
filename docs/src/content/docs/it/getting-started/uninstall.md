---
title: Disinstallazione
description: Rimuovi Latet in modo pulito, nell'ordine giusto, così i tuoi agenti continuano a funzionare.
---

L'ordine conta: annulla la configurazione degli agenti **prima** di rimuovere Latet. Altrimenti i
tuoi agenti continuano a inviare il loro traffico a `127.0.0.1:5283` e falliscono con errori di
connessione quando il daemon non c'è più.

## 1. Disattiva Latet

```sh
latet global-uninstall
```

Quest'unico comando:

- ripristina ogni agente che Latet ha configurato. Per Claude Code disinstalla il plugin e riporta
  `settings.json` com'era prima di `latet global-install`. Per Codex rimuove la riga
  `openai_base_url`, per OpenCode gli override di `baseURL`. Se nel frattempo hai modificato uno di
  questi file, Latet rimuove solo ciò che ha aggiunto e mantiene le tue modifiche;
- rimuove l'avvio all'accesso;
- ferma il daemon.

Non cambia gli strumenti che hai puntato tu su Latet, nelle loro impostazioni o nella tua shell:
GitHub Copilot, Zed, Cline, Kilo Code, Continue, Crush, Junie, Android Studio, Hermes Agent,
OpenClaw, Goose, OpenHands, Aider, e una riga `latet env` nel profilo della tua shell. Rimetti in
ciascuno il suo indirizzo, altrimenti falliscono con errori di connessione quando il daemon è fermo.

Stampa cosa ha fatto per ciascuna parte, per esempio:

```text
Claude Code: restored
Codex: restored
OpenCode: not applied
autostart: removed
daemon: stopped
```

Poi riavvia i tuoi agenti. Per ripristinare invece un solo agente, esegui
`latet agents revert claude` (o `codex`, `opencode`).

## 2. Rimuovi lo strumento

```sh
uv tool uninstall latet
```

Se eseguivi Latet da un checkout dei sorgenti, non c'è niente da disinstallare: esegui
`uv run latet global-uninstall` al passaggio 1, poi elimina il checkout. Se l'hai installato con
`py -m pip`, esegui `py -m latet global-uninstall` al passaggio 1, poi `py -m pip uninstall latet`.

## 3. Elimina i dati locali (facoltativo)

Per conservare le tue impostazioni e la cassaforte, per dopo o per un altro computer, prima salvale:
`latet backup latet.backup --vault` (vedi
[Esportazione e importazione](../../concepts/export-import/#la-tua-installazione-per-un-altro-computer)).

Latet conserva i suoi dati solo sul tuo computer, in due cartelle:

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Dati | `%LOCALAPPDATA%\latet` | `~/Library/Application Support/latet` | `~/.local/share/latet` |
| Configurazione (`config.yaml`) | `%APPDATA%\latet` | `~/Library/Application Support/latet` | `~/.config/latet` |

Se hai impostato `LATET_DATA_DIR` o `LATET_CONFIG`, guarda invece lì. La cartella dei dati contiene:

- `latet.db`: la cassaforte, che associa ogni originale al suo valore fittizio. Gli originali sono
  cifrati.
- `exchanges.db`: la cronologia della dashboard.
- `backups/`: le copie dei file di configurazione dei tuoi agenti di prima che `latet global-install`
  li modificasse. `latet global-uninstall` ne ha bisogno, quindi eliminale solo dopo il passaggio 1.
- `models/`: il modello locale dei nomi e i modelli di riconoscimento del testo (circa 1,6 GB e
  31 MB), che `latet ner remove` e `latet ocr remove` eliminano.
- `logs/`, `bridge.token`, `integrations.json` (il registro di ciò che `latet global-install` ha
  cambiato), `claude-marketplace/` (la copia del plugin di Claude Code), `tutorial.json` (i capitoli
  del [tutorial](../tutorial/) che hai completato), `repositories.json` (le cartelle dei repository con
  un `latet.yaml` che Latet ha visto), e `root.key` se il tuo sistema non ha un archivio delle
  credenziali.

La chiave che cifra la cassaforte sta nell'archivio delle credenziali del tuo sistema operativo
(Gestione credenziali di Windows, il Portachiavi di macOS, o il portachiavi del desktop su Linux),
con il nome di servizio `latet` e l'account `root-key`. Elimina anche quella voce.

## Se il comando `latet` non c'è più

Annulla le modifiche a mano:

- **Claude Code**: esegui `claude plugin uninstall latet@latet` e
  `claude plugin marketplace remove latet`. Poi rimuovi queste chiavi dal blocco `env` di
  `~/.claude/settings.json` (o `$CLAUDE_CONFIG_DIR/settings.json`): `ANTHROPIC_BASE_URL`,
  `LATET_BRIDGE_URL`, `LATET_BRIDGE_TOKEN`, `ENABLE_TOOL_SEARCH` e
  `CLAUDE_CODE_AUTO_MODE_SERVER`. Mantieni quelle che avevi già impostato tu prima. Rimuovi anche
  `LATET_BRIDGE_HELPER`, `NODE_EXTRA_CA_CERTS`, `USER` e `USERNAME` se le ha impostate Latet, e
  rimetti com'è nella copia di `settings.json` in `backups/` ogni indirizzo che punta a `127.0.0.1`
  (`ANTHROPIC_BEDROCK_BASE_URL`, `ANTHROPIC_VERTEX_BASE_URL`, `ANTHROPIC_FOUNDRY_BASE_URL`).
- **Codex**: rimuovi la riga `openai_base_url` da `~/.codex/config.toml` (o
  `$CODEX_HOME/config.toml`) e, per un provider tuo, rimetti il `base_url` della sua sezione
  `[model_providers.<id>]`, in `config.toml` o nel `<name>.config.toml` del profilo, dalla copia in
  `backups/`.
- **OpenCode**: rimuovi da `~/.config/opencode/opencode.jsonc` (o `opencode.json`) le voci `baseURL`
  che puntano a `127.0.0.1:5283`.
- **Altri client**: rimuovi le variabili `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `ANTHROPIC_BASE_URL`,
  `GOOGLE_GEMINI_BASE_URL` e `GOOGLE_VERTEX_BASE_URL`, o rimetti l'URL di base che usavi prima.
  Negli strumenti che hai puntato tu su Latet nelle loro impostazioni, rimetti il loro indirizzo.
- **Avvio all'accesso**: su Windows, elimina il valore `Latet` in
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. Su macOS, esegui
  `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.latet.daemon.plist`, poi elimina quel
  file. Su Linux, esegui `systemctl --user disable latet.service`, poi elimina
  `~/.config/systemd/user/latet.service`.

Le copie dei tuoi file di configurazione di prima di `latet global-install` sono nella cartella
`backups/` della cartella dei dati.
