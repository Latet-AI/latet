// The bar at the foot of the rules, in sight while the page scrolls: a row for each set of settings the page shows
// (the global rules; your settings for a project, its latet.yaml), with whether it holds unsaved changes or why its
// last save was refused, then Discard and Save.

import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../api/client'
import { RefusedDetails } from './rules'
import { errorText } from './ui'

export function SaveBar({ children }: { children: ReactNode }) {
  return (
    <div className="sticky bottom-0 z-10 -mx-7 mt-2 border-t border-line bg-canvas/95 px-7 py-3 backdrop-blur-sm max-lg:-mx-5 max-lg:px-5">
      <div className="space-y-3">{children}</div>
    </div>
  )
}

export function SaveRow({
  dirty,
  saving,
  error,
  conflict,
  saved,
  saveLabel,
  onDiscard,
  onSave,
  disabled = false,
  force = false,
}: {
  dirty: boolean
  saving: boolean
  /** Why the last save was refused, or did not complete. */
  error: unknown
  /** What a 409 says here (the repository file changed since it was read); others say the daemon's words. */
  conflict?: string
  /** What shows once saved, with nothing changed since (else: no changes). */
  saved?: ReactNode
  saveLabel: string
  onDiscard: () => void
  onSave: () => void
  disabled?: boolean
  /** Saving is offered with nothing changed (creating an empty file). */
  force?: boolean
}) {
  const { t } = useTranslation()
  const refusal = (refused: unknown) =>
    refused instanceof ApiError && refused.status === 409 && conflict !== undefined
      ? conflict
      : refused instanceof ApiError && refused.status === 400 && refused.details.length > 0
        ? t('rules.invalid')
        : errorText(t, refused)
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="min-w-0 text-ui" role="status">
        {error ? (
          <div className="text-crit-ink">
            <p className="font-medium">{refusal(error)}</p>
            {error instanceof ApiError && error.details.length > 0 && <RefusedDetails details={error.details} />}
          </div>
        ) : dirty ? (
          <span className="text-ink">{t('rules.unsaved')}</span>
        ) : (
          (saved ?? <span className="text-ink-3">{t('rules.upToDate')}</span>)
        )}
      </div>
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn" disabled={!dirty || saving} onClick={onDiscard}>
          {t('rules.discard')}
        </button>
        <button type="button" className="btn btn-primary" disabled={(!dirty && !force) || saving || disabled} onClick={onSave}>
          {saving ? t('rules.saving') : saveLabel}
        </button>
      </div>
    </div>
  )
}
