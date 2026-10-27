---
title: Werte vergessen
description: Die Werte einer Person aus dem Tresor eines API-Schlüssels entfernen, wenn sie verlangt, dass ihre Daten verschwinden.
sidebar:
  label: Werte vergessen
---

Der Tresor eines API-Schlüssels bewahrt jeden Wert, den er maskiert hat, mit seinem Ersatzwert auf,
solange der Schlüssel besteht: Eine Wiederherstellung funktioniert auch bei einer Logzeile vom letzten
Jahr. Wenn eine Person verlangt, dass ihre Daten aus Ihren Logs oder aus dem Index Ihrer RAG-Anwendung
verschwinden, vergisst der Server ihre Werte. Jeder Wert verlässt den Tresor des Schlüssels, sein
Ersatzwert stellt von da an nichts mehr wieder her, und der Wert erhält einen neuen Ersatzwert, wenn
ein Text ihn wieder enthält.

Es gibt zwei Wege: Ein Administrator führt `latet server keys forget` aus, oder eine Anwendung ruft die
API für Anwendungen mit einem Token auf, der die Berechtigung `forget` hat. Beide tun dasselbe, und
beide stehen im Administrationsprotokoll, mit Zahlen und nie mit einem Wert.

## Der Administrator

Schreiben Sie die Werte in eine Datei, einen pro Zeile, in UTF-8:

```text
mario.rossi@acme.it
+39 347 123 4567
Mario Rossi
```

```sh
latet server --server https://latet.acme.lan keys forget "Shop" --file erasure.txt
```

Der Befehl sagt, wie viele Werte er gelesen hat, fragt, bevor er sie sendet, und sagt dann, wie viele
der Tresor des Schlüssels enthielt und wie viele nicht. Ohne `--file` bittet er Sie, die Werte im
Terminal einzugeben, einen pro Zeile; eine leere Zeile beendet sie. Er nimmt nie einen Wert von der
Befehlszeile, wo die Shell ihn in ihrem Verlauf behalten würde. In einem KI-Agenten verweigert er die
Arbeit, denn die Werte sind Daten einer Person, und in einem Skript braucht er `--yes`. Er sendet die
Werte in Aufrufen von höchstens 1.000. Jeder Aufruf ist ein Eintrag `key.forgotten` im
Administrationsprotokoll, mit der Zahl der angefragten, vergessenen und nicht im Tresor gefundenen
Werte. Löschen Sie die Datei danach: Sie enthält die Werte.

Das erste Vergessen eines Schlüssels nach einem Upgrade indiziert zuerst, was der lokale Prüfer des
Schlüssels gespeichert und sein Verlauf vor dem Upgrade aufbewahrt hat: Bei einem Schlüssel mit viel
Verlauf dauert das Minuten. Der Befehl wartet bis zu fünf Minuten auf jeden Aufruf, und der Aufruf
einer Anwendung kann ebenso lange dauern. Hört ein Aufruf auf zu warten, führen Sie ihn erneut aus:
Was der Server vergessen hat, bleibt vergessen und zählt dann als nicht im Tresor.

Das funktioniert, ob der Schlüssel aktiviert oder deaktiviert ist. Der Tresor einer Person wird nicht
auf diesem Weg geleert: Personen vergessen Werte in ihrem Dashboard (Tresor).

## Eine Anwendung

Geben Sie der Anwendung einen eigenen Token mit `forget`. Keine andere Berechtigung schließt sie ein,
und der erste Token des Schlüssels, der analysieren, maskieren und wiederherstellen darf, darf nicht
vergessen:

```sh
latet server --server https://latet.acme.lan keys add-token "Shop" erasure --forget --out erasure.token
```

Die Anwendung sendet bis zu 1.000 Werte pro Aufruf:

```sh
printf 'Authorization: Bearer %s' "$(cat erasure.token)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"values": ["mario.rossi@acme.it", "+39 347 123 4567"]}' \
  https://latet.acme.lan/_latet/apps/v1/forget
```

```json
{"forgotten": 1, "unknown": 1}
```

`forgotten` ist die Zahl der Werte, die der Tresor des Schlüssels enthielt, `unknown` die Zahl derer,
die er nicht enthielt; jeder Wert zählt einmal. Jeder Aufruf ist ein Eintrag `apps.forgotten` im
Administrationsprotokoll (der Token, die Zahl der angefragten, vergessenen und nicht gefundenen Werte),
geschrieben in derselben Transaktion, in der die Werte vergessen werden: Nimmt das Log ihn nicht an,
wird nichts vergessen, und der Aufruf antwortet mit 503. Mit dem Python-Client sendet
`latet.forget([...])` eine längere Liste in mehreren Aufrufen und gibt `ForgetResult(forgotten,
unknown)` zurück. Ein Token ohne die Berechtigung erhält 403.

## Was entfernt wird

- **Jeder Wert so, wie er geschrieben ist.** Andere Groß- und Kleinschreibung, andere Leerzeichen oder
  eine andere Schreibweise (`mario.rossi%40acme.it` in einer URL) sind ein anderer Wert. Der Tresor
  bewahrt jede Form, der er begegnet ist, als eigenen Eintrag auf; nennen Sie also jede Form, die
  verschwinden soll.
- **Alles im Tresor, was ihn nennt:** sein Eintrag und sein Ersatzwert, versiegelt mit dem
  Wurzelschlüssel des API-Schlüssels, die Hashes, die der Index von ihren Wörtern aufbewahrt, und die
  gespeicherten Antworten des lokalen Prüfers, die den Wert gefunden haben, ob der Tresor ihn enthielt
  oder nicht. Danach entfernt jede Instanz ihn aus ihrem Speicher, zusammen mit den Texten, die sie
  mit seinem Ersatzwert maskiert vorhält, sobald die Aufrufe enden, die dort bereits laufen.
- **Was die Agenten des Schlüssels in Latet hinterlassen haben.** Wenn der erste Token des
  Schlüssels Agenten über das Gateway bedient, verschwindet der Eintrag des Werts auch aus ihren
  Unterhaltungen, und der Verlauf des Schlüssels verliert die Texte, die Antworten des Modells und
  die Funde des lokalen Prüfers, die ihn nannten, mit seinem alten Ersatzwert. Eine frühere
  Nachricht, die ein Agent erneut sendet, wird neu maskiert, mit dem neuen Ersatzwert.
- **Sein alter Ersatzwert stellt nichts mehr wieder her, und kein anderer Wert erhält ihn je.** Eine
  früher maskierte Logzeile behält den alten Ersatzwert, und die Wiederherstellung lässt ihn, wie er
  ist. Der Ersatzwert wird stillgelegt: Der Tresor behält nur seinen Hash mit Schlüssel, an keinen
  Eintrag gebunden, solange es den Tresor gibt, sodass er nie wieder für einen anderen Wert gezogen
  und eine alte Zeile nie mit dem Wert einer anderen Person wiederhergestellt wird. Ein stillgelegter
  Ersatzwert belegt keinen Platz, denn die Grenze des Tresors zählt Werte, aber er bleibt: Der Status
  des Servers und `latet_vault_near_limit` wiegen die stillgelegten Ersatzwerte eines Schlüssels mit
  seinen Werten, sodass ein Token, der in einer Schleife maskiert und vergisst, dort auffällt. Geben
  Sie `forget` nur dem Prozess, der Löschanfragen bearbeitet.
- **Der Wert erhält einen neuen Ersatzwert, wenn er wiederkommt.** Ersatzwerte und Einträge werden
  aus dem Wert gebildet, deshalb bildet der Tresor des Schlüssels sie nach einem Vergessen in einer
  neuen Generation: Der Wert erhält einen neuen Ersatzwert und einen neuen Eintrag, und der alte
  Ersatzwert kommt nie zurück. Die maskierten Texte, die Latet vorher verlassen haben, lassen sich
  über ihre Ersatzwerte nicht mit den neuen verbinden.
- **Eine Adresse hält die Domain ihres Unternehmens getrennt.** Eine Adresse mit der Domain eines
  Unternehmens (`mario.rossi@acme.it`) bewahrt die Domain als eigenen Wert auf, und die Domain des
  Ersatzwerts ist der Ersatzwert dieser Domain. Wer die Adresse vergisst, lässt die Domain stehen, also
  wird der Domain-Teil des alten Ersatzwerts weiterhin zu `acme.it` wiederhergestellt; vergessen Sie
  auch `acme.it`, wenn sie verschwinden soll. Die Domain eines E-Mail-Anbieters wie `gmail.com` wird nie
  aufbewahrt.

Was bleibt:

- die Zahlen im Administrationsprotokoll, und die Austausche im Verlauf, deren Texte entfernt wurden,
  ohne diese Texte;
- die maskierten Texte, die Latet bereits verlassen haben, in Ihrem Log-Dienst oder Ihrem Index, die
  den alten Ersatzwert enthalten und die Sie dort löschen, wenn sie verschwinden sollen;
- in Valkey, versiegelt, eine Woche lang nach ihrer letzten Verwendung: die Texte der Unterhaltungen
  der Agenten des Schlüssels, so wie sie hinausgingen, und die Antworten des Modells, so wie es sie
  geschrieben hat, die den alten Ersatzwert enthalten, nie den Wert. Eine frühere Nachricht, die
  erneut gesendet wird, wird neu maskiert; eine Antwort des Modells geht so an es zurück, wie es sie
  geschrieben hat;
- höchstens eine Woche nach der letzten Verwendung einer Unterhaltung die ID des alten Eintrags in
  dieser Unterhaltung der Agenten des Schlüssels (ein mit Schlüssel gebildeter Hash des Werts, nie der
  Wert), wenn Valkey direkt nach dem Vergessen nicht antwortete oder wenn eine Instanz die Nachricht
  darüber verpasst hat (Valkey bewahrt keine Nachrichten auf) und die ID aus einer Unterhaltung, die
  sie vorhält, zurückgeschrieben hat;
- was PostgreSQL selbst von gelöschten Zeilen aufbewahrt, versiegelt, in seinen Dateien, bis es ihren
  Platz wiederverwendet, in seinem Write-Ahead-Log und in den Sicherungen der Datenbank.

Ein Aufruf, der läuft, während vergessen wird, kann noch den alten Ersatzwert verwenden, und was er
in diesem Moment aufbewahrt, kann den Wert noch nennen: eine Antwort des lokalen Prüfers, die ein
zweites Vergessen einen Moment später entfernt, und ein Austausch, den der Verlauf aufzeichnet, der
bleibt, solange der Verlauf ihn aufbewahrt (`retention_days`). Die Instanzen entfernen den Wert,
sobald die Aufrufe enden, die dort bereits laufen.

## Wer was erfahren kann

Die Antwort sagt, wie viele Werte der Tresor enthielt; ein Token, der vergessen darf, erfährt also, ob
ein Wert zum Schlüssel gehört, so wie einer, der analysieren oder maskieren darf
([wer was erfahren kann](../#wer-was-erfahren-kann)). Geben Sie `forget` nur dem Vorgang, der
Löschanfragen bearbeitet, und lesen Sie das Log mit `latet server log --action apps.forgotten`.
