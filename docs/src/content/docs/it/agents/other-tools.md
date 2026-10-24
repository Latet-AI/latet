---
title: GitHub Copilot, JetBrains, Zed e altri strumenti
description: Fai passare dal gateway di Latet GitHub Copilot, Junie e AI Assistant di JetBrains, Zed, Cline, Kilo Code, Continue, Qwen Code, Crush, Prime Agent, Android Studio e Claude Desktop, e scopri quali strumenti Latet non può proteggere.
sidebar:
  label: Copilot, JetBrains e altri
---

Molti strumenti di AI ti permettono di scegliere l'indirizzo del modello che chiamano. Imposta
quell'indirizzo sul **gateway** di Latet e ogni richiesta che lo strumento invia viene mascherata
prima di uscire dal tuo computer, e sul tuo computer la risposta viene ripristinata. Nello strumento
non si installa niente: basta un'impostazione.

Tieni la chiave API del tuo provider nello strumento, come al solito. Latet la inoltra al provider
senza modifiche e non la scrive mai nei suoi log né nella dashboard.

| Provider | Indirizzo da dare allo strumento |
| --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `http://127.0.0.1:5283/anthropic`, o `http://127.0.0.1:5283/anthropic/v1` per uno strumento che vi aggiunge solo `/messages` |
| OpenRouter | `http://127.0.0.1:5283/openrouter/api/v1` |

Ogni sezione qui sotto indica l'indirizzo esatto che serve allo strumento. Uno strumento il cui
provider OpenRouter integrato non permette di cambiarne l'indirizzo raggiunge OpenRouter attraverso
un provider compatibile con OpenAI, con l'indirizzo di OpenRouter qui sopra.

Junie CLI, Kilo Code, Continue e Crush leggono le impostazioni anche dal progetto che apri, e le
impostazioni di un progetto possono puntarli a un altro indirizzo: controllale prima di lavorare in un
repository che non hai scritto tu.

## In sintesi

*Provato* significa che lo strumento stesso è stato fatto passare da Latet verso un provider di
prova, su ogni API che parla: il provider ha ricevuto valori fittizi, e lo strumento ha mostrato
quelli reali.

| Strumento | Cosa passa da Latet | Cosa no | Provato |
| --- | --- | --- | --- |
| GitHub Copilot in VS Code | La chat e gli agenti, con un modello Custom Endpoint | I suggerimenti inline e le altre funzioni che usano il servizio di GitHub | Sì |
| GitHub Copilot CLI | Ogni richiesta, con un tuo provider | Niente, in modalità offline | Sì |
| GitHub Copilot negli IDE JetBrains e in Visual Studio | La chat, con un endpoint personalizzato compatibile con OpenAI | I suggerimenti inline | No |
| JetBrains Junie CLI | Ogni richiesta, con un modello personalizzato | Niente | Sì |
| JetBrains AI Assistant | AI Chat e le funzioni a cui assegni il modello | Il completamento del codice e gli agenti in AI Chat | No |
| Zed | Il pannello dell'agente e l'assistente inline | Le edit prediction | No |
| Cline | Ogni richiesta | Niente | La CLI |
| Kilo Code | Ogni richiesta, con un provider personalizzato | Niente | La CLI |
| Continue | Ogni richiesta di un modello il cui `apiBase` è Latet | Niente | La CLI |
| Qwen Code | Ogni richiesta | Un indirizzo email scritto nel prompt | Sì |
| Crush | Ogni richiesta | Niente | Sì |
| Prime Agent | Ogni richiesta ad Anthropic, OpenAI e OpenRouter, con una chiave API | Prime Inference e un abbonamento ChatGPT; con un abbonamento Claude, le richieste vengono controllate, non mascherate | Sì |
| Android Studio | La chat e la modalità agente, con un provider remoto di terze parti | Le funzioni proprie di Gemini | No |
| Claude Desktop su un gateway | Ogni richiesta viene controllata, e fermata se porterebbe con sé un valore sensibile | Il mascheramento, fuori dalla scheda Code | No |

## GitHub Copilot in VS Code

Il **Custom Endpoint** di VS Code invia Copilot Chat a un modello all'indirizzo che scegli tu.
Funziona con o senza l'accesso a GitHub, con o senza un piano Copilot; con Copilot Business ed
Enterprise, un amministratore può disattivarlo.

1. Esegui **Chat: Manage Language Models**, poi **Add Models** → **Custom Endpoint**.
2. Inserisci un nome e la chiave API del tuo provider. VS Code conserva la chiave nel suo archivio
   dei segreti.
3. Descrivi ogni modello nel file che VS Code apre, `chatLanguageModels.json`, accanto al tuo
   `settings.json`. L'`url` di ogni modello è l'indirizzo completo della chiamata, e `apiType` è
   l'API che parla:

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

Per l'API Chat Completions, usa `http://127.0.0.1:5283/openai/v1/chat/completions` con
`"apiType": "chat-completions"`. Scegli il modello nel selettore dei modelli della chat; per
funzionare in modalità agente, un modello ha bisogno di `"toolCalling": true`.

**Cosa non passa da Latet.** I suggerimenti inline (i completamenti del codice e i next edit
suggestions), la ricerca nel codice e le altre funzioni che si basano sugli embedding di GitHub usano
il servizio di GitHub, non il tuo modello: quando hai fatto l'accesso, escono senza passare da Latet.
Con l'accesso, Copilot affida anche i compiti brevi, come i titoli delle chat e i messaggi di commit,
ai modelli di GitHub; per tenerli sul tuo modello, imposta
`"chat.byokUtilityModelDefault": "mainAgent"` nelle tue impostazioni, oppure scegli i modelli per
quei compiti con `chat.utilityModel` e `chat.utilitySmallModel`.

## GitHub Copilot CLI

La CLI di Copilot passa da Latet con un tuo provider: una chiave di OpenAI, Anthropic o OpenRouter,
o un altro indirizzo, dati nelle variabili d'ambiente. Con l'accesso a GitHub, sui modelli di un
piano Copilot, invia al servizio Copilot di GitHub, davanti al quale Latet non può mettersi:
`latet copilot` lo dice e non avvia nulla.

```sh
export COPILOT_PROVIDER_TYPE=openai          # Chat Completions
export COPILOT_PROVIDER_API_KEY=...          # your OpenAI key
export COPILOT_MODEL=gpt-5.1
export COPILOT_OFFLINE=true                  # no GitHub sign-in, no telemetry
latet copilot
```

In PowerShell:

```powershell
$env:COPILOT_PROVIDER_TYPE = "openai"
$env:COPILOT_PROVIDER_API_KEY = "..."        # your OpenAI key
$env:COPILOT_MODEL = "gpt-5.1"
$env:COPILOT_OFFLINE = "true"
latet copilot
```

`latet copilot` fa puntare l'indirizzo del provider a Latet solo per quella sessione: quelli di
OpenAI, Anthropic e OpenRouter vanno alle rotte di Latet, un altro indirizzo diventa una delle tue
destinazioni, e uno su questo computer resta com'è. Con OpenRouter, imposta
`COPILOT_PROVIDER_BASE_URL=https://openrouter.ai/api/v1` e la tua chiave di OpenRouter; per
Anthropic, `COPILOT_PROVIDER_TYPE=anthropic` e la tua chiave di Anthropic. Per avviare tu stesso
`copilot`, imposta invece `COPILOT_PROVIDER_BASE_URL` all'indirizzo di Latet:
`http://127.0.0.1:5283/openai/v1`, `/openrouter/api/v1` o `/anthropic`.

`COPILOT_PROVIDER_WIRE_API=responses` usa invece la Responses API. Il modello deve supportare le
chiamate agli strumenti e lo streaming. Senza `COPILOT_OFFLINE=true`, la CLI invia comunque la
telemetria a GitHub, e funzioni come `/delegate` richiedono l'accesso a GitHub; i tuoi prompt vanno
solo al tuo provider.

## GitHub Copilot negli IDE JetBrains e in Visual Studio

Il plugin di Copilot per JetBrains accetta un endpoint personalizzato compatibile con OpenAI: apri
**Manage Models** dal selettore dei modelli della chat, aggiungi l'endpoint
`http://127.0.0.1:5283/openai/v1` con la tua chiave API e scegli il suo modello nella chat. Visual
Studio 2026 fa lo stesso in modalità agente, dalle sue impostazioni dei modelli. In entrambi, i
suggerimenti inline continuano a usare il servizio di GitHub.

## JetBrains Junie CLI

Junie CLI legge un modello personalizzato da un file di profilo in `~/.junie/models/`, o in
`.junie/models/` in un progetto. Il suo `baseUrl` è l'indirizzo completo della chiamata: Junie non vi
aggiunge niente.

```json title="~/.junie/models/latet.json"
{
  "id": "gpt-5.1",
  "baseUrl": "http://127.0.0.1:5283/openai/v1/responses",
  "apiType": "OpenAIResponses",
  "apiKey": "${OPENAI_API_KEY}"
}
```

Avvialo con `junie --model custom:latet`, oppure scegli il modello sotto **Custom** in `/model`. Gli
altri valori di `apiType`:

| `apiType` | `baseUrl` |
| --- | --- |
| `OpenAICompletion` (Chat Completions) | `http://127.0.0.1:5283/openai/v1/chat/completions` |
| `Anthropic` | `http://127.0.0.1:5283/anthropic/v1/messages` |

Un modello personalizzato non richiede un account JetBrains.
`JUNIE_SHARE_ANONYMOUS_STATISTICS=false` disattiva le statistiche d'uso anonime. Junie dentro l'IDE
accetta solo chiavi di OpenAI e di Anthropic, non un indirizzo personalizzato.

## JetBrains AI Assistant

In **Settings | Tools | AI Assistant | Providers & API keys**, sotto **Third-party AI providers**,
aggiungi un provider compatibile con OpenAI con l'URL `http://127.0.0.1:5283/openai/v1` e la tua
chiave API, e attiva **Tool calling**. I suoi modelli compaiono nel selettore dei modelli di AI Chat;
in **Models Assignment** puoi affidare loro anche le altre funzioni, come i messaggi di commit.

Il completamento del codice non usa un tuo modello, e gli agenti in AI Chat, come Junie, Claude Agent
e Codex, accettano le chiavi dei loro provider, non un indirizzo personalizzato: non passano da
Latet.

## Zed

Aggiungi un provider nel `settings.json` di Zed, poi scegli il suo modello nel pannello dell'agente:

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

Zed legge la chiave del provider `Latet` dalle impostazioni dell'agente, o dalla variabile
`LATET_API_KEY`; il provider Anthropic mantiene la sua chiave. Anche i provider OpenAI e OpenRouter
integrati in Zed accettano un `api_url`.

Le **edit prediction** arrivano per impostazione predefinita dal servizio di Zed e non passano da
Latet: imposta `"edit_predictions": { "provider": "none" }` per disattivarle.

## Cline

Nelle impostazioni di Cline, scegli il provider **OpenAI Compatible** con l'URL di base
`http://127.0.0.1:5283/openai/v1`, la tua chiave API e l'ID di un modello. Per Anthropic, scegli il
provider **Anthropic**, spunta **Use custom base URL** e inserisci
`http://127.0.0.1:5283/anthropic/v1`: Cline vi aggiunge `/messages`.

La CLI di Cline accetta il provider compatibile con OpenAI dalla riga di comando:

```sh
cline auth -p openai-compatible -k "$OPENAI_API_KEY" -m gpt-5.1 -b http://127.0.0.1:5283/openai/v1
```

Per il suo provider Anthropic, imposta `baseUrl` in `~/.cline/data/settings/providers.json`:
`cline auth` accetta un URL di base solo per i provider compatibili con OpenAI.

## Kilo Code

Nell'estensione Kilo Code, aggiungi un provider personalizzato (**custom provider**) nelle sue
impostazioni, con l'API che parla (OpenAI Compatible, OpenAI Responses o Anthropic Messages) e l'URL
di base qui sotto. La CLI `kilo` legge lo stesso provider da `kilo.json`, in `~/.config/kilo/` o in
un progetto:

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

`disabled_providers` disattiva il provider di Kilo, le cui richieste vanno al servizio di Kilo senza
passare da Latet.
Per Anthropic, usa `"npm": "@ai-sdk/anthropic"` con l'URL di base
`http://127.0.0.1:5283/anthropic/v1`; per la Responses API, `"npm": "@ai-sdk/openai"` con
`http://127.0.0.1:5283/openai/v1`.

## Continue

Le estensioni di Continue per VS Code e JetBrains e la sua CLI `cn` leggono lo stesso
`~/.continue/config.yaml`. Dai a ogni modello l'indirizzo di Latet in `apiBase`:

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

L'`apiBase` di Anthropic finisce con `/v1`: Continue vi aggiunge `/messages`.

## Qwen Code

Qwen Code legge il suo indirizzo dalle stesse variabili degli SDK ufficiali, quindi `latet run` lo
fa passare da Latet:

```sh
latet run qwen
```

Usa `OPENAI_BASE_URL` con il suo provider compatibile con OpenAI e `ANTHROPIC_BASE_URL` con quello
di Anthropic; scegli il provider in `~/.qwen/settings.json`
(`"security": { "auth": { "selectedType": "openai" } }`, oppure `"anthropic"`), e imposta la chiave e
il modello in `OPENAI_API_KEY` e `OPENAI_MODEL`, oppure in `ANTHROPIC_API_KEY` e `ANTHROPIC_MODEL`.
Senza `latet run`, imposta tu gli URL di base: `http://127.0.0.1:5283/openai/v1` e
`http://127.0.0.1:5283/anthropic`.

**Un indirizzo email che scrivi tu.** Qwen Code legge ogni `@` in un prompt che scrivi come un file
da allegare, e invia `mario.rossi@acme.it` come `mario.rossi @acme.it`. Latet non lo riconosce come
un indirizzo, quindi esce in chiaro. Gli indirizzi nei file, nell'output dei comandi e nelle pagine
che Qwen Code legge non vengono modificati, e Latet li maschera.

## Crush

Aggiungi un provider al `crush.json` di Crush, in un progetto o nella cartella delle impostazioni di
Crush:

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

Scegli quel provider per entrambi i modelli di Crush, così che nessuna richiesta, comprese quelle
brevi che danno un nome a una sessione, vada a un provider che Crush ha configurato da sé a partire
dal tuo ambiente:

```json title="crush.json"
"models": {
  "large": { "model": "gpt-5.1", "provider": "latet" },
  "small": { "model": "gpt-5.1", "provider": "latet" }
}
```

Per Anthropic, usa `"type": "anthropic"` con `http://127.0.0.1:5283/anthropic`. Il tipo `openai` di
Crush parla la Responses API, e il suo tipo `openrouter` non accetta un URL di base: raggiungi
OpenRouter con `openai-compat` e `http://127.0.0.1:5283/openrouter/api/v1`.

## Prime Agent

Prime Agent legge l'indirizzo di ogni provider da `models.json`, in `~/.prime/agent`
(`%USERPROFILE%\.prime\agent` su Windows). Dai ai suoi provider integrati l'indirizzo di Latet:

```json title="~/.prime/agent/models.json"
{
  "providers": {
    "anthropic": { "baseUrl": "http://127.0.0.1:5283/anthropic" },
    "openai": { "baseUrl": "http://127.0.0.1:5283/openai/v1" },
    "openrouter": { "baseUrl": "http://127.0.0.1:5283/openrouter/api/v1" }
  }
}
```

Da quel momento ogni modello di questi provider passa da Latet, con la chiave API che hai dato a
Prime Agent con `/login` o in `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` e `OPENROUTER_API_KEY`.

**Con un abbonamento invece di una chiave.** Con l'accesso a un abbonamento Claude, Prime Agent si
presenta ad Anthropic come Claude Code. Le sue richieste passano comunque da Latet, che controlla le
richieste fatte con un abbonamento Claude e non le riscrive mai: quella che porterebbe con sé un
segreto o un dato personale in chiaro viene fermata, con un messaggio al posto della risposta. Usa
una chiave API per farle mascherare. Le richieste fatte con un abbonamento ChatGPT, e quelle a Prime
Inference, il servizio di Prime Intellect, non passano da Latet.

## Android Studio

In **Settings → Tools → AI → Model Providers**, aggiungi un **Third-Party Remote Provider** con
l'indirizzo di Latet al posto di quello del provider e la tua chiave API, poi aggiorna l'elenco dei
modelli e scegli quelli da usare: `http://127.0.0.1:5283/openai/v1` per OpenAI,
`http://127.0.0.1:5283/anthropic` per Anthropic, `http://127.0.0.1:5283/openrouter/api/v1` per
OpenRouter. La chat e la modalità agente usano il modello che scegli; le funzioni proprie di Gemini
continuano a usare il servizio di Google.

## Claude Desktop su un gateway

Claude Desktop può funzionare con un gateway di inferenza invece che con il servizio di Anthropic,
senza l'accesso a Claude: un'organizzazione lo usa per collegarlo al proprio provider. In questo caso
non c'è un abbonamento Pro o Max: le richieste si pagano a consumo, con una chiave API di Anthropic o
attraverso il provider della tua azienda. Su un singolo computer, senza fare l'accesso, attiva
**Help → Troubleshooting → Enable Developer Mode**, poi apri
**Developer → Configure Third-Party Inference**; un'organizzazione imposta le stesse chiavi da un
profilo di gestione dei dispositivi. Imposta Latet come gateway:

| Chiave | Valore |
| --- | --- |
| `inferenceProvider` | `gateway` |
| `inferenceGatewayBaseUrl` | `http://127.0.0.1:5283/anthropic` |
| `inferenceGatewayApiKey` | La tua chiave API di Anthropic |
| `inferenceGatewayAuthScheme` | `x-api-key` |

Claude Desktop accetta un indirizzo `http://` quando punta a questo computer. Chat, Cowork e Code
inviano allora ogni richiesta attraverso Latet, e anche l'elenco dei modelli arriva da Anthropic
passando da Latet.

**Cosa ne fa Latet.** Queste richieste le costruisce il motore di Claude Code, e Latet non riscrive
mai una richiesta di Claude Code: le controlla una per una, e ferma quella che porterebbe con sé un
segreto o un dato personale in chiaro, con un messaggio al posto della risposta. Non le maschera,
come fa invece per gli altri strumenti di questa pagina: la
[guida ai gateway](https://code.claude.com/docs/en/llm-gateway-protocol) di Anthropic chiede a un
gateway di esaminare le richieste di Claude Code, non di modificarle. Nella scheda Code il plugin di
Latet maschera i contenuti prima che la richiesta venga costruita, quindi la scheda Code ha sia il
mascheramento sia il controllo.

Con l'accesso a Claude, Claude Desktop non usa un gateway: Chat e Cowork non sono coperte, e nella
scheda Code il plugin maschera senza nessun controllo dietro. Vedi
[Claude Code](../claude-code/#copertura).

## Cosa Latet non può proteggere

Latet gira sul tuo computer, tra lo strumento e il modello. Questi strumenti inviano le loro
richieste dai server del produttore, o chiamano un servizio che nessuna impostazione può cambiare,
quindi le loro richieste non passano mai da Latet:

| Strumento | Perché |
| --- | --- |
| Cursor e la sua CLI | Cursor costruisce e invia ogni richiesta al modello dai propri server, anche con la tua chiave API. |
| Windsurf e Devin Desktop | Le richieste vanno ai server del produttore, che chiamano il modello. |
| Google Antigravity (l'IDE) | Le richieste vanno al servizio di Google, che chiama il modello. |
| Jules | Gira nel cloud di Google, non sul tuo computer. |
| Kiro e Amazon Q Developer | Le richieste vanno al servizio di Amazon, che chiama il modello. |
| Warp | Le richieste vanno ai server di Warp, che chiamano il modello. |
| L'app desktop di ChatGPT | Parla solo con il servizio di OpenAI, e non ha un'impostazione per un altro indirizzo. |
| Claude Desktop con l'accesso a Claude, e le sessioni cloud | Chat e Cowork chiamano direttamente il servizio di Anthropic, e le sessioni cloud girano sull'infrastruttura di Anthropic. |

Se uno strumento che usi non è in questa pagina, cerca un'impostazione chiamata base URL, API base,
endpoint o custom provider: se ce l'ha, passa da Latet come gli strumenti qui sopra. Se non
funziona, [apri una issue](https://github.com/Latet-AI/latet/issues), per favore.
