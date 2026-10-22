// The Live Reviewer in mock mode: a queue of texts waiting for approval, with a new batch every few
// seconds while the Live Reviewer is on, and a change to your settings that lowers protection waiting
// for you. ?mock-approvals-empty starts with an empty queue that stays so, and no change; so does
// ?mock-tutorial, where the tutorial puts in the queue what its chapters need.

import { ApiError, type Method, type StreamState } from '../api/client'
import type {
  Agent,
  ApprovalEvent,
  ApprovalMask,
  Batch,
  ChangeState,
  ConfigView,
  LiveItem,
  SettingsChange,
  SlotKind,
} from '../api/types'
import { isCategory } from '../lib/approvals'
import { MAX_RECTS, MIN_RECT, textRects } from '../lib/images'
import { CC_SESSION, CODEX_SESSION, drawing, drawnLines, MOCK_ASSETS, MOCK_GUIDANCE, OC_SESSION } from './fixtures'
import { mockMask } from './playground'
import { projectOfAgent } from './projects'
import { seeded, token, URLSAFE } from './rng'

const EMPTY = window.location.search.includes('mock-approvals-empty') || window.location.search.includes('mock-tutorial')
const rng = seeded(7310)

interface ItemTemplate {
  kind: SlotKind
  path: string | null
  tool?: string
  text: string
  /** What the local reviewer would suggest: a word of the text and its category. */
  suggest?: [string, string][]
  failed?: string
  /** The text was read in a screenshot (kind image_text): the image is a drawing of it. */
  image?: boolean
}

interface BatchTemplate {
  agent: Agent
  items: ItemTemplate[]
}

const SESSION: Record<Agent, string> = {
  'claude-code': CC_SESSION,
  codex: CODEX_SESSION,
  opencode: OC_SESSION,
  'gemini-cli': 'conv:3f9a1c6e0b2d4f8a',
  'tutorial-agent': 'x-session-id:tutorial-0001',
  other: 'conv:8c2e4a7f1b9d3e5a',
}

const TEMPLATES: (() => BatchTemplate)[] = [
  () => ({
    agent: 'claude-code',
    items: [
      {
        kind: 'user_text',
        path: '$.messages[14].content[0].text',
        text: 'Thanks 👍 Now try the webhook with the test customer Emily Carter, emily.carter@northwind-legal.com, phone +44 20 7946 0958.',
        suggest: [['Emily Carter', 'person']],
      },
    ],
  }),
  () => ({
    agent: 'codex',
    items: [
      {
        kind: 'tool_result',
        path: '$.input[6].output',
        tool: 'shell',
        text: `$ cat .env\nDATABASE_PASSWORD=${token(rng, 'Xk', 10, URLSAFE)}\nSTRIPE_SECRET_KEY=${token(rng, 'sk_live_', 24)}\nSUPPORT_CONTACT=Mark Ellis\nSUPPORT_NOTES=call after 6 pm, on treatment for diabetes\n`,
        suggest: [
          ['Mark Ellis', 'person'],
          ['treatment for diabetes', 'health'],
        ],
      },
    ],
  }),
  () => ({
    agent: 'claude-code',
    items: [
      {
        kind: 'image_text',
        path: '$.messages[18].content[0].content[0]#text',
        tool: 'Read',
        text: 'Invoice 2026/118 - Hayes & Partners\nCustomer: Whitfield & Co, VAT GB570092021\nIBAN GB82WEST12345698765432\nContact: Emily Carter, emily.carter@northwind-legal.com\nNote: deliver to 42 Mill Lane, Bristol',
        image: true,
        suggest: [
          ['Emily Carter', 'person'],
          ['42 Mill Lane, Bristol', 'address'],
        ],
      },
    ],
  }),
  () => ({
    agent: 'opencode',
    items: [
      {
        kind: 'attachment',
        path: '$.messages[3].content[1].text',
        text: 'Minutes of the meeting with Acme Logistics: Project Aurora starts in November.\nContact: Laura Bass (at the cardiologist on Tuesday, so away).\nBudget approved: £48,000.',
        suggest: [
          ['Acme Logistics', 'org'],
          ['Laura Bass', 'person'],
          ['at the cardiologist', 'health'],
        ],
      },
    ],
  }),
  () => ({
    agent: 'claude-code',
    items: [
      {
        kind: 'system',
        path: '$.system[2].text',
        text: 'Contents of C:\\Users\\dhayes\\dev\\billing-app\\CLAUDE.md:\n\n# billing-app\nInvoices for Whitfield & Co and the other clients of Hayes & Partners. Never commit .env.',
      },
      {
        kind: 'user_text',
        path: '$.messages[16].content[0].text',
        text: 'Write to Whitfield & Co that invoice 2026/118 is ready, copying Paul Ferris.',
        suggest: [['Paul Ferris', 'person']],
      },
    ],
  }),
  () => ({
    agent: 'codex',
    items: [
      {
        kind: 'tool_result',
        path: '$.input[9].output',
        tool: 'shell',
        text: '2026-10-07 09:14:02 INFO  payment received for invoice 2026/117\n2026-10-07 09:14:03 WARN  retrying notification to daniel.hayes@hayes-partners.com\n2026-10-07 09:14:05 INFO  done in 3.1 s',
        failed: 'timeout',
      },
    ],
  }),
]

let batches: Batch[] = []
let changes: SettingsChange[] = []
let applyChange: (change: SettingsChange) => void = () => undefined
let made = 0
const listeners = new Set<(event: ApprovalEvent) => void>()

/** What was decided in the queue: a batch (with the values masked by hand) or the change to the settings. */
export type Decision =
  | { kind: 'batch'; id: string; action: 'approve' | 'reject'; masks: ApprovalMask[] }
  | { kind: 'change'; id: string; action: 'approve' | 'reject' }
const decisions = new Set<(decision: Decision) => void>()
const applies = new Map<string, () => void>()

/** Told of each decision, after the queue has changed. Returns a function that stops it. */
export function onDecided(listener: (decision: Decision) => void): () => void {
  decisions.add(listener)
  return () => decisions.delete(listener)
}

/** A batch waiting for your approval, as if an agent had sent it (the tutorial's chapters). */
export function addBatch(batch: Batch): void {
  batches = [...batches, batch]
  emit({ type: 'pending', batch })
}

/** A change to your settings waiting for you; `apply` runs if you approve it. */
export function addChange(change: SettingsChange, apply: () => void): void {
  applies.set(change.id, apply)
  changes = [change]
  emit({ type: 'change', change })
}

/** Takes away what waits: the tutorial starts a chapter over. */
export function resetQueue(): void {
  for (const batch of [...batches]) settle(batch.id, 'rejected')
  for (const change of [...changes]) settleChange(change.id, 'superseded')
  applies.clear()
}

function emit(event: ApprovalEvent) {
  for (const listener of listeners) listener(structuredClone(event))
}

/** Where a word of the text is, in code points, like the daemon's offsets. */
function codePointsOf(text: string, word: string): { start: number; end: number } | null {
  const at = text.indexOf(word)
  if (at < 0) return null
  const start = [...text.slice(0, at)].length
  return { start, end: start + [...word].length }
}

function makeItem(template: ItemTemplate, config: ConfigView): LiveItem {
  const result = mockMask(template.text, config.terms, config.allow_values)
  const replacements = result.replacements
  const suggestions = (template.suggest ?? [])
    .map(([word, category]) => ({ range: codePointsOf(template.text, word), category }))
    .filter((s): s is { range: { start: number; end: number }; category: string } => s.range !== null)
    .filter((s) => !replacements.some((r) => r.start < s.range.end && s.range.start < r.end))
    .map((s) => ({ ...s.range, category: s.category }))
  const item: LiveItem = {
    kind: template.kind,
    path: template.path,
    tool: template.tool ?? null,
    original: template.text,
    masked: result.masked,
    replacements,
    suggestions,
    failed: template.failed ?? null,
  }
  if (template.image) {
    const { width, height } = drawing(template.text)
    const image = { width, height, media_type: 'image/png', lines: drawnLines(template.text), covered: [], rects: [], withheld: false }
    item.image = {
      ...image,
      covered: replacements.flatMap((r) => textRects(image, r.start, r.end).map((rect) => ({ rect, start: r.start, end: r.end, type: r.type }))),
    }
  }
  return item
}

function makeBatch(config: ConfigView, created: number): Batch {
  const template = TEMPLATES[made++ % TEMPLATES.length]!()
  const timeout = config.live_review.timeout_s
  const batch: Batch = {
    id: `ap_${Math.floor(created * 1000).toString(36)}${made}`,
    session: SESSION[template.agent],
    project: projectOfAgent(template.agent)?.id ?? null,
    agent: template.agent,
    created,
    expires: timeout ? created + timeout : null,
    items: template.items.map((item) => makeItem(item, config)),
  }
  batch.items.forEach((item, index) => {
    if (!item.image) return
    MOCK_ASSETS[`/approvals/${batch.id}/images/${index}`] = drawing(item.original).url
    MOCK_ASSETS[`/approvals/${batch.id}/images/${index}?view=sent`] = drawing(item.masked).url
  })
  return batch
}

/** The values you mask, checked as the daemon checks them. */
function checkMasks(batch: Batch, masks: unknown): void {
  if (!Array.isArray(masks)) throw new ApiError(400, 'action is approve or reject; masks a list')
  for (const raw of masks as { item?: number; start?: number; end?: number; category?: string }[]) {
    const item = batch.items[raw.item ?? -1]
    if (!item) throw new ApiError(422, `there is no text ${raw.item} in this batch`)
    const chars = [...item.original]
    const start = raw.start ?? -1
    const end = raw.end ?? -1
    if (!(start >= 0 && start < end && end <= chars.length)) throw new ApiError(422, 'a mask must lie inside its text')
    if (!isCategory(raw.category ?? '')) throw new ApiError(422, `unknown category '${raw.category}'`)
    const value = chars.slice(start, end).join('')
    if (value !== value.trim() || chars.slice(start, end).length < 2) {
      throw new ApiError(422, 'a masked value has two characters or more, with no space around it')
    }
    if (item.replacements.some((r) => r.start < end && start < r.end)) throw new ApiError(422, 'this part is masked already')
  }
}

/** What you did to the images, checked as the daemon checks it. */
function checkImages(batch: Batch, images: unknown): void {
  if (!Array.isArray(images)) throw new ApiError(400, 'action is approve or reject; masks and images lists')
  for (const raw of images as { item?: number; rects?: unknown; withhold?: unknown }[]) {
    const image = batch.items[raw.item ?? -1]?.image
    if (!image) throw new ApiError(422, `there is no image ${raw.item} in this batch`)
    const rects = (raw.rects ?? []) as number[][]
    if (!Array.isArray(rects) || rects.length > MAX_RECTS) throw new ApiError(422, `at most ${MAX_RECTS} rectangles on an image`)
    for (const [x0 = -1, y0 = -1, x1 = -1, y1 = -1] of rects) {
      if (!(x0 >= 0 && y0 >= 0 && x1 <= image.width && y1 <= image.height)) throw new ApiError(422, 'a rectangle must lie inside its image')
      if (x1 - x0 < MIN_RECT || y1 - y0 < MIN_RECT) throw new ApiError(422, `a rectangle is ${MIN_RECT} pixels or more on each side`)
    }
  }
}

function settle(id: string, state: 'approved' | 'rejected' | 'expired') {
  batches = batches.filter((b) => b.id !== id)
  emit({ type: 'settled', id, state })
}

function settleChange(id: string, state: ChangeState) {
  changes = changes.filter((c) => c.id !== id)
  emit({ type: 'change_settled', id, state })
}

export function routeApprovals(method: Method, path: string, body: unknown): unknown {
  if (method === 'GET' && path === '/changes') return { changes: structuredClone(changes) }
  const changeMatch = path.match(/^\/changes\/([^/]+)$/)
  if (method === 'POST' && changeMatch) {
    const { action } = (body ?? {}) as { action?: string }
    if (action !== 'approve' && action !== 'reject') throw new ApiError(400, 'action is approve or reject')
    const change = changes.find((c) => c.id === decodeURIComponent(changeMatch[1] ?? ''))
    if (!change) throw new ApiError(409, 'it is not waiting any more')
    if (action === 'approve') (applies.get(change.id) ?? (() => applyChange(change)))()
    const state = action === 'approve' ? 'approved' : 'rejected'
    settleChange(change.id, state)
    applies.delete(change.id)
    for (const listener of decisions) listener({ kind: 'change', id: change.id, action })
    return { ok: true, state }
  }
  if (method === 'GET' && path === '/approvals') return { batches: structuredClone(batches), viewers: listeners.size }
  if (method === 'POST' && path === '/approvals/approve-all') {
    const filter = body && typeof body === 'object' && 'project' in body ? (body as { project: string | null }) : null
    const chosen = batches.filter((b) => filter === null || b.project === filter.project)
    for (const batch of chosen) {
      settle(batch.id, 'approved')
      for (const listener of decisions) listener({ kind: 'batch', id: batch.id, action: 'approve', masks: [] })
    }
    return { approved: chosen.length }
  }
  const match = path.match(/^\/approvals\/([^/]+)$/)
  if (method === 'POST' && match) {
    const { action, masks = [], images = [] } = (body ?? {}) as { action?: string; masks?: unknown; images?: unknown }
    if (action !== 'approve' && action !== 'reject') throw new ApiError(400, 'action is approve or reject; masks a list')
    const batch = batches.find((b) => b.id === decodeURIComponent(match[1] ?? ''))
    if (!batch) throw new ApiError(409, 'it is not waiting any more')
    if (action === 'approve') {
      checkMasks(batch, masks)
      checkImages(batch, images)
    }
    settle(batch.id, action === 'approve' ? 'approved' : 'rejected')
    for (const listener of decisions) listener({ kind: 'batch', id: batch.id, action, masks: action === 'approve' ? (masks as ApprovalMask[]) : [] })
    return { ok: true }
  }
  return undefined
}

export function subscribeApprovals(onEvent: (event: ApprovalEvent) => void, onState: (state: StreamState) => void): () => void {
  onState('connecting')
  const opened = setTimeout(() => {
    listeners.add(onEvent)
    onState('open')
    onEvent({ type: 'snapshot', batches: structuredClone(batches), changes: structuredClone(changes), viewers: listeners.size })
    emit({ type: 'viewers', viewers: listeners.size })
  }, 300)
  return () => {
    clearTimeout(opened)
    if (listeners.delete(onEvent)) emit({ type: 'viewers', viewers: listeners.size })
    onState('closed')
  }
}

/**
 * Starts the queue: two batches waiting already, then a new one every 6 to 10 seconds while it is on;
 * and a change an agent made to your settings, waiting (`apply` applies it when you approve it).
 */
export function startApprovals(config: () => ConfigView, apply: (change: SettingsChange) => void): void {
  applyChange = apply
  if (EMPTY) return
  const now = Date.now() / 1000
  batches = [makeBatch(config(), now - 95), makeBatch(config(), now - 40)]
  changes = [
    {
      id: 'ch_mock1',
      created: now - 130,
      source: 'command',
      agent: 'hermes',
      loosened: ['values never to mask: 1 added', 'the Live Reviewer: turned off', 'the local reviewer: its guidance changed'],
      keys: ['allow_values', 'live_review', 'llm_review'],
      guidance: [{ where: null, before: MOCK_GUIDANCE, after: `${MOCK_GUIDANCE}\nDo not report the names of our suppliers.` }],
    },
  ]
  const next = () =>
    setTimeout(
      () => {
        const current = config()
        if (current.live_review.enabled && batches.length < 6) {
          const batch = makeBatch(current, Date.now() / 1000)
          batches = [...batches, batch]
          emit({ type: 'pending', batch })
        }
        next()
      },
      6000 + rng() * 4000,
    )
  next()
  // Batches past their time limit leave the queue unsent, as the daemon's reaper does.
  setInterval(() => {
    const now = Date.now() / 1000
    for (const batch of batches) if (batch.expires !== null && batch.expires <= now) settle(batch.id, 'expired')
  }, 1000)
}
