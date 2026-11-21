import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AuditLogView, ReportFile } from '../ee/types'
const today = new Date('2026-10-12T10:30:00Z'), period = 'from=2026-10-01&to=2026-10-12'
async function loadedAs(flags = '', space: string | null = null, server = true) {
  vi.stubGlobal('window', { location: { search: `?${server ? 'mock-server&' : ''}mock-sign-in&${flags}` }, sessionStorage: { getItem: () => space } }); vi.resetModules()
  const { routeReports } = await import('./reports')
  return (path = '/ee/audit', q = period) => routeReports('GET', path, undefined, new URLSearchParams(q), today)
}
afterEach(() => { vi.unstubAllGlobals(); vi.resetModules() })
const failure = (call: () => unknown) => { try { call() } catch (error) { return error }; throw new Error('expected refusal') }
describe('audit mock follows server roles, filters and export wire format', () => {
  it('names readers and targets for all authorized roles, giving addresses to administrators alone', async () => {
    for (const role of ['', 'mock-security', 'mock-auditor']) {
      const call = await loadedAs(role), value = call() as AuditLogView
      expect(value.addresses).toBe(role === '')
      expect(value.entries.some(e => e.actor.name === 'Emily Carter')).toBe(true)
      expect(value.entries.some(e => e.target?.name === 'Laura Chen')).toBe(true)
      expect(value.entries.every(e => role === '' ? e.address !== undefined : e.address === null)).toBe(true)
    }
    for (const role of ['mock-group-lead', 'mock-siem-only', 'mock-server-key']) expect(failure(await loadedAs(role))).toMatchObject({ status: 403 })
    expect(failure(await loadedAs('', 'u_billingci2'))).toMatchObject({ status: 403 })
    expect((await loadedAs('', null, false))()).toBeUndefined()
  })
  it('applies period, literal action/prefix and actor filters before deriving choices', async () => {
    const call = await loadedAs()
    const filtered = call('/ee/audit', `${period}&action=person.*&actor=scim%3As_7hq2m4pzxa`) as AuditLogView
    expect(filtered.entries).toHaveLength(1); expect(filtered.actions).toEqual(['person.added']); expect(filtered.actors.map(a => a.id)).toEqual(['scim:s_7hq2m4pzxa'])
    for (const action of ['person.%', 'person._', 'person.\\*', 'person.*x']) expect((call('/ee/audit', `${period}&action=${encodeURIComponent(action)}`) as AuditLogView).entries).toEqual([])
    expect((call('/ee/audit', 'from=2026-01-01&to=2026-01-02') as AuditLogView).entries).toEqual([])
    expect(failure(() => call('/ee/audit', `${period}&actor=${'x'.repeat(201)}`))).toMatchObject({ status: 400 })
    expect(failure(() => call('/ee/audit', 'from=2026-02-30&to=2026-03-01'))).toMatchObject({ status: 400 })
  })
  it('anchors an omitted start date to the supplied end date as the server does', async () => {
    const call = await loadedAs()
    expect((call('/ee/audit', 'to=2026-10-01') as AuditLogView).period).toEqual({ from: '2026-09-02', to: '2026-10-01' })
    expect(JSON.parse((call('/ee/audit/export', 'to=2026-10-01&format=json') as ReportFile).file).period).toEqual({ from: '2026-09-02', to: '2026-10-01' })
  })
  it('returns the newest 5000 matches while exporting the whole filtered period oldest first', async () => {
    const call = await loadedAs('mock-audit-count=5001')
    const value = call() as AuditLogView
    expect(value.entries).toHaveLength(5000); expect(value.truncated).toBe(true)
    const exported = JSON.parse((call('/ee/audit/export', `${period}&format=json`) as ReportFile).file)
    expect(Object.keys(exported)).toEqual(['period', 'entries']); expect(exported.period).toEqual({ from: '2026-10-01', to: '2026-10-12' }); expect(exported.entries).toHaveLength(5001)
    expect(exported.entries[0].id).toBeLessThan(Math.min(...value.entries.map(e => e.id)))
    expect(exported.entries.every((e: { id: number }, i: number) => i === 0 || e.id > exported.entries[i - 1].id)).toBe(true)
    const tooLarge = await loadedAs('mock-audit-count=100001')
    expect(failure(() => tooLarge('/ee/audit/export', `${period}&format=json`))).toMatchObject({ status: 413 })
  })
  it('emits exact eight-column BOM/CRLF CSV and protects quoted formula-like names', async () => {
    const call = await loadedAs('mock-audit-formula')
    const file = call('/ee/audit/export', `${period}&format=csv&actor=scim%3As_7hq2m4pzxa`) as ReportFile
    expect(file.name).toBe('latet-audit-2026-10-01-2026-10-12.csv'); expect(file.type).toBe('text/csv')
    expect(file.file).toBe('\uFEFFtime,actor,actor_name,action,target,target_name,details,address\r\n2026-10-09T02:30:00Z,scim:s_7hq2m4pzxa,,person.added,u_priyashahx,"\'=SUM(1,2)",,10.0.0.9\r\n')
  })
  it('preserves event-only skip metadata without making verification claims', async () => {
    const call = await loadedAs(); const value = call() as AuditLogView
    expect(value.entries.find(e => e.action === 'siem.skipped')).toMatchObject({ target: { id: 'siem', name: null }, details: { output: 'elastic', from: null, to: null, count: 0, dropped: 4 } })
    expect(value.entries.find(e => e.action === 'siem.gap')).toMatchObject({ target: { id: 'elastic', name: null }, details: { output: 'elastic', from: 20, to: 22, count: 3 } })
    expect(value.entries.some(e => e.actor.kind === 'key')).toBe(true)
    expect(value.entries.some(e => e.actor.kind === 'unknown')).toBe(true)
  })
  it('allows code-session reads but refuses their exports, paused production and invalid formats', async () => {
    const call = await loadedAs('mock-code-session'); expect(call()).toHaveProperty('entries')
    expect(failure(() => call('/ee/audit/export', `${period}&format=csv`))).toMatchObject({ status: 403, body: { code: 'company-sign-in' } })
    const paused = await loadedAs('mock-reports-paused=users')
    expect(failure(paused)).toMatchObject({ status: 403, body: { code: 'not-producing', paused: { why: 'users' } } })
    const active = await loadedAs(); expect(failure(() => active('/ee/audit/export', `${period}&format=xml`))).toMatchObject({ status: 400 })
  })
})
