---
title: Mitwirken
description: Eine Entwicklungsumgebung einrichten, die Prüfungen ausführen, die Dokumentation aktuell halten und eine Sprache hinzufügen.
---

Bevor Sie mit etwas Größerem beginnen, eröffnen Sie ein Issue, um es zu besprechen.

Für eine Veröffentlichung folgen Sie [Latet veröffentlichen](../releasing/): zusammenführen,
die vollständige CI für den genauen main-Commit abwarten, dann das Tag erstellen.

## Entwicklungsumgebung

Sie brauchen [uv](https://docs.astral.sh/uv/); es installiert die Python-Version, die das Projekt
verwendet.

```sh
git clone https://github.com/Latet-AI/latet.git
cd latet
uv sync
```

Starten Sie Latet aus dem Checkout mit `uv run`:

```sh
uv run latet serve       # the daemon in the foreground, with its log in the terminal
uv run latet claude      # Claude Code with the plugin from src/latet/claude_plugin
uv run latet doctor
```

Um eine Testinstanz von einem Latet zu trennen, das Sie wirklich nutzen, setzen Sie `LATET_DATA_DIR`
und `LATET_CONFIG` auf einen Scratch-Ordner und wählen in dieser `config.yaml` einen anderen Port.

`latet serve --passthrough` leitet den Datenverkehr ohne Maskierung weiter, und `--capture <folder>`
schreibt jeden Austausch in lokale Dateien (Header mit Zugangsdaten geschwärzt), was hilft, wenn Sie
ein Protokoll untersuchen. Mitschnitte enthalten Inhalte von Unterhaltungen: Committen Sie sie nie
(aus diesem Grund ignoriert git `notes/captures/`).

## Prüfungen

Führen Sie diese Prüfungen aus, bevor Sie einen Pull-Request eröffnen. Die CI führt sie in ihren
Lint- und Test-Jobs aus, die Tests unter Windows, macOS und Linux mit mehr Property-Test-Beispielen
(`HYPOTHESIS_PROFILE=ci`). Außerdem führt die CI `uv run python scripts/gen_notices.py --check` aus,
die Servertests und die unten beschriebenen Prüfungen für Plugin, Dashboard und Dokumentation, und
sie baut die Installer und das Container-Image.

```sh
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright
uv run python scripts/gen_reference.py --check
```

- **Referenzseiten.** Die Seiten [CLI](../reference/cli/), [Konfiguration](../reference/configuration/)
  und [Detektoren](../reference/detectors/) werden aus dem Code erzeugt. Nachdem Sie einen Befehl,
  einen Konfigurationsschlüssel oder einen Detektor geändert haben, erzeugen Sie sie mit
  `uv run python scripts/gen_reference.py` neu. Bearbeiten Sie sie nie von Hand.
- **Claude-Code-Plugin.** `claude plugin validate src/latet/claude_plugin` prüft das Plugin
  mit einer kompatiblen installierten Version. `claude plugin test`, ausgeführt in
  `src/latet/claude_plugin/`, ist nicht in jeder installierten Laufzeitumgebung verfügbar;
  verwenden Sie es nur, wenn sie den Testbefehl anbietet.
- **Fehlalarme.** `tests/unit/test_fp_budget.py` lässt die Detektoren über die Standardbibliothek
  von Python laufen, die keine echten geheimen Werte enthält, mit einem Budget pro Profil, das nur
  sinken darf. `uv run python scripts/fp_corpus.py` gibt aus, was auf einem größeren Korpus
  anschlägt, Regel für Regel.
- **Hinweise zu Drittanbietern.** Nachdem Sie Abhängigkeiten geändert haben, führen Sie `npm ci` in
  `dashboard/` und in `docs/` aus und erzeugen sie dann mit `uv run python scripts/gen_notices.py`
  neu. Fügen Sie `--check` hinzu, um zu prüfen, ob die Datei aktuell ist, ohne sie neu zu schreiben;
  die CI führt diese Prüfung aus.

Grundregel: Tests verwenden nur synthetische Daten. Committen Sie nie echte geheime Werte, echte
personenbezogene Daten oder Mitschnitte des Datenverkehrs.

## Servertests

Verwenden Sie Docker und das vorhandene Hilfsskript für isolierte Testdienste mit
PostgreSQL, Valkey und PostgreSQL mit TLS. Beim ersten Start werden fehlende Images geladen:

```sh
uv run python scripts/server-services.py up
uv run python scripts/server-services.py env
```

Setzen Sie die ausgegebenen `LATET_TEST_DATABASE_URL`, `LATET_TEST_VALKEY_URL` und
`LATET_TEST_TLS_DATABASE_URL` mit der Syntax Ihrer Test-Shell. Sie bezeichnen kurzlebige
Testdienste, niemals Produktionsdaten. Setzen Sie `LATET_TEST_REQUIRE_SERVICES=1` und führen Sie aus:

```sh
uv run pytest -q -m server
```

Jeder Test verwendet sein eigenes Datenbankschema und Valkey-Präfix. Der CI-Job `server`
stellt die Dienste bereit und verlangt sie: ein unerreichbarer Dienst muss den Test
fehlschlagen lassen, statt ihn still zu überspringen. Stoppen Sie Ihre Hilfsskript-Dienste
mit `uv run python scripts/server-services.py down`; stimmen Sie gemeinsame Dienste mit ihrem Betreiber ab.

Tests realer Agenten werden mit `LATET_AGENT_TESTS=1` und `uv run pytest -m agents`
ausdrücklich aktiviert. Sie starten installierte Drittanbieterprogramme gegen lokale
Testanbieter; diese Programme können dennoch eigene externe Hosts kontaktieren.
Verwenden Sie isolierte Benutzerverzeichnisse und synthetische Zugangsdaten; lokale Fixtures
garantieren keinen ausbleibenden externen Verkehr. Mit `live` markierte Tests verwenden die lokalen
Modelle für Namens- und Texterkennung: Sie werden übersprungen, wo diese Modelle nicht installiert
sind, wie in der CI.

## Dashboard

Das Dashboard liegt in `dashboard/` (React, Vite und [i18next](https://www.i18next.com)) und braucht
Node.js 22.12 oder neuer.

```sh
cd dashboard
npm ci
npm run dev:mock    # the dashboard with sample data, no daemon needed
npm run lint
npm run check:i18n  # every key the code uses is in en.json, and every language has its keys
npm test
npm run build       # writes the files the daemon serves into src/latet/web/
```

Ein neuer Text kommt in `en.json` und, übersetzt, auch in die Dateien der anderen Sprachen, in
derselben Änderung: `check:i18n` verlangt in jeder Sprache dieselben Schlüssel.

## Das Tutorial

Die Kapitel von `latet tutorial` sind YAML-Dateien in `src/latet/tutorial/chapters/`: Prompt und
Erklärung jedes Schritts, die Antwort des simulierten Modells und die Prüfungen, die danach gelten
müssen. Die Tests spielen jedes Kapitel zweimal von Anfang bis Ende mit der echten Maskierung durch,
daher schlagen sie fehl, wenn eine Änderung an Latet ein Kapitel unwahr macht: Ändern Sie das Kapitel
zusammen mit dem Verhalten. Sie lehnen auch ein Kapitel ab, das eine Seite des Bildschirms nennt (auf
einem schmalen Bildschirm stapelt die Seite ihre Bereiche untereinander) oder das einen Wert enthält,
den ein Secret-Scanner für den Schlüssel eines Anbieters halten würde.

Aus einem Checkout braucht `uv run latet tutorial` das gebaute Dashboard (`npm run build` in
`dashboard/`). Auch der Mock-Modus des Dashboards spielt die Kapitel ab, mit `?mock-tutorial`, aus
seiner eigenen Kopie in `dashboard/src/mock/tutorial.ts`: Ändern Sie sie zusammen mit dem Kapitel.

## Dokumentation

Die Website liegt in `docs/` und wird mit [Starlight](https://starlight.astro.build) gebaut. Sie
brauchen Node.js 22.12 oder neuer.

```sh
cd docs
npm ci
npm run dev     # http://localhost:4321/
npm run build   # static site in docs/dist/
```

Die Seiten sind Markdown- oder MDX-Dateien in `docs/src/content/docs/`. Die Seitenleiste ist in
`docs/astro.config.mjs` definiert.

**Englisch ist die Referenzversion.** Ein Pull-Request, der ändert, was Latet tut, aktualisiert die
englischen Seiten, die davon betroffen sind, im selben Pull-Request: Eine neue Option, ein neuer
Befehl oder ein neues Verhalten wird auf Englisch dokumentiert, bevor die Änderung gemergt wird.
Übersetzungen folgen den englischen Seiten, nie umgekehrt. Ein Pull-Request darf nur die
englischen Seiten ändern: Die anderen Sprachen ziehen die Maintainer nach.

**Die Screenshots des Dashboards** erstellt ein Skript aus seinen Beispieldaten, in jeder Sprache und
in beiden Farbschemata: Führen Sie nach einer Änderung, die auf einem davon zu sehen ist,
`npm run screenshots` in `dashboard/` aus (`npm run screenshots -- live it` erstellt nur einige neu).
Beim ersten Mal lädt `npx playwright install chromium` den Browser herunter, den es verwendet. Eine
MDX-Seite zeigt einen mit `<Screenshot name="live" alt="…" />`, nachdem sie
`docs/src/components/Screenshot.astro` als `dashboard.mdx` importiert hat.

## So fügen Sie eine Sprache hinzu

### Dokumentation

Die Dokumentation gibt es in fünf Sprachen: Englisch auf der obersten Ebene, als Referenz, und
Italienisch, Deutsch, Französisch und Spanisch unter `/it/`, `/de/`, `/fr/` und `/es/` (die `locales`
in `docs/astro.config.mjs`). Eine noch nicht übersetzte Seite zeigt die englische, mit einem Hinweis.
Die aus dem Code erzeugten Referenzseiten bleiben in jeder Sprache englisch.

- **Eine Seite übersetzen** heißt, sie unter `docs/src/content/docs/<lang>/` anzulegen, unter
  demselben Pfad wie die englische: Aus `concepts/how-it-works.mdx` wird
  `it/concepts/how-it-works.mdx`. Ihre Importe und Bildpfade bekommen ein `../` mehr, da die Datei
  einen Ordner tiefer liegt.
- **Links zwischen Seiten bleiben relativ**, so behält eine übersetzte Seite sie in ihrer Sprache.
  Eine übersetzte Überschrift ändert ihren Anker, daher ändern sich auch die Links darauf:
  `scripts/check_docs_links.py` findet die übersehenen (führen Sie es nach `npm run build` im
  Stammverzeichnis des Repositorys aus; die CI führt es ebenfalls aus).
- **Die Texte der Oberfläche**, die Latet hinzufügt, stehen in `docs/src/content/i18n/<lang>.json`,
  die Beschriftungen der Seitenleiste in den `translations` von `docs/astro.config.mjs`. Die
  Oberfläche von Starlight selbst ist bereits übersetzt.
- **Eine weitere Sprache** ist ein weiterer Eintrag in `locales`, ihre
  `docs/src/content/i18n/<lang>.json` und ihre `translations` für die Seitenleiste.

Führen Sie `npm run build` aus, und prüfen Sie das Ergebnis mit `npm run preview`.

### Dashboard

Das Dashboard gibt es auf Englisch, Italienisch, Deutsch, Französisch und Spanisch. So fügen Sie
eine weitere Sprache hinzu (hier Portugiesisch):

1. Kopieren Sie `dashboard/src/locales/en.json` nach `pt.json`, und übersetzen Sie die Werte.
   Behalten Sie die Schlüssel und die `{{variables}}` bei, und fügen Sie jede Pluralform hinzu, die
   die Sprache braucht (`key_many` im Portugiesischen).
2. Führen Sie `npm run check:i18n` aus: Es listet die fehlenden oder überzähligen Schlüssel, die
   abweichenden Variablen und die Pluralformen auf, die der Datei fehlen.
3. Registrieren Sie die Sprache in `dashboard/src/i18n.ts`, mit ihrem Namen in der eigenen Sprache
   (`LANGUAGE_NAMES`), und fügen Sie sie den Sprachen hinzu, die `dashboard/src/i18n.test.ts`
   erwartet. Diese Testdatei nimmt Portugiesisch als eine Sprache, die dem Dashboard fehlt
   (`pt-BR`, `docsUrl(…, 'pt')`): Stellen Sie diese Fälle auf eine andere Sprache um. Solange es die
   Dokumentation nicht in der neuen Sprache gibt, lassen Sie `docsUrl` für sie auf die englischen
   Seiten führen; sobald es sie gibt, fügen Sie die Sprache zu `LANGUAGES` in
   `dashboard/scripts/screenshots.mjs` hinzu.

Das Dashboard öffnet sich in der Sprache, die in seinem Menü gewählt wurde, sonst in der ersten der
Sprachen des Browsers, die es anbietet, sonst auf Englisch. `dashboard/README.md` enthält die
Einzelheiten.

### Tutorial

Die Texte eines Kapitels in einer anderen Sprache stehen in
`src/latet/tutorial/chapters/<lang>/<chapter>.yaml`: seine Titel, Erklärungen und Prompts, die
Zeilen des Agenten und die Antworten des Modells, Schritt für Schritt, jeweils mit den Platzhaltern
ihres englischen Texts. Der Rest des Kapitels, seine Werte und seine Prüfungen, kommt aus der
englischen Datei. Wer die englischen Texte eines Kapitels ändert, ändert sie in jeder Sprache. Die
Tests spielen jedes Kapitel in jeder Sprache durch und prüfen jede Sprache nach ihren eigenen
redaktionellen Regeln (`tests/unit/tutorial/test_chapters.py`). Eine neue Sprache braucht in dieser
Datei auch ihre eigenen Regeln, in `ON_SCREEN` und `JARGON`, sonst schlagen die Tests fehl.

Ein Pull-Request darf nur die englischen Texte eines Kapitels ändern. Gewinnt oder verliert ein
Text einen Platzhalter, braucht er dieselbe Änderung in den anderen Sprachen, sonst schlagen die
Tests fehl; den Rest übersetzen die Maintainer.

## So fügen Sie ein Land hinzu

Nationale Identifikations- und Steuernummern liegen in `src/latet/engine/detectors/ids/regions/`,
ein Modul pro Land, jeweils ein Tupel aus `IdSpec`-Objekten. Umsatzsteuer-Identifikationsnummern
liegen in `ids/vat.py`.

1. Schreiben Sie `regions/<cc>.py` (der Code nach ISO 3166-1 alpha-2): für jede Nummer ein Muster
   dafür, wie sie geschrieben wird, einen Validator, einen Generator gültiger Ersatzwerte im selben
   Format, ihre Schlüsselwörter und wann ein Schlüsselwort erforderlich ist (`context`: `never`,
   `bare`, `code` oder `always`). Schreiben Sie den Validator nach der öffentlichen Spezifikation
   der ausstellenden Behörde. python-stdnum ist eine Referenz nur für die Tests, unter der LGPL:
   Kopieren Sie nie seinen Code.
2. Registrieren Sie das Modul in `regions/__init__.py` und das Land in `ID_REGIONS` in
   `src/latet/config.py`.
3. Fügen Sie `tests/unit/test_ids_oracle.py` ein Format und das stdnum-Modul hinzu, falls es eines
   gibt, und bei Bedarf ein Schlüsselwort in den Fällen von `test_ids.py`. Der Orakel-Test prüft
   200 Ersatzwerte pro Typ und vergleicht den Validator mit stdnum an Nummern, die knapp neben
   gültigen liegen; listen Sie beabsichtigte Abweichungen dort auf.
4. Fügen Sie die Bezeichnung des Typs in den fünf Sprachen des Dashboards hinzu,
   `dashboard/src/locales/<lang>.json` (`rules.pii.types`), in `PII_TYPES` in
   `dashboard/src/lib/labels.ts` und seine Beschreibung in `PII_HELP` in
   `dashboard/src/pages/RulesPage.tsx`; fügen Sie den Typ und das Land auch dem Katalog des Mocks
   hinzu (`PII_CATALOG` und `id_regions_available` in `dashboard/src/mock/fixtures.ts`, `ID_TYPES`
   in `dashboard/src/mock/builder.ts`). Führen Sie dann `uv run python scripts/gen_reference.py` für
   die Referenztabellen aus.
5. Führen Sie das Fehlalarm-Budget aus (`tests/unit/test_fp_budget.py`): Keine Regel für
   Identifikationsnummern darf auf der Stichprobe aus der Standardbibliothek anschlagen.

## So messen Sie einen lokalen Prüfer

Welches lokale Modell als [lokaler Prüfer](../concepts/llm-review/#wie-schnell-und-welches-modell) am
besten funktioniert, ist noch offen, und Ihre Messungen sind willkommen. Stellen Sie ein Modell auf
diesem Rechner bereit (llama.cpp, Ollama oder LM Studio), richten Sie `LATET_LLM_URL` und
`LATET_LLM_MODEL` darauf aus, und führen Sie den Benchmark mit seinen 93 erfundenen Fällen aus:

```sh
uv run python scripts/llm_review_bench.py run --label <model> --variants A
uv run python scripts/llm_review_bench.py report
```

Senden Sie, was er in `benchmarks/llm-review/` geschrieben hat (Ihre Zeilen in `results/` und
`tables.md`), in einem Pull-Request, mit Angabe Ihrer Grafikkarte oder CPU, des Servers und der
Quantisierung des Modells. `benchmarks/llm-review/README.md` beschreibt, wie die veröffentlichten Zahlen
entstanden sind.

## Lizenz

Latet steht unter der Latet Sustainable Use License, seine Enterprise Edition unter der Latet
Enterprise License, und Latet wird auch unter kommerziellen Lizenzen angeboten ([LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md)).
Beiträge werden gemäß der [Lizenzvereinbarung für Beitragende](https://github.com/Latet-AI/latet/blob/main/CLA.md)
angenommen: Sie behalten Ihr Urheberrecht, und Ihr Beitrag kann unter allen diesen Lizenzen Teil
von Latet sein. Um zuzustimmen, kreuzen Sie in der Vorlage Ihres ersten Pull-Requests das Kästchen
"I agree to the Latet contributor license agreement (CLA.md)." an.
