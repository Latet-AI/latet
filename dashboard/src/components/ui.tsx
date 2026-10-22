import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError, NetworkError, UnauthorizedError } from '../api/client'
import type { DocsTarget } from '../lib/docsLinks'
import { splitInlineCode } from '../lib/inlineCode'
import { refusedWords, savedChangePendingWords } from '../lib/refused'
import { pageOnServer } from '../lib/serverPage'
import { DocsPageLink } from './DocsLinks'
import { Icon, type IconName } from './Icon'

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // Clipboard API unavailable: fall back to a temporary selection.
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    let ok: boolean
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    area.remove()
    return ok
  }
}

export function CopyButton({ text, label, compact = false }: { text: string; label?: string; compact?: boolean }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  const name = label ?? t('common.copy')
  return (
    <button
      type="button"
      className={compact ? 'btn btn-ghost btn-sm btn-icon' : 'btn btn-sm'}
      onClick={async () => {
        if (await copyText(text)) {
          setCopied(true)
          clearTimeout(timer.current)
          timer.current = setTimeout(() => setCopied(false), 1600)
        }
      }}
      aria-label={compact ? name : undefined}
      title={compact ? name : undefined}
    >
      <Icon name={copied ? 'check' : 'copy'} size={14} />
      {!compact && <span>{copied ? t('common.copied') : name}</span>}
      {compact && copied && <span className="sr-only">{t('common.copied')}</span>}
    </button>
  )
}

/** A command the user can copy and paste in a terminal. */
export function CommandLine({ command }: { command: string }) {
  return (
    <div className="well flex items-center gap-2 py-1 pr-1 pl-3">
      <span className="mono text-ink-3 select-none" aria-hidden="true">
        {'$'}
      </span>
      <code className="mono min-w-0 flex-1 truncate text-ui text-ink">{command}</code>
      <CopyButton text={command} compact />
    </div>
  )
}

/** Text from the daemon where `backticks` mark commands. */
export function InlineCodeText({ text }: { text: string }) {
  return (
    <>
      {splitInlineCode(text).map((part, i) =>
        part.code ? (
          <code key={i} className="mono rounded-xs bg-wash-2 px-1 py-px text-[0.95em] whitespace-nowrap text-ink">
            {part.text}
          </code>
        ) : (
          <span key={i}>{part.text}</span>
        ),
      )}
    </>
  )
}

export function PageHeader({
  title,
  description,
  actions,
  docs,
}: {
  title: string
  description?: ReactNode
  actions?: ReactNode
  /** The documentation of the page: "Documentation ↗" at the top right, on the title's line, apart from the actions. */
  docs?: DocsTarget
}) {
  const heading = <h1 className="min-w-0 font-display text-page font-extrabold tracking-[-0.01em] text-ink">{title}</h1>
  return (
    <header className="pb-5">
      {docs && (
        <div className="flex items-center justify-between gap-x-6">
          {heading}
          <span className="flex-none">
            <DocsPageLink target={docs} />
          </span>
        </div>
      )}
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 max-w-[72ch]">
          {!docs && heading}
          {description && <p className="mt-1 text-body text-ink-2">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </header>
  )
}

export function EmptyState({
  icon = 'info',
  title,
  children,
  action,
}: {
  icon?: IconName
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-start gap-3 px-6 py-12 sm:items-center sm:text-center">
      <span className="grid size-9 place-items-center rounded-md border border-line bg-well text-ink-3">
        <Icon name={icon} size={18} />
      </span>
      <div className="max-w-[56ch]">
        <h2 className="text-title font-semibold text-ink">{title}</h2>
        {children && <div className="mt-1.5 text-body text-ink-2">{children}</div>}
      </div>
      {action}
    </div>
  )
}

/** What went wrong, in the reader's language. A failure of the page's own server is said as a computer's daemon's or
 * as a Latet server's by the page's address (`pageOnServer`): no `/me` answer is needed, nor at hand in a toast. */
export function errorText(t: (key: string, options?: Record<string, unknown>) => string, error: unknown): string {
  if (error instanceof NetworkError) return pageOnServer() ? t('errors.networkServer') : t('errors.network')
  if (error instanceof UnauthorizedError) return t('errors.unauthorized')
  const pending = savedChangePendingWords(t, error)
  if (pending !== null) return pending
  const refused = refusedWords(t, error) // a save of your settings a Latet server did not keep: why, in your language
  if (refused !== null) return refused
  if (error instanceof ApiError) {
    const said = { status: error.status, message: error.message }
    return pageOnServer() ? t('errors.httpServer', said) : t('errors.http', said)
  }
  return t('errors.unexpected', { message: error instanceof Error ? error.message : String(error) })
}

export function ErrorState({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  const { t } = useTranslation()
  return (
    <div role="alert" className="flex flex-col items-start gap-3 px-6 py-10 sm:items-center sm:text-center">
      <span className="grid size-9 place-items-center rounded-md border border-line bg-well text-crit-ink">
        <Icon name="ban" size={18} />
      </span>
      <div className="max-w-[60ch]">
        <h2 className="text-title font-semibold text-ink">{title ?? t('errors.title')}</h2>
        <p className="mt-1.5 text-body text-ink-2">
          <InlineCodeText text={errorText(t, error)} />
        </p>
      </div>
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry}>
          <Icon name="refresh" size={14} />
          {t('common.retry')}
        </button>
      )}
    </div>
  )
}

/** Inline error under a form or an action. */
export function InlineError({ error }: { error: unknown }) {
  const { t } = useTranslation()
  return (
    <p role="alert" className="flex items-start gap-1.5 text-meta text-crit-ink">
      <Icon name="ban" size={14} className="mt-px flex-none" />
      <span>
        <InlineCodeText text={errorText(t, error)} />
      </span>
    </p>
  )
}

/** First load: three bars drawing in, the mark of the app. */
export function Loading({ label }: { label?: string }) {
  const { t } = useTranslation()
  return (
    <div className="flex items-center gap-3 px-6 py-12 text-ui text-ink-3 sm:justify-center" role="status">
      <span className="flex flex-col gap-[3px]" aria-hidden="true">
        <span className="h-[3px] w-5 animate-pulse rounded-[1px] bg-line-2" />
        <span className="h-[3px] w-3.5 animate-pulse rounded-[1px] bg-brand" />
        <span className="h-[3px] w-4 animate-pulse rounded-[1px] bg-line-2" />
      </span>
      {label ?? t('common.loading')}
    </div>
  )
}

export function Switch({
  checked,
  onChange,
  label,
  description,
  disabled,
}: {
  checked: boolean
  onChange: (checked: boolean) => void
  label: ReactNode
  description?: ReactNode
  disabled?: boolean
}) {
  const id = useId()
  return (
    <label htmlFor={id} className={`flex items-start gap-2.5 ${disabled ? 'cursor-not-allowed' : 'cursor-pointer'}`}>
      <input
        id={id}
        type="checkbox"
        role="switch"
        className="peer sr-only"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="switch mt-px" aria-hidden="true" />
      <span className="min-w-0">
        <span className="block text-ui text-ink">{label}</span>
        {description && <span className="mt-0.5 block text-meta text-ink-3">{description}</span>}
      </span>
    </label>
  )
}

export interface SegmentOption<V extends string> {
  value: V
  label: string
  icon?: IconName
  /** Hide the label visually (icon-only), keeping it for screen readers. */
  iconOnly?: boolean
}

/**
 * A small radio group drawn as a segmented control (`.seg`, `.seg-item` in index.css): the choices in a well, the
 * chosen one raised. As tall as a button (`md`) or a small one (`sm`), so that it sits in a row of them.
 */
export function Segmented<V extends string>({
  value,
  options,
  onChange,
  label,
  size = 'md',
}: {
  value: V
  options: readonly SegmentOption<V>[]
  onChange: (value: V) => void
  label: string
  size?: 'sm' | 'md'
}) {
  const name = useId()
  return (
    <div role="radiogroup" aria-label={label} className="seg">
      {options.map((option) => {
        const selected = option.value === value
        return (
          <label
            key={option.value}
            title={option.iconOnly ? option.label : undefined}
            className={`seg-item relative py-0 ${size === 'sm' ? 'h-[18px] gap-1 px-1.5 text-micro' : 'h-[22px] px-2.5 text-meta'}`}
          >
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={selected}
              onChange={() => onChange(option.value)}
              className="peer sr-only"
            />
            {option.icon && <Icon name={option.icon} size={13} />}
            <span className={option.iconOnly ? 'sr-only' : undefined}>{option.label}</span>
            <span className="pointer-events-none absolute inset-0 rounded-[inherit] peer-focus-visible:outline-2 peer-focus-visible:outline-offset-1 peer-focus-visible:outline-[var(--focus)]" />
          </label>
        )
      })}
    </div>
  )
}

/** A native modal dialog asking to confirm a destructive action. */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  onConfirm,
  onCancel,
  busy,
  destructive = true,
  cancelLabel,
}: {
  open: boolean
  title: string
  children: ReactNode
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
  busy?: boolean
  /** The action deletes something: the confirm button says so in the critical color. */
  destructive?: boolean
  /** What the button that changes nothing says (Cancel). */
  cancelLabel?: string
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
        onCancel()
      }}
      className="floating m-auto w-[420px] max-w-[calc(100vw-32px)] p-0 backdrop:bg-[rgb(10_11_13/0.45)]"
    >
      <div className="p-5">
        <h2 id={titleId} className="text-title font-semibold text-ink">
          {title}
        </h2>
        <div className="mt-2 text-body text-ink-2">{children}</div>
      </div>
      <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
        <button type="button" className="btn" onClick={onCancel} autoFocus>
          {cancelLabel ?? t('common.cancel')}
        </button>
        <button type="button" className={`btn ${destructive ? 'btn-destructive' : 'btn-primary'}`} onClick={onConfirm} disabled={busy}>
          {confirmLabel}
        </button>
      </div>
    </dialog>
  )
}

/** A labelled value in a definition grid. */
export function Meta({ label, children, wrap = false }: { label: string; children: ReactNode; wrap?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-meta text-ink-3">{label}</dt>
      <dd className={`mt-0.5 min-w-0 text-ui text-ink ${wrap ? '' : 'truncate'}`}>{children}</dd>
    </div>
  )
}
