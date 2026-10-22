import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'

export type Placement = 'bottom' | 'top'

/** The first line box of an inline element (a value that wraps over lines), or its whole box. */
function anchorRect(anchor: Element): DOMRect {
  const rects = anchor.getClientRects()
  return rects.length > 0 ? (rects[0] as DOMRect) : anchor.getBoundingClientRect()
}

/**
 * Keep the element the returned ref is attached to (position: fixed) next to `anchor` while open:
 * below it, or above when there is no room, clamped to the viewport. Positioning is imperative: no
 * re-render on scroll.
 */
export function useFloatingPosition<T extends HTMLElement>(
  anchor: Element | null,
  open: boolean,
  placement: Placement = 'bottom',
  gap = 6,
): RefObject<T | null> {
  const ref = useRef<T | null>(null)
  useLayoutEffect(() => {
    const floating = ref.current
    if (!open || !anchor || !floating) return
    const update = () => {
      const a = anchorRect(anchor)
      const f = floating.getBoundingClientRect()
      const vw = document.documentElement.clientWidth
      const vh = document.documentElement.clientHeight
      const below = a.bottom + gap
      const above = a.top - gap - f.height
      let top = placement === 'top' ? above : below
      if (placement === 'bottom' && below + f.height > vh - 8 && above >= 8) top = above
      if (placement === 'top' && above < 8) top = below
      const left = Math.min(Math.max(8, a.left), Math.max(8, vw - f.width - 8))
      floating.style.top = `${Math.round(Math.max(8, top))}px`
      floating.style.left = `${Math.round(left)}px`
      floating.style.visibility = 'visible'
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(floating)
    window.addEventListener('scroll', update, true)
    window.addEventListener('resize', update)
    return () => {
      observer.disconnect()
      window.removeEventListener('scroll', update, true)
      window.removeEventListener('resize', update)
    }
  }, [anchor, open, placement, gap])
  return ref
}

interface TooltipProps {
  content: ReactNode
  children: ReactNode
  placement?: Placement
  /** Make the wrapper focusable so keyboard users get the tooltip too (off in dense tables). */
  focusable?: boolean
  className?: string
}

/** A text tooltip on hover and keyboard focus. */
export function Tooltip({ content, children, placement = 'top', focusable = true, className }: TooltipProps) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState<HTMLSpanElement | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const floating = useFloatingPosition<HTMLDivElement>(anchor, open, placement)

  useEffect(() => () => clearTimeout(timer.current), [])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const show = () => {
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setOpen(true), 250)
  }
  const hide = () => {
    clearTimeout(timer.current)
    setOpen(false)
  }

  return (
    <span
      ref={setAnchor}
      className={className ?? 'inline-flex'}
      tabIndex={focusable ? 0 : undefined}
      aria-describedby={open ? id : undefined}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={() => {
        clearTimeout(timer.current)
        setOpen(true)
      }}
      onBlur={hide}
    >
      {children}
      {open &&
        createPortal(
          <div
            ref={floating}
            id={id}
            role="tooltip"
            className="floating pointer-events-none fixed z-50 max-w-[320px] px-2.5 py-1.5 text-meta text-ink-2"
            style={{ top: 0, left: 0, visibility: 'hidden' }}
          >
            {content}
          </div>,
          document.body,
        )}
    </span>
  )
}
