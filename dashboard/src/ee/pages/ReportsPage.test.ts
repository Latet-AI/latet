import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { EnterpriseReadOnly } from '../components/TrialEnded'
import type { Report } from '../types'

const state: { data?: Report; error?: unknown; busy: boolean; company: boolean } = { busy: false, company: false }
vi.mock('../lib/useReports', () => ({ useReports: () => ({ ...state, reload: () => undefined, exportAs: async () => undefined }) }))
const { ReportsPage, ReportView } = await import('./ReportsPage')
const counts = { exchanges: 10, masked: 4, stopped: 2 }
const residual = { count: 0, exchanges: 0, masked: 0, stopped: 0 }
function report(): Report {
  return {
    period: { from: '2026-10-01', to: '2026-10-02', days: 2, step: 'day' }, previous: { from: '2026-09-29', to: '2026-09-30' },
    people: 'named', filters: { group: null, project: null, agent: null, category: null },
    totals: { ...counts, approvals: { approved: 2 }, changes: { approved: 1 }, apps: { mask: 3 }, uncounted: 1 }, before: counts,
    series: [{ key: '2026-10-01', ...counts }], by_group: [], by_project: [], by_agent: [], by_key: [],
    by_person: [{ key: 'Emily Carter', label: 'Emily Carter', ...counts }], by_approver: [],
    others: { by_group: residual, by_project: residual, by_agent: residual, by_key: residual, by_person: residual, by_approver: { count: 0, approvals: 0, changes: 0 } },
    by_category: [{ key: 'CLIENT_NAME', masked: 4 }], stopped_by_reason: [],
    options: { groups: [{ id: 'g_paymentsaa', name: 'Private group' }], projects: ['Private project'], agents: [], categories: [] },
    viewer: { groups: null, people: 'named', siem: 'edit' },
  }
}
function page(readOnly = false, location = '/reports') {
  return renderToStaticMarkup(h(MemoryRouter, { initialEntries: [location] }, h(EnterpriseReadOnly.Provider, { value: readOnly }, h(ReportsPage))))
}
describe('Reports presentation', () => {
  it('shows application calls when the selected category has no exchanges, values or decisions', () => {
    const data = report()
    data.filters.category = 'ABSENT_TYPE'
    data.totals = { exchanges: 0, masked: 0, stopped: 0, approvals: {}, changes: {}, apps: { mask: 5, scan: 2 }, uncounted: 0 }
    const html = renderToStaticMarkup(h(ReportView, { report: data }))
    expect(html).toContain('Applications API calls')
    expect(html).toContain('5 to mask texts, 2 to scan them')
    expect(html).not.toMatch(/<h2[^>]*>Nothing in this period<\/h2>/)
    data.totals.apps = { mask: 0, scan: 0 }
    expect(renderToStaticMarkup(h(ReportView, { report: data }))).toMatch(/<h2[^>]*>Nothing in this period<\/h2>/)
  })
  it('hides exports when the enclosing Enterprise page is read-only', () => {
    Object.assign(state, { data: report(), busy: false, error: undefined, company: false })
    expect(page()).toContain('Export CSV')
    expect(page(true)).not.toContain('Export CSV'); expect(page(true)).not.toContain('Export JSON')
  })
  it.each([403, 413, 429])('does not render old privileged data, options or export after %s', status => {
    Object.assign(state, { data: report(), busy: false, error: new ApiError(status, 'refused'), company: false })
    const html = page()
    for (const secret of ['Emily Carter', 'Private group', 'Private project', 'Export CSV', 'Export JSON']) expect(html).not.toContain(secret)
  })
  it('hides stale names and exports for invalid dates and during reload', () => {
    Object.assign(state, { data: report(), busy: false, error: undefined, company: false })
    const invalid = page(false, '/reports?period=custom&from=2026-02-30&to=2026-03-01')
    expect(invalid).not.toContain('Emily Carter'); expect(invalid).not.toContain('Private project'); expect(invalid).not.toContain('Export CSV')
    state.busy = true
    const loading = page()
    expect(loading).not.toContain('Emily Carter'); expect(loading).not.toContain('Private project')
  })
  it('shows each count-zero positive residual, including anonymous approvers', () => {
    const data = report()
    for (const key of ['by_group', 'by_project', 'by_agent', 'by_key', 'by_person'] as const) data.others[key] = { count: 0, exchanges: 2, masked: 3, stopped: 1 }
    data.others.by_approver = { count: 0, approvals: 2, changes: 1 }
    const html = renderToStaticMarkup(h(ReportView, { report: data }))
    expect(html.match(/Others \(0\)/g)).toHaveLength(6)
    expect(html).toContain('3 values masked, 1 requests stopped')
    expect(html).toContain('2 in the Live Reviewer, 1 changes to the settings')
  })
  it('does not invent person or approver lists in group-only reports', () => {
    const data = report()
    data.people = 'group-only'; data.by_person = null; data.by_approver = null
    data.others.by_person = null; data.others.by_approver = null
    const html = renderToStaticMarkup(h(ReportView, { report: data }))
    expect(html).not.toContain('By person'); expect(html).not.toContain('Decisions by who made them')
  })
})
