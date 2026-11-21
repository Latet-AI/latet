// Counts-only synthetic Reports and SIEM fixtures, with the same scope and export gates as the server.
import { ApiError, currentSpace } from '../api/client'
import type { Me } from '../api/types'
import { isoDate, rangeProblem } from '../ee/lib/reports'
import { bindingOf, localOutput } from '../ee/lib/siem'
import type { AuditEntryView, AuditLogView, Paused, Report, ReportCounts, ReportFile, ReportPeople, ReportRow, SiemOutputSettings, SiemOutputView, SiemView } from '../ee/types'

const flags = new URLSearchParams(typeof window === 'undefined' ? '' : window.location.search)
const ON_SERVER = flags.has('mock-server') || flags.has('mock-server-key')
const SECURITY = flags.has('mock-security')
const AUDITOR = flags.has('mock-auditor')
const LEAD = flags.has('mock-group-lead')
const DAY = 86_400_000
const GROUPS = [{ id: 'g_paymentsaa', name: 'Payments' }, { id: 'g_platformaa', name: 'Platform' }, { id: 'g_dataaaaaaa', name: 'Data' }]
const PROJECTS = flags.has('mock-reports-overflow') ? Array.from({ length: 205 }, (_, n) => `project-${String(n).padStart(3, '0')}`) : ['billing-api', 'web-shop', 'data-pipeline', flags.has('mock-reports-formula') ? '=SUM(1,2)' : 'internal-tools']
const AGENTS = ['claude-code', 'codex', 'opencode', 'apps']
const CATEGORIES = ['EMAIL', 'PERSON', 'CLIENT_NAME', 'AWS_ACCESS_KEY', 'IBAN', 'PHONE']
const ZERO: ReportCounts = { exchanges: 0, stopped: 0, masked: 0 }
const sum = (rows: readonly ReportCounts[]) => rows.reduce((a, r) => ({ exchanges: a.exchanges + r.exchanges, stopped: a.stopped + r.stopped, masked: a.masked + r.masked }), { ...ZERO })
const dayOf = (text: string) => Date.parse(`${text}T00:00:00Z`) / DAY
const iso = (day: number) => new Date(day * DAY).toISOString().slice(0, 10)
const groupName = (id: string) => GROUPS.find(g => g.id === id)?.name ?? ''
const denied = () => { throw new ApiError(403, 'this view is not available to this person') }
const keySpace = () => currentSpace() !== null || flags.has('mock-server-key')
const peopleMode: ReportPeople = flags.get('mock-reports-people') === 'named' ? 'named' : flags.get('mock-reports-people') === 'pseudonymous' ? 'pseudonymous' : 'group-only'

function pause(): Paused | null {
  const asked = flags.get('mock-reports-paused')
  if (asked && ['expired', 'users', 'trial-ended', 'unusable'].includes(asked)) return { why: asked as Paused['why'], expires: null }
  const license = flags.get('mock-license')
  if (license === 'paused') return { why: 'expired', expires: '2026-08-31' }
  if (license === 'trial-ended') return { why: 'trial-ended', expires: '2026-10-10' }
  if (license === 'invalid' || license === 'none') return { why: 'unusable', expires: null }
  return null
}
function producing() {
  const paused = pause()
  if (paused) throw new ApiError(403, 'Reports are paused', [], { code: 'not-producing', paused })
}
function company() {
  if (flags.has('mock-code-session') && flags.has('mock-sign-in')) {
    throw new ApiError(403, 'sign in with your company account to export', [], { code: 'company-sign-in' })
  }
}

/** Auditor and group-lead navigation use the same role selection as these routes. */
export function withReports(me: Me): Me {
  if (!ON_SERVER || !flags.has('mock-sign-in') || me.edition?.name !== 'enterprise' || keySpace()) return me
  let current = me
  if (!SECURITY && (AUDITOR || LEAD)) current = { ...me, edition: { ...me.edition, pages: AUDITOR ? ['sign-in', 'reports', 'audit', 'siem'] : ['sign-in', 'reports'], roles: [AUDITOR ? 'auditor' : 'group-lead', 'user'], actions: AUDITOR ? ['reports.group', 'reports.all', 'audit.read', 'policy.read'] : ['reports.group'] } }
  if (flags.has('mock-siem-only')) current = { ...current, edition: { ...current.edition!, features: ['siem'], pages: current.edition!.pages.filter(page => !['reports', 'audit'].includes(page)) } }
  return current
}

interface Counted extends ReportCounts {
  day: string; group: string; kind: 'person' | 'key'; name: string; identity: string; project: string; agent: string; by_category: Record<string, number>
}
function selected(q: URLSearchParams, today: Date) {
  const to = q.get('to') || isoDate(today)
  if (rangeProblem(to, to)) throw new ApiError(400, 'invalid report dates')
  const from = q.get('from') || iso(dayOf(to) - 29)
  if (rangeProblem(from, to)) throw new ApiError(400, 'use real dates from 2000 through 2999, in order, at most 366 days')
  const group = q.get('group') || null
  if (group && !/^g_[a-z2-7]{10}$/.test(group)) throw new ApiError(400, 'group is not a group id')
  if (LEAD && !SECURITY && !AUDITOR && group && group !== GROUPS[0]!.id) denied()
  for (const key of ['project', 'agent', 'category']) {
    const value = q.get(key)
    if (value && (value.length > 200 || [...value].some(char => char.charCodeAt(0) < 32 || (char.charCodeAt(0) >= 127 && char.charCodeAt(0) <= 159)))) throw new ApiError(400, 'invalid report filter')
  }
  if (q.get('category') && !/^[A-Z][A-Z0-9_]{0,63}$/.test(q.get('category')!)) throw new ApiError(400, 'invalid category code')
  return { from, to, days: dayOf(to) - dayOf(from) + 1, step: dayOf(to) - dayOf(from) + 1 > 92 ? 'week' as const : 'day' as const }
}
const bucket = (day: number, weekly: boolean) => iso(weekly ? day - ((new Date(day * DAY).getUTCDay() + 6) % 7) : day)

function records(period: Report['period']): Counted[] {
  const rows: Counted[] = []
  for (let d = dayOf(period.from); d <= dayOf(period.to); d++) {
    for (let p = 0; p < PROJECTS.length; p++) {
      for (let person = 0; person < 5; person++) {
        const key = person >= 3
        const agent = key ? 'apps' : AGENTS[(p + person) % 3]!
        const weekend = [0, 6].includes(new Date(d * DAY).getUTCDay())
        const exchanges = Math.round((weekend ? 3 : 12) * (1 + ((d + p + person) % 5) / 10))
        const masked = Math.round(exchanges * 0.7), stopped = key ? 0 : (d + p + person) % 7 === 0 ? 1 : 0
        const category = CATEGORIES[(p + person) % CATEGORIES.length]!
        const name = key ? ['Billing CI', 'Release bot'][person - 3]! : peopleMode === 'group-only' ? '' : peopleMode === 'pseudonymous' ? `Person ${person + 1}` : ['Emily Carter', 'Daniel Okafor', 'Priya Shah'][person]!
        rows.push({ day: bucket(d, period.step === 'week'), group: key ? '' : GROUPS[person]!.id, kind: key ? 'key' : 'person', name, identity: key ? ['u_billingci2', 'u_releasebot'][person - 3]! : name, project: PROJECTS[p]!, agent, exchanges, stopped, masked, by_category: { [category]: masked } })
      }
    }
  }
  return rows
}
function scope(rows: Counted[], q: URLSearchParams, filters: boolean): Counted[] {
  return rows.filter(r => (!(LEAD && !SECURITY && !AUDITOR) || r.group === GROUPS[0]!.id) && (!q.get('group') || r.group === q.get('group')) && (!filters || ((!q.get('project') || r.project === q.get('project')) && (!q.get('agent') || r.agent === q.get('agent')))))
    .filter(r => !filters || !q.get('category') || (r.by_category[q.get('category')!] ?? 0) > 0)
    .map(r => q.get('category') && filters ? { ...r, masked: r.by_category[q.get('category')!] ?? 0, by_category: { [q.get('category')!]: r.by_category[q.get('category')!] ?? 0 } } : r)
}
function ranked(rows: Counted[], key: 'group' | 'project' | 'agent' | 'identity'): [ReportRow[], Report['others']['by_project']] {
  const buckets = new Map<string, ReportCounts>()
  for (const r of rows) buckets.set(r[key], sum([buckets.get(r[key]) ?? ZERO, r]))
  const all = [...buckets].map(([value, counts]) => ({ key: value, label: key === 'group' ? groupName(value) || null : key === 'identity' ? rows.find(r => r.identity === value)?.name || null : value || null, ...counts }))
    .sort((a, b) => b.exchanges - a.exchanges || b.masked - a.masked || a.key.localeCompare(b.key))
  const shown = all.slice(0, 200), hidden = all.slice(200)
  return [shown, { count: hidden.filter(r => r.key).length, ...sum(hidden) }]
}

export function mockReport(q: URLSearchParams, today: Date): Report {
  const period = selected(q, today)
  const current = records(period)
  const within = scope(current, q, true)
  const offered = scope(current, q, false)
  // Decisions and application calls describe operations, independent of the selected value type.
  const operationsQuery = new URLSearchParams(q)
  operationsQuery.delete('category')
  const operations = scope(current, operationsQuery, true)
  const operationTotal = sum(operations)
  const totals = sum(within)
  const [by_group, groups] = ranked(within, 'group'), [by_project, projects] = ranked(within, 'project'), [by_agent, agents] = ranked(within, 'agent')
  const [by_key, keys] = ranked(within.filter(r => r.kind === 'key'), 'identity')
  const [persons, people] = ranked(within.filter(r => r.kind === 'person'), 'identity')
  const series = new Map<string, ReportCounts>()
  for (let d = dayOf(period.from); d <= dayOf(period.to); d++) series.set(bucket(d, period.step === 'week'), { ...ZERO })
  for (const r of within) series.set(r.day, sum([series.get(r.day) ?? ZERO, r]))
  const cats: Record<string, number> = {}
  for (const r of within) for (const [key, value] of Object.entries(r.by_category)) cats[key] = (cats[key] ?? 0) + value
  const apiCalls = operations.filter(r => r.kind === 'key').reduce((n, r) => n + r.exchanges, 0)
  const approvals = { approved: Math.floor(operationTotal.exchanges / 40), rejected: Math.floor(operationTotal.exchanges / 400) }
  const choices = (key: 'project' | 'agent' | 'group') => ranked(offered, key)[0].map(r => r.key).filter(Boolean).sort()
  const previous = { from: iso(dayOf(period.from) - period.days), to: iso(dayOf(period.from) - 1) }
  return {
    period, previous, filters: { group: q.get('group'), project: q.get('project'), agent: q.get('agent'), category: q.get('category') }, people: peopleMode,
    totals: { ...totals, approvals, changes: {}, apps: apiCalls ? { mask: apiCalls } : {}, uncounted: 0 },
    before: sum(scope(records({ ...period, ...previous }), q, true)), series: [...series].map(([key, counts]) => ({ key, ...counts })),
    by_group, by_project, by_agent, by_key, by_person: peopleMode === 'group-only' ? null : persons, by_approver: peopleMode === 'group-only' ? null : [],
    others: { by_group: groups, by_project: projects, by_agent: agents, by_key: keys, by_person: peopleMode === 'group-only' ? null : people, by_approver: peopleMode === 'group-only' ? null : { count: 0, approvals: approvals.approved + approvals.rejected, changes: 0 } },
    by_category: Object.entries(cats).map(([key, masked]) => ({ key, masked })), stopped_by_reason: totals.stopped ? [{ key: 'leak', count: totals.stopped }] : [],
    options: { groups: choices('group').map(id => ({ id, name: groupName(id) })), projects: choices('project'), agents: choices('agent'), categories: [...new Set(offered.flatMap(r => Object.keys(r.by_category)))].sort() },
    viewer: { groups: LEAD && !SECURITY && !AUDITOR ? [GROUPS[0]!.id] : null, people: peopleMode, siem: SECURITY ? 'edit' : AUDITOR ? 'read' : null },
  }
}

function mockExport(q: URLSearchParams, today: Date): ReportFile {
  const format = q.get('format') || 'csv'
  if (!['csv', 'json'].includes(format)) throw new ApiError(400, 'format is csv or json')
  const report = mockReport(q, today)
  const raw = scope(records(report.period), q, true)
  const merged = new Map<string, Counted>()
  for (const row of raw) {
    const id = JSON.stringify([row.day, row.group, row.kind, row.name, row.project, row.agent])
    const was = merged.get(id)
    if (!was) merged.set(id, { ...row, by_category: { ...row.by_category } })
    else { Object.assign(was, sum([was, row])); for (const [key, n] of Object.entries(row.by_category)) was.by_category[key] = (was.by_category[key] ?? 0) + n }
  }
  const rows = [...merged.values()].map(r => ({ day: r.day, group: groupName(r.group), kind: r.kind, name: r.name, project: r.project, agent: r.agent, exchanges: r.exchanges, stopped: r.stopped, masked: r.masked, by_category: r.by_category }))
  const categories = [...new Set(rows.flatMap(r => Object.keys(r.by_category)))].sort()
  const name = `latet-report-${report.period.from}-${report.period.to}.${format}`
  if (format === 'json') {
    const admin = SECURITY || AUDITOR
    const administration = [{ id: 1, ts: `${report.period.from}T10:00:00+00:00`, actor: { id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' }, action: 'policy.changed', target: null, details: {}, address: flags.has('mock-admin') ? '192.0.2.10' : null }]
    return { name, type: 'application/json', file: JSON.stringify({ period: report.period, filters: report.filters, people: report.people, totals: report.totals, before: report.before, categories, rows,
      ...(admin ? flags.has('mock-reports-truncated') ? { administration_truncated: true } : { administration } : {}),
    }, null, 1) }
  }
  const cell = (value: string | number) => {
    let text = String(value)
    if (typeof value === 'string' && /^[=+@\-\t\r]/.test(text)) text = `'${text}`
    return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
  }
  const lines = [['day', 'group', 'kind', 'name', 'project', 'agent', 'exchanges', 'stopped', 'masked', ...categories],
    ...rows.map(r => [r.day, r.group, r.kind, r.name, r.project, r.agent, r.exchanges, r.stopped, r.masked, ...categories.map(c => r.by_category[c] ?? 0)])]
  return { name, type: 'text/csv', file: `\uFEFF${lines.map(r => r.map(cell).join(',')).join('\r\n')}\r\n` }
}

/** Synthetic administration metadata only; every actor type is distinct from a human administrator. */
function auditEntries(today: Date): AuditEntryView[] {
  const at = (hours: number) => new Date(today.getTime() - hours * 3_600_000).toISOString().replace(/\.000Z$/, 'Z')
  const emily = { id: 'u_emilycartr', name: 'Emily Carter', kind: 'person' }
  const token = { id: 'admin:3f9a', name: null, kind: 'token' }
  const rows: AuditEntryView[] = [
    { id: 40, ts: at(80), action: 'person.added', actor: { id: 'scim:s_7hq2m4pzxa', name: null, kind: 'scim' }, target: { id: 'u_priyashahx', name: flags.has('mock-audit-formula') ? '=SUM(1,2)' : 'Priya Shah' }, details: {}, address: '10.0.0.9' },
    { id: 41, ts: at(70), action: 'person.added', actor: token, target: { id: 'u_tombeckerx', name: 'Tom Becker' }, details: {}, address: '10.0.0.7' },
    { id: 42, ts: at(52), action: 'group.added', actor: emily, target: { id: 'g_platformxx', name: 'Platform' }, details: {}, address: '10.0.0.8' },
    { id: 43, ts: at(51), action: 'role.granted', actor: emily, target: { id: 'u_laurachenx', name: 'Laura Chen' }, details: { role: 'auditor' }, address: '10.0.0.8' },
    { id: 44, ts: at(26), action: 'key.added', actor: token, target: { id: 'u_billingcix', name: 'Billing CI' }, details: {}, address: '10.0.0.7' },
    { id: 45, ts: at(3), action: 'siem.changed', actor: { id: 'u_danielokfr', name: 'Daniel Okafor', kind: 'person' }, target: { id: 'siem', name: null }, details: { section: 'siem', outputs: ['splunk', 'elastic'] }, address: '10.0.0.12' },
    { id: 46, ts: at(2), action: 'apps.forgotten', actor: { id: 'u_billingcix', name: 'Billing CI', kind: 'key' }, target: { id: 'u_billingcix', name: 'Billing CI' }, details: { forgotten: 2, unknown: 0 }, address: '10.0.0.10' },
    { id: 47, ts: at(1), action: 'calls.refused', actor: { id: 'unknown', name: null, kind: 'unknown' }, target: null, details: { count: 1, reason: 'busy' }, address: null },
    { id: 48, ts: at(0.5), action: 'siem.skipped', actor: token, target: { id: 'siem', name: null }, details: { output: 'elastic', from: null, to: null, count: 0, dropped: 4 }, address: '10.0.0.7' },
    { id: 49, ts: at(0.25), action: 'siem.gap', actor: { id: 'server', name: null, kind: 'server' }, target: { id: 'elastic', name: null }, details: { output: 'elastic', from: 20, to: 22, count: 3 }, address: null },
  ]
  const count = Number(flags.get('mock-audit-count'))
  return Number.isSafeInteger(count) && count > 0 ? Array.from({ length: Math.min(count, 100001) }, (_, i) => ({ ...rows[i % rows.length]!, id: i + 1 })) : rows
}
function auditSelected(q: URLSearchParams, today: Date) {
  // Server-instance local dates. Date-only values never pass through UTC conversion.
  const to = q.get('to') || isoDate(today), start = new Date(`${to}T00:00:00`)
  start.setDate(start.getDate() - 29)
  const from = q.get('from') || isoDate(start), action = q.get('action') || '', actor = q.get('actor') || ''
  if (rangeProblem(from, to) || [...action].length > 200 || [...actor].length > 200) throw new ApiError(400, 'invalid audit period or filter')
  const addresses = !SECURITY && !AUDITOR && !LEAD
  const since = new Date(`${from}T00:00:00`), end = new Date(`${to}T00:00:00`); end.setDate(end.getDate() + 1)
  const entries = auditEntries(today).filter(e => {
    const time = Date.parse(e.ts!)
    return time >= since.getTime() && time < end.getTime() && (!actor || e.actor.id === actor)
      && (!action || (action.endsWith('*') ? e.action.startsWith(action.slice(0, -1)) : e.action === action))
  }).map(e => addresses ? e : { ...e, address: null }).sort((a, b) => a.id - b.id)
  return { period: { from, to }, entries, addresses }
}
function mockAudit(q: URLSearchParams, today: Date): AuditLogView {
  const all = auditSelected(q, today), entries = all.entries.slice(-5000)
  return { ...all, entries, truncated: all.entries.length > 5000, actions: [...new Set(entries.map(e => e.action))].sort(),
    actors: [...new Map(entries.map(e => [e.actor.id, e.actor])).values()].sort((a, b) => (a.name || a.id).toLowerCase().localeCompare((b.name || b.id).toLowerCase())) }
}
function mockAuditExport(q: URLSearchParams, today: Date): ReportFile {
  const format = q.get('format') || 'csv'
  if (format !== 'csv' && format !== 'json') throw new ApiError(400, 'format is csv or json')
  const { period, entries } = auditSelected(q, today)
  if (entries.length > 100000) throw new ApiError(413, 'narrow the period or filters: the audit export holds at most 100000 entries')
  const name = `latet-audit-${period.from}-${period.to}.${format}`
  if (format === 'json') return { name, type: 'application/json', file: JSON.stringify({ period, entries }, null, 1) }
  const cell = (value: string | null | undefined) => {
    let text = value ?? ''
    if (/^[=+@\-\t\r]/.test(text)) text = `'${text}`
    return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
  }
  // Python json.dumps(sort_keys=True)'s spacing, including nested objects and arrays.
  const jsonText = (value: unknown): string => Array.isArray(value) ? `[${value.map(jsonText).join(', ')}]`
    : value !== null && typeof value === 'object' ? `{${Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, item]) => `${JSON.stringify(key)}: ${jsonText(item)}`).join(', ')}}`
    : JSON.stringify(value)
  const details = (value: Record<string, unknown>) => Object.keys(value).length ? jsonText(value) : ''
  const lines = [['time', 'actor', 'actor_name', 'action', 'target', 'target_name', 'details', 'address'], ...entries.map(e => [e.ts, e.actor.id, e.actor.name, e.action, e.target?.id, e.target?.name, details(e.details), e.address])]
  return { name, type: 'text/csv', file: `\uFEFF${lines.map(line => line.map(cell).join(',')).join('\r\n')}\r\n` }
}

const SINCE = Date.now() / 1000
const empty = { queued: 0, dropped: 0, last_ok: null, last_error: null, failing_since: null, blocked: false, held: false, collapsed: 0 }
let outputs: SiemOutputView[] = [
  { ...empty, name: 'splunk', kind: 'splunk', url: 'https://splunk.acme.com:8088/services/collector/event', headers: {}, secret: 'siem-splunk', format: 'json', has_secret: true, secret_destination_changed: false, last_ok: SINCE - 40 },
  { ...empty, name: 'elastic', kind: 'webhook', url: 'https://elastic.acme.com:9880/latet', headers: { Authorization: 'ApiKey {secret}' }, secret: 'siem-elastic', format: 'json', has_secret: true, secret_destination_changed: false, queued: 1203, held: true, last_ok: SINCE - 7200, last_error: 'it could not be reached', failing_since: SINCE - 5400 },
  { ...empty, name: 'syslog', kind: 'syslog', url: 'tls://syslog.acme.com:6514', headers: {}, secret: null, format: 'json', has_secret: false, secret_destination_changed: false, last_ok: SINCE - 60 },
  ...(flags.has('mock-siem-local') ? [{ ...empty, name: 'local', kind: 'webhook' as const, url: 'http://localhost:9880/in', headers: {}, secret: null, format: 'json' as const, has_secret: false, secret_destination_changed: false, queued: 9 }] : []),
]
// No secret values are retained. The fixture keeps only which destination/header binding was supplied.
function binding(output: SiemOutputSettings): string {
  const result = bindingOf(output)
  if (result === null) throw new ApiError(400, 'invalid output address')
  return result
}
let bindings = new Map(outputs.filter(o => o.secret !== null).map(o => [o.name, binding(o)]))
let etag = 1
function siemView(): SiemView {
  return { outputs: outputs.map(o => {
    const saved = o.secret ? bindings.get(o.name) : undefined
    const current = { ...o, headers: { ...o.headers }, has_secret: saved === binding(o), secret_destination_changed: saved !== undefined && saved !== binding(o) }
    if (SECURITY) return current
    const url = new URL(o.url)
    return { ...current, url: `${url.protocol}//${url.host}${!['', '/'].includes(url.pathname) || url.search ? '/…' : ''}`, headers: Object.fromEntries(Object.keys(o.headers).map(key => [key, '…'])) }
  }), etag: SECURITY ? String(etag) : null, editable: SECURITY && !pause(), paused: pause() }
}

export function routeReports(method: string, path: string, body: unknown, q: URLSearchParams, today: Date): unknown {
  if (!ON_SERVER || !['/ee/reports', '/ee/reports/export', '/ee/audit', '/ee/audit/export', '/ee/siem', '/ee/siem/secret', '/ee/siem/test'].includes(path)) return undefined
  if (keySpace() || !flags.has('mock-sign-in')) denied()
  if (path.startsWith('/ee/audit')) {
    if (flags.has('mock-siem-only') || (LEAD && !SECURITY && !AUDITOR)) denied()
    producing()
    if (path.endsWith('/export')) company()
    const status = Number(flags.get('mock-audit-error'))
    if ([403, 413, 429].includes(status)) throw new ApiError(status, 'the audit operation was refused')
    return method === 'GET' ? path.endsWith('/export') ? mockAuditExport(q, today) : mockAudit(q, today) : undefined
  }
  if (path.startsWith('/ee/reports')) {
    if (flags.has('mock-siem-only')) denied()
    if (!SECURITY && !AUDITOR && !LEAD) denied()
    producing()
    if (path.endsWith('/export')) company()
    const status = Number(flags.get('mock-reports-error'))
    if ([403, 413, 429].includes(status)) throw new ApiError(status, 'the report could not be made')
    if (method === 'GET') return path.endsWith('/export') ? mockExport(q, today) : mockReport(q, today)
    return undefined
  }
  if (!SECURITY && !AUDITOR) denied()
  if (method === 'GET' && path === '/ee/siem') return siemView()
  if (!SECURITY) denied()
  producing(); company()
  const status = Number(flags.get('mock-siem-error'))
  if ([403, 413, 429].includes(status)) throw new ApiError(status, 'the SIEM operation was refused')
  if (method === 'PUT' && path === '/ee/siem') {
    const sent = body as { outputs?: SiemOutputSettings[]; secrets?: Record<string, string | null>; etag?: string }
    if (sent.etag !== undefined && sent.etag !== String(etag)) throw new ApiError(409, 'the SIEM outputs changed since they were read', [], { code: 'siem-changed' })
    if (!Array.isArray(sent.outputs) || sent.outputs.length > 50) throw new ApiError(400, 'invalid SIEM outputs')
    for (const settings of sent.outputs) {
      if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(settings.name) || (settings.secret !== null && settings.secret !== `siem-${settings.name}`)) throw new ApiError(400, 'invalid output secret name')
      if (localOutput(settings)) {
        const previous = outputs.find(o => o.name === settings.name)
        if (!previous || JSON.stringify([previous.kind, previous.url, previous.headers, previous.secret, previous.format]) !== JSON.stringify([settings.kind, settings.url, settings.headers, settings.secret, settings.format])) throw new ApiError(403, 'local outputs are changed with the command line')
      }
      try { binding(settings) } catch { throw new ApiError(400, 'invalid output address or headers') }
    }
    outputs = sent.outputs.map(settings => ({ ...empty, ...outputs.find(o => o.name === settings.name), ...structuredClone(settings), has_secret: false, secret_destination_changed: false }))
    bindings = new Map([...bindings].filter(([name]) => outputs.some(o => o.name === name && o.secret !== null)))
    for (const output of outputs) {
      const secret = sent.secrets?.[output.name]
      if (secret === null) bindings.delete(output.name)
      else if (typeof secret === 'string' && secret) {
        output.secret ??= `siem-${output.name}`
        bindings.set(output.name, binding(output))
      }
    }
    etag++; return siemView()
  }
  const sent = body as { name?: string; secret?: string | null }
  const output = outputs.find(o => o.name === sent?.name)
  if (!output) throw new ApiError(404, 'there is no SIEM output of that name')
  if (method === 'PUT' && path === '/ee/siem/secret') {
    if (sent.secret === null) bindings.delete(output.name)
    else if (typeof sent.secret === 'string' && sent.secret) {
      if (output.secret === null) { output.secret = `siem-${output.name}`; etag++ }
      bindings.set(output.name, binding(output))
    }
    else throw new ApiError(400, 'invalid output secret')
    return { ok: true }
  }
  if (method === 'POST' && path === '/ee/siem/test') {
    if (localOutput(output)) throw new ApiError(403, 'local outputs are tested with the command line')
    return { ok: !output.failing_since && (!output.secret || bindings.get(output.name) === binding(output)), detail: output.failing_since ? 'it could not be reached' : 'test finished' }
  }
  return undefined
}
