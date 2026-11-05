---
title: Screenshot e immagini
description: Il testo di un'immagine letto sul tuo computer, mascherato come qualsiasi altro testo, e coperto sull'immagine con il suo valore fittizio.
---

Lo screenshot di un terminale, di un modulo o di una fattura contiene gli stessi valori del testo che
lo circonda: un indirizzo email, un IBAN, il nome di un cliente. Con il riconoscimento del testo
installato, Latet legge sul tuo computer il testo di ogni immagine contenuta in una richiesta, lo
maschera come qualsiasi altro testo, e sull'immagine copre ogni valore che trova con il suo valore
fittizio. Il modello legge il valore fittizio nell'immagine; quando lo riscrive, Latet rimette il
valore reale, come fa per il testo.

## Attivarlo

```sh
latet ocr install   # downloads the models once (about 31 MB)
```

- I modelli usano l'extra `ocr` (ONNX Runtime e Pillow), che gli installer installano insieme a
  Latet; `latet upgrade` lo aggiunge a un Latet installato senza.
- I modelli sono [PP-OCRv6 small](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx) di
  PaddleOCR, uno che trova le righe di testo e uno che le legge, con licenza Apache-2.0. Vengono da
  huggingface.co a una revisione fissata, e prima dell'uso ogni file viene confrontato con uno SHA-256
  fissato.
- Girano sulla tua CPU, in circa un secondo per uno screenshot full HD, in pochi secondi per quello
  di uno schermo 4K o 5K, e in qualche secondo in più per lo screenshot di una pagina intera. Non
  viene inviato niente da nessuna parte, e Latet si collega a huggingface.co solo quando esegui
  `latet ocr install`.
- Un daemon in esecuzione inizia a usarli subito. `latet ocr status` mostra se sono in uso;
  `latet ocr remove` li elimina. Per tenerli installati ma non usarli, imposta `ocr: off`.
- `latet global-install --ocr` li scarica insieme al resto della configurazione. In
  [Docker](../../getting-started/docker/): `docker exec latet latet ocr install --yes`.

## Cosa succede a un'immagine

1. Latet legge le righe di testo che contiene, e dove si trova ogni carattere. Un'immagine lunga più
   di circa 3.000 pixel, come lo screenshot di uno schermo 4K o 5K, di una pagina intera molto più
   alta di uno schermo o di più schermi affiancati, viene letta in parti da 2.000 a 3.000 pixel che
   si sovrappongono, a una dimensione in cui il suo testo si legge: se la si rimpicciolisse per
   leggerla tutta in una volta, il testo piccolo verrebbe letto male, o non verrebbe trovato affatto.
2. Il testo letto diventa un testo in più della richiesta, *testo in un'immagine*: i rilevatori, i
   tuoi termini, la cassaforte, il [revisore locale](../llm-review/) e il
   [Revisore Live](../live-review/) lo vedono come qualsiasi altro.
3. Ogni valore trovato viene coperto sull'immagine: un riquadro del colore dello sfondo, con sopra il
   valore fittizio scritto in lettere di circa la stessa dimensione. Un tipo il cui
   [criterio](../profiles/#criteri) è `redact` riceve una semplice barra. L'immagine mantiene il suo
   formato, senza i suoi metadati; una che risulterebbe più grande di quanto accettano i provider
   (5 MB) viene inviata come JPEG, o trattenuta se è troppo grande anche così.
4. Inviata di nuovo, dato che ogni agente reinvia la conversazione, la stessa immagine esce identica,
   byte per byte, anche dopo un riavvio: la cache del prompt del provider continua a funzionare.

Un'immagine senza niente da mascherare esce così com'era. Un'immagine che Latet non riesce a leggere
viene trattenuta: al suo posto il modello riceve una breve nota, *[image withheld by Latet: ...]*,
con il motivo. Succede a un'immagine animata, a una più grande di 20 MB o di 40 megapixel, a una che
non è un'immagine valida, a una così lunga rispetto alla sua larghezza, o così larga rispetto alla
sua altezza, che andrebbe letta in più di 16 parti (lo screenshot di una pagina lunga più di circa
29.000 pixel), e a qualsiasi immagine mentre i modelli si stanno ancora caricando (una richiesta li
attende fino a 20 secondi). Da una richiesta che Latet si limita a verificare, come quelle di Claude Code, non
si può togliere un'immagine: una con un'immagine che Latet non riesce a leggere non viene inviata.

## Quali immagini

| Cosa | Lette e mascherate |
| --- | --- |
| Immagini nei messaggi: le immagini base64 di Claude, gli URL `data:` di OpenAI Chat e Responses, Codex via HTTP e via il suo WebSocket, le immagini inline di Gemini (`inlineData`) | sì |
| Screenshot restituiti dagli strumenti: Read di Claude Code su un'immagine, server MCP come quello di un browser, il computer use di OpenAI | sì |
| Immagini indicate dal loro indirizzo (un URL `https://`, un id di file), PDF e altri documenti, audio | no: la richiesta non viene inviata |

## Le impostazioni

`images` dice cosa può uscire, `ocr` se le immagini vengono lette. Entrambe sono in **Regole** →
**Protezione** → **Dati di produzione**, nella [configurazione](../../reference/configuration/), e
in un progetto.

| `images` | Con il riconoscimento del testo | Senza |
| --- | --- | --- |
| `allow` (predefinito) | le immagini vengono lette e mascherate | non viene inviata nessuna richiesta con un'immagine |
| `mask` | vengono inviate solo le immagini lette e mascherate: una richiesta che ne contiene una che Latet non riesce a leggere (un PDF, un'immagine indicata dal suo indirizzo) non viene inviata | non viene inviata nessuna richiesta con un'immagine |
| `block` | non viene inviata nessuna richiesta che contiene un'immagine o un documento | lo stesso |

- Il [profilo `max`](../profiles/) non invia immagini, come `block`. Un tuo progetto può impostare
  `images: mask` per inviarle mascherate.
- Il `latet.yaml` di un repository può solo inviare meno immagini. Inviarne di più, dalle tue
  impostazioni o da un tuo progetto, e `ocr: off` attendono la tua approvazione quando li chiede un
  agente ([Gli agenti e le tue impostazioni](../agents-and-settings/)).
- Con `ocr: off`, come senza il riconoscimento del testo, non viene inviata nessuna richiesta con
  un'immagine.
- Un agente che viene fermato riceve un errore che dice perché e cosa fare. Senza il riconoscimento
  del testo, gli viene detto di eseguire `latet ocr install` e di iniziare una nuova conversazione,
  perché l'immagine resta nella sua cronologia:

  ```text
  Latet cannot read this image's text: text recognition is not installed. On a computer, latet ocr install installs it; on a Latet server, its administrator installs it there. Then rewind to before the image or start a new conversation, because the image stays in its history.
  ```

## Le foto e i loro metadati

Una richiesta che Latet riscrive (Codex, OpenCode e gli altri agenti in modalità gateway) invia ogni
immagine riscritta, senza metadati. Una richiesta che Latet si limita a verificare, come quelle di Claude Code,
esce come l'ha costruita l'agente, quindi lì Latet rende conto di ogni byte di ogni immagine: la
struttura del suo formato, letta secondo la sua grammatica; il testo dei suoi metadati (il commento
*Screenshot* di uno screenshot, il programma che l'ha creata, il nome del suo profilo colore), letto
dal controllo finale; e i pixel dell'immagine principale, di cui il riconoscimento del testo ha letto
il testo. Ciò che resta non viene inviato:

- una posizione (GPS) nei metadati di una foto;
- le note che il produttore della fotocamera scrive in una forma tutta sua;
- la miniatura che una fotocamera conserva dentro una foto, e qualsiasi altra immagine al suo interno,
  come le mappe di profondità e di guadagno della foto ritratto di un telefono;
- ciò che segue la fine dell'immagine, come il video di una foto animata.

All'agente viene detto come andare avanti:

```text
Latet cannot read all of this image's metadata or embedded images. Rewind to before the image or start a new conversation, because the image stays in its history. Remove the metadata before attaching it again: iOS: Share, Options, Location off; macOS Preview: Tools, Show Inspector, Remove Location Info; Windows: Properties, Details, Remove Properties and Personal Information.
```

Riporta la conversazione a prima dell'immagine, o iniziane una nuova, poi allega di nuovo l'immagine
senza i metadati: su iOS, *Condividi*, *Opzioni*, *Posizione* disattivata; in Anteprima su macOS,
*Strumenti*, *Mostra Inspector*, *Rimuovi informazioni sulla posizione*; su Windows, *Proprietà* del
file, *Dettagli*, *Rimuovi proprietà e informazioni personali*. Un normale screenshot non ha niente di
tutto questo, e passa.

Lì nemmeno un'immagine WebP originale può essere spiegata byte per byte, e viene rifiutata anche lei:
esportala come PNG, poi torna indietro o inizia una nuova conversazione. Una richiesta che Latet
riscrive invia un'immagine WebP riscritta come PNG.

## In Claude Code

In [modalità client](../../agents/claude-code/) il plugin maschera le immagini prima che Claude Code
le invii:

- **Le immagini restituite dagli strumenti** (Read su un PNG, lo screenshot di un server MCP) vengono
  sostituite dalla loro copia mascherata prima che Claude le veda. Il tuo schermo mostra l'originale.
- **Uno screenshot che incolli** non può essere modificato, solo tolto dal prompt. Quando Latet vi
  trova valori da mascherare, il plugin lo toglie e aggiunge al tuo prompt una nota con il percorso
  di una copia mascherata, `.latet/masked-<id>.png` nel tuo progetto, che Claude apre con Read. Lì
  non viene scritto niente: è il plugin stesso a rispondere a quella chiamata a Read.
- Il controllo prima di ogni richiesta riguarda anche le immagini: un'immagine con un valore in
  chiaro arrivata nella richiesta per un'altra via la ferma.

## La revisione delle immagini

- **Nel [Revisore Live](../live-review/),** il testo di un'immagine attende come qualsiasi testo, con
  l'immagine sopra: un riquadro dove ogni valore riceve il suo valore fittizio, coperti anche i valori
  che mascheri nel suo testo, e puoi trascinare un rettangolo su qualsiasi sua parte per coprirla con
  una barra, o non inviare affatto l'immagine. La tua decisione viene conservata, sigillata, per
  quell'immagine: inviata di nuovo, esce allo stesso modo.
- **Il [revisore locale](../llm-review/#immagini)** legge comunque il testo di ogni immagine. Gli si
  può mostrare anche l'immagine stessa, solo quando lo attivi tu, per trovare ciò che il
  riconoscimento del testo non ha colto.

## Nella dashboard

- Uno scambio mostra le sue immagini come sono state inviate (con `history: full`), ognuna con un
  link al testo letto al suo interno, e le segnalazioni *Immagine: mascherata* e *Immagine: trattenuta*.
- **Regole** mostra se il riconoscimento del testo è installato e in uso, e il comando che lo
  installa.

## Cosa non può fare il riconoscimento del testo

- **Non è infallibile, come per i nomi.** Il testo che non legge (molto piccolo, sbiadito, scritto a
  mano, ruotato, in un carattere stilizzato) esce così com'è. Per gli screenshot che non devono mai
  uscire, usa `images: block`, i rettangoli del Revisore Live, o il revisore locale a cui mostri le
  immagini.
- **Un valore letto male viene mascherato come è stato letto.** Una `O` letta al posto di uno `0` in
  un IBAN o in un codice fiscale italiano viene corretta grazie alle loro cifre di controllo; un altro
  valore letto male potrebbe non essere riconosciuto.
- **Il modello legge il valore fittizio.** Se lo legge male, la sua risposta contiene il valore
  fittizio letto male, al posto del quale Latet non può rimettere il valore reale.
- **I PDF e gli altri documenti non vengono letti**, né le immagini indicate dal loro indirizzo: una
  richiesta che ne contiene uno non viene inviata.
- **I codici QR e i codici a barre non vengono decodificati.** Il riconoscimento del testo legge il
  testo: i dati che un codice contiene escono con l'immagine. Non inviare un codice che contiene un
  valore che proteggi.
- **Solo testo.** Volti, firme, loghi e scrittura a mano non vengono coperti.
