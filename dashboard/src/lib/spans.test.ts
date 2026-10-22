import { describe, expect, it } from 'vitest'
import { codePointMapper, lineCount, offsetAfterLines, sliceRange, splitByRanges } from './spans'

const texts = (text: string, ranges: { start: number; end: number }[], units?: 'codepoint' | 'utf16') =>
  splitByRanges(text, ranges, units).map((s) => [s.text, s.index])

describe('splitByRanges', () => {
  it('cuts a text into plain and highlighted segments that cover all of it', () => {
    const text = 'DB_PASSWORD=hunter2 and EMAIL=a@b.it'
    const segments = splitByRanges(text, [
      { start: 12, end: 19 },
      { start: 30, end: 36 },
    ])
    expect(segments.map((s) => s.text).join('')).toBe(text)
    expect(segments.map((s) => [s.text, s.index])).toEqual([
      ['DB_PASSWORD=', -1],
      ['hunter2', 0],
      [' and EMAIL=', -1],
      ['a@b.it', 1],
    ])
  })

  it('sorts ranges by position and keeps the index of each in the input', () => {
    expect(texts('abcdef', [{ start: 4, end: 6 }, { start: 0, end: 2 }])).toEqual([
      ['ab', 1],
      ['cd', -1],
      ['ef', 0],
    ])
  })

  it('handles ranges at both ends and adjacent ranges', () => {
    expect(texts('abcd', [{ start: 0, end: 2 }, { start: 2, end: 4 }])).toEqual([
      ['ab', 0],
      ['cd', 1],
    ])
  })

  it('skips empty, inverted and overlapping ranges, and clamps out-of-range offsets', () => {
    expect(
      texts('abcdefgh', [
        { start: 1, end: 1 },
        { start: 5, end: 3 },
        { start: 1, end: 4 },
        { start: 2, end: 6 },
        { start: 6, end: 99 },
      ]),
    ).toEqual([
      ['a', -1],
      ['bcd', 2],
      ['ef', -1],
      ['gh', 4],
    ])
  })

  it('returns one plain segment for a text without ranges, even an empty one', () => {
    expect(texts('plain', [])).toEqual([['plain', -1]])
    expect(texts('', [])).toEqual([['', -1]])
  })

  it('reads offsets as code points, like the daemon (Python string indices)', () => {
    // "🚀" is one code point but two UTF-16 units: the value after it starts at code point 2.
    const text = '🚀 IT60X0542811101000000123456 ok'
    const start = 2
    const end = start + 27
    const segments = splitByRanges(text, [{ start, end }])
    expect(segments[1]?.text).toBe('IT60X0542811101000000123456')
    expect(segments.map((s) => s.text).join('')).toBe(text)
    // The same offsets read as UTF-16 units would be off by one.
    expect(splitByRanges(text, [{ start, end }], 'utf16')[1]?.text).toBe(' IT60X054281110100000012345')
  })
})

describe('codePointMapper', () => {
  it('is the identity for texts without astral characters', () => {
    const map = codePointMapper('plain text')
    expect([0, 3, 10].map(map)).toEqual([0, 3, 10])
  })

  it('maps code points after an emoji to UTF-16 offsets', () => {
    const map = codePointMapper('a😀b😀c')
    expect([0, 1, 2, 3, 4, 5].map(map)).toEqual([0, 1, 3, 4, 6, 7])
    expect(map(99)).toBe(7)
  })
})

describe('sliceRange and line helpers', () => {
  it('slices with code point offsets', () => {
    expect(sliceRange('😀 secret', { start: 2, end: 8 })).toBe('secret')
  })

  it('counts lines and finds where a line starts', () => {
    expect(lineCount('')).toBe(0)
    expect(lineCount('a\nb\nc')).toBe(3)
    expect(offsetAfterLines('a\nbb\nccc', 2)).toBe(5)
    expect(offsetAfterLines('a\nbb', 5)).toBe(4)
  })
})
