import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import type { Report, ReportFile, ReportQuery } from '../types'
import { ReportGate, ReportSession } from './reportSession'

const query = (project: string): ReportQuery => ({ from: '2026-10-01', to: '2026-10-02', project })
const file: ReportFile = { name: 'report.csv', type: 'text/csv', file: '\uFEFFday,project\r\n2026-10-01,"\'=SUM(1,2)"\r\n' }
// Complete server-shaped data: stale identities and choices are the sensitive boundary under test.
function report(project: string): Report {
  const counts = { exchanges: 10, stopped: 1, masked: 4 }
  const residual = { count: 0, exchanges: 0, stopped: 0, masked: 0 }
  return {
    period: { from: '2026-10-01', to: '2026-10-02', days: 2, step: 'day' }, previous: { from: '2026-09-29', to: '2026-09-30' },
    filters: { group: null, project, agent: null, category: null }, people: 'named',
    totals: { ...counts, approvals: {}, changes: {}, apps: {}, uncounted: 0 }, before: counts,
    series: [{ key: '2026-10-01', ...counts }], by_group: [], by_project: [{ key: project, label: project, ...counts }],
    by_agent: [], by_key: [], by_person: [{ key: 'Emily Carter', label: 'Emily Carter', ...counts }], by_approver: [],
    others: { by_group: residual, by_project: residual, by_agent: residual, by_key: residual, by_person: residual, by_approver: { count: 0, approvals: 0, changes: 0 } },
    by_category: [], stopped_by_reason: [], options: { groups: [], projects: [project], agents: [], categories: [] },
    viewer: { groups: null, people: 'named', siem: 'edit' },
  }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const tick = async () => { for (let n = 0; n < 12; n++) await Promise.resolve() }
function setup(gate = new ReportGate()) {
  const reads: { q: ReportQuery; done: ReturnType<typeof deferred<Report>> }[] = []
  const exports: { q: ReportQuery; done: ReturnType<typeof deferred<ReportFile>> }[] = []
  const saved: ReportFile[] = []
  const session = new ReportSession({
    report: (q: ReportQuery) => { const done = deferred<Report>(); reads.push({ q, done }); return done.promise },
    exportReport: (q: ReportQuery) => { const done = deferred<ReportFile>(); exports.push({ q, done }); return done.promise },
  }, (value: ReportFile) => saved.push(value), gate)
  return { session, reads, exports, saved }
}

describe('one report/export operation until settlement', () => {
  it('coalesces rapid filters and refuses exports until the current read succeeds', async () => {
    const { session, reads, exports } = setup()
    session.setQuery(query('A')); await tick()
    session.setQuery(query('B')); session.setQuery(query('C'))
    await session.exportAs('csv'); await tick()
    expect(reads.map(r => r.q.project)).toEqual(['A']); expect(exports).toHaveLength(0)
    reads[0]!.done.resolve(report('A')); await tick()
    expect(reads.map(r => r.q.project)).toEqual(['A', 'C'])
    expect(session.snapshot().data).toBeUndefined()
    reads[1]!.done.resolve(report('C')); await tick()
    expect(session.snapshot().data?.filters.project).toBe('C')
    expect(session.snapshot().busy).toBe(false)
  })
  it('waits for export settlement before reading changed filters and suppresses stale downloads', async () => {
    const { session, reads, exports, saved } = setup()
    session.setQuery(query('A')); await tick(); reads[0]!.done.resolve(report('A')); await tick()
    const exporting = session.exportAs('csv'); await tick()
    await session.exportAs('json'); session.setQuery(query('B')); await tick()
    expect(exports).toHaveLength(1); expect(reads).toHaveLength(1)
    expect(session.snapshot().data).toBeUndefined()
    exports[0]!.done.resolve(file); await exporting; await tick()
    expect(saved).toEqual([]); expect(reads[1]?.q.project).toBe('B')
    reads[1]!.done.resolve(report('B')); await tick()
  })
  it('passes export bytes unchanged to the download/toast effect only for a current mounted view', async () => {
    const { session, reads, exports, saved } = setup()
    session.setQuery(query('A')); await tick(); reads[0]!.done.resolve(report('A')); await tick()
    const first = session.exportAs('csv'); await tick(); exports[0]!.done.resolve(file); await first
    expect(saved).toEqual([file])
    const second = session.exportAs('json'); await tick(); session.dispose()
    exports[1]!.done.resolve({ ...file, file: '{"administration_truncated":true}' }); await second
    expect(saved).toEqual([file])
  })
  it('keeps the shared gate across unmount/remount without treating abort as release', async () => {
    const gate = new ReportGate()
    const a = setup(gate), b = setup(gate)
    a.session.setQuery(query('A')); await tick(); a.session.dispose()
    b.session.setQuery(query('B')); await tick(); expect(b.reads).toHaveLength(0)
    a.reads[0]!.done.resolve(report('A')); await tick()
    expect(b.reads).toHaveLength(1); expect(a.session.snapshot().data).toBeUndefined()
    b.reads[0]!.done.resolve(report('B')); await tick()
  })
  it.each([403, 413, 429])('clears identities/options after %s and recovers on retry', async status => {
    const { session, reads, exports } = setup()
    session.setQuery(query('A')); await tick(); reads[0]!.done.resolve(report('A')); await tick()
    session.setQuery(query('A')); await tick()
    reads[1]!.done.reject(new ApiError(status, 'refused')); await tick()
    expect(session.snapshot().data).toBeUndefined(); expect(session.snapshot().error).toBeInstanceOf(ApiError)
    await session.exportAs('csv'); expect(exports).toHaveLength(0)
    session.setQuery(query('B')); await tick(); reads[2]!.done.resolve(report('B')); await tick()
    expect(session.snapshot().data?.filters.project).toBe('B')
  })
  it('clears data and choices on invalid dates or a production pause', async () => {
    const { session, reads, exports } = setup()
    session.setQuery(query('A')); await tick(); reads[0]!.done.resolve(report('A')); await tick()
    session.setQuery({ ...query('A'), from: '2026-02-30' }); await tick()
    expect(session.snapshot().data).toBeUndefined(); expect(reads).toHaveLength(1)
    await session.exportAs('csv'); expect(exports).toHaveLength(0)
    session.setQuery(query('A')); await tick()
    reads[1]!.done.reject(new ApiError(403, 'paused', [], { code: 'not-producing', paused: { why: 'users', expires: null } })); await tick()
    expect(session.snapshot().data).toBeUndefined(); expect(session.snapshot().busy).toBe(false)
  })
})
