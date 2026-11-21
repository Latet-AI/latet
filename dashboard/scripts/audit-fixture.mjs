// Real page and hook under changing /me authority, with synthetic controlled transport boundaries.
/* global window, document, HTMLAnchorElement */
import { createElement as h, act, Profiler } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { MeContext } from '../src/lib/me'
import { ToastProvider } from '../src/components/Toast'
import { EnterpriseReadOnly } from '../src/ee/components/TrialEnded'
import { AuditPage } from '../src/ee/pages/AuditPage'
import { auditApi } from '../src/ee/api'
import { api, ApiError, setTransport } from '../src/api/client'
import { mockTransport } from '../src/mock'

export async function checkAudit() {
  setTransport(mockTransport); window.IS_REACT_ACT_ENVIRONMENT = true
  const original = await api.me(), sample = await auditApi.log({ from: '2026-10-01', to: '2026-10-12' })
  sample.entries[0].ts = null; sample.entries[1].ts = 'invalid'
  sample.entries[0].details = { from: null, count: 0, dropped: 4, nested: { ok: false }, label: '<img src=x onerror=alert(1)>' }
  const reads = [], exports = [], problems = [], commits = []
  const originalLog = auditApi.log, originalExport = auditApi.exportLog
  auditApi.log = q => new Promise((resolve, reject) => reads.push({ q, resolve, reject }))
  auditApi.exportLog = (q, format) => new Promise((resolve, reject) => exports.push({ q, format, resolve, reject }))
  let downloads = 0
  const oldClick = HTMLAnchorElement.prototype.click
  HTMLAnchorElement.prototype.click = function () { if (this.download) downloads++; else oldClick.call(this) }
  const host = document.createElement('div'); document.body.append(host); const mounted = createRoot(host)
  let currentMe = original, unreachable = false, readOnly = false, label = 'initial'
  const check = (ok, why) => { if (!ok) problems.push(why) }
  const button = text => [...host.querySelectorAll('button')].find(node => node.textContent.trim() === text)
  const render = async () => act(async () => mounted.render(h(MeContext.Provider, { value: { me: currentMe, unreachable, reload() {}, customTypes: new Set(), reloadConfig() {} } },
    h(ToastProvider, null, h(MemoryRouter, { initialEntries: ['/audit?period=custom&from=2026-10-01&to=2026-10-12'] }, h(EnterpriseReadOnly.Provider, { value: readOnly },
      h(Profiler, { id: 'audit', onRender: () => commits.push({ label, html: host.innerHTML }) }, h(AuditPage))))))))
  const settle = async index => act(async () => reads[index].resolve(structuredClone(sample)))
  const privateIn = html => html.includes('Emily Carter') || html.includes('10.0.0.8')
  const restore = async () => { currentMe = structuredClone(original); unreachable = false; readOnly = false; label = 'restore'; await render(); await settle(reads.length - 1) }
  try {
    await render(); await settle(0)
    check(host.textContent.includes('Time unavailable'), 'nullable/invalid timestamps lost the row')
    check(host.textContent.includes('count: 0') && host.textContent.includes('"ok":false'), 'nested or zero details lost')
    check(!host.querySelector('img'), 'metadata became HTML')
    const transitions = [
      ['auditor', me => { me.edition.roles = ['auditor', 'user']; me.edition.actions = ['reports.group', 'reports.all', 'audit.read', 'policy.read'] }, true],
      ['security', me => { me.edition.roles = ['security', 'user']; me.edition.actions = ['reports.group', 'reports.all', 'audit.read', 'policy.read', 'policy.edit', 'terms.manage', 'approvals.decide', 'siem.edit'] }, true],
      ['identity', me => { me.server.identity.id = 'u_anotheraaa' }, true],
      ['equal refreshed payload', () => undefined, true],
      ['code session', me => { me.session.via = 'code' }, true],
      ['trial ended', me => { me.edition.license = { ...me.edition.license, state: 'trial-ended' } }, true],
      ['key space', me => { me.server.identity.kind = 'key' }, false],
      ['computer', me => { me.server = null }, false],
      ['missing action', me => { me.edition.actions = [] }, false],
      ['missing page', me => { me.edition.pages = [] }, false],
      ['SIEM-only', me => { me.edition.features = ['siem'] }, false],
    ]
    for (const [name, change, allowed] of transitions) {
      await restore(); label = name; currentMe = structuredClone(original); change(currentMe)
      const first = commits.length, before = reads.length
      await render()
      check(!privateIn(commits[first]?.html ?? ''), `${name}: first commit retained old names/addresses/options`)
      check(reads.length === before + (allowed ? 1 : 0), `${name}: actual server authority read mismatch`)
      if (allowed) await settle(before)
      if (name === 'auditor' || name === 'security') {
        check(!host.textContent.includes('10.0.0.8'), `${name}: retained administrator addresses`)
        check(host.textContent.includes('Emily Carter'), `${name}: legitimate actor name was concealed`)
      }
      if (!allowed || name === 'trial ended') check(!button('Export CSV'), `${name}: export remained available`)
    }
    await restore(); unreachable = true; label = 'unreachable'; let first = commits.length
    await render(); check(!privateIn(commits[first]?.html ?? ''), 'unreachable: stale display')
    await restore(); readOnly = true; label = 'read-only'; first = commits.length
    await render(); check(!privateIn(commits[first]?.html ?? ''), 'read-only: first stale commit'); await settle(reads.length - 1); check(!button('Export CSV'), 'read-only export action')
    for (const status of [403, 413, 429]) {
      await restore(); const csv = button('Export CSV'), json = button('Export JSON'), before = exports.length
      check(!!csv && !!json, 'export controls missing after valid read')
      await act(async () => { csv?.click(); json?.click() })
      check(exports.length === before + 1, 'same-tick duplicate export')
      await act(async () => exports.at(-1).reject(new ApiError(status, '<private-error-canary>')))
      check(!privateIn(host.innerHTML) && !button('Export CSV'), `${status}: stale names/options/export`)
      check(!host.textContent.includes('private-error-canary'), `${status}: raw refusal echoed`)
    }
    await restore(); await act(async () => button('Export CSV').click())
    const pending = exports.at(-1); currentMe = structuredClone(original); currentMe.edition.roles = ['auditor']; label = 'pending export authority'
    await render(); await act(async () => pending.resolve({ name: 'obsolete.csv', type: 'text/csv', file: '\uFEFFmetadata\r\n' }))
    check(downloads === 0 && !host.textContent.includes('obsolete.csv'), 'obsolete authority downloaded or notified')
    await settle(reads.length - 1)
    await restore(); await act(async () => button('Export CSV').click())
    const late = exports.at(-1), before = reads.length
    await act(async () => mounted.unmount()); await act(async () => late.resolve({ name: 'late.csv', type: 'text/csv', file: 'metadata' }))
    check(downloads === 0 && reads.length === before, 'unmount caused a download or reload')
    return { problems, transitions: transitions.length + 3, commits: commits.length, exports: exports.length, downloads }
  } finally { await act(async () => mounted.unmount()); host.remove(); auditApi.log = originalLog; auditApi.exportLog = originalExport; HTMLAnchorElement.prototype.click = oldClick }
}
