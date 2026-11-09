---
title: Umstieg auf 0.11
description: Was Latet 0.11 ablehnt, das 0.10 gesendet hat, was Sie vor dem Upgrade prüfen und wie Sie zurückgehen.
---

Latet 0.11.0 liest jeden Teil einer Anfrage, bevor es sie sendet: den Pfad, die Abfrageparameter,
die Header und den Inhalt. Eine Anfrage mit einem Teil, den Latet weder prüft noch durch eine Regel
oder eine von Ihnen genehmigte Ausnahme durchlässt, wird nicht gesendet. Ein leerer Inhalt reicht
nicht mehr aus, um eine Anfrage durchzulassen. Die Zugangsdaten, die Ihr Anbieter braucht, und die
Teile, die ein Anbieter signiert, folgen eigenen Regeln. Das gilt auf Ihrem Rechner genauso wie auf
einem Latet-Server.

Ein Aufruf, den 0.10 ungelesen gesendet hat, kann jetzt also abgelehnt werden. Diese Seite zeigt, was
sich ändert, was `latet doctor` vor und nach dem Upgrade für Sie prüft und was in jedem Fall zu tun ist.

## Was 0.11 ablehnt, das 0.10 gesendet hat

**Aufrufe ohne Inhalt.** Die Liste der Modelle und das Lesen eines Modells gehen bei jedem Anbieter
durch, den Latet kennt: Anthropic, OpenAI, OpenRouter, ChatGPT (die Modellliste von Codex), Gemini
und die Inferenzprofile von Bedrock. Auch ihre Abfrageparameter werden gelesen (`limit`, `after_id`,
`pageToken`, `maxResults`...). Jeder andere Aufruf wird abgelehnt: Dateien und Uploads, Batches,
Audio, Bilderzeugung, Moderation, das Lesen oder Löschen einer gespeicherten Antwort, das Lesen eines
Gemini-Caches, die Endpunkte eines Kontos. Der Agent erfährt es, und nichts wird gesendet:

```text
Latet: request envelope refused (envelope-route): route /{destination}/{unsupported-route}; this version of Latet does not support this call, so change the agent or its settings so that it does not make it. Nothing was sent; run `latet dashboard` to see where it was
```

Eine Ausnahme nennt einen Header, einen Abfrageparameter oder ein Element des Inhalts, nie eine ganze
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
jeder mit einem Namen aus Kleinbuchstaben, Ziffern und Bindestrichen, höchstens 48 nach
`anthropic-` und kein Wort zwischen seinen Bindestrichen länger als 16, und einem Wert von 1 bis
1.024 druckbaren Zeichen; ein Geheimnis oder einer Ihrer Werte darin wird weiterhin abgelehnt. Bei einer Anfrage, die Latet umschreibt, etwa der eines
Anthropic-SDKs, werden sie abgelehnt wie jeder andere Header, den kein Profil nennt.

Manche Header werden stattdessen entfernt: die Telemetrie- und Tracing-Header der SDKs und einige
eigene Header der Agenten, die freien Text enthalten können.
[Header, die Latet entfernt](#header-die-latet-entfernt) listet sie auf.

**Elemente des Inhalts.** Jedes Element des Inhalts einer Anfrage wird nach seinem Profil gelesen:
maskiert, von der abschließenden Prüfung geprüft oder an die Form gebunden, die der Anbieter ihm gibt
(die ID eines Modells, die ID eines Tool-Aufrufs, eine Einstellung). Ein Element, das kein Profil
nennt, wird abgelehnt, mit der Angabe, wo es steht (`top-level`, `message content block`,
`tool definition`...), nie mit seinem Namen oder Wert:

```text
Latet: request body refused (unknown member in top-level). Nothing was sent; run `latet dashboard` to see where it was
```

Eine [genaue Ausnahme](#eine-genaue-ausnahme-genehmigen) kann ein solches Element über seinen
JSON-Pointer nennen, außer dort, wo keine es erreicht: in einem eigenen Element des Anbieters
(`provider item`: Thinking, Reasoning), in der Quelle eines Bildes (`inline image`) und in jedem
WebSocket-Frame. Der Agent erhält dieselben Worte; das Dashboard zeichnet die Ablehnung als
`body-unknown-member-no-exception` statt `body-unknown-member` auf und sagt *Ihr Inhalt enthielt ein
Feld, das kein Anfrageprofil vorsieht, an einer Stelle, die keine Ausnahme erreicht (in einem
Element des Anbieters, der Quelle eines Bildes oder einem WebSocket-Turn).*

Eine Anfrage, die Latet nur prüft (Claude Code, Bedrock, Vertex), wird nie umgeschrieben, daher gilt
dort eine Regel mehr: Ein neues Element, dessen Wert ein Text oder eine Liste von Texten ist, geht
durch, sobald die abschließende Prüfung seinen Namen und seinen Text gelesen hat, und das Dashboard
zeigt es als *nur geprüft*. Ein verschachteltes oder anderes neues Element wird wie oben abgelehnt.
`client_metadata` von Codex, das den Pfad und die Remotes des Repositorys wiederholt, wird aus der
Anfrage entfernt, bevor sie maskiert wird.

**Was der Anbieter zurückgegeben hat.** Die Thinking-Signaturen des Modells, verschlüsseltes
Reasoning, Kompaktierungen und Ergebnisse von Websuchen kommen in jeder Runde zurück. Sie gehen durch,
weil Latet sie in dieser Unterhaltung vom Anbieter kommen sah. Wo es das nicht sah (der Daemon eines
Rechners wurde zum Beispiel neu gestartet), lässt eine eingebaute Regel die verschlüsselten Felder
des Anbieters durch, nur in den Zügen des Modells und nur an die eigene Adresse des Anbieters; das
Dashboard zeigt sie als *durch eine Regel erlaubt*. Eine Unterhaltung über ein
[Unternehmens-Gateway](#unterhaltungen-über-ein-unternehmens-gateway) hat diese Regel nicht.

**Bilder und Dokumente.** Ein Bild wird erst gesendet, wenn Latet seinen Text gelesen hat. Ohne
Texterkennung wird eine Anfrage mit einem Bild abgelehnt, mit dem Hinweis, `latet ocr install`
auszuführen und eine neue Unterhaltung zu beginnen, da das Bild in ihrem Verlauf bleibt.
`images: allow` sendet kein Bild mehr, das Latet nicht gelesen hat. PDFs und andere Dokumente, Audio
und Bilder, die über ihre Adresse angegeben sind, werden abgelehnt, was immer `images` sagt. Bei
einer Anfrage, die Latet nur prüft (Claude Code, Bedrock, Vertex), muss jedes Byte eines Bildes
erfasst sein, und ein Handyfoto mit einem Standort, den Herstellerangaben der Kamera, einer
Vorschau oder einem zweiten Bild darin (einer Tiefenkarte, dem Video eines Live-Fotos) wird
abgelehnt: siehe [Fotos und ihre Metadaten](../../concepts/images/#fotos-und-ihre-metadaten). Dort
wird auch ein ursprüngliches WebP-Bild abgelehnt: Exportieren Sie es als PNG. Anfragen, die Latet
umschreibt (Codex und die anderen), senden die Bilder neu geschrieben, ohne Metadaten, und sehen
keine solche Ablehnung.

**Ein Projekt, in dem Latet aus ist,** sendet nichts: siehe [unten](#ein-projekt-in-dem-latet-aus-ist).
**`latet serve --passthrough`** gibt es nicht mehr: Die Option bleibt in 0.11 verborgen erhalten und
beendet sich sofort mit einer Meldung, sodass ein Dienst, der sie noch nennt, sichtbar stehen bleibt.
Entfernen Sie sie aus dem Befehl des Dienstes oder aus dem Skript, das Latet startet.

**Ein Ordner, den eine Anfrage nennt, fügt nur Schutz hinzu:** siehe
[Ordner, die eine Anfrage nennt](#ordner-die-eine-anfrage-nennt).

## Header, die Latet entfernt

Latet entfernt diese Header aus einer Anfrage, bevor es sie sendet, welcher Agent sie auch gesendet
hat:

| Wo | Header |
| --- | --- |
| Jede Anfrage: Telemetrie und Tracing der SDKs | `x-stainless-*`, `x-datadog-*`, `x-b3-*`, `traceparent`, `tracestate`, `baggage` |
| Claude Code, im Format von Anthropic (Anthropic, Bedrock, Vertex, ein Unternehmens-Gateway) | `x-claude-code-agent-id` und `x-claude-code-parent-agent-id`, die IDs eines Subagenten, die seinen Namen enthalten können; `x-claude-remote-session-id`, `x-claude-remote-container-id`, `x-claude-remote-session-origin` und `x-client-app`, die Claude Code aus seiner Umgebung übernimmt; `x-claude-code-prev-tool-durations`, ein Hinweis für Gateways, der die ausgeführten Werkzeuge nennt (darunter die eigenen Namen eines MCP-Servers); jeder andere `x-claude-code-*`-Header, den Latet noch nicht nennt |
| Bedrock: das AWS SDK | `amz-sdk-*`, `x-amz-user-agent` |
| Gemini: die Clientbibliothek von Google | `x-goog-api-client` |
| Codex, im Format von OpenAI und von ChatGPT | `x-codex-turn-metadata`, der Pfad, Remotes und Commit Ihres Repositorys enthält; `x-openai-subagent`, die Art oder eine freie Bezeichnung eines Subagenten |

Auch die eigenen `x-latet-*`-Header von Latet erreichen nie einen Anbieter. Zugangsdaten werden nie
entfernt. Bei einer Anfrage, die Latet nur prüft (Claude Code, Bedrock, Vertex), geht der Inhalt Byte
für Byte hinaus: Diese Header sind die einzige Änderung. `client_metadata` von Codex, die Kopie von
`x-codex-turn-metadata` im Inhalt, wird aus dem Inhalt einer Anfrage entfernt, die Latet umschreibt.

Jeder Austausch im Dashboard zählt die Header und Elemente des Inhalts, die ihm entfernt wurden.
Antwortet der Anbieter mit `401` oder `403` auf eine Anfrage, aus der welche entfernt wurden, erfährt
der Agent es, und der Austausch sagt, dass dieser Header einer sein kann, den der Anbieter braucht:

```text
Latet: the provider answered 401 after Latet removed 2 SDK/tracing headers
```

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
einen Abfrageparameter oder ein Element des JSON-Inhalts. Um zum Beispiel den Header `x-team` von
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

- **Sie wartet auf Ihre Genehmigung.** Eine Ausnahme senkt den Schutz, daher gilt sie erst, wenn Sie
  sie genehmigen, auf der Seite **Freigaben** im Dashboard, unter *Einstellungen*. Latet gibt ihr dann
  eine ID (`x1a2b3c4d5e`) und den Zeitpunkt der Genehmigung. Eine zu entfernen oder sie weniger nennen
  zu lassen, gilt sofort.
- **Sie gilt 30 Tage** ab ihrer Genehmigung, standardmäßig, und höchstens 90 (`days`). Das Dashboard
  warnt sieben Tage vor ihrem Ende, und eine danach abgelehnte Anfrage nennt die Ausnahme und den Tag,
  an dem sie endete. Sie zu verlängern ist eine neue Genehmigung.
- **Dauerhaft, mit Absicht.** `permanent: true` macht eine, die nie abläuft. Sie wird wie jede
  Ausnahme genehmigt, im Dashboard immer als *dauerhaft* markiert und von `latet doctor` und, auf
  einem Server, von `latet server status` aufgelistet; ihre Genehmigung auf einem Server schreibt einen
  Eintrag ins Verwaltungsprotokoll, nur mit ihrer ID und der Art ihres Geltungsbereichs.
- **Wessen Anfragen.** Eine Ausnahme ohne `project` gilt für jede Ihrer Anfragen, welcher Agent sie
  auch sendet, und das Dashboard sagt das, bevor Sie sie genehmigen. Mit `project: C:\dev\acme` gilt
  sie nur für Anfragen, deren Projekt Latet dort vertraut: einem Agenten, den es mit seinem Schlüssel
  gestartet hat, oder Claude Code mit dem Plugin (siehe
  [Projekte](../../concepts/projects/#zu-welchem-projekt-eine-sitzung-gehört)).
- **Nie in einem Repository.** Die `latet.yaml` eines Projekts kann keine Ausnahmen enthalten.
  Höchstens 128, 32 für ein Projekt, jede mit höchstens 64 Bestandteilen; `description` ist nur für
  Sie, nie in einem Protokoll.
- **Nicht bei einem WebSocket.** In 0.11.0 gilt eine Ausnahme nicht für einen WebSocket: Codex mit
  ChatGPT-Anmeldung, das einen verwendet, kann keinen eigenen Header von Ihnen senden. Das Dashboard
  bietet dort bei einer Ablehnung keine an und sagt *Ein WebSocket nimmt in dieser Version von Latet
  keine Anfrage-Ausnahme an.*
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
| Anthropic Messages (Claude Code, das SDK von Anthropic), Tokenzählung, die Modellliste und ein Modell | `anthropic.messages.1`, `anthropic.count_tokens.1`, `anthropic.models.1`, `anthropic.model.1` |
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
Dashboard genehmigt; die eines API-Schlüssels werden über die Verwaltung gesetzt.

## Bedrock und Vertex: was aufzulisten ist

Eine Modell-ID in der dokumentierten Form eines Anbieters geht von selbst durch, im Pfad wie im
Inhalt, und ebenso ein von Amazon definiertes Inferenzprofil von Bedrock (`us.anthropic.claude-…`,
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

Auf OpenRouter wird ein Modell mit seinem Anbieter genannt. Die von Anthropic, OpenAI und Google gehen
von selbst durch, wenn der Teil nach dem Anbieter in der dokumentierten Form dieses Anbieters ist, mit
oder ohne eine Variante wie `:free` oder `:thinking` (`anthropic/claude-sonnet-4.5`, `openai/gpt-4o`,
`google/gemini-2.5-pro`), und ebenso `openrouter/auto`. Das Modell jedes anderen Anbieters
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
Agent, den Sie mit `latet claude`, `latet codex` oder einem anderen `latet`-Befehl starten, startet
für sich (`claude`, `codex`); einer, dessen eigene Einstellungen auf Latet zeigen (nach
`latet global-install`), geht über Latet, wo immer er startet: Geben Sie ihm für diese Sitzung die
Adresse seines Anbieters, wie seine Dokumentation sagt, oder setzen Sie ihn mit `latet agents revert`
zurück. Latet sieht dann nichts von dem, was er sendet. Wenn Sie Latet ausgeschaltet hatten, um
personenbezogene Daten durchzulassen, tut *Nur Geheimnisse* genau das und schützt die Geheimnisse
weiter ([Projekte](../../concepts/projects/#nur-geheimnisse-oder-latet-aus-in-einem-projekt)).

## Ordner, die eine Anfrage nennt

Eine Anfrage kann ihren Ordner nennen: den Header `x-latet-project` oder den Arbeitsordner, den Codex,
OpenCode und Gemini CLI in ihre Anfragen schreiben. In 0.11 ist ein solcher Ordner *nur behauptet*: Die
Regeln seines Projekts, die Schutz hinzufügen, gelten, seine Lockerungen aber nicht (Werte, die nie
maskiert werden, ausgeschaltete Geheimnisregeln, ein schwächeres Profil, `data_tools`, die Hinweise
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
Aufzeichnungen der Unterhaltung nicht mehr da sind, wird eine solche Unterhaltung in der nächsten Runde
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
`config.reviewed.json`, die zuletzt genehmigten Einstellungen im Datenordner von Latet. 0.10 startet
nicht, wenn sie in `config.yaml` steht, und liest eine `config.reviewed.json`, die sie enthält, als
gäbe es keine genehmigten Einstellungen: Eine Änderung an `config.yaml` würde dann gelten, ohne auf
Ihre Genehmigung zu warten. Wenn Sie eines gewählt haben, spielen Sie die Kopie zurück, die das Upgrade
auf 0.11 angelegt hat und die beide Dateien so enthält, wie sie vorher waren: `latet upgrade --to`
bietet sie beim Zurückgehen an (sie liegt im Datenordner unter `backups/before-<Version>-<Zeit>`,
benannt nach der installierten Version, zum Beispiel `before-0.12.0-…`). Ohne
diese Kopie (0.11 mit dem Installationsskript installiert, das keine anlegt) entfernen Sie den
Schlüssel vor dem Zurückgehen aus beiden Dateien: die Zeilen `request_profile:` aus `config.yaml` und
jedes Element `"request_profile": "…"` aus `config.reviewed.json`, sodass es gültiges JSON bleibt.

Auf einem Latet-Server fügt 0.11 der Datenbank Tabellen hinzu, und eine Instanz lehnt eine Datenbank
ab, die neuer ist als sie selbst: Zurückgehen bedeutet die vor dem Upgrade erstellte Sicherung der
Datenbank, mit allen Instanzen angehalten
([Aktualisieren und Instanzen hinzufügen](../../concepts/server-operations/#aktualisieren-und-instanzen-hinzufügen)).
