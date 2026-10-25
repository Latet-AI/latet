// Mock transport: the whole API in memory, with a live feed that produces a new exchange every few
// seconds. Loaded only by the dev server in mock mode (VITE_LATET_MOCK=1 or `npm run dev:mock`).

import { ApiError, NetworkError, notifyUnauthorized, UnauthorizedError, type Method, type StreamState, type Transport } from '../api/client'
import { defaultPii } from '../lib/pii'
import type { ConfigUpdate, ConfigView, Destination, ExchangeSummary, RecognizerEntry, ReviewRecord, Session, Stats, Term, VaultPage } from '../api/types'
import { hasFlag } from '../lib/flags'
import { dayRange } from '../lib/stats'
import { hasLetterOrDigit, TERM_LONGEST, termTooLong } from '../lib/terms'
import { addBatch, addChange, onDecided, resetQueue, routeApprovals, startApprovals, subscribeApprovals } from './approvals'
import {
  detailOf,
  initialConfig,
  initialExchanges,
  integrations,
  MOCK_GUIDANCE,
  MOCK_ASSETS,
  MOCK_PROMPT,
  nextLiveExchange,
  NOW,
  vault,
  type MockExchange,
} from './fixtures'
import { mockColumnOpinions, mockColumns, mockColumnShape } from './columns'
import { mockMask } from './playground'
import { dataEffective, personalOf, projectOfAgent, routeProjects, setPersonalOf, strictIn } from './projects'
import { mockTestRecognizer } from './recognizers'
import { seeded } from './rng'
import { MockTutorial, reviewRecordOf, sandboxBase } from './tutorial'

// Review the error screens: open the page with ?mock-401 or ?mock-offline (read once, at load,
// because the router rewrites the address on the first render).
const REVIEW = window.location.search
// ?mock-tutorial: the tutorial's sandbox, with its own empty history and settings and its chapters; no
// feed of exchanges coming in by themselves. ?mock-tutorial=terms opens that chapter from the menu,
// &mock-tutorial-fail makes each step fail once (the daemon's `error`), then run when tried again, and
// &mock-tutorial-no-ocr leaves the sandbox without your text recognition models: the screenshots chapter then
// says what it needs (`missing`) and does not start.
const TUTORIAL = REVIEW.includes('mock-tutorial')

/** An exchange with the project of its session (the mock gives each agent one). */
function inProject(exchange: MockExchange): MockExchange {
  return { ...exchange, summary: { ...exchange.summary, project: projectOfAgent(exchange.summary.agent) } }
}

let exchanges: MockExchange[] = TUTORIAL ? [] : initialExchanges.map(inProject)
let config: ConfigView = TUTORIAL ? sandboxBase(initialConfig) : structuredClone(initialConfig)
if (TUTORIAL) vault.entities.clear() // a sandbox knows no value yet
// ?mock-big-vault: 1,200 more addresses, a month old and a minute apart, to page through.
if (REVIEW.includes('mock-big-vault')) {
  for (let n = 0; n < 1200; n++) {
    vault.value('EMAIL', 'PII', 'pii:email', `client${n}@northwind-legal.com`, `person${n}@contoso.example`, NOW - 30 * 86400 - n * 60)
  }
}
let reviewerKeySaved = true
// Local name detection: ?mock-ner-active (installed and on), ?mock-ner-missing (extra not installed).
if (REVIEW.includes('mock-ner-active')) config.ner_status = { runtime: true, model: true, active: true }
if (REVIEW.includes('mock-ner-missing')) config.ner_status = { runtime: false, model: false, active: false }
// ?mock-server: a person's space on a Latet server, with the destinations its administrators set for everyone (a
// server sets no rule for everyone in the Community Edition); ?mock-server-key: an API key's space there.
const SERVER_KEY = REVIEW.includes('mock-server-key')
const SERVER = SERVER_KEY || REVIEW.includes('mock-server')
const MOCK_SHARED_DESTINATIONS: Destination[] = [{ name: 'llm-acme-com', url: 'https://llm.acme.com' }]
if (SERVER) {
  config.identity = { ...config.identity, hostname: 'emily-laptop', hostnames: ['emily-desktop'] }
}
const MOCK_SERVER = {
  url: 'https://latet.acme.lan',
  identity: SERVER_KEY
    ? { id: 'u_billingci2', name: 'Billing CI', kind: 'key' as const }
    : { id: 'u_emilycarte', name: 'Emily Carter', kind: 'person' as const },
}
// ?mock-stats-partial: the period holds more exchanges than one read of the stats counts.
const STATS_PARTIAL = REVIEW.includes('mock-stats-partial')

function nerActive(next: ConfigView): boolean {
  return next.ner === 'auto' && next.profile !== 'secrets' && next.ner_status.runtime && next.ner_status.model
}
let rulesVersion = 12
const listeners = new Set<(exchange: ExchangeSummary) => void>()
const reviewListeners = new Set<(review: ReviewRecord) => void>()

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

const tutorial = TUTORIAL
  ? new MockTutorial(
      {
        now: () => Date.now(),
        config: () => config,
        patchConfig: (patch) => {
          config = { ...config, ...patch }
          rulesVersion++
        },
        resetConfig: (patch) => {
          config = { ...sandboxBase(initialConfig), ...patch }
          rulesVersion++
        },
        history: {
          clear: () => {
            exchanges = []
          },
          add: (exchange) => {
            exchange.kept = config.history === 'metadata' ? 'metadata' : 'full'
            exchanges = [exchange, ...exchanges]
            for (const listener of listeners) listener(json(exchange.summary))
          },
        },
        queue: { reset: resetQueue, addBatch, addChange, onDecided },
      },
      new URLSearchParams(REVIEW).get('mock-tutorial') || null,
      { failOnce: REVIEW.includes('mock-tutorial-fail'), noOcr: REVIEW.includes('mock-tutorial-no-ocr') },
    )
  : null

function json<T>(value: T): T {
  return structuredClone(value)
}

function sessions(): Session[] {
  const map = new Map<string, Session>()
  for (const { summary } of exchanges) {
    const current = map.get(summary.session)
    if (!current) {
      map.set(summary.session, {
        key: summary.session,
        agent: summary.agent,
        first_ts: summary.ts,
        last_ts: summary.ts,
        exchanges: 1,
        masked: summary.new_masked,
        blocked: hasFlag(summary.flags, 'blocked') ? 1 : 0,
      })
    } else {
      current.first_ts = Math.min(current.first_ts, summary.ts)
      current.last_ts = Math.max(current.last_ts, summary.ts)
      current.exchanges++
      current.masked += summary.new_masked
      current.blocked += hasFlag(summary.flags, 'blocked') ? 1 : 0
    }
  }
  return [...map.values()].sort((a, b) => b.last_ts - a.last_ts)
}

const TYPE_WEIGHTS: Record<string, number> = {
  EMAIL: 0.22,
  USERNAME: 0.17,
  PHONE: 0.07,
  PASSWORD: 0.08,
  GB_NINO: 0.06,
  IBAN: 0.05,
  ASSIGNED_SECRET: 0.07,
  HOSTNAME: 0.04,
  URL_PASSWORD: 0.03,
  STRIPE_ACCESS_TOKEN: 0.03,
  PROJECT: 0.04,
  CLIENT: 0.03,
  GITHUB_PAT: 0.02,
  PERSON: 0.03,
  VAT_NUMBER: 0.02,
  JWT: 0.015,
  AWS_ACCESS_TOKEN: 0.01,
  PRIVATE_KEY: 0.005,
  ANTHROPIC_API_KEY: 0.005,
}

function stats(days: number): Stats {
  const rng = seeded(4242)
  const today = new Date(NOW * 1000)
  const range = dayRange(today, Math.min(days, 14)) // retention: 14 days
  const by_day = range.map((day) => {
    const weekday = new Date(`${day}T12:00:00`).getDay()
    const weekend = weekday === 0 || weekday === 6
    const exchangesCount = Math.round((weekend ? 6 : 70) + rng() * (weekend ? 24 : 110))
    return {
      day,
      exchanges: exchangesCount,
      masked: Math.round(exchangesCount * (0.35 + rng() * 0.5)),
      blocked: rng() < 0.45 ? 0 : Math.round(rng() * 3),
    }
  })
  const maskedTotal = by_day.reduce((n, d) => n + d.masked, 0)
  const exchangesTotal = by_day.reduce((n, d) => n + d.exchanges, 0)
  const by_type: Record<string, number> = {}
  for (const [type, weight] of Object.entries(TYPE_WEIGHTS)) {
    const value = Math.round(maskedTotal * weight)
    if (value > 0) by_type[type] = value
  }
  return {
    by_day,
    by_type,
    by_agent: {
      'claude-code': Math.round(exchangesTotal * 0.56),
      codex: Math.round(exchangesTotal * 0.36),
      opencode: Math.round(exchangesTotal * 0.06),
      other: Math.round(exchangesTotal * 0.02),
    },
    latency_ms: { p50: 4870, p95: 18420 },
    overhead_ms: { p50: 4.2, p95: 21.7 },
    cache_read_ratio: 0.8312,
    ...(STATS_PARTIAL ? { partial: true, latest: 100_000 } : {}),
  }
}

// The settings' etag (the daemon's is their digest): every change to them makes a new one.
const etagOf = () => `mock-settings-${rulesVersion}`
const STALE_SETTINGS = 'your settings changed since they were read: make the change again'

/** A save's etag, as the daemon checks it: one older than the settings in use (or none, on a Latet server) is refused. */
function checkEtag(body: unknown): void {
  const etag = (body as { etag?: unknown } | null)?.etag
  if ((etag === undefined || etag === null) && !SERVER) return
  if (etag !== etagOf()) throw new ApiError(409, STALE_SETTINGS, [], { error: STALE_SETTINGS, reason: 'settings_stale' })
}

/** A body without its etag. */
function withoutEtag<T>(body: T): T {
  if (body === null || typeof body !== 'object') return body
  const { etag: _etag, ...rest } = body as Record<string, unknown>
  return rest as T
}

/** The lists of a rules file written as JSON; null for any other file (YAML), which the mock answers as it always has. */
function jsonRules(file: string): { terms?: Term[]; allow_values?: string[]; allow_patterns?: string[]; recognizers?: RecognizerEntry[] } | null {
  try {
    const parsed = JSON.parse(file) as unknown
    return parsed !== null && typeof parsed === 'object' && 'latet_rules' in parsed ? (parsed as ReturnType<typeof jsonRules>) : null
  } catch {
    return null
  }
}

/** One list imported into your global rules or (with `project`) your own settings for a project, as transfer.py does. */
function importList(list: NonNullable<ReturnType<typeof jsonRules>>, project: string | undefined, apply: boolean, all: boolean) {
  const target: { terms?: Term[]; allow_values?: string[]; allow_patterns?: string[]; recognizers?: RecognizerEntry[] } = project ? (personalOf(project) ?? {}) : config
  const report = { added: [] as string[], skipped: [] as string[], held: [] as string[], notices: [] as string[] }
  const have = target.terms ?? []
  const terms = (list.terms ?? []).filter((term) => !have.some((t) => t.value === term.value && t.type.toUpperCase() === term.type.toUpperCase()))
  if (terms.length) report.added.push(`terms: ${terms.map((term) => term.value).join(', ')}`)
  const recognizers: RecognizerEntry[] = []
  for (const entry of list.recognizers ?? []) {
    const mine = (target.recognizers ?? []).find((r) => r.name === entry.name)
    if (!mine) {
      recognizers.push(entry)
      report.added.push(`recognizer ${String(entry.name)}`)
    } else if (JSON.stringify(mine) !== JSON.stringify(entry)) report.skipped.push(`recognizer ${String(entry.name)}: yours, with the same name, is kept`)
  }
  const values = (list.allow_values ?? []).filter((v) => !(target.allow_values ?? []).includes(v))
  const patterns = (list.allow_patterns ?? []).filter((v) => !(target.allow_patterns ?? []).includes(v))
  if (values.length) report.held.push(`values never to mask: ${values.join(', ')}`)
  if (patterns.length) report.held.push(`patterns of values never to mask: ${patterns.join(', ')}`)
  if (!report.added.length && !report.held.length) report.skipped.unshift('nothing new: these rules are yours already')
  if (!apply) return { report, applied: false }
  const next = {
    ...target,
    terms: [...have, ...terms],
    recognizers: [...(target.recognizers ?? []), ...recognizers],
    allow_values: [...(target.allow_values ?? []), ...(all ? values : [])],
    allow_patterns: [...(target.allow_patterns ?? []), ...(all ? patterns : [])],
  }
  if (project) setPersonalOf(project, next)
  else {
    const recognizer_status = next.recognizers.map((entry) => {
      const tried = mockTestRecognizer(entry, '')
      return { name: tried.name, state: tried.state, errors: tried.errors, warnings: tried.warnings }
    })
    config = { ...config, ...next, recognizer_status }
  }
  rulesVersion++
  return { report, applied: true, rules_version: rulesVersion }
}

/** A page of the vault, as the daemon answers it (rest.py `_vault_page`). */
function vaultPage(q: string, type: string, cls: string, page: number, perPage: number): VaultPage {
  const query = q.trim().toLowerCase()
  const types: Record<string, number> = {}
  for (const e of vault.entities.values()) types[e.type] = (types[e.type] ?? 0) + 1
  const matched = [...vault.entities.values()]
    .filter((e) => !type || e.type === type.toUpperCase())
    .filter((e) => !cls || e.class === cls.toUpperCase())
    .filter(
      (e) =>
        !query ||
        e.surrogate.toLowerCase().includes(query) ||
        e.type.toLowerCase().includes(query) ||
        e.id === query ||
        e.original.toLowerCase().includes(query),
    )
    .sort((a, b) => b.created - a.created || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  const size = Math.max(10, Math.min(200, perPage))
  const pages = Math.max(1, Math.ceil(matched.length / size))
  const shown = Math.min(Math.max(1, page), pages)
  const items = matched
    .slice((shown - 1) * size, shown * size)
    .map((e) => ({ id: e.id, type: e.type, class: e.class, surrogate: e.surrogate, created: e.created, sessions: e.sessions }))
  return { items, total: matched.length, page: shown, pages, per_page: size, types }
}

// As the daemon says it (config.TERM_WITHOUT_LETTERS, config.TERM_TOO_LONG).
const TERM_WITHOUT_LETTERS = 'a term needs a letter or a digit: a fake keeps the signs of a value, so signs alone, such as -- or ###, cannot be masked'
const TERM_TOO_LONG = `a term holds at most ${TERM_LONGEST} characters: a longer one is matched on its own, and one of about a million letters cannot be matched at all`

function validate(next: ConfigView, current: ConfigView): { loc: (string | number)[]; msg: string; type: string }[] {
  const errors: { loc: (string | number)[]; msg: string; type: string }[] = []
  next.allow_patterns.forEach((pattern, i) => {
    try {
      new RegExp(pattern)
    } catch (error) {
      errors.push({ loc: ['allow_patterns', i], msg: `Value error, invalid regular expression: ${(error as Error).message}`, type: 'value_error' })
    }
  })
  if (!Number.isInteger(next.retention_days) || next.retention_days < 1 || next.retention_days > 365) {
    errors.push({ loc: ['retention_days'], msg: 'Input should be between 1 and 365', type: 'less_than_equal' })
  }
  next.id_regions.forEach((region, i) => {
    if (!next.id_regions_available.includes(region)) errors.push({ loc: ['id_regions', i], msg: `Value error, unknown ${region}`, type: 'value_error' })
  })
  next.phone_regions.forEach((region, i) => {
    if (!/^[A-Z]{2}$/.test(region)) errors.push({ loc: ['phone_regions', i], msg: 'Value error, expected a two-letter region code', type: 'value_error' })
  })
  // A term there already stays as it is (written by hand): only a new one is refused.
  const kept = new Set(current.terms.map((term) => term.value))
  next.terms.forEach((term, i) => {
    const why = !hasLetterOrDigit(term.value) ? TERM_WITHOUT_LETTERS : termTooLong(term.value) ? TERM_TOO_LONG : null
    if (why !== null && !kept.has(term.value)) errors.push({ loc: ['terms', i, 'value'], msg: why, type: 'value_error' })
  })
  return errors
}

function route(method: Method, path: string, body: unknown): unknown {
  const url = new URL(path, 'http://mock')
  const p = url.pathname
  const q = url.searchParams

  if (method === 'GET' && p === '/me') {
    return {
      version: '0.9.10',
      profile: config.profile,
      port: 5283,
      data_dir: 'C:\\Users\\dhayes\\AppData\\Local\\latet',
      recording: config.history !== 'off',
      tutorial: tutorial ? tutorial.me() : null,
      server: SERVER ? MOCK_SERVER : null,
      // The documentation of its version, as a release has it (?mock-no-docs: an install from a checkout without it,
      // whose links go to docs.latet.ai). The dev server has no /_latet/docs/: its links are to look at, not to open.
      docs: !REVIEW.includes('mock-no-docs'),
    }
  }
  if (method === 'POST' && p === '/logout') return { ok: true }
  if (tutorial && p.startsWith('/tutorial/')) {
    const answer = tutorial.route(method, p, body)
    if (answer !== undefined) return json(answer)
  }
  if (method === 'GET' && p === '/exchanges') {
    const limit = Math.max(1, Math.min(500, Number(q.get('limit') ?? 100)))
    let list = exchanges.map((e) => e.summary)
    const session = q.get('session')
    const agent = q.get('agent')
    const flag = q.get('flag')
    const project = q.get('project')
    if (project) list = list.filter((s) => s.project?.id === project)
    if (session) list = list.filter((s) => s.session === session)
    if (agent) list = list.filter((s) => s.agent === agent)
    if (flag) list = list.filter((s) => hasFlag(s.flags, flag))
    const before = q.get('before')
    if (before) {
      const index = list.findIndex((s) => s.id === before)
      list = index === -1 ? [] : list.slice(index + 1)
    }
    const items = list.slice(0, limit)
    return { items: json(items), next: list.length > limit ? (items[items.length - 1]?.id ?? null) : null }
  }
  if (method === 'DELETE' && p === '/exchanges') {
    exchanges = []
    return { ok: true }
  }
  const columnsMatch = p.match(/^\/exchanges\/([^/]+)\/columns(\/review)?$/)
  if (columnsMatch && (method === 'GET' || (method === 'POST' && columnsMatch[2]))) {
    if (config.history !== 'full') throw new ApiError(409, `history: ${config.history} keeps no texts, and the columns are read from them`)
    const found = exchanges.find((e) => e.summary.id === decodeURIComponent(columnsMatch[1] ?? ''))
    const slot = found ? detailOf(found).request.slots[Number(q.get('slot'))] : undefined
    if (!found || slot?.kind !== 'tool_result') throw new ApiError(404, 'not found: no tool output there')
    const project = found.summary.project ?? null
    const columns = mockColumns(slot, project, config.llm_review.enabled, TUTORIAL ? config.fields : undefined, strictIn(project, config))
    if (method === 'GET') return json(q.get('shape') === '1' ? mockColumnShape(columns) : columns)
    if (!config.llm_review.enabled) throw new ApiError(409, 'the local reviewer is off: turn it on in Rules first')
    return json(mockColumnOpinions(columns.columns))
  }
  const exchangeMatch = p.match(/^\/exchanges\/([^/]+)$/)
  if (method === 'GET' && exchangeMatch) {
    const id = decodeURIComponent(exchangeMatch[1] ?? '')
    const found = exchanges.find((e) => e.summary.id === id)
    if (!found) throw new ApiError(404, 'not found')
    return json(detailOf(found))
  }
  if (method === 'GET' && p === '/sessions') {
    return { items: sessions().slice(0, Number(q.get('limit') ?? 50)) }
  }
  if (method === 'GET' && p === '/vault') {
    const page = Number(q.get('page') ?? 1)
    const perPage = Number(q.get('per_page') ?? 50)
    if (!Number.isInteger(page) || !Number.isInteger(perPage)) throw new ApiError(400, 'invalid page')
    return vaultPage(q.get('q') ?? '', q.get('type') ?? '', q.get('class') ?? '', page, perPage)
  }
  const revealMatch = p.match(/^\/vault\/([^/]+)\/reveal$/)
  if (method === 'POST' && revealMatch) {
    const entity = vault.entities.get(decodeURIComponent(revealMatch[1] ?? ''))
    if (!entity) throw new ApiError(404, 'not found')
    return { original: entity.original }
  }
  const vaultMatch = p.match(/^\/vault\/([^/]+)$/)
  if (method === 'DELETE' && vaultMatch) {
    if (!vault.forget(decodeURIComponent(vaultMatch[1] ?? ''))) throw new ApiError(404, 'not found')
    return { ok: true }
  }
  if (method === 'GET' && p === '/rules/export') {
    const terms = config.terms.map((term) => `- value: ${term.value}\n  type: ${term.type}`).join('\n')
    const file = `# Latet rules: \`latet rules import FILE\`, or Import rules in the dashboard.\nlatet_rules: 1\nprofile: ${config.profile}\nterms:\n${terms}\n`
    const project = q.get('project')
    const name = project ? 'latet-rules-billing-app' : 'latet-rules'
    // As export_rules: a recognizer whose examples hold a value Latet has seen for real is left out, and said.
    const recognizers = project ? (personalOf(project)?.recognizers ?? []) : config.recognizers
    const originals = new Set([...vault.entities.values()].map((entity) => entity.original))
    const warnings = recognizers
      .filter((entry) => [...(entry.examples?.match ?? []), ...(entry.examples?.no_match ?? [])].some((example) => originals.has(example)))
      .map((entry) => `recognizer ${String(entry.name)}: left out, its examples hold values Latet has seen for real`)
    return { name: `${name}-2026-10-08.yaml`, file, warnings }
  }
  if (method === 'POST' && p === '/rules/import') {
    const { apply = false, all = false, file = '', project } = (body ?? {}) as { apply?: boolean; all?: boolean; file?: string; project?: string }
    // A file of one list, as a table of Rules writes it (JSON, which the daemon reads as YAML): merged as the daemon
    // merges, what is new added, what lowers protection held back, nothing of yours replaced.
    const list = jsonRules(file)
    if (list) return importList(list, project, apply, all)
    const report = {
      added: ['terms: Harbor Bank, Fernsby Studio', 'recognizer badge-number', 'policy for IBAN: redact (yours: mask)'],
      skipped: ['recognizer ticket: yours, with the same name, is kept'],
      held: ['values never to mask: acme-dev', "the local reviewer's guidance: Ward and department names are confidential."],
      notices: ['port: not a rule, it stays on each machine'],
    }
    if (!apply) return { report, applied: false }
    const added = ['Harbor Bank', 'Fernsby Studio']
      .filter((value) => !config.terms.some((term) => term.value === value))
      .map((value) => ({ value, type: 'CLIENT', case_sensitive: false, whole_word: true }))
    config = {
      ...config,
      terms: [...config.terms, ...added],
      policies: { ...config.policies, IBAN: 'redact' },
      allow_values: all && !config.allow_values.includes('acme-dev') ? [...config.allow_values, 'acme-dev'] : config.allow_values,
    }
    rulesVersion++
    return { report, applied: true, rules_version: rulesVersion }
  }
  if (method === 'GET' && p === '/config') {
    return json({ ...config, data_effective: dataEffective(config.data, config.profile), etag: etagOf() })
  }
  if (method === 'PUT' && p === '/config') {
    checkEtag(body)
    const patch = withoutEtag((body ?? {}) as ConfigUpdate)
    const next: ConfigView = { ...config, ...patch, identity: { ...config.identity, ...(patch.identity ?? {}) } }
    next.pii_available = config.pii_available
    next.pii_catalog = config.pii_catalog
    next.pii_base = config.pii_base
    next.id_regions_available = config.id_regions_available
    next.checksums = config.checksums
    next.recognizer_status = next.recognizers.map((entry) => {
      const report = mockTestRecognizer(entry, '')
      return { name: report.name, state: report.state, errors: report.errors, warnings: report.warnings }
    })
    next.pii_effective =
      next.profile === 'secrets' ? [] : (next.pii ?? defaultPii(next.pii_catalog, next.pii_base, next.id_regions, next.profile === 'max'))
    next.ner_status = { ...config.ner_status }
    next.ner_status.active = nerActive(next)
    next.data_effective = dataEffective(next.data, next.profile)
    const errors = validate(next, config)
    if (errors.length) throw new ApiError(400, 'invalid configuration', errors)
    config = next
    rulesVersion++
    return { ok: true, rules_version: rulesVersion }
  }
  if (method === 'POST' && p === '/recognizers/test') {
    const { recognizer, text = '' } = (body ?? {}) as { recognizer?: RecognizerEntry; text?: string }
    if (!recognizer || typeof recognizer !== 'object') throw new ApiError(400, 'recognizer required')
    return mockTestRecognizer(recognizer, text)
  }
  if (method === 'POST' && p === '/allow') {
    const value = (body as { value?: string }).value ?? ''
    if (!value.trim()) throw new ApiError(400, 'value required')
    if (!config.allow_values.includes(value)) config = { ...config, allow_values: [...config.allow_values, value] }
    rulesVersion++
    return { ok: true, rules_version: rulesVersion }
  }
  if (method === 'POST' && p === '/terms') {
    const { value = '', type = 'CUSTOM' } = body as { value?: string; type?: string }
    if (!value.trim()) throw new ApiError(400, 'value required')
    if (!hasLetterOrDigit(value)) throw new ApiError(400, TERM_WITHOUT_LETTERS)
    if (termTooLong(value)) throw new ApiError(400, TERM_TOO_LONG)
    if (!config.terms.some((t) => t.value === value)) {
      config = {
        ...config,
        terms: [...config.terms, { value, type: type.toUpperCase(), case_sensitive: false, whole_word: true }],
        allow_values: config.allow_values.filter((v) => v !== value),
      }
    }
    rulesVersion++
    return { ok: true, rules_version: rulesVersion }
  }
  if (method === 'GET' && p === '/reviews') {
    return { items: mockReviews(q.get('session')) }
  }
  if (p === '/reviewer/key') {
    // On a Latet server the server gives the key: nothing of how it holds it.
    if (SERVER) {
      if (method !== 'GET') throw new ApiError(409, "not on a Latet server: the local reviewer's key, which its administrator sets for everyone")
      return { source: 'server', variable: null, stored: null }
    }
    if (method === 'PUT') reviewerKeySaved = true
    if (method === 'DELETE') reviewerKeySaved = false
    return { source: reviewerKeySaved ? 'saved' : null, variable: config.llm_review.api_key_env, stored: method === 'PUT' ? 'keyring' : null }
  }
  if (p === '/reviewer/certificate') {
    const fingerprint = 'sha256:' + '3f9a2c71'.repeat(8)
    const { url = '' } = (body ?? {}) as { url?: string }
    if (!url.startsWith('https://')) throw new ApiError(400, 'an https:// address is required')
    return { fingerprint, readable: (fingerprint.slice(7).toUpperCase().match(/../g) ?? []).join(':') }
  }
  if (method === 'GET' && p === '/reviewer/prompt') {
    return MOCK_PROMPT
  }
  if (method === 'POST' && p === '/reviewer/test') {
    const view = "Hi, I'm Paul Turner: on Thursday I'm taking my daughter Grace to the cardiologist, at 42 Mill Lane in Bristol. The password of the NAS at home is sunset-77."
    return {
      ok: true,
      error: null,
      models: ['bonsai2-27b'],
      model: 'bonsai2-27b',
      slots: 1,
      n_ctx: 32768,
      vision: false,
      seconds: 1.4,
      text: view,
      view,
      findings: [
        { text: 'Paul Turner', category: 'person', masked: true },
        { text: 'Grace', category: 'person', masked: true },
        { text: 'cardiologist', category: 'health', masked: true },
        { text: '42 Mill Lane', category: 'address', masked: true },
        { text: 'sunset-77', category: 'secret', masked: true },
      ],
      block: false,
    }
  }
  if (method === 'POST' && p === '/recognizers/draft') {
    const draft: RecognizerEntry = {
      name: 'badge-number',
      type: 'BADGE',
      description: 'Badge numbers: BD, a dash and six digits',
      pattern: '\\bBD-\\d{6}\\b',
      examples: { match: ['badge BD-381904', 'BD-120044 at the gate'], no_match: ['BD-12', 'ABD-381904'] },
    }
    return { draft, report: { name: 'badge-number', state: 'active', errors: [], warnings: [], examples: [] } }
  }
  if (method === 'POST' && p === '/playground') {
    const { text, profile } = body as { text?: string; profile?: string }
    if (typeof text !== 'string') throw new ApiError(400, 'text required')
    if (text.length > 1_000_000) throw new ApiError(413, 'text too long')
    if (profile !== undefined && !['balanced', 'max', 'secrets'].includes(profile)) throw new ApiError(400, 'unknown profile')
    return mockMask(text, config.terms, config.allow_values, (profile ?? config.profile) === 'secrets')
  }
  if (method === 'GET' && p === '/stats') {
    return stats(Math.max(1, Math.min(90, Number(q.get('days') ?? 7))))
  }
  if (method === 'GET' && p === '/integrations') {
    if (!SERVER) return json(integrations)
    // On a Latet server: no agent (they run on the person's computers), the destinations set for everyone marked.
    const shared = new Set(MOCK_SHARED_DESTINATIONS.map((d) => d.name))
    return json({
      checks: [{ status: 'ok', title: 'Server', detail: `masking for ${MOCK_SERVER.identity.name} at ${MOCK_SERVER.url}` }],
      agents: [],
      destinations: (integrations.destinations ?? []).map((d) => ({ ...d, shared: shared.has(d.name) })),
    })
  }
  const destinationMatch = p.match(/^\/destinations\/([^/]+)$/)
  if (method === 'DELETE' && destinationMatch) {
    const name = decodeURIComponent(destinationMatch[1] ?? '')
    const found = integrations.destinations?.find((d) => d.name === name)
    if (!found) throw new ApiError(404, `there is no destination named ${name}`)
    if (found.in_use) throw new ApiError(409, `${name} is still in use: an agent's settings point at it`)
    integrations.destinations = integrations.destinations?.filter((d) => d.name !== name)
    rulesVersion++
    return { ok: true, rules_version: rulesVersion }
  }
  if (p.startsWith('/approvals') || p.startsWith('/changes')) {
    const answer = routeApprovals(method, p, body)
    if (answer !== undefined) return json(answer)
  }
  if (p.startsWith('/projects')) {
    // Your own settings for a project are part of your settings: saved with their etag, they make a new one.
    const personal = method === 'PUT' && /^\/projects\/[^/]+\/personal$/.test(p)
    if (personal) checkEtag(body)
    const answer = routeProjects(method, p, personal ? withoutEtag(body) : body, config)
    if (answer !== undefined) {
      if (personal || (method === 'DELETE' && p.endsWith('/personal'))) rulesVersion++
      return json(answer)
    }
  }
  throw new ApiError(404, 'not found')
}

/**
 * What the local reviewer said of a session's blocks: in the tutorial's sandbox, the values the Tutorial Reviewer found and
 * masked (each by a rule `llm:<category>`); otherwise a company name it only suggested.
 */
function mockReviews(session: string | null): unknown[] {
  const out: unknown[] = []
  const seen = new Set<string>()
  for (const exchange of exchanges) {
    if (session && exchange.summary.session !== session) continue
    for (const s of detailOf(exchange).request.slots) {
      const review = reviewRecordOf(exchange.summary, s)
      if (review && !seen.has(review.block)) {
        seen.add(review.block) // a block is reviewed once, however many turns resend it
        out.push(review)
        continue
      }
      const at = s.masked.indexOf('Stripe')
      if (at < 0 || !s.block || seen.has(s.block)) continue
      seen.add(s.block) // a block is reviewed once, however many turns resend it
      const start = [...s.masked.slice(0, at)].length
      out.push({
        id: `rv_${s.block.slice(0, 8)}`,
        ts: exchange.summary.ts + 1,
        session: exchange.summary.session,
        project: exchange.summary.project?.id ?? null,
        block: s.block,
        kind: s.kind,
        inline: true,
        findings: [{ category: 'org', masked: false, entity: null, at: [start, start + 6] }],
        failed: null,
        block_asked: false,
        seconds: 1.3,
      })
    }
  }
  return out
}

export const mockTransport: Transport = {
  mock: true,
  assetUrl: (path) => MOCK_ASSETS[path] ?? '',
  async health() {
    await wait(60)
    const docs = !REVIEW.includes('mock-no-docs') // as /me says it
    return TUTORIAL ? { tutorial: true, docs } : SERVER ? { server: true, docs } : { docs }
  },
  async request(method, path, body, signal) {
    await wait(120 + Math.random() * 260)
    if (signal?.aborted) throw new DOMException('aborted', 'AbortError')
    if (REVIEW.includes('mock-401')) {
      notifyUnauthorized()
      throw new UnauthorizedError()
    }
    if (REVIEW.includes('mock-offline')) throw new NetworkError('Failed to fetch')
    return route(method, path, body)
  },
  subscribe(onExchange, onState: (state: StreamState) => void, onReview?: (review: ReviewRecord) => void) {
    listeners.add(onExchange)
    if (onReview) reviewListeners.add(onReview)
    onState('connecting')
    const opened = setTimeout(() => onState('open'), 300)
    return () => {
      clearTimeout(opened)
      listeners.delete(onExchange)
      if (onReview) reviewListeners.delete(onReview)
      onState('closed')
    }
  },
  subscribeApprovals,
}

let arrived = 0

/** Every third live exchange, the reviewer reading in the background finds a name in its prompt after it left. */
function lateReview(exchange: MockExchange): ReviewRecord | null {
  if (arrived++ % 3 !== 0) return null
  const slot = detailOf(exchange).request.slots.find((s) => s.block && s.kind === 'user_text')
  if (!slot?.block) return null
  return {
    id: `rv_late_${arrived}`,
    ts: exchange.summary.ts + 2,
    session: exchange.summary.session,
    project: exchange.summary.project?.id ?? null,
    block: slot.block,
    kind: slot.kind,
    inline: false,
    findings: [{ category: 'person', masked: true, entity: null, at: null }],
    failed: null,
    block_asked: false,
    seconds: 2.4,
  }
}

// One feed for the whole page, whoever listens: a new exchange every 4 to 9 seconds.
function schedule() {
  setTimeout(
    () => {
      if (config.history !== 'off') {
        const exchange = inProject(nextLiveExchange())
        exchange.kept = config.history
        exchanges = [exchange, ...exchanges]
        for (const listener of listeners) listener(json(exchange.summary))
        const review = lateReview(exchange)
        if (review) setTimeout(() => reviewListeners.forEach((listener) => listener(review)), 1500)
      }
      schedule()
    },
    4000 + Math.random() * 5000,
  )
}
if (!TUTORIAL) schedule()
// The change waiting in mock mode: approved, it adds ACME to the values never masked and turns the
// Live Reviewer off, as `latet config` would have.
startApprovals(
  () => config,
  () => {
    config = {
      ...config,
      allow_values: config.allow_values.includes('ACME') ? config.allow_values : [...config.allow_values, 'ACME'],
      live_review: { ...config.live_review, enabled: false },
      llm_review: { ...config.llm_review, guidance: `${MOCK_GUIDANCE}\nDo not report the names of our suppliers.` },
    }
    rulesVersion++
  },
)
