// Focused Reports checks in the existing Chromium. No daemon, external network or dependency downloads.
/* global window, document */
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
  const context = await browser.newContext({ locale: 'en', timezoneId: 'America/Los_Angeles', viewport: { width: 1440, height: 900 } })
  await context.clock.setFixedTime(new Date('2026-10-12T10:30:00Z'))
  const page = await context.newPage()
  const errors = [], downloads = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('download', value => downloads.push(value))
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === origin) return route.continue()
    errors.push('unexpected external request'); return route.abort()
  })
  const open = async (flags = '&mock-reports-people=named') => {
    await page.goto(`${origin}${base}/reports?mock-server&mock-sign-in&mock-security${flags}`)
    await page.getByRole('button', { name: 'Export CSV', exact: true }).waitFor()
  }
  const period = async days => {
    await page.getByRole('radiogroup', { name: 'Period', exact: true }).getByText(`Last ${days} days`, { exact: true }).click()
    // Router navigation changes the URL before React commits the controlled radio and query effect.
    await page.waitForFunction(value => {
      const radio = document.querySelector(`input[type="radio"][value="${value}"]`)
      return radio?.checked && (new URL(window.location.href).searchParams.get('period') ?? '30') === value
    }, String(days), { timeout: 10000 })
  }
  await open()
  await page.evaluate(async base => {
    const { reportsApi } = await import(`${base}/src/ee/api.ts`)
    const sample = await reportsApi.report({ from: '2026-10-01', to: '2026-10-12' })
    window.reportChecks = { reads: [], exports: [], sample }
    reportsApi.report = q => new Promise((resolve, reject) => window.reportChecks.reads.push({ q, resolve, reject }))
    reportsApi.exportReport = (q, format) => new Promise((resolve, reject) => window.reportChecks.exports.push({ q, format, resolve, reject }))
  }, base)
  await period(90)
  await page.waitForFunction(() => window.reportChecks.reads.length === 1)
  await period(7)
  await period(30)
  assert.equal(await page.getByRole('button', { name: 'Export CSV', exact: true }).count(), 0)
  assert.equal(await page.getByText('Emily Carter', { exact: true }).count(), 0)
  assert.equal(await page.evaluate(() => window.reportChecks.reads.length), 1)
  const settle = index => page.evaluate(index => {
    const { reads, sample } = window.reportChecks
    const read = reads[index]
    read.resolve({ ...sample, filters: { group: null, project: read.q.project ?? null, agent: null, category: null } })
  }, index)
  await settle(0)
  await page.waitForFunction(() => window.reportChecks.reads.length === 2)
  assert.equal(await page.evaluate(() => window.reportChecks.reads[1].q.from), '2026-09-13')
  await settle(1)
  await page.getByRole('button', { name: 'Export CSV', exact: true }).click()
  await page.waitForFunction(() => window.reportChecks.exports.length === 1)
  await period(7)
  assert.equal(await page.evaluate(() => window.reportChecks.reads.length), 2)
  await page.evaluate(() => window.reportChecks.exports[0].resolve({ name: 'stale.csv', type: 'text/csv', file: 'stale' }))
  await page.waitForFunction(() => window.reportChecks.reads.length === 3)
  await settle(2)
  await page.getByRole('button', { name: 'Export CSV', exact: true }).waitFor()
  assert.equal(downloads.length, 0)
  // A previously successful named report must disappear on a refused reload.
  await period(90)
  await page.waitForFunction(() => window.reportChecks.reads.length === 4)
  await page.evaluate(async base => {
    const { ApiError } = await import(`${base}/src/api/client.ts`)
    window.reportChecks.reads[3].reject(new ApiError(403, 'refused'))
  }, base)
  await page.getByText('You no longer have access to this report.', { exact: false }).waitFor()
  assert.equal(await page.getByText('Emily Carter', { exact: true }).count(), 0)
  assert.equal(await page.locator('select option').filter({ hasText: 'billing-api' }).count(), 0)
  assert.equal(await page.getByRole('button', { name: 'Export CSV', exact: true }).count(), 0)
  await page.getByRole('button', { name: 'Try again', exact: true }).click()
  await page.waitForFunction(() => window.reportChecks.reads.length === 5)
  await settle(4)
  await page.getByRole('button', { name: 'Export CSV', exact: true }).click()
  await page.waitForFunction(() => window.reportChecks.exports.length === 2)
  await page.getByRole('link', { name: 'Stats', exact: true }).click()
  await page.getByRole('heading', { name: 'Stats', exact: true }).waitFor()
  await page.evaluate(async () => {
    window.reportChecks.exports[1].resolve({ name: 'late.csv', type: 'text/csv', file: 'late' })
    await Promise.resolve()
  })
  assert.equal(downloads.length, 0)
  assert.equal(await page.locator('[data-toasts]').getByText(/Report saved/).count(), 0)
  console.log('PASS mounted gate, rapid filters, export ordering, refusal clearing, retry and unmount')

  await open('&mock-reports-truncated&mock-reports-formula')
  const csvReady = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export CSV', exact: true }).click()
  const csv = await csvReady
  const csvParts = []
  for await (const chunk of await csv.createReadStream()) csvParts.push(chunk)
  const csvBytes = Buffer.concat(csvParts)
  assert.deepEqual([...csvBytes.subarray(0, 3)], [239, 187, 191])
  assert.ok(csvBytes.toString('utf8').includes('"\'=SUM(1,2)"'))
  await page.getByRole('button', { name: 'Export JSON', exact: true }).waitFor()
  const jsonReady = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export JSON', exact: true }).click()
  const jsonParts = []
  for await (const chunk of await (await jsonReady).createReadStream()) jsonParts.push(chunk)
  const json = JSON.parse(Buffer.concat(jsonParts).toString('utf8'))
  assert.equal(json.administration_truncated, true)
  assert.ok(json.rows.every(row => row.day && !('key' in row)))
  assert.ok(!('administration' in json))
  console.log('PASS browser CSV bytes and JSON truncation export')

  await open('&mock-code-session')
  await page.getByRole('button', { name: 'Export CSV', exact: true }).click()
  await page.locator('a[href="/_latet/sso/login"]').waitFor()
  assert.equal(await page.getByRole('button', { name: 'Export CSV', exact: true }).count(), 0)
  await page.goto(`${origin}${base}/reports?mock-server&mock-sign-in&mock-security&mock-license=trial-ended`)
  await page.getByRole('heading', { name: 'Reports are paused', exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: 'Export CSV', exact: true }).count(), 0)
  await page.goto(`${origin}${base}/reports?mock-server&mock-sign-in&mock-security&period=custom&from=2026-02-30&to=2026-03-01`)
  await page.getByText('Enter real dates as YYYY-MM-DD, from 2000 through 2999.', { exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: 'Export CSV', exact: true }).count(), 0)
  console.log('PASS company sign-in, paused trial and invalid date presentation')

  // Existing screenshots use administrators, whose navigation must continue to exclude Reports.
  for (const path of ['license?mock-license=server&', 'people?', 'sign-in?']) {
    await page.goto(`${origin}${base}/${path}mock-server&mock-sign-in`)
    await page.getByRole('link', { name: 'People and groups', exact: true }).waitFor()
    assert.equal(await page.getByRole('link', { name: 'Reports', exact: true }).count(), 0)
  }
  assert.deepEqual(errors, [])
  console.log('PASS unchanged administrator navigation; no browser errors or external requests')
  await context.close()
} finally {
  await browser?.close()
  await server.close()
}
