// The lists of the rules as rows of a table: each item with where it comes from (the global rules, your own settings for
// a project, its latet.yaml) and a key no other row has; then what a table shows of them, searched, filtered, sorted and
// paged, and what a selection becomes when the search changes.

import type { Policy, RecognizerEntry, Term } from '../api/types'

/** Where a row comes from: the global rules, your own settings for a project, or its repository file. */
export type RuleSource = 'global' | 'personal' | 'repo'

/** The order the sources come in, a project's rows of yours first. */
export const SOURCES: readonly RuleSource[] = ['global', 'personal', 'repo']

/** One item of a list, with where it comes from and a key unique across sources and kinds. */
export interface RuleRow<T> {
  key: string
  source: RuleSource
  item: T
}

/** What a table is asked to show: its search, a value per filter (`''`: any), the column it sorts by, its page. */
export interface TableQuery {
  search: string
  filters: Record<string, string>
  sort: { column: string; dir: 'asc' | 'desc' } | null
  page: number
}

/** A column as the query reads it: its text for a row (what the search finds, a filter compares, the sort orders). */
export interface Column<T> {
  id: string
  text: (row: RuleRow<T>) => string
  filter?: true
  /** False: the search does not read it, a column of words every row has (yes or no, where a row comes from, a
   * state), which a filter picks; a short search would find nearly every row there. */
  searched?: false
}

export const PER_PAGE = 50

/** A value never masked, or a pattern of such values: one table holds both. */
export interface AllowItem {
  kind: 'value' | 'pattern'
  value: string
}

/** What happens to a type found: a policy (a criterion) per type. */
export interface PolicyItem {
  type: string
  policy: Policy
}

/** Lower case and without accents, so that "prufer" finds "Prüfer". */
const folded = (text: string) => text.normalize('NFD').replace(/\p{M}/gu, '').toLocaleLowerCase()

/**
 * The rows a table shows: those holding the search (any case) in a column's text and equal to each filter set, sorted
 * (stable), and the page asked for, kept between the first and the last; `all`, every row matched (what an export writes).
 */
export function queried<T>(
  rows: RuleRow<T>[],
  query: TableQuery,
  columns: Column<T>[],
): { rows: RuleRow<T>[]; matched: number; page: number; pages: number; all: RuleRow<T>[] } {
  const search = folded(query.search.trim())
  const byId = new Map(columns.map((column) => [column.id, column]))
  const filters = Object.entries(query.filters).filter(([id, value]) => value !== '' && byId.has(id))
  let found = rows.filter(
    (row) =>
      filters.every(([id, value]) => byId.get(id)?.text(row) === value) &&
      (search === '' || columns.some((column) => column.searched !== false && folded(column.text(row)).includes(search))),
  )
  const sortBy = query.sort ? byId.get(query.sort.column) : undefined
  if (query.sort && sortBy) {
    const sign = query.sort.dir === 'asc' ? 1 : -1
    found = [...found].sort((a, b) => sign * sortBy.text(a).localeCompare(sortBy.text(b), undefined, { numeric: true }))
  }
  const pages = Math.max(1, Math.ceil(found.length / PER_PAGE))
  const page = Math.min(Math.max(1, Math.trunc(query.page) || 1), pages)
  return { rows: found.slice((page - 1) * PER_PAGE, page * PER_PAGE), matched: found.length, page, pages, all: found }
}

/**
 * The filters of a table as its menus allow them now: one whose value left its menu (the rows that had it were deleted,
 * turned on, changed) is let go, back to any, instead of filtering on a value its menu no longer shows. The same object
 * when nothing is let go.
 */
export function filtersKept(filters: Record<string, string>, menus: readonly { column: string; options: readonly { value: string }[] }[]): Record<string, string> {
  const gone = Object.entries(filters).filter(([column, value]) => {
    if (value === '') return false
    const menu = menus.find((m) => m.column === column)
    return menu !== undefined && !menu.options.some((option) => option.value === value)
  })
  if (gone.length === 0) return filters
  return { ...filters, ...Object.fromEntries(gone.map(([column]) => [column, ''])) }
}

/** A selection after the query changed: let go when the search or a filter changed, kept for a page or a sort. */
export function selectionAfter(prev: TableQuery, next: TableQuery, selected: Set<string>): Set<string> {
  const filters = (query: TableQuery) =>
    JSON.stringify(
      Object.entries(query.filters)
        .filter(([, value]) => value !== '')
        .sort(([a], [b]) => a.localeCompare(b)),
    )
  if (prev.search !== next.search || filters(prev) !== filters(next)) return new Set()
  return selected
}

/** The rows of `items` per source, in the order of the sources, keyed `source:kind:identity` (`~2` for a second one). */
function rowsOf<T>(kind: (item: T) => string, identity: (item: T, index: number) => string, sources: Partial<Record<RuleSource, T[]>>): RuleRow<T>[] {
  const rows: RuleRow<T>[] = []
  for (const source of SOURCES) {
    const seen = new Map<string, number>()
    ;(sources[source] ?? []).forEach((item, index) => {
      const base = `${source}:${kind(item)}:${identity(item, index)}`
      const count = (seen.get(base) ?? 0) + 1
      seen.set(base, count)
      rows.push({ key: count === 1 ? base : `${base}~${count}`, source, item })
    })
  }
  return rows
}

/** A term by its type and its value, as the daemon tells terms apart (a type holds no colon). */
export function termRows(sources: Partial<Record<RuleSource, Term[]>>): RuleRow<Term>[] {
  return rowsOf(() => 'term', (term) => `${term.type.toUpperCase()}:${term.value}`, sources)
}

/** A recognizer by its name; one without a name (a broken entry) by its place. */
export function recognizerRows(sources: Partial<Record<RuleSource, RecognizerEntry[]>>): RuleRow<RecognizerEntry>[] {
  return rowsOf(
    () => 'recognizer',
    (entry, index) => (typeof entry.name === 'string' && entry.name !== '' ? entry.name : `#${index + 1}`),
    sources,
  )
}

/** The values, then the patterns, of each source. */
export function allowRows(sources: Partial<Record<RuleSource, { values: string[]; patterns: string[] }>>): RuleRow<AllowItem>[] {
  const items: Partial<Record<RuleSource, AllowItem[]>> = {}
  for (const source of SOURCES) {
    const lists = sources[source]
    if (lists) items[source] = allowItems(lists.values, lists.patterns)
  }
  return allowItemRows(items)
}

/** The values and patterns of each source, a row each in their order. */
export function allowItemRows(sources: Partial<Record<RuleSource, AllowItem[]>>): RuleRow<AllowItem>[] {
  return rowsOf((item) => item.kind, (item) => item.value, sources)
}

/** The values and the patterns as one list, the values first. */
export function allowItems(values: readonly string[], patterns: readonly string[]): AllowItem[] {
  return [...values.map((value) => ({ kind: 'value' as const, value })), ...patterns.map((value) => ({ kind: 'pattern' as const, value }))]
}

export function policyRows(sources: Partial<Record<RuleSource, Record<string, Policy>>>): RuleRow<PolicyItem>[] {
  const items: Partial<Record<RuleSource, PolicyItem[]>> = {}
  for (const source of SOURCES) {
    const policies = sources[source]
    if (policies) items[source] = Object.entries(policies).map(([type, policy]) => ({ type, policy }))
  }
  return policyItemRows(items)
}

/** The policies of each source, a row each in their order. */
export function policyItemRows(sources: Partial<Record<RuleSource, PolicyItem[]>>): RuleRow<PolicyItem>[] {
  return rowsOf(() => 'policy', (item) => item.type, sources)
}

/**
 * The items of `source` without the rows of `keys`, in their order: `rows` are rows made from these items (and maybe
 * other sources'), so the rows of `source` follow its items one by one.
 */
export function withoutKeys<T>(items: T[], rows: RuleRow<T>[], keys: ReadonlySet<string>, source: RuleSource): T[] {
  const own = rows.filter((row) => row.source === source)
  return items.filter((_, index) => {
    const row = own[index]
    return row === undefined || !keys.has(row.key)
  })
}
