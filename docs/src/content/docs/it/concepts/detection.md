---
title: Rilevamento
description: Come Latet riconosce segreti e dati personali, interamente sul tuo computer.
---

Il rilevamento avviene sul tuo computer, o sul server Latet a cui è connesso: non viene mai chiamato
nessun servizio di rilevamento nel cloud. Diversi rilevatori leggono ogni porzione di testo che una richiesta contiene.
Questa pagina li spiega con degli esempi; il [riferimento dei rilevatori](../../reference/detectors/)
elenca ogni rilevatore e ogni regola per i segreti.

## Segreti

I segreti vengono mascherati in ogni [profilo](../profiles/).

Sono incluse le credenziali di Latet: `LATET_PERSONAL_TOKEN` (`latet_pat_`), `LATET_COMPUTER_KEY`
(`latet_ck_`), `LATET_API_KEY` (`latet_key_`), `LATET_ADMIN_TOKEN` (`latet_adm_`),
`LATET_SCIM_TOKEN` (`latet_scim_`, il token SCIM di un server Enterprise), `LATET_BRIDGE_TOKEN`
(`latet_brg_`, il token che il plugin di Claude Code dà al daemon del tuo computer) e
`LATET_ONE_TIME_CODE` (`latet_code_`, il codice che un amministratore dà a una persona per collegare
un computer), ciascun prefisso seguito da 43 caratteri URL-safe (32 per il codice monouso; `A-Z`,
`a-z`, `0-9`, `_`, `-`). Un token del bridge o un codice monouso creato da una versione precedente
non ha prefisso: viene mascherato dal nome con cui è scritto, come ogni segreto. In `https://<host>/u/<route-key>`, i 16-128 caratteri
URL-safe della chiave di percorso vengono mascherati come `LATET_ROUTE_KEY`; l'host e gli altri
segmenti del percorso restano. Vale sul tuo computer e in uno spazio sul server, anche nei dump
dell'ambiente, nel file `settings.json` di Claude Code e nel file `config.toml` di Codex. Le chiamate
agli strumenti locali ricevono i valori reali. Una chiamata a uno strumento esterno che contiene un
valore fittizio viene fermata dove Latet può intercettarla (vedi [Ambito della protezione](../protection-scope/)).

- **Formati noti.** Il set di regole predefinito di [gitleaks](https://github.com/gitleaks/gitleaks)
  (222 regole) è incluso in Latet e gira su [RE2](https://github.com/google/re2), con le parole
  chiave, le soglie di entropia e gli elenchi di valori consentiti che usa gitleaks. Copre le chiavi
  API e i token di molti provider, come GitHub, Anthropic, OpenAI, AWS, Stripe o Slack.
- **Valori assegnati a nomi sensibili**, nei file `.env`, nella configurazione e nel codice. Decide il
  nome, qualunque aspetto abbia il valore: `DB_PASSWORD=...`, `"api_key": "..."` e
  `$env:TOKEN = ...` vengono mascherati; `APP_NAME=shop` e `DEBUG=true` no. I segnaposto come
  `changeme`, `${DB_PASSWORD}` o `<your-key>` restano come sono, e così anche i riferimenti nel codice
  come `process.env.API_KEY`. Una coppia `password=...` viene mascherata ovunque stia in una riga, dopo
  altre parole in un log (`INFO login ok password=...`), tra parentesi, nella query di un URL o dopo `--`
  in un comando (`--password=...`); dopo un nome forte (`password`, `secret`, `api_key`,
  `access_token`...) viene mascherato anche un valore letterale tra virgolette dopo due punti o un
  segno di uguale con spazi (`{ password: 's3cr3t' }`, `SET password = '...'`). Un confronto
  (`token==expected`) non è una coppia. Due tipi di valore restano come sono dopo il nome di un segreto,
  perché dicono cosa ha trovato un controllo e non sono il segreto. Una parola di stato, in minuscolo,
  sotto qualunque nome (`auth=failed`, `token=expired`): `ok`, `fail`, `failed`, `failure`, `denied`,
  `refused`, `rejected`, `accepted`, `granted`, `success`, `succeeded`, `error`, `expired`, `missing`,
  `invalid`, `valid`, `required`, `present`, `absent`, `revoked`, `rotated`, `refreshed`, `pending`,
  `unknown`, `anonymous`, `unauthorized`, `forbidden`, `enabled`, `disabled`, `active`, `inactive`,
  `skipped` e `passed`. E, sotto un nome che finisce con `status`, `state`, `result`, `results`,
  `outcome`, `scheme`, `changed`, `reason` o `error` (`auth_status`, `token_error`, `password_changed`),
  un valore con la forma di uno stato: una parola in minuscolo di al massimo 20 caratteri con al massimo
  un `-` o `_`, un numero di al massimo 10 cifre, un'ora, una data o un booleano; qualunque altro valore
  lì (`password_changed=Winter2024x`, una frase segreta come `correct-horse-battery-staple`, un numero
  più lungo) viene mascherato.
  Un nome che finisce con `reset`, `source`, `arg`, `option` o `flag` resta il nome
  di un segreto (`password_reset`, `token_source`), e solo dopo `--` in mezzo a una riga un'opzione che
  finisce così (`--pass-arg=...`) resta com'è, perché prende il nome di qualcosa.
- **Chiavi private**: l'intero corpo di un blocco di chiave privata PEM, OpenSSH o PGP. Le righe
  `BEGIN` ed `END` restano visibili, così il modello sa comunque che lì c'è una chiave.
- **Credenziali**: la password in `postgres://user:password@host/db` e in altri URL, i JSON Web Token
  e le credenziali `Bearer` o `Basic`. In una stringa di connessione (una variabile
  `connectionString`, una colonna `connection_string`) vengono mascherate solo le credenziali: i
  valori di `Password=`, `PWD=`, `AccountKey=` e simili, o la password del suo URL. Il server,
  l'utente e il database restano, come in `Server=db;User Id=app;Password=…;`.

Per esempio, un file `.env` così com'è sul tuo disco:

```sh title=".env"
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=hunter2hunter
PAYMENTS_API_KEY=pk_9f2c4e7a1b3d5f8e0a6c
```

e ciò che riceve il modello quando l'agente lo legge:

```sh
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=jeeepo3ixhbkw
PAYMENTS_API_KEY=pk_3a8d1f6b0e9c2a7d4b5e
```

Il nome host non è un segreto e resta. La password e la chiave vengono sostituite da valori fittizi
della stessa lunghezza e dello stesso formato (i tuoi saranno diversi).

### Segnaposto

Un valore che tiene soltanto il posto di un segreto resta com'è: in un'assegnazione, come password di
un URL, in una colonna segreta e come credenziale di una stringa di connessione
(`User Id={{username}};Password={{password}};`). L'intero valore deve essere uno di questi, senza
distinzione tra maiuscole e minuscole:

| Forma | Esempi |
| --- | --- |
| Tra parentesi angolari o quadre | `<password>`, `<your-key>`, `[password]` |
| Una variabile o un template | `${DB_PASSWORD}`, `$DB_PASSWORD`, `%DB_PASSWORD%`, `{{password}}`, `{env:DB_PASSWORD}`, `{file:./secret}` |
| *Your* e un nome | `YOUR_PASSWORD`, `your-api-key`, `your.token` |
| Una parola che sta per un valore | `changeme`, `change_me`, `password`, `passwd`, `secret`, `token`, `placeholder`, `example`, `redacted`, `todo`, `tbd` |
| Riempitivi | `xxx`, `***`, `...`, `---`, `___` |
| Nessun valore | `null`, `none`, `nil`, `undefined`, `empty`, `true`, `false` |

`changeme123` non è un segnaposto: potrebbe essere una password reale. In un'assegnazione resta com'è
anche un riferimento nel codice: `process.env.API_KEY`, `os.environ["TOKEN"]`, `os.getenv(...)`,
`config.`, `settings.`. Per lasciare in chiaro un segnaposto di un'altra forma, aggiungilo ai valori
da non mascherare mai ([valori consentiti](../profiles/#valori-consentiti)): se appartiene a un progetto,
nelle tue impostazioni per quel progetto.

Resta com'è anche un valore fatto solo di simboli, chiunque lo trovi e per quanto sia lungo:
`!!!!!!!!` come password, `--------------------` dopo `Bearer`, una corrispondenza di uno dei tuoi
[riconoscitori](../custom-recognizers/) o un valore segnalato dal [revisore locale](../llm-review/).
Il valore fittizio conserva i simboli dell'originale, quindi coinciderebbe con l'originale. Fa eccezione
una chiave di percorso di Latet: ogni carattere del valore fittizio viene generato di nuovo, anche
quando l'originale è fatto solo di `_` o `-`.

## Dati personali

I dati personali vengono mascherati nei profili `balanced` e `max`. Ogni corrispondenza viene
convalidata prima di essere mascherata, e questo riduce i falsi allarmi nel codice:

| Tipo | Esempio | Come viene confermata una corrispondenza |
| --- | --- | --- |
| Email | `emily.carter@northwind-legal.com` | Il dominio di primo livello viene confrontato con l'elenco IANA, incluso in Latet per funzionare offline |
| Numero di telefono | `+44 20 7946 0958` | Analizzato e convalidato con [phonenumbers](https://github.com/daviddrysdale/python-phonenumbers), un port di libphonenumber di Google |
| IBAN | `GB82WEST12345698765432` | Lunghezza prevista per il paese e checksum mod-97 |
| Numero di carta | `4111 1111 1111 1111` | Prefisso dell'emittente e checksum di Luhn |
| Numero identificativo nazionale e numero fiscale | `SSN NNN-NN-NNNN`, `DNI NNNNNNNNA` | Cifre di controllo, e una parola chiave dove il numero da solo potrebbe essere qualsiasi cosa (vedi sotto) |
| Partita IVA | `DENNNNNNNNN`, `CHE-NNN.NNN.NNN MWST` | Il prefisso del paese e le cifre di controllo di quel paese |
| Indirizzo IP | `151.101.1.69` | Indirizzi pubblici; quelli privati solo in `max` o con `include_private_ips` |
| Indirizzo Bitcoin | `bc1q...` | Checksum Base58Check o bech32 |

I riconoscitori di email, IBAN, carte, codice fiscale e partita IVA sono ripresi da
[Presidio](https://github.com/data-privacy-stack/presidio).

### Numeri identificativi e fiscali per paese

Latet conosce i numeri identificativi nazionali e i numeri fiscali di 15 paesi: Belgio, Canada,
Finlandia, Francia, Germania, Italia, Lettonia, Paesi Bassi, Polonia, Portogallo, Spagna, Svezia,
Svizzera, Regno Unito e Stati Uniti. `id_regions` in `config.yaml` sceglie i paesi (`IT`, `US` e
`GB` per impostazione predefinita); `latet global-install` propone il tuo paese, e il profilo `max`
li prende tutti. La pagina **Regole** della dashboard elenca i numeri di ogni paese.

Molti di questi numeri sono semplici sequenze di nove o undici cifre, come innumerevoli id nel codice.
Per questo ognuno viene confermato dalle sue cifre di controllo, e quelli che potrebbero comunque
essere qualsiasi cosa richiedono una parola chiave vicina: *SSN* o *social security* accanto a un
numero di previdenza sociale statunitense, *BSN* accanto a un numero di servizio al cittadino
olandese, *Steuer-ID* accanto a un numero di identificazione fiscale tedesco. La parola chiave conta
se è sulla stessa riga, a pochi caratteri di distanza, o nell'intestazione della colonna in cui si
trova il numero (tabelle CSV, TSV e Markdown):

```text
name,ssn,city
Jane,NNN-NN-NNNN,Boston        ← found: the column is called ssn
```

Il DNI e il NIE spagnoli, il numero AVS svizzero e il codice personale finlandese hanno una lettera di
controllo o una struttura caratteristica, quindi non richiedono una parola chiave. Un numero non viene
mai preso dall'interno di un numero più lungo, di una versione, di un decimale o di un percorso. Il
[riferimento dei rilevatori](../../reference/detectors/) ha l'elenco completo, con la struttura e il
controllo di ogni numero.

Le partite IVA vengono riconosciute per i 27 paesi dell'UE, l'Irlanda del Nord, il Regno Unito, la
Svizzera e la Norvegia, qualunque cosa dica `id_regions`: hanno sempre il prefisso del loro paese.

Alcune cose restano come sono di proposito:

- le email ai domini di documentazione `example.com`, `example.net` e `example.org`, o sotto
  `.example`, `.test`, `.invalid` e `.localhost`, e i mittenti di sistema come `git@` o `noreply@`;
- nel codice e nell'output degli strumenti, le sequenze di cifre senza `+`, `00`, spazi o parentesi,
  che molto più spesso sono id, date o numeri di build che numeri di telefono. Nei tuoi messaggi le
  regole sono meno severe. I numeri senza prefisso internazionale contano solo per i paesi in
  `phone_regions` (`IT`, `US` e `GB` per impostazione predefinita);
- i numeri di versione che sembrano indirizzi IP, come `v1.2.3.4`.

### Nomi

I nomi nel testo libero, come in *Emily Carter ha scritto questo*, richiedono un modello statistico.
Con il [modello locale dei nomi](../../getting-started/installation/) facoltativo installato, Latet
trova da sé nomi e indirizzi; anche il [revisore locale](../llm-review/), un modello linguistico sul
tuo computer o su un tuo server, li trova, insieme ai dati sulla salute e agli altri valori che
nessun pattern descrive.
Senza nessuno dei due, un nome viene mascherato solo quando è il nome del tuo autore git (vedi
sotto), uno dei tuoi termini o un valore già nella cassaforte.

Prendi questo prompt:

```text
Hi, I'm Emily Carter and I live at 42 Mill Lane, Bristol BS1 4DJ.
```

Con il modello dei nomi, nel profilo `balanced`, il modello nel cloud riceve una versione come questa:

```text
Hi, I'm Hannah Mitchell and I live at 45 Moore Lane, Derby YN4 9CP.
```

Quando torna la risposta, il tuo agente vede comunque il nome e l'indirizzo reali.

Cosa cerca il modello dei nomi dipende dal [profilo](../profiles/):

- **`balanced`**: nomi completi (due parole o più) e indirizzi, in ciò che scrivi e alleghi e nel
  testo letto nelle immagini. Una città o un codice postale contano solo accanto a una via. Il codice, l'output degli strumenti e il testo
  di sistema non vengono letti.
- **`max`**: anche nomi di una sola parola e città, e anche il testo di sistema e l'output degli
  strumenti.
- **`secrets`**: mai.

Una volta trovato, un nome entra tra i [valori noti](#valori-noti) e da lì in poi viene mascherato
ovunque, output degli strumenti compreso, in ogni profilo, anche `secrets`.

Il modello non dà garanzie:

- **Gli sfuggono dei nomi.** I nomi poco comuni, le iniziali e i nomi scritti in minuscolo sono quelli
  che sfuggono più spesso. I saluti e i titoli davanti a un nome (*Ciao Paul*, *Dr. Sarah Mitchell*)
  restano fuori da ciò che viene mascherato.
- **Scambia alcune parole per nomi.** Le parole tecniche come *Redis* o *Postgres* vengono filtrate,
  e `balanced` richiede almeno due parole.
- **È lento sui testi lunghi.** Legge circa 700 token al secondo su una CPU desktop, in un thread
  tutto suo, così le altre richieste non lo aspettano. Un testo più lungo di 4.096 token (8.192 in `max`), o di 16.000 caratteri
  (64.000 in `max`), non viene letto, e la richiesta viene segnalata nella dashboard.

## I tuoi termini

Aggiungi le parole che solo tu sai essere sensibili: nomi di clienti, nomi in codice di progetti,
domini interni. Vengono mascherate in ogni profilo, ovunque compaiano:

```yaml title="config.yaml"
terms:
  - value: Project Falcon
    type: PROJECT
  - value: Acme Logistics
    type: CLIENT
```

Con questi termini, *"Project Falcon parte con Acme Logistics a maggio"* arriva al modello come
qualcosa di simile a *"Consolidated Margie parte con Northwind Adatum a maggio"*. Per impostazione
predefinita un termine corrisponde senza distinzione tra maiuscole e minuscole, e solo come parola
intera; `case_sensitive` e `whole_word` cambiano questo comportamento. Puoi aggiungere termini anche
nella pagina **Regole** della dashboard, o con **Maschera sempre** su qualsiasi valore in una
richiesta.

Un termine deve contenere almeno una lettera o una cifra: il valore fittizio conserva i simboli
dell'originale, quindi dei simboli da soli, come `--` o `###`, non si possono mascherare. La
dashboard e `latet config` rifiutano un termine così. Uno scritto a mano in `config.yaml` o nel
`latet.yaml` di un progetto viene lasciato fuori, e la pagina **Regole** e `latet config validate`
lo segnalano.

Un termine contiene al massimo 8.192 caratteri: uno più lungo viene cercato da solo, e uno di circa
un milione di lettere non si può cercare affatto. La dashboard, `latet config` e un'importazione
rifiutano un termine più lungo. Uno scritto a mano in `config.yaml` viene lasciato fuori, e
`latet reload`, il log del daemon e `latet config validate` dicono quale voce; uno nel `latet.yaml`
di un progetto viene lasciato fuori anche lui, e la pagina del progetto lo dice.

Per un formato invece che per un valore, come ogni numero di badge scritto `AB-123456`, scrivi un
[riconoscitore personalizzato](../custom-recognizers/): un pattern, cifre di controllo e parole
chiave, provato su esempi inventati.

## L'identità di questo computer

In `balanced` e `max`, Latet rileva chi sei e dove sei ogni volta che si avvia, e lo maschera:

- il tuo nome utente del sistema operativo nei percorsi della cartella home:
  `C:\Users\anna\shop\app.py`, `/home/anna/...` o `/Users/anna/...`. Un nome utente di sei caratteri
  o più viene mascherato ovunque compaia;
- il tuo nome host;
- il nome e l'email del tuo autore git, da `git config --global`, e il dominio di quell'email quando
  è quello della tua azienda (`studio-rossi.it`, non `gmail.com`), ovunque compaia: viene mascherato
  anche `git.studio-rossi.it`.

Poiché vengono rilevati di nuovo a ogni avvio, una nuova email git o un nuovo nome utente vengono
mascherati senza che tu debba fare nulla. La sezione `identity` della
[configurazione](../../reference/configuration/) disattiva questo comportamento, o fissa i valori: una
volta che li scrivi lì, Latet usa quelli.

## Valori noti

Una volta rilevato un valore, Latet lo ricorda nella cassaforte. Le occorrenze successive vengono
mascherate anche dove i rilevatori da soli non le riconoscerebbero: per esempio la password del tuo
`.env`, citata più tardi in un messaggio di commit. Due tipi non vengono ricordati così: un valore che
la modalità dati rigorosa o una regola di campo maschera in una tabella o in un JSON resta mascherato
solo nella sua struttura ([dati di produzione](../production-data/#modalità-dati-rigorosa)), e un
valore di un tipo impostato su `redact` non viene conservato affatto. I valori noti sono anche ciò
che il controllo finale cerca nelle richieste in uscita.

Un valore viene riconosciuto di nuovo a partire da quattro caratteri, il nome di una persona da tre
(*Ugo*, *Ada*), e solo com'è scritto: *Ugo* non è *ugo*, né una parte di *Ugolino*.

Un valore viene trovato anche dove è scritto con sequenze di escape: come `\u00f2` o `\\u00f2` in un
JSON che Latet non legge come documento (un blocco di codice, righe JSON, un documento contenuto in
una stringa), come byte in un log (`\xc3\xb2`) o in un percorso che git mette tra virgolette
(`\303\262`), codificato in un URL (`%C3%B2`, `%20`, oppure `+` in una query o in un modulo), o come
riferimento a un carattere HTML (`&#242;`, `&ograve;`). Lo stesso vale per i tuoi termini e per
l'identità di questo computer e, dove una sequenza scrive un carattere, per i dati personali e i
valori dei tuoi riconoscitori: così viene trovato anche un indirizzo scritto `mario.rossi%40acme.it`
(un segreto o un valore di una struttura viene trovato solo com'è scritto). Un valore trovato così
viene conservato com'è scritto, come un valore a sé con un valore fittizio suo, scritto con le stesse
sequenze dove fanno parte del testo: `Paolo%20Fontana` in un link, `\'` in una stringa tra apici.
All'agente torna il valore esattamente com'era scritto, così un link resta un link e una modifica
ritrova la sua riga. Dove il modello scrive il valore fittizio così come si legge,
`Paolo Fontana`, all'agente torna il valore così come si legge. La cassaforte registra come è stato
trovato ogni valore, così com'è scritto o dove i suoi escape si leggono, e quindi questo vale per un
valore di ogni tipo trovato in quel modo: un numero di telefono noto come `333 1234567` e incontrato
come `333+1234567` in una query torna anch'esso come `333 1234567`.

## Quando due rilevatori trovano lo stesso testo

Le corrispondenze sovrapposte vengono unite in una sola, che le copre tutte, così un valore non viene
mai sostituito a metà. Il tipo più forte dà il nome al risultato: prima i segreti, poi i tuoi termini e
l'identità di questo computer, poi i dati personali. Anche i tuoi termini e i valori noti che si
sovrappongono tra loro vengono trovati tutti: se sono noti sia *Anna Maria* sia *Maria Rossi*, il nome
*Anna Maria Rossi* viene mascherato per intero. Le sovrapposizioni vengono cercate fino a un limite in
ogni testo: oltre quel limite, il resto del testo viene mascherato come prima, un valore dopo l'altro.

Se va in errore un rilevatore diverso da quello dei dati personali e dal modello locale dei nomi
(segreti, i tuoi termini, identità, valori noti), il mascheramento non riesce e la richiesta viene
bloccata: niente esce senza controllo. Se va in errore il rilevatore dei dati personali o il modello
locale dei nomi, viene saltato e la richiesta viene segnalata nella dashboard.

## Perché RE2

RE2 trova le corrispondenze in tempo lineare, quindi un input ostile o semplicemente enorme non può
causare un backtracking catastrofico delle espressioni regolari e bloccare il tuo agente.

## Falsi positivi e falsi negativi

Ogni rilevatore è un compromesso tra valori che sfuggono e falsi allarmi. I [profili](../profiles/)
scelgono dove collocarsi in questo compromesso, e i [valori consentiti](../profiles/#valori-consentiti)
correggono i singoli falsi allarmi. Per vedere come verrebbe mascherato un testo, incollalo nel
**Laboratorio** della dashboard: niente di ciò che scrivi lì viene conservato o inviato a un
modello.
