import type { TFunction } from 'i18next'

/** What the vault's reveal answers: the real value, or, for Latet's own launch key, which is never revealed, a flag. */
export interface Revealed {
  original: string | null
  launch_key?: boolean
}

/**
 * The text a reveal shows: the value as it is, or a plain label in place of a live launch key the vault holds (a
 * masked tool output put it there). That key is Latet's own credential, and seeing it gains a person nothing.
 */
export function revealedText(answer: Revealed, t: TFunction): string | null {
  return answer.launch_key ? t('vault.reveal.launchKey') : answer.original
}
