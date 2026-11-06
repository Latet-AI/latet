// What Latet read of a request, and what let it through (the request-coverage plan's P2-7): from the history's safe
// summary alone, by fixed categories and counts. Never a value: what a rule or an exception let through is named by its
// category, a header by nothing at all here.

import type { ExchangeSummary, LaunchCategory, PassCategory } from '../api/types'
import { isBlocked } from './flags'

/** What became of a request, as the exchange page says it first. */
export type CoverageOutcome =
  /** Stopped. `sent`: before anything left, or after an attempt that may already have reached the provider. */
  | { kind: 'blocked'; sent: 'before' | 'attempted' }
  /** Every part of it read: masked where it held values, nothing let through unread. */
  | { kind: 'read' }
  /** Read, and let through by the rules or exceptions named (`unread`: some of it passed without being read). */
  | { kind: 'passed'; passes: PassCategory[]; unread: boolean }
  /** How much of it was read was not recorded: an older Latet's record (a WebSocket turn records its own). */
  | { kind: 'unknown' }
  /** A check of the Claude Code plugin's, inside Claude Code: it sends nothing, so it has no coverage to say. */
  | { kind: 'observed' }

/** The order the passes are said in: what was read first, what passed unread after. */
export const PASS_ORDER: readonly PassCategory[] = [
  'checked-only',
  'sent-before',
  'model-written',
  'image-remainder',
  'provider-echo',
  'provider-encrypted',
  'exception',
]

/** The passes under which a part of the request left without being read: an image's remainder after its text was
 * read, a provider's opaque item (an echo it observed, or the built-in rule), and what an exception let through. */
const UNREAD: ReadonlySet<PassCategory> = new Set(['image-remainder', 'provider-echo', 'provider-encrypted', 'exception'])

function passesOf(summary: ExchangeSummary): PassCategory[] {
  const given = new Set(summary.passes ?? [])
  return PASS_ORDER.filter((pass) => given.has(pass))
}

export function coverageOutcome(summary: ExchangeSummary): CoverageOutcome {
  if (isBlocked(summary.flags)) return { kind: 'blocked', sent: summary.overhead_ms === null ? 'before' : 'attempted' }
  if (summary.transport === 'plugin') return { kind: 'observed' }
  // `legacy-uninspected` is written by nothing any more: like any older record, its coverage is unknown.
  if (summary.coverage !== 'complete-accounting') return { kind: 'unknown' }
  const passes = passesOf(summary)
  if (passes.length === 0) return { kind: 'read' }
  return { kind: 'passed', passes, unread: passes.some((pass) => UNREAD.has(pass)) }
}

/**
 * Whether the page may say "Verified" in green: the egress check found nothing in clear, and every part of the request
 * is known to have been read (no rule or exception that let a part through unread, no part Latet could not inspect).
 * Never for unread material, and never for a record whose coverage is not known: an older Latet's, which sent unread
 * parts without saying so, or a check of the plugin's (review I-3).
 */
export function showsVerified(summary: ExchangeSummary): boolean {
  if (!summary.verified) return false
  if (summary.flags.some((flag) => flag.startsWith('uninspected:'))) return false
  const outcome = coverageOutcome(summary)
  return outcome.kind === 'read' || (outcome.kind === 'passed' && !outcome.unread)
}

/** Why a request was stopped, by the category of its safe reason code: what the dashboard says in the reader's language
 * (the agent was told it in English). */
export type RefusalCategory =
  | 'header' // a header no profile names (D4)
  | 'query' // a query parameter no profile names
  | 'body-member' // a body member no profile names (D6), which an exact exception can name
  | 'body-member-closed' // one no exception can reach: in a provider's item, an image's source, a frame (I-8)
  | 'route' // a call no built-in profile covers (D1)
  | 'expired' // the exception that let it through ended (D3)
  | 'resource' // a model, an ARN, a project or a region Latet does not know by its form
  | 'sensitive' // a sensitive value in its address or headers
  | 'auth' // the credentials Latet signs with, or the provider's own
  | 'egress' // a destination of yours this machine may not reach
  | 'parse' // a part Latet cannot read as its protocol says (an escape, an encoding, a duplicate)
  | 'limit' // past what Latet reads of one request, or too busy
  | 'state' // a state it needs could not be read
  | 'settings' // its settings changed while it was prepared: sent again, it goes under the new ones
  | 'masking' // masking failed (a vault at its limit)
  | 'other'

const REFUSALS: Record<string, RefusalCategory> = {
  'envelope-header-unknown': 'header',
  'envelope-query-unknown': 'query',
  'body-unknown-member': 'body-member',
  'body-unknown-member-no-exception': 'body-member-closed',
  'envelope-route': 'route',
  'envelope-profile': 'route',
  'envelope-bodyless': 'route',
  'no-route': 'route',
  unread: 'route',
  'no-signing-route': 'route',
  'envelope-exception-expired': 'expired',
  'envelope-resource': 'resource',
  'envelope-region': 'resource',
  'envelope-location': 'resource',
  'envelope-binding': 'resource',
  'envelope-sensitive': 'sensitive',
  credentials: 'auth',
  'envelope-credential': 'auth',
  'egress-refused': 'egress',
  'envelope-escape': 'parse',
  'envelope-encoding': 'parse',
  'envelope-control': 'parse',
  'envelope-path': 'parse',
  'envelope-header-duplicate': 'parse',
  'envelope-header-syntax': 'parse',
  'envelope-header-value': 'parse',
  'envelope-query-duplicate': 'parse',
  'envelope-query-value': 'parse',
  'proof-incomplete': 'parse',
  'proof-foreign': 'parse',
  evidence: 'parse',
  'coverage-limit': 'limit',
  'too-large': 'limit',
  busy: 'limit',
  'conversation-unread': 'limit',
  'envelope-state-unavailable': 'state',
  'settings-changed': 'settings',
  'masking-failed': 'masking',
}

export function refusalCategory(code: string | null | undefined): RefusalCategory | null {
  if (!code) return null
  return REFUSALS[code] ?? 'other'
}

/** What the page offers as the way through a refusal. An exact request exception only where it can name what was
 * refused (a header or a query parameter no profile names) or renew one that ended: never for a failure of
 * authorization, egress, reading or a work limit, nor for a route (an exception names components of a profile that
 * exists, F-4). */
export type Remedy = 'exception' | 'exception-docs' | 'renew' | 'resource' | 'again' | 'websocket' | 'none'

/** A body member is let through by an exception named in your settings: the page says where that is explained, and
 * offers no action of its own (exceptions come from config.yaml or a command, and wait for your approval). */
export function remedyFor(category: RefusalCategory | null): Remedy {
  if (category === 'header' || category === 'query') return 'exception'
  if (category === 'body-member') return 'exception-docs'
  if (category === 'expired') return 'renew'
  if (category === 'resource') return 'resource'
  if (category === 'settings') return 'again'
  return 'none'
}

/** The remedy of one exchange's refusal: on a WebSocket none that an exception would be, since a WebSocket takes no
 * request exception in 0.11.0 (F-2), the page then saying so. */
export function remedyOf(summary: ExchangeSummary, category: RefusalCategory | null): Remedy {
  const remedy = remedyFor(category)
  const excepting = remedy === 'exception' || remedy === 'exception-docs' || remedy === 'renew'
  return summary.transport === 'ws' && excepting ? 'websocket' : remedy
}

/** What a request's launch was decided as (P2-6a, R-120); null when none was (an older record, the plugin's checks). */
export function launchOf(summary: ExchangeSummary): LaunchCategory | null {
  const launch = summary.launch
  return launch === 'launched' || launch === 'launched-key-missing' || launch === 'trusted' || launch === 'claimed'
    ? launch
    : null
}

/** A session's launch status, from its exchanges: the least trusted seen, so that a key that went missing once is
 * not hidden by the requests that had it. */
export function sessionLaunch(exchanges: readonly ExchangeSummary[]): LaunchCategory | null {
  const seen = new Set(exchanges.map(launchOf))
  for (const launch of ['launched-key-missing', 'claimed', 'trusted', 'launched'] as const) {
    if (seen.has(launch)) return launch
  }
  return null
}

/** Whether a request's project was trusted (a verified launch key, the Claude Code plugin's binding: its project's
 * loosenings apply) or claimed (a header or a folder in its text: its project's rules that add protection alone, D7);
 * null without a project, or for an older record that says neither. */
export function projectBinding(summary: ExchangeSummary): 'trusted' | 'claimed' | null {
  if (!summary.project) return null
  if (summary.flags.includes('project-claimed')) return 'claimed'
  const launch = launchOf(summary)
  if (launch === 'launched' || launch === 'trusted') return 'trusted'
  if (launch === 'claimed' || launch === 'launched-key-missing') return 'claimed'
  return null
}

/** What its profile removed of a request that left (D4): headers and body members, counted; and whether the
 * provider then answered 401 or 403, which a removed header may have caused. Null when nothing was removed, or the
 * request never left. */
export function removedNote(summary: ExchangeSummary): { headers: number; members: number; authAfter: boolean } | null {
  const headers = summary.removed?.headers ?? 0
  const members = summary.removed?.members ?? 0
  if ((headers === 0 && members === 0) || isBlocked(summary.flags)) return null
  return { headers, members, authAfter: headers > 0 && (summary.status === 401 || summary.status === 403) }
}
