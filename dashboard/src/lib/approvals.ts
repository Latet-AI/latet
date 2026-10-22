// The Live Reviewer in the dashboard: the queue of batches waiting for you, and the values you mask
// by hand before approving one.

import type { Agent, ApprovalDecision, ApprovalEvent, Batch, LiveItem, ReviewCategory, SettingsChange, SlotKind } from '../api/types'
import { imagesBody, type ImageChoice } from './images'
import { AGENT_LABEL } from './labels'
import { CATEGORY_TYPE, REVIEW_CATEGORIES } from './reviewer'
import type { Range } from './spans'
import { hasLetterOrDigit } from './terms'

/** A value you masked in a batch, waiting to be sent with your approval. */
export interface AddedMask {
  /** The item of the batch it was chosen in. */
  item: number
  /** Offsets in that item's original, in code points (as the daemon counts them). */
  start: number
  end: number
  value: string
  category: ReviewCategory
  /** Also add it to your terms once approved. */
  always: boolean
}

export interface QueueState {
  batches: Batch[]
  /** The change to your settings waiting for your approval: at most one, a newer one replaces it. */
  changes: SettingsChange[]
  viewers: number
  /** The first snapshot arrived. */
  loaded: boolean
}

export const EMPTY_QUEUE: QueueState = { batches: [], changes: [], viewers: 0, loaded: false }

const byCreated = (a: { created: number }, b: { created: number }) => a.created - b.created

/** The queue after an event of the approvals stream: oldest first, each batch once. */
export function applyEvent(state: QueueState, event: ApprovalEvent): QueueState {
  switch (event.type) {
    case 'snapshot':
      return {
        batches: [...event.batches].sort(byCreated),
        changes: [...event.changes].sort(byCreated).slice(-1),
        viewers: event.viewers,
        loaded: true,
      }
    case 'pending':
      if (state.batches.some((b) => b.id === event.batch.id)) return state
      return { ...state, batches: [...state.batches, event.batch].sort(byCreated) }
    case 'updated':
      // In its place; one decided meanwhile does not come back.
      if (!state.batches.some((b) => b.id === event.batch.id)) return state
      return { ...state, batches: state.batches.map((b) => (b.id === event.batch.id ? event.batch : b)) }
    case 'settled':
      return withoutBatch(state, event.id)
    case 'change':
      // At most one waits: a newer change replaces the one before (settled as superseded).
      if (state.changes.some((c) => c.id === event.change.id)) return state
      return { ...state, changes: [event.change] }
    case 'change_settled':
      return withoutChange(state, event.id)
    case 'viewers':
      return { ...state, viewers: event.viewers }
  }
}

export function withoutBatch(state: QueueState, id: string): QueueState {
  if (!state.batches.some((b) => b.id === id)) return state
  return { ...state, batches: state.batches.filter((b) => b.id !== id) }
}

export function withoutChange(state: QueueState, id: string): QueueState {
  if (!state.changes.some((c) => c.id === id)) return state
  return { ...state, changes: state.changes.filter((c) => c.id !== id) }
}

/** Everything waiting for you: the batches and the change to your settings (the menu's count, the tab title). */
export function waitingCount(state: Pick<QueueState, 'batches' | 'changes'>): number {
  return state.batches.length + state.changes.length
}

type Translate = (key: string, options?: Record<string, unknown>) => string

/**
 * Where a change to your settings came from, as one sentence. On a Latet server (`server`) there is no config.yaml: a
 * change found as the settings were applied again (`file`: a change set for everyone, or yours saved on another
 * instance) and one found as your space was made (`start`) each have words of their own.
 */
export function changeOrigin(t: Translate, change: Pick<SettingsChange, 'source' | 'agent'>, server = false): string {
  switch (change.source) {
    case 'command': {
      if (!change.agent) return t('approvals.change.source.commandNoAgent')
      const label = AGENT_LABEL[change.agent as Agent]
      return t('approvals.change.source.command', { agent: label ? t(label) : change.agent })
    }
    case 'file':
      return server ? t('approvals.change.source.serverFile') : t('approvals.change.source.file')
    case 'start':
      return server ? t('approvals.change.source.serverStart') : t('approvals.change.source.start')
    default:
      return t('approvals.change.source.other')
  }
}

/**
 * The batch shown: the one chosen while it waits; once it leaves, the one that took its place in the
 * queue (or the last); the oldest when nothing was chosen.
 */
export function selectedBatch(batches: readonly Batch[], selection: { id: string; index: number } | null): Batch | null {
  if (batches.length === 0) return null
  if (!selection) return batches[0] ?? null
  return batches.find((b) => b.id === selection.id) ?? batches[Math.min(selection.index, batches.length - 1)] ?? null
}

/** Which batches the queue shows: every one (undefined), those of no project (null), or one project's. */
export type ProjectFilter = string | null | undefined

/** The projects with batches waiting, each once, in the order they first wait (null: no project). */
export function projectsWaiting(batches: readonly Pick<Batch, 'project'>[]): (string | null)[] {
  return [...new Set(batches.map((b) => b.project))]
}

/** The batches a project filter shows; a filter whose project has nothing waiting any more shows them all. */
export function inProject<B extends Pick<Batch, 'project'>>(batches: readonly B[], filter: ProjectFilter): readonly B[] {
  if (filter === undefined || !batches.some((b) => b.project === filter)) return batches
  return batches.filter((b) => b.project === filter)
}

/** The kinds of a batch's texts, each once, in order. */
export function batchKinds(batch: Pick<Batch, 'items'>): SlotKind[] {
  return [...new Set(batch.items.map((item) => item.kind))]
}

/** A session key, short enough for a list: its prefix and the first characters of its id. */
export function shortSession(session: string): string {
  const colon = session.indexOf(':')
  if (colon < 0) return session.length > 12 ? `${session.slice(0, 12)}…` : session
  const id = session.slice(colon + 1)
  return `${session.slice(0, colon + 1)}${id.length > 8 ? `${id.slice(0, 8)}…` : id}`
}

export function isCategory(value: string): value is ReviewCategory {
  return (REVIEW_CATEGORIES as readonly string[]).includes(value)
}

/** The code point offset of a UTF-16 offset of `text`. */
export function toCodePoint(text: string, utf16: number): number {
  return [...text.slice(0, utf16)].length
}

const isHigh = (code: number) => code >= 0xd800 && code <= 0xdbff
const isLow = (code: number) => code >= 0xdc00 && code <= 0xdfff
const SPACE = /\s/
const WORD = /[\p{L}\p{N}_]/u

export type SelectionCheck =
  | { ok: true; start: number; end: number; value: string }
  | { ok: false; reason: 'empty' | 'short' | 'noword' | 'masked' | 'added' }

const overlaps = (a: Range, b: Range) => a.start < b.end && b.start < a.end

/**
 * A selection of an item's original (UTF-16 offsets, as the browser gives them) as a value to mask:
 * spaces around it left out, at least two characters, over clear text only (`masked`: the values
 * Latet masked already; `added`: those you masked, both in code points).
 */
export function checkSelection(
  original: string,
  start16: number,
  end16: number,
  masked: readonly Range[],
  added: readonly Range[],
): SelectionCheck {
  let start = Math.max(0, Math.min(start16, end16))
  let end = Math.min(original.length, Math.max(start16, end16))
  // Never half a character outside the BMP.
  if (start > 0 && isLow(original.charCodeAt(start)) && isHigh(original.charCodeAt(start - 1))) start--
  if (end < original.length && isLow(original.charCodeAt(end)) && isHigh(original.charCodeAt(end - 1))) end++
  while (start < end && SPACE.test(original[start] ?? '')) start++
  while (end > start && SPACE.test(original[end - 1] ?? '')) end--
  if (end <= start) return { ok: false, reason: 'empty' }
  // Whole words: the daemon masks a value wherever it appears as a word.
  while (start > 0 && WORD.test(original[start - 1] ?? '')) start--
  while (end < original.length && WORD.test(original[end] ?? '')) end++
  if (!hasLetterOrDigit(original.slice(start, end))) return { ok: false, reason: 'noword' }
  const range = { start: toCodePoint(original, start), end: toCodePoint(original, end) }
  if (range.end - range.start < 2) return { ok: false, reason: 'short' }
  if (masked.some((r) => overlaps(r, range))) return { ok: false, reason: 'masked' }
  if (added.some((r) => overlaps(r, range))) return { ok: false, reason: 'added' }
  return { ok: true, ...range, value: original.slice(start, end) }
}

/** What stands in a preview for a value you masked: the daemon picks the real fake when you approve. */
export function placeholder(category: ReviewCategory): string {
  return `⟦${CATEGORY_TYPE[category]}⟧`
}

export interface Found<K> extends Range {
  key: K
}

/**
 * Where the values you masked appear in `text` (UTF-16 offsets): every occurrence outside `taken`,
 * the longest value first where two overlap. The daemon masks a value wherever it appears.
 */
export function findValues<K>(text: string, values: readonly { value: string; key: K }[], taken: readonly Range[]): Found<K>[] {
  const out: Found<K>[] = []
  const sorted = [...values].sort((a, b) => b.value.length - a.value.length)
  for (const { value, key } of sorted) {
    if (!value) continue
    let at = text.indexOf(value)
    while (at >= 0) {
      const range = { start: at, end: at + value.length }
      if (!taken.some((r) => overlaps(r, range)) && !out.some((r) => overlaps(r, range))) out.push({ ...range, key })
      at = text.indexOf(value, at + 1)
    }
  }
  return out.sort((a, b) => a.start - b.start)
}

/**
 * The text that will be sent, as far as the dashboard can tell before you approve: `masked` with the
 * values you masked replaced by a placeholder, the marks of what Latet masked moved to match (all
 * UTF-16 offsets).
 */
export function previewSent<M extends Range, K>(
  masked: string,
  marks: readonly M[],
  added: readonly { value: string; category: ReviewCategory; key: K }[],
): { text: string; marks: M[]; added: (Found<K> & { category: ReviewCategory })[] } {
  const categories = new Map(added.map((a) => [a.key, a.category]))
  const hits = findValues(masked, added, marks)
  if (hits.length === 0) return { text: masked, marks: [...marks], added: [] }
  let text = ''
  let pos = 0
  const placed: (Found<K> & { category: ReviewCategory })[] = []
  const shifts: { at: number; delta: number }[] = []
  let delta = 0
  for (const hit of hits) {
    const category = categories.get(hit.key) ?? 'sensitive'
    const shown = placeholder(category)
    text += masked.slice(pos, hit.start)
    placed.push({ start: text.length, end: text.length + shown.length, key: hit.key, category })
    text += shown
    pos = hit.end
    delta += shown.length - (hit.end - hit.start)
    shifts.push({ at: hit.end, delta })
  }
  text += masked.slice(pos)
  const shiftOf = (offset: number) => {
    let d = 0
    for (const s of shifts) if (s.at <= offset) d = s.delta
    return d
  }
  const moved = marks.map((m) => ({ ...m, start: m.start + shiftOf(m.start), end: m.end + shiftOf(m.start) }))
  return { text, marks: moved, added: placed }
}

/** The replacements of an item on its original, in code points (to refuse a selection over them). */
export function maskedRanges(item: Pick<LiveItem, 'replacements'>): Range[] {
  return item.replacements.map((r) => ({ start: r.start, end: r.end }))
}

/** The suggestions of an item still open: a known category, not covered by a value you masked. */
export function openSuggestions(item: Pick<LiveItem, 'suggestions'>, added: readonly Range[]): { start: number; end: number; category: ReviewCategory }[] {
  return item.suggestions
    .filter((s): s is typeof s & { category: ReviewCategory } => isCategory(s.category))
    .filter((s) => !added.some((a) => overlaps(a, s)))
}

/** The body of an approval: the values you masked, each once per item. */
export function approveBody(masks: readonly AddedMask[], images: Readonly<Record<number, ImageChoice>> = {}): ApprovalDecision {
  const decided = imagesBody(images)
  const body: ApprovalDecision = { action: 'approve' }
  // With its value: a text shown anew while you looked (masked again) can have moved it, and the daemon finds it again.
  if (masks.length > 0) body.masks = masks.map(({ item, start, end, value, category }) => ({ item, start, end, value, category }))
  if (decided.length > 0) body.images = decided
  return body
}
