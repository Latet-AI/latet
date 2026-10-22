---
title: Amministrazione del server
description: Amministra un server dal tuo computer e mantieni private le credenziali.
---

Esegui `latet server` nel tuo terminale. I comandi che modificano l'amministrazione rifiutano
un agente; anche `config get` e `rules export`, nel terminale o in un file, lo rifiutano, perché
possono mostrare valori in chiaro (i termini di una chiave API, le indicazioni per il revisore).
Stato, elenchi delle identità e log di amministrazione non contengono credenziali.

Indica il server e inserisci il token nel prompt nascosto:

```sh
latet server --server https://latet.acme.lan status
```

Oppure usa `--admin-token-file FILE`: un file UTF-8 privato con il solo token, o un record
`{"url": "https://latet.acme.lan", "token": "<administration token>"}`.
Un record è accettato solo per il proprio server. Su POSIX usa `chmod 600 FILE` (o `0400`
per l'account che esegue un container), in una cartella che altri utenti non possono modificare.
Su Windows il file e la cartella devono consentire l'accesso solo al tuo account, SYSTEM e
Administrators; segui la correzione `icacls` mostrata dal comando.
`--admin-token-file -` legge una riga dallo standard input.

Su un computer dove vengono eseguiti agenti, tieni i token di amministrazione fuori dai profili
della shell e dalle variabili d'ambiente dell'account: i processi dello stesso utente possono leggere
l'ambiente di un altro processo. `LATET_SERVER_ADMIN_TOKEN` è per l'archivio dei segreti di una CI
o di un container, insieme a `LATET_SERVER_URL`; non viene mai inviato a un server diverso.

`keys add`, `keys rotate` e `admin-token issue` accettano `--out FILE`: il nuovo file privato
riceve il segreto e il comando mostra solo id e file. Non mettere mai codici, chiavi o token
di amministrazione in un ticket, canale condiviso o conversazione con un agente. Il codice monouso
di una persona permette a chi lo usa di collegare un computer come quella persona e aprirne la
dashboard; la sua emissione viene registrata.

Usa `admin-token issue --label "Daniel's laptop" --days 30` per un token con scadenza.
Un token `--never` richiede un'etichetta e permette tutto ciò che può fare un amministratore,
fino alla revoca: persone e relativi codici, chiavi API, impostazioni condivise e token di
amministrazione. Un token ne emette altri che terminano al più tardi insieme a lui, quindi solo un
token che non scade può emetterne un altro che non scade; sul computer del server,
`latet server init --new-admin-token --never` ne crea uno. Tienilo separato, usandolo
solo per emettere gli altri. Una CI che lo possiede dà questi poteri per sempre a chi può modificarne
i processi; assegna alla CI un token con `--days` ed emettilo di nuovo prima della scadenza.

Le rimozioni chiedono conferma nel tuo terminale anche con `--yes`; gli script richiedono `--yes`.
`config reset` sostituisce solo una riga di impostazioni condivise che il server non riesce ad aprire,
dopo conferma. `config get` e `rules export` possono mostrare valori in chiaro: mantieni privato
l'output.
`log verify` mostra la testa della catena; conservane una copia altrove e confrontala la volta
successiva: rimuovere voci dalla fine lascia integra la catena rimanente.

## Chi amministra e la perdita di un token

**L'amministratore è chiunque possieda un token di amministrazione.** Non esiste un account
amministratore: `latet server init` mostra il primo token a chi inizializza il server, e ogni
token può emetterne altri. Un token non è una persona: il log di amministrazione indica il token
che ha fatto ogni cosa (`admin:a_3fk7…`), e un amministratore non ha bisogno di uno spazio proprio
sul server, anche se può essere aggiunto come persona come chiunque altro. Nella Community Edition
ogni token può fare tutto; i ruoli, come un revisore in sola lettura, sono previsti per la
Enterprise Edition.

Un token emesso senza `--days` né `--never` termina dopo `admin_token_days` (90 giorni, se le
impostazioni del server non dicono altro), o prima, se termina prima il token che lo ha emesso; uno
con `--days` termina dopo quei giorni. Un token così, dimenticato, smette di funzionare da solo. Un
token `--never` funziona finché non viene revocato: elenca ogni tanto i token con `admin-token list`,
e revoca quelli che nessuno usa.

**Un token perso, con un altro ancora valido:** da qualsiasi computer, elencali, revoca quello perso
ed emettine uno nuovo:

```sh
latet server --server https://latet.acme.lan admin-token list
latet server --server https://latet.acme.lan admin-token revoke a_3fk7qz2mwd
latet server --server https://latet.acme.lan admin-token issue --label "Marco" --out marco.token
```

Senza `--days`, il nuovo token dura quanto il valore predefinito del server, tagliato alla fine del
token che lo emette. Un token con `--days` non può terminare dopo il token che lo emette: il server lo
rifiuta, e `admin-token list` mostra quando termina ogni token.

**Nessun token rimasto**, perché tutti persi o scaduti: dove si possono raggiungere le impostazioni
del server, la sua chiave principale e il suo database, `latet server init --new-admin-token` ne
emette uno nuovo (con `--never`, uno di scorta da tenere separato). Nella distribuzione Compose,
esegui di nuovo il `docker run` dell'inizializzazione di
[Gestione del server](../../concepts/server-operations/), terminandolo con
`server init --new-admin-token`, con i log di Docker disattivati come prima. Poi revoca quelli persi,
se non sono già terminati. Persone, chiavi API, impostazioni, casseforti e cronologia restano
com'erano in entrambi i casi.

**Un token trapelato**: revocalo subito da un altro token, poi leggi che cosa ha fatto:

```sh
latet server --server https://latet.acme.lan log --actor admin:a_3fk7qz2mwd --since 30d
```

Perdere la **chiave principale** è peggio che perdere un token: sigilla la chiave di ogni spazio,
e senza di essa nessuna cassaforte potrà più essere letta. Tienine una copia separata dai backup
del database, come dice [Gestione del server](../../concepts/server-operations/).

## Leggere il log di amministrazione

`latet server log --limit 100000 --json` legge pagine successive, prima le voci con data e ora
più recenti, poi quelle con id maggiore a parità di data e ora. `--limit` accetta al massimo
1.000.000 di voci. Ogni richiesta ne chiede al massimo 10.000; una pagina più corta può comunque
avere una continuazione, perché le pagine hanno anche un limite in byte. L'output JSON contiene
solo voci, una per riga; gli avvisi vanno su stderr.

`--since` include il proprio estremo e `--until` lo esclude. `--actor` seleziona un'identità o
un token di amministrazione; `--action` seleziona un'azione o un prefisso letterale che termina
con `*`. Le azioni indicano che cosa è cambiato (`person.added`, `code.issued`, `token.revoked`,
`key.rotated`, `settings.changed`, `shared.changed`, `admin-token.issued`, `admin-token.revoked`), e
le letture sono registrate con il nome della chiamata (`status.read`, `log.read`):
`--action 'admin-token.*'` seleziona ogni modifica dei token di amministrazione.
Per riprendere, passa il cursore del log mostrato a `--cursor`, con gli stessi filtri
originali. Usa date assolute o secondi epoch esatti se devi riprendere più tardi: un tempo relativo
come `7d` cambia tra un'esecuzione e l'altra. Non sostituire mai `--until` con una data arrotondata
dell'output testuale. Un cursore indica una posizione, non è una credenziale né prova che il log sia invariato.

Ogni esecuzione è limitata a 1.000 richieste di pagine, 128 MiB di corpi ricevuti, comprese le
risposte dei tentativi ripetuti, 128 MiB di output UTF-8 prodotto e 300 secondi di lettura ed
elaborazione. Il JSON con caratteri di escape può essere più grande del corpo ricevuto. Le pagine
vengono validate e preparate prima della stampa; una pagina che non rientra nel limite non viene
stampata in parte. Questi controlli sul tempo non possono interrompere una pipe di output bloccata dal sistema operativo.

Un limite raggiunto, una pagina non valida o un errore del server termina con codice diverso da
zero, indica quante voci sono state stampate e segnala che la lettura è incompleta. Un cursore
di ripresa validato, quando disponibile, indica la posizione dopo l'ultima pagina stampata.
Se la prima pagina supera il limite di output, richiedi meno righe. Un rifiuto del server come
`log_entry_too_large` non salta la voce che lo ha causato. Raggiungere il numero di righe richiesto
è una conclusione normale; l'avviso indica se resta una continuazione.

La prima pagina fissa un limite alle inserzioni: le voci aggiunte dopo, anche con date precedenti,
non entrano nella lettura in corso. La conservazione o l'eliminazione possono comunque rimuovere
voci non ancora lette. L'esaurimento indica solo che quelle letture non vedevano altre righe
corrispondenti; non è un'esportazione immutabile né una verifica della catena. Usa `log verify`
separatamente, tenendo presente il limite sulla rimozione delle ultime voci descritto sopra.

## Metriche delle immagini non lette

`latet_ocr_unread_total{reason}` conta le occorrenze di immagini con un esito finale
di OCR non letto su ciascuna istanza del server, sia quando una richiesta controllata
viene rifiutata sia quando un'immagine riscritta viene trattenuta. L'unica etichetta
è `reason`, con cinque valori normali:

| Motivo | Significato |
| --- | --- |
| `busy` | L'identità, il lettore o il pool non ha ammesso l'immagine entro il limite di attesa o di coda. |
| `too_long` | La lettura ha superato il tempo consentito. |
| `too_many_tiles` | L'immagine richiedeva più riquadri di quanti ne consente l'OCR. |
| `too_wide` | Una riga di testo ha superato il limite di larghezza del riconoscitore. |
| `unavailable` | L'OCR era in caricamento, non è riuscito a caricarsi o ha incontrato un altro errore di esecuzione gestito. |

Un'occorrenza successiva della stessa immagine originale viene contata di nuovo se
riceve un errore OCR dalla cache. Il contatore misura le occorrenze non lette, non
le chiamate ai modelli. Tentativi interni, riquadri, batch e riparazioni dei lettori
senza un'immagine inviata non aggiungono conteggi. Sono esclusi OCR riuscito,
trattenimento manuale, errori di decodifica o dei limiti sui pixel, OCR disattivato
o non installato, reinvio esatto di un'immagine dipinta o di un segnaposto di Latet
e OCR locale di un computer. I controlli finali dello stesso lavoro non contano
di nuovo. Nessuna etichetta contiene immagini, percorsi o identità.
