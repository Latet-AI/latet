---
title: Eigene Erkenner
description: Bringen Sie Latet ein eigenes Format bei, etwa eine Personalnummer oder einen Vertragscode.
---

Latet kennt geheime Werte, personenbezogene Daten und nationale Identifikationsnummern, aber nicht
die Formate Ihres Unternehmens: Ausweisnummern, Vertragscodes, Kunden-IDs. Ein **Erkenner** bringt
ihm eines bei. Er besteht aus einem Muster, optional Prüfziffern und Schlüsselwörtern, und einigen
erfundenen Beispielen, an denen er geprüft wird.

Verwenden Sie stattdessen einen [Begriff](../detection/#ihre-eigenen-begriffe), wenn es einen einzelnen,
genauen Wert zu verbergen gilt, etwa den Namen eines Kunden. Ein Erkenner ist für jeden Wert
gedacht, der auf eine bestimmte Weise geschrieben ist.

## Nie echte Werte einfügen

Ein Erkenner beschreibt ein Format. Seine Beispiele sind erfunden: Schreiben Sie sie anhand der
Beschreibung, oder nehmen Sie einen echten Wert und ändern Sie jeden Buchstaben und jede Ziffer,
wobei der Aufbau erhalten bleibt. `latet rules add` lehnt ein Beispiel ab, das Latet bereits als
echten Wert gesehen hat. Wenn ein KI-Agent den Erkenner für Sie schreibt, beschreiben Sie ihm das
Format in Worten: zwei Großbuchstaben, ein Bindestrich, sechs Ziffern, zu finden in der Spalte badge
des HR-Exports.

## Die Formate finden

Sie wissen nicht genau, welche Formate Ihre Dateien enthalten? Lassen Sie Latet nachsehen, auf
diesem Rechner:

```sh
latet rules suggest ./my-project
```

Der Befehl listet die Formate codeähnlicher Werte auf, die noch keine Regel abdeckt, etwa
`AA-999999` (A für einen Buchstaben, 9 für eine Ziffer), mit der Anzahl verschiedener Werte, die er
gesehen hat, den Dateien, in denen sie stehen, den Prüfziffern, die sie bestehen, und den
Bezeichnungen daneben (*badge*, *invoice_ref*). Er gibt nie einen Wert aus. In einem Git-Repository
liest er nur die Dateien, die Git kennt, sodass ignorierte Dateien außen vor bleiben. `--yaml`
entwirft für jedes Format einen Erkenner, mit Beispielen, die nach dem Format erfunden sind;
beschreiben Sie, was jeder findet, bevor Sie ihn hinzufügen. `--show-values` zeigt drei Werte jedes
Formats in Ihrem eigenen Terminal und verweigert die Ausführung innerhalb eines KI-Agenten oder einer
Pipe.

## Ein Beispiel

```yaml
recognizers:
  - name: employee-id
    description: Badge numbers of the HR system
    pattern: '\b[A-Z]{2}-\d{6}\b'
    context:
      words: [badge, employee]
    examples:
      match: ["badge AB-123456", "employee: QX-904417"]
      no_match: ["AB-123456 alone", "build AB-12345"]
```

Legen Sie ihn in einer eigenen Datei ab, etwa `rules.yaml`, und dann:

```sh
latet rules test rules.yaml     # every example, with the reason when one fails
latet rules add rules.yaml      # saves it in config.yaml and applies it
latet rules list
```

Ab dann wird `AB-123456` neben dem Wort *badge* als `EMPLOYEE_ID` maskiert, und das Dashboard nennt
die Regel `recognizer:employee-id`. Der Ersatzwert behält das Format: zwei Buchstaben, ein
Bindestrich, sechs Ziffern.

Die Seite **Regeln** im Dashboard erledigt dasselbe in ihrem Tab *Erkenner*: eine Tabelle Ihrer
Erkenner, jede Zeile mit dem Namen, dem Typ, dem, was er findet (das Muster, gekürzt), dem, was mit
seinen Werten geschieht, und wie er geladen wurde. *Erkenner hinzufügen* öffnet einen Bereich mit
jedem Feld: ausfüllen und *Ausprobieren* mit einem eigenen Text (nichts, was Sie dort eingeben,
wird aufbewahrt). Bevor Sie speichern, zeigt sie das Ergebnis jedes Beispiels und die Treffer in
gewöhnlichem Code. Wählen Sie Zeilen aus, um sie ein- oder auszuschalten oder zu löschen;
*Exportieren* und *Importieren* übertragen sie als Regeldatei. Ist der
[lokale Prüfer](../llm-review/#erkenner-aus-einer-beschreibung) eingeschaltet, schreibt *Entwurf aus
einer Beschreibung* den Erkenner aus einem Satz für Sie (*"Ausweisnummern: BD, ein Bindestrich und
sechs Ziffern"*), mit erfundenen Beispielen, und öffnet ihn im Bereich, wenn diese bestehen, damit
Sie ihn prüfen und speichern.

## Für ein einzelnes Projekt

Ein Erkenner kann zu einem [Projekt](../projects/) gehören statt zu ganz Latet:

```sh
latet rules add rules.yaml --repo .          # into the project's latet.yaml, shared with the team
latet rules add rules.yaml --project .       # into your own settings for the project
latet rules list --project .
```

Die Seite **Regeln** im Dashboard erledigt dasselbe, sobald Sie oben in der Auswahl das Projekt
wählen: Ihre Tabelle *Erkenner* zeigt die des Projekts, aus Ihren Einstellungen und aus der
`latet.yaml`, mit Ihren globalen, und ein neuer kommt dorthin, wo *Speichern in* in seinem Bereich
es sagt.

## Woraus ein Erkenner besteht

| Schlüssel | Was er tut |
| --- | --- |
| `name` | Kleinbuchstaben, Ziffern und Bindestriche, eindeutig. |
| `type` | Der Typ, der für seine Werte angezeigt wird, in Großbuchstaben. Ist er nicht gesetzt, der Name in Großbuchstaben. Er darf kein Typ sein, den Latet bereits hat. |
| `pattern` | Ein regulärer Ausdruck in [RE2](https://github.com/google/re2/wiki/Syntax). Eine Gruppe namens `value` wählt den Teil aus, der maskiert wird. |
| `checksum` | Prüfziffern, die der Wert bestehen muss: `luhn`, `iban`, `mod97-10`, `mod11-10`, `ean`, `verhoeff`, `damm` oder eine gewichtete Summe. Auch die Ersatzwerte bestehen sie. |
| `context` | Schlüsselwörter (`words`, unabhängig von Groß- und Kleinschreibung; `acronyms`, in genau dieser Schreibweise), die in derselben Zeile stehen müssen, innerhalb von `window` Zeichen, oder in der Spaltenüberschrift einer Tabelle. |
| `examples` | `match` und `no_match`: erfundene Texte, die bei jedem Laden der Regeln geprüft werden. |
| `class` | `custom` oder `secret` für Werte, die Ihren Rechner nie im Klartext verlassen dürfen: Sie werden wie üblich maskiert, und eine Anfrage, die einen im Klartext enthielte, wird gestoppt, auch in Claude Code. |
| `policy` | `mask` (Standard), `redact` oder `allow`. Ein Eintrag in `policies` hat Vorrang. |
| `surrogate` | `shape` oder `{keep: 3}`, um ein Präfix unverändert zu lassen. |
| `enabled` | `false` behält die Regel in der Datei, ausgeschaltet. |

Die [Konfigurationsreferenz](../../reference/configuration/) listet jeden Schlüssel auf, und mit dem
[JSON Schema](https://docs.latet.ai/config.schema.json) kann Ihr Editor sie vervollständigen.

## Muster sind RE2

RE2 läuft in linearer Zeit, daher kann kein Muster eine Anfrage aufhalten. Es kennt keine
Lookarounds (`(?=`, `(?<=`) und keine Rückwärtsreferenzen (`\1`). Wo Sie ein Lookbehind verwenden
würden, nehmen Sie den Kontext ins Muster auf und markieren Sie den Wert mit einer benannten Gruppe:

```yaml
recognizers:
  - name: contract-code
    pattern: '(?i)\bcontract[ #:]+(?P<value>[A-Z]{3}\d{5})\b'
    examples:
      match: ["Contract #KLM48213"]
      no_match: ["contract signed"]
```

Nur `KLM48213` wird maskiert. Schreiben Sie Muster in YAML in einfache Anführungszeichen, damit
Backslashes erhalten bleiben.

Ein Wert braucht einen Buchstaben oder eine Ziffer. Ein Treffer aus reinen Sonderzeichen, etwa das
`---` einer Markdown-Tabelle, bleibt unverändert, da sein Ersatzwert das Original selbst wäre;
`latet rules test` und *Ausprobieren* weisen darauf hin.

## Präzise Regeln

Eine Regel, die überall in gewöhnlichem Code anschlägt, würde den Code selbst maskieren, daher prüft
Latet jede an einer Stichprobe der Python-Standardbibliothek: Bei mehr als 50 Treffern wird sie
ausgeschaltet. Schlüsselwörter und Prüfziffern helfen dagegen. Zum Beispiel eine Nummer, deren
letzte Ziffer eine Luhn-Prüfziffer ist:

```yaml
recognizers:
  - name: card-ref
    type: CARD_REF
    pattern: '\bCR(?P<value>\d{8})\b'
    checksum: luhn
    class: secret
    examples:
      match: ["ref CR12345674"]
      no_match: ["ref CR12345678"]
```

`latet rules test rules.yaml --corpus src/` zeigt auch, wie viele Werte die Regel in Ihren eigenen
Dateien finden würde. Der Befehl gibt nur ihre Formate aus (`AA-999999 x12`), nie die Werte.

## Wenn etwas nicht stimmt

Ein Erkenner mit einem Fehler (ein Muster, das RE2 ablehnt, eine unbekannte Prüfsumme, ein falsch
geschriebener Schlüssel) wird ausgeschaltet; die anderen arbeiten weiter, und die Datei behält ihn
so, wie Sie ihn geschrieben haben. `latet reload`, `latet doctor` und die Seite **Regeln** im
Dashboard sagen, welcher es ist und warum, und `latet rules test` erklärt jedes Beispiel.
`latet config validate` prüft die ganze Datei.

Sie schreiben Erkenner mit einem KI-Agenten? Verweisen Sie ihn auf
[`llms-rules.md`](https://github.com/Latet-AI/latet/blob/main/llms-rules.md) im Repository:
dieselben Schritte, für ihn geschrieben.
