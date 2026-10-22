import { describe, expect, it } from 'vitest'
import kinds from '../../../tests/data/technical_kinds.json'
import { canKeep, isUnlocked, keepInClear, ruleField, strictField, technicalKind, unlocked } from './fields'

describe('strict data fields', () => {
  it('reads the field of a rule', () => {
    expect(strictField('strict:items[].sku')).toBe('items[].sku')
    expect(strictField('strict:')).toBeNull()
    expect(strictField('pii:email')).toBeNull()
    expect(strictField(null)).toBeNull()
    expect(ruleField('field:note_*')).toBe('note_*')
    expect(ruleField('field-secret:api_key')).toBeNull()
  })

  it('keeps a field in clear once, replacing a rule of the same name', () => {
    const before = [{ name: 'Stato', type: 'STATE' }, { name: 'note_*', type: 'NOTE' }]
    const after = unlocked(before, 'stato')
    expect(after).toEqual([{ name: 'stato', mask: false }, { name: 'note_*', type: 'NOTE' }]) // first: it wins
    expect(unlocked(after, 'stato')).toEqual(after)
    expect(isUnlocked(after, 'STATO')).toBe(true)
    expect(isUnlocked(after, 'note_*')).toBe(false)
    expect(isUnlocked(undefined, 'stato')).toBe(false)
  })

  it('keeps several columns in clear at once, first, each once', () => {
    const before = [{ name: 'note_*', type: 'NOTE' }, { name: 'STATO', type: 'STATE' }]
    expect(keepInClear(before, ['stato', 'creato_il', 'Stato'])).toEqual([
      { name: 'stato', mask: false },
      { name: 'creato_il', mask: false },
      { name: 'note_*', type: 'NOTE' },
    ])
  })

  it('cannot keep a column whose name has a star: it would match others', () => {
    expect(canKeep('items[].sku')).toBe(true)
    expect(canKeep('price*')).toBe(false)
  })

  it('tells technical fields from the others', () => {
    for (const { field, kind } of kinds.cases) expect(technicalKind(field), field).toBe(kind) // the same cases as the daemon's
  })
})
