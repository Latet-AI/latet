import { expect, test } from 'claude-code/testing'
import type { On, Settings } from 'claude-code'

// These exercise registered consumers. No invented helper result or launch-setting names are mocked.
const LOCAL = 'http://127.0.0.1:5283'
const SPACE = 'https://latet.acme.lan/u/abcdefghijklmnopqrstuvwxyz'

function environment(on: On, bridge: string, layers: Record<string, Settings> = {}, failed?: string) {
  const values: Record<string, string> = { LATET_BRIDGE_URL: bridge, ANTHROPIC_BASE_URL: `${bridge}/anthropic` }
  on('env.get', ($, e) => ({ value: values[e.name] }))
  on('settings.read', ($, e) => {
    if (e.source === failed) return { deny: 'TASK6_SOURCE_ERROR_SENTINEL' }
    return { value: layers[e.source ?? ''] ?? { env: {} } }
  })
  on('session.id', () => ({ value: 'task6-session' }))
  on('session.messages', () => ({ value: [] }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('prompt.submit', ($, e) => ({ text: e.text }))
}

function observe(on: On) {
  const fetched: string[] = []
  const spawned: string[][] = []
  const diagnostics: string[] = []
  on('http.fetch', ($, e) => {
    fetched.push(e.url)
    return { deny: 'TASK6_RAW_RESPONSE_SENTINEL' }
  })
  on('process.run', ($, e) => {
    spawned.push([...e.argv])
    return { deny: 'TASK6_CHILD_STDERR_SENTINEL' }
  })
  on('ui.toast', ($, e) => {
    diagnostics.push(e.text)
    return { value: undefined }
  })
  return { fetched, spawned, diagnostics }
}

// Behavioral RED candidates: today's bridgeProblem repeats raw untrusted addresses.
for (const [name, address, secret] of [
  ['userinfo', 'https://user:TASK6_PASSWORD_SENTINEL@latet.acme.lan/u/abcdefghijklmnopqrstuvwxyz', 'TASK6_PASSWORD_SENTINEL'],
  ['query', `${SPACE}?TASK6_QUERY_SENTINEL`, 'TASK6_QUERY_SENTINEL'],
  ['fragment', `${SPACE}#TASK6_FRAGMENT_SENTINEL`, 'TASK6_FRAGMENT_SENTINEL'],
  ['route', 'https://latet.acme.lan/u/TASK6_ROUTE_SECRET_SENTINEL/extra', 'TASK6_ROUTE_SECRET_SENTINEL'],
  ['control', `${SPACE}\nTASK6_CONTROL_SENTINEL`, 'TASK6_CONTROL_SENTINEL'],
] as const) {
  test(`invalid remote ${name} is absent from every rejection diagnostic`, async ($, on) => {
    const seen = observe(on)
    environment(on, address)
    await $.session.start({ cwd: '/task6-project', surface: 'terminal', isInteractive: true })
    const answer = await $.prompt.submit({ text: 'TASK6_PLAINTEXT_SENTINEL', wait: false, origin: { kind: 'composer' } })
    expect(answer.drop).toBeTruthy()
    expect(seen.diagnostics.length).toBeGreaterThan(0)
    const diagnostics = JSON.stringify([...seen.diagnostics, answer.drop])
    expect(diagnostics).not.toContain(secret)
    expect(diagnostics).not.toContain('abcdefghijklmnopqrstuvwxyz')
    expect(diagnostics).not.toContain('TASK6_PLAINTEXT_SENTINEL')
    expect(seen.fetched).toEqual([])
    expect(seen.spawned).toEqual([])
  })
}

// Characterizations: retain real local fetch and successfully inspected layer precedence.
for (const source of ['user', 'project', 'local', 'flag', 'policy']) {
  test(`the effective local route source follows ${source} precedence`, async ($, on) => {
    let inspected: Record<string, string> | undefined
    const layers = Object.fromEntries(['user', 'project', 'local', 'flag', 'policy'].map((name) => [name, { env: {} }]))
    for (const name of ['user', 'project', 'local', 'flag', 'policy']) {
      layers[name] = { env: { ANTHROPIC_BASE_URL: `${LOCAL}/anthropic` } }
      if (name === source) break
    }
    environment(on, LOCAL, layers)
    on('ui.toast', () => ({ value: undefined }))
    on('http.fetch', ($, e) => {
      const path = e.url.split('/_latet/engine/v1/')[1]
      const body = e.init?.body ? JSON.parse(e.init.body) : {}
      if (path === 'route-check') inspected = body.sources
      const result = path === 'ping' ? { version: '0.10.3' } : path === 'instructions' ? { findings: [] } : { ok: true }
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(result) } }
    })
    await $.session.start({ cwd: '/task6-project', surface: 'terminal', isInteractive: true })
    expect(inspected?.ANTHROPIC_BASE_URL).toBe(source)
  })
}

// Prospective guards only until a reviewed valid binding supplies a positive remote control.
// A current pass caused by all remote bridges being unsupported is not provenance evidence.
for (const [name, layers, failed] of [
  ['failed project read', {}, 'project'],
  ['failed policy read after repository layer', { project: { env: { LATET_BRIDGE_URL: SPACE } } }, 'policy'],
  ['malformed repository env', { local: { env: 'TASK6_MALFORMED_SENTINEL' } }, undefined],
  ['malformed higher trusted env', { policy: { env: [] } }, undefined],
] as const) {
  test(`uncertain remote provenance with ${name} sends nothing from consumers`, async ($, on) => {
    const seen = observe(on)
    environment(on, SPACE, layers, failed)
    let toolRan = false
    on('tool.call', () => {
      toolRan = true
      return { result: 'TASK6_TOOL_RESULT_SENTINEL' }
    })
    on('ui.render', () => ({ type: 'Text' }))
    on('clock.sleep', () => ({ value: undefined }))
    await $.session.start({ cwd: '/task6-project', surface: 'terminal', isInteractive: true })
    const prompt = await $.prompt.submit({ text: 'TASK6_PLAINTEXT_SENTINEL', wait: false, origin: { kind: 'composer' } })
    const tool = await $.tool.call({ tool: 'Write', file_path: 'task6.txt', content: 'TASK6_PLAINTEXT_SENTINEL' })
    await $.ui.render({ surface: 'terminal', component: 'AssistantMessage', requestId: 'task6-render', props: { text: 'FAKE-999', isFirstOfReply: true } })
    expect(prompt.drop).toBeTruthy()
    expect(tool.deny).toBeTruthy()
    expect(toolRan).toBe(false)
    expect(seen.fetched).toEqual([])
    expect(seen.spawned).toEqual([])
    expect(JSON.stringify(seen.diagnostics)).not.toContain('TASK6_SOURCE_ERROR_SENTINEL')
    expect(JSON.stringify(seen.diagnostics)).not.toContain('TASK6_RAW_RESPONSE_SENTINEL')
    expect(JSON.stringify(seen.diagnostics)).not.toContain('TASK6_CHILD_STDERR_SENTINEL')
  })
}
