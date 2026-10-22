// The keyboard shortcuts of Approvals: their list, with the switch that turns them on, in a modal dialog. They are off
// until you turn them on, and this browser keeps the choice (`approvals.shortcuts` in lib/prefs).

import { useEffect, useId, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { tk } from '../i18n'
import { SHORTCUTS, type ShortcutAction } from '../lib/shortcuts'
import { Switch } from './ui'

const SHORTCUT_LABEL: Record<ShortcutAction, string> = {
  approve: tk('approvals.shortcuts.keys.approve'),
  skip: tk('approvals.shortcuts.keys.skip'),
  next: tk('approvals.shortcuts.keys.next'),
  previous: tk('approvals.shortcuts.keys.previous'),
  mask: tk('approvals.shortcuts.keys.mask'),
  approveAll: tk('approvals.shortcuts.keys.approveAll'),
  help: tk('approvals.shortcuts.keys.help'),
}

/** The key of a shortcut, as on the keyboard: a capital letter, Shift in the language of the page. */
export function ShortcutKeys({ action }: { action: ShortcutAction }) {
  const { t } = useTranslation()
  const shortcut = SHORTCUTS.find((s) => s.action === action)
  if (!shortcut) return null
  return (
    <span className="inline-flex items-center gap-1">
      {shortcut.shift && (
        <>
          <kbd className="kbd">{t('approvals.shortcuts.shift')}</kbd>+
        </>
      )}
      <kbd className="kbd">{shortcut.key.toUpperCase()}</kbd>
    </span>
  )
}

export function ShortcutsDialog({
  open,
  onClose,
  enabled,
  onEnabled,
}: {
  open: boolean
  onClose: () => void
  enabled: boolean
  onEnabled: (enabled: boolean) => void
}) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault()
        onClose()
      }}
      className="floating m-auto w-[460px] max-w-[calc(100vw-32px)] p-0 backdrop:bg-[rgb(10_11_13/0.45)]"
    >
      <div className="p-5">
        <h2 id={titleId} className="text-title font-semibold text-ink">
          {t('approvals.shortcuts.title')}
        </h2>
        <p className="mt-2 text-body text-ink-2">{t('approvals.shortcuts.intro')}</p>
        <div className="mt-4">
          <Switch checked={enabled} onChange={onEnabled} label={t('approvals.shortcuts.switch')} />
        </div>
        <dl className="mt-4 divide-y divide-line border-y border-line">
          {SHORTCUTS.map((shortcut) => (
            <div key={shortcut.action} className="flex items-center gap-4 py-1.5" data-shortcut={shortcut.action}>
              <dt className="w-24 flex-none text-meta text-ink-2">
                <ShortcutKeys action={shortcut.action} />
              </dt>
              <dd className="min-w-0 text-ui text-ink">{t(SHORTCUT_LABEL[shortcut.action])}</dd>
            </div>
          ))}
        </dl>
      </div>
      <div className="flex justify-end border-t border-line px-5 py-3">
        <button type="button" className="btn" onClick={onClose}>
          {t('common.close')}
        </button>
      </div>
    </dialog>
  )
}
