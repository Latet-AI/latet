// Small hand-made SVG and HTML charts: thin marks, hairline grids, values reachable without hovering
// (direct labels or a table view), the same details on keyboard focus as on hover.

import { useId, useLayoutEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { niceTicks } from '../lib/stats'
import { Segmented } from './ui'

export interface ColumnDatum {
  key: string
  /** Axis label (short). */
  label: string
  /** Full label for tooltips and the table ("Mon, Oct 6"). */
  longLabel: string
  value: number
}

function useWidth<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0]?.contentRect.width ?? 0)
      setWidth((prev) => (prev === next ? prev : next))
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [el])
  return [setEl, width]
}

/** A column path with a rounded data end (top) and a square base. */
function columnPath(x: number, y: number, width: number, height: number, radius = 4): string {
  const r = Math.min(radius, width / 2, height)
  const bottom = y + height
  return [
    `M${x},${bottom}`,
    `V${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `H${x + width - r}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `V${bottom}`,
    'Z',
  ].join(' ')
}

interface ChartCardProps {
  title: string
  subtitle?: ReactNode
  table: ReactNode
  children: ReactNode
}

/** A chart with its title and a switch to its table view (the accessible twin of the chart). */
export function ChartCard({ title, subtitle, table, children }: ChartCardProps) {
  const { t } = useTranslation()
  const [view, setView] = useState<'chart' | 'table'>('chart')
  const id = useId()
  return (
    <figure className="panel flex min-w-0 flex-col p-4" aria-labelledby={id}>
      <div className="mb-3 flex items-start justify-between gap-3">
        <figcaption className="min-w-0">
          <span id={id} className="block text-ui font-semibold text-ink">
            {title}
          </span>
          {subtitle && <span className="mt-0.5 block text-meta text-ink-3">{subtitle}</span>}
        </figcaption>
        <Segmented<'chart' | 'table'>
          label={t('charts.view')}
          value={view}
          onChange={setView}
          size="sm"
          options={[
            { value: 'chart', label: t('charts.chart') },
            { value: 'table', label: t('charts.table') },
          ]}
        />
      </div>
      {view === 'chart' ? children : table}
    </figure>
  )
}

interface ColumnChartProps {
  data: readonly ColumnDatum[]
  /** CSS color of the marks. */
  color: string
  format: (value: number) => string
  /** Name of the measure, for tooltips and the accessible summary ("exchanges"). */
  measure: string
  height?: number
  /** Show every nth axis label (1 = all). */
  labelEvery?: number
}

export function ColumnChart({ data, color, format, measure, height = 150, labelEvery = 1 }: ColumnChartProps) {
  const { t } = useTranslation()
  const [ref, width] = useWidth<HTMLDivElement>()
  const [active, setActive] = useState<number | null>(null)
  const max = data.reduce((m, d) => Math.max(m, d.value), 0)
  const ticks = niceTicks(max, 3)
  const top = ticks[ticks.length - 1] ?? 1
  const margin = { top: 16, right: 4, bottom: 22, left: 34 }
  const plotW = Math.max(0, width - margin.left - margin.right)
  const plotH = height - margin.top - margin.bottom
  const band = data.length ? plotW / data.length : 0
  const barW = Math.max(2, Math.min(24, band - 4))
  const y = (v: number) => margin.top + plotH - (v / top) * plotH
  const peak = data.reduce((best, d, i) => (d.value > (data[best]?.value ?? -1) ? i : best), 0)
  const summary = t('charts.columnSummary', {
    measure,
    count: data.length,
    max: format(max),
    peak: data[peak]?.longLabel ?? '',
  })
  const activeDatum = active === null ? undefined : data[active]

  return (
    <div ref={ref} className="relative" style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label={summary} className="block overflow-visible">
          {ticks.map((tick) => (
            <g key={tick}>
              <line x1={margin.left} x2={width - margin.right} y1={y(tick)} y2={y(tick)} stroke={tick === 0 ? 'var(--line-2)' : 'var(--line)'} strokeWidth={1} shapeRendering="crispEdges" />
              <text x={margin.left - 6} y={y(tick)} dy="0.32em" textAnchor="end" fontSize={11} fill="var(--ink-3)" className="tabular-nums">
                {format(tick)}
              </text>
            </g>
          ))}
          {data.map((d, i) => {
            const x = margin.left + i * band + (band - barW) / 2
            const h = (d.value / top) * plotH
            const isActive = active === i
            return (
              <g
                key={d.key}
                tabIndex={0}
                role="img"
                aria-label={t('charts.barLabel', { label: d.longLabel, value: format(d.value), measure })}
                onPointerEnter={() => setActive(i)}
                onPointerLeave={() => setActive((a) => (a === i ? null : a))}
                onFocus={() => setActive(i)}
                onBlur={() => setActive((a) => (a === i ? null : a))}
                className="outline-none [&:focus-visible>rect:first-child]:stroke-[var(--focus)]"
              >
                {/* The hit area covers the whole band, not only the painted mark. */}
                <rect x={margin.left + i * band} y={margin.top} width={band} height={plotH} fill="transparent" stroke="transparent" strokeWidth={2} />
                {d.value > 0 && (
                  <path d={columnPath(x, y(d.value), barW, h)} fill={color} opacity={active === null || isActive ? 1 : 0.55} />
                )}
                {i === peak && d.value > 0 && (
                  <text x={x + barW / 2} y={y(d.value) - 5} textAnchor="middle" fontSize={11} fill="var(--ink-2)" className="tabular-nums">
                    {format(d.value)}
                  </text>
                )}
                {(i % labelEvery === 0 || i === data.length - 1) && (
                  <text x={margin.left + i * band + band / 2} y={height - 6} textAnchor="middle" fontSize={11} fill="var(--ink-3)">
                    {d.label}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      )}
      {activeDatum && active !== null && (
        <div
          className="floating pointer-events-none absolute z-10 px-2.5 py-1.5 whitespace-nowrap"
          style={{
            left: Math.min(Math.max(0, margin.left + active * band + band / 2 - 70), Math.max(0, width - 140)),
            top: Math.max(0, y(activeDatum.value) - 52),
          }}
          aria-hidden="true"
        >
          <p className="text-ui font-semibold text-ink tabular-nums">{format(activeDatum.value)}</p>
          <p className="text-micro text-ink-3">
            {measure}
            {', '}
            {activeDatum.longLabel}
          </p>
        </div>
      )}
    </div>
  )
}

export interface BarRow {
  key: string
  label: ReactNode
  value: number
  /** data-tone for the class colors; omitted: the single-series data ink. */
  tone?: string
  title?: string
}

/** Horizontal bars with the value at the tip of each: all values are labelled. */
export function BarList({
  rows,
  format,
  labelWidth = 150,
  valueWidth = 56,
}: {
  rows: readonly BarRow[]
  format: (value: number) => string
  labelWidth?: number
  /** Room kept at the end of the longest bar for its value label. */
  valueWidth?: number
}) {
  const max = rows.reduce((m, r) => Math.max(m, r.value), 0)
  return (
    <ul className="space-y-1.5">
      {rows.map((row) => (
        <li key={row.key} className="grid items-center gap-3 text-meta" style={{ gridTemplateColumns: `${labelWidth}px minmax(0,1fr)` }} title={row.title}>
          <span className="min-w-0 truncate text-ink-2">{row.label}</span>
          <span className="flex min-w-0 items-center gap-2">
            <span
              className="h-[10px] rounded-r-[4px]"
              data-tone={row.tone}
              style={{
                width: max > 0 ? `calc((100% - ${valueWidth}px) * ${row.value / max})` : 0,
                minWidth: row.value > 0 ? 2 : 0,
                background: row.tone ? 'var(--tone)' : 'var(--data)',
              }}
              aria-hidden="true"
            />
            <span className="whitespace-nowrap text-ink tabular-nums">{format(row.value)}</span>
          </span>
        </li>
      ))}
    </ul>
  )
}

/** A ratio against its whole: the track is a lighter step of the fill. */
export function Meter({ ratio, label }: { ratio: number; label: string }) {
  const clamped = Math.max(0, Math.min(1, ratio))
  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped * 100)}
      className="h-2 w-full overflow-hidden rounded-[3px]"
      style={{ background: 'color-mix(in oklab, var(--data) 16%, transparent)' }}
    >
      <div className="h-full rounded-r-[3px]" style={{ width: `${clamped * 100}%`, background: 'var(--data)' }} />
    </div>
  )
}

export function StatTile({ label, value, children }: { label: string; value: string; children?: ReactNode }) {
  return (
    <div className="panel min-w-0 px-4 py-3">
      <p className="text-meta text-ink-2">{label}</p>
      <p className="mt-1 font-display text-figure font-bold tracking-[-0.02em] text-ink">{value}</p>
      {children && <div className="mt-1.5 text-meta text-ink-3">{children}</div>}
    </div>
  )
}

/** The table twin of a chart. */
export function DataTable({ head, rows }: { head: [string, string]; rows: readonly [ReactNode, string][] }) {
  return (
    <div className="max-h-[260px] overflow-auto">
      <table className="data-table text-meta">
        <thead>
          <tr>
            <th scope="col">{head[0]}</th>
            <th scope="col" className="num">
              {head[1]}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, value], i) => (
            <tr key={i}>
              <td className="text-ink-2">{label}</td>
              <td className="num text-ink">{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
