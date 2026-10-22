import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import {
  formatBytes,
  formatCompact,
  formatDate,
  formatDateTime,
  formatDay,
  formatDuration,
  formatList,
  formatNumber,
  formatPercent,
  formatRelative,
  formatTime,
} from './format'

/** Intl formatters bound to the active language. */
export function useFormat() {
  const { i18n } = useTranslation()
  const locale = i18n.resolvedLanguage ?? i18n.language ?? 'en'
  return useMemo(
    () => ({
      locale,
      number: (value: number, options?: Intl.NumberFormatOptions) => formatNumber(locale, value, options),
      compact: (value: number) => formatCompact(locale, value),
      percent: (ratio: number, digits?: number) => formatPercent(locale, ratio, digits),
      duration: (ms: number) => formatDuration(locale, ms),
      bytes: (bytes: number) => formatBytes(locale, bytes),
      time: (ts: number) => formatTime(locale, ts),
      dateTime: (ts: number) => formatDateTime(locale, ts),
      date: (ts: number) => formatDate(locale, ts),
      day: (day: string, style?: 'short' | 'weekday') => formatDay(locale, day, style),
      relative: (ts: number) => formatRelative(locale, ts),
      list: (items: readonly string[]) => formatList(locale, items),
    }),
    [locale],
  )
}

export type Formatters = ReturnType<typeof useFormat>
