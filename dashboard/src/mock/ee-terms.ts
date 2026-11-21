// Term sync in mock mode (Latet Enterprise, E6), the security role's: Daniel Okafor (mock/people.ts, withSecurity,
// behind ?mock-server&mock-sign-in&mock-security). A CRM export, Microsoft Entra ID's people and groups, a directory
// whose sign-in is refused, and three terms no longer in their source. The answers are the server's
// (src/latet/ee/sync/api.py), in its words and with its codes: the state answers in every case; the gone list and the
// sources are shown to a session the company's sign-in opened, and every change is made from one (&mock-code-session:
// 403 company-sign-in); a sync, and every change, is refused while the license pauses term sync (&mock-license=paused or
// invalid: 403 not-manageable) and once a trial ended (&mock-license=trial-ended: 403 trial-ended). A change of the
// sources, and a secret's, names the etag of the sources read (400 without it, 409 sources-changed for another); a source
// at the server's own computer, and its secret, are `latet server terms`' alone: added, changed, taken out or its secret
// set from the page, 403 by-command (&mock-terms-local adds one the command set). The mock keeps the text it is given (a line with a [ it does not close is refused, as the server
// refuses YAML it cannot read) and reads its sources from it. A sync asked for runs at the next read of the state; the
// directory's sign-in is refused until its secret is set again here. No secret is kept, only that one is.

import { ApiError, type Method } from '../api/client'
import { localSource, needsSecret } from '../ee/lib/termSync'
import type { GoneTerm, TermSourcePlace, TermSourceSettings, TermSyncRun, TermSyncSource, TermSyncState } from '../ee/types'
import { NOW } from './fixtures'
import { SECURITY_MOCK } from './people'

const flags = new URLSearchParams(typeof window === 'undefined' ? '' : window.location.search)
const CODE_SESSION = flags.has('mock-code-session') // the session `latet dashboard`'s code opened
const LICENSE = flags.get('mock-license')
const TRIAL_ENDED = LICENSE === 'trial-ended'
const PAUSED = TRIAL_ENDED || LICENSE === 'paused' || LICENSE === 'invalid' // the license pauses term sync
const LOCAL = flags.has('mock-terms-local')

const MOST = 50000
const KINDS = ['file', 'url', 'graph', 'ldap'] as const
const NAME = /^[a-z0-9][a-z0-9-]{0,39}$/
const ID = /^[0-9a-f]{24}$/
// The server's words (ee/sync/api.py, ee/settings.py, ee/routes.py), which a refusal here repeats with its code.
const TERMS_BY_COMPANY = "the terms are shown to a session your company's sign-in opened: sign in with your company account"
const SOURCES_BY_COMPANY = "the sources are shown to a session your company's sign-in opened: sign in with your company account"
const CHANGES_BY_SSO = 'sign in with your company account to change what applies to everyone'
const TRIAL = "the Latet Enterprise trial ended on 2026-10-10: install a license, or remove the trial's file to go back to the Community Edition; protection is unchanged"
const NOT_MANAGEABLE =
  LICENSE === 'invalid'
    ? 'the license file cannot be used (`latet server license show` says why): the Enterprise settings can be read, not changed, until it is replaced; protection is unchanged'
    : 'the Latet Enterprise license is paused (past the 30 days of grace): the Enterprise settings can be read, not changed, until a license that covers them is installed; protection is unchanged'
const SOURCES_CHANGED = 'the sources changed since they were read: read them again'
const ETAG_NEEDED = 'a change from the dashboard names the "etag" of the sources it was made from, as they were read'
const BY_COMMAND = 'a source at this computer (localhost, 127.0.0.1, [::1]) is set with `latet server terms sources set`, never from the dashboard'
const CREDENTIAL_BY_COMMAND =
  'the secret of a source at this computer (localhost, 127.0.0.1, [::1]) is set with `latet server terms secret`, never from the dashboard'
const NO_SOURCE = 'no source has that name'
const NO_SECRET = 'no secret is kept for that source'
const NEEDS_NONE = 'this source reads with no secret'
const RUN_BODY = "give a source's name, or nothing for every source"
const REMOVE_BODY = 'say which terms: their ids, a source, all, or those no key opens'
const SOURCES_BODY = 'the body is {"sources": [...]} or {"text": "<the section in YAML>"}, and the "etag" of those read'
const TEXT = 'the text is the section term_sync in YAML: `sources:` and the list of sources'
const SECRET_BODY = 'the body is {"value": "<the secret>"}, and the "etag" of the sources read'
const REMOVAL_BODY = 'the body is nothing, or {"etag": "<the etag of the sources read>"}'
const REFUSED_SIGN_IN = 'the directory refused the sign-in (invalidCredentials)'

// A scheme's own port where an address writes none (ee/settings.py default_port).
const PORTS: Record<string, number> = { https: 443, http: 80, ldaps: 636, ldap: 389 }

/** An address as the server says where a source connects: `scheme://host:port`, the host in lowercase with no trailing
 * dot, an IPv6 host in brackets. */
function addressOf(url: string): string | null {
  const found = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/(\[[^\]]*\]|[^/?#:]*)(?::(\d+))?/.exec(url)
  if (!found) return null
  const scheme = found[1]!.toLowerCase()
  return `${scheme}://${found[2]!.toLowerCase().replace(/\.+$/, '')}:${found[3] ? Number(found[3]) : (PORTS[scheme] ?? 0)}`
}

/** Where a source connects, and the DN a directory signs in as (ee/sync/api.py `show_sources`' `where`). */
function whereOf(s: TermSourceSettings): TermSourcePlace {
  const text = (value: unknown, otherwise: string) => (typeof value === 'string' ? value : otherwise)
  const urls =
    s.kind === 'graph' ? [text(s.login_url, 'https://login.microsoftonline.com'), text(s.graph_url, 'https://graph.microsoft.com')]
      : s.kind === 'url' || s.kind === 'ldap' ? [text(s.url, '')]
        : []
  const addresses = [...new Set(urls.map(addressOf).filter((a): a is string => a !== null))].sort()
  return { addresses, bind: s.kind === 'ldap' && typeof s.bind === 'string' ? s.bind : null }
}

const refused = (status: number, error: string, code?: string) => new ApiError(status, error, [], code ? { error, code } : { error })
const ago = (minutes: number) => NOW - minutes * 60

/** A source as the mock keeps it: its settings and their text, its terms still in it by type, its last run, an ask. */
interface Kept {
  settings: TermSourceSettings
  configured: boolean
  terms: Record<string, number>
  retired: number // terms it had when it was taken out, no longer in the source (none of them listed)
  last: TermSyncRun | null
  asked: number | null
}

const CLIENT_ID = '3c7f2a91-6d4e-4b8a-9f15-0e2d7c6b5a49' // Latet's app in the company's Microsoft Entra tenant
const SETTINGS: TermSourceSettings[] = [
  { name: 'customers', kind: 'file', path: 'customers.csv', every_minutes: 60, mapping: { company: 'CLIENT', contact: 'PERSON' } },
  { name: 'colleagues', kind: 'graph', tenant: 'acme.onmicrosoft.com', client_id: CLIENT_ID, objects: 'users', every_minutes: 360, mapping: { displayName: 'PERSON', mail: 'EMAIL' } },
  { name: 'teams', kind: 'graph', tenant: 'acme.onmicrosoft.com', client_id: CLIENT_ID, objects: 'groups', every_minutes: 360, mapping: { displayName: 'TEAM' }, exclude: ['Sales', 'Support'] },
  { name: 'directory', kind: 'ldap', url: 'ldaps://dc1.acme.lan', bind: 'CN=latet-sync,OU=Service,DC=acme,DC=lan', base: 'OU=People,DC=acme,DC=lan', filter: '(objectClass=person)', every_minutes: 60, mapping: { '{givenName} {sn}': 'PERSON' } },
  ...(LOCAL
    ? [{ name: 'staff', kind: 'ldap' as const, url: 'ldap://localhost:389', start_tls: false, bind: 'CN=latet,OU=Service,DC=acme,DC=lan', base: 'OU=Staff,DC=acme,DC=lan', every_minutes: 60, mapping: { cn: 'PERSON' } }]
    : []),
]
const TERMS: Record<string, Record<string, number>> = {
  customers: { CLIENT: 1180, PERSON: 24 },
  colleagues: { PERSON: 412, EMAIL: 409 },
  teams: { TEAM: 38 },
  staff: { PERSON: 57 },
}
/** What a source gives at its first run that succeeds: the directory once its secret is set again, 12 of each type a
 * new source maps. */
const FIRST_RUN: Record<string, Record<string, number>> = { directory: { PERSON: 640 } }
const LAST: Record<string, [number, string | null]> = { customers: [12, null], colleagues: [95, null], teams: [95, null], directory: [5, REFUSED_SIGN_IN], staff: [40, null] }

function ran(settings: TermSourceSettings, finished: number, terms: Record<string, number>, failure: [string, string] | null, added = 0): TermSyncRun {
  const found = failure ? 0 : Object.values(terms).reduce((a, b) => a + b, 0)
  const [code, reason] = failure ?? [null, null]
  return { source: settings.name, kind: settings.kind, started: finished - 4, finished, outcome: failure ? 'failed' : 'ok', code, reason, found, added, gone: 0, skipped: {} }
}

let kept: Kept[] = SETTINGS.map((settings) => {
  const terms = TERMS[settings.name] ?? {}
  const [minutes, reason] = LAST[settings.name] ?? [60, null]
  return { settings, configured: true, terms, retired: 0, last: ran(settings, ago(minutes), terms, reason ? ['refused', reason] : null), asked: null }
})
let text = `sources:\n${SETTINGS.map(itemText).join('')}`
let version = 0
const etag = () => `7c3e9a1f5b2d48e6${String(version).padStart(16, '0')}` // each change makes another, as a digest would
const secrets = new Set(['colleagues', 'teams', 'directory', ...(LOCAL ? ['staff'] : [])]) // whose secret is kept
const setHere = new Set<string>() // whose secret was set again from the page: the directory signs in with it
let gone: GoneTerm[] = [
  { id: '1f0c6a93d47e25b8c9a04e61', source: 'customers', type: 'CLIENT', value: 'Halden Insurance', gone: ago(12) },
  { id: '5d82be41c09a7f3e6b1d2c48', source: 'customers', type: 'PERSON', value: 'Grace Whitfield', gone: ago(12) },
  { id: 'e1c7064f9a3b85d2c0f4b719', source: 'colleagues', type: 'PERSON', value: 'Martin Hale', gone: ago(95) },
]

// --- the sources' YAML, as the mock writes it and reads it back -------------------------------------------------------

/** A YAML scalar: plain where YAML reads it back as the same string, else in double quotes. */
function scalar(value: unknown): string {
  if (value === null || value === undefined) return 'null'
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  const plain = String(value)
  const risky = plain === '' || /^[\s{[&*!|>'"%@`#,?:-]/.test(plain) || /: |\s#|\s$/.test(plain) || /^(?:true|false|null|yes|no|on|off|~|[-+.\d][\d._]*)$/i.test(plain)
  return risky ? JSON.stringify(plain) : plain
}

/** A source as an item of the list `sources:`, its name first. */
function itemText(settings: TermSourceSettings): string {
  return `${Object.entries(settings)
    .map(([key, value], i) => {
      const lead = i === 0 ? '- ' : '  '
      if (Array.isArray(value)) return `${lead}${key}: [${value.map(scalar).join(', ')}]`
      if (value !== null && typeof value === 'object') {
        return [`${lead}${key}:`, ...Object.entries(value).map(([k, v]) => `    ${scalar(k)}: ${scalar(v)}`)].join('\n')
      }
      return `${lead}${key}: ${scalar(value)}`
    })
    .join('\n')}\n`
}

/** A scalar read back: quoted, a list in brackets, null, a boolean, a number, or the plain string. */
function readScalar(raw: string): unknown {
  const value = raw.replace(/\s+#.*$/, '').trim()
  if (value.startsWith('"')) return JSON.parse(value)
  if (value.startsWith("'")) return value.slice(1, -1).replaceAll("''", "'")
  if (value.startsWith('[')) return value.slice(1, -1).split(',').map((part) => part.trim()).filter(Boolean).map(readScalar)
  if (/^(?:null|~)?$/.test(value)) return null
  if (/^(?:true|false)$/i.test(value)) return value.toLowerCase() === 'true'
  if (/^-?\d+$/.test(value)) return Number(value)
  return value
}

const FIELD = /^(\s*)("[^"]*"|'[^']*'|[^\s:#][^:#]*?):(?:\s+(.*))?$/

/** The sources a YAML text sets, read as the server would (400 in its words: where, never what), in the subset the
 * mock writes and a person types: an item a field a line, a map or a list under a field, a list in brackets. */
function readSources(given: string): TermSourceSettings[] {
  const lines = given.replace(/\r\n/g, '\n').split('\n')
  const open = lines.findIndex((line) => /\[[^\]]*$/.test(line.replace(/\s+#.*$/, '')))
  if (open >= 0) throw refused(400, `not valid YAML (line ${open + 1}, column ${(lines[open] ?? '').lastIndexOf('[') + 1})`)
  const start = lines.findIndex((line) => /^sources:\s*(?:\[\s*\])?\s*(?:#.*)?$/.test(line))
  if (start < 0 || lines.slice(0, start).some((line) => line.trim() && !line.trim().startsWith('#'))) throw refused(400, TEXT)
  const items: { indent: number; lines: string[] }[] = []
  for (const line of lines.slice(start + 1)) {
    if (!line.trim() || line.trim().startsWith('#')) continue
    const dash = /^(\s*)- (.*)$/.exec(line)
    const outer = items[0]?.indent ?? (dash ? (dash[1] ?? '').length : -1)
    if (dash && (dash[1] ?? '').length === outer) items.push({ indent: outer, lines: [`${dash[1]}  ${dash[2]}`] })
    else if (items.length && /^\s/.test(line)) items.at(-1)!.lines.push(line)
    else throw refused(400, `not valid YAML (line ${lines.indexOf(line) + 1}, column 1)`)
  }
  const sources = items.map(({ lines: item }, n) => {
    const settings: Record<string, unknown> = {}
    const indent = item[0]!.length - item[0]!.trimStart().length
    let nested: Record<string, unknown> | unknown[] | null = null
    for (const line of item) {
      const depth = line.length - line.trimStart().length
      const field = FIELD.exec(line)
      if (depth > indent && nested !== null) {
        if (Array.isArray(nested) && line.trimStart().startsWith('- ')) nested.push(readScalar(line.trimStart().slice(2)))
        else if (!Array.isArray(nested) && field) nested[String(readScalar(field[2]!))] = readScalar(field[3] ?? '')
        else throw refused(400, `not changed: term_sync.sources.${n}: a field is \`name: value\``)
        continue
      }
      if (depth !== indent || !field) throw refused(400, `not changed: term_sync.sources.${n}: a field is \`name: value\``)
      const key = String(readScalar(field[2]!))
      if (field[3] === undefined || field[3].trim() === '') {
        const next = item[item.indexOf(line) + 1]?.trimStart() ?? ''
        nested = next.startsWith('- ') ? [] : {}
        settings[key] = nested
      } else {
        nested = null
        settings[key] = readScalar(field[3])
      }
    }
    const { name, kind } = settings
    if (typeof name !== 'string' || !NAME.test(name)) throw refused(400, `not changed: term_sync.sources.${n}.name: lowercase letters, digits and hyphens, 40 at most`)
    if (!KINDS.includes(kind as (typeof KINDS)[number])) throw refused(400, `not changed: term_sync.sources.${n}: a source's kind is file, url, graph or ldap`)
    return settings as TermSourceSettings
  })
  if (new Set(sources.map((s) => s.name)).size !== sources.length) throw refused(400, 'not changed: term_sync.sources: each source has a name of its own')
  return sources
}

/** The same settings (or the same value of a field), whatever the order of their fields. */
function same(a: unknown, b: unknown): boolean {
  const sorted = (value: unknown): unknown =>
    Array.isArray(value) ? value.map(sorted)
      : value !== null && typeof value === 'object' ? Object.fromEntries(Object.entries(value).sort(([x], [y]) => x.localeCompare(y)).map(([k, v]) => [k, sorted(v)]))
        : value
  return JSON.stringify(sorted(a)) === JSON.stringify(sorted(b))
}

// --- the state --------------------------------------------------------------------------------------------------------

const total = (terms: Record<string, number>) => Object.values(terms).reduce((a, b) => a + b, 0)
const goneOf = (k: Kept) => gone.filter((term) => term.source === k.settings.name).length + k.retired

/** A sync asked for, run: the directory's sign-in refused until its secret is set again from the page; a source that
 * reads with a secret the mock no longer keeps, not run; any other, its terms as they are (its first ones, once). */
function runNow(k: Kept): void {
  const now = Date.now() / 1000
  const { name } = k.settings
  let failure: [string, string] | null = null
  if (needsSecret(k.settings) && !secrets.has(name)) {
    failure = ['no-secret', `its secret is not set for where it goes and what it reads now: \`latet server terms secret ${name}\` sets it`]
  } else if (name === 'directory' && !setHere.has(name)) failure = ['refused', REFUSED_SIGN_IN]
  const first = !failure && total(k.terms) === 0
  if (first) k.terms = FIRST_RUN[name] ?? Object.fromEntries([...new Set(Object.values(k.settings.mapping ?? {}))].map((type) => [type, 12]))
  k.last = ran(k.settings, now, k.terms, failure, first ? total(k.terms) : 0)
  k.asked = null
}

function view(k: Kept): TermSyncSource {
  const { name, kind, every_minutes } = k.settings
  const every = every_minutes ?? 60
  return {
    name,
    kind: k.configured ? kind : null,
    every_minutes: k.configured ? every : null,
    configured: k.configured,
    terms: total(k.terms),
    by_type: { ...k.terms },
    gone: goneOf(k),
    asked: k.asked,
    last: k.last && { ...k.last },
    next: k.configured && k.last ? k.last.finished + every * 60 : null,
  }
}

function state(): TermSyncState {
  for (const k of kept) if (k.configured && k.asked !== null) runNow(k)
  // The sources set up, in the section's order, then those it no longer sets whose terms the server keeps.
  const listed = [...kept.filter((k) => k.configured), ...kept.filter((k) => !k.configured && goneOf(k) > 0).sort((a, b) => a.settings.name.localeCompare(b.settings.name))]
  const terms = listed.reduce((sum, k) => sum + total(k.terms) + goneOf(k), 0)
  return {
    licensed: true,
    paused: PAUSED,
    masked: terms,
    kept: terms,
    most: MOST,
    gone: listed.reduce((sum, k) => sum + goneOf(k), 0),
    unreadable: 0,
    behind: null,
    problems: [],
    sources: listed.map(view),
  }
}

// --- the changes ------------------------------------------------------------------------------------------------------

/** What a change the security role makes meets first: the company's sign-in (before the body is read), then the
 * license (E1's gate). */
function mayChange(): void {
  if (CODE_SESSION) throw refused(403, CHANGES_BY_SSO, 'company-sign-in')
  if (TRIAL_ENDED) throw refused(403, TRIAL, 'trial-ended')
  if (PAUSED) throw refused(403, NOT_MANAGEABLE, 'not-manageable')
}

/** The etag a person's change names, read with its body: 400 without one. */
function etagGiven(data: Record<string, unknown>): void {
  if (!('etag' in data)) throw refused(400, ETAG_NEEDED)
  if (typeof data.etag !== 'string' || !data.etag) throw refused(400, 'etag is the etag of the sources the change was made from, as they were read')
}

/** In the change: 409 sources-changed when it names another etag than the sources' now. */
function unchanged(data: Record<string, unknown>): void {
  if (data.etag !== etag()) throw refused(409, SOURCES_CHANGED, 'sources-changed')
}

function saveSources(data: Record<string, unknown>) {
  if (Object.keys(data).some((key) => !['text', 'etag'].includes(key)) || typeof data.text !== 'string') throw refused(400, SOURCES_BODY)
  etagGiven(data)
  const given = data.text
  const next = readSources(given)
  unchanged(data)
  const before = new Map(kept.filter((k) => k.configured).map((k) => [k.settings.name, k.settings]))
  // A source at the server's own computer: neither added, changed nor taken out from the page, nor kept under its name
  // for a source elsewhere or of another kind: the page keeps it exactly as it is.
  const moved = [...before.values()].some((s) => localSource(s) && !same(next.find((n) => n.name === s.name), s))
  if (moved || next.some((s) => localSource(s) && !same(before.get(s.name), s))) throw refused(403, BY_COMMAND, 'by-command')
  const added = next.filter((s) => !before.has(s.name)).map((s) => s.name)
  const removed = [...before.keys()].filter((name) => !next.some((s) => s.name === name))
  const changed = next.filter((s) => before.has(s.name) && !same(before.get(s.name), s))
  const fields = Object.fromEntries(
    changed.map((s) => {
      const was = before.get(s.name)!
      return [s.name, [...new Set([...Object.keys(was), ...Object.keys(s)])].filter((key) => !same(was[key], s[key])).sort()]
    }),
  )
  // A source moved, or made to read other data, with its secret: kept for where it went, it must be set again.
  const problems = changed
    .filter((s) => needsSecret(s) && secrets.has(s.name) && fields[s.name]!.some((field) => !['every_minutes', 'exclude', 'min_length'].includes(field)))
    .map((s) => `${s.name}: its secret must be set again (\`latet server terms secret ${s.name}\`): it is kept for where the source went and what it read`)
  for (const s of changed) if (problems.some((line) => line.startsWith(`${s.name}:`))) secrets.delete(s.name)
  kept = [
    ...next.map((settings) => {
      const was = kept.find((k) => k.settings.name === settings.name)
      if (!was) return { settings, configured: true, terms: {}, retired: 0, last: null, asked: null }
      // Changed, or set up again: its last run, of the source it was, forgotten (it runs as it is now).
      const forgotten = was.configured && same(was.settings, settings) ? {} : { last: null, asked: null }
      return { ...was, settings, configured: true, retired: was.configured ? was.retired : 0, terms: was.configured ? was.terms : {}, ...forgotten }
    }),
    // Taken out: its terms no longer in the source, masked until someone removes them; its last run forgotten.
    ...kept.filter((k) => !next.some((s) => s.name === k.settings.name)).map((k) => (k.configured ? { ...k, configured: false, retired: k.retired + total(k.terms), terms: {}, asked: null, last: null } : k)),
  ]
  text = given
  version += 1
  return { added, removed, changed: changed.map((s) => s.name), fields, problems, etag: etag() }
}

function secret(method: Method, name: string, data: Record<string, unknown>) {
  if (method === 'PUT') {
    if (!('value' in data) || Object.keys(data).some((key) => !['value', 'etag'].includes(key))) throw refused(400, SECRET_BODY)
    etagGiven(data)
    if (typeof data.value !== 'string' || !data.value.trim()) throw refused(400, 'a secret is not empty')
  } else {
    if (Object.keys(data).some((key) => key !== 'etag')) throw refused(400, REMOVAL_BODY)
    etagGiven(data)
  }
  unchanged(data)
  const source = kept.find((k) => k.configured && k.settings.name === name)
  if (method === 'PUT') {
    if (!source) throw refused(404, NO_SOURCE)
    if (!needsSecret(source.settings)) throw refused(409, NEEDS_NONE)
    if (localSource(source.settings)) throw refused(403, CREDENTIAL_BY_COMMAND, 'by-command')
    secrets.add(name) // what it is, the mock never keeps
    setHere.add(name)
    source.asked = Date.now() / 1000 // a sync of it asked for in the same change
    return { saved: name }
  }
  if (source && localSource(source.settings)) throw refused(403, CREDENTIAL_BY_COMMAND, 'by-command')
  if (!secrets.delete(name)) throw refused(404, NO_SECRET)
  setHere.delete(name)
  return { removed: name }
}

/** `/ee/term-sync…` in mock mode for the security role (?mock-server&mock-sign-in&mock-security); undefined for any
 * other path, for anyone else, and on the mock's computer (R31). */
export function routeTermSync(method: Method, path: string, body: unknown): unknown {
  if (!SECURITY_MOCK) return undefined
  const p = new URL(path, 'http://mock').pathname
  const data = (body ?? {}) as Record<string, unknown>
  if (method === 'GET' && p === '/ee/term-sync') return structuredClone(state())
  if (method === 'GET' && p === '/ee/term-sync/gone') {
    if (CODE_SESSION) throw refused(403, TERMS_BY_COMPANY, 'company-sign-in')
    const items = [...gone].sort((a, b) => b.gone - a.gone || a.id.localeCompare(b.id))
    return structuredClone({ items, total: items.length })
  }
  if (method === 'GET' && p === '/ee/term-sync/sources') {
    if (CODE_SESSION) throw refused(403, SOURCES_BY_COMPANY, 'company-sign-in')
    const shown = kept.filter((k) => k.configured).map((k) => k.settings)
    return structuredClone({ sources: shown, text, etag: etag(), where: Object.fromEntries(shown.map((s) => [s.name, whereOf(s)])) })
  }
  const secretOf = p.match(/^\/ee\/term-sync\/secrets\/([^/]+)$/)
  const known = (method === 'POST' && (p === '/ee/term-sync/run' || p === '/ee/term-sync/remove')) || (method === 'PUT' && p === '/ee/term-sync/sources') || ((method === 'PUT' || method === 'DELETE') && secretOf)
  if (!known) return undefined
  mayChange()
  if (p === '/ee/term-sync/run') {
    const source = data.source ?? null
    if (Object.keys(data).some((key) => key !== 'source') || (source !== null && typeof source !== 'string')) throw refused(400, RUN_BODY)
    const picked = kept.filter((k) => k.configured && (source === null || k.settings.name === source))
    if (picked.length === 0) throw refused(404, NO_SOURCE)
    const now = Date.now() / 1000
    for (const k of picked) k.asked = now
    return { asked: picked.map((k) => k.settings.name) }
  }
  if (p === '/ee/term-sync/remove') {
    const ids = data.ids
    if (Object.keys(data).length !== 1 || !Array.isArray(ids) || ids.length > 1000 || !ids.every((id) => typeof id === 'string' && ID.test(id))) throw refused(400, REMOVE_BODY)
    const removed = gone.filter((term) => ids.includes(term.id))
    gone = gone.filter((term) => !ids.includes(term.id))
    const byType: Record<string, number> = {}
    for (const term of removed) byType[term.type] = (byType[term.type] ?? 0) + 1
    return { removed: removed.length, by_type: byType }
  }
  if (p === '/ee/term-sync/sources') return saveSources(data)
  return secret(method, decodeURIComponent(secretOf![1] ?? ''), data)
}
