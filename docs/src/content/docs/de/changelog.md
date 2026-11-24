---
title: Änderungsprotokoll
description: Die wichtigen Änderungen an Latet, die neuesten zuerst.
---

Latet folgt der [semantischen Versionierung](https://semver.org). Änderungen, die noch in keiner
Version sind, stehen unter **Noch nicht veröffentlicht**; jede Version listet ihre Änderungen, die
neuesten zuerst.

Diese Seite enthält die noch nicht veröffentlichten Änderungen und die Versionen ab 0.10.2. Die
früheren Versionen, von 0.10.0 an zurück, stehen im
[englischen Änderungsprotokoll](../../changelog/#0100-2026-10-27).

## Noch nicht veröffentlicht

Was als Nächstes kommt, steht auf der [Roadmap](../roadmap/): eine klare Ablehnung durch den
Auto-Modus, MCP-Tools maskiert, bevor Claude Code sie liest, ein signiertes Installationsprogramm
für Windows, eigene Namensmodelle, weitere Agenten und die Dokumentation in weiteren Sprachen.

## 0.15.2 (2026-11-24)

- **`unknown_members: check` in einem Projekt hält Latet nicht mehr an.** Ein Projekt kann nur
  `unknown_members: refuse` setzen, und ein von Hand geschriebenes `check` im Eintrag eines Projekts
  unter `projects` in `config.yaml` machte die ganze Datei ungültig: Latet startete nicht, und
  `latet reload` schlug fehl. Jetzt wird es ausgelassen, und `latet reload` und das Log des Daemons
  nennen den Eintrag; der Rest der Datei gilt, und das Projekt folgt Ihrem eigenen
  `unknown_members`, wie ohne diese Zeile.

- **Eine von SCIM verwaltete Person wird mit ihrer ID benannt.** Auf einem Server mit Latet Enterprise setzt ein
  SCIM-Token jeden Wert, der eine von SCIM verwaltete Person benennt: Benutzername, E-Mail-Adresse und Name. Ein Token
  in falschen Händen konnte diese Werte auf ein damit angelegtes Konto verschieben, das ein Befehl, der die Person mit
  E-Mail-Adresse oder Benutzernamen nennt, etwa `latet server roles grant`, dann erreichte. Eine solche Person wird
  jetzt allein mit ihrer ID (`u_…`) benannt, die kein Token ändert; die Werte, die ein Token setzt, finden sie weder
  in `latet server` noch in der Administrations-API des Servers mehr. Aktualisieren Sie `latet` auf den Rechnern, von
  denen Administratoren es nutzen: Eine ältere Version findet eine solche Person noch über Namen oder E-Mail-Adresse.
  Auch zwei Gruppen, deren Namen sich nur in einem Buchstaben wie ß und ss unterscheiden, werden nicht mehr
  verwechselt: `latet server` lehnt einen solchen Namen ab, und die Gruppe wird mit ihrer ID benannt. Von Hand
  hinzugefügte Personen werden weiter mit E-Mail-Adresse oder Benutzernamen benannt, und das Dashboard, das alle mit
  ihrer ID benennt, ändert sich nicht.

- **Eine Person, deren Konto eine Anmeldung angelegt hat, wird mit ihrer ID benannt.** Auf einem Server mit Latet
  Enterprise und `accounts: group` oder `open` legt eine erste Anmeldung ein Konto mit dem Namen an, den das ID-Token
  angibt und den viele Anbieter die Person selbst wählen lassen: Jemand konnte den Namen einer anderen Person
  annehmen, bevor diese sich zum ersten Mal anmeldete, und `latet server people code "Emily Carter"` gab den Code dann
  dem Konto, das den Namen angenommen hatte. `latet server people code`, `disable`, `enable`, `remove` und `tokens`
  lehnen nun den Namen und die E-Mail-Adresse einer Person ab, deren Konto eine Anmeldung angelegt hat, wie es die
  Enterprise-Befehle schon taten, und benennen sie mit ihrer ID (`u_…`): `latet server people list` zeigt sie und
  kennzeichnet nun solche Konten. Die Prüfung macht `latet` auf dem Rechner, von dem Sie es nutzen: Aktualisieren Sie
  es dort.

## 0.15.1 (2026-11-22)

- **Eine angeforderte Synchronisierung bleibt nicht mehr offen.** Auf einem Server mit Latet
  Enterprise konnte eine Synchronisierung, die (mit **Jetzt synchronisieren** oder
  `latet server terms run`) in dem Moment angefordert wurde, in dem eine Synchronisierung derselben
  Quelle begann, etwa die nach einer Änderung der Quellen, als angefordert stehen bleiben: Diese
  Synchronisierung las die Quelle, endete aber, bevor die Anforderung gespeichert war, sodass keine
  die Anforderung übernahm bis zur nächsten Synchronisierung der Quelle in ihrem Intervall, und
  `latet server terms run` wartete seine ganze Minute und gab *still running* aus. Jetzt beantwortet
  eine Synchronisierung, die bei der Anforderung oder danach begann, die Anforderung, gleich welche
  von beiden zuerst gespeichert wird. Die synchronisierten und maskierten Begriffe waren die ganze
  Zeit richtig.

## 0.15.0 (2026-11-21)

Latet 0.15.0 bringt Latet Enterprise: auf einem Latet-Server die Funktionen, die das
Sicherheitsteam eines Unternehmens braucht, im selben Paket und im selben Image wie die Community
Edition und ohne Lizenzdatei ausgeschaltet. Auf einem Server ändern sich die Tabellen, deshalb
werden seine Instanzen für die Migration einmal angehalten: Lesen Sie den Punkt **Aktualisieren** am
Ende dieser Liste, bevor Sie einen Server aktualisieren.

- **Latet Enterprise.** Die Enterprise-Funktionen laufen auf einem
  [Latet-Server](../getting-started/server/), eingeschaltet durch eine Lizenzdatei. Ihr Code liegt
  in den `ee`-Ordnern des Repositorys und steht unter der Latet Enterprise License, die das Paket
  und das Image neben der Latet Sustainable Use License enthalten. Die Lizenzdatei ist vom
  Lizenzgeber signiert: `latet server license install` installiert sie, und der Server prüft sie
  offline, mit dem öffentlichen Schlüssel des Lizenzgebers, den Latet mitbringt. Sie deckt eine
  Anzahl von Benutzern ab: Jede aktivierte Person und jeder aktivierte API-Schlüssel ist ein
  Benutzer. Ein Unternehmen kann unter [info@latet.ai](mailto:info@latet.ai) eine Testphase von
  32 Tagen anfragen. Endet eine Lizenz, weist der Server 30 Tage vorher darauf hin, alles
  funktioniert nach dem Ende noch 30 Tage weiter, und dann pausieren Berichte, Ereignisse und die
  Synchronisierung der Begriffe. Eine Testphase hat keine 30 weiteren Tage: Ist sie beendet, können
  ihre Enterprise-Einstellungen eingesehen, aber nicht geändert werden. Keines von beiden verringert
  den Schutz: Die gesperrten Regeln bleiben in Kraft, und nichts geht unmaskiert hinaus. Siehe
  [Editionen und Lizenz](../enterprise/).
- **Gesperrte Regeln.** Ein Unternehmen legt auf seinem Server eine Untergrenze fest, die keine
  Person und kein API-Schlüssel unterschreiten kann: Vorgaben im Format einer Regeldatei, mit
  `latet server policy set` für jede Instanz festgelegt. `latet policy check` liest eine Datei,
  bevor sie in Kraft gesetzt wird, und sagt, was sie sperrt. Eine Änderung, die unter die
  Untergrenze ginge, wird abgelehnt, und die Ablehnung nennt die Einstellung, die sie senkt; das
  Dashboard zeigt jede Einstellung, die das Unternehmen festlegt, mit einem Schloss und
  *Von Ihrem Unternehmen festgelegt*. Mit `exceptions: company` zählen nur die Ausnahmen des
  Unternehmens. Siehe [Gesperrte Regeln](../enterprise/locked-rules/).
- **Single Sign-on, Gruppen und Rollen.** Personen melden sich mit ihrem Unternehmenskonto über
  OpenID Connect (Microsoft Entra ID, Okta, Google, Keycloak und weitere) am Dashboard an; der
  Identitätsanbieter legt ihre Konten an, ändert und deaktiviert sie und überträgt seine Gruppen per
  SCIM 2.0; fünf Rollen (Benutzer, Gruppenleitung, Sicherheit, Auditor, Administrator), auf der
  Seite **Personen und Gruppen** an Personen oder Gruppen vergeben, entscheiden, wer was sieht und
  ändert; und die Personen oder Gruppen, die ein Administrator auswählt, steuern jeden
  API-Schlüssel. Siehe [Anmeldung, Gruppen und Rollen](../enterprise/sign-in/).
- **Berichte, das Administrationsprotokoll und Ereignisse an Ihr SIEM.** Der Server zählt
  Anfragen, maskierte Werte und gestoppte Anfragen nach Gruppe, Projekt, Agent, API-Schlüssel und
  Werttyp, nie einen Wert selbst: Die Seite **Berichte** zeigt sie, mit Exporten als CSV und JSON,
  und die Seite **Administrationsprotokoll** zeigt, was die Administration des Servers getan hat.
  Die Ereignisse und die Einträge des Protokolls gehen an den HTTP Event Collector von Splunk, an
  JSON-Webhooks oder an Syslog über TLS. Ein `stopped`-Ereignis nennt seinen Grund aus der Liste
  *Warum eine Anfrage gestoppt wurde* auf der Seite zu den Berichten, `settings-changed`
  eingeschlossen: Eine SIEM-Regel, die anhand dieser Liste geschrieben wurde, erfasst ihn. Siehe
  [Berichte und Administrationsprotokoll](../enterprise/reports/) und
  [Ereignisse an Ihr SIEM](../enterprise/siem/).
- **Zentrale Freigaben.** Das Sicherheitsteam des Unternehmens entscheidet über die Änderungen, die
  den Schutz verringern, anstelle der Person, die sie angefordert hat; diese sieht, wer entscheidet,
  und kann die Änderung zurückziehen, aber nicht freigeben. Die Änderungen eines API-Schlüssels
  gehen an die anderen Personen, die ihn steuern. Wenn das Unternehmen es so wählt, entscheidet das
  Sicherheitsteam auch über die Texte des Live-Prüfers. Ein Administrator schaltet die zentralen
  Freigaben mit `latet server approvals set` ein. Siehe [Zentrale Freigaben](../enterprise/approvals/).
- **Synchronisierte Begriffe.** Der Server liest die Listen eines Unternehmens mit Kunden, Kollegen
  und Codenamen von Projekten in festen Abständen aus einer Datei oder von einer Webadresse (CSV,
  JSON oder YAML), aus Microsoft Entra ID oder aus einem LDAP-Verzeichnis wie Active Directory und
  maskiert sie für jede Person und jeden API-Schlüssel, die er bedient, ohne dass jemand sie
  eintippen muss. LDAP braucht das neue Extra `ldap`, das im Image enthalten ist. Siehe
  [Begriffssynchronisierung](../enterprise/term-sync/).
- **Die eigenen Verbindungen von Latet Enterprise, unter jeder Adresse.** Der Identitätsanbieter,
  eine SIEM-Ausgabe (Splunk, ein Webhook oder Syslog), eine Begriffsquelle unter einer Webadresse,
  Microsoft Entra ID und ein LDAP-Verzeichnis werden unter der nächsten Adresse erreicht, die die
  Auflösung ihres Namens geprüft hat, wenn die Verbindung zur vorherigen fehlschlägt, wie seit
  0.12.1 schon ein eigenes Ziel einer Person. Steht eine Verbindung, wird keine andere Adresse mehr
  versucht.
- **Was ein Community-Server von der Edition zeigt.** `latet server status` schließt die Zeile
  jeder Instanz mit ihrer Edition ab, *Community Edition* oder *Enterprise Edition*, und auch die
  gemeinsamen Einstellungen der Administrations-API enthalten `edition`. Lehnt ein
  Community-Server eine Regel für alle ab, nennt die Ablehnung, wie sich Regeln von Hand teilen
  lassen, und erklärt, dass solche Regeln mit der Enterprise Edition die Vorgaben des Unternehmens
  sind, die mit `latet server policy set` festgelegt werden.
- **Die Antworten eines Servers an Agenten und Programme, für einen Browser in einer Sandbox.** Auf
  einem Server enthält jede Antwort unter einer Adresse für Modelle (`/u/…`) und jede Antwort der
  API für Anwendungen den Header `Content-Security-Policy: default-src 'none'; sandbox`: Ein
  Browser, der eine davon öffnet, führt darin kein Skript aus und behandelt sie nie als Seite des
  Servers. Ein Cookie, das ein Ziel setzt, erreicht den Client nur für diese Adresse und nur, wenn
  es nicht den Namen eines Cookies von Latet trägt, und der Header `Clear-Site-Data` eines Ziels
  wird nicht mehr weitergegeben. HTTP-Clients ignorieren den neuen Header.
- **Das Bild des Live-Prüfers, so wie es jetzt gesendet wird.** Ein Bild, das im Live-Prüfer
  wartet, wird neu gezeichnet, wenn seine Texte während des Wartens erneut maskiert werden, nach
  einer Änderung der Regeln oder des Tresors, sodass *So, wie es jetzt gesendet wird* zeigt, was
  gesendet wird; bisher blieb es bei dem Bild, das gezeichnet wurde, als sein Text zu warten begann.
  Ein wartendes Bild wird dem Dashboard außerdem mit dem Typ geliefert, den seine Bytes tatsächlich
  haben, PNG, JPEG, WebP oder GIF, nie mit dem, den seine Anfrage angab, und unter einer Richtlinie,
  die es in eine Sandbox setzt.
- **Stabilere Zeitmessungstests.** Die beiden Tests, die messen, wie lange Latet für eine Zeile aus
  `key=value`-Paaren braucht, die so lang ist, wie ein Text sein kann, prüfen jetzt, dass die ganze
  Zeile weniger als das 30-Fache der Zeit für ein Zehntel davon braucht: Wächst die Dauer des Lesens
  mit der Länge, ist das erfüllt; wächst sie mit dem Quadrat der Länge, ist es das nicht, wie
  schnell der Rechner auch ist. Auf einer langsamen CI-Maschine scheitern sie nicht mehr.
- **Hermes Agent mit Anmeldung über ChatGPT.** Drei seiner Anfragen wurden seit 0.11.0 abgelehnt
  und gehen jetzt durch, jede in ihrer genauen Form: eine Unterhaltung, die über seine Kommandozeile
  oder sein Gateway begonnen wurde (eine Sitzungs-ID aus `local-` und 64 Hexadezimalziffern), die
  Datenresidenz eines ChatGPT-Workspace (`x-openai-internal-codex-residency`, zwei Kleinbuchstaben
  oder `no_constraint`, nur an den Adressen von ChatGPT) und eine fortgesetzte Unterhaltung, deren
  Antworten ihre Phase mitführen (`commentary`, `partial_answer` oder `final_answer`, nur an den
  eigenen Nachrichten des Modells). Jeder andere Wert wird weiterhin abgelehnt. Siehe
  [Hermes Agent](../agents/autonomous-agents/#mit-chatgpt-angemeldet).
- **Ein Geheimnis vor einem maskierten Anführungszeichen.** In der Ausgabe eines Werkzeugs, die ein
  JSON-Dokument enthält, wurde ein Geheimnis, das ohne Anführungszeichen direkt vor einem
  Anführungszeichen steht, das das Dokument mit einem Escape schreibt (`token=…\"`), mitsamt dem
  Backslash gelernt, und sein Ersatzwert nahm den Platz des Backslashs ein: Das Dokument ließ sich
  nicht mehr lesen. Die Backslashes werden jetzt als Teil des Anführungszeichens gelesen: Nur das
  Geheimnis wird maskiert, und das Dokument bleibt ganz.
- **Ein Bearer-Token, gefolgt von Punkten.** Die Punkte nach einem Token, wo ein Text es abkürzt
  (`Bearer <token>...`) oder einen Satz damit beendet, werden nicht mehr als Teil davon gelesen,
  wenn davor ein ganzes Token steht und kein Platzhalter wie `YOUR_API_KEY`: Das Token behält seinen
  einen Ersatzwert, und ein Ersatzwert, gefolgt von `...`, hält keine Anfrage mehr auf.
- **Ein Name in der Bedingung eines Aufrufs.** Im Code bezeichnet `f(access_token=fallback or "")`
  eine Variable, wie schon `f(token=fallback)`: Er wird nicht mehr als Geheimnis maskiert. Ein Wert
  mit einer Ziffer wird wie bisher maskiert.
- **Ein später erkanntes Geheimnis im Verlauf der Unterhaltung.** Wenn eine spätere Ausgabe zeigt,
  dass ein Wert, den eine frühere Ausgabe nebenbei nannte, ein Geheimnis ist (ein Token nach
  `Bearer`), wird die frühere Ausgabe ab der nächsten Anfrage neu maskiert, ein einziges Mal, statt
  erneut im Klartext gesendet zu werden. Ihr Text ändert sich für den Anbieter, sodass sein Cache der
  Unterhaltung von dort an neu beginnt, einmal. Andere Werte gehen weiterhin so hinaus, wie sie
  hinausgingen. Die Anfragen von Claude Code sind nicht betroffen: Latet prüft sie und schreibt sie
  nie um.
- **Claude Code 2.1.323.** Jede seiner Anfragen wurde abgelehnt, mit *unknown member in message*,
  und nichts wurde gesendet. Seine drei neuen Elemente gehen jetzt durch, jedes nur so, wie Claude
  Code es sendet: eine Systemnachricht in der Unterhaltung, die den Aufwand ab ihrer Runde festlegt
  (`output_config` nur mit `effort`), die Bearbeitung des Kontexts, die das Denken der früheren
  Runden behält (`context_management`, `clear_thinking_20251015` mit `keep: "all"`), und der Aufwand
  `xhigh`, seine Voreinstellung und einer der fünf Werte, die `--effort` annimmt. Der Aufwand `xhigh`
  geht bei jeder Anfrage an die API von Anthropic durch. Eine Anfrageausnahme, die zuvor für ein
  Element unter `context_management`, für das `output_config` einer Nachricht oder für die `phase`
  einer Nachricht in der Responses-API von OpenAI genehmigt wurde, wartet erneut auf Genehmigung:
  Latet liest diese Elemente jetzt nach eigenen Regeln.
- **Ein Feld, das noch kein Anfrageprofil vorsieht, gelesen und geprüft.** Ein Agent hörte hinter
  Latet auf zu funktionieren, jedes Mal wenn eine neue Version von ihm seinen Anfragen ein Element
  hinzufügte, bis eine Version von Latet dieses Element nannte. Latet liest ein solches Element
  jetzt ganz, bei jeder Anfrage, bei denen, die es nur prüft, und bei denen, die es umschreibt,
  prüft es so streng wie einen Text, den es maskiert, und sendet es unverändert, wenn darin nichts
  maskiert werden müsste und es keine Datei, keine Adresse, keine gespeicherte Unterhaltung und kein
  Modell nennt: Ein Modus, ein Flag, eine Zahl oder ein kurzes Token geht durch, und ebenso ein
  Objekt oder eine Liste davon. Eine Webadresse, eine Datei, ein Anhang, freier Text in einem eigenen
  Element des Anbieters, kodierte Daten oder ein sensibler Wert stoppen die Anfrage weiterhin, mit den
  Worten von vorher. Ein solches Element wird nie maskiert, und sein Name wird nie aufgezeichnet: Das
  Dashboard führt *ein Feld, das noch kein Anfrageprofil vorsieht, gelesen und geprüft* unter dem auf,
  was eine Anfrage durchgelassen hat, und einen darin gefundenen Wert unter der Art *Neues Element der
  Anfrage*. `unknown_members: refuse` lehnt sie ab wie bisher, in Ihren Einstellungen oder auf der
  Seite **Regeln**; die Einstellungen eines Projekts und die `latet.yaml` seines Repositorys können sie
  nur ablehnen, und mit Latet Enterprise können die Vorgaben eines Unternehmens `refuse` sperren.
  `latet doctor` und `latet server status` sagen, welcher Wert gilt. Siehe
  [Neue Elemente in Anfragen](../getting-started/upgrading-to-0-11/#neue-elemente-in-anfragen).
- **Das Schema eines MCP-Tools, mit Namen.** Wenn ein Wert im Klartext im Parameterschema eines
  MCP-Tools die Anfragen von Claude Code stoppt, wo Latets Plugin ihn nicht maskieren kann, nennt die
  Fehlermeldung das Tool und sagt, was zu tun ist: den Wert durchlassen, wenn er nicht sensibel ist,
  oder den Server abschalten. Das Dashboard zeigt den Block als *Parameterschema des Tools*, mit
  einer eigenen Erklärung. Siehe [Fehlerbehebung](../troubleshooting/).
- **CLAUDE.md und Ihre E-Mail-Adresse, mit Claude Code 2.1.322 und neuer.** In einer interaktiven
  Sitzung liest Claude Code den Kontext der ersten Nachricht (Ihre CLAUDE.md-Dateien, Ihre
  Memory-Dateien, Ihre E-Mail-Adresse), bevor es das Plugin von Latet startet, und behält diese
  Antwort für die ganze Unterhaltung. Das Plugin hielt ihn dann zurück, weil es noch nicht wusste,
  wohin die Anfragen gehen, und Claude arbeitete die ganze Sitzung ohne ihn; nichts wurde im
  Klartext gesendet. Jetzt lässt das Plugin Claude Code ihn erneut lesen, sobald es das weiß, und
  die erste Nachricht trägt ihn maskiert. Mit `claude -p` wartet der Kontext der SessionStart-Hooks,
  der eintraf, während das Plugin noch startete, einige Sekunden darauf, statt zurückgehalten zu
  werden.
- **Die Verbindung von Claude Code beim Start.** Beim Start öffnet Claude Code seine Verbindung mit
  einem Aufruf `HEAD` an `/api/hello`, ohne Inhalt und ohne Zugangsdaten, und liest die Antwort
  nicht. Latet lehnte ihn ab, weil kein Profil ihn nannte: Gesendet wurde nichts, aber jeder Start
  hinterließ unter *Sitzungen* eine blockierte Anfrage, angezeigt als `/other`, und `latet doctor`
  führte ihn unter den Aufrufen auf, die kein Profil abdeckt. Jetzt geht er unverändert durch, unter
  einem eigenen Profil, `anthropic.hello.1`: nur als `HEAD`, nur an das Ziel von Anthropic, ohne
  Abfrageparameter und ohne Inhalt, und nur mit den Headern, die ein Profil nennt.
- **Ein Geheimnis aus einem Wort und bloßen Zeichen.** Ein Geheimnis aus einem Wort, auf das nur
  Zeichen folgen, etwa ein Bearer-Token (`Bearer abcdefghijk.........`, `Bearer abc---------`) oder
  ein Wert nach `token =`, bekam keinen Ersatzwert: Latet hielt das Wort für ein öffentliches
  Präfix, wie `sk-ant-` eines ist, und behielt es zusammen mit den Zeichen, sodass der einzige
  Ersatzwert des Geheimnisses das Geheimnis selbst war. Die Anfrage wurde abgelehnt, ebenso jede
  spätere Anfrage derselben Unterhaltung; gesendet wurde nichts. Jetzt werden seine Buchstaben
  ersetzt, wie bei jedem Geheimnis.
- **Eine Adresse und die Wörter um sie herum.** Wenn das Namensmodell nicht sicher war, dass die
  Stadt zu einer Adresse wie `via Garibaldi 12 a Bologna` gehört, endete die Straße auf dem Wort vor
  der Stadt, und der Ersatzwert der Straße nahm dieses Wort mit (`via Mariani 90 p Bologna`). Am
  Anfang eines Satzes wurden die Wörter vor der Straße (`Abito in via Garibaldi 12`) mit ihr
  maskiert. Jetzt wird die Straße allein maskiert, und die Wörter um sie herum bleiben, wie sie sind.
- **Der Ersatzwert eines Geheimnisses behält nur das Präfix des Anbieters.** Der Ersatzwert eines
  Geheimnisses behielt bis zu 24 Buchstaben, Ziffern und Trennzeichen vom Anfang des Werts. Bei
  einem Token, dessen zufälliger Teil `-`, `_` oder `.` enthält, reichte das in den zufälligen Teil
  hinein, und bei einem Wert, der einem Namen zugewiesen ist (`my-company-prod-…`), behielt es die
  Wörter des Werts: Diese Zeichen gingen im Ersatzwert an den Anbieter des Modells. Jetzt behält ein
  Ersatzwert nur das Präfix, das der Anbieter jedem Schlüssel seiner Art voranstellt (`ghp_`,
  `sk-ant-api03-`, `AKIA` und Latets eigenes `latet_pat_`), und zieht jeden anderen Buchstaben und
  jede andere Ziffer neu; ein Geheimnis jeder anderen Art behält keinen seiner Buchstaben und keine
  seiner Ziffern, nur seine Sonderzeichen (`-`, `_`, `.`) an ihrer Stelle. Ein Ersatzwert zog
  außerdem eine Ziffer, wo der Wert eine Ziffer hat, einen Großbuchstaben, wo er einen Großbuchstaben
  hat, und einen Kleinbuchstaben, wo er einen Kleinbuchstaben hat, und zeigte so, an welchen Stellen
  ein Passwort wie `Summertime2024` oder ein Schlüssel eine Ziffer, einen Groß- oder einen
  Kleinbuchstaben hat. Jetzt wird jeder Abschnitt aus Buchstaben und Ziffern zwischen Sonderzeichen
  an jeder Stelle aus allen Zeichenarten gezogen, die er enthält: ein Abschnitt hexadezimaler Ziffern
  aus allen sechzehn (`9f2c4e…`), `Summertime2024` aus allen 62 Buchstaben und Ziffern; ein eigener
  Erkenner mit `class: secret` behält die Form, die er beschreibt (`surrogate: shape`: an jeder
  Stelle dieselbe Zeichenart). Ein Ersatzwert,
  der schon in Ihrem Tresor liegt, bleibt, wie er ist, damit eine
  laufende Unterhaltung denselben Ersatzwert sieht; nur ein Wert, den Sie mit *Vergessen* entfernen,
  erhält einen Ersatzwert der neuen Art, wenn er wieder auftaucht.
- **Der Port von Latet, unter Windows reserviert.** Hyper-V, WSL und Docker reservieren bei jedem
  Start von Windows Blöcke von Ports, innerhalb des dynamischen Portbereichs von Windows; wo ein
  Programm diesen Bereich von 49152 nach unten verschoben hat, kann ein Block Port 5283 belegen. Der
  Daemon konnte dann bis zum nächsten Neustart nicht starten, und der Rat, den Latet gab,
  `latet global-install`, half nicht, wenn derselbe Block auch seine Ausweichports belegte. Der neue
  Befehl `latet reserve-port` reserviert den Port von Latet mit einer einzigen Bestätigung eines
  Administrators, sodass Windows ihn nicht mehr belegen kann; wo Windows ihn bereits belegt, stellt
  er Latet auf einen freien Port um, mit den Agenten, die `latet global-install` eingerichtet hat,
  und reserviert diesen. Er fragt, bevor er etwas ändert, ein Agent kann ihn nicht ausführen, und
  `latet reserve-port --remove` gibt die Ports frei, die er reserviert hat. Das Installationsskript
  für Windows bietet ihn am Ende an, wo Windows den Port belegen kann; `latet start`, `latet claude`
  und die anderen Wrapper nennen ihn, wo Windows den Port belegt; `latet doctor` nennt ihn ebenfalls,
  mit einem Fehler, wo Windows den Port belegt, und einer Warnung, wo Windows ihn belegen kann; und
  `latet global-uninstall` bietet an, die Reservierungen aufzuheben. `latet global-install` sucht
  einen freien Port jetzt jenseits der Ports, die Windows belegt, nicht mehr nur zwischen 5284 und
  5335, und ein Port, der nur in einem Portausschluss eines Administrators oder in einem IPv6-Bereich
  liegt, gilt nicht mehr als von Windows belegt. Siehe
  [Port 5283 ist bereits belegt](../troubleshooting/#port-5283-ist-bereits-belegt).
- **Geheimnisse in den gespeicherten Sitzungen Ihrer Agenten.** Der neue Befehl `latet leak` liest
  die Sitzungen, die Claude Code, Codex, OpenCode, Copilot CLI, Gemini CLI, Qwen Code und Hermes
  Agent auf diesem Rechner gespeichert haben, mit den eigenen Detektoren von Latet, und führt die
  Geheimnisse darin in drei Listen auf: im Klartext, ohne ein Zeichen, dass Latet sie ersetzt hat,
  also möglicherweise beim Modellanbieter angekommen; von Latet ersetzt, dessen Ersatzwert an ihrer
  Stelle hinausging; und geschwärzt. Ein Geheimnis zeigt seinen Typ, einen Hinweis, der nur das
  öffentliche Präfix seines Anbieters behält (`ghp_…`), die Agenten, die Sitzungen, die Ordner und
  die Daten, nie seinen Wert; die Schlüssel eines Anbieters, die zuerst auszutauschen sind, stehen
  vorn. Der Befehl ändert nichts und braucht kein Netzwerk, und der Tresor von Latet erfährt dabei
  nichts. Siehe [Geheimnisse in gespeicherten Sitzungen](../concepts/leaks/).
- **Die deutsche, französische, italienische und spanische Fassung, noch einmal gelesen.** Die
  Dokumentation und das Dashboard auf Deutsch, Französisch, Italienisch und Spanisch wurden Satz
  für Satz noch einmal mit dem Englischen verglichen. Wo sie etwas anderes, weniger oder mehr sagten
  als das Englische, wurden sie korrigiert, ebenso ihre Begriffe und Formulierungen.
- **Die Suche der Dokumentation auf Italienisch, Deutsch und Französisch.** In der italienischen,
  deutschen und französischen Dokumentation antwortete die Suche auf Englisch
  (*17 results for eccezione*): Starlight gibt der Suche ihre Wörter nur auf Spanisch, und die
  Übersetzungen von Pagefind erreichen eine so gebaute Seite nie. Jetzt antwortet sie in der Sprache
  der Seite (*17 risultati per eccezione*).
- **Abhängigkeiten.** Das Extra `server` bringt PyJWT mit, das das ID-Token einer Anmeldung mit dem
  Unternehmenskonto prüft; das neue Extra `ldap` installiert ldap3 und pyasn1 für die
  Synchronisierung der Begriffe aus LDAP; und httpcore, das schon mit httpx kam, ist jetzt selbst
  eine Abhängigkeit. Die Hinweise zu Drittanbietern führen sie auf.
- **Aktualisieren.**
  - **Ein Server: Die Tabellen ändern sich.** 0.15.0 fügt der Datenbank jedes Servers die Tabellen
    der Enterprise Edition hinzu, leer und ohne Lizenz ungenutzt, deshalb lassen sich die Instanzen
    nicht eine nach der anderen ersetzen. Erstellen Sie mit `pg_dump` eine Kopie der Datenbank und
    führen Sie `latet server migrate --check` mit 0.15.0 aus, das meldet, dass sich die Tabellen
    ändern; halten Sie dann alle Instanzen an, führen Sie `latet server migrate` einmal mit 0.15.0
    aus und starten Sie die Instanzen damit, wie
    [Serverbetrieb](../concepts/server-operations/#aktualisieren-und-instanzen-hinzufügen) zeigt. Im
    Compose-Beispiel ist `ghcr.io/latet-ai/latet:latest` jetzt 0.15.0: Wer es neu zieht, folgt
    denselben Schritten. Zu seinen Migrationen gehört 018 (`server_notes`), in der der Server
    vermerkt, dass er die neue Voreinstellung für neue Elemente in Anfragen weiter unten aufgezeichnet
    hat: `latet server migrate` wendet sie mit den anderen an. Um zurückzukehren, spielen Sie die
    Kopie wieder ein und starten Sie 0.12.1.
  - **Ein Rechner:** `latet upgrade` (in Docker `docker compose pull`, dann `docker compose up -d`),
    sonst nichts: Die Enterprise Edition gehört zu einem Server.
  - **Ohne Lizenz ändert die Enterprise Edition nichts:** keine Enterprise-Seite und keine
    Enterprise-Einstellung. Ein Skript, das `latet server status` liest, findet die Edition am Ende
    der Zeile jeder Instanz.
  - **Neue Elemente in Anfragen: Jede Installation, Server eingeschlossen, wechselt zu `check`;
    setzen Sie `refuse`, um die heutige Ablehnung zu behalten.** Ab 0.15.0 wird ein Element einer
    Anfrage, das noch kein Anfrageprofil nennt, gelesen und geprüft, nicht mehr abgelehnt, ohne
    Freigabe. Um die Ablehnung zu behalten, setzen Sie `unknown_members: refuse`: in `config.yaml`
    auf einem Rechner, in den Einstellungen einer Person oder eines API-Schlüssels auf einem Server
    oder für alle in den Vorgaben des Unternehmens mit Latet Enterprise. Auf einem Rechner sagt es
    `latet doctor`, solange `config.yaml` diesen Schlüssel nicht hat; `latet server status` nennt
    den Wert des Servers in einer eigenen Zeile, der erste Start von 0.15.0 eines Servers schreibt
    einen Eintrag `setting.defaulted` in das Administrationsprotokoll, und eine Freigabe, die
    Einstellungen von `refuse` zurück auf `check` stellt, schreibt `setting.loosened`.
  - **Um Latet Enterprise auszuprobieren:** Fragen Sie unter [info@latet.ai](mailto:info@latet.ai)
    eine Testphase an, und installieren Sie für die Synchronisierung der Begriffe aus LDAP das Extra
    `ldap`.
  - **Keine Wörter eines Schlüssels neu gebildet:** 0.15.0 bildet die Wörter keines API-Schlüssels
    neu.

## 0.12.1 (2026-11-09)

- **Ein Ziel, erreicht unter seinen anderen Adressen.** Auf einem Server wird ein eigenes Ziel einer
  Person, dessen Name mehrere Adressen hat, unter der nächsten erreicht, in der Reihenfolge, die das
  System angibt, wenn die Verbindung zur ersten abgelehnt wird oder nicht rechtzeitig zustande
  kommt, bevor etwas von der Anfrage gesendet ist. Ein System kann die IPv6-Adresse eines Namens
  zuerst nennen (Windows tut es für `localhost`), und ein Anbieter, der nur unter seiner
  IPv4-Adresse antwortet, wird dann dort erreicht; eine Verbindung, die zu einer von ihnen noch
  offen ist, wird zuerst genutzt, sodass eine Adresse, die ablehnt, ihre Zeit nur einmal kostet,
  solange diese Verbindung offen bleibt. Versucht werden nur die Adressen, die dieselbe Auflösung
  geprüft hat, höchstens acht, in der Zeit, die eine Verbindung hat; eine Anfrage, die eine erreicht
  hat, wird nie an eine andere gesendet, und ein Name mit irgendeiner Adresse im Netz des Servers
  wird weiterhin abgelehnt.
- **Ein während einer Einstellungsänderung abgelehntes Wiederherstellen wird nicht mehr
  protokolliert.** Ein Wiederherstellen über die Anwendungs-API wurde abgelehnt (`503`, *send it
  again*), wenn während seiner Beantwortung begonnen wurde, die Einstellungen des Schlüssels
  anzuwenden. Es gab nichts zurück, hinterließ aber trotzdem einen Eintrag `apps.unmasked` im
  Administrationsprotokoll, und sein erneuter Versuch einen zweiten. Ein Wiederherstellen, das schon
  beim Eintreffen abgelehnt wurde, weil die Einstellungen gerade angewendet wurden, hinterließ
  keinen. Der Eintrag wird jetzt nur für ein Wiederherstellen geschrieben, das beantwortet wird,
  sodass jeder Eintrag ein Wiederherstellen ist, das seine Texte zurückgegeben hat. Ein Analysieren,
  Maskieren oder Wiederherstellen, das während seiner Beantwortung abgelehnt wird, wird auch in
  `latet_apps_texts_total` und `latet_apps_values_total` nicht mehr gezählt.

## 0.12.0 (2026-11-09)

- **Eine Anfrage, deren Einstellungen sich vor dem Senden ändern.** Eine Anfrage, deren
  Einstellungen sich zwischen ihrer Entscheidung und ihrem Senden ändern, wird abgelehnt wie eine,
  deren Einstellungen sich während ihrer Vorbereitung geändert haben: Ihr Agent erfährt, dass sie
  erneut zu senden ist, und der Verlauf zeichnet eine Ablehnung auf, wo sie vorher mit `500` *Latet
  could not send this request as it was decided* beantwortet und als Austausch aufgezeichnet wurde.
  Eine Anfrage, die vorbereitet und gesendet wird, während ein Speichern noch angewendet wird, geht
  nicht mehr mit halb angewendeten Einstellungen hinaus: Sie wird erneut vorbereitet, sobald alle an
  ihrem Platz sind, oder ebenso abgelehnt. Ein Speichern, das mittendrin scheitert, stellt die
  Einstellungen von vorher wieder her; lassen sich auch diese nicht wiederherstellen, wird jede
  Anfrage ebenso abgelehnt, bis ein späteres Speichern gelingt oder Latet neu startet.
- **Neue Header von Claude Code.** Bei einer Anfrage, die Latet nur prüft (Claude Code, Bedrock,
  Vertex), geht ein `anthropic-*`-Header, den noch kein Profil nennt, oder ein `anthropic-beta`-Wert
  einer neuen Form durch, sobald die abschließende Prüfung ihn gelesen hat, angezeigt als *nur
  geprüft*, wie es ein neues Element des Inhalts mit Text schon tut; ein neuer `x-claude-code-*`-Header
  wird entfernt und gezählt. Claude Code arbeitet weiter, wenn eine Version einen solchen Header
  hinzufügt, bevor Latet ihn nennt. Ein Geheimnis oder einer Ihrer Werte in einem solchen Header wird
  weiterhin abgelehnt.
- **Einen Server von 0.10 aktualisieren.** Das Bilden der Fingerabdrücke eines von 0.10 gehaltenen
  Tresors belegt nicht mehr einen der zwei Indexaufbauten einer Instanz: Die Indizes der anderen
  Identitäten werden währenddessen aufgebaut, und nur die Identitäten, deren Fingerabdrücke noch
  nicht alle gebildet sind, werden über die Suche in PostgreSQL bedient, wie die Schritte der
  Aktualisierung sagen.
- **Python 3.14.** Latet läuft auch unter Python 3.14 und wird neben 3.12 und 3.13 auch damit
  getestet. Eine neue Installation auf einem Computer ohne Python erhält 3.14; `latet upgrade`
  stellt ein installiertes Latet nur dort auf 3.14 um, wo 3.14 schon vorhanden ist.

## 0.11.1 (2026-11-06)

- **Die Hinweis-Header von Claude Code.** Ist Claude Code so eingestellt, dass es seine Hinweis-Header
  für Gateways sendet (`CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`), wird es nach einer Kompaktierung oder
  einem Werkzeugaufruf nicht mehr abgelehnt: `x-claude-code-compaction` und
  `x-claude-code-context-compacted` gehen durch, auf ihre drei Werte beschränkt, und
  `x-claude-code-prev-tool-durations`, der die ausgeführten Werkzeuge nennt, wird entfernt und
  gezählt, wie die IDs seiner Subagenten.
- **Eine Anfrage, während sich die Einstellungen ändern.** Eine Anfrage, die vorbereitet wurde,
  während ihre Einstellungen erneut angewendet wurden (ein Speichern, oder auf einem Server eine
  gemeinsame Änderung), wird unter den neuen erneut vorbereitet, wie Latet es schon tat, wenn sich
  nur die Regeln änderten, statt mit *could not account for every part of this request* abgelehnt zu
  werden. Eine Anfrage, deren Route sich inzwischen geändert hat oder deren Einstellungen sich
  geändert haben, nachdem sie vorbereitet wurde, wird weiterhin nicht gesendet, und ihr Agent
  erfährt, dass sich die Einstellungen geändert haben und sie erneut zu senden ist; der Verlauf, das
  Log und das Dashboard sagen dasselbe, mit einem eigenen Grund.
- **Eine vollständig gestreamte Antwort wird vollständig aufgezeichnet.** Eine gestreamte Antwort, die
  ihren Client vollständig erreicht hat, wird vollständig aufgezeichnet, auch wenn der Client die
  Verbindung gleich danach schließt, wie es bei einem WebSocket-Turn schon war; vorher zeigte der
  Verlauf sie als Fehler, ohne ihren Text.
- **Ein WebSocket, den der Anbieter ablehnt, steht im Verlauf.** Ein WebSocket, dessen Handshake der
  Anbieter ablehnt (`401`, `403`) oder umleitet, oder dessen Anbieter nicht erreichbar ist, wird im
  Verlauf aufgezeichnet wie dieselbe Antwort über HTTP; vorher hinterließ er keinen Eintrag.
- **Der Index der Anfrage-Ausnahmen auf dem Server.** Ein Bereich, der auf mehreren Workern oder
  Instanzen zugleich aufgebaut wird, schreibt den Index der Anfrage-Ausnahmen der Identität nicht mehr
  jedes Mal neu: Er wird nur geschrieben, wenn er sich geändert hat, von einem nach dem anderen, sodass
  das Log nicht mehr warnt, er sei nicht geschrieben worden, und `latet server status` nie eine
  inzwischen widerrufene Ausnahme auflistet.
- **Eine Route, die zwischen zwei AWS-Versuchen wegfällt.** Wird eine Bedrock-Route zwischen den
  zwei AWS-Versuchen einer Anfrage entfernt (der erste wurde mit einem abgelaufenen Token
  beantwortet), erfährt der Agent, dass kein weiterer Versuch gesendet wurde und der erste
  angekommen sein kann, statt nur *no upstream for this path*.
- **Wer eine Anfrage-Ausnahme genehmigt hat.** Eine genehmigte Ausnahme, deren `issuer` (wer sie
  genehmigt hat) von Hand geändert wurde, wartet erneut auf Genehmigung, auch wenn ihre
  Genehmigungszeit zudem vorverlegt wurde; vorher hielt diese frühere Zeit sie mit dem neuen
  `issuer` in Kraft.

## 0.11.0 (2026-11-05)

Latet 0.11.0 blockiert Anfragen standardmäßig, wenn ein Teil des Pfads, der Abfrageparameter, der
Header oder des Inhalts weder geprüft noch ausdrücklich genehmigt ist. Ein leerer Inhalt reicht nicht
aus, um eine Anfrage zuzulassen. Erforderliche Zugangsdaten des Anbieters und signierte Bestandteile
bleiben nach den festgelegten Regeln erhalten. Prüfen Sie vor dem Upgrade Ihre Routen und eng
begrenzten Ausnahmen; bisher ungeprüft gesendete Aufrufe können nun blockiert werden. Das Dashboard
kennzeichnet ausdrücklich zugelassene ungeprüfte Teile. Eine Ablehnung sagt dem Agenten nur dann,
dass nichts gesendet wurde, wenn nichts hinausging: Wird ein zweiter Versuch abgelehnt, nachdem ein
erster den Anbieter erreicht hat, erfährt der Agent, dass kein weiterer Versuch gesendet wurde und
der erste angekommen sein kann. [Umstieg auf 0.11](../getting-started/upgrading-to-0-11/) sagt, was
zuerst zu prüfen ist, und `latet doctor` listet auf, was stehen bleiben wird.

- **Auf einem Rechner wie auf einem Server.** Ein Rechner lehnt jetzt ab, was ein Latet-Server
  ablehnte: eine Anfrage, deren Inhalt Latet nicht liest, Inhalte in einer Form, die es nicht liest,
  einen WebSocket-Frame, der keine Runde ist. Die Tool-Definitionen von Claude Code werden auch auf
  einem Rechner geprüft. Auf einem Rechner ist der Inhalt einer Anfrage auf 32 MiB begrenzt.
- **Aufrufe ohne Inhalt.** Die Liste der Modelle und das Lesen eines Modells gehen bei jedem Anbieter
  durch, den Latet kennt, die Inferenzprofile von Bedrock eingeschlossen, und ihre Abfrageparameter
  werden gelesen. Jeder andere Aufruf wird abgelehnt, ohne eine Route von Ihnen zu nennen: Dateien,
  Uploads, Batches, Audio, Bilderzeugung, Moderation, eine gespeicherte Antwort, das Lesen eines
  Gemini-Caches.
- **Header und Abfrageparameter.** Jeder wird nach einer eigenen Regel gelesen. Ein Header, den kein
  Profil nennt, wird abgelehnt, und sein Name wird nur dem Agenten genannt. Die Telemetrie- und
  Tracing-Header der SDKs (`x-stainless-*`, `traceparent`, `amz-sdk-*` des AWS SDK,
  `x-goog-api-client`), die IDs der Subagenten von Claude Code und die Werte, die es aus seiner
  Umgebung übernimmt (`x-claude-code-agent-id`, `x-claude-remote-session-id`...), sowie
  `x-codex-turn-metadata` und `x-openai-subagent` von Codex werden entfernt und im Dashboard gezählt,
  und ein darauf folgendes `401` oder `403` sagt es ([die Liste](../getting-started/upgrading-to-0-11/#header-die-latet-entfernt)). Bei einer Anfrage, die
  Latet nur prüft, geht der Inhalt trotzdem Byte für Byte hinaus. Ein komprimierter Anfrageinhalt
  wird aus dem, was Latet gelesen hat, neu komprimiert und geht nie so hinaus, wie er kam, sodass ein
  Dateiname in seinem gzip-Header oder Bytes nach seinen komprimierten Daten den Anbieter nie
  erreichen; bei einer Anfrage, die Latet nur prüft, wird ein komprimierter Inhalt abgelehnt, mit
  einem Hinweis, ihn unkomprimiert zu senden. Ein Abfrageparameter, den kein Profil nennt, wird
  abgelehnt, und sein Name wird nur dem Agenten genannt. Eine Anfrage, die wegen eines Headers, eines
  Abfrageparameters oder ihres Pfads abgelehnt wird, steht mit dieser Kategorie im Verlauf, ohne
  Header; ein so abgelehnter WebSocket wird mit dem Code 1008 und einem Grund geschlossen, der
  dasselbe sagt. Header, die Sie für einen Agenten setzen (`ANTHROPIC_CUSTOM_HEADERS`, `http_headers` von
  Codex, `COPILOT_PROVIDER_HEADERS`), werden abgelehnt, bis eine Ausnahme sie nennt.
- **Jedes Element des Inhalts.** Ein Element, das kein Profil nennt, wird abgelehnt, mit der Angabe,
  wo es steht, nie mit seinem Namen; bei einer Anfrage, die Latet nur prüft (Claude Code, Bedrock,
  Vertex), geht ein neues Element mit Text durch, sobald es geprüft ist, angezeigt als *nur geprüft*.
  `client_metadata` von Codex wird entfernt, bevor die Anfrage maskiert wird. Modell-IDs gehen in den
  von ihren Anbietern dokumentierten Formen durch; ein Bedrock-ARN, ein Vertex-Projekt, der eigene
  Modellname eines Gateways oder eine `api-version` wird zuerst in `request_resources` aufgelistet.
  Bei OpenRouter geht ein Modell als `anthropic/`, `openai/` oder `google/` gefolgt von einem Modell
  dieses Anbieters durch, oder als `openrouter/auto`; das Modell eines anderen Anbieters wird dort
  ebenfalls zuerst aufgelistet.
- **Was der Anbieter zurückgegeben hat.** Thinking-Signaturen, verschlüsseltes Reasoning,
  Kompaktierungen und Ergebnisse von Websuchen gehen durch, wenn Latet sie in dieser Unterhaltung vom
  Anbieter kommen sah, auf jeder Instanz eines Servers; nach einem Neustart des Daemons eines Rechners
  lässt eine eingebaute Regel die verschlüsselten Felder des Anbieters selbst durch, nur in den Zügen
  des Modells und nur an die Adresse des Anbieters. Eine Unterhaltung über ein Unternehmens-Gateway,
  das Thinking-Signaturen weiterreicht, wird nach einem Neustart abgelehnt.
- **Googles eigene Tools und Code bei Gemini.** Der Code, den Google für das Modell ausführt, und
  was dieser Code ausgegeben hat, sowie die Eingaben und Ausgaben von Googles eingebauten Tools
  (Google Search, URL-Kontext, Maps, File Search) werden jetzt maskiert, wie der Aufruf einer
  Funktion und ihr Ergebnis; 0.10 ließ sie, wie sie geschrieben waren. Das Nachdenken des Modells
  bleibt weiterhin, wie das Modell es geschrieben hat.
- **Bilder und Dokumente.** Eine Anfrage mit einem Bild wird erst gesendet, wenn Latet seinen Text
  gelesen hat: Ohne Texterkennung oder mit `ocr: off` wird sie abgelehnt, mit einem Hinweis auf
  `latet ocr install`. `images: allow` sendet kein Bild mehr, das Latet nicht gelesen hat, und
  Dokumente, Audio und über ihre Adresse angegebene Bilder werden abgelehnt. Bei einer Anfrage, die
  Latet nur prüft, muss jedes Byte eines Bildes erfasst sein: Der Standort eines Fotos, die
  Herstellerangaben einer Kamera, eine Vorschau, ein zweites Bild und das Video eines Live-Fotos
  werden abgelehnt, mit dem Weg, sie zu entfernen, und ein ursprüngliches WebP-Bild wird abgelehnt;
  QR-Codes werden nicht entschlüsselt. Anfragen, die Latet umschreibt, senden die Bilder neu
  geschrieben, ohne Metadaten.
  Auf einem Server, dessen Texterkennung nicht verfügbar ist (sie konnte nicht geladen werden),
  zählt `latet_ocr_unread_total{reason="unavailable"}` jede abgelehnte Anfrage und jeden abgelehnten
  WebSocket-Zug einmal, gleich wie viele Bilder sie enthalten.
- **Ein Projekt, in dem Latet aus ist, sendet nichts.** Seine Anfragen werden abgelehnt, über HTTP und
  über einen WebSocket, mit einem Hinweis, wie man Latet dort einschaltet oder den Anbieter direkt
  erreicht. `latet project off` und der Schalter im Dashboard sagen das vorher.
  `latet serve --passthrough` gibt es nicht mehr: Die verborgene Option beendet sich mit einer
  Meldung.
- **Vertrauenswürdige und behauptete Projekte.** Ein Ordner, den eine Anfrage nennt
  (`x-latet-project`, der Arbeitsordner, den Codex, OpenCode und Gemini CLI schreiben), fügt nur
  Schutz hinzu: Die Lockerungen und Anfrageausnahmen seines Projekts gelten nicht, und ein
  behauptetes Projekt, das aus ist, wird abgelehnt. Ein Projekt ist vertrauenswürdig durch die
  Bindung des Plugins von Claude Code, die nach 30 Minuten ohne Aufruf endet, oder durch einen
  Startschlüssel. Die Aufrufe von Claude Code vor dem ersten Aufruf des Plugins werden nur mit Ihren
  eigenen Regeln entschieden.
- **Startschlüssel.** `latet claude`, `latet codex`, `latet opencode`, `latet gemini` und
  `latet copilot` geben dem Agenten einen geheimen Schlüssel dieses Starts, in seiner eigenen
  Header-Einstellung, den Latet prüft und entfernt; Codex bei seinen eingebauten Anbietern, Codex vor
  0.115, OpenCode vor 1.19.5, Gemini CLI im ACP-Modus, Copilot CLI außerhalb von Chat Completions
  und `latet run` geben keinen mit, und der
  startende Befehl sagt das in einem Satz. Ein gültiger Schlüssel in der Ausgabe eines Tools wird wie
  das Token von Latet maskiert. Das Dashboard zeigt für jede Anfrage, ob ihr Projekt vertrauenswürdig
  oder nur behauptet war, und ihren Start: *Von Latet gestartet, Schlüssel geprüft*, *Von Latet
  gestartet, Schlüssel fehlt*, *Vom Claude-Code-Plugin gebunden* oder *Nicht von Latet gestartet*;
  `latet doctor` nennt die Agenten, die ohne ihren Schlüssel gesehen wurden, und ein Codex, OpenCode
  oder Gemini CLI, das zu alt ist, um einen mitzugeben.
- **Anfrageausnahmen.** `request_exceptions` nennt genau, was es durchlässt (einen Header, einen
  Abfrageparameter oder ein Element des Inhalts, bei einem Ziel und einem Anfrageprofil), für jede
  Ihrer Anfragen oder, unter einer vertrauenswürdigen Bindung, für ein Projekt. Jede wartet auf Ihre
  Genehmigung, gilt standardmäßig 30 Tage und höchstens 90, mit einer Warnung sieben Tage vor ihrem
  Ende, oder wird mit Absicht dauerhaft gemacht, als solche markiert und von `latet doctor` und
  `latet server status` aufgelistet. Eine Profilrevision, die ändert, was eine nennt, setzt sie aus,
  bis Sie sie erneut genehmigen. In 0.11.0 gilt keine Ausnahme für einen WebSocket oder
  für ein Element des Inhalts in einem Element des Anbieters oder der Quelle eines Bildes, und keine
  nennt eine ganze Route: Das Dashboard bietet dort keine an. Bei ausgeschaltetem Verlauf werden die Anfragen, die jede durchgelassen hat, im
  Speicher gezählt und über die Instanzen eines Servers addiert. Ein Server führt in PostgreSQL einen
  Index der genehmigten Ausnahmen jeder Person, den `latet server status` liest: nur IDs, Arten des
  Geltungsbereichs, Enden und Zustände, nie das, was eine Ausnahme nennt, oder ihre Beschreibung.
- **Sichtbare Abdeckung.** Jeder Austausch im Dashboard zeigt, wie seine Anfrage abgedeckt wurde:
  blockiert, jeder Teil gelesen oder durch eine genannte Regel oder Ausnahme durchgelassen, mit den
  Headern und Elementen, die sein Profil entfernt hat, und seinem Anfrageprofil. Ein vor 0.11
  aufgezeichneter Austausch wird nie als geprüft gezeigt: Er sagt *Von einer älteren Version von Latet
  aufgezeichnet. Wie viel sie von dieser Anfrage gelesen hat, wurde nicht aufgezeichnet, daher wird
  sie nicht als geprüft angezeigt.* Ebenso wenig einer, der einen Teil ungelesen durchgelassen hat,
  oder eine Prüfung des Claude-Code-Plugins.
- **Was Logs behalten.** Logs, die Metadaten des Verlaufs und die Entscheidungsaufzeichnungen eines
  Servers behalten feste Kategorien und Routenvorlagen, nie den Namen eines Headers oder eines Tools,
  einen Wert oder den Pfad einer Anfrage, wie er gesendet wurde.
- `latet doctor`, und der erste Start des Daemons mit 0.11 in seinem Log, listen nach Art und Anzahl
  auf, was 0.11 anhält, das 0.10 gesendet hat: Projekte, in denen Latet aus ist, nicht installierte
  oder ausgeschaltete Texterkennung, `--passthrough` in einem Eintrag, der Latet startet, und die
  Aufrufe in Ihrem Verlauf, die kein Profil abdeckt.

- Die Projektordner in Ihren Einstellungen werden in kanonischer Form gespeichert: umgekehrte
  Schrägstriche und ein großer Laufwerksbuchstabe unter Windows, und kein abschließendes
  Trennzeichen (`C:/work/shop/` wird als `C:\work\shop` gespeichert). Die ID jedes Projekts bleibt
  unverändert, ebenso die Einstellungen, die dafür gelten.

- Der Tresor einer Person auf einem Server wird ganz im Speicher gehalten, wenn er beim Aufbau ihres
  Bereichs 35.000 Werte oder weniger hat (etwa 1,6 KB pro Wert auf jeder Instanz und jedem Worker,
  der die Person bedient, im Speicher des Bereichs gezählt): Die Texte der Person werden maskiert,
  ohne PostgreSQL nach ihren Werten zu fragen, etwa doppelt so schnell, wie sie nachzuschlagen. Über
  35.000 Werten hält der Bereich einer Person die Werte im Speicher, die die Person zuletzt verwendet
  hat (`key_working_values`), und findet jeden anderen in PostgreSQL, wie es der eines
  API-Schlüssels tut: Der Bereich einer Person mit 200.000 Werten belegt 24 MB, wo der von 0.10.0
  495 MB belegte. So oder so fragt jede Anfrage zuerst PostgreSQL, was die anderen Instanzen
  geändert haben, sodass ein auf einer Instanz behaltener oder vergessener Wert von der nächsten
  Anfrage auf jeder anderen gesehen wird ([Serverbetrieb](../concepts/server-operations/)).
- **Ein Server maskiert schneller.** Die Detektoren wenden auf jeden Text weniger Muster an, die Texte
  eines Aufrufs der API für Anwendungen werden zusammen gelesen, und eine Instanz findet die Werte
  eines API-Schlüssels oder einer Person über Fingerabdrücke der Wörter seines Tresors, die sie im
  Speicher hält (etwa 15 MB für eine Million Werte), und fragt PostgreSQL nur nach den Wörtern, die
  ein Wert oder ein Ersatzwert sein können. Gemessen auf einer Instanz auf dem Rechner der
  Dokumentation, maskiert ein API-Schlüssel mit 200.000 Werten 871 Logzeilen pro Sekunde und stellt
  2.960 wieder her, wo 0.10.0, auf dieselbe Weise gemessen, 350 maskierte und 2.114 wiederherstellte;
  einer mit 1.000.000 Werten 773 und 2.967, gegenüber 304 und 2.014; ein neuer Schlüssel maskiert
  191, gegenüber 153. Das Ziel von 1.000 Zeilen pro Sekunde bei diesen Größen wird nicht erreicht
  ([Wie schnell es ist](../applications/#wie-schnell-es-ist)).
- **Die Aktualisierung hält den Server einmal an.** 0.11 gibt jeder Wortzeile eines Tresors einen
  Platz für ihren Fingerabdruck und für die Zählung der Änderung, die sie zuletzt geschrieben hat:
  Halten Sie jede
  Instanz und jeden Worker an, führen Sie `latet server migrate` einmal aus und starten Sie sie mit
  0.11. Ein Tresor, den 0.10 gespeichert hat, erhält seine Fingerabdrücke nach dem Neustart, im
  Hintergrund, einmal, eine Instanz nach der anderen, während seine Anfragen und Aufrufe über die
  Suche in PostgreSQL beantwortet werden, langsamer, keiner abgewiesen; dann baut jede Instanz ihren
  Index daraus auf, in 1,5 Sekunden für 1.000.000 Werte
  ([Einen Server aktualisieren](../applications/#einen-server-aktualisieren)).
- `latet server migrate`, und `latet server init` auf Tabellen, für die eine Migration ansteht, ändern
  nichts, solange eine Instanz des Servers läuft, von 0.10 oder 0.11, oder Valkey nicht antwortet: Sie
  sagen, wie viele Instanzen laufen und mit welcher Version, und enden mit 2. Eine Instanz von 0.10
  zählt noch 30 Sekunden nach ihrem Stopp als laufend. Sind die Migrationen angewandt, fragen sie
  erneut, und wenn inzwischen eine Instanz zu laufen begonnen hat, sagen sie es und enden mit einem
  Fehler (`migrate` 1, `init` 2): Eine Instanz von 0.10, die auf den Tabellen von 0.11 weiterliefe,
  würde weiter Anfragen bedienen, ohne sie zu lesen, wie 0.11 es tut.
- `latet server status` listet eine noch laufende Instanz von Latet 0.10 neben denen von 0.11 auf,
  mit ihrer ID, als Latet 0.10 oder älter, und sagt, wenn seine Liste an ihrer Grenze abgeschnitten
  wurde (`instances: the list is partial, cut at its bound: an instance may be alive and not
  listed`, und `instances_whole` false in seiner Antwort).
- **Jeden Teil einer Anfrage zu lesen kostet Zeit.** Gemessen auf einer Instanz, im Median, gegenüber
  derselben Version ohne dieses Lesen: Eine kleine Chat-Anfrage braucht etwa 1 ms mehr auf einem
  Rechner (2,02 ms gegenüber 0,94) und auf einem Server (6,99 ms gegenüber 5,95); eine Anfrage mit
  4.000 Slots braucht auf einem Rechner das 3,8-Fache (362 ms gegenüber 96) und auf einem Server das
  1,4-Fache; eine Anfrage mit 500 bereits maskierten Texten und einem neuen auf einem Rechner das
  2,8-Fache (36 ms gegenüber 13) und auf einem Server das 1,16-Fache.
- `latet server run --workers N` (oder die Einstellung `workers`) führt N Instanzen des Servers auf einer
  Maschine aus, am selben Port, jede ein eigener Prozess, damit ein Server mehr seiner Kerne nutzt. Unter Linux
  lauscht jeder Worker mit einem eigenen Socket am Port, und der Kernel verteilt neue Verbindungen nach einem
  Hash der Adressen und Ports jeder Verbindung auf sie: Viele Verbindungen verteilen sich gleichmäßig, wenige
  langlebige, etwa die eines Lastverteilers, können trotzdem ungleich landen. Der Prozess, der die Worker
  startet, hält den Port die ganze Zeit, und nur Programme, die unter dem Benutzer des Servers laufen, können
  dort neben ihnen lauschen, daher führt dieser Benutzer nur Latet aus. Gemessen unter Linux über die API für
  Anwendungen maskieren acht Worker das 6,28-Fache dessen, was einer maskiert, und stellen das 5,21-Fache wieder
  her, und jeder belegt 185 bis 227 MB mit dem Bereich eines API-Schlüssels von 200.000 Werten. Mehrere Worker
  werden unter Windows abgelehnt, und unter macOS teilen sie sich einen Socket
  ([Serverbetrieb](../concepts/server-operations/), Mehrere Instanzen auf einer Maschine).
- Behoben: `latet upgrade` nannte in dem, was es installierte, nur die Namenserkennung und den Text in
  Bildern, und uv, das ein Tool mit genau den Extras installiert, die seine Anforderung nennt, nahm
  die anderen weg: Ein mit `latet[server]` installierter Server, den es aktualisierte, verlor psycopg
  und redis, und `latet server run` verweigerte dann den Start. Das Upgrade nennt jetzt jedes Extra,
  das das installierte Latet hat, wie uvs Aufzeichnung seiner Installation sie auflistet (wo diese
  Aufzeichnung fehlt oder sich nicht lesen lässt, jedes Extra, dessen Pakete alle installiert sind),
  neben der Namenserkennung und dem Text in Bildern; wo sich diese beiden nicht installieren lassen,
  bleiben die anderen Extras. `--to` und das eigene Fenster des Upgrades unter Windows behalten sie
  ebenfalls, und ebenso die Installationsskripte, erneut über ein mit anderen Extras installiertes
  Latet ausgeführt, wie uv sie auflistet (ein uv, das zu alt ist, um sie aufzulisten, liefert keine,
  wie bisher).

  Ein Upgrade führt den Code der installierten Version aus, daher nennt ein Latet bis 0.10.4 auf dem
  Weg zu dieser Version weiterhin nur die Namenserkennung und den Text in Bildern. Ein Server mit
  0.10.4 oder älter bekommt diese Version deshalb einmal auf anderem Weg: mit dem Befehl, mit dem er
  installiert wurde, der `server` und seine `UV_*`-Ordner nennt, oder mit dem erneut von GitHub
  geholten und mit diesen Variablen ausgeführten Installationsskript. Ein Server, dessen Upgrade
  `server` schon weggenommen hat, bekommt es mit diesem Befehl zurück
  ([Serverbetrieb](../concepts/server-operations/), Ein als Betriebssystemdienst installiertes Latet
  aktualisieren). Wo das Extra fehlt, gibt `latet server run` jetzt diesen Befehl mit den anderen
  Extras darin aus, und `latet upgrade` und die Fehlerbehebung sagen, welche Extras `LATET_SOURCE`
  für eine Installation von Hand nennt.
- Behoben: `latet upgrade` installierte in uvs eigenen Tool-Ordner, gleich in welchem Ordner das
  laufende Latet lag: Ein Server, der mit einem eigenen `UV_TOOL_DIR` installiert war, wie
  Serverbetrieb es beschreibt, wurde nie aktualisiert, und ein zweites Latet wurde im Home-Ordner des
  Kontos installiert und gestartet, das den Befehl ausführte. `latet upgrade` hält dort jetzt an,
  bevor sich etwas ändert, und gibt stattdessen den auszuführenden Befehl aus, mit den Ordnern, die es
  gefunden hat. Serverbetrieb hat einen neuen Abschnitt über das Aktualisieren eines als
  Betriebssystemdienst installierten Latet, und die Installation verweist einen Server darauf.

## 0.10.4 (2026-11-04)

- Behoben: Unter Windows konnte die lokale Namenserkennung aufhören, Namen zu finden, wenn Latet in
  einer als Paket installierten App lief, etwa in der Desktop-App von Claude. Windows legt die
  Dateien einer solchen App in einem eigenen Ordner der App ab
  (`...\Packages\Claude_...\LocalCache\Local\latet`) und zeigt sie am gewohnten Ort, sodass der
  Ordner des Namensmodells am gewohnten Ort zu finden sein konnte, während seine Dateien im Ordner
  der App lagen, auch wenn er über einen Link auf den gewohnten Ort erreicht wurde. ONNX Runtime ab
  Version 1.24 (eine Installation erhält jetzt 1.30.0) lehnte es dann ab, das Modell zu laden
  ("External data path escapes model directory"), und die Anfragen gingen mit Namen im Klartext
  weiter: Nur das Log des Daemons sagte, dass das lokale Namensmodell nicht geladen werden konnte.
  Latet öffnet das Modell jetzt an dem Ort, an dem seine Datei wirklich liegt, und Namen werden
  wieder gefunden. In allen anderen Fällen lädt das Modell wie bisher.

## 0.10.3 (2026-10-29)

- Behoben: Auf einem Latet-Server startete eine Speicherung der Einstellungen, die die Namen von
  Zielen prüft (aus dem Dashboard oder der Verwaltung), die Suche nach dem nächsten Namen, nachdem
  eine Suche die ganze Zeit der Speicherung verbraucht hatte, wenn die Systemuhr noch nicht
  weitergelaufen war (unter Windows gesehen). Jetzt startet sie keine: Diese Namen werden geprüft,
  wenn der Server sich verbindet, wie es schon bei Namen geschah, die nicht rechtzeitig antworten.
- Die Seite [Sicherheit](../security/) erklärt, wie Latet Anfragen zu Unterhaltungen gruppiert: die
  Header und Felder, die es liest, den Anfang des Verlaufs, den es verwendet, wenn es keine gibt,
  warum zwei Chats, die gleich beginnen, eine Unterhaltung teilen und gegenseitig ihre Werte
  wiederherstellen können, und dass jede Identität eines Servers ihren eigenen Tresor und ihre
  eigenen Unterhaltungen hat.
- Eine Version wird erst veröffentlicht, wenn die vollständige CI ihres genauen Commits auf `main`
  erfolgreich war: Der Release-Workflow prüft das, bevor er etwas baut, und hält sonst an. Die neue
  Seite [Latet veröffentlichen](../releasing/) beschreibt die Schritte, die Leitfäden für Beiträge
  verweisen darauf, und die Vorlage für Pull Requests fragt, welche Prüfungen gelaufen sind, wer die
  Änderung geprüft hat und wo die Belege sind.

## 0.10.2 (2026-10-28)

- Behoben: Ein geheimer Wert mit weniger als zwölf Zeichen (`password=1234`, `token=ab12f3`) erhielt
  einen Ersatzwert seiner eigenen Form, vier Ziffern oder sechs Hexadezimalziffern, den ein späterer
  Text zufällig in einer ID oder einer Zahl enthalten konnte. Das Unmask der Anwendungs-API, das jeden
  Ersatzwert des Tresors eines Schlüssels liest, setzte dann den geheimen Wert dort ein: Eine
  Logzeile, die ihn nie enthalten hatte, kam verändert zurück (`request_id=347bf309420a2290` als
  `347bf573020a2290`), und ihr Mask hielt den in einem Wort gefundenen Ersatzwert für einen eigenen
  Wert. Ein solcher geheimer Wert erhält jetzt einen längeren Ersatzwert derselben Arten, seine
  Zeichen an ihrem Platz: fünfzehn Ziffern für eine Zahl (mit Vorzeichen und Dezimalpunkt, damit eine
  JSON-Zahl eine Zahl bleibt), zwölf Hexadezimalziffern für einen Hexadezimalwert, sonst zwölf
  Buchstaben und Ziffern. Ein geheimer Wert mit zwölf Zeichen oder mehr und personenbezogene Daten
  behalten Ersatzwerte ihrer Form. Ein früher vergebener kurzer Ersatzwert (acht Zeichen oder weniger)
  bleibt, wie jeder Ersatzwert. Eine Unterhaltung, auf einem Rechner oder einem Server, setzt ihn wie
  bisher wieder ein, auch innerhalb eines Wortes, da sie nur die Ersatzwerte wiederherstellt, die ihr
  gezeigt wurden. Das Unmask der Anwendungs-API setzt ihn nur dort wieder ein, wo er als ganzes Wort
  steht, ohne Buchstaben, Ziffer oder Unterstrich daneben: Wo der Wert eines solchen Geheimnisses
  innerhalb eines längeren Wortes oder einer längeren Zahl maskiert wurde (`postgres` in
  `postgresql://`, `1234` in `order_id=9912343`), lässt Unmask seinen Ersatzwert stehen. Ein
  Ersatzwert mit 9 bis 11 Zeichen wird überall wie bisher wieder eingesetzt. Eine eigene
  Erkennungsregel der Klasse `secret` behält den Ersatzwert, den ihre Regel gibt, in der Form des
  Werts; einer mit acht Zeichen oder weniger wird wieder eingesetzt, wie ein früher vergebener kurzer
  Ersatzwert. Aktualisieren Sie jede Instanz eines Servers: Eine, die noch 0.10.0 ausführt, gibt neuen
  kurzen Geheimnissen kurze Ersatzwerte, und auch diese bleiben.
- Behoben: Eine Anfrage, die Latet ablehnt, bevor es sie maskiert (ihre Maskierung schlägt fehl oder,
  auf einem Server, ihr Inhalt würde über `max_request_mb` hinaus dekodiert), erschien im Verlauf als
  ungelesen gesendet, mit einem Fehler, obwohl nichts gesendet worden war. Sie erscheint jetzt als
  blockiert und nicht gesendet, mit ihrem Grund in den Worten von Latet und der Größe ihres Inhalts,
  und auf einem Server zählt die Metrik der Stopps (`latet_agent_stops_total`) sie.
- Behoben: Ein Text, der maskiert wurde, bevor der Tresor einen Wert um einen bereits darin maskierten
  Wert herum gelernt hatte, ließ den Rest dieses Werts im Klartext, wenn er erneut maskiert wurde. Eine
  IBAN, die auf beiden Seiten an Buchstaben hing (`MarioIT60X0542811101000000123456Ferri`) und einen
  dem Tresor bekannten geheimen Wert enthielt, hatte nur diesen geheimen Wert maskiert; sobald ein
  späterer Text dem Tresor die ganze IBAN beigebracht hatte, zeigte derselbe Text, erneut so maskiert,
  wie er vorher maskiert worden war (durch einen späteren Aufruf der Anwendungen, in einer neuen
  Unterhaltung oder innerhalb desselben Aufrufs oder derselben Anfrage), noch die übrigen Zeichen der
  IBAN. Ein ganz gelernter Name (`Mario Rossi`) um einen früher maskierten Nachnamen (`Rossi`) ließ
  `Mario` auf dieselbe Weise im Klartext. Die abschließende Prüfung stoppte das nicht, da kein ganzer
  Wert im Klartext stand. Ein solcher Text wird jetzt erneut maskiert, ganz. Auch der Text einer
  Unterhaltung, der nie hinausging (seine Anfrage blockiert oder aufgegeben), wird neu maskiert; einer,
  der hinausging, wird wieder so gesendet, wie er hinausging, wie bisher.
