// A list of rules as a table built for hundreds of rows, as the Vault's: its own search, filters that fit the list,
// sortable columns, pages of fifty, a checkbox per row with actions on the rows selected, and Add. The rows are text to
// read: a click or Enter on one opens it in a panel, where it is edited (RulePanel).

import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { pageNumbers } from '../lib/pages'
import { filtersKept, queried, selectionAfter, type Column, type RuleRow, type TableQuery } from '../lib/ruleTable'
import { Icon } from './Icon'
import { useToast } from './Toast'
import { ConfirmDialog, errorText } from './ui'

/** A column of the table: its header, and its cell (the text of `text`, cut to one line, by default). */
export interface RuleColumn<T> extends Column<T> {
  header: string
  cell?: (row: RuleRow<T>) => ReactNode
  sortable?: true
  /** The classes of its cells: their width, a font. */
  className?: string
  /** Not shown: a filter and the search read it (a recognizer's class). */
  hidden?: true
}

/** A filter: a menu of the values a column may equal. */
export interface RuleFilter {
  column: string
  label: string
  options: { value: string; label: string }[]
}

/** What can be done to the rows selected: applied at once, after a confirmation (`confirm`: its words; none: ask none). */
export interface RuleAction {
  id: string
  label: string
  danger?: true
  confirm?: (count: number, keys: Set<string>) => string
  /** Done once it resolves, and the selection let go; `false`: nothing was done (a dialog cancelled), it stays. */
  run: (keys: Set<string>) => Promise<void | boolean>
}

const EMPTY_QUERY: TableQuery = { search: '', filters: {}, sort: null, page: 1 }

/** A text cut to the width of its cell, whole in its title: a row is one line, however long its value. */
export function Truncated({ text, className = '' }: { text: string; className?: string }) {
  return (
    <span className={`block max-w-[44ch] truncate ${className}`} title={text}>
      {text}
    </span>
  )
}

export function RuleTable<T>({
  label,
  rows,
  columns,
  filters,
  actions,
  onOpen,
  onAdd,
  addLabel,
  extra,
  empty,
  selectable = () => true,
  opened = null,
  search: outside = '',
}: {
  /** What the list is (Terms): the table's name for a screen reader. */
  label: string
  rows: RuleRow<T>[]
  columns: RuleColumn<T>[]
  filters: RuleFilter[]
  actions: RuleAction[]
  onOpen: (row: RuleRow<T>) => void
  onAdd?: () => void
  addLabel?: string
  /** More in the toolbar (Import, Export): of the rows the search and the filters keep, on every page. */
  extra?: ReactNode | ((matched: RuleRow<T>[]) => ReactNode)
  /** What an empty list says. */
  empty: string
  /** Whether a row can be selected (a row of the global rules shown in a project cannot: it is edited there). */
  selectable?: (row: RuleRow<T>) => boolean
  /** The key of the row open in the panel, marked. */
  opened?: string | null
  /** The search at the top of Rules: while it holds text, the table shows what it finds, and has no search of its own. */
  search?: string
}) {
  const { t } = useTranslation()
  const toast = useToast()
  const [query, setQueryState] = useState<TableQuery>(EMPTY_QUERY)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [confirming, setConfirming] = useState<RuleAction | null>(null)
  const [busy, setBusy] = useState(false)
  const pageBox = useRef<HTMLInputElement>(null)
  const searching = outside.trim() !== ''
  const effective: TableQuery = searching ? { ...query, search: outside } : query
  // A new search at the top: the selection goes and the first page shows, as for a search of the table's own.
  const [lastOutside, setLastOutside] = useState(outside)
  if (lastOutside !== outside) {
    setLastOutside(outside)
    setSelected(new Set())
    setQueryState((current) => ({ ...current, page: 1 }))
  }

  const setQuery = (next: TableQuery) => {
    setSelected((current) => selectionAfter(query, next, current))
    setQueryState(next)
  }
  // A filter whose value left its menu (its rows deleted or turned on) goes back to any, as its menu shows.
  const kept = filtersKept(query.filters, filters)
  if (kept !== query.filters) setQueryState({ ...query, filters: kept, page: 1 })
  const shown = queried(rows, effective, columns)
  const visible = columns.filter((column) => !column.hidden)
  // Of the rows selected, those still in the list (a save may have removed some).
  const keys = new Set(rows.map((row) => row.key))
  const chosen = new Set([...selected].filter((key) => keys.has(key)))
  const pageChoosable = shown.rows.filter(selectable)
  const pageAll = pageChoosable.length > 0 && pageChoosable.every((row) => chosen.has(row.key))
  const pageSome = pageChoosable.some((row) => chosen.has(row.key))
  useEffect(() => {
    if (pageBox.current) pageBox.current.indeterminate = pageSome && !pageAll
  }, [pageSome, pageAll])

  const toggle = (key: string, on: boolean) =>
    setSelected(() => {
      const next = new Set(chosen)
      if (on) next.add(key)
      else next.delete(key)
      return next
    })
  const togglePage = (on: boolean) =>
    setSelected(() => {
      const next = new Set(chosen)
      for (const row of pageChoosable) {
        if (on) next.add(row.key)
        else next.delete(row.key)
      }
      return next
    })
  const filtered = effective.search.trim() !== '' || Object.values(effective.filters).some((value) => value !== '')

  const act = async (action: RuleAction) => {
    setBusy(true)
    try {
      const done = await action.run(chosen)
      if (done !== false) setSelected(new Set())
      setConfirming(null)
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    } finally {
      setBusy(false)
    }
  }
  const start = (action: RuleAction) => {
    if (action.confirm) setConfirming(action)
    else void act(action)
  }
  const sortOf = (id: string) => (effective.sort?.column === id ? effective.sort.dir : null)
  const sortBy = (id: string) => {
    const dir = sortOf(id) === 'asc' ? 'desc' : 'asc'
    setQuery({ ...query, sort: { column: id, dir }, page: 1 })
  }
  const openKey = (event: KeyboardEvent<HTMLTableRowElement>, row: RuleRow<T>) => {
    if (event.key === 'Enter' && event.target === event.currentTarget) {
      event.preventDefault()
      onOpen(row)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        {!searching && (
          <label className="flex min-w-[220px] flex-1 flex-col gap-1 sm:max-w-[340px]">
            <span className="label">{t('rules.table.search')}</span>
            <span role="search" className="relative">
              <Icon name="search" size={14} className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-ink-3" />
              <input
                type="search"
                className="field w-full pl-8"
                value={query.search}
                onChange={(e) => setQuery({ ...query, search: e.target.value, page: 1 })}
                autoComplete="off"
                spellCheck={false}
              />
            </span>
          </label>
        )}
        {filters.map((filter) => (
          <label key={filter.column} className="flex flex-col gap-1">
            <span className="label">{filter.label}</span>
            <select
              className="field max-w-[220px]"
              value={query.filters[filter.column] ?? ''}
              onChange={(e) => setQuery({ ...query, filters: { ...query.filters, [filter.column]: e.target.value }, page: 1 })}
            >
              <option value="">{t('rules.table.any')}</option>
              {filter.options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        ))}
        <p className="pb-1.5 text-meta text-ink-3" role="status">
          {filtered ? t('rules.table.matched', { matched: shown.matched, count: rows.length }) : t('rules.table.count', { count: rows.length })}
        </p>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {typeof extra === 'function' ? extra(shown.all) : extra}
          {onAdd && (
            <button type="button" className="btn" onClick={onAdd}>
              <Icon name="plus" size={14} />
              {addLabel}
            </button>
          )}
        </div>
      </div>

      {chosen.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line-2 bg-wash px-3 py-2" role="toolbar" aria-label={t('rules.table.actions')}>
          <span className="text-ui font-medium text-ink">{t('rules.table.selected', { count: chosen.size })}</span>
          {actions.map((action) => (
            <button
              key={action.id}
              type="button"
              className={`btn btn-sm ${action.danger ? 'btn-quiet-danger' : ''}`}
              disabled={busy}
              onClick={() => start(action)}
            >
              {action.label}
            </button>
          ))}
          <button type="button" className="btn btn-ghost btn-sm ml-auto" onClick={() => setSelected(new Set())}>
            {t('rules.table.clear')}
          </button>
        </div>
      )}

      {rows.length === 0 ? (
        <p className="text-meta text-ink-3">{empty}</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="data-table" aria-label={label}>
            <thead>
              <tr>
                <th scope="col" className="w-9">
                  <input
                    ref={pageBox}
                    type="checkbox"
                    className="mt-1 accent-[var(--ink)]"
                    checked={pageAll}
                    disabled={pageChoosable.length === 0}
                    onChange={(e) => togglePage(e.target.checked)}
                    aria-label={t('rules.table.selectPage')}
                  />
                </th>
                {visible.map((column) => {
                  const dir = sortOf(column.id)
                  return (
                    <th key={column.id} scope="col" aria-sort={dir === 'asc' ? 'ascending' : dir === 'desc' ? 'descending' : undefined}>
                      {column.sortable ? (
                        <button type="button" className="inline-flex items-center gap-1 hover:text-ink" onClick={() => sortBy(column.id)}>
                          {column.header}
                          {dir && <Icon name="chevronDown" size={12} className={dir === 'asc' ? 'rotate-180' : ''} />}
                        </button>
                      ) : (
                        column.header
                      )}
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {shown.rows.map((row) => {
                const name = visible[0]?.text(row) ?? row.key
                return (
                  <tr
                    key={row.key}
                    data-key={row.key}
                    tabIndex={0}
                    aria-current={opened === row.key ? 'true' : undefined}
                    className={`cursor-pointer outline-offset-[-2px] focus-visible:outline-2 focus-visible:outline-[var(--focus)] ${
                      opened === row.key ? '[&>td]:bg-wash' : ''
                    }`}
                    onClick={() => onOpen(row)}
                    onKeyDown={(event) => openKey(event, row)}
                  >
                    <td className="w-9" onClick={(event) => event.stopPropagation()}>
                      {selectable(row) && (
                        <input
                          type="checkbox"
                          className="mt-1 accent-[var(--ink)]"
                          checked={chosen.has(row.key)}
                          onChange={(e) => toggle(row.key, e.target.checked)}
                          aria-label={t('rules.table.select', { row: name })}
                        />
                      )}
                    </td>
                    {visible.map((column) => (
                      <td key={column.id} className={column.className}>
                        {column.cell ? column.cell(row) : <Truncated text={column.text(row)} />}
                      </td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
          {shown.matched === 0 && <p className="px-3 py-4 text-ui text-ink-2">{t('rules.table.noMatch')}</p>}
        </div>
      )}

      {shown.pages > 1 && <Pager page={shown.page} pages={shown.pages} onPage={(page) => setQuery({ ...query, page })} />}

      <ConfirmDialog
        open={confirming !== null}
        title={confirming?.label ?? ''}
        confirmLabel={confirming?.label ?? ''}
        destructive={confirming?.danger === true}
        busy={busy}
        onConfirm={() => confirming && void act(confirming)}
        onCancel={() => setConfirming(null)}
      >
        {confirming?.confirm?.(chosen.size, chosen)}
      </ConfirmDialog>
    </div>
  )
}

/** The pages of a table, as the Vault's are drawn, without changing the address (the Rules page's names its tab). */
function Pager({ page, pages, onPage }: { page: number; pages: number; onPage: (page: number) => void }) {
  const { t } = useTranslation()
  return (
    <nav aria-label={t('pagination.label')} className="flex flex-wrap items-center justify-center gap-1">
      <button type="button" className="btn btn-ghost btn-sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        <Icon name="chevronLeft" size={13} />
        {t('pagination.previous')}
      </button>
      {pageNumbers(page, pages).map((number, i) =>
        number === 'gap' ? (
          <span key={`gap-${i}`} className="px-1 text-meta text-ink-3" aria-hidden="true">
            …
          </span>
        ) : (
          <button
            key={number}
            type="button"
            className={`btn btn-sm tabular-nums ${number === page ? 'btn-primary' : 'btn-ghost'}`}
            aria-current={number === page ? 'page' : undefined}
            aria-label={t('pagination.page', { page: number })}
            onClick={() => onPage(number)}
          >
            {number}
          </button>
        ),
      )}
      <button type="button" className="btn btn-ghost btn-sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        {t('pagination.next')}
        <Icon name="chevronRight" size={13} />
      </button>
    </nav>
  )
}
