---
title: Gestione del server
description: Gestire archiviazione, rete, chiavi, limiti e osservabilità di un server di Latet.
---

Questi requisiti valgono per ogni istanza di un server di Latet configurato. Mantieni
coerenti indirizzo HTTPS pubblico, database PostgreSQL, Valkey e chiave principale tra
le istanze. [Latet su un server](../server/) spiega quali dati raggiungono il server
e quali restano leggibili nei suoi archivi.

## Configurazione e chiavi

`server.yaml` contiene le impostazioni dell'istanza. `--config` sceglie il file per i
comandi server; `LATET_SERVER_CONFIG` lo sceglie tramite l'ambiente. Le corrispondenti
variabili `LATET_SERVER_*` prevalgono sui valori del file. Le impostazioni dell'istanza
sono separate dalle impostazioni condivise e dalle impostazioni e regole proprie di ogni
identità, conservate in PostgreSQL.
La [tabella generata delle impostazioni del server](../../../reference/configuration/#server-settings),
in inglese, elenca ogni impostazione dell'istanza, il valore predefinito e la sua funzione.

Fornisci la chiave principale tramite `master_key_file` o l'input di ambiente supportato
per questa chiave. Preferisci un file segreto protetto; non inserirla in un repository
o nella riga di comando. Tutte le istanze richiedono la stessa chiave. Conservane un backup
separato ad accesso ristretto: sostituire il file con una nuova chiave casuale non ruota
i dati cifrati e rende illeggibili quelli esistenti. Una voce della cassaforte che non si
apre (sigillata con un'altra chiave, o modificata nel database) viene lasciata fuori: il log
ne indica l'id, mai il valore, e il mascheramento prosegue con le altre voci.

Usa `database_password_file`, `valkey_password_file`, `metrics_token_file` e
`reviewer_key_file` per i rispettivi segreti. La chiave del revisore viene letta una volta
all'avvio e inviata solo al revisore condiviso scelto dagli amministratori. Deve essere
la sua credenziale, mai una chiave principale o un token amministrativo. Il server non
cerca la chiave del revisore nell'ambiente, nel portachiavi o nella chiave locale salvata di una persona.

## Servizi di sistema

Installa il pacchetto server in un percorso attendibile a livello di sistema, fuori
dalle cartelle personali. Prepara `server.yaml` con URL, percorsi dei file segreti e
configurazione TLS descritti sopra; il servizio deve poter leggere i file protetti.
Crea prima la chiave principale e inizializza dal tuo terminale amministrativo privato:
il primo token amministrativo viene stampato una volta. Non conservarlo in log condivisi.

Su Linux servono prima alcune cose, da root. Installa Latet con l'extra `server` per tutta la
macchina: le cartelle di uv per strumenti, comandi e Python tutte fuori dalle cartelle
personali. Crea l'utente e il gruppo `latet` con cui gira il servizio, poi la cartella che
contiene le impostazioni e i segreti, con dentro la chiave principale e la password del database:

```sh
UV_TOOL_DIR=/opt/latet/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/latet/python \
  uv tool install --python ">=3.12,<3.15" "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin latet
install -d -m 0750 -o root -g latet /etc/latet
(umask 077 && openssl rand -base64 32 > /etc/latet/master.key)
(umask 077 && openssl rand -hex 32 > /etc/latet/db.password)   # or the password your database administrator gives
```

Metti la password di Valkey in `/etc/latet/valkey.password`, e il certificato TLS con la sua chiave
in `/etc/latet/server.crt` e `/etc/latet/server.key`. Poi scrivi `/etc/latet/server.yaml`; ecco
uno minimo per un servizio sull'host:

```yaml
url: https://latet.acme.lan
database_url: postgresql://latet@db.acme.lan:5432/latet?sslmode=verify-full&sslrootcert=system
database_password_file: /etc/latet/db.password
valkey_url: rediss://valkey.acme.lan:6379/0
valkey_password_file: /etc/latet/valkey.password
master_key_file: /etc/latet/master.key
certificate: /etc/latet/server.crt
key: /etc/latet/server.key
```

Assegna `server.yaml` e ogni file segreto che indica, `key` compresa, a root e al gruppo `latet`,
leggibili solo da loro (`0640`, oppure `0440`):

```sh
cd /etc/latet
chown root:latet server.yaml master.key db.password valkey.password server.key
chmod 0640 server.yaml master.key db.password valkey.password server.key
```

Aggiungi `ner` e `ocr` all'extra per il rilevamento dei nomi e il riconoscimento del testo.
L'installazione del servizio dice quale di queste cose manca: un'installazione sotto una
cartella personale, nessun utente o gruppo `latet`, oppure un file con un altro proprietario
o un'altra modalità.

La configurazione predefinita è `/etc/latet/server.yaml`, l'unità è `latet-server` e la
cartella dei dati è `/var/lib/latet`:

```sh
latet server init --config /etc/latet/server.yaml
latet server service install --config /etc/latet/server.yaml --print
latet server service install --config /etc/latet/server.yaml
systemctl start latet-server
```

Su Windows la configurazione predefinita è `C:/ProgramData/Latet/server.yaml`.
Il servizio gira come `NT SERVICE\LatetServer`, con ACL protette sulle cartelle.
Il log è `C:/ProgramData/Latet/logs/server.log`, con rotazione su cinque file da 10 MiB:

```powershell
latet server init --config C:/ProgramData/Latet/server.yaml
latet server service install --config C:/ProgramData/Latet/server.yaml --print
latet server service install --config C:/ProgramData/Latet/server.yaml
Start-Service LatetServer
```

`--print` mostra il piano senza installare. L'installazione richiede root su Linux o
privilegi amministrativi su Windows e abilita il servizio all'avvio; avvialo esplicitamente
quando sei pronto. Le invocazioni degli agenti sono rifiutate anche con `--print` o
`--yes`. Dal tuo terminale l'installazione chiede conferma prima di cambiare qualcosa, con `--yes`
o senza; `--yes` permette a uno script tuo, senza terminale, di installare senza la domanda.
L'installazione in un container è rifiutata. Questi comandi descrivono la procedura, non un
servizio già installato sul computer.

## Il profilo server di Compose

Usa `compose.yaml` e `deploy/Caddyfile` del repository. Prepara una cartella privata
`secrets/` sull'host: su POSIX appartiene all'amministratore e ha modalità `0700`;
i singoli file montati hanno modalità `0444`, così l'utente di ogni container può leggerli.
Su Windows limita le ACL di cartella e file agli amministratori e all'account del
servizio Docker che deve accedervi. I segreti montati da Compose non rimappano proprietario
o modalità; impostare tutti i file a `0600` può renderli illeggibili nei container.

Crea nella cartella `master.key` (32 byte casuali in base64), `db-admin.password`,
`db.password`, `valkey.password` e `metrics.token` prima di inizializzare. Usa valori
casuali robusti; `db.password` e `valkey.password` devono essere esadecimali per i comandi
di avvio forniti (`openssl rand -hex 32`). Non sostituire una chiave principale esistente
per semplificare la configurazione. L'opzionale `reviewer.key` richiede di decommentare
insieme impostazione del file, montaggio e definizione del segreto. Questi segreti
provengono solo da file, senza ripiego su variabili d'ambiente. Fornisci i file TLS in
`certs/` e proteggi la chiave privata.

Le istanze si collegano a PostgreSQL come ruolo `latet`, che non è un superutente. Su un
volume di dati nuovo, `deploy/postgres-init.sh` lo crea con la password di `db.password`,
come proprietario del database `latet`. Il superutente di PostgreSQL, `postgres`, ha
`db-admin.password`, che nessuna istanza riceve: un'istanza compromessa non può eseguire un
programma nel container del database (`COPY … PROGRAM` richiede un superutente). Lo script
gira solo finché il volume è nuovo e non modifica un database esistente. Un'installazione il
cui volume è stato creato prima tiene `latet` come superutente di PostgreSQL, un attributo che
PostgreSQL non lascia mai perdere a quel ruolo (`ALTER ROLE latet NOSUPERUSER` viene rifiutato):
per passare alla nuova configurazione, esporta il database con
`pg_dump --no-owner --no-privileges`, avvia PostgreSQL su un volume nuovo e ripristina
l'esportazione come `latet`.

Scrivi l'indirizzo del server in un file chiamato `.env` accanto a `compose.yaml`, che Compose
legge per ogni comando, così che un aggiornamento eseguito più tardi da un altro terminale lo
mantenga (non contiene segreti):

```sh
printf 'LATET_SERVER_URL=https://latet.acme.lan\nLATET_SERVER_HOST=latet.acme.lan\n' > .env
```

Questo esempio per shell POSIX sceglie il progetto `latet`, con rete interna
`latet_latet-back`. L'inizializzazione usa `--log-driver none` di Docker perché stampa
il token amministrativo; mantieni privato il terminale e trasferisci il token nel tuo
archivio di segreti. Verifica che l'inizializzazione sia riuscita prima di avviare le istanze:

```sh
# --wait returns only once PostgreSQL and Valkey are healthy (at most 120 s): go on only if it succeeds
docker compose -p latet --profile server up -d --wait --wait-timeout 120 postgres valkey
docker run --rm --log-driver none --network latet_latet-back \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=bind,source=$PWD/secrets/master.key,target=/run/secrets/latet-master-key,readonly" \
  --mount "type=bind,source=$PWD/secrets/db.password,target=/run/secrets/latet-db-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/valkey.password,target=/run/secrets/latet-valkey-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/metrics.token,target=/run/secrets/latet-metrics-token,readonly" \
  --env LATET_SERVER_URL=https://latet.acme.lan \
  --env LATET_SERVER_DATABASE_URL=postgresql://latet@postgres:5432/latet \
  --env LATET_SERVER_DATABASE_PASSWORD_FILE=/run/secrets/latet-db-password \
  --env LATET_SERVER_DATABASE_PRIVATE_NETWORK=1 \
  --env LATET_SERVER_VALKEY_URL=redis://valkey:6379/0 \
  --env LATET_SERVER_VALKEY_PASSWORD_FILE=/run/secrets/latet-valkey-password \
  --env LATET_SERVER_VALKEY_PRIVATE_NETWORK=1 \
  --env LATET_MASTER_KEY_FILE=/run/secrets/latet-master-key \
  --env LATET_SERVER_METRICS_TOKEN_FILE=/run/secrets/latet-metrics-token \
  ghcr.io/latet-ai/latet:latest server init
```

Quando l'inizializzazione termina con successo, avvia le istanze e Caddy:

```sh
docker compose -p latet --profile server up -d latet-server-1 latet-server-2 caddy
```

Nel profilo server solo Caddy pubblica una porta: `443`. Il servizio separato per computer
`latet` resta nel file Compose; l'elenco esplicito sopra non lo avvia. La rete frontale è
`172.28.0.0/24`, con assegnazione automatica da `172.28.0.128/25`; l'indirizzo fisso
di Caddy `172.28.0.2` è fuori da quell'intervallo ed è l'unico proxy attendibile.
La rete posteriore è interna.

Caddy usa i file TLS forniti, un limite di richiesta di **32 MiB**, inoltro immediato
degli stream e upgrade WebSocket. Lascia disattivati i log di accesso e conserva
l'esclusione globale `log default { exclude http.log.error }`: gli errori del proxy
possono altrimenti registrare URI e header di autenticazione personalizzati.
Caddy non include un limitatore di frequenza; i limiti per client spettano a un proxy
esterno o a un WAF.

I container server usano log JSON-file limitati e non bloccanti. Le istanze girano con
l'utente non privilegiato dell'immagine, uid e gid `10001`, mai come root. Hanno root
in sola lettura, capability rimosse e `/tmp` temporanea; questo esempio non impone un
limite di memoria alle istanze in produzione. Dimensiona spazi, modelli e OCR concorrente
senza copiare i limiti degli smoke test. Valkey ha memoria limitata con `noeviction`.
L'immagine include gli avvisi di terze parti.

## PostgreSQL e Valkey

Per PostgreSQL fuori dalla macchina dell'istanza, usa TLS verificato nell'URL di
connessione, per esempio `?sslmode=verify-full&sslrootcert=system`, oppure indica il file
del certificato attendibile. `verify-full` senza `sslrootcert` qui non basta. URL senza
host e connessioni tramite nome di servizio vengono rifiutati; le variabili di ambiente
`PG*` non completano le impostazioni mancanti. Codifica con percent-encoding i caratteri
riservati della password negli URL, oppure usa il file della password. Il timeout di
connessione non comprende la risoluzione DNS.

Usa Valkey 7.2 o successivo, oppure Redis 7, come unico server logico; sono possibili
repliche, mentre Cluster non è supportato. Gli script operano su più prefissi di chiave.
Imposta `maxmemory-policy` su `noeviction`, assegna memoria per gli spazi previsti e
controlla la capacità. Perdere stato di coordinamento non equivale a eliminare una cache applicativa innocua.

Richiedi autenticazione e TLS verificato (`rediss://`) quando Valkey non è sulla macchina
dell'istanza. Le eccezioni `database_private_network`, `valkey_private_network` e
`valkey_unauthenticated` sono decisioni esplicite di configurazione per servizi raggiungibili
solo dalle istanze previste; non usarle per nascondere errori di certificato o autenticazione su una rete condivisa ordinaria.

Prima di usare questa ricetta, verifica che l'utente Valkey `latet` non esista. La ricetta
crea un utente nuovo; se esiste già, fermati e fai verificare all'operatore ACL, password
e selettori esistenti. Applicare il comando a un utente esistente aggiunge regole alla sua
configurazione; questa ricetta non la reimposta e non esegue la rotazione delle password.

Assegna all'applicazione un utente Valkey dedicato con questa ACL. Usa il namespace
predefinito `latet:`; sostituisci il segnaposto `password` tramite una connessione
amministrativa protetta. È un comando Valkey, non della shell: evita che il segreto reale
finisca negli argomenti della shell, nella cronologia o nei repository.

```text
ACL SETUSER latet on >password ~latet:* resetchannels &latet:identity:* &latet:approvals:* &latet:feed:* &latet:shared &latet:identities &latet:signins &latet:beat +@all -@dangerous -monitor -config -keys -debug
```

Mantieni letterali i pattern delle sottoscrizioni: richiedono esattamente quei permessi.
Gli script Lua e `SCAN` restano disponibili per coordinamento e pulizia selettiva;
`MONITOR`, `CONFIG`, `KEYS` e `DEBUG` sono negati. Un test automatico isolato verifica
questa ricetta sostituendo solo utente, password e namespace; non esamina né configura
il servizio della tua installazione.

Esegui le migrazioni come proprietario dello schema. La prima migrazione crea lo schema
e richiede `CREATE` sul database; quelle successive non richiedono quel privilegio di
creazione. Il blocco delle migrazioni attende al massimo 600 secondi. Un ruolo applicativo
senza `DELETE` su `audit` e `audit_anchor` non può eliminare le voci scadute del registro
amministrativo: concedi quei diritti oppure mantieni il ruolo del proprietario per le
istanze. Conserva un backup recuperabile prima di un aggiornamento. Un pooler di
connessioni deve preservare le `options` di avvio richieste da PostgreSQL.

### Dimensionare Valkey

I budget in byte per identità sono 32 MiB per i testi come sono usciti, 64 MiB per la
coda delle approvazioni e 16 MiB per gli eventi del feed conservati per riferimento.
La somma, **112 MiB, non è un tetto alla memoria totale**. Altre famiglie hanno limiti
sul numero di voci: conta anche la dimensione dei record. Le conversazioni ammettono
1.000.000 di campi e testi approvati distribuiti su non più di 10.000 conversazioni.
Le marcature dei blocchi controllati e registrati ammettono 200.000 voci ciascuna; i
testi inversi e le associazioni delle risposte 50.000 ciascuno, le sessioni AWS 200 e
le sessioni della dashboard 10 per persona.
I testi inversi sono risposte del modello conservate per riconoscerle quando tornano
in una richiesta successiva.

Questi sono esempi di pianificazione, non misurazioni. Si ipotizzano 256 byte allocati
per ogni voce di conversazione e 192 per ogni marcatura. Le altre dimensioni indicano
il testo in chiaro codificato: le stime aggiungono i 30 byte della struttura di cifratura,
l'arrotondamento dell'allocazione e 256 byte stimati per chiave e indice. I tre budget
in byte comprendono già questi costi.

| Famiglia | Esempio di carico moderato | Esempio a conteggi saturi, stesse dimensioni |
| --- | --- | --- |
| Testi / coda / eventi | 8 / 8 / 1 MiB | 32 / 64 / 16 MiB |
| Voci delle conversazioni | 20.000 da 256 byte | 1.000.000 da 256 byte |
| Marcature controllate / registrate | 10.000 ciascuna da 192 byte | 200.000 ciascuna da 192 byte |
| Testi inversi | 5.000 da 2 KiB | 50.000 da 2 KiB |
| Associazioni delle risposte | 5.000 da 64 byte | 50.000 da 64 byte |
| Sessioni AWS | 10 da 2 KiB | 200 da 2 KiB |
| Sessioni della dashboard | 2 da 128 byte | 10 da 128 byte |
| **Subtotale stimato per identità** | **40,76 MiB** | **581,75 MiB** |

Con una riserva illustrativa del 50% diventano 61,13 e 872,62 MiB per identità, oppure
circa 5,97 e 85,22 GiB per 100 identità. Prevedi separatamente memoria di base di Valkey,
connessioni, replica/persistenza e strutture residue o frammentazione che superino tale
riserva. Conta le identità i cui dati sono ancora conservati, non solo quelle che fanno
richieste ora. Cache dei processi applicativi e modelli OCR richiedono RAM separata.

L'esempio saturo **non è il caso peggiore assoluto**: i record piccoli restano un'ipotesi.
La coda di scrittura in attesa da 4 MiB non limita a 4 MiB la famiglia persistente dei
testi inversi. Una successione di record prossimi a 4 MiB può avvicinarsi a 195 GiB per
50.000 soli record inversi, prima delle altre famiglie. È una stima prudenziale ricavata
dal sorgente, non un carico misurato o una quota totale. Anche una chiave di conversazione
o una cartella lunga può superare la dimensione ipotizzata. Mantieni `noeviction`, osserva
dimensioni e occupazione effettive e dimensiona per il carico ammesso: i soli conteggi
non garantiscono un piccolo tetto di memoria per identità.

Latet 0.11 aggiunge tre tipi di chiavi, ciascuno limitato, e nessuno contiene un testo, un valore o un
valore fittizio:

- **Ciò che il provider ha dato a una conversazione** (firme del ragionamento, ragionamento cifrato,
  compattazioni), come hash con chiave, perché il turno successivo passi su qualsiasi istanza: al
  massimo 20.000 voci e 4 MiB per identità, e 256 MiB per l'intero server, conservati una settimana
  dopo l'ultimo uso. Una scrittura che Valkey rifiuta (un limite, o `noeviction` senza memoria) non
  registra niente: un elemento così viene allora lasciato passare dalla regola integrata per i campi
  cifrati del provider dove si applica, oppure rifiutato, mai con una risposta `503`.
- **Le chiavi di avvio** degli agenti avviati da Latet sui computer connessi: un record sigillato di
  al massimo 16 KiB per ogni avvio aperto in quel momento, al massimo 64 alla volta per identità.
- **Le eccezioni di richiesta usate con la cronologia disattivata**: una riga per istanza e identità,
  con soli id e numeri, aggiornata quando l'istanza controlla i suoi spazi e cancellata 30 secondi
  dopo che l'istanza si ferma. Valkey come lo fornisce il profilo Compose non conserva niente su disco
  (`save ""`, `appendonly no`); un operatore che ne attiva la persistenza scrive questi conteggi su
  disco.

## HTTPS, proxy e capacità

Termina TLS nell'istanza o nel bilanciatore di carico. Imposta `forwarded_allow_ips` sui
proxy effettivamente attendibili, mai `*`; gli header inoltrati da altri indirizzi non
sono considerati attendibili. Mantieni gli orologi sincronizzati, entro 60 secondi.
Limita separatamente l'accesso diretto ai fornitori se il server è il punto di controllo obbligatorio dell'azienda.

Il limite predefinito del corpo della richiesta è 32 MiB. `requests_in_flight` vale 16
per identità su un'istanza, con quote separate per richieste alle destinazioni, alla
dashboard, al bridge e all'API per le applicazioni. Long polling e flussi hanno limiti propri. Un rifiuto per capacità
richiede di controllare carico e limiti, non di disabilitare la protezione. All'arresto
un'istanza ha 30 secondi per terminare; i flussi lunghi possono interrompersi, quindi
i client devono gestire la disconnessione.

Un'istanza fa la maggior parte del suo lavoro su un solo core del processore. Una macchina con più
core serve di più con più worker su Linux ([sotto](#più-istanze-su-una-macchina)), e un server serve
di più con più istanze dietro il bilanciatore di carico.
[Quanto è veloce](../../applications/#quanto-è-veloce) riporta quanto hanno mascherato un'istanza e
più worker attraverso l'API per le applicazioni sul computer di misura.

Latet 0.11 legge ogni parte di ogni richiesta che inoltra
([Passare alla 0.11](../../getting-started/upgrading-to-0-11/)), e questo costa tempo su ciascuna.
Misurato con un'istanza su Windows su quel computer, come mediana, rispetto alla stessa versione senza
quella lettura: una piccola richiesta di chat ha richiesto 6,99 ms invece di 5,95, la prima richiesta
di una chiave nuova 104 ms invece di 97, un corpo JSON da 1 MiB 391 ms invece di 373, una richiesta da
4.000 slot 1.088 ms invece di 779, e una richiesta di 500 testi già mascherati e uno nuovo 183 ms
invece di 158.

L'OCR usa lettori limitati, ognuno con i modelli in memoria. `ocr_readers` vale 2 e non può
superare i processori disponibili per l'istanza (con più worker, la parte di un worker: i processori
della macchina divisi per i worker). `ocr_read_seconds` vale 30;
`ocr_wait_seconds` vale 35 e non può essere inferiore al tempo di lettura. Aumentare i
lettori costa memoria oltre alla CPU. Un'immagine non letta viene trattenuta oppure la
richiesta verificata viene rifiutata, secondo il percorso protetto; aumentare i timeout non migliora l'accuratezza del riconoscimento.

Lo spazio di una chiave API tiene in memoria i valori usati più di recente dalla chiave
(`key_working_values`, 5.000 per impostazione predefinita, circa 10 MiB per chiave), le impronte
delle parole di tutta la sua cassaforte (circa 15 MB per un milione di valori) e, per sempre, i suoi
segreti brevi più recenti che nessuna parola di un testo trova quando altre lettere vi sono attaccate
(`key_held_secrets`, 1.000 per impostazione predefinita, circa 23 KB ciascuno, per lo più nel
rilevatore che li trova). Ogni istanza e ogni worker che serve la chiave tiene tutto questo. Mentre
costruisce l'indice delle impronte di una cassaforte, un'istanza tiene di più per un momento: 49 MB in
più per 1.000.000 di valori, per circa 1,4 secondi, dentro i 64 MB che mette da parte prima, e al
massimo due costruzioni alla volta. Aumenta `key_held_secrets` per una chiave che vede molti segreti
brevi, come un token di sessione in ogni riga di log: ogni testo che la chiave maschera o ripristina
viene letto per cercare ognuno di quelli tenuti, quindi anche le sue chiamate rallentano un po' (il
ripristino circa un decimo più lento con 1.000 tenuti, misurato con Latet 0.10.0)
([la cassaforte di una chiave](../../applications/#la-cassaforte-di-una-chiave)).

Lo spazio di una persona costruito quando la cassaforte ha 35.000 valori o meno la tiene intera
in memoria
(circa 1,6 KB per valore su ogni istanza che serve la persona, contati nella memoria dello
spazio): i testi della persona vengono mascherati senza chiedere i loro valori a PostgreSQL,
circa due volte più in fretta che cercandoli. Ogni richiesta chiede comunque prima a
PostgreSQL che cosa hanno cambiato le altre istanze, così un valore salvato o dimenticato su
un'istanza è visto dalla richiesta successiva su qualunque altra. Oltre i 35.000 valori, e in uno spazio costruito oltre quel limite finché non viene ricostruito
(anche dopo che dei valori sono stati dimenticati), lo spazio di una persona funziona come quello di una
chiave API: tiene in memoria i valori usati
più di recente dalla persona (`key_working_values`) e, per sempre, ogni valore che nessuna
parola di un testo trova, qualunque sia la sua età, e cerca ogni altro valore in PostgreSQL
quando un testo ne ha bisogno, così nessuna istanza tiene intera una cassaforte grande. Una
cassaforte salvata da una versione precedente di Latet viene servita dalla memoria, come
prima, finché le sue parole non vengono create, una sola volta, in background su un'istanza;
lo spazio costruito dopo lavora su PostgreSQL.

Misurato con Latet 0.11 sul computer di [quanto è veloce](../../applications/#quanto-è-veloce), sul motore (il
solo mascheramento del gateway, non attraverso HTTP), una riga alla volta, a turni di 500 righe alternati con la
cassaforte di Latet 0.10.0 nello stesso processo, cinque ciascuno (mediane dei turni): la cassaforte di una
persona da 20.000 valori, tenuta intera, ha mascherato 851 righe al secondo contro 558 della 0.10.0, 1,52 volte
dove questa versione si proponeva di arrivare al doppio; una da 5.000 valori 885 contro 1.094, 0,81 volte,
perché la lettura del contatore delle modifiche fatta prima di ogni riga, circa mezzo millisecondo lì, è una
parte grande del tempo di una riga; una da 200.000 valori, sul suo archivio, 385 contro 59. A 200.000 valori lo
spazio di una persona occupava 24 MB in più dopo le sue righe, dove la cassaforte della 0.10.0 ne occupava 495.

Lo spazio di una persona tiene ogni valore che nessuna parola di un testo trova quando altre
lettere vi sono attaccate, qualunque cosa dica `key_held_secrets` (circa 23 KB ciascuno su
ogni istanza). Una richiesta i cui testi superano i limiti di una ricerca, come un lockfile o
un lungo log di hash, non viene mai rifiutata: lo spazio di una persona che tiene intera la
sua cassaforte la legge dalla memoria; uno più grande legge una volta la sua intera
cassaforte, condivide quella lettura con le richieste che ne hanno bisogno nello stesso
momento e la conta nella memoria dello spazio finché dura (a 200.000 valori, misurato con Latet
0.11, le prime 500 righe oltre un limite hanno richiesto circa 10 secondi, lettura compresa, e la
lettura occupava circa 380 MB sull'istanza o sul worker che l'ha fatta, circa quanto la cassaforte
della 0.10.0); lo spazio di una chiave API cerca le parole dei suoi testi come faceva Latet 0.10.2.

### Connessioni, limiti e fiducia

- **Le credenziali rifiutate** contano sull'indirizzo da cui arrivano: un indirizzo IPv4 o, per IPv6, il suo /64
  e, insieme, il suo /48. Una credenziale trovata valida nell'ultima ora, e un link di accesso emesso dal server,
  non contano per quel limite, quindi i tentativi falliti di un vicino dietro lo stesso NAT trattengono solo le credenziali
  non viste valide di recente, finché i tentativi continuano; un proxy o un firewall davanti che limita ogni client
  lo evita. Mentre i thread che controllano le credenziali sono occupati, le registrazioni dei computer e le
  credenziali nuove per ogni istanza ricevono 503 con `Retry-After`, e un link di accesso alla dashboard viene
  mandato a `?login=busy`.
- **Il token e la chiave di un computer:** il token che gli agenti di un computer tengono apre il bridge del
  plugin, il proxy dello spazio e le due chiamate del computer all'avvio di un agente (una destinazione
  aggiunta per un indirizzo, che non riduce nulla, e il valore fittizio del nome utente), dice di chi è e
  revoca se stesso; un link di accesso alla dashboard e una
  modifica dell'identità della persona chiedono anche la chiave del computer (header `x-latet-computer-key`),
  che resta nell'archivio delle credenziali di quel computer. Una chiave mancante o sbagliata viene rifiutata
  (403 `computer_key_required`) e conta sul suo indirizzo come una credenziale rifiutata.
- **Un'identità** ha al massimo `requests_in_flight` richieste in corso su un'istanza (16 per impostazione
  predefinita); oltre, 429 con `Retry-After`. Un corpo viene letto fino a `max_request_mb` (32 MiB per impostazione
  predefinita), una chiamata dell'API per le applicazioni fino a `apps_request_mb` (8 MiB); un corpo JSON vicino a quel limite tiene il lock dell'interprete da uno a tre secondi ovunque venga
  analizzato, quindi tieni il limite basso quanto i tuoi agenti permettono ed esegui più istanze.
- **PostgreSQL:** ogni istanza tiene fino a `database_pool` connessioni (10 per impostazione predefinita), per
  il lavoro di ogni identità, e ogni worker è un'istanza; il `max_connections` di PostgreSQL deve coprire il
  pool di ogni istanza e i comandi degli amministratori. Mentre costruisce l'indice di una cassaforte, di una
  chiave API o di una persona, un'istanza apre anche fino a due connessioni sue, chiuse alla fine della
  costruzione: contale anche nel `max_connections`. Se non ce ne sono di libere, la costruzione usa una
  connessione del pool. Una chiamata all'API per le applicazioni che conserva valori nuovi ne tiene una finché
  non finisce, al massimo tre decimi del pool (tre su dieci); oltre, le chiamate conservano ogni valore nuovo
  subito, più lentamente. Aumenta `database_pool`, e con esso `max_connections`, per un'istanza che maschera per
  molte chiavi nuove alla volta.
- **Valkey:** ogni istanza, e anche ogni worker, tiene fino a `valkey_connections` connessioni (100 per
  impostazione predefinita), e oltre una chiamata attende al massimo cinque secondi. Il `maxclients` di Valkey
  deve coprire le connessioni di tutte le istanze. La connessione a Valkey, il suo TLS e la sua prima risposta
  condividono un unico limite di cinque secondi.
- **Memoria:** ogni istanza, e anche ogni worker, dà agli spazi che tiene 2 GiB, pesati dai conteggi che le loro
  parti tengono anziché letti dal processo. Uno spazio ne prende al massimo la metà, e i suoi rilevatori fino a
  un ottavo di quella metà in più (128 MiB), che uno spazio occupato tiene mentre le sue richieste sono in corso:
  più spazi occupati possono superare i 2 GiB di tanto ciascuno. Il processo stesso, i modelli di riconoscimento
  e gli indici in costruzione vanno in più. Un worker viene riavviato quando termina o resta in silenzio, mai per
  la sua memoria: pianifica la memoria della macchina per N worker
  ([più istanze su una macchina](#più-istanze-su-una-macchina)).
- **I certificati dei fornitori:** un'istanza si fida dell'archivio dei certificati del sistema operativo; su
  Linux, i percorsi predefiniti di OpenSSL, che `SSL_CERT_FILE` e `SSL_CERT_DIR` cambiano per tutto il processo.
  Cambiali solo per aggiungere l'autorità della tua azienda.
- **`SSLKEYLOGFILE`** non deve essere impostata per un'istanza: le connessioni TLS che la leggono, tra cui
  quella verso Valkey, scrivono in quel file le chiavi delle loro sessioni, e chi lo legge legge quel traffico.
  Un'istanza avviata con questa variabile lo segnala nel suo log.

## Aggiornare e aggiungere istanze

Dalla 0.10 alla 0.11, leggi prima [Passare alla 0.11](../../getting-started/upgrading-to-0-11/): la 0.11
rifiuta le parti delle richieste che non legge, su un server come su un computer, e `latet doctor`
elenca ciò che si fermerà.

La 0.11 cambia anche le tabelle, quindi il server si ferma per la sua migrazione:

1. Fai la copia del database descritta sotto, ed esegui `latet server migrate --check` con la 0.11:
   dice che le tabelle cambiano.
2. Ferma ogni istanza, ogni worker di ogni macchina. Un server ancora alla 0.10 risponde a
   `latet server status` con la sola istanza che ha risposto, perché la 0.10 non riporta le sue
   istanze: la verifica che non ne resti nessuna è quella del passo successivo.
3. Aspetta 30 secondi, poi esegui `latet server migrate` una volta, con la 0.11: un'istanza della
   0.10 lascia la sua chiave in Valkey quando si ferma, e conta come attiva finché la chiave non
   scade, 30 secondi dopo il suo ultimo battito. Finché un'istanza della 0.10 o della 0.11 è attiva,
   non migra nulla, dice quante sono attive e di quale versione (`1 instance of the server is alive
   (1 of Latet 0.10 or older): nothing was migrated`) ed esce con 2: fermale ed eseguilo di nuovo.
   Con una migrazione da applicare, né il comando né `latet server init` migrano nulla nemmeno finché
   Valkey non risponde. La migrazione aggiunge a ogni riga di parola di ogni cassaforte un posto vuoto
   per la sua impronta e per il conteggio della modifica che l'ha scritta per ultima, con i loro
   indici: su un database con casseforti grandi richiede più tempo delle migrazioni precedenti, e le
   impronte stesse vengono calcolate dopo, in background (sotto). Una volta applicate le migrazioni,
   chiede di nuovo a Valkey, e quando un'istanza è diventata attiva nel frattempo lo dice dopo ciò che
   ha applicato ed esce con 1 (`The tables are migrated, and 1 instance of the server is alive (1 of
   Latet 0.10 or older): stop it, then start it with this version of Latet`).
4. Avvia ogni istanza con la 0.11. Tra l'arresto e questo avvio nessuna chiamata riceve risposta.

Poi, in background: le impronte di ogni cassaforte conservata dalla 0.10 vengono calcolate dalla
prima istanza che ne costruisce lo spazio, una volta, un'istanza alla volta, e ogni istanza costruisce
il proprio indice delle impronte, per la cassaforte di una persona come per quella di una chiave API.
Fino ad allora le richieste e le chiamate di quell'identità ricevono risposta cercando le loro parole
in PostgreSQL, come faceva la 0.10, più lentamente, e nessuna viene rifiutata per questo. Un'istanza
il cui indice non si può costruire (le impronte non ancora tutte calcolate, PostgreSQL o Valkey che non
rispondono, un aggiornamento dell'indice fallito) tiene quell'identità sulla ricerca in PostgreSQL e
costruisce di nuovo l'indice: subito quando finisce il calcolo delle impronte, su quell'istanza o su
un'altra, altrimenti 10, 20 e 30 secondi dopo ogni tentativo e poi una volta al minuto, due costruzioni
alla volta su ogni istanza, la successiva appena una finisce. Il log dice una volta quando l'indice di
uno spazio smette di essere usato (`the vault index of <id> is not used (<why>): its lookups go to
PostgreSQL until it is built again`) e una volta quando viene usato di nuovo. Una cassaforte oltre
16.777.216 impronte, circa otto milioni di valori, non ha indice finché il suo spazio dura: le sue
ricerche vanno a PostgreSQL, e un nuovo spazio ci riprova una volta. La cassaforte di una persona
conservata prima di Latet 0.10.0, i cui valori non hanno ancora parole, viene tenuta intera in
memoria, come prima, finché le sue parole e le sue impronte non vengono create, una volta, su
un'istanza; lo spazio della persona costruito dopo lavora su PostgreSQL. La 0.11 non rifà le parole
di nessuna chiave ([aggiornare un server](../../applications/#aggiornare-un-server)).

Latet legge ogni richiesta secondo il profilo del suo percorso, che nomina ogni campo, intestazione e
parametro di query che porta. A ogni rilascio di Latet, e almeno ogni due settimane, le versioni più
recenti degli agenti che Latet supporta passano per le richieste che questi profili descrivono. In
una richiesta che Latet si limita a verificare (Claude Code, Bedrock, Vertex), un membro nuovo del
corpo che contiene testo, un'intestazione `anthropic-*` nuova o un valore nuovo di `anthropic-beta`
passa una volta letto dal controllo finale, e un'intestazione `x-claude-code-*` nuova viene tolta.
Quando l'aggiornamento di un agente invia qualsiasi altra cosa che nessun profilo nomina, la nuova
revisione del profilo esce in una versione di correzione; finché non la installi, quella richiesta
viene rifiutata, nominando la sua categoria, e non ne parte nulla. Tieni le istanze sull'ultima
versione di correzione della loro versione.

Le istanze condividono le stesse tabelle, e un'istanza parte solo su tabelle della sua versione. Il
database è tuo da custodire: Latet non ne copia nulla. Prima di scaricare una nuova immagine e di
eseguire `latet server migrate`, fanne una copia con `pg_dump`, che resta coerente anche mentre le
istanze scrivono:

```sh
pg_dump --format=custom --file=latet-before-upgrade.dump "postgresql://latet@db.acme.lan/latet"
```

Per tornare indietro, ripristina quella copia (`pg_restore --clean --if-exists`) e avvia l'immagine
precedente: un'istanza non parte su tabelle di una versione più nuova. I valori nella copia restano
cifrati, come nel database: solo la chiave master del server li apre. Prima di un aggiornamento, chiedi alla
nuova versione se cambia le tabelle:

```sh
latet server migrate --check
```

- **Le tabelle restano uguali:** sostituisci le istanze una alla volta dietro il bilanciatore. Un'istanza
  fermata completa le sue richieste entro 30 secondi.
- **Le tabelle cambiano:** ferma tutte le istanze, esegui `latet server migrate` una volta con la nuova
  versione, poi avvia le istanze con quella. `latet server migrate` si rifiuta finché un'istanza è attiva (una
  della 0.10 per 30 secondi dopo il suo arresto), e un'istanza non parte su tabelle di un'altra versione.
- **I programmi usano l'API per le applicazioni:** ferma ogni istanza più vecchia prima che partano
  quelle nuove, anche quando le tabelle restano uguali. Mentre due versioni girano insieme, un valore
  che un'istanza più vecchia scrive nella cassaforte di una chiave può non essere trovato da una più
  nuova finché lo spazio della chiave non viene ricostruito
  ([aggiornare un server](../../applications/#aggiornare-un-server)).
- **Una versione che rifà le parole di una chiave** (il changelog lo dice; la 0.11 no): la prima istanza che
  costruisce lo spazio di ogni chiave API ricalcola le parole della sua cassaforte prima di rispondere alla
  prima chiamata di quella chiave, cosa che a Latet 0.10.0 richiedeva circa 40 secondi per 200.000 valori e
  quattro minuti e mezzo per 1.000.000. Avviene una volta per chiave, ma un'istanza costruisce quattro spazi
  alla volta, di persone e di chiavi, quindi più chiavi grandi costruite insieme dopo un riavvio trattengono le
  prime chiamate di tutti gli altri spazi su quell'istanza: invia una chiamata a ogni chiave grande, una chiave
  dopo l'altra, prima che riprenda il traffico.

Nell'esempio Compose, quando le tabelle cambiano, gli stessi passi girano in container della nuova
immagine (la pausa lascia scadere le chiavi delle istanze della 0.10 fermate; un'istanza della 0.11
toglie la propria quando si ferma):

```sh
docker compose -p latet --profile server pull
docker compose -p latet --profile server run --rm latet-server-1 server migrate --check
docker compose -p latet --profile server stop latet-server-1 latet-server-2
sleep 30
docker compose -p latet --profile server run --rm latet-server-1 server migrate
docker compose -p latet --profile server up -d latet-server-1 latet-server-2
```

Per aggiungere un'istanza, avviane un'altra con le stesse impostazioni, la stessa chiave principale e gli
stessi PostgreSQL e Valkey, poi aggiungi il suo indirizzo al bilanciatore. Le istanze tengono il loro
stato in PostgreSQL e Valkey, quindi ognuna può rispondere a qualsiasi richiesta.

### Più istanze su una macchina

Un'istanza fa la maggior parte del suo lavoro su un solo core del processore. Per usare più core di una
macchina, `latet server run --workers N` (o l'impostazione `workers`, `LATET_SERVER_WORKERS`) avvia N
istanze, dette worker, sulla stessa porta, ognuna in un processo suo: condividono PostgreSQL e Valkey
come farebbero istanze su N macchine, e il bilanciatore resta com'è. Nell'esempio Compose, imposta
`LATET_SERVER_WORKERS` nel file `.env`. Comincia con un worker per ogni core che la macchina dà al server.

- **Quanti:** `--workers` sulla riga di comando, altrimenti `LATET_SERVER_WORKERS`, altrimenti
  `workers` in `server.yaml`, altrimenti uno; da 1 a 64. Le impostazioni rifiutano un numero le cui
  porte delle metriche (sotto) andrebbero oltre 65535 o prenderebbero la porta dell'istanza stessa.
  Nell'esempio Compose ogni container del server avvia i propri worker: due container con
  `LATET_SERVER_WORKERS=4` sono otto istanze, con otto pool di connessioni.
- **Che cosa danno:** misurato su Linux attraverso l'API per le applicazioni, otto worker hanno
  mascherato 6,28 volte quanto uno e ripristinato 5,21 volte, dove questa versione si proponeva di
  arrivare a 5 volte ([quanto è veloce](../../applications/#quanto-è-veloce)). Con meno core liberi,
  o con poche connessioni di lunga durata, si ottiene meno: N worker non valgono N volte uno.
- **Come arrivano le connessioni:** su Linux ogni worker ascolta sulla porta con un socket suo, e il
  sistema distribuisce le nuove connessioni tra loro in base a un hash degli indirizzi e delle porte
  di ciascuna. Molte connessioni si distribuiscono in modo uniforme; poche connessioni di lunga
  durata, come quelle di un bilanciatore, possono comunque finire distribuite in modo non uniforme,
  e più connessioni si distribuiscono meglio. Su macOS, che darebbe ogni connessione a uno solo dei
  socket aperti su una porta, i worker condividono invece un unico socket, e le connessioni li
  raggiungono in modo meno uniforme. Perché sia il kernel a distribuire le connessioni, fai girare
  il server su Linux o nell'immagine del container.
- **L'utente del server:** su Linux il processo che avvia i worker tiene la porta per tutta la
  durata del server, così nessun altro programma la prende, nemmeno mentre nessun worker è in
  ascolto; subito dopo l'arresto di un server con una sola istanza sulla stessa porta, aspetta fino
  a un minuto che le connessioni chiuse di quel server scompaiano. Un programma può ascoltare sulla
  porta accanto ai worker solo se gira con lo stesso utente del server, e in quel caso potrebbe
  prendersi una parte delle connessioni, o tutte. Fai girare il server con un utente che non esegue
  nient'altro: l'utente `latet` del servizio di sistema, o quello del container.
- **Memoria e connessioni:** ogni worker tiene in memoria circa quanto un'istanza: su Linux circa
  110 MB residenti a riposo, e da 185 a 227 MB con lo spazio di una chiave API di 200.000 valori dopo
  aver servito 200.000 righe di log distinte, misurati per ogni worker senza modelli di
  riconoscimento; di più per altri spazi, le sue cache e i modelli di riconoscimento quando sono
  installati. Un worker che serve una persona la cui cassaforte è tenuta intera la tiene tutta, circa
  1,6 KB per valore (circa 58 MB a 35.000 valori). Prevedi N volte tanto, più fino a circa 70 MB per
  il processo che li avvia e li sorveglia. Il riconoscimento del testo dà a ogni worker la sua parte
  dei processori della macchina. Ogni worker ha anche le sue connessioni, `database_pool` verso
  PostgreSQL e `valkey_connections` verso Valkey: fai in modo che `max_connections` di PostgreSQL e
  `maxclients` di Valkey coprano ogni worker di ogni macchina.
- **Il processo che li avvia:** non costruisce nessuno spazio e non apre nessun database, chiave
  principale o certificato; ogni worker lo fa nel proprio processo. Parte con le impostazioni e
  l'ambiente del server e li passa ai worker, quindi proteggilo come un'istanza.
- **Metriche:** il worker 0 risponde a Prometheus su `metrics_port`, ogni altro worker sulla porta
  successiva (7283, 7284 e così via): leggi le metriche di ciascuno. `latet_process_memory_bytes` è la
  memoria di un worker (la memoria residente su Linux, i byte privati su Windows), e
  `latet_vault_index_bytes` la parte che ne occupano gli indici delle sue casseforti.
- **Sorveglianza:** un worker che termina, o che non manda un battito per 30 secondi, viene chiuso e
  avviato di nuovo, al massimo una volta ogni dieci secondi. Un worker che non riesce ad avviarsi
  tre volte di fila (le sue impostazioni, il suo database o il suo certificato lo rifiutano) ferma
  tutti i worker e il server, come si ferma un'istanza sola, così decide il gestore dei servizi. Un
  worker che non è pronto (PostgreSQL o Valkey non rispondono) resta: decide il controllo di
  prontezza del bilanciatore. `latet server status` elenca ogni istanza attiva, ogni worker di ogni
  macchina, con il numero del worker, se è pronta, la sua memoria e la sua versione; un worker morto
  resta in elenco fino a 30 secondi dopo l'avvio di quello che lo sostituisce. Una richiesta che un
  worker stava servendo quando è terminato non riceve risposta: il suo client riceve un errore e deve
  inviarla di nuovo, e nessuna riceve risposta senza mascheramento. Su Linux le connessioni ancora in
  attesa di un worker terminato vengono interrotte, e i loro client devono riconnettersi, e un
  worker che smette di mandare battiti tiene in attesa la sua parte di nuove connessioni finché non
  viene chiuso.
- **Passare ad altri worker le connessioni in attesa (facoltativo):** da Linux 5.14 in poi, con
  `net.ipv4.tcp_migrate_req` impostato a 1, il sistema passa agli altri worker le connessioni in
  attesa di un worker che termina, invece di interromperle. Nell'esempio Compose, togli il commento
  alla voce `sysctls` dei servizi del server; per un servizio di sistema, metti
  `net.ipv4.tcp_migrate_req = 1` in un file sotto `/etc/sysctl.d/` e caricalo con `sysctl --system`.
  Non è impostato in modo predefinito: su un kernel più vecchio, Docker rifiuta di avviare un
  container che lo imposta.
- **Arresto:** un segnale di arresto raggiunge ogni worker, e ognuno ha 30 secondi per finire il lavoro
  in corso, come un'istanza sola.
- **Windows:** più worker richiedono Linux o l'immagine del container. Su Windows `latet server run` ne
  rifiuta più di uno: lì un socket serve un solo processo, e l'unico ciclo di eventi che potrebbe
  condividerlo chiude un worker oltre le 512 connessioni. Avvia un'istanza su ogni macchina Windows dietro
  il bilanciatore; il servizio Windows ne avvia una sola, qualunque cosa dica l'impostazione.

### Aggiornare un Latet installato come servizio di sistema

Un Latet installato come servizio di sistema, come sopra, si aggiorna con il comando con cui è stato
installato, eseguito di nuovo con il wheel della nuova versione, le stesse cartelle `UV_*` e gli stessi
extra (`server`, con `ner` e `ocr` se li ha):

```sh
UV_TOOL_DIR=/opt/latet/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/latet/python \
  uv tool install --upgrade --python ">=3.12,<3.15" "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
```

Poi chiedi alla nuova versione se cambia le tabelle, come sopra (`latet server migrate --check`), e
riavvia il servizio (`systemctl restart latet-server`). Su Windows, che non sostituisce i file di un
programma in esecuzione, ferma il servizio prima dell'installazione (`Stop-Service LatetServer`) e
avvialo dopo (`Start-Service LatetServer`). `latet upgrade` installa nelle cartelle di uv: su un Latet
installato in cartelle sue si ferma prima di cambiare qualcosa e stampa questo comando, con le
cartelle che ha trovato.

Fino alla 0.10.4, `latet upgrade` indicava solo il rilevamento dei nomi e il testo nelle immagini, e uv
toglieva gli altri extra: un server aggiornato così perdeva `server` (psycopg e redis), e
`latet server run` poi si rifiutava di partire. Un server alla 0.10.4 o precedente passa quindi a una
versione successiva una volta con il comando qui sopra, indicando `server`, oppure con l'installer
scaricato di nuovo da GitHub ed eseguito con le stesse variabili `UV_*`. Un server che ha già perso
`server` lo riottiene con il comando qui sopra: l'installer non può sapere di un extra che non c'è più.

## Modelli di riconoscimento su un server

Il rilevamento dei nomi e il riconoscimento del testo leggono i modelli dalla cartella dati del
server, non dal computer dell'amministratore. Installali lì, con l'account con cui girano le istanze:
`latet ner install` e `latet ocr install` con `LATET_DATA_DIR` impostata su quella cartella.
Nell'esempio Compose la cartella è il volume `latet-server-data`, montato in `/data` in ogni istanza:
installali da un container di un'istanza, poi riavvia le istanze.

```sh
docker compose -p latet --profile server run --rm latet-server-1 ner install --yes
docker compose -p latet --profile server run --rm latet-server-1 ocr install --yes
docker compose -p latet --profile server restart latet-server-1 latet-server-2
```

Con il servizio Linux, le istanze girano come utente `latet`, e la loro cartella è
`/var/lib/latet`, che systemd crea quando il servizio parte per la prima volta. Una volta partito,
installa i modelli come quell'utente, poi riavvia il servizio:

```sh
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ner install --yes
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ocr install --yes
systemctl restart latet-server
```

Per il servizio Windows, questa pagina non dà una procedura per installare i modelli.

## Programmi con una chiave API

Un programma raggiunge i suoi percorsi con la sua chiave API e tiene la propria chiave del fornitore.
Scrivi la chiave API in un file privato, poi stampa le variabili che fanno passare il programma dal
server:

```sh
latet env --key-file program.key --server https://latet.acme.lan
```

Le variabili stampate contengono la chiave di percorso: tienile segrete quanto la chiave, mai in un repository o
in un profilo condiviso. Solo il primo token della chiave ha questi indirizzi: un token creato con
`keys add-token` non ne ha, qualunque siano i suoi permessi, e chiama soltanto l'API per le applicazioni.
`latet env` chiede al server di che tipo è il token: per un token così non stampa alcuna variabile e lo
dice.

Il programma deve fidarsi del certificato del server: uno di un'autorità di cui si fida già, oppure il
certificato aggiunto alle sue impostazioni di fiducia.

Un programma che maschera i propri log o documenti chiama invece l'[API per le applicazioni](../../applications/):
nessuna variabile, un token della chiave nella sua intestazione `Authorization`, con i soli permessi
che gli servono. Le sue chiamate condividono lo spazio della chiave con gli agenti della chiave, e
quindi la sua cassaforte e i suoi valori fittizi.

Due tipi del suo lavoro fanno coda per tutto il server, non per ogni chiave. La voce del registro
amministrativo di ogni ripristino viene scritta prima della sua risposta, una alla volta sulla catena
del registro, da due thread di ogni istanza per i ripristini di tutte le chiavi; e gli oblii girano due
alla volta su ogni istanza, per tutte le chiavi. Una chiave che invia molti ripristini insieme, o che
dimentica una lista lunga, ritarda i ripristini o gli oblii delle altre chiavi, mai un'iscrizione, una
revoca o ciò che inviano gli agenti. Il registro amministrativo cresce di una riga per ogni chiamata di
ripristino.

## Log, metriche e recupero

I log dell'applicazione vanno allo standard output in formato `text` o `json`. Proteggi
e ruota i file di log dell'installazione. Il registro amministrativo è separato e annota
le operazioni, incluse emissioni di codici e modifiche alle credenziali. Leggilo con
`latet server log` e verifica la catena con `latet server log verify`. Conserva la testa
restituita fuori dal database: eliminare la coda può lasciare valida la catena restante.

Il verdetto sulla catena del comando status è in cache e può essere sconosciuto; non è
una nuova verifica completa. Le metriche Prometheus usano un listener separato, per
impostazione predefinita `127.0.0.1:7283`, e richiedono `metrics_token_file`. Distingui un verdetto sconosciuto
da una catena interrotta. Il [riferimento amministrativo](../../reference/server-administration/)
definisce la paginazione del registro e il significato preciso di `latet_ocr_unread_total{reason}`.

Ogni ripristino attraverso l'API per le applicazioni è una voce `apps.unmasked` del registro
amministrativo, con il token e quanti valori ha ripristinato, e ogni oblio una voce `apps.forgotten`,
con il token e quanti valori ha tolto dalla cassaforte della chiave; analisi e mascheramento sono
contati
solo nelle metriche (`latet_apps_texts_total` e `latet_apps_values_total`, per operazione, e le
richieste come `latet_http_requests_total{kind="apps"}`), senza un'etichetta per la chiave
([che cosa registra il server](../../reference/apps-api/#che-cosa-registra-il-server)).

In questa configurazione Compose, leggi `127.0.0.1:7283` dall'interno di ogni istanza con
il token delle metriche. Non ci sono una porta pubblicata, un percorso Caddy o una rete
di monitoraggio fornita. Un'altra distribuzione deve predisporre esplicitamente un percorso
di lettura protetto e autenticato; la porta HTTPS dell'applicazione non serve queste metriche.
Il listener delle metriche risponde solo a `GET /_latet/metrics`, a ogni altro percorso con `404`:
Prometheus chiede `/metrics`, a meno che il suo `metrics_path` non dica altro.

Un'istanza all'ora percorre la catena del registro amministrativo e ne conserva il verdetto; lo stato e le
metriche mostrano quel verdetto per due ore, e lo dicono sconosciuto quando non ce n'è uno (un server
nuovo, o Valkey ripartito da zero). Latet non ha uno strumento che ripari una catena rotta: confrontala con la
testa che hai conservato fuori dal database, trova la voce che indica e ripristina il database da un backup se il
registro è stato alterato.

Un Prometheus accanto a un'istanza la interroga così, ogni 30 secondi; va bene qualsiasi intervallo fino
a due minuti. Gli avvisi aspettano più di un'interrogazione, perché la prima dopo un avvio non ha ancora
le letture dei servizi:

```yaml
scrape_configs:
  - job_name: latet
    scrape_interval: 30s
    metrics_path: /_latet/metrics
    authorization:
      credentials_file: /etc/prometheus/latet-metrics.token
    static_configs:
      - targets: ["127.0.0.1:7283"]
```

```yaml
groups:
  - name: latet
    rules:
      - alert: LatetDatabaseDown
        expr: latet_database_up == 0
        for: 2m
      - alert: LatetValkeyDown
        expr: latet_valkey_up == 0
        for: 2m
      - alert: LatetAuditChainBroken
        expr: latet_audit_chain_known == 1 and latet_audit_chain_ok == 0
        for: 5m
      - alert: LatetLogsDropped
        expr: increase(latet_log_dropped_total[10m]) > 0
```

Esegui backup di PostgreSQL, della chiave principale corrispondente e della configurazione,
con controlli di accesso appropriati. Prova il ripristino in un ambiente isolato prima
di affidarti al backup. Ripristinare un vecchio database può ripristinare anche vecchie
autorizzazioni: riconcilia credenziali e revoche prima di riaprire il servizio.
Conservazione della cronologia e sua cancellazione dalla dashboard non eliminano backup
o cassaforte. Controlla questi archivi separatamente.
