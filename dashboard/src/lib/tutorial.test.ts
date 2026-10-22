import { describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../api/client'
import type {
  ExchangeDetail,
  ExchangeSummary,
  Leak,
  Me,
  Replacement,
  Slot,
  TutorialCard,
  TutorialChapter,
  TutorialLine,
  TutorialMenu,
  TutorialShown,
  TutorialState,
  TutorialStep,
  TutorialStepKind,
  TutorialWaitKind,
} from '../api/types'
import { OWN_KEYS, type KeyFacts } from './keys'
import {
  autoSends,
  cannotStart,
  conflictState,
  countsAsNext,
  enterAction,
  errorAt,
  explainKey,
  FIRST_POLL_MS,
  flashes,
  groupLines,
  homePath,
  identityOffered,
  initialView,
  isDashboardPath,
  keepsButton,
  laterPath,
  menuCards,
  nextBody,
  nextCard,
  openChapter,
  openPath,
  phaseOf,
  pinMatches,
  POLL_MS,
  pollDelay,
  primaryAction,
  requestedCard,
  resumedLines,
  resumePath,
  reviewerAskable,
  reviewerOffered,
  targetOf,
  triesAgain,
  tutorialElsewhere,
  typedPrompt,
  viewReducer,
  visibleResult,
  waitMet,
  WHY_ID,
  type OpenApi,
  type View,
} from './tutorial'

// ---- The page's flow --------------------------------------------------------------------------------

const step = (kind: TutorialStepKind, extra: Partial<TutorialStep> = {}): TutorialStep => ({
  index: 0,
  kind,
  title: kind,
  prompt: kind === 'do' ? null : 'make no mistakes',
  button: kind === 'do' ? null : 'Send it',
  wait: null,
  resumed: [],
  ...extra,
})
const wait = (kind: TutorialWaitKind) => ({ kind, hint: 'do it' })
const chapterOf = (...steps: TutorialStep[]) => ({ steps: steps.map((s, index) => ({ ...s, index })) })

// The shape of `basics`: two watch steps, one `do` that waits for a pin, a last watch step.
const BASICS = chapterOf(step('watch'), step('watch', { button: 'Let it write' }), step('do', { wait: wait('pin') }), step('watch'))

const stateOf = (chapter: { steps: readonly TutorialStep[] }, ran: number[], extra: Partial<TutorialState> = {}): TutorialState => ({
  chapter: 'basics',
  run: 'r1',
  next: Math.min(...chapter.steps.map((s) => s.index).filter((i) => !ran.includes(i)), chapter.steps.length),
  busy: false,
  finished: ran.length === chapter.steps.length,
  lines: [],
  steps: chapter.steps.map((s) => ({ index: s.index, ran: ran.includes(s.index), explain: ran.includes(s.index) ? `explain ${s.index}` : '', shown: null })),
  waiting: null,
  end: null,
  error: null,
  error_step: null,
  ...extra,
})

const at = (chapter: { steps: readonly TutorialStep[] }, state: TutorialState, over: Partial<View> = {}): View => ({ ...initialView(chapter.steps.length, state), ...over })

describe('the phases of a step', () => {
  it('starts at the first step, ready to send it', () => {
    const state = stateOf(BASICS, [])
    const view = at(BASICS, state)
    expect(view).toEqual({ steps: 4, step: 0, pinned: false, sending: false, failed: false, reads: 0, unread: false })
    expect(phaseOf(BASICS, state, view)).toBe('ready')
    expect(primaryAction(BASICS, state, view)).toBe('send')
  })

  it('runs while the call is out, with nothing to press', () => {
    const state = stateOf(BASICS, [])
    const view = viewReducer(at(BASICS, state), { type: 'send' })
    expect(phaseOf(BASICS, state, view)).toBe('running')
    expect(primaryAction(BASICS, state, view)).toBeNull()
  })

  it('shows what a step did once it ran, and Next moves on in the page only', () => {
    const state = stateOf(BASICS, [0])
    let view = viewReducer(at(BASICS, state, { step: 0 }), { type: 'answer', state })
    expect(phaseOf(BASICS, state, view)).toBe('shown')
    expect(primaryAction(BASICS, state, view)).toBe('next')
    view = viewReducer(view, { type: 'advance' })
    expect(view.step).toBe(1)
    expect(phaseOf(BASICS, state, view)).toBe('ready') // step 2 is for the daemon to run
  })

  it('ends the chapter after the last step: Finish, then the next chapter or the menu', () => {
    const all = stateOf(BASICS, [0, 1, 2, 3], { end: { learned: ['a', 'b', 'c'], where: [], next: 'terms' } })
    let view = at(BASICS, all, { step: 3 })
    expect(phaseOf(BASICS, all, view)).toBe('shown')
    expect(primaryAction(BASICS, all, view)).toBe('finish')
    view = viewReducer(view, { type: 'advance' })
    expect(phaseOf(BASICS, all, view)).toBe('end')
    expect(primaryAction(BASICS, all, view)).toBe('chapter')
    expect(primaryAction(BASICS, { ...all, end: { ...all.end!, next: null } }, view)).toBe('menu')
    expect(viewReducer(view, { type: 'advance' }).step).toBe(4) // never past the end
  })

  it('waits for the user at a do step, and the page sees a pin by itself', () => {
    const state = stateOf(BASICS, [0, 1], { waiting: { step: 2, kind: 'pin', satisfied: false } })
    let view = at(BASICS, state)
    expect(view.step).toBe(2)
    expect(phaseOf(BASICS, state, view)).toBe('waiting')
    expect(primaryAction(BASICS, state, view)).toBeNull()
    expect(waitMet(state.waiting!, view)).toBe(false)
    expect(autoSends(BASICS, state, view)).toBe(false)

    view = viewReducer(view, { type: 'pinned' })
    expect(waitMet(state.waiting!, view)).toBe(true)
    expect(autoSends(BASICS, state, view)).toBe(true) // the agent tries again, without a button
    expect(phaseOf(BASICS, state, view)).toBe('running')
    expect(nextBody(state, view)).toEqual({ step: 2, seen: ['pin'] })

    view = viewReducer(view, { type: 'send' })
    expect(autoSends(BASICS, state, view)).toBe(false) // once: the call is out
  })

  it.each<TutorialWaitKind>(['term', 'allow', 'approve', 'change', 'fields', 'guidance'])('a %s wait is met when the daemon says so', (kind) => {
    const chapter = chapterOf(step('watch'), step('do', { wait: wait(kind) }))
    const open = stateOf(chapter, [0], { waiting: { step: 1, kind, satisfied: false } })
    const view = at(chapter, open)
    expect(phaseOf(chapter, open, view)).toBe('waiting')
    // Pinning something is no answer to a wait the daemon checks.
    expect(autoSends(chapter, open, viewReducer(view, { type: 'pinned' }))).toBe(false)
    const met = { ...open, waiting: { step: 1, kind, satisfied: true } }
    expect(autoSends(chapter, met, view)).toBe(true)
    expect(phaseOf(chapter, met, view)).toBe('running')
    expect(nextBody(met, view)).toEqual({ step: 1 }) // no `seen`: the daemon saw it
  })

  it('does not run a step that has a button before the user presses it', () => {
    // The Live Reviewer: "Send it" starts the agent, and the request then waits for the user's approval.
    const chapter = chapterOf(step('do', { wait: wait('approve'), button: 'Send it' }))
    const idle = stateOf(chapter, [], { waiting: { step: 0, kind: 'approve', satisfied: true } })
    const view = at(chapter, idle)
    expect(phaseOf(chapter, idle, view)).toBe('ready')
    expect(primaryAction(chapter, idle, view)).toBe('send')
    expect(autoSends(chapter, idle, view)).toBe(false)
  })

  it('a request held for approval is both busy and waiting: the user acts', () => {
    const chapter = chapterOf(step('do', { wait: wait('approve'), button: 'Send it' }))
    const held = stateOf(chapter, [], { busy: true, waiting: { step: 0, kind: 'approve', satisfied: false } })
    const view = at(chapter, held)
    expect(phaseOf(chapter, held, view)).toBe('waiting')
    expect(primaryAction(chapter, held, view)).toBeNull()
    expect(pollDelay(held, view)).toBe(FIRST_POLL_MS) // then less often: a request can wait long for you
    // Decided: the held request goes on, and the step is shown when the agent has finished.
    const decided = { ...held, waiting: { step: 0, kind: 'approve' as const, satisfied: true } }
    expect(phaseOf(chapter, decided, view)).toBe('running')
    const done = stateOf(chapter, [0])
    expect(phaseOf(chapter, done, view)).toBe('shown')
  })

  it('a pin step reloaded while the agent tries again is running: the daemon has the pin already', () => {
    // The page sent `seen: ["pin"]` and was reloaded: it no longer knows of the pin, the daemon works on the step.
    const busy = stateOf(BASICS, [0, 1], { busy: true, waiting: { step: 2, kind: 'pin', satisfied: false } })
    const view = at(BASICS, busy)
    expect(view).toMatchObject({ step: 2, pinned: false })
    expect(phaseOf(BASICS, busy, view)).toBe('running')
    expect(primaryAction(BASICS, busy, view)).toBeNull()
    expect(autoSends(BASICS, busy, view)).toBe(false)
  })

  it('does not send by itself after the daemon said a step failed, and offers to try it again', () => {
    // A term step: the wait was met, the page sent `next`, and the daemon's run failed (not busy, an error).
    const chapter = chapterOf(step('watch'), step('do', { wait: wait('term') }))
    const failed = stateOf(chapter, [0], { waiting: { step: 1, kind: 'term', satisfied: true }, error: 'the history did not record it in time', error_step: 1 })
    const view = at(chapter, failed)
    expect(autoSends(chapter, failed, view)).toBe(false) // no loop: each send would fail again
    expect(primaryAction(chapter, failed, view)).toBe('retry')
    // A step with a button: Try again, not its button as if nothing had happened.
    const watch = stateOf(BASICS, [], { error: 'boom', error_step: 0 })
    expect(primaryAction(BASICS, watch, at(BASICS, watch))).toBe('retry')
    // Nothing to press while the daemon or the page is at it, nor once the step ran.
    expect(primaryAction(BASICS, { ...watch, busy: true }, at(BASICS, watch))).toBeNull()
    expect(primaryAction(BASICS, watch, viewReducer(at(BASICS, watch), { type: 'send' }))).toBeNull()
    const ran = stateOf(BASICS, [0], { error: 'boom', error_step: 0 })
    expect(primaryAction(BASICS, ran, at(BASICS, ran, { step: 0 }))).toBe('next')
  })

  it('keeps an error with its step: the step after one that ran with an error has none to look at', () => {
    // Step 0 ran, but not as written: the daemon says so in `error`, and the page shows it with the step.
    const chapter = chapterOf(step('watch'), step('do', { wait: wait('term') }), step('watch'))
    const ran = stateOf(chapter, [0], {
      error: "its explanation names X, which the model never received",
      error_step: 0,
      waiting: { step: 1, kind: 'term', satisfied: true },
    })
    let view = at(chapter, ran, { step: 0 })
    expect(primaryAction(chapter, ran, view)).toBe('next')
    // Next: the step after it is not run yet, and the error is not its own. Its wait is met: the agent goes on.
    view = viewReducer(view, { type: 'advance' })
    expect(autoSends(chapter, ran, view)).toBe(true)
    expect(primaryAction(chapter, ran, view)).toBeNull() // running: the page sends it
    // Likewise a step with a button: Send it, not Try again.
    const buttons = chapterOf(step('watch'), step('watch'))
    const next = stateOf(buttons, [0], { error: 'boom', error_step: 0 })
    expect(primaryAction(buttons, next, at(buttons, next))).toBe('send')
    expect(phaseOf(buttons, next, at(buttons, next))).toBe('ready')
    // The error shown is the one of the step on screen.
    expect(errorAt(ran, 0)).toBe("its explanation names X, which the model never received")
    expect(errorAt(ran, view.step)).toBeNull()
  })

  it('asks for the error of the step on screen only', () => {
    const state = stateOf(BASICS, [0], { error: 'boom', error_step: 1 })
    expect(errorAt(state, 1)).toBe('boom')
    expect(errorAt(state, 0)).toBeNull()
    expect(errorAt(state, 2)).toBeNull()
    expect(errorAt(stateOf(BASICS, [0]), 1)).toBeNull()
  })

  it('says the agent tries again for a step the user unblocked, not for one its button started', () => {
    expect(triesAgain(BASICS, { step: 2 })).toBe(true) // the pin step
    expect(triesAgain(BASICS, { step: 0 })).toBe(false)
    // The request held in the Live Reviewer goes on once decided: it is not tried again.
    expect(triesAgain(chapterOf(step('do', { wait: wait('approve'), button: 'Send it' })), { step: 0 })).toBe(false)
    expect(triesAgain(BASICS, { step: 4 })).toBe(false) // the end
  })

  it('is running after a reload while the daemon is still busy', () => {
    const busy = stateOf(BASICS, [0], { busy: true })
    const view = at(BASICS, busy)
    expect(view.step).toBe(1)
    expect(phaseOf(BASICS, busy, view)).toBe('running')
    expect(primaryAction(BASICS, busy, view)).toBeNull()
    expect(autoSends(BASICS, busy, view)).toBe(false)
    // The answer of the poll: the step is over.
    const next = stateOf(BASICS, [0, 1])
    const view2 = viewReducer(view, { type: 'answer', state: next })
    expect(phaseOf(BASICS, next, view2)).toBe('shown')
  })

  it('restores at the step the daemon runs next, or at the end of a finished chapter', () => {
    expect(initialView(4, stateOf(BASICS, [0, 1])).step).toBe(2)
    expect(initialView(4, stateOf(BASICS, [0, 1, 2, 3])).step).toBe(4)
    expect(phaseOf(BASICS, stateOf(BASICS, [0, 1, 2, 3]), initialView(4, stateOf(BASICS, [0, 1, 2, 3])))).toBe('end')
  })

  it('never gets ahead of the daemon: a chapter started over elsewhere pulls the page back', () => {
    const view = at(BASICS, stateOf(BASICS, [0, 1, 2]))
    expect(view.step).toBe(3)
    const restarted = stateOf(BASICS, [], { run: 'r2' })
    expect(viewReducer(view, { type: 'answer', state: restarted }).step).toBe(0)
    // Behind is fine: the step shown is one the user has not moved on from.
    const behind = at(BASICS, stateOf(BASICS, [0]), { step: 0 })
    expect(viewReducer(behind, { type: 'answer', state: stateOf(BASICS, [0, 1]) }).step).toBe(0)
  })

  it('a poll does not end a call that is out, and a call that answered does', () => {
    const state = stateOf(BASICS, [0])
    const sending = viewReducer(at(BASICS, state), { type: 'send' })
    expect(viewReducer(sending, { type: 'poll', state }).sending).toBe(true) // an old state must not let a second call go out
    expect(viewReducer(sending, { type: 'answer', state }).sending).toBe(false)
    expect(pollDelay({ ...state, busy: true }, sending)).toBeNull() // the call out will say
  })

  it('forgets a pin when the page goes back to another step', () => {
    const view = at(BASICS, stateOf(BASICS, [0, 1]), { pinned: true })
    expect(viewReducer(view, { type: 'answer', state: stateOf(BASICS, []) })).toMatchObject({ step: 0, pinned: false })
  })

  it('stops after a failed call until the user tries again, so a failure cannot loop', () => {
    const state = stateOf(BASICS, [0, 1], { waiting: { step: 2, kind: 'pin', satisfied: false } })
    let view = viewReducer(viewReducer(at(BASICS, state), { type: 'pinned' }), { type: 'send' })
    view = viewReducer(view, { type: 'failed' })
    expect(view).toMatchObject({ sending: false, failed: true })
    expect(autoSends(BASICS, state, view)).toBe(false)
    expect(primaryAction(BASICS, state, view)).toBe('retry')
    view = viewReducer(view, { type: 'retry' })
    expect(view.failed).toBe(false)
    expect(autoSends(BASICS, state, view)).toBe(true)
  })

  it('polls while the agent works or a wait is the daemon\'s to check, never for a pin', () => {
    const idle = stateOf(BASICS, [0])
    const view = at(BASICS, idle)
    expect(pollDelay(idle, view)).toBeNull()
    expect(pollDelay({ ...idle, busy: true }, view)).toBe(FIRST_POLL_MS) // a page opened on a busy daemon too
    expect(pollDelay({ ...idle, busy: true }, { ...view, reads: 9 })).toBe(POLL_MS)
    const pin = stateOf(BASICS, [0, 1], { waiting: { step: 2, kind: 'pin', satisfied: false } })
    expect(pollDelay(pin, at(BASICS, pin))).toBeNull()
    const term = stateOf(BASICS, [0, 1], { waiting: { step: 2, kind: 'term', satisfied: false } })
    expect(pollDelay(term, at(BASICS, term))).toBe(POLL_MS)
    expect(pollDelay({ ...term, waiting: { step: 2, kind: 'term', satisfied: true } }, at(BASICS, term))).toBeNull() // the page sends it
    // Your guidance saved is the daemon's to see (the editor saves it): the page reads the state until it does.
    const guidance = stateOf(BASICS, [0, 1], { waiting: { step: 2, kind: 'guidance', satisfied: false } })
    expect(pollDelay(guidance, at(BASICS, guidance))).toBe(POLL_MS)
  })

  it('does not poll an approval before Send it: no request waits for you until the step runs', () => {
    const chapter = chapterOf(step('do', { wait: wait('approve'), button: 'Send it' }))
    const ready = stateOf(chapter, [], { waiting: { step: 0, kind: 'approve', satisfied: false } })
    const view = at(chapter, ready)
    expect(phaseOf(chapter, ready, view)).toBe('ready')
    expect(pollDelay(ready, view)).toBeNull()
    // Sent: the request is held until you decide, and the step is busy all that time.
    expect(pollDelay({ ...ready, busy: true }, view)).toBe(FIRST_POLL_MS)
  })

  it('reads the state again soon after a call answered busy, then less and less often', () => {
    // The daemon runs every step in the background and answers `next` at once: most steps are done in a moment.
    const busy = stateOf(BASICS, [], { busy: true })
    let view = viewReducer(viewReducer(at(BASICS, stateOf(BASICS, [])), { type: 'send' }), { type: 'answer', state: busy })
    const delays: (number | null)[] = []
    for (let i = 0; i < 5; i++) {
      delays.push(pollDelay(busy, view))
      view = viewReducer(view, { type: 'poll', state: busy })
    }
    expect(delays).toEqual([FIRST_POLL_MS, 2 * FIRST_POLL_MS, 4 * FIRST_POLL_MS, POLL_MS, POLL_MS])
    expect(FIRST_POLL_MS).toBeLessThan(POLL_MS / 4)
    // The next call starts quick again; a wait the daemon checks is read once a second.
    view = viewReducer(viewReducer(view, { type: 'send' }), { type: 'answer', state: busy })
    expect(pollDelay(busy, view)).toBe(FIRST_POLL_MS)
    const term = stateOf(BASICS, [0, 1], { waiting: { step: 2, kind: 'term', satisfied: false } })
    expect(pollDelay(term, view)).toBe(POLL_MS)
  })

  it('asks again at the usual pace while a read fails, not every 150 ms', () => {
    const busy = stateOf(BASICS, [], { busy: true })
    let view = viewReducer(viewReducer(at(BASICS, stateOf(BASICS, [])), { type: 'send' }), { type: 'answer', state: busy })
    expect(pollDelay(busy, view)).toBe(FIRST_POLL_MS)
    view = viewReducer(view, { type: 'unread' }) // the daemon did not answer
    expect(view.unread).toBe(true)
    expect(pollDelay(busy, view)).toBe(POLL_MS)
    expect(pollDelay(busy, viewReducer(view, { type: 'unread' }))).toBe(POLL_MS) // and again
    // It answers again: the quick reads are back. A call that got its answer says the daemon is there, too.
    const read = viewReducer(view, { type: 'poll', state: busy })
    expect(read.unread).toBe(false)
    expect(pollDelay(busy, read)).toBeLessThan(POLL_MS)
    expect(viewReducer(view, { type: 'answer', state: busy }).unread).toBe(false)
    // Nothing to read, nothing asked: a failed read does not make a poll of a wait the page has no use for.
    expect(pollDelay(stateOf(BASICS, [0]), view)).toBeNull()
  })

  it('shows, for a step not run yet, what the nearest step before it has to say', () => {
    const state = stateOf(BASICS, [0], {
      steps: [
        { index: 0, ran: true, explain: 'one', shown: { component: 'none' } },
        { index: 1, ran: false, explain: '', shown: null },
        { index: 2, ran: false, explain: 'click it', shown: { component: 'exchange', exchange: 'ex1' } },
        { index: 3, ran: false, explain: '', shown: null },
      ],
    })
    expect(visibleResult(state, 0)?.explain).toBe('one')
    expect(visibleResult(state, 1)?.explain).toBe('one') // nothing yet: the step before
    expect(visibleResult(state, 2)?.explain).toBe('click it') // a do step has its own before it runs
    expect(visibleResult(state, 3)?.explain).toBe('click it')
    expect(visibleResult({ ...state, steps: [state.steps[1]!] }, 0)).toBeNull()
  })
})

describe('the prompt typed in the agent pane', () => {
  const typed = (chapter: { readonly steps: readonly Pick<TutorialStep, 'button' | 'prompt'>[] }, state: TutorialState, view: View) =>
    typedPrompt(chapter, state, view, phaseOf(chapter, state, view))
  const prompted = (step: number, text: string): TutorialLine => ({ step, type: 'prompt', text })

  it('stays from the start of the step until the daemon puts it in the log', () => {
    const state = stateOf(BASICS, [])
    const view = at(BASICS, state)
    expect(typed(BASICS, state, view)).toBe('make no mistakes') // ready: typed, not sent
    const sending = viewReducer(view, { type: 'send' })
    expect(typed(BASICS, state, sending)).toBe('make no mistakes') // the call is out
    const busy = { ...state, busy: true, lines: [prompted(0, 'make no mistakes')] }
    expect(typed(BASICS, busy, sending)).toBeNull() // in the log: shown there
    // Tried again after a failed run: the log has it already.
    expect(typed(BASICS, { ...state, error: 'boom', error_step: 0, lines: [prompted(0, 'make no mistakes')] }, view)).toBeNull()
  })

  it('stays while a request the Live Reviewer holds is on its way, and is gone once it is in the log', () => {
    // The step is waiting for the user (its request is held) and the call is out, both at once: no flicker.
    const live = chapterOf(step('do', { wait: wait('approve'), button: 'Send it', prompt: 'file this' }))
    const idle = stateOf(live, [], { waiting: { step: 0, kind: 'approve', satisfied: false } })
    const sending = viewReducer(at(live, idle), { type: 'send' })
    expect(phaseOf(live, idle, sending)).toBe('waiting')
    expect(typed(live, idle, sending)).toBe('file this')
    const held = { ...idle, busy: true, lines: [prompted(0, 'file this')] }
    expect(typed(live, held, viewReducer(sending, { type: 'answer', state: held }))).toBeNull()
  })

  it('is none for a step without a prompt, or one that waits for you', () => {
    const state = stateOf(BASICS, [0, 1], { waiting: { step: 2, kind: 'pin', satisfied: false } })
    expect(typed(BASICS, state, at(BASICS, state))).toBeNull() // the pin step: its wait, no prompt
    const waiting = chapterOf(step('do', { wait: wait('term'), prompt: 'again' }))
    const open = stateOf(waiting, [], { waiting: { step: 0, kind: 'term', satisfied: false } })
    expect(typed(waiting, open, at(waiting, open))).toBeNull() // not sent yet, and not sending: the user acts
  })
})

describe('the conversation a step resumes, in the agent pane', () => {
  // What the daemon's chapter gives a step that resumes a conversation from before Latet was on.
  const RESUMED: TutorialLine[] = [
    { step: 0, type: 'system', text: "/resume: yesterday's conversation, from before Latet was on", note: 'resumed' },
    { step: 0, type: 'prompt', text: 'deploy with KEY-1', resumed: true },
    { step: 0, type: 'model', text: 'Deploying.', resumed: true },
  ]
  const STOPPED = chapterOf(step('watch', { prompt: 'continue the deploy', resumed: RESUMED }), step('watch', { prompt: 'again' }))
  const shown = (state: TutorialState, view: View) => resumedLines(STOPPED, state, view, phaseOf(STOPPED, state, view))

  it('is there from the start of the step, before Send it, as /resume shows it, above the typed prompt', () => {
    const state = stateOf(STOPPED, [])
    const view = at(STOPPED, state)
    expect(shown(state, view)).toEqual(RESUMED)
    expect(typedPrompt(STOPPED, state, view, phaseOf(STOPPED, state, view))).toBe('continue the deploy')
    expect(shown(state, viewReducer(view, { type: 'send' }))).toEqual(RESUMED) // the call is out
  })

  it('gives way to the same lines in the log once the step is sent, and is none for a step that resumes none', () => {
    const sent = { ...stateOf(STOPPED, []), busy: true, lines: [...RESUMED, { step: 0, type: 'prompt', text: 'continue the deploy' }] as TutorialLine[] }
    expect(shown(sent, viewReducer(at(STOPPED, sent), { type: 'send' }))).toEqual([]) // the log shows them now
    const next = stateOf(STOPPED, [0], { lines: sent.lines })
    expect(shown(next, at(STOPPED, next))).toEqual([]) // the step on screen is the next one, which resumes none
    expect(shown(next, at(STOPPED, next, { step: 0 }))).toEqual([]) // the step that ran: its lines are in the log
  })
})

// ---- Enter -------------------------------------------------------------------------------------------

const PLAIN: KeyFacts = {
  key: 'Enter',
  repeat: false,
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  defaultPrevented: false,
  ownedByTarget: false,
  dialogOpen: false,
}

describe('Enter means Next', () => {
  it('counts a plain, first press that nothing else took', () => {
    expect(countsAsNext(PLAIN)).toBe(true)
  })

  it.each<[string, Partial<KeyFacts>]>([
    ['another key', { key: ' ' }],
    ['a key held down', { repeat: true }],
    ['Alt', { altKey: true }],
    ['Control', { ctrlKey: true }],
    ['Meta', { metaKey: true }],
    ['Shift', { shiftKey: true }],
    ['a key another handler took', { defaultPrevented: true }],
    ['Enter on a button, a link or a value (it presses it)', { ownedByTarget: true }],
    ['a dialog open', { dialogOpen: true }],
  ])('does not count %s', (_, over) => {
    expect(countsAsNext({ ...PLAIN, ...over })).toBe(false)
  })

  it('leaves Enter to the buttons and dialogs that keep their own keys', () => {
    expect(OWN_KEYS.split(',').map((selector) => selector.trim())).toEqual(expect.arrayContaining(['button', 'dialog', '[role="dialog"]']))
  })

  it('does what the phase says, and nothing while the user has something to do', () => {
    const ready = stateOf(BASICS, [])
    expect(enterAction(BASICS, ready, at(BASICS, ready), PLAIN)).toBe('send')
    const shown = stateOf(BASICS, [0])
    expect(enterAction(BASICS, shown, at(BASICS, shown, { step: 0 }), PLAIN)).toBe('next')
    const waiting = stateOf(BASICS, [0, 1], { waiting: { step: 2, kind: 'pin', satisfied: false } })
    expect(enterAction(BASICS, waiting, at(BASICS, waiting), PLAIN)).toBeNull()
    expect(enterAction(BASICS, ready, at(BASICS, ready), { ...PLAIN, repeat: true })).toBeNull()
  })
})

// ---- What to point at ---------------------------------------------------------------------------------

// Values with spaces in them, so that no secret scanner takes this file's invented key for a real one.
const KEY = 'Qm7 xT2 pLw 9Rk 3Jd 8Hv'
const KEY_FAKE = 'Zc4 bN1 vXu 6Es 5Ga 2Fy'
const EMAIL = 'james.wilson@acme.com'
const EMAIL_FAKE = 'emily.adams@proseware-4795.example'
const PHONE = '+44 20 7946 0958'
const PHONE_FAKE = '+44 28 9578 4573'

/** The replacement of `original` by `fake`, where each is in the two texts of its block. */
const replacement = (original: string, fake: string, texts: { original: string; masked: string }, extra: Partial<Replacement> = {}): Replacement => {
  const start = [...texts.original.slice(0, texts.original.indexOf(original))].length
  const masked_start = [...texts.masked.slice(0, texts.masked.indexOf(fake))].length
  return {
    start,
    end: start + [...original].length,
    masked_start,
    masked_end: masked_start + [...fake].length,
    type: 'EMAIL',
    class: 'PII',
    rule: 'pii:email',
    module: 'personal',
    via: 'detector',
    entity: `e-${original.length}`,
    known: true,
    ...extra,
  }
}

const slotOf = (original: string, masked: string, replacements: Replacement[], extra: Partial<Slot> = {}): Slot => ({
  block: `b-${masked.length}`,
  path: '$.messages[0].content',
  kind: 'user_text',
  from_model: false,
  new: true,
  original,
  masked,
  replacements,
  ...extra,
})

const summaryOf = (extra: Partial<ExchangeSummary> = {}): ExchangeSummary => ({
  id: 'ex_1',
  ts: 0,
  session: 's',
  agent: 'tutorial-agent',
  agent_version: null,
  upstream: 'anthropic',
  path: '/v1/messages',
  protocol: 'anthropic',
  mode: 'rewrite',
  transport: 'http',
  model: 'tutorial-model',
  status: 200,
  latency_ms: 1,
  overhead_ms: 1,
  tokens: null,
  masked: 0,
  new_masked: 0,
  by_type: {},
  new_by_type: {},
  restored: 0,
  flags: [],
  verified: true,
  ...extra,
})

const detailOf = (slots: Slot[], extra: Partial<ExchangeDetail> = {}, summary: Partial<ExchangeSummary> = {}): ExchangeDetail => ({
  summary: summaryOf(summary),
  request: { headers: [], slots, body_bytes: 0 },
  response: null,
  leaks: [],
  ...extra,
})

const exchange = (extra: Partial<TutorialShown> = {}): TutorialShown => ({ component: 'exchange', exchange: 'ex_1', part: 'request', ...extra })

describe('targetOf: what the page points at in a request', () => {
  const env = `# billing\nDEMO_API_KEY=${KEY}\n`
  const envMasked = env.replace(KEY, KEY_FAKE)
  const keyRule = replacement(KEY, KEY_FAKE, { original: env, masked: envMasked }, { type: 'GENERIC_API_KEY', class: 'SECRET', rule: 'gitleaks:generic-api-key', module: 'secrets' })
  const prompt = `email ${EMAIL} the invoice, his number is ${PHONE}. Who is JSON?`
  const promptMasked = prompt.replace(EMAIL, EMAIL_FAKE).replace(PHONE, PHONE_FAKE)

  it('is the pair of a value masked in the block, with the scope the block view gets', () => {
    const detail = detailOf([slotOf('make no mistakes', 'make no mistakes', []), slotOf(env, envMasked, [keyRule])])
    expect(targetOf(detail, exchange({ slot: 1, highlight: [KEY] }))).toEqual({ pairs: ['ex_1:1:0'], blocks: [] })
  })

  it('is the pairs of two values, in the order of the highlight', () => {
    const texts = { original: prompt, masked: promptMasked }
    const email = replacement(EMAIL, EMAIL_FAKE, texts)
    const phone = replacement(PHONE, PHONE_FAKE, texts, { type: 'PHONE', rule: 'pii:phone' })
    const detail = detailOf([slotOf(prompt, promptMasked, [phone, email])]) // the daemon's order is not the text's
    // Pairs are numbered by where the value is in the sent text: the email comes first.
    expect(targetOf(detail, exchange({ slot: 0, highlight: [PHONE, EMAIL] }))).toEqual({ pairs: ['ex_1:0:1', 'ex_1:0:0'], blocks: [] })
  })

  it('is the block when the value is in clear', () => {
    const roadmap = 'Project Falcon ships in March.'
    const detail = detailOf([slotOf(roadmap, roadmap, [], { kind: 'tool_result' })])
    expect(targetOf(detail, exchange({ slot: 0, highlight: ['Project Falcon'] }))).toEqual({ pairs: [], blocks: ['block-0'] })
  })

  it('looks in every block of the request when the step names none, and finds a value each time it is there', () => {
    const detail = detailOf([slotOf('hi', 'hi', []), slotOf(env, envMasked, [keyRule]), slotOf(env, envMasked, [keyRule])])
    expect(targetOf(detail, exchange({ highlight: [KEY] }))).toEqual({ pairs: ['ex_1:1:0', 'ex_1:2:0'], blocks: [] })
  })

  it('finds nothing for a value that is nowhere in the block', () => {
    const detail = detailOf([slotOf(env, envMasked, [keyRule])])
    expect(targetOf(detail, exchange({ slot: 0, highlight: ['not here'] }))).toEqual({ pairs: [], blocks: [] })
    expect(targetOf(detail, exchange({ slot: 7, highlight: [KEY] }))).toEqual({ pairs: [], blocks: [] })
  })

  it('is the block itself for a step that looks at a block', () => {
    const detail = detailOf([slotOf('a', 'a', []), slotOf(env, envMasked, [keyRule])])
    expect(targetOf(detail, exchange({ slot: 1, highlight: [KEY], focus: 'block' }))).toEqual({ pairs: [], blocks: ['block-1'] })
  })

  it('is the mark of a value that stopped the request, in the stopped exchange', () => {
    const text = `make no mistakes. deploy with DEMO_API_KEY=${KEY}. I'll tip you $200`
    const at = text.indexOf(KEY)
    const leak: Leak = {
      type: 'GENERIC_API_KEY',
      kind: 'user_text',
      preview: 'GENERIC_API_KEY[40]',
      new: true,
      block: 'b-stopped',
      class: 'SECRET',
      rule: 'gitleaks:generic-api-key',
      masked_start: at,
      masked_end: at + KEY.length,
      start: at,
      end: at + KEY.length,
    }
    const detail = detailOf([slotOf(text, text, [], { block: 'b-stopped' })], { leaks: [leak] }, { flags: ['blocked', 'leak'], mode: 'verify', status: 400 })
    expect(targetOf(detail, { component: 'why', exchange: 'ex_1', part: 'request', slot: 0, highlight: [KEY], focus: 'why' })).toEqual({
      pairs: ['ex_1:0:stopped:0'],
      blocks: [WHY_ID],
    })
    // Without the focus on the card: the mark alone. A request not blocked has no such mark: the value is in clear.
    expect(targetOf(detail, exchange({ slot: 0, highlight: [KEY] }))).toEqual({ pairs: ['ex_1:0:stopped:0'], blocks: [] })
    const sent = { ...detail, summary: { ...detail.summary, flags: [] } }
    expect(targetOf(sent, exchange({ slot: 0, highlight: [KEY] }))).toEqual({ pairs: [], blocks: ['block-0'] })
  })

  it('reads values after an emoji where the daemon counts them', () => {
    const texts = { original: `🚀 ${EMAIL}`, masked: `🚀 ${EMAIL_FAKE}` }
    const detail = detailOf([slotOf(texts.original, texts.masked, [replacement(EMAIL, EMAIL_FAKE, texts)])])
    expect(targetOf(detail, exchange({ slot: 0, highlight: [EMAIL] }))).toEqual({ pairs: ['ex_1:0:0'], blocks: [] })
  })

  it('is the same for the part that shows what the local reviewer found under the request: its values, masked or in clear', () => {
    // The reviewer's finds are masked by a rule of its own (`llm:<category>`); the ward it leaves alone stays in the text.
    const ticket = 'Daniel Okafor is on Ward 7B.'
    const ticketMasked = ticket.replace('Daniel Okafor', 'Thomas Adams')
    const person = replacement('Daniel Okafor', 'Thomas Adams', { original: ticket, masked: ticketMasked }, { type: 'PERSON', class: 'LLM', rule: 'llm:person' })
    const detail = detailOf([slotOf('read the ticket', 'read the ticket', []), slotOf(ticket, ticketMasked, [person], { kind: 'tool_result' })])
    const reviewed = (extra: Partial<TutorialShown>): TutorialShown => ({ component: 'review', exchange: 'ex_1', part: 'request', slot: 1, ...extra })
    expect(targetOf(detail, reviewed({ highlight: ['Daniel Okafor'] }))).toEqual({ pairs: ['ex_1:1:0'], blocks: [] })
    expect(targetOf(detail, reviewed({ highlight: ['Ward 7B'] }))).toEqual({ pairs: [], blocks: ['block-1'] })
    expect(targetOf(detail, reviewed({ slot: undefined, highlight: ['Daniel Okafor', 'Ward 7B'] }))).toEqual({ pairs: ['ex_1:1:0'], blocks: ['block-1'] }) // no block named: every one
    expect(targetOf(detail, reviewed({ slot: 0, highlight: ['Daniel Okafor'] }))).toEqual({ pairs: [], blocks: [] }) // not in the block named
  })

  it('is the same for the part that shows an image: the values of the text read in it, a block of its own', () => {
    // The text read in a picture is a block of its own (kind image_text), which the step names with `slot`.
    const read = `Payment refused. Contact ${EMAIL} to pay.`
    const readMasked = read.replace(EMAIL, EMAIL_FAKE)
    const email = replacement(EMAIL, EMAIL_FAKE, { original: read, masked: readMasked })
    const detail = detailOf([slotOf('why was it refused?', 'why was it refused?', []), slotOf(read, readMasked, [email], { kind: 'image_text' })])
    const pictured = (extra: Partial<TutorialShown>): TutorialShown => ({
      component: 'images',
      exchange: 'ex_1',
      part: 'request',
      slot: 1,
      original: '/tutorial/chapters/images/pictures/payment.png',
      ...extra,
    })
    expect(targetOf(detail, pictured({ highlight: [EMAIL] }))).toEqual({ pairs: ['ex_1:1:0'], blocks: [] })
    expect(targetOf(detail, pictured({ highlight: ['Payment refused'] }))).toEqual({ pairs: [], blocks: ['block-1'] }) // read, not masked
    expect(targetOf(detail, pictured({ highlight: [EMAIL, 'Payment refused'] }))).toEqual({ pairs: ['ex_1:1:0'], blocks: ['block-1'] })
    expect(targetOf(detail, pictured({ slot: 0, highlight: [EMAIL] }))).toEqual({ pairs: [], blocks: [] }) // not in the block named
    expect(targetOf(detail, pictured({ highlight: [EMAIL], focus: 'block' }))).toEqual({ pairs: [], blocks: ['block-1'] })
  })

  it('points at nothing in a part that is not an exchange', () => {
    const detail = detailOf([slotOf(env, envMasked, [keyRule])])
    for (const component of ['terms', 'command', 'approvals', 'changes', 'columns', 'guidance', 'none'] as const) {
      expect(targetOf(detail, { component, exchange: 'ex_1', slot: 0, highlight: [KEY] })).toEqual({ pairs: [], blocks: [] })
    }
  })
})

describe('targetOf: what the page points at in a response', () => {
  const text = `Who is JSON? I wrote down ${EMAIL_FAKE}.`
  const restored = `Who is JSON? I wrote down ${EMAIL}.`
  const spans = [
    {
      start: restored.indexOf(EMAIL),
      end: restored.indexOf(EMAIL) + EMAIL.length,
      masked_start: text.indexOf(EMAIL_FAKE),
      masked_end: text.indexOf(EMAIL_FAKE) + EMAIL_FAKE.length,
      type: 'EMAIL',
      class: 'PII' as const,
      rule: 'pii:email',
      module: 'personal' as const,
      via: 'restore' as const,
      entity: 'e1',
      known: true,
    },
  ]
  const args = JSON.stringify({ file_path: 'config.local.json', content: KEY_FAKE })
  const argsRestored = JSON.stringify({ file_path: 'config.local.json', content: KEY })
  const keySpan = {
    ...spans[0]!,
    start: argsRestored.indexOf(KEY),
    end: argsRestored.indexOf(KEY) + KEY.length,
    masked_start: args.indexOf(KEY_FAKE),
    masked_end: args.indexOf(KEY_FAKE) + KEY_FAKE.length,
    type: 'GENERIC_API_KEY',
    class: 'SECRET' as const,
    rule: 'gitleaks:generic-api-key',
  }
  const response = {
    headers: [],
    text_masked: text,
    text_restored: restored,
    text_spans: spans,
    tool_calls: [{ name: 'Write', args_masked: args, args_restored: argsRestored, spans: [keySpan] }],
  }
  const detail = detailOf([slotOf('hi', 'hi', [])], { response })

  it('is the pair of a value in the answer text (slot -1), with the scope the response view gives it', () => {
    expect(targetOf(detail, exchange({ part: 'response', slot: -1, highlight: [EMAIL] }))).toEqual({ pairs: ['response:0'], blocks: [] })
  })

  it('is the pair of a value in a tool call (its index)', () => {
    expect(targetOf(detail, exchange({ part: 'response', slot: 0, highlight: [KEY] }))).toEqual({ pairs: ['tool0:0'], blocks: [] })
  })

  it('looks at the text and every tool call when the step names none', () => {
    expect(targetOf(detail, exchange({ part: 'response', highlight: [KEY, EMAIL] }))).toEqual({ pairs: ['tool0:0', 'response:0'], blocks: [] })
  })

  it('is the part itself for a value in clear', () => {
    expect(targetOf(detail, exchange({ part: 'response', slot: -1, highlight: ['Who is JSON?'] }))).toEqual({ pairs: [], blocks: ['response-text'] })
    expect(targetOf(detail, exchange({ part: 'response', slot: 0, highlight: ['config.local.json'] }))).toEqual({ pairs: [], blocks: ['response-tool-0'] })
  })

  it('finds a value by the fakes it knows when the daemon sent no spans', () => {
    const texts = { original: `mail ${EMAIL}`, masked: `mail ${EMAIL_FAKE}` }
    const old = { ...response, text_spans: undefined }
    const known = detailOf([slotOf(texts.original, texts.masked, [replacement(EMAIL, EMAIL_FAKE, texts)])], { response: old })
    expect(targetOf(known, exchange({ part: 'response', slot: -1, highlight: [EMAIL] }))).toEqual({ pairs: ['response:0'], blocks: [] })
  })

  it('has nothing to point at when there is no response', () => {
    expect(targetOf(detailOf([]), exchange({ part: 'response', slot: -1, highlight: [EMAIL] }))).toEqual({ pairs: [], blocks: [] })
  })
})

describe('finding it later in the dashboard', () => {
  it('opens the exchange, the terms, or the tab of Approvals where the normal dashboard shows them', () => {
    expect(laterPath(exchange({ exchange: 'ex 1' }))).toBe('/exchanges/ex%201')
    expect(laterPath({ component: 'why', exchange: 'ex_2' })).toBe('/exchanges/ex_2')
    expect(laterPath({ component: 'columns', exchange: 'ex_3', slot: 4 })).toBe('/exchanges/ex_3')
    expect(laterPath({ component: 'terms' })).toBe('/rules#terms')
    expect(laterPath({ component: 'approvals' })).toBe('/approvals#texts')
    expect(laterPath({ component: 'changes' })).toBe('/approvals#settings')
  })

  it("opens the exchange, with the local reviewer's panel, for what the reviewer found; Rules for your guidance", () => {
    expect(laterPath({ component: 'review', exchange: 'ex 5', part: 'request', slot: 2 })).toBe('/exchanges/ex%205')
    expect(laterPath({ component: 'guidance', text: 'Ward names are confidential.' })).toBe('/rules#reviewer')
    expect(laterPath({ component: 'guidance' })).toBe('/rules#reviewer') // the sentence is the editor's, not the link's
  })

  it('opens the exchange, with its images, for the part that shows an image', () => {
    const original = '/tutorial/chapters/images/pictures/payment.png' // the picture's own path says nothing about where to go
    expect(laterPath({ component: 'images', exchange: 'ex 6', part: 'request', slot: 1, original })).toBe('/exchanges/ex%206')
  })

  it('has nowhere to go for a command, nothing shown, or an exchange it does not name', () => {
    expect(laterPath({ component: 'command', command: 'latet claude' })).toBeNull()
    expect(laterPath({ component: 'none' })).toBeNull()
    expect(laterPath({ component: 'exchange' })).toBeNull()
    expect(laterPath({ component: 'review' })).toBeNull()
    expect(laterPath({ component: 'images' })).toBeNull()
  })

  it('follows only the dashboard\'s own paths from a chapter\'s end', () => {
    expect(isDashboardPath('/exchanges/ex_1')).toBe(true)
    expect(isDashboardPath('/rules#terms')).toBe(true)
    expect(isDashboardPath('https://example.com/')).toBe(false)
    expect(isDashboardPath('//example.com/')).toBe(false)
    expect(isDashboardPath('exchange:write')).toBe(false)
  })
})

describe("a chapter's picture on this computer", () => {
  it('is asked for at the path a step gives, which the daemon encoded already: not encoded again', () => {
    expect(api.tutorial.picture('/tutorial/chapters/images/pictures/payment.png')).toBe('/_latet/api/v1/tutorial/chapters/images/pictures/payment.png')
    expect(api.tutorial.picture('/tutorial/chapters/images/pictures/bank%20statement.png')).toBe(
      '/_latet/api/v1/tutorial/chapters/images/pictures/bank%20statement.png',
    )
  })
})

describe('pinning the value asked for', () => {
  it('is met by a value of the target, on either side of it', () => {
    const target = { pairs: ['ex_1:1:0'], blocks: [] }
    expect(pinMatches({ pair: 'ex_1:1:0' }, target)).toBe(true)
    expect(pinMatches({ pair: 'ex_1:1:1' }, target)).toBe(false)
    expect(pinMatches({ pair: 'ex_1:1:0' }, { pairs: [], blocks: ['block-1'] })).toBe(false)
  })
})

// ---- The menu and the rest -----------------------------------------------------------------------------

const card = (id: string, number: number, extra: Partial<TutorialCard> = {}): TutorialCard => ({
  id,
  number,
  title: id,
  minutes: 5,
  learn: ['a', 'b', 'c'],
  phase: 1,
  done: false,
  missing: [],
  ...extra,
})

describe('the menu', () => {
  const menu: TutorialMenu = {
    chapters: [card('terms', 2, { done: true }), card('basics', 1, { done: true }), card('images', 9, { phase: 2 }), card('stopped', 4), card('false-positives', 3)],
    requested: null,
    current: { chapter: 'false-positives', step: 1 },
  }

  it('lists every chapter in order, numbered one after the other, each with where it stands', () => {
    const cards = menuCards(menu)
    expect(cards.map((c) => [c.id, c.position, c.state])).toEqual([
      ['basics', 1, 'done'],
      ['terms', 2, 'done'],
      ['false-positives', 3, 'current'],
      ['stopped', 4, 'todo'],
      ['images', 5, 'todo'], // of the second phase: listed all the same
    ])
    expect(cards.every((c) => c.total === 5)).toBe(true)
  })

  it('lists every chapter, numbered by position, whatever numbers and phases the daemon gave', () => {
    const odd: TutorialMenu = { ...menu, chapters: [card('b', 5), card('a', 3), card('x', 4, { phase: 2 })], current: null }
    expect(menuCards(odd).map((c) => [c.id, c.number, c.position])).toEqual([['a', 3, 1], ['x', 4, 2], ['b', 5, 3]])
  })

  it("leaves the daemon's list in the order it came: the page sorts a copy", () => {
    const came: TutorialMenu = { ...menu, chapters: [card('b', 5), card('a', 3)] }
    menuCards(came)
    expect(came.chapters.map((c) => c.id)).toEqual(['b', 'a'])
  })

  it('a chapter done and begun again is in progress', () => {
    const again: TutorialMenu = { ...menu, current: { chapter: 'basics', step: 2 } }
    expect(menuCards(again)[0]).toMatchObject({ id: 'basics', state: 'current', done: true })
  })

  it('a chapter whose last step ran is done, and the one after it is the one to do next', () => {
    // The daemon has no chapter in progress once the last step of the chapter ran.
    const finished = menuCards({ ...menu, current: null })
    expect(finished.map((c) => [c.id, c.state])).toEqual([
      ['basics', 'done'],
      ['terms', 'done'],
      ['false-positives', 'todo'],
      ['stopped', 'todo'],
      ['images', 'todo'],
    ])
    expect(nextCard(finished)?.id).toBe('false-positives')
  })

  it('stands out the chapter in progress, else the first one not done, else none', () => {
    expect(nextCard(menuCards(menu))?.id).toBe('false-positives') // half way: Continue
    // A chapter done and begun again is in progress: before the chapters not done yet.
    expect(nextCard(menuCards({ ...menu, current: { chapter: 'basics', step: 0 } }))?.id).toBe('basics')
    const all = menuCards({ ...menu, chapters: menu.chapters.map((c) => ({ ...c, done: true })), current: null })
    expect(all.every((c) => c.state === 'done')).toBe(true)
    expect(nextCard(all)).toBeNull()
  })

  it('opens a chapter that is done as a new run, and the others where they stand', () => {
    expect(menuCards(menu).map((c) => openPath(c))).toEqual([
      '/tutorial/basics?restart',
      '/tutorial/terms?restart',
      '/tutorial/false-positives',
      '/tutorial/stopped',
      '/tutorial/images',
    ])
    expect(openPath({ id: 'a b', state: 'done' })).toBe('/tutorial/a%20b?restart')
  })

  it('cannot start a chapter that misses what it needs, but goes on with one in progress, whose run opens as it stands', () => {
    const missing = (id: string, number: number, extra: Partial<TutorialCard> = {}) => card(id, number, { missing: ['ocr'], ...extra })
    const needs: TutorialMenu = {
      chapters: [card('basics', 1), missing('terms', 2, { done: true }), missing('stopped', 3), missing('images', 4, { phase: 2 }), card('live', 5, { done: true })],
      requested: null,
      current: { chapter: 'stopped', step: 1 },
    }
    expect(menuCards(needs).map((c) => [c.id, c.state, cannotStart(c)])).toEqual([
      ['basics', 'todo', false], // misses nothing
      ['terms', 'done', true], // Start again is a new run
      ['stopped', 'current', false], // Continue: it is started
      ['images', 'todo', true], // Start
      ['live', 'done', false],
    ])
  })

  it('opens the chapter named on the command line, if the menu has it, whatever its phase', () => {
    expect(requestedCard({ ...menu, requested: 'stopped' })?.id).toBe('stopped')
    expect(requestedCard({ ...menu, requested: 'images' })?.id).toBe('images') // of the second phase
    expect(requestedCard({ ...menu, requested: 'nope' })).toBeNull()
    expect(requestedCard(menu)).toBeNull()
  })
})

describe('where the tutorial leads', () => {
  const me = (tutorial: Me['tutorial']): Me => ({ version: '0', profile: 'balanced', port: 1, data_dir: '', recording: true, tutorial, docs: false })

  it('starts the sandbox at the tutorial and a real daemon at Live', () => {
    expect(homePath(me({ current: null }))).toBe('/tutorial')
    expect(homePath(me(null))).toBe('/live')
    expect(homePath(me(undefined))).toBe('/live')
    expect(homePath(undefined)).toBe('/live')
  })

  it('goes back to the chapter in progress, or to the menu', () => {
    expect(resumePath(me({ current: { chapter: 'terms', number: 2, title: 'Your terms', step: 1 } }))).toBe('/tutorial/terms')
    expect(resumePath(me({ current: null }))).toBe('/tutorial')
    expect(resumePath(me(null))).toBeNull()
  })

  it('offers the local reviewer only where it works: never in the sandbox, unless it says so', () => {
    expect(reviewerOffered(me(null))).toBe(true)
    expect(reviewerOffered(undefined)).toBe(true)
    expect(reviewerOffered(me({ current: null }))).toBe(false)
    expect(reviewerOffered(me({ current: null, reviewer: false }))).toBe(false)
    expect(reviewerOffered(me({ current: null, reviewer: true }))).toBe(true)
  })

  it("asks the local reviewer from the page (Try it, a recognizer's draft, a column's opinion) only on a real daemon: the sandbox refuses it, with the Tutorial Reviewer too", () => {
    expect(reviewerAskable(me(null))).toBe(true)
    expect(reviewerAskable(me(undefined))).toBe(true)
    expect(reviewerAskable(undefined)).toBe(true)
    expect(reviewerAskable(me({ current: null }))).toBe(false)
    expect(reviewerAskable(me({ current: null, reviewer: false }))).toBe(false)
    expect(reviewerAskable(me({ current: null, reviewer: true }))).toBe(false)
  })

  it("offers the identity switch only on a real daemon: the sandbox has nobody's identity, and refuses it", () => {
    expect(identityOffered(me(null))).toBe(true)
    expect(identityOffered(me(undefined))).toBe(true)
    expect(identityOffered(undefined)).toBe(true)
    expect(identityOffered(me({ current: null }))).toBe(false)
    expect(identityOffered(me({ current: null, reviewer: true }))).toBe(false)
  })

  it('says how to start the tutorial on a real daemon, whose /me has none; not before /me answered', () => {
    expect(tutorialElsewhere(me(null))).toBe(true)
    expect(tutorialElsewhere(me(undefined))).toBe(true)
    expect(tutorialElsewhere(me({ current: null }))).toBe(false)
    expect(tutorialElsewhere(undefined)).toBe(false) // loading, or the daemon does not answer: the page says
  })
})

describe("Latet's pane: its text lights up when it changes", () => {
  it('lights up for a new step, or a step whose result takes the place of what it asked, and not for a poll', () => {
    const asked = explainKey({ index: 1, explain: 'Click the card number and choose Not sensitive.' })
    const done = explainKey({ index: 1, explain: 'The agent then reads the file again.' })
    const next = explainKey({ index: 2, explain: 'The agent then reads the file again.' }) // the same words, another step
    expect(flashes(null, asked)).toBe(true) // from the chapter's intro to its first step
    expect(flashes(asked, done)).toBe(true)
    expect(flashes(done, next)).toBe(true)
    expect(flashes(done, done)).toBe(false) // read again, nothing new
    expect(flashes(undefined, asked)).toBe(false) // the page just opened on it: nothing changed before your eyes
    expect(flashes(asked, null)).toBe(false) // back to the intro (Start over): no step's text to look at
  })
})

describe('the agent pane', () => {
  const line = (step: number, text: string): TutorialLine => ({ step, type: 'agent', text })

  it('groups the lines of each step, in order', () => {
    const lines: TutorialLine[] = [
      { step: 0, type: 'prompt', text: 'p' },
      line(0, 'a'),
      { step: 0, type: 'tool', name: 'Read', summary: '.env', pending: false },
      { step: 1, type: 'file', path: 'config.local.json', content: '{}' },
      line(1, 'b'),
      line(3, 'c'),
    ]
    expect(groupLines(lines).map((g) => [g.step, g.lines.length])).toEqual([
      [0, 3],
      [1, 2],
      [3, 1],
    ])
    expect(groupLines([])).toEqual([])
  })

  it('keeps a step that comes back in its own group, as lines arrive in order', () => {
    expect(groupLines([line(0, 'a'), line(1, 'b'), line(0, 'c')]).map((g) => g.step)).toEqual([0, 1, 0])
  })

  it('keeps the busy button while its step runs, not while a request waits for you in Approvals', () => {
    type Steps = { readonly steps: readonly TutorialStep[] }
    const kept = (chapter: Steps, state: TutorialState, view: View) =>
      keepsButton(chapter, view, phaseOf(chapter, state, view), primaryAction(chapter, state, view))
    // A step to watch: Send it, then the same button, busy, until Next takes its place.
    const ready = stateOf(BASICS, [])
    expect(kept(BASICS, ready, at(BASICS, ready))).toBe(false) // it is Send it
    expect(kept(BASICS, ready, viewReducer(at(BASICS, ready), { type: 'send' }))).toBe(true)
    expect(kept(BASICS, stateOf(BASICS, [0]), at(BASICS, ready))).toBe(false) // it is Next
    // The Live Reviewer's step: while the request waits, its own Approve button sends it, not the step's.
    const live = chapterOf(step('do', { wait: wait('approve'), button: 'Send it' }))
    const held = stateOf(live, [], { busy: true, waiting: { step: 0, kind: 'approve', satisfied: false } })
    const sent = viewReducer(at(live, held), { type: 'send' })
    expect(phaseOf(live, held, sent)).toBe('waiting')
    expect(kept(live, held, sent)).toBe(false)
    // Decided: the agent goes on, and the button holds the focus for the one of what comes next.
    const decided = { ...held, waiting: { step: 0, kind: 'approve' as const, satisfied: true } }
    expect(phaseOf(live, decided, sent)).toBe('running')
    expect(kept(live, decided, sent)).toBe(true)
    // A step the user unblocked has no button to keep.
    const pinned = stateOf(BASICS, [0, 1], { busy: true, waiting: { step: 2, kind: 'pin', satisfied: false } })
    expect(kept(BASICS, pinned, at(BASICS, pinned))).toBe(false)
  })
})

describe('a 409 from the tutorial', () => {
  const state = stateOf(BASICS, [0])

  it('carries the state to go on from', () => {
    expect(conflictState(new ApiError(409, 'not this step', [], { error: 'not this step', state }))).toEqual(state)
  })

  it('is not one when it is another answer, or has no state', () => {
    expect(conflictState(new ApiError(404, 'not found', [], { error: 'x', state }))).toBeNull()
    expect(conflictState(new ApiError(409, 'busy', [], { error: 'busy' }))).toBeNull()
    expect(conflictState(new ApiError(409, 'busy'))).toBeNull()
    expect(conflictState(new Error('boom'))).toBeNull()
    expect(conflictState(null)).toBeNull()
  })
})

describe('opening a chapter', () => {
  const two = chapterOf(step('watch'), step('do', { wait: wait('term') }))
  const chapter: TutorialChapter = { ...card('terms', 2), agent: { name: 'Tutorial Agent', model: 'Tutorial Model' }, steps: two.steps }
  const finished = stateOf(two, [0, 1], { chapter: 'terms', end: { learned: ['a', 'b', 'c'], where: [], next: 'false-positives' } })
  const fresh = stateOf(two, [], { chapter: 'terms', run: 'r2' })
  const notStarted = new ApiError(409, 'not started', [], { error: 'not started', state: null })
  const still = () => new AbortController().signal
  const gone = () => {
    const left = new AbortController()
    left.abort()
    return left.signal
  }

  /** The tutorial's API as a stand-in: `terms` is the chapter in progress, finished, unless ``state`` says otherwise. */
  const standIn = (state: () => Promise<{ state: TutorialState }> = async () => ({ state: finished })) =>
    ({
      chapter: vi.fn(async () => chapter),
      state: vi.fn(state),
      start: vi.fn(async () => ({ chapter, state: fresh })),
    }) satisfies OpenApi

  it('opens a chapter where it stands, the end of a finished one included', async () => {
    const api = standIn()
    expect(await openChapter(api, 'terms', false, still(), 'en')).toEqual({ chapter, state: finished })
    expect(api.start).not.toHaveBeenCalled()
  })

  it('starts a chapter that is not the one in progress', async () => {
    const api = standIn(async () => {
      throw notStarted
    })
    expect(await openChapter(api, 'terms', false, still(), 'en')).toEqual({ chapter, state: fresh })
    expect(api.start).toHaveBeenCalledWith('terms', 'en')
  })

  it("asks for the chapter in the page's language, and starts it in that language", async () => {
    // The daemon answers a chapter that runs in the language it started in, whatever the page asks.
    const api = standIn(async () => {
      throw notStarted
    })
    await openChapter(api, 'terms', false, still(), 'it')
    expect(api.chapter).toHaveBeenCalledWith('terms', 'it', expect.any(AbortSignal))
    expect(api.start).toHaveBeenCalledWith('terms', 'it')
    const again = standIn()
    await openChapter(again, 'terms', true, still(), 'de')
    expect(again.chapter).toHaveBeenCalledWith('terms', 'de', expect.any(AbortSignal))
    expect(again.start).toHaveBeenCalledWith('terms', 'de')
  })

  it('starts a chapter over when asked (Start again), not showing the end of the run that finished', async () => {
    const api = standIn()
    expect(await openChapter(api, 'terms', true, still(), 'en')).toEqual({ chapter, state: fresh })
    expect(api.start).toHaveBeenCalledTimes(1)
    expect(api.state).not.toHaveBeenCalled()
  })

  it('fails the page on any other trouble, and starts nothing', async () => {
    const api = standIn(async () => {
      throw new ApiError(500, 'boom')
    })
    await expect(openChapter(api, 'terms', false, still(), 'en')).rejects.toThrow('boom')
    expect(api.start).not.toHaveBeenCalled()
  })

  it('opens a chapter that misses what it needs without starting it', async () => {
    const needs: TutorialChapter = { ...chapter, missing: ['ocr'] }
    const api = { ...standIn(async () => { throw notStarted }), chapter: vi.fn(async () => needs) } satisfies OpenApi
    expect(await openChapter(api, 'terms', false, still(), 'en')).toEqual({ chapter: needs, state: null })
    expect(api.start).not.toHaveBeenCalled()
  })

  it('does not start it over either (Start again), and opens one in progress where it stands, whatever it misses', async () => {
    const needs: TutorialChapter = { ...chapter, missing: ['ocr'] }
    const again = { ...standIn(), chapter: vi.fn(async () => needs) } satisfies OpenApi
    expect(await openChapter(again, 'terms', true, still(), 'en')).toEqual({ chapter: needs, state: null })
    expect(again.start).not.toHaveBeenCalled()
    // The sandbox keeps the reader it found: a chapter in progress is open, and the page shows it as it stands.
    const going = { ...standIn(), chapter: vi.fn(async () => needs) } satisfies OpenApi
    expect(await openChapter(going, 'terms', false, still(), 'en')).toEqual({ chapter: needs, state: finished })
    expect(going.start).not.toHaveBeenCalled()
  })

  it('looks again each time it is opened: a chapter that has what it needs now is started', async () => {
    const needs: TutorialChapter = { ...chapter, missing: ['ocr'] }
    const calls = [needs, chapter]
    const api = {
      ...standIn(async () => {
        throw notStarted
      }),
      chapter: vi.fn(async () => calls.shift() ?? chapter),
    } satisfies OpenApi
    expect(await openChapter(api, 'terms', false, still(), 'en')).toEqual({ chapter: needs, state: null })
    expect(await openChapter(api, 'terms', false, still(), 'en')).toEqual({ chapter, state: fresh })
    expect(api.start).toHaveBeenCalledTimes(1)
  })

  it('opens a chapter whose start says it needs what the sandbox lost since it was read as one that waits for it', async () => {
    // The models went between the two calls: the chapter said `missing: []`, and `start` answers 409 "needs ocr".
    const lost = new ApiError(409, 'needs ocr', [], { error: 'needs ocr', state: null })
    const opened = standIn(async () => {
      throw notStarted
    })
    opened.start.mockRejectedValue(lost)
    expect(await openChapter(opened, 'terms', false, still(), 'en')).toEqual({ chapter, state: null })
    expect(opened.start).toHaveBeenCalledTimes(1)
    // Start again, which does not read the state, is told the same.
    const again = standIn()
    again.start.mockRejectedValue(lost)
    expect(await openChapter(again, 'terms', true, still(), 'en')).toEqual({ chapter, state: null })
  })

  it("fails the page when its start is refused for another reason: a busy sandbox, a broken one, another 'needs'", async () => {
    const refused = (error: Error) => {
      const api = standIn(async () => {
        throw notStarted
      })
      api.start.mockRejectedValue(error)
      return api
    }
    await expect(openChapter(refused(new ApiError(409, 'busy', [], { error: 'busy', state: fresh })), 'terms', false, still(), 'en')).rejects.toThrow('busy')
    await expect(openChapter(refused(new ApiError(500, 'needs ocr')), 'terms', false, still(), 'en')).rejects.toThrow('needs ocr') // not a 409
    await expect(openChapter(refused(new ApiError(409, 'not needs ocr')), 'terms', false, still(), 'en')).rejects.toThrow('not needs ocr')
    await expect(openChapter(refused(new Error('boom')), 'terms', false, still(), 'en')).rejects.toThrow('boom')
  })

  it('starts nothing for a page already left', async () => {
    const over = standIn()
    await expect(openChapter(over, 'terms', true, gone(), 'en')).rejects.toThrow()
    const unstarted = standIn(async () => {
      throw notStarted
    })
    await expect(openChapter(unstarted, 'terms', false, gone(), 'en')).rejects.toThrow()
    expect(over.start).not.toHaveBeenCalled()
    expect(unstarted.start).not.toHaveBeenCalled()
  })
})
