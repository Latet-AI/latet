---
title: Claude Code
description: Der Client-Modus für Claude Code, mit Pro- und Max-Abonnements oder API-Schlüsseln, und welche Oberflächen von Claude abgedeckt sind.
---

Claude Code nutzt den **Client-Modus**: Das Latet-Plugin maskiert die Inhalte in Claude Code, und
der lokale Daemon prüft nur. Das funktioniert mit Pro- und Max-Abonnements ohne API-Schlüssel und
ebenso mit API-Schlüsseln.

## Verbundenes Windows-Terminal

Der verbundene Server-Hilfsprozess unterstützt Claude Code in einem Windows-Terminal
mit vertrauenswürdigen Benutzereinstellungen und der von Latet installierten privaten
Bindung. Dies erweitert die verbundene Unterstützung nicht auf Desktop, VS Code,
JetBrains, Linux oder macOS. Unter Linux und macOS sagen `latet connect`, `latet claude`,
`latet agents apply claude` und `latet global-install` das vorab, die Einstellungen von
Claude Code erhalten kein Token, und das Plugin hält jeden Prompt zurück; `latet codex` und
`latet opencode` gehen über den Server. Folgen Sie der [Verbindungsanleitung](../../getting-started/server/)
für `latet connect`, `latet agents apply claude`, Tokenspeicherung und `latet disconnect`.

Auf einem verbundenen Rechner enthält `LATET_BRIDGE_TOKEN` in den Einstellungen und der Umgebung
von Claude Code das Token dieses Rechners, und jeder Befehl, den Claude Code ausführt, kann es lesen.
Von überall, wo der Server erreichbar ist, öffnet das Token die Bridge des Plugins und den Proxy Ihres
Bereichs: Wer es besitzt, kann in Ihrem Bereich Texte maskieren lassen, die Ersatzwerte, die er hat,
wiederherstellen lassen und Anfragen darüber senden. Es öffnet nie Ihr Dashboard, in dem jedes
Original angezeigt werden kann, ändert nie Ihre Identität und verringert nie Ihren Schutz: Die einzige
Änderung, die es an Ihren Einstellungen vornehmen kann, ist ein Ziel für eine Adresse hinzuzufügen, was
nichts verringert. Für das Dashboard und die Identität verlangt der Server zusätzlich den
Rechnerschlüssel, der im Anmeldedatenspeicher (oder in der privaten Tokendatei) bleibt und nie einem
Agenten gegeben wird. Der Schlüssel hindert ein nach außen gelangtes Token (aus der Umgebung, den
Einstellungen, einem Log oder mit einem einzigen `curl`) daran, das Dashboard zu öffnen; einen Befehl,
der geschrieben wurde, um beide aus dem Anmeldedatenspeicher zu lesen, hält er nicht auf. Auf einem
verbundenen Rechner verweigern `latet dashboard` und `latet identity send` die Ausführung in einem
Agenten. Wenn das Token nach außen gelangt sein kann, widerruft `latet disconnect` es.

Auf einem verbundenen Rechner wird der Print-Modus (`claude -p`) nicht unterstützt: das
Plugin hält seine Anfragen zurück. Verwenden Sie das interaktive Terminal. Hält das Plugin eine
Anfrage zurück, sagt es warum: der Print-Modus oder eine andere Oberfläche; eine fehlende oder
veraltete Bindung des Hilfsprozesses (`latet agents apply claude`); ein Server, der nicht
antwortete (`latet status`); oder ein Token, das der Server nicht mehr annimmt
(`latet disconnect`, dann `latet connect` mit einem neuen Code eines Administrators).
Das Plugin maskiert die Beschreibungen der Tools von Claude Code; auf einem Server liest die
abschließende Prüfung auch ihre Beschreibungen, Parameterschemas und Beispiele, und ein darin
im Klartext gebliebener Wert stoppt die Anfrage ([Schutzumfang](../../concepts/protection-scope/)). Die folgenden Abdeckungstabellen und Daemon-Anweisungen
beschreiben **lokales Latet**; sie erweitern diese Hilfsprozessunterstützung nicht.

## Abdeckung

| Oberfläche | Plugin | Daemon, nur prüfend | Hinweise |
| --- | --- | --- | --- |
| Claude Code CLI (Terminal) | ✅ | ✅ | Vollständiger Client-Modus |
| Erweiterungen für VS Code und JetBrains | ✅ | ✅ | Dasselbe Claude Code, dieselben Plugins und Einstellungen |
| Code-Tab von Claude Desktop (lokale Sitzungen), mit Anmeldung bei Claude | ✅ | ❌ | Nur das Plugin: Die App ignoriert `ANTHROPIC_BASE_URL`, daher prüft kein Daemon hinter dem Plugin, und im Dashboard wird nichts aufgezeichnet |
| Chat und Cowork in Claude Desktop, mit Anmeldung bei Claude | ❌ | ❌ | Nicht abgedeckt: Sie laden die Plugins von Claude Code nicht und laufen nicht über Latet |
| Claude Desktop über das Gateway eines Drittanbieters: Chat, Cowork und Code | Code-Tab | ✅ | Jede Anfrage läuft über Latet und wird geprüft; maskiert wird nur im Code-Tab |
| Cloud-Sitzungen | ❌ | ❌ | Nicht abgedeckt: Sie laufen auf der Infrastruktur von Anthropic, nicht auf Ihrem Rechner |

**Claude Desktop, zwei Varianten.** Mit Anmeldung bei Claude, mit einem Pro- oder Max-Abonnement,
lädt der Code-Tab das Plugin von Latet, das die Inhalte maskiert und vor jeder Anfrage selbst eine
abschließende Prüfung vornimmt. Die App ignoriert allerdings `ANTHROPIC_BASE_URL`, daher prüft kein
Daemon die Anfrage hinter dem Plugin, und im Dashboard wird nichts aufgezeichnet. Chat und Cowork
laden die Plugins von Claude Code nicht und laufen nicht über Latet: Sie sind nicht abgedeckt.

Über das Gateway eines Drittanbieters ist Claude Desktop nicht bei Claude angemeldet, es gibt also
kein Pro- oder Max-Abonnement: Die Anfragen werden nach Verbrauch abgerechnet, über einen
API-Schlüssel von Anthropic oder über den Anbieter Ihres Unternehmens. Ist Latet als Gateway
eingestellt, senden Chat, Cowork und Code jede Anfrage darüber. Diese Anfragen erstellt die Engine
von Claude Code, daher prüft Latet jede und stoppt eine, die einen geheimen Wert oder
personenbezogene Daten im Klartext enthalten würde, maskiert sie aber nicht: Der
[Leitfaden für Gateways](https://code.claude.com/docs/en/llm-gateway-protocol) von Anthropic
verlangt von einem Gateway, die Anfragen von Claude Code zu untersuchen, nicht sie zu verändern.
Der Code-Tab erhält beides, die Maskierung des Plugins und die Prüfung des Daemons. Wie Sie das
einrichten, steht unter [Claude Desktop über ein Gateway](../other-tools/#claude-desktop-über-ein-gateway).

Bei der Arbeit läuft Claude Code oft über das Gateway Ihres Unternehmens, Amazon Bedrock, Google
Vertex oder Microsoft Foundry: Latet liest diese Route aus Ihren Einstellungen und schaltet sich
davor, samt Schlüsseln. Siehe [Unternehmens-Endpunkte](../company-endpoints/).

Live getestet: die CLI mit einem Abonnement. Die IDE-Erweiterungen und der Code-Tab von Claude
Desktop nutzen dasselbe Plugin und dieselben Einstellungen; wenn dort etwas nicht funktioniert,
[eröffnen Sie bitte ein Issue](https://github.com/Latet-AI/latet/issues). Cursor ist nicht abgedeckt:
Seine Anfragen an das Modell werden von den Servern von Cursor gesendet, nicht von Ihrem Rechner.

## Pro- und Max-Abonnements

Der Client-Modus funktioniert mit Claude Code, wenn Sie mit einem Pro- oder Max-Abonnement
angemeldet sind, ohne API-Schlüssel, und ebenso mit einem API-Schlüssel. Die Seite
[Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) von Anthropic legt
fest, wie ein Abonnement genutzt werden darf. Punkt für Punkt:

| Die Bedingungen von Anthropic | Was Latet tut |
| --- | --- |
| Die Anmeldung mit einem Abonnement ist für die gewöhnliche Nutzung von Claude Code und der eigenen Apps von Anthropic gedacht. | Ihr Abonnement wird nur in Claude Code genutzt. Codex, OpenCode, Hermes Agent und alle anderen Agenten laufen mit ihrer eigenen Anmeldung über das Gateway: einem API-Schlüssel oder, bei Codex, der Anmeldung mit ChatGPT. |
| Die Binärdatei von Claude Code darf nicht verändert werden. | Claude Code läuft so, wie Anthropic es veröffentlicht. Latet fügt ein Plugin hinzu, über die eigenen Plugin-Befehle von Claude Code, und einige Umgebungseinstellungen in `settings.json`. |
| Die Anmeldung muss über den eigenen Ablauf von Anthropic erfolgen; Entwickler dürfen Zugangsdaten für Claude.ai weder sammeln noch speichern noch als Zwischenstelle weitergeben. | Sie melden sich über den eigenen Anmeldevorgang von Claude Code an. Latet fragt nie nach Ihren Zugangsdaten, liest sie nie für sich und speichert sie nie. |
| Die Regeln hindern Sie nicht daran, sich in der unveränderten Binärdatei von Claude Code mit Ihrem eigenen Abonnement anzumelden. | Genau so wird Latet genutzt: Ihr Claude Code, Ihre Anmeldung. |

**Der Weg einer Anfrage.** Wenn `ANTHROPIC_BASE_URL` auf den lokalen Daemon zeigt, sendet Claude
Code seine Anfragen über ihn, wie über jedes Gateway: Anthropic dokumentiert ein
[Gateway](https://code.claude.com/docs/en/llm-gateway) mit Anmeldung per Abonnement, und sein
[Leitfaden für Gateways](https://code.claude.com/docs/en/llm-gateway-protocol) verlangt von einem
Gateway, Anfragen unverändert weiterzuleiten und sie zu untersuchen, ohne sie zu verändern. Genau
das tut der Daemon: Der Inhalt jeder Anfrage erreicht Anthropic Byte für Byte, und ihre Header so,
wie Claude Code sie geschrieben hat, mit Ihrem Token darin, ohne zwei Arten, die Latet entfernt und im
Dashboard zählt: die Telemetrie- und Tracing-Header, die sein SDK hinzufügt (`x-stainless-*`,
`traceparent`...), und die eigenen Header von Claude Code, die freien Text enthalten können (die IDs
eines Subagenten, die seinen Namen enthalten können, Werte, die Claude Code aus seiner Umgebung
übernimmt, und ein Hinweis für Gateways, der die ausgeführten Werkzeuge nennt). [Header, die Latet entfernt](../../getting-started/upgrading-to-0-11/#header-die-latet-entfernt) listet sie alle auf. Das Token wird nie gespeichert,
protokolliert oder im Dashboard angezeigt.

**Warum nichts umgeschrieben werden muss.** Claude Code fügt dem System-Prompt jeder Anfrage einen
Attributionsblock mit einem Fingerabdruck hinzu, der aus der Unterhaltung abgeleitet ist. Ein Proxy,
der Anfragen umschreibt, würde verändern, was Claude Code gesendet hat. Das Plugin maskiert
stattdessen in Claude Code, bevor die Anfrage erstellt wird, sodass der Daemon sie nur prüft: Er kann
eine Anfrage durchlassen oder blockieren, nie verändern. Ein Beispiel Schritt für Schritt finden Sie
unter [Funktionsweise](../../concepts/how-it-works/).

Dies beschreibt, wie Latet funktioniert; es ist keine Rechtsberatung, und maßgeblich sind die
Bedingungen von Anthropic.

## Was das Plugin tut

Das Plugin überlässt das Erkennen und Maskieren dem lokalen Daemon; in Claude Code selbst wird
nichts erkannt.

- **Maskiert Inhalte dort, wo sie entstehen**, bevor sie in die Unterhaltung gelangen: die Prompts,
  die Sie eingeben, den System-Prompt, `CLAUDE.md` und anderen Kontext, den Claude Code hinzufügt,
  Anhänge, Skill-Prompts, Tool-Beschreibungen und jedes Tool-Ergebnis (Dateiinhalte, Befehlsausgaben,
  Hook-Ausgaben).
- **Setzt die Originale in den Argumenten von Tool-Aufrufen wieder ein**, bevor ein Tool läuft,
  sodass Read, Edit, Write, Bash und Ihre lokalen MCP-Server mit den echten Werten arbeiten und die
  Dateien auf der Festplatte stimmen.
- **Behält die Ersatzwerte bei, wo Argumente Ihren Rechner verlassen**: WebFetch, WebSearch und
  claude.ai-Connectors erhalten die Ersatzwerte, die das Modell geschrieben hat. Auch Subagenten
  erhalten sie und arbeiten damit wie das Modell.
- **Stoppt ein Tool außerhalb Ihres Rechners, das mit einem Ersatzwert arbeiten würde**: Ein Aufruf
  von WebSearch, WebFetch oder einem claude.ai-Connector mit einem Ersatzwert darin würde nach jemand
  anderem suchen, die falsche Seite öffnen oder an eine Adresse schreiben, die es nicht gibt. Er wird
  nicht ausgeführt; das Modell erfährt, warum, und fragt Sie, ob der Wert im Klartext hinausgehen
  darf. Das Dashboard zeigt den Aufruf, mit *Nicht sensibel* für jeden Wert.
- **Setzt in das, was Sie sehen, die echten Werte wieder ein**: Ihre Nachrichten, die Antworten des
  Modells, die Befehle und Tool-Aufrufe auf dem Bildschirm (auch eine eingeklappte Gruppe davon,
  während sie läuft) und was sie ausgegeben haben, enthalten die echten Werte. Was das Modell liest,
  ändert sich nicht.
- **Prüft jede Anfrage, bevor sie gesendet wird.** Wenn ein sensibler Wert im Klartext
  hinauszugehen droht, wird die Anfrage nicht gesendet, und Sie sehen stattdessen eine Meldung:

  ```text
  Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
  sent in clear. Nothing was sent.
  ```

  Das Dashboard zeigt die Anfrage, mit dem Wert, der sie gestoppt hat, rot umrahmt: Wenn er nicht
  sensibel ist, klicken Sie darauf, wählen Sie *Nicht sensibel*, und versuchen Sie es dann erneut.

  Wenn die Werte im Klartext in einer Unterhaltung stehen, die begann, bevor Latet aktiv war, sagt
  die Meldung das und bittet Sie, mit `/clear` eine neue Unterhaltung zu beginnen (siehe
  [Fehlerbehebung](../../troubleshooting/#jede-anfrage-einer-unterhaltung-wird-blockiert)).

Wenn der Daemon nicht läuft, startet das Plugin ihn. Wenn das fehlschlägt, hält das Plugin Inhalte
zurück, statt sie unmaskiert zu senden: Ihr Prompt wird nicht gesendet, Tool-Ergebnisse werden durch
einen Hinweis ersetzt, und Tool-Aufrufe werden abgelehnt, bis der Daemon wieder da ist.
`latet doctor` sagt Ihnen, was nicht stimmt.

## Was der Daemon tut

Wenn `ANTHROPIC_BASE_URL` auf den Daemon zeigt, läuft jede Anfrage über ihn. Der Daemon:

- prüft die ausgehende Anfrage auf sensible Werte im Klartext und blockiert sie, wenn er einen
  findet: Claude Code zeigt dann einen API-Fehler mit der Meldung
  `Latet: sensitive values would leave in clear`;
- liest jeden anderen Teil davon: Ein Header, ein Abfrageparameter oder ein Element des Inhalts, das
  kein Profil nennt, wird abgelehnt
  ([Jeder Teil einer Anfrage](../../concepts/protection-scope/#jeder-teil-einer-anfrage)), außer einem
  neuen Element des Inhalts mit Text, einem neuen `anthropic-*`-Header oder einem neuen
  `anthropic-beta`-Wert, die durchgehen, sobald die abschließende Prüfung sie gelesen hat, und einem
  neuen `x-claude-code-*`-Header, den er entfernt und zählt;
- leitet sonst den Inhalt Byte für Byte weiter, und die Header ohne jene, die er entfernt und zählt
  ([Header, die Latet entfernt](../../getting-started/upgrading-to-0-11/#header-die-latet-entfernt));
- zeichnet den Austausch für das [Dashboard](../../dashboard/) auf, ohne seine Zugangsdaten.

Er schreibt nie eine Anfrage von Claude Code um, und auch keine, die das Token eines
Claude-Abonnements enthält, egal wer sie sendet.

## Jede Sitzung oder nur eine

Es gibt zwei Wege, Claude Code über Latet laufen zu lassen:

| | `latet global-install`, `latet agents apply claude` | `latet claude` |
| --- | --- | --- |
| Was es abdeckt | Jede Sitzung von Claude Code: das Terminal, VS Code, JetBrains und den Code-Tab von Desktop (nur das Plugin) | Nur diese eine Sitzung, im Terminal |
| Was es ändert | Installiert das Plugin und fügt `settings.json` die Einstellungen unten hinzu, nachdem die Datei gesichert wurde | Nichts: Das Plugin und die Einstellungen erhält nur diese Sitzung |
| Rückgängig machen | `latet agents revert claude` oder `latet global-uninstall` | Die Sitzung beenden |
| `latet doctor` | Prüft das Plugin und die Einstellungen | Meldet Claude Code als nicht angewendet, was zu erwarten ist |

`latet agents apply claude` leitet nur Claude Code über Latet: Führen Sie es aus, wenn Sie Claude
Code nach `latet global-install` installieren oder nachdem Sie den Port geändert haben.
`latet claude` startet bei Bedarf den Daemon und gibt jedes Argument an `claude` weiter:

```sh
latet claude
latet claude --continue
```

`claude`, allein gestartet, ohne `latet global-install`, läuft nicht über Latet; in einer Shell, in
der `latet env` `ANTHROPIC_BASE_URL` gesetzt hat, erreicht es Latet ohne das Plugin, und seine
Anfragen werden nur geprüft. `latet run claude` ist dasselbe wie `latet claude`: Claude Code braucht
außer der Adresse auch das Plugin, und `latet run` allein würde ihm nur die Adresse geben.

### Sein Projekt und eigene Header

Latet wendet die Einstellungen eines Projekts nur dann vollständig an, wenn es dem Projekt der Sitzung
vertraut ([Projekte](../../concepts/projects/#vertrauenswürdig-und-behauptet)):

- **Mit `latet claude` gestartet,** führt die Sitzung einen Startschlüssel mit, den `latet claude` in
  der privaten Einstellungsdatei dieser Sitzung zu `ANTHROPIC_CUSTOM_HEADERS` hinzufügt. Wo Ihre
  verwalteten oder Projekteinstellungen, oder die Einstellungsquellen, die Sie auf der Kommandozeile
  wählen, unklar machen würden, welche Einstellungen gelten, fügt es keinen hinzu, sagt das, und das
  Projekt der Sitzung ist nur behauptet.
- **Mit `latet global-install` eingerichtet,** bindet das Plugin jede Sitzung an ihr Projekt; die
  Bindung endet nach 30 Minuten ohne Aufruf des Plugins. Die Aufrufe, die Claude Code beim Start
  macht, vor dem ersten Aufruf des Plugins, werden nur mit Ihren eigenen Regeln entschieden.

Header, die Sie in `ANTHROPIC_CUSTOM_HEADERS` setzen, werden abgelehnt, jeder beim Namen genannt, bis
eine genaue Ausnahme ihn durchlässt, eine für jede Art von Aufruf, die ihn sendet
(`anthropic.messages.1`, `anthropic.count_tokens.1`, `anthropic.models.1`): siehe
[Umstieg auf 0.11](../../getting-started/upgrading-to-0-11/#header-die-sie-für-ihre-agenten-setzen).
Einer, dessen Name mit `anthropic-` beginnt, braucht keine: Er geht durch, sobald die abschließende
Prüfung ihn gelesen hat, wie ein neuer Header von Claude Code selbst; einer, dessen Name mit
`x-claude-code-` beginnt, wird entfernt.

## Was global-install ändert

`latet global-install` oder `latet agents apply claude` tut zwei Dinge:

1. Es installiert das Plugin über die eigenen Plugin-Befehle von Claude Code, aus einem lokalen
   Marketplace im Datenordner von Latet.
2. Es fügt `~/.claude/settings.json` (oder `$CLAUDE_CONFIG_DIR/settings.json`) einen `env`-Block
   hinzu, nachdem es die Datei gesichert hat:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/anthropic",
    "LATET_BRIDGE_URL": "http://127.0.0.1:5283",
    "LATET_BRIDGE_TOKEN": "<a random token, created when first needed>",
    "ENABLE_TOOL_SEARCH": "true",
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0"
  }
}
```

- `ANTHROPIC_BASE_URL` leitet jede Anfrage über den Daemon. Bei dem Gateway Ihres Unternehmens,
  Amazon Bedrock, Google Vertex oder Microsoft Foundry schreibt Latet stattdessen die Variable
  dieser Route (siehe [Unternehmens-Endpunkte](../company-endpoints/)); bei einer Adresse auf diesem
  Rechner lässt es Ihre, wie sie ist.
- `LATET_BRIDGE_URL` und `LATET_BRIDGE_TOKEN` sagen dem Plugin, wo der Daemon ist, und weisen dem
  Daemon nach, dass das Plugin Ihres ist.
- `ENABLE_TOOL_SEARCH` lässt die MCP-Tool-Suche eingeschaltet: Claude Code schaltet sie aus, wenn
  eine eigene Base-URL gesetzt ist.
- `CLAUDE_CODE_AUTO_MODE_SERVER` verlegt die Auto-Modus-Prüfung in Anfragen, die Latet kontrollieren
  kann. Ohne diese Zeile reist die Prüfung als zusätzliches Feld jeder Anfrage mit, außerhalb der
  Reichweite von Plugin und Daemon.
  `latet agents apply claude --keep-classifier-context` lässt diese Zeile weg.

Starten Sie Claude Code danach neu. `latet agents status` zeigt, ob es angewendet ist, und
`latet agents revert claude` macht beide Schritte rückgängig.

### Der Prüfer des Auto-Modus

Im Auto-Modus fragt Claude Code ein Prüfmodell, ob jede Aktion sicher ist. Der Prüfer erhält Ihre
CLAUDE.md, die Aktion, Ihren Benutzernamen im Betriebssystem (als *User identity*) und die Pfade
Ihres Home- und Arbeitsverzeichnisses, in einem Text, den das Plugin nicht erreichen kann. Er nutzt
sie, um Ihre Sachen von denen anderer Personen zu unterscheiden: Ein Push auf einen Branch
`yourname/...` ist Ihrer, einer auf `someone-else/...` nicht. Der Daemon prüft diese Anfragen wie
jede andere, und `auto_mode_review` entscheidet, Angabe für Angabe, was von Ihrer Identität den
Prüfer erreichen darf:

| Angabe | Was das ist |
| --- | --- |
| `user_name` | Ihr Benutzername im Betriebssystem, die *User identity* des Prüfers. |
| `paths` | Der Name Ihres Benutzerordners in den Pfaden Ihres Home- und Arbeitsverzeichnisses. |
| `host_name` | Der Hostname dieses Rechners, wenn Ihre CLAUDE.md oder die Aktion ihn enthält. |
| `git_author` | Ihr Name und Ihre E-Mail-Adresse wie in Ihrem Git-Autor, und die Domain dieser E-Mail-Adresse, wenn es die Ihres Unternehmens ist, sofern Ihre CLAUDE.md oder die Aktion sie enthält. |

Jede Angabe ist `block` (die Voreinstellung) oder `allow`. Eine Prüfung, die eine blockierte Angabe
enthalten würde, wird gestoppt, sodass die Angabe lokal bleibt, und der Auto-Modus kann die Aktion
nicht genehmigen. Claude Code lehnt sie dann ab, meldet seinen Prüfer als vorübergehend nicht
verfügbar und fragt Sie nicht: Führen Sie die Aktion selbst aus, oder stellen Sie Claude Code auf
einen Berechtigungsmodus um, der Sie fragt. Eine zugelassene Angabe erreicht den Prüfer, und nur
den Prüfer. `auto_mode_review: block` und `auto_mode_review: allow-identity` setzen alle Angaben
auf einmal; einzeln sieht es so aus:

```yaml
auto_mode_review:
  user_name: allow
  paths: allow
  host_name: block
  git_author: block
```

Geheime Werte und Daten anderer Personen werden nie durchgelassen: Ein Schlüssel oder die
E-Mail-Adresse eines Kunden in der Aktion stoppt die Prüfung, was auch immer Sie wählen. Ihre
eigene E-Mail-Adresse zählt zu Ihrem Git-Autor.

**Halten Sie geheime Werte aus Ihrer CLAUDE.md heraus.** Claude Code gibt dem Prüfer Ihre CLAUDE.md
so, wie sie auf der Festplatte steht, in einer Anfrage, die es selbst erstellt, sodass das Plugin
sie dort nicht maskieren kann, und der Daemon verändert die Anfragen von Claude Code nie. Ein
Passwort oder die E-Mail-Adresse eines Kunden in der Datei stoppt dann jede Prüfung, und der
Auto-Modus lehnt jede Aktion ab, die eine Prüfung braucht. Das Modell, das die Arbeit erledigt, ist
nicht betroffen: Es erhält Ihre CLAUDE.md maskiert. Verschieben Sie diese Werte in `.env`, und
nennen Sie stattdessen in der Datei die Variable (*die Staging-Datenbank steht in `DATABASE_URL`*).
Das Plugin weist darauf hin, wenn eine Sitzung beginnt, und `latet doctor` listet jeden Wert nach
Zeile und Typ auf, nie den Wert selbst.

Standardmäßig bleibt jede Angabe lokal; `latet global-install --auto-mode-review user_name,paths`
lässt sie einzeln durch. Später ändern Sie das auf der Seite **Regeln** des Dashboards und in
`config.yaml`. Wenn eine Prüfung gestoppt wird, zeigt das Dashboard die Angaben, die sie enthielt,
und die Einstellung, die sie durchlässt.

**Ein Ersatzname.** Mit `user_name: surrogate` gibt Latet Claude Code den Ersatzwert Ihres
Benutzernamens in `USERNAME` und `USER`. Claude Code nennt dem Prüfer diesen Namen, denselben
Ersatznamen, den das Modell überall sonst sieht, und eine Prüfung, die noch Ihren echten
Benutzernamen enthält, wird gestoppt. Führen Sie nach dieser Wahl `latet agents apply claude` aus,
und starten Sie Claude Code dann neu. Auch die Befehle, die Claude Code ausführt, sehen den
Ersatznamen, in `%USERNAME%`, `$USER` und in `getpass.getuser()` von Python; `whoami`, Ihr
Home-Verzeichnis und der Git-Autor bleiben, wie sie sind. Das hilft, wenn Ihr Benutzername und Ihr
Benutzerordner verschieden sind, was unter Windows häufig ist, zusammen mit `paths: allow`: Der
Prüfer erhält dann den Namen Ihres Ordners, aber nie Ihren Benutzernamen. Sind beide gleich,
enthalten die Pfade ohnehin den echten Namen.

## Grenzen

- **Der Print-Modus zeigt die Ersatzwerte.** In `claude -p` enthält die ausgegebene Antwort
  Ersatzwerte, weil nur die interaktive Oberfläche in das, was auf dem Bildschirm steht, die echten
  Werte wieder einsetzt. Dateien, die Tools schreiben, erhalten trotzdem die echten Werte.
- **Claude Desktop mit Anmeldung bei Claude**: Der Code-Tab erhält das Plugin und dessen
  abschließende Prüfung, aber nicht den Daemon dahinter, und im Dashboard wird nichts aufgezeichnet;
  Chat und Cowork sind nicht abgedeckt. Über das Gateway eines Drittanbieters wird jede Anfrage
  geprüft, und maskiert wird nur im Code-Tab: siehe [Abdeckung](#abdeckung).
- **Remote Control ist aus**, solange Latet dazwischengeschaltet ist: Claude Code schaltet es aus,
  wenn eine eigene Base-URL gesetzt ist.
- **Web-Tools und claude.ai-Connectors können keinen maskierten Wert verwenden.** Ihre Argumente
  verlassen Ihren Rechner, also würden sie den Ersatzwert erhalten: Ein solcher Aufruf wird
  gestoppt, und Sie entscheiden, ob der Wert im Klartext hinausgehen darf (*Nicht sensibel*).
  Subagenten bleiben auf Ihrem Rechner und arbeiten wie das Modell mit Ersatzwerten. Die Ergebnisse
  von Connectors erreichen Claude Code über die Server von Anthropic, ihr Inhalt ist also dort,
  bevor Latet ihn sieht. Siehe
  [Was geschützt ist und was nicht](../../concepts/protection-scope/).
- **Bilder**, mit der optionalen [Texterkennung](../../concepts/images/): Bilder, die Tools
  zurückgeben, werden durch ihre maskierte Kopie ersetzt; ein Screenshot mit Werten darin, den Sie
  einfügen, wird aus dem Prompt genommen, und Claude erhält den Pfad einer maskierten Kopie, um sie
  mit Read zu öffnen. Ohne Texterkennung wird eine Anfrage mit einem Bild nicht gesendet. Da Latet
  die Anfragen von Claude Code nur prüft, werden ein Foto mit Standort, Herstellerangaben der Kamera
  oder einem Bild darin sowie ein ursprüngliches WebP-Bild abgelehnt
  ([Fotos und ihre Metadaten](../../concepts/images/#fotos-und-ihre-metadaten)). **PDFs** werden nicht
  gelesen: Eine Anfrage, die eines enthält, wird nicht gesendet.
