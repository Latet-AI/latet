// Mock projects: one with a latet.yaml and settings of your own, one where Latet is off.

import { ApiError, type Method } from '../api/client'
import type { ConfigView, DataMode, PersonalSettings, Profile, ProjectDetail, ProjectSettings, ProjectSummary, SuggestResult } from '../api/types'
import { REPO_KEYS } from '../lib/projects'

/** Strict data mode as it applies, as the daemon works it out: `data` when set, else max turns it on. */
export function dataEffective(data: DataMode | null | undefined, profile: Profile): DataMode {
  return data ?? (profile === 'max' ? 'strict' : 'normal')
}

const BILLING = 'a1b2c3d4e5f6'
const SANDBOX = 'f6e5d4c3b2a1'

function tagOf(settings: ProjectSettings): string {
  let h = 0
  for (const c of JSON.stringify(settings)) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return h.toString(16).padStart(8, '0')
}

function project(id: string, name: string, repo: ProjectSettings | null, personal: PersonalSettings | null, notices: ProjectDetail['notices']): ProjectDetail {
  const root = `C:\\Users\\dhayes\\dev\\${name}`
  return {
    id,
    name,
    root,
    repo_file: repo ? `${root}\\latet.yaml` : null,
    has_personal: personal !== null,
    enabled: personal?.enabled !== false,
    profile: 'balanced',
    notice_count: notices.length,
    last_seen: 0,
    repo: {
      path: `${root}\\latet.yaml`,
      exists: repo !== null,
      settings: repo ?? {},
      tag: repo ? tagOf(repo) : '',
      readable: true,
      writable: true,
    },
    personal,
    notices,
    effective: {
      profile: 'balanced',
      pii: [],
      id_regions: [],
      phone_regions: [],
      include_private_ips: null,
      ner: 'auto',
      terms: 0,
      recognizers: 0,
      allow_values: 0,
      allow_patterns: 0,
      policies: {},
      data: 'normal',
      data_tools: [],
      data_files: [],
      data_hosts: [],
      fields: [],
      dump_guard: null,
      images: 'allow',
      llm_review: 'off',
    },
    recognizer_status: [],
  }
}

let projects: ProjectDetail[] = [
  project(
    BILLING,
    'billing-app',
    {
      profile: 'max',
      id_regions: ['DE'],
      terms: [{ value: 'Project Falcon', type: 'PROJECT', case_sensitive: false, whole_word: true }],
      recognizers: [
        {
          name: 'invoice-ref',
          type: 'INVOICE_REF',
          description: 'Invoice references: INV, a dash, four digits of year and five more',
          pattern: '\\bINV-\\d{4}-\\d{5}\\b',
          examples: { match: ['invoice INV-2026-00412'], no_match: ['INV-2026'] },
        },
      ],
      policies: { EMAIL: 'redact' },
      data: 'strict',
      fields: [{ name: 'note_*', type: 'NOTE' }],
      dump_guard: 50,
    },
    {
      terms: [{ value: 'Ashford Paints', type: 'CLIENT', case_sensitive: false, whole_word: true }],
      allow_values: ['billing-dev'],
      fields: [{ name: 'open_invoices', mask: false }],
    },
    [
      {
        source: 'repo',
        key: 'allow_values',
        level: 'ignored',
        message: 'a repository file cannot set values never to mask: put it in your own settings for the project',
        reason: 'repo_cannot_set',
      },
    ],
  ),
  project(SANDBOX, 'sandbox', null, { enabled: false }, []),
]

/** Your own settings for the project `id` (null: none yet), as a rules import reads them. */
export function personalOf(id: string): PersonalSettings | null {
  const found = projects.find((p) => p.id === id)
  if (!found) throw new ApiError(404, 'not found')
  return found.personal
}

/** Your own settings for the project `id` replaced, as a rules import writes them. */
export function setPersonalOf(id: string, personal: PersonalSettings): void {
  projects = projects.map((p) => (p.id === id ? { ...p, personal, has_personal: true } : p))
}

/** The project the mock's exchanges of an agent belong to. */
export function projectOfAgent(agent: string): { id: string; name: string } | null {
  if (agent === 'claude-code') return { id: BILLING, name: 'billing-app' }
  if (agent === 'opencode') return { id: SANDBOX, name: 'sandbox' }
  return null
}

/**
 * Strict data mode as it applies to an exchange of `project` (the global rules for one of no project), as the daemon's
 * answer for a tool output's columns says it.
 */
export function strictIn(project: { id: string } | null, config: ConfigView): boolean {
  const found = project ? projects.find((p) => p.id === project.id) : undefined
  return (found ? withEffective(found, config).effective.data : dataEffective(config.data, config.profile)) === 'strict'
}

function withEffective(p: ProjectDetail, config: ConfigView): ProjectDetail {
  const repo = p.repo.settings
  const mine = p.personal ?? {}
  const order = ['secrets', 'balanced', 'max'] as const
  let profile = config.profile
  if (repo.profile && order.indexOf(repo.profile) > order.indexOf(profile)) profile = repo.profile
  if (mine.profile) profile = mine.profile
  const regions = [...new Set([...config.id_regions, ...(repo.id_regions ?? []), ...(mine.id_regions ?? [])])]
  const enabled = mine.enabled !== false
  return {
    ...p,
    enabled,
    has_personal: p.personal !== null,
    profile,
    repo_file: p.repo.exists ? p.repo.path : null,
    notice_count: p.notices.length,
    effective: {
      profile,
      pii: profile === 'secrets' ? [] : [...config.pii_effective, ...(repo.pii ?? []), ...(mine.pii ?? [])],
      id_regions: regions,
      phone_regions: config.phone_regions,
      include_private_ips: repo.include_private_ips ?? mine.include_private_ips ?? config.include_private_ips,
      ner: config.ner,
      terms: config.terms.length + (repo.terms?.length ?? 0) + (mine.terms?.length ?? 0),
      recognizers: config.recognizers.length + (repo.recognizers?.length ?? 0) + (mine.recognizers?.length ?? 0),
      allow_values: config.allow_values.length + (mine.allow_values?.length ?? 0),
      allow_patterns: config.allow_patterns.length + (mine.allow_patterns?.length ?? 0),
      policies: { ...config.policies, ...(repo.policies ?? {}), ...(mine.policies ?? {}) },
      data: mine.data ?? (repo.data === 'strict' ? 'strict' : dataEffective(config.data, profile)),
      data_tools: mine.data_tools ?? config.data_tools,
      // Added by every layer: the global ones, the repository file's, yours.
      data_files: [...new Set([...config.data_files, ...(repo.data_files ?? []), ...(mine.data_files ?? [])])],
      data_hosts: [...new Set([...config.data_hosts, ...(repo.data_hosts ?? []), ...(mine.data_hosts ?? [])])],
      fields: [...(mine.fields ?? []), ...(repo.fields ?? []).filter((f) => f.mask !== false), ...config.fields],
      dump_guard: mine.dump_guard !== undefined ? mine.dump_guard : (repo.dump_guard ?? config.dump_guard),
      images: mine.images ?? (repo.images === 'block' ? 'block' : config.images),
      llm_review: config.llm_review.enabled ? (mine.llm_review ?? repo.llm_review ?? 'inline') : 'off',
    },
    recognizer_status: [...(repo.recognizers ?? []), ...(mine.recognizers ?? [])].map((r) => ({
      name: String(r.name ?? ''),
      state: r.enabled === false ? ('off' as const) : ('active' as const),
      errors: [],
      warnings: [],
    })),
  }
}

function summary(p: ProjectDetail): ProjectSummary {
  const { id, name, root, repo_file, has_personal, enabled, profile, notice_count, last_seen } = p
  return { id, name, root, repo_file, has_personal, enabled, profile, notice_count, last_seen }
}

/** The answer to a /projects request, or undefined when the path is not one of them. */
export function routeProjects(method: Method, path: string, body: unknown, config: ConfigView): unknown {
  if (path === '/projects' && method === 'GET') return { items: projects.map((p) => summary(withEffective(p, config))) }
  if (path === '/projects' && method === 'POST') {
    const folder = String((body as { path?: string })?.path ?? '').trim()
    if (!folder) throw new ApiError(400, 'path required')
    const name = folder.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || folder
    const found = projects.find((p) => p.root.toLowerCase() === folder.toLowerCase())
    if (found) return withEffective(found, config)
    const created = { ...project(tagOf({ terms: [{ value: folder, type: 'X', case_sensitive: false, whole_word: true }] }), name, null, null, []), root: folder }
    projects = [...projects, created]
    return withEffective(created, config)
  }
  const match = path.match(/^\/projects\/([^/]+)(\/personal|\/repo|\/suggest)?$/)
  if (!match) return undefined
  const id = decodeURIComponent(match[1] ?? '')
  const index = projects.findIndex((p) => p.id === id)
  if (index === -1) throw new ApiError(404, 'not found')
  const current = projects[index]!
  const replace = (next: ProjectDetail) => {
    projects = projects.map((p, i) => (i === index ? next : p))
    return next
  }
  const part = match[2] ?? ''
  if (part === '' && method === 'GET') return withEffective(current, config)
  if (part === '/personal' && method === 'PUT') {
    replace({ ...current, personal: body as PersonalSettings })
    return { ok: true, rules_version: 13 }
  }
  if (part === '/personal' && method === 'DELETE') {
    replace({ ...current, personal: null })
    return { ok: true, rules_version: 13 }
  }
  if (part === '/repo' && method === 'PUT') {
    const { settings, tag } = body as { settings: ProjectSettings; tag: string }
    const bad = Object.keys(settings).filter((k) => !(REPO_KEYS as readonly string[]).includes(k))
    if (bad.length > 0) {
      throw new ApiError(400, 'invalid settings', bad.map((k) => ({ loc: ['settings', k], msg: 'a repository file cannot set this', type: 'value_error' })))
    }
    if (tag !== current.repo.tag) throw new ApiError(409, 'the file changed since it was read: open the project again')
    const next = replace({ ...current, repo: { ...current.repo, exists: true, settings, tag: tagOf(settings) } })
    return withEffective(next, config)
  }
  if (part === '/suggest' && method === 'POST') {
    const result: SuggestResult = {
      files: 214,
      shapes: [{ shape: 'AA-9999-99999', count: 31, distinct: 12, file_count: 4, files: ['src/invoices.ts'], checksum: null, labels: [{ word: 'ref', values: 9 }] }],
      drafts: [
        {
          name: 'aa-9999-99999',
          description: 'Values shaped AA-9999-99999 (describe what they are)',
          pattern: '\\b[A-Z]{2}-\\d{4}-\\d{5}\\b',
          examples: { match: ['ref QZ-4817-20365'], no_match: ['QZ-4817'] },
        },
      ],
    }
    return result
  }
  return undefined
}
