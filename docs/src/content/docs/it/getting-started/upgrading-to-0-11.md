---
title: Passare alla 0.11
description: Cosa rifiuta Latet 0.11 che la 0.10 inviava, cosa controllare prima dell'aggiornamento e come tornare indietro.
---

Latet 0.11.0 legge ogni parte di una richiesta prima di inviarla: il percorso, i parametri di query,
gli header e il corpo. Una richiesta con una parte che Latet non ispeziona né lascia passare per
una regola o un'eccezione che hai approvato non viene inviata. Un corpo vuoto non basta più per far
passare una richiesta. Le credenziali che servono al tuo provider, e le parti che un provider
firma, seguono regole proprie. Vale allo stesso modo sul tuo computer e su un server di Latet.

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

Un'eccezione nomina un header, un parametro di query o un membro del corpo, mai un percorso
intero: nella 0.11.0 nessuna eccezione fa passare una di queste chiamate. Cambia le impostazioni
dell'agente perché non faccia la chiamata, oppure falla senza Latet, all'indirizzo del provider,
sapendo che Latet non vede ciò che invia.

**Parametri di query.** Un parametro di query che nessun profilo nomina viene rifiutato, e
all'agente viene detto il suo nome, mai il suo valore:

```text
Latet: request envelope refused (envelope-query-unknown): query parameter drift. Nothing was sent; run `latet dashboard` to see where it was
```

**Header.** Ogni header è letto con una regola propria: una credenziale viene inviata com'è
arrivata e mai scritta nei log; un header proprio di un agente deve avere la forma che quell'agente
gli dà (un id di sessione deve essere un UUID, per esempio); un header che può contenere testo
libero, come `user-agent`, passa il controllo finale. Un header che nessun profilo nomina viene
rifiutato, e all'agente viene detto il suo nome, mai il suo valore:

```text
Latet: request envelope refused (envelope-header-unknown): header x-team. Nothing was sent; run `latet dashboard` to see where it was
```

In una richiesta che Latet si limita a verificare (Claude Code, Bedrock, Vertex), gli header che
Claude Code aggiunge quando acquista nuove funzioni passano, come chiede la sua guida per i gateway:
un header `anthropic-*` nuovo, o un valore di `anthropic-beta` di forma nuova, passa una volta letto
dal controllo finale, indicato come *solo controllato* nella dashboard, e un header
`x-claude-code-*` nuovo viene tolto. Passano fino a otto header `anthropic-*` di questo tipo,
ciascuno con un nome di lettere minuscole, cifre e trattini, al massimo 48 dopo `anthropic-` e
nessuna parola tra i suoi trattini più lunga di 16, e un valore da 1 a 1.024 caratteri stampabili;
un segreto o un tuo valore in uno di essi viene comunque rifiutato. In una richiesta che Latet riscrive, come quella di un SDK di
Anthropic, vengono rifiutati come ogni altro header che nessun profilo nomina.

Alcuni header vengono invece tolti: quelli di telemetria e di tracing degli SDK, e alcuni degli
agenti stessi che possono contenere testo libero.
[Gli header che Latet toglie](#gli-header-che-latet-toglie) li elenca.

**Membri del corpo.** Ogni membro del corpo di una richiesta è letto secondo il suo profilo:
mascherato, verificato dal controllo finale, o vincolato alla forma che gli dà il provider (l'id di un
modello, l'id di una chiamata a uno strumento, un'impostazione). Nella 0.11 un membro che nessun
profilo nominava veniva rifiutato, dicendo dove si trova (`top-level`, `message content block`,
`tool definition`...), mai il suo nome o il suo valore:

```text
Latet: request body refused (unknown member in top-level). Nothing was sent; run `latet dashboard` to see where it was
```

Dalla 0.15.0 un membro del genere viene letto per intero e controllato, e passa così com'è quando
nulla al suo interno andrebbe mascherato e non indica nulla su cui il provider agisce: vedi
[Nuovi membri delle richieste](#nuovi-membri-delle-richieste). Viene ancora rifiutato, con le stesse
parole, quando indica un file, un indirizzo, una conversazione salvata o un modello, contiene dati
codificati o porta più di brevi token dentro un elemento proprio del provider; e con
`unknown_members: refuse` viene rifiutato come nella 0.11.

Un'[eccezione esatta](#approvare-uneccezione-esatta) può nominare un membro rifiutato con il suo
puntatore JSON, tranne dove nessuna può arrivare: dentro un elemento proprio del provider
(`provider item`: thinking, reasoning), nella sorgente di un'immagine (`inline image`) e in ogni
frame WebSocket. All'agente vengono dette le stesse parole; la dashboard registra il rifiuto come
`body-unknown-member-no-exception` invece di `body-unknown-member`, e dice *Il corpo conteneva un
membro che nessun profilo di richiesta prevede, dove nessuna eccezione può arrivare (dentro un
elemento di un provider, la sorgente di un'immagine o un turno WebSocket).*

Una richiesta che Latet si limita a verificare (Claude Code, Bedrock, Vertex) non viene mai riscritta, quindi
lì vale una regola in più: un membro nuovo il cui valore è un testo o un elenco di testi passa una
volta che il controllo finale ne ha letto il nome e il testo, e la dashboard lo mostra come
*solo controllato*. Un membro nuovo annidato, o di altro tipo, viene letto come dice
[Nuovi membri delle richieste](#nuovi-membri-delle-richieste).
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
**`latet serve --passthrough`** non esiste più: l'opzione resta nascosta per tutta la 0.11, e fa
terminare subito il comando con un messaggio, così un servizio che la nomina ancora si ferma dove lo vedi. Toglila dal
comando del servizio, o dallo script che avvia Latet.

**Una cartella nominata da una richiesta aggiunge solo protezione:** vedi
[Le cartelle che una richiesta nomina](#le-cartelle-che-una-richiesta-nomina).

## Gli header che Latet toglie

Latet toglie questi header da una richiesta prima di inviarla, qualunque agente li abbia inviati:

| Dove | Header |
| --- | --- |
| Ogni richiesta: telemetria e tracing degli SDK | `x-stainless-*`, `x-datadog-*`, `x-b3-*`, `traceparent`, `tracestate`, `baggage` |
| Claude Code, nel formato di Anthropic (Anthropic, Bedrock, Vertex, un gateway aziendale) | `x-claude-code-agent-id` e `x-claude-code-parent-agent-id`, gli id di un subagente, che possono contenerne il nome; `x-claude-remote-session-id`, `x-claude-remote-container-id`, `x-claude-remote-session-origin` e `x-client-app`, che Claude Code copia dal suo ambiente; `x-claude-code-prev-tool-durations`, un suggerimento per i gateway che nomina gli strumenti eseguiti (tra cui i nomi propri di un server MCP); ogni altro header `x-claude-code-*`, che Latet non nomina ancora |
| Bedrock: l'SDK di AWS | `amz-sdk-*`, `x-amz-user-agent` |
| Gemini: la libreria client di Google | `x-goog-api-client` |
| Codex, nel formato di OpenAI e in quello di ChatGPT | `x-codex-turn-metadata`, che contiene il percorso, i remote e il commit del tuo repository; `x-openai-subagent`, il tipo o un'etichetta libera di un subagente |

Nemmeno gli header `x-latet-*` di Latet raggiungono mai un provider. Una credenziale non viene
mai tolta. In una richiesta che Latet si limita a verificare (Claude Code, Bedrock, Vertex), il corpo
esce byte per byte: questi header sono l'unico cambiamento. `client_metadata` di Codex, la copia
di `x-codex-turn-metadata` nel corpo, viene tolto dal corpo di una richiesta che Latet riscrive.

Ogni scambio nella dashboard conta gli header e i membri del corpo che gli sono stati tolti.
Quando il provider risponde `401` o `403` a una richiesta da cui ne sono stati tolti, l'agente ne
viene informato, e lo scambio dice che quell'header potrebbe servire al provider:

```text
Latet: the provider answered 401 after Latet removed 2 SDK/tracing headers
```

## Nuovi membri delle richieste

Una nuova versione di un agente aggiunge spesso un membro alle sue richieste: una modalità, un
flag, un'impostazione tutta sua. Prima della 0.15.0 Latet rifiutava una richiesta con un membro che
nessun profilo di richiesta nomina, a meno che, in una richiesta che si limita a verificare, il
membro contenesse solo testo: l'agente si fermava finché una versione di Latet non lo nominava.
Dalla 0.15.0 Latet legge un membro del genere per intero, ogni nome e ogni valore al suo interno, lo
controlla con lo stesso rigore di un testo che maschera, e lo invia così com'è quando nulla al suo
interno andrebbe mascherato e non indica nulla su cui il provider agisce. È
`unknown_members: check`, il valore predefinito, in ogni richiesta: in quelle che Latet si limita a
verificare (Claude Code, Bedrock, Vertex) e in quelle che riscrive (Codex, OpenCode, Gemini CLI,
Hermes Agent, gli SDK). Un membro del genere non viene mai mascherato: esce così com'è, oppure la
richiesta si ferma.

**Cosa passa:** un membro fatto di valori di controllo, come
`{"mode": "compact", "level": 2, "on": true}`: una modalità, un flag, un numero, un breve token, o
un oggetto o un elenco di questi.

**Cosa ferma ancora la richiesta,** con le parole e i codici della 0.11, ovunque si trovi nel
membro:

- un indirizzo web o qualsiasi altro URI, compreso uno `data:`, o l'id di un file, di una
  conversazione, di una risposta salvata o di un'altra risorsa che il provider conserva (`file-…`,
  `conv_…`, `resp_…`, `gs://…`);
- un nome che dice che contiene qualcosa su cui il provider agirebbe, come un file, un allegato,
  un'immagine, un documento, un indirizzo web, un server, un server MCP, un container, una
  conversazione, una cache o un modello;
- dati codificati: una sequenza di 64 o più caratteri base64 che mescola cifre, maiuscole e
  minuscole, o di 48 cifre esadecimali;
- un numero non finito, o più di quanto Latet legge di una richiesta;
- più di quanto Latet legge dei membri nuovi di una richiesta: un nome di più di 256 caratteri, più
  di 4.096 testi diversi in tutto (ogni nome e ogni valore, e un valore una volta in più per ogni
  campo in cui si trova), più di 256 KiB di questi testi, o membri che il modello locale dei nomi
  non riesce a leggere nei 5 secondi che ha per un documento;
- dentro un elemento proprio del provider (thinking, reasoning, i risultati di una ricerca web, la
  sorgente di un'immagine), qualsiasi cosa che non sia un breve token: testo libero, una chiave o un
  valore di più di 64 lettere, cifre, `_`, `.`, `:` o `-`, o più di 32 valori o 2.048 byte in tutto;
- un valore sensibile: tutto ciò che le tue regole maschererebbero o oscurerebbero in un testo (un
  segreto, un nome, un indirizzo e-mail, un numero di telefono, un valore che una delle tue regole
  di campo indica, per la sua chiave o il suo percorso), o un valore che la tua cassaforte conserva.
  La richiesta si ferma come per un valore del genere in qualsiasi altro testo, e la dashboard
  indica il tipo del valore, *Nuovo membro della richiesta*, mai il nome del membro.

Un testo di un membro del genere già uscito passa di nuovo nella stessa conversazione, come il testo
già inviato, anche dopo che Latet ha imparato un valore che contiene: una conversazione non resta mai
bloccata per questo. Nella dashboard, uno scambio che ne ha fatto passare uno elenca *un campo che
nessun profilo di richiesta prevede ancora, letto e controllato* tra ciò che lo ha lasciato passare.
Il nome di un membro non viene mai registrato, scritto nei log o mostrato.

### Rifiutarli di nuovo

`unknown_members: refuse` rifiuta un membro del genere come faceva Latet prima della 0.15.0: una
richiesta che ne ha uno viene rifiutata, a meno che, in una richiesta che Latet si limita a
verificare, il membro contenga solo testo. In `config.yaml`:

```yaml
unknown_members: refuse
```

oppure nella dashboard, sotto *Nuovi membri delle richieste* nei dati di produzione di **Regole**.
Il passaggio a `refuse` si applica subito. Il ritorno a `check` abbassa la protezione: fatto in
`config.yaml`, o da un agente, attende la tua approvazione in **Approvazioni**; fatto nella
dashboard, il salvataggio è la tua approvazione. Su un server Latet le cui [approvazioni
centrali](../../enterprise/approvals/) (Latet Enterprise) mandano queste modifiche al team di
sicurezza della tua azienda, attende invece la sua decisione, anche quando è fatto nella dashboard.
Dove le direttive della tua azienda bloccano `unknown_members: refuse`, `refuse` resta in vigore
qualunque cosa dicano le tue impostazioni, e la dashboard offre solo quello. Una richiesta rifiutata
con `refuse` dice nella dashboard che `unknown_members: refuse`, nelle tue impostazioni, in quelle
del progetto o nelle direttive della tua azienda, rifiuta membri del genere, e cosa farebbe `check`,
con un collegamento a questa sezione; dove un'eccezione esatta può arrivare al membro, dice ancora
come approvarne una, perché un'eccezione viene letta prima di questa impostazione.

- **Un progetto può solo rifiutarli.** `unknown_members: refuse` nelle tue impostazioni per un
  progetto (la sua voce sotto `projects` in `config.yaml`) o nel `latet.yaml` del suo repository li
  rifiuta solo in quel progetto. `check` spetta solo alle tue impostazioni: la voce di un progetto in
  `config.yaml` accetta solo `refuse` (un `check` scritto lì a mano viene lasciato fuori, e
  `latet reload` e il log del daemon dicono quale voce), e un `latet.yaml` che dice `check` viene
  elencato sotto *Cosa non viene applicato*.
- **Il `refuse` di un repository vincola un progetto attendibile o dichiarato.** Come ogni regola di
  un progetto che aggiunge protezione, si applica alle richieste che Latet colloca in quel progetto,
  sia che lo consideri attendibile sia che una richiesta lo dichiari: un client che nomina un'altra
  cartella non ne è vincolato, a meno che la sua chiave di avvio non lo leghi al progetto (vedi
  [Le cartelle che una richiesta nomina](#le-cartelle-che-una-richiesta-nomina)).
- **Su un server Latet**, ogni persona e ogni chiave API ha un valore proprio, `check` a meno che non
  imposti `refuse`, e `latet server status` dice il valore in vigore (`new request members: …`). Con
  Latet Enterprise, le direttive dell'azienda possono bloccare `unknown_members: refuse` per ogni persona e
  chiave API ([Regole bloccate](../../enterprise/locked-rules/)); come ogni regola bloccata, resta in
  vigore quando la licenza finisce.
- **`latet doctor`** dice il valore in vigore, sotto *New request members*, e, finché `config.yaml`
  non contiene `unknown_members`, che `check` è il valore predefinito e come rifiutarli di nuovo.

### Cosa non copre

- **Il revisore locale**, il modello linguistico che legge i testi per categorie, non legge questi
  membri: lo fanno i rilevatori di Latet e la tua cassaforte.
- **La modalità dati rigorosa** non legge questi membri: copre gli output degli strumenti, i file e
  gli host che indica, e un membro nuovo non è nessuno di questi. Le tue regole di campo li leggono,
  per la chiave o il percorso di un valore, e così i rilevatori di Latet.
- **I dati codificati non vengono decodificati:** un segreto in base64 più corto delle sequenze
  indicate sopra, o scritto in una forma tutta sua, viene letto come il testo che è, come in ogni
  testo che Latet controlla.
- **In una richiesta che Latet riscrive,** un valore sensibile in un membro del genere ferma la
  richiesta invece di uscire come un valore fittizio, ogni volta che viene inviata di nuovo, a meno
  che non sia già uscito in quella conversazione. Un client che copia in un membro nuovo i valori che Latet ha
  ripristinato si ferma finché una versione di Latet non nomina quel membro.
- **In una richiesta che Latet si limita a verificare,** un membro nuovo il cui valore è un testo o
  un elenco di testi, fuori da un elemento del provider, mantiene la regola della 0.11 (*solo
  controllato*): il controllo finale vi ferma i segreti, i tuoi termini, la tua identità e i dati
  personali, ma non cerca nomi e non applica né le tue regole di campo né la modalità dati rigorosa,
  così che nulla di ciò che partiva prima della 0.15.0 venga rifiutato ora.
- **Il `refuse` di un repository** vincola solo le richieste che Latet colloca nel suo progetto, come
  sopra.
- **L'aggiornamento ha portato ogni installazione a `check`**, server compresi, senza
  un'approvazione: se contavi sul rifiuto, imposta `unknown_members: refuse`.

## Cosa controlla `latet doctor`

Esegui `latet doctor` prima dell'aggiornamento, e di nuovo dopo. Elenca, per tipo e per numero, mai
un valore:

- i progetti in cui Latet è disattivato (le richieste di ciascuno verranno rifiutate);
- il riconoscimento del testo non installato, o disattivato con `ocr: off` (ogni richiesta con
  un'immagine verrà rifiutata);
- `--passthrough` in una voce che avvia Latet all'accesso;
- le chiamate viste nella tua cronologia che nessun profilo copre, per metodo e modello di percorso,
  mai il loro percorso;
- le tue eccezioni permanenti;
- gli agenti avviati da Latet le cui richieste sono arrivate senza la loro chiave di avvio, con
  l'agente e la sua versione.

Il daemon dice lo stesso nel suo log al primo avvio con la 0.11.

## Installare il riconoscimento del testo

```sh
latet ocr install   # circa 31 MB, una volta sola
```

`latet upgrade` aggiunge ciò su cui girano i modelli a un Latet installato senza. In Docker:
`docker exec latet latet ocr install --yes`. Su un server di Latet, gli amministratori installano i
modelli su ogni istanza
([Modelli di riconoscimento su un server](../../concepts/server-operations/#modelli-di-riconoscimento-su-un-server)).
Vedi [Screenshot e immagini](../../concepts/images/).

## Gli header che imposti per i tuoi agenti

Un agente configurato con header tuoi li invia con ogni richiesta, e la 0.11 li rifiuta finché non
li lasci passare: `ANTHROPIC_CUSTOM_HEADERS` di Claude Code, `http_headers` ed `env_http_headers`
di Codex, `COPILOT_PROVIDER_HEADERS` di Copilot CLI, `options.headers` di OpenCode,
`GEMINI_CLI_CUSTOM_HEADERS` di Gemini CLI. Il rifiuto nomina l'header. Lasciali passare uno per uno
con un'eccezione esatta, come sotto, nominando la destinazione a cui vanno; la chiave di un gateway
aziendale in un header si nomina come credenziale, che Latet invia com'è arrivata e non legge mai.
La chiave di avvio di Latet, che `latet claude` e gli altri aggiungono a queste impostazioni, non
spetta a te nominarla: Latet la toglie prima di inoltrare qualsiasi cosa.

Un header che imposti per Claude Code il cui nome inizia con `anthropic-` non ha bisogno di
eccezioni: passa una volta letto dal controllo finale, come un header nuovo di Claude Code stesso
([Header](#cosa-rifiuta-la-011-che-la-010-inviava)); uno il cui nome inizia con `x-claude-code-`
viene tolto. Una credenziale di una destinazione in un header di questo tipo si nomina comunque con
un'eccezione, così Latet la invia com'è arrivata e non la legge mai. Un'eccezione che nomina un
header di questo tipo lo decide anche dove non si applica: in una richiesta di un altro progetto, o
quando l'eccezione è scaduta, l'header viene rifiutato (nominandolo, o nominando l'eccezione e il
giorno in cui è scaduta), e mai letto e poi inviato al posto dell'eccezione. Togli l'eccezione se
l'header deve passare ovunque dopo il controllo.

## Approvare un'eccezione esatta

Un'eccezione nomina esattamente ciò che lascia passare, su una destinazione e un profilo di
richiesta: un header, un parametro di query o un membro del corpo JSON. Per esempio, per far
arrivare ad Anthropic l'header `x-team` di Claude Code:

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
  approvi, nella pagina **Approvazioni** della dashboard, nella scheda *Impostazioni*. Latet le dà allora un id
  (`x1a2b3c4d5e`) e l'ora dell'approvazione. Toglierne una, o farle nominare meno, si applica subito.
- **Dura 30 giorni** dall'approvazione, per impostazione predefinita, e 90 al massimo (`days`). La
  dashboard avvisa sette giorni prima della scadenza, e una richiesta rifiutata dopo nomina
  l'eccezione e il giorno in cui è scaduta. Rinnovarla è una nuova approvazione.
- **Permanente, di proposito.** `permanent: true` ne fa una che non scade mai. Si approva come ogni
  eccezione, è sempre segnata come *permanente* nella dashboard, ed è elencata da `latet doctor` e, su
  un server, da `latet server status`; approvarne una su un server scrive una voce nel registro
  amministrativo, con il suo id e il tipo del suo ambito soltanto.
- **Di chi sono le richieste.** Un'eccezione senza `project` si applica a ogni tua richiesta, qualunque
  agente la invii, e la dashboard lo dice prima che tu la approvi. Con `project: C:\dev\acme`, si
  applica solo alle richieste di cui Latet considera attendibile il progetto: un agente che ha avviato
  con la sua chiave, o Claude Code con il plugin (vedi
  [Progetti](../../concepts/projects/#in-quale-progetto-è-una-sessione)).
- **Mai in un repository.** Il `latet.yaml` di un progetto non può contenere eccezioni. Al massimo 128,
  32 per un progetto, ciascuna con al massimo 64 componenti; `description` è solo per te, mai in un
  log.
- **Non su un WebSocket.** Nella 0.11.0 un'eccezione non si applica a un WebSocket: Codex con l'accesso
  ChatGPT, che ne usa uno, non può inviare un header tuo. Per un rifiuto lì la dashboard non ne
  propone nessuna, e dice *In questa versione di Latet un WebSocket non accetta eccezioni di
  richiesta.*
- **Dopo un aggiornamento.** Una versione successiva che cambia una parte di un profilo nominata da
  un'eccezione sospende quell'eccezione finché non la approvi di nuovo; la dashboard e `latet doctor`
  dicono che va approvata di nuovo (*Da approvare di nuovo: il suo profilo è stato rivisto*), anche
  quelle permanenti; su un server, `latet server status` la elenca appena lo spazio dell'identità è
  partito con la nuova versione. Un aggiornamento non allarga mai ciò che un'eccezione lascia passare.

Il profilo è il tipo di chiamata. Un'eccezione nomina un solo profilo, quindi un header che un
agente invia con ogni chiamata richiede un'eccezione per ogni tipo di chiamata che fa:

| Chiamate | Profilo |
| --- | --- |
| Anthropic Messages (Claude Code, l'SDK di Anthropic), conteggio dei token, l'elenco dei modelli, un modello e la chiamata che apre la connessione di Claude Code all'avvio (un HEAD senza corpo) | `anthropic.messages.1`, `anthropic.count_tokens.1`, `anthropic.models.1`, `anthropic.model.1`, `anthropic.hello.1` |
| Il formato di OpenAI (OpenAI, OpenRouter, un gateway aziendale): Chat Completions, la vecchia API Completions, Responses via HTTP, embedding, l'elenco dei modelli e un modello | `openai.chat.1`, `openai.completions.1`, `openai.responses.1`, `openai.embeddings.1`, `openai.models.1`, `openai.model.1` |
| Codex con l'accesso ChatGPT, via HTTP: Responses e l'elenco dei modelli | `chatgpt.codex.responses.1`, `chatgpt.codex.models.1` |
| Gemini, sulla Gemini API o su Vertex AI: generazione, streaming, conteggio, embedding (uno o in blocco), l'elenco dei modelli e un modello, creazione o modifica di una cache, la chat e gli embedding compatibili con OpenAI | `gemini.<version>.generateContent.1`, `gemini.<version>.streamGenerateContent.1`, `gemini.<version>.countTokens.1`, `gemini.<version>.embedContent.1`, `gemini.<version>.batchEmbedContents.1`, `gemini.<version>.models.1`, `gemini.<version>.model.1`, `gemini.<version>.cache-create.1`, `gemini.<version>.cache-patch.1`, `gemini.<version>.openai.completions.1`, `gemini.<version>.openai.embeddings.1` |
| Bedrock: invocazione, streaming, conteggio dei token, i profili di inferenza e uno di essi | `bedrock.invoke.1`, `bedrock.invoke-with-response-stream.1`, `bedrock.count-tokens.1`, `bedrock.inference-profiles.1`, `bedrock.inference-profile.1` |
| Claude su Vertex AI | `vertex.anthropic.rawPredict.1`, `vertex.anthropic.streamRawPredict.1` |
| Un gateway aziendale davanti a Bedrock o a Claude su Vertex AI | `company.bedrock.invoke.1`, `company.bedrock.invoke-with-response-stream.1`, `company.bedrock.count-tokens.1`, `company.vertex.rawPredict.1`, `company.vertex.streamRawPredict.1` |
| Destinazioni Azure OpenAI | `azure-openai.chat.1`, `azure-openai.completions.1`, `azure-openai.responses.1`, `azure-openai.embeddings.1` |
| Destinazioni Microsoft Foundry | `foundry.chat.1`, `foundry.embeddings.1` |

In un id di Gemini, `<version>` è quella che nomina il percorso della richiesta: `v1` o `v1beta`
sulla Gemini API, `v1` o `v1beta1` su Vertex AI (`gemini.v1beta.generateContent.1`).

Su un server di Latet, le eccezioni fanno parte delle impostazioni di ciascuna persona, che le approva
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

Su OpenRouter un modello è nominato con il suo produttore. Quelli di Anthropic, OpenAI e Google
passano da soli quando la parte dopo il produttore è nella forma documentata da quel provider, con o
senza una variante come `:free` o `:thinking` (`anthropic/claude-sonnet-4.5`, `openai/gpt-4o`,
`google/gemini-2.5-pro`), e così `openrouter/auto`. Il modello di ogni altro produttore
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
avvii con `latet claude`, `latet codex` o un altro comando `latet` si avvia anche direttamente
(`claude`, `codex`); uno le cui impostazioni puntano a Latet (dopo `latet global-install`) passa da Latet
ovunque si avvii, quindi dagli per quella sessione l'indirizzo del suo provider, come dice la sua
documentazione, oppure usa `latet agents revert`. Latet non vede allora niente di ciò che invia. Se
avevi disattivato Latet per lasciar passare i dati personali, *Solo segreti* fa proprio questo e
continua a proteggere i segreti
([Progetti](../../concepts/projects/#solo-segreti-o-latet-disattivato-in-un-progetto)).

## Le cartelle che una richiesta nomina

Una richiesta può nominare la sua cartella: l'header `x-latet-project`, o la cartella di lavoro
che Codex, OpenCode e Gemini CLI scrivono nelle loro richieste. Nella 0.11 una cartella così è
*dichiarata*: le regole del suo progetto che aggiungono protezione si applicano, i suoi allentamenti
no (valori da non mascherare mai, regole per i segreti disattivate, un profilo più debole, `data_tools`,
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
cifrati del provider vale solo per l'indirizzo del provider stesso, mai per un percorso aziendale.
Dopo un riavvio del daemon, o su un server quando i dati della conversazione non ci sono più, una
conversazione del genere viene rifiutata al turno successivo: iniziane una nuova.

## Tornare alla 0.10

```sh
latet upgrade --to 0.10.4
```

Tornando alla versione con cui è stata fatta una copia dei tuoi dati, ti viene proposto di
ripristinare quella copia ([Aggiornamento](../installation/#aggiornamento)). Latet 0.10 ignora
`request_exceptions` e `request_resources`, e invia di nuovo ciò che la 0.11 rifiuta: le parti non
lette delle richieste, le richieste dei progetti in cui Latet è disattivato e le immagini che non sa
leggere. Torna indietro solo se ne sei consapevole.

Un'impostazione è sconosciuta alla 0.10: `request_profile`, su una destinazione in `upstreams`. La
0.11 la scrive solo per una destinazione per cui hai scelto un profilo, in `config.yaml` e in
`config.reviewed.json`, le impostazioni che hai approvato per ultime, nella cartella dei dati di Latet.
La 0.10 non si avvia se è in `config.yaml`, e legge un `config.reviewed.json` che la contiene come se
non ci fossero impostazioni approvate: una modifica a `config.yaml` si applicherebbe allora senza
attendere la tua approvazione. Se ne hai scelto uno, ripristina la copia che l'aggiornamento alla 0.11 ha
fatto, che contiene i due file com'erano prima: `latet upgrade --to` la propone mentre torna indietro
(si trova nella cartella dei dati, in `backups/before-<versione>-<ora>`, e prende il nome dalla versione
installata, per esempio `before-0.15.2-…`). Senza quella copia (la 0.11
installata con l'installer, che non ne fa), togli la chiave da entrambi i file prima di tornare
indietro: le righe `request_profile:` da `config.yaml`, e ogni membro `"request_profile": "…"` da
`config.reviewed.json`, lasciandolo un JSON valido.

Su un server di Latet, la 0.11 aggiunge tabelle al database, e un'istanza rifiuta un database più
recente di essa: tornare indietro vuol dire usare il backup del database fatto prima
dell'aggiornamento, con tutte le istanze ferme
([Aggiornare e aggiungere istanze](../../concepts/server-operations/#aggiornare-e-aggiungere-istanze)).
