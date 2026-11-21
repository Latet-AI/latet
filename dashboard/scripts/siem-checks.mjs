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
const server = await createServer({ root, configFile: join(root, 'vite.config.ts'), configLoader: 'runner', envDir: false, cacheDir: cache, mode: 'mock', logLevel: 'warn',
  server: { host: '127.0.0.1', port: 0, hmr: false, fs: { strict: true, allow: [root, await realpath(join(root, 'node_modules'))] } } })
let browser
try {
  await server.listen()
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`
  const fixtureUrl = `${origin}${base}/siem-check?mock-server&mock-sign-in&mock-security`
  const html = await server.transformIndexHtml(`${base}/siem-check`, '<!doctype html><html><head></head><body></body></html>')
  browser = await chromium.launch()
  const context = await browser.newContext({ locale: 'en', timezoneId: 'UTC' })
  const page = await context.newPage(), errors = [], consoleProblems = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', msg => { if (['error', 'warning'].includes(msg.type()) || msg.text().includes('synthetic-secret-canary')) consoleProblems.push(msg.type()) })
  await context.route('**/*', route => {
    if (route.request().url() === fixtureUrl) return route.fulfill({ contentType: 'text/html', body: html })
    if (new URL(route.request().url()).origin === origin) return route.continue()
    errors.push('unexpected external request'); return route.abort()
  })
  await page.goto(fixtureUrl)
  const result = await page.evaluate(async base => (await import(`${base}/scripts/siem-fixture.mjs`)).checkSiem(), base)
  console.log(JSON.stringify({ ...result, errors, consoleProblems }, null, 2))
  assert.deepEqual(result.problems, []); assert.deepEqual(errors, []); assert.deepEqual(consoleProblems, [])
  // Exercise the real router and sidebar in addition to the controlled mounted fixture.
  await page.goto(`${origin}${base}/reports/siem?mock-server&mock-sign-in&mock-security`)
  await page.locator('section[aria-label="syslog"]').waitFor()
  const current = await page.locator('nav a[aria-current="page"]').evaluateAll(links => links.map(link => new URL(link.href).pathname))
  console.log(JSON.stringify({ currentNavigation: current }))
  assert.deepEqual(current, [`${base}/reports/siem`], 'only SIEM is the current sidebar page')
  await page.goto(`${origin}${base}/reports/siem?mock-server&mock-sign-in&mock-security&mock-siem-only`)
  await page.locator('section[aria-label="syslog"]').waitFor()
  assert.equal(await page.locator(`a[href="${base}/reports"]`).count(), 0, 'SIEM-only licensing has no Reports link')
  for (const license of ['', '&mock-siem-only']) {
    await page.goto(`${origin}${base}/reports/siem?mock-server&mock-sign-in&mock-auditor&mock-siem-local${license}`)
    await page.locator('section[aria-label="local"]').waitFor()
    const actions = await page.evaluate(async base => (await (await import(`${base}/src/api/client.ts`)).api.me()).edition.actions, base)
    assert.deepEqual([...actions].sort(), ['reports.group', 'reports.all', 'audit.read', 'policy.read'].sort(), 'mock auditor matches the real action contract')
    assert.ok((await page.locator('section[aria-label="syslog"]').textContent()).includes('tls://syslog.acme.com:6514'))
    assert.ok((await page.locator('section[aria-label="local"]').textContent()).includes('latet server siem test'))
    assert.equal(await page.locator('main input[type="password"]').count(), 0)
    assert.equal(await page.getByRole('button', { name: 'Send a test event', exact: true }).count(), 0)
    assert.equal(await page.getByRole('button', { name: 'Edit', exact: true }).count(), 0)
    if (license) assert.equal(await page.locator(`a[href="${base}/reports"]`).count(), 0, 'SIEM-only auditor has no Reports link')
  }
  assert.deepEqual(errors, []); assert.deepEqual(consoleProblems, [])
  console.log(JSON.stringify({ realRoutes: 4, navigationAndReaderChecks: 'passed' }))
  await context.close()
} finally { await browser?.close(); await server.close() }
