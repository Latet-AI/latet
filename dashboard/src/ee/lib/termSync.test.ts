import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import i18n from '../../i18n'
import de from '../../locales/de.json'
import en from '../../locales/en.json'
import es from '../../locales/es.json'
import fr from '../../locales/fr.json'
import itLocale from '../../locales/it.json'
import type { TermSourceSettings, TermSyncRun, TermSyncSource } from '../types'
import {
  allSelected,
  byType,
  FOLLOWED_FOR_MS,
  followed,
  keepListed,
  lastStarted,
  localSource,
  needsSecret,
  newRuns,
  QUICK_FOR_MS,
  ranText,
  refusedKey,
  sourceState,
  sourcesRefusal,
  stillRunning,
  toggle,
  type Following,
} from './termSync'

const run = (outcome: 'ok' | 'failed'): TermSyncRun => ({
  source: 'customers', kind: 'file', started: 1, finished: 2, outcome, code: null, reason: null, found: 0, added: 0, gone: 0, skipped: {},
})
const source = (over: Partial<TermSyncSource> = {}): TermSyncSource => ({
  name: 'customers', kind: 'file', every_minutes: 60, configured: true, terms: 3, by_type: {}, gone: 0, asked: null, last: null, next: null, ...over,
})
const settings = (over: Partial<TermSourceSettings> & Pick<TermSourceSettings, 'kind'>): TermSourceSettings => ({
  name: 'customers', every_minutes: 60, mapping: { company: 'CLIENT' }, ...over,
})

describe('term sync', () => {
  it('tells how a source last synced', () => {
    expect(sourceState(source())).toBe('never')
    expect(sourceState(source({ last: run('ok') }))).toBe('ok')
    expect(sourceState(source({ last: run('failed') }))).toBe('failed')
    expect(sourceState(source({ configured: false, kind: null, last: run('ok') }))).toBe('retired')
  })

  it('lists the types with the most terms first', () => {
    expect(byType({ PERSON: 24, CLIENT: 1180, EMAIL: 24 })).toEqual([['CLIENT', 1180], ['EMAIL', 24], ['PERSON', 24]])
  })

  it('selects terms one by one, and keeps only those still listed', () => {
    const one = toggle(new Set(), 'a')
    expect([...toggle(one, 'b')]).toEqual(['a', 'b'])
    expect([...toggle(one, 'a')]).toEqual([])
    expect([...keepListed(new Set(['a', 'gone']), ['a', 'b'])]).toEqual(['a'])
    expect(allSelected(['a', 'b'], new Set(['a', 'b']))).toBe(true)
    expect(allSelected([], new Set())).toBe(false)
  })

  it('waits while a source asked for has not run since', () => {
    expect(stillRunning([source({ asked: 100, last: run('ok') })], ['customers'])).toBe(true) // its last run began before
    expect(stillRunning([source({ asked: null, last: run('ok') })], ['customers'])).toBe(false)
    expect(stillRunning([source({ asked: 100 })], [])).toBe(false) // nothing asked from this page
  })

  it("gives each source asked for its new run, once there: never the run it had when it was asked", () => {
    const before = lastStarted([source({ last: run('ok') }), source({ name: 'directory' })], ['customers', 'directory'])
    expect(before).toEqual({ customers: 1, directory: null })
    const again = { ...run('failed'), started: 50, finished: 52 }
    // The ask still stands: nothing to tell yet.
    expect(newRuns([source({ asked: 40, last: run('ok') })], ['customers'], before)).toEqual([])
    // Run since: its new run.
    expect(newRuns([source({ last: again })], ['customers'], before)).toEqual([again])
    // Its ask gone with no new run (nothing ran, the source set up otherwise): nothing to tell.
    expect(newRuns([source({ last: run('ok') })], ['customers'], before)).toEqual([])
    // A source that had never run, and one taken out since.
    const first = { ...run('ok'), source: 'directory', started: 60, finished: 61 }
    expect(newRuns([source({ name: 'directory', last: first })], ['customers', 'directory'], before)).toEqual([first])
  })

  it('tells which sources read with a secret, as the server does', () => {
    expect(needsSecret(settings({ kind: 'graph', tenant: 'acme.onmicrosoft.com' }))).toBe(true)
    expect(needsSecret(settings({ kind: 'ldap', url: 'ldaps://dc1.acme.lan', bind: 'CN=latet-sync,OU=Service,DC=acme,DC=lan' }))).toBe(true)
    expect(needsSecret(settings({ kind: 'ldap', url: 'ldaps://dc1.acme.lan', bind: null }))).toBe(false) // anonymous
    expect(needsSecret(settings({ kind: 'ldap', url: 'ldaps://dc1.acme.lan' }))).toBe(false)
    expect(needsSecret(settings({ kind: 'url', url: 'https://crm.acme.com/export?key={secret}', headers: {} }))).toBe(true)
    expect(needsSecret(settings({ kind: 'url', url: 'https://crm.acme.com/export', headers: { Authorization: 'Bearer {secret}' } }))).toBe(true)
    expect(needsSecret(settings({ kind: 'url', url: 'https://crm.acme.com/export', headers: { Accept: 'text/csv' } }))).toBe(false)
    expect(needsSecret(settings({ kind: 'file', path: 'customers.csv' }))).toBe(false)
  })

  it("tells a source at the server's own computer: set with latet server terms alone", () => {
    for (const url of ['ldap://localhost', 'ldap://LOCALHOST.:389', 'ldaps://127.0.0.1:636', 'ldap://[::1]', 'https://localhost/export.csv']) {
      expect(localSource(settings({ kind: url.startsWith('https') ? 'url' : 'ldap', url })), url).toBe(true)
    }
    for (const url of ['ldaps://dc1.acme.lan', 'https://crm.acme.com/localhost', 'ldaps://127.0.0.2', 'https://localhost.acme.com']) {
      expect(localSource(settings({ kind: url.startsWith('https') ? 'url' : 'ldap', url })), url).toBe(false)
    }
    expect(localSource(settings({ kind: 'graph', login_url: 'https://login.microsoftonline.com' }))).toBe(false)
    expect(localSource(settings({ kind: 'file', path: 'customers.csv' }))).toBe(false)
  })

  it("words a refused change of the sources, or of a secret, by the server's code", () => {
    const refused = (status: number, code?: string) => new ApiError(status, 'its words', [], { error: 'its words', ...(code ? { code } : {}) })
    expect(sourcesRefusal(refused(409, 'sources-changed'))).toBe('changed')
    expect(sourcesRefusal(refused(403, 'by-command'))).toBe('byCommand')
    expect(sourcesRefusal(refused(403, 'company-sign-in'))).toBe('company')
    expect(sourcesRefusal(refused(429))).toBe('tooMany')
    // Where, and what kind of error, in the server's words (never what was typed): shown as they are.
    for (const status of [400, 404, 409, 413]) expect(sourcesRefusal(refused(status)), String(status)).toBe('refused')
    for (const error of [refused(403, 'not-manageable'), refused(403, 'trial-ended'), refused(503), new Error('down')]) {
      expect(sourcesRefusal(error)).toBe('failed') // said as any other error
    }
  })

  it("words a source's command for a change of the sources, and the secret's command for a secret's", () => {
    expect(refusedKey('byCommand', 'sources')).toBe('ee.termSync.sources.byCommand')
    expect(refusedKey('byCommand', 'secret')).toBe('ee.termSync.secret.byCommand')
    expect(refusedKey('tooMany', 'sources')).toBe('ee.termSync.sources.tooMany')
    for (const why of ['changed', 'company', 'refused', 'failed'] as const) expect(refusedKey(why, 'secret')).toBeNull()
    for (const locale of [en, itLocale, de, fr, es]) {
      expect(locale.ee.termSync.secret.byCommand).toContain('`latet server terms secret`')
      expect(locale.ee.termSync.sources.byCommand).toContain('`latet server terms sources set`')
    }
  })

  it("tells a sync's result with its numbers formatted, the count choosing the plural", () => {
    const t = i18n.t.bind(i18n) as (key: string, options?: Record<string, unknown>) => string
    const number = (n: number) => n.toLocaleString('en')
    const ok = { ...run('ok'), found: 1204, added: 1180, gone: 2 }
    expect(ranText(t, number, ok)).toBe('customers: 1,204 terms, 1,180 new, 2 no longer in the source')
    expect(ranText(t, number, { ...ok, found: 1, added: 0, gone: 0 })).toBe('customers: 1 term, 0 new, 0 no longer in the source')
    const failed = { ...run('failed'), source: 'directory', reason: 'the directory refused the sign-in (invalidCredentials)' }
    expect(ranText(t, number, failed)).toBe('directory: not synced. the directory refused the sign-in (invalidCredentials)')
  })

  it('says terms no longer in their source in full in French, never an ambiguous "plus dans"', () => {
    const strings = (value: unknown): string[] =>
      typeof value === 'string' ? [value] : Object.values(value as Record<string, unknown>).flatMap(strings)
    const ambiguous = strings(fr.ee.termSync).filter((text) => /plus dans/.test(text) && !/(?:n'est|ne sont) plus dans/.test(text))
    expect(ambiguous).toEqual([])
  })
})

describe('following a sync asked for', () => {
  const asked = (over: Partial<Following> = {}): Following => ({ before: { customers: 1, directory: null }, since: 0, late: false, failed: false, ...over })
  const ran = (name: string, started: number, outcome: 'ok' | 'failed' = 'ok'): TermSyncSource =>
    source({ name, last: { ...run(outcome), source: name, started, finished: started + 1 } })

  it('reads again while an ask stands, and tells each source its new run once there', () => {
    const waiting = followed(asked(), { sources: [source({ asked: 5, last: run('ok') }), source({ name: 'directory', asked: 5 })] }, 2000)
    expect(waiting).toMatchObject({ done: [], runs: [], say: [], stop: false, wait: 2000 })
    const half = followed(asked(), { sources: [ran('customers', 9), source({ name: 'directory', asked: 5 })] }, 4000)
    expect(half).toMatchObject({ done: ['customers'], say: [], stop: false })
    expect(half.runs.map((r) => r.source)).toEqual(['customers'])
    const all = followed(asked({ before: { directory: null } }), { sources: [ran('directory', 9, 'failed')] }, 6000)
    expect(all).toMatchObject({ done: ['directory'], stop: true })
    expect(all.runs.map((r) => [r.source, r.outcome])).toEqual([['directory', 'failed']])
  })

  it('says once that it is still syncing past a minute, then reads every 15 seconds', () => {
    const standing = { sources: [source({ asked: 5, last: run('ok') }), source({ name: 'directory', asked: 5 })] }
    expect(followed(asked(), standing, QUICK_FOR_MS)).toMatchObject({ say: ['stillRunning'], late: true, wait: 15000, stop: false })
    expect(followed(asked({ late: true }), standing, QUICK_FOR_MS + 15000)).toMatchObject({ say: [], wait: 15000, stop: false })
  })

  it('stops after 15 minutes, saying the run did not show in time', () => {
    const standing = { sources: [source({ asked: 5, last: run('ok') })] }
    expect(FOLLOWED_FOR_MS).toBe(15 * 60000)
    expect(followed(asked({ late: true }), standing, FOLLOWED_FOR_MS)).toMatchObject({ say: ['notShown'], stop: true, wait: null })
  })

  it('says a failing read once and reads again, and stops on a refusal that reading again does not change', () => {
    const down = followed(asked(), { error: 'again' }, 2000)
    expect(down).toMatchObject({ say: ['failed'], failed: true, stop: false, wait: 2000 })
    expect(followed(asked({ failed: true }), { error: 'again' }, 4000)).toMatchObject({ say: [], stop: false })
    expect(followed(asked(), { error: 'stop' }, 2000)).toMatchObject({ say: ['failed'], stop: true, wait: null })
    expect(followed(asked({ failed: true }), { error: 'again' }, FOLLOWED_FOR_MS)).toMatchObject({ say: ['notShown'], stop: true })
  })
})
