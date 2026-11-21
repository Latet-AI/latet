import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { Me } from '../../api/types'
import { MeContext, type MeState } from '../../lib/me'
import { EnterpriseReadOnly } from '../components/TrialEnded'
import type { Group, ScimTokenInfo, SignInView, SsoSettings } from '../types'

// The page as it renders for what the server answered: each of its reads gets its own answer.
const answers: {
  'ee-sign-in'?: SignInView
  'ee-groups'?: { groups: Group[] }
  'company-sign-in'?: { login: string } | null
} = {}
const asked: (string | null)[] = [] // every read the page made, by its key
vi.mock('../../lib/useAsync', () => ({
  useAsync: (key: keyof typeof answers | null) => {
    asked.push(key)
    return {
      data: key === null ? undefined : answers[key],
      error: undefined,
      loading: false,
      stale: false,
      reload: () => undefined,
      mutate: () => undefined,
    }
  },
}))

const { SignInPage } = await import('./SignInPage')

const sso: SsoSettings = {
  issuer: 'https://login.microsoftonline.com/4f2c9a71-3b8e-4d5a-9c6f-2e1b7d8a0c35/v2.0',
  client_id: '8b1e6f3a-52c4-4e9d-a7b2-0f9c3d6e1a84',
  client_secret: 'sso-client-secret',
  scopes: ['openid', 'profile', 'email'],
  user_name_claim: 'preferred_username',
  groups_claim: 'groups',
  accounts: 'group',
  access_group: '6e2f4a8c-1b3d-4f5a-8c7e-9d0b1a2c3e4f',
  allowed_domains: [],
  groups: { '2d7e9b14-6c3a-4f8e-a1d5-90b8c7e6f342': 'Payments leads' },
}
const token = (id: string, more: Partial<ScimTokenInfo> = {}): ScimTokenInfo => ({
  id,
  created: 1_789_000_000,
  expires: 1_797_000_000,
  last_used: null,
  made_by: 'u_emilycartr',
  maker: { kind: 'person', id: 'u_emilycartr', name: 'Emily Carter', admin: true, disabled: false },
  ...more,
})
const view = (more: Partial<SignInView> = {}): SignInView => ({
  sso,
  etag: 'b5e2a9c47d100000',
  company_changes: 'sso',
  redirect_uri: 'https://latet.acme.lan/_latet/sso/callback',
  secret: true,
  secret_issuer_changed: false,
  scim: { url: 'https://latet.acme.lan/scim/v2', tokens: [token('s_3f9a1c7e2b')] },
  ...more,
})

interface Seen {
  id?: string
  kind?: 'person' | 'key'
  via?: 'sso' | 'code'
  features?: string[]
  actions?: string[]
  readOnly?: boolean
  /** What `/me.edition.linked` says (true when left out); null: it says nothing (an API key, or the server could not read it). */
  linked?: boolean | null
  /** The Latet groups, with their roles; the page reads them to warn of a name that gives Admin. */
  groups?: Group[]
  /** Whether the server serves single sign-on (the sign-in page's button leads somewhere). */
  served?: boolean
}

/** Whether the markup has a button or a link with this label. */
const has = (html: string, label: string) => new RegExp(`<(button|a)[^>]*>(<svg[\\s\\S]*?</svg>)?${label}<`).test(html)

function page(answer: SignInView | undefined, seen: Seen = {}) {
  answers['ee-sign-in'] = answer
  answers['ee-groups'] = { groups: seen.groups ?? [] }
  answers['company-sign-in'] = seen.served === false ? null : { login: '/_latet/sso/login' }
  asked.length = 0
  const me = {
    server: { url: 'https://latet.acme.lan', identity: { id: seen.id ?? 'u_emilycartr', name: 'Emily Carter', kind: seen.kind ?? 'person' } },
    session: { via: seen.via ?? 'sso' },
    edition: {
      features: seen.features ?? ['sso', 'roles'],
      actions: seen.actions ?? ['sign-in.edit', 'people.manage'],
      ...(seen.linked === null ? {} : { linked: seen.linked ?? true }),
    },
  } as unknown as Me
  const value: MeState = { me, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
  const tree = seen.readOnly ? h(EnterpriseReadOnly.Provider, { value: true }, h(SignInPage)) : h(SignInPage)
  const html = renderToStaticMarkup(h(MemoryRouter, null, h(MeContext.Provider, { value }, tree))).replace(/<dialog[\s\S]*?<\/dialog>/g, '')
  return { html, text: html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' '), asked: [...asked] }
}

describe('the Sign-in page', () => {
  it("shows an administrator the settings with what edits them, and the sections' words", () => {
    const { html, text } = page(view())
    for (const label of ['Save', 'Test', 'Make a token', 'Make a code', 'Add a group', 'Save the secret']) expect(has(html, label), label).toBe(true)
    for (const words of ['Single sign-on', 'Provisioning', 'Who gets an account at a first sign-in', 'Members of the access group', 'Group of the ID token']) {
      expect(text, words).toContain(words)
    }
    expect(text).not.toContain('Read-only')
  })

  it('shows everyone else the settings, read-only, while roles do not decide', () => {
    const { html, text } = page(view(), { features: ['sso'], actions: [] })
    expect(text).toContain('Read-only: an administrator changes these settings.')
    for (const label of ['Save', 'Test', 'Make a token', 'Save the secret', 'Add a group', 'Remove']) expect(has(html, label), label).toBe(false)
  })

  it('shows a person who may not read them no settings at all, and the way to connect a computer', () => {
    const { html, text } = page(undefined, { actions: [] })
    expect(text).not.toContain('Single sign-on')
    expect(has(html, 'Make a code')).toBe(true)
  })

  it('shows no editing control, no token action and no way to connect a computer once a trial ended', () => {
    const { html, text } = page(view(), { readOnly: true })
    for (const label of ['Save', 'Test', 'Make a token', 'Make a code', 'Save the secret', 'Add a group', 'Remove']) expect(has(html, label), label).toBe(false)
    expect(text).toContain('Read-only')
    expect(text).toContain('Single sign-on') // what it holds can be read
  })

  it('asks Google for its domains, and nobody else', () => {
    expect(page(view()).text).not.toContain('Allowed domains')
    const google = { ...sso, issuer: 'https://accounts.google.com', accounts: 'scim' as const, access_group: null, allowed_domains: ['acme.com'] }
    expect(page(view({ sso: google })).text).toContain('Allowed domains')
  })

  it('warns of a group of the ID token that gives Admin by its name, and not of one given by its ID', () => {
    const group = (name: string, roles: Group['roles']): Group => ({ id: `g_${name}`, name, source: 'local', external_id: null, members: [], roles })
    const groups = [group('Latet admins', ['admin']), group('Payments leads', ['group-lead'])]
    const byName = { ...sso, groups: { 'latet-admins': 'Latet admins', 'payments-leads': 'Payments leads' } }
    const { text } = page(view({ sso: byName }), { groups })
    expect(text).toContain('latet-admins is matched by its name, and the Latet group Latet admins holds the Admin role.')
    expect(text.split('is matched by its name').length - 1).toBe(1) // a role of a lower kind is not Admin
    const byId = { ...sso, groups: { '2d7e9b14-6c3a-4f8e-a1d5-90b8c7e6f342': 'Latet admins' } }
    expect(page(view({ sso: byId }), { groups }).text).not.toContain('is matched by its name')
    expect(page(view({ sso: byName }), { groups: [] }).text).not.toContain('is matched by its name')
  })

  it('says whether the changes that apply to everyone need the company sign-in, and the command that sets it', () => {
    expect(page(view({ company_changes: 'sso' })).text).toContain('These changes need a session opened with your company account.')
    expect(page(view({ company_changes: 'any-session' })).text).toContain("Any administrator's session makes these changes")
    expect(page(view({ company_changes: null, sso: null })).text).toContain('Single sign-on is not set up')
    expect(page(view()).text).toContain('latet server sign-in set --company-changes')
  })

  it("says an administrator's own code session cannot make them, and offers the company sign-in", () => {
    const code = page(view(), { via: 'code' })
    expect(code.text).toContain('This session was opened with latet dashboard, so it cannot make these changes.')
    expect(code.html).toContain('href="/_latet/sso/login"')
    const company = page(view(), { via: 'sso' })
    expect(company.text).not.toContain('This session was opened with latet dashboard')
    expect(page(view({ company_changes: 'any-session' }), { via: 'code' }).text).not.toContain('This session was opened with latet dashboard')
  })

  it('leaves turning single sign-on off to the server while the company sign-in is asked for', () => {
    const asked = page(view({ company_changes: 'sso' }))
    expect(has(asked.html, 'Turn single sign-on off')).toBe(false)
    expect(asked.text).toContain('latet server sign-in off')
    expect(has(page(view({ company_changes: 'any-session' })).html, 'Turn single sign-on off')).toBe(true)
  })

  it('says who made each SCIM token, and when its maker no longer administers', () => {
    const left = token('s_8d2e5b1f4a', { maker: { kind: 'person', id: 'u_samriverax', name: 'Sam Rivera', admin: false, disabled: false } })
    const byCommand = token('s_2b9c4d6e1f', { maker: { kind: 'administration', id: 'admin:a1' } })
    const { text } = page(view({ scim: { url: 'https://latet.acme.lan/scim/v2', tokens: [token('s_3f9a1c7e2b'), left, byCommand] } }))
    expect(text).toContain('made by Emily Carter')
    expect(text).toContain('made by Sam Rivera')
    expect(text).toContain('made with latet server')
    expect(text.split('Its maker no longer administers').length - 1).toBe(1)
    expect(text).toContain('A role given to a group from SCIM is also given to whoever holds a SCIM token')
  })

  it('warns that a secret saved for another issuer is saved again, and that open accounts count in the license', () => {
    expect(page(view({ secret: false, secret_issuer_changed: true })).text).toContain('Saved for another issuer')
    expect(page(view({ secret: false })).text).toContain('Not saved')
    expect(page(view({ sso: { ...sso, accounts: 'open', access_group: null } })).text).toContain('each one counts in the license')
  })

  it('says the issuer is changed with the server once people have signed in', () => {
    expect(page(view()).text).toContain('the issuer is changed with latet server sign-in set, not here')
  })

  it('offers Link my company account to a person in a code session whose account /me says is not linked', () => {
    const unlinked = { via: 'code' as const, linked: false }
    expect(has(page(view(), unlinked).html, 'Link my company account')).toBe(true)
    expect(page(view(), unlinked).html).toContain('href="/_latet/sso/link"')
    expect(has(page(view(), { ...unlinked, linked: true }).html, 'Link my company account')).toBe(false) // linked already
    expect(has(page(view(), { via: 'sso', linked: false }).html, 'Link my company account')).toBe(false) // the company sign-in opened this session
    expect(has(page(view(), { ...unlinked, kind: 'key' }).html, 'Link my company account')).toBe(false) // an API key is no person
    expect(has(page(view(), { ...unlinked, readOnly: true }).html, 'Link my company account')).toBe(false)
  })

  it('offers it to nobody /me does not say it for, whoever they are, and tells a linked person nothing false', () => {
    // /me says nothing (the server could not read it, or it is no person's): no section, and no claim that it is not linked.
    for (const actions of [['sign-in.edit', 'people.manage'], [], ['sign-in.edit']]) {
      const { html, text } = page(view(), { via: 'code', linked: null, actions })
      expect(has(html, 'Link my company account'), actions.join()).toBe(false)
      expect(text).not.toContain('is not linked to your company account')
    }
    // A linked person, administrator or not: the section is not there, from the first render (nothing flashes).
    for (const actions of [['sign-in.edit', 'people.manage'], []]) {
      const { text } = page(view(), { via: 'code', linked: true, actions })
      expect(text, actions.join()).not.toContain('is not linked to your company account')
    }
    // Anyone it is false for gets it, an administrator or not.
    for (const actions of [['sign-in.edit', 'people.manage'], []]) {
      const { text } = page(view(), { via: 'code', linked: false, actions })
      expect(text, actions.join()).toContain('Your account on this server is not linked to your company account')
    }
  })

  it('does not read the People list to know whether a person is linked', () => {
    for (const actions of [['sign-in.edit', 'people.manage'], []]) {
      expect(page(view(), { via: 'code', linked: false, actions }).asked, actions.join()).not.toContain('ee-people')
    }
  })

  it('offers nothing to link where the server does not serve single sign-on, nor a way to the company sign-in', () => {
    const unserved = page(view(), { via: 'code', linked: false, served: false })
    expect(has(unserved.html, 'Link my company account')).toBe(false)
    expect(unserved.html).not.toContain('href="/_latet/sso/login"')
  })
})
