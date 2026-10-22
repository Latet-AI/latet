import { createContext, useCallback, useContext } from 'react'
import type { Me } from '../api/types'
import { inferClass, type ViewClass } from './labels'
import { deploymentKey } from './deployment'

export interface MeState {
  me: Me | undefined
  /** The last GET /me failed (the daemon is not answering). */
  unreachable: boolean
  reload: () => void
  /** Types of the user's custom terms (from GET /config), to color them as custom terms. */
  customTypes: ReadonlySet<string>
  reloadConfig: () => void
}

export const MeContext = createContext<MeState>({
  me: undefined,
  unreachable: false,
  reload: () => undefined,
  customTypes: new Set(),
  reloadConfig: () => undefined,
})

/** The daemon this dashboard talks to (`GET /me`). */
export function useMe(): MeState {
  return useContext(MeContext)
}

/** Select location-sensitive words only after /me has established their location. */
export function useDeploymentKey(): (key: string) => string {
  const { me, unreachable } = useMe()
  return useCallback((key: string) => deploymentKey(me, key, unreachable), [me, unreachable])
}

/** The class of a type when only the type is known (summaries and stats carry types only). */
export function useTypeClass(): (type: string) => ViewClass {
  const { customTypes } = useMe()
  return useCallback((type: string) => inferClass(type, customTypes), [customTypes])
}
