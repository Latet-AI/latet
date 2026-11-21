import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Report, ReportFile, SiemView } from '../ee/types'

const today = new Date(2026, 9, 12)
const period = 'from=2026-10-01&to=2026-10-12'
async function loadedAs(flags = 'mock-security', space: string | null = null) {
  vi.stubGlobal('window', { location: { search: `?mock-server&mock-sign-in&${flags}` }, sessionStorage: { getItem: () => space } })
  vi.resetModules()
  const { routeReports } = await import('./reports')
  return (path = '/ee/reports', q = period, method = 'GET', body?: unknown) => routeReports(method, path, body, new URLSearchParams(q), today)
}
afterEach(() => { vi.unstubAllGlobals(); vi.resetModules() })
const failure = (call: () => unknown) => { try { call() } catch (error) { return error }; throw new Error('expected refusal') }

describe('report mock access and wire contracts', () => {
  it('admits security/auditor, scopes group leads and refuses administrators and key spaces', async () => {
    for (const role of ['mock-security', 'mock-auditor']) {
      const call = await loadedAs(role)
      expect(call()).toMatchObject({ viewer: { groups: null, siem: role === 'mock-security' ? 'edit' : 'read' } })
    }
    expect(failure(await loadedAs(''))).toMatchObject({ status: 403 })
    expect(failure(await loadedAs('mock-security', 'u_billingci2'))).toMatchObject({ status: 403 })
    expect(failure(await loadedAs('mock-security&mock-server-key'))).toMatchObject({ status: 403 })
    const lead = await loadedAs('mock-group-lead')
    const view = lead() as Report
    expect(view.viewer.groups).toEqual(['g_paymentsaa']); expect(view.by_key).toEqual([])
    expect(view.options.groups).toEqual([{ id: 'g_paymentsaa', name: 'Payments' }])
    expect(view.by_group.map(r => r.key)).toEqual(['g_paymentsaa'])
    expect(failure(() => lead('/ee/reports', `${period}&group=g_platformaa`))).toMatchObject({ status: 403 })
    expect(failure(() => lead('/ee/reports', `${period}&group=g_invalid`))).toMatchObject({ status: 400 })
  })
  it('refuses company-sign-in exports even though GET report reads are permitted', async () => {
    const call = await loadedAs('mock-security&mock-code-session')
    expect(call()).toHaveProperty('totals')
    expect(failure(() => call('/ee/reports/export', `${period}&format=csv`))).toMatchObject({ status: 403, body: { code: 'company-sign-in' } })
  })
  it.each([
    ['mock-license=paused', 'expired'], ['mock-reports-paused=users', 'users'],
    ['mock-license=trial-ended', 'trial-ended'], ['mock-license=invalid', 'unusable'],
  ])('refuses reads and exports while %s', async (flag, why) => {
    const call = await loadedAs(`mock-security&${flag}`)
    for (const path of ['/ee/reports', '/ee/reports/export']) expect(failure(() => call(path))).toMatchObject({ status: 403, body: { code: 'not-producing', paused: { why } } })
  })
  it('validates dates before allocating a series or exporting', async () => {
    const call = await loadedAs()
    for (const q of ['from=0000-01-01&to=9999-12-31', 'from=2026-02-30&to=2026-03-01', 'from=2024-01-01&to=2025-01-01']) {
      for (const path of ['/ee/reports', '/ee/reports/export']) expect(failure(() => call(path, q))).toMatchObject({ status: 400 })
    }
    expect(call('/ee/reports', 'from=2026-01-01&to=2026-04-02')).toMatchObject({ period: { days: 92, step: 'day' } })
    expect(call('/ee/reports', 'from=2026-01-01&to=2026-04-03')).toMatchObject({ period: { days: 93, step: 'week' } })
  })
  it('bounds ranked choices while preserving overflow totals and nullable people', async () => {
    const call = await loadedAs('mock-security&mock-reports-overflow')
    const view = call() as Report
    expect(view.by_project).toHaveLength(200); expect(view.options.projects).toHaveLength(200)
    expect(view.others.by_project.count).toBe(5)
    expect(view.by_project.reduce((n, row) => n + row.exchanges, 0) + view.others.by_project.exchanges).toBe(view.totals.exchanges)
    expect(view.by_person).toBeNull(); expect(view.others.by_person).toBeNull(); expect(view.others.by_approver).toBeNull()
    expect(view.options.groups.every(g => /^g_[a-z2-7]{10}$/.test(g.id))).toBe(true)
  })
  it('keeps formula-safe export fixtures out of ordinary project choices', async () => {
    const call = await loadedAs()
    const view = call() as Report
    expect(view.options.projects).not.toContain('=SUM(1,2)')
    const formula = await loadedAs('mock-security&mock-reports-formula')
    expect((formula() as Report).options.projects).toContain('=SUM(1,2)')
  })
  it('filters category requests as well as values, retaining the full offered type choices', async () => {
    const call = await loadedAs()
    const full = call() as Report
    const selected = call('/ee/reports', `${period}&category=EMAIL`) as Report
    expect(selected.totals.exchanges).toBeGreaterThan(0)
    expect(selected.totals.exchanges).toBeLessThan(full.totals.exchanges)
    expect(selected.by_category.map(row => row.key)).toEqual(['EMAIL'])
    expect(selected.options.categories).toEqual(full.options.categories)
    expect(selected.totals.approvals).toEqual(full.totals.approvals)
    expect(selected.totals.apps).toEqual(full.totals.apps)
    const file = JSON.parse((call('/ee/reports/export', `${period}&category=EMAIL&format=json`) as ReportFile).file)
    expect(file.rows.every((row: { masked: number }) => row.masked > 0)).toBe(true)
    expect(file.rows.reduce((n: number, row: { exchanges: number }) => n + row.exchanges, 0)).toBe(selected.totals.exchanges)
  })
  it('exports the server JSON envelope, daily row keys and administration visibility', async () => {
    const security = await loadedAs()
    const file = security('/ee/reports/export', `${period}&format=json`) as ReportFile
    const parsed = JSON.parse(file.file)
    expect(parsed).toMatchObject({ people: 'group-only', categories: expect.any(Array), filters: { group: null }, before: expect.any(Object) })
    expect(parsed.rows[0]).toMatchObject({ day: '2026-10-01', group: expect.any(String), kind: expect.any(String), name: expect.any(String), project: expect.any(String), agent: expect.any(String), by_category: expect.any(Object) })
    expect(parsed.rows[0]).not.toHaveProperty('key')
    expect(parsed.administration).toHaveLength(1); expect(parsed.administration[0].address).toBeNull()
    const lead = await loadedAs('mock-group-lead')
    expect(JSON.parse((lead('/ee/reports/export', `${period}&format=json`) as ReportFile).file)).not.toHaveProperty('administration')
    const cut = await loadedAs('mock-security&mock-reports-truncated')
    const cutFile = JSON.parse((cut('/ee/reports/export', `${period}&format=json`) as ReportFile).file)
    expect(cutFile.administration_truncated).toBe(true); expect(cutFile).not.toHaveProperty('administration')
  })
  it('exports BOM, CRLF, category columns and quoted formula-safe text as the server does', async () => {
    const call = await loadedAs('mock-security&mock-reports-formula')
    const file = call('/ee/reports/export', `${period}&format=csv&project=${encodeURIComponent('=SUM(1,2)')}`) as ReportFile
    expect(file.type).toBe('text/csv')
    expect(file.file).toMatch(/^\uFEFFday,group,kind,name,project,agent,exchanges,stopped,masked,/)
    expect(file.file).toContain('"\'=SUM(1,2)"')
    expect(file.file.endsWith('\r\n')).toBe(true)
  })
  it.each([413, 429])('represents a bounded %s refusal', async status => {
    const call = await loadedAs(`mock-security&mock-reports-error=${status}`)
    expect(failure(() => call('/ee/reports/export'))).toMatchObject({ status })
  })
})

describe('SIEM foundation for the following task', () => {
  it('preserves local outputs while editing another, refuses local tests, and permits removal', async () => {
    const call = await loadedAs('mock-security&mock-siem-local')
    const before = call('/ee/siem') as SiemView
    const settings = before.outputs.map(({ name, kind, url, headers, secret, format }) => ({ name, kind, url, headers, secret, format }))
    settings[1]!.format = 'ndjson'
    const after = call('/ee/siem', '', 'PUT', { outputs: settings, etag: before.etag }) as SiemView
    expect(after.outputs.find(o => o.name === 'local')).toEqual(before.outputs.find(o => o.name === 'local'))
    expect(failure(() => call('/ee/siem/test', '', 'POST', { name: 'local' }))).toMatchObject({ status: 403 })
    expect((call('/ee/siem', '', 'PUT', { outputs: settings.filter(o => o.name !== 'local'), etag: after.etag }) as SiemView).outputs.some(o => o.name === 'local')).toBe(false)
  })
  it('preserves normalized bindings but differentiates an empty path from a slash', async () => {
    const call = await loadedAs()
    const settings = { name: 'normalize', kind: 'webhook', url: 'https://host', headers: { Authorization: 'Bearer {secret}', 'X-Trace': 'old' }, secret: 'siem-normalize', format: 'json' }
    call('/ee/siem', '', 'PUT', { outputs: [settings], secrets: { normalize: 'synthetic-binding' } })
    const harmless = { ...settings, url: 'HTTPS://HOST...:443', headers: { 'X-Trace': 'new', authorization: 'Bearer {secret}' } }
    expect((call('/ee/siem', '', 'PUT', { outputs: [harmless] }) as SiemView).outputs[0]?.has_secret).toBe(true)
    expect((call('/ee/siem', '', 'PUT', { outputs: [{ ...harmless, url: harmless.url + '/' }] }) as SiemView).outputs[0]?.secret_destination_changed).toBe(true)
  })
  it.each(['mock-admin', 'mock-group-lead', 'mock-security&mock-server-key'])('refuses SIEM access for %s', async flags => {
    const call = await loadedAs(flags)
    expect(failure(() => call('/ee/siem'))).toMatchObject({ status: 403 })
  })
  it('reads SIEM alone without licensing Reports and distinguishes company-sign-in and bounded refusals', async () => {
    const only = await loadedAs('mock-security&mock-siem-only')
    expect((only('/ee/siem') as SiemView).editable).toBe(true)
    expect(failure(() => only('/ee/reports'))).toMatchObject({ status: 403 })
    const code = await loadedAs('mock-security&mock-code-session')
    expect(failure(() => code('/ee/siem/test', '', 'POST', { name: 'splunk' }))).toMatchObject({ status: 403, body: { code: 'company-sign-in' } })
    for (const status of [413, 429]) {
      const call = await loadedAs(`mock-security&mock-siem-error=${status}`)
      expect(failure(() => call('/ee/siem/test', '', 'POST', { name: 'splunk' }))).toMatchObject({ status })
    }
  })
  it.each(['settings', 'secret'])('assigns the first secret name through the %s endpoint without retaining its value', async endpoint => {
    const call = await loadedAs()
    const settings = { name: 'first', kind: 'webhook', url: 'https://collector.example.test/events', headers: { Authorization: 'Bearer {secret}' }, secret: null, format: 'json' }
    const value = 'synthetic-first-secret-only'
    call('/ee/siem', '', 'PUT', { outputs: [settings], ...(endpoint === 'settings' ? { secrets: { first: value } } : {}) })
    if (endpoint === 'secret') call('/ee/siem/secret', '', 'PUT', { name: 'first', secret: value })
    const view = call('/ee/siem') as SiemView
    expect(view.outputs[0]).toMatchObject({ secret: 'siem-first', has_secret: true, secret_destination_changed: false })
    expect(JSON.stringify(view)).not.toContain(value)
    const changed = call('/ee/siem', '', 'PUT', { outputs: [{ ...settings, secret: 'siem-first', headers: { Authorization: 'Token {secret}' } }] }) as SiemView
    expect(changed.outputs[0]).toMatchObject({ has_secret: false, secret_destination_changed: true })
    expect(JSON.stringify(changed)).not.toContain(value)
  })
  it('redacts reader destinations and headers, omits etags, and refuses writes', async () => {
    const call = await loadedAs('mock-auditor')
    const view = call('/ee/siem') as SiemView
    expect(view).toMatchObject({ etag: null, editable: false })
    expect(view.outputs[1]?.url).toBe('https://elastic.acme.com:9880/…')
    expect(view.outputs[1]?.headers.Authorization).toBe('…')
    expect(view.outputs.find(o => o.kind === 'syslog')?.url).toBe('tls://syslog.acme.com:6514')
    expect(failure(() => call('/ee/siem', '', 'PUT', { outputs: [] }))).toMatchObject({ status: 403 })
    expect(view.outputs.some(o => o.held && !o.blocked)).toBe(true)
  })
  it.each(['url', 'headers'] as const)('invalidates a secret when its bound %s changes until reset', async field => {
    const call = await loadedAs()
    const view = call('/ee/siem') as SiemView
    const settings = view.outputs.map(({ name, kind, url, headers, secret, format }) => ({ name, kind, url, headers, secret, format }))
    if (field === 'url') settings[0]!.url += '/changed'
    else settings[0]!.headers = { 'X-Stream': '{secret}' }
    const changed = call('/ee/siem', '', 'PUT', { outputs: settings, etag: view.etag }) as SiemView
    expect(changed.outputs[0]).toMatchObject({ has_secret: false, secret_destination_changed: true })
    call('/ee/siem/secret', '', 'PUT', { name: settings[0]!.name, secret: 'synthetic-fixture-only' })
    expect((call('/ee/siem') as SiemView).outputs[0]).toMatchObject({ has_secret: true, secret_destination_changed: false })
  })
})
