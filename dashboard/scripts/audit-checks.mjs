/* global document */
import assert from 'node:assert/strict'
import { mkdir, realpath } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const root = join(dirname(fileURLToPath(import.meta.url)), '..'), base = '/_latet/ui', cache = process.env.LATET_SCREENSHOT_CACHE
assert.ok(cache, 'a task-owned Vite cache is required')
const visual = join(cache, 'audit-authority-visuals')
await mkdir(visual, { recursive: true })
const server = await createServer({ root, configFile: join(root, 'vite.config.ts'), configLoader: 'runner', envDir: false, cacheDir: cache, mode: 'mock', logLevel: 'warn',
  server: { host: '127.0.0.1', port: 0, hmr: false, fs: { strict: true, allow: [root, await realpath(join(root, 'node_modules'))] } } })
let browser
try {
  await server.listen(); const origin = `http://127.0.0.1:${server.httpServer.address().port}`
  const fixtureUrl = `${origin}${base}/audit-check?mock-server&mock-sign-in`
  const html = await server.transformIndexHtml(`${base}/audit-check`, '<!doctype html><html><head></head><body></body></html>')
  browser = await chromium.launch()
  const context = await browser.newContext({ locale: 'en', timezoneId: 'UTC', colorScheme: 'light', viewport: { width: 1440, height: 1000 } }), page = await context.newPage(), errors = [], consoleProblems = []
  await context.clock.setFixedTime(new Date('2026-10-12T10:30:00Z'))
  page.on('pageerror', error => errors.push(error.message)); page.on('console', msg => { if (['error', 'warning'].includes(msg.type())) consoleProblems.push(msg.type()) })
  await context.route('**/*', route => {
    if (route.request().url() === fixtureUrl) return route.fulfill({ contentType: 'text/html', body: html })
    if (new URL(route.request().url()).origin === origin) return route.continue()
    errors.push('unexpected external request'); return route.abort()
  })
  await page.goto(fixtureUrl)
  const mounted = await page.evaluate(async base => (await import(`${base}/scripts/audit-fixture.mjs`)).checkAudit(), base)
  console.log(JSON.stringify({ mounted, errors, consoleProblems }, null, 2)); assert.deepEqual(mounted.problems, []); assert.deepEqual(errors, []); assert.deepEqual(consoleProblems, [])
  const open = async flags => page.goto(`${origin}${base}/audit?mock-server&mock-sign-in&period=custom&from=2026-10-01&to=2026-10-12${flags}`)
  for (const flags of ['', '&mock-security', '&mock-auditor']) {
    await open(flags); await page.locator('table.data-table').waitFor()
    assert.equal(await page.getByRole('columnheader', { name: 'From', exact: true }).count(), flags === '' ? 1 : 0)
    assert.equal(await page.locator('nav a[aria-current="page"]').getAttribute('href'), `${base}/audit`)
    if (!flags) assert.equal(await page.locator(`a[href="${base}/reports"]`).count(), 0)
    if (flags) await page.screenshot({ path: join(visual, `audit-${flags.slice(6)}-en-light.png`), fullPage: true })
  }
  await open('&actor=u_gone000000&action=person.*')
  await page.getByText('Nothing in this period', { exact: true }).waitFor()
  assert.equal(await page.getByRole('combobox', { name: 'Who', exact: true }).inputValue(), 'u_gone000000')
  assert.equal(await page.getByRole('combobox', { name: 'Action', exact: true }).inputValue(), 'person.*')
  await page.getByRole('combobox', { name: 'Who', exact: true }).selectOption('')
  await page.locator('table.data-table').waitFor(); assert.equal(await page.locator('tbody tr').count(), 2)
  await page.getByRole('combobox', { name: 'Action', exact: true }).selectOption(''); await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 10)
  const downloading = page.waitForEvent('download'); await page.getByRole('button', { name: 'Export JSON', exact: true }).click()
  const download = await downloading, stream = await download.createReadStream(), chunks = []
  for await (const chunk of stream) chunks.push(chunk)
  const exported = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  assert.deepEqual(Object.keys(exported), ['period', 'entries']); assert.equal(exported.entries.length, 10)
  assert.equal(exported.entries[0].id, 40); assert.equal(exported.entries.at(-1).id, 49)
  await open('&mock-code-session'); await page.locator('table.data-table').waitFor(); await page.getByRole('button', { name: 'Export CSV', exact: true }).click()
  await page.getByText('An export of the administration log takes a session opened with your company account.', { exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: 'Export CSV', exact: true }).count(), 0)
  await open('&mock-reports-paused=users'); await page.getByText('Reports are paused', { exact: true }).waitFor()
  assert.equal(await page.locator('table.data-table').count(), 0)
  for (const flags of ['&mock-group-lead', '&mock-server-key', '&mock-siem-only']) {
    await open(flags); await page.getByRole('heading', { name: "There's no page here", level: 2, exact: true }).waitFor()
    assert.equal(await page.locator('table.data-table').count(), 0); assert.equal(await page.locator(`nav a[href="${base}/audit"]`).count(), 0)
  }
  // Browser date labels can differ from the date-only server query, including across DST.
  const west = await browser.newContext({ locale: 'en', timezoneId: 'America/Los_Angeles' })
  await west.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
  await west.clock.setFixedTime(new Date('2026-10-12T00:30:00Z'))
  const westPage = await west.newPage()
  await westPage.goto(`${origin}${base}/audit?mock-server&mock-sign-in&period=custom&from=2026-03-08&to=2026-03-08`)
  await westPage.getByText('Nothing in this period', { exact: true }).waitFor()
  assert.equal(await westPage.getByLabel('From', { exact: true }).inputValue(), '2026-03-08')
  assert.equal(new URL(westPage.url()).searchParams.get('to'), '2026-03-08')
  await west.close(); assert.deepEqual(errors, []); assert.deepEqual(consoleProblems, [])
  console.log(JSON.stringify({ realRoutes: 9, currentFiltersExportsAndDates: 'passed' })); await context.close()
} finally { await browser?.close(); await server.close() }
