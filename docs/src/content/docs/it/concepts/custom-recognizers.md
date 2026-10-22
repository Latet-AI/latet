---
title: Riconoscitori personalizzati
description: Insegna a Latet un tuo formato, come un numero di matricola o un codice di contratto.
---

Latet conosce i segreti, i dati personali e i numeri identificativi nazionali, ma non i formati della
tua azienda: numeri di badge, codici di contratto, id dei clienti. Un **riconoscitore** gliene insegna
uno. È un pattern, facoltativamente con cifre di controllo e parole chiave, e qualche esempio
inventato su cui viene verificato.

Usa invece un [termine](../detection/#i-tuoi-termini) quando c'è un unico valore esatto da
nascondere, come il nome di un cliente. Un riconoscitore serve per ogni valore scritto in un certo
modo.

## Non incollare mai valori reali

Un riconoscitore descrive un formato. I suoi esempi sono inventati: scrivili a partire dalla
descrizione, oppure prendi un valore reale e cambia ogni lettera e ogni cifra, mantenendo la
disposizione. `latet rules add` rifiuta un esempio che Latet ha già visto come valore reale. Quando
è un agente AI a scrivere il riconoscitore per te, descrivigli il formato a parole: due lettere
maiuscole, un trattino, sei cifre, nella colonna badge dell'export delle risorse umane.

## Trovare i formati

Non sai bene quali formati contengono i tuoi file? Lascia che sia Latet a cercarli, su questo
computer:

```sh
latet rules suggest ./my-project
```

Elenca i formati dei valori simili a codici che nessuna regola copre ancora, come `AA-999999` (A per
una lettera, 9 per una cifra), con quanti valori diversi ha visto, in quali file, le cifre di
controllo che superano e le etichette accanto a loro (*badge*, *invoice_ref*). Non stampa mai un
valore. In un repository git legge solo i file che git conosce, quindi i file ignorati restano fuori.
`--yaml` abbozza un riconoscitore per ogni formato, con esempi inventati a partire dal formato;
descrivi cosa trova ciascuno prima di aggiungerlo. `--show-values` mostra tre valori di ogni formato
nel tuo terminale, e si rifiuta di funzionare dentro un agente AI o in una pipe.

## Un esempio

```yaml
recognizers:
  - name: employee-id
    description: Badge numbers of the HR system
    pattern: '\b[A-Z]{2}-\d{6}\b'
    context:
      words: [badge, employee]
    examples:
      match: ["badge AB-123456", "employee: QX-904417"]
      no_match: ["AB-123456 alone", "build AB-12345"]
```

Mettilo in un file a parte, come `rules.yaml`, poi:

```sh
latet rules test rules.yaml     # every example, with the reason when one fails
latet rules add rules.yaml      # saves it in config.yaml and applies it
latet rules list
```

Da quel momento `AB-123456` accanto alla parola *badge* viene mascherato come `EMPLOYEE_ID`, e la
dashboard chiama la regola `recognizer:employee-id`. Il valore fittizio mantiene il formato: due
lettere, un trattino, sei cifre.

La pagina **Regole** della dashboard fa lo stesso nella sua scheda *Riconoscitori*: una tabella dei
tuoi riconoscitori, ogni riga con il nome, il tipo, cosa trova (il pattern, accorciato), cosa
succede ai suoi valori e come si è caricato. *Aggiungi riconoscitore* apre un pannello con ogni
campo: compilalo, e *Provalo* su un tuo testo (niente di ciò che scrivi lì viene conservato). Prima
che tu salvi, mostra il risultato di ogni esempio e le corrispondenze nel codice comune. Seleziona
delle righe per attivarle o disattivarle, o per eliminarle; *Esporta* e *Importa* le spostano come
file di regole. Con il [revisore locale](../llm-review/#riconoscitori-da-una-descrizione) attivo,
*Bozza da una descrizione* scrive il riconoscitore per te a partire da una frase
(*"numeri di badge: BD, un trattino e sei cifre"*), con esempi inventati, e lo apre nel pannello
quando li supera, perché tu lo controlli e lo salvi.

## Per un solo progetto

Un riconoscitore può appartenere a un [progetto](../projects/) invece che a tutto Latet:

```sh
latet rules add rules.yaml --repo .          # into the project's latet.yaml, shared with the team
latet rules add rules.yaml --project .       # into your own settings for the project
latet rules list --project .
```

La pagina **Regole** della dashboard fa lo stesso una volta che scegli il progetto nel selettore in
alto: la sua tabella *Riconoscitori* mostra quelli del progetto, dalle tue impostazioni e da
`latet.yaml`, insieme a quelli globali, e uno nuovo va dove dice il *Salva in* del suo pannello.

## Cosa contiene un riconoscitore

| Chiave | Cosa fa |
| --- | --- |
| `name` | Lettere minuscole, cifre e trattini, unico. |
| `type` | Il tipo mostrato per i suoi valori, in maiuscolo. Se non è impostato, il nome in maiuscolo. Non può essere un tipo che Latet ha già. |
| `pattern` | Un'espressione regolare [RE2](https://github.com/google/re2/wiki/Syntax). Un gruppo chiamato `value` sceglie la parte da mascherare. |
| `checksum` | Cifre di controllo che il valore deve superare: `luhn`, `iban`, `mod97-10`, `mod11-10`, `ean`, `verhoeff`, `damm`, o una somma pesata. Le superano anche i valori fittizi. |
| `context` | Parole chiave (`words`, senza distinguere maiuscole e minuscole; `acronyms`, con maiuscole e minuscole esatte) che devono trovarsi sulla stessa riga, entro `window` caratteri, o nell'intestazione della colonna di una tabella. |
| `examples` | `match` e `no_match`: testi inventati, verificati ogni volta che le regole vengono caricate. |
| `class` | `custom`, o `secret` per i valori che non devono mai uscire in chiaro: vengono mascherati come al solito, e una richiesta che ne porterebbe uno in chiaro viene fermata, anche in Claude Code. |
| `policy` | `mask` (il predefinito), `redact` o `allow`. Una voce in `policies` ha la precedenza. |
| `surrogate` | `shape`, o `{keep: 3}` per mantenere un prefisso così com'è. |
| `enabled` | `false` mantiene la regola nel file, disattivata. |

Il [riferimento della configurazione](../../reference/configuration/) elenca ogni chiave, e lo
[JSON Schema](https://docs.latet.ai/config.schema.json) permette al tuo editor di completarle.

## I pattern sono RE2

RE2 viene eseguito in tempo lineare, quindi nessun pattern può bloccare una richiesta. Non ha
lookaround (`(?=`, `(?<=`) né backreference (`\1`). Dove useresti un lookbehind, includi il contesto
nel pattern e segna il valore con un gruppo con nome:

```yaml
recognizers:
  - name: contract-code
    pattern: '(?i)\bcontract[ #:]+(?P<value>[A-Z]{3}\d{5})\b'
    examples:
      match: ["Contract #KLM48213"]
      no_match: ["contract signed"]
```

Viene mascherato solo `KLM48213`. In YAML scrivi i pattern tra apici singoli, così le barre rovesciate
restano come sono.

Un valore deve contenere una lettera o una cifra. Una corrispondenza fatta solo di simboli, come il
`---` di una tabella Markdown, resta com'è, perché il suo valore fittizio coinciderebbe con
l'originale; `latet rules test` e *Provalo* lo dicono.

## Regole precise

Una regola che scatta dappertutto nel codice comune maschererebbe il codice stesso, quindi Latet
verifica ognuna su un campione della libreria standard di Python: più di 50 corrispondenze e viene
disattivata. Le parole chiave e le cifre di controllo sono il rimedio. Per esempio, un numero la cui
ultima cifra è un controllo Luhn:

```yaml
recognizers:
  - name: card-ref
    type: CARD_REF
    pattern: '\bCR(?P<value>\d{8})\b'
    checksum: luhn
    class: secret
    examples:
      match: ["ref CR12345674"]
      no_match: ["ref CR12345678"]
```

`latet rules test rules.yaml --corpus src/` dice anche quanti valori la regola troverebbe nei tuoi
file. Ne stampa solo i formati (`AA-999999 x12`), mai i valori.

## Quando qualcosa non va

Un riconoscitore con un errore (un pattern che RE2 rifiuta, un checksum sconosciuto, una chiave
scritta male) viene disattivato; gli altri continuano a funzionare, e il file lo conserva come l'hai
scritto. `latet reload`, `latet doctor` e la pagina **Regole** della dashboard dicono quale e perché,
e `latet rules test` spiega ogni esempio. `latet config validate` controlla l'intero file.

Scrivi i riconoscitori con un agente AI? Indicagli
[`llms-rules.md`](https://github.com/Latet-AI/latet/blob/main/llms-rules.md) nel repository: gli
stessi passaggi, scritti per lui.
