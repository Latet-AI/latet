// Synthetic render fixtures only. These never configure a daemon or read an installed agent.
import type { ExchangeDetail, LlmReviewSettings, Me, Slot } from '../api/types'
import type { SpanInfo } from '../components/masked/types'

export const computer: Me = {
  version: 'test', profile: 'balanced', port: 5283, data_dir: '/synthetic/computer', recording: true, server: null, docs: false,
}

export const tutorial: Me = { ...computer, tutorial: { current: null, reviewer: true } }

export function server(kind: 'person' | 'key'): Me {
  return {
    ...computer,
    // Deliberately loopback: the authenticated descriptor, not the URL, determines deployment.
    data_dir: '',
    server: { url: 'http://127.0.0.1:5283', identity: { id: `example-${kind}`, name: 'Example', kind } },
  }
}

export const reviewer: LlmReviewSettings = {
  enabled: true, url: 'https://reviewer.example.invalid/v1', remote: true, model: 'example-model',
  api_key_env: '', api_key_file: null, inline: [], budget_ms: null, on_failure: 'stop', background: false,
  slots: 1, certificate: null, block: true, auto_mask: [], chunk_chars: 1000, images: false, guidance: null,
}

export const slot: Slot = {
  kind: 'outbound_tool', path: '$.messages[0].content', original: 'Example text', masked: 'Example text',
  from_model: false, new: true, replacements: [],
}

export function exchange(flags: string[], reason: string | null = null, transport = 'http'): ExchangeDetail {
  // Only the fields read by the real explanation, flow and transport consumers are needed here.
  return {
    summary: {
      flags, block_reason: reason, transport, mode: 'verify', masked: 0, restored: 0,
      project: null, model: 'example-model', upstream: 'example', status: null,
    },
    block_reason: reason,
    request: { headers: [], slots: [slot], body_bytes: 0 },
    response: null,
    leaks: flags.includes('leak') ? [{ type: 'EMAIL', kind: 'system', preview: 'EMAIL[5]', new: true }] : [],
  } as unknown as ExchangeDetail
}

export function span(cls: SpanInfo['cls']): SpanInfo {
  return {
    pair: `example-${cls}`, footnote: null, type: 'EXAMPLE', cls, rule: null, module: null, via: null,
    entity: null, original: 'Example', sent: 'Example surrogate', redacted: false, known: true,
    json: false, side: 'original',
  }
}
