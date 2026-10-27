---
title: Serverbetrieb
description: Speicher, Netzwerk, Schlüssel, Grenzen und Überwachung eines Latet-Servers betreiben.
---

Diese Anforderungen gelten für jede Instanz eines eingerichteten Latet-Servers. Halten
Sie öffentliche HTTPS-Adresse, PostgreSQL-Datenbank, Valkey und Hauptschlüssel auf allen
Instanzen konsistent. [Latet auf einem Server](../server/) erklärt, welche Daten den
Server erreichen und welche in seinen Speichern lesbar bleiben.

## Konfiguration und Schlüssel

`server.yaml` enthält Instanzeinstellungen. `--config` wählt die Datei für Serverbefehle;
`LATET_SERVER_CONFIG` wählt sie über die Umgebung. Die jeweiligen `LATET_SERVER_*`-Variablen
überschreiben Dateiwerte. Instanzeinstellungen sind von den gemeinsamen Einstellungen und den
eigenen Einstellungen und Regeln jeder Identität getrennt, die in PostgreSQL gespeichert werden.
Die generierte [Tabelle der Servereinstellungen](../../../reference/configuration/#server-settings)
führt auf Englisch jede Instanzeinstellung mit ihrem Standardwert und Zweck auf.

Übergeben Sie den Hauptschlüssel mit `master_key_file` oder der unterstützten
Umgebungseingabe für den Hauptschlüssel. Verwenden Sie vorzugsweise eine geschützte
Geheimnisdatei, kein Repository und keine Befehlszeile. Alle Instanzen benötigen denselben
Schlüssel. Sichern Sie ihn getrennt mit eingeschränktem Zugriff: Eine neue Zufallszeichenfolge
in der Datei rotiert keine verschlüsselten Daten und macht vorhandene Daten unlesbar.
Ein Tresoreintrag, der sich nicht öffnen lässt (mit einem anderen Schlüssel versiegelt oder in
der Datenbank geändert), wird ausgelassen: Das Log nennt seine ID, nie seinen Wert, und die
Maskierung geht mit den übrigen Einträgen weiter.

Verwenden Sie `database_password_file`, `valkey_password_file`, `metrics_token_file` und
`reviewer_key_file` für die jeweiligen Geheimnisse. Der Prüfschlüssel wird einmal beim
Start gelesen und nur an den von den Administratoren gewählten gemeinsamen Prüfdienst
gesendet. Er muss dessen Zugangsschlüssel sein, niemals Hauptschlüssel oder Administrationstoken.
Der Server sucht keinen Prüfschlüssel in der Umgebung, im Schlüsselbund oder in einem
gespeicherten lokalen Schlüssel einer Person.

## Betriebssystemdienste

Installieren Sie das Serverpaket an einem vertrauenswürdigen systemweiten Ort außerhalb
persönlicher Ordner. Bereiten Sie `server.yaml` mit den oben beschriebenen URLs,
Geheimnisdateipfaden und TLS-Einstellungen vor; der Dienst muss die geschützten Dateien
lesen können. Erstellen Sie den Hauptschlüssel vorab und initialisieren Sie in Ihrem
privaten Administrator-Terminal: das erste Administrationstoken wird einmal ausgegeben.
Erfassen Sie diese Ausgabe nicht in gemeinsamen Logs.

Unter Linux kommen zuerst einige Dinge, als root. Installieren Sie Latet mit dem Extra `server`
für die ganze Maschine: die Ordner von uv für Tools, Befehle und Python alle außerhalb der
persönlichen Ordner. Legen Sie den Benutzer und die Gruppe `latet` an, unter denen der Dienst
läuft, dann den Ordner, der die Einstellungen und die Geheimnisse enthält, mit dem Hauptschlüssel
und dem Passwort der Datenbank darin:

```sh
UV_TOOL_DIR=/opt/latet/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/latet/python \
  uv tool install --python ">=3.12,<3.14" "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin latet
install -d -m 0750 -o root -g latet /etc/latet
(umask 077 && openssl rand -base64 32 > /etc/latet/master.key)
(umask 077 && openssl rand -hex 32 > /etc/latet/db.password)   # or the password your database administrator gives
```

Legen Sie das Passwort von Valkey in `/etc/latet/valkey.password` und das TLS-Zertifikat samt
Schlüssel in `/etc/latet/server.crt` und `/etc/latet/server.key`. Schreiben Sie dann
`/etc/latet/server.yaml`; eine minimale für einen Host-Dienst:

```yaml
url: https://latet.acme.lan
database_url: postgresql://latet@db.acme.lan:5432/latet?sslmode=verify-full&sslrootcert=system
database_password_file: /etc/latet/db.password
valkey_url: rediss://valkey.acme.lan:6379/0
valkey_password_file: /etc/latet/valkey.password
master_key_file: /etc/latet/master.key
certificate: /etc/latet/server.crt
key: /etc/latet/server.key
```

Übergeben Sie `server.yaml` und jede darin genannte Geheimnisdatei, `key` eingeschlossen, an root
und die Gruppe `latet`, nur für diese lesbar (`0640` oder `0440`):

```sh
cd /etc/latet
chown root:latet server.yaml master.key db.password valkey.password server.key
chmod 0640 server.yaml master.key db.password valkey.password server.key
```

Fügen Sie `ner` und `ocr` zum Extra hinzu, für Namens- und Texterkennung. Die Installation
des Dienstes sagt, was davon fehlt: eine Installation unter einem persönlichen Ordner, kein
Benutzer oder keine Gruppe `latet`, oder eine Datei mit einem anderen Eigentümer oder Modus.

Die Standardkonfiguration ist `/etc/latet/server.yaml`, die Unit heißt `latet-server` und
das Datenverzeichnis ist `/var/lib/latet`:

```sh
latet server init --config /etc/latet/server.yaml
latet server service install --config /etc/latet/server.yaml --print
latet server service install --config /etc/latet/server.yaml
systemctl start latet-server
```

Unter Windows ist die Standardkonfiguration `C:/ProgramData/Latet/server.yaml`.
Der Dienst läuft als `NT SERVICE\LatetServer` mit geschützten Verzeichnis-ACLs.
Sein Log liegt in `C:/ProgramData/Latet/logs/server.log`, mit fünf rotierenden Dateien
von je 10 MiB:

```powershell
latet server init --config C:/ProgramData/Latet/server.yaml
latet server service install --config C:/ProgramData/Latet/server.yaml --print
latet server service install --config C:/ProgramData/Latet/server.yaml
Start-Service LatetServer
```

`--print` zeigt den Plan ohne Installation. Die Installation braucht Linux-root oder
Windows-Administratorrechte und aktiviert den Dienst beim Systemstart; starten Sie ihn
ausdrücklich, sobald Sie bereit sind. Agentenaufrufe werden auch mit `--print` oder
`--yes` abgelehnt. In Ihrem eigenen Terminal fragt die Installation nach, bevor sie etwas ändert,
mit `--yes` oder ohne; `--yes` lässt ein Skript von Ihnen, ohne Terminal, ohne die Frage
installieren. Die Installation im Container wird abgelehnt. Dies beschreibt das Verfahren, keinen
bereits installierten Dienst auf Ihrem Rechner.

## Das Compose-Serverprofil

Verwenden Sie `compose.yaml` und `deploy/Caddyfile` aus dem Repository. Bereiten Sie
ein privates Host-Verzeichnis `secrets/` vor: unter POSIX gehört es dem Administrator
und hat Modus `0700`; die einzeln eingebundenen Dateien haben Modus `0444`, damit
der jeweilige Containerbenutzer sie lesen kann. Beschränken Sie unter Windows die
Verzeichnis- und Datei-ACLs auf Administratoren und das benötigte Docker-Dienstkonto.
Compose-Bind-Secrets bilden Eigentümer oder Modi nicht um; `0600` für alle Dateien
kann sie im Container unlesbar machen.

Erstellen Sie vorher `master.key` (32 zufällige Bytes in base64), `db-admin.password`,
`db.password`, `valkey.password` und `metrics.token` in diesem Verzeichnis. Verwenden Sie
starke Zufallswerte; `db.password` und `valkey.password` müssen für die mitgelieferten
Startbefehle hexadezimal sein (`openssl rand -hex 32`). Ersetzen Sie keinen bestehenden
Hauptschlüssel als Abkürzung bei der Einrichtung. Für die optionale `reviewer.key` müssen
Dateieinstellung, Secret-Einbindung und Definition zusammen von ihren Kommentarzeichen
befreit werden. Diese Geheimnisse kommen nur aus Dateien, ohne Umgebungsvariablen als
Ersatz. Legen Sie die TLS-Dateien in `certs/` und schützen Sie den privaten Schlüssel.

Die Instanzen verbinden sich mit PostgreSQL als Rolle `latet`, die kein Superuser ist. Auf
einem neuen Datenvolume legt `deploy/postgres-init.sh` sie mit dem Passwort aus `db.password`
an, als Eigentümerin der Datenbank `latet`. Der eigene Superuser von PostgreSQL, `postgres`,
hat `db-admin.password`, das keine Instanz erhält: Eine kompromittierte Instanz kann kein
Programm im Container der Datenbank ausführen (`COPY … PROGRAM` braucht einen Superuser).
Das Skript läuft nur, solange das Volume neu ist, und ändert keine bestehende Datenbank.
Eine Bereitstellung, deren Volume davor angelegt wurde, behält `latet` als eigenen Superuser
von PostgreSQL, ein Attribut, das PostgreSQL dieser Rolle nie nehmen lässt
(`ALTER ROLE latet NOSUPERUSER` wird abgelehnt): Für den Umstieg sichern Sie die Datenbank mit
`pg_dump --no-owner --no-privileges`, starten PostgreSQL auf einem neuen Volume und spielen die
Sicherung als `latet` wieder ein.

Schreiben Sie die Adresse des Servers in eine Datei namens `.env` neben `compose.yaml`, die Compose
bei jedem Befehl liest, damit ein Upgrade, das später aus einem anderen Terminal läuft, sie behält
(sie enthält kein Geheimnis):

```sh
printf 'LATET_SERVER_URL=https://latet.acme.lan\nLATET_SERVER_HOST=latet.acme.lan\n' > .env
```

Dieses POSIX-Shell-Beispiel wählt Projekt `latet` mit dem internen Netzwerk
`latet_latet-back`. Die Initialisierung verwendet Dockers `--log-driver none`, da sie
das Administrationstoken ausgibt; halten Sie das Terminal privat und übertragen Sie das
Token in Ihren Geheimnisspeicher. Prüfen Sie den Erfolg, bevor Sie die Instanzen starten:

```sh
# --wait returns only once PostgreSQL and Valkey are healthy (at most 120 s): go on only if it succeeds
docker compose -p latet --profile server up -d --wait --wait-timeout 120 postgres valkey
docker run --rm --log-driver none --network latet_latet-back \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=bind,source=$PWD/secrets/master.key,target=/run/secrets/latet-master-key,readonly" \
  --mount "type=bind,source=$PWD/secrets/db.password,target=/run/secrets/latet-db-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/valkey.password,target=/run/secrets/latet-valkey-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/metrics.token,target=/run/secrets/latet-metrics-token,readonly" \
  --env LATET_SERVER_URL=https://latet.acme.lan \
  --env LATET_SERVER_DATABASE_URL=postgresql://latet@postgres:5432/latet \
  --env LATET_SERVER_DATABASE_PASSWORD_FILE=/run/secrets/latet-db-password \
  --env LATET_SERVER_DATABASE_PRIVATE_NETWORK=1 \
  --env LATET_SERVER_VALKEY_URL=redis://valkey:6379/0 \
  --env LATET_SERVER_VALKEY_PASSWORD_FILE=/run/secrets/latet-valkey-password \
  --env LATET_SERVER_VALKEY_PRIVATE_NETWORK=1 \
  --env LATET_MASTER_KEY_FILE=/run/secrets/latet-master-key \
  --env LATET_SERVER_METRICS_TOKEN_FILE=/run/secrets/latet-metrics-token \
  ghcr.io/latet-ai/latet:latest server init
```

Nach erfolgreicher Initialisierung starten Sie die Instanzen und Caddy:

```sh
docker compose -p latet --profile server up -d latet-server-1 latet-server-2 caddy
```

Im Serverprofil veröffentlicht nur Caddy einen Port: `443`. Der separate lokale Dienst
`latet` bleibt in der Compose-Datei; die ausdrückliche Dienstliste oben startet ihn nicht.
Das Frontnetz ist `172.28.0.0/24` mit automatischer Vergabe aus `172.28.0.128/25`;
Caddys feste Adresse `172.28.0.2` liegt außerhalb und ist der einzige vertrauenswürdige
Proxy. Das hintere Netzwerk ist intern.

Caddy verwendet die gelieferten TLS-Dateien, ein Anfragelimit von **32 MiB**, sofortiges
Weiterleiten von Streams und WebSocket-Upgrades. Lassen Sie Zugriffslogs deaktiviert und
behalten Sie den globalen Ausschluss `log default { exclude http.log.error }` bei:
Proxyfehler können sonst Anfrage-URIs und eigene Authentifizierungsheader protokollieren.
Das mitgelieferte Caddy hat keine Ratenbegrenzung; Grenzen pro Client gehören an einen
externen Proxy oder eine WAF.

Servercontainer verwenden begrenzte, nicht blockierende JSON-file-Logs. Instanzen laufen
unter dem eigenen unprivilegierten Benutzer des Images, UID und GID `10001`, nie als root.
Sie haben schreibgeschützte Root-Dateisysteme, keine Capabilities und ein temporäres `/tmp`; das
Beispiel setzt keine Speichergrenze für Produktionsinstanzen. Planen Sie für Bereiche,
Modelle und gleichzeitige OCR-Arbeit, statt Smoke-Test-Grenzen zu übernehmen.
Valkey hat begrenzten Speicher mit `noeviction`. Das Image enthält die Drittanbieterhinweise.

## PostgreSQL und Valkey

Verwenden Sie für PostgreSQL außerhalb des Instanzrechners geprüftes TLS in der
Verbindungs-URL, etwa `?sslmode=verify-full&sslrootcert=system`, oder geben Sie die Datei
des vertrauenswürdigen Zertifikats an. `verify-full` ohne `sslrootcert` reicht hier nicht.
URLs ohne Host und Verbindungen über Dienstnamen werden abgelehnt; `PG*`-Umgebungsvariablen
ergänzen keine fehlenden Verbindungseinstellungen. Kodieren Sie reservierte Passwortzeichen
in URLs mit Prozentkodierung oder verwenden Sie die Passwortdatei. Das Verbindungszeitlimit
umfasst nicht die DNS-Auflösung.

Verwenden Sie Valkey ab 7.2 oder Redis 7 als einen logischen Server. Replikate sind möglich,
Cluster wird nicht unterstützt. Die Skripte arbeiten über mehrere Schlüsselpräfixe hinweg.
Setzen Sie `maxmemory-policy` auf `noeviction`, planen Sie Speicher für die erwarteten
Bereiche ein und überwachen Sie die Kapazität. Der Verlust von Koordinationszustand ist
nicht mit dem Entfernen eines harmlosen Anwendungscaches gleichzusetzen.

Verlangen Sie Authentifizierung und geprüftes TLS (`rediss://`), wenn Valkey außerhalb
des Instanzrechners läuft. Die Ausnahmen `database_private_network`, `valkey_private_network`
und `valkey_unauthenticated` sind ausdrückliche Betriebsentscheidungen für Dienste, die
nur die vorgesehenen Instanzen erreichen können. Verwenden Sie sie nicht, um Zertifikats-
oder Authentifizierungsfehler in einem gewöhnlichen gemeinsamen Netzwerk zu unterdrücken.

Prüfen Sie vor der Anwendung dieses Beispiels, dass der Valkey-Benutzer `latet` noch nicht
existiert. Es erstellt einen neuen Benutzer. Existiert er bereits, brechen Sie ab und
lassen Sie seine ACL, Passwörter und Selektoren vom Betreiber prüfen. Bei einem bestehenden
Benutzer ergänzt der Befehl die Konfiguration; er setzt sie nicht zurück und ersetzt keine
bisherigen Passwörter.

Geben Sie der Anwendung einen eigenen Valkey-Benutzer mit dieser ACL. Sie verwendet den
Standardnamensraum `latet:`; ersetzen Sie den Platzhalter `password` über eine geschützte
Administrationsverbindung. Dies ist ein Valkey-Befehl, kein Shell-Befehl. Halten Sie das
echte Geheimnis aus Shell-Argumenten, Verlauf und Repositories heraus.

```text
ACL SETUSER latet on >password ~latet:* resetchannels &latet:identity:* &latet:approvals:* &latet:feed:* &latet:shared &latet:identities &latet:signins &latet:beat +@all -@dangerous -monitor -config -keys -debug
```

Behalten Sie die Abonnementmuster wortgetreu bei: Musterabonnements benötigen genau diese
Berechtigungen. Lua-Skripte und `SCAN` bleiben für Koordination und gezielte Bereinigung
verfügbar; `MONITOR`, `CONFIG`, `KEYS` und `DEBUG` sind gesperrt. Ein isolierter automatischer
Test prüft diese Regel mit ausgetauschtem Benutzer, Passwort und Namensraum; er untersucht
oder konfiguriert Ihren bereitgestellten Dienst nicht.

Führen Sie Migrationen als Schemaeigentümer aus. Die erste Migration erstellt das Schema
und benötigt `CREATE` auf der Datenbank; spätere Migrationen benötigen dieses Erstellungsrecht
nicht. Die Migrationssperre wartet höchstens 600 Sekunden. Eine Anwendungsrolle ohne
`DELETE` auf `audit` und `audit_anchor` kann das Administrationsprotokoll nicht bereinigen:
Erteilen Sie diese Rechte oder behalten Sie die Eigentümerrolle für die Instanzen bei.
Halten Sie vor einem Upgrade eine wiederherstellbare Sicherung bereit. Ein Verbindungspooler
muss die erforderlichen PostgreSQL-Startparameter `options` erhalten.

### Valkey dimensionieren

Die Bytebudgets pro Identität betragen 32 MiB für Texte in ihrer versendeten Form,
64 MiB für die Freigabewarteschlange und 16 MiB für per Referenz gespeicherte
Feed-Ereignisse. Ihre Summe, **112 MiB, ist keine Gesamtspeichergrenze**. Andere Familien
sind nach Anzahl begrenzt; daher zählt auch die Datensatzgröße. Für Unterhaltungen sind
1.000.000 Felder und freigegebene Texte in höchstens 10.000 Unterhaltungen vorgesehen.
Markierungen geprüfter und aufgezeichneter Blöcke erlauben jeweils 200.000 Einträge;
Rückzuordnungstexte und Antwortzuordnungen jeweils 50.000, AWS-Sitzungen 200 und
Dashboard-Sitzungen 10 pro Person.
Rückzuordnungstexte sind gespeicherte Modellausgaben, die wiedererkannt werden, wenn sie
in einer späteren Anfrage zurückkommen.

Dies sind Planungsbeispiele, keine Messwerte. Angenommen werden 256 belegte Bytes pro
Unterhaltungseintrag und 192 pro Blockmarkierung. Die anderen Größen bezeichnen den
kodierten Klartext: Die Schätzung addiert den 30-Byte-Verschlüsselungsrahmen,
Allokationsrundung und geschätzte 256 Bytes für Schlüssel und Index. Die drei Bytebudgets
berücksichtigen diesen Aufwand bereits.

| Familie | Beispiel mit mäßiger Belegung | Ausgeschöpfte Anzahlen bei gleichen Datensatzgrößen |
| --- | --- | --- |
| Texte / Warteschlange / Ereignisse | 8 / 8 / 1 MiB | 32 / 64 / 16 MiB |
| Unterhaltungseinträge | 20.000 zu 256 Bytes | 1.000.000 zu 256 Bytes |
| Geprüfte / aufgezeichnete Markierungen | Je 10.000 zu 192 Bytes | Je 200.000 zu 192 Bytes |
| Rückzuordnungstexte | 5.000 zu 2 KiB | 50.000 zu 2 KiB |
| Antwortzuordnungen | 5.000 zu 64 Bytes | 50.000 zu 64 Bytes |
| AWS-Sitzungen | 10 zu 2 KiB | 200 zu 2 KiB |
| Dashboard-Sitzungen | 2 zu 128 Bytes | 10 zu 128 Bytes |
| **Modellierte Zwischensumme pro Identität** | **40,76 MiB** | **581,75 MiB** |

Mit einer beispielhaften Reserve von 50 % ergeben sich 61,13 und 872,62 MiB pro Identität
oder etwa 5,97 und 85,22 GiB für 100 Identitäten. Planen Sie Valkeys Grundspeicher,
Verbindungen, Replikation/Persistenz sowie weitere Strukturen oder Fragmentierung, die
diese Reserve übersteigen, gesondert ein. Zählen Sie Identitäten, deren Daten noch
gespeichert sind, nicht nur die momentan anfragenden. Anwendungscaches und OCR-Modelle
benötigen eigenen Arbeitsspeicher.

Das ausgeschöpfte Beispiel ist **kein absoluter ungünstigster Fall**: Kleine Datensätze
bleiben eine Annahme. Die 4-MiB-Warteschlange des Schreibers begrenzt die dauerhaft
gespeicherten Rückzuordnungstexte nicht auf 4 MiB. Aufeinanderfolgende Datensätze von
knapp 4 MiB können bei 50.000 Rückzuordnungsdatensätzen allein in die Größenordnung von
195 GiB kommen, vor allen anderen Familien. Das ist eine grobe Abschätzung aus dem
Quellcode, keine Messung oder Gesamtquote. Auch ein langer Unterhaltungsschlüssel oder
Ordnerpfad kann die angenommene Eintragsgröße überschreiten. Behalten Sie `noeviction`
bei, beobachten Sie tatsächliche Größen und Belegung und planen Sie für die zugelassene
Last; Eintragszahlen allein garantieren keine kleine Speicherobergrenze pro Identität.

## HTTPS, Proxys und Kapazität

Beenden Sie TLS an der Instanz oder am Lastverteiler. Setzen Sie `forwarded_allow_ips`
auf die tatsächlich vertrauenswürdigen Proxys, niemals auf `*`. Weitergeleiteten Headern
anderer Adressen wird nicht vertraut. Synchronisieren Sie die Uhren mit höchstens
60 Sekunden Abweichung. Beschränken Sie direkte Anbieterzugriffe gesondert, wenn der
Server der vorgeschriebene Kontrollpunkt des Unternehmens ist.

Das voreingestellte Limit für Anfragekörper beträgt 32 MiB. `requests_in_flight` ist
standardmäßig 16 je Identität auf einer Instanz, mit getrennten Kontingenten für Ziel-,
Dashboard-, Bridge- und Anwendungsanfragen. Long Polling und Streams haben eigene Grenzen. Prüfen
Sie bei Kapazitätsablehnungen Last und Grenzen, statt den Schutz abzuschalten. Beim
Herunterfahren bleiben einer Instanz 30 Sekunden zum Abschluss. Lange Streams können
abbrechen; Clients müssen Verbindungsabbrüche behandeln.

OCR verwendet begrenzte Leser, deren Modelle jeweils Speicher belegen. `ocr_readers`
ist standardmäßig 2 und darf die für die Instanz verfügbaren Prozessoren nicht überschreiten.
`ocr_read_seconds` ist standardmäßig 30, `ocr_wait_seconds` 35 und darf nicht kürzer als
das Lesebudget sein. Mehr Leser benötigen neben CPU auch Speicher. Ein ungelesenes Bild
wird je nach geschütztem Pfad zurückgehalten oder seine geprüfte Anfrage abgelehnt.
Längere Zeitlimits verbessern nicht die Erkennungsgenauigkeit.

Der Bereich eines API-Schlüssels hält die Werte im Speicher, die der Schlüssel zuletzt
verwendet hat (`key_working_values`, standardmäßig 5.000, etwa 10 MiB pro Schlüssel), und
dauerhaft seine neuesten kurzen Geheimnisse, die kein Wort eines Textes findet, wenn andere
Buchstaben an sie grenzen (`key_held_secrets`, standardmäßig 1.000, etwa 2,2 KB pro Stück auf
jeder Instanz). Erhöhen Sie `key_held_secrets` für einen Schlüssel, der viele kurze Geheimnisse
sieht, etwa ein Sitzungstoken in jeder Logzeile: Jeder Text, den der Schlüssel maskiert oder
wiederherstellt, wird nach jedem gehaltenen durchsucht, sodass auch seine Aufrufe etwas
langsamer werden (das Wiederherstellen mit 1.000 gehaltenen etwa ein Zehntel
langsamer)
([der Tresor eines Schlüssels](../../applications/#der-tresor-eines-schlüssels)).

### Verbindungen, Grenzen und Vertrauen

- **Abgelehnte Zugangsdaten** zählen für die Adresse, von der sie kommen: eine IPv4-Adresse oder bei IPv6 ihr /64
  und zusammen ihr /48. Zugangsdaten, die in der letzten Stunde als gültig erkannt wurden, und ein Anmeldelink, den
  der Server ausgestellt hat, zählen nicht für diese Grenze; die Fehlversuche eines Nachbarn hinter demselben NAT halten
  also nur Zugangsdaten auf, die nicht kürzlich als gültig erkannt wurden, solange die Versuche andauern; ein Proxy
  oder eine Firewall davor, die jeden Client begrenzt, verhindert das. Solange die Threads, die Zugangsdaten
  prüfen, ausgelastet sind, erhalten Registrierungen von Rechnern und Zugangsdaten, die jeder Instanz neu sind,
  503 mit `Retry-After`, und ein Anmeldelink zum Dashboard wird an `?login=busy` geschickt.
- **Token und Schlüssel eines Rechners:** Das Token, das die Agenten eines Rechners halten, öffnet die Bridge
  des Plugins, den Proxy des Bereichs und die beiden Aufrufe des Rechners beim Start eines Agenten (ein Ziel
  für eine Adresse hinzufügen, was nichts verringert, und den Ersatznamen des Benutzernamens abfragen), sagt,
  wem es gehört, und widerruft sich selbst; ein Anmeldelink zum
  Dashboard und eine Änderung der Identität der Person verlangen zusätzlich den Rechnerschlüssel (Header
  `x-latet-computer-key`), der im Anmeldedatenspeicher dieses Rechners bleibt. Ein fehlender oder falscher
  Schlüssel wird abgelehnt (403 `computer_key_required`) und zählt für seine Adresse wie abgelehnte Zugangsdaten.
- **Eine Identität** hat höchstens `requests_in_flight` laufende Anfragen auf einer Instanz (standardmäßig 16);
  darüber 429 mit `Retry-After`. Ein Body wird bis `max_request_mb` gelesen (standardmäßig 32 MiB), ein Aufruf der API für
  Anwendungen bis `apps_request_mb` (8 MiB); ein JSON-Body
  nahe dieser Grenze hält die Sperre des Interpreters ein bis drei Sekunden, wo immer er geparst wird; halten Sie
  die Grenze also so niedrig, wie Ihre Agenten es erlauben, und betreiben Sie mehrere Instanzen.
- **PostgreSQL:** Jede Instanz hält bis zu `database_pool` Verbindungen (standardmäßig 10) für die Arbeit aller
  Identitäten; `max_connections` von PostgreSQL muss den Pool jeder Instanz und die Befehle der Administratoren
  abdecken. Ein Aufruf der API für Anwendungen, der neue Werte speichert, hält eine davon, bis er endet, höchstens
  drei Zehntel des Pools (drei von zehn); darüber hinaus speichern Aufrufe jeden neuen Wert sofort, was langsamer
  ist. Erhöhen Sie `database_pool` und mit ihm `max_connections` für eine Instanz, die für viele neue Schlüssel
  gleichzeitig maskiert.
- **Valkey:** Jede Instanz hält bis zu `valkey_connections` Verbindungen (standardmäßig 100), darüber wartet ein
  Aufruf höchstens fünf Sekunden. `maxclients` von Valkey muss die Verbindungen aller Instanzen abdecken. Der
  Verbindungsaufbau zu Valkey, sein TLS und seine erste Antwort teilen sich eine Grenze von fünf Sekunden.
- **Die Zertifikate der Anbieter:** Eine Instanz vertraut dem Zertifikatsspeicher des Betriebssystems; unter
  Linux den Standardpfaden von OpenSSL, die `SSL_CERT_FILE` und `SSL_CERT_DIR` für den ganzen Prozess ändern.
  Ändern Sie sie nur, um die Zertifizierungsstelle Ihres Unternehmens hinzuzufügen.
- **`SSLKEYLOGFILE`** darf für eine Instanz nicht gesetzt sein: Die TLS-Verbindungen, die sie lesen, darunter die
  zu Valkey, schreiben ihre Sitzungsschlüssel in diese Datei, und wer sie liest, liest diesen Verkehr. Eine
  Instanz, die damit gestartet wird, meldet das in ihrem Protokoll.

## Aktualisieren und Instanzen hinzufügen

Die Instanzen teilen sich dieselben Tabellen, und eine Instanz startet nur auf Tabellen ihrer eigenen Version.
Die Datenbank zu sichern ist Ihre Sache: Latet kopiert nichts davon. Bevor Sie ein neues Image laden und
`latet server migrate` ausführen, legen Sie mit `pg_dump` eine Kopie an, die auch dann konsistent ist, wenn
die Instanzen weiterschreiben:

```sh
pg_dump --format=custom --file=latet-before-upgrade.dump "postgresql://latet@db.acme.lan/latet"
```

Um zurückzukehren, spielen Sie diese Kopie wieder ein (`pg_restore --clean --if-exists`) und starten Sie das
vorherige Image: Eine Instanz startet nicht auf Tabellen einer neueren Version. Die Werte in der Kopie bleiben
verschlüsselt, wie in der Datenbank: Nur der Master-Schlüssel des Servers öffnet sie. Fragen Sie vor einer
Aktualisierung die neue Version, ob sie die Tabellen ändert:

```sh
latet server migrate --check
```

- **Die Tabellen bleiben gleich:** Ersetzen Sie die Instanzen hinter dem Lastverteiler nacheinander.
  Eine angehaltene Instanz beendet ihre Anfragen innerhalb von 30 Sekunden.
- **Die Tabellen ändern sich:** Halten Sie alle Instanzen an, führen Sie `latet server migrate` einmal
  mit der neuen Version aus und starten Sie die Instanzen dann mit ihr. Eine Instanz startet nicht auf
  Tabellen einer anderen Version.
- **Programme nutzen die API für Anwendungen:** Halten Sie jede ältere Instanz an, bevor die neuen
  starten, auch wenn die Tabellen gleich bleiben. Solange zwei Versionen nebeneinander laufen, findet
  eine neuere Instanz einen Wert, den eine ältere in den Tresor eines Schlüssels schreibt, unter
  Umständen erst, wenn der Bereich des Schlüssels neu aufgebaut wird
  ([einen Server aktualisieren](../../applications/#einen-server-aktualisieren)).
- **Eine Version, die den Tresor eines Schlüssels neu indiziert** (das Changelog sagt es): Die erste
  Instanz, die den Bereich eines API-Schlüssels aufbaut, bildet die Wörter seines Tresors neu, bevor sie
  den ersten Aufruf dieses Schlüssels beantwortet, etwa 40 Sekunden für 200.000 Werte und viereinhalb
  Minuten für 1.000.000. Das geschieht einmal pro Schlüssel, aber eine Instanz baut vier Bereiche
  gleichzeitig auf, von Personen wie von Schlüsseln, sodass mehrere große Schlüssel, die nach einem
  Neustart zusammen aufgebaut werden, die ersten Aufrufe aller anderen Bereiche auf dieser Instanz
  aufhalten: Senden Sie jedem großen Schlüssel einen Aufruf, einen Schlüssel nach dem anderen, bevor
  der Verkehr zurückkommt.

Im Compose-Beispiel laufen dieselben Schritte in Containern des neuen Images, wenn sich die Tabellen
ändern:

```sh
docker compose -p latet --profile server pull
docker compose -p latet --profile server run --rm latet-server-1 server migrate --check
docker compose -p latet --profile server stop latet-server-1 latet-server-2
docker compose -p latet --profile server run --rm latet-server-1 server migrate
docker compose -p latet --profile server up -d latet-server-1 latet-server-2
```

Für eine weitere Instanz starten Sie eine mit denselben Einstellungen, demselben Hauptschlüssel und
denselben PostgreSQL und Valkey und tragen ihre Adresse im Lastverteiler ein. Die Instanzen halten ihren
Zustand in PostgreSQL und Valkey, daher kann jede jede Anfrage beantworten.

## Erkennungsmodelle auf einem Server

Namenserkennung und Texterkennung lesen ihre Modelle aus dem Datenordner des Servers, nicht vom Rechner
des Administrators. Installieren Sie sie dort, mit dem Konto, unter dem die Instanzen laufen:
`latet ner install` und `latet ocr install` mit `LATET_DATA_DIR` auf diesen Ordner gesetzt. Im
Compose-Beispiel ist der Ordner das Volume `latet-server-data`, in jeder Instanz unter `/data`
eingehängt: Installieren Sie sie aus einem Container einer Instanz und starten Sie die Instanzen
danach neu.

```sh
docker compose -p latet --profile server run --rm latet-server-1 ner install --yes
docker compose -p latet --profile server run --rm latet-server-1 ocr install --yes
docker compose -p latet --profile server restart latet-server-1 latet-server-2
```

Beim Linux-Dienst laufen die Instanzen als Benutzer `latet`, und ihr Ordner ist `/var/lib/latet`,
den systemd beim ersten Start des Dienstes anlegt. Sobald er gestartet ist, installieren Sie die
Modelle als dieser Benutzer und starten dann den Dienst neu:

```sh
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ner install --yes
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ocr install --yes
systemctl restart latet-server
```

Beim Windows-Dienst ist das Verfahren zum Installieren der Modelle noch nicht dokumentiert.

## Programme mit einem API-Schlüssel

Ein Programm erreicht seine Routen mit seinem API-Schlüssel und behält seinen eigenen
Anbieterschlüssel. Schreiben Sie den API-Schlüssel in eine private Datei und geben Sie dann die
Variablen aus, die das Programm über den Server leiten:

```sh
latet env --key-file program.key --server https://latet.acme.lan
```

Die ausgegebenen Variablen enthalten den Routenschlüssel: Halten Sie sie so geheim wie den Schlüssel, nie in
einem Repository oder einem gemeinsam genutzten Profil. Nur der erste Token des Schlüssels hat diese
Adressen: Ein Token, der mit `keys add-token` erstellt wurde, hat keine, welche Berechtigungen er auch
hat, und ruft nur die API für Anwendungen auf. `latet env` fragt den Server, welcher Art der Token ist:
Für einen solchen Token gibt es keine Variable aus und sagt das.

Das Programm muss dem Zertifikat des Servers vertrauen: einem Zertifikat einer Zertifizierungsstelle, der es schon
vertraut, oder dem Zertifikat, das seinen eigenen Vertrauenseinstellungen hinzugefügt wurde.

Ein Programm, das seine eigenen Logs oder Dokumente maskiert, ruft stattdessen die
[API für Anwendungen](../../applications/) auf: keine Variablen, ein Token des Schlüssels in seinem
`Authorization`-Header, mit nur den Berechtigungen, die es braucht. Seine Aufrufe teilen den Bereich des
Schlüssels mit dessen Agenten, also auch dessen Tresor und Ersatzwerte.

Zwei Arten seiner Arbeit stehen für den ganzen Server an, nicht pro Schlüssel. Der Eintrag jeder
Wiederherstellung im Verwaltungsprotokoll wird vor ihrer Antwort geschrieben, einer nach dem anderen
an der Kette des Protokolls, von zwei Threads jeder Instanz für die Wiederherstellungen aller
Schlüssel; und das Vergessen läuft auf jeder Instanz zu zweit, für alle Schlüssel. Ein Schlüssel, der
viele Wiederherstellungen auf einmal sendet oder eine lange Liste vergisst, verzögert die
Wiederherstellungen oder das Vergessen der anderen Schlüssel, nie eine Anmeldung, einen Widerruf oder
das, was Agenten senden. Das Verwaltungsprotokoll wächst um eine Zeile pro Aufruf zur
Wiederherstellung.

## Protokolle, Metriken und Wiederherstellung

Anwendungsprotokolle gehen als `text` oder `json` an die Standardausgabe. Schützen und
rotieren Sie die Protokolldateien des Betriebs. Das Administrationsprotokoll ist getrennt
und erfasst Vorgänge einschließlich Codeausstellung und Zugangsdatenänderungen. Lesen
Sie es mit `latet server log` und prüfen Sie seine Kette mit `latet server log verify`.
Bewahren Sie den zurückgegebenen Kopf außerhalb der Datenbank auf: Das Löschen des Endes
kann die verbleibende Kette gültig lassen.

Das Kettenurteil des Statusbefehls ist zwischengespeichert und kann unbekannt sein; es
ist keine neue vollständige Prüfung. Prometheus-Metriken verwenden einen getrennten
Listener, standardmäßig `127.0.0.1:7283`, und benötigen `metrics_token_file`. Unterscheiden Sie ein unbekanntes
Urteil von einer beschädigten Kette. Die [Verwaltungsreferenz](../../reference/server-administration/)
definiert Protokollseiten und die genaue Bedeutung von `latet_ocr_unread_total{reason}`.

Jede Wiederherstellung über die API für Anwendungen ist ein Eintrag `apps.unmasked` im
Administrationsprotokoll, mit dem Token und der Zahl der wiederhergestellten Werte, und jedes
Vergessen ein Eintrag `apps.forgotten`, mit dem Token und der Zahl der aus dem Tresor des Schlüssels
entfernten Werte; Analysieren und Maskieren
werden nur in den Metriken gezählt (`latet_apps_texts_total` und `latet_apps_values_total`, nach Operation,
und die Anfragen als `latet_http_requests_total{kind="apps"}`), ohne Label für den Schlüssel
([was der Server festhält](../../reference/apps-api/#was-der-server-festhält)).

In dieser Compose-Konfiguration lesen Sie `127.0.0.1:7283` innerhalb jeder Instanz mit
ihrem Metrik-Token aus. Es gibt keinen veröffentlichten Metrik-Port, keine Caddy-Route und
kein mitgeliefertes Überwachungsnetz. Eine andere Bereitstellung muss ausdrücklich einen
geschützten, authentifizierten Zugriff einrichten; der HTTPS-Port der Anwendung liefert
diese Metriken nicht. Der Metrik-Listener beantwortet nur `GET /_latet/metrics`, jeden anderen
Pfad mit `404`: Prometheus fragt nach `/metrics`, sofern sein `metrics_path` nichts anderes sagt.

Eine Instanz pro Stunde durchläuft die Kette des Administrationsprotokolls und bewahrt das Ergebnis auf; der
Status und die Metriken zeigen dieses Ergebnis, zwei Stunden lang, und nennen es unbekannt, wenn keines
vorliegt (ein neuer Server oder ein frisch gestartetes Valkey). Latet hat kein Werkzeug, das eine gebrochene
Kette repariert: Vergleichen Sie sie mit dem Kopf, den Sie außerhalb der Datenbank aufbewahrt haben, suchen Sie
den genannten Eintrag und stellen Sie die Datenbank aus einer Sicherung wieder her, wenn das Protokoll verändert
wurde.

Ein Prometheus neben einer Instanz fragt sie so alle 30 Sekunden ab; jedes Intervall bis zu zwei Minuten
funktioniert. Die Alarme warten länger als eine Abfrage, weil die erste nach einem Start noch keine
Messwerte der Dienste hat:

```yaml
scrape_configs:
  - job_name: latet
    scrape_interval: 30s
    metrics_path: /_latet/metrics
    authorization:
      credentials_file: /etc/prometheus/latet-metrics.token
    static_configs:
      - targets: ["127.0.0.1:7283"]
```

```yaml
groups:
  - name: latet
    rules:
      - alert: LatetDatabaseDown
        expr: latet_database_up == 0
        for: 2m
      - alert: LatetValkeyDown
        expr: latet_valkey_up == 0
        for: 2m
      - alert: LatetAuditChainBroken
        expr: latet_audit_chain_known == 1 and latet_audit_chain_ok == 0
        for: 5m
      - alert: LatetLogsDropped
        expr: increase(latet_log_dropped_total[10m]) > 0
```

Sichern Sie PostgreSQL, den passenden Hauptschlüssel und die Betriebskonfiguration mit
angemessenen Zugriffskontrollen. Testen Sie die Wiederherstellung in einer isolierten
Umgebung, bevor Sie sich auf die Sicherung verlassen. Eine ältere Datenbank kann auch
alte Berechtigungen wiederherstellen. Gleichen Sie Zugangsdaten und Widerrufe ab, bevor
Sie den Dienst wieder öffnen. Die Aufbewahrungsfrist des Verlaufs und das Leeren im
Dashboard löschen weder Sicherungen noch den Tresor. Überwachen Sie diese Speicher getrennt.
