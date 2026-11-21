// Feature-local coordination: an aborted client fetch does not release the server's person slot.
import { companySignInNeeded } from '../../lib/refused'
import type { Report, ReportFile, ReportQuery } from '../types'
import { rangeProblem } from './reports'

export interface ReportTransport {
  report(query: ReportQuery): Promise<Report>
  exportReport(query: ReportQuery, format: 'csv' | 'json'): Promise<ReportFile>
}
export interface ReportState {
  key: string
  data?: Report
  error?: unknown
  busy: boolean
  company: boolean
}

/** Shared across page mounts: only settlement releases the next submitted operation. */
export class ReportGate {
  private tail: Promise<unknown> = Promise.resolve()
  run(action: () => Promise<void>): Promise<void> {
    const next = this.tail.then(action)
    this.tail = next.catch(() => undefined)
    return next
  }
}
export const reportOperationGate = new ReportGate()

export class ReportSession {
  private state: ReportState = { key: '', busy: false, company: false }
  private listeners = new Set<() => void>()
  private revision = 0
  private mounted = true
  private query: ReportQuery | null = null
  constructor(private api: ReportTransport, private saved: (file: ReportFile) => void, private gate = reportOperationGate) {}

  snapshot = (): ReportState => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { this.mounted = true }
  dispose() { this.mounted = false; this.revision++; this.query = null; this.state = { key: '', busy: false, company: false } }
  private publish(state: ReportState) {
    this.state = state
    for (const listener of this.listeners) listener()
  }
  private current(revision: number) { return this.mounted && this.revision === revision }

  setQuery(query: ReportQuery) {
    const revision = ++this.revision
    const key = JSON.stringify(query)
    this.query = { ...query }
    const valid = !rangeProblem(query.from, query.to)
    this.publish({ key, busy: valid, company: false })
    if (!valid || !this.mounted) return
    void this.gate.run(async () => {
      // Pending queries coalesce while the preceding submitted request settles.
      if (!this.current(revision)) return
      try {
        const data = await this.api.report(query)
        if (this.current(revision)) this.publish({ key, data, busy: false, company: false })
      } catch (error) {
        if (this.current(revision)) this.publish({ key, error, busy: false, company: companySignInNeeded(error) })
      }
    })
  }

  async exportAs(format: 'csv' | 'json') {
    if (!this.mounted || !this.query || this.state.busy || !this.state.data || this.state.error) return
    const revision = this.revision
    const query = this.query
    const previous = this.state
    this.publish({ ...previous, busy: true, company: false })
    await this.gate.run(async () => {
      if (!this.current(revision)) return
      try {
        const file = await this.api.exportReport(query, format)
        if (this.current(revision)) {
          this.saved(file)
          this.publish({ ...previous, busy: false, company: false })
        }
      } catch (error) {
        if (this.current(revision)) this.publish({ key: previous.key, error, busy: false, company: companySignInNeeded(error) })
      }
    })
  }
}
