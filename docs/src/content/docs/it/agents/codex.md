---
title: Codex (CLI e Desktop)
description: Fai passare Codex CLI e Codex Desktop dal gateway di Latet con openai_base_url.
sidebar:
  label: Codex
---

Codex usa la **modalità gateway**: il daemon locale maschera le richieste e ripristina le risposte.
Funziona sia se accedi con ChatGPT sia se usi una chiave API di OpenAI.

## Farlo passare da Latet

Codex CLI e Codex Desktop leggono lo stesso file di configurazione, `~/.codex/config.toml` (o
`$CODEX_HOME/config.toml` se imposti `CODEX_HOME`). Lì basta una riga per far passare Codex da Latet.

`latet global-install`, o `latet agents apply codex`, la scrive per te. Chiede a Codex come accedi
(`codex login status`) e sceglie l'indirizzo corrispondente:

```toml title="~/.codex/config.toml"
# Signed in with ChatGPT:
openai_base_url = "http://127.0.0.1:5283/backend-api/codex"

# With an OpenAI API key instead:
# openai_base_url = "http://127.0.0.1:5283/openai/v1"
```

Il resto del file, commenti compresi, resta com'era, e prima viene fatto un backup. Poi riavvia Codex
(o l'app Codex Desktop).

- Un Codex precedente alla 0.115 ignora `openai_base_url`. `latet doctor` lo segnala: aggiorna Codex
  (`npm i -g @openai/codex`). `latet global-install` e `latet agents apply codex` lasciano un Codex del
  genere com'è e dicono perché, dato che quella riga non lo farebbe passare da Latet. Finché non lo
  aggiorni, `latet codex` passa a Codex l'indirizzo in `OPENAI_BASE_URL`, l'unica variabile che Codex
  legge, al posto di quello impostato dalla tua shell. `latet codex` non avvia un Codex che ha un
  provider suo (vedi sotto): quel Codex aggirerebbe Latet.
- Se in seguito passi dall'accesso con ChatGPT alla chiave API, o al contrario, esegui di nuovo
  `latet agents apply codex`, così l'indirizzo corrisponde.

**Senza toccare `config.toml`**, fai passare una sola sessione da Latet con il wrapper. Avvia il
daemon se serve e passa ogni argomento a `codex`:

```sh
latet codex
latet codex exec "add the payments key from .env to config/local.toml"
```

`latet run codex` equivale a `latet codex`, che sceglie l'indirizzo adatto al tuo accesso.

Su un computer collegato a un server Latet, `latet codex` avvia la sessione interattiva e
`codex exec`, `resume` e `fork`; rifiuta gli altri sottocomandi di Codex, come `review` o `mcp`, che
non può far passare dal server.

**Anche un provider tuo** (Azure OpenAI, il gateway della tua azienda, Mistral...) passa da Latet. Si
sposta su Latet solo il suo `base_url`; la sua chiave e tutto il resto restano come li hai scritti.

- **Per una sessione**, `latet codex` sposta i provider di `config.toml` e del profilo che indichi
  (`--profile work`), con `-c model_providers.<id>.base_url=...`. Codex non legge altri file, e si
  rifiuta di avviarsi con un'opzione per un provider che non carica. Un Codex precedente alla 0.115
  non accetta quelle opzioni: se ha un provider suo, `latet codex` non lo avvia e dice di aggiornare
  Codex.
- **In modo permanente**, `latet global-install` cambia ogni indirizzo nel file che lo scrive,
  compresi i file di ogni profilo, dato che un profilo può far passare Codex a uno qualsiasi di
  quegli indirizzi.

Allo stesso modo passa da Latet anche un `openai_base_url` scritto in un profilo (l'indirizzo di
un'azienda al posto di quello di OpenAI). Vedi [Endpoint aziendali](../company-endpoints/).

## Come si comporta

- Con l'accesso tramite ChatGPT, Codex parla con il provider su un WebSocket. Il daemon maschera ogni
  richiesta che Codex invia su di esso e ripristina gli eventi che tornano in streaming. Con una
  chiave API succede lo stesso su HTTP.
- Il provider vede i valori fittizi. Codex vede gli originali, nel testo della risposta e negli
  argomenti delle chiamate agli strumenti, così i comandi che esegue e le patch che applica usano i
  valori reali. [Come funziona](../../concepts/how-it-works/) segue una chiave da `.env` attraverso
  un turno di Codex, passo per passo.
- I corpi compressi delle richieste vengono decodificati, mascherati e compressi di nuovo. Un corpo
  che Latet non riesce a decodificare viene rifiutato, non inviato senza controllo.
- Il tuo token di accesso o la tua chiave API vengono inoltrati senza modifiche; il loro valore non
  viene mai scritto nei log o nella dashboard.
- Se il daemon non è in esecuzione, Codex non può raggiungere il provider. È voluto: niente esce
  senza essere mascherato.
- Codex legge i file con comandi della shell, quindi la
  [modalità dati rigorosa](../../concepts/production-data/#quali-strumenti) copre anche i file che legge.
  Indica il suo strumento per il database in `data_tools` per coprire solo quello.

Provato dal vivo: la CLI di Codex con l'accesso tramite ChatGPT. Codex Desktop legge lo stesso
`config.toml`; se lì qualcosa non funziona, [apri una issue](https://github.com/Latet-AI/latet/issues),
per favore.

Per smettere di usare Latet, esegui `latet agents revert codex`, o `latet global-uninstall` per tutti
gli agenti. Vedi [Disinstallazione](../../getting-started/uninstall/).
