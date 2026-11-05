---
title: L'API per le applicazioni
description: Analizza, maschera e ripristina testo dai tuoi programmi, con una chiave API di un server Latet.
sidebar:
  label: Panoramica
---

Latet protegge ciò che un agente invia a un modello. Ma i programmi mandano testo sensibile anche
altrove: i loro log a Datadog, Splunk o Elastic, i loro documenti all'indice di un'applicazione RAG.
L'API per le applicazioni permette a un programma di chiedere a un server Latet, con il token di una
chiave API, di:

- **analizzare** un testo (`scan`): che cosa contiene di sensibile, e dove;
- **mascherarlo** (`mask`): lo stesso testo con valori fittizi realistici al posto dei suoi valori;
- **ripristinarlo** (`unmask`): i valori reali di nuovo al posto di quelli fittizi;
- **dimenticare** dei valori (`forget`): toglierli dalla cassaforte della chiave, quando una persona
  chiede che i suoi dati spariscano ([dimenticare dei valori](forget/)).

È una funzione del server Latet: le chiavi API e i loro token esistono solo lì, e un computer senza
server non ne ha nulla. Per provarla sul tuo computer, avvia lì un server Community con il profilo
server di `compose.yaml` ([gestione del server](../concepts/server-operations/)).

Che cosa dà in più di una regola di oscuramento:

- **Lo stesso valore fittizio ogni volta.** Un valore riceve lo stesso valore fittizio in ogni
  chiamata, su ogni istanza del server, finché la cassaforte della chiave lo conserva. In un servizio
  di log grafici e ricerche continuano a funzionare, e si può seguire un cliente per giorni di log
  senza che nessuno veda chi è. In un indice RAG, una persona resta la stessa persona da un documento
  all'altro.
- **Una sola cassaforte per tutto il flusso.** Documenti e domande mascherati con la stessa chiave
  condividono i valori fittizi, così un modello ragiona su valori fittizi coerenti, e `unmask` rimette
  i valori reali nella sua risposta.
- **Reversibile, sotto controllo.** Il ripristino è un permesso a sé, raggiunge solo i valori fittizi
  della chiave, e ogni ripristino attraverso l'API per le applicazioni viene scritto nel registro
  amministrativo del server.

Due pagine seguono i due usi per cui è nata: i [log](logs/) e il [RAG](rag/). Il
[riferimento HTTP](../reference/apps-api/) dà il contratto per i programmi in qualunque linguaggio, e
la [descrizione OpenAPI](../../apps-api.openapi.json) dell'API permette di generarne un client. I
programmi Python possono usare il pacchetto [`latet-client`](https://pypi.org/project/latet-client/),
che funziona con LangChain, e i workflow di n8n i [nodi Latet per n8n](n8n/).

## Una prima chiamata

Un programma invia una lista di testi e riceve una lista, nello stesso ordine:

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"texts": ["Rimborsa la fattura di mario.rossi@acme.it su IT60X0542811101000000123456, per favore."]}' \
  https://latet.acme.lan/_latet/apps/v1/mask
```

```json
{"results": [{"text": "Rimborsa la fattura di tommaso.gallo@margie-6e5a.example su IT17F3945471087320356678487, per favore.",
  "replacements": [{"type": "EMAIL", "class": "pii", "start": 23, "end": 56},
                   {"type": "IBAN", "class": "pii", "start": 60, "end": 87}],
  "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

`shop.key` contiene il primo token della chiave, che scrive `keys add` ([più sotto](#chiavi-api-e-token)).
`printf`, che la shell esegue da sé, passa l'intestazione a curl sul suo standard input (`-H @-`):
così il token non compare nella riga di comando di curl, dove altri utenti del computer potrebbero
leggerlo. I valori fittizi sono quelli della chiave: un'altra chiave, o un altro server, ne dà altri.
Con un token che può ripristinare, lo stesso testo inviato a `/_latet/apps/v1/unmask` torna com'era,
con `"restored": 2`.

## Chiavi API e token

Una chiave API è uno spazio sul server: le sue regole, la sua cassaforte di valori fittizi, la sua
cronologia. Può avere più **token**, i segreti che agiscono in quello spazio, e ogni token ha i suoi
**permessi**:

| Permesso | Che cosa può fare il token |
| --- | --- |
| `scan` | Dire che cosa contiene un testo e dove; non cambia nulla. |
| `mask` | Sostituire i valori di un testo con i valori fittizi della chiave. Un token che può mascherare può anche analizzare. |
| `unmask` | Rimettere i valori reali al posto dei valori fittizi della chiave. |
| `forget` | Togliere dei valori dalla cassaforte della chiave ([dimenticare dei valori](forget/)). Nessun altro permesso lo comprende. |

Dai a ogni programma solo ciò che gli serve. Un'applicazione che spedisce i suoi log ha bisogno di
`mask`; lo strumento di assistenza che legge una riga copiata dal servizio di log ha bisogno di
`unmask`; un'applicazione RAG che maschera i suoi documenti e ripristina la risposta del modello ha
bisogno di entrambi.

```sh
latet server --server https://latet.acme.lan keys add "Shop" --out shop.key
latet server --server https://latet.acme.lan keys add-token "Shop" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop" support --unmask --out support.token
latet server --server https://latet.acme.lan keys tokens "Shop"
```

Il primo token di una chiave, quello che crea `keys add`, ha `scan`, `mask` e `unmask`, e così il
token di ogni chiave creata prima che i token avessero permessi: quelle chiavi funzionano come prima.
Nessuno di loro può dimenticare: `forget` si dà a un token tutto suo
(`keys add-token "Shop" erasure --forget`). Il primo token della chiave ha anche un indirizzo per i
modelli, il [gateway](../getting-started/server/#dare-una-chiave-api-a-un-programma), e attraverso di
esso serve gli agenti della chiave: `keys tokens` lo indica con `serves agents (has an address)`, così
che non venga revocato al posto di un token aggiunto con gli stessi permessi. Un token creato con `keys add-token` non ha indirizzo, qualunque
siano i suoi permessi, `--mask --unmask` compresi: il gateway e il bridge del plugin lo rifiutano
(403), e chiama soltanto l'API per le applicazioni.

Ogni token si ferma per conto suo: `keys tokens "Shop" --revoke t_4kxq2m7zap` ne revoca uno per l'id
che `keys tokens` elenca, entro pochi secondi su ogni istanza. `keys rotate "Shop" --token
t_4kxq2m7zap --out shipper.token` sostituisce quel token con uno nuovo, con un nuovo segreto e un nuovo
id, lo stesso nome e gli stessi permessi; una chiave con un solo token non ha bisogno di `--token`.
Ogni modifica finisce nel registro amministrativo (`token.added`, `token.revoked`, `key.rotated`), con
gli id dei token, mai un segreto.

L'API per le applicazioni accetta solo i token delle chiavi API: il token del computer di una persona
viene rifiutato (403), e così una richiesta fatta da una pagina web. Le persone usano la loro
dashboard.

## Prosa e dati

`scan` e `mask` accettano un `kind` per i testi di una chiamata:

- `prose` (il predefinito): testo scritto da persone, come messaggi, ticket e documenti. Viene
  mascherato come il gateway maschera i tuoi messaggi.
- `data`: log, record, l'output di un programma. Viene mascherato come il gateway maschera l'output
  di uno strumento.

La differenza sta in ciò che è probabile: nel profilo `balanced` i nomi vengono cercati solo nella
prosa (con il [modello locale dei nomi](../concepts/server-operations/#modelli-di-riconoscimento-su-un-server)
installato sul server), e una sequenza di cifre conta come numero di telefono solo nella prosa, dove
in un log è molto più spesso un id. Con `phone_regions: [IT]` nelle regole della chiave, il numero in
`Chiamami domani al 3471234567.` riceve un valore fittizio come prosa e resta com'è come dati.
Un documento JSON inviato come testo viene mascherato come testo, con la sua impaginazione, così le
posizioni nella risposta sono posizioni in esso.

## Che cosa legge un mascheramento

`scan` e `mask` usano le impostazioni della chiave come sono quando arriva la chiamata: le regole
proprie della chiave sopra le impostazioni condivise del server, come fa il gateway per le richieste
della chiave. Non ci sono regole solo per l'API. Si impostano con `latet server config --key` e
`latet server rules --key`
([impostazioni condivise e regole delle chiavi API](../getting-started/server/#configurare-le-impostazioni-condivise-e-le-regole-delle-chiavi-api)).

- **Il revisore locale**, quando le regole della chiave ne indicano uno, legge ogni testo di una
  chiamata `mask` prima della risposta, qualunque cosa dica `llm_review.inline` per il gateway:
  un'applicazione non ha un momento successivo. Ha a disposizione `llm_review.budget_ms` se
  impostato, altrimenti 30 secondi. Un testo la cui revisione fallisce segue `llm_review.on_failure`:
  con `stop` (il predefinito) la risposta è `blocked`, con `send` è il testo come lo hanno mascherato
  le regole. Quindi, con un revisore fermo, ogni testo viene bloccato finché non torna a rispondere.
- **`scan` non esegue il revisore locale.** Il revisore insegna alla cassaforte ciò che trova, e
  l'analisi non cambia nulla. Su una chiave con un revisore, `scan` può riportare meno di quanto
  `mask` sostituisce.
- **Il Revisore Live non trattiene mai il testo di un'applicazione.** Un'applicazione non può
  aspettare una persona: un testo viene mascherato solo dalle regole e dal revisore locale, anche
  quando le regole della chiave attivano il Revisore Live.
- **La protezione dai dump** vale per i testi `data` come per l'output di uno strumento: uno che
  contiene più valori personali di quanti ne permetta `dump_guard` riceve `blocked`.

Un testo bloccato torna come `{"blocked": "<motivo>"}` senza testo, e il motivo non nomina alcun
valore. Quando qualcosa ha funzionato in modo ridotto durante la chiamata (un rilevatore che non ha
potuto leggere un testo, una revisione che non è finita), la risposta lo dice in `"degraded"`, con i
nomi di ciò che lo ha fatto, come `ner` o `llm-review`.

## Mascherato due volte, ripristinato due volte

`mask` considera reale ogni valore che trova, anche i valori fittizi della chiave stessa: nessuno può
capire da una stringa se è un valore fittizio o un valore reale che gli somiglia. Un testo mascherato
due volte contiene valori fittizi di valori fittizi, e un ripristino lo riporta indietro di un passo:

| Passo | Testo |
| --- | --- |
| Originale | `Scrivi a mario.rossi@acme.it` |
| Mascherato | `Scrivi a tommaso.gallo@margie-6e5a.example` |
| Mascherato di nuovo | `Scrivi a simone.desantis@humongous-0e54.example` |
| Ripristinato una volta | `Scrivi a tommaso.gallo@margie-6e5a.example` |

Maschera un testo una volta sola, partendo dall'originale. Il gateway segue la stessa regola: un testo
mascherato con l'API e poi inviato a un modello attraverso il gateway di Latet viene mascherato di
nuovo lì, e la risposta del modello torna con i valori fittizi dell'API. Per questo un'applicazione
RAG invia il testo mascherato direttamente al modello e ripristina la risposta con l'API
([RAG](rag/)).

## Chi può sapere che cosa

**Il ripristino** dà i valori reali dei valori fittizi della chiave, qualunque chiamata o conversazione
della chiave li abbia creati, anche quelli dell'anno scorso. Non raggiunge i valori fittizi di altre
chiavi né quelli di una persona: il testo che non è un valore fittizio della chiave resta com'è. Il nome
di una persona torna solo intero: una sua parte da sola (un nome di battesimo) può essere una parola
comune, o il nome di un'altra persona, e resta com'è. Ogni ripristino viene scritto nel registro
amministrativo prima che parta la risposta (`apps.unmasked`, con il token, quanti testi e quanti valori;
mai un testo o un valore), e un registro che non riesce a scriverlo rifiuta la chiamata (503). Il primo
token della chiave, che può analizzare, mascherare e ripristinare, serve anche agli agenti attraverso il
gateway: le loro risposte vengono ripristinate come quelle di ogni agente, senza una voce del registro
amministrativo per ogni ripristino. Un token creato con `keys add-token` non lo fa mai, quindi ogni suo
ripristino è una voce `apps.unmasked`. Dai `unmask` agli strumenti delle persone che possono vedere i dati
reali, e leggi il registro con `latet server log --action apps.unmasked`.

**Anche il mascheramento rivela qualcosa.** Con un token che può mascherare e l'accesso ai log
mascherati, qualcuno può confermare un'ipotesi: maschera `mario.rossi@acme.it` e cerca nei log il suo
valore fittizio. Se c'è, Mario Rossi è un cliente. Per questo dai `mask` solo ai programmi che ne
hanno bisogno, conserva il token dove tengono i loro segreti, e tratta i log mascherati come dati
pseudonimi, non anonimi.

**Anche l'analisi rivela qualcosa.** Non crea valori fittizi e non cambia nulla, ma riporta i valori
che la cassaforte della chiave conosce già, ovunque compaiano. Un token che può solo analizzare può
quindi confermare che un valore che nessun rilevatore troverebbe da solo (un nome in un testo `data`,
un valore che una regola della chiave ha trovato prima di essere tolta) è uno di quelli della chiave.
Dai `scan` con la stessa cura di `mask`.

**Anche l'oblio rivela qualcosa.** La sua risposta dice quanti dei valori la cassaforte della chiave
conteneva, quindi un token che può dimenticare può confermare che un valore è della chiave, come uno
che può analizzare. Dai `forget` solo al processo che gestisce le richieste di cancellazione; ogni
oblio finisce nel registro amministrativo (`apps.forgotten`, con il token e i conteggi, mai un
valore).

## La cassaforte di una chiave

La cassaforte di una chiave conserva ogni valore che ha mascherato, con il suo valore fittizio,
finché la chiave esiste: un ripristino funziona su una riga di log dell'anno scorso, e un cliente
mantiene lo stesso valore fittizio per anni. I valori non scadono: un valore lascia la cassaforte quando
viene dimenticato ([dimenticare dei valori](forget/)), e ogni valore se ne va con la chiave, perché
`latet server keys remove` elimina il suo spazio, e con esso la sua cassaforte. Un valore trovato una
volta viene mascherato da allora ovunque compaia, anche dove nessun rilevatore lo troverebbe di nuovo:
l'indirizzo di un cliente che una regola della chiave ha trovato una volta, e che poi è stata tolta,
resta mascherato.

Uno spazio di log può contenere milioni di valori, quindi un'istanza non tiene in memoria la
cassaforte di una chiave. Tiene i valori usati più di recente dalla chiave (`key_working_values`,
5.000 per impostazione predefinita, circa 10 MiB per chiave) e le impronte di tutta la cassaforte:
un'impronta di 64 bit di ogni parola dei suoi valori e dei suoi valori fittizi, calcolata con una
chiave segreta, circa 15 MB per un milione di valori. Le parole di una chiamata vengono cercate prima
tra le impronte: una parola senza impronta non è di nessun valore né di nessun valore fittizio, e a
PostgreSQL non viene chiesta. I valori e i valori fittizi le cui parole ne hanno una vengono letti da
PostgreSQL, tutti quelli della chiamata in una volta, e ognuno viene decifrato e confrontato con il
testo prima di mascherare o ripristinare qualcosa, perché due parole possono avere la stessa
impronta. Né le impronte né l'indice delle parole in PostgreSQL contengono una parola in chiaro.
Prima di ogni chiamata l'istanza legge in PostgreSQL il contatore delle modifiche della chiave e
recepisce ciò che le altre istanze hanno conservato o dimenticato nel frattempo, così un valore
conservato su un'istanza prima che una chiamata cominci viene trovato da quella chiamata su
qualsiasi altra, anche quando il messaggio che lo annunciava è andato perso. Una chiave con pochi
valori funziona allo stesso modo: solo la cassaforte di una persona con 35.000 valori o meno viene
tenuta intera in memoria ([gestione del server](../concepts/server-operations/)).

**Segreti attaccati ad altre lettere.** Da PostgreSQL, un segreto scritto come un'unica sequenza di lettere e
cifre, senza spazi né punteggiatura, viene trovato dalla sua parola, e dai suoi primi e ultimi sei caratteri
quando altre lettere o cifre vi sono attaccate: su un lato, al bordo della sequenza del testo che lo contiene, e
su entrambi i lati (`xhunter2y`), dentro quella sequenza, che una chiamata legge sei caratteri alla volta. Una
chiamata legge al massimo un milione di queste posizioni: oltre, le sue sequenze successive vengono lette solo
per le loro parole e i loro bordi. Un segreto di sei caratteri o meno, o la cui parola a un bordo attaccato ha
sei caratteri o meno, non viene trovato da nessuna parola del testo quando è attaccato. Per questo un'istanza
tiene in memoria per sempre i segreti brevi di una chiave, quelli di un'unica sequenza di 16 caratteri o meno o
con una parola così breve a un bordo: i `key_held_secrets` più recenti (1.000, se le impostazioni del server non
dicono altro), trovati comunque siano attaccati, anche oltre il milione di posizioni di una chiamata. Uno più
vecchio, o uno più lungo, viene trovato dove niente gli è attaccato, attaccato su un lato quando la parola a
quel bordo ha sette caratteri o più, e attaccato su entrambi i lati quando è un'unica sequenza di sette
caratteri o più, entro il milione di posizioni della chiamata. Aumenta `key_held_secrets` per una chiave che
vede molti segreti brevi, come un token di sessione in ogni riga di log: ognuno tenuto occupa circa 23 KB su
ogni istanza che serve la chiave, per lo più nel rilevatore che lo trova, e ogni testo che la chiave maschera o
ripristina viene letto per cercarlo, il che rallenta un po' le sue chiamate. Misurata con Latet 0.10.0 sul
computer di [quanto è veloce](#quanto-è-veloce), con un token di sessione proprio in ogni riga (un valore in più
da conservare in ognuna), una chiave nuova con 1.000 di questi segreti tenuti ha mascherato 73 righe al secondo
e ne ha ripristinate 1.935, contro 75 e 2.103 senza segreti tenuti. Un valore fittizio viene trovato attaccato
come il suo segreto: ne mantiene i segni al loro posto, così un valore fittizio attaccato in modo che nessuna
delle sue parole resti a sé è quello di un segreto tenuto in memoria, e viene trovato finché il segreto è
tenuto. Come ovunque in Latet, un valore viene riconosciuto di nuovo da quattro caratteri, il nome di una
persona da tre ([valori noti](../concepts/detection/#valori-noti)).

**Segreti brevi.** Un segreto di meno di dodici caratteri (`password=1234`, un token breve) riceve
un valore fittizio più lungo degli stessi tipi, con i suoi segni al loro posto: quindici cifre per
un numero (con il suo segno e il suo punto decimale, così che un numero JSON resti un numero),
dodici cifre esadecimali per un valore esadecimale, altrimenti dodici lettere e cifre. Un valore
fittizio breve quanto il suo valore comparirebbe per caso negli id e nei numeri di altre righe di
log, e un ripristino cambierebbe righe che non hanno mai contenuto il segreto.

Un valore fittizio breve che la cassaforte di una chiave ha dato prima di Latet 0.10.2 (di otto
caratteri o meno) resta, come ogni valore fittizio. `unmask`, che legge ogni valore fittizio della
cassaforte della chiave, lo rimette solo dove sta come parola intera, senza lettere, cifre o
trattini bassi accanto. Il costo: dove il valore di un segreto del genere è stato mascherato dentro
una parola o un numero più lungo (`postgres` in `postgresql://`, `1234` in `order_id=9912343`),
`unmask` vi lascia il suo valore fittizio. Una conversazione del primo token della chiave,
attraverso il gateway o il plugin, lo rimette come prima, anche dentro una parola, perché ripristina
solo i valori fittizi che la conversazione ha visto. Finché un'istanza del server esegue ancora la
0.10.0, dà valori fittizi brevi ai nuovi segreti brevi, e anche quelli restano: aggiorna ogni
istanza.

**Che cosa conserva una chiamata che fallisce.** Una chiamata `mask` che fallisce mentre maschera (un
507, un commit che non arriva a PostgreSQL) non conserva nessuno dei suoi valori nuovi quando teneva
per essi una delle transazioni dell'istanza, tre alla volta con il `database_pool` predefinito: lo
spazio della chiave li dimentica. Quando quelle erano occupate, la chiamata ha conservato ogni valore
nuovo man mano, e quelli restano nella cassaforte della chiave. Un errore dopo il mascheramento, nella revisione locale o nel preparare la risposta,
arriva dopo il commit, quindi anche in quel caso i valori nuovi della chiamata restano. Mascherare di
nuovo i testi è sicuro in ogni caso: un valore che la cassaforte ha conservato ha il suo valore
fittizio, e uno che ha dimenticato ne riceve di nuovo uno. Un agente della stessa chiave che nel
frattempo ha usato un valore dimenticato attraverso il gateway ha già inviato il suo valore fittizio,
che nessun ripristino rimetterà a posto.

## Limiti

- **La cassaforte.** La cassaforte di una chiave contiene i valori di `vault_limit` del server (200.000
  se le impostazioni del server non dicono altro), oppure un limite proprio:
  `latet server keys limit "Shop" 5000000` (da 1.000 a 100.000.000; `default` le ridà quello del
  server). Una cassaforte piena rifiuta con 507 la chiamata `mask` che aggiungerebbe un valore: della
  chiamata non torna nulla, e nulla esce in chiaro. Lo stato del server elenca le identità al 90% del
  loro limite, e la metrica `latet_vault_near_limit` le conta, quindi alza un limite prima che venga
  raggiunto. Ogni istanza conta i valori nuovi di una chiamata prima di confermarli: due istanze, o
  una chiamata e un agente della stessa chiave, possono superare il limite dei valori nuovi di una
  chiamata ciascuna prima che il valore successivo venga rifiutato. Un indice delle impronte di una
  cassaforte viene costruito fino a 16.777.216 impronte, circa otto milioni di valori del tipo che
  [quanto è veloce](#quanto-è-veloce) misura: oltre, nessun indice viene costruito finché lo spazio
  della chiave dura, e le sue chiamate cercano le loro parole solo in PostgreSQL, più lentamente.
- **Una chiamata** accetta al massimo 1.000 testi e l'`apps_request_mb` del server (8 MiB se le sue
  impostazioni non dicono altro, mai più del suo `max_request_mb`): una chiamata attende in memoria il
  thread della chiave, e `latet-client` divide una lista più lunga in chiamate di 8 MiB al massimo. I
  testi le cui parole nominano più di 100.000 valori o valori fittizi della cassaforte vengono
  rifiutati per intero con 422, con parole fisse che non nominano alcun valore: della chiamata non
  viene mascherato né ripristinato nulla, quindi invia meno testi per chiamata. Un 503 è un'altra
  cosa: un archivio del server (PostgreSQL, Valkey) non ha risposto, e la stessa chiamata si può
  inviare di nuovo più tardi ([riferimento HTTP](../reference/apps-api/)).
- **Chiamate contemporanee.** Una chiave ha al massimo `requests_in_flight` richieste di applicazioni
  su un'istanza (16 se le impostazioni non dicono altro); oltre, 429 con `Retry-After`.

## Quanto è veloce

Le chiamate di una chiave vengono eseguite una dopo l'altra su un'istanza, nel thread del suo spazio;
le chiamate di chiavi diverse, e quelle che arrivano a istanze diverse, vanno in parallelo. Per andare
più veloce con una chiave, aggiungi istanze dietro il bilanciatore di carico, o più worker su una
macchina Linux ([più istanze su una macchina](../concepts/server-operations/#più-istanze-su-una-macchina)).

Misurato con Latet 0.11 su un'istanza (Intel i9-10850K, 64 GB, Windows 10, PostgreSQL 18 in Docker
Desktop sullo stesso computer, le impostazioni predefinite del server): 40.000 righe di log di circa
200 caratteri, ciascuna con un indirizzo email e un indirizzo IPv4 pubblico, metà delle quali nomina
un cliente già visto, in chiamate da 500 come `data`; poi 50 documenti di circa 20 KB di prosa con
dieci valori ciascuno, uno per chiamata. Le righe al secondo sono le righe diviso il tempo che le loro
chiamate hanno impiegato; il tempo di un documento è la mediana delle sue 50 chiamate:

| La cassaforte della chiave | Mascheramento, righe al secondo | Ripristino, righe al secondo | Un documento, mascheramento / ripristino |
| --- | ---: | ---: | ---: |
| Nuova, impara i valori strada facendo | 191 | 2.894 | 126 / 31 ms |
| 200.000 valori | 871 | 2.960 | 117 / 32 ms |
| 1.000.000 di valori | 773 | 2.967 | 124 / 32 ms |

Ogni documento porta valori nuovi per la chiave, che li scrive in PostgreSQL mentre li maschera: gli stessi 50
documenti mascherati di nuovo, con i valori già noti, hanno richiesto 16,6 ms a 200.000 valori e 16,4 ms a
1.000.000 (ripristino 23 ms in entrambi i casi). Anche una chiave nuova scrive la maggior parte dei valori delle
sue righe, ed è per questo che ne maschera meno.

Latet 0.10.0, misurato allo stesso modo sullo stesso computer, mascherava 350 e 304 righe al secondo
a 200.000 e 1.000.000 di valori e 153 per una chiave nuova, ne ripristinava 2.114 e 2.014, e
mascherava un documento in un tempo da 163 a 182 ms. Latet 0.11 si proponeva di mascherare almeno
1.000 righe di questo tipo al secondo su un'istanza a 200.000 e a 1.000.000 di valori, e un documento
in 30 ms o meno: ne maschera 871 e 773, e un documento in 30 ms solo quando la chiave ne conosce già i
valori. La maggior parte del tempo di una chiamata di mascheramento va ai rilevatori, e una parte
all'attesa di PostgreSQL.

Un'istanza trova i valori di una chiave attraverso le impronte delle parole della sua cassaforte ([la cassaforte
di una chiave](#la-cassaforte-di-una-chiave)), che occupano circa 15 MB per un milione di valori (14,77 MB a
1.000.000) e vengono costruite in background mentre lo spazio della chiave si apre: in 1,5 secondi a 1.000.000
di valori (la mediana di tre aperture, da 1,42 a 1,62 s). Finché non sono pronte, le chiamate della chiave
cercano le loro parole solo in PostgreSQL: in una misurazione precedente con Latet 0.11, con le impronte
disattivate, la chiave da 200.000 valori ha mascherato 494 righe al secondo e ne ha ripristinate 1.382. Ogni
chiamata legge prima il contatore delle modifiche della chiave, 0,51 ms come mediana, e recepisce ciò che è
cambiato nel frattempo: 1,87 ms per un valore nuovo e 2,11 ms per 100, anche questi come mediana.

In una chiamata di testi già mascherati, solo quelli nuovi vengono letti di nuovo, ma ognuno degli
altri viene comunque cercato e confrontato con ciò che la chiave ha mascherato: 500 testi già
mascherati e uno nuovo hanno richiesto 168 ms, quello nuovo da solo 6,1 ms (mediane di tre
esecuzioni).

Su una macchina Linux i worker si sommano. Misurato in Docker sullo stesso computer (una macchina
virtuale Linux con 20 processori e 16 GB, PostgreSQL 18.6 e Valkey 9.1.2 in container accanto), con
`latet server run --workers` e una chiave API da 200.000 valori: 200.000 righe distinte del tipo
descritto sopra in chiamate da 62, quattro connessioni per worker, ogni numero di worker per 20
secondi, la mediana di tre giri; la memoria è la memoria residente di ogni worker dopo i giri:

| Worker | Mascheramento, righe al secondo | Ripristino, righe al secondo | Memoria per worker |
| ---: | ---: | ---: | ---: |
| 1 | 1.190 | 1.947 | 227 MB |
| 2 | 2.260 | 3.711 | 227 MB |
| 4 | 4.253 | 6.594 | 216 MB |
| 8 | 7.477 | 10.147 | 185 MB |

Otto worker hanno mascherato 6,28 volte quanto uno e ripristinato 5,21 volte, dove Latet 0.11 si
proponeva di arrivare ad almeno 5 volte in entrambi; nessun giro con otto worker ha ripristinato a meno
di 5,07 volte. Il kernel assegna le connessioni ai worker secondo un hash, non a turno, quindi alcuni
worker ne ricevono più di altri: da 1 a 10 delle 32 in un giro con otto worker. Il processo che avvia
i worker occupava da 47 a 48 MB accanto a loro. Queste chiamate arrivano da più connessioni insieme,
quindi confronta le righe di questa tabella tra loro, non con la prima tabella.

Scrivere valori nuovi richiede connessioni a PostgreSQL: un'istanza, e ogni worker, ne tiene al
massimo `database_pool` (10, se le impostazioni del server non dicono altro) per il lavoro di ogni
identità, e lascia che tre decimi di esse (tre su dieci) siano tenute da chiamate che conservano i
loro valori nuovi in una transazione ciascuna; le altre conservano ogni valore man mano, più
lentamente. Aumenta `database_pool` per un'istanza che maschera per molte chiavi nuove alla volta, e
con esso il `max_connections` di PostgreSQL
([gestione del server](../concepts/server-operations/#connessioni-limiti-e-fiducia)).
`scripts/bench_apps.py` nel repository misura un tuo server come è stata misurata la prima tabella.

## Aggiornare un server

Latet 0.11 cambia le tabelle. Ferma ogni istanza e ogni worker, su ogni macchina, esegui
`latet server migrate` una volta con la 0.11, poi avviali tutti con essa
([aggiornare](../concepts/server-operations/#aggiornare-e-aggiungere-istanze)). Un'istanza non parte
su tabelle di un'altra versione, quindi tra l'arresto e l'avvio nessuna chiamata riceve risposta, e
`latet server migrate` non cambia nulla finché un'istanza è attiva, una della 0.10 per 30 secondi
dopo il suo arresto. La migrazione aggiunge a ogni riga di parola di ogni cassaforte un posto vuoto
per la sua impronta e per il conteggio della modifica che l'ha scritta per ultima, con i loro indici:
su un database con casseforti grandi richiede più tempo delle migrazioni precedenti, e le impronte
stesse vengono calcolate dopo, in background.

Una cassaforte conservata da Latet 0.10 non ha ancora impronte. Dopo il riavvio, la prima istanza che costruisce
lo spazio di una chiave risponde subito alle chiamate della chiave, cercando le loro parole in PostgreSQL come
faceva la 0.10, e calcola le impronte della cassaforte in background, una volta, un'istanza alla volta; ogni
istanza poi costruisce il proprio indice delle impronte e lo usa. Fino ad allora le chiamate della chiave
mascherano gli stessi valori, più lentamente ([quanto è veloce](#quanto-è-veloce): 494 righe al secondo a
200.000 valori con le impronte disattivate, in una misurazione precedente), e nessuna viene rifiutata per
questo. Quando le impronte non sono ancora tutte calcolate o un indice non si può costruire (PostgreSQL o Valkey
che non rispondono, un aggiornamento dell'indice fallito), le chiamate restano sulla ricerca in PostgreSQL
finché l'indice non viene costruito di nuovo: subito quando un'altra istanza ha calcolato le impronte,
altrimenti 10, 20 e 30 secondi dopo ogni tentativo e poi una volta al minuto. Il log dice una volta quando un
indice smette di essere usato e una volta quando viene usato di nuovo. Una cassaforte oltre la dimensione per
cui si costruisce un indice ([limiti](#limiti)) resta sulla ricerca in PostgreSQL finché il suo spazio dura. Le
parole stesse restano come le ha fatte la 0.10: la 0.11 non le rifà.

Una versione che cambia il modo in cui sono fatte le parole di una cassaforte (il changelog lo dice)
rifà le parole di una chiave nella prima istanza che ne costruisce lo spazio, prima che quello spazio
risponda alla prima chiamata della chiave: Latet 0.10.0 impiegava circa 40 secondi per 200.000 valori
e quattro minuti e mezzo per 1.000.000 sul computer indicato sopra. Una chiamata che arriva nel
frattempo aspetta, e un'istanza costruisce quattro spazi alla volta, quindi poche chiavi grandi
ritardano le prime chiamate delle altre chiavi su quell'istanza: dopo una versione del genere, invia
a ogni chiave grande una chiamata, con un timeout abbastanza lungo, prima che i suoi programmi
ripartano.

Mentre istanze di due versioni girano insieme, cosa che una versione che non cambia le tabelle
consente, un valore scritto da un'istanza più vecchia viene trovato dalle più nuove finché lo tengono
in memoria, ma non in PostgreSQL finché lo spazio della chiave non viene ricostruito. Quando l'API per
le applicazioni è in uso, ferma ogni istanza più vecchia prima che partano quelle nuove, anche quando
le tabelle restano le stesse.
