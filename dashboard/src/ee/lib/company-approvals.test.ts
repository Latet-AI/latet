import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import type { CompanyBatchRow, CompanyChange, CompanyQueue, Decided, DecidedState } from '../types'
import {
  arrivals,
  companyAnswer,
  companyOrigin,
  companyReads,
  companyWaiting,
  countDelay,
  decidedByWords,
  decidedWhat,
  floorReasons,
  loosenedLines,
  menuCount,
  pollDelay,
  ROUTE_LABEL,
  sameReading,
  STATE_LABEL,
  waitingItems,
  withoutItem,
} from './company-approvals'

const emily = { id: 'u_emilycarte', name: 'Emily Carter', kind: 'person' as const }
const change = (id: string, created: number, mine = false): CompanyChange => ({
  id,
  identity: emily,
  group: null,
  route: 'security',
  created,
  mine,
  source: 'command',
  agent: 'claude-code',
  loosened: ['values never to mask: 1 added'],
  keys: ['allow_values'],
})
const batch = (id: string, created: number, mine = false): CompanyBatchRow => ({
  id,
  agent: 'claude-code',
  kinds: ['user_text'],
  items: 1,
  identity: emily,
  group: null,
  route: 'security',
  created,
  mine,
})
const queue = (changes: CompanyChange[], batches: CompanyBatchRow[]): CompanyQueue => ({ changes, batches, more: 0 })
const t = (key: string, options?: Record<string, unknown>) => (options ? `${key} ${JSON.stringify(options)}` : key)

describe('the queue of what you decide', () => {
  it('lists everything oldest first, and counts what you decide on, not your own requests', () => {
    const q = queue([change('c2', 30), change('c1', 10, true)], [batch('b1', 20)])
    expect(waitingItems(q).map((item) => item.id)).toEqual(['c1', 'b1', 'c2'])
    expect(companyWaiting(q)).toBe(2)
  })

  it('counts what waits for you past the list as well, so the menu says the same with the page open or closed', () => {
    const q = { ...queue([change('c1', 10), change('c2', 20, true)], []), more: 7 }
    expect(companyWaiting(q)).toBe(8)
  })

  it('finds what arrived since it was last seen, nothing at first, never your own', () => {
    const q = queue([change('c1', 10), change('c3', 40, true)], [batch('b2', 30)])
    expect(arrivals(null, q)).toEqual([])
    expect(arrivals(new Set(['c1']), q).map((item) => item.id)).toEqual(['b2'])
  })

  it('drops an item decided here', () => {
    const q = withoutItem(queue([change('c1', 10)], [batch('b1', 20)]), 'b1')
    expect(q.batches).toEqual([])
    expect(q.more).toBe(0)
    expect(q.changes.map((c) => c.id)).toEqual(['c1'])
  })

  it('asks often while in sight, less in a background tab', () => {
    expect(pollDelay(false)).toBe(4000)
    expect(pollDelay(true)).toBe(15000)
  })

  it('counts what waits seldom while the page is closed', () => {
    expect(countDelay(false)).toBe(30000)
    expect(countDelay(true)).toBe(60000)
  })

  it('reads the queue while the page is open, the count while it is closed, and nothing for who decides nothing', () => {
    expect(companyReads('none', true)).toEqual({ queue: false, count: false, notify: false })
    expect(companyReads('page', true)).toEqual({ queue: true, count: false, notify: true })
    expect(companyReads('page', false)).toEqual({ queue: false, count: true, notify: true })
    // Once a trial ended nobody decides: the page reads what waits, and nothing counts or notifies.
    expect(companyReads('read-only', true)).toEqual({ queue: true, count: false, notify: false })
    expect(companyReads('read-only', false)).toEqual({ queue: false, count: false, notify: false })
  })

  it("counts in the menu the open page's queue, else the last count, and nothing once nobody decides", () => {
    const q = queue([change('c1', 10), change('c2', 20, true)], [batch('b1', 30)])
    expect(menuCount('page', q, 7)).toBe(2)
    expect(menuCount('page', null, 7)).toBe(7)
    expect(menuCount('read-only', q, 7)).toBe(0)
    expect(menuCount('none', null, 7)).toBe(0)
  })
})

describe('a text, as it was read and as it is now', () => {
  const item = (masked: string, replacements: unknown[] = [{ start: 0, end: 4 }]) =>
    ({ masked, replacements, original: 'Ada Byron', kind: 'user_text' }) as never
  const text = (version: string, ...items: never[]) => ({ version, items })

  it('is the same reading only while it would leave as it was read', () => {
    expect(sameReading(text('v1', item('<P1>')), text('v1', item('<P1>')))).toBe(true)
    expect(sameReading(text('v1', item('<P1>')), text('v2', item('<P1>')))).toBe(false) // masked again on the server
    expect(sameReading(text('v1', item('<P1>')), text('v1', item('Ada Byron')))).toBe(false)
    expect(sameReading(text('v1', item('<P1>')), text('v1', item('<P1>', [])))).toBe(false)
    expect(sameReading(text('v1', item('<P1>')), text('v1', item('<P1>'), item('<P2>')))).toBe(false)
  })
})

describe('a change, as whoever decides reads it', () => {
  it("says where it came from in words about the person who asked, never the reader's own settings", () => {
    expect(companyOrigin(t, { source: 'command', agent: 'codex' })).toBe('approvals.change.source.command {"agent":"agents.codex"}')
    expect(companyOrigin(t, { source: 'command', agent: 'hermes' })).toBe('approvals.change.source.command {"agent":"hermes"}')
    expect(companyOrigin(t, { source: 'command', agent: null })).toBe('approvals.change.source.commandNoAgent')
    expect(companyOrigin(t, { source: 'dashboard', agent: null })).toBe('ee.companyApprovals.source.dashboard')
    expect(companyOrigin(t, { source: 'file', agent: null })).toBe('ee.companyApprovals.source.file')
    expect(companyOrigin(t, { source: 'start', agent: null })).toBe('ee.companyApprovals.source.start')
  })

  it("counts the reasons past the hundred the record keeps in the reader's language", () => {
    const hundred = Array.from({ length: 100 }, (_, i) => `terms: ${i + 1} value no longer masked`)
    expect(loosenedLines(t, ['values never to mask: 1 added'])).toEqual(['values never to mask: 1 added'])
    expect(loosenedLines(t, [...hundred, '37 more'])).toEqual([...hundred, 'ee.companyApprovals.changes.moreReasons {"count":37}'])
    // Below the record's bound, a line is Latet's own reason, whatever it says.
    expect(loosenedLines(t, ['terms: 1 value no longer masked', '2 more'])).toEqual(['terms: 1 value no longer masked', '2 more'])
  })
})

describe('what the Company approvals routes answer', () => {
  const refused = (status: number, code?: string, extra: Record<string, unknown> = {}) =>
    new ApiError(status, 'the server words it in English', [], code === undefined ? { error: 'x', ...extra } : { error: 'x', code, ...extra })

  it('words every refusal and every decision not taken by its code, and says when the item no longer waits', () => {
    const cases: [number, string, string, boolean, 'info' | 'fail'][] = [
      [403, 'yours', 'yours', false, 'fail'],
      [403, 'not-yours', 'notYours', true, 'info'],
      [409, 'gone', 'gone', true, 'info'],
      [409, 'changed', 'changed', false, 'info'], // masked again since it was read: shown anew, still waiting
      [409, 'stale', 'stale', true, 'info'],
      [409, 'approved-unapplied', 'approvedUnapplied', true, 'info'],
      [409, 'below-floor', 'belowFloor', true, 'info'],
      [503, 'unrecorded', 'unrecorded', false, 'fail'],
      [503, 'opening-unrecorded', 'openingUnrecorded', false, 'fail'], // a text opened: nothing shown
      [503, 'unreached', 'unreached', false, 'fail'],
      [503, 'unkept', 'unkept', false, 'fail'],
      [503, 'floor-unavailable', 'floorUnavailable', false, 'fail'],
      [503, 'account-unavailable', 'accountUnavailable', false, 'fail'],
      [500, 'unexpected', 'unexpected', true, 'fail'],
      [403, 'trial-ended', 'trialEnded', false, 'fail'],
    ]
    for (const [status, code, key, ended, tone] of cases) {
      // A trial that ended meanwhile: /me is read again, and the page turns read-only.
      const reread = code === 'trial-ended' ? { reread: true } : {}
      expect(companyAnswer(refused(status, code)), code).toEqual({ key: `ee.companyApprovals.answer.${key}`, ended, tone, ...reread })
    }
  })

  it('leaves the company sign-in, a mask refused and anything else to their own words', () => {
    expect(companyAnswer(refused(403, 'company-sign-in'))).toBeNull() // CompanySignInWay says it, with the way in
    expect(companyAnswer(refused(422))).toBeNull() // the mask's own problem, in the server's words
    expect(companyAnswer(refused(409, 'some-new-code'))).toBeNull()
    expect(companyAnswer(new Error('offline'))).toBeNull()
    expect(companyAnswer(undefined)).toBeNull()
  })

  it("reads the company floor's reasons from a change approved below it", () => {
    expect(floorReasons(refused(409, 'below-floor', { floor: ['profile: from max to balanced', 3] }))).toEqual(['profile: from max to balanced'])
    expect(floorReasons(refused(409, 'gone'))).toEqual([])
    expect(floorReasons(new Error('x'))).toEqual([])
  })
})

describe('what was decided', () => {
  const decided = (kind: 'change' | 'batch', extra: Partial<Decided> = {}): Decided => ({
    id: 'd1',
    kind,
    route: 'security',
    identity: emily,
    created: 1,
    decided: 2,
    state: 'approved',
    by: { id: 'u_markellisa', name: 'Mark Ellis' },
    ...extra,
  })

  it('says what each decision was about', () => {
    expect(decidedWhat(t, decided('change'))).toBe('ee.companyApprovals.history.change')
    expect(decidedWhat(t, decided('batch', { agent: 'codex', items: 2 }))).toBe(
      'ee.companyApprovals.history.batch {"count":2,"agent":"agents.codex"}',
    )
  })

  it('has a label for every way it ended and every route', () => {
    const states: DecidedState[] = ['approved', 'rejected', 'stale', 'superseded', 'withdrawn', 'expired', 'abandoned', 'gone', 'approved-unapplied', 'approved-changed']
    for (const state of states) expect(STATE_LABEL[state]).toMatch(/^ee\.companyApprovals\.state\./)
    expect(ROUTE_LABEL.controllers).toBe('ee.companyApprovals.route.controllers')
  })

  it('names who decided; nobody for an end nobody decided; not recorded for a decision found ended in a queue', () => {
    expect(decidedByWords(t, decided('change'))).toBe('Mark Ellis')
    for (const state of ['withdrawn', 'expired', 'abandoned', 'gone', 'superseded'] as const) {
      expect(decidedByWords(t, decided('change', { state, by: null })), state).toBe('ee.companyApprovals.history.nobody')
    }
    for (const state of ['approved', 'rejected', 'stale', 'approved-unapplied', 'approved-changed'] as const) {
      expect(decidedByWords(t, decided('change', { state, by: null })), state).toBe('ee.companyApprovals.history.unrecorded')
    }
  })
})
