---
title: Sicurezza e modello delle minacce
description: Da cosa difende Latet, da cosa no, e come protegge se stesso.
---

:::note[Un livello di difesa]
Latet maschera quello che i suoi rilevatori riconoscono, e alcuni agenti e alcune superfici sono
coperti per progettazione, non provati dal vivo (la pagina di ogni agente dice quali). Non farne la tua unica
protezione per dati che non devono mai uscire dal tuo computer.
:::

## Cosa protegge Latet

I segreti e i dati personali riconoscibili nel tuo spazio di lavoro e nelle tue conversazioni: chiavi
API, token, password, chiavi private, credenziali nelle stringhe di connessione, email, numeri di
telefono, IBAN, numeri di carta, numeri identificativi nazionali e fiscali, partite IVA, indirizzi IP,
i termini che aggiungi e l'identità di questo computer. La
[pagina sul rilevamento](../concepts/detection/) dice esattamente cosa viene riconosciuto.

## Confini di fiducia

In una distribuzione connessa vale anche il [confine di fiducia del server](../concepts/server/):
gli originali possono raggiungere il server e le viste autenticate della dashboard.
Gli amministratori gestiscono la configurazione del server e le impostazioni che condivide, come
le sue destinazioni e il suo revisore; impronte, credenziali e associazioni degli agenti restano
sui computer. Imposta separatamente i limiti di accesso
ai provider se il server deve essere il punto di controllo aziendale. Un server non prende i
marcatori di una richiesta come prova che il plugin di Claude Code sia stato eseguito: controlla
anche le descrizioni, gli schemi e gli esempi degli strumenti.

- **Il tuo computer** è considerato affidabile: l'agente, i suoi strumenti, il plugin e il daemon di
  Latet, e i tuoi file.
- **Il provider del modello** non è considerato affidabile per quei valori. Latet non presume nulla su
  come le richieste vengono registrate, conservate o usate una volta arrivate, quindi tiene gli
  originali lontani da lui.
- **La rete** in mezzo è protetta da TLS, come al solito.

## Nel perimetro

- La divulgazione accidentale di valori riconoscibili al provider attraverso i prompt, i file,
  l'output degli strumenti e la cronologia della conversazione.
- Le richieste che in modalità client uscirebbero con un valore sensibile in chiaro, per esempio
  attraverso una via che il plugin non copre: il controllo finale del plugin e il daemon le bloccano.
- Le pagine web che cercano di usare il daemon locale (DNS rebinding, richieste cross-site).

## Fuori dal perimetro

- **I valori che sfuggono ai rilevatori.** Vedi [Cosa è protetto e cosa no](../concepts/protection-scope/).
- **Un computer compromesso.** Un malware, altri utenti del tuo account, o un plugin o un agente
  malevolo possono leggere direttamente i tuoi file. Latet non è una sandbox.
- **Un agente deciso a ridurre la protezione di Latet, o indotto con l'inganno a farlo.** Una modifica
  alle impostazioni che riduce la protezione attende la tua approvazione, e i comandi che disattivano
  Latet si rifiutano di girare dentro un agente: una barriera contro gli errori, non una serratura. Un
  agente gira con il tuo utente, quindi uno deciso ad aggirarla, o ingannato da una prompt injection,
  può leggere quello che legge Latet e approvare la propria modifica attraverso l'interfaccia locale
  del daemon. Un vero confine richiede che Latet giri come un altro utente, o su un altro computer
  ([Latet su un server](../concepts/server/)). Vedi
  [Gli agenti e le tue impostazioni](../concepts/agents-and-settings/).
- **L'esfiltrazione attraverso gli strumenti.** Gli originali vengono rimessi negli argomenti delle
  chiamate agli strumenti, così il lavoro dell'agente è corretto su disco. Un'istruzione arrivata con
  una prompt injection potrebbe indurre l'agente a passare un valore fittizio a un comando che invia
  dati in rete, come `curl`; l'originale ripristinato uscirebbe allora attraverso quel comando. Latet
  lascia i valori fittizi negli strumenti web e nei connettori di claude.ai, ma un comando della shell
  o un server MCP locale ricevono il valore reale. Tieni attive le richieste di permesso del tuo
  agente per i comandi di rete.
- **La deduzione dal contesto.** I valori fittizi mantengono il formato dell'originale, e il testo
  intorno può rivelare a cosa serve un valore.
- **Le superfici che Latet non può vedere**: le sessioni cloud, Chat e Cowork di Claude Desktop,
  Cursor, e i risultati dei connettori di claude.ai, che tornano attraverso i server di Anthropic.

## Come Latet protegge se stesso

**Il daemon e la dashboard**

- Sono in ascolto solo su `127.0.0.1`, quindi niente sulla tua rete può raggiungerli.
- Le richieste il cui header `Host` non è un indirizzo locale vengono rifiutate, il che sventa il DNS
  rebinding.
- Le richieste che arrivano da una pagina web (con un header `Origin`, o un `Sec-Fetch-Site`
  cross-site) vengono rifiutate sui percorsi del proxy.
- La dashboard richiede un link di accesso monouso da `latet dashboard`, poi un cookie di sessione
  `HttpOnly`, `SameSite=Strict`, e ogni modifica richiede un header che gli altri siti non possono
  inviare. Vedi la [guida alla dashboard](../dashboard/#la-privacy-della-dashboard-stessa).
- Il plugin di Claude Code parla con il daemon con un token casuale, conservato in `bridge.token` nella
  cartella dei dati. Senza il token, il daemon si rifiuta di mascherare, ripristinare o controllare
  qualsiasi cosa.
- **I tuoi agenti raggiungono il daemon in semplice http su `127.0.0.1`, ed è voluto.** Quel traffico
  non raggiunge mai una scheda di rete: nessun altro computer, neanche sul tuo Wi-Fi, può vederlo. Un
  programma sul tuo computer che potesse leggerlo (con i diritti di amministratore, o con il tuo
  utente) potrebbe leggere direttamente la cassaforte e i tuoi file, quindi cifrarlo non lo
  fermerebbe. Richiederebbe un'autorità di certificazione tua, installata nel sistema operativo, la
  cui chiave, se rubata, permetterebbe a qualcuno di spacciarsi per qualsiasi sito web sul tuo
  computer. Quello che esce dal tuo computer è cifrato: le richieste al tuo provider, così come le
  invia l'agente, e al revisore locale su un altro computer (più sotto).

**Il tutorial**

- `latet tutorial` esegue la sua sandbox dentro il comando, su `127.0.0.1` e una porta libera,
  protetta come la dashboard: un link monouso, poi un cookie suo, che non prende mai il posto
  dell'accesso alla tua dashboard.
- La sandbox ha i suoi dati, le sue impostazioni e la sua cassaforte, in una cartella temporanea, e
  nessuna delle tue chiavi o dei tuoi token. Il suo unico percorso porta a un modello simulato nello
  stesso processo, quindi nessuna richiesta raggiunge un provider; quello che andrebbe all'esterno,
  come una destinazione o il server di un revisore locale, lì viene rifiutato. Vedi
  [Tutorial](../getting-started/tutorial/#una-sandbox).

**Il revisore locale su un altro computer**

- Riceve i testi originali, quindi viene raggiunto via `https://`. Fuori dalla tua rete locale non si
  accetta nient'altro.
- Un tuo server con un certificato autofirmato viene considerato attendibile in base alla sua impronta
  digitale, che confronti una volta con quella del server (`latet llm trust`, oppure *È la stessa: consideralo
  attendibile* nella dashboard). Da quel momento viene accettato solo quel certificato, controllato
  prima di inviare qualsiasi testo, e al sistema operativo non viene aggiunta nessuna autorità.
- Sulla tua rete locale `http://` è ancora accettato, per i server configurati prima: la dashboard,
  `latet doctor` e il log del daemon avvisano che i testi attraversano la tua rete senza cifratura.

**Il file di un progetto**

- Un `latet.yaml` in un repository può solo aggiungere protezione: un profilo più rigoroso, più
  termini e riconoscitori. Quello che ridurrebbe la protezione (valori da non mascherare mai, regole
  disattivate, Latet disattivato) viene ignorato, così un repository che cloni non può indebolire le
  tue impostazioni. Vedi [Progetti](../concepts/projects/#il-file-del-progetto).
- I suoi riconoscitori sono espressioni regolari eseguite da RE2, in un tempo lineare rispetto al
  testo, e il file ha una dimensione massima: un file ostile non può mandare in stallo il
  mascheramento.

**I tuoi valori**

- La cassaforte cifra ogni originale su disco con AES-GCM. La chiave è derivata da una chiave radice
  propria di ogni installazione, conservata nell'archivio delle credenziali del tuo sistema operativo
  (Gestione credenziali di Windows, il Portachiavi di macOS, il keyring del desktop su Linux), o in un
  file privato, `root.key`, quando non c'è.
- I valori fittizi sono derivati con HMAC da quella chiave propria dell'installazione: senza la
  chiave, nessuno può risalire da un valore fittizio all'originale.
- Un valore fittizio viene ripristinato solo nella conversazione a cui è stato mostrato, così un'altra
  sessione non può usare la cassaforte come oracolo.
- La cronologia della dashboard conserva le richieste come sono uscite dal tuo computer, già
  mascherate (quelle bloccate come sarebbero uscite), cifrate con la stessa chiave, per 14 giorni per
  impostazione predefinita. Imposta `history: metadata` (solo conteggi e tempi) o `history: off`
  nella [configurazione](../reference/configuration/) per conservare meno.

**Backup**

`latet backup` scrive [la tua installazione](../concepts/export-import/#la-tua-installazione-per-un-altro-computer)
in un unico file:
- **La chiave viene dalla tua passphrase.** È derivata con Argon2id (128 MiB di memoria, 3
  passaggi) e un salt casuale.
- **Il corpo è sigillato a segmenti.** È uno stream tar tagliato in segmenti da 1 MiB, ciascuno
  sigillato con AES-256-GCM. Il nonce di un segmento è il suo numero e se è l'ultimo, e l'intestazione
  del file (il suo formato, e i parametri e il salt della chiave) è autenticata con ogni segmento.
- **Cosa viene rifiutato, prima che qualsiasi cosa del file venga usata:**
  - un'altra passphrase;
  - un segmento modificato, spostato, rimosso o aggiunto;
  - un file troncato, o un'intestazione modificata;
  - parametri della chiave oltre quelli che Latet accetta (un file non può fargli usare tutta la
    memoria);
  - qualsiasi cosa al suo interno diversa dai file previsti.
- **Niente è in chiaro su disco.** I database vengono copiati così come sono (con i loro valori
  sigillati), la chiave radice esiste solo dentro lo stream cifrato, e il file viene scritto per
  intero, leggibile solo da te.
- **Con `--vault` il file contiene i tuoi valori reali e la chiave per leggerli:** conservalo come
  conserveresti i valori.

`latet upgrade` copia, prima di installare una versione, la cassaforte, la cronologia e le
impostazioni in `backups/before-<versione>-<ora>` nella cartella dei dati di Latet
([Aggiornamento](../getting-started/installation/#aggiornamento)):
- **Sigillate come la cassaforte.** I database vengono copiati così come sono, con i loro valori
  sigillati con la chiave di questo computer, che non viene copiata; la cartella è leggibile solo
  da te.
- **Seguono ciò che rimuovi**, mentre gira Latet 0.9.10 o una versione successiva.
  *Dimentica* toglie un valore da ogni copia, cancellare la cronologia
  cancella anche la loro, e la loro cronologia non viene conservata più a lungo della tua. Una copia
  che non si può ripulire viene cancellata, o messa da parte e non più proposta.
- **Tre al massimo, e tornare indietro spetta a te.** Rimettere una copia viene proposto solo in un
  tuo terminale, e un agente non può eseguire `latet upgrade --to` verso una versione più vecchia.

**Il revisore locale** (opzionale, [disattivato finché non lo attivi](../concepts/llm-review/))

- Gira su un tuo server: su questo computer, o su un altro tuo computer una volta che lo confermi
  (`remote: true`), via https fuori dalla tua rete locale. Legge il testo originale, quindi dagli solo
  un server che controlli tu.
- Può aggiungere a quello che viene mascherato, mai togliere niente; quello che trova viene verificato
  sul testo (un valore che inventa, o uno dentro un segnaposto, viene scartato).
- Un output di uno strumento con istruzioni rivolte al revisore può al massimo fargli trovare meno; i
  rilevatori si applicano comunque, e una risposta vuota a un testo che si rivolge al revisore non è
  considerata affidabile (la richiesta viene fermata).
- La chiave API del suo server è conservata nell'archivio delle credenziali del sistema operativo (o
  arriva da una variabile o da un file), viene inviata solo a quel server, e non viene mai scritta nei
  log, nella dashboard o in config.yaml. La dashboard può salvarla o rimuoverla, mai mostrarla.
- Quello che ha trovato viene memorizzato sigillato nel database della cassaforte, come gli originali
  della cassaforte.
- Gli vengono mostrate le immagini solo quando lo attivi (`llm_review.images`): mostrarle a un server
  su un altro computer attende la tua approvazione quando è un agente a chiederlo.

**Immagini** (con il [riconoscimento del testo](../concepts/images/) opzionale)

- Le immagini vengono lette sul tuo computer; i modelli vengono scaricati una sola volta, a una
  revisione fissata, ogni file verificato rispetto a uno SHA-256 fissato, e poi girano offline.
- Le immagini così come sono state inviate vengono conservate nella cronologia della dashboard,
  sigillate, solo con `history: full` (al massimo 10 MB ciascuna), e se ne vanno insieme al loro
  scambio.
- Quello che decidi su un'immagine nel Revisore Live, e un'immagine che il revisore locale ha
  trattenuto, vengono memorizzati sigillati nel database della cassaforte, tramite un hash con chiave
  dell'immagine.
- Un'immagine che non può essere letta viene trattenuta, mai inviata com'era.

**Il traffico**

- Gli header con le credenziali (`Authorization`, chiavi API, cookie e simili) vengono inoltrati senza
  modifiche ma non vengono mai scritti nei log o nella dashboard: viene registrata solo la loro
  lunghezza. Gli header di Latet stesso (`x-latet-*`, uno dei quali contiene il nome di una tua
  cartella) non escono mai dal tuo computer.
- Un'eccezione, quando usi Amazon Bedrock con il tuo login AWS: Claude Code non invia credenziali, e
  Latet firma ogni richiesta per Amazon (SigV4) con il tuo profilo AWS. Legge il profilo come fa la AWS
  CLI, tiene le credenziali solo in memoria, non le scrive mai da nessuna parte, e firma solo le
  richieste ad Amazon Bedrock. Le credenziali che si trovano solo nella tua shell (aws-vault, chiavi
  esportate) vengono passate da `latet claude` solo per la sua sessione: Latet le dimentica quando
  quella sessione termina, anche se `latet claude` viene chiuso a forza, e alla loro scadenza. Un
  percorso che firma contiene una chiave generata dal token di Latet, quindi un altro utente del tuo
  computer, o un programma che non può leggere i tuoi file, non può far firmare richieste a tuo nome;
  un programma che gira con il tuo utente può leggere quel token, come può leggere i tuoi file AWS. La
  chiave non finisce mai in un log. Vedi
  [Endpoint aziendali](../agents/company-endpoints/#amazon-bedrock).
- Un repository non può decidere dove vanno le tue richieste: Latet legge il percorso di un agente
  dalle tue impostazioni, mai da quelle di un progetto, e il plugin di Claude Code ferma i prompt
  quando le impostazioni di un progetto stabiliscono dove vanno, o un proxy per loro, anche attraverso
  Latet. Un indirizzo scritto in modo che due programmi possano leggerlo diversamente (con dentro un
  backslash, uno spazio, un nome utente) viene rifiutato, mai indovinato. Quando il plugin non può
  controllare il percorso di una sessione, i prompt di quella sessione vengono fermati.
- Le richieste di Claude Code, o che contengono il token di un abbonamento Claude, non vengono mai
  riscritte, solo controllate.
- Quando il mascheramento non riesce, la richiesta viene rifiutata invece che inviata: un corpo che
  Latet non riesce a decodificare, un rilevatore richiesto che va in errore, o un valore che non riesce
  a sostituire finiscono tutti in un errore che dice che non è stato inviato niente. In Claude Code,
  quando il daemon non è raggiungibile, il plugin trattiene i contenuti.
- Il TLS verso la destinazione viene verificato con l'archivio dei certificati del tuo sistema
  operativo, così un proxy aziendale il cui certificato radice è installato lì continua a funzionare.
- La cattura grezza degli scambi per il debug è disattivata per impostazione predefinita. Quando la
  attivi (`latet serve --capture <folder>`), gli header con le credenziali vengono oscurati, ma le
  catture contengono comunque il contenuto delle conversazioni: tienile sul tuo computer.

Il daemon non apre connessioni di rete se non per inoltrare le richieste del tuo agente al provider
(o all'endpoint aziendale) che il tuo agente usa già e, quando lo attivi, per interrogare il revisore
locale, su questo computer o su un altro dei tuoi. Quando firma per Amazon Bedrock con un profilo SSO, la AWS CLI che esegue
può contattare il servizio di accesso di AWS per rinnovare il tuo login, come fa quando la usi tu. I
comandi usano la rete solo per quello che chiedi: `latet ner install` e `latet ocr install` scaricano
i loro modelli da huggingface.co, e `latet upgrade` cerca l'ultima release su GitHub e la scarica. Non
ci sono telemetria né analytics: né nel daemon, né nella dashboard, né in questo sito di
documentazione.

**Su un server Latet** ([Latet su un server](../concepts/server/))

- L'amministrazione risponde solo su HTTPS, a un programma: una richiesta da una pagina web viene
  rifiutata, e un token di amministrazione viene controllato solo dopo che il suo indirizzo ha passato
  un limite di tentativi rifiutati.
- Il server non conserva nessun segreto che emette: solo un hash con chiave di ogni token, chiave e
  codice. La chiave di ogni spazio è sigillata con la chiave principale del server, tenuta separata
  dai backup del database.
- Ogni chiamata amministrativa viene registrata in un log a catena, anche le letture e i rifiuti;
  `latet server log verify` controlla la catena.
- Il token in mano agli agenti di un computer apre il bridge del plugin e il proxy dello spazio, mai
  la dashboard: per quella serve anche la chiave del computer, che nessun agente riceve.

## Segnalare una vulnerabilità

Per favore non aprire una issue pubblica. Usa la segnalazione privata delle vulnerabilità di GitHub
sul [repository di Latet](https://github.com/Latet-AI/latet/security).
