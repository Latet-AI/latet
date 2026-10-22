import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, httpTransport, setTransport, type Method, type Transport } from './client'

/** A transport that answers from `answers` (by method and path) and keeps every call, in order. */
function recording(answers: Record<string, unknown>): { calls: { method: Method; path: string; body: unknown }[] } {
  const calls: { method: Method; path: string; body: unknown }[] = []
  const transport: Transport = {
    mock: true,
    async request(method, path, body) {
      calls.push({ method, path, body })
      return answers[`${method} ${path}`] ?? { ok: true, rules_version: 2 }
    },
    subscribe: () => () => undefined,
    subscribeApprovals: () => () => undefined,
    health: async () => ({}),
  }
  setTransport(transport)
  return { calls }
}

afterEach(() => {
  setTransport(httpTransport)
  vi.unstubAllGlobals()
})

describe('a save of your settings', () => {
  it('sends the etag of the settings the page was made from', async () => {
    const { calls } = recording({})
    await api.saveConfig({ profile: 'max' }, 'etag-read')
    await api.savePersonal('p1', { terms: [] }, 'etag-read')
    expect(calls).toEqual([
      { method: 'PUT', path: '/config', body: { profile: 'max', etag: 'etag-read' } },
      { method: 'PUT', path: '/projects/p1/personal', body: { terms: [], etag: 'etag-read' } },
    ])
  })

  it('changes the settings as they are now, with the etag of that read', async () => {
    const { calls } = recording({ 'GET /config': { etag: 'now', fields: [{ name: 'notes' }] } })
    await api.changeConfig((config) => ({ fields: [...config.fields, { name: 'email' }] }))
    expect(calls).toEqual([
      { method: 'GET', path: '/config', body: undefined },
      { method: 'PUT', path: '/config', body: { fields: [{ name: 'notes' }, { name: 'email' }], etag: 'now' } },
    ])
  })

  it('reads the etag before a project, so that a change between the two reads refuses the save', async () => {
    const { calls } = recording({
      'GET /config': { etag: 'first' },
      'GET /projects/a%2Fb': { personal: { allow_values: ['x'] } },
    })
    await api.changePersonal('a/b', (personal) => ({ ...personal, allow_values: [...(personal.allow_values ?? []), 'y'] }))
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual(['GET /config', 'GET /projects/a%2Fb', 'PUT /projects/a%2Fb/personal'])
    expect(calls[2]?.body).toEqual({ allow_values: ['x', 'y'], etag: 'first' })
  })
})

describe('the columns of a tool output', () => {
  it('asks the light answer for the notice (?shape=1), the full one for the panel', async () => {
    const { calls } = recording({})
    await api.columnShape('ex/1', 3)
    await api.columns('ex/1', 3)
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual(['GET /exchanges/ex%2F1/columns?slot=3&shape=1', 'GET /exchanges/ex%2F1/columns?slot=3'])
  })
})

describe('signing out', () => {
  it('is a POST a page of another site cannot make: it carries X-Latet-UI', async () => {
    const fetched: { url: string; init: RequestInit }[] = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      fetched.push({ url, init })
      return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'content-type': 'application/json' } })
    })
    setTransport(httpTransport)
    await api.logout()
    expect(fetched).toHaveLength(1)
    expect(fetched[0]?.url).toBe('/_latet/api/v1/logout')
    expect(fetched[0]?.init.method).toBe('POST')
    expect((fetched[0]?.init.headers as Record<string, string>)['X-Latet-UI']).toBe('1')
  })
})
