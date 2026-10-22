---
title: GitHub Copilot, JetBrains, Zed und andere Tools
description: GitHub Copilot, JetBrains Junie und AI Assistant, Zed, Cline, Kilo Code, Continue, Qwen Code, Crush, Android Studio und Claude Desktop über das Gateway von Latet leiten, und welche Tools Latet nicht schützen kann.
sidebar:
  label: Copilot, JetBrains und mehr
---

In vielen KI-Tools können Sie die Adresse des Modells wählen, das sie aufrufen. Setzen Sie diese
Adresse auf das **Gateway** von Latet, dann wird jede Anfrage des Tools maskiert, bevor sie Ihren
Rechner verlässt, und in der Antwort werden dort die echten Werte wieder eingesetzt. Im Tool wird
nichts installiert: Eine Einstellung genügt.

Den API-Schlüssel Ihres Anbieters behalten Sie wie gewohnt im Tool. Latet leitet ihn unverändert an
den Anbieter weiter und schreibt ihn nie in seine Logs oder ins Dashboard.

| Anbieter | Adresse für das Tool |
| --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `http://127.0.0.1:5283/anthropic`, oder `http://127.0.0.1:5283/anthropic/v1` für ein Tool, das nur `/messages` daran anhängt |
| OpenRouter | `http://127.0.0.1:5283/openrouter/api/v1` |

Jeder Abschnitt unten nennt die genaue Adresse, die das Tool braucht. Lässt der eingebaute
OpenRouter-Anbieter eines Tools seine Adresse nicht ändern, erreicht das Tool OpenRouter über einen
OpenAI-kompatiblen Anbieter mit der OpenRouter-Adresse oben.

Junie CLI, Kilo Code, Continue und Crush lesen außerdem Einstellungen aus dem Projekt, das Sie
öffnen, und die eigenen Einstellungen eines Projekts können sie auf eine andere Adresse richten:
Prüfen Sie sie, bevor Sie in einem Repository arbeiten, das Sie nicht selbst geschrieben haben.

## Auf einen Blick

Getestet heißt: Das Tool selbst lief über Latet zu einem Testanbieter, auf jeder API, die es
spricht. Der Anbieter erhielt Ersatzwerte, und das Tool zeigte die echten Werte.

| Tool | Was über Latet läuft | Was nicht | Getestet |
| --- | --- | --- | --- |
| GitHub Copilot in VS Code | Chat und Agenten, mit einem Modell über Custom Endpoint | Inline-Vorschläge und die anderen Funktionen, die den eigenen Dienst von GitHub nutzen | Ja |
| GitHub Copilot CLI | Jede Anfrage, mit einem eigenen Anbieter | Nichts, offline | Ja |
| GitHub Copilot in JetBrains-IDEs und Visual Studio | Chat mit einem eigenen OpenAI-kompatiblen Endpunkt | Inline-Vorschläge | Nein |
| JetBrains Junie CLI | Jede Anfrage, mit einem benutzerdefinierten Modell | Nichts | Ja |
| JetBrains AI Assistant | AI Chat und die Funktionen, denen Sie das Modell zuweisen | Code-Vervollständigung und die Agenten in AI Chat | Nein |
| Zed | Das Agent-Panel und der Inline-Assistent | Edit Predictions | Nein |
| Cline | Jede Anfrage | Nichts | Die CLI |
| Kilo Code | Jede Anfrage, mit einem benutzerdefinierten Anbieter | Nichts | Die CLI |
| Continue | Jede Anfrage eines Modells, dessen `apiBase` Latet ist | Nichts | Die CLI |
| Qwen Code | Jede Anfrage | Eine E-Mail-Adresse, die Sie im Prompt eintippen | Ja |
| Crush | Jede Anfrage | Nichts | Ja |
| Android Studio | Chat und Agent-Modus, mit einem Third-Party Remote Provider | Die eigenen Funktionen von Gemini | Nein |
| Claude Desktop über ein Gateway | Jede Anfrage wird geprüft und gestoppt, wenn ein sensibler Wert darin steht | Die Maskierung, außerhalb des Code-Tabs | Nein |

## GitHub Copilot in VS Code

Mit **Custom Endpoint** schickt VS Code den Copilot Chat an ein Modell unter einer Adresse Ihrer
Wahl. Das funktioniert mit oder ohne Anmeldung bei GitHub, mit oder ohne Copilot-Abonnement; bei
Copilot Business und Enterprise kann ein Administrator es abschalten.

1. Führen Sie **Chat: Manage Language Models** aus, dann **Add Models** → **Custom Endpoint**.
2. Geben Sie einen Namen und den API-Schlüssel Ihres Anbieters ein. VS Code legt den Schlüssel in
   seinem geschützten Speicher für Zugangsdaten ab.
3. Beschreiben Sie jedes Modell in der Datei, die VS Code öffnet: `chatLanguageModels.json`, neben
   Ihrer `settings.json`. Die `url` eines Modells ist die vollständige Adresse des Aufrufs, und
   `apiType` ist die API, die es spricht:

```json title="chatLanguageModels.json"
[
  {
    "name": "Latet",
    "vendor": "customendpoint",
    "models": [
      {
        "id": "gpt-5.1",
        "name": "GPT-5.1 through Latet",
        "url": "http://127.0.0.1:5283/openai/v1/responses",
        "apiType": "responses",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 272000,
        "maxOutputTokens": 128000
      },
      {
        "id": "claude-sonnet-4-5",
        "name": "Claude Sonnet 4.5 through Latet",
        "url": "http://127.0.0.1:5283/anthropic/v1/messages",
        "apiType": "messages",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 200000,
        "maxOutputTokens": 64000
      }
    ]
  }
]
```

Für die Chat Completions API verwenden Sie `http://127.0.0.1:5283/openai/v1/chat/completions` mit
`"apiType": "chat-completions"`. Wählen Sie das Modell in der Modellauswahl des Chats; im
Agent-Modus braucht ein Modell `"toolCalling": true`.

**Was nicht über Latet läuft.** Inline-Vorschläge (Code-Vervollständigungen und Next Edit
Suggestions), die Codesuche und die anderen Funktionen, die auf den Embeddings von GitHub beruhen,
nutzen den eigenen Dienst von GitHub, nicht Ihr Modell: Wenn Sie angemeldet sind, gehen sie ohne
Latet hinaus. Mit Anmeldung gibt Copilot außerdem kurze Aufgaben wie Chat-Titel und
Commit-Nachrichten an die Modelle von GitHub. Damit auch diese bei Ihrem Modell bleiben, setzen Sie
`"chat.byokUtilityModelDefault": "mainAgent"` in Ihren Einstellungen, oder wählen Sie mit
`chat.utilityModel` und `chat.utilitySmallModel` eigene Modelle dafür.

## GitHub Copilot CLI

Die Copilot CLI übernimmt einen eigenen Anbieter aus Umgebungsvariablen:

```sh
export COPILOT_PROVIDER_BASE_URL=http://127.0.0.1:5283/openai/v1
export COPILOT_PROVIDER_TYPE=openai          # Chat Completions
export COPILOT_PROVIDER_API_KEY=...          # your OpenAI key
export COPILOT_MODEL=gpt-5.1
export COPILOT_OFFLINE=true                  # no GitHub sign-in, no telemetry
copilot
```

Mit `COPILOT_PROVIDER_WIRE_API=responses` nutzt sie stattdessen die Responses API. Für Anthropic
setzen Sie `COPILOT_PROVIDER_TYPE=anthropic` und
`COPILOT_PROVIDER_BASE_URL=http://127.0.0.1:5283/anthropic`, mit Ihrem Schlüssel von Anthropic. Das
Modell muss Tool-Aufrufe und Streaming unterstützen. Ohne `COPILOT_OFFLINE=true` sendet die CLI
weiterhin Telemetriedaten an GitHub, und Funktionen wie `/delegate` brauchen eine Anmeldung bei
GitHub; Ihre Prompts gehen nur an Ihren Anbieter.

## GitHub Copilot in JetBrains-IDEs und Visual Studio

Das JetBrains-Plugin von Copilot akzeptiert einen eigenen OpenAI-kompatiblen Endpunkt: Öffnen Sie
**Manage Models** in der Modellauswahl des Chats, fügen Sie den Endpunkt
`http://127.0.0.1:5283/openai/v1` mit Ihrem API-Schlüssel hinzu, und wählen Sie sein Modell im Chat.
Visual Studio 2026 macht dasselbe im Agent-Modus, über seine eigenen Modelleinstellungen.
Inline-Vorschläge nutzen in beiden weiterhin den Dienst von GitHub.

## JetBrains Junie CLI

Die Junie CLI liest ein benutzerdefiniertes Modell aus einer Profildatei in `~/.junie/models/` oder
in `.junie/models/` eines Projekts. Das Feld `baseUrl` ist die vollständige Adresse des Aufrufs:
Junie hängt nichts daran an.

```json title="~/.junie/models/latet.json"
{
  "id": "gpt-5.1",
  "baseUrl": "http://127.0.0.1:5283/openai/v1/responses",
  "apiType": "OpenAIResponses",
  "apiKey": "${OPENAI_API_KEY}"
}
```

Starten Sie Junie mit `junie --model custom:latet`, oder wählen Sie das Modell unter **Custom** in
`/model`. Weitere Werte für `apiType`:

| `apiType` | `baseUrl` |
| --- | --- |
| `OpenAICompletion` (Chat Completions) | `http://127.0.0.1:5283/openai/v1/chat/completions` |
| `Anthropic` | `http://127.0.0.1:5283/anthropic/v1/messages` |

Ein benutzerdefiniertes Modell braucht kein JetBrains-Konto. `JUNIE_SHARE_ANONYMOUS_STATISTICS=false`
schaltet die anonymen Nutzungsstatistiken ab. Junie in der IDE akzeptiert nur Schlüssel von OpenAI
und Anthropic, keine eigene Adresse.

## JetBrains AI Assistant

Fügen Sie unter **Settings | Tools | AI Assistant | Providers & API keys**, im Bereich
**Third-party AI providers**, einen OpenAI-kompatiblen Anbieter mit der URL
`http://127.0.0.1:5283/openai/v1` und Ihrem API-Schlüssel hinzu, und schalten Sie **Tool calling**
ein. Seine Modelle erscheinen in der Modellauswahl von AI Chat; unter **Models Assignment** können
Sie ihnen auch die anderen Funktionen zuweisen, etwa die Commit-Nachrichten.

Die Code-Vervollständigung nutzt kein Modell von Ihnen, und die Agenten in AI Chat, etwa Junie,
Claude Agent und Codex, nehmen die eigenen Schlüssel ihrer Anbieter, keine eigene Adresse: Sie
laufen nicht über Latet.

## Zed

Fügen Sie in der `settings.json` von Zed einen Anbieter hinzu, und wählen Sie dann sein Modell im
Agent-Panel:

```json title="settings.json"
{
  "language_models": {
    "openai_compatible": {
      "Latet": {
        "api_url": "http://127.0.0.1:5283/openai/v1",
        "available_models": [{ "name": "gpt-5.1", "max_tokens": 272000 }]
      }
    },
    "anthropic": { "api_url": "http://127.0.0.1:5283/anthropic" }
  }
}
```

Den Schlüssel des Anbieters `Latet` liest Zed aus den Agent-Einstellungen oder aus der Variablen
`LATET_API_KEY`; der Anthropic-Anbieter behält seinen eigenen Schlüssel. Auch die eingebauten
Anbieter von Zed für OpenAI und OpenRouter akzeptieren eine `api_url`.

**Edit Predictions** kommen standardmäßig vom eigenen Dienst von Zed und laufen nicht über Latet:
Setzen Sie `"edit_predictions": { "provider": "none" }`, um sie abzuschalten.

## Cline

Wählen Sie in den Einstellungen von Cline den Anbieter **OpenAI Compatible** mit der Base-URL
`http://127.0.0.1:5283/openai/v1`, Ihrem API-Schlüssel und einer Modell-ID. Für Anthropic wählen
Sie den Anbieter **Anthropic**, aktivieren **Use custom base URL** und geben
`http://127.0.0.1:5283/anthropic/v1` ein: Cline hängt `/messages` daran an.

Die Cline CLI übernimmt den OpenAI-kompatiblen Anbieter von der Kommandozeile:

```sh
cline auth -p openai-compatible -k "$OPENAI_API_KEY" -m gpt-5.1 -b http://127.0.0.1:5283/openai/v1
```

Für ihren Anthropic-Anbieter setzen Sie `baseUrl` in `~/.cline/data/settings/providers.json`:
`cline auth` akzeptiert eine Base-URL nur für OpenAI-kompatible Anbieter.

## Kilo Code

Fügen Sie in den Einstellungen der Erweiterung Kilo Code einen **benutzerdefinierten Anbieter**
hinzu, mit der API, die er spricht (OpenAI Compatible, OpenAI Responses oder Anthropic Messages),
und der Base-URL unten. Die CLI `kilo` liest denselben Anbieter aus `kilo.json`, in
`~/.config/kilo/` oder in einem Projekt:

```json title="kilo.json"
{
  "provider": {
    "latet": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Latet",
      "options": { "baseURL": "http://127.0.0.1:5283/openai/v1", "apiKey": "{env:OPENAI_API_KEY}" },
      "models": { "gpt-5.1": { "name": "GPT-5.1 through Latet" } }
    }
  },
  "model": "latet/gpt-5.1",
  "disabled_providers": ["kilo"]
}
```

`disabled_providers` schaltet den eigenen Anbieter von Kilo ab, dessen Anfragen ohne Latet an den
Dienst von Kilo gehen.
Für Anthropic verwenden Sie `"npm": "@ai-sdk/anthropic"` mit der Base-URL
`http://127.0.0.1:5283/anthropic/v1`, für die Responses API `"npm": "@ai-sdk/openai"` mit
`http://127.0.0.1:5283/openai/v1`.

## Continue

Die Erweiterungen von Continue für VS Code und JetBrains und seine CLI `cn` lesen dieselbe
`~/.continue/config.yaml`. Geben Sie jedem Modell die Adresse von Latet in `apiBase`:

```yaml title="~/.continue/config.yaml"
name: Latet
version: 1.0.0
schema: v1
models:
  - name: GPT-5.1 through Latet
    provider: openai
    model: gpt-5.1
    apiBase: http://127.0.0.1:5283/openai/v1
    apiKey: ${{ secrets.OPENAI_API_KEY }}
  - name: Claude through Latet
    provider: anthropic
    model: claude-sonnet-4-5
    apiBase: http://127.0.0.1:5283/anthropic/v1
    apiKey: ${{ secrets.ANTHROPIC_API_KEY }}
```

Die `apiBase` für Anthropic endet auf `/v1`: Continue hängt `/messages` daran an.

## Qwen Code

Qwen Code liest seine Adresse aus denselben Variablen wie die offiziellen SDKs, daher leitet
`latet run` es über Latet:

```sh
latet run qwen
```

Mit seinem OpenAI-kompatiblen Anbieter nutzt es `OPENAI_BASE_URL`, mit seinem Anthropic-Anbieter
`ANTHROPIC_BASE_URL`. Wählen Sie den Anbieter in `~/.qwen/settings.json`
(`"security": { "auth": { "selectedType": "openai" } }` oder `"anthropic"`), und setzen Sie Schlüssel
und Modell in `OPENAI_API_KEY` und `OPENAI_MODEL` oder in `ANTHROPIC_API_KEY` und
`ANTHROPIC_MODEL`. Ohne `latet run` setzen Sie die Base-URLs selbst:
`http://127.0.0.1:5283/openai/v1` und `http://127.0.0.1:5283/anthropic`.

**Eine E-Mail-Adresse, die Sie eintippen.** Qwen Code liest jedes `@` in einem Prompt, den Sie
eintippen, als Datei, die angehängt werden soll, und sendet `mario.rossi@acme.it` als
`mario.rossi @acme.it`. Latet erkennt das nicht als Adresse, daher geht sie im Klartext hinaus.
Adressen in den Dateien, Befehlsausgaben und Seiten, die Qwen Code liest, bleiben unverändert, und
Latet maskiert sie.

## Crush

Fügen Sie der `crush.json` von Crush einen Anbieter hinzu, in einem Projekt oder in Ihrem
Einstellungsordner von Crush:

```json title="crush.json"
{
  "providers": {
    "latet": {
      "type": "openai-compat",
      "base_url": "http://127.0.0.1:5283/openai/v1",
      "api_key": "$OPENAI_API_KEY",
      "models": [{ "id": "gpt-5.1", "name": "GPT-5.1 through Latet", "context_window": 272000, "default_max_tokens": 16000 }]
    }
  }
}
```

Wählen Sie diesen Anbieter für beide Modelle von Crush, damit keine Anfrage, auch nicht die kurzen,
die eine Sitzung benennen, an einen Anbieter geht, den Crush selbst aus Ihrer Umgebung eingerichtet
hat:

```json title="crush.json"
"models": {
  "large": { "model": "gpt-5.1", "provider": "latet" },
  "small": { "model": "gpt-5.1", "provider": "latet" }
}
```

Für Anthropic verwenden Sie `"type": "anthropic"` mit `http://127.0.0.1:5283/anthropic`. Der Typ
`openai` von Crush spricht die Responses API, und der Typ `openrouter` akzeptiert keine Base-URL:
Erreichen Sie OpenRouter mit `openai-compat` und `http://127.0.0.1:5283/openrouter/api/v1`.

## Android Studio

Fügen Sie unter **Settings → Tools → AI → Model Providers** einen **Third-Party Remote Provider**
hinzu, mit der Adresse von Latet statt der des Anbieters und mit Ihrem API-Schlüssel. Aktualisieren
Sie dann die Modellliste, und wählen Sie die Modelle: `http://127.0.0.1:5283/openai/v1` für OpenAI,
`http://127.0.0.1:5283/anthropic` für Anthropic, `http://127.0.0.1:5283/openrouter/api/v1` für
OpenRouter. Chat und Agent-Modus nutzen das gewählte Modell; die eigenen Funktionen von Gemini
nutzen weiterhin den Dienst von Google.

## Claude Desktop über ein Gateway

Claude Desktop kann statt über den Dienst von Anthropic über ein Inferenz-Gateway laufen, ohne
Anmeldung bei Claude: So leitet eine Organisation es zu ihrem eigenen Anbieter. Ein Pro- oder
Max-Abonnement gibt es dann nicht: Die Anfragen werden nach Verbrauch abgerechnet, über einen
API-Schlüssel von Anthropic oder über den Anbieter Ihres Unternehmens. Auf einem einzelnen Rechner
schalten Sie ohne Anmeldung
**Help → Troubleshooting → Enable Developer Mode** ein und öffnen dann
**Developer → Configure Third-Party Inference**; eine Organisation setzt dieselben Einstellungen über
ein Profil ihrer Geräteverwaltung. Richten Sie das Gateway auf Latet aus:

| Einstellung | Wert |
| --- | --- |
| `inferenceProvider` | `gateway` |
| `inferenceGatewayBaseUrl` | `http://127.0.0.1:5283/anthropic` |
| `inferenceGatewayApiKey` | Ihr API-Schlüssel von Anthropic |
| `inferenceGatewayAuthScheme` | `x-api-key` |

Für eine Adresse auf diesem Rechner akzeptiert Claude Desktop `http://`. Chat, Cowork und Code
senden dann jede Anfrage über Latet, und auch die Modellliste kommt über Latet von Anthropic.

**Was Latet damit macht.** Die Engine von Claude Code erstellt diese Anfragen, und Latet schreibt
eine Anfrage von Claude Code nie um: Es prüft jede und stoppt eine, die einen geheimen Wert oder
personenbezogene Daten im Klartext enthalten würde, mit einer Meldung statt einer Antwort. Anders
als bei den anderen Tools auf dieser Seite maskiert es sie nicht: Der
[Leitfaden für Gateways](https://code.claude.com/docs/en/llm-gateway-protocol) von Anthropic
verlangt von einem Gateway, die Anfragen von Claude Code zu untersuchen, nicht sie zu verändern. Im
Code-Tab maskiert das Plugin von Latet die Inhalte, bevor die Anfrage erstellt wird, sodass der
Code-Tab beides erhält: die Maskierung und die Prüfung.

Mit Anmeldung bei Claude nutzt Claude Desktop kein Gateway: Chat und Cowork sind nicht abgedeckt,
und im Code-Tab maskiert das Plugin ohne eine Prüfung dahinter. Siehe
[Claude Code](../claude-code/#abdeckung).

## Was Latet nicht schützen kann

Latet läuft auf Ihrem Rechner, zwischen dem Tool und dem Modell. Die folgenden Tools senden ihre
Anfragen von den eigenen Servern des Herstellers oder rufen einen Dienst auf, den keine Einstellung
ändern kann, daher laufen ihre Anfragen nie über Latet:

| Tool | Grund |
| --- | --- |
| Cursor und seine CLI | Cursor erstellt und sendet jede Anfrage an das Modell von seinen eigenen Servern, auch mit Ihrem eigenen API-Schlüssel. |
| Windsurf und Devin Desktop | Die Anfragen gehen an die Server des Herstellers, die das Modell aufrufen. |
| Google Antigravity (die IDE) | Die Anfragen gehen an den Dienst von Google, der das Modell aufruft. |
| Jules | Es läuft in der Cloud von Google, nicht auf Ihrem Rechner. |
| Kiro und Amazon Q Developer | Die Anfragen gehen an den Dienst von Amazon, der das Modell aufruft. |
| Warp | Die Anfragen gehen an die Server von Warp, die das Modell aufrufen. |
| Die Desktop-App von ChatGPT | Sie spricht nur mit dem Dienst von OpenAI; eine Einstellung für eine andere Adresse hat sie nicht. |
| Claude Desktop mit Anmeldung bei Claude, und Cloud-Sitzungen | Chat und Cowork rufen den Dienst von Anthropic direkt auf, und Cloud-Sitzungen laufen auf der Infrastruktur von Anthropic. |

Wenn ein Tool, das Sie nutzen, nicht auf dieser Seite steht, suchen Sie nach einer Einstellung wie
Base-URL, API Base, Endpunkt oder benutzerdefinierter Anbieter: Hat es eine, läuft es wie die Tools
oben über Latet. Wenn es nicht funktioniert,
[eröffnen Sie bitte ein Issue](https://github.com/Latet-AI/latet/issues).
