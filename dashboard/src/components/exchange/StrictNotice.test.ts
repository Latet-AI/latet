import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, test } from 'vitest'
import type { ProjectDetail } from '../../api/types'
import i18n from '../../i18n'
import { StrictLine } from './StrictNotice'

// The line itself, and where it leads. When it shows (strict data mode off, a tool output the daemon reads columns
// in) is `holdsStructure` and `strictOn` (lib/columns.test.ts), and the page as it loads is in scripts/ui-checks.mjs.

const project = (personal: ProjectDetail['personal'], repo: ProjectDetail['repo']['settings'] = {}) =>
  ({ id: 'a1b2c3', name: 'billing-app', personal, repo: { settings: repo } }) as ProjectDetail

function render(of: ProjectDetail | null): string {
  return renderToStaticMarkup(h(MemoryRouter, null, h(StrictLine, { project: of })))
}

function text(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim()
}

function links(html: string): { href: string; text: string }[] {
  return [...html.matchAll(/<a\b[^>]*href="([^"]*)"[^>]*>(.*?)<\/a>/g)].map((m) => ({ href: (m[1] ?? '').replace(/&amp;/g, '&'), text: text(m[2] ?? '') }))
}

describe('StrictLine', () => {
  test('says the request holds structured values, as a note with an icon, and leads to Production data in Rules', () => {
    expect(i18n.exists('exchange.strict.notice') && i18n.exists('exchange.strict.open')).toBe(true)
    const html = render(null)
    expect(text(html)).toContain(i18n.t('exchange.strict.notice'))
    expect(links(html)[0]).toEqual({ href: '/rules#data', text: i18n.t('exchange.strict.open') })
    // Said in words, with an icon beside them: never a colour alone.
    expect(html).toMatch(/^<p\b[^>]*role="note"/)
    expect(html).toContain('<svg')
  })

  test('says why, with the section of the documentation on strict data mode in an exchange', () => {
    // Not signed in to a daemon that says it serves its documentation: docs.latet.ai.
    expect(links(render(null))[1]).toEqual({
      href: 'https://docs.latet.ai/concepts/production-data/#in-an-exchange',
      text: `${i18n.t('docs.why')} ↗`,
    })
  })

  test("leads to the project's rules when the project's own settings say `data`", () => {
    expect(links(render(project({ data: 'normal' })))[0]).toEqual({ href: '/rules?project=a1b2c3#project-data', text: i18n.t('exchange.strict.open') })
  })

  test('leads to the global rules when the project leaves `data` to them', () => {
    expect(links(render(project(null)))[0]?.href).toBe('/rules#data')
    expect(links(render(project({ profile: 'max' }, { data: 'normal' })))[0]?.href).toBe('/rules#data')
  })

  test('says it in every language', async () => {
    try {
      for (const lang of ['it', 'de', 'fr', 'es']) {
        await i18n.changeLanguage(lang)
        expect(text(render(null))).toContain(i18n.t('exchange.strict.notice'))
        expect(i18n.t('exchange.strict.notice')).not.toBe(i18n.getFixedT('en')('exchange.strict.notice'))
        expect(i18n.t('exchange.strict.open')).not.toBe(i18n.getFixedT('en')('exchange.strict.open'))
      }
    } finally {
      await i18n.changeLanguage('en')
    }
  })
})
