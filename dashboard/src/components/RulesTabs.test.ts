import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import i18n from '../i18n'
import { PROJECT_TABS, RULES_TABS, type RulesAnchor, type TabRow } from '../lib/rulesTabs'
import { shownSections, useRulesTabs, type RulesSection } from './RulesTabs'
import { SaveRow } from './SaveBar'

const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/\s+/g, ' ').trim()

/** The tab `useRulesTabs` shows at `address`. */
function activeAt(address: string, tabs: TabRow): string {
  let active = ''
  function Probe() {
    active = useRulesTabs(tabs).active
    return null
  }
  renderToStaticMarkup(h(MemoryRouter, { initialEntries: [address] }, h(Probe)))
  return active
}

describe('useRulesTabs', () => {
  it('shows the tab that holds the section the address names', () => {
    expect(activeAt('/rules#reviewer', RULES_TABS)).toBe('reviewers')
    expect(activeAt('/rules?project=a1b2c3#project-data', PROJECT_TABS)).toBe('protection')
    expect(activeAt('/rules?project=a1b2c3#repo', PROJECT_TABS)).toBe('repo')
  })

  it('shows the first tab for an address that names no section of its own', () => {
    expect(activeAt('/rules', RULES_TABS)).toBe('protection')
    expect(activeAt('/rules?project=a1b2c3', PROJECT_TABS)).toBe('overview')
    expect(activeAt('/rules?project=a1b2c3#reviewer', PROJECT_TABS)).toBe('overview')
  })
})

describe('SaveRow', () => {
  const row = (props: Partial<Parameters<typeof SaveRow>[0]>) =>
    renderToStaticMarkup(
      h(SaveRow, { dirty: false, saving: false, error: null, saveLabel: 'Save', onDiscard: () => undefined, onSave: () => undefined, ...props }),
    )
  const buttons = (html: string) => [...html.matchAll(/<button\b([^>]*)>(.*?)<\/button>/g)].map((m) => [text(m[2] ?? ''), /\bdisabled=""/.test(m[1] ?? '')])

  it('says there is nothing to save, and offers nothing, until something changed', () => {
    const html = row({})
    expect(text(html)).toContain(i18n.t('rules.upToDate'))
    expect(buttons(html)).toEqual([
      [i18n.t('rules.discard'), true],
      ['Save', true],
    ])
  })

  it('says a change is not saved, and offers Discard and Save', () => {
    const html = row({ dirty: true })
    expect(text(html)).toContain(i18n.t('rules.unsaved'))
    expect(buttons(html)).toEqual([
      [i18n.t('rules.discard'), false],
      ['Save', false],
    ])
  })

  it('offers to save with nothing changed when told to (a file to create)', () => {
    expect(buttons(row({ force: true }))[1]).toEqual(['Save', false])
  })

  it('shows what a save left once it is done', () => {
    expect(text(row({ saved: h('span', null, 'Version 4 applies') }))).toBe('Version 4 applies Discard changes Save')
  })

  it('says why a save was refused: rules it cannot save, field by field', () => {
    const refused = new ApiError(400, 'invalid settings', [{ loc: ['terms', 0, 'value'], msg: 'too long' }])
    const shown = text(row({ dirty: true, error: refused }))
    expect(shown).toContain(i18n.t('rules.invalid'))
    expect(shown).toContain('terms.0.value: too long')
  })

  it("says a repository file changed meanwhile in this page's words", () => {
    expect(text(row({ error: new ApiError(409, 'changed'), conflict: 'latet.yaml changed' }))).toContain('latet.yaml changed')
  })
})

describe('shownSections', () => {
  const section = (title: string, found?: number): RulesSection => ({ title, description: '', body: null, found })
  const sections = Object.fromEntries(RULES_TABS.flatMap((tab) => tab.anchors).map((anchor) => [anchor, section(anchor)])) as Record<RulesAnchor, RulesSection>

  it('shows the sections of the tab shown while the search is empty', () => {
    expect(shownSections(RULES_TABS, sections, 'terms', '  ')).toEqual(new Set(['terms']))
  })

  it('while the search holds text, shows a section whose rows hold it, whatever its title says', () => {
    const found: Record<RulesAnchor, RulesSection> = { ...sections, terms: section('Custom terms', 1), allow: section('Allow list', 0) }
    expect(shownSections(RULES_TABS, found, 'protection', 'Aurora')).toEqual(new Set(['terms']))
    // A title the search holds shows its section, rows found or not.
    const titled: Record<RulesAnchor, RulesSection> = { ...sections, allow: section('Allow list', 0) }
    expect(shownSections(RULES_TABS, titled, 'protection', 'allow')).toEqual(new Set(['allow']))
  })
})
