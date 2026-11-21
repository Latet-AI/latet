import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { afterAll, describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { SaveRow } from '../../components/SaveBar'
import i18n from '../../i18n'
import { lockLines, locksOf, type PolicyView } from '../lib/policy'
import { FloorRefused, Locked, PolicyNotice } from './Locked'

// What a person sees of the company's policy, in each language: the lock under a section, the refusal of a save, and
// that no value of the company's is in any of it.

const view: PolicyView = {
  floors: [
    {
      source: 'server',
      origin: 'https://latet.acme.lan',
      received: 1791191520,
      exceptions: 'company',
      rules: { latet_rules: 1, profile: 'max', terms: 2, allow_values: 1, policies: { IBAN: 'redact' } },
      notices: 0,
    },
  ],
  below: ['profile: from max to balanced'],
  problems: [],
}
const lines = lockLines(locksOf(view)!)
const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;|&#39;/g, "'").replace(/\s+/g, ' ').trim()
const LANGUAGES = ['en', 'it', 'de', 'fr', 'es']

afterAll(() => void i18n.changeLanguage('en'))

describe('Locked', () => {
  it('says what the company sets under a section, with its counts, in every language', async () => {
    for (const lng of LANGUAGES) {
      await i18n.changeLanguage(lng)
      const words = text(renderToStaticMarkup(h(Locked, { lines: lines.terms })))
      expect(words).toContain(i18n.t('ee.policy.setBy'))
      expect(words).toContain(i18n.t('ee.policy.lock.terms', { count: 2 }))
      expect(words).toContain('2')
    }
  })

  it('shows nothing for a section the policy does not lock', () => {
    expect(renderToStaticMarkup(h(Locked, { lines: lines.ner }))).toBe('')
    expect(renderToStaticMarkup(h(Locked, { lines: undefined }))).toBe('')
  })

  it('names the profile as Rules does', async () => {
    await i18n.changeLanguage('it')
    expect(text(renderToStaticMarkup(h(Locked, { lines: lines.profile })))).toContain(`Almeno ${i18n.t('rules.profile.max.label')}`)
  })
})

describe('PolicyNotice', () => {
  it('says the policy is in force and which of your settings are below it, with the way to the page when it is yours', async () => {
    await i18n.changeLanguage('en')
    const html = renderToStaticMarkup(h(MemoryRouter, null, h(PolicyNotice, { view, linked: true })))
    expect(text(html)).toContain(i18n.t('ee.policy.inForce'))
    expect(text(html)).toContain('profile: from max to balanced')
    expect(html).toContain('href="/policy"')
    expect(renderToStaticMarkup(h(MemoryRouter, null, h(PolicyNotice, { view, linked: false })))).not.toContain('href="/policy"')
    expect(renderToStaticMarkup(h(MemoryRouter, null, h(PolicyNotice, { view: { ...view, floors: [] }, linked: true })))).toBe('')
  })
})

describe('PolicyNotice without a floor', () => {
  it("says why a policy is not in force or cannot be read, in the daemon's fixed words", async () => {
    await i18n.changeLanguage('en')
    const lapsed: PolicyView = { floors: [], below: [], problems: ["the server's policy is not in force: its license does not list locked rules"] }
    const html = renderToStaticMarkup(h(MemoryRouter, null, h(PolicyNotice, { view: lapsed, linked: false })))
    expect(text(html)).toContain('is not in force')
    expect(text(html)).not.toContain(i18n.t('ee.policy.inForce'))
  })
})

describe('a save the policy refused', () => {
  const refusal = new ApiError(409, 'Set by your company, not changed: profile: from max to balanced', [], {
    error: 'Set by your company, not changed: profile: from max to balanced',
    floor: ['profile: from max to balanced'],
  })

  it('FloorRefused names the settings, in every language', async () => {
    for (const lng of LANGUAGES) {
      await i18n.changeLanguage(lng)
      const words = text(renderToStaticMarkup(h(FloorRefused, { reasons: ['profile: from max to balanced'] })))
      expect(words).toContain(i18n.t('ee.policy.refused'))
      expect(words).toContain('profile: from max to balanced')
      expect(words).toContain(i18n.t('ee.policy.refusedHint'))
    }
  })

  it('the save bar says it before any other 409, and a 409 without `floor` is as it was', async () => {
    await i18n.changeLanguage('en')
    const row = (error: unknown, conflict?: string) =>
      text(
        renderToStaticMarkup(
          h(SaveRow, { dirty: true, saving: false, error, conflict, saveLabel: 'Save', onDiscard: () => undefined, onSave: () => undefined }),
        ),
      )
    expect(row(refusal, 'The file changed since')).toContain(i18n.t('ee.policy.refused'))
    expect(row(refusal, 'The file changed since')).not.toContain('The file changed since')
    expect(row(new ApiError(409, 'stale'), 'The file changed since')).toContain('The file changed since')
  })
})
