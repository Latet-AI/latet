import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MeContext, type MeState } from '../../lib/me'
import { EnterpriseReadOnly } from '../components/TrialEnded'
import type { SiemOutputView, SiemView } from '../types'

const state = { data: undefined as SiemView | undefined, busy: false, company: false, changed: false, error: undefined as string | undefined, draft: null, removing: null }
vi.mock('../lib/useSiem', () => ({ useSiem: () => ({ ...state, editable: true, reload() {}, edit() {}, change() {}, save() {}, cancel() {}, remove() {}, confirmRemove() {}, test() {} }) }))
const { SiemPage } = await import('./SiemPage')
const output: SiemOutputView = { name: 'collector', kind: 'webhook', url: 'https://collector.example/private', headers: {}, secret: null, format: 'json', has_secret: false, secret_destination_changed: false, queued: 7, dropped: 0, collapsed: 0, last_ok: null, last_error: null, failing_since: null, blocked: false, held: false }
function page(pages = ['siem', 'reports'], readOnly = false) {
  const context = { me: { server: {}, edition: { pages, actions: ['siem.edit'], roles: ['security'] } }, unreachable: false, customTypes: new Set(), reload() {}, reloadConfig() {} } as unknown as MeState
  return renderToStaticMarkup(h(MeContext.Provider, { value: context }, h(MemoryRouter, null, h(EnterpriseReadOnly.Provider, { value: readOnly }, h(SiemPage)))))
}
beforeEach(() => Object.assign(state, { data: { outputs: [{ ...output }], editable: true, etag: 'one', paused: null }, busy: false, error: undefined, company: false, changed: false }))
describe('SIEM presentation', () => {
  it.each(['kind', 'format', 'paused.why'])('uses the fixed read fallback for array %s without exposing destination controls', field => {
    const malformed = field === 'paused.why'
      ? { ...state.data!, paused: { why: ['expired'], expires: null } }
      : { ...state.data!, outputs: [{ ...output, [field]: [field === 'kind' ? 'webhook' : 'json'] }] }
    state.data = malformed as unknown as SiemView
    const html = page()
    expect(html).toContain('The current outputs could not be read. Reload before editing or testing.')
    expect(html).toContain('role="alert"')
    expect(html).not.toContain('https://collector.example/private'); expect(html).not.toContain('section aria-label="collector"')
    expect(html).not.toContain('Add an output'); expect(html).not.toContain('Send a test event'); expect(html).not.toContain('>Edit<')
  })
  it('gates Reports links by current pages and uses neutral empty wording', () => {
    expect(page()).toContain('href="/reports"')
    state.data!.outputs = []
    const html = page(['siem'])
    expect(html).not.toContain('href="/reports"'); expect(html).not.toContain('counted for the reports')
  })
  it('hides stale destination data and controls on read failures', () => {
    state.error = 'read'
    const html = page()
    expect(html).not.toContain('https://collector.example/private'); expect(html).not.toContain('Add an output'); expect(html).not.toContain('Send a test event')
  })
  it('hides all mutations for trial-ended and auditor views', () => {
    for (const html of [page(['siem'], true), (() => { state.data!.editable = false; state.data!.etag = null; return page() })()]) {
      expect(html).not.toContain('Add an output'); expect(html).not.toContain('Send a test event'); expect(html).not.toContain('>Edit<')
    }
  })
  it('distinguishes held events from held administration entries, with the CLI remedy', () => {
    state.data!.outputs[0] = { ...output, held: true, blocked: false }
    const html = page()
    expect(html).toContain('latet server siem skip'); expect(html).not.toContain('at an entry of the administration log')
    expect(html).not.toContain('1970')
    state.data!.outputs[0] = { ...output, held: true, blocked: true }
    expect(page()).toContain('administration log')
  })
  it('keeps local output removal while explaining CLI-only edits and tests', () => {
    state.data!.outputs[0] = { ...output, url: 'http://localhost:99/in' }
    const html = page()
    expect(html).toContain('latet server siem set'); expect(html).toContain('latet server siem test')
    expect(html).not.toContain('Send a test event'); expect(html).not.toContain('>Edit<'); expect(html).toContain('>Remove<')
  })
  it('renders server text safely and malformed collections with a fixed fallback', () => {
    state.data!.outputs[0] = { ...output, held: true, last_error: '<img src=x onerror=alert(1)>' }
    expect(page()).not.toContain('<img src=x')
    state.data!.outputs = {} as SiemOutputView[]
    expect(() => page()).not.toThrow(); expect(page()).not.toContain('Add an output')
  })
})
