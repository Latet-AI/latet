import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import i18n from '../i18n'
import { tabIds, Tabs } from './Tabs'

// Each tab as rendered: its attributes and its visible and spoken text.
function tabsOf(html: string): { attributes: string; text: string }[] {
  return [...html.matchAll(/<button\b([^>]*)>(.*?)<\/button>/g)].map((match) => ({
    attributes: match[1] ?? '',
    text: (match[2] ?? '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(),
  }))
}

const attribute = (attributes: string, name: string) => new RegExp(`\\b${name}="([^"]*)"`).exec(attributes)?.[1]

describe('Tabs', () => {
  const render = (active: string) =>
    renderToStaticMarkup(
      h(Tabs, {
        label: 'Sections of the rules',
        idPrefix: 'rules',
        active,
        onChange: () => undefined,
        tabs: [
          { id: 'protection', label: 'Protection' },
          { id: 'terms', label: 'Terms and recognizers', dirty: true },
          { id: 'texts', label: 'Texts', count: 2 },
        ],
      }),
    )

  it('is a tablist named by its label, with a tab each', () => {
    const html = render('terms')
    expect(html).toMatch(/^<div[^>]*role="tablist"[^>]*>/)
    expect(attribute(html.slice(0, html.indexOf('>')), 'aria-label')).toBe('Sections of the rules')
    const tabs = tabsOf(html)
    expect(tabs).toHaveLength(3)
    for (const tab of tabs) {
      expect(attribute(tab.attributes, 'role')).toBe('tab')
      expect(attribute(tab.attributes, 'type')).toBe('button')
      expect(attribute(tab.attributes, 'class')?.split(/\s+/)).toContain('tab')
    }
  })

  it('selects the active tab, the only one reached with Tab (the arrow keys move between them)', () => {
    const tabs = tabsOf(render('terms'))
    expect(tabs.map((tab) => attribute(tab.attributes, 'aria-selected'))).toEqual(['false', 'true', 'false'])
    expect(tabs.map((tab) => attribute(tab.attributes, 'tabindex'))).toEqual(['-1', '0', '-1'])
  })

  it('ties each tab to its panel', () => {
    const tabs = tabsOf(render('protection'))
    expect(attribute(tabs[0]?.attributes ?? '', 'id')).toBe(tabIds('rules', 'protection').tab)
    expect(attribute(tabs[0]?.attributes ?? '', 'aria-controls')).toBe(tabIds('rules', 'protection').panel)
    expect(tabIds('rules', 'terms')).toEqual({ tab: 'rules-tab-terms', panel: 'rules-panel-terms' })
  })

  it('marks a tab with unsaved changes by a dot and in words, not by colour alone', () => {
    const tabs = tabsOf(render('protection'))
    const unsaved = i18n.t('tabs.unsaved')
    expect(unsaved).not.toBe('tabs.unsaved')
    expect(tabs[1]?.text).toBe(`Terms and recognizers ${unsaved}`)
    expect(tabs[1]?.attributes).toMatch(/data-dirty=""/)
    expect(tabs[0]?.text).toBe('Protection')
    expect(tabs[0]?.attributes).not.toMatch(/data-dirty/)
  })

  it('shows a count beside the label', () => {
    expect(tabsOf(render('protection'))[2]?.text).toBe('Texts 2')
  })

  it('draws a count in the warm tone when asked (a change that lowers protection waits), else a neutral one', () => {
    const html = renderToStaticMarkup(
      h(Tabs, {
        label: 'What waits for you',
        idPrefix: 'approvals',
        active: 'texts',
        onChange: () => undefined,
        tabs: [
          { id: 'texts', label: 'Texts', count: 2 },
          { id: 'settings', label: 'Settings', count: 1, countTone: 'warm' },
        ],
      }),
    )
    const chips = [...html.matchAll(/<span\b([^>]*)>(\d+)<\/span>/g)].map((match) => ({
      count: match[2],
      tone: attribute(match[1] ?? '', 'data-tone'),
      classes: attribute(match[1] ?? '', 'class')?.split(/\s+/) ?? [],
    }))
    expect(chips.map((chip) => [chip.count, chip.tone])).toEqual([
      ['2', undefined],
      ['1', 'warm'],
    ])
    expect(chips[0]?.classes).toEqual(expect.arrayContaining(['bg-wash-2', 'text-ink-2']))
    expect(chips[1]?.classes).toEqual(expect.arrayContaining(['bg-real-low', 'text-real-ink']))
    expect(chips[1]?.classes).not.toContain('bg-wash-2')
    expect(tabsOf(html).map((tab) => tab.text)).toEqual(['Texts 2', 'Settings 1']) // what a screen reader says is the same
  })
})
