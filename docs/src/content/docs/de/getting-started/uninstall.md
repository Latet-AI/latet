---
title: Deinstallation
description: Entfernen Sie Latet sauber und in der richtigen Reihenfolge, damit Ihre Agenten weiter funktionieren.
---

Die Reihenfolge ist wichtig: Machen Sie die Konfiguration der Agenten rückgängig, **bevor** Sie Latet
entfernen. Sonst senden Ihre Agenten ihren Datenverkehr weiter an `127.0.0.1:5283` und scheitern mit
Verbindungsfehlern, sobald der Daemon nicht mehr da ist.

## 1. Latet ausschalten

```sh
latet global-uninstall
```

Dieser eine Befehl:

- stellt jeden Agenten wieder her, den Latet konfiguriert hat. Bei Claude Code deinstalliert er das
  Plugin und stellt `settings.json` so wieder her, wie die Datei vor `latet global-install` war. Bei Codex
  entfernt er die Zeile `openai_base_url`, bei OpenCode die überschriebenen Werte für `baseURL`. Wenn
  Sie eine dieser Dateien seitdem bearbeitet haben, entfernt Latet nur, was es hinzugefügt hat, und
  behält Ihre Änderungen;
- entfernt den Autostart bei der Anmeldung;
- beendet den Daemon.

Er ändert nicht die Tools, die Sie selbst auf Latet gerichtet haben, in ihren eigenen Einstellungen
oder in Ihrer Shell: GitHub Copilot, Zed, Cline, Kilo Code, Continue, Crush, Prime Agent, Junie,
Android Studio, Hermes Agent, OpenClaw, Goose, OpenHands, Aider und eine Zeile `latet env` in Ihrem
Shell-Profil. Stellen Sie in jedem davon die eigene Adresse wieder ein, sonst scheitern sie mit
Verbindungsfehlern, sobald der Daemon beendet ist.

Er gibt für jeden Teil aus, was er getan hat, zum Beispiel:

```text
Claude Code: restored
Codex: restored
OpenCode: not applied
autostart: removed
daemon: stopped
```

Starten Sie Ihre Agenten danach neu. Um stattdessen einen einzelnen Agenten wiederherzustellen,
führen Sie `latet agents revert claude` aus (oder `codex`, `opencode`).

## 2. Das Tool entfernen

```sh
uv tool uninstall latet
```

Wenn Sie Latet aus einem Checkout des Quellcodes ausgeführt haben, gibt es nichts zu deinstallieren:
Führen Sie in Schritt 1 `uv run latet global-uninstall` aus, und löschen Sie dann den Checkout.
Wenn Sie es mit `py -m pip` installiert haben, führen Sie in Schritt 1 `py -m latet global-uninstall`
aus und dann `py -m pip uninstall latet`.

## 3. Lokale Daten löschen (optional)

Um Ihre Einstellungen und den Tresor zu behalten, für später oder für einen anderen Rechner, sichern
Sie sie zuerst: `latet backup latet.backup --vault` (siehe
[Export und Import](../../concepts/export-import/#ihre-installation-für-einen-anderen-rechner)).

Latet bewahrt seine Daten nur auf Ihrem Rechner auf, in zwei Ordnern:

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Daten | `%LOCALAPPDATA%\latet` | `~/Library/Application Support/latet` | `~/.local/share/latet` |
| Konfiguration (`config.yaml`) | `%APPDATA%\latet` | `~/Library/Application Support/latet` | `~/.config/latet` |

Wenn Sie `LATET_DATA_DIR` oder `LATET_CONFIG` gesetzt haben, sehen Sie stattdessen dort nach. Der
Datenordner enthält:

- `latet.db`: den Tresor, der jedem Original seinen Ersatzwert zuordnet. Die Originale sind
  verschlüsselt.
- `exchanges.db`: den Verlauf des Dashboards.
- `backups/`: Kopien der Konfigurationsdateien Ihrer Agenten aus der Zeit, bevor
  `latet global-install` sie geändert hat. `latet global-uninstall` braucht sie, löschen Sie sie
  daher erst nach Schritt 1.
- `models/`: das lokale Namensmodell und die Modelle für die Texterkennung (etwa 1,6 GB und 31 MB),
  die `latet ner remove` und `latet ocr remove` löschen.
- `logs/`, `bridge.token`, `integrations.json` (die Aufzeichnung dessen, was `latet global-install`
  geändert hat), `claude-marketplace/` (die Kopie des Plugins für Claude Code), `tutorial.json` (die
  Kapitel des [Tutorials](../tutorial/), die Sie abgeschlossen haben), `repositories.json` (die Ordner
  der Repositorys mit einer `latet.yaml`, die Latet gesehen hat) und `root.key`, falls Ihr System
  keinen Anmeldedatenspeicher hat.

Der Schlüssel, der den Tresor verschlüsselt, liegt im Anmeldedatenspeicher Ihres Betriebssystems
(Windows-Anmeldeinformationsverwaltung, macOS-Schlüsselbund oder der Schlüsselbund des Desktops unter
Linux), unter dem Dienstnamen `latet` und dem Konto `root-key`. Löschen Sie diesen Eintrag ebenfalls.

## Wenn der Befehl `latet` schon fehlt

Machen Sie die Änderungen von Hand rückgängig:

- **Claude Code**: Führen Sie `claude plugin uninstall latet@latet` und
  `claude plugin marketplace remove latet` aus. Entfernen Sie dann diese Schlüssel aus dem Block
  `env` von `~/.claude/settings.json` (oder `$CLAUDE_CONFIG_DIR/settings.json`):
  `ANTHROPIC_BASE_URL`, `LATET_BRIDGE_URL`, `LATET_BRIDGE_TOKEN`, `ENABLE_TOOL_SEARCH` und
  `CLAUDE_CODE_AUTO_MODE_SERVER`. Behalten Sie die, die Sie schon vorher selbst gesetzt hatten.
  Entfernen Sie auch `LATET_BRIDGE_HELPER`, `NODE_EXTRA_CA_CERTS`, `USER` und `USERNAME`, falls
  Latet sie gesetzt hat, und stellen Sie jede Adresse, die auf `127.0.0.1` zeigt
  (`ANTHROPIC_BEDROCK_BASE_URL`, `ANTHROPIC_VERTEX_BASE_URL`, `ANTHROPIC_FOUNDRY_BASE_URL`), so
  wieder her, wie sie in der Kopie von `settings.json` in `backups/` steht.
- **Codex**: Entfernen Sie die Zeile `openai_base_url` aus `~/.codex/config.toml` (oder
  `$CODEX_HOME/config.toml`), und stellen Sie bei einem eigenen Anbieter die `base_url` seines
  Abschnitts `[model_providers.<id>]` aus der Kopie in `backups/` wieder her, in `config.toml` oder
  in der `<name>.config.toml` des Profils.
- **OpenCode**: Entfernen Sie die Einträge `baseURL`, die auf `127.0.0.1:5283` zeigen, aus
  `~/.config/opencode/opencode.jsonc` (oder `opencode.json`).
- **Andere Clients**: Entfernen Sie die Umgebungsvariablen `OPENAI_BASE_URL`, `OPENAI_API_BASE`,
  `ANTHROPIC_BASE_URL`, `GOOGLE_GEMINI_BASE_URL` und `GOOGLE_VERTEX_BASE_URL`, oder setzen Sie
  wieder die Base-URL, die Sie vorher verwendet haben. Stellen Sie in den Tools, die Sie in ihren
  eigenen Einstellungen auf Latet gerichtet haben, ihre eigene Adresse wieder ein.
- **Autostart bei der Anmeldung**: Löschen Sie unter Windows den Wert `Latet` unter
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. Führen Sie unter macOS
  `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.latet.daemon.plist` aus, und löschen
  Sie dann diese Datei. Führen Sie unter Linux `systemctl --user disable latet.service` aus, und
  löschen Sie dann `~/.config/systemd/user/latet.service`.

Kopien Ihrer Konfigurationsdateien aus der Zeit vor `latet global-install` liegen im Ordner
`backups/` des Datenordners.
