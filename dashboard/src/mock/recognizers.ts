// Mock of POST /recognizers/test: JavaScript regular expressions stand in for RE2, and only the
// Luhn check digit is computed. Close enough to try the editor without a daemon.

import type { RecognizerEntry, RecognizerExample, RecognizerReport } from '../api/types'
import { hasLetterOrDigit } from '../lib/terms'

function luhn(digits: string): boolean {
  let sum = 0
  for (let i = 0; i < digits.length; i++) {
    let d = Number(digits[digits.length - 1 - i])
    if (i % 2 === 1) d = d * 2 > 9 ? d * 2 - 9 : d * 2
    sum += d
  }
  return digits.length > 1 && sum % 10 === 0
}

function compile(pattern: string): RegExp {
  const ignoreCase = pattern.startsWith('(?i)')
  return new RegExp((ignoreCase ? pattern.slice(4) : pattern).replace(/\(\?P</g, '(?<'), ignoreCase ? 'gi' : 'g')
}

export function mockTestRecognizer(entry: RecognizerEntry, text: string): RecognizerReport {
  const name = typeof entry.name === 'string' && entry.name ? entry.name : '(no name)'
  let regex: RegExp
  try {
    regex = compile(String(entry.pattern ?? ''))
  } catch (error) {
    return { name, state: 'error', errors: [`pattern: not a valid RE2 expression (${(error as Error).message})`], warnings: [] }
  }
  if (compile(String(entry.pattern ?? '')).test('')) {
    return { name, state: 'error', errors: ['pattern: it matches an empty text, so it would match everywhere'], warnings: [] }
  }
  const words = [...(entry.context?.words ?? []), ...(entry.context?.acronyms ?? [])].map((w) => w.toLowerCase())
  const explain = (sample: string) => {
    const found: string[] = []
    const spans: { start: number; end: number }[] = []
    const reasons: string[] = []
    for (const m of sample.matchAll(regex)) {
      const value = m.groups?.value ?? m[0]
      const start = (m.index ?? 0) + (m.groups?.value !== undefined ? m[0].indexOf(value) : 0)
      if (!hasLetterOrDigit(value)) {
        reasons.push(`matched '${value}', but it has no letter or digit: signs alone are never masked`)
        continue
      }
      if (entry.checksum === 'luhn' && !luhn(value.replace(/\D/g, ''))) {
        reasons.push(`matched '${value}', but the luhn check fails`)
        continue
      }
      const before = sample.slice(Math.max(0, start - 48), start).toLowerCase()
      if (words.length && !words.some((w) => before.includes(w))) {
        reasons.push(`matched '${value}', but no keyword (${words.join(', ')}) within 48 characters`)
        continue
      }
      found.push(value)
      spans.push({ start, end: start + value.length })
    }
    const reason = found.length || reasons.length ? reasons.join('; ') || null : 'the pattern does not match'
    return { found, spans, reason }
  }
  const examples: RecognizerExample[] = [
    ...(entry.examples?.match ?? []).map((sample) => {
      const { found, reason } = explain(sample)
      return { text: sample, expect: 'match' as const, ok: found.length > 0, found, reason }
    }),
    ...(entry.examples?.no_match ?? []).map((sample) => {
      const { found, reason } = explain(sample)
      return { text: sample, expect: 'no_match' as const, ok: found.length === 0, found, reason }
    }),
  ]
  return {
    name,
    state: entry.enabled === false ? 'off' : 'active',
    errors: [],
    warnings: examples.filter((e) => !e.ok).map((e) => `examples.${e.expect} '${e.text}'`),
    type: typeof entry.type === 'string' && entry.type ? entry.type : name.toUpperCase().replace(/-/g, '_'),
    class: entry.class ?? 'custom',
    examples,
    stdlib: { matches: 0, samples: [] },
    try: text ? explain(text) : undefined,
  }
}
