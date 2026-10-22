import { useTranslation } from 'react-i18next'
import { Icon, type IconName } from '../Icon'

interface PrimaryButtonProps {
  label: string
  icon?: IconName
  /** Its step is running: the button stays, dimmed, and does nothing. It is not `disabled`, which would drop the focus on it. */
  busy?: boolean
  onClick: () => void
}

/** The one thing to do next on the tutorial's page: Enter anywhere on the page does it too. */
export function PrimaryButton({ label, icon, busy = false, onClick }: PrimaryButtonProps) {
  const { t } = useTranslation()
  return (
    <span className="inline-flex items-center gap-2">
      <button
        type="button"
        className="btn btn-primary"
        aria-disabled={busy || undefined}
        aria-busy={busy || undefined}
        onClick={busy ? undefined : onClick}
        // Enter held down on the button would press it again at each repeat (the page's own Enter ignores repeats).
        onKeyDown={(event) => {
          if (event.key === 'Enter' && event.repeat) event.preventDefault()
        }}
      >
        {icon && <Icon name={icon} size={14} />}
        {label}
      </button>
      <kbd
        className={`${busy ? 'invisible ' : ''}rounded-xs border border-line-2 bg-surface px-1 py-px font-sans text-micro text-ink-3 max-sm:hidden`}
        title={t('tutorial.keyHelp')}
      >
        {t('tutorial.key')}
      </kbd>
    </span>
  )
}
