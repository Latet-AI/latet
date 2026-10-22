import { describe, expect, it } from 'vitest'
import { importable } from './transfer'

describe('importing rules', () => {
  const report = { added: [], skipped: ['nothing new: these rules are yours already'], held: ['values never to mask: ACME'], notices: [] }

  it('changes something only when it adds protection, or when the rest is chosen too', () => {
    expect(importable(report, false)).toBe(false)
    expect(importable(report, true)).toBe(true)
    expect(importable({ ...report, added: ['terms: Banca Ligure'] }, false)).toBe(true)
    expect(importable({ ...report, held: [] }, true)).toBe(false)
  })
})
