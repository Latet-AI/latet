import { describe, expect, it } from 'vitest'
import { ANCHORS, PROJECT_ANCHORS } from '../components/rules'
import { personalDraftOf, setProtection } from './projects'
import {
  dirtyTabs,
  FIELD_SECTIONS,
  lostAfter,
  matchingSections,
  PROJECT_FIELD_SECTIONS,
  PROJECT_TABS,
  projectDirtyTabs,
  projectTabOf,
  RULES_TABS,
  SAVE_BAR_TABS,
  tabOf,
} from './rulesTabs'

describe('the tabs of Rules', () => {
  it('group the sections, the profile first, each list in a tab of its own', () => {
    expect(RULES_TABS.map((tab) => [tab.id, tab.anchors])).toEqual([
      ['protection', [ANCHORS.profile, ANCHORS.pii, ANCHORS.ner, ANCHORS.phones, ANCHORS.privateIps, ANCHORS.data]],
      ['terms', [ANCHORS.terms]],
      ['recognizers', [ANCHORS.recognizers]],
      ['values', [ANCHORS.allow]],
      ['policies', [ANCHORS.policies]],
      ['reviewers', [ANCHORS.reviewer, ANCHORS.live]],
      ['advanced', [ANCHORS.identity, ANCHORS.history]],
    ])
  })

  it('every anchor opens the tab that now holds it', () => {
    expect(tabOf(ANCHORS.terms)).toBe('terms')
    expect(tabOf(ANCHORS.recognizers)).toBe('recognizers')
    expect(tabOf(ANCHORS.allow)).toBe('values')
    expect(tabOf(ANCHORS.policies)).toBe('policies')
    expect(tabOf(ANCHORS.profile)).toBe('protection')
    expect(tabOf(ANCHORS.data)).toBe('protection')
    expect(tabOf(ANCHORS.live)).toBe('reviewers')
    expect(tabOf(ANCHORS.history)).toBe('advanced')
  })

  it('show the save bar only on the tabs without tables: protection, reviewers and advanced', () => {
    expect(SAVE_BAR_TABS).toEqual(['protection', 'reviewers', 'advanced'])
    const lists = RULES_TABS.filter((tab) => !SAVE_BAR_TABS.includes(tab.id)).flatMap((tab) => tab.anchors)
    expect(lists).toEqual([ANCHORS.terms, ANCHORS.recognizers, ANCHORS.allow, ANCHORS.policies])
  })

  it('hold every section of the rules in exactly one tab', () => {
    const listed = RULES_TABS.flatMap((tab) => tab.anchors)
    expect(new Set(listed).size).toBe(listed.length)
    expect([...listed].sort()).toEqual(Object.values(ANCHORS).sort())
  })

  it("name no section of a project's rules: a link to one never opens a tab of the global rules", () => {
    const listed: readonly string[] = RULES_TABS.flatMap((tab) => tab.anchors)
    for (const anchor of Object.values(PROJECT_ANCHORS)) expect(listed).not.toContain(anchor)
  })
})

describe("the tabs of a project's rules", () => {
  it('are those of the global rules that a project sets, by their ids and in their order, after the overview and before the repository file', () => {
    expect(PROJECT_TABS.map((tab) => tab.id)).toEqual(['overview', 'protection', 'terms', 'recognizers', 'values', 'policies', 'reviewers', 'repo'])
    const shared = PROJECT_TABS.map((tab) => tab.id as string).filter((id) => RULES_TABS.some((tab) => tab.id === id))
    expect(shared).toEqual(RULES_TABS.map((tab) => tab.id as string).filter((id) => id !== 'advanced'))
  })

  it('group the sections, how much of Latet applies first in Protection as the profile is in the global rules', () => {
    expect(PROJECT_TABS.map((tab) => [tab.id, tab.anchors])).toEqual([
      ['overview', [PROJECT_ANCHORS.effective, PROJECT_ANCHORS.personal]],
      ['protection', [PROJECT_ANCHORS.protection, PROJECT_ANCHORS.projectData]],
      ['terms', [PROJECT_ANCHORS.terms]],
      ['recognizers', [PROJECT_ANCHORS.recognizers]],
      ['values', [PROJECT_ANCHORS.allow]],
      ['policies', [PROJECT_ANCHORS.policies]],
      ['reviewers', [PROJECT_ANCHORS.reviewer, PROJECT_ANCHORS.live]],
      ['repo', [PROJECT_ANCHORS.repo]],
    ])
  })

  it("hold every part of a project's rules in exactly one tab", () => {
    const listed = PROJECT_TABS.flatMap((tab) => tab.anchors)
    expect(new Set(listed).size).toBe(listed.length)
    expect([...listed].sort()).toEqual(Object.values(PROJECT_ANCHORS).sort())
  })

  it('keep the addresses links already use: #protection, #effective, #personal, #repo and #project-data', () => {
    expect(PROJECT_ANCHORS).toMatchObject({
      protection: 'protection',
      effective: 'effective',
      personal: 'personal',
      repo: 'repo',
      projectData: 'project-data',
    })
  })
})

describe('projectTabOf', () => {
  it('gives the tab that holds a section of the project', () => {
    expect(projectTabOf('project-data')).toBe('protection')
    expect(projectTabOf('protection')).toBe('protection')
    expect(projectTabOf('effective')).toBe('overview')
    expect(projectTabOf('personal')).toBe('overview')
    expect(projectTabOf('project-terms')).toBe('terms')
    expect(projectTabOf('project-recognizers')).toBe('recognizers')
    expect(projectTabOf('project-allow')).toBe('values')
    expect(projectTabOf('project-policies')).toBe('policies')
    expect(projectTabOf('project-live-review')).toBe('reviewers')
    expect(projectTabOf('repo')).toBe('repo')
    for (const tab of PROJECT_TABS) for (const anchor of tab.anchors) expect(projectTabOf(anchor)).toBe(tab.id)
  })

  it('opens the overview for no section, one it does not know, or one of the global rules', () => {
    expect(projectTabOf('')).toBe('overview')
    expect(projectTabOf('nowhere')).toBe('overview')
    expect(projectTabOf(ANCHORS.terms)).toBe('overview')
  })
})

describe('projectDirtyTabs', () => {
  const saved = personalDraftOf({
    terms: [{ value: 'Ashford Paints', type: 'CLIENT', case_sensitive: false, whole_word: true }],
    allow_values: ['billing-dev'],
    fields: [{ name: 'open_invoices', mask: false }],
  })

  it('is empty when nothing changed', () => {
    expect(projectDirtyTabs(saved, structuredClone(saved), false)).toEqual(new Set())
  })

  it('names the tab of each setting that differs, the repository file when it changed', () => {
    expect(projectDirtyTabs(saved, setProtection(saved, 'off'), false)).toEqual(new Set(['protection']))
    expect(projectDirtyTabs(saved, setProtection(saved, 'secrets'), false)).toEqual(new Set(['protection']))
    expect(projectDirtyTabs(saved, { ...saved, data: 'strict' }, false)).toEqual(new Set(['protection']))
    expect(projectDirtyTabs(saved, { ...saved, terms: [] }, false)).toEqual(new Set(['terms']))
    expect(projectDirtyTabs(saved, { ...saved, allow_patterns: ['^dev-'], policies: { EMAIL: 'allow' } }, false)).toEqual(new Set(['values', 'policies']))
    expect(projectDirtyTabs(saved, { ...saved, live_review: true }, false)).toEqual(new Set(['reviewers']))
    expect(projectDirtyTabs(saved, { ...saved, review_guidance: 'Ticket numbers are internal.' }, false)).toEqual(new Set(['reviewers']))
    expect(projectDirtyTabs(saved, saved, true)).toEqual(new Set(['repo']))
  })

  it('compares the settings as they would be saved: guidance of spaces alone is no change', () => {
    expect(projectDirtyTabs(saved, { ...saved, review_guidance: '   ' }, false)).toEqual(new Set())
  })

  it('places every setting of yours the page edits in a section of a tab, and leaves out only what it keeps as it is', () => {
    const sections: readonly string[] = PROJECT_TABS.flatMap((tab) => tab.anchors)
    for (const section of Object.values(PROJECT_FIELD_SECTIONS)) expect(sections).toContain(section)
    const unplaced = Object.keys(personalDraftOf(null)).filter((field) => !(field in PROJECT_FIELD_SECTIONS))
    expect(unplaced.sort()).toEqual(['disabled_secret_rules', 'id_regions', 'include_private_ips', 'ner', 'ocr', 'phone_regions', 'pii'])
  })
})

describe('tabOf', () => {
  it('gives the tab that holds a section', () => {
    expect(tabOf('terms')).toBe('terms')
    expect(tabOf(ANCHORS.data)).toBe('protection')
    expect(tabOf(ANCHORS.reviewer)).toBe('reviewers')
    expect(tabOf(ANCHORS.live)).toBe('reviewers')
    expect(tabOf(ANCHORS.policies)).toBe('policies')
    expect(tabOf(ANCHORS.history)).toBe('advanced')
    for (const tab of RULES_TABS) for (const anchor of tab.anchors) expect(tabOf(anchor)).toBe(tab.id)
  })

  it('opens Protection for no section or one it does not know', () => {
    expect(tabOf('')).toBe('protection')
    expect(tabOf('nowhere')).toBe('protection')
    expect(tabOf(PROJECT_ANCHORS.projectData)).toBe('protection')
  })
})

describe('dirtyTabs', () => {
  const saved = {
    profile: 'balanced',
    terms: [{ value: 'Project Aurora' }],
    allow_values: ['a@example.com'],
    llm_review: { enabled: false },
    history: 'full',
  }
  const sectionOf = {
    profile: ANCHORS.profile,
    terms: ANCHORS.terms,
    allow_values: ANCHORS.allow,
    llm_review: ANCHORS.reviewer,
    history: ANCHORS.history,
  } as const

  it('is empty when nothing changed', () => {
    expect(dirtyTabs(saved, structuredClone(saved), sectionOf)).toEqual(new Set())
  })

  it('names the tab of each field that differs from the saved settings', () => {
    const draft = { ...saved, terms: [{ value: 'Project Aurora 2' }], profile: 'max' }
    expect(dirtyTabs(saved, draft, sectionOf)).toEqual(new Set(['terms', 'protection']))
    expect(dirtyTabs(saved, { ...saved, llm_review: { enabled: true } }, sectionOf)).toEqual(new Set(['reviewers']))
  })

  it('says a field is unchanged once it is back as it was', () => {
    const draft = { ...saved, allow_values: [...saved.allow_values] }
    expect(dirtyTabs(saved, draft, sectionOf)).toEqual(new Set())
  })

  it('places every setting of the form in a section of a tab', () => {
    const sections: readonly string[] = RULES_TABS.flatMap((tab) => tab.anchors)
    for (const section of Object.values(FIELD_SECTIONS)) expect(sections).toContain(section)
    expect(FIELD_SECTIONS.terms).toBe(ANCHORS.terms)
    expect(FIELD_SECTIONS.profile).toBe(ANCHORS.profile)
    expect(FIELD_SECTIONS.data_tools).toBe(ANCHORS.data)
    expect(FIELD_SECTIONS.auto_mode_review).toBe(ANCHORS.identity)
  })
})

describe('matchingSections', () => {
  const sections = [
    { anchor: 'phones', title: 'Phone regions', description: 'Numbers without an international prefix.' },
    { anchor: 'terms', title: 'Custom terms', description: 'Words of yours that Latet always masks.' },
    { anchor: 'reviewer', title: 'Revisore locale', description: 'Un modello linguistico sul tuo computer.' },
    { anchor: 'live', title: 'Live-Prüfer', description: 'Hält Anfragen an.' },
  ]

  it('finds a section by its title, whatever the case', () => {
    expect(matchingSections(sections, 'phone').map((s) => s.anchor)).toEqual(['phones'])
    expect(matchingSections(sections, 'CUSTOM').map((s) => s.anchor)).toEqual(['terms'])
  })

  it('finds a section by its description', () => {
    expect(matchingSections(sections, 'masks').map((s) => s.anchor)).toEqual(['terms'])
  })

  it('needs every word, in any order, and ignores accents', () => {
    expect(matchingSections(sections, 'computer modello').map((s) => s.anchor)).toEqual(['reviewer'])
    expect(matchingSections(sections, 'prufer').map((s) => s.anchor)).toEqual(['live'])
    expect(matchingSections(sections, 'phone masks')).toEqual([])
  })

  it('keeps every section for an empty search', () => {
    expect(matchingSections(sections, '   ')).toEqual(sections)
  })
})

describe('lostAfter', () => {
  it('keeps the tabs said to have lost an edit until Dismiss, a later merge adding to them', () => {
    expect(lostAfter([], ['protection'])).toEqual(['protection'])
    // A second merge of the same reload, or a later save, finds nothing more to drop: the notice stays.
    const said = ['protection']
    expect(lostAfter(said, [])).toBe(said)
    expect(lostAfter(['protection'], ['reviewers', 'protection'])).toEqual(['protection', 'reviewers'])
  })
})
