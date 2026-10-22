---
title: Contribuire
description: Prepara un ambiente di sviluppo, esegui i controlli, tieni aggiornata la documentazione e aggiungi una lingua.
---

Prima di iniziare qualcosa di grande, apri una issue per discuterne.

## Ambiente di sviluppo

Ti serve [uv](https://docs.astral.sh/uv/), che installa la versione di Python usata dal progetto.

```sh
git clone https://github.com/Latet-AI/latet.git
cd latet
uv sync
```

Esegui Latet dal checkout con `uv run`:

```sh
uv run latet serve       # the daemon in the foreground, with its log in the terminal
uv run latet claude      # Claude Code with the plugin from src/latet/claude_plugin
uv run latet doctor
```

`latet serve --passthrough` inoltra il traffico senza mascherarlo, e `--capture <folder>` scrive
ogni scambio in file locali (con gli header delle credenziali oscurati), il che è utile quando
studi un protocollo. Le catture contengono il contenuto delle conversazioni: non includerle mai
in un commit (per questo git ignora `notes/captures/`).

Per tenere un'istanza di prova separata da un Latet che usi per davvero, imposta `LATET_DATA_DIR` e
`LATET_CONFIG` su una cartella di prova e scegli un'altra porta in quel `config.yaml`.

## Controlli

Eseguili prima di aprire una pull request. La CI li esegue nei suoi job di lint e di test, i test su
Windows, macOS e Linux con più esempi per i test a proprietà (`HYPOTHESIS_PROFILE=ci`). La CI esegue
anche `uv run python scripts/gen_notices.py --check`, i test del server e i controlli del plugin,
della dashboard e della documentazione descritti più sotto, e costruisce gli installer e l'immagine
del container.

```sh
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright
uv run python scripts/gen_reference.py --check
```

- **Pagine di riferimento.** Le pagine [CLI](../reference/cli/), [configurazione](../reference/configuration/)
  e [rilevatori](../reference/detectors/) sono generate dal codice. Dopo aver cambiato un comando,
  una chiave di configurazione o un rilevatore, rigenerale con
  `uv run python scripts/gen_reference.py`. Non modificarle mai a mano.
- **Plugin di Claude Code.** `claude plugin validate src/latet/claude_plugin` verifica il
  plugin con una versione installata compatibile. `claude plugin test`, eseguito in
  `src/latet/claude_plugin/`, non è disponibile in ogni runtime installato; usalo solo se quel
  runtime espone il comando di test.
- **Falsi positivi.** `tests/unit/test_fp_budget.py` esegue i rilevatori sulla libreria standard
  di Python, che non contiene segreti reali, con un budget per profilo che può solo scendere.
  `uv run python scripts/fp_corpus.py` stampa ciò che scatta su un corpus più ampio, regola per
  regola.
- **Avvisi di terze parti.** Dopo aver cambiato le dipendenze, esegui `npm ci` in `dashboard/` e in
  `docs/`, poi rigenerali con `uv run python scripts/gen_notices.py`. Aggiungi `--check` per
  verificare che il file sia aggiornato senza riscriverlo; la CI esegue quel controllo.

Regola di base: i test usano solo dati sintetici. Non includere mai in un commit segreti reali, dati
personali reali o catture del traffico.

## Test del server

Usa Docker e lo script esistente per PostgreSQL, Valkey e PostgreSQL con TLS isolati per
i test. Il primo avvio scarica le immagini se mancano:

```sh
uv run python scripts/server-services.py up
uv run python scripts/server-services.py env
```

Imposta nella shell le variabili stampate `LATET_TEST_DATABASE_URL`, `LATET_TEST_VALKEY_URL`
e `LATET_TEST_TLS_DATABASE_URL`, con la sintassi della shell. Indicano servizi temporanei
di test, mai dati di produzione. Imposta `LATET_TEST_REQUIRE_SERVICES=1`, poi esegui:

```sh
uv run pytest -q -m server
```

Ogni test usa uno schema del database e un prefisso Valkey propri. Il job CI `server`
fornisce i servizi e li richiede: un servizio indisponibile deve far fallire il test,
non saltarlo silenziosamente. Ferma i tuoi servizi creati dallo script con
`uv run python scripts/server-services.py down`; coordina quelli condivisi con chi li gestisce.

I test degli agenti reali sono facoltativi, con `LATET_AGENT_TESTS=1` e
`uv run pytest -m agents`. Usano programmi di terze parti installati contro provider
simulati locali, ma quei programmi possono contattare i propri host esterni.
Usa cartelle personali isolate e credenziali sintetiche; le fixture locali non garantiscono
assenza di traffico esterno. I test marcati `live` usano i modelli locali per il riconoscimento dei
nomi e del testo: vengono saltati dove quei modelli non sono installati, come in CI.

## Dashboard

La dashboard si trova in `dashboard/` (React, Vite e [i18next](https://www.i18next.com)) e
richiede Node.js 22.12 o successivo.

```sh
cd dashboard
npm ci
npm run dev:mock    # the dashboard with sample data, no daemon needed
npm run lint
npm run check:i18n  # every key the code uses is in en.json, and every language has its keys
npm test
npm run build       # writes the files the daemon serves into src/latet/web/
```

Una nuova stringa va in `en.json` e, tradotta, anche nei file delle altre lingue, nella stessa
modifica: `check:i18n` vuole le stesse chiavi in ogni lingua.

## Il tutorial

I capitoli di `latet tutorial` sono file YAML in `src/latet/tutorial/chapters/`: il prompt e la
spiegazione di ogni passaggio, la risposta del modello simulato, e i controlli che devono valere
dopo. I test riproducono ogni capitolo dall'inizio alla fine con il mascheramento reale, due volte,
quindi una modifica a Latet che rende un capitolo non più vero li fa fallire: cambia il capitolo
insieme al comportamento. Rifiutano anche un capitolo che nomina un lato dello schermo (su uno
schermo stretto la pagina impila i suoi riquadri) o che contiene un valore che uno scanner di
segreti scambierebbe per la chiave di un provider.

Da un checkout, `uv run latet tutorial` richiede la dashboard compilata (`npm run build` in
`dashboard/`). Anche la modalità mock della dashboard riproduce i capitoli, con `?mock-tutorial`,
dalla sua copia in `dashboard/src/mock/tutorial.ts`: modificala insieme al capitolo.

## Documentazione

Il sito si trova in `docs/` ed è costruito con [Starlight](https://starlight.astro.build). Ti serve
Node.js 22.12 o successivo.

```sh
cd docs
npm ci
npm run dev     # http://localhost:4321/
npm run build   # static site in docs/dist/
```

Le pagine sono file Markdown o MDX in `docs/src/content/docs/`. La barra laterale è definita in
`docs/astro.config.mjs`.

**L'inglese è la versione di riferimento.** Una pull request che cambia ciò che fa Latet aggiorna
le pagine in inglese che tocca, nella stessa pull request: una nuova opzione, un nuovo comando o un
nuovo comportamento viene documentato in inglese prima di essere integrato. Le traduzioni seguono
le pagine in inglese, mai il contrario. Una pull request può cambiare solo le pagine in inglese: le
altre lingue le allineano i maintainer.

**Gli screenshot della dashboard** li fa uno script, dai suoi dati di esempio, in ogni lingua e in
entrambi i temi: dopo una modifica che si vede in uno di essi, esegui `npm run screenshots` in
`dashboard/` (`npm run screenshots -- live it` ne rifà solo alcuni). La prima volta,
`npx playwright install chromium` scarica il browser che usa. Una pagina MDX ne mostra uno con
`<Screenshot name="live" alt="…" />`, dopo aver importato `docs/src/components/Screenshot.astro` come
fa `dashboard.mdx`.

## Come aggiungere una lingua

### Documentazione

La documentazione esiste in cinque lingue: l'inglese alla radice, come riferimento, e l'italiano,
il tedesco, il francese e lo spagnolo sotto `/it/`, `/de/`, `/fr/` e `/es/` (i `locales` in
`docs/astro.config.mjs`). Una pagina non ancora tradotta mostra quella in inglese, con un avviso.
Le pagine di riferimento generate dal codice restano in inglese in ogni lingua.

- **Tradurre una pagina** significa crearla sotto `docs/src/content/docs/<lang>/`, allo stesso
  percorso di quella in inglese: `concepts/how-it-works.mdx` diventa `it/concepts/how-it-works.mdx`.
  I percorsi dei suoi import e delle sue immagini prendono un `../` in più, perché il file sta una
  cartella più in basso.
- **I link tra le pagine restano relativi**, così una pagina tradotta li mantiene nella sua lingua.
  Un titolo tradotto cambia la sua ancora, quindi cambiano anche i link che vi puntano:
  `scripts/check_docs_links.py` trova quelli dimenticati (eseguilo dalla radice del repository dopo
  `npm run build`; lo esegue anche la CI).
- **Le stringhe dell'interfaccia** che Latet aggiunge sono in `docs/src/content/i18n/<lang>.json`, e
  le etichette della barra laterale nelle `translations` di `docs/astro.config.mjs`. L'interfaccia
  di Starlight è già tradotta.
- **Un'altra lingua** è una voce in più in `locales`, il suo `docs/src/content/i18n/<lang>.json` e
  le sue `translations` della barra laterale.

Esegui `npm run build` e controlla il risultato con `npm run preview`.

### Dashboard

La dashboard è disponibile in inglese, italiano, tedesco, francese e spagnolo. Per aggiungere
un'altra lingua (qui il portoghese):

1. Copia `dashboard/src/locales/en.json` in `pt.json` e traduci i valori. Mantieni le chiavi e le
   `{{variables}}`, e aggiungi ogni forma plurale di cui la lingua ha bisogno (`key_many` in
   portoghese).
2. Esegui `npm run check:i18n`: elenca le chiavi mancanti o in più, le variabili che differiscono e
   le forme plurali che mancano nel file.
3. Registrala in `dashboard/src/i18n.ts`, con il suo nome nella lingua stessa (`LANGUAGE_NAMES`), e
   aggiungila alle lingue che `dashboard/src/i18n.test.ts` si aspetta. Quel file di test usa il
   portoghese come lingua che la dashboard non ha (`pt-BR`, `docsUrl(…, 'pt')`): cambia quei casi con
   un'altra lingua. Finché la documentazione non esiste nella nuova lingua, fai in modo che `docsUrl`
   porti alle pagine in inglese; quando esiste, aggiungi la lingua a `LANGUAGES` in
   `dashboard/scripts/screenshots.mjs`.

La dashboard si apre nella lingua che chi legge ha scelto nel suo menu, altrimenti nella prima delle
lingue del browser di cui dispone, altrimenti in inglese. `dashboard/README.md` ha i dettagli.

### Tutorial

I testi di un capitolo in un'altra lingua sono in
`src/latet/tutorial/chapters/<lang>/<chapter>.yaml`: i titoli, le spiegazioni, i prompt, le righe
dell'agente e le risposte del modello, passaggio per passaggio, ciascuno con i segnaposto del suo
testo inglese. Il resto del capitolo, i valori e i controlli, viene dal file inglese. Chi modifica
i testi inglesi di un capitolo li cambia in ogni lingua. I test eseguono ogni capitolo in ogni
lingua, e applicano a ogni lingua le sue regole editoriali (`tests/unit/tutorial/test_chapters.py`).
Una nuova lingua ha bisogno anche delle sue regole in quel file, in `ON_SCREEN` e `JARGON`, altrimenti
i test falliscono.

Una pull request può cambiare solo i testi inglesi di un capitolo. Un testo che acquista o perde
un segnaposto richiede la stessa modifica nelle altre lingue, altrimenti i test falliscono; il resto
lo traducono i maintainer.

## Come aggiungere un paese

I numeri identificativi e fiscali nazionali si trovano in `src/latet/engine/detectors/ids/regions/`,
un modulo per paese, ciascuno una tupla di `IdSpec`. I numeri di partita IVA si trovano in
`ids/vat.py`.

1. Scrivi `regions/<cc>.py` (il codice ISO 3166-1 alpha-2): per ogni numero, un pattern per come
   viene scritto, un validatore, un generatore di valori fittizi validi nello stesso formato, le
   sue parole chiave, e quando una parola chiave è obbligatoria (`context`: `never`, `bare`, `code`
   o `always`). Scrivi il validatore partendo dalla specifica pubblica dell'autorità che rilascia il
   numero. python-stdnum è un riferimento solo per i test, con licenza LGPL: non copiarne mai il
   codice.
2. Registra il modulo in `regions/__init__.py`, e il paese in `ID_REGIONS` in
   `src/latet/config.py`.
3. Aggiungi un formato e il modulo di stdnum, se esiste, a `tests/unit/test_ids_oracle.py`, e una
   parola chiave nei casi di `test_ids.py` se serve. Il test oracolo controlla 200 valori fittizi
   per tipo e confronta il validatore con stdnum su numeri vicini a quelli validi; elenca lì le
   differenze volute.
4. Aggiungi l'etichetta del tipo alle cinque lingue della dashboard,
   `dashboard/src/locales/<lang>.json` (`rules.pii.types`), a `PII_TYPES` in
   `dashboard/src/lib/labels.ts` e la sua descrizione a `PII_HELP` in
   `dashboard/src/pages/RulesPage.tsx`; aggiungi il tipo e il paese anche al catalogo del mock
   (`PII_CATALOG` e `id_regions_available` in `dashboard/src/mock/fixtures.ts`, `ID_TYPES` in
   `dashboard/src/mock/builder.ts`). Poi esegui `uv run python scripts/gen_reference.py` per le
   tabelle di riferimento.
5. Esegui il budget dei falsi positivi (`tests/unit/test_fp_budget.py`): nessuna regola sui numeri
   identificativi può scattare sul campione della libreria standard.

## Come misurare un revisore locale

Quale modello locale funzioni meglio come [revisore locale](../concepts/llm-review/#quanto-è-veloce-e-quale-modello)
è ancora una questione aperta, e le tue misure sono benvenute. Fai girare un modello su questo
computer (llama.cpp, Ollama o LM Studio), fai puntare `LATET_LLM_URL` e `LATET_LLM_MODEL` al
modello, ed esegui il benchmark sui suoi 93 casi inventati:

```sh
uv run python scripts/llm_review_bench.py run --label <model> --variants A
uv run python scripts/llm_review_bench.py report
```

Invia ciò che ha scritto in `benchmarks/llm-review/` (le tue righe in `results/`, e `tables.md`)
in una pull request, con la tua scheda grafica o la tua CPU, il server e la quantizzazione del
modello. `benchmarks/llm-review/README.md` spiega come sono stati ottenuti i numeri pubblicati.

## Licenza

Latet è concesso in licenza secondo la Latet Sustainable Use License, la sua Enterprise Edition secondo
la Latet Enterprise License, e Latet è offerto anche con licenze commerciali ([LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md)).
I contributi sono accettati secondo l'[accordo di licenza per i contributori](https://github.com/Latet-AI/latet/blob/main/CLA.md):
mantieni il tuo copyright, e il tuo contributo può far parte di Latet con tutte queste licenze. Per
accettarlo, spunta la casella "I agree to the Latet contributor license agreement (CLA.md)." nel
modello della tua prima pull request.
