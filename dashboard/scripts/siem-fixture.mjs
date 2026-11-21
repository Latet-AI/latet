// Mounted SIEM regression fixture: real page and hook, controlled synthetic transport boundaries.
/* global window, document, HTMLInputElement */
import { createElement as h, act, Profiler } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { MeContext } from '../src/lib/me'
import { ToastProvider } from '../src/components/Toast'
import { EnterpriseReadOnly } from '../src/ee/components/TrialEnded'
import { SiemPage } from '../src/ee/pages/SiemPage'
import { reportsApi } from '../src/ee/api'
import { api, ApiError, setTransport } from '../src/api/client'
import { mockTransport } from '../src/mock'

// The real server's auditor actions (roles.py), including when only SIEM is licensed.
const AUDITOR_ACTIONS = ['reports.group', 'reports.all', 'audit.read', 'policy.read']

export async function checkSiem() {
  setTransport(mockTransport)
  window.IS_REACT_ACT_ENVIRONMENT = true
  const original = await api.me()
  const sample = await reportsApi.siem()
  sample.outputs[0].url = 'https://splunk.acme.com/private-tenant-path'
  const reads = [], writes = [], tests = [], problems = [], commits = []
  reportsApi.siem = () => new Promise((resolve, reject) => reads.push({ resolve, reject }))
  reportsApi.saveSiem = (outputs, secrets, etag) => new Promise((resolve, reject) => writes.push({ outputs, secrets, etag, resolve, reject }))
  reportsApi.testSiem = name => new Promise((resolve, reject) => tests.push({ name, resolve, reject }))
  const canary = 'synthetic-secret-canary-15'
  const host = document.createElement('div'); document.body.append(host)
  const mounted = createRoot(host)
  let currentMe = original, unreachable = false, readOnly = false, label = 'initial'
  const check = (ok, why) => { if (!ok) problems.push(why) }
  // A closed native dialog retains its buttons in the DOM but exposes no controls to the reader.
  const button = text => [...host.querySelectorAll('button')].find(node => !node.closest('dialog:not([open])') && node.textContent.trim() === text)
  const render = async () => act(async () => mounted.render(h(MeContext.Provider, { value: { me: currentMe, unreachable, reload() {}, customTypes: new Set(), reloadConfig() {} } },
    h(ToastProvider, null, h(MemoryRouter, { initialEntries: ['/reports/siem'] }, h(EnterpriseReadOnly.Provider, { value: readOnly },
      h(Profiler, { id: 'siem', onRender: () => commits.push({ label, html: host.innerHTML }) }, h(SiemPage))))))))
  const settle = async (index, data = sample) => act(async () => reads[index].resolve(structuredClone(data)))
  const click = async text => { const node = button(text); check(!!node, `missing ${text}`); if (node) await act(async () => node.click()) }
  const typeSecret = async () => {
    const input = host.querySelector('input[type=password]')
    check(!!input, 'secret input missing')
    if (input) await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, canary); input.dispatchEvent(new Event('input', { bubbles: true })) })
  }
  const privateIn = html => html.includes('private-tenant-path') || html.includes(canary) || html.includes('type="password"')
  try {
    await render(); await settle(0)
    await click('Edit'); await typeSecret()
    check(host.querySelector('input[type=password]')?.value === canary, 'fixture did not enter secret')
    check(!host.textContent.includes(canary), 'secret appeared as text')
    const form = host.querySelector('form'), beforeWrites = writes.length
    await act(async () => { form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })
    check(writes.length === beforeWrites + 1, 'same-tick duplicate write')
    check(writes.at(-1)?.secrets.splunk === canary, 'secret absent from authorized field')
    check(!JSON.stringify(writes.at(-1)?.outputs).includes(canary), 'secret leaked into settings')
    check(!host.querySelector('input[type=password]'), 'submitted secret remained in DOM')
    await act(async () => writes.at(-1).reject(new ApiError(409, canary, [], { code: 'siem-changed' })))
    check(!host.innerHTML.includes(canary), 'conflict echoed secret')
    check(!host.querySelector('input[type=password]'), 'conflict retained form')
    const newer = structuredClone(sample); newer.etag = 'fresh'
    await settle(reads.length - 1, newer)
    check(writes.length === beforeWrites + 1, 'conflict automatically replayed write')
    await click('Edit'); await typeSecret()
    const transitions = [
      ['auditor', me => { me.edition.roles = ['auditor']; me.edition.actions = [...AUDITOR_ACTIONS] }],
      ['SIEM-only auditor', me => { me.edition.roles = ['auditor']; me.edition.actions = [...AUDITOR_ACTIONS]; me.edition.pages = ['siem']; me.edition.features = ['siem'] }],
      ['identity', me => { me.server.identity.id = 'u_anotheraaa' }],
      ['equal refreshed payload', () => undefined],
      ['code session', me => { me.session.via = 'code' }],
      ['trial ended', me => { me.edition.license = { ...me.edition.license, state: 'trial-ended' } }],
      ['key space', me => { me.server.identity.kind = 'key' }],
      ['missing action', me => { me.edition.actions = [] }],
      ['missing page', me => { me.edition.pages = [] }],
    ]
    for (const [name, change] of transitions) {
      label = name; currentMe = structuredClone(original); change(currentMe)
      const first = commits.length, before = reads.length
      await render()
      check(!privateIn(commits[first]?.html ?? ''), `${name}: first commit leaked prior data/draft`)
      const auditor = name === 'auditor' || name === 'SIEM-only auditor'
      if (auditor) check(reads.length === before + 1, `${name}: real server actions did not start a SIEM read`)
      if (reads.length > before) {
        const reader = structuredClone(sample); reader.editable = false; reader.etag = null
        reader.outputs = reader.outputs.map(o => ({ ...o, url: `${new URL(o.url).protocol}//${new URL(o.url).host}`, headers: {} }))
        await settle(before, reader)
      }
      check(!host.querySelector('input[type=password]'), `${name}: retained secret input`)
      if (auditor) {
        check(!!host.querySelector('section[aria-label="syslog"]'), `${name}: redacted outputs not rendered`)
        check(!button('Add an output') && !button('Send a test event') && !button('Edit') && !button('Remove') && !host.querySelector('dialog[open]'), `${name}: reader gained mutation controls`)
        const beforeMutations = writes.length + tests.length
        const hiddenConfirm = [...host.querySelectorAll('dialog:not([open]) button')].find(node => node.textContent.trim() === 'Remove')
        if (hiddenConfirm) await act(async () => hiddenConfirm.click())
        check(writes.length + tests.length === beforeMutations, `${name}: closed confirmation submitted a mutation`)
        check(!privateIn(host.innerHTML), `${name}: current reader view leaked privileged data`)
      }
      if (name === 'SIEM-only auditor') check(!host.querySelector('a[href="/reports"]'), 'SIEM-only auditor gained a Reports link')
    }
    label = 'restore'; currentMe = structuredClone(original)
    await render(); await settle(reads.length - 1)
    await click('Edit'); await typeSecret()
    label = 'unreachable'; unreachable = true; const first = commits.length
    await render(); check(!privateIn(commits[first]?.html ?? ''), 'unreachable leaked privileged data')
    unreachable = false; currentMe = structuredClone(original); label = 'restore'
    await render(); await settle(reads.length - 1)
    await click('Edit'); await typeSecret()
    readOnly = true; label = 'context read-only'; const at = commits.length
    await render(); check(!privateIn(commits[at]?.html ?? ''), 'read-only transition kept secret')
    if (reads.length) await settle(reads.length - 1)
    check(!button('Add an output') && !button('Send a test event'), 'read-only retained mutations')
    readOnly = false; currentMe = structuredClone(original); await render(); await settle(reads.length - 1)
    await click('Send a test event')
    const pending = tests.at(-1), before = reads.length
    currentMe = structuredClone(original); currentMe.edition.roles = ['auditor']; currentMe.edition.actions = [...AUDITOR_ACTIONS]; label = 'pending test authority'
    await render(); check(reads.length === before, 'authority change released submitted operation early')
    await act(async () => pending.resolve({ ok: true, detail: canary }))
    check(!host.textContent.includes('The test send completed'), 'old test emitted success toast')
    check(!host.innerHTML.includes(canary), 'old test echoed receiver canary')
    check(reads.length === before + 1, 'pending test authority: real auditor read did not resume after settlement')
    if (reads.length > before) await settle(reads.length - 1, { ...sample, editable: false, etag: null, outputs: [] })
    check(!JSON.stringify(window.localStorage).includes(canary) && !JSON.stringify(window.sessionStorage).includes(canary), 'secret persisted in browser storage')
    return { problems, transitions: transitions.length + 3, commits: commits.length, writes: writes.length, tests: tests.length }
  } finally { await act(async () => mounted.unmount()); host.remove() }
}
