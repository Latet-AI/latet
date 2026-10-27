---
title: Sicherheit und Bedrohungsmodell
description: Wogegen Latet schützt, wogegen nicht und wie es sich selbst schützt.
---

:::note[Eine Schutzschicht unter mehreren]
Latet maskiert, was seine Detektoren erkennen, und manche Agenten und Oberflächen sind konzeptionell
abgedeckt statt live getestet (die Seite jedes Agenten sagt, welche). Machen Sie es nicht zu Ihrem
einzigen Schutz für Daten, die Ihren Rechner nie verlassen dürfen.
:::

## Was Latet schützt

Erkennbare geheime Werte und personenbezogene Daten in Ihrem Arbeitsbereich und Ihren
Unterhaltungen: API-Schlüssel, Tokens, Passwörter, private Schlüssel, Zugangsdaten in
Verbindungszeichenfolgen, E-Mail-Adressen, Telefonnummern, IBANs, Kartennummern,
Identifikations-, Steuer- und Umsatzsteuer-Identifikationsnummern, IP-Adressen, die Begriffe, die
Sie hinzufügen, und die Identität dieses Rechners. Die [Seite zur Erkennung](../concepts/detection/)
sagt genau, was erkannt wird.

## Vertrauensgrenzen

Bei einer verbundenen Bereitstellung gilt auch die [Vertrauensgrenze des Servers](../concepts/server/):
Originale können den Server und authentifizierte Dashboard-Ansichten erreichen.
Administratoren verwalten die Serverkonfiguration und die Einstellungen, die der Server teilt,
etwa seine Ziele und seinen Prüfer; Zertifikatsbindungen, Zugangsdaten und Agentenbindungen
bleiben auf den Rechnern. Erzwingen Sie den beschränkten
Providerzugriff getrennt, wenn der Server die Kontrollstelle des Unternehmens sein muss.
Ein Server nimmt die Marker einer Anfrage nicht als Beleg dafür, dass das Plugin von Claude Code
lief: Er prüft auch die Beschreibungen, Schemas und Beispiele der Tools.

- **Ihr Rechner** gilt als vertrauenswürdig: der Agent, seine Tools, das Plugin und der Daemon von
  Latet und Ihre Dateien.
- **Dem Anbieter des Modells** werden diese Werte nicht anvertraut. Latet nimmt nichts darüber an,
  wie Anfragen protokolliert, aufbewahrt oder verwendet werden, sobald sie ankommen, und hält die
  Originale daher von ihm fern.
- **Das Netzwerk** dazwischen ist wie üblich durch TLS geschützt.

## Im Geltungsbereich

- Versehentliche Weitergabe erkennbarer Werte an den Anbieter über Prompts, Dateien,
  Tool-Ausgaben und den Verlauf der Unterhaltung.
- Anfragen, die im Client-Modus mit einem sensiblen Wert im Klartext hinausgehen würden, zum
  Beispiel über einen Weg, den das Plugin nicht abdeckt: Die abschließende Prüfung des Plugins und
  der Daemon blockieren sie.
- Webseiten, die versuchen, den lokalen Daemon zu nutzen (DNS-Rebinding, Cross-Site-Anfragen).

## Außerhalb des Geltungsbereichs

- **Werte, die die Detektoren übersehen.** Siehe
  [Was geschützt ist und was nicht](../concepts/protection-scope/).
- **Ein kompromittierter Rechner.** Malware, andere Benutzer Ihres Kontos oder ein bösartiges
  Plugin oder ein bösartiger Agent können Ihre Dateien direkt lesen. Latet ist keine Sandbox.
- **Ein Agent, der entschlossen ist, den Schutz von Latet zu senken, oder dazu verleitet wird.**
  Eine Änderung der Einstellungen, die den Schutz senkt, wartet auf Ihre Freigabe, und Befehle, die
  Latet ausschalten, verweigern in einem Agenten die Ausführung: eine Leitplanke gegen Fehler, kein
  Schloss. Ein Agent läuft als Sie, daher kann einer, der es darauf anlegt, Latet zu umgehen, oder
  einer, der durch eine Prompt-Injection verleitet wird, lesen, was Latet liest, und seine eigene
  Änderung über die lokale Schnittstelle des Daemons freigeben. Eine echte Grenze erfordert, dass
  Latet als anderer Benutzer oder auf einem anderen Rechner läuft
  ([Latet auf einem Server](../concepts/server/)). Siehe
  [Agenten und Ihre Einstellungen](../concepts/agents-and-settings/).
- **Exfiltration über Tools.** In den Argumenten von Tool-Aufrufen werden die Originale wieder
  eingesetzt, damit die Arbeit des Agenten auf der Festplatte stimmt. Eine per Prompt-Injection
  eingeschleuste Anweisung könnte den Agenten dazu bringen, einen Ersatzwert an einen Befehl zu
  übergeben, der Daten über das Netzwerk sendet, etwa `curl`; das wieder eingesetzte Original würde
  dann über diesen Befehl hinausgehen. Latet behält die Ersatzwerte in Web-Tools und
  claude.ai-Connectors bei, aber ein Shell-Befehl oder ein lokaler MCP-Server erhält den echten
  Wert. Lassen Sie die Berechtigungsabfragen Ihres Agenten für Netzwerkbefehle eingeschaltet.
- **Rückschlüsse aus dem Kontext.** Ersatzwerte behalten das Format des Originals, und der umgebende
  Text kann verraten, wofür ein Wert verwendet wird.
- **Oberflächen, die Latet nicht sieht**: Cloud-Sitzungen, Chat und Cowork in Claude Desktop,
  Cursor und die Ergebnisse von claude.ai-Connectors, die über die Server von Anthropic zurückkommen.

## Wie Latet sich selbst schützt

**Der Daemon und das Dashboard**

- Sie lauschen nur auf `127.0.0.1`, daher kann nichts in Ihrem Netzwerk sie erreichen.
- Anfragen, deren `Host`-Header keine lokale Adresse ist, werden abgewiesen, was DNS-Rebinding
  vereitelt.
- Anfragen, die von einer Webseite kommen (mit einem `Origin`-Header oder einem Cross-Site-Header
  `Sec-Fetch-Site`), werden auf den Proxy-Routen abgelehnt.
- Das Dashboard verlangt einen Einmal-Link zur Anmeldung, den `latet dashboard` erzeugt, danach ein
  Sitzungscookie mit `HttpOnly` und `SameSite=Strict`, und jede Änderung verlangt einen Header, den
  andere Websites nicht senden können. Siehe die
  [Anleitung zum Dashboard](../dashboard/#der-datenschutz-des-dashboards-selbst).
- Das Plugin für Claude Code spricht mit dem Daemon über ein zufälliges Token, das mit `latet_brg_`
  beginnt und in `bridge.token` im Datenordner liegt (ein Token aus einer früheren Version hat kein
  Präfix und funktioniert wie bisher). Um es durch eines mit Präfix zu ersetzen, führen Sie
  `latet stop` aus, löschen Sie `bridge.token` im [Datenordner](../getting-started/uninstall/), führen
  Sie `latet start` aus (es legt ein neues an) und `latet agents apply`, das das neue Token in die
  Einstellungen Ihrer Agenten schreibt. Ohne das Token weigert sich der Daemon, irgendetwas zu
  maskieren, wieder einzusetzen oder zu prüfen.
- **Ihre Agenten erreichen den Daemon über einfaches http auf `127.0.0.1`, und das ist Absicht.**
  Dieser Verkehr erreicht nie eine Netzwerkkarte: Kein anderer Rechner, nicht einmal in Ihrem WLAN,
  kann ihn sehen. Ein Programm auf Ihrem Rechner, das ihn lesen könnte (mit Administratorrechten
  oder als Sie), könnte den Tresor und Ihre Dateien direkt lesen, also würde eine Verschlüsselung es
  nicht aufhalten. Sie würde eine eigene Zertifizierungsstelle erfordern, die im Betriebssystem
  installiert ist und deren Schlüssel es jemandem, der ihn stiehlt, erlauben würde, sich auf Ihrem
  Rechner als jede beliebige Website auszugeben. Was Ihren Rechner verlässt, ist verschlüsselt: die
  Anfragen an Ihren Anbieter, so wie der Agent sie sendet, und an den lokalen Prüfer auf einem
  anderen Rechner (unten).

**Das Tutorial**

- `latet tutorial` betreibt seine Sandbox innerhalb des Befehls, auf `127.0.0.1` und einem freien
  Port, geschützt wie das Dashboard: ein Einmal-Link, dann ein eigenes Cookie, das nie die Anmeldung
  an Ihrem Dashboard ersetzt.
- Die Sandbox hat eigene Daten, Einstellungen und einen eigenen Tresor, in einem temporären Ordner,
  und keinen Ihrer Schlüssel oder Tokens. Ihre einzige Route führt zu einem simulierten Modell im
  selben Prozess, sodass keine Anfrage einen Anbieter erreicht; was nach außen reichen würde, etwa
  ein Ziel oder der Server eines lokalen Prüfers, wird dort abgelehnt. Siehe
  [Tutorial](../getting-started/tutorial/#eine-sandbox).

**Der lokale Prüfer auf einem anderen Rechner**

- Er erhält die Originaltexte, daher wird er über `https://` erreicht. Außerhalb Ihres lokalen
  Netzwerks wird nichts anderes akzeptiert.
- Einem eigenen Server mit einem selbstsignierten Zertifikat wird anhand seines Fingerabdrucks
  vertraut, den Sie einmal mit dem des Servers vergleichen (`latet llm trust` oder
  *Stimmt überein: vertrauen* im Dashboard). Ab dann wird nur dieses Zertifikat akzeptiert, geprüft,
  bevor irgendein Text gesendet wird, und dem Betriebssystem wird keine Zertifizierungsstelle
  hinzugefügt.
- In Ihrem lokalen Netzwerk wird `http://` weiterhin akzeptiert, für früher eingerichtete Server:
  Das Dashboard, `latet doctor` und das Log des Daemons warnen, dass die Texte Ihr Netzwerk
  unverschlüsselt durchqueren.

**Die Datei eines Projekts**

- Eine `latet.yaml` in einem Repository kann nur Schutz hinzufügen: ein strengeres Profil, mehr
  Begriffe und Erkenner. Was den Schutz senken würde (Werte, die nie maskiert werden sollen,
  ausgeschaltete Regeln, Latet aus), wird ignoriert, sodass ein Repository, das Sie klonen, Ihre
  Einstellungen nicht schwächen kann. Siehe [Projekte](../concepts/projects/#die-projektdatei).
- Ihre Erkenner sind reguläre Ausdrücke, die RE2 in linearer Zeit zur Länge des Textes ausführt, und
  die Größe der Datei ist begrenzt: Eine feindselige Datei kann das Maskieren nicht hängen lassen.

**Ihre Werte**

- Der Tresor verschlüsselt jedes Original auf der Festplatte mit AES-GCM. Der Schlüssel wird aus
  einem Root-Schlüssel pro Installation abgeleitet, der im Anmeldedatenspeicher Ihres
  Betriebssystems liegt (Windows-Anmeldeinformationsverwaltung, macOS-Schlüsselbund, der
  Schlüsselbund des Desktops unter Linux), oder in einer privaten Datei, `root.key`, wenn es keinen
  gibt.
- Ersatzwerte werden mit HMAC aus diesem Schlüssel der Installation abgeleitet: Ohne den Schlüssel
  kann niemand von einem Ersatzwert auf das Original zurückschließen.
- Ein Ersatzwert wird nur in der Unterhaltung wieder eingesetzt, der er gezeigt wurde, sodass eine
  andere Sitzung den Tresor nicht als Orakel benutzen kann.
- Der Verlauf des Dashboards bewahrt Anfragen so auf, wie sie Ihren Rechner verlassen haben, also
  bereits maskiert (blockierte so, wie sie ihn verlassen hätten), verschlüsselt mit demselben
  Schlüssel, standardmäßig 14 Tage lang. Setzen Sie `history: metadata` (nur Anzahlen und Zeiten)
  oder `history: off` in der [Konfiguration](../reference/configuration/), um weniger aufzubewahren.

**Backups**

`latet backup` schreibt [Ihre Installation](../concepts/export-import/#ihre-installation-für-einen-anderen-rechner)
in eine Datei:
- **Der Schlüssel stammt aus Ihrer Passphrase.** Er wird mit Argon2id (128 MiB Arbeitsspeicher,
  3 Durchläufe) und einem zufälligen Salt abgeleitet.
- **Der Inhalt wird in Chunks versiegelt.** Er ist ein tar-Stream, in Chunks von 1 MiB geschnitten,
  jeder mit AES-256-GCM versiegelt. Die Nonce eines Chunks ist seine Nummer und ob er der letzte
  ist, und der Header der Datei (ihr Format sowie die Parameter und das Salt des Schlüssels) wird mit
  jedem Chunk authentifiziert.
- **Was abgelehnt wird, bevor irgendetwas aus der Datei verwendet wird:**
  - eine andere Passphrase;
  - ein Chunk, der geändert, verschoben, entfernt oder hinzugefügt wurde;
  - eine abgeschnittene Datei oder ein geänderter Header;
  - Schlüsselparameter jenseits dessen, was Latet akzeptiert (eine Datei kann es nicht dazu bringen,
    den gesamten Arbeitsspeicher zu belegen);
  - alles darin außer den erwarteten Dateien.
- **Nichts liegt im Klartext auf der Festplatte.** Die Datenbanken werden so kopiert, wie sie sind
  (ihre Werte versiegelt), der Root-Schlüssel existiert nur innerhalb des verschlüsselten Streams,
  und die Datei wird vollständig geschrieben, nur für Sie lesbar.
- **Mit `--vault` enthält die Datei Ihre echten Werte und den Schlüssel dazu:** Bewahren Sie sie so
  auf, wie Sie die Werte aufbewahren würden.

`latet upgrade` kopiert, bevor es eine Version installiert, den Tresor, den Verlauf und die
Einstellungen nach `backups/before-<Version>-<Zeit>` im Datenordner von Latet
([Aktualisierung](../getting-started/installation/#aktualisierung)):
- **Versiegelt wie der Tresor.** Die Datenbanken werden so kopiert, wie sie sind, ihre Werte mit dem
  Schlüssel dieses Rechners versiegelt, der nicht mitkopiert wird; der Ordner ist nur für Sie
  lesbar.
- **Sie folgen dem, was Sie entfernen**, solange Latet 0.9.10 oder eine spätere Version läuft.
  *Vergessen* entfernt einen Wert aus jeder Kopie, das
  Löschen des Verlaufs löscht auch ihren, und ihr Verlauf wird nicht länger aufbewahrt als Ihrer.
  Eine Kopie, die sich nicht bereinigen lässt, wird gelöscht oder beiseitegelegt und nie wieder
  angeboten.
- **Höchstens drei, und die Rückkehr entscheiden Sie.** Das Zurückspielen einer Kopie wird nur in
  einem eigenen Terminal angeboten, und ein Agent kann `latet upgrade --to` auf eine ältere
  Version nicht ausführen.

**Der lokale Prüfer** (optional, [aus, bis Sie ihn einschalten](../concepts/llm-review/))

- Er läuft auf Ihrem eigenen Server: auf diesem Rechner oder auf einem anderen Rechner von Ihnen,
  sobald Sie das bestätigen (`remote: true`), außerhalb Ihres lokalen Netzwerks über https. Er liest
  den Originaltext, also geben Sie ihm nur einen Server, den Sie kontrollieren.
- Er kann zu dem, was maskiert wird, etwas hinzufügen, nie etwas wegnehmen; was er findet, wird am
  Text überprüft (ein Wert, den er erfindet, oder einer innerhalb eines Platzhalters wird verworfen).
- Eine Tool-Ausgabe mit Anweisungen an ihn kann ihn schlimmstenfalls weniger finden lassen; die
  Detektoren greifen weiterhin, und einer leeren Antwort auf einen Text, der sich an den Prüfer
  richtet, wird nicht vertraut (die Anfrage wird gestoppt).
- Der API-Schlüssel seines Servers liegt im Anmeldedatenspeicher des Betriebssystems (oder kommt aus
  einer Variable oder einer Datei), wird nur an diesen Server gesendet und nie in Logs, ins
  Dashboard oder in config.yaml geschrieben. Das Dashboard kann ihn speichern oder entfernen, nie
  anzeigen.
- Was er gefunden hat, wird versiegelt in der Datenbank des Tresors gespeichert, wie die Originale
  des Tresors.
- Bilder werden ihm nur gezeigt, wenn Sie das einschalten (`llm_review.images`): Wenn ein Agent
  darum bittet, sie einem Server auf einem anderen Rechner zu zeigen, wartet das auf Ihre Freigabe.

**Bilder** (mit der optionalen [Texterkennung](../concepts/images/))

- Bilder werden auf Ihrem Rechner gelesen; die Modelle werden einmal heruntergeladen, in einer
  festgelegten Revision, jede Datei wird gegen einen festgelegten SHA-256 geprüft, und danach laufen
  sie offline.
- Die Bilder, so wie sie gesendet wurden, werden nur mit `history: full` im Verlauf des Dashboards
  aufbewahrt, versiegelt (höchstens 10 MB pro Bild), und verschwinden mit ihrem Austausch.
- Was Sie im Live-Prüfer über ein Bild entscheiden, und ein Bild, das der lokale Prüfer
  zurückgehalten hat, werden versiegelt in der Datenbank des Tresors gespeichert, über einen
  schlüsselabhängigen Hash des Bildes.
- Ein Bild, das nicht gelesen werden kann, wird zurückgehalten, nie so gesendet, wie es war.

**Datenverkehr**

- Header mit Zugangsdaten (`Authorization`, API-Schlüssel, Cookies und Ähnliches) werden unverändert
  weitergeleitet, aber nie in Logs oder ins Dashboard geschrieben: Nur ihre Länge wird aufgezeichnet.
  Die eigenen Header von Latet (`x-latet-*`, von denen einer einen Ihrer Ordner nennt) verlassen
  Ihren Rechner nie.
- Eine Ausnahme, wenn Sie Amazon Bedrock mit Ihrer AWS-Anmeldung nutzen: Claude Code sendet keine
  Zugangsdaten, und Latet signiert jede Anfrage für Amazon (SigV4) mit Ihrem AWS-Profil. Es liest
  das Profil so, wie die AWS CLI es tut, hält die Zugangsdaten nur im Arbeitsspeicher, schreibt sie
  nirgends hin und signiert nur Anfragen an Amazon Bedrock. Zugangsdaten, die nur in Ihrer Shell zu
  finden sind (aws-vault, exportierte Schlüssel), übergibt `latet claude` nur für seine eigene
  Sitzung: Latet vergisst sie, wenn diese Sitzung endet, auch wenn `latet claude` gewaltsam beendet
  wird, und wenn sie selbst ablaufen. Eine Route, die signiert, enthält einen Schlüssel, der aus dem
  eigenen Token von Latet erzeugt wird, sodass ein anderer Benutzer Ihres Rechners oder ein
  Programm, das Ihre Dateien nicht lesen kann, keine Anfragen in Ihrem Namen signieren lassen kann;
  ein Programm, das als Sie läuft, kann dieses Token lesen, so wie es Ihre AWS-Dateien lesen kann.
  Der Schlüssel gelangt nie in ein Log. Siehe
  [Unternehmens-Endpunkte](../agents/company-endpoints/#amazon-bedrock).
- Ein Repository kann nicht entscheiden, wohin Ihre Anfragen gehen: Latet liest die Route eines
  Agenten aus Ihren eigenen Einstellungen, nie aus denen eines Projekts, und das Plugin für Claude
  Code stoppt die Prompts, wenn die Einstellungen eines Projekts festlegen, wohin sie gehen, oder
  einen Proxy für sie, selbst über Latet. Eine Adresse, die so geschrieben ist, dass zwei Programme
  sie unterschiedlich lesen könnten (mit einem Backslash, einem Leerzeichen, einem Benutzernamen
  darin), wird abgelehnt, nie erraten. Wenn das Plugin die Route einer Sitzung nicht prüfen kann,
  werden deren Prompts gestoppt.
- Anfragen von Claude Code oder mit dem Token eines Claude-Abonnements werden nie umgeschrieben, nur
  geprüft.
- Wenn das Maskieren fehlschlägt, wird die Anfrage abgelehnt statt gesendet: Ein Body, den Latet
  nicht dekodieren kann, ein erforderlicher Detektor, der einen Fehler meldet, oder ein Wert, den es
  nicht ersetzen kann, enden alle in einem Fehler, der sagt, dass nichts gesendet wurde. In Claude
  Code hält das Plugin Inhalte zurück, wenn der Daemon nicht erreichbar ist.
- Das TLS der ausgehenden Verbindungen wird gegen den Zertifikatsspeicher Ihres Betriebssystems
  geprüft, sodass ein Unternehmens-Proxy, dessen Root-Zertifikat dort installiert ist, weiter
  funktioniert.
- Die Rohaufzeichnung von Austauschen zur Fehlersuche ist standardmäßig aus. Wenn Sie sie
  einschalten (`latet serve --capture <folder>`), werden Header mit Zugangsdaten geschwärzt, aber
  die Aufzeichnungen enthalten trotzdem Inhalte der Unterhaltungen: Behalten Sie sie auf Ihrem
  Rechner.

Der Daemon baut keine Netzwerkverbindungen auf, außer um die Anfragen Ihres Agenten an den Anbieter
(oder den Unternehmens-Endpunkt) weiterzuleiten, den Ihr Agent bereits nutzt, und, wenn Sie ihn
einschalten, um den lokalen Prüfer auf diesem oder einem anderen Ihrer Rechner zu fragen. Wenn er
für Amazon Bedrock mit einem SSO-Profil signiert, kann die AWS CLI, die er ausführt, den
Anmeldedienst von AWS erreichen, um Ihre Anmeldung zu erneuern, so wie sie es tut, wenn Sie sie
selbst verwenden. Die Befehle greifen nur für das auf das Netzwerk zu, worum Sie bitten:
`latet ner install` und `latet ocr install` laden ihre Modelle von huggingface.co herunter, und
`latet upgrade` sucht das neueste Release auf GitHub und lädt es herunter. Es gibt keine Telemetrie
und keine Nutzungsanalyse, weder im Daemon noch im Dashboard noch auf dieser Dokumentationsseite.

**Auf einem Latet-Server** ([Latet auf einem Server](../concepts/server/))

- Die Administration antwortet nur über HTTPS und nur einem Programm: Eine Anfrage von einer Webseite
  wird abgelehnt, und ein Verwaltungstoken wird erst geprüft, nachdem seine Adresse eine Grenze
  abgelehnter Versuche passiert hat.
- Der Server bewahrt kein Geheimnis auf, das er ausstellt: nur einen Hash mit Schlüssel von jedem
  Token, Schlüssel und Code. Der Schlüssel jedes Bereichs ist mit dem Hauptschlüssel des Servers
  versiegelt, der getrennt von den Sicherungen der Datenbank aufbewahrt wird.
- Jeder Administrationsaufruf wird in einem verketteten Log erfasst, auch Lesezugriffe und
  Ablehnungen; `latet server log verify` prüft die Kette.
- Das Token, das die Agenten eines Rechners besitzen, öffnet die Bridge des Plugins und den Proxy des
  Bereichs, nie das Dashboard: Dafür braucht es außerdem den Rechnerschlüssel, den kein Agent erhält.

## Eine Sicherheitslücke melden

Bitte eröffnen Sie kein öffentliches Issue. Nutzen Sie die private Meldung von Sicherheitslücken auf
GitHub im [Repository von Latet](https://github.com/Latet-AI/latet/security).
