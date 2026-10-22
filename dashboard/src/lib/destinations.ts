// Destinations: where an agent's requests go after Latet, when they go to a company's gateway, Azure
// OpenAI or Microsoft Foundry rather than straight to the provider.

import type { DestinationUse } from '../api/types'

/** The destinations Claude Code and Codex no longer use (no settings point at them, none registered them since the daemon started): they may be removed. */
export function unused(destinations: readonly DestinationUse[]): DestinationUse[] {
  return destinations.filter((d) => !d.in_use)
}

/** Who Latet signed a Bedrock request with: an AWS profile, or the credentials of `latet claude`'s shell. */
export function signedBy(signed: string | null | undefined): { by: 'profile'; profile: string } | { by: 'session' } | null {
  if (!signed) return null
  if (signed.startsWith('aws-profile:')) return { by: 'profile', profile: signed.slice('aws-profile:'.length) }
  return { by: 'session' }
}

/** An address by its host, for a compact list. */
export function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}
