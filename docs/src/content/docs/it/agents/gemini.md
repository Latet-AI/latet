---
title: Gemini CLI e Google GenAI
description: Fai passare dal gateway di Latet Gemini CLI, gli SDK Google GenAI e Gemini su Vertex AI.
---

Gemini CLI e i programmi costruiti sugli SDK GenAI di Google usano la **modalità gateway**: il
daemon locale maschera le richieste e ripristina le risposte. Copre la Gemini API con una chiave
API, e Gemini su Vertex AI, con un progetto Google Cloud o in modalità express con una chiave API.

## Base URL

| Servizio | Invece di | Usa | Variabile |
| --- | --- | --- | --- |
| Gemini API | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` | `GOOGLE_GEMINI_BASE_URL` |
| Vertex AI | `https://<location>-aiplatform.googleapis.com` | `http://127.0.0.1:5283/vertex` | `GOOGLE_VERTEX_BASE_URL` |

La tua chiave API o il tuo accesso a Google Cloud restano dove sono. Il gateway li inoltra senza
modifiche (una chiave nell'header `x-goog-api-key` o nel parametro `key` dell'indirizzo, un token di
Vertex in `Authorization`) e non scrive mai il loro valore nei log, nella dashboard o in una
cattura. Su Vertex, ogni richiesta va all'indirizzo della location che nomina.

## Gemini CLI

Fai passare Gemini CLI da Latet con il wrapper. Avvia il daemon se serve, imposta i due base URL
solo per quell'esecuzione e passa ogni argomento a `gemini`:

```sh
latet gemini
latet gemini -p "write the release notes for the payments module"
```

`latet run gemini` fa lo stesso. Gemini CLI prende il suo indirizzo solo dall'ambiente, quindi non
è disponibile in modalità globale: `latet global-install` non lo configura, e non c'è un
`latet agents apply gemini`. Una modifica permanente non arriverebbe a un progetto con un suo file
`.env`, e lì Gemini CLI parlerebbe con Google direttamente senza dirlo.

### Scegli una chiave API o Vertex AI

In Gemini CLI, `/auth` sceglie come raggiunge il suo modello, e la scelta resta in
`~/.gemini/settings.json`:

```json title="~/.gemini/settings.json"
{ "security": { "auth": { "selectedType": "gemini-api-key" } } }
```

- **Una chiave API** (`gemini-api-key`): la contiene `GEMINI_API_KEY`.
- **Vertex AI** (`vertex-ai`): `GOOGLE_GENAI_USE_VERTEXAI=true`, con `GOOGLE_CLOUD_PROJECT` e
  `GOOGLE_CLOUD_LOCATION` e il tuo accesso a Google Cloud, oppure solo `GOOGLE_API_KEY` in modalità
  express.

Se non è ancora scelto nulla, `latet gemini` propone la chiave API nella finestra di Gemini CLI, o
Vertex AI quando è impostato `GOOGLE_GENAI_USE_VERTEXAI=true`. Un avvio con `-p` ha bisogno che la
scelta sia già fatta: imposta `selectedType` come sopra.

**Con l'accesso Google**, che lo scelgano le tue impostazioni, quelle del progetto
(`.gemini/settings.json` nella cartella in cui lo avvii) o quelle del computer, Gemini CLI manda le
sue richieste a un endpoint che Latet non legge, quindi non verrebbe mascherato nulla. In quel caso
`latet gemini` non lo avvia, e dice dove è fatta la scelta e cosa scegliere al suo posto.

### Statistiche d'uso

Gemini CLI manda le sue statistiche d'uso direttamente a Google, non tramite Latet. Per
disattivarle:

```json title="~/.gemini/settings.json"
{ "privacy": { "usageStatisticsEnabled": false } }
```

## Script e SDK Google GenAI

`latet run` imposta `GOOGLE_GEMINI_BASE_URL` e `GOOGLE_VERTEX_BASE_URL` accanto alle variabili di
OpenAI e Anthropic, e `latet env` le stampa per un'intera shell. Gli SDK di Google per Python
(`google-genai`) e JavaScript (`@google/genai`) le leggono:

```python title="welcome.py"
from google import genai

client = genai.Client()  # reads GEMINI_API_KEY and GOOGLE_GEMINI_BASE_URL
reply = client.models.generate_content(
    model="gemini-3-pro",
    contents="Write a short welcome email to emily.carter@northwind-legal.com",
)
print(reply.text)
```

Eseguilo con `latet run python welcome.py`: Google legge un indirizzo finto della stessa forma, e lo
script stampa l'email con quello vero. Le risposte in streaming funzionano allo stesso modo, pezzo
per pezzo.

Anche l'endpoint di Google compatibile con OpenAI passa da Latet: dai a un client OpenAI il base URL
`http://127.0.0.1:5283/gemini/v1beta/openai`.

## Cosa legge il gateway

- **Generazione**, in streaming o no (`generateContent`, `streamGenerateContent`), **conteggio dei
  token** (`countTokens`), **embedding** (`embedContent`, `batchEmbedContents`) e **creazione di una
  cache** (`cachedContents`): l'istruzione di sistema, ogni messaggio, le chiamate di funzione e i
  loro risultati, le dichiarazioni delle funzioni e lo schema della risposta vengono mascherati, e
  le risposte ripristinate.
- Le **immagini** inviate nella richiesta vengono lette e mascherate con il
  [riconoscimento del testo](../../concepts/images/) facoltativo. Gli altri file, e i file indicati
  con il loro indirizzo, vengono inviati così come sono, con la segnalazione *Non ispezionato* nella
  dashboard.
- Il **ragionamento** del modello, e il codice che Google esegue per lui, restano come li ha scritti
  il modello.
- **Ogni altra richiesta con un corpo** su queste route viene rifiutata, con un errore che lo dice,
  e un WebSocket della Live API viene chiuso prima che passi qualcosa: l'Interactions API, la Live
  API, la Batch API, il caricamento di file e il `:predict` di Vertex (i suoi embedding e i suoi
  modelli di immagini) portano una conversazione in forme che Latet non legge ancora (sono nella
  [roadmap](../../roadmap/)). Una richiesta senza corpo, come l'elenco dei modelli, passa.

## Limiti

- **Gemini CLI con l'accesso Google** non è coperto (vedi sopra).
- **Antigravity CLI** accetta un base URL con una chiave API (`GOOGLE_GEMINI_BASE_URL`), ma la sua
  documentazione non dice quale protocollo di Google parli poi, e non è verificato con Latet. Se usa
  l'Interactions API, Latet rifiuta le sue richieste invece di inviarle senza mascherarle.
- Una risposta che usa una **cache esplicita** riceve gli originali dei valori che porta la sua
  stessa conversazione.
