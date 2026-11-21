---
title: Latet veröffentlichen
description: Eine Version erst veröffentlichen, nachdem genau ihr Commit auf main den vollständigen CI-Workflow bestanden hat.
---

Dieses Verfahren gilt für die `v*`-Tags von Latet und `.github/workflows/release.yml`. Halten Sie sich an
einen öffentlichen Commit pro Version, mit Code, Tests, Versionsnummern und Dokumentation in allen fünf Sprachen.

## Zusammenführen, CI abwarten, dann taggen

1. Bereiten Sie die Version als einen öffentlichen Commit vor und prüfen Sie sie. Halten Sie die Versionsnummern
   des Pakets und des Claude-Plugins gleich; der Build der Veröffentlichung vergleicht sie mit dem Tag.
2. Führen Sie diesen Commit in `main` zusammen. Halten Sie seinen vollständigen SHA fest.
3. Warten Sie, bis der **vollständige CI-Workflow** (`.github/workflows/ci.yml`), ausgelöst durch diesen Push
   auf `main`, für genau diesen SHA **erfolgreich** abgeschlossen ist. Prüfen Sie das Workflow-Ergebnis
   einschließlich aller Matrix-Jobs und der Servertests. Ein erfolgreicher Lauf für einen Pull-Request oder ein
   einzelner erfolgreicher Job genügt nicht.
4. Erstellen Sie das Versions-Tag genau auf diesem Commit und pushen Sie es erst, wenn die Veröffentlichung
   freigegeben ist. Leichtgewichtige und annotierte Tags werden unterstützt; bei einem annotierten Tag wird der
   Commit geprüft, auf den es verweist.
5. Prüfen Sie das Ergebnis des Veröffentlichungs-Workflows. Bewahren Sie den Tag-SHA und die URL des erfolgreichen
   main-CI-Laufs zusammen mit dem Prüfbericht der Version auf. Die Job-Zusammenfassung der Prüfung hält den
   akzeptierten Lauf und Versuch fest.

Ändert sich der Commit, prüfen Sie vor dem Taggen die main-CI für den neuen SHA erneut. Behalten Sie den einzelnen
öffentlichen Versions-Commit bei; fügen Sie keinen eigenen öffentlichen Commit hinzu, nur um erfolgreiche Prüfungen
festzuhalten.

## Anforderungen der Veröffentlichungsprüfung

Vor Builds, Attestierungen oder Veröffentlichung ermittelt die Prüfung die tatsächliche CI-Workflow-ID aus der
Workflow-Datei, prüft, ob der Tag-Commit zu `main` gehört, und liest die `push`-Läufe genau dieses SHA auf `main`.
Sie wählt den neuesten Lauf und ruft dessen aktuellen Versuch neu ab. Nur `completed` mit `success` wird akzeptiert.
Die nachfolgenden Jobs checken den geprüften Commit-SHA aus und hängen von dieser Prüfung ab.

Fehlende, eingereihte, laufende, wartende, fehlgeschlagene, abgebrochene, übersprungene oder neutrale Ergebnisse
blockieren die Veröffentlichung. API- oder Authentifizierungsfehler, widersprüchliche Antworten und unvollständige
Paginierung blockieren sie ebenfalls. Erreicht eine Liste die Obergrenze von 1.000 Ergebnissen, die GitHub für
gefilterte Suchen setzt, lehnt die Prüfung sie ab. Die Prüfung wartet nicht und versucht es nicht erneut, bis ein
günstiges Ergebnis vorliegt. Untersuchen Sie die fehlgeschlagene Prüfung, sorgen Sie dafür, dass die vollständige
CI für genau diesen main-Commit erfolgreich ist, und starten Sie danach die Veröffentlichung ausdrücklich neu, sobald sie
freigegeben ist. Verschieben Sie kein veröffentlichtes Tag, um einen Fehler zu umgehen.

Das Ergebnis gilt zum Prüfzeitpunkt; ein späterer erneuter CI-Lauf kann den Status des festgehaltenen Laufs ändern.
Eine grüne Prüfung bestätigt, dass die CI erfolgreich war, nicht, dass jeder denkbare Fehler ausgeschlossen ist.

## Ein Prüfbericht für jede Version

Verlinken Sie aus den Versionshinweisen eine kurze englische Prüfzusammenfassung, mit einer versionsbezogenen
Markdown-Datei und gemeinsamen Manifesten für Quellstand und Nachweise als Anhänge außerhalb von Git.
Stellen Sie den Bericht fertig, nachdem der öffentliche Commit existiert und seine vollständige main-CI erfolgreich
war; hängen Sie die Dateien nach dem freigegebenen Tag und der Veröffentlichung manuell an und ergänzen Sie die Links in den
Versionshinweisen. Der aktuelle Veröffentlichungs-Workflow erzeugt diese Prüfdateien nicht. Behalten Sie einen
öffentlichen Commit pro Version bei: Der externe Bericht kann dessen genauen SHA nennen, ohne ihn
in eine Datei aufzunehmen, die diesen SHA bestimmt.

Nehmen Sie Folgendes auf:

- Version, vollständigen öffentlichen Commit-SHA und den Commit-SHA, auf den das Tag verweist;
  getrennt davon den tatsächlich getesteten privaten Quellstand (Commit oder Basis mit aufbewahrtem
  Patch/Manifest). Ein Manifest nennt Pfade und Inhalts-Hashes von getestetem Code, Tests, Test-Harness
  und Konfiguration. Halten Sie alle Unterschiede zum öffentlichen Quellstand, Ausschlüsse und nötigen erneuten
  Prüfungen fest; ein privates Testergebnis gilt dadurch nicht automatisch für den öffentlichen SHA.
  Halten Sie die Inventare kanonischer Git-Blobs und tatsächlich ausgeführter Bytes für Läufe auf sauberem wie
  auf verändertem Arbeitsbaum getrennt; erfassen Sie EOL-/Filter-Umwandlungen und Unterschiede, ohne die Hashes
  der ausgeführten Bytes zu normalisieren.
- Die tatsächlich an der Prüfung Beteiligten oder identifizierbare Rollen und Berichtsverweise, Prüfumfang, Prüfart
  und die Angabe, ob ein unabhängiger Mensch geprüft hat. Die Prüfung durch einen separaten automatisierten Agenten
  ist keine menschliche Prüfung. Listen Sie geschlossene Befunde mit Nachprüfungsbelegen sowie offene Befunde mit
  Schweregrad und verbleibenden Arbeiten auf.
- Befehle der Testsuiten, Umgebung, Ergebnisse und übersprungene Tests; Fuzz-Konfigurations-IDs,
  Harness/Quellstand, Seeds oder Korpus, Soll-/Ist-Zahlen und abgeschlossene/geplante Konfigurationen.
  Kennzeichnen Sie fehlgeschlagene, unvollständige und nicht ausgeführte Arbeiten ausdrücklich.
  Zählkampagnen belegen keine Leistungsmessungen.
- Genaues CI-Repository, Workflow-Pfad, Ereignis/Ref, Head-SHA, Lauf-ID, Versuchs-URL, Status/Ergebnis
  des gesamten Workflows und Beobachtungszeitpunkt. Prüfen Sie die finale `push`/`main`-CI vor dem Taggen
  separat; PR-Prüfungen, lokale Ergebnisse und eine erfolgreiche Veröffentlichung ersetzen sie nicht.
- Grenzen der Tests: simulierte Upstreams oder direkte Space-Runtime-Tests gegenüber authentifizierten
  End-to-End-Tests, ausgelassene Konfigurationen und nicht verfügbare Nachweise. Private Logs sind nicht
  öffentlich prüfbar; stellen Sie nach Möglichkeit einen unbedenklichen öffentlichen Auszug bereit und
  geben Sie an, was Leser nicht selbst prüfen können.

Bewahren Sie Zusammenfassung und Manifeste außerhalb des Commits auf, den sie identifizieren. Versehen Sie
Nachweisdateien mit Prüfsummen und Revisionskennungen, bewahren Sie frühere Revisionen auf und erläutern Sie
Korrekturen. Ein Link oder eine Prüfsumme allein belegt weder eine unabhängige Prüfung noch eine unveränderliche
Speicherung. Verwenden Sie synthetische Daten und unbedenkliche Auszüge, niemals Zugangsdaten oder echten
Datenverkehr. Verfassen Sie den Bericht zu jeder Version nur auf Englisch, mit gemeinsamen technischen Manifesten;
die Anleitung zur Veröffentlichung und die übrige öffentliche Dokumentation bleiben in allen fünf Sprachen. Ein Vorlageneintrag
oder Häkchen allein beweist nichts. Ein Bericht belegt nur den angegebenen Quellstand und Prüfumfang; nennen Sie
unvollständige Kampagnen und ausstehende Veröffentlichungsschritte ausdrücklich.

## Getrennte Veröffentlichungen des Clients

Die `sdk-python-v*`-Tags des Python-Clients verwenden `.github/workflows/sdk-python-release.yml` und eine eigene,
unabhängige Versionierung. Dieser Workflow liegt außerhalb dieser Prüfung; seine Veröffentlichung belegt keine
erfolgreiche main-CI.
