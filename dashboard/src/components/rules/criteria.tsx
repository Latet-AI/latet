// The policies as a table, a type each: what happens to a value of that type (mask, redact, allow). Add picks the type
// among those Latet knows (its own, the common ones, and those your terms and recognizers make), or takes one typed: a
// type Latet finds that the menu does not list, such as a secret rule's.

import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { Policy } from '../../api/types'
import { POLICIES, POLICY_HELP, POLICY_LABEL } from '../../lib/labels'
import { TYPE_PATTERN } from '../../lib/recognizers'
import { policiesList, recognizersList, termsList, typesToAdd } from '../../lib/ruleLists'
import type { PolicyItem } from '../../lib/ruleTable'
import { ANCHORS } from '../rules'
import type { RuleColumn } from '../RuleTable'
import { Segmented } from '../ui'
import type { RulesScope } from './editing'
import { RuleList, rowsIn, type FieldsProps, type ListFilter, type ListSpec, type TableProps, type Translate } from './RuleList'
import { typeOf } from './terms'

const NEW_POLICY: PolicyItem = { type: '', policy: 'redact' }

/** The menu's choice that types the type (no type is written so). */
const OTHER = '*'

export function PoliciesTable(props: TableProps) {
  const { t } = useTranslation()
  return <RuleList spec={policiesSpec(t, props.scope)} {...props} />
}

/** The policies' table and panel; in a project, with the action in force there. */
export function policiesSpec(t: Translate, scope: RulesScope): ListSpec<PolicyItem, PolicyItem> {
  const action = (policy: Policy) => t(POLICY_LABEL[policy])
  // In a project: the action in force there, which a latet.yaml's applies only where it is stricter.
  const inForce = scope.kind === 'project' ? scope.project.effective.policies : null
  const forced = (type: string): Policy => inForce?.[type] ?? 'mask'
  const columns: RuleColumn<PolicyItem>[] = [
    { id: 'type', header: t('rules.policies.type'), text: (row) => row.item.type, sortable: true, className: 'mono' },
    {
      id: 'action',
      header: t('rules.policies.action'),
      text: (row) => action(row.item.policy),
      // In a project, a row whose action is not the one in force there says so on its own action.
      cell: (row) => (
        <span className="whitespace-nowrap">
          {action(row.item.policy)}
          {inForce && forced(row.item.type) !== row.item.policy && <span className="ml-1.5 text-meta text-ink-3">({t('rules.policies.notApplied')})</span>}
        </span>
      ),
      sortable: true,
      filter: true,
      searched: false,
    },
    ...(inForce
      ? [
          {
            id: 'inForce',
            header: t('rules.policies.inForce'),
            text: (row: { item: PolicyItem }) => action(forced(row.item.type)),
            sortable: true as const,
            searched: false as const,
          },
        ]
      : []),
  ]
  const filters: ListFilter[] = [
    { column: 'action', label: t('rules.policies.action'), options: POLICIES.map((policy) => ({ value: action(policy), label: action(policy) })) },
  ]

  return {
    list: (source) => policiesList(source),
    anchor: ANCHORS.policies,
    title: t('rules.policies.title'),
    docs: 'panel.policy',
    empty: t('rules.policies.empty'),
    addLabel: t('rules.policies.add'),
    newTitle: t('rules.policies.new'),
    rowTitle: (item) => item.type,
    columns,
    filters,
    deleteConfirm: (count) => t('rules.policies.deleteConfirm', { count }),
    blank: NEW_POLICY,
    toDraft: (item) => item,
    toItem: (draft) => draft,
    check: (draft, others) => {
      if (draft.type === '') return { ok: false, problem: null }
      if (!TYPE_PATTERN.test(draft.type)) return { ok: false, problem: t('rules.recognizers.problems.type') }
      if (others.some((item) => item.type === draft.type)) return { ok: false, problem: t('rules.policies.taken') }
      return { ok: true, problem: null }
    },
    Fields: PolicyFields,
  }
}

/** A policy's type (for a new one picked among those not set yet, or typed; fixed for one set) and what happens to its
 * values. */
function PolicyFields({ draft, set, problem, row, others, scope }: FieldsProps<PolicyItem, PolicyItem>) {
  const { t } = useTranslation()
  const id = useId()
  const known = scope.kind === 'global' ? scope.settings.pii_available : scope.global.pii_available
  const choices =
    row === null
      ? typesToAdd(
          known,
          rowsIn(scope, termsList).map((term) => term.item),
          rowsIn(scope, recognizersList).map((entry) => entry.item),
          others.map((item) => item.type),
        )
      : null
  // A draft whose type the menu does not list shows it typed.
  const [typing, setTyping] = useState(() => choices !== null && draft.type !== '' && !choices.includes(draft.type))
  const pick = (value: string) => {
    setTyping(value === OTHER)
    set((item) => ({ ...item, type: value === OTHER ? '' : value }))
  }
  return (
    <>
      {choices ? (
        <>
          <label className="flex flex-col gap-1">
            <span className="label">{t('rules.policies.type')}</span>
            <select className="field mono w-[280px] max-w-full" value={typing ? OTHER : draft.type} onChange={(e) => pick(e.target.value)}>
              <option value="">{t('rules.policies.choose')}</option>
              {choices.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
              <option value={OTHER}>{t('rules.policies.other')}</option>
            </select>
          </label>
          {typing && (
            <div className="flex flex-col gap-1">
              <label className="flex flex-col gap-1">
                <span className="label">{t('rules.policies.typeOther')}</span>
                <input
                  className="field mono w-[280px] max-w-full"
                  value={draft.type}
                  onChange={(e) => set((item) => ({ ...item, type: typeOf(e.target.value) }))}
                  aria-invalid={problem !== null || undefined}
                  aria-describedby={`${id}-help`}
                  spellCheck={false}
                />
              </label>
              <span id={`${id}-help`} className="text-meta text-ink-3">
                {t('rules.policies.typeOtherHelp')}
              </span>
            </div>
          )}
          {problem !== null && <span className="text-meta text-crit-ink">{problem}</span>}
        </>
      ) : (
        <div className="flex flex-col gap-1">
          <span className="label">{t('rules.policies.type')}</span>
          <code className="mono text-ui text-ink">{draft.type}</code>
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        <span className="label">{t('rules.policies.action')}</span>
        <Segmented<Policy>
          label={t('rules.policies.for', { type: draft.type })}
          value={draft.policy}
          onChange={(policy) => set((item) => ({ ...item, policy }))}
          options={POLICIES.map((policy) => ({ value: policy, label: t(POLICY_LABEL[policy]) }))}
        />
      </div>
      <dl className="grid grid-cols-[90px_1fr] gap-x-3 gap-y-1 text-meta text-ink-3">
        {POLICIES.map((policy) => (
          <div key={policy} className="contents">
            <dt className="font-medium text-ink-2">{t(POLICY_LABEL[policy])}</dt>
            <dd>{t(POLICY_HELP[policy])}</dd>
          </div>
        ))}
      </dl>
    </>
  )
}
