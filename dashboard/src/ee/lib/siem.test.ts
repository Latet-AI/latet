import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import type { SiemKind, SiemOutputView, SiemView } from '../types'
import { bindingOf, draftErrors, draftOf, localOutput, newDraft, secretChange, settingsOf, siemChanged, statusOf, validSiemView, withOutput, withoutOutput } from './siem'
import tokenCases from './siemTokenCases.json'

export const output = (over: Partial<SiemOutputView> = {}): SiemOutputView => ({
  name: 'collector', kind: 'webhook', url: 'https://collector.example.test/in', headers: {}, secret: null, format: 'json',
  has_secret: false, secret_destination_changed: false, queued: 0, dropped: 0, last_ok: null, last_error: null,
  failing_since: null, blocked: false, held: false, collapsed: 0, ...over,
})
export const siemView = (over: Partial<SiemView> = {}): SiemView => ({ outputs: [output()], etag: 'one', editable: true, paused: null, ...over })
const draft = (url = 'https://collector.example.test/in', kind: SiemKind = 'webhook') => ({ ...newDraft(kind), name: 'collector', url })

describe('SIEM destination feedback mirrors the server contract', () => {
  it.each(tokenCases)('decodes shared token case $name with its own kind', row => {
    const kind = ('kind' in row ? row.kind : 'webhook') as SiemKind
    expect(draftErrors({ ...draft(row.url, kind), secret: 'synthetic-secret' }, []).includes('token')).toBe(row.token)
  })
  it.each(['https://localhost./in', 'https://LOCALHOST.../in', 'http://127.0.0.1/in', 'tls://[::1]:6514'])('leaves %s to the CLI', url => {
    expect(draftErrors(draft(url, url.startsWith('tls:') ? 'syslog' : 'webhook'), [])).toContain('local')
    expect(localOutput({ url })).toBe(true)
  })
  it('recognizes a redacted local origin for reader guidance', () => {
    expect(localOutput({ url: 'http://localhost:9880/…' })).toBe(true)
  })
  it.each(['https://host_name/in', 'https://host/in\n', 'https://host/in path', 'https://host:65536/in', 'https://host:abc/in',
    'https://{secret}.host/in', 'https://host:{secret}/in', 'https://user:pass@host/in', 'https://host/in#frag',
    'https://127.1/in', 'https://2130706433/in', 'https://0x7f000001/in', 'https://127.0.0.2/in', 'https://a.localhost/in',
    'https://[0:0:0:0:0:0:0:1]/in', 'https://host\\other/in', 'http://remote.example/in'])('refuses raw URL %s before browser normalization', url => {
    expect(draftErrors(draft(url), [])).toContain('url')
  })
  it.each(['tls://host/path', 'tls://host?query=1', 'udp://host:514'])('refuses syslog URL %s', url => {
    expect(draftErrors(draft(url, 'syslog'), [])).toContain('url')
  })
  it('accepts exact URL and count boundaries', () => {
    expect(draftErrors(draft('tls://host:0/', 'syslog'), [])).toEqual([])
    const base = 'https://host/'
    expect(draftErrors(draft(base + 'x'.repeat(2048 - base.length)), [])).toEqual([])
    expect(draftErrors(draft(base + 'x'.repeat(2049 - base.length)), [])).toContain('url')
    expect(draftErrors(draft(), Array.from({ length: 49 }, (_, i) => ({ name: `out-${i}` })))).toEqual([])
    expect(draftErrors(draft(), Array.from({ length: 50 }, (_, i) => ({ name: `out-${i}` })))).toContain('outputs')
  })
  it('retains malformed escapes and replacement UTF8 while plus remains plus in token checks', () => {
    expect(draftErrors(draft('https://host/%FF%zz/lowercase01234567890123'), [])).toContain('token')
    expect(draftErrors(draft('https://host/in?q=Abcdefghijkl+123456789'), [])).toContain('token')
    expect(draftErrors(draft('https://host/%FF%zz'), [])).toEqual([])
  })
  it.each(['Host', 'X-Forwarded-For', 'Accept-Encoding', 'X-HTTP-Method-Override', 'X-Latet-Signature', 'X-Original-URL', 'Trailer'])('refuses reserved %s', name => {
    expect(draftErrors({ ...draft(), headers: [[name, 'value']] }, [])).toContain('header')
  })
  it.each(['Authorization', 'X-Functions-Key', 'X-Authentication', 'X-Pass', 'Ocp-Apim-Subscription-Key'])('requires a secret placeholder in %s', name => {
    expect(draftErrors({ ...draft(), headers: [[name, 'synthetic-value']] }, [])).toContain('credential')
    expect(draftErrors({ ...draft(), headers: [[name, 'Bearer {secret}']], secret: 'synthetic-value' }, [])).toEqual([])
  })
  it('validates duplicate headers, visible ASCII and their exact bounds', () => {
    expect(draftErrors({ ...draft(), headers: [['X-One', 'a'], ['x-one', 'b']] }, [])).toContain('header')
    for (const value of ['bad\nline', 'Zürich', 'a'.repeat(4097)]) expect(draftErrors({ ...draft(), headers: [['X-One', value]] }, [])).toContain('header')
    expect(draftErrors({ ...draft(), headers: [['X-One', 'a'.repeat(4096)]] }, [])).toEqual([])
    expect(draftErrors({ ...draft(), headers: Array.from({ length: 33 }, (_, i) => [`X-${i}`, 'a']) }, [])).toContain('header')
    expect(draftErrors({ ...draft(), headers: [['', 'pasted-value']] }, [])).toContain('header')
  })
  it('checks the original secret before trimming and refuses implicit signing removal', () => {
    for (const secret of ['\nsecret', 'secret\r', 'é', 'a'.repeat(4097), '   ']) expect(draftErrors({ ...draft(), secret }, [])).toContain('secret')
    expect(draftErrors({ ...draft(), secret: 'a'.repeat(4096) }, [])).toEqual([])
    const signed = draftOf(output({ secret: 'siem-collector', has_secret: true }))
    expect(draftErrors({ ...signed, removeSecret: true }, [])).toContain('secretNeeded')
  })
})

describe('secret bindings and submitted settings', () => {
  const signed = () => draftOf(output({ secret: 'siem-collector', has_secret: true, headers: { Authorization: 'Bearer {secret}' } }))
  it('requires replacement on name, kind, path, query or secret header changes', () => {
    const was = signed()
    for (const change of [{ name: 'renamed' }, { kind: 'splunk' as const }, { url: was.url + '/new' }, { url: was.url + '?source=new' }, { headers: [['X-Copy', '{secret}']] as [string, string][] }]) {
      expect(draftErrors({ ...was, ...change }, [])).toContain('secretNeeded')
      expect(draftErrors({ ...was, ...change, secret: 'replacement' }, [])).not.toContain('secretNeeded')
    }
    expect(draftErrors(draftOf(output({ secret: 'siem-collector', secret_destination_changed: true })), [])).toContain('secretNeeded')
  })
  it('preserves harmless host/header normalization and non-secret headers but not empty path', () => {
    const was = signed()
    expect(draftErrors({ ...was, url: 'HTTPS://COLLECTOR.EXAMPLE.TEST...:443/in', headers: [['authorization', 'Bearer {secret}'], ['X-Source', 'latet']] }, [])).toEqual([])
    expect(bindingOf(output({ url: 'https://host' }))).not.toBe(bindingOf(output({ url: 'https://host/' })))
    expect(bindingOf(output({ url: 'tls://host:0' }))).toBe(bindingOf(output({ url: 'tls://host:6514' })))
  })
  it('derives a secret reference from the submitted name and keeps values apart', () => {
    const d = { ...signed(), name: 'new-name', secret: ' replacement ' }
    expect(settingsOf(d)).toMatchObject({ name: 'new-name', secret: 'siem-new-name' })
    expect(JSON.stringify(settingsOf(d))).not.toContain('replacement')
    expect(secretChange(d)).toBe('replacement')
    expect(secretChange({ ...d, secret: '' })).toBeUndefined()
    expect(secretChange({ ...d, secret: '', removeSecret: true })).toBeNull()
    expect(settingsOf({ ...draft(), kind: 'syslog', headers: [['X', 'ignored']], format: 'ndjson' })).toMatchObject({ headers: {}, format: 'json' })
  })
  it('preserves CLI local settings verbatim while changing or removing another output', () => {
    const local = output({ name: 'local', url: 'http://localhost.:5289/in' })
    const items = [local, output()]
    expect(withOutput(items, 'collector', settingsOf(draft()))[0]).toEqual({ name: 'local', kind: 'webhook', url: 'http://localhost.:5289/in', headers: {}, secret: null, format: 'json' })
    expect(withoutOutput(items, 'collector')).toHaveLength(1)
    expect(withoutOutput(items, 'local')[0]?.name).toBe('collector')
  })
})

describe('status and fail-closed read validation', () => {
  it.each(['kind', 'format', 'paused.why'])('rejects a singleton array for %s', field => {
    const malformed = field === 'paused.why'
      ? { ...siemView(), paused: { why: ['expired'], expires: null } }
      : { ...siemView(), outputs: [{ ...output(), [field]: [field === 'kind' ? 'webhook' : 'json'] }] }
    expect(validSiemView(malformed)).toBe(false)
  })
  it.each(['webhook', 'splunk', 'syslog'] as const)('preserves the valid kind %s', kind => {
    expect(validSiemView(siemView({ outputs: [output({ kind })] }))).toBe(true)
  })
  it.each(['json', 'ndjson'] as const)('preserves the valid format %s', format => {
    expect(validSiemView(siemView({ outputs: [output({ format })] }))).toBe(true)
  })
  it.each(['expired', 'users', 'trial-ended', 'unusable'] as const)('preserves the valid paused reason %s', why => {
    expect(validSiemView(siemView({ paused: { why, expires: null } }))).toBe(true)
  })
  it('shows event-only holds as held independently of the administration flag', () => {
    expect(statusOf(output({ held: true, blocked: false }))).toBe('held')
    expect(statusOf(output({ failing_since: 0 }))).toBe('failing')
    expect(statusOf(output({ last_ok: 0 }))).toBe('ok')
    expect(statusOf(output())).toBe('idle')
  })
  it('accepts a redacted auditor with nullable etag and rejects malformed data', () => {
    expect(validSiemView(siemView({ editable: false, etag: null, outputs: [output({ url: 'tls://log.example:6514' })] }))).toBe(true)
    for (const bad of [null, {}, { ...siemView(), outputs: {} }, { ...siemView(), etag: null }, { ...siemView(), paused: { why: 'surprise' } },
      ...[{ queued: -1 }, { dropped: NaN }, { collapsed: 0.5 }, { last_ok: Infinity }, { failing_since: 1e20 }, { kind: 'other' }, { headers: [] }, { held: 'true' }].map(change => ({ ...siemView(), outputs: [{ ...output(), ...change }] }))]) {
      expect(validSiemView(bad)).toBe(false)
    }
  })
  it('identifies only the SIEM compare-and-set marker', () => {
    expect(siemChanged(new ApiError(409, 'fixed', [], { code: 'siem-changed' }))).toBe(true)
    expect(siemChanged(new ApiError(409, 'fixed', [], { code: 'sign-in-changed' }))).toBe(false)
  })
})
