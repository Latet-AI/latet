---
title: Produktionsdaten
description: Abfrageergebnisse, API-Antworten und Logs maskieren, die ein Agent liest, jeden Wert oder Feld für Feld.
---

Ein Agent, der einem Bug nachgeht, liest oft echte Daten: eine Abfrage auf der Produktionsdatenbank,
die Antwort einer REST-API, ein Log, eine Seite im Browser. Die Detektoren von Latet finden darin,
was ein erkennbares Format hat, etwa E-Mail-Adressen, IBANs, Tokens und Identifikationsnummern. Eine
Notiz, ein Gehalt oder eine Vertragsklausel haben kein Format: In einer Tabelle würden sie
unverändert hinausgehen.

Latet liest diese Ausgaben anhand ihrer Struktur. Die Ausgabe eines Tools ist oft eine Tabelle, ein
JSON-Dokument oder Zeilen mit `key=value`-Paaren, und der Name jeder Spalte oder jedes Schlüssels
sagt, welche Werte darin stehen. Drei Dinge nutzen das:

| | Wann | Was es maskiert |
| --- | --- | --- |
| **Geheime Spalten** | immer | die Werte von Spalten und Schlüsseln, die wie ein geheimer Wert heißen: `password_hash`, `api_key`, `reset_token` |
| **Feldregeln** | wenn Sie sie schreiben | die Werte der Felder, die Sie nennen, als eigener Typ |
| **Strikter Datenmodus** | mit dem Profil `max` oder wenn Sie ihn einschalten | jeden anderen Wert der Struktur |

## Die Strukturen, die Latet liest

- **Tabellen:** psql, CSV, TSV und mit `;` getrennte, Markdown, HTML sowie die Tabellen im
  Accessibility-Snapshot eines Browsers (`columnheader`, `cell`) und mit Rahmenzeichen gezeichnete
  Tabellen (`console.table` von Node, der Unicode-Stil von psql).
- **`key=value`-Paare:** zwei oder mehr in einer Zeile, wie in einem Log. Ein Wert ohne Anführungszeichen endet
  am ersten Leerzeichen, außer eine Telefonnummer steht am Anfang: deren Gruppen gehören dann dazu
  (`phone=+39 347 123 4567`).
- **JSON:** ein ganzes Dokument Wert für Wert, jeder mit seinem Pfad (`items[].sku`); ein Fragment
  `"key": value` in anderem Text, etwa in einer Log-Zeile, anhand seines Schlüssels.

## Geheime Spalten

Die geheimen Spalten sind immer aktiv. Eine CSV-Datei mit Benutzern:

```text
id,email,api_key,password_hash,reset_token,role
7,ops@acme-intra.com,ak_live_9fK2LmQx7Rt3vW8zB1nY5pD,$2b$12$Kq3v0rZQ8xGm1o5t,8f14e45fceea167a,admin
```

geht so hinaus:

```text
id,email,api_key,password_hash,reset_token,role
7,oliver@consolidated-e55f.example,ak_live_3zF1ObRs6Wa1eH2mU6tO2fM,p5cp22yIa9h4bDH9pBx9p8n,a03c15f272977c30,admin
```

Der Schlüssel, der Hash und das Token sind geheime Werte wegen ihrer Spalte, die E-Mail-Adresse
wegen ihres Formats; `admin` bleibt unberührt. Eine geheime Spalte bleibt maskiert, auch wenn eine
Feldregel ihr Feld im Klartext lässt.

## Strikter Datenmodus

Das [Profil `max`](../profiles/) schaltet ihn ein. Mit den anderen Profilen, oder um es ausdrücklich
festzulegen:

```yaml
data: strict
```

`data: normal` schaltet ihn auch mit `max` aus; lassen Sie `data` weg, entscheidet das Profil. Ihn
dort auszuschalten, wo er eingeschaltet ist, verringert den Schutz: Kommt das von einem Agenten
oder über die Kommandozeile, wartet die Änderung auf Ihre Freigabe.

Jeder Wert einer Tabelle, eines JSON-Dokuments oder von `key=value`-Paaren in der Ausgabe eines
Tools wird maskiert; die Namen von Spalten und Schlüsseln bleiben, außer einem Schlüssel, der selbst
ein Wert ist, etwa eine E-Mail-Adresse: Er wird maskiert wie Werte überall. Ein Abfrageergebnis:

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | SHIPPED | 1250.5 | call back on +44 20 7946 0958
```

geht so hinaus:

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | QXWRLTE | 2939.3 | wkqp xoaj zd +44 20 3318 4452
```

- **Derselbe Wert erhält immer denselben Ersatzwert.** Zählungen, Gruppierungen und Joins
  funktionieren weiterhin mit dem, was das Modell liest.
- **Eine Abfrage, die mit einem Ersatzwert geschrieben wurde, läuft mit dem echten Wert.** Das
  Modell schreibt `WHERE status = 'QXWRLTE'`; das Tool führt `WHERE status = 'SHIPPED'` aus.
- **Ein Ersatzwert behält das Format des Werts.** Eine Zahl bleibt eine Zahl mit ebenso vielen
  Ziffern, ein ISO-Datum ein gültiges Datum (höchstens zwei Jahre vom echten entfernt), eine Uhrzeit
  eine gültige Uhrzeit. Wenn es für ein Format keinen freien Ersatzwert mehr gibt, etwa bei
  Hunderten dreistelliger IDs in einer Ausgabe, ist der Ersatzwert ein Zeichen länger.
- **Ein Link bleibt ein Link.** Eine Webadresse behält ihr Schema (`https://`, `s3://`...), der Rest
  wird maskiert.
- **Was einen Wert nur benennt oder sagt, wie eine Seite aussieht, bleibt im Klartext:** ein
  Platzhalter einer Vorlage oder einer Shell (`${from}`, `{{ subject }}`, `$MAIL_FROM`) sowie
  `class` und `style` von Markup, das ein Befehl ausgibt, außer der Stil lädt eine Adresse. Eine
  Tabellenzeile, die nicht zu ihren Spalten passt, wird ganz maskiert.
- **Ein Fund innerhalb eines Werts behält seinen eigenen Typ.** Die Telefonnummer in der Notiz
  erhält eine erfundene Telefonnummer; der Rest der Notiz wird als Notiz maskiert.
- **Ein Wert bleibt in seiner Struktur.** `SHIPPED` wird in der Tabelle maskiert, nie in Ihrem
  Code: Der strikte Datenmodus fügt seine Werte nicht zu denen hinzu, die überall maskiert werden.
- **Werte aus einem oder zwei Zeichen bleiben im Klartext,** etwa `17` oben: Solche Werte sagen
  wenig aus, und ein so kurzer Ersatzwert ließe sich in einer Antwort nicht unterscheiden. Leere
  Werte und Wörter, die für nichts oder für Ja und Nein stehen, etwa `null`, `n/a`, `true` und
  `false`, bleiben ebenfalls im Klartext.
- **Ein Wert von mehr als 16.384 Zeichen wird geschwärzt,** nicht durch einen Ersatzwert ersetzt:
  Das Modell erhält `[REDACTED:DATA]`, und nichts wird wieder eingesetzt.
- **Dateien bleiben unberührt.** Die Ausgabe der Tools, die Dateien lesen (`Read`, `Grep`, `Glob`,
  `LS`... von Claude Code), ist Code und Dokumente, keine Daten, es sei denn, eine Regel nennt die
  Datei ([Dateien, die als Daten gelten](#dateien-die-als-daten-gelten)). Die anderen Detektoren
  gelten trotzdem dafür.
- **Ebenso Web-Ergebnisse.** Suchergebnisse und Seiten (`WebSearch`, `WebFetch`) sind öffentliche
  Inhalte, nicht Ihre Daten, und sie kommen als JSON: Der strikte Datenmodus würde jeden Titel und
  jede Adresse in zufällige Buchstaben verwandeln und dem Modell nichts zum Lesen oder Zitieren
  lassen. Die anderen Detektoren gelten trotzdem dafür, sodass ein Schlüssel oder eine
  E-Mail-Adresse in einer Seite maskiert wird wie überall sonst. Um eine Webseite dennoch
  abzudecken, nennen Sie ihre Adresse in `data_hosts`
  ([Webadressen, die als Daten gelten](#webadressen-die-als-daten-gelten)). Um ein Web-Tool
  abzudecken, nehmen Sie es zusammen mit jedem anderen Tool, das der strikte Datenmodus abdecken
  soll, in `data_tools` auf: Sobald `data_tools` gesetzt ist, deckt der strikte Datenmodus nur noch
  die Tools ab, die es nennt, und `"*"` nennt jedes Tool außer denen, die Dateien lesen.

### Welche Tools

Der strikte Datenmodus deckt die Ausgabe jedes Tools ab, außer den Tools, die Dateien lesen, und den
Web-Tools. Um nur einige abzudecken, nennen Sie sie, mit `*` für beliebige Zeichen:

```yaml
data: strict
data_tools: [mcp__postgres__*, Bash]
```

Ein Tool eines MCP-Servers trägt den Namen des Servers: `mcp__crm__*` nennt jedes Tool des Servers
`crm`.

Ein Befehl, den Sie in Claude Code mit `!` ausführen, zählt als Ausgabe des Tools `Bash`.

Codex liest Dateien mit Shell-Befehlen, daher deckt der strikte Datenmodus in Codex auch die
Dateien ab, die es liest. Nennen Sie sein Datenbank-Tool in `data_tools`, um nur dieses abzudecken.

### Dateien, die als Daten gelten

```yaml
data: strict
data_files: ["exports/**", "data/*.csv", ".env*"]
```

Eine Datei, die ein Muster nennt, gilt als Daten, ob der strikte Datenmodus an ist oder nicht: Sie zu nennen genügt. Eine Datei, die der Agent liest und deren Pfad zu einem Muster passt, gilt als Daten: Jeder Wert
darin wird maskiert, wie in der Ausgabe eines Tools. Ein Muster ohne Schrägstrich nennt eine Datei
bei ihrem Namen (`*.json`, `.env*`), eines mit Schrägstrich das Ende ihres Pfads (`exports/**`,
`**/*.csv`); `*` steht für beliebige Zeichen, und Groß- und Kleinschreibung spielen keine Rolle. Die
Werte werden nach dem Format der Datei gefunden:

- **JSON** (`.json`, `.jsonl` oder eine Datei mit anderem Namen, die JSON enthält): jede
  Zeichenkette und jede Zahl, nach dem Pfad ihres Schlüssels, wie in der JSON-Ausgabe eines Tools.
  Die Schlüssel bleiben.
- **Tabellen** (`.csv`, `.tsv`): jede Zelle, nach ihrer Spalte.
- **`.env`-, Properties- und ini-Dateien:** der Wert jeder Zeile `KEY=value`.
- **YAML:** jeder Wert, nach seinem Pfad (`servers[].host`); die Schlüssel und die Struktur bleiben.
- **Jede andere Datei:** die Tabellen, `key=value`-Paare und JSON-Fragmente darin.

Jeder Wert wird dort ersetzt, wo er steht: Die Datei behält ihre Zeilen, deren Nummern und ihre
Schlüssel, sodass der Agent sie weiterhin anhand des gelesenen Texts bearbeiten kann, und eine mit
einem Ersatzwert geschriebene Änderung schreibt den echten Wert. Nur die Tools, die den Text einer
Datei zurückgeben, machen sie zu Daten (`Read` und `NotebookRead` von Claude Code): Die Zeilen einer
Suche (`Grep`) und eine Auflistung (`Glob`, `LS`) bleiben, wie sie sind. Ändert `Edit`, `MultiEdit`
oder `Write` von Claude Code eine Datei, die ein Muster nennt, werden die Zeilen der Datei, die das
Tool zeigt und die der Agent vielleicht nie gelesen hat, wie die Datei maskiert, auch wenn
`data_tools` andere Tools nennt.

- **Ein breites Muster deckt mehr als Daten ab.** `*.json` deckt auch `package.json` und
  `tsconfig.json` ab, deren Werte dann überall im Gespräch maskiert würden: Nennen Sie die Ordner,
  die Daten enthalten (`exports/**`).
- **Markdown gilt nur, wenn ein Muster es nennt** (`*.md`, `docs/**/*.md`): Agenten lesen ihre
  Anweisungen und Dokumente in Markdown, und ein Muster für jede Datei (`*`) lässt sie unberührt. In
  einer Markdown-Datei, die ein Muster nennt, werden nur die Tabellen maskiert, nie der Fließtext.
- **PDF-Dateien sind nicht abgedeckt:** Latet liest den Text einer PDF nicht. `images: mask` oder
  `block` verhindert, dass Dokumente überhaupt gesendet werden ([Bilder](../images/)).

### Webadressen, die als Daten gelten

```yaml
data: strict
data_hosts: [api.internal.example.com, "*.salesforce.com"]
```

Eine Adresse, die ein Muster nennt, gilt als Daten, ob der strikte Datenmodus an ist oder nicht. Eine Seite, die ein Web-Tool (`WebFetch`) von einem Host abruft, der zu einem Muster passt, gilt als
Daten, wie die Ausgabe eines Tools: Jeder Wert ihrer Tabellen, ihres JSON und ihrer
`key=value`-Paare wird maskiert. `*` steht für beliebige Zeichen; Groß- und Kleinschreibung und der
Port spielen keine Rolle. Ein Muster für jede Subdomain nennt auch die Domain selbst:
`*.salesforce.com` nennt auch `salesforce.com`. Ein Host mit einem Punkt am Ende (`example.com.`) ist
derselbe Host wie ohne ihn. Eine Suche (`WebSearch`) hat keine eigene Adresse. Um Suchen abzudecken,
nehmen Sie `WebSearch` zusammen mit den anderen Tools, die Sie abdecken wollen, in `data_tools` auf,
oder schreiben Sie `data_tools: ["*"]` für jedes Tool außer denen, die Dateien lesen: Ein
`data_tools`, das nur `WebSearch` nennt, deckt nur Suchen ab und lässt Ihre Datenbank- und
Shell-Tools aus. Wenn der strikte Datenmodus an ist, schränkt es ihn ein, `data_tools` zu setzen,
wo es leer war, und wartet daher auf Ihre Freigabe.

Ein Muster oder eine Adresse hinzuzufügen gilt sofort; eines zu entfernen deckt weniger ab und
verringert damit den Schutz: Kommt das von einem Agenten oder über die Kommandozeile, wartet die
Änderung auf Ihre Freigabe. Im Dashboard werden sie unter **Regeln** → **Schutz** →
**Produktionsdaten** festgelegt, neben den Tools.

### In einem Austausch

Der strikte Datenmodus wird unter **Regeln** → **Schutz** → **Produktionsdaten** oder in Ihren
Einstellungen für ein Projekt festgelegt; ein Austausch hat keinen Schalter dafür. Solange er für
einen Austausch ausgeschaltet ist (im Projekt des Austauschs oder, bei einem Austausch ohne Projekt,
in Ihren globalen Regeln), fragt das Dashboard Latet, ob eine der zehn neuesten Tool-Ausgaben der
Anfrage eine Tabelle, JSON oder `key=value`-Paare enthält, gelesen wie beim
[Wählen der Spalten, die im Klartext bleiben](#die-spalten-wählen-die-im-klartext-bleiben), und von
einem Tool stammt, das der strikte Datenmodus abdeckt ([welche Tools](#welche-tools): keines, das
Dateien liest, und kein Web-Tool, es sei denn, eine Regel nennt die Datei, die Adresse oder das Tool). Ist das bei einer der Fall, sagt
eine Zeile über den Blöcken der Anfrage das, mit einem Link dorthin, wo der strikte Datenmodus für
diesen Austausch festgelegt wird: Ihre Einstellungen für sein Projekt, wenn sie `data` festlegen,
sonst unter **Regeln** → **Schutz** → **Produktionsdaten**. Latet liest die Ausgaben aus den Texten,
die der Verlauf aufbewahrt: Mit `history: metadata` oder `off` gibt es keine Zeile.

## Feldregeln

Eine Feldregel nennt eine Spalte, einen JSON-Schlüssel oder einen JSON-Pfad (`items[].sku`); `*`
steht für beliebige Zeichen, und Groß-/Kleinschreibung spielt keine Rolle. Jedes andere Zeichen wird
so genommen, wie es geschrieben ist, `?` und Klammern eingeschlossen, daher nennt
`orders[].items[].sku` genau diesen Pfad. Die Werte des Felds werden mit dem Typ maskiert, den Sie
angeben, mit oder ohne strikten Datenmodus:

```yaml
fields:
  - {name: note_*, type: NOTE}
  - {name: ral, type: SALARY}
  - {name: iban_fornitore, type: IBAN, secret: true}
```

- `secret: true` macht die Werte des Felds zu geheimen Werten: Ein solcher Wert, der eine Anfrage
  im Klartext erreicht, blockiert sie.
- `mask: false` lässt ein Feld im strikten Datenmodus im Klartext. Das gehört in Ihre eigenen
  Einstellungen, nicht in die `latet.yaml` eines Projekts, die nur Schutz hinzufügen kann.

Für ein Feld werden Ihre erste Regel, die darauf passt (zuerst Ihre Einstellungen für ein Projekt,
dann Ihre globalen Regeln), und die erste der `latet.yaml` des Projekts, die darauf passt,
verglichen: Es gilt die strengere (geschwärzt, dann maskiert, dann im Klartext), bei gleicher Ihre.

## Die Spalten wählen, die im Klartext bleiben

Der strikte Datenmodus maskiert jeden Wert, daher kann das Modell auch keinen Status und kein Datum
lesen: `SHIPPED` wird zu `QXWRLTE`, und ein Agent, der herausfinden soll, welche Bestellungen
verspätet versandt wurden, hat keine Anhaltspunkte. Die meisten Tabellen haben Spalten, die nichts
über eine Person aussagen, und das Dashboard schlägt sie vor.

Öffnen Sie in einem Austausch unter einer Tool-Ausgabe, die der strikte Datenmodus maskiert hat,
*Spalten auswählen, die im Klartext bleiben*. Jede Spalte der Ausgabe kommt mit einem Vorschlag und
seiner Begründung:

| Vorschlag | Für |
| --- | --- |
| **Im Klartext lassen** | ein Name, der auf IDs oder Codes hinweist, Datumsangaben von Ereignissen (`created_at`, `order_date`), Status (`status`, `state`), Ja oder Nein (`active`), Anzahlen (`qty`) |
| **Maskiert lassen** | personenbezogene Daten oder ein geheimer Wert in ihren Werten gefunden; ein Name, der Personen betrifft (`customer`, `created_by`, `doctor`), geheime Werte (`api_key`), Gesundheit, Überzeugungen oder Privatleben (`hiv_status`, `marital_status`); Freitext; Geburts- oder Sterbedaten |
| **Ihre Entscheidung** | Beträge; Datumsangaben, Ja oder Nein oder wenige Werte, die sich wiederholen, wenn der Name nicht sagt, was sie sind; die Spalten, zu denen es keinen Hinweis gibt |

Eine Spalte wird nur dann im Klartext vorgeschlagen, wenn ihr Name sagt, was sie enthält, und nichts
in ihren Werten personenbezogene Daten oder ein geheimer Wert ist: Dieselben Detektoren prüfen sie
wie alles andere. Das Format der Werte allein bringt eine Spalte nie in den Klartext, denn
Benutzernamen wiederholen sich genauso wie Status. Die Vorschläge sind ein Anfang: Sehen Sie sich
die Werte an, die neben jeder Spalte angezeigt werden, bevor Sie die Auswahl anwenden.

Die Spalten, die im Klartext vorgeschlagen werden, sind bereits angehakt.
*2 Spalten im Klartext lassen* fügt für jede eine Regel hinzu, etwa `{name: status, mask: false}`,
ganz oben in Ihren Einstellungen für das Projekt (*Dieses Projekt*) oder in Ihren globalen Regeln
(*Überall*): Von Ihren Regeln gewinnt die erste passende, daher maskiert eine allgemeinere Regel von
Ihnen wie `note_*` sie nicht wieder; eine Regel der `latet.yaml` des Projekts, die die Spalte
maskiert, gilt dort weiterhin, was auch immer Sie wählen. Die nächsten Ausgaben zeigen diese Spalten
unverändert. Nichts gilt, bevor Sie klicken.

- **Ermittelt, wenn Sie die Liste öffnen.** Die Spalten werden aus der Ausgabe gelesen, die der
  Verlauf aufbewahrt, auf Ihrem Rechner, und nichts Neues wird gespeichert. Mit `history: metadata`
  oder `off` gibt es keine Liste.
- **Die Einschätzung des lokalen Prüfers, wenn Sie fragen.** Ist der [lokale Prüfer](../llm-review/)
  eingeschaltet, sendet ihm *Lokalen Prüfer fragen* die Spaltennamen und einige Werte jeder Spalte
  und zeigt seine Einschätzung neben den Vorschlägen an. Sie wird nie von selbst übernommen.
- **Die eigenen Regeln eines Projekts bleiben.** Eine Spalte, die Ihre Einstellungen für ein Projekt
  maskieren, bleibt dort maskiert, was auch immer *Überall* hinzufügt: Die Liste weist darauf hin,
  und *Dieses Projekt* ändert sie. Eine Spalte, die die `latet.yaml` des Projekts maskiert, bleibt
  dort maskiert, was auch immer Sie wählen, und die Liste sagt, welche.
- **Nach einem Neustart** werden die Einstellungen eines Projekts bei seiner nächsten Sitzung
  geladen: Bis dahin kann die Auswahl nur in Ihre globalen Regeln gehen.
- **Codex und OpenCode** senden mit jeder Anfrage die ganze Unterhaltung, und Latet maskiert sie
  jedes Mal neu: Ab der nächsten Anfrage gehen auch die Ausgaben, die schon darin stehen, mit diesen
  Spalten im Klartext hinaus. In Claude Code bleibt eine Ausgabe so, wie das Plugin sie bei ihrem
  Eintreffen maskiert hat.

Für eine einzelne Spalte klicken Sie auf einen ihrer Werte: Das Fenster, das sich dann öffnet,
bietet *`status` in diesem Projekt nicht maskieren* an und sagt, wenn der Name einer Spalte nach
einer ID, einem Datum, einem Status oder einer Anzahl aussieht.

## Der Dump-Schutz

```yaml
dump_guard: 50
```

Eine Tool-Ausgabe, die mehr personenbezogene Werte enthält als diese Zahl (Namen, E-Mail-Adressen,
Telefonnummern, Identifikationsnummern...), wird zurückgehalten: Das Modell erhält stattdessen einen
kurzen Hinweis, der es auffordert, weniger Zeilen oder Zählungen abzufragen. Das Dashboard markiert
den Austausch mit *Zurückgehalten*. Nicht gesetzt oder 0: keine Grenze.

Im Profil `max` wird eine Tool-Ausgabe, wenn das [lokale Namensmodell](../../getting-started/installation/)
installiert ist, auch nach Namen und Adressen gelesen: Die Texte und Schlüssel einer JSON-Ausgabe
werden auf einmal gelesen, und eine, die das Modell nicht innerhalb von fünf Sekunden lesen kann, wird
ebenso zurückgehalten, mit einem Hinweis, der das Modell auffordert, weniger Zeilen oder Felder
abzufragen. Nichts, was es darin gefunden hätte, geht im Klartext hinaus.

## Der Browser

Ein Agent, der einen Browser steuert, liest Seiten als Tool-Ausgaben: Accessibility-Snapshots, HTML,
die Ergebnisse von Skripten, Netzwerkantworten. Der strikte Datenmodus gilt für sie wie für alles
andere, Tabellen eingeschlossen.

Seine Screenshots sind Bilder: Ist die [Texterkennung](../images/) installiert, wird der Text darin
gelesen und maskiert, und jeder Wert wird mit seinem Ersatzwert überdeckt. Um in einem Projekt nur
die Bilder zu senden, die Latet gelesen und maskiert hat, oder gar keine:

```yaml
images: mask    # or block: no image or PDF is sent
```

## In einem Projekt

Diese Schlüssel funktionieren in [config.yaml](../../reference/configuration/), in der
[`latet.yaml`](../projects/) eines Projekts und in Ihren eigenen Einstellungen für ein Projekt. Die
Projektdatei kann nur Schutz hinzufügen:

| Schlüssel | In `latet.yaml` | In Ihren Einstellungen für das Projekt |
| --- | --- | --- |
| `data` | nur `strict` | `strict` oder `normal` |
| `data_tools` | ignoriert | zu Ihren hinzugefügt |
| `data_files` | hinzugefügt | zu Ihren hinzugefügt |
| `data_hosts` | hinzugefügt | zu Ihren hinzugefügt |
| `fields` | nur Regeln, die maskieren, mit einem dort maskierten Typ | jede Regel, `mask: false` eingeschlossen |
| `dump_guard` | nur niedriger | jeder Wert; 0 schaltet ihn aus |
| `images` | nur weniger Bilder: `mask`, dann `block` | `allow`, `mask` oder `block` |
| `ocr` | nur `auto` | `auto` oder `off` |

Ein Projekt, dessen Profil `max` ist, in seiner Datei oder in Ihren Einstellungen dafür, hat den
strikten Datenmodus eingeschaltet, sofern `data` dort oder in Ihren globalen Regeln nichts anderes
festlegt.
