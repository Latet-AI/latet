---
title: Dati di produzione
description: Mascherare i risultati delle query, le risposte delle API e i log che un agente legge, ogni valore o campo per campo.
---

Un agente che indaga su un bug spesso legge dati reali: una query sul database di produzione, la
risposta di un'API REST, un log, una pagina nel browser. I rilevatori di Latet vi trovano ciò che ha
un formato riconoscibile, come indirizzi email, IBAN, token e numeri identificativi. Una nota, uno
stipendio o una clausola di un contratto non hanno un formato: in una tabella uscirebbero così come
sono.

Latet legge quegli output in base alla loro struttura. L'output di uno strumento è spesso una tabella,
un documento JSON o righe di coppie `key=value`, e il nome di ogni colonna o chiave dice cosa sono i
suoi valori. Tre cose lo sfruttano:

| | Quando | Cosa maschera |
| --- | --- | --- |
| **Colonne segrete** | sempre | i valori delle colonne e delle chiavi con il nome di un segreto: `password_hash`, `api_key`, `reset_token` |
| **Regole di campo** | quando le scrivi | i valori dei campi che indichi, come un tipo tuo |
| **Modalità dati rigorosa** | con il profilo `max`, o quando la attivi | ogni altro valore della struttura |

## Le strutture che Latet legge

- **Tabelle:** psql, CSV, TSV e separate da `;`, markdown, HTML, e le tabelle dello snapshot di
  accessibilità di un browser (`columnheader`, `cell`), e le tabelle disegnate con caratteri di
  riquadro (`console.table` di Node, lo stile unicode di psql).
- **Coppie `key=value`:** due o più su una riga, come in un log.
- **JSON:** un documento intero valore per valore, ognuno con il suo percorso (`items[].sku`); un
  frammento `"key": value` dentro un altro testo, come una riga di log, in base alla sua chiave.

## Colonne segrete

Sono sempre attive. Un CSV di utenti:

```text
id,email,api_key,password_hash,reset_token,role
7,ops@acme-intra.com,ak_live_9fK2LmQx7Rt3vW8zB1nY5pD,$2b$12$Kq3v0rZQ8xGm1o5t,8f14e45fceea167a,admin
```

esce così:

```text
id,email,api_key,password_hash,reset_token,role
7,oliver@consolidated-e55f.example,ak_live_3zF1ObRs6Wa1eH2mU6tO2fM,p5cp22yIa9h4bDH9pBx9p8n,a03c15f272977c30,admin
```

La chiave, l'hash e il token sono segreti per via della loro colonna, l'indirizzo email per via del
suo formato; `admin` resta com'è. Una colonna segreta resta mascherata anche quando una regola di
campo lascia in chiaro il suo campo.

## Modalità dati rigorosa

La attiva il [profilo `max`](../profiles/). Con gli altri profili, o per dirlo esplicitamente:

```yaml
data: strict
```

`data: normal` la disattiva anche con `max`; se ometti `data`, decide il profilo. Disattivarla dove è
attiva riduce la protezione: se lo fa un agente o la riga di comando, la modifica attende la tua
approvazione.

Ogni valore di una tabella, di un documento JSON o di coppie `key=value` nell'output di uno strumento
viene mascherato; i nomi delle colonne e delle chiavi restano, tranne una chiave che è essa stessa un
valore, come un indirizzo email, che viene mascherata come i valori ovunque. Il risultato di una
query:

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | SHIPPED | 1250.5 | call back on +44 20 7946 0958
```

esce così:

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | QXWRLTE | 2939.3 | wkqp xoaj zd +44 20 3318 4452
```

- **Lo stesso valore riceve sempre lo stesso valore fittizio.** Conteggi, raggruppamenti e join
  continuano a funzionare su ciò che il modello legge.
- **Una query scritta con un valore fittizio viene eseguita con il valore reale.** Il modello scrive
  `WHERE status = 'QXWRLTE'`; lo strumento esegue `WHERE status = 'SHIPPED'`.
- **Un valore fittizio mantiene il formato del valore.** Un numero resta un numero con altrettante
  cifre, una data ISO una data valida (entro due anni da quella reale), un orario un orario valido.
  Quando per un formato non restano valori fittizi liberi, come con centinaia di id di tre cifre in
  un solo output, il valore fittizio è più lungo di un carattere.
- **Un link resta un link.** Un indirizzo web mantiene lo schema (`https://`, `s3://`...); il resto è
  mascherato.
- **Ciò che nomina un valore o dice come appare una pagina resta in chiaro:** un segnaposto di un
  template o di una shell (`${from}`, `{{ subject }}`, `$MAIL_FROM`), e `class` e `style` del markup
  che un comando stampa, a meno che lo stile carichi un indirizzo. Una riga di tabella che non
  corrisponde alle sue colonne è mascherata per intero.
- **Un valore trovato dentro un altro mantiene il suo tipo.** Il numero di telefono nella nota riceve
  un numero di telefono fittizio; il resto della nota viene mascherato come nota.
- **Un valore resta nella sua struttura.** `SHIPPED` viene mascherato nella tabella, mai nel tuo
  codice: la modalità dati rigorosa non aggiunge i suoi valori a quelli mascherati ovunque.
- **I valori di uno o due caratteri restano in chiaro,** come `17` qui sopra: dicono poco, e un valore
  fittizio così corto non si potrebbe distinguere in una risposta. Restano in chiaro anche i valori
  vuoti e le parole che stanno per nessun valore o per sì e no, come `null`, `n/a`, `true` e `false`.
- **Un valore più lungo di 16.384 caratteri viene oscurato,** non sostituito con un valore fittizio:
  il modello riceve `[REDACTED:DATA]`, e niente viene rimesso al suo posto.
- **I file restano come sono.** L'output degli strumenti che leggono file (`Read`, `Grep`, `Glob`,
  `LS`... di Claude Code) è codice e documenti, non dati, a meno che una regola indichi il file
  ([file che contano come dati](#file-che-contano-come-dati)). Gli altri rilevatori vi si applicano
  comunque.
- **Lo stesso vale per i risultati web.** I risultati delle ricerche e le pagine (`WebSearch`,
  `WebFetch`) sono contenuti pubblici, non i tuoi dati, e arrivano come JSON: la modalità dati
  rigorosa trasformerebbe ogni titolo e ogni indirizzo in lettere casuali, lasciando al modello niente
  da leggere o da citare. Gli altri rilevatori vi si applicano comunque, quindi una chiave o un
  indirizzo email in una pagina viene mascherato come in qualsiasi altro posto. Per coprire comunque
  una pagina web, indica il suo indirizzo in `data_hosts`
  ([indirizzi web che contano come dati](#indirizzi-web-che-contano-come-dati)). Per coprire uno
  strumento web, aggiungilo a `data_tools` insieme a ogni altro strumento che la modalità dati
  rigorosa deve coprire: una volta impostato `data_tools`, la modalità dati rigorosa copre solo gli
  strumenti che indica, e `"*"` indica tutti gli strumenti tranne quelli che leggono file.

### Quali strumenti

La modalità dati rigorosa copre l'output di ogni strumento, tranne quelli che leggono file e gli
strumenti web. Per coprirne solo alcuni, indicali, con `*` per qualsiasi sequenza di caratteri:

```yaml
data: strict
data_tools: [mcp__postgres__*, Bash]
```

Uno strumento di un server MCP porta il nome del server: `mcp__crm__*` indica ogni strumento del
server `crm`.

Un comando che esegui con `!` in Claude Code conta come output dello strumento `Bash`.

Codex legge i file con comandi della shell, quindi in Codex la modalità dati rigorosa copre anche i
file che legge. Indica il suo strumento per il database in `data_tools` per coprire solo quello.

### File che contano come dati

```yaml
data: strict
data_files: ["exports/**", "data/*.csv", ".env*"]
```

Un file che un modello indica è un dato che la modalità dati rigorosa sia attiva o no: basta indicarlo. Un file che l'agente legge e il cui percorso corrisponde a un modello conta come dati: ogni suo
valore viene mascherato, come nell'output di uno strumento. Un modello senza barra indica un file per
nome (`*.json`, `.env*`), uno con la barra la fine del suo percorso (`exports/**`, `**/*.csv`); `*`
indica qualsiasi sequenza di caratteri, e maiuscole e minuscole non contano. I valori si trovano
secondo il formato del file:

- **JSON** (`.json`, `.jsonl`, o un file con un altro nome che contiene JSON): ogni stringa e ogni
  numero, secondo il percorso della sua chiave, come nell'output JSON di uno strumento. Le chiavi
  restano.
- **Tabelle** (`.csv`, `.tsv`): ogni cella, secondo la sua colonna.
- **File `.env`, properties e ini:** il valore di ogni riga `KEY=value`.
- **YAML:** ogni valore, secondo il suo percorso (`servers[].host`); le chiavi e la struttura restano.
- **Qualsiasi altro file:** le tabelle, le coppie `key=value` e i frammenti JSON che contiene.

Ogni valore viene sostituito dov'è: il file conserva le sue righe, i loro numeri e le sue chiavi,
così l'agente può ancora modificarlo in base al testo che ha letto, e una modifica scritta con un
valore fittizio scrive il valore reale. Solo gli strumenti che restituiscono il testo di un file lo
rendono dati (`Read` e `NotebookRead` di Claude Code): le righe di una ricerca (`Grep`) e un elenco
(`Glob`, `LS`) restano come sono. Quando `Edit`, `MultiEdit` o `Write` di Claude Code modificano un
file che un modello indica, le righe del file che mostrano, che l'agente potrebbe non aver mai letto,
vengono mascherate come il file, anche quando `data_tools` indica altri strumenti.

- **Un modello ampio copre più dei dati.** `*.json` copre anche `package.json` e `tsconfig.json`, i
  cui valori verrebbero allora mascherati ovunque nella conversazione: indica le cartelle che
  contengono dati (`exports/**`).
- **Il markdown conta solo quando un modello lo indica** (`*.md`, `docs/**/*.md`): gli agenti
  leggono le loro istruzioni e i loro documenti in markdown, e un modello per ogni file (`*`) li
  lascia come sono. In un file markdown che un modello indica vengono mascherate solo le tabelle, mai
  il testo.
- **I PDF non sono coperti:** Latet non legge il testo di un PDF. `images: mask` o `block` impedisce
  del tutto l'invio dei documenti ([immagini](../images/)).

### Indirizzi web che contano come dati

```yaml
data: strict
data_hosts: [api.internal.example.com, "*.salesforce.com"]
```

Un indirizzo che un modello indica è un dato che la modalità dati rigorosa sia attiva o no. Una pagina che uno strumento web (`WebFetch`) scarica da un host che corrisponde a un modello conta
come dati, come l'output di uno strumento: ogni valore delle sue tabelle, del suo JSON e delle coppie
`key=value` viene mascherato. `*` indica qualsiasi sequenza di caratteri; maiuscole, minuscole e
porta non contano. Un modello per ogni sottodominio indica anche il dominio stesso: anche
`salesforce.com` rientra in `*.salesforce.com`. Un host scritto con un punto finale (`example.com.`)
è lo stesso host scritto senza. Una ricerca (`WebSearch`) non ha un indirizzo suo. Per coprire le
ricerche, aggiungi `WebSearch` a `data_tools` insieme agli altri strumenti che vuoi coprire, oppure
scrivi `data_tools: ["*"]` per ogni strumento tranne quelli che leggono file: un `data_tools` che
indica solo `WebSearch` copre soltanto le ricerche, e lascia fuori i tuoi strumenti per il database e
per la shell. Con la modalità dati rigorosa attiva, impostare `data_tools` dove era vuoto la
restringe, quindi attende la tua approvazione.

Aggiungere un modello o un indirizzo vale subito; toglierne uno copre meno, quindi riduce la
protezione: se lo fa un agente o la riga di comando, la modifica attende la tua approvazione. Nella
dashboard si impostano in **Regole** → **Protezione** → **Dati di produzione**, accanto agli
strumenti.

### In uno scambio

La modalità dati rigorosa si imposta in **Regole** → **Protezione** → **Dati di produzione**, o
nelle tue impostazioni per un progetto; uno scambio non ha un interruttore per attivarla. Mentre è
disattivata per uno scambio (nel progetto dello scambio, o nelle tue regole globali per uno scambio
senza progetto), la dashboard chiede a Latet se uno dei dieci output di strumenti più recenti della
richiesta contiene una tabella, del JSON o coppie `key=value`, letti come per
[scegliere le colonne da lasciare in chiaro](#scegliere-le-colonne-da-lasciare-in-chiaro), e viene
da uno strumento che la modalità dati rigorosa copre ([quali strumenti](#quali-strumenti): non uno
che legge file, né uno strumento web, a meno che una regola indichi il file, l'indirizzo o lo strumento). Se ne trova uno, una riga sopra i
blocchi della richiesta lo dice, con un link a dove si imposta la modalità dati rigorosa per quello
scambio: le tue impostazioni per il suo progetto quando impostano `data`, altrimenti **Regole** →
**Protezione** → **Dati di produzione**. Latet legge gli output dai testi che conserva la
cronologia: con `history: metadata` o `off` la riga non c'è.

## Regole di campo

Una regola di campo indica una colonna, una chiave JSON o un percorso JSON (`items[].sku`); `*`
corrisponde a qualsiasi sequenza di caratteri, e maiuscole e minuscole non contano. Ogni altro
carattere viene preso com'è scritto, `?` e parentesi quadre comprese, quindi `orders[].items[].sku`
indica proprio quel percorso. I suoi valori vengono mascherati come il tipo che indichi, con o senza
modalità dati rigorosa:

```yaml
fields:
  - {name: note_*, type: NOTE}
  - {name: ral, type: SALARY}
  - {name: iban_fornitore, type: IBAN, secret: true}
```

- `secret: true` rende segreti i suoi valori: uno che arriva in chiaro in una richiesta la blocca.
- `mask: false` lascia un campo in chiaro nella modalità dati rigorosa. Va nelle tue impostazioni, non
  nel `latet.yaml` di un progetto, che può solo aggiungere protezione.

Vince la prima regola che corrisponde a un campo: le tue impostazioni per un progetto vengono prima
del suo `latet.yaml`, ed entrambi prima delle tue regole globali.

## Scegliere le colonne da lasciare in chiaro

La modalità dati rigorosa maschera ogni valore, quindi il modello non può leggere nemmeno uno stato o
una data: `SHIPPED` diventa `QXWRLTE`, e un agente a cui si chiede quali ordini sono stati spediti in
ritardo non ha niente su cui basarsi. La maggior parte delle tabelle ha colonne che non dicono nulla
di una persona, e la dashboard le propone.

In uno scambio, sotto l'output di uno strumento che la modalità dati rigorosa ha mascherato, apri
*Scegli le colonne da lasciare in chiaro*. Ogni colonna dell'output ha una proposta e il suo motivo:

| Proposta | Per |
| --- | --- |
| **Lascia in chiaro** | un nome che indica id o codici, date di eventi (`created_at`, `order_date`), stati (`status`, `state`), sì o no (`active`), conteggi (`qty`) |
| **Lascia mascherata** | dati personali o un segreto trovati nei suoi valori; un nome che riguarda persone (`customer`, `created_by`, `doctor`), segreti (`api_key`), salute, convinzioni o vita privata (`hiv_status`, `marital_status`); testo libero; date di nascita o di morte |
| **Decidi tu** | importi; date, sì o no, o pochi valori che si ripetono, quando il nome non dice cosa sono; le colonne su cui niente dà indicazioni |

Una colonna viene proposta in chiaro solo quando il suo nome dice cosa contiene e niente nei suoi
valori è un dato personale o un segreto: li controllano gli stessi rilevatori di tutto il resto. Il
formato dei valori da solo non mette mai una colonna in chiaro, perché i nomi utente si ripetono come
gli stati. Le proposte sono un punto di partenza: guarda i valori mostrati accanto a ogni colonna
prima di applicare.

Le colonne proposte in chiaro sono già selezionate. *Lascia 2 colonne in chiaro* aggiunge una regola
per ognuna, come `{name: status, mask: false}`, in cima alle tue impostazioni per il progetto
(*Questo progetto*) o alle tue regole globali (*Ovunque*): vince la prima regola che corrisponde,
quindi una regola più ampia come `note_*` non la maschera di nuovo. Gli output successivi mostrano
quelle colonne così come sono. Non si applica nulla finché non fai clic.

- **Calcolato quando apri l'elenco.** Le colonne vengono lette dall'output che conserva la cronologia,
  sul tuo computer, e non viene salvato nulla di nuovo. Con `history: metadata` o `off` non c'è
  nessun elenco.
- **Il parere del revisore locale, se lo chiedi.** Con il [revisore locale](../llm-review/) attivo,
  *Chiedi al revisore locale* gli invia i nomi delle colonne e alcuni valori di ognuna, e mostra il
  suo parere accanto alle proposte. Non viene mai applicato da solo.
- **Le regole di un progetto vengono prima.** Le tue impostazioni per un progetto e il suo
  `latet.yaml` vengono prima delle tue regole globali: una colonna che uno dei due maschera lì resta
  mascherata, qualunque cosa aggiunga *Ovunque*, e l'elenco lo dice.
- **Dopo un riavvio,** le impostazioni di un progetto si caricano alla sua sessione successiva: fino
  ad allora la scelta può andare solo nelle tue regole globali.
- **Codex e OpenCode** inviano tutta la conversazione con ogni richiesta, e Latet la maschera di nuovo
  ogni volta: dalla richiesta successiva, anche gli output già presenti partono con quelle colonne in
  chiaro. In Claude Code un output resta come il plugin l'ha mascherato quando è arrivato.

Per una sola colonna, fai clic su uno dei suoi valori: il pannello che si apre propone
*Non mascherare `status` in questo progetto*, e dice quando il nome di una colonna sembra un id, una
data, uno stato o un conteggio.

## La protezione dai dump

```yaml
dump_guard: 50
```

L'output di uno strumento che contiene più valori personali di questo numero (nomi, indirizzi email,
numeri di telefono, numeri identificativi...) viene trattenuto: il modello riceve al suo posto una
breve nota che gli dice di chiedere meno righe o dei conteggi. La dashboard segna lo scambio con
*Trattenuto*. Non impostato, o 0: nessun limite.

Nel profilo `max`, con il [modello locale dei nomi](../../getting-started/installation/) installato,
l'output di uno strumento viene letto anche in cerca di nomi e indirizzi: i testi e le chiavi di un
output JSON vengono letti tutti insieme, e uno che il modello non riesce a leggere entro cinque
secondi viene trattenuto allo stesso modo, con una nota che dice al modello di chiedere meno righe o
campi. Nulla di ciò che vi avrebbe trovato esce in chiaro.

## Il browser

Un agente che guida un browser legge le pagine come output di strumenti: snapshot di accessibilità,
HTML, risultati di script, risposte di rete. La modalità dati rigorosa si applica a loro come al
resto, tabelle comprese.

I suoi screenshot sono immagini: con il [riconoscimento del testo](../images/) installato, il loro
testo viene letto e mascherato, e ogni valore viene coperto con il suo valore fittizio. Per inviare in
un progetto solo le immagini che Latet ha letto e mascherato, o nessuna:

```yaml
images: mask    # or block: no image or PDF is sent
```

## In un progetto

Queste chiavi funzionano in [config.yaml](../../reference/configuration/), nel
[`latet.yaml`](../projects/) di un progetto e nelle tue impostazioni per un progetto. Il file del
progetto può solo aggiungere protezione:

| Chiave | In `latet.yaml` | Nelle tue impostazioni per il progetto |
| --- | --- | --- |
| `data` | solo `strict` | `strict` o `normal` |
| `data_tools` | ignorato | aggiunto ai tuoi |
| `data_files` | aggiunto | aggiunto ai tuoi |
| `data_hosts` | aggiunto | aggiunto ai tuoi |
| `fields` | solo regole che mascherano | qualsiasi regola, `mask: false` compreso |
| `dump_guard` | solo più basso | qualsiasi valore; 0 la disattiva |
| `images` | solo meno immagini: `mask`, poi `block` | `allow`, `mask` o `block` |
| `ocr` | solo `auto` | `auto` o `off` |

Un progetto il cui profilo è `max`, nel suo file o nelle tue impostazioni per il progetto, ha la
modalità dati rigorosa attiva, a meno che `data` non dica altrimenti lì o nelle tue regole globali.
