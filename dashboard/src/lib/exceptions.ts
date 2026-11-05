// The request exceptions of D3 and D4 as the dashboard lists them: by state, the ones about to end first, and what
// their uses are counted as while the daemon runs (M1, D2).

import type { RequestException, RequestStatus } from '../api/types'

/** The exceptions that end within 7 days: warned before they do (D3). */
export function expiringSoon(items: readonly RequestException[]): RequestException[] {
  return items.filter((item) => item.state === 'expiring')
}

/** The list once an exception is revoked: that one taken out, the others as they were. */
export function withoutRevoked(items: readonly RequestException[], id: string): RequestException[] {
  return items.filter((item) => item.id !== id)
}

/** In force now: approved, within its lifetime (a permanent one always, unless the edition forbids it). */
export function inForce(item: RequestException): boolean {
  return item.state === 'active' || item.state === 'expiring' || item.state === 'permanent'
}

const ORDER: RequestException['state'][] = [
  'expiring',
  'needs approval again (profile revised)',
  'pending',
  'expired',
  'forbidden',
  'permanent',
  'active',
]

/** What to look at first: about to end, suspended, waiting, ended; then those in force. */
export function ordered(items: readonly RequestException[]): RequestException[] {
  return [...items].sort((a, b) => ORDER.indexOf(a.state) - ORDER.indexOf(b.state))
}

/** The uses of exceptions counted since the daemon started (each row whose key is an exception's reference, never
 * its id), what could not be counted, and whether a server's other instances are in the sum. */
export function exceptionUses(status: RequestStatus): {
  uses: number
  overflow: number
  full: boolean
  server: 'summed' | 'alone' | null
} {
  // A server's sum holds this instance's counts too (D2): read instead of them, never added to them.
  const summed = status.server?.available && status.server.counts ? status.server : null
  const rows = summed?.counts ?? status.local.counts
  const uses = rows.reduce((sum, row) => (typeof row[0] === 'string' ? sum + row[3] : sum), 0)
  const server = status.server === null ? null : status.server.available ? 'summed' : 'alone'
  return {
    uses,
    overflow: summed ? (summed.overflow ?? 0) : status.local.overflow,
    full: status.local.saturated || status.local.partial || (status.server?.saturated ?? false),
    server,
  }
}
