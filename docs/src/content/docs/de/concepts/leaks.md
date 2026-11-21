---
title: Geheimnisse in gespeicherten Sitzungen
description: latet leak liest die Sitzungen, die Ihre Agenten auf diesem Rechner gespeichert haben, listet die Geheimnisse darin auf, ohne einen Wert zu zeigen, und sagt, welche beim Modellanbieter angekommen sein können.
---

Ihre Coding-Agenten speichern jede Unterhaltung vollständig auf der Festplatte: die Prompts, die
Sie eingegeben haben, die Dateien, die sie gelesen haben, die Ausgabe der Befehle, die sie
ausgeführt haben. Ein Geheimnis, das in eine Unterhaltung kam, bevor Sie Latet installiert hatten,
oder in einer Sitzung, die nicht über Latet lief, steht in diesen Dateien. Das
[Dashboard](../../dashboard/) kann es nicht zeigen: Es sieht nur die Anfragen, die über Latet
gingen, und nur so lange, wie sein Verlauf sie aufbewahrt (standardmäßig 14 Tage). `latet leak`
liest die gespeicherten Sitzungen mit den eigenen Detektoren von Latet und listet die Geheimnisse
darin auf, damit Sie wissen, welche Schlüssel Sie austauschen und welche Unterhaltungen Sie nicht
fortsetzen sollten.

```sh
latet leak
```

Der Befehl liest und gibt aus, und er zeigt nie einen Wert. Er ändert keine Ihrer Dateien, braucht
weder ein Netzwerk noch einen laufenden Daemon, und der Tresor von Latet erfährt dabei nichts: Die
gefundenen Werte bleiben nur während des Lesens im Speicher des Befehls, danach nicht mehr. Da er
keinen Wert ausgibt, kann auch ein Agent ihn ausführen.

## Wo Ihre Agenten ihre Sitzungen speichern

`~` ist Ihr Benutzerordner (`C:\Users\you` unter Windows). Ist die Variable eines Agenten gesetzt,
liegen seine Sitzungen in dem Ordner, den sie nennt, und `latet leak` liest sie dort.

| Agent | Seine gespeicherten Sitzungen | Die Variable, die sie verlegt |
| --- | --- | --- |
| [Claude Code](../../agents/claude-code/) | `~/.claude/projects/`: eine `.jsonl`-Datei je Sitzung und je Subagent | `CLAUDE_CONFIG_DIR`, anstelle von `~/.claude` |
| [Codex](../../agents/codex/) | `~/.codex/sessions/` und `~/.codex/archived_sessions/` | `CODEX_HOME`, anstelle von `~/.codex` |
| [OpenCode](../../agents/opencode/) | `~/.local/share/opencode/opencode.db` und der ältere Ordner `storage` daneben | `XDG_DATA_HOME`, anstelle von `~/.local/share` |
| [Copilot CLI](../../agents/other-tools/#github-copilot-cli) | `~/.copilot/session-state/`: eine Datei `events.jsonl` je Sitzung | `COPILOT_HOME`, anstelle von `~/.copilot` |
| [Gemini CLI](../../agents/gemini/) | `~/.gemini/tmp/`: der Ordner `chats` jedes Projekts | `GEMINI_CLI_HOME`, anstelle von `~` |
| [Qwen Code](../../agents/other-tools/#qwen-code) | `~/.qwen/projects/`: der Ordner `chats` jedes Projekts; dazu das ältere `~/.qwen/tmp/` | `QWEN_HOME`, anstelle von `~/.qwen` |
| [Hermes Agent](../../agents/autonomous-agents/#hermes-agent) | `state.db` in `~/.hermes` und unter Windows in `%LOCALAPPDATA%\hermes` | `HERMES_HOME`, der Ordner, in dem `state.db` liegt |

`--agent` liest nur die Agenten, die Sie nennen, unter diesen Namen: `claude`, `codex`, `opencode`,
`copilot`, `gemini`, `qwen` und `hermes`; wiederholen Sie die Option für weitere. `--path` liest
zusätzlich eine Datei oder die Dateien `.json`, `.jsonl` und `.ndjson` eines Ordners und seiner
Unterordner, auf dieselbe Weise: etwa die Sitzungen eines Agenten, den diese Tabelle nicht aufführt,
oder einen Ordner mit Sitzungen, die Sie von einem anderen Rechner kopiert haben.

**Was gelesen wird.** In jedem Datensatz jeder Text: Ihre Prompts, die Antworten des Modells, die
Werkzeugaufrufe und ihre Ergebnisse, auch ein Text, der selbst JSON ist. Nicht gelesen werden die
IDs, die Zeitangaben und die Signaturen, die nichts von der Unterhaltung enthalten. Jeder Text wird
von Ihren eigenen Detektoren mit Ihren globalen Einstellungen gelesen, so wie Latet die Ausgabe
eines Werkzeugs liest: die eingebauten Regeln, Ihre Begriffe und Ihre Erkenner, jeder Typ mit seiner
Richtlinie. Ein Wert, den Ihre Einstellungen durchlassen, wird nicht aufgeführt. Das lokale
Namensmodell und der lokale Prüfer werden nicht verwendet, und die `latet.yaml` eines Projekts wird
nicht gelesen.

Standardmäßig sucht der Befehl nur nach Geheimnissen: Schlüssel, Tokens, Passwörter, private
Schlüssel, Zugangsdaten in einer Webadresse. `--personal` zählt zusätzlich personenbezogene Daten
wie E-Mail-Adressen und Telefonnummern nach Typ: wie viele Sitzungen jeden Typ enthalten, nie einen
Wert.

## Was der Befehl ausgibt

Ein Beispiel mit erfundenen Ordnern und Zahlen:

```text
$ latet leak
Latet read 242 Claude Code sessions, 38 Codex sessions and 5 Copilot CLI sessions.

2 secrets in clear (no sign that Latet replaced them: they may have reached the provider):
  By type: 1 Github pat and 1 Password.
  Github pat  ghp_… (40 characters)  Claude Code, 4 sessions in C:\Users\you\dev\shop
      2026-09-02 to 2026-09-09
  Password (17 characters)  Codex, 1 session in C:\Users\you\dev\billing
      2026-09-20
Latet can tell only from 2026-10-10 on, the earliest date its vault and history keep on this computer: an emptied vault, or a history past the days it keeps (14 by default), keeps nothing older. A session listed in clear from before then may have gone through Latet all the same.

2 secrets Latet replaced, or may have (its fake left instead where the session went through Latet):
  By type: 1 Anthropic api key and 1 Github pat.
  Anthropic api key  sk-ant-api03-… (108 characters)  Claude Code and Codex, 6 sessions in C:\Users\you\dev\billing, C:\Users\you\dev\shop
      2026-10-12 to 2026-11-14; 5 confirmed by Latet's history, 1 if the session went through Latet
  Github pat  ghp_… (40 characters)  Claude Code, 2 sessions in C:\Users\you\dev\shop
      2026-10-12 to 2026-11-14; 1 confirmed by Latet's history, 1 if the session went through Latet

Rotate each secret in clear at its vendor: what reached a model provider may stay in its logs. Do not resume those sessions (a resumed conversation sends them again): start new ones. The secrets Latet replaced need nothing where the session went through Latet.
```

Dasselbe Geheimnis, in mehreren Sitzungen eingefügt, steht in einer Zeile, mit der Zahl der
Sitzungen. Ebenso ein Geheimnis, das die Detektoren als zwei Typen gefunden haben, etwa ein
GitHub-Token im Header eines `curl`-Befehls und einer Variablen zugewiesen: Die Zeile zeigt den Typ
des Anbieters. Ein Geheimnis kann in zwei Listen stehen, wie das GitHub-Token oben: eingefügt, bevor
Latet da war, und danach noch einmal.

### Im Klartext

Nichts zeigt, dass Latet sie ersetzt hat, also können sie beim Modellanbieter angekommen sein (*no
sign that Latet replaced them: they may have reached the provider*). Eine Sitzung zählt hier, wenn
sie den Wert enthielt, bevor der Tresor von Latet ihn zum ersten Mal aufbewahrte, oder wenn der
Tresor den Wert gar nicht kennt. Zwei weitere Fälle zählen hier: eine Sitzung, deren Datensätze
keine Zeitangabe haben und sich daher weder vor noch nach dieses Datum setzen lassen, und eine
Sitzung, die den Wert laut dem Verlauf von Latet zuerst im Klartext gesendet hat: Der Verlauf
enthält noch die ersten Anfragen der Sitzung und zeigt den Wert erst in späteren maskiert. Diese
Geheimnisse sollten Sie austauschen. Unter der Liste sagt der Befehl, ab welchem Datum Latet es
feststellen kann: ab dem frühesten Datum, das sein Tresor und sein Verlauf auf diesem Rechner
aufbewahren.

### Von Latet ersetzt

Der Tresor kannte den Wert schon, als die Sitzung ihn enthielt: Wo die Sitzung über Latet lief, hat
der Modellanbieter also den Ersatzwert von Latet erhalten. Der Wert steht trotzdem in der Datei der
Sitzung, denn der Agent speichert, was er hatte, bevor Latet den Wert auf dem Weg nach außen
ersetzte. Für den Modellanbieter müssen Sie nichts austauschen, aber der Wert liegt in einer Datei
auf diesem Rechner.

Bei jedem Geheimnis steht, wie viele seiner Sitzungen *confirmed by Latet's history* sind, also vom
Verlauf von Latet bestätigt: Der Verlauf enthält noch eine Anfrage der Sitzung, in der der Wert
maskiert wurde. Dafür muss der Verlauf vollständig aufbewahrt werden (*Alles*, die Voreinstellung)
und die Sitzung noch enthalten (standardmäßig 14 Tage), und der Agent muss seine Sitzung in seinen
Anfragen nennen, wie es Claude Code, Codex und Hermes Agent tun. Jede andere Sitzung zählt *if the
session went through Latet*, also nur, wenn sie über Latet lief: Ein Agent, der damals nicht für
Latet eingerichtet war, hat den Wert im Klartext gesendet, und nur der Verlauf kann das klären. Hat
ein Geheimnis eine solche Sitzung, sagt die Überschrift *Latet replaced, or may have*.

### Geschwärzt

Geheimnisse, die Ihre Einstellungen schwärzen oder die Latet schwärzt, weil sie zu lang zum
Aufbewahren sind (*your settings redact, or Latet redacts as too long to keep*): ein Geheimnis eines
Typs, den Ihre [Richtlinien](../profiles/#richtlinien) auf `redact` setzen, oder eines, das zu lang
für den Tresor ist (mehr als 16.384 Zeichen). Latet ersetzt einen solchen Wert durch einen
Platzhalter und bewahrt ihn nirgends auf, sein Tresor hat also kein Datum dafür. Die Sitzungen ab
dem frühesten Datum, das Latet auf diesem Rechner hat, stehen hier, die davor im Klartext. Ihre
Einstellungen können damals andere gewesen sein, und die Sitzung ist vielleicht nicht über Latet
gelaufen: Deshalb sagt die Überschrift, dass Latet an seiner Stelle einen Platzhalter gesendet
haben kann (*may have sent a marker*).

### Jede Liste

Unter ihrer Überschrift zählt eine Liste ihre Geheimnisse nach Typ: die sechs Typen mit den meisten,
dann die übrigen zusammen. Dann folgen die Geheimnisse, zuerst die Schlüssel eines Anbieters (ein
GitHub-Token, ein Anthropic-Schlüssel, ein AWS-Schlüssel: die, die Sie zuerst austauschen sollten),
dann die in den meisten Sitzungen. Eine Liste zeigt die ersten 20 und sagt, wie viele es noch gibt;
`--all` führt alle auf.

Ein Geheimnis zeigt seinen Typ, einen Hinweis und, für die Sitzungen dieser Liste, die Agenten, die
Zahl der Sitzungen, die Projektordner (höchstens drei, dann wie viele weitere) und die Daten, an
denen es zuerst und zuletzt auftauchte, nach der Uhr dieses Rechners, so wie `--since` ein Datum
liest. Der Hinweis behält nur das öffentliche Präfix, das der Anbieter jedem Schlüssel seiner Art
voranstellt (`ghp_…`, `sk-ant-api03-…`, `AKIA…`), dasselbe, das ein Ersatzwert behält, und die
Länge; ein Geheimnis ohne ein solches Präfix zeigt seinen Typ und seine Länge
(`Password (17 characters)`).

Am Ende stehen mit `--personal` die personenbezogenen Daten, dann, was zu tun ist, und was nicht
gelesen werden konnte, mit dem Grund, nie mit seinem Inhalt: eine Datei, die sich nicht öffnen
lässt, eine JSON-Datei von mehr als 32 MB, ein einzelner Text von mehr als 8 MB, eine Datenbank, die
sich bei jedem Kopieren geändert hat.

**Ohne die Antworten von Latet.** Auf einem Rechner, der mit einem [Latet-Server](../server/)
verbunden ist, gehören der Tresor und der Verlauf dem Server. Der Befehl sagt dann, dass er nicht
feststellen konnte, welche Geheimnisse Latet ersetzt hat, und führt jedes Geheimnis im Klartext auf,
außer denen, die er als geschwärzt aufführt. Ebenso verfährt er, und nennt den Grund, wenn sich der
Tresor von Latet auf diesem Rechner oder sein Schlüssel nicht lesen ließ oder kein hier gefundener
Schlüssel ihn öffnet. Auf einem Rechner ohne Tresor von Latet und ohne Latet-Server hat Latet nichts
ersetzt: Der Befehl sagt das und führt jedes Geheimnis im Klartext auf, auch die der geschwärzten
Typen.

## Optionen

| Option | Was sie bewirkt |
| --- | --- |
| `--agent NAME` | Nur die Sitzungen dieses Agenten lesen: `claude`, `codex`, `opencode`, `copilot`, `gemini`, `qwen` oder `hermes`. Für weitere wiederholen. |
| `--since DATE` | Die Datensätze ab diesem Datum lesen (`2026-10-01`), nach der Zeit jedes Datensatzes, sonst nach der letzten Änderung seiner Datei. |
| `--personal` | Zusätzlich personenbezogene Daten nach Typ zählen: wie viele Sitzungen jeden Typ enthalten. |
| `--path PATH` | Zusätzlich diese Datei lesen, oder die JSON- und JSON-Lines-Dateien dieses Ordners. Für weitere wiederholen. |
| `--json` | Ein JSON-Dokument statt Text ausgeben, mit jedem Geheimnis. |
| `--all` | Jedes Geheimnis jeder Liste aufführen, nicht nur die ersten 20. |

`--json` gibt für jedes Geheimnis einen Datensatz aus: seinen `type` (den, den der Text zeigt),
`types` (jeden Typ, als den es gefunden wurde), `class`, `hint`, seine `agents` und `folders` und
seine Sitzungen `in_clear`, `replaced` und `redacted`, jeweils mit ihrer Zahl, dem ersten und dem
letzten Datum, wie viele davon bestätigt sind, und den `agents` und `folders` dieser Sitzungen.
Daneben: die gelesenen Sitzungen je Agent, die personenbezogenen Daten nach Typ, was nicht gelesen
werden konnte (`skipped`) und wie viele Dinge das sind (`unread`), `latet_answers` (ob der Tresor
und der Verlauf von Latet gefragt werden konnten) und `latet_since` (das früheste Datum, das sie
aufbewahren, angegeben, wenn ein Geheimnis im Klartext steht).

Der Exit-Code ist `0`, wenn nichts gefunden wurde, `1`, wenn ein Geheimnis gefunden wurde, in
welcher Liste auch immer, oder mit `--personal` personenbezogene Daten, und `2`, wenn keine Sitzung
gelesen werden konnte oder eine Option falsch ist. Ein Skript kann den Befehl ausführen und danach
handeln.

## Wie lange es dauert

`latet leak` liest jeden Text jeder gespeicherten Sitzung, und das dauert, wo es viele gibt: etwa
11 Minuten für 10 GB Sitzungen auf einem Desktop-PC. In einer Konsole zeigt er in einer Zeile den
Agenten, den er gerade liest, und wie viele seiner Sitzungen er schon gelesen hat, danach, wie lange
es gedauert hat; nach einer Minute sagt er, dass `--agent` und `--since` weniger lesen:

```sh
latet leak --agent claude --since 2026-10-01
```

Eine Sitzung wird nach den gelesenen Datensätzen beurteilt: Mit `--since` kann ein Geheimnis als
ersetzt aufgeführt werden, das ein früherer Datensatz derselben Sitzung im Klartext gesendet hat.

Diese Zeilen gehen in die Standardfehlerausgabe, und nur in eine Konsole: In die Standardausgabe
geht allein der Bericht, und mit `--json` bleibt er dort ein einziges JSON-Dokument.

## Was der Befehl nicht feststellen kann

- **Vor dem Datum von Latet.** Latet kennt einen Wert ab dem Tag, an dem sein Tresor ihn zum ersten
  Mal aufbewahrt hat, und eine Sitzung, die den Wert vor diesem Tag enthielt, steht im Klartext. Ein
  geleerter Tresor, ein Wert, den Sie mit *Vergessen* entfernt haben (der Tresor kennt ihn wieder ab
  dem Moment, in dem er zurückkommt), oder ein Verlauf jenseits der Tage, die er aufbewahrt,
  bewahrt nichts Älteres auf: Eine Sitzung, die im Klartext steht und älter ist als das Datum unter
  der Liste, kann trotzdem über Latet gelaufen sein.
- **Eine Sitzung an Latet vorbei.** Ein Geheimnis, das als ersetzt aufgeführt ist, in einer Sitzung,
  die der Verlauf nicht bestätigt, kann an Latet vorbei gesendet worden sein: Nur der Verlauf kann
  das klären, für die Tage, die er aufbewahrt.
- **Was Claude Code für seine eigene Anzeige aufbewahrt.** Claude Code speichert eine zweite Kopie
  des Ergebnisses eines Werkzeugs für seine eigene Anzeige (`toolUseResult`): eine gelesene Datei,
  die ganze Datei einer Bearbeitung, die Ausgabe eines Befehls. Diese Kopie erreicht den
  Modellanbieter nie, und `latet leak` liest sie nicht. Gelesen wird die Kopie, die die Anfrage
  enthielt.
- **Ein Geheimnis in zwei Teilen.** Jeder Text wird für sich gelesen: Ein Geheimnis, das über zwei
  Datensätze verteilt ist, wird nicht gefunden, und ein Wert, den ein Text am Wort davor erkennen
  lässt (`password:`), wird nicht gefunden, wo ein anderer Text ihn allein schreibt.
- **Was die Detektoren nicht erkennen,** wie beim Maskieren von Latet
  ([Nicht geschützt](../protection-scope/#nicht-geschützt)). Auch ein Wert, den Latet nur aus seinem
  Tresor kennt, weil der lokale Prüfer oder Sie ihn gefunden haben, wird nicht gefunden: Der Scan
  beginnt mit einem eigenen, leeren Tresor.

Eine Datenbank im Write-Ahead-Modus, die schon beim Lesen Dateien neben sich anlegen würde, wird aus
einer privaten Kopie in einem temporären Ordner gelesen, die danach gelöscht wird; die Kopie wird nur
angelegt, wo danach noch mindestens 1 GB auf der Festplatte frei bleibt. Das gilt für die
Datenbanken von OpenCode und Hermes Agent und für den Tresor und den Verlauf von Latet selbst.

## Was Sie mit einem Fund tun

- **Tauschen Sie jedes Geheimnis im Klartext beim Anbieter des Schlüssels aus,** zuerst die
  Schlüssel eines Anbieters, so wie die Liste sie ordnet: Erstellen Sie einen neuen Schlüssel dort,
  wo der Anbieter sie ausgibt (GitHub, Anthropic, AWS ...), setzen Sie ihn anstelle des alten ein,
  und widerrufen Sie den alten; ändern Sie ein Passwort. Was bei einem Modellanbieter angekommen
  ist, kann in dessen Protokollen bleiben, egal was Sie auf Ihrem Rechner löschen. Der Typ, das
  Präfix, die Ordner und die Daten sagen Ihnen, welcher Schlüssel es ist und wo er verwendet wurde.
- **Setzen Sie diese Unterhaltungen nicht fort.** Eine fortgesetzte Unterhaltung sendet alle ihre
  Nachrichten noch einmal, das Geheimnis mit ihnen: Beginnen Sie eine neue.
- **Die Geheimnisse, die Latet ersetzt hat,** brauchen beim Anbieter des Schlüssels nichts, wo die
  Sitzung über Latet lief: Der Modellanbieter hat einen Ersatzwert erhalten. **Die geschwärzten
  brauchen vielleicht nichts:** Lief die Sitzung über Latet, hat der Modellanbieter an ihrer Stelle
  einen Platzhalter erhalten.
- **Die Dateien auf der Festplatte gehören Ihrem Agenten.** Latet löscht und verschiebt keine davon.
  Um einen Wert von diesem Rechner zu entfernen, löschen oder verschieben Sie die Sitzungsdateien,
  die ihn enthalten, bei geschlossenem Agenten: Der Agent, der Projektordner und die Daten in der
  Liste sagen Ihnen, wo Sie suchen müssen. Eine Unterhaltung, deren Datei Sie löschen, lässt sich
  nicht mehr fortsetzen.

Ein Geheimnis, das Sie ausgetauscht haben, wird weiter aufgeführt, solange seine Sitzungsdateien
da sind: Die Liste sagt, was die Dateien enthalten, nicht, ob ein Schlüssel noch gilt.
