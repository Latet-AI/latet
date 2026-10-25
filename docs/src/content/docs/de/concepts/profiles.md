---
title: Profile und Richtlinien
description: Wählen Sie, wie viel Latet maskiert (balanced, max, secrets) und was mit jeder Art von Wert geschieht.
---

## Profile

Ein Profil legt fest, welche [Detektoren](../detection/) aktiv sind. Es gibt drei:

| Maskiert | `balanced` (Standard) | `max` | `secrets` |
| --- | --- | --- | --- |
| Geheime Werte: API-Schlüssel, Tokens, Passwörter, private Schlüssel | ✅ | ✅ | ✅ |
| Die Werte geheimer Spalten und Felder (`password`, `api_key`, `reset_token`...) in Tabellen und JSON | ✅ | ✅ | ✅ |
| Ihre eigenen Begriffe | ✅ | ✅ | ✅ |
| Ihre eigenen [Erkenner](../custom-recognizers/) | ✅ | ✅ | ✅ |
| Werte, die schon im Tresor stehen | ✅ | ✅ | ✅ |
| Personenbezogene Daten: E-Mail-Adressen, Telefonnummern, IBANs, Kartennummern, Umsatzsteuer-Identifikationsnummern, öffentliche IP-Adressen, Bitcoin-Adressen | ✅ | ✅ | ❌ |
| Nationale Identifikations- und Steuernummern ([nach Land](../detection/)) | die Länder in `id_regions` | alle Länder | ❌ |
| Die Identität dieses Rechners: Ihr Benutzername in Pfaden, Hostname, Git-Autor und die Domain seines Unternehmens | ✅ | ✅ | ❌ |
| Private IP-Adressen (`10.x`, `192.168.x`...) | ❌ | ✅ | ❌ |
| Jeder Wert der Tabellen, des JSON und der `key=value`-Paare, die ein Tool ausgibt ([strikter Datenmodus](../production-data/#strikter-datenmodus)) | ❌ | ✅ | ❌ |
| Namen und Anschriften, mit dem [optionalen lokalen Namensmodell](../../getting-started/installation/) | in Ihren Prompts, Anhängen und im Text in Bildern | überall | ❌ |
| Was kein Muster findet, mit dem [optionalen lokalen Prüfer](../llm-review/) | wenn er eingeschaltet ist | wenn er eingeschaltet ist | wenn er eingeschaltet ist |
| Der Text in Bildern, mit der [optionalen Texterkennung](../images/) | maskiert | nicht gesendet | maskiert |
| PDFs sowie Bilder ohne Texterkennung | gesendet und markiert | nicht gesendet | gesendet und markiert |

- **`balanced`** passt zu den meisten Arbeiten an Code. Es ist das Standardprofil.
- **`max`** fügt private IP-Adressen und die Identifikationsnummern aller Länder hinzu, schaltet den
  [strikten Datenmodus](../production-data/#strikter-datenmodus) ein (jeder Wert einer Tabelle, die ein
  Tool ausgibt, wird maskiert, auch eine ID oder ein Status), sucht überall nach Namen, wenn das
  lokale Modell installiert ist, und weigert sich, eine Anfrage zu senden, die ein Bild oder ein PDF
  enthält. Die Fehlermeldung sagt, was zu tun ist: das Bild aus der Unterhaltung entfernen (in
  Claude Code mit `/rewind`) oder zu `balanced` wechseln. Ein Projekt von Ihnen kann Bilder mit
  maskiertem Text senden, mit `images: mask`.
- **`secrets`** maskiert nur geheime Werte und Ihre eigenen Begriffe. Personenbezogene Daten und
  Ihre Identität gehen durch. Wählen Sie es, wenn Fehlalarme bei personenbezogenen Daten Sie
  behindern.

### Ein Profil wählen

- **Wenn Sie Ihre Agenten dauerhaft über Latet leiten:** `latet global-install --profile max`.
- **Im Dashboard:** die Seite **Regeln**. Die Änderung gilt sofort für neue Anfragen, ohne
  Neustart.
- **In `config.yaml`:** Setzen Sie `profile: max`, und führen Sie dann `latet reload` aus, um es ohne
  Neustart anzuwenden.

### Feinabstimmung

Über das Profil hinaus können Sie auf der Seite **Regeln** im Dashboard und in `config.yaml` wählen:

- `pii`: welche Typen personenbezogener Daten erkannt werden, zum Beispiel `[EMAIL, IBAN]`. Die
  Liste ersetzt die Wahl des Profils: Ein Typ, den sie auslässt, wird nicht erkannt, auch nicht die
  Identifikationsnummern aus `id_regions` (und die jedes Landes in `max`). Fehlt der Schlüssel, wird
  jeder Typ erkannt, den das Profil aktiviert. In Ihren Einstellungen für ein Projekt fügt `pii`
  stattdessen Typen zu den globalen hinzu.
- `phone_regions`: Länder, deren Telefonnummern ohne internationale Vorwahl erkannt werden
  (standardmäßig `IT`, `US`, `GB`).
- `include_private_ips: true`: private IP-Adressen auch in `balanced` maskieren.
- `data`: `strict` schaltet den strikten Datenmodus auch in `balanced` und `secrets` ein, `normal`
  schaltet ihn in `max` aus. Fehlt der Schlüssel, entscheidet das Profil.
- `disabled_secret_rules`: gitleaks-Regeln, die ausgeschaltet werden, etwa `generic-api-key`.
- `identity`: das Maskieren der Identität dieses Rechners ausschalten oder ihre Werte selbst
  eintragen.

Jeder Schlüssel ist in der [Referenz der Konfiguration](../../reference/configuration/) aufgeführt.

## Richtlinien

Eine Richtlinie bestimmt, was mit den Werten eines Typs geschieht, sobald sie erkannt werden. Der
Standard ist `mask`:

| Richtlinie | Was das Modell sieht | Was Sie zurückbekommen |
| --- | --- | --- |
| `mask` (Standard) | Ein Ersatzwert: `+44 20 3318 4452` | Das Original: `+44 20 7946 0958` |
| `redact` | Ein fester Platzhalter: `[REDACTED:PHONE]` | Der Platzhalter. Nichts wird wieder eingesetzt, daher enthält auch eine Datei, die der Agent schreibt, `[REDACTED:PHONE]` |
| `allow` | Das Original, unverändert | Das Original |

Legen Sie eine Richtlinie pro Typ in `config.yaml` fest oder auf der Seite **Regeln** im Dashboard:

```yaml
policies:
  PHONE: redact
  IP_ADDRESS: allow
```

Die Typnamen sind die, die das Dashboard neben jedem maskierten Wert anzeigt, etwa `EMAIL`,
`PHONE`, `IBAN`, `PASSWORD`, oder der Typ, den Sie einem eigenen Begriff gegeben haben. Typen ohne
Richtlinie werden maskiert.

`allow` lässt den Wert dieses Typs im Klartext, und nichts weiter: Ein Wert eines maskierten Typs,
der darin geschrieben steht, geht nie mit ihm hinaus. Mit `EMAIL: allow` erreicht `IT60X0542811101000000123456.ops@acme-intra.it`
das Modell mit der IBAN durch einen Ersatzwert ersetzt und `.ops@acme-intra.it` so, wie es
geschrieben ist; ein Geheimnis oder ein Begriff von Ihnen in einer Adresse wird mit der ganzen
Adresse maskiert, wie bisher. Ein Begriff oder eine Feldregel eines durchgelassenen Typs lässt im
Klartext, was nur sie finden; was ein Detektor eines maskierten Typs dort findet, wird maskiert.

`redact` eignet sich für Werte, die das Modell nie braucht, nicht einmal als Ersatzwert. Es ist
unumkehrbar: Das Modell erhält den Platzhalter, und Latet setzt das Original nie wieder in Antworten
oder Dateien ein. Ob Latet das Original aufbewahrt, hängt davon ab, wie der Wert gefunden wurde:

- Ein Wert, den die Detektoren finden, einschließlich Ihrer Begriffe und Erkenner, wird nicht
  aufbewahrt.
- Ein Wert, den der [lokale Prüfer](../llm-review/) findet oder den Sie im
  [Live-Prüfer](../live-review/) markieren, wird trotzdem aufbewahrt: versiegelt im Tresor, mit
  einem eigenen Ersatzwert, der nicht gesendet wird. So findet Latet ihn überall wieder, wo er
  vorkommt, und schwärzt ihn auch dort.

Ein Wert von mehr als 16.384 Zeichen, etwa ein ganzer JSON-String im
[strikten Datenmodus](../production-data/#strikter-datenmodus), wird ungeachtet der Richtlinie
seines Typs geschwärzt, es sei denn, dieser Typ ist `allow`: Er ist zu lang, als dass der Tresor ihn
aufbewahren könnte, wird also nie wieder eingesetzt, und eine Datei, die der Agent daraus schreibt,
enthält den Platzhalter.

## Zulassungslisten

Wenn Latet etwas maskiert, das nicht sensibel ist (ein falsch positives Ergebnis), weisen Sie es an,
diesen Wert in Ruhe zu lassen:

```yaml
allow_values:
  - demo@northwind-legal.com # exact value, any letter case
allow_patterns:
  - "test_[0-9a-f]{16}"      # RE2 regular expression, matched against the whole value
```

Im Dashboard geht das mit einem Klick: Öffnen Sie eine Anfrage, klicken Sie auf den maskierten
Wert, und wählen Sie **Nicht sensibel: immer zulassen**. Die gegenteilige Aktion,
**Immer maskieren**, fügt den Wert als eigenen Begriff hinzu. In einer Anfrage eines
[Projekts](../projects/) wählen Sie zuerst, ob das für *Dieses Projekt* oder *Überall* gilt.

Jeder solche Klick fügt einen Wert hinzu, die Liste wächst also. Auf der Seite **Regeln** des
Dashboards zeigt der Tab *Zugelassene Werte* sie als Tabelle, Werte und Muster zusammen mit ihrer
Art: Durchsuchen Sie sie, filtern Sie sie, öffnen Sie eine Zeile, um sie zu ändern (der Bereich
eines Musters probiert es an einem Wert aus), oder wählen Sie Zeilen aus und löschen Sie sie.
*Exportieren* und *Importieren* übertragen die Liste als CSV (`kind,value`); ein Import fügt hinzu,
was neu ist, und wendet es, da es Werte durchlässt, nur an, wenn Sie *Auch diese anwenden*
ankreuzen.

Der Name Latet bleibt immer im Klartext: Er ist das Tool, über das der Agent spricht, nie ein Wert
von Ihnen.

Um Ihre Regeln an einem Text auszuprobieren, ohne etwas zu senden, fügen Sie ihn in den
**Testbereich** des Dashboards ein.

## Im Client-Modus

Im [Client-Modus](../how-it-works/) (Claude Code) geschieht das Maskieren im Plugin, mit demselben
Profil, denselben Richtlinien und denselben Zulassungslisten. Der Daemon schreibt eine Anfrage nie
um: Er kann sie nur durchlassen oder blockieren. Wenn er eine blockiert, weil ein Wert den Rechner
im Klartext verlassen würde, öffnen Sie die Anfrage im Dashboard: Dieser Wert ist rot umrahmt. Wenn
er nicht sensibel ist, klicken Sie darauf, wählen Sie **Nicht sensibel: immer zulassen** wie oben,
und versuchen Sie es dann erneut.
