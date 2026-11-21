import { afterEach, expect, it, vi } from 'vitest'
import { httpTransport, setTransport } from '../../api/client'
import { auditApi, reportsApi } from '../api'
afterEach(() => { vi.unstubAllGlobals(); setTransport(httpTransport) })
// L1 of the whole-branch review: an export is a GET that records itself and holds the instance's one export, so the
// server refuses it without the header a page of another site cannot send, as it refuses a change.
it('asks for both exports with the header a page of another site cannot send', async () => {
  const requests: { url: string; init: RequestInit }[] = []
  const file = { name: 'export.csv', type: 'text/csv', file: '' }
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => { requests.push({ url, init }); return new Response(JSON.stringify(file), { status: 200, headers: { 'Content-Type': 'application/json' } }) })
  setTransport(httpTransport)
  const period = { from: '2026-03-01', to: '2026-03-31' }
  expect(await reportsApi.exportReport(period, 'csv')).toEqual(file)
  expect(await auditApi.exportLog(period, 'json')).toEqual(file)
  expect(requests.map(r => [r.url.split('?')[0], r.init.method, (r.init.headers as Record<string, string>)['X-Latet-UI']])).toEqual([
    ['/_latet/api/v1/ee/reports/export', 'GET', '1'],
    ['/_latet/api/v1/ee/audit/export', 'GET', '1'],
  ])
})
