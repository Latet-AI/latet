// Actual Reports router transition: settle A after B commits, before passive setQuery(B).
/* global window, document, HTMLAnchorElement */
import { createElement as h, act, useLayoutEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router'
import { MeContext } from '../src/lib/me'
import { ToastProvider } from '../src/components/Toast'
import { EnterpriseReadOnly } from '../src/ee/components/TrialEnded'
import { ReportsPage } from '../src/ee/pages/ReportsPage'
import { ReportSession } from '../src/ee/lib/reportSession'
import { reportsApi } from '../src/ee/api'
import { api, setTransport } from '../src/api/client'
import { mockTransport } from '../src/mock'

export async function checkReportsQueryCommit() {
  setTransport(mockTransport)
  const originalActEnvironment = window.IS_REACT_ACT_ENVIRONMENT
  window.IS_REACT_ACT_ENVIRONMENT = true
  const me = await api.me(), queryA = { from: '2026-10-01', to: '2026-10-12' }, queryB = { ...queryA, to: '2026-10-11' }
  const sampleA = await reportsApi.report(queryA), sampleB = await reportsApi.report(queryB)
  sampleB.options.projects = ['current-query-b']
  const reads = [], exports = [], setQueries = [], problems = [], downloads = [], events = []
  const originalReport = reportsApi.report, originalExport = reportsApi.exportReport, originalSetQuery = ReportSession.prototype.setQuery
  const originalClick = HTMLAnchorElement.prototype.click
  const isB = query => query.from === queryB.from && query.to === queryB.to
  const bSetQueryCount = () => setQueries.filter(isB).length
  let oldSession, boundary, pending, aResolved = false, observeBoundary, observeReadB
  const observed = new Promise(resolve => { observeBoundary = resolve })
  const readBStarted = new Promise(resolve => { observeReadB = resolve })
  reportsApi.report = q => new Promise((resolve, reject) => {
    reads.push({ q, resolve, reject })
    events.push({ event: 'read', q, aResolved })
    if (isB(q)) observeReadB()
  })
  reportsApi.exportReport = (q, format) => new Promise((resolve, reject) => exports.push({ q, format, resolve, reject }))
  ReportSession.prototype.setQuery = function (query) {
    oldSession ??= this
    setQueries.push({ ...query })
    events.push({ event: 'setQuery', q: { ...query }, aResolved })
    return originalSetQuery.call(this, query)
  }
  HTMLAnchorElement.prototype.click = function () {
    if (this.download) { downloads.push(this.download); events.push({ event: 'download', name: this.download, bSetQueryCalls: bSetQueryCount() }) }
    else originalClick.call(this)
  }
  const host = document.createElement('div'); document.body.append(host); const mounted = createRoot(host)
  const check = (ok, why) => { if (!ok) problems.push(why) }
  const button = text => [...host.querySelectorAll('button')].find(node => node.textContent.trim() === text)
  const until = async (promise, stage) => {
    let timer
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`INCONCLUSIVE Reports query fixture: ${stage} was not observed`)), 10000)
      })])
    } finally { clearTimeout(timer) }
  }
  function Boundary() {
    const location = useLocation()
    useLayoutEffect(() => {
      if (new URLSearchParams(location.search).get('to') !== queryB.to || boundary) return
      boundary = {
        committedTo: host.querySelectorAll('input[type="date"]')[1]?.value,
        bSetQueryCallsAtCommit: bSetQueryCount(),
        bReadsAtCommit: reads.filter(read => isB(read.q)).length,
        oldSessionKeyAtCommit: oldSession.snapshot().key,
      }
      events.push({ event: 'layoutB', ...boundary })
      aResolved = true
      events.push({ event: 'resolveExportA' })
      pending.resolve({ name: 'obsolete-query-a.csv', type: 'text/csv', file: 'synthetic obsolete report counts' })
      // Resolving the native promise queues A's await continuation first. Observe immediately after it,
      // before any React work queued by A's publish; a second microtask would skip that boundary.
      queueMicrotask(() => {
        boundary.bSetQueryCallsAtObservation = bSetQueryCount()
        boundary.downloadsAtObservation = [...downloads]
        events.push({ event: 'afterExportContinuation', bSetQueryCalls: bSetQueryCount(), downloads: [...downloads] })
        observeBoundary()
      })
    }, [location])
    return h(ReportsPage)
  }
  const routeA = '/reports?period=custom&from=2026-10-01&to=2026-10-12'
  const routeB = '/reports?period=custom&from=2026-10-01&to=2026-10-11'
  const router = createMemoryRouter([{ path: '/reports', element: h(Boundary) }], { initialEntries: [routeA] })
  try {
    await act(async () => mounted.render(h(MeContext.Provider, { value: { me, unreachable: false, reload() {}, customTypes: new Set(), reloadConfig() {} } },
      h(ToastProvider, null, h(EnterpriseReadOnly.Provider, { value: false }, h(RouterProvider, { router }))))))
    if (!reads[0]) throw new Error('INCONCLUSIVE Reports query fixture: initial A read missing')
    await act(async () => reads[0].resolve(structuredClone(sampleA)))
    const csv = button('Export CSV'), json = button('Export JSON')
    if (!csv || !json) throw new Error('INCONCLUSIVE Reports query fixture: initial export controls missing')
    await act(async () => { csv.click(); json.click() })
    check(exports.length === 1, 'same-tick export exclusion failed')
    pending = exports[0]
    if (!pending) throw new Error('INCONCLUSIVE Reports query fixture: held A export missing')
    check(JSON.stringify(pending.q) === JSON.stringify(queryA) && pending.format === 'csv', 'export did not capture exact query A and CSV')
    check(reads.length === 1, 'another report read began while A export was held')
    // This is a real concurrent router commit, intentionally outside act, as in the accepted Audit fixture.
    window.IS_REACT_ACT_ENVIRONMENT = false
    void router.navigate(routeB)
    await until(observed, 'B layout/microtask boundary')
    const boundaryValid = boundary.committedTo === queryB.to && boundary.bSetQueryCallsAtCommit === 0
      && boundary.bSetQueryCallsAtObservation === 0 && boundary.bReadsAtCommit === 0
    check(downloads.length === 0, 'obsolete query A downloaded after B committed')
    await until(readBStarted, 'B read after A settlement')
    window.IS_REACT_ACT_ENVIRONMENT = true
    const next = reads.find(read => isB(read.q))
    await act(async () => next.resolve(structuredClone(sampleB)))
    check(!host.querySelector('[data-toasts]')?.textContent.includes('obsolete-query-a.csv'), 'obsolete query A saved toast after B committed')
    check(bSetQueryCount() === 1 && reads.filter(read => isB(read.q)).length === 1, 'B did not receive exactly one setQuery and one read')
    check(events.filter(event => event.event === 'read' && isB(event.q)).every(event => event.aResolved), 'shared gate released B before A export settled')
    check(host.querySelectorAll('input[type="date"]')[1]?.value === queryB.to
      && [...host.querySelectorAll('select option')].some(node => node.value === 'current-query-b') && !!button('Export CSV'), 'current B report and export controls did not recover')
    await act(async () => button('Export CSV')?.click())
    check(exports.length === 2 && JSON.stringify(exports[1].q) === JSON.stringify(queryB), 'current B export did not capture exact query B')
    if (exports[1]) await act(async () => exports[1].resolve({ name: 'current-query-b.csv', type: 'text/csv', file: 'synthetic current report counts' }))
    check(downloads.filter(name => name === 'current-query-b.csv').length === 1, 'current B export was not saved exactly once')
    check(host.querySelector('[data-toasts]')?.textContent.includes('current-query-b.csv'), 'current B export did not confirm its save')
    return { boundaryValid, boundary, problems, downloads, setQueries, reads: reads.length, exports: exports.length, events }
  } finally {
    window.IS_REACT_ACT_ENVIRONMENT = true
    await act(async () => mounted.unmount()); router.dispose(); host.remove()
    reportsApi.report = originalReport; reportsApi.exportReport = originalExport
    ReportSession.prototype.setQuery = originalSetQuery; HTMLAnchorElement.prototype.click = originalClick
    window.IS_REACT_ACT_ENVIRONMENT = originalActEnvironment
  }
}
