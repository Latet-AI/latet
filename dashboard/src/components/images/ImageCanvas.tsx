// An image waiting in the Live Reviewer: where Latet covers each value with its fake, the values you
// masked in its text, and the rectangles you draw on it by hand, covered with a bar when you approve.

import { useState, type PointerEvent } from 'react'
import { useTranslation } from 'react-i18next'
import type { LiveImage } from '../../api/types'
import { MAX_RECTS, rectBetween, textRects, toImage, usable, type ImageChoice, type Rect } from '../../lib/images'
import { Icon } from '../Icon'

type View = 'original' | 'sent'

const COVERED = { fill: 'color-mix(in oklab, var(--good) 16%, transparent)', stroke: 'var(--good-ink)' }
const PENDING = { fill: 'color-mix(in oklab, var(--brand) 24%, transparent)', stroke: 'var(--brand-ink)' }
const BAR = '#16181c'
const DRAFT = { fill: 'color-mix(in oklab, #16181c 35%, transparent)', stroke: BAR }
const VIEWS: View[] = ['original', 'sent']

export function ImageCanvas({
  image,
  src,
  choice,
  onChange,
  pending,
}: {
  image: LiveImage
  /** Where the image is shown from: as it came, or as it would leave now. */
  src: (view: View) => string
  choice: ImageChoice
  onChange: (choice: ImageChoice) => void
  /** The values you masked in its text (code points of the text read). */
  pending: readonly { start: number; end: number }[]
}) {
  const { t } = useTranslation()
  const [view, setView] = useState<View>('original')
  const [draft, setDraft] = useState<{ from: [number, number]; to: [number, number] } | null>(null)
  const [failed, setFailed] = useState<View | null>(null)
  const { width, height } = image
  const full = choice.rects.length >= MAX_RECTS
  const drawing = !choice.withhold && !full

  const point = (e: PointerEvent<SVGSVGElement>) => toImage(e.clientX, e.clientY, e.currentTarget.getBoundingClientRect(), width, height)
  const down = (e: PointerEvent<SVGSVGElement>) => {
    if (!drawing || e.button !== 0) return
    e.currentTarget.setPointerCapture(e.pointerId)
    const at = point(e)
    setDraft({ from: at, to: at })
  }
  const move = (e: PointerEvent<SVGSVGElement>) => {
    if (draft) setDraft({ ...draft, to: point(e) })
  }
  const up = (e: PointerEvent<SVGSVGElement>) => {
    if (!draft) return
    const rect = rectBetween(draft.from, point(e), width, height)
    setDraft(null)
    if (usable(rect)) onChange({ ...choice, rects: [...choice.rects, rect] })
  }
  const remove = (n: number) => onChange({ ...choice, rects: choice.rects.filter((_, i) => i !== n) })

  const shown = draft ? rectBetween(draft.from, draft.to, width, height) : null
  const pendingRects = pending.flatMap((p) => textRects(image, p.start, p.end))
  const box = (rect: Rect, key: string | number, style: { fill: string; stroke: string }, dashed: boolean, title?: string) => (
    <rect
      key={key}
      x={rect[0]}
      y={rect[1]}
      width={rect[2] - rect[0]}
      height={rect[3] - rect[1]}
      fill={style.fill}
      stroke={style.stroke}
      strokeWidth={1.5}
      strokeDasharray={dashed ? '4 3' : undefined}
      vectorEffect="non-scaling-stroke"
    >
      {title && <title>{title}</title>}
    </rect>
  )

  return (
    <div className="border-b border-line">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line bg-surface px-3 py-1.5">
        <span className="flex items-center gap-1.5 text-meta text-ink-2">
          <Icon name="image" size={12} />
          {t('approvals.image.title', { width, height })}
        </span>
        <div className="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label={t('approvals.image.view')}>
          {VIEWS.map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={view === v}
              onClick={() => setView(v)}
              title={v === 'sent' ? t('approvals.image.sentHelp') : undefined}
              className={`px-2 py-0.5 text-meta ${view === v ? 'bg-wash-2 font-semibold text-ink' : 'text-ink-2 hover:bg-wash'}`}
            >
              {v === 'original' ? t('approvals.panes.original') : t('approvals.image.sent')}
            </button>
          ))}
        </div>
        <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-meta text-ink-2" title={t('approvals.image.withholdHelp')}>
          <input
            type="checkbox"
            className="accent-[var(--ink)]"
            checked={choice.withhold}
            onChange={(e) => onChange({ ...choice, withhold: e.target.checked })}
          />
          {t('approvals.image.withhold')}
        </label>
      </div>

      <div className="flex justify-center bg-well p-3">
        {failed === view ? (
          <p className="py-8 text-meta text-ink-3">{t('approvals.image.gone')}</p>
        ) : (
          <div className="relative inline-block max-w-full">
            <img
              src={src(view)}
              alt={t('approvals.image.alt')}
              onError={() => setFailed(view)}
              draggable={false}
              className={`block max-h-[520px] max-w-full select-none ${choice.withhold ? 'opacity-30' : ''}`}
            />
            {!choice.withhold && (
              <svg
                viewBox={`0 0 ${width} ${height}`}
                preserveAspectRatio="none"
                className={`absolute inset-0 h-full w-full touch-none ${drawing ? 'cursor-crosshair' : ''}`}
                onPointerDown={down}
                onPointerMove={move}
                onPointerUp={up}
                onPointerCancel={() => setDraft(null)}
                aria-hidden="true"
              >
                {view === 'original' &&
                  image.covered.map((c, i) => box(c.rect, `c${i}`, COVERED, false, t('approvals.image.coveredTitle', { type: c.type ?? '' })))}
                {pendingRects.map((rect, i) => box(rect, `p${i}`, PENDING, true))}
                {choice.rects.map((rect, i) => box(rect, `r${i}`, { fill: BAR, stroke: BAR }, false))}
                {shown && box(shown, -1, DRAFT, true)}
              </svg>
            )}
            {choice.withhold && (
              <p className="absolute inset-0 flex items-center justify-center p-4 text-center">
                <span className="flex items-center gap-1.5 rounded-md border border-line bg-surface px-3 py-1.5 text-ui font-semibold text-ink shadow-[var(--shadow)]">
                  <Icon name="ban" size={14} />
                  {t('approvals.image.withheld')}
                </span>
              </p>
            )}
          </div>
        )}
      </div>

      {!choice.withhold && (
        <div className="space-y-1.5 border-t border-line px-3 py-2">
          <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-meta text-ink-2" aria-label={t('legend.label')}>
            <li className="inline-flex items-center gap-1.5">
              <Swatch {...COVERED} />
              {t('approvals.image.legend.covered')}
            </li>
            {pendingRects.length > 0 && (
              <li className="inline-flex items-center gap-1.5">
                <Swatch {...PENDING} dashed />
                {t('approvals.image.legend.pending')}
              </li>
            )}
            <li className="inline-flex items-center gap-1.5">
              <Swatch fill={BAR} stroke={BAR} />
              {t('approvals.image.legend.drawn')}
            </li>
          </ul>
          <p className="text-meta text-ink-3">{full ? t('approvals.image.tooMany', { count: MAX_RECTS }) : t('approvals.image.drawHint')}</p>
          {choice.rects.length > 0 && (
            <ul className="flex flex-wrap items-center gap-2" aria-label={t('approvals.image.rects.label')}>
              {choice.rects.map((rect, i) => (
                <li key={i} className="inline-flex items-center gap-1 rounded-md border border-line bg-surface py-0.5 pr-0.5 pl-2 text-meta text-ink">
                  {t('approvals.image.rects.item', { number: i + 1, width: rect[2] - rect[0], height: rect[3] - rect[1] })}
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm btn-icon"
                    aria-label={t('approvals.image.rects.remove', { number: i + 1 })}
                    title={t('approvals.image.rects.remove', { number: i + 1 })}
                    onClick={() => remove(i)}
                  >
                    <Icon name="x" size={12} />
                  </button>
                </li>
              ))}
              {choice.rects.length > 1 && (
                <li>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => onChange({ ...choice, rects: [] })}>
                    {t('approvals.image.rects.clear')}
                  </button>
                </li>
              )}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

function Swatch({ fill, stroke, dashed = false }: { fill: string; stroke: string; dashed?: boolean }) {
  return (
    <svg width="18" height="12" aria-hidden="true" className="flex-none">
      <rect x="1" y="1" width="16" height="10" rx="1" fill={fill} stroke={stroke} strokeWidth="1.5" strokeDasharray={dashed ? '3 2' : undefined} />
    </svg>
  )
}
