import { describe, expect, it } from 'vitest'
import { actorLabel, auditQuery, auditTime, detailsText, filterProblem, newestFirst, validAuditLog } from './audit'

const t = (key: string, options?: Record<string, unknown>) => `${key}${options ? JSON.stringify(options) : ''}`
const actor = { id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' }
const row = (id = 1) => ({ id, ts: '2026-10-12T10:00:00Z', action: 'person.added', actor, target: { id: 'u_laurachenx', name: 'Laura Chen' }, details: {}, address: '10.0.0.8' })
const log = () => ({ period: { from: '2026-10-01', to: '2026-10-12' }, entries: [row()], actors: [actor], actions: ['person.added'], truncated: false, addresses: true })

describe('audit labels and unmodified metadata', () => {
  it('names people and keys, labels tokens and system actors, and falls back to IDs for unknown kinds', () => {
    for (const [kind, id, name, expected] of [
      ['person', 'u_emilycartr', 'Emily Carter', 'Emily Carter'], ['key', 'u_billingcix', 'Billing CI', 'Billing CI'],
      [null, 'u_gone000000', null, 'u_gone000000'], ['new-kind', 'actor-id', 'untrusted label', 'actor-id'],
      ['token', 'admin:3f9a', null, 'ee.audit.token{"id":"3f9a"}'], ['scim', 'scim:s_7hq2m4pzxa', null, 'ee.audit.scim{"id":"s_7hq2m4pzxa"}'],
      ['server', 'server', null, 'ee.audit.server'], ['unknown', 'unknown', null, 'ee.audit.unknown'],
    ]) expect(actorLabel(t, { id, name, kind } as never)).toBe(expected)
  })
  it('preserves event-only gaps, nested data, zero, false and inert text', () => {
    expect(detailsText({ from: null, to: null, entries: 0, dropped: 4, verified: false, outputs: ['splunk'], nested: { count: 0 }, text: '<img src=x>' }))
      .toBe('from: null · to: null · entries: 0 · dropped: 4 · verified: false · outputs: ["splunk"] · nested: {"count":0} · text: <img src=x>')
  })
  it('orders by IDs without mutating the input, regardless of timestamps', () => {
    const rows = [row(1), { ...row(3), ts: null }, { ...row(2), ts: '2027-01-01T00:00:00Z' }]
    expect(newestFirst(rows as never).map(r => r.id)).toEqual([3, 2, 1])
    expect(rows.map(r => r.id)).toEqual([1, 3, 2])
  })
  it('retains invalid and null timestamps using the localized fallback', () => {
    expect(auditTime(null, () => 'wrong', t)).toBe('ee.audit.timeUnavailable')
    expect(auditTime('invalid', () => 'wrong', t)).toBe('ee.audit.timeUnavailable')
    expect(auditTime('2026-10-12T00:00:00Z', seconds => String(seconds), t)).toBe('1791763200')
  })
  it('preserves date-only intent and literal filters, permitting only the server length limit', () => {
    expect(auditQuery({ from: '2026-03-29', to: '2026-03-29' }, 'person_%\\*', null)).toEqual({ from: '2026-03-29', to: '2026-03-29', action: 'person_%\\*' })
    expect(filterProblem({ from: '2026-01-01', to: '2026-01-01', action: 'x'.repeat(200), actor: '😀'.repeat(200) })).toBe(false)
    expect(filterProblem({ from: '', to: '', actor: 'x'.repeat(201) })).toBe(true)
  })
})

describe('audit response validation before display or export', () => {
  it('accepts nullable time and unknown string kinds, preserving rows', () => {
    const value = log(); value.entries[0] = { ...row(), ts: null, actor: { ...actor, kind: 'future' } } as never
    expect(validAuditLog(value)).toBe(true)
  })
  it.each([
    { entries: null }, { entries: {} }, { actors: 'actors' }, { actions: [1] }, { addresses: 'yes' }, { truncated: 0 },
    { period: { from: 1, to: '2026-10-12' } }, { entries: [{ ...row(), actor: null }] },
    { entries: [{ ...row(), actor: { ...actor, kind: ['person'] } }] }, { actors: [{ ...actor, name: {} }] },
    { entries: [{ ...row(), target: { id: 1, name: null } }] }, { entries: [{ ...row(), details: [] }] },
    { entries: [{ ...row(), details: null }] }, { entries: [{ ...row(), ts: 12 }] }, { entries: [{ ...row(), action: [] }] },
    { entries: [{ ...row(), address: {} }] }, { entries: [row(), row()] },
  ])('rejects malformed consumed fields %#', change => expect(validAuditLog({ ...log(), ...change })).toBe(false))
})
