---
title: Latet veröffentlichen
description: Eine Version erst veröffentlichen, wenn der vollständige CI-Workflow für den genauen main-Commit erfolgreich war.
---

Dieses Verfahren gilt für Latets `v*`-Tags und `.github/workflows/release.yml`. Pro Version bleibt es bei
einem öffentlichen Commit mit Code, Tests, Versionsnummern und Dokumentation in allen fünf Sprachen.

## Zusammenführen, CI abwarten, dann taggen

1. Bereite die Version als einen öffentlichen Commit vor und lasse sie prüfen. Halte die Versionsnummern
   des Pakets und des Claude-Plugins gleich; der Release-Build vergleicht sie mit dem Tag.
2. Führe diesen Commit in `main` zusammen. Halte seinen vollständigen SHA fest.
3. Warte, bis der **vollständige CI-Workflow** (`.github/workflows/ci.yml`), ausgelöst durch diesen Push
   auf `main`, für genau diesen SHA **erfolgreich** beendet ist. Prüfe das Workflow-Ergebnis einschließlich
   aller Matrix-Jobs und Servertests. Ein erfolgreicher Pull Request oder einzelner Job genügt nicht.
4. Erstelle das Versions-Tag genau auf diesem Commit und pushe es erst nach Freigabe der Veröffentlichung.
   Leichte und annotierte Tags werden unterstützt; bei annotierten Tags wird der zugrunde liegende Commit geprüft.
5. Prüfe das Ergebnis des Release-Workflows. Bewahre den Tag-SHA und die URL des erfolgreichen main-CI-Laufs
   im Prüfprotokoll auf. Die Job-Zusammenfassung hält den akzeptierten Lauf und Versuch fest.

Ändert sich der Commit, prüfe vor dem Taggen die main-CI für den neuen SHA erneut. Behalte den einzelnen
öffentlichen Versions-Commit bei; füge keinen öffentlichen Commit nur zum Festhalten erfolgreicher Prüfungen hinzu.

## Anforderungen der Release-Prüfung

Vor Builds, Attestierungen oder Veröffentlichung ermittelt die Prüfung die tatsächliche CI-Workflow-ID aus der
Datei, prüft die Zugehörigkeit des Tag-Commits zu `main` und liest dessen `push`-Läufe auf `main`.
Sie wählt den neuesten Lauf und fragt dessen aktuellen Versuch ab. Nur `completed` mit `success` wird akzeptiert.
Die nachfolgenden Jobs verwenden den geprüften Commit-SHA und hängen von dieser Prüfung ab.

Fehlende, eingereihte, laufende, wartende, fehlgeschlagene, abgebrochene, übersprungene oder neutrale Ergebnisse
blockieren die Veröffentlichung. API- oder Authentifizierungsfehler, widersprüchliche Antworten und unvollständige
Paginierung blockieren sie ebenfalls. Listen an GitHubs Grenze von 1.000 Ergebnissen für gefilterte Suchen werden
abgelehnt. Die Prüfung wartet nicht und wiederholt sich nicht bis zu einem günstigen Ergebnis. Untersuche den
Fehler, warte auf den Erfolg der vollständigen CI für genau diesen main-Commit und starte danach den Release
mit Freigabe ausdrücklich erneut. Verschiebe kein veröffentlichtes Tag, um einen Fehler zu umgehen.

Das Ergebnis gilt zum Prüfzeitpunkt; ein späterer CI-Neustart kann den Status des festgehaltenen Laufs ändern.
Eine grüne Prüfung bestätigt erfolgreiche CI, nicht den Ausschluss aller denkbaren Fehler.

## Ein Prüfbericht für jede Version

Verlinke aus den Release-Notizen eine kurze englische Prüfzusammenfassung mit einer versionsbezogenen
Markdown-Datei und gemeinsamen Manifesten für Quellstand und Nachweise als Anhänge außerhalb von Git.
Stelle den Bericht nach Erstellung des öffentlichen Commits und erfolgreicher vollständiger main-CI
fertig; hänge die Dateien nach autorisiertem Tag und Release manuell an und ergänze die Links in den
Release-Notizen. Der aktuelle Release-Workflow erzeugt diese Prüfdateien nicht. Behalte einen
öffentlichen Commit pro Version bei: Der externe Bericht kann dessen genauen SHA nennen, ohne ihn
in eine Datei aufzunehmen, die diesen SHA bestimmt.

Nimm Folgendes auf:

- Version, vollständigen öffentlichen Commit-SHA und den dem Tag zugrunde liegenden Commit-SHA;
  getrennt davon den tatsächlich getesteten privaten Quellstand (Commit oder Basis mit aufbewahrtem
  Patch/Manifest). Ein Manifest nennt Pfade und Inhalts-Hashes von geprüftem Code, Tests, Test-Harness
  und Konfiguration. Alle Unterschiede zum öffentlichen Quellstand, Ausschlüsse und nötigen erneuten
  Prüfungen festhalten; ein privates Testergebnis gilt dadurch nicht automatisch für den öffentlichen SHA.
  Inventare kanonischer Git-Blobs und tatsächlich ausgeführter Bytes für saubere wie geänderte
  Checkouts getrennt halten; EOL-/Filter-Umwandlungen und Unterschiede erfassen, ohne die Hashes
  der ausgeführten Bytes zu normalisieren.
- Tatsächliche Prüfer oder identifizierbare Rollen und Berichtsverweise, Prüfumfang, Prüfart und die
  Angabe, ob eine unabhängige menschliche Prüfung erfolgte. Ein separater automatisierter Agent ist
  kein menschlicher Prüfer. Geschlossene Befunde mit Nachprüfungsbelegen sowie offene Befunde mit
  Schweregrad und verbleibenden Arbeiten auflisten.
- Befehle der Testsuiten, Umgebung, Ergebnisse und übersprungene Tests; Fuzz-Konfigurations-IDs,
  Harness/Quellstand, Seeds oder Korpus, Soll-/Ist-Zahlen und abgeschlossene/geplante Konfigurationen.
  Fehlgeschlagene, unvollständige und nicht ausgeführte Arbeiten ausdrücklich kennzeichnen.
  Zählkampagnen belegen keine Leistungsmessungen.
- Genaues CI-Repository, Workflow-Pfad, Ereignis/Ref, Commit-SHA, Lauf-ID, Versuchs-URL, Status/Ergebnis
  des gesamten Workflows und Beobachtungszeitpunkt. Die finale `push`/`main`-CI vor dem Taggen separat
  prüfen; PR-Prüfungen, lokale Ergebnisse und erfolgreiche Veröffentlichung ersetzen sie nicht.
- Grenzen der Tests: simulierte Upstreams oder direkte Space-Runtime-Tests gegenüber authentifizierten
  End-to-End-Tests, ausgelassene Konfigurationen und nicht verfügbare Nachweise. Private Logs sind nicht
  öffentlich prüfbar; nach Möglichkeit einen unbedenklichen öffentlichen Auszug bereitstellen und
  angeben, was Leser nicht selbst prüfen können.

Zusammenfassung und Manifeste außerhalb des Commits halten, den sie identifizieren. Nachweisdateien mit
Prüfsummen und Revisionskennungen versehen, frühere Revisionen aufbewahren und Korrekturen erläutern.
Ein Link oder eine Prüfsumme allein belegt weder unabhängige Prüfung noch unveränderliche Speicherung.
Synthetische Daten und unbedenkliche Auszüge verwenden, niemals Zugangsdaten oder echten Datenverkehr.
Den versionsbezogenen Bericht nur auf Englisch mit gemeinsamen technischen Manifesten verfassen;
die Release-Anleitung und die übrige öffentliche Dokumentation bleiben in allen fünf Sprachen.
Ein Vorlageneintrag oder Häkchen allein beweist nichts. Ein Bericht belegt nur den angegebenen
Quellstand und Prüfumfang; unvollständige Kampagnen und ausstehende Veröffentlichungsschritte ausdrücklich nennen.

## Separate Client-Releases

Die `sdk-python-v*`-Tags des Python-Clients verwenden `.github/workflows/sdk-python-release.yml` und unabhängige
Versionen. Dieser Workflow liegt außerhalb dieser Prüfung; seine Veröffentlichung belegt keine erfolgreiche main-CI.
