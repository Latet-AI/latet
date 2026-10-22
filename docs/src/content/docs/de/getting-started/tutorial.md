---
title: Tutorial
description: Sehen Sie Latet in etwa 30 Minuten auf Ihrem Rechner bei der Arbeit, mit einem simulierten Agenten, einem simulierten Modell und erfundenen Daten, bevor Sie einen echten Agenten über Latet laufen lassen.
---

`latet tutorial` zeigt Latet in Ihrem Browser bei der Arbeit, in etwa 30 Minuten, bevor Sie einen
echten Agenten über Latet laufen lassen. Ein simulierter Agent sendet seine Anfragen an ein
simuliertes Modell. Latet dazwischen ist echt: dieselbe Maskierung, dieselben Prüfungen und dasselbe
Dashboard wie mit Ihrem eigenen Agenten. Die Daten sind erfunden, und nichts verlässt Ihren Rechner.

## Starten

```sh
latet tutorial
```

Der Befehl öffnet das Tutorial in Ihrem Browser, im Kapitelmenü, und läuft, bis Sie es beenden. Das
Terminal nennt die Adresse des Tutorials und dass Strg+C es beendet:

```text
Tutorial at http://127.0.0.1:52817 — Ctrl+C ends it
```

| Befehl | Was er tut |
| --- | --- |
| `latet tutorial` | Öffnet das Kapitelmenü. |
| `latet tutorial terms` | Öffnet ein einzelnes Kapitel über seine ID: hier *Ihre eigenen Begriffe*. |
| `latet tutorial --list` | Listet die Kapitel auf, mit ihrer Dauer in Minuten und welche Sie abgeschlossen haben, und endet dann. |
| `latet tutorial --print-url` | Gibt den Anmeldelink aus, statt den Browser zu öffnen. Er funktioniert einmal, zwei Minuten lang. |
| `latet tutorial --keep` | Behält den Ordner der Sandbox, wenn das Tutorial endet, und sagt, wo er liegt. |

Das Tutorial läuft auf Ihrem Rechner, nicht im [Container](../docker/) von Latet.

## Die Kapitel

Jedes Kapitel besteht aus wenigen Schritten. Sie senden dem Agenten einen Prompt, der schon für Sie
eingegeben ist, und der Bereich von Latet zeigt, was mit seiner Anfrage geschehen ist, in den
Ansichten des Dashboards selbst. Manche Schritte bitten Sie, dort zu handeln: auf einen Wert klicken,
ein Wort zu Ihren Begriffen hinzufügen, eine Anfrage freigeben, Ihre Hinweise speichern.

| Kapitel | Minuten | Was passiert |
| --- | --- | --- |
| `basics`: Die Grundlagen | 5 | Ein Schlüssel in `.env`, eine E-Mail-Adresse und eine Telefonnummer verlassen den Rechner als Ersatzwerte; die Datei, die der Agent schreibt, enthält den echten Schlüssel. |
| `terms`: Ihre eigenen Begriffe | 3 | Ein Codename verlässt den Rechner im Klartext, bis Sie ihn zu Ihren Begriffen hinzufügen; ab dann erhält das Modell einen Ersatzwert, einen Namen, den es nie gesehen hat. |
| `false-positives`: Ein grundlos maskierter Wert | 3 | Die Testkarte von Stripe wird wie die Karte eines Kunden maskiert; *Nicht sensibel* lässt sie durch. |
| `stopped`: Eine gestoppte Anfrage | 3 | Ein Schlüssel, eingefügt in eine Unterhaltung aus der Zeit, bevor Latet aktiv war, stoppt die Anfrage; eine neue Unterhaltung ohne ihn geht durch. |
| `live`: Der Live-Prüfer | 3 | Eine Anfrage wartet auf Ihre Freigabe: Sie maskieren den Namen eines Kunden von Hand und geben sie dann frei. |
| `settings`: Ein Agent, der Ihre Regeln lockert | 3 | Der Agent bittet Latet, E-Mail-Adressen durchzulassen, und die Änderung wartet auf Sie: Geben Sie sie frei, oder machen Sie sie rückgängig. |
| `data`: Produktionsdaten | 4 | Der strikte Datenmodus maskiert jeden Wert der Tabelle einer Abfrage; Sie lassen IDs, Datumsangaben und Pläne im Klartext. |
| `llm`: Der lokale Prüfer | 4 | Ein Ticket enthält eine ausgeschriebene E-Mail-Adresse, eine IBAN mit einem Tippfehler, einen Namen und eine Diagnose: Kein Muster findet sie, der lokale Prüfer schon. Mit Ihren Hinweisen findet er dann auch die Stationen des Krankenhauses. Der Prüfer ist simuliert: Es ist der Tutorial Reviewer. |
| `images`: Screenshots | 3 | Der Agent sendet einen Screenshot einer abgelehnten Zahlung: Latet liest darin die E-Mail-Adresse, die IBAN und die Telefonnummer und überdeckt jeden dieser Werte mit seinem Ersatzwert, bevor das Bild Ihren Rechner verlässt. Das Kapitel braucht Texterkennung: `latet ocr install`. |

Ein Kapitel endet mit dem, was Sie gelernt haben, und wo Sie es im Dashboard finden. Das letzte endet
damit, wie Sie Latet mit Ihrem eigenen Agenten verwenden. Das Menü markiert die Kapitel, die Sie
abgeschlossen haben, von einem Durchlauf zum nächsten, und *Neu beginnen* setzt ein Kapitel auf
seinen Anfang zurück.

## Eine Sandbox

Die Kapitel maskieren Werte, stoppen Anfragen und ändern Einstellungen, daher laufen sie auf einem
eigenen Latet, in einer Sandbox:

- **Eigene Daten, Einstellungen und ein eigener Verlauf**, in einem temporären Ordner, und ein
  eigener Server auf `127.0.0.1`, an einem freien Port. Ihr eigener Daemon bleibt unberührt und muss
  nicht laufen.
- **Nichts von Ihnen ändert sich:** Ihre Einstellungen, Ihr Tresor und Ihr Verlauf und die Anmeldung
  an Ihrem Dashboard (die Seite des Tutorials meldet sich mit einem eigenen Cookie an).
- **Nichts verlässt Ihren Rechner.** Die einzige Route der Sandbox führt zum simulierten Modell, das
  im Befehl `latet tutorial` läuft: Keine Anfrage kann einen Anbieter erreichen, und weder ein
  API-Schlüssel noch eine Anmeldung ist nötig.
- **Bei jedem Durchlauf dieselben Ersatzwerte:** Die Sandbox erzeugt sie aus einem festen Schlüssel,
  nicht aus Ihrem.
- **Eine einzige Datei außerhalb:** `tutorial.json` in Ihrem
  [Datenordner](../uninstall/#3-lokale-daten-löschen-optional), mit den IDs der Kapitel, die Sie
  abgeschlossen haben.

Die Sandbox hat keine Namenserkennung und nichts von Ihrer Identität: Die Kapitel zeigen, was die
Detektoren finden und was Sie von Hand maskieren. Im Kapitel über den lokalen Prüfer ist der Prüfer
der Tutorial Reviewer: Er ist simuliert, findet, was im Kapitel steht, und keine Anfrage erreicht
einen Server. Im Kapitel über Screenshots liest die Sandbox den Text in Bildern mit Ihren eigenen
Modellen für die Texterkennung, sobald diese installiert sind (`latet ocr install`). Jede andere
Seite ihres Dashboards steht zum Erkunden bereit, mit einer Leiste oben, die Sie zum Kapitel
zurückbringt.

## Beenden

Klicken Sie auf der Seite auf *Tutorial beenden*, oder drücken Sie Strg+C im Terminal: Die Sandbox
stoppt, und ihr Ordner wird gelöscht. Ein Durchlauf, der auf andere Weise abbricht, etwa weil sein
Terminal geschlossen wurde, lässt den Ordner in Ihrem temporären Ordner zurück, und das nächste
`latet tutorial` löscht ihn. Ein Durchlauf, der wegen eines Fehlers stoppt, gibt die letzten Zeilen
seines Logs aus und behält den Ordner mit dem ganzen Log bis zum nächsten Durchlauf.

## In Ihrem eigenen Dashboard

Was die Kapitel zeigen, im Dashboard Ihres eigenen Latet (`latet dashboard`):

| Was | Wo |
| --- | --- |
| Jede Anfrage: der Text auf Ihrem Rechner und was das Modell erhalten hat | **Live**, dann die Anfrage |
| Jeder Wert, den Latet ersetzt hat, und sein Ersatzwert | **Tresor** |
| Ihre Begriffe | **Regeln** → **Begriffe** |
| Die Werte, die Sie durchlassen | **Regeln** → **Zugelassene Werte** |
| Die Anfragen, die Latet gestoppt hat, und warum | **Live**, gefiltert nach *Blockiert*, dann die Anfrage |
| Was auf Ihre Freigabe wartet: Anfragen und Änderungen an Ihren Einstellungen | **Freigaben** |
| Der Live-Prüfer, für alle Projekte oder für eines | **Regeln** → **Prüfer** → **Live-Prüfer** |
| Der strikte Datenmodus und die Spalten, die Sie im Klartext lassen | **Regeln** → **Schutz** → **Produktionsdaten** |
| Was der lokale Prüfer in einer Anfrage gefunden hat | **Live**, dann die Anfrage: *Lokaler Prüfer* |
| Der lokale Prüfer und Ihre Hinweise | **Regeln** → **Prüfer** → **Lokaler Prüfer** |
| Ein Bild in einer Anfrage, wie es gesendet wurde, und der darin gelesene Text | **Live**, dann die Anfrage: *Bilder* |
| Was mit einem Bild oder einem PDF Ihren Rechner verlässt und ob der Text in Bildern gelesen wird | **Regeln** → **Schutz** → **Produktionsdaten**: *Bilder und PDFs* |

Probieren Sie es dann mit Ihrem eigenen Agenten aus, eine Sitzung lang, mit einem erfundenen
Schlüssel: siehe [Schnellstart](../quickstart/).
