import { describe, expect, it } from 'vitest'
import { compareLanguage, flatten, variables } from './i18n-rules.mjs'

const EN = flatten({
  app: { name: 'Latet' },
  errors: { http: 'The daemon answered {{status}}: {{message}}' },
  nav: { waiting_one: 'One text waits', waiting_other: '{{count}} texts wait' },
})

describe('variables', () => {
  it('lists the {{names}} of a text, once each, sorted', () => {
    expect(variables('{{b}} and {{a}}, {{b}}')).toEqual(['a', 'b'])
    expect(variables('none')).toEqual([])
  })
})

describe('compareLanguage', () => {
  const it_ = (overrides) => flatten({
    app: { name: 'Latet' },
    errors: { http: 'Il daemon ha risposto {{status}}: {{message}}' },
    nav: { waiting_one: 'Un testo attende', waiting_many: '{{count}} testi attendono', waiting_other: '{{count}} testi attendono' },
    ...overrides,
  })

  it('accepts a complete translation', () => {
    const result = compareLanguage(EN, it_({}), 'it')
    expect(result).toMatchObject({ missing: [], extra: [], variables: [], plurals: [] })
  })

  it('finds a key English has and the translation lacks, and the other way round', () => {
    const other = it_({ errors: {}, extra: { key: 'x' } })
    const result = compareLanguage(EN, other, 'it')
    expect(result.missing).toEqual(['errors.http'])
    expect(result.extra).toEqual(['extra.key'])
  })

  it('finds a variable a translation lost or invented', () => {
    const other = it_({ errors: { http: 'Il daemon ha risposto {{code}}' } })
    expect(compareLanguage(EN, other, 'it').variables).toEqual([
      { key: 'errors.http', en: ['message', 'status'], other: ['code'] },
    ])
  })

  it('keeps the variables of each plural form, but {{count}}, and invents none', () => {
    const en = flatten({ all_one: 'Approve the one in {{project}}', all_other: 'Approve all {{count}} in {{project}}' })
    const fine = flatten({
      all_one: 'Approva quella in {{project}}',
      all_many: 'Approva tutte in {{project}}',
      all_other: 'Approva tutte le {{count}} in {{project}}',
    })
    expect(compareLanguage(en, fine, 'it').variables).toEqual([])
    const lost = flatten({ all_one: 'Approva quella', all_many: 'Approva tutte le {{count}}', all_other: 'Approva tutte in {{project}}' })
    expect(compareLanguage(en, lost, 'it').variables).toEqual([
      { key: 'all_one', en: ['project'], other: [] },
      { key: 'all_many', en: ['count', 'project'], other: ['count'] },
    ])
    const invented = flatten({ ...fine, all_other: 'Approva tutte in {{project}} di {{agent}}' })
    expect(compareLanguage(en, invented, 'it').variables).toEqual([
      { key: 'all_other', en: ['count', 'project'], other: ['agent', 'project'] },
    ])
  })

  it('asks for every plural form the language has: many in Italian, not in German', () => {
    const noMany = it_({ nav: { waiting_one: 'Un testo', waiting_other: '{{count}} testi' } })
    expect(compareLanguage(EN, noMany, 'it').plurals).toEqual([{ key: 'nav.waiting', lacks: ['many'] }])
    const de = flatten({
      app: { name: 'Latet' },
      errors: { http: 'Der Daemon antwortete {{status}}: {{message}}' },
      nav: { waiting_one: 'Ein Text wartet', waiting_other: '{{count}} Texte warten' },
    })
    expect(compareLanguage(EN, de, 'de').plurals).toEqual([])
  })

  it('counts the strings still the same as English', () => {
    expect(compareLanguage(EN, EN, 'it').untranslated).toBe(4)
  })
})
