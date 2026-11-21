// Company approvals in mock mode (Latet Enterprise): a change and a text waiting for the security team, and what was
// decided lately. The mock's own person, Daniel Okafor, holds the security role (mock/people.ts, withSecurity, behind
// ?mock-server&mock-sign-in&mock-security); Emily Carter and Paul Ferris ask. &mock-own-change adds a change of
// Daniel's own, waiting for Mark Ellis: he may withdraw it, never approve it. The answers are the server's, with their
// codes (ee/approvals_api.py): a text and every decision on one take the company's sign-in [D6], a decision on a change
// takes it while single sign-on is set up (decision (c): &mock-code-session refuses both), and once a trial ended
// (&mock-license=trial-ended) nothing is decided and no text is read; the lists and the history answer in every case.
// A text read says its version, which an approval names; &mock-masked-again masks the text again once it was first
// read (here leaving the address in clear), so that the page's read before approving finds it changed.
// ?mock-server&mock-sign-in without &mock-security is Emily Carter, an administrator: the page is hers too, as it is every
// admin's on a server (the API keys nobody else controls), with nothing waiting for her and her own requests decided
// lately.

import { ApiError, type Method } from '../api/client'
import type { LiveItem } from '../api/types'
import type { Approver, Asker, CompanyChange, CompanyText, Decided } from '../ee/types'
import { MOCK_DECIDERS } from './approvals'
import { CC_SESSION } from './fixtures'
import { SECURITY_MOCK, SIGN_IN_MOCK } from './people'
import { mockMask } from './playground'

const search = typeof window === 'undefined' ? '' : window.location.search
const CODE_SESSION = search.includes('mock-code-session') // the session `latet dashboard`'s code opened
const TRIAL_ENDED = search.includes('mock-license=trial-ended')
const OWN_CHANGE = search.includes('mock-own-change')
const MASKED_AGAIN = search.includes('mock-masked-again')
const ADMIN_MOCK = search.includes('mock-server') && SIGN_IN_MOCK && !SECURITY_MOCK // Emily Carter, an administrator

const DANIEL: Approver = { id: 'u_danielokfr', name: 'Daniel Okafor' }
const DANIEL_ASKS: Asker = { ...DANIEL, kind: 'person' }
const EMILY: Asker = { id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' }
const PAUL: Asker = { id: 'u_paulferris', name: 'Paul Ferris', kind: 'person' }
const MARK: Approver = { id: 'u_markellisx', name: 'Mark Ellis' }
const BILLING: Asker = { id: 'u_billingcix', name: 'Billing CI', kind: 'key' }
const TEXT = 'Send the overdue notice for invoice 2026/118 to Laura Bennett, laura.bennett@whitfield.co.uk, before Friday.'

// The server's words (ee/approvals_api.py, ee/routes.py), which a refusal here repeats with its code.
const GONE = 'it no longer waits: someone else decided, or it was withdrawn'
const YOURS = 'you asked for it: someone else decides on it'
const TEXT_SIGN_IN = "sign in with your company account to open or decide on what someone else's agent sends"
const CHANGES_BY_SSO = 'sign in with your company account to change what applies to everyone'
const TRIAL = 'the Latet Enterprise trial ended on 2026-10-10: install a license, or remove the trial\'s file to go back to the Community Edition; protection is unchanged'
const CHANGED = 'it was masked again since you read it: nothing was decided; read it again, then decide'
const UNVERSIONED = 'an approval of a text names the version of it you read (`version`, as reading it gives it)'

const refused = (status: number, error: string, code: string) => new ApiError(status, error, [], { error, code })

function item(text: string): LiveItem {
  const result = mockMask(text, [], [])
  const at = [...text.slice(0, text.indexOf('Laura Bennett'))].length
  return {
    kind: 'user_text',
    path: '$.messages[22].content[0].text',
    tool: null,
    original: text,
    masked: result.masked,
    replacements: result.replacements,
    suggestions: [{ start: at, end: at + 'Laura Bennett'.length, category: 'person' }],
    failed: null,
  }
}

const now = Date.now() / 1000
let changes: CompanyChange[] = [
  {
    id: 'cc_mock1',
    identity: EMILY,
    group: 'Payments leads',
    route: 'security',
    created: now - 340,
    mine: false,
    source: 'dashboard',
    agent: null,
    loosened: ['values never to mask: 1 added', 'the Live Reviewer: turned off'],
    keys: ['allow_values', 'live_review'],
  },
  ...(OWN_CHANGE
    ? [
        {
          id: 'cc_mock2',
          identity: DANIEL_ASKS,
          group: 'Platform',
          route: 'security',
          created: now - 140,
          mine: true,
          source: 'command',
          agent: 'claude-code',
          loosened: ['profile: from max to balanced'],
          keys: ['profile'],
        } satisfies CompanyChange,
      ]
    : []),
]
let texts: CompanyText[] = [
  {
    id: 'cb_mock1',
    session: CC_SESSION,
    project: null,
    agent: 'claude-code',
    created: now - 75,
    expires: null,
    items: [item(TEXT)],
    decides: 'security',
    deciders: [...MOCK_DECIDERS],
    identity: PAUL,
    group: 'Platform',
    route: 'security',
    mine: false,
    version: '3b7e91c04d2a58f6',
  },
]
const maskedAgain = new Set<string>() // the texts &mock-masked-again masked again, once each
let history: Decided[] = [
  { id: 'cw_mock0', kind: 'change', route: 'security', identity: EMILY, created: now - 3000, decided: now - 2900, state: 'withdrawn', by: null, source: 'dashboard', loosened: ['terms: 1 value no longer masked'], keys: ['terms'] },
  { id: 'cb_mock0', kind: 'batch', route: 'security', identity: EMILY, created: now - 5300, decided: now - 5220, state: 'rejected', by: MARK, agent: 'codex', kinds: ['tool_result'], items: 1 },
  { id: 'ck_mock0', kind: 'change', route: 'security', identity: BILLING, created: now - 7400, decided: now - 7000, state: 'approved', by: MARK, source: 'command', loosened: ['profile: from max to balanced'], keys: ['profile'] },
]

/** What waits for Daniel to decide: his own requests left out. */
const waiting = () => changes.filter((c) => !c.mine).length + texts.filter((b) => !b.mine).length

/** `/ee/approvals…` for the administrator (?mock-server&mock-sign-in): no key that nobody else controls has a change
 * waiting, so nothing waits for her; what was decided lately holds her own requests. */
function routeForAdmin(method: Method, p: string, body: unknown): unknown {
  if (method === 'GET' && p === '/ee/approvals') return { changes: [], batches: [], more: 0 }
  if (method === 'GET' && p === '/ee/approvals/count') return { waiting: 0 }
  if (method === 'GET' && p === '/ee/approvals/history') return { items: structuredClone(history.filter((row) => row.identity.id === EMILY.id)) }
  if (method === 'GET' && /^\/ee\/approvals\/batches\/[^/]+$/.test(p)) throw refused(409, GONE, 'gone')
  if (method !== 'POST' || !/^\/ee\/approvals\/(changes|batches)\/[^/]+$/.test(p)) return undefined
  const { action } = (body ?? {}) as { action?: string }
  if (action !== 'approve' && action !== 'reject') throw new ApiError(400, 'action is approve or reject')
  throw refused(409, GONE, 'gone')
}

/** `/ee/approvals…` in mock mode for the security role (?mock-server&mock-sign-in&mock-security), and for the
 * administrator (routeForAdmin); undefined for any other path, and on the mock's computer (R31). */
export function routeCompanyApprovals(method: Method, path: string, body: unknown): unknown {
  if (!path.startsWith('/ee/approvals')) return undefined
  const p = new URL(path, 'http://mock').pathname
  if (ADMIN_MOCK) return routeForAdmin(method, p, body)
  if (!SECURITY_MOCK) return undefined
  if (method === 'GET' && p === '/ee/approvals') {
    const rows = texts.map(({ id, agent, items, identity, group, route, created, mine }) => ({
      id, agent, kinds: ['user_text'], items: items.length, identity, group, route, created, mine,
    }))
    return structuredClone({ changes, batches: rows, more: 0 })
  }
  if (method === 'GET' && p === '/ee/approvals/count') return { waiting: waiting() }
  if (method === 'GET' && p === '/ee/approvals/history') return { items: structuredClone(history) }
  const opened = p.match(/^\/ee\/approvals\/batches\/([^/]+)$/)
  if (method === 'GET' && opened) {
    if (CODE_SESSION) throw refused(403, TEXT_SIGN_IN, 'company-sign-in') // [D6], before anything is read
    if (TRIAL_ENDED) throw refused(403, TRIAL, 'trial-ended') // read to decide on it (R61)
    const text = texts.find((b) => b.id === decodeURIComponent(opened[1] ?? ''))
    if (!text) throw refused(409, GONE, 'gone')
    const read = structuredClone(text)
    if (MASKED_AGAIN && !maskedAgain.has(text.id)) {
      // Its previews made anew after this first read, as a space does when the person's rules change: nothing masked.
      maskedAgain.add(text.id)
      text.items = text.items.map((shown) => ({ ...shown, masked: shown.original, replacements: [] }))
      text.version = '9c04a1e7b25f3d68'
    }
    return read
  }
  const match = p.match(/^\/ee\/approvals\/(changes|batches)\/([^/]+)$/)
  if (method !== 'POST' || !match) return undefined
  const { action } = (body ?? {}) as { action?: string }
  if (action !== 'approve' && action !== 'reject') throw new ApiError(400, 'action is approve or reject')
  // A decision on a text is reading it [D6]; on a change, it applies to everyone (decision (c): single sign-on is set up).
  if (CODE_SESSION) throw match[1] === 'batches' ? refused(403, TEXT_SIGN_IN, 'company-sign-in') : refused(403, CHANGES_BY_SSO, 'company-sign-in')
  if (TRIAL_ENDED) throw refused(403, TRIAL, 'trial-ended')
  const id = decodeURIComponent(match[2] ?? '')
  const decided = Date.now() / 1000
  if (match[1] === 'changes') {
    const change = changes.find((c) => c.id === id)
    if (!change) throw refused(409, GONE, 'gone')
    if (change.mine && action === 'approve') throw refused(403, YOURS, 'yours')
    changes = changes.filter((c) => c !== change)
    const { identity, route, created, source, loosened, keys } = change
    // Whoever asked withdraws it: nobody decided (`by` null).
    const [state, by] = change.mine ? (['withdrawn', null] as const) : action === 'approve' ? (['approved', DANIEL] as const) : (['rejected', DANIEL] as const)
    history = [{ id, kind: 'change', route, identity, created, decided, state, by, source, loosened, keys }, ...history]
    return { ok: true, state: action === 'approve' ? 'approved' : 'rejected' }
  }
  const text = texts.find((b) => b.id === id)
  if (!text) throw refused(409, GONE, 'gone')
  if (text.mine && action === 'approve') throw refused(403, YOURS, 'yours')
  if (action === 'approve') {
    // Approved only as it was read (the review of Task 10, I1): its version named, and still its version.
    const { version } = body as { version?: unknown }
    if (typeof version !== 'string' || !version) throw new ApiError(400, UNVERSIONED, [], { error: UNVERSIONED })
    if (version !== text.version) throw refused(409, CHANGED, 'changed')
  }
  texts = texts.filter((b) => b !== text)
  const { identity, route, created, agent, items } = text
  const [state, by] = text.mine ? (['withdrawn', null] as const) : action === 'approve' ? (['approved', DANIEL] as const) : (['rejected', DANIEL] as const)
  history = [{ id, kind: 'batch', route, identity, created, decided, state, by, agent, kinds: ['user_text'], items: items.length }, ...history]
  return { ok: true, state: action === 'approve' ? 'approved' : 'rejected' }
}
