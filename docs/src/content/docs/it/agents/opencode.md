---
title: OpenCode
description: Fai passare dal gateway di Latet i provider di OpenCode con chiave API.
---

OpenCode usa la **modalità gateway**: il daemon locale maschera le richieste e ripristina le
risposte. Copre i provider che usi con una chiave API: Anthropic, OpenAI e OpenRouter.

## Farlo passare da Latet

`latet global-install`, o `latet agents apply opencode`, fa puntare quei tre provider al daemon nella
tua configurazione globale di OpenCode, `~/.config/opencode/opencode.jsonc` (o `opencode.json` se è
quello il file che hai). Prima viene fatto un backup del file, e i suoi commenti vengono mantenuti.
Aggiunge:

```json title="~/.config/opencode/opencode.jsonc"
{
  "provider": {
    "anthropic": { "options": { "baseURL": "http://127.0.0.1:5283/anthropic/v1" } },
    "openai": { "options": { "baseURL": "http://127.0.0.1:5283/openai/v1" } },
    "openrouter": { "options": { "baseURL": "http://127.0.0.1:5283/openrouter/api/v1" } }
  }
}
```

Le tue chiavi API restano in OpenCode come prima: il gateway le inoltra senza modifiche e non scrive
mai i loro valori nei log o nella dashboard. Poi riavvia OpenCode.

L'`opencode.json` di un progetto viene letto dopo il tuo file globale, e un indirizzo che imposta per
uno di questi provider prende il posto di quello di Latet: in un progetto del genere, avvia OpenCode
con `latet opencode`, i cui indirizzi OpenCode legge per ultimi.

**Senza toccare la tua configurazione**, fai passare una sola sessione da Latet con il wrapper. Passa
a OpenCode le stesse modifiche solo per quell'esecuzione (attraverso `OPENCODE_CONFIG_CONTENT`),
avvia il daemon se serve e passa ogni argomento a `opencode`:

```sh
latet opencode
latet opencode run "add the payments key from .env to config/local.toml"
```

**OpenCode Desktop** legge la stessa configurazione globale: `latet agents apply opencode` (o
`latet global-install`) lo fa passare da Latet dopo che hai riavviato l'app, mentre `latet opencode`
avvia solo OpenCode da riga di comando. `latet agents revert opencode` toglie di nuovo gli indirizzi
di Latet e lascia il resto del file com'è.

## Limiti

- **Passano da Latet solo questi tre provider.** Con un modello di qualsiasi altro provider (Amazon
  Bedrock o Azure in OpenCode, per esempio), OpenCode parla direttamente con il provider, senza
  Latet, e niente ti avvisa: scegli un modello di Anthropic, OpenAI o OpenRouter. Claude Code e
  Codex passano da Latet anche con quei cloud: vedi [Endpoint aziendali](../company-endpoints/).
- **L'accesso con ChatGPT non è coperto.** Il plugin ChatGPT di OpenCode ha l'indirizzo del provider
  integrato, quindi un URL di base non può reindirizzarlo. OpenCode con l'accesso con ChatGPT è nella
  [roadmap](../../roadmap/).
- **Niente accesso con Claude Pro o Max.** Anthropic consente un abbonamento in Claude Code e nelle
  sue app, e Latet non riscrive mai una richiesta che contiene il token di un abbonamento Claude: si
  limita a controllarla, e la blocca se contiene un valore sensibile. Senza un plugin che prima
  mascheri il contenuto, richieste del genere verrebbero bloccate invece che mascherate. Usa una
  chiave API di Anthropic.

Per smettere di usare Latet, esegui `latet agents revert opencode`, o `latet global-uninstall` per
tutti gli agenti.
