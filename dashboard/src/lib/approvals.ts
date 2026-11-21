// The Live Reviewer in the dashboard: the queue of batches waiting for you, and the values you mask
// by hand before approving one.

import { ApiError } from '../api/client'
import type { Agent, ApprovalDecision, ApprovalEvent, Batch, Decides, LiveItem, ReviewCategory, SettingsChange, SlotKind } from '../api/types'
import { tk } from '../i18n'
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
    case 'withheld': // approved by the team, withheld as it left (Latet Enterprise): settled already, as a rule
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

/** What waits for you to decide: the batches and the change to your settings (the menu's count, the tab title); not what
 * someone else decides on. */
export function waitingCount(state: Pick<QueueState, 'batches' | 'changes'>): number {
  return state.batches.filter((b) => !othersDecide(b)).length + state.changes.filter((c) => !othersDecide(c)).length
}

/** Whether someone else decides on it (Latet Enterprise): you may stop or undo it, not approve it. */
export function othersDecide(item: { decides?: Decides }): boolean {
  return item.decides !== undefined && item.decides !== 'self'
}

/** The batches that wait for your approval: not those your company's security team decides on. */
export function yourBatches<B extends { decides?: Decides }>(batches: readonly B[]): B[] {
  return batches.filter((b) => !othersDecide(b))
}

/**
 * The Live Reviewer's words, by where its texts go (/me's `edition.approvals.live_review`): to you, or to your company's
 * security team (Latet Enterprise), who see each text values included and decide on it; you may only stop it. `team`:
 * whether they go to the team. Each word is a translation key.
 */
export function liveReviewWords(routes: { live_review: Decides } | null | undefined) {
  const team = routes?.live_review === 'security'
  return {
    team,
    /** The Rules page's Live Reviewer section. */
    description: team ? tk('liveReview.descriptionSecurity') : tk('liveReview.description'),
    enabled: team ? tk('liveReview.enabledSecurity') : tk('liveReview.enabled'),
    kinds: team ? tk('liveReview.kindsSecurity') : tk('liveReview.kinds'),
    timeoutHelp: team ? tk('liveReview.timeoutHelpSecurity') : tk('liveReview.timeoutHelp'),
    /** The switch of the Live and Approvals pages, and its message. */
    switchOn: team ? tk('liveReview.switchOnSecurity') : tk('liveReview.switchOn'),
    turnedOn: team ? tk('liveReview.turnedOnSecurity') : tk('liveReview.turnedOn'),
    /** The Approvals page: its description, what its empty queue says, and what it says while the reviewer is off. */
    page: team ? tk('approvals.descriptionSecurity') : tk('approvals.description'),
    empty: team ? tk('approvals.empty.bodySecurity') : tk('approvals.empty.body'),
    off: team ? tk('approvals.off.bodySecurity') : tk('approvals.off.body'),
    /** The name a screen reader gives the Approvals page's queue of texts. */
    queue: team ? tk('approvals.queue.labelSecurity') : tk('approvals.queue.label'),
    /** A project's "Live Reviewer here" (its rules' page). */
    projectHelp: team ? tk('projects.live.helpSecurity') : tk('projects.live.help'),
  }
}

/** The name a screen reader gives the tabs of Approvals: what waits for you, or, when someone else decides on your
 * changes or your texts (Latet Enterprise), what waits for a decision. A translation key. */
export function tabsLabel(routes: { changes: Decides; live_review: Decides } | null | undefined): string {
  const others = routes != null && (routes.changes !== 'self' || routes.live_review !== 'self')
  return others ? tk('approvals.tabs.labelOthers') : tk('approvals.tabs.label')
}

/** What the Settings tab of Approvals says while no change waits: who a change that lowers protection would wait for
 * (/me's `edition.approvals.changes`). A translation key. */
export function settingsEmptyWords(routes: { changes: Decides } | null | undefined): string {
  if (routes?.changes === 'security') return tk('approvals.settingsEmpty.bodySecurity')
  if (routes?.changes === 'controllers') return tk('approvals.settingsEmpty.bodyControllers')
  return tk('approvals.settingsEmpty.body')
}

/**
 * What a save held for whoever decides says (the Rules page): that it waits for them and the form shows the settings
 * in use, once they were read again (`remade`); else only that it waits, the form still holding the change.
 */
export function heldSaveWords(pending: Pick<SettingsChange, 'decides'>, remade: boolean): string {
  if (!remade) return tk('errors.settingsWaitingForOthers')
  return pending.decides === 'controllers' ? tk('approvals.whoDecides.sentControllers') : tk('approvals.whoDecides.sent')
}

type Translate = (key: string, options?: Record<string, unknown>) => string

/**
 * Who decides, for a sentence: their names as the language lists them, then how many more there are past those the
 * server names (`more`: /me's `deciders_more` or `text_deciders_more`); or that nobody can yet.
 */
export function decidersText(
  t: Translate,
  list: (items: readonly string[]) => string,
  deciders: readonly string[] | undefined,
  more = 0,
): string {
  if (!deciders || deciders.length === 0) return t('approvals.whoDecides.nobody')
  return list(more > 0 ? [...deciders, t('approvals.whoDecides.more', { count: more })] : deciders)
}

/**
 * What you are told when someone else decided on your change (Latet Enterprise, `by` set): the words' key, which names
 * them as `name`, and the message's tone. An approval your settings did not take (R60), or that came after they moved
 * on (stale), applied nothing: ask again. Null for your own decisions and for ends nobody decided.
 */
export function decidedBy(
  event: Extract<ApprovalEvent, { type: 'change_settled' }>,
): { key: string; tone: 'ok' | 'info' | 'fail' } | null {
  if (!event.by) return null
  switch (event.state) {
    case 'approved':
      return { key: tk('approvals.whoDecides.approvedBy'), tone: 'ok' }
    case 'rejected':
      return { key: tk('approvals.whoDecides.rejectedBy'), tone: 'info' }
    case 'approved-unapplied':
    case 'stale':
      return { key: tk('approvals.whoDecides.unappliedBy'), tone: 'fail' }
    default:
      return null
  }
}

/** How many deciders the server names on an item at most (its MOST_NAMED): an item carries no count of the others. */
export const MOST_NAMED = 20

/** How many more decide on an item than it names: /me's count for the same route (`counted`) when the item's list is
 * full, else none (the whole team is named). */
export function moreDeciders(names: readonly string[] | undefined, counted: number | undefined): number {
  return names !== undefined && names.length >= MOST_NAMED ? (counted ?? 0) : 0
}

/** The change a save in the dashboard made, waiting for whoever decides on it (the daemon's 409), or null. */
export function pendingOf(error: unknown): SettingsChange | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const pending = (error.body as { pending?: unknown } | undefined)?.pending
  if (!pending || typeof pending !== 'object' || typeof (pending as { id?: unknown }).id !== 'string') return null
  return pending as SettingsChange
}

/**
 * Where a change to your settings came from, as one sentence. On a Latet server (`server`) there is no config.yaml: a
 * change found as the settings were applied again (`file`: a change set for everyone, or yours saved on another
 * instance) and one found as your space was made (`start`) each have words of their own, which say that the settings
 * you approved run until someone else decides when it is theirs to decide (Latet Enterprise).
 */
export function changeOrigin(t: Translate, change: Pick<SettingsChange, 'source' | 'agent' | 'decides'>, server = false): string {
  const others = othersDecide(change)
  switch (change.source) {
    case 'command': {
      if (!change.agent) return t('approvals.change.source.commandNoAgent')
      const label = AGENT_LABEL[change.agent as Agent]
      return t('approvals.change.source.command', { agent: label ? t(label) : change.agent })
    }
    case 'file':
      if (!server) return t('approvals.change.source.file')
      return others ? t('approvals.change.source.serverFileOthers') : t('approvals.change.source.serverFile')
    case 'start':
      if (!server) return t('approvals.change.source.start')
      return others ? t('approvals.change.source.serverStartOthers') : t('approvals.change.source.serverStart')
    case 'dashboard':
      return t('approvals.change.source.dashboard')
    default:
      return t('approvals.change.source.other')
  }
}

/**
 * The batch shown: the one chosen while it waits; once it leaves, the one that took its place in the
 * queue (or the last); the oldest when nothing was chosen.
 */
export function selectedBatch<B extends Pick<Batch, 'id'>>(batches: readonly B[], selection: { id: string; index: number } | null): B | null {
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
