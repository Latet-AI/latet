// Aggregations behind the Stats page and the Live summary.

import type { DayStats, ExchangeSummary } from '../api/types'

export interface Totals {
  exchanges: number
  masked: number
  blocked: number
}

export function totals(byDay: readonly DayStats[]): Totals {
  return byDay.reduce<Totals>(
    (acc, day) => ({
      exchanges: acc.exchanges + day.exchanges,
      masked: acc.masked + day.masked,
      blocked: acc.blocked + day.blocked,
    }),
    { exchanges: 0, masked: 0, blocked: 0 },
  )
}

export const OTHER = '__other__'

export interface Ranked {
  key: string
  value: number
}

/**
 * Entries sorted by value (largest first, ties by key). With `limit`, the tail beyond `limit - 1`
 * entries folds into one `OTHER` entry so the list never exceeds `limit` rows.
 */
export function rankEntries(record: Readonly<Record<string, number>>, limit?: number): Ranked[] {
  const entries = Object.entries(record)
    .filter(([, value]) => Number.isFinite(value) && value > 0)
    .map(([key, value]) => ({ key, value }))
    .sort((a, b) => b.value - a.value || a.key.localeCompare(b.key))
  if (limit === undefined || entries.length <= limit) return entries
  const head = entries.slice(0, Math.max(0, limit - 1))
  const rest = entries.slice(head.length).reduce((sum, e) => sum + e.value, 0)
  return [...head, { key: OTHER, value: rest }]
}

const roundTick = (value: number) => Math.round(value * 1e6) / 1e6

/**
 * Round axis ticks from 0 covering `max` with at most `count` intervals. Steps are 1, 2, 2.5 or 5
 * times a power of ten; with `integer` (counts), steps are whole numbers.
 */
export function niceTicks(max: number, count = 4, integer = true): number[] {
  if (!Number.isFinite(max) || max <= 0) return integer ? [0, 1] : [0, 1]
  const raw = max / count
  const power = 10 ** Math.floor(Math.log10(raw))
  const steps = [1, 2, 2.5, 5, 10].map((s) => roundTick(s * power)).filter((s) => !integer || Number.isInteger(s))
  let step = steps.find((s) => Math.ceil(roundTick(max / s)) <= count) ?? steps[steps.length - 1] ?? 1
  if (integer && step < 1) step = 1
  const intervals = Math.max(1, Math.ceil(roundTick(max / step)))
  return Array.from({ length: intervals + 1 }, (_, i) => roundTick(i * step))
}

/** The top of the axis `niceTicks` would draw. */
export function niceMax(max: number, count = 4, integer = true): number {
  const ticks = niceTicks(max, count, integer)
  return ticks[ticks.length - 1] ?? 1
}

/** Days from `end` going back `days` days (inclusive), as YYYY-MM-DD in local time. */
export function dayRange(end: Date, days: number): string[] {
  const out: string[] = []
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(end.getFullYear(), end.getMonth(), end.getDate() - i)
    out.push(isoDay(d))
  }
  return out
}

export function isoDay(d: Date): string {
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${month}-${day}`
}

/** One row per day of the period, zeros where the daemon reported nothing, oldest first. */
export function fillDays(byDay: readonly DayStats[], days: number, end: Date): DayStats[] {
  const known = new Map(byDay.map((d) => [d.day, d]))
  const range = dayRange(end, days)
  // Keep days the daemon reported outside the local range (time zones) rather than dropping them.
  const extra = byDay.filter((d) => !range.includes(d.day))
  const rows = range.map((day) => known.get(day) ?? { day, exchanges: 0, masked: 0, blocked: 0 })
  return [...extra, ...rows].sort((a, b) => a.day.localeCompare(b.day))
}

/** Sum of masked values per type over a list of exchanges. */
export function aggregateByType(exchanges: readonly ExchangeSummary[]): Record<string, number> {
  const out: Record<string, number> = {}
  for (const exchange of exchanges) {
    for (const [type, count] of Object.entries(exchange.by_type)) out[type] = (out[type] ?? 0) + count
  }
  return out
}

/** value / total, or null when the total is zero. */
export function share(value: number, total: number): number | null {
  return total > 0 ? value / total : null
}

/** Cache read ratio from token counts, as the daemon defines it. */
export function cacheReadRatio(tokens: { input: number; cache_read: number; cache_write: number }): number | null {
  return share(tokens.cache_read, tokens.input + tokens.cache_read + tokens.cache_write)
}

/** The largest value of a series, 0 when empty. */
export function seriesMax(values: readonly number[]): number {
  return values.reduce((max, v) => (v > max ? v : max), 0)
}
