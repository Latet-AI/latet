// The Enterprise pages a person may open, as /me lists them (`edition.pages`): the menu shows those, and their routes
// open only those. The Community Edition lists none.

import type { Me } from '../api/types'

/** Whether this person may open the Enterprise page `page`. */
export function hasPage(me: Me | undefined, page: string): boolean {
  return me?.edition?.pages.includes(page) ?? false
}

/** The items of the Enterprise group this person may open, in their order. */
export function visibleItems<T extends { page: string }>(items: readonly T[], me: Me | undefined): T[] {
  return items.filter((item) => hasPage(me, item.page))
}

/**
 * What an Enterprise page shows: nothing of its own when /me does not list it (`none`); once a trial ended, the page
 * read-only (`read-only`: the notice that the trial ended above it, what it holds read, not changed), except License,
 * where a server's license is shown, which its administrators change there while it can manage, else from the command
 * line; else the page.
 */
export function enterpriseView(me: Me | undefined, page: string): 'none' | 'read-only' | 'page' {
  if (!hasPage(me, page)) return 'none'
  return page !== 'license' && me?.edition?.license?.state === 'trial-ended' ? 'read-only' : 'page'
}
