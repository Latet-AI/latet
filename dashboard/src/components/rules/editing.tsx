// The editing of one row of a rules table in its panel, shared by every list: the row as the panel found it and as it
// is being edited, what its last save said, and the save itself, made on the settings as they are now (saveOne). And
// what an action on the rows selected says once it is done.

import { useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../../api/client'
import type { ConfigView, ProjectDetail } from '../../api/types'
import { tk } from '../../i18n'
import { useDeploymentKey } from '../../lib/me'
import { afterConflict, RepoNotWritable, rowChange, saveOne, type Change, type ListAccess, type SaveIo, type SaveOutcome } from '../../lib/panelSave'
import type { RuleSource } from '../../lib/ruleTable'
import { RefusedDetails } from '../rules'
import { useToast } from '../Toast'
import { ConfirmDialog, errorText } from '../ui'

/** Where a table's rows come from: the global rules (as last read), or a project's every source. */
export type RulesScope = { kind: 'global'; settings: ConfigView } | { kind: 'project'; project: ProjectDetail; global: ConfigView }

/** A row in the panel: which (null: a new one), where it is saved, as found, as edited. */
export interface Editing<T> {
  key: string | null
  source: RuleSource
  initial: T
  draft: T
  /** The row of the list as the panel found it when it opened (undefined: a new row): its save is made only over that
   * row, and a page that reads it changed since says so (`resync`). */
  found?: unknown
}

/** The words of a save kept but waiting for approval, as the save bar says them. */
const PENDING = tk('errors.settingsSavedChangePending')

/** Why a save was refused, as the save bar says it: rules it cannot save field by field, or the daemon's words. */
export function SaveRefusal({ error }: { error: unknown }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const invalid = error instanceof ApiError && error.status === 400 && error.details.length > 0
  const words = invalid ? t('rules.invalid') : error instanceof RepoNotWritable ? t(key('projects.repo.readOnly')) : errorText(t, error)
  return (
    <div className="text-crit-ink">
      <p className="font-medium">{words}</p>
      {invalid && <RefusedDetails details={error.details} />}
    </div>
  )
}

export function useEditing<T>() {
  const { t } = useTranslation()
  const toast = useToast()
  const [editing, setEditing] = useState<Editing<T> | null>(null)
  // A row asked for while the one open holds changes: opened once those are let go.
  const [waiting, setWaiting] = useState<Editing<T> | null>(null)
  const [status, setStatus] = useState<ReactNode>(null)
  const [busy, setBusy] = useState(false)
  const dirty = editing !== null && JSON.stringify(editing.draft) !== JSON.stringify(editing.initial)

  const show = (next: Editing<T> | null) => {
    setEditing(next)
    setStatus(null)
  }
  const open = (next: Editing<T>) => (dirty ? setWaiting(next) : show(next))
  const close = () => show(null)
  const setDraft = (update: (draft: T) => T) => setEditing((current) => (current ? { ...current, draft: update(current.draft) } : current))

  /**
   * Saves the row open with `change`, on the settings `io` reads now (`found`: those the table shows). Saved, the
   * panel closes; kept but waiting for approval, it says so; refused because its row changed meanwhile, it shows the
   * row as it is now and says so; refused, it says why. `onSaved` reads the settings again.
   */
  const save = async <S, I = T>(
    io: SaveIo<S>,
    found: S,
    change: Change<S>,
    list: ListAccess<S, I>,
    saved: (version?: number | string) => string,
    onSaved: () => void,
    // The row of the list a draft is (a recognizer's form is not the recognizer): by default the draft itself.
    convert: { toItem: (draft: T) => I; toDraft: (item: I) => T } = { toItem: (draft) => draft as unknown as I, toDraft: (item) => item as unknown as T },
  ) => {
    if (!editing) return
    const current = editing
    const keyOf = (draft: T) => list.rows([convert.toItem(draft)])[0]?.key
    setBusy(true)
    setStatus(null)
    try {
      const outcome = await saveOne(io, found, change)
      if (outcome.kind === 'saved') {
        toast.show(saved(outcome.version))
        close()
        onSaved()
      } else if (outcome.kind === 'pending') {
        setEditing({ ...current, key: keyOf(current.draft) ?? current.key, initial: current.draft, found: convert.toItem(current.draft) })
        setStatus(<p className="font-medium text-crit-ink">{t(PENDING)}</p>)
        onSaved()
      } else if (outcome.kind === 'conflict') {
        const now = current.key === null ? undefined : list.rows(list.get(outcome.settings)).find((row) => row.key === current.key)
        const shown = afterConflict(current.key === null, now)
        if (shown === 'changed' && now) {
          const draft = convert.toDraft(now.item)
          setEditing({ key: now.key, source: current.source, initial: draft, draft, found: now.item })
          setStatus(<p className="font-medium text-crit-ink">{t('rules.panel.changed')}</p>)
        } else {
          // A new row whose name or value came meanwhile, or a row removed meanwhile: what was typed stays, as a new
          // row Save adds (once changed, for a name or value taken).
          setEditing({ ...current, key: null, found: undefined })
          setStatus(<p className="font-medium text-crit-ink">{shown === 'taken' ? t('rules.panel.taken') : t('rules.panel.gone')}</p>)
        }
        onSaved()
      } else {
        setStatus(<SaveRefusal error={outcome.error} />)
      }
    } finally {
      setBusy(false)
    }
  }

  /** Asks before letting go of the changes of the row open for the row asked for. */
  const question = (
    <ConfirmDialog
      open={waiting !== null}
      title={t('rules.panel.discardTitle')}
      confirmLabel={t('rules.panel.discard')}
      cancelLabel={t('rules.panel.keepEditing')}
      onConfirm={() => {
        show(waiting)
        setWaiting(null)
      }}
      onCancel={() => setWaiting(null)}
    >
      {t('rules.panel.discardBody')}
    </ConfirmDialog>
  )

  /** Where a new row is saved (Save to, in a project). */
  const setSource = (source: RuleSource) => setEditing((current) => (current ? { ...current, source } : current))

  /**
   * The row open, as the page reads it now (undefined: gone), when it is not as the panel found it: without edits the
   * panel shows it (or closes, the row gone); with edits, it says the row changed and shows it as it is now (the row
   * gone: what was typed stays, as a new row). Never a save over a change the panel did not see.
   */
  const resync = <I,>(now: I | undefined, toDraft: (item: I) => T) => {
    if (!editing || editing.key === null || editing.found === undefined) return
    const change = rowChange(editing.found, now, dirty)
    if (change === 'same') return
    if (change === 'close') return close()
    if (change === 'gone') {
      setEditing({ ...editing, key: null, found: undefined })
      setStatus(<p className="font-medium text-crit-ink">{t('rules.panel.gone')}</p>)
      return
    }
    const draft = toDraft(now as I)
    setEditing({ ...editing, initial: draft, draft, found: now })
    setStatus(change === 'changed' ? <p className="font-medium text-crit-ink">{t('rules.panel.changed')}</p> : null)
  }

  return { editing, open, close, setDraft, setSource, resync, dirty, busy, status, save, question }
}

/**
 * What an action on the rows selected says once done: saved, kept but waiting for approval, or nothing done because
 * one of them changed meanwhile (the table shows them as they are now). A refusal is thrown, for the table to say.
 */
export function useSettle() {
  const { t } = useTranslation()
  const toast = useToast()
  return <S,>(outcome: SaveOutcome<S>, saved: (version?: number | string) => string, onSaved: () => void) => {
    if (outcome.kind === 'error') throw outcome.error
    if (outcome.kind === 'saved') toast.show(saved(outcome.version))
    else if (outcome.kind === 'pending') toast.show(t(PENDING), 'fail')
    else toast.show(t('rules.table.changed'), 'fail')
    onSaved()
  }
}
