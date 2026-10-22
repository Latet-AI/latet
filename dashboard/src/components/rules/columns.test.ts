import { describe, expect, it } from 'vitest'
import i18n from '../../i18n'
import type { RuleColumn } from '../RuleTable'
import { allowSpec } from './allowed'
import { policiesSpec } from './criteria'
import type { RulesScope } from './editing'
import { recognizersSpec } from './recognizers'
import { fromColumnFor, type Translate } from './RuleList'
import { termsSpec } from './terms'

// What the search of a list reads: the columns of values (a term, a type, a name, a pattern), not those of words every
// row has (yes or no, an action, a state, where a row comes from), which the filters pick.

const t = i18n.t.bind(i18n) as Translate
const scope = { kind: 'global', settings: {} } as unknown as RulesScope
const searched = <T>(columns: RuleColumn<T>[]) => columns.filter((column) => column.searched !== false).map((column) => column.id)

describe('the search of a list', () => {
  it('reads the columns of values only', () => {
    expect(searched(termsSpec(t, String).columns)).toEqual(['value', 'type'])
    expect(searched(recognizersSpec(t, (words) => words, scope).columns)).toEqual(['name', 'type', 'finds'])
    expect(searched(allowSpec(t).columns)).toEqual(['value'])
    expect(searched(policiesSpec(t, scope).columns)).toEqual(['type'])
    expect(fromColumnFor(t, 'billing-app').searched).toBe(false)
  })
})
