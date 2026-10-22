# Latet documentation

The Latet documentation site, built with [Astro](https://astro.build) and
[Starlight](https://starlight.astro.build). It is published at
<https://docs.latet.ai/> by `.github/workflows/docs.yml`.

## Development

Requires Node.js 22.12 or newer, like the dashboard.

```sh
cd docs
npm ci              # install the exact versions from package-lock.json
npm run dev         # dev server with live reload: http://localhost:4321/
npm run build       # static site in docs/dist/, including the Pagefind search index
npm run preview     # serve the built site locally
npm run build:bundle  # the copy Latet serves itself, in src/latet/docs/ (see below)
```

### The copy Latet serves itself

Each install of Latet serves the documentation of its own version at `/_latet/docs/`, where every
link of the dashboard opens it. `npm run build:bundle` builds that copy into `src/latet/docs/`
(git-ignored), which the wheel and the container image carry: the same pages under the base
`/_latet/docs/`, without the product video (a link to watch it on docs.latet.ai stands in its
place) and with each screenshot at 1440 px only. Then, from the repository's root,
`uv run python scripts/check_docs_bundle.py` checks it (every address under the base, nothing
fetched from another host, at most 14 MB zipped) and writes the hashes of its inline scripts to
`src/latet/docs/.csp.json`, which the daemon's security policy for these pages lists: the daemon
serves no copy without that file, and its links then go to docs.latet.ai.

Astro collects anonymous CLI telemetry by default. Turn it off on your machine with
`npx astro telemetry disable`, or set `ASTRO_TELEMETRY_DISABLED=1` (the CI workflow does).

If a build warns about "Duplicate id" for pages you just edited, the local content cache is stale:
delete `node_modules/.astro/` and build again.

## Layout

| Path | Contents |
| --- | --- |
| `astro.config.mjs` | Site settings, languages, logo, fonts, styles and sidebar |
| `src/content/docs/` | The pages, in English: the reference version |
| `src/content/i18n/` | UI strings of ours, one file per language |
| `src/pages/404.astro` | The page for an address that does not exist |
| `src/assets/` | The logo, and the images and videos the pages show |
| `src/styles/custom.css` | The site's colours and type, from the logo and the product video |
| `public/` | Static files copied as is (favicon) |

Links between pages are relative (for example `../quickstart/`), so they work in every language:
`scripts/check_docs_links.py` refuses an absolute one, and any link or anchor that lands nowhere, in CI.
Run it from the repository's root after a build: `uv run python scripts/check_docs_links.py`.

Search uses Starlight's built-in [Pagefind](https://pagefind.app) index, generated at build time
and served with the site. The fonts (Inter, Montserrat, JetBrains Mono) come from their `@fontsource`
packages and are served with the site too. There are no analytics, external fonts or CDNs.

The footer (`src/components/Footer.astro`) adds the legal line of latet.ai: who runs the site, with the VAT
number, and links to the privacy policy and the legal notice, which live on latet.ai (repository
`latet-site`) and cover these pages too. The name and VAT number are a copy of that repository's
`src/data/legal.ts`, in `src/data/legal.json`: change both together. The privacy policy says the site
loads nothing from other websites; keep it that way, or update the policy first.

The English pages are the reference version: a change to Latet updates them in the same pull
request. Translations are welcome; the "How to add a language" section of the Contributing page
(`src/content/docs/contributing.md`) says how.

## Deployment

The workflow builds the site on every push to `main` and on pull requests, and deploys it to GitHub
Pages on pushes to `main` (a private fork builds but does not deploy). Set **Settings > Pages >
Source** to "GitHub Actions" first.
