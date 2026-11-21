---
title: Serveradministration
description: Verwalten Sie einen Server von Ihrem Rechner aus und schützen Sie seine Zugangsdaten.
---

Führen Sie `latet server` in Ihrem eigenen Terminal aus. Befehle, die die Administration ändern,
verweigern die Ausführung in einem Agenten; auch `config get` und `rules export`, ins Terminal oder
in eine Datei, verweigern sie dort, weil sie Werte im Klartext ausgeben können (die Begriffe eines
API-Schlüssels, die Hinweise für den Prüfer). Status, Identitätslisten und das
Administrationsprotokoll enthalten keine Zugangsdaten.

Geben Sie den Server an und das Token an der verdeckten Eingabeaufforderung ein:

```sh
latet server --server https://latet.acme.lan status
```

`status` listet jede laufende Instanz des Servers auf, jeden Worker jeder Maschine in einer eigenen
Zeile: ihre ID, die Nummer des Workers, ob sie bereit ist, ihren Speicher und die Indizes ihrer Tresore
in MiB, ihre Bereiche und ihre Version; was eine Instanz nicht gesagt hat, erscheint als unbekannt oder
fehlt. Eine eigene Zeile, `new request members:`, sagt, wie ein Element einer Anfrage, das noch kein
Anfrageprofil nennt, für jede Identität gelesen wird: `read and checked` (`unknown_members: check`,
wobei die eigenen Einstellungen einer Identität `refuse` setzen können), `refused for every identity`,
wo die Vorgaben des Unternehmens `refuse` sperren, oder `cannot be said now`, wenn der Server seine
gemeinsamen Einstellungen oder die Vorgaben in dem Moment nicht lesen kann ([Neue Elemente in
Anfragen](../../getting-started/upgrading-to-0-11/#neue-elemente-in-anfragen)). Eine Instanz von Latet
0.10 oder älter sagt nichts über sich: Wenn eine Instanz von 0.11 antwortet, listet es eine noch
laufende von 0.10 mit ihrer ID auf, als Latet 0.10 oder älter; ein Server, der noch 0.10 ausführt,
antwortet nur mit der antwortenden Instanz. Vor einer Aktualisierung ist `latet server migrate` die
Prüfung: Es ändert keine Tabelle, solange eine Instanz irgendeiner Version läuft, eine von 0.10 noch 30
Sekunden nach ihrem Stopp, und sagt, wie viele laufen und mit welcher Version. Die Liste stammt aus
einem begrenzten Lesen der Schlüssel von Valkey: Wurde dieses Lesen an seiner Grenze abgeschnitten, sagt
`status` `instances: the list is partial, cut at its bound: an instance may be alive and not listed, and
run another release or edition`, und das Feld `instances_whole` seiner Antwort ist false (null, wenn
Valkey nicht geantwortet hat).

Auf der Maschine des Servers startet `latet server run --workers N` N Instanzen, die Worker, auf
demselben Port, jede in einem eigenen Prozess; `LATET_SERVER_WORKERS` oder `workers` in `server.yaml`
geben dasselbe an, und `--workers` hat Vorrang vor beiden, die Variable vor der Datei. Von 1 bis 64,
standardmäßig einer. Mehr als einer braucht Linux oder das Container-Image: Unter Windows lehnt
`latet server run` sie ab, und der Windows-Dienst startet einen
([mehrere Instanzen auf einer Maschine](../../concepts/server-operations/#mehrere-instanzen-auf-einer-maschine)).

Oder verwenden Sie `--admin-token-file FILE`: eine private UTF-8-Datei mit dem Token allein
oder einem Datensatz `{"url": "https://latet.acme.lan", "token": "<administration token>"}`.
Ein Datensatz wird nur für seinen Server akzeptiert. Verwenden Sie unter POSIX `chmod 600 FILE`
(oder `0400` für das Konto, das einen Container ausführt), in einem Ordner, den andere Benutzer
nicht beschreiben können. Unter Windows dürfen Datei und Ordner nur Ihrem Konto, SYSTEM und
Administrators Zugriff gewähren; folgen Sie der ausgegebenen `icacls`-Korrektur.
`--admin-token-file -` liest eine Zeile aus der Standardeingabe.

Halten Sie Administrationstokens auf einem Rechner mit Agenten aus Shellprofilen und den
Umgebungsvariablen des Kontos heraus: Prozesse desselben Benutzers können die Umgebung anderer
Prozesse lesen. `LATET_SERVER_ADMIN_TOKEN` ist für den Geheimnisspeicher einer CI oder eines
Containers bestimmt, zusammen mit `LATET_SERVER_URL`; es wird nie an einen anderen Server gesendet.

`keys add`, `keys add-token`, `keys rotate` und `admin-token issue` akzeptieren `--out FILE`: Die neue private
Datei erhält das Geheimnis, und der Befehl zeigt nur ID und Datei. Geben Sie Codes, Schlüssel
oder Administrationstokens nie in Tickets, gemeinsamen Kanälen oder Unterhaltungen mit Agenten weiter.
Mit dem Einmalcode einer Person kann jeder, der ihn verwendet, einen Rechner als diese Person
verbinden und ihr Dashboard öffnen; die Ausstellung wird protokolliert.

Verwenden Sie `admin-token issue --label "Daniel's laptop" --days 30` für ein befristetes Token.
Ein `--never`-Token braucht eine Bezeichnung und kann bis zum Widerruf alles, was ein
Administrator kann: Personen und ihre Codes, API-Schlüssel, gemeinsame Einstellungen und
Administrationstoken verwalten. Ein Token stellt andere aus, die spätestens mit ihm enden; daher kann
nur ein nie ablaufendes Token ein weiteres nie ablaufendes ausstellen; auf dem Serverrechner
erzeugt `latet server init --new-admin-token --never` eines.
Bewahren Sie dieses Token getrennt auf und verwenden Sie es nur zum Ausstellen anderer Token.
Eine CI mit diesem Token gibt jedem, der ihre Jobs ändern kann, diese Rechte dauerhaft;
geben Sie der CI ein Token mit `--days` und stellen Sie es vor Ablauf neu aus.

Entfernungen fragen in Ihrem Terminal auch mit `--yes` nach; Skripte brauchen `--yes`.
`config reset` ersetzt nach Bestätigung nur gemeinsame Einstellungen, die der Server nicht öffnen kann.
`config get` und `rules export` können Werte im Klartext ausgeben: Halten Sie die Ausgabe privat.
`log verify` zeigt den Kopf der Kette; bewahren Sie eine Kopie anderswo auf und vergleichen
Sie sie beim nächsten Mal: Das Entfernen letzter Einträge lässt die übrige Kette intakt.

## Wer verwaltet, und ein verlorenes Token

**Administrator ist, wer ein Administrationstoken besitzt.** Es gibt kein Administratorkonto:
`latet server init` gibt das erste Token an den aus, der den Server initialisiert, und jedes Token
kann weitere ausstellen. Ein Token ist keine Person: Das Administrationsprotokoll nennt das Token, das etwas
getan hat (`admin:a_3fk7…`), und ein Administrator braucht keinen eigenen Bereich auf dem Server,
kann aber wie jeder andere als Person hinzugefügt werden. Jedes Administrationstoken kann
alles, in beiden Editionen. Mit der Enterprise Edition können Personen des Unternehmens im Dashboard Rollen erhalten,
etwa die eines Auditors, der die Berichte und das Administrationsprotokoll liest und nichts ändert.

Ein Token, das ohne `--days` oder `--never` ausgestellt wurde, endet nach `admin_token_days`
(90 Tage, sofern die Einstellungen des Servers nichts anderes festlegen) oder früher, wenn zuerst das
Token endet, das es ausgestellt hat; eines mit `--days` endet nach diesen Tagen. Ein solches Token
hört, vergessen, von selbst auf zu funktionieren. Ein Token mit `--never` funktioniert, bis es
widerrufen wird: Listen Sie die Tokens von Zeit zu Zeit mit `admin-token list` auf und widerrufen
Sie die, die niemand verwendet.

**Ein Token verloren, ein anderes noch vorhanden:** Listen Sie die Tokens von einem beliebigen
Rechner aus auf, widerrufen Sie das verlorene und stellen Sie ein neues aus:

```sh
latet server --server https://latet.acme.lan admin-token list
latet server --server https://latet.acme.lan admin-token revoke a_3fk7qz2mwd
latet server --server https://latet.acme.lan admin-token issue --label "Marco" --out marco.token
```

Ohne `--days` gilt für das neue Token der Standardwert des Servers, gekappt am Ende des Tokens,
das es ausstellt. Ein Token mit `--days` kann nicht nach dem Token enden, das es ausstellt: Der
Server lehnt es ab, und `admin-token list` zeigt, wann jedes Token endet.

**Kein Token mehr übrig**, weil alle verloren oder abgelaufen sind: Dort, wo die Einstellungen des
Servers, sein Hauptschlüssel und seine Datenbank erreichbar sind, stellt
`latet server init --new-admin-token` ein neues aus (mit `--never` ein Ersatztoken, das Sie
getrennt aufbewahren). Führen Sie im Compose-Betrieb das `docker run` der Initialisierung aus
[Serverbetrieb](../../concepts/server-operations/) erneut aus, mit `server init --new-admin-token`
am Ende und wie zuvor ausgeschaltetem Logging von Docker. Wie dort führt eine Person den Befehl in
einem eigenen Terminal aus, nie ein KI-Agent: Wer ihn ausführt, erhält das neue Token, und in einem
Container kann Latets Schutz vor Agenten nicht erkennen, wer ihn ausführt. Widerrufen Sie danach die
verlorenen, falls sie noch nicht abgelaufen sind. Personen, API-Schlüssel, Einstellungen, Tresore
und Verlauf bleiben in beiden Fällen unverändert.

**Ein Token, das nach außen gelangt ist**: Widerrufen Sie es sofort mit einem anderen Token und lesen
Sie dann, was es getan hat:

```sh
latet server --server https://latet.acme.lan log --actor admin:a_3fk7qz2mwd --since 30d
```

Der Verlust des **Hauptschlüssels** ist schlimmer als der eines Tokens: Er versiegelt den Schlüssel
jedes Bereichs, und ohne ihn lässt sich kein Tresor mehr lesen. Bewahren Sie eine Kopie getrennt von
den Sicherungen der Datenbank auf, wie [Serverbetrieb](../../concepts/server-operations/) es
beschreibt.

## Werte eines API-Schlüssels vergessen

Wenn eine Person verlangt, dass ihre Daten aus dem Tresor eines API-Schlüssels verschwinden, entfernt
`keys forget` ihre Werte daraus, zusammen mit dem, was die Unterhaltungen und der Verlauf des
Schlüssels von ihnen aufbewahren, auf jeder Instanz, sobald die Aufrufe enden, die dort bereits laufen:

```sh
latet server --server https://latet.acme.lan keys forget "Shop" --file erasure.txt
```

Die Datei enthält einen Wert pro Zeile, in UTF-8; ohne `--file` geben Sie die Werte im Terminal ein,
und eine leere Zeile beendet sie. Ein Wert wird nie von der Befehlszeile genommen, die die Shell in
ihrem Verlauf behält, und der Befehl verweigert die Ausführung in einem Agenten, wie `config get`. Er fragt,
bevor er sie sendet, in Ihrem Terminal auch mit `--yes`; ein Skript braucht `--yes`. Jeder Wert wird
genau so gesucht, wie er geschrieben ist: Nennen Sie jede Form, die verschwinden soll, und auch die
Unternehmensdomain einer Adresse, die der Tresor als eigenen Wert aufbewahrt. Jeder Aufruf mit höchstens
1.000 Werten ist ein Eintrag `key.forgotten` mit der Zahl der angefragten, vergessenen und nicht im
Tresor gefundenen Werte, nie mit einem Wert. Das funktioniert auch bei einem deaktivierten Schlüssel.
Eine Anwendung tut dasselbe mit einem Token, dem Sie `--forget` gegeben haben (`keys add-token "Shop"
erasure --forget`) und das keine andere Berechtigung einschließt: siehe
[Werte vergessen](../../applications/forget/).

## Das Administrationsprotokoll lesen

`latet server log --limit 100000 --json` liest aufeinanderfolgende Seiten, zuerst die neuesten
Zeitstempel und bei gleichem Zeitstempel die höchste ID. `--limit` erlaubt höchstens 1.000.000
Einträge. Jede Anfrage fordert höchstens 10.000 an; auch eine kürzere Seite kann eine Fortsetzung
haben, weil Seiten zusätzlich eine Bytegrenze haben. Die JSON-Ausgabe enthält nur Einträge,
einen pro Zeile; Hinweise werden auf stderr ausgegeben.

`--since` schließt seinen Grenzwert ein, `--until` schließt ihn aus. `--actor` wählt eine Identität
oder ein Administrationstoken; `--action` wählt eine Aktion oder ein wörtliches Präfix mit abschließendem
`*`. Die Aktionen nennen, was sich geändert hat (`person.added`, `code.issued`, `token.added`,
`token.revoked`, `key.rotated`, `key.updated`, `settings.changed`, `shared.changed`, `admin-token.issued`,
`admin-token.revoked`), und Lesezugriffe werden unter dem Namen des Aufrufs erfasst (`status.read`,
`log.read`): `--action 'admin-token.*'` wählt jede Änderung der Administrationstokens.

Zwei Aktionen zeichnen auf, wie ein Element einer Anfrage, das noch kein Anfrageprofil nennt, gelesen
wird (`unknown_members`), nie einen Ordner oder den Namen eines Elements. `setting.defaulted` wird für
den Server einmal geschrieben, von `server`, beim ersten Start von 0.15.0: sein Ziel `unknown_members`,
der Wert `check`, auf den jede Identität umgestellt wurde, und die Version. `setting.loosened` ist eine
Freigabe, die die Einstellungen einer Person oder eines API-Schlüssels von `refuse` auf `check` stellt:
die Einstellung, der Wert, die Art ihres Bereichs (`identity`, wenn sich die Einstellungen selbst
ändern, `project`, wenn ein Projekt aufhört, sie abzulehnen) und wie viele Projekte; ihr Urheber ist
die Identität, deren Änderung es ist, oder die Person, die für sie entschieden hat. Das Speichern der
Einstellungen eines API-Schlüssels durch die Administration bleibt `settings.changed`, wie bisher.

Die Tokens eines API-Schlüssels werden mit ihrer ID genannt, nie mit ihrem Geheimnis oder ihrem Namen:
`token.added` nennt das neue Token und seine Berechtigungen, `key.rotated` das Token, das bleibt
(`token`), und das, das es ersetzt hat (`replaced`), und `key.updated` eine Änderung wie die
Tresorgrenze des Schlüssels (`vault_limit`). `apps.unmasked` ist eine Wiederherstellung über die
[API für Anwendungen](../apps-api/): das Token, wie viele Texte und wie viele Werte sie
wiederhergestellt hat, nie ein Text oder ein Wert. `--action apps.unmasked` listet auf, wer wann
wiederhergestellt hat. `key.forgotten` und `apps.forgotten` sind Werte, die aus dem Tresor eines
API-Schlüssels entfernt wurden, mit `keys forget` und über die API für Anwendungen: wie viele Werte
angefragt, vergessen und nicht gefunden wurden, bei der API auch das Token, nie ein Wert.

Zum Fortsetzen übergeben Sie den ausgegebenen Log-Cursor mit `--cursor` und genau denselben
ursprünglichen Filtern. Verwenden Sie absolute Daten oder genaue Epochensekunden, wenn Sie später
fortsetzen wollen: Eine relative Zeit wie `7d` ändert sich zwischen Aufrufen. Ersetzen Sie `--until`
nie durch eine gerundete Zeit aus der Textausgabe. Ein Cursor bezeichnet eine Position; er ist weder
ein Zugangsnachweis noch ein Beleg dafür, dass das Log unverändert ist.

Jeder Aufruf ist auf 1.000 Seitenanfragen, 128 MiB empfangene Bodys einschließlich der Antworten
auf Wiederholungen, 128 MiB ausgegebene UTF-8-Daten und 300 Sekunden für Abruf und Verarbeitung
begrenzt. JSON mit Escape-Sequenzen kann größer als der empfangene Body sein. Seiten werden
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

Die Begriffssynchronisierung von Latet Enterprise erfasst vier Aktionen, mit Namen von Quellen und Anzahlen, nie mit
einem Begriff oder einem geheimen Wert:

| Aktion | Was sie erfasst |
| --- | --- |
| `terms.source.changed` | Die Quellen der Begriffssynchronisierung wurden festgelegt (`added`, `removed`, `changed`: ihre Namen; `fields`: die in jeder geänderten Felder, nie ihre Werte) |
| `terms.secret.changed` | Der geheime Wert einer Quelle wurde festgelegt oder entfernt (`saved`, `removed`: die Namen der Quellen) |
| `terms.removed` | Begriffe, die nicht mehr in ihrer Quelle stehen, wurden entfernt (`count`, `by_type`, `source`; `unreadable`, wenn es die waren, die kein Schlüssel des Servers öffnet und keine Quelle mehr liefert) |
| `terms.sync.asked` | Eine Synchronisierung wurde angefordert (`source`: ihr Name, oder keiner für alle Quellen) |

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
Modellaufrufe, mit einer Ausnahme: Eine Anfrage oder eine WebSocket-Runde, die abgelehnt
wird, bevor eines ihrer Bilder gelesen ist, weil die Texterkennung nicht verfügbar ist,
zählt `unavailable` einmal, gleich wie viele Bilder sie enthält. Interne Wiederholungen,
Kacheln, Batches und Leserreparaturen ohne eingereichtes Bild zählen nicht zusätzlich.
Ausgenommen sind erfolgreiche OCR, manuelles Zurückhalten, Dekodierungs- oder
Pixelrichtlinienfehler, deaktivierte oder nicht installierte OCR, die exakte
Wiederholung eines von Latet übermalten Bildes oder Platzhalters und die lokale OCR
eines Rechners. Abschließende Prüfungen derselben Arbeit zählen nicht erneut. Kein Label
enthält Bildinhalt, Pfade oder Identitäten.
