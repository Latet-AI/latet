---
title: Endpoint aziendali
description: Claude Code e Codex attraverso il gateway della tua azienda, Amazon Bedrock, Google Vertex, Microsoft Foundry o Azure OpenAI, con Latet davanti.
---

Al lavoro, spesso Claude Code e Codex raggiungono il modello attraverso un percorso scelto dalla tua
azienda, invece di andare direttamente ad Anthropic o a OpenAI: il suo gateway LLM (LiteLLM,
Portkey...), Amazon Bedrock, Google Vertex, Microsoft Foundry o Azure OpenAI. Il modello è lo stesso;
cambia chi lo vende e dove vanno le richieste.

Latet legge quel percorso dalle impostazioni dell'agente stesso e si mette davanti. Le richieste
raggiungono lo stesso endpoint, con le stesse chiavi, dopo che Latet le ha controllate (Claude Code) o
mascherate (Codex). Niente da configurare a mano: lo fanno `latet claude`, `latet codex` e
`latet global-install`.

## Come funziona

- **Latet legge dove va l'agente.** Per Claude Code: le variabili della tua shell, il tuo
  `settings.json` e le impostazioni gestite della tua organizzazione, come le unisce Claude Code. Per
  Codex: `config.toml` e i file dei profili (`<name>.config.toml`). Le impostazioni di un repository
  non vengono mai lette: un progetto che cloni non può decidere dove vanno le tue richieste.
- **L'indirizzo di un'azienda diventa una destinazione.** Viene salvato in `config.yaml` sotto
  [`upstreams`](../../reference/configuration/#upstreams-items), con un nome preso dall'indirizzo
  (`https://llm.acme.com` diventa `llm-acme-com`), e l'agente invia invece a
  `http://127.0.0.1:5283/llm-acme-com`. Lo stesso indirizzo ha sempre la stessa destinazione.
  Alcuni nomi sono delle rotte di Latet, e una destinazione non può usarli:
  `anthropic`, `openai`, `chatgpt`, `openrouter`, `gemini`, `bedrock`, `vertex` e `backend-api`.
- **Bedrock e Vertex hanno percorsi propri**, `/bedrock/<region>/` e `/vertex/`, che raggiungono solo
  gli indirizzi di Amazon e di Google.
- **Quando Latet non può mettersi davanti, lo dice.** L'agente non si avvia, e Latet dice perché e
  cosa fare. Non lascia mai che un agente lo aggiri in silenzio.

Per una sessione, `latet claude` passa le sue impostazioni con l'opzione `--settings` di Claude Code,
che prevale sulle tue impostazioni e su quelle del progetto; `latet codex` aggiunge opzioni `-c`. In
modo permanente, `latet global-install` scrive lo stesso nei file degli agenti, e
`latet global-uninstall` rimette ogni indirizzo che ha sostituito, anche se nel frattempo hai
modificato il file.

## Il gateway della tua azienda

Claude Code configurato con il gateway della tua azienda:

```json title="~/.claude/settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "https://llm.acme.com", "ANTHROPIC_AUTH_TOKEN": "sk-acme-..." } }
```

Con `latet claude`, Claude Code invia a `http://127.0.0.1:5283/llm-acme-com`, e Latet inoltra ogni
richiesta a `https://llm.acme.com`, con la chiave della tua azienda così come l'ha inviata Claude Code.
Codex funziona allo stesso modo, con `openai_base_url` o con un provider suo:

```toml title="~/.codex/config.toml"
model_provider = "acme"

[model_providers.acme]
name = "Acme gateway"
base_url = "https://llm.acme.com/v1"
env_key = "ACME_API_KEY"
```

Si sposta solo l'indirizzo di quel provider; la sua chiave, e tutto il resto, restano come li hai
scritti. Un provider scritto nel file di un profilo (`~/.codex/work.config.toml`) viene spostato lì, e
`latet codex --profile work` fa passare da Latet i provider di quel profilo e il suo `openai_base_url`.

## Amazon Bedrock

Claude Code su Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`) dimostra chi è in uno di due modi, e Latet li
gestisce entrambi.

- **Una chiave API di Bedrock** (`AWS_BEARER_TOKEN_BEDROCK`) viaggia con ogni richiesta come una
  password: passa attraverso Latet così com'è.
- **Il tuo login AWS** (un profilo AWS, un login SSO, chiavi in `~/.aws`): Claude Code firma ogni
  richiesta per l'indirizzo a cui la invia. Una firma fatta per l'indirizzo di Latet non varrebbe da
  Amazon, come una raccomandata a cui è stato cambiato l'indirizzo. Quindi Claude Code invia a Latet
  richieste non firmate (`CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`), e Latet firma ognuna per Amazon, dopo
  averla controllata, con il tuo profilo AWS.

Per firmare, Latet legge il tuo profilo come fanno gli strumenti di Amazon: con la AWS CLI
(`aws configure export-credentials`) quando è installata, il che copre i login SSO e i ruoli, oppure
dalle chiavi scritte in `~/.aws`. Latet cerca `aws` nel suo PATH, poi in `/usr/local/bin`,
`/opt/homebrew/bin`, `/usr/bin`, `~/.local/bin` e `/snap/bin` (in `C:\Program Files\Amazon\AWSCLIV2`
su Windows): il Latet che si avvia quando accedi al computer non vede il PATH della tua shell. Se la
tua AWS CLI è altrove, avvia Latet da una shell in cui `aws` funziona. Le credenziali restano nella
memoria di Latet, non vengono mai scritte da nessuna parte, e firmano solo le richieste ad Amazon
Bedrock. Quando il tuo login scade, Claude Code lo dice a parole chiare:
`run aws sso login --profile work`; una volta fatto, torna a funzionare senza riavviare niente.
Le impostazioni `awsAuthRefresh` e `awsCredentialExport` di Claude Code non vengono usate mentre
firma Latet: Latet legge da solo il tuo profilo AWS, quindi il login che avrebbero eseguito lo esegui
tu. `latet doctor` avvisa quando sono impostate.

Il percorso che firma contiene una chiave che solo il tuo Latet può creare
(`/bedrock/eu-west-1/profile/work/<key>`), così nessun altro programma o utente del tuo computer può
far firmare richieste a tuo nome. Le credenziali che esistono solo nelle variabili della tua shell
(aws-vault, per esempio) vengono passate da `latet claude` a Latet per quella sessione. Latet le tiene
in memoria, e solo finché quella sessione è in corso: `latet claude` tiene aperta una connessione con
Latet, e quando termina (anche se lo chiudi a forza) Latet dimentica le credenziali entro pochi
secondi. Le dimentica anche alla loro scadenza, quando il tuo strumento ne imposta una
(`AWS_SESSION_EXPIRATION` o `AWS_CREDENTIAL_EXPIRATION`). Dopo di che Claude Code dice a parole chiare
che le credenziali non ci sono più: rinnovale e avvia di nuovo `latet claude`. `global-install` ha
bisogno di un profilo AWS, che Latet può leggere da solo.

Con il tuo login AWS, scrivere nelle impostazioni l'indirizzo pubblico di Bedrock
(`ANTHROPIC_BEDROCK_BASE_URL` impostato su `https://bedrock-runtime.eu-west-1.amazonaws.com`) equivale
a non scriverne nessuno: Latet firma per quell'indirizzo. Viene usata la regione dell'indirizzo, anche
quando `AWS_REGION` ne indica un'altra: l'indirizzo è dove vanno le richieste. L'indirizzo FIPS di
Bedrock e un endpoint VPC sono altri casi: vedi
[Dove Latet non può mettersi davanti](#dove-latet-non-può-mettersi-davanti).

Le richieste a Bedrock vengono controllate, mai riscritte: arrivano da Claude Code, il cui plugin le ha
già mascherate. Le risposte di Bedrock arrivano a Claude Code esattamente come le ha inviate Amazon;
Latet le legge solo per mostrarle nella dashboard. Gli id dei modelli e gli ARN dei profili di
inferenza arrivano ad Amazon esattamente come li ha scritti Claude Code.

## Google Vertex

Claude Code su Vertex (`CLAUDE_CODE_USE_VERTEX=1`) invia il tuo login Google come token, che passa
attraverso Latet così com'è: niente da firmare. Ogni richiesta indica la sua località, e Latet la
invia all'indirizzo di Google corrispondente: l'endpoint globale, le multiregioni `eu` e `us`, o una
regione come `us-east5`. Funzionano anche modelli diversi in regioni diverse.

## Microsoft Foundry e Azure OpenAI

Claude Code su Microsoft Foundry (`CLAUDE_CODE_USE_FOUNDRY=1`, con `ANTHROPIC_FOUNDRY_RESOURCE` o
`ANTHROPIC_FOUNDRY_BASE_URL`) e Codex su Azure OpenAI (un provider il cui `base_url` è
`https://<resource>.openai.azure.com/openai/v1`) sono destinazioni come un gateway: l'indirizzo della
risorsa viene salvato (`foundry-acme`, `azure-acme`) e la chiave passa così com'è.

## Un modello su questo computer

Un indirizzo su questo computer (`http://localhost:11434` per Ollama, per esempio) viene lasciato
stare: un modello che gira qui non invia niente altrove, e Latet non può distinguerlo da un proxy che
inoltra al cloud. Se è un proxy del genere, LiteLLM per esempio, aggiungilo come destinazione, e Latet
si mette davanti:

```sh
latet config add upstreams "{name: litellm, url: http://localhost:4000}"
```

Poi avvia una nuova sessione di `latet claude` o di `latet codex`; se hai configurato i tuoi agenti
con `latet global-install`, esegui di nuovo `latet agents apply`: fino ad allora le loro impostazioni
continuano a inviare direttamente al proxy.

Latet te lo ricorda ogni volta che lascia stare un indirizzo del genere. Gli indirizzi sulla tua rete
locale e su internet passano sempre da Latet.

## Dove Latet non può mettersi davanti

- **Impostazioni gestite che impongono un indirizzo.** Quando le impostazioni gestite della tua
  organizzazione impostano `ANTHROPIC_BASE_URL` (o l'indirizzo di un cloud) su qualcos'altro, niente
  può scavalcarle. Latet lo dice e non avvia Claude Code. Il tuo reparto IT può impostare
  quell'indirizzo su Latet, come qui sotto.
- **La Mantle API di Amazon Bedrock** (`CLAUDE_CODE_USE_MANTLE`): Latet non la legge ancora. Usa la
  Invoke API di Bedrock (`CLAUDE_CODE_USE_BEDROCK`) o il gateway della tua azienda.
- **Amazon Bedrock attraverso un endpoint VPC** (un `ANTHROPIC_BEDROCK_BASE_URL` su
  `vpce-….vpce.amazonaws.com`): non ancora supportato. Latet dovrebbe firmare per quell'endpoint, e
  Amazon rifiuta una firma fatta per l'indirizzo di Latet. Latet lo dice e non avvia Claude Code: usa
  l'indirizzo pubblico di Bedrock, o il gateway della tua azienda. Con una chiave API di Bedrock
  (`AWS_BEARER_TOKEN_BEDROCK`) non c'è niente da firmare, e l'endpoint è una destinazione come
  qualsiasi indirizzo aziendale.
- **L'indirizzo FIPS di Amazon Bedrock** (un `ANTHROPIC_BEDROCK_BASE_URL` su
  `bedrock-runtime-fips.us-east-1.amazonaws.com`): non ancora supportato. Latet può inviare solo
  all'indirizzo standard, che non è un endpoint FIPS, quindi lo dice e non avvia Claude Code, invece
  di lasciar cadere il tuo requisito FIPS. Usa il gateway della tua azienda, o l'indirizzo standard se
  non ti serve FIPS. Con una chiave API di Bedrock (`AWS_BEARER_TOKEN_BEDROCK`) non c'è niente da
  firmare, e l'indirizzo FIPS resta la destinazione, come qualsiasi indirizzo aziendale.
- **Più di un cloud alla volta** (`CLAUDE_CODE_USE_BEDROCK` e `CLAUDE_CODE_USE_VERTEX`, per esempio):
  Latet non può sapere quale usa Claude Code, quindi lo dice e non lo avvia. Tienine uno.
- **Un indirizzo che due programmi potrebbero leggere in modo diverso** (con dentro un backslash, uno
  spazio, un nome utente o una password), o un server fuori dalla tua rete locale raggiunto via
  `http`: Latet non lo fa passare, e dice perché.
- **La scheda Code di Claude Desktop** ignora queste variabili: lì il plugin maschera, ma il daemon non
  vede le richieste (vedi [Claude Code](../claude-code/#copertura)).
- **OpenCode** passa da Latet con Anthropic, OpenAI e OpenRouter; i suoi altri provider no.

## Latet per tutti in un'azienda

Dove sono le impostazioni gestite a decidere l'indirizzo di Claude Code, il reparto IT può farlo
puntare al Latet di ogni computer, e dare a ogni Latet l'endpoint dell'azienda come destinazione:

```json title="managed-settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/llm-acme-com" } }
```

```sh
latet config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Ogni computer ha comunque bisogno del plugin di Latet per Claude Code: esegui lì `latet global-install`,
oppure avvia Claude Code con `latet claude`. Senza il plugin, le richieste di Claude Code arrivano a
Latet non mascherate, quindi Latet le controlla soltanto e ferma ognuna che contiene un valore
sensibile in chiaro. L'indirizzo indica la porta 5283: su un computer il cui Latet gira su un'altra
porta, Latet dice che le impostazioni gestite mandano Claude Code altrove, e non lo avvia.

## Controlli lungo il percorso

- **Il plugin controlla ogni sessione.** Quando Claude Code si avvia, il plugin guarda dove vanno
  davvero le sue richieste, impostazioni comprese. Se il `.claude/settings.json` di un progetto
  stabilisce dove vanno (un indirizzo, un cloud, un proxy), il plugin ferma i prompt della sessione e
  indica il file, anche quando punta a Latet: metti invece quella riga nelle tue impostazioni. Un
  progetto non può nemmeno far puntare il plugin a un altro Latet. Anche quando il controllo stesso
  non riesce, i prompt vengono fermati.
- **`latet agents status`** mostra il percorso di ogni agente. **`latet doctor`** lo mostra
  anch'esso, con una variabile della tua shell che le impostazioni di Claude Code scavalcano (non
  l'indirizzo della tua azienda quando Latet ci ha messo davanti il suo: è giusto così), e se la AWS
  CLI c'è quando Latet firma per Bedrock.
- **La dashboard** elenca le tue destinazioni in Agenti protetti → Dove vanno le richieste, con l'agente
  che ha indicato ciascuna e quando è stata usata l'ultima volta, e su ogni scambio mostra dove è
  andato e se Latet l'ha firmato. Rimuoverne una lì toglie quella sola destinazione e nessun'altra.
  Una destinazione ancora in uso non si può rimuovere: le impostazioni di Claude Code o di Codex, o le
  impostazioni gestite della tua organizzazione, ci puntano, oppure è stata registrata da quando Latet
  si è avviato, da una sessione di `latet claude` o di `latet codex`, da `latet agents apply` o da
  `latet global-install`. Latet non vede una destinazione che usa solo un altro agente (OpenCode,
  Hermes, un programma avviato con `latet run`): controlla che nessuno la usi prima di rimuoverla.
- **Una destinazione non si sposta senza di te.** Cambiare l'indirizzo di una destinazione, o
  rimuoverla, fuori dalla dashboard (il comando di un agente, `config.yaml` modificato a mano) attende
  la tua approvazione in Approvazioni, con l'indirizzo prima e dopo: manderebbe le tue richieste, e le
  tue chiavi, da un'altra parte. Aggiungerne una no: la usa solo un agente che ci punta. Se una
  modifica è in attesa quando un agente ne aggiunge una, Approvazioni mostra di nuovo la modifica, con
  dentro la nuova destinazione e quello che sposterebbe ora; una modifica che non riduce più niente si
  applica subito.

## Con cosa è stato provato

Il vero Claude Code (2.1.294) e il vero Codex (0.161.0), attraverso Latet, con provider che rispondono
come fa ciascun cloud, su un solo computer: il gateway di un'azienda, Bedrock con una chiave API, con
un profilo per cui Latet ha firmato e con l'ARN di un modello, Vertex, e un provider di Codex nella
forma di Azure. Le firme di Latet corrispondono a quelle della libreria di AWS, richiesta per
richiesta. Se usi uno di questi cloud al lavoro,
[facci sapere come va](https://github.com/Latet-AI/latet/issues).
