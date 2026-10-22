// Per-viewer conveniences kept in localStorage (theme, view toggles). Storage can be unavailable
// (private windows, blocked site data): every access is guarded and the defaults always work.

import { useCallback, useState } from 'react'

const PREFIX = 'latet.'

export function readPref<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(PREFIX + key)
    return raw === null ? fallback : (JSON.parse(raw) as T)
  } catch {
    return fallback
  }
}

export function writePref<T>(key: string, value: T): void {
  try {
    window.localStorage.setItem(PREFIX + key, JSON.stringify(value))
  } catch {
    // storage unavailable: the preference lasts for this page only
  }
}

/** useState backed by a stored preference. */
export function usePref<T>(key: string, fallback: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => readPref(key, fallback))
  const update = useCallback(
    (next: T) => {
      setValue(next)
      writePref(key, next)
    },
    [key],
  )
  return [value, update]
}
