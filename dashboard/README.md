# Latet dashboard

The local web dashboard of Latet. It answers one question: **what exactly left my machine, and
how was it masked?** The daemon serves it at `http://127.0.0.1:5283/_latet/ui/`; it talks only to
the daemon's API under `/_latet/api/v1/` (see `docs/dashboard-api.md`).

React, TypeScript (strict), Vite, Tailwind CSS, React Router and i18next. Charts are small
hand-made SVG components. The fonts (Inter, Montserrat, JetBrains Mono) come from their `@fontsource`
packages and are served with the page; nothing is loaded from the internet, no CDNs or analytics, so
the page works under the daemon's `default-src 'self'` content security policy.

## Commands

Node.js 22.12 or newer. Run them in `dashboard/`.

| Command | What it does |
| --- | --- |
| `npm ci` | Install the exact dependencies of `package-lock.json`. |
| `npm run dev` | Dev server on `http://127.0.0.1:5173/_latet/ui/`, talking to the daemon on `127.0.0.1:5283`. |
| `npm run dev:mock` | The same with built-in example data, no daemon needed. `VITE_LATET_MOCK=1 npm run dev` does the same. |
| `npm run build` | Type check, then build into `../src/latet/web/` (hashed files under `assets/`), keeping its `.gitkeep`. |
| `npm run lint` | ESLint, including `i18next/no-literal-string`: user-visible text in JSX must come from a translation. |
| `npm run check:i18n` | Fails if a key used in the source is missing from `src/locales/en.json`, or if another language's keys, variables or plural forms do not match English (see Languages); lists unused keys. |
| `npm test` | Unit tests (Vitest) of the pure functions in `src/lib/`. |
| `npm run ui-checks` | Checks in a real browser (Playwright's headless Chromium) on the mock, served on a free port of 127.0.0.1 with every other address refused: what a page loads and what a click does. `npm run ui-checks -- strict` runs only the checks whose names hold that word. |
| `npm run docs-checks -- <daemon> <bridge.token>` | Checks of the documentation a real daemon serves at `/_latet/docs/` (the built dashboard and `npm run build:bundle` in `docs/`): a link opens it in the dashboard's language, its search works under its security policy, and reading it asks nothing of another host. |

Every page, section of the rules, rule panel and notice that says what happened links its documentation:
the targets are in `src/lib/docsLinks.json` (a page and an English anchor), their anchors in each translation
in `src/lib/docsAnchors.json`, written by `scripts/gen_docs_anchors.py` from the built documentation and
checked in CI by `scripts/check_docs_anchors.py` (see CONTRIBUTING.md). They open this install's own copy
when `GET /me` says it has one (`docs`), else docs.latet.ai.

### Real mode

`npm run dev` proxies `/_latet` (except the UI itself) to the daemon; set `LATET_DAEMON` to use
another address. To sign in through the dev server, run `latet dashboard --print-url` and open
the printed link with the port changed to `5173`: the daemon sets its session cookie (cookies are
per host, not per port) and redirects back to the dev server.

### Mock mode

Every page works on invented data: a Claude Code session in verify mode (one request blocked by the
daemon, one stopped by the plugin's final check), a Codex session over WebSocket in rewrite mode, an
OpenCode session through OpenRouter (one request blocked for carrying an image), an embeddings client
and an unknown route. A new exchange arrives every few seconds. Vault, rules, allow list,
custom terms, clear history and the playground work in memory. Keys and passwords in the fixtures
are generated at run time, so the source holds nothing a secret scanner would flag.

Add `?mock-401` or `?mock-offline` to the page address to see the sign-in screen or the error
state, and `?mock-ner-active` or `?mock-ner-missing` for the states of local name detection on the
Rules page. `?mock-tutorial` shows the tutorial's sandbox instead: an empty history, no feed of new
exchanges, and the nine chapters answering the tutorial's API (`?mock-tutorial=terms` offers that
chapter from the menu; `&mock-tutorial-fail` makes each step fail once, to see the page say so and try
again; `&mock-tutorial-no-ocr` leaves the sandbox without your text recognition models, to see the menu
and the screenshots chapter say what they need). Mock mode exists only in the dev server: a production
build never contains the fixtures.

## Layout

```
src/
  api/          types.ts (the contract), client.ts (fetch, SSE, errors, 401 handling)
  components/   shell, badges, charts, inspector and highlighted text (masked/), exchange views
  pages/        one file per page
  lib/          pure helpers (spans, restore alignment, stats, flags, JSON paths, formatting)
  locales/      translations (en.json, it.json, de.json, fr.json, es.json)
  mock/         mock transport and fixtures (dev server only)
scripts/        check-i18n.mjs and its rules (i18n-rules.mjs), keep-gitkeep.mjs
```

Offsets from the daemon count Unicode code points (Python string indices); `lib/spans.ts` converts
them to JavaScript string offsets, so texts with emoji highlight correctly.

## Colors

Tokens live in `src/index.css` (light and dark, following the OS or the toggle in the sidebar). The
palette is the documentation's: a green-tinted near-black, and one green for the brand. The four
entity classes (secret, personal data, identity, custom term) have one hue each, chosen so that every
pair stays distinct under protanopia and deuteranopia simulation, in both themes and with at least
3:1 contrast on the code background. Names found by a model share the personal-data hue with a dashed
underline. Status colors (blocked, warnings, verified) are separate from the brand and always come
with an icon and a label.

`src/theme.test.ts` holds all of it: text at 4.5:1 or more on its background (7:1 for body text),
marks and borders that carry meaning at 3:1, the classes apart under both simulations, and the brand
apart from the status colors.

## Languages

The dashboard comes in English, Italian, German, French and Spanish. All user-visible text lives in
`src/locales/<lang>.json` (`en`, `it`, `de`, `fr`, `es`), read through `t()` from `react-i18next`;
English is the reference and the fallback. Dates, numbers and durations are formatted with `Intl` in
the active language, and country names are sorted in it. What the daemon says (its errors, the
doctor's checks) stays in English.

The dashboard opens in the language the reader chose, else in the first of the browser's languages it
has, else in English. The language menu in the sidebar, above the theme switch, changes it at once; the
choice is kept in this browser (`latet.language` in local storage).

`npm run check:i18n` holds every language to English, with the rules of `scripts/i18n-rules.mjs`:

- the same keys: none missing, none that English lacks;
- the same `{{variables}}` in each string. A plural form has those of English's form of the same
  category (`_other`'s, for `_many`), though it may leave out or add `{{count}}`, and none that the
  key's English forms lack;
- every plural form the language needs, as `Intl.PluralRules` lists them: `key_one` and `key_other`
  in English and German, and `key_many` too in Italian, French and Spanish.

It fails on any of these, and counts per language the strings still the same as English (a warning:
some stay the same in every language, like the name Latet). A change that adds or changes a string
updates every language in the same change.

To add a sixth language (Portuguese here):

1. Copy `src/locales/en.json` to `src/locales/pt.json` and translate the values. Keep the keys and
   the `{{variables}}`, and add every plural form the language needs (`key_many` in Portuguese).
2. Run `npm run check:i18n`: it lists, for `pt.json`, the keys missing or extra, the variables that
   differ and the plural forms it lacks.
3. Register it in `src/i18n.ts`, with its name in its own words, and add it to the languages
   `src/i18n.test.ts` expects:

   ```ts
   import pt from './locales/pt.json'

   export const resources = {
     // …
     pt: { translation: pt },
   } as const

   export const LANGUAGE_NAMES: Record<Language, string> = {
     // …
     pt: 'Português',
   }
   ```

   The language menu lists it from then on.

Keys used through a variable are wrapped in `tk('…')` (see `src/lib/labels.ts`), so the check sees
them too. Commands shown for copying (`latet dashboard`…) are not translated.
