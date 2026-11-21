import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import type { SiemOutputSettings, SiemOutputView, SiemTest, SiemView } from '../types'
import { ReportGate } from './reportSession'
import { SiemSession } from './siemSession'

const output: SiemOutputView = { name: 'remote', kind: 'webhook', url: 'https://collector.example/in', headers: {}, secret: null, format: 'json', has_secret: false, secret_destination_changed: false, queued: 8, dropped: 0, collapsed: 0, last_ok: null, failing_since: null, last_error: null, blocked: false, held: false }
const view = (etag = 'old'): SiemView => ({ outputs: [{ ...output }], editable: true, etag, paused: null })
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const tick = async () => { for (let i = 0; i < 15; i++) await Promise.resolve() }
function setup(gate = new ReportGate(), editing = true) {
  const reads: ReturnType<typeof deferred<SiemView>>[] = []
  const writes: { outputs: SiemOutputSettings[]; secrets: Record<string, string | null>; etag: string; done: ReturnType<typeof deferred<SiemView>> }[] = []
  const tests: { name: string; done: ReturnType<typeof deferred<SiemTest>> }[] = []
  const notices: string[] = []
  const session = new SiemSession({
    siem: () => { const done = deferred<SiemView>(); reads.push(done); return done.promise },
    saveSiem: (outputs, secrets, etag) => { const done = deferred<SiemView>(); writes.push({ outputs, secrets, etag, done }); return done.promise },
    testSiem: name => { const done = deferred<SiemTest>(); tests.push({ name, done }); return done.promise },
  }, editing, kind => notices.push(kind), gate)
  const ready = async () => { session.reload(); await tick(); reads[0]!.resolve(view()); await tick() }
  return { session, reads, writes, tests, notices, ready }
}

describe('SIEM intent and operation lifecycle', () => {
  it('says that an output at http:// or on the server, and its secret, are the command\'s when the server refuses a change of it', async () => {
    const s = setup(); await s.ready()
    s.session.edit('remote'); s.session.change({ ...s.session.snapshot().draft!, secret: 'synthetic-canary' })
    s.session.save(); await tick()
    s.writes[0]!.done.reject(new ApiError(403, 'the secret of an output at http:// ... is set with `latet server siem secret`', [], { error: 'refused', code: 'by-command' })); await tick()
    expect(s.session.snapshot()).toMatchObject({ busy: false, company: false, error: 'byCommand', draft: null })
    expect(JSON.stringify(s.session.snapshot())).not.toContain('synthetic-canary')
  })

  it.each(['kind', 'format', 'paused.why'])('fails closed on array %s after refresh and recovers on a valid retry', async field => {
    const s = setup(); await s.ready()
    s.session.edit('remote'); s.session.change({ ...s.session.snapshot().draft!, secret: 'synthetic-draft-canary' })
    s.session.reload(); await tick()
    const malformed = field === 'paused.why'
      ? { ...view('malformed'), paused: { why: ['expired'], expires: null } }
      : { ...view('malformed'), outputs: [{ ...output, [field]: [field === 'kind' ? 'webhook' : 'json'] }] }
    s.reads[1]!.resolve(malformed as unknown as SiemView); await tick()
    expect(s.session.snapshot()).toEqual({ draft: null, removing: null, busy: false, company: false, changed: false, error: 'read' })
    expect(s.session.editable()).toBe(false)
    s.session.edit('remote'); s.session.save(); s.session.remove('remote'); s.session.confirmRemove(); s.session.test('remote'); await tick()
    expect(s.writes).toHaveLength(0); expect(s.tests).toHaveLength(0); expect(s.notices).toEqual([])
    expect(JSON.stringify(s.session.snapshot())).not.toContain('synthetic-draft-canary')
    s.session.reload(); await tick(); s.reads[2]!.resolve(view('fresh')); await tick()
    expect(s.session.snapshot().error).toBeUndefined(); expect(s.session.editable()).toBe(true)
    s.session.edit('remote'); s.session.save(); await tick()
    expect(s.writes).toHaveLength(1); expect(s.writes[0]!.etag).toBe('fresh'); expect(s.writes[0]!.secrets).toEqual({})
    s.writes[0]!.done.resolve(view('saved')); await tick(); s.reads[3]!.resolve(view('saved')); await tick()
    expect(s.notices).toEqual(['saved'])
  })
  it('captures the editor etag, freezes submission, and excludes all writes/tests through refresh', async () => {
    const s = setup(); await s.ready()
    s.session.edit('remote')
    const d = s.session.snapshot().draft!
    s.session.change({ ...d, secret: 'synthetic-canary' })
    s.session.save(); s.session.save(); s.session.test('remote'); s.session.remove('remote')
    await tick()
    expect(s.writes).toHaveLength(1); expect(s.tests).toHaveLength(0)
    expect(s.writes[0]).toMatchObject({ etag: 'old', secrets: { remote: 'synthetic-canary' } })
    expect(s.session.snapshot().draft).toBeNull()
    expect(JSON.stringify(s.writes[0]!.outputs)).not.toContain('synthetic-canary')
    s.writes[0]!.done.resolve(view('new')); await tick()
    s.session.test('remote'); s.session.edit('remote'); await tick()
    expect(s.tests).toHaveLength(0); expect(s.session.snapshot().busy).toBe(true)
    s.reads[1]!.resolve(view('new')); await tick()
    expect(s.session.snapshot().busy).toBe(false); expect(s.notices).toEqual(['saved'])
  })
  it('discards conflicted secret and removal intents before deferred refresh and never retries them', async () => {
    for (const removal of [false, true]) {
      const s = setup(); await s.ready()
      if (removal) s.session.remove('remote')
      else { s.session.edit('remote'); s.session.change({ ...s.session.snapshot().draft!, secret: 'synthetic-canary' }) }
      if (removal) s.session.confirmRemove(); else s.session.save()
      await tick(); s.writes[0]!.done.reject(new ApiError(409, 'synthetic-canary', [], { code: 'siem-changed' })); await tick()
      expect(s.session.snapshot()).toMatchObject({ draft: null, removing: null, changed: true, busy: true })
      expect(JSON.stringify(s.session.snapshot())).not.toContain('synthetic-canary')
      s.session.save(); s.session.confirmRemove(); await tick(); expect(s.writes).toHaveLength(1)
      s.reads[1]!.resolve({ ...view('new'), outputs: [] }); await tick()
      s.session.save(); s.session.confirmRemove(); expect(s.writes).toHaveLength(1)
      expect(s.notices).toEqual([])
    }
  })
  it('serializes same-tick tests and sends only a saved name', async () => {
    const s = setup(); await s.ready()
    s.session.test('remote'); s.session.test('remote'); s.session.test('unsaved'); await tick()
    expect(s.tests.map(t => t.name)).toEqual(['remote'])
    s.tests[0]!.done.resolve({ ok: false, detail: '<img src=x>synthetic-receiver-canary' }); await tick()
    expect(s.notices).toEqual([])
    s.reads[1]!.resolve(view()); await tick()
    expect(s.notices).toEqual(['testFail']); expect(JSON.stringify(s.session.snapshot())).not.toContain('synthetic-receiver-canary')
  })
  it.each([403, 413, 429])('fails closed after %s without echoing the request or auto retrying', async status => {
    const s = setup(); await s.ready(); s.session.edit('remote'); s.session.save(); await tick()
    s.writes[0]!.done.reject(new ApiError(status, 'synthetic-error-canary')); await tick()
    expect(s.session.snapshot().data).toBeUndefined(); expect(s.session.snapshot().draft).toBeNull()
    expect(s.session.snapshot().error).toBeTruthy(); expect(JSON.stringify(s.session.snapshot())).not.toContain('synthetic-error-canary')
    s.session.save(); s.session.test('remote'); await tick(); expect(s.writes).toHaveLength(1); expect(s.tests).toHaveLength(0)
    s.session.reload(); await tick(); s.reads[1]!.resolve(view('fresh')); await tick()
    s.session.edit('remote'); s.session.save(); await tick(); expect(s.writes[1]!.etag).toBe('fresh')
  })
  it('distinguishes company sign-in and clears data on failed refresh after a successful save', async () => {
    const s = setup(); await s.ready(); s.session.test('remote'); await tick()
    s.tests[0]!.done.reject(new ApiError(403, 'fixed', [], { code: 'company-sign-in' })); await tick()
    expect(s.session.snapshot().company).toBe(true)
    s.session.reload(); await tick(); s.reads[1]!.resolve(view()); await tick()
    s.session.edit('remote'); s.session.save(); await tick(); s.writes[0]!.done.resolve(view('new')); await tick()
    s.reads[2]!.reject(new Error('private-address')); await tick()
    expect(s.session.snapshot().data).toBeUndefined(); expect(s.notices).toEqual([])
  })
  it('suppresses late effects and holds the shared gate through a disposed mutation', async () => {
    const gate = new ReportGate(); const a = setup(gate); await a.ready()
    a.session.edit('remote'); a.session.save(); await tick(); a.session.dispose()
    const b = setup(gate, false); b.session.reload(); await tick(); expect(b.reads).toHaveLength(0)
    a.writes[0]!.done.resolve(view('new')); await tick()
    expect(a.notices).toEqual([]); expect(a.reads).toHaveLength(1); expect(a.session.snapshot().data).toBeUndefined()
    b.reads[0]!.resolve({ ...view(), editable: false, etag: null }); await tick()
    b.session.edit('remote'); b.session.test('remote'); expect(b.session.snapshot().draft).toBeNull(); expect(b.tests).toHaveLength(0)
  })
  it('allows local removal but refuses local edits/tests and frozen existing names', async () => {
    const s = setup(); s.session.reload(); await tick(); s.reads[0]!.resolve({ ...view(), outputs: [{ ...output, url: 'http://localhost:99/in' }] }); await tick()
    s.session.edit('remote'); s.session.test('remote'); expect(s.session.snapshot().draft).toBeNull(); expect(s.tests).toHaveLength(0)
    s.session.remove('remote'); expect(s.session.snapshot().removing?.queued).toBe(8)
    s.session.cancel(); s.session.reload(); await tick(); s.reads[1]!.resolve(view()); await tick()
    s.session.edit('remote'); s.session.change({ ...s.session.snapshot().draft!, name: 'renamed' })
    expect(s.session.snapshot().draft?.name).toBe('remote')
  })
})
