import { describe, expect, it } from 'vitest'
import type { PersonalSettings, Term } from '../api/types'
import { allowList, policiesList, recognizersList, recognizerType, termsList, turnedOff, turnedOn, typesToAdd, wholeMatch } from './ruleLists'

const term = (value: string): Term => ({ value, type: 'CLIENT', case_sensitive: false, whole_word: true })

describe('termsList', () => {
  it('reads and writes the terms of any settings, with the rows of its source', () => {
    const list = termsList<PersonalSettings>('personal')
    expect(list.get({})).toEqual([])
    expect(list.set({ allow_values: ['dev'] }, [term('Orion Labs')])).toEqual({ allow_values: ['dev'], terms: [term('Orion Labs')] })
    expect(list.rows([term('Orion Labs')]).map((row) => [row.source, row.item.value])).toEqual([['personal', 'Orion Labs']])
  })
})

describe('allowList', () => {
  const list = allowList<PersonalSettings>('global')

  it('reads the values, then the patterns, as one list', () => {
    expect(list.get({})).toEqual([])
    expect(list.get({ allow_values: ['dev'], allow_patterns: ['^test-'] })).toEqual([
      { kind: 'value', value: 'dev' },
      { kind: 'pattern', value: '^test-' },
    ])
  })

  it('writes each item back to its own list, a value turned pattern among them', () => {
    const items = list.get({ allow_values: ['dev', 'ops'], allow_patterns: ['^test-'] })
    const rows = list.rows(items)
    expect(rows.map((row) => row.key)).toEqual(['global:value:dev', 'global:value:ops', 'global:pattern:^test-'])
    const moved = items.map((item) => (item.value === 'dev' ? { kind: 'pattern' as const, value: '^dev-' } : item))
    expect(list.set({ terms: [] }, moved)).toEqual({ terms: [], allow_values: ['ops'], allow_patterns: ['^dev-', '^test-'] })
  })

  it('makes a row of each item in its own order', () => {
    const mixed = [
      { kind: 'pattern' as const, value: 'a' },
      { kind: 'value' as const, value: 'b' },
    ]
    expect(list.rows(mixed).map((row) => row.item)).toEqual(mixed)
  })
})

describe('wholeMatch', () => {
  it('says whether a pattern matches a value whole, as the daemon matches values never masked', () => {
    expect(wholeMatch('^test-[a-z0-9]{8}@example\\.com$', 'test-ab12cd34@example.com')).toBe(true)
    expect(wholeMatch('test-[a-z0-9]{8}@example\\.com', 'x test-ab12cd34@example.com')).toBe(false)
    expect(wholeMatch('dev|ops', 'dev')).toBe(true)
    expect(wholeMatch('dev|ops', 'devops')).toBe(false)
    expect(wholeMatch('DEV', 'dev')).toBe(false)
  })

  it('says nothing of a pattern that is not one', () => {
    expect(wholeMatch('(', 'x')).toBeNull()
  })

  it('reads the classes of letters as RE2 does, and says nothing of what only RE2 reads', () => {
    expect(wholeMatch('\\p{L}+ Test', 'José Test')).toBe(true)
    expect(wholeMatch('\\p{L}+ Test', '123 Test')).toBe(false)
    // RE2 reads these as a class, a quote and anchors; the browser cannot, so no verdict is given.
    expect(wholeMatch('\\pL+', 'José')).toBeNull()
    expect(wholeMatch('[[:alpha:]]+', 'abc')).toBeNull()
    expect(wholeMatch('\\Qa.b\\E', 'a.b')).toBeNull()
    expect(wholeMatch('\\Aabc\\z', 'abc')).toBeNull()
  })
})

describe('policiesList', () => {
  const list = policiesList<PersonalSettings>('personal')

  it('reads the policies a type each, and writes them back as a map', () => {
    expect(list.get({})).toEqual([])
    const items = list.get({ policies: { EMAIL: 'redact', IBAN: 'allow' } })
    expect(items).toEqual([
      { type: 'EMAIL', policy: 'redact' },
      { type: 'IBAN', policy: 'allow' },
    ])
    expect(list.rows(items).map((row) => row.key)).toEqual(['personal:policy:EMAIL', 'personal:policy:IBAN'])
    expect(list.set({ terms: [] }, [{ type: 'IBAN', policy: 'mask' }])).toEqual({ terms: [], policies: { IBAN: 'mask' } })
  })
})

describe('the types a policy can be added for', () => {
  it("are Latet's own, the common ones, those of your terms and of your recognizers, but those set already", () => {
    const types = typesToAdd(
      ['EMAIL', 'IBAN'],
      [term('Orion Labs'), { ...term('Project Aurora'), type: 'PROJECT' }],
      [{ name: 'employee-id', pattern: 'x' }, { name: 'badge', type: 'BADGE_NUMBER', pattern: 'y' }],
      ['IBAN', 'PRIVATE_KEY'],
    )
    expect(types).toContain('EMAIL')
    expect(types).toContain('PASSWORD')
    expect(types).toContain('CLIENT')
    expect(types).toContain('PROJECT')
    expect(types).toContain('EMPLOYEE_ID')
    expect(types).toContain('BADGE_NUMBER')
    expect(types).not.toContain('IBAN')
    expect(types).not.toContain('PRIVATE_KEY')
    expect([...types].sort()).toEqual(types)
    expect(new Set(types).size).toBe(types.length)
  })

  it("name a recognizer's type as the daemon does: its own, else its name in capitals", () => {
    expect(recognizerType({ name: 'employee-id' })).toBe('EMPLOYEE_ID')
    expect(recognizerType({ name: 'badge', type: 'BADGE_NUMBER' })).toBe('BADGE_NUMBER')
    expect(recognizerType({})).toBe('')
  })
})

describe('recognizersList', () => {
  it('reads and writes the recognizers as they are, what the form cannot show among them', () => {
    const list = recognizersList<PersonalSettings>('repo')
    const odd = { name: 'odd', pattern: 'x', future_key: 3 }
    expect(list.get({})).toEqual([])
    expect(list.set({}, [odd])).toEqual({ recognizers: [odd] })
    expect(list.rows([odd]).map((row) => row.key)).toEqual(['repo:recognizer:odd'])
  })
})

describe('turning a recognizer on and off', () => {
  it('changes `enabled` alone, keeping whatever else it holds', () => {
    const entry = { name: 'ticket-ref', pattern: 'x', class: 'secret' as const, future_key: 3 }
    expect(turnedOff(entry)).toEqual({ ...entry, enabled: false })
    expect(turnedOn({ ...entry, enabled: false })).toEqual(entry)
    expect(turnedOn(entry)).toEqual(entry)
  })
})
