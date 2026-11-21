// What the Enterprise pages decide when a change they asked for is refused, and how a notice is brought to the person.
// Pure functions: the pages and their components do the rest.

import { companySignInNeeded } from '../../lib/refused'
import { signInChanged } from './signIn'
import { siemChanged } from './siem'

/**
 * What a refused change does next:
 * - `company`: the server asks for the company sign-in (403 `company-sign-in`); the page's banner says why and leads
 *   to it, so no toast repeats it.
 * - `changed`: the settings changed since the page read them (409 `sign-in-changed` or `siem-changed`), and the page can read them
 *   again (`readsAgain`): it does, and says someone else changed them. Where it cannot, the reason is a toast.
 * - `toast`: any other refusal, said with the server's reason.
 */
export type Refusal = 'company' | 'changed' | 'toast'

export function refusalOf(error: unknown, readsAgain: boolean): Refusal {
  if (companySignInNeeded(error)) return 'company'
  if (readsAgain && (signInChanged(error) || siemChanged(error))) return 'changed'
  return 'toast'
}

/** What a banner needs to be brought to the person: the two calls of an element. */
export interface Revealable {
  scrollIntoView(options?: ScrollIntoViewOptions): void
  focus(options?: FocusOptions): void
}

/**
 * A banner that is the only feedback of a change (the toast is not raised for it) may be above or below what the person
 * sees, with the button they pressed far from it: it is scrolled into view, only as far as it needs, and focused, so
 * that a screen reader reads it. Nothing before it is on the page.
 */
export function reveal(banner: Revealable | null): void {
  if (!banner) return
  banner.scrollIntoView({ block: 'nearest' })
  banner.focus({ preventScroll: true })
}
