---
title: Die API für Anwendungen
description: Text aus Ihren eigenen Programmen analysieren, maskieren und wiederherstellen, mit einem API-Schlüssel eines Latet-Servers.
sidebar:
  label: Überblick
---

Latet schützt, was ein Agent an ein Modell sendet. Programme senden vertraulichen Text aber auch an
andere Stellen: ihre Logs an Datadog, Splunk oder Elastic, ihre Dokumente an den Index einer
RAG-Anwendung. Mit der API für Anwendungen bittet ein Programm einen Latet-Server, mit dem Token
eines API-Schlüssels, einen Text:

- zu **analysieren** (`scan`): was darin vertraulich ist, und wo;
- zu **maskieren** (`mask`): derselbe Text mit realistischen Ersatzwerten anstelle seiner Werte;
- **wiederherzustellen** (`unmask`): die echten Werte wieder anstelle der Ersatzwerte;
- Werte zu **vergessen** (`forget`): sie aus dem Tresor des Schlüssels zu entfernen, wenn eine Person
  verlangt, dass ihre Daten verschwinden ([Werte vergessen](forget/)).

Sie ist eine Funktion des Latet-Servers: API-Schlüssel und ihre Token gibt es nur dort, und ein
Rechner ohne Server hat nichts davon. Um sie auf Ihrem eigenen Rechner auszuprobieren, starten Sie
dort einen Community-Server mit dem Serverprofil von `compose.yaml`
([Serverbetrieb](../concepts/server-operations/)).

Was sie mehr bietet als eine Schwärzungsregel:

- **Jedes Mal derselbe Ersatzwert.** Ein Wert erhält in jedem Aufruf, auf jeder Instanz des Servers
  denselben Ersatzwert, solange der Tresor des Schlüssels ihn behält. In einem Log-Dienst
  funktionieren Diagramme und Suchen weiter, und ein Kunde lässt sich über Tage von Logs verfolgen,
  ohne dass jemand sieht, wer er ist. In einem RAG-Index bleibt eine Person über alle Dokumente
  dieselbe Person.
- **Ein Tresor für den ganzen Ablauf.** Dokumente und Fragen, die mit demselben Schlüssel maskiert
  werden, teilen ihre Ersatzwerte. So arbeitet ein Modell mit stimmigen Ersatzwerten, und `unmask`
  setzt die echten Werte in seine Antwort wieder ein.
- **Umkehrbar, unter Kontrolle.** Wiederherstellen ist eine eigene Berechtigung, erreicht nur die
  Ersatzwerte des Schlüssels, und jede Wiederherstellung über die API für Anwendungen wird im
  Administrationsprotokoll des Servers festgehalten.

Zwei Seiten führen durch die beiden Einsätze, für die sie gebaut wurde: [Logs](logs/) und
[RAG](rag/). Die [HTTP-Referenz](../reference/apps-api/) beschreibt den Vertrag für Programme in jeder
Sprache, und mit der [OpenAPI-Beschreibung](../../apps-api.openapi.json) der API lässt sich ein Client
dafür erzeugen. Python-Programme können das Paket [`latet-client`](https://pypi.org/project/latet-client/)
verwenden, das mit LangChain zusammenarbeitet, und n8n-Workflows die [Latet-Nodes für n8n](n8n/).

## Ein erster Aufruf

Ein Programm sendet eine Liste von Texten und erhält eine Liste zurück, in derselben Reihenfolge:

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"texts": ["Bitte erstatten Sie die Bestellung von mario.rossi@acme.it auf IT60X0542811101000000123456."]}' \
  https://latet.acme.lan/_latet/apps/v1/mask
```

```json
{"results": [{"text": "Bitte erstatten Sie die Bestellung von tommaso.gallo@margie-6e5a.example auf IT17F3945471087320356678487.",
  "replacements": [{"type": "EMAIL", "class": "pii", "start": 39, "end": 72},
                   {"type": "IBAN", "class": "pii", "start": 77, "end": 104}],
  "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

`shop.key` enthält den ersten Token des Schlüssels, den `keys add` schreibt
([weiter unten](#api-schlüssel-und-token)).
`printf`, das die Shell selbst ausführt, übergibt den Header über die Standardeingabe an curl
(`-H @-`). So steht der Token nicht in der Befehlszeile von curl, wo andere Benutzer des Rechners ihn
lesen könnten. Die Ersatzwerte sind die des Schlüssels: Ein anderer Schlüssel oder ein anderer Server
liefert andere. Mit einem Token, der wiederherstellen darf, kommt derselbe Text, an
`/_latet/apps/v1/unmask` gesendet, so zurück, wie er war, mit `"restored": 2`.

## API-Schlüssel und Token

Ein API-Schlüssel ist ein Bereich auf dem Server: seine Regeln, sein Tresor mit Ersatzwerten, sein
Verlauf. Er kann mehrere **Token** haben, die Geheimnisse, die in diesem Bereich handeln, und jeder
Token hat seine eigenen **Berechtigungen**:

| Berechtigung | Was der Token darf |
| --- | --- |
| `scan` | Sagen, was ein Text enthält und wo; er ändert nichts. |
| `mask` | Die Werte eines Textes durch die Ersatzwerte des Schlüssels ersetzen. Ein Token, der maskieren darf, darf auch analysieren. |
| `unmask` | Die echten Werte wieder anstelle der Ersatzwerte des Schlüssels einsetzen. |
| `forget` | Werte aus dem Tresor des Schlüssels entfernen ([Werte vergessen](forget/)). Keine andere Berechtigung schließt sie ein. |

Geben Sie jedem Programm nur, was es braucht. Eine Anwendung, die ihre Logs versendet, braucht
`mask`; das Support-Werkzeug, das eine aus dem Log-Dienst kopierte Zeile liest, braucht `unmask`; eine
RAG-Anwendung, die ihre Dokumente maskiert und die Antwort des Modells wiederherstellt, braucht beides.

```sh
latet server --server https://latet.acme.lan keys add "Shop" --out shop.key
latet server --server https://latet.acme.lan keys add-token "Shop" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop" support --unmask --out support.token
latet server --server https://latet.acme.lan keys tokens "Shop"
```

Der erste Token eines Schlüssels, den `keys add` erzeugt, hat `scan`, `mask` und `unmask`, ebenso der
Token jedes Schlüssels, der angelegt wurde, bevor Token Berechtigungen hatten: Diese Schlüssel
funktionieren wie bisher. Der erste Token des Schlüssels hat außerdem eine Adresse für Modelle, das
[Gateway](../getting-started/server/#einem-programm-einen-api-schlüssel-geben), und bedient darüber die
Agenten des Schlüssels: `keys tokens` kennzeichnet ihn mit `serves agents (has an address)`, damit er
nicht anstelle eines Tokens widerrufen wird, der mit denselben Berechtigungen hinzugefügt wurde. Ein
Token, der mit `keys add-token` erstellt wurde, hat keine Adresse, welche
Berechtigungen er auch hat, `--mask --unmask` eingeschlossen: Das Gateway und die Bridge des Plugins
weisen ihn ab (403), und er ruft nur die API für Anwendungen auf.

Jeder Token lässt sich einzeln stoppen: `keys tokens "Shop" --revoke t_4kxq2m7zap` widerruft einen
anhand der ID, die `keys tokens` auflistet, innerhalb von Sekunden auf jeder Instanz. `keys rotate
"Shop" --token t_4kxq2m7zap --out shipper.token` ersetzt diesen Token durch einen neuen, mit neuem
Geheimnis und neuer ID, demselben Namen und denselben Berechtigungen; ein Schlüssel mit nur einem
Token braucht kein `--token`. Jede Änderung steht im Administrationsprotokoll (`token.added`,
`token.revoked`, `key.rotated`), mit den IDs der Token, nie mit einem Geheimnis.

Die API für Anwendungen nimmt nur Token von API-Schlüsseln an: Der Rechner-Token einer Person wird
abgewiesen (403), ebenso eine Anfrage, die eine Webseite stellt. Personen verwenden ihr Dashboard.

## Prosa und Daten

`scan` und `mask` nehmen eine Art (`kind`) für die Texte eines Aufrufs:

- `prose` (die Voreinstellung): Text, den Menschen geschrieben haben, etwa Nachrichten, Tickets und
  Dokumente. Er wird maskiert, wie das Gateway Ihre eigenen Nachrichten maskiert.
- `data`: Logs, Datensätze, die Ausgabe eines Programms. Sie werden maskiert, wie das Gateway die
  Ausgabe eines Werkzeugs maskiert.

Der Unterschied liegt in dem, was wahrscheinlich ist: Im Profil `balanced` werden Namen nur in Prosa
gesucht (mit dem auf dem Server installierten
[lokalen Namensmodell](../concepts/server-operations/#erkennungsmodelle-auf-einem-server)), und eine
Ziffernfolge zählt nur in Prosa als Telefonnummer, während sie in einem Log weit öfter eine ID ist. Mit
`phone_regions: [IT]` in den Regeln des Schlüssels erhält die Nummer in
`Rufen Sie mich morgen unter 3471234567 an.` als Prosa einen Ersatzwert und bleibt als Daten, wie sie
ist. Ein JSON-Dokument, das als Text gesendet wird, wird als Text maskiert und behält sein Layout,
sodass die Positionen in der Antwort Positionen darin sind.

## Was eine Maskierung liest

`scan` und `mask` verwenden die Einstellungen des Schlüssels, wie sie beim Eintreffen des Aufrufs
sind: die eigenen Regeln des Schlüssels über den gemeinsamen Einstellungen des Servers, wie es das
Gateway für die Anfragen des Schlüssels tut. Es gibt keine Regeln nur für die API. Sie werden mit
`latet server config --key` und `latet server rules --key` gesetzt
([gemeinsame Einstellungen und Regeln von API-Schlüsseln](../getting-started/server/#gemeinsame-einstellungen-und-regeln-von-api-schlüsseln-festlegen)).

- **Der lokale Prüfer** liest, wenn die Regeln des Schlüssels einen festlegen, jeden Text eines
  `mask`-Aufrufs vor der Antwort, was auch immer `llm_review.inline` für das Gateway sagt: Eine
  Anwendung hat keinen späteren Zeitpunkt. Er erhält `llm_review.budget_ms`, wenn gesetzt, sonst 30
  Sekunden. Ein Text, dessen Prüfung scheitert, folgt `llm_review.on_failure`: Mit `stop` (der
  Voreinstellung) lautet die Antwort `blocked`, mit `send` kommt der Text so, wie ihn die Regeln
  maskiert haben. Bei einem ausgefallenen Prüfer wird also jeder Text blockiert, bis er wieder
  antwortet.
- **`scan` führt den lokalen Prüfer nicht aus.** Der Prüfer bringt dem Tresor bei, was er findet, und
  eine Analyse mit `scan` ändert nichts. Bei einem Schlüssel mit Prüfer kann `scan` weniger melden,
  als `mask` ersetzt.
- **Der Live-Prüfer hält den Text einer Anwendung nie an.** Eine Anwendung kann nicht auf einen
  Menschen warten: Ein Text wird allein von den Regeln und dem lokalen Prüfer maskiert, auch wenn die
  Regeln des Schlüssels den Live-Prüfer einschalten.
- **Der Dump-Schutz** gilt für `data`-Texte wie für die Ausgabe eines Werkzeugs: Einer, der mehr
  personenbezogene Werte enthält, als `dump_guard` erlaubt, erhält `blocked`.

Ein blockierter Text kommt als `{"blocked": "<Grund>"}` ohne Text zurück, und der Grund nennt keinen
Wert. Wenn im Aufruf etwas eingeschränkt lief (ein Detektor, der einen Text nicht lesen konnte, eine
Prüfung, die nicht fertig wurde), sagt die Antwort das in `"degraded"`, mit den Namen dessen, was es
betraf, etwa `ner` oder `llm-review`.

## Zweimal maskiert, zweimal wiederhergestellt

`mask` hält jeden Wert, den es findet, für echt, auch die eigenen Ersatzwerte des Schlüssels: Niemand
kann einer Zeichenkette ansehen, ob sie ein Ersatzwert ist oder ein echter Wert, der wie einer aussieht.
Ein zweimal maskierter Text enthält Ersatzwerte von Ersatzwerten, und eine Wiederherstellung führt ihn
um einen Schritt zurück:

| Schritt | Text |
| --- | --- |
| Original | `Schreiben Sie an mario.rossi@acme.it` |
| Maskiert | `Schreiben Sie an tommaso.gallo@margie-6e5a.example` |
| Erneut maskiert | `Schreiben Sie an simone.desantis@humongous-0e54.example` |
| Einmal wiederhergestellt | `Schreiben Sie an tommaso.gallo@margie-6e5a.example` |

Maskieren Sie einen Text einmal, ausgehend vom Original. Das Gateway folgt derselben Regel: Ein Text,
der über die API maskiert und dann über das Gateway von Latet an ein Modell gesendet wird, wird dort
erneut maskiert, und die Antwort des Modells kommt mit den Ersatzwerten der API zurück. Eine
RAG-Anwendung sendet den maskierten Text deshalb direkt an das Modell und stellt die Antwort über die
API wieder her ([RAG](rag/)).

## Wer was erfahren kann

**Wiederherstellen** liefert die echten Werte der Ersatzwerte des Schlüssels, gleich welcher Aufruf oder
welche Unterhaltung des Schlüssels sie erzeugt hat, auch die vom letzten Jahr. Es erreicht keine
Ersatzwerte eines anderen Schlüssels und keine einer Person: Text, der kein Ersatzwert des Schlüssels
ist, bleibt, wie er ist. Der Name einer Person kommt nur als Ganzes zurück: Ein Teil davon allein (ein
Vorname) kann ein gewöhnliches Wort oder der Name einer anderen Person sein und bleibt, wie er ist. Jede
Wiederherstellung wird im Administrationsprotokoll festgehalten, bevor ihre Antwort gesendet wird
(`apps.unmasked`, mit dem Token, wie viele Texte und wie viele Werte; nie ein Text oder ein Wert), und
ein Protokoll, das sie nicht aufnehmen kann, weist den Aufruf ab (503). Der erste Token des Schlüssels,
der analysieren, maskieren und wiederherstellen darf, bedient auch Agenten über das Gateway: Ihre
Antworten werden wie die jedes Agenten wiederhergestellt, ohne einen Eintrag im Administrationsprotokoll
für jede Wiederherstellung. Ein Token, der mit `keys add-token` erstellt wurde, tut das nie, daher ist
jede seiner Wiederherstellungen ein Eintrag `apps.unmasked`. Geben Sie `unmask` den Werkzeugen der
Personen, die die echten Daten sehen
dürfen, und lesen Sie das Protokoll mit `latet server log --action apps.unmasked`.

**Auch Maskieren verrät etwas.** Mit einem Token, der maskieren darf, und Zugriff auf die maskierten
Logs kann jemand eine Vermutung bestätigen: `mario.rossi@acme.it` maskieren und in den Logs nach dem
Ersatzwert suchen. Ist er da, ist Mario Rossi ein Kunde. Geben Sie `mask` deshalb nur den Programmen,
die es brauchen, bewahren Sie den Token dort auf, wo sie ihre Geheimnisse aufbewahren, und behandeln
Sie die maskierten Logs als pseudonyme Daten, nicht als anonyme.

**Auch Analysieren verrät etwas.** Es erzeugt keinen Ersatzwert und ändert nichts, meldet aber die
Werte, die der Tresor des Schlüssels schon kennt, wo immer sie stehen. Ein Token, der nur analysieren
darf, kann also bestätigen, dass ein Wert, den kein Detektor von sich aus finden würde (ein Name in
einem `data`-Text, ein Wert, den eine Regel des Schlüssels fand, bevor sie entfernt wurde), einer des
Schlüssels ist. Vergeben Sie `scan` mit derselben Sorgfalt wie `mask`.

**Auch Vergessen verrät etwas.** Seine Antwort sagt, wie viele der Werte der Tresor des Schlüssels
enthielt; ein Token, der vergessen darf, kann also bestätigen, dass ein Wert einer des Schlüssels ist,
wie einer, der analysieren darf. Geben Sie `forget` nur dem Vorgang, der Löschanfragen bearbeitet;
jedes Vergessen steht im Administrationsprotokoll (`apps.forgotten`, mit dem Token und den Zahlen, nie
mit einem Wert). Keiner der Token, die `keys add` erzeugt, darf vergessen: `forget` erhält ein eigener
Token (`keys add-token "Shop" erasure --forget`).

## Der Tresor eines Schlüssels

Der Tresor eines Schlüssels behält jeden Wert, den er maskiert hat, mit seinem Ersatzwert, solange
der Schlüssel besteht: Eine Wiederherstellung funktioniert mit einer Logzeile vom letzten Jahr, und
ein Kunde behält jahrelang denselben Ersatzwert. Werte laufen nicht ab: Ein Wert verlässt den Tresor,
wenn er vergessen wird ([Werte vergessen](forget/)), und alle Werte gehen mit dem Schlüssel, denn
`latet server keys remove` löscht seinen Bereich und damit seinen Tresor. Ein einmal
gefundener Wert wird von da an überall maskiert, wo er auftaucht, auch dort, wo kein Detektor ihn
wieder finden würde: Die Adresse eines Kunden, die eine inzwischen entfernte Regel des Schlüssels
einmal gefunden hat, bleibt maskiert.

Ein Log-Bereich kann Millionen von Werten enthalten, deshalb hält eine Instanz den Tresor eines
Schlüssels nicht im Speicher. Sie behält die Werte, die der Schlüssel zuletzt verwendet hat
(`key_working_values`, standardmäßig 5.000, etwa 10 MiB pro Schlüssel), und findet jeden anderen in
PostgreSQL, sobald die Texte eines Aufrufs ihn brauchen, mit einer einzigen Abfrage für alle, über
einen Index der Wörter der Werte und Ersatzwerte. Dieser Index enthält Hashes, die mit einem geheimen
Schlüssel gebildet sind, nie ein Wort im Klartext.

**Geheimnisse, an die andere Buchstaben grenzen.** In PostgreSQL wird ein Geheimnis, das als eine
einzige Folge von Buchstaben und Ziffern ohne Leerzeichen oder Satzzeichen geschrieben ist, über sein
Wort gefunden, und über seine ersten und letzten sechs Zeichen, wenn auf einer Seite andere Buchstaben
oder Ziffern angrenzen. Grenzen sie auf beiden Seiten an (`xhunter2y`), oder grenzt überhaupt etwas
an, wenn das Wort an seinem Rand sechs Zeichen oder weniger hat, findet es kein Wort des Textes. Darum
hält eine Instanz die kurzen Geheimnisse eines Schlüssels dauerhaft im Speicher, die aus einer
einzigen Folge von 16 Zeichen oder weniger oder mit einem so kurzen Wort am Rand: die neuesten
`key_held_secrets` davon (1.000, sofern die Einstellungen des Servers nichts anderes sagen), gefunden,
wie auch immer etwas an sie grenzt. Ein älteres wird gefunden, wo nichts an es grenzt, oder wo eine
Seite frei ist und es sieben Zeichen oder mehr hat; ein längeres überall dort, wo eine Seite frei ist.
Erhöhen Sie `key_held_secrets` für einen Schlüssel, der viele kurze Geheimnisse sieht, etwa ein
Sitzungstoken in jeder Logzeile: Jedes gehaltene belegt etwa 2,2 KB auf jeder Instanz, und jeder Text,
den der Schlüssel maskiert oder wiederherstellt, wird danach durchsucht, was seine Aufrufe etwas
verlangsamt. Gemessen wie unten mit einem eigenen Sitzungstoken in jeder Zeile (ein Wert mehr, der in
jeder aufzubewahren ist), maskierte ein neuer Schlüssel mit 1.000 gehaltenen solchen Geheimnissen 73
Zeilen pro Sekunde und stellte 1.935 wieder her, gegenüber 75 und 2.103 ohne gehaltene. Auch ein
Ersatzwert, der so angrenzt, dass keines seiner Wörter für sich steht, wird nicht gefunden; das
Maskieren erzeugt so einen nie. Wie überall in Latet wird ein Wert ab vier Zeichen wiedererkannt, der
Name einer Person ab drei ([bekannte Werte](../concepts/detection/#bekannte-werte)).

**Was ein gescheiterter Aufruf behält.** Ein `mask`-Aufruf, der beim Maskieren scheitert (ein 507, ein
Commit, der PostgreSQL nicht erreicht), behält keinen seiner neuen Werte, wenn er dafür eine der
Transaktionen der Instanz hielt, von denen es mit dem standardmäßigen `database_pool` drei
gleichzeitig gibt: Der Bereich des Schlüssels vergisst sie. Waren diese belegt, hat der Aufruf jeden
neuen Wert sofort gespeichert, und diese bleiben im Tresor des Schlüssels. Ein Fehler nach dem Maskieren, in der lokalen Prüfung oder beim
Erstellen der Antwort, kommt nach dem Commit; auch dann bleiben die neuen Werte des Aufrufs. Die Texte
erneut zu maskieren ist in jedem Fall sicher: Ein Wert, den der Tresor behalten hat, hat seinen
Ersatzwert, und einer, den er vergessen hat, erhält wieder einen. Ein Agent desselben Schlüssels, der
einen vergessenen Wert inzwischen über das Gateway verwendet hat, hat dessen Ersatzwert bereits
gesendet, den keine spätere Wiederherstellung zurückverwandelt.

## Grenzen

- **Der Tresor.** Der Tresor eines Schlüssels fasst die `vault_limit` Werte des Servers (200.000, wenn
  die Einstellungen des Servers nichts anderes sagen), oder eine eigene Grenze:
  `latet server keys limit "Shop" 5000000` (von 1.000 bis 100.000.000; `default` gibt ihm wieder die
  des Servers). Ein voller Tresor weist den `mask`-Aufruf, der einen Wert hinzufügen würde, mit 507
  ab: Vom Aufruf kommt nichts zurück, und nichts verlässt den Server im Klartext. Der Status des
  Servers listet die Identitäten bei 90 % ihrer Grenze auf, und die Metrik `latet_vault_near_limit`
  zählt sie; heben Sie eine Grenze also an, bevor sie erreicht ist. Jede Instanz zählt die neuen Werte
  eines Aufrufs, bevor sie sie bestätigt: Zwei Instanzen, oder ein Aufruf und ein Agent desselben
  Schlüssels, können die Grenze um die neuen Werte je eines Aufrufs überschreiten, bevor der nächste
  Wert abgewiesen wird.
- **Ein Aufruf** nimmt höchstens 1.000 Texte und das `apps_request_mb` des Servers (8 MiB, wenn seine
  Einstellungen nichts anderes sagen, nie mehr als sein `max_request_mb`): Ein Aufruf wartet im
  Speicher auf den Thread des Schlüssels, und `latet-client` teilt eine längere Liste in Aufrufe von
  höchstens 8 MiB auf. Texte, deren Wörter mehr als 100.000 Werte oder Ersatzwerte des
  Tresors nennen, werden mit 422 abgewiesen, ohne dass etwas maskiert wird: Senden Sie weniger Texte
  pro Aufruf.
- **Gleichzeitige Aufrufe.** Ein Schlüssel hat höchstens `requests_in_flight` Anwendungsanfragen auf
  einer Instanz (16, wenn die Einstellungen nichts anderes sagen); darüber hinaus 429 mit
  `Retry-After`.

## Wie schnell es ist

Die Aufrufe eines Schlüssels laufen auf einer Instanz nacheinander, im Thread seines Bereichs; Aufrufe
verschiedener Schlüssel und Aufrufe, die verschiedene Instanzen erreichen, laufen nebeneinander. Um
mit einem Schlüssel schneller zu werden, betreiben Sie mehr Instanzen hinter dem Lastverteiler.

Gemessen auf einer Instanz (Intel i9-10850K, 64 GB, Windows 10, PostgreSQL in Docker Desktop auf
demselben Rechner), mit Logzeilen von etwa 200 Zeichen mit je einer E-Mail-Adresse und einer
öffentlichen IPv4-Adresse, von denen die Hälfte einen bereits gesehenen Kunden nennt, 500 Zeilen pro
Aufruf als `data`; und mit Dokumenten von etwa 20 KB Prosa mit je zehn Werten, eines pro Aufruf:

| Der Tresor des Schlüssels | Maskieren, Zeilen pro Sekunde | Wiederherstellen, Zeilen pro Sekunde | Ein Dokument, maskieren / wiederherstellen |
| --- | ---: | ---: | ---: |
| Neu, lernt seine Werte nach und nach | 189 | 2.691 | 77 / 28 ms |
| 200.000 Werte | 440 | 1.691 | 87 / 30 ms |
| 1.000.000 Werte | 410 | 1.498 | 95 / 32 ms |

Das Maskieren wird von den Detektoren begrenzt: Auf einem Thread, mit allen Werten im Speicher und
ohne Datenbank, maskiert die Engine etwa 900 solcher Zeilen pro Sekunde. Ein neuer Schlüssel schreibt
außerdem jeden neuen Wert in PostgreSQL. Eine Instanz hält höchstens `database_pool` Verbindungen zu
PostgreSQL (10, sofern die Einstellungen des Servers nichts anderes sagen) für die Arbeit aller
Identitäten und lässt drei Zehntel davon (drei von zehn) von Aufrufen halten, die ihre neuen Werte in
je einer Transaktion speichern; die übrigen speichern jeden Wert einzeln, was langsamer ist. Erhöhen
Sie `database_pool` für eine Instanz, die für viele neue Schlüssel gleichzeitig maskiert, und mit ihm
`max_connections` von PostgreSQL
([Serverbetrieb](../concepts/server-operations/#verbindungen-grenzen-und-vertrauen)).
`scripts/bench_apps.py` im Repository misst einen Server von Ihnen auf dieselbe Weise.

## Einen Server aktualisieren

Nach einer Aktualisierung, die ändert, wie die Wörter eines Tresors indiziert werden, erstellt die
erste Instanz, die den Bereich eines Schlüssels aufbaut, sie neu, bevor sie den ersten Aufruf des
Schlüssels beantwortet: etwa 40 Sekunden für 200.000 Werte und viereinhalb Minuten für 1.000.000 auf
dem oben genannten Rechner. Ein Aufruf, der in der Zwischenzeit eintrifft, wartet, und eine Instanz
baut vier Bereiche gleichzeitig auf; einige große Schlüssel verzögern also die ersten Aufrufe der
anderen Schlüssel auf ihr. Das Changelog sagt, wann eine Version das tut; senden Sie dann jedem großen
Schlüssel einen Aufruf mit ausreichend langem Timeout, bevor seine Programme wieder starten.

Solange Instanzen zweier Versionen nebeneinander laufen, wird ein Wert, den eine ältere Instanz
schreibt, von den neueren gefunden, solange sie ihn im Speicher halten, aber nicht in PostgreSQL, bis
der Bereich des Schlüssels neu aufgebaut wird. Wenn die API für Anwendungen genutzt wird, stoppen Sie
jede ältere Instanz, bevor die neuen starten, auch wenn die Tabellen gleich bleiben
([Aktualisieren](../concepts/server-operations/#aktualisieren-und-instanzen-hinzufügen)).
