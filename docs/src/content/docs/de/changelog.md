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
Auto-Modus, ein signiertes Installationsprogramm für Windows, eigene Namensmodelle, weitere Agenten
und die Dokumentation in weiteren Sprachen.

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
