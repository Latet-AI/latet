// The recognizers as a table: a row each (its name, type, what it finds, what happens to its values, how it loaded),
// opened in a panel with every field of the editor and its Try it; turned on, turned off or deleted a selection at a
// time. A recognizer drafted from a description opens in the panel, new, for you to check and save.

import { useTranslation } from 'react-i18next'
import type { Policy, RecognizerEntry, RecognizerStatus } from '../../api/types'
import { tk } from '../../i18n'
import { useDeploymentKey } from '../../lib/me'
import { draftProblems, EMPTY_DRAFT, fromDraft, stateOf, toDraft, type RecognizerDraft as Draft } from '../../lib/recognizers'
import { recognizersList, recognizerType, turnedOff, turnedOn } from '../../lib/ruleLists'
import type { RuleRow, RuleSource } from '../../lib/ruleTable'
import { ANCHORS } from '../rules'
import { RecognizerDraft } from '../RecognizerDraft'
import { useDocsHref } from '../DocsLinks'
import { DraftProblems, RecognizerFields, RecognizerTry, STATE_LABEL, STATE_STYLE } from '../RecognizersEditor'
import { Truncated, type RuleColumn } from '../RuleTable'
import type { RulesScope } from './editing'
import { RuleList, settingsOfSource, type FieldsProps, type Incoming, type ListFilter, type ListSpec, type TableProps, type Translate } from './RuleList'

const ACTION_LABEL: Record<Policy, string> = {
  mask: tk('rules.recognizers.policyDefault'),
  redact: tk('rules.recognizers.policyRedact'),
  allow: tk('rules.recognizers.policyAllow'),
}

/** Draft from a description: offered with the local reviewer on, said how to turn it on when it is off, or neither. */
export type Drafting = 'on' | 'off' | null

/** The recognizers as loaded, of the rules a row comes from: the global rules, or the project's. */
function statusesOf(scope: RulesScope, source: RuleSource): RecognizerStatus[] {
  if (scope.kind === 'global') return scope.settings.recognizer_status ?? []
  return source === 'global' ? (scope.global.recognizer_status ?? []) : scope.project.recognizer_status
}

const nameOf = (entry: RecognizerEntry) => (typeof entry.name === 'string' ? entry.name : '')
const patternOf = (entry: RecognizerEntry) => (typeof entry.pattern === 'string' ? entry.pattern : '')

export function RecognizersTable({
  scope,
  onSaved,
  drafting = null,
  incoming = null,
  search,
  shown,
}: TableProps & {
  drafting?: Drafting
  /** A recognizer suggested from the project's files, opened in the panel. */
  incoming?: Incoming<Draft> | null
}) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const howTo = useDocsHref('section.recognizers')
  return (
    <RuleList
      spec={recognizersSpec(t, key, scope)}
      scope={scope}
      onSaved={onSaved}
      incoming={incoming}
      search={search}
      shown={shown}
      below={(open) => (
        <div className="space-y-3">
          <a className="link text-meta" href={howTo} target="_blank" rel="noreferrer">
            {t('rules.recognizers.docs')}
          </a>
          {drafting === 'on' ? (
            <RecognizerDraft onAdd={(entry) => open(toDraft(entry).draft)} />
          ) : drafting === 'off' ? (
            <p className="text-meta text-ink-3">{t('rules.recognizers.draft.off')}</p>
          ) : null}
        </div>
      )}
    />
  )
}

/** The recognizers' table and panel. `key`: the words of a key as this deployment says them (a server's, a computer's). */
export function recognizersSpec(t: Translate, key: (words: string) => string, scope: RulesScope): ListSpec<RecognizerEntry, Draft> {
  const list = (source: RuleSource) => recognizersList(source)
  const saved = (source: RuleSource) => list(source).get(settingsOfSource(scope, source))
  const state = (row: RuleRow<RecognizerEntry>) => stateOf(row.item, saved(row.source), statusesOf(scope, row.source))
  const name = (entry: RecognizerEntry) => nameOf(entry) || t('rules.recognizers.noName')
  const kind = (entry: RecognizerEntry) => (entry.class === 'secret' ? t('rules.recognizers.classSecret') : t('rules.recognizers.classCustom'))

  const columns: RuleColumn<RecognizerEntry>[] = [
    { id: 'name', header: t('rules.recognizers.name'), text: (row) => name(row.item), sortable: true, className: 'mono' },
    { id: 'type', header: t('rules.recognizers.type'), text: (row) => recognizerType(row.item), sortable: true, filter: true, className: 'mono text-ink-2' },
    {
      id: 'finds',
      header: t('rules.recognizers.finds'),
      text: (row) => patternOf(row.item),
      cell: (row) => <Truncated text={patternOf(row.item)} className="mono max-w-[30ch] text-ink-2" />,
    },
    { id: 'action', header: t('rules.policies.action'), text: (row) => t(ACTION_LABEL[row.item.policy ?? 'mask']), sortable: true, searched: false },
    {
      id: 'state',
      header: t('rules.recognizers.stateTitle'),
      text: (row) => t(STATE_LABEL[state(row)]),
      cell: (row) => <span className={STATE_STYLE[state(row)]}>{t(STATE_LABEL[state(row)])}</span>,
      sortable: true,
      filter: true,
      searched: false,
    },
    { id: 'class', header: t('rules.recognizers.class'), text: (row) => kind(row.item), filter: true, hidden: true, searched: false },
  ]
  const filters: ListFilter[] = [
    { column: 'type', label: t('rules.recognizers.type') },
    { column: 'state', label: t('rules.recognizers.stateTitle') },
    { column: 'class', label: t('rules.recognizers.class') },
  ]

  return {
    list,
    anchor: ANCHORS.recognizers,
    title: t('rules.recognizers.title'),
    docs: 'panel.recognizer',
    empty: t('rules.recognizers.empty'),
    addLabel: t('rules.recognizers.add'),
    newTitle: t('rules.recognizers.new'),
    rowTitle: name,
    columns,
    filters,
    deleteConfirm: (count) => t('rules.recognizers.deleteConfirm', { count }),
    more: [
      { id: 'on', label: t('rules.recognizers.turnOn'), confirm: (count) => t('rules.recognizers.turnOnConfirm', { count }), update: turnedOn },
      { id: 'off', label: t('rules.recognizers.turnOff'), confirm: (count) => t('rules.recognizers.turnOffConfirm', { count }), update: turnedOff },
    ],
    copy: true,
    transfer: 'recognizers',
    blank: EMPTY_DRAFT,
    toDraft: (entry) => toDraft(entry).draft,
    toItem: fromDraft,
    check: (draft, others) => {
      const problems = draftProblems(draft, others.map(nameOf))
      return { ok: problems.length === 0, problem: problems.length > 0 ? <DraftProblems problems={problems} /> : null }
    },
    Fields: RecognizerPanelFields,
    // Changed by hand in the file it is in: config.yaml (the global rules, your own for a project), or latet.yaml.
    readOnly: (row) =>
      toDraft(row.item).editable ? undefined : row.source === 'repo' ? t('rules.recognizers.handEditedRepo') : t(key('rules.recognizers.handEdited')),
  }
}

/** A recognizer's panel: how it loaded when it was last saved, every field of the editor, what it lacks, and Try it. */
function RecognizerPanelFields({ draft, set, problem, row, scope }: FieldsProps<Draft, RecognizerEntry>) {
  const { t } = useTranslation()
  const settings = scope.kind === 'global' ? scope.settings : scope.global
  const status = row ? statusesOf(scope, row.source).find((s) => s.name === row.item.name) : undefined
  const loaded = [...(status?.errors ?? []), ...(status?.warnings ?? [])]
  return (
    <>
      {loaded.length > 0 && (
        <div className="rounded-lg border border-line p-3 text-meta">
          <p className="font-medium text-ink-2">{t('rules.recognizers.loaded')}</p>
          <ul className="mt-1 space-y-0.5 text-crit-ink">
            {loaded.map((line) => (
              <li key={line} className="break-words">
                {line}
              </li>
            ))}
          </ul>
        </div>
      )}
      <RecognizerFields draft={draft} set={(patch) => set((current) => ({ ...current, ...patch }))} checksums={settings.checksums ?? []} />
      {problem}
      <div className="border-t border-line pt-4">
        <RecognizerTry draft={draft} />
      </div>
    </>
  )
}
