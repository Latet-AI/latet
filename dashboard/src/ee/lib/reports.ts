// The Reports page's logic (Enterprise): the period it asks for, the trend against the period before, and the words
// for the codes the server answers with.

import { ApiError } from '../../api/client'
import { tk } from '../../i18n'
import type { ApproverResidual, Paused, ReportPeople, ReportQuery, ReportResidual } from '../types'

export const PRESETS = ['7', '30', '90'] as const
export type Preset = (typeof PRESETS)[number] | 'custom'
export const FILTERS = ['group', 'project', 'agent', 'category'] as const
export type Filter = (typeof FILTERS)[number]
export const MAX_DAYS = 366

const pad = (n: number) => String(n).padStart(2, '0')

/** A day as the server writes it (YYYY-MM-DD), in local time. */
export function isoDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** The last `preset` days, today included. */
export function presetRange(preset: Exclude<Preset, 'custom'>, today: Date): { from: string; to: string } {
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - Number(preset) + 1)
  return { from: isoDate(start), to: isoDate(today) }
}

export type RangeProblem = 'format' | 'order' | 'length'

/** What is wrong with two dates typed in, as the server would refuse them; null: nothing. */
export function rangeProblem(from: string, to: string): RangeProblem | null {
  const day = /^[2][0-9]{3}-[0-9]{2}-[0-9]{2}$/
  const start = Date.parse(`${from}T00:00:00Z`)
  const end = Date.parse(`${to}T00:00:00Z`)
  if (!day.test(from) || !day.test(to) || Number.isNaN(start) || Number.isNaN(end)) return 'format'
  if (new Date(start).toISOString().slice(0, 10) !== from || new Date(end).toISOString().slice(0, 10) !== to) return 'format'
  if (start > end) return 'order'
  return (end - start) / 86_400_000 + 1 > MAX_DAYS ? 'length' : null
}

export const RANGE_PROBLEM: Record<RangeProblem, string> = {
  format: tk('ee.reports.range.format'),
  order: tk('ee.reports.range.order'),
  length: tk('ee.reports.range.length'),
}

/** The query of a report: its period, and the filters that are set. */
export function reportQuery(range: { from: string; to: string }, filters: Partial<Record<Filter, string | null>>): ReportQuery {
  const q: ReportQuery = { from: range.from, to: range.to }
  for (const name of FILTERS) {
    const value = filters[name]
    if (value) q[name] = value
  }
  return q
}

export interface Trend {
  direction: 'up' | 'down' | 'same' | 'new'
  /** The change as a share of the period before; null when there was nothing before. */
  ratio: number | null
}

/** How a total moved from the period before. */
export function trendOf(now: number, before: number): Trend {
  if (before === 0) return now === 0 ? { direction: 'same', ratio: 0 } : { direction: 'new', ratio: null }
  if (now === before) return { direction: 'same', ratio: 0 }
  return { direction: now > before ? 'up' : 'down', ratio: Math.abs(now - before) / before }
}

const OTHER_REASON = tk('ee.reports.reasons.other')
const REASONS: Record<string, string> = {
  leak: tk('ee.reports.reasons.leak'),
  history: tk('ee.reports.reasons.history'),
  'auto-mode-review': tk('ee.reports.reasons.autoModeReview'),
  unread: tk('ee.reports.reasons.unread'),
  'unread-request': tk('ee.reports.reasons.unreadRequest'),
  'too-large': tk('ee.reports.reasons.tooLarge'),
  unreadable: tk('ee.reports.reasons.unreadable'),
  'not-messages': tk('ee.reports.reasons.notMessages'),
  'masking-failed': tk('ee.reports.reasons.maskingFailed'),
  'local-reviewer': tk('ee.reports.reasons.localReviewer'),
  'live-reviewer': tk('ee.reports.reasons.liveReviewer'),
  'settings-changed': tk('ee.reports.reasons.settingsChanged'),
  'aws-signing': tk('ee.reports.reasons.awsSigning'),
  'route-bypass': tk('ee.reports.reasons.routeBypass'),
  'outbound-tool': tk('ee.reports.reasons.outboundTool'),
}

/** The words for why a request was stopped (a code of the server's); one it does not know is "Other". */
export function reasonLabel(code: string): string {
  return REASONS[code] ?? OTHER_REASON
}

/** An unnamed historic row can have activity with count zero. */
export function hasResidual(row: ReportResidual | ApproverResidual | null): boolean {
  return row !== null && Object.values(row).some((value) => value > 0)
}

export const PEOPLE_NOTE: Record<ReportPeople, string> = {
  'group-only': tk('ee.reports.people.groupOnly'),
  pseudonymous: tk('ee.reports.people.pseudonymous'),
  named: tk('ee.reports.people.named'),
}

/** Decisions split into those that let a text or a change through and the others. */
export function splitOutcomes(outcomes: Readonly<Record<string, number>>, through: readonly string[]): { through: number; other: number } {
  let yes = 0
  let no = 0
  for (const [outcome, n] of Object.entries(outcomes)) {
    if (through.includes(outcome)) yes += n
    else no += n
  }
  return { through: yes, other: no }
}

const WHYS: readonly Paused['why'][] = ['expired', 'users', 'trial-ended', 'unusable']

/** The pause a 403 `not-producing` of the server carries (R2), or null when the error is another. */
export function pausedOf(error: unknown): Paused | null {
  if (!(error instanceof ApiError) || error.status !== 403) return null
  const body = error.body as { code?: unknown; paused?: Partial<Paused> | null } | undefined
  const paused = body?.code === 'not-producing' ? body.paused : null
  if (!paused || !WHYS.includes(paused.why as Paused['why'])) return null
  return { why: paused.why as Paused['why'], expires: typeof paused.expires === 'string' ? paused.expires : null }
}

export const PAUSED_TEXT: Record<Paused['why'], string> = {
  expired: tk('ee.reports.paused.expired'),
  users: tk('ee.reports.paused.users'),
  'trial-ended': tk('ee.reports.paused.trialEnded'),
  unusable: tk('ee.reports.paused.unusable'),
}
