// The tutorial page, as pure functions: where a chapter stands and what the user can do next, which
// key presses mean Next, what to point at in an exchange, and the menu. The page keeps a `View` with
// `viewReducer`, reads the daemon's `TutorialState`, and asks these functions what to show and do.
//
//   phaseOf       ready | running | shown | waiting | end
//   primaryAction the one thing the button (and Enter) does in that phase
//   autoSends     a `do` step whose wait is met sends `next` by itself ("the agent tries again")
//   pollDelay     when to read `GET state` again

import { ApiError } from '../api/client'
import type {
  ExchangeDetail,
  Me,
  Replacement,
  TutorialCard,
  TutorialChapter,
  TutorialLine,
  TutorialMenu,
  TutorialNext,
  TutorialShown,
  TutorialState,
  TutorialStep,
  TutorialStepResult,
  TutorialWaiting,
} from '../api/types'
import { blockDomId, RESPONSE_TEXT_ID, responseToolId } from './blocks'
import type { KeyFacts } from './keys'
import { knownValues, pairMarks, requestMarks, RESPONSE_TEXT_SCOPE, toolScope, type BlockMarks } from '../components/masked/marks'
import type { SpanInfo, TextMark } from '../components/masked/types'

/** How often the page reads the state while the agent works or a wait is the daemon's to check. */
export const POLL_MS = 1000

/**
 * The first read after a call answered `busy` (the daemon runs every step in the background and answers at
 * once, and most steps are done in a moment): then twice as long each time, up to `POLL_MS`.
 */
export const FIRST_POLL_MS = 150

/** The element id of the card that says why a request was stopped (the page that shows it sets it). */
export const WHY_ID = 'tutorial-why'

// ---- Where a chapter stands ----------------------------------------------------------------------------

type ChapterSteps = { readonly steps: readonly Pick<TutorialStep, 'button'>[] }

/**
 * ready: a step to start (its button); running: the agent works (or a call is out); shown: the step ran,
 * its explanation and what it shows are on the right; waiting: the user has something to do in the
 * dashboard; end: the chapter is over.
 */
export type Phase = 'ready' | 'running' | 'shown' | 'waiting' | 'end'

/** What the page itself keeps, besides the daemon's state. */
export interface View {
  /** How many steps the chapter has (`step` is that when the end is shown). */
  steps: number
  /** The step on screen. Next moves it on in the page only; the daemon runs a step when asked. */
  step: number
  /** The user opened the value a `pin` step asks for: the daemon cannot see that, the page can. */
  pinned: boolean
  /** A `next` call is out. */
  sending: boolean
  /** The last `next` call failed: nothing is sent by itself until the user tries again. */
  failed: boolean
  /** The state was read this many times since a call answered: the first reads come quickly. */
  reads: number
  /** The last read of the state failed (the daemon does not answer): the next one waits the usual time. */
  unread: boolean
}

/** The step a state leaves the page at: the one the daemon runs next, or the end of a finished chapter. */
function limit(steps: number, state: TutorialState): number {
  return state.finished ? steps : Math.min(state.next, steps)
}

/** A page opening on a chapter (first time, reload, started over): at the step the daemon is at. */
export function initialView(steps: number, state: TutorialState): View {
  return { steps, step: limit(steps, state), pinned: false, sending: false, failed: false, reads: 0, unread: false }
}

export type ViewEvent =
  | { type: 'send' } // the user pressed the step's button, or a wait was met: a `next` call goes out
  | { type: 'answer'; state: TutorialState } // that call answered
  | { type: 'poll'; state: TutorialState } // `GET state` answered
  | { type: 'unread' } // `GET state` failed
  | { type: 'failed' } // that call failed
  | { type: 'retry' } // the user tries again
  | { type: 'advance' } // Next: the page moves on to the next step
  | { type: 'pinned' } // the user opened the value the step asks for

export function viewReducer(view: View, event: ViewEvent): View {
  switch (event.type) {
    case 'send':
      return { ...view, sending: true, failed: false }
    case 'retry':
      return { ...view, failed: false }
    case 'failed':
      return { ...view, sending: false, failed: true }
    case 'unread':
      return { ...view, unread: true }
    case 'answer':
    case 'poll': {
      // Never ahead of the daemon (the chapter was started over in another tab), possibly behind (Next not pressed yet).
      const step = Math.min(view.step, limit(view.steps, event.state))
      const sending = event.type === 'answer' ? false : view.sending
      const reads = event.type === 'answer' ? 0 : view.reads + 1
      return { ...view, sending, reads, unread: false, step, pinned: step === view.step ? view.pinned : false }
    }
    case 'advance':
      return { ...view, step: Math.min(view.step + 1, view.steps), pinned: false, failed: false }
    case 'pinned':
      return { ...view, pinned: true }
  }
}

/** The wait of the step on screen, when the daemon has one for it. */
function waitingAt(state: TutorialState, view: View): TutorialWaiting | null {
  return state.waiting !== null && state.waiting.step === view.step ? state.waiting : null
}

/**
 * The daemon's trouble with ``step``, if the step is the one it is about: the step asked for last. The error stays
 * in the state until the next call, so it outlives a step that ran with it, and the step after is not its own.
 */
export function errorAt(state: TutorialState, step: number): string | null {
  return state.error !== null && state.error_step === step ? state.error : null
}

/**
 * Whether the user did what the step waits for: a pin is seen by the page, every other wait by the daemon.
 * A daemon `busy` with a pin step has the pin already (the page that sent it may have been reloaded since).
 */
export function waitMet(waiting: TutorialWaiting, view: Pick<View, 'pinned'>, busy = false): boolean {
  return waiting.kind === 'pin' ? view.pinned || busy : waiting.satisfied
}

export function phaseOf(chapter: ChapterSteps, state: TutorialState, view: View): Phase {
  if (view.step >= view.steps) return 'end'
  if (state.steps[view.step]?.ran) return 'shown'
  const started = view.sending || state.busy
  const startsByButton = chapter.steps[view.step]?.button != null
  const waiting = waitingAt(state, view)
  if (waiting !== null && !waitMet(waiting, view, state.busy)) {
    // A request the Live Reviewer holds is busy and waiting at once: the user acts. A step with a button
    // that is not started yet waits for the button first.
    return started || !startsByButton ? 'waiting' : 'ready'
  }
  if (started) return 'running'
  if (waiting !== null && !startsByButton) return 'running' // the wait is met: the page sends the call itself
  return 'ready'
}

/** A step that runs once the user did what it asked (a `do` step without a button): the agent tries again. */
export function triesAgain(chapter: { readonly steps: readonly Pick<TutorialStep, 'kind' | 'button'>[] }, view: Pick<View, 'step'>): boolean {
  const step = chapter.steps[view.step]
  return step?.kind === 'do' && step.button === null
}

/**
 * The prompt of the step on screen, typed in the agent pane and not sent yet: from the start of the step until the
 * daemon puts it in the log, the moment the agent sends it (a step tried again after a failed run has it there
 * already). A request the Live Reviewer holds is waiting for you and on its way at once: the prompt stays for the
 * round trip, so that the pane does not flicker.
 */
export function typedPrompt(chapter: { readonly steps: readonly Pick<TutorialStep, 'prompt'>[] }, state: TutorialState, view: View, phase: Phase): string | null {
  const prompt = chapter.steps[view.step]?.prompt
  const sent = state.lines.some((line) => line.step === view.step && line.type === 'prompt')
  const going = phase === 'ready' || phase === 'running' || (phase === 'waiting' && view.sending)
  return prompt && !sent && going ? prompt : null
}

/** No line: the same empty list each time, so that what is built from it is not built again. */
const NO_LINES: readonly TutorialLine[] = []

/**
 * The conversation the step on screen resumes from before Latet was on, as /resume shows it before anything is
 * sent: with the step's typed prompt, above it, until the daemon puts the step's lines in the log. They are the very
 * lines the log then holds, in the same place, so nothing moves when the step runs.
 */
export function resumedLines(
  chapter: { readonly steps: readonly Pick<TutorialStep, 'prompt' | 'resumed'>[] },
  state: TutorialState,
  view: View,
  phase: Phase,
): readonly TutorialLine[] {
  const resumed = chapter.steps[view.step]?.resumed ?? NO_LINES
  return resumed.length > 0 && typedPrompt(chapter, state, view, phase) !== null ? resumed : NO_LINES
}

/** A `do` step without a button, whose wait is met and not sent yet, and no failed run of it to look at first. */
function wouldAutoSend(chapter: ChapterSteps, state: TutorialState, view: View): boolean {
  const waiting = waitingAt(state, view)
  if (waiting === null || view.sending || state.busy || errorAt(state, view.step) !== null || state.steps[view.step]?.ran) return false
  if (chapter.steps[view.step]?.button != null) return false
  return waitMet(waiting, view)
}

/** The page sends `next` for this step by itself: the user did what it waited for. Not again after a failure. */
export function autoSends(chapter: ChapterSteps, state: TutorialState, view: View): boolean {
  return !view.failed && wouldAutoSend(chapter, state, view)
}

/** The body of the `next` call for the step on screen. */
export function nextBody(state: TutorialState, view: View): TutorialNext {
  const waiting = waitingAt(state, view)
  return waiting !== null && waiting.kind === 'pin' && view.pinned ? { step: view.step, seen: ['pin'] } : { step: view.step }
}

/** What the button and Enter do: run the step, move on, finish the chapter, go on to the next one or the menu. */
export type Action = 'send' | 'next' | 'finish' | 'retry' | 'chapter' | 'menu'

export function primaryAction(chapter: ChapterSteps, state: TutorialState, view: View): Action | null {
  if (view.failed && wouldAutoSend(chapter, state, view)) return 'retry'
  const phase = phaseOf(chapter, state, view)
  // The daemon could not run the step on screen: the user tries it again, nothing sends it by itself.
  if (errorAt(state, view.step) !== null && !state.busy && !view.sending && (phase === 'ready' || phase === 'running')) return 'retry'
  switch (phase) {
    case 'ready':
      return 'send'
    case 'shown':
      return view.step + 1 < view.steps ? 'next' : 'finish'
    case 'end':
      return state.end?.next ? 'chapter' : 'menu'
    case 'running':
    case 'waiting':
      return null
  }
}

/**
 * The step's button stays on screen, busy, while its step runs: the focus on it is kept, and the button of what comes
 * next takes its place (one that unmounts leaves the focus on the page). Not while a request the Live Reviewer holds
 * waits for you: its own Approve button sends it there. ``action``: what the button does now.
 */
export function keepsButton(chapter: ChapterSteps, view: Pick<View, 'step'>, phase: Phase, action: Action | null): boolean {
  return action === null && phase === 'running' && chapter.steps[view.step]?.button != null
}

/**
 * When to read `GET state` again: while the agent works (soon after a call answered, then less often; at the usual
 * pace while a read fails: a daemon that is gone is not asked every 150 ms), or once a second while a wait is the
 * daemon's to check. Not for a pin (the page sees it), nor for an approval while the step is not busy: the request
 * that waits for you is sent by the step's button, and held while the step runs. Null: not needed.
 */
export function pollDelay(state: TutorialState, view: Pick<View, 'sending' | 'reads' | 'unread'>): number | null {
  if (view.sending) return null // the call out will say
  if (state.busy) return view.unread ? POLL_MS : Math.min(FIRST_POLL_MS * 2 ** view.reads, POLL_MS)
  const waiting = state.waiting
  return waiting !== null && waiting.kind !== 'pin' && waiting.kind !== 'approve' && !waiting.satisfied ? POLL_MS : null
}

/** What the right pane shows for a step: its own result, or the nearest step before it that has one. */
export function visibleResult(state: TutorialState, step: number): TutorialStepResult | null {
  for (let i = Math.min(step, state.steps.length - 1); i >= 0; i--) {
    const result = state.steps[i]
    if (result && (result.ran || result.shown !== null || result.explain !== '')) return result
  }
  return null
}

/** What Latet's pane says, as it lights up when it changes: the step it is of, and its words. Null: the intro. */
export function explainKey(result: Pick<TutorialStepResult, 'index' | 'explain'> | null): string | null {
  return result === null ? null : `${result.index}\n${result.explain}`
}

/**
 * Whether the text of Latet's pane lights up: it changed (a new step's, or a step's result in the place of what it
 * asked), so the eye goes there. Not when the page just opened on it (`before` undefined: nothing changed before
 * your eyes), nor back at the intro.
 */
export function flashes(before: string | null | undefined, now: string | null): boolean {
  return before !== undefined && now !== null && now !== before
}

// ---- Enter -----------------------------------------------------------------------------------------------

/** An Enter that means Next: a plain, first press that nothing else took (not a button's, a dialog's, or a handler's). */
export function countsAsNext(facts: KeyFacts): boolean {
  return (
    facts.key === 'Enter' &&
    !facts.repeat &&
    !(facts.altKey || facts.ctrlKey || facts.metaKey || facts.shiftKey) &&
    !facts.defaultPrevented &&
    !facts.ownedByTarget &&
    !facts.dialogOpen
  )
}

/** What this key press does on this page: the primary action when it counts as Next, else nothing. */
export function enterAction(chapter: ChapterSteps, state: TutorialState, view: View, facts: KeyFacts): Action | null {
  return countsAsNext(facts) ? primaryAction(chapter, state, view) : null
}

// ---- What to point at ---------------------------------------------------------------------------------------

/** What the page makes pulse in the pane: marks (`data-pair` values) and elements (ids). */
export interface Target {
  pairs: string[]
  /** Where a value has no mark, because it is in clear: the block that holds it, or the card that explains. */
  blocks: string[]
}

function noTarget(): Target {
  return { pairs: [], blocks: [] }
}

/** The pairs of the marks that stand for `value` in a block (original on this computer), each once. */
function pairsOf(marks: readonly TextMark[], value: string): string[] {
  return [...new Set(marks.filter((mark) => mark.info.original === value).map((mark) => mark.info.pair))]
}

function inRequest(detail: ExchangeDetail, shown: TutorialShown, highlight: readonly string[]): Target {
  const marks = requestMarks(detail)
  const slots = shown.slot === undefined ? detail.request.slots.map((_, i) => i) : [shown.slot]
  const found = new Set<string>()
  const holding = new Set<number>() // the blocks that have one of the values, masked or in clear
  const clear = new Set<string>()
  // A block's marks on this computer's side; the sent side when the original is unknown.
  const marksOf = (block: BlockMarks | undefined): readonly TextMark[] => block?.original ?? block?.sent ?? []
  for (const value of highlight) {
    for (const i of slots) {
      const slot = detail.request.slots[i]
      if (!slot) continue
      const pairs = pairsOf(marksOf(marks[i]), value)
      for (const pair of pairs) found.add(pair)
      if (pairs.length > 0) holding.add(i)
      // No mark: the value was not masked, it is in the text.
      else if ((slot.original ?? slot.masked).includes(value)) {
        clear.add(blockDomId(i))
        holding.add(i)
      }
    }
  }
  if (shown.focus === 'why') {
    for (const i of slots) for (const mark of marksOf(marks[i])) if (mark.info.stopped) found.add(mark.info.pair)
  }
  if (shown.focus === 'block') {
    const wanted = shown.slot === undefined ? [...holding] : detail.request.slots[shown.slot] ? [shown.slot] : []
    return { pairs: [], blocks: wanted.map(blockDomId) }
  }
  return { pairs: [...found], blocks: [...clear] }
}

interface ResponsePart {
  masked: string
  restored: string
  spans: readonly Replacement[] | undefined
  scope: string
  id: string
}

function inResponse(detail: ExchangeDetail, shown: TutorialShown, highlight: readonly string[]): Target {
  const response = detail.response
  if (!response) return noTarget()
  const text: ResponsePart = {
    masked: response.text_masked,
    restored: response.text_restored,
    spans: response.text_spans,
    scope: RESPONSE_TEXT_SCOPE,
    id: RESPONSE_TEXT_ID,
  }
  const tools = response.tool_calls.map(
    (call, i): ResponsePart => ({ masked: call.args_masked, restored: call.args_restored, spans: call.spans, scope: toolScope(i), id: responseToolId(i) }),
  )
  // The step names the answer's text (-1), one tool call (its index), or none: all of them.
  const parts = shown.slot === undefined ? [text, ...tools] : shown.slot < 0 ? [text] : tools.slice(shown.slot, shown.slot + 1)
  const known = knownValues(requestMarks(detail))
  const marked = parts.map((part) => ({ part, agent: pairMarks(part.masked, part.restored, part.spans, known, part.scope).agent }))
  if (shown.focus === 'block') return { pairs: [], blocks: marked.map(({ part }) => part.id) }
  const found = new Set<string>()
  const blocks = new Set<string>()
  for (const value of highlight) {
    for (const { part, agent } of marked) {
      const pairs = pairsOf(agent, value)
      for (const pair of pairs) found.add(pair)
      if (pairs.length === 0 && part.restored.includes(value)) blocks.add(part.id)
    }
  }
  return { pairs: [...found], blocks: [...blocks] }
}

/**
 * What a step makes pulse in the exchange it shows: the marks of the values it highlights (the invented,
 * original ones), found with the same scopes the block and response views give their marks; the block or
 * part for a value in clear; for a stopped request, the mark of the value that stopped it. The part that
 * shows what the local reviewer found (`review`) and the one that shows the images (`images`) are the exchange's
 * request, so they point at the same: for the images, at the values of the text read in them.
 * Other parts (terms, your guidance, a command, the embedded Approvals...) point at nothing here.
 */
export function targetOf(detail: ExchangeDetail, shown: TutorialShown): Target {
  if (shown.component !== 'exchange' && shown.component !== 'why' && shown.component !== 'review' && shown.component !== 'images') return noTarget()
  const highlight = shown.highlight ?? []
  const target = shown.part === 'response' ? inResponse(detail, shown, highlight) : inRequest(detail, shown, highlight)
  if (shown.focus === 'why') target.blocks.push(WHY_ID)
  return target
}

/** "Find it later": where the normal dashboard shows what a step shows; null when it has no page of its own. */
export function laterPath(shown: TutorialShown): string | null {
  switch (shown.component) {
    case 'exchange':
    case 'why':
    case 'review':
    case 'images': // the exchange's page shows its images, and the text read in them
    case 'columns':
      return shown.exchange ? `/exchanges/${encodeURIComponent(shown.exchange)}` : null
    case 'terms':
      return '/rules#terms'
    case 'guidance':
      return '/rules#reviewer'
    case 'approvals':
      return '/approvals#texts'
    case 'changes':
      return '/approvals#settings'
    case 'command':
    case 'none':
      return null
  }
}

/** A path of this dashboard (`/live`, `/rules#terms`), the only kind of link a chapter's end may give. */
export function isDashboardPath(to: string): boolean {
  return to.startsWith('/') && !to.startsWith('//')
}

/** The value a pin step asks for was opened (clicked, or Enter on it): on either side of it. */
export function pinMatches(info: Pick<SpanInfo, 'pair'>, target: Target): boolean {
  return target.pairs.includes(info.pair)
}

// ---- The menu and the rest -------------------------------------------------------------------------------------

export type CardState = 'done' | 'current' | 'todo'

export interface MenuCard extends TutorialCard {
  state: CardState
  /** Its place among the chapters listed (1-based): the number shown, whatever the daemon numbers the others. */
  position: number
  total: number
}

/**
 * Every chapter of the menu, in order of their numbers (whatever their phase), each with where it stands: in
 * progress (the daemon has steps of it left to run; a chapter done and begun again is in progress), done, or still
 * to do.
 */
export function menuCards(menu: TutorialMenu): MenuCard[] {
  const cards = [...menu.chapters].sort((a, b) => a.number - b.number) // a copy: the menu is the page's state
  return cards.map((card, i) => ({
    ...card,
    position: i + 1,
    total: cards.length,
    state: menu.current?.chapter === card.id ? 'current' : card.done ? 'done' : 'todo',
  }))
}

/** The chapter to do next, which the menu stands out: the one in progress, else the first not done yet; none when all are done. */
export function nextCard(cards: readonly MenuCard[]): MenuCard | null {
  return cards.find((card) => card.state === 'current') ?? cards.find((card) => card.state === 'todo') ?? null
}

/** What the address of a chapter says when its menu card (Start again) asks for a new run. */
export const RESTART = 'restart'

/** Where a card leads: a chapter done starts a new run, any other opens as it stands (in progress: where it was). */
export function openPath(card: Pick<MenuCard, 'id' | 'state'>): string {
  const path = `/tutorial/${encodeURIComponent(card.id)}`
  return card.state === 'done' ? `${path}?${RESTART}` : path
}

/**
 * A chapter that misses what it needs (`missing`) cannot be started, new or again: the menu says what to install in
 * place of its button. One in progress goes on: its run opens as it stands, whatever it misses.
 */
export function cannotStart(card: Pick<MenuCard, 'state' | 'missing'>): boolean {
  return card.missing.length > 0 && card.state !== 'current'
}

/** A chapter and where it stands, as the page opens on it. */
export interface Opened {
  chapter: TutorialChapter
  /** Null: the chapter misses what it needs, and is not started. */
  state: TutorialState | null
}

/** The calls that open a chapter: the tutorial's API (`api.tutorial`), or a stand-in. */
export interface OpenApi {
  chapter(id: string, lang: string, signal?: AbortSignal): Promise<TutorialChapter>
  state(id: string, signal?: AbortSignal): Promise<{ state: TutorialState }>
  start(id: string, lang: string): Promise<Opened>
}

/**
 * The chapter and where it stands, in the page's language `lang` (one the sandbox runs comes in the language it
 * started in). One the sandbox is not running (the menu's Start, a link) is started in `lang`, and so is one asked
 * to begin again (`restart`: Start again): the sandbox still holds a chapter done, and its state is the end of that
 * run. A page already left starts nothing: the answer of the first call is awaited before anything is started, so a
 * page opened and closed at once (in development, the effects run twice) starts one run, not two. A chapter that
 * misses what it needs (`missing`: text recognition's models, which the sandbox looks for again at each call) is
 * not started, new or begun again: it opens with no state, and the page says what to install. So does one whose
 * start answers that it needs something (409 "needs ocr": what it needs went since the chapter was read). One in
 * progress opens as it stands.
 */
export async function openChapter(tutorial: OpenApi, id: string, restart: boolean, signal: AbortSignal, lang: string): Promise<Opened> {
  const chapter = await tutorial.chapter(id, lang, signal)
  if (!restart) {
    try {
      const { state } = await tutorial.state(id, signal)
      return { chapter, state }
    } catch (error) {
      // 409: not started. Anything else fails the page.
      if (!(error instanceof ApiError) || error.status !== 409) throw error
    }
  }
  if (chapter.missing.length > 0) return { chapter, state: null }
  signal.throwIfAborted()
  try {
    return await tutorial.start(id, lang)
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && error.message.startsWith('needs ')) return { chapter, state: null }
    throw error
  }
}

/** The chapter named on the command line (`latet tutorial terms`), when the menu lists it. */
export function requestedCard(menu: TutorialMenu): MenuCard | null {
  return menuCards(menu).find((card) => card.id === menu.requested) ?? null
}

/** Where the dashboard opens: the tutorial in its sandbox, Live on a real daemon. */
export function homePath(me: Me | undefined): string {
  return me?.tutorial ? '/tutorial' : '/live'
}

/** Back to the chapter in progress (or the menu), for the bar every page of the sandbox shows; null outside it. */
export function resumePath(me: Me | undefined): string | null {
  const tutorial = me?.tutorial
  if (!tutorial) return null
  return tutorial.current ? `/tutorial/${encodeURIComponent(tutorial.current.chapter)}` : '/tutorial'
}

/**
 * The local reviewer works on a real daemon. The sandbox refuses it, but in a chapter that has the Tutorial Reviewer
 * (a simulated one): `/me` says so then, and the reader may change its guidance only.
 */
export function reviewerOffered(me: Me | undefined): boolean {
  return !me?.tutorial || me.tutorial.reviewer === true
}

/**
 * The page asks the local reviewer itself (Try it, a recognizer's draft, a column's opinion) on a real daemon only: the
 * sandbox refuses each (409), in the Tutorial Reviewer's chapter too, where Rules offers your guidance alone.
 */
export function reviewerAskable(me: Me | undefined): boolean {
  return !me?.tutorial
}

/** Your identity is masked on a real daemon; the sandbox has nobody's, and refuses to turn it on (409). */
export function identityOffered(me: Me | undefined): boolean {
  return !me?.tutorial
}

/**
 * The tutorial's pages on a real daemon (its `/me` has no `tutorial`): they say how to start it, as their routes
 * exist in the sandbox only. Not while `/me` loads or fails: the page says what it can.
 */
export function tutorialElsewhere(me: Me | undefined): boolean {
  return me !== undefined && !me.tutorial
}

export interface LineGroup {
  step: number
  lines: TutorialLine[]
}

/** The agent pane's lines grouped by step, in order. */
export function groupLines(lines: readonly TutorialLine[]): LineGroup[] {
  const groups: LineGroup[] = []
  for (const line of lines) {
    const last = groups[groups.length - 1]
    if (last && last.step === line.step) last.lines.push(line)
    else groups.push({ step: line.step, lines: [line] })
  }
  return groups
}

function isState(value: unknown): value is TutorialState {
  if (typeof value !== 'object' || value === null) return false
  const { chapter, steps } = value as Partial<TutorialState>
  return typeof chapter === 'string' && Array.isArray(steps)
}

/** The state a tutorial 409 carries (not this step, waiting, busy, not started), to go on from. */
export function conflictState(error: unknown): TutorialState | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const state = (error.body as { state?: unknown } | null | undefined)?.state
  return isState(state) ? state : null
}
