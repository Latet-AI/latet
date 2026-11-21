// Actual RouterProvider transition: settle A after B's layout commit, before its passive query effect.
/* global window, document, HTMLAnchorElement */
import { createElement as h, act, useLayoutEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router'
import { MeContext } from '../src/lib/me'
import { ToastProvider } from '../src/components/Toast'
import { EnterpriseReadOnly } from '../src/ee/components/TrialEnded'
import { AuditPage } from '../src/ee/pages/AuditPage'
import { AuditSession } from '../src/ee/lib/auditSession'
import { auditApi } from '../src/ee/api'
import { api, setTransport } from '../src/api/client'
import { mockTransport } from '../src/mock'

export async function checkAuditQueryCommit() {
  setTransport(mockTransport); window.IS_REACT_ACT_ENVIRONMENT = true
  const original = await api.me(), queryA = { from: '2026-10-01', to: '2026-10-12' }
  const queryB = { ...queryA, action: 'person.*' }, sampleA = await auditApi.log(queryA), sampleB = await auditApi.log(queryB)
  const reads = [], exports = [], problems = [], downloads = []
  const originalLog = auditApi.log, originalExport = auditApi.exportLog, originalSetQuery = AuditSession.prototype.setQuery
  const oldClick = HTMLAnchorElement.prototype.click
  let oldSession, boundary, pending, observeBoundary, observeReadB
  const observed = new Promise(resolve => { observeBoundary = resolve })
  const readBStarted = new Promise(resolve => { observeReadB = resolve })
  auditApi.log = q => new Promise((resolve, reject) => {
    reads.push({ q, resolve, reject }); if (q.action === 'person.*') observeReadB()
  })
  auditApi.exportLog = (q, format) => new Promise((resolve, reject) => exports.push({ q, format, resolve, reject }))
  AuditSession.prototype.setQuery = function (query) { oldSession ??= this; return originalSetQuery.call(this, query) }
  HTMLAnchorElement.prototype.click = function () { if (this.download) downloads.push(this.download); else oldClick.call(this) }
  const host = document.createElement('div'); document.body.append(host); const mounted = createRoot(host)
  const check = (ok, why) => { if (!ok) problems.push(why) }
  const button = text => [...host.querySelectorAll('button')].find(node => node.textContent.trim() === text)
  function Boundary() {
    const location = useLocation()
    useLayoutEffect(() => {
      if (!location.search.includes('action=person.*') || boundary) return
      boundary = {
        bCommitted: [...host.querySelectorAll('select')].some(node => node.value === 'person.*'),
        bReadsAtCommit: reads.filter(read => read.q.action === 'person.*').length,
        oldSessionStillA: oldSession.snapshot().key === JSON.stringify(queryA),
      }
      // Native promise continuation runs at this commit's microtask checkpoint, outside act.
      pending.resolve({ name: 'obsolete-query-a.csv', type: 'text/csv', file: 'synthetic administration metadata' })
      queueMicrotask(() => queueMicrotask(observeBoundary))
    }, [location])
    return h(AuditPage)
  }
  const routeA = '/audit?period=custom&from=2026-10-01&to=2026-10-12'
  const router = createMemoryRouter([{ path: '/audit', element: h(Boundary) }], { initialEntries: [routeA] })
  try {
    await act(async () => mounted.render(h(MeContext.Provider, { value: { me: original, unreachable: false, reload() {}, customTypes: new Set(), reloadConfig() {} } },
      h(ToastProvider, null, h(EnterpriseReadOnly.Provider, { value: false }, h(RouterProvider, { router }))))))
    await act(async () => reads[0].resolve(structuredClone(sampleA)))
    await act(async () => { button('Export CSV').click(); button('Export JSON')?.click() })
    check(exports.length === 1, 'same-tick export exclusion failed')
    pending = exports[0]
    check(JSON.stringify(pending.q) === JSON.stringify(queryA), 'export did not capture query A')
    // Do not use act here: it would flush passive effects and erase the causal scheduling boundary.
    window.IS_REACT_ACT_ENVIRONMENT = false
    void router.navigate(`${routeA}&action=person.*`)
    await observed
    check(boundary.bCommitted && boundary.bReadsAtCommit === 0, 'did not observe B committed before its passive read')
    check(downloads.length === 0, 'query A downloaded after query B committed')
    // Only now allow B's passive read and queued toast update, then exercise normal recovery.
    await readBStarted
    window.IS_REACT_ACT_ENVIRONMENT = true
    const next = reads.find(read => read.q.action === 'person.*')
    check(!!next, 'query B read did not start')
    if (next) await act(async () => next.resolve(structuredClone(sampleB)))
    check(!host.querySelector('[data-toasts]')?.textContent.includes('obsolete-query-a.csv'), 'query A saved toast after query B committed')
    check(host.querySelectorAll('tbody tr').length === sampleB.entries.length && !!button('Export CSV'), 'query B did not recover after current read')
    await act(async () => button('Export CSV')?.click())
    check(exports.length === 2 && exports[1].q.action === 'person.*', 'query B export did not recover after A settled')
    if (exports[1]) await act(async () => exports[1].resolve({ name: 'current-query-b.csv', type: 'text/csv', file: 'synthetic current metadata' }))
    check(downloads.filter(name => name === 'current-query-b.csv').length === 1, 'current query B export was not saved exactly once')
    return { boundary, problems, downloads, reads: reads.length, exports: exports.length }
  } finally {
    window.IS_REACT_ACT_ENVIRONMENT = true
    await act(async () => mounted.unmount()); router.dispose(); host.remove()
    auditApi.log = originalLog; auditApi.exportLog = originalExport; AuditSession.prototype.setQuery = originalSetQuery; HTMLAnchorElement.prototype.click = oldClick
  }
}
