import { describe, expect, it } from 'vitest'
import type { ExchangeSummary } from '../api/types'
import { aggregateByType, cacheReadRatio, dayRange, fillDays, niceMax, niceTicks, OTHER, rankEntries, share, totals } from './stats'

describe('totals', () => {
  it('sums exchanges, masked values and blocked requests over the days', () => {
    expect(
      totals([
        { day: '2026-10-05', exchanges: 10, masked: 4, blocked: 1 },
        { day: '2026-10-06', exchanges: 5, masked: 0, blocked: 0 },
      ]),
    ).toEqual({ exchanges: 15, masked: 4, blocked: 1 })
    expect(totals([])).toEqual({ exchanges: 0, masked: 0, blocked: 0 })
  })
})

describe('rankEntries', () => {
  it('sorts by value, then by key, and drops zeros', () => {
    expect(rankEntries({ PHONE: 2, EMAIL: 5, IBAN: 2, JWT: 0 })).toEqual([
      { key: 'EMAIL', value: 5 },
      { key: 'IBAN', value: 2 },
      { key: 'PHONE', value: 2 },
    ])
  })

  it('folds the tail into one "other" row so the list fits the limit', () => {
    const ranked = rankEntries({ A: 9, B: 7, C: 3, D: 2, E: 1 }, 3)
    expect(ranked).toEqual([
      { key: 'A', value: 9 },
      { key: 'B', value: 7 },
      { key: OTHER, value: 6 },
    ])
  })

  it('does not fold when the entries fit', () => {
    expect(rankEntries({ A: 1, B: 2 }, 2)).toHaveLength(2)
  })
})

describe('niceTicks', () => {
  it('gives whole-number ticks for small counts', () => {
    expect(niceTicks(3)).toEqual([0, 1, 2, 3])
    expect(niceTicks(1)).toEqual([0, 1])
  })

  it('rounds the top to a nice value', () => {
    expect(niceTicks(37)).toEqual([0, 10, 20, 30, 40])
    expect(niceTicks(1234)).toEqual([0, 500, 1000, 1500])
    expect(niceMax(87)).toBe(100)
  })

  it('allows fractional steps for ratios', () => {
    expect(niceTicks(0.83, 4, false)).toEqual([0, 0.25, 0.5, 0.75, 1])
  })

  it('has a usable axis when there is no data', () => {
    expect(niceTicks(0)).toEqual([0, 1])
  })
})

describe('days', () => {
  it('lists the days of a period, oldest first', () => {
    expect(dayRange(new Date(2026, 9, 6), 3)).toEqual(['2026-10-04', '2026-10-05', '2026-10-06'])
  })

  it('crosses month boundaries', () => {
    expect(dayRange(new Date(2026, 9, 1), 2)).toEqual(['2026-09-30', '2026-10-01'])
  })

  it('fills the days the daemon did not report with zeros', () => {
    const filled = fillDays([{ day: '2026-10-05', exchanges: 3, masked: 1, blocked: 0 }], 3, new Date(2026, 9, 6))
    expect(filled).toEqual([
      { day: '2026-10-04', exchanges: 0, masked: 0, blocked: 0 },
      { day: '2026-10-05', exchanges: 3, masked: 1, blocked: 0 },
      { day: '2026-10-06', exchanges: 0, masked: 0, blocked: 0 },
    ])
  })

  it('keeps reported days outside the local range instead of dropping them', () => {
    const filled = fillDays([{ day: '2026-10-07', exchanges: 1, masked: 0, blocked: 0 }], 1, new Date(2026, 9, 6))
    expect(filled.map((d) => d.day)).toEqual(['2026-10-06', '2026-10-07'])
  })
})

describe('ratios and aggregates', () => {
  it('computes the cache read ratio as the daemon defines it', () => {
    expect(cacheReadRatio({ input: 10, cache_read: 80, cache_write: 10 })).toBe(0.8)
    expect(cacheReadRatio({ input: 0, cache_read: 0, cache_write: 0 })).toBeNull()
  })

  it('returns null for a share of nothing', () => {
    expect(share(1, 0)).toBeNull()
    expect(share(1, 4)).toBe(0.25)
  })

  it('sums masked values per type over exchanges', () => {
    const base = { by_type: {} } as ExchangeSummary
    expect(
      aggregateByType([
        { ...base, by_type: { EMAIL: 2, PASSWORD: 1 } },
        { ...base, by_type: { EMAIL: 1 } },
      ]),
    ).toEqual({ EMAIL: 3, PASSWORD: 1 })
  })
})
