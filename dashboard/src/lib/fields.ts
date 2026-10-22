// The fields of tool outputs (a column, a JSON key or path) that strict data mode masks: keeping
// one in clear, and telling the ones that are usually safe to keep (ids, dates, states, counts).

import type { FieldRule } from '../api/types'

/** The field a strict data mode rule masked a value of (`strict:items[].sku` → `items[].sku`). */
export function strictField(rule: string | null | undefined): string | null {
  if (!rule?.startsWith('strict:')) return null
  return rule.slice('strict:'.length) || null
}

/** The field a field rule names (`field:note_*` → `note_*`). */
export function ruleField(rule: string | null | undefined): string | null {
  if (!rule?.startsWith('field:')) return null
  return rule.slice('field:'.length) || null
}

/** The rules with `field` kept in clear: once, in place of a rule of the same name, and first, since the
 * first rule that matches wins (a broader one, such as `note_*`, would mask it otherwise). */
export function unlocked(fields: readonly FieldRule[], field: string): FieldRule[] {
  return keepInClear(fields, [field])
}

/** The rules with these columns kept in clear: each once, first, in place of rules of the same name. */
export function keepInClear(fields: readonly FieldRule[], names: readonly string[]): FieldRule[] {
  const kept: FieldRule[] = []
  for (const name of names) {
    if (!kept.some((f) => f.name.toLowerCase() === name.toLowerCase())) kept.push({ name, mask: false })
  }
  const taken = new Set(kept.map((f) => f.name.toLowerCase()))
  return [...kept, ...fields.filter((f) => !taken.has(f.name.toLowerCase()))]
}

/** Whether a column can be kept in clear by its name: a `*` in it would match other columns too. */
export function canKeep(field: string): boolean {
  return !field.includes('*')
}

/** Whether these rules keep `field` in clear (a rule of that very name). */
export function isUnlocked(fields: readonly FieldRule[] | null | undefined, field: string): boolean {
  const name = field.toLowerCase()
  return (fields ?? []).some((f) => f.mask === false && f.name.toLowerCase() === name)
}

export type TechnicalKind = 'id' | 'date' | 'state' | 'count'

/** The words of a field's last segment: `items[].createdAt` → created, at. */
function words(field: string): string[] {
  const last = field.split('.').pop() ?? field
  return last
    .replace(/\[\]/g, '')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
}

const ID_WORDS = new Set(['id', 'uuid', 'guid', 'pk', 'seq', 'version', 'rev'])
const DATE_WORDS = new Set([
  ...['date', 'time', 'timestamp', 'ts', 'day', 'month', 'year', 'created', 'updated', 'modified', 'deleted'],
  ...['creato', 'creata', 'aggiornato', 'modificato', 'scadenza'],
])
const DATE_LAST = new Set(['at', 'on', 'il'])
// Dates about a person (of birth, of death), not of events.
const PERSONAL_DATE = new Set([
  ...['birth', 'birthday', 'birthdate', 'dob', 'born', 'nascita', 'nato', 'nata', 'compleanno', 'nacimiento'],
  ...['naissance', 'geburtsdatum', 'geburtstag', 'death', 'died', 'deceased', 'morte', 'decesso'],
])
// Someone's health, beliefs or private life: never "usually safe", whatever else the name says (hiv_status).
const SENSITIVE = new Set([
  ...['health', 'salute', 'hiv', 'aids', 'malattia', 'malattie', 'disease', 'illness', 'pregnant', 'pregnancy'],
  ...['gravidanza', 'incinta', 'disability', 'disabilita', 'invalidita', 'handicap', 'medical', 'clinical'],
  ...['clinico', 'clinica', 'therapy', 'terapia', 'allergy', 'allergia', 'blood', 'sangue', 'vaccine', 'vaccino'],
  ...['marital', 'married', 'civile', 'coniuge', 'spouse', 'religion', 'religione', 'faith', 'political'],
  ...['politics', 'politico', 'partito', 'union', 'sindacato', 'ethnicity', 'ethnic', 'etnia', 'race', 'razza'],
  ...['sexual', 'sessuale', 'orientation', 'orientamento', 'criminal', 'penale', 'conviction', 'condanna'],
  ...['biometric', 'biometrico', 'genetic', 'genetico'],
])
const STATE_WORDS = new Set([
  ...['status', 'state', 'stato', 'type', 'kind', 'category', 'level', 'priority', 'enabled', 'active', 'flag'],
  ...['attivo', 'attiva', 'abilitato', 'abilitata', 'tipo', 'categoria', 'livello', 'priorita'],
])
const COUNT_WORDS = new Set(['count', 'qty', 'quantity'])

/**
 * What a field's name says its values are, when they are usually safe to keep in clear: ids, the
 * dates of events (not of birth), states and counts. Null when the name says nothing of the kind.
 */
export function technicalKind(field: string): TechnicalKind | null {
  const w = words(field)
  if (w.length === 0) return null
  const last = w[w.length - 1]!
  if (ID_WORDS.has(last)) return 'id'
  if (w.some((x) => PERSONAL_DATE.has(x) || SENSITIVE.has(x))) return null
  const italianDate = w.length > 1 && w[0] === 'data' // data_ordine
  if (w.some((x) => DATE_WORDS.has(x)) || italianDate || (w.length > 1 && DATE_LAST.has(last))) return 'date'
  if (STATE_WORDS.has(last)) return 'state'
  if (COUNT_WORDS.has(last)) return 'count'
  return null
}
