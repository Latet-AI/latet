// Mock of GET /exchanges/{id}/columns and its review: the columns of a JSON or psql tool output, with
// proposals from a few names instead of the daemon's detectors. Close enough to try the panel without a daemon.

import type { ColumnOpinions, ColumnReason, FieldRule, OutputColumn, OutputColumns, OutputShape, Slot } from '../api/types'
import { isUnlocked, technicalKind } from '../lib/fields'

const MASK: Record<string, [ColumnReason, string | null]> = {
  nino: ['people', 'GB_NINO'],
  iban: ['people', 'IBAN'],
  phone: ['person', null],
  note: ['free-text', null],
  name: ['person', null],
  email: ['people', 'EMAIL'],
}
const KEEP_STATES = new Set(['status'])
const ASK: Record<string, ColumnReason> = { amount: 'amounts' }
const KIND_REASON = { id: 'ids', date: 'dates', state: 'states', count: 'counts' } as const

/** The columns of a table as `psql` prints it: names, a line of dashes and plus signs, rows, "(5 rows)". */
function table(text: string): Map<string, string[]> | null {
  const lines = text.split('\n')
  const rule = lines.findIndex((line) => line.includes('+') && /^[-+]{3,}$/.test(line.trim()))
  if (rule < 1) return null
  const names = lines[rule - 1]!.split('|').map((cell) => cell.trim())
  const out = new Map(names.map((name) => [name, [] as string[]]))
  for (const line of lines.slice(rule + 1)) {
    if (!line.includes('|')) break
    line.split('|').forEach((cell, i) => out.get(names[i] ?? '')?.push(cell.trim()))
  }
  return out
}

function collect(value: unknown, path: string, out: Map<string, string[]>): void {
  if (Array.isArray(value)) {
    for (const item of value) collect(item, `${path}[]`, out)
  } else if (value !== null && typeof value === 'object') {
    for (const [key, item] of Object.entries(value)) collect(item, path ? `${path}.${key}` : key, out)
  } else if (path) {
    const name = path.startsWith('[].') ? path.slice(3) : path
    out.set(name, [...(out.get(name) ?? []), String(value)])
  }
}

/**
 * `fields`: the rules in use, to tell what is kept in clear now (the fixtures' `id` column is, when they are not given);
 * `strict`: strict data mode is on where the exchange was. The structure is read whether it is on or not, as the daemon does.
 */
export function mockColumns(
  slot: Slot,
  project: { id: string; name: string } | null,
  reviewer: boolean,
  fields?: readonly FieldRule[],
  strict = true,
): OutputColumns {
  const original = slot.original ?? ''
  let found = new Map<string, string[]>()
  try {
    collect(JSON.parse(original), '', found)
  } catch {
    found = table(original) ?? found // not JSON: a table, or nothing
  }
  const masked = new Set(slot.replacements.filter((r) => r.module === 'data').map((r) => original.slice(r.start, r.end)))
  const columns = [...found].map(([name, values]): OutputColumn => {
    const kind = technicalKind(name)
    const mask = MASK[name]
    const [proposal, reason, detail]: [OutputColumn['proposal'], ColumnReason, string | null] = mask
      ? ['mask', ...mask]
      : KEEP_STATES.has(name)
        ? ['keep', 'states', [...new Set(values)].join(', ')]
        : kind
          ? ['keep', KIND_REASON[kind], null]
          : ['ask', ASK[name] ?? 'unclear', null]
    const count = values.filter((v) => masked.has(v)).length
    const kept_now = fields ? isUnlocked(fields, name) : name === 'id'
    return { name, masked: count, proposal, reason, detail, kept_now, everywhere: true, examples: values.slice(0, 3) }
  })
  // The mock's tool outputs with a structure are a query's: strict data mode covers them. Its file reads are not JSON
  // nor a psql table, so they have no columns here.
  return { columns, strict, applies: true, project: project ? { ...project, loaded: true } : null, reviewer }
}

/** The light answer (`?shape=1`), as the daemon gives it: the columns' names, none when strict data mode does not cover the tool. */
export function mockColumnShape(found: OutputColumns): OutputShape {
  return { columns: found.applies ? found.columns.map((column) => column.name) : [], strict: found.strict, applies: found.applies }
}

export function mockColumnOpinions(columns: readonly OutputColumn[]): ColumnOpinions {
  const opinions: ColumnOpinions['opinions'] = {}
  for (const column of columns) {
    const keep = column.proposal === 'keep' || column.name === 'open_invoices'
    opinions[column.name] = { keep, why: keep ? 'a code or a count: says nothing about a person' : 'could identify a customer' }
  }
  return { opinions }
}
