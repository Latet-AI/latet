---
title: Gli agenti e le tue impostazioni
description: Lascia che il tuo agente guardi e modifichi le impostazioni di Latet. Ciò che aggiunge protezione si applica; ciò che la riduce attende la tua approvazione.
---

:::caution[Un guardrail, non una serratura]
Permette a un agente di aiutarti a configurare Latet e i tuoi progetti senza ridurre la protezione
per errore. Non può resistere a un agente deciso ad aggirarlo, o a uno ingannato da una prompt
injection: l'agente viene eseguito con il tuo utente. Un vero confine richiede di più, come Latet in
esecuzione con un altro utente o su un altro computer: vedi [quanto vale](#come-se-ne-accorge-latet-e-quanto-vale).
:::

Puoi chiedere al tuo agente di configurare Latet per te:
*"maschera ovunque il nostro cliente Falcon"*, *"attiva il Revisore Live in questo progetto"*,
*"perché questo valore è stato mascherato?"*. Indicagli
[`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md) nel repository: è
scritto per gli agenti, e dice loro quali comandi usare e cosa chiederti prima.

## Cosa può fare un agente da solo

Guarda con `latet config get`, `latet project show` e `latet status`, e modifica con
`latet config set`, `add` e `remove`. Ogni valore viene verificato rispetto allo schema delle
impostazioni, e la modifica si applica subito al daemon in esecuzione,
**purché aggiunga protezione**: un termine, un riconoscitore, un profilo più rigoroso, il revisore
locale che legge di più, il Revisore Live attivato.

```sh
latet config add terms '{value: Project Falcon, type: PROJECT}'
latet config set live_review true --project .
```

## Cosa attende la tua approvazione

Una modifica che **riduce** la protezione non si applica finché non la approvi, chiunque la faccia:

- un comando `latet` eseguito da un agente, o da uno script;
- `config.yaml` modificato a mano, quando il daemon lo ricarica;
- `config.yaml` trovato modificato all'avvio del daemon: finché non decidi, il daemon usa le
  ultime impostazioni che hai approvato.

Ridurre la protezione significa, per esempio, un valore da non mascherare mai, un profilo più basso,
un termine o un riconoscitore rimosso, un criterio impostato su `allow`, un termine o una regola di
campo di un tipo il cui criterio è `allow` (ciò che trova uscirebbe in chiaro), il revisore locale
o il Revisore Live disattivato o che legge di meno, Latet disattivato in un progetto, il server del
revisore locale spostato su un altro computer, o
[le sue indicazioni](../llm-review/#le-tue-indicazioni) modificate: un testo libero potrebbe dirgli
di non segnalare nulla, quindi qualsiasi modifica attende, e la dashboard mostra le indicazioni
prima e dopo. Lo stesso vale per una [destinazione](../../agents/company-endpoints/) spostata su un
altro indirizzo, o rimossa: un agente che vi punta invierebbe le sue richieste, e le sue chiavi, da
un'altra parte, quindi la dashboard mostra l'indirizzo prima e dopo.

La modifica attende nella pagina **Approvazioni** della dashboard, in *Impostazioni*, con ciò che
riduce, da dove viene (l'agente che ha eseguito il comando, quando Latet riesce a capirlo) e quali
impostazioni tocca. **Approva** la applica. **Annullala** riporta `config.yaml` com'era. Attende una
sola modifica alla volta; una più recente la sostituisce. In un tuo terminale, invece, è il comando
a chiedertelo.

All'agente viene detto cosa riduce la modifica, mai un valore, e che la modifica attende la tua
approvazione: la guida gli chiede di dirti cosa ha cambiato e perché.

**Un repository che Latet ora non riesce a leggere.** Una modifica viene valutata anche in ogni
repository con un `latet.yaml` proprio che Latet ha visto. I loro file vengono riletti da un processo
ausiliario, entro 10 secondi in tutto e 2 per ciascuno, a un ricaricamento o a un avvio con una
modifica che potrebbe ridurre la protezione in un repository, e a un'importazione nella dashboard
(vedi [I repository che Latet ricorda](../projects/#i-repository-che-latet-ricorda)). Quando uno di
essi non può essere letto entro quel tempo, o al momento non può essere letto affatto (una
condivisione che non risponde, un permesso negato), una modifica che lì potrebbe ridurre la
protezione non si applica né attende la tua approvazione: `latet reload` lo segnala e non cambia
nulla, e lo stesso fa un'importazione nella dashboard, finché il repository non torna leggibile. Una
modifica che non può ridurre nulla in un repository (un termine aggiunto, la porta, la cronologia) si
applica come al solito. Un avvio che trova `config.yaml` modificato e non riesce a valutarlo in
questo modo usa le ultime impostazioni che hai approvato. `latet repositories` mostra di quale
repository si tratta, e `latet repositories forget` lo toglie dall'elenco, in un tuo terminale.

**Su un server di Latet con Latet Enterprise,** una modifica che riduce la protezione (lì, i tuoi salvataggi nella
dashboard e ciò che il server trova ridotto quando applica di nuovo le tue impostazioni) può andare invece al team di
sicurezza della tua azienda: la dashboard dice chi decide, e puoi annullare la modifica, non approvarla. Vedi
[Approvazioni centrali](../../enterprise/approvals/).

## Cosa non possono fare gli agenti

`latet global-uninstall`, `latet agents revert` e `latet service uninstall` tolgono la protezione a
tutti gli agenti in una volta: eseguiti da un agente, si rifiutano e ti chiedono di eseguirli in un
tuo terminale. Lo stesso fanno `latet repositories forget`, `latet backup` e `latet restore` (la tua
cassaforte e la sua chiave), `latet connect` e `latet disconnect` (dove vengono mascherate le
richieste dei tuoi agenti), `latet dashboard` e `latet identity send` su un computer connesso,
l'amministrazione con `latet server` e `latet rules suggest --show-values`, che stamperebbe valori
reali.

## Come se ne accorge Latet, e quanto vale

Un agente contrassegna le shell in cui esegue i comandi con variabili proprie, che Latet riconosce:
Claude Code, Codex, OpenCode, Gemini CLI, Cursor, Hermes Agent, Goose, OpenClaw, e qualsiasi agente
che imposti `AGENT`. Il daemon indica anche l'agente nella modifica che ti mostra.

Sono protezioni contro gli errori, non serrature:

- **Il controllo che conta è quello del daemon.** Confronta ogni nuova configurazione con l'ultima
  che hai approvato, chiunque l'abbia scritta. Un agente che modifica `config.yaml` invece di usare
  i comandi riceve la stessa risposta.
- **Un agente viene eseguito con il tuo utente.** Uno deciso a farlo, o ingannato da un testo che ha letto (una
  prompt injection), può rimuovere le sue variabili, leggere i file che legge Latet, e approvare la
  propria modifica tramite l'interfaccia locale del daemon. Latet non può impedire ciò che può fare il
  tuo account utente: per questo serve Latet in esecuzione con un altro utente, o su un altro
  computer ([Latet su un server](../server/)).

Per un agente che supervisioni, questo basta: non può indebolire Latet per errore, e vedi ogni
modifica che riduce la protezione prima che si applichi. Vedi [Sicurezza](../../security/) per il
resto del modello delle minacce.

## Su un computer connesso

Le impostazioni condivise e le impostazioni e regole proprie di ogni identità risiedono sul
server. Usa `latet dashboard` per le impostazioni di una persona e l'amministrazione con
`latet server` per le impostazioni condivise o per quelle di una chiave API. Con il computer connesso,
`config`, `rules`, `llm`, `project`, `identity`, `repositories` e `reload` rifiutano sia di leggere
le impostazioni sia di cambiarle; funzionano solo `config schema`, `rules test` e `identity send`.
`latet identity send` reinvia esplicitamente i dati identificativi di questo computer.

I metadati della connessione, l'impronta del certificato, le credenziali e le associazioni degli
agenti restano sul computer. `LATET_CONNECTION` seleziona i metadati, non un file del token
personale. Su Windows, applicare la configurazione a Claude scrive il token nelle impostazioni utente
di Claude: è l'eccezione documentata dell'installazione (su Linux e macOS non viene scritto alcun
token, perché Claude Code passa da un server solo da Windows); la disconnessione rimuove
l'associazione. Gli endpoint del revisore sul server, le chiavi, i certificati e l'installazione di
OCR/NER spettano agli amministratori. Un progetto nella dashboard non permette al server di
scansionare il repository del computer.
Vedi [configurazione del server](../../getting-started/server/) e
[impostazioni condivise e regole proprie](../server/).

## Comandi

| Comando | Cosa fa |
| --- | --- |
| `latet config get [KEY]` | Un'impostazione (i punti per ciò che sta dentro: `llm_review.enabled`), o tutte |
| `latet config set KEY VALUE` | Modifica un'impostazione (il valore in YAML); `--unset` rimette il suo valore predefinito |
| `latet config add KEY VALUE` / `remove KEY VALUE` | Aggiunge un elemento a un elenco, o ne toglie uno (i termini per valore, i riconoscitori per nome) |
| `--project FOLDER` | Le tue impostazioni per il progetto in quella cartella, invece di quelle globali |
| `--wait` | Quando la modifica attende la tua approvazione, aspetta la tua risposta |
| `latet reload` | Applica un `config.yaml` modificato a mano (ciò che riduce la protezione attende la tua approvazione) |

Codici di uscita: `0` applicata, o salvata mentre il daemon non è in esecuzione (si applica all'avvio
del daemon, e ciò che riduce la protezione attende allora la tua approvazione); `3` attende la tua
approvazione; `1` il daemon ha
rifiutato la modifica (salvata, non applicata: `latet reload` dice perché), o l'elemento da rimuovere
non c'è; `2` non valida (non viene salvato niente).
