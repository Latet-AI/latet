import { useState, type KeyboardEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { CLASS_TONE, isJudgement } from '../../lib/labels'
import { lineCount, offsetAfterLines, splitByRanges } from '../../lib/spans'
import { useInspector } from './Inspector'
import type { TextMark } from './types'

/**
 * The two sides of a text, as on latet.ai: the value as it is on this computer (real, warm) and the fake that
 * left in its place (green). The class still shows in the bar under the value.
 */
export type MarkTone = 'real' | 'fake'

// The wash and text of each tone, and a stronger wash while the value or its twin is pointed at (data-active).
const TONE_CLASSES: Record<MarkTone, string> = {
  real: 'bg-real-low text-real-ink hover:bg-real/25 focus-visible:bg-real/25 data-active:bg-real/25',
  fake: 'bg-fake-low text-fake-ink hover:bg-fake/25 focus-visible:bg-fake/25 data-active:bg-fake/25',
}

/**
 * CSS classes and tone of a highlighted value, from what it is and which side shows it. With `side`, a value takes
 * the tone of its pane, unless it is neither a real value nor a fake: a one-way redaction's marker, a value the
 * vault lost (the original pane can only show what was sent) or a value that stopped the request (in clear).
 */
export function markStyle(mark: TextMark, side?: MarkTone): { className: string; tone: string } {
  const { info } = mark
  const classes = ['mask']
  const sentSide = info.side === 'sent' || info.side === 'model'
  const redactedMarker = info.redacted && sentSide
  // On the original side, a value the vault doesn't hold (forgotten, or redacted one way) can only
  // show what was sent: hatched, like the panes Latet cannot fill.
  const gone = !info.known && !sentSide
  if (redactedMarker) classes.push('mask-redacted')
  if (mark.soft) classes.push('mask-soft')
  if (gone) classes.push('mask-gone')
  // A value that stopped the request was masked by nothing: a box in the color of a stop, not a bar.
  if (info.stopped) return { className: [...classes, 'mask-stopped'].join(' '), tone: 'crit' }
  if (side && !redactedMarker && !gone) classes.push(TONE_CLASSES[side])
  return { className: classes.join(' '), tone: CLASS_TONE[info.cls] ?? 'neutral' }
}

interface MaskedTextProps {
  text: string
  marks: readonly TextMark[]
  /** Collapse texts longer than this many lines (0: never). */
  collapseAfter?: number
  /** Lines shown while collapsed. */
  previewLines?: number
  /** Number the values (footnotes link a value to its twin in the other pane). */
  footnotes?: boolean
  /** Accessible name of the text region. */
  label?: string
  className?: string
  empty?: ReactNode
  /** The side the text is: its values take the real tone (the original) or the fake tone (what was sent). */
  tone?: MarkTone
}

/**
 * A text exactly as given, with its masked values highlighted in their class color, or in the tone of the side
 * shown. Values are focusable: the page's inspector explains them on hover or focus, and Enter keeps it open.
 */
export function MaskedText({
  text,
  marks,
  collapseAfter = 40,
  previewLines = 24,
  footnotes = false,
  label,
  className = '',
  empty,
  tone,
}: MaskedTextProps) {
  const { t } = useTranslation()
  const inspector = useInspector()
  const lines = lineCount(text)
  const collapsible = collapseAfter > 0 && lines > collapseAfter
  // The value that stopped the request is what the page is about: never folded away below the fold.
  const [expanded, setExpanded] = useState(() => marks.some((m) => m.info.stopped))
  const cut = collapsible && !expanded ? offsetAfterLines(text, previewLines) : text.length
  const shown = cut < text.length ? text.slice(0, cut) : text
  const visibleMarks = cut < text.length ? marks.filter((m) => m.start < cut).map((m) => ({ ...m, end: Math.min(m.end, cut) })) : marks
  const hiddenMarks = marks.length - visibleMarks.length
  const segments = splitByRanges(shown, visibleMarks, 'utf16')

  if (text === '') {
    return <div className={`code px-3 py-2.5 text-ink-3 ${className}`}>{empty ?? t('masked.empty')}</div>
  }

  const onKey = (event: KeyboardEvent<HTMLElement>, mark: TextMark) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      inspector?.pin(event.currentTarget, mark.info)
    }
  }

  return (
    <div className={className}>
      <div className="code px-3 py-2.5 text-ink" role={label ? 'region' : undefined} aria-label={label}>
        {segments.map((segment, i) => {
          const mark = segment.range
          if (!mark) return <span key={i}>{segment.text}</span>
          const info = mark.info
          const style = markStyle(mark, tone)
          return (
            <span key={i}>
              <mark
                tabIndex={0}
                className={style.className}
                data-tone={style.tone}
                data-category={info.cls}
                data-dashed={isJudgement(info.cls) ? '' : undefined}
                data-pair={info.pair}
                aria-label={
                  info.stopped
                    ? t('masked.stoppedLabel', { type: info.type, value: segment.text })
                    : t('masked.valueLabel', { type: info.type, value: segment.text })
                }
                onMouseEnter={(event) => inspector?.hover(event.currentTarget, info)}
                onMouseLeave={() => inspector?.leave()}
                onFocus={(event) => inspector?.focus(event.currentTarget, info)}
                onBlur={() => inspector?.blur()}
                onClick={(event) => inspector?.pin(event.currentTarget, info)}
                onKeyDown={(event) => onKey(event, mark)}
              >
                {segment.text}
              </mark>
              {footnotes && info.footnote !== null && (
                <sup className="fn" aria-hidden="true">
                  {info.footnote}
                </sup>
              )}
            </span>
          )
        })}
      </div>
      {collapsible && (
        <div className="flex flex-wrap items-center gap-3 border-t border-line px-3 py-1.5">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
            {expanded ? t('masked.collapse') : t('masked.expand', { count: lines })}
          </button>
          {!expanded && hiddenMarks > 0 && <span className="text-meta text-ink-3">{t('masked.hiddenValues', { count: hiddenMarks })}</span>}
        </div>
      )}
    </div>
  )
}
