import { describe, expect, it } from 'vitest'
import anchors from './docsAnchors.json'
import { docsBundled, docsHref, DOCS_TARGETS, probeDocs } from './docsLinks'
import links from './docsLinks.json'

// Each link of the dashboard to the documentation opens the section it names, in the reader's language: the copy this
// install serves at /_latet/docs/ (its own version), or docs.latet.ai when the install has none.

describe('whether this install serves its docs', () => {
  it('takes what /me says once it answers, and the probe of /_latet/docs/ until then', () => {
    expect(docsBundled({ docs: true }, false)).toBe(true)
    expect(docsBundled({ docs: false }, true)).toBe(false)
    expect(docsBundled(null, true)).toBe(true) // the sign-in page, or before /me answers
    expect(docsBundled(null, null)).toBe(false) // nothing known yet: the public site
  })

  it('reads what /health says, public, and nothing when it cannot be asked', async () => {
    expect(await probeDocs(async () => ({ docs: true }))).toBe(true)
    expect(await probeDocs(async () => ({ docs: false }))).toBe(false)
    expect(await probeDocs(async () => ({}))).toBe(false) // an older daemon
    expect(await probeDocs(async () => Promise.reject(new TypeError('offline')))).toBe(false)
  })
})

describe('docsHref', () => {
  it('builds the local link in each language with its anchor', () => {
    expect(docsHref('section.allow', 'en', true)).toBe('/_latet/docs/concepts/profiles/#allow-lists')
    expect(docsHref('section.allow', 'it', true)).toBe('/_latet/docs/it/concepts/profiles/#valori-consentiti')
    expect(docsHref('section.allow', 'de', true)).toBe('/_latet/docs/de/concepts/profiles/#zulassungslisten')
    expect(docsHref('section.allow', 'fr', true)).toBe('/_latet/docs/fr/concepts/profiles/#listes-dautorisation')
    expect(docsHref('section.allow', 'es', true)).toBe('/_latet/docs/es/concepts/profiles/#listas-de-permitidos')
  })

  it('opens a page at its top when the link names no section', () => {
    expect(docsHref('page.tutorial', 'en', true)).toBe('/_latet/docs/getting-started/tutorial/')
    expect(docsHref('page.tutorial', 'fr', true)).toBe('/_latet/docs/fr/getting-started/tutorial/')
  })

  it('falls back to the public site without the bundle', () => {
    expect(docsHref('section.allow', 'en', false)).toBe('https://docs.latet.ai/concepts/profiles/#allow-lists')
    expect(docsHref('section.allow', 'it', false)).toBe('https://docs.latet.ai/it/concepts/profiles/#valori-consentiti')
    expect(docsHref('page.tutorial', 'es', false)).toBe('https://docs.latet.ai/es/getting-started/tutorial/')
  })

  it('opens English for a language the documentation does not have', () => {
    expect(docsHref('section.allow', 'pt', true)).toBe('/_latet/docs/concepts/profiles/#allow-lists')
    expect(docsHref('section.allow', undefined, true)).toBe('/_latet/docs/concepts/profiles/#allow-lists')
    expect(docsHref('section.allow', 'it-IT', false)).toBe('https://docs.latet.ai/concepts/profiles/#allow-lists')
  })
})

describe('the link table', () => {
  it('has every target with its anchor in every language', () => {
    const translated = anchors as Record<string, Record<string, Record<string, string>>>
    const missing: string[] = []
    for (const [target, { page, anchor }] of Object.entries(links)) {
      if (anchor === null) continue
      for (const lang of ['it', 'de', 'fr', 'es']) {
        if (!translated[lang]?.[page]?.[anchor]) missing.push(`${target} (${lang})`)
      }
    }
    expect(missing).toEqual([])
  })

  it('names pages as the documentation builds them: a folder, never a file or an address', () => {
    for (const { page, anchor } of Object.values(links)) {
      expect(page).toMatch(/^([a-z0-9-]+\/)+$/)
      if (anchor !== null) expect(anchor).toMatch(/^[a-z0-9-]+$/)
    }
    expect(DOCS_TARGETS).toEqual(Object.keys(links))
  })
})
