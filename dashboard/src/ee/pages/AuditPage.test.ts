import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import { EnterpriseReadOnly } from '../components/TrialEnded'
import type { AuditLogView } from '../types'
const state = { data: undefined as AuditLogView | undefined, busy: false, error: undefined as unknown, company: false, addresses: true }
vi.mock('../lib/useAudit', () => ({ useAudit: () => ({ ...state, canExport: true, reload() {}, exportAs() {} }) }))
const { AuditPage } = await import('./AuditPage')
const view = (): AuditLogView => ({ period: { from: '2026-10-01', to: '2026-10-12' }, entries: [{ id: 1, ts: null, actor: { id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' }, action: 'siem.skipped', target: null, details: { from: null, entries: 0, dropped: 4 }, address: '10.0.0.8' }], actions: ['siem.skipped'], actors: [{ id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' }], addresses: true, truncated: false })
const page = (suffix = '', readOnly = false) => renderToStaticMarkup(h(MemoryRouter, { initialEntries: [`/audit?period=custom&from=2026-10-01&to=2026-10-12${suffix}`] }, h(EnterpriseReadOnly.Provider, { value: readOnly }, h(AuditPage))))
beforeEach(() => Object.assign(state, { data: view(), busy: false, error: undefined, company: false, addresses: true }))
describe('Audit presentation', () => {
  it('retains null-time rows and null/zero metadata without claiming verification', () => {
    const html = page()
    expect(html).toContain('Emily Carter'); expect(html).toContain('Time unavailable'); expect(html).toContain('entries: 0'); expect(html).toContain('from: null')
    expect(html).not.toContain('1970'); expect(html).not.toContain('verified')
    expect(html).toContain('server instance'); expect(html).toContain('browser')
  })
  it('clamps addresses by current administrator authority even if the response still carries them', () => {
    expect(page()).toContain('10.0.0.8'); state.addresses = false
    expect(page()).not.toContain('10.0.0.8'); expect(page()).not.toContain('>From</th>')
  })
  it('clears prior rows and choices after errors, busy queries, invalid dates or malformed data', () => {
    for (const change of [{ error: new ApiError(403, 'private-response-canary') }, { busy: true }, { data: { ...view(), entries: null } as never }]) {
      Object.assign(state, { data: view(), busy: false, error: undefined }, change)
      const html = page(); expect(html).not.toContain('Emily Carter'); expect(html).not.toContain('10.0.0.8'); expect(html).not.toContain('Export CSV'); expect(html).not.toContain('private-response-canary')
    }
  })
  it('keeps missing active actor and prefix filters visible and individually clearable', () => {
    const html = page('&actor=u_gone000000&action=person.*')
    expect(html).toContain('value="u_gone000000" selected'); expect(html).toContain('value="person.*" selected')
    expect((html.match(/value=""/g) ?? []).length).toBeGreaterThanOrEqual(2)
  })
  it('hides exports in read-only mode and describes the latest matching limit', () => {
    expect(page('', true)).not.toContain('Export CSV'); state.data!.truncated = true
    expect(page()).toContain('newest 5,000 matching entries'); expect(page()).not.toContain('first 5,000')
  })
  it('escapes names, targets, details and actions as inert text', () => {
    const text = '<img src=x onerror=alert(1)>'
    const row = state.data!.entries[0]!
    row.actor.name = text; row.target = { id: 'target', name: text }; row.action = text; row.details = { label: text }
    const html = page(); expect(html).not.toContain('<img'); expect(html).toContain('&lt;img'); expect(html).not.toContain('href="http')
  })
})
