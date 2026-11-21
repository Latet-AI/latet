import assert from 'node:assert/strict'
import { realpath } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const root = join(dirname(fileURLToPath(import.meta.url)), '..'), base = '/_latet/ui', cache = process.env.LATET_SCREENSHOT_CACHE
assert.ok(cache, 'a task-owned Vite cache is required')
const server = await createServer({ root, configFile: join(root, 'vite.config.ts'), configLoader: 'runner', envDir: false, cacheDir: cache, mode: 'mock', logLevel: 'warn',
  server: { host: '127.0.0.1', port: 0, hmr: false, fs: { strict: true, allow: [root, await realpath(join(root, 'node_modules'))] } } })
let browser
try {
  await server.listen(); const origin = `http://127.0.0.1:${server.httpServer.address().port}`, fixtureUrl = `${origin}${base}/reports-query-check?mock-server&mock-sign-in&mock-security`
  const html = await server.transformIndexHtml(`${base}/reports-query-check`, '<!doctype html><html><head></head><body></body></html>')
  browser = await chromium.launch()
  const context = await browser.newContext({ locale: 'en', timezoneId: 'UTC' }), page = await context.newPage(), errors = [], consoleProblems = []
  await context.clock.setFixedTime(new Date('2026-10-12T10:30:00Z'))
  page.on('pageerror', error => errors.push(error.message)); page.on('console', msg => { if (['error', 'warning'].includes(msg.type())) consoleProblems.push(msg.text()) })
  await context.route('**/*', route => {
    if (route.request().url() === fixtureUrl) return route.fulfill({ contentType: 'text/html', body: html })
    if (new URL(route.request().url()).origin === origin) return route.continue()
    errors.push('unexpected external request'); return route.abort()
  })
  await page.goto(fixtureUrl)
  const mounted = await page.evaluate(async base => (await import(`${base}/scripts/reports-query-fixture.mjs`)).checkReportsQueryCommit(), base)
  console.log(JSON.stringify({ classification: mounted.boundaryValid ? 'boundary-observed' : 'INCONCLUSIVE', mounted, errors, consoleProblems }, null, 2))
  assert.ok(mounted.boundaryValid, 'INCONCLUSIVE: Reports B commit-before-setQuery boundary was not observed')
  assert.deepEqual(errors, []); assert.deepEqual(consoleProblems, []); assert.deepEqual(mounted.problems, [])
  await context.close()
} finally { await browser?.close(); await server.close() }
