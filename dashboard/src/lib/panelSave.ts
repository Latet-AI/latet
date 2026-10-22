// One change saved from a panel of a rules table: made on the settings as they are now (read again first), so that
// nothing changed meanwhile elsewhere (another tab, a change applied) is written over; made again once when the save
// meets settings changed since, as long as the row it touches is as the panel found it. And the save bar's edits not
// saved yet, carried over onto the settings read after such a save when nothing they touch changed meanwhile.

import { api, ApiError } from '../api/client'
import type { ConfigView, PersonalSettings, ProjectSettings } from '../api/types'
import { draftOf, personalDraftOf, personalOf, settingsOf } from './projects'
import { savedChangePending, stale } from './refused'
import type { RuleRow } from './ruleTable'
import { editable } from './saves'

/** A panel's one change: how it rewrites the settings, and whether the row it touches is now as the panel found it. */
export interface Change<S> {
  apply: (settings: S) => S
  unchanged: (found: S, now: S) => boolean
}

/** How a save ended: saved; kept but waiting for approval; refused because its row changed (as it is now); refused. */
export type SaveOutcome<S = unknown> =
  | { kind: 'saved'; version?: number | string }
  | { kind: 'pending' }
  | { kind: 'conflict'; settings: S }
  | { kind: 'error'; error: unknown }

/** Where a set of settings is read and written: its tag (an etag, a file's tag) goes back with a write. */
export interface SaveIo<S> {
  read: () => Promise<{ settings: S; tag: string }>
  write: (settings: S, tag: string) => Promise<{ version?: number | string }>
  /** Whether a refusal says the settings changed since they were read (by default the daemon's `settings_stale`). */
  stale?: (error: unknown) => boolean
}

/**
 * Saves `change` on the settings as they are now: read, checked (its row as found), written with the tag of that
 * read. Refused as stale, it reads them again and tries once more if its row is still as found.
 */
export async function saveOne<S>(io: SaveIo<S>, found: S, change: Change<S>): Promise<SaveOutcome<S>> {
  const isStale = io.stale ?? stale
  for (let attempt = 0; ; attempt++) {
    let now: { settings: S; tag: string }
    try {
      now = await io.read()
    } catch (error) {
      return { kind: 'error', error }
    }
    if (!change.unchanged(found, now.settings)) return { kind: 'conflict', settings: now.settings }
    try {
      const { version } = await io.write(change.apply(now.settings), now.tag)
      return version === undefined ? { kind: 'saved' } : { kind: 'saved', version }
    } catch (error) {
      if (savedChangePending(error)) return { kind: 'pending' }
      if (attempt === 0 && isStale(error)) continue
      return { kind: 'error', error }
    }
  }
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

/**
 * The save bar's draft carried onto `fresh`, the settings read after another save: each field the draft changed from
 * `base` is kept when `fresh` still has it as `base` did; when `fresh` has it as the draft does, it was saved; else it
 * changed meanwhile, and the edit is dropped and named in `lost`, never saved back over what changed.
 */
export function keepUnsaved<S>(base: S, draft: S, fresh: S, fields: readonly (keyof S)[]): { kept: S; lost: (keyof S)[] } {
  const kept = { ...fresh }
  const lost: (keyof S)[] = []
  for (const field of fields) {
    if (same(draft[field], base[field]) || same(fresh[field], draft[field])) continue
    if (same(fresh[field], base[field])) kept[field] = draft[field]
    else lost.push(field)
  }
  return { kept, lost }
}

/** A list of the settings, as one source's table shows it: how it is read, how it is written, its rows. */
export interface ListAccess<S, T> {
  get: (settings: S) => T[]
  set: (settings: S, items: T[]) => S
  rows: (items: T[]) => RuleRow<T>[]
}

function rowOf<S, T>(list: ListAccess<S, T>, settings: S, key: string): RuleRow<T> | undefined {
  return list.rows(list.get(settings)).find((row) => row.key === key)
}

/** The rows `keys` are in `now` as they were in `found`. */
function rowsUnchanged<S, T>(list: ListAccess<S, T>, keys: Iterable<string>, found: S, now: S): boolean {
  for (const key of keys) {
    const before = rowOf(list, found, key)
    const after = rowOf(list, now, key)
    if (!before || !after || !same(before.item, after.item)) return false
  }
  return true
}

/**
 * The row `key` replaced by `next`; with `key` null, `next` added. Refused when the row changed or went, or when
 * another row took the identity of `next` meanwhile. `foundItem`: the row as the panel found it when it opened, which
 * the row must still be (the page may have read the settings again since, a change of that row among them); without
 * it, the row must be as in the settings given as found.
 */
export function putChange<S, T>(list: ListAccess<S, T>, key: string | null, next: T, foundItem?: T): Change<S> {
  const nextKey = list.rows([next])[0]?.key
  return {
    apply: (settings) => {
      const items = list.get(settings)
      if (key === null) return list.set(settings, [...items, next])
      const rows = list.rows(items)
      return list.set(
        settings,
        items.map((item, index) => (rows[index]?.key === key ? next : item)),
      )
    },
    unchanged: (found, now) => {
      const taken = nextKey !== undefined && nextKey !== key && rowOf(list, now, nextKey) !== undefined
      if (taken) return false
      if (key === null) return true
      if (foundItem === undefined) return rowsUnchanged(list, [key], found, now)
      const after = rowOf(list, now, key)
      return after !== undefined && same(after.item, foundItem)
    },
  }
}

/**
 * What an open panel does when the page reads its row again (`now`; undefined: the row went): nothing when it is as the
 * panel found it; else, without edits, it shows the row as it is now (or closes, the row gone); with edits, it says the
 * row changed and shows it as it is now (or, the row gone, keeps what was typed as a new row).
 */
export function rowChange<T>(found: T, now: T | undefined, dirty: boolean): 'same' | 'refresh' | 'close' | 'changed' | 'gone' {
  if (now !== undefined && same(found, now)) return 'same'
  if (!dirty) return now === undefined ? 'close' : 'refresh'
  return now === undefined ? 'gone' : 'changed'
}

/**
 * What a panel shows when its save was refused because its row changed meanwhile: a new row keeps what was typed (a
 * row of the same name or value came meanwhile); an edited row shows the row as it is now, or, the row gone, keeps what
 * was typed as a new row.
 */
export function afterConflict<T>(isNew: boolean, now: T | undefined): 'taken' | 'changed' | 'gone' {
  if (isNew) return 'taken'
  return now === undefined ? 'gone' : 'changed'
}

/** The rows `keys` removed; refused when one of them changed meanwhile (one gone already is no matter). */
export function removeChange<S, T>(list: ListAccess<S, T>, keys: ReadonlySet<string>): Change<S> {
  return {
    apply: (settings) => {
      const items = list.get(settings)
      const rows = list.rows(items)
      return list.set(
        settings,
        items.filter((_, index) => !keys.has(rows[index]?.key ?? '')),
      )
    },
    unchanged: (found, now) => [...keys].every((key) => !rowOf(list, now, key) || rowsUnchanged(list, [key], found, now)),
  }
}

/** The rows `keys` each rewritten by `update` (turned off, turned on); refused when one of them changed meanwhile. */
export function updateChange<S, T>(list: ListAccess<S, T>, keys: ReadonlySet<string>, update: (item: T) => T): Change<S> {
  return {
    apply: (settings) => {
      const items = list.get(settings)
      const rows = list.rows(items)
      return list.set(
        settings,
        items.map((item, index) => (keys.has(rows[index]?.key ?? '') ? update(item) : item)),
      )
    },
    unchanged: (found, now) => rowsUnchanged(list, keys, found, now),
  }
}

/** Your global rules: read with their etag, written whole as the save bar writes them. */
export function globalIo(): SaveIo<ConfigView> {
  return {
    read: async () => {
      const config = await api.config()
      return { settings: config, tag: config.etag }
    },
    write: async (settings, tag) => ({ version: (await api.saveConfig(editable(settings), tag)).rules_version }),
  }
}

/** Your own settings for a project: the etag of your settings read first, then the project, as `changePersonal` does. */
export function personalIo(projectId: string): SaveIo<PersonalSettings> {
  return {
    read: async () => {
      const { etag } = await api.config()
      const project = await api.project(projectId)
      return { settings: project.personal ?? {}, tag: etag }
    },
    write: async (settings, tag) => ({ version: (await api.savePersonal(projectId, personalOf(personalDraftOf(settings)), tag)).rules_version }),
  }
}

/** A project's latet.yaml found, at the save, to be one the dashboard may not write here (said in the reader's
 * language by SaveRefusal). */
export class RepoNotWritable extends Error {
  constructor() {
    super("this project's latet.yaml cannot be written here")
    this.name = 'RepoNotWritable'
  }
}

/** A project's latet.yaml, with the tag of the file as read; never written where the dashboard may not write it. */
export function repoIo(projectId: string): SaveIo<ProjectSettings> {
  let writable = false
  return {
    read: async () => {
      const project = await api.project(projectId)
      writable = project.repo.writable
      return { settings: project.repo.settings, tag: project.repo.tag }
    },
    write: async (settings, tag) => {
      if (!writable) throw new RepoNotWritable()
      await api.saveRepo(projectId, settingsOf(draftOf(settings), true), tag)
      return {}
    },
    // The file changed since it was read: a 409 without a reason (one with a reason says the file cannot be read).
    stale: (error) =>
      error instanceof ApiError &&
      error.status === 409 &&
      (error.body === undefined || error.body === null || typeof error.body !== 'object' || !('reason' in error.body)),
  }
}
