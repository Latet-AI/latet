import { expect, mock, test } from 'claude-code/testing'

import { collectKeys, renameKeys, rowTexts } from '../hooks/walk.ts'

// A fake bridge: masks SECRET-123 as FAKE-999, restores it, and reports leaks of LEAK-1.
function bridge(
  on,
  calls: string[] = [],
  down: boolean | (() => boolean) = false,
  bodies: Record<string, unknown>[] = [],
  findings: unknown[] = [],
  verdict: Record<string, unknown> = { ok: true, reason: null, toast: null },
) {
  on('http.fetch', ($, e) => {
    const path = e.url.split('/_latet/engine/v1/')[1]
    calls.push(path)
    const body = e.init?.body ? JSON.parse(e.init.body) : {}
    bodies.push(body)
    if (typeof down === 'function' ? down() : down) return { deny: 'connection refused' }
    let reply: unknown = { ok: true }
    if (path === 'mask') {
      reply = {
        items: body.items.map((i) => {
          if (i.image) {
            // Images: SECRET-IMG comes back painted (MASKED-IMG), CLEAN-IMG as it was, BAD-IMG withheld.
            if (i.image.data === 'BAD-IMG') return { image: null, count: 0, withheld: 'it could not be read' }
            if (i.image.data === 'CLEAN-IMG') return { image: i.image, count: 0 }
            return { image: { data: 'MASKED-IMG', media_type: 'image/png' }, count: 1 }
          }
          return i.text.includes('STOP-ME')
            ? { text: '[withheld by Latet: no]', count: 0, blocked: 'the local reviewer could not read the user text' }
            : { text: i.text.replaceAll('SECRET-123', 'FAKE-999'), count: 1 }
        }),
      }
    }
    if (path === 'rehydrate') reply = { items: body.items.map((t) => t.replaceAll('FAKE-999', 'SECRET-123')), restored: 1 }
    if (path === 'ping') reply = { ok: true, version: '0.9.9' }
    if (path === 'outbound') {
      const fake = body.values.some((v) => v.includes('FAKE-999'))
      reply = fake ? { found: ['PERSON'], reason: `Latet stopped this ${body.tool} call.` } : { found: [], reason: null }
    }
    if (path === 'instructions') reply = { findings }
    if (path === 'route-check') reply = verdict
    if (path === 'verify') {
      const at = body.slots.findIndex((s) => s.text.includes('LEAK-1'))
      const blocking = at >= 0 ? [{ type: 'PASSWORD', kind: 'tool_result', preview: 'PASSWORD[6]', new: true }] : []
      if ((body.images ?? []).some((i) => i.data === 'SECRET-IMG')) blocking.push({ type: 'EMAIL', kind: 'image_text', preview: 'EMAIL[19]', new: true })
      reply = { blocking, flagged: [], inherited: at >= 0 && at < (body.history ?? 0) }
    }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(reply) } }
  })
}

test('a typed prompt is masked before Claude Code sends it', async ($, on) => {
  bridge(on)
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await startLocal($, on)
  const result = await $.prompt.submit({ text: 'my key is SECRET-123', wait: false })
  expect(result.text).toBe('my key is FAKE-999')
})

test('the prompt is held back when the daemon cannot be reached', async ($, on) => {
  let down = false
  const calls: string[] = []
  bridge(on, calls, () => down)
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await startLocal($, on, calls)
  down = true
  const result = await $.prompt.submit({ text: 'my key is SECRET-123', wait: false })
  expect(result.drop).toContain('Latet could not mask this prompt')
  expect(calls).toEqual(['mask'])
})

test('row texts are collected and put back in place (session.append)', async () => {
  const content = [
    { type: 'text', text: 'see the file' },
    { type: 'tool_result', tool_use_id: 't1', content: 'API_KEY=SECRET-123' },
    { type: 'tool_result', tool_use_id: 't2', content: [{ type: 'text', text: 'TOKEN=SECRET-123' }, { type: 'image' }] },
  ]
  const { texts, ids, rebuild } = rowTexts(content)
  expect(texts).toEqual(['see the file', 'API_KEY=SECRET-123', 'TOKEN=SECRET-123'])
  expect(ids).toEqual([undefined, 't1', 't2'])
  const out = rebuild(texts.map((t) => t.replaceAll('SECRET-123', 'FAKE-999')))
  expect(out[1].content).toBe('API_KEY=FAKE-999')
  expect(out[2].content[0].text).toBe('TOKEN=FAKE-999')
  expect(out[2].content[1]).toEqual({ type: 'image' })
  expect(content[1].content).toBe('API_KEY=SECRET-123') // the original is untouched
})

test('local tools receive the real values', async ($, on) => {
  bridge(on)
  let written = ''
  on('tool.call', ($, e) => {
    written = e.content
    return { result: 'ok' }
  })
  await startLocal($, on)
  await $.tool.call({ tool: 'Write', file_path: 'key.txt', content: 'FAKE-999' })
  expect(written).toBe('SECRET-123')
})

test('a local tool\'s input goes to the daemon marked as the model\'s own, and nothing on screen does', async ($, on) => {
  const calls: string[] = []
  const bodies: Record<string, unknown>[] = []
  bridge(on, calls, false, bodies)
  on('tool.call', () => ({ result: 'ok' }))
  on('ui.render', () => ({ type: 'Text' }))
  await startLocal($, on, calls, bodies)
  await $.tool.call({ tool: 'Write', file_path: 'key.txt', content: 'FAKE-999' })
  const call = bodies[calls.indexOf('rehydrate')]
  expect(call.model).toBe(true)
  expect(call.items).toEqual(['key.txt', 'FAKE-999']) // as the model wrote it: the daemon restores it
  calls.length = 0
  bodies.length = 0
  await $.ui.render({ surface: 'terminal', component: 'AssistantMessage', requestId: 'm1', props: { text: 'saved FAKE-999' } })
  expect(calls).toContain('rehydrate')
  expect(bodies[calls.indexOf('rehydrate')].model).toBeUndefined()
})

test('a key of a local tool\'s input that holds a stand-in gets the real value too', async ($, on) => {
  bridge(on)
  let fields: Record<string, unknown> = {}
  on('tool.call', ($, e) => {
    fields = e.fields
    return { result: 'ok' }
  })
  await startLocal($, on)
  await $.tool.call({ tool: 'mcp__crm__update', fields: { first: 1, 'FAKE-999': { role: 'FAKE-999' }, last: 2 } })
  expect(fields).toEqual({ first: 1, 'SECRET-123': { role: 'SECRET-123' }, last: 2 })
  expect(Object.keys(fields)).toEqual(['first', 'SECRET-123', 'last']) // in their order
})

test('keys are collected with the path of their object and renamed in place, the deepest first', async () => {
  const value = { args: { 'FAKE-999': { 'FAKE-999': 'x', b: 'y' }, list: [{ 'FAKE-999': 1 }] } }
  const keys = collectKeys(value)
  expect(keys.map((k) => [k.path, k.key])).toEqual([
    [['args'], 'FAKE-999'],
    [['args', 'FAKE-999'], 'FAKE-999'],
    [['args', 'FAKE-999'], 'b'],
    [['args'], 'list'],
    [['args', 'list', 0], 'FAKE-999'],
  ])
  const renamed = renameKeys(value, keys, keys.map((k) => k.key.replaceAll('FAKE-999', 'SECRET-123')))
  expect(renamed).toEqual({ args: { 'SECRET-123': { 'SECRET-123': 'x', b: 'y' }, list: [{ 'SECRET-123': 1 }] } })
  expect(Object.keys(value.args)).toEqual(['FAKE-999', 'list']) // the original is untouched
})

test('two keys that would be one once restored are left as the model wrote them, the others restored', async () => {
  const value = { args: { 'FAKE-999': 1, 'SECRET-123': 2, other: 3 }, more: { 'FAKE-999': 4 } }
  const keys = collectKeys(value)
  const renamed = renameKeys(value, keys, keys.map((k) => k.key.replaceAll('FAKE-999', 'SECRET-123')))
  expect(renamed).toEqual({ args: { 'FAKE-999': 1, 'SECRET-123': 2, other: 3 }, more: { 'SECRET-123': 4 } })
  expect(Object.keys(renamed.args)).toEqual(['FAKE-999', 'SECRET-123', 'other']) // nothing lost, in their order
})

test('web tools and subagents never get the real values back', async ($, on) => {
  const calls: string[] = []
  bridge(on, calls)
  const seen: string[] = []
  on('tool.call', ($, e) => {
    seen.push(e.url ?? e.prompt)
    return { result: 'ok' }
  })
  await startLocal($, on, calls)
  await $.tool.call({ tool: 'WebFetch', url: 'https://example.com/?q=public', prompt: 'x' })
  await $.tool.call({ tool: 'Task', prompt: 'look into FAKE-999' })
  expect(seen).toEqual(['https://example.com/?q=public', 'look into FAKE-999'])
  expect(calls).not.toContain('rehydrate')
})

test('a request that would leak is not sent', async ($, on) => {
  bridge(on)
  mock.clock(on)
  on('ui.toast', () => ({ value: undefined }))
  let sent = false
  on('turn.step', async function* ($, e) {
    sent = true
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await startLocal($, on, [], [], [{ role: 'user', content: [{ type: 'text', text: 'password LEAK-1' }] }])
  const stream = $.turn.step({ turnId: 't', index: 0, model: 'claude-test', messageCount: 1 })
  let step = await stream.next()
  while (step.done !== true) step = await stream.next()
  expect(sent).toBe(false)
  expect(step.value.answer).toContain('Latet stopped this request')
})

test('a conversation that began before Latet is told to start again', async ($, on) => {
  const bodies: Record<string, unknown>[] = []
  bridge(on, [], false, bodies)
  mock.clock(on)
  on('ui.toast', () => ({ value: undefined }))
  const old = [
    { role: 'user', content: [{ type: 'text', text: 'the db password is LEAK-1' }] },
    { role: 'assistant', content: [{ type: 'text', text: 'noted' }] },
  ]
  on('turn.step', async function* ($, e) {
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await startLocal($, on, [], bodies, [...old, { role: 'user', content: [{ type: 'text', text: 'hello again' }] }])
  const stream = $.turn.step({ turnId: 't2', index: 0, model: 'claude-test', messageCount: 3 })
  let step = await stream.next()
  while (step.done !== true) step = await stream.next()
  expect(step.value.answer).toContain('/clear')
  expect(step.value.answer).toContain('already held values in clear')
  const check = bodies.find((b) => Array.isArray(b.slots) && 'history' in b)
  expect(check?.history).toBe(2) // the two messages before the last one
})

test('a CLAUDE.md holding secrets is named when the session starts', async ($, on) => {
  const found = [
    { file: '/work/shop/CLAUDE.md', line: 6, type: 'URL_PASSWORD', length: 14 },
    { file: '/work/shop/CLAUDE.md', line: 9, type: 'EMAIL', length: 22 },
  ]
  bridge(on, [], false, [], found)
  mock.clock(on)
  const toasts: string[] = []
  on('env.get', () => ({ value: undefined }))
  on('session.id', () => ({ value: 's3' }))
  on('session.messages', () => ({ value: [] }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.toast', ($, e) => {
    toasts.push(String(e.message ?? e.text ?? e))
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work/shop', surface: null, isInteractive: false })
  const toast = toasts.join(' ')
  expect(toast).toContain('shop/CLAUDE.md')
  expect(toast).toContain('lines 6, 9')
  expect(toast).toContain('.env')
})

test('what the screen shows of tool calls carries the real values', async ($, on) => {
  bridge(on)
  const seen: Record<string, unknown> = {}
  on('ui.render', ($, e) => {
    seen[e.component] = e.props
    return { type: 'Text' }
  })
  await startLocal($, on)
  const call = { tool: 'Bash', input: { command: 'psql postgres://app:FAKE-999@db/shop' }, isRunning: true, isErrored: false, isInterrupted: false }
  await $.ui.render({ surface: 'terminal', component: 'ToolGroup', requestId: 'g1', props: { calls: [call], isActive: true, isExpanded: false } })
  expect(seen.ToolGroup.calls[0].input.command).toBe('psql postgres://app:SECRET-123@db/shop')
  await $.ui.render({
    surface: 'terminal', component: 'ToolResult', requestId: 't1',
    props: { tool_use_id: 't1', tool: 'Bash', output: { stdout: 'KEY=FAKE-999', stderr: '' }, isErrored: false },
  })
  expect(seen.ToolResult.output.stdout).toBe('KEY=SECRET-123')
  await $.ui.render({
    surface: 'terminal', component: 'ToolUse', requestId: 't2',
    props: { tool_use_id: 't2', tool: 'Bash', input: { command: 'echo FAKE-999' }, output: 'failed: FAKE-999', isRunning: false, isErrored: true, isInterrupted: false },
  })
  expect(seen.ToolUse.input.command).toBe('echo SECRET-123')
  expect(seen.ToolUse.output).toBe('failed: SECRET-123')
  await $.ui.render({
    surface: 'terminal', component: 'ToolUse', requestId: 't3',
    props: { tool_use_id: 't3', tool: 'mcp__crm__update', input: { fields: { 'FAKE-999': 'owner' } }, isRunning: false, isErrored: false, isInterrupted: false },
  })
  expect(seen.ToolUse.input.fields).toEqual({ 'SECRET-123': 'owner' }) // a key that holds a stand-in too
})

test('a daemon in a container is not started by the plugin', async ($, on) => {
  bridge(on, [], true)
  on('clock.sleep', () => ({ value: undefined }))
  const started: string[][] = []
  const toasts: string[] = []
  on('env.get', ($, e) => ({ value: e.name === 'LATET_AUTOSTART' ? '0' : undefined }))
  on('session.id', () => ({ value: 's1' }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('process.run', ($, e) => {
    started.push([...e.argv])
    return { value: { code: 0, stdout: '', stderr: '' } }
  })
  on('ui.toast', ($, e) => {
    toasts.push(String(e.message ?? e.text ?? e))
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
  expect(started).toEqual([])
  expect(toasts.join(' ')).toContain('docker start latet')
})

test('every call names the folder Claude Code runs in, for its project settings', async ($, on) => {
  const bodies: Record<string, unknown>[] = []
  bridge(on, [], false, bodies)
  on('env.get', () => ({ value: undefined }))
  on('session.id', () => ({ value: 's2' }))
  on('session.messages', () => ({ value: [] }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/work/acme', surface: null, isInteractive: false })
  await $.prompt.submit({ text: 'my key is SECRET-123', wait: false })
  const masked = bodies.find((b) => Array.isArray(b.items))
  expect(masked?.cwd).toBe('/work/acme')
  expect(masked?.session).toBe('s2')
})

test('a tool result is masked knowing the tool it comes from', async ($, on) => {
  const calls: string[] = []
  const bodies: Record<string, unknown>[] = []
  bridge(on, calls, false, bodies)
  on('tool.call', () => ({ result: 'ok' }))
  await startLocal($, on, calls, bodies)
  await $.tool.call({ tool: 'Bash', tool_use_id: 'toolu_1', command: 'psql -c "select * from users"' })
  const row = { type: 'tool_result', tool_use_id: 'toolu_1', content: ' id | email' }
  // Nothing beneath the plugin stores the row here: only what it sent the daemon matters.
  await $.session.append({ door: 'tool-result', message: { role: 'user', content: [row] } }).catch(() => undefined)
  expect(bodies[calls.indexOf('mask')]?.items).toEqual([{ text: ' id | email', kind: 'tool_result', tool: 'Bash' }])
})

test('a tool result is masked knowing the file it read or the address it fetched', async ($, on) => {
  const calls: string[] = []
  const bodies: Record<string, unknown>[] = []
  bridge(on, calls, false, bodies)
  on('tool.call', () => ({ result: 'ok' }))
  await startLocal($, on, calls, bodies)
  await $.tool.call({ tool: 'Read', tool_use_id: 'toolu_1', file_path: '/work/acme/data.json' })
  await $.tool.call({ tool: 'WebFetch', tool_use_id: 'toolu_2', url: 'https://crm.example.com/a', prompt: 'x' })
  const rows = [
    { type: 'tool_result', tool_use_id: 'toolu_1', content: '     1\t{"tier": "gold"}' },
    { type: 'tool_result', tool_use_id: 'toolu_2', content: 'page' },
  ]
  await $.session.append({ door: 'tool-result', message: { role: 'user', content: rows } }).catch(() => undefined)
  expect(bodies[calls.indexOf('mask')]?.items).toEqual([
    { text: '     1\t{"tier": "gold"}', kind: 'tool_result', tool: 'Read', path: '/work/acme/data.json' },
    { text: 'page', kind: 'tool_result', tool: 'WebFetch', path: 'https://crm.example.com/a' },
  ])
})

test('a prompt the local reviewer stopped is not sent', async ($, on) => {
  bridge(on)
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await startLocal($, on)
  const result = await $.prompt.submit({ text: 'please STOP-ME now', wait: false })
  expect(result.drop).toContain('the local reviewer could not read the user text')
})

// A fake bridge with the Live Reviewer on: a prompt waits, is asked about twice, then comes back as
// `decide` says (with VALUE masked, or stopped).
function liveBridge(on, decide: 'approve' | 'reject', calls: string[] = [], viewers = 1) {
  let polls = 0
  on('http.fetch', ($, e) => {
    const path = e.url.split('/_latet/engine/v1/')[1]
    calls.push(path)
    const body = e.init?.body ? JSON.parse(e.init.body) : {}
    let reply: unknown = { pending: 'b1', viewers }
    if (path === 'ping') reply = { ok: true, version: '0.9.9' }
    if (path === 'route-check') reply = { ok: true, reason: null, toast: null }
    if (path === 'instructions') reply = { findings: [] }
    if (path === 'approvals/wait') {
      expect(body.id).toBe('b1')
      polls += 1
      if (polls > 1) {
        const stopped = 'you did not approve it in the Live Reviewer'
        reply = {
          items: [
            decide === 'approve'
              ? { text: 'call Ada about PROJECT-1', count: 1 }
              : { text: `[withheld by Latet: ${stopped}]`, count: 0, blocked: stopped },
          ],
        }
      }
    }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(reply) } }
  })
}

test('with the Live Reviewer on, a prompt waits for you and leaves as you approved it', async ($, on) => {
  const calls: string[] = []
  liveBridge(on, 'approve', calls)
  const status: (string | undefined)[] = []
  on('ui.status', ($, e) => {
    status.push(e.text)
    return { value: undefined }
  })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await startLocal($, on, calls)
  const result = await $.prompt.submit({ text: 'call Ada about Orion', wait: false })
  expect(result.text).toBe('call Ada about PROJECT-1')
  expect(calls).toEqual(['mask', 'approvals/wait', 'approvals/wait'])
  expect(status).toEqual(['Latet: waiting for your approval in the dashboard', undefined])
})

test('a prompt you stop in the Live Reviewer is not sent', async ($, on) => {
  liveBridge(on, 'reject', [], 0)
  const toasts: string[] = []
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', ($, e) => {
    toasts.push(String(e.text))
    return { value: undefined }
  })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await startLocal($, on)
  const result = await $.prompt.submit({ text: 'call Ada about Orion', wait: false })
  expect(result.drop).toBe('Latet did not send this prompt: you did not approve it in the Live Reviewer.')
  expect(toasts.join(' ')).toContain('latet dashboard')
})

test('a web search with a stand-in in it is stopped, and the model is told why', async ($, on) => {
  const calls: string[] = []
  bridge(on, calls)
  let ran = ''
  on('tool.call', ($, e) => {
    ran = e.query
    return { result: 'results' }
  })
  await startLocal($, on, calls)
  const stopped = await $.tool.call({ tool: 'WebSearch', query: 'FAKE-999 biography' })
  expect(stopped.deny).toBe('Latet stopped this WebSearch call.')
  expect(ran).toBe('')
  await $.tool.call({ tool: 'WebSearch', query: 'antirez redis' })
  expect(ran).toBe('antirez redis') // nothing replaced in it: it runs, as written
  await $.tool.call({ tool: 'Task', prompt: 'look into FAKE-999' }) // a subagent stays here: not checked
  expect(calls.filter((c) => c === 'outbound')).toHaveLength(2)
})

// A session whose variables (and the settings file that set each) are these.
function session(on, env: Record<string, string>, sources: Record<string, Record<string, string>> = {}, messages: unknown[] = []) {
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('settings.read', ($, e) => ({ value: { env: sources[e.source] ?? {} } }))
  on('session.id', () => ({ value: 's-route' }))
  on('session.messages', () => ({ value: messages }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
}

// Content hooks run only after the real production startup has admitted this local route.
// Keep the action's original transport observations separate from startup's calls.
async function startLocal($, on, calls: string[] = [], bodies: Record<string, unknown>[] = [], messages: unknown[] = []) {
  session(on, { LATET_BRIDGE_URL: 'http://127.0.0.1:5283', LATET_AUTOSTART: '0' }, {}, messages)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  calls.length = 0
  bodies.length = 0
}

test('a project that sends Claude Code elsewhere stops its prompts, and says which file', async ($, on) => {
  const bodies: Record<string, unknown>[] = []
  const reason = "Claude Code sends its requests to x.example (ANTHROPIC_BASE_URL from this project's .claude/settings.json)"
  bridge(on, [], false, bodies, [], { ok: false, reason, toast: null })
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(String(e.message ?? e.text ?? e))
    return { value: undefined }
  })
  session(on, { ANTHROPIC_BASE_URL: 'https://x.example', LATET_BRIDGE_URL: 'http://127.0.0.1:5283' }, {
    project: { ANTHROPIC_BASE_URL: 'https://x.example' },
  })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const facts = bodies.find((b) => 'sources' in b) as { sources: Record<string, string>; env: Record<string, string> }
  expect(facts.sources.ANTHROPIC_BASE_URL).toBe('project')
  expect(facts.env.ANTHROPIC_BASE_URL).toBe('https://x.example')
  const result = await $.prompt.submit({ text: 'hello', wait: false })
  expect(result.drop).toContain('.claude/settings.json')
  expect(toasts.join(' ')).toContain('Prompts are stopped')
})

test('a bridge address named by the project is never called', async ($, on) => {
  const calls: string[] = []
  bridge(on, calls)
  on('ui.toast', () => ({ value: undefined }))
  session(on, { LATET_BRIDGE_URL: 'http://127.0.0.1:5283', ANTHROPIC_BASE_URL: 'http://127.0.0.1:5283/anthropic' }, {
    project: { LATET_BRIDGE_URL: 'http://127.0.0.1:5283' },
  })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const result = await $.prompt.submit({ text: 'my key is SECRET-123', wait: false })
  expect(calls).toEqual([])
  expect(result.drop).toContain('LATET_BRIDGE_URL')
})

test('a bridge away from this computer is never called', async ($, on) => {
  const calls: string[] = []
  bridge(on, calls)
  on('ui.toast', () => ({ value: undefined }))
  session(on, { LATET_BRIDGE_URL: 'https://evil.example', ANTHROPIC_BASE_URL: 'https://evil.example/anthropic' })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const result = await $.prompt.submit({ text: 'my key is SECRET-123', wait: false })
  expect(calls).toEqual([])
  expect(result.drop).toContain('not an address on this computer')
})

test('a request of a session that goes around Latet is not sent', async ($, on) => {
  bridge(on, [], false, [], [], { ok: false, reason: 'Claude Code sends its requests to api.anthropic.com directly', toast: null })
  on('ui.toast', () => ({ value: undefined }))
  session(on, { LATET_BRIDGE_URL: 'http://127.0.0.1:5283' })
  let sent = false
  on('turn.step', async function* ($, e) {
    sent = true
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
  const stream = $.turn.step({ turnId: 't-route', index: 0, model: 'claude-test', messageCount: 1 })
  let step = await stream.next()
  while (step.done !== true) step = await stream.next()
  expect(sent).toBe(false)
  expect(step.value.answer).toContain('api.anthropic.com')
})

test('Claude Desktop is told, not stopped', async ($, on) => {
  const toast = "Claude Desktop's Code tab sends requests itself"
  bridge(on, [], false, [], [], { ok: true, reason: null, toast })
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(String(e.message ?? e.text ?? e))
    return { value: undefined }
  })
  session(on, { LATET_BRIDGE_URL: 'http://127.0.0.1:5283', CLAUDE_CODE_ENTRYPOINT: 'claude-desktop' })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  const result = await $.prompt.submit({ text: 'my key is SECRET-123', wait: false })
  expect(result.text).toBe('my key is FAKE-999')
  expect(toasts.join(' ')).toContain('Claude Desktop')
})

// --- images ------------------------------------------------------------------------------------------

const PNG = (data: string) => ({ type: 'image', file: { base64: data, type: 'image/png', originalSize: 10 } })

test('a Read of an image gives the model the masked image', async ($, on) => {
  bridge(on)
  on('tool.call', () => ({ result: PNG('SECRET-IMG') }))
  await startLocal($, on)
  const answer = await $.tool.call({ tool: 'Read', file_path: 'shot.png' })
  expect(answer.result.file.base64).toBe('MASKED-IMG')
})

test('an image with nothing to mask is left as it is, and one that cannot be read is refused', async ($, on) => {
  bridge(on)
  let data = 'CLEAN-IMG'
  on('tool.call', () => ({ result: PNG(data) }))
  await startLocal($, on)
  expect((await $.tool.call({ tool: 'Read', file_path: 'a.png' })).result.file.base64).toBe('CLEAN-IMG')
  data = 'BAD-IMG'
  expect((await $.tool.call({ tool: 'Read', file_path: 'b.png' })).deny).toContain('it could not be read')
})

test('the images of an MCP tool are masked, a withheld one becomes a notice', async ($, on) => {
  bridge(on)
  const image = (data: string) => ({ type: 'image', source: { type: 'base64', media_type: 'image/png', data } })
  on('tool.call', () => ({ result: [{ type: 'text', text: 'page:' }, image('SECRET-IMG'), image('BAD-IMG')] }))
  await startLocal($, on)
  const answer = await $.tool.call({ tool: 'mcp__browser__screenshot' })
  expect(answer.result[1].source.data).toBe('MASKED-IMG')
  expect(answer.result[2]).toEqual({ type: 'text', text: '[image withheld by Latet: it could not be read]' })
})

test('an image result is refused when the daemon cannot mask it', async ($, on) => {
  let down = false
  on('http.fetch', ($, e) => {
    if (down) return { deny: 'connection refused' }
    const path = e.url.split('/_latet/engine/v1/')[1]
    if (path === 'ping') return { value: { status: 200, ok: true, headers: {}, text: '{"version":"0.9.9"}' } }
    if (path === 'route-check') return { value: { status: 200, ok: true, headers: {}, text: '{"ok":true}' } }
    if (path === 'instructions') return { value: { status: 200, ok: true, headers: {}, text: '{"findings":[]}' } }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ items: [], restored: 0 }) } }
  })
  on('tool.call', () => {
    down = true
    return { result: PNG('SECRET-IMG') }
  })
  await startLocal($, on)
  expect((await $.tool.call({ tool: 'Read', file_path: 'shot.png' })).deny).toContain('could not mask the images')
})

test('a pasted image is left out and a masked copy is offered to read', async ($, on) => {
  bridge(on)
  let kept: unknown[] = []
  on('tool.call', () => ({ result: PNG('never read from disk') }))
  on('session.append', ($, e, next) => {
    kept = e.message.content
    return next(e)
  })
  await startLocal($, on)
  const pasted = { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'SECRET-IMG' } }
  await $.session.append({
    message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'what is this?' }, pasted] },
    door: 'prompt',
    origin: { kind: 'person' },
    uuid: 'row-1',
  })
  expect(kept.some((b: any) => b.type === 'image')).toBe(false)
  const note = (kept[0] as any).text as string
  expect(note).toContain('what is this?')
  const path = /at (\S+\.png)/.exec(note)?.[1]
  expect(path).toContain('.latet')
  const copy = await $.tool.call({ tool: 'Read', file_path: path })
  expect(copy.result.file.base64).toBe('MASKED-IMG')
})

test('the final check reads the images of the turn too', async ($, on) => {
  bridge(on)
  mock.clock(on)
  on('ui.toast', () => ({ value: undefined }))
  const image = { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'SECRET-IMG' } }
  let sent = false
  on('turn.step', async function* ($, e) {
    sent = true
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await startLocal($, on, [], [], [{ role: 'user', content: [{ type: 'text', text: 'look' }, image] }])
  const stream = $.turn.step({ turnId: 'ti', index: 0, model: 'claude-test', messageCount: 1 })
  let step = await stream.next()
  while (step.done !== true) step = await stream.next()
  expect(sent).toBe(false)
  expect(step.value.answer).toContain('Latet stopped this request')
})

test('a bridge address named by the project gets nothing from any hook', async ($, on) => {
  const calls: string[] = []
  bridge(on, calls)
  on('ui.toast', () => ({ value: undefined }))
  session(on, { LATET_BRIDGE_URL: 'http://127.0.0.1:5283' }, { project: { LATET_BRIDGE_URL: 'http://127.0.0.1:5283' } })
  const seen: Record<string, unknown> = {}
  on('ui.render', ($, e) => {
    seen[e.component] = e.props
    return { type: 'Text' }
  })
  let ran = false
  on('tool.call', () => {
    ran = true
    return { result: 'ok' }
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  await $.ui.render({
    surface: 'terminal', component: 'ToolResult', requestId: 't1',
    props: { tool_use_id: 't1', tool: 'Bash', output: { stdout: 'KEY=FAKE-999', stderr: '' }, isErrored: false },
  })
  const call = await $.tool.call({ tool: 'Write', file_path: 'key.txt', content: 'FAKE-999' })
  expect(calls).toEqual([])
  expect(seen.ToolResult.output.stdout).toBe('KEY=FAKE-999') // shown as it is
  expect(ran).toBe(false)
  expect(call.deny).toBeTruthy()
})

// A daemon that answers the route check with `status`, and masks SECRET-123 as FAKE-999.
function checking(on, status: number, calls: string[] = []) {
  on('http.fetch', ($, e) => {
    const path = e.url.split('/_latet/engine/v1/')[1]
    calls.push(path)
    const body = e.init?.body ? JSON.parse(e.init.body) : {}
    if (path === 'route-check') return { value: { status, ok: status < 300, headers: {}, text: '{"error":"no"}' } }
    let reply: unknown = { ok: true, version: '0.9.9', findings: [] }
    if (path === 'mask') reply = { items: body.items.map((i) => ({ text: i.text.replaceAll('SECRET-123', 'FAKE-999'), count: 1 })) }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(reply) } }
  })
  on('ui.toast', () => ({ value: undefined }))
  session(on, { LATET_BRIDGE_URL: 'http://127.0.0.1:5283' })
  on('prompt.submit', ($, e) => ({ text: e.text }))
}

test('a route the daemon fails to check stops prompts', async ($, on) => {
  checking(on, 500)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const result = await $.prompt.submit({ text: 'my key is SECRET-123', wait: false })
  expect(result.drop).toContain('could not check')
})

test('an older daemon, without the check, lets prompts go', async ($, on) => {
  checking(on, 404)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const result = await $.prompt.submit({ text: 'my key is SECRET-123', wait: false })
  expect(result.text).toBe('my key is FAKE-999')
})

test('a session started while the daemon was away is checked at its first prompt', async ($, on) => {
  let up = false
  const calls: string[] = []
  on('http.fetch', ($, e) => {
    if (!up) return { deny: 'connection refused' }
    const path = e.url.split('/_latet/engine/v1/')[1]
    calls.push(path)
    const verdict = { ok: false, reason: 'Claude Code sends its requests to x.example', toast: null }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(path === 'route-check' ? verdict : { items: [] }) } }
  })
  on('clock.sleep', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  session(on, { LATET_BRIDGE_URL: 'http://127.0.0.1:5283', LATET_AUTOSTART: '0' })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const away = await $.prompt.submit({ text: 'hello', wait: false })
  expect(away.drop).toContain('could not mask') // held back, and checked again at the next prompt
  up = true
  const result = await $.prompt.submit({ text: 'hello', wait: false })
  expect(calls[0]).toBe('route-check')
  expect(result.drop).toContain('x.example')
})

test('the plugin says which file sets the entry point and a proxy, never the proxy itself', async ($, on) => {
  const bodies: Record<string, unknown>[] = []
  bridge(on, [], false, bodies)
  on('ui.toast', () => ({ value: undefined }))
  const set = { CLAUDE_CODE_ENTRYPOINT: 'claude-desktop', HTTPS_PROXY: 'http://me:pw@proxy.example:8080' }
  session(on, { LATET_BRIDGE_URL: 'http://127.0.0.1:5283', ...set }, { project: set })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const facts = bodies.find((b) => 'sources' in b) as { sources: Record<string, string>; env: Record<string, string> }
  expect(facts.sources.CLAUDE_CODE_ENTRYPOINT).toBe('project')
  expect(facts.sources.HTTPS_PROXY).toBe('project')
  expect(JSON.stringify(facts)).not.toContain('pw@')
})

test('a session whose route cannot even be read stops its prompts', async ($, on) => {
  bridge(on)
  on('ui.toast', () => ({ value: undefined }))
  on('env.get', ($, e) => (e.name === 'ANTHROPIC_BASE_URL' ? { deny: 'unreadable' } : { value: undefined }))
  on('session.id', () => ({ value: 's-route' }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const result = await $.prompt.submit({ text: 'my key is SECRET-123', wait: false })
  expect(result.drop).toContain('could not read where Claude Code sends')
})

test('a tool description is masked before Claude Code sends it', async ($, on) => {
  bridge(on)
  on('tool.describe', ($, e) => ({ description: e.description }))
  await startLocal($, on)
  const result = await $.tool.describe({ tool: 'mcp__crm__lookup', description: 'Looks up SECRET-123 in the CRM' })
  expect(result.description).toBe('Looks up FAKE-999 in the CRM')
})

test('a tool description that cannot be masked is withheld, never sent as it is (R17)', async ($, on) => {
  let down = false
  bridge(on, [], () => down)
  on('tool.describe', ($, e) => ({ description: e.description }))
  await startLocal($, on)
  down = true
  const result = await $.tool.describe({ tool: 'mcp__crm__lookup', description: 'Looks up SECRET-123 in the CRM' })
  expect(result.description).not.toContain('SECRET-123')
  expect(result.description).toContain('withheld by Latet')
})
