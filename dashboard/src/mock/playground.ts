// A tiny stand-in for the engine, so the Playground works in mock mode. The real masking happens in
// the daemon; these patterns only cover the common cases well enough to try the page.

import type { EntityClass, PlaygroundResult, Replacement } from '../api/types'
import { moduleOf } from './builder'
import { hashSeed, seeded, shape } from './rng'

interface Finding {
  start: number
  end: number
  type: string
  cls: EntityClass
  rule: string
  redact?: boolean
}

const DETECTORS: { pattern: RegExp; type: string; cls: EntityClass; rule: string; group?: number; redact?: boolean }[] = [
  { pattern: /-----BEGIN [A-Z ]*PRIVATE KEY-----([\s\S]+?)-----END [A-Z ]*PRIVATE KEY-----/g, type: 'PRIVATE_KEY', cls: 'SECRET', rule: 'private-key:pem', group: 1, redact: true },
  { pattern: /\bsk-ant-[A-Za-z0-9_-]{20,}/g, type: 'ANTHROPIC_API_KEY', cls: 'SECRET', rule: 'gitleaks:anthropic-api-key' },
  { pattern: /\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}/g, type: 'OPENAI_API_KEY', cls: 'SECRET', rule: 'gitleaks:openai-api-key' },
  { pattern: /\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}/g, type: 'STRIPE_ACCESS_TOKEN', cls: 'SECRET', rule: 'gitleaks:stripe-access-token' },
  { pattern: /\bgh[pousr]_[A-Za-z0-9]{30,}/g, type: 'GITHUB_PAT', cls: 'SECRET', rule: 'gitleaks:github-pat' },
  { pattern: /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, type: 'AWS_ACCESS_TOKEN', cls: 'SECRET', rule: 'gitleaks:aws-access-token' },
  { pattern: /\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{10,}/g, type: 'JWT', cls: 'SECRET', rule: 'credentials:jwt' },
  { pattern: /[a-z][a-z0-9+.-]{1,30}:\/\/[^\s:/?#@]{1,128}:([^\s/?#@]{1,256})@/gi, type: 'URL_PASSWORD', cls: 'SECRET', rule: 'credentials:url', group: 1 },
  { pattern: /^[ \t]*(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*(?:PASSWORD|PASSWD|PWD)[A-Za-z0-9_]*[ \t]*[=:][ \t]*["']?([^\s"']{3,})/gim, type: 'PASSWORD', cls: 'SECRET', rule: 'assignment', group: 1 },
  { pattern: /^[ \t]*(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*(?:SECRET|TOKEN|API_?KEY)[A-Za-z0-9_]*[ \t]*[=:][ \t]*["']?([^\s"']{6,})/gim, type: 'ASSIGNED_SECRET', cls: 'SECRET', rule: 'assignment', group: 1 },
  { pattern: /\b[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b/g, type: 'EMAIL', cls: 'PII', rule: 'pii:email' },
  { pattern: /\bGB\d{2}[A-Z]{4}\d{14}\b/g, type: 'IBAN', cls: 'PII', rule: 'pii:iban' },
  { pattern: /\b[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b/g, type: 'GB_NINO', cls: 'PII', rule: 'pii:gb-nino' },
  { pattern: /(?:\+|\b00)44[ ]?\d{2,4}[ ]?\d{3,4}[ ]?\d{3,4}\b/g, type: 'PHONE', cls: 'PII', rule: 'pii:phone' },
  { pattern: /\bGB ?\d{3} ?\d{4} ?\d{2}\b/g, type: 'VAT_NUMBER', cls: 'PII', rule: 'pii:vat-gb' },
  { pattern: /[\\/](?:Users|home)[\\/](dhayes)\b/gi, type: 'USERNAME', cls: 'IDENTITY', rule: 'identity:home-path', group: 1 },
  { pattern: /\bDaniel Hayes\b/g, type: 'PERSON', cls: 'IDENTITY', rule: 'identity:person' },
  { pattern: /\bDH-THINKPAD\b/gi, type: 'HOSTNAME', cls: 'IDENTITY', rule: 'identity:hostname' },
]

const RESERVED = /@example\.(?:com|net|org)$/i

/** Characters of the original a surrogate keeps (public prefixes, country codes). */
const KEEP: Record<string, number> = {
  STRIPE_ACCESS_TOKEN: 8,
  GITHUB_PAT: 4,
  AWS_ACCESS_TOKEN: 4,
  ANTHROPIC_API_KEY: 13,
  OPENAI_API_KEY: 8,
  IBAN: 2,
  PHONE: 3,
  JWT: 3,
}

export function mockMask(
  text: string,
  terms: { value: string; type: string }[],
  allow: string[],
  secretsOnly = false,
): PlaygroundResult {
  const findings: Finding[] = []
  const taken = (start: number, end: number) => findings.some((f) => start < f.end && end > f.start)
  const detectors = [
    ...terms.map((term) => ({
      pattern: new RegExp(`\\b${term.value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'gi'),
      type: term.type,
      cls: 'CUSTOM' as const,
      rule: `terms:${term.type.toLowerCase()}`,
      group: undefined,
      redact: false,
    })),
    // Secrets only: secrets and your terms, as the daemon's profile.
    ...DETECTORS.filter((detector) => !secretsOnly || detector.cls === 'SECRET'),
  ]
  for (const detector of detectors) {
    for (const match of text.matchAll(detector.pattern)) {
      const value = detector.group ? match[detector.group] : match[0]
      if (!value) continue
      const offset = detector.group ? match[0].indexOf(value) : 0
      const start = (match.index ?? 0) + offset
      const end = start + value.length
      if (allow.includes(value) || (detector.type === 'EMAIL' && RESERVED.test(value))) continue
      if (taken(start, end)) continue
      findings.push({ start, end, type: detector.type, cls: detector.cls, rule: detector.rule, redact: detector.redact })
    }
  }
  findings.sort((a, b) => a.start - b.start)

  // Offsets in code points, like the daemon.
  const cp = (utf16: number) => [...text.slice(0, utf16)].length
  let masked = ''
  let pos = 0
  let delta = 0
  const replacements: Replacement[] = []
  for (const f of findings) {
    masked += text.slice(pos, f.start)
    const value = text.slice(f.start, f.end)
    const sent = f.redact ? `[REDACTED:${f.type}]` : shape(seeded(hashSeed(value)), value, KEEP[f.type] ?? 0)
    const start = cp(f.start)
    const end = cp(f.end)
    replacements.push({
      start,
      end,
      masked_start: start + delta,
      masked_end: start + delta + [...sent].length,
      type: f.type,
      class: f.cls,
      rule: f.rule,
      module: moduleOf(f.rule, f.type),
      via: 'detector',
      entity: f.redact ? null : `pg${hashSeed(value).toString(16).padStart(8, '0')}${'0'.repeat(10)}`,
      known: !f.redact,
    })
    delta += [...sent].length - (end - start)
    masked += sent
    pos = f.end
  }
  masked += text.slice(pos)
  return { masked, replacements }
}
