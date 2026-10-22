// Translation keys for values the API returns, so pages can look them up by value.

import type { Agent, CheckStatus, EntityClass, Mode, Module, Policy, Profile, ReviewDetail, SlotKind, Via } from '../api/types'
import { tk } from '../i18n'
import type { FlagKey, ParsedFlag } from './flags'

export const CLASS_ORDER: readonly EntityClass[] = ['SECRET', 'PII', 'IDENTITY', 'CUSTOM', 'LLM', 'NER', 'STRUCT']

/** A class as shown: the API's classes, plus UNKNOWN when the daemon lost track of a value ("?"). */
export type ViewClass = EntityClass | 'UNKNOWN'

export const CLASS_LABEL: Record<ViewClass, string> = {
  SECRET: tk('classes.SECRET.label'),
  PII: tk('classes.PII.label'),
  IDENTITY: tk('classes.IDENTITY.label'),
  CUSTOM: tk('classes.CUSTOM.label'),
  NER: tk('classes.NER.label'),
  LLM: tk('classes.LLM.label'),
  STRUCT: tk('classes.STRUCT.label'),
  UNKNOWN: tk('classes.UNKNOWN.label'),
}

export const CLASS_HELP: Record<ViewClass, string> = {
  SECRET: tk('classes.SECRET.help'),
  PII: tk('classes.PII.help'),
  IDENTITY: tk('classes.IDENTITY.help'),
  CUSTOM: tk('classes.CUSTOM.help'),
  NER: tk('classes.NER.help'),
  LLM: tk('classes.LLM.help'),
  STRUCT: tk('classes.STRUCT.help'),
  UNKNOWN: tk('classes.UNKNOWN.help'),
}

/**
 * CSS suffix of a class's color tokens (the name model's and the local reviewer's findings share the
 * personal-data hue, drawn dashed; the values of a structure take the ink of data, outside the set of
 * hues).
 */
export const CLASS_TONE: Record<ViewClass, string> = {
  SECRET: 'secret',
  PII: 'pii',
  IDENTITY: 'identity',
  CUSTOM: 'custom',
  NER: 'pii',
  LLM: 'pii',
  STRUCT: 'data',
  UNKNOWN: 'neutral',
}

const VIEW_CLASSES = new Set<string>([...CLASS_ORDER, 'UNKNOWN'])

/** Found by a model (the name model, the local reviewer): a judgement, drawn with a dashed line. */
export function isJudgement(cls: ViewClass | null | undefined): boolean {
  return cls === 'NER' || cls === 'LLM'
}

/** The tone of a class, or the neutral tone when there is none ("Other"). */
export function toneOf(cls: ViewClass | null | undefined): string {
  return cls ? CLASS_TONE[cls] : 'neutral'
}

/** The class of a value as the API wrote it ("?" or anything unexpected becomes UNKNOWN). */
export function viewClass(value: string): ViewClass {
  return VIEW_CLASSES.has(value) ? (value as ViewClass) : 'UNKNOWN'
}

export const KIND_LABEL: Record<SlotKind, string> = {
  system: tk('kinds.system'),
  user_text: tk('kinds.user_text'),
  assistant_text: tk('kinds.assistant_text'),
  tool_input: tk('kinds.tool_input'),
  tool_result: tk('kinds.tool_result'),
  attachment: tk('kinds.attachment'),
  tool_description: tk('kinds.tool_description'),
  outbound_tool: tk('kinds.outbound_tool'),
  image_text: tk('kinds.image_text'),
  other: tk('kinds.other'),
}

/** The kinds a text can be tried as (a tool outside this computer is not one you type). */
export const SLOT_KINDS = (Object.keys(KIND_LABEL) as SlotKind[]).filter((kind) => kind !== 'outbound_tool')

/** What Claude Code's auto mode reviewer is sent of this machine's identity, as a title. */
export const REVIEW_DETAIL_TITLE: Record<ReviewDetail, string> = {
  user_name: tk('rules.review.details.user_name'),
  paths: tk('rules.review.details.paths'),
  host_name: tk('rules.review.details.host_name'),
  git_author: tk('rules.review.details.git_author'),
}

/** What Claude Code's auto mode reviewer is sent of this machine's identity, as in a sentence. */
export const REVIEW_DETAIL_LABEL: Record<ReviewDetail, string> = {
  user_name: tk('reviewDetails.user_name'),
  paths: tk('reviewDetails.paths'),
  host_name: tk('reviewDetails.host_name'),
  git_author: tk('reviewDetails.git_author'),
}

export const AGENT_LABEL: Record<Agent, string> = {
  'claude-code': tk('agents.claude-code'),
  codex: tk('agents.codex'),
  opencode: tk('agents.opencode'),
  'gemini-cli': tk('agents.gemini-cli'),
  'tutorial-agent': tk('agents.tutorial-agent'),
  other: tk('agents.other'),
}

export const AGENTS = Object.keys(AGENT_LABEL) as Agent[]

/** The agents a filter offers: the tutorial's pretend agent only inside its sandbox, where it is the one that talks. */
export function filterAgents(tutorial: boolean): Agent[] {
  return AGENTS.filter((agent) => agent !== 'tutorial-agent' || tutorial)
}

export const MODE_LABEL: Record<Mode, string> = {
  rewrite: tk('modes.rewrite.label'),
  verify: tk('modes.verify.label'),
  passthrough: tk('modes.passthrough.label'),
}

export const MODES = ['rewrite', 'verify', 'passthrough'] as const satisfies readonly Mode[]

export const MODE_HELP: Record<Mode, string> = {
  rewrite: tk('modes.rewrite.help'),
  verify: tk('modes.verify.help'),
  passthrough: tk('modes.passthrough.help'),
}

export const FLAG_LABEL: Record<FlagKey, string> = {
  blocked: tk('flags.blocked.label'),
  leak: tk('flags.leak.label'),
  error: tk('flags.error.label'),
  uninspected: tk('flags.uninspected.label'),
  degraded: tk('flags.degraded.label'),
  'unknown-route': tk('flags.unknown-route.label'),
  'auto-mode-review': tk('flags.auto-mode-review.label'),
  'project-off': tk('flags.project-off.label'),
  withheld: tk('flags.withheld.label'),
  history: tk('flags.history.label'),
  outbound: tk('flags.outbound.label'),
  'route-bypass': tk('flags.route-bypass.label'),
  image: tk('flags.image.label'),
  other: tk('flags.other.label'),
}

export const FLAG_HELP: Record<FlagKey, string> = {
  blocked: tk('flags.blocked.help'),
  leak: tk('flags.leak.help'),
  error: tk('flags.error.help'),
  uninspected: tk('flags.uninspected.help'),
  degraded: tk('flags.degraded.help'),
  'unknown-route': tk('flags.unknown-route.help'),
  'auto-mode-review': tk('flags.auto-mode-review.help'),
  'project-off': tk('flags.project-off.help'),
  withheld: tk('flags.withheld.help'),
  history: tk('flags.history.help'),
  outbound: tk('flags.outbound.help'),
  'route-bypass': tk('flags.route-bypass.help'),
  image: tk('flags.image.help'),
  other: tk('flags.other.help'),
}

// A flag's detail in the reader's words: what a request carried that Latet could not read (`uninspected:image`),
// and what became of an image (`image:withheld`).
const FLAG_DETAIL: Partial<Record<FlagKey, ReadonlyMap<string, string>>> = {
  uninspected: new Map([
    ['image', tk('flags.details.image')],
    ['document', tk('flags.details.document')],
    ['file', tk('flags.details.file')],
    ['input_file', tk('flags.details.file')],
    ['audio', tk('flags.details.audio')],
    ['input_audio', tk('flags.details.audio')],
  ]),
  image: new Map([
    ['masked', tk('flags.details.masked')],
    ['withheld', tk('flags.details.withheld')],
  ]),
}

/** The key of a flag's detail in the reader's words; null for one that names something (a detector, a tool) or that
 * the dashboard does not know, shown as the daemon wrote it. */
export function flagDetailKey(flag: Pick<ParsedFlag, 'key' | 'detail'>): string | null {
  return (flag.detail !== null && FLAG_DETAIL[flag.key]?.get(flag.detail)) || null
}

/** A flag's detail as the reader reads it: in their words when the dashboard knows it, else as the daemon wrote it. */
export function flagDetail(flag: Pick<ParsedFlag, 'key' | 'detail' | 'raw'>, t: (key: string) => string): string {
  const key = flagDetailKey(flag)
  return key ? t(key) : (flag.detail ?? flag.raw)
}

/** Flags offered in the Live filter (the daemon's flags without their detail). */
export const FILTER_FLAGS: readonly FlagKey[] = [
  'blocked',
  'leak',
  'error',
  'uninspected',
  'degraded',
  'unknown-route',
  'auto-mode-review',
  'project-off',
  'withheld',
  'history',
  'outbound',
  'image',
]

export const PROFILE_LABEL: Record<Profile, string> = {
  balanced: tk('rules.profile.balanced.label'),
  max: tk('rules.profile.max.label'),
  secrets: tk('rules.profile.secrets.label'),
}

export const PROFILE_HELP: Record<Profile, string> = {
  balanced: tk('rules.profile.balanced.help'),
  max: tk('rules.profile.max.help'),
  secrets: tk('rules.profile.secrets.help'),
}

export const PROFILES = Object.keys(PROFILE_LABEL) as Profile[]

export const POLICY_LABEL: Record<Policy, string> = {
  mask: tk('rules.policies.mask'),
  redact: tk('rules.policies.redact'),
  allow: tk('rules.policies.allow'),
}

export const POLICY_HELP: Record<Policy, string> = {
  mask: tk('rules.policies.help.mask'),
  redact: tk('rules.policies.help.redact'),
  allow: tk('rules.policies.help.allow'),
}

export const POLICIES = Object.keys(POLICY_LABEL) as Policy[]

export const CHECK_LABEL: Record<CheckStatus, string> = {
  ok: tk('integrations.status.ok'),
  warn: tk('integrations.status.warn'),
  fail: tk('integrations.status.fail'),
}

/** Personal-data and identity types the engine knows; everything else it masks is a secret type. */
const PII_TYPES = new Set([
  'EMAIL',
  'DOMAIN',
  'PHONE',
  'IBAN',
  'CREDIT_CARD',
  'IT_FISCAL_CODE',
  'IT_VAT_CODE',
  'IP_ADDRESS',
  'CRYPTO_WALLET',
  'CRYPTO', // the name before 0.1
  'VAT_NUMBER',
  'ES_DNI',
  'ES_NIE',
  'FR_NIR',
  'DE_TAX_ID',
  'NL_BSN',
  'BE_NATIONAL_NUMBER',
  'PT_NIF',
  'PL_PESEL',
  'CH_AHV',
  'SE_PERSONNUMMER',
  'FI_HETU',
  'GB_NINO',
  'US_SSN',
  'US_ITIN',
  'CA_SIN',
  'LV_PERSONAL_CODE',
])
const IDENTITY_TYPES = new Set(['USERNAME', 'HOSTNAME', 'PERSON'])
/** Types only the local language model finds (names are PERSON, shared with the identity). */
const NER_TYPES = new Set(['STREET_ADDRESS', 'CITY', 'ZIPCODE'])

/**
 * Best guess of a type's class when only the type is known. The daemon reports classes where it
 * can (`by_class`, replacements); this fills the gaps (Stats, records older than `by_class`).
 */
export function inferClass(type: string, customTypes: ReadonlySet<string> = new Set()): EntityClass {
  if (PII_TYPES.has(type)) return 'PII'
  if (IDENTITY_TYPES.has(type)) return 'IDENTITY'
  if (NER_TYPES.has(type)) return 'NER'
  if (type === 'DATA') return 'STRUCT'
  if (type === 'CUSTOM' || customTypes.has(type)) return 'CUSTOM'
  return 'SECRET'
}

/** Counts per class, keyed by view class, zeros dropped. */
export type ClassCounts = Partial<Record<ViewClass, number>>

function addCount(out: ClassCounts, cls: ViewClass, n: number): void {
  if (n > 0) out[cls] = (out[cls] ?? 0) + n
}

/** The classes of an exchange's masked values: the daemon's `by_class`, or a guess from `by_type`. */
export function classCounts(
  summary: { by_type: Record<string, number>; by_class?: Record<string, number> },
  guess: (type: string) => ViewClass,
): { counts: ClassCounts; guessed: boolean } {
  const counts: ClassCounts = {}
  if (summary.by_class) {
    for (const [cls, n] of Object.entries(summary.by_class)) addCount(counts, viewClass(cls), n)
    return { counts, guessed: false }
  }
  for (const [type, n] of Object.entries(summary.by_type)) addCount(counts, guess(type), n)
  return { counts, guessed: true }
}

function sameCounts(a: ClassCounts, b: ClassCounts): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]) as Set<ViewClass>
  for (const key of keys) if ((a[key] ?? 0) !== (b[key] ?? 0)) return false
  return true
}

/**
 * The class of each type of an exchange, for coloring its type chips. `by_class` is the truth: the
 * guess is used only when it adds up to the same counts (or when `by_class` is missing); a single
 * class colors every type; otherwise types stay uncolored rather than risk a wrong color.
 */
export function typeClassesFor(
  summary: { by_type: Record<string, number>; by_class?: Record<string, number> },
  guess: (type: string) => ViewClass,
): (type: string) => ViewClass | undefined {
  if (!summary.by_class) return guess
  const actual = classCounts(summary, guess).counts
  const guessed: ClassCounts = {}
  for (const [type, n] of Object.entries(summary.by_type)) addCount(guessed, guess(type), n)
  if (sameCounts(actual, guessed)) return guess
  const classes = Object.keys(actual) as ViewClass[]
  if (classes.length === 1) {
    const only = classes[0]
    return () => only
  }
  return () => undefined
}

export const MODULES: readonly Module[] = [
  'secrets',
  'secret-structure',
  'personal',
  'ids',
  'identity',
  'terms',
  'recognizers',
  'names',
  'reviewer',
  'live',
  'fields',
  'data',
  'other',
]

export const MODULE_LABEL: Record<Module, string> = {
  secrets: tk('modules.secrets'),
  'secret-structure': tk('modules.secret-structure'),
  personal: tk('modules.personal'),
  ids: tk('modules.ids'),
  identity: tk('modules.identity'),
  terms: tk('modules.terms'),
  recognizers: tk('modules.recognizers'),
  names: tk('modules.names'),
  reviewer: tk('modules.reviewer'),
  live: tk('modules.live'),
  fields: tk('modules.fields'),
  data: tk('modules.data'),
  other: tk('modules.other'),
}

export const VIA_LABEL: Record<Via, string> = {
  detector: tk('via.detector'),
  known: tk('via.known'),
  repeat: tk('via.repeat'),
  echo: tk('via.echo'),
  plugin: tk('via.plugin'),
  restore: tk('via.restore'),
}

/** An exchange's `by_module`, in the order modules are listed (empty for older records). */
export function moduleCounts(summary: { by_module?: Partial<Record<Module, number>> }): [Module, number][] {
  const counts = summary.by_module ?? {}
  return MODULES.flatMap((m) => ((counts[m] ?? 0) > 0 ? [[m, counts[m] ?? 0] as [Module, number]] : []))
}
