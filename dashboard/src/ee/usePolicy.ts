import { currentSpace, get } from '../api/client'
import type { Me } from '../api/types'
import { useMe } from '../lib/me'
import { useAsync } from '../lib/useAsync'
import type { PolicyView } from './lib/policy'

/**
 * Whether the policy is asked: `/me` says there is one (or what is wrong with it), and this is your own space. In an
 * API key's space every Enterprise route is a person's and answers a key 403, so the locks are not shown there; a save
 * below the policy is refused all the same.
 */
export function asksPolicy(me: Me | undefined, space: string | null): boolean {
  return Boolean(me?.edition?.policy) && space === null
}

/** The company's policy when one is in force or said to be wrong (Latet Enterprise): null otherwise, and while it
 * loads. Every person reads it, as a person is told it (what is locked, never a value): their Rules page shows its
 * locks. `reload` after a save: what is below it may have changed. */
export function usePolicy(): { view: PolicyView | null; reload: () => void } {
  const { me } = useMe()
  const policy = useAsync(asksPolicy(me, currentSpace()) ? 'ee-policy' : null, (signal) => get<PolicyView>('/ee/policy', signal))
  return { view: policy.data ?? null, reload: policy.reload }
}
