// Whether the window is narrow (half a screen, a phone): pages that lay out their content another way there ask it.

import { useMemo, useSyncExternalStore } from 'react'

/** The widest window that counts as narrow, in CSS pixels: below 1100 px. */
export const NARROW_MAX = 1099

/** The media query of a window `maxWidth` px wide or less. */
export const narrowQuery = (maxWidth: number) => `(max-width: ${maxWidth}px)`

type MatchMedia = (query: string) => MediaQueryList

/** Whether the window is `maxWidth` px wide or less, read through `matchMedia`, and how to follow it as it changes. */
export function narrowStore(matchMedia: MatchMedia | undefined, maxWidth: number) {
  const list = matchMedia?.(narrowQuery(maxWidth))
  return {
    get: (): boolean => list?.matches ?? false,
    subscribe: (onChange: () => void): (() => void) => {
      list?.addEventListener('change', onChange)
      return () => list?.removeEventListener('change', onChange)
    },
  }
}

/** True while the window is `maxWidth` px wide or less, updated as it is resized. */
export function useNarrow(maxWidth = NARROW_MAX): boolean {
  const store = useMemo(
    () => narrowStore(typeof window !== 'undefined' && window.matchMedia ? (query) => window.matchMedia(query) : undefined, maxWidth),
    [maxWidth],
  )
  // No server renders this app: the window is read the same way on a first render.
  return useSyncExternalStore(store.subscribe, store.get, store.get)
}
