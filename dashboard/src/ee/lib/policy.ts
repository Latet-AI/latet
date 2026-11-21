// The company's policy as the dashboard reads it (Latet Enterprise, locked rules): what it locks, all at once, and the
// lines each section of Rules shows under its lock. A person is told what is locked, never a value of the company's: its
// terms, allowed values and patterns, files, web addresses and tools come as counts, its recognizers and field rules by
// name.

import type { TFunction } from 'i18next'
import { ApiError } from '../../api/client'
import type { Profile } from '../../api/types'
import { tk } from '../../i18n'
import { POLICY_LABEL, PROFILE_LABEL } from '../../lib/labels'

export type PolicyImages = 'allow' | 'mask' | 'block'
export type PolicyValue = 'allow' | 'mask' | 'redact'

/** A policy as a person is told it: a rules file naming only what it sets, with no value of the company's in it. */
export interface PolicyRules {
  latet_rules: number
  profile?: Profile
  pii?: string[]
  id_regions?: string[]
  phone_regions?: string[]
  include_private_ips?: boolean
  ner?: 'auto' | 'off'
  /** How many terms it keeps masked. */
  terms?: number
  /** Their names. */
  recognizers?: string[]
  policies?: Record<string, PolicyValue>
  /** The names of its field rules that mask, and how many keep a field in clear (its exceptions). */
  fields?: string[]
  fields_clear?: number
  data?: 'normal' | 'strict'
  dump_guard?: number
  images?: PolicyImages
  ocr?: 'auto' | 'off'
  /** How a request member no request profile names yet is read: `refuse` locks it refused for everyone (as Latet did
   * before, a member of text alone still passing on a request Latet only checks); `check` locks nothing. */
  unknown_members?: 'check' | 'refuse'
  /** How many values and patterns it never masks. */
  allow_values?: number
  allow_patterns?: number
  disabled_secret_rules?: string[]
  /** How many tool patterns strict data mode covers (a pattern can name a client: an MCP server's name). */
  data_tools?: number
  /** How many file rules and web address rules strict data mode covers. */
  data_files?: number
  data_hosts?: number
  /** Whether it gives the local reviewer guidance. */
  guidance?: boolean
}

export interface PolicyFloor {
  source: 'managed' | 'server'
  /** Its file, or the server's address. */
  origin: string
  /** When it was written (a file) or set (on a server), in seconds. */
  received: number
  exceptions: 'personal' | 'company'
  rules: PolicyRules
  /** The keys its profile locks without the file naming them (a rules file's key names). */
  brought?: string[]
  /** How many settings in it were not read: their words stay with the administrators. */
  notices: number
}

/** GET /ee/policy: the floor in force (none or one), and where your own settings are below it. */
export interface PolicyView {
  floors: PolicyFloor[]
  /** Your settings below it: raised while in use. */
  below: string[]
  problems: string[]
}

const PROFILE_RANK: Record<Profile, number> = { secrets: 0, balanced: 1, max: 2 }
const IMAGES_RANK: Record<PolicyImages, number> = { allow: 0, mask: 1, block: 2 }
const POLICY_RANK: Record<PolicyValue, number> = { allow: 0, mask: 1, redact: 2 }

/** What the company's policy locks (one floor at most is in force; the strictest of each, should there be more). */
export interface Locks {
  profile: Profile | null
  pii: string[]
  regions: string[]
  phones: string[]
  privateIps: boolean
  ner: boolean
  ocr: boolean
  /** How many of the company's terms stay masked. */
  terms: number
  recognizers: string[]
  fields: string[]
  /** How many field rules of the company keep a field in clear. */
  fieldsClear: number
  policies: Record<string, PolicyValue>
  strictData: boolean
  dumpGuard: number | null
  images: PolicyImages | null
  /** New request members refused for everyone (`unknown_members: refuse`): `check` is not offered. */
  unknownMembers: boolean
  guidance: boolean
  companyExceptions: boolean
  /** How many values and patterns the company never masks. */
  exceptions: number
  /** How many file rules and web address rules strict data mode covers. */
  dataFiles: number
  dataHosts: number
}

export type LockedSection = 'profile' | 'pii' | 'ner' | 'phones' | 'privateIps' | 'terms' | 'recognizers' | 'allow' | 'policies' | 'reviewer' | 'data'

/** A line under a section with a lock: an i18n key and its values. */
export interface LockLine {
  key: string
  vars?: Record<string, string | number>
}

const union = (a: string[], b: string[]) => [...new Set([...a, ...b])]

/** The images rule a policy locks, as Latet reads it: the setting, else block with max; null when it locks none. */
export function imagesOf(rules: Pick<PolicyRules, 'images' | 'profile'>): PolicyImages | null {
  if (rules.images === 'block' || rules.images === 'mask') return rules.images
  return rules.profile === 'max' ? 'block' : null
}

export function locksOf(view: PolicyView | null | undefined): Locks | null {
  if (!view || view.floors.length === 0) return null
  const locks: Locks = {
    profile: null, pii: [], regions: [], phones: [], privateIps: false, ner: false, ocr: false, terms: 0, recognizers: [],
    fields: [], fieldsClear: 0, policies: {}, strictData: false, dumpGuard: null, images: null, unknownMembers: false, guidance: false,
    companyExceptions: false, exceptions: 0, dataFiles: 0, dataHosts: 0,
  }
  for (const floor of view.floors) {
    const r = floor.rules
    if (r.profile && (locks.profile === null || PROFILE_RANK[r.profile] > PROFILE_RANK[locks.profile])) locks.profile = r.profile
    locks.pii = union(locks.pii, r.pii ?? [])
    locks.regions = union(locks.regions, r.id_regions ?? [])
    locks.phones = union(locks.phones, r.phone_regions ?? [])
    const brought = floor.brought ?? []
    locks.privateIps ||= (r.include_private_ips ?? r.profile === 'max') || brought.includes('include_private_ips')
    locks.ner ||= r.ner === 'auto'
    locks.ocr ||= r.ocr === 'auto'
    locks.terms += r.terms ?? 0
    locks.recognizers = union(locks.recognizers, r.recognizers ?? [])
    locks.fields = union(locks.fields, r.fields ?? [])
    locks.fieldsClear += r.fields_clear ?? 0
    for (const [type, value] of Object.entries(r.policies ?? {})) {
      const current = locks.policies[type]
      if (current === undefined || POLICY_RANK[value] > POLICY_RANK[current]) locks.policies[type] = value
    }
    locks.strictData ||= r.data === 'strict' || (r.profile === 'max' && r.data !== 'normal') || brought.includes('data')
    if (r.dump_guard) locks.dumpGuard = locks.dumpGuard === null ? r.dump_guard : Math.min(locks.dumpGuard, r.dump_guard)
    const images = imagesOf(r)
    if (images && (locks.images === null || IMAGES_RANK[images] > IMAGES_RANK[locks.images])) locks.images = images
    locks.unknownMembers ||= r.unknown_members === 'refuse' // `check` locks nothing: a person may always refuse
    locks.guidance ||= Boolean(r.guidance)
    locks.companyExceptions ||= floor.exceptions === 'company'
    locks.exceptions += (r.allow_values ?? 0) + (r.allow_patterns ?? 0)
    locks.dataFiles += r.data_files ?? 0
    locks.dataHosts += r.data_hosts ?? 0
  }
  return locks
}

/** A profile the policy allows: at its profile or above, and never secrets (no personal data) when it locks some. */
export function profileAllowed(locks: Locks | null, profile: Profile): boolean {
  if (!locks) return true
  if (profile === 'secrets' && (locks.pii.length > 0 || locks.regions.length > 0)) return false
  return !locks.profile || PROFILE_RANK[profile] >= PROFILE_RANK[locks.profile]
}

/** A project's protection the policy allows: Latet stays on, and secrets only where the profile allows it. */
export function protectionAllowed(locks: Locks | null, protection: 'on' | 'secrets' | 'off'): boolean {
  if (!locks) return true
  if (protection === 'off') return false
  return protection === 'on' || profileAllowed(locks, 'secrets')
}

export function lockLines(locks: Locks): Partial<Record<LockedSection, LockLine[]>> {
  const out: Partial<Record<LockedSection, LockLine[]>> = {}
  const add = (section: LockedSection, line: LockLine) => {
    out[section] = [...(out[section] ?? []), line]
  }
  if (locks.profile) add('profile', { key: tk('ee.policy.lock.profile'), vars: { profile: locks.profile } })
  if (locks.pii.length) add('pii', { key: tk('ee.policy.lock.pii'), vars: { types: locks.pii.join(', ') } })
  if (locks.regions.length) add('pii', { key: tk('ee.policy.lock.regions'), vars: { regions: locks.regions.join(', ') } })
  if (locks.ner) add('ner', { key: tk('ee.policy.lock.ner') })
  if (locks.phones.length) add('phones', { key: tk('ee.policy.lock.phones'), vars: { regions: locks.phones.join(', ') } })
  if (locks.privateIps) add('privateIps', { key: tk('ee.policy.lock.privateIps') })
  if (locks.terms > 0) add('terms', { key: tk('ee.policy.lock.terms'), vars: { count: locks.terms } })
  if (locks.recognizers.length) add('recognizers', { key: tk('ee.policy.lock.recognizers'), vars: { names: locks.recognizers.join(', ') } })
  if (locks.companyExceptions) {
    add('allow', { key: tk('ee.policy.lock.company') })
    if (locks.exceptions > 0) add('allow', { key: tk('ee.policy.lock.exceptions'), vars: { count: locks.exceptions } })
  } else if (locks.terms > 0) {
    add('allow', { key: tk('ee.policy.lock.termsStay') })
  }
  const policies = Object.entries(locks.policies).map(([type, value]) => `${type} ${value}`)
  if (policies.length) add('policies', { key: tk('ee.policy.lock.policies'), vars: { policies: policies.join(', ') } })
  if (locks.guidance) add('reviewer', { key: tk('ee.policy.lock.guidance') })
  if (locks.strictData) add('data', { key: tk('ee.policy.lock.data') })
  if (locks.dataFiles > 0 || locks.dataHosts > 0) {
    add('data', { key: tk('ee.policy.lock.dataSources'), vars: { files: locks.dataFiles, hosts: locks.dataHosts } })
  }
  if (locks.fields.length) add('data', { key: tk('ee.policy.lock.fields'), vars: { names: locks.fields.join(', ') } })
  if (locks.fieldsClear > 0) add('data', { key: tk('ee.policy.lock.fieldsClear'), vars: { count: locks.fieldsClear } })
  if (locks.dumpGuard !== null) add('data', { key: tk('ee.policy.lock.dumpGuard'), vars: { limit: locks.dumpGuard } })
  if (locks.images) add('data', { key: tk('ee.policy.lock.images'), vars: { images: locks.images } })
  if (locks.ocr) add('data', { key: tk('ee.policy.lock.ocr') })
  if (locks.unknownMembers) add('data', { key: tk('ee.policy.lock.unknownMembers') })
  return out
}

const IMAGES_LABEL: Record<PolicyImages, string> = {
  allow: tk('data.images.allow'),
  mask: tk('data.images.mask'),
  block: tk('data.images.block'),
}

/** A line in the reader's language: its profile, its images rule and its policies by the names Rules gives them. */
export function lineText(t: TFunction, line: LockLine): string {
  const vars: Record<string, string | number> = { ...line.vars }
  if (typeof vars.profile === 'string' && vars.profile in PROFILE_LABEL) vars.profile = t(PROFILE_LABEL[vars.profile as Profile])
  if (typeof vars.images === 'string' && vars.images in IMAGES_LABEL) vars.images = t(IMAGES_LABEL[vars.images as PolicyImages])
  if (typeof vars.policies === 'string') {
    vars.policies = vars.policies
      .split(', ')
      .map((entry) => {
        const [type, value] = entry.split(' ')
        return value && value in POLICY_LABEL ? `${type} (${t(POLICY_LABEL[value as PolicyValue])})` : entry
      })
      .join(', ')
  }
  return t(line.key, vars) as string
}

/** The settings a save refused by the company's policy names (409 with `floor`), or null. */
export function floorRefusal(error: unknown): string[] | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const reasons = (error.body as { floor?: unknown } | undefined)?.floor
  return Array.isArray(reasons) ? reasons.map(String) : null
}

/** GET /ee/policy/text (a server, for policy.edit): the stored policy with its text, as `latet server policy show`
 * sees it; `policy` null when none is stored, or when the server cannot read the one stored (its problem in
 * `problems`). */
export interface PolicyText {
  /** The stored policy's etag, readable or not (null: none stored): what a save sends back. */
  etag: string | null
  policy: {
    etag: string
    set_at: number
    /** A person's id, or admin:<token id> for `latet server policy set`. */
    set_by: string
    keys: string[]
    /** The keys its profile locks without the file naming them (a rules file's key names). */
    brought: string[]
    exceptions: 'personal' | 'company' | null
    notices: string[]
    in_force: boolean
    text: string
  } | null
  problems: string[]
}

/** PUT /ee/policy: what the policy saved sets. */
export interface PolicySaved {
  etag: string
  keys: string[]
  exceptions: 'personal' | 'company'
  notices: string[]
  /** E2's words: the other instances not told now, or instances that do not apply the policy (R27). */
  problems: string[]
}

/** Whether the person sets the policy on the Policy page: on a server, the security role (policy.edit). */
export function editsPolicy(edition: { actions?: string[] } | null | undefined): boolean {
  return (edition?.actions ?? []).includes('policy.edit')
}

/** The text the editor starts from: the stored policy's, or none. */
export function storedText(view: PolicyText | null | undefined): string {
  return view?.policy?.text ?? ''
}

/** Whether Save has something to save: a text, other than the stored one. */
export function canSave(draft: string, view: PolicyText | null | undefined): boolean {
  return draft.trim() !== '' && draft !== storedText(view)
}

/** Whether a policy is stored that Remove takes away: one the server reads, or one it cannot read (shown with its
 * problem and no text). */
export function removable(view: PolicyText | null | undefined): boolean {
  return Boolean(view && (view.policy !== null || view.etag !== null))
}

/** The server's reason for not taking a policy ("not changed: …", or too large), shown under the editor; null for any
 * other failure, which a toast says. */
export function policyRefusal(error: unknown): string | null {
  return error instanceof ApiError && (error.status === 400 || error.status === 413) ? error.message : null
}

/** Whether a save was refused because the policy was changed since its text was read (409 `policy-changed`): someone
 * else's change, from the page or from `latet server policy set`, which the editor shows again. */
export function policyChanged(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false
  const body = error.body as { code?: unknown } | null | undefined
  return body?.code === 'policy-changed'
}

/** What a change of the policy from the editor came to: made (with the problems the server found beside it, in its
 * words), refused with a reason shown under the text, changed meanwhile by someone else (the editor shows the policy as it
 * is now), or failed for any other reason (a toast says it). */
export type PolicyChange =
  | { kind: 'done'; problems: string[] }
  | { kind: 'refused'; reason: string }
  | { kind: 'changed' }
  | { kind: 'failed'; error: unknown }

/** Runs a save or a removal of the policy and says what it came to. Both name the etag of the policy they were made
 * from, so both are refused as `changed` when it is no longer the one stored. */
export async function changePolicy(action: () => Promise<unknown>): Promise<PolicyChange> {
  try {
    const answer = (await action()) as Partial<PolicySaved> | null
    return { kind: 'done', problems: answer?.problems ?? [] }
  } catch (error) {
    const reason = policyRefusal(error)
    if (reason !== null) return { kind: 'refused', reason }
    if (policyChanged(error)) return { kind: 'changed' }
    return { kind: 'failed', error }
  }
}
