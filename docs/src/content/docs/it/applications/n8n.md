---
title: Mascherare i testi di un workflow n8n
description: I nodi Latet per n8n analizzano, mascherano, ripristinano e dimenticano i testi degli item di un workflow, attraverso l'API per le applicazioni.
sidebar:
  label: n8n
---

I nodi Latet per n8n ([`@latet-ai/n8n-nodes-latet`](https://www.npmjs.com/package/@latet-ai/n8n-nodes-latet),
con licenza MIT) chiamano l'[API per le applicazioni](../) di un server Latet da un workflow.
Analizzano, mascherano, ripristinano e dimenticano i testi degli item del workflow: un workflow
maschera ciò che manda a un modello o a un servizio, e ripristina ciò che torna. Servono un server
Latet, dalla 0.10.0 in poi, e il token di una chiave API; il token del computer di una persona viene
rifiutato.

## Installare i nodi

In n8n: **Settings > Community Nodes > Install**, poi scrivi `@latet-ai/n8n-nodes-latet`. La
[guida all'installazione](https://docs.n8n.io/integrations/community-nodes/installation/) di n8n ha i
dettagli.

## Un token per il workflow

L'amministratore del server Latet crea un token con i permessi che il workflow usa, e nessuno di più
([chiavi API e token](../#chiavi-api-e-token)):

```sh
latet server --server https://latet.acme.lan keys add-token "Support desk" n8n --mask --unmask --out n8n.token
```

| Operazione | Permesso |
| --- | --- |
| Scan | `scan`, o `mask` |
| Mask | `mask` |
| Unmask | `unmask` |
| Forget | `forget`, che nessun altro permesso porta con sé |

In n8n, le credenziali **Latet API** contengono l'indirizzo del server (`https://latet.acme.lan`) e il
token, cioè il contenuto di `n8n.token`. Usa un indirizzo `https://`: con `http://` il token e i testi
viaggiano in chiaro. n8n prova le credenziali con l'analisi di una parola, che non conserva nulla e
richiede `scan` o `mask`: un token che può solo ripristinare o dimenticare non supera la prova, e nel
nodo funziona lo stesso.

## Che cosa fa ogni operazione

Ogni operazione legge il testo di ogni item da un campo (**Text Field**, `text` se non indicato) e
manda gli item in chiamate di al massimo 1.000 testi.

- **Scan** dice dove sono i valori in ogni testo, per tipo, e non conserva nulla.
- **Mask** restituisce ogni testo con valori fittizi al posto dei suoi valori, lo stesso valore
  fittizio per lo stesso valore in ogni chiamata della chiave. **Kind** dice che cosa sono i testi:
  **Prose** per il testo scritto da persone, **Data** per log, record e l'output di un programma
  ([prosa e dati](../#prosa-e-dati)).
- **Unmask** rimette i valori reali al posto dei valori fittizi della chiave. Il server scrive ogni
  ripristino nel suo registro amministrativo.
- **Forget** toglie dei valori dalla cassaforte della chiave, uno per item (**Value Field**, `value`
  se non indicato), scritti come erano quando sono stati mascherati
  ([dimenticare dei valori](../forget/)).

Un testo a cui il server non risponde, perché il suo revisore locale l'ha fermato o la sua protezione
dai dump l'ha trattenuto, esce con `blocked` e senza testo, così il testo in chiaro non va oltre.

## Un modello che non vede mai i valori

Un workflow che risponde ai messaggi dell'assistenza con un modello:

1. Un trigger riceve il messaggio.
2. **Latet: Mask**, kind **Prose**, maschera il messaggio.
3. Il nodo del modello (OpenAI, Anthropic, Gemini, un AI Agent) riceve nel prompt il messaggio
   mascherato.
4. **Latet: Unmask**, con **Text Field** impostato sul campo che contiene la risposta del modello,
   rimette i valori reali.
5. Il workflow invia la risposta.

Il modello vede solo valori fittizi, sempre gli stessi nei messaggi di uno stesso cliente, e il
cliente legge i valori reali. Non dare a un agente AI l'operazione Unmask come strumento:
consegnerebbe al modello proprio i valori che il mascheramento gli tiene nascosti.

Il [README](https://github.com/Latet-AI/n8n-nodes-latet#readme) del pacchetto elenca ogni campo che i
nodi scrivono e gli errori che danno.
