// A list of the rules as a table and its panel, filled per list (terms, recognizers, values never masked, policies):
// its columns, what a row's panel holds, how a draft is checked; the rest is the same for every list. The panel saves a
// row on the settings as they are now (saveOne); an action on the rows selected applies at once after a confirmation.
//
// In a project, the table shows every rule that applies there, a row per source with where it comes from (From): your
// global rules, your own settings for the project, its latet.yaml. A row is edited where it is born: a global row is
// read only there, with a link to the global rules; a latet.yaml row is saved to the file where the dashboard may write
// it; a new row goes to your settings for the project unless Save to says latet.yaml.

import { useState, type ComponentType, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import type { ConfigView, PersonalSettings, ProjectSettings } from '../../api/types'
import type { DocsTarget } from '../../lib/docsLinks'
import { useDeploymentKey, useMe } from '../../lib/me'
import { globalIo, personalIo, putChange, removeChange, repoIo, saveOne, updateChange, type Change, type ListAccess, type SaveIo } from '../../lib/panelSave'
import { queried, type RuleRow, type RuleSource } from '../../lib/ruleTable'
import { Icon } from '../Icon'
import { RulePanel } from '../RulePanel'
import { RuleTable, Truncated, type RuleAction, type RuleColumn, type RuleFilter } from '../RuleTable'
import { Segmented } from '../ui'
import { CopyToProject } from './copy'
import { ListTransfer, type TransferKind } from './transfer'
import { useEditing, useSettle, type Editing, type RulesScope } from './editing'

/** Any settings a list is in: the global rules, your own for a project, a latet.yaml. */
export type SettingsOf = ConfigView | PersonalSettings | ProjectSettings

/** What a list's panel shows: the draft, how it changes, why it cannot be saved, and what is around it. */
export interface FieldsProps<D, T> {
  draft: D
  set: (update: (draft: D) => D) => void
  /** Why the draft cannot be saved, worded; null when it can, or when it says nothing (a field still empty). */
  problem: ReactNode | null
  /** The row as saved (null: a new one). */
  row: RuleRow<T> | null
  /** The other items of the list the draft is saved in. */
  others: T[]
  scope: RulesScope
  /** Where the draft is saved. */
  source: RuleSource
}

/** A filter of a list: its menu's values given, or (none given) those of its column in the table's rows. */
export interface ListFilter {
  column: string
  label: string
  options?: { value: string; label: string }[]
}

/** One list, as its table and its panel need it. */
export interface ListSpec<T, D> {
  /** The list in the settings of a source. */
  list: (source: RuleSource) => ListAccess<SettingsOf, T>
  /** False: a latet.yaml cannot set it (values never masked), so a project's rows of it come from two sources. */
  repo?: false
  /** The section of the global rules it is in: a global row opened in a project links there. */
  anchor: string
  /** The table's name, what an empty list says, the Add button. */
  title: string
  /** Where a row's fields are explained: the "?" at the top of its panel, named after the list. */
  docs: DocsTarget
  empty: string
  addLabel: string
  /** The panel's title: for a new row, and for a row of the list. */
  newTitle: string
  rowTitle: (item: T) => string
  columns: RuleColumn<T>[]
  filters: ListFilter[]
  deleteConfirm: (count: number) => string
  /** Actions on the rows selected besides Delete (turn on, turn off): each rewrites every row selected. */
  more?: { id: string; label: string; confirm: (count: number) => string; update: (item: T) => T }[]
  /** Copy to a project, in the global rules. */
  copy?: boolean
  blank: D
  toDraft: (item: T) => D
  toItem: (draft: D) => T
  /** Whether the draft can be saved, and why not in words (`others`: the other items of its list). */
  check: (draft: D, others: T[]) => { ok: boolean; problem: ReactNode | null }
  Fields: ComponentType<FieldsProps<D, T>>
  /** Why a row cannot be changed in its panel (a recognizer with keys the form cannot show). */
  readOnly?: (row: RuleRow<T>) => ReactNode | undefined
  /** Its own Import and Export: what a file of it holds (none: the list is short, the policies). */
  transfer?: TransferKind
}

/** What a page gives a list's table. */
export interface TableProps {
  scope: RulesScope
  onSaved: () => void
  /** The search at the top of Rules. */
  search?: string
  /** False while the table's tab is not the one shown: a row open in its panel is kept, out of sight. */
  shown?: boolean
}

/** A draft opened from outside the table (a recognizer suggested for a project): opened once per `id`. */
export interface Incoming<D> {
  id: number
  draft: D
  source: RuleSource
}

/** The settings of a source, as the page read them. */
export function settingsOfSource(scope: RulesScope, source: RuleSource): SettingsOf {
  if (scope.kind === 'global') return scope.settings
  if (source === 'global') return scope.global
  return source === 'personal' ? (scope.project.personal ?? {}) : scope.project.repo.settings
}

/** Where a source's settings are read and written now. */
function ioOf(scope: RulesScope, source: RuleSource): SaveIo<SettingsOf> {
  if (scope.kind === 'global' || source === 'global') return globalIo() as SaveIo<SettingsOf>
  return (source === 'personal' ? personalIo(scope.project.id) : repoIo(scope.project.id)) as SaveIo<SettingsOf>
}

/** The rows of a list in a scope: the global ones; in a project, those of every source it may come from. */
export function rowsIn<T>(scope: RulesScope, list: (source: RuleSource) => ListAccess<SettingsOf, T>, repo = true): RuleRow<T>[] {
  const sources: RuleSource[] = scope.kind === 'global' ? ['global'] : repo ? ['global', 'personal', 'repo'] : ['global', 'personal']
  return sources.flatMap((source) => {
    const access = list(source)
    return access.rows(access.get(settingsOfSource(scope, source)))
  })
}

/** The words of the page's translation function, as the lists use them. */
export type Translate = (key: string, options?: Record<string, unknown>) => string

/** Where a row of a project comes from, in words. */
function fromText(t: Translate, project: string, source: RuleSource): string {
  return source === 'global' ? t('rules.from.global') : source === 'personal' ? t('rules.from.personal', { project }) : t('rules.from.repo')
}

/** The From column of a project's table: the source of each row, a padlock on those of the global rules. */
export function fromColumnFor<T>(t: Translate, project: string): RuleColumn<T> {
  return {
    id: 'from',
    header: t('rules.from.title'),
    text: (row) => fromText(t, project, row.source),
    cell: (row) => (
      <span className="inline-flex items-center gap-1 text-ink-2">
        <Truncated text={fromText(t, project, row.source)} className={row.source === 'repo' ? 'mono' : ''} />
        {row.source === 'global' && <Icon name="vault" size={12} className="flex-none text-ink-3" />}
      </span>
    ),
    sortable: true,
    filter: true,
    searched: false,
  }
}

/** The columns of a list's table in a scope: a project's have From too. */
function columnsIn<T, D>(t: Translate, spec: ListSpec<T, D>, scope: RulesScope): RuleColumn<T>[] {
  return scope.kind === 'project' ? [...spec.columns, fromColumnFor<T>(t, scope.project.name)] : spec.columns
}

/** How many rows of a list the search at the top of Rules finds, as its table finds them. */
export function foundIn<T, D>(t: Translate, spec: ListSpec<T, D>, scope: RulesScope, search: string): number {
  if (search.trim() === '') return 0
  const rows = rowsIn(scope, spec.list, spec.repo !== false)
  return queried(rows, { search, filters: {}, sort: null, page: 1 }, columnsIn(t, spec, scope)).matched
}

export function RuleList<T, D>({
  spec,
  scope,
  onSaved,
  below,
  incoming = null,
  search = '',
  shown = true,
}: TableProps & {
  spec: ListSpec<T, D>
  /** Under the table (Draft from a description): `open` shows a draft, new, in the panel. */
  below?: (open: (draft: D) => void) => ReactNode
  incoming?: Incoming<D> | null
}) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const { me } = useMe()
  const editor = useEditing<D>()
  const settle = useSettle()
  // The rows being copied to a project, and what the table hears when the copy is done or cancelled.
  const [copying, setCopying] = useState<{ items: T[]; done: (copied: boolean) => void } | null>(null)
  const [opened, setOpened] = useState<number | null>(null)
  const project = scope.kind === 'project' ? scope.project : null
  // The repository file is written for you only on this machine, where the dashboard may write it.
  const canWriteRepo = project !== null && me !== undefined && !me.server && project.repo.writable
  const hasRepo = spec.repo !== false
  const rows = rowsIn(scope, spec.list, hasRepo)
  const editable = (source: RuleSource) => (scope.kind === 'global' ? true : source === 'personal' || (source === 'repo' && canWriteRepo))

  const savedWords = (source: RuleSource) => (version?: number | string) =>
    source === 'global' ? t('rules.saved', { version }) : source === 'personal' ? t('projects.personal.saved') : t('projects.repo.saved')
  const fromWords = (source: RuleSource) => fromText(t, project?.name ?? '', source)
  const columns = columnsIn(t, spec, scope)
  const listFilters: ListFilter[] = project
    ? [
        ...spec.filters,
        {
          column: 'from',
          label: t('rules.from.title'),
          options: (['personal', 'global', ...(hasRepo ? ['repo' as const] : [])] as RuleSource[]).map((source) => ({ value: fromWords(source), label: fromWords(source) })),
        },
      ]
    : spec.filters
  const filters: RuleFilter[] = listFilters.map((filter) => {
    if (filter.options) return { column: filter.column, label: filter.label, options: filter.options }
    const column = columns.find((c) => c.id === filter.column)
    const values = column ? [...new Set(rows.map((row) => column.text(row)))].filter(Boolean).sort() : []
    return { column: filter.column, label: filter.label, options: values.map((value) => ({ value, label: value })) }
  })

  /** One change per source the rows selected come from, each saved on that source's settings as they are now. */
  const bySource = async (keys: Set<string>, change: (list: ListAccess<SettingsOf, T>, keys: Set<string>) => Change<SettingsOf>) => {
    for (const source of ['global', 'personal', 'repo'] as RuleSource[]) {
      const own = new Set(rows.filter((row) => row.source === source && keys.has(row.key)).map((row) => row.key))
      if (own.size === 0 || !editable(source)) continue
      settle(await saveOne(ioOf(scope, source), settingsOfSource(scope, source), change(spec.list(source), own)), savedWords(source), onSaved)
    }
  }
  // A selection holding rows of latet.yaml says, before it is changed, that the file read by everyone is written again.
  const withRepo = (confirm: (count: number) => string) => (count: number, keys: Set<string>) => {
    const inRepo = rows.filter((row) => row.source === 'repo' && keys.has(row.key)).length
    return inRepo > 0 ? `${confirm(count)} ${t('rules.table.repoRows', { count: inRepo })}` : confirm(count)
  }
  const actions: RuleAction[] = [
    { id: 'delete', label: t('rules.table.delete'), danger: true, confirm: withRepo(spec.deleteConfirm), run: (keys) => bySource(keys, removeChange) },
    ...(spec.more ?? []).map(
      (action): RuleAction => ({
        id: action.id,
        label: action.label,
        confirm: withRepo(action.confirm),
        run: (keys) => bySource(keys, (list, own) => updateChange(list, own, action.update)),
      }),
    ),
  ]
  if (spec.copy && scope.kind === 'global') {
    actions.push({
      id: 'copy',
      label: t('rules.table.copy'),
      // The selection stays until the copy is done: cancelled, it is there to pick again.
      run: (keys) => new Promise<boolean>((done) => setCopying({ items: rows.filter((row) => keys.has(row.key)).map((row) => row.item), done })),
    })
  }

  const editing = editor.editing
  const row = editing?.key ? (rows.find((r) => r.key === editing.key) ?? null) : null
  const source: RuleSource = editing?.source ?? (project ? 'personal' : 'global')
  const others = rows.filter((r) => r.source === source && r.key !== editing?.key).map((r) => r.item)
  const verdict = editing ? spec.check(editing.draft, others) : { ok: false, problem: null }
  const readOnly: ReactNode | undefined = !row
    ? undefined
    : row.source === 'global' && project
      ? (
          <>
            <p>{t('rules.panel.global')}</p>
            <Link className="link mt-1 inline-block" to={`/rules#${spec.anchor}`}>
              {t('rules.panel.openGlobal')}
            </Link>
          </>
        )
      : row.source === 'repo' && !canWriteRepo
        ? project?.repo.readable === false
          ? t('projects.repo.unreadable')
          : t(key('projects.repo.readOnly'))
        : spec.readOnly?.(row)
  const openRow = (r: RuleRow<T>) => editor.open({ key: r.key, source: r.source, initial: spec.toDraft(r.item), draft: spec.toDraft(r.item), found: r.item })
  // The page read the settings again (a save, an action on a selection, an import): the row open follows, or says it
  // changed under its edits; its save is never made over a change it did not show.
  if (editing?.key && editing.found !== undefined) editor.resync(row?.item, spec.toDraft)
  const openDraft = (draft: D, from: RuleSource = project ? 'personal' : 'global') => {
    const next: Editing<D> = { key: null, source: from, initial: spec.blank, draft }
    editor.open(next)
  }
  if (incoming && incoming.id !== opened) {
    setOpened(incoming.id)
    openDraft(incoming.draft, incoming.source === 'repo' && !(hasRepo && canWriteRepo) ? 'personal' : incoming.source)
  }
  const save = () => {
    if (!editing) return
    const list = spec.list(editing.source)
    const change = putChange(list, editing.key, spec.toItem(editing.draft), editing.found as T | undefined)
    void editor.save(ioOf(scope, editing.source), settingsOfSource(scope, editing.source), change, list, savedWords(editing.source), onSaved, {
      toItem: spec.toItem,
      toDraft: spec.toDraft,
    })
  }
  const Fields = spec.Fields
  const offerRepo = project !== null && hasRepo && canWriteRepo

  return (
    <>
      <RuleTable<T>
        label={spec.title}
        rows={rows}
        columns={columns}
        filters={filters}
        actions={actions}
        onOpen={openRow}
        onAdd={() => openDraft(spec.blank)}
        addLabel={project ? t('rules.table.addFor', { project: project.name }) : spec.addLabel}
        empty={spec.empty}
        opened={editing?.key ?? null}
        selectable={(r) => editable(r.source)}
        search={search}
        extra={spec.transfer ? (matched) => <ListTransfer kind={spec.transfer!} rows={matched} project={project?.id} onImported={onSaved} /> : undefined}
      />
      {below?.((draft) => openDraft(draft))}
      <RulePanel
        open={editing !== null}
        title={row ? spec.rowTitle(row.item) : spec.newTitle}
        dirty={editor.dirty}
        busy={editor.busy}
        readOnly={readOnly}
        onSave={save}
        canSave={verdict.ok && editor.dirty}
        onClose={editor.close}
        status={editor.status}
        hidden={!shown}
        docs={{ target: spec.docs, section: spec.title }}
      >
        {editing && project && !row && offerRepo && (
          <SaveTo source={editing.source} project={project.name} exists={project.repo.exists} onChange={editor.setSource} />
        )}
        {editing && row?.source === 'repo' && canWriteRepo && (
          <p className="text-meta text-ink-3">
            {t('rules.panel.repoFile')} {t('rules.saveTo.rewritten')}
          </p>
        )}
        {editing && (
          <Fields draft={editing.draft} set={editor.setDraft} problem={verdict.problem} row={row} others={others} scope={scope} source={editing.source} />
        )}
      </RulePanel>
      {editor.question}
      {copying && (
        <CopyToProject<T>
          items={copying.items}
          list={spec.list('personal') as ListAccess<PersonalSettings, T>}
          onClose={(copied) => {
            copying.done(copied)
            setCopying(null)
          }}
        />
      )}
    </>
  )
}

/** Where a new row of a project is saved: your settings for it (the default), or its latet.yaml, read by everyone,
 * created by the save when the project has none, written again whole when it has one. */
function SaveTo({ source, project, exists, onChange }: { source: RuleSource; project: string; exists: boolean; onChange: (source: RuleSource) => void }) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col gap-1.5">
      <span className="label">{t('rules.saveTo.title')}</span>
      <Segmented<'personal' | 'repo'>
        label={t('rules.saveTo.title')}
        value={source === 'repo' ? 'repo' : 'personal'}
        onChange={onChange}
        options={[
          { value: 'personal', label: t('rules.saveTo.personal', { project }) },
          { value: 'repo', label: t('rules.saveTo.repo') },
        ]}
      />
      {source === 'repo' && (
        <p className="text-meta text-ink-3">
          {t('rules.saveTo.repoHelp')} {exists ? t('rules.saveTo.rewritten') : t('rules.saveTo.created')}
        </p>
      )}
    </div>
  )
}
