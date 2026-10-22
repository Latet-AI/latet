import { createElement as h, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it } from 'vitest'
import type { Me } from '../api/types'
import i18n from '../i18n'
import { MeContext, type MeState } from '../lib/me'
import { computer } from '../test/eop35-fixtures'
import { FlagList } from './badges'
import { DocsPageLink, DocsSectionLink, DocsWhyLink, TitleWithDocs } from './DocsLinks'
import { RulePanelContent } from './RulePanel'
import { Section } from './rules'
import { PageHeader } from './ui'

// The three ways the dashboard opens its documentation: a page's "Documentation", a section's "?", a notice's "Why".
// Each opens the section it names in a new tab, in the dashboard's language, from this install's own copy when it has
// one, and names where it leads to a screen reader.

function rendered(element: ReactElement, docs: boolean | undefined): string {
  const me: Me | undefined = docs === undefined ? undefined : { ...computer, docs }
  const state: MeState = { me, unreachable: false, reload: () => undefined, customTypes: new Set(), reloadConfig: () => undefined }
  return renderToStaticMarkup(h(MeContext.Provider, { value: state }, element))
}

/** The attributes of the first link of the markup, and its text without tags. */
function link(html: string): { href?: string; label?: string; title?: string; target?: string; rel?: string; text: string } {
  const tag = /<a\b([^>]*)>([\s\S]*?)<\/a>/.exec(html)
  const attr = (name: string) => tag && new RegExp(`\\b${name}="([^"]*)"`).exec(tag[1] ?? '')?.[1]?.replace(/&amp;/g, '&')
  return {
    href: attr('href') ?? undefined,
    label: attr('aria-label') ?? undefined,
    title: attr('title') ?? undefined,
    target: attr('target') ?? undefined,
    rel: attr('rel') ?? undefined,
    text: (tag?.[2] ?? '').replace(/<[^>]*aria-hidden="true"[^>]*>[\s\S]*?<\/[a-z]+>/g, '').replace(/<[^>]+>/g, '').trim(),
  }
}

afterEach(async () => {
  await i18n.changeLanguage('en')
})

describe('DocsPageLink', () => {
  it("opens the page's documentation in a new tab, named Documentation", () => {
    const found = link(rendered(h(DocsPageLink, { target: 'section.allow' }), true))
    expect(found).toMatchObject({
      href: '/_latet/docs/concepts/profiles/#allow-lists',
      target: '_blank',
      rel: 'noreferrer',
      text: 'Documentation',
    })
  })

  it("opens docs.latet.ai when this install has no documentation, or before it says", () => {
    expect(link(rendered(h(DocsPageLink, { target: 'section.allow' }), false)).href).toBe(
      'https://docs.latet.ai/concepts/profiles/#allow-lists',
    )
    expect(link(rendered(h(DocsPageLink, { target: 'section.allow' }), undefined)).href).toBe(
      'https://docs.latet.ai/concepts/profiles/#allow-lists',
    )
  })

  it("opens the documentation in the dashboard's language", async () => {
    await i18n.changeLanguage('it')
    const found = link(rendered(h(DocsPageLink, { target: 'section.allow' }), true))
    expect(found.href).toBe('/_latet/docs/it/concepts/profiles/#valori-consentiti')
    expect(found.text).toBe('Documentazione')
  })
})

describe('DocsSectionLink', () => {
  it('is a ? that says which section of the documentation it opens', () => {
    const found = link(rendered(h(DocsSectionLink, { target: 'section.allow', section: 'Allowed values' }), true))
    expect(found).toMatchObject({
      href: '/_latet/docs/concepts/profiles/#allow-lists',
      label: 'Documentation: Allowed values',
      title: 'Documentation: Allowed values',
      target: '_blank',
      rel: 'noreferrer',
    })
  })

  it('says it in the dashboard’s language', async () => {
    await i18n.changeLanguage('de')
    const found = link(rendered(h(DocsSectionLink, { target: 'section.allow', section: 'Zulässige Werte' }), true))
    expect(found.label).toBe('Dokumentation: Zulässige Werte')
    expect(found.href).toBe('/_latet/docs/de/concepts/profiles/#zulassungslisten')
  })
})

describe('DocsWhyLink', () => {
  it('is a Why that opens the section explaining what happened', () => {
    const found = link(rendered(h(DocsWhyLink, { target: 'section.allow' }), true))
    expect(found).toMatchObject({ href: '/_latet/docs/concepts/profiles/#allow-lists', target: '_blank', rel: 'noreferrer', text: 'Why' })
  })

  it('says what it explains to a screen reader, beside others of its kind', () => {
    const found = link(rendered(h(DocsWhyLink, { target: 'why.withheld', about: 'Withheld' }), true))
    expect(found).toMatchObject({ label: 'Why: Withheld', text: 'Why', href: '/_latet/docs/troubleshooting/#a-tool-output-was-withheld' })
  })
})

describe('TitleWithDocs', () => {
  it('is a sub-section’s title with the ? of its documentation beside it, named after it', () => {
    const html = rendered(h(TitleWithDocs, { title: 'Dump guard', docs: 'section.dumpGuard' }), true)
    expect(html).toMatch(/<h3 class="[^"]*">Dump guard<\/h3>/)
    expect(link(html)).toMatchObject({
      href: '/_latet/docs/concepts/production-data/#the-dump-guard',
      label: 'Documentation: Dump guard',
    })
  })
})

describe('Section', () => {
  it('has the ? of its documentation beside its title, named after it', () => {
    const list = h('p', null, 'the list')
    const html = rendered(h(Section, { title: 'Allowed values', docs: 'section.allow', children: list }), true)
    const heading = /<h2\b[\s\S]*?<\/h2>[\s\S]*?(?=<p>the list)/.exec(html)?.[0] ?? ''
    expect(link(heading)).toMatchObject({ href: '/_latet/docs/concepts/profiles/#allow-lists', label: 'Documentation: Allowed values' })
    expect(rendered(h(Section, { title: 'Allowed values', children: list }), true)).not.toContain('<a')
  })
})

describe('RulePanel', () => {
  it('has the ? of its list at its top, named after the list, not after the row it shows', () => {
    const panel = h(RulePanelContent, {
      open: true,
      title: 'acme-dev',
      dirty: false,
      busy: false,
      onClose: () => undefined,
      docs: { target: 'section.allow', section: 'Allowed values' },
    })
    const found = link(rendered(panel, true))
    expect(found).toMatchObject({ href: '/_latet/docs/concepts/profiles/#allow-lists', label: 'Documentation: Allowed values' })
  })
})

describe('FlagList', () => {
  it('says why beside each flag that tells what happened, on the request’s own page', () => {
    const html = rendered(h(FlagList, { flags: ['withheld', 'blocked', 'error', 'history'], why: true }), true)
    const labels = [...html.matchAll(/<a\b[^>]*aria-label="([^"]*)"/g)].map((match) => match[1])
    expect(labels).toEqual(['Why: Withheld', 'Why: From the history'])
    expect(html).toContain('href="/_latet/docs/troubleshooting/#a-tool-output-was-withheld"')
  })

  it('says nothing more in a list of requests', () => {
    expect(rendered(h(FlagList, { flags: ['withheld', 'history'] }), true)).not.toContain('<a')
  })
})

describe('PageHeader', () => {
  it('has the Documentation link of its page at the top right, on its title’s line, whatever its actions', () => {
    const html = rendered(
      h(PageHeader, { title: 'Rules', description: 'Your rules.', docs: 'section.allow', actions: h('button', null, 'Export rules') }),
      true,
    )
    // The title, then the link: before the description and the actions, which wrap under it as they need.
    expect(html.indexOf('</h1>')).toBeLessThan(html.indexOf('<a'))
    expect(html.indexOf('<a')).toBeLessThan(html.indexOf('Your rules.'))
    expect(html.indexOf('Your rules.')).toBeLessThan(html.indexOf('Export rules'))
    expect(link(html.slice(html.indexOf('<a'))).href).toBe('/_latet/docs/concepts/profiles/#allow-lists')
  })

  it('has none when the page names no documentation', () => {
    expect(rendered(h(PageHeader, { title: 'Rules' }), true)).not.toContain('<a')
  })
})
