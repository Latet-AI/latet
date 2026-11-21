import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Me } from '../api/types'
import type { CompanyQueue, Decided } from '../ee/types'

// The mock reads its flags from the address once, when it loads: each case loads it again with the address it asks.
async function loadedAs(search: string) {
  vi.stubGlobal('window', {
    location: { search },
    sessionStorage: { getItem: () => null, setItem: () => undefined, removeItem: () => undefined },
  })
  vi.resetModules()
  const [ee, people] = await Promise.all([import('./ee'), import('./people')])
  return { routeEe: ee.routeEe, withSecurity: people.withSecurity, withSignIn: people.withSignIn }
}

/** What a route threw: its status and its body's code. */
function refusal(run: () => unknown): { status?: number; body?: { code?: string } } | undefined {
  try {
    run()
  } catch (error) {
    return error as { status?: number; body?: { code?: string } }
  }
  return undefined
}

const SECURITY = '?mock-server&mock-sign-in&mock-security'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('Company approvals in the mock', () => {
  it("is the security role's page on a server, and an administrator's: a computer has none of it (R31)", async () => {
    for (const search of ['', '?mock-security', '?mock-sign-in']) {
      const { routeEe } = await loadedAs(search)
      expect(routeEe('GET', '/ee/approvals', undefined), search).toBeUndefined()
    }
    const edition = { name: 'enterprise', license: null, features: [], pages: [], roles: [], actions: [] }
    const me = { server: { url: 'https://latet.acme.lan', identity: null }, edition } as unknown as Me
    const { withSecurity, withSignIn } = await loadedAs(SECURITY)
    expect(withSecurity(withSignIn(me)).edition?.pages).toContain('company-approvals')
    const admin = await loadedAs('?mock-server&mock-sign-in')
    expect(admin.withSecurity(admin.withSignIn(me)).edition?.pages).toContain('company-approvals')
  })

  it("answers the administrator with nothing waiting for her, and her own requests decided lately", async () => {
    const { routeEe } = await loadedAs('?mock-server&mock-sign-in')
    expect(routeEe('GET', '/ee/approvals', undefined)).toEqual({ changes: [], batches: [], more: 0 })
    expect(routeEe('GET', '/ee/approvals/count', undefined)).toEqual({ waiting: 0 }) // the menu shows no number
    const { items } = routeEe('GET', '/ee/approvals/history', undefined) as { items: Decided[] }
    expect(items.length).toBeGreaterThan(0)
    expect(items.every((row) => row.identity.name === 'Emily Carter')).toBe(true)
    expect(refusal(() => routeEe('POST', '/ee/approvals/changes/cc_mock1', { action: 'approve' }))).toMatchObject({ status: 409, body: { code: 'gone' } })
  })

  it('lists what waits with no text in it, counts what Daniel decides on, and opens a text, values included', async () => {
    const { routeEe } = await loadedAs(`${SECURITY}&mock-own-change`)
    const queue = routeEe('GET', '/ee/approvals', undefined) as CompanyQueue
    expect(queue.changes.map((c) => [c.identity.name, c.mine])).toEqual([
      ['Emily Carter', false],
      ['Daniel Okafor', true],
    ])
    expect(queue.batches).toHaveLength(1)
    expect(JSON.stringify(queue)).not.toContain('laura.bennett') // the queue holds no text (R14)
    expect(routeEe('GET', '/ee/approvals/count', undefined)).toEqual({ waiting: 2 }) // his own left out
    const text = routeEe('GET', `/ee/approvals/batches/${queue.batches[0]?.id}`, undefined) as { items: { original: string }[] }
    expect(text.items[0]?.original).toContain('laura.bennett@whitfield.co.uk')
  })

  it('records who decided, ends what was decided, and answers 409 gone to a second decision', async () => {
    const { routeEe } = await loadedAs(SECURITY)
    expect(routeEe('POST', '/ee/approvals/changes/cc_mock1', { action: 'approve' })).toEqual({ ok: true, state: 'approved' })
    expect((routeEe('GET', '/ee/approvals', undefined) as CompanyQueue).changes).toEqual([])
    const { items } = routeEe('GET', '/ee/approvals/history', undefined) as { items: Decided[] }
    expect(items[0]).toMatchObject({ id: 'cc_mock1', state: 'approved', by: { name: 'Daniel Okafor' } })
    expect(refusal(() => routeEe('POST', '/ee/approvals/changes/cc_mock1', { action: 'reject' }))).toMatchObject({ status: 409, body: { code: 'gone' } })
    expect(refusal(() => routeEe('GET', '/ee/approvals/batches/cb_gone', undefined))).toMatchObject({ status: 409, body: { code: 'gone' } })
  })

  it("lets Daniel withdraw his own request, never approve it: nobody decided it", async () => {
    const { routeEe } = await loadedAs(`${SECURITY}&mock-own-change`)
    expect(refusal(() => routeEe('POST', '/ee/approvals/changes/cc_mock2', { action: 'approve' }))).toMatchObject({ status: 403, body: { code: 'yours' } })
    expect(routeEe('POST', '/ee/approvals/changes/cc_mock2', { action: 'reject' })).toEqual({ ok: true, state: 'rejected' })
    const { items } = routeEe('GET', '/ee/approvals/history', undefined) as { items: Decided[] }
    expect(items[0]).toMatchObject({ id: 'cc_mock2', state: 'withdrawn', by: null })
  })

  it("refuses a text, and every decision, to a session of latet dashboard's code: the company's sign-in [D6]", async () => {
    const { routeEe } = await loadedAs(`${SECURITY}&mock-code-session`)
    expect(routeEe('GET', '/ee/approvals', undefined)).toMatchObject({ more: 0 }) // the lists answer
    for (const run of [
      () => routeEe('GET', '/ee/approvals/batches/cb_mock1', undefined),
      () => routeEe('POST', '/ee/approvals/batches/cb_mock1', { action: 'reject' }),
      () => routeEe('POST', '/ee/approvals/changes/cc_mock1', { action: 'reject' }),
    ]) {
      expect(refusal(run)).toMatchObject({ status: 403, body: { code: 'company-sign-in' } })
    }
  })

  it('approves a text only as it was read: one masked again since answers 409 changed, and an approval names a version', async () => {
    const { routeEe } = await loadedAs(`${SECURITY}&mock-masked-again`)
    const read = routeEe('GET', '/ee/approvals/batches/cb_mock1', undefined) as { version: string; items: { masked: string }[] }
    expect(refusal(() => routeEe('POST', '/ee/approvals/batches/cb_mock1', { action: 'approve' }))).toMatchObject({ status: 400 })
    // &mock-masked-again: the text is masked again once it was first read (here leaving the address in clear).
    const now = routeEe('GET', '/ee/approvals/batches/cb_mock1', undefined) as { version: string; items: { masked: string }[] }
    expect(now.version).not.toBe(read.version)
    expect(now.items[0]?.masked).toContain('laura.bennett@whitfield.co.uk')
    const asRead = { action: 'approve', version: read.version }
    expect(refusal(() => routeEe('POST', '/ee/approvals/batches/cb_mock1', asRead))).toMatchObject({ status: 409, body: { code: 'changed' } })
    expect(routeEe('POST', '/ee/approvals/batches/cb_mock1', { action: 'approve', version: now.version })).toEqual({ ok: true, state: 'approved' })
  })

  it('decides nothing and opens no text once a trial ended, and still lists what waits and what was decided', async () => {
    const { routeEe } = await loadedAs(`${SECURITY}&mock-license=trial-ended`)
    expect((routeEe('GET', '/ee/approvals', undefined) as CompanyQueue).changes).toHaveLength(1)
    expect(routeEe('GET', '/ee/approvals/history', undefined)).toMatchObject({ items: expect.any(Array) })
    for (const run of [
      () => routeEe('GET', '/ee/approvals/batches/cb_mock1', undefined),
      () => routeEe('POST', '/ee/approvals/changes/cc_mock1', { action: 'approve' }),
    ]) {
      expect(refusal(run)).toMatchObject({ status: 403, body: { code: 'trial-ended' } })
    }
  })

  // The final security review's L1: with ?mock-routed the security team decides, so a removal of the license, which
  // lists approvals, asks first, as the server does (409 `approvals: "stop"`), and is made once confirmed.
  it('asks again before a removal of the license stops central approvals (?mock-routed): 409 approvals stop', async () => {
    const { routeEe } = await loadedAs('?mock-server&mock-sign-in&mock-license=server&mock-routed')
    const asked = refusal(() => routeEe('DELETE', '/ee/license', undefined)) as { status?: number; message?: string; body?: Record<string, unknown> }
    expect(asked).toMatchObject({ status: 409, body: { approvals: 'stop' } })
    expect(asked.body).not.toHaveProperty('policy')
    expect(asked.message).toContain('central approvals stop')
    expect(routeEe('DELETE', '/ee/license', { policy_stops: true })).toMatchObject({ state: 'none', removed: true })
    const unrouted = await loadedAs('?mock-server&mock-sign-in&mock-license=server')
    expect(unrouted.routeEe('DELETE', '/ee/license', undefined)).toMatchObject({ state: 'none', removed: true })
  })
})
