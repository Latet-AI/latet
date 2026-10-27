---
title: Dimenticare dei valori
description: Togliere i valori di una persona dalla cassaforte di una chiave API, quando chiede che i suoi dati spariscano.
sidebar:
  label: Dimenticare dei valori
---

La cassaforte di una chiave API conserva ogni valore che ha mascherato, con il suo valore fittizio,
finché la chiave esiste: un ripristino funziona su una riga di log dell'anno scorso. Quando una
persona chiede che i suoi dati spariscano dai tuoi log o dall'indice della tua applicazione RAG, il
server dimentica i suoi valori. Ogni valore esce dalla cassaforte della chiave, il suo valore fittizio
da quel momento non ripristina più nulla, e il valore riceve un nuovo valore fittizio se un testo lo
contiene di nuovo.

Ci sono due strade: un amministratore esegue `latet server keys forget`, oppure un'applicazione chiama
l'API per le applicazioni con un token che ha il permesso `forget`. Fanno la stessa cosa, ed entrambe
finiscono nel registro amministrativo con dei conteggi, mai con un valore.

## L'amministratore

Metti i valori in un file, uno per riga, in UTF-8:

```text
mario.rossi@acme.it
+39 347 123 4567
Mario Rossi
```

```sh
latet server --server https://latet.acme.lan keys forget "Shop" --file erasure.txt
```

Il comando dice quanti valori ha letto, chiede conferma prima di inviarli, e poi dice quanti la
cassaforte della chiave ne conteneva e quanti no. Senza `--file` ti chiede di scrivere i valori nel
terminale, uno per riga, e una riga vuota li chiude. Non prende mai un valore dalla riga di comando,
dove la shell lo terrebbe nella sua cronologia. Si rifiuta di funzionare dentro un agente di IA, perché
i valori sono dati di una persona, e in uno script vuole `--yes`. Invia i valori in chiamate da 1.000 al
massimo. Ogni chiamata è una voce `key.forgotten` del registro amministrativo, con quanti valori sono
stati chiesti, dimenticati e non trovati nella cassaforte. Cancella il file quando hai finito: contiene
i valori.

Il primo oblio di una chiave dopo un aggiornamento indicizza prima ciò che il revisore locale della
chiave ricordava e ciò che la sua cronologia conservava prima dell'aggiornamento: su una chiave con
molta cronologia richiede minuti. Il comando aspetta fino a cinque minuti per ogni chiamata, e la
chiamata di un'applicazione può durare altrettanto. Se una chiamata smette di aspettare, eseguila di
nuovo: ciò che il server ha dimenticato resta dimenticato, e viene allora contato come non presente
nella cassaforte.

Funziona sia con la chiave attiva sia con la chiave disattivata. La cassaforte di una persona non si
dimentica così: le persone dimenticano i valori nella loro dashboard (Cassaforte).

## Un'applicazione

Dai all'applicazione un token tutto suo con `forget`. Nessun altro permesso lo comprende, e il primo
token della chiave, che può analizzare, mascherare e ripristinare, non può dimenticare:

```sh
latet server --server https://latet.acme.lan keys add-token "Shop" erasure --forget --out erasure.token
```

L'applicazione invia fino a 1.000 valori per chiamata:

```sh
printf 'Authorization: Bearer %s' "$(cat erasure.token)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"values": ["mario.rossi@acme.it", "+39 347 123 4567"]}' \
  https://latet.acme.lan/_latet/apps/v1/forget
```

```json
{"forgotten": 1, "unknown": 1}
```

`forgotten` è quanti valori la cassaforte della chiave conteneva, `unknown` quanti no, contando ogni
valore una volta sola. Ogni chiamata è una voce `apps.forgotten` del registro amministrativo (il token,
quanti valori chiesti, dimenticati e non trovati), scritta nella stessa transazione in cui i valori
vengono dimenticati: se il registro non la accetta, non viene dimenticato nulla e la chiamata risponde
503. Con il client Python, `latet.forget([...])` invia un elenco più lungo in più chiamate e restituisce
`ForgetResult(forgotten, unknown)`. Un token senza il permesso riceve 403.

## Che cosa toglie

- **Ogni valore com'è scritto.** Maiuscole diverse, altri spazi o un'altra forma scritta
  (`mario.rossi%40acme.it` in un URL) sono un altro valore. La cassaforte tiene ogni forma che ha
  incontrato come una voce a sé, quindi elenca tutte le forme che devono sparire.
- **Tutto ciò che nella cassaforte lo nomina:** la sua voce e il suo valore fittizio, sigillati con la
  chiave radice della chiave API, gli hash che l'indice conserva delle loro parole, e le risposte
  ricordate del revisore locale che hanno trovato il valore, che la cassaforte lo contenesse o no. Poi
  ogni istanza lo toglie dalla sua memoria, insieme ai testi che tiene mascherati con il suo valore
  fittizio, appena finiscono le chiamate già in corso lì.
- **Ciò che gli agenti della chiave hanno lasciato in Latet.** Quando il primo token della chiave
  serve degli agenti attraverso il gateway, la voce del valore esce anche dalle loro conversazioni, e
  la cronologia della chiave perde i testi, le risposte del modello e i risultati del revisore locale
  che lo nominavano, con il suo vecchio valore fittizio. Un messaggio precedente che un agente invia di
  nuovo viene mascherato da capo, con il nuovo valore fittizio.
- **Il vecchio valore fittizio non ripristina più nulla, e nessun altro valore lo riceve mai.** Una
  riga di log mascherata prima conserva il vecchio valore fittizio, e il ripristino la lascia com'è.
  Il valore fittizio viene ritirato: la cassaforte ne conserva solo l'hash con chiave, legato a nessuna
  voce, finché esiste la cassaforte, quindi non viene mai estratto di nuovo per un altro valore e una
  vecchia riga non viene mai ripristinata con il valore di qualcun altro. Un valore fittizio ritirato
  non occupa spazio, perché il limite della cassaforte conta i valori, ma resta: lo stato del server e
  `latet_vault_near_limit` pesano i valori fittizi ritirati di una chiave insieme ai suoi valori, così
  un token che maschera e dimentica in un ciclo si vede lì. Dai `forget` solo al processo che gestisce
  le richieste di cancellazione.
- **Il valore riceve un nuovo valore fittizio se ritorna.** I valori fittizi e le voci derivano dal
  valore, quindi dopo un oblio la cassaforte della chiave li genera in una nuova generazione: il
  valore riceve un nuovo valore fittizio e una nuova voce, e il vecchio valore fittizio non torna mai.
  I testi mascherati che hanno lasciato Latet prima non si possono collegare a quelli nuovi attraverso
  i loro valori fittizi.
- **Un indirizzo tiene a parte il dominio dell'azienda.** Un indirizzo con il dominio di un'azienda
  (`mario.rossi@acme.it`) conserva il dominio come un valore a sé, e il dominio del valore fittizio è
  il valore fittizio di quel dominio. Dimenticare l'indirizzo lascia il dominio, quindi la parte del
  dominio del vecchio valore fittizio si ripristina ancora in `acme.it`; dimentica anche `acme.it` se
  deve sparire. Il dominio di un fornitore di posta, come `gmail.com`, non viene mai conservato.

Che cosa resta:

- i conteggi nel registro amministrativo, e gli scambi della cronologia i cui testi sono stati tolti,
  senza quei testi;
- i testi mascherati che hanno già lasciato Latet, nel tuo servizio di log o nel tuo indice, che
  contengono il vecchio valore fittizio e che cancelli lì se devono sparire;
- in Valkey, sigillati, per una settimana dal loro ultimo uso: i testi delle conversazioni degli
  agenti della chiave così come sono partiti e le risposte del modello così come le ha scritte, che
  contengono il vecchio valore fittizio, mai il valore. Un messaggio precedente inviato di nuovo viene
  mascherato da capo; una risposta del modello gli torna così come l'ha scritta;
- per una settimana al massimo dall'ultimo uso di una conversazione, l'id della vecchia voce in quella
  conversazione degli agenti della chiave (un hash con chiave del valore, mai il valore), quando Valkey
  non ha risposto subito dopo l'oblio, o quando un'istanza ha perso il messaggio che lo annuncia
  (Valkey non conserva i messaggi) e ha riscritto l'id da una conversazione che tiene;
- ciò che PostgreSQL stesso conserva delle righe cancellate, sigillato, nei suoi file finché non riusa
  il loro spazio, nel suo write-ahead log e nei backup del database.

Una chiamata in corso mentre avviene l'oblio può ancora usare il vecchio valore fittizio, e ciò che
conserva in quel momento può ancora nominare il valore: una risposta del revisore locale, che un
secondo oblio poco dopo toglie, e uno scambio che la cronologia registra, che resta finché la
cronologia lo conserva (`retention_days`). Le istanze tolgono il valore appena finiscono le chiamate
già in corso lì.

## Chi può sapere che cosa

La risposta dice quanti valori la cassaforte conteneva, quindi un token che può dimenticare sa se un
valore è della chiave, come lo sa un token che può analizzare o mascherare
([chi può sapere che cosa](../#chi-può-sapere-che-cosa)). Dai `forget` solo al processo che gestisce le
richieste di cancellazione, e leggi il registro con `latet server log --action apps.forgotten`.
