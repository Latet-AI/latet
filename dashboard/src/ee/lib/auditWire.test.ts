import { afterEach, expect, it, vi } from 'vitest'
import { httpTransport, setTransport } from '../../api/client'
import { auditApi } from '../api'
afterEach(() => { vi.unstubAllGlobals(); setTransport(httpTransport) })
it('sends date-only filters verbatim and preserves server export bytes', async () => {
  const requests: { url: string; init: RequestInit }[] = []
  const file = { name: 'audit.csv', type: 'text/csv', file: '\uFEFFtime,actor\r\n,"\'=SUM(1,2)"\r\nZürich\r\n' }
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => { requests.push({ url, init }); return new Response(JSON.stringify(url.includes('/export') ? file : {}), { status: 200, headers: { 'Content-Type': 'application/json' } }) })
  setTransport(httpTransport)
  const q = { from: '2026-03-29', to: '2026-03-29', action: 'person_%\\*', actor: 'admin:3f9a' }
  await auditApi.log(q)
  expect(await auditApi.exportLog(q, 'csv')).toEqual(file)
  for (const request of requests) {
    const url = new URL(request.url, 'http://localhost')
    expect(Object.fromEntries(url.searchParams)).toEqual({ ...q, ...(url.pathname.endsWith('export') ? { format: 'csv' } : {}) })
    expect(request.init).toMatchObject({ method: 'GET', credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } })
  }
  expect(requests.map(r => r.url.split('?')[0])).toEqual(['/_latet/api/v1/ee/audit', '/_latet/api/v1/ee/audit/export'])
})
