// The documentation's screenshots of the dashboard: each screen in every language and both themes, from the mock's
// English example data, at 1440 × 900, written to docs/src/assets/screens/<lang>/<screen>-<light|dark>.webp.
//
//   npm run screenshots                every screen, every language, both themes
//   npm run screenshots -- live it     only the screens and the languages named, both themes
//
// Playwright drives the Chromium of its pinned version (`npx playwright install chromium` fetches it once), so an
// image changes only where the dashboard did. The clock is fixed: every time the dashboard shows is the same at
// every run.

// The functions given to page.evaluate and addInitScript run in the page, with its globals.
/* global document, window, getComputedStyle, Notification */

import { mkdir, realpath, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const OUT = join(root, '..', 'docs', 'src', 'assets', 'screens')
const BASE = '/_latet/ui'
const LANGUAGES = ['en', 'it', 'de', 'fr', 'es']
const THEMES = ['light', 'dark']
const SIZE = { width: 1440, height: 900 }
const CLOCK = new Date('2026-10-12T10:30:00Z')
const QUALITY = 85

// Each screen: where it is, and what is done there before the picture.
const SCREENS = {
  // Every request as it happens.
  live: async (page) => {
    await open(page, '/live')
    await page.locator('a[href*="/exchanges/"]').first().waitFor()
  },
  // An exchange side by side: the fixture the agent wrote, with a customer's record in it.
  exchange: async (page, ids) => {
    await open(page, `/exchanges/${ids.exchange}`)
    await page.locator('#response').waitFor()
    await top(page, 'nav:has(+ #request)') // the exchange's flow, then its request
  },
  // The same exchange, a masked value clicked: what it is, which rule found it, what to do about it.
  inspector: async (page, ids) => {
    await open(page, `/exchanges/${ids.exchange}`)
    await page.locator('#response').waitFor()
    await top(page, 'nav:has(+ #request)') // the exchange's flow, then its request
    // The card takes the value's project when it opens: opened before the project is known, it offers no choice
    // between this project and everywhere. Opened again until it does.
    const value = page.locator('#request mark[data-pair]').filter({ hasText: 'Whitfield' }).first()
    const choice = page.locator('[role="dialog"] [role="radiogroup"]').first()
    for (let tries = 0; ; tries++) {
      await value.click()
      if (await choice.waitFor({ timeout: 1500 }).then(() => true, () => false)) break
      if (tries === 5) throw new Error('the masked value card never offered this project or everywhere')
      await page.keyboard.press('Escape')
      await page.waitForTimeout(500)
    }
  },
  // Texts waiting for approval: a name the local reviewer suggested, masked before the text leaves. The page opens on
  // its Texts tab, the change to the settings the sample has waiting counted on the Settings tab.
  approvals: async (page) => {
    await open(page, '/approvals')
    const suggested = page.locator('mark.mask-suggested').first()
    await suggested.waitFor()
    await suggested.click()
    await page.locator('mark.mask-pending').first().waitFor()
  },
  // A project's rules: Rules, with the project picked at the top, open on its Overview tab.
  project: async (page, ids) => {
    await open(page, `/rules?project=${encodeURIComponent(ids.project)}`)
    await page.locator('#effective').waitFor()
  },
  // A list of the rules as a table: the Terms tab, a row open in the side panel where it is edited and saved.
  rules: async (page) => {
    await open(page, '/rules#terms')
    const row = page.locator('#terms tbody tr').filter({ hasText: 'Whitfield' })
    await row.waitFor()
    await row.click()
    await page.locator('[data-rule-panel]').waitFor()
    await settle(page)
  },
  // The local reviewer's settings, in Rules (its tab, Reviewers).
  reviewer: async (page) => {
    await open(page, '/rules#reviewer')
    // The API key's line says "no key" until the key's state has come (the sample has one saved: its field then has
    // Save and Remove beside it), and the section grows as the editor loads: the picture waits for both.
    await page.waitForFunction(
      () => document.querySelector('#reviewer input[type="password"]')?.parentElement?.querySelectorAll('button').length === 2,
    )
    await settle(page)
    await top(page, '#reviewer')
  },
}

/**
 * What the screens open, read once from the mock's own modules: the exchange whose new blocks are the fixture the
 * agent writes (`customer.json`), and Claude Code's project.
 */
async function lookups(browser, origin) {
  const page = await browser.newPage()
  try {
    await page.goto(`${origin}${BASE}/live`)
    await page.locator('main').waitFor()
    return await page.evaluate(async (base) => {
      const { initialExchanges } = await import(`${base}/src/mock/fixtures.ts`)
      const { projectOfAgent } = await import(`${base}/src/mock/projects.ts`)
      const text = (slot) => slot.parts.map((part) => (typeof part === 'string' ? part : part.original)).join('')
      const write = initialExchanges.find((exchange) =>
        exchange.slots.some((slot) => slot.new && slot.kind === 'tool_input' && text(slot).includes('customer.json')),
      )
      if (!write) throw new Error('no exchange writes customer.json in the mock')
      return { exchange: write.summary.id, project: projectOfAgent('claude-code').id }
    }, BASE)
  } finally {
    await page.close()
  }
}

async function open(page, path) {
  await page.goto(`${page.context().origin}${BASE}${path}`)
  await page.locator('main').waitFor()
  await page.evaluate(() => document.fonts.ready)
  await settle(page)
}

/** Waits until the page stops growing: what it loads after the first paint is in place (3 s at most). */
async function settle(page) {
  await page.evaluate(async () => {
    const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
    let height = -1
    for (let tries = 0; tries < 15 && height !== document.documentElement.scrollHeight; tries++) {
      height = document.documentElement.scrollHeight
      await pause(200)
    }
  })
}

/** Scrolls the page so that a section starts at the top: just below the bar that stays there, else a little below. */
async function top(page, selector) {
  const section = page.locator(selector)
  await section.waitFor()
  await section.evaluate((element) => {
    element.scrollIntoView({ block: 'start' })
    const bar = [...document.querySelectorAll('main *')]
      .filter((node) => getComputedStyle(node).position === 'sticky' && Math.abs(node.getBoundingClientRect().top) <= 1)
      .reduce((bottom, node) => Math.max(bottom, node.getBoundingClientRect().bottom), 0)
    window.scrollBy(0, element.getBoundingClientRect().top - (bar ? bar + 4 : 12))
  })
}

async function shoot(browser, origin, ids, screen, lang, theme) {
  const context = await browser.newContext({
    viewport: SIZE,
    deviceScaleFactor: 1,
    locale: lang,
    timezoneId: 'UTC',
    colorScheme: theme,
    reducedMotion: 'reduce',
  })
  context.origin = origin
  await context.clock.setFixedTime(CLOCK)
  await context.addInitScript(
    ([language, mode]) => {
      localStorage.setItem('latet.language', JSON.stringify(language))
      localStorage.setItem('latet.theme', JSON.stringify(mode))
      // A browser that has not been asked yet, as a reader's is: headless Chromium refuses notifications.
      if ('Notification' in window) Object.defineProperty(Notification, 'permission', { get: () => 'default' })
    },
    [lang, theme],
  )
  const page = await context.newPage()
  try {
    await SCREENS[screen](page, ids)
    // The tag that says the data is a sample, and the notes that come and go, are not what a reader looks at.
    await page.addStyleTag({ content: '[data-mock-tag],[data-toasts]{display:none!important}' })
    await page.waitForTimeout(250)
    const cdp = await context.newCDPSession(page)
    const { data } = await cdp.send('Page.captureScreenshot', { format: 'webp', quality: QUALITY })
    const file = join(OUT, lang, `${screen}-${theme}.webp`)
    await mkdir(dirname(file), { recursive: true })
    await writeFile(file, Buffer.from(data, 'base64'))
    return file
  } finally {
    await context.close()
  }
}

async function main() {
  const args = process.argv.slice(2)
  const screens = args.filter((arg) => arg in SCREENS)
  const languages = args.filter((arg) => LANGUAGES.includes(arg))
  const unknown = args.filter((arg) => !(arg in SCREENS) && !LANGUAGES.includes(arg))
  if (unknown.length) {
    console.error(`Unknown: ${unknown.join(', ')}. Screens: ${Object.keys(SCREENS).join(', ')}; languages: ${LANGUAGES.join(', ')}.`)
    process.exit(2)
  }
  const server = await createServer({
    root,
    configFile: join(root, 'vite.config.ts'),
    mode: 'mock',
    logLevel: 'warn',
    // Port 0: one the system knows is free (Windows reserves blocks of ports that a fixed one may fall in).
    // node_modules may be a link to another checkout's: Vite serves the files it resolves there (the fonts) only when
    // their real folder is allowed too, else the fonts get a 403 and the pictures show the fallback ones.
    server: { host: '127.0.0.1', port: 0, fs: { strict: true, allow: [root, await realpath(join(root, 'node_modules'))] } },
  })
  await server.listen()
  const origin = server.resolvedUrls.local[0].replace(/\/_latet\/ui\/?$/, '').replace(/\/$/, '')
  const browser = await chromium.launch()
  let made = 0
  try {
    const ids = await lookups(browser, origin)
    // One language at a time per worker, the languages side by side.
    await Promise.all(
      (languages.length ? languages : LANGUAGES).map(async (lang) => {
        for (const screen of screens.length ? screens : Object.keys(SCREENS)) {
          for (const theme of THEMES) {
            const file = await shoot(browser, origin, ids, screen, lang, theme)
            made++
            console.log(`  ${file.slice(OUT.length + 1).replaceAll('\\', '/')}`)
          }
        }
      }),
    )
  } finally {
    await browser.close()
    await server.close()
  }
  console.log(`${made} screenshot(s) in ${OUT}`)
}

await main()
