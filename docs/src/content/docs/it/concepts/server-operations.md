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
  uv tool install --python ">=3.12,<3.14" "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
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

L'OCR usa lettori limitati, ognuno con i modelli in memoria. `ocr_readers` vale 2 e non può
superare i processori disponibili per l'istanza. `ocr_read_seconds` vale 30;
`ocr_wait_seconds` vale 35 e non può essere inferiore al tempo di lettura. Aumentare i
lettori costa memoria oltre alla CPU. Un'immagine non letta viene trattenuta oppure la
richiesta verificata viene rifiutata, secondo il percorso protetto; aumentare i timeout non migliora l'accuratezza del riconoscimento.

Lo spazio di una chiave API tiene in memoria i valori usati più di recente dalla chiave
(`key_working_values`, 5.000 per impostazione predefinita, circa 10 MiB per chiave) e, per
sempre, i suoi segreti brevi più recenti che nessuna parola di un testo trova quando altre
lettere vi sono attaccate (`key_held_secrets`, 1.000 per impostazione predefinita, circa
2,2 KB ciascuno su ogni istanza). Aumenta `key_held_secrets` per una chiave che vede molti
segreti brevi, come un token di sessione in ogni riga di log: ogni testo che la chiave
maschera o ripristina viene letto per cercare ognuno di quelli tenuti, quindi anche le sue
chiamate rallentano un po' (il ripristino circa un decimo più lento con 1.000 tenuti)
([la cassaforte di una chiave](../../applications/#la-cassaforte-di-una-chiave)).

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
- **PostgreSQL:** ogni istanza tiene fino a `database_pool` connessioni (10 per impostazione predefinita), per il
  lavoro di ogni identità; il `max_connections` di PostgreSQL deve coprire il pool di ogni istanza e i comandi
  degli amministratori. Una chiamata all'API per le applicazioni che conserva valori nuovi ne tiene una finché non
  finisce, al massimo tre decimi del pool (tre su dieci); oltre, le chiamate conservano ogni valore nuovo subito,
  più lentamente. Aumenta `database_pool`, e con esso `max_connections`, per un'istanza che maschera per molte
  chiavi nuove alla volta.
- **Valkey:** ogni istanza tiene fino a `valkey_connections` connessioni (100 per impostazione predefinita), e oltre una
  chiamata attende al massimo cinque secondi. Il `maxclients` di Valkey deve coprire le connessioni di tutte le
  istanze. La connessione a Valkey, il suo TLS e la sua prima risposta condividono un unico limite di cinque
  secondi.
- **I certificati dei fornitori:** un'istanza si fida dell'archivio dei certificati del sistema operativo; su
  Linux, i percorsi predefiniti di OpenSSL, che `SSL_CERT_FILE` e `SSL_CERT_DIR` cambiano per tutto il processo.
  Cambiali solo per aggiungere l'autorità della tua azienda.
- **`SSLKEYLOGFILE`** non deve essere impostata per un'istanza: le connessioni TLS che la leggono, tra cui
  quella verso Valkey, scrivono in quel file le chiavi delle loro sessioni, e chi lo legge legge quel traffico.
  Un'istanza avviata con questa variabile lo segnala nel suo log.

## Aggiornare e aggiungere istanze

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
  versione, poi avvia le istanze con quella. Un'istanza non parte su tabelle di un'altra versione.
- **I programmi usano l'API per le applicazioni:** ferma ogni istanza più vecchia prima che partano
  quelle nuove, anche quando le tabelle restano uguali. Mentre due versioni girano insieme, un valore
  che un'istanza più vecchia scrive nella cassaforte di una chiave può non essere trovato da una più
  nuova finché lo spazio della chiave non viene ricostruito
  ([aggiornare un server](../../applications/#aggiornare-un-server)).
- **Una versione che indicizza di nuovo la cassaforte di una chiave** (il changelog lo dice): la prima
  istanza che costruisce lo spazio di ogni chiave API ricalcola le parole della sua cassaforte prima di
  rispondere alla prima chiamata di quella chiave, circa 40 secondi per 200.000 valori e quattro minuti
  e mezzo per 1.000.000. Avviene una volta per chiave, ma un'istanza costruisce quattro spazi alla
  volta, di persone e di chiavi, quindi più chiavi grandi costruite insieme dopo un riavvio trattengono
  le prime chiamate di tutti gli altri spazi su quell'istanza: invia una chiamata a ogni chiave grande,
  una chiave dopo l'altra, prima che riprenda il traffico.

Nell'esempio Compose, quando le tabelle cambiano, gli stessi passi girano in container della nuova
immagine:

```sh
docker compose -p latet --profile server pull
docker compose -p latet --profile server run --rm latet-server-1 server migrate --check
docker compose -p latet --profile server stop latet-server-1 latet-server-2
docker compose -p latet --profile server run --rm latet-server-1 server migrate
docker compose -p latet --profile server up -d latet-server-1 latet-server-2
```

Per aggiungere un'istanza, avviane un'altra con le stesse impostazioni, la stessa chiave principale e gli
stessi PostgreSQL e Valkey, poi aggiungi il suo indirizzo al bilanciatore. Le istanze tengono il loro
stato in PostgreSQL e Valkey, quindi ognuna può rispondere a qualsiasi richiesta.

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

Con il servizio Windows, la procedura per installare i modelli non è ancora documentata.

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
