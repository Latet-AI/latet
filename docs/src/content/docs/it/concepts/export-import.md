---
title: Esportazione e importazione
description: Le tue regole in un file per un collega, e tutta la tua installazione in un file cifrato per un altro computer.
---

Possono viaggiare due cose diverse:

- **Le tue regole, per un collega:** termini, riconoscitori, criteri e il resto, in un file YAML.
  Una volta importate, si aggiungono a quelle del collega.
- **La tua installazione, per un altro computer:** le tue impostazioni e, se lo chiedi, la cassaforte
  con la sua chiave, in un unico file cifrato.

## Le tue regole, per un collega

```sh
latet rules export acme-rules.yaml     # here
latet rules import acme-rules.yaml     # on your colleague's machine
```

Oppure **Esporta le regole** e **Importa le regole** in cima a **Regole** nella dashboard:
l'importazione mostra cosa farebbe prima che cambi qualcosa. Le tabelle di *Termini*,
*Riconoscitori* e *Valori consentiti* hanno un *Esporta* e un *Importa* tutti loro, solo per
quell'elenco: i termini e i valori in CSV, i riconoscitori come file di regole. Un'esportazione così
ha le stesse cautele: un riconoscitore i cui esempi contengono un valore che Latet ha visto davvero
resta fuori, e una cella CSV che un foglio di calcolo eseguirebbe come formula è scritta dopo un
apice. Un'importazione così passa dalla stessa anteprima, aggiunge e non sostituisce mai, e
trattiene allo stesso modo ciò che abbassa la protezione; la tabella dei *Riconoscitori* accetta un
file di soli riconoscitori.

**Cosa contiene il file:**
- i termini, i riconoscitori, i criteri e le regole di campo;
- i tipi di dati personali, e i paesi di cui vengono riconosciuti i numeri identificativi e di
  telefono;
- il profilo, gli indirizzi IP privati, il rilevamento locale dei nomi, la modalità dati rigorosa con
  i file e gli indirizzi web che copre, la protezione dai dump, le immagini e il riconoscimento del
  testo;
- le eccezioni: valori e pattern da non mascherare mai, regole per i segreti disattivate, gli
  strumenti coperti dalla modalità dati rigorosa;
- le [indicazioni](../llm-review/#le-tue-indicazioni) per il revisore locale.

Mai l'identità, la porta, le cartelle dei progetti o i server di questo computer.

Un file di regole contiene al massimo 2 MB. Oltre 5.000 termini, 500 riconoscitori, 500 regole di
campo, 5.000 valori o 500 pattern da non mascherare mai, vengono letti solo i primi, e l'importazione
lo dice. Un file fatto da un Latet più recente viene rifiutato.

**I termini sono valori in chiaro.** Il nome di un cliente tra i tuoi termini è nel file così com'è:
invia il file come invieresti i nomi stessi. Un riconoscitore i cui esempi contengono valori che
Latet ha visto davvero viene escluso, perché gli esempi devono essere inventati.

**Un'importazione aggiunge protezione, e questa si applica subito:**
- i nuovi termini, riconoscitori e regole di campo vengono aggiunti, e i tuoi restano;
- un valore più rigoroso del tuo viene adottato: un profilo più alto, `redact` per un tipo che
  mascheri;
- un riconoscitore con il nome o il tipo di uno dei tuoi, o una regola per un campo per cui hai una
  regola, viene escluso: viene mantenuto il tuo.

**Ciò che ridurrebbe la protezione viene elencato ed escluso:** le eccezioni, un valore più debole,
le indicazioni del revisore, un riconoscitore che lascia passare ciò che trova (`policy: allow`), un
termine o una regola di campo di un tipo che i tuoi criteri lasciano passare (lascerebbe in chiaro
ciò che trova), un campo lasciato in chiaro. `--all`, o *Applica anche queste* nella dashboard,
applica anche quelle. Se a eseguire l'importazione è un agente o uno script, `--all` fa dell'intera
importazione una sola modifica, che attende la tua approvazione come qualsiasi
[modifica che riduce la protezione](../agents-and-settings/): ciò che aggiunge protezione attende
con lei, e nulla dell'importazione si applica finché non la approvi. `--dry-run` dice soltanto cosa
farebbe un'importazione.

```text
$ latet rules import acme-rules.yaml
Would add:
  terms: Fonderia Brambilla, Banca Ligure
  recognizer badge-number
  policy for IBAN: redact (yours: mask)
Yours already, or kept as yours:
  recognizer ticket: yours, with the same name, is kept
Left out, it would lower protection:
  values never to mask: acme-dev
--all applies them too: they wait for your approval.
saved C:\Users\you\AppData\Roaming\latet\config.yaml
Rules reloaded.
```

**Ciò che si è applicato è la risposta del daemon.** Il comando dice prima cosa aggiungerebbe, poi
salva `config.yaml`, chiede al daemon di ricaricarlo e dice com'è andata: *Rules reloaded.* (codice
di uscita `0`), una modifica che attende la tua approvazione (`3`) oppure, se il daemon non ha
risposto, che il file è salvato e che non è confermato che si applichi (`1`: `latet reload` lo
applica quando il daemon è in esecuzione). Il comando non legge i repository con un `latet.yaml`
proprio che Latet ha visto: il daemon vi pesa l'importazione quando ricarica, e un'aggiunta che
riduce la protezione in uno di essi attende anch'essa la tua approvazione.

Si possono importare anche un `config.yaml` o il `latet.yaml` di un progetto: ciò che contengono e
che non è una regola (la porta, l'identità...) viene escluso, e l'importazione lo dice.

### Le regole di un progetto

Le regole condivise di un progetto viaggiano già con il progetto: il suo
[`latet.yaml`](../projects/), che la dashboard scrive quando le modifichi, è nel repository, e chi lo
clona le ha.

Le tue impostazioni per un progetto, come i nomi dei clienti che tieni fuori dal repository, o le sue
eccezioni, formano un file a parte:

```sh
latet rules export shop-rules.yaml --project C:\dev\shop
latet rules import shop-rules.yaml --project D:\work\shop
```

Nella dashboard, li ha anche la pagina **Regole** di un progetto, sotto *Le tue, per il progetto*:
*Esporta queste impostazioni* e *Importa impostazioni*.

### Su un server Latet

Su un computer connesso a un [server Latet](../server/), le tue regole sono nel tuo spazio sul
server, e `latet rules export` e `import` si rifiutano di funzionare. **Esporta le regole** e
**Importa le regole** in cima a **Regole** nella tua dashboard sul server funzionano come sopra,
sulle tue regole: un team condivide le sue regole come file, e ciascuno lo importa. Il `latet.yaml`
di un progetto lì non si applica: importalo allo stesso modo per usarne le regole.

Le regole di una chiave API le esporta e le importa un amministratore:

```sh
latet server --server https://latet.acme.lan rules --key "Billing CI" export billing-rules.yaml
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

Le regole impostate per tutti su un server, che nessuno può allentare, sono previste per la
Enterprise Edition.

## La tua installazione, per un altro computer

```sh
latet backup latet.backup --vault    # here
latet restore latet.backup           # on the other machine
```

`latet backup` chiede due volte una passphrase (almeno 12 caratteri: qualche parola è facile da
ricordare e difficile da indovinare), e scrive un unico file:

| | Senza `--vault` | Con `--vault` |
| --- | --- | --- |
| Le tue impostazioni (`config.yaml`) | sì | sì |
| I certificati che il revisore locale considera attendibili | sì | sì |
| La cassaforte e la sua chiave radice | no | sì |
| La cronologia della dashboard | no | con `--history` |

- **Con la cassaforte**, l'altro computer produce gli stessi valori fittizi di questo, ricorda ogni
  valore trovato finora (dai rilevatori, dal revisore locale, da te nel Revisore Live), e continua a
  ripristinare le conversazioni iniziate qui. Il file contiene allora i tuoi valori reali, cifrati:
  conservalo dove conserveresti i valori stessi.
- **Senza**, l'altro computer produce valori fittizi suoi.
- **Mai nel file:** la chiave API del revisore locale, il token del bridge (tra il plugin di
  Claude Code e il daemon), i log, il modello locale dei nomi, i modelli di riconoscimento del testo
  e le impostazioni dei tuoi agenti. Ogni computer ha i suoi.

`latet restore` prima legge e controlla l'intero file, mostra cosa contiene e cosa nelle sue
impostazioni riduce la protezione rispetto alle tue, e ti chiede se procedere. Poi:

- **Questo computer mantiene ciò che è suo:** la sua porta, la sua identità (aggiunta a quella del
  backup, così vengono mascherate entrambe), e niente di `capture_dir` o `api_key_file`, che sono
  percorsi dell'altro computer.
- **Cartelle dei progetti in un altro posto:** `--paths C:\Users\anna\dev=D:\work` sposta sotto la
  seconda cartella quelle che si trovano sotto la prima. Le cartelle che qui non ci sono vengono
  elencate.
- **Una cassaforte già presente qui:** se la cassaforte di questo computer ha valori suoi, viene
  sostituita solo con `--replace`. Prima vengono salvati in `before-restore-<date>.latet` accanto
  alla cassaforte, con la stessa passphrase, così `latet restore` può riportarli indietro.
- **Il daemon** viene fermato prima e riavviato dopo, e niente lo avvia mentre è in corso un
  ripristino. Un ripristino interrotto (un crash, un'interruzione di corrente) lo lascia fermo
  finché `latet restore
  --abort` non rimette le cose a posto.
- **Ciò che aveva questo computer** (la cassaforte, la cronologia, `config.yaml`) resta in una
  cartella `before-restore-` datata accanto alla cassaforte.

Poi: `latet llm key` per la chiave API del revisore, `latet ner install` per il rilevamento locale
dei nomi, `latet ocr install` per il testo nelle immagini (fino ad allora le immagini vengono inviate
così come sono, con la segnalazione *Non ispezionato*, o non vengono inviate affatto con
`images: mask`), e `latet global-install` (o `latet claude`) per i tuoi agenti.

**Solo nel tuo terminale.** Entrambi i comandi si rifiutano di funzionare dentro un agente: gestiscono
la tua cassaforte e la sua chiave. `latet restore` ha anche bisogno di un terminale per farti le
domande, e si rifiuta quando la chiave radice viene da `LATET_ROOT_KEY` o `LATET_ROOT_KEY_FILE`, che
una chiave ripristinata non potrebbe sostituire.

Senza la passphrase nessuno può leggere il file, nemmeno tu. Un file modificato, troncato o messo
insieme con pezzi di altri viene rifiutato prima che venga usata qualsiasi sua parte: vedi
[Sicurezza](../../security/#come-latet-protegge-se-stesso).

### In Docker

La cassaforte e la sua chiave sono nel volume `latet-data`, le impostazioni in `latet-config`:
copiando entrambi i volumi si sposta tutto. Con i comandi:

```sh
docker exec -it latet latet backup /data/latet.backup --vault
docker cp latet:/data/latet.backup .
```

Sull'altro computer, con il suo container fermo:

```sh
docker compose stop
docker cp latet.backup latet:/data/
docker compose run --rm -it latet latet restore /data/latet.backup
docker compose start
```

Se il container prende la sua chiave da `LATET_ROOT_KEY_FILE`, `latet restore` si rifiuta: sposta
invece i volumi `latet-data` e `latet-config` e il file della chiave.
