// A waiting text's original, where you mask more: select a value with the mouse and pick what it is.
// It shows what Latet masked, what the local reviewer suggested, and the values you masked.

import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type KeyboardEvent, type Ref } from 'react'
import { useTranslation } from 'react-i18next'
import type { ReviewCategory } from '../../api/types'
import type { SelectionCheck } from '../../lib/approvals'
import { CATEGORY_TYPE, REVIEW_CATEGORIES } from '../../lib/reviewer'
import { isJudgement } from '../../lib/labels'
import { splitByRanges } from '../../lib/spans'
import { tk } from '../../i18n'
import { Icon } from '../Icon'
import { CATEGORY_LABEL } from '../ReviewerEditor'
import { useInspector } from './Inspector'
import { markStyle, type MarkTone } from './MaskedText'
import type { TextMark } from './types'

/** A range of the text (UTF-16 offsets) with what it is. */
export interface ShownRange {
  start: number
  end: number
  /** Index of the suggestion, or of the value you masked. */
  key: number
  category: ReviewCategory
}

type Piece =
  | { start: number; end: number; kind: 'masked'; mark: TextMark }
  | { start: number; end: number; kind: 'suggested' | 'added'; range: ShownRange }

/** What the page can ask of the text: the menu of what the selection is, as releasing the mouse opens it. */
export interface SelectableHandle {
  /** Opens the menu for the text selected in this one; false when no selection lies in it. */
  pickSelection: () => boolean
}

/** Why a selection cannot be masked, as told to you. */
export const REFUSED: Record<Exclude<SelectionCheck, { ok: true }>['reason'], string> = {
  empty: tk('approvals.select.empty'),
  short: tk('approvals.select.short'),
  noword: tk('approvals.select.noword'),
  masked: tk('approvals.select.masked'),
  added: tk('approvals.select.added'),
}

interface Picker {
  check: SelectionCheck
  top: number
  left: number
}

/** The UTF-16 offset of a point of the DOM inside `box`, counted on its text. */
function offsetIn(box: HTMLElement, node: Node, offset: number): number {
  const range = document.createRange()
  range.selectNodeContents(box)
  range.setEnd(node, offset)
  return range.toString().length
}

export function SelectableText({
  text,
  marks,
  suggestions,
  added,
  check,
  onMask,
  onSuggestion,
  label,
  tone,
  ref,
}: {
  text: string
  /** What Latet masked (UTF-16 offsets). */
  marks: readonly TextMark[]
  suggestions: readonly ShownRange[]
  added: readonly ShownRange[]
  /** A selection (UTF-16 offsets) as a value to mask, or why it cannot be one. */
  check: (start: number, end: number) => SelectionCheck
  onMask: (value: Extract<SelectionCheck, { ok: true }>, category: ReviewCategory) => void
  /** Mask a suggestion as the local reviewer categorised it. */
  onSuggestion: (key: number) => void
  label: string
  /** The tone of the values Latet masked: the real tone, in the original of a waiting text. */
  tone?: MarkTone
  ref?: Ref<SelectableHandle>
}) {
  const { t } = useTranslation()
  const inspector = useInspector()
  const wrap = useRef<HTMLDivElement>(null)
  const box = useRef<HTMLDivElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)
  const [picker, setPicker] = useState<Picker | null>(null)

  // The picker opens under the selection, inside a panel that clips what overflows it: under a short text the
  // panel grows to hold it while it is open (and only then: nothing moves while it is closed).
  useLayoutEffect(() => {
    const host = wrap.current
    const floating = pickerRef.current
    if (!host || !picker || !floating) return
    host.style.minHeight = `${picker.top + floating.offsetHeight + 8}px`
    return () => {
      host.style.minHeight = ''
    }
  }, [picker])

  // A click elsewhere or Esc closes the picker.
  useEffect(() => {
    if (!picker) return
    const onDown = (event: MouseEvent) => {
      if (!pickerRef.current?.contains(event.target as Node)) setPicker(null)
    }
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setPicker(null)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [picker])

  /** The menu of what the selection is, under it, when the text selected lies in this one. */
  const pickSelection = (): boolean => {
    const selection = window.getSelection()
    const container = box.current
    const host = wrap.current
    if (!selection || selection.isCollapsed || selection.rangeCount === 0 || !container || !host) return false
    const range = selection.getRangeAt(0)
    if (!container.contains(range.startContainer) || !container.contains(range.endContainer)) return false
    const start = offsetIn(container, range.startContainer, range.startOffset)
    const end = offsetIn(container, range.endContainer, range.endOffset)
    const rect = range.getBoundingClientRect()
    const area = host.getBoundingClientRect()
    const left = Math.max(0, Math.min(rect.left - area.left, area.width - 340))
    setPicker({ check: check(start, end), top: rect.bottom - area.top + 6, left })
    return true
  }
  // The M shortcut of Approvals asks for the same menu.
  useImperativeHandle(ref, () => ({ pickSelection }))

  const choose = (category: ReviewCategory) => {
    if (!picker?.check.ok) return
    onMask(picker.check, category)
    window.getSelection()?.removeAllRanges()
    setPicker(null)
  }

  const pieces: Piece[] = [
    ...marks.map((mark) => ({ start: mark.start, end: mark.end, kind: 'masked' as const, mark })),
    ...added.map((range) => ({ start: range.start, end: range.end, kind: 'added' as const, range })),
    ...suggestions.map((range) => ({ start: range.start, end: range.end, kind: 'suggested' as const, range })),
  ]
  const segments = splitByRanges(text, pieces, 'utf16')

  const onSuggestionKey = (event: KeyboardEvent<HTMLElement>, key: number) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      event.stopPropagation()
      onSuggestion(key)
    }
  }

  if (text === '') return <div className="code px-3 py-2.5 text-ink-3">{t('masked.empty')}</div>

  return (
    <div ref={wrap} className="relative">
      <div
        ref={box}
        className="code max-h-[480px] overflow-auto px-3 py-2.5 text-ink"
        role="region"
        aria-label={label}
        onMouseUp={() => pickSelection()}
        onScroll={() => setPicker(null)}
      >
        {segments.map((segment, i) => {
          const piece = segment.range
          if (!piece) return <span key={i}>{segment.text}</span>
          if (piece.kind === 'masked') {
            const info = piece.mark.info
            const style = markStyle(piece.mark, tone)
            return (
              <mark
                key={i}
                tabIndex={0}
                className={style.className}
                data-tone={style.tone}
                data-category={info.cls}
                data-dashed={isJudgement(info.cls) ? '' : undefined}
                data-pair={info.pair}
                aria-label={t('masked.valueLabel', { type: info.type, value: segment.text })}
                onMouseEnter={(event) => inspector?.hover(event.currentTarget, info)}
                onMouseLeave={() => inspector?.leave()}
                onFocus={(event) => inspector?.focus(event.currentTarget, info)}
                onBlur={() => inspector?.blur()}
              >
                {segment.text}
              </mark>
            )
          }
          const category = t(CATEGORY_LABEL[piece.range.category])
          if (piece.kind === 'added') {
            return (
              <mark key={i} className="mask-pending" title={t('approvals.added.title', { category })}>
                {segment.text}
              </mark>
            )
          }
          return (
            <mark
              key={i}
              role="button"
              tabIndex={0}
              className="mask-suggested"
              title={t('approvals.suggestion.title', { category })}
              aria-label={t('approvals.suggestion.mask', { value: segment.text, category })}
              onClick={() => onSuggestion(piece.range.key)}
              onKeyDown={(event) => onSuggestionKey(event, piece.range.key)}
            >
              {segment.text}
            </mark>
          )
        })}
      </div>
      {picker && (
        <div
          ref={pickerRef}
          role="dialog"
          aria-label={t('approvals.select.title')}
          className="floating absolute z-30 w-[340px] max-w-full p-3"
          style={{ top: picker.top, left: picker.left }}
        >
          {picker.check.ok ? (
            <>
              <p className="mono break-all text-meta text-ink">{picker.check.value}</p>
              <p className="mt-1.5 text-meta text-ink-2">{t('approvals.select.as')}</p>
              <ul className="mt-2 flex flex-wrap gap-1.5">
                {REVIEW_CATEGORIES.map((category, i) => (
                  <li key={category}>
                    <button
                      type="button"
                      className="btn btn-sm"
                      title={CATEGORY_TYPE[category]}
                      autoFocus={i === 0}
                      onClick={() => choose(category)}
                    >
                      {t(CATEGORY_LABEL[category])}
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p role="alert" className="flex items-start gap-1.5 text-meta text-ink-2">
              <Icon name="info" size={14} className="mt-px flex-none" />
              {t(REFUSED[picker.check.reason])}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
