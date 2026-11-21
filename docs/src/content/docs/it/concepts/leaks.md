---
title: Segreti nelle sessioni salvate
description: latet leak legge le sessioni che i tuoi agenti hanno salvato su questo computer, elenca i segreti che contengono senza mostrare alcun valore e dice quali possono essere arrivati al provider del modello.
---

I tuoi agenti di programmazione tengono ogni conversazione sul disco, per intero: i prompt che hai
scritto, i file che hanno letto, l'output dei comandi che hanno eseguito. Un segreto entrato in una
conversazione prima che installassi Latet, o in una sessione che non è passata da Latet, è in quei
file. La [dashboard](../../dashboard/) non può mostrarlo: vede solo le richieste passate da Latet, e
solo finché la sua cronologia le conserva (14 giorni per impostazione predefinita). `latet leak`
legge le sessioni salvate con i rilevatori di Latet ed elenca i segreti che contengono, così sai
quali chiavi cambiare e quali conversazioni non riprendere.

```sh
latet leak
```

Il comando legge e stampa, e non mostra mai un valore. Non modifica nessuno dei tuoi file, non ha
bisogno della rete né di un daemon in esecuzione, e la cassaforte di Latet non ne impara nulla: i
valori che trova restano nella sua memoria mentre legge, e non oltre. Dato che non stampa alcun
valore, può eseguirlo anche un agente.

## Dove i tuoi agenti salvano le sessioni

`~` è la tua cartella utente (`C:\Users\you` su Windows). Se la variabile di un agente è impostata,
le sue sessioni sono nella cartella che indica, e `latet leak` le legge lì.

| Agente | Le sue sessioni salvate | La variabile che le sposta |
| --- | --- | --- |
| [Claude Code](../../agents/claude-code/) | `~/.claude/projects/`: un file `.jsonl` per sessione e per subagente | `CLAUDE_CONFIG_DIR`, al posto di `~/.claude` |
| [Codex](../../agents/codex/) | `~/.codex/sessions/` e `~/.codex/archived_sessions/` | `CODEX_HOME`, al posto di `~/.codex` |
| [OpenCode](../../agents/opencode/) | `~/.local/share/opencode/opencode.db`, e la vecchia cartella `storage` accanto | `XDG_DATA_HOME`, al posto di `~/.local/share` |
| [Copilot CLI](../../agents/other-tools/#github-copilot-cli) | `~/.copilot/session-state/`: un file `events.jsonl` per sessione | `COPILOT_HOME`, al posto di `~/.copilot` |
| [Gemini CLI](../../agents/gemini/) | `~/.gemini/tmp/`: la cartella `chats` di ogni progetto | `GEMINI_CLI_HOME`, al posto di `~` |
| [Qwen Code](../../agents/other-tools/#qwen-code) | `~/.qwen/projects/`: la cartella `chats` di ogni progetto; e la vecchia `~/.qwen/tmp/` | `QWEN_HOME`, al posto di `~/.qwen` |
| [Hermes Agent](../../agents/autonomous-agents/#hermes-agent) | `state.db` in `~/.hermes` e, su Windows, in `%LOCALAPPDATA%\hermes` | `HERMES_HOME`, la cartella che contiene `state.db` |

`--agent` legge solo gli agenti che nomini, con questi nomi: `claude`, `codex`, `opencode`,
`copilot`, `gemini`, `qwen` e `hermes`; ripetilo per leggerne altri. `--path` legge in più un file,
o i file `.json`, `.jsonl` e `.ndjson` di una cartella e delle sue sottocartelle, allo stesso modo:
per esempio le sessioni di un agente che questa tabella non elenca, o una cartella di sessioni
copiata da un altro computer.

**Cosa legge.** In ogni record, ogni testo: i tuoi prompt, le risposte del modello, le chiamate
degli strumenti e i loro risultati, anche un testo che è a sua volta JSON. Non gli id, gli orari e
le firme, che non contengono nulla della conversazione. Ogni testo viene letto dai tuoi rilevatori
con le tue impostazioni globali, come Latet legge l'output di uno strumento: le regole integrate, i
tuoi termini e i tuoi riconoscitori, ogni tipo con il suo criterio. Un valore che le tue
impostazioni lasciano passare non viene elencato. Il modello locale dei nomi e il revisore locale
non vengono usati, e il `latet.yaml` di un progetto non viene letto.

Per impostazione predefinita cerca solo i segreti: chiavi, token, password, chiavi private,
credenziali in un indirizzo web. `--personal` conta in più i dati personali, come indirizzi e-mail e
numeri di telefono, per tipo: quante sessioni contengono ogni tipo, mai un valore.

## Cosa stampa

Un esempio, con cartelle e numeri inventati:

```text
$ latet leak
Latet read 242 Claude Code sessions, 38 Codex sessions and 5 Copilot CLI sessions.

2 secrets in clear (no sign that Latet replaced them: they may have reached the provider):
  By type: 1 Github pat and 1 Password.
  Github pat  ghp_… (40 characters)  Claude Code, 4 sessions in C:\Users\you\dev\shop
      2026-09-02 to 2026-09-09
  Password (17 characters)  Codex, 1 session in C:\Users\you\dev\billing
      2026-09-20
Latet can tell only from 2026-10-10 on, the earliest date its vault and history keep on this computer: an emptied vault, or a history past the days it keeps (14 by default), keeps nothing older. A session listed in clear from before then may have gone through Latet all the same.

2 secrets Latet replaced, or may have (its fake left instead where the session went through Latet):
  By type: 1 Anthropic api key and 1 Github pat.
  Anthropic api key  sk-ant-api03-… (108 characters)  Claude Code and Codex, 6 sessions in C:\Users\you\dev\billing, C:\Users\you\dev\shop
      2026-10-12 to 2026-11-14; 5 confirmed by Latet's history, 1 if the session went through Latet
  Github pat  ghp_… (40 characters)  Claude Code, 2 sessions in C:\Users\you\dev\shop
      2026-10-12 to 2026-11-14; 1 confirmed by Latet's history, 1 if the session went through Latet

Rotate each secret in clear at its vendor: what reached a model provider may stay in its logs. Do not resume those sessions (a resumed conversation sends them again): start new ones. The secrets Latet replaced need nothing where the session went through Latet.
```

Lo stesso segreto incollato in più sessioni sta su una riga, con il numero di sessioni. Così anche
un segreto che i rilevatori hanno trovato come due tipi, per esempio un token GitHub nell'header di
un comando `curl` e assegnato a una variabile: la riga mostra il tipo del fornitore. Un segreto può
stare in due elenchi, come il token GitHub qui sopra: incollato prima che ci fosse Latet, e di nuovo
dopo.

### In chiaro

Niente indica che Latet li abbia sostituiti: possono essere arrivati al provider del modello (*no
sign that Latet replaced them: they may have reached the provider*). Una sessione conta qui quando
conteneva il valore prima che la cassaforte di Latet lo conservasse per la prima volta, o quando la
cassaforte non conosce affatto il valore. Contano qui anche altri due casi: una sessione i cui
record non hanno un'ora, che non si può collocare né prima né dopo quella data, e una sessione che,
secondo la cronologia di Latet, ha inviato prima il valore in chiaro: la cronologia conserva ancora
le prime richieste della sessione, e mostra il valore mascherato solo in richieste successive. Sono
questi i segreti da cambiare. Sotto l'elenco, il comando dice da quale data Latet può saperlo: la
data più vecchia che la sua cassaforte e la sua cronologia conservano su questo computer.

### Sostituiti da Latet

La cassaforte conosceva già il valore quando la sessione lo conteneva: dove la sessione è passata da
Latet, il provider del modello ha quindi ricevuto il valore fittizio di Latet. Il valore è comunque
nel file della sessione, perché l'agente salva quello che aveva prima che Latet lo sostituisse in
uscita. Non c'è niente da cambiare per via del provider, anche se il valore si trova in un file su
questo computer.

Ogni segreto dice quante delle sue sessioni sono *confirmed by Latet's history*, confermate dalla
cronologia di Latet: la cronologia conserva ancora una richiesta della sessione in cui il valore è
stato mascherato. Per questo serve una cronologia conservata per intero (*Tutto*, l'impostazione
predefinita) che contenga ancora la sessione (14 giorni per impostazione predefinita), e un agente
che nomini la sua sessione nelle richieste, come fanno Claude Code, Codex e Hermes Agent. Ogni altra
sessione conta *if the session went through Latet*, solo se è passata da Latet: un agente che allora
non era configurato per Latet ha inviato il valore in chiaro, e solo la cronologia può dirlo. Quando
un segreto ha una sessione così, l'intestazione dice *Latet replaced, or may have*.

### Oscurati

I segreti che le tue impostazioni oscurano, o che Latet oscura perché troppo lunghi da conservare
(*your settings redact, or Latet redacts as too long to keep*): un segreto di un tipo che i tuoi
[criteri](../profiles/#criteri) impostano su `redact`, o uno troppo lungo per la cassaforte (più di
16.384 caratteri). Latet sostituisce un valore così con un marcatore e non lo conserva da nessuna
parte, quindi la sua cassaforte non ha una data per lui. Le sessioni dalla data più vecchia che Latet
ha su questo computer sono elencate qui, quelle precedenti in chiaro. Le tue impostazioni allora
possono essere state diverse, e la sessione può non essere passata da Latet: per questo
l'intestazione dice che Latet può aver inviato un marcatore al suo posto (*may have sent a marker*).

### Ogni elenco

Sotto l'intestazione, un elenco conta i suoi segreti per tipo: i sei tipi con più segreti, poi gli
altri insieme. Poi vengono i segreti, prima le chiavi di un fornitore (un token GitHub, una chiave
Anthropic, una chiave AWS: quelle da cambiare per prime), poi quelli presenti in più sessioni. Un
elenco mostra i primi 20 e dice quanti altri ne ha; `--all` li elenca tutti.

Un segreto mostra il suo tipo, un indizio e, per le sessioni di quell'elenco, gli agenti, il numero
di sessioni, le cartelle di progetto (al massimo tre, poi quante altre) e le date in cui è comparso
la prima e l'ultima volta, secondo l'orologio di questo computer, come `--since` legge una data.
L'indizio tiene solo il prefisso pubblico che il fornitore mette davanti a ogni chiave del suo tipo
(`ghp_…`, `sk-ant-api03-…`, `AKIA…`), lo stesso che tiene un valore fittizio, e la lunghezza; un
segreto senza un prefisso così mostra il suo tipo e la sua lunghezza (`Password (17 characters)`).

In fondo vengono i dati personali con `--personal`, cosa fare, e ciò che non è stato possibile
leggere, con il motivo, mai il contenuto: un file che non si apre, un file JSON di più di 32 MB, un
singolo testo di più di 8 MB, un database che è cambiato a ogni copia.

**Senza le risposte di Latet.** Su un computer collegato a un [server Latet](../server/), la
cassaforte e la cronologia sono quelle del server. Il comando dice allora che non ha potuto sapere
quali segreti Latet ha sostituito, ed elenca ogni segreto in chiaro, tranne quelli che elenca come
oscurati. Fa lo stesso, e dice perché, quando non è stato possibile leggere la cassaforte di Latet
su questo computer, o la sua chiave, o nessuna chiave trovata qui la apre. Su un computer senza
cassaforte di Latet né server Latet, Latet non ha sostituito niente: il comando lo dice, ed elenca
ogni segreto in chiaro, anche quelli dei tipi oscurati.

## Opzioni

| Opzione | Cosa fa |
| --- | --- |
| `--agent NAME` | Leggere solo le sessioni di questo agente: `claude`, `codex`, `opencode`, `copilot`, `gemini`, `qwen` o `hermes`. Ripetila per leggerne altri. |
| `--since DATE` | Leggere i record da questa data in poi (`2026-10-01`), in base all'ora di ogni record, altrimenti all'ultima modifica del suo file. |
| `--personal` | Contare in più i dati personali, per tipo: quante sessioni contengono ogni tipo. |
| `--path PATH` | Leggere in più questo file, o i file JSON e JSON Lines di questa cartella. Ripetila per leggerne altri. |
| `--json` | Stampare un documento JSON invece del testo, con ogni segreto. |
| `--all` | Elencare ogni segreto di ogni elenco, non solo i primi 20. |

`--json` dà un record per ogni segreto: il suo `type` (quello che mostra il testo), i suoi `types`
(ogni tipo con cui è stato trovato), la sua `class`, il suo `hint`, i suoi `agents` e `folders`, e
le sue sessioni `in_clear`, `replaced` e `redacted`, ognuna con quante sono, la prima e l'ultima
data, quante sono confermate, e gli `agents` e i `folders` di quelle sessioni. Accanto: le sessioni
lette per agente, i dati personali per tipo, ciò che non è stato possibile leggere (`skipped`) e
quante cose sono (`unread`), `latet_answers` (se è stato possibile interrogare la cassaforte e la
cronologia di Latet) e `latet_since` (la data più vecchia che conservano, data quando un segreto è
in chiaro).

Il codice di uscita è `0` quando non trova niente, `1` quando trova un segreto, in qualsiasi elenco,
o dati personali con `--personal`, e `2` quando non ha potuto leggere nessuna sessione o un'opzione
è sbagliata. Uno script può eseguirlo e agire di conseguenza.

## Quanto ci mette

`latet leak` legge ogni testo di ogni sessione salvata, e questo richiede tempo quando sono tante:
circa 11 minuti per 10 GB di sessioni su un PC desktop. In una console mostra su una riga l'agente
che sta leggendo e quante delle sue sessioni ha già letto, poi quanto ci ha messo; dopo un minuto,
dice che `--agent` e `--since` leggono meno:

```sh
latet leak --agent claude --since 2026-10-01
```

Una sessione viene giudicata sui record letti: con `--since`, un segreto che un record precedente
della stessa sessione ha inviato in chiaro può essere elencato come sostituito.

Queste righe vanno sullo standard error, e solo verso una console: sullo standard output va solo il
rapporto, e con `--json` lì resta un unico documento JSON.

## Cosa non può sapere

- **Prima della data di Latet.** Latet conosce un valore dal giorno in cui la sua cassaforte l'ha
  conservato per la prima volta, e una sessione che conteneva il valore prima di quel giorno viene
  elencata in chiaro. Una cassaforte svuotata, un valore tolto con *Dimentica* (la cassaforte lo
  conosce di nuovo da quando ricompare) o una cronologia oltre i giorni che conserva non tengono
  nulla di più vecchio: una sessione elencata in chiaro, anteriore alla data sotto l'elenco, può
  essere passata da Latet lo stesso.
- **Una sessione che non è passata da Latet.** Un segreto elencato come sostituito, in una sessione
  che la cronologia non conferma, può essere uscito senza passare da Latet: solo la cronologia può
  dirlo, per i giorni che conserva.
- **Ciò che Claude Code tiene per il proprio schermo.** Claude Code salva una seconda copia del
  risultato di uno strumento per mostrarlo lui stesso (`toolUseResult`): un file che ha letto, il
  file intero di una modifica, l'output di un comando. Quella copia non arriva mai al provider del
  modello, e `latet leak` non la legge. Legge invece la copia che la richiesta portava.
- **Un segreto diviso in due.** Ogni testo viene letto da solo: un segreto diviso fra due record non
  viene trovato, e un valore che un testo fa riconoscere dalla parola che lo precede (`password:`)
  non viene trovato dove un altro testo lo scrive da solo.
- **Ciò che i rilevatori non riconoscono,** come nel mascheramento di Latet
  ([Non protetto](../protection-scope/#non-protetto)). Non viene trovato nemmeno un valore che Latet
  conosce solo dalla sua cassaforte, perché l'ha trovato il revisore locale o l'hai trovato tu:
  l'analisi parte da una cassaforte tutta sua, vuota.

Un database in modalità write-ahead, accanto al quale una semplice lettura aggiungerebbe dei file,
viene letto da una copia privata in una cartella temporanea, cancellata dopo; la copia viene fatta
solo dove sul disco resta almeno 1 GB libero dopo di essa. Vale per i database di OpenCode e di
Hermes Agent, e per la cassaforte e la cronologia di Latet stesso.

## Cosa fare con un segreto trovato

- **Cambia ogni segreto in chiaro presso il fornitore della chiave,** prima le chiavi di un
  fornitore, nell'ordine dell'elenco: crea una chiave nuova dove il fornitore le rilascia (GitHub,
  Anthropic, AWS...), usala al posto della vecchia e revoca la vecchia; cambia una password. Quello
  che è arrivato a un provider di modelli può restare nei suoi log, qualunque cosa cancelli sul tuo
  computer. Il tipo, il prefisso, le cartelle e le date ti dicono di quale chiave si tratta e dove è
  stata usata.
- **Non riprendere quelle conversazioni.** Una conversazione ripresa invia di nuovo tutti i suoi
  messaggi, e il segreto con essi: iniziane una nuova.
- **I segreti che Latet ha sostituito** non richiedono nulla presso il fornitore della chiave dove la
  sessione è passata da Latet: il provider del modello ha ricevuto un valore fittizio. **Quelli
  oscurati forse non richiedono nulla:** se la sessione è passata da Latet, il provider del modello
  ha ricevuto un marcatore al loro posto.
- **I file sul disco sono del tuo agente.** Latet non ne cancella né ne sposta nessuno. Per togliere
  un valore da questo computer, cancella o sposta i file di sessione che lo contengono, ad agente
  chiuso: l'agente, la cartella del progetto e le date dell'elenco ti dicono dove cercare. Una
  conversazione di cui cancelli il file non si può più riprendere.

Un segreto che hai cambiato resta nell'elenco finché i suoi file di sessione sono lì: l'elenco dice
cosa contengono i file, non se una chiave è ancora valida.
