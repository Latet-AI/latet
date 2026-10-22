import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import { useDeploymentKey } from '../lib/me'
import { Icon } from './Icon'
import { useToast } from './Toast'
import { ConfirmDialog, errorText } from './ui'

/** "Clear history" with its confirmation: deletes the recorded exchanges, keeps the vault. */
export function ClearHistoryButton({ onCleared, variant = 'ghost' }: { onCleared?: () => void; variant?: 'ghost' | 'normal' }) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  const clear = async () => {
    setBusy(true)
    try {
      await api.clearHistory()
      toast.show(t('history.clear.done'))
      setOpen(false)
      onCleared?.()
    } catch (error) {
      toast.show(errorText(t, error), 'fail')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button type="button" className={`btn btn-danger ${variant === 'ghost' ? 'btn-ghost' : ''}`} onClick={() => setOpen(true)}>
        <Icon name="trash" size={14} />
        {t('history.clear.action')}
      </button>
      <ConfirmDialog
        open={open}
        title={t('history.clear.title')}
        confirmLabel={t('history.clear.confirm')}
        onConfirm={() => void clear()}
        onCancel={() => setOpen(false)}
        busy={busy}
      >
        <p>{t(key('history.clear.body'))}</p>
      </ConfirmDialog>
    </>
  )
}
