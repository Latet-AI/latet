import { companySignInNeeded } from '../../lib/refused'
import type { AuditLogView, AuditQuery, ReportFile } from '../types'
import { filterProblem, validAuditLog } from './audit'
import { reportOperationGate } from './reportSession'
import { rangeProblem } from './reports'

interface AuditTransport { log(query: AuditQuery): Promise<AuditLogView>; exportLog(query: AuditQuery, format: 'csv' | 'json'): Promise<ReportFile> }
interface AuditState { key: string; data?: AuditLogView; error?: unknown; busy: boolean; company: boolean }
export class AuditResponseError extends Error {}

/** Reads need no server person slot. Exports share Reports' settlement gate across page mounts. */
export class AuditSession {
  private state: AuditState = { key: '', busy: false, company: false }
  private listeners = new Set<() => void>()
  private revision = 0
  private mounted = true
  private query: AuditQuery | null = null
  constructor(private api: AuditTransport, private saved: (file: ReportFile) => void, private gate = reportOperationGate) {}
  snapshot = (): AuditState => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { this.mounted = true }
  dispose() { this.mounted = false; this.revision++; this.query = null; this.state = { key: '', busy: false, company: false } }
  private publish(state: AuditState) { this.state = state; for (const listener of this.listeners) listener() }
  private current(revision: number) { return this.mounted && this.revision === revision }
  setQuery(query: AuditQuery) {
    if (!this.mounted) return
    const captured = { ...query }, revision = ++this.revision, key = JSON.stringify(captured)
    this.query = captured
    const valid = !rangeProblem(captured.from, captured.to) && !filterProblem(captured)
    this.publish({ key, busy: valid, company: false })
    if (valid) void this.read(captured, revision, key)
  }
  private async read(query: AuditQuery, revision: number, key: string) {
    try {
      const data = await this.api.log(query)
      if (!validAuditLog(data) || data.period.from !== query.from || data.period.to !== query.to) throw new AuditResponseError()
      if (this.current(revision)) this.publish({ key, data, busy: false, company: false })
    } catch (error) { if (this.current(revision)) this.publish({ key, error, busy: false, company: companySignInNeeded(error) }) }
  }
  async exportAs(format: 'csv' | 'json') {
    if (!this.mounted || !this.query || this.state.busy || !this.state.data || this.state.error) return
    const revision = this.revision, query = { ...this.query }, previous = this.state
    this.publish({ ...previous, busy: true, company: false })
    await this.gate.run(async () => {
      if (!this.current(revision)) return
      try {
        const file = await this.api.exportLog(query, format)
        if (!file || typeof file.name !== 'string' || typeof file.file !== 'string' || typeof file.type !== 'string') throw new AuditResponseError()
        if (this.current(revision)) { this.saved(file); this.publish({ ...previous, busy: false, company: false }) }
      } catch (error) { if (this.current(revision)) this.publish({ key: previous.key, error, busy: false, company: companySignInNeeded(error) }) }
    })
  }
}
