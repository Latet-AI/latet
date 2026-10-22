import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Icon, StatusGlyph } from './Icon'

type Tone = 'ok' | 'fail' | 'info'

interface Toast {
  id: number
  tone: Tone
  message: string
}

interface ToastApi {
  show: (message: string, tone?: Tone) => void
}

const ToastContext = createContext<ToastApi>({ show: () => undefined })

export function useToast(): ToastApi {
  return useContext(ToastContext)
}

/** Short confirmations of actions ("Added to the allow list"), announced to screen readers. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const [toasts, setToasts] = useState<Toast[]>([])
  const next = useRef(1)

  const dismiss = useCallback((id: number) => setToasts((list) => list.filter((toast) => toast.id !== id)), [])

  const show = useCallback(
    (message: string, tone: Tone = 'ok') => {
      const id = next.current++
      setToasts((list) => [...list.slice(-3), { id, tone, message }])
      setTimeout(() => dismiss(id), tone === 'fail' ? 8000 : 4000)
    },
    [dismiss],
  )

  const api = useMemo(() => ({ show }), [show])

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        className="pointer-events-none fixed right-4 bottom-4 z-[60] flex w-[360px] max-w-[calc(100vw-32px)] flex-col gap-2"
        role="status"
        aria-live="polite"
        data-toasts
      >
        {toasts.map((toast) => (
          <div key={toast.id} className="floating pointer-events-auto flex items-start gap-2.5 px-3 py-2.5 text-ui">
            {toast.tone === 'info' ? (
              <Icon name="info" className="mt-px flex-none text-ink-2" />
            ) : (
              <span className="mt-px">
                <StatusGlyph status={toast.tone} />
              </span>
            )}
            <p className="min-w-0 flex-1 text-ink">{toast.message}</p>
            <button
              type="button"
              className="btn btn-ghost btn-sm btn-icon -my-1 -mr-1"
              onClick={() => dismiss(toast.id)}
              aria-label={t('common.dismiss')}
            >
              <Icon name="x" size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
