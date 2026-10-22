---
title: Serververwaltung
description: Verwalten Sie einen Server von Ihrem Rechner aus und schützen Sie seine Zugangsdaten.
---

Führen Sie `latet server` in Ihrem eigenen Terminal aus. Befehle, die die Verwaltung ändern,
lehnen einen Agenten ab; auch `config get` und `rules export`, ins Terminal oder in eine Datei,
lehnen ihn ab, weil sie Werte im Klartext ausgeben können (die Begriffe eines API-Schlüssels, die
Hinweise für den Prüfer). Status, Identitätslisten und Verwaltungslog enthalten keine Zugangsdaten.

Geben Sie den Server an und das Token an der verdeckten Eingabeaufforderung ein:

```sh
latet server --server https://latet.acme.lan status
```

Oder verwenden Sie `--admin-token-file FILE`: eine private UTF-8-Datei mit dem Token allein
oder einem Datensatz `{"url": "https://latet.acme.lan", "token": "<administration token>"}`.
Ein Datensatz wird nur für seinen Server akzeptiert. Verwenden Sie unter POSIX `chmod 600 FILE`
(oder `0400` für das Konto, das einen Container ausführt), in einem Ordner, den andere nicht
beschreiben können. Unter Windows dürfen Datei und Ordner nur Ihrem Konto, SYSTEM und
Administrators Zugriff gewähren; folgen Sie der ausgegebenen `icacls`-Korrektur.
`--admin-token-file -` liest eine Zeile aus der Standardeingabe.

Halten Sie Verwaltungstoken auf einem Rechner mit Agenten aus Shellprofilen und den
Umgebungsvariablen des Kontos heraus: Prozesse desselben Benutzers können die Umgebung anderer
Prozesse lesen. `LATET_SERVER_ADMIN_TOKEN` ist für den Geheimnisspeicher einer CI oder eines
Containers bestimmt, zusammen mit `LATET_SERVER_URL`; es wird nie an einen anderen Server gesendet.

`keys add`, `keys rotate` und `admin-token issue` akzeptieren `--out FILE`: die neue private
Datei erhält das Geheimnis, und der Befehl zeigt nur ID und Datei. Geben Sie Codes, Schlüssel
oder Verwaltungstoken nie in Tickets, gemeinsamen Kanälen oder Gesprächen mit Agenten weiter.
Mit dem Einmalcode einer Person kann jeder, der ihn verwendet, einen Rechner als diese Person
verbinden und ihr Dashboard öffnen; die Ausstellung wird protokolliert.

Verwenden Sie `admin-token issue --label "Daniel's laptop" --days 30` für ein befristetes Token.
Ein `--never`-Token braucht eine Bezeichnung und kann bis zum Widerruf alles, was ein
Administrator kann: Personen und ihre Codes, API-Schlüssel, gemeinsame Einstellungen und
Verwaltungstoken verwalten. Ein Token stellt andere aus, die spätestens mit ihm enden; daher kann
nur ein nie ablaufendes Token ein weiteres nie ablaufendes ausstellen; auf dem Serverrechner
erzeugt `latet server init --new-admin-token --never` eines.
Bewahren Sie dieses Token getrennt auf und verwenden Sie es nur zum Ausstellen anderer Token.
Eine CI mit diesem Token gibt jedem, der ihre Jobs ändern kann, diese Rechte dauerhaft;
geben Sie der CI ein Token mit `--days` und stellen Sie es vor Ablauf neu aus.

Entfernungen fragen in Ihrem Terminal auch mit `--yes` nach; Skripte brauchen `--yes`.
`config reset` ersetzt nach Bestätigung nur gemeinsame Einstellungen, die der Server nicht öffnen kann.
`config get` und `rules export` können Werte im Klartext ausgeben: halten Sie die Ausgabe privat.
`log verify` zeigt den Kopf der Kette; bewahren Sie eine Kopie anderswo auf und vergleichen
Sie sie beim nächsten Mal: Das Entfernen letzter Einträge lässt die übrige Kette intakt.

## Wer verwaltet, und ein verlorenes Token

**Administrator ist, wer ein Verwaltungstoken besitzt.** Es gibt kein Administratorkonto:
`latet server init` gibt das erste Token an den aus, der den Server initialisiert, und jedes Token
kann weitere ausstellen. Ein Token ist keine Person: Das Verwaltungslog nennt das Token, das etwas
getan hat (`admin:a_3fk7…`), und ein Administrator braucht keinen eigenen Bereich auf dem Server,
kann aber wie jeder andere als Person hinzugefügt werden. In der Community Edition kann jedes Token
alles; Rollen, etwa ein Prüfer mit reinem Lesezugriff, sind für die Enterprise Edition geplant.

Ein Token, das ohne `--days` oder `--never` ausgestellt wurde, endet nach `admin_token_days`
(90 Tage, sofern die Einstellungen des Servers nichts anderes sagen) oder früher, wenn zuerst das
Token endet, das es ausgestellt hat; eines mit `--days` endet nach diesen Tagen. Ein solches Token
hört, vergessen, von selbst auf zu funktionieren. Ein Token mit `--never` funktioniert, bis es
widerrufen wird: Listen Sie die Token von Zeit zu Zeit mit `admin-token list` auf und widerrufen
Sie die, die niemand verwendet.

**Ein Token verloren, ein anderes noch vorhanden:** Listen Sie die Token von einem beliebigen
Rechner aus auf, widerrufen Sie das verlorene und stellen Sie ein neues aus:

```sh
latet server --server https://latet.acme.lan admin-token list
latet server --server https://latet.acme.lan admin-token revoke a_3fk7qz2mwd
latet server --server https://latet.acme.lan admin-token issue --label "Marco" --out marco.token
```

Ohne `--days` gilt für das neue Token die Voreinstellung des Servers, gekappt am Ende des Tokens,
das es ausstellt. Ein Token mit `--days` kann nicht nach dem Token enden, das es ausstellt: Der
Server lehnt es ab, und `admin-token list` zeigt, wann jedes Token endet.

**Kein Token mehr übrig**, weil alle verloren oder abgelaufen sind: Dort, wo die Einstellungen des
Servers, sein Hauptschlüssel und seine Datenbank erreichbar sind, stellt
`latet server init --new-admin-token` ein neues aus (mit `--never` ein Ersatztoken, das Sie
getrennt aufbewahren). Führen Sie im Compose-Betrieb das `docker run` der Initialisierung aus
[Serverbetrieb](../../concepts/server-operations/) erneut aus, mit `server init --new-admin-token`
am Ende und wie zuvor ausgeschaltetem Logging von Docker. Widerrufen Sie danach die verlorenen,
falls sie noch nicht abgelaufen sind. Personen, API-Schlüssel, Einstellungen, Tresore und Verlauf
bleiben in beiden Fällen unverändert.

**Ein Token, das bekannt geworden ist**: Widerrufen Sie es sofort mit einem anderen Token und lesen
Sie dann, was es getan hat:

```sh
latet server --server https://latet.acme.lan log --actor admin:a_3fk7qz2mwd --since 30d
```

Der Verlust des **Hauptschlüssels** ist schlimmer als der eines Tokens: Er versiegelt den Schlüssel
jedes Bereichs, und ohne ihn lässt sich kein Tresor mehr lesen. Bewahren Sie eine Kopie getrennt von
den Sicherungen der Datenbank auf, wie [Serverbetrieb](../../concepts/server-operations/) es
beschreibt.

## Das Verwaltungslog lesen

`latet server log --limit 100000 --json` liest aufeinanderfolgende Seiten, zuerst die neuesten
Zeitstempel und bei gleichem Zeitstempel die höchste id. `--limit` erlaubt höchstens 1.000.000
Einträge. Jede Anfrage fordert höchstens 10.000 an; auch eine kürzere Seite kann eine Fortsetzung
haben, weil Seiten zusätzlich eine Bytegrenze haben. Die JSON-Ausgabe enthält nur Einträge,
einen pro Zeile; Hinweise werden auf stderr ausgegeben.

`--since` schließt seinen Grenzwert ein, `--until` schließt ihn aus. `--actor` wählt eine Identität
oder ein Verwaltungstoken; `--action` wählt eine Aktion oder ein wörtliches Präfix mit abschließendem
`*`. Die Aktionen nennen, was sich geändert hat (`person.added`, `code.issued`, `token.revoked`,
`key.rotated`, `settings.changed`, `shared.changed`, `admin-token.issued`, `admin-token.revoked`),
und Lesezugriffe werden unter dem Namen des Aufrufs erfasst (`status.read`, `log.read`):
`--action 'admin-token.*'` wählt jede Änderung der Verwaltungstoken.
Zum Fortsetzen übergeben Sie den ausgegebenen Log-Cursor mit `--cursor` und genau denselben
ursprünglichen Filtern. Verwenden Sie absolute Daten oder genaue Epochensekunden, wenn Sie später
fortsetzen wollen: Eine relative Zeit wie `7d` ändert sich zwischen Aufrufen. Ersetzen Sie `--until`
nie durch eine gerundete Zeit aus der Textausgabe. Ein Cursor bezeichnet eine Position; er ist weder
ein Zugangsnachweis noch ein Beleg dafür, dass das Log unverändert ist.

Jeder Aufruf ist auf 1.000 Seitenanfragen, 128 MiB empfangene Antwortkörper einschließlich
Wiederholungsantworten, 128 MiB ausgegebene UTF-8-Daten und 300 Sekunden für Abruf und Verarbeitung
begrenzt. JSON mit Escape-Sequenzen kann größer als der empfangene Antwortkörper sein. Seiten werden
vor der Ausgabe geprüft und aufbereitet; eine Seite, die nicht mehr passt, wird nicht teilweise
ausgegeben. Diese Zeitprüfungen können keine blockierte Ausgabepipe des Betriebssystems unterbrechen.

Ein erreichtes Budget, eine ungültige Seite oder ein Serverfehler beendet den Aufruf mit einem
Fehlercode. Die Meldung nennt die Zahl ausgegebener Einträge und sagt, dass der Abruf unvollständig
ist. Ein geprüfter Cursor zum Fortsetzen verweist, soweit verfügbar, hinter die zuletzt ausgegebene
Seite. Überschreitet die erste Seite das Ausgabebudget, fordern Sie weniger Zeilen an. Eine Ablehnung
wie `log_entry_too_large` überspringt den betroffenen Eintrag nicht. Das Erreichen Ihrer gewünschten
Zeilenzahl ist ein normaler Abschluss; ein Hinweis sagt, ob eine Fortsetzung bleibt.

Die erste Seite legt eine Einfügegrenze fest: Später hinzugefügte Einträge gehören nicht zu diesem
Abruf, auch wenn sie frühere Zeitstempel tragen. Aufbewahrungsregeln oder Löschungen können ungelesene
Einträge trotzdem entfernen. Das Ende bedeutet nur, dass bei diesen Lesezugriffen keine weiteren
passenden Zeilen sichtbar waren; es ist weder ein unveränderlicher Export noch eine Kettenprüfung.
Verwenden Sie `log verify` gesondert und beachten Sie die oben beschriebene Grenze bei entfernten letzten Einträgen.

## Metriken für ungelesene Bilder

`latet_ocr_unread_total{reason}` zählt auf jeder Serverinstanz Bildvorkommen, deren
abschließendes OCR-Ergebnis ungelesen ist: sowohl bei einer abgelehnten geprüften
Anfrage als auch bei einem zurückgehaltenen umgeschriebenen Bild. Das einzige Label
ist `reason`, mit fünf regulären Werten:

| Grund | Bedeutung |
| --- | --- |
| `busy` | Identität, Leser oder Pool konnten das Bild innerhalb der Warte- oder Warteschlangengrenze nicht zulassen. |
| `too_long` | Das Lesen überschritt sein Zeitbudget. |
| `too_many_tiles` | Das Bild benötigte mehr Kacheln, als OCR erlaubt. |
| `too_wide` | Eine Textzeile überschritt die Breitengrenze des Erkenners. |
| `unavailable` | OCR wurde geladen, konnte nicht geladen werden oder stieß auf einen anderen behandelten Ausführungsfehler. |

Ein späteres Vorkommen desselben Originalbildes zählt erneut, wenn es einen
gespeicherten OCR-Fehler erhält. Der Zähler misst erfolglose Bildvorkommen, keine
Modellaufrufe. Interne Wiederholungen, Kacheln, Batches und Leserreparaturen ohne
eingereichtes Bild zählen nicht zusätzlich. Ausgenommen sind erfolgreiche OCR,
manuelles Zurückhalten, Dekodierungs- oder Pixelrichtlinienfehler, deaktivierte
oder nicht installierte OCR, die exakte Wiederholung eines von Latet übermalten
Bildes oder Platzhalters und die lokale OCR eines Rechners. Abschließende Prüfungen
derselben Arbeit zählen nicht erneut. Kein Label enthält Bildinhalt, Pfade oder Identitäten.
