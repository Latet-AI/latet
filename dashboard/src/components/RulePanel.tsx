// The panel a row of a rules table opens in, with every field of that row, and its own Save: on a wide window a panel on
// the right with its own scroll, the table still in reach; on a narrow one (a phone) over the whole page, as a modal,
// Close at the top and the buttons always in sight at the bottom. Esc and Close ask before dropping an edit not saved;
// the focus moves in when it opens, and back to what opened it when it closes. It is put at the end of the page's body
// (a portal), so that nothing on the page hides it (the search at the top hiding its section); it stays, out of sight,
// while its tab is not the one shown, with what is being edited in it.

import { useEffect, useEffectEvent, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { useUnsaved } from '../lib/unsaved'
import { useNarrow } from '../lib/useNarrow'
import { DocsSectionLink, type DocsRef } from './DocsLinks'
import { Icon } from './Icon'
import { ConfirmDialog } from './ui'

/** What a panel's Tab reaches: its fields and buttons, in order. */
const FOCUSABLE = 'input:not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled]), a[href], [tabindex="0"]'

export interface RulePanelProps {
  open: boolean
  title: string
  /** Its fields hold an edit not saved: closing it asks first. */
  dirty: boolean
  /** A save runs: the fields wait. */
  busy: boolean
  /** Why it cannot be changed here, and where it can: no Save, the fields read only. */
  readOnly?: ReactNode
  onSave?: () => void
  /** Save is offered (the fields hold no problem). */
  canSave?: boolean
  onClose: () => void
  /** What the last save said: refused, waiting for approval, changed meanwhile. */
  status?: ReactNode
  /** Its tab is not the one shown: kept, out of sight. */
  hidden?: boolean
  /** The section of the documentation that explains its fields: a "?" at its top, named after its list. */
  docs?: DocsRef
  children?: ReactNode
}

export function RulePanel(props: RulePanelProps) {
  if (!props.open || typeof document === 'undefined') return null
  return createPortal(<RulePanelContent {...props} />, document.body)
}

/** The open panel itself (exported for its tests, which render no portal). */
export function RulePanelContent({ title, dirty, busy, readOnly, onSave, canSave = true, onClose, status, hidden = false, docs, children }: RulePanelProps) {
  const { t } = useTranslation()
  const narrow = useNarrow()
  const titleId = useId()
  // An edit not saved here: leaving the page asks first (LeaveGuard).
  useUnsaved(titleId, dirty)
  const panel = useRef<HTMLDivElement>(null)
  const [asking, setAsking] = useState(false)
  // What had the focus when it opened (the row, Add): it gets it back when it closes.
  const [opener] = useState<Element | null>(() => (typeof document === 'undefined' ? null : document.activeElement))

  useEffect(() => {
    const box = panel.current
    const first = box?.querySelector<HTMLElement>(`fieldset ${FOCUSABLE}`)
    ;(first ?? box)?.focus()
    return () => {
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [opener])

  // Over the whole page (a phone), the page behind it does not scroll.
  const covers = narrow && !hidden
  useEffect(() => {
    if (!covers) return
    const before = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = before
    }
  }, [covers])

  const close = () => (dirty ? setAsking(true) : onClose())
  // Esc closes it from anywhere on the page, but in a dialog of its own (its question, an import's preview).
  // Not a panel out of sight (left open in a tab not shown): its question would be a dialog nobody sees.
  const onEscape = useEffectEvent((event: globalThis.KeyboardEvent) => {
    if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector('dialog[open]')) return
    if (!panel.current || panel.current.getClientRects().length === 0) return
    event.preventDefault()
    close()
  })
  useEffect(() => {
    const listener = (event: globalThis.KeyboardEvent) => onEscape(event)
    document.addEventListener('keydown', listener)
    return () => document.removeEventListener('keydown', listener)
  }, [])

  // Over the whole page, Tab stays in the panel.
  const trap = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!narrow || event.key !== 'Tab') return
    const items = [...(panel.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])]
    const first = items[0]
    const last = items.at(-1)
    if (!first || !last) return
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return (
    <div
      ref={panel}
      role="dialog"
      aria-modal={narrow ? true : undefined}
      aria-labelledby={titleId}
      tabIndex={-1}
      hidden={hidden}
      onKeyDown={trap}
      data-rule-panel=""
      className={`fixed z-40 flex flex-col bg-surface outline-none ${
        narrow ? 'inset-0' : 'top-0 right-0 bottom-0 w-[44vw] max-w-[820px] min-w-[440px] border-l border-line-2 shadow-[var(--shadow)]'
      }`}
    >
      <div className="flex flex-none items-center justify-between gap-3 border-b border-line px-5 py-3">
        <div className="flex min-w-0 items-center gap-1">
          <h2 id={titleId} className="min-w-0 truncate text-title font-semibold text-ink" title={title}>
            {title}
          </h2>
          {docs && <DocsSectionLink {...docs} />}
        </div>
        <button type="button" className="btn btn-ghost btn-sm flex-none" onClick={close} aria-label={t('rules.panel.close')}>
          <Icon name="x" size={14} />
          {narrow && <span aria-hidden="true">{t('rules.panel.close')}</span>}
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {readOnly !== undefined && (
          <div className="mb-4 flex items-start gap-2 rounded-lg border border-line bg-well p-3 text-ui text-ink-2">
            <Icon name="info" size={14} className="mt-0.5 flex-none" />
            <div className="min-w-0">{readOnly}</div>
          </div>
        )}
        <fieldset disabled={readOnly !== undefined || busy} className="min-w-0 space-y-4">
          {children}
        </fieldset>
      </div>
      <div className="flex flex-none flex-wrap items-center justify-between gap-3 border-t border-line px-5 py-3">
        <div className="min-w-0 flex-1 text-ui" role="status">
          {status}
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn" onClick={close}>
            {readOnly !== undefined || !onSave ? t('rules.panel.close') : t('common.cancel')}
          </button>
          {onSave && readOnly === undefined && (
            <button type="button" className="btn btn-primary" disabled={busy || !canSave} onClick={onSave}>
              {busy ? t('rules.panel.saving') : t('rules.panel.save')}
            </button>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={asking}
        title={t('rules.panel.discardTitle')}
        confirmLabel={t('rules.panel.discard')}
        cancelLabel={t('rules.panel.keepEditing')}
        onConfirm={() => {
          setAsking(false)
          onClose()
        }}
        onCancel={() => setAsking(false)}
      >
        {t('rules.panel.discardBody')}
      </ConfirmDialog>
    </div>
  )
}
