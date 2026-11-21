// The License page's logic, and the notice bar's: what a license's standing means for the reader, as keys of the
// translations and their values. Pure functions: the page and the bar render what they answer.

import { ApiError } from '../../api/client'
import type { LicenseNotice, LicenseState, LicenseSummary, Me } from '../../api/types'
import { tk } from '../../i18n'
import { companySignInNeeded } from '../../lib/refused'
import type { LicenseErrorCode, LicenseStanding } from '../types'

export type Tone = 'good' | 'warn' | 'crit' | 'neutral'

export const STATE_TONE: Record<LicenseState, Tone> = {
  none: 'neutral',
  valid: 'good',
  expiring: 'warn',
  grace: 'warn',
  paused: 'crit',
  'trial-ended': 'crit',
  invalid: 'crit',
}

export const STATE_LABEL: Record<LicenseState, string> = {
  none: tk('ee.license.states.none'),
  valid: tk('ee.license.states.valid'),
  expiring: tk('ee.license.states.expiring'),
  grace: tk('ee.license.states.grace'),
  paused: tk('ee.license.states.paused'),
  'trial-ended': tk('ee.license.states.trialEnded'),
  invalid: tk('ee.license.states.invalid'),
}

/** What each state means, in a sentence: `date` (the day production pauses) and `reason` (why a file cannot be used). */
export const STATE_TEXT: Record<LicenseState, string> = {
  none: tk('ee.license.stateText.none'),
  valid: tk('ee.license.stateText.valid'),
  expiring: tk('ee.license.stateText.expiring'),
  grace: tk('ee.license.stateText.grace'),
  paused: tk('ee.license.stateText.paused'),
  'trial-ended': tk('ee.license.stateText.trialEnded'),
  invalid: tk('ee.license.stateText.invalidServer'),
}

/**
 * The sentence of a standing's state: a grace whose pause day is not told (on a server, a person is not told the users'
 * grace) says that things keep working, without a day. A file the server cannot use is said as the command line says
 * it (`describe()`): the server keeps the features its installing instance verified, and its database not answering is
 * said as such.
 */
export function stateTextKey(
  standing: Pick<LicenseStanding, 'state' | 'pauses_on'> & Partial<Pick<LicenseStanding, 'source' | 'reason_code'>>,
): string {
  if (standing.state === 'grace' && standing.pauses_on === null) return tk('ee.license.stateText.graceUndated')
  if (standing.state === 'invalid' && standing.reason_code === 'unreadable') return tk('ee.license.stateText.invalidServerDown')
  return STATE_TEXT[standing.state]
}

const FEATURE_LABEL: Record<string, string> = {
  'locked-rules': tk('ee.license.features.lockedRules'),
  approvals: tk('ee.license.features.approvals'),
  reports: tk('ee.license.features.reports'),
  siem: tk('ee.license.features.siem'),
  'term-sync': tk('ee.license.features.termSync'),
  sso: tk('ee.license.features.sso'),
  roles: tk('ee.license.features.roles'),
}

/** A feature's name in the reader's language; one this dashboard does not know (a newer license), as it is. */
export function featureLabel(t: (key: string) => string, feature: string): string {
  const key = FEATURE_LABEL[feature]
  return key ? t(key) : feature
}

const REASON: Record<LicenseErrorCode, string> = {
  unreadable: tk('ee.license.reasons.unreadable'),
  format: tk('ee.license.reasons.format'),
  'no-keys': tk('ee.license.reasons.noKeys'),
  key: tk('ee.license.reasons.key'),
  signature: tk('ee.license.reasons.signature'),
  version: tk('ee.license.reasons.version'),
  content: tk('ee.license.reasons.content'),
  ended: tk('ee.license.reasons.ended'),
}

/** Why a file is no license, as a key of the translations; a code this dashboard does not know: "not a license". */
export function reasonKey(code: string | null | undefined): string {
  return REASON[code as LicenseErrorCode] ?? REASON.format
}

/**
 * What a change of the license stops, as the server says it: the server's policy, the synced terms (E6), central
 * approvals while the security team or an API key's other controllers decide (the final security review's L1, L1'), or
 * several.
 */
export interface Stopping {
  policy: boolean
  terms: boolean
  approvals: boolean
}

/**
 * What the server's refusal of a license change says stops with it (409, `policy: "stops"` when the server's policy
 * stops being in force, `terms: "stop"` when the synced terms stop being masked, `approvals: "stop"` when central
 * approvals stop, or several); null for any other answer.
 */
export function licenseStops(error: unknown): Stopping | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  if (error.body === null || typeof error.body !== 'object') return null
  const said = error.body as { policy?: unknown; terms?: unknown; approvals?: unknown }
  const stopping = { policy: said.policy === 'stops', terms: said.terms === 'stop', approvals: said.approvals === 'stop' }
  return stopping.policy || stopping.terms || stopping.approvals ? stopping : null
}

/**
 * The question's title and words, as keys of the translations: the policy's, the synced terms', or both's; central
 * approvals' alone, or, beside the others, a paragraph of their own (`also`).
 */
export function stopsKeys(stopping: Stopping): { title: string; body: string; also?: string } {
  const also = stopping.approvals ? { also: tk('ee.license.change.approvalsStopAlso') } : {}
  if (stopping.policy && stopping.terms) {
    return { title: tk('ee.license.change.bothStopTitle'), body: tk('ee.license.change.bothStopBody'), ...also }
  }
  if (stopping.terms) {
    return { title: tk('ee.license.change.termsStopTitle'), body: tk('ee.license.change.termsStopBody'), ...also }
  }
  if (stopping.approvals && !stopping.policy) {
    return { title: tk('ee.license.change.approvalsStopTitle'), body: tk('ee.license.change.approvalsStopBody') }
  }
  return { title: tk('ee.license.change.stopsTitle'), body: tk('ee.license.change.stopsBody'), ...also }
}

/** The standings from which the dashboard can install or remove a license: a license that is valid, ending soon or in its grace. */
const MANAGEABLE: readonly LicenseState[] = ['valid', 'expiring', 'grace']

/**
 * Whether the dashboard offers to install or remove the license: to whoever may manage it (`license.manage`) while the
 * license in use can manage (the server refuses any other standing: a trial that ended, a pause, a file that cannot be
 * used, none). In those standings the license is installed from the command line.
 */
export function licenseChangeable(state: LicenseState, actions: readonly string[]): boolean {
  return actions.includes('license.manage') && MANAGEABLE.includes(state)
}

/**
 * The most bytes the server reads of the body that installs a license (413 past it). It counts the JSON the page sends,
 * not the file: the text in quotes, each quote, backslash, newline and control character written with a backslash.
 */
export const MAX_LICENSE_BODY = 256 * 1024

/** The body that installs a license: its text, and the word that the policy stopping was seen once the person said so. */
export function installBody(text: string, policyStops = false): { license: string; policy_stops?: true } {
  return { license: text, ...(policyStops ? { policy_stops: true as const } : {}) }
}

/**
 * The bytes of the body that installs `text`, as it will be sent. The larger form is counted, the one that says the
 * policy stopping was seen: a file that fits the first call fits the second.
 */
export function licenseBodySize(text: string): number {
  return new TextEncoder().encode(JSON.stringify(installBody(text, true))).length
}

/** A license file chosen, read: its text, or why it is not sent (too large to be a license, or not readable). */
export type LicenseFile = { text: string } | { refused: 'tooLarge' | 'unreadable' }

/**
 * Reads a chosen file to send as a license. A file larger than the server's limit is not read: its body would be
 * larger still. One smaller is measured as it will be sent (`licenseBodySize`), for the JSON is larger than the file.
 */
export async function readLicenseFile(file: { size: number; text: () => Promise<string> }): Promise<LicenseFile> {
  if (file.size > MAX_LICENSE_BODY) return { refused: 'tooLarge' }
  let text: string
  try {
    text = await file.text()
  } catch {
    return { refused: 'unreadable' }
  }
  return licenseBodySize(text) > MAX_LICENSE_BODY ? { refused: 'tooLarge' } : { text }
}

/** Why the server refused a file as a license (400, with the code of its reason); null for any other refusal. */
export function licenseErrorCode(error: unknown): LicenseErrorCode | null {
  if (!(error instanceof ApiError) || error.status !== 400) return null
  const code = error.body !== null && typeof error.body === 'object' ? (error.body as { code?: unknown }).code : undefined
  return typeof code === 'string' && Object.hasOwn(REASON, code) ? (code as LicenseErrorCode) : null
}

/**
 * A change of the license that waits for the person's word that the server's policy stops being in force with it, the
 * synced terms being masked, or central approvals.
 */
export type PendingLicense = { kind: 'install'; text: string } | { kind: 'remove' }

/** The two calls of a change (`eeApi.installLicense` and `eeApi.removeLicense`). */
export interface LicenseCalls {
  installLicense: (text: string, options?: { policyStops?: boolean }) => Promise<unknown>
  removeLicense: (options?: { policyStops?: boolean }) => Promise<unknown>
}

/**
 * What a change of the license came to: made; the server's policy would stop being in force, the synced terms being
 * masked, or central approvals (`stopping` says which: nothing changed, the page asks, and calls again with the same
 * change); the session was not opened with the company sign-in (the page's banner says so); or refused (a file that is
 * no license, by its code, or any other reason).
 */
export type LicenseOutcome =
  | { kind: 'done'; pending: PendingLicense }
  | { kind: 'stops'; pending: PendingLicense; stopping: Stopping }
  | { kind: 'company' }
  | { kind: 'refused'; code: LicenseErrorCode | null; error: unknown }

/**
 * Installs or removes the license. `seen` says the person saw what stops (the policy, the synced terms, central
 * approvals): the first call never does, the second, made after the page asked, does, with the same file.
 */
export async function changeLicense(pending: PendingLicense, seen: boolean, calls: LicenseCalls): Promise<LicenseOutcome> {
  try {
    if (pending.kind === 'install') await calls.installLicense(pending.text, { policyStops: seen })
    else await calls.removeLicense({ policyStops: seen })
    return { kind: 'done', pending }
  } catch (error) {
    const stopping = licenseStops(error)
    if (stopping) return { kind: 'stops', pending, stopping }
    if (companySignInNeeded(error)) return { kind: 'company' }
    return { kind: 'refused', code: licenseErrorCode(error), error }
  }
}

/** How far the last day is: in 12 days, its last day, ended 3 days ago. */
export function daysText(daysLeft: number): { key: string; count: number } {
  if (daysLeft > 0) return { key: tk('ee.license.daysLeft'), count: daysLeft }
  if (daysLeft === 0) return { key: tk('ee.license.endsToday'), count: 0 }
  return { key: tk('ee.license.endedAgo'), count: -daysLeft }
}

/** A license's day (YYYY-MM-DD, a UTC day) in the reader's language, with its year. */
export function formatLicenseDay(locale: string, iso: string): string {
  const [y, m, d] = iso.split('-').map(Number)
  const format = new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' })
  return format.format(new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1)))
}

export type Weighed = Pick<LicenseSummary, 'expires' | 'days_left' | 'users' | 'users_in_use' | 'trial' | 'notices' | 'pauses_on'>

/** What the notices need of the page's standing. */
export function weighedOf(standing: LicenseStanding): Weighed {
  const license = standing.license
  return {
    expires: license?.expires ?? null,
    users: license?.users ?? null,
    trial: license?.trial ?? false,
    days_left: standing.days_left,
    users_in_use: standing.users_in_use,
    notices: standing.notices,
    pauses_on: standing.pauses_on,
  }
}

export interface NoticeText {
  key: string
  values: Record<string, string | number>
  tone: 'warn' | 'crit'
}

/**
 * What each notice says, the most serious first; `day` writes a YYYY-MM-DD day in the reader's language. The notices
 * about the users say how many are in use: where that is not told (`users_in_use` null: on a server, a person is not
 * told the administrators' count), they are not said, and the day a pause would start is said only where it is told.
 */
export function noticeTexts(license: Weighed, day: (iso: string) => string): NoticeText[] {
  const has = (code: LicenseNotice) => license.notices.includes(code)
  const date = license.expires ? day(license.expires) : ''
  const pausesOn = license.pauses_on ? day(license.pauses_on) : ''
  const counted = license.users_in_use !== null
  const counts = { inUse: license.users_in_use ?? 0, users: license.users ?? 0 }
  const out: NoticeText[] = []
  if (has('invalid')) out.push({ key: tk('ee.license.notices.invalid'), values: {}, tone: 'crit' })
  if (has('trial-ended')) out.push({ key: tk('ee.license.notices.trialEnded'), values: { date }, tone: 'crit' })
  if (has('trial-day')) {
    // The trial's second warning: the day before its last day, and its last day.
    const key = (license.days_left ?? 0) > 0 ? tk('ee.license.notices.trialTomorrow') : tk('ee.license.notices.trialToday')
    out.push({ key, values: { date }, tone: 'crit' })
  }
  if (has('paused') && (license.days_left ?? 0) < 0) {
    out.push({ key: tk('ee.license.notices.expiredPaused'), values: { date }, tone: 'crit' })
  }
  if (has('users-over') && counted) {
    out.push(
      has('paused')
        ? { key: tk('ee.license.notices.usersOverPaused'), values: counts, tone: 'crit' }
        : { key: tk('ee.license.notices.usersOver'), values: { ...counts, pausesOn }, tone: 'warn' },
    )
  }
  if (has('grace')) out.push({ key: tk('ee.license.notices.grace'), values: { date, pausesOn }, tone: 'warn' })
  if (has('expiring')) {
    out.push({ key: tk('ee.license.notices.expiring'), values: { date, count: license.days_left ?? 0 }, tone: 'warn' })
  }
  if (has('trial-week')) {
    out.push({ key: tk('ee.license.notices.trialWeek'), values: { date, count: license.days_left ?? 0 }, tone: 'warn' })
  }
  if (has('users-near') && counted) out.push({ key: tk('ee.license.notices.usersNear'), values: counts, tone: 'warn' })
  return out.sort((a, b) => Number(b.tone === 'crit') - Number(a.tone === 'crit'))
}

/**
 * The notice the bar shows on every page, or null: the most serious one, to whoever can act on it: a server's
 * administrators (a role E3 gives). A person on a server has no license to change, and a computer has none.
 */
export function licenseBanner(me: Me | undefined, day: (iso: string) => string): NoticeText | null {
  const edition = me?.edition
  if (!me || !edition?.license) return null
  if (!edition.roles.includes('admin')) return null
  return noticeTexts(edition.license, day)[0] ?? null
}
