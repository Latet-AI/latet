---
title: Gemini CLI und Google GenAI
description: Gemini CLI, die Google-GenAI-SDKs und Gemini auf Vertex AI über das Latet-Gateway leiten.
---

Gemini CLI und die Programme, die auf Googles GenAI-SDKs aufbauen, nutzen den **Gateway-Modus**: Der
lokale Daemon maskiert die Anfragen und stellt die Antworten wieder her. Er deckt die Gemini API mit
einem API-Schlüssel ab, und Gemini auf Vertex AI, mit einem Google-Cloud-Projekt oder im
Express-Modus mit einem API-Schlüssel.

## Basis-URLs

| Dienst | Statt | Verwenden Sie | Variable |
| --- | --- | --- | --- |
| Gemini API | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` | `GOOGLE_GEMINI_BASE_URL` |
| Vertex AI | `https://<location>-aiplatform.googleapis.com` | `http://127.0.0.1:5283/vertex` | `GOOGLE_VERTEX_BASE_URL` |

Ihr API-Schlüssel oder Ihre Google-Cloud-Anmeldung bleiben, wo sie sind. Das Gateway leitet sie
unverändert weiter (einen Schlüssel im Header `x-goog-api-key` oder im Parameter `key` der Adresse,
ein Vertex-Token in `Authorization`) und schreibt ihren Wert nie in Logs, ins Dashboard oder in einen
Mitschnitt. Auf Vertex geht jede Anfrage an die Adresse der Location, die sie nennt.

## Gemini CLI

Leiten Sie Gemini CLI mit dem Wrapper über Latet. Er startet bei Bedarf den Daemon, setzt beide
Basis-URLs nur für diesen Lauf und gibt jedes Argument an `gemini` weiter:

```sh
latet gemini
latet gemini -p "write the release notes for the payments module"
```

`latet run gemini` tut dasselbe. Gemini CLI übernimmt seine Adresse nur aus der Umgebung, darum ist
es im globalen Modus nicht verfügbar: `latet global-install` richtet es nicht ein, und es gibt kein
`latet agents apply gemini`. Eine dauerhafte Änderung würde ein Projekt mit einer eigenen
`.env`-Datei nicht erreichen, und dort spräche Gemini CLI direkt mit Google, ohne es zu sagen.

### Einen API-Schlüssel oder Vertex AI wählen

In Gemini CLI wählt `/auth`, wie es sein Modell erreicht, und die Wahl bleibt in
`~/.gemini/settings.json`:

```json title="~/.gemini/settings.json"
{ "security": { "auth": { "selectedType": "gemini-api-key" } } }
```

- **Ein API-Schlüssel** (`gemini-api-key`): `GEMINI_API_KEY` enthält ihn.
- **Vertex AI** (`vertex-ai`): `GOOGLE_GENAI_USE_VERTEXAI=true`, mit `GOOGLE_CLOUD_PROJECT` und
  `GOOGLE_CLOUD_LOCATION` und Ihrer Google-Cloud-Anmeldung, oder nur `GOOGLE_API_KEY` im
  Express-Modus.

Ist noch nichts gewählt, bietet `latet gemini` im Dialog von Gemini CLI den API-Schlüssel an, oder
Vertex AI, wenn `GOOGLE_GENAI_USE_VERTEXAI=true` gesetzt ist. Ein Lauf mit `-p` braucht die Wahl
vorher: Setzen Sie `selectedType` wie oben.

**Mit der Google-Anmeldung**, ob Ihre Einstellungen sie wählen, die des Projekts
(`.gemini/settings.json` im Ordner, in dem Sie es starten) oder die des Rechners, schickt Gemini CLI
seine Anfragen an einen Endpunkt, den Latet nicht liest, es würde also nichts maskiert. Dann startet
`latet gemini` es nicht und sagt, wo die Wahl getroffen ist und was stattdessen zu wählen ist.

### Nutzungsstatistiken

Gemini CLI schickt seine Nutzungsstatistiken direkt an Google, nicht über Latet. Um sie
abzuschalten:

```json title="~/.gemini/settings.json"
{ "privacy": { "usageStatisticsEnabled": false } }
```

## Skripte und die Google-GenAI-SDKs

`latet run` setzt `GOOGLE_GEMINI_BASE_URL` und `GOOGLE_VERTEX_BASE_URL` neben den Variablen für
OpenAI und Anthropic, und `latet env` gibt sie für eine ganze Shell aus. Googles SDKs für Python
(`google-genai`) und JavaScript (`@google/genai`) lesen sie:

```python title="welcome.py"
from google import genai

client = genai.Client()  # reads GEMINI_API_KEY and GOOGLE_GEMINI_BASE_URL
reply = client.models.generate_content(
    model="gemini-3-pro",
    contents="Write a short welcome email to emily.carter@northwind-legal.com",
)
print(reply.text)
```

Starten Sie es mit `latet run python welcome.py`: Google liest eine falsche Adresse derselben Form, und
das Skript gibt die E-Mail mit der echten aus. Gestreamte Antworten funktionieren genauso, Stück für
Stück.

Auch Googles OpenAI-kompatibler Endpunkt läuft über Latet: Geben Sie einem OpenAI-Client die Basis-URL
`http://127.0.0.1:5283/gemini/v1beta/openai`.

## Was das Gateway liest

- **Generieren**, gestreamt oder nicht (`generateContent`, `streamGenerateContent`), **Token zählen**
  (`countTokens`), **Embeddings** (`embedContent`, `batchEmbedContents`) und **einen Cache anlegen**
  (`cachedContents`): Die Systemanweisung, jede Nachricht, Funktionsaufrufe und ihre Ergebnisse, die
  Deklarationen der Funktionen und das Schema der Antwort werden maskiert, und die Antworten
  wiederhergestellt.
- **Bilder** in der Anfrage werden mit der optionalen [Texterkennung](../../concepts/images/) gelesen
  und maskiert. Andere Dateien, und Dateien, die über ihre Adresse angegeben sind, werden so
  gesendet, wie sie sind, und im Dashboard als *Nicht gelesen* markiert.
- Das **Nachdenken** des Modells, und der Code, den Google für es ausführt, bleiben, wie das Modell
  sie geschrieben hat.
- **Jede andere Anfrage mit einem Body** auf diesen Routen wird abgelehnt, mit einem Fehler, der es
  sagt, und ein WebSocket der Live API wird geschlossen, bevor etwas durchgeht: Die Interactions
  API, die Live API, die Batch API, das Hochladen von Dateien und `:predict` von Vertex (seine
  eigenen Embeddings und Bildmodelle) tragen eine Unterhaltung in Formen, die Latet noch nicht liest
  (sie stehen auf der [Roadmap](../../roadmap/)). Eine Anfrage ohne Body, etwa die Liste der
  Modelle, geht durch.

## Grenzen

- **Gemini CLI mit der Google-Anmeldung** ist nicht abgedeckt (siehe oben).
- **Antigravity CLI** nimmt mit einem API-Schlüssel eine Basis-URL an (`GOOGLE_GEMINI_BASE_URL`),
  aber seine Dokumentation sagt nicht, welches Protokoll von Google es dann spricht, und es ist mit
  Latet nicht geprüft. Nutzt es die Interactions API, lehnt Latet seine Anfragen ab, statt sie
  unmaskiert zu senden.
- Eine Antwort, die einen **expliziten Cache** nutzt, bekommt die Originale der Werte zurück, die
  ihre eigene Unterhaltung trägt.
