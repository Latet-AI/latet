---
title: Export und Import
description: Ihre Regeln in einer Datei für Kollegen und Ihre ganze Installation in einer verschlüsselten Datei für einen anderen Rechner.
---

Zwei verschiedene Dinge lassen sich übertragen:

- **Ihre Regeln, für Kollegen:** Begriffe, Erkenner, Richtlinien und der Rest, in einer YAML-Datei.
  Importiert ergänzen sie deren eigene Regeln.
- **Ihre Installation, für einen anderen Rechner:** Ihre Einstellungen und, wenn Sie möchten, der
  Tresor mit seinem Schlüssel, in einer einzigen verschlüsselten Datei.

## Ihre Regeln, für Kollegen

```sh
latet rules export acme-rules.yaml     # here
latet rules import acme-rules.yaml     # on your colleague's machine
```

Oder **Regeln exportieren** und **Regeln importieren** oben auf der Seite **Regeln** im Dashboard:
Der Import zeigt, was er tun würde, bevor sich etwas ändert. Die Tabellen *Begriffe*, *Erkenner* und
*Zugelassene Werte* haben ein eigenes *Exportieren* und *Importieren*, nur für diese Liste: die
Begriffe und die Werte als CSV, die Erkenner als Regeldatei. Ein solcher Export hat dieselben
Vorkehrungen: Ein Erkenner, dessen Beispiele einen Wert enthalten, den Latet tatsächlich gesehen
hat, bleibt draußen, und eine CSV-Zelle, die eine Tabellenkalkulation als Formel ausführen würde,
wird nach einem Apostroph geschrieben. Ein solcher Import geht durch dieselbe Vorschau, fügt hinzu
und ersetzt nie, und hält auf dieselbe Weise zurück, was den Schutz senkt; die Tabelle *Erkenner*
nimmt eine Datei, die nur Erkenner enthält.

**Was die Datei enthält:**
- die Begriffe, Erkenner, Richtlinien und Feldregeln;
- die Typen personenbezogener Daten und die Länder, deren Identifikations- und Telefonnummern
  erkannt werden;
- das Profil, die privaten IP-Adressen, die lokale Namenserkennung, den strikten Datenmodus mit den
  Dateien und Webadressen, die er erfasst, den Dump-Schutz, die Bilder und die Texterkennung;
- die Ausnahmen: Werte und Muster, die nie maskiert werden, ausgeschaltete Regeln für geheime Werte,
  die Tools, die der strikte Datenmodus erfasst;
- die [Hinweise](../llm-review/#ihre-hinweise) für den lokalen Prüfer.

Nie die Identität, den Port, die Projektordner oder die Server dieses Rechners.

Eine Regeldatei enthält höchstens 2 MB. Über 5.000 Begriffen, 500 Erkennern, 500 Feldregeln, 5.000
Werten oder 500 Mustern, die nie maskiert werden, werden nur die ersten gelesen, und der Import sagt
das. Eine Datei, die ein neueres Latet erstellt hat, wird abgelehnt.

**Die Begriffe sind Werte im Klartext.** Der Name eines Kunden unter Ihren Begriffen steht
unverändert in der Datei: Versenden Sie die Datei so, wie Sie die Namen selbst versenden würden. Ein
Erkenner, dessen Beispiele Werte enthalten, die Latet als echte Werte gesehen hat, wird ausgelassen,
da Beispiele erfunden sein sollten.

**Ein Import erhöht den Schutz, und das gilt sofort:**
- neue Begriffe, Erkenner und Feldregeln werden hinzugefügt, und Ihre bleiben;
- ein strengerer Wert als Ihrer wird übernommen: ein höheres Profil, `redact` für einen Typ, den Sie
  maskieren;
- ein Erkenner mit dem Namen oder dem Typ eines Ihrer Erkenner oder eine Regel für ein Feld, für das
  Sie bereits eine Regel haben, wird ausgelassen: Ihre Fassung bleibt erhalten.

**Was den Schutz verringern würde, wird aufgelistet und ausgelassen:** die Ausnahmen, ein
schwächerer Wert, die Hinweise für den Prüfer, ein Erkenner, der durchlässt, was er findet
(`policy: allow`), ein Begriff oder eine Feldregel mit einem Typ, den Ihre Richtlinien durchlassen
(das Gefundene bliebe im Klartext), ein im Klartext gelassenes Feld. `--all` oder *Auch diese
anwenden* im Dashboard wendet sie ebenfalls an. Führt ein Agent oder ein Skript den Import aus,
macht `--all` den ganzen Import zu einer einzigen Änderung, die wie jede
[Änderung, die den Schutz verringert](../agents-and-settings/), auf Ihre Freigabe wartet: Was den
Schutz erhöht, wartet mit ihr, und nichts vom Import gilt, bis Sie sie freigeben. `--dry-run` zeigt
nur, was ein Import tun würde.

```text
$ latet rules import acme-rules.yaml
Would add:
  terms: Fonderia Brambilla, Banca Ligure
  recognizer badge-number
  policy for IBAN: redact (yours: mask)
Yours already, or kept as yours:
  recognizer ticket: yours, with the same name, is kept
Left out, it would lower protection:
  values never to mask: acme-dev
--all applies them too: they wait for your approval.
saved C:\Users\you\AppData\Roaming\latet\config.yaml
Rules reloaded.
```

**Was gilt, sagt die Antwort des Daemons.** Der Befehl sagt zuerst, was er hinzufügen würde, speichert
dann die `config.yaml`, bittet den Daemon, sie neu zu laden, und sagt, was daraus wurde: *Rules
reloaded.* (Exit-Code `0`), eine Änderung, die auf Ihre Freigabe wartet (`3`), oder, wenn der Daemon
nicht geantwortet hat, dass die Datei gespeichert ist und nicht bestätigt ist, ob sie gilt (`1`:
`latet reload` wendet sie an, sobald der Daemon läuft). Der Befehl liest die Repositorys mit einer
eigenen `latet.yaml`, die Latet gesehen hat, nicht: Der Daemon wägt den Import beim Neuladen in ihnen
ab, und eine Ergänzung, die den Schutz in einem von ihnen verringert, wartet ebenfalls auf Ihre
Freigabe.

Auch eine `config.yaml` oder die `latet.yaml` eines Projekts lässt sich importieren: Was darin keine
Regel ist (der Port, die Identität...), wird ausgelassen, und der Import sagt das.

### Die Regeln eines Projekts

Die geteilten Regeln eines Projekts werden bereits mit ihm übertragen: Seine
[`latet.yaml`](../projects/), die das Dashboard schreibt, wenn Sie die Regeln ändern, liegt im
Repository, und wer es klont, hat sie.

Ihre eigenen Einstellungen für ein Projekt, etwa Kundennamen, die Sie aus dem Repository heraushalten,
oder seine Ausnahmen, ergeben eine eigene Datei:

```sh
latet rules export shop-rules.yaml --project C:\dev\shop
latet rules import shop-rules.yaml --project D:\work\shop
```

Im Dashboard bietet auch die Seite **Regeln** eines Projekts beides, unter
*Ihre Einstellungen für dieses Projekt*: *Diese Einstellungen exportieren* und
*Einstellungen importieren*.

### Auf einem Latet-Server

Auf einem Rechner, der mit einem [Latet-Server](../server/) verbunden ist, liegen Ihre Regeln in
Ihrem Bereich auf dem Server, und `latet rules export` und `import` verweigern den Dienst.
**Regeln exportieren** und **Regeln importieren** oben auf der Seite **Regeln** in Ihrem Dashboard
auf dem Server funktionieren wie oben beschrieben, mit Ihren eigenen Regeln: Ein Team teilt seine
Regeln als Datei, und jede Person importiert sie. Die `latet.yaml` eines Projekts gilt dort nicht:
Importieren Sie sie auf dieselbe Weise, um ihre Regeln zu verwenden.

Die Regeln eines API-Schlüssels exportiert und importiert ein Administrator:

```sh
latet server --server https://latet.acme.lan rules --key "Billing CI" export billing-rules.yaml
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

Regeln, die auf einem Server für alle festgelegt sind und die niemand lockern kann, sind für die
Enterprise Edition geplant.

## Ihre Installation, für einen anderen Rechner

```sh
latet backup latet.backup --vault    # here
latet restore latet.backup           # on the other machine
```

`latet backup` fragt zweimal nach einer Passphrase (mindestens 12 Zeichen: Ein paar Wörter sind
leicht zu merken und schwer zu erraten) und schreibt eine einzige Datei:

| | Ohne `--vault` | Mit `--vault` |
| --- | --- | --- |
| Ihre Einstellungen (`config.yaml`) | ja | ja |
| Die Zertifikate, denen der lokale Prüfer vertraut | ja | ja |
| Der Tresor und sein Root-Schlüssel | nein | ja |
| Der Verlauf des Dashboards | nein | mit `--history` |

- **Mit dem Tresor** erzeugt der andere Rechner dieselben Ersatzwerte wie dieser, kennt jeden bisher
  gefundenen Wert (von den Detektoren, vom lokalen Prüfer, von Ihnen im Live-Prüfer) und setzt in
  Unterhaltungen, die hier begonnen haben, weiterhin die echten Werte wieder ein. Die Datei enthält
  dann Ihre echten Werte, verschlüsselt: Bewahren Sie sie dort auf, wo Sie auch die Werte selbst
  aufbewahren würden.
- **Ohne ihn** erzeugt der andere Rechner eigene Ersatzwerte.
- **Nie darin:** der API-Schlüssel des lokalen Prüfers, das Bridge-Token (zwischen dem
  Claude-Code-Plugin und dem Daemon), die Logs, das lokale Namensmodell, die Modelle für die
  Texterkennung und die Einstellungen Ihrer Agenten. Jeder Rechner hat seine eigenen.

`latet restore` liest und prüft zuerst die ganze Datei, zeigt, was sie enthält und was in ihren
Einstellungen den Schutz gegenüber Ihren verringert, und fragt Sie. Dann:

- **Dieser Rechner behält seine eigenen Einstellungen:** seinen Port, seine Identität (zu der des
  Backups hinzugefügt, damit beide maskiert werden), und von `capture_dir` und `api_key_file` wird
  nichts übernommen, da das Pfade des anderen Rechners sind.
- **Projektordner an einem anderen Ort:** `--paths C:\Users\anna\dev=D:\work` verschiebt diejenigen
  unter dem ersten Ordner unter den zweiten. Ordner, die es hier nicht gibt, werden aufgelistet.
- **Hier gibt es schon einen Tresor:** Hat der Tresor dieses Rechners eigene Werte, wird er nur mit
  `--replace` ersetzt. Diese Werte werden zuerst in `before-restore-<date>.latet` neben dem Tresor
  gesichert, mit derselben Passphrase, damit `latet restore` sie zurückholen kann.
- **Der Daemon** wird davor gestoppt und danach wieder gestartet, und nichts startet ihn, solange
  eine Wiederherstellung läuft. Eine abgebrochene Wiederherstellung (ein Absturz, ein Stromausfall)
  lässt ihn gestoppt, bis `latet restore --abort` den vorherigen Zustand wiederherstellt.
- **Was dieser Rechner hatte** (der Tresor, der Verlauf, `config.yaml`), bleibt in einem datierten
  Ordner `before-restore-` neben dem Tresor.

Danach: `latet llm key` für den API-Schlüssel des Prüfers, `latet ner install` für die lokale
Namenserkennung, `latet ocr install` für den Text in Bildern (bis dahin werden Bilder so gesendet,
wie sie sind, mit der Markierung *Nicht gelesen*, oder mit `images: mask` gar nicht gesendet) und
`latet global-install` (oder `latet claude`) für Ihre Agenten.

**Nur in Ihrem Terminal.** Beide Befehle verweigern die Ausführung innerhalb eines Agenten: Sie
verarbeiten Ihren Tresor und seinen Schlüssel. `latet restore` braucht außerdem ein Terminal, um Sie
zu fragen, und verweigert die Ausführung, wenn der Root-Schlüssel aus `LATET_ROOT_KEY` oder
`LATET_ROOT_KEY_FILE` stammt, den ein wiederhergestellter Schlüssel nicht ersetzen könnte.

Ohne die Passphrase kann niemand die Datei lesen, auch Sie nicht. Eine veränderte, gekürzte oder aus
Teilen anderer Dateien zusammengesetzte Datei wird abgelehnt, bevor irgendetwas daraus verwendet
wird: siehe [Sicherheit](../../security/#wie-latet-sich-selbst-schützt).

### In Docker

Der Tresor und sein Schlüssel liegen im Volume `latet-data`, die Einstellungen in `latet-config`:
Wer beide Volumes kopiert, überträgt alles. Mit den Befehlen:

```sh
docker exec -it latet latet backup /data/latet.backup --vault
docker cp latet:/data/latet.backup .
```

Auf dem anderen Rechner, mit gestopptem Container:

```sh
docker compose stop
docker cp latet.backup latet:/data/
docker compose run --rm -it latet latet restore /data/latet.backup
docker compose start
```

Wenn der Container seinen Schlüssel aus `LATET_ROOT_KEY_FILE` nimmt, verweigert `latet restore` die
Ausführung: Übertragen Sie stattdessen die Volumes `latet-data` und `latet-config` und die
Schlüsseldatei.
