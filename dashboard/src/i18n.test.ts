import { describe, expect, it } from 'vitest'
import { LANGUAGE_NAMES, LANGUAGES, pickLanguage } from './i18n'

describe('the languages', () => {
  it('are English and the four translations, each with its own name', () => {
    expect(LANGUAGES).toEqual(['en', 'it', 'de', 'fr', 'es'])
    expect(LANGUAGES.map((lng) => LANGUAGE_NAMES[lng])).toEqual(['English', 'Italiano', 'Deutsch', 'Français', 'Español'])
  })
})

describe('pickLanguage', () => {
  it("takes the reader's choice first", () => {
    expect(pickLanguage('de', ['it-IT', 'en'])).toBe('de')
  })

  it('takes the browser when there is no choice, by exact tag or by its language', () => {
    expect(pickLanguage(null, ['fr-CA', 'en'])).toBe('fr')
    expect(pickLanguage(undefined, ['pt-BR', 'es-419'])).toBe('es')
  })

  it('ignores a stored value that is not a language', () => {
    expect(pickLanguage('klingon', ['it-IT'])).toBe('it')
    expect(pickLanguage(42, [])).toBe('en')
  })

  it('falls back to English for a language Latet does not have', () => {
    expect(pickLanguage(null, ['pt-BR', 'ja'])).toBe('en')
  })
})
