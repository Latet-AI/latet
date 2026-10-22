---
title: Erkennung
description: Wie Latet geheime Werte und personenbezogene Daten erkennt, vollständig auf Ihrem Rechner.
---

Die Erkennung läuft auf Ihrem Rechner oder auf dem Latet-Server, mit dem Ihr Rechner verbunden ist:
Es wird nie ein Erkennungsdienst in der Cloud aufgerufen. Mehrere Detektoren lesen jedes Stück Text, das eine Anfrage enthält. Diese Seite erklärt
sie mit Beispielen; die [Referenz der Detektoren](../../reference/detectors/) listet jeden Detektor
und jede Regel für geheime Werte auf.

## Geheimnisse

Geheime Werte werden in jedem [Profil](../profiles/) maskiert.

Auch Latets eigene Zugangsdaten sind enthalten: `LATET_PERSONAL_TOKEN` (`latet_pat_`),
`LATET_COMPUTER_KEY` (`latet_ck_`), `LATET_API_KEY` (`latet_key_`) und `LATET_ADMIN_TOKEN` (`latet_adm_`),
jeweils mit 43 URL-sicheren
Zeichen (`A-Z`, `a-z`, `0-9`, `_`, `-`) nach dem Präfix. In `https://<host>/u/<route-key>` werden
die 16 bis 128 URL-sicheren Zeichen des Routenschlüssels als `LATET_ROUTE_KEY` maskiert; der Host
und die übrigen Pfadsegmente bleiben erhalten. Das gilt auf Ihrem Rechner und in einem Serverbereich,
auch für Umgebungsabbilder, Claude Codes `settings.json` und Codex' `config.toml`. Lokale
Werkzeugaufrufe erhalten die echten Werte zurück. Ein externer Werkzeugaufruf mit einem Ersatzwert
wird gestoppt, wo Latet ihn abfangen kann (siehe [Schutzumfang](../protection-scope/)).

- **Bekannte Formate.** Der Standard-Regelsatz von [gitleaks](https://github.com/gitleaks/gitleaks)
  (222 Regeln) ist in Latet enthalten und läuft auf [RE2](https://github.com/google/re2), mit den
  Schlüsselwörtern, Entropie-Schwellenwerten und Allowlists, die gitleaks verwendet. Er deckt die
  API-Schlüssel und Tokens vieler Anbieter ab, etwa GitHub, Anthropic, OpenAI, AWS, Stripe oder Slack.
- **Werte, die sensiblen Namen zugewiesen werden**, in `.env`-Dateien, in der Konfiguration und im
  Code. Es entscheidet der Name, egal wie der Wert aussieht: `DB_PASSWORD=...`, `"api_key": "..."`
  und `$env:TOKEN = ...` werden maskiert, `APP_NAME=shop` und `DEBUG=true` nicht. Platzhalter wie
  `changeme`, `${DB_PASSWORD}` oder `<your-key>` bleiben unberührt, ebenso Verweise im Code wie
  `process.env.API_KEY`.
- **Private Schlüssel**: der gesamte Inhalt eines privaten PEM-, OpenSSH- oder PGP-Schlüsselblocks.
  Die Zeilen `BEGIN` und `END` bleiben sichtbar, sodass das Modell weiterhin weiß, dass dort ein
  Schlüssel steht.
- **Zugangsdaten**: das Passwort in `postgres://user:password@host/db` und anderen URLs, JSON Web
  Tokens sowie Zugangsdaten vom Typ `Bearer` oder `Basic`. In einer Verbindungszeichenfolge (einer
  Variablen `connectionString`, einer Spalte `connection_string`) werden nur die Zugangsdaten
  maskiert: die Werte von `Password=`, `PWD=`, `AccountKey=` und Ähnlichem oder das Passwort ihrer
  URL. Server, Benutzer und Datenbank bleiben, wie in `Server=db;User Id=app;Password=…;`.

Zum Beispiel eine `.env`-Datei, wie sie auf Ihrer Festplatte liegt:

```sh title=".env"
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=hunter2hunter
PAYMENTS_API_KEY=pk_9f2c4e7a1b3d5f8e0a6c
```

und was das Modell erhält, wenn der Agent sie liest:

```sh
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=jeeepo3ixhbkw
PAYMENTS_API_KEY=pk_3a8d1f6b0e9c2a7d4b5e
```

Der Hostname ist kein geheimer Wert und bleibt. Das Passwort und der Schlüssel werden durch
Ersatzwerte gleicher Länge und im selben Format ersetzt (Ihre werden andere sein).

### Platzhalter

Ein Wert, der nur den Platz eines geheimen Werts freihält, bleibt unberührt: in einer Zuweisung, als
Passwort einer URL, in einer geheimen Spalte und als Zugangsdaten einer Verbindungszeichenfolge
(`User Id={{username}};Password={{password}};`). Der ganze Wert muss einer der folgenden sein, in
beliebiger Groß-/Kleinschreibung:

| Form | Beispiele |
| --- | --- |
| In spitzen oder eckigen Klammern | `<password>`, `<your-key>`, `[password]` |
| Eine Variable oder ein Template | `${DB_PASSWORD}`, `$DB_PASSWORD`, `%DB_PASSWORD%`, `{{password}}`, `{env:DB_PASSWORD}`, `{file:./secret}` |
| *Your* und ein Name | `YOUR_PASSWORD`, `your-api-key`, `your.token` |
| Ein Wort, das für einen Wert steht | `changeme`, `change_me`, `password`, `passwd`, `secret`, `token`, `placeholder`, `example`, `redacted`, `todo`, `tbd` |
| Füllzeichen | `xxx`, `***`, `...`, `---`, `___` |
| Kein Wert | `null`, `none`, `nil`, `undefined`, `empty`, `true`, `false` |

`changeme123` ist kein Platzhalter: Es könnte ein echtes Passwort sein. In einer Zuweisung bleibt
auch ein Verweis im Code unberührt: `process.env.API_KEY`, `os.environ["TOKEN"]`, `os.getenv(...)`,
`config.`, `settings.`. Um einen Platzhalter einer anderen Form im Klartext zu lassen, fügen Sie ihn
den Werten hinzu, die nie maskiert werden ([Zulassungslisten](../profiles/#zulassungslisten)), in Ihren
Einstellungen für ein Projekt, wenn er zu einem gehört.

Auch ein Wert aus reinen Sonderzeichen bleibt unberührt, gleich wer ihn findet und wie lang er ist:
`!!!!!!!!` als Passwort, `--------------------` nach `Bearer`, ein Treffer eines Ihrer
[Erkenner](../custom-recognizers/) oder ein Wert, den der [lokale Prüfer](../llm-review/) meldet.
Der Ersatzwert behält die Sonderzeichen des Originals bei, er wäre also das Original selbst.
Ein Routenschlüssel von Latet ist die Ausnahme: Jedes Zeichen seines Ersatzwerts wird neu erzeugt,
auch wenn das Original nur aus `_` oder `-` besteht.

## Personenbezogene Daten

Personenbezogene Daten werden in den Profilen `balanced` und `max` maskiert. Jeder Treffer wird vor
dem Maskieren validiert, was Fehlalarme im Code gering hält:

| Typ | Beispiel | Wie ein Treffer bestätigt wird |
| --- | --- | --- |
| E-Mail-Adresse | `emily.carter@northwind-legal.com` | Die Top-Level-Domain wird mit der IANA-Liste abgeglichen, die für den Offline-Betrieb mitgeliefert wird |
| Telefonnummer | `+44 20 7946 0958` | Geparst und validiert mit [phonenumbers](https://github.com/daviddrysdale/python-phonenumbers), einer Portierung von Googles libphonenumber |
| IBAN | `GB82WEST12345698765432` | Länge je Land und Mod-97-Prüfsumme |
| Kartennummer | `4111 1111 1111 1111` | Präfix des Ausstellers und Luhn-Prüfsumme |
| Nationale Identifikationsnummer und Steuernummer | `SSN NNN-NN-NNNN`, `DNI NNNNNNNNA` | Prüfziffern und ein Schlüsselwort, wo die Nummer allein alles Mögliche sein könnte (siehe unten) |
| Umsatzsteuer-Identifikationsnummer | `DENNNNNNNNN`, `CHE-NNN.NNN.NNN MWST` | Das Länderpräfix und die Prüfziffern dieses Landes |
| IP-Adresse | `151.101.1.69` | Öffentliche Adressen; private nur in `max` oder mit `include_private_ips` |
| Bitcoin-Adresse | `bc1q...` | Prüfsumme nach Base58Check oder bech32 |

Die Erkenner für E-Mail-Adressen, IBANs, Kartennummern, codice fiscale und partita IVA sind aus
[Presidio](https://github.com/data-privacy-stack/presidio) übernommen.

### Identifikations- und Steuernummern nach Land

Latet kennt die nationalen Identifikations- und Steuernummern von 15 Ländern: Belgien, Deutschland,
Finnland, Frankreich, Italien, Kanada, Lettland, den Niederlanden, Polen, Portugal, Schweden, der
Schweiz, Spanien, dem Vereinigten Königreich und den Vereinigten Staaten. `id_regions` in
`config.yaml` legt die Länder fest (standardmäßig `IT`, `US` und `GB`); `latet global-install`
schlägt Ihr eigenes vor, und das Profil `max` nimmt sie alle. Die Seite **Regeln** im Dashboard
listet die Nummern jedes Landes auf.

Viele dieser Nummern bestehen aus neun oder elf bloßen Ziffern, wie unzählige IDs im Code. Deshalb
wird jede durch ihre Prüfziffern bestätigt, und die, die trotzdem alles Mögliche sein könnten,
brauchen ein Schlüsselwort in der Nähe: *SSN* oder *social security* neben einer US-amerikanischen
Social Security Number, *BSN* neben einer niederländischen Bürgerservicenummer, *Steuer-ID* neben
einer deutschen Steuer-Identifikationsnummer. Das Schlüsselwort zählt in derselben Zeile, wenige
Zeichen entfernt, oder in der Überschrift der Spalte, in der die Nummer steht (CSV-, TSV- und
Markdown-Tabellen):

```text
name,ssn,city
Jane,NNN-NN-NNNN,Boston        ← found: the column is called ssn
```

Die spanischen DNI und NIE, die Schweizer AHV-Nummer und die finnische Personenkennung tragen einen
Prüfbuchstaben oder einen unverwechselbaren Aufbau, daher brauchen sie kein Schlüsselwort. Eine
Nummer wird nie aus einer längeren Nummer, einer Version, einer Dezimalzahl oder einem Pfad
herausgelöst. Die [Referenz der Detektoren](../../reference/detectors/) enthält die vollständige
Liste, mit dem Aufbau und der Prüfung jeder Nummer.

Umsatzsteuer-Identifikationsnummern werden für die 27 EU-Länder, Nordirland, das Vereinigte
Königreich, die Schweiz und Norwegen erkannt, unabhängig davon, was `id_regions` festlegt, denn sie
tragen immer ihr Länderpräfix.

Einige Dinge bleiben absichtlich unberührt:

- E-Mail-Adressen unter den Dokumentations-Domains `example.com`, `example.net` und `example.org`
  oder unter `.example`, `.test`, `.invalid` und `.localhost` sowie Systemabsender wie `git@` oder
  `noreply@`;
- in Code und Tool-Ausgaben Ziffernfolgen ohne `+`, `00`, Leerzeichen oder Klammern, die weit öfter
  IDs, Datumsangaben oder Build-Nummern sind als Telefonnummern. In Ihren eigenen Nachrichten sind
  die Regeln lockerer. Nummern ohne internationale Vorwahl zählen nur für die Länder in
  `phone_regions` (standardmäßig `IT`, `US` und `GB`);
- Versionsnummern, die wie IP-Adressen aussehen, etwa `v1.2.3.4`.

### Namen

Namen in Freitext, etwa *Emily Carter hat das geschrieben*, erfordern ein statistisches Modell. Ist
das optionale [lokale Namensmodell](../../getting-started/installation/) installiert, findet Latet
Namen und Anschriften selbst; der [lokale Prüfer](../llm-review/), ein Sprachmodell auf Ihrem
Rechner oder auf einem Ihrer Server, findet sie ebenfalls, zusammen mit Gesundheitsangaben und den
anderen Werten, die kein Muster beschreibt. Ist keines von beiden vorhanden, wird ein Name nur
maskiert, wenn er der Name Ihres eigenen Git-Autors ist (siehe unten), einer Ihrer eigenen Begriffe
oder ein Wert, der schon im Tresor steht.

Nehmen Sie diesen Prompt:

```text
Hi, I'm Emily Carter and I live at 42 Mill Lane, Bristol BS1 4DJ.
```

Mit dem Namensmodell erhält das Modell in der Cloud im Profil `balanced` eine Version wie diese:

```text
Hi, I'm Hannah Mitchell and I live at 45 Moore Lane, Derby YN4 9CP.
```

Ihr Agent sieht trotzdem den echten Namen und die echte Anschrift, wenn die Antwort zurückkommt.

Wonach das Namensmodell sucht, hängt vom [Profil](../profiles/) ab:

- **`balanced`**: vollständige Namen (zwei Wörter oder mehr) und Anschriften, in dem, was Sie
  eingeben und anhängen, und im Text, der in Bildern gelesen wird. Eine Stadt oder Postleitzahl
  zählt nur neben einer Straße. Code, Tool-Ausgaben und Systemtext werden nicht gelesen.
- **`max`**: auch Namen aus einem Wort und Städte, und dazu Systemtext und Tool-Ausgaben.
- **`secrets`**: nie.

Einmal gefunden, kommt ein Name zu den [bekannten Werten](#bekannte-werte) und wird danach überall
maskiert, auch in Tool-Ausgaben, in jedem Profil, auch in `secrets`.

Das Modell arbeitet ohne Gewähr:

- **Es übersieht Namen.** Ungewöhnliche Namen, Initialen und kleingeschriebene Namen werden am
  häufigsten übersehen. Grußformeln und Titel vor einem Namen (*Hallo Paul*, *Dr. Sarah Mitchell*)
  werden nicht mitmaskiert.
- **Es hält manche Wörter für Namen.** Fachwörter wie *Redis* oder *Postgres* werden herausgefiltert,
  und `balanced` verlangt mindestens zwei Wörter.
- **Es ist bei langen Texten langsam.** Es liest auf einer Desktop-CPU etwa 700 Token pro Sekunde,
  in einem eigenen Thread, sodass andere Anfragen nicht darauf warten. Ein Text mit mehr als
  4.096 Token (8.192 in `max`) oder 16.000 Zeichen (64.000 in `max`) wird nicht gelesen, und die
  Anfrage wird im Dashboard markiert.

## Ihre eigenen Begriffe

Fügen Sie die Wörter hinzu, von denen nur Sie wissen, dass sie sensibel sind: Kundennamen, Codenamen
von Projekten, interne Domains. Diese Wörter werden in jedem Profil maskiert, wo immer sie vorkommen:

```yaml title="config.yaml"
terms:
  - value: Project Falcon
    type: PROJECT
  - value: Acme Logistics
    type: CLIENT
```

Mit diesen Begriffen erreicht *"Project Falcon startet im Mai mit Acme Logistics"* das Modell etwa
als *"Consolidated Margie startet im Mai mit Northwind Adatum"*. Standardmäßig wird ein Begriff in
beliebiger Groß-/Kleinschreibung erkannt und nur als ganzes Wort; `case_sensitive` und
`whole_word` ändern das. Sie können Begriffe auch auf der Seite **Regeln** im Dashboard hinzufügen oder mit
**Immer maskieren** bei einem beliebigen Wert in einer Anfrage.

Ein Begriff braucht mindestens einen Buchstaben oder eine Ziffer: Der Ersatzwert behält die
Sonderzeichen des Originals bei, daher lassen sich reine Sonderzeichen wie `--` oder `###` nicht
maskieren. Das Dashboard und `latet config` lehnen einen solchen Begriff ab. Steht einer von Hand in
`config.yaml` oder in der `latet.yaml` eines Projekts, wird er ausgelassen, und die Seite **Regeln**
und `latet config validate` weisen darauf hin.

Ein Begriff hat höchstens 8.192 Zeichen: Ein längerer wird für sich allein gesucht, und einer aus
etwa einer Million Buchstaben lässt sich gar nicht suchen. Das Dashboard, `latet config` und ein
Import lehnen einen längeren Begriff ab. Steht einer von Hand in `config.yaml`, wird er ausgelassen,
und `latet reload`, das Log des Daemons und `latet config validate` nennen den Eintrag; steht einer
in der `latet.yaml` eines Projekts, wird er ebenfalls ausgelassen, und die Seite des Projekts sagt
es.

Für ein Format statt eines Werts, etwa jede Ausweisnummer der Form `AB-123456`, schreiben Sie einen
[eigenen Erkenner](../custom-recognizers/): ein Muster, Prüfziffern und Schlüsselwörter, getestet an
erfundenen Beispielen.

## Die Identität dieses Rechners

In `balanced` und `max` erkennt Latet bei jedem Start, wer und wo Sie sind, und maskiert das:

- Ihren Benutzernamen im Betriebssystem in Pfaden des Home-Verzeichnisses:
  `C:\Users\anna\shop\app.py`, `/home/anna/...` oder `/Users/anna/...`. Ein Benutzername mit sechs
  oder mehr Zeichen wird überall maskiert, wo er vorkommt;
- Ihren Hostnamen;
- den Namen und die E-Mail-Adresse Ihres Git-Autors, aus `git config --global`, und die Domain
  dieser E-Mail-Adresse, wenn es die Ihres Unternehmens ist (`studio-rossi.it`, nicht `gmail.com`),
  wo immer sie vorkommt: Auch `git.studio-rossi.it` wird maskiert.

Weil Latet diese Angaben bei jedem Start neu erkennt, wird eine neue Git-E-Mail-Adresse oder ein
neuer Benutzername ohne Ihr Zutun maskiert. Der Abschnitt `identity` der
[Konfiguration](../../reference/configuration/) schaltet das aus oder legt die Werte fest: Sobald
Sie sie dort eintragen, verwendet Latet stattdessen diese.

## Bekannte Werte

Sobald ein Wert erkannt wurde, merkt sich Latet ihn im Tresor. Spätere Vorkommen werden auch dort
maskiert, wo die Detektoren sie allein nicht erkennen würden: etwa das Passwort aus Ihrer `.env`,
das später in einer Commit-Nachricht erwähnt wird. Zwei Arten werden so nicht gemerkt: Ein Wert, den
der strikte Datenmodus oder eine Feldregel in einer Tabelle oder in JSON maskiert, bleibt nur in
seiner Struktur maskiert ([Produktionsdaten](../production-data/#strikter-datenmodus)), und ein Wert
eines Typs mit `redact` wird gar nicht aufbewahrt. Nach den bekannten Werten sucht auch die
abschließende Prüfung in ausgehenden Anfragen.

Ein Wert wird ab vier Zeichen wiedererkannt, der Name einer Person ab drei (*Ugo*, *Ada*), und nur
so, wie er geschrieben ist: *Ugo* ist nicht *ugo* und auch nicht Teil von *Ugolino*.

## Wenn zwei Detektoren denselben Text finden

Überlappende Treffer werden zu einem zusammengeführt, der sie alle abdeckt, sodass ein Wert nie
halb ersetzt wird. Das Ergebnis erhält den Namen der stärkeren Art: zuerst geheime Werte, dann Ihre
Begriffe und die Identität dieses Rechners, dann personenbezogene Daten.

Fällt ein anderer Detektor als der für personenbezogene Daten und das lokale Namensmodell aus
(geheime Werte, Ihre Begriffe, Identität, bekannte Werte), schlägt das Maskieren fehl, und die
Anfrage wird blockiert: Nichts geht ungeprüft hinaus. Fällt der Detektor für personenbezogene Daten
oder das lokale Namensmodell aus, wird er übersprungen, und die Anfrage wird im Dashboard markiert.

## Warum RE2

RE2 findet Treffer in linearer Zeit, daher kann eine feindselige oder einfach riesige Eingabe kein
katastrophales Backtracking regulärer Ausdrücke auslösen und Ihren Agenten lahmlegen.

## Falsch positive und falsch negative Ergebnisse

Jeder Detektor wägt übersehene Werte gegen Fehlalarme ab. [Profile](../profiles/) legen fest, wo Sie
in diesem Zielkonflikt stehen, und [Zulassungslisten](../profiles/#zulassungslisten) beheben einzelne
Fehlalarme. Um zu sehen, wie ein Text maskiert würde, fügen Sie ihn in den **Testbereich** des
Dashboards ein: Nichts, was Sie dort eingeben, wird aufbewahrt oder an ein Modell gesendet.
