---
title: Mascherare i log prima che escano
description: Un'applicazione maschera le righe di log con un server Latet prima di spedirle, e uno strumento di assistenza ripristina la riga che gli serve.
sidebar:
  label: Log
---

I log di un'applicazione nominano i suoi clienti: indirizzi email, indirizzi IP, IBAN, ogni tanto un
token. Spediti così come sono, restano per mesi in un servizio di log, letti da chiunque legga i log.
Con l'[API per le applicazioni](../), l'applicazione maschera ogni gruppo di righe con un server Latet
prima di spedirlo, e il servizio di log riceve valori fittizi. Poiché un valore riceve sempre lo
stesso valore fittizio, le righe di un cliente si possono ancora cercare, contare e seguire per giorni;
quando l'assistenza deve sapere di chi si trattava, uno strumento con il token giusto ripristina la
riga.

## I token

Una chiave API per i log i cui clienti vuoi seguire insieme, con un token per ciascun lato:

```sh
latet server --server https://latet.acme.lan keys add "Shop logs" --out shop-logs.key
latet server --server https://latet.acme.lan keys add-token "Shop logs" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop logs" support --unmask --out support.token
```

L'applicazione tiene `shipper.token`, che può mascherare e non può vedere alcun valore reale; lo
strumento di assistenza tiene `support.token`, che può ripristinare e nient'altro. Il primo token,
`shop-logs.key`, resta all'amministratore: ha tutti i permessi. Le righe mascherate con un'altra
chiave ricevono altri valori fittizi, quindi usa una sola chiave per i servizi i cui log cerchi
insieme.

## Mascherare le righe

Con il client Python, `latet-client` (`pip install latet-client`, Python 3.10 o successivo):

```python
from pathlib import Path

from latet_client import Latet, LatetError

latet = Latet("https://latet.acme.lan", Path("shipper.token").read_text().strip())


def ship(lines: list[str]) -> None:
    """Mask a batch of log lines, then send them to the log service."""
    try:
        results = latet.mask(lines, kind="data")
    except LatetError:
        keep_for_later(lines)  # never send the lines as they are
        return
    for result in results:
        if result.text is not None:
            send_to_log_service(result.text)
        else:  # blocked: the reason names no value
            send_to_log_service(f"[line withheld by Latet: {result.blocked}]")
```

Le righe

```text
2026-10-26T09:14:02Z INFO payment accepted customer=mario.rossi@acme.it iban=IT60X0542811101000000123456
2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

escono come:

```text
2026-10-26T09:14:02Z INFO payment accepted customer=tommaso.gallo@margie-6e5a.example iban=IT17F3945471087320356678487
2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41
```

Invia le righe a gruppi, non una chiamata per riga: una chiamata accetta fino a 1.000 righe, e il
client invia una lista più lunga in più chiamate, una dopo l'altra. Le righe di una chiamata vengono
mascherate insieme: un valore che una riga della chiamata insegna alla cassaforte viene mascherato
anche nelle altre righe della chiamata. Un valore che solo una chiamata successiva insegna non viene
mascherato nelle righe delle chiamate precedenti, che hanno già avuto risposta. Usa `kind="data"` per
i log ([prosa e dati](../#prosa-e-dati)).

Quando la chiamata fallisce, tieni le righe e riprova, oppure scartale: non inviarle mai senza
mascherarle. Il client riprova da solo un 429 e un 503 (tre volte, dopo l'attesa che chiede il
server) prima di sollevare l'errore. Mascherare le stesse righe due volte dà gli stessi valori
fittizi, quindi riprovare è sicuro.

Ogni rifiuto del server è un `LatetError`, con il suo stato e le sue parole, e una chiamata che non è
andata a buon fine è un `ConnectionFailed` (stato 0). `ConnectionFailed` prende il posto degli errori
di `httpx`, anche di quelli di un `httpx.Client` che passi come `client=`, e non contiene la
richiesta, così il token non finisce in nessun errore: intercetta gli errori del client, non quelli di
`httpx`. Un indirizzo `http://` di un altro computer dà un `ClearTextWarning`, perché il token e le
righe viaggerebbero in chiaro. Il client giudica solo dall'indirizzo, quindi anche un tuo `client=` che
raggiunge il server in TLS per un'altra strada riceve l'avviso; il modulo `warnings` di Python lo
silenzia.

## Il formato delle righe

Ciò che i rilevatori riconoscono viene mascherato ovunque si trovi in una riga: indirizzi email, IBAN,
numeri di carta, indirizzi IP pubblici, token e chiavi, i termini e i riconoscitori della chiave. Per
il resto, imposta le regole della chiave
([impostazioni condivise e regole delle chiavi API](../../getting-started/server/#configurare-le-impostazioni-condivise-e-le-regole-delle-chiavi-api)):

- Le **regole di campo** mascherano i valori di un campo per nome, come `phone` o `customer_name`, e la
  **modalità dati rigorosa** (`data: strict`) maschera ogni valore di un record
  ([dati di produzione](../../concepts/production-data/)). Trovano un valore dalla sua chiave nel
  JSON, e nelle righe `chiave=valore` dove un valore che contiene spazi è tra virgolette:
  `phone="+39 347 123 4567"`. Un valore senza virgolette finisce al primo spazio. Scrivi le righe di
  log in JSON, oppure metti tra virgolette questi valori.
- I **termini** e i **riconoscitori** colgono i tuoi nomi e i tuoi formati: il nome di un cliente, un
  numero d'ordine scritto `ORD-123456` ([riconoscitori personalizzati](../../concepts/custom-recognizers/)).
- I **nomi** nel testo libero di un messaggio vengono trovati con il modello locale dei nomi
  installato sul server, nei testi `prose`; nei testi `data`, con il profilo `max`.

`scan`, con un token che può analizzare, mostra che cosa maschererebbe un campione delle tue righe,
senza conservare nulla: provalo su righe reali prima di attivare il mascheramento.

## Ripristinare una riga

Una persona dell'assistenza copia una riga mascherata dal servizio di log; il suo strumento la
ripristina:

```python
support = Latet("https://latet.acme.lan", Path("support.token").read_text().strip())
line = "2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41"
print(support.unmask(line).text)
# 2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

Per trovare tutte le righe di un cliente, maschera il suo indirizzo con un token che può mascherare e
cerca il valore fittizio nel servizio di log. Ogni ripristino viene scritto nel registro
amministrativo del server, con il token e quanti valori ha ripristinato;
`latet server log --action apps.unmasked` li elenca.

## Quanto è veloce, e quante istanze

Su un'istanza il mascheramento procede a circa 190 righe al secondo per una chiave nuova e da 410 a
440 per una chiave la cui cassaforte contiene da 200.000 a 1.000.000 di valori; il ripristino da 1.500
a 2.700 ([le misure](../#quanto-è-veloce)). Le chiamate di una chiave vengono eseguite una dopo
l'altra su un'istanza, quindi un servizio che scrive log più in fretta ha bisogno di più istanze
dietro il bilanciatore di carico, oppure di dividere le sue righe tra chiavi i cui clienti non serve
seguire insieme.

Una chiamata tiene le righe in memoria finché non riceve risposta: spedisci da una coda tua, così un
server lento o irraggiungibile ritarda i log invece di perderli o di inviarli senza maschera.

## Senza Python

Qualunque linguaggio può chiamare l'API in HTTPS: un `POST` con un corpo JSON e il token in
un'intestazione `Authorization: Bearer`. Il [riferimento HTTP](../../reference/apps-api/) contiene il
contratto, i suoi errori e una descrizione OpenAPI da cui generare un client.
