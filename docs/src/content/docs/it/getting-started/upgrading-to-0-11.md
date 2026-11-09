---
title: Passare alla 0.11
description: Cosa rifiuta Latet 0.11 che la 0.10 inviava, cosa controllare prima dell'aggiornamento e come tornare indietro.
---

Latet 0.11.0 legge ogni parte di una richiesta prima di inviarla: il percorso, i parametri di query,
le intestazioni e il corpo. Una richiesta con una parte che Latet non ispeziona né lascia passare per
una regola o un'eccezione che hai approvato non viene inviata. Un corpo vuoto non basta più per far
passare una richiesta. Le credenziali che servono al tuo provider, e le parti che un provider
firma, seguono regole proprie. Vale allo stesso modo sul tuo computer e su un server Latet.

Una chiamata che la 0.10 inviava senza leggerla può quindi essere rifiutata ora. Questa pagina elenca
cosa cambia, cosa controlla per te `latet doctor` prima e dopo l'aggiornamento, e cosa fare in ogni
caso.

## Cosa rifiuta la 0.11 che la 0.10 inviava

**Chiamate senza corpo.** L'elenco dei modelli e la lettura di un modello passano su ogni provider
che Latet conosce: Anthropic, OpenAI, OpenRouter, ChatGPT (l'elenco dei modelli di Codex), Gemini e i
profili di inferenza di Bedrock. Anche i loro parametri di query vengono letti (`limit`, `after_id`,
`pageToken`, `maxResults`...). Ogni altra chiamata viene rifiutata: file e upload, batch, audio,
generazione di immagini, moderazione, lettura o cancellazione di una risposta salvata, lettura di una
cache di Gemini, gli endpoint di un account. L'agente lo viene a sapere, e non viene inviato niente:

```text
Latet: request envelope refused (envelope-route): route /{destination}/{unsupported-route}; this version of Latet does not support this call, so change the agent or its settings so that it does not make it. Nothing was sent; run `latet dashboard` to see where it was
```

Un'eccezione nomina un'intestazione, un parametro di query o un membro del corpo, mai una rotta
intera: nella 0.11.0 nessuna eccezione fa passare una di queste chiamate. Cambia le impostazioni
dell'agente perché non faccia la chiamata, oppure falla senza Latet, all'indirizzo del provider,
sapendo che Latet non vede ciò che invia.

**Parametri di query.** Un parametro di query che nessun profilo nomina viene rifiutato, e
all'agente viene detto il suo nome, mai il suo valore:

```text
Latet: request envelope refused (envelope-query-unknown): query parameter drift. Nothing was sent; run `latet dashboard` to see where it was
```

**Intestazioni.** Ogni intestazione è letta con una regola propria: una credenziale viene inviata
com'è arrivata e mai scritta nei log; un'intestazione di un agente deve avere la forma che quell'agente
le dà (un id di sessione deve essere un UUID, per esempio); un'intestazione che può contenere testo
libero, come `user-agent`, passa il controllo finale. Un'intestazione che nessun profilo nomina viene
rifiutata, e all'agente viene detto il suo nome, mai il suo valore:

```text
Latet: request envelope refused (envelope-header-unknown): header x-team. Nothing was sent; run `latet dashboard` to see where it was
```

In una richiesta che Latet si limita a verificare (Claude Code, Bedrock, Vertex), le intestazioni che
Claude Code aggiunge quando acquista nuove funzioni passano, come chiede la sua guida per i gateway:
un'intestazione `anthropic-*` nuova, o un valore di `anthropic-beta` di forma nuova, passa una volta
letta dal controllo finale, indicata come *solo controllato* nella dashboard, e un'intestazione
`x-claude-code-*` nuova viene tolta. Passano fino a otto intestazioni `anthropic-*` di questo tipo,
ciascuna con un nome di lettere minuscole, cifre e trattini, al massimo 48 dopo `anthropic-` e
nessuna parola tra i suoi trattini più lunga di 16, e un valore da 1 a 1.024 caratteri stampabili;
un segreto o un tuo valore in una di esse viene comunque rifiutato. In una richiesta che Latet riscrive, come quella di un SDK di
Anthropic, vengono rifiutate come ogni altra intestazione che nessun profilo nomina.

Alcune intestazioni vengono invece tolte: quelle di telemetria e di tracing degli SDK, e alcune
degli agenti stessi che possono contenere testo libero.
[Le intestazioni che Latet toglie](#le-intestazioni-che-latet-toglie) le elenca.

**Membri del corpo.** Ogni membro del corpo di una richiesta è letto secondo il suo profilo:
mascherato, verificato dal controllo finale, o tenuto alla forma che gli dà il provider (l'id di un
modello, l'id di una chiamata a uno strumento, un'impostazione). Un membro che nessun profilo nomina
viene rifiutato, dicendo dove si trova (`top-level`, `message content block`, `tool definition`...),
mai il suo nome o il suo valore:

```text
Latet: request body refused (unknown member in top-level). Nothing was sent; run `latet dashboard` to see where it was
```

Un'[eccezione esatta](#approvare-uneccezione-esatta) può nominare un membro del genere con il suo
puntatore JSON, tranne dove nessuna può arrivare: dentro un elemento proprio del provider
(`provider item`: thinking, ragionamento), nella sorgente di un'immagine (`inline image`) e in ogni
frame WebSocket. All'agente vengono dette le stesse parole; la dashboard registra il rifiuto come
`body-unknown-member-no-exception` invece di `body-unknown-member`, e dice *Il corpo conteneva un
campo che nessun profilo di richiesta prevede, dove nessuna eccezione può arrivare (dentro un
elemento del fornitore, la sorgente di un'immagine o un turno WebSocket).*

Una richiesta che Latet si limita a verificare (Claude Code, Bedrock, Vertex) non viene mai riscritta, quindi
lì vale una regola in più: un membro nuovo il cui valore è un testo o un elenco di testi passa una
volta che il controllo finale ne ha letto il nome e il testo, e la dashboard lo mostra come
*solo controllato*. Un membro nuovo annidato, o di altro tipo, viene rifiutato come sopra.
`client_metadata` di Codex, che ripete il percorso e i remote del repository, viene tolto dalla
richiesta prima che sia mascherata.

**Ciò che il provider ha restituito.** Le firme del ragionamento del modello, il ragionamento
cifrato, le compattazioni e i risultati delle ricerche web tornano a ogni turno. Passano perché Latet
li ha visti arrivare dal provider in quella conversazione. Dove non li ha visti (il daemon di un
computer è stato riavviato, per esempio), una regola integrata lascia passare i campi cifrati del
provider, solo nei turni del modello e solo verso l'indirizzo del provider stesso; la dashboard li
mostra come *consentito da una regola*. Una conversazione attraverso un
[gateway aziendale](#conversazioni-attraverso-un-gateway-aziendale) non ha questa regola.

**Immagini e documenti.** Un'immagine viene inviata solo dopo che Latet ne ha letto il testo. Senza il
riconoscimento del testo, una richiesta che contiene un'immagine viene rifiutata, con parole che
dicono di eseguire `latet ocr install` e di iniziare una nuova conversazione, perché l'immagine resta
nella sua cronologia. `images: allow` non invia più un'immagine che Latet non ha letto. PDF e altri
documenti, audio e immagini indicate dal loro indirizzo vengono rifiutati, qualunque cosa dica
`images`. In una richiesta che Latet si limita a verificare (Claude Code, Bedrock, Vertex) ogni byte di
un'immagine deve trovare la sua spiegazione, e una foto del telefono con la posizione, le note del
produttore della fotocamera, una miniatura o una seconda immagine al suo interno (una mappa di
profondità, il video di una foto animata) viene rifiutata: vedi
[Le foto e i loro metadati](../../concepts/images/#le-foto-e-i-loro-metadati). Lì viene rifiutata
anche un'immagine WebP originale: esportala come PNG. Le richieste che Latet riscrive (Codex e gli
altri) inviano le immagini riscritte, senza metadati, e non vedono questi rifiuti.

**Un progetto in cui Latet è disattivato** non invia niente: vedi [sotto](#un-progetto-in-cui-latet-è-disattivato).
**`latet serve --passthrough`** non esiste più: l'opzione resta nascosta per tutta la 0.11, ed esce
subito con un messaggio, così un servizio che la nomina ancora si ferma dove lo vedi. Toglila dal
comando del servizio, o dallo script che avvia Latet.

**Una cartella nominata da una richiesta aggiunge solo protezione:** vedi
[Le cartelle che una richiesta nomina](#le-cartelle-che-una-richiesta-nomina).

## Le intestazioni che Latet toglie

Latet toglie queste intestazioni da una richiesta prima di inviarla, qualunque agente le abbia
inviate:

| Dove | Intestazioni |
| --- | --- |
| Ogni richiesta: telemetria e tracing degli SDK | `x-stainless-*`, `x-datadog-*`, `x-b3-*`, `traceparent`, `tracestate`, `baggage` |
| Claude Code, nel formato di Anthropic (Anthropic, Bedrock, Vertex, un gateway aziendale) | `x-claude-code-agent-id` e `x-claude-code-parent-agent-id`, gli id di un subagente, che possono contenerne il nome; `x-claude-remote-session-id`, `x-claude-remote-container-id`, `x-claude-remote-session-origin` e `x-client-app`, che Claude Code copia dal suo ambiente; `x-claude-code-prev-tool-durations`, un suggerimento per i gateway che nomina gli strumenti eseguiti (tra cui i nomi propri di un server MCP); ogni altra intestazione `x-claude-code-*`, che Latet non nomina ancora |
| Bedrock: l'SDK di AWS | `amz-sdk-*`, `x-amz-user-agent` |
| Gemini: la libreria client di Google | `x-goog-api-client` |
| Codex, nel formato di OpenAI e in quello di ChatGPT | `x-codex-turn-metadata`, che contiene il percorso, i remote e il commit del tuo repository; `x-openai-subagent`, il tipo o un'etichetta libera di un subagente |

Nemmeno le intestazioni `x-latet-*` di Latet raggiungono mai un provider. Una credenziale non viene
mai tolta. In una richiesta che Latet si limita a verificare (Claude Code, Bedrock, Vertex), il corpo
esce byte per byte: queste intestazioni sono l'unico cambiamento. `client_metadata` di Codex, la copia
di `x-codex-turn-metadata` nel corpo, viene tolto dal corpo di una richiesta che Latet riscrive.

Ogni scambio nella dashboard conta le intestazioni e i membri del corpo che gli sono stati tolti.
Quando il provider risponde `401` o `403` a una richiesta da cui ne sono state tolte, all'agente
viene detto, e lo scambio dice che quell'intestazione potrebbe servire al provider:

```text
Latet: the provider answered 401 after Latet removed 2 SDK/tracing headers
```

## Cosa controlla `latet doctor`

Esegui `latet doctor` prima dell'aggiornamento, e di nuovo dopo. Elenca, per tipo e per numero, mai
un valore:

- i progetti in cui Latet è disattivato (le richieste di ciascuno verranno rifiutate);
- il riconoscimento del testo non installato, o disattivato con `ocr: off` (ogni richiesta con
  un'immagine verrà rifiutata);
- `--passthrough` in una voce che avvia Latet all'accesso;
- le chiamate viste nella tua cronologia che nessun profilo copre, per metodo e schema della rotta, mai
  il loro percorso;
- le tue eccezioni permanenti;
- gli agenti avviati da Latet le cui richieste sono arrivate senza la loro chiave di avvio, con
  l'agente e la sua versione.

Il daemon dice lo stesso nel suo log al primo avvio con la 0.11.

## Installare il riconoscimento del testo

```sh
latet ocr install   # circa 31 MB, una volta sola
```

`latet upgrade` aggiunge ciò su cui girano i modelli a un Latet installato senza. In Docker:
`docker exec latet latet ocr install --yes`. Su un server Latet, gli amministratori installano i
modelli su ogni istanza
([Modelli di riconoscimento su un server](../../concepts/server-operations/#modelli-di-riconoscimento-su-un-server)).
Vedi [Screenshot e immagini](../../concepts/images/).

## Le intestazioni che imposti per i tuoi agenti

Un agente configurato con intestazioni tue le invia con ogni richiesta, e la 0.11 le rifiuta finché
non le lasci passare: `ANTHROPIC_CUSTOM_HEADERS` di Claude Code, `http_headers` ed `env_http_headers`
di Codex, `COPILOT_PROVIDER_HEADERS` di Copilot CLI, `options.headers` di OpenCode,
`GEMINI_CLI_CUSTOM_HEADERS` di Gemini CLI. Il rifiuto nomina l'intestazione. Lasciale passare una per
una con un'eccezione esatta, come sotto, nominando la destinazione a cui vanno; la chiave di un
gateway aziendale in un'intestazione si nomina come credenziale, che Latet invia com'è arrivata e
non legge mai. La chiave di avvio di Latet, che `latet claude` e gli altri aggiungono a queste
impostazioni, non va nominata: Latet la toglie prima di inoltrare qualsiasi cosa.

Un'intestazione che imposti per Claude Code il cui nome inizia con `anthropic-` non ha bisogno di
eccezioni: passa una volta letta dal controllo finale, come un'intestazione nuova di Claude Code
stesso ([Intestazioni](#cosa-rifiuta-la-011-che-la-010-inviava)); una il cui nome inizia con
`x-claude-code-` viene tolta. Una credenziale di una destinazione in un'intestazione di questo tipo
si nomina comunque con un'eccezione, così Latet la invia com'è arrivata e non la legge mai.
Un'eccezione che nomina un'intestazione di questo tipo la decide anche dove non si applica: in una
richiesta di un altro progetto, o quando l'eccezione è scaduta, l'intestazione viene rifiutata
(nominandola, o nominando l'eccezione e il giorno in cui è scaduta), mai letta né inviata al posto
dell'eccezione. Togli l'eccezione se l'intestazione deve passare controllata ovunque.

## Approvare un'eccezione esatta

Un'eccezione nomina esattamente ciò che lascia passare, su una destinazione e un profilo di
richiesta: un'intestazione, un parametro di query o un membro del corpo JSON. Per esempio, per far
arrivare ad Anthropic l'intestazione `x-team` di Claude Code:

```sh
latet config add request_exceptions '{destination: anthropic, profile: anthropic.messages.1, components: [{kind: header, name: x-team}], reason: custom-header}'
```

Lo stesso in `config.yaml`:

```yaml
request_exceptions:
  - destination: anthropic           # la rotta di un provider, o una delle tue destinazioni
    profile: anthropic.messages.1    # un metodo, una rotta e un trasporto
    components:
      - {kind: header, name: x-team}                  # verificata: il valore passa il controllo finale
    reason: custom-header
  - destination: llm-acme-com
    profile: openai.chat.1
    components:
      - {kind: header, name: x-acme-key, role: credential}   # inviata com'è arrivata, mai letta
    reason: destination-credential
  - destination: openai
    profile: openai.responses.1
    components:
      - {kind: body, name: /metadata_x/team, type: string}  # un puntatore JSON esatto e il tipo del valore
    reason: other
```

- **Attende la tua approvazione.** Un'eccezione riduce la protezione, quindi si applica solo quando la
  approvi, nella pagina **Approvazioni** della dashboard, in *Impostazioni*. Latet le dà allora un id
  (`x1a2b3c4d5e`) e l'ora dell'approvazione. Toglierne una, o farle nominare meno, si applica subito.
- **Dura 30 giorni** dall'approvazione, per impostazione predefinita, e 90 al massimo (`days`). La
  dashboard avvisa sette giorni prima della fine, e una richiesta rifiutata dopo nomina l'eccezione e
  il giorno in cui è finita. Rinnovarla è una nuova approvazione.
- **Permanente, di proposito.** `permanent: true` ne fa una che non scade mai. Si approva come ogni
  eccezione, è sempre segnata come *permanente* nella dashboard, ed è elencata da `latet doctor` e, su
  un server, da `latet server status`; approvarne una su un server scrive una voce nel registro
  dell'amministrazione, con il suo id e il tipo del suo ambito soltanto.
- **Le richieste di chi.** Un'eccezione senza `project` si applica a ogni tua richiesta, qualunque
  agente la invii, e la dashboard lo dice prima che tu la approvi. Con `project: C:\dev\acme`, si
  applica solo alle richieste di cui Latet considera attendibile il progetto: un agente che ha avviato
  con la sua chiave, o Claude Code con il plugin (vedi
  [Progetti](../../concepts/projects/#in-quale-progetto-è-una-sessione)).
- **Mai in un repository.** Il `latet.yaml` di un progetto non può contenere eccezioni. Al massimo 128,
  32 per un progetto, ciascuna con al massimo 64 componenti; `description` è solo per te, mai in un
  log.
- **Non su un WebSocket.** Nella 0.11.0 un'eccezione non si applica a un WebSocket: Codex con l'accesso
  ChatGPT, che ne usa uno, non può inviare un'intestazione tua. Per un rifiuto lì la dashboard non ne
  propone nessuna, e dice *In questa versione di Latet un WebSocket non accetta eccezioni alle
  richieste.*
- **Dopo un aggiornamento.** Una versione successiva che cambia una parte di un profilo nominata da
  un'eccezione sospende quell'eccezione finché non la approvi di nuovo; la dashboard e `latet doctor`
  dicono che va approvata di nuovo (*Da approvare di nuovo: il suo profilo è stato rivisto*), anche
  quelle permanenti; su un server, `latet server status` la elenca appena lo spazio dell'identità è
  partito con la nuova versione. Un aggiornamento non allarga mai ciò che un'eccezione lascia passare.

Il profilo è il tipo di chiamata. Un'eccezione nomina un solo profilo, quindi un'intestazione che un
agente invia con ogni chiamata richiede un'eccezione per ogni tipo di chiamata che fa:

| Chiamate | Profilo |
| --- | --- |
| Anthropic Messages (Claude Code, l'SDK di Anthropic), conteggio dei token, l'elenco dei modelli e un modello | `anthropic.messages.1`, `anthropic.count_tokens.1`, `anthropic.models.1`, `anthropic.model.1` |
| Il formato di OpenAI (OpenAI, OpenRouter, un gateway aziendale): Chat Completions, i vecchi completions, Responses via HTTP, embedding, l'elenco dei modelli e un modello | `openai.chat.1`, `openai.completions.1`, `openai.responses.1`, `openai.embeddings.1`, `openai.models.1`, `openai.model.1` |
| Codex con l'accesso ChatGPT, via HTTP: Responses e l'elenco dei modelli | `chatgpt.codex.responses.1`, `chatgpt.codex.models.1` |
| Gemini, sulla Gemini API o su Vertex AI: generazione, streaming, conteggio, embedding (uno o in blocco), l'elenco dei modelli e un modello, creazione o modifica di una cache, la chat e gli embedding compatibili con OpenAI | `gemini.<version>.generateContent.1`, `gemini.<version>.streamGenerateContent.1`, `gemini.<version>.countTokens.1`, `gemini.<version>.embedContent.1`, `gemini.<version>.batchEmbedContents.1`, `gemini.<version>.models.1`, `gemini.<version>.model.1`, `gemini.<version>.cache-create.1`, `gemini.<version>.cache-patch.1`, `gemini.<version>.openai.completions.1`, `gemini.<version>.openai.embeddings.1` |
| Bedrock: invocazione, streaming, conteggio dei token, i profili di inferenza e uno di essi | `bedrock.invoke.1`, `bedrock.invoke-with-response-stream.1`, `bedrock.count-tokens.1`, `bedrock.inference-profiles.1`, `bedrock.inference-profile.1` |
| Claude su Vertex AI | `vertex.anthropic.rawPredict.1`, `vertex.anthropic.streamRawPredict.1` |
| Un gateway aziendale davanti a Bedrock o a Claude su Vertex AI | `company.bedrock.invoke.1`, `company.bedrock.invoke-with-response-stream.1`, `company.bedrock.count-tokens.1`, `company.vertex.rawPredict.1`, `company.vertex.streamRawPredict.1` |
| Destinazioni Azure OpenAI | `azure-openai.chat.1`, `azure-openai.completions.1`, `azure-openai.responses.1`, `azure-openai.embeddings.1` |
| Destinazioni Microsoft Foundry | `foundry.chat.1`, `foundry.embeddings.1` |

In un id di Gemini, `<version>` è quella che nomina il percorso della richiesta: `v1` o `v1beta`
sulla Gemini API, `v1` o `v1beta1` su Vertex AI (`gemini.v1beta.generateContent.1`).

Su un server Latet, le eccezioni fanno parte delle impostazioni di ciascuna persona, che le approva
nella dashboard; quelle di una chiave API si impostano tramite l'amministrazione.

## Bedrock e Vertex: cosa elencare

Un id di modello scritto nella forma documentata da un provider passa da sé, in un percorso come in
un corpo, e così un profilo di inferenza di Bedrock definito da Amazon (`us.anthropic.claude-…`,
`eu.`, `apac.`, `global.`). Ciò che appartiene al tuo account o al tuo progetto cloud va elencato, in
`request_resources`, prima che una richiesta che lo nomina venga inviata: ogni ARN in un percorso di
Bedrock (compreso l'ARN di un profilo di inferenza applicativo) e il progetto di Vertex. Ogni riga
nomina la destinazione, il suo indirizzo, il ruolo del valore e il valore:

```yaml
request_resources:
  - destination: bedrock
    url: https://bedrock-runtime.us-east-1.amazonaws.com    # chiamate al modello; le letture vanno a https://bedrock.us-east-1.amazonaws.com
    role: model                                             # inference-profile, per leggerne uno
    value: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6
  - destination: vertex
    url: https://us-east5-aiplatform.googleapis.com
    role: project
    value: acme-ml-prod
```

`latet config add request_resources '{...}'` ne aggiunge una. Una riga lascia passare esattamente
quel valore verso quella destinazione e nient'altro, e si applica subito. Finché non c'è, una
richiesta del genere viene rifiutata con `envelope-resource`. Un id di modello fuori dalle forme
documentate (i nomi dei modelli di un gateway aziendale) si elenca allo stesso modo, con il ruolo
`model`.

Su OpenRouter un modello è nominato con il suo fornitore. Quelli di Anthropic, OpenAI e Google
passano da soli quando la parte dopo il fornitore è nella forma documentata da quel provider, con o
senza una variante come `:free` o `:thinking` (`anthropic/claude-sonnet-4.5`, `openai/gpt-4o`,
`google/gemini-2.5-pro`), e così `openrouter/auto`. Il modello di ogni altro fornitore
(`meta-llama/…`, `deepseek/…`) si elenca con la destinazione `openrouter`, l'indirizzo
`https://openrouter.ai` e il ruolo `model`.

## Un progetto in cui Latet è disattivato

Nella 0.10, un progetto in cui Latet era disattivato inviava le sue richieste senza mascherarle.
Nella 0.11 Latet non ne invia nessuna, via HTTP o WebSocket, che il progetto venga dall'avvio
dell'agente, dal plugin o da una cartella nominata dalla richiesta. All'agente viene detto:

```text
Latet is off in this project: Latet sends no request from there; turn it on for the project (`latet project on` in its folder, or the project's switch in the dashboard), or reach the provider directly for this folder by starting the agent without Latet's address. Nothing was sent; run `latet dashboard` to see where it was
```

Quando il progetto è solo nominato dalla richiesta (dichiarato, sotto), le parole iniziano con
`request claimed a project where Latet is off:` e proseguono allo stesso modo.

`latet project off` e l'interruttore della dashboard lo dicono prima di disattivare un progetto. Per
lavorare in quella cartella senza Latet, avvia lì l'agente senza l'indirizzo di Latet: un agente che
avvii con `latet claude`, `latet codex` o un altro comando `latet` si avvia da solo (`claude`,
`codex`); uno le cui impostazioni puntano a Latet (dopo `latet global-install`) passa da Latet
ovunque si avvii, quindi dagli per quella sessione l'indirizzo del suo provider, come dice la sua
documentazione, oppure usa `latet agents revert`. Latet non vede allora niente di ciò che invia. Se
avevi disattivato Latet per lasciar passare i dati personali, *Solo segreti* fa proprio questo e
continua a proteggere i segreti
([Progetti](../../concepts/projects/#solo-segreti-o-latet-disattivato-in-un-progetto)).

## Le cartelle che una richiesta nomina

Una richiesta può nominare la sua cartella: l'intestazione `x-latet-project`, o la cartella di lavoro
che Codex, OpenCode e Gemini CLI scrivono nelle loro richieste. Nella 0.11 una cartella così è
*dichiarata*: le regole del suo progetto che aggiungono protezione si applicano, i suoi allentamenti
no (valori da non mascherare mai, regole dei segreti disattivate, un profilo più debole, `data_tools`,
le indicazioni e le immagini per il revisore locale). Un progetto dichiarato in cui Latet è
disattivato viene rifiutato, come sopra. Un progetto è *attendibile* quando Latet lo conosce da sé:
dalla chiave di avvio di un agente che ha avviato, o dal plugin di Claude Code. La dashboard mostra
per ogni richiesta se il progetto era attendibile o dichiarato, e come è stato deciso il suo avvio.
Vedi [Progetti](../../concepts/projects/#in-quale-progetto-è-una-sessione).

Cosa vuol dire per ogni agente:

- **Avviato da Latet** (`latet claude`, `latet codex`, `latet opencode`, `latet gemini`,
  `latet copilot`): attendibile grazie alla sua chiave di avvio, dove l'agente la porta. Codex sui suoi
  provider integrati (con l'accesso ChatGPT, o il provider di OpenAI), Codex prima della 0.115,
  OpenCode prima della 1.19.5, Gemini CLI in modalità ACP, Copilot CLI fuori da Chat Completions e i programmi avviati da `latet run` non ne portano: il comando che li
  avvia lo dice in una frase, e i loro progetti sono dichiarati.
- **Claude Code con il plugin**, configurato in modo permanente: attendibile grazie al plugin.
- **Ogni altro agente configurato in modo permanente**: dichiarato. Le tue regole e ogni eccezione
  senza `project` si applicano comunque.

## Conversazioni attraverso un gateway aziendale

Claude Code attraverso LiteLLM o un altro gateway aziendale che inoltra le firme del ragionamento fa
affidamento sul fatto che Latet abbia visto tornare quelle firme. La regola integrata per i campi
cifrati del provider vale solo per l'indirizzo del provider stesso, mai per una rotta aziendale.
Dopo un riavvio del daemon, o su un server quando i dati della conversazione non ci sono più, una
conversazione del genere viene rifiutata al turno successivo: iniziane una nuova.

## Tornare alla 0.10

```sh
latet upgrade --to 0.10.4
```

Tornando alla versione con cui è stata fatta una copia dei tuoi dati, ti viene proposto di
rimettere quella copia ([Aggiornamento](../installation/#aggiornamento)). Latet 0.10 ignora
`request_exceptions` e `request_resources`, e invia di nuovo ciò che la 0.11 rifiuta: le parti non
lette delle richieste, le richieste dei progetti in cui Latet è disattivato e le immagini che non sa
leggere. Torna indietro solo sapendolo.

Un'impostazione è sconosciuta alla 0.10: `request_profile`, su una destinazione in `upstreams`. La
0.11 la scrive solo per una destinazione per cui hai scelto un profilo, in `config.yaml` e in
`config.reviewed.json`, le impostazioni che hai approvato per ultime, nella cartella dei dati di Latet.
La 0.10 non si avvia se è in `config.yaml`, e legge un `config.reviewed.json` che la contiene come se
non ci fossero impostazioni approvate: una modifica a `config.yaml` si applicherebbe allora senza
attendere la tua approvazione. Se ne hai scelto uno, rimetti la copia che l'aggiornamento alla 0.11 ha
fatto, che contiene i due file com'erano prima: `latet upgrade --to` la propone mentre torna indietro
(si trova nella cartella dei dati, in `backups/before-<versione>-<ora>`, dal nome della versione
installata, per esempio `before-0.12.1-…`). Senza quella copia (la 0.11
installata con l'installer, che non ne fa), togli la chiave da entrambi i file prima di tornare
indietro: le righe `request_profile:` da `config.yaml`, e ogni membro `"request_profile": "…"` da
`config.reviewed.json`, lasciandolo un JSON valido.

Su un server Latet, la 0.11 aggiunge tabelle al database, e un'istanza rifiuta un database più
recente di lei: tornare indietro vuol dire usare il backup del database fatto prima
dell'aggiornamento, con tutte le istanze ferme
([Aggiornare e aggiungere istanze](../../concepts/server-operations/#aggiornare-e-aggiungere-istanze)).
