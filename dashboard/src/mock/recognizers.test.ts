import { describe, expect, it } from 'vitest'
import { mockTestRecognizer } from './recognizers'

describe('mockTestRecognizer', () => {
  it('leaves a match of signs alone and says why, as the daemon does', () => {
    const lines = { name: 'rule-line', pattern: '-{3,}', examples: { match: ['a --- b'] } }
    const report = mockTestRecognizer(lines, '| a |\n|---|')
    const reason = "matched '---', but it has no letter or digit: signs alone are never masked"
    expect(report.examples?.[0]).toMatchObject({ ok: false, found: [], reason })
    expect(report.try).toEqual({ found: [], spans: [], reason })
  })
})
