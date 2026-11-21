import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import i18n, { LANGUAGES } from '../../i18n'
import type { Group, SsoSettings } from '../types'
import {
  adminByName,
  byName,
  connectRefused,
  draftProblems,
  editsSettings,
  fromDraft,
  googleIssuer,
  LINK_START,
  NOTICE_TEXT,
  saveFrom,
  secureIssuer,
  seesSettings,
  signInChanged,
  signInNotice,
  toDraft,
} from './signIn'

const sso: SsoSettings = {
  issuer: 'https://login.microsoftonline.com/4f2c9a71-3b8e-4d5a-9c6f-2e1b7d8a0c35/v2.0',
  client_id: '8b1e6f3a-52c4-4e9d-a7b2-0f9c3d6e1a84',
  client_secret: 'sso-client-secret',
  scopes: ['openid', 'profile', 'email'],
  user_name_claim: 'preferred_username',
  groups_claim: 'groups',
  accounts: 'group',
  access_group: 'latet-users',
  allowed_domains: [],
  groups: { '2d7e9b14-6c3a-4f8e-a1d5-90b8c7e6f342': 'Payments leads' },
}

const google: SsoSettings = {
  ...sso,
  issuer: 'https://accounts.google.com',
  accounts: 'scim',
  access_group: null,
  allowed_domains: ['acme.com'],
}

describe('the sign-in settings form', () => {
  it('turns the saved settings into a draft and back unchanged', () => {
    expect(fromDraft(toDraft(sso), sso)).toEqual(sso)
    expect(fromDraft(toDraft(google), google)).toEqual(google)
  })

  it('starts empty, with the defaults of the settings', () => {
    const draft = toDraft(null)
    expect(draft.scopes).toBe('openid profile email')
    expect(draft.accounts).toBe('scim') // the narrowest way in: nobody gets an account that SCIM did not provision
    expect(fromDraft({ ...draft, issuer: 'https://login.acme.com', clientId: 'latet' }, null).client_secret).toBe('sso-client-secret')
  })

  it('says what cannot work before it is saved', () => {
    expect(draftProblems(toDraft(sso))).toEqual([])
    const broken = {
      ...toDraft(sso),
      issuer: 'http://login.acme.com',
      clientId: ' ',
      scopes: 'profile email',
      rows: [
        { claim: 'payments-leads', group: 'Payments leads' },
        { claim: '/Payments-Leads', group: 'Payments' },
        { claim: '', group: ' ' },
      ],
    }
    expect(draftProblems(broken).sort()).toEqual(['claimEmpty', 'claimTwice', 'clientId', 'groupName', 'issuer', 'openid'])
  })

  it('says who gets an account as the server asks it: a group for the group, none for anyone, domains for Google alone', () => {
    const draft = toDraft(sso)
    expect(draftProblems({ ...draft, accessGroup: ' ' })).toEqual(['accessGroup'])
    expect(draftProblems({ ...draft, accounts: 'open' })).toEqual(['accessGroupOpen'])
    expect(draftProblems({ ...draft, accounts: 'open', accessGroup: '' })).toEqual([])
    expect(draftProblems({ ...draft, accounts: 'scim', accessGroup: '' })).toEqual([])
    expect(draftProblems({ ...draft, allowedDomains: 'acme.com' })).toEqual(['domainsNotGoogle'])
    const workspace = toDraft(google)
    expect(draftProblems(workspace)).toEqual([])
    expect(draftProblems({ ...workspace, issuer: 'https://accounts.google.com/', allowedDomains: '' })).toEqual(['domains'])
    expect(draftProblems({ ...workspace, allowedDomains: 'acme.com, not a domain' })).toEqual(['domain'])
    expect(draftProblems({ ...workspace, allowedDomains: 'Acme.COM\nacme.de' })).toEqual([])
  })

  it('keeps the allowed domains as a list, lower case, each once', () => {
    const draft = { ...toDraft(google), allowedDomains: 'Acme.com, acme.de\nacme.COM' }
    expect(fromDraft(draft, google).allowed_domains).toEqual(['acme.com', 'acme.de'])
  })

  it('takes http only on this computer', () => {
    expect(secureIssuer('http://localhost:8080/realms/acme')).toBe(true)
    expect(secureIssuer('http://keycloak.acme.lan/realms/acme')).toBe(false)
    expect(secureIssuer('not an address')).toBe(false)
  })
})

describe('the sign-in page', () => {
  it('reads why the last sign-in did not happen, an unknown reason being a failure', () => {
    expect(signInNotice('?sign-in=not-allowed')).toBe('notAllowed')
    expect(signInNotice('?sign-in=not-provisioned')).toBe('notProvisioned')
    expect(signInNotice('?sign-in=trial-ended')).toBe('trialEnded')
    expect(signInNotice('?sign-in=whatever')).toBe('failed')
    expect(signInNotice('?login=expired')).toBeNull()
  })

  it('shows the settings to everyone while roles do not decide, and then to those who edit them', () => {
    expect(seesSettings({ features: ['sso'], actions: [] })).toBe(true)
    expect(seesSettings({ features: ['sso', 'roles'], actions: [] })).toBe(false)
    expect(seesSettings({ features: ['sso', 'roles'], actions: ['sign-in.edit'] })).toBe(true)
    expect(editsSettings({ features: ['sso'], actions: [] })).toBe(false)
  })
})

const group = (name: string, roles: Group['roles']): Group => ({ id: `g_${name}`, name, source: 'local', external_id: null, members: [], roles })

describe('a group of the ID token mapped by its name', () => {
  it('tells a name from the ID a provider gives a group', () => {
    expect(byName('latet-admins')).toBe(true)
    expect(byName('/Payments/Leads')).toBe(true) // Keycloak's path is a name
    expect(byName('2d7e9b14-6c3a-4f8e-a1d5-90b8c7e6f342')).toBe(false) // Microsoft Entra's and Keycloak's IDs
    expect(byName('00gabcdefghijklmnopq')).toBe(false) // Okta's
    expect(byName('1234567890')).toBe(false) // a number
    expect(byName(' / ')).toBe(false) // nothing to map yet
  })

  it('warns of a name that gives the Admin role, and of nothing else', () => {
    const groups = [group('Latet admins', ['admin']), group('Payments leads', ['group-lead']), group('Platform', [])]
    const rows = [
      { claim: 'latet-admins', group: ' latet ADMINS ' }, // by name, to a group with Admin: the server compares names case aside
      { claim: '6e2f4a8c-1b3d-4f5a-8c7e-9d0b1a2c3e4f', group: 'Latet admins' }, // by ID: only its owner can join it
      { claim: 'payments', group: 'Payments leads' }, // a role, but not Admin
      { claim: 'platform', group: 'Platform' },
      { claim: 'ops', group: 'Not made yet' },
    ]
    expect(adminByName(rows, groups)).toEqual([rows[0]])
    expect(adminByName(rows, [])).toEqual([])
  })
})

describe('what the sign-in page tells', () => {
  it('knows Google by its issuer, a trailing slash aside', () => {
    expect(googleIssuer('https://accounts.google.com')).toBe(true)
    expect(googleIssuer(' https://accounts.google.com/ ')).toBe(true)
    expect(googleIssuer('https://login.microsoftonline.com/x/v2.0')).toBe(false)
  })

  it('begins linking at a page of the server, and says every reason in every language', async () => {
    expect(LINK_START).toBe('/_latet/sso/link')
    for (const language of LANGUAGES) {
      await i18n.changeLanguage(language)
      for (const key of Object.values(NOTICE_TEXT)) expect(i18n.exists(key, { lng: language, fallbackLng: [] }), `${language}: ${key}`).toBe(true)
    }
    await i18n.changeLanguage('en')
  })
})

describe('a save of the sign-in settings made from settings that changed since', () => {
  const words = 'not changed: the sign-in settings were changed since they were read here: read them again, then change them'

  it('is known by its code, from a conflict alone', () => {
    expect(signInChanged(new ApiError(409, words, [], { error: words, code: 'sign-in-changed' }))).toBe(true)
    // The other conflicts of the page have their own words and their own way on.
    const off = 'while changes that apply to everyone take the company sign-in, single sign-on is turned off with a command'
    expect(signInChanged(new ApiError(409, off, [], { error: off }))).toBe(false)
    expect(signInChanged(new ApiError(409, 'x', [], { error: 'x', code: 'policy-changed' }))).toBe(false)
    expect(signInChanged(new ApiError(403, words, [], { error: words, code: 'sign-in-changed' }))).toBe(false)
    expect(signInChanged(new ApiError(409, words))).toBe(false) // the words alone are no marker
    expect(signInChanged(new Error('sign-in-changed'))).toBe(false)
    expect(signInChanged(null)).toBe(false)
  })

  it('names the etag of the view the form was made from, null while single sign-on was off', async () => {
    const saved: unknown[][] = []
    const save = async (...args: unknown[]) => saved.push(args)
    await saveFrom({ etag: 'b5e2a9c47d100003' }, sso, save)
    await saveFrom({ etag: null }, sso, save) // not "no etag": none was set up
    await saveFrom({ etag: 'b5e2a9c47d100003' }, null, save)
    expect(saved).toEqual([
      [sso, 'b5e2a9c47d100003'],
      [sso, null],
      [null, 'b5e2a9c47d100003'],
    ])
  })
})

describe('a code to connect a computer the server refused for its limits', () => {
  const t = i18n.t.bind(i18n)
  const most = (count: number) => {
    const words = `you have ${count} computers or more connected to your account, the most one person may connect with their own codes`
    return new ApiError(409, words, [], { error: words, code: 'computers-most', most: count })
  }
  const many = (count: number) => {
    const words = `you made ${count} codes in the last hour, the most one person may: try again later`
    return new ApiError(429, words, [], { error: words, code: 'codes-too-many', most: count })
  }

  it("says why in the reader's words, with the server's own number", () => {
    const computers = connectRefused(t, most(17))
    expect(computers).toBe(t('ee.signIn.connect.most', { most: 17 }))
    expect(computers).toContain('17')
    expect(computers).toContain('`latet disconnect`')
    const codes = connectRefused(t, many(7))
    expect(codes).toBe(t('ee.signIn.connect.tooMany', { most: 7 }))
    expect(codes).toContain('7')
  })

  it('leaves every other refusal to the server’s reason, as every change says it', () => {
    expect(connectRefused(t, new ApiError(409, 'your account is disabled', [], { error: 'your account is disabled' }))).toBeNull()
    expect(connectRefused(t, new ApiError(404, 'not found', [], { error: 'not found' }))).toBeNull()
    expect(connectRefused(t, new ApiError(429, 'x', [], { error: 'x', code: 'computers-most', most: 3 }))).toBeNull() // each its own status
    expect(connectRefused(t, new ApiError(409, 'x', [], { error: 'x', code: 'codes-too-many', most: 3 }))).toBeNull()
    expect(connectRefused(t, new ApiError(409, 'x'))).toBeNull() // the words alone are no marker
    expect(connectRefused(t, new Error('computers-most'))).toBeNull()
    expect(connectRefused(t, null)).toBeNull()
  })

  it('says it in every language', async () => {
    for (const language of LANGUAGES) {
      await i18n.changeLanguage(language)
      for (const key of ['ee.signIn.connect.most', 'ee.signIn.connect.tooMany']) {
        expect(i18n.exists(key, { lng: language, fallbackLng: [] }), `${language}: ${key}`).toBe(true)
        expect(i18n.t(key, { most: 13 }), `${language}: ${key}`).toContain('13')
      }
    }
    await i18n.changeLanguage('en')
  })
})
