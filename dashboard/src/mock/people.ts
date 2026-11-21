import { ApiError, currentSpace, type Method } from '../api/client'
import type { ComputerOfYours, ConnectedNotice, Me } from '../api/types'
import type { PolicyView } from '../ee/lib/policy'
import type {
  ApiKey,
  Controller,
  DisabledBy,
  GrantedRole,
  Group,
  GroupHead,
  GroupSource,
  Person,
  ScimTokenInfo,
  SignInView,
  SsoSettings,
} from '../ee/types'
import { NOW } from './fixtures'

// ?mock-server&mock-sign-in: the mock is a server of Latet Enterprise, its user an administrator signed in with the
// company's sign-in, with people, groups, API keys, single sign-on and SCIM set up: the People and groups and Sign-in
// pages, and their screenshots. Without ?mock-server the mock is a computer, the Community Edition, and this flag
// changes nothing (R31). &mock-code-session opens the session with `latet dashboard`'s one-time code instead (a
// session that cannot say a notice seen), &mock-unlinked leaves that account not linked to the company sign-in (/me says
// `linked: false`, and the Sign-in page then offers Link my company account in a code session), and &mock-connected
// has the dashboard tell of a computer and a code.
const search = typeof window === 'undefined' ? '' : window.location.search
export const SIGN_IN_MOCK = search.includes('mock-sign-in')
const ON_SERVER = search.includes('mock-server') // the routes below are a server's: a computer has none of them (R31)
const CODE_SESSION = search.includes('mock-code-session')
const CONNECTED = search.includes('mock-connected')
const UNLINKED = search.includes('mock-unlinked') // the administrator's account is not linked to the company sign-in yet

const SERVER = 'https://latet.acme.lan'
const DAY = 86400
const CODE = 'K7QX-M2PV-9WDT'
const COMMAND = `latet connect ${SERVER}` // E0c's: the code is shown apart, out of a shell's history
const ADMIN = 'u_emilycartr'
// The words the server gives (ee/people_api.py, ee/sign_in_api.py), which a refusal here repeats.
const CONNECTED_ALREADY =
  'a computer of theirs is connected already: a code made here would connect another one they never see; `latet server people code` makes it'
const CHANGES_BY_SSO = 'sign in with your company account to change what applies to everyone'
const TOLD_BY_SSO =
  'what your dashboard tells you is said seen from a session opened with the company sign-in; without one, it goes by itself 14 days after it happened'

// ?mock-server&mock-sign-in&mock-security: the person is Daniel Okafor, of the security role, who sets the server's policy
// on the Policy page and decides on Company approvals (mock/ee-approvals.ts); with ?mock-policy too, E2's sample is the policy in force, and the editor starts from its text. The
// mock keeps the text it is given (a line with a [ it does not close is refused, as the server refuses YAML it cannot
// read); whatever text is kept, the rules shown are the sample's. A save names the etag it replaces, as the server asks.
// Without ?mock-server the mock is a computer, which has none of this (R31).
export const SECURITY_MOCK = ON_SERVER && SIGN_IN_MOCK && search.includes('mock-security')
const POLICY_MOCK = search.includes('mock-policy') // mock/ee.ts's POLICY, read here too: it imports this module
const SECURITY_ACTIONS = ['approvals.decide', 'audit.read', 'policy.edit', 'policy.read', 'reports.all', 'reports.group', 'siem.edit', 'terms.manage']
const SAMPLE_KEYS = ['profile', 'ner', 'terms', 'policies', 'allow_values']
const SAMPLE_BROUGHT = ['pii', 'id_regions', 'include_private_ips', 'data', 'images'] // what max locks without naming it
const POLICY_CHANGED = 'not changed: the policy was changed since its text was read here: read it again, then change it'
const REMOVAL_BODY = `the body is {"etag": "<the etag of the policy it removes>"}, as the policy's text gave it, null when none is stored`
let saves = 0 // each save's etag differs, as a text's SHA-256 would
let storedPolicy: { text: string; setAt: number; etag: string } | null = POLICY_MOCK
  ? {
      text: 'latet_rules: 1\nexceptions: company\nprofile: max\nner: auto\nterms:\n  - {value: Project Falcon, type: PROJECT}\n  - {value: Northwind Traders, type: CLIENT}\npolicies: {IBAN: redact}\nallow_values: [acme.com]\n',
      setAt: NOW - 2 * DAY,
      etag: '3f1c9a7e52d04b18',
    }
  : null

type Base = Omit<Person, 'groups' | 'roles' | 'through'>
type KeyBase = Omit<ApiKey, 'controllers'>
interface Membership {
  group: string
  person: string
  source: GroupSource
}

const person = (id: string, name: string, source: Base['source'], seen: number, disabled = false): Base => {
  const email = `${name.toLowerCase().replace(' ', '.')}@acme.com`
  return {
    id,
    name,
    email,
    user_name: source === 'local' ? null : email,
    disabled,
    source,
    last_seen: NOW - seen,
    linked: source !== 'local', // those the provider made, or who signed in with it, have their company account linked
    claimable: false, // linked by SCIM or an administrator, and nobody signed in to it yet
    scim_managed: source === 'scim',
    disabled_by: disabled ? 'local' : null, // by an administrator here: SCIM does not enable them again
  }
}

let people: Base[] = [
  { ...person(ADMIN, 'Emily Carter', 'scim', 300), linked: !UNLINKED },
  person('u_danielokfr', 'Daniel Okafor', 'scim', 2 * 3600),
  { ...person('u_priyashahx', 'Priya Shah', 'scim', DAY), linked: false, claimable: true }, // SCIM made her: her first sign-in finds her
  person('u_laurachenx', 'Laura Chen', 'oidc', 3 * DAY),
  { ...person('u_tombeckerx', 'Tom Becker', 'local', 0), last_seen: null }, // added by hand, no computer connected yet
  person('u_samriverax', 'Sam Rivera', 'scim', 40 * DAY, true),
]
let groups: GroupHead[] = [
  { id: 'g_latetadmns', name: 'Latet admins', source: 'scim', external_id: '8c1d2e3f-a4b5-4c6d-9e7f-0a1b2c3d4e5f' },
  { id: 'g_securityxx', name: 'Security team', source: 'scim', external_id: '4b5a6c7d-8e9f-4a0b-8c1d-2e3f4a5b6c7d' },
  { id: 'g_paymentsld', name: 'Payments leads', source: 'oidc', external_id: null },
  { id: 'g_platformxx', name: 'Platform', source: 'local', external_id: null },
]
let memberships: Membership[] = [
  { group: 'g_latetadmns', person: ADMIN, source: 'scim' },
  { group: 'g_securityxx', person: 'u_danielokfr', source: 'scim' },
  { group: 'g_paymentsld', person: 'u_priyashahx', source: 'oidc' },
  { group: 'g_platformxx', person: 'u_danielokfr', source: 'local' },
  { group: 'g_platformxx', person: 'u_tombeckerx', source: 'local' },
]
let roles: Record<string, GrantedRole[]> = {
  g_latetadmns: ['admin'],
  g_securityxx: ['security'],
  g_paymentsld: ['group-lead'],
  u_laurachenx: ['auditor'],
}
let keys: KeyBase[] = [
  { id: 'u_billingcix', name: 'Billing CI', disabled: false, created: NOW - 30 * DAY, last_seen: NOW - 600 },
  { id: 'u_nightlyimp', name: 'Nightly import', disabled: false, created: NOW - 60 * DAY, last_seen: NOW - DAY },
]
let controllers: Record<string, string[]> = { u_billingcix: ['g_platformxx'], u_nightlyimp: ['u_laurachenx'] }
let sso: SsoSettings | null = {
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
let signInSaves = 0 // each save of the settings makes a new etag, as the digest of the section would
const SIGN_IN_CHANGED = 'not changed: the sign-in settings were changed since they were read here: read them again, then change them'
/** The etag of the sign-in settings (null: single sign-on off), which a change made from them names. */
const signInEtag = () => (sso === null ? null : `b5e2a9c47d10${String(signInSaves).padStart(4, '0')}`)
let secret = true
let scimTokens: ScimTokenInfo[] = [scimToken('s_3f9a1c7e2b', NOW - 12 * DAY, NOW + 78 * DAY, NOW - 900)]
let notice: ConnectedNotice | undefined = CONNECTED
  ? { until: 4821, computers: [{ label: 'emily-laptop', connected: NOW - 2 * 3600 }], codes: [{ made: NOW - 3 * 3600, by_administrator: true }], more: 0 }
  : undefined
// The computers connected to the administrator's account, listed for good (/me.edition.computers): her desktop, with a
// code she made; with &mock-connected, also the laptop the notice tells of, with a code an administrator made.
const COMPUTERS: ComputerOfYours[] = [
  ...(CONNECTED ? [{ label: 'emily-laptop', connected: NOW - 2 * 3600, last_used: NOW - 3600, by_administrator: true }] : []),
  { label: 'emily-desktop', connected: NOW - 40 * DAY, last_used: NOW - 300, by_administrator: false },
]

/** A SCIM token as the server lists it to an administrator: with its maker, here always Emily Carter. */
function scimToken(id: string, created: number, expires: number | null, lastUsed: number | null): ScimTokenInfo {
  const maker = { kind: 'person' as const, id: ADMIN, name: 'Emily Carter', admin: true, disabled: false }
  return { id, created, expires, last_used: lastUsed, made_by: ADMIN, maker }
}

function groupRef(id: string) {
  const found = groups.find((group) => group.id === id)
  return found ? { id: found.id, name: found.name } : null
}

function personView(base: Base): Person {
  const mine = memberships
    .filter((m) => m.person === base.id)
    .flatMap((m) => {
      const ref = groupRef(m.group)
      return ref ? [{ ...ref, source: m.source }] : []
    })
  const through = mine.flatMap((group) => (roles[group.id] ?? []).map((role) => ({ role, group: { id: group.id, name: group.name } })))
  return { ...base, groups: mine, roles: roles[base.id] ?? [], through }
}

function groupView(base: GroupHead): Group {
  const members = memberships
    .filter((m) => m.group === base.id)
    .flatMap((m) => {
      const found = people.find((p) => p.id === m.person)
      return found ? [{ id: found.id, name: found.name, source: m.source }] : []
    })
  return { ...base, members, roles: roles[base.id] ?? [] }
}

function controllerOf(subject: string): Controller | null {
  const found = people.find((p) => p.id === subject)
  if (found) return { kind: 'person', id: found.id, name: found.name }
  const ref = groupRef(subject)
  return ref ? { kind: 'group', ...ref } : null
}

function keyView(base: KeyBase): ApiKey {
  return { ...base, controllers: (controllers[base.id] ?? []).flatMap((s) => controllerOf(s) ?? []) }
}

function signInView(): SignInView {
  return {
    sso,
    etag: signInEtag(),
    company_changes: sso ? 'sso' : null,
    redirect_uri: `${SERVER}/_latet/sso/callback`,
    secret,
    secret_issuer_changed: false,
    scim: { url: `${SERVER}/scim/v2`, tokens: scimTokens },
  }
}

function findPerson(key: string): Base {
  const found = people.find((p) => p.id === key || p.email === key)
  if (!found) throw new ApiError(404, 'there is no such person on this server')
  return found
}

function findGroup(key: string): GroupHead {
  const found = groups.find((group) => group.id === key || group.name.toLowerCase() === key.toLowerCase())
  if (!found) throw new ApiError(404, 'there is no such group on this server')
  return found
}

function subjectOf(key: string): Controller {
  const group = groups.find((g) => g.id === key || g.name.toLowerCase() === key.toLowerCase())
  if (group) return { kind: 'group', id: group.id, name: group.name }
  const found = findPerson(key)
  return { kind: 'person', id: found.id, name: found.name }
}

const idOf = (prefix: string, name: string) => `${prefix}_${name.toLowerCase().replace(/[^a-z]/g, '').padEnd(10, 'x').slice(0, 10)}`
const refOf = (base: Base) => ({ id: base.id, name: base.name })

/** /me as a server of Latet Enterprise answers its administrator with ?mock-server&mock-sign-in (or an API key's
 * space, when one is open in this tab); as it was otherwise, and on the mock's computer (no ?mock-server: R31). */
export function withSignIn(me: Me): Me {
  const edition = me.edition
  if (!SIGN_IN_MOCK || edition?.name !== 'enterprise') return me
  const key = keys.find((k) => k.id === currentSpace()) ?? (search.includes('mock-server-key') ? keys[0] : undefined)
  const identity = key ? { id: key.id, name: key.name, kind: 'key' as const } : { id: ADMIN, name: 'Emily Carter', kind: 'person' as const }
  return {
    ...me,
    server: { url: SERVER, identity },
    session: { via: CODE_SESSION ? 'code' : 'sso' },
    edition: {
      ...edition,
      name: 'enterprise',
      features: ['locked-rules', 'approvals', 'reports', 'siem', 'term-sync', 'sso', 'roles'],
      // Company approvals (E5) too: an administrator decides on the API keys nobody else controls (mock/ee-approvals.ts).
      pages: key ? [] : ['license', 'people', 'sign-in', 'audit', 'company-approvals'],
      roles: key ? [] : ['admin', 'user'],
      actions: key ? [] : ['audit.read', 'keys.manage', 'license.manage', 'people.manage', 'sign-in.edit'],
      ...(notice && !key ? { connected: notice } : {}),
      ...(key ? {} : { linked: !UNLINKED, computers: COMPUTERS }), // a person's own; an API key has no account
    },
  }
}

/** /me for Daniel Okafor, of the security role (?mock-security): the Policy page with its editor. As it is otherwise,
 * and in an API key's space. */
export function withSecurity(me: Me): Me {
  const edition = me.edition
  if (!SECURITY_MOCK || edition?.name !== 'enterprise' || currentSpace() !== null || me.server?.identity?.kind === 'key') return me // ?mock-server (R31)
  const policy = storedPolicy && {
    sources: [{ source: 'server' as const, origin: SERVER, received: storedPolicy.setAt, notices: 0 }], // a count (E2's M6)
    problems: [],
  }
  return {
    ...me,
    server: { url: SERVER, identity: { id: 'u_danielokfr', name: 'Daniel Okafor', kind: 'person' as const } },
    // Company approvals (E5): the security role decides (`approvals.decide`), mock/ee-approvals.ts; Term sync (E6):
    // the security role's (`terms.manage`), mock/ee-terms.ts.
    edition: {
      ...edition,
      pages: ['policy', 'sign-in', 'reports', 'audit', 'siem', 'company-approvals', 'term-sync'],
      roles: ['security', 'user'],
      actions: SECURITY_ACTIONS,
      policy,
      linked: true,
    },
  }
}

/** GET /ee/policy while the security role sets the policy (?mock-security): E2's sample floor (`sample`) set on the
 * server when the mock's text was kept, whichever text it is, and none once it is removed, so that the floor, /me and
 * the editor agree; `view` as it is otherwise. */
export function asServerFloor(view: PolicyView, sample: PolicyView): PolicyView {
  if (!SECURITY_MOCK) return view
  const setAt = storedPolicy?.setAt
  if (setAt === undefined) return { floors: [], below: [], problems: [] }
  return { ...sample, floors: sample.floors.map((floor) => ({ ...floor, source: 'server' as const, origin: SERVER, received: setAt })) }
}

/** The routes of the People and groups and Sign-in pages; undefined for any other. */
export function routePeople(method: Method, path: string, body: unknown): unknown {
  if (!ON_SERVER) return undefined
  const p = new URL(path, 'http://mock').pathname
  const data = (body ?? {}) as Record<string, unknown>
  const text = (key: string) => String(data[key] ?? '').trim()
  const enabled = () => people.filter((x) => !x.disabled).length + keys.filter((k) => !k.disabled).length

  // A session `latet dashboard`'s code opened changes nothing that applies to everyone while the settings ask for the
  // company sign-in (they do, with single sign-on set up): as the server says it, before the body is read.
  if (CODE_SESSION && sso !== null && method !== 'GET' && p !== '/ee/notices/told') {
    throw new ApiError(403, CHANGES_BY_SSO, [], { error: CHANGES_BY_SSO, code: 'company-sign-in' })
  }

  if (method === 'GET' && p === '/ee/people') return { people: people.map(personView), enabled: enabled() }
  if (method === 'POST' && p === '/ee/people') {
    const id = idOf('u', text('name'))
    people = [...people, { ...person(id, text('name'), 'local', 0), email: text('email') || null, user_name: null, last_seen: null, linked: false }]
    return { person: { id, name: text('name') }, code: CODE, command: COMMAND }
  }
  const state = p.match(/^\/ee\/people\/([^/]+)\/(code|disable|enable)$/)
  if (method === 'POST' && state) {
    const found = findPerson(decodeURIComponent(state[1] ?? ''))
    if (state[2] === 'code') {
      if (found.disabled) throw new ApiError(409, 'this person is disabled: enable them first')
      if (found.last_seen !== null) throw new ApiError(409, CONNECTED_ALREADY)
      return { code: CODE, command: COMMAND, person: refOf(found) }
    }
    const disabled = state[2] === 'disable'
    const by: DisabledBy | null = disabled ? 'local' : null
    people = people.map((x) => (x.id === found.id ? { ...x, disabled, disabled_by: by } : x))
    return { ok: true, person: refOf(found) }
  }

  if (method === 'GET' && p === '/ee/groups') return { groups: groups.map(groupView) }
  if (method === 'POST' && p === '/ee/groups') {
    if (groups.some((group) => group.name.toLowerCase() === text('name').toLowerCase())) throw new ApiError(409, 'there is a group of this name already')
    const made: GroupHead = { id: idOf('g', text('name')), name: text('name'), source: 'local', external_id: null }
    groups = [...groups, made]
    return { group: made }
  }
  const members = p.match(/^\/ee\/groups\/([^/]+)\/members(?:\/([^/]+))?$/)
  if (members) {
    const group = findGroup(decodeURIComponent(members[1] ?? ''))
    const who = findPerson(method === 'POST' ? text('person') : decodeURIComponent(members[2] ?? ''))
    if (method === 'POST') {
      memberships = [...memberships.filter((m) => !(m.group === group.id && m.person === who.id && m.source === 'local')), { group: group.id, person: who.id, source: 'local' }]
      return { ok: true, person: refOf(who) }
    }
    if (!memberships.some((m) => m.group === group.id && m.person === who.id && m.source === 'local')) {
      throw new ApiError(409, 'this person is in this group through your identity provider: change their groups there')
    }
    memberships = memberships.filter((m) => !(m.group === group.id && m.person === who.id && m.source === 'local'))
    return { ok: true, person: refOf(who) }
  }
  const oneGroup = p.match(/^\/ee\/groups\/([^/]+)$/)
  if (method === 'PUT' && oneGroup) {
    const group = findGroup(decodeURIComponent(oneGroup[1] ?? ''))
    if (group.source !== 'local') throw new ApiError(409, 'this group comes from your identity provider: rename it there')
    const renamed = { ...group, name: text('name') }
    groups = groups.map((g) => (g.id === group.id ? renamed : g))
    return { group: renamed }
  }
  if (method === 'DELETE' && oneGroup) {
    const group = findGroup(decodeURIComponent(oneGroup[1] ?? ''))
    if (group.source === 'scim') throw new ApiError(409, 'this group comes from your identity provider through SCIM: it is removed there')
    groups = groups.filter((g) => g.id !== group.id)
    memberships = memberships.filter((m) => m.group !== group.id)
    return { ok: true }
  }

  if (method === 'POST' && p === '/ee/roles') {
    const subject = subjectOf(text('subject'))
    const role = text('role') as GrantedRole
    roles = { ...roles, [subject.id]: [...new Set([...(roles[subject.id] ?? []), role])] }
    return { roles: roles[subject.id], subject }
  }
  const revoked = p.match(/^\/ee\/roles\/([^/]+)\/([^/]+)$/)
  if (method === 'DELETE' && revoked) {
    const subject = subjectOf(decodeURIComponent(revoked[1] ?? ''))
    if (!(roles[subject.id] ?? []).includes(revoked[2] as GrantedRole)) throw new ApiError(404, 'this role was not given to this person or group')
    roles = { ...roles, [subject.id]: (roles[subject.id] ?? []).filter((role) => role !== revoked[2]) }
    return { roles: roles[subject.id], subject }
  }

  // Always the person's own keys (the dashboard never asks them through a key's space): the administrator manages all.
  if (method === 'GET' && p === '/ee/keys') return { keys: keys.map(keyView), manage: true }
  if (method === 'POST' && p === '/ee/keys') {
    const made: KeyBase = { id: 'u_newkeyxxxx', name: text('name'), disabled: false, created: Date.now() / 1000, last_seen: null }
    keys = [...keys, made]
    const secretOfKey = 'latet_key_EXAMPLEkeyOfTheMockOnly00000000000000'
    return { key: keyView(made), secret: secretOfKey, address: `${SERVER}/u/k4m2x9q7w3z8n5b6v1c0r2t4y6/` }
  }
  const keyAction = p.match(/^\/ee\/keys\/([^/]+)\/(rotate|disable|enable)$/)
  if (method === 'POST' && keyAction) {
    const id = decodeURIComponent(keyAction[1] ?? '')
    if (keyAction[2] === 'rotate') {
      return { secret: 'latet_key_EXAMPLErotatedKeyOfTheMock000000000000', address: `${SERVER}/u/p8r3m6x2k9w4z7q1v5b0n3c8t2/` }
    }
    keys = keys.map((k) => (k.id === id ? { ...k, disabled: keyAction[2] === 'disable' } : k))
    return { ok: true }
  }
  const control = p.match(/^\/ee\/keys\/([^/]+)\/controllers(?:\/([^/]+))?$/)
  if (control) {
    const id = decodeURIComponent(control[1] ?? '')
    const subject = subjectOf(method === 'POST' ? text('subject') : decodeURIComponent(control[2] ?? ''))
    const now = (controllers[id] ?? []).filter((s) => s !== subject.id)
    controllers = { ...controllers, [id]: method === 'POST' ? [...now, subject.id] : now }
    return { controllers: (controllers[id] ?? []).flatMap((s) => controllerOf(s) ?? []), subject }
  }

  if (method === 'GET' && p === '/ee/sign-in') return signInView()
  if (method === 'PUT' && p === '/ee/sign-in') {
    const asked = (data.sso ?? null) as (SsoSettings & { company_changes?: unknown }) | null
    if (asked?.company_changes !== undefined) {
      throw new ApiError(400, 'whether changes that apply to everyone take the company sign-in is set with `latet server sign-in set --company-changes`')
    }
    // A change may name the etag of the settings it was made from, null for none: refused when they changed meanwhile.
    if ('etag' in data && data.etag !== signInEtag()) throw new ApiError(409, SIGN_IN_CHANGED, [], { error: SIGN_IN_CHANGED, code: 'sign-in-changed' })
    if (asked === null && sso !== null) {
      throw new ApiError(409, 'while changes that apply to everyone take the company sign-in, single sign-on is turned off with `latet server sign-in off`')
    }
    sso = asked
    signInSaves += 1
    return signInView()
  }
  if (p === '/ee/sign-in/secret') {
    secret = method === 'PUT'
    return { ok: true }
  }
  if (method === 'POST' && p === '/ee/sign-in/test') {
    const issuer = (data.sso as SsoSettings | undefined)?.issuer ?? sso?.issuer ?? null
    return { ok: true, error: null, issuer, authorization_endpoint: `${issuer}/authorize`, token_endpoint: `${issuer}/token`, keys: 2 }
  }
  if (method === 'POST' && p === '/ee/sign-in/scim-tokens') {
    const made = scimToken('s_8d2e5b1f4a', Date.now() / 1000, Date.now() / 1000 + 90 * DAY, null)
    scimTokens = [...scimTokens, made]
    return { id: made.id, token: 'latet_scim_EXAMPLEtokenOfTheMockOnly0000000000', url: `${SERVER}/scim/v2`, expires: made.expires }
  }
  const token = p.match(/^\/ee\/sign-in\/scim-tokens\/([^/]+)$/)
  if (method === 'DELETE' && token) {
    scimTokens = scimTokens.filter((t) => t.id !== decodeURIComponent(token[1] ?? ''))
    return { ok: true }
  }
  if (method === 'POST' && p === '/ee/sign-in/code') return { code: CODE, command: COMMAND }
  if (method === 'POST' && p === '/ee/notices/told') {
    if (CODE_SESSION) throw new ApiError(403, TOLD_BY_SSO)
    const moved = notice !== undefined && data.until === notice.until
    if (moved) notice = undefined
    return { ok: true, told: moved }
  }
  // The policy's editor, for the security role (?mock-security); the server refuses it to anyone else.
  if (SECURITY_MOCK && method === 'GET' && p === '/ee/policy/text') {
    if (CODE_SESSION && sso !== null) {
      // The text holds the company's terms and values: while single sign-on asks for it, a code session reads it not.
      const error = "sign in with your company account to read the policy's text"
      throw new ApiError(403, error, [], { error, code: 'company-sign-in' })
    }
    const policy = storedPolicy && {
      etag: storedPolicy.etag,
      set_at: storedPolicy.setAt,
      set_by: 'u_danielokfr',
      keys: SAMPLE_KEYS,
      brought: SAMPLE_BROUGHT,
      exceptions: 'company' as const,
      notices: [],
      in_force: true,
      text: storedPolicy.text,
    }
    return { etag: storedPolicy?.etag ?? null, policy, problems: [] }
  }
  if (SECURITY_MOCK && method === 'PUT' && p === '/ee/policy') {
    const policyText = String(data.policy ?? '')
    const lines = policyText.split('\n')
    const open = lines.findIndex((line) => /\[[^\]]*$/.test(line))
    if (open >= 0) {
      // The server's words: where the mistake is, never what the line says.
      const column = (lines[open] ?? '').lastIndexOf('[') + 1
      throw new ApiError(400, `not changed: ${SERVER}: not valid YAML (line ${open + 1}, column ${column})`)
    }
    if (data.etag !== (storedPolicy?.etag ?? null)) throw new ApiError(409, POLICY_CHANGED, [], { error: POLICY_CHANGED, code: 'policy-changed' })
    saves += 1
    storedPolicy = { text: policyText, setAt: Date.now() / 1000, etag: `8d2e5b1f4a6c${String(saves).padStart(4, '0')}` }
    return { etag: storedPolicy.etag, keys: SAMPLE_KEYS, exceptions: 'company', notices: [], problems: [] }
  }
  if (SECURITY_MOCK && method === 'DELETE' && p === '/ee/policy') {
    // As a save does, a removal names the etag of the policy it removes: the server answers 400 without one.
    if (!('etag' in data) || (data.etag !== null && typeof data.etag !== 'string')) {
      throw new ApiError(400, REMOVAL_BODY, [], { error: REMOVAL_BODY })
    }
    if (data.etag !== (storedPolicy?.etag ?? null)) throw new ApiError(409, POLICY_CHANGED, [], { error: POLICY_CHANGED, code: 'policy-changed' })
    const removed = storedPolicy !== null
    storedPolicy = null
    return { removed }
  }
  return undefined
}
