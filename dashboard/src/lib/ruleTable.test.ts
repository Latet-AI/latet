import { describe, expect, it } from 'vitest'
import type { RecognizerEntry, Term } from '../api/types'
import {
  allowRows,
  filtersKept,
  PER_PAGE,
  policyRows,
  queried,
  recognizerRows,
  selectionAfter,
  termRows,
  withoutKeys,
  type Column,
  type RuleRow,
  type TableQuery,
} from './ruleTable'

const term = (value: string, type = 'CLIENT'): Term => ({ value, type, case_sensitive: false, whole_word: true })

const TERM_COLUMNS: Column<Term>[] = [
  { id: 'value', text: (row) => row.item.value },
  { id: 'type', text: (row) => row.item.type, filter: true },
  { id: 'source', text: (row) => row.source, filter: true },
]

const query = (patch: Partial<TableQuery> = {}): TableQuery => ({ search: '', filters: {}, sort: null, page: 1, ...patch })

const values = (rows: RuleRow<Term>[]) => rows.map((row) => row.item.value)

describe('queried', () => {
  const rows = termRows({ global: [term('Project Aurora', 'PROJECT'), term('Whitfield & Co'), term('Orion Labs')] })

  it('queried finds in any column, any case', () => {
    expect(values(queried(rows, query({ search: 'aurora' }), TERM_COLUMNS).rows)).toEqual(['Project Aurora'])
    expect(values(queried(rows, query({ search: 'project' }), TERM_COLUMNS).rows)).toEqual(['Project Aurora'])
    expect(values(queried(rows, query({ search: 'CLIENT' }), TERM_COLUMNS).rows)).toEqual(['Whitfield & Co', 'Orion Labs'])
    expect(values(queried(rows, query({ search: '  orion  ' }), TERM_COLUMNS).rows)).toEqual(['Orion Labs'])
    const none = queried(rows, query({ search: 'nothing says this' }), TERM_COLUMNS)
    expect(none).toMatchObject({ rows: [], matched: 0, page: 1, pages: 1 })
  })

  it('queried leaves to the filters a column of words every row has (yes or no, where a row comes from)', () => {
    const columns: Column<Term>[] = [...TERM_COLUMNS, { id: 'case', text: (row) => (row.item.case_sensitive ? 'Yes' : 'No'), filter: true, searched: false }]
    // "no", on the way to "Nova", does not find every term that is not case sensitive.
    expect(values(queried(rows, query({ search: 'no' }), columns).rows)).toEqual([])
    expect(values(queried(rows, query({ filters: { case: 'No' } }), columns).rows)).toEqual(['Project Aurora', 'Whitfield & Co', 'Orion Labs'])
  })

  it('queried filters exactly and pages by 50', () => {
    const many = termRows({ global: Array.from({ length: 120 }, (_, i) => term(`Client ${i}`, i % 2 ? 'CLIENT' : 'CLIENTS')) })
    expect(PER_PAGE).toBe(50)
    const first = queried(many, query(), TERM_COLUMNS)
    expect(first).toMatchObject({ matched: 120, page: 1, pages: 3 })
    expect(first.rows).toHaveLength(50)
    expect(first.rows[0]?.item.value).toBe('Client 0')
    const last = queried(many, query({ page: 9 }), TERM_COLUMNS)
    expect(last).toMatchObject({ page: 3, pages: 3 })
    expect(last.rows).toHaveLength(20)
    expect(last.rows[0]?.item.value).toBe('Client 100')
    expect(queried(many, query({ page: 0 }), TERM_COLUMNS).page).toBe(1)
    // Exact: CLIENT is not CLIENTS.
    const clients = queried(many, query({ filters: { type: 'CLIENT' } }), TERM_COLUMNS)
    expect(clients.matched).toBe(60)
    expect(clients.rows.every((row) => row.item.type === 'CLIENT')).toBe(true)
    expect(queried(many, query({ filters: { type: '' } }), TERM_COLUMNS).matched).toBe(120)
  })

  it('queried sorts stably', () => {
    const mixed = termRows({ global: [term('b', 'Y'), term('a', 'X'), term('c', 'Y'), term('d', 'X')] })
    const byType = queried(mixed, query({ sort: { column: 'type', dir: 'asc' } }), TERM_COLUMNS)
    expect(values(byType.rows)).toEqual(['a', 'd', 'b', 'c'])
    const down = queried(mixed, query({ sort: { column: 'type', dir: 'desc' } }), TERM_COLUMNS)
    expect(values(down.rows)).toEqual(['b', 'c', 'a', 'd'])
    const byValue = queried(mixed, query({ sort: { column: 'value', dir: 'desc' } }), TERM_COLUMNS)
    expect(values(byValue.rows)).toEqual(['d', 'c', 'b', 'a'])
  })

  it('queried gives every row it keeps, on every page, for an export', () => {
    const many = termRows({ global: Array.from({ length: 120 }, (_, i) => term(`Client ${i}`, i % 2 ? 'CLIENT' : 'OTHER')) })
    const shown = queried(many, query({ filters: { type: 'CLIENT' } }), TERM_COLUMNS)
    expect(shown.rows).toHaveLength(50)
    expect(shown.all).toHaveLength(60)
    expect(shown.all.every((row) => row.item.type === 'CLIENT')).toBe(true)
  })

  it('projectRows keeps a row per source', () => {
    const both = termRows({ global: [term('Ashford Paints')], personal: [term('Ashford Paints')], repo: [term('Project Falcon')] })
    expect(both.map((row) => row.source)).toEqual(['global', 'personal', 'repo'])
    expect(new Set(both.map((row) => row.key)).size).toBe(3)
    const global = queried(both, query({ filters: { source: 'global' } }), TERM_COLUMNS)
    expect(global.matched).toBe(1)
    expect(global.rows[0]?.source).toBe('global')
    expect(queried(both, query({ filters: { source: 'personal' } }), TERM_COLUMNS).rows.map((row) => row.key)).toEqual([both[1]?.key])
  })
})

describe('the rows of each list', () => {
  it('have a key unique across sources and kinds, even for an item twice in one source', () => {
    const twice = termRows({ global: [term('Orion', 'CLIENT'), term('Orion', 'PROJECT')] })
    expect(new Set(twice.map((row) => row.key)).size).toBe(2)
    const allowed = allowRows({ global: { values: ['dev'], patterns: ['dev'] }, personal: { values: ['dev'], patterns: [] } })
    expect(allowed.map((row) => [row.source, row.item.kind, row.item.value])).toEqual([
      ['global', 'value', 'dev'],
      ['global', 'pattern', 'dev'],
      ['personal', 'value', 'dev'],
    ])
    expect(new Set(allowed.map((row) => row.key)).size).toBe(3)
  })

  it('give a recognizer, a criterion and a term a key from what names it', () => {
    const recognizers: RecognizerEntry[] = [{ name: 'employee-id', pattern: 'x' }, { pattern: 'no name' }]
    const rows = recognizerRows({ repo: recognizers })
    expect(rows.map((row) => row.item)).toEqual(recognizers)
    expect(rows[0]?.key).toContain('employee-id')
    expect(new Set(rows.map((row) => row.key)).size).toBe(2)
    const policies = policyRows({ global: { EMAIL: 'redact', IBAN: 'allow' }, personal: { EMAIL: 'mask' } })
    expect(policies.map((row) => [row.source, row.item.type, row.item.policy])).toEqual([
      ['global', 'EMAIL', 'redact'],
      ['global', 'IBAN', 'allow'],
      ['personal', 'EMAIL', 'mask'],
    ])
    // A term by its value and its type, as the daemon tells terms apart: not by how it matches.
    expect(termRows({ global: [term('Orion')] })[0]?.key).not.toBe(termRows({ global: [term('Orion', 'OTHER')] })[0]?.key)
    expect(termRows({ global: [term('Orion')] })[0]?.key).toBe(termRows({ global: [{ ...term('Orion'), case_sensitive: true }] })[0]?.key)
  })
})

describe('the selection', () => {
  it('selection clears on a new query', () => {
    const selected = new Set(['global:term:a', 'global:term:b'])
    const before = query({ search: 'a', filters: { type: 'CLIENT' } })
    expect(selectionAfter(before, { ...before, search: 'ab' }, selected)).toEqual(new Set())
    expect(selectionAfter(before, { ...before, filters: { type: 'PROJECT' } }, selected)).toEqual(new Set())
    expect(selectionAfter(before, { ...before, filters: { type: 'CLIENT', source: 'repo' } }, selected)).toEqual(new Set())
    expect(selectionAfter(before, { ...before, page: 2 }, selected)).toBe(selected)
    expect(selectionAfter(before, { ...before, sort: { column: 'value', dir: 'asc' } }, selected)).toBe(selected)
    expect(selectionAfter(before, { ...before, filters: { type: 'CLIENT' } }, selected)).toBe(selected)
  })

  it('bulk delete removes exactly the selected keys', () => {
    const global = [term('a'), term('b'), term('c'), term('d')]
    const personal = [term('b'), term('e')]
    const rows = termRows({ global, personal })
    const keys = new Set([rows[1]!.key, rows[3]!.key, rows[4]!.key]) // global b and d, personal b
    expect(values(termRows({ global: withoutKeys(global, rows, keys, 'global') }))).toEqual(['a', 'c'])
    expect(withoutKeys(personal, rows, keys, 'personal')).toEqual([term('e')])
    // Another source's keys leave a list alone.
    expect(withoutKeys(global, rows, new Set([rows[4]!.key]), 'global')).toEqual(global)
  })
})

describe('filtersKept', () => {
  it('lets go a filter whose value left its menu (the rows that had it deleted or changed), keeping the others', () => {
    const menus = [
      { column: 'type', options: [{ value: 'CLIENT' }] },
      { column: 'source', options: [{ value: 'global' }] },
    ]
    const filters = { type: 'CLIENT', source: 'global' }
    expect(filtersKept(filters, menus)).toBe(filters)
    const none = { type: '' }
    expect(filtersKept(none, menus)).toBe(none)
    expect(filtersKept({ type: 'PROJECT', source: 'global' }, menus)).toEqual({ type: '', source: 'global' })
  })
})
