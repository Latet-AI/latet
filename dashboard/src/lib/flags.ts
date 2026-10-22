// Exchange flags: "blocked", "leak", "uninspected:image", "degraded:pii", "unknown-route", ...

export type FlagKey =
  | 'blocked'
  | 'leak'
  | 'error'
  | 'uninspected'
  | 'degraded'
  | 'unknown-route'
  | 'auto-mode-review'
  | 'project-off'
  | 'withheld'
  | 'history'
  | 'outbound'
  | 'route-bypass'
  | 'image'
  | 'other'
export type Severity = 'critical' | 'warning' | 'neutral'

export interface ParsedFlag {
  raw: string
  key: FlagKey
  /** The part after ":" ("image" in "uninspected:image"), if any. */
  detail: string | null
  severity: Severity
}

const SEVERITY: Record<FlagKey, Severity> = {
  blocked: 'critical',
  leak: 'critical',
  error: 'critical',
  uninspected: 'warning',
  degraded: 'warning',
  'unknown-route': 'neutral',
  'auto-mode-review': 'neutral',
  'project-off': 'warning',
  withheld: 'warning',
  history: 'warning',
  outbound: 'warning',
  'route-bypass': 'critical',
  image: 'neutral',
  other: 'neutral',
}

const KNOWN = new Set<FlagKey>([
  'blocked',
  'leak',
  'error',
  'uninspected',
  'degraded',
  'unknown-route',
  'auto-mode-review',
  'project-off',
  'withheld',
  'history',
  'outbound',
  'route-bypass',
  'image',
])
const ORDER: Severity[] = ['critical', 'warning', 'neutral']

export function parseFlag(raw: string): ParsedFlag {
  const colon = raw.indexOf(':')
  const head = colon === -1 ? raw : raw.slice(0, colon)
  const detail = colon === -1 ? null : raw.slice(colon + 1) || null
  const key: FlagKey = KNOWN.has(head as FlagKey) ? (head as FlagKey) : 'other'
  // An image withheld was not sent: worth a look; one masked went as it should.
  const severity: Severity = key === 'image' && detail === 'withheld' ? 'warning' : SEVERITY[key]
  return { raw, key, detail, severity }
}

/** Parsed flags, most severe first, keeping the daemon's order within a severity. */
export function parseFlags(flags: readonly string[]): ParsedFlag[] {
  return flags
    .map(parseFlag)
    .map((flag, i) => ({ flag, i }))
    .sort((a, b) => ORDER.indexOf(a.flag.severity) - ORDER.indexOf(b.flag.severity) || a.i - b.i)
    .map(({ flag }) => flag)
}

/** Does an exchange carry `filter`? A bare filter ("uninspected") matches any detail. */
export function hasFlag(flags: readonly string[], filter: string): boolean {
  if (!filter) return true
  return flags.some((flag) => flag === filter || flag.startsWith(`${filter}:`))
}

export function isBlocked(flags: readonly string[]): boolean {
  return hasFlag(flags, 'blocked')
}
