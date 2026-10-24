import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, ProcessRunResult, Settings } from 'claude-code'

// Frozen task-6-helper-protocol-freeze.md. These mock only the process/host resource boundary;
// they do not prove installed wire, descriptor ACLs, native-loader isolation or process cleanup.
const SPACE = 'https://latet.acme.lan/u/abcdefghijklmnopqrstuvwxyz'
const TOKEN = `latet_pat_${'A'.repeat(43)}`
const PROJECT = 'C:\\Task6Project-CWD-SENTINEL'
const SESSION = 'task6-session-SENTINEL'
const ROOT = 'C:\\Task6Private\\connection\\claude-helper'
const WINDOWS = 'C:\\Windows'
const HELPER = {
  v: 1,
  python: 'C:\\Task6TrustedInstall\\python.exe',
  cwd: `${ROOT}\\cwd`,
  descriptor: `${ROOT}\\permanent.json`,
  env: {
    PATH: `${WINDOWS}\\System32`, SystemRoot: WINDOWS, WINDIR: WINDOWS,
    HOME: `${ROOT}\\homes\\user`, USERPROFILE: `${ROOT}\\homes\\user`,
    CODEX_HOME: `${ROOT}\\homes\\codex`, CLAUDE_CONFIG_DIR: `${ROOT}\\homes\\claude`,
    XDG_CONFIG_HOME: `${ROOT}\\homes\\xdg`, APPDATA: `${ROOT}\\homes\\roaming`,
    LOCALAPPDATA: `${ROOT}\\homes\\local`, HOMEDRIVE: 'C:',
    HOMEPATH: '\\Task6Private\\connection\\claude-helper\\homes\\user',
    TEMP: `${ROOT}\\temp`, TMP: `${ROOT}\\temp`, TMPDIR: `${ROOT}\\temp`,
    LATET_KEYRING: 'off', PYTHON_KEYRING_BACKEND: 'keyring.backends.null.Keyring',
    PYTHONHOME: '', PYTHONPATH: '', PYTHONSTARTUP: '', PYTHONUSERBASE: '',
    PYTHONEXECUTABLE: '', __PYVENV_LAUNCHER__: '', __COMPAT_LAYER: '',
    SSL_CERT_FILE: '', SSL_CERT_DIR: '', SSLKEYLOGFILE: '', OPENSSL_CONF: '',
    OPENSSL_CONF_INCLUDE: '', OPENSSL_ENGINES: '', OPENSSL_MODULES: '', OPENSSL_TRACE: '',
    LATET_DATA_DIR: '', LATET_CONNECTION: '', LATET_CONFIG: '', LATET_SERVER_URL: '',
    LATET_SERVER_FINGERPRINT: '', LATET_SERVER_ADMIN_TOKEN: '', LATET_BRIDGE_URL: '',
    LATET_BRIDGE_TOKEN: '', LATET_BRIDGE_HELPER: '', LATET_TRUSTED_ACCOUNTS: '',
  },
}
const SOURCES = ['user', 'project', 'local', 'flag', 'policy']
const AUTHORITY = {
  LATET_BRIDGE_URL: SPACE, LATET_BRIDGE_TOKEN: TOKEN,
  LATET_BRIDGE_HELPER: JSON.stringify(HELPER), ANTHROPIC_BASE_URL: `${SPACE}/anthropic`,
}
type Input = { v: number; op: string; bridge: string; token: string; body: any }
type Options = {
  beforeEnv?: (name: string) => void | Promise<void>
  layers?: Record<string, unknown>
  failed?: string
  effective?: Record<string, string | undefined>
  result?: (input: Input, valid: ProcessRunResult) => ProcessRunResult | { deny: string }
  verdict?: unknown
  messagesFail?: () => boolean
}

function success(response: unknown): ProcessRunResult {
  return { exitCode: 0, stdout: JSON.stringify({ v: 1, ok: true, response }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
}

function remote(on: On, options: Options = {}) {
  const calls: Input[] = [], launches: { argv: string[]; init: unknown }[] = []
  const fetched: string[] = [], diagnostics: string[] = [], inspected: string[] = []
  const tools: Record<string, any>[] = [], screens: string[] = [], appended: unknown[] = []
  const renders: { component: string; props: unknown }[] = []
  const messages = [{ role: 'user' as const, content: [{ type: 'text' as const, text: 'already FAKE-999' }] }]
  const masked = (items: { text: string }[]) => ({ items: items.map((item) => ({ text: item.text.replaceAll('SECRET-123', 'FAKE-999'), count: 1 })) })
  let pending: unknown = null, providerCalls = 0
  on('env.get', async ($, e) => {
    await options.beforeEnv?.(e.name)
    return { value: { ...AUTHORITY, LATET_AUTOSTART: '0', OS: 'Windows_NT', ...options.effective }[e.name] }
  })
  on('settings.read', ($, e) => {
    inspected.push(e.source ?? '')
    if (e.source === options.failed) return { deny: 'TASK6_SOURCE_ERROR_SENTINEL' }
    return { value: (options.layers?.[e.source ?? ''] ?? { env: e.source === 'user' ? AUTHORITY : {} }) as Settings }
  })
  on('session.id', () => ({ value: SESSION }))
  on('session.messages', () => (options.messagesFail?.() ? { deny: 'TASK6_RAW_OUTPUT_SENTINEL' } : { value: messages }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('prompt.submit', ($, e) => ({ text: e.text }))
  on('prompt.section', ($, e) => ({ text: e.text }))
  on('session.append', ($, e, next) => {
    appended.push(e.message)
    return next(e)
  })
  on('tool.call', ($, e) => {
    tools.push(e)
    return { result: 'tool completed' }
  })
  on('ui.render', ($, e) => {
    renders.push({ component: e.component, props: e.props })
    if (e.component === 'AssistantMessage') screens.push(e.props.text)
    return { type: 'Text' }
  })
  on('ui.toast', ($, e) => { diagnostics.push(e.text); return { value: undefined } })
  on('ui.status', () => ({ value: undefined }))
  on('clock.sleep', () => ({ value: undefined }))
  on('turn.step', async function* ($, e) {
    providerCalls++
    return { turnId: e.turnId, index: e.index, answer: 'provider completed', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  on('http.fetch', ($, e) => {
    fetched.push(e.url)
    return { deny: 'TASK6_HTTP_FALLBACK_SENTINEL' }
  })
  // No real filesystem lookup: this bounded resource seam is not an ownership/ACL attestation.
  on('fs.stat', ($, e) => {
    // A host other than Windows takes C:\… for a relative path and resolves it against the tests' folder: undone.
    const resolved = e.path.replaceAll('/', '\\')
    const path = /^[A-Za-z]:\\/.test(resolved) ? resolved : (/\\([A-Za-z]:\\.*)$/.exec(resolved)?.[1] ?? resolved)
    const admitted = path === HELPER.python || path === 'C:\\Task6TrustedInstall' || path === WINDOWS ||
      path === `${WINDOWS}\\System32` || path === 'C:\\' || path === 'C:\\Task6Private' || path.startsWith(`${ROOT}\\`) ||
      path === ROOT || path === 'C:\\Task6Private\\connection'
    if (!admitted) return { deny: 'TASK6_UNBOUND_PATH_SENTINEL' }
    return { value: { kind: path === HELPER.python || path === HELPER.descriptor ? 'file' : 'dir', size: 128, mtimeMs: 1, isLink: false, realPath: path } }
  })
  on('process.run', ($, e) => {
    launches.push({ argv: [...e.argv], init: e.init })
    if (launches.length > 24) return { deny: 'TASK6_UNBOUNDED_CALLS_SENTINEL' }
    const input = JSON.parse(e.init?.stdin ?? '') as Input
    calls.push(input)
    let response: unknown
    switch (input.op) {
      case 'ping': response = { ok: true, version: '0.9.8' }; break
      case 'route-check': response = options.verdict === undefined ? { ok: true } : options.verdict; break
      case 'scope': response = { ok: true }; break
      case 'instructions': response = { findings: [] }; break
      case 'mask': pending = masked(input.body.items); response = { pending: 'task6pending', viewers: 1 }; break
      case 'approvals/wait': response = pending; pending = null; break
      case 'rehydrate': response = { items: input.body.items.map((text: string) => text.replaceAll('FAKE-999', 'SECRET-123')), restored: 1 }; break
      case 'outbound': response = { found: [], reason: null }; break
      case 'verify': response = { blocking: [], flagged: [], inherited: false }; break
      default: return { deny: 'TASK6_UNKNOWN_OPERATION_SENTINEL' }
    }
    const valid = success(response)
    const result = options.result?.(input, valid) ?? valid
    return 'deny' in result ? result : { value: result }
  })
  return { calls, launches, fetched, diagnostics, inspected, tools, screens, renders, appended, providerCalls: () => providerCalls }
}

async function start($: Engine) {
  await $.session.start({ cwd: PROJECT, surface: 'terminal', isInteractive: true })
}

async function turn($: Engine) {
  const stream = $.turn.step({ turnId: 'task6-turn', index: 0, model: 'claude-test', messageCount: 1 })
  let answer = await stream.next()
  while (!answer.done) answer = await stream.next()
  return answer.value
}

function noDiagnostics(seen: ReturnType<typeof remote>, extra: unknown = '') {
  const diagnostic = JSON.stringify([seen.diagnostics, extra])
  for (const secret of [TOKEN, 'abcdefghijklmnopqrstuvwxyz', PROJECT, SESSION, 'SECRET-123', ROOT,
    HELPER.python, HELPER.cwd, HELPER.descriptor,
    'TASK6_CHILD_STDERR_SENTINEL', 'TASK6_RAW_OUTPUT_SENTINEL', 'TASK6_SOURCE_ERROR_SENTINEL']) {
    expect(diagnostic).not.toContain(secret)
  }
  expect(seen.fetched).toEqual([])
}

const startupRenders = [
  ['AssistantMessage', ($: Engine) => $.ui.render({ surface: 'terminal', component: 'AssistantMessage', requestId: 'startup-assistant', props: { text: 'startup FAKE-999', isFirstOfReply: true } })],
  ['UserMessage', ($: Engine) => $.ui.render({ surface: 'terminal', component: 'UserMessage', requestId: 'startup-user', props: { text: 'startup FAKE-999', origin: { kind: 'composer' }, isExpanded: false } })],
  ['ToolUse', ($: Engine) => $.ui.render({ surface: 'terminal', component: 'ToolUse', requestId: 'startup-tool', props: { tool_use_id: 'startup-tool', tool: 'Bash', input: { command: 'echo FAKE-999' }, output: 'FAKE-999', isRunning: false, isErrored: false, isInterrupted: false } })],
  ['ToolResult', ($: Engine) => $.ui.render({ surface: 'terminal', component: 'ToolResult', requestId: 'startup-result', props: { tool_use_id: 'startup-result', tool: 'Bash', output: { stdout: 'FAKE-999', stderr: '' }, isErrored: false } })],
  ['ToolGroup', ($: Engine) => $.ui.render({ surface: 'terminal', component: 'ToolGroup', requestId: 'startup-group', props: { calls: [{ tool: 'Bash', input: { command: 'echo FAKE-999' }, isRunning: false, isErrored: false, isInterrupted: false }], isActive: false, isExpanded: false } })],
] as const

for (const [component, render] of startupRenders) {
  test(`startup trust: ${component} makes no transport call before initialization and restores afterwards`, async ($, on) => {
    const seen = remote(on)
    await render($)
    expect(seen.renders).toHaveLength(1)
    expect(JSON.stringify(seen.renders[0]?.props)).toContain('FAKE-999')
    expect(seen.fetched).toEqual([])
    expect(seen.launches).toEqual([])
    await start($)
    seen.renders.length = 0
    await render($)
    expect(seen.renders).toHaveLength(1)
    expect(JSON.stringify(seen.renders[0]?.props)).toContain('SECRET-123')
    expect(seen.calls.some((call) => call.op === 'rehydrate')).toBe(true)
    expect(seen.fetched).toEqual([])
  })
}

test('startup trust: prompts and tool content are refused before initialization without a local fallback', async ($, on) => {
  const seen = remote(on)
  const prompt = await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })
  const tool = await $.tool.call({ tool: 'Write', file_path: 'startup.txt', content: 'FAKE-999' })
  expect(prompt.drop).toBeTruthy()
  expect(tool.deny).toBeTruthy()
  expect(seen.fetched).toEqual([])
  expect(seen.launches).toEqual([])
  expect(seen.tools).toEqual([])
  await start($)
  const ready = await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })
  expect(ready.text).toBe('FAKE-999')
})

test('startup trust: a new session withdraws the previous transport before its first awaited environment read', async ($, on) => {
  let armed = false
  let entered!: () => void, release!: () => void
  const blocked = new Promise<void>((resolve) => { entered = resolve })
  const released = new Promise<void>((resolve) => { release = resolve })
  const seen = remote(on, { beforeEnv: async (name) => {
    if (armed && name === 'LATET_BRIDGE_URL') { entered(); await released }
  } })
  await start($)
  const before = seen.launches.length
  armed = true
  const starting = start($)
  await blocked
  try {
    await $.ui.render({ surface: 'terminal', component: 'AssistantMessage', requestId: 'startup-race', props: { text: 'new-session FAKE-999', isFirstOfReply: true } })
    expect(seen.screens).toEqual(['new-session FAKE-999'])
    expect(seen.launches).toHaveLength(before)
    expect(seen.fetched).toEqual([])
  } finally {
    release()
    await starting
  }
  await $.ui.render({ surface: 'terminal', component: 'AssistantMessage', requestId: 'startup-after', props: { text: 'new-session FAKE-999', isFirstOfReply: true } })
  expect(seen.screens.at(-1)).toBe('new-session SECRET-123')
})

test('the fixed helper reaches all nine operations through their real hooks', async ($, on) => {
  const seen = remote(on)
  await start($)
  const prompt = await $.prompt.submit({ text: 'my key SECRET-123', wait: false, origin: { kind: 'composer' } })
  expect(prompt.text).toBe('my key FAKE-999')
  const section = await $.prompt.section({ name: 'memory', text: 'remember SECRET-123' })
  expect(section.text).toBe('remember FAKE-999')
  await $.session.append({ door: 'tool-result', origin: { kind: 'tool', tool: 'Bash' }, uuid: 'task6-positive-row', message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'result SECRET-123' }] } })
  expect(JSON.stringify(seen.appended)).toContain('result FAKE-999')
  expect(JSON.stringify(seen.appended)).not.toContain('SECRET-123')
  await $.tool.call({ tool: 'Write', file_path: 'task6.txt', content: 'FAKE-999' })
  expect(seen.tools[0]?.content).toBe('SECRET-123')
  await $.tool.call({ tool: 'WebFetch', url: 'https://example.invalid/public', prompt: 'public' })
  expect(seen.tools[1]?.tool).toBe('WebFetch')
  await $.ui.render({ surface: 'terminal', component: 'AssistantMessage', requestId: 'task6-render', props: { text: 'visible FAKE-999', isFirstOfReply: true } })
  expect(seen.screens).toEqual(['visible SECRET-123'])
  expect((await turn($)).answer).toBe('provider completed')
  expect(seen.providerCalls()).toBe(1)
  expect([...new Set(seen.calls.map((call) => call.op))].sort()).toEqual(
    ['ping', 'route-check', 'scope', 'instructions', 'mask', 'approvals/wait', 'rehydrate', 'outbound', 'verify'].sort(),
  )
  expect(seen.calls[0]?.op).toBe('ping')
  expect(seen.calls[0]?.body).toBeNull()
  expect(seen.calls.findIndex((call) => call.op === 'route-check')).toBeLessThan(seen.calls.findIndex((call) => call.op === 'scope'))
  for (const source of SOURCES) expect(seen.inspected).toContain(source)
  for (const [index, launch] of seen.launches.entries()) {
    const init = launch.init as { cwd: string; env: Record<string, string>; stdin: string; timeoutMs: number }
    expect(launch.argv).toEqual([HELPER.python, '-I', '-B', '-m', 'latet.claude_bridge_helper', '--descriptor', HELPER.descriptor])
    expect(init.cwd).toBe(HELPER.cwd)
    expect(init.env).toEqual(HELPER.env)
    expect(init.timeoutMs).toBe(35000)
    const input = seen.calls[index]
    if (!input) throw new Error('a process launch has no matching protocol input')
    expect(Object.keys(input).sort()).toEqual(['v', 'op', 'bridge', 'token', 'body'].sort())
    expect(input.v).toBe(1)
    expect(input.bridge).toBe(SPACE)
    expect(input.token).toBe(TOKEN)
    expect(new TextEncoder().encode(init.stdin).byteLength).toBeLessThanOrEqual(4 * 1024 * 1024)
    const publicLaunch = JSON.stringify({ argv: launch.argv, cwd: init.cwd, env: init.env })
    for (const secret of [TOKEN, SPACE, PROJECT, SESSION, 'SECRET-123']) expect(publicLaunch).not.toContain(secret)
    if (input.op !== 'ping') {
      expect(input.body.session).toBe(SESSION)
      expect(input.body.cwd).toBe(PROJECT)
    }
  }
  noDiagnostics(seen)
})

test('the complete UTF-8 stdout ceiling preserves the allowed screen result and wholly refuses one more byte', async ($, on) => {
  const limit = 4 * 1024 * 1024 - 1
  const bytes = (text: string) => new TextEncoder().encode(text).byteLength
  const overhead = bytes(success({ items: [''], restored: 1 }).stdout)
  function output(size: number) {
    const room = size - overhead
    const text = 'é'.repeat(Math.floor(room / 2)) + (room % 2 ? 'x' : '')
    const result = success({ items: [text], restored: 1 })
    expect(bytes(result.stdout)).toBe(size)
    return { text, result }
  }
  let reply = output(limit)
  const seen = remote(on, { result: (input, valid) => input.op === 'rehydrate' ? reply.result : valid })
  await start($)
  await $.ui.render({ surface: 'terminal', component: 'AssistantMessage', requestId: 'bounded-render', props: { text: 'first FAKE-999', isFirstOfReply: true } })
  expect(seen.screens.length).toBe(1)
  expect(seen.screens[0] === reply.text).toBe(true)
  const before = seen.calls.length
  reply = output(limit + 1) // Exactly the host's untruncated 4 MiB; the protocol's own ceiling is smaller.
  await $.ui.render({ surface: 'terminal', component: 'AssistantMessage', requestId: 'oversize-render', props: { text: 'second FAKE-999', isFirstOfReply: true } })
  expect(seen.calls.slice(before).map((input) => input.op)).toEqual(['rehydrate'])
  expect(seen.screens[1] === 'second FAKE-999').toBe(true)
  noDiagnostics(seen)
})

const badOutputs: [string, (valid: ProcessRunResult) => ProcessRunResult | { deny: string }][] = [
  ['nonzero success', (valid) => ({ ...valid, exitCode: 2 })],
  ['zero failure', (valid) => ({ ...valid, stdout: '{"v":1,"ok":false,"code":"binding"}' })],
  ['wrong version', (valid) => ({ ...valid, stdout: JSON.stringify({ ...JSON.parse(valid.stdout), v: 2 }) })],
  ['missing response', (valid) => ({ ...valid, stdout: '{"v":1,"ok":true}' })],
  ['extra field', (valid) => ({ ...valid, stdout: JSON.stringify({ ...JSON.parse(valid.stdout), extra: 1 }) })],
  ['duplicate field', (valid) => ({ ...valid, stdout: valid.stdout.replace('{"v":1,', '{"v":1,"v":1,') })],
  ['extra object', (valid) => ({ ...valid, stdout: `${valid.stdout}\n{"v":1}` })],
  ['malformed JSON', (valid) => ({ ...valid, stdout: 'TASK6_RAW_OUTPUT_SENTINEL' })],
  ['stdout truncation', (valid) => ({ ...valid, isStdoutTruncated: true })],
  ['stderr truncation', (valid) => ({ ...valid, isStderrTruncated: true })],
  ['normal stderr', (valid) => ({ ...valid, stderr: 'TASK6_CHILD_STDERR_SENTINEL' })],
  ['process timeout', () => ({ deny: 'TASK6_CHILD_STDERR_SENTINEL' })],
  ['failure status extra', (valid) => ({ ...valid, exitCode: 2, stdout: '{"v":1,"ok":false,"code":"binding","status":404}' })],
  ['failure retry extra', (valid) => ({ ...valid, exitCode: 2, stdout: '{"v":1,"ok":false,"code":"unavailable","retry_after":1}' })],
]

for (const [name, failed] of badOutputs) {
  test(`remote ${name} is refused by the prompt after a real valid helper result`, async ($, on) => {
    let failing = false
    const seen = remote(on, { result: (input, valid) => failing && input.op === 'mask' ? failed(valid) : valid })
    await start($)
    expect((await $.prompt.submit({ text: 'positive SECRET-123', wait: false, origin: { kind: 'composer' } })).text).toBe('positive FAKE-999')
    const before = seen.calls.length
    failing = true
    const result = await $.prompt.submit({ text: 'withhold SECRET-123', wait: false, origin: { kind: 'composer' } })
    expect(result.drop).toBeTruthy()
    expect(result.text).toBeUndefined()
    expect(seen.calls.slice(before).map((input) => input.op)).toEqual(['mask'])
    noDiagnostics(seen, result.drop)
  })
}

test('the six controlled failure codes never release or retry a prompt', async ($, on) => {
  let code: string | undefined
  const seen = remote(on, { result: (input, valid) => code && input.op === 'mask'
    ? { ...valid, exitCode: 2, stdout: JSON.stringify({ v: 1, ok: false, code }) } : valid })
  await start($)
  expect((await $.prompt.submit({ text: 'positive SECRET-123', wait: false, origin: { kind: 'composer' } })).text).toBe('positive FAKE-999')
  for (code of ['bad_input', 'binding', 'unsupported', 'deadline', 'unavailable', 'response']) {
    const before = seen.calls.length
    const result = await $.prompt.submit({ text: 'withhold SECRET-123', wait: false, origin: { kind: 'composer' } })
    expect(result.drop).toBeTruthy()
    expect(seen.calls.slice(before).map((input) => input.op)).toEqual(['mask'])
    noDiagnostics(seen, result.drop)
  }
})

for (const verdict of [{}, null, [], { ok: null }, { ok: 0 }, { ok: 'true' }, { ok: false, reason: 'TASK6_RAW_OUTPUT_SENTINEL' }]) {
  test(`remote verdict ${JSON.stringify(verdict)} withholds every sensitive consumer`, async ($, on) => {
    const seen = remote(on, { verdict })
    await start($)
    const prompt = await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })
    expect(prompt.drop).toBeTruthy()
    expect(JSON.stringify((await $.prompt.section({ name: 'memory', text: 'SECRET-123' })).text)).not.toContain('SECRET-123')
    await $.session.append({ door: 'tool-result', origin: { kind: 'tool', tool: 'Bash' }, uuid: 'task6-refused-row', message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'SECRET-123' }] } })
    const tool = await $.tool.call({ tool: 'Write', file_path: 'task6.txt', content: 'FAKE-999' })
    expect(tool.deny).toBeTruthy()
    const outside = await $.tool.call({ tool: 'WebFetch', url: 'https://example.invalid/public', prompt: 'public' })
    expect(outside.deny).toBeTruthy()
    await $.ui.render({ surface: 'terminal', component: 'AssistantMessage', requestId: 'refused-render', props: { text: 'FAKE-999', isFirstOfReply: true } })
    await turn($)
    expect(seen.tools).toEqual([])
    expect(seen.providerCalls()).toBe(0)
    expect(JSON.stringify(seen.appended)).not.toContain('SECRET-123')
    expect(JSON.stringify(seen.screens)).not.toContain('SECRET-123')
    expect(seen.calls.some((input) => !['ping', 'route-check'].includes(input.op))).toBe(false)
    noDiagnostics(seen, prompt.drop)
  })
}

for (const source of ['project', 'local']) {
  test(`repository ${source} helper authority refuses before spawning`, async ($, on) => {
    const seen = remote(on, { layers: { [source]: { env: { LATET_BRIDGE_HELPER: JSON.stringify(HELPER) } } } })
    await start($)
    expect((await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })).drop).toBeTruthy()
    expect(seen.launches).toEqual([])
    noDiagnostics(seen)
  })
}

for (const source of SOURCES) {
  test(`failed ${source} inspection cannot use an otherwise valid remote binding`, async ($, on) => {
    const seen = remote(on, { failed: source })
    await start($)
    expect((await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })).drop).toBeTruthy()
    expect(seen.launches).toEqual([])
    noDiagnostics(seen)
  })
}

for (const source of ['flag', 'policy']) {
  test(`a complete trusted ${source} binding overrides a lower repository object`, async ($, on) => {
    const seen = remote(on, { layers: {
      project: { env: { LATET_BRIDGE_HELPER: '{"python":"TASK6_PROJECT_EXEC_SENTINEL"}' } },
      [source]: { env: { LATET_BRIDGE_HELPER: JSON.stringify(HELPER) } },
    } })
    await start($)
    expect((await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })).text).toBe('FAKE-999')
    expect(seen.launches.length).toBeGreaterThan(0)
    for (const launch of seen.launches) expect(launch.argv[0]).toBe(HELPER.python)
    noDiagnostics(seen)
  })
}

test('a higher partial helper object cannot borrow missing fields from a lower trusted object', async ($, on) => {
  const partial = JSON.stringify({ v: 1, cwd: HELPER.cwd, descriptor: HELPER.descriptor, env: HELPER.env })
  const seen = remote(on, { layers: { flag: { env: { LATET_BRIDGE_HELPER: partial } } }, effective: { LATET_BRIDGE_HELPER: partial } })
  await start($)
  expect((await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })).drop).toBeTruthy()
  expect(seen.launches).toEqual([])
  noDiagnostics(seen)
})

for (const name of ['LATET_BRIDGE_HELPER', 'LATET_BRIDGE_TOKEN']) {
  test(`missing ${name} never launches a credential-finding fallback`, async ($, on) => {
    const seen = remote(on, { layers: { user: { env: { ...AUTHORITY, [name]: '' } } }, effective: { [name]: '' } })
    await start($)
    expect((await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })).drop).toBeTruthy()
    expect(seen.launches).toEqual([])
    noDiagnostics(seen)
  })
}

for (const [name, env] of [
  ['extra key', { ...HELPER.env, TASK6_EXTRA_SELECTOR: 'TASK6_RAW_OUTPUT_SENTINEL' }],
  ['case alias', { ...HELPER.env, sslkeylogfile: '' }],
  ['nonempty TLS key log', { ...HELPER.env, SSLKEYLOGFILE: `${PROJECT}\\keys.log` }],
] as const) {
  test(`a trusted helper with ${name} violates the frozen map before process.run`, async ($, on) => {
    const value = JSON.stringify({ ...HELPER, env })
    const seen = remote(on, { layers: { user: { env: { ...AUTHORITY, LATET_BRIDGE_HELPER: value } } }, effective: { LATET_BRIDGE_HELPER: value } })
    await start($)
    expect((await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })).drop).toBeTruthy()
    expect(seen.launches).toEqual([])
    noDiagnostics(seen)
  })
}

// R6: each refusal says its cause, since what the person does differs.
for (const surface of ['print', null] as const) {
  test(`a ${surface ?? 'missing'} surface says print mode and the other surfaces are held back, before spawning`, async ($, on) => {
    const seen = remote(on)
    await $.session.start({ cwd: PROJECT, surface, isInteractive: false })
    const prompt = await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })
    expect(prompt.drop).toContain('print mode (claude -p)')
    expect(prompt.drop).toContain('interactive terminal')
    expect(seen.diagnostics.join(' ')).toContain('print mode (claude -p)')
    expect(seen.launches).toEqual([])
    noDiagnostics(seen, prompt.drop)
  })
}

test('a missing helper binding says `latet agents apply claude`, before spawning', async ($, on) => {
  const seen = remote(on, { layers: { user: { env: { ...AUTHORITY, LATET_BRIDGE_HELPER: '' } } }, effective: { LATET_BRIDGE_HELPER: '' } })
  await start($)
  const prompt = await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })
  expect(prompt.drop).toContain('latet agents apply claude')
  expect(seen.launches).toEqual([])
  noDiagnostics(seen, prompt.drop)
})

for (const [code, words] of [
  ['binding', 'latet agents apply claude'],
  ['unavailable', 'latet status'],
  ['deadline', 'latet status'],
  ['refused', 'no longer accepts this computer\'s token'],
  ['unsupported', 'needs a Windows terminal'],
  ['response', 'latet doctor'],
] as const) {
  test(`the helper's ${code} refusal is said with its cause, and the prompt withheld`, async ($, on) => {
    let failing = false
    const seen = remote(on, { result: (input, valid) => failing && input.op === 'mask'
      ? { ...valid, exitCode: 2, stdout: JSON.stringify({ v: 1, ok: false, code }) } : valid })
    await start($)
    failing = true
    const prompt = await $.prompt.submit({ text: 'withhold SECRET-123', wait: false, origin: { kind: 'composer' } })
    expect(prompt.text).toBeUndefined()
    expect(prompt.drop).toContain(words)
    noDiagnostics(seen, prompt.drop)
  })
}

test('a tool description the server cannot mask is withheld (R17)', async ($, on) => {
  let failing = false
  const seen = remote(on, { result: (input, valid) => failing && input.op === 'mask'
    ? { ...valid, exitCode: 2, stdout: JSON.stringify({ v: 1, ok: false, code: 'unavailable' }) } : valid })
  on('tool.describe', ($, e) => ({ description: e.description }))
  await start($)
  const masked = await $.tool.describe({ tool: 'mcp__crm__lookup', description: 'Looks up SECRET-123' })
  expect(masked.description).toBe('Looks up FAKE-999')
  failing = true
  const withheld = await $.tool.describe({ tool: 'mcp__crm__lookup', description: 'Looks up SECRET-123' })
  expect(withheld.description).not.toContain('SECRET-123')
  expect(withheld.description).toContain('withheld by Latet')
  noDiagnostics(seen)
})

test('on Linux and macOS a connected session says Claude Code needs a Windows terminal (R5, Minor 2)', async ($, on) => {
  const nothing = { LATET_BRIDGE_URL: SPACE, ANTHROPIC_BASE_URL: `${SPACE}/anthropic` } // no helper, no token: never written there
  const seen = remote(on, {
    layers: { user: { env: nothing } },
    effective: { LATET_BRIDGE_HELPER: undefined, LATET_BRIDGE_TOKEN: undefined, OS: undefined },
  })
  await start($)
  const prompt = await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })
  expect(prompt.drop).toContain('needs a Windows terminal')
  expect(prompt.drop).not.toContain('latet agents apply claude')
  expect(seen.launches).toEqual([])
  noDiagnostics(seen, prompt.drop)
})

test("a failure outside the helper says its own cause, never an earlier refusal's (Minor 9)", async ($, on) => {
  let failing = false, messagesFail = false
  const seen = remote(on, {
    result: (input, valid) => failing && input.op === 'mask'
      ? { ...valid, exitCode: 2, stdout: JSON.stringify({ v: 1, ok: false, code: 'refused' }) } : valid,
    messagesFail: () => messagesFail,
  })
  await start($)
  failing = true
  const prompt = await $.prompt.submit({ text: 'SECRET-123', wait: false, origin: { kind: 'composer' } })
  expect(prompt.drop).toContain('no longer accepts this computer')
  failing = false
  messagesFail = true
  const answer = (await turn($)).answer
  expect(answer).toContain('latet doctor')
  expect(answer).not.toContain('no longer accepts this computer')
  expect(seen.providerCalls()).toBe(0)
  noDiagnostics(seen, answer)
})
