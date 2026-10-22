import { describe, expect, it } from 'vitest'
import { hasFlag, parseFlag, parseFlags } from './flags'
import { formatCompact, formatDuration, formatNumber, formatPercent } from './format'
import { extractCommands, splitInlineCode } from './inlineCode'
import { buildFromPaths, parsePath, toJson } from './jsonpath'

describe('flags', () => {
  it('splits a flag into its key and detail', () => {
    expect(parseFlag('uninspected:image')).toEqual({ raw: 'uninspected:image', key: 'uninspected', detail: 'image', severity: 'warning' })
    expect(parseFlag('blocked')).toMatchObject({ key: 'blocked', detail: null, severity: 'critical' })
    expect(parseFlag('something-new')).toMatchObject({ key: 'other', severity: 'neutral' })
  })

  it('orders flags by severity', () => {
    expect(parseFlags(['unknown-route', 'degraded:pii', 'leak']).map((f) => f.key)).toEqual(['leak', 'degraded', 'unknown-route'])
  })

  it('matches a filter with or without detail, like the daemon', () => {
    expect(hasFlag(['uninspected:image'], 'uninspected')).toBe(true)
    expect(hasFlag(['uninspected:image'], 'uninspected:image')).toBe(true)
    expect(hasFlag(['uninspectedx'], 'uninspected')).toBe(false)
    expect(hasFlag([], '')).toBe(true)
  })
})

describe('inline code', () => {
  it('splits the doctor messages into text and commands', () => {
    expect(splitInlineCode('not installed: `latet service install`')).toEqual([
      { text: 'not installed: ', code: false },
      { text: 'latet service install', code: true },
    ])
    expect(extractCommands('run `latet start` or `latet doctor`.')).toEqual(['latet start', 'latet doctor'])
    expect(splitInlineCode('an unmatched ` backtick')).toEqual([{ text: 'an unmatched ` backtick', code: false }])
  })
})

describe('JSON paths', () => {
  it('parses dotted and bracketed paths', () => {
    expect(parsePath('$.messages[3].content[0].text')).toEqual(['messages', 3, 'content', 0, 'text'])
    expect(parsePath("$['odd key'].x")).toEqual(['odd key', 'x'])
    expect(parsePath('$')).toEqual([])
    expect(parsePath('messages[0]')).toBeNull()
    expect(parsePath('$.a[x]')).toBeNull()
  })

  it('rebuilds a document from the slots, with holes as null', () => {
    const { root, skipped } = buildFromPaths([
      { path: '$.system[1].text', value: 'You are...' },
      { path: '$.messages[0].content[0].text', value: 'hello' },
      { path: '$.messages[2].content', value: 'later' },
    ])
    expect(skipped).toEqual([])
    expect(JSON.parse(toJson(root))).toEqual({
      system: [null, { text: 'You are...' }],
      messages: [{ content: [{ text: 'hello' }] }, null, { content: 'later' }],
    })
  })

  it('skips entries that conflict with earlier ones', () => {
    const { root, skipped } = buildFromPaths([
      { path: '$.input', value: 'text' },
      { path: '$.input[0]', value: 'clash' },
      { path: 'not a path', value: 1 },
    ])
    expect(root).toEqual({ input: 'text' })
    expect(skipped).toEqual(['$.input[0]', 'not a path'])
  })
})

describe('formatting', () => {
  it('formats durations, compact counts and percentages', () => {
    expect(formatDuration('en', 842)).toBe('842 ms')
    expect(formatDuration('en', 1420)).toBe('1.42 sec')
    expect(formatCompact('en', 9999)).toBe('9,999')
    expect(formatCompact('en', 18240)).toBe('18.2K')
    expect(formatPercent('en', 0.8312)).toBe('83%')
  })

  it("formats numbers in the reader's language", () => {
    expect(formatNumber('de', 1234.5)).toBe('1.234,5')
    expect(formatNumber('it', 1234.5)).toBe('1234,5')
    expect(formatNumber('en', 1234.5)).toBe('1,234.5')
    expect(formatPercent('es', 0.8312)).toBe('83' + String.fromCharCode(0xa0) + '%')
  })
})
