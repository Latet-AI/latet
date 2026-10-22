---
title: Hermes Agent, OpenClaw e altri agenti
description: Gli agenti autonomi attraverso il gateway di Latet, tra cui Hermes Agent, OpenClaw, Goose e OpenHands, e qualsiasi agente che accetti un URL di base compatibile con OpenAI.
sidebar:
  label: Agenti autonomi
---

Gli agenti autonomi lavorano da soli, per ore: leggono caselle di posta, navigano sul web, interrogano
database, eseguono comandi e chiamano API, e nessuno legge ogni passaggio. Ogni pagina, file e
risultato che leggono va al modello con la richiesta successiva. Se li fai puntare a Latet, ognuna di
quelle richieste viene mascherata prima di uscire dal tuo computer, e sul tuo computer le risposte
vengono ripristinate.

Passano dal **gateway** di Latet, come qualsiasi client compatibile con OpenAI: imposti l'URL di base
dell'agente su Latet e tieni la chiave API del tuo provider nell'agente, come al solito.
`latet global-install` non cambia i file di questi agenti: bastano le impostazioni qui sotto.

| Provider | URL di base da impostare | Chiave |
| --- | --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` | La tua chiave API di OpenAI |
| OpenRouter (Claude, Gemini e centinaia di altri modelli) | `http://127.0.0.1:5283/openrouter/api/v1` | La tua chiave API di OpenRouter |
| Anthropic, per un agente che parla il formato di Anthropic | `http://127.0.0.1:5283/anthropic` | La tua chiave API di Anthropic |

Un abbonamento Claude Pro o Max funziona solo in Claude Code, come dicono i termini di Anthropic,
quindi questi agenti usano una chiave API: raggiungono i modelli Claude attraverso OpenRouter, o con
una chiave API di Anthropic. La chiave passa attraverso Latet fino al provider e non viene mai scritta
nei log né mostrata nella dashboard.

## Hermes Agent

Hermes legge il suo modello e il suo endpoint da `config.yaml` nella sua cartella: `~/.hermes` su
macOS e Linux, `%LOCALAPPDATA%\hermes` su Windows (`HERMES_HOME` ne imposta un'altra). Fai
puntare un provider personalizzato a Latet:

```yaml title="config.yaml"
model:
  provider: custom
  base_url: http://127.0.0.1:5283/openrouter/api/v1   # or http://127.0.0.1:5283/openai/v1
  key_env: OPENROUTER_API_KEY                          # the variable that holds your key
  default: anthropic/claude-sonnet-4.5                 # a model your provider lists
```

`hermes model` imposta le stesse cose in modo interattivo. `OPENAI_BASE_URL` nell'ambiente non basta:
Hermes ne tiene conto solo per il suo provider `openai-api`, quindi `latet run hermes` non farebbe
passare da Latet un provider personalizzato.

## OpenClaw

OpenClaw legge `~/.openclaw/openclaw.json` (JSON5) e applica da solo le modifiche che ci fai. Aggiungi
un provider che punta a Latet, e rendi uno dei suoi modelli quello predefinito:

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

Per OpenRouter, usa `http://127.0.0.1:5283/openrouter/api/v1`, la tua chiave di OpenRouter e i nomi
dei suoi modelli.

## Goose

Il provider OpenAI di Goose accetta un host e un percorso, che unisce:

```sh
export OPENAI_HOST=http://127.0.0.1:5283
export OPENAI_BASE_PATH=openai/v1/chat/completions
export OPENAI_API_KEY=...        # your OpenAI key, as usual
export GOOSE_PROVIDER=openai
export GOOSE_MODEL=gpt-5.1
goose session
```

`GOOSE_PROVIDER` e `GOOSE_MODEL` scelgono il provider e il modello: senza, Goose ti chiede di
eseguire prima `goose configure`, che salva gli stessi valori. Per OpenRouter, aggiungi con
`goose configure` un provider personalizzato compatibile con OpenAI, con l'URL di base
`http://127.0.0.1:5283/openrouter/api/v1` e la tua chiave di OpenRouter; questa configurazione non è
tra quelle provate.

## OpenHands

Nelle impostazioni LLM di OpenHands, sotto **Advanced**, scegli un modello personalizzato con il
prefisso del suo provider (`openai/gpt-5.1`), imposta l'URL di base su
`http://127.0.0.1:5283/openai/v1` e inserisci la tua chiave API. Dalla riga di comando:

```sh
LLM_MODEL=openai/gpt-5.1 LLM_BASE_URL=http://127.0.0.1:5283/openai/v1 LLM_API_KEY=... \
  openhands --override-with-envs
```

## Qualsiasi altro agente

Un agente che accetta un URL di base compatibile con OpenAI passa da Latet con gli indirizzi della
tabella qui sopra. Uno che legge `OPENAI_BASE_URL` o `ANTHROPIC_BASE_URL` dall'ambiente si può
avviare con [`latet run`](../openai-compatible/#per-un-comando-latet-run), che le imposta solo per
quell'agente.

**Un agente in un container** non raggiunge Latet su `127.0.0.1`: lì, `127.0.0.1` è il container
stesso, e Latet risponde solo alle richieste indirizzate a `127.0.0.1` o a `localhost`. Esegui
l'agente sul tuo computer; su Linux, anche un container avviato con `--network host` condivide il
`127.0.0.1` del tuo computer.

## Per un agente che nessuno sorveglia

- **Attiva il [revisore locale](../../concepts/llm-review/)** se hai un server di modelli locale.
  Legge quello che l'agente recupera, la parte che nessuno scrive, e per impostazione predefinita una
  richiesta attende la sua revisione e viene fermata quando il revisore non può rispondere.
- **Usa la [modalità dati rigorosa](../../concepts/production-data/)** per un agente che legge
  database o log: ogni valore di una tabella o di un documento JSON viene mascherato, e le join
  funzionano ancora.
- **Seguilo nella dashboard:** `latet dashboard` mostra ogni richiesta che l'agente ha inviato, cosa
  è stato mascherato al suo interno e cosa è stato bloccato.
- **Lascia che gestisca Latet, in sicurezza:** indicagli
  [`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md). Quello che aggiunge
  protezione si applica; quello che la riduce attende la tua approvazione nella dashboard. Vedi
  [Gli agenti e le tue impostazioni](../../concepts/agents-and-settings/).

Ognuno di questi quattro agenti è provato davvero, configurato come qui sopra: invia un compito
attraverso Latet a un provider di prova che risponde come OpenAI, il provider riceve valori
fittizi e l'agente riceve indietro quelli reali. Se un agente non funziona attraverso Latet,
[apri una issue](https://github.com/Latet-AI/latet/issues), per favore.
