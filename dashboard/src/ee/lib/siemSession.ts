import { ApiError } from '../../api/client'
import { companySignInNeeded } from '../../lib/refused'
import type { SiemOutputSettings, SiemOutputView, SiemTest, SiemView } from '../types'
import { ReportGate } from './reportSession'
import { draftErrors, draftOf, localOutput, newDraft, secretChange, settingsOf, siemChanged, validSiemView, withOutput, withoutOutput, type Draft } from './siem'

export interface SiemTransport {
  siem(): Promise<SiemView>
  saveSiem(outputs: SiemOutputSettings[], secrets: Record<string, string | null>, etag: string): Promise<SiemView>
  testSiem(name: string): Promise<SiemTest>
}
export type SiemFailure = 'read' | 'failed' | 'forbidden' | 'byCommand' | 'large' | 'rate'
export type SiemNotice = 'saved' | 'testOk' | 'testFail'
export interface SiemState {
  data?: SiemView
  draft: Draft | null
  removing: SiemOutputView | null
  busy: boolean
  company: boolean
  changed: boolean
  error?: SiemFailure
}
const empty = (): SiemState => ({ draft: null, removing: null, busy: false, company: false, changed: false })
// SIEM operations share a gate across mounts; client disposal never releases a submitted server operation.
const sharedGate = new ReportGate()
export class SiemSession {
  private state = empty()
  private listeners = new Set<() => void>()
  private mounted = true
  private revision = 0
  private intentEtag: string | null = null
  constructor(private api: SiemTransport, private editing: boolean, private notice: (kind: SiemNotice) => void, private gate = sharedGate) {}
  snapshot = (): SiemState => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  activate() { this.mounted = true }
  dispose() { this.mounted = false; this.revision++; this.intentEtag = null; this.state = empty() }
  private publish(state: SiemState) { this.state = state; for (const listener of this.listeners) listener() }
  private current(revision: number) { return this.mounted && this.revision === revision }
  editable = () => this.mounted && this.editing && !this.state.busy && !this.state.error && !!this.state.data?.editable && !this.state.data.paused && !!this.state.data.etag
  private async read(revision: number, changed = false) {
    try {
      const data = await this.api.siem()
      if (!this.current(revision)) return false
      if (!validSiemView(data)) { this.publish({ ...empty(), changed, error: 'read' }); return false }
      this.publish({ ...empty(), data, changed })
      return true
    } catch (error) {
      if (this.current(revision)) this.publish({ ...empty(), changed, error: 'read', company: companySignInNeeded(error) })
      return false
    }
  }
  reload = () => {
    if (!this.mounted || this.state.busy) return
    const revision = ++this.revision
    this.intentEtag = null
    this.publish({ ...empty(), busy: true })
    void this.gate.run(async () => { if (this.current(revision)) await this.read(revision) })
  }
  edit = (name?: string) => {
    if (!this.editable()) return
    const data = this.state.data!
    const output = name === undefined ? undefined : data.outputs.find(o => o.name === name)
    if (name !== undefined && (!output || localOutput(output)) || name === undefined && data.outputs.length >= 50) return
    this.intentEtag = data.etag
    this.publish({ ...this.state, draft: output ? draftOf(output) : newDraft(), removing: null, changed: false, company: false })
  }
  change = (draft: Draft) => {
    const was = this.state.draft
    if (!this.editable() || !was) return
    this.publish({ ...this.state, draft: { ...draft, original: was.original, name: was.original ?? draft.name,
      secretName: was.secretName, hasSecret: was.hasSecret, binding: was.binding } })
  }
  cancel = () => {
    if (!this.mounted || this.state.busy) return
    this.intentEtag = null
    this.publish({ ...this.state, draft: null, removing: null })
  }
  remove = (name: string) => {
    if (!this.editable()) return
    const removing = this.state.data!.outputs.find(o => o.name === name)
    if (!removing) return
    this.intentEtag = this.state.data!.etag
    this.publish({ ...this.state, removing, draft: null, changed: false, company: false })
  }
  save = () => {
    const { data, draft } = this.state
    if (!this.editable() || !data || !draft || this.intentEtag === null || this.intentEtag !== data.etag
      || draftErrors(draft, data.outputs.filter(o => o.name !== draft.original)).length) return
    const change = secretChange(draft)
    const outputs = withOutput(data.outputs, draft.original, settingsOf(draft))
    const secrets = change === undefined ? {} : { [draft.name]: change }
    const etag = this.intentEtag
    this.perform(async () => { await this.api.saveSiem(outputs, secrets, etag); return 'saved' })
  }
  confirmRemove = () => {
    const { data, removing } = this.state
    if (!this.editable() || !data || !removing || this.intentEtag === null || this.intentEtag !== data.etag) return
    const outputs = withoutOutput(data.outputs, removing.name), etag = this.intentEtag
    this.perform(async () => { await this.api.saveSiem(outputs, {}, etag); return 'saved' })
  }
  test = (name: string) => {
    if (!this.editable() || this.state.draft || this.state.removing) return
    const output = this.state.data!.outputs.find(o => o.name === name)
    if (!output || localOutput(output)) return
    this.perform(async () => {
      const result = await this.api.testSiem(name)
      if (!result || typeof result.ok !== 'boolean') throw new Error('invalid SIEM test response')
      return result.ok ? 'testOk' : 'testFail'
    })
  }
  private perform(action: () => Promise<SiemNotice>) {
    // The synchronous state change is also the exclusion guard for same-tick events.
    const revision = ++this.revision
    this.intentEtag = null
    this.publish({ ...empty(), busy: true })
    void this.gate.run(async () => {
      if (!this.current(revision)) return
      try {
        const notice = await action()
        if (this.current(revision) && await this.read(revision) && this.current(revision)) this.notice(notice)
      } catch (error) {
        if (!this.current(revision)) return
        if (siemChanged(error)) {
          this.publish({ ...empty(), busy: true, changed: true })
          await this.read(revision, true)
        } else {
          const company = companySignInNeeded(error)
          const status = error instanceof ApiError ? error.status : 0
          // An output at http:// or on the server, or its secret, is the command's (403 `by-command`): said in the page's words.
          const byCommand = status === 403 && error instanceof ApiError && (error.body as { code?: unknown } | undefined)?.code === 'by-command'
          this.publish({ ...empty(), company, error: company ? undefined : byCommand ? 'byCommand' : status === 403 ? 'forbidden' : status === 413 ? 'large' : status === 429 ? 'rate' : 'failed' })
        }
      }
    })
  }
}
