---
title: Regole bloccate
description: Direttive che la tua azienda imposta sul suo server di Latet, sotto le quali nessuna persona e nessuna chiave API può scendere.
---

Con Latet Enterprise un'azienda stabilisce un **livello minimo** sul suo [server di Latet](../../getting-started/server/):
impostazioni sotto le quali nessuna persona e nessuna chiave API può scendere. Ogni persona mantiene le proprie
impostazioni e può sempre aggiungere protezione. Una modifica che scenderebbe sotto il livello minimo viene rifiutata,
con il messaggio che l'impostazione è definita dall'azienda; viene rifiutata, non messa in attesa di approvazione,
perché la persona non può approvarla. Una modifica viene rifiutata solo per ciò che abbassa essa stessa: le
impostazioni che erano già sotto il livello minimo non impediscono di modificare altro.

## Il file delle direttive

Le direttive sono un file di regole, nel formato che scrive `latet rules export` (vedi
[Esportazione e importazione](../../concepts/export-import/)), quindi un'azienda può partire da un'esportazione:

```yaml
latet_rules: 1
exceptions: company
profile: max
terms:
  - {value: Project Falcon, type: PROJECT}
  - {value: Northwind Traders, type: CLIENT}
policies: {IBAN: redact}
ocr: auto
```

**Ciò che il file indica è bloccato, e ciò che omette resta a scelta di ciascuna persona.** Con questo file nessuno può scegliere un
profilo inferiore a `max` né smettere di leggere il testo nelle immagini, Project Falcon e Northwind Traders sono
sempre mascherati, gli IBAN sono sempre oscurati, e contano solo le eccezioni dell'azienda (qui non ne imposta
nessuna); chiunque può comunque aggiungere termini, riconoscitori e valori propri più rigorosi. Per bloccare
un'impostazione al valore predefinito di Latet, scrivila nel file.

- **Il profilo porta con sé ciò che lo accompagna:** con `profile: max` sono bloccati anche i numeri di
  identificazione di ogni paese, gli indirizzi IP privati, la modalità dati rigorosa e la regola di max per le
  immagini.
- **Gli elenchi sono un minimo:** i termini, i riconoscitori, le regole di campo, i tipi di dati personali, i paesi e
  le regioni telefoniche nelle direttive, e i file e gli indirizzi web che la modalità dati rigorosa copre, sono
  sempre in vigore, e quelli di ciascuna persona si aggiungono a questi. Un termine, un riconoscitore o una regola di
  campo di un tipo che le direttive consentono (`policies: {CLIENT: allow}` e un termine di tipo `CLIENT`) è invece
  un'eccezione dell'azienda: non viene aggiunto alle impostazioni di nessuno, e una persona che maschera quel valore
  con un termine o un riconoscitore propri continua a mascherarlo.
- **I riconoscitori e le regole di campo dell'azienda si usano accanto a quelli di ciascuna persona:** un
  riconoscitore o una regola di campo propri di una persona non vengono mai messi fuori uso da quelli dell'azienda. Se un
  riconoscitore di una persona ha il nome o il tipo di uno dell'azienda, quello dell'azienda viene usato con un nome
  o un tipo propri; se una regola di campo di una persona ha il nome di una dell'azienda, le due vengono unite e il
  campo viene mascherato.
- **Le indicazioni per il revisore locale** contenute nelle direttive gli vengono sempre date, prima di quelle
  della persona.
- **Anche in ogni progetto:** Latet resta attivo in ogni progetto, e le impostazioni di un progetto non lo portano mai
  sotto il livello minimo.
- **Nuovi membri delle richieste:** `unknown_members: refuse` rifiuta, per ogni persona e chiave API, un membro di una
  richiesta che nessun profilo di richiesta nomina ancora, come faceva Latet prima della 0.15.0, mentre un testo o un
  elenco di testi fuori da un elemento del provider passa ancora, in una richiesta che Latet si limita a verificare
  (Claude Code, Bedrock, Vertex), una volta letto dal controllo finale ([Nuovi membri delle
  richieste](../../getting-started/upgrading-to-0-11/#nuovi-membri-delle-richieste)); `unknown_members: check` non
  blocca nulla, perché chiunque può rifiutarli.

`latet policy check policy.yaml` legge un file prima che venga messo in vigore: cosa blocca, cosa lascia a ciascuna
persona, di chi contano le eccezioni e cosa non si è potuto leggere. Un'impostazione con un errore, o scritta senza
valore (`ocr:` da solo), non viene letta, quindi non blocca nulla. Il comando non apre nessuna connessione, ed esce con
codice 0 se tutto va bene, 1 se qualcosa nel file non viene letto e 2 se il file non si può leggere affatto.

## Eccezioni

I valori e i pattern da non mascherare mai, le regole per i segreti disattivate e ciò che lascia valori in chiaro (un
riconoscitore il cui criterio è `allow`, `allow` per un tipo in `policies`, una regola di campo con `mask: false`)
sono le eccezioni. La chiave `exceptions` delle direttive dice di chi contano:

- **`personal`** (il valore predefinito): ogni persona può ancora segnare un valore come **Non sensibile** per sé, e
  mantiene le proprie scelte `allow`, tranne che per i tipi dei termini che l'azienda maschera.
- **`company`**: contano solo le eccezioni nelle direttive. Quelle proprie di una persona vengono rifiutate quando le
  aggiunge, e non usate dove c'erano già: i suoi valori e pattern da non mascherare mai, le sue regole per i segreti
  disattivate, i suoi riconoscitori che lasciano in chiaro ciò che trovano, i suoi `allow` per un tipo che l'azienda
  non consente e le sue regole di campo con `mask: false`. Anche una sua regola di campo di un tipo che l'azienda
  consente (`policies: {URL: allow}` nelle direttive, e una sua regola di campo di tipo `URL`) è un'eccezione, perché i
  valori del campo resterebbero in chiaro, anche dove la modalità dati rigorosa li maschererebbe: viene rifiutata
  quando la aggiunge, non viene usata dove c'era già, e **Regole** dice quante delle sue vengono escluse in questo modo. Dove un
  tipo del genere è mascherato in uno dei suoi progetti, la regola di campo viene mantenuta, e il suo tipo viene
  mascherato ovunque avrebbe lasciato il campo in chiaro. Un suo termine di un tipo del genere viene mantenuto, e
  salvato quando lo aggiunge: non lascia mai in chiaro nulla che l'azienda
  maschera (un termine dell'azienda, ciò che trovano un riconoscitore o una regola di campo dell'azienda, i dati
  personali) né il dominio di un indirizzo, ovunque il mascheramento lo maschererebbe senza i termini della persona. Tra i suoi
  termini vince la corrispondenza più lunga, come senza direttive. La sua identità (il suo nome, i nomi utente, gli
  indirizzi email e i nomi host), di un tipo che l'azienda consente, viene mantenuta allo stesso modo: non lascia mai
  in chiaro ciò che il mascheramento maschererebbe senza di essa. Dove il suo account è collegato al provider di
  identità dell'azienda (SCIM, o l'accesso unico) e l'azienda lascia passare i nomi (`PERSON`), il suo nome è quello
  che dà il provider dove anche le sue impostazioni lasciano passare i nomi, in ciascuno dei suoi progetti;
  altrimenti resta in uso il suo nome, mascherato ovunque i nomi siano mascherati. In ogni caso, cambiarlo dalle sue
  impostazioni viene rifiutato; un computer lo inserisce solo dove non ce n'è uno.

I termini dell'azienda di un tipo che l'azienda non consente restano mascherati, come li ha scritti l'azienda. Sono bloccati nel
mascheramento stesso: un termine viene mascherato qualunque valore da non mascherare, pattern, riconoscitore o criterio
`allow` una persona imposti, e nessuno può segnarne uno come **Non sensibile**. Se una persona ha consentito un testo
più lungo che contiene un termine, il resto rimane come l'ha consentito e il termine viene mascherato. Un valore di
`exceptions` che Latet non riesce a leggere, o nessun valore (`exceptions:` da solo), conta come `company`, il più
rigoroso, e `latet policy check` lo dice.

Con `personal`, solo i termini dell'azienda sono protetti in questo modo. Le eccezioni proprie di una persona possono
ancora lasciare in chiaro ciò che trova un riconoscitore o una regola di campo dell'azienda: un testo più lungo che ha
consentito e che lo contiene, oppure un suo termine, riconoscitore o regola di campo di un tipo che consente. Per
escludere questa possibilità, usa `exceptions: company`.

Le eccezioni di richiesta (le [eccezioni
esatte](../../getting-started/upgrading-to-0-11/#approvare-uneccezione-esatta) che lasciano passare un header, un
parametro di query o un membro del corpo) seguono la stessa chiave. Con `personal` quelle di una persona sono sue: le
approva come senza direttive, e su quelle di una chiave API si decide come sulle sue altre modifiche. Con `company` sono
dell'azienda: chi ne chiede una la propone, e la richiesta attende il team di sicurezza in **Approvazioni aziendali**,
chiunque decida sul resto delle sue modifiche; la sua approvazione, o quella di chi controlla una chiave API, non ne
concede nessuna. Vale anche per quelle approvate prima che le direttive dicessero `company`, comprese le permanenti: da
quando il server applica le direttive non concedono nulla, la dashboard mostra ciascuna come *Non in vigore: sulle
eccezioni di richiesta decide il team di sicurezza della tua azienda, che non ha approvato questa eccezione*, e
`latet server status` la elenca come `forbidden` tra le eccezioni di richiesta che ora non concedono nulla, quando il suo spazio ha
applicato le direttive: uno spazio non aperto su nessuna istanza da quando le direttive sono cambiate vi mostra le sue
eccezioni com'erano finché non viene ricostruito, mentre le sue richieste sono già giudicate dalle nuove direttive.
Chiesta di nuovo, un'eccezione attende il team di sicurezza; quelle eccezioni contano di nuovo ogni volta che l'azienda
non decide più su di esse: quando le direttive tornano a `personal` o vengono rimosse, quando la licenza viene rimossa,
o quando viene installata una licenza che non elenca le regole bloccate. Senza le approvazioni centrali nella
licenza nessuno può approvarne una: un'eccezione chiesta resta in attesa di un'approvazione che può solo respingerla
(chi l'ha chiesta può ritirarla, e la sua stessa approvazione viene rifiutata, dicendo che decide il team di sicurezza),
e non concede nulla. Perché le persone possano avere eccezioni di richiesta con `company`, la licenza deve elencare
`approvals`; senza, `company` blocca comunque tutte le altre eccezioni. Il salvataggio delle impostazioni di una chiave
API da parte dell'amministrazione approva comunque le eccezioni che aggiunge. Con l'uno o l'altro valore un'eccezione in
attesa non concede nulla e non viene mai rifiutata come se andasse sotto le direttive: ciò che le direttive giudicano è
la sua approvazione, quando arriva.

## Su un server di Latet

Con [Latet su un server](../../getting-started/server/), un amministratore imposta le direttive da qualsiasi computer
che abbia il token amministrativo del server:

```sh
latet policy check policy.yaml
latet server policy set policy.yaml
```

`latet server policy set` legge prima il file, e non cambia nulla se non si può leggere. Ciò che nel file non riesce a
leggere, lo segnala prima di inviare qualcosa, e chiede conferma: un'impostazione con un errore resta fuori per
intero, quindi un solo termine malformato lascia fuori dalle direttive tutti i termini del file. Chiede conferma anche
prima di impostare direttive che bloccano meno impostazioni di quelle del server, che permettono di nuovo a ogni
persona di aggiungere eccezioni dove contavano solo quelle dell'azienda, o che sostituiscono direttive che qui non
riesce a leggere; se lo esegui tu da un terminale chiede conferma, altrimenti `--yes` indica che sei d'accordo. Imposta il file al
posto delle direttive con cui l'ha confrontato: se nel frattempo qualcuno cambia le direttive del server, dalla pagina
**Direttive** o con un altro `latet server policy set`, il server rifiuta l'impostazione, e il comando rilegge le
direttive, le confronta di nuovo e chiede di nuovo conferma. Un agente AI viene rifiutato: impostare o rimuovere le
direttive spetta a una persona. Il `PUT` dell'API di amministrazione rifiuta un file con un'impostazione rimasta fuori
per intero (400, nominando l'impostazione), a meno che la richiesta non indichi anche `"accept_notices": true`. Un `PUT` che
indica anche `"etag"`, le direttive che sostituisce così come le ha date il `GET` (`null` se non ce ne sono), o un
`DELETE` il cui corpo indica allo stesso modo le direttive che rimuove, viene rifiutato con 409 quando quelle non
sono più le direttive conservate.

Ogni persona e ogni chiave API sul server viene portata al livello delle direttive: ogni istanza alza ogni spazio non
appena viene avvisata, e controlla di nuovo ogni 10 secondi, quindi un'istanza che ha perso il messaggio ha le
direttive entro 10 secondi. Il server le conserva nel suo database, con il testo sigillato con la chiave principale (i
termini al loro interno sono valori), e registra ogni modifica nel registro amministrativo, con le impostazioni che
blocca e mai un valore. `latet server policy show` dice quando e da chi sono state impostate, cosa impostano e se sono
in vigore, e `latet server policy show --file policy.yaml` scrive il loro testo in un file, per modificarlo.
`latet server policy remove` le toglie, dopo aver chiesto conferma: toglie le direttive che ha letto, e se nel
frattempo qualcuno cambia le direttive del server, il server rifiuta la rimozione, e il comando le rilegge e chiede
di nuovo conferma.

Con `roles` nella licenza, le persone con il ruolo Sicurezza possono impostarle anche dalla dashboard
([Accesso, gruppi e ruoli](../sign-in/)): nella pagina **Direttive** ne modificano il testo, poi scelgono **Salva**
oppure **Rimuovi**. Un testo che il server non riesce a leggere viene rifiutato con il motivo, e le direttive in vigore
restano. Un salvataggio indica le direttive che sostituisce: se qualcuno le ha cambiate dopo l'apertura del loro testo,
viene rifiutato, e la pagina mostra le direttive come sono ora. Il testo delle direttive contiene i termini e i valori
dell'azienda, quindi finché il server chiede l'accesso aziendale (`company_changes: sso`, il valore predefinito una
volta configurato l'accesso unico), leggerlo e modificarlo richiedono una sessione aperta con l'account aziendale. Ogni
modifica è nel registro amministrativo, a nome di chi l'ha fatta.

**Le direttive si applicano su ogni istanza man mano che viene aggiornata.** Un'istanza di una versione precedente, o
senza l'Enterprise Edition, non applica nessuna direttiva. Imposta le direttive quando ogni istanza usa già questa
versione, oppure impostale di nuovo a quel punto: `latet server policy set` e il `PUT` dell'API di amministrazione
avvisano, dicendo quante istanze attive non le applicano. Vedi
[Aggiornare e aggiungere istanze](../../concepts/server-operations/#aggiornare-e-aggiungere-istanze).

Finché le direttive bloccano dei termini (un loro termine di un tipo che non consentono) o dicono `exceptions: company`,
il server legge separatamente ogni testo di una chiamata dell'[API per le applicazioni](../../reference/apps-api/), come
faceva Latet 0.10, dove altrimenti legge insieme i testi di una chiamata: il mascheramento resta lo stesso, e una
chiamata con molti testi richiede più tempo. Lo stesso vale per i termini sincronizzati, finché ne viene mascherato
qualcuno
([Sincronizzazione dei termini](../term-sync/)).

Le impostazioni condivise (`latet server config`, `latet server rules`) restano come le imposta un amministratore:
ciò che al loro interno è sotto le direttive viene alzato in ogni spazio, come avviene per le impostazioni di una
persona. Impostare per una chiave API valori sotto le direttive viene rifiutato: `latet server config --key ... set`
esce con codice 2.

## Cosa vedono le persone

- **Nella dashboard**, un'impostazione definita dall'azienda mostra un lucchetto e **Definito dalla tua azienda** in
  **Regole** e nelle regole di un progetto. Una persona vede cosa è bloccato e quanti termini, eccezioni e regole ci
  sono, per esempio che 2 termini dell'azienda restano mascherati; mai i termini, né altri valori dell'azienda. Su un
  server, **Direttive** è per il ruolo Sicurezza, che legge e modifica lì il testo delle direttive, e per gli auditor,
  che leggono ciò che bloccano; finché la licenza non ha `roles`, tutti leggono ciò che bloccano. Gli amministratori le
  leggono con `latet server policy show`.
- **Una modifica rifiutata nomina l'impostazione:** `Set by your company, not changed: profile: from max to balanced`.
  La dashboard risponde con un conflitto (409) e mostra il rifiuto dove è stata fatta la modifica, e nel terminale il
  comando esce con codice 2. Una sola modifica che abbassa le impostazioni di più di 20 cartelle di progetto viene
  rifiutata per intero: modifica meno cartelle alla volta.
- **Le tue impostazioni restano come le hai impostate:** ciò che è sotto il livello minimo viene alzato mentre è in
  uso, e le tue impostazioni conservano ciò che hai scritto, quindi valgono com'erano scritte se le direttive vengono
  rimosse.
- **`latet server status`** ha una riga per le direttive: quando sono state impostate (in UTC), e un avviso quando non
  sono in vigore o non si possono leggere.

## La licenza

Le direttive sono in vigore con una licenza Latet Enterprise che include le regole bloccate
([Edizioni e licenza](../)). La protezione non scende mai: quando la licenza scade, supera i suoi utenti, è in pausa
o una prova termina, le direttive restano in vigore esattamente come sono. Impostare o rimuovere le direttive di un
server richiede una licenza che possa ancora gestire le impostazioni Enterprise: quando una prova termina, le
direttive sono congelate, e `latet server policy show` le mostra ancora, finché non viene installata una licenza.

Le direttive non sono più in vigore in due soli casi: quando la licenza viene rimossa e quando viene installata
una licenza che non elenca le regole bloccate. `latet server license remove` e `latet server license install` lo
dicono e chiedono conferma prima, e rifiutano un agente AI; le direttive del server vengono conservate, e tornano in
vigore con una licenza che elenca le regole bloccate.

## I limiti

- **Solo su un server di Latet:** l'Enterprise Edition è una funzionalità del server. Latet su un computer senza server è la Community
  Edition, qualunque cosa vi sia installata, e non ha direttive.
- **Un computer connesso a un server di Latet** viene mascherato dal server, con le direttive del server.
- **Un'istanza che non riesce a leggere le direttive:** direttive conservate ma illeggibili (create da una versione più
  recente, o sigillate con un'altra chiave principale) lasciano un'istanza che aveva già letto delle direttive ad
  applicare le ultime che ha letto: i suoi spazi restano su quel livello minimo più vecchio, che può essere più basso
  del nuovo, e `latet server status` dice che le nuove direttive non si possono leggere. Un'istanza che non ha mai
  letto delle direttive, o che non ha ancora letto la licenza, non serve nessuna persona e nessuna chiave API finché
  non può farlo: la richiesta viene rifiutata con un 503 che dice di riprovare. `latet server policy remove` ripara
  direttive che non si possono leggere; anche impostarle di nuovo con una versione che usano tutte le istanze le
  ripara.
- **Il database è considerato attendibile:** chi può scrivere nel PostgreSQL del server può cancellare la riga `ee_policy`, o
  rimetterne una più vecchia. Il livello minimo allora scende, o torna alle direttive più vecchie, su ogni istanza, e
  nessuna voce del registro amministrativo lo dice. Le voci `policy.changed` del registro, ognuna con l'etag delle
  direttive impostate, sono il riferimento con cui confrontare le direttive in vigore (`latet server policy show`);
  lascia l'accesso in scrittura a quel database al solo server.
- **Il testo nelle immagini:** direttive che bloccano `terms` ma non `ocr` e `images` lasciano una persona libera di
  disattivare la lettura del testo nelle immagini, e un termine dell'azienda dentro un'immagine allora non viene letto.
  Blocca anche `ocr: auto` e `images` perché i termini coprano le immagini.
- **Un agente puntato direttamente verso un provider di AI** non viene mascherato da nessuno. Un server è il punto di controllo
  dell'azienda solo quando la sua rete permette di raggiungere i provider soltanto dal server
  ([Latet su un server](../../concepts/server/)).
