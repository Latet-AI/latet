---
title: Referenz der API für Anwendungen
description: Der HTTP-Vertrag der API für Anwendungen eines Latet-Servers, ihre Antworten, ihre Fehler und ihre Grenzen.
sidebar:
  label: API für Anwendungen
---

Die [API für Anwendungen](../../applications/) eines Latet-Servers beantwortet vier Operationen unter
`https://<Ihr Server>/_latet/apps/v1/`. Dies ist Version 1: Eine Änderung, die ein dafür geschriebenes
Programm brechen würde, kommt als `v2`, daneben. Die [OpenAPI-Beschreibung](../../../apps-api.openapi.json)
sagt dasselbe für Werkzeuge, die einen Client erzeugen; Python-Programme können `latet-client`
verwenden ([Logs](../../applications/logs/), [RAG](../../applications/rag/)).

## Authentifizierung

Jeder Aufruf trägt den Token eines API-Schlüssels in einem einzigen Header:

```http
Authorization: Bearer latet_key_...
```

Ein Token ist `latet_key_` gefolgt von 43 Zeichen, und der Administrator erzeugt ihn
(`latet server keys add`, `keys add-token`). Jede Operation braucht ihre eigene Berechtigung: `scan`,
`mask` (das `scan` einschließt), `unmask` oder `forget` (das keine andere einschließt und das der erste
Token eines Schlüssels nicht hat). Der Server weist ab:

- mit **401** und `WWW-Authenticate: Bearer` einen Aufruf ohne `Authorization`-Header, mit zweien, mit
  einem anderen Schema als `Bearer` oder mit einem Token, den er nicht kennt (widerrufen, rotiert, von
  einem deaktivierten oder entfernten Schlüssel);
- mit **403** den Rechner-Token einer Person, einen Token ohne die Berechtigung der Operation und jede
  Anfrage, die einen Header `Origin` oder `Sec-Fetch-Site` trägt, wie die eines Browsers: Die API ist
  für Programme, nie für eine Webseite.

Token, die der Server abweist, zählen gegen die Adresse, von der sie kommen, wie auf jedem Pfad des
Servers: über der Grenze 429. Rufen Sie den Server über HTTPS auf; sonst reisen der Token und die Texte
im Klartext.

## Anfragen

| Operation | Pfad | Berechtigung | Körper |
| --- | --- | --- | --- |
| Analysieren | `POST /_latet/apps/v1/scan` | `scan` | `{"texts": [...], "kind": "prose"}` |
| Maskieren | `POST /_latet/apps/v1/mask` | `mask` | `{"texts": [...], "kind": "prose"}` |
| Wiederherstellen | `POST /_latet/apps/v1/unmask` | `unmask` | `{"texts": [...]}` |
| Vergessen | `POST /_latet/apps/v1/forget` | `forget` | `{"values": [...]}` |

- `texts` ist eine Liste von 1 bis 1.000 Zeichenketten, die in derselben Reihenfolge beantwortet
  werden. Ein leerer Text kommt unverändert zurück, ohne Funde und mit `restored: 0`.
- `values` ist eine Liste von 1 bis 1.000 Zeichenketten, die Werte, die den Tresor des Schlüssels
  verlassen sollen, jeder genau so gesucht, wie er geschrieben ist
  ([Werte vergessen](../../applications/forget/)).
- `kind` ist `prose` (Text, den Menschen geschrieben haben, die Voreinstellung) oder `data` (Logs,
  Datensätze, die Ausgabe eines Programms); siehe [Prosa und Daten](../../applications/#prosa-und-daten).
  `null` oder ein anderes Wort wird abgewiesen. Wiederherstellen nimmt keine Art.
- Der Körper ist ein JSON-Objekt von höchstens `apps_request_mb` des Servers (8 MiB, wenn seine
  Einstellungen nichts anderes sagen, nie mehr als sein `max_request_mb`).

Analysieren und Wiederherstellen mit curl, der Token über die Standardeingabe übergeben wie beim
[ersten Aufruf](../../applications/#ein-erster-aufruf), der maskiert; die Antworten stehen weiter unten:

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Bitte erstatten Sie die Bestellung von mario.rossi@acme.it auf IT60X0542811101000000123456."]}' \
  https://latet.acme.lan/_latet/apps/v1/scan
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Bitte erstatten Sie die Bestellung von tommaso.gallo@margie-6e5a.example auf IT17F3945471087320356678487."]}' \
  https://latet.acme.lan/_latet/apps/v1/unmask
```

## Antworten

Jede Antwort von Analysieren, Maskieren und Wiederherstellen ist ein JSON-Objekt, dessen `results` für
jeden gesendeten Text einen Eintrag enthalten, in Reihenfolge; ein Vergessen antwortet mit zwei Zahlen.

**Analysieren** sagt, wo die Werte im gesendeten Text stehen, und behält nichts:

```json
{"results": [{"findings": [{"type": "EMAIL", "class": "pii", "start": 39, "end": 58},
                           {"type": "IBAN", "class": "pii", "start": 63, "end": 90}],
              "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

**Maskieren** gibt jeden Text mit den Ersatzwerten des Schlüssels zurück, und wo sie im
zurückgegebenen Text stehen:

```json
{"results": [{"text": "Bitte erstatten Sie die Bestellung von tommaso.gallo@margie-6e5a.example auf IT17F3945471087320356678487.",
              "replacements": [{"type": "EMAIL", "class": "pii", "start": 39, "end": 72},
                               {"type": "IBAN", "class": "pii", "start": 77, "end": 104}],
              "counts": {"EMAIL": 1, "IBAN": 1}},
             {"blocked": "the local reviewer could not read the user text (it took longer than the 30 seconds an application's call waits for it, which llm_review.budget_ms changes), so it was not sent"}],
 "degraded": ["llm-review"]}
```

- Ein Ergebnis mit `blocked` und ohne `text` ist ein Text, den Latet nicht beantwortet: Der lokale
  Prüfer hat ihn gestoppt, seine Prüfung ist gescheitert und `llm_review.on_failure` des Schlüssels ist
  `stop`, oder der Dump-Schutz hat ihn zurückgehalten. Der Grund, auf Englisch, nennt keinen Wert.
- `degraded`, nur vorhanden, wenn im Aufruf etwas eingeschränkt lief, nennt, was es betraf: einen
  Detektor, der einen Text nicht lesen konnte (etwa `ner`), oder `llm-review`.

**Wiederherstellen** gibt jeden Text zurück, in dem jeder Ersatzwert aus dem Tresor des Schlüssels
durch seinen Wert ersetzt ist, und wie viele verschiedene Werte es wiederhergestellt hat:

```json
{"results": [{"text": "Bitte erstatten Sie die Bestellung von mario.rossi@acme.it auf IT60X0542811101000000123456.",
              "restored": 2}]}
```

**Vergessen** sagt, wie viele der Werte der Tresor des Schlüssels enthielt, die jetzt mit ihren
Ersatzwerten verschwunden sind, und wie viele er nicht enthielt; jeder Wert zählt einmal:

```json
{"forgotten": 1, "unknown": 1}
```

### Positionen

`start` und `end` zählen Unicode-Codepunkte (`end` nicht eingeschlossen): In Python ist
`text[start:end]` der Wert. JavaScript, Java und C# zählen UTF-16-Einheiten, in denen ein Zeichen
jenseits der Basic Multilingual Plane, wie die meisten Emoji, zwei belegt. In
`Schreiben Sie an 👩‍💻 mario.rossi@acme.it` beginnt die Adresse bei 21 in Codepunkten und bei 23 in
UTF-16-Einheiten; in JavaScript liefert sie `Array.from(text).slice(start, end).join("")`.

### Typen und Klassen

`type` ist der Typ des Wertes: `EMAIL`, `PHONE`, `IBAN`, `CREDIT_CARD`, `IP_ADDRESS` und die anderen
der [Detektoren](../detectors/), die Regel eines Geheimnisses oder der Typ eines Begriffs, Erkenners
oder einer Feldregel des Schlüssels. `class` ist die Familie dessen, was ihn gefunden hat: `secret`,
`custom` (die Begriffe und Erkenner des Schlüssels), `identity`, `pii`, `ner` (das Namensmodell),
`llm` (der lokale Prüfer) oder `struct` (der strikte Datenmodus).

## Fehler

Ein Fehler ist `{"error": "<Worte>"}`. Die Worte, auf Englisch, sind für Menschen: Sie zitieren nie
einen gesendeten Text, und die Abweisungen am Eingang des Servers (Authentifizierung, Grenzen)
beginnen mit `latet: `. Entscheiden Sie nach dem Status.

| Status | Wann | Was tun |
| --- | --- | --- |
| 400 | Der Körper ist kein JSON-Objekt mit `texts` als Liste von 1 bis 1.000 Zeichenketten und, für Analysieren und Maskieren, einer Art (`kind`) `prose` oder `data`; beim Vergessen mit `values` als Liste von 1 bis 1.000 Zeichenketten. | Den Aufruf korrigieren. |
| 401 | Kein Token, zwei `Authorization`-Header, ein anderes Schema oder ein Token, den der Server nicht kennt. | Den Token prüfen; einen neuen anfordern. |
| 403 | Dem Token fehlt die Berechtigung (`this token lacks the mask permission`), es ist der Token einer Person, oder eine Webseite hat die Anfrage gestellt. | Einen Token mit der Berechtigung verwenden. |
| 404 | Ein anderer Pfad unter `/_latet/apps/v1/`. | |
| 405 | Eine andere Methode als `POST` (`Allow: POST`). | |
| 413 | Der Körper ist größer als `apps_request_mb`. | Weniger Texte pro Aufruf senden. |
| 422 | Analysieren, Maskieren und Wiederherstellen: Die Texte nennen mehr als 100.000 Werte oder Ersatzwerte des Tresors, mehr als ein Aufruf abwägen darf. Nichts wurde maskiert oder wiederhergestellt. | Weniger Texte pro Aufruf senden. |
| 429 | Zu viele abgewiesene Token von dieser Adresse, oder der Schlüssel hat `requests_in_flight` Anwendungsanfragen auf dieser Instanz (16, wenn die Einstellungen nichts anderes sagen). | `Retry-After` abwarten, dann erneut versuchen. |
| 500 | Ein anderer Fehler; seine Worte nennen keinen Wert. | Erneut versuchen; dem Administrator melden, wenn es anhält. |
| 503 | Ein Speicher des Servers hat nicht geantwortet, der Bereich des Schlüssels ist auf dieser Instanz gerade nicht verfügbar, oder das Administrationsprotokoll hat den Eintrag nicht aufgenommen: Beim Wiederherstellen wurde nichts wiederhergestellt, beim Vergessen nichts vergessen. | Warten (`Retry-After`, wenn angegeben), dann erneut versuchen. |
| 507 | Nur beim Maskieren: Der Tresor des Schlüssels enthält die meisten Werte, die er darf. Vom Aufruf kam nichts zurück, und nichts ging im Klartext hinaus. | Ein Administrator hebt die Grenze mit `latet server keys limit` an. |

Ein Maskieren oder ein Wiederherstellen lässt sich gefahrlos wiederholen: Ein Wert, den der Tresor
behalten hat, erhält wieder denselben Ersatzwert, und ein Wert, den ein gescheiterter Aufruf nicht
behalten hat, erhält wieder einen. Auch ein Vergessen lässt sich wiederholen: Ein Wert, der schon fort
ist, zählt unter `unknown`. Ein gescheiterter `mask`-Aufruf kann einige seiner neuen Werte
behalten haben ([der Tresor eines Schlüssels](../../applications/#der-tresor-eines-schlüssels) sagt,
wann).

## Header

- Jede Antwort sagt `Cache-Control: no-store`: Kein Cache behält einen Text oder einen Wert.
- `Retry-After`, in Sekunden, kommt mit jedem 429 und mit manchen 503.

## Grenzen

| Grenze | Wert | Festgelegt durch |
| --- | --- | --- |
| Texte pro Aufruf, oder Werte pro Vergessen | 1.000 | Latet |
| Körper eines Aufrufs | 8 MiB | `apps_request_mb` des Servers |
| Gleichzeitige Anwendungsanfragen eines Schlüssels auf einer Instanz | 16 | `requests_in_flight` des Servers |
| Werte oder Ersatzwerte des Tresors, die die Texte eines Aufrufs nennen dürfen | 100.000 | Latet |
| Werte, die der Tresor eines Schlüssels fasst | 200.000 | `vault_limit` des Servers, oder die eigene des Schlüssels: `latet server keys limit` |
| Werte aus dem Tresor eines Schlüssels, die eine Instanz im Speicher hält | 5.000 | `key_working_values` des Servers |

## Was der Server festhält

- **Das Administrationsprotokoll** erhält für jeden Wiederherstellungsaufruf einen Eintrag
  `apps.unmasked`, bevor die Antwort gesendet wird: die ID des Tokens, wie viele Texte und wie viele
  Werte wiederhergestellt wurden (`{"token": "t_...", "texts": 3, "restored": 5}`), und die Adresse
  des Clients. Nie ein Text oder ein Wert. Analysieren und Maskieren stehen nicht darin. Lesen Sie die
  Einträge mit `latet server log --action apps.unmasked`. Jeder Aufruf zum Vergessen ist ein Eintrag
  `apps.forgotten`, geschrieben in derselben Transaktion wie das Vergessen: die ID des Tokens, wie viele
  Werte angefragt, vergessen und nicht gefunden wurden (`{"token": "t_...", "values": 2,
  "forgotten": 1, "unknown": 1}`), und die Adresse des Clients.
- **Die Metriken** zählen die Anfragen der API als `latet_http_requests_total{kind="apps"}`, nach
  Status, und ihre Texte und Werte nach Operation: `latet_apps_texts_total{operation}` und
  `latet_apps_values_total{operation}` (die Werte, die das Analysieren gefunden, das Maskieren ersetzt und
  das Wiederherstellen zurückgesetzt hat; beim Vergessen die angefragten und die vergessenen Werte).
  Sie haben kein Label für den Schlüssel: Wer was getan hat,
  steht im Administrationsprotokoll.
- **Das Log des Servers** hat eine Zeile pro Aufruf: die Methode, den Pfad, den Status und die Dauer,
  nie einen Header, den Körper oder einen Wert. Abweisungen (401, 403, 404, 405, 413, 429 und die des
  Servers selbst) werden einmal pro Minute mit ihrer Anzahl zusammengefasst, statt je eine Zeile zu
  erhalten.
