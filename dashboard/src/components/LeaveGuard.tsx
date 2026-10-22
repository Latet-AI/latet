// Leaving a page with an edit not saved (a panel's row, the save bar's settings) asks first: a link, the sidebar,
// another project in the selector, and the browser's tab closed or reloaded. Another tab of the same page asks nothing.

import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { useBlocker } from 'react-router'
import { anyUnsaved, leavesPage, useAnyUnsaved } from '../lib/unsaved'
import { ConfirmDialog } from './ui'

export function LeaveGuard() {
  const { t } = useTranslation()
  const unsaved = useAnyUnsaved()
  const blocker = useBlocker(({ currentLocation, nextLocation }) => anyUnsaved() && leavesPage(currentLocation, nextLocation))

  // The browser asks in its own words when its tab is closed or reloaded.
  useEffect(() => {
    if (!unsaved) return
    const ask = (event: BeforeUnloadEvent) => {
      event.preventDefault()
    }
    window.addEventListener('beforeunload', ask)
    return () => window.removeEventListener('beforeunload', ask)
  }, [unsaved])

  return (
    <ConfirmDialog
      open={blocker.state === 'blocked'}
      title={t('rules.leave.title')}
      confirmLabel={t('rules.leave.confirm')}
      cancelLabel={t('rules.leave.stay')}
      onConfirm={() => blocker.proceed?.()}
      onCancel={() => blocker.reset?.()}
    >
      {t('rules.leave.body')}
    </ConfirmDialog>
  )
}
