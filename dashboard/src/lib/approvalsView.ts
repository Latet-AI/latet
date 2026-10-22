// How the Approvals page shows what waits for you: the texts and the change to your settings in two tabs, which tab
// it opens on, in a narrow window which text of the queue shows, and when a text just shown can be decided.

/** The tabs of Approvals: the texts waiting, and the change to your settings waiting. */
export type ApprovalsTab = 'texts' | 'settings'

export const APPROVALS_TABS: readonly ApprovalsTab[] = ['texts', 'settings']

/** The tab a link names (`/approvals#settings`), or null for none. */
export function approvalsTabNamed(hash: string): ApprovalsTab | null {
  const id = hash.startsWith('#') ? hash.slice(1) : null
  return APPROVALS_TABS.find((tab) => tab === id) ?? null
}

/**
 * The tab Approvals opens on, chosen once as it loads: Texts, unless no text waits and a change to your settings
 * does. What arrives later only changes the counts: the tab under you never moves.
 */
export function initialApprovalsTab(batches: readonly unknown[], changes: readonly unknown[]): ApprovalsTab {
  return batches.length === 0 && changes.length > 0 ? 'settings' : 'texts'
}

/** In a narrow window, one text at a time: which it is of how many wait, and the texts before and after it. */
export function queuePosition<B>(batches: readonly B[], shown: B): { number: number; count: number; previous: B | null; next: B | null } {
  const index = batches.indexOf(shown)
  return { number: index + 1, count: batches.length, previous: batches[index - 1] ?? null, next: batches[index + 1] ?? null }
}

/**
 * How long a text shows before it can be decided: Approve and Don't send stay where they are as the next text shows, so
 * the second click of a double click, or of a double tap, would otherwise decide the next text unseen.
 */
export const DECIDE_AFTER_MS = 400

/** A text that came into view, and when (ms, by the page's monotonic clock). */
export interface Shown {
  id: string
  at: number
}

/** Whether the text `id` can be decided at `now` (ms): it is the one shown (`shown`, since `shown.at`), for DECIDE_AFTER_MS. */
export function mayDecide(shown: Shown | null, id: string, now: number): boolean {
  return shown !== null && shown.id === id && now - shown.at >= DECIDE_AFTER_MS
}

/** The text `id` comes into view now. */
export function shownNow(id: string): Shown {
  return { id, at: performance.now() }
}

/** Whether the text `id` can be decided now (`mayDecide`, by the page's clock). */
export function mayDecideNow(shown: Shown | null, id: string): boolean {
  return mayDecide(shown, id, performance.now())
}
