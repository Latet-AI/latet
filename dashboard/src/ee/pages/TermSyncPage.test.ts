import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MeContext, type MeState } from '../../lib/me'
import { EnterpriseReadOnly } from '../components/TrialEnded'
import type { GoneTerm, TermSyncSources, TermSyncState } from '../types'

// What each read of the page answers, by its key (useAsync's first argument).
const answers: Record<string, unknown> = {}
vi.mock('../../lib/useAsync', () => ({
  useAsync: (key: string | null) => ({
    data: key === null ? undefined : answers[key],
    error: undefined,
    loading: false,
    stale: false,
    reload() {},
    mutate() {},
  }),
}))
const { SecretDialog, TermSyncPage } = await import('./TermSyncPage')

const run = { source: 'colleagues', kind: 'graph' as const, started: 1, finished: 2, outcome: 'ok' as const, code: null, reason: null, found: 821, added: 0, gone: 0, skipped: {} }
const state = (paused: boolean): TermSyncState => ({
  licensed: true,
  paused,
  masked: 822,
  kept: 822,
  most: 50000,
  gone: 1,
  unreadable: 0,
  behind: null,
  problems: [],
  sources: [{ name: 'colleagues', kind: 'graph', every_minutes: 360, configured: true, terms: 821, by_type: { PERSON: 821 }, gone: 1, asked: null, last: run, next: 3 }],
})
const gone: { items: GoneTerm[]; total: number } = {
  items: [{ id: 'e1c7064f9a3b85d2c0f4b719', source: 'colleagues', type: 'PERSON', value: 'Martin Hale', gone: 1 }],
  total: 1,
}
const sources: TermSyncSources = {
  sources: [{ name: 'colleagues', kind: 'graph', tenant: 'acme.onmicrosoft.com', every_minutes: 360, mapping: { displayName: 'PERSON' } }],
  text: 'sources:\n- name: colleagues\n',
  etag: '7c3e9a1f5b2d48e60000000000000000',
  where: { colleagues: { addresses: ['https://graph.microsoft.com:443', 'https://login.microsoftonline.com:443'], bind: null } },
}

function page(readOnly = false): string {
  const me = { me: { server: {}, edition: { pages: ['term-sync'], actions: ['terms.manage'], roles: ['security'] } }, unreachable: false, customTypes: new Set(), reload() {}, reloadConfig() {} } as unknown as MeState
  return renderToStaticMarkup(h(MeContext.Provider, { value: me }, h(MemoryRouter, null, h(EnterpriseReadOnly.Provider, { value: readOnly }, h(TermSyncPage)))))
}

/** The secret dialog of `source`, open, saying where it connects (`place`), as the page renders it. */
function dialogOf(source: string, place: TermSyncSources['where'][string] | null): string {
  const me = { me: { server: {}, edition: { pages: ['term-sync'], actions: ['terms.manage'], roles: ['security'] } }, unreachable: false, customTypes: new Set(), reload() {}, reloadConfig() {} } as unknown as MeState
  const props = { source, place, paused: false, etag: sources.etag, onClose() {}, onChanged() {}, onSaved() {} }
  return secretDialog(renderToStaticMarkup(h(MeContext.Provider, { value: me }, h(SecretDialog, props))))
}

/** The buttons (and inputs) whose text or label says `label`, as their opening tags. */
function controls(html: string, label: string): string[] {
  const buttons = [...html.matchAll(/<button\b[^>]*>(?:(?!<\/button>).)*<\/button>/g)].map((m) => m[0]).filter((b) => b.replace(/<[^>]+>/g, '').includes(label))
  const inputs = [...html.matchAll(/<input\b[^>]*>/g)].map((m) => m[0]).filter((i) => i.includes(`aria-label="${label}`))
  return [...buttons, ...inputs].map((tag) => tag.slice(0, tag.indexOf('>') + 1))
}

/** The secret dialog, from its opening tag to the first closing tag of a dialog after it. */
function secretDialog(html: string): string {
  const start = html.indexOf('<dialog aria-labelledby="term-sync-secret-title"')
  expect(start).toBeGreaterThanOrEqual(0)
  return html.slice(start, html.indexOf('</dialog>', start) + '</dialog>'.length)
}

beforeEach(() => {
  answers['ee-term-sync'] = state(false)
  answers['ee-term-sync-gone'] = gone
  answers['ee-term-sync-sources'] = sources
})

describe('the Term sync page', () => {
  it('offers its changes while term sync runs', () => {
    const html = page()
    for (const label of ['Edit the sources', 'Secret', 'Select Martin Hale', 'Sync now']) {
      const found = controls(html, label)
      expect(found.length, label).toBeGreaterThan(0)
      expect(found.every((tag) => !/\sdisabled=""/.test(tag)), label).toBe(true)
    }
  })

  it('disables every change while the license pauses term sync, as the SIEM page does', () => {
    answers['ee-term-sync'] = state(true)
    const html = page()
    for (const label of ['Edit the sources', 'Secret', 'Select all', 'Select Martin Hale', 'Remove 0 terms', 'Sync now', 'Sync all now']) {
      const found = controls(html, label)
      expect(found.length, label).toBeGreaterThan(0)
      expect(found.every((tag) => /\sdisabled=""/.test(tag)), label).toBe(true)
    }
    // The secret dialog's Save and Remove too, were it open.
    const dialog = secretDialog(html)
    for (const label of ['Save', 'Remove']) expect(controls(dialog, label).every((tag) => /\sdisabled=""/.test(tag)), label).toBe(true)
  })

  it('keeps the confirmation of a removal outside the secret dialog: Escape there closes it alone', () => {
    const dialog = secretDialog(page())
    expect(dialog.slice(1)).not.toContain('<dialog') // no dialog nested in it, whose cancel would reach it
    expect(page()).toContain('Remove the secret of ?') // the confirmation is rendered, beside it
  })

  it("leaves the secret field out of the browser's password manager", () => {
    const field = dialogOf('colleagues', sources.where.colleagues!).match(/<input\b[^>]*type="password"[^>]*>/)?.[0] ?? ''
    expect(field).toMatch(/\sautocomplete="off"/i)
    expect(field).not.toContain('new-password')
  })

  // The re-review's N1: each line the server says of its instances is shown in its own words, its commands as code:
  // the line of an instance without the `ldap` extra is no line about terms left unmasked.
  it("shows each problem the server says as it says it, never as unmasked terms when it is not", () => {
    const ldap = 'directory: this instance has no `ldap` extra, which LDAP needs: it leaves the source to the instances that have it'
    const older = '1 instance does not run this release of Latet with the Enterprise Edition: it masks no synced term in the spaces it serves'
    answers['ee-term-sync'] = { ...state(false), problems: [ldap, older] }
    const html = page()
    expect(html).not.toContain('do not mask synced terms')
    expect(html).toContain('<code class="mono rounded-xs bg-wash-2 px-1 py-px text-[0.95em] whitespace-nowrap text-ink">ldap</code>')
    expect(html).not.toContain('`ldap`')
    expect(html).toContain('it masks no synced term in the spaces it serves')
  })

  it('shows no change at all once a trial ended', () => {
    const html = page(true)
    for (const label of ['Edit the sources', 'Secret', 'Select all', 'Sync now', 'Sync all now']) expect(controls(html, label), label).toEqual([])
  })
})

// The final review's M1: the dialog says where the source connects before it asks for the secret, as
// `latet server terms secret` does, so that a source someone pointed elsewhere never gets the secret typed in unseen.
describe('the secret dialog', () => {
  it('says where the source connects, and whom a directory signs in as, before it asks for the secret', () => {
    const html = dialogOf('directory', { addresses: ['ldaps://dc1.acme.lan:636'], bind: 'CN=latet-sync,OU=Service,DC=acme,DC=lan' })
    const asked = html.indexOf('type="password"')
    expect(asked).toBeGreaterThan(0)
    for (const said of ['It connects to', 'ldaps://dc1.acme.lan:636', 'It signs in as CN=latet-sync,OU=Service,DC=acme,DC=lan.']) {
      expect(html.indexOf(said), said).toBeGreaterThan(0)
      expect(html.indexOf(said), said).toBeLessThan(asked)
    }
  })

  it('names every address of a Graph source and no sign-in, and asks for nothing while it cannot say where one connects', () => {
    const graph = dialogOf('colleagues', sources.where.colleagues!)
    expect(graph).toContain('https://graph.microsoft.com:443')
    expect(graph).toContain('https://login.microsoftonline.com:443')
    expect(graph).not.toContain('signs in as')
    const unknown = dialogOf('directory', null)
    expect(unknown).toContain('Where this source connects cannot be shown')
    expect(unknown).not.toContain('type="password"')
    expect(controls(unknown, 'Save').every((tag) => tag.includes(' disabled=""'))).toBe(true)
  })
})
