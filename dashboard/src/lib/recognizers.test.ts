import { describe, expect, it } from 'vitest'
import { draftProblems, EMPTY_DRAFT, fromDraft, stateOf, toDraft } from './recognizers'

const badge = {
  name: 'employee-id',
  description: 'Badge numbers',
  pattern: '\\b[A-Z]{2}-\\d{6}\\b',
  context: { words: ['badge'] },
  examples: { match: ['badge AB-123456'], no_match: ['AB-12345'] },
}

describe('recognizer drafts', () => {
  it('round-trips an entry through the form, writing only what differs from the defaults', () => {
    const { draft, editable } = toDraft(badge)
    expect(editable).toBe(true)
    expect(fromDraft(draft)).toEqual(badge)
    const weighted = { name: 'n', pattern: 'x', checksum: { algorithm: 'weighted' as const, weights: [9, 8, -1], modulus: 11 } }
    expect(fromDraft(toDraft(weighted).draft)).toEqual(weighted)
    const secret = { name: 's', pattern: 'x', class: 'secret' as const, policy: 'redact' as const, surrogate: { keep: 3 }, enabled: false }
    expect(fromDraft(toDraft(secret).draft)).toEqual(secret)
  })

  it('marks entries the form cannot show as edited by hand', () => {
    expect(toDraft({ ...badge, colour: 'red' }).editable).toBe(false)
    expect(toDraft({ ...badge, context: { words: 'badge' } } as never).editable).toBe(false)
    expect(toDraft({ ...badge, checksum: 42 } as never).editable).toBe(false)
  })

  it('finds the problems the form can tell', () => {
    expect(draftProblems({ ...EMPTY_DRAFT, name: 'ok', pattern: 'x' }, [])).toEqual([])
    expect(draftProblems({ ...EMPTY_DRAFT, name: 'Bad Name', pattern: '' }, [])).toEqual(['name', 'pattern'])
    expect(draftProblems({ ...EMPTY_DRAFT, name: 'ok', pattern: 'x' }, ['ok'])).toEqual(['nameTaken'])
    expect(draftProblems({ ...EMPTY_DRAFT, name: 'ok', pattern: 'x', checksum: 'weighted', weights: '', modulus: '1' }, [])).toEqual([
      'weights',
      'modulus',
    ])
  })

  it('tells saved, failing and unsaved recognizers apart', () => {
    const status = { name: 'employee-id', state: 'active' as const, errors: [], warnings: [] }
    expect(stateOf(badge, [badge], [status])).toBe('active')
    expect(stateOf({ ...badge, description: 'changed' }, [badge], [status])).toBe('unsaved')
    expect(stateOf(badge, [badge], [{ ...status, warnings: ['examples.match ...'] }])).toBe('failing')
    expect(stateOf(badge, [badge], [{ ...status, state: 'error' as const }])).toBe('error')
  })
})
