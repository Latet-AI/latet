---
title: Cosa è protetto e cosa no
description: I limiti di Latet. Leggi questa pagina prima di farci affidamento.
---

Latet riduce ciò che il tuo agente rivela al provider del modello. Non è una garanzia che niente di
sensibile esca mai dal tuo computer. Questa pagina elenca cosa è coperto e cosa no.

## Protetto

Quando un [rilevatore](../detection/) riconosce un valore, Latet lo sostituisce con un valore
fittizio prima che la richiesta protetta raggiunga il provider del modello. Con un server,
i dati originali possono prima raggiungere quel server. Questo copre:

- **In Claude Code (modalità client):** i prompt che scrivi, il prompt di sistema, i file `CLAUDE.md` e
  l'altro contesto che Claude Code aggiunge, gli allegati, i prompt delle skill, le descrizioni degli
  strumenti e ogni risultato di uno strumento: contenuto dei file, output dei comandi, output degli
  hook. Un controllo finale legge poi i testi di ogni richiesta prima che venga inviata. Ciò che ha
  scritto il modello non ferma niente quando torna esattamente come il modello l'ha scritto, cosa che
  Latet sa dalle risposte che ha fatto passare e dalle chiamate agli strumenti che il plugin ha fatto
  ripristinare; ogni altro testo al posto del modello viene letto come tuo. Su un computer il daemon
  tiene tutto questo in memoria: dopo un riavvio, un testo che il modello ha scritto prima nella
  conversazione viene letto come tuo, e uno che contiene un valore che Latet maschera ferma la
  richiesta, quindi in quel caso inizia una nuova conversazione. Un server Latet lo conserva per ogni
  istanza, per un'ora dal suo ultimo uso.
- **In modalità gateway:** il prompt di sistema o le istruzioni, e ogni messaggio della conversazione:
  il tuo testo, le risposte precedenti del modello, le chiamate agli strumenti e i loro risultati.
  Anche l'input degli embedding. Un controllo finale legge poi quei testi prima che la richiesta
  venga inviata, come in modalità client.

Risposte gateway, stream e argomenti degli strumenti vengono ripristinati dove gira Latet:
sul computer per l'uso locale, sul server per un gateway connesso. Il plugin di Claude Code
ripristina argomenti degli strumenti e testo interattivo sullo schermo del computer;
stdout di `claude -p` contiene ancora valori fittizi. OCR/NER vengono eseguiti dove gira
il motore Latet interessato; un revisore configurato può ricevere testo originale.

Le chiavi di un documento JSON, come l'output di uno strumento, vengono mascherate come i suoi
valori: un indirizzo email, un numero di telefono, un segreto, un nome o uno dei tuoi termini scritto
come chiave viene sostituito con il valore fittizio che ha ovunque, e una chiave in cui il modello
scrive un valore fittizio viene ripristinata per l'agente. Una regola di campo e la modalità dati
rigorosa trovano ancora un valore dalla sua chiave così com'era, e una chiave che dà soltanto il nome
a un campo, come `id` o `email`, resta così com'è.

Con il [revisore locale](../llm-review/) attivo, anche un modello linguistico sul tuo computer legge
quei testi prima che escano, e maschera i nomi, i dati sulla salute e gli altri valori che nessun
rilevatore trova, anche nelle chiavi di un documento JSON. Una chiave scritta come il nome di un
campo, in lettere minuscole, cifre, `_` e `-` (`exit_code`, ma anche `mario_rossi`), non gli viene
mostrata: i rilevatori la leggono come leggono ogni chiave.

## Non protetto

**Valori che i rilevatori non riconoscono.** Latet maschera solo ciò che rileva. Un segreto in un
formato insolito, diviso su più righe o codificato (per esempio in base64 sotto un nome qualunque) può
passare. Nomi e indirizzi nel testo libero vengono trovati solo con il
[modello locale dei nomi](../../getting-started/installation/) facoltativo o con il
[revisore locale](../llm-review/), entrambi senza garanzie; senza di loro vengono mascherati solo il
nome del tuo autore git, i tuoi termini e i valori già nella cassaforte. Le date di nascita non
vengono rilevate. Il rilevamento ha sempre dei falsi negativi.

**Immagini.** Con il [riconoscimento del testo](../images/) facoltativo, il testo di uno screenshot
viene letto sul tuo computer e ogni valore trovato viene coperto con il suo valore fittizio. Non dà
garanzie, come per i nomi: il testo che non legge esce così com'è. Senza il riconoscimento del testo,
le immagini vengono inviate così come sono e segnalate con *Non ispezionato*.

**PDF e altri documenti, immagini indicate dal loro indirizzo, clip audio.** Il loro contenuto non
viene letto e viene inviato così com'è, anche da un server Latet; la dashboard segna quelle richieste
con *Non ispezionato*. Non allegare documenti che contengono dati sensibili, oppure imposta
`images: block` (il profilo `max` fa lo stesso): una richiesta che ne contiene uno allora non viene
inviata. Le pagine di un browser, lette come testo, vengono mascherate come qualsiasi output di uno
strumento ([dati di produzione](../production-data/)); i suoi screenshot sono immagini.

**Gli strumenti web, i subagenti e i connettori di claude.ai mantengono i valori fittizi (Claude Code).**
WebFetch, WebSearch, i subagenti e i connettori di claude.ai (Gmail, Calendar...) ricevono i valori
fittizi che il modello ha scritto, non gli originali, perché i loro argomenti escono dal tuo computer
o avviano un'altra conversazione con un modello. Una ricerca web di un nome mascherato cercherebbe
qualcun altro, e un connettore a cui si chiede di scrivere a `emily.carter@northwind-legal.com`
riceverebbe l'indirizzo fittizio, quindi una chiamata a uno strumento web o a un connettore che
contiene un valore fittizio viene fermata prima di essere eseguita: il modello si rivolge a te, e
*Non sensibile* nella dashboard lascia uscire il valore in chiaro. I risultati dei connettori di
claude.ai tornano attraverso i server di Anthropic, dove gira il connettore: qualunque cosa
contengano è già lì prima che Latet possa mascherarla.

**Strumenti eseguiti dal provider (agenti in modalità gateway).** Una ricerca web o un server MCP
remoto che il provider esegue da sé, come può fare Codex, lavora su ciò che il modello ha scritto: il
valore fittizio. Latet vede una chiamata del genere solo dopo che è stata eseguita, nella cronologia
della richiesta successiva: la dashboard la segnala (*Valore fittizio a uno strumento esterno*), con
*Non sensibile* per ogni valore, così la volta successiva il modello usa quello reale.

**Ambienti che Latet non può vedere:**

- Chat e Cowork di Claude Desktop, con l'accesso a Claude;
- le sessioni cloud, che girano sull'infrastruttura del provider invece che sul tuo computer;
- Cursor, Windsurf e gli altri strumenti le cui richieste al modello partono dai server del
  produttore ([l'elenco](../../agents/other-tools/#cosa-latet-non-può-proteggere));
- i suggerimenti inline di GitHub Copilot e le edit prediction di Zed, che arrivano dai servizi dei
  loro produttori;
- OpenCode con accesso tramite ChatGPT (nella [roadmap](../../roadmap/));
- ciò che gli strumenti di un agente inviano quando vengono eseguiti, come un'API di ricerca sul
  web, un database o un vector store nel cloud: ricevono i valori reali (vedi
  [Framework per agenti](../../agents/openai-compatible/#framework-per-agenti));
- qualsiasi agente o ambiente che non hai configurato per usare Latet.

Con l'accesso a Claude, nella scheda Code di Claude Desktop il plugin maschera i contenuti, ma l'app
ignora `ANTHROPIC_BASE_URL`: dietro il plugin non c'è il controllo del daemon, e nella dashboard non
resta nessuna traccia. Su un gateway di terze parti, senza l'accesso a Claude, ogni richiesta di
Chat, Cowork e Code passa da Latet: Latet le controlla una per una e ferma quella che porterebbe con
sé un valore sensibile in chiaro, ma maschera solo nella scheda Code, con il plugin. Vedi
[Claude Code](../../agents/claude-code/#copertura) e
[Claude Desktop su un gateway](../../agents/other-tools/#claude-desktop-su-un-gateway).

**Contesto e metadati.** I nomi dei file, i percorsi, la struttura del codice, i commenti e la logica
di business vengono inviati così come sono, a parte i valori che Latet vi rileva, come il tuo nome
utente in un percorso della cartella home. I valori fittizi mantengono la forma dell'originale
(lunghezza, formato, prefisso), e il testo intorno può rivelare che cos'è un valore ("la password del
database di produzione").

**Ciò che un documento JSON contiene dove il mascheramento non arriva.** Un numero di carta scritto
come numero JSON, che il profilo `balanced` non maschera, viene trovato dal controllo finale, che
allora ferma la richiesta: all'agente viene detto perché, e la dashboard mostra dov'è il valore.
Finché un output del genere resta nella conversazione, ogni sua richiesta si ferma: inizia una nuova
conversazione e chiedi all'agente i dati in un'altra forma. Il valore fittizio estratto per una chiave
non è mai un'altra chiave del suo oggetto; una chiamata a uno strumento il cui input contiene come due
chiavi un valore e il suo valore fittizio, che diventerebbero una sola una volta mascherate, ferma la
richiesta, perché un oggetto contiene una chiave una volta sola, e due chiavi così restano come le ha
scritte il modello quando Latet ripristina la sua risposta per l'agente. Un oggetto JSON che ripete
una chiave viene letto con la sua ultima copia, ed esce riscritto con quella copia sola, così la copia
che Latet non ha letto non esce mai; una richiesta che Latet si limita a controllare, come quelle di
Claude Code, viene fermata quando è il suo stesso corpo a ripetere una chiave.

**Chiamate con chiavi in conflitto nella cronologia.** Conservare entrambe le chiavi in una chiamata
ripristinata ne preserva i dati, ma mascherarla di nuovo può rendere le chiavi identiche. È un limite
documentato: vengono rifiutate la richiesta successiva e tutte quelle che conservano quella chiamata.
Inizia una nuova conversazione, oppure rimuovi o riformula la chiamata tramite i controlli della
cronologia supportati dal tuo client. Riprovare con la richiesta invariata non sblocca la conversazione.

**Argomenti degli strumenti in streaming.** Latet trattiene gli argomenti JSON fino al completamento
della chiamata, poi ripristina l'intero documento senza unire chiavi distinte. Il testo normale continua
in streaming. I canali JSON trattenuti condividono un unico budget limitato della risposta, inclusi
metadati e crescita dovuta a ricostruzione e serializzazione. Superarlo, o ricevere argomenti incompleti
o non validi, termina lo stream con un errore. Una chiamata Anthropic con input iniziale non vuoto non
può fornire anche una seconda fonte JSON non vuota degli argomenti. Una chiamata interrotta prima del
completamento non viene registrata come output completo del modello.

**Registrazioni accettate dell'output del modello.** Dopo aver convalidato un gruppo di risposte,
Latet accetta le registrazioni dell'output e ne accoda il salvataggio prima di restituire l'output di
quel gruppo. Il salvataggio avviene in background: l'accettazione non prova né la consegna al client
né che i dati siano già stati salvati. Una cancellazione prima dell'accettazione non pubblica
registrazioni; dopo l'accettazione, il salvataggio può terminare anche se la consegna viene annullata.

In una singola istanza, il gateway di ogni identità ammette al massimo 32 MiB di queste registrazioni
e 256 gruppi fra le sue sessioni, contando sia il lavoro in coda sia quello in esecuzione.
Contano il testo mascherato e ripristinato,
gli identificatori delle risposte e il costo delle strutture di registrazione. È un limite al nuovo
lavoro trattenuto, non alla memoria totale del processo (RSS): la permanenza più lunga dei dati in
chiaro e il costo dell'interprete consumano comunque memoria. Se l'ammissione è piena o chiusa, o
l'accodamento fallisce, l'output di quel gruppo viene rifiutato. Il testo già trasmesso in streaming
non può essere ritirato.

Le registrazioni diventano visibili in seguito. Le richieste accodate dopo sullo stesso processo di
lavoro del gateway seguono il tentativo di registrazione. Su un server Latet ciò non garantisce che
Valkey li abbia scritti, né che un'altra istanza li veda, né il loro ordine rispetto al lavoro del
plugin di Claude Code. L'arresto può scartare le registrazioni in coda. Un errore di salvataggio dopo la consegna
produce un messaggio diagnostico con frequenza limitata, contenente solo una categoria fissa e il
tipo di eccezione; non può annullare la consegna né ripristinare atomicamente lo stato precedente.
Se mancano registrazioni, restano in vigore le normali regole di ricerca nella sessione,
mascheramento e rifiuto.

**Campi della richiesta fuori dalla conversazione.** In modalità gateway, le descrizioni degli
strumenti, gli schemi dei loro parametri e gli esempi del loro input vengono mascherati come la
conversazione, e così lo schema di un output strutturato, un output previsto e ciò che una richiesta
dice di chi la fa (`user`, `safety_identifier`, `metadata`). I nomi degli strumenti, ciò che permette
al provider di eseguire uno strumento suo (l'indirizzo e le intestazioni di un server MCP remoto, la
posizione di una ricerca web) e le altre impostazioni della richiesta vengono inoltrati così come
sono. In Claude Code il plugin maschera le descrizioni degli strumenti e il controllo finale legge
`metadata` e lo schema di un output strutturato; da un computer gli schemi dei parametri degli
strumenti vengono inviati così come sono, e un server Latet li controlla (vedi sotto). Un client
diverso da Claude Code su una via che Latet si limita a controllare (l'accesso di un abbonamento,
Bedrock, Vertex) non ha un plugin: il controllo finale legge anche le descrizioni, i parametri e gli
esempi dei suoi strumenti, e ferma una richiesta che vi lascerebbe uscire un valore in chiaro.

**Gli strumenti di Claude Code su un server.** Il plugin maschera le descrizioni degli strumenti,
incluse quelle di `tool_addition`. Un server Latet non prende i marcatori della richiesta come prova
che il plugin sia stato eseguito: il suo controllo finale legge la descrizione di ogni strumento, lo
schema dei suoi parametri e gli esempi del suo input, come per un client senza plugin, e ferma una
richiesta che vi lascerebbe uscire un valore in chiaro. Uno strumento MCP il cui schema nomina
l'indirizzo di un cliente ferma quindi le richieste di Claude Code su un server, finché il valore non
viene tolto dallo schema o consentito nelle tue impostazioni. Una definizione di strumento in una
forma che Latet non legge viene rifiutata, come per ogni client. Restano i controlli di metadati,
output strutturati e schemi di fallback descritti qui.

Per Anthropic, la lettura degli output strutturati comprende anche descrizioni, chiavi e valori degli
schemi in ogni `fallbacks[].output_config.format`. Il mascheramento protegge gli schemi sensibili;
nelle richieste solo controllate, gli schemi sensibili vengono rifiutati e i byte sicuri restano
invariati. Restano supportati i fallback che indicano solo il modello. Un server con protezione attiva
rifiuta contenitori di fallback e schemi di output in forme non valide.

**Ciò che Latet non legge, su un server Latet.** Un computer invia così com'è ciò che Latet non
legge; un server Latet lo rifiuta, con un errore che dice perché. Rifiuta una richiesta di cui Latet
non legge il corpo: verso un endpoint diverso da una chiamata al modello, come files o batches, o con
un metodo diverso da POST. Rifiuta dei contenuti in una forma che Latet non legge: un prompt di
completamento dato come token; una variabile di un prompt, una definizione di strumento o il formato
di una risposta di un tipo che non conosce; una parte di contenuto, un blocco di un messaggio o un
elemento dell'input di un tipo che non conosce, nelle chat completions come nei messaggi di Anthropic
e nella Responses API, o uno che non è un oggetto; un campo che Latet legge dato in un'altra forma,
come l'input di una chiamata a uno strumento che non è un oggetto o un prompt di sistema che non è né
testo né blocchi; e, nella Responses API, l'audio di una conversazione dal vivo (`audio`,
`output_audio`) e ciò che è stato sentito in una clip audio. Rifiuta un frame WebSocket che non è un
turno (`response.create`; `response.cancel` passa) e, su una richiesta che si limita a controllare
(Claude Code, Bedrock, Vertex), un'immagine di cui non ha potuto leggere il testo, qualunque cosa
dica `images`. Ciò che il provider ha prodotto e gli viene restituito così come l'ha prodotto passa:
il ragionamento del modello, una compattazione, le chiamate agli strumenti del provider e i loro
risultati. Lo stesso vale per ciò che non contiene nulla, come un riferimento a un elemento
precedente, e per una richiesta senza corpo, ad esempio l'elenco dei modelli.

Un server con protezione attiva rifiuta inoltre messaggi chat con `name`, blocchi Anthropic
`search_result` e blocchi documento con `title` o `context`, anche nei risultati degli strumenti e
con `images: allow`. Queste forme facoltative non sono supportate sul server neppure con valori vuoti
o null. Un documento senza titolo né contesto segue l'impostazione `images`, come ovunque. Un computer
non rifiuta queste forme, e nemmeno le maschera: il loro testo esce com'è arrivato. Un risultato di
ricerca e un documento ricevono la segnalazione *Non ispezionato*; il `name` di un messaggio chat non
viene né mascherato né segnalato, quindi tieni fuori da esso i valori che proteggi.

**La revisione della modalità auto di Claude Code.** Il revisore della modalità auto riceve il tuo nome
utente del sistema operativo e i percorsi della tua cartella home e della cartella di lavoro, più
qualsiasi nome host o autore git presente nel tuo CLAUDE.md o nell'azione, e nessun plugin può
mascherarli. Per impostazione predefinita il daemon ferma quelle revisioni, così i dettagli restano in
locale e la modalità auto non può approvare azioni da sola. `auto_mode_review` lascia passare ogni
dettaglio al revisore, e solo a lui, oppure dà al revisore un nome utente fittizio; vedi
[Claude Code](../../agents/claude-code/).

**Il tuo computer.** Latet non è una sandbox: non protegge da malware, da altri utenti del tuo account
o da un agente compromesso. Gli strumenti che l'agente esegue sul tuo computer, server MCP locali
compresi, ricevono i valori reali, così file e comandi funzionano: un comando che invia dati in rete
invia il valore reale. Esegui l'agente in una sandbox quando ne ha bisogno.

**Regole aggiunte a conversazione in corso (Claude Code).** Mettiamo che tu aggiunga `Project Falcon`
come termine personalizzato a metà di una conversazione. I contenuti nuovi vengono mascherati da quel
momento in poi, ma i turni precedenti, già arrivati ad Anthropic con il nome in chiaro, vengono
inviati di nuovo con ogni richiesta. Latet li segnala nella dashboard invece di bloccarli: sono già
stati inviati, e bloccarli fermerebbe la conversazione. Inizia una nuova conversazione per lasciarli
alle spalle.

**Dati inviati in precedenza.** Tutto ciò che è stato inviato prima dell'installazione di Latet, o
mentre era disattivato, è già presso il provider.

Vedi il [modello di sicurezza e delle minacce](../../security/) per le ragioni dietro questi limiti.
