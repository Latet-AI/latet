// The Enterprise Edition in mock mode. A computer runs the Community Edition: nothing Enterprise shows, as in the
// documentation's screenshots of the other pages. ?mock-server is a person's space on a Latet server, and there
// ?mock-license shows a valid license on the License page; ?mock-license=expiring|grace|paused|users-near|users-over|
// invalid|trial|trial-week|trial-day|trial-ended|server shows that case. The days are those of the screenshots' clock
// (2026-10-12); each trial runs 32 days. Every case is a server's. The cases that count users (users-near, users-over,
// server) show what a server's administrators see: a person on a server is told no count (`users_in_use` and
// `over_since` null), as the daemon answers. An administrator installs and removes the license there while it can
// manage (valid, ending soon, or in its grace), as the server lets them; any other case answers 403, and a removal that
// stops the policy in force (?mock-policy), or the synced terms being masked (&mock-synced-terms: the server keeps terms
// synced from the company's sources), or central approvals (?mock-routed), is asked again with `policy_stops`. With
// ?mock-routed the company's security team decides on the person's changes and texts (mock/approvals.ts).

import { ApiError, type Method } from '../api/client'
import type { ConfigView, Edition, IdentityView, LicenseSummary, PolicySummary } from '../api/types'
import type { PolicyView } from '../ee/lib/policy'
import type { LicenseDetails, LicenseStanding } from '../ee/types'
import { MOCK_DECIDERS, ROUTED } from './approvals'
import { routeCompanyApprovals } from './ee-approvals'
import { routeTermSync } from './ee-terms'
import { NOW } from './fixtures'
import { routePeople } from './people'

const FEATURES = ['locked-rules', 'approvals', 'reports', 'siem', 'term-sync', 'sso', 'roles']
const LICENSE: LicenseDetails = {
  id: 'L-2026-0042',
  licensee: 'Acme Corp',
  users: 300,
  features: FEATURES,
  issued: '2026-03-01',
  expires: '2027-02-28',
  trial: false,
}

const NONE: LicenseStanding = {
  state: 'none',
  license: null,
  users_in_use: null,
  days_left: null,
  over_since: null,
  pauses_on: null,
  reason: null,
  reason_code: null,
  notices: [],
  source: null,
}

function valid(standing: Partial<LicenseStanding> = {}, license: Partial<LicenseDetails> = {}): LicenseStanding {
  return { ...NONE, state: 'valid', license: { ...LICENSE, ...license }, days_left: 139, source: 'server', ...standing }
}

const CASES: Record<string, LicenseStanding> = {
  valid: valid(),
  expiring: valid({ state: 'expiring', days_left: 19, notices: ['expiring'] }, { expires: '2026-10-31' }),
  grace: valid({ state: 'grace', days_left: -11, pauses_on: '2026-11-01', notices: ['grace'] }, { expires: '2026-10-01' }),
  paused: valid({ state: 'paused', days_left: -42, notices: ['paused'] }, { expires: '2026-08-31' }),
  'users-near': valid({ users_in_use: 276, notices: ['users-near'] }),
  'users-over': valid({
    state: 'grace',
    users_in_use: 312,
    over_since: NOW - 5 * 86400,
    pauses_on: '2026-11-06',
    notices: ['users-over'],
  }),
  invalid: {
    ...NONE,
    state: 'invalid',
    reason: 'its signature does not match: the file was changed after it was made',
    reason_code: 'signature',
    notices: ['invalid'],
    source: 'server',
  },
  trial: valid({ days_left: 20 }, { id: 'L-2026-0051', issued: '2026-10-01', expires: '2026-11-01', trial: true }),
  'trial-week': valid(
    { state: 'expiring', days_left: 6, notices: ['trial-week'] },
    { id: 'L-2026-0051', issued: '2026-09-17', expires: '2026-10-18', trial: true },
  ),
  'trial-day': valid(
    { state: 'expiring', days_left: 1, notices: ['trial-day'] },
    { id: 'L-2026-0051', issued: '2026-09-12', expires: '2026-10-13', trial: true },
  ),
  'trial-ended': valid(
    { state: 'trial-ended', days_left: -2, notices: ['trial-ended'] },
    { id: 'L-2026-0051', issued: '2026-09-09', expires: '2026-10-10', trial: true },
  ),
  server: valid({ users_in_use: 214 }),
}

const FLAG = typeof window === 'undefined' ? null : new URLSearchParams(window.location.search).get('mock-license')
let standing: LicenseStanding = FLAG === null ? NONE : (CASES[FLAG || 'valid'] ?? valid())

function summaryOf(s: LicenseStanding): LicenseSummary | null {
  if (s.state === 'none') return null
  return {
    state: s.state,
    licensee: s.license?.licensee ?? null,
    users: s.license?.users ?? null,
    users_in_use: s.users_in_use,
    expires: s.license?.expires ?? null,
    days_left: s.days_left,
    trial: s.license?.trial ?? false,
    notices: s.notices,
    pauses_on: s.pauses_on,
  }
}

// ?mock-server&mock-policy: the server's policy is in force (the Policy page, the locks on Rules and on projects). A person is told
// what it locks and never a value: its terms and exceptions are counts, as the daemon answers.
export const POLICY = typeof window !== 'undefined' && window.location.search.includes('mock-policy')
// &mock-synced-terms: the server keeps terms synced from the company's sources (E6), which a license without term sync
// stops masking: its removal is asked again (R29).
const SYNCED_TERMS = typeof window !== 'undefined' && window.location.search.includes('mock-synced-terms')
const ORIGIN = 'https://latet.acme.lan'
const WRITTEN = 1791191520

export function policySummary(): PolicySummary | null {
  return POLICY ? { sources: [{ source: 'server', origin: ORIGIN, received: WRITTEN, notices: 0 }], problems: [] } : null
}

export function policyView(config: Pick<ConfigView, 'profile'>): PolicyView {
  return POLICY ? samplePolicyView(config) : { floors: [], below: [], problems: [] }
}

/** The sample policy's floor as a person on the server is told it, whatever the flags (the security role's mock keeps
 * the text it is given and shows this floor for it: mock/people.ts). */
export function samplePolicyView(config: Pick<ConfigView, 'profile'>): PolicyView {
  return {
    floors: [
      {
        source: 'server',
        origin: ORIGIN,
        received: WRITTEN,
        exceptions: 'company',
        rules: { latet_rules: 1, profile: 'max', ner: 'auto', terms: 2, policies: { IBAN: 'redact' }, allow_values: 1 },
        notices: 0,
      },
    ],
    below: config.profile === 'max' ? [] : [`profile: from max to ${config.profile}`],
    problems: [],
  }
}

/** A save the sample policy refuses, as the daemon answers it: a profile changed to one below max. */
export function policyRefusal(before: Pick<ConfigView, 'profile'>, next: Pick<ConfigView, 'profile'>): ApiError | null {
  if (!POLICY || next.profile === 'max' || next.profile === before.profile) return null
  const reasons = [`profile: from max to ${next.profile}`]
  const error = `Set by your company, not changed: ${reasons.join('; ')}`
  return new ApiError(409, error, [], { error, floor: reasons })
}

// The same flag as the mock's server: a person's space on a Latet server (mock/index.ts reads it too).
const SERVER = typeof window !== 'undefined' && window.location.search.includes('mock-server')

// ?mock-server&mock-provider-name: the company's identity provider names the person, whose name Rules then says is the
// provider's (`full_name_from: "provider"`); &mock-name-masked: their own name is in use, because their settings mask
// names, and the provider names them all the same, so a change of it is refused (`full_name_locked`). A computer's
// identity is its own whatever the flags say (R31).
const NAME_FROM_PROVIDER = SERVER && typeof window !== 'undefined' && window.location.search.includes('mock-provider-name')
const NAME_MASKED = SERVER && typeof window !== 'undefined' && window.location.search.includes('mock-name-masked')

/** The identity `GET /config` answers: as it is, or with the name the flags above give it. */
export function identityView(identity: IdentityView): IdentityView {
  if (NAME_MASKED) return { ...identity, full_name_from: null, full_name_locked: true }
  if (NAME_FROM_PROVIDER) return { ...identity, full_name: 'Emily Carter', full_name_from: 'provider', full_name_locked: true }
  return identity
}

/** `/me`'s edition: a computer, and the tutorial's sandbox, run the Community Edition; a server, the Enterprise one. */
export function mockEdition(tutorial: boolean): Edition {
  if (tutorial || !SERVER) return { name: 'community', license: null, features: [], pages: [], roles: [], actions: [] }
  return {
    name: 'enterprise',
    license: summaryOf(standing),
    features: standing.license?.features ?? [],
    pages: [...(standing.state === 'none' ? [] : ['license']), ...(POLICY ? ['policy'] : [])],
    roles: [],
    actions: [],
    policy: policySummary(),
    // Who decides on the person's changes and texts (E5): the security team with ?mock-routed, else the person.
    approvals: {
      changes: ROUTED ? 'security' : 'self',
      live_review: ROUTED ? 'security' : 'self',
      deciders: ROUTED ? [...MOCK_DECIDERS] : [],
      deciders_more: 0,
      text_deciders: ROUTED ? [...MOCK_DECIDERS] : [],
      text_deciders_more: 0,
    },
  }
}

const MANAGEABLE: LicenseStanding['state'][] = ['valid', 'expiring', 'grace']

/** Why the license in use cannot be changed from the dashboard, in the server's words and codes; null while it can. */
function changeRefusal(): ApiError | null {
  if (MANAGEABLE.includes(standing.state)) return null
  const ended = standing.license?.expires ?? ''
  const [error, code] =
    standing.state === 'trial-ended'
      ? [`the Latet Enterprise trial ended on ${ended}: install a license, or remove the trial's file to go back to the Community Edition; protection is unchanged`, 'trial-ended']
      : standing.state === 'paused'
        ? ['the Latet Enterprise license is paused (past the 30 days of grace): the Enterprise settings can be read, not changed, until a license that covers them is installed; protection is unchanged', 'not-manageable']
        : standing.state === 'invalid'
          ? ['the license file cannot be used (`latet server license show` says why): the Enterprise settings can be read, not changed, until it is replaced; protection is unchanged', 'not-manageable']
          : ['there is no Latet Enterprise license: the Enterprise settings can be read, not changed, until one is installed; protection is unchanged', 'not-manageable']
  return new ApiError(403, error, [], { error, code })
}

/** The license installed or removed from the dashboard: `PUT {license, policy_stops?}` and `DELETE {policy_stops?}`. */
function changeLicense(method: Method, body: unknown): LicenseStanding | (LicenseStanding & { removed: boolean }) {
  const refused = changeRefusal()
  if (refused) throw refused
  const data = (body ?? {}) as Record<string, unknown>
  if (method === 'PUT') {
    if (typeof data.license !== 'string' || data.license.trim() === '') {
      const error = 'the body is {"license": "<the license file\'s text>"}'
      throw new ApiError(400, error, [], { error })
    }
    // Any text installs the sample license, which lists every feature: a policy in force stays in force.
    standing = valid()
    return standing
  }
  // What stops with the removal, in the server's words (ee/api.py's stops_said): the policy, the synced terms, central
  // approvals while the security team decides (?mock-routed), or several; nothing changes until it is sent again with
  // `policy_stops`.
  const policy = POLICY && standing.license?.features.includes('locked-rules')
  const terms = SYNCED_TERMS && standing.license?.features.includes('term-sync')
  const approvals = ROUTED && standing.license?.features.includes('approvals')
  if ((policy || terms || approvals) && data.policy_stops !== true) {
    const stopping: { words: string; lacks: string }[] = [
      ...(policy ? [{ words: "the server's policy stops being in force", lacks: 'locked rules' }] : []),
      ...(terms ? [{ words: 'the synced terms stop being masked', lacks: 'term sync' }] : []),
      ...(approvals ? [{ words: 'central approvals stop', lacks: 'approvals' }] : []),
    ]
    const listed = (words: string[]) => (words.length < 2 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words.slice(-1).join('')}`)
    const lacks = stopping.map((each) => each.lacks)
    const left = lacks.length === 1 ? `no ${listed(lacks)}` : lacks.length === 2 ? `neither ${lacks.join(' nor ')}` : `none of ${listed(lacks)}`
    const said = listed(stopping.map((each) => each.words))
    const error = `not changed: ${said} with this change (the license left lists ${left}): send it again with "policy_stops": true to make it`
    const named = { ...(policy ? { policy: 'stops' } : {}), ...(terms ? { terms: 'stop' } : {}), ...(approvals ? { approvals: 'stop' } : {}) }
    throw new ApiError(409, error, [], { error, ...named })
  }
  const removed = standing.state !== 'none'
  standing = NONE
  return { ...standing, removed }
}

/** `/ee/…`, or undefined when the path is not one of its routes. */
export function routeEe(method: Method, path: string, body: unknown): unknown {
  // Company approvals (E5): the security role's, with its own answers to a session of `latet dashboard`'s code.
  if (path.startsWith('/ee/approvals')) return routeCompanyApprovals(method, path, body)
  // Term sync (E6): the security role's, with its own answers to a session of `latet dashboard`'s code.
  if (path.startsWith('/ee/term-sync')) return routeTermSync(method, path, body)
  const people = routePeople(method, path, body)
  if (people !== undefined) return people
  // A computer has no license to read or change, whatever ?mock-license says (R31): the Enterprise Edition is a server's.
  if (path === '/ee/license' && SERVER) {
    if (method === 'GET') return standing
    if (method === 'PUT' || method === 'DELETE') return changeLicense(method, body)
  }
  return undefined
}
