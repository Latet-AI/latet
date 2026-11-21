import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Me } from '../api/types'

// The mock reads its flags from the address once, when it loads: each case loads it again with the address it asks.
async function loadedAs(search: string) {
  vi.stubGlobal('window', {
    location: { search },
    sessionStorage: { getItem: () => null, setItem: () => undefined, removeItem: () => undefined },
  })
  vi.resetModules()
  const [people, ee] = await Promise.all([import('./people'), import('./ee')])
  return { ...people, routeEe: ee.routeEe, identityView: ee.identityView }
}

interface Listed {
  people: { id: string; linked: boolean; last_seen: number | null; disabled_by: string | null }[]
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('the mock of the Enterprise pages', () => {
  it("is a computer's without ?mock-server: no person, group, key, sign-in or license change answers", async () => {
    const { routePeople, routeEe } = await loadedAs('?mock-sign-in&mock-license')
    for (const [method, path] of [
      ['GET', '/ee/people'],
      ['GET', '/ee/groups'],
      ['GET', '/ee/keys'],
      ['GET', '/ee/sign-in'],
      ['POST', '/ee/notices/told'],
    ] as const) {
      expect(routePeople(method, path, undefined), path).toBeUndefined()
      expect(routeEe(method, path, undefined), path).toBeUndefined()
    }
    expect(routeEe('DELETE', '/ee/license', undefined)).toBeUndefined()
  })

  it("answers GET /ee/license with ?mock-server alone: a computer has no license to read (R31)", async () => {
    for (const search of ['', '?mock-license', '?mock-sign-in&mock-license=valid', '?mock-license=trial-ended']) {
      const { routeEe } = await loadedAs(search)
      expect(routeEe('GET', '/ee/license', undefined), search).toBeUndefined()
    }
    const { routeEe } = await loadedAs('?mock-server&mock-license')
    expect(routeEe('GET', '/ee/license', undefined)).toMatchObject({ state: 'valid', source: 'server' })
    expect((await loadedAs('?mock-server&mock-license=trial-ended')).routeEe('GET', '/ee/license', undefined)).toMatchObject({ state: 'trial-ended' })
    expect((await loadedAs('?mock-server')).routeEe('GET', '/ee/license', undefined)).toMatchObject({ state: 'none', license: null })
  })

  it("names the person by the provider on a server alone: the Rules page's line under the full name (R31)", async () => {
    const own = { enabled: true, usernames: ['ecarter'], hostname: null, full_name: 'Emily C.', emails: [] }
    expect((await loadedAs('?mock-provider-name&mock-name-masked')).identityView(own)).toBe(own) // a computer's is its own
    expect((await loadedAs('?mock-server')).identityView(own)).toBe(own)
    expect((await loadedAs('?mock-server&mock-provider-name')).identityView(own)).toMatchObject({
      full_name: 'Emily Carter',
      full_name_from: 'provider',
      full_name_locked: true,
    })
    const masked = (await loadedAs('?mock-server&mock-name-masked')).identityView(own)
    expect(masked).toMatchObject({ full_name: 'Emily C.', full_name_from: null, full_name_locked: true }) // hers, not hers to change
  })

  it("is a server's with ?mock-server: its people, and a license an administrator changes", async () => {
    const { routePeople, routeEe } = await loadedAs('?mock-server&mock-sign-in&mock-license')
    expect(routePeople('GET', '/ee/people', undefined)).toMatchObject({ enabled: 7 })
    expect(routeEe('GET', '/ee/keys', undefined)).toMatchObject({ manage: true })
    expect(routeEe('DELETE', '/ee/license', undefined)).toMatchObject({ state: 'none', removed: true })
  })

  it("leaves /me as it is on a computer, and says a server's session and roles on a server", async () => {
    const me = { server: null, edition: { name: 'community' } } as unknown as Me
    expect((await loadedAs('?mock-sign-in')).withSignIn(me)).toBe(me)
    const edition = { name: 'enterprise', license: null, features: [], pages: [], roles: [], actions: [] }
    const server = { ...me, server: { url: 'https://latet.acme.lan', identity: null }, edition } as unknown as Me
    const found = (await loadedAs('?mock-server&mock-sign-in')).withSignIn(server)
    expect(found.session).toEqual({ via: 'sso' })
    expect(found.edition?.roles).toEqual(['admin', 'user'])
  })

  it('has the security role set the policy on a server alone (?mock-security)', async () => {
    const computer = await loadedAs('?mock-sign-in&mock-security&mock-policy')
    expect(computer.SECURITY_MOCK).toBe(false)
    expect(computer.routePeople('PUT', '/ee/policy', { policy: 'latet_rules: 1\n', etag: null })).toBeUndefined()
    const me = { server: null, edition: { name: 'community' } } as unknown as Me
    expect(computer.withSecurity(me)).toBe(me)
    const empty = { floors: [], below: [], problems: [] }
    expect(computer.asServerFloor(empty, { ...empty, below: ['profile: from max to balanced'] })).toBe(empty)
    const { routePeople, withSecurity, withSignIn } = await loadedAs('?mock-server&mock-sign-in&mock-security&mock-policy')
    const edition = { name: 'enterprise', license: null, features: [], pages: [], roles: [], actions: [] }
    const daniel = withSecurity(withSignIn({ ...me, edition } as unknown as Me))
    expect(daniel.server?.identity?.name).toBe('Daniel Okafor')
    expect(daniel.edition?.actions).toContain('policy.edit')
    const shown = routePeople('GET', '/ee/policy/text', undefined) as { etag: string }
    expect(shown).toMatchObject({ policy: { in_force: true, exceptions: 'company', etag: shown.etag } })
    expect(() => routePeople('PUT', '/ee/policy', { policy: 'latet_rules: 1\nallow_values: [acme.com\n', etag: shown.etag })).toThrow('not valid YAML (line 2')
    expect(() => routePeople('PUT', '/ee/policy', { policy: 'latet_rules: 1\n', etag: null })).toThrow('was changed since')
    // A removal names the etag of the policy it removes, as a save does: the server answers 400 without one, and 409
    // `policy-changed` with a stale one, the policy staying.
    expect(() => routePeople('DELETE', '/ee/policy', undefined)).toThrow('the body is {"etag"')
    expect(() => routePeople('DELETE', '/ee/policy', { etag: 7 })).toThrow('the body is {"etag"')
    const stale = (() => {
      try {
        routePeople('DELETE', '/ee/policy', { etag: null })
      } catch (error) {
        return error as { status?: number; body?: { code?: string } }
      }
      return undefined
    })()
    expect(stale).toMatchObject({ status: 409, body: { code: 'policy-changed' } })
    expect(routePeople('GET', '/ee/policy/text', undefined)).toMatchObject({ policy: { etag: shown.etag } }) // still stored
    expect(routePeople('DELETE', '/ee/policy', { etag: shown.etag })).toEqual({ removed: true })
    expect(routePeople('GET', '/ee/policy/text', undefined)).toEqual({ etag: null, policy: null, problems: [] })
  })

  it("refuses the policy's text to a session of latet dashboard's code while single sign-on asks for the company's", async () => {
    const { routePeople } = await loadedAs('?mock-server&mock-sign-in&mock-security&mock-policy&mock-code-session')
    expect(() => routePeople('GET', '/ee/policy/text', undefined)).toThrow("sign in with your company account to read the policy's text")
  })

  it('keeps the floor, /me and the editor in agreement without ?mock-policy too', async () => {
    const { asServerFloor, routePeople, withSecurity, withSignIn } = await loadedAs('?mock-server&mock-sign-in&mock-security')
    const me = { server: null, edition: { name: 'enterprise', license: null, features: [], pages: [], roles: [], actions: [] } } as unknown as Me
    const floor = { source: 'server', origin: 'https://latet.acme.lan', received: 1, exceptions: 'company', rules: { latet_rules: 1 }, notices: 0 }
    const sample = { floors: [floor], below: [], problems: [] } as never
    const empty = { floors: [], below: [], problems: [] }
    expect(routePeople('GET', '/ee/policy/text', undefined)).toEqual({ etag: null, policy: null, problems: [] })
    expect(asServerFloor(empty, sample).floors).toEqual([])
    expect(withSecurity(withSignIn(me)).edition?.policy).toBeNull()
    const saved = routePeople('PUT', '/ee/policy', { policy: 'latet_rules: 1\nprofile: max\n', etag: null }) as { etag: string }
    expect(saved).toMatchObject({ keys: ['profile', 'ner', 'terms', 'policies', 'allow_values'] })
    expect(asServerFloor(empty, sample).floors).toHaveLength(1) // saved: the server's floor, as /me says
    expect(withSecurity(withSignIn(me)).edition?.policy?.sources).toHaveLength(1)
    expect(routePeople('DELETE', '/ee/policy', { etag: saved.etag })).toEqual({ removed: true })
    expect(asServerFloor(empty, sample).floors).toEqual([])
  })

  it("reads for a session of latet dashboard's code and refuses what applies to everyone, as the server says it", async () => {
    const { routePeople, routeEe } = await loadedAs('?mock-server&mock-sign-in&mock-code-session')
    expect(routePeople('GET', '/ee/people', undefined)).toMatchObject({ enabled: 7 })
    const refusal = (method: 'POST' | 'PUT' | 'DELETE', path: string) => {
      try {
        routeEe(method, path, {})
      } catch (error) {
        return error as { status?: number; body?: { code?: string } }
      }
      return undefined
    }
    for (const [method, path] of [
      ['POST', '/ee/people'],
      ['POST', '/ee/roles'],
      ['POST', '/ee/keys'],
      ['PUT', '/ee/sign-in'],
      ['POST', '/ee/sign-in/code'],
      ['POST', '/ee/sign-in/scim-tokens'],
      ['DELETE', '/ee/license'],
    ] as const) {
      expect(refusal(method, path), path).toMatchObject({ status: 403, body: { code: 'company-sign-in' } })
    }
    // A session the company sign-in opened makes them.
    const company = await loadedAs('?mock-server&mock-sign-in')
    expect(company.routePeople('POST', '/ee/sign-in/code', undefined)).toMatchObject({ code: expect.any(String) })
  })

  it('leaves the administrator unlinked with ?mock-unlinked, and Tom Becker without a computer', async () => {
    const { routePeople } = await loadedAs('?mock-server&mock-sign-in&mock-unlinked')
    const { people } = routePeople('GET', '/ee/people', undefined) as Listed
    expect(people.find((person) => person.id === 'u_emilycartr')?.linked).toBe(false)
    expect(people.find((person) => person.id === 'u_tombeckerx')?.last_seen).toBeNull()
    expect(people.find((person) => person.id === 'u_samriverax')?.disabled_by).toBe('local')
    const linked = (await loadedAs('?mock-server&mock-sign-in')).routePeople('GET', '/ee/people', undefined) as Listed
    expect(linked.people.find((person) => person.id === 'u_emilycartr')?.linked).toBe(true)
  })

  it("tells in /me, as the server does, whether the person's own account is linked, and nothing on a computer", async () => {
    const edition = { name: 'enterprise', license: null, features: [], pages: [], roles: [], actions: [] }
    const me = { server: { url: 'https://latet.acme.lan', identity: null }, edition } as unknown as Me
    expect((await loadedAs('?mock-server&mock-sign-in')).withSignIn(me).edition?.linked).toBe(true)
    expect((await loadedAs('?mock-server&mock-sign-in&mock-unlinked')).withSignIn(me).edition?.linked).toBe(false)
    const computer = { server: null, edition: { ...edition, name: 'community' } } as unknown as Me
    expect((await loadedAs('?mock-sign-in&mock-unlinked')).withSignIn(computer).edition).not.toHaveProperty('linked')
  })

  it('refuses a save of the sign-in settings made from settings that changed since, and keeps them (409 sign-in-changed)', async () => {
    const { routePeople } = await loadedAs('?mock-server&mock-sign-in')
    const refusal = (etag: unknown) => {
      try {
        routePeople('PUT', '/ee/sign-in', { sso: null, etag })
      } catch (error) {
        return error as { status?: number; body?: { code?: string } }
      }
      return undefined
    }
    const read = routePeople('GET', '/ee/sign-in', undefined) as { etag: string | null; sso: { issuer: string } | null }
    expect(read.etag).toEqual(expect.any(String))
    expect(refusal(null)).toMatchObject({ status: 409, body: { code: 'sign-in-changed' } }) // said off when it is not
    expect(refusal('b5e2a9c47d10ffff')).toMatchObject({ status: 409, body: { code: 'sign-in-changed' } })
    expect((routePeople('GET', '/ee/sign-in', undefined) as { sso: unknown }).sso).not.toBeNull() // nothing changed
  })

  it('makes a new etag with each save of the sign-in settings, and answers the settings it saved with it', async () => {
    const { routePeople } = await loadedAs('?mock-server&mock-sign-in')
    const first = routePeople('GET', '/ee/sign-in', undefined) as { etag: string; sso: Record<string, unknown> }
    const saved = routePeople('PUT', '/ee/sign-in', { sso: { ...first.sso, client_id: 'a-new-client' }, etag: first.etag }) as { etag: string; sso: { client_id: string } }
    expect(saved.sso.client_id).toBe('a-new-client')
    expect(saved.etag).not.toBe(first.etag)
    expect((routePeople('GET', '/ee/sign-in', undefined) as { etag: string }).etag).toBe(saved.etag)
    // The etag they were read with is stale now: a second save from the old form is refused.
    expect(() => routePeople('PUT', '/ee/sign-in', { sso: first.sso, etag: first.etag })).toThrow('were changed since they were read here')
    // Without an etag, as before (a client that does not name one).
    expect(routePeople('PUT', '/ee/sign-in', { sso: first.sso })).toMatchObject({ sso: { client_id: first.sso.client_id } })
  })

  it('tells which people their first sign-in finds by the user name an administrator or SCIM linked (claimable)', async () => {
    const { routePeople } = await loadedAs('?mock-server&mock-sign-in')
    const { people } = routePeople('GET', '/ee/people', undefined) as { people: { id: string; linked: boolean; claimable: boolean }[] }
    expect(people.every((person) => typeof person.claimable === 'boolean')).toBe(true)
    const claimable = people.filter((person) => person.claimable)
    expect(claimable.map((person) => person.id)).toEqual(['u_priyashahx'])
    expect(claimable.every((person) => !person.linked)).toBe(true) // linked (a subject) and claimable (waiting for one) are apart
  })
})
