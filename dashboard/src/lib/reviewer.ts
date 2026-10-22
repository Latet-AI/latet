// The local reviewer in the dashboard: its settings' choices, and what it said of an exchange's blocks.

import type { ExchangeSummary, LiveReviewSettings, LlmReviewSettings, ReviewCategory, ReviewFinding, ReviewKind, ReviewRecord, Slot } from '../api/types'

/** What it may read before a text leaves, in the order the settings list them. */
export const REVIEW_KINDS: readonly ReviewKind[] = [
  'user_text',
  'attachment',
  'system',
  'tool_result',
  'image_text',
  'tool_input',
  'tool_description',
]

export const REVIEW_CATEGORIES: readonly ReviewCategory[] = [
  'person',
  'address',
  'phone',
  'email',
  'id',
  'health',
  'sensitive',
  'financial',
  'secret',
  'org',
  'confidential',
]

/** The type a category's values get as a term ("Always mask this"), as the daemon masks them. */
export const CATEGORY_TYPE: Record<ReviewCategory, string> = {
  person: 'PERSON',
  address: 'STREET_ADDRESS',
  phone: 'PHONE',
  email: 'EMAIL',
  id: 'ID_NUMBER',
  health: 'HEALTH',
  sensitive: 'SENSITIVE',
  financial: 'FINANCIAL',
  secret: 'SECRET',
  org: 'ORG',
  confidential: 'CONFIDENTIAL',
}

/** The most characters of guidance: the global one, and a project's (config.py `MAX_GUIDANCE`). */
export const MAX_GUIDANCE = 2000

/** Guidance as the daemon keeps it: plain line ends, trimmed; empty is none (config.py `clean_guidance`). */
export function cleanGuidance(text: string | null | undefined): string | null {
  const clean = (text ?? '').replace(/\r\n?/g, '\n').trim()
  return clean === '' ? null : clean
}

/** What the reviewer reads in a project: your guidance, then the project's (config.py `join_guidance`). */
export function joinGuidance(...parts: (string | null | undefined)[]): string | null {
  const kept = parts.map(cleanGuidance).filter((part): part is string => part !== null)
  return kept.length > 0 ? kept.join('\n\n') : null
}

/** The settings as the daemon starts with them (config.py `LlmReview`). */
export const DEFAULT_REVIEW: LlmReviewSettings = {
  images: false,
  enabled: false,
  url: 'http://127.0.0.1:8080/v1',
  remote: false,
  model: null,
  api_key_env: 'LATET_LLM_API_KEY',
  api_key_file: null,
  inline: ['user_text', 'attachment', 'system', 'tool_result'],
  budget_ms: null,
  on_failure: 'stop',
  background: true,
  slots: null,
  block: false,
  auto_mask: ['person', 'address', 'email', 'phone', 'secret', 'health', 'financial'],
  chunk_chars: 7000,
  certificate: null,
  guidance: null,
}

/** The Live Reviewer's settings as the daemon starts with them (config.py `LiveReview`). */
export const DEFAULT_LIVE: LiveReviewSettings = {
  enabled: false,
  kinds: ['user_text', 'attachment', 'system', 'tool_result'],
  auto_approve_clean: false,
  timeout_s: null,
}

/** A finding as shown: with the text it covers in the block (the surrogate of a masked value). */
export interface ShownFinding extends ReviewFinding {
  text: string | null
}

export interface ShownReview {
  review: ReviewRecord
  /** The exchange's block it is about, when the exchange carries it. */
  slot: Slot | null
  findings: ShownFinding[]
}

/**
 * The reviews of an exchange's blocks, newest first, each finding with its text from the block. A
 * block reviewed more than once (again after a restart) shows its newest review.
 */
export function reviewsOf(slots: readonly Slot[], reviews: readonly ReviewRecord[]): ShownReview[] {
  const byBlock = new Map<string, Slot>()
  for (const slot of slots) if (slot.block) byBlock.set(slot.block, slot)
  const newest = new Map<string, ReviewRecord>()
  for (const r of reviews) {
    const seen = newest.get(r.block)
    if (byBlock.has(r.block) && (!seen || r.ts > seen.ts)) newest.set(r.block, r)
  }
  return [...newest.values()]
    .sort((a, b) => b.ts - a.ts)
    .map((review) => {
      const slot = byBlock.get(review.block) ?? null
      const findings = review.findings.map((f) => ({
        ...f,
        text: slot && f.at ? [...slot.masked].slice(f.at[0], f.at[1]).join('') : null,
      }))
      return { review, slot, findings }
    })
}

/** How many values the reviewer found in the background, among the records given. */
export function foundAfterSending(reviews: readonly ReviewRecord[]): number {
  return reviews.filter((r) => !r.inline).reduce((n, r) => n + r.findings.length, 0)
}

/**
 * Where the notice of values found after sending leads: the newest exchange listed of the session
 * the newest of them is from (the exchange shows what the reviewer said), or that session's exchanges.
 */
export function lateTarget(
  late: readonly ReviewRecord[],
  exchanges: readonly Pick<ExchangeSummary, 'id' | 'session' | 'ts'>[],
): { href: string; exchange: boolean } | null {
  let newest: ReviewRecord | null = null
  for (const r of late) if (!r.inline && r.findings.length > 0 && (!newest || r.ts > newest.ts)) newest = r
  if (!newest) return null
  let found: Pick<ExchangeSummary, 'id' | 'session' | 'ts'> | null = null
  for (const x of exchanges) if (x.session === newest.session && (!found || x.ts > found.ts)) found = x
  return found
    ? { href: `/exchanges/${encodeURIComponent(found.id)}`, exchange: true }
    : { href: `/live?session=${encodeURIComponent(newest.session)}`, exchange: false }
}

const LOCAL_SUFFIXES = ['.local', '.lan', '.home.arpa', '.internal']

function privateIp(host: string): boolean {
  const v4 = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(host)
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])]
    return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254)
  }
  const v6 = host.replace(/^\[|\]$/g, '').toLowerCase()
  return v6.includes(':') && (v6.startsWith('fc') || v6.startsWith('fd') || v6.startsWith('fe80'))
}

/**
 * Where the reviewer's server is, as the daemon decides it (config.py): this machine, your local
 * network, or the internet; null for an address that is not one.
 */
export function serverPlace(url: string): 'here' | 'network' | 'internet' | null {
  let host: string
  try {
    host = new URL(url).hostname.toLowerCase()
  } catch {
    return null
  }
  if (['127.0.0.1', 'localhost', '[::1]', '::1', 'host.docker.internal'].includes(host)) return 'here'
  if (privateIp(host) || !host.includes('.') || LOCAL_SUFFIXES.some((s) => host.endsWith(s))) return 'network'
  return 'internet'
}

/** A server on your network reached over http: the original texts cross it unencrypted. */
export function inClear(url: string): boolean {
  return serverPlace(url) === 'network' && url.trim().toLowerCase().startsWith('http:')
}

/** A server whose own certificate can be trusted: https, on another machine. */
export function canTrust(url: string): boolean {
  const place = serverPlace(url)
  return (place === 'network' || place === 'internet') && url.trim().toLowerCase().startsWith('https:')
}

/** sha256:<hex> as AB:CD:..., the way servers and openssl print a fingerprint. */
export function readableFingerprint(fingerprint: string): string {
  return (fingerprint.replace(/^sha256:/, '').toUpperCase().match(/../g) ?? []).join(':')
}

/** A server on the internet must be reached over https: it is sent the original texts. */
export function needsHttps(url: string): boolean {
  return serverPlace(url) === 'internet' && url.trim().toLowerCase().startsWith('http:')
}
