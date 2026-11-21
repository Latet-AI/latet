// Mounted authority regressions: real ReportsPage/useReports, synthetic /me and deferred transports.
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
  const fixtureUrl = `${origin}${base}/authority-check?mock-server&mock-sign-in&mock-security`
  const fixtureHtml = await server.transformIndexHtml(`${base}/authority-check`, '<!doctype html><html><head></head><body></body></html>')
  browser = await chromium.launch()
  const context = await browser.newContext({ locale: 'en', timezoneId: 'UTC' })
  const page = await context.newPage()
  const errors = [], downloads = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('download', file => downloads.push(file.suggestedFilename()))
  await context.route('**/*', route => {
    if (route.request().url() === fixtureUrl) return route.fulfill({ contentType: 'text/html', body: fixtureHtml })
    if (new URL(route.request().url()).origin === origin) return route.continue()
    errors.push('unexpected external request'); return route.abort()
  })
  await page.goto(fixtureUrl)
  const result = await page.evaluate(async base => (await import(`${base}/scripts/reports-authority-fixture.mjs`)).checkAuthority(), base)
  console.log(JSON.stringify({ ...result, downloads, errors }, null, 2))
  assert.deepEqual(result.problems, [])
  assert.deepEqual(downloads, [])
  assert.deepEqual(errors, [])
  await context.close()
} finally {
  await browser?.close()
  await server.close()
}
