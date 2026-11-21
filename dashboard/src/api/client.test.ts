import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { approvalsApi, eeApi, peopleApi, policyApi } from '../ee/api'
import type { SsoSettings } from '../ee/types'
import { SPACE_STORAGE } from '../lib/space'
import { API_BASE, api, currentSpace, httpTransport, setTransport, switchSpace, type Method, type Transport } from './client'

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

const answer = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

describe("an API key's space", () => {
  const stored = new Map<string, string>()
  const assigned: string[] = []
  const fetched: string[] = []

  beforeEach(() => {
    stored.clear()
    assigned.length = 0
    fetched.length = 0
    vi.stubGlobal('window', {
      sessionStorage: {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => void stored.set(key, value),
        removeItem: (key: string) => void stored.delete(key),
      },
      location: { assign: (to: string) => void assigned.push(to) },
    })
  })

  afterEach(() => switchSpace(null)) // before the global is taken away: the page starts again in your own space

  it('is opened for this tab alone and sends each request of the dashboard there, but those about the keys', async () => {
    vi.stubGlobal('fetch', async (url: string) => {
      fetched.push(url)
      return answer({})
    })
    switchSpace('u_billingcix')
    expect(currentSpace()).toBe('u_billingcix')
    expect(stored.get(SPACE_STORAGE)).toBe('u_billingcix') // sessionStorage: this tab; nothing else is kept
    expect(assigned).toEqual(['/']) // the dashboard starts again there
    await httpTransport.request('GET', '/config')
    await httpTransport.request('GET', '/ee/keys')
    await httpTransport.request('POST', '/ee/keys/u_nightlyimp/controllers', { subject: 'g_platformxx' })
    expect(fetched).toEqual([
      `${API_BASE}/ee/keys/u_billingcix/space/config`,
      `${API_BASE}/ee/keys`,
      `${API_BASE}/ee/keys/u_nightlyimp/controllers`,
    ])
  })

  it('starts again with the flags the mock was opened with, though the router dropped them, and with none on a real server', async () => {
    const location = { search: '?mock-server&mock-sign-in', assign: (to: string) => void assigned.push(to) }
    vi.stubGlobal('window', { sessionStorage: { getItem: () => null, setItem: () => undefined, removeItem: () => undefined }, location })
    vi.resetModules()
    const fresh = await import('./client') // reads the address when it loads
    location.search = '' // the router moved the address on
    fresh.switchSpace('u_billingcix')
    expect(assigned).toEqual(['/']) // the real transport: no flag
    fresh.setTransport({ ...httpTransport, mock: true })
    fresh.switchSpace(null)
    expect(assigned).toEqual(['/', '/?mock-server&mock-sign-in'])
  })

  it('goes back to your own space, and starts there again, when the server closes it to you', async () => {
    const refusal = { error: 'you do not control this API key', space: 'closed' }
    vi.stubGlobal('fetch', async () => answer(refusal, 403))
    switchSpace('u_billingcix')
    await expect(httpTransport.request('GET', '/config')).rejects.toMatchObject({ status: 403 })
    expect(currentSpace()).toBeNull()
    expect(stored.has(SPACE_STORAGE)).toBe(false)
    expect(assigned).toEqual(['/', '/'])
  })

  it("stays where it is for a refusal that is the key's own", async () => {
    vi.stubGlobal('fetch', async () => answer({ error: 'Set by your company, not changed', floor: ['profile'] }, 409))
    switchSpace('u_billingcix')
    await expect(httpTransport.request('PUT', '/config', {})).rejects.toMatchObject({ status: 409 })
    expect(currentSpace()).toBe('u_billingcix')
    expect(assigned).toEqual(['/'])
  })

  it('is never left in your own space, which has none to leave', async () => {
    vi.stubGlobal('fetch', async () => answer({ error: 'origin not allowed' }, 403))
    await expect(httpTransport.request('GET', '/config')).rejects.toMatchObject({ status: 403 })
    expect(assigned).toEqual([])
  })

  it("signs you out of your own session, not as a change in the key's space", async () => {
    vi.stubGlobal('fetch', async (url: string) => {
      fetched.push(url)
      return answer({ ok: true })
    })
    switchSpace('u_billingcix')
    await api.logout()
    expect(fetched).toEqual([`${API_BASE}/logout`])
  })

  describe('and its live stream, which the browser gives up when the server closes it', () => {
    class Source {
      static CLOSED = 2
      static all: Source[] = []
      readyState = 0
      onopen: (() => void) | null = null
      onerror: (() => void) | null = null
      constructor(readonly url: string) {
        Source.all.push(this)
      }
      addEventListener() {}
      close() {}
    }

    beforeEach(() => {
      Source.all = []
      vi.stubGlobal('EventSource', Source)
    })

    /** Opens the stream in the key's space and has the browser give up on it, as it does on a refused stream. */
    function gaveUp(): () => void {
      switchSpace('u_billingcix')
      const close = httpTransport.subscribe(() => undefined, () => undefined)
      expect(Source.all.map((source) => source.url)).toEqual([`${API_BASE}/ee/keys/u_billingcix/space/stream`])
      const source = Source.all[0]
      if (!source) throw new Error('no stream was opened')
      source.readyState = Source.CLOSED
      source.onerror?.()
      return close
    }

    it('goes back to your own space when the probe it makes finds the space closed', async () => {
      vi.stubGlobal('fetch', async (url: string) => {
        fetched.push(url)
        return answer({ error: 'this API key is disabled', space: 'closed' }, 409)
      })
      const close = gaveUp()
      await vi.waitFor(() => expect(currentSpace()).toBeNull())
      expect(fetched).toEqual([`${API_BASE}/ee/keys/u_billingcix/space/me`])
      expect(assigned).toEqual(['/', '/'])
      close()
    })

    it('stays in the space when the probe is refused for anything else', async () => {
      vi.stubGlobal('fetch', async (url: string) => {
        fetched.push(url)
        return answer({ error: 'origin not allowed' }, 403)
      })
      const close = gaveUp()
      await vi.waitFor(() => expect(fetched).toHaveLength(1))
      await new Promise((resolve) => setTimeout(resolve, 0)) // the probe's answer is read after it arrives
      expect(currentSpace()).toBe('u_billingcix')
      expect(assigned).toEqual(['/'])
      close()
    })
  })
})

describe('the company sign-in', () => {
  const asked = (found: Response | Error) =>
    vi.stubGlobal('fetch', async () => {
      if (found instanceof Error) throw found
      return found
    })

  it("is the server's own login page, where it answers one", async () => {
    asked(answer({ login: '/_latet/sso/login' }))
    expect(await api.companySignIn()).toEqual({ login: '/_latet/sso/login' })
  })

  it('is none where the server answers 404 or cannot be reached', async () => {
    asked(answer({ error: 'not found' }, 404))
    expect(await api.companySignIn()).toBeNull()
    asked(new TypeError('Failed to fetch'))
    expect(await api.companySignIn()).toBeNull()
  })

  it("is never an address the answer made up: the button leads to the server's own single sign-on, nowhere else", async () => {
    for (const login of ['https://login.evil.example/', '//login.evil.example/', 'javascript:alert(1)', '/elsewhere', '/_latet/sso/../x', 7, null]) {
      asked(answer({ login }))
      expect(await api.companySignIn()).toBeNull()
    }
    asked(answer(null))
    expect(await api.companySignIn()).toBeNull()
  })
})

describe('the routes of the Enterprise pages', () => {
  it('name each person, group and key by an encoded id, and send what the server reads', async () => {
    const { calls } = recording({})
    await peopleApi.revoke('u_a/b', 'admin')
    await peopleApi.leave('g_x y', 'u_emilycartr')
    await peopleApi.release('u_billingcix', 'g_platformxx')
    await peopleApi.grant('Payments leads', 'group-lead')
    await peopleApi.makeScimToken()
    await peopleApi.told(4821)
    expect(calls).toEqual([
      { method: 'DELETE', path: '/ee/roles/u_a%2Fb/admin', body: undefined },
      { method: 'DELETE', path: '/ee/groups/g_x%20y/members/u_emilycartr', body: undefined },
      { method: 'DELETE', path: '/ee/keys/u_billingcix/controllers/g_platformxx', body: undefined },
      { method: 'POST', path: '/ee/roles', body: { subject: 'Payments leads', role: 'group-lead' } },
      { method: 'POST', path: '/ee/sign-in/scim-tokens', body: undefined },
      { method: 'POST', path: '/ee/notices/told', body: { until: 4821 } },
    ])
  })

  it('installs and removes a license, saying that the policy stopping was seen only when it was', async () => {
    const { calls } = recording({})
    await eeApi.installLicense('LICENSE FILE')
    await eeApi.installLicense('LICENSE FILE', { policyStops: true })
    await eeApi.removeLicense()
    await eeApi.removeLicense({ policyStops: true })
    expect(calls).toEqual([
      { method: 'PUT', path: '/ee/license', body: { license: 'LICENSE FILE' } },
      { method: 'PUT', path: '/ee/license', body: { license: 'LICENSE FILE', policy_stops: true } },
      { method: 'DELETE', path: '/ee/license', body: undefined },
      { method: 'DELETE', path: '/ee/license', body: { policy_stops: true } },
    ])
  })

  it('reads the license, the people, the groups, the keys, the sign-in and the policy text from their own addresses', async () => {
    const { calls } = recording({})
    await eeApi.license()
    await peopleApi.people()
    await peopleApi.groups()
    await peopleApi.keys()
    await peopleApi.signIn()
    await policyApi.text()
    expect(calls).toEqual([
      { method: 'GET', path: '/ee/license', body: undefined },
      { method: 'GET', path: '/ee/people', body: undefined },
      { method: 'GET', path: '/ee/groups', body: undefined },
      { method: 'GET', path: '/ee/keys', body: undefined },
      { method: 'GET', path: '/ee/sign-in', body: undefined },
      { method: 'GET', path: '/ee/policy/text', body: undefined },
    ])
  })

  it('adds a person with or without an e-mail address, makes their code, and disables and enables them, by an encoded id', async () => {
    const { calls } = recording({})
    await peopleApi.addPerson('Tom Becker', null)
    await peopleApi.addPerson('Emily Carter', 'emily.carter@acme.com')
    await peopleApi.code('u_tom/becker')
    await peopleApi.disable('u_tombeckerx')
    await peopleApi.enable('u_tombeckerx')
    expect(calls).toEqual([
      { method: 'POST', path: '/ee/people', body: { name: 'Tom Becker', email: null } },
      { method: 'POST', path: '/ee/people', body: { name: 'Emily Carter', email: 'emily.carter@acme.com' } },
      { method: 'POST', path: '/ee/people/u_tom%2Fbecker/code', body: undefined },
      { method: 'POST', path: '/ee/people/u_tombeckerx/disable', body: undefined },
      { method: 'POST', path: '/ee/people/u_tombeckerx/enable', body: undefined },
    ])
  })

  it('makes, renames and removes a group and puts a person in it, by an encoded id', async () => {
    const { calls } = recording({})
    await peopleApi.addGroup('Platform')
    await peopleApi.renameGroup('g_platformxx', 'Platform team')
    await peopleApi.removeGroup('g_platform xx')
    await peopleApi.join('g_platformxx', 'u_tombeckerx')
    expect(calls).toEqual([
      { method: 'POST', path: '/ee/groups', body: { name: 'Platform' } },
      { method: 'PUT', path: '/ee/groups/g_platformxx', body: { name: 'Platform team' } },
      { method: 'DELETE', path: '/ee/groups/g_platform%20xx', body: undefined },
      { method: 'POST', path: '/ee/groups/g_platformxx/members', body: { person: 'u_tombeckerx' } },
    ])
  })

  it("makes, rotates, disables and enables an API key, and gives its control, by an encoded id", async () => {
    const { calls } = recording({})
    await peopleApi.makeKey('Billing CI')
    await peopleApi.rotateKey('u_billing/cix')
    await peopleApi.disableKey('u_billingcix')
    await peopleApi.enableKey('u_billingcix')
    await peopleApi.control('u_billingcix', 'g_platformxx')
    expect(calls).toEqual([
      { method: 'POST', path: '/ee/keys', body: { name: 'Billing CI' } },
      { method: 'POST', path: '/ee/keys/u_billing%2Fcix/rotate', body: undefined },
      { method: 'POST', path: '/ee/keys/u_billingcix/disable', body: undefined },
      { method: 'POST', path: '/ee/keys/u_billingcix/enable', body: undefined },
      { method: 'POST', path: '/ee/keys/u_billingcix/controllers', body: { subject: 'g_platformxx' } },
    ])
  })

  it("saves the sign-in settings with the etag they were read with, and sends one even when single sign-on was off", async () => {
    const { calls } = recording({})
    const sso = { issuer: 'https://login.example.com', client_id: 'latet' } as unknown as SsoSettings
    await peopleApi.saveSignIn(sso, 'b5e2a9c47d100001')
    await peopleApi.saveSignIn(sso, null) // single sign-on was off when they were read: "none was set up", not "no etag"
    await peopleApi.saveSignIn(null, 'b5e2a9c47d100002')
    expect(calls).toEqual([
      { method: 'PUT', path: '/ee/sign-in', body: { sso, etag: 'b5e2a9c47d100001' } },
      { method: 'PUT', path: '/ee/sign-in', body: { sso, etag: null } },
      { method: 'PUT', path: '/ee/sign-in', body: { sso: null, etag: 'b5e2a9c47d100002' } },
    ])
    expect(calls.every((call) => 'etag' in (call.body as object))).toBe(true)
  })

  it('saves, forgets and tests the client secret, removes a SCIM token by an encoded id and makes a code to connect a computer', async () => {
    const { calls } = recording({})
    const sso = { issuer: 'https://login.example.com' } as unknown as SsoSettings
    await peopleApi.saveSecret('the-secret')
    await peopleApi.removeSecret()
    await peopleApi.testSignIn(sso)
    await peopleApi.removeScimToken('s_3f9a/1c7e')
    await peopleApi.connectCode()
    expect(calls).toEqual([
      { method: 'PUT', path: '/ee/sign-in/secret', body: { secret: 'the-secret' } },
      { method: 'DELETE', path: '/ee/sign-in/secret', body: undefined },
      { method: 'POST', path: '/ee/sign-in/test', body: { sso } },
      { method: 'DELETE', path: '/ee/sign-in/scim-tokens/s_3f9a%2F1c7e', body: undefined },
      { method: 'POST', path: '/ee/sign-in/code', body: undefined },
    ])
  })

  it('saves the policy and removes it naming the etag of the policy its text was read from, null when none was stored', async () => {
    const { calls } = recording({})
    await policyApi.save('latet_rules: 1', '3f1c9a7e52d04b18')
    await policyApi.save('latet_rules: 1', null)
    await policyApi.remove('3f1c9a7e52d04b18')
    await policyApi.remove(null)
    expect(calls).toEqual([
      { method: 'PUT', path: '/ee/policy', body: { policy: 'latet_rules: 1', etag: '3f1c9a7e52d04b18' } },
      { method: 'PUT', path: '/ee/policy', body: { policy: 'latet_rules: 1', etag: null } },
      { method: 'DELETE', path: '/ee/policy', body: { etag: '3f1c9a7e52d04b18' } },
      { method: 'DELETE', path: '/ee/policy', body: { etag: null } },
    ])
  })
})

describe('an image waiting in the Live Reviewer', () => {
  it('is asked as it came, or as it leaves now with the mark of its text as masked', () => {
    expect(api.approvalImageUrl('b/1', 0)).toBe('/_latet/api/v1/approvals/b%2F1/images/0')
    expect(api.approvalImageUrl('b/1', 0, 'sent')).toBe('/_latet/api/v1/approvals/b%2F1/images/0?view=sent')
    expect(api.approvalImageUrl('b/1', 2, 'sent', 'k3x9')).toBe('/_latet/api/v1/approvals/b%2F1/images/2?view=sent&at=k3x9')
    expect(api.approvalImageUrl('b/1', 2, 'original', 'k3x9')).toBe('/_latet/api/v1/approvals/b%2F1/images/2')
    // Company approvals' route for the same image (Latet Enterprise), with the same mark.
    expect(approvalsApi.imageUrl('b/1', 2, 'sent', 'k3x9')).toBe('/_latet/api/v1/ee/approvals/batches/b%2F1/images/2?view=sent&at=k3x9')
    expect(approvalsApi.imageUrl('b/1', 2, 'original', 'k3x9')).toBe('/_latet/api/v1/ee/approvals/batches/b%2F1/images/2')
    expect(approvalsApi.imageUrl('b/1', 0, 'sent')).toBe('/_latet/api/v1/ee/approvals/batches/b%2F1/images/0?view=sent')
  })
})
