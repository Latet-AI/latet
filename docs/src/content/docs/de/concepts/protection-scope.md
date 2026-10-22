---
title: Was geschützt ist und was nicht
description: Die Grenzen von Latet. Lesen Sie dies, bevor Sie sich darauf verlassen.
---

Latet verringert, was Ihr Agent dem Anbieter des Modells preisgibt. Es ist keine Garantie dafür,
dass nie etwas Sensibles Ihren Rechner verlässt. Diese Seite listet auf, was abgedeckt ist und was
nicht.

## Geschützt

Wenn ein [Detektor](../detection/) einen Wert erkennt, ersetzt Latet ihn durch einen Ersatzwert,
bevor die geschützte Anfrage den Modellanbieter erreicht. Bei einem Server können die
Originaldaten zuerst diesen Server erreichen. Das umfasst:

- **In Claude Code (Client-Modus):** die Prompts, die Sie eingeben, den System-Prompt,
  `CLAUDE.md`-Dateien und den übrigen Kontext, den Claude Code hinzufügt, Anhänge, Skill-Prompts,
  Tool-Beschreibungen und jedes Tool-Ergebnis: Dateiinhalte, Befehlsausgaben, Hook-Ausgaben. Eine
  abschließende Prüfung liest dann die Texte jeder Anfrage, bevor sie gesendet wird. Was das Modell
  geschrieben hat, stoppt nichts, wenn es genau so zurückkommt, wie das Modell es geschrieben hat, was
  Latet aus den Antworten weiß, die es weitergegeben hat, und aus den Tool-Aufrufen, die das Plugin
  hat wiederherstellen lassen; jeder andere Text an der Stelle des Modells wird wie Ihrer gelesen.
  Auf einem Rechner hält der Daemon das im Speicher: Nach einem Neustart wird ein Text, den das
  Modell früher in der Unterhaltung geschrieben hat, wie Ihrer gelesen, und einer, der einen Wert
  enthält, den Latet maskiert, stoppt die Anfrage; beginnen Sie dann eine neue Unterhaltung. Ein
  Latet-Server bewahrt es für jede Instanz auf, eine Stunde lang nach der letzten Verwendung.
- **Im Gateway-Modus:** den System-Prompt oder die Anweisungen und jede Nachricht der Unterhaltung:
  Ihren Text, die früheren Antworten des Modells, Tool-Aufrufe und Tool-Ergebnisse. Auch die
  Eingaben für Embeddings. Eine abschließende Prüfung liest dann diese Texte, bevor die Anfrage
  gesendet wird, wie im Client-Modus.

Gateway-Antworten, Streams und Tool-Argumente werden dort wiederhergestellt, wo Latet läuft:
lokal auf dem Rechner, bei einem verbundenen Gateway auf dem Server. Das Claude-Code-Plugin
stellt Tool-Argumente und interaktiven Bildschirmtext auf dem Rechner wieder her;
stdout von `claude -p` enthält weiterhin Ersatzwerte. OCR/NER laufen dort, wo die zuständige
Latet-Engine läuft; ein konfigurierter Prüfer kann Originaltext erhalten.

Die Schlüssel eines JSON-Dokuments, etwa einer Tool-Ausgabe, werden wie seine Werte maskiert: Eine
E-Mail-Adresse, eine Telefonnummer, ein geheimer Wert, ein Name oder einer Ihrer Begriffe, der als
Schlüssel geschrieben ist, wird durch den Ersatzwert ersetzt, den er überall hat, und ein Schlüssel,
in den das Modell einen Ersatzwert schreibt, wird für den Agenten wieder eingesetzt. Eine Feldregel
und der strikte Datenmodus finden einen Wert weiterhin über seinen Schlüssel, wie er ankam, und ein
Schlüssel, der nur ein Feld benennt, etwa `id` oder `email`, bleibt unverändert.

Ist der [lokale Prüfer](../llm-review/) eingeschaltet, liest außerdem ein Sprachmodell auf Ihrem
Rechner diese Texte, bevor sie hinausgehen, und maskiert die Namen, Gesundheitsangaben und anderen
Werte, die kein Detektor findet, auch in den Schlüsseln eines JSON-Dokuments. Ein Schlüssel, der wie
der Name eines Feldes geschrieben ist, in Kleinbuchstaben, Ziffern, `_` und `-` (`exit_code`, aber
auch `mario_rossi`), wird ihm nicht gezeigt: Die Detektoren lesen ihn, wie sie jeden Schlüssel lesen.

## Nicht geschützt

**Werte, die die Detektoren nicht erkennen.** Latet maskiert nur, was es erkennt. Ein geheimer Wert
in einem ungewöhnlichen Format, über mehrere Zeilen verteilt oder kodiert (etwa in Base64 unter
einem gewöhnlichen Namen), kann durchgehen. Namen und Anschriften in Freitext werden nur mit dem
optionalen [lokalen Namensmodell](../../getting-started/installation/) oder dem
[lokalen Prüfer](../llm-review/) gefunden, beide ohne Gewähr; ohne sie werden nur der Name Ihres
eigenen Git-Autors, Ihre Begriffe und Werte, die schon im Tresor stehen, maskiert. Geburtsdaten
werden nicht erkannt. Bei der Erkennung gibt es immer falsch negative Ergebnisse.

**Bilder.** Mit der optionalen [Texterkennung](../images/) wird der Text in einem Screenshot auf
Ihrem Rechner gelesen, und jeder gefundene Wert wird mit seinem Ersatzwert überdeckt. Das geschieht
ohne Gewähr, wie bei Namen: Text, den sie nicht liest, geht unverändert hinaus. Ohne sie werden
Bilder unverändert gesendet und als *Nicht gelesen* markiert.

**PDFs und andere Dokumente, per Adresse eingebundene Bilder, Audioclips.** Ihr Inhalt wird nicht
gelesen und unverändert gesendet, auch von einem Latet-Server; das Dashboard markiert diese Anfragen
als *Nicht gelesen*. Hängen Sie keine Dokumente an, die sensible Daten enthalten, oder setzen Sie
`images: block` (das Profil `max` tut dasselbe): Eine Anfrage, die ein solches enthält, wird dann
nicht gesendet. Die Seiten eines Browsers, als Text gelesen, werden wie jede Tool-Ausgabe maskiert
([Produktionsdaten](../production-data/)); seine Screenshots sind Bilder.

**Web-Tools, Subagenten und claude.ai-Connectors behalten die Ersatzwerte (Claude Code).** WebFetch,
WebSearch, Subagenten und claude.ai-Connectors (Gmail, Calendar...) erhalten die Ersatzwerte, die das
Modell geschrieben hat, nicht die Originale, denn ihre Argumente verlassen Ihren Rechner oder
starten eine weitere Unterhaltung mit einem Modell. Eine Websuche nach einem maskierten Namen würde
nach jemand anderem suchen, und ein Connector, der eine E-Mail an `emily.carter@northwind-legal.com`
schicken soll, bekäme die erfundene Adresse. Deshalb wird ein Aufruf eines Web-Tools oder
Connectors, der einen Ersatzwert enthält, gestoppt, bevor er läuft: Das Modell fragt Sie, und
*Nicht sensibel* im Dashboard lässt den Wert im Klartext hinaus. Die Ergebnisse von
claude.ai-Connectors kommen über die Server von Anthropic zurück, wo der Connector läuft: Was immer
sie enthalten, ist schon dort, bevor Latet es maskieren kann.

**Tools, die der Anbieter ausführt (Agenten im Gateway-Modus).** Eine Websuche oder ein entfernter
MCP-Server, den der Anbieter selbst ausführt, wie es bei Codex möglich ist, arbeitet mit dem, was
das Modell geschrieben hat: dem Ersatzwert. Latet sieht einen solchen Aufruf erst, nachdem er
gelaufen ist, im Verlauf der nächsten Anfrage: Das Dashboard markiert ihn
(*Ersatzwert an externes Tool*), mit *Nicht sensibel* für jeden Wert, damit das Modell beim
nächsten Mal den echten verwendet.

**Umgebungen, die Latet nicht sieht:**

- Chat und Cowork in Claude Desktop, mit Anmeldung bei Claude;
- Cloud-Sitzungen, die auf der Infrastruktur des Anbieters laufen statt auf Ihrem Rechner;
- Cursor, Windsurf und die anderen Tools, deren Anfragen an das Modell von den Servern des
  Herstellers gesendet werden ([die Liste](../../agents/other-tools/#was-latet-nicht-schützen-kann));
- die Inline-Vorschläge von GitHub Copilot und die Edit Predictions von Zed, die von den eigenen
  Diensten der Hersteller kommen;
- OpenCode mit Anmeldung über ChatGPT (auf der [Roadmap](../../roadmap/));
- was die eigenen Tools eines Agenten beim Ausführen senden, etwa eine Websuche-API, eine Datenbank
  oder ein Vektorspeicher in der Cloud: Sie bekommen die echten Werte (siehe
  [Agent-Frameworks](../../agents/openai-compatible/#agent-frameworks));
- jeder Agent und jede Umgebung, die Sie nicht für Latet eingerichtet haben.

Mit Anmeldung bei Claude maskiert das Plugin die Inhalte im Code-Tab von Claude Desktop, aber die
App ignoriert `ANTHROPIC_BASE_URL`: Hinter dem Plugin gibt es keine Prüfung durch den Daemon und
keine Aufzeichnung im Dashboard. Über das Gateway eines Drittanbieters, ohne Anmeldung bei Claude,
senden Chat, Cowork und Code jede Anfrage über Latet: Latet prüft jede und stoppt eine, die einen
sensiblen Wert im Klartext enthalten würde, maskiert aber nur im Code-Tab, mit dem Plugin. Siehe
[Claude Code](../../agents/claude-code/#abdeckung) und
[Claude Desktop über ein Gateway](../../agents/other-tools/#claude-desktop-über-ein-gateway).

**Kontext und Metadaten.** Dateinamen, Pfade, Codestruktur, Kommentare und Geschäftslogik werden
unverändert gesendet, abgesehen von den Werten, die Latet darin erkennt, etwa Ihrem Benutzernamen in
einem Pfad des Home-Verzeichnisses. Ersatzwerte behalten die Form des Originals (Länge, Format,
Präfix), und der umgebende Text kann verraten, was ein Wert ist ("das Passwort der
Produktionsdatenbank").

**Was ein JSON-Dokument dort enthält, wo das Maskieren nicht hinreicht.** Eine Kartennummer, die als
JSON-Zahl geschrieben ist und die das Profil `balanced` nicht maskiert, wird von der abschließenden
Prüfung gefunden, die die Anfrage dann stoppt: Der Agent erfährt den Grund, und das Dashboard zeigt,
wo der Wert steht. Solange eine solche Ausgabe in der Unterhaltung steht, wird jede ihrer Anfragen
gestoppt: Beginnen Sie eine neue Unterhaltung, und bitten Sie den Agenten um die Daten in einer
anderen Form. Der für einen Schlüssel gezogene Ersatzwert ist nie ein anderer Schlüssel seines
Objekts; ein Tool-Aufruf, dessen Eingabe einen Wert und seinen Ersatzwert als zwei Schlüssel enthält,
die nach dem Maskieren einer wären, stoppt die Anfrage, denn ein Objekt enthält jeden Schlüssel nur
einmal, und zwei solche Schlüssel bleiben, wie das Modell sie geschrieben hat, wenn Latet seine
Antwort für den Agenten wiederherstellt. Ein JSON-Objekt, das einen Schlüssel zweimal angibt, wird
mit seiner letzten Kopie gelesen und geht neu geschrieben mit dieser Kopie allein hinaus, sodass die
Kopie, die Latet nicht gelesen hat, nie hinausgeht; eine Anfrage, die Latet nur prüft, etwa von
Claude Code, wird gestoppt, wenn ihr eigener Body einen Schlüssel zweimal angibt.

**Tool-Aufrufe mit Schlüsselkonflikten im Verlauf.** Beide Schlüssel in einem wiederhergestellten
Aufruf zu behalten bewahrt seine Daten, doch erneutes Maskieren kann die Schlüssel gleich machen.
Das ist eine dokumentierte Einschränkung: Die nächste Anfrage und jede weitere mit diesem Aufruf
werden abgelehnt. Beginnen Sie eine neue Unterhaltung, oder entfernen beziehungsweise ändern Sie den
Aufruf über die unterstützte Verlaufsverwaltung Ihres Clients. Ein unveränderter erneuter Versuch
setzt die Unterhaltung nicht fort.

**Tool-Argumente im Stream.** Latet hält JSON-Argumente bis zum Abschluss des Aufrufs zurück und
stellt dann das ganze Dokument wieder her, ohne verschiedene Schlüssel zusammenzuführen. Normaler
Text wird weiterhin fortlaufend gesendet. Zurückgehaltene JSON-Kanäle teilen sich ein begrenztes
Antwortbudget, einschließlich Metadaten sowie Wachstum bei Rekonstruktion und Serialisierung.
Eine Überschreitung oder unvollständige beziehungsweise ungültige Argumente beenden den Stream mit
einem Fehler. Ein Anthropic-Aufruf mit nicht leerer Anfangseingabe darf keine zweite nicht leere
JSON-Argumentquelle liefern. Ein vor Abschluss unterbrochener Aufruf wird nicht als vollständige
Modellausgabe gespeichert.

**Angenommene Aufzeichnungen der Modellausgabe.** Nach der Prüfung eines Antwortpakets nimmt Latet
die Aufzeichnungen der Modellausgabe an und reiht ihre Speicherung vor der Rückgabe dieser Ausgabe
ein. Die Speicherung erfolgt im Hintergrund: Die Annahme belegt weder die Zustellung an den Client
noch die bereits erfolgte Speicherung. Ein Abbruch vor der Annahme veröffentlicht keine
Aufzeichnungen; nach der Annahme kann ihre Speicherung auch bei abgebrochener Zustellung fertig werden.

Innerhalb einer Instanz lässt das Gateway jeder Identität über deren Sitzungen hinweg höchstens
32 MiB solcher Aufzeichnungen und 256 Pakete zu, einschließlich wartender und laufender Arbeit.
Maskierter und wiederhergestellter
Text, Antwortkennungen und der Verwaltungsaufwand der Aufzeichnungen zählen mit. Das begrenzt die neu
zurückgehaltene Arbeit, nicht den gesamten Prozessspeicher (RSS); länger vorgehaltene Klartextdaten
und der Interpreter benötigen weiterhin Speicher. Ist die Annahme voll oder geschlossen oder
schlägt das Einreihen fehl, wird die Ausgabe dieses Pakets abgelehnt. Bereits gestreamter Text
lässt sich nicht zurücknehmen.

Die Aufzeichnungen werden erst später sichtbar. Danach beim selben Gateway-Worker eingereihte
Anfragen folgen dessen Speicherungsversuch. Auf einem Latet-Server garantiert das weder, dass
Valkey sie geschrieben hat, noch dass eine andere Instanz sie sieht, noch ihre Reihenfolge
gegenüber der Arbeit des Plugins für Claude Code. Beim Herunterfahren können wartende Aufzeichnungen verloren gehen. Ein Speicherfehler
nach der Zustellung erzeugt eine in ihrer Häufigkeit begrenzte Diagnose nur mit einer festen
Kategorie und dem Ausnahmetyp; er kann weder die Zustellung zurücknehmen noch eine Transaktion
zurückrollen. Bei fehlenden Aufzeichnungen gelten weiterhin die üblichen Regeln für Sitzungssuche,
Maskierung und Ablehnung.

**Felder der Anfrage außerhalb der Unterhaltung.** Im Gateway-Modus werden die Beschreibungen der
Tools, die Schemas ihrer Parameter und die Beispiele ihrer Eingabe wie die Unterhaltung maskiert,
ebenso das Schema einer strukturierten Ausgabe, eine vorhergesagte Ausgabe und das, was eine Anfrage
darüber sagt, für wen sie ist (`user`, `safety_identifier`, `metadata`). Die Namen der Tools, das,
womit der Anbieter ein eigenes Tool ausführt (Adresse und Header eines entfernten MCP-Servers, der
Standort einer Websuche), und die übrigen Einstellungen der Anfrage werden unverändert
weitergeleitet. In Claude Code maskiert das Plugin die Beschreibungen der Tools, und die
abschließende Prüfung liest `metadata` und das Schema einer strukturierten Ausgabe; von einem Rechner
werden die Schemas der Tool-Parameter unverändert gesendet, und ein Latet-Server prüft sie (siehe
unten). Ein anderer Client als Claude Code auf einem Weg, den Latet nur prüft (die Anmeldung eines
Abonnements, Bedrock, Vertex), hat kein Plugin: Die abschließende Prüfung liest auch die
Beschreibungen, Parameter und Beispiele seiner Tools und stoppt eine Anfrage, die darin einen Wert im
Klartext hinauslassen würde.

**Die Tools von Claude Code auf einem Server.** Das Plugin maskiert die Beschreibungen der Tools, auch
die von `tool_addition`. Ein Latet-Server nimmt die Marker der Anfrage nicht als Beleg dafür, dass das
Plugin ausgeführt wurde: Seine abschließende Prüfung liest die Beschreibung jedes Tools, das Schema
seiner Parameter und die Beispiele seiner Eingabe, wie bei einem Client ohne Plugin, und stoppt eine
Anfrage, die darin einen Wert im Klartext hinauslassen würde. Ein MCP-Tool, dessen Schema die Adresse
eines Kunden nennt, stoppt deshalb die Anfragen von Claude Code auf einem Server, bis der Wert aus dem
Schema entfernt oder in Ihren Einstellungen erlaubt ist. Eine Tool-Definition in einer Form, die Latet
nicht liest, wird abgelehnt, wie bei jedem Client. Die hier beschriebenen Prüfungen von Metadaten,
strukturierten Ausgaben und Fallback-Schemas gelten weiterhin.

Bei Anthropic umfasst die Prüfung strukturierter Ausgaben auch Beschreibungen und die Schlüssel
und Werte der Schemas in jedem `fallbacks[].output_config.format`. Beim Umschreiben werden sensible
Schemas maskiert; bei nur geprüften Anfragen werden sie abgelehnt, sichere Bytes bleiben unverändert.
Fallbacks mit nur einem Modell bleiben unterstützt. Ein Server mit aktivem Schutz lehnt fehlerhafte
Fallback-Container und Ausgabeschemas ab.

**Was Latet nicht liest, auf einem Latet-Server.** Ein Rechner sendet unverändert, was Latet nicht
liest; ein Latet-Server lehnt es ab, mit einem Fehler, der den Grund nennt. Er lehnt eine Anfrage
ab, deren Body Latet nicht liest: an einen anderen Endpunkt als den Aufruf eines Modells, etwa files
oder batches, oder mit einer anderen Methode als POST. Er lehnt Inhalte in einer Form ab, die Latet
nicht liest: einen Completions-Prompt aus Tokens; eine Prompt-Variable, eine Tool-Definition oder
das Format einer Antwort einer Art, die Latet nicht kennt; einen Teil des Inhalts, einen Block einer
Nachricht oder ein Element der Eingabe einer Art, die Latet nicht kennt, in Chat Completions, den
Nachrichten von Anthropic und der Responses API gleichermaßen, oder eines, das kein Objekt ist; ein
Feld, das Latet liest, in einer anderen Form, etwa die Eingabe eines Tool-Aufrufs, die kein Objekt
ist, oder einen System-Prompt, der weder Text noch Blöcke ist; und in der Responses API das Audio
einer Live-Unterhaltung (`audio`, `output_audio`) und das, was in einem Audioclip gehört wurde. Er
lehnt einen WebSocket-Frame ab, der keine Runde ist (`response.create`; `response.cancel` geht
durch), und bei einer Anfrage, die er nur prüft (Claude Code, Bedrock, Vertex), ein Bild, dessen
Text er nicht lesen konnte, was immer `images` sagt. Was der Anbieter erzeugt hat und ihm so
zurückgegeben wird, wie er es erzeugt hat, geht durch: das Reasoning des Modells, eine
Kompaktierung, die Aufrufe der eigenen Tools des Anbieters und ihre Ergebnisse. Ebenso geht durch,
was nichts enthält, etwa ein Verweis auf ein früheres Element, und eine Anfrage ohne Body, etwa die
Liste der Modelle.

Ein Server mit aktivem Schutz lehnt außerdem Chat-Nachrichten mit `name`, Anthropic-Blöcke vom Typ
`search_result` und Dokumentblöcke mit `title` oder `context` ab, auch innerhalb von Tool-Ergebnissen
und bei `images: allow`. Diese optionalen Formen werden auf dem Server auch mit leeren Werten oder
null nicht unterstützt. Ein Dokument ohne Titel oder Kontext folgt der Einstellung `images`, wie
überall. Ein Rechner lehnt diese Formen nicht ab und maskiert sie auch nicht: Ihr Text geht so
hinaus, wie er kam. Ein Suchergebnis und ein Dokument werden als *Nicht gelesen* markiert; das
`name` einer Chat-Nachricht wird weder maskiert noch markiert, halten Sie die Werte, die Sie
schützen, also aus ihm heraus.

**Die Prüfung im Auto-Modus von Claude Code.** Der Prüfer des Auto-Modus erhält Ihren Benutzernamen
im Betriebssystem und die Pfade Ihres Home- und Arbeitsverzeichnisses, dazu jeden Hostnamen oder
Git-Autor in Ihrer CLAUDE.md oder in der Aktion, und kein Plugin kann sie maskieren. Standardmäßig
stoppt der Daemon diese Prüfungen, sodass die Angaben lokal bleiben und der Auto-Modus Aktionen
nicht selbst genehmigen kann. `auto_mode_review` lässt jede Angabe zum Prüfer durch, und nur zu ihm,
oder gibt dem Prüfer einen Ersatznamen; siehe [Claude Code](../../agents/claude-code/).

**Ihr eigener Rechner.** Latet ist keine Sandbox: Es schützt nicht vor Malware, anderen Benutzern
Ihres Kontos oder einem kompromittierten Agenten. Die Tools, die der Agent auf Ihrem Rechner
ausführt, lokale MCP-Server eingeschlossen, erhalten die echten Werte, damit Dateien und Befehle
funktionieren: Ein Befehl, der Daten über das Netzwerk sendet, sendet den echten Wert. Führen Sie
den Agenten in einer Sandbox aus, wenn er eine braucht.

**Regeln, die mitten in einer Unterhaltung hinzukommen (Claude Code).** Angenommen, Sie fügen
`Project Falcon` mitten in einer Unterhaltung als eigenen Begriff hinzu. Neue Inhalte werden ab dann
maskiert, aber die früheren Runden, die Anthropic schon mit dem Namen im Klartext erreicht haben,
werden mit jeder Anfrage erneut gesendet. Latet markiert sie im Dashboard, statt sie zu blockieren,
denn sie wurden bereits gesendet, und Blockieren würde die Unterhaltung anhalten. Beginnen Sie eine
neue Unterhaltung, um sie hinter sich zu lassen.

**Früher gesendete Daten.** Alles, was gesendet wurde, bevor Latet installiert war oder während es
ausgeschaltet war, liegt bereits beim Anbieter.

Die Überlegungen hinter diesen Grenzen stehen unter [Sicherheit und Bedrohungsmodell](../../security/).
