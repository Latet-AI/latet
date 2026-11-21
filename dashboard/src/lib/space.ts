/** Where this tab keeps the API key whose space it shows (E3, a server with Latet Enterprise): this tab only. */
export const SPACE_STORAGE = 'latet.space'

/**
 * Where a request of the API goes: under `base`, or, while an API key's space is open, under that space
 * (`/ee/keys/{id}/space`). The routes about the keys themselves (`/ee/keys…`) are always the person's own, and so is
 * signing out: it ends the person's session, which a request in the key's space would only record as a change there.
 */
export function spaceUrl(base: string, space: string | null, path: string): string {
  const own = path === '/logout' || path === '/ee/keys' || path.startsWith('/ee/keys/') || path.startsWith('/ee/keys?')
  if (!space || own) return base + path
  return `${base}/ee/keys/${encodeURIComponent(space)}/space${path}`
}

// The statuses of an answer that says `space: "closed"`: refused (the license without roles, control taken away, a trial
// that ended), no such key, disabled, not available (ee/keys.py, and the gate in front of the space, ee/access.py).
const CLOSED = [403, 404, 409, 503]

/**
 * Whether an answer says the space this tab shows was closed to the person: the server marks what it closes with
 * `space: "closed"` (control taken away, key disabled, a trial ended, the license without roles, who may do what
 * unreadable). Every other refusal is the key's own (a 404, a 409 of its settings), or the person's own space would
 * refuse it as well (the dashboard's checks), and leaves the space open. A 401 is the session's end, which the
 * transport says before it asks.
 */
export function closesSpace(status: number, body: unknown): boolean {
  if (typeof body !== 'object' || body === null) return false
  return (body as { space?: unknown }).space === 'closed' && CLOSED.includes(status)
}
