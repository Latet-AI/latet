---
title: Registro delle modifiche
description: Le modifiche importanti di Latet, dalle più recenti.
---

Latet segue il [versionamento semantico](https://semver.org). Le modifiche non ancora in una versione
sono elencate in **Non rilasciato**; ogni versione elenca le sue modifiche dalla più recente.

Questa pagina riporta le modifiche non ancora rilasciate e le versioni dalla 0.10.2 in poi. Le
versioni precedenti, dalla 0.10.0 indietro, sono nel
[registro delle modifiche in inglese](../../changelog/#0100-2026-10-27).

## Non rilasciato

Cosa arriva dopo è nella [roadmap](../roadmap/): un rifiuto esplicito dalla modalità auto, un
installer firmato per Windows, i tuoi modelli dei nomi, altri agenti, e la documentazione in altre
lingue.

## 0.12.0 (2026-11-09)

- **Una richiesta le cui impostazioni cambiano prima che parta.** Una richiesta le cui impostazioni
  cambiano tra la sua decisione e il suo invio viene rifiutata come una le cui impostazioni sono
  cambiate mentre veniva preparata: al suo agente viene detto di inviarla di nuovo e la cronologia
  registra un rifiuto, dove prima riceveva un `500` *Latet could not send this request as it was
  decided* ed era registrata come uno scambio. Una richiesta preparata e inviata mentre un salvataggio
  è ancora in corso di applicazione non parte più con le impostazioni applicate a metà: viene
  preparata di nuovo quando sono tutte al loro posto, o rifiutata allo stesso modo. Un salvataggio
  che non riesce a metà rimette le impostazioni che c'erano prima; se nemmeno quelle tornano, ogni
  richiesta viene rifiutata allo stesso modo finché un salvataggio successivo non riesce o Latet non
  riparte.
- **Nuove intestazioni di Claude Code.** In una richiesta che Latet si limita a verificare (Claude
  Code, Bedrock, Vertex), un'intestazione `anthropic-*` che nessun profilo nomina ancora, o un valore
  di `anthropic-beta` di forma nuova, passa una volta letta dal controllo finale, indicata come *solo
  controllato*, come già accade per un membro nuovo del corpo che contiene testo; una nuova
  intestazione `x-claude-code-*` viene tolta e contata. Claude Code continua a funzionare con una
  versione che ne aggiunge una prima che Latet la nomini. Un segreto o un tuo valore in una di queste
  intestazioni viene ancora rifiutato.
- **Aggiornare un server dalla 0.10.** Calcolare le impronte di una cassaforte conservata dalla 0.10
  non occupa più una delle due costruzioni di indice di un'istanza: gli indici delle altre identità
  vengono costruiti nel frattempo, e solo le identità le cui impronte non sono ancora tutte
  calcolate ricevono risposta dalla ricerca in PostgreSQL, come dicono i passaggi
  dell'aggiornamento.
- **Python 3.14.** Latet funziona anche con Python 3.14, su cui è testato come sulla 3.12 e sulla
  3.13. Una nuova installazione su un computer senza Python riceve la 3.14; `latet upgrade` porta alla
  3.14 un Latet già installato solo dove una 3.14 c'è già.

## 0.11.1 (2026-11-06)

- **Le intestazioni di suggerimento di Claude Code.** Quando Claude Code è impostato per inviare le
  sue intestazioni di suggerimento per i gateway (`CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`), non viene
  più rifiutato dopo una compattazione o una chiamata a uno strumento: `x-claude-code-compaction` e
  `x-claude-code-context-compacted` passano, limitate ai loro tre valori, e
  `x-claude-code-prev-tool-durations`, che nomina gli strumenti eseguiti, viene tolta e contata, come
  gli id dei suoi subagenti.
- **Una richiesta inviata mentre le impostazioni cambiano.** Una richiesta preparata mentre le sue
  impostazioni venivano applicate di nuovo (un salvataggio, o su un server una modifica condivisa)
  viene preparata di nuovo con quelle nuove, come Latet faceva già quando cambiavano solo le regole,
  invece di essere rifiutata con *could not account for every part of this request*. Una richiesta
  la cui rotta è cambiata nel frattempo, o le cui impostazioni sono cambiate dopo che è stata
  preparata, continua a non essere inviata, e al suo agente viene detto che le impostazioni sono
  cambiate e di inviarla di nuovo; la cronologia, il log e la dashboard dicono lo stesso, con un
  motivo tutto suo.
- **Una risposta inviata per intero viene registrata per intero.** Una risposta in streaming arrivata
  per intero al suo client viene registrata per intero anche quando il client chiude la connessione
  subito dopo, come già accadeva per un turno WebSocket; prima la cronologia la mostrava come un
  errore, senza il suo testo.
- **Un WebSocket rifiutato dal provider è nella cronologia.** Un WebSocket il cui handshake il provider
  rifiuta (`401`, `403`) o reindirizza, o il cui provider non si raggiunge, viene registrato nella
  cronologia come la stessa risposta via HTTP; prima non lasciava traccia.
- **L'indice delle eccezioni delle richieste sul server.** Uno spazio costruito su più worker o istanze
  insieme non riscrive più ogni volta l'indice delle eccezioni delle richieste dell'identità: viene
  scritto solo quando è cambiato, da uno alla volta, così il log non avverte più che non è stato
  scritto e `latet server status` non elenca mai un'eccezione revocata nel frattempo.
- **Una rotta scomparsa tra due tentativi AWS.** Quando una rotta di Bedrock viene tolta tra i due
  tentativi AWS di una richiesta (al primo è stato risposto con un token scaduto), all'agente viene
  detto che non è stato inviato nessun altro tentativo e che il primo potrebbe essere arrivato,
  invece del solo *no upstream for this path*.
- **Chi ha approvato un'eccezione di richiesta.** Un'eccezione approvata il cui `issuer` (chi l'ha
  approvata) è stato cambiato a mano attende di nuovo l'approvazione, anche se è stata anticipata
  pure la sua ora di approvazione; prima, quell'ora anticipata la lasciava in vigore con il nuovo
  `issuer`.

## 0.11.0 (2026-11-05)

Latet 0.11.0 blocca per impostazione predefinita le richieste quando una parte del percorso, dei
parametri di query, delle intestazioni o del corpo non è né ispezionata né autorizzata esplicitamente.
Un corpo vuoto non basta per consentire una richiesta. Le credenziali necessarie del provider e i
componenti firmati vengono preservati secondo le regole definite. Prima dell'aggiornamento, verifica
le rotte e le eccezioni circoscritte: le chiamate prima inviate senza ispezione potrebbero essere
bloccate. La dashboard indica le parti non ispezionate consentite esplicitamente. Un rifiuto dice
all'agente che non è stato inviato niente solo quando niente è partito: quando un secondo tentativo
viene rifiutato dopo che un primo ha raggiunto il provider, all'agente viene detto che non è stato
inviato nessun altro tentativo e che il primo potrebbe essere arrivato.
[Passare alla 0.11](../getting-started/upgrading-to-0-11/) dice cosa controllare prima, e
`latet doctor` elenca ciò che si fermerà.

- **Lo stesso su un computer e su un server.** Un computer ora rifiuta ciò che un server Latet
  rifiutava: una richiesta di cui Latet non legge il corpo, contenuti in una forma che non legge, un
  frame WebSocket che non è un turno. Anche su un computer vengono controllate le definizioni degli
  strumenti di Claude Code. Su un computer il corpo di una richiesta ha un limite di 32 MiB.
- **Chiamate senza corpo.** L'elenco dei modelli e la lettura di un modello passano su ogni provider
  che Latet conosce, compresi i profili di inferenza di Bedrock, e i loro parametri di query vengono
  letti. Ogni altra chiamata viene rifiutata, senza nominare nessuna tua rotta: file, upload, batch,
  audio, generazione di immagini, moderazione, una risposta salvata, le letture di una cache di
  Gemini.
- **Intestazioni e parametri di query.** Ognuno è letto con una regola propria. Un'intestazione che
  nessun profilo nomina viene rifiutata, e il suo nome viene detto solo all'agente. Le intestazioni
  di telemetria e di tracing degli SDK (`x-stainless-*`, `traceparent`, `amz-sdk-*` dell'SDK di AWS,
  `x-goog-api-client`), gli id dei subagenti di Claude Code e i valori che copia dal suo ambiente
  (`x-claude-code-agent-id`, `x-claude-remote-session-id`...), e `x-codex-turn-metadata` e
  `x-openai-subagent` di Codex vengono tolti e contati nella dashboard, e un `401` o un `403` che
  segue lo dice ([l'elenco](../getting-started/upgrading-to-0-11/#le-intestazioni-che-latet-toglie)). In una richiesta che Latet si limita a verificare, il corpo
  esce comunque byte per byte. Un corpo di richiesta compresso esce compresso di nuovo da ciò che
  Latet ha letto, mai così come è arrivato, così il nome di file della sua intestazione gzip o i byte
  dopo i suoi dati compressi non arrivano mai al provider; in una richiesta che Latet si limita a
  verificare, un corpo compresso viene rifiutato, con parole che dicono di inviarlo non compresso.
  Un parametro di query che nessun profilo nomina viene rifiutato, e il
  suo nome viene detto solo all'agente. Una richiesta rifiutata per un'intestazione, un parametro di
  query o il suo percorso finisce nella cronologia con quella categoria, senza intestazioni; un
  WebSocket rifiutato così viene chiuso con il codice 1008 e un motivo che dice lo stesso. Le
  intestazioni che imposti per un agente (`ANTHROPIC_CUSTOM_HEADERS`, `http_headers` di Codex,
  `COPILOT_PROVIDER_HEADERS`) vengono rifiutate finché un'eccezione non le nomina.
- **Ogni membro del corpo.** Un membro che nessun profilo nomina viene rifiutato, dicendo dove si
  trova, mai il suo nome; in una richiesta che Latet si limita a verificare (Claude Code, Bedrock, Vertex), un
  membro nuovo che contiene testo passa una volta verificato, mostrato come *solo controllato*.
  `client_metadata` di Codex viene tolto prima che la richiesta sia mascherata. Gli id dei modelli
  passano nelle forme documentate dai loro provider; un ARN di Bedrock, un progetto di Vertex, il nome
  di un modello proprio di un gateway o una `api-version` vanno prima elencati in `request_resources`.
  Su OpenRouter un modello passa come `anthropic/`, `openai/` o `google/` seguito da un modello di
  quel provider, oppure come `openrouter/auto`; anche il modello di un altro provider va prima
  elencato lì.
- **Ciò che il provider ha restituito.** Le firme del ragionamento, il ragionamento cifrato, le
  compattazioni e i risultati delle ricerche web passano quando Latet li ha visti arrivare dal
  provider in quella conversazione, su qualsiasi istanza di un server; dopo un riavvio del daemon di
  un computer, una regola integrata lascia passare i campi cifrati del provider stesso, solo nei turni
  del modello e solo verso l'indirizzo del provider. Una conversazione attraverso un gateway aziendale
  che inoltra le firme del ragionamento viene rifiutata dopo un riavvio.
- **Strumenti e codice di Gemini.** Il codice che Google esegue per il modello e ciò che quel codice
  ha stampato, e gli input e gli output degli strumenti integrati di Google (Google Search, contesto
  URL, Maps, File Search), vengono ora mascherati, come la chiamata di una funzione e il suo
  risultato; la 0.10 li lasciava come erano stati scritti. Il ragionamento del modello resta ancora
  come l'ha scritto.
- **Immagini e documenti.** Una richiesta con un'immagine viene inviata solo dopo che Latet ne ha
  letto il testo: senza il riconoscimento del testo, o con `ocr: off`, viene rifiutata con parole che
  nominano `latet ocr install`. `images: allow` non invia più un'immagine che Latet non ha letto, e
  documenti, audio e immagini indicate dal loro indirizzo vengono rifiutati. In una richiesta che
  Latet si limita a verificare, ogni byte di un'immagine deve trovare la sua spiegazione: la posizione di una
  foto, le note del produttore di una fotocamera, una miniatura, una seconda immagine e il video di una
  foto animata vengono rifiutati, con il modo di toglierli, e un'immagine WebP originale viene
  rifiutata; i codici QR non vengono decodificati. Le richieste che Latet riscrive inviano le immagini
  riscritte, senza metadati.
  Su un server il cui riconoscimento del testo non è disponibile (non è riuscito a caricarsi),
  `latet_ocr_unread_total{reason="unavailable"}` conta una volta ogni richiesta o turno WebSocket
  rifiutato, quante che siano le immagini che porta.
- **Un progetto in cui Latet è disattivato non invia niente.** Le sue richieste vengono rifiutate, via
  HTTP e via WebSocket, con parole che dicono come riattivare Latet lì o raggiungere direttamente il
  provider. `latet project off` e l'interruttore della dashboard lo dicono prima.
  `latet serve --passthrough` non esiste più: l'opzione nascosta esce con un messaggio.
- **Progetti attendibili e dichiarati.** Una cartella che una richiesta nomina (`x-latet-project`, la
  cartella di lavoro che scrivono Codex, OpenCode e Gemini CLI) aggiunge solo protezione: gli
  allentamenti e le eccezioni di richiesta del suo progetto non si applicano, e un progetto dichiarato
  che è disattivato viene rifiutato. Un progetto è attendibile grazie al legame del plugin di Claude
  Code, che finisce dopo 30 minuti senza chiamate, o grazie a una chiave di avvio. Le chiamate di
  Claude Code prima della prima chiamata del plugin vengono decise con le tue regole soltanto.
- **Chiavi di avvio.** `latet claude`, `latet codex`, `latet opencode`, `latet gemini` e
  `latet copilot` danno all'agente una chiave segreta di quell'avvio, nella sua impostazione delle
  intestazioni, che Latet verifica e toglie; Codex sui suoi provider integrati, Codex prima della
  0.115, OpenCode prima della 1.19.5, Gemini CLI in modalità ACP, Copilot CLI fuori da Chat
  Completions e `latet run` non ne passano nessuna, e il comando che li
  avvia lo dice in una frase. Una chiave attiva nell'output di uno strumento viene mascherata come il token di
  Latet. La dashboard mostra per ogni richiesta se il progetto era attendibile o dichiarato, e il suo
  avvio: *Avviato da Latet, chiave verificata*, *Avviato da Latet, chiave mancante*, *Collegato dal
  plugin di Claude Code* o *Non avviato da Latet*; `latet doctor` nomina gli agenti visti senza la
  loro chiave, e un Codex, un OpenCode o un Gemini CLI troppo vecchio per portarne una.
- **Eccezioni di richiesta.** `request_exceptions` nomina esattamente ciò che lascia passare
  (un'intestazione, un parametro di query o un membro del corpo, su una destinazione e un profilo di
  richiesta), per ogni tua richiesta o, con un legame attendibile, per un progetto. Ognuna attende la
  tua approvazione, dura 30 giorni per impostazione predefinita e 90 al massimo, con un avviso sette
  giorni prima della fine, oppure è resa permanente di proposito, segnata come tale ed elencata da
  `latet doctor` e da `latet server status`. Una revisione di un profilo che cambia ciò che una nomina
  la sospende finché non la approvi di nuovo. Nella 0.11.0 nessuna eccezione si applica a un
  WebSocket, o a un membro del corpo dentro un elemento del provider o nella sorgente di un'immagine,
  e nessuna nomina un'intera rotta: lì la dashboard non ne propone nessuna. Con la cronologia disattivata, le richieste che ognuna
  ha lasciato passare vengono contate in memoria, e sommate tra le istanze di un server. Un server
  tiene in PostgreSQL un indice delle eccezioni approvate di ogni persona, che `latet server status`
  legge: solo id, tipi di ambito, fini e stati, mai ciò che un'eccezione indica né la sua descrizione.
- **Copertura visibile.** Ogni scambio nella dashboard dice come è stata coperta la sua richiesta:
  bloccata, ogni parte letta, o lasciata passare da una regola o da un'eccezione indicata, con le
  intestazioni e i membri che il suo profilo ha tolto e il suo profilo di richiesta. Uno scambio
  registrato prima della 0.11 non viene mai mostrato come verificato: dice *Registrata da una
  versione precedente di Latet. Non è registrato quanto di questa richiesta abbia letto, quindi non
  risulta verificata.* Né lo è uno che ha lasciato passare una parte senza leggerla, o un controllo
  del plugin di Claude Code.
- **Cosa conservano i log.** I log, i metadati della cronologia e le registrazioni delle decisioni di
  un server conservano categorie fisse e schemi di rotta, mai il nome di un'intestazione o di uno
  strumento, un valore o il percorso di una richiesta così come è stato inviato.
- `latet doctor`, e il primo avvio del daemon con la 0.11 nel suo log, elencano per tipo e per numero
  ciò che la 0.11 ferma e la 0.10 inviava: i progetti in cui Latet è disattivato, il riconoscimento
  del testo non installato o disattivato, `--passthrough` in una voce che avvia Latet, e le chiamate
  della tua cronologia che nessun profilo copre.

- Le cartelle dei progetti nelle tue impostazioni vengono salvate in forma canonica: barre rovesciate
  e lettera di unità maiuscola su Windows, e nessun separatore finale (`C:/work/shop/` viene salvata
  come `C:\work\shop`). L'id di ogni progetto non cambia, e nemmeno le impostazioni che vi si
  applicano.

- La cassaforte di una persona su un server viene tenuta tutta in memoria quando, al momento in cui
  il suo spazio viene costruito, contiene 35.000 valori o meno (circa 1,6 KB per valore su ogni
  istanza e ogni worker che serve la persona, contati nella memoria dello spazio): i testi della
  persona vengono mascherati senza chiedere i loro valori a PostgreSQL, circa due volte più in fretta
  che cercandoli. Oltre i 35.000 valori lo spazio di una persona tiene in memoria i valori che la
  persona ha usato per ultimi (`key_working_values`) e trova ogni altro in PostgreSQL, come fa quello
  di una chiave API: lo spazio di una persona da 200.000 valori occupa 24 MB, dove quello della 0.10.0
  ne occupava 495. In entrambi i casi ogni richiesta chiede prima a PostgreSQL cosa hanno cambiato
  le altre istanze, così un valore tenuto o dimenticato su un'istanza viene visto dalla richiesta
  successiva su qualsiasi altra ([Gestione del server](../concepts/server-operations/)).
- **Un server maschera più in fretta.** I rilevatori passano meno pattern su ogni testo, i testi di
  una chiamata dell'API per le applicazioni vengono letti insieme, e un'istanza trova i valori di una
  chiave API o di una persona attraverso impronte delle parole della sua cassaforte tenute in memoria
  (circa 15 MB per un milione di valori), chiedendo a PostgreSQL solo le parole che possono essere un
  valore o un valore fittizio. Misurato su un'istanza sul computer della documentazione, una chiave API
  da 200.000 valori maschera 871 righe di log al secondo e ne ripristina 2.960, dove la 0.10.0,
  misurata allo stesso modo, ne mascherava 350 e ne ripristinava 2.114; una da 1.000.000 di valori 773
  e 2.967, contro 304 e 2.014; una chiave nuova ne maschera 191, contro 153. L'obiettivo di 1.000
  righe al secondo a quelle dimensioni non è raggiunto ([Quanto è veloce](../applications/#quanto-è-veloce)).
- **L'aggiornamento ferma il server una volta.** La 0.11 aggiunge a ogni riga di parola di una cassaforte un
  posto per la sua impronta e per il conteggio della modifica che l'ha scritta per ultima: ferma ogni istanza e
  worker, esegui `latet server migrate` una volta e avviali con la 0.11. Una cassaforte conservata dalla 0.10
  riceve le sue impronte dopo il riavvio, in background, una volta, un'istanza alla volta, mentre le sue
  richieste e chiamate ricevono risposta attraverso la ricerca in PostgreSQL, più lentamente, nessuna rifiutata;
  poi ogni istanza costruisce il suo indice delle impronte, in 1,5 secondi per 1.000.000 di valori ([Aggiornare
  un server](../applications/#aggiornare-un-server)).
- `latet server migrate`, e `latet server init` su tabelle per cui è dovuta una migrazione, non
  cambiano nulla finché un'istanza del server è attiva, della 0.10 o della 0.11, o finché Valkey non
  risponde: dicono quante istanze sono attive e di quale versione, ed escono con 2. Un'istanza della
  0.10 conta come attiva per 30 secondi dopo il suo arresto. Una volta applicate le migrazioni
  chiedono di nuovo, e quando un'istanza è diventata attiva nel frattempo lo dicono ed escono con un
  errore (`migrate` 1, `init` 2): un'istanza della 0.10 lasciata in funzione sulle tabelle della 0.11
  continuerebbe a servire richieste senza leggerle come fa la 0.11.
- `latet server status` elenca un'istanza di Latet 0.10 ancora attiva accanto a quelle della 0.11,
  con il suo id, come Latet 0.10 o precedente, e dice quando il suo elenco è stato tagliato al suo
  limite (`instances: the list is partial, cut at its bound: an instance may be alive and not
  listed`, e `instances_whole` false nella sua risposta).
- **Leggere ogni parte di una richiesta costa tempo.** Misurato su un'istanza, come mediana, rispetto
  alla stessa versione senza quella lettura: una piccola richiesta di chat richiede circa 1 ms in più
  su un computer (2,02 ms contro 0,94) e su un server (6,99 ms contro 5,95); una richiesta da 4.000
  slot richiede 3,8 volte tanto su un computer (362 ms contro 96) e 1,4 volte su un server; una
  richiesta di 500 testi già mascherati e uno nuovo 2,8 volte su un computer (36 ms contro 13) e 1,16
  volte su un server.
- `latet server run --workers N` (o l'impostazione `workers`) esegue N istanze del server su una macchina, sulla
  stessa porta, ciascuna un processo a sé, perché un server usi più dei suoi core. Su Linux ogni worker ascolta
  sulla porta con un socket suo, e il kernel distribuisce le nuove connessioni tra di loro secondo un hash degli
  indirizzi e delle porte di ciascuna connessione: molte connessioni si distribuiscono in modo uniforme, poche
  connessioni di lunga durata, come quelle di un bilanciatore di carico, possono comunque finire in modo
  disuguale. Il processo che avvia i worker tiene la porta per tutto il tempo, e solo i programmi che girano con
  l'utente del server possono ascoltare lì accanto a loro, quindi quell'utente esegue soltanto Latet. Misurato
  su Linux attraverso l'API per le applicazioni, otto worker mascherano 6,28 volte quanto ne maschera uno e
  ripristinano 5,21 volte, e ognuno occupa da 185 a 227 MB con lo spazio di una chiave API da 200.000 valori.
  Più worker vengono rifiutati su Windows, e su macOS condividono un socket ([Gestione del
  server](../concepts/server-operations/), Più istanze su una macchina).
- Corretto: `latet upgrade` nominava, in ciò che installava, solo il rilevamento dei nomi e il testo
  nelle immagini, e uv, che installa uno strumento con esattamente gli extra che il suo requisito
  nomina, toglieva gli altri: un server installato con `latet[server]` che veniva aggiornato perdeva
  psycopg e redis, e `latet server run` allora rifiutava di avviarsi. L'aggiornamento ora nomina ogni
  extra che ha il Latet installato, come li elenca il registro di uv della sua installazione (dove
  quel registro manca o non si può leggere, ogni extra i cui pacchetti sono tutti installati), accanto
  al rilevamento dei nomi e al testo nelle immagini; dove quei due non si possono installare, gli
  altri extra restano. Li conservano anche `--to` e la finestra dell'aggiornamento su Windows, e così
  gli installer, eseguiti di nuovo su un Latet installato con altri extra, come li elenca uv (un uv
  troppo vecchio per elencarli non ne dà nessuno, come prima).

  Un aggiornamento esegue il codice della versione installata, quindi un Latet fino alla 0.10.4 nomina
  ancora solo il rilevamento dei nomi e il testo nelle immagini quando passa a questa versione. Un
  server alla 0.10.4 o precedente prende quindi questa versione una volta per un'altra via: il comando
  con cui è stato installato, che nomina `server` e le sue cartelle `UV_*`, oppure l'installer
  scaricato di nuovo da GitHub ed eseguito con quelle variabili. Un server il cui aggiornamento ha già
  tolto `server` lo riottiene con quel comando ([Gestione del server](../concepts/server-operations/),
  Aggiornare un Latet installato come servizio di sistema). Dove l'extra manca, `latet server run` ora
  dà quel comando con dentro gli altri extra, e `latet upgrade` e la Risoluzione dei problemi dicono
  quali extra nomina `LATET_SOURCE` per un'installazione a mano.
- Corretto: `latet upgrade` installava nella cartella degli strumenti di uv, qualunque fosse la
  cartella del Latet in esecuzione: un server installato con un suo `UV_TOOL_DIR`, come dice Gestione
  del server, non veniva mai aggiornato, e un secondo Latet veniva installato e avviato nella cartella
  home dell'account che eseguiva il comando. `latet upgrade` ora si ferma lì prima di cambiare
  qualsiasi cosa, e stampa il comando da eseguire invece, con le cartelle che ha trovato. Gestione del
  server ha una nuova sezione sull'aggiornamento di un Latet installato come servizio di sistema, e
  Installazione vi rimanda un server.

## 0.10.4 (2026-11-04)

- Corretto: su Windows, il rilevamento locale dei nomi poteva smettere di trovare nomi quando Latet
  girava dentro un'app installata come pacchetto, come l'app desktop di Claude. Windows tiene i file
  di un'app così in una cartella dell'app stessa
  (`...\Packages\Claude_...\LocalCache\Local\latet`) e li mostra al posto solito, quindi la cartella
  del modello dei nomi poteva trovarsi al posto solito mentre i suoi file erano nella cartella
  dell'app, anche quando ci si arrivava attraverso un collegamento al posto solito. ONNX Runtime,
  dalla versione 1.24 in poi (un'installazione riceve ora la 1.30.0), rifiutava allora di caricare
  il modello ("External data path escapes model directory"), e le richieste proseguivano con i nomi
  lasciati in chiaro: solo il log del daemon diceva che il modello locale dei nomi non si poteva
  caricare. Latet ora apre il modello dal posto in cui si trova davvero il suo file, e i nomi
  vengono di nuovo trovati. In tutti gli altri casi il modello si carica come prima.

## 0.10.3 (2026-10-29)

- Corretto: su un server Latet, un salvataggio delle impostazioni che controlla i nomi delle
  destinazioni (dalla dashboard o dall'amministrazione) avviava la ricerca del nome successivo dopo
  che una ricerca aveva esaurito tutto il tempo del salvataggio, quando l'orologio di sistema non era
  ancora avanzato (visto su Windows). Ora non ne avvia nessuna: quei nomi vengono controllati quando
  il server si connette, come già succedeva per i nomi che non rispondono in tempo.
- La pagina [sicurezza](../security/) spiega come Latet raggruppa le richieste in conversazioni: le
  intestazioni e i campi che legge, l'inizio della cronologia che usa quando non ce ne sono, perché
  due chat che iniziano allo stesso modo possono condividere una conversazione e ripristinare l'una i
  valori dell'altra, e che ogni identità di un server ha la propria cassaforte e le proprie
  conversazioni.
- Una versione viene pubblicata solo dopo che la CI completa del suo commit esatto su `main` è
  riuscita: il workflow di rilascio lo controlla prima di costruire qualsiasi cosa, e altrimenti si
  ferma. La nuova pagina [Pubblicare Latet](../releasing/) descrive i passaggi, le guide per chi
  contribuisce vi rimandano, e il modello delle pull request chiede quali controlli sono stati
  eseguiti, chi ha revisionato la modifica e dove sono le prove.

## 0.10.2 (2026-10-28)

- Corretto: un segreto più corto di dodici caratteri (`password=1234`, `token=ab12f3`) riceveva un
  valore fittizio della sua stessa forma, quattro cifre o sei cifre esadecimali, che un testo
  successivo poteva contenere per caso dentro un id o un numero. L'unmask dell'API delle applicazioni,
  che legge ogni valore fittizio della cassaforte di una chiave, metteva allora lì il segreto: una riga
  di log che non lo aveva mai contenuto tornava cambiata (`request_id=347bf309420a2290` come
  `347bf573020a2290`), e il suo mask prendeva il valore fittizio trovato dentro una parola per un
  valore a sé. Un segreto così riceve ora un valore fittizio più lungo degli stessi tipi, con i suoi
  segni al loro posto: quindici cifre per un numero (con il segno e la virgola decimale, così che un
  numero JSON resti un numero), dodici cifre esadecimali per un valore esadecimale, altrimenti dodici
  lettere e cifre. Un segreto di dodici caratteri o più, e i dati personali, conservano valori
  fittizi della loro forma. Un valore fittizio corto dato prima (otto caratteri o meno) resta, come
  ogni valore fittizio. Una conversazione, su un computer o su un server, lo rimette come prima, anche
  dentro una parola, perché ripristina solo i valori fittizi che le sono stati mostrati. L'unmask
  dell'API delle applicazioni lo rimette solo dove sta come parola intera, senza lettere, cifre o
  trattini bassi accanto: dove il valore di un segreto così era mascherato dentro una parola o un
  numero più lunghi (`postgres` in `postgresql://`, `1234` in `order_id=9912343`), unmask lascia lì il
  suo valore fittizio. Un valore fittizio da 9 a 11 caratteri viene rimesso come prima ovunque. Un tuo
  riconoscitore della classe `secret` conserva il valore fittizio che dà la sua regola, della forma
  del valore; uno di otto caratteri o meno viene rimesso come un valore fittizio corto dato prima.
  Aggiorna ogni istanza di un server: una che esegue ancora la 0.10.0 dà valori fittizi corti ai nuovi
  segreti corti, e anche quelli restano.
- Corretto: una richiesta che Latet rifiuta prima di mascherarla (il mascheramento fallisce o, su un
  server, il suo corpo verrebbe decodificato oltre `max_request_mb`) appariva nella Cronologia come
  inviata senza lettura, con un errore, anche se non era stato inviato niente. Ora appare come bloccata
  e non inviata, con il motivo nelle parole di Latet e la dimensione del suo corpo, e su un server la
  metrica degli arresti (`latet_agent_stops_total`) la conta.
- Corretto: un testo mascherato prima che la cassaforte imparasse un valore attorno a un valore già
  mascherato al suo interno lasciava in chiaro il resto di quel valore quando veniva mascherato di
  nuovo. Un IBAN attaccato a lettere su entrambi i lati (`MarioIT60X0542811101000000123456Ferri`) che
  conteneva un segreto noto alla cassaforte aveva mascherato solo quel segreto; una volta che un testo
  successivo aveva insegnato alla cassaforte l'intero IBAN, lo stesso testo mascherato di nuovo come
  era stato mascherato prima (da una chiamata successiva alle applicazioni, in una nuova conversazione,
  o nella stessa chiamata o richiesta) mostrava ancora gli altri caratteri dell'IBAN. Un nome imparato
  per intero (`Mario Rossi`) attorno a un cognome mascherato prima (`Rossi`) lasciava `Mario` in chiaro
  allo stesso modo. Il controllo finale non lo fermava, perché nessun valore intero era in chiaro. Un
  testo così ora viene mascherato di nuovo, per intero. Anche il testo di una conversazione che non è
  mai uscito (la sua richiesta bloccata o abbandonata) viene mascherato da capo; uno che è uscito viene
  inviato di nuovo come è uscito, come prima.
