import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { AuditSession } from './auditSession'
import { ReportGate } from './reportSession'
import type { AuditLogView, AuditQuery, ReportFile } from '../types'

const query = (action = 'person.*'): AuditQuery => ({ from: '2026-10-01', to: '2026-10-12', action })
const view = (): AuditLogView => ({ period: { from: '2026-10-01', to: '2026-10-12' }, entries: [{ id: 1, ts: null, actor: { id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' }, action: 'person.added', target: null, details: {}, address: '10.0.0.8' }], actions: ['person.added'], actors: [{ id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' }], addresses: true, truncated: false })
const file: ReportFile = { name: 'audit.csv', type: 'text/csv', file: '\uFEFFtime,actor\r\n,"\'=SUM(1,2)"\r\nZürich\r\n' }
function deferred<T>() { let resolve!: (v: T) => void, reject!: (e: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const tick = async () => { for (let n = 0; n < 12; n++) await Promise.resolve() }
function setup(gate = new ReportGate()) {
  const reads: { q: AuditQuery; done: ReturnType<typeof deferred<AuditLogView>> }[] = [], exports: { q: AuditQuery; done: ReturnType<typeof deferred<ReportFile>> }[] = [], saved: ReportFile[] = []
  const session = new AuditSession({ log: (q: AuditQuery) => { const done = deferred<AuditLogView>(); reads.push({ q, done }); return done.promise }, exportLog: (q: AuditQuery) => { const done = deferred<ReportFile>(); exports.push({ q, done }); return done.promise } }, (f: ReportFile) => saved.push(f), gate)
  return { session, reads, exports, saved }
}
describe('current audit query and export lifecycle', () => {
  it('reads independently of the Reports person gate, but queues exports behind it', async () => {
    const gate = new ReportGate(), hold = deferred<void>(); const held = gate.run(() => hold.promise)
    const { session, reads, exports } = setup(gate)
    session.setQuery(query()); await tick(); expect(reads).toHaveLength(1)
    reads[0]!.done.resolve(view()); await tick(); const exporting = session.exportAs('csv'); await tick()
    expect(exports).toHaveLength(0); hold.resolve(); await held; await tick(); expect(exports).toHaveLength(1)
    exports[0]!.done.resolve(file); await exporting
  })
  it('excludes same-tick CSV/JSON and suppresses obsolete export effects when filters change', async () => {
    const { session, reads, exports, saved } = setup()
    session.setQuery(query()); await tick(); reads[0]!.done.resolve(view()); await tick()
    const exporting = session.exportAs('csv'); void session.exportAs('json'); await tick()
    expect(exports).toHaveLength(1); expect(exports[0]!.q).toEqual(query())
    session.setQuery(query('key.*')); await tick(); expect(session.snapshot().data).toBeUndefined()
    exports[0]!.done.resolve(file); await exporting; expect(saved).toEqual([])
    reads[1]!.done.resolve(view()); await tick()
  })
  it('discards stale reads, invalid dates, overlong filters and malformed current responses', async () => {
    const { session, reads, exports } = setup()
    session.setQuery(query()); session.setQuery(query('key.*')); await tick()
    reads[0]!.done.resolve(view()); await tick(); expect(session.snapshot().data).toBeUndefined()
    reads[1]!.done.resolve({ ...view(), entries: null } as never); await tick(); expect(session.snapshot().error).toBeDefined()
    await session.exportAs('csv'); expect(exports).toHaveLength(0)
    session.setQuery({ ...query(), from: '2026-02-30' }); session.setQuery(query('x'.repeat(201))); await tick()
    expect(reads).toHaveLength(2); expect(session.snapshot().data).toBeUndefined()
  })
  it.each([403, 413, 429])('clears data/options on %s, never retries, and permits explicit recovery', async status => {
    const { session, reads, exports } = setup()
    session.setQuery(query()); await tick(); reads[0]!.done.resolve(view()); await tick()
    const exporting = session.exportAs('csv'); await tick(); exports[0]!.done.reject(new ApiError(status, 'refused')); await exporting; await tick()
    expect(session.snapshot().data).toBeUndefined(); expect(exports).toHaveLength(1)
    session.setQuery(query()); await tick(); reads[1]!.done.resolve(view()); await tick(); expect(session.snapshot().data).toBeDefined()
  })
  it('preserves server bytes and suppresses downloads, notifications and later reads after disposal', async () => {
    const { session, reads, exports, saved } = setup()
    session.setQuery(query()); await tick(); reads[0]!.done.resolve(view()); await tick()
    const exporting = session.exportAs('csv'); await tick(); exports[0]!.done.resolve(file); await exporting; expect(saved).toEqual([file])
    const late = session.exportAs('json'); await tick(); session.dispose(); exports[1]!.done.resolve(file); await late
    session.setQuery(query()); await tick(); expect(reads).toHaveLength(1); expect(saved).toEqual([file]); expect(session.snapshot().data).toBeUndefined()
  })
  it('shows company sign-in only for that server refusal and drops data after a production pause', async () => {
    const { session, reads, exports } = setup()
    session.setQuery(query()); await tick(); reads[0]!.done.resolve(view()); await tick()
    const exporting = session.exportAs('csv'); await tick(); exports[0]!.done.reject(new ApiError(403, 'company', [], { code: 'company-sign-in' })); await exporting
    expect(session.snapshot().company).toBe(true); expect(session.snapshot().data).toBeUndefined()
    session.setQuery(query()); await tick(); reads[1]!.done.reject(new ApiError(403, 'paused', [], { code: 'not-producing', paused: { why: 'users', expires: null } })); await tick()
    expect(session.snapshot().company).toBe(false); expect(session.snapshot().data).toBeUndefined()
  })
})
