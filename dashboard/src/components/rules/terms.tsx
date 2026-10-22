// The terms as a table: a row each (term, type, how it matches), searched and filtered, opened in a panel where it is
// edited and saved at once; deleted, or copied to a project, a selection at a time.

import { useId } from 'react'
import { useTranslation } from 'react-i18next'
import type { Term } from '../../api/types'
import { termsList } from '../../lib/ruleLists'
import { TERM_LONGEST, termProblem } from '../../lib/terms'
import { useFormat } from '../../lib/useFormat'
import { ANCHORS } from '../rules'
import type { RuleColumn } from '../RuleTable'
import { InlineCodeText, Switch } from '../ui'
import { RuleList, type FieldsProps, type ListSpec, type TableProps, type Translate } from './RuleList'

const EMPTY_TERM: Term = { value: '', type: 'CUSTOM', case_sensitive: false, whole_word: true }

/** A type as the daemon names them: capitals, digits and underscores. */
export const typeOf = (value: string) => value.toUpperCase().replace(/[^A-Z0-9_]/g, '_')

export function TermsTable(props: TableProps) {
  const { t } = useTranslation()
  const format = useFormat()
  return <RuleList spec={termsSpec(t, format.number)} {...props} />
}

/** The terms' table and panel. `number`: a number in the reader's language. */
export function termsSpec(t: Translate, number: (value: number) => string): ListSpec<Term, Term> {
  const yesNo = (on: boolean) => (on ? t('common.yes') : t('common.no'))
  const columns: RuleColumn<Term>[] = [
    { id: 'value', header: t('rules.terms.value'), text: (row) => row.item.value, sortable: true },
    { id: 'type', header: t('rules.terms.type'), text: (row) => row.item.type, sortable: true, filter: true, className: 'mono text-ink-2' },
    { id: 'case', header: t('rules.terms.caseSensitive'), text: (row) => yesNo(row.item.case_sensitive), sortable: true, searched: false },
    { id: 'word', header: t('rules.terms.wholeWord'), text: (row) => yesNo(row.item.whole_word), sortable: true, searched: false },
  ]

  return {
    list: (source) => termsList(source),
    anchor: ANCHORS.terms,
    title: t('rules.terms.title'),
    docs: 'panel.term',
    empty: t('rules.terms.empty'),
    addLabel: t('rules.terms.add'),
    newTitle: t('rules.terms.newValue'),
    rowTitle: (term) => term.value,
    columns,
    filters: [{ column: 'type', label: t('rules.terms.type') }],
    deleteConfirm: (count) => t('rules.terms.deleteConfirm', { count }),
    copy: true,
    transfer: 'terms',
    blank: EMPTY_TERM,
    toDraft: (term) => term,
    toItem: (draft) => ({ ...draft, value: draft.value.trim(), type: draft.type || 'CUSTOM' }),
    check: (draft, others) => {
      const problem = termProblem({ value: draft.value, type: draft.type || 'CUSTOM' }, others)
      const words =
        problem === 'noLetterOrDigit'
          ? t('rules.terms.noLetterOrDigit')
          : problem === 'tooLong'
            ? t('rules.terms.tooLong', { max: number(TERM_LONGEST) })
            : problem === 'taken'
              ? t('rules.terms.taken')
              : null
      return { ok: problem === null, problem: words === null ? null : <InlineCodeText text={words} /> }
    },
    Fields: TermFields,
  }
}

/** The fields of a term: its value whole, its type, how it matches. */
function TermFields({ draft, set, problem }: FieldsProps<Term, Term>) {
  const { t } = useTranslation()
  const id = useId()
  return (
    <>
      <div className="flex flex-col gap-1">
        <label className="flex flex-col gap-1">
          <span className="label">{t('rules.terms.value')}</span>
          <textarea
            className="field min-h-[60px] w-full break-all"
            rows={2}
            value={draft.value}
            placeholder={t('rules.terms.placeholder')}
            onChange={(e) => set((term) => ({ ...term, value: e.target.value }))}
            // A term is one line: Enter adds no line to it.
            onKeyDown={(e) => {
              if (e.key === 'Enter') e.preventDefault()
            }}
            aria-invalid={problem !== null || undefined}
            aria-describedby={problem !== null ? `${id}-problem` : undefined}
            spellCheck={false}
          />
        </label>
        {problem !== null && (
          <span id={`${id}-problem`} className="text-meta text-crit-ink">
            {problem}
          </span>
        )}
      </div>
      <label className="flex flex-col gap-1">
        <span className="label">{t('rules.terms.type')}</span>
        <input className="field mono w-[240px] max-w-full" value={draft.type} onChange={(e) => set((term) => ({ ...term, type: typeOf(e.target.value) }))} spellCheck={false} />
      </label>
      <Switch checked={draft.case_sensitive} onChange={(on) => set((term) => ({ ...term, case_sensitive: on }))} label={t('rules.terms.caseSensitive')} />
      <Switch checked={draft.whole_word} onChange={(on) => set((term) => ({ ...term, whole_word: on }))} label={t('rules.terms.wholeWord')} />
    </>
  )
}
