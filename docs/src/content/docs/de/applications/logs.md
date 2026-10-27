---
title: Logs maskieren, bevor sie die Anwendung verlassen
description: Eine Anwendung maskiert ihre Logzeilen mit einem Latet-Server, bevor sie sie versendet, und ein Support-Werkzeug stellt die Zeile wieder her, die es braucht.
sidebar:
  label: Logs
---

Die Logs einer Anwendung nennen ihre Kunden: deren E-Mail-Adressen, IP-Adressen, IBANs, ab und zu
einen Token. Unverändert versendet, liegen sie monatelang in einem Log-Dienst und werden von allen
gelesen, die die Logs lesen. Mit der [API für Anwendungen](../) maskiert die Anwendung jeden Stapel
von Zeilen mit einem Latet-Server, bevor sie ihn versendet, und der Log-Dienst erhält Ersatzwerte. Da
ein Wert immer denselben Ersatzwert erhält, lassen sich die Zeilen eines Kunden weiterhin suchen,
zählen und über Tage verfolgen; wenn der Support wissen muss, wer es war, stellt ein Werkzeug mit dem
passenden Token die Zeile wieder her.

## Die Token

Ein API-Schlüssel für die Logs, deren Kunden Sie gemeinsam verfolgen wollen, mit einem Token für jede
Seite:

```sh
latet server --server https://latet.acme.lan keys add "Shop logs" --out shop-logs.key
latet server --server https://latet.acme.lan keys add-token "Shop logs" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop logs" support --unmask --out support.token
```

Die Anwendung erhält `shipper.token`, der maskieren kann und keinen echten Wert sehen kann; das
Support-Werkzeug erhält `support.token`, der wiederherstellen kann und sonst nichts. Der erste Token,
`shop-logs.key`, bleibt beim Administrator: Er hat alle Berechtigungen. Mit einem anderen Schlüssel
maskierte Zeilen erhalten andere Ersatzwerte; verwenden Sie also einen Schlüssel für die Dienste,
deren Logs Sie gemeinsam durchsuchen.

## Die Zeilen maskieren

Mit dem Python-Client `latet-client` (`pip install latet-client`, Python 3.10 oder neuer):

```python
from pathlib import Path

from latet_client import Latet, LatetError

latet = Latet("https://latet.acme.lan", Path("shipper.token").read_text().strip())


def ship(lines: list[str]) -> None:
    """Mask a batch of log lines, then send them to the log service."""
    try:
        results = latet.mask(lines, kind="data")
    except LatetError:
        keep_for_later(lines)  # never send the lines as they are
        return
    for result in results:
        if result.text is not None:
            send_to_log_service(result.text)
        else:  # blocked: the reason names no value
            send_to_log_service(f"[line withheld by Latet: {result.blocked}]")
```

Die Zeilen

```text
2026-10-26T09:14:02Z INFO payment accepted customer=mario.rossi@acme.it iban=IT60X0542811101000000123456
2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

gehen so hinaus:

```text
2026-10-26T09:14:02Z INFO payment accepted customer=tommaso.gallo@margie-6e5a.example iban=IT17F3945471087320356678487
2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41
```

Senden Sie die Zeilen in Stapeln, nicht einen Aufruf pro Zeile: Ein Aufruf nimmt bis zu 1.000 Zeilen,
und der Client sendet eine längere Liste in mehreren Aufrufen nacheinander. Die Zeilen eines Aufrufs
werden gemeinsam maskiert: Ein Wert, den eine Zeile des Aufrufs dem Tresor beibringt, wird auch in den
anderen Zeilen des Aufrufs maskiert. Ein Wert, den erst ein späterer Aufruf beibringt, wird in den
Zeilen der früheren Aufrufe, die schon beantwortet sind, nicht maskiert. Verwenden Sie `kind="data"`
für Logs ([Prosa und Daten](../#prosa-und-daten)).

Wenn der Aufruf scheitert, behalten Sie die Zeilen und versuchen es erneut, oder verwerfen Sie sie:
Senden Sie sie nie unmaskiert. Der Client wiederholt einen 429 und einen 503 von selbst (dreimal, nach
der Wartezeit, die der Server verlangt), bevor er den Fehler auslöst. Dieselben Zeilen zweimal zu
maskieren ergibt dieselben Ersatzwerte, ein erneuter Versuch ist also sicher.

Jede Abweisung des Servers ist ein `LatetError`, mit ihrem Status und ihren Worten, und ein Aufruf,
der nicht durchkam, ist ein `ConnectionFailed` (Status 0). `ConnectionFailed` tritt an die Stelle der
Fehler von `httpx`, auch derer eines `httpx.Client`, den Sie als `client=` übergeben, und enthält
keine Anfrage, sodass der Token in keinem Fehler steht: Fangen Sie die Fehler des Clients ab, nicht
die von `httpx`. Eine `http://`-Adresse eines anderen Rechners ergibt eine `ClearTextWarning`, weil
der Token und die Zeilen im Klartext reisen würden. Der Client urteilt allein nach der Adresse; auch
ein eigener `client=`, der den Server auf anderem Weg über TLS erreicht, erhält die Warnung also. Das
Python-Modul `warnings` schaltet sie stumm.

## Das Format der Zeilen

Was die Detektoren erkennen, wird überall in einer Zeile maskiert: E-Mail-Adressen, IBANs,
Kartennummern, öffentliche IP-Adressen, Token und Schlüssel, die Begriffe und Erkenner des
Schlüssels. Für den Rest setzen Sie die Regeln des Schlüssels
([gemeinsame Einstellungen und Regeln von API-Schlüsseln](../../getting-started/server/#gemeinsame-einstellungen-und-regeln-von-api-schlüsseln-festlegen)):

- **Feldregeln** maskieren die Werte eines Feldes nach seinem Namen, etwa `phone` oder
  `customer_name`, und der **strikte Datenmodus** (`data: strict`) maskiert jeden Wert eines
  Datensatzes ([Produktionsdaten](../../concepts/production-data/)). Sie finden einen Wert über seinen
  Schlüssel in JSON und in `schlüssel=wert`-Zeilen, in denen ein Wert mit Leerzeichen in
  Anführungszeichen steht: `phone="+39 347 123 4567"`. Ein Wert ohne Anführungszeichen endet am ersten
  Leerzeichen. Schreiben Sie Ihre Logzeilen als JSON, oder setzen Sie solche Werte in
  Anführungszeichen.
- **Begriffe** und **Erkenner** fangen Ihre eigenen Namen und Formate ab: den Namen eines Kunden,
  eine Bestellnummer der Form `ORD-123456` ([eigene Erkenner](../../concepts/custom-recognizers/)).
- **Namen** im freien Text einer Meldung werden mit dem auf dem Server installierten lokalen
  Namensmodell gefunden, in `prose`-Texten; in `data`-Texten mit dem Profil `max`.

`scan` zeigt mit einem Token, der analysieren darf, was eine Stichprobe Ihrer Zeilen maskieren würde, ohne
etwas zu behalten: Probieren Sie es mit echten Zeilen aus, bevor Sie das Maskieren einschalten.

## Eine Zeile wiederherstellen

Eine Person im Support kopiert eine maskierte Zeile aus dem Log-Dienst; ihr Werkzeug stellt sie wieder
her:

```python
support = Latet("https://latet.acme.lan", Path("support.token").read_text().strip())
line = "2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41"
print(support.unmask(line).text)
# 2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

Um alle Zeilen eines Kunden zu finden, maskieren Sie seine Adresse mit einem Token, der maskieren
darf, und suchen im Log-Dienst nach dem Ersatzwert. Jede Wiederherstellung wird im
Administrationsprotokoll des Servers festgehalten, mit dem Token und der Zahl der wiederhergestellten
Werte; `latet server log --action apps.unmasked` listet sie auf.

## Wie schnell, und wie viele Instanzen

Auf einer Instanz maskiert der Server etwa 190 Zeilen pro Sekunde für einen neuen Schlüssel und 410
bis 440 für einen Schlüssel, dessen Tresor 200.000 bis 1.000.000 Werte enthält; er stellt 1.500 bis
2.700 wieder her ([die Messungen](../#wie-schnell-es-ist)). Die Aufrufe eines Schlüssels laufen auf
einer Instanz nacheinander; ein Dienst, der schneller loggt, braucht also mehr Instanzen hinter dem
Lastverteiler, oder seine Zeilen werden auf Schlüssel aufgeteilt, deren Kunden nicht gemeinsam
verfolgt werden müssen.

Ein Aufruf hält die Zeilen im Speicher, bis er beantwortet ist: Versenden Sie aus einer eigenen
Warteschlange, damit ein langsamer oder unerreichbarer Server die Logs verzögert, statt sie zu
verlieren oder unmaskiert zu senden.

## Ohne Python

Jede Sprache kann die API über HTTPS aufrufen: ein `POST` mit einem JSON-Körper und dem Token in einem
Header `Authorization: Bearer`. Die [HTTP-Referenz](../../reference/apps-api/) enthält den Vertrag,
seine Fehler und eine OpenAPI-Beschreibung, aus der sich ein Client erzeugen lässt.
