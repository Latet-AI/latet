/* global document */
// Real browser geometry for each new localized navigation label, active and inactive in both themes.
import assert from 'node:assert/strict'
import { realpath } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'
const root = join(dirname(fileURLToPath(import.meta.url)), '..'), cache = process.env.LATET_SCREENSHOT_CACHE
assert.ok(cache)
const server = await createServer({ root, configFile: join(root, 'vite.config.ts'), configLoader: 'runner', envDir: false, cacheDir: cache, mode: 'mock', logLevel: 'warn', server: { host: '127.0.0.1', port: 0, hmr: false, fs: { strict: true, allow: [root, await realpath(join(root, 'node_modules'))] } } })
let browser
try {
  await server.listen(); const origin = `http://127.0.0.1:${server.httpServer.address().port}`, base = '/_latet/ui'
  browser = await chromium.launch(); const problems = [], errors = []
  for (const language of ['en', 'it', 'de', 'fr', 'es']) for (const theme of ['light', 'dark']) {
    const context = await browser.newContext({ locale: language, timezoneId: 'UTC', colorScheme: theme, viewport: { width: 1440, height: 900 } })
    try {
      await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
      await context.clock.setFixedTime(new Date('2026-10-12T10:30:00Z'))
      await context.addInitScript(([lang, mode]) => { localStorage.setItem('latet.language', JSON.stringify(lang)); localStorage.setItem('latet.theme', JSON.stringify(mode)) }, [language, theme])
      const page = await context.newPage(); page.on('pageerror', () => errors.push('pageerror'))
      for (const screen of ['audit', 'people']) {
        await page.goto(`${origin}${base}/${screen}?mock-server&mock-sign-in`)
        await page.locator('main h1').waitFor(); await page.evaluate(() => document.fonts.ready)
        const geometry = await page.locator(`nav a[href="${base}/audit"]`).evaluate(link => {
          const span = link.querySelector('span'), range = document.createRange(); range.selectNodeContents(span)
          const row = link.getBoundingClientRect(), text = range.getBoundingClientRect(), lines = [...range.getClientRects()]
          return { label: span.textContent, active: link.getAttribute('aria-current') === 'page', lines: lines.length, row: { top: row.top, bottom: row.bottom, left: row.left, right: row.right }, text: { top: text.top, bottom: text.bottom, left: text.left, right: text.right } }
        })
        console.log(JSON.stringify({ language, theme, screen, ...geometry }))
        assert.equal(geometry.active, screen === 'audit')
        if (geometry.lines !== 1 || geometry.text.top < geometry.row.top || geometry.text.bottom > geometry.row.bottom || geometry.text.left < geometry.row.left || geometry.text.right > geometry.row.right) problems.push(`${language}/${theme}/${screen}`)
      }
    } finally { await context.close() }
  }
  assert.deepEqual(errors, []); assert.deepEqual(problems, [], 'Audit navigation labels must fit inside their rows')
} finally { await browser?.close(); await server.close() }
