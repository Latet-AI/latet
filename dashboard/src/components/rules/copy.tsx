// "Copy to a project…": rows of the global rules added to your own settings for a project you pick, those already
// there left as they are. Saved on your settings as they are now, as a panel's change is.

import { useEffect, useId, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import type { PersonalSettings } from '../../api/types'
import { personalIo, saveOne, type ListAccess } from '../../lib/panelSave'
import { useAsync } from '../../lib/useAsync'
import { Icon } from '../Icon'
import { useToast } from '../Toast'
import { errorText } from '../ui'
import { SaveRefusal } from './editing'

/** `onClose(true)` once the copy is saved; `onClose(false)` when it is cancelled. */
export function CopyToProject<T>({ items, list, onClose }: { items: T[]; list: ListAccess<PersonalSettings, T>; onClose: (copied: boolean) => void }) {
  const { t } = useTranslation()
  const toast = useToast()
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const projects = useAsync('copy-projects', (signal) => api.projects(signal))
  const [chosen, setChosen] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<unknown>(null)
  const choices = projects.data?.items ?? []
  const project = choices.find((p) => p.id === chosen) ?? choices[0]

  useEffect(() => {
    const dialog = ref.current
    if (dialog && !dialog.open) dialog.showModal()
  }, [])

  const copy = async () => {
    if (!project) return
    setBusy(true)
    setRefused(null)
    let added = 0
    try {
      const outcome = await saveOne(personalIo(project.id), {} as PersonalSettings, {
        apply: (settings) => {
          const current = list.get(settings)
          const there = new Set(list.rows(current).map((row) => row.key))
          const fresh: T[] = []
          for (const item of items) {
            const key = list.rows([item])[0]?.key
            if (key !== undefined && !there.has(key)) {
              there.add(key)
              fresh.push(item)
            }
          }
          added = fresh.length
          return list.set(settings, [...current, ...fresh])
        },
        unchanged: () => true,
      })
      if (outcome.kind === 'error') {
        setRefused(outcome.error)
        return
      }
      if (outcome.kind === 'pending') toast.show(t('errors.settingsSavedChangePending'), 'fail')
      else toast.show(added > 0 ? t('rules.copy.done', { count: added, project: project.name }) : t('rules.copy.nothing', { project: project.name }))
      onClose(true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault()
        onClose(false)
      }}
      className="floating m-auto w-[460px] max-w-[calc(100vw-32px)] p-0 backdrop:bg-[rgb(10_11_13/0.45)]"
    >
      <div className="space-y-3 p-5">
        <h2 id={titleId} className="text-title font-semibold text-ink">
          {t('rules.copy.title')}
        </h2>
        <p className="text-body text-ink-2">{t('rules.copy.help', { count: items.length })}</p>
        {projects.error !== undefined && projects.error !== null && !projects.data ? (
          <p className="text-meta text-crit-ink">{errorText(t, projects.error)}</p>
        ) : projects.data && choices.length === 0 ? (
          <p className="text-ui text-ink-2">{t('rules.copy.none')}</p>
        ) : (
          <label className="flex flex-col gap-1">
            <span className="label">{t('rules.copy.project')}</span>
            <select className="field" value={project?.id ?? ''} onChange={(e) => setChosen(e.target.value)} disabled={!projects.data}>
              {choices.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {refused !== null && <SaveRefusal error={refused} />}
      </div>
      <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
        <button type="button" className="btn" onClick={() => onClose(false)} autoFocus>
          {t('common.cancel')}
        </button>
        <button type="button" className="btn btn-primary" disabled={busy || !project} onClick={() => void copy()}>
          <Icon name="copy" size={14} />
          {t('rules.copy.confirm')}
        </button>
      </div>
    </dialog>
  )
}
