import { afterEach, expect, it, vi } from 'vitest'
import { httpTransport, setTransport } from '../../api/client'
import { reportsApi } from '../api'
import type { SiemView } from '../types'
import { ReportGate } from './reportSession'
import { SiemSession } from './siemSession'

afterEach(() => { vi.unstubAllGlobals(); setTransport(httpTransport) })
it('serializes only the captured etag and secret request field, then tests only a saved name', async () => {
  const view: SiemView = { etag: 'captured-etag', editable: true, paused: null, outputs: [{ name: 'remote', kind: 'webhook', url: 'https://collector.example/in', headers: {}, secret: null, format: 'json', has_secret: false, secret_destination_changed: false, queued: 0, dropped: 0, collapsed: 0, last_ok: null, failing_since: null, last_error: null, held: false, blocked: false }] }
  const requests: { url: string; init: RequestInit }[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    requests.push({ url, init })
    return new Response(JSON.stringify(url.endsWith('/test') ? { ok: true, detail: 'fixed' } : view), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  setTransport(httpTransport)
  const session = new SiemSession(reportsApi, true, () => undefined, new ReportGate())
  const tick = async () => { for (let i = 0; i < 35; i++) await Promise.resolve() }
  session.reload(); await tick(); session.edit('remote')
  session.change({ ...session.snapshot().draft!, secret: 'synthetic-wire-canary' }); session.save(); await tick()
  const write = requests.find(r => r.init.method === 'PUT')!
  expect(write.url).toBe('/_latet/api/v1/ee/siem')
  expect(write.init.credentials).toBe('same-origin')
  expect(write.init.cache).toBe('no-store')
  expect(write.init.headers).toEqual({ Accept: 'application/json', 'Content-Type': 'application/json', 'X-Latet-UI': '1' })
  expect(JSON.parse(String(write.init.body))).toEqual({ outputs: [{ name: 'remote', kind: 'webhook', url: 'https://collector.example/in', headers: {}, secret: 'siem-remote', format: 'json' }], secrets: { remote: 'synthetic-wire-canary' }, etag: 'captured-etag' })
  expect(requests.map(r => r.init.method)).toEqual(['GET', 'PUT', 'GET'])
  expect(JSON.stringify(session.snapshot())).not.toContain('synthetic-wire-canary')
  session.test('remote'); await tick()
  expect(requests.find(r => r.init.method === 'POST')).toMatchObject({ url: '/_latet/api/v1/ee/siem/test', init: { body: '{"name":"remote"}' } })
  session.dispose()
})
