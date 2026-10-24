---
title: Agenten und Ihre Einstellungen
description: Lassen Sie Ihren Agenten die Einstellungen von Latet ansehen und ändern. Was den Schutz erhöht, wird angewendet; was ihn verringert, wartet auf Sie.
---

:::caution[Eine Leitplanke, kein Schloss]
Damit kann Ihnen ein Agent helfen, Latet und Ihre Projekte einzurichten, ohne den Schutz
versehentlich zu verringern. Gegen einen Agenten, der gezielt versucht, das zu umgehen, oder einen,
der durch eine Prompt-Injection getäuscht wurde, hilft das nicht: Der Agent läuft unter Ihrem
Benutzerkonto. Eine echte Grenze erfordert mehr, etwa dass Latet unter einem anderen Benutzer oder
auf einem anderen Rechner läuft: siehe [was das wert ist](#woran-latet-es-erkennt-und-was-das-wert-ist).
:::

Sie können Ihren Agenten bitten, Latet für Sie einzurichten:
*"maskiere unseren Kunden Falcon überall"*, *"schalte in diesem Projekt den Live-Prüfer ein"*,
*"warum wurde dieser Wert maskiert?"*. Verweisen Sie ihn auf
[`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md) im Repository: Die
Datei ist für Agenten geschrieben und sagt ihnen, welche Befehle sie verwenden und was sie zuerst mit
Ihnen klären sollen.

## Was ein Agent selbst tun kann

Er sieht mit `latet config get`, `latet project show` und `latet status` nach und ändert mit
`latet config set`, `add` und `remove`. Jeder Wert wird gegen das Schema der Einstellungen geprüft,
und die Änderung gilt sofort für den laufenden Daemon, **sofern sie den Schutz erhöht**: ein
Begriff, ein Erkenner, ein strengeres Profil, der lokale Prüfer, der mehr liest, der eingeschaltete
Live-Prüfer.

```sh
latet config add terms '{value: Project Falcon, type: PROJECT}'
latet config set live_review true --project .
```

## Was auf Sie wartet

Eine Änderung, die den Schutz **verringert**, gilt erst, wenn Sie sie freigeben, egal, wer sie
vornimmt:

- ein `latet`-Befehl, den ein Agent oder ein Skript ausführt;
- eine von Hand bearbeitete `config.yaml`, wenn der Daemon sie neu lädt;
- eine `config.yaml`, die beim Start des Daemons verändert vorgefunden wird: Bis Sie entscheiden,
  läuft er mit den Einstellungen, die Sie zuletzt freigegeben haben.

Den Schutz verringern heißt zum Beispiel: ein Wert, der nie maskiert werden soll, ein niedrigeres
Profil, ein entfernter Begriff oder Erkenner, eine Richtlinie auf `allow`, ein Begriff oder eine
Feldregel mit einem Typ, dessen Richtlinie `allow` ist (das Gefundene bliebe im Klartext), der
lokale Prüfer oder der Live-Prüfer ausgeschaltet oder so eingestellt, dass er weniger liest, Latet
in einem Projekt ausgeschaltet, der Server des lokalen Prüfers auf einen anderen Rechner verlegt
oder [seine Hinweise](../llm-review/#ihre-hinweise) geändert: Freier Text könnte ihm sagen, nichts
zu melden, daher wartet jede Änderung, und das Dashboard zeigt die Hinweise vorher und nachher.
Dasselbe gilt für ein [Ziel](../../agents/company-endpoints/), das auf eine andere Adresse verlegt
oder entfernt wird: Ein Agent, der darauf zeigt, würde seine Anfragen und seine Schlüssel
woandershin senden, daher zeigt das Dashboard die Adresse vorher und nachher.

Die Änderung wartet auf der Seite **Freigaben** im Dashboard, unter *Einstellungen*, mit dem, was
sie verringert, woher sie kommt (der Agent, der den Befehl ausgeführt hat, wenn Latet das erkennen
kann) und welche Einstellungen sie betrifft. **Freigeben** wendet sie an. **Rückgängig machen**
setzt `config.yaml` auf den vorherigen Stand zurück. Es wartet immer nur eine Änderung; eine neuere
ersetzt sie. In einem eigenen Terminal fragt Sie stattdessen der Befehl.

Der Agent erfährt, was die Änderung verringert, nie einen Wert, und dass sie auf Sie wartet: Die
Anleitung bittet ihn, Ihnen zu sagen, was er geändert hat und warum.

**Ein Repository, das Latet gerade nicht lesen kann.** Eine Änderung wird auch in jedem Repository
mit einer eigenen `latet.yaml` abgewogen, das Latet gesehen hat. Ihre Dateien liest ein Hilfsprozess
erneut, mit 10 Sekunden für alle und 2 für jede, bei einem Neuladen oder Start, dessen Änderung den
Schutz in einem Repository verringern könnte, und bei einem Import im Dashboard (siehe
[Die Repositorys, die Latet sich merkt](../projects/#die-repositorys-die-latet-sich-merkt)). Lässt
sich eines in dieser Zeit nicht lesen, oder gerade überhaupt nicht (eine Netzwerkfreigabe, die nicht
antwortet, eine verweigerte Berechtigung), gilt eine Änderung, die dort den Schutz verringern
könnte, weder, noch wartet sie auf Sie: `latet reload` sagt das und ändert nichts, ebenso ein Import
im Dashboard, bis sich das Repository wieder lesen lässt. Eine Änderung, die in keinem Repository
etwas verringern kann (ein hinzugefügter Begriff, der Port, der Verlauf), gilt wie gewohnt. Findet
ein Start die `config.yaml` geändert und kann sie so nicht abwägen, laufen die zuletzt freigegebenen
Einstellungen. `latet repositories` zeigt, welches Repository es ist, und
`latet repositories forget` nimmt es aus der Liste, in einem eigenen Terminal.

## Was Agenten nicht können

`latet global-uninstall`, `latet agents revert` und `latet service uninstall` nehmen allen Agenten
auf einmal den Schutz: In einem Agenten ausgeführt, verweigern sie die Ausführung und bitten Sie,
sie in einem eigenen Terminal auszuführen. Ebenso `latet repositories forget`, `latet backup` und
`latet restore` (Ihr Tresor und sein Schlüssel), `latet connect` und `latet disconnect` (wo die
Anfragen Ihrer Agenten maskiert werden), `latet dashboard` und `latet identity send` auf einem
verbundenen Rechner, die Administration mit `latet server` und `latet rules suggest --show-values`,
das echte Werte ausgeben würde.

## Woran Latet es erkennt und was das wert ist

Ein Agent kennzeichnet die Shells, in denen er Befehle ausführt, mit eigenen Variablen, die Latet
erkennt: Claude Code, Codex, OpenCode, Gemini CLI, Cursor, Hermes Agent, Goose, OpenClaw und jeder
Agent, der `AGENT` setzt. Der Daemon nennt den Agenten auch in der Änderung, die er Ihnen zeigt.

Das sind Schutzvorkehrungen gegen Versehen, keine Schlösser:

- **Entscheidend ist die Prüfung des Daemons.** Er vergleicht jede neue Konfiguration mit der, die
  Sie zuletzt freigegeben haben, egal, wer sie geschrieben hat. Ein Agent, der `config.yaml`
  bearbeitet, statt die Befehle zu verwenden, erhält dieselbe Antwort.
- **Ein Agent läuft unter Ihrem Benutzerkonto.** Einer, der es darauf anlegt oder von gelesenem Text
  getäuscht wird (einer Prompt-Injection), kann seine Variablen entfernen, die Dateien lesen, die
  Latet liest, und seine eigene Änderung über die lokale Schnittstelle des Daemons freigeben. Latet
  kann nicht verhindern, was Ihr eigenes Benutzerkonto tun kann: Dafür muss Latet unter einem
  anderen Benutzer oder auf einem anderen Rechner laufen
  ([Latet auf einem Server](../server/)).

Für einen Agenten, den Sie beaufsichtigen, genügt das: Er kann Latet nicht versehentlich schwächen,
und Sie sehen jede Änderung, die den Schutz verringert, bevor sie gilt. Siehe
[Sicherheit](../../security/) für den Rest des Bedrohungsmodells.

## Auf einem verbundenen Rechner

Die gemeinsamen Einstellungen und die eigenen Einstellungen und Regeln jeder Identität liegen
auf dem Server. Verwenden Sie `latet dashboard` für eine Person und die Administration mit
`latet server` für die gemeinsamen Einstellungen oder die eines API-Schlüssels. Im verbundenen
Zustand verweigern `config`, `rules`, `llm`, `project`, `identity`, `repositories` und `reload` das
Lesen wie das Ändern von Einstellungen; nur `config schema`, `rules test` und `identity send` laufen.
`latet identity send` sendet die Identitätsangaben dieses Rechners ausdrücklich erneut.

Verbindungsmetadaten, Zertifikatsbindung, Zugangsdaten und Agentenbindungen bleiben auf
dem Rechner. `LATET_CONNECTION` wählt Metadaten, keine Datei mit dem persönlichen Token.
Die Claude-Integration schreibt das Token unter Windows als dokumentierte Installationsausnahme in
die Claude-Benutzereinstellungen (unter Linux und macOS wird kein Token geschrieben, da Claude Code
nur von Windows aus über einen Server geht); die Trennung entfernt die Bindung. Server-Prüferendpunkte,
Schlüssel, Zertifikate und OCR/NER-Installation sind Sache der Administratoren.
Ein Projekt im Dashboard erlaubt dem Server keine Suche im Rechner-Repository.
Siehe [Servereinrichtung](../../getting-started/server/) und
[gemeinsame Einstellungen und eigene Regeln](../server/).

## Befehle

| Befehl | Was er tut |
| --- | --- |
| `latet config get [KEY]` | Eine Einstellung (Punkte für Verschachteltes: `llm_review.enabled`) oder alle |
| `latet config set KEY VALUE` | Ändert eine Einstellung (der Wert in YAML); `--unset` stellt ihren Standardwert wieder her |
| `latet config add KEY VALUE` / `remove KEY VALUE` | Fügt einer Liste einen Eintrag hinzu oder nimmt einen heraus (Begriffe nach Wert, Erkenner nach Name) |
| `--project FOLDER` | Ihre eigenen Einstellungen für das Projekt in diesem Ordner statt der globalen |
| `--wait` | Wartet auf Ihre Antwort, wenn die Änderung auf Sie wartet |
| `latet reload` | Wendet eine von Hand bearbeitete `config.yaml` an (was den Schutz verringert, wartet auf Sie) |

Exit-Codes: `0` angewendet, oder gespeichert, während der Daemon nicht läuft (er wendet es beim
Start an, und was den Schutz verringert, wartet dann auf Sie); `3` wartet auf Sie; `1` der Daemon
hat die Änderung abgelehnt (gespeichert, nicht angewendet: `latet reload` sagt, warum), oder das zu
entfernende Element ist nicht vorhanden; `2` ungültig (nichts gespeichert).
