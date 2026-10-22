import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { errorText } from '../components/ui'
import i18n, { LANGUAGES } from '../i18n'
import { refusedSave, refusedWords } from '../lib/refused'

// Dependency gate: ES owns the backend TermsTooLong subtype branch and no-save evidence.
// This tests only consumption of its adopted response, not that the response exists yet.
const longTerm = () => new ApiError(409, 'synthetic English diagnostic', [], {
  error: 'synthetic English diagnostic', reason: 'settings_terms_too_long',
})

describe('EOP36 reason consumed by import error paths', () => {
  it('recognizes the exact stable reason without relabeling unrelated refusals', () => {
    expect(refusedSave(longTerm())).toBe('errors.settingsTermsTooLong')
    expect(refusedSave(new ApiError(409, 'settings_terms_too_long'))).toBeNull()
    expect(refusedSave(new ApiError(409, 'too long', [], { reason: 'unrecognized_example_reason' }))).toBeNull()
    expect(refusedSave(new ApiError(409, 'stale', [], { reason: 'settings_stale' }))).toBe('errors.settingsStale')
    expect(refusedSave(new ApiError(409, 'waits', [], { reason: 'settings_change_waits' }))).toBe('errors.settingsChangeWaits')
  })

  it.each(LANGUAGES)('uses %s translation with fallback disabled through the actual shared formatter', (lng) => {
    const key = 'errors.settingsTermsTooLong'
    expect(i18n.exists(key, { lng, fallbackLng: [] })).toBe(true)
    const translate = i18n.getFixedT(lng)
    const words = refusedWords(translate, longTerm())
    expect(words).not.toBeNull()
    expect(words).not.toBe(key)
    expect(words).not.toContain('synthetic English diagnostic')
    expect(words).not.toContain('{{')
    expect(errorText(translate, longTerm())).toBe(words)
  })

  it('does not use imported text or details as a translation key or HTML', () => {
    const error = new ApiError(409, '<script>example</script>', [{ msg: 'example term content' }], {
      reason: 'settings_terms_too_long', details: [{ msg: 'example term content' }],
    })
    const words = errorText(i18n.getFixedT('en'), error)
    expect(words).not.toContain('<script>')
    expect(words).not.toContain('example term content')
  })
})
