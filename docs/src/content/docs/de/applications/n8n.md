---
title: Die Texte eines n8n-Workflows maskieren
description: Die Latet-Nodes für n8n analysieren, maskieren, stellen wieder her und vergessen die Texte der Items eines Workflows, über die API für Anwendungen.
sidebar:
  label: n8n
---

Die Latet-Nodes für n8n ([`@latet-ai/n8n-nodes-latet`](https://www.npmjs.com/package/@latet-ai/n8n-nodes-latet),
unter der MIT-Lizenz) rufen die [API für Anwendungen](../) eines Latet-Servers aus einem Workflow auf.
Sie analysieren, maskieren, stellen wieder her und vergessen die Texte der Items des Workflows: Ein
Workflow maskiert, was er an ein Modell oder einen Dienst sendet, und stellt wieder her, was
zurückkommt. Sie brauchen einen Latet-Server ab 0.10.0 und das Token eines API-Schlüssels; das Token
des Rechners einer Person wird abgelehnt.

## Die Nodes installieren

In n8n: **Settings > Community Nodes > Install**, dann `@latet-ai/n8n-nodes-latet` eingeben. Die
[Installationsanleitung](https://docs.n8n.io/integrations/community-nodes/installation/) von n8n hat
die Einzelheiten.

## Ein Token für den Workflow

Der Administrator des Latet-Servers erstellt ein Token mit den Berechtigungen, die der Workflow
nutzt, und keiner weiteren ([API-Schlüssel und Token](../#api-schlüssel-und-token)):

```sh
latet server --server https://latet.acme.lan keys add-token "Support desk" n8n --mask --unmask --out n8n.token
```

| Operation | Berechtigung |
| --- | --- |
| Scan | `scan` oder `mask` |
| Mask | `mask` |
| Unmask | `unmask` |
| Forget | `forget`, das keine andere Berechtigung mitbringt |

In n8n enthalten die Zugangsdaten **Latet API** die Adresse des Servers (`https://latet.acme.lan`) und
das Token, also den Inhalt von `n8n.token`. Verwenden Sie eine `https://`-Adresse: Über `http://`
gehen das Token und die Texte im Klartext. n8n prüft die Zugangsdaten mit der Analyse eines Worts,
die nichts behält und `scan` oder `mask` braucht: Ein Token, das nur wiederherstellen oder vergessen
darf, besteht die Prüfung nicht und funktioniert im Node trotzdem.

## Was jede Operation tut

Jede Operation liest den Text jedes Items aus einem Feld (**Text Field**, standardmäßig `text`) und
sendet die Items in Aufrufen von höchstens 1.000 Texten.

- **Scan** sagt, wo die Werte in jedem Text stehen, nach Typ, und behält nichts.
- **Mask** gibt jeden Text mit Ersatzwerten anstelle seiner Werte zurück, derselbe Ersatzwert für
  denselben Wert in jedem Aufruf des Schlüssels. **Kind** sagt, was die Texte sind: **Prose** für
  Text, den Menschen geschrieben haben, **Data** für Logs, Datensätze und die Ausgabe eines
  Programms ([Prosa und Daten](../#prosa-und-daten)).
- **Unmask** setzt die echten Werte wieder anstelle der Ersatzwerte des Schlüssels ein. Der Server
  schreibt jede Wiederherstellung in sein Administrationsprotokoll.
- **Forget** entfernt Werte aus dem Tresor des Schlüssels, einen pro Item (**Value Field**,
  standardmäßig `value`), so geschrieben, wie sie maskiert wurden ([Werte vergessen](../forget/)).

Ein Text, den der Server nicht beantwortet, weil sein lokaler Prüfer ihn angehalten oder sein
Dump-Schutz ihn zurückgehalten hat, kommt mit `blocked` und ohne Text heraus, damit der Text im
Klartext nicht weitergeht.

## Ein Modell, das die Werte nie sieht

Ein Workflow, der Support-Nachrichten mit einem Modell beantwortet:

1. Ein Trigger empfängt die Nachricht.
2. **Latet: Mask**, Kind **Prose**, maskiert die Nachricht.
3. Der Node des Modells (OpenAI, Anthropic, Gemini, ein AI Agent) bekommt die maskierte Nachricht in
   seinem Prompt.
4. **Latet: Unmask**, mit **Text Field** auf dem Feld, das die Antwort des Modells enthält, setzt die
   echten Werte wieder ein.
5. Der Workflow sendet die Antwort.

Das Modell sieht nur Ersatzwerte, dieselben über die Nachrichten eines Kunden hinweg, und der Kunde
liest die echten Werte. Geben Sie einem KI-Agenten die Operation Unmask nicht als Werkzeug: Sie würde
dem Modell genau die Werte geben, die das Maskieren vor ihm verbirgt.

Die [README](https://github.com/Latet-AI/n8n-nodes-latet#readme) des Pakets listet jedes Feld, das die
Nodes schreiben, und die Fehler, die sie melden.
