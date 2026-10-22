// Checks of the documentation Latet serves itself, at /_latet/docs/ of a real daemon, in a real browser: what the
// mock's dev server cannot show (ui-checks.mjs reads where the links lead; these open them). The daemon serves the
// built dashboard (`npm run build` here) and the bundle (`npm run build:bundle` in docs/, then
// scripts/check_docs_bundle.py from the repository's root). Then:
//
//   npm run docs-checks -- http://127.0.0.1:<port> <its data folder's bridge.token>
//   npm run docs-checks -- http://127.0.0.1:<port> <token file> search     only the checks whose names hold a word
//
// Each check signs in with a one-time code, as `latet dashboard` does, in a fresh context of Playwright's Chromium,
// which may reach the daemon's origin alone: a request anywhere else is refused, and fails the check; so does an
// error on a page, or a breach of a page's security policy.

// The functions given to page.evaluate and addInitScript run in the page, with its globals.
/* global document, window */

import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { chromium } from 'playwright'

/**
 * The dashboard in Italian: its Rules page's "Documentazione" opens the Italian guide at the Rules section, from the
 * daemon; the "?" of Allowed values opens the profiles page at its allow lists, in Italian too.
 */
async function localDocsOpenInTheDashboardLanguage({ page, context, origin }) {
  await page.addInitScript(() => window.localStorage.setItem('latet.language', '"it"'))
  await page.goto(`${origin}/_latet/ui/rules#allow`)
  const header = page.locator('main header').getByRole('link', { name: 'Documentazione', exact: true })
  await waitForHref(header, `${origin}/_latet/docs/it/dashboard/#regole`)
  const docs = await opened(context, header)
  assert.equal(docs.url(), `${origin}/_latet/docs/it/dashboard/#regole`)
  assert.equal(await docs.evaluate(() => document.documentElement.lang), 'it')
  await docs.locator('h3#regole').waitFor()
  assert.match(await docs.title(), /Guida alla dashboard/)
  const help = page.locator('#allow').getByRole('link', { name: /^Documentazione: / })
  const section = await opened(context, help)
  assert.equal(section.url(), `${origin}/_latet/docs/it/concepts/profiles/#valori-consentiti`)
  await section.locator('h2#valori-consentiti').waitFor()
}

/** The documentation's own search works under its strict policy: Pagefind loads, and finds "vault". */
async function localDocsSearchWorks({ page, origin }) {
  await page.goto(`${origin}/_latet/docs/`)
  await page.locator('button[data-open-modal]').click()
  const field = page.locator('dialog[open] .pagefind-ui__search-input')
  await field.waitFor()
  await field.fill('vault')
  const results = page.locator('dialog[open] .pagefind-ui__result')
  await results.first().waitFor({ timeout: 10000 })
  assert.ok((await results.count()) > 0, 'Pagefind found nothing for "vault"')
  const link = await results.first().locator('a').first().getAttribute('href')
  assert.ok(link?.startsWith('/_latet/docs/'), `a result leads out of the bundle: ${link}`)
}

/** Three pages of the documentation, read one after the other, ask nothing of any host but the daemon. */
async function localDocsFetchNothingElsewhere({ page, origin, requests }) {
  for (const path of ['/_latet/docs/', '/_latet/docs/dashboard/', '/_latet/docs/it/concepts/profiles/']) {
    const answer = await page.goto(`${origin}${path}`)
    assert.equal(answer?.status(), 200, `${path}: ${answer?.status()}`)
    await page.waitForLoadState('networkidle')
  }
  const seen = new Set(requests.map((url) => new URL(url).origin))
  assert.deepEqual([...seen], [origin], `requests went to ${[...seen].join(', ')}`)
  // The product video is a link to docs.latet.ai, followed by no request until it is clicked.
  await page.goto(`${origin}/_latet/docs/`)
  const video = page.locator('.video-link a')
  assert.equal(await video.getAttribute('href'), 'https://docs.latet.ai/')
  assert.equal(await page.locator('video').count(), 0, 'the bundle plays a video')
}

const CHECKS = [localDocsOpenInTheDashboardLanguage, localDocsSearchWorks, localDocsFetchNothingElsewhere]

/** Until the dashboard has heard from GET /me that the daemon serves its documentation, a link leads to docs.latet.ai. */
async function waitForHref(link, href) {
  await link.waitFor()
  const element = await link.elementHandle()
  await link
    .page()
    .waitForFunction(([shown, want]) => shown.href === want, [element, href], { timeout: 5000 })
    .catch(async () => {
      throw new Error(`the link leads to ${await element.evaluate((shown) => shown.href)}, not ${href}`)
    })
}

/** The page a link opens in a new tab, once it has loaded. */
async function opened(context, link) {
  const [popup] = await Promise.all([context.waitForEvent('page'), link.click()])
  await popup.waitForLoadState('load')
  return popup
}

async function signedIn(context, origin, token) {
  const answer = await fetch(`${origin}/_latet/admin/login-code`, { method: 'POST', headers: { 'x-latet-token': token } })
  assert.equal(answer.status, 200, `the daemon gave no sign-in code (${answer.status})`)
  const { code } = await answer.json()
  const page = await context.newPage()
  await page.goto(`${origin}/_latet/ui/login?code=${encodeURIComponent(code)}`)
  await page.close()
}

async function run(browser, origin, token, check) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'en', reducedMotion: 'reduce' })
  const requests = []
  const forbidden = []
  const errors = []
  try {
    await context.route('**/*', (route) => {
      const url = route.request().url()
      requests.push(url)
      if (new URL(url).origin === origin) return route.continue()
      forbidden.push(url)
      return route.abort('blockedbyclient')
    })
    // A breach of a page's security policy (an inline script whose hash it does not list, a fetch elsewhere).
    await context.addInitScript(() => {
      document.addEventListener('securitypolicyviolation', (event) => {
        console.error(`CSP: ${event.violatedDirective} refused ${event.blockedURI || 'inline'}`)
      })
    })
    context.on('page', (page) => {
      page.on('pageerror', (error) => errors.push(`${page.url()}: ${error.message}`))
      page.on('console', (message) => {
        if (message.type() === 'error') errors.push(`${page.url()}: ${message.text()}`)
      })
    })
    await signedIn(context, origin, token)
    requests.length = 0
    const page = await context.newPage()
    page.setDefaultTimeout(8000)
    await check({ page, context, origin, requests })
    assert.deepEqual(errors, [], 'errors on a page')
    assert.deepEqual(forbidden, [], 'requests off the daemon')
  } finally {
    await context.close()
  }
}

async function main() {
  const [origin, tokenFile, ...words] = process.argv.slice(2)
  if (!origin || !tokenFile) {
    console.error('Usage: npm run docs-checks -- http://127.0.0.1:<port> <path of the daemon’s bridge.token> [words]')
    process.exit(2)
  }
  const token = (await readFile(tokenFile, 'utf8')).trim()
  const selected = CHECKS.filter((check) => !words.length || words.some((word) => check.name.toLowerCase().includes(word.toLowerCase())))
  const browser = await chromium.launch({ headless: true })
  let failed = 0
  try {
    for (const check of selected) {
      try {
        await run(browser, origin.replace(/\/$/, ''), token, check)
        console.log(`PASS ${check.name}`)
      } catch (error) {
        failed++
        console.log(`FAIL ${check.name}\n     ${String(error.message ?? error).replaceAll('\n', '\n     ')}`)
      }
    }
  } finally {
    await browser.close()
  }
  console.log(`${selected.length - failed} passed, ${failed} failed`)
  process.exit(failed ? 1 : 0)
}

await main()
