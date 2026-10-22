import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { Term } from '../api/types'
import i18n from '../i18n'
import { termRows } from '../lib/ruleTable'
import { RulePanel, RulePanelContent } from './RulePanel'
import { RuleTable } from './RuleTable'

// The table of a list of rules and the panel a row opens in, as they render. Searching, selecting, paging and saving in
// a browser are in scripts/ui-checks.mjs.

const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim()

const term = (value: string): Term => ({ value, type: 'CLIENT', case_sensitive: false, whole_word: true })

function table(terms: Term[], search = '') {
  return renderToStaticMarkup(
    h(RuleTable<Term>, {
      search,
      label: 'Terms',
      rows: termRows({ global: terms }),
      columns: [
        { id: 'value', header: 'Term', text: (row) => row.item.value, sortable: true },
        { id: 'type', header: 'Type', text: (row) => row.item.type, filter: true },
      ],
      filters: [{ column: 'type', label: 'Type', options: [{ value: 'CLIENT', label: 'CLIENT' }] }],
      actions: [{ id: 'delete', label: 'Delete', danger: true, run: async () => undefined }],
      onOpen: () => undefined,
      onAdd: () => undefined,
      addLabel: 'Add term',
      empty: 'No terms.',
    }),
  )
}

/** The rows of the table's body. */
const bodyRows = (html: string) => [...(/<tbody>(.*?)<\/tbody>/s.exec(html)?.[1] ?? '').matchAll(/<tr\b/g)].length

describe('RuleTable', () => {
  it('a table shows a row per item on the page and the count', () => {
    const three = table([term('Project Aurora'), term('Whitfield & Co'), term('Orion Labs')])
    expect(bodyRows(three)).toBe(3)
    expect(text(three)).toContain(i18n.t('rules.table.count', { count: 3 }))
    expect(text(three)).toContain('Whitfield & Co')
    const many = table(Array.from({ length: 120 }, (_, i) => term(`Client ${i}`)))
    expect(bodyRows(many)).toBe(50)
    expect(text(many)).toContain(i18n.t('rules.table.count', { count: 120 }))
    expect(many).toContain(`aria-label="${i18n.t('pagination.label')}"`)
    // Each row says what it is, and its checkbox which row it selects.
    expect(many).toContain(`aria-label="${i18n.t('rules.table.select', { row: 'Client 0' })}"`)
    expect(many).toContain(`aria-label="${i18n.t('rules.table.selectPage')}"`)
  })

  it('says the list is empty', () => {
    const none = table([])
    expect(bodyRows(none)).toBe(0)
    expect(text(none)).toContain('No terms.')
  })

  it('a long value is truncated with its whole text in the title', () => {
    const long = `Client ${'x'.repeat(8192)}`
    const html = table([term(long)])
    const cell = new RegExp(`<span[^>]*class="([^"]*)"[^>]*title="${long}"[^>]*>${long}</span>`).exec(html)
    expect(cell).not.toBeNull()
    expect(cell?.[1]).toContain('truncate')
  })
})

// The panel is put at the end of the page's body (a portal, which renders in a browser only): its content is tested here.
describe('RulePanel', () => {
  const panel = (props: Partial<Parameters<typeof RulePanelContent>[0]> = {}) =>
    props.open === false
      ? renderToStaticMarkup(h(RulePanel, { open: false, title: '', dirty: false, busy: false, onClose: () => undefined }))
      : renderToStaticMarkup(
          h(
            RulePanelContent,
            { open: true, title: 'Project Aurora', dirty: false, busy: false, onSave: () => undefined, onClose: () => undefined, ...props },
            h('p', null, 'the fields'),
          ),
        )
  const buttons = (html: string) => [...html.matchAll(/<button\b[^>]*>(.*?)<\/button>/g)].map((match) => text(match[1] ?? ''))

  it('is a dialog named by its title, with its fields, a Save and a Close', () => {
    const html = panel()
    expect(html).toMatch(/role="dialog"/)
    expect(text(html)).toContain('Project Aurora')
    expect(text(html)).toContain('the fields')
    expect(buttons(html)).toContain(i18n.t('rules.panel.save'))
    expect(html).toContain(`aria-label="${i18n.t('rules.panel.close')}"`)
  })

  it('the panel says it is read only and has no Save when readOnly is given', () => {
    const html = panel({ readOnly: h('span', null, 'In your global rules.') })
    expect(text(html)).toContain('In your global rules.')
    expect(buttons(html)).not.toContain(i18n.t('rules.panel.save'))
    expect(html).toMatch(/<fieldset disabled=""/)
  })

  it('shows nothing while closed', () => {
    expect(panel({ open: false })).toBe('')
  })

  it('is kept, out of sight, while its tab is not the one shown', () => {
    expect(panel({ hidden: true })).toMatch(/<div[^>]*role="dialog"[^>]*hidden=""/)
    expect(panel()).not.toMatch(/<div[^>]*role="dialog"[^>]*hidden=""/)
  })
})

describe('RuleTable with the search at the top of Rules', () => {
  it('shows the rows that search finds, and no search of its own while it holds text', () => {
    const html = table([term('Project Aurora'), term('Whitfield & Co'), term('Orion Labs')], 'whitfield')
    expect(bodyRows(html)).toBe(1)
    expect(text(html)).toContain('Whitfield & Co')
    expect(text(html)).toContain(i18n.t('rules.table.matched', { matched: 1, count: 3 }))
    expect(html).not.toContain('type="search"')
    expect(table([term('Project Aurora')])).toContain('type="search"')
  })
})
