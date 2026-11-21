// Browser-only test fixture, served by Vite for the bounded authority regression harness.
/* global window, document */
import { createElement as h, act, Profiler } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { MeContext } from '../src/lib/me'
import { ToastProvider } from '../src/components/Toast'
import { ReportsPage } from '../src/ee/pages/ReportsPage'
import { reportsApi } from '../src/ee/api'
import { api, setTransport } from '../src/api/client'
import { mockTransport } from '../src/mock'

export async function checkAuthority() {
    setTransport(mockTransport)
    window.IS_REACT_ACT_ENVIRONMENT = true
    const original = await api.me()
    const sample = await reportsApi.report({ from: '2026-10-01', to: '2026-10-12' })
    const privateReport = structuredClone(sample)
    privateReport.by_person = [{ key: 'Private identity', label: 'Private identity', exchanges: 1, masked: 1, stopped: 0 }]
    privateReport.options.projects = ['Private project']
    const scopedReport = structuredClone(sample)
    scopedReport.by_person = null
    scopedReport.options.projects = ['Scoped project']
    const reads = [], exports = [], problems = [], commits = []
    reportsApi.report = q => new Promise(resolve => reads.push({ q, resolve }))
    reportsApi.exportReport = (q, format) => new Promise(resolve => exports.push({ q, format, resolve }))
    const host = document.createElement('div')
    document.body.append(host)
    const mounted = createRoot(host)
    let label = 'initial', currentMe = original, unreachable = false
    const check = (ok, message) => { if (!ok) problems.push(message) }
    const render = async () => {
      await act(async () => mounted.render(h(MeContext.Provider, { value: {
        me: currentMe, unreachable, reload: () => undefined, customTypes: new Set(), reloadConfig: () => undefined,
      } }, h(ToastProvider, null, h(MemoryRouter, { initialEntries: ['/reports'] },
        h(Profiler, { id: 'reports', onRender: () => commits.push({ label, html: host.innerHTML }) }, h(ReportsPage)))))))
    }
    const settle = async (index, report = privateReport) => { await act(async () => reads[index].resolve(report)) }
    const privateIn = html => html.includes('Private identity') || html.includes('Private project') || html.includes('Export CSV')
    await render()
    await settle(0)
    check(privateIn(host.innerHTML), 'fixture: privileged report must first be visible')
    // The page remains mounted and permitted during the role/identity transitions. Additional cases directly
    // exercise the hook even where the outer App guard would also unmount an entirely forbidden page.
    const transitions = [
      ['security to group lead', me => { me.edition.roles = ['group-lead', 'user'] }],
      ['person identity', me => { me.server.identity.id = 'u_anotheraaa' }],
      ['identity display name', me => { me.server.identity.name = 'Another person' }],
      ['key space', me => { me.server.identity.kind = 'key' }],
      ['server address', me => { me.server.url = 'https://other.example.test' }],
      ['session sign-in method', me => { me.session.via = 'code' }],
      ['allowed actions', me => { me.edition.actions = [] }],
      ['allowed pages', me => { me.edition.pages = ['reports'] }],
      ['licensed features', me => { me.edition.features = [] }],
      ['license state', me => { me.edition.license = { ...me.edition.license, state: 'paused' } }],
      ['identical refreshed payload', () => undefined],
    ]
    for (const [name, change] of transitions) {
      label = name
      currentMe = structuredClone(original)
      change(currentMe)
      const before = reads.length, firstCommit = commits.length
      await render()
      check(!privateIn(commits[firstCommit].html), `${name}: first committed render leaked prior data or export`)
      check(reads.length === before + 1, `${name}: fresh scoped read was not submitted`)
      if (reads.length > before) await settle(before)
    }
    label = 'unreachable'
    unreachable = true
    const firstCommit = commits.length
    await render()
    check(!privateIn(commits[firstCommit].html), 'unreachable: cached privileged data remained visible')
    // Settle any admitted request before returning to a known identity.
    if (reads.length && !host.innerHTML.includes('Export CSV')) await settle(reads.length - 1)
    unreachable = false; currentMe = structuredClone(original); label = 'restore'
    const restore = reads.length
    await render()
    if (reads.length > restore) await settle(restore)
    const csv = [...host.querySelectorAll('button')].find(node => node.textContent.includes('Export CSV'))
    await act(async () => csv.click())
    check(exports.length === 1, 'fixture: export must be pending')
    const beforeExport = reads.length
    currentMe = structuredClone(original); currentMe.edition.roles = ['group-lead', 'user']; label = 'export narrowing'
    await render()
    currentMe = structuredClone(currentMe); currentMe.server.identity.id = 'u_anotheraaa'; label = 'export identity'
    await render()
    check(reads.length === beforeExport, 'authority change released the gate before export settlement')
    await act(async () => exports[0].resolve({ name: 'obsolete-authority.csv', type: 'text/csv', file: 'synthetic private export' }))
    check(!host.textContent.includes('Report saved'), 'old authority export produced a toast')
    check(reads.length === beforeExport + 1, 'queued authority reads did not coalesce after export settlement')
    check(!privateIn(host.innerHTML), 'old export completion restored privileged report data')
    if (reads.length > beforeExport) await settle(beforeExport, scopedReport)
    check(host.innerHTML.includes('Scoped project'), 'new scoped report was not rendered')
    // An in-flight read also remains in the shared gate; its old result must never become visible.
    currentMe = structuredClone(currentMe); label = 'held read'
    const beforeRead = reads.length
    await render()
    if (reads.length > beforeRead) {
      currentMe = structuredClone(original); currentMe.edition.roles = ['group-lead', 'user']; label = 'read narrowing'
      await render()
      check(reads.length === beforeRead + 1, 'authority change released the gate before read settlement')
      await settle(beforeRead)
      check(!privateIn(host.innerHTML), 'stale read restored private rows/options/export')
      check(reads.length === beforeRead + 2, 'new scope did not read after stale read settlement')
      if (reads.length > beforeRead + 1) await settle(beforeRead + 1, scopedReport)
    } else problems.push('refresh failed to start held read')
    await act(async () => mounted.unmount())
    host.remove()
    return { problems, transitions: transitions.length, commits: commits.length }
}
