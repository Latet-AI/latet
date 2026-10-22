import type { Policy, RecognizerEntry, RecognizerStatus } from '../api/types'

/** The editor's form: strings where the user types numbers, lists for words and examples. */
export interface RecognizerDraft {
  name: string
  type: string
  description: string
  pattern: string
  /** '' (none), a registry name, or 'weighted'. */
  checksum: string
  weights: string
  modulus: string
  remainder: string
  words: string[]
  acronyms: string[]
  window: string
  header: boolean
  match: string[]
  noMatch: string[]
  secret: boolean
  policy: '' | Policy
  keep: string
  minLength: string
  enabled: boolean
}

export const NAME_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/
export const TYPE_PATTERN = /^[A-Z][A-Z0-9_]{1,63}$/

const KEYS = new Set([
  'name',
  'type',
  'description',
  'pattern',
  'checksum',
  'context',
  'examples',
  'class',
  'policy',
  'surrogate',
  'min_length',
  'enabled',
])

export const EMPTY_DRAFT: RecognizerDraft = {
  name: '',
  type: '',
  description: '',
  pattern: '',
  checksum: '',
  weights: '',
  modulus: '11',
  remainder: '0',
  words: [],
  acronyms: [],
  window: '48',
  header: true,
  match: [],
  noMatch: [],
  secret: false,
  policy: '',
  keep: '0',
  minLength: '1',
  enabled: true,
}

const strings = (value: unknown): string[] | null =>
  Array.isArray(value) && value.every((v) => typeof v === 'string') ? (value as string[]) : null

/**
 * The form for an entry. `editable` is false when the entry holds something the form cannot show
 * (a key it does not know, a value of the wrong kind): saving the form would lose it, so such an
 * entry is changed in config.yaml by hand.
 */
export function toDraft(entry: RecognizerEntry): { draft: RecognizerDraft; editable: boolean } {
  const draft: RecognizerDraft = { ...EMPTY_DRAFT, words: [], acronyms: [], match: [], noMatch: [] }
  let editable = Object.keys(entry).every((key) => KEYS.has(key))
  const text = (value: unknown, fallback = '') => {
    if (value === undefined || value === null) return fallback
    if (typeof value === 'string') return value
    editable = false
    return fallback
  }
  draft.name = text(entry.name)
  draft.type = text(entry.type)
  draft.description = text(entry.description)
  draft.pattern = text(entry.pattern)
  const checksum = entry.checksum
  if (typeof checksum === 'string') draft.checksum = checksum
  else if (checksum && typeof checksum === 'object' && checksum.algorithm === 'weighted' && Array.isArray(checksum.weights)) {
    draft.checksum = 'weighted'
    draft.weights = checksum.weights.join(', ')
    draft.modulus = String(checksum.modulus ?? 11)
    draft.remainder = String(checksum.remainder ?? 0)
  } else if (checksum !== undefined && checksum !== null) editable = false
  const context = entry.context
  if (context && typeof context === 'object') {
    draft.words = strings(context.words) ?? (context.words === undefined ? [] : ((editable = false), []))
    draft.acronyms = strings(context.acronyms) ?? (context.acronyms === undefined ? [] : ((editable = false), []))
    if (context.window !== undefined) draft.window = String(context.window)
    if (context.header !== undefined) draft.header = Boolean(context.header)
  } else if (context !== undefined && context !== null) editable = false
  const examples = entry.examples
  if (examples && typeof examples === 'object') {
    draft.match = strings(examples.match) ?? (examples.match === undefined ? [] : ((editable = false), []))
    draft.noMatch = strings(examples.no_match) ?? (examples.no_match === undefined ? [] : ((editable = false), []))
  } else if (examples !== undefined && examples !== null) editable = false
  draft.secret = entry.class === 'secret'
  if (entry.class !== undefined && entry.class !== 'secret' && entry.class !== 'custom') editable = false
  if (entry.policy === 'mask' || entry.policy === 'redact' || entry.policy === 'allow') draft.policy = entry.policy
  else if (entry.policy !== undefined && entry.policy !== null) editable = false
  const surrogate = entry.surrogate
  if (surrogate && typeof surrogate === 'object' && typeof surrogate.keep === 'number') draft.keep = String(surrogate.keep)
  else if (surrogate !== undefined && surrogate !== 'shape') editable = false
  if (entry.min_length !== undefined) draft.minLength = String(entry.min_length)
  if (entry.enabled !== undefined) draft.enabled = entry.enabled !== false
  return { draft, editable }
}

const numbers = (value: string): number[] =>
  value
    .split(/[\s,;]+/)
    .filter(Boolean)
    .map(Number)

/** The entry written to config.yaml: only what differs from the defaults. */
export function fromDraft(draft: RecognizerDraft): RecognizerEntry {
  const entry: RecognizerEntry = { name: draft.name.trim() }
  if (draft.type.trim()) entry.type = draft.type.trim()
  if (draft.description.trim()) entry.description = draft.description.trim()
  entry.pattern = draft.pattern
  if (draft.checksum === 'weighted') {
    entry.checksum = { algorithm: 'weighted', weights: numbers(draft.weights), modulus: Number(draft.modulus) }
    if (Number(draft.remainder)) entry.checksum.remainder = Number(draft.remainder)
  } else if (draft.checksum) entry.checksum = draft.checksum
  if (draft.words.length || draft.acronyms.length) {
    entry.context = {}
    if (draft.words.length) entry.context.words = draft.words
    if (draft.acronyms.length) entry.context.acronyms = draft.acronyms
    if (draft.window.trim() && Number(draft.window) !== 48) entry.context.window = Number(draft.window)
    if (!draft.header) entry.context.header = false
  }
  if (draft.match.length || draft.noMatch.length) {
    entry.examples = {}
    if (draft.match.length) entry.examples.match = draft.match
    if (draft.noMatch.length) entry.examples.no_match = draft.noMatch
  }
  if (draft.secret) entry.class = 'secret'
  if (draft.policy) entry.policy = draft.policy
  if (Number(draft.keep) > 0) entry.surrogate = { keep: Number(draft.keep) }
  if (Number(draft.minLength) > 1) entry.min_length = Number(draft.minLength)
  if (!draft.enabled) entry.enabled = false
  return entry
}

/** What keeps a draft from being applied; the daemon checks the rest (RE2, ordinary code). */
export type DraftProblem =
  | 'name'
  | 'nameTaken'
  | 'type'
  | 'pattern'
  | 'weights'
  | 'modulus'
  | 'window'
  | 'keep'
  | 'minLength'

const integer = (value: string, min: number, max: number) => /^\d+$/.test(value.trim()) && Number(value) >= min && Number(value) <= max

/** `otherNames`: the other recognizers of the list. */
export function draftProblems(draft: RecognizerDraft, otherNames: readonly string[]): DraftProblem[] {
  const problems: DraftProblem[] = []
  const name = draft.name.trim()
  if (!NAME_PATTERN.test(name)) problems.push('name')
  else if (otherNames.includes(name)) problems.push('nameTaken')
  if (draft.type.trim() && !TYPE_PATTERN.test(draft.type.trim())) problems.push('type')
  if (!draft.pattern.trim() || draft.pattern.length > 2000) problems.push('pattern')
  if (draft.checksum === 'weighted') {
    const weights = numbers(draft.weights)
    if (!weights.length || weights.some((w) => !Number.isInteger(w))) problems.push('weights')
    if (!integer(draft.modulus, 2, 1_000_000)) problems.push('modulus')
  }
  if (draft.words.length + draft.acronyms.length > 0 && !integer(draft.window, 1, 500)) problems.push('window')
  if (!integer(draft.keep, 0, 32)) problems.push('keep')
  if (!integer(draft.minLength, 1, 10_000)) problems.push('minLength')
  return problems
}

export type RecognizerState = 'active' | 'off' | 'error' | 'failing' | 'unsaved'

/** A saved recognizer's state; 'unsaved' for one the page changed since the last save. */
export function stateOf(entry: RecognizerEntry, saved: readonly RecognizerEntry[], statuses: readonly RecognizerStatus[]): RecognizerState {
  const same = saved.some((s) => JSON.stringify(s) === JSON.stringify(entry))
  if (!same) return 'unsaved'
  const status = statuses.find((s) => s.name === entry.name)
  if (!status) return 'unsaved'
  if (status.state === 'error') return 'error'
  if (status.warnings.length > 0) return 'failing'
  return status.state
}
