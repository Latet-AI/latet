import { useCallback, useEffect, useEffectEvent, useState } from 'react'

export interface AsyncState<T> {
  data: T | undefined
  error: unknown
  /** True until the first answer for the current key arrives. */
  loading: boolean
  /** True when `data` belongs to an earlier key or reload (refetch keeps the frame). */
  stale: boolean
  reload: () => void
  /** Update the cached data locally (after a mutation). */
  mutate: (update: (data: T | undefined) => T | undefined) => void
}

interface Store<T> {
  key: string | null
  data: T | undefined
  error: unknown
}

/**
 * Load data for `key` (null skips loading). A new key or `reload()` refetches; the previous data
 * stays available, flagged `stale`, until the new answer arrives.
 */
export function useAsync<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>): AsyncState<T> {
  const [store, setStore] = useState<Store<T>>({ key: null, data: undefined, error: undefined })
  const [generation, setGeneration] = useState(0)
  const fullKey = key === null ? null : `${key}#${generation}`
  const run = useEffectEvent((signal: AbortSignal) => load(signal))

  useEffect(() => {
    if (fullKey === null) return
    const controller = new AbortController()
    run(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setStore({ key: fullKey, data, error: undefined })
      },
      (error: unknown) => {
        if (!controller.signal.aborted) setStore((prev) => ({ key: fullKey, data: prev.data, error }))
      },
    )
    return () => controller.abort()
  }, [fullKey])

  const reload = useCallback(() => setGeneration((g) => g + 1), [])
  const mutate = useCallback(
    (update: (data: T | undefined) => T | undefined) => setStore((prev) => ({ ...prev, data: update(prev.data) })),
    [],
  )

  const current = store.key === fullKey
  return {
    data: store.data,
    error: current ? store.error : undefined,
    loading: fullKey !== null && !current && store.data === undefined,
    stale: fullKey !== null && !current,
    reload,
    mutate,
  }
}
