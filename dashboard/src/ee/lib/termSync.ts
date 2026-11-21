// Term sync (E6): how the page shows each source, the selection of terms to remove, the syncs asked for from the page
// and what they gave, which sources read with a secret, and how a refused change of the sources is said.

import { ApiError } from '../../api/client'
import { tk } from '../../i18n'
import type { TermSourceSettings, TermSyncRun, TermSyncSource } from '../types'

type Translate = (key: string, options?: Record<string, unknown>) => string

export type SourceState = 'ok' | 'failed' | 'never' | 'retired'

/** How a source's last sync went: `never` before its first, `retired` once it is no longer set up. */
export function sourceState(source: TermSyncSource): SourceState {
  if (!source.configured) return 'retired'
  return source.last?.outcome ?? 'never'
}

/** A source's terms by type, the most first, then by name. */
export function byType(counts: Readonly<Record<string, number>>): [string, number][] {
  return Object.entries(counts).sort(([a, x], [b, y]) => y - x || a.localeCompare(b))
}

/** The selection with one term added, or taken away. */
export function toggle(selected: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(selected)
  if (!next.delete(id)) next.add(id)
  return next
}

/** The selection kept when the list changes: only the terms still listed. */
export function keepListed(selected: ReadonlySet<string>, ids: readonly string[]): Set<string> {
  return new Set(ids.filter((id) => selected.has(id)))
}

/** Whether every term listed is selected (and one is listed). */
export function allSelected(ids: readonly string[], selected: ReadonlySet<string>): boolean {
  return ids.length > 0 && ids.every((id) => selected.has(id))
}

/** Whether a sync asked for from this page (`asked`, the sources' names) is still to run: a source whose ask stands. */
export function stillRunning(sources: readonly TermSyncSource[], asked: readonly string[]): boolean {
  return sources.some((s) => asked.includes(s.name) && s.asked !== null)
}

/** When the last run of each source named began, as the page read them when it asked for a sync (null: never run). */
export function lastStarted(sources: readonly TermSyncSource[], names: readonly string[]): Record<string, number | null> {
  return Object.fromEntries(names.map((name) => [name, sources.find((s) => s.name === name)?.last?.started ?? null]))
}

/**
 * The runs a sync asked for gave, once there: of each source asked for whose ask no longer stands, its last run when it
 * began after the one it had when it was asked (`before`, from `lastStarted`). None for a source whose ask was taken
 * with no run since (nothing ran: the page has nothing to tell), nor for one taken out.
 */
export function newRuns(
  sources: readonly TermSyncSource[],
  asked: readonly string[],
  before: Readonly<Record<string, number | null>>,
): TermSyncRun[] {
  return asked.flatMap((name) => {
    const source = sources.find((s) => s.name === name)
    if (!source || source.asked !== null || !source.last || source.last.started === before[name]) return []
    return [source.last]
  })
}

/** A sync's result, as its toast says it: its numbers formatted (`number`), the count of terms choosing the plural. */
export function ranText(t: Translate, number: (n: number) => string, run: TermSyncRun): string {
  return run.outcome === 'ok'
    ? t('ee.termSync.ran', { source: run.source, count: run.found, n: number(run.found), added: number(run.added), gone: number(run.gone) })
    : t('ee.termSync.runFailed', { source: run.source, reason: run.reason ?? '' })
}

/** The 2-second reads after a sync was asked for last a minute; then every 15 seconds, for 15 minutes in all (the
 * lease's turns: a run that has not shown by then is not waited for). */
export const QUICK_MS = 2000
export const QUICK_FOR_MS = 60000
export const SLOW_MS = 15000
export const FOLLOWED_FOR_MS = 15 * 60000

/** How long until the next read, `elapsed` milliseconds after the page asked. */
export function pace(elapsed: number): number {
  return elapsed >= QUICK_FOR_MS ? SLOW_MS : QUICK_MS
}

/** The syncs the page asked for and follows: each source's last run when it was asked (`lastStarted`), when the page
 * last asked, and whether it said it was still syncing, and that a read failed. */
export interface Following {
  before: Record<string, number | null>
  since: number
  late: boolean
  failed: boolean
}

/** What a read of the state gave: the sources, or an error, `stop` when reading again changes nothing (a refusal). */
export type FollowRead = { sources: readonly TermSyncSource[] } | { error: 'again' | 'stop' }

/** What a read means for the syncs followed: the sources whose ask no longer stands (`done`), the new runs to tell,
 * what to say (`stillRunning` once past a minute, `failed` once for a read that failed, `notShown` at the bound), and
 * whether to stop or when to read again. */
export interface Followed {
  done: string[]
  runs: TermSyncRun[]
  say: ('stillRunning' | 'failed' | 'notShown')[]
  late: boolean
  failed: boolean
  stop: boolean
  wait: number | null
}

/** One read of the syncs followed, `now` (milliseconds): see `Followed`. */
export function followed(current: Following, read: FollowRead, now: number): Followed {
  const elapsed = now - current.since
  const over = elapsed >= FOLLOWED_FOR_MS
  if ('error' in read) {
    const say: Followed['say'] = current.failed ? [] : ['failed']
    const stop = read.error === 'stop' || over
    if (over && read.error !== 'stop') say.push('notShown')
    return { done: [], runs: [], say, late: current.late, failed: true, stop, wait: stop ? null : pace(elapsed) }
  }
  const names = Object.keys(current.before)
  const done = names.filter((name) => !stillRunning(read.sources, [name]))
  const runs = newRuns(read.sources, done, current.before)
  const base = { done, runs, failed: current.failed }
  if (done.length === names.length) return { ...base, say: [], late: current.late, stop: true, wait: null }
  if (over) return { ...base, say: ['notShown'], late: current.late, stop: true, wait: null }
  const late = !current.late && elapsed >= QUICK_FOR_MS
  return { ...base, say: late ? ['stillRunning'] : [], late: current.late || late, stop: false, wait: pace(elapsed) }
}

/** Whether a source reads with a secret, as the server weighs it: a graph source always, an ldap source that signs in
 * (`bind`), a url source that writes `{secret}` in its address or a header. */
export function needsSecret(source: Pick<TermSourceSettings, 'kind' | 'url' | 'headers' | 'bind'>): boolean {
  if (source.kind === 'graph') return true
  if (source.kind === 'ldap') return typeof source.bind === 'string'
  if (source.kind === 'url') return `${source.url ?? ''}${Object.values(source.headers ?? {}).join('')}`.includes('{secret}')
  return false
}

const LOOPBACK = ['localhost', '127.0.0.1', '[::1]']

/**
 * Whether a source reaches the server's own computer (a url or ldap source at localhost, 127.0.0.1 or [::1], a trailing
 * dot and the case aside): it is set, with its secret, with `latet server terms` alone, never from the dashboard.
 */
export function localSource(source: Pick<TermSourceSettings, 'kind' | 'url'>): boolean {
  if ((source.kind !== 'url' && source.kind !== 'ldap') || typeof source.url !== 'string') return false
  const host = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/(?:[^/?#@]*@)?(\[[^\]]*\]|[^/?#:]*)/.exec(source.url)?.[1] ?? ''
  return LOOPBACK.includes(host.toLowerCase().replace(/\.+$/, ''))
}

/**
 * What the page says of a change of the sources, or of a secret, the server refused: `changed`, they changed since the
 * page read them (409 `sources-changed`); `byCommand`, a source at the server's own computer (403); `company`, a
 * session the company's sign-in did not open (403); `tooMany`, past the destinations a person adds or moves in an hour
 * (429); `refused`, the server's own words, which say where and what kind of error, never what was typed (400, 404, 409,
 * 413); `failed`, any other error, said as the page says errors.
 */
export type SourcesRefusal = 'changed' | 'byCommand' | 'company' | 'tooMany' | 'refused' | 'failed'

export function sourcesRefusal(error: unknown): SourcesRefusal {
  if (!(error instanceof ApiError)) return 'failed'
  const body = error.body !== null && typeof error.body === 'object' ? (error.body as { code?: unknown }) : {}
  if (error.status === 409 && body.code === 'sources-changed') return 'changed'
  if (error.status === 403 && body.code === 'by-command') return 'byCommand'
  if (error.status === 403 && body.code === 'company-sign-in') return 'company'
  if (error.status === 429) return 'tooMany'
  if ([400, 404, 409, 413].includes(error.status)) return 'refused'
  return 'failed'
}

/** The page's words for a refusal it words itself, of a change of the sources or of a secret: a source at the server's
 * own computer names the command that sets it (the sources', or the secret's); null for the others. */
export function refusedKey(why: SourcesRefusal, of: 'sources' | 'secret'): string | null {
  if (why === 'byCommand') return of === 'secret' ? tk('ee.termSync.secret.byCommand') : tk('ee.termSync.sources.byCommand')
  if (why === 'tooMany') return tk('ee.termSync.sources.tooMany')
  return null
}
