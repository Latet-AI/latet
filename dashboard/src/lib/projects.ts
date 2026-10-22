// A project's settings as the dashboard edits them: its latet.yaml, and your own for it.

import type {
  DataMode,
  FieldRule,
  ImagesMode,
  NerMode,
  OcrMode,
  PersonalSettings,
  Policy,
  Profile,
  ProjectDetail,
  ProjectSettings,
  RecognizerEntry,
  ReviewMode,
  Term,
} from '../api/types'
import { cleanGuidance } from './reviewer'

/** What a repository file may set, and only to add protection. */
export const REPO_KEYS = [
  'profile',
  'pii',
  'id_regions',
  'phone_regions',
  'include_private_ips',
  'ner',
  'terms',
  'recognizers',
  'policies',
  'data',
  'data_files',
  'data_hosts',
  'fields',
  'dump_guard',
  'images',
  'ocr',
  'llm_review',
] as const

/** A project's settings with every list and map present, so the editors need no checks. */
export interface SettingsDraft {
  profile: Profile | null
  pii: string[]
  id_regions: string[]
  phone_regions: string[]
  include_private_ips: boolean | null
  ner: NerMode | null
  terms: Term[]
  recognizers: RecognizerEntry[]
  allow_values: string[]
  allow_patterns: string[]
  policies: Record<string, Policy>
  disabled_secret_rules: string[]
  data: DataMode | null
  data_tools: string[]
  data_files: string[]
  data_hosts: string[]
  fields: FieldRule[]
  dump_guard: number | null
  images: ImagesMode | null
  /** No editor sets it here: kept as the file or your config.yaml says, so that a save never drops it. */
  ocr: OcrMode | null
  llm_review: ReviewMode | null
}

/**
 * Yours alone: whether Latet is on there, the Live Reviewer (null: as in the global settings), and
 * your guidance to the local reviewer there (after the global one).
 */
export type PersonalDraft = SettingsDraft & {
  enabled: boolean
  live_review: boolean | null
  review_guidance: string | null
  review_images: boolean | null
}

export function draftOf(settings: ProjectSettings | null | undefined): SettingsDraft {
  const s = settings ?? {}
  return {
    profile: s.profile ?? null,
    pii: [...(s.pii ?? [])],
    id_regions: [...(s.id_regions ?? [])],
    phone_regions: [...(s.phone_regions ?? [])],
    include_private_ips: s.include_private_ips ?? null,
    ner: s.ner ?? null,
    terms: [...(s.terms ?? [])],
    recognizers: [...(s.recognizers ?? [])],
    allow_values: [...(s.allow_values ?? [])],
    allow_patterns: [...(s.allow_patterns ?? [])],
    policies: { ...(s.policies ?? {}) },
    disabled_secret_rules: [...(s.disabled_secret_rules ?? [])],
    data: s.data ?? null,
    data_tools: [...(s.data_tools ?? [])],
    data_files: [...(s.data_files ?? [])],
    data_hosts: [...(s.data_hosts ?? [])],
    fields: [...(s.fields ?? [])],
    dump_guard: s.dump_guard ?? null,
    images: s.images ?? null,
    ocr: s.ocr ?? null,
    llm_review: s.llm_review ?? null,
  }
}

export function personalDraftOf(personal: PersonalSettings | null): PersonalDraft {
  return {
    ...draftOf(personal),
    enabled: personal?.enabled ?? true,
    live_review: personal?.live_review ?? null,
    review_images: personal?.review_images ?? null,
    review_guidance: personal?.review_guidance ?? null,
  }
}

const LISTS = [
  ...['pii', 'id_regions', 'phone_regions', 'terms', 'recognizers', 'allow_values', 'allow_patterns'],
  ...['disabled_secret_rules', 'data_tools', 'data_files', 'data_hosts', 'fields'],
] as const

/** The settings to save: empty values left out; for a repository file, only what it may set. */
export function settingsOf(draft: SettingsDraft, repo: boolean): ProjectSettings {
  const out: ProjectSettings = {}
  if (draft.profile) out.profile = draft.profile
  for (const key of LISTS) {
    const list = draft[key]
    if (list.length > 0) (out as Record<string, unknown>)[key] = list
  }
  if (draft.include_private_ips !== null) out.include_private_ips = draft.include_private_ips
  if (draft.ner) out.ner = draft.ner
  if (Object.keys(draft.policies).length > 0) out.policies = draft.policies
  if (draft.data) out.data = draft.data
  if (draft.dump_guard !== null) out.dump_guard = draft.dump_guard
  if (draft.images) out.images = draft.images
  if (draft.ocr) out.ocr = draft.ocr
  if (draft.llm_review) out.llm_review = draft.llm_review
  if (repo) {
    for (const key of Object.keys(out)) {
      if (!(REPO_KEYS as readonly string[]).includes(key)) delete (out as Record<string, unknown>)[key]
    }
  }
  return out
}

export function personalOf(draft: PersonalDraft): PersonalSettings {
  const guidance = cleanGuidance(draft.review_guidance)
  return {
    ...settingsOf(draft, false),
    ...(draft.enabled ? {} : { enabled: false }),
    ...(draft.live_review === null ? {} : { live_review: draft.live_review }),
    ...(draft.review_images === null ? {} : { review_images: draft.review_images }),
    ...(guidance === null ? {} : { review_guidance: guidance }),
  }
}

/** Two drafts that would save the same thing. */
export function sameSettings(a: SettingsDraft, b: SettingsDraft): boolean {
  return JSON.stringify(settingsOf(a, false)) === JSON.stringify(settingsOf(b, false))
}

/** Two drafts of your own settings that would save the same thing, what only you may set included. */
export function samePersonal(a: PersonalDraft, b: PersonalDraft): boolean {
  return JSON.stringify(personalOf(a)) === JSON.stringify(personalOf(b))
}

/** How much of Latet applies in a project: as the other settings say, secrets only, or none. */
export type Protection = 'on' | 'secrets' | 'off'

export function protectionOf(draft: PersonalDraft): Protection {
  if (!draft.enabled) return 'off'
  return draft.profile === 'secrets' ? 'secrets' : 'on'
}

export function setProtection(draft: PersonalDraft, protection: Protection): PersonalDraft {
  if (protection === 'off') return { ...draft, enabled: false }
  if (protection === 'secrets') return { ...draft, enabled: true, profile: 'secrets' }
  return { ...draft, enabled: true, profile: draft.profile === 'secrets' ? null : draft.profile }
}

/** Which of a project's own settings a rule comes from: its latet.yaml, yours, or neither (the global ones). */
export function ruleLayer(rule: string | null, project: Pick<ProjectDetail, 'repo' | 'personal'> | null): 'repo' | 'personal' | null {
  if (!rule || !project) return null
  const repo = project.repo.settings
  const personal = project.personal ?? {}
  if (rule.startsWith('recognizer:')) {
    const name = rule.slice('recognizer:'.length)
    if (repo.recognizers?.some((r) => r.name === name)) return 'repo'
    if (personal.recognizers?.some((r) => r.name === name)) return 'personal'
    return null
  }
  if (rule.startsWith('terms:')) {
    const type = rule.slice('terms:'.length).toUpperCase()
    if (repo.terms?.some((t) => t.type.toUpperCase() === type)) return 'repo'
    if (personal.terms?.some((t) => t.type.toUpperCase() === type)) return 'personal'
  }
  if (rule.startsWith('field:')) {
    const name = rule.slice('field:'.length).toLowerCase()
    if (personal.fields?.some((f) => f.name.toLowerCase() === name)) return 'personal'
    if (repo.fields?.some((f) => f.name.toLowerCase() === name)) return 'repo'
  }
  if (rule.startsWith('strict:')) {
    if (personal.data) return personal.data === 'strict' ? 'personal' : null
    if (repo.data === 'strict') return 'repo'
    // Set in neither: the profile in effect there decides, and max turns it on.
    if (personal.profile) return personal.profile === 'max' ? 'personal' : null
    if (repo.profile === 'max') return 'repo'
  }
  return null
}

/** "Always mask" or "Not sensitive" on one value, from an exchange or a review. */
export type ValueChoice = { kind: 'mask'; value: string; type: string } | { kind: 'allow'; value: string }

/**
 * Your settings for a project with a value added to its terms or to its values never to mask. As in
 * the global settings, "always mask" takes the value out of an earlier "not sensitive".
 */
export function withValue(personal: PersonalSettings, choice: ValueChoice): PersonalSettings {
  const allow = personal.allow_values ?? []
  if (choice.kind === 'allow') {
    return allow.includes(choice.value) ? personal : { ...personal, allow_values: [...allow, choice.value] }
  }
  const terms = personal.terms ?? []
  const term: Term = { value: choice.value, type: choice.type.toUpperCase(), case_sensitive: false, whole_word: true }
  const out: PersonalSettings = { ...personal, terms: terms.some((t) => t.value === choice.value) ? terms : [...terms, term] }
  if (personal.allow_values) out.allow_values = allow.filter((v) => v !== choice.value)
  return out
}
