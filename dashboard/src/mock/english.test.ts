import { describe, expect, it } from 'vitest'
import en from '../locales/en.json'

/** The mock's sources and the Playground's example, as text: what the dashboard shows without a daemon, and what
 * the documentation's screenshots show. */
const SOURCES = import.meta.glob<string>(['./*.ts', '!./*.test.ts', '../lib/samples.ts'], {
  query: '?raw',
  import: 'default',
  eager: true,
})

/** What the Italian mock was made of: its words, names, addresses and conventions. */
const ITALIAN = [
  /\b(?:fattur\w*|cliente|clienti|aziend[ae]|colorificio|codice|fiscale|progetto|ciao|grazie|rossi|bianchi|giulia|mario)\b/i,
  /\bPEC(?![a-zA-Z])/,
  /\bfiscal code\b/i,
  /\bacme\.it\b/i,
  /\+39\b/,
  /\bFT-\d/,
]

/** The English strings name Italian documents (codice fiscale, partita IVA) on purpose: only examples count there. */
const ITALIAN_EXAMPLES = [/\b(?:progetto|rossi|bianchi|giulia|mario|fattur\w*|colorificio|azienda)\b/i, /\bacme\.it\b/i]

function found(where: string, text: string, patterns: readonly RegExp[]): string[] {
  return text.split('\n').flatMap((line, i) =>
    patterns.flatMap((pattern) => {
      // A string's \n or \t ends a word, as on the page.
      const match = line.replace(/\\[nt]/g, ' ').match(pattern)
      return match ? [`${where}:${i + 1}: ${match[0]}`] : []
    }),
  )
}

function strings(value: unknown, key = ''): [string, string][] {
  if (typeof value === 'string') return [[key, value]]
  if (value && typeof value === 'object') {
    return Object.entries(value).flatMap(([k, v]) => strings(v, key ? `${key}.${k}` : k))
  }
  return []
}

describe('the example data is English', () => {
  it('in the mock and in the Playground example', () => {
    expect(Object.keys(SOURCES)).toContain('./fixtures.ts')
    expect(Object.entries(SOURCES).flatMap(([file, text]) => found(file, text, ITALIAN))).toEqual([])
  })

  it('in the English strings', () => {
    expect(strings(en).flatMap(([key, text]) => found(key, text, ITALIAN_EXAMPLES))).toEqual([])
  })
})
