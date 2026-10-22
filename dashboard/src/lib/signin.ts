// The sign-in page: why the link that opened it signed no one in, and whether it is a Latet server's.

import type { Health } from '../api/client'
import { servedFromServer } from './serverPage'

/**
 * Why the sign-in link that opened the page signed no one in (`?login=`): it was used, too old, or opened from another
 * site's page (`expired`); on a Latet server also too many links tried from one address (`limited`), or the server busy
 * (`busy`). Null: no link, or one that worked.
 */
export type LoginRefusal = 'expired' | 'limited' | 'busy' | null

/** `?login=` of the address the page was opened at (read once: the router rewrites it), one of the reasons above alone. */
export function loginRefusal(search: string): LoginRefusal {
  const said = new URLSearchParams(search).get('login')
  return said === 'expired' || said === 'limited' || said === 'busy' ? said : null
}

/**
 * Whether the sign-in page is a Latet server's: what its health answer says once it has one (`server`), so that a
 * computer's daemon reached under a name of its own (a tunnel, a proxy) keeps a computer's words; until then, whether
 * the page comes from a server, a computer's dashboard being always on this computer (`servedFromServer`).
 */
export function signInOnServer(health: Health | undefined, hostname: string): boolean {
  return health !== undefined ? health.server === true : servedFromServer(hostname)
}
