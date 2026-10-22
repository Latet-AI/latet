// The tabs of Rules, the global ones and a project's: which sections each holds, which tab a link to a section
// opens, which tabs hold unsaved changes, and which sections a search finds.

import type { PersonalSettings } from '../api/types'
import { ANCHORS, PROJECT_ANCHORS } from '../components/rules'
import { personalOf, type PersonalDraft } from './projects'

export type RulesTab = 'protection' | 'terms' | 'recognizers' | 'values' | 'policies' | 'reviewers' | 'advanced'

/** A section of the rules, as links name it (`/rules#personal-data`). */
export type RulesAnchor = (typeof ANCHORS)[keyof typeof ANCHORS]

/** A tab, with its sections in the order they show. */
export interface TabSections<T extends string = string, A extends string = string> {
  id: T
  anchors: readonly A[]
}

/** A row of tabs, the first opened when a link names none of its sections. */
export type TabRow<T extends string = string, A extends string = string> = readonly [TabSections<T, A>, ...TabSections<T, A>[]]

/** The tabs, in order, with their sections in the order they show: the profile first, then each list in a tab of its
 * own (a table), then the reviewers and the rest. */
export const RULES_TABS: TabRow<RulesTab, RulesAnchor> = [
  { id: 'protection', anchors: [ANCHORS.profile, ANCHORS.pii, ANCHORS.ner, ANCHORS.phones, ANCHORS.privateIps, ANCHORS.data] },
  { id: 'terms', anchors: [ANCHORS.terms] },
  { id: 'recognizers', anchors: [ANCHORS.recognizers] },
  { id: 'values', anchors: [ANCHORS.allow] },
  { id: 'policies', anchors: [ANCHORS.policies] },
  { id: 'reviewers', anchors: [ANCHORS.reviewer, ANCHORS.live] },
  { id: 'advanced', anchors: [ANCHORS.identity, ANCHORS.history] },
]

/** The tabs without tables, whose settings the save bar saves: a table's rows are saved from their panel. */
export const SAVE_BAR_TABS: readonly RulesTab[] = ['protection', 'reviewers', 'advanced']

/** The tab of `tabs` that holds the section `anchor`; the first, for none or one it does not know. */
export function tabIn<T extends string>(tabs: TabRow<T>, anchor: string): T {
  return (tabs.find((tab) => (tab.anchors as readonly string[]).includes(anchor)) ?? tabs[0]).id
}

/** The tab that holds the section `anchor`; Protection, the first, for none or one it does not know. */
export function tabOf(anchor: string): RulesTab {
  return tabIn(RULES_TABS, anchor)
}

/** The section each setting of the form is edited in (the Rules page checks that it names every one). */
export const FIELD_SECTIONS = {
  profile: ANCHORS.profile,
  pii: ANCHORS.pii,
  id_regions: ANCHORS.pii,
  ner: ANCHORS.ner,
  phone_regions: ANCHORS.phones,
  include_private_ips: ANCHORS.privateIps,
  data: ANCHORS.data,
  data_tools: ANCHORS.data,
  data_files: ANCHORS.data,
  data_hosts: ANCHORS.data,
  fields: ANCHORS.data,
  dump_guard: ANCHORS.data,
  images: ANCHORS.data,
  ocr: ANCHORS.data,
  terms: ANCHORS.terms,
  recognizers: ANCHORS.recognizers,
  allow_values: ANCHORS.allow,
  allow_patterns: ANCHORS.allow,
  policies: ANCHORS.policies,
  llm_review: ANCHORS.reviewer,
  live_review: ANCHORS.live,
  identity: ANCHORS.identity,
  auto_mode_review: ANCHORS.identity,
  history: ANCHORS.history,
  retention_days: ANCHORS.history,
} as const satisfies Record<string, RulesAnchor>

/** The tabs of `tabs` with a setting in `draft` that differs from the saved one, by the section it is edited in. */
export function dirtyTabsIn<T extends string, F extends object>(
  tabs: TabRow<T>,
  saved: F,
  draft: F,
  sectionOf: { readonly [K in keyof F]?: string },
): Set<T> {
  const dirty = new Set<T>()
  for (const field of Object.keys(sectionOf) as (keyof F)[]) {
    const section = sectionOf[field]
    if (section !== undefined && JSON.stringify(saved[field]) !== JSON.stringify(draft[field])) dirty.add(tabIn(tabs, section))
  }
  return dirty
}

/** The tabs with a setting in `draft` that differs from the saved one, by the section each setting is edited in. */
export function dirtyTabs<T extends object>(saved: T, draft: T, sectionOf: { readonly [K in keyof T]: RulesAnchor }): Set<RulesTab> {
  return dirtyTabsIn(RULES_TABS, saved, draft, sectionOf)
}

/** The tabs of a project's rules: its overview, the tabs of the global rules that a project sets, its latet.yaml. */
export type ProjectTab = 'overview' | Exclude<RulesTab, 'advanced'> | 'repo'

/** A section of a project's rules, as links name it (`/rules?project=<id>#project-data`). */
export type ProjectAnchor = (typeof PROJECT_ANCHORS)[keyof typeof PROJECT_ANCHORS]

/**
 * A project's tabs, in order: first its overview (what is in effect there, your settings for it as a whole); then your
 * settings for it in the tabs of the global rules, with their names and in their order, each holding the sections of
 * its namesake that a project sets (Protection opens on how much of Latet applies there, as the global one on the
 * profile; none of Advanced: identity and history are global); last, its repository file, which has a save of its own.
 */
export const PROJECT_TABS: TabRow<ProjectTab, ProjectAnchor> = [
  { id: 'overview', anchors: [PROJECT_ANCHORS.effective, PROJECT_ANCHORS.personal] },
  { id: 'protection', anchors: [PROJECT_ANCHORS.protection, PROJECT_ANCHORS.projectData] },
  { id: 'terms', anchors: [PROJECT_ANCHORS.terms] },
  { id: 'recognizers', anchors: [PROJECT_ANCHORS.recognizers] },
  { id: 'values', anchors: [PROJECT_ANCHORS.allow] },
  { id: 'policies', anchors: [PROJECT_ANCHORS.policies] },
  { id: 'reviewers', anchors: [PROJECT_ANCHORS.reviewer, PROJECT_ANCHORS.live] },
  { id: 'repo', anchors: [PROJECT_ANCHORS.repo] },
]

/** The tab of a project's rules that holds the section `anchor`; the overview for none or one it does not know. */
export function projectTabOf(anchor: string): ProjectTab {
  return tabIn(PROJECT_TABS, anchor)
}

/**
 * The section each of your own settings for a project is edited in. What the page does not edit (the personal-data
 * types and countries, the phone regions, private IP addresses, name detection, reading the text in images, the
 * secret rules turned off) is saved back as it is, and never differs.
 */
export const PROJECT_FIELD_SECTIONS = {
  enabled: PROJECT_ANCHORS.protection,
  profile: PROJECT_ANCHORS.protection,
  data: PROJECT_ANCHORS.projectData,
  data_tools: PROJECT_ANCHORS.projectData,
  data_files: PROJECT_ANCHORS.projectData,
  data_hosts: PROJECT_ANCHORS.projectData,
  fields: PROJECT_ANCHORS.projectData,
  dump_guard: PROJECT_ANCHORS.projectData,
  images: PROJECT_ANCHORS.projectData,
  terms: PROJECT_ANCHORS.terms,
  recognizers: PROJECT_ANCHORS.recognizers,
  allow_values: PROJECT_ANCHORS.allow,
  allow_patterns: PROJECT_ANCHORS.allow,
  policies: PROJECT_ANCHORS.policies,
  llm_review: PROJECT_ANCHORS.reviewer,
  review_images: PROJECT_ANCHORS.reviewer,
  review_guidance: PROJECT_ANCHORS.reviewer,
  live_review: PROJECT_ANCHORS.live,
} as const satisfies { readonly [K in keyof PersonalSettings]?: ProjectAnchor }

/**
 * The tabs of a project's rules with unsaved changes: those where your settings differ from the saved ones as they
 * would be saved (a guidance of spaces alone is none), and the repository file's when it changed.
 */
export function projectDirtyTabs(saved: PersonalDraft, draft: PersonalDraft, repoDirty: boolean): Set<ProjectTab> {
  const dirty = dirtyTabsIn(PROJECT_TABS, personalOf(saved), personalOf(draft), PROJECT_FIELD_SECTIONS)
  if (repoDirty) dirty.add('repo')
  return dirty
}

/**
 * The tabs the notice says lost an unsaved edit, after a merge that dropped those of `dropped`: the ones said before
 * stay until Dismiss (a second merge of the same reload, or a later save, finds nothing more to drop).
 */
export function lostAfter<T>(said: readonly T[], dropped: Iterable<T>): readonly T[] {
  const added = [...new Set(dropped)].filter((tab) => !said.includes(tab))
  return added.length === 0 ? said : [...said, ...added]
}

/** Lower case and without accents, so that "prufer" finds "Prüfer" and "telefono" finds "telefónicas". */
const folded = (text: string) => text.normalize('NFD').replace(/\p{M}/gu, '').toLocaleLowerCase()

/** The sections whose title or description holds every word of `query`, in their order; all of them for no words. */
export function matchingSections<S extends { title: string; description?: string }>(sections: readonly S[], query: string): S[] {
  const words = folded(query).split(/\s+/).filter(Boolean)
  if (words.length === 0) return [...sections]
  return sections.filter((section) => {
    const text = folded(`${section.title} ${section.description ?? ''}`)
    return words.every((word) => text.includes(word))
  })
}
