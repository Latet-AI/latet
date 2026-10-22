// Why a Latet server did not keep a save of your settings, or builds no rules for a project: the daemon's `reason`, as
// the dashboard words it.

import { ApiError } from '../api/client'
import type { ProjectNotice } from '../api/types'
import i18n, { tk } from '../i18n'

type Translate = (key: string, options?: Record<string, unknown>) => string

const REFUSED_SAVES: Record<string, string> = {
  settings_stale: tk('errors.settingsStale'), // they changed since the page read them, here or elsewhere (409)
  settings_too_large: tk('errors.settingsTooLarge'), // more than a server keeps for one person (413)
  settings_terms_too_long: tk('errors.settingsTermsTooLong'), // a term exceeds the server's accepted length (409)
  settings_too_many_rules: tk('errors.settingsTooManyRules'), // more recognizers or patterns than it keeps (409)
  settings_too_many_projects: tk('errors.settingsTooManyProjects'), // more projects than it keeps for one person (409)
  settings_drops_shared: tk('errors.settingsDropsShared'), // it would remove items set for everyone: `dropped` counts them (409)
  settings_unavailable: tk('errors.settingsUnavailable'), // the server's store did not answer (503)
  settings_change_waits: tk('errors.settingsChangeWaits'), // a change to your settings waits for your decision (409)
  repositories_unchecked: tk('errors.repositoriesUnchecked'), // a repository Latet has seen could not be read now (503)
  repo_file_unreadable: tk('errors.repoFileUnreadable'), // the project's latet.yaml is not a regular file, or too large (409)
  repo_file_unavailable: tk('errors.repoFileUnavailable'), // the project's latet.yaml cannot be read now (503)
}

const NOTICES: Record<string, string> = {
  space_past_share: tk('projects.pastShare'), // your space holds its share of the server's memory: no rules built
  project_unbuilt: tk('projects.unbuilt'), // its rules could not be built: its texts are refused until they can be
  repo_cannot_set: tk('projects.repoCannotSet'), // a latet.yaml sets what only your own settings for the project may set
  repo_whole_latet: tk('projects.repoWholeLatet'), // a latet.yaml sets a setting of the whole of Latet (its port...)
  repo_unknown: tk('projects.repoUnknown'), // a latet.yaml sets what Latet does not know
}

// A field of a save refused (`details[].reason`), on a Latet server.
const DETAILS: Record<string, string> = {
  destination_refused: tk('errors.destinationRefused'), // a destination of your own that only the server would reach
  reviewer_key_server: tk('errors.reviewerKeyServer'), // a setting that names where the local reviewer's key is read from
}

// The lists a save may remove items set for everyone from: the destinations alone, since a Latet server sets no rule
// for everyone in the Community Edition (a count for any other list is not worded).
const DROPPED = {
  upstreams: tk('errors.dropped.upstreams'),
} as const satisfies Record<string, string>

const reasonOf = (body: unknown): unknown =>
  body !== null && typeof body === 'object' ? (body as { reason?: unknown }).reason : undefined

/** The translation key of why a save of your settings was not kept on a Latet server (none of it applies); null for
 * any other error, which is worded as usual. */
export function refusedSave(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const reason = reasonOf(error.body)
  return typeof reason === 'string' ? (REFUSED_SAVES[reason] ?? null) : null
}

/** Why a save of your settings was not kept, in the reader's language, with the counts its words name; null for any
 * other error. */
export function refusedWords(t: Translate, error: unknown): string | null {
  const key = refusedSave(error)
  if (key === null) return null
  return t(key, { items: droppedItems(t, (error as ApiError).body) })
}

/** The settings were kept, but the pending approval could not be refreshed. This is not a refused save and must
 * never make a caller replay the mutation or remove that approval from its queue. */
export function savedChangePending(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && reasonOf(error.body) === 'settings_saved_change_pending'
    && (error.body as { saved?: unknown }).saved === true
}

export function savedChangePendingWords(t: Translate, error: unknown): string | null {
  return savedChangePending(error) ? t(tk('errors.settingsSavedChangePending')) : null
}

/** What a save would have removed of what is set for everyone, counted by kind (never which items): "2
 * destinations". */
function droppedItems(t: Translate, body: unknown): string {
  const dropped = body !== null && typeof body === 'object' ? (body as { dropped?: unknown }).dropped : undefined
  if (dropped === null || typeof dropped !== 'object') return ''
  const counts = dropped as Record<string, unknown>
  const named = (Object.keys(DROPPED) as (keyof typeof DROPPED)[])
    .filter((key) => typeof counts[key] === 'number' && (counts[key] as number) > 0)
    .map((key) => t(DROPPED[key], { count: counts[key] }))
  try {
    return new Intl.ListFormat(i18n.resolvedLanguage ?? 'en', { type: 'conjunction' }).format(named)
  } catch {
    return named.join(', ')
  }
}

/** Whether a save was refused because your settings changed since the page read them (409, `settings_stale`):
 * nothing was saved, and the page reads them again. */
export function stale(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && reasonOf(error.body) === 'settings_stale'
}

/** After a refused save: when it was refused because your settings changed since the page read them, each of `reads`
 * reads them again, so that what the page shows is what is in use and the change can be made anew; whether it was. */
export function whenStale(error: unknown, ...reads: (() => void)[]): boolean {
  if (!stale(error)) return false
  for (const read of reads) read()
  return true
}

/** The translation key of a project's notice the daemon gives a reason for; null for any other, shown in its words. */
export function noticeReason(notice: ProjectNotice): string | null {
  return typeof notice.reason === 'string' ? (NOTICES[notice.reason] ?? null) : null
}

/** The translation key of a field of a save refused for a reason the daemon gives (`details[].reason`); null for any
 * other, shown in its words. */
export function detailReason(detail: unknown): string | null {
  const reason = reasonOf(detail)
  return typeof reason === 'string' ? (DETAILS[reason] ?? null) : null
}

/** What the words of a refused field name: the setting (its place, `loc`), and a destination's address (`url`). */
export function detailValues(detail: unknown): { setting: string; url: string } {
  const { loc, url } = (detail !== null && typeof detail === 'object' ? detail : {}) as { loc?: unknown; url?: unknown }
  return { setting: Array.isArray(loc) ? loc.join('.') : '', url: typeof url === 'string' ? url : '' }
}
