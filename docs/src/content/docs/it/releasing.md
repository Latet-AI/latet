---
title: Pubblicare Latet
description: Pubblica una versione solo dopo il successo dell'intero workflow CI sull'esatto commit di main.
---

Questa procedura riguarda i tag `v*` di Latet e `.github/workflows/release.yml`. Mantieni un solo
commit pubblico per versione, con codice, test, numeri di versione e documentazione in tutte e cinque le lingue.

## Integra, attendi la CI, poi crea il tag

1. Prepara e revisiona la versione come un unico commit pubblico. Allinea i numeri di versione del
   pacchetto e del plugin Claude; la build di rilascio li confronta con il tag.
2. Integra quel commit in `main`. Registra il suo SHA completo.
3. Attendi che **l'intero workflow CI** (`.github/workflows/ci.yml`), avviato da quel push su `main`,
   termini con **successo** per quello SHA esatto. Controlla il risultato del workflow, inclusi tutti
   i job della matrice e i test del server. Non basta una pull request o un singolo job riuscito.
4. Crea il tag della versione su quel commit esatto e invialo solo quando la pubblicazione è autorizzata.
   Sono supportati tag leggeri e annotati; per quelli annotati si controlla il commit sottostante.
5. Controlla il risultato del workflow di rilascio. Conserva lo SHA del tag e l'URL della CI riuscita su
   main nel resoconto di verifica. Il riepilogo del controllo registra esecuzione e tentativo accettati.

Se il commit cambia, ripeti il controllo della CI su main per il nuovo SHA prima di creare il tag.
Mantieni il singolo commit pubblico della versione; non aggiungerne uno solo per registrare i controlli riusciti.

## Cosa richiede il controllo di rilascio

Prima di build, attestazioni o pubblicazione, il controllo ricava l'ID effettivo del workflow CI dal suo file,
verifica che il commit del tag appartenga a `main` e legge le esecuzioni `push` su `main` per quello SHA.
Sceglie l'esecuzione più recente e aggiorna il suo tentativo corrente. Passa solo `completed` con `success`.
I job successivi usano lo SHA del commit verificato e dipendono dal controllo.

Risultati assenti, in coda, in corso, in attesa, falliti, annullati, saltati o neutrali bloccano il rilascio.
Lo bloccano anche errori API o di autenticazione, risposte incoerenti e paginazione incompleta.
Il controllo rifiuta elenchi che raggiungono il limite di 1.000 risultati delle ricerche filtrate di GitHub.
Non attende né riprova fino a ottenere un risultato favorevole. Indaga sul controllo fallito, attendi il
successo dell'intera CI su quell'esatto commit di main, poi riesegui esplicitamente il rilascio quando autorizzato.
Non spostare un tag pubblicato per aggirare un errore.

Il risultato è una prova al momento del controllo; una successiva riesecuzione della CI può modificarne lo stato.
Un controllo verde conferma il successo della CI, non l'assenza di ogni possibile difetto.

## Un resoconto di verifica per ogni versione

Collega dalle note di rilascio un breve riepilogo di verifica in inglese, con un file Markdown
identificato per versione e manifest condivisi dei sorgenti e delle prove, allegati fuori da Git.
Completa il resoconto dopo la creazione del commit pubblico e il successo della sua intera CI su main;
dopo il tag e il rilascio autorizzati, allega manualmente i file e aggiungi i link nelle note di rilascio.
Il workflow di rilascio attuale non genera questi file di verifica. Mantieni un solo commit pubblico
per versione: il resoconto esterno può citarne lo SHA esatto senza inserirlo in un file che lo determina.

Includi:

- Versione, SHA completo del commit pubblico e del commit sottostante al tag; separatamente, i sorgenti
  privati realmente testati (commit oppure base più patch/manifest conservati). Un manifest elenca
  percorsi e hash del contenuto di codice, test, harness e configurazione verificati. Registra tutte
  le differenze dai sorgenti pubblici, le esclusioni e gli eventuali nuovi controlli; un risultato su
  sorgenti privati non diventa un risultato sullo SHA pubblico.
  Separa gli inventari dei blob Git canonici e dei byte realmente eseguiti, sia per checkout puliti
  sia per quelli modificati; registra conversioni EOL/filtri e differenze senza normalizzare gli hash dei byte eseguiti.
- Partecipanti effettivi alle revisioni o ruoli identificabili e riferimenti ai rapporti, ambito,
  tipo di revisione e presenza di una revisione umana indipendente. Quella di un agente automatico
  separato non è una revisione umana. Elenca rilievi chiusi con prove del nuovo controllo e rilievi
  aperti con gravità e lavoro restante.
- Comandi delle suite, ambiente, risultati e test saltati; ID delle configurazioni fuzz, harness e
  sorgenti, seed o corpus, conteggi previsti/effettivi e configurazioni completate/previste. Indica
  esplicitamente attività fallite, incomplete e non eseguite. Le campagne di conteggio non misurano le prestazioni.
- Repository CI, percorso del workflow, evento/ref, SHA esatto, ID dell'esecuzione, URL del tentativo,
  stato/esito dell'intero workflow e momento dell'osservazione. Verifica separatamente la CI finale
  `push`/`main` prima del tag; controlli PR, risultati locali e pubblicazione riuscita non la sostituiscono.
- Limiti verificati: upstream simulato o test diretti dei runtime degli spazi rispetto a test end-to-end
  autenticati, configurazioni omesse e prove non disponibili. I log privati non sono prove consultabili
  dal pubblico; fornisci un estratto pubblico sicuro quando possibile e indica cosa non è verificabile.

Mantieni riepilogo e manifest fuori dal commit che identificano. Assegna checksum e identificatori di
revisione ai file delle prove; conserva le revisioni precedenti e spiega le correzioni. Un link o checksum
da solo non prova una revisione indipendente né un'archiviazione immutabile. Usa dati sintetici ed estratti
sicuri, mai credenziali o traffico reale. Scrivi il resoconto per versione solo in inglese, con manifest
tecnici condivisi; la guida al rilascio e l'altra documentazione pubblica restano nelle cinque lingue.
Una voce nel modello o una casella spuntata non prova nulla. Il resoconto vale solo per i sorgenti e
l'ambito dichiarati; indica esplicitamente campagne incomplete e passaggi di pubblicazione ancora da svolgere.

## Rilasci separati del client

I tag `sdk-python-v*` del client Python usano `.github/workflows/sdk-python-release.yml` e versioni indipendenti.
Quel workflow è fuori da questo controllo; la sua pubblicazione non dimostra il successo della CI su main.
