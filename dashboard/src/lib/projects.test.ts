import { describe, expect, it } from 'vitest'
import type { ProjectDetail } from '../api/types'
import {
  draftOf,
  personalDraftOf,
  personalOf,
  protectionOf,
  ruleLayer,
  samePersonal,
  sameSettings,
  setProtection,
  settingsOf,
  withValue,
} from './projects'

describe('project drafts', () => {
  it('saves only what is set, and for a repository file only what it may set', () => {
    const draft = draftOf({ profile: 'max', id_regions: ['DE'], allow_values: ['x'] })
    expect(settingsOf(draft, false)).toEqual({ profile: 'max', id_regions: ['DE'], allow_values: ['x'] })
    expect(settingsOf(draft, true)).toEqual({ profile: 'max', id_regions: ['DE'] })
    expect(settingsOf(draftOf(null), true)).toEqual({})
  })

  it('saves the files and the web addresses strict data mode covers, a repository file too, its tools only yours', () => {
    const draft = draftOf({ data_files: ['*.json'], data_hosts: ['*.salesforce.com'], data_tools: ['mcp__crm__*'] })
    expect(settingsOf(draft, false)).toEqual({ data_files: ['*.json'], data_hosts: ['*.salesforce.com'], data_tools: ['mcp__crm__*'] })
    expect(settingsOf(draft, true)).toEqual({ data_files: ['*.json'], data_hosts: ['*.salesforce.com'] })
  })

  it('saves back whether the text in images is read, which no editor sets: a save never drops it', () => {
    expect(settingsOf(draftOf({ ocr: 'off', terms: [] }), false)).toEqual({ ocr: 'off' })
    expect(settingsOf(draftOf({ ocr: 'auto' }), true)).toEqual({ ocr: 'auto' })
    expect(personalOf(personalDraftOf({ ocr: 'off', allow_values: ['billing-dev'] }))).toEqual({ ocr: 'off', allow_values: ['billing-dev'] })
    expect(settingsOf(draftOf({}), false)).toEqual({})
  })

  it('compares drafts by what they would save', () => {
    expect(sameSettings(draftOf({ pii: [] }), draftOf({}))).toBe(true)
    expect(sameSettings(draftOf({ pii: ['IBAN'] }), draftOf({}))).toBe(false)
  })
})

describe('protection', () => {
  it('maps your settings to on, secrets only or off and back', () => {
    const on = personalDraftOf(null)
    expect(protectionOf(on)).toBe('on')
    const off = setProtection(on, 'off')
    expect(protectionOf(off)).toBe('off')
    expect(personalOf(off)).toEqual({ enabled: false })
    const secrets = setProtection(off, 'secrets')
    expect([protectionOf(secrets), secrets.enabled, secrets.profile]).toEqual(['secrets', true, 'secrets'])
    expect(setProtection(secrets, 'on').profile).toBeNull()
    expect(setProtection(personalDraftOf({ profile: 'max' }), 'on').profile).toBe('max') // a stricter profile stays
  })
})

describe('ruleLayer', () => {
  const project = {
    repo: { path: null, exists: true, settings: { recognizers: [{ name: 'badge' }], terms: [] }, tag: '', writable: true },
    personal: { terms: [{ value: 'Acme', type: 'CLIENT', case_sensitive: false, whole_word: true }] },
  } as unknown as ProjectDetail

  it('says which of the project settings a rule comes from', () => {
    expect(ruleLayer('recognizer:badge', project)).toBe('repo')
    expect(ruleLayer('terms:client', project)).toBe('personal')
    expect(ruleLayer('pii:email', project)).toBeNull()
    expect(ruleLayer('recognizer:badge', null)).toBeNull()
  })

  it('names the layer whose max profile turned strict data mode on, when neither sets it', () => {
    const layers = (settings: object, personal: object | null) =>
      ({ repo: { path: null, exists: true, settings, tag: '', writable: true }, personal }) as unknown as ProjectDetail
    expect(ruleLayer('strict:stato', layers({ profile: 'max' }, null))).toBe('repo')
    expect(ruleLayer('strict:stato', layers({}, { profile: 'max' }))).toBe('personal')
    expect(ruleLayer('strict:stato', layers({ profile: 'max' }, { profile: 'balanced' }))).toBeNull() // yours wins: the global rules
    expect(ruleLayer('strict:stato', layers({ profile: 'max', data: 'strict' }, null))).toBe('repo')
    expect(ruleLayer('strict:stato', layers({}, null))).toBeNull()
  })
})

describe('a value for one project', () => {
  it('adds it to the project terms, out of its values never to mask', () => {
    const personal = { profile: 'max' as const, allow_values: ['Ugo', 'x'] }
    const masked = withValue(personal, { kind: 'mask', value: 'Ugo', type: 'person' })
    expect(masked).toEqual({
      profile: 'max',
      allow_values: ['x'],
      terms: [{ value: 'Ugo', type: 'PERSON', case_sensitive: false, whole_word: true }],
    })
    expect(withValue(masked, { kind: 'mask', value: 'Ugo', type: 'PERSON' }).terms).toHaveLength(1)
    expect(withValue({}, { kind: 'mask', value: 'Ugo', type: 'PERSON' })).not.toHaveProperty('allow_values')
  })

  it('adds it to the values never to mask once', () => {
    const allowed = withValue({}, { kind: 'allow', value: 'Bertani' })
    expect(allowed).toEqual({ allow_values: ['Bertani'] })
    expect(withValue(allowed, { kind: 'allow', value: 'Bertani' })).toBe(allowed)
  })
})

describe('your own settings for a project', () => {
  it('save your guidance to the reviewer, and count it in what changed', () => {
    const base = personalDraftOf({ live_review: true })
    expect(base.review_guidance).toBeNull()
    const written = { ...base, review_guidance: ' PRJ- codes are internal projects. ' }
    expect(personalOf(written)).toEqual({ live_review: true, review_guidance: 'PRJ- codes are internal projects.' })
    expect(samePersonal(written, base)).toBe(false)
    expect(samePersonal({ ...base, review_guidance: '  ' }, base)).toBe(true) // empty is none
    expect(samePersonal({ ...base, enabled: false }, base)).toBe(false)
    expect(personalDraftOf(personalOf(written)).review_guidance).toBe('PRJ- codes are internal projects.')
  })
})
