import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { pausedOf, presetRange, rangeProblem, reasonLabel, reportQuery, splitOutcomes, trendOf, hasResidual } from './reports'

describe('report periods', () => {
  it.each([
    ['2024-02-29', '2024-02-29', null], ['2025-02-29', '2025-03-01', 'format'],
    ['2026-04-31', '2026-05-01', 'format'], ['1999-12-31', '2000-01-01', 'format'],
    ['2999-12-31', '3000-01-01', 'format'], ['2026-1-01', '2026-01-31', 'format'],
    ['2000-01-01', '2000-12-31', null], ['2024-01-01', '2025-01-01', 'length'],
    ['2026-10-02', '2026-10-01', 'order'], ['', '2026-10-01', 'format'],
  ])('validates real bounded calendar days %s through %s', (from, to, expected) => {
    expect(rangeProblem(from, to)).toBe(expected)
  })
  it('counts calendar days across leap years and clock changes', () => {
    expect(presetRange('30', new Date(2024, 2, 1))).toEqual({ from: '2024-02-01', to: '2024-03-01' })
    expect(presetRange('7', new Date(2026, 2, 30))).toEqual({ from: '2026-03-24', to: '2026-03-30' })
    expect(presetRange('7', new Date(2026, 9, 26))).toEqual({ from: '2026-10-20', to: '2026-10-26' })
  })
})

it('keeps only selected report filters', () => {
  expect(reportQuery({ from: '2026-10-01', to: '2026-10-02' }, { group: 'g_paymentsaa', project: null, agent: '', category: 'EMAIL' }))
    .toEqual({ from: '2026-10-01', to: '2026-10-02', group: 'g_paymentsaa', category: 'EMAIL' })
})
it('compares totals and distinguishes a new total from zero', () => {
  expect(trendOf(150, 100)).toEqual({ direction: 'up', ratio: 0.5 })
  expect(trendOf(75, 100)).toEqual({ direction: 'down', ratio: 0.25 })
  expect(trendOf(5, 0)).toEqual({ direction: 'new', ratio: null })
  expect(trendOf(0, 0)).toEqual({ direction: 'same', ratio: 0 })
  expect(splitOutcomes({ approved: 4, rejected: 1, expired: 2 }, ['approved'])).toEqual({ through: 4, other: 3 })
})
it('retains anonymous residual activity even with no distinct names', () => {
  expect(hasResidual({ count: 0, exchanges: 0, stopped: 1, masked: 0 })).toBe(true)
  expect(hasResidual({ count: 0, approvals: 0, changes: 2 })).toBe(true)
  expect(hasResidual({ count: 5, exchanges: 0, stopped: 0, masked: 0 })).toBe(true)
  expect(hasResidual({ count: 0, exchanges: 0, stopped: 0, masked: 0 })).toBe(false)
  expect(hasResidual(null)).toBe(false)
})
it('labels every carried stop reason and falls back for a future code', () => {
  expect(reasonLabel('unread-request')).toBe('ee.reports.reasons.unreadRequest')
  expect(reasonLabel('too-large')).toBe('ee.reports.reasons.tooLarge')
  expect(reasonLabel('live-reviewer')).toBe('ee.reports.reasons.liveReviewer')
  expect(reasonLabel('settings-changed')).toBe('ee.reports.reasons.settingsChanged')
  expect(reasonLabel('future-code')).toBe('ee.reports.reasons.other')
})
it('recognizes only the structured production refusal', () => {
  for (const why of ['expired', 'users', 'trial-ended', 'unusable']) {
    expect(pausedOf(new ApiError(403, 'paused', [], { code: 'not-producing', paused: { why, expires: null } }))).toEqual({ why, expires: null })
  }
  expect(pausedOf(new ApiError(403, 'denied', [], { code: 'company-sign-in' }))).toBeNull()
  expect(pausedOf(new ApiError(429, 'busy'))).toBeNull()
  expect(pausedOf(new Error('offline'))).toBeNull()
})
