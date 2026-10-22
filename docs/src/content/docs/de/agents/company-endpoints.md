---
title: Unternehmens-Endpunkte
description: Claude Code und Codex über das Gateway Ihres Unternehmens, Amazon Bedrock, Google Vertex, Microsoft Foundry oder Azure OpenAI, mit Latet davor.
---

Bei der Arbeit erreichen Claude Code und Codex das Modell oft über eine Route, die Ihr Unternehmen
gewählt hat, statt direkt bei Anthropic oder OpenAI: sein eigenes LLM-Gateway (LiteLLM,
Portkey...), Amazon Bedrock, Google Vertex, Microsoft Foundry oder Azure OpenAI. Das Modell ist
dasselbe; was sich ändert, ist, wer es verkauft und wohin die Anfragen gehen.

Latet liest diese Route aus den eigenen Einstellungen des Agenten und schaltet sich davor. Die
Anfragen erreichen denselben Endpunkt, mit denselben Schlüsseln, nachdem Latet sie geprüft (Claude
Code) oder maskiert (Codex) hat. Von Hand ist nichts einzurichten: `latet claude`, `latet codex`
und `latet global-install` erledigen das.

## So funktioniert es

- **Latet liest, wohin der Agent sendet.** Bei Claude Code: die Variablen Ihrer Shell, Ihre eigene
  `settings.json` und die verwalteten Einstellungen Ihrer Organisation, so wie Claude Code sie
  zusammenführt. Bei Codex: `config.toml` und die Dateien der Profile (`<name>.config.toml`). Die
  Einstellungen eines Repositorys werden nie gelesen: Ein Projekt, das Sie klonen, kann nicht
  entscheiden, wohin Ihre Anfragen gehen.
- **Die Adresse eines Unternehmens wird zu einem Ziel.** Sie wird in `config.yaml` unter
  [`upstreams`](../../reference/configuration/#upstreams-items) gespeichert, benannt nach der
  Adresse (aus `https://llm.acme.com` wird `llm-acme-com`), und der Agent sendet stattdessen an
  `http://127.0.0.1:5283/llm-acme-com`. Dieselbe Adresse erhält immer dasselbe Ziel.
  Einige Namen gehören zu Latets eigenen Routen, und ein Ziel kann sie nicht tragen:
  `anthropic`, `openai`, `chatgpt`, `openrouter`, `gemini`, `bedrock`, `vertex` und `backend-api`.
- **Bedrock und Vertex haben eigene Routen**, `/bedrock/<region>/` und `/vertex/`, die nur die
  Adressen von Amazon und Google erreichen.
- **Wenn Latet sich nicht davorschalten kann, sagt es das.** Der Agent startet nicht, und Latet
  sagt, warum und was zu tun ist. Es lässt nie stillschweigend einen Agenten an sich vorbei.

Für eine Sitzung übergibt `latet claude` seine Einstellungen mit der Option `--settings` von Claude
Code, die Vorrang vor Ihren Einstellungen und denen des Projekts hat; `latet codex` fügt
`-c`-Optionen hinzu. Dauerhaft schreibt `latet global-install` dasselbe in die Dateien der Agenten,
und `latet global-uninstall` stellt jede Adresse wieder her, die es ersetzt hat, auch wenn Sie die
Datei seitdem bearbeitet haben.

## Das Gateway Ihres Unternehmens

Claude Code, eingerichtet mit dem Gateway Ihres Unternehmens:

```json title="~/.claude/settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "https://llm.acme.com", "ANTHROPIC_AUTH_TOKEN": "sk-acme-..." } }
```

Mit `latet claude` sendet Claude Code an `http://127.0.0.1:5283/llm-acme-com`, und Latet leitet jede
Anfrage an `https://llm.acme.com` weiter, mit dem Schlüssel Ihres Unternehmens, so wie Claude Code
ihn gesendet hat. Codex funktioniert genauso, mit `openai_base_url` oder einem eigenen Anbieter:

```toml title="~/.codex/config.toml"
model_provider = "acme"

[model_providers.acme]
name = "Acme gateway"
base_url = "https://llm.acme.com/v1"
env_key = "ACME_API_KEY"
```

Nur die Adresse dieses Anbieters wird umgeleitet; sein Schlüssel und alles andere bleiben, wie Sie
sie geschrieben haben. Ein Anbieter, der in der Datei eines Profils steht
(`~/.codex/work.config.toml`), wird dort umgeleitet, und `latet codex --profile work` leitet die
Anbieter dieses Profils und seine `openai_base_url` über Latet.

## Amazon Bedrock

Claude Code auf Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`) weist auf eine von zwei Arten nach, wer es
ist, und Latet beherrscht beide.

- **Ein API-Schlüssel für Bedrock** (`AWS_BEARER_TOKEN_BEDROCK`) reist mit jeder Anfrage wie ein
  Passwort: Er geht unverändert durch Latet.
- **Ihre AWS-Anmeldung** (ein AWS-Profil, eine SSO-Anmeldung, Schlüssel in `~/.aws`): Claude Code
  signiert jede Anfrage für die Adresse, an die es sie sendet. Eine Signatur für die Adresse von
  Latet wäre bei Amazon nicht gültig, wie ein Einschreiben, dessen Adresse geändert wurde. Daher
  sendet Claude Code unsignierte Anfragen an Latet (`CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`), und Latet
  signiert jede davon für Amazon, nachdem es sie geprüft hat, mit Ihrem AWS-Profil.

Zum Signieren liest Latet Ihr Profil so, wie es die eigenen Tools von Amazon tun: mit der AWS CLI
(`aws configure export-credentials`), wenn sie installiert ist, was SSO-Anmeldungen und Rollen
abdeckt, oder aus den Schlüsseln in `~/.aws`. Latet sucht `aws` in seinem eigenen PATH, dann in
`/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`, `~/.local/bin` und `/snap/bin` (unter Windows in
`C:\Program Files\Amazon\AWSCLIV2`): Das Latet, das bei Ihrer Anmeldung am Rechner startet, sieht
den PATH Ihrer Shell nicht. Liegt die AWS CLI bei Ihnen woanders, starten Sie Latet aus einer
Shell, in der `aws` läuft. Die Zugangsdaten bleiben im Arbeitsspeicher von Latet, werden nirgends
gespeichert und signieren nur Anfragen an Amazon Bedrock. Wenn Ihre Anmeldung abläuft, sagt Claude
Code es in klaren Worten: `run aws sso login --profile work`; sobald Sie diesen Befehl ausgeführt
haben, funktioniert es wieder, ohne dass etwas neu gestartet werden muss.
Die eigenen Einstellungen `awsAuthRefresh` und `awsCredentialExport` von Claude Code werden nicht
verwendet, solange Latet signiert: Latet liest Ihr AWS-Profil selbst, führen Sie also die Anmeldung,
die sie ausgeführt hätten, selbst aus. `latet doctor` warnt, wenn sie gesetzt sind.

Die Route, die signiert, enthält einen Schlüssel, den nur Ihr Latet erzeugen kann
(`/bedrock/eu-west-1/profile/work/<key>`), sodass kein anderes Programm und kein anderer Benutzer
Ihres Rechners Anfragen in Ihrem Namen signieren lassen kann. Zugangsdaten, die nur in den
Variablen Ihrer Shell stehen (zum Beispiel aws-vault), übergibt `latet claude` für diese Sitzung an
Latet. Latet hält sie im Arbeitsspeicher, und nur, solange diese Sitzung läuft: `latet claude` hält
eine Verbindung zu Latet offen, und wenn es endet (auch wenn Sie den Prozess gewaltsam beenden),
vergisst Latet die Zugangsdaten innerhalb von Sekunden. Es vergisst sie auch, wenn sie selbst
ablaufen, sofern Ihr Tool ein Ablaufdatum setzt (`AWS_SESSION_EXPIRATION` oder
`AWS_CREDENTIAL_EXPIRATION`). Danach sagt Claude Code in klaren Worten, dass die Zugangsdaten weg
sind: Erneuern Sie sie, und starten Sie `latet claude` noch einmal. `global-install` braucht ein
AWS-Profil, das Latet selbst lesen kann.

Mit Ihrer AWS-Anmeldung ist die öffentliche Adresse von Bedrock, ausgeschrieben in Ihren
Einstellungen (`ANTHROPIC_BEDROCK_BASE_URL` auf `https://bedrock-runtime.eu-west-1.amazonaws.com`
gesetzt), dasselbe, als stünde dort keine: Latet signiert dafür. Verwendet wird die Region in der
Adresse, auch wenn `AWS_REGION` eine andere angibt: Die Adresse ist das, wohin die Anfragen gehen.
Die FIPS-Adresse von Bedrock und ein VPC-Endpunkt sind andere Fälle: siehe
[Wo sich Latet nicht davorschalten kann](#wo-sich-latet-nicht-davorschalten-kann).

Anfragen an Bedrock werden geprüft, nie umgeschrieben: Sie kommen von Claude Code, dessen Plugin
sie bereits maskiert hat. Die Antworten von Bedrock erreichen Claude Code genau so, wie Amazon sie
gesendet hat; Latet liest sie nur, um sie im Dashboard anzuzeigen. Modell-IDs und ARNs von
Inferenzprofilen erreichen Amazon genau so, wie Claude Code sie geschrieben hat.

## Google Vertex

Claude Code auf Vertex (`CLAUDE_CODE_USE_VERTEX=1`) sendet Ihre Google-Anmeldung als Token, das
unverändert durch Latet geht: Es gibt nichts zu signieren. Jede Anfrage nennt ihren Standort, und
Latet sendet sie an die passende Adresse von Google: den globalen Endpunkt, die Multiregionen `eu`
und `us` oder eine Region wie `us-east5`. Verschiedene Modelle in verschiedenen Regionen
funktionieren ebenfalls.

## Microsoft Foundry und Azure OpenAI

Claude Code auf Microsoft Foundry (`CLAUDE_CODE_USE_FOUNDRY=1`, mit `ANTHROPIC_FOUNDRY_RESOURCE`
oder `ANTHROPIC_FOUNDRY_BASE_URL`) und Codex auf Azure OpenAI (ein Anbieter, dessen `base_url`
`https://<resource>.openai.azure.com/openai/v1` ist) sind Ziele wie ein Gateway: Die Adresse der
Ressource wird gespeichert (`foundry-acme`, `azure-acme`), und der Schlüssel geht unverändert
durch.

## Ein Modell auf diesem Rechner

Eine Adresse auf diesem Rechner (etwa `http://localhost:11434` für Ollama) bleibt unberührt: Ein
Modell, das hier läuft, sendet nichts nach außen, und Latet kann es nicht von einem Proxy
unterscheiden, der in die Cloud weiterleitet. Ist es ein solcher Proxy, zum Beispiel LiteLLM, fügen
Sie ihn als Ziel hinzu, und Latet schaltet sich davor:

```sh
latet config add upstreams "{name: litellm, url: http://localhost:4000}"
```

Starten Sie danach eine neue Sitzung mit `latet claude` oder `latet codex`; wenn Sie Ihre Agenten
mit `latet global-install` eingerichtet haben, führen Sie `latet agents apply` erneut aus: Bis
dahin senden ihre Einstellungen weiter direkt an den Proxy.

Latet erinnert Sie jedes Mal daran, wenn es eine solche Adresse unberührt lässt. Adressen in Ihrem
lokalen Netzwerk und im Internet laufen immer über Latet.

## Wo sich Latet nicht davorschalten kann

- **Verwaltete Einstellungen, die eine Adresse erzwingen.** Wenn die verwalteten Einstellungen Ihrer
  Organisation `ANTHROPIC_BASE_URL` (oder die Adresse einer Cloud) auf etwas anderes setzen, kann
  nichts sie überschreiben. Latet sagt das und startet Claude Code nicht. Ihre IT kann diese Adresse
  auf Latet setzen, wie unten beschrieben.
- **Die Mantle API von Amazon Bedrock** (`CLAUDE_CODE_USE_MANTLE`): Latet kann sie noch nicht lesen.
  Verwenden Sie die Invoke API von Bedrock (`CLAUDE_CODE_USE_BEDROCK`) oder das Gateway Ihres
  Unternehmens.
- **Amazon Bedrock über einen VPC-Endpunkt** (eine `ANTHROPIC_BEDROCK_BASE_URL` auf
  `vpce-….vpce.amazonaws.com`): noch nicht unterstützt. Latet müsste für diesen Endpunkt signieren,
  und Amazon lehnt eine Signatur ab, die für die Adresse von Latet erstellt wurde. Latet sagt das und
  startet Claude Code nicht: Verwenden Sie die öffentliche Adresse von Bedrock oder das Gateway Ihres
  Unternehmens. Mit einem API-Schlüssel für Bedrock (`AWS_BEARER_TOKEN_BEDROCK`) muss nichts
  signiert werden, und der Endpunkt ist ein Ziel wie jede Adresse eines Unternehmens.
- **Die FIPS-Adresse von Amazon Bedrock** (eine `ANTHROPIC_BEDROCK_BASE_URL` auf
  `bedrock-runtime-fips.us-east-1.amazonaws.com`): noch nicht unterstützt. Latet kann nur an die
  Standardadresse senden, die kein FIPS-Endpunkt ist, also sagt es das und startet Claude Code nicht,
  statt Ihre FIPS-Anforderung fallen zu lassen. Verwenden Sie das Gateway Ihres Unternehmens oder
  die Standardadresse, wenn Sie FIPS nicht brauchen. Mit einem API-Schlüssel für Bedrock
  (`AWS_BEARER_TOKEN_BEDROCK`) muss nichts signiert werden, und die FIPS-Adresse bleibt das Ziel,
  wie jede Adresse eines Unternehmens.
- **Mehr als eine Cloud gleichzeitig** (etwa `CLAUDE_CODE_USE_BEDROCK` und
  `CLAUDE_CODE_USE_VERTEX`): Latet kann nicht erkennen, welche Claude Code verwendet, also sagt es
  das und startet Claude Code nicht. Behalten Sie eine.
- **Eine Adresse, die zwei Programme unterschiedlich lesen könnten** (mit einem Backslash, einem
  Leerzeichen, einem Benutzernamen oder Passwort darin), oder ein Server außerhalb Ihres lokalen
  Netzwerks, der über `http` erreicht wird: Latet leitet sie nicht weiter und sagt, warum.
- **Der Code-Tab von Claude Desktop** ignoriert diese Variablen: Das Plugin maskiert dort, aber der
  Daemon sieht die Anfragen nicht (siehe [Claude Code](../claude-code/#abdeckung)).
- **OpenCode** läuft mit Anthropic, OpenAI und OpenRouter über Latet; seine anderen Anbieter nicht.

## Latet für alle in einem Unternehmen

Wo verwaltete Einstellungen die Adresse von Claude Code festlegen, kann die IT sie auf das Latet
jedes Rechners richten und jedem Latet den Endpunkt des Unternehmens als Ziel geben:

```json title="managed-settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/llm-acme-com" } }
```

```sh
latet config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Jeder Rechner braucht außerdem das Plugin von Latet für Claude Code: Führen Sie dort
`latet global-install` aus oder starten Sie Claude Code mit `latet claude`. Ohne das Plugin
erreichen die Anfragen von Claude Code Latet unmaskiert, sodass Latet sie nur prüft und jede stoppt,
die einen sensiblen Wert im Klartext enthält. Die Adresse nennt den Port 5283: Auf einem Rechner,
dessen Latet auf einem anderen Port läuft, sagt Latet, dass die verwalteten Einstellungen Claude
Code woandershin senden, und startet es nicht.

## Prüfungen unterwegs

- **Das Plugin prüft jede Sitzung.** Wenn Claude Code startet, sieht das Plugin nach, wohin die
  Anfragen von Claude Code wirklich gehen, Einstellungen eingeschlossen. Wenn die
  `.claude/settings.json` eines Projekts festlegt, wohin sie gehen (eine Adresse, eine Cloud, ein
  Proxy), stoppt das Plugin die Prompts und nennt die Datei, auch wenn sie auf Latet zeigt:
  Schreiben Sie die Zeile stattdessen in Ihre eigenen Einstellungen. Ein Projekt kann das Plugin
  auch nicht auf ein anderes Latet richten. Wenn die Prüfung selbst fehlschlägt, werden die Prompts
  ebenfalls gestoppt.
- **`latet agents status`** zeigt die Route jedes Agenten. **`latet doctor`** zeigt sie ebenfalls,
  dazu eine Variable in Ihrer Shell, die von den Einstellungen von Claude Code überschrieben wird
  (nicht die Adresse Ihres Unternehmens, wenn Latet seine eigene davorgesetzt hat: So soll es sein),
  und ob die AWS CLI vorhanden ist, wenn Latet für Bedrock signiert.
- **Das Dashboard** listet Ihre Ziele unter Geschützte Agenten → Wohin Anfragen gehen auf, mit dem
  Agenten, der jedes davon genannt hat, und wann es zuletzt verwendet wurde, und zeigt bei jedem
  Austausch, wohin er ging und ob Latet ihn signiert hat. Wenn Sie dort eines entfernen,
  verschwindet genau dieses eine Ziel und kein anderes. Eines, das noch verwendet wird, kann nicht
  entfernt werden: Die Einstellungen von Claude Code oder Codex oder die verwalteten Einstellungen
  Ihrer Organisation zeigen darauf, oder es wurde seit dem Start von Latet registriert, durch eine
  Sitzung von `latet claude` oder `latet codex`, durch `latet agents apply` oder
  `latet global-install`. Latet sieht kein Ziel, das nur ein anderer Agent verwendet (OpenCode,
  Hermes, ein mit `latet run` gestartetes Programm): Vergewissern Sie sich, dass keiner es
  verwendet, bevor Sie es entfernen.
- **Das Verlegen eines Ziels wartet auf Sie.** Die Adresse eines Ziels zu ändern oder es zu
  entfernen, außerhalb des Dashboards (ein Befehl eines Agenten, eine von Hand bearbeitete
  `config.yaml`), wartet auf der Seite Freigaben auf Ihre Freigabe, mit der Adresse vorher und
  nachher: Es würde Ihre Anfragen und Ihre Schlüssel woandershin senden. Ein Ziel hinzuzufügen
  wartet nicht: Nur ein Agent, der darauf zeigt, verwendet es. Wenn eine Änderung wartet, während ein
  Agent eines hinzufügt, zeigt die Seite Freigaben die Änderung erneut, mit dem neuen Ziel darin und
  dem, was sie jetzt verlegen würde; eine Änderung, die nichts mehr senkt, gilt sofort.

## Womit es getestet wurde

Das echte Claude Code (2.1.294) und Codex (0.161.0), über Latet, gegen Anbieter, die so antworten
wie die jeweilige Cloud, auf einem Rechner: das Gateway eines Unternehmens, Bedrock mit einem
API-Schlüssel, mit einem Profil, für das Latet signiert hat, und mit einem Modell-ARN, Vertex und
ein Codex-Anbieter in der Form von Azure. Die Signaturen von Latet stimmen Anfrage für Anfrage mit
denen der eigenen Bibliothek von AWS überein. Wenn Sie eine dieser Clouds bei der Arbeit nutzen,
[erzählen Sie uns, wie es läuft](https://github.com/Latet-AI/latet/issues).
