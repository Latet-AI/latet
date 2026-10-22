// Whose rules the Rules page shows: the global ones, or one project's.

import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router'
import { api } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Icon } from './Icon'
import { errorText } from './ui'

const ADD = '__add__'

export function rulesHref(projectId: string | null): string {
  return projectId ? `/rules?project=${encodeURIComponent(projectId)}` : '/rules'
}

export function ScopeBar({ current }: { current: string | null }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const projects = useAsync('projects', (signal) => api.projects(signal))
  const [adding, setAdding] = useState(false)
  const [path, setPath] = useState('')
  const [error, setError] = useState<unknown>(null)
  const items = projects.data?.items ?? []

  const add = async () => {
    setError(null)
    try {
      const project = await api.addProject(path.trim())
      setAdding(false)
      setPath('')
      projects.reload()
      navigate(rulesHref(project.id))
    } catch (e) {
      setError(e)
    }
  }

  // Sticky: whichever section you scroll to, you see whose rules these are.
  return (
    <div className="sticky top-0 z-20 -mx-7 mb-5 space-y-2 border-b border-line bg-canvas/95 px-7 py-3 backdrop-blur-sm max-lg:-mx-5 max-lg:px-5">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="label">{t('projects.scope.label')}</span>
          <select
            className="field w-[340px] max-w-full"
            value={current ?? ''}
            onChange={(e) => {
              if (e.target.value === ADD) setAdding(true)
              else navigate(rulesHref(e.target.value || null))
            }}
          >
            <option value="">{t('projects.scope.global')}</option>
            {current && !items.some((p) => p.id === current) && <option value={current}>{current}</option>}
            {items.map((p) => (
              <option key={p.id} value={p.id}>
                {p.enabled ? p.name : t('projects.scope.offName', { name: p.name })}
              </option>
            ))}
            <option value={ADD}>{t('projects.scope.add')}</option>
          </select>
        </label>
        {current && (
          <p className="pb-2 text-meta text-ink-3">{t('projects.scope.hint')}</p>
        )}
      </div>
      {adding && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (path.trim()) void add()
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="label">{t('projects.scope.folder')}</span>
            <input className="field mono w-[420px] max-w-full" value={path} onChange={(e) => setPath(e.target.value)} placeholder={t('projects.scope.folderPlaceholder')} />
          </label>
          <button type="submit" className="btn" disabled={!path.trim()}>
            <Icon name="plus" size={14} />
            {t('projects.scope.addButton')}
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => setAdding(false)}>
            {t('common.cancel')}
          </button>
        </form>
      )}
      {error !== null && <p className="text-meta text-crit-ink">{errorText(t, error)}</p>}
    </div>
  )
}
