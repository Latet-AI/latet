---
title: Claude Code
description: La modalità client per Claude Code, con gli abbonamenti Pro e Max o con le chiavi API, e quali superfici di Claude sono coperte.
---

Claude Code usa la **modalità client**: il plugin di Latet maschera i contenuti dentro Claude Code, e
il daemon locale si limita a verificare. Funziona con gli abbonamenti Pro e Max senza una chiave API,
e anche con le chiavi API.

## Terminale Windows connesso

Il processo ausiliario connesso al server è supportato per Claude Code in un terminale
Windows, con impostazioni utente attendibili e l'associazione privata installata da Latet.
Questo non estende il supporto connesso a Desktop, VS Code, JetBrains, Linux o macOS.
Su Linux e macOS `latet connect`, `latet claude`, `latet agents apply claude` e
`latet global-install` lo dicono subito, le impostazioni di Claude Code non ricevono il token
e il plugin trattiene ogni prompt; `latet codex` e `latet opencode` passano dal server.
Segui [la guida di connessione](../../getting-started/server/) per `latet connect`,
`latet agents apply claude`, conservazione del token e `latet disconnect`.

Su un computer connesso `LATET_BRIDGE_TOKEN`, nelle impostazioni e nell'ambiente di Claude Code,
contiene il token di questo computer, e ogni comando che Claude Code esegue può leggerlo. Da ovunque
il server sia raggiungibile, il token apre il bridge del plugin e il proxy del tuo spazio: chi lo
possiede può far mascherare testi nel tuo spazio, far ripristinare i valori fittizi che ha e inviare
richieste attraverso di esso. Non apre mai la tua dashboard, dove ogni originale può essere mostrato,
non cambia mai la tua identità e non riduce mai la tua protezione: l'unica modifica che può fare alle
tue impostazioni è aggiungere una destinazione per un indirizzo, che non riduce nulla. Per la dashboard
e l'identità il server chiede anche la chiave del computer, che resta nell'archivio delle credenziali
(o nel file privato del token) e non viene mai data a un agente. La chiave impedisce a un token
trapelato (dall'ambiente, dalle impostazioni, da un log o con un solo `curl`) di aprire la dashboard;
non ferma un comando scritto per leggerli entrambi dall'archivio delle credenziali. Su un computer
connesso `latet dashboard` e `latet identity send` rifiutano di essere eseguiti da un agente. Se il
token può essere trapelato, `latet disconnect` lo revoca.

Su un computer connesso la modalità di stampa (`claude -p`) non è supportata: il plugin
trattiene le sue richieste. Usa il terminale interattivo. Quando il plugin trattiene una
richiesta dice perché: la modalità di stampa o un'altra superficie; un'associazione del processo
ausiliario mancante o non aggiornata (`latet agents apply claude`); un server che non ha
risposto (`latet status`); un token che il server non accetta più (`latet disconnect`, poi
`latet connect` con un nuovo codice di un amministratore).
Il plugin maschera le descrizioni degli strumenti di Claude Code; su un server il controllo
finale legge anche le loro descrizioni, gli schemi dei parametri e gli esempi, e un valore
rimasto in chiaro ferma la richiesta ([perimetro della protezione](../../concepts/protection-scope/)). Le tabelle e le istruzioni del daemon
sotto descrivono **Latet locale**; non ampliano il supporto del processo ausiliario connesso.

## Copertura

| Superficie | Plugin | Daemon in sola verifica | Note |
| --- | --- | --- | --- |
| Claude Code CLI (terminale) | ✅ | ✅ | Modalità client completa |
| Estensioni per VS Code e JetBrains | ✅ | ✅ | Lo stesso Claude Code, con gli stessi plugin e le stesse impostazioni |
| Scheda Code di Claude Desktop (sessioni locali), con l'accesso a Claude | ✅ | ❌ | Solo il plugin: l'app ignora `ANTHROPIC_BASE_URL`, quindi nessun daemon controlla dietro il plugin e nella dashboard non viene registrato niente |
| Chat e Cowork di Claude Desktop, con l'accesso a Claude | ❌ | ❌ | Non coperte: non caricano i plugin di Claude Code e non passano da Latet |
| Claude Desktop su un gateway di terze parti: Chat, Cowork e Code | Scheda Code | ✅ | Ogni richiesta passa da Latet e viene controllata; solo la scheda Code maschera |
| Sessioni cloud | ❌ | ❌ | Non coperte: girano sull'infrastruttura di Anthropic, non sul tuo computer |

**Claude Desktop, due configurazioni.** Con l'accesso a Claude e un abbonamento Pro o Max, la scheda
Code carica il plugin di Latet, che maschera i contenuti e fa un proprio controllo finale prima di
ogni richiesta. L'app però ignora `ANTHROPIC_BASE_URL`, quindi nessun daemon controlla la richiesta
dietro il plugin, e nella dashboard non viene registrato niente. Chat e Cowork non caricano i plugin
di Claude Code e non passano da Latet: non sono coperte.

Su un gateway di terze parti Claude Desktop non ha l'accesso a Claude, quindi non c'è un abbonamento
Pro o Max: le richieste si pagano a consumo, con una chiave API di Anthropic o attraverso il provider
della tua azienda. Se imposti Latet come gateway, Chat, Cowork e Code gli inviano ogni richiesta.
Queste richieste le costruisce il motore di Claude Code, quindi Latet le controlla una per una e
ferma quella che porterebbe con sé un segreto o un dato personale in chiaro, ma non la maschera: la
[guida ai gateway](https://code.claude.com/docs/en/llm-gateway-protocol) di Anthropic chiede a un
gateway di esaminare le richieste di Claude Code, non di modificarle. La scheda Code ha sia il
mascheramento del plugin sia il controllo del daemon. Per la configurazione, vedi
[Claude Desktop su un gateway](../other-tools/#claude-desktop-su-un-gateway).

Al lavoro, Claude Code passa spesso dal gateway della tua azienda, da Amazon Bedrock, da Google Vertex
o da Microsoft Foundry: Latet legge quel percorso dalle tue impostazioni e si mette davanti, chiavi
comprese. Vedi [Endpoint aziendali](../company-endpoints/).

Provato dal vivo: la CLI con un abbonamento. Le estensioni per gli IDE e la scheda Code di Claude
Desktop usano lo stesso plugin e le stesse impostazioni; se lì qualcosa non funziona,
[apri una issue](https://github.com/Latet-AI/latet/issues), per favore. Cursor non è coperto: le sue
richieste al modello partono dai server di Cursor, non dal tuo computer.

## Abbonamenti Pro e Max

La modalità client funziona con Claude Code quando accedi con un abbonamento Pro o Max, senza chiave
API, e anche con una chiave API. La pagina
[Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) di Anthropic stabilisce
come si può usare un abbonamento. Punto per punto:

| I termini di Anthropic | Cosa fa Latet |
| --- | --- |
| L'accesso con un abbonamento è pensato per l'uso ordinario di Claude Code e delle app di Anthropic. | Il tuo abbonamento viene usato solo in Claude Code. Codex, OpenCode, Hermes Agent e ogni altro agente passano dal gateway con il proprio accesso: una chiave API, oppure l'accesso con ChatGPT di Codex. |
| Il binario di Claude Code non deve essere modificato. | Claude Code gira così come lo pubblica Anthropic. Latet aggiunge un plugin, attraverso i comandi per i plugin di Claude Code stesso, e alcune variabili d'ambiente in `settings.json`. |
| L'accesso deve avvenire attraverso il flusso di Anthropic; gli sviluppatori non possono raccogliere, conservare o fare da intermediari per le credenziali di Claude.ai. | Accedi con il login di Claude Code stesso. Latet non ti chiede mai le credenziali, non le legge mai per sé e non le conserva mai. |
| Le regole non ti impediscono di accedere al binario non modificato di Claude Code con il tuo abbonamento. | È così che si usa Latet: il tuo Claude Code, il tuo login. |

**Il percorso di una richiesta.** Con `ANTHROPIC_BASE_URL` che punta al daemon locale, Claude Code
invia le sue richieste attraverso il daemon, come attraverso qualsiasi gateway: Anthropic documenta un
[gateway](https://code.claude.com/docs/en/llm-gateway) con il login tramite abbonamento, e la sua
[guida ai gateway](https://code.claude.com/docs/en/llm-gateway-protocol) chiede a un gateway di
inoltrare le richieste senza cambiarle e di ispezionarle senza modificarle. È quello che fa il daemon:
ogni richiesta arriva ad Anthropic byte per byte, header compresi, con dentro il tuo token così come
l'ha messo Claude Code. Il token non viene mai salvato, scritto nei log o mostrato nella dashboard.

**Perché non serve riscrivere niente.** Claude Code aggiunge al prompt di sistema di ogni richiesta un
blocco di attribuzione con un'impronta derivata dalla conversazione. Un proxy che riscrivesse le
richieste cambierebbe quello che Claude Code ha inviato. Il plugin invece maschera dentro Claude Code,
prima che la richiesta venga costruita, così il daemon si limita a controllarla: può lasciar passare
una richiesta o bloccarla, mai cambiarla. Vedi [Come funziona](../../concepts/how-it-works/) per un
esempio passo per passo.

Questo descrive come funziona Latet; non è una consulenza legale, e il riferimento sono i termini di
Anthropic.

## Cosa fa il plugin

Il plugin chiede al daemon locale di rilevare e mascherare; dentro Claude Code stesso non viene
rilevato niente.

- **Maschera i contenuti dove nascono**, prima che entrino nella conversazione: i prompt che scrivi,
  il prompt di sistema, `CLAUDE.md` e l'altro contesto che Claude Code aggiunge, gli allegati, i
  prompt delle skill, le descrizioni degli strumenti e ogni risultato di uno strumento (contenuti dei
  file, output dei comandi, output degli hook).
- **Rimette gli originali negli argomenti delle chiamate agli strumenti** prima che uno strumento
  venga eseguito, così Read, Edit, Write, Bash e i tuoi server MCP locali lavorano sui valori reali e
  i file su disco sono corretti.
- **Lascia i valori fittizi dove gli argomenti escono dal tuo computer**: WebFetch, WebSearch e i
  connettori di claude.ai ricevono i valori fittizi che ha scritto il modello. Anche i subagent li
  ricevono, e ci lavorano come fa il modello.
- **Ferma uno strumento fuori dal tuo computer che lavorerebbe su un valore fittizio**: una chiamata a
  WebSearch, a WebFetch o a un connettore di claude.ai con dentro un valore fittizio cercherebbe
  qualcun altro, aprirebbe la pagina sbagliata o scriverebbe a un indirizzo che non esiste. La
  chiamata non viene eseguita; il modello viene informato del perché, e ti chiede se il valore può
  uscire in chiaro. La dashboard mostra la chiamata, con *Non sensibile* per ogni valore.
- **Ripristina quello che vedi**: i tuoi messaggi, le risposte del modello, i comandi e le chiamate
  agli strumenti mostrati sullo schermo (anche un loro gruppo compresso, mentre è in corso) e quello
  che hanno stampato contengono i valori reali. Quello che legge il modello non cambia.
- **Controlla ogni richiesta prima che venga inviata.** Se un valore sensibile sta per uscire in
  chiaro, la richiesta non viene inviata e vedi invece un messaggio:

  ```text
  Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
  sent in clear. Nothing was sent.
  ```

  La dashboard mostra la richiesta, con il valore che l'ha fermata riquadrato in rosso: se non è
  sensibile, fai clic sul valore e scegli *Non sensibile*, poi riprova il turno.

  Quando i valori in chiaro sono in una conversazione iniziata prima che Latet fosse attivo, il
  messaggio lo dice, e ti chiede di iniziare una nuova conversazione con `/clear` (vedi
  [Risoluzione dei problemi](../../troubleshooting/#ogni-richiesta-di-una-conversazione-viene-bloccata)).

Se il daemon non è in esecuzione, il plugin lo avvia. Se non ci riesce, il plugin trattiene i
contenuti invece di inviarli senza mascherarli: il tuo prompt non viene inviato, i risultati degli
strumenti vengono sostituiti da un avviso e le chiamate agli strumenti vengono rifiutate, finché il
daemon non torna. `latet doctor` ti dice cosa non va.

## Cosa fa il daemon

Con `ANTHROPIC_BASE_URL` che punta al daemon, ogni richiesta passa dal daemon, che:

- controlla se la richiesta in uscita contiene valori sensibili in chiaro, e la blocca se ne trova
  uno: Claude Code mostra allora un errore dell'API con il messaggio
  `Latet: sensitive values would leave in clear`;
- altrimenti inoltra i byte senza modifiche, header compresi;
- registra lo scambio per la [dashboard](../../dashboard/), senza le sue credenziali.

Non riscrive mai una richiesta di Claude Code, né una che contiene il token di un abbonamento Claude,
chiunque la invii.

## Ogni sessione, o una sola

Ci sono due modi per far passare Claude Code da Latet:

| | `latet global-install`, `latet agents apply claude` | `latet claude` |
| --- | --- | --- |
| Cosa copre | Ogni sessione di Claude Code: il terminale, VS Code, JetBrains e la scheda Code di Desktop (solo il plugin) | Quella sola sessione, nel terminale |
| Cosa cambia | Installa il plugin e aggiunge a `settings.json` le impostazioni qui sotto, dopo averne fatto un backup | Niente: il plugin e le impostazioni vengono dati solo a quella sessione |
| Per annullare | `latet agents revert claude`, oppure `latet global-uninstall` | Chiudi la sessione |
| `latet doctor` | Controlla il plugin e le impostazioni | Segnala Claude Code come non applicato, ed è normale |

`latet agents apply claude` fa passare da Latet solo Claude Code: eseguilo quando installi Claude Code
dopo `latet global-install`, o dopo aver cambiato la porta. `latet claude` avvia il daemon se serve e
passa ogni argomento a `claude`:

```sh
latet claude
latet claude --continue
```

`claude` avviato da solo, senza `latet global-install`, non passa da Latet; in una shell in cui
`latet env` ha impostato `ANTHROPIC_BASE_URL`, arriva a Latet senza il plugin, e le sue richieste
vengono solo controllate. `latet run claude` equivale a `latet claude`: Claude Code ha bisogno del
plugin oltre che dell'indirizzo, e `latet run` da solo gli darebbe solo l'indirizzo.

## Cosa cambia global-install

`latet global-install`, o `latet agents apply claude`, fa due cose:

1. Installa il plugin attraverso i comandi per i plugin di Claude Code stesso, da un marketplace
   locale nella cartella dei dati di Latet.
2. Aggiunge un blocco `env` a `~/.claude/settings.json` (o a `$CLAUDE_CONFIG_DIR/settings.json`),
   dopo aver fatto un backup del file:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/anthropic",
    "LATET_BRIDGE_URL": "http://127.0.0.1:5283",
    "LATET_BRIDGE_TOKEN": "<a random token, created when first needed>",
    "ENABLE_TOOL_SEARCH": "true",
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0"
  }
}
```

- `ANTHROPIC_BASE_URL` fa passare ogni richiesta dal daemon. Con il gateway della tua azienda,
  Amazon Bedrock, Google Vertex o Microsoft Foundry, Latet scrive al suo posto la variabile di quel
  percorso (vedi [Endpoint aziendali](../company-endpoints/)); con un indirizzo su questo computer,
  lascia il tuo com'è.
- `LATET_BRIDGE_URL` e `LATET_BRIDGE_TOKEN` dicono al plugin dov'è il daemon, e dimostrano al daemon
  che il plugin è il tuo.
- `ENABLE_TOOL_SEARCH` lascia attiva la ricerca degli strumenti MCP: Claude Code la disattiva quando
  l'URL di base è personalizzato.
- `CLAUDE_CODE_AUTO_MODE_SERVER` sposta la revisione della modalità auto in richieste che Latet può
  controllare. Senza questa riga, la revisione viaggia come campo aggiuntivo di ogni richiesta, fuori
  dalla portata sia del plugin sia del daemon. `latet agents apply claude --keep-classifier-context`
  lascia fuori questa riga.

Poi riavvia Claude Code. `latet agents status` mostra se è applicato, e `latet agents revert claude`
annulla entrambi i passaggi.

### Il revisore della modalità auto

In modalità auto, Claude Code chiede a un modello revisore se ogni azione è sicura. Al revisore
vengono inviati il tuo CLAUDE.md, l'azione, il tuo nome utente del sistema operativo
(come *User identity*) e i percorsi della tua cartella home e della cartella di lavoro, in un testo
che il plugin non può raggiungere. Il revisore li usa per distinguere le tue cose da quelle degli
altri: un push su un branch `yourname/...` è tuo, uno su `someone-else/...` no. Il daemon controlla
queste richieste come tutte le altre, e `auto_mode_review` decide, dettaglio per dettaglio, cosa della
tua identità può arrivare al revisore:

| Dettaglio | Cos'è |
| --- | --- |
| `user_name` | Il tuo nome utente del sistema operativo, la *User identity* del revisore. |
| `paths` | Il nome della tua cartella utente nei percorsi della tua cartella home e della cartella di lavoro. |
| `host_name` | Il nome host di questo computer, quando è nel tuo CLAUDE.md o nell'azione. |
| `git_author` | Il tuo nome e la tua email come nel tuo autore git, e il dominio di quell'email quando è quello della tua azienda, quando sono nel tuo CLAUDE.md o nell'azione. |

Ogni dettaglio è `block` (il predefinito) o `allow`. Una revisione che conterrebbe un dettaglio
bloccato viene fermata, così il dettaglio resta in locale, e la modalità auto non può approvare
l'azione. Claude Code allora la rifiuta, segnalando il suo revisore come temporaneamente non
disponibile, e non ti chiede nulla: esegui l'azione tu stesso, o passa Claude Code a una modalità dei
permessi che ti chieda conferma. Un dettaglio consentito arriva al revisore, e solo al revisore.
`auto_mode_review: block` e `auto_mode_review: allow-identity` impostano tutti i dettagli in una
volta; uno per uno, si scrive così:

```yaml
auto_mode_review:
  user_name: allow
  paths: allow
  host_name: block
  git_author: block
```

I segreti e i dati di altre persone non passano mai: una chiave o l'email di un cliente nell'azione
ferma la revisione, qualunque cosa tu scelga. La tua email conta come parte del tuo autore git.

**Tieni i segreti fuori dal tuo CLAUDE.md.** Claude Code dà al revisore il tuo CLAUDE.md così com'è
su disco, in una richiesta che costruisce da sé, quindi lì il plugin non può mascherarlo, e il daemon
non cambia mai le richieste di Claude Code. Una password o l'email di un cliente nel file ferma allora
ogni revisione, e la modalità auto rifiuta ogni azione che ne ha bisogno. Il modello che fa il lavoro
non ne risente: riceve il tuo CLAUDE.md mascherato. Sposta quei valori in `.env` e nel file scrivi
invece il nome della variabile (*il database di staging è in `DATABASE_URL`*). Il plugin lo segnala
quando inizia una sessione, e `latet doctor` elenca ogni valore per riga e tipo, mai il valore stesso.

Per impostazione predefinita ogni dettaglio resta in locale;
`latet global-install --auto-mode-review user_name,paths` li lascia passare uno per uno. In seguito
puoi cambiarlo dalla pagina **Regole** della dashboard e in `config.yaml`. Quando una revisione viene
fermata, la dashboard mostra i dettagli che conteneva e l'impostazione che li lascia passare.

**Un nome utente fittizio.** Con `user_name: surrogate`, Latet dà a Claude Code il valore fittizio del
tuo nome utente in `USERNAME` e `USER`. Claude Code comunica al revisore quel nome, lo stesso nome
fittizio che il modello vede ovunque altrove, e una revisione che contiene ancora il tuo vero nome
utente viene fermata. Dopo averlo scelto, esegui `latet agents apply claude`, poi riavvia Claude Code.
Anche i comandi che Claude Code esegue vedono il nome fittizio, in `%USERNAME%`, `$USER` e nel
`getpass.getuser()` di Python; `whoami`, la tua cartella home e l'autore di git restano come sono. È
utile quando il tuo nome utente e la tua cartella utente sono diversi, cosa comune su Windows,
insieme a `paths: allow`: il revisore riceve allora il nome della tua cartella ma mai il tuo nome
utente. Quando sono uguali, i percorsi contengono comunque il nome reale.

## Limiti

- **La modalità print mostra i valori fittizi.** Con `claude -p` la risposta stampata contiene valori
  fittizi, perché solo l'interfaccia interattiva ripristina quello che c'è sullo schermo. I file
  scritti dagli strumenti ricevono comunque i valori reali.
- **Claude Desktop con l'accesso a Claude**: la scheda Code ha il plugin e il suo controllo finale,
  ma non il daemon che sta dietro, e nella dashboard non viene registrato niente; Chat e Cowork non
  sono coperte. Su un gateway di terze parti ogni richiesta viene controllata e solo la scheda Code
  maschera: vedi [Copertura](#copertura).
- **Remote Control è disattivato** mentre Latet è sul percorso delle richieste: Claude Code lo
  disattiva quando l'URL di base è personalizzato.
- **Gli strumenti web e i connettori di claude.ai non possono usare un valore mascherato.** I loro
  argomenti escono dal tuo computer, quindi riceverebbero quello fittizio: una chiamata del genere
  viene fermata, e scegli tu se il valore può uscire in chiaro (*Non sensibile*). I subagent restano
  sul tuo computer e lavorano sui valori fittizi come fa il modello. I risultati dei connettori
  arrivano a Claude Code attraverso i server di Anthropic, quindi il loro contenuto è già lì prima che
  Latet lo veda. Vedi
  [Cosa è protetto e cosa no](../../concepts/protection-scope/).
- **Immagini**, con il [riconoscimento del testo](../../concepts/images/) opzionale: quelle che
  restituiscono gli strumenti vengono sostituite dalla loro copia mascherata; uno screenshot che
  incolli con dei valori dentro viene tolto dal prompt, e Claude riceve il percorso di una copia
  mascherata da aprire con Read. I **PDF** non vengono letti e vengono inviati così come sono.
