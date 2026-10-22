---
title: Codex (CLI und Desktop)
description: Codex CLI und Codex Desktop mit openai_base_url über das Gateway von Latet leiten.
sidebar:
  label: Codex
---

Codex nutzt den **Gateway-Modus**: Der lokale Daemon maskiert die Anfragen und setzt in den
Antworten die echten Werte wieder ein. Das funktioniert, egal ob Sie sich mit ChatGPT anmelden oder
einen API-Schlüssel von OpenAI verwenden.

## Über Latet leiten

Codex CLI und Codex Desktop lesen dieselbe Konfigurationsdatei, `~/.codex/config.toml` (oder
`$CODEX_HOME/config.toml`, wenn Sie `CODEX_HOME` gesetzt haben). Eine Zeile darin leitet Codex über
Latet.

`latet global-install` oder `latet agents apply codex` schreibt sie für Sie. Es fragt Codex, wie Sie
sich anmelden (`codex login status`), und wählt die passende Adresse:

```toml title="~/.codex/config.toml"
# Signed in with ChatGPT:
openai_base_url = "http://127.0.0.1:5283/backend-api/codex"

# With an OpenAI API key instead:
# openai_base_url = "http://127.0.0.1:5283/openai/v1"
```

Der Rest der Datei, Kommentare eingeschlossen, bleibt, wie er war, und vorher wird eine Sicherung
angelegt. Starten Sie Codex (oder die App Codex Desktop) danach neu.

- Codex vor Version 0.115 ignoriert `openai_base_url`. `latet doctor` warnt davor: Aktualisieren Sie
  Codex (`npm i -g @openai/codex`). `latet global-install` und `latet agents apply codex` lassen ein
  solches Codex, wie es ist, und sagen, warum, denn diese Zeile würde es nicht über Latet leiten. Bis
  Sie es aktualisieren, erhält Codex von `latet codex` die Adresse in `OPENAI_BASE_URL`, der einzigen
  Variable, die Codex liest, anstelle der Adresse, die Ihre Shell dort setzt. `latet codex` startet
  ein solches Codex nicht, wenn es einen eigenen Anbieter hat (siehe unten): Dieses Codex würde an
  Latet vorbeilaufen.
- Wenn Sie später zwischen der Anmeldung mit ChatGPT und einem API-Schlüssel wechseln, führen Sie
  `latet agents apply codex` erneut aus, damit die Adresse passt.

**Ohne `config.toml` anzufassen**, lassen Sie eine Sitzung mit dem Wrapper über Latet laufen. Er
startet bei Bedarf den Daemon und gibt jedes Argument an `codex` weiter:

```sh
latet codex
latet codex exec "add the payments key from .env to config/local.toml"
```

`latet run codex` ist dasselbe wie `latet codex`, das die Adresse passend zu Ihrer Anmeldung wählt.

Auf einem Rechner, der mit einem Latet-Server verbunden ist, startet `latet codex` die interaktive
Sitzung sowie `codex exec`, `resume` und `fork`; die übrigen Unterbefehle von Codex, etwa `review`
oder `mcp`, lehnt es ab, weil es sie nicht über den Server senden kann.

**Ein eigener Anbieter** (Azure OpenAI, das Gateway Ihres Unternehmens, Mistral...) läuft ebenfalls
über Latet. Nur seine `base_url` wechselt zu Latet; sein Schlüssel und alles andere bleiben, wie Sie
sie geschrieben haben.

- **Für eine Sitzung** leitet `latet codex` die Anbieter aus `config.toml` und aus dem Profil, das
  Sie nennen (`--profile work`), über Latet, mit `-c model_providers.<id>.base_url=...`. Codex liest
  keine andere Datei, und es verweigert den Start bei einer Option für einen Anbieter, den es nicht
  lädt. Ein Codex vor Version 0.115 kann diese Optionen nicht annehmen: Hat es einen eigenen
  Anbieter, startet `latet codex` es nicht und sagt, dass Sie Codex aktualisieren sollen.
- **Dauerhaft** ändert `latet global-install` jede Adresse in der Datei, in der sie steht, die Datei
  jedes Profils eingeschlossen, denn ein Profil kann Codex auf jede dieser Adressen umstellen.

Die eigene `openai_base_url` eines Profils (die Adresse eines Unternehmens anstelle der von OpenAI)
läuft auf dieselbe Weise über Latet. Siehe [Unternehmens-Endpunkte](../company-endpoints/).

## Wie es sich verhält

- Mit einer Anmeldung über ChatGPT spricht Codex über einen WebSocket mit dem Anbieter. Der Daemon
  maskiert jede Anfrage, die Codex darüber sendet, und setzt in den Ereignissen, die zurückgestreamt
  werden, die Originale wieder ein. Mit einem API-Schlüssel geschieht dasselbe über HTTP.
- Der Anbieter sieht Ersatzwerte. Codex sieht die Originale, im Text der Antwort und in den
  Argumenten von Tool-Aufrufen, sodass die Befehle, die es ausführt, und die Patches, die es
  anwendet, die echten Werte verwenden. [Funktionsweise](../../concepts/how-it-works/) verfolgt
  einen Schlüssel aus `.env` Schritt für Schritt durch eine Runde von Codex.
- Ein komprimierter Body einer Anfrage wird dekodiert, maskiert und wieder komprimiert. Ein Body,
  den Latet nicht dekodieren kann, wird abgelehnt, nicht ungeprüft gesendet.
- Ihr Anmelde-Token oder API-Schlüssel wird unverändert weitergeleitet; sein Wert wird nie in Logs
  oder ins Dashboard geschrieben.
- Wenn der Daemon nicht läuft, kann Codex den Anbieter nicht erreichen. Das ist Absicht: Nichts geht
  unmaskiert hinaus.
- Codex liest Dateien mit Shell-Befehlen, daher deckt der
  [strikte Datenmodus](../../concepts/production-data/#welche-tools) auch die Dateien ab, die es
  liest. Nennen Sie sein Datenbank-Tool in `data_tools`, um nur dieses abzudecken.

Live getestet: die Codex CLI mit Anmeldung über ChatGPT. Codex Desktop liest dieselbe
`config.toml`; wenn dort etwas nicht funktioniert,
[eröffnen Sie bitte ein Issue](https://github.com/Latet-AI/latet/issues).

Um Latet nicht mehr zu verwenden, führen Sie `latet agents revert codex` aus, oder
`latet global-uninstall` für alle Agenten. Siehe [Deinstallation](../../getting-started/uninstall/).
