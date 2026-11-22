---
title: Umstieg auf 0.11
description: Was Latet 0.11 ablehnt, das 0.10 gesendet hat, was Sie vor dem Upgrade prüfen und wie Sie zurückgehen.
---

Latet 0.11.0 liest jeden Teil einer Anfrage, bevor es sie sendet: den Pfad, die Abfrageparameter,
die Header und den Body. Eine Anfrage mit einem Teil, den Latet weder prüft noch durch eine Regel
oder eine von Ihnen genehmigte Ausnahme durchlässt, wird nicht gesendet. Ein leerer Body reicht
nicht mehr aus, um eine Anfrage durchzulassen. Die Zugangsdaten, die Ihr Anbieter braucht, und die
Teile, die ein Anbieter signiert, folgen eigenen Regeln. Das gilt auf Ihrem Rechner genauso wie auf
einem Latet-Server.

Ein Aufruf, den 0.10 ungelesen gesendet hat, kann jetzt also abgelehnt werden. Diese Seite zeigt, was
sich ändert, was `latet doctor` vor und nach dem Upgrade für Sie prüft und was in jedem Fall zu tun ist.

## Was 0.11 ablehnt, das 0.10 gesendet hat

**Aufrufe ohne Body.** Das Auflisten der Modelle und das Lesen eines Modells gehen bei jedem Anbieter
durch, den Latet kennt: Anthropic, OpenAI, OpenRouter, ChatGPT (die Modellliste von Codex), Gemini
und die Inferenzprofile von Bedrock. Auch ihre Abfrageparameter werden gelesen (`limit`, `after_id`,
`pageToken`, `maxResults`...). Jeder andere Aufruf wird abgelehnt: Dateien und Uploads, Batches,
Audio, Bilderzeugung, Moderation, das Lesen oder Löschen einer gespeicherten Antwort, das Lesen eines
Gemini-Caches, die Endpunkte eines Kontos. Der Agent erfährt es, und nichts wird gesendet:

```text
Latet: request envelope refused (envelope-route): route /{destination}/{unsupported-route}; this version of Latet does not support this call, so change the agent or its settings so that it does not make it. Nothing was sent; run `latet dashboard` to see where it was
```

Eine Ausnahme nennt einen Header, einen Abfrageparameter oder ein Element des Bodys, nie eine ganze
Route: In 0.11.0 lässt keine Ausnahme einen dieser Aufrufe durch. Ändern Sie die Einstellungen des
Agenten so, dass er den Aufruf nicht macht, oder führen Sie ihn ohne Latet aus, an die Adresse des
Anbieters, im Wissen, dass Latet nicht sieht, was er sendet.

**Abfrageparameter.** Ein Abfrageparameter, den kein Profil nennt, wird abgelehnt, und dem Agenten
wird sein Name genannt, nie sein Wert:

```text
Latet: request envelope refused (envelope-query-unknown): query parameter drift. Nothing was sent; run `latet dashboard` to see where it was
```

**Header.** Jeder Header wird nach einer eigenen Regel gelesen: Zugangsdaten werden gesendet, wie sie
kamen, und nie protokolliert; der eigene Header eines Agenten muss die Form haben, die dieser Agent
ihm gibt (eine Sitzungs-ID muss zum Beispiel eine UUID sein); ein Header, der freien Text enthalten
kann, wie `user-agent`, durchläuft die abschließende Prüfung. Ein Header, den kein Profil nennt, wird
abgelehnt, und dem Agenten wird sein Name genannt, nie sein Wert:

```text
Latet: request envelope refused (envelope-header-unknown): header x-team. Nothing was sent; run `latet dashboard` to see where it was
```

Bei einer Anfrage, die Latet nur prüft (Claude Code, Bedrock, Vertex), werden die Header
durchgelassen, die Claude Code mit neuen Funktionen hinzufügt, wie es sein Leitfaden für Gateways
verlangt: Ein neuer `anthropic-*`-Header oder ein `anthropic-beta`-Wert einer neuen Form geht durch,
sobald die abschließende Prüfung ihn gelesen hat, im Dashboard angezeigt als *nur geprüft*, und ein
neuer `x-claude-code-*`-Header wird entfernt. Bis zu acht solcher `anthropic-*`-Header gehen durch,
jeder mit einem Namen aus Kleinbuchstaben, Ziffern und Bindestrichen, mit höchstens 48 Zeichen nach
`anthropic-` und keinem Wort zwischen den Bindestrichen, das länger als 16 Zeichen ist, und mit einem
Wert aus 1 bis 1.024 druckbaren Zeichen; ein geheimer Wert oder einer Ihrer Werte darin wird weiterhin abgelehnt. Bei einer Anfrage, die Latet umschreibt, etwa der eines
Anthropic-SDKs, werden sie abgelehnt wie jeder andere Header, den kein Profil nennt.

Manche Header werden stattdessen entfernt: die Telemetrie- und Tracing-Header der SDKs und einige
eigene Header der Agenten, die freien Text enthalten können.
[Header, die Latet entfernt](#header-die-latet-entfernt) listet sie auf.

**Elemente des Bodys.** Jedes Element des Bodys einer Anfrage wird nach seinem Profil gelesen:
maskiert, von der abschließenden Prüfung geprüft oder an die Form gebunden, die der Anbieter ihm gibt
(die ID eines Modells, die ID eines Tool-Aufrufs, eine Einstellung). In 0.11 wurde ein Element, das
kein Profil nannte, abgelehnt, mit der Angabe, wo es steht (`top-level`, `message content block`,
`tool definition`...), nie mit seinem Namen oder Wert:

```text
Latet: request body refused (unknown member in top-level). Nothing was sent; run `latet dashboard` to see where it was
```

Seit 0.15.0 wird ein solches Element ganz gelesen und geprüft, und es geht unverändert durch, wenn
darin nichts maskiert werden müsste und es nichts nennt, worauf der Anbieter handelt: siehe
[Neue Elemente in Anfragen](#neue-elemente-in-anfragen). Mit denselben Worten abgelehnt wird es
weiterhin, wenn es eine Datei, eine Adresse, eine gespeicherte Unterhaltung oder ein Modell nennt,
kodierte Daten enthält oder in einem eigenen Element des Anbieters mehr als kurze Tokens trägt; und
mit `unknown_members: refuse` wird es abgelehnt wie in 0.11.

Eine [genaue Ausnahme](#eine-genaue-ausnahme-genehmigen) kann ein abgelehntes Element über seinen
JSON-Pointer nennen, außer dort, wo keine Ausnahme es erreichen kann: in einem eigenen Element des Anbieters
(`provider item`: Thinking, Reasoning), in der Quelle eines Bildes (`inline image`) und in jedem
WebSocket-Frame. Der Agent erhält dieselben Worte; das Dashboard zeichnet die Ablehnung als
`body-unknown-member-no-exception` statt `body-unknown-member` auf und sagt *Ihr Body enthielt ein
Element, das kein Anfrageprofil vorsieht, an einer Stelle, die keine Ausnahme erreicht (in einem
Element des Anbieters, der Quelle eines Bildes oder einer Runde über WebSocket).*

Eine Anfrage, die Latet nur prüft (Claude Code, Bedrock, Vertex), wird nie umgeschrieben, daher gilt
dort eine Regel mehr: Ein neues Element, dessen Wert ein Text oder eine Liste von Texten ist, geht
durch, sobald die abschließende Prüfung seinen Namen und seinen Text gelesen hat, und das Dashboard
zeigt es als *nur geprüft*. Ein verschachteltes oder anderes neues Element wird so gelesen, wie
[Neue Elemente in Anfragen](#neue-elemente-in-anfragen) es beschreibt.
`client_metadata` von Codex, das den Pfad und die Remotes des Repositorys wiederholt, wird aus der
Anfrage entfernt, bevor sie maskiert wird.

**Was der Anbieter zurückgegeben hat.** Die Thinking-Signaturen des Modells, verschlüsseltes
Reasoning, Kompaktierungen und Ergebnisse von Websuchen kommen in jeder Runde zurück. Sie gehen durch,
weil Latet sie in dieser Unterhaltung vom Anbieter kommen sah. Wo es das nicht sah (der Daemon eines
Rechners wurde zum Beispiel neu gestartet), lässt eine eingebaute Regel die verschlüsselten Felder
des Anbieters durch, nur in den Runden des Modells und nur an die eigene Adresse des Anbieters; das
Dashboard zeigt sie als *durch eine Regel erlaubt*. Eine Unterhaltung über ein
[Unternehmens-Gateway](#unterhaltungen-über-ein-unternehmens-gateway) hat diese Regel nicht.

**Bilder und Dokumente.** Ein Bild wird erst gesendet, wenn Latet seinen Text gelesen hat. Ohne
Texterkennung wird eine Anfrage mit einem Bild abgelehnt, mit dem Hinweis, `latet ocr install`
auszuführen und eine neue Unterhaltung zu beginnen, da das Bild in ihrem Verlauf bleibt.
`images: allow` sendet kein Bild mehr, das Latet nicht gelesen hat. PDFs und andere Dokumente, Audio
und per Adresse eingebundene Bilder werden abgelehnt, was immer `images` sagt. Bei
einer Anfrage, die Latet nur prüft (Claude Code, Bedrock, Vertex), muss jedes Byte eines Bildes
erfasst sein, und ein Handyfoto mit einem Standort, den Herstellerangaben der Kamera, einer
Vorschau oder einem zweiten Bild darin (einer Tiefenkarte, dem Video eines Live-Fotos) wird
abgelehnt: siehe [Fotos und ihre Metadaten](../../concepts/images/#fotos-und-ihre-metadaten). Dort
wird auch ein ursprüngliches WebP-Bild abgelehnt: Exportieren Sie es als PNG. Anfragen, die Latet
umschreibt (Codex und die anderen), senden die Bilder neu geschrieben, ohne Metadaten, und sehen
keine solche Ablehnung.

**Ein Projekt, in dem Latet aus ist,** sendet nichts: siehe [unten](#ein-projekt-in-dem-latet-aus-ist).
**`latet serve --passthrough`** gibt es nicht mehr: Die Option bleibt in 0.11 verborgen erhalten und
beendet den Befehl sofort mit einer Meldung, sodass ein Dienst, der sie noch nennt, sichtbar stehen bleibt.
Entfernen Sie sie aus dem Befehl des Dienstes oder aus dem Skript, das Latet startet.

**Ein Ordner, den eine Anfrage nennt, fügt nur Schutz hinzu:** siehe
[Ordner, die eine Anfrage nennt](#ordner-die-eine-anfrage-nennt).

## Header, die Latet entfernt

Latet entfernt diese Header aus einer Anfrage, bevor es sie sendet, welcher Agent sie auch gesendet
hat:

| Wo | Header |
| --- | --- |
| Jede Anfrage: Telemetrie und Tracing der SDKs | `x-stainless-*`, `x-datadog-*`, `x-b3-*`, `traceparent`, `tracestate`, `baggage` |
| Claude Code, im Format von Anthropic (Anthropic, Bedrock, Vertex, ein Unternehmens-Gateway) | `x-claude-code-agent-id` und `x-claude-code-parent-agent-id`, die IDs eines Subagenten, die seinen Namen enthalten können; `x-claude-remote-session-id`, `x-claude-remote-container-id`, `x-claude-remote-session-origin` und `x-client-app`, die Claude Code aus seiner Umgebung übernimmt; `x-claude-code-prev-tool-durations`, ein Hinweis für Gateways, der die ausgeführten Tools nennt (darunter die eigenen Namen eines MCP-Servers); jeder andere `x-claude-code-*`-Header, den Latet nicht eigens nennt |
| Bedrock: das AWS SDK | `amz-sdk-*`, `x-amz-user-agent` |
| Gemini: die Clientbibliothek von Google | `x-goog-api-client` |
| Codex, im Format von OpenAI und von ChatGPT | `x-codex-turn-metadata`, der Pfad, Remotes und Commit Ihres Repositorys enthält; `x-openai-subagent`, die Art oder eine freie Bezeichnung eines Subagenten |

Auch die eigenen `x-latet-*`-Header von Latet erreichen nie einen Anbieter. Zugangsdaten werden nie
entfernt. Bei einer Anfrage, die Latet nur prüft (Claude Code, Bedrock, Vertex), verlässt der Body den
Rechner Byte für Byte unverändert: Diese Header sind die einzige Änderung. `client_metadata` von Codex, die Kopie von
`x-codex-turn-metadata` im Body, wird aus dem Body einer Anfrage entfernt, die Latet umschreibt.

Jeder Austausch im Dashboard zählt die Header und Elemente des Bodys, die aus ihm entfernt wurden.
Antwortet der Anbieter mit `401` oder `403` auf eine Anfrage, aus der welche entfernt wurden, erfährt
der Agent es, und der Austausch sagt, dass dieser Header einer sein kann, den der Anbieter braucht:

```text
Latet: the provider answered 401 after Latet removed 2 SDK/tracing headers
```

## Neue Elemente in Anfragen

Eine neue Version eines Agenten fügt seinen Anfragen oft ein Element hinzu: einen Modus, ein Flag,
eine eigene Einstellung. Vor 0.15.0 lehnte Latet eine Anfrage mit einem Element ab, das kein
Anfrageprofil nennt, außer das Element enthielt bei einer Anfrage, die Latet nur prüft, nur Text: Der
Agent blieb stehen, bis eine Version von Latet das Element nannte. Seit 0.15.0 liest Latet ein
solches Element ganz, jeden Namen und jeden Wert darin, prüft es so streng wie einen Text, den es
maskiert, und sendet es unverändert, wenn darin nichts maskiert werden müsste und es nichts nennt,
worauf der Anbieter handelt. Das ist `unknown_members: check`, die Voreinstellung, bei jeder
Anfrage: bei denen, die Latet nur prüft (Claude Code, Bedrock, Vertex), und bei denen, die es
umschreibt (Codex, OpenCode, Gemini CLI, Hermes Agent, die SDKs). Ein solches Element wird nie
maskiert: Es geht unverändert hinaus, oder die Anfrage stoppt.

**Was durchgeht:** ein Element aus Steuerwerten, etwa `{"mode": "compact", "level": 2, "on": true}`:
ein Modus, ein Flag, eine Zahl, ein kurzes Token oder ein Objekt oder eine Liste davon.

**Was die Anfrage weiterhin stoppt,** mit den Worten und Codes von 0.11, wo immer es im Element
steht:

- eine Webadresse oder eine andere URI, auch eine `data:`-URI, oder die ID einer Datei, einer
  Unterhaltung, einer gespeicherten Antwort oder einer anderen Ressource, die der Anbieter aufbewahrt
  (`file-…`, `conv_…`, `resp_…`, `gs://…`);
- ein Name, der sagt, dass es um etwas geht, worauf der Anbieter handeln würde, etwa eine Datei,
  einen Anhang, ein Bild, ein Dokument, eine Webadresse, einen Server, einen MCP-Server, einen
  Container, eine Unterhaltung, einen Cache oder ein Modell;
- kodierte Daten: eine Folge von 64 oder mehr Base64-Zeichen, die Ziffern, Groß- und Kleinbuchstaben
  mischt, oder von 48 Hexadezimalziffern;
- eine Zahl, die nicht endlich ist, oder mehr, als Latet von einer Anfrage liest;
- mehr, als Latet von den neuen Elementen einer Anfrage liest: ein Name mit mehr als 256 Zeichen,
  mehr als 4.096 verschiedene Texte insgesamt (jeder Name und jeder Wert, ein Wert ein weiteres Mal
  für jedes Feld, in dem er steht), mehr als 256 KiB dieser Texte oder Elemente, die das lokale
  Namensmodell nicht in den 5 Sekunden lesen kann, die es für ein Dokument hat;
- in einem eigenen Element des Anbieters (Thinking, Reasoning, die Ergebnisse einer Websuche, die
  Quelle eines Bildes) alles außer kurzen Tokens: freier Text, ein Schlüssel oder ein Wert mit mehr
  als 64 Buchstaben, Ziffern, `_`, `.`, `:` oder `-`, oder mehr als 32 Werte oder 2.048 Byte
  insgesamt;
- ein sensibler Wert: alles, was Ihre Regeln in einem Text maskieren oder schwärzen würden (ein
  Geheimnis, ein Name, eine E-Mail-Adresse, eine Telefonnummer, ein Wert, den eine Ihrer Feldregeln
  nennt, nach seinem Schlüssel oder seinem Pfad), oder ein Wert, den Ihr Tresor kennt. Die Anfrage
  stoppt, wie sie es bei einem solchen Wert in jedem anderen Text tut, und das Dashboard nennt die
  Art des Werts, *Neues Element der Anfrage*, nie den Namen des Elements.

Ein Text eines solchen Elements, der einmal hinausging, geht in derselben Unterhaltung wieder
durch, wie schon gesendeter Text, auch nachdem Latet einen Wert darin gelernt hat: Eine Unterhaltung
bleibt nie daran hängen. Im Dashboard führt ein Austausch, der eines durchgelassen hat, *ein Feld,
das noch kein Anfrageprofil vorsieht, gelesen und geprüft* unter dem auf, was ihn durchgelassen hat.
Der Name eines Elements wird nie aufgezeichnet, protokolliert oder angezeigt.

### Sie wieder ablehnen

`unknown_members: refuse` lehnt ein solches Element ab, wie Latet es vor 0.15.0 tat: Eine Anfrage,
die eines enthält, wird abgelehnt, außer das Element enthält bei einer Anfrage, die Latet nur
prüft, nur Text. In `config.yaml`:

```yaml
unknown_members: refuse
```

oder im Dashboard unter *Neue Elemente in Anfragen* bei den Produktionsdaten von **Regeln**. Der
Wechsel zu `refuse` gilt sofort. Der Wechsel zurück zu `check` senkt den Schutz: In `config.yaml`
oder von einem Agenten vorgenommen, wartet er auf Ihre Freigabe unter **Freigaben**; im Dashboard
vorgenommen, ist das Speichern Ihre Freigabe. Auf einem Latet-Server, dessen [zentrale
Freigaben](../../enterprise/approvals/) (Latet Enterprise) solche Änderungen an das Sicherheitsteam
Ihres Unternehmens geben, wartet er stattdessen auf dessen Entscheidung, auch wenn Sie im Dashboard
speichern. Wo die Vorgaben Ihres Unternehmens `unknown_members: refuse` sperren, bleibt `refuse` in
Kraft, was immer Ihre Einstellungen sagen, und das Dashboard bietet nur diesen Wert an. Eine unter
`refuse` abgelehnte Anfrage sagt im Dashboard, dass `unknown_members: refuse`, in Ihren
Einstellungen, in denen des Projekts oder in den Vorgaben Ihres Unternehmens, solche Elemente
ablehnt, und was `check` tun würde, mit einem Link zu diesem Abschnitt; wo eine genaue Ausnahme das
Element erreichen kann, sagt sie weiterhin, wie Sie eine genehmigen, da eine Ausnahme vor dieser
Einstellung gelesen wird.

- **Ein Projekt kann sie nur ablehnen.** `unknown_members: refuse` in Ihren Einstellungen für ein
  Projekt (sein Eintrag unter `projects` in `config.yaml`) oder in der `latet.yaml` seines
  Repositorys lehnt sie nur in diesem Projekt ab. `check` steht allein Ihren Einstellungen zu: Der
  Eintrag eines Projekts in `config.yaml` nimmt nur `refuse` an, und eine `latet.yaml`, die `check`
  sagt, wird unter *Was nicht oder anders als geschrieben angewendet wird* aufgeführt.
- **Das `refuse` eines Repositorys bindet ein vertrauenswürdiges oder behauptetes Projekt.** Wie jede
  Regel eines Projekts, die Schutz hinzufügt, gilt es für die Anfragen, die Latet diesem Projekt
  zuordnet, ob es das Projekt für vertrauenswürdig hält oder eine Anfrage es behauptet: Ein Client,
  der einen anderen Ordner nennt, ist nicht daran gebunden, außer sein Startschlüssel bindet ihn an
  das Projekt (siehe [Ordner, die eine Anfrage nennt](#ordner-die-eine-anfrage-nennt)).
- **Auf einem Latet-Server** hat jede Person und jeder API-Schlüssel einen eigenen Wert, `check`,
  außer sie setzen `refuse`, und `latet server status` nennt den geltenden Wert
  (`new request members: …`). Mit Latet Enterprise können die Vorgaben des Unternehmens
  `unknown_members: refuse` für jede Person und jeden API-Schlüssel sperren
  ([Gesperrte Regeln](../../enterprise/locked-rules/)); wie jede gesperrte Regel bleibt es in Kraft,
  wenn die Lizenz endet.
- **`latet doctor`** nennt den geltenden Wert unter *New request members* und, solange
  `config.yaml` kein `unknown_members` enthält, dass `check` die Voreinstellung ist und wie Sie
  solche Elemente wieder ablehnen.

### Was es nicht abdeckt

- **Der lokale Prüfer**, das Sprachmodell, das Texte nach Kategorien liest, liest diese Elemente
  nicht: Das tun die Detektoren von Latet und Ihr Tresor.
- **Der strikte Datenmodus** liest diese Elemente nicht: Er deckt die Tool-Ausgaben, Dateien und
  Hosts ab, die er nennt, und ein neues Element ist nichts davon. Ihre Feldregeln lesen sie, nach
  dem Schlüssel oder dem Pfad eines Werts, und ebenso die Detektoren von Latet.
- **Kodierte Daten werden nicht dekodiert:** Ein Geheimnis in Base64, kürzer als die oben genannten
  Folgen, oder in einer eigenen Form geschrieben, wird als der Text gelesen, der es ist, wie in jedem
  Text, den Latet prüft.
- **Bei einer Anfrage, die Latet umschreibt,** stoppt ein sensibler Wert in einem solchen Element
  die Anfrage, statt als Ersatzwert hinauszugehen, jedes Mal, wenn sie erneut gesendet wird, außer er
  ging in dieser Unterhaltung schon hinaus. Ein Client, der die Werte, die Latet wiederhergestellt
  hat, in ein neues Element kopiert, bleibt stehen, bis eine Version von Latet dieses Element nennt.
- **Bei einer Anfrage, die Latet nur prüft,** behält ein neues Element, dessen Wert ein Text oder
  eine Liste von Texten ist, außerhalb eines Elements des Anbieters die Regel von 0.11 (*nur
  geprüft*): Die abschließende Prüfung stoppt darin Geheimnisse, Ihre Begriffe, Ihre Identität und
  personenbezogene Daten, sucht aber keine Namen und wendet dort weder Ihre Feldregeln noch den
  strikten Datenmodus an, damit nichts, was vor 0.15.0 gesendet wurde, jetzt abgelehnt wird.
- **Das `refuse` eines Repositorys** bindet nur die Anfragen, die Latet seinem Projekt zuordnet,
  wie oben.
- **Das Update hat jede Installation auf `check` umgestellt**, Server eingeschlossen, ohne
  Freigabe: Wenn Sie sich auf die Ablehnung verlassen haben, setzen Sie `unknown_members: refuse`.

## Was `latet doctor` prüft

Führen Sie `latet doctor` vor dem Upgrade aus, und danach noch einmal. Es listet nach Art und Anzahl
auf, nie einen Wert:

- die Projekte, in denen Latet aus ist (die Anfragen jedes davon werden abgelehnt);
- nicht installierte oder mit `ocr: off` ausgeschaltete Texterkennung (jede Anfrage mit einem Bild
  wird abgelehnt);
- `--passthrough` in einem Eintrag, der Latet bei der Anmeldung startet;
- die in Ihrem Verlauf gesehenen Aufrufe, die kein Profil abdeckt, nach Methode und Routenvorlage,
  nie mit ihrem Pfad;
- Ihre dauerhaften Anfrageausnahmen;
- die von Latet gestarteten Agenten, deren Anfragen ohne ihren Startschlüssel ankamen, mit dem
  Agenten und seiner Version.

Der Daemon sagt dasselbe in seinem Log, wenn er zum ersten Mal mit 0.11 startet.

## Die Texterkennung installieren

```sh
latet ocr install   # etwa 31 MB, einmalig
```

`latet upgrade` fügt einem ohne sie installierten Latet hinzu, worauf die Modelle laufen. In Docker:
`docker exec latet latet ocr install --yes`. Auf einem Latet-Server installieren die
Administratoren die Modelle auf jeder Instanz
([Erkennungsmodelle auf einem Server](../../concepts/server-operations/#erkennungsmodelle-auf-einem-server)).
Siehe [Screenshots und Bilder](../../concepts/images/).

## Header, die Sie für Ihre Agenten setzen

Ein Agent, der mit eigenen Headern eingerichtet ist, sendet sie mit jeder Anfrage, und 0.11 lehnt sie
ab, bis Sie sie durchlassen: `ANTHROPIC_CUSTOM_HEADERS` von Claude Code, `http_headers` und
`env_http_headers` von Codex, `COPILOT_PROVIDER_HEADERS` von Copilot CLI, `options.headers` von
OpenCode, `GEMINI_CLI_CUSTOM_HEADERS` von Gemini CLI. Die Ablehnung nennt den Header. Lassen Sie jeden
mit einer genauen Ausnahme durch, wie unten, unter Angabe des Ziels, an das er geht; der Schlüssel
eines Unternehmens-Gateways in einem Header wird als Zugangsdaten genannt, die Latet sendet, wie sie
kamen, und nie liest. Den Startschlüssel von Latet, den `latet claude` und die anderen diesen
Einstellungen hinzufügen, nennen Sie nicht: Latet entfernt ihn, bevor etwas weitergesendet wird.

Ein Header, den Sie für Claude Code setzen und dessen Name mit `anthropic-` beginnt, braucht keine
Ausnahme: Er geht durch, sobald die abschließende Prüfung ihn gelesen hat, wie ein neuer Header von
Claude Code selbst ([Header](#was-011-ablehnt-das-010-gesendet-hat)); einer, dessen Name mit
`x-claude-code-` beginnt, wird entfernt. Zugangsdaten eines Ziels in einem solchen Header nennen Sie
dennoch mit einer Ausnahme, damit Latet sie sendet, wie sie kamen, und nie liest. Eine Ausnahme, die
einen solchen Header nennt, entscheidet über ihn auch dort, wo sie nicht gilt: Bei einer Anfrage
eines anderen Projekts oder wenn die Ausnahme abgelaufen ist, wird der Header abgelehnt (mit seinem
Namen oder mit der Ausnahme und dem Tag, an dem sie ablief), weder gelesen noch an ihrer Stelle
gesendet.
Entfernen Sie die Ausnahme, wenn der Header überall geprüft durchgehen soll.

## Eine genaue Ausnahme genehmigen

Eine Ausnahme nennt genau, was sie durchlässt, bei einem Ziel und einem Anfrageprofil: einen Header,
einen Abfrageparameter oder ein Element des JSON-Bodys. Um zum Beispiel den Header `x-team` von
Claude Code zu Anthropic durchzulassen:

```sh
latet config add request_exceptions '{destination: anthropic, profile: anthropic.messages.1, components: [{kind: header, name: x-team}], reason: custom-header}'
```

Dasselbe in `config.yaml`:

```yaml
request_exceptions:
  - destination: anthropic           # die Route eines Anbieters oder eines Ihrer Ziele
    profile: anthropic.messages.1    # eine Methode, eine Route und ein Transport
    components:
      - {kind: header, name: x-team}                  # geprüft: der Wert durchläuft die abschließende Prüfung
    reason: custom-header
  - destination: llm-acme-com
    profile: openai.chat.1
    components:
      - {kind: header, name: x-acme-key, role: credential}   # gesendet, wie er kam, nie gelesen
    reason: destination-credential
  - destination: openai
    profile: openai.responses.1
    components:
      - {kind: body, name: /metadata_x/team, type: string}  # ein genauer JSON-Pointer und der Typ des Werts
    reason: other
```

- **Sie wartet auf Ihre Genehmigung.** Eine Ausnahme verringert den Schutz, daher gilt sie erst, wenn Sie
  sie genehmigen, auf der Seite **Freigaben** im Dashboard, unter *Einstellungen*. Latet gibt ihr dann
  eine ID (`x1a2b3c4d5e`) und den Zeitpunkt der Genehmigung. Das Entfernen einer Ausnahme oder das
  Einschränken dessen, was sie nennt, gilt sofort.
- **Sie gilt standardmäßig 30 Tage** ab ihrer Genehmigung und höchstens 90 (`days`). Das Dashboard
  warnt sieben Tage vor ihrem Ende, und eine danach abgelehnte Anfrage nennt die Ausnahme und den Tag,
  an dem sie endete. Eine Verlängerung ist eine neue Genehmigung.
- **Dauerhaft, mit Absicht.** `permanent: true` macht eine, die nie abläuft. Sie wird wie jede
  Ausnahme genehmigt, im Dashboard immer als *dauerhaft* markiert und von `latet doctor` und, auf
  einem Server, von `latet server status` aufgelistet; ihre Genehmigung auf einem Server schreibt einen
  Eintrag ins Administrationsprotokoll, nur mit ihrer ID und der Art ihres Geltungsbereichs.
- **Wessen Anfragen.** Eine Ausnahme ohne `project` gilt für jede Ihrer Anfragen, welcher Agent sie
  auch sendet, und das Dashboard sagt das, bevor Sie sie genehmigen. Mit `project: C:\dev\acme` gilt
  sie nur für Anfragen, deren Projekt Latet dort vertraut: die eines Agenten, den es mit seinem
  Schlüssel gestartet hat, oder die von Claude Code mit dem Plugin (siehe
  [Projekte](../../concepts/projects/#zu-welchem-projekt-eine-sitzung-gehört)).
- **Nie in einem Repository.** Die `latet.yaml` eines Projekts kann keine Ausnahmen enthalten.
  Höchstens 128, 32 für ein Projekt, jede mit höchstens 64 Bestandteilen; `description` ist nur für
  Sie, nie in einem Log.
- **Nicht bei einem WebSocket.** In 0.11.0 gilt eine Ausnahme nicht für einen WebSocket: Codex mit
  ChatGPT-Anmeldung, das einen verwendet, kann keinen eigenen Header von Ihnen senden. Das Dashboard
  bietet dort bei einer Ablehnung keine an und sagt *Ein WebSocket nimmt in dieser Version von Latet
  keine Anfrageausnahme an.*
- **Nach einem Upgrade.** Eine spätere Version, die einen von einer Ausnahme genannten Teil eines
  Profils ändert, setzt diese Ausnahme aus, bis Sie sie erneut genehmigen; Dashboard und
  `latet doctor` sagen, dass sie neu genehmigt werden muss (*Muss neu genehmigt werden: Ihr Profil
  wurde überarbeitet*), auch dauerhafte; auf einem Server listet `latet server status` sie auf, sobald
  der Bereich der Identität mit der neuen Version gestartet ist. Ein Upgrade erweitert nie, was eine
  Ausnahme durchlässt.

Das Profil ist die Art des Aufrufs. Eine Ausnahme nennt ein einziges Profil, daher braucht ein Header,
den ein Agent mit jedem Aufruf sendet, eine Ausnahme für jede Art von Aufruf, die er macht:

| Aufrufe | Profil |
| --- | --- |
| Anthropic Messages (Claude Code, das SDK von Anthropic), Tokenzählung, die Modellliste, ein Modell und der Aufruf, der beim Start die Verbindung von Claude Code öffnet (ein HEAD ohne Inhalt) | `anthropic.messages.1`, `anthropic.count_tokens.1`, `anthropic.models.1`, `anthropic.model.1`, `anthropic.hello.1` |
| Das Format von OpenAI (OpenAI, OpenRouter, ein Unternehmens-Gateway): Chat Completions, die älteren Completions, Responses über HTTP, Embeddings, die Modellliste und ein Modell | `openai.chat.1`, `openai.completions.1`, `openai.responses.1`, `openai.embeddings.1`, `openai.models.1`, `openai.model.1` |
| Codex mit ChatGPT-Anmeldung, über HTTP: Responses und die Modellliste | `chatgpt.codex.responses.1`, `chatgpt.codex.models.1` |
| Gemini, über die Gemini API oder Vertex AI: Erzeugen, Streaming, Zählen, Embeddings (einzeln oder gebündelt), die Modellliste und ein Modell, einen Cache anlegen oder ändern, Chat und Embeddings im Format von OpenAI | `gemini.<version>.generateContent.1`, `gemini.<version>.streamGenerateContent.1`, `gemini.<version>.countTokens.1`, `gemini.<version>.embedContent.1`, `gemini.<version>.batchEmbedContents.1`, `gemini.<version>.models.1`, `gemini.<version>.model.1`, `gemini.<version>.cache-create.1`, `gemini.<version>.cache-patch.1`, `gemini.<version>.openai.completions.1`, `gemini.<version>.openai.embeddings.1` |
| Bedrock: Aufrufen, Streaming, Tokenzählung, die Inferenzprofile und eines davon | `bedrock.invoke.1`, `bedrock.invoke-with-response-stream.1`, `bedrock.count-tokens.1`, `bedrock.inference-profiles.1`, `bedrock.inference-profile.1` |
| Claude auf Vertex AI | `vertex.anthropic.rawPredict.1`, `vertex.anthropic.streamRawPredict.1` |
| Ein Unternehmens-Gateway vor Bedrock oder vor Claude auf Vertex AI | `company.bedrock.invoke.1`, `company.bedrock.invoke-with-response-stream.1`, `company.bedrock.count-tokens.1`, `company.vertex.rawPredict.1`, `company.vertex.streamRawPredict.1` |
| Ziele für Azure OpenAI | `azure-openai.chat.1`, `azure-openai.completions.1`, `azure-openai.responses.1`, `azure-openai.embeddings.1` |
| Ziele für Microsoft Foundry | `foundry.chat.1`, `foundry.embeddings.1` |

In einer Gemini-ID ist `<version>` die, die der Pfad der Anfrage nennt: `v1` oder `v1beta` bei der
Gemini API, `v1` oder `v1beta1` bei Vertex AI (`gemini.v1beta.generateContent.1`).

Auf einem Latet-Server gehören Ausnahmen zu den eigenen Einstellungen jeder Person, die sie im
Dashboard genehmigt; die eines API-Schlüssels werden über die Administration gesetzt.

## Bedrock und Vertex: was aufzulisten ist

Eine Modell-ID in der dokumentierten Form eines Anbieters geht von selbst durch, im Pfad wie im
Body, und ebenso ein von Amazon definiertes Inferenzprofil von Bedrock (`us.anthropic.claude-…`,
`eu.`, `apac.`, `global.`). Was zu Ihrem Konto oder Ihrem Cloud-Projekt gehört, muss in
`request_resources` aufgelistet sein, bevor eine Anfrage, die es nennt, gesendet wird: jeder ARN in
einem Bedrock-Pfad (auch der ARN eines Anwendungs-Inferenzprofils) und das Vertex-Projekt. Jede Zeile
nennt das Ziel, seine Adresse, die Rolle des Werts und den Wert:

```yaml
request_resources:
  - destination: bedrock
    url: https://bedrock-runtime.us-east-1.amazonaws.com    # Modellaufrufe; Lesezugriffe gehen an https://bedrock.us-east-1.amazonaws.com
    role: model                                             # inference-profile, um eines zu lesen
    value: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6
  - destination: vertex
    url: https://us-east5-aiplatform.googleapis.com
    role: project
    value: acme-ml-prod
```

`latet config add request_resources '{...}'` fügt eine hinzu. Eine Zeile lässt genau diesen Wert an
diesem Ziel durch und nichts anderes, und sie gilt sofort. Solange sie fehlt, wird eine solche Anfrage
mit `envelope-resource` abgelehnt. Eine Modell-ID außerhalb der dokumentierten Formen (die eigenen
Modellnamen eines Unternehmens-Gateways) wird genauso aufgelistet, mit der Rolle `model`.

Auf OpenRouter wird ein Modell mit seinem Hersteller benannt. Die von Anthropic, OpenAI und Google gehen
von selbst durch, wenn der Teil nach dem Hersteller in der dokumentierten Form dieses Anbieters ist, mit
oder ohne eine Variante wie `:free` oder `:thinking` (`anthropic/claude-sonnet-4.5`, `openai/gpt-4o`,
`google/gemini-2.5-pro`), und ebenso `openrouter/auto`. Das Modell jedes anderen Herstellers
(`meta-llama/…`, `deepseek/…`) wird mit dem Ziel `openrouter`, der Adresse `https://openrouter.ai`
und der Rolle `model` aufgelistet.

## Ein Projekt, in dem Latet aus ist

In 0.10 sendete ein Projekt, in dem Latet aus war, seine Anfragen unmaskiert. In 0.11 sendet Latet
keine davon, weder über HTTP noch über einen WebSocket, ob das Projekt nun aus dem Start des Agenten,
aus dem Plugin oder aus einem Ordner kommt, den die Anfrage nennt. Der Agent erfährt:

```text
Latet is off in this project: Latet sends no request from there; turn it on for the project (`latet project on` in its folder, or the project's switch in the dashboard), or reach the provider directly for this folder by starting the agent without Latet's address. Nothing was sent; run `latet dashboard` to see where it was
```

Wenn das Projekt nur von der Anfrage genannt wird (nur behauptet, siehe unten), beginnen die Worte mit
`request claimed a project where Latet is off:` und gehen genauso weiter.

`latet project off` und der Schalter im Dashboard sagen das, bevor sie ein Projekt ausschalten. Um in
diesem Ordner ohne Latet zu arbeiten, starten Sie den Agenten dort ohne die Adresse von Latet: Ein
Agent, den Sie mit `latet claude`, `latet codex` oder einem anderen `latet`-Befehl starten, läuft
ohne Latet, wenn Sie ihn direkt starten (`claude`, `codex`); einer, dessen eigene Einstellungen auf Latet zeigen (nach
`latet global-install`), geht über Latet, wo immer er startet: Geben Sie ihm für diese Sitzung die
Adresse seines Anbieters, wie seine Dokumentation sagt, oder setzen Sie ihn mit `latet agents revert`
zurück. Latet sieht dann nichts von dem, was er sendet. Wenn Sie Latet ausgeschaltet hatten, um
personenbezogene Daten durchzulassen, tut *Nur Geheimnisse* genau das und schützt geheime Werte
weiterhin ([Projekte](../../concepts/projects/#nur-geheimnisse-oder-latet-aus-in-einem-projekt)).

## Ordner, die eine Anfrage nennt

Eine Anfrage kann ihren Ordner nennen: den Header `x-latet-project` oder den Arbeitsordner, den Codex,
OpenCode und Gemini CLI in ihre Anfragen schreiben. In 0.11 ist ein solcher Ordner *nur behauptet*: Die
Regeln seines Projekts, die Schutz hinzufügen, gelten, seine Lockerungen aber nicht (Werte, die nie
maskiert werden, ausgeschaltete Regeln für geheime Werte, ein schwächeres Profil, `data_tools`, die Hinweise
und Bilder für den lokalen Prüfer). Ein behauptetes Projekt, in dem Latet aus ist, wird wie oben
abgelehnt. Ein Projekt ist *vertrauenswürdig*, wenn Latet es selbst kennt: aus dem Startschlüssel
eines Agenten, den es gestartet hat, oder aus dem Plugin von Claude Code. Das Dashboard zeigt für
jede Anfrage, ob ihr Projekt vertrauenswürdig oder nur behauptet war und wie ihr Start entschieden
wurde. Siehe [Projekte](../../concepts/projects/#zu-welchem-projekt-eine-sitzung-gehört).

Was das für jeden Agenten bedeutet:

- **Von Latet gestartet** (`latet claude`, `latet codex`, `latet opencode`, `latet gemini`,
  `latet copilot`): vertrauenswürdig durch seinen Startschlüssel, wo der Agent ihn mitführt. Codex
  bei seinen eingebauten Anbietern (mit ChatGPT-Anmeldung oder dem eigenen Anbieter von OpenAI),
  Codex vor 0.115, OpenCode vor 1.19.5, Gemini CLI im ACP-Modus, Copilot CLI außerhalb von Chat
  Completions und die Programme, die `latet run`
  startet, führen keinen mit: Der startende Befehl sagt das in einem Satz, und ihre Projekte sind
  nur behauptet.
- **Claude Code mit dem Plugin**, dauerhaft eingerichtet: vertrauenswürdig durch das Plugin.
- **Jeder andere dauerhaft eingerichtete Agent**: nur behauptet. Ihre eigenen Regeln und jede Ausnahme
  ohne `project` gelten weiterhin.

## Unterhaltungen über ein Unternehmens-Gateway

Claude Code über LiteLLM oder ein anderes Unternehmens-Gateway, das die Thinking-Signaturen
weiterreicht, ist darauf angewiesen, dass Latet diese Signaturen zurückkommen sah. Die eingebaute
Regel für die verschlüsselten Felder des Anbieters gilt nur für die eigene Adresse des Anbieters, nie
für eine Unternehmensroute. Nach einem Neustart des Daemons, oder auf einem Server, wenn die
Datensätze der Unterhaltung nicht mehr da sind, wird eine solche Unterhaltung in der nächsten Runde
abgelehnt: Beginnen Sie eine neue.

## Zurück zu 0.10

```sh
latet upgrade --to 0.10.4
```

Beim Zurückgehen zu der Version, unter der eine Kopie Ihrer Daten erstellt wurde, wird angeboten,
diese Kopie zurückzuspielen ([Aktualisierung](../installation/#aktualisierung)). Latet 0.10 ignoriert
`request_exceptions` und `request_resources` und sendet wieder, was 0.11 ablehnt: ungelesene Teile von
Anfragen, die Anfragen von Projekten, in denen Latet aus ist, und Bilder, die es nicht lesen kann.
Gehen Sie nur in diesem Wissen zurück.

Eine Einstellung kennt 0.10 nicht: `request_profile` bei einem Ziel in `upstreams`. 0.11 schreibt
sie nur für ein Ziel, für das Sie ein Profil gewählt haben, in `config.yaml` und in
`config.reviewed.json`, die zuletzt freigegebenen Einstellungen im Datenordner von Latet. 0.10 startet
nicht, wenn sie in `config.yaml` steht, und liest eine `config.reviewed.json`, die sie enthält, als
gäbe es keine freigegebenen Einstellungen: Eine Änderung an `config.yaml` würde dann gelten, ohne auf
Ihre Freigabe zu warten. Wenn Sie eines gewählt haben, spielen Sie die Kopie zurück, die das Upgrade
auf 0.11 angelegt hat und die beide Dateien so enthält, wie sie vorher waren: `latet upgrade --to`
bietet sie beim Zurückgehen an (sie liegt im Datenordner unter `backups/before-<Version>-<Zeit>`,
benannt nach der installierten Version, zum Beispiel `before-0.15.1-…`). Ohne
diese Kopie (0.11 mit dem Installationsskript installiert, das keine anlegt) entfernen Sie den
Schlüssel vor dem Zurückgehen aus beiden Dateien: die Zeilen `request_profile:` aus `config.yaml` und
jedes Element `"request_profile": "…"` aus `config.reviewed.json`, sodass die Datei gültiges JSON bleibt.

Auf einem Latet-Server fügt 0.11 der Datenbank Tabellen hinzu, und eine Instanz lehnt eine Datenbank
ab, die neuer ist als sie selbst: Zurückgehen bedeutet die vor dem Upgrade erstellte Sicherung der
Datenbank, mit allen Instanzen angehalten
([Aktualisieren und Instanzen hinzufügen](../../concepts/server-operations/#aktualisieren-und-instanzen-hinzufügen)).
