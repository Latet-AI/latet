import { describe, expect, it } from 'vitest'
import type { RecognizerEntry } from '../../api/types'
import i18n from '../../i18n'
import type { RuleRow, RuleSource } from '../../lib/ruleTable'
import type { RulesScope } from './editing'
import { recognizersSpec } from './recognizers'
import type { Translate } from './RuleList'

// A recognizer with keys its panel cannot show is read only there, and says in which file it is changed by hand.

const t = i18n.t.bind(i18n) as Translate
const scope = { kind: 'global', settings: {} } as unknown as RulesScope
const handEdited: RecognizerEntry = { name: 'badge-id', pattern: 'B-[0-9]{6}', comment_by_hand: 'kept' }
const row = (source: RuleSource): RuleRow<RecognizerEntry> => ({ key: `${source}:recognizer:badge-id`, source, item: handEdited })

describe('a recognizer edited by hand', () => {
  it('says to change it in the file it is in: config.yaml, or the project latet.yaml', () => {
    const readOnly = recognizersSpec(t, (words) => words, scope).readOnly!
    expect(readOnly(row('global'))).toBe(i18n.t('rules.recognizers.handEdited'))
    expect(readOnly(row('personal'))).toBe(i18n.t('rules.recognizers.handEdited'))
    expect(readOnly(row('repo'))).toBe(i18n.t('rules.recognizers.handEditedRepo'))
  })
})
