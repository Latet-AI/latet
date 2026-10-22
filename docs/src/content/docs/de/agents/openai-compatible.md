---
title: OpenAI-kompatibel und OpenRouter
description: Latet mit jedem Client nutzen, in dem Sie die Base-URL von OpenAI oder Anthropic ändern können, und mit OpenRouter.
---

Jeder Client, in dem Sie die Base-URL für OpenAI oder Anthropic ändern können, kann den
**Gateway-Modus** nutzen: Ihre eigenen Skripte, die offiziellen SDKs und die Tools, die darauf
aufbauen.

## Base-URLs

| Anbieter | Statt | Verwenden Sie |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `https://api.anthropic.com` | `http://127.0.0.1:5283/anthropic` |
| OpenRouter | `https://openrouter.ai/api/v1` | `http://127.0.0.1:5283/openrouter/api/v1` |
| Google Gemini | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` |

Behalten Sie Ihren API-Schlüssel wie gewohnt im Client. Das Gateway leitet ihn unverändert weiter
und schreibt seinen Wert nie in Logs oder ins Dashboard.

## Für einen Befehl: `latet run`

`latet run` startet bei Bedarf den Daemon und führt einen beliebigen Befehl mit gesetzten Base-URLs
aus:

```sh
latet run python welcome.py
```

Der Befehl sieht `OPENAI_BASE_URL` und `OPENAI_API_BASE` auf `http://127.0.0.1:5283/openai/v1`
gesetzt, `ANTHROPIC_BASE_URL` auf `http://127.0.0.1:5283/anthropic`, und `GOOGLE_GEMINI_BASE_URL` und
`GOOGLE_VERTEX_BASE_URL` auf `http://127.0.0.1:5283/gemini` und `http://127.0.0.1:5283/vertex`. Die
offiziellen SDKs von OpenAI, Anthropic und Google GenAI lesen diese Variablen: zu denen von Google
siehe [Gemini CLI und Google GenAI](../gemini/).

## Für eine ganze Shell: `latet env`

`latet env` gibt dieselben Variablen in der Syntax Ihrer Shell aus. Um sie auf die aktuelle Sitzung
anzuwenden:

```sh
eval "$(latet env)"              # macOS and Linux
```

```powershell
latet env | Invoke-Expression    # Windows PowerShell
```

`--shell bash`, `--shell powershell` oder `--shell cmd` wählt die Syntax; standardmäßig richtet sie
sich nach Ihrem System. Damit die Variablen erhalten bleiben, fügen Sie die ausgegebenen Zeilen
Ihrem Shell-Profil hinzu. Claude Code, das in einer solchen Shell mit `claude` gestartet wird,
erreicht Latet dann ohne sein Plugin: Seine Anfragen werden nur geprüft, und jede, die einen
sensiblen Wert im Klartext enthält, wird gestoppt. Starten Sie es mit `latet claude` oder richten
Sie es mit `latet global-install` ein; beides gibt ihm auch das Plugin.

## Beispiel

Ein Skript, das ein Modell bittet, eine E-Mail zu entwerfen:

```python title="welcome.py"
from openai import OpenAI

client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL
reply = client.chat.completions.create(
    model="your-model",
    messages=[{"role": "user", "content": "Write a short welcome email to emily.carter@northwind-legal.com"}],
)
print(reply.choices[0].message.content)
```

Führen Sie es mit `latet run python welcome.py` aus:

1. Der Daemon ersetzt die Adresse, bevor die Anfrage hinausgeht. Der Anbieter liest *"Write a short
   welcome email to laura.bennett@contoso-4f1a.example"*.
2. Das Modell schreibt eine E-Mail an `laura.bennett@contoso-4f1a.example`.
3. Auf dem Rückweg setzt der Daemon das Original ein, sodass das Skript eine E-Mail an
   `emily.carter@northwind-legal.com` ausgibt.

Nur der Ersatzwert selbst wird wieder eingesetzt. Wenn das Modell auch *Dear Laura* geschrieben hat,
einen Namen, den es aus der Ersatzadresse erraten hat, bleibt dieses Wort so, wie das Modell es
geschrieben hat.

Gestreamte Antworten funktionieren genauso, Stück für Stück.

## Agent-Frameworks

LangChain, LangGraph, AutoGen und CrewAI erreichen das Modell über die offiziellen SDKs oder über
LiteLLM, die die Variablen lesen, die `latet run` setzt: Starten Sie Ihr Programm mit `latet run`,
wie jedes Skript. Jedes davon ist echt über Latet getestet, eingerichtet wie unten:

| Framework | Einrichtung | Getestet |
| --- | --- | --- |
| LangChain und LangGraph | `ChatOpenAI`, ohne Base-URL (`ChatAnthropic` liest `ANTHROPIC_BASE_URL` auf dieselbe Weise) | Ein Chatmodell und ein Graph darauf |
| AutoGen | `OpenAIChatCompletionClient`, ohne Base-URL | Ein `AssistantAgent` |
| CrewAI | `LLM(model="openai/gpt-5.1")`, ohne Base-URL | Sein `LLM` und eine Crew mit einer Aufgabe |

Wenn Sie die Adresse lieber in Ihrem Code setzen, geben Sie sie als `base_url` an:
`http://127.0.0.1:5283/openai/v1` für die Clients von OpenAI, `http://127.0.0.1:5283/anthropic` für
die von Anthropic.

Aider erreicht das Modell über LiteLLM, das `OPENAI_API_BASE` liest: Starten Sie es mit
`latet run aider --model openai/gpt-5.1`, oder tragen Sie
`openai-api-base: http://127.0.0.1:5283/openai/v1` in seine `.aider.conf.yml` ein. Es ist auf
dieselbe Weise echt getestet.

**Was Latet nicht sieht.** Latet sitzt zwischen Ihrem Programm und dem Modell, und nirgendwo sonst.
Ein Tool, das Ihr Agent selbst aufruft, etwa eine Websuche-API, eine Datenbank oder ein
Vektorspeicher in der Cloud, bekommt die echten Werte: Das Modell fordert es mit Ersatzwerten an,
und Latet setzt die Originale in dessen Antwort wieder ein, bevor Ihr Programm das Tool ausführt.
Lassen Sie solche Tools auf Ihrem Rechner laufen, oder betrachten Sie das, was sie erhalten, als im
Klartext gesendet.

## Was das Gateway liest

- **Chat Completions** (`/chat/completions` und das ältere `/completions`), die **Responses API**
  (`/responses`) und **Anthropic Messages** (`/v1/messages`, auch das Zählen von Tokens): Der
  System-Prompt, jede Nachricht, Tool-Aufrufe und Tool-Ergebnisse werden maskiert, und in den
  Antworten werden die Originale wieder eingesetzt, gestreamt oder nicht.
- **Embeddings** (`/embeddings`): Die Eingabe wird maskiert.
- **Bilder**, die als `data:`-URLs gesendet werden, werden gelesen und maskiert, mit der optionalen
  [Texterkennung](../../concepts/images/). Bilder, die über ihre Adresse angegeben sind, Audio und
  Dateien werden so gesendet, wie sie sind, und im Dashboard als *Nicht gelesen* markiert.
- **Jede andere Anfrage** an OpenAI, Anthropic oder OpenRouter, etwa das Auflisten der Modelle, geht
  unverändert durch. Auf den Routen von Google wird eine Anfrage mit einem Body, den Latet nicht
  liest, stattdessen abgelehnt: Was Latet dort liest, steht unter
  [Gemini CLI und Google GenAI](../gemini/#was-das-gateway-liest).

Eine Anfrage an Anthropic, die von Claude Code kommt oder das Token eines Claude-Abonnements
enthält, wird nie umgeschrieben: Sie wird nur geprüft und blockiert, wenn ein sensibler Wert darin
steht. Siehe [Claude Code](../claude-code/).

## Gut zu wissen

- Das Gateway lauscht nur auf `127.0.0.1` und lehnt Anfragen ab, die von einer Webseite kommen.
- Ein Pfad außerhalb der Routen oben erhält einen Fehler `404`: `no upstream for this path`.
- **Ein anderer OpenAI-kompatibler Anbieter** (DeepSeek, Mistral, ein Modellserver in Ihrem
  Netzwerk): Fügen Sie ihn als Ziel hinzu, und verwenden Sie dann seine Route als Base-URL des
  Agenten, hier `http://127.0.0.1:5283/deepseek/v1`:

  ```sh
  latet config add upstreams "{name: deepseek, url: https://api.deepseek.com}"
  ```

  Siehe [`upstreams`](../../reference/configuration/#upstreams-items).
- Autonome Agenten wie Hermes Agent und OpenClaw akzeptieren diese Base-URLs in ihren eigenen
  Einstellungen: siehe [Hermes Agent, OpenClaw und andere Agenten](../autonomous-agents/).
