import { createElement as h, Fragment, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { Me, ProjectDetail } from '../api/types'
import type { RulesTabsState } from '../components/RulesTabs'
import i18n from '../i18n'
import { MeContext, type MeState } from '../lib/me'
import type { ProjectTab } from '../lib/rulesTabs'
import { initialConfig } from '../mock/fixtures'
import { computer, server } from '../test/eop35-fixtures'
import { ProjectForm } from './ProjectRules'

// A project's rules as they render, tab by tab: the tabs, which sections each shows, and the save bar's rows. Which
// tab a link opens and which tabs hold changes are lib/rulesTabs.test.ts; following links, picking tabs and saving in
// a browser are in scripts/ui-checks.mjs.

vi.mock('../components/Floating', async (original) => ({
  ...(await original<typeof import('../components/Floating')>()),
  Tooltip: ({ children }: { children: ReactNode }) => h(Fragment, null, children),
}))

const project: ProjectDetail = {
  id: 'a1b2c3',
  name: 'billing-app',
  root: '/home/marc/dev/billing-app',
  repo_file: '/home/marc/dev/billing-app/latet.yaml',
  has_personal: true,
  enabled: true,
  profile: 'max',
  notice_count: 0,
  last_seen: 0,
  repo: {
    path: '/home/marc/dev/billing-app/latet.yaml',
    exists: true,
    settings: { profile: 'max', terms: [{ value: 'Project Falcon', type: 'PROJECT', case_sensitive: false, whole_word: true }] },
    tag: 'tag-1',
    readable: true,
    writable: true,
  },
  personal: { allow_values: ['billing-dev'] },
  notices: [],
  effective: {
    profile: 'max',
    pii: [],
    id_regions: [],
    phone_regions: [],
    include_private_ips: null,
    ner: 'auto',
    terms: 1,
    recognizers: 0,
    allow_values: 1,
    allow_patterns: 0,
    policies: {},
    data: 'strict',
    data_tools: [],
    data_files: [],
    data_hosts: [],
    fields: [],
    dump_guard: null,
    images: 'allow',
    llm_review: 'off',
  },
  recognizer_status: [],
}

function render(active: ProjectTab, { query = '', me = computer, of = project }: { query?: string; me?: Me; of?: ProjectDetail } = {}) {
  const tabs: RulesTabsState<ProjectTab> = { active, pick: () => undefined, query, setQuery: () => undefined }
  const value: MeState = { me, unreachable: false, customTypes: new Set(), reload: () => undefined, reloadConfig: () => undefined }
  return renderToStaticMarkup(
    h(
      MemoryRouter,
      null,
      h(
        MeContext.Provider,
        { value },
        h(ProjectForm, { project: of, config: initialConfig, etag: 'e1', refused: null, tabs, onSaved: () => undefined, onRefresh: () => undefined }),
      ),
    ),
  )
}

const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim()

/** The tabs' names, as they read (the words a screen reader hears for a dot left out, and the count of a list's rows). */
const tabNames = (html: string) =>
  [...html.matchAll(/<button\b[^>]*role="tab"[^>]*>(.*?)<\/button>/g)].map((match) =>
    text((match[1] ?? '').replace(/<span class="sr-only">.*?<\/span>/g, '').replace(/<span[^>]*inline-grid[^>]*>.*?<\/span>/g, '')),
  )

/** The count each tab shows beside its name. */
const tabCounts = (html: string) =>
  [...html.matchAll(/<button\b[^>]*role="tab"[^>]*id="project-rules-tab-([a-z]+)"[^>]*>(.*?)<\/button>/g)].map((match) => [
    match[1],
    text(/<span[^>]*inline-grid[^>]*>(.*?)<\/span>/.exec(match[2] ?? '')?.[1] ?? ''),
  ])

/** The ids of the sections that show (the others are on the page, hidden). */
const shownSections = (html: string) =>
  [...html.matchAll(/<section\b([^>]*)>/g)].map((match) => match[1] ?? '').filter((attributes) => !/\bhidden=""/.test(attributes)).map((attributes) => /\bid="([^"]*)"/.exec(attributes)?.[1])

/** One section's markup, from its tag to the next section's. */
const section = (html: string, id: string) => {
  const start = html.indexOf(`<section id="${id}"`)
  const end = html.indexOf('<section', start + 1)
  return html.slice(start, end < 0 ? undefined : end)
}

/** The labels of the save bar's buttons, in order. */
const saveButtons = (html: string) => {
  const bar = html.slice(html.lastIndexOf('sticky bottom-0'))
  return [...bar.matchAll(/<button\b[^>]*>(.*?)<\/button>/g)].map((match) => text(match[1] ?? ''))
}

describe("a project's rules", () => {
  it('have the tabs of the global rules a project sets, named as there, between its overview and its repository file', () => {
    expect(tabNames(render('overview'))).toEqual([
      i18n.t('projects.tabs.overview'),
      i18n.t('rules.tabs.protection'),
      i18n.t('rules.tabs.terms'),
      i18n.t('rules.tabs.recognizers'),
      i18n.t('rules.tabs.values'),
      i18n.t('rules.tabs.policies'),
      i18n.t('rules.tabs.reviewers'),
      i18n.t('projects.tabs.repo'),
    ])
    expect(render('overview')).toContain(`aria-label="${i18n.t('rules.tabs.label')}"`)
  })

  it('open on what is in effect there, and your settings as a whole, with the save of yours below', () => {
    const html = render('overview')
    expect(shownSections(html)).toEqual(['effective', 'personal'])
    expect(text(section(html, 'personal'))).toContain(i18n.t('transfer.exportProject'))
    expect(text(section(html, 'personal'))).toContain(i18n.t('projects.personal.remove'))
    expect(saveButtons(html)).toEqual([i18n.t('rules.discard'), i18n.t('projects.personal.save')])
  })

  it('show each tab of your settings with the sections of its namesake in the global rules', () => {
    expect(shownSections(render('protection'))).toEqual(['protection', 'project-data'])
    expect(shownSections(render('terms'))).toEqual(['project-terms'])
    expect(shownSections(render('recognizers'))).toEqual(['project-recognizers'])
    expect(shownSections(render('values'))).toEqual(['project-allow'])
    expect(shownSections(render('policies'))).toEqual(['project-policies'])
    expect(shownSections(render('reviewers'))).toEqual(['project-reviewer', 'project-live-review'])
    // The global rules' own titles: Custom terms, Allow list, Production data.
    expect(text(section(render('terms'), 'project-terms'))).toContain(i18n.t('rules.terms.title'))
    expect(text(section(render('values'), 'project-allow'))).toContain(i18n.t('rules.allow.title'))
    expect(text(section(render('protection'), 'project-data'))).toContain(i18n.t('data.title'))
  })

  it('keep every section on the page, those of the other tabs hidden, so that nothing being edited is lost', () => {
    const html = render('terms')
    for (const id of ['effective', 'personal', 'protection', 'project-data', 'project-allow', 'project-reviewer', 'repo']) {
      expect(html).toContain(`<section id="${id}"`)
    }
  })

  it('give the repository file a tab of its own, with its own save, offering only what a repository file sets and no list', () => {
    const html = render('repo')
    expect(shownSections(html)).toEqual(['repo'])
    expect(saveButtons(html)).toEqual([i18n.t('rules.discard'), i18n.t('projects.repo.save')])
    const repo = text(section(html, 'repo'))
    for (const key of ['rules.allow.title', 'data.tools.title', 'data.fields.clear', 'projects.live.title', 'reviewer.guidance.projectTitle']) {
      expect(repo).not.toContain(i18n.t(key))
    }
    // Its terms, recognizers and policies are rows of the tables, from latet.yaml.
    for (const key of ['rules.terms.title', 'rules.recognizers.title', 'rules.policies.title']) expect(repo).not.toContain(i18n.t(key))
    expect(repo).toContain(i18n.t('projects.repo.profile'))
  })

  it("show every term that applies there in the Terms tab, a row per source with where it comes from, and no save bar", () => {
    const html = render('terms')
    const terms = text(section(html, 'project-terms'))
    expect(terms).toContain('Project Falcon')
    expect(terms).toContain(i18n.t('rules.from.repo'))
    for (const term of initialConfig.terms) expect(terms).toContain(term.value)
    expect(terms).toContain(i18n.t('rules.from.global'))
    expect(html).not.toContain('sticky bottom-0')
  })

  it("count a list's rows of yours and in all on its tab", () => {
    const counts = Object.fromEntries(tabCounts(render('overview')))
    // Yours: none; in all: the global rules' and latet.yaml's Project Falcon.
    expect(counts.terms).toBe(i18n.t('rules.tabs.countProject', { count: 0, all: initialConfig.terms.length + 1 }))
    expect(counts.values).toBe(
      i18n.t('rules.tabs.countProject', { count: 1, all: initialConfig.allow_values.length + initialConfig.allow_patterns.length + 1 }),
    )
  })

  it("count yours in the singular and the plural of each language", () => {
    const count = (lng: string, yours: number) => i18n.getFixedT(lng)('rules.tabs.countProject', { count: yours, all: 4 })
    expect(count('en', 1)).toBe('1 yours · 4 in all')
    expect(count('it', 1)).toBe('1 tuo · 4 in tutto')
    expect(count('it', 2)).toBe('2 tuoi · 4 in tutto')
    expect(count('es', 1)).toBe('1 tuyo · 4 en total')
    expect(count('es', 2)).toBe('2 tuyos · 4 en total')
  })

  it('offer to create the repository file when the project has none', () => {
    const none = { ...project, repo: { ...project.repo, exists: false, settings: {} } }
    expect(saveButtons(render('repo', { of: none }))).toEqual([i18n.t('rules.discard'), i18n.t('projects.repo.create')])
  })

  it('only show the repository file on a server, which cannot write it: its fields are off, and nothing saves it', () => {
    const html = render('repo', { me: server('person') })
    expect(section(html, 'repo')).toMatch(/<fieldset disabled=""/)
    expect(html).not.toContain('sticky bottom-0')
  })

  it('find sections in every tab while the search holds text, the tabs hidden meanwhile', () => {
    const html = render('overview', { query: i18n.t('rules.policies.title') })
    expect(html).not.toContain('role="tablist"')
    expect(shownSections(html)).toContain('project-policies')
    expect(shownSections(html)).not.toContain('effective')
  })

  it('say so when the search finds nothing', () => {
    const html = render('overview', { query: 'no section says this' })
    expect(shownSections(html)).toEqual([])
    expect(text(html)).toContain(i18n.t('rules.searchNone'))
  })
})
