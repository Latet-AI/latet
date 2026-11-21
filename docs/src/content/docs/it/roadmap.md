---
title: Roadmap
description: I prossimi passi di Latet, a partire dal più vicino nel tempo.
---

I prossimi passi, a partire dal più vicino nel tempo. Idee e aiuto sono benvenuti: apri una
[issue](https://github.com/Latet-AI/latet/issues), o vedi [Contribuire](../contributing/). Ciò che
è già cambiato è nel [changelog](../changelog/).

## La modalità auto di Claude Code, un rifiuto chiaro

Quando una revisione della modalità auto conterrebbe un dettaglio che tieni in locale (il tuo nome
utente, i tuoi percorsi), oggi Latet la ferma, e Claude Code segnala il suo revisore come non
disponibile e rifiuta l'azione. Latet potrebbe invece rispondere con il verdetto del revisore
stesso, *blocked*, così il rifiuto si presenterebbe per quello che è. Quella risposta verrebbe da
Latet al posto del revisore di Anthropic, quindi prima chiediamo: una issue spiega il caso con la
pull request pronta, la inviamo ad Anthropic, e la pull request viene integrata solo con il loro
consenso, allegato alla issue.

## Strumenti MCP mascherati prima che Claude Code li legga

Lo schema dei parametri di uno strumento MCP può contenere un valore che Latet maschera, come un
indirizzo d'esempio, e Claude Code lo invia con le sue richieste, dove il plugin di Latet non arriva:
oggi Latet ferma quelle richieste (vedi [Risoluzione dei problemi](../troubleshooting/)). Latet si
metterebbe invece tra Claude Code e i server MCP che configuri: maschererebbe le descrizioni e gli
schemi degli strumenti nell'elenco che Claude Code legge, e rimetterebbe i valori veri nelle chiamate
del modello, così le richieste di Claude Code restano come Claude Code le ha costruite. Varrebbe per i
server che aggiungi tu, non per i connettori che arrivano da claude.ai.

## Un installer firmato per Windows

Gli antivirus controllano i programmi che non conoscono ogni volta che vengono avviati, e i launcher
di un'installazione Python non sono firmati, quindi su Windows ogni comando può attendere alcuni
secondi prima di partire (vedi
[Risoluzione dei problemi](../troubleshooting/#i-comandi-sono-lenti-su-windows)). Un installer
firmato, anche tramite winget, li fa partire subito.

## Il tuo modello dei nomi

Oggi Latet usa un solo modello dei nomi, [Privacy Filter](https://huggingface.co/openai/privacy-filter)
di OpenAI, che scarica su richiesta (`latet ner install`) e che legge molte lingue. Un modello tuo
troverebbe i nomi e i termini del tuo settore, come
farmaci e diagnosi in una clinica, o parti e tribunali in uno studio legale: un classificatore di
token ONNX sul tuo computer, o un endpoint che gestisci tu, come un servizio GLiNER o Presidio. Più
modelli potrebbero lavorare insieme, ognuno aggiungendo ciò che trova.

## Più della Gemini API di Google

Leggere il resto di quello che mandano i client di Gemini: l'Interactions API, la Live API, la Batch API, il
caricamento di file, la generazione di immagini e le richieste `:predict` di Vertex (i suoi modelli
di embedding e di immagini). Fino ad allora Latet le rifiuta sui suoi percorsi di Gemini invece di
inviarle senza mascherarle (vedi [Gemini CLI e Google GenAI](../agents/gemini/)).

## Altri agenti

OpenCode con l'accesso tramite ChatGPT, o con i suoi modelli su Amazon Bedrock o Azure. L'API Mantle
di Amazon Bedrock per Claude Code, e Bedrock tramite un endpoint VPC o il suo indirizzo FIPS con il
tuo login AWS (con una chiave API di Bedrock funzionano già entrambi). Antigravity CLI, verificato
con Latet.

## pi, oh-my-pi e ThinkRail

Un plugin per [pi](https://github.com/earendil-works/pi) e
[oh-my-pi](https://github.com/can1357/oh-my-pi) maschererebbe i valori sensibili dentro l'agente,
prima che ogni richiesta venga costruita, come fa il plugin per Claude Code. Il mascheramento non
dipenderebbe dal provider, quindi coprirebbe JetBrains AI e gli abbonamenti oltre alle chiavi API, e
ogni richiesta passerebbe anche dal gateway di Latet e comparirebbe nella dashboard. JetBrains
ThinkRail usa pi, quindi anche le sue conversazioni sarebbero protette; i suoi subagenti e un
pulsante per la dashboard aspettano che ThinkRail carichi estensioni fatte da altri.

## File con dati sensibili

I file che un agente invia al modello, come PDF, documenti Word e fogli Excel, verrebbero mascherati
come il testo, un formato alla volta. In ogni file di un formato che sa leggere, Latet sostituirebbe
i valori sensibili con valori fittizi e invierebbe il file mascherato, poi rimetterebbe i valori
reali nella risposta, anche nei file che il modello restituisce. Un file in un formato che Latet non
legge viene rifiutato, in una conversazione come caricato a parte tramite l'API dei file di un
provider, su un computer come su un server (vedi
[Cosa è protetto e cosa no](../concepts/protection-scope/)).

## Latet Enterprise

Latet Enterprise è pensato per un'azienda il cui team di sicurezza stabilisce le regole per tutti:
regole che nessuno può allentare, l'accesso unico (SSO) con gruppi e ruoli come quello di auditor in
sola lettura, approvazioni centrali delle modifiche che riducono la protezione, report di audit,
eventi per un SIEM e termini sincronizzati (vedi [Edizioni e licenza](../enterprise/)). Sono in
programma due aggiunte:

- **I gruppi da Microsoft AD FS.** Connesso direttamente, AD FS dà a Latet il nome utente di una
  persona ma non i suoi gruppi: li aggiunge solo a una risposta rimandata con un form post. Latet
  chiederebbe quella risposta, così che i gruppi di AD FS mettano le persone nei gruppi di Latet e
  decidano chi riceve un account, come fanno i gruppi degli altri provider, senza Keycloak in mezzo.
- **Disconnettere dall'elenco I tuoi computer.** Una persona potrebbe disconnettere un suo computer,
  per esempio uno che non ha più, dall'elenco **I tuoi computer** nella dashboard, senza doverlo
  chiedere a un amministratore.

## Altre lingue

La dashboard, il tutorial e la documentazione sono disponibili in inglese, italiano, tedesco,
francese e spagnolo. Per altre lingue i contributi sono benvenuti: vedi
[Contribuire](../contributing/#come-aggiungere-una-lingua).
