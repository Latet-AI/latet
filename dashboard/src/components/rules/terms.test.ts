import { describe, expect, it } from 'vitest'
import type { Term } from '../../api/types'
import i18n from '../../i18n'
import type { Translate } from './RuleList'
import { termsSpec } from './terms'

// A term's panel: two terms of the same value and different types (a client list imported over a term of yours) are
// two terms, each saved from its panel, as the daemon tells them apart.

const t = i18n.t.bind(i18n) as Translate
const term = (value: string, type: string): Term => ({ value, type, case_sensitive: false, whole_word: true })
const check = (draft: Term, others: Term[]) => termsSpec(t, String).check(draft, others)

describe("a term's panel", () => {
  it('saves a value the list holds with another type', () => {
    expect(check(term('Acme', 'CLIENT'), [term('Acme', 'CUSTOM')]).ok).toBe(true)
  })

  it('says a value is in the list already with its type, an empty type being CUSTOM', () => {
    expect(check(term('Acme', 'CUSTOM'), [term('Acme', 'CUSTOM')]).ok).toBe(false)
    expect(check(term('Acme', ''), [term('Acme', 'CUSTOM')]).ok).toBe(false)
  })
})
