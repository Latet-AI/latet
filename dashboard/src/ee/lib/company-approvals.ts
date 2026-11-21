// The Company approvals page (Latet Enterprise): what waits for you to decide, and what was decided.

import { ApiError } from '../../api/client'
import type { Agent } from '../../api/types'
import { tk } from '../../i18n'
import { AGENT_LABEL } from '../../lib/labels'
import type { ApprovalRoute, CompanyBatchRow, CompanyChange, CompanyQueue, Decided, DecidedState } from '../types'

export type CompanyItem = CompanyChange | CompanyBatchRow

/** Everything waiting, oldest first. */
export function waitingItems(queue: CompanyQueue): CompanyItem[] {
  return [...queue.changes, ...queue.batches].sort((a, b) => a.created - b.created)
}

/** What waits for you to decide (the menu's count): your own requests wait for someone else; what waits past the list
 * (`more`) counts too, as the count the menu asks while the page is closed does. */
export function companyWaiting(queue: CompanyQueue): number {
  return waitingItems(queue).filter((item) => !item.mine).length + queue.more
}

/** What arrived since the queue was last seen (null: never seen, so nothing is new), never your own. */
export function arrivals(seen: ReadonlySet<string> | null, queue: CompanyQueue): CompanyItem[] {
  if (seen === null) return []
  return waitingItems(queue).filter((item) => !item.mine && !seen.has(item.id))
}

/** The queue without an item decided here (the next answer confirms it). */
export function withoutItem(queue: CompanyQueue, id: string): CompanyQueue {
  return { ...queue, changes: queue.changes.filter((c) => c.id !== id), batches: queue.batches.filter((b) => b.id !== id) }
}

/** How long until the queue is asked again while the page is open: often in sight, less in a background tab. */
export function pollDelay(hidden: boolean): number {
  return hidden ? 15000 : 4000
}

/** How long until the menu's count is asked again while the page is closed (R39): one small read, seldom. */
export function countDelay(hidden: boolean): number {
  return hidden ? 60000 : 30000
}

/** What the page is to this person (`enterpriseView`): not theirs, read only (a trial ended), or theirs to decide on. */
export type CompanyView = 'none' | 'read-only' | 'page'

/**
 * Which reads the app makes of what waits (R39): the queue while the page is open (`watched`), read only too; the
 * menu's count while it is closed and the person decides; a browser notification only while they decide; nothing for
 * someone the page is not for.
 */
export function companyReads(view: CompanyView, watched: boolean): { queue: boolean; count: boolean; notify: boolean } {
  if (view === 'none') return { queue: false, count: false, notify: false }
  const deciding = view === 'page'
  return { queue: watched, count: deciding && !watched, notify: deciding }
}

/** The menu's count: none unless the person decides now; the open page's queue's (`queue`), else the last count. */
export function menuCount(view: CompanyView, queue: CompanyQueue | null, counted: number): number {
  if (view !== 'page') return 0
  return queue ? companyWaiting(queue) : counted
}

/** A text as it was read, and as it is now: what it would leave as (each text masked, and where its values sit). */
interface Reading {
  version: string
  items: readonly { masked: string; replacements: readonly unknown[] }[]
}

/**
 * Whether a text read again is the one on screen: the same version (the server's digest of how it would leave), and
 * each text masked the same way. Otherwise it was masked again while the decider looked, and is shown anew before
 * anything is approved (the review of Task 10, I1).
 */
export function sameReading(shown: Reading, now: Reading): boolean {
  if (shown.version !== now.version || shown.items.length !== now.items.length) return false
  return shown.items.every((item, i) => {
    const other = now.items[i]
    return other !== undefined && item.masked === other.masked && JSON.stringify(item.replacements) === JSON.stringify(other.replacements)
  })
}

export const STATE_LABEL: Record<DecidedState, string> = {
  approved: tk('ee.companyApprovals.state.approved'),
  rejected: tk('ee.companyApprovals.state.rejected'),
  stale: tk('ee.companyApprovals.state.stale'),
  superseded: tk('ee.companyApprovals.state.superseded'),
  withdrawn: tk('ee.companyApprovals.state.withdrawn'),
  expired: tk('ee.companyApprovals.state.expired'),
  abandoned: tk('ee.companyApprovals.state.abandoned'),
  gone: tk('ee.companyApprovals.state.gone'),
  'approved-unapplied': tk('ee.companyApprovals.state.approvedUnapplied'),
  'approved-changed': tk('ee.companyApprovals.state.approvedChanged'),
}

export const ROUTE_LABEL: Record<ApprovalRoute, string> = {
  security: tk('ee.companyApprovals.route.security'),
  controllers: tk('ee.companyApprovals.route.controllers'),
}

type Translate = (key: string, options?: Record<string, unknown>) => string

const agentName = (t: Translate, agent: string): string => {
  const label = AGENT_LABEL[agent as Agent] as string | undefined
  return label ? t(label) : agent
}

/** What a decision was about, as the history says it: a change, or texts from an agent. */
export function decidedWhat(t: Translate, item: Decided): string {
  if (item.kind === 'change') return t('ee.companyApprovals.history.change')
  return t('ee.companyApprovals.history.batch', { count: item.items ?? 1, agent: item.agent ? agentName(t, item.agent) : '' })
}

/** The ends someone decides: one of them with no `by` was found ended in a queue, whoever decided it unheard. */
const DECIDED: readonly DecidedState[] = ['approved', 'rejected', 'stale', 'approved-unapplied', 'approved-changed']

/** Who decided, as the history says it: their name; nobody, for an end nobody decided (withdrawn by whoever asked,
 * expired, given up, gone, replaced); not recorded, for a decision the record found ended in a queue. */
export function decidedByWords(t: Translate, item: Pick<Decided, 'by' | 'state'>): string {
  if (item.by) return item.by.name
  return DECIDED.includes(item.state) ? t('ee.companyApprovals.history.unrecorded') : t('ee.companyApprovals.history.nobody')
}

/**
 * Where a change came from, as whoever decides on it reads it: words about the person (or the API key) who asked,
 * on a server, where no config.yaml is. What the server found as it applied their settings again (`file`) or made their
 * space (`start`) runs as they approved it until it is decided.
 */
export function companyOrigin(t: Translate, change: Pick<CompanyChange, 'source' | 'agent'>): string {
  switch (change.source) {
    case 'command':
      return change.agent ? t('approvals.change.source.command', { agent: agentName(t, change.agent) }) : t('approvals.change.source.commandNoAgent')
    case 'file':
      return t('ee.companyApprovals.source.file')
    case 'start':
      return t('ee.companyApprovals.source.start')
    default:
      return t('ee.companyApprovals.source.dashboard')
  }
}

/** How many reasons of a change the record keeps (the server's MOST_REASONS): past them, one more line counts the rest. */
const MOST_REASONS = 100
const MORE_REASONS = /^(\d+) more$/

/** What a change lowers, as the page lists it: Latet's own lines, as the server keeps them; past the hundred it keeps,
 * its count of the rest (its last line, "N more") in the reader's language. */
export function loosenedLines(t: Translate, lines: readonly string[]): string[] {
  const last = lines.length === MOST_REASONS + 1 ? MORE_REASONS.exec(lines[MOST_REASONS] ?? '') : null
  if (!last) return [...lines]
  return [...lines.slice(0, MOST_REASONS), t('ee.companyApprovals.changes.moreReasons', { count: Number(last[1]) })]
}

/** An answer of the Company approvals routes, by its code: its words, whether the item no longer waits for you (it
 * leaves the queue), the tone of its message, and whether /me is read again (`reread`: a trial ended meanwhile, and
 * the page turns read-only). */
export interface CompanyAnswer {
  key: string
  ended: boolean
  tone: 'info' | 'fail'
  reread?: true
}

// The codes the deciders' routes answer with (ee/approvals_api.py; E1's `trial-ended`): 403 yours (you asked for it),
// not-yours (not, or no longer, yours to decide); 409 what ended, the decision not taken, or a text masked again
// since it was read (changed: it still waits); 503 nothing decided, or a text not shown since its opening could not be
// written to the administration log (opening-unrecorded), try again; 500 an end the server does not expect.
// Each word names the person who asked as {{name}} where it needs to.
const ANSWERS: Record<string, CompanyAnswer> = {
  yours: { key: tk('ee.companyApprovals.answer.yours'), ended: false, tone: 'fail' },
  'not-yours': { key: tk('ee.companyApprovals.answer.notYours'), ended: true, tone: 'info' },
  gone: { key: tk('ee.companyApprovals.answer.gone'), ended: true, tone: 'info' },
  changed: { key: tk('ee.companyApprovals.answer.changed'), ended: false, tone: 'info' },
  stale: { key: tk('ee.companyApprovals.answer.stale'), ended: true, tone: 'info' },
  'approved-unapplied': { key: tk('ee.companyApprovals.answer.approvedUnapplied'), ended: true, tone: 'info' },
  'below-floor': { key: tk('ee.companyApprovals.answer.belowFloor'), ended: true, tone: 'info' },
  unrecorded: { key: tk('ee.companyApprovals.answer.unrecorded'), ended: false, tone: 'fail' },
  'opening-unrecorded': { key: tk('ee.companyApprovals.answer.openingUnrecorded'), ended: false, tone: 'fail' },
  unreached: { key: tk('ee.companyApprovals.answer.unreached'), ended: false, tone: 'fail' },
  unkept: { key: tk('ee.companyApprovals.answer.unkept'), ended: false, tone: 'fail' },
  'floor-unavailable': { key: tk('ee.companyApprovals.answer.floorUnavailable'), ended: false, tone: 'fail' },
  'account-unavailable': { key: tk('ee.companyApprovals.answer.accountUnavailable'), ended: false, tone: 'fail' },
  unexpected: { key: tk('ee.companyApprovals.answer.unexpected'), ended: true, tone: 'fail' },
  'trial-ended': { key: tk('ee.companyApprovals.answer.trialEnded'), ended: false, tone: 'fail', reread: true },
}

const bodyOf = (error: unknown): Record<string, unknown> =>
  error instanceof ApiError && error.body !== null && typeof error.body === 'object' ? (error.body as Record<string, unknown>) : {}

/** How the page words an answer of the Company approvals routes; null for one it leaves to other words: the company
 * sign-in (`companySignInNeeded`, said with the way in), a mask refused (422, the server's words on the mask), and an
 * error with no code it knows. */
export function companyAnswer(error: unknown): CompanyAnswer | null {
  const code = bodyOf(error).code
  return typeof code === 'string' ? (ANSWERS[code] ?? null) : null
}

/** The reasons a change approved below the company's floor was rejected for (409 `below-floor`): Latet's own lines. */
export function floorReasons(error: unknown): string[] {
  const floor = bodyOf(error).floor
  return Array.isArray(floor) ? floor.filter((reason): reason is string => typeof reason === 'string') : []
}
