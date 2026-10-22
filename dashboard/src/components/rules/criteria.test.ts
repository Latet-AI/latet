import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import i18n from '../../i18n'
import type { PolicyItem, RuleRow } from '../../lib/ruleTable'
import type { RulesScope } from './editing'
import { policiesSpec } from './criteria'
import type { Translate } from './RuleList'

// A new policy's type: picked from the menu, or typed (a type Latet detects but does not list, a secret rule's).

const t = i18n.t.bind(i18n) as Translate
const scope = { kind: 'global', settings: {} } as unknown as RulesScope
const check = (type: string, others: PolicyItem[] = []) => policiesSpec(t, scope).check({ type, policy: 'redact' }, others)

describe('a policy typed', () => {
  it('saves a type as the daemon names them', () => {
    expect(check('SLACK_WEBHOOK_URL')).toEqual({ ok: true, problem: null })
    expect(check('EMAIL', [{ type: 'IP_ADDRESS', policy: 'allow' }])).toEqual({ ok: true, problem: null })
  })

  it('says nothing while the type is empty, and cannot be saved', () => {
    expect(check('')).toEqual({ ok: false, problem: null })
  })

  it('says what a type takes when it is not one', () => {
    expect(check('9X')).toEqual({ ok: false, problem: i18n.t('rules.recognizers.problems.type') })
    expect(check('A')).toEqual({ ok: false, problem: i18n.t('rules.recognizers.problems.type') })
  })

  it('says a type has a policy already', () => {
    expect(check('EMAIL', [{ type: 'EMAIL', policy: 'allow' }])).toEqual({ ok: false, problem: i18n.t('rules.policies.taken') })
  })
})

describe('a policy in a project', () => {
  it('says on its own action that it is not applied there, the action in force said as it is', () => {
    const project = { kind: 'project', project: { name: 'billing-app', effective: { policies: { EMAIL: 'mask' } } } } as unknown as RulesScope
    const columns = policiesSpec(t, project).columns
    const row: RuleRow<PolicyItem> = { key: 'personal:policy:EMAIL', source: 'personal', item: { type: 'EMAIL', policy: 'allow' } }
    const cell = (id: string) => {
      const column = columns.find((c) => c.id === id)!
      return renderToStaticMarkup(column.cell ? column.cell(row) : column.text(row)).replace(/<[^>]+>/g, '')
    }
    const note = `(${i18n.t('rules.policies.notApplied')})`
    expect(cell('action')).toContain(note)
    expect(cell('inForce')).not.toContain(note)
    expect(cell('inForce')).toBe(i18n.t('rules.policies.mask'))
  })
})
