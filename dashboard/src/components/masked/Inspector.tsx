import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ContextType,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router'
import { api } from '../../api/client'
import { strictField, technicalKind, unlocked, type TechnicalKind } from '../../lib/fields'
import { CLASS_LABEL, MODULE_LABEL, VIA_LABEL } from '../../lib/labels'
import { useDeploymentKey, useMe } from '../../lib/me'
import { savedChangePending } from '../../lib/refused'
import { tk } from '../../i18n'
import { ClassSwatch } from '../badges'
import { RuleName, RuleScope } from '../RuleName'
import { useFloatingPosition } from '../Floating'
import { Icon } from '../Icon'
import { useToast } from '../Toast'
import { errorText } from '../ui'
import { chooseValue, useWhere, WhereChoice } from '../ValueScope'
import type { SpanInfo } from './types'

/** The project of the exchange a mark is in (the inspector is rendered outside the page). */
type Scope = ContextType<typeof RuleScope>

interface InspectorApi {
  /** Pointer over a mark: show after a short delay. */
  hover: (el: HTMLElement, info: SpanInfo, scope?: Scope) => void
  /** Pointer left a mark: hide unless it moved into the inspector. */
  leave: () => void
  /** Keyboard focus on a mark: show at once. */
  focus: (el: HTMLElement, info: SpanInfo, scope?: Scope) => void
  blur: () => void
  /** Click or Enter: keep it open and move focus into it. */
  pin: (el: HTMLElement, info: SpanInfo, scope?: Scope) => void
  /** Whether span actions (allow, always mask) are offered. */
  actions: boolean
}

const InspectorContext = createContext<InspectorApi | null>(null)

/** The page's inspector, told the project of the marks that use it. */
export function useInspector(): InspectorApi | null {
  const inspector = useContext(InspectorContext)
  const scope = useContext(RuleScope)
  return useMemo(
    () =>
      inspector && scope
        ? {
            ...inspector,
            hover: (el, info) => inspector.hover(el, info, scope),
            focus: (el, info) => inspector.focus(el, info, scope),
            pin: (el, info) => inspector.pin(el, info, scope),
          }
        : inspector,
    [inspector, scope],
  )
}

interface Current {
  el: HTMLElement
  info: SpanInfo
  pinned: boolean
  scope: Scope
}

function setPairActive(pair: string | null, active: boolean) {
  if (!pair) return
  for (const el of document.querySelectorAll<HTMLElement>(`[data-pair="${CSS.escape(pair)}"]`)) {
    if (active) el.setAttribute('data-active', '')
    else el.removeAttribute('data-active')
  }
}

/**
 * One inspector for a page: the popover that explains a masked value (type, class, rule, entity) and
 * offers "Not sensitive, always allow" and "Always mask this". It highlights the value's twin in the
 * other pane while open. `onPin` is told of each value the user opens (click, or Enter on it), as
 * the tutorial's "click the highlighted value" steps need; hovering is no pin.
 */
export function InspectorProvider({
  children,
  actions = true,
  onPin,
}: {
  children: ReactNode
  actions?: boolean
  onPin?: (info: SpanInfo) => void
}) {
  const [current, setCurrent] = useState<Current | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const overPopover = useRef(false)
  const activePair = useRef<string | null>(null)
  // The latest handler, so a new one does not change what the marks hold.
  const pinned = useRef(onPin)
  useEffect(() => {
    pinned.current = onPin
  })

  const open = useCallback((next: Current) => {
    clearTimeout(timer.current)
    if (activePair.current !== next.info.pair) {
      setPairActive(activePair.current, false)
      activePair.current = next.info.pair
      setPairActive(next.info.pair, true)
    }
    setCurrent(next)
  }, [])

  const close = useCallback(() => {
    clearTimeout(timer.current)
    setPairActive(activePair.current, false)
    activePair.current = null
    overPopover.current = false
    setCurrent(null)
  }, [])

  const value = useMemo<InspectorApi>(
    () => ({
      actions,
      hover: (el, info, scope = null) => {
        clearTimeout(timer.current)
        timer.current = setTimeout(() => {
          setCurrent((prev) => {
            if (prev?.pinned) return prev
            if (activePair.current !== info.pair) {
              setPairActive(activePair.current, false)
              activePair.current = info.pair
              setPairActive(info.pair, true)
            }
            return { el, info, pinned: false, scope }
          })
        }, 120)
      },
      leave: () => {
        clearTimeout(timer.current)
        timer.current = setTimeout(() => {
          if (overPopover.current) return
          setCurrent((prev) => {
            if (prev?.pinned) return prev
            setPairActive(activePair.current, false)
            activePair.current = null
            return null
          })
        }, 220)
      },
      focus: (el, info, scope = null) => open({ el, info, pinned: false, scope }),
      blur: () => {
        clearTimeout(timer.current)
        timer.current = setTimeout(() => {
          if (overPopover.current) return
          setCurrent((prev) => {
            if (prev?.pinned) return prev
            setPairActive(activePair.current, false)
            activePair.current = null
            return null
          })
        }, 150)
      },
      pin: (el, info, scope = null) => {
        open({ el, info, pinned: true, scope })
        pinned.current?.(info)
      },
    }),
    [actions, open],
  )

  useEffect(() => () => clearTimeout(timer.current), [])

  return (
    <InspectorContext.Provider value={value}>
      {children}
      {current &&
        createPortal(
          <RuleScope.Provider value={current.scope}>
            <SpanPopover
              key={`${current.info.pair}:${current.info.side}`}
              current={current}
              actions={actions}
              onClose={(restoreFocus) => {
                const el = current.el
                close()
                if (restoreFocus && el.isConnected) el.focus({ preventScroll: true })
              }}
              onPointer={(inside) => {
                overPopover.current = inside
                if (!inside && !current.pinned) value.leave()
                if (inside) clearTimeout(timer.current)
              }}
            />
          </RuleScope.Provider>,
          document.body,
        )}
    </InspectorContext.Provider>
  )
}

type ActionState = 'idle' | 'busy' | 'done' | 'pending'

function SpanPopover({
  current,
  actions,
  onClose,
  onPointer,
}: {
  current: Current
  actions: boolean
  onClose: (restoreFocus: boolean) => void
  onPointer: (inside: boolean) => void
}) {
  const { t } = useTranslation()
  const key = useDeploymentKey()
  const toast = useToast()
  const titleId = useId()
  const { el, info, pinned } = current
  const floatingRef = useFloatingPosition<HTMLDivElement>(el, true, 'bottom', 8)
  const [revealed, setRevealed] = useState<string | null>(
    (info.side === 'original' || info.side === 'agent') && info.known ? info.original : null,
  )
  const [revealError, setRevealError] = useState<unknown>(null)
  const [allowState, setAllowState] = useState<ActionState>('idle')
  const [termState, setTermState] = useState<ActionState>('idle')
  const { reloadConfig } = useMe()
  // Pinned: focus moves into the popover; Escape or a click outside closes it.
  useEffect(() => {
    const floating = floatingRef.current
    if (!pinned || !floating) return
    floating.focus({ preventScroll: true })
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose(true)
      }
    }
    const onDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (!floating.contains(target) && !el.contains(target)) onClose(false)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('pointerdown', onDown, true)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('pointerdown', onDown, true)
    }
  }, [pinned, floatingRef, el, onClose])

  useEffect(() => {
    if (pinned) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pinned, onClose])

  /** The real value: known, or fetched from the vault for values Latet only saw masked. */
  const resolveOriginal = async (): Promise<string | null> => {
    if (info.original !== null) return info.original
    if (!info.entity) return null
    const { original } = await api.reveal(info.entity)
    return original
  }

  const reveal = async () => {
    setRevealError(null)
    try {
      setRevealed(await resolveOriginal())
    } catch (error) {
      setRevealError(error)
    }
  }

  const { hasProject, where, setWhere, projectId } = useWhere()

  const allow = async () => {
    setAllowState('busy')
    try {
      // A stopped value read more than one way is allowed in every way it was read, or the check stops again on the rest.
      const values = info.texts ?? [await resolveOriginal()]
      for (const value of values) {
        if (value === null) throw new Error(t('inspector.noOriginal'))
        await chooseValue({ kind: 'allow', value }, projectId)
      }
      setAllowState('done')
      toast.show(projectId ? t('inspector.allowedInProject', { type: info.type }) : t('inspector.allowed', { type: info.type }))
    } catch (error) {
      // One value may have been kept before its approval refresh failed; the rest of this loop did not run.
      const pending = savedChangePending(error)
      setAllowState(pending ? 'pending' : 'idle')
      if (pending) reloadConfig()
      toast.show(errorText(t, error), 'fail')
    }
  }

  const alwaysMask = async () => {
    setTermState('busy')
    try {
      const value = await resolveOriginal()
      if (value === null) throw new Error(t('inspector.noOriginal'))
      await chooseValue({ kind: 'mask', value, type: info.type }, projectId)
      setTermState('done')
      toast.show(projectId ? t('inspector.termAddedInProject', { type: info.type }) : t('inspector.termAdded', { type: info.type }))
    } catch (error) {
      const pending = savedChangePending(error)
      setTermState(pending ? 'pending' : 'idle')
      if (pending) reloadConfig()
      toast.show(errorText(t, error), 'fail')
    }
  }

  const canResolve = info.known && (info.original !== null || info.entity !== null)
  const realSide = info.side === 'original' || info.side === 'agent'
  const field = strictField(info.rule)

  return (
    <div
      ref={floatingRef}
      role="dialog"
      aria-labelledby={titleId}
      tabIndex={-1}
      className="floating fixed z-50 w-[340px] max-w-[calc(100vw-16px)] text-ui outline-none"
      style={{ top: 0, left: 0, visibility: 'hidden' }}
      onPointerEnter={() => onPointer(true)}
      onPointerLeave={() => onPointer(false)}
    >
      <div className="flex items-start justify-between gap-3 border-b border-line px-3 pt-2.5 pb-2">
        <div className="min-w-0">
          <p id={titleId} className="mono truncate text-ui font-semibold text-ink">
            {info.footnote !== null && <span className="mr-1.5 font-sans text-meta text-ink-3">{info.footnote}</span>}
            {info.type}
          </p>
          <p className="mt-0.5 flex items-center gap-1.5 text-meta text-ink-2">
            <ClassSwatch cls={info.cls} />
            {t(CLASS_LABEL[info.cls])}
            {info.redacted && <span className="text-ink-3">{t('inspector.redactedNote')}</span>}
          </p>
          {info.stopped && <p className="mt-1 text-meta font-medium text-crit-ink">{t(key('inspector.stopped'))}</p>}
          {info.json && <p className="mt-0.5 text-micro text-ink-3">{t('inspector.jsonNote')}</p>}
        </div>
        {pinned && (
          <button type="button" className="btn btn-ghost btn-sm btn-icon -mt-0.5 -mr-1" onClick={() => onClose(true)} aria-label={t('common.close')}>
            <Icon name="x" size={14} />
          </button>
        )}
      </div>

      <dl className="grid grid-cols-[84px_1fr] gap-x-3 gap-y-1.5 px-3 py-2.5 text-meta">
        <dt className="text-ink-3">{info.stopped ? t('inspector.value') : t('inspector.sentAs')}</dt>
        {/* A key or a certificate is many lines: the value scrolls, the button stays in reach. */}
        <dd className={`mono min-w-0 break-all text-ink ${info.stopped ? 'max-h-28 overflow-y-auto' : ''}`}>{info.sent}</dd>
        {!info.stopped && (
          <>
            <dt className="text-ink-3">{realSide ? t('inspector.realValue') : t('inspector.replaces')}</dt>
            <dd className="min-w-0">
              {revealed !== null ? (
                <span className="mono break-all text-ink">{revealed}</span>
              ) : canResolve ? (
                <button type="button" className="link text-meta" onClick={() => void reveal()}>
                  {info.original === null ? t('inspector.revealFromVault') : t('inspector.showOriginal')}
                </button>
              ) : (
                <span className="text-ink-3">{info.redacted ? t('inspector.notKept') : info.known ? t('inspector.unknown') : t('inspector.forgotten')}</span>
              )}
              {revealError !== null && <span className="mt-1 block text-crit-ink">{errorText(t, revealError)}</span>}
            </dd>
          </>
        )}
        {info.module && (
          <>
            <dt className="text-ink-3">{t('inspector.module')}</dt>
            <dd className="min-w-0 text-ink">{t(MODULE_LABEL[info.module])}</dd>
          </>
        )}
        {(info.rule || info.module) && (
          <>
            <dt className="text-ink-3">{t('inspector.rule')}</dt>
            <dd className="min-w-0">
              <RuleName rule={info.rule} />
            </dd>
          </>
        )}
        {info.via && (
          <>
            <dt className="text-ink-3">{t('inspector.via')}</dt>
            <dd className="min-w-0 text-ink-2">{t(VIA_LABEL[info.via])}</dd>
          </>
        )}
        {!info.stopped && (
          <>
            <dt className="text-ink-3">{t('inspector.entity')}</dt>
            <dd className="mono min-w-0 break-all text-ink-2">
              {info.entity ? (
                <Link to={`/vault?q=${encodeURIComponent(info.sent)}`} className="link">
                  {info.entity}
                </Link>
              ) : (
                <span className="font-sans text-ink-3">{t('inspector.noEntity')}</span>
              )}
            </dd>
          </>
        )}
      </dl>

      {actions && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-line px-3 py-2">
          {hasProject && (
            <div className="mb-0.5 w-full">
              <WhereChoice where={where} onChange={setWhere} />
            </div>
          )}
          <button
            type="button"
            className="btn btn-sm"
            disabled={!canResolve || allowState !== 'idle'}
            onClick={() => void allow()}
            title={canResolve ? undefined : t('inspector.noOriginal')}
          >
            <Icon name={allowState === 'done' ? 'check' : 'eye'} size={13} />
            {allowState === 'done' ? t('inspector.allowDone') : t('inspector.allow')}
          </button>
          {/* A request that is only checked is not masked by Latet (the plugin does): nothing to always mask. */}
          {!info.stopped && (
            <button
              type="button"
              className="btn btn-sm"
              disabled={!canResolve || termState !== 'idle'}
              onClick={() => void alwaysMask()}
              title={canResolve ? undefined : t('inspector.noOriginal')}
            >
              <Icon name={termState === 'done' ? 'check' : 'eyeOff'} size={13} />
              {termState === 'done' ? t('inspector.termDone') : t('inspector.alwaysMask')}
            </button>
          )}
          {info.stopped && info.texts && info.texts.length > 1 && (
            <p className="w-full text-micro text-ink-3">{t('inspector.stoppedMany', { number: info.texts.length })}</p>
          )}
          {info.stopped && <p className="w-full text-micro text-ink-3">{t('inspector.stoppedHint')}</p>}
          {(allowState === 'pending' || termState === 'pending') && (
            <p className="w-full text-meta text-warn-ink">{t('errors.settingsSavedChangePending')}</p>
          )}
        </div>
      )}
      {actions && !info.stopped && field !== null && <KeepField field={field} projectId={projectId} />}
      {!pinned && <p className="border-t border-line px-3 py-1.5 text-micro text-ink-3">{t('inspector.pinHint')}</p>}
    </div>
  )
}

const TECHNICAL_HINT: Record<TechnicalKind, string> = {
  id: tk('inspector.technical.id'),
  date: tk('inspector.technical.date'),
  state: tk('inspector.technical.state'),
  count: tk('inspector.technical.count'),
}

/** A value masked by strict data mode: keep its field in clear, in the exchange's project or everywhere. */
function KeepField({ field, projectId }: { field: string; projectId: string | null }) {
  const { t } = useTranslation()
  const toast = useToast()
  const { reloadConfig } = useMe()
  const [state, setState] = useState<ActionState>('idle')
  const kind = technicalKind(field)
  const keep = async () => {
    setState('busy')
    try {
      if (projectId) {
        await api.changePersonal(projectId, (personal) => ({ ...personal, fields: unlocked(personal.fields ?? [], field) }))
      } else {
        await api.changeConfig((config) => ({ fields: unlocked(config.fields ?? [], field) }))
      }
      setState('done')
      toast.show(projectId ? t('inspector.keptInProject', { field }) : t('inspector.kept', { field }))
    } catch (error) {
      const pending = savedChangePending(error)
      setState(pending ? 'pending' : 'idle')
      if (pending) reloadConfig()
      toast.show(errorText(t, error), 'fail')
    }
  }
  return (
    <div className="border-t border-line px-3 py-2">
      {kind && <p className="mb-1.5 text-micro text-ink-3">{t(TECHNICAL_HINT[kind])}</p>}
      <button type="button" className="btn btn-sm" disabled={state !== 'idle'} onClick={() => void keep()}>
        <Icon name={state === 'done' ? 'check' : 'eye'} size={13} />
        {state === 'done' ? t('inspector.keepDone') : projectId ? t('inspector.keepInProject', { field }) : t('inspector.keep', { field })}
      </button>
      {state === 'pending' && <p className="mt-1 text-meta text-warn-ink">{t('errors.settingsSavedChangePending')}</p>}
    </div>
  )
}
