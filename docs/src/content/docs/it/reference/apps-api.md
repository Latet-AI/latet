---
title: Riferimento dell'API per le applicazioni
description: Il contratto HTTP dell'API per le applicazioni di un server Latet, le sue risposte, i suoi errori e i suoi limiti.
sidebar:
  label: API per le applicazioni
---

L'[API per le applicazioni](../../applications/) di un server Latet risponde a quattro operazioni sotto
`https://<il tuo server>/_latet/apps/v1/`. Questa è la versione 1: una modifica che romperebbe un
programma scritto per essa arriva come `v2`, accanto. La [descrizione OpenAPI](../../../apps-api.openapi.json)
dice le stesse cose per gli strumenti che generano un client; i programmi Python possono usare
`latet-client` ([log](../../applications/logs/), [RAG](../../applications/rag/)).

## Autenticazione

Ogni chiamata porta il token di una chiave API in un'unica intestazione:

```http
Authorization: Bearer latet_key_...
```

Un token è `latet_key_` seguito da 43 caratteri, e lo crea l'amministratore (`latet server keys add`,
`keys add-token`). Ogni operazione richiede il suo permesso: `scan`, `mask` (che comprende `scan`),
`unmask` o `forget` (che nessun altro comprende, e che il primo token di una chiave non ha). Il server
rifiuta:

- con **401** e `WWW-Authenticate: Bearer`, una chiamata senza intestazione `Authorization`, con due,
  con uno schema diverso da `Bearer`, o con un token che non conosce (revocato, ruotato, di una chiave
  disattivata o rimossa);
- con **403**, il token del computer di una persona, un token senza il permesso dell'operazione, e
  qualsiasi richiesta che porta un'intestazione `Origin` o `Sec-Fetch-Site`, come quelle di un
  browser: l'API è per i programmi, mai per una pagina web.

I token rifiutati dal server contano contro l'indirizzo da cui arrivano, come su ogni percorso del
server: oltre il limite, 429. Chiama il server in HTTPS; altrimenti il token e i testi viaggiano in
chiaro.

## Richieste

| Operazione | Percorso | Permesso | Corpo |
| --- | --- | --- | --- |
| Analisi | `POST /_latet/apps/v1/scan` | `scan` | `{"texts": [...], "kind": "prose"}` |
| Mascheramento | `POST /_latet/apps/v1/mask` | `mask` | `{"texts": [...], "kind": "prose"}` |
| Ripristino | `POST /_latet/apps/v1/unmask` | `unmask` | `{"texts": [...]}` |
| Oblio | `POST /_latet/apps/v1/forget` | `forget` | `{"values": [...]}` |

- `texts` è una lista da 1 a 1.000 stringhe, a cui si risponde nello stesso ordine. Un testo vuoto
  torna com'è, senza risultati e con `restored: 0`.
- `values` è una lista da 1 a 1.000 stringhe, i valori da togliere dalla cassaforte della chiave,
  ognuno cercato esattamente com'è scritto ([dimenticare dei valori](../../applications/forget/)).
- `kind` è `prose` (testo scritto da persone, il predefinito) o `data` (log, record, l'output di un
  programma); vedi [prosa e dati](../../applications/#prosa-e-dati). `null` o un'altra parola vengono
  rifiutati. Il ripristino non prende un kind.
- Il corpo è un oggetto JSON di al massimo `apps_request_mb` del server (8 MiB se le sue impostazioni
  non dicono altro, mai più del suo `max_request_mb`).

Analisi e ripristino con curl, con il token passato sullo standard input come nella
[prima chiamata](../../applications/#una-prima-chiamata), che maschera; le risposte sono più sotto:

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Rimborsa la fattura di mario.rossi@acme.it su IT60X0542811101000000123456, per favore."]}' \
  https://latet.acme.lan/_latet/apps/v1/scan
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Rimborsa la fattura di tommaso.gallo@margie-6e5a.example su IT17F3945471087320356678487, per favore."]}' \
  https://latet.acme.lan/_latet/apps/v1/unmask
```

## Risposte

Ogni risposta di analisi, mascheramento e ripristino è un oggetto JSON i cui `results` contengono una
voce per ogni testo inviato, in ordine; un oblio risponde con due conteggi.

**L'analisi** dice dove sono i valori nel testo inviato, e non conserva nulla:

```json
{"results": [{"findings": [{"type": "EMAIL", "class": "pii", "start": 23, "end": 42},
                           {"type": "IBAN", "class": "pii", "start": 46, "end": 73}],
              "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

**Il mascheramento** dà ogni testo con i valori fittizi della chiave, e dove si trovano nel testo
restituito:

```json
{"results": [{"text": "Rimborsa la fattura di tommaso.gallo@margie-6e5a.example su IT17F3945471087320356678487, per favore.",
              "replacements": [{"type": "EMAIL", "class": "pii", "start": 23, "end": 56},
                               {"type": "IBAN", "class": "pii", "start": 60, "end": 87}],
              "counts": {"EMAIL": 1, "IBAN": 1}},
             {"blocked": "the local reviewer could not read the user text (it took longer than the 30 seconds an application's call waits for it, which llm_review.budget_ms changes), so it was not sent"}],
 "degraded": ["llm-review"]}
```

- Un risultato con `blocked` e senza `text` è un testo a cui Latet non risponde: il revisore locale lo
  ha fermato, la sua revisione è fallita e `llm_review.on_failure` della chiave è `stop`, oppure la
  protezione dai dump lo ha trattenuto. Il motivo, in inglese, non nomina alcun valore.
- `degraded`, presente solo quando qualcosa ha funzionato in modo ridotto durante la chiamata, nomina
  ciò che lo ha fatto: un rilevatore che non ha potuto leggere un testo (come `ner`) o `llm-review`.

**Il ripristino** dà ogni testo con ogni valore fittizio della cassaforte della chiave sostituito dal
suo valore, e quanti valori diversi ha ripristinato:

```json
{"results": [{"text": "Rimborsa la fattura di mario.rossi@acme.it su IT60X0542811101000000123456, per favore.",
              "restored": 2}]}
```

**L'oblio** dice quanti dei valori la cassaforte della chiave conteneva, ora spariti con i loro valori
fittizi, e quanti non conteneva, contando ogni valore una volta sola:

```json
{"forgotten": 1, "unknown": 1}
```

### Posizioni

`start` ed `end` contano i code point Unicode (`end` escluso): in Python, `text[start:end]` è il valore.
JavaScript, Java e C# contano unità UTF-16, in cui un carattere oltre il piano multilingue di base,
come la maggior parte delle emoji, ne occupa due. In `Scrivi a 👩‍💻 mario.rossi@acme.it` l'indirizzo
inizia a 13 in code point e a 15 in unità UTF-16; in JavaScript lo dà
`Array.from(text).slice(start, end).join("")`.

### Tipi e classi

`type` è il tipo del valore: `EMAIL`, `PHONE`, `IBAN`, `CREDIT_CARD`, `IP_ADDRESS` e gli altri dei
[rilevatori](../detectors/), la regola di un segreto, oppure il tipo di uno dei termini, dei
riconoscitori o delle regole di campo della chiave. `class` è la famiglia di ciò che lo ha trovato:
`secret`, `custom` (i termini e i riconoscitori della chiave), `identity`, `pii`, `ner` (il modello dei
nomi), `llm` (il revisore locale) o `struct` (la modalità dati rigorosa).

## Errori

Un errore è `{"error": "<parole>"}`. Le parole, in inglese, sono per le persone: non citano mai un
testo inviato, e i rifiuti del fronte del server (autenticazione, limiti) iniziano con `latet: `.
Decidi in base allo stato.

| Stato | Quando | Che cosa fare |
| --- | --- | --- |
| 400 | Il corpo non è un oggetto JSON con `texts` una lista da 1 a 1.000 stringhe e, per analisi e mascheramento, un `kind` che sia `prose` o `data`; per l'oblio, con `values` una lista da 1 a 1.000 stringhe. | Correggi la chiamata. |
| 401 | Nessun token, due intestazioni `Authorization`, un altro schema, o un token che il server non conosce. | Controlla il token; chiedine uno nuovo. |
| 403 | Al token manca il permesso (`this token lacks the mask permission`), è il token di una persona, o la richiesta l'ha fatta una pagina web. | Usa un token con il permesso. |
| 404 | Un altro percorso sotto `/_latet/apps/v1/`. | |
| 405 | Un metodo diverso da `POST` (`Allow: POST`). | |
| 413 | Il corpo è più grande di `apps_request_mb`. | Invia meno testi per chiamata. |
| 422 | Analisi, mascheramento e ripristino: i testi nominano più di 100.000 valori o valori fittizi della cassaforte, più di quanti una chiamata possa valutarne. Nulla è stato mascherato o ripristinato. | Invia meno testi per chiamata. |
| 429 | Troppi token rifiutati da questo indirizzo, oppure la chiave ha `requests_in_flight` richieste di applicazioni su questa istanza (16 se le impostazioni non dicono altro). | Aspetta `Retry-After`, poi riprova. |
| 500 | Un altro errore; le sue parole non nominano alcun valore. | Riprova; avvisa l'amministratore se continua. |
| 503 | Un archivio del server non ha risposto, lo spazio della chiave non è disponibile ora su questa istanza, oppure il registro amministrativo non ha accettato la voce: per il ripristino non è stato ripristinato nulla, per l'oblio non è stato dimenticato nulla. | Aspetta (`Retry-After` se presente), poi riprova. |
| 507 | Solo per il mascheramento: la cassaforte della chiave contiene il massimo dei valori consentiti. Della chiamata non è tornato nulla, e nulla è uscito in chiaro. | Un amministratore alza il limite con `latet server keys limit`. |

Un mascheramento o un ripristino si possono ripetere senza rischi: un valore che la cassaforte ha
conservato ha di nuovo lo stesso valore fittizio, e un valore che una chiamata fallita non ha
conservato ne riceve di nuovo uno. Anche un oblio si può ripetere: un valore già sparito viene contato
in `unknown`. Una chiamata `mask` fallita può aver conservato alcuni dei suoi
valori nuovi ([la cassaforte di una chiave](../../applications/#la-cassaforte-di-una-chiave) dice
quando).

## Intestazioni

- Ogni risposta dice `Cache-Control: no-store`: nessuna cache conserva un testo o un valore.
- `Retry-After`, in secondi, accompagna ogni 429, e alcuni 503.

## Limiti

| Limite | Valore | Stabilito da |
| --- | --- | --- |
| Testi per chiamata, o valori in un oblio | 1.000 | Latet |
| Corpo di una chiamata | 8 MiB | `apps_request_mb` del server |
| Richieste di applicazioni contemporanee di una chiave su un'istanza | 16 | `requests_in_flight` del server |
| Valori o valori fittizi della cassaforte che i testi di una chiamata possono nominare | 100.000 | Latet |
| Valori che la cassaforte di una chiave contiene | 200.000 | `vault_limit` del server, o quello della chiave: `latet server keys limit` |
| Valori della cassaforte di una chiave che un'istanza tiene in memoria | 5.000 | `key_working_values` del server |

## Che cosa registra il server

- **Il registro amministrativo** riceve una voce `apps.unmasked` per ogni chiamata di ripristino,
  prima che parta la risposta: l'id del token, quanti testi e quanti valori sono stati ripristinati
  (`{"token": "t_...", "texts": 3, "restored": 5}`), e l'indirizzo del client. Mai un testo o un
  valore. Analisi e mascheramento non ci finiscono. Leggile con
  `latet server log --action apps.unmasked`. Ogni chiamata di oblio è una voce `apps.forgotten`,
  scritta nella stessa transazione dell'oblio: l'id del token, quanti valori sono stati chiesti,
  dimenticati e non trovati (`{"token": "t_...", "values": 2, "forgotten": 1, "unknown": 1}`), e
  l'indirizzo del client.
- **Le metriche** contano le richieste dell'API come `latet_http_requests_total{kind="apps"}`, per
  stato, e i suoi testi e valori per operazione: `latet_apps_texts_total{operation}` e
  `latet_apps_values_total{operation}` (i valori che l'analisi ha trovato, il mascheramento ha
  sostituito e il ripristino ha rimesso a posto; per l'oblio, i valori chiesti e quelli dimenticati).
  Non hanno un'etichetta per la chiave: chi ha fatto
  che cosa è nel registro amministrativo.
- **Il log del server** ha una riga per ogni chiamata: il metodo, il percorso, lo stato e quanto ci ha
  messo, mai un'intestazione, il corpo o un valore. I rifiuti (401, 403, 404, 405, 413, 429, e quelli
  del server stesso) vengono riassunti una volta al minuto, con quanti erano, invece di una riga
  ciascuno.
