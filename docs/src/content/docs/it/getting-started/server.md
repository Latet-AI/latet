---
title: Configurare e amministrare un server di Latet
description: Distribuire un server, connettere un computer e gestire persone, chiavi API e impostazioni condivise.
---

Esegui i comandi amministrativi nel tuo terminale, su un computer con Latet installato.
Leggi [come funzionano gli spazi e la fiducia](../../concepts/server/) prima di concedere
accesso. Un amministratore prepara il server; ogni persona connette il proprio computer.
Un agente AI può spiegarti i passi e controllare la macchina, ma i comandi che cambiano
l'amministrazione, `config get`, `rules export`, `latet server init`, `latet server service install`
e `latet connect` rifiutano di girare dentro un agente: indicagli [`llms-server.md`](https://github.com/Latet-AI/latet/blob/main/llms-server.md).
Non dare mai a un agente un token amministrativo: con uno, i comandi che si limitano a leggere (lo
stato, gli elenchi di persone e chiavi, il registro) girano per lui.

## Preparare il server

Scegli il [servizio di sistema o Compose](../../concepts/server-operations/).
Il servizio richiede un'installazione attendibile a livello di sistema e una configurazione
protetta; il profilo server di Compose fornisce PostgreSQL, Valkey, due istanze e Caddy.
Imposta l'indirizzo HTTPS pubblico e prepara prima la chiave principale e i file segreti.
L'inizializzazione stampa un token amministrativo: conservalo in un terminale privato e
in un archivio di segreti. Per un container segui l'inizializzazione con i log Docker
disattivati nella guida operativa; un'inizializzazione con log ordinari può conservare il token.

Usa un certificato attendibile per i computer. Per una prima prova, crea un certificato
server autofirmato e controllane l'impronta digitale:

```sh
latet server certificate --make --url https://latet.acme.lan --out certs
latet server certificate certs/server.crt
```

Mantieni privato `server.key`. Compose monta `certs/server.crt` e `certs/server.key` in
Caddy; una distribuzione senza container configura TLS sull'istanza o sul suo reverse
proxy attendibile. Comunica l'impronta verificata attraverso un canale attendibile separato.
Un'impronta letta da una connessione di rete non verificata non autentica da sola il server.

## Controllare il server

```sh
# a self-signed certificate: its fingerprint, verified as above, for every `latet server` command in this shell
export LATET_SERVER_FINGERPRINT="<fingerprint>"
latet server --server https://latet.acme.lan status
```

Inserisci il token amministrativo alla richiesta nascosta. La risposta riporta database,
Valkey, identità e istanza che ha risposto. Non controlla ogni agente e non dimostra che
tutto il traffico di un computer passi attraverso Latet.

Per amministrare il server più volte, `--admin-token-file FILE` legge un file privato con
il token o un record associato a questo server. Tienilo fuori dai repository e dagli spazi
di lavoro degli agenti. Segui i controlli di permessi e origine in
[Amministrazione del server](../../reference/server-administration/). Non esportare un
token amministrativo nell'ambiente della shell di un account in cui vengono eseguiti agenti.

Nella risposta, `database` è sempre vero, perché un'istanza che non raggiunge PostgreSQL non dà alcuno
stato, e `valkey` di solito è vero per lo stesso motivo: leggi il loro stato di salute nelle metriche
(`latet_database_up`, `latet_valkey_up`). `audit` dà l'ultimo verdetto della catena (`ok`, `broken` o
`unknown`), e
`vault` elenca le identità al 90% o più del limite della loro cassaforte.

## Dare a una persona il proprio spazio

```sh
latet server --server https://latet.acme.lan people add "Emily Carter" --email emily.carter@acme.com
latet server --server https://latet.acme.lan people list
```

Il primo comando stampa un codice monouso. Consegnalo privatamente alla persona: chiunque
lo usi può connettersi a suo nome e aprire la sua dashboard. L'emissione del codice viene
annotata nel registro amministrativo. Il codice funziona una volta sola, entro 24 ore.
`people code "Emily Carter"` ne emette un altro quando il primo è scaduto o è andato perso, e quello
di prima smette di funzionare; non inserire mai un codice in un ticket, canale condiviso o
conversazione con un agente.

I computer di una persona condividono uno spazio: cassaforte, cronologia e impostazioni.
Per esaminare i token dei computer, usa:

```sh
latet server --server https://latet.acme.lan people tokens "Emily Carter"
```

Revoca il token di un computer perso usando il suo id nell'elenco con `--revoke TOKEN_ID`.
`people disable "Emily Carter"` ferma ogni suo token entro pochi secondi e conserva il suo spazio
(`people enable` glielo restituisce); `people remove "Emily Carter"` elimina lo spazio attivo di lei
sul server, incluse cassaforte, cronologia e impostazioni, una volta che confermi. `keys disable`,
`enable` e `remove` fanno lo stesso per una chiave API. Nessuna delle due operazioni elimina
copie in backup, browser o presso i fornitori del modello. Leggi la conferma prima di rimuovere una persona.

Con una rimozione, i dati della persona in PostgreSQL se ne vanno con lei, i suoi dati in Valkey vengono
cancellati una volta da un'istanza (di nuovo al controllo successivo se non si riesce a finire), ciò che vi resta
scade da solo, entro otto giorni al massimo, e le sue sessioni della dashboard e di AWS finiscono subito.

## Connettere il computer di una persona

Quando il server è pronto e l'amministratore ha emesso il codice della persona, esegui:

```sh
latet connect https://latet.acme.lan
```

Inserisci il codice monouso quando richiesto. Con un certificato autofirmato aggiungi
`--fingerprint FINGERPRINT`, usando l'impronta verificata con l'amministratore.
Non rimuovere il vincolo al certificato per aggirare un errore. Il record di connessione,
anche nel file scelto da `LATET_CONNECTION`, contiene metadati e impronta; il token
personale e la chiave del computer sono conservati insieme, a parte, nell'archivio delle credenziali
o in un file privato.

Per Claude Code il processo ausiliario connesso è supportato in un **terminale Windows**.
Applica l'integrazione e riavvia Claude Code, oppure avvia una singola sessione:

```sh
latet agents apply claude
latet claude
```

`latet agents apply claude` e `latet global-install` scrivono il token personale nel
`settings.json` dell'utente Claude come `LATET_BRIDGE_TOKEN`; il comando indica dove.
È l'eccezione di installazione alla regola che esclude il token dai file degli agenti.
L'associazione privata del processo ausiliario contiene metadati; il processo riceve il
token esistente tramite stdin. Un software che osserva quell'input può leggerlo.
Questo contratto non supporta il processo ausiliario connesso in Claude Desktop,
VS Code, JetBrains, Linux o macOS: su Linux e macOS questi comandi lo dicono subito e non
scrivono il token, e il plugin trattiene ogni prompt.

Quel token apre il bridge del plugin e il proxy dello spazio da ovunque il server sia raggiungibile;
non apre mai la dashboard, non cambia mai l'identità della persona e non riduce mai la sua protezione:
l'unica modifica che può fare alle sue impostazioni è aggiungere una destinazione per un indirizzo, che
non riduce nulla. La dashboard e l'identità richiedono anche la chiave del computer, che
`latet connect` conserva con il token nell'archivio delle credenziali (o nel file privato) e che
leggono solo `latet dashboard` e `latet identity send`, che rifiutano entrambi di essere eseguiti da un
agente; nessun agente la riceve.

Usa questi comandi sul computer connesso:

```sh
latet status
latet doctor
latet dashboard
latet identity send
latet disconnect
```

`status` e `doctor` offrono diagnostica limitata, non una prova di ogni percorso degli agenti.
`dashboard` apre la dashboard della persona sul server; su un computer connesso un agente non può
eseguirlo. Stampa il link privato di accesso
solo con `--print-url`; mantienilo privato. `config`, `rules`, `llm`,
`project`, `identity` e `reload` rifiutano durante la connessione, sia per leggere le
impostazioni sia per cambiarle; funzionano solo `config schema`, `rules test` e `identity send`.
Usa la dashboard del server o i comandi amministrativi. `latet identity send` è il
comando esplicito per reinviare i dati identificativi di questo computer. Anche `latet start`
rifiuta: durante la connessione nessuno usa il daemon di questo computer.

`latet disconnect` revoca il token del computer e rimuove le associazioni degli agenti
alla connessione, incluso il token Claude salvato. La revoca termina anche le sessioni
della dashboard della persona sugli altri computer. Se non puoi confermare la revoca,
leggi l'esito e chiedi a un amministratore di controllare gli id dei token prima di riprovare.
Su un computer connesso la modalità di stampa (`claude -p`) non è supportata: il plugin
trattiene le sue richieste e lo dice. Usa il terminale interattivo. La
[guida di Claude Code](../../agents/claude-code/) distingue il supporto connesso dalle superfici locali.

Togliere le copie di questo computer non è revocare il token. Con `LATET_KEYRING=off` Latet non salva
credenziali nel portachiavi del sistema operativo, ma una copia più vecchia può esserci ancora:
`latet connect` rifiuta finché c'è, e `latet disconnect` la legge, la revoca sul suo server e la rimuove;
un portachiavi bloccato può chiedere di essere sbloccato. Se una connessione o una disconnessione si
interrompe, un file temporaneo con il token rimasto viene cancellato senza essere letto, quindi il computer
non può revocare il token che conteneva. In quel caso, o quando `latet disconnect` dice che una copia
resta, lo revoca un amministratore sul server: elenca i token della persona come sopra, trova quello giusto
dal suo id (l'etichetta con il nome host è solo un indizio: due computer possono avere lo stesso nome) e lo
revoca con `--revoke TOKEN_ID`. Se non si riesce a distinguere il token giusto, `--revoke-all` revoca tutti
i token della persona, e ognuno dei suoi computer dovrà poi connettersi di nuovo.

## Dare una chiave API a un programma

Una chiave API ha il proprio spazio, senza dashboard personale né registrazione di
computer. L'amministratore ne gestisce le impostazioni e le regole. Crea il segreto in un nuovo file privato:

```sh
latet server --server https://latet.acme.lan keys add "Billing CI" --out billing.key
latet server --server https://latet.acme.lan keys list
```

Usa una cartella protetta fuori dal repository. Il comando stampa l'identità e il percorso
del file senza mostrare il segreto. Conserva la chiave nell'archivio dei segreti del
programma. `keys rotate "Billing CI" --out replacement.key` sostituisce la credenziale;
aggiorna insieme la chiave del programma e i suoi indirizzi di percorso. Anche una chiave
di percorso contenuta in un indirizzo è sensibile.

## Configurare le impostazioni condivise e le regole delle chiavi API

Le impostazioni condivise contengono ciò che serve al server per funzionare, uguale per ogni
spazio: le destinazioni verso cui possono andare le richieste (`upstreams`), il revisore locale
condiviso (`llm_review`), il riconoscimento del testo (`ocr`), la cronologia e per quanto tempo
viene conservata (`retention_days`).

```sh
latet server --server https://latet.acme.lan config get
latet server --server https://latet.acme.lan config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Senza `--key`, `config get`, `set`, `add` e `remove` agiscono solo su queste impostazioni. Una regola
di protezione, come un termine o il profilo, viene rifiutata, con un messaggio che spiega come
condividerla a mano. Con `--key`, gli stessi comandi leggono e cambiano le impostazioni e le regole proprie
di una chiave API, e `latet server rules --key` ne gestisce i riconoscitori e vi importa un file
di regole:

```sh
latet server --server https://latet.acme.lan config --key "Billing CI" set profile max
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

`config get` e `rules export` possono stampare valori in chiaro (i termini di una chiave API, le
indicazioni per il revisore): mantieni privato il loro output. Le regole proprie di una persona si
gestiscono nella sua dashboard, dove le imposta lei stessa.

Per dare a un team le stesse regole, condividile a mano: esportale da un posto
(`latet rules export FILE` su un computer non connesso a un server, o **Esporta le regole** nella
pagina **Regole** di una dashboard) e importa il file in ciascuno (`latet rules import FILE`, o
**Importa le regole** nella dashboard); quelle di una chiave API con
`latet server rules --key KEY import FILE`. Sui computer non connessi a un server, il `latet.yaml`
di un progetto le porta nel suo repository. Le regole impostate per tutti, che nessuno può
allentare, sono previste per la Enterprise Edition.

Una modifica alle impostazioni condivise che riduce la protezione di una persona, come una
destinazione spostata a un altro indirizzo o il revisore spento, attende la sua approvazione.
Salvare modifiche personali non approva una modifica condivisa in attesa. Un salvataggio
può conservare le modifiche personali senza riuscire ad aggiornare la proposta in attesa;
leggi il risultato e ricarica prima di cambiare altro.
Per una chiave API non c'è nessuno che approvi una modifica del genere: arriva subito allo spazio
della chiave, anche quando riduce la protezione, quindi controlla che cosa fa ai tuoi programmi una
modifica alle impostazioni condivise prima di farla.
[Impostazioni condivise e regole proprie](../../concepts/server/) spiega la distinzione.

Usa token amministrativi con una scadenza per il lavoro quotidiano. Il
[riferimento amministrativo](../../reference/server-administration/) descrive emissione,
revoca, file di output privati, recupero e lettura del registro.
