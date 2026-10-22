---
title: OpenCode
description: Die Anbieter von OpenCode, die mit einem API-Schlüssel angebunden sind, über das Gateway von Latet leiten.
---

OpenCode nutzt den **Gateway-Modus**: Der lokale Daemon maskiert die Anfragen und setzt in den
Antworten die echten Werte wieder ein. Abgedeckt sind die Anbieter, die Sie mit einem API-Schlüssel
nutzen: Anthropic, OpenAI und OpenRouter.

## Über Latet leiten

`latet global-install` oder `latet agents apply opencode` richtet diese drei Anbieter auf den Daemon
aus, in Ihrer globalen Konfiguration von OpenCode, `~/.config/opencode/opencode.jsonc` (oder
`opencode.json`, wenn Sie diese Datei haben). Die Datei wird vorher gesichert, und ihre Kommentare
bleiben erhalten. Hinzugefügt wird:

```json title="~/.config/opencode/opencode.jsonc"
{
  "provider": {
    "anthropic": { "options": { "baseURL": "http://127.0.0.1:5283/anthropic/v1" } },
    "openai": { "options": { "baseURL": "http://127.0.0.1:5283/openai/v1" } },
    "openrouter": { "options": { "baseURL": "http://127.0.0.1:5283/openrouter/api/v1" } }
  }
}
```

Ihre API-Schlüssel bleiben wie bisher in OpenCode: Das Gateway leitet sie unverändert weiter und
schreibt ihre Werte nie in Logs oder ins Dashboard. Starten Sie OpenCode danach neu.

Die eigene `opencode.json` eines Projekts wird nach Ihrer globalen Datei gelesen, und eine Adresse,
die sie für einen dieser Anbieter setzt, tritt an die Stelle der Adresse von Latet: Starten Sie
OpenCode in einem solchen Projekt mit `latet opencode`, dessen Adressen OpenCode zuletzt liest.

**Ohne Ihre Konfiguration anzufassen**, lassen Sie eine Sitzung mit dem Wrapper über Latet laufen.
Er gibt OpenCode dieselben Überschreibungen nur für diesen Lauf (über `OPENCODE_CONFIG_CONTENT`),
startet bei Bedarf den Daemon und gibt jedes Argument an `opencode` weiter:

```sh
latet opencode
latet opencode run "add the payments key from .env to config/local.toml"
```

**OpenCode Desktop** liest dieselbe globale Konfiguration: `latet agents apply opencode` (oder
`latet global-install`) leitet die App über Latet, sobald Sie sie neu starten, während
`latet opencode` nur das OpenCode der Befehlszeile startet. `latet agents revert opencode` entfernt
die Adressen von Latet wieder und lässt den Rest der Datei, wie er ist.

## Grenzen

- **Nur diese drei Anbieter werden über Latet geleitet.** Mit dem Modell eines anderen Anbieters
  (zum Beispiel Amazon Bedrock oder Azure in OpenCode) spricht OpenCode direkt mit diesem Anbieter,
  ohne Latet, und nichts warnt Sie davor: Wählen Sie ein Modell von Anthropic, OpenAI oder
  OpenRouter. Claude Code und Codex laufen auch mit diesen Clouds über Latet: siehe
  [Unternehmens-Endpunkte](../company-endpoints/).
- **Die Anmeldung mit ChatGPT ist nicht abgedeckt.** Das ChatGPT-Plugin von OpenCode hat die Adresse
  des Anbieters fest eingebaut, daher kann eine Base-URL es nicht umleiten. OpenCode mit der
  Anmeldung bei ChatGPT steht auf der [Roadmap](../../roadmap/).
- **Keine Anmeldung mit Claude Pro oder Max.** Anthropic erlaubt ein Abonnement in Claude Code und
  seinen eigenen Apps, und Latet schreibt eine Anfrage, die das Token eines Claude-Abonnements
  enthält, nie um: Es prüft sie nur und blockiert sie, wenn ein sensibler Wert darin steht. Ohne ein
  Plugin, das den Inhalt vorher maskiert, würden solche Anfragen blockiert statt maskiert. Verwenden
  Sie einen API-Schlüssel von Anthropic.

Um Latet nicht mehr zu verwenden, führen Sie `latet agents revert opencode` aus, oder
`latet global-uninstall` für alle Agenten.
