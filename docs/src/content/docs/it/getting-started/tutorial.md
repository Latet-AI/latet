---
title: Tutorial
description: Guarda Latet al lavoro sul tuo computer in circa 30 minuti, con un agente simulato, un modello simulato e dati inventati, prima di collegarci un agente vero.
---

`latet tutorial` ti mostra Latet al lavoro nel tuo browser, in circa 30 minuti, prima di collegarci
un agente vero. Un agente simulato invia le sue richieste a un modello simulato. Latet, tra i due, è
quello vero: lo stesso mascheramento, gli stessi controlli e la stessa dashboard che hai con il tuo
agente. I dati sono inventati, e niente esce dal tuo computer.

## Avvialo

```sh
latet tutorial
```

Apre il tutorial nel tuo browser, sul menu dei capitoli, e resta in esecuzione finché non lo
termini. Il terminale dice dove si trova:

```text
Tutorial at http://127.0.0.1:52817 — Ctrl+C ends it
```

| Comando | Cosa fa |
| --- | --- |
| `latet tutorial` | Apre il menu dei capitoli. |
| `latet tutorial terms` | Apre un capitolo, dal suo id: qui, *I tuoi termini*. |
| `latet tutorial --list` | Stampa i capitoli, la loro durata in minuti e quelli che hai completato, poi si ferma. |
| `latet tutorial --print-url` | Stampa il link di accesso invece di aprire il browser. Funziona una volta sola, per due minuti. |
| `latet tutorial --keep` | Conserva la cartella della sandbox quando il tutorial finisce, e dice dove si trova. |

Il tutorial gira sul tuo computer, non nel [container](../docker/) di Latet.

## I capitoli

Ogni capitolo si compone di pochi passaggi. Invii all'agente un prompt, già scritto per te, e il
riquadro di Latet mostra cosa è successo alla sua richiesta, nelle viste della dashboard stessa.
Alcuni passaggi ti chiedono di agire lì: fare clic su un valore, aggiungere una parola ai tuoi
termini, approvare una richiesta, salvare le tue indicazioni.

| Capitolo | Minuti | Cosa succede |
| --- | --- | --- |
| `basics`: Le basi | 5 | Una chiave in `.env`, un indirizzo email e un numero di telefono escono come valori fittizi; il file che l'agente scrive contiene la chiave reale. |
| `terms`: I tuoi termini | 3 | Un nome in codice esce in chiaro finché non lo aggiungi ai tuoi termini; da lì in poi il modello riceve un valore fittizio, un nome che non ha mai visto. |
| `false-positives`: Un valore mascherato senza motivo | 3 | La carta di test di Stripe viene mascherata come la carta di un cliente; *Non sensibile* la lascia passare. |
| `stopped`: Una richiesta fermata | 3 | Una chiave incollata in una conversazione di prima che Latet fosse attivo ferma la richiesta; una nuova conversazione senza la chiave passa. |
| `live`: Il Revisore Live | 3 | Una richiesta attende la tua approvazione: mascheri a mano il nome di un cliente, poi la approvi. |
| `settings`: Un agente che allenta le tue regole | 3 | L'agente chiede a Latet di lasciar passare gli indirizzi email, e la modifica ti aspetta: approvala o annullala. |
| `data`: Dati di produzione | 4 | La modalità dati rigorosa maschera ogni valore della tabella di una query; tu lasci in chiaro id, date e piani. |
| `llm`: Il revisore locale | 4 | Un ticket contiene un indirizzo email scritto a parole, un IBAN con un errore di battitura, un nome e una diagnosi: nessun pattern li trova, il revisore locale sì. Poi le tue indicazioni gli fanno trovare anche i reparti dell'ospedale. Il revisore è simulato: è il Tutorial Reviewer. |
| `images`: Screenshot | 3 | L'agente invia uno screenshot di un pagamento rifiutato: Latet vi legge l'indirizzo email, l'IBAN e il numero di telefono, e copre ognuno con il suo valore fittizio prima che l'immagine esca. Il capitolo richiede il riconoscimento del testo: `latet ocr install`. |

Un capitolo finisce con ciò che hai imparato e dove si trova nella dashboard. L'ultimo finisce
spiegando come usare Latet con il tuo agente. Il menu segna i capitoli che hai completato, da
un'esecuzione all'altra, e *Ricomincia* riporta un capitolo com'era all'inizio.

## Una sandbox

I capitoli mascherano valori, fermano richieste e cambiano impostazioni, quindi girano su un Latet
tutto loro, in una sandbox:

- **Dati, impostazioni e cronologia propri**, in una cartella temporanea, e un proprio server su
  `127.0.0.1`, su una porta libera. Il tuo daemon non viene toccato, e non serve che sia in
  esecuzione.
- **Niente di tuo cambia:** le tue impostazioni, la tua cassaforte e la cronologia, e l'accesso alla
  tua dashboard (la pagina del tutorial accede con un cookie tutto suo).
- **Niente esce dal tuo computer.** L'unico percorso della sandbox porta al modello simulato, che
  gira dentro il comando `latet tutorial`: nessuna richiesta può raggiungere un provider, e non
  servono né una chiave API né un accesso.
- **Gli stessi valori fittizi a ogni esecuzione:** la sandbox li crea da una chiave fissa, non dalla
  tua.
- **Un solo file fuori dalla sandbox:** `tutorial.json` nella tua
  [cartella dei dati](../uninstall/#3-elimina-i-dati-locali-facoltativo), con gli id dei capitoli che hai
  completato.

La sandbox non ha il rilevamento dei nomi né niente della tua identità: i capitoli mostrano ciò che
trovano i rilevatori, e ciò che mascheri a mano. Nel capitolo sul revisore locale, il revisore è il
Tutorial Reviewer: simulato, trova ciò che dice il capitolo, e nessuna richiesta raggiunge un server.
Nel capitolo sugli screenshot, la sandbox legge il testo nelle immagini con i tuoi modelli di
riconoscimento del testo, una volta installati (`latet ocr install`). Ogni altra pagina della sua
dashboard è lì da esplorare, con una barra in cima che ti riporta al capitolo.

## Terminalo

Fai clic su *Termina il tutorial* nella pagina, o premi Ctrl+C nel terminale: la sandbox si ferma, e
la sua cartella viene eliminata. Un'esecuzione interrotta in un altro modo, per esempio chiudendo il
suo terminale, lascia la cartella nella tua cartella temporanea, e il successivo `latet tutorial` la
elimina. Un'esecuzione che si ferma per un errore stampa le ultime righe del suo log, e conserva la
cartella, con il log completo, fino all'esecuzione successiva.

## Nella tua dashboard

Ciò che mostrano i capitoli, nella dashboard del tuo Latet (`latet dashboard`):

| Cosa | Dove |
| --- | --- |
| Ogni richiesta: il testo del tuo computer, e ciò che ha ricevuto il modello | **Live**, poi la richiesta |
| Ogni valore sostituito da Latet, e il suo valore fittizio | **Cassaforte** |
| I tuoi termini | **Regole** → **Termini** |
| I valori che lasci passare | **Regole** → **Valori consentiti** |
| Le richieste che Latet ha fermato, e perché | **Live**, con il filtro *Bloccata*, poi la richiesta |
| Ciò che attende la tua approvazione: richieste, e modifiche alle tue impostazioni | **Approvazioni** |
| Il Revisore Live, per tutti i progetti o per uno | **Regole** → **Revisori** → **Revisore Live** |
| La modalità dati rigorosa, e le colonne che lasci in chiaro | **Regole** → **Protezione** → **Dati di produzione** |
| Ciò che ha trovato il revisore locale in una richiesta | **Live**, poi la richiesta: *Revisore locale* |
| Il revisore locale, e le tue indicazioni | **Regole** → **Revisori** → **Revisore locale** |
| Un'immagine contenuta in una richiesta, come è stata inviata, e il testo letto al suo interno | **Live**, poi la richiesta: *Immagini* |
| Ciò che esce con un'immagine o un PDF, e se il testo nelle immagini viene letto | **Regole** → **Protezione** → **Dati di produzione**: *Immagini e PDF* |

Poi provalo con il tuo agente, per una sessione, con una chiave inventata: vedi
l'[Avvio rapido](../quickstart/).
