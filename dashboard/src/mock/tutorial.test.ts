import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import i18n from '../i18n'
import type { ApprovalMask, Batch, ConfigView, SettingsChange, Slot, TutorialChapter, TutorialState } from '../api/types'
import { keepInClear } from '../lib/fields'
import { reviewsOf } from '../lib/reviewer'
import {
  autoSends,
  conflictState,
  initialView,
  nextBody,
  openChapter,
  phaseOf,
  primaryAction,
  targetOf,
  viewReducer,
  visibleResult,
  type OpenApi,
  type Opened,
  type View,
} from '../lib/tutorial'
import type { Decision } from './approvals'
import { mockColumns } from './columns'
import { detailOf, initialConfig, MOCK_ASSETS, type MockExchange } from './fixtures'
import { BUSY_MS, MockTutorial, reviewRecordOf, sandboxBase, STEP_FAILED, type TutorialHost } from './tutorial'

/** The live chapter's customer: no rule knows her name, which the user masks by hand. */
const CUSTOMER = 'Emily Carter'
const LIVE_TEXT = `draft a reply to ${CUSTOMER}, she was charged twice. her address is emily.carter@acme.com`

/** The local reviewer's chapter: the four values of the ticket that no pattern finds, and what the guidance is for. */
const PATIENT = 'Daniel Okafor'
const CONDITION = 'type 2 diabetes'
const WRITTEN = 'daniel dot okafor at stmarys-health dot org'
const ACCOUNT = 'GB82 WEST 1234 5698 7654 23'
const GUIDANCE = "Ward names are confidential: in this hospital, a ward's name says who is where."
/** What the sandbox's fixed key makes of each (the chapter's e2e test pins the same). */
const PATIENT_FAKE = 'Thomas Adams'
const CONDITION_FAKE = 'etzx 6 mslookdz'
const WRITTEN_FAKE = 'laura dot turner at southridge-ebf9 dot example'
const ACCOUNT_FAKE = 'GT61 MRIP 5646 5638 4242 65'

/** The screenshots' chapter: the three values drawn in the picture (an e-mail address, an IBAN, a phone number). */
const SHOT_EMAIL = 'amelia.hughes@acme.com'
const SHOT_IBAN = 'GB82 WEST 1234 5698 7654 32'
const SHOT_PHONE = '+44 20 7946 0321'
/**
 * What the sandbox's fixed key makes of each: tests/e2e/test_tutorial_chapters.py pins the same in the text read in the
 * screenshot as it was sent (test_images_sends_the_text_read_in_the_screenshot_with_the_three_fakes).
 */
const SHOT_EMAIL_FAKE = 'jack.jones@proseware-4795.example'
const SHOT_IBAN_FAKE = 'GB83 CDIA 1362 6967 9217 64'
const SHOT_PHONE_FAKE = '+44 21 8430 8193'
/** The picture's six lines (scripts/tutorial_screenshot.py), which the text read in it joins; and the sent text. */
const SHOT_LINES = [
  'Payment refused',
  'Amount: £1,240.00',
  `To account: ${SHOT_IBAN}`,
  'Reason: the receiving bank closed this account on 2 March.',
  `Customer: ${SHOT_EMAIL}`,
  `Phone: ${SHOT_PHONE}`,
]
const SHOT_PICTURE = '/tutorial/chapters/images/pictures/payment.png'
const SHOT_ANSWER =
  'The receiving bank closed the account, so the payment could not go through. Ask the customer for another account, then send it again.'

/** The picture an `<img>` of the mock is given (a data URL), as the SVG it is drawn from. */
const drawn = (url = '') => decodeURIComponent(url.slice(url.indexOf(',') + 1))

/** The mock dashboard's side of the tutorial, in memory, with a clock the test moves. */
function sandbox(requested: string | null = null, options: { failOnce?: boolean; noOcr?: boolean } = {}) {
  let now = 1_700_000_000_000
  let config: ConfigView = sandboxBase(initialConfig)
  let history: MockExchange[] = []
  let batches: Batch[] = []
  let changes: SettingsChange[] = []
  const applies = new Map<string, () => void>()
  const listeners = new Set<(decision: Decision) => void>()
  const decide = (decision: Decision) => {
    if (decision.kind === 'batch') batches = batches.filter((b) => b.id !== decision.id)
    else {
      if (decision.action === 'approve') applies.get(decision.id)?.()
      changes = changes.filter((c) => c.id !== decision.id)
    }
    for (const listener of listeners) listener(decision)
  }
  const host: TutorialHost = {
    now: () => now,
    config: () => config,
    patchConfig: (patch) => void (config = { ...config, ...patch }),
    resetConfig: (patch) => void (config = { ...sandboxBase(initialConfig), ...patch }),
    history: { clear: () => void (history = []), add: (exchange) => void history.unshift(exchange) },
    queue: {
      reset: () => {
        batches = []
        changes = []
      },
      addBatch: (batch) => void batches.push(batch),
      addChange: (change, apply) => {
        changes.push(change)
        applies.set(change.id, apply)
      },
      onDecided: (listener) => {
        listeners.add(listener)
        return () => listeners.delete(listener)
      },
    },
  }
  const tutorial = new MockTutorial(host, requested, options)
  const call = (method: string, path: string, body?: unknown) => tutorial.route(method, `/tutorial${path}`, body)
  return {
    tutorial,
    call,
    host,
    decide,
    get config() {
      return config
    },
    get history() {
      return history
    },
    get batches() {
      return batches
    },
    get changes() {
      return changes
    },
    wait: (ms: number) => void (now += ms),
  }
}

type Sandbox = ReturnType<typeof sandbox>

/** What a user does for each wait, in the real dashboard. */
function userDoes(box: Sandbox, kind: string) {
  const config = box.config
  switch (kind) {
    case 'term':
      box.host.patchConfig({ terms: [...config.terms, { value: 'Project Falcon', type: 'CUSTOM', case_sensitive: false, whole_word: true }] })
      break
    case 'allow':
      box.host.patchConfig({ allow_values: [...config.allow_values, '4242 4242 4242 4242'] })
      break
    case 'fields':
      box.host.patchConfig({ fields: keepInClear(config.fields, ['id', 'signup_date', 'plan_type']) })
      break
    case 'approve': {
      // Selects the name in the text and masks it as a person, then approves.
      const batch = box.batches[0]!
      const original = batch.items[0]!.original
      const start = [...original.slice(0, original.indexOf(CUSTOMER))].length
      const masks: ApprovalMask[] = [{ item: 0, start, end: start + [...CUSTOMER].length, value: CUSTOMER, category: 'person' }]
      box.decide({ kind: 'batch', id: batch.id, action: 'approve', masks })
      break
    }
    case 'change':
      box.decide({ kind: 'change', id: box.changes[0]!.id, action: 'reject' })
      break
    case 'guidance':
      // The page's editor saves the settings as they are, with the guidance written in.
      box.host.patchConfig({ llm_review: { ...config.llm_review, guidance: GUIDANCE } })
      break
  }
}

/** What a block's replacements say as the dashboard reads them: the text on this computer, the text sent, and the rule's own words. */
const replaced = (slot: Slot) =>
  slot.replacements.map((r) => [
    [...(slot.original ?? '')].slice(r.start, r.end).join(''),
    [...slot.masked].slice(r.masked_start, r.masked_end).join(''),
    r.type,
    r.class,
    r.rule,
  ])

/** The page: starts a chapter and does what the reducer says, as a user would, until the end. */
function playThrough(box: Sandbox, id: string) {
  const { chapter, state: first } = box.call('POST', `/chapters/${id}/start`) as { chapter: TutorialChapter; state: TutorialState }
  let state = first
  let view: View = initialView(chapter.steps.length, state)
  const seen: { step: number; phase: string; action: string | null }[] = []
  const dispatch = (event: Parameters<typeof viewReducer>[1]) => (view = viewReducer(view, event))
  const poll = () => {
    state = (box.call('GET', `/chapters/${id}/state`) as { state: TutorialState }).state
    dispatch({ type: 'poll', state })
  }
  const post = () => {
    dispatch({ type: 'send' })
    const answer = box.call('POST', `/chapters/${id}/next`, nextBody(state, view)) as { state: TutorialState }
    state = answer.state
    dispatch({ type: 'answer', state })
  }
  for (let turn = 0; turn < 60 && phaseOf(chapter, state, view) !== 'end'; turn++) {
    const phase = phaseOf(chapter, state, view)
    const action = primaryAction(chapter, state, view)
    seen.push({ step: view.step, phase, action })
    if (autoSends(chapter, state, view)) post()
    else if (action === 'send') post()
    else if (action === 'next' || action === 'finish') dispatch({ type: 'advance' })
    else if (phase === 'waiting') {
      const waiting = state.waiting!
      if (waiting.kind === 'pin') dispatch({ type: 'pinned' })
      else userDoes(box, waiting.kind)
      poll()
    } else {
      box.wait(BUSY_MS) // the agent works
      poll()
    }
  }
  return { chapter, state, view, seen }
}

describe('the mock tutorial: the menu', () => {
  it('lists the nine chapters in order, each with its own phase, and the chapter asked for once', () => {
    const box = sandbox('terms')
    const menu = box.call('GET', '/chapters') as { chapters: { id: string; number: number; phase: number; done: boolean }[]; requested: string | null; current: unknown }
    expect(menu.chapters).toHaveLength(9) // no card that stands in for a chapter the sandbox does not have
    expect(menu.chapters.filter((c) => c.phase === 1).map((c) => [c.number, c.id])).toEqual([
      [1, 'basics'],
      [2, 'terms'],
      [3, 'false-positives'],
      [4, 'stopped'],
      [5, 'live'],
      [6, 'settings'],
      [7, 'data'],
    ])
    expect(menu.chapters.filter((c) => c.phase === 2).map((c) => [c.number, c.id])).toEqual([
      [8, 'llm'],
      [9, 'images'],
    ])
    expect(menu.requested).toBe('terms')
    expect(menu.current).toBeNull()
    expect((box.call('GET', '/chapters') as { requested: string | null }).requested).toBeNull() // given once
  })

  it('says where the sandbox is in /me', () => {
    const box = sandbox()
    expect(box.tutorial.me()).toEqual({ current: null, reviewer: false })
    box.call('POST', '/chapters/terms/start')
    expect(box.tutorial.me().current).toMatchObject({ chapter: 'terms', number: 2, title: 'Your own terms', step: 0 })
  })

  it('says a chapter is in progress until its last step ran, and then it is done', () => {
    const box = sandbox()
    // The menu's and /me's chapter in progress, and whether the card of `terms` says done. A step whose time is up is
    // over by the next read, whoever reads: the daemon works in the background.
    const standing = () => {
      const menu = box.call('GET', '/chapters') as { chapters: { id: string; done: boolean }[]; current: unknown }
      const me = box.tutorial.me().current
      return [menu.current, me && { chapter: me.chapter, step: me.step }, menu.chapters.find((c) => c.id === 'terms')!.done]
    }
    box.call('POST', '/chapters/terms/start')
    expect(standing()).toEqual([{ chapter: 'terms', step: 0 }, { chapter: 'terms', step: 0 }, false])
    box.call('POST', '/chapters/terms/next', { step: 0 })
    box.wait(BUSY_MS)
    expect(standing()).toEqual([{ chapter: 'terms', step: 1 }, { chapter: 'terms', step: 1 }, false]) // half way
    userDoes(box, 'term')
    box.call('POST', '/chapters/terms/next', { step: 1 })
    box.wait(BUSY_MS)
    expect(standing()).toEqual([{ chapter: 'terms', step: 2 }, { chapter: 'terms', step: 2 }, false])
    box.call('POST', '/chapters/terms/next', { step: 2 })
    box.wait(BUSY_MS)
    expect(standing()).toEqual([null, null, true]) // every step ran: done, not in progress any more
    box.call('POST', '/chapters/terms/start') // Start again: a new run, in progress, and done still
    expect(standing()).toEqual([{ chapter: 'terms', step: 0 }, { chapter: 'terms', step: 0 }, true])
  })

  it('knows no chapter it does not have, and no route it does not have', () => {
    const box = sandbox()
    expect(() => box.call('GET', '/chapters/nope')).toThrow(ApiError)
    expect(box.call('GET', '/elsewhere')).toBeUndefined()
    expect(box.call('POST', '/end')).toEqual({ ending: true })
  })
})

describe('the mock tutorial: the contract', () => {
  it('answers a chapter with its steps, as the page needs them before they run', () => {
    const box = sandbox()
    const chapter = box.call('GET', '/chapters/basics') as TutorialChapter
    expect(chapter.agent).toEqual({ name: 'Tutorial Agent', model: 'Tutorial Model' })
    expect(chapter.steps.map((s) => [s.kind, s.button, s.wait?.kind ?? null])).toEqual([
      ['watch', 'Send it', null],
      ['watch', 'Let it write', null],
      ['do', null, 'pin'],
      ['watch', 'Send it', null],
    ])
    expect(chapter.steps[0]!.prompt).toContain('read .env')
  })

  it('refuses a step out of turn, with the state to go on from', () => {
    const box = sandbox()
    const getState = () => box.call('GET', '/chapters/basics/state')
    let error: unknown
    try {
      getState()
    } catch (e) {
      error = e
    }
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(409) // not started
    expect((error as ApiError).body).toEqual({ error: 'not started', state: null }) // as the daemon answers it
    expect(() => box.call('POST', '/chapters/basics/next', { step: 0 })).toThrow('not started')
    try {
      box.call('POST', '/chapters/basics/next', { step: 0 })
    } catch (e) {
      error = e
    }
    expect((error as ApiError).body).toEqual({ error: 'not started', state: null })

    box.call('POST', '/chapters/basics/start')
    try {
      box.call('POST', '/chapters/basics/next', { step: 2 })
    } catch (e) {
      error = e
    }
    expect((error as ApiError).message).toBe('not this step')
    expect(conflictState(error)).toMatchObject({ chapter: 'basics', next: 0, busy: false })
  })

  it('is busy while the agent works, refuses another call, and has the result after', () => {
    const box = sandbox()
    box.call('POST', '/chapters/basics/start')
    const started = (box.call('POST', '/chapters/basics/next', { step: 0 }) as { state: TutorialState }).state
    expect(started.busy).toBe(true)
    expect(started.next).toBe(0)
    expect(started.lines.map((l) => l.type)).toEqual(['prompt', 'agent']) // the prompt and the agent's line, at once
    expect(started.steps[0]).toMatchObject({ ran: false, explain: '', shown: null })
    let error: unknown
    try {
      box.call('POST', '/chapters/basics/next', { step: 0 })
    } catch (e) {
      error = e
    }
    expect((error as ApiError).message).toBe('busy')
    box.wait(BUSY_MS)
    const done = (box.call('GET', '/chapters/basics/state') as { state: TutorialState }).state
    expect(done).toMatchObject({ busy: false, next: 1, finished: false })
    expect(done.steps[0]).toMatchObject({ ran: true })
    expect(done.steps[0]!.shown).toMatchObject({ component: 'exchange', part: 'request', highlight: [expect.any(String)] })
    // The Write call is waiting to be run: the next step runs it.
    expect(done.lines.some((l) => l.type === 'tool' && l.name === 'Write' && l.pending === true)).toBe(true)
  })

  it('has a wait the user must meet first, and a pin the page says it saw', () => {
    const box = sandbox()
    box.call('POST', '/chapters/basics/start')
    for (const step of [0, 1]) {
      box.call('POST', '/chapters/basics/next', { step })
      box.wait(BUSY_MS)
    }
    const state = (box.call('GET', '/chapters/basics/state') as { state: TutorialState }).state
    expect(state.waiting).toEqual({ step: 2, kind: 'pin', satisfied: false })
    expect(state.steps[2]).toMatchObject({ ran: false, explain: expect.stringContaining('Click'), shown: { component: 'exchange', part: 'response' } })
    let error: unknown
    try {
      box.call('POST', '/chapters/basics/next', { step: 2 })
    } catch (e) {
      error = e
    }
    expect((error as ApiError).message).toBe('waiting')
    expect(conflictState(error)?.waiting?.kind).toBe('pin')
    box.call('POST', '/chapters/basics/next', { step: 2, seen: ['pin'] })
    box.wait(BUSY_MS)
    expect((box.call('GET', '/chapters/basics/state') as { state: TutorialState }).state.next).toBe(3)
  })

  it('starts over at once when the step has had its time, even with no one asking for the state since', () => {
    const box = sandbox()
    box.call('POST', '/chapters/basics/start')
    box.call('POST', '/chapters/basics/next', { step: 0 })
    box.wait(BUSY_MS) // the agent is done, but nothing read the state to see it
    const { state } = box.call('POST', '/chapters/basics/start') as { state: TutorialState }
    expect(state).toMatchObject({ next: 0, busy: false, run: 'run-2' })
  })

  it('starts a chapter over: history, values, settings and the queue are the chapter\'s own again', () => {
    const box = sandbox()
    playThrough(box, 'live')
    expect(box.history.length).toBeGreaterThan(0)
    box.call('POST', '/chapters/terms/start')
    expect(box.history).toEqual([])
    expect(box.config.live_review.enabled).toBe(false) // another chapter's setting
    box.call('POST', '/chapters/live/start')
    expect(box.config.live_review.enabled).toBe(true)
    box.call('POST', '/chapters/data/start')
    expect(box.config.data).toBe('strict')
  })
})

describe('the mock tutorial: every chapter, played as the page would play it', () => {
  const ORDER = ['basics', 'terms', 'false-positives', 'stopped', 'live', 'settings', 'data', 'llm', 'images']

  it.each(ORDER)('%s: runs to its end, with something to point at in every exchange it shows', (id) => {
    const box = sandbox()
    const { chapter, state, view, seen } = playThrough(box, id)
    expect(phaseOf(chapter, state, view)).toBe('end')
    expect(state.finished).toBe(true)
    expect(state.end!.learned).toHaveLength(3)
    expect(state.steps.every((s) => s.ran)).toBe(true)
    // The ends lead on: each chapter to the next, the last to none.
    expect(state.end!.next).toBe(ORDER[ORDER.indexOf(id) + 1] ?? null)

    for (const [index, result] of state.steps.entries()) {
      const shown = result.shown!
      expect(shown, `step ${index} shows something`).not.toBeNull()
      if (shown.component === 'exchange' || shown.component === 'why' || shown.component === 'review' || shown.component === 'images') {
        const found = box.history.find((e) => e.summary.id === shown.exchange)
        expect(found, `step ${index}: the exchange is in the history`).toBeDefined()
        const target = targetOf(detailOf(found!), shown)
        if ((shown.highlight ?? []).length > 0) {
          expect(target.pairs.length + target.blocks.length, `step ${index} points at something`).toBeGreaterThan(0)
        }
      }
    }
    expect(seen.length).toBeGreaterThan(0)
  })

  it('has every chapter done and none in progress once all of them were played', () => {
    const box = sandbox()
    for (const id of ORDER) playThrough(box, id)
    const menu = box.call('GET', '/chapters') as { chapters: { phase: number; done: boolean }[]; current: unknown }
    expect(menu.chapters.every((c) => c.done)).toBe(true) // the eighth and the ninth, of phase 2, too: the menu lists them like the others
    expect(menu.current).toBeNull()
    expect(box.tutorial.me().current).toBeNull()
  })

  it('basics: ready, running and shown for each step, waiting at the pin, the last one finishing', () => {
    const { seen } = playThrough(sandbox(), 'basics')
    const phases = seen.map((x) => `${x.step}:${x.phase}${x.action ? `/${x.action}` : ''}`).filter((x, i, all) => x !== all[i - 1])
    expect(phases).toEqual([
      '0:ready/send', '0:running', '0:shown/next',
      '1:ready/send', '1:running', '1:shown/next',
      '2:waiting', '2:running', '2:shown/next',
      '3:ready/send', '3:running', '3:shown/finish',
    ])
  })

  it('live: the request is sent, waits for the user while the agent is busy, goes on when it is decided', () => {
    const { seen } = playThrough(sandbox(), 'live')
    const phases = seen.map((x) => `${x.step}:${x.phase}${x.action ? `/${x.action}` : ''}`).filter((x, i, all) => x !== all[i - 1])
    expect(phases).toEqual(['0:ready/send', '0:waiting', '0:running', '0:shown/finish'])
  })

  it('basics: the pane shows what the sandbox shows, in its order', () => {
    const { state } = playThrough(sandbox(), 'basics')
    expect(state.lines.map((line) => `${line.step}:${line.type}${line.type === 'tool' ? `:${line.name}` : ''}`)).toEqual([
      '0:prompt', '0:agent', '0:tool:Read', '0:model', '0:tool:Write',
      '1:tool:Write', '1:file', '1:model',
      '3:prompt', '3:model',
    ])
    expect(state.lines[2]).toMatchObject({ summary: '.env', output: 'Read 3 lines' }) // the model got the text
  })

  it('basics: the pin step waits for the page, and the key is a mark to open', () => {
    const box = sandbox()
    const { seen, state } = playThrough(box, 'basics')
    expect(seen.map((s) => s.phase)).toContain('waiting')
    const [first, second] = [state.steps[0]!.shown!, state.steps[1]!.shown!]
    const write = box.history.find((e) => e.summary.id === first.exchange)!
    const key = first.highlight![0]!
    // Request: masked, with a pair on both sides; response: the agent got the real one.
    expect(targetOf(detailOf(write), first).pairs).toHaveLength(1)
    expect(targetOf(detailOf(write), second).pairs).toHaveLength(1)
    expect(detailOf(write).request.slots.some((s) => s.original?.includes(key) && !s.masked.includes(key))).toBe(true)
  })

  it('terms: the codename is in clear until it is a term, then a name the model has never seen', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'terms')
    const [before, terms, after] = state.steps.map((s) => s.shown!)
    expect(terms).toEqual({ component: 'terms' }) // the term, where you added it
    const clear = detailOf(box.history.find((e) => e.summary.id === before!.exchange)!)
    const masked = detailOf(box.history.find((e) => e.summary.id === after!.exchange)!)
    expect(targetOf(clear, before!)).toMatchObject({ pairs: [], blocks: [expect.stringMatching(/^block-/)] })
    expect(targetOf(masked, after!).pairs).toHaveLength(1)
    // A new conversation: the prompt alone, with the fake in it; not the roadmap, nor the model's first answer.
    expect(masked.request.slots.map((slot) => slot.kind)).toEqual(['system', 'user_text'])
    expect(masked.request.slots[1]!.masked).toBe('what ships after Woodgrove Tailspin?')
    expect(state.lines.filter((line) => line.step === 2)).toEqual([
      { step: 2, type: 'system', text: '/clear: a new conversation, with no history', note: 'cleared' },
      { step: 2, type: 'prompt', text: 'what ships after Project Falcon?' },
      { step: 2, type: 'agent', text: 'New conversation.' },
      { step: 2, type: 'model', text: "What's Project Falcon? No idea what this is." },
    ])
    expect(state.steps[2]!.explain).toContain('The model got Woodgrove Tailspin:')
  })

  it('false-positives: after Not sensitive, the agent reads the file again, and the card goes in clear there', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'false-positives')
    expect(state.lines.filter((line) => line.step === 1).map((line) => line.type)).toEqual(['prompt', 'tool', 'model'])
    const shown = state.steps[1]!.shown!
    const detail = detailOf(box.history.find((e) => e.summary.id === shown.exchange)!)
    const reads = detail.request.slots.flatMap((slot, i) => (slot.kind === 'tool_result' ? [i] : []))
    expect(reads).toHaveLength(2)
    expect(shown.slot).toBe(reads[1]) // the new read, not the first one sent again
    expect(detail.request.slots[reads[1]!]!.masked).toContain('4242 4242 4242 4242')
    expect(state.steps[1]!.explain).toContain('The agent then reads the file again, and this time the card goes in clear')
  })

  it('stopped: a conversation resumed from before Latet is stopped, the key marked where it was said; a new one goes', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'stopped')
    const exchanges = state.steps.map((s) => box.history.find((e) => e.summary.id === s.shown!.exchange)!)
    expect(exchanges.map((e) => [e.summary.mode, e.summary.status, e.summary.flags.includes('blocked')])).toEqual([
      ['verify', 400, true],
      ['verify', 200, false],
    ])
    const first = state.steps[0]!.shown!
    expect(first).toMatchObject({ component: 'why', focus: 'why' })
    expect(targetOf(detailOf(exchanges[0]!), first)).toMatchObject({ pairs: [expect.stringContaining(':stopped:')], blocks: ['tutorial-why'] })
    // What was said yesterday, the key in it, goes again before today's prompt: the key is marked there.
    const slots = detailOf(exchanges[0]!).request.slots
    expect(slots.map((slot) => slot.kind)).toEqual(['system', 'user_text', 'assistant_text', 'user_text'])
    expect(first.slot).toBe(1)
  })

  it('stopped: says what happens and what to do, without naming how a request is signed', () => {
    const { chapter, state } = playThrough(sandbox(), 'stopped')
    const texts = [
      ...chapter.steps.map((step) => step.title),
      ...state.steps.map((step) => step.explain),
      ...state.end!.learned,
      ...state.lines.flatMap((line) => (line.type === 'agent' || line.type === 'prompt' || line.type === 'model' ? [line.text] : [])),
    ]
    expect(texts.filter((text) => /\bsign(?:ed|s)?\b|\bsubscriptions?\b|\bPro or Max\b|\bverify\b|\bcheck-only\b/i.test(text))).toEqual([])
    expect(state.steps[0]!.explain).toContain('Claude Code seals each request with a fingerprint of that conversation, so on the way out Latet may not change it, only check it.')
  })

  it('stopped: the chapter has the conversation resumed before anything is sent, the lines its pane then holds', () => {
    const box = sandbox()
    const { chapter, state: fresh } = box.call('POST', '/chapters/stopped/start') as { chapter: TutorialChapter; state: TutorialState }
    const resumed = chapter.steps[0]!.resumed
    expect(resumed.map((line) => [line.type, 'resumed' in line && line.resumed === true])).toEqual([
      ['system', false],
      ['prompt', true],
      ['model', true],
    ])
    expect(fresh.lines).toEqual([]) // nothing ran yet
    expect(chapter.steps[1]!.resumed).toEqual([])
    const { state } = playThrough(box, 'stopped')
    expect(state.lines.slice(0, resumed.length)).toEqual(resumed)
  })

  it('stopped: the pane shows the conversation resumed, then the new one', () => {
    const { state } = playThrough(sandbox(), 'stopped')
    expect(state.lines.filter((line) => line.type === 'system')).toEqual([
      { step: 0, type: 'system', text: "/resume: yesterday's conversation, from before Latet was on", note: 'resumed' },
      { step: 1, type: 'system', text: '/clear: a new conversation, with no history', note: 'cleared' },
    ])
    expect(state.lines.filter((line) => line.type !== 'tool' && line.type !== 'file' && line.resumed)).toEqual([
      expect.objectContaining({ step: 0, type: 'prompt', resumed: true }),
      { step: 0, type: 'model', text: "Deploying to prod. I'll tell you when it's done.", resumed: true },
    ])
  })

  it('stopped: the lines the dashboard says in your language read, in English, as the daemon writes them', () => {
    const { state } = playThrough(sandbox(), 'stopped')
    const marked = state.lines.flatMap((line) => (line.type === 'system' && line.note ? [line] : []))
    expect(marked).toHaveLength(2)
    for (const line of marked) expect(i18n.t(`tutorial.agent.${line.note}`)).toBe(line.text)
  })

  it('stopped: the prompts are the chapter file\'s, with a space after the key (a dot would be read as part of it)', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'stopped')
    const key = state.steps[0]!.shown!.highlight![0]!
    expect(state.lines.flatMap((line) => (line.type === 'prompt' ? [line.text] : []))).toEqual([
      `deploy to prod with DEMO_API_KEY=${key} and tell me when it's done`, // yesterday's, resumed
      'continue the deploy',
      'deploy to prod with the key in DEMO_API_KEY, and never paste it',
    ])
    // The request that was stopped carries that text, with the key in clear: the space is what ends it.
    const stopped = detailOf(box.history.find((e) => e.summary.id === state.steps[0]!.shown!.exchange)!)
    expect(stopped.request.slots.some((slot) => slot.original?.includes(`DEMO_API_KEY=${key} and tell me when it's done`))).toBe(true)
  })

  it('live: the request is held until the user decided, and goes with the name masked by hand', () => {
    const box = sandbox()
    const first = (box.call('POST', '/chapters/live/start') as { state: TutorialState }).state
    expect(first.waiting).toEqual({ step: 0, kind: 'approve', satisfied: false })
    expect(first.busy).toBe(false)
    const started = (box.call('POST', '/chapters/live/next', { step: 0 }) as { state: TutorialState }).state
    expect(started.busy).toBe(true)
    expect(box.batches).toHaveLength(1)
    // However long it waits, nothing is sent.
    box.wait(10 * BUSY_MS)
    const waiting = (box.call('GET', '/chapters/live/state') as { state: TutorialState }).state
    expect(waiting).toMatchObject({ busy: true, waiting: { kind: 'approve', satisfied: false } })
    expect(waiting.steps[0]!.shown).toEqual({ component: 'approvals' })
    expect(box.history).toEqual([])

    userDoes(box, 'approve')
    const decided = (box.call('GET', '/chapters/live/state') as { state: TutorialState }).state
    expect(decided).toMatchObject({ busy: true, waiting: { satisfied: true } }) // the agent goes on
    box.wait(BUSY_MS)
    const done = (box.call('GET', '/chapters/live/state') as { state: TutorialState }).state
    expect(done).toMatchObject({ busy: false, finished: true })
    const sent = detailOf(box.history[0]!)
    const note = sent.request.slots.find((s) => s.original?.includes('draft a reply'))!
    expect(note.original).toContain(CUSTOMER)
    expect(note.masked).not.toContain(CUSTOMER)
    expect(note.masked).toContain('David Green') // the sandbox's fake for her, as a person
    expect(note.replacements.map((r) => [r.type, r.class, r.rule])).toEqual([
      ['PERSON', 'CUSTOM', 'human:person'],
      ['EMAIL', 'PII', 'pii:email'],
    ])
  })

  it('live: says how it ended when the name went in clear, all of it or a word of it', () => {
    const ended = (masked: string) => {
      const box = sandbox()
      box.call('POST', '/chapters/live/start')
      box.call('POST', '/chapters/live/next', { step: 0 })
      const batch = box.batches[0]!
      const start = [...LIVE_TEXT.slice(0, LIVE_TEXT.indexOf(masked))].length
      const masks: ApprovalMask[] = masked ? [{ item: 0, start, end: start + [...masked].length, value: masked, category: 'person' }] : []
      box.decide({ kind: 'batch', id: batch.id, action: 'approve', masks })
      box.wait(BUSY_MS)
      return (box.call('GET', '/chapters/live/state') as { state: TutorialState }).state.steps[0]!.explain
    }
    expect(ended('')).toContain(`the model got ${CUSTOMER} in clear`) // approved as it was (the Approve button)
    expect(ended('Emily')).toContain('Part of the name was left in clear')
    expect(ended(CUSTOMER)).toContain(`nothing tells it that ${CUSTOMER} is a customer: this sandbox has no name detection.`) // as the chapter asks
  })

  it('live: not sending it stops the request, and says who stopped it', () => {
    const box = sandbox()
    box.call('POST', '/chapters/live/start')
    box.call('POST', '/chapters/live/next', { step: 0 })
    box.decide({ kind: 'batch', id: box.batches[0]!.id, action: 'reject', masks: [] })
    box.wait(BUSY_MS)
    const done = (box.call('GET', '/chapters/live/state') as { state: TutorialState }).state
    expect(done.finished).toBe(true)
    expect(done.steps[0]!.shown).toMatchObject({ component: 'why' })
    expect(box.history[0]!.summary.flags).toContain('blocked')
    expect(box.history[0]!.summary.block_reason).toContain('did not approve it in the Live Reviewer')
    // What the agent was told, as the gateway says it: Latet named once.
    expect(done.lines.at(-1)).toEqual({
      step: 0,
      type: 'error',
      text: "Latet did not send this request: you did not approve it in the Live Reviewer. Nothing was sent; see where it was in Latet's pane",
    })
  })

  it('live: starting over while the request is held lets it go (the queue is emptied)', () => {
    const box = sandbox()
    box.call('POST', '/chapters/live/start')
    box.call('POST', '/chapters/live/next', { step: 0 })
    expect(box.batches).toHaveLength(1)
    box.call('POST', '/chapters/live/start')
    expect(box.batches).toEqual([])
  })

  it('settings: the change waits with its reasons; deciding it is what the step waits for', () => {
    const box = sandbox()
    box.call('POST', '/chapters/settings/start')
    box.call('POST', '/chapters/settings/next', { step: 0 })
    box.wait(BUSY_MS)
    const state = (box.call('GET', '/chapters/settings/state') as { state: TutorialState }).state
    expect(state.steps[0]!.shown).toEqual({ component: 'changes' })
    expect(box.changes).toHaveLength(1)
    expect(box.changes[0]).toMatchObject({ source: 'command', agent: 'tutorial-agent', keys: ['policies'] })
    expect(box.changes[0]!.loosened).toEqual(['policy for EMAIL: from mask to allow'])
    expect(state.waiting).toEqual({ step: 1, kind: 'change', satisfied: false })
    expect(box.config.policies).toEqual({}) // nothing the agent asked for applies before you decide
    // The agent read the test, then ran the command, which printed what the CLI prints: the change waits, with its id.
    expect(state.lines.filter((line) => line.type === 'tool').map((line) => line.type === 'tool' && line.name)).toEqual(['Read', 'Bash'])
    const printed = state.lines.find((line) => line.type === 'tool' && line.name === 'Bash')
    expect(printed).toMatchObject({ summary: 'latet config set policies "{EMAIL: allow}"', output: expect.stringContaining(`Change id: ${box.changes[0]!.id} `) })
    // The dashboard it names is this tutorial's: `latet dashboard` would open the user's own.
    expect(printed).toMatchObject({ output: expect.stringContaining("approval in the dashboard (here: Latet's pane). ") })
    expect(printed).not.toMatchObject({ output: expect.stringContaining('latet dashboard') })
    expect(state.lines.at(-1)).toEqual({
      step: 0,
      type: 'model',
      text: 'The test needs real e-mail addresses and Latet masks them, so I asked it to let them through. It waits for your approval.',
    })
    userDoes(box, 'change')
    expect((box.call('GET', '/chapters/settings/state') as { state: TutorialState }).state.waiting).toEqual({ step: 1, kind: 'change', satisfied: true })
  })

  it('settings: undone, the agent finds the settings as they were', () => {
    const { state } = playThrough(sandbox(), 'settings')
    expect(state.lines.at(-1)).toEqual({ step: 2, type: 'model', text: "Undone: e-mail addresses stay masked. I'll fix the test without the real ones." })
    expect(state.steps[2]!.explain).toContain('you undid the change, so your settings are as they were')
  })

  it('settings: approved instead, e-mail addresses leave in clear, and the step says how to try again', () => {
    const box = sandbox()
    box.call('POST', '/chapters/settings/start')
    box.call('POST', '/chapters/settings/next', { step: 0 })
    box.wait(BUSY_MS)
    box.call('GET', '/chapters/settings/state')
    box.decide({ kind: 'change', id: box.changes[0]!.id, action: 'approve' })
    expect(box.config.policies).toEqual({ EMAIL: 'allow' })
    box.call('POST', '/chapters/settings/next', { step: 1 })
    box.wait(BUSY_MS)
    box.call('POST', '/chapters/settings/next', { step: 2 })
    box.wait(BUSY_MS)
    const done = (box.call('GET', '/chapters/settings/state') as { state: TutorialState }).state
    expect(done.lines.at(-1)).toEqual({ step: 2, type: 'model', text: 'Approved: e-mail addresses now leave in clear, so the test can have the real ones.' })
    expect(done.steps[2]!.explain).toContain('You approved the change, so e-mail addresses now leave in clear')
    expect(done.steps[2]!.explain).toContain('Start over')
  })

  it('data: the table is masked to its last value, then the kept columns come in clear', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'data')
    const [first, second] = state.steps.map((s) => s.shown!)
    expect(first).toMatchObject({ component: 'exchange' })
    const table = detailOf(box.history.find((e) => e.summary.id === first!.exchange)!).request.slots.find((s) => s.kind === 'tool_result')!
    const ids = [...table.masked.matchAll(/^ *(\d+) \|/gm)].map((m) => m[1])
    expect(ids).toEqual(['8146', '1279', '7780', '1042', '6756']) // the sandbox's fakes: not even an id left in clear
    expect(table.replacements).toHaveLength(30)

    // Both tables of the second query, the history's first one too: ids, dates and plans in clear, the people's masked.
    const tables = detailOf(box.history.find((e) => e.summary.id === second!.exchange)!).request.slots.filter((s) => s.kind === 'tool_result')
    expect(tables).toHaveLength(2)
    for (const again of tables) {
      expect(again.masked).toContain(' 1001 | ')
      expect(again.masked).toContain('2025-01-14')
      expect(again.masked).not.toContain('Mary')
      expect(again.replacements.map((r) => r.rule).filter((rule, i, all) => all.indexOf(rule) === i)).toEqual(['strict:name', 'pii:email', 'strict:amount'])
    }
  })

  it('data: the panel offers the columns, with ids, dates and plans to keep and none kept yet', () => {
    const box = sandbox()
    box.call('POST', '/chapters/data/start')
    box.call('POST', '/chapters/data/next', { step: 0 })
    box.wait(BUSY_MS)
    const state = (box.call('GET', '/chapters/data/state') as { state: TutorialState }).state
    const look = state.steps[1]!.shown!
    expect(look).toMatchObject({ component: 'columns' })
    const exchange = detailOf(box.history.find((e) => e.summary.id === look.exchange)!)
    const columns = mockColumns(exchange.request.slots[look.slot!]!, null, false, box.config.fields)
    expect(columns.columns.map((c) => [c.name, c.proposal, c.kept_now])).toEqual([
      ['id', 'keep', false],
      ['name', 'mask', false],
      ['email', 'mask', false],
      ['signup_date', 'keep', false],
      ['plan_type', 'keep', false],
      ['amount', 'ask', false],
    ])
    userDoes(box, 'fields')
    expect(mockColumns(exchange.request.slots[look.slot!]!, null, false, box.config.fields).columns.filter((c) => c.kept_now).map((c) => c.name)).toEqual(['id', 'signup_date', 'plan_type'])
  })

  it('llm: the reviewer finds the four values, then the wards once the guidance is saved, said to be simulated', () => {
    const box = sandbox()
    expect(box.tutorial.me().reviewer).toBe(false)
    const { state } = playThrough(box, 'llm')
    expect(box.tutorial.me().reviewer).toBe(true)
    expect(state.steps.map((s) => s.shown?.component)).toEqual(['review', 'guidance', 'review'])
    const last = box.history.find((e) => e.summary.id === state.steps[2]!.shown!.exchange)!
    const rules = detailOf(last).request.slots.flatMap((s) => s.replacements.map((r) => r.rule))
    expect(rules.filter((r) => r === 'llm:confidential')).toHaveLength(2)
    expect(state.steps[1]!.explain).toContain('the Tutorial Reviewer, which is simulated, only checks that some guidance is saved')
  })

  it('llm: ready, running and shown for the ticket, waiting at the guidance, a new conversation for the handover note', () => {
    const { seen } = playThrough(sandbox(), 'llm')
    const phases = seen.map((x) => `${x.step}:${x.phase}${x.action ? `/${x.action}` : ''}`).filter((x, i, all) => x !== all[i - 1])
    expect(phases).toEqual([
      '0:ready/send', '0:running', '0:shown/next',
      '1:waiting', '1:running', '1:shown/next',
      '2:ready/send', '2:running', '2:shown/finish',
    ])
  })

  it('llm: the settings are the chapter\'s own: the Tutorial Reviewer is on, with no guidance, until another chapter starts', () => {
    const box = sandbox()
    box.call('POST', '/chapters/llm/start')
    expect(box.config.llm_review).toMatchObject({ enabled: true, url: 'http://127.0.0.1:9/v1', model: 'tutorial-reviewer', slots: 1, guidance: null })
    expect(box.config.llm_review.auto_mask).toEqual(['person', 'address', 'email', 'phone', 'secret', 'health', 'financial', 'id', 'confidential'])
    expect(box.tutorial.me().reviewer).toBe(true)
    box.call('POST', '/chapters/terms/start') // the sandbox's settings start over: no reviewer
    expect(box.config.llm_review.enabled).toBe(false)
    expect(box.tutorial.me().reviewer).toBe(false)
  })

  it('llm: the ticket leaves with the four values no pattern finds masked as the reviewer found them, and the ward in clear', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'llm')
    const first = state.steps[0]!.shown!
    expect(first).toMatchObject({ component: 'review', part: 'request', highlight: [PATIENT, CONDITION, WRITTEN, ACCOUNT] })
    const sent = detailOf(box.history.find((e) => e.summary.id === first.exchange)!)
    const ticket = sent.request.slots[first.slot!]!
    expect(ticket.kind).toBe('tool_result')
    expect(replaced(ticket)).toEqual([
      [PATIENT, PATIENT_FAKE, 'PERSON', 'LLM', 'llm:person'],
      [CONDITION, CONDITION_FAKE, 'HEALTH', 'LLM', 'llm:health'],
      [WRITTEN, WRITTEN_FAKE, 'EMAIL', 'LLM', 'llm:email'],
      [ACCOUNT, ACCOUNT_FAKE, 'ID_NUMBER', 'LLM', 'llm:id'],
    ])
    expect(ticket.replacements.every((r) => r.module === 'reviewer')).toBe(true)
    expect(ticket.masked).toContain('a patient on Ward 7B,') // nothing tells the reviewer that a ward's name says who is where
    expect(targetOf(sent, first).pairs).toHaveLength(4) // each of the four is a mark to point at
    // The agent shows what the model wrote with the real values put back.
    expect(state.lines.filter((line) => line.step === 0)).toEqual([
      { step: 0, type: 'prompt', text: 'read ticket-4182.md and sum it up' },
      { step: 0, type: 'agent', text: "I'll read the ticket." },
      { step: 0, type: 'tool', name: 'Read', summary: 'ticket-4182.md', output: 'Read 9 lines' },
      {
        step: 0,
        type: 'model',
        text: `${PATIENT} has not been able to get into the patient portal since Monday and needs the blood test results before Friday (${CONDITION}). Reply to ${WRITTEN}; the parking refund goes to ${ACCOUNT}.`,
      },
    ])
    expect(detailOf(box.history.find((e) => e.summary.id === first.exchange)!).response!.text_masked).toContain(`${PATIENT_FAKE} has not been able`)
    expect(state.steps[0]!.explain).toContain(`and Latet masked them: the model got ${PATIENT_FAKE} for the name. Ward 7B left in clear:`)
  })

  it('llm: the guidance step carries the sentence written in, waits for any guidance saved, and shows the editor with it', () => {
    const box = sandbox()
    const stateNow = () => (box.call('GET', '/chapters/llm/state') as { state: TutorialState }).state
    const { chapter } = box.call('POST', '/chapters/llm/start') as { chapter: TutorialChapter }
    expect(chapter.steps.map((step) => [step.kind, step.button, step.wait])).toEqual([
      ['watch', 'Send it', null],
      ['do', null, { kind: 'guidance', hint: 'Save the guidance as it is written.', text: GUIDANCE }],
      ['watch', 'Send it', null],
    ])
    box.call('POST', '/chapters/llm/next', { step: 0 })
    box.wait(BUSY_MS)
    let state = stateNow()
    expect(state.waiting).toEqual({ step: 1, kind: 'guidance', satisfied: false })
    expect(state.steps[1]).toMatchObject({ ran: false, shown: { component: 'guidance', text: GUIDANCE } }) // what it asks to use
    expect(() => box.call('POST', '/chapters/llm/next', { step: 1 })).toThrow('waiting')
    box.host.patchConfig({ llm_review: { ...box.config.llm_review, guidance: 'Our suppliers are confidential.' } }) // the reader's own words
    expect(stateNow().waiting).toEqual({ step: 1, kind: 'guidance', satisfied: true })
    box.call('POST', '/chapters/llm/next', { step: 1 })
    box.wait(BUSY_MS)
    state = stateNow()
    expect(state.steps[1]).toMatchObject({ ran: true, shown: { component: 'guidance', text: GUIDANCE } })
    expect(state.lines.filter((line) => line.step === 1)).toEqual([]) // the agent does nothing here
    expect(state.steps[1]!.explain).toContain('In your own dashboard it is in Rules, under Local reviewer.')
  })

  it('llm: with the guidance saved, the handover note is read in a new conversation, and both wards are masked', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'llm')
    expect(state.lines.filter((line) => line.step === 2)).toEqual([
      { step: 2, type: 'system', text: '/clear: a new conversation, with no history', note: 'cleared' },
      { step: 2, type: 'prompt', text: 'read handover.md and tell me what the night shift needs' },
      { step: 2, type: 'agent', text: 'New conversation.' },
      { step: 2, type: 'tool', name: 'Read', summary: 'handover.md', output: 'Read 5 lines' },
      { step: 2, type: 'model', text: 'Ward 3A is short of one nurse tonight, and the agency sends one at 23:00. Ward 7B has two beds free.' },
    ])
    const exchangeOf = (step: number) => detailOf(box.history.find((e) => e.summary.id === state.steps[step]!.shown!.exchange)!)
    const [first, last] = [exchangeOf(0), exchangeOf(2)] as const
    expect(last.summary.session).not.toBe(first.summary.session)
    // A new conversation: the ticket, which left with Ward 7B in clear, does not go with it.
    expect(last.request.slots.map((slot) => slot.kind)).toEqual(['system', 'user_text', 'tool_input', 'tool_result'])
    const shown = state.steps[2]!.shown!
    expect(shown).toMatchObject({ component: 'review', part: 'request', slot: 3, highlight: ['Ward 7B', 'Ward 3A'] })
    expect(replaced(last.request.slots[shown.slot!]!)).toEqual([
      ['Ward 7B', 'Fqnm 2T', 'CONFIDENTIAL', 'LLM', 'llm:confidential'],
      ['Ward 3A', 'Axvn 2S', 'CONFIDENTIAL', 'LLM', 'llm:confidential'],
    ])
    expect(targetOf(last, shown).pairs).toHaveLength(2)
    expect(state.steps[2]!.explain).toContain('The model got Axvn 2S for Ward 3A.')
  })

  it('llm: the reviewer\'s records list what it found in each block it changed, with the offsets the panel reads', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'llm')
    const panel = (step: number) => {
      const shown = state.steps[step]!.shown!
      const detail = detailOf(box.history.find((e) => e.summary.id === shown.exchange)!)
      const records = detail.request.slots.flatMap((slot) => reviewRecordOf(detail.summary, slot) ?? [])
      return { detail, shown, records, listed: reviewsOf(detail.request.slots, records) }
    }
    const ticket = panel(0)
    expect(ticket.records).toHaveLength(1) // the system block, the prompt and the model's own call: nothing found there
    expect(ticket.listed[0]!.review).toMatchObject({
      block: ticket.detail.request.slots[ticket.shown.slot!]!.block,
      kind: 'tool_result',
      session: ticket.detail.summary.session,
      inline: true, // read before the request left
      failed: null,
      block_asked: false,
    })
    // The panel names each finding by the text it covers in the block as it was sent.
    expect(ticket.listed[0]!.findings.map((f) => [f.category, f.masked, f.text])).toEqual([
      ['person', true, PATIENT_FAKE],
      ['health', true, CONDITION_FAKE],
      ['email', true, WRITTEN_FAKE],
      ['id', true, ACCOUNT_FAKE],
    ])
    expect(ticket.listed[0]!.findings.map((f) => f.entity)).toEqual(ticket.detail.request.slots[ticket.shown.slot!]!.replacements.map((r) => r.entity))
    const note = panel(2)
    expect(note.records).toHaveLength(1)
    expect(note.listed[0]!.review.block).toBe(note.detail.request.slots[note.shown.slot!]!.block)
    expect(note.listed[0]!.findings.map((f) => [f.category, f.masked, f.text])).toEqual([
      ['confidential', true, 'Fqnm 2T'],
      ['confidential', true, 'Axvn 2S'],
    ])
  })

  it('llm: no record for a block the reviewer changed nothing in, nor for one masked by another rule', () => {
    const box = sandbox()
    playThrough(box, 'terms')
    const exchange = detailOf(box.history[0]!)
    expect(exchange.request.slots.some((slot) => slot.replacements.length > 0)).toBe(true) // the term's mask is there
    expect(exchange.request.slots.flatMap((slot) => reviewRecordOf(exchange.summary, slot) ?? [])).toEqual([])
  })

  it('images: the screenshot leaves covered, shown as it was and as it was sent, and a value of its text opens', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'images')
    const shown = state.steps[0]!.shown!
    expect(shown).toMatchObject({ component: 'images', original: '/tutorial/chapters/images/pictures/payment.png' })
    const exchange = box.history.find((e) => e.summary.id === shown.exchange)!
    expect(exchange.summary.flags).toContain('image:masked')
    const rules = detailOf(exchange).request.slots.flatMap((s) => s.replacements.map((r) => r.rule))
    expect(rules).toEqual(expect.arrayContaining(['pii:email', 'pii:iban', 'pii:phone']))
  })

  it('images: the text read in the picture is a block of its own, with the three values masked as the sandbox masks them', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'images')
    const first = state.steps[0]!.shown!
    expect(first).toMatchObject({ component: 'images', part: 'request', highlight: [SHOT_EMAIL, SHOT_IBAN, SHOT_PHONE] })
    const sent = detailOf(box.history.find((e) => e.summary.id === first.exchange)!)
    const text = sent.request.slots[first.slot!]!
    expect(text.kind).toBe('image_text')
    expect(text.original).toBe(SHOT_LINES.join('\n'))
    expect(text.masked).toBe(
      SHOT_LINES.join('\n').replace(SHOT_IBAN, SHOT_IBAN_FAKE).replace(SHOT_EMAIL, SHOT_EMAIL_FAKE).replace(SHOT_PHONE, SHOT_PHONE_FAKE),
    )
    expect(replaced(text)).toEqual([
      [SHOT_IBAN, SHOT_IBAN_FAKE, 'IBAN', 'PII', 'pii:iban'],
      [SHOT_EMAIL, SHOT_EMAIL_FAKE, 'EMAIL', 'PII', 'pii:email'],
      [SHOT_PHONE, SHOT_PHONE_FAKE, 'PHONE', 'PII', 'pii:phone'],
    ])
    expect(targetOf(sent, first).pairs).toHaveLength(3) // each of the three is a mark to open
    // The picture is no block of text: the agent's Read answered with it alone, so the request has no tool output.
    expect(sent.request.slots.map((slot) => slot.kind)).toEqual(['system', 'user_text', 'tool_input', 'image_text'])
    expect(sent.summary).toMatchObject({ masked: 3, new_masked: 3, restored: 0, status: 200, mode: 'rewrite' })
    expect(sent.summary.by_type).toEqual({ IBAN: 1, EMAIL: 1, PHONE: 1 })
  })

  it('images: the request carries the picture, kept, with the block of its text, and both pictures can be shown', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'images')
    const shown = state.steps[0]!.shown!
    const sent = detailOf(box.history.find((e) => e.summary.id === shown.exchange)!)
    expect(sent.request.images).toHaveLength(1)
    const [image] = sent.request.images!
    expect(image).toMatchObject({ state: 'masked', kept: true, block: sent.request.slots[shown.slot!]!.block })
    // Where the image is in the Anthropic request: the data of the image block in the tool's answer.
    expect(image!.path).toMatch(/^\$\.messages\[\d+\]\.content\[0\]\.content\[0\]\.source\.data$/)
    expect(sent.request.slots[shown.slot!]!.path).toBe(`${image!.path}#text`)
    // The picture as it was is the six lines; the one sent has the fakes in place of the three values.
    const before = drawn(MOCK_ASSETS[shown.original!])
    const after = drawn(MOCK_ASSETS[`/images/${image!.id}`])
    for (const line of SHOT_LINES) expect(before).toContain(line)
    expect(after).toContain('Payment refused')
    for (const [real, fake] of [
      [SHOT_IBAN, SHOT_IBAN_FAKE],
      [SHOT_EMAIL, SHOT_EMAIL_FAKE],
      [SHOT_PHONE, SHOT_PHONE_FAKE],
    ] as const) {
      expect(before).not.toContain(fake)
      expect(after).toContain(fake)
      expect(after).not.toContain(real)
    }
    expect(shown.original).toBe(SHOT_PICTURE)
  })

  it('images: the pretend model answers in words, with no value in them, and the agent shows what it did', () => {
    const box = sandbox()
    const { state } = playThrough(box, 'images')
    const exchange = box.history.find((e) => e.summary.id === state.steps[0]!.shown!.exchange)!
    const answer = detailOf(exchange).response!
    expect(answer.text_masked).toBe(SHOT_ANSWER)
    expect(answer.text_spans ?? []).toEqual([]) // nothing to restore: the model read no value
    // The first request is the model's Read call, the second carries the picture.
    expect(box.history.map((e) => e.summary.flags.includes('image:masked'))).toEqual([true, false])
    expect(state.lines).toEqual([
      { step: 0, type: 'prompt', text: 'why was the payment in payment.png refused?' },
      { step: 0, type: 'agent', text: "I'll look at the screenshot." },
      { step: 0, type: 'tool', name: 'Read', summary: 'payment.png', output: 'Read image (33 KB)' },
      { step: 0, type: 'model', text: SHOT_ANSWER },
    ])
    expect(state.steps[0]!.explain).toBe(
      "The agent sent the screenshot as it is. Latet read the text in it on your computer, found an e-mail address, an IBAN and a phone number, and covered each with its fake before the image left. Latet's pane shows the image as it is on your computer and as it was sent, and the text read in it.",
    )
  })

  it('images: ready, running and shown for the screenshot, then waiting for a click on a value, the last step finishing', () => {
    const { seen } = playThrough(sandbox(), 'images')
    const phases = seen.map((x) => `${x.step}:${x.phase}${x.action ? `/${x.action}` : ''}`).filter((x, i, all) => x !== all[i - 1])
    expect(phases).toEqual(['0:ready/send', '0:running', '0:shown/next', '1:waiting', '1:running', '1:shown/finish'])
  })

  it('images: the second step waits for a click on a value of the text read, and shows the same pane as the first', () => {
    const box = sandbox()
    const stateNow = () => (box.call('GET', '/chapters/images/state') as { state: TutorialState }).state
    const { chapter } = box.call('POST', '/chapters/images/start') as { chapter: TutorialChapter }
    expect(chapter.steps.map((step) => [step.kind, step.title, step.prompt, step.button, step.wait])).toEqual([
      ['watch', 'A screenshot leaves covered', 'why was the payment in payment.png refused?', 'Send it', null],
      ['do', 'Ask Latet', null, null, { kind: 'pin', hint: `Click ${SHOT_EMAIL} in the text read from the image.` }],
    ])
    box.call('POST', '/chapters/images/next', { step: 0 })
    box.wait(BUSY_MS)
    const state = stateNow()
    expect(state.waiting).toEqual({ step: 1, kind: 'pin', satisfied: false })
    expect(state.steps[1]).toMatchObject({ ran: false, shown: state.steps[0]!.shown })
    expect(state.steps[1]!.explain).toBe(
      'Click a highlighted value in the text Latet read from the image. Latet says what it is, which rule found it, and what was sent instead.',
    )
    expect(() => box.call('POST', '/chapters/images/next', { step: 1 })).toThrow('waiting')
    box.call('POST', '/chapters/images/next', { step: 1, seen: ['pin'] }) // the page saw a value opened
    box.wait(BUSY_MS)
    const done = stateNow()
    expect(done).toMatchObject({ finished: true, busy: false })
    expect(done.steps[1]).toMatchObject({ ran: true, shown: state.steps[0]!.shown })
    expect(done.lines.filter((line) => line.step === 1)).toEqual([]) // the agent does nothing here
  })

  it('images: text recognition is on in the chapter, as its settings say, until another chapter starts', () => {
    const box = sandbox()
    expect(box.config.ocr).toBe('off') // the sandbox's base
    box.call('POST', '/chapters/images/start')
    expect(box.config.ocr).toBe('auto')
    box.call('POST', '/chapters/terms/start')
    expect(box.config.ocr).toBe('off')
  })

  it('images: its card says what it is, and its end says what is read and what is not, and leads to no other chapter', () => {
    const box = sandbox()
    const menu = box.call('GET', '/chapters') as { chapters: { id: string; title: string; minutes: number; learn: string[]; phase: number; missing: string[] }[] }
    expect(menu.chapters.find((c) => c.id === 'images')).toMatchObject({
      title: 'Screenshots',
      minutes: 3,
      phase: 2,
      learn: [
        'What Latet does with a screenshot your agent sends',
        'Where the dashboard shows the image, and the text read in it',
        'What Latet does not read',
      ],
    })
    const { state } = playThrough(box, 'images')
    expect(state.end).toEqual({
      learned: [
        'With text recognition installed, Latet reads the text in a screenshot on your computer, and covers each value with its fake before the image leaves.',
        'The dashboard shows the image as it was sent, and the text read in it with each value marked.',
        "A PDF, or an image sent by its address, is not read: it leaves as it is, marked Not inspected, unless Images and PDFs in Rules is set to Only masked or Don't send, and then the request is not sent.",
      ],
      where: [
        { label: 'Images and PDFs, in Rules', to: '/rules#data' },
        { label: 'The request with the screenshot', to: `/exchanges/${state.steps[0]!.shown!.exchange}` },
      ],
      next: null, // the last chapter
    })
  })

  it('llm: its end leads on to the screenshots', () => {
    const { state } = playThrough(sandbox(), 'llm')
    expect(state.end!.next).toBe('images')
  })

  it('live and settings: their ends open Approvals on the tab they are about (the texts, the change)', () => {
    const approvals = (chapter: string) => playThrough(sandbox(), chapter).state.end!.where.filter((w) => w.to.startsWith('/approvals')).map((w) => w.to)
    expect(approvals('live')).toEqual(['/approvals#texts'])
    expect(approvals('settings')).toEqual(['/approvals#settings'])
  })
})

describe('the mock tutorial: without your text recognition models (&mock-tutorial-no-ocr)', () => {
  const menuOf = (box: Sandbox) => (box.call('GET', '/chapters') as { chapters: { id: string; missing: string[] }[] }).chapters

  it('without text recognition the screenshots chapter says what it needs and does not start', () => {
    const box = sandbox(null, { noOcr: true })
    const menu = box.call('GET', '/chapters') as { chapters: { id: string; missing: string[] }[] }
    expect(menu.chapters.find((c) => c.id === 'images')!.missing).toEqual(['ocr'])
    expect(() => box.call('POST', '/chapters/images/start')).toThrow('needs ocr')
  })

  it('misses nothing else: no other chapter needs text recognition, and with it none misses anything', () => {
    expect(menuOf(sandbox(null, { noOcr: true })).filter((c) => c.missing.length > 0).map((c) => c.id)).toEqual(['images'])
    expect(menuOf(sandbox()).every((c) => c.missing.length === 0)).toBe(true)
    expect(menuOf(sandbox(null, { noOcr: false })).every((c) => c.missing.length === 0)).toBe(true)
  })

  it('says it in the chapter\'s own view too, as the daemon does, and the other chapters play as ever', () => {
    const box = sandbox(null, { noOcr: true })
    expect((box.call('GET', '/chapters/images') as TutorialChapter).missing).toEqual(['ocr'])
    expect((box.call('GET', '/chapters/terms') as TutorialChapter).missing).toEqual([])
    const { state } = playThrough(box, 'terms')
    expect(state.finished).toBe(true)
    expect(box.config.ocr).toBe('off')
  })

  it('refuses the start with the state to go on from: none when nothing is in progress, the chapter\'s when one is, nothing reset', () => {
    const box = sandbox(null, { noOcr: true })
    const refused = (): unknown => {
      try {
        box.call('POST', '/chapters/images/start')
      } catch (e) {
        return e
      }
      return undefined
    }
    const first = refused()
    expect(first).toBeInstanceOf(ApiError)
    expect((first as ApiError).status).toBe(409)
    expect((first as ApiError).body).toEqual({ error: 'needs ocr', state: null }) // as the daemon answers it
    expect(conflictState(first)).toBeNull()

    box.call('POST', '/chapters/terms/start')
    box.call('POST', '/chapters/terms/next', { step: 0 })
    box.wait(BUSY_MS)
    const kept = (box.call('GET', '/chapters/terms/state') as { state: TutorialState }).state
    expect(kept).toMatchObject({ chapter: 'terms', next: 1 })
    expect(box.history.length).toBeGreaterThan(0)
    const error = refused()
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).message).toBe('needs ocr')
    expect(conflictState(error)).toMatchObject({ chapter: 'terms', run: kept.run, next: 1 })
    // Nothing was reset: the chapter in progress, its history and its run are as they were.
    expect((box.call('GET', '/chapters/terms/state') as { state: TutorialState }).state).toEqual(kept)
    expect(box.history.length).toBeGreaterThan(0)
    expect(box.tutorial.me().current).toMatchObject({ chapter: 'terms', step: 1 })
  })

  it('opens on the page as a chapter with no state, for the page to say what to install', async () => {
    const box = sandbox(null, { noOcr: true })
    const calls: string[] = []
    const api: OpenApi = {
      chapter: async (id) => box.call('GET', `/chapters/${id}`) as TutorialChapter,
      state: async (id) => (box.call('GET', `/chapters/${id}/state`) as { state: TutorialState }),
      start: async (id) => {
        calls.push(id)
        return box.call('POST', `/chapters/${id}/start`) as Opened
      },
    }
    const opened = await openChapter(api, 'images', false, new AbortController().signal, 'en')
    expect(opened.state).toBeNull()
    expect(opened.chapter.missing).toEqual(['ocr'])
    expect(calls).toEqual([]) // the page does not even ask to start it
    // A chapter that needs nothing opens and starts as ever.
    expect((await openChapter(api, 'terms', false, new AbortController().signal, 'en')).state).not.toBeNull()
  })
})

describe('the page, with the mock', () => {
  it('says a run failed, sends nothing by itself, and runs the step when the user tries again', () => {
    const box = sandbox(null, { failOnce: true })
    const stateNow = () => (box.call('GET', '/chapters/terms/state') as { state: TutorialState }).state
    const { chapter } = box.call('POST', '/chapters/terms/start') as { chapter: TutorialChapter }
    box.call('POST', '/chapters/terms/next', { step: 0 })
    box.wait(BUSY_MS)
    let state = stateNow()
    expect(state).toMatchObject({ busy: false, next: 0, error: STEP_FAILED, error_step: 0 })
    expect(primaryAction(chapter, state, initialView(chapter.steps.length, state))).toBe('retry')
    box.call('POST', '/chapters/terms/next', { step: 0 })
    box.wait(BUSY_MS)
    state = stateNow()
    expect(state).toMatchObject({ busy: false, next: 1, error: null, error_step: null })
    expect(state.lines.filter((line) => line.type === 'prompt')).toHaveLength(1) // asked twice, typed once

    // A wait met sends `next` by itself, once: after a failed run, the user decides.
    box.host.patchConfig({ terms: [{ value: 'Project Falcon', type: 'CUSTOM', case_sensitive: false, whole_word: true }] })
    let view = initialView(chapter.steps.length, stateNow())
    expect(autoSends(chapter, stateNow(), view)).toBe(true)
    view = viewReducer(view, { type: 'send' })
    box.call('POST', '/chapters/terms/next', nextBody(stateNow(), view))
    box.wait(BUSY_MS)
    state = stateNow()
    view = viewReducer(view, { type: 'answer', state })
    expect(state.error).toBe(STEP_FAILED)
    expect(state.error_step).toBe(1) // the step that failed, not the one before it
    expect(autoSends(chapter, state, view)).toBe(false)
    expect(primaryAction(chapter, state, view)).toBe('retry')
  })

  it('reads a chapter that is already past the first step when it opens again', () => {
    const box = sandbox()
    box.call('POST', '/chapters/basics/start')
    box.call('POST', '/chapters/basics/next', { step: 0 })
    box.wait(BUSY_MS)
    box.call('POST', '/chapters/basics/next', { step: 1 })
    // Reloaded while the second step works: busy, and at the second step.
    const state = (box.call('GET', '/chapters/basics/state') as { state: TutorialState }).state
    const chapter = box.call('GET', '/chapters/basics') as TutorialChapter
    const view = initialView(chapter.steps.length, state)
    expect(view.step).toBe(1)
    expect(phaseOf(chapter, state, view)).toBe('running')
    expect(visibleResult(state, 1)?.explain).toContain('Latet swapped the key')
    box.wait(BUSY_MS)
    const after = (box.call('GET', '/chapters/basics/state') as { state: TutorialState }).state
    expect(phaseOf(chapter, after, viewReducer(view, { type: 'poll', state: after }))).toBe('shown')
  })
})
