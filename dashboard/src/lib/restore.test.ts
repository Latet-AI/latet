import { describe, expect, it } from 'vitest'
import { alignRestored, findSurrogates, type KnownValue } from './restore'

const known: KnownValue<string>[] = [
  { surrogate: 'marta.greco@gmail.com', original: 'giulia.conti@pec-studio.it', info: 'email' },
  { surrogate: 'IT20W0306909606100000063451', original: 'IT60X0542811101000000123456', info: 'iban' },
  { surrogate: 'Luca Bianchi', original: 'Mario Rossi', info: 'person' },
]

describe('findSurrogates', () => {
  it('finds every occurrence, in order', () => {
    const text = 'Write to marta.greco@gmail.com, then again marta.greco@gmail.com about IT20W0306909606100000063451.'
    expect(findSurrogates(text, known).map((f) => [text.slice(f.start, f.end), f.info])).toEqual([
      ['marta.greco@gmail.com', 'email'],
      ['marta.greco@gmail.com', 'email'],
      ['IT20W0306909606100000063451', 'iban'],
    ])
  })

  it('prefers the longest surrogate when two overlap', () => {
    const values: KnownValue<string>[] = [
      { surrogate: 'abc', original: null, info: 'short' },
      { surrogate: 'abcdef', original: null, info: 'long' },
    ]
    expect(findSurrogates('xx abcdef', values).map((f) => f.info)).toEqual(['long'])
  })
})

describe('alignRestored', () => {
  it('locates each restored original in what the agent received', () => {
    const masked = 'Customer marta.greco@gmail.com pays on IT20W0306909606100000063451.'
    const restored = 'Customer giulia.conti@pec-studio.it pays on IT60X0542811101000000123456.'
    const result = alignRestored(masked, restored, known)
    expect(result.aligned).toBe(true)
    expect(result.restored.map((r) => restored.slice(r.start, r.end))).toEqual([
      'giulia.conti@pec-studio.it',
      'IT60X0542811101000000123456',
    ])
  })

  it('uses the known original even when it contains the text that follows', () => {
    // "Mario Rossi" contains a space, and the text after the surrogate starts with one.
    const result = alignRestored('By Luca Bianchi today', 'By Mario Rossi today', known)
    expect(result.aligned).toBe(true)
    expect(result.restored.map((r) => 'By Mario Rossi today'.slice(r.start, r.end))).toEqual(['Mario Rossi'])
  })

  it('aligns values with an unknown original by length or by the next unchanged text', () => {
    const values: KnownValue<string>[] = [{ surrogate: 'giorgio17', original: null, info: 'user' }]
    const masked = 'C:\\Users\\giorgio17\\dev'
    const restored = 'C:\\Users\\mrossi\\dev'
    const result = alignRestored(masked, restored, values)
    expect(result.aligned).toBe(true)
    expect(restored.slice(result.restored[0]!.start, result.restored[0]!.end)).toBe('mrossi')
  })

  it('handles a value at the very end of the text', () => {
    const result = alignRestored('Mail: marta.greco@gmail.com', 'Mail: giulia.conti@pec-studio.it', known)
    expect(result.aligned).toBe(true)
    expect(result.restored).toHaveLength(1)
  })

  it('keeps the surrogate highlighted when nothing was restored', () => {
    const text = 'Sent to marta.greco@gmail.com'
    const result = alignRestored(text, text, known)
    expect(result.aligned).toBe(true)
    expect(result.restored.map((r) => text.slice(r.start, r.end))).toEqual(['marta.greco@gmail.com'])
  })

  it('reports texts it cannot line up, keeping only the model side', () => {
    const result = alignRestored('Hi marta.greco@gmail.com!', 'Something else entirely', known)
    expect(result.aligned).toBe(false)
    expect(result.masked).toHaveLength(1)
    expect(result.restored).toEqual([])
  })

  it('has nothing to align when no surrogate appears', () => {
    expect(alignRestored('plain', 'plain', known)).toEqual({ masked: [], restored: [], aligned: true })
  })
})
