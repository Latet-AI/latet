import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Me } from '../api/types'
import type { GoneTerm, TermSyncSources, TermSyncState } from '../ee/types'

// The mock reads its flags from the address once, when it loads: each case loads it again with the address it asks.
async function loadedAs(search: string) {
  vi.stubGlobal('window', {
    location: { search },
    sessionStorage: { getItem: () => null, setItem: () => undefined, removeItem: () => undefined },
  })
  vi.resetModules()
  const [ee, people, terms] = await Promise.all([import('./ee'), import('./people'), import('./ee-terms')])
  return { routeEe: ee.routeEe, withSecurity: people.withSecurity, withSignIn: people.withSignIn, routeTermSync: terms.routeTermSync }
}

/** What a route threw: its status and its body. */
function refusal(run: () => unknown): { status?: number; message?: string; body?: { code?: string; terms?: string; policy?: string } } | undefined {
  try {
    run()
  } catch (error) {
    return error as { status?: number; message?: string; body?: { code?: string } }
  }
  return undefined
}

const SECURITY = '?mock-server&mock-sign-in&mock-security'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('Term sync in the mock', () => {
  it("is the security role's page on a server: a computer has none of it (R31), nor the administrator", async () => {
    for (const search of ['', '?mock-security', '?mock-sign-in&mock-security', '?mock-server&mock-sign-in']) {
      const { routeEe } = await loadedAs(search)
      expect(routeEe('GET', '/ee/term-sync', undefined), search).toBeUndefined()
    }
    const edition = { name: 'enterprise', license: null, features: [], pages: [], roles: [], actions: [] }
    const me = { server: { url: 'https://latet.acme.lan', identity: null }, edition } as unknown as Me
    const security = await loadedAs(SECURITY)
    expect(security.withSecurity(security.withSignIn(me)).edition?.pages).toContain('term-sync')
    const admin = await loadedAs('?mock-server&mock-sign-in')
    expect(admin.withSecurity(admin.withSignIn(me)).edition?.pages).not.toContain('term-sync')
  })

  it('answers the state with four sources, one failed with its reason, and never a term', async () => {
    const { routeEe } = await loadedAs(SECURITY)
    const state = routeEe('GET', '/ee/term-sync', undefined) as TermSyncState
    expect(state).toMatchObject({ licensed: true, paused: false, most: 50000, unreadable: 0, behind: null, problems: [] })
    expect(state.sources.map((s) => [s.name, s.kind, s.last?.outcome])).toEqual([
      ['customers', 'file', 'ok'],
      ['colleagues', 'graph', 'ok'],
      ['teams', 'graph', 'ok'],
      ['directory', 'ldap', 'failed'],
    ])
    expect(state.sources.every((s) => s.asked === null && s.last?.kind === s.kind)).toBe(true)
    expect(state.sources[3]?.last).toMatchObject({ code: 'refused', reason: expect.stringContaining('refused the sign-in') })
    const terms = state.sources.reduce((sum, s) => sum + s.terms + s.gone, 0)
    expect(state.kept).toBe(terms)
    expect(state.gone).toBe(3)
    expect(JSON.stringify(state)).not.toContain('Halden') // a term is in the gone list alone
  })

  it('lists the three terms no longer in their source, the latest first, and removes the ones it is given', async () => {
    const { routeEe } = await loadedAs(SECURITY)
    const { items, total } = routeEe('GET', '/ee/term-sync/gone', undefined) as { items: GoneTerm[]; total: number }
    expect(total).toBe(3)
    expect(items.map((term) => term.value)).toEqual(['Halden Insurance', 'Grace Whitfield', 'Martin Hale'])
    expect(items.every((term) => /^[0-9a-f]{24}$/.test(term.id))).toBe(true) // the server's ids
    const [first, second] = items
    expect(routeEe('POST', '/ee/term-sync/remove', { ids: [first!.id, second!.id] })).toEqual({ removed: 2, by_type: { CLIENT: 1, PERSON: 1 } })
    expect(routeEe('GET', '/ee/term-sync/gone', undefined)).toMatchObject({ total: 1, items: [{ value: 'Martin Hale' }] })
    expect((routeEe('GET', '/ee/term-sync', undefined) as TermSyncState).gone).toBe(1)
    expect(refusal(() => routeEe('POST', '/ee/term-sync/remove', { all: true }))).toMatchObject({ status: 400 }) // a person's: ids alone
  })

  it('asks a sync, sets the source asked until the next read of the state, which gives its new run', async () => {
    const { routeEe } = await loadedAs(SECURITY)
    const before = routeEe('GET', '/ee/term-sync', undefined) as TermSyncState
    expect(routeEe('POST', '/ee/term-sync/run', { source: 'customers' })).toEqual({ asked: ['customers'] })
    const after = routeEe('GET', '/ee/term-sync', undefined) as TermSyncState
    const customers = after.sources.find((s) => s.name === 'customers')!
    expect(customers.asked).toBeNull()
    expect(customers.last).toMatchObject({ outcome: 'ok', kind: 'file', found: customers.terms })
    expect(customers.last!.started).toBeGreaterThan(before.sources[0]!.last!.started)
    expect(routeEe('POST', '/ee/term-sync/run', {})).toEqual({ asked: ['customers', 'colleagues', 'teams', 'directory'] })
    expect(refusal(() => routeEe('POST', '/ee/term-sync/run', { source: 'nobody' }))).toMatchObject({ status: 404 })
  })

  it('refuses a sync while the license pauses term sync (M3), and every change once a trial ended', async () => {
    const paused = await loadedAs(`${SECURITY}&mock-license=paused`)
    expect((paused.routeEe('GET', '/ee/term-sync', undefined) as TermSyncState).paused).toBe(true)
    expect(refusal(() => paused.routeEe('POST', '/ee/term-sync/run', {}))).toMatchObject({ status: 403, body: { code: 'not-manageable' } })
    const ended = await loadedAs(`${SECURITY}&mock-license=trial-ended`)
    expect(ended.routeEe('GET', '/ee/term-sync/gone', undefined)).toMatchObject({ total: 3 }) // read in every standing
    const { etag } = ended.routeEe('GET', '/ee/term-sync/sources', undefined) as TermSyncSources
    for (const [method, path, body] of [
      ['POST', '/ee/term-sync/run', {}],
      ['POST', '/ee/term-sync/remove', { ids: [] }],
      ['PUT', '/ee/term-sync/sources', { text: 'sources: []\n', etag }],
      ['PUT', '/ee/term-sync/secrets/colleagues', { value: 'x', etag }],
      ['DELETE', '/ee/term-sync/secrets/colleagues', { etag }],
    ] as const) {
      expect(refusal(() => ended.routeEe(method, path, body)), path).toMatchObject({ status: 403, body: { code: 'trial-ended' } })
    }
  })

  it("shows the terms and the sources to a session the company's sign-in opened alone, and changes nothing from another", async () => {
    const { routeEe } = await loadedAs(`${SECURITY}&mock-code-session`)
    expect(routeEe('GET', '/ee/term-sync', undefined)).toMatchObject({ licensed: true }) // never a term: answered
    for (const path of ['/ee/term-sync/gone', '/ee/term-sync/sources']) {
      expect(refusal(() => routeEe('GET', path, undefined)), path).toMatchObject({ status: 403, body: { code: 'company-sign-in' } })
    }
    for (const [method, path] of [['POST', '/ee/term-sync/run'], ['POST', '/ee/term-sync/remove'], ['PUT', '/ee/term-sync/sources'], ['PUT', '/ee/term-sync/secrets/directory']] as const) {
      expect(refusal(() => routeEe(method, path, {})), path).toMatchObject({ status: 403, body: { code: 'company-sign-in' } })
    }
  })

  it('answers the sources as JSON and YAML with their etag, never a secret, and keeps a change made from that etag', async () => {
    const { routeEe } = await loadedAs(SECURITY)
    const read = routeEe('GET', '/ee/term-sync/sources', undefined) as TermSyncSources
    expect(read.sources.map((s) => [s.name, s.kind])).toEqual([['customers', 'file'], ['colleagues', 'graph'], ['teams', 'graph'], ['directory', 'ldap']])
    expect(read.text).toMatch(/^sources:\n/)
    expect(read.etag).toMatch(/^[0-9a-f]{32}$/)
    // Where each connects, as the server says it (the final review's M1): the secret dialog says it before it asks.
    expect(read.where).toEqual({
      customers: { addresses: [], bind: null },
      colleagues: { addresses: ['https://graph.microsoft.com:443', 'https://login.microsoftonline.com:443'], bind: null },
      teams: { addresses: ['https://graph.microsoft.com:443', 'https://login.microsoftonline.com:443'], bind: null },
      directory: { addresses: ['ldaps://dc1.acme.lan:636'], bind: 'CN=latet-sync,OU=Service,DC=acme,DC=lan' },
    })
    const added = `${read.text}- name: projects\n  kind: url\n  url: https://wiki.acme.com/projects.json\n  headers:\n    Authorization: Bearer {secret}\n  mapping:\n    codename: PROJECT\n`
    // A change names the etag of the sources it was made from: none is 400, another is 409 sources-changed.
    expect(refusal(() => routeEe('PUT', '/ee/term-sync/sources', { text: added }))).toMatchObject({ status: 400 })
    expect(refusal(() => routeEe('PUT', '/ee/term-sync/sources', { text: added, etag: '0'.repeat(32) }))).toMatchObject({ status: 409, body: { code: 'sources-changed' } })
    const saved = routeEe('PUT', '/ee/term-sync/sources', { text: added, etag: read.etag }) as { added: string[]; removed: string[]; changed: string[]; problems: string[]; etag: string }
    expect(saved).toMatchObject({ added: ['projects'], removed: [], changed: [], problems: [] })
    expect(saved.etag).not.toBe(read.etag)
    const now = routeEe('GET', '/ee/term-sync/sources', undefined) as TermSyncSources
    expect(now).toMatchObject({ etag: saved.etag, text: added })
    expect(now.sources.at(-1)).toMatchObject({ name: 'projects', kind: 'url', headers: { Authorization: 'Bearer {secret}' } })
    const state = routeEe('GET', '/ee/term-sync', undefined) as TermSyncState
    expect(state.sources.at(-1)).toMatchObject({ name: 'projects', kind: 'url', configured: true, last: null })
    expect(refusal(() => routeEe('PUT', '/ee/term-sync/sources', { text: added, etag: read.etag }))).toMatchObject({ status: 409, body: { code: 'sources-changed' } })
    // Taken out: the source stays listed while the server keeps its terms, no longer set up.
    const without = routeEe('PUT', '/ee/term-sync/sources', { text: added.replace(/- name: teams[\s\S]*?(?=- name: directory)/, ''), etag: saved.etag })
    expect(without).toMatchObject({ removed: ['teams'] })
    const teams = (routeEe('GET', '/ee/term-sync', undefined) as TermSyncState).sources.find((s) => s.name === 'teams')
    expect(teams).toMatchObject({ configured: false, kind: null, every_minutes: null, last: null }) // its last run forgotten
    // Changed: its last run, of the source it was, forgotten too (the server's next look runs it as it is now).
    const read2 = routeEe('GET', '/ee/term-sync/sources', undefined) as TermSyncSources
    const every = routeEe('PUT', '/ee/term-sync/sources', { text: read2.text.replace('every_minutes: 360', 'every_minutes: 720'), etag: read2.etag })
    expect(every).toMatchObject({ changed: ['colleagues'] })
    const colleagues = (routeEe('GET', '/ee/term-sync', undefined) as TermSyncState).sources.find((s) => s.name === 'colleagues')
    expect(colleagues).toMatchObject({ configured: true, last: null })
  })

  it("refuses a source at the server's own computer, and its secret: latet server terms sets them (403 by-command)", async () => {
    const { routeEe } = await loadedAs(SECURITY)
    const { text, etag } = routeEe('GET', '/ee/term-sync/sources', undefined) as TermSyncSources
    for (const host of ['localhost', '127.0.0.1', '[::1]', 'LocalHost.']) {
      const local = `${text}- name: staff\n  kind: ldap\n  url: ldap://${host}:389\n  start_tls: false\n  base: OU=People,DC=acme,DC=lan\n  mapping:\n    cn: PERSON\n`
      expect(refusal(() => routeEe('PUT', '/ee/term-sync/sources', { text: local, etag })), host).toMatchObject({ status: 403, body: { code: 'by-command' } })
    }
    // A YAML text it cannot read: where, never what.
    const broken = refusal(() => routeEe('PUT', '/ee/term-sync/sources', { text: `${text}  exclude: [Sales\n`, etag }))
    expect(broken).toMatchObject({ status: 400, message: expect.stringMatching(/^not valid YAML \(line \d+, column \d+\)$/) })
    // One set by the command (&mock-terms-local): kept as it is by a save from the page, its secret refused.
    const command = await loadedAs(`${SECURITY}&mock-terms-local`)
    const read = command.routeEe('GET', '/ee/term-sync/sources', undefined) as TermSyncSources
    expect(read.sources.at(-1)).toMatchObject({ name: 'staff', url: 'ldap://localhost:389' })
    expect(refusal(() => command.routeEe('PUT', '/ee/term-sync/secrets/staff', { value: 'p4ss', etag: read.etag }))).toMatchObject({ status: 403, body: { code: 'by-command' } })
    expect(refusal(() => command.routeEe('DELETE', '/ee/term-sync/secrets/staff', { etag: read.etag }))).toMatchObject({ status: 403, body: { code: 'by-command' } })
    // Taken out from the page, as changed: refused, nothing changed (the command takes it out).
    const without = read.text.replace(/- name: staff[\s\S]*$/, '')
    expect(refusal(() => command.routeEe('PUT', '/ee/term-sync/sources', { text: without, etag: read.etag }))).toMatchObject({ status: 403, body: { code: 'by-command' } })
    // Kept under its name, it is neither moved elsewhere nor made a source of another kind (the final review's I1).
    const moved = read.text.replace('ldap://localhost:389', 'ldaps://dc9.acme.lan')
    expect(moved).not.toBe(read.text)
    const file = `${without}- name: staff\n  kind: file\n  path: staff.csv\n  mapping:\n    cn: PERSON\n`
    for (const text of [moved, file]) {
      expect(refusal(() => command.routeEe('PUT', '/ee/term-sync/sources', { text, etag: read.etag }))).toMatchObject({ status: 403, body: { code: 'by-command' } })
    }
    expect(command.routeEe('PUT', '/ee/term-sync/sources', { text: read.text, etag: read.etag })).toMatchObject({ added: [], removed: [], changed: [] })
  })

  it("keeps a source's secret with no value in any answer, asks its sync, and removes it", async () => {
    const { routeEe } = await loadedAs(SECURITY)
    const { etag } = routeEe('GET', '/ee/term-sync/sources', undefined) as TermSyncSources
    const value = 'n3w-d1rectory-p4ssword'
    expect(refusal(() => routeEe('PUT', '/ee/term-sync/secrets/directory', { value }))).toMatchObject({ status: 400 }) // no etag
    expect(refusal(() => routeEe('PUT', '/ee/term-sync/secrets/directory', { value, etag: 'f'.repeat(32) }))).toMatchObject({ status: 409, body: { code: 'sources-changed' } })
    expect(refusal(() => routeEe('PUT', '/ee/term-sync/secrets/customers', { value, etag }))).toMatchObject({ status: 409 }) // a file reads with none
    expect(refusal(() => routeEe('PUT', '/ee/term-sync/secrets/nobody', { value, etag }))).toMatchObject({ status: 404 })
    expect(refusal(() => routeEe('PUT', '/ee/term-sync/secrets/directory', { value: '  ', etag }))).toMatchObject({ status: 400 })
    const saved = routeEe('PUT', '/ee/term-sync/secrets/directory', { value, etag })
    expect(saved).toEqual({ saved: 'directory' })
    // The sync it asks for runs: the directory, refused before, signs in with the new secret.
    const state = routeEe('GET', '/ee/term-sync', undefined) as TermSyncState
    expect(state.sources.find((s) => s.name === 'directory')).toMatchObject({ asked: null, last: { outcome: 'ok' } })
    const sources = routeEe('GET', '/ee/term-sync/sources', undefined) as TermSyncSources
    expect(JSON.stringify([saved, state, sources])).not.toContain(value)
    expect(routeEe('DELETE', '/ee/term-sync/secrets/directory', { etag })).toEqual({ removed: 'directory' })
    expect(refusal(() => routeEe('DELETE', '/ee/term-sync/secrets/directory', { etag }))).toMatchObject({ status: 404 }) // none kept now
    expect(refusal(() => routeEe('DELETE', '/ee/term-sync/secrets/colleagues', {}))).toMatchObject({ status: 400 }) // no etag
  })

  it('answers undefined for any other path', async () => {
    const { routeTermSync } = await loadedAs(SECURITY)
    expect(routeTermSync('GET', '/ee/term-sync/nothing', undefined)).toBeUndefined()
    expect(routeTermSync('DELETE', '/ee/term-sync', undefined)).toBeUndefined()
  })

  it("asks again before a removal of the license stops the synced terms (&mock-synced-terms): 409 terms stop", async () => {
    const { routeEe } = await loadedAs('?mock-server&mock-sign-in&mock-license=server&mock-synced-terms')
    expect(refusal(() => routeEe('DELETE', '/ee/license', undefined))).toMatchObject({ status: 409, body: { terms: 'stop' } })
    expect(routeEe('DELETE', '/ee/license', { policy_stops: true })).toMatchObject({ state: 'none', removed: true })
    const both = await loadedAs('?mock-server&mock-sign-in&mock-license=server&mock-synced-terms&mock-policy')
    expect(refusal(() => both.routeEe('DELETE', '/ee/license', undefined))).toMatchObject({ status: 409, body: { terms: 'stop', policy: 'stops' } })
  })
})
