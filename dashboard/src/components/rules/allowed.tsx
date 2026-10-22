// The values and the patterns never masked as one table, a Kind for each row: every Not sensitive adds a value, so the
// list grows long. A row opens in a panel; a pattern's panel tries it on a value, matched in the browser as the daemon
// matches it (whole), after the same check of what RE2 reads that the page made before (validatePattern).

import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { allowList, wholeMatch } from '../../lib/ruleLists'
import type { AllowItem } from '../../lib/ruleTable'
import type { RuleColumn } from '../RuleTable'
import { ANCHORS, validatePattern } from '../rules'
import { InlineCodeText, Segmented } from '../ui'
import { RuleList, type FieldsProps, type ListFilter, type ListSpec, type TableProps, type Translate } from './RuleList'

const EMPTY_VALUE: AllowItem = { kind: 'value', value: '' }

export function AllowedTable(props: TableProps) {
  const { t } = useTranslation()
  return <RuleList spec={allowSpec(t)} {...props} />
}

/** The table and panel of the values never masked and their patterns. */
export function allowSpec(t: Translate): ListSpec<AllowItem, AllowItem> {
  const kindWord = (kind: AllowItem['kind']) => (kind === 'value' ? t('rules.allow.kindValue') : t('rules.allow.kindPattern'))
  const columns: RuleColumn<AllowItem>[] = [
    { id: 'value', header: t('rules.allow.column'), text: (row) => row.item.value, sortable: true, className: 'mono' },
    { id: 'kind', header: t('rules.allow.kind'), text: (row) => kindWord(row.item.kind), sortable: true, filter: true, searched: false },
  ]
  const filters: ListFilter[] = [
    {
      column: 'kind',
      label: t('rules.allow.kind'),
      options: [
        { value: kindWord('value'), label: kindWord('value') },
        { value: kindWord('pattern'), label: kindWord('pattern') },
      ],
    },
  ]

  return {
    list: (source) => allowList(source),
    // A repository file cannot set values never to mask (the daemon ignores them there).
    repo: false,
    anchor: ANCHORS.allow,
    title: t('rules.allow.title'),
    docs: 'panel.allowed',
    empty: t('rules.allow.noValues'),
    addLabel: t('rules.allow.add'),
    newTitle: t('rules.allow.new'),
    rowTitle: (item) => item.value,
    columns,
    filters,
    deleteConfirm: (count) => t('rules.allow.deleteConfirm', { count }),
    copy: true,
    transfer: 'values',
    blank: EMPTY_VALUE,
    toDraft: (item) => item,
    toItem: (draft) => ({ kind: draft.kind, value: draft.value.trim() }),
    check: (draft, others) => {
      const text = draft.value.trim()
      if (text === '') return { ok: false, problem: null }
      const words = others.some((item) => item.kind === draft.kind && item.value === text)
        ? t('rules.allow.taken')
        : draft.kind === 'pattern'
          ? validatePattern(t, text)
          : null
      return { ok: words === null, problem: words === null ? null : <InlineCodeText text={words} /> }
    },
    Fields: AllowedFields,
  }
}

/** A value or a pattern: which it is, the text whole, and for a pattern a value to try it on. */
function AllowedFields({ draft, set, problem }: FieldsProps<AllowItem, AllowItem>) {
  const { t } = useTranslation()
  const id = useId()
  const [sample, setSample] = useState('')
  const pattern = draft.kind === 'pattern'
  // Tried once a pattern that can be saved and a value are typed: matched or not, or (null) not readable here as RE2 reads it.
  const tried = pattern && sample !== '' && problem === null && draft.value.trim() !== ''
  const matched = tried ? wholeMatch(draft.value.trim(), sample) : null
  return (
    <>
      <div className="flex flex-col gap-1">
        <span className="label">{t('rules.allow.kind')}</span>
        <Segmented<AllowItem['kind']>
          label={t('rules.allow.kind')}
          value={draft.kind}
          onChange={(kind) => set((item) => ({ ...item, kind }))}
          options={[
            { value: 'value', label: t('rules.allow.kindValue') },
            { value: 'pattern', label: t('rules.allow.kindPattern') },
          ]}
        />
      </div>
      <div className="flex flex-col gap-1">
        <label className="flex flex-col gap-1">
          <span className="label">{t('rules.allow.column')}</span>
          <textarea
            className="field mono min-h-[60px] w-full break-all"
            rows={2}
            value={draft.value}
            placeholder={pattern ? t('rules.allow.patternPlaceholder') : t('rules.allow.valuePlaceholder')}
            onChange={(e) => set((item) => ({ ...item, value: e.target.value }))}
            // One value, or one pattern, a line: Enter adds no line to it.
            onKeyDown={(e) => {
              if (e.key === 'Enter') e.preventDefault()
            }}
            aria-invalid={problem !== null || undefined}
            aria-describedby={`${id}-help${problem !== null ? ` ${id}-problem` : ''}`}
            spellCheck={false}
          />
        </label>
        <span id={`${id}-help`} className="text-meta text-ink-3">
          {pattern ? t('rules.allow.patternsHelp') : t('rules.allow.valuesHelp')}
        </span>
        {problem !== null && (
          <span id={`${id}-problem`} className="text-meta text-crit-ink">
            {problem}
          </span>
        )}
      </div>
      {pattern && (
        <div className="space-y-1.5 rounded-lg border border-line p-3">
          <label className="flex flex-col gap-1">
            <span className="label">{t('rules.allow.try.label')}</span>
            <input
              className="field mono w-full"
              value={sample}
              onChange={(e) => setSample(e.target.value)}
              placeholder={t('rules.allow.try.placeholder')}
              spellCheck={false}
            />
          </label>
          {tried && (
            <p className={`text-meta ${matched ? 'text-good-ink' : 'text-ink-2'}`} role="status">
              {matched === null ? t('rules.allow.try.cannot') : matched ? t('rules.allow.try.matches') : t('rules.allow.try.noMatch')}
            </p>
          )}
        </div>
      )}
    </>
  )
}
