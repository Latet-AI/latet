// What Rules says under the full name of the identity: whose name it is, and whether the person can change it.

import type { IdentityView } from '../api/types'
import { tk } from '../i18n'

/**
 * The key of the line under the full name, or null when the name is the person's own to change.
 *
 * `provider`: the name in use is the one the company's identity provider gives. `full_name_locked` without it: the
 * provider names the person all the same, but their own name is in use (their settings mask names), and a change of it
 * is refused either way. The server's answer says it as `full_name_locked`, whichever name is in use.
 */
export function fullNameNote(identity: Pick<IdentityView, 'full_name_from' | 'full_name_locked'>): string | null {
  if (identity.full_name_from === 'provider') return tk('rules.identity.fullNameProvider')
  if (identity.full_name_locked) return tk('rules.identity.fullNameLocked')
  return null
}
