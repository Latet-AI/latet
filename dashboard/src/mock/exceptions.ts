// The mock's request exceptions (P2-7): synthetic, as `GET /exceptions` lists them, each state the page shows once;
// and what their uses are counted as since the pretend daemon started (`/me`'s request_status).

import { ApiError } from '../api/client'
import type { ExceptionChange, RequestException, RequestStatus } from '../api/types'
import { NOW } from './fixtures'

const DAY = 86400

/** An ISO day `days` from the mock's now. */
function day(days: number): string {
  return new Date((NOW + days * DAY) * 1000).toISOString().slice(0, 10)
}

function granted(daysAgo: number): string {
  return new Date((NOW - daysAgo * DAY) * 1000).toISOString()
}

let items: RequestException[] = [
  {
    id: 'x8b14e07c52',
    destination: 'azure-acme',
    profile: 'azure-openai.responses.1',
    scope: 'project',
    project: 'billing-app',
    reason: 'custom-header',
    components: [{ kind: 'header', name: 'x-acme-cost-center', role: 'checked', type: null }],
    days: 30,
    permanent: false,
    description: 'The cost center our Azure gateway bills Codex to.',
    state: 'expiring',
    ends: day(4),
    days_left: 4,
    granted: granted(26),
    issuer: 'owner',
  },
  {
    id: 'x3f9a2c41d7',
    destination: 'llm-acme-com',
    profile: 'anthropic.messages.1',
    scope: 'identity',
    project: null,
    reason: 'destination-credential',
    components: [{ kind: 'header', name: 'x-acme-gateway-key', role: 'credential', type: null }],
    days: 30,
    permanent: false,
    description: "The company gateway's own key, which Claude Code sends to it.",
    state: 'active',
    ends: day(19),
    days_left: 19,
    granted: granted(11),
    issuer: 'owner',
  },
  {
    id: 'x5d2e9a1f30',
    destination: 'openrouter',
    profile: 'openai.chat.1',
    scope: 'identity',
    project: null,
    reason: 'query-parameter',
    components: [{ kind: 'query', name: 'route-tag', role: 'checked', type: null }],
    days: 30,
    permanent: true,
    description: null,
    state: 'permanent',
    ends: null,
    days_left: null,
    granted: granted(40),
    issuer: 'owner',
  },
  {
    id: null,
    destination: 'anthropic',
    profile: 'anthropic.messages.1',
    scope: 'identity',
    project: null,
    reason: 'other',
    components: [{ kind: 'body', name: '/metadata_x/team', role: 'checked', type: 'string' }],
    days: 30,
    permanent: false,
    description: null,
    state: 'pending',
    ends: null,
    days_left: null,
    granted: null,
    issuer: null,
  },
  {
    id: 'x0c77d2b9e4',
    destination: 'openai',
    profile: 'openai.embeddings.1',
    scope: 'identity',
    project: null,
    reason: 'custom-header',
    components: [{ kind: 'header', name: 'x-acme-batch', role: 'checked', type: null }],
    days: 30,
    permanent: false,
    description: null,
    state: 'expired',
    ends: day(-3),
    days_left: 0,
    granted: granted(33),
    issuer: 'owner',
  },
]

/** The exception the sample's waiting change asks for: what the Approvals page shows before you approve it. */
export const MOCK_ASKED: ExceptionChange = {
  id: null,
  destination: 'anthropic',
  profile: 'anthropic.messages.1',
  scope: 'identity',
  project: null,
  reason: 'other',
  components: [{ kind: 'body', name: '/metadata_x/team', role: 'checked', type: 'string' }],
  days: 30,
  permanent: false,
  description: null,
  how: 'added',
}

export function mockRequestStatus(): RequestStatus {
  return {
    schema: 1,
    local: {
      counts: [
        ['5f0c9a7b3e2d41a68c0f1e2d3c4b5a69', 1, 1, 14, 14, 14, 0],
        ['0a9b8c7d6e5f40312a1b2c3d4e5f6a7b', 1, 2, 3, 3, 3, 0],
        [1, 0, 0, 212, 212, 212, 0],
      ],
      overflow: 0,
      partial: false,
      removed_headers: 6,
      saturated: false,
    },
    server: null,
  }
}

export function routeExceptions(method: string, path: string): unknown {
  if (method === 'GET' && path === '/exceptions') return { items: structuredClone(items), permanent_allowed: true }
  const match = path.match(/^\/exceptions\/([^/]+)$/)
  if (method === 'DELETE' && match) {
    const id = decodeURIComponent(match[1] ?? '')
    if (!items.some((item) => item.id === id)) throw new ApiError(404, 'there is no request exception with that id')
    items = items.filter((item) => item.id !== id)
    return { ok: true, rules_version: 1 }
  }
  return undefined
}
