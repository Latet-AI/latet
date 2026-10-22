// From the API's replacements to highlighted ranges on both sides of a block.

import type { ExchangeDetail, Replacement } from '../../api/types'
import { stoppedValues, type StoppedValue } from '../../lib/blocks'
import { isBlocked } from '../../lib/flags'
import { viewClass } from '../../lib/labels'
import { alignRestored, type KnownValue } from '../../lib/restore'
import { codePointMapper } from '../../lib/spans'
import type { Side, SpanInfo, TextMark } from './types'

export interface BlockMarks {
  /** Marks on the original text; null when the original is unknown. */
  original: TextMark[] | null
  sent: TextMark[]
  /** One entry per value, in footnote order. */
  values: SpanInfo[]
}

/**
 * Marks for a block given as (original, masked, replacements): a request slot, a playground result or
 * a response the daemon annotated. Offsets from the API count code points. `stopped` are the values of
 * the block that stopped the request: in clear in both texts, marked beside the masked values but not
 * among them (`values` lists what was masked).
 */
export function blockMarks(
  original: string | null,
  masked: string,
  replacements: readonly Replacement[],
  scope: string,
  sides: [Side, Side] = ['original', 'sent'],
  stopped: readonly StoppedValue[] = [],
): BlockMarks {
  const toSent = codePointMapper(masked)
  const toOriginal = original === null ? null : codePointMapper(original)
  const ordered = replacements
    .map((r, i) => ({ r, i }))
    .sort((a, b) => a.r.masked_start - b.r.masked_start || a.i - b.i)
    .map(({ r }) => r)

  const values: SpanInfo[] = []
  const sent: TextMark[] = []
  const originalMarks: TextMark[] | null = toOriginal ? [] : null
  ordered.forEach((r, index) => {
    const sentStart = toSent(r.masked_start)
    const sentEnd = toSent(r.masked_end)
    const known = r.known !== false
    const redacted = r.entity === null
    const sentValue = masked.slice(sentStart, sentEnd)
    let originalValue: string | null = null
    let originalRange: { start: number; end: number } | null = null
    if (original !== null && toOriginal) {
      originalRange = { start: toOriginal(r.start), end: toOriginal(r.end) }
      if (known) originalValue = original.slice(originalRange.start, originalRange.end)
    }
    const base: Omit<SpanInfo, 'side'> = {
      pair: `${scope}:${index}`,
      footnote: index + 1,
      type: r.type,
      cls: viewClass(r.class),
      rule: r.rule || null,
      module: r.module ?? null,
      via: r.via ?? null,
      entity: r.entity,
      original: originalValue,
      sent: sentValue,
      redacted,
      known,
      json: r.json === true,
    }
    values.push({ ...base, side: sides[1] })
    sent.push({ start: sentStart, end: sentEnd, info: { ...base, side: sides[1] } })
    if (originalMarks && originalRange) originalMarks.push({ ...originalRange, info: { ...base, side: sides[0] } })
  })
  stoppedGroups(stopped).forEach((group, index) => {
    const [first] = group
    if (!first) return
    const sentStart = toSent(first.masked_start)
    const sentEnd = toSent(Math.max(...group.map((value) => value.masked_end)))
    // The text as it was to be sent is what the check read: what Not sensitive must name to let it through,
    // for each way it was read.
    const text = masked.slice(sentStart, sentEnd)
    const read = [...new Set(group.map((value) => masked.slice(toSent(value.masked_start), toSent(value.masked_end))))]
    const base: Omit<SpanInfo, 'side'> = {
      pair: `${scope}:stopped:${index}`,
      footnote: null,
      type: first.type,
      cls: viewClass(first.class ?? '?'),
      rule: first.rule ?? null,
      module: null,
      via: null,
      entity: null,
      original: text,
      sent: text,
      redacted: false,
      known: true,
      json: false,
      stopped: true,
      texts: read.some((one) => one !== '') ? read.filter((one) => one !== '') : [text],
    }
    sent.push({ start: sentStart, end: sentEnd, info: { ...base, side: sides[1] } })
    if (originalMarks && toOriginal) {
      const start = toOriginal(Math.min(...group.map((value) => value.start)))
      const end = toOriginal(Math.max(...group.map((value) => value.end)))
      originalMarks.push({ start, end, info: { ...base, side: sides[0] } })
    }
  })
  return { original: originalMarks, sent, values }
}

/**
 * The values that stopped a request, grouped as the page marks them. Two rules can read one value, one inside
 * the other (a PEM key whole, and its body) or across: under the outer mark the inner one would be skipped,
 * with nothing to click. Those that overlap are one group, the outermost first; values that only touch are not.
 */
function stoppedGroups(stopped: readonly StoppedValue[]): StoppedValue[][] {
  const groups: StoppedValue[][] = []
  let reach = -1 // where the last group ends, in the block as it was sent
  const ordered = [...stopped].sort((a, b) => a.masked_start - b.masked_start || b.masked_end - a.masked_end)
  for (const value of ordered) {
    const last = groups[groups.length - 1]
    if (last && value.masked_start < reach) {
      last.push(value)
      reach = Math.max(reach, value.masked_end)
    } else {
      groups.push([value])
      reach = value.masked_end
    }
  }
  return groups
}

/**
 * The marks of every block of a request, scoped as the exchange page scopes them (`<exchange>:<block>`):
 * the page draws these, and the tutorial finds in them what it points at. The values that stopped a
 * blocked request are marked in the block they are in.
 */
export function requestMarks(detail: ExchangeDetail): BlockMarks[] {
  const stopped = isBlocked(detail.summary.flags) ? stoppedValues(detail.leaks) : []
  return detail.request.slots.map((slot, i) =>
    blockMarks(
      slot.original,
      slot.masked,
      slot.replacements,
      `${detail.summary.id}:${i}`,
      ['original', 'sent'],
      stopped.filter((value) => value.block === slot.block),
    ),
  )
}

/** Surrogates known from the request blocks, to find them again in the response. */
export function knownValues(blocks: readonly BlockMarks[]): KnownValue<Omit<SpanInfo, 'side' | 'pair' | 'footnote'>>[] {
  const out: KnownValue<Omit<SpanInfo, 'side' | 'pair' | 'footnote'>>[] = []
  const seen = new Set<string>()
  for (const block of blocks) {
    for (const value of block.values) {
      if (seen.has(value.sent)) continue
      seen.add(value.sent)
      const info: Omit<SpanInfo, 'side' | 'pair' | 'footnote'> = {
        type: value.type,
        cls: value.cls,
        rule: value.rule,
        module: value.module,
        via: value.via,
        entity: value.entity,
        original: value.original,
        sent: value.sent,
        redacted: value.redacted,
        known: value.known,
        json: value.json,
      }
      out.push({ surrogate: value.sent, original: value.known ? value.original : null, info })
    }
  }
  return out
}

/**
 * Marks for a response text: the API gives the model's text and what the agent received, without
 * offsets, so known surrogates are located in the first and aligned in the second.
 */
export function responseMarks(
  masked: string,
  restored: string,
  known: ReturnType<typeof knownValues>,
  scope: string,
): { model: TextMark[]; agent: TextMark[]; aligned: boolean } {
  const alignment = alignRestored(masked, restored, known)
  const model: TextMark[] = []
  const agent: TextMark[] = []
  alignment.masked.forEach((found, index) => {
    const restoredRange = alignment.aligned ? alignment.restored[index] : undefined
    const restoredValue = restoredRange ? restored.slice(restoredRange.start, restoredRange.end) : null
    const base = {
      ...found.info,
      pair: `${scope}:${index}`,
      footnote: index + 1,
      sent: masked.slice(found.start, found.end),
      original: restoredValue ?? found.info.original,
      via: 'restore' as const,
    }
    model.push({ start: found.start, end: found.end, info: { ...base, side: 'model' }, soft: true })
    if (restoredRange) agent.push({ ...restoredRange, info: { ...base, side: 'agent' }, soft: true })
  })
  return { model, agent, aligned: alignment.aligned }
}

/** The scope of the marks of the response's text, and of the tool call at `index` (the pairs the page highlights). */
export const RESPONSE_TEXT_SCOPE = 'response'
export const toolScope = (index: number) => `tool${index}`

/**
 * Highlights for a response text: the daemon's spans when it sent them (offsets on both sides),
 * otherwise known surrogates found in the model's text and lined up with what the agent received.
 */
export function pairMarks(
  masked: string,
  restored: string,
  spans: readonly Replacement[] | undefined,
  known: ReturnType<typeof knownValues>,
  scope: string,
): { model: TextMark[]; agent: TextMark[]; aligned: boolean } {
  if (spans) {
    const marks = blockMarks(restored, masked, spans, scope, ['agent', 'model'])
    return { model: marks.sent, agent: marks.original ?? [], aligned: true }
  }
  return responseMarks(masked, restored, known, scope)
}
