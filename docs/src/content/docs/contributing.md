---
title: Contributing
description: Set up a development environment, run the checks, keep the documentation up to date, and add a language.
---

Before starting on something large, open an issue to discuss it.

## Development setup

You need [uv](https://docs.astral.sh/uv/); it installs the Python version the project uses.

```sh
git clone https://github.com/Latet-AI/latet.git
cd latet
uv sync
```

Run Latet from the checkout with `uv run`:

```sh
uv run latet serve       # the daemon in the foreground, with its log in the terminal
uv run latet claude      # Claude Code with the plugin from src/latet/claude_plugin
uv run latet doctor
```

To keep a test instance apart from a Latet you use for real, set `LATET_DATA_DIR` and
`LATET_CONFIG` to a scratch folder and choose another port in that `config.yaml`.

`latet serve --passthrough` forwards traffic without masking, and `--capture <folder>` writes
every exchange to local files (credential headers redacted), which helps when you study a
protocol. Captures hold conversation content: never commit them (`notes/captures/` is ignored by
git for this reason).

## Checks

Run these before opening a pull request. CI runs them in its lint and test jobs, the tests on
Windows, macOS and Linux with more property-test examples (`HYPOTHESIS_PROFILE=ci`). CI also runs
`uv run python scripts/gen_notices.py --check`, the server tests, and the plugin, dashboard and
documentation checks described below, and it builds the installers and the container image.

```sh
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright
uv run python scripts/gen_reference.py --check
```

- **Reference pages.** The [CLI](../reference/cli/), [configuration](../reference/configuration/)
  and [detectors](../reference/detectors/) pages are generated from the code. After changing a
  command, a configuration key or a detector, regenerate them with
  `uv run python scripts/gen_reference.py`. Never edit them by hand.
- **Claude Code plugin.** `claude plugin validate src/latet/claude_plugin` validates the
  plugin with a compatible installed Claude Code. `claude plugin test`, run in
  `src/latet/claude_plugin/`, is not available in every installed runtime; use it only when that
  runtime exposes the test command.
- **False positives.** `tests/unit/test_fp_budget.py` runs the detectors over Python's standard
  library, which holds no real secrets, with a budget per profile that may only go down.
  `uv run python scripts/fp_corpus.py` prints what fires on a larger corpus, rule by rule.
- **Third-party notices.** After changing dependencies, run `npm ci` in `dashboard/` and in
  `docs/`, then regenerate them with `uv run python scripts/gen_notices.py`. Add `--check` to verify
  that the file is up to date without rewriting it; CI runs that check.

Ground rule: tests use synthetic data only. Never commit real secrets, real personal data or
traffic captures.

## Server tests

Use Docker and the existing helper for isolated test PostgreSQL, Valkey and PostgreSQL
with TLS. The first start downloads their images if absent:

```sh
uv run python scripts/server-services.py up
uv run python scripts/server-services.py env
```

Set the printed `LATET_TEST_DATABASE_URL`, `LATET_TEST_VALKEY_URL` and
`LATET_TEST_TLS_DATABASE_URL` in the test shell, using that shell's assignment syntax.
They name disposable test services, never production data. Set `LATET_TEST_REQUIRE_SERVICES=1`,
then run:

```sh
uv run pytest -q -m server
```

Each test uses its own database schema and Valkey prefix. CI's `server` job provides and
requires the services: an unavailable service must fail rather than silently skip.
Stop your helper-owned services with `uv run python scripts/server-services.py down`;
coordinate shared services with their owner.

Real-agent tests are opt-in with `LATET_AGENT_TESTS=1` and `uv run pytest -m agents`.
They run installed third-party programs against local fake providers, but those programs
may contact their own external hosts. Use isolated test homes and synthetic credentials;
local fixtures do not guarantee zero external traffic. Tests marked `live` use the local name
and text recognition models: they are skipped where those models are not installed, as in CI.

## Dashboard

The dashboard lives in `dashboard/` (React, Vite and [i18next](https://www.i18next.com)) and needs
Node.js 22.12 or newer.

```sh
cd dashboard
npm ci
npm run dev:mock    # the dashboard with sample data, no daemon needed
npm run lint
npm run check:i18n  # every key the code uses is in en.json, and every language has its keys
npm test
npm run build       # writes the files the daemon serves into src/latet/web/
```

A new string goes in `en.json` and, translated, in the other languages' files, in the same change:
`check:i18n` wants the same keys in every language.

## The tutorial

The chapters of `latet tutorial` are YAML files in `src/latet/tutorial/chapters/`: each step's
prompt and explanation, the pretend model's answer, and the checks that must hold after it. The
tests play every chapter from start to end through the real masking, twice, so a change to Latet
that makes a chapter untrue fails them: change the chapter with the behaviour. They also refuse a
chapter that names a side of the screen (on a narrow screen the page stacks its panes) or holds a
value a secret scanner would take for a provider's key.

From a checkout, `uv run latet tutorial` needs the dashboard built (`npm run build` in
`dashboard/`). The dashboard's mock mode plays the chapters too, with `?mock-tutorial`, from its own
copy in `dashboard/src/mock/tutorial.ts`: change it with the chapter.

## Documentation

The site lives in `docs/` and is built with [Starlight](https://starlight.astro.build). You need
Node.js 22.12 or newer.

```sh
cd docs
npm ci
npm run dev     # http://localhost:4321/
npm run build   # static site in docs/dist/
```

Pages are Markdown or MDX files in `docs/src/content/docs/`. The sidebar is defined in
`docs/astro.config.mjs`.

**English is the reference version.** A pull request that changes what Latet does updates the
English pages it affects, in the same pull request: a new option, command or behaviour is
documented in English before it is merged. Translations follow the English pages, never the other
way round. A pull request may change the English pages alone: the maintainers bring the other
languages in line.

**The dashboard's screenshots** are made by a script, from its sample data, in every language and both
themes: after a change that shows on one of them, run `npm run screenshots` in `dashboard/`
(`npm run screenshots -- live it` makes some only). The first time, `npx playwright install chromium`
fetches the browser it uses. An MDX page shows one with `<Screenshot name="live" alt="…" />`, after
importing `docs/src/components/Screenshot.astro` as `dashboard.mdx` does.

## How to add a language

### Documentation

The documentation exists in five languages: English at the root, the reference, and Italian,
German, French and Spanish under `/it/`, `/de/`, `/fr/` and `/es/` (the `locales` in
`docs/astro.config.mjs`). A page not translated yet shows the English one, with a notice. The
reference pages generated from the code stay English in every language.

- **Translating a page** is creating it under `docs/src/content/docs/<lang>/`, at the same path as
  the English one: `concepts/how-it-works.mdx` becomes `it/concepts/how-it-works.mdx`. Its imports
  and image paths go one `../` deeper, since the file is one folder further down.
- **Links between pages stay relative**, so a translated page keeps them in its language. A
  translated heading changes its anchor, so the links to it change too: `scripts/check_docs_links.py`
  finds the ones missed (run it from the repository's root after `npm run build`; CI runs it too).
- **The interface strings** Latet adds are in `docs/src/content/i18n/<lang>.json`, and the sidebar's
  labels in the `translations` of `docs/astro.config.mjs`. Starlight's own interface is already
  translated.
- **Another language** is one more entry in `locales`, its `docs/src/content/i18n/<lang>.json`, and
  its sidebar `translations`.

Run `npm run build` and check the result with `npm run preview`.

### Dashboard

The dashboard comes in English, Italian, German, French and Spanish. To add another (Portuguese here):

1. Copy `dashboard/src/locales/en.json` to `pt.json` and translate the values. Keep the keys and the
   `{{variables}}`, and add every plural form the language needs (`key_many` in Portuguese).
2. Run `npm run check:i18n`: it lists the keys missing or extra, the variables that differ and the
   plural forms the file lacks.
3. Register it in `dashboard/src/i18n.ts`, with its name in its own words (`LANGUAGE_NAMES`), and add
   it to the languages `dashboard/src/i18n.test.ts` expects. That test file uses Portuguese as a
   language the dashboard lacks (`pt-BR`, `docsUrl(…, 'pt')`): change those cases to another
   language. Until the documentation comes in the new language, make `docsUrl` lead to the English
   pages for it; once it does, add the language to `LANGUAGES` in `dashboard/scripts/screenshots.mjs`.

The dashboard opens in the language the reader chose in its menu, else in the first of the browser's
languages it has, else in English. `dashboard/README.md` has the details.

### Tutorial

A chapter's texts in another language are in `src/latet/tutorial/chapters/<lang>/<chapter>.yaml`:
its titles, explanations, prompts, the agent's lines and the model's turns, step by step, each with
the placeholders of its English text. The rest of the chapter, its values and its checks, comes
from the English file. Whoever changes a chapter's English texts changes them in every language.
The tests play every chapter in every language, and hold each language to its own editorial rules
(`tests/unit/tutorial/test_chapters.py`). A new language also needs its own rules in that file, in
`ON_SCREEN` and `JARGON`, or the tests fail.

A pull request may change a chapter's English texts alone. A text that gains or loses a
placeholder needs the same change in its other languages, or the tests fail; the maintainers
translate the rest.

## How to add a country

National ID and tax numbers live in `src/latet/engine/detectors/ids/regions/`, one module per
country, each a tuple of `IdSpec`s. VAT numbers live in `ids/vat.py`.

1. Write `regions/<cc>.py` (the ISO 3166-1 alpha-2 code): for each number, a pattern for how it is
   written, a validator, a generator of valid fakes in the same layout, its keywords, and when a
   keyword is required (`context`: `never`, `bare`, `code` or `always`). Write the validator from
   the issuing authority's public specification. python-stdnum is a test-only reference under the
   LGPL: never copy its code.
2. Register the module in `regions/__init__.py`, and the country in `ID_REGIONS` in
   `src/latet/config.py`.
3. Add a layout and the stdnum module, if there is one, to `tests/unit/test_ids_oracle.py`, and a
   keyword in the `test_ids.py` cases if needed. The oracle test checks 200 fakes per type and
   compares the validator with stdnum next to valid numbers; list deliberate differences there.
4. Add the type's label to the dashboard's five languages, `dashboard/src/locales/<lang>.json`
   (`rules.pii.types`), to `PII_TYPES` in `dashboard/src/lib/labels.ts` and its description to
   `PII_HELP` in `dashboard/src/pages/RulesPage.tsx`; add the type and the country to the mock's
   catalog too (`PII_CATALOG` and `id_regions_available` in `dashboard/src/mock/fixtures.ts`,
   `ID_TYPES` in `dashboard/src/mock/builder.ts`). Then run `uv run python scripts/gen_reference.py`
   for the reference tables.
5. Run the false-positive budget (`tests/unit/test_fp_budget.py`): no ID rule may fire on the
   standard-library sample.

## How to measure a local reviewer

Which local model works best as a [local reviewer](../concepts/llm-review/#how-fast-and-which-model)
is still open, and your measurements are welcome. Serve a model on this computer (llama.cpp, Ollama
or LM Studio), point `LATET_LLM_URL` and `LATET_LLM_MODEL` at it, and run the benchmark on its 93
invented cases:

```sh
uv run python scripts/llm_review_bench.py run --label <model> --variants A
uv run python scripts/llm_review_bench.py report
```

Send what it wrote in `benchmarks/llm-review/` (your rows in `results/`, and `tables.md`) in a pull
request, with your graphics card or CPU, the server and the model's quantization.
`benchmarks/llm-review/README.md` says how the published numbers were made.

## License

Latet is licensed under the Latet Sustainable Use License, its Enterprise Edition under the Latet
Enterprise License, and Latet is also offered under commercial licenses ([LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md)).
Contributions are accepted under the [contributor license agreement](https://github.com/Latet-AI/latet/blob/main/CLA.md):
you keep your copyright, and your contribution can be part of Latet under all of them. To agree, tick the
box "I agree to the Latet contributor license agreement (CLA.md)." in the template of your first
pull request.
