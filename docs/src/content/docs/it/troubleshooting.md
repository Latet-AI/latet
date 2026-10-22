---
title: Risoluzione dei problemi e FAQ
description: Problemi comuni e domande frequenti.
---

## Inizia da qui

La maggior parte dei problemi emerge con un solo comando:

```sh
latet doctor
```

Controlla il daemon, la connessione del plugin al daemon, l'avvio all'accesso, la porta, un
autotest del mascheramento, e ogni agente che trova. Ogni riga `WARN` o `FAIL` indica il comando
che risolve il problema.

Quando gira in background, il daemon scrive il suo log in `logs/daemon.log` nella cartella dei dati:
`%LOCALAPPDATA%\latet` su Windows, `~/Library/Application Support/latet` su macOS,
`~/.local/share/latet` su Linux. Per seguirlo invece in tempo reale, ferma il daemon ed esegui
`latet serve`, che scrive il log nel terminale.

## Risoluzione dei problemi

### Il mio agente dice che non riesce a connettersi

L'agente è configurato per usare Latet, ma il daemon non è in esecuzione. Controllalo, e avvialo:

```sh
latet status
latet start
```

`latet status` stampa `running: Latet 0.9.0 on 127.0.0.1:5283 (masking), pid 12345` se il daemon
è in esecuzione, altrimenti `not running`. Se invece vuoi smettere di usare Latet, esegui prima
`latet global-uninstall`: riporta i tuoi agenti com'erano. Vedi
[Disinstallazione](../getting-started/uninstall/).

### Claude Code dice che Latet ha trattenuto del contenuto

Messaggi come questi significano che il plugin non ha ricevuto una risposta dal daemon, o che il
daemon non è riuscito a elaborare il contenuto, quindi il plugin l'ha trattenuto invece di inviarlo
senza mascherarlo:

- `Latet could not mask this prompt: the local daemon did not answer, or failed on it.`
- `[withheld by Latet: the local daemon did not answer, or failed on it, so this content was not sent]`
- `Latet could not check this request (the local daemon did not answer, or failed on it), so it was not sent.`

Esegui `latet start`, poi `latet doctor`. Se il daemon è in esecuzione, il suo log (`daemon.log`
nella cartella `logs` della cartella dei dati) dice cosa non ha funzionato: apri una issue
allegandolo. Il plugin avvia il daemon da solo con il comando `latet`; quando esegui Latet da un
checkout dei sorgenti quel comando non è nel tuo `PATH`, quindi avvialo con `uv run latet start`.

### Latet rifiuta la sua cartella dei dati

Il messaggio nomina la cartella, chi altro può cambiarla e cosa fare, come in:

```text
Integration records unavailable (unsafe folder namespace): C:\Users\you lets PC\other (S-1-5-21-…)
change what is in it (its access list grants full control, (F), mask 0x001f01ff; `icacls "C:\Users\you"`
lists it): remove that entry (`icacls "C:\Users\you" /remove:g *S-1-5-21-…`), trust that account
(LATET_TRUSTED_ACCOUNTS), or use a data folder whose every parent only you, SYSTEM, the Administrators
and the accounts in LATET_TRUSTED_ACCOUNTS can change (LATET_DATA_DIR); with a new data folder,
`latet agents apply` applies your agents again.
```

Latet tiene ciò che ha applicato ai tuoi agenti, e su un computer connesso a un server Latet la sua
connessione, in una cartella dei dati che nessun altro account del computer può cambiare: un altro
account potrebbe mettervi ciò che Latet poi applica o invia. La cartella nominata è la cartella dei
dati o una delle cartelle che la contengono.

- **La cartella dei dati stessa** viene rifiutata ogni volta che un altro account può cambiarla, e
  così una cartella superiore che appartiene a un altro account.
- **Una cartella superiore che un altro account può cambiare** viene rifiutata su un computer connesso
  a un server Latet, dove è in gioco il token del server. Su un computer non connesso Latet va avanti
  e `latet doctor` lo segnala, con le stesse parole: un account che può cambiare il tuo profilo può già
  cambiare tutto ciò che è tuo al suo interno, compresi i dati di Latet.

Su Windows, `icacls "<cartella>"` elenca chi può cambiare la cartella: una voce per un altro account
con `(F)`, `(M)` o `(W)` glielo permette. Le soluzioni sono tre:

1. Rimuovi quella voce, se il computer lo puoi cambiare tu. Una voce segnata `(I)` viene dalla
   cartella superiore, e si rimuove lì; una voce che compare solo come SID è un account che Windows non
   trova più, spesso uno eliminato.
2. Fidati di quell'account: imposta `LATET_TRUSTED_ACCOUNTS` sul suo nome o sul suo SID per il tuo
   account, come in `setx LATET_TRUSTED_ACCOUNTS "PC\other"`, o nelle impostazioni di sistema, o per
   tutto il computer con i criteri di gruppo, poi apri un nuovo terminale. Latet la legge lì, nel
   registro, e mai dall'ambiente di un terminale: un valore impostato solo in una sessione di terminale
   (`$env:` o `set`) non conta, così che le impostazioni di un repository o la sessione di un agente
   non possano cambiarla. Più account si separano con `;`, e un SID si può scrivere `*S-1-5-…`, come
   lo scrive icacls. Vale solo per le cartelle superiori, mai per la cartella dei dati stessa, che
   contiene la chiave del vault, né per una cartella al suo interno.
3. Imposta `LATET_DATA_DIR` su una cartella le cui cartelle superiori possono cambiare solo tu, SYSTEM,
   gli Administrators e gli account di cui ti fidi, poi esegui di nuovo `latet agents apply`.

Un membro diretto del gruppo locale Administrators, come un gruppo dell'assistenza aggiunto dal tuo
reparto IT, è considerato affidabile sulle cartelle superiori come il gruppo stesso, senza alcuna
impostazione. La fiducia in questi account e membri vale per le cartelle superiori di ogni cartella
che Latet controlla così: quelle della cartella dei dati, quelle sopra la cartella dei dati che
controlla il processo ausiliario di Claude Code, quelle della sua installazione e quelle della
cartella dei log del servizio Windows di un server Latet. `latet doctor` elenca gli account di cui si
fida `LATET_TRUSTED_ACCOUNTS`, con il punto in cui è impostata, e i membri di cui si è fidato. Nomina
ogni voce che non rende affidabile nessuno: un nome che Windows non trova, o un account che non è di
questo computer, di un dominio, di Entra ID o di un servizio, come Everyone, Users o NTLM
Authentication. Dice anche quando la variabile è impostata solo nel suo terminale.

Su Linux e macOS, `ls -ld <cartella>` mostra il proprietario e i permessi. Una cartella in cui altri
possono scrivere viene rifiutata: `chmod go-w <cartella>`, o `chmod 700` per la cartella dei dati
stessa. Una cartella dei dati che altri possono solo leggere (0755, come la crea `mkdir`) diventa
solo tua (0700) la prima volta che serve a Latet, e Latet lo dice una volta: nel log del daemon, o nel
terminale del comando che ha fatto la modifica. `LATET_TRUSTED_ACCOUNTS` lì non cambia nulla: i
controlli si basano su proprietario e permessi.

Un computer che non si è mai connesso a un server Latet non viene mai rifiutato per una connessione.

### Claude Code su un computer connesso trattiene ogni prompt

Il plugin dice perché, e ogni causa ha la sua soluzione:

- **La modalità di stampa o un'altra superficie:** `claude -p`, e ogni superficie tranne il
  terminale interattivo, sono trattenuti su un computer connesso a un server Latet. Usa il terminale
  interattivo.
- **L'associazione del processo ausiliario manca o non è aggiornata**, dopo un aggiornamento o una
  nuova connessione: esegui `latet agents apply claude`, o avvia Claude Code con `latet claude`.
  `latet doctor` la controlla.
- **Il server Latet non ha risposto:** `latet status` dice se risponde e se accetta il token di
  questo computer.
- **Il server non accetta più il token di questo computer:** è stato revocato, o la tua identità è
  disattivata. Esegui `latet disconnect`, poi `latet connect <url>` con un nuovo codice di un
  amministratore.
- **Linux e macOS:** Claude Code tramite un server richiede un terminale Windows in questa versione;
  `latet codex` e `latet opencode` passano dal server.

`latet agents apply claude` e `latet claude` possono rifiutare con `Claude Code's helper binding is
unavailable`. Il messaggio nomina la parte dell'installazione che non va (Python, il suo ambiente
`sys.prefix` o la sua installazione `sys.base_prefix`, il pacchetto di Latet, o la cartella di
Windows), la cartella e il motivo: il suo proprietario, o un account a cui il suo elenco di accesso
permette di cambiarla. Il processo ausiliario esegue quel codice con il token di questo computer,
quindi lo esegue solo da cartelle che possono cambiare solo tu, SYSTEM e gli Administrators, e le cui
cartelle superiori possono cambiare solo loro, i membri diretti degli Administrators e gli account in
`LATET_TRUSTED_ACCOUNTS`. Controlla la cartella nominata con `icacls "<cartella>"` e rimuovi la voce dell'altro account;
oppure installa Python e Latet in cartelle tue, che `UV_PYTHON_INSTALL_DIR` e `UV_TOOL_DIR` di uv
scelgono.

### Una richiesta è stata bloccata

In Claude Code, il controllo finale del plugin risponde nella conversazione:

```text
Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
sent in clear. Nothing was sent.
```

Il messaggio indica il tipo di valore che sarebbe uscito in chiaro e la parte della conversazione in
cui si trovava; non è stato inviato nulla. Un blocco del genere significa che parte del contenuto è
arrivata nella richiesta senza essere mascherata. Esegui `latet doctor`, e controlla con
`latet agents status` che Claude Code mostri `applied`; se non lo mostra, esegui
`latet agents apply claude` e riavvia Claude Code.

Quando invece è il daemon a bloccare una richiesta, l'agente mostra un errore con il messaggio
`Latet: sensitive values would leave in clear` (dei valori sensibili uscirebbero in chiaro), seguito
dal tipo e dalla lunghezza di ogni valore, come `PASSWORD[13]`. Esegui `latet dashboard`: la
richiesta bloccata è lì, come sarebbe uscita, con i valori che l'hanno fermata evidenziati da un
riquadro rosso. Per andare avanti, togli il valore: inizia una nuova conversazione (in Claude Code,
`/clear`) e indica all'agente il file o la variabile che lo contiene, invece di incollarlo. Solo se
un valore non è affatto un segreto, come un valore di test o di esempio, fai clic su di esso e
scegli **Non sensibile: consenti sempre**, poi riprova il turno.

In modalità gateway, un errore con `Latet: masking failed` o `Latet: unreadable request body`
significa che il daemon non è riuscito a mascherare la richiesta, quindi l'ha rifiutata.
Il log del daemon dice perché.

### La modalità auto dice che il suo revisore è temporaneamente non disponibile

In modalità auto, Claude Code chiede a un modello revisore se ogni azione è sicura. Quando Latet
ferma quella revisione, la modalità auto non può approvare l'azione: Claude Code la rifiuta, dicendo
che il revisore è temporaneamente non disponibile, e non ti chiede nulla. Esegui tu l'azione, passa
Claude Code a una modalità dei permessi che ti chieda conferma, o lascia passare i dettagli come
spiegato qui sotto. Nella dashboard queste richieste ricevono una risposta `400` e hanno la segnalazione
*Revisione della modalità auto*; il motivo dice cosa conteneva la revisione. Due cose la fermano:

- **La tua identità:** il tuo nome utente del sistema operativo e la tua cartella utente nei
  percorsi, per impostazione predefinita. Per lasciarli passare al revisore, e solo al revisore,
  imposta `auto_mode_review: {user_name: allow, paths: allow}` o usa *Revisioni della modalità auto
  di Claude Code* in **Regole** → **Avanzate** → **Identità** nella dashboard. Vedi
  [Claude Code](../agents/claude-code/#il-revisore-della-modalità-auto).
- **Un segreto o i dati di qualcun altro,** che non vanno mai al revisore. Spesso si trovano nel tuo
  CLAUDE.md: Claude Code dà al revisore quel file senza mascherarlo. Sposta quei valori in `.env` e
  nel file indica il nome della variabile. Quando inizia una sessione, il plugin indica il file e le
  righe, e `latet doctor` li elenca.

  **Se sono segnaposto,** scrivili in una [forma che Latet conosce](../concepts/detection/#segnaposto)
  e non tocca: `postgres://app:<password>@host/db`, `${DB_PASSWORD}`, `YOUR_PASSWORD`,
  `changeme` o `***`. Un segnaposto di un'altra forma va tra i valori del progetto da non mascherare
  mai (**Regole**, scegli il progetto, *Valori consentiti*), di cui tiene conto anche
  `latet doctor`, se eseguito nella cartella del progetto.

Tieni le richieste di permesso invece di passare alla modalità bypass permissions: nei comandi che
esegue, l'agente lavora con i valori reali.

### Ogni richiesta di una conversazione viene bloccata

In Claude Code, un messaggio come questo ferma ogni richiesta di una conversazione:

```text
Latet stopped this request before it left your machine: this conversation already held values in
clear before Latet checked it (PASSWORD (user text), EMAIL (tool result)). It began before Latet
was on, or was resumed from then, and every request sends that history again. Start a new
conversation with /clear: from there everything is masked as it comes in. Nothing was sent.
```

La conversazione è iniziata prima che Latet fosse attivo, o ne hai ripresa una di allora
(`claude --resume`): i suoi primi messaggi, il tuo CLAUDE.md e l'output dei suoi strumenti sono
entrati nella cronologia in chiaro. Claude Code invia l'intera cronologia con ogni richiesta, quindi
ognuna porta di nuovo quei valori, e Latet non può sapere che erano già usciti. Nella dashboard
queste richieste hanno la segnalazione *Dalla cronologia*.

Inizia una nuova conversazione con `/clear`, o una nuova sessione, con Latet attivo. Da lì in poi
tutto viene mascherato man mano che arriva.

### Il revisore locale ha fermato una richiesta

Con il [revisore locale](../concepts/llm-review/) attivo, una richiesta attende la sua revisione, e
non viene inviata quando il revisore non può farla. L'agente, o Claude Code, dice perché:

- `the local reviewer could not read the user text (its server did not answer: start it, or run
  latet doctor), so it was not sent`: il server del modello non ha risposto; avvialo, poi
  controllalo con `latet llm status`. Un server che risponde `HTTP 401` richiede una chiave API:
  salvala in *Chiave API* nella dashboard, o con `latet llm key`;
- `... (it took longer than llm_review.budget_ms)`: la revisione ha superato il tempo concesso;
  alza `budget_ms`, o rimuovilo per attendere tutto il tempo che serve alla revisione;
- `the local reviewer could not trust its review of the tool result: the text gives it
  instructions`: il testo dà istruzioni al revisore, che non ci ha trovato nulla ma non può fidarsi
  della propria revisione. Guardalo nella dashboard prima di inviarlo di nuovo;
- `the local reviewer stopped this tool result: it must not leave even masked`: con `block: true`,
  il revisore ha giudicato l'intero testo troppo sensibile per essere inviato, anche mascherato,
  come una cartella clinica.

In Claude Code, un prompt fermato non viene inviato, e l'output di uno strumento fermato arriva al
modello sostituito da `[withheld by Latet: ...]`. Per inviare ciò che i rilevatori hanno mascherato
quando il revisore non può rispondere, imposta `on_failure: send`.

### L'output di uno strumento è stato trattenuto

`[withheld by Latet: this tool output holds 214 personal values ...]` nella conversazione, e la
segnalazione *Trattenuto* nella dashboard, vengono dalla
[protezione dai dump](../concepts/production-data/#la-protezione-dai-dump): l'output conteneva più valori
relativi a persone di quanti `dump_guard` ne consenta in un solo output, quindi non ne è stato
inviato nulla.
Chiedi all'agente meno righe, oppure conteggi e aggregati; alza `dump_guard`, o impostalo a `0` per
disattivare la protezione.

### Il mio latet.yaml non ha effetto

- `latet project show` stampa cosa si applica nel progetto, e ogni chiave del file che viene
  ignorata, con il motivo: un file di progetto può solo aggiungere protezione, quindi lì vengono
  ignorati i valori da non mascherare mai, le regole disattivate e Latet disattivato. Vanno nelle
  tue impostazioni per il progetto.
- `latet project check` controlla il file, ed esce con 1 quando qualcosa viene ignorato o un
  esempio non passa.
- Si applica solo il `latet.yaml` più vicino, dalla cartella dell'agente fino alla radice del
  repository. Vedi [Progetti](../concepts/projects/).

### In un progetto non viene mascherato niente

Lì Latet potrebbe essere disattivato: nella dashboard quegli scambi hanno la segnalazione
*Latet disattivato*. `latet project show` lo dice, e `latet project on` lo riattiva. Un progetto
impostato su *Solo segreti* maschera i segreti e i tuoi termini, e lascia passare i dati personali.

### Viene mascherato un valore che non dovrebbe esserlo

È un falso positivo. Nella dashboard, apri la richiesta, fai clic sul valore e scegli
**Non sensibile: consenti sempre**, oppure aggiungi il valore a `allow_values` in `config.yaml`. Per
disattivare un'intera regola dei segreti, aggiungi il suo id a `disabled_secret_rules`: se nella
dashboard un valore è stato trovato da `gitleaks:generic-api-key`, la sua regola si disattiva con
`generic-api-key`. Vedi [Valori consentiti](../concepts/profiles/#valori-consentiti).

### Un valore non viene mascherato

- Controlla il [profilo](../concepts/profiles/): `secrets` non maschera i dati personali.
- I nomi nel testo libero richiedono il
  [modello locale dei nomi](../getting-started/installation/#rilevamento-dei-nomi) facoltativo o il
  [revisore locale](../concepts/llm-review/), e nessuno dei due dà garanzie: aggiungi i nomi che
  contano come [termini personalizzati](../concepts/detection/#i-tuoi-termini).
- Incolla il testo nel **Laboratorio** della dashboard per vedere cosa trovano le regole attuali.

### La risposta di `claude -p` mostra valori fittizi

È il comportamento previsto: in modalità print Claude Code non ripristina ciò che stampa. I file
scritti dagli strumenti contengono i valori reali, e anche l'interfaccia interattiva li mostra. Vedi
[Claude Code](../agents/claude-code/#limiti). Su un computer connesso a un server Latet la modalità di
stampa è trattenuta: vedi [Claude Code su un computer connesso trattiene ogni prompt](#claude-code-su-un-computer-connesso-trattiene-ogni-prompt).

### I comandi sono lenti su Windows

Ogni comando `latet` impiega alcuni secondi ad avviarsi, anche `latet version`, mentre i tuoi agenti
lavorano veloci come sempre. È il tuo antivirus. Controlla i programmi che non conosce ogni volta
che vengono eseguiti, e il piccolo launcher che un installer crea per il comando `latet` (sia quello
di uv sia quello di pip) è uno di questi: non è firmato, ed è diverso su ogni computer. Con Windows
Defender costa circa mezzo secondo; alcuni antivirus, per esempio Avira, impiegano diversi secondi
per ogni launcher. Latet in sé si avvia in una frazione di secondo, e i tuoi agenti parlano con il
daemon che è già in esecuzione, quindi l'attesa riguarda solo l'avvio di un comando.

Due rimedi:

- **Aggiungi Latet alle esclusioni del tuo antivirus.** Con un'installazione fatta con uv, le
  cartelle sono `%APPDATA%\uv\tools\latet` e `%USERPROFILE%\.local\bin` (nella seconda bastano
  `latet.exe` e `latet-daemon.exe`).
- **Oppure eseguilo tramite Python stesso,** che è firmato e parte subito: installa il wheel
  dell'[ultima release](https://github.com/Latet-AI/latet/releases/latest) con
  `py -m pip install "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"`
  (con la sua versione al posto di `X.Y.Z`) ed esegui ogni comando come `py -m latet`, per esempio
  `py -m latet claude`. Il `latet.exe` che crea pip è lento quanto gli altri, quindi lascialo da
  parte. Latet allora condivide quel Python con i tuoi altri programmi invece di averne uno suo. Per
  aggiornarlo, esegui lo stesso `py -m pip install` con il wheel della nuova release: `latet upgrade`
  installa con uv, come seconda copia. Per rimuoverlo, esegui `py -m latet global-uninstall`, poi
  `py -m pip uninstall latet`.

Con un antivirus lento, il plugin di Claude Code potrebbe non riuscire ad avviare il daemon da solo
in tempo, e nel frattempo trattiene i contenuti. Fai avviare il daemon all'accesso
(`latet global-install` se ne occupa), o esegui `latet start` prima di aprire Claude Code.

### La porta 5283 è già in uso

Latet è in ascolto su `127.0.0.1:5283`, una porta che nessun software comune usa. Se un altro
programma l'ha presa, o Windows l'ha riservata (Hyper-V, WSL e Docker riservano blocchi di porte che
possono cambiare a ogni avvio), `latet start` lo dice subito:

```text
Latet cannot listen on 127.0.0.1:5283: in use by another program.
Run `latet global-install` to move it to a free port (your agents are updated too).
```

`latet global-install` sceglie la prima porta libera da 5284 a 5335, la salva in `config.yaml` e fa
puntare alla nuova porta ogni agente che ha configurato. Se nessuna è libera, scegline una tu:
`latet global-install --port 6655`. `latet serve --port <port>` cambia la porta solo per una
singola esecuzione in primo piano.

### Latet gira in Docker e gli agenti non riescono a raggiungerlo

Esegui `docker exec latet latet status`, poi controlla che la porta sia pubblicata su `127.0.0.1` e
sia la stessa dentro e fuori (`LATET_PORT` le imposta entrambe in `compose.yaml`). Dopo un cambio di
porta, esegui di nuovo i comandi `agents merge` stampati da `docker exec latet latet agents print`.
Claude Code con `LATET_AUTOSTART=0` non avvia mai un daemon suo: aspetta il container. Dietro un
proxy, vedi [Docker](../getting-started/docker/#dietro-un-proxy).

### "browser requests are not accepted on proxy routes"

Hai aperto un URL del proxy in un browser, o una pagina web ha provato a raggiungerlo. È voluto: il
daemon rifiuta le richieste che arrivano dai browser. Gli agenti non ne risentono. Per guardare il
traffico, usa `latet dashboard`.

### "host not allowed"

La richiesta ha usato un nome host diverso da `127.0.0.1`, `localhost` o `[::1]`, e il daemon non lo
consente. Usa uno di questi nell'URL di base.

### Codex non passa da Latet

- Codex nelle versioni precedenti alla 0.115 ignora `openai_base_url`: aggiornalo. `latet doctor`
  lo segnala, e `latet agents apply codex` lascia com'è un Codex del genere. Fino ad allora, avvialo
  con `latet codex`, che gli passa l'indirizzo in `OPENAI_BASE_URL`, l'unico che legge. Con un
  provider suo (Azure, il gateway della tua azienda), `latet codex` non lo avvia, e dice di
  aggiornare Codex.
- Esegui `latet agents status`: Codex dovrebbe mostrare `applied`, e il suo percorso.
- Se hai cambiato modo di accesso, da ChatGPT a una chiave API o viceversa, o hai aggiunto un
  provider, esegui di nuovo `latet agents apply codex`.

### Claude Code dice che le sue richieste aggirano Latet

Il plugin ha trovato Claude Code impostato per inviare le sue richieste altrove, e ha fermato i suoi
prompt: niente controllerebbe ciò che esce. Il messaggio indica il file che l'ha impostato così.

- **Il `.claude/settings.json` di questo progetto** (o `.claude/settings.local.json`) imposta
  `ANTHROPIC_BASE_URL`, l'indirizzo di un cloud o l'opzione che lo attiva, o un proxy
  (`HTTPS_PROXY`): un repository non decide mai dove vanno le richieste, nemmeno tramite Latet.
  Sposta la riga nelle tue impostazioni ed esegui `latet agents apply claude`, o avvia Claude Code
  con `latet claude`, le cui impostazioni prevalgono sull'indirizzo del progetto. Un proxy impostato
  dal progetto ferma i prompt finché non viene rimosso da lì.
- **È attivo più di un cloud** (per esempio `CLAUDE_CODE_USE_BEDROCK` e `CLAUDE_CODE_USE_VERTEX`):
  tienine uno.
- **Nessun indirizzo**, o un cloud raggiunto direttamente (`CLAUDE_CODE_USE_BEDROCK` senza il suo
  indirizzo): avvialo con `latet claude`, o esegui una volta `latet agents apply claude` per tutte
  le sessioni.
- **Le impostazioni gestite della tua organizzazione** impongono un indirizzo: vedi
  [Endpoint aziendali](../agents/company-endpoints/#dove-latet-non-può-mettersi-davanti).

### Bedrock dice che il login AWS è scaduto

Latet firma le richieste a Bedrock con il tuo profilo AWS, e il login su cui si basa è scaduto.
Esegui il comando indicato nel messaggio (`aws sso login --profile work`), poi invia di nuovo: non
serve riavviare nulla. Senza la AWS CLI, Latet legge solo le chiavi scritte in `~/.aws`; un login
SSO richiede la AWS CLI, e `latet doctor` lo dice. Latet cerca `aws` nel proprio PATH, poi in
`/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`, `~/.local/bin` e `/snap/bin` (in
`C:\Program Files\Amazon\AWSCLIV2` su Windows). Se la tua AWS CLI si trova altrove, avvia Latet da
una shell in cui `aws` funziona.

### Un endpoint aziendale non è raggiungibile

La richiesta è arrivata a Latet, ma la destinazione a cui la inoltra non ha risposto. Controlla
l'indirizzo in Agenti protetti → Dove vanno le richieste nella dashboard, o con
`latet config get upstreams`; Claude Code mostra l'host della destinazione nel suo errore.

### Claude Desktop non passa dal daemon

È il comportamento previsto: l'app desktop ignora `ANTHROPIC_BASE_URL`. Le sessioni locali nella
sua scheda Code hanno il plugin e il suo controllo finale, ma non il controllo del daemon, e non
vengono registrate nella dashboard. Vedi [Claude Code](../agents/claude-code/).

### Remote Control non c'è più in Claude Code

È il comportamento previsto finché le richieste passano da Latet: Claude Code disattiva Remote
Control quando il suo URL di base è personalizzato. `latet agents revert claude` lo riporta, e
Claude Code non passa più da Latet.

### La dashboard dice che i suoi file non fanno parte di questa installazione

Stai eseguendo Latet da un checkout dei sorgenti. Compila la dashboard: `npm ci`, poi
`npm run build`, in `dashboard/`. Le release la includono. Anche la pagina del tutorial ne ha
bisogno: fino ad allora, `latet tutorial` avvisa che la sua pagina non verrà mostrata.

### Il tutorial non si apre, o chiede di essere avviato di nuovo

- **Non si è aperto nessun browser:** esegui `latet tutorial --print-url`, e apri nel browser che
  preferisci il link che stampa. Il link funziona una volta sola, per due minuti.
- **La pagina chiede di avviare di nuovo il tutorial:** il suo link è già stato usato, o sono passati
  più di due minuti. Premi Ctrl+C dove gira il tutorial, e avvialo di nuovo: i capitoli che hai
  finito restano completati.
- **Si è fermato per un errore:** ha stampato le ultime righe del suo log, e conserva il log completo
  nella cartella della sandbox finché non lo esegui di nuovo. Apri una issue allegandolo.

### Un aggiornamento non riesce su Windows perché dei file sono in uso

Usa `latet upgrade`: ferma il daemon prima di sostituirne i file, e lo riavvia dopo.

### Non si riesce a trovare l'ultima release

Gli installer e `latet upgrade` chiedono all'API di GitHub qual è l'ultima release, e ne installano
il wheel. Quando non ci riescono, dicono perché e non cambiano nulla:

- **L'API di GitHub ha risposto 403** (o 429): accetta 60 richieste all'ora da uno stesso
  indirizzo, e un ufficio dietro un unico indirizzo può esaurirle. In quel caso entrambi leggono la
  pagina dell'ultima release, che non viene conteggiata; se non riesce nemmeno questo, aspetta
  un'ora, o installa a mano (vedi sotto).
- **GitHub non è raggiungibile:** non c'è connessione, o c'è di mezzo un proxy.
- **L'ultima release non ha un wheel,** o non ce n'è nessuna: installa
  [dai sorgenti](../getting-started/installation/#esegui-dai-sorgenti).

Per installare una release a mano, prendi l'indirizzo del suo wheel dalla pagina delle
[release](https://github.com/Latet-AI/latet/releases), e passalo all'installer in `LATET_SOURCE`
(aggiorna anche un Latet già installato; poi esegui `latet start`, e `latet agents apply claude` se
Claude Code passa da Latet in modo permanente):

```sh
curl -LsSf https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.sh | LATET_SOURCE="latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl" sh
```

Su Windows (PowerShell):

```powershell
$env:LATET_SOURCE = "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
irm https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.ps1 | iex
```

Funziona anche un wheel che hai scaricato: il suo percorso al posto dell'indirizzo.

## FAQ

### Funziona con un abbonamento Claude Pro o Max?

Sì, senza chiave API: la modalità client serve proprio a questo. Claude Code gira senza modifiche,
accedi con il suo login, il plugin maschera al suo interno, e il daemon inoltra ogni richiesta byte
per byte. Vedi [Claude Code](../agents/claude-code/#abbonamenti-pro-e-max).

### Hermes Agent o OpenClaw possono usare il mio abbonamento Claude tramite Latet?

No. Anthropic consente di usare un abbonamento in Claude Code e nelle sue app, e Latet non lo presta
a nient'altro. Gli agenti autonomi passano dal gateway con una chiave API: una chiave OpenAI o
OpenRouter (OpenRouter offre anche i modelli Claude), o una chiave API di Anthropic. Vedi
[Hermes Agent, OpenClaw e altri agenti](../agents/autonomous-agents/).

### Perché Latet non riscrive semplicemente il traffico di Claude Code?

Claude Code aggiunge a ogni richiesta un blocco di attribuzione con un'impronta della conversazione,
e la guida di Anthropic sui gateway chiede a un gateway di ispezionare le richieste senza
modificarle. Per questo il mascheramento avviene dentro Claude Code, prima che la richiesta venga
costruita, e il daemon si limita a controllarla. Vedi
[Come funziona](../concepts/how-it-works/#modalità-client-claude-code).

### Il modello riesce ancora a fare il suo lavoro con valori fittizi?

Di solito sì. I valori fittizi mantengono il formato dell'originale, quindi il codice che gestisce
una chiave, un indirizzo email o un IBAN ha ancora senso per il modello. Solo che non conosce mai il
valore reale. Latet però lo rimette solo al posto del valore fittizio in sé: se il modello scrive
*Dear Laura* perché l'indirizzo fittizio inizia con `laura.`, quel saluto resta.

### Il mascheramento rompe la cache dei prompt del provider?

No. Lo stesso valore riceve sempre lo stesso valore fittizio, e i turni precedenti vengono inviati di
nuovo esattamente come prima, quindi il provider può ancora leggerli dalla sua cache. La pagina
**Statistiche** della dashboard mostra quanta parte di ogni prompt veniva dalla cache.

### Dove posso vedere cosa è stato davvero inviato?

Esegui `latet dashboard`. Vedi la [guida alla dashboard](../dashboard/).

### Come lo disattivo?

`latet global-uninstall` riporta ogni agente com'era, rimuove l'avvio all'accesso e ferma il daemon.
Gli strumenti che hai puntato su Latet nelle loro impostazioni ne tengono l'indirizzo: rimetti il
loro. Eseguire di nuovo `latet global-install` lo riattiva.

### Latet invia qualcosa da qualche parte?

Solo il traffico del tuo agente, verso il provider (o l'endpoint aziendale) che il tuo agente usa
già. Le eccezioni avvengono tutte quando lo chiedi tu: `latet ner install` e `latet ocr install`
scaricano i loro modelli da huggingface.co una volta; gli installer e `latet upgrade` chiedono a
GitHub qual è l'ultima release, e ne scaricano il wheel da lì; e il revisore locale viene
interrogato sul tuo server. Non c'è nessun servizio cloud di Latet, nessuna telemetria e nessuna
statistica di utilizzo.

### I miei dati sono al sicuro una volta installato Latet?

Più al sicuro, non del tutto al sicuro. Latet maschera solo ciò che i suoi rilevatori riconoscono, e
alcune superfici e alcuni tipi di file sono fuori dalla sua portata. Leggi
[Cosa è protetto e cosa no](../concepts/protection-scope/).

### E le immagini e i PDF?

Scarica i modelli di riconoscimento del testo con `latet ocr install` (un Latet installato senza
l'extra `ocr` su cui girano lo ottiene con `latet upgrade`): il testo negli screenshot e nelle
immagini viene allora letto sul tuo computer e mascherato, e ogni valore viene coperto sull'immagine
con il suo valore fittizio. Vedi [Screenshot e immagini](../concepts/images/).

I PDF e gli altri documenti non vengono letti: vengono inviati così come sono, e nella dashboard
quelle richieste hanno la segnalazione *Non ispezionato*. Evita di allegare file che contengono dati
sensibili, o imposta `images: block` (il profilo `max` fa lo stesso) così che una richiesta che ne
contiene uno non venga inviata.

### Un'immagine è stata trattenuta

Il modello ha ricevuto *[image withheld by Latet: ...]* al posto di un'immagine, con il motivo:
- *the text recognition models were still loading*: i modelli di riconoscimento del testo si
  stavano ancora caricando, perché il daemon era appena partito. Inviala di nuovo.
- *it could not be read*: non è stato possibile leggerla. Può essere un'immagine animata, una più
  grande di 20 MB o di 40 megapixel, o un file che non è un'immagine valida.
- *its text could not be read (OcrTooManyTiles)*: l'immagine è così lunga rispetto alla sua
  larghezza, o così larga rispetto alla sua altezza, che leggerla richiederebbe più di 16 parti, come
  lo screenshot di una pagina lunga più di circa 29.000 pixel. Cattura la pagina in più screenshot, o
  ritagliala su ciò che serve all'agente.
- *its text could not be read (OcrTooWide)*: una riga di testo è troppo lunga rispetto alla sua
  altezza per essere letta, come una riga che attraversa più schermate. Ritaglia l'immagine su ciò
  che serve all'agente.
- *its text could not be read (OcrTooLong)*: leggerla ha richiesto più tempo di quanto una lettura
  possa durare. Inviala di nuovo, o invia un'immagine più piccola.
- *you withheld it in the Live Reviewer*: l'hai trattenuta tu nel Revisore Live, spuntando
  *Non inviare questa immagine*.
- *the local reviewer saw a value in it ...*: il [revisore locale](../concepts/llm-review/#immagini),
  a cui è stata mostrata l'immagine, ci ha visto un valore che Latet non ha trovato nel testo letto,
  quindi non ha potuto coprirlo. Mascheralo a mano nel Revisore Live, o ritaglia l'immagine per
  escluderlo.

### Posso eseguire Latet in una sandbox con il mio agente?

Sì. Latet non è a sua volta una sandbox, e funziona dentro una sandbox: installalo dove gira
l'agente, così l'agente lo raggiunge su `127.0.0.1`.

### La mia azienda può usare Latet gratuitamente?

Sì, qualunque sia la sua dimensione, per il proprio lavoro, su qualsiasi numero di computer e
server. Anche l'uso personale e quello non commerciale sono gratuiti. Per la Community Edition serve
una licenza commerciale solo per vendere Latet, offrirlo ad altri come servizio, integrarlo in un
prodotto che vendi o per un fork commerciale: scrivi a [info@latet.ai](mailto:info@latet.ai). La
Enterprise Edition, i file nelle cartelle `ee`, aggiunge ciò che serve a un'azienda per gestire Latet
in modo centralizzato, e in produzione richiede una licenza commerciale; svilupparci e provarla è
gratuito. La Community Edition non controlla mai una licenza: non ci sono chiavi.
[LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md) ha i dettagli e gli esempi.

### Da dove viene il nome?

*Latet* in latino significa "si nasconde", come in *latet anguis in herba*: "un serpente si
nasconde nell'erba". Latet nasconde i tuoi segreti in bella vista: il modello vede un valore del
tutto simile a quello reale.
