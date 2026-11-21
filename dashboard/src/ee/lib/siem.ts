// SIEM form feedback follows settings.py; DNS and egress decisions remain the server's.
import { ApiError } from '../../api/client'
import { tk } from '../../i18n'
import type { Paused, SiemKind, SiemOutputSettings, SiemOutputView, SiemView } from '../types'

export interface Draft {
  original: string | null
  name: string
  kind: SiemKind
  url: string
  headers: [string, string][]
  format: 'json' | 'ndjson'
  secretName: string | null
  hasSecret: boolean
  binding: string | null
  secret: string
  removeSecret: boolean
}
export type DraftError = 'name' | 'nameTaken' | 'url' | 'local' | 'token' | 'header' | 'credential' | 'secretNeeded' | 'secret' | 'outputs'
export type OutputStatus = 'ok' | 'failing' | 'idle' | 'held'
const NAME = /^[a-z0-9][a-z0-9-]{0,39}$/
const HEADER = /^[A-Za-z0-9-]{1,64}$/
const CREDENTIAL = /authorization|api[-_]?key|token|secret|signature|password|passwd|pwd|credential|cookie|^sig$|^code$|(?:^|[-_])(?:key|pass)(?:$|[-_])|(?:^|[-_])auth(?:entication|n|z)?(?:$|[-_])/i
const RESERVED = /^(?:host|content-length|content-type|content-encoding|transfer-encoding|te|trailer|connection|keep-alive|upgrade|expect|accept-encoding|proxy-.*|forwarded|via|x-forwarded|x-forwarded-.*|x-real-ip|x-client-ip|true-client-ip|x-cluster-client-ip|client-ip|cf-connecting-ip|fastly-client-ip|x-remote-ip|x-remote-addr|x-originating-ip|x-azure-clientip|x-host|x-envoy-.*|x-original-.*|x-rewrite-url|x-http-method-override|x-http-method|x-method-override|cookie|origin|referer|sec-.*|user-agent|x-latet-.*)$/i
const GUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i
const bare = (host: string) => host.toLowerCase().replace(/\.+$/, '')
const here = (host: string) => ['localhost', '127.0.0.1', '[::1]'].includes(bare(host))
const visible = (text: string) => !/[^\x20-\x7e]/.test(text)

/** Python unquote: keep malformed percent escapes, replace invalid UTF8, never turn plus into a space. */
function unquote(text: string): string {
  return text.replace(/(?:%[0-9a-f]{2})+/gi, run => new TextDecoder('utf-8', { ignoreBOM: true }).decode(Uint8Array.from(run.match(/[0-9a-f]{2}/gi)!, hex => parseInt(hex, 16))))
}
function tokenish(text: string): boolean {
  if (/[0-9a-f]{32,}/i.test(text) || GUID.test(text)) return true
  if ((text.match(/[A-Za-z0-9_.=~+-]{20,}/g) ?? []).some(run => /[0-9]/.test(run) && /[a-z]/.test(run) && /[A-Z]/.test(run) && run.split(/[-_.~]/).some(p => p.length >= 12))) return true
  return (text.match(/[A-Za-z0-9=+]{20,}/g) ?? []).some(piece => /[0-9]/.test(piece) && /[A-Za-z]/.test(piece))
}
function tokenIn(kind: SiemKind, host: string, path: string, query: string): boolean {
  const segments = unquote(path).split('/')
  const logic = bare(host).endsWith('.logic.azure.com')
  if (segments.some((part, i) => !(logic && i > 0 && segments[i - 1] === 'workflows') && tokenish(part))) return true
  const pairs = query.split('&').map(pair => { const at = pair.indexOf('='); return at < 0 ? [pair, ''] : [pair.slice(0, at), pair.slice(at + 1)] })
  const channels = pairs.filter(([key]) => unquote(key!).toLowerCase() === 'channel').length
  return pairs.some(([written, given]) => {
    const name = unquote(written!), value = unquote(given!)
    if (kind === 'splunk' && written === 'channel' && channels === 1 && value.length === 36 && GUID.test(value)) return false
    return tokenish(name) || tokenish(value)
  })
}
interface Address { scheme: string; host: string; port: number | null; path: string; query: string }
/** Keep path/query and IPv6 spelling intact for binding; WHATWG normalization would erase distinctions. */
function address(text: string): Address | null {
  if (!text.length || text.length > 2048 || /[^\x21-\x7e]/.test(text) || text.includes('\\')) return null
  const match = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^/?#]+)([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/.exec(text)
  if (!match || match[5]) return null
  const [, protocol, authority, path, query = ''] = match
  if (authority!.includes('@') || authority!.includes('{secret}')) return null
  const hostPort = /^(\[[^\]]+\]|[A-Za-z0-9.-]+)(?::([0-9]*))?$/.exec(authority!)
  if (!hostPort) return null
  const host = hostPort[1]!.toLowerCase(), port = hostPort[2] ? Number(hostPort[2]) : null
  if (port !== null && (!Number.isInteger(port) || port > 65535)) return null
  if (host.startsWith('[')) {
    try { new URL(`https://${host}`) } catch { return null }
    if (host.includes('%')) return null
  }
  return { scheme: protocol!.toLowerCase(), host, port, path: path!, query }
}
function unreachableHost(host: string): boolean {
  const name = bare(host)
  if (here(name)) return false
  if (name.endsWith('.localhost')) return true
  if (name.startsWith('[')) {
    try { const normalized = new URL(`https://${name}`).hostname; return normalized === '[::1]' || normalized.startsWith('[::ffff:') } catch { return true }
  }
  if (/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/.test(name)) {
    const parts = name.split('.')
    return parts.some(p => Number(p) > 255 || String(Number(p)) !== p) || parts[0] === '127'
  }
  return /^(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+))*$/.test(name)
}
export function localOutput(output: Pick<SiemOutputSettings, 'url'>): boolean {
  // Reader URLs replace the path with an ellipsis; classification needs only the origin.
  const origin = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/[^/?#]+/.exec(output.url)?.[0]
  const parsed = origin ? address(origin) : null
  return parsed !== null && (parsed.scheme === 'http' || here(parsed.host))
}
export function bindingOf(output: SiemOutputSettings): string | null {
  const parsed = address(output.url)
  if (!parsed) return null
  const { scheme, host, path, query } = parsed
  const port = parsed.port || ({ https: 443, http: 80, tls: 6514 }[scheme] ?? 0)
  const headers = Object.entries(output.headers).filter(([, value]) => value.includes('{secret}')).map(([name, value]) => [name.toLowerCase(), value]).sort(([a], [b]) => a! < b! ? -1 : a! > b! ? 1 : 0)
  return `siem:${output.name}:${output.kind}:${scheme}://${bare(host)}:${port}${path}${query ? `?${query}` : ''}${headers.length ? `#headers=${JSON.stringify(headers)}` : ''}`
}
export function newDraft(kind: SiemKind = 'webhook'): Draft {
  return { original: null, name: '', kind, url: '', headers: [], format: 'json', secretName: null, hasSecret: false, binding: null, secret: '', removeSecret: false }
}
export function draftOf(view: SiemOutputView): Draft {
  return { original: view.name, name: view.name, kind: view.kind, url: view.url, headers: Object.entries(view.headers), format: view.format,
    secretName: view.secret, hasSecret: view.has_secret && !view.secret_destination_changed, binding: bindingOf(view), secret: '', removeSecret: false }
}
export function settingsOf(draft: Draft): SiemOutputSettings {
  return { name: draft.name, kind: draft.kind, url: draft.url, headers: draft.kind === 'webhook' ? Object.fromEntries(draft.headers.filter(([k, v]) => k !== '' || v !== '')) : {},
    secret: draft.secretName !== null || draft.secret.trim() !== '' ? `siem-${draft.name}` : null, format: draft.kind === 'webhook' ? draft.format : 'json' }
}
export function draftErrors(draft: Draft, others: readonly { name: string }[]): DraftError[] {
  const errors: DraftError[] = []
  if (!NAME.test(draft.name) || !visible(draft.name)) errors.push('name')
  else if (others.some(o => o.name === draft.name)) errors.push('nameTaken')
  if (others.length >= 50) errors.push('outputs')
  const parsed = address(draft.url)
  if (!parsed || unreachableHost(parsed.host) || (draft.kind === 'syslog'
    ? parsed.scheme !== 'tls' || !['', '/'].includes(parsed.path) || parsed.query !== ''
    : parsed.scheme !== 'https' && !(parsed.scheme === 'http' && here(parsed.host)))) errors.push('url')
  else if (localOutput(draft)) errors.push('local')
  if (parsed && tokenIn(draft.kind, parsed.host, parsed.path, parsed.query)) errors.push('token')
  const headers = draft.kind === 'webhook' ? draft.headers.filter(([k, v]) => k !== '' || v !== '') : []
  if (headers.length > 32 || new Set(headers.map(([key]) => key.toLowerCase())).size !== headers.length || headers.some(([key, value]) => !HEADER.test(key) || !visible(key) || RESERVED.test(key) || value.length > 4096 || !visible(value))) errors.push('header')
  const places = [...headers, ...(parsed ? [...new URLSearchParams(parsed.query).entries()] : [])]
  if (places.some(([key, value]) => CREDENTIAL.test(key) && !value.includes('{secret}'))) errors.push('credential')
  if (draft.secret !== '' && (!draft.secret.trim() || draft.secret.length > 4096 || !visible(draft.secret))) errors.push('secret')
  const settings = settingsOf(draft)
  const kept = draft.hasSecret && !draft.removeSecret && draft.binding !== null && draft.binding === bindingOf(settings)
  const required = draft.secretName !== null || draft.kind === 'splunk' || draft.url.includes('{secret}') || headers.some(([, v]) => v.includes('{secret}'))
  if (required && !kept && draft.secret.trim() === '') errors.push('secretNeeded')
  return errors
}
export function secretChange(draft: Draft): string | null | undefined { return draft.secret.trim() || (draft.removeSecret ? null : undefined) }
function settingsOfView({ name, kind, url, headers, secret, format }: SiemOutputView): SiemOutputSettings { return { name, kind, url, headers: { ...headers }, secret, format } }
export function withOutput(outputs: readonly SiemOutputView[], original: string | null, output: SiemOutputSettings): SiemOutputSettings[] {
  const kept = outputs.map(o => o.name === original ? output : settingsOfView(o))
  return outputs.some(o => o.name === original) ? kept : [...kept, output]
}
export function withoutOutput(outputs: readonly SiemOutputView[], name: string): SiemOutputSettings[] { return outputs.filter(o => o.name !== name).map(settingsOfView) }
export function statusOf(view: Pick<SiemOutputView, 'held' | 'blocked' | 'failing_since' | 'last_ok'>): OutputStatus {
  return view.held || view.blocked ? 'held' : view.failing_since !== null ? 'failing' : view.last_ok !== null ? 'ok' : 'idle'
}
export function siemChanged(error: unknown): boolean { return error instanceof ApiError && error.status === 409 && (error.body as { code?: unknown } | undefined)?.code === 'siem-changed' }
export const SIEM_PAUSED_TEXT: Record<Paused['why'], string> = {
  expired: tk('ee.reports.siem.paused.expired'), users: tk('ee.reports.siem.paused.users'), 'trial-ended': tk('ee.reports.siem.paused.trialEnded'), unusable: tk('ee.reports.siem.paused.unusable'),
}
const record = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v)
const text = (v: unknown, max: number): v is string => typeof v === 'string' && v.length <= max && [...v].every(c => c.charCodeAt(0) >= 32 && c.charCodeAt(0) !== 127)
const count = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0
const timestamp = (v: unknown) => v === null || typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 8.64e12
/** Feature-local malformed-response guard: no retained privileged data or enabled writes on an unknown shape. */
export function validSiemView(value: unknown): value is SiemView {
  if (!record(value) || !Array.isArray(value.outputs) || value.outputs.length > 50 || typeof value.editable !== 'boolean'
    || !(value.etag === null || text(value.etag, 256) && value.etag.length > 0) || value.editable && value.etag === null) return false
  if (value.paused !== null && (!record(value.paused) || typeof value.paused.why !== 'string' || !['expired', 'users', 'trial-ended', 'unusable'].includes(value.paused.why)
    || !(value.paused.expires === null || text(value.paused.expires, 10) && /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value.paused.expires)))) return false
  const names = new Set<string>()
  return value.outputs.every(o => {
    if (!record(o) || typeof o.name !== 'string' || !NAME.test(o.name) || !visible(o.name) || names.has(o.name)) return false
    names.add(o.name)
    return typeof o.kind === 'string' && ['webhook', 'splunk', 'syslog'].includes(o.kind)
      && typeof o.format === 'string' && ['json', 'ndjson'].includes(o.format)
      && text(o.url, 2048) && o.url.length > 0 && record(o.headers) && Object.keys(o.headers).length <= 32
      && Object.entries(o.headers).every(([key, v]) => HEADER.test(key) && visible(key) && text(v, 4096))
      && (o.secret === null || o.secret === `siem-${o.name}`) && typeof o.has_secret === 'boolean' && typeof o.secret_destination_changed === 'boolean'
      && count(o.queued) && count(o.dropped) && count(o.collapsed) && timestamp(o.last_ok) && timestamp(o.failing_since)
      && (o.last_error === null || text(o.last_error, 4096)) && typeof o.blocked === 'boolean' && typeof o.held === 'boolean'
  })
}
