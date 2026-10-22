---
title: Screenshots und Bilder
description: Der Text in einem Bild, auf Ihrem Rechner gelesen, maskiert wie jeder andere Text und auf dem Bild mit seinem Ersatzwert überdeckt.
---

Ein Screenshot eines Terminals, eines Formulars oder einer Rechnung enthält dieselben Werte wie der
umgebende Text: eine E-Mail-Adresse, eine IBAN, den Namen eines Kunden. Ist die Texterkennung
installiert, liest Latet auf Ihrem Rechner den Text jedes Bildes, das eine Anfrage enthält, maskiert
ihn wie jeden anderen Text und übermalt jeden Wert, den es findet, mit seinem Ersatzwert. Das Modell
liest den Ersatzwert im Bild; schreibt es ihn zurück, setzt Latet den echten Wert wieder ein, wie
bei Text.

## Einschalten

```sh
latet ocr install   # downloads the models once (about 31 MB)
```

- Die Modelle laufen mit dem Extra `ocr` (ONNX Runtime und Pillow), das die Installer zusammen mit
  Latet installieren; `latet upgrade` fügt es einer Latet-Installation hinzu, der es fehlt.
- Es sind die Modelle [PP-OCRv6 small](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx)
  von PaddleOCR, eines, das die Textzeilen findet, und eines, das sie liest, unter der Lizenz
  Apache-2.0. Sie stammen von huggingface.co in einer festgelegten Revision, und jede Datei wird vor
  der Verwendung mit einem festgelegten SHA-256-Hash abgeglichen.
- Sie laufen auf Ihrer CPU, in etwa einer Sekunde für einen Full-HD-Screenshot, in wenigen Sekunden
  für den eines 4K- oder 5K-Bildschirms und in einigen Sekunden mehr für den Screenshot einer ganzen
  Seite. Nichts wird irgendwohin gesendet, und Latet verbindet sich nur dann mit huggingface.co,
  wenn Sie `latet ocr install` ausführen.
- Ein laufender Daemon verwendet sie sofort. `latet ocr status` zeigt, ob sie verwendet werden;
  `latet ocr remove` löscht sie. Um sie installiert zu lassen, aber nicht zu verwenden, setzen Sie
  `ocr: off`.
- `latet global-install --ocr` lädt sie zusammen mit der restlichen Einrichtung herunter. In
  [Docker](../../getting-started/docker/): `docker exec latet latet ocr install --yes`.

## Was mit einem Bild geschieht

1. Latet liest die Textzeilen darin und wo jedes Zeichen steht. Ein Bild, das länger als etwa
   3.000 Pixel ist, etwa der Screenshot eines 4K- oder 5K-Bildschirms, einer ganzen Seite, die viel
   höher als ein Bildschirm ist, oder mehrerer Bildschirme nebeneinander, wird in sich überlappenden
   Teilen von 2.000 bis 3.000 Pixeln gelesen, in einer Größe, in der sein Text lesbar ist: Würde es
   verkleinert, um es auf einmal zu lesen, würde kleiner Text falsch oder gar nicht gelesen.
2. Der gelesene Text wird zu einem weiteren Text der Anfrage, *Text in einem Bild*: Die Detektoren,
   Ihre Begriffe, der Tresor, der [lokale Prüfer](../llm-review/) und der
   [Live-Prüfer](../live-review/) sehen ihn wie jeden anderen.
3. Jeder gefundene Wert wird auf dem Bild überdeckt: ein Kasten in der Farbe des Hintergrunds,
   darauf der Ersatzwert in Buchstaben etwa derselben Größe. Ein Typ, dessen
   [Richtlinie](../profiles/#richtlinien) `redact` ist, erhält einen einfachen Balken. Das Bild behält
   sein Format, ohne seine Metadaten; eines, das größer würde, als Anbieter annehmen (5 MB), wird als
   JPEG gesendet oder zurückgehalten, wenn selbst das zu groß ist.
4. Erneut gesendet, wie es jeder Agent mit der Unterhaltung tut, verlässt dasselbe Bild Ihren
   Rechner Byte für Byte gleich, auch nach einem Neustart: Der Prompt-Cache des Anbieters
   funktioniert weiter.

Ein Bild, in dem es nichts zu maskieren gibt, wird unverändert gesendet. Ein Bild, das Latet nicht
lesen kann, wird zurückgehalten: Das Modell erhält an seiner Stelle einen kurzen Hinweis,
*[image withheld by Latet: ...]*, mit dem Grund. Das betrifft ein animiertes Bild, eines, das größer
als 20 MB oder 40 Megapixel ist, eines, das kein gültiges Bild ist, eines, das für seine Breite so
lang oder für seine Höhe so breit ist, dass es in mehr als 16 Teilen gelesen werden müsste (der
Screenshot einer Seite, die länger als etwa 29.000 Pixel ist), und jedes Bild, solange die Modelle
noch laden (eine Anfrage wartet bis zu 20 Sekunden auf sie).

## Welche Bilder

| Was | Gelesen und maskiert |
| --- | --- |
| Bilder in den Nachrichten: die base64-Bilder von Claude, die `data:`-URLs von OpenAI Chat und Responses, Codex über HTTP und über seinen WebSocket, die Inline-Bilder von Gemini (`inlineData`) | ja |
| Screenshots, die Tools zurückgeben: Read von Claude Code auf einem Bild, MCP-Server wie der eines Browsers, Computer Use von OpenAI | ja |
| Per Adresse eingebundene Bilder (eine `https://`-URL, eine Datei-ID), PDFs und andere Dokumente, Audio | nein: unverändert gesendet, mit der Markierung *Nicht gelesen*, oder nicht gesendet (siehe unten) |

## Die Einstellungen

`images` legt fest, was gesendet werden darf, `ocr`, ob Bilder gelesen werden. Beide finden Sie
unter **Regeln** → **Schutz** → **Produktionsdaten**, in der
[Konfiguration](../../reference/configuration/) und in einem Projekt.

| `images` | Mit Texterkennung | Ohne |
| --- | --- | --- |
| `allow` (Standard) | Bilder werden gelesen und maskiert | Bilder werden unverändert gesendet, mit der Markierung *Nicht gelesen* |
| `mask` | nur gelesene und maskierte Bilder werden gesendet: Eine Anfrage, die eines enthält, das Latet nicht lesen kann (ein PDF, ein per Adresse eingebundenes Bild), wird nicht gesendet | keine Anfrage mit einem Bild wird gesendet |
| `block` | keine Anfrage, die ein Bild oder ein Dokument enthält, wird gesendet | ebenso |

- Das [Profil `max`](../profiles/) sendet keine Bilder, wie `block`. Ein eigenes Projekt kann
  `images: mask` setzen, um sie maskiert zu senden.
- Die `latet.yaml` eines Repositorys kann nur bewirken, dass weniger Bilder gesendet werden. Mehr
  Bilder zu senden, über Ihre Einstellungen oder ein eigenes Projekt, und `ocr: off` warten auf Ihre
  Freigabe, wenn ein Agent sie verlangt ([Agenten und Ihre Einstellungen](../agents-and-settings/)).
- Ein Agent, der gestoppt wird, erhält einen Fehler, der sagt, warum und was zu tun ist.

## In Claude Code

Im [Client-Modus](../../agents/claude-code/) maskiert das Plugin die Bilder, bevor Claude Code sie
sendet:

- **Bilder, die Tools zurückgeben** (Read auf einer PNG-Datei, der Screenshot eines MCP-Servers),
  werden durch ihre maskierte Kopie ersetzt, bevor Claude sie sieht. Ihr Bildschirm zeigt das
  Original.
- **Ein Screenshot, den Sie einfügen,** lässt sich nicht verändern, nur aus dem Prompt entfernen.
  Findet Latet darin Werte zum Maskieren, entfernt das Plugin ihn und fügt Ihrem Prompt einen Hinweis
  mit dem Pfad einer maskierten Kopie hinzu, `.latet/masked-<id>.png` in Ihrem Projekt, die Claude
  mit Read öffnet. Dort wird nichts geschrieben: Das Plugin beantwortet diesen Read-Aufruf selbst.
- Die Prüfung vor jeder Anfrage erfasst auch Bilder: Ein Bild mit einem Wert im Klartext, das auf
  anderem Weg in die Anfrage gelangt ist, stoppt sie.

## Bilder prüfen

- **Im [Live-Prüfer](../live-review/)** wartet der Text eines Bildes wie jeder Text, mit dem Bild
  darüber: ein Rahmen, wo jeder Wert seinen Ersatzwert erhält, die Werte, die Sie in seinem Text
  maskieren, ebenfalls überdeckt, und Sie können ein Rechteck über einen beliebigen Teil ziehen, um
  ihn mit einem Balken zu überdecken, oder das Bild gar nicht senden. Ihre Entscheidung wird für
  dieses Bild versiegelt aufbewahrt: Wird es erneut gesendet, geschieht das auf dieselbe Weise.
- **Der [lokale Prüfer](../llm-review/#bilder)** liest ohnehin den Text jedes Bildes. Ihm kann, nur
  wenn Sie das einschalten, auch das Bild selbst gezeigt werden, damit er findet, was die
  Texterkennung übersehen hat.

## Im Dashboard

- Ein Austausch zeigt seine Bilder so, wie sie gesendet wurden (mit `history: full`), jedes mit
  einem Link zu dem darin gelesenen Text, und die Markierungen *Bild: maskiert* und
  *Bild: zurückgehalten*.
- **Regeln** zeigt, ob die Texterkennung installiert ist und verwendet wird, und den Befehl, der sie
  installiert.

## Was die Texterkennung nicht kann

- **Wie bei Namen gibt es keine Garantie.** Text, den Latet nicht liest (sehr klein, blass,
  handschriftlich, gedreht, in einer stilisierten Schrift), wird unverändert gesendet. Für
  Screenshots, die Ihren Rechner nie verlassen dürfen, verwenden Sie `images: block`, die Rechtecke
  des Live-Prüfers oder den lokalen Prüfer, dem Sie die Bilder zeigen.
- **Ein falsch gelesener Wert wird so maskiert, wie er gelesen wurde.** Ein `O`, das in einer IBAN
  oder einer italienischen Steuernummer statt einer `0` gelesen wird, wird durch deren Prüfziffern
  korrigiert; ein anderer falsch gelesener Wert wird möglicherweise nicht erkannt.
- **Das Modell liest den Ersatzwert.** Liest es ihn falsch, enthält seine Antwort den falsch
  gelesenen Ersatzwert, für den Latet den echten Wert nicht wieder einsetzen kann.
- **PDFs und andere Dokumente werden nicht gelesen**, ebenso wenig per Adresse eingebundene Bilder:
  siehe die Tabelle oben.
- **Nur Text.** Gesichter, Unterschriften, Logos und Handschrift werden nicht überdeckt.
