// What is edited and not saved on a page: a panel's row, the save bar's settings, each marked by the part of the page
// that holds it. Leaving the page (another page, another project, the browser's tab closed) asks first while any is
// marked; another tab of the same page is no leaving.

import { useEffect, useSyncExternalStore } from 'react'

const marked = new Set<string>()
const listeners = new Set<() => void>()

/** Marks `id` as holding an edit not saved, or no longer; those that listen hear of a change. */
export function markUnsaved(id: string, on: boolean): void {
  if (marked.has(id) === on) return
  if (on) marked.add(id)
  else marked.delete(id)
  for (const listener of listeners) listener()
}

/** Whether any part of the page holds an edit not saved. */
export function anyUnsaved(): boolean {
  return marked.size > 0
}

export function subscribeUnsaved(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** Marks `id` while `on`, and lets it go when the part of the page that holds it goes. */
export function useUnsaved(id: string, on: boolean): void {
  useEffect(() => {
    markUnsaved(id, on)
    return () => markUnsaved(id, false)
  }, [id, on])
}

/** Whether any part of the page holds an edit not saved, as it changes. */
export function useAnyUnsaved(): boolean {
  return useSyncExternalStore(subscribeUnsaved, anyUnsaved, () => false)
}

/** Whether a move leaves the page: another path, or another query (another project); a hash alone (a tab) stays. */
export function leavesPage(from: { pathname: string; search: string }, to: { pathname: string; search: string }): boolean {
  return from.pathname !== to.pathname || from.search !== to.search
}
