---
title: Einen Latet-Server einrichten und verwalten
description: Einen Server bereitstellen, einen Rechner verbinden und Personen, API-Schlüssel und gemeinsame Einstellungen verwalten.
---

Führen Sie Administrationsbefehle in Ihrem eigenen Terminal auf einem Rechner mit Latet aus.
Lesen Sie [Bereiche und Vertrauen](../../concepts/server/), bevor Sie Zugriff gewähren.
Ein Administrator richtet den Server ein; jede Person verbindet ihren eigenen Rechner.
Ein KI-Agent kann die Schritte erklären und den Rechner prüfen, aber die Befehle, die die
Administration ändern, `config get`, `rules export`, `latet server init`,
`latet server service install` und `latet connect` laufen nicht in einem Agenten: Verweisen Sie ihn auf [`llms-server.md`](https://github.com/Latet-AI/latet/blob/main/llms-server.md).
Geben Sie einem Agenten nie ein Administrationstoken: Mit einem laufen für ihn die Befehle, die nur
lesen (der Status, die Listen der Personen und Schlüssel, das Protokoll).

## Die Bereitstellung vorbereiten

Wählen Sie den [Betriebssystemdienst oder Compose](../../concepts/server-operations/).
Der Dienst braucht eine vertrauenswürdige systemweite Installation und geschützte
Konfiguration; das Compose-Serverprofil enthält PostgreSQL, Valkey, zwei Instanzen und Caddy.
Legen Sie die öffentliche HTTPS-Adresse fest und bereiten Sie Hauptschlüssel und
Geheimnisdateien vor. Die Initialisierung gibt ein Administrationstoken aus: bewahren Sie
es in einem privaten Terminal und Geheimnisspeicher auf. Folgen Sie für Container der
Initialisierung mit deaktivierten Docker-Logs in der Betriebsanleitung; gewöhnliche
Initialisierungslogs können das Token speichern.

Verwenden Sie ein Zertifikat, dem die Rechner vertrauen. Für einen ersten Versuch erstellen
Sie ein selbstsigniertes Serverzertifikat und prüfen seinen Fingerabdruck:

```sh
latet server certificate --make --url https://latet.acme.lan --out certs
latet server certificate certs/server.crt
```

Halten Sie `server.key` privat. Compose bindet `certs/server.crt` und `certs/server.key`
in Caddy ein; ohne Container konfigurieren Sie TLS an der Instanz oder ihrem
vertrauenswürdigen Reverse-Proxy. Teilen Sie den geprüften Fingerabdruck über einen
separaten vertrauenswürdigen Kanal mit. Ein Fingerabdruck aus einer ungeprüften
Netzwerkverbindung allein authentifiziert den Server nicht.

## Den Server prüfen

```sh
# a self-signed certificate: its fingerprint, verified as above, for every `latet server` command in this shell
export LATET_SERVER_FINGERPRINT="<fingerprint>"
latet server --server https://latet.acme.lan status
```

Geben Sie das Administrationstoken an der verdeckten Eingabeaufforderung ein. Die Antwort
nennt Datenbank, Valkey, Identitäten und die antwortende Instanz. Sie prüft nicht jeden
Agenten und belegt nicht, dass der gesamte Verkehr eines Rechners durch Latet läuft.

Für wiederholte Verwaltungsaufgaben liest `--admin-token-file FILE` eine private Datei
mit dem Token oder einem an diesen Server gebundenen Datensatz. Bewahren Sie sie außerhalb
von Repositorys und Agenten-Arbeitsbereichen auf. Beachten Sie die Berechtigungs- und
Herkunftsprüfungen unter [Serververwaltung](../../reference/server-administration/).
Exportieren Sie kein Administrationstoken in die Shell-Umgebung eines Kontos, unter dem Agenten laufen.

In der Antwort ist `database` immer wahr, da eine Instanz, die PostgreSQL nicht erreicht, gar keinen
Status gibt, und `valkey` aus demselben Grund meistens wahr: Lesen Sie ihren Zustand also in den
Metriken (`latet_database_up`, `latet_valkey_up`). `audit` gibt das letzte Ergebnis der Kette an
(`ok`, `broken` oder `unknown`), und `vault` listet die Identitäten bei 90 % oder mehr ihrer
Tresorgrenze.

## Einer Person einen eigenen Bereich geben

```sh
latet server --server https://latet.acme.lan people add "Emily Carter" --email emily.carter@acme.com
latet server --server https://latet.acme.lan people list
```

Der erste Befehl gibt einen Einmalcode aus. Geben Sie ihn nur dieser Person: Wer ihn
verwendet, kann sich als diese Person verbinden und ihr Dashboard öffnen. Die Ausgabe
eines Codes wird im Administrationsprotokoll erfasst. Der Code funktioniert einmal, innerhalb von
24 Stunden. `people code "Emily Carter"` stellt einen weiteren aus, wenn er abgelaufen ist oder
verloren ging, und der vorherige funktioniert nicht mehr; schreiben Sie Codes nie in Tickets,
gemeinsame Kanäle oder Gespräche mit einem Agenten.

Die Rechner einer Person teilen einen Bereich: ihren Tresor, Verlauf und ihre
Einstellungen. Die Rechnertoken können Sie so ansehen:

```sh
latet server --server https://latet.acme.lan people tokens "Emily Carter"
```

Widerrufen Sie das Token eines verlorenen Rechners mit seiner aufgeführten ID und
`--revoke TOKEN_ID`. `people disable "Emily Carter"` stoppt innerhalb von Sekunden jedes ihrer
Token und lässt ihren Bereich bestehen (`people enable` gibt ihn zurück); `people remove "Emily Carter"`
löscht ihren aktiven Serverbereich samt Tresor, Verlauf und Einstellungen, sobald Sie bestätigen.
`keys disable`, `enable` und `remove` tun dasselbe für einen API-Schlüssel. Keiner der Vorgänge
löscht Kopien in Sicherungen, Browsern oder bei Modellanbietern. Lesen Sie vor dem Entfernen die
Bestätigung.

Bei einer Entfernung gehen die Daten der Person in PostgreSQL mit ihr, ihre Datensätze in Valkey werden einmal
von einer Instanz gelöscht (bei der nächsten Prüfung erneut, falls das nicht fertig wird), was dort bleibt, läuft
von selbst ab, spätestens nach acht Tagen, und ihre Dashboard- und AWS-Sitzungen enden sofort.

## Den Rechner einer Person verbinden

Wenn der Server bereit ist und der Administrator den Code der Person ausgestellt hat:

```sh
latet connect https://latet.acme.lan
```

Geben Sie den Einmalcode bei der Aufforderung ein. Fügen Sie bei einem selbstsignierten
Zertifikat `--fingerprint FINGERPRINT` mit dem beim Administrator geprüften Fingerabdruck
hinzu. Entfernen Sie keine Zertifikatsbindung, um einen Fehler zu umgehen. Der
Verbindungsdatensatz, auch in einer mit `LATET_CONNECTION` ausgewählten Datei, enthält
Metadaten und Fingerabdruck; das persönliche Token und der Rechnerschlüssel bleiben zusammen, getrennt davon,
im Anmeldedatenspeicher oder in einer privaten Datei.

Der verbundene Hilfsprozess für Claude Code wird in einem **Windows-Terminal** unterstützt.
Wenden Sie die Integration an und starten Sie Claude Code neu, oder starten Sie eine einzelne Sitzung:

```sh
latet agents apply claude
latet claude
```

`latet agents apply claude` und `latet global-install` schreiben das persönliche Token als
`LATET_BRIDGE_TOKEN` in die Claude-Datei `settings.json` des Benutzers; der Befehl nennt den Ort.
Dies ist die Installationsausnahme von der Regel, Tokens aus Agentendateien herauszuhalten.
Die private Bindung des Hilfsprozesses enthält Metadaten; er erhält das vorhandene Token
über stdin. Software, die diese Prozesseingabe beobachten kann, kann es lesen. Verbundene
Hilfsprozesse in Claude Desktop, VS Code, JetBrains, Linux und macOS werden durch diesen
Vertrag nicht unterstützt: unter Linux und macOS sagen diese Befehle das vorab und schreiben
kein Token, und das Plugin hält jeden Prompt zurück.

Dieses Token öffnet von überall, wo der Server erreichbar ist, die Bridge des Plugins und den Proxy
des Bereichs; es öffnet nie das Dashboard, ändert nie die Identität der Person und verringert nie ihren
Schutz: Die einzige Änderung, die es an ihren Einstellungen vornehmen kann, ist ein Ziel für eine Adresse
hinzuzufügen, was nichts verringert. Das Dashboard und die Identität brauchen zusätzlich den
Rechnerschlüssel, den `latet connect` mit dem Token im Anmeldedatenspeicher (oder in der privaten Datei)
aufbewahrt und den nur `latet dashboard` und `latet identity send` lesen, die beide die Ausführung in
einem Agenten verweigern; kein Agent erhält ihn.

Verwenden Sie auf dem verbundenen Rechner:

```sh
latet status
latet doctor
latet dashboard
latet identity send
latet disconnect
```

`status` und `doctor` liefern begrenzte Diagnosen, keinen Nachweis für jeden Agentenpfad.
`dashboard` öffnet das Server-Dashboard der Person; auf einem verbundenen Rechner kann ein Agent
es nicht ausführen. Nur `--print-url` gibt den privaten
Anmeldelink aus; halten Sie ihn privat. `config`, `rules`,
`llm`, `project`, `identity` und `reload` verweigern im verbundenen Zustand das Lesen wie das
Ändern von Einstellungen; nur `config schema`, `rules test` und `identity send` laufen.
Verwenden Sie das Server-Dashboard oder Administrationsbefehle. Mit `latet identity send`
senden Sie die Identitätsangaben dieses Rechners ausdrücklich erneut. Auch `latet start`
verweigert: im verbundenen Zustand nutzt niemand den Daemon dieses Rechners.

`latet disconnect` widerruft das Token dieses Rechners und entfernt die Agentenbindungen
der Verbindung, einschließlich des gespeicherten Claude-Tokens. Der Widerruf beendet auch
die Dashboard-Sitzungen der Person auf anderen Rechnern. Kann der Widerruf nicht bestätigt
werden, lesen Sie das Ergebnis und lassen Sie vor einem neuen Versuch einen Administrator
die Token-IDs prüfen. Auf einem verbundenen Rechner wird der Print-Modus (`claude -p`) nicht
unterstützt: das Plugin hält seine Anfragen zurück und sagt es. Verwenden Sie das interaktive
Terminal. Die [Claude-Code-Anleitung](../../agents/claude-code/) unterscheidet verbundene
Unterstützung von lokalen Oberflächen.

Die Kopien dieses Rechners zu entfernen, widerruft das Token nicht. Mit `LATET_KEYRING=off` speichert
Latet keine Zugangsdaten im Schlüsselbund des Betriebssystems, aber eine ältere Kopie kann noch dort sein:
`latet connect` lehnt ab, solange sie da ist, und `latet disconnect` liest sie, widerruft sie bei ihrem
Server und entfernt sie; ein gesperrter Schlüsselbund kann zum Entsperren auffordern. Wird ein Verbinden
oder Trennen unterbrochen, wird eine übrig gebliebene temporäre Token-Datei ungelesen gelöscht, sodass der
Rechner das darin enthaltene Token nicht widerrufen kann. Dann, oder wenn `latet disconnect` meldet, dass
eine Kopie bleibt, widerruft es ein Administrator auf dem Server: die Token der Person wie oben auflisten,
das richtige an seiner ID erkennen (die Bezeichnung mit dem Hostnamen ist nur ein Hinweis: zwei Rechner
können gleich heißen) und es mit `--revoke TOKEN_ID` widerrufen. Lässt sich das richtige Token nicht
unterscheiden, widerruft `--revoke-all` alle Token der Person, und jeder ihrer Rechner muss sich danach neu
verbinden.

## Einem Programm einen API-Schlüssel geben

Ein API-Schlüssel hat einen eigenen Bereich, aber kein persönliches Dashboard und keine
Rechnerregistrierung. Seine Einstellungen und Regeln verwaltet der Administrator. Erstellen Sie
das Geheimnis in einer neuen privaten Datei:

```sh
latet server --server https://latet.acme.lan keys add "Billing CI" --out billing.key
latet server --server https://latet.acme.lan keys list
```

Verwenden Sie dafür einen geschützten Ordner außerhalb des Repositorys. Der Befehl gibt
Identität und Dateipfad aus, ohne das Geheimnis anzuzeigen. Speichern Sie den Schlüssel
im Geheimnisspeicher des Programms. `keys rotate "Billing CI" --out replacement.key`
ersetzt die Zugangsdaten; aktualisieren Sie Schlüssel und Routenadressen des Programms
zusammen. Auch ein Routenschlüssel in einer Adresse ist vertraulich.

Ein Schlüssel kann mehrere Token haben, jeder mit eigenen Berechtigungen aus `scan`, `mask`, `unmask`
und `forget`, für Programme, die die [API für Anwendungen](../../applications/) aufrufen: Eine
Anwendung, die ihre Logs maskiert versendet, braucht nur `mask`, das Support-Werkzeug, das sie liest,
nur `unmask`.

```sh
latet server --server https://latet.acme.lan keys add-token "Billing CI" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys tokens "Billing CI"
```

Der erste Token des Schlüssels darf analysieren, maskieren und wiederherstellen; `forget`, das Werte aus
dem Tresor des Schlüssels entfernt, erhält ein eigener Token
([Werte vergessen](../../applications/forget/)). Nur der erste Token des Schlüssels hat eine Adresse
für Modelle: Ein Token, der mit `keys add-token` erstellt wurde, wird dort abgewiesen, welche
Berechtigungen er auch hat, und ruft nur die API für Anwendungen auf, wo jede Wiederherstellung im
Administrationsprotokoll steht. Sobald ein Schlüssel mehrere Token hat, braucht
`keys rotate` die Option `--token` mit der ID, die `keys tokens` auflistet, und
`keys tokens "Billing CI" --revoke TOKEN_ID` stoppt einen. `keys limit "Billing CI" 1000000` gibt dem
Tresor des Schlüssels eine eigene Grenze, über oder unter dem `vault_limit` des Servers.

## Gemeinsame Einstellungen und Regeln von API-Schlüsseln festlegen

Die gemeinsamen Einstellungen enthalten, was der Server zum Betrieb braucht, für jeden Bereich
gleich: die Ziele, an die Anfragen gehen dürfen (`upstreams`), den gemeinsamen lokalen Prüfer
(`llm_review`), die Texterkennung (`ocr`), den Verlauf und wie lange er aufbewahrt wird
(`retention_days`).

```sh
latet server --server https://latet.acme.lan config get
latet server --server https://latet.acme.lan config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Ohne `--key` wirken `config get`, `set`, `add` und `remove` nur auf diese Einstellungen. Eine
Schutzregel, etwa ein Begriff oder das Profil, wird abgelehnt, mit einem Hinweis, wie man sie von
Hand teilt. Mit `--key` lesen und ändern dieselben Befehle die eigenen Einstellungen und Regeln eines
API-Schlüssels, und `latet server rules --key` verwaltet seine Erkenner und importiert eine
Regeldatei in seine Regeln:

```sh
latet server --server https://latet.acme.lan config --key "Billing CI" set profile max
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

`config get` und `rules export` können Werte im Klartext ausgeben (die Begriffe eines
API-Schlüssels, die Hinweise für den Prüfer). Halten Sie diese Ausgabe privat. Die eigenen Regeln
einer Person gehören in ihr Dashboard, wo sie sie selbst festlegt.

Um einem Team dieselben Regeln zu geben, teilen Sie sie von Hand: Exportieren Sie sie an einer
Stelle (`latet rules export FILE` auf einem Rechner, der mit keinem Server verbunden ist, oder
**Regeln exportieren** auf der Seite **Regeln** eines Dashboards), und importieren Sie die Datei an
jeder anderen (`latet rules import FILE` oder **Regeln importieren** im Dashboard); die Regeln eines
API-Schlüssels mit `latet server rules --key KEY import FILE`. Auf Rechnern, die mit keinem Server
verbunden sind, trägt die `latet.yaml` eines Projekts sie in seinem Repository. Regeln, die für
alle festgelegt sind und die niemand lockern kann, sind für die Enterprise Edition geplant.

Eine Änderung der gemeinsamen Einstellungen, die den Schutz einer Person verringert, etwa ein an
eine andere Adresse verschobenes Ziel oder ein ausgeschalteter Prüfer, wartet auf ihre Freigabe.
Das Speichern eigener Änderungen genehmigt keine wartende gemeinsame Änderung. Eigene Änderungen
können gespeichert werden, obwohl die Aktualisierung des wartenden Vorschlags fehlschlägt.
Lesen Sie das Ergebnis und laden Sie die Seite neu, bevor Sie weitere Änderungen vornehmen.
Bei einem API-Schlüssel gibt es niemanden, der eine solche Änderung freigibt: Sie erreicht den
Bereich des Schlüssels sofort, auch eine Änderung, die den Schutz verringert. Prüfen Sie daher, was
eine Änderung an den gemeinsamen Einstellungen für Ihre Programme bedeutet, bevor Sie sie vornehmen.
[Gemeinsame Einstellungen und eigene Regeln](../../concepts/server/) erläutert den Unterschied.

Verwenden Sie für den Alltag Administrationstoken mit Ablaufdatum. Die
[Verwaltungsreferenz](../../reference/server-administration/) beschreibt Ausstellung,
Widerruf, private Ausgabedateien, Wiederherstellung und das Lesen des Protokolls.
