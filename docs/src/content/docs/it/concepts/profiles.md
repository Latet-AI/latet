---
title: Profili e criteri
description: Scegli quanto maschera Latet (balanced, max, secrets) e cosa succede a ogni tipo di valore.
---

## Profili

Un profilo decide quali [rilevatori](../detection/) sono attivi. Ce ne sono tre:

| Cosa viene mascherato | `balanced` (predefinito) | `max` | `secrets` |
| --- | --- | --- | --- |
| Segreti: chiavi API, token, password, chiavi private | ✅ | ✅ | ✅ |
| I valori delle colonne e dei campi segreti (`password`, `api_key`, `reset_token`...) in tabelle e JSON | ✅ | ✅ | ✅ |
| I tuoi termini | ✅ | ✅ | ✅ |
| I tuoi [riconoscitori](../custom-recognizers/) | ✅ | ✅ | ✅ |
| Valori già nella cassaforte | ✅ | ✅ | ✅ |
| Dati personali: email, numeri di telefono, IBAN, numeri di carta, partite IVA, indirizzi IP pubblici, indirizzi Bitcoin | ✅ | ✅ | ❌ |
| Numeri identificativi nazionali e numeri fiscali ([per paese](../detection/)) | i paesi in `id_regions` | tutti i paesi | ❌ |
| L'identità di questo computer: il tuo nome utente nei percorsi, il nome host, l'autore git e il dominio della sua azienda | ✅ | ✅ | ❌ |
| Indirizzi IP privati (`10.x`, `192.168.x`...) | ❌ | ✅ | ❌ |
| Ogni valore delle tabelle, del JSON e delle coppie `key=value` che uno strumento stampa ([modalità dati rigorosa](../production-data/#modalità-dati-rigorosa)) | ❌ | ✅ | ❌ |
| Nomi e indirizzi, con il [modello locale dei nomi facoltativo](../../getting-started/installation/) | nei tuoi prompt, allegati e nel testo nelle immagini | ovunque | ❌ |
| Ciò che nessun pattern trova, con il [revisore locale facoltativo](../llm-review/) | quando è attivo | quando è attivo | quando è attivo |
| Il testo nelle immagini, con il [riconoscimento del testo facoltativo](../images/) | mascherato | non inviato | mascherato |
| PDF, e immagini senza riconoscimento del testo | inviati, e segnalati | non inviati | inviati, e segnalati |

- **`balanced`** va bene per la maggior parte del lavoro sul codice. È il profilo predefinito.
- **`max`** aggiunge gli indirizzi IP privati e i numeri identificativi di tutti i paesi, attiva la
  [modalità dati rigorosa](../production-data/#modalità-dati-rigorosa) (viene mascherato ogni valore di una
  tabella che uno strumento stampa, anche un id o uno stato), cerca i nomi ovunque quando il modello
  locale è installato e si rifiuta di inviare una richiesta che contiene un'immagine o un PDF.
  L'errore dice cosa fare: rimuovi l'immagine dalla conversazione (in Claude Code, `/rewind`), oppure
  passa a `balanced`. Un tuo progetto può inviare le immagini con il loro testo mascherato, con
  `images: mask`.
- **`secrets`** maschera solo i segreti e i tuoi termini. I dati personali e la tua identità passano.
  Sceglilo quando i falsi allarmi sui dati personali ti intralciano.

### Scegliere un profilo

- **Quando fai passare i tuoi agenti da Latet in modo permanente:** `latet global-install --profile max`.
- **Nella dashboard:** la pagina **Regole**. La modifica si applica subito alle nuove richieste, senza
  riavvio.
- **In `config.yaml`:** imposta `profile: max`, poi esegui `latet reload` per applicarlo senza
  riavvio.

### Regolazione fine

Oltre al profilo, la pagina **Regole** della dashboard e `config.yaml` ti permettono di scegliere:

- `pii`: quali tipi di dati personali rilevare, per esempio `[EMAIL, IBAN]`. L'elenco sostituisce la
  scelta del profilo: un tipo che lascia fuori non viene rilevato, compresi i numeri identificativi
  di `id_regions` (e di ogni paese in `max`). Se manca, viene rilevato ogni tipo che il profilo
  attiva. Nelle tue impostazioni per un progetto, `pii` aggiunge invece dei tipi a quelli globali.
- `phone_regions`: i paesi i cui numeri di telefono vengono riconosciuti senza prefisso internazionale
  (predefiniti `IT`, `US`, `GB`).
- `include_private_ips: true`: maschera gli indirizzi IP privati anche in `balanced`.
- `data`: `strict` attiva la modalità dati rigorosa anche in `balanced` e `secrets`, `normal` la
  disattiva in `max`. Se manca, decide il profilo.
- `disabled_secret_rules`: le regole di gitleaks da disattivare, come `generic-api-key`.
- `identity`: disattiva il mascheramento dell'identità di questo computer, o scrivi tu i suoi valori.

Ogni chiave è elencata nel [riferimento della configurazione](../../reference/configuration/).

## Criteri

Un criterio decide cosa succede a un tipo di valore una volta rilevato. Il criterio predefinito è
`mask`:

| Criterio | Cosa vede il modello | Cosa ricevi indietro |
| --- | --- | --- |
| `mask` (predefinito) | Un valore fittizio: `+44 20 3318 4452` | L'originale: `+44 20 7946 0958` |
| `redact` | Un marcatore fisso: `[REDACTED:PHONE]` | Il marcatore. Non viene ripristinato nulla, quindi anche un file che l'agente scrive contiene `[REDACTED:PHONE]` |
| `allow` | L'originale, invariato | L'originale |

Imposta un criterio per tipo in `config.yaml`, o nella pagina **Regole** della dashboard:

```yaml
policies:
  PHONE: redact
  IP_ADDRESS: allow
```

I nomi dei tipi sono quelli che la dashboard mostra accanto a ogni valore mascherato, come `EMAIL`,
`PHONE`, `IBAN`, `PASSWORD`, o il tipo che hai dato a un termine personalizzato. I tipi senza un
criterio vengono mascherati.

`allow` lascia in chiaro il valore di quel tipo, e nient'altro: un valore di un tipo mascherato
scritto al suo interno non passa con lui. Con `EMAIL: allow`, `IT60X0542811101000000123456.ops@acme-intra.it`
arriva al modello con l'IBAN sostituito da uno fittizio e `.ops@acme-intra.it` com'è scritto; un
segreto o un tuo termine scritto in un indirizzo viene mascherato con l'indirizzo intero, come
prima. Un termine o una regola di campo di un tipo lasciato passare lascia in chiaro ciò che trova
solo lui; ciò che vi trova un rilevatore di un tipo mascherato viene mascherato.

`redact` va bene per i valori di cui il modello non ha mai bisogno, nemmeno in forma fittizia. Funziona
in un solo senso: il modello riceve il marcatore, e Latet non rimette mai l'originale nelle risposte o
nei file. Che Latet conservi l'originale o no dipende da come è stato trovato il valore:

- Un valore trovato dai rilevatori, compresi i tuoi termini e i tuoi riconoscitori, non viene
  conservato.
- Un valore trovato dal [revisore locale](../llm-review/), o da te nel
  [Revisore Live](../live-review/), viene conservato comunque: sigillato nella cassaforte, con un
  valore fittizio tutto suo che non viene inviato. Così Latet lo ritrova ovunque compaia, e lo oscura
  anche lì.

Un valore più lungo di 16.384 caratteri, come un'intera stringa JSON in
[modalità dati rigorosa](../production-data/#modalità-dati-rigorosa), viene oscurato qualunque sia il
criterio del suo tipo, a meno che quel tipo sia `allow`: è troppo lungo perché la cassaforte lo
conservi, quindi non viene mai rimesso al suo posto, e un file che l'agente scrive a partire da esso
contiene il marcatore.

## Valori consentiti

Quando Latet maschera qualcosa che non è sensibile (un falso positivo), digli di lasciare stare quel
valore:

```yaml
allow_values:
  - demo@northwind-legal.com # exact value, any letter case
allow_patterns:
  - "test_[0-9a-f]{16}"      # RE2 regular expression, matched against the whole value
```

Nella dashboard puoi farlo con un clic: apri una richiesta, fai clic sul valore mascherato e scegli
**Non sensibile: consenti sempre**. L'azione opposta, **Maschera sempre**, aggiunge il valore come
termine personalizzato. In una richiesta di un [progetto](../projects/), scegli prima se vale per
*Questo progetto* o *Ovunque*.

Ogni clic di questo tipo aggiunge un valore, quindi l'elenco cresce. Nella pagina **Regole** della
dashboard, la scheda *Valori consentiti* lo mostra come tabella, valori e pattern insieme con il loro
tipo: puoi cercarlo, filtrarlo, aprire una riga per cambiarla (il pannello di un pattern lo prova su
un valore), oppure selezionare righe ed eliminarle. *Esporta* e *Importa* spostano l'elenco in CSV
(`kind,value`); un'importazione aggiunge ciò che è nuovo e, siccome lascia passare dei valori, lo
applica solo quando spunti *Applica anche queste*.

Il nome di Latet resta sempre in chiaro: è lo strumento di cui parla l'agente, mai un tuo valore.

Per provare le tue regole su un testo senza inviare nulla, incollalo nel **Laboratorio** della
dashboard.

## In modalità client

In [modalità client](../how-it-works/) (Claude Code), il mascheramento avviene nel plugin, con lo
stesso profilo, gli stessi criteri e gli stessi valori consentiti. Il daemon non riscrive mai una
richiesta: può solo lasciarla passare o bloccarla. Quando ne blocca una perché un valore uscirebbe in
chiaro, apri la richiesta nella dashboard: quel valore è riquadrato in rosso. Se non è sensibile, fai
clic su di esso e scegli **Non sensibile: consenti sempre**, come sopra, poi riprova.
