// Reports axis readability at the documentation viewport, including long localized dates.
/* global document */
import assert from 'node:assert/strict'
import { realpath } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const base = '/_latet/ui'
const cache = process.env.LATET_SCREENSHOT_CACHE
assert.ok(cache, 'a task-owned Vite cache is required')
const server = await createServer({ root, configFile: join(root, 'vite.config.ts'), configLoader: 'runner', envDir: false,
  cacheDir: cache, mode: 'mock', logLevel: 'warn', server: { host: '127.0.0.1', port: 0, hmr: false,
    fs: { strict: true, allow: [root, await realpath(join(root, 'node_modules'))] } } })
let browser
try {
  await server.listen()
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`
  browser = await chromium.launch()
  const problems = []
  const periods = ['7', '30', '90', 'custom&from=2026-07-12&to=2026-10-12', 'custom&from=2025-10-12&to=2026-10-12']
  for (const lang of ['en', 'it', 'de', 'fr', 'es']) {
    let minimumGap = Infinity
    const context = await browser.newContext({ locale: lang, timezoneId: 'UTC', viewport: { width: 1440, height: 900 } })
    await context.clock.setFixedTime(new Date('2026-10-12T10:30:00Z'))
    await context.route('**/*', route => {
      if (new URL(route.request().url()).origin === origin) return route.continue()
      problems.push(`${lang}: unexpected external request`); return route.abort()
    })
    const page = await context.newPage()
    page.on('pageerror', error => problems.push(`${lang}: ${error.message}`))
    for (const period of periods) {
      await page.goto(`${origin}${base}/reports?mock-server&mock-sign-in&mock-security&period=${period}`)
      await page.locator('figure.panel svg').nth(2).waitFor()
      await page.evaluate(() => document.fonts.ready)
      const measured = await page.locator('figure.panel svg').evaluateAll(svgs => svgs.slice(0, 3).map(svg => {
        const labels = [...svg.querySelectorAll('text')]
          .filter(node => node.textContent.trim() && Number(node.getAttribute('y')) >= svg.height.baseVal.value - 12)
          .map(node => { const box = node.getBoundingClientRect(); return { text: node.textContent, left: box.left, right: box.right } })
          .sort((a, b) => a.left - b.left)
        const points = [...svg.querySelectorAll('g[tabindex]')]
        return { labels, points: points.length, named: points.every(node => node.getAttribute('aria-label')?.trim()) }
      }))
      assert.equal(measured.length, 3)
      for (const [chart, view] of measured.entries()) {
        assert.ok(view.points > 0 && view.named, `${lang}/${period}/${chart}: every point keeps its full date and count`)
        assert.ok(view.labels.length >= 2, `${lang}/${period}/${chart}: readable dates remain on the axis`)
        for (let i = 1; i < view.labels.length; i++) {
          const previous = view.labels[i - 1], current = view.labels[i]
          const gap = current.left - previous.right
          minimumGap = Math.min(minimumGap, gap)
          if (gap < 4) problems.push(`${lang}/${period}/${chart}: ${previous.text} → ${current.text}, gap ${gap.toFixed(2)}px (${previous.right.toFixed(2)} → ${current.left.toFixed(2)})`)
        }
      }
      // The table keeps every bucket, even when the graph's axis displays fewer dates.
      const tableLabel = await page.evaluate(async base => (await import(`${base}/src/i18n.ts`)).default.t('charts.table'), base)
      const first = page.locator('figure.panel').first()
      await first.getByText(tableLabel, { exact: true }).click()
      await first.locator('tbody tr').first().waitFor()
      assert.equal(await first.locator('tbody tr').count(), measured[0].points)
    }
    await context.close()
    console.log(`Checked ${lang}: 7/30/90 days and 93/366-day weekly periods, all three axes and full tables; minimum gap ${minimumGap.toFixed(2)}px`)
  }
  assert.deepEqual(problems, [], 'localized axis labels must not overlap')
} finally {
  await browser?.close()
  await server.close()
}
