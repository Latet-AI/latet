---
title: Hermes Agent, OpenClaw und andere Agenten
description: Autonome Agenten über das Gateway von Latet, darunter Hermes Agent, OpenClaw, Goose und OpenHands, und jeder Agent, der eine OpenAI-kompatible Base-URL akzeptiert.
sidebar:
  label: Autonome Agenten
---

Autonome Agenten arbeiten selbstständig, stundenlang: Sie lesen Postfächer, surfen im Web, fragen
Datenbanken ab, führen Befehle aus und rufen APIs auf, und niemand liest jeden Schritt mit. Jede
Seite, jede Datei und jedes Ergebnis, das sie lesen, geht mit der nächsten Anfrage an das Modell.
Wenn sie auf Latet zeigen, wird jede dieser Anfragen maskiert, bevor sie Ihren Rechner verlässt,
und in den Antworten werden dort die echten Werte wieder eingesetzt.

Sie laufen über das **Gateway** von Latet, wie jeder OpenAI-kompatible Client: Sie setzen die
Base-URL des Agenten auf Latet und behalten den API-Schlüssel Ihres Anbieters wie gewohnt im
Agenten. `latet global-install` ändert die Dateien dieser Agenten nicht: Die Einstellungen unten
sind alles, was es braucht.

| Anbieter | Zu setzende Base-URL | Schlüssel |
| --- | --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` | Ihr API-Schlüssel von OpenAI |
| OpenRouter (Claude, Gemini und Hunderte anderer Modelle) | `http://127.0.0.1:5283/openrouter/api/v1` | Ihr API-Schlüssel von OpenRouter |
| Anthropic, für einen Agenten, der das Format von Anthropic spricht | `http://127.0.0.1:5283/anthropic` | Ihr API-Schlüssel von Anthropic |

Ein Pro- oder Max-Abonnement von Claude funktioniert nur in Claude Code, wie es die Bedingungen von
Anthropic vorsehen, daher nutzen diese Agenten einen API-Schlüssel: Sie erreichen Claude-Modelle
über OpenRouter oder mit einem API-Schlüssel von Anthropic. Der Schlüssel geht über Latet an den
Anbieter und wird nie protokolliert oder im Dashboard angezeigt.

## Hermes Agent

Hermes liest sein Modell und seinen Endpunkt aus `config.yaml` in seinem Ordner: `~/.hermes` unter
macOS und Linux, `%LOCALAPPDATA%\hermes` unter Windows (`HERMES_HOME` legt einen anderen fest).
Richten Sie einen benutzerdefinierten Anbieter auf Latet aus:

```yaml title="config.yaml"
model:
  provider: custom
  base_url: http://127.0.0.1:5283/openrouter/api/v1   # or http://127.0.0.1:5283/openai/v1
  key_env: OPENROUTER_API_KEY                          # the variable that holds your key
  default: anthropic/claude-sonnet-4.5                 # a model your provider lists
```

`hermes model` stellt dasselbe interaktiv ein. `OPENAI_BASE_URL` in der Umgebung genügt nicht:
Hermes beachtet diese Variable nur für seinen Anbieter `openai-api`, daher würde
`latet run hermes` einen benutzerdefinierten Anbieter nicht über Latet leiten.

## OpenClaw

OpenClaw liest `~/.openclaw/openclaw.json` (JSON5) und übernimmt Änderungen daran von selbst. Fügen
Sie einen Anbieter hinzu, der auf Latet zeigt, und machen Sie eines seiner Modelle zum Standard:

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

Für OpenRouter verwenden Sie `http://127.0.0.1:5283/openrouter/api/v1`, Ihren Schlüssel von
OpenRouter und dessen Modellnamen.

## Goose

Der OpenAI-Anbieter von Goose nimmt einen Host und einen Pfad entgegen, die er zusammensetzt:

```sh
export OPENAI_HOST=http://127.0.0.1:5283
export OPENAI_BASE_PATH=openai/v1/chat/completions
export OPENAI_API_KEY=...        # your OpenAI key, as usual
export GOOSE_PROVIDER=openai
export GOOSE_MODEL=gpt-5.1
goose session
```

`GOOSE_PROVIDER` und `GOOSE_MODEL` wählen den Anbieter und das Modell: Ohne sie verlangt Goose, dass
Sie zuerst `goose configure` ausführen, das dieselben Werte speichert. Für OpenRouter fügen Sie mit
`goose configure` einen benutzerdefinierten OpenAI-kompatiblen Anbieter hinzu, mit der Base-URL
`http://127.0.0.1:5283/openrouter/api/v1` und Ihrem OpenRouter-Schlüssel; diese Einrichtung gehört
nicht zu den getesteten.

## OpenHands

Wählen Sie in den LLM-Einstellungen von OpenHands unter **Advanced** ein benutzerdefiniertes Modell
mit dem Präfix seines Anbieters (`openai/gpt-5.1`), setzen Sie die Base-URL auf
`http://127.0.0.1:5283/openai/v1`, und geben Sie Ihren API-Schlüssel ein. Mit der Kommandozeile:

```sh
LLM_MODEL=openai/gpt-5.1 LLM_BASE_URL=http://127.0.0.1:5283/openai/v1 LLM_API_KEY=... \
  openhands --override-with-envs
```

## Jeder andere Agent

Ein Agent, der eine OpenAI-kompatible Base-URL akzeptiert, läuft mit den Adressen aus der Tabelle
oben über Latet. Einer, der `OPENAI_BASE_URL` oder `ANTHROPIC_BASE_URL` aus der Umgebung liest, kann
mit [`latet run`](../openai-compatible/#für-einen-befehl-latet-run) gestartet werden, das sie nur für
ihn setzt.

**Ein Agent in einem Container** erreicht Latet nicht unter `127.0.0.1`: Dort ist `127.0.0.1` der
Container selbst, und Latet beantwortet nur Anfragen, die an `127.0.0.1` oder `localhost` gerichtet
sind. Lassen Sie den Agenten auf Ihrem Rechner laufen; unter Linux teilt auch ein Container, der mit
`--network host` gestartet wurde, die Adresse `127.0.0.1` Ihres Rechners.

## Für einen Agenten, den niemand beobachtet

- **Schalten Sie den [lokalen Prüfer](../../concepts/llm-review/) ein**, wenn Sie einen lokalen
  Modellserver haben. Er liest, was der Agent abruft, den Teil, den niemand eintippt, und
  standardmäßig wartet eine Anfrage auf seine Prüfung und wird gestoppt, wenn der Prüfer nicht
  antworten kann.
- **Nutzen Sie den [strikten Datenmodus](../../concepts/production-data/)** für einen Agenten, der
  Datenbanken oder Logs liest: Jeder Wert einer Tabelle oder eines JSON-Dokuments wird maskiert, und
  Joins funktionieren weiterhin.
- **Beobachten Sie ihn im Dashboard:** `latet dashboard` zeigt jede Anfrage, die der Agent gesendet
  hat, was darin maskiert wurde und was blockiert wurde.
- **Lassen Sie ihn Latet sicher verwalten:** Verweisen Sie ihn auf
  [`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md). Was Schutz
  hinzufügt, wird angewendet; was ihn senkt, wartet im Dashboard auf Ihre Freigabe. Siehe
  [Agenten und Ihre Einstellungen](../../concepts/agents-and-settings/).

Jeder dieser vier Agenten ist echt getestet, eingerichtet wie oben: Er schickt eine Aufgabe über
Latet an einen Testanbieter, der wie OpenAI antwortet; der Anbieter erhält Ersatzwerte, und der
Agent bekommt die echten Werte zurück. Wenn ein Agent über Latet nicht funktioniert,
[eröffnen Sie bitte ein Issue](https://github.com/Latet-AI/latet/issues).
