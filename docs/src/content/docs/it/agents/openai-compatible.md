---
title: Compatibili con OpenAI e OpenRouter
description: Usa Latet con qualsiasi client che ti permetta di cambiare l'URL di base di OpenAI o di Anthropic, e con OpenRouter.
---

Qualsiasi client che ti permetta di cambiare il suo URL di base di OpenAI o di Anthropic può usare la
**modalità gateway**: i tuoi script, gli SDK ufficiali e gli strumenti costruiti su di essi.

## URL di base

| Provider | Invece di | Usa |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `https://api.anthropic.com` | `http://127.0.0.1:5283/anthropic` |
| OpenRouter | `https://openrouter.ai/api/v1` | `http://127.0.0.1:5283/openrouter/api/v1` |
| Google Gemini | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` |

Tieni la tua chiave API nel client come al solito. Il gateway la inoltra senza modifiche e non scrive
mai il suo valore nei log o nella dashboard.

## Per un comando: `latet run`

`latet run` avvia il daemon se serve ed esegue qualsiasi comando con gli URL di base impostati:

```sh
latet run python welcome.py
```

Il comando vede `OPENAI_BASE_URL` e `OPENAI_API_BASE` impostati su `http://127.0.0.1:5283/openai/v1`,
`ANTHROPIC_BASE_URL` impostato su `http://127.0.0.1:5283/anthropic`, e `GOOGLE_GEMINI_BASE_URL` e
`GOOGLE_VERTEX_BASE_URL` impostati su `http://127.0.0.1:5283/gemini` e `http://127.0.0.1:5283/vertex`.
Gli SDK ufficiali di OpenAI, di Anthropic e Google GenAI leggono queste variabili: per quelli di
Google vedi [Gemini CLI e Google GenAI](../gemini/).

## Per un'intera shell: `latet env`

`latet env` stampa le stesse variabili nella sintassi della tua shell. Per applicarle alla sessione
corrente:

```sh
eval "$(latet env)"              # macOS and Linux
```

```powershell
latet env | Invoke-Expression    # Windows PowerShell
```

`--shell bash`, `--shell powershell` o `--shell cmd` sceglie la sintassi; quella predefinita segue il
tuo sistema. Per mantenere le variabili, aggiungi le righe stampate al profilo della tua shell. Claude
Code avviato con `claude` in una shell del genere arriva a Latet senza il suo plugin: le sue richieste
vengono solo controllate, e ognuna che contiene un valore sensibile in chiaro viene fermata. Avvialo
con `latet claude`, oppure configuralo con `latet global-install`, che gli danno anche il plugin.

## Esempio

Uno script che chiede a un modello di scrivere la bozza di un'email:

```python title="welcome.py"
from openai import OpenAI

client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL
reply = client.chat.completions.create(
    model="your-model",
    messages=[{"role": "user", "content": "Write a short welcome email to emily.carter@northwind-legal.com"}],
)
print(reply.choices[0].message.content)
```

Eseguilo con `latet run python welcome.py`:

1. Il daemon sostituisce l'indirizzo prima che la richiesta parta. Il provider legge *"Write a short
   welcome email to laura.bennett@contoso-4f1a.example"*.
2. Il modello scrive un'email a `laura.bennett@contoso-4f1a.example`.
3. Al ritorno il daemon rimette l'originale, così lo script stampa un'email indirizzata a
   `emily.carter@northwind-legal.com`.

Viene ripristinato solo il valore fittizio in sé. Se il modello ha scritto anche *Dear Laura*, un nome
che ha dedotto dall'indirizzo fittizio, quella parola resta come l'ha scritta il modello.

Le risposte in streaming funzionano allo stesso modo, un pezzo alla volta.

## Framework per agenti

LangChain, LangGraph, AutoGen e CrewAI raggiungono il modello attraverso gli SDK ufficiali o
LiteLLM, che leggono le variabili impostate da `latet run`: avvia il programma con `latet run`, come
qualsiasi script. Ognuno è provato davvero attraverso Latet, configurato come qui sotto:

| Framework | Configurazione | Provato |
| --- | --- | --- |
| LangChain e LangGraph | `ChatOpenAI`, senza URL di base (`ChatAnthropic` legge `ANTHROPIC_BASE_URL` allo stesso modo) | Un modello di chat e un grafo costruito su di esso |
| AutoGen | `OpenAIChatCompletionClient`, senza URL di base | Un `AssistantAgent` |
| CrewAI | `LLM(model="openai/gpt-5.1")`, senza URL di base | Il suo `LLM` e una crew con un compito |

Per impostare l'indirizzo nel tuo codice, indicalo come `base_url`: `http://127.0.0.1:5283/openai/v1`
per i client di OpenAI, `http://127.0.0.1:5283/anthropic` per quelli di Anthropic.

Aider raggiunge il modello attraverso LiteLLM, che legge `OPENAI_API_BASE`: avvialo con
`latet run aider --model openai/gpt-5.1`, oppure tieni `openai-api-base: http://127.0.0.1:5283/openai/v1`
nel suo `.aider.conf.yml`. È provato davvero allo stesso modo.

**Cosa Latet non vede.** Latet sta tra il tuo programma e il modello, e da nessun'altra parte. Uno
strumento che il tuo agente chiama da sé, come un'API di ricerca sul web, un database o un vector
store nel cloud, riceve i valori reali: il modello lo richiede con valori fittizi, e Latet rimette
gli originali nella sua risposta prima che il tuo programma esegua lo strumento. Tieni questi
strumenti sulla tua macchina, oppure considera ciò che ricevono come inviato in chiaro.

## Cosa legge il gateway

- **Chat Completions** (`/chat/completions`, e il più vecchio `/completions`), la **Responses API**
  (`/responses`) e **Anthropic Messages** (`/v1/messages`, anche il conteggio dei token): il prompt di
  sistema, ogni messaggio, le chiamate agli strumenti e i risultati degli strumenti vengono mascherati,
  e le risposte vengono ripristinate, in streaming o no.
- **Embeddings** (`/embeddings`): l'input viene mascherato.
- Le **immagini** inviate come URL `data:` vengono lette e mascherate, con il
  [riconoscimento del testo](../../concepts/images/) opzionale. Le immagini indicate dal loro
  indirizzo, l'audio e i file vengono inviati così come sono, e nella dashboard hanno la
  segnalazione *Non ispezionato*.
- **Ogni altra richiesta** a OpenAI, Anthropic o OpenRouter, come l'elenco dei modelli, passa senza
  modifiche. Sui percorsi di Google, invece, una richiesta con un corpo che Latet non legge viene
  rifiutata: quello che Latet legge lì è elencato in
  [Gemini CLI e Google GenAI](../gemini/#cosa-legge-il-gateway).

Una richiesta ad Anthropic che arriva da Claude Code, o che contiene il token di un abbonamento
Claude, non viene mai riscritta: viene solo controllata, e bloccata se contiene un valore sensibile.
Vedi [Claude Code](../claude-code/).

## Buono a sapersi

- Il gateway è in ascolto solo su `127.0.0.1` e rifiuta le richieste che arrivano da una pagina web.
- Una richiesta a un percorso diverso da quelli qui sopra riceve un errore `404`:
  `no upstream for this path`.
- **Un altro provider compatibile con OpenAI** (DeepSeek, Mistral, un server di modelli sulla tua
  rete): aggiungilo come destinazione, poi usa il suo percorso come URL di base dell'agente, qui
  `http://127.0.0.1:5283/deepseek/v1`:

  ```sh
  latet config add upstreams "{name: deepseek, url: https://api.deepseek.com}"
  ```

  Vedi [`upstreams`](../../reference/configuration/#upstreams-items).
- Gli agenti autonomi come Hermes Agent e OpenClaw accettano questi URL di base nelle loro
  impostazioni: vedi [Hermes Agent, OpenClaw e altri agenti](../autonomous-agents/).
