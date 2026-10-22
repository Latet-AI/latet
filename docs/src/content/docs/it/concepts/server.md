---
title: Latet su un server
description: Spazi, archiviazione, impostazioni condivise e confine di fiducia di un server di Latet.
---

Un server di Latet riceve le richieste protette dagli agenti connessi. In modalità gateway
le maschera prima di inoltrarle al provider del modello e ripristina il percorso di ritorno
protetto; le richieste riconosciute come Claude Code usano il plugin e il confine di sola
verifica descritto sotto. I dati originali possono raggiungere il server di Latet. Usare
un server cambia di chi devi fidarti; non rende infallibile il rilevamento e non protegge
il traffico che non raggiunge Latet.

## Client connessi

Il computer deve usare il percorso del server e mantenere private le associazioni di
connessione e credenziali. Il mascheramento sul server copre i client gateway. Claude Code
richiede anche il plugin: nel terminale Windows connesso supportato, l'associazione privata
del processo ausiliario e le impostazioni attendibili scelgono il bridge del server.
Il token personale passa tramite stdin; l'associazione di metadati non conserva token.
Un'associazione assente o non sicura ferma quel percorso.

Per le richieste riconosciute come Claude, il plugin maschera le descrizioni degli strumenti,
e il controllo finale del server legge anche le descrizioni, gli schemi dei parametri e gli
esempi degli strumenti: un valore rimasto in chiaro ferma la richiesta. I marcatori della richiesta
non provano che il plugin sia stato eseguito. Leggi il [perimetro della protezione](../protection-scope/)
per questa eccezione limitata e i controlli separati sugli schemi di output.
[Connettere un computer](../../getting-started/server/) descrive applicazione, diagnostica,
reinvio dell'identità e disconnessione.

Se Latet è spento in una cartella, una richiesta che un'istanza riceve per quella cartella prima che il plugin
l'abbia comunicato al server viene mascherata come se Latet fosse acceso: nel dubbio protegge.

### Il token e la chiave di un computer

Quando un computer si connette, il server gli dà due segreti, ciascuno mostrato una volta sola. Il
**token** va agli agenti: Claude Code lo tiene nelle impostazioni e nell'ambiente
(`LATET_BRIDGE_TOKEN`), dove ogni comando che esegue può leggerlo. Da ovunque il server sia
raggiungibile, il token apre il bridge del plugin e il proxy dello spazio (chi lo possiede può far
mascherare testi nello spazio e far ripristinare i valori fittizi che ha), dice di chi è e revoca se
stesso. La **chiave del computer** resta nell'archivio delle credenziali del computer, o nel file
privato del token, e nessun agente la riceve: il server la chiede anche prima di dare un link di
accesso alla dashboard o di aggiungere qualcosa all'identità della persona, così il token da solo non
apre mai la dashboard, dove ogni originale può essere mostrato, e non riduce mai la protezione della
persona: l'unica modifica che può fare alle sue impostazioni è aggiungere una destinazione per un
indirizzo, che non riduce nulla. La chiave impedisce a un token trapelato (dall'ambiente, dalle
impostazioni, da un log o con un solo `curl`) di aprire la dashboard; non ferma un comando scritto per
leggerli entrambi dall'archivio delle credenziali. Su un computer connesso `latet dashboard` e
`latet identity send` rifiutano di essere eseguiti da un agente, e `latet disconnect` li dimentica
entrambi.

### Cosa resta sul computer

- **Il `latet.yaml` di un repository non si applica.** Il server non legge alcun file del computer:
  ciò che un team condivide nel file del progetto (un profilo più rigoroso, i suoi termini e
  riconoscitori) non raggiunge le richieste di una persona connessa. In un progetto così
  `latet doctor` e `latet claude` lo dicono. Impostalo nella tua dashboard: importa il `latet.yaml`
  del progetto in **Regole** con **Importa le regole**.
- **Il controllo dei file CLAUDE.md**, che Claude Code dà al revisore della modalità auto, avviene
  sul computer, in `latet doctor`.
- **Claude Code tramite un server richiede un terminale Windows** in questa versione. Su Linux e
  macOS il suo plugin trattiene ogni prompt, e Latet lo dice subito dove lo applicherebbe. La
  modalità di stampa (`claude -p`) è trattenuta su ogni computer connesso.

### Certificati e connessioni proprie degli agenti

Ogni agente si fida del certificato del server tramite una sua variabile, che Latet imposta:
`NODE_EXTRA_CA_CERTS` per Claude Code e OpenCode, `CODEX_CA_CERTIFICATE` per Codex. Si aggiunge a ciò di cui
l'agente si fida già, e la copia che Latet scrive mantiene i certificati che la persona vi aveva indicato, per
esempio l'autorità della sua azienda. Un certificato autofirmato arriva agli agenti solo nella forma che gli dà
`latet server certificate --make`, limitato al nome o all'indirizzo del server; un certificato dell'autorità
dell'azienda funziona così com'è.

All'avvio gli agenti chiamano anche i propri fornitori, fuori da Latet: Claude Code `api.anthropic.com`, con la
sua chiave API, a meno che il traffico non essenziale sia disattivato nelle sue impostazioni; Codex `chatgpt.com` e
`api.github.com` per i suoi plugin; OpenCode il registro npm e GitHub. Nessuna di queste chiamate porta l'indirizzo
dello spazio, la sua chiave di percorso o il token. Dove il server deve essere l'unica via verso i modelli, blocca
gli host dei fornitori di modelli, tra cui `api.anthropic.com` e `chatgpt.com`, e disattiva il traffico non
essenziale di Claude Code; l'elenco dei plugin di Codex allora non funziona. Per esempio:

```text
allow  latet.acme.lan:443                        # the Latet server
allow  api.github.com:443, github.com:443        # Codex's plugins and OpenCode's start-up, if wanted
allow  registry.npmjs.org:443                    # OpenCode's start-up, if wanted
deny   api.anthropic.com:443, chatgpt.com:443    # the models only through the server
deny   every other host of the AI providers
```

## Spazi e istanze

Ogni persona e ogni chiave API ha uno spazio separato, con cassaforte, cronologia e impostazioni
propri. I computer di una persona condividono quello spazio. Una chiave API rappresenta
un programma, non ha una dashboard personale e riceve le impostazioni dall'amministratore.

Un'istanza è un processo server di Latet in esecuzione. Più istanze possono servire lo
stesso indirizzo HTTPS pubblico dietro un bilanciatore di carico, usando lo stesso database
PostgreSQL, Valkey e la stessa chiave principale. PostgreSQL conserva lo stato durevole;
Valkey coordina quello temporaneo. Le richieste non richiedono affinità di sessione.
Aggiungere istanze non crea copie separate dello spazio di una persona. I requisiti dei
servizi sono in [Gestione del server](../server-operations/).

## Cosa conserva il server

I valori e i valori fittizi nella cassaforte, testi e immagini registrati e le impostazioni di uno
spazio sono cifrati con la chiave dell'identità. Questa è cifrata con la chiave principale
del server; le impostazioni condivise sono cifrate con la chiave principale. Conserva
la chiave principale separatamente dai backup del database. Senza la chiave corrispondente
non è possibile recuperare i dati cifrati.

Alcuni metadati restano leggibili in PostgreSQL: nomi e indirizzi email delle identità,
identificatori esterni, etichette e date dei token, nomi delle regole della cassaforte, contatori
e date, oltre ad attori, azioni, destinatari, identificatori, conteggi e indirizzi client
del registro amministrativo. Gli identificatori di progetto e conversazione nella
cronologia usano hash con chiave. Valkey usa nomi derivati con chiave, payload sensibili
cifrati e messaggi di coordinamento firmati.

La cifratura dei dati archiviati non nasconde gli originali al server in esecuzione né
al suo operatore. Le viste autorizzate della dashboard ricevono gli originali nel browser.
Un amministratore può emettere il codice di registrazione di una persona e usarlo per
accedere alla sua dashboard; l'operazione viene registrata. Può anche configurare un
servizio di revisione che riceve testo originale. Devi quindi fidarti degli amministratori,
di chi gestisce l'infrastruttura e del revisore. Cifratura e firme non impediscono a chi
può scrivere nel database di ripristinare uno stato valido precedente.

Cancellare la cronologia registrata rimuove gli scambi di questo spazio, non la cassaforte.
Non elimina backup, trascrizioni degli agenti, registrazioni del fornitore o copie già ricevute da un browser.

## Impostazioni condivise e regole proprie

Le impostazioni condivise sono ciò che serve al server per funzionare, uguale per ogni spazio: le
destinazioni verso cui possono andare le richieste, il revisore locale condiviso, il
riconoscimento del testo, la cronologia e la sua conservazione. Le impostano gli amministratori.
Le regole di protezione (termini, riconoscitori, il profilo, i criteri e il resto) sono proprie di
ogni identità: le persone impostano le loro nella dashboard, e un amministratore imposta quelle di
una chiave API con `latet server config --key` e `latet server rules --key`.

Un team condivide le sue regole a mano: una persona le esporta (**Esporta le regole** nella pagina
**Regole** della dashboard, o `latet rules export` su un computer non connesso a un server) e
ciascuno importa il file nella propria dashboard. [Esportazione e importazione](../export-import/)
descrive il file. Le regole impostate per tutti, che nessuno può allentare, sono previste per la
Enterprise Edition.

Una modifica condivisa che riduce la protezione, come una destinazione spostata o il revisore
spento, attende l'approvazione della persona. Fino ad allora restano in vigore le impostazioni più
rigorose già approvate. Salvare modifiche personali non approva la modifica condivisa in attesa,
nemmeno se il salvataggio contiene lo stesso elemento. Un salvataggio personale non può rimuovere
silenziosamente una destinazione impostata per tutti.

Per una chiave API non c'è nessuno che approvi una modifica del genere: arriva subito allo spazio
della chiave, anche quando riduce la protezione, quindi controlla che cosa fa ai tuoi programmi una
modifica alle impostazioni condivise prima di farla.

Distingui questi esiti del salvataggio:

| Risultato | Significato |
| --- | --- |
| `409 settings_stale` | Le impostazioni sono cambiate dopo la lettura della pagina; nulla di questo salvataggio è stato conservato. Ricarica prima di modificarle ancora. |
| `409 settings_change_waits` | Al momento non è possibile combinare la modifica in attesa con le impostazioni approvate; nulla di questo salvataggio è stato conservato. |
| `409 settings_saved_change_pending`, `saved: true` | Le tue modifiche sono state conservate, ma l'aggiornamento della proposta in attesa non è riuscito. Ricarica e controlla la modifica in attesa; non ripetere il salvataggio alla cieca. |
| `409 settings_terms_too_long` | Un termine supera la lunghezza accettata; nulla di questo salvataggio è stato conservato. |

Anche altri limiti o errori di archiviazione possono impedire un salvataggio. Un errore
HTTP da solo non dimostra che nulla sia stato scritto; conserva il motivo restituito
e l'eventuale indicazione di un salvataggio parziale.

### Le destinazioni proprie di una persona

Una destinazione che una persona imposta nelle proprie impostazioni non porta mai nella rete del server. Un
indirizzo privato, unique-local, di NAT carrier-grade, di loopback, link-local, multicast o riservato viene
rifiutato, scritto come indirizzo o come nome che vi si risolve; così le forme IPv6 che portano un indirizzo IPv4 o
conducono a un gateway (6to4, Teredo, prefissi NAT64). Su una rete con NAT64 il server ne scopre i prefissi come
descrive l'RFC 7050, e finché non li conosce non raggiunge alcun indirizzo IPv6 che un NAT64 potrebbe tradurre: un
nome che ha anche un indirizzo IPv4 va lì. Un nome viene risolto di nuovo quando il server si connette, e la
richiesta va all'indirizzo che è stato controllato. Per queste destinazioni non si usa alcun proxy. Solo un
amministratore, nelle impostazioni condivise, imposta una destinazione dentro la rete.

Un server solo IPv6 dietro un DNS64 sul prefisso noto non raggiunge quindi alcuna destinazione propria
raggiungibile solo in IPv4.

## I limiti di uno spazio

- **Regole:** le impostazioni proprie di una persona contengono al massimo 500 riconoscitori, 500 regole sui
  campi e 500 pattern di valori da non mascherare mai, contati sulle impostazioni e su tutti i loro progetti. Un
  salvataggio oltre questi limiti viene rifiutato.
- **Progetti:** al massimo 200 per ogni persona.
- **Valori:** un valore più lungo di 16.384 caratteri viene oscurato invece che sostituito con un valore
  fittizio, e non viene mai conservato né ripristinato; un termine contiene al massimo 8.192 caratteri.
- **Memoria:** un'istanza dà ai suoi spazi 2 GiB fissi, e uno spazio ne usa al massimo la metà. Uno spazio
  arrivato alla sua quota non impara nuovi valori: una richiesta che ne porta uno viene fermata con un messaggio
  che lo dice, finché non si dimenticano valori nella dashboard, non si riducono regole o progetti, o il server
  ne ha di più. Nessuno spazio di un'altra persona viene chiuso per fare posto.
- **Le statistiche** contano gli ultimi 100.000 scambi, e una persona tiene le sue dieci sessioni della
  dashboard più recenti.

## Il confine della protezione

Latet può esaminare soltanto le richieste supportate che passano attraverso di esso.
Se le regole aziendali richiedono che tutto il traffico verso i modelli passi dal server,
la rete deve impedire ai computer di raggiungere direttamente i fornitori. Configura e
mantieni questa restrizione al confine della rete; Latet non la installa né la applica.
Il traffico di rete arbitrario degli strumenti è fuori da questa garanzia.

[Cosa è protetto e cosa no](../protection-scope/) descrive i limiti di rilevamento,
immagini e protocolli. Contenuti non supportati possono essere rifiutati o trattenuti
su un percorso server protetto; un indicatore storico o un endpoint disponibile del
fornitore non ne dimostra il supporto. Il server non può leggere il repository di un
computer connesso solo perché un progetto appare nella dashboard. Le informazioni sul
progetto che possiede sono quelle che gli sono state fornite.

Per i comandi amministrativi, parti da [Amministrare un server di Latet](../../getting-started/server/).
