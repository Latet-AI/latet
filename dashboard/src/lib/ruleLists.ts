// Each list of the rules as one source's table reads and writes it (`ListAccess`): the global rules, your own settings
// for a project and its latet.yaml all hold them under the same keys, a list not set being an empty one.

import type { Policy, RecognizerEntry, Term } from '../api/types'
import type { ListAccess } from './panelSave'
import { allowItemRows, allowItems, policyItemRows, recognizerRows, termRows, type AllowItem, type PolicyItem, type RuleSource } from './ruleTable'

export function termsList<S extends { terms?: Term[] }>(source: RuleSource): ListAccess<S, Term> {
  return {
    get: (settings) => settings.terms ?? [],
    set: (settings, terms) => ({ ...settings, terms }),
    rows: (terms) => termRows({ [source]: terms }),
  }
}

/** The values never masked and their patterns, as one list: each item goes back to its own list. */
export function allowList<S extends { allow_values?: string[]; allow_patterns?: string[] }>(source: RuleSource): ListAccess<S, AllowItem> {
  return {
    get: (settings) => allowItems(settings.allow_values ?? [], settings.allow_patterns ?? []),
    set: (settings, items) => ({
      ...settings,
      allow_values: items.filter((item) => item.kind === 'value').map((item) => item.value),
      allow_patterns: items.filter((item) => item.kind === 'pattern').map((item) => item.value),
    }),
    rows: (items) => allowItemRows({ [source]: items }),
  }
}

/**
 * Whether `pattern` matches `value` whole, as the daemon lets a value through (RE2's full match, letter case as
 * written); null for a pattern the browser cannot read as RE2 does. Read with the `u` flag, the browser reads the
 * classes of letters (`\p{L}`) as RE2; what RE2 alone reads (`\pL`, `[[:alpha:]]`, `\Q…\E`, `\A`, `\z`) is an error
 * there instead of a literal, so no verdict is given rather than a wrong one.
 */
export function wholeMatch(pattern: string, value: string): boolean | null {
  try {
    return new RegExp(`^(?:${pattern})$`, 'u').test(value)
  } catch {
    return null
  }
}

/** The policies, a type each: a map in the settings. */
export function policiesList<S extends { policies?: Record<string, Policy> }>(source: RuleSource): ListAccess<S, PolicyItem> {
  return {
    get: (settings) => Object.entries(settings.policies ?? {}).map(([type, policy]) => ({ type, policy })),
    set: (settings, items) => ({ ...settings, policies: Object.fromEntries(items.map((item) => [item.type, item.policy])) }),
    rows: (items) => policyItemRows({ [source]: items }),
  }
}

/** Types a policy is often set for, besides the personal-data types and those of your terms and recognizers. */
export const COMMON_TYPES = [
  'PASSWORD',
  'ASSIGNED_SECRET',
  'PRIVATE_KEY',
  'URL_PASSWORD',
  'JWT',
  'BEARER_TOKEN',
  'USERNAME',
  'HOSTNAME',
  'PERSON',
  'GITHUB_PAT',
  'AWS_ACCESS_TOKEN',
  'STRIPE_ACCESS_TOKEN',
  'ANTHROPIC_API_KEY',
  'OPENAI_API_KEY',
]

/** The type a recognizer's values have: its own, else its name in capitals, a hyphen an underscore. */
export function recognizerType(entry: RecognizerEntry): string {
  if (typeof entry.type === 'string' && entry.type.trim() !== '') return entry.type.trim()
  return typeof entry.name === 'string' ? entry.name.toUpperCase().replace(/-/g, '_') : ''
}

/**
 * The types a policy can be added for, sorted: those Latet knows (`known`: its personal-data types), the common ones,
 * and those of your terms and recognizers; not those `taken` (a policy is set for them already).
 */
export function typesToAdd(known: readonly string[], terms: readonly Term[], recognizers: readonly RecognizerEntry[], taken: readonly string[]): string[] {
  const all = new Set([...known, ...COMMON_TYPES, ...terms.map((term) => term.type), ...recognizers.map(recognizerType)])
  for (const type of taken) all.delete(type)
  all.delete('')
  return [...all].sort()
}

/** The recognizers as they are written, what the form cannot show among them. */
export function recognizersList<S extends { recognizers?: RecognizerEntry[] }>(source: RuleSource): ListAccess<S, RecognizerEntry> {
  return {
    get: (settings) => settings.recognizers ?? [],
    set: (settings, recognizers) => ({ ...settings, recognizers }),
    rows: (recognizers) => recognizerRows({ [source]: recognizers }),
  }
}

/** A recognizer turned off: `enabled: false`, and nothing else of it changed. */
export function turnedOff(entry: RecognizerEntry): RecognizerEntry {
  return { ...entry, enabled: false }
}

/** A recognizer turned on: `enabled` left out (on is its default), and nothing else of it changed. */
export function turnedOn(entry: RecognizerEntry): RecognizerEntry {
  const { enabled: _enabled, ...rest } = entry
  return rest
}
