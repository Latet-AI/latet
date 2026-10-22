// Number, date and duration formatting with Intl, in the active language.

const cache = new Map<string, Intl.NumberFormat | Intl.DateTimeFormat | Intl.RelativeTimeFormat>()

function numberFormat(locale: string, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `n|${locale}|${JSON.stringify(options)}`
  let format = cache.get(key) as Intl.NumberFormat | undefined
  if (!format) {
    format = new Intl.NumberFormat(locale, options)
    cache.set(key, format)
  }
  return format
}

function dateFormat(locale: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `d|${locale}|${JSON.stringify(options)}`
  let format = cache.get(key) as Intl.DateTimeFormat | undefined
  if (!format) {
    format = new Intl.DateTimeFormat(locale, options)
    cache.set(key, format)
  }
  return format
}

function relativeFormat(locale: string): Intl.RelativeTimeFormat {
  const key = `r|${locale}`
  let format = cache.get(key) as Intl.RelativeTimeFormat | undefined
  if (!format) {
    format = new Intl.RelativeTimeFormat(locale, { numeric: 'auto', style: 'short' })
    cache.set(key, format)
  }
  return format
}

export function formatNumber(locale: string, value: number, options: Intl.NumberFormatOptions = {}): string {
  return numberFormat(locale, options).format(value)
}

/** 18,204 → "18.2K" (compact above 10,000 so small counts stay exact). */
export function formatCompact(locale: string, value: number): string {
  if (Math.abs(value) < 10000) return numberFormat(locale, {}).format(value)
  return numberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

export function formatPercent(locale: string, ratio: number, digits = 0): string {
  return numberFormat(locale, { style: 'percent', maximumFractionDigits: digits }).format(ratio)
}

/** Milliseconds as "842 ms", "1.42 s" or "2 min 5 s". */
export function formatDuration(locale: string, ms: number): string {
  if (ms < 1) return numberFormat(locale, { style: 'unit', unit: 'millisecond', maximumFractionDigits: 1 }).format(ms)
  if (ms < 1000) {
    return numberFormat(locale, { style: 'unit', unit: 'millisecond', maximumFractionDigits: 0 }).format(ms)
  }
  if (ms < 60000) {
    const digits = ms < 10000 ? 2 : 1
    return numberFormat(locale, { style: 'unit', unit: 'second', maximumFractionDigits: digits }).format(ms / 1000)
  }
  const minutes = Math.floor(ms / 60000)
  const seconds = Math.round((ms % 60000) / 1000)
  const m = numberFormat(locale, { style: 'unit', unit: 'minute' }).format(minutes)
  return seconds ? `${m} ${numberFormat(locale, { style: 'unit', unit: 'second' }).format(seconds)}` : m
}

export function formatBytes(locale: string, bytes: number): string {
  const units = ['byte', 'kilobyte', 'megabyte', 'gigabyte'] as const
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return numberFormat(locale, {
    style: 'unit',
    unit: units[unit],
    unitDisplay: unit === 0 ? 'long' : 'short',
    maximumFractionDigits: unit === 0 ? 0 : 1,
  }).format(value)
}

const sameDay = (a: Date, b: Date) =>
  a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()

/** A unix timestamp (seconds) as a clock time today, or a short date and time otherwise. */
export function formatTime(locale: string, ts: number, now: Date = new Date()): string {
  const date = new Date(ts * 1000)
  if (sameDay(date, now)) {
    return dateFormat(locale, { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
  }
  return dateFormat(locale, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date)
}

export function formatDateTime(locale: string, ts: number): string {
  return dateFormat(locale, { dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(ts * 1000))
}

export function formatDate(locale: string, ts: number): string {
  return dateFormat(locale, { dateStyle: 'medium' }).format(new Date(ts * 1000))
}

/** "YYYY-MM-DD" as a short day label ("Oct 6"). */
export function formatDay(locale: string, day: string, style: 'short' | 'weekday' = 'short'): string {
  const [y, m, d] = day.split('-').map(Number)
  const date = new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1)
  if (style === 'weekday') return dateFormat(locale, { weekday: 'short' }).format(date)
  return dateFormat(locale, { month: 'short', day: 'numeric' }).format(date)
}

/** "3 min ago", "yesterday"... relative to `now`. */
export function formatRelative(locale: string, ts: number, now: number = Date.now() / 1000): string {
  const diff = ts - now
  const abs = Math.abs(diff)
  const rtf = relativeFormat(locale)
  if (abs < 45) return rtf.format(Math.round(diff), 'second')
  if (abs < 45 * 60) return rtf.format(Math.round(diff / 60), 'minute')
  if (abs < 22 * 3600) return rtf.format(Math.round(diff / 3600), 'hour')
  return rtf.format(Math.round(diff / 86400), 'day')
}

/** A list joined the way the language joins lists ("a, b and c"). */
export function formatList(locale: string, items: readonly string[]): string {
  return new Intl.ListFormat(locale, { style: 'long', type: 'conjunction' }).format(items)
}
