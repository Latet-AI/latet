// One list of the rules in a file and back: the terms and the values never masked as CSV (a client list pastes in one
// go), and any list as a small rules file the daemon reads (`latet rules import`, POST /rules/import): JSON, which is
// YAML too, so that no YAML library is needed here.

import type { RecognizerEntry, Term } from '../api/types'
import type { AllowItem } from './ruleTable'
import { hasLetterOrDigit, termTooLong } from './terms'

/** A line of a CSV that was not read, by its number (the header is line 1), and why. */
export interface CsvProblem {
  line: number
  reason: 'empty' | 'yesNo' | 'noLetterOrDigit' | 'tooLong' | 'quote' | 'kind' | 'pattern' | 'type' | 'columns'
}

const TERM_HEADER = ['value', 'type', 'case_sensitive', 'whole_word']
const ALLOWED_HEADER = ['kind', 'value']

/** What a spreadsheet runs as a formula when a cell starts with it (an allowed value may come from an agent's text). */
const FORMULA = /^[=+\-@\t\r]/

/**
 * A field as CSV writes it: a cell a spreadsheet would run as a formula after a quote, so that it opens as text (read
 * back without it); then in quotes, its quotes doubled, when it holds a comma, a quote or a line break.
 */
function field(text: string): string {
  const cell = FORMULA.test(text) ? `'${text}` : text
  return /[",\r\n]/.test(cell) || cell !== cell.trim() ? `"${cell.replace(/"/g, '""')}"` : cell
}

/** A cell read back: the quote written before what a spreadsheet would run taken off (a value's own quote stays). */
function unguarded(text: string): string {
  return text.startsWith("'") && FORMULA.test(text.slice(1)) ? text.slice(1) : text
}

/** The recognizers the daemon's export of the rules left out, their examples holding values Latet has seen for real
 * (`export_rules`'s warnings), by name. */
export function leftOutByExport(warnings: readonly string[]): Set<string> {
  return new Set(warnings.flatMap((warning) => /^recognizer (.+): left out, /.exec(warning)?.[1] ?? []))
}

function csv(rows: string[][]): string {
  return rows.map((row) => row.map(field).join(',') + '\r\n').join('')
}

/** The records of a CSV, each with the line it starts on; a quote left open is a record of its own, unread. */
function records(text: string): { line: number; fields: string[]; open: boolean }[] {
  const out: { line: number; fields: string[]; open: boolean }[] = []
  let line = 1
  let i = 0
  // A byte order mark, as a spreadsheet may start its CSV with, is no part of the first field.
  const source = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text
  while (i < source.length) {
    const start = line
    const fields: string[] = []
    let value = ''
    let quoted = false
    let open = false
    for (;;) {
      if (i >= source.length) {
        if (quoted) open = true
        fields.push(value)
        break
      }
      const c = source[i]
      if (quoted) {
        if (c === '"' && source[i + 1] === '"') {
          value += '"'
          i += 2
        } else if (c === '"') {
          quoted = false
          i++
        } else {
          if (c === '\n') line++
          value += c
          i++
        }
      } else if (c === '"' && value === '') {
        quoted = true
        i++
      } else if (c === ',') {
        fields.push(value)
        value = ''
        i++
      } else if (c === '\r' || c === '\n') {
        fields.push(value)
        i += c === '\r' && source[i + 1] === '\n' ? 2 : 1
        line++
        break
      } else {
        value += c
        i++
      }
    }
    if (!(fields.length === 1 && fields[0] === '' && !open)) out.push({ line: start, fields, open })
  }
  return out
}

const isHeader = (fields: string[], header: string[]) => fields.length > 0 && fields.every((f, i) => f.trim().toLowerCase() === header[i])

/** Yes or no as a CSV may write it; null for neither. */
function yesNo(text: string | undefined, otherwise: boolean): boolean | null {
  const word = (text ?? '').trim().toLowerCase()
  if (word === '') return otherwise
  if (['true', 'yes', '1'].includes(word)) return true
  if (['false', 'no', '0'].includes(word)) return false
  return null
}

export function termsCsv(terms: Term[]): string {
  return csv([TERM_HEADER, ...terms.map((t) => [t.value, t.type, String(t.case_sensitive), String(t.whole_word)])])
}

/** The terms of a CSV: value, then type (CUSTOM), case sensitive (no) and whole word (yes); a header row or none. */
export function termsFromCsv(text: string): { terms: Term[]; problems: CsvProblem[] } {
  const terms: Term[] = []
  const problems: CsvProblem[] = []
  records(text).forEach(({ line, fields, open }, index) => {
    if (open) return problems.push({ line, reason: 'quote' })
    if (index === 0 && isHeader(fields, TERM_HEADER)) return
    // A comma not in quotes splits a value ("Acme, Inc."): more fields than a term has, or a type that is not one.
    if (fields.length > TERM_HEADER.length) return problems.push({ line, reason: 'columns' })
    if (!/^[A-Za-z0-9_-]*$/.test((fields[1] ?? '').trim())) return problems.push({ line, reason: 'type' })
    const value = unguarded((fields[0] ?? '').trim())
    if (value === '') return problems.push({ line, reason: 'empty' })
    if (!hasLetterOrDigit(value)) return problems.push({ line, reason: 'noLetterOrDigit' })
    if (termTooLong(value)) return problems.push({ line, reason: 'tooLong' })
    const caseSensitive = yesNo(fields[2], false)
    const wholeWord = yesNo(fields[3], true)
    if (caseSensitive === null || wholeWord === null) return problems.push({ line, reason: 'yesNo' })
    const type = (fields[1] ?? '').trim().toUpperCase().replace(/[^A-Z0-9_]/g, '_') || 'CUSTOM'
    terms.push({ value, type, case_sensitive: caseSensitive, whole_word: wholeWord })
  })
  return { terms, problems }
}

export function allowedCsv(items: AllowItem[]): string {
  return csv([ALLOWED_HEADER, ...items.map((item) => [item.kind, item.value])])
}

/** The values and patterns of a CSV: kind (value or pattern), then the text; a line of one field is a value. */
export function allowedFromCsv(text: string): { items: AllowItem[]; problems: CsvProblem[] } {
  const items: AllowItem[] = []
  const problems: CsvProblem[] = []
  records(text).forEach(({ line, fields, open }, index) => {
    if (open) return problems.push({ line, reason: 'quote' })
    if (index === 0 && isHeader(fields, ALLOWED_HEADER)) return
    const [kindText, ...rest] = fields.length === 1 ? ['value', fields[0] ?? ''] : fields
    const kind = (kindText ?? '').trim().toLowerCase()
    const value = unguarded(rest.join(',').trim())
    if (kind !== 'value' && kind !== 'pattern') return problems.push({ line, reason: 'kind' })
    if (value === '') return problems.push({ line, reason: 'empty' })
    if (kind === 'pattern' && !compiles(value)) return problems.push({ line, reason: 'pattern' })
    items.push({ kind, value })
  })
  return { items, problems }
}

/** A pattern the browser reads, with nothing RE2 refuses (lookarounds, backreferences), as validatePattern checks. */
function compiles(pattern: string): boolean {
  try {
    new RegExp(pattern)
  } catch {
    return false
  }
  return !/\(\?<?[=!]|\\[1-9]/.test(pattern)
}

/** A rules file holding only the lists given (an empty one left out): JSON, which the daemon reads as YAML. */
export function rulesFile(part: { terms?: Term[]; allow_values?: string[]; allow_patterns?: string[]; recognizers?: RecognizerEntry[] }): string {
  const body: Record<string, unknown> = { latet_rules: 1 }
  for (const [key, list] of Object.entries(part)) if (list && list.length > 0) body[key] = list
  return `${JSON.stringify(body, null, 2)}\n`
}

/** The keys a file of recognizers alone holds: the mark of a rules file, and its recognizers. */
const NOT_LISTS = new Set(['latet_rules', 'recognizers'])

/**
 * The lists of a rules file besides its recognizers (a colleague's whole export): its top-level keys, read from JSON,
 * or from YAML as the lines that start at the margin with a key. The Recognizers table imports recognizers alone.
 */
export function otherLists(text: string): string[] {
  let keys: string[]
  try {
    const parsed: unknown = JSON.parse(text)
    keys = parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed) ? Object.keys(parsed) : []
  } catch {
    keys = text.split(/\r?\n/).flatMap((line) => /^([A-Za-z_][A-Za-z0-9_]*)\s*:/.exec(line)?.[1] ?? [])
  }
  return [...new Set(keys)].filter((key) => !NOT_LISTS.has(key))
}
