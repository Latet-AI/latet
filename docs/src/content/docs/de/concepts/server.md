---
title: Latet auf einem Server
description: Bereiche, Speicherung, gemeinsame Einstellungen und die Vertrauensgrenze eines Latet-Servers.
---

Ein Latet-Server empfängt geschützte Anfragen verbundener Agenten. Im Gateway-Modus
maskiert er sie vor der Weiterleitung an den Modellanbieter und stellt den geschützten
Rückweg wieder her; als Claude Code erkannte Anfragen nutzen das Plugin und die unten
beschriebene reine Prüfung. Originaldaten können den Latet-Server erreichen. Ein Server
verändert, wem Sie vertrauen müssen. Er macht die Erkennung nicht unfehlbar und schützt
keinen Verkehr, der Latet nicht erreicht.

## Verbundene Clients

Der Rechner muss die Serverroute verwenden und Verbindungs- und Zugangsdatenbindungen
privat halten. Die serverseitige Maskierung deckt Gateway-Clients ab. Claude Code braucht
zusätzlich das Plugin: im unterstützten verbundenen Windows-Terminal wählen die private
Hilfsprozessbindung und vertrauenswürdige Einstellungen die Server-Bridge aus.
Das persönliche Token wird über stdin übergeben; die Metadatenbindung ist kein Tokenspeicher.
Eine fehlende oder unsichere Bindung stoppt diesen Weg.

Bei als Claude erkannten Anfragen maskiert das Plugin Tool-Beschreibungen, und die
abschließende Prüfung des Servers liest auch Beschreibungen, Parameterschemas und Beispiele
der Tools: Ein darin im Klartext gebliebener Wert stoppt die Anfrage. Anfragemarker belegen nicht,
dass das Plugin lief. Der [Schutzumfang](../protection-scope/) erklärt diese enge Ausnahme
und die getrennten Ausgabeschema-Prüfungen. [Einen Rechner verbinden](../../getting-started/server/)
beschreibt Anwendung, Diagnosen, erneutes Senden der Identität und Trennung.

Ist Latet in einem Ordner ausgeschaltet, wird eine Anfrage, die eine Instanz dafür erhält, bevor das Plugin es
dem Server mitgeteilt hat, so maskiert, als wäre Latet eingeschaltet: Im Zweifel schützt es.

### Token und Schlüssel eines Rechners

Wenn sich ein Rechner verbindet, gibt ihm der Server zwei Geheimnisse, jedes nur einmal angezeigt. Das
**Token** geht an die Agenten: Claude Code hält es in seinen Einstellungen und seiner Umgebung
(`LATET_BRIDGE_TOKEN`), wo jeder Befehl, den es ausführt, es lesen kann. Von überall, wo der Server
erreichbar ist, öffnet das Token die Bridge des Plugins und den Proxy des Bereichs (wer es besitzt, kann
im Bereich Texte maskieren und die Ersatzwerte, die er hat, wiederherstellen lassen), sagt, wem es gehört,
und widerruft sich selbst. Der **Rechnerschlüssel** bleibt im Anmeldedatenspeicher des Rechners oder in
der privaten Tokendatei, und kein Agent erhält ihn: Der Server verlangt ihn zusätzlich, bevor er einen
Anmeldelink zum Dashboard ausgibt oder der Identität der Person etwas hinzufügt. So öffnet das Token
allein nie das Dashboard, in dem jedes Original angezeigt werden kann, und verringert nie den Schutz der
Person: Die einzige Änderung, die es an ihren Einstellungen vornehmen kann, ist ein Ziel für eine Adresse
hinzuzufügen, was nichts verringert. Der Schlüssel hindert ein nach außen gelangtes Token (aus der
Umgebung, den Einstellungen, einem Log oder mit einem einzigen `curl`) daran, das Dashboard zu öffnen;
einen Befehl, der geschrieben wurde, um beide aus dem Anmeldedatenspeicher zu lesen, hält er nicht auf.
Auf einem verbundenen Rechner verweigern `latet dashboard` und `latet identity send` die Ausführung in
einem Agenten, und `latet disconnect` vergisst beides.

### Was auf dem Rechner bleibt

- **Die `latet.yaml` eines Repositorys gilt nicht.** Der Server liest keine Datei des Rechners: Was
  ein Team in seiner Projektdatei teilt (ein strengeres Profil, seine Begriffe und Erkenner), erreicht
  die Anfragen einer verbundenen Person nicht. In einem solchen Projekt sagen `latet doctor` und
  `latet claude` das. Legen Sie es in Ihrem Dashboard fest: Importieren Sie die `latet.yaml` des
  Projekts unter **Regeln** mit **Regeln importieren**.
- **Die Prüfung der CLAUDE.md-Dateien**, die Claude Code seinem Prüfer des Auto-Modus gibt, läuft
  auf dem Rechner, in `latet doctor`.
- **Claude Code über einen Server braucht in dieser Version ein Windows-Terminal.** Unter Linux und
  macOS hält sein Plugin jeden Prompt zurück, und Latet sagt das vorab, wo es es anwenden würde. Der
  Print-Modus (`claude -p`) wird auf jedem verbundenen Rechner zurückgehalten.

### Zertifikate und eigene Verbindungen der Agenten

Jeder Agent vertraut dem Zertifikat des Servers über eine eigene Variable, die Latet setzt:
`NODE_EXTRA_CA_CERTS` für Claude Code und OpenCode, `CODEX_CA_CERTIFICATE` für Codex. Sie ergänzt, was der Agent
schon vertraut, und die Kopie, die Latet schreibt, behält die Zertifikate, die die Person dort angegeben hatte,
etwa die Zertifizierungsstelle ihres Unternehmens. Ein selbstsigniertes Zertifikat erreicht die Agenten nur in
der Form, die `latet server certificate --make` ihm gibt, beschränkt auf den eigenen Namen oder die Adresse des
Servers; ein Zertifikat der Zertifizierungsstelle des Unternehmens funktioniert so, wie es ist.

Beim Start rufen die Agenten auch ihre eigenen Anbieter auf, außerhalb von Latet: Claude Code `api.anthropic.com`
mit seinem API-Schlüssel, sofern sein nicht notwendiger Datenverkehr in seinen Einstellungen nicht abgeschaltet ist;
Codex `chatgpt.com` und `api.github.com` für seine Plugins; OpenCode die npm-Registry und GitHub. Keiner dieser
Aufrufe enthält die Adresse des Bereichs, seinen Routenschlüssel oder das Token. Wo der Server der einzige Weg zu
den Modellen sein muss, sperren Sie die Hosts der Modellanbieter, darunter `api.anthropic.com` und `chatgpt.com`,
und schalten Sie den nicht notwendigen Datenverkehr von Claude Code ab; die Plugin-Liste von Codex funktioniert dann
nicht. Zum Beispiel:

```text
allow  latet.acme.lan:443                        # the Latet server
allow  api.github.com:443, github.com:443        # Codex's plugins and OpenCode's start-up, if wanted
allow  registry.npmjs.org:443                    # OpenCode's start-up, if wanted
deny   api.anthropic.com:443, chatgpt.com:443    # the models only through the server
deny   every other host of the AI providers
```

## Bereiche und Instanzen

Jede Person und jeder API-Schlüssel hat einen eigenen Bereich mit Tresor, Verlauf und
Einstellungen. Die Rechner einer Person teilen diesen Bereich. Ein API-Schlüssel steht
für ein Programm, hat kein persönliches Dashboard und erhält seine Einstellungen vom Administrator.

Eine Instanz ist ein laufender Latet-Serverprozess. Mehrere Instanzen können dieselbe
öffentliche HTTPS-Adresse hinter einem Lastverteiler bedienen und dieselbe PostgreSQL-Datenbank,
Valkey und denselben Hauptschlüssel verwenden. PostgreSQL speichert dauerhaften Zustand;
Valkey koordiniert kurzlebigen Zustand. Anfragen benötigen keine Sitzungsbindung.
Weitere Instanzen erzeugen keine getrennten Kopien des Bereichs einer Person. Die
Dienstanforderungen stehen unter [Serverbetrieb](../server-operations/).

## Was der Server speichert

Werte und Ersatzwerte im Tresor, aufgezeichnete Texte und Bilder sowie die Einstellungen
eines Bereichs werden mit dessen Identitätsschlüssel verschlüsselt. Dieser Schlüssel
wird mit dem Hauptschlüssel des Servers verschlüsselt; gemeinsame Einstellungen ebenfalls
mit dem Hauptschlüssel. Bewahren Sie den Hauptschlüssel getrennt von Datenbanksicherungen
auf. Ohne den passenden Schlüssel lassen sich verschlüsselte Daten nicht wiederherstellen.

Einige Metadaten bleiben in PostgreSQL lesbar: Identitätsnamen und E-Mail-Adressen,
externe Kennungen, Tokenbezeichnungen und Zeitpunkte, Regelnamen im Tresor, Zähler und
Zeitpunkte sowie Akteure, Aktionen, Ziele, Kennungen, Anzahlen und Clientadressen des
Administrationsprotokolls. Projekt- und Gesprächskennungen im Verlauf verwenden Hashes
mit Schlüssel. Valkey verwendet mit Schlüssel abgeleitete Namen, verschlüsselte sensible
Nutzdaten und signierte Koordinationsnachrichten.

Die Verschlüsselung gespeicherter Daten verbirgt Originale nicht vor dem laufenden
Server oder seinem Betreiber. Berechtigte Dashboardansichten erhalten Originale im
Browser. Ein Administrator kann den Registrierungscode einer Person ausstellen und
damit auf ihr Dashboard zugreifen; dieser Vorgang wird protokolliert. Er kann auch einen
Prüfdienst konfigurieren, der Originaltext erhält. Vertrauen Sie entsprechend den
Serveradministratoren, dem Betreiber und diesem Prüfdienst. Verschlüsselung und Signaturen
verhindern nicht, dass jemand mit Datenbankschreibzugriff einen älteren gültigen Zustand wiederherstellt.

Das Leeren des aufgezeichneten Verlaufs entfernt die Aufzeichnungen dieses Bereichs,
nicht seinen Tresor. Sicherungen, Agententranskripte, Aufzeichnungen des Anbieters und
bereits an einen Browser gelieferte Kopien werden dadurch nicht gelöscht.

## Gemeinsame Einstellungen und eigene Regeln

Die gemeinsamen Einstellungen sind, was der Server zum Betrieb braucht, für jeden Bereich gleich:
die Ziele, an die Anfragen gehen dürfen, der gemeinsame lokale Prüfer, die Texterkennung, der
Verlauf und seine Aufbewahrung. Administratoren legen sie fest. Schutzregeln (Begriffe, Erkenner,
das Profil, Richtlinien und der Rest) gehören jeder Identität selbst: Personen legen ihre im
Dashboard fest, und ein Administrator legt die eines API-Schlüssels mit `latet server config --key`
und `latet server rules --key` fest.

Ein Team teilt seine Regeln von Hand: Eine Person exportiert sie (**Regeln exportieren** auf der
Seite **Regeln** des Dashboards oder `latet rules export` auf einem Rechner, der mit keinem Server
verbunden ist), und jede Person importiert die Datei in ihr eigenes Dashboard.
[Export und Import](../export-import/) beschreibt die Datei. Regeln, die für alle festgelegt sind
und die niemand lockern kann, sind für die Enterprise Edition geplant.

Eine gemeinsame Änderung, die den Schutz verringert, etwa ein verschobenes Ziel oder ein
ausgeschalteter Prüfer, wartet auf die Freigabe der Person. Bis dahin gelten ihre zuvor
genehmigten, strengeren Einstellungen. Das Speichern eigener Änderungen genehmigt die wartende
gemeinsame Änderung nicht, auch wenn derselbe Eintrag enthalten ist. Ein persönlicher
Speichervorgang kann kein für alle festgelegtes Ziel unbemerkt entfernen.

Bei einem API-Schlüssel gibt es niemanden, der eine solche Änderung freigibt: Sie erreicht den
Bereich des Schlüssels sofort, auch eine Änderung, die den Schutz verringert. Prüfen Sie daher, was
eine Änderung an den gemeinsamen Einstellungen für Ihre Programme bedeutet, bevor Sie sie vornehmen.

Unterscheiden Sie diese Speicherergebnisse:

| Ergebnis | Bedeutung |
| --- | --- |
| `409 settings_stale` | Die Einstellungen wurden seit dem Laden der Seite geändert; von diesem Speichervorgang wurde nichts übernommen. Laden Sie vor weiteren Änderungen neu. |
| `409 settings_change_waits` | Die wartende Änderung lässt sich derzeit nicht mit den genehmigten Einstellungen verbinden; von diesem Speichervorgang wurde nichts übernommen. |
| `409 settings_saved_change_pending`, `saved: true` | Ihre Änderungen wurden gespeichert, aber der wartende Vorschlag konnte nicht aktualisiert werden. Laden Sie neu und prüfen Sie die wartende Änderung; wiederholen Sie den Speichervorgang nicht ungeprüft. |
| `409 settings_terms_too_long` | Ein Begriff überschreitet die zulässige Länge; von diesem Speichervorgang wurde nichts übernommen. |

Auch andere Grenzen und Speicherfehler können einen Speichervorgang ablehnen. Ein
HTTP-Fehler allein belegt nicht, dass nichts geschrieben wurde. Bewahren Sie den
zurückgegebenen Grund und einen möglichen Hinweis auf teilweise gespeicherte Änderungen auf.

### Eigene Ziele einer Person

Ein Ziel, das eine Person in ihren eigenen Einstellungen festlegt, führt nie in das Netz des Servers. Eine private,
Unique-Local-, Carrier-Grade-NAT-, Loopback-, Link-Local-, Multicast- oder reservierte Adresse wird abgelehnt, als
Adresse geschrieben oder als Name, der dorthin auflöst; ebenso die IPv6-Formen, die eine IPv4-Adresse tragen oder zu
einem Gateway führen (6to4, Teredo, NAT64-Präfixe). In einem Netz mit NAT64 erfährt der Server dessen Präfixe, wie
RFC 7050 es beschreibt, und bis dahin erreicht er keine IPv6-Adresse, die ein NAT64 übersetzen könnte: Ein Name, der
auch eine IPv4-Adresse hat, geht dorthin. Ein Name wird beim Verbinden erneut aufgelöst, und die Anfrage geht an die
geprüfte Adresse. Für diese Ziele wird kein Proxy verwendet. Nur ein Administrator legt in den gemeinsamen
Einstellungen ein Ziel innerhalb des Netzes fest.

Ein reiner IPv6-Server hinter DNS64 mit dem bekannten Präfix erreicht daher kein eigenes Ziel, das nur über
IPv4 erreichbar ist.

## Die Grenzen eines Bereichs

- **Regeln:** Die eigenen Einstellungen einer Person enthalten höchstens 500 Erkenner, 500 Feldregeln und 500
  Muster für Werte, die nie maskiert werden, gezählt über die Einstellungen und alle ihre Projekte. Ein
  Speichern darüber hinaus wird abgelehnt.
- **Projekte:** höchstens 200 pro Person.
- **Werte:** Ein Wert mit mehr als 16.384 Zeichen wird geschwärzt statt durch einen Ersatzwert ersetzt und
  weder aufbewahrt noch wiederhergestellt; ein Begriff hat höchstens 8.192 Zeichen.
- **Speicher:** Eine Instanz gibt ihren Bereichen feste 2 GiB, und ein Bereich nutzt höchstens die Hälfte
  davon. Ein Bereich an seinem Anteil lernt keinen neuen Wert: Eine Anfrage, die einen bringt, wird mit einem
  Hinweis darauf angehalten, bis im Dashboard Werte vergessen werden, Regeln oder Projekte weniger werden oder
  der Server mehr hat. Kein Bereich einer anderen Person wird geschlossen, um Platz zu schaffen.
- **Statistiken** zählen die letzten 100.000 Austausche, und eine Person behält ihre zehn neuesten
  Dashboard-Sitzungen.

## Die Schutzgrenze

Latet kann nur unterstützte Anfragen prüfen, die durch Latet geleitet werden. Verlangt
die Firmenrichtlinie, dass sämtlicher Modellverkehr über den Server läuft, muss das
Netzwerk direkte Verbindungen der Rechner zu Modellanbietern verhindern. Richten Sie
diese Einschränkung an der Netzwerkgrenze ein und pflegen Sie sie dort; Latet installiert
und erzwingt sie nicht. Beliebiger Netzwerkverkehr von Werkzeugen ist von dieser Zusage nicht abgedeckt.

[Was geschützt ist und was nicht](../protection-scope/) beschreibt Erkennungs-, Bild-
und Protokollgrenzen. Nicht unterstützte Inhalte können auf einem geschützten Serverpfad
abgelehnt oder zurückgehalten werden. Eine historische Markierung oder ein verfügbarer
Anbieterendpunkt belegt keine Unterstützung. Ein Server kann das Repository eines
verbundenen Rechners nicht lesen, nur weil ein Projekt im Dashboard erscheint. Er kennt
nur die Projektinformationen, die ihm übermittelt wurden.

Für Verwaltungsbefehle beginnen Sie mit [Einen Latet-Server verwalten](../../getting-started/server/).
