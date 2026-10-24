// Latet for Claude Code (client mode).
//
// Masks sensitive values inside Claude Code before a request is built, so the model
// only ever reads surrogates; restores the real values for local tools and on screen; and checks
// every request before it leaves. All detection happens in the local Latet daemon (the bridge).

import { apiImages, copyId, copyPath, pastedImages, toolImages, type MaskedPicture, type Picture } from './images.ts'
import { apiSlots, collectKeys, collectStrings, renameKeys, replaceStrings, rowTexts } from './walk.ts'
import type { EngineInterface } from 'claude-code'

const DEFAULT_BRIDGE = 'http://127.0.0.1:5283'
// Kept equal to plugin.json and the Python package by the release check.
const PLUGIN_VERSION = '0.9.8'
const API = '/_latet/engine/v1/'
// Tools whose arguments leave this machine (web, Anthropic-hosted connectors) or start another
// model conversation: they keep the surrogates the model wrote.
const KEEP_SURROGATES = /^(?:WebFetch|WebSearch|Task|Agent|SendMessage|mcp__claude_ai_.*)$/
// Of those, the ones that run outside this computer: given a stand-in, they would search for someone
// else, open the wrong page, or write to an address that does not exist. Subagents stay here.
const OUTSIDE = /^(?:WebFetch|WebSearch|mcp__claude_ai_.*)$/
// Fields of a tool.call event that are Claude Code's own, not the tool's arguments.
const PINNED = new Set(['tool', 'tool_use_id', 'agentId', 'turnId'])
// Rows the model produced, or that no request carries: nothing to mask.
const SKIP_DOORS = new Set(['response', 'compaction', 'notice'])
const WITHHELD = '[withheld by Latet: the local daemon did not answer, or failed on it, so this content was not sent]'
const RENDER_CACHE_LIMIT = 2000
const TOOL_NAMES_LIMIT = 2000
const COPIES_LIMIT = 50

let bridge = DEFAULT_BRIDGE
let token = ''
let session = ''
let cwd = '' // the folder Claude Code runs in: the daemon applies that project's settings
const verified = new Map<string, number>() // agent → messages already verified
const rendered = new Map<string, string>() // surrogate text → restored text, for redraws
// tool_use id → tool name: the daemon reads a tool's result by the tool (strict data mode leaves
// what Read returns alone)
const toolNames = new Map<string, string>()
// tool_use id → the file the tool read or the address it fetched: strict data mode covers a file or an
// address a rule names (`data_files`, `data_hosts`)
const toolSources = new Map<string, string>()
// The arguments of a tool call that say where its result comes from.
const SOURCE_ARGUMENTS = ['file_path', 'notebook_path', 'path', 'url'] as const
// Masked copies of images you pasted, by id: the Read tool gets them from here, never from a file.
const copies = new Map<string, Picture>()
// Whether this session's requests go through Latet (the daemon's verdict at start): when they do
// not, prompts are stopped, since nothing would check what leaves.
type Verdict = { ok: boolean; reason?: string | null; toast?: string | null }
let route: Verdict | null = null
// The session's route facts, while the daemon has not checked them (it was away at the start).
let unchecked: Record<string, unknown> | null = null
// Why Latet's own address cannot be trusted (a repository named it, or it is not on this computer):
// then nothing at all is sent to it, since it would receive every text and the token.
let untrusted: string | null = null
// The session start got as far as a verdict, or as facts to check later: otherwise prompts are stopped.
let routeKnown = false
const LOOPBACK = /^http:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):\d+\/?$/
const SOURCES = ['user', 'project', 'local', 'flag', 'policy'] as const // rising precedence
// Where each is set matters too, never its value: a proxy's address may hold a password.
const SET_ONLY = ['LATET_BRIDGE_URL', 'CLAUDE_CODE_ENTRYPOINT', 'ANTHROPIC_FOUNDRY_RESOURCE'] as const
const PROXIES = ['HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'] as const
// Why a connected computer's requests are withheld, each said with its cause (R6): what the person can do differs.
const REMOTE_FAILURE = 'Latet could not use its server for Claude Code, so requests are withheld: `latet doctor` says why'
const SURFACE = 'print mode (claude -p) and the other surfaces are not supported on a computer connected to a Latet server, so requests are withheld: use Claude Code\'s interactive terminal'
const BINDING = 'Latet\'s helper binding for Claude Code is missing or out of date, so requests are withheld: run `latet agents apply claude`, or start Claude Code with `latet claude`'
const UNANSWERED = 'the Latet server did not answer, so requests are withheld: `latet status` says why'
const TOKEN_REFUSED = 'the server no longer accepts this computer\'s token (revoked, or your identity disabled): `latet disconnect`, then `latet connect <url>` with a new code from an administrator'
const PLATFORM = 'Claude Code through a Latet server needs a Windows terminal in this version, so requests are withheld'
const CODE_WORDS: Record<string, string> = {
  bad_input: REMOTE_FAILURE, binding: BINDING, unsupported: PLATFORM, deadline: UNANSWERED,
  unavailable: UNANSWERED, refused: TOKEN_REFUSED, response: REMOTE_FAILURE,
}
const WITHHELD_REMOTE = '[withheld by Latet: its server did not mask this content, so it was not sent]'
const WITHHELD_DESCRIPTION = '[withheld by Latet: this tool\'s description could not be masked, so it was not sent]'
const INPUT_LIMIT = 4 * 1024 * 1024
const OUTPUT_LIMIT = INPUT_LIMIT - 1
const OPERATIONS = new Set(['ping', 'route-check', 'mask', 'rehydrate', 'scope', 'verify', 'instructions', 'approvals/wait', 'outbound'])
type Launch = { v: 1; python: string; cwd: string; descriptor: string; env: Record<string, string> }
let remote = false
let launch: Launch | null = null
let inspected = true
let helperSource: string | undefined
let helperSetting: unknown
let selectedBridge: unknown
// Why the session start's remote check or its first call failed, from the error caught there (``causeOf``).
let startCause = REMOTE_FAILURE

class RemoteRefusal extends Error {}
function refused(why: string = REMOTE_FAILURE): never { throw new RemoteRefusal(why) }
/** The words of a remote failure, from the error caught: a refusal's own cause, else the generic one (never another
 * hook's earlier cause). */
function causeOf(error: unknown): string { return error instanceof RemoteRefusal ? error.message : REMOTE_FAILURE }
/** Whether Claude Code runs on Windows, where its helper can be bound (E0c): Linux and macOS set neither variable. */
async function onWindows($): Promise<boolean> {
  try {
    return (await $.env.get('OS')) === 'Windows_NT' || Boolean(await $.env.get('SystemRoot'))
  } catch { return true } // unknown: the binding's own words, which name `latet agents apply claude`
}
function withheld(): string { return remote ? WITHHELD_REMOTE : WITHHELD }
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
function keys(value: Record<string, unknown>, names: string[]): boolean {
  return Object.keys(value).length === names.length && names.every((name) => Object.prototype.hasOwnProperty.call(value, name))
}
function byteLength(text: string): number { return new TextEncoder().encode(text).byteLength }

/** One bounded JSON value; duplicate decoded keys and nonfinite numbers are refused before use. */
function protocolJSON(text: string, limit: number): unknown {
  if (text.length > limit || byteLength(text) > limit) refused()
  let at = 0, nodes = 0
  const number = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y
  function space() { while (at < text.length && ' \t\r\n'.includes(text.charAt(at))) at++ }
  function string(): string {
    if (text[at] !== '"') refused()
    const begin = at++
    while (at < text.length) {
      if (text[at] === '\\') { at += 2; continue }
      if (text[at++] === '"') return JSON.parse(text.slice(begin, at))
    }
    return refused()
  }
  function value(depth: number): unknown {
    if (++nodes > 100000) refused()
    space()
    const first = text[at]
    if (first === '"') return string()
    if (first === '{' || first === '[') {
      if (++depth > 64) refused() // Containers, with the root container at level one.
      at++
      space()
      if (first === '[') {
        const result: unknown[] = []
        if (text[at] === ']') { at++; return result }
        while (true) {
          result.push(value(depth)); space()
          if (text[at] === ']') { at++; return result }
          if (text[at++] !== ',') refused()
        }
      }
      const result: Record<string, unknown> = Object.create(null)
      if (text[at] === '}') { at++; return result }
      while (true) {
        space()
        const key = string()
        if (Object.prototype.hasOwnProperty.call(result, key)) refused()
        space()
        if (text[at++] !== ':') refused()
        result[key] = value(depth); space()
        if (text[at] === '}') { at++; return result }
        if (text[at++] !== ',') refused()
      }
    }
    for (const [literal, result] of [['true', true], ['false', false], ['null', null]] as const) {
      if (text.startsWith(literal, at)) { at += literal.length; return result }
    }
    number.lastIndex = at
    const found = number.exec(text)
    if (!found) return refused()
    at = number.lastIndex
    const result = Number(found[0])
    if (!Number.isFinite(result)) refused()
    return result
  }
  const result = value(0)
  space()
  if (at !== text.length) refused()
  return result
}

/** Count before building the complete stdin string, including escaping and envelope overhead. */
function protocolInput(input: unknown): string {
  let bytes = 0, nodes = 0
  function charge(text: string) {
    if (text.length > INPUT_LIMIT || (bytes += byteLength(text)) > INPUT_LIMIT) refused()
  }
  function quoted(text: string) {
    if (text.length > INPUT_LIMIT) refused()
    charge(JSON.stringify(text))
  }
  function visit(value: unknown, depth: number) {
    if (++nodes > 100000) refused()
    if (typeof value === 'string') { quoted(value); return }
    if (value === null || typeof value === 'boolean') { charge(JSON.stringify(value)); return }
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) refused()
      charge(JSON.stringify(value)); return
    }
    if (!object(value) && !Array.isArray(value)) refused()
    if (++depth > 64) refused()
    if (Array.isArray(value)) {
      if (value.length > 100000) refused()
      charge('[')
      for (let index = 0; index < value.length; index++) {
        if (index) charge(',')
        visit(value[index], depth)
      }
      charge(']')
    } else {
      charge('{')
      Object.entries(value).forEach(([key, item], index) => {
        if (index) charge(',')
        quoted(key); charge(':'); visit(item, depth)
      })
      charge('}')
    }
  }
  visit(input, 0)
  const text = JSON.stringify(input)
  if (byteLength(text) > INPUT_LIMIT) refused()
  return text
}

function drivePath(path: unknown): path is string {
  return typeof path === 'string' && /^[A-Za-z]:\\/.test(path) &&
    !/[\x00-\x1f/<>"|?*:]/.test(path.slice(2)) &&
    path.slice(3).split('\\').every((part) => part !== '' && part !== '.' && part !== '..' && !/[. ]$/.test(part))
}

/** The initial candidate is the exact frozen Windows overlay, never an inherited env dump. */
function boundLaunch(raw: unknown): Launch {
  if (typeof raw !== 'string') return refused()
  const parsed = protocolJSON(raw, 16384)
  if (!object(parsed) || !keys(parsed, ['v', 'python', 'cwd', 'descriptor', 'env']) || parsed.v !== 1 ||
    !drivePath(parsed.python) || !drivePath(parsed.cwd) || !drivePath(parsed.descriptor) || !object(parsed.env)) return refused()
  const match = /^(.*\\connection\\claude-helper)\\(?:permanent\.json|session-[0-9a-f]{32}\.json)$/.exec(parsed.descriptor)
  if (!match || !match[1] || parsed.cwd !== `${match[1]}\\cwd`) return refused()
  const root = match[1], env = parsed.env
  const windows = env.SystemRoot
  if (!drivePath(windows)) return refused()
  const expected: Record<string, string> = {
    PATH: `${windows}\\System32`, SystemRoot: windows, WINDIR: windows,
    HOME: `${root}\\homes\\user`, USERPROFILE: `${root}\\homes\\user`,
    CODEX_HOME: `${root}\\homes\\codex`, CLAUDE_CONFIG_DIR: `${root}\\homes\\claude`,
    XDG_CONFIG_HOME: `${root}\\homes\\xdg`, APPDATA: `${root}\\homes\\roaming`, LOCALAPPDATA: `${root}\\homes\\local`,
    HOMEDRIVE: root.slice(0, 2), HOMEPATH: `${root.slice(2)}\\homes\\user`,
    TEMP: `${root}\\temp`, TMP: `${root}\\temp`, TMPDIR: `${root}\\temp`,
    LATET_KEYRING: 'off', PYTHON_KEYRING_BACKEND: 'keyring.backends.null.Keyring',
    PYTHONHOME: '', PYTHONPATH: '', PYTHONSTARTUP: '', PYTHONUSERBASE: '', PYTHONEXECUTABLE: '',
    __PYVENV_LAUNCHER__: '', __COMPAT_LAYER: '', SSL_CERT_FILE: '', SSL_CERT_DIR: '', SSLKEYLOGFILE: '',
    OPENSSL_CONF: '', OPENSSL_CONF_INCLUDE: '', OPENSSL_ENGINES: '', OPENSSL_MODULES: '', OPENSSL_TRACE: '',
    LATET_DATA_DIR: '', LATET_CONNECTION: '', LATET_CONFIG: '', LATET_SERVER_URL: '', LATET_SERVER_FINGERPRINT: '',
    LATET_SERVER_ADMIN_TOKEN: '', LATET_BRIDGE_URL: '', LATET_BRIDGE_TOKEN: '', LATET_BRIDGE_HELPER: '',
    LATET_TRUSTED_ACCOUNTS: '',
  }
  const folded = Object.keys(env).map((key) => key.toLowerCase())
  if (new Set(folded).size !== folded.length || !keys(env, Object.keys(expected)) ||
    Object.entries(expected).some(([key, value]) => env[key] !== value)) return refused()
  return { v: 1, python: parsed.python, cwd: parsed.cwd, descriptor: parsed.descriptor, env: expected }
}

function remoteSpace(address: string): string | null {
  if (/[\x00-\x20\x7f\\]/.test(address)) return null
  try {
    const parsed = new URL(address)
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash ||
      !/^\/u\/[a-z2-7]{26}\/?$/.test(parsed.pathname)) return null
    return parsed.origin + parsed.pathname.replace(/\/$/, '')
  } catch { return null }
}

async function helperCall($: EngineInterface, operation: string, body: Record<string, unknown> | null) {
  if (untrusted || !launch || !OPERATIONS.has(operation) || (operation !== 'ping' && operation !== 'route-check' && route?.ok !== true)) refused()
  const bound = launch
  const stdin = protocolInput({ v: 1, op: operation, bridge, token, body })
  try {
    // Recheck the bound names; the helper independently checks the actual private snapshot and installation.
    for (const [path, kind] of [[bound.python, 'file'], [bound.cwd, 'dir'], [bound.descriptor, 'file']] as const) {
      const stat = await $.fs.stat(path, { resolve: true })
      if (stat.kind !== kind || stat.isLink !== false || stat.realPath !== path ||
        (kind === 'file' && (!Number.isSafeInteger(stat.size) || stat.size <= 0)) ||
        (path === bound.descriptor && stat.size > 16384)) refused()
    }
    const result = await $.process.run([bound.python, '-I', '-B', '-m', 'latet.claude_bridge_helper', '--descriptor', bound.descriptor], {
      cwd: bound.cwd, env: { ...bound.env }, stdin, timeoutMs: 35000,
    })
    if (!result || typeof result.stdout !== 'string' || result.stderr !== '' ||
      result.isStdoutTruncated !== false || result.isStderrTruncated !== false) refused()
    const answer = protocolJSON(result.stdout, OUTPUT_LIMIT)
    if (!object(answer) || answer.v !== 1) refused()
    if (answer.ok === false) {
      if (!keys(answer, ['v', 'ok', 'code']) || result.exitCode !== 2 ||
        typeof answer.code !== 'string' || !Object.prototype.hasOwnProperty.call(CODE_WORDS, answer.code)) refused()
      return refused(CODE_WORDS[answer.code as string])
    }
    if (answer.ok !== true || result.exitCode !== 0 || !keys(answer, ['v', 'ok', 'response'])) refused()
    const response = answer.response
    if (operation === 'route-check') return response // checkRoute admits only the literal affirmative verdict.
    if (!object(response)) refused()
    if (operation === 'ping' && typeof response.version !== 'string') refused()
    if (operation === 'instructions' && !Array.isArray(response.findings)) refused()
    if (operation === 'verify' && (!Array.isArray(response.blocking) || !Array.isArray(response.flagged))) refused()
    if (operation === 'outbound' && (!Array.isArray(response.found) || !(response.reason === null || typeof response.reason === 'string'))) refused()
    if (operation === 'mask' || operation === 'approvals/wait') {
      if (response.pending !== undefined) {
        if (typeof response.pending !== 'string' || !response.pending ||
          (response.viewers !== undefined && (typeof response.viewers !== 'number' || !Number.isSafeInteger(response.viewers) || response.viewers < 0))) refused()
      } else if (!Array.isArray(response.items)) refused()
    }
    if (operation === 'rehydrate' && (!Array.isArray(response.items) || !Array.isArray(body?.items) ||
      response.items.length !== body.items.length || response.items.some((item) => typeof item !== 'string'))) refused()
    return response
  } catch (error) {
    if (error instanceof RemoteRefusal) throw error // its cause, said: never a raw child result or error
    return refused() // No raw child result/error, retry, shell, host fetch or local fallback.
  }
}

/** Claude Code's route as it runs: its variables (settings applied), and the settings file that set each. */
async function routeFacts($, e) {
  const env = {
    ANTHROPIC_BASE_URL: await $.env.get('ANTHROPIC_BASE_URL'),
    ANTHROPIC_BEDROCK_BASE_URL: await $.env.get('ANTHROPIC_BEDROCK_BASE_URL'),
    ANTHROPIC_VERTEX_BASE_URL: await $.env.get('ANTHROPIC_VERTEX_BASE_URL'),
    ANTHROPIC_FOUNDRY_BASE_URL: await $.env.get('ANTHROPIC_FOUNDRY_BASE_URL'),
    CLAUDE_CODE_USE_BEDROCK: await $.env.get('CLAUDE_CODE_USE_BEDROCK'),
    CLAUDE_CODE_USE_VERTEX: await $.env.get('CLAUDE_CODE_USE_VERTEX'),
    CLAUDE_CODE_USE_FOUNDRY: await $.env.get('CLAUDE_CODE_USE_FOUNDRY'),
    CLAUDE_CODE_USE_MANTLE: await $.env.get('CLAUDE_CODE_USE_MANTLE'),
  }
  const names = [...Object.keys(env), ...SET_ONLY, ...PROXIES]
  const sources: Record<string, string> = {}
  for (const source of SOURCES) {
    try {
      const settings = await $.settings.read({ source })
      if (!object(settings)) throw new Error(REMOTE_FAILURE)
      const block = settings.env === undefined ? {} : settings.env
      if (!object(block)) throw new Error(REMOTE_FAILURE)
      if (Object.values(block).some((value) => typeof value !== 'string')) inspected = false
      for (const name of names) if (name in block) sources[name] = source
      if (Object.prototype.hasOwnProperty.call(block, 'LATET_BRIDGE_URL')) selectedBridge = block.LATET_BRIDGE_URL
      if (Object.prototype.hasOwnProperty.call(block, 'LATET_BRIDGE_HELPER')) {
        helperSetting = block.LATET_BRIDGE_HELPER
        helperSource = source
      }
    } catch {
      inspected = false // Remote authority cannot be inferred from an unreadable or malformed layer.
    }
  }
  const entrypoint = await $.env.get('CLAUDE_CODE_ENTRYPOINT')
  const set = Object.fromEntries(Object.entries(env).filter(([, value]) => typeof value === 'string'))
  return { bridge, env: set, sources, entrypoint: entrypoint ?? null, surface: e.surface ?? null }
}

/** Why Latet's own address cannot be trusted: named by a repository, or not on this computer. */
function bridgeProblem(source: string | undefined): string | null {
  if (source === 'project' || source === 'local') {
    const file = source === 'project' ? '.claude/settings.json' : '.claude/settings.local.json'
    return `this project's ${file} sets LATET_BRIDGE_URL: Latet never takes its address from a repository`
  }
  if (!LOOPBACK.test(bridge) && !remoteSpace(bridge)) return 'LATET_BRIDGE_URL is not an address on this computer'
  return null
}

type Item = { text?: string; image?: Picture; kind: string; from_model?: boolean; json?: boolean; tool?: string }

/** The daemon answered, with an error. */
class BridgeError extends Error {
  constructor(
    readonly status: number,
    path: string,
  ) {
    super(`Latet bridge ${path}: HTTP ${status}`)
  }
}

async function bridgeCall($, path: string, body: Record<string, unknown>) {
  // Rendering can precede session.start; never use defaults or a previous session's transport.
  if (!routeKnown) refused()
  if (untrusted) throw new Error(`Latet: ${untrusted}`)
  if (remote) return helperCall($, path, { session, cwd, ...body })
  const response = await $.http.fetch(bridge + API + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-latet-token': token },
    body: JSON.stringify({ session, cwd, ...body }),
  })
  if (!response.ok) throw new BridgeError(response.status, path)
  return JSON.parse(response.text)
}

/**
 * The daemon's verdict on where the session's requests go. An older daemon (404) has no such check;
 * any other answer that is not a verdict stops prompts, since nothing says where they would go. Throws
 * while the daemon does not answer: the check is made again at the next prompt.
 */
async function checkRoute($, facts: Record<string, unknown>): Promise<Verdict | null> {
  if (remote) {
    const verdict = await bridgeCall($, 'route-check', facts)
    return object(verdict) && verdict.ok === true ? { ok: true } : { ok: false, reason: REMOTE_FAILURE }
  }
  let verdict: Verdict | null
  try {
    verdict = await bridgeCall($, 'route-check', facts)
  } catch (error) {
    if (!(error instanceof BridgeError)) throw error
    if (error.status === 404) return null
    verdict = { ok: false, reason: 'Latet could not check where Claude Code sends its requests (`latet doctor` says why)' }
  }
  if (verdict?.toast) $.ui.toast(`Latet: ${verdict.toast}.`)
  if (verdict?.ok === false) $.ui.toast(`Latet: ${verdict.reason}. Prompts are stopped.`)
  return verdict
}

type Masked = { text: string; blocked?: string }

const WAITING = 'Latet: waiting for your approval in the dashboard'
const NO_DASHBOARD = 'Latet: a text waits for your approval, and no dashboard is open: run `latet dashboard`.'
let toldNoDashboard = false

/** The daemon's masked items: a text, and why it was stopped when it was (the local reviewer, or you).
 *
 * With the Live Reviewer on, new texts wait for you in the dashboard: the daemon answers `pending` at
 * once, and is asked again until you decide (each call is held up to 20 s; a fetch gives up at 30). */
async function maskItems($, items: Item[]): Promise<Masked[]> {
  if (items.length === 0) return []
  let result = await bridgeCall($, 'mask', { items })
  if (result.pending) {
    $.ui.status(WAITING)
    try {
      while (result.pending) {
        if (!result.viewers && !toldNoDashboard) {
          toldNoDashboard = true
          $.ui.toast(NO_DASHBOARD)
        }
        if (result.viewers) toldNoDashboard = false
        result = await bridgeCall($, 'approvals/wait', { id: result.pending })
      }
    } finally {
      $.ui.status(undefined)
    }
  }
  if (remote && (result.items.length !== items.length || result.items.some((item: unknown) => !object(item) ||
    (typeof item.text !== 'string' && !Object.prototype.hasOwnProperty.call(item, 'image'))))) refused()
  return result.items
}

async function mask($, items: Item[]): Promise<string[]> {
  return (await maskItems($, items)).map((item) => item.text)
}

/** Images masked by the daemon: the text in them read, and painted over where values were. */
async function maskPictures($, pictures: Picture[], kind: string, tool?: string): Promise<MaskedPicture[]> {
  const items: Item[] = pictures.map((image) => (tool ? { image, kind, tool } : { image, kind }))
  return (await maskItems($, items)) as unknown as MaskedPicture[]
}

/** A tool's result with its images masked; a Read of an image that was withheld is denied. */
async function maskToolResult($, tool: string, answer) {
  if (!answer || 'deny' in answer || answer.result === undefined) return answer
  const found = toolImages(answer.result)
  if (found === null) return answer
  const masked = await maskPictures($, found.pictures, 'tool_result', tool)
  if (masked.every((m, i) => m.image?.data === found.pictures[i].data)) return answer
  return found.rebuild(masked)
}

async function digest(data: string): Promise<string> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(data))
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 12)
}

/** Keep a masked copy of a pasted image for the Read tool, and say where it is. */
async function offerCopy(image: Picture): Promise<string> {
  const id = await digest(image.data)
  copies.set(id, image)
  if (copies.size > COPIES_LIMIT) copies.delete(copies.keys().next().value)
  return copyPath(cwd || '.', id, image.media_type)
}

/** Texts with the real values. `model`: a tool call's input as the model wrote it, which the next request carries back
 * as it is: the daemon's final check passes it as the model's own words. */
async function restore($, texts: string[], json = false, model = false): Promise<string[]> {
  if (texts.length === 0) return []
  const result = await bridgeCall($, 'rehydrate', model ? { items: texts, json, model } : { items: texts, json })
  return result.items
}

/** The daemon's version, or null when it does not answer. */
async function ping($): Promise<string | null> {
  if (untrusted) return null
  try {
    if (remote) {
      const response = await helperCall($, 'ping', null)
      return object(response) && typeof response.version === 'string' ? response.version : null
    }
    const response = await $.http.fetch(bridge + API + 'ping', { headers: { 'x-latet-token': token } })
    if (!response.ok) return null
    return String(JSON.parse(response.text).version ?? '')
  } catch (error) {
    startCause = causeOf(error)
    return null
  }
}

async function ensureDaemon($, autostart: boolean): Promise<string | null> {
  const running = await ping($)
  if (running !== null) return running
  if (remote) return null
  // Hooks have 10 s: launch without waiting, then watch for it briefly. A daemon in a container
  // (LATET_AUTOSTART=0) is started there, not by the plugin.
  if (autostart) {
    try {
      await $.process.run(['latet', 'start', '--no-wait'], { timeoutMs: 6000 })
    } catch {
      return null
    }
  }
  for (let i = 0; i < (autostart ? 10 : 4); i++) {
    const version = await ping($)
    if (version !== null) return version
    await $.clock.sleep(250)
  }
  return null
}

async function restoreForScreen($, text: string): Promise<string> {
  const hit = rendered.get(text)
  if (hit !== undefined) return hit
  const [out] = await restore($, [text])
  if (rendered.size >= RENDER_CACHE_LIMIT) rendered.clear()
  rendered.set(text, out)
  return out
}

/**
 * A tool row's `input` and `output` (or a group's call) with the real values, for the screen only:
 * the stored call and result keep what the model reads. A top-level string, such as the text of an
 * errored call, is a value too.
 */
async function restoredForScreen<T extends Record<string, unknown>>($, props: T, keys: readonly string[]): Promise<T> {
  const parts = keys.filter((key) => props[key] !== undefined && props[key] !== null)
  const leaves = parts.flatMap((key) => {
    const value = props[key]
    return typeof value === 'string' ? [{ key, leaf: { path: [], value } }] : collectStrings(value).map((leaf) => ({ key, leaf }))
  })
  // The keys of the objects inside each part as well: a key can hold a value.
  const named = parts.flatMap((key) => (typeof props[key] === 'string' ? [] : collectKeys(props[key]).map((leaf) => ({ key, leaf }))))
  if (leaves.length === 0 && named.length === 0) return props
  const restored = await restore($, [...leaves.map((x) => x.leaf.value), ...named.map((x) => x.leaf.key)])
  const names = restored.slice(leaves.length)
  if (restored.every((value, i) => value === (i < leaves.length ? leaves[i].leaf.value : named[i - leaves.length].leaf.key))) {
    return props
  }
  const out: Record<string, unknown> = { ...props }
  for (const key of parts) {
    const mine = leaves.flatMap((x, i) => (x.key === key ? [i] : []))
    const value = props[key]
    if (typeof value === 'string') {
      out[key] = restored[mine[0]]
      continue
    }
    const keyed = named.flatMap((x, i) => (x.key === key ? [i] : []))
    const replaced = replaceStrings(value, mine.map((i) => leaves[i].leaf), mine.map((i) => restored[i]))
    out[key] = renameKeys(replaced, keyed.map((i) => named[i].leaf), keyed.map((i) => names[i]))
  }
  return out as T
}

function blockedAnswer(blocking: Array<{ type: string; kind: string }>, inherited: boolean): string {
  const what = [...new Set(blocking.map((b) => `${b.type} (${b.kind.replace('_', ' ')})`))].join(', ')
  if (inherited) {
    return (
      `Latet stopped this request before it left your machine: this conversation already held values in clear ` +
      `before Latet checked it (${what}). It began before Latet was on, or was resumed from then, and every ` +
      'request sends that history again. Start a new conversation with /clear: from there everything is masked as ' +
      'it comes in. Nothing was sent.'
    )
  }
  return (
    `Latet stopped this request before it left your machine: ${what} would have been sent in clear. ` +
    'Nothing was sent. Run `latet dashboard` to see where it was, and `latet doctor` to see why it was not masked.'
  )
}

/** What the CLAUDE.md files hold that auto mode's reviewer would get unmasked, in one toast. */
function instructionsWarning(found: Array<{ file: string; line: number; type: string }>): string {
  const names = [...new Set(found.map((f) => f.file.split(/[\\/]/).slice(-2).join('/')))].join(', ')
  const lines = found.slice(0, 4).map((f) => f.line).join(', ') + (found.length > 4 ? '…' : '')
  return (
    `Latet: ${names} holds secrets or other people's data (lines ${lines}). Claude Code gives auto mode's ` +
    'reviewer that file unmasked, so its reviews will be stopped: move them to .env. `latet doctor` lists them.'
  )
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    routeKnown = false
    bridge = (await $.env.get('LATET_BRIDGE_URL')) || DEFAULT_BRIDGE
    token = (await $.env.get('LATET_BRIDGE_TOKEN')) || ''
    session = await $.session.id()
    cwd = typeof e.cwd === 'string' ? e.cwd : ''
    const autostart = (await $.env.get('LATET_AUTOSTART')) !== '0'
    route = null
    unchecked = null
    untrusted = null
    remote = !LOOPBACK.test(bridge)
    startCause = REMOTE_FAILURE
    launch = null
    inspected = true
    helperSource = undefined
    helperSetting = undefined
    selectedBridge = undefined
    const facts = await routeFacts($, e)
    untrusted = bridgeProblem(facts.sources.LATET_BRIDGE_URL)
    if (remote && !untrusted) {
      try {
        const effective = await $.env.get('LATET_BRIDGE_HELPER')
        const source = facts.sources.LATET_BRIDGE_URL
        if (e.surface !== 'terminal') refused(SURFACE) // T20.8: the helper runs on the interactive terminal alone
        if (!inspected || selectedBridge !== bridge ||
          typeof source !== 'string' || !['user', 'flag', 'policy'].includes(source)) refused()
        if (!helperSource || !['user', 'flag', 'policy'].includes(helperSource) ||
          effective !== helperSetting || !/^latet_pat_[A-Za-z0-9_-]{43}$/.test(token)) {
          refused((await onWindows($)) ? BINDING : PLATFORM) // no binding is ever made on Linux or macOS (R5)
        }
        try {
          launch = boundLaunch(helperSetting)
        } catch { refused(BINDING) }
        const canonical = remoteSpace(bridge)
        if (!canonical) refused()
        bridge = canonical
      } catch (error) { untrusted = causeOf(error) }
    }
    routeKnown = true // from here on: a verdict, or facts the first prompt checks
    if (untrusted) {
      route = { ok: false, reason: untrusted }
      $.ui.toast(`Latet: ${untrusted}. Prompts are stopped.`)
      return next(e)
    }
    const version = await ensureDaemon($, autostart)
    if (version === null) {
      unchecked = facts
      $.ui.toast(
        remote ? `Latet: ${startCause}.` : autostart
          ? 'Latet: the local daemon is not running; requests will be withheld until it is.'
          : 'Latet: the daemon is not answering; start its container (docker start latet). Requests are withheld until then.',
      )
    } else {
      if (!remote && version && version !== PLUGIN_VERSION) {
        $.ui.toast(`Latet: the plugin is ${PLUGIN_VERSION} and the daemon ${version}; update the older one.`)
      }
      try {
        route = await checkRoute($, facts)
      } catch {
        unchecked = facts // the daemon stopped answering: checked at the first prompt
      }
      if (remote && route?.ok !== true) return next(e)
      // A resumed conversation is already in surrogate space: let the daemon scope its values.
      const messages = await $.session.messages({ as: 'api' })
      const texts = apiSlots(messages).map((slot) => slot.text)
      try {
        if (texts.length > 0) await bridgeCall($, 'scope', { items: texts })
      } catch (error) {
        startCause = causeOf(error)
        throw error
      }
      try {
        const check = await bridgeCall($, 'instructions', {})
        const found = Array.isArray(check.findings) ? check.findings : []
        if (found.length > 0) $.ui.toast(instructionsWarning(found))
      } catch {
        // An older daemon has no such check: nothing to say.
      }
    }
    return next(e)
  }).catch(async ($, e, next) => {
    // Never hold the session back: masking hooks fail closed on their own if the daemon is away.
    if (!routeKnown) route = { ok: false, reason: 'Latet could not read where Claude Code sends its requests' }
    $.ui.toast(remote ? `Latet: ${startCause}.` : 'Latet: the local daemon did not answer; requests will be withheld until it does.')
    return next.called ? undefined : next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    try {
      if (unchecked && !untrusted) {
        route = await checkRoute($, unchecked)
        unchecked = null
      }
      if (route?.ok === false) return { drop: `Latet did not send this prompt: ${route.reason}.` }
      const items: Item[] = [{ text: e.text, kind: 'user_text' }]
      for (const extra of e.context ?? []) items.push({ text: extra, kind: 'attachment' })
      const masked = await maskItems($, items)
      const stopped = masked.find((item) => item.blocked)
      if (stopped) return { drop: `Latet did not send this prompt: ${stopped.blocked}.` }
      const [text, ...context] = masked.map((item) => item.text)
      return next(e.context ? { ...e, text, context } : { ...e, text })
    } catch (error) {
      if (remote && !next.called) return { drop: `Latet did not send this prompt: ${causeOf(error)}.` }
      throw error
    }
  }).catch(async ($, e, next) =>
    next.called ? next(e) : {
      drop: remote
        ? `Latet did not send this prompt: ${REMOTE_FAILURE}.`
        : 'Latet could not mask this prompt: the local daemon did not answer, or failed on it (its log says why). Run `latet doctor`.',
    },
  )

  on('prompt.section', async ($, e, next) => {
    const result = await next(e)
    if (!result.text) return result
    const [text] = await mask($, [{ text: result.text, kind: 'system' }])
    return { text }
  }).catch(async () => ({ text: withheld() }))

  on('prompt.context', async ($, e, next) => {
    const result = await next(e)
    const texts = await mask($, result.blocks.map((b) => ({ text: b.text, kind: 'system' })))
    return { ...result, blocks: result.blocks.map((b, i) => ({ ...b, text: texts[i] })) }
  }).catch(async ($, e, next) => {
    const result = next.called ? await next(e) : e
    return { ...result, blocks: result.blocks.map((b) => ({ ...b, text: b.text ? withheld() : b.text })) }
  })

  on('prompt.attachment', async ($, e, next) => {
    const result = await next(e)
    if (!result.text) return result
    const [text] = await mask($, [{ text: result.text, kind: 'attachment' }])
    return { text }
  }).catch(async () => ({ text: withheld() }))

  on('skill.prompt', async ($, e, next) => {
    const result = await next(e)
    if (!result.text) return result
    const [text] = await mask($, [{ text: result.text, kind: 'system' }])
    return { text }
  }).catch(async () => ({ text: withheld() }))

  on('tool.describe', async ($, e, next) => {
    const result = await next(e)
    const [description] = await mask($, [{ text: result.description, kind: 'tool_description' }])
    return { ...result, description }
  }).catch(async ($, e) => ({
    // A description that could not be masked is withheld, as every other masking hook does (R17): never sent as it is.
    description: WITHHELD_DESCRIPTION,
    ...(e.isDeferred === true ? { isDeferred: true } : {}),
  }))

  on('session.append', async ($, e, next) => {
    if (SKIP_DOORS.has(e.door) || !e.message.role) return next(e)
    const pasted = e.door === 'prompt' ? pastedImages(e.message.content) : null
    if (pasted !== null) {
      // An image you pasted cannot be changed, only left out: a masked copy is offered to read instead.
      const masked = await maskPictures($, pasted.pictures, 'user_text')
      const offered = await Promise.all(masked.map((m) => (m.image && m.count > 0 ? offerCopy(m.image) : null)))
      e = { ...e, message: { ...e.message, content: pasted.rebuild(masked, offered) } }
    }
    const { texts, ids, rebuild } = rowTexts(e.message.content)
    if (texts.length === 0) return next(e)
    const kind = e.door === 'tool-result' ? 'tool_result' : e.door === 'prompt' ? 'user_text' : 'attachment'
    const masked = await mask(
      $,
      texts.map((text, i) => {
        const tool = kind === 'tool_result' ? toolNames.get(ids[i] ?? '') : undefined
        const path = kind === 'tool_result' ? toolSources.get(ids[i] ?? '') : undefined
        return tool ? { text, kind, tool, ...(path ? { path } : {}) } : { text, kind }
      }),
    )
    return next({ ...e, message: { ...e.message, content: rebuild(masked) } })
  }).catch(async ($, e, next) => {
    if (next.called) return next(e)
    const pasted = e.door === 'prompt' ? pastedImages(e.message.content) : null
    const content = pasted ? pasted.rebuild(pasted.pictures.map(() => ({ image: null, count: 0, withheld: 'the local daemon did not answer' })), []) : e.message.content
    const { texts, rebuild } = rowTexts(content)
    return next({ ...e, message: { ...e.message, content: rebuild(texts.map(() => withheld())) } })
  })

  on('tool.call', async ($, e, next) => {
    try {
      if (typeof e.tool_use_id === 'string' && typeof e.tool === 'string') {
        toolNames.set(e.tool_use_id, e.tool)
        if (toolNames.size > TOOL_NAMES_LIMIT) toolNames.delete(toolNames.keys().next().value)
        const source = SOURCE_ARGUMENTS.map((key) => (e as Record<string, unknown>)[key]).find((value) => typeof value === 'string' && value !== '')
        if (typeof source === 'string') {
          toolSources.set(e.tool_use_id, source)
          if (toolSources.size > TOOL_NAMES_LIMIT) toolSources.delete(toolSources.keys().next().value)
        }
      }
      const copy = e.tool === 'Read' ? copies.get(copyId(e.file_path) ?? '') : undefined
      if (copy) return { result: { type: 'image', file: { base64: copy.data, type: copy.media_type, originalSize: copy.data.length } } }
      if (KEEP_SURROGATES.test(e.tool)) {
        if (OUTSIDE.test(e.tool)) {
          const values = collectStrings(e, PINNED).map((leaf) => leaf.value)
          const check = values.length > 0 ? await bridgeCall($, 'outbound', { tool: e.tool, values }) : null
          if (check?.reason) return { deny: check.reason }
        }
        return maskToolResult($, e.tool, await next(e))
      }
      // The strings of its arguments, and the keys of the objects among them: a key the model wrote with a stand-in in it
      // gets its real value back too.
      const leaves = collectStrings(e, PINNED)
      const keys = collectKeys(e)
      if (leaves.length === 0 && keys.length === 0) return maskToolResult($, e.tool, await next(e))
      const restored = await restore($, [...leaves.map((leaf) => leaf.value), ...keys.map((leaf) => leaf.key)], false, true)
      const values = restored.slice(0, leaves.length)
      const names = restored.slice(leaves.length)
      if (values.every((value, i) => value === leaves[i].value) && names.every((name, i) => name === keys[i].key)) {
        return maskToolResult($, e.tool, await next(e))
      }
      return maskToolResult($, e.tool, await next(renameKeys(replaceStrings(e, leaves, values), keys, names)))
    } catch (error) {
      if (remote) return { deny: `Latet withheld this tool call: ${causeOf(error)}.` }
      throw error
    }
  }).catch(async ($, e, next) =>
    remote
      ? { deny: `Latet withheld this tool call: ${REMOTE_FAILURE}.` }
      : next.called
        ? { deny: 'Latet could not mask the images of this tool\'s result: the local daemon did not answer, or failed on it.' }
        : { deny: 'Latet could not restore the real values for this tool call: the local daemon did not answer, or failed on it.' },
  )

  on('ui.render', { component: ['AssistantMessage', 'UserMessage'] }, async ($, e, next) => {
    const text = await restoreForScreen($, e.props.text)
    return text === e.props.text ? next(e) : next({ ...e, props: { ...e.props, text } })
  }).catch(async ($, e, next) => (next.called ? undefined : next(e)))

  // What you see carries the real values: the command a tool runs, what it printed, and a folded
  // group of calls while it runs. The model's own copy keeps the surrogates.
  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const props = await restoredForScreen($, e.props, ['input', 'output'])
    return props === e.props ? next(e) : next({ ...e, props })
  }).catch(async ($, e, next) => (next.called ? undefined : next(e)))

  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => {
    const props = await restoredForScreen($, e.props, ['output'])
    return props === e.props ? next(e) : next({ ...e, props })
  }).catch(async ($, e, next) => (next.called ? undefined : next(e)))

  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    const calls = await Promise.all(e.props.calls.map((call) => restoredForScreen($, call, ['input', 'output'])))
    return calls.every((call, i) => call === e.props.calls[i]) ? next(e) : next({ ...e, props: { ...e.props, calls } })
  }).catch(async ($, e, next) => (next.called ? undefined : next(e)))

  on('turn.step', async function* ($, e, next) {
    try {
      if (unchecked && !untrusted) {
        route = await checkRoute($, unchecked)
        unchecked = null
      }
      if (route?.ok === false) {
        const answer = `Latet did not send this request: ${route.reason}.`
        return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: null, usage: null }
      }
      const agent = e.agentId ?? ''
      const answer = e.agentId ? await $.session.messages({ as: 'api', agentId: e.agentId }) : await $.session.messages({ as: 'api' })
      const messages = Array.isArray(answer) ? answer : (answer?.messages ?? [])
      const from = verified.get(agent) ?? 0
      const fresh = messages.length >= from ? messages.slice(from) : messages // compaction: check everything
      // The first check of this conversation here: what came before its last message is the history
      // Claude Code had when Latet started checking (from before Latet was on, if it holds values in clear).
      const history = from === 0 && messages.length > 1 ? apiSlots(messages.slice(0, -1)).length : 0
      const result = await bridgeCall($, 'verify', { slots: apiSlots(fresh), images: apiImages(fresh), history })
      if (result.blocking.length > 0) {
        $.ui.toast(
          result.inherited
            ? 'Latet: this conversation began before Latet was on. Start a new one with /clear.'
            : 'Latet blocked a request: a sensitive value would have been sent in clear.',
        )
        const answer = blockedAnswer(result.blocking, result.inherited === true)
        return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: null, usage: null }
      }
      verified.set(agent, messages.length)
    } catch (error) {
      if (!remote) throw error
      const answer = `Latet did not send this request: ${causeOf(error)}.`
      return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: null, usage: null }
    }
    return yield* next(e)
  }).catch(async function* ($, e) {
    return {
      turnId: e.turnId,
      index: e.index,
      answer: remote
        ? `Latet did not send this request: ${REMOTE_FAILURE}.`
        : 'Latet could not check this request (the local daemon did not answer, or failed on it), so it was not sent. Run `latet doctor`.',
      toolUses: [],
      stopReason: null,
      usage: null,
    }
  })
}
