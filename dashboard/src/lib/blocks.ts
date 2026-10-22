// Why a request was stopped, in the terms the exchange page explains it: what happened, what it
// meant for you, and what you can do.

import type { ExchangeDetail, Leak, ReviewDetail, Slot } from '../api/types'
import { isBlocked, parseFlags } from './flags'

export type BlockKind =
  | 'review' // Claude Code's auto mode reviewer would have received identity details or a secret
  | 'history' // a conversation that held values in clear before Latet masked it
  | 'plugin' // the Claude Code plugin's final check found a value in clear
  | 'uninspected' // an image or document the max profile does not send
  | 'imagesSetting' // an image or document that images: block does not send
  | 'imagesMask' // an image Latet could not read and mask, which images: mask does not send
  | 'live' // the Live Reviewer: you stopped it, or it waited too long
  | 'localReviewer' // the local reviewer could not read it, or stopped it
  | 'leak' // a value in clear, through the gateway
  | 'outbound' // a tool that runs outside this computer would have got a stand-in
  | 'other' // Latet could not handle it (an unreadable body, masking failed)

export type LiveEnd = 'rejected' | 'expired' | 'abandoned'

export interface BlockExplanation {
  kind: BlockKind
  /** Auto mode review: the identity details it carried, each once, in order. */
  details: ReviewDetail[]
  /** Auto mode review: the types of the other values it carried (secrets, personal data), never let through. */
  others: string[]
  /** What was not sent because Latet cannot inspect it (image, document...). */
  uninspected: string[]
  /** The Live Reviewer: how the wait ended. */
  live: LiveEnd | null
  /** The reason the agent was given, as Latet wrote it. */
  given: string | null
  /** The values that stopped the request are marked in its blocks: the page can point at them. */
  marked: boolean
}

const DETAIL_ORDER: ReviewDetail[] = ['user_name', 'paths', 'host_name', 'git_author']

function liveEnd(reason: string): LiveEnd | null {
  if (reason.includes('did not approve it in the Live Reviewer')) return 'rejected'
  if (reason.includes('live_review.timeout_s')) return 'expired'
  if (reason.includes('the agent stopped waiting')) return 'abandoned'
  return null
}

/** Why the request of this exchange was stopped; null when it was not. */
export function explainBlock(detail: ExchangeDetail): BlockExplanation | null {
  const { summary, leaks } = detail
  if (!isBlocked(summary.flags)) return null
  const given = detail.block_reason ?? summary.block_reason ?? null
  const reason = given ?? ''
  const uninspected = parseFlags(summary.flags)
    .filter((f) => f.key === 'uninspected')
    .map((f) => f.detail ?? f.raw)
  const found = new Set(leaks.flatMap((leak) => (leak.detail ? [leak.detail] : [])))
  const shown = new Set(detail.request.slots.map((slot) => slot.block))
  const base = {
    details: DETAIL_ORDER.filter((d) => found.has(d)),
    others: [...new Set(leaks.filter((leak) => !leak.detail).map((leak) => leak.type))],
    uninspected,
    live: null,
    given,
    marked: stoppedValues(leaks).some((value) => shown.has(value.block)),
  }
  const plugin = summary.transport === 'plugin'
  let kind: BlockKind
  if (outboundTools(summary.flags).length > 0) kind = 'outbound'
  else if (leaks.length === 0 && uninspected.length > 0)
    kind = reason.includes('images: block') ? 'imagesSetting' : reason.includes('images: mask') ? 'imagesMask' : 'uninspected'
  else if (summary.flags.includes('auto-mode-review')) kind = 'review'
  else if (liveEnd(reason)) kind = 'live'
  else if (reason.includes('the local reviewer')) kind = 'localReviewer'
  else if (plugin && summary.flags.includes('history')) kind = 'history'
  else if (plugin) kind = 'plugin'
  else if (leaks.length > 0) kind = 'leak'
  else kind = 'other'
  return { ...base, kind, live: kind === 'live' ? liveEnd(reason) : null }
}

/** A value that stopped the request, with its place in the block it is in (code points, on both sides). */
export type StoppedValue = Leak & Required<Pick<Leak, 'block' | 'masked_start' | 'masked_end' | 'start' | 'end'>>

/**
 * The values to mark in the request of a stopped exchange: those that stopped it (new, and not let
 * through) and that the daemon placed in a block. Not an identity detail of auto mode's reviewer, which
 * has its choices of its own, nor a value that was only flagged because it left before. A block sent
 * twice reports its value twice: it is told once.
 */
export function stoppedValues(leaks: readonly Leak[]): StoppedValue[] {
  const seen = new Set<string>()
  const out: StoppedValue[] = []
  for (const leak of leaks) {
    const { block, masked_start, masked_end, start, end } = leak
    if (!leak.new || leak.allowed || leak.detail) continue
    if (block === undefined || masked_start === undefined || masked_end === undefined || start === undefined || end === undefined) continue
    const key = `${block}:${masked_start}:${masked_end}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push({ ...leak, block, masked_start, masked_end, start, end })
  }
  return out
}

/** The tools outside this computer that got (or would have got) stand-ins, from the flags. */
export function outboundTools(flags: readonly string[]): string[] {
  return parseFlags(flags)
    .filter((f) => f.key === 'outbound')
    .map((f) => f.detail ?? f.raw)
}

export interface OutboundValue {
  /** The real value, as this computer knows it. */
  value: string
  /** The stand-in the tool got instead. */
  sent: string
  type: string
}

/** The real values behind the stand-ins a tool outside this computer got, each once. */
export function outboundValues(slots: readonly Slot[]): OutboundValue[] {
  const out = new Map<string, OutboundValue>()
  for (const slot of slots) {
    if (slot.kind !== 'outbound_tool' || slot.original === null) continue
    const original = [...slot.original]
    const masked = [...slot.masked]
    for (const r of slot.replacements) {
      if (!r.known) continue
      const value = original.slice(r.start, r.end).join('')
      if (value && !out.has(value)) out.set(value, { value, sent: masked.slice(r.masked_start, r.masked_end).join(''), type: r.type })
    }
  }
  return [...out.values()]
}

/** The element id of the block at `index` of a request, which a page can scroll to or point at. */
export const blockDomId = (index: number) => `block-${index}`
/** The element ids of the response's text and of its tool call at `index`. */
export const RESPONSE_TEXT_ID = 'response-text'
export const responseToolId = (index: number) => `response-tool-${index}`
