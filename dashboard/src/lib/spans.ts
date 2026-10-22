// Split a text into plain and highlighted segments from replacement offsets.

export interface Range {
  start: number
  end: number
}

export interface Segment<R extends Range = Range> {
  text: string
  /** UTF-16 offsets in the JavaScript string. */
  start: number
  end: number
  /** The range this segment highlights, or null for plain text. */
  range: R | null
  /** Index of `range` in the input array, -1 for plain text. */
  index: number
}

/**
 * Offsets from the daemon count Unicode code points (Python `str` indices); JavaScript strings index
 * UTF-16 code units. They only differ when the text has characters outside the BMP (emoji, some CJK).
 */
export type OffsetUnits = 'codepoint' | 'utf16'

const ASTRAL = /[\uD800-\uDBFF][\uDC00-\uDFFF]/

/** A function mapping a code point offset to the matching UTF-16 offset of `text`. */
export function codePointMapper(text: string): (offset: number) => number {
  if (!ASTRAL.test(text)) return (offset) => offset
  const units: number[] = [0]
  let position = 0
  for (const char of text) {
    position += char.length
    units.push(position)
  }
  const last = units.length - 1
  return (offset) => {
    if (offset <= 0) return 0
    if (offset >= last) return units[last] ?? text.length
    return units[Math.floor(offset)] ?? text.length
  }
}

/**
 * Cut `text` into consecutive segments covering all of it. Ranges outside the text are clamped,
 * empty ones are dropped, and a range overlapping an earlier one is skipped (the daemon merges
 * overlaps, so this only protects the view from malformed data).
 */
export function splitByRanges<R extends Range>(
  text: string,
  ranges: readonly R[],
  units: OffsetUnits = 'codepoint',
): Segment<R>[] {
  const toUnits = units === 'codepoint' ? codePointMapper(text) : (offset: number) => offset
  const length = text.length
  const items = ranges
    .map((range, index) => {
      const start = Math.min(Math.max(0, toUnits(range.start)), length)
      const end = Math.min(Math.max(0, toUnits(range.end)), length)
      return { range, index, start, end }
    })
    .filter((item) => Number.isFinite(item.start) && Number.isFinite(item.end) && item.end > item.start)
    .sort((a, b) => a.start - b.start || b.end - a.end || a.index - b.index)

  const segments: Segment<R>[] = []
  let cursor = 0
  for (const item of items) {
    if (item.start < cursor) continue
    if (item.start > cursor) {
      segments.push({ text: text.slice(cursor, item.start), start: cursor, end: item.start, range: null, index: -1 })
    }
    segments.push({
      text: text.slice(item.start, item.end),
      start: item.start,
      end: item.end,
      range: item.range,
      index: item.index,
    })
    cursor = item.end
  }
  if (cursor < length || segments.length === 0) {
    segments.push({ text: text.slice(cursor), start: cursor, end: length, range: null, index: -1 })
  }
  return segments
}

/** The substring a range covers, with the same offset conventions as `splitByRanges`. */
export function sliceRange(text: string, range: Range, units: OffsetUnits = 'codepoint'): string {
  const toUnits = units === 'codepoint' ? codePointMapper(text) : (offset: number) => offset
  return text.slice(toUnits(range.start), toUnits(range.end))
}

/** Split a text into lines while keeping offsets, for collapsing long blocks. */
export function lineCount(text: string): number {
  if (!text) return 0
  let count = 1
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) count++
  return count
}

/** UTF-16 offset where line `lines` (1-based, exclusive) starts, or the text length. */
export function offsetAfterLines(text: string, lines: number): number {
  let seen = 0
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) === 10) {
      seen++
      if (seen === lines) return i + 1
    }
  }
  return text.length
}
