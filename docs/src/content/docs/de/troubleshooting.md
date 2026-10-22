---
title: Fehlerbehebung und FAQ
description: Häufige Probleme und häufig gestellte Fragen.
---

## Beginnen Sie hier

Die meisten Probleme zeigt ein einziger Befehl:

```sh
latet doctor
```

Er prüft den Daemon, die Verbindung des Plugins zum Daemon, den Autostart bei der Anmeldung, den
Port, einen Selbsttest der Maskierung und jeden Agenten, den er findet. Jede Zeile mit `WARN` oder
`FAIL` nennt den Befehl, der das Problem behebt.

Wenn der Daemon im Hintergrund läuft, schreibt er sein Log in `logs/daemon.log` im Datenordner:
`%LOCALAPPDATA%\latet` unter Windows, `~/Library/Application Support/latet` unter macOS,
`~/.local/share/latet` unter Linux. Um es stattdessen live zu verfolgen, stoppen Sie den Daemon, und
führen Sie `latet serve` aus, der das Log im Terminal ausgibt.

## Fehlerbehebung

### Mein Agent meldet, dass er keine Verbindung herstellen kann

Der Agent ist für Latet eingerichtet, aber der Daemon läuft nicht. Prüfen Sie ihn, und starten Sie
ihn:

```sh
latet status
latet start
```

`latet status` gibt `running: Latet 0.9.3 on 127.0.0.1:5283 (masking), pid 12345` aus, wenn der
Daemon läuft, sonst `not running`. Wenn Sie Latet stattdessen nicht mehr verwenden möchten, führen
Sie zuerst `latet global-uninstall` aus: Er stellt den vorherigen Zustand Ihrer Agenten wieder her.
Siehe [Deinstallation](../getting-started/uninstall/).

### Claude Code meldet, dass Latet Inhalte zurückgehalten hat

Meldungen wie diese bedeuten, dass das Plugin keine Antwort vom Daemon bekommen hat oder der Daemon
an dem Inhalt gescheitert ist; deshalb hat das Plugin den Inhalt zurückgehalten, statt ihn
unmaskiert zu senden:

- `Latet could not mask this prompt: the local daemon did not answer, or failed on it.`
- `[withheld by Latet: the local daemon did not answer, or failed on it, so this content was not sent]`
- `Latet could not check this request (the local daemon did not answer, or failed on it), so it was not sent.`

Führen Sie `latet start` aus, dann `latet doctor`. Wenn der Daemon läuft, steht in seinem Log
(`daemon.log` im Ordner `logs` des Datenordners), was fehlgeschlagen ist: Bitte eröffnen Sie ein
Issue mit diesem Log. Das Plugin startet den Daemon selbst mit dem Befehl `latet`; wenn Sie Latet
aus einem Quellcode-Checkout ausführen, liegt dieser Befehl nicht in Ihrem `PATH`, starten Sie den
Daemon daher mit `uv run latet start`.

### Latet lehnt seinen Datenordner ab

Die Meldung nennt den Ordner, wer ihn sonst ändern darf, und was zu tun ist, etwa:

```text
Integration records unavailable (unsafe folder namespace): C:\Users\you lets PC\other (S-1-5-21-…)
change what is in it (its access list grants full control, (F), mask 0x001f01ff; `icacls "C:\Users\you"`
lists it): remove that entry (`icacls "C:\Users\you" /remove:g *S-1-5-21-…`), trust that account
(LATET_TRUSTED_ACCOUNTS), or use a data folder whose every parent only you, SYSTEM, the Administrators
and the accounts in LATET_TRUSTED_ACCOUNTS can change (LATET_DATA_DIR); with a new data folder,
`latet agents apply` applies your agents again.
```

Latet bewahrt, was es auf Ihre Agenten angewendet hat, und auf einem Rechner, der mit einem
Latet-Server verbunden ist, seine Verbindung in einem Datenordner auf, den kein anderes Konto des
Rechners ändern kann: Ein anderes Konto könnte dort ablegen, was Latet dann anwendet oder sendet. Der
genannte Ordner ist der Datenordner oder einer der Ordner darüber.

- **Der Datenordner selbst** wird abgelehnt, sobald ein anderes Konto ihn ändern kann, ebenso ein
  übergeordneter Ordner, der einem anderen Konto gehört.
- **Ein übergeordneter Ordner, den ein anderes Konto ändern kann,** wird auf einem Rechner abgelehnt,
  der mit einem Latet-Server verbunden ist: Dort steht das Token des Servers auf dem Spiel. Auf einem
  nicht verbundenen Rechner macht Latet weiter, und `latet doctor` warnt mit denselben Worten: Ein
  Konto, das Ihr Profil ändern kann, kann darin schon alles ändern, was Ihnen gehört, auch die Daten
  von Latet.

Unter Windows listet `icacls "<Ordner>"`, wer den Ordner ändern darf: Ein Eintrag für ein anderes Konto
mit `(F)`, `(M)` oder `(W)` erlaubt es ihm. Es gibt drei Auswege:

1. Entfernen Sie diesen Eintrag, wo der Rechner Ihnen gehört. Ein mit `(I)` markierter Eintrag kommt
   vom Ordner darüber und wird dort entfernt; ein Eintrag, der nur als SID erscheint, ist ein Konto,
   das Windows nicht mehr findet, oft ein gelöschtes.
2. Vertrauen Sie diesem Konto: Setzen Sie `LATET_TRUSTED_ACCOUNTS` für Ihr Konto auf seinen Namen
   oder seine SID, etwa mit `setx LATET_TRUSTED_ACCOUNTS "PC\other"`, oder in den
   Systemeinstellungen, oder für den ganzen Rechner per Gruppenrichtlinie, und öffnen Sie dann ein
   neues Terminal. Latet liest die Variable dort, in der Registrierung, und nie aus der Umgebung eines
   Terminals: Ein Wert, der nur in einer Terminalsitzung gesetzt ist (`$env:` oder `set`), zählt
   nicht, damit die Einstellungen eines Repositorys oder die Sitzung eines Agenten ihn nicht ändern
   können. Mehrere Konten werden durch `;` getrennt, und eine SID darf `*S-1-5-…` geschrieben werden,
   wie icacls sie schreibt. Das gilt nur für die übergeordneten Ordner, nie für den Datenordner
   selbst, der den Schlüssel des Tresors enthält, und nie für einen Ordner darin.
3. Setzen Sie `LATET_DATA_DIR` auf einen Ordner, dessen übergeordnete Ordner nur Sie, SYSTEM, die
   Administratoren und die Konten ändern können, denen Sie vertrauen, und führen Sie dann
   `latet agents apply` erneut aus.

Einem direkten Mitglied der lokalen Gruppe Administratoren, etwa einer Helpdesk-Gruppe, die Ihre
IT-Abteilung hinzugefügt hat, wird bei den übergeordneten Ordnern wie der Gruppe selbst vertraut, ohne
jede Einstellung. Das Vertrauen in diese Konten und Mitglieder gilt für die übergeordneten Ordner
jedes Ordners, den Latet so prüft: die des Datenordners, die über dem Datenordner, die der
Hilfsprozess von Claude Code prüft, die seiner Installation und die des Log-Ordners des
Windows-Dienstes eines Latet-Servers. `latet doctor` listet die Konten, denen
`LATET_TRUSTED_ACCOUNTS` vertraut, mit dem Ort, an dem die Variable gesetzt ist, und die Mitglieder,
denen es vertraut hat. Es nennt jeden Eintrag, der niemandem Vertrauen gibt: einen Namen, den Windows
nicht findet, oder ein Konto, das keines dieses Rechners, einer Domäne, von Entra ID oder eines
Dienstes ist, wie Jeder, Benutzer oder NTLM-Authentifizierung. Es sagt auch, wenn die Variable nur in
seinem Terminal gesetzt ist.

Unter Linux und macOS zeigt `ls -ld <Ordner>` Besitzer und Modus. Ein Ordner, in den andere schreiben
dürfen, wird abgelehnt: `chmod go-w <Ordner>`, oder `chmod 700` für den Datenordner selbst. Ein
Datenordner, den andere nur lesen dürfen (0755, wie `mkdir` ihn anlegt), wird beim ersten Mal, wenn
Latet ihn braucht, nur Ihrer (0700), und Latet sagt das einmal: im Log des Daemons oder im Terminal des
Befehls, der die Änderung vorgenommen hat. `LATET_TRUSTED_ACCOUNTS` ändert dort nichts: Die Prüfungen
richten sich nach Besitzer und Modus.

Ein Rechner, der nie mit einem Latet-Server verbunden war, wird nie wegen einer Verbindung abgelehnt.

### Claude Code hält auf einem verbundenen Rechner jeden Prompt zurück

Das Plugin sagt warum, und jede Ursache hat ihre eigene Lösung:

- **Print-Modus oder eine andere Oberfläche:** `claude -p` und jede Oberfläche außer dem
  interaktiven Terminal werden auf einem Rechner, der mit einem Latet-Server verbunden ist,
  zurückgehalten. Verwenden Sie das interaktive Terminal.
- **Die Bindung des Hilfsprozesses fehlt oder ist veraltet**, nach einem Upgrade oder einer neuen
  Verbindung: Führen Sie `latet agents apply claude` aus, oder starten Sie Claude Code mit
  `latet claude`. `latet doctor` prüft sie.
- **Der Latet-Server antwortete nicht:** `latet status` sagt, ob er antwortet und das Token dieses
  Rechners annimmt.
- **Der Server nimmt das Token dieses Rechners nicht mehr an:** Es wurde widerrufen, oder Ihre
  Identität deaktiviert. Führen Sie `latet disconnect` aus, dann `latet connect <url>` mit einem
  neuen Code eines Administrators.
- **Linux und macOS:** Claude Code über einen Server braucht in dieser Version ein Windows-Terminal;
  `latet codex` und `latet opencode` gehen über den Server.

`latet agents apply claude` und `latet claude` können mit `Claude Code's helper binding is
unavailable` ablehnen. Die Meldung nennt den Teil der Installation, der fehlschlug (Python, seine
Umgebung `sys.prefix` oder seine Installation `sys.base_prefix`, das Paket von Latet oder den
Windows-Ordner), den Ordner und den Grund: seinen Besitzer, oder ein Konto, dem seine
Zugriffsliste erlaubt, ihn zu ändern. Der Hilfsprozess führt diesen Code mit dem Token dieses
Rechners aus, daher nur aus Ordnern, die nur Sie, SYSTEM und die Administratoren ändern können und
deren übergeordnete Ordner nur diese, die direkten Mitglieder der Administratoren und die Konten in
`LATET_TRUSTED_ACCOUNTS` ändern können. Prüfen Sie den genannten Ordner mit `icacls "<Ordner>"` und entfernen Sie den Eintrag des anderen
Kontos; oder installieren Sie Python und Latet in eigenen Ordnern, die uvs
`UV_PYTHON_INSTALL_DIR` und `UV_TOOL_DIR` wählen.

### Eine Anfrage wurde blockiert

In Claude Code antwortet die abschließende Prüfung des Plugins in der Unterhaltung:

```text
Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
sent in clear. Nothing was sent.
```

Die Meldung nennt die Art des Werts, der im Klartext gesendet worden wäre, und den Teil der
Unterhaltung, in dem er stand; gesendet wurde nichts. Eine solche Blockierung bedeutet, dass Inhalte
ohne Maskierung in die Anfrage gelangt sind. Führen Sie `latet doctor` aus, und prüfen Sie mit
`latet agents status`, ob Claude Code `applied` (eingerichtet) anzeigt; wenn nicht, führen Sie
`latet agents apply claude` aus, und starten Sie Claude Code neu.

Wenn stattdessen der Daemon eine Anfrage blockiert, zeigt der Agent einen Fehler mit der Meldung
`Latet: sensitive values would leave in clear` (sensible Werte würden im Klartext hinausgehen),
gefolgt von Typ und Länge jedes Werts, etwa `PASSWORD[13]`. Führen Sie `latet dashboard` aus: Dort
steht die blockierte Anfrage so, wie sie gesendet worden wäre, mit den Werten, die sie gestoppt
haben, rot umrahmt. Um weiterzumachen, nehmen Sie den Wert heraus: Beginnen Sie eine neue
Unterhaltung (in Claude Code `/clear`), und verweisen Sie den Agenten auf die Datei oder Variable,
die ihn enthält, statt ihn einzufügen. Nur wenn ein Wert überhaupt nicht geheim ist, etwa ein Test-
oder Beispielwert, klicken Sie darauf, wählen Sie **Nicht sensibel: immer zulassen**, und versuchen
Sie es dann erneut.

Im Gateway-Modus bedeutet ein Fehler mit `Latet: masking failed` oder `Latet: unreadable request body`,
dass der Daemon die Anfrage nicht maskieren konnte und sie deshalb abgelehnt hat.
Das Log des Daemons sagt, warum.

### Der Auto-Modus meldet, sein Prüfer sei vorübergehend nicht verfügbar

Im Auto-Modus fragt Claude Code ein Prüfmodell, ob jede Aktion sicher ist. Wenn Latet diese Prüfung
stoppt, kann der Auto-Modus die Aktion nicht genehmigen: Claude Code lehnt sie ab, mit der Meldung,
der Prüfer sei vorübergehend nicht verfügbar, und fragt Sie nicht. Führen Sie die Aktion selbst aus,
stellen Sie Claude Code auf einen Berechtigungsmodus um, der Sie fragt, oder lassen Sie die Angaben
durch, wie unten beschrieben. Im Dashboard haben diese Anfragen die Antwort `400` und die Markierung
*Auto-Modus-Prüfung*; die Begründung nennt, was die Prüfung enthielt. Zwei Dinge stoppen sie:

- **Ihre Identität:** standardmäßig Ihr Benutzername im Betriebssystem und Ihr Benutzerordner in
  Pfaden. Um sie zum Prüfer durchzulassen, und nur zu ihm, setzen Sie
  `auto_mode_review: {user_name: allow, paths: allow}`, oder verwenden Sie im Dashboard
  *Prüfungen im Auto-Modus von Claude Code* unter **Regeln** → **Erweitert** → **Identität**. Siehe
  [Claude Code](../agents/claude-code/#der-prüfer-des-auto-modus).
- **Ein geheimer Wert oder die Daten anderer Personen,** die nie zum Prüfer gehen. Oft stehen sie in
  Ihrer CLAUDE.md: Claude Code gibt dem Prüfer diese Datei unmaskiert. Verschieben Sie diese Werte
  nach `.env`, und nennen Sie in der Datei den Namen der Variablen. Wenn eine Sitzung beginnt, nennt
  das Plugin die Datei und die Zeilen, und `latet doctor` listet sie auf.

  **Wenn es Platzhalter sind,** schreiben Sie sie in einer [Form, die Latet kennt](../concepts/detection/#platzhalter)
  und unberührt lässt: `postgres://app:<password>@host/db`, `${DB_PASSWORD}`, `YOUR_PASSWORD`,
  `changeme` oder `***`. Ein Platzhalter in einer anderen Form gehört zu den Werten des Projekts, die
  nie maskiert werden (**Regeln**, Projekt wählen, *Zugelassene Werte*); danach
  richtet sich auch `latet doctor`, wenn er im Ordner des Projekts ausgeführt wird.

Behalten Sie die Berechtigungsabfragen bei, statt in den Modus bypass permissions zu wechseln: In
den Befehlen, die er ausführt, arbeitet der Agent mit den echten Werten.

### Jede Anfrage einer Unterhaltung wird blockiert

In Claude Code stoppt eine Meldung wie diese jede Anfrage einer Unterhaltung:

```text
Latet stopped this request before it left your machine: this conversation already held values in
clear before Latet checked it (PASSWORD (user text), EMAIL (tool result)). It began before Latet
was on, or was resumed from then, and every request sends that history again. Start a new
conversation with /clear: from there everything is masked as it comes in. Nothing was sent.
```

Die Unterhaltung begann, bevor Latet aktiv war, oder Sie haben eine aus dieser Zeit fortgesetzt
(`claude --resume`): Die ersten Nachrichten der Unterhaltung, Ihre CLAUDE.md und die Ausgaben ihrer
Tools sind im Klartext in den Verlauf gelangt. Claude Code sendet mit jeder Anfrage den ganzen
Verlauf, daher trägt jede Anfrage diese Werte erneut, und Latet kann nicht wissen, dass sie Ihren
Rechner bereits verlassen hatten. Im Dashboard haben diese Anfragen die Markierung *Aus dem Verlauf*.

Beginnen Sie mit `/clear` eine neue Unterhaltung, oder eine neue Sitzung, mit eingeschaltetem
Latet. Ab dann wird alles maskiert, sobald es hinzukommt.

### Der lokale Prüfer hat eine Anfrage gestoppt

Wenn der [lokale Prüfer](../concepts/llm-review/) eingeschaltet ist, wartet eine Anfrage auf seine
Prüfung und wird nicht gesendet, wenn der Prüfer keine liefern kann. Der Agent, oder Claude Code,
sagt, warum:

- `the local reviewer could not read the user text (its server did not answer: start it, or run
  latet doctor), so it was not sent`: Der Server des Modells hat nicht geantwortet. Starten Sie ihn,
  und prüfen Sie ihn dann mit `latet llm status`. Ein Server, der mit `HTTP 401` antwortet, verlangt
  einen API-Schlüssel: Speichern Sie ihn im Dashboard unter *API-Schlüssel* oder mit `latet llm key`;
- `... (it took longer than llm_review.budget_ms)`: Die Prüfung hat länger gedauert als erlaubt.
  Erhöhen Sie `budget_ms`, oder entfernen Sie die Einstellung, um so lange zu warten, wie die
  Prüfung dauert;
- `the local reviewer could not trust its review of the tool result: the text gives it
  instructions`: Der Text gibt dem Prüfer Anweisungen, und der Prüfer hat nichts darin gefunden,
  traut seiner Prüfung aber nicht. Sehen Sie sich den Text im Dashboard an, bevor Sie ihn erneut
  senden;
- `the local reviewer stopped this tool result: it must not leave even masked`: Mit `block: true`
  hat der Prüfer den ganzen Text als zu sensibel eingestuft, um ihn zu senden, auch maskiert, etwa
  eine Krankenakte.

In Claude Code wird ein gestoppter Prompt nicht gesendet, und eine gestoppte Tool-Ausgabe erreicht
das Modell als `[withheld by Latet: ...]` (von Latet zurückgehalten). Um zu senden, was die
Detektoren maskiert haben, wenn der Prüfer nicht antworten kann, setzen Sie `on_failure: send`.

### Eine Tool-Ausgabe wurde zurückgehalten

`[withheld by Latet: this tool output holds 214 personal values ...]` in der Unterhaltung und die
Markierung *Zurückgehalten* im Dashboard kommen vom
[Dump-Schutz](../concepts/production-data/#der-dump-schutz): Die Ausgabe enthielt mehr Werte über
Personen, als `dump_guard` in einer Ausgabe erlaubt, daher wurde nichts davon gesendet. Bitten Sie
den Agenten um weniger Zeilen oder um Anzahlen und Aggregate; erhöhen Sie `dump_guard`, oder setzen
Sie die Einstellung auf `0`, um den Schutz auszuschalten.

### Meine latet.yaml wirkt nicht

- `latet project show` gibt aus, was im Projekt gilt, und jeden Schlüssel der Datei, der ignoriert
  wird, mit dem Grund: Eine Projektdatei kann nur Schutz hinzufügen, daher werden Werte, die nie
  maskiert werden sollen, ausgeschaltete Regeln und ein ausgeschaltetes Latet dort ignoriert. Sie
  gehören in Ihre eigenen Einstellungen für das Projekt.
- `latet project check` prüft die Datei und endet mit dem Exit-Code 1, wenn etwas ignoriert wird
  oder ein Beispiel fehlschlägt.
- Es gilt nur die nächstgelegene `latet.yaml`, vom Ordner des Agenten bis hinauf zum
  Stammverzeichnis des Repositorys. Siehe [Projekte](../concepts/projects/).

### In einem Projekt wird nichts maskiert

Latet ist dort vielleicht ausgeschaltet: Das Dashboard markiert diese Austausche mit *Latet aus*.
`latet project show` zeigt das an, und `latet project on` schaltet es wieder ein. Ein Projekt mit
*Nur Geheimnisse* maskiert geheime Werte und Ihre Begriffe und lässt personenbezogene Daten durch.

### Ein Wert wird maskiert, der nicht maskiert werden sollte

Das ist ein Fehlalarm. Öffnen Sie im Dashboard die Anfrage, klicken Sie auf den Wert, und wählen Sie
**Nicht sensibel: immer zulassen**, oder fügen Sie den Wert zu `allow_values` in `config.yaml`
hinzu. Um eine ganze Regel für geheime Werte auszuschalten, fügen Sie ihre ID zu
`disabled_secret_rules` hinzu: Zeigt das Dashboard einen Wert als von `gitleaks:generic-api-key`
gefunden, ist die ID `generic-api-key`. Siehe
[Zulassungslisten](../concepts/profiles/#zulassungslisten).

### Ein Wert wird nicht maskiert

- Prüfen Sie das [Profil](../concepts/profiles/): `secrets` maskiert keine personenbezogenen Daten.
- Namen in freiem Text brauchen das optionale
  [lokale Namensmodell](../getting-started/installation/#namenserkennung) oder den
  [lokalen Prüfer](../concepts/llm-review/), und keines von beiden findet garantiert alles: Fügen Sie
  die Namen, auf die es ankommt, als [eigene Begriffe](../concepts/detection/#ihre-eigenen-begriffe) hinzu.
- Fügen Sie den Text in den **Testbereich** des Dashboards ein, um zu sehen, was die aktuellen Regeln
  finden.

### Die Antwort von `claude -p` zeigt Ersatzwerte

Erwartetes Verhalten: Im Print-Modus setzt Claude Code in dem, was es ausgibt, die echten Werte nicht
wieder ein. Dateien, die Tools schreiben, enthalten die echten Werte, und die interaktive Oberfläche
zeigt sie ebenfalls. Siehe [Claude Code](../agents/claude-code/#grenzen). Auf einem Rechner, der mit
einem Latet-Server verbunden ist, wird der Print-Modus zurückgehalten: siehe
[Claude Code hält auf einem verbundenen Rechner jeden Prompt zurück](#claude-code-hält-auf-einem-verbundenen-rechner-jeden-prompt-zurück).

### Befehle sind unter Windows langsam

Jeder `latet`-Befehl braucht Sekunden zum Starten, auch `latet version`, während Ihre Agenten so
schnell arbeiten wie immer. Das liegt an Ihrem Virenscanner. Er prüft die Programme, die er nicht
kennt, bei jedem Start, und der kleine Launcher, den ein Installer für den Befehl `latet` anlegt (bei
uv wie bei pip), ist eines davon: nicht signiert und auf jedem Rechner anders. Mit Windows Defender
kostet das etwa eine halbe Sekunde; manche Virenscanner, etwa Avira, brauchen für jeden Launcher
mehrere Sekunden. Latet selbst startet in einem Bruchteil einer Sekunde, und Ihre Agenten sprechen
mit dem Daemon, der bereits läuft, daher wartet nur der Start eines Befehls.

Zwei Abhilfen:

- **Fügen Sie Latet zu den Ausnahmen Ihres Virenscanners hinzu.** Bei einer Installation mit uv sind
  die Ordner `%APPDATA%\uv\tools\latet` und `%USERPROFILE%\.local\bin` (im zweiten genügen
  `latet.exe` und `latet-daemon.exe`).
- **Oder führen Sie es über Python selbst aus,** das signiert ist und sofort startet: Installieren
  Sie das Wheel des [neuesten Release](https://github.com/Latet-AI/latet/releases/latest) mit
  `py -m pip install "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"`
  (mit dessen Version anstelle von `X.Y.Z`), und führen Sie jeden Befehl als `py -m latet` aus, etwa
  `py -m latet claude`. Die `latet.exe`, die pip anlegt, ist so langsam wie die anderen, verwenden Sie
  sie also nicht. Latet teilt sich dann dieses Python mit Ihren anderen Programmen, statt ein eigenes
  zu haben. Zum Aktualisieren führen Sie dasselbe `py -m pip install` mit dem Wheel des neuen
  Release aus: `latet upgrade` installiert mit uv, als zweite Kopie. Zum Entfernen führen Sie
  `py -m latet global-uninstall` aus und dann `py -m pip uninstall latet`.

Mit einem langsamen Virenscanner schafft es das Claude-Code-Plugin womöglich nicht, den Daemon
rechtzeitig selbst zu starten, und hält Inhalte so lange zurück. Lassen Sie den Daemon bei der
Anmeldung starten (`latet global-install` richtet das ein), oder führen Sie `latet start` aus, bevor
Sie Claude Code öffnen.

### Port 5283 ist bereits belegt

Latet lauscht auf `127.0.0.1:5283`, einem Port, den keine gängige Software verwendet. Wenn ein
anderes Programm ihn belegt hat oder Windows ihn reserviert hat (Hyper-V, WSL und Docker reservieren
Portbereiche, die sich bei jedem Systemstart ändern können), meldet `latet start` das sofort:

```text
Latet cannot listen on 127.0.0.1:5283: in use by another program.
Run `latet global-install` to move it to a free port (your agents are updated too).
```

`latet global-install` wählt den ersten freien Port zwischen 5284 und 5335, speichert ihn in
`config.yaml` und stellt jeden Agenten, den er eingerichtet hat, auf den neuen Port um. Ist keiner
davon frei, wählen Sie selbst einen: `latet global-install --port 6655`. `latet serve --port <port>`
ändert den Port nur für einen einzelnen Lauf im Vordergrund.

### Latet läuft in Docker, und die Agenten erreichen es nicht

Führen Sie `docker exec latet latet status` aus, und prüfen Sie dann, ob der Port auf `127.0.0.1`
veröffentlicht ist und innen wie außen derselbe ist (`LATET_PORT` legt in `compose.yaml` beide
fest). Führen Sie nach einer Portänderung die Befehle `agents merge`, die
`docker exec latet latet agents print` ausgibt, erneut aus. Claude Code mit `LATET_AUTOSTART=0`
startet nie einen eigenen Daemon: Es wartet auf den Container. Hinter einem Proxy: siehe
[Docker](../getting-started/docker/#hinter-einem-proxy).

### "browser requests are not accepted on proxy routes"

Sie haben eine Proxy-URL in einem Webbrowser geöffnet, oder eine Webseite hat versucht, sie zu
erreichen. Das ist beabsichtigt: Der Daemon lehnt Anfragen ab, die von Browsern kommen. Agenten sind
davon nicht betroffen. Um den Datenverkehr anzusehen, verwenden Sie `latet dashboard`.

### "host not allowed"

Die Anfrage hat einen anderen Hostnamen als `127.0.0.1`, `localhost` oder `[::1]` verwendet, und der
Daemon lässt ihn nicht zu. Verwenden Sie einen davon in der Basis-URL.

### Codex läuft nicht über Latet

- Codex vor Version 0.115 ignoriert `openai_base_url`: Aktualisieren Sie Codex. `latet doctor` warnt
  davor, und `latet agents apply codex` lässt eine solche Codex-Version unverändert. Bis dahin
  starten Sie Codex mit `latet codex`, der ihm die Adresse in `OPENAI_BASE_URL` übergibt: Nur diese
  liest es. Hat Codex einen eigenen Anbieter (Azure, das Gateway Ihres Unternehmens), startet
  `latet codex` es nicht und rät, Codex zu aktualisieren.
- Führen Sie `latet agents status` aus: Codex sollte `applied` (eingerichtet) und seine Route
  anzeigen.
- Wenn Sie zwischen der Anmeldung über ChatGPT und einem API-Schlüssel gewechselt oder einen Anbieter
  hinzugefügt haben, führen Sie `latet agents apply codex` erneut aus.

### Claude Code meldet, dass seine Anfragen an Latet vorbeigehen

Das Plugin hat festgestellt, dass Claude Code seine Anfragen woandershin senden soll, und hat seine
Prompts gestoppt: Nichts würde prüfen, was hinausgeht. Die Meldung nennt die Datei, die das
festgelegt hat.

- **Die `.claude/settings.json` dieses Projekts** (oder `.claude/settings.local.json`) setzt
  `ANTHROPIC_BASE_URL`, die Adresse oder den Schalter einer Cloud oder einen Proxy (`HTTPS_PROXY`):
  Ein Repository entscheidet nie, wohin Anfragen gehen, auch nicht über Latet. Verschieben Sie die
  Zeile in Ihre eigenen Einstellungen, und führen Sie `latet agents apply claude` aus, oder starten
  Sie Claude Code mit `latet claude`, dessen Einstellungen Vorrang vor der Adresse des Projekts
  haben. Ein vom Projekt gesetzter Proxy stoppt die Prompts, bis er dort entfernt wird.
- **Mehr als eine Cloud ist eingeschaltet** (etwa `CLAUDE_CODE_USE_BEDROCK` und `CLAUDE_CODE_USE_VERTEX`):
  Behalten Sie eine.
- **Gar keine Adresse**, oder eine Cloud, die direkt erreicht wird (`CLAUDE_CODE_USE_BEDROCK` ohne
  ihre Adresse): Starten Sie Claude Code mit `latet claude`, oder führen Sie
  `latet agents apply claude` aus, dann gilt es für jede Sitzung.
- **Die verwalteten Einstellungen Ihrer Organisation** erzwingen eine Adresse: siehe
  [Unternehmens-Endpunkte](../agents/company-endpoints/#wo-sich-latet-nicht-davorschalten-kann).

### Bedrock meldet, dass die AWS-Anmeldung abgelaufen ist

Latet signiert die Anfragen an Bedrock mit Ihrem AWS-Profil, und die Anmeldung dahinter ist
abgelaufen. Führen Sie den Befehl aus der Meldung aus (`aws sso login --profile work`), und senden
Sie dann erneut: Nichts muss neu gestartet werden. Ohne die AWS CLI liest Latet nur Schlüssel, die in
`~/.aws` stehen; eine SSO-Anmeldung braucht die AWS CLI, und `latet doctor` weist darauf hin. Latet
sucht `aws` in seinem eigenen PATH, dann in `/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`,
`~/.local/bin` und `/snap/bin` (unter Windows in `C:\Program Files\Amazon\AWSCLIV2`). Liegt Ihre AWS
CLI woanders, starten Sie Latet aus einer Shell, in der `aws` läuft.

### Ein Unternehmens-Endpunkt ist nicht erreichbar

Die Anfrage hat Latet erreicht, aber das Ziel, an das Latet sie weiterleitet, hat nicht geantwortet.
Prüfen Sie die Adresse im Dashboard unter Geschützte Agenten → Wohin Anfragen gehen oder mit
`latet config get upstreams`; Claude Code zeigt den Host des Ziels in seiner Fehlermeldung.

### Claude Desktop läuft nicht über den Daemon

Erwartetes Verhalten: Die Desktop-App ignoriert `ANTHROPIC_BASE_URL`. Lokale Sitzungen im Tab Code
der App bekommen das Plugin und seine abschließende Prüfung, aber nicht die Prüfung des Daemons, und
sie werden nicht im Dashboard aufgezeichnet. Siehe [Claude Code](../agents/claude-code/).

### Remote Control ist in Claude Code verschwunden

Erwartetes Verhalten, solange Latet dazwischengeschaltet ist: Claude Code schaltet Remote Control
aus, wenn seine Basis-URL angepasst ist. `latet agents revert claude` bringt Remote Control zurück,
und Claude Code läuft dann nicht mehr über Latet.

### Das Dashboard meldet, dass seine Dateien nicht Teil dieser Installation sind

Sie führen Latet aus einem Quellcode-Checkout aus. Bauen Sie das Dashboard: `npm ci`, dann
`npm run build`, in `dashboard/`. Releases enthalten es. Auch die Seite des Tutorials braucht es:
Bis dahin warnt `latet tutorial`, dass seine Seite nicht angezeigt wird.

### Das Tutorial öffnet sich nicht oder verlangt, neu gestartet zu werden

- **Kein Browser hat sich geöffnet:** Führen Sie `latet tutorial --print-url` aus, und öffnen Sie den
  ausgegebenen Link im Browser Ihrer Wahl. Der Link funktioniert einmal, zwei Minuten lang.
- **Die Seite verlangt, das Tutorial neu zu starten:** Ihr Link wurde bereits verwendet, oder es
  sind mehr als zwei Minuten vergangen. Drücken Sie Strg+C dort, wo das Tutorial läuft, und starten
  Sie es erneut: Die Kapitel, die Sie abgeschlossen haben, bleiben abgeschlossen.
- **Es ist mit einem Fehler stehen geblieben:** Es hat die letzten Zeilen seines Logs ausgegeben und
  bewahrt das ganze Log im Ordner der Sandbox auf, bis Sie es erneut ausführen. Bitte eröffnen Sie
  ein Issue mit diesem Log.

### Ein Upgrade schlägt unter Windows fehl, weil Dateien in Gebrauch sind

Verwenden Sie `latet upgrade`: Er stoppt den Daemon, installiert die neue Version nach dem Ende des
Befehls in einem eigenen Fenster (Windows ersetzt keine Dateien eines laufenden Programms) und startet
Latet wieder.

Ein Latet 0.9.0 oder 0.9.1, das unter Windows mit seinem eigenen `latet upgrade` aktualisiert wurde,
kann danach nicht mehr starten, und `latet` antwortet `No module named 'latet'`: Führen Sie das
Installationsskript erneut aus, es installiert die neueste Version vollständig.

### Das neueste Release lässt sich nicht ermitteln

Die Installer und `latet upgrade` fragen die API von GitHub, welches Release das neueste ist, und
installieren dessen Wheel. Wenn das nicht gelingt, sagen sie, warum, und ändern nichts:

- **Die API von GitHub hat mit 403 geantwortet** (oder 429): Sie nimmt 60 Anfragen pro Stunde von
  einer Adresse an, und ein Büro hinter einer einzigen Adresse kann sie aufbrauchen. Beide lesen dann
  die Seite des neuesten Release, die nicht mitgezählt wird; schlägt auch das fehl, warten Sie eine
  Stunde, oder installieren Sie von Hand (siehe unten).
- **GitHub war nicht erreichbar:** Es gibt keine Verbindung, oder ein Proxy steht im Weg.
- **Das neueste Release hat kein Wheel,** oder es gibt gar keines: Installieren Sie
  [aus dem Quellcode](../getting-started/installation/#aus-dem-quellcode-ausführen).

Um ein Release von Hand zu installieren, nehmen Sie die Adresse seines Wheels von der Seite
[Releases](https://github.com/Latet-AI/latet/releases), und übergeben Sie sie dem Installer in
`LATET_SOURCE` (er aktualisiert auch ein bereits installiertes Latet; führen Sie dann `latet start`
aus, und `latet agents apply claude`, wenn Claude Code dauerhaft über Latet läuft):

```sh
curl -LsSf https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.sh | LATET_SOURCE="latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl" sh
```

Unter Windows (PowerShell):

```powershell
$env:LATET_SOURCE = "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
irm https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.ps1 | iex
```

Ein heruntergeladenes Wheel funktioniert ebenso: sein Pfad anstelle der Adresse.

## FAQ

### Funktioniert es mit einem Abonnement Claude Pro oder Max?

Ja, ohne API-Schlüssel: Dafür ist der Client-Modus da. Claude Code läuft unverändert, Sie verwenden
seine eigene Anmeldung, das Plugin maskiert in Claude Code selbst, und der Daemon leitet jede Anfrage
Byte für Byte weiter. Siehe [Claude Code](../agents/claude-code/#pro--und-max-abonnements).

### Können Hermes Agent oder OpenClaw über Latet mein Claude-Abonnement nutzen?

Nein. Anthropic erlaubt ein Abonnement in Claude Code und in seinen eigenen Apps, und Latet stellt es
keinem anderen Programm zur Verfügung. Autonome Agenten laufen mit einem API-Schlüssel über das
Gateway: mit einem Schlüssel von OpenAI oder OpenRouter (OpenRouter bietet auch Claude-Modelle an)
oder mit einem API-Schlüssel von Anthropic. Siehe
[Hermes Agent, OpenClaw und andere Agenten](../agents/autonomous-agents/).

### Warum schreibt Latet den Datenverkehr von Claude Code nicht einfach um?

Claude Code fügt jeder Anfrage einen Attributionsblock mit einem Fingerabdruck der Unterhaltung
hinzu, und der Leitfaden von Anthropic für Gateways verlangt, dass ein Gateway Anfragen prüft, ohne
sie zu verändern. Deshalb findet die Maskierung in Claude Code statt, bevor die Anfrage
zusammengestellt wird, und der Daemon prüft sie nur. Siehe
[Funktionsweise](../concepts/how-it-works/#client-modus-claude-code).

### Kann das Modell mit Ersatzwerten noch seine Arbeit tun?

Meistens ja. Ersatzwerte behalten das Format des Originals, daher ergibt Code, der einen Schlüssel,
eine E-Mail-Adresse oder eine IBAN verarbeitet, für das Modell weiterhin Sinn. Es erfährt nur nie
den echten Wert. Der echte Wert wird allerdings nur dort wieder eingesetzt, wo der Ersatzwert selbst
steht: Wenn das Modell *Dear Laura* schreibt, weil die Ersatzadresse mit `laura.` beginnt, bleibt
diese Anrede stehen.

### Macht die Maskierung den Prompt-Cache des Anbieters zunichte?

Nein. Derselbe Wert bekommt immer denselben Ersatzwert, und frühere Runden der Unterhaltung werden
genau wie zuvor erneut gesendet, daher kann der Anbieter sie weiterhin aus seinem Cache lesen. Die
Seite **Statistik** des Dashboards zeigt, wie viel von jedem Prompt aus dem Cache kam.

### Wo sehe ich, was tatsächlich gesendet wurde?

Führen Sie `latet dashboard` aus. Siehe [Anleitung zum Dashboard](../dashboard/).

### Wie schalte ich es aus?

`latet global-uninstall` stellt den vorherigen Zustand jedes Agenten wieder her, entfernt den
Autostart bei der Anmeldung und stoppt den Daemon. Tools, die Sie in ihren eigenen Einstellungen auf
Latet gerichtet haben, behalten seine Adresse: Stellen Sie ihre eigene wieder ein. Ein erneutes
`latet global-install` schaltet es wieder ein.

### Sendet Latet irgendetwas irgendwohin?

Nur den eigenen Datenverkehr Ihres Agenten, an den Anbieter (oder den Unternehmens-Endpunkt), den Ihr
Agent bereits verwendet. Alle Ausnahmen geschehen nur, wenn Sie es verlangen: `latet ner install` und
`latet ocr install` laden ihre Modelle einmal von huggingface.co herunter; die Installer und
`latet upgrade` fragen GitHub, welches Release das neueste ist, und laden dessen Wheel von dort
herunter; und der lokale Prüfer wird auf Ihrem eigenen Server befragt. Es gibt keinen Cloud-Dienst
von Latet, keine Telemetrie und keine Nutzungsanalyse.

### Sind meine Daten sicher, sobald Latet installiert ist?

Sicherer, aber nicht sicher. Latet maskiert nur, was seine Detektoren erkennen, und manche
Oberflächen und Dateitypen liegen außerhalb seiner Reichweite. Lesen Sie
[Was geschützt ist und was nicht](../concepts/protection-scope/).

### Was ist mit Bildern und PDFs?

Laden Sie die Modelle für die Texterkennung mit `latet ocr install` herunter (sie laufen auf dem
Extra `ocr`; ein Latet, das ohne dieses Extra installiert wurde, bekommt es mit `latet upgrade`):
Der Text in Screenshots und Bildern wird dann auf Ihrem Rechner gelesen und maskiert, und jeder Wert
wird im Bild mit seinem Ersatzwert überdeckt. Siehe [Screenshots und Bilder](../concepts/images/).

PDFs und andere Dokumente werden nicht gelesen: Sie werden unverändert gesendet, und das Dashboard
markiert diese Anfragen mit *Nicht gelesen*. Hängen Sie keine Dateien mit sensiblen Daten an, oder
setzen Sie `images: block` (das Profil `max` tut dasselbe), damit eine Anfrage, die eine solche
Datei enthält, nicht gesendet wird.

### Ein Bild wurde zurückgehalten

Das Modell hat anstelle eines Bildes *[image withheld by Latet: ...]* bekommen, mit der Begründung:
- *the text recognition models were still loading*: Die Modelle für die Texterkennung wurden noch
  geladen, denn der Daemon war gerade erst gestartet. Senden Sie das Bild erneut.
- *it could not be read*: Das Bild war nicht lesbar, weil es animiert war, größer als 20 MB oder
  40 Megapixel oder kein gültiges Bild.
- *its text could not be read (OcrTooManyTiles)*: Das Bild ist für seine Breite so lang oder für
  seine Höhe so breit, dass es in mehr als 16 Teilen gelesen werden müsste, etwa der Screenshot
  einer Seite, die länger als etwa 29.000 Pixel ist. Nehmen Sie die Seite in mehreren Screenshots
  auf, oder schneiden Sie sie auf das zu, was der Agent braucht.
- *its text could not be read (OcrTooWide)*: Eine Textzeile darin ist für ihre Höhe zu lang, um
  gelesen zu werden, etwa eine Zeile über mehrere Bildschirme. Schneiden Sie das Bild auf das zu,
  was der Agent braucht.
- *its text could not be read (OcrTooLong)*: Das Lesen dauerte länger, als ein Lesevorgang dauern
  darf. Senden Sie es erneut, oder ein kleineres Bild.
- *you withheld it in the Live Reviewer*: Sie haben im Live-Prüfer *Dieses Bild nicht senden* dafür
  angekreuzt.
- *the local reviewer saw a value in it ...*: Der [lokale Prüfer](../concepts/llm-review/#bilder),
  dem das Bild gezeigt wurde, hat darin einen Wert gesehen, den Latet im gelesenen Text nicht finden
  konnte, daher konnte Latet ihn nicht überdecken. Maskieren Sie ihn im Live-Prüfer von Hand, oder
  schneiden Sie ihn aus dem Bild heraus.

### Kann ich Latet zusammen mit meinem Agenten in einer Sandbox ausführen?

Ja. Latet ist selbst keine Sandbox, und es funktioniert innerhalb einer: Installieren Sie es dort, wo
der Agent läuft, damit der Agent es unter `127.0.0.1` erreicht.

### Kann mein Unternehmen Latet kostenlos nutzen?

Ja, unabhängig von seiner Größe, für seine eigene Arbeit, auf beliebig vielen Computern und Servern.
Auch die private und die nicht kommerzielle Nutzung sind kostenlos. Für die Community Edition
brauchen Sie eine kommerzielle Lizenz nur, um Latet zu verkaufen, es anderen als Dienst anzubieten,
es in ein Produkt einzubauen, das Sie verkaufen, oder für einen kommerziellen Fork: Schreiben Sie an
[info@latet.ai](mailto:info@latet.ai). Die Enterprise Edition, die Dateien in den Verzeichnissen
`ee`, bringt, was ein Unternehmen braucht, um Latet zentral zu verwalten, und braucht im
Produktivbetrieb eine kommerzielle Lizenz; das Entwickeln und Testen damit ist kostenlos. Die
Community Edition prüft nie eine Lizenz: Es gibt keine Schlüssel. [LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md) enthält die
Einzelheiten und Beispiele.

### Woher kommt der Name?

*Latet* ist Latein für "es liegt verborgen", wie in *latet anguis in herba*: "eine Schlange liegt im
Gras verborgen". Latet versteckt Ihre geheimen Werte vor aller Augen: Das Modell sieht einen Wert,
der genau wie der echte aussieht.
